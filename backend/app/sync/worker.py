"""Durable background synchronization for local/offline devices."""
import json
import threading
from datetime import datetime, timezone

import requests
from flask import current_app

from app.extensions import db
from app.models import (
    Product, Shop, Staff, SystemSetting, Sale, SaleItem, SalePayment, StockMovement,
    SyncOutboxItem, SyncState,
)
from app.sync.device import get_current_device_id

BATCH_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 8
LAST_PULL_KEY = "last_pull_at"


def _parse_datetime(value):
    """Deserialize central ISO-8601 timestamps for SQLAlchemy DateTime fields.

    The central API emits ``datetime.isoformat()`` values. The existing models
    use timezone-naive ``DateTime`` columns but create timestamps in UTC, so
    normalize aware input to UTC before binding it. This preserves the instant
    even on SQLite, whose DateTime storage does not retain an offset. Naive
    legacy values remain naive. ``Z`` is normalized for Python versions where
    ``fromisoformat`` does not accept it.
    """
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("Expected an ISO-8601 timestamp string or null")

    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed


def _set_state(key, value):
    row = db.session.get(SyncState, key)
    if not row:
        row = SyncState(key=key)
        db.session.add(row)
    row.value = value


def push_pending_once(app) -> dict:
    with app.app_context():
        items = (SyncOutboxItem.query.filter_by(status="pending")
                 .order_by(SyncOutboxItem.created_at.asc()).limit(BATCH_SIZE).all())
        if not current_app.config.get("SYNC_API_KEY"):
            _set_state("last_sync_error", "Cloud synchronization is not configured on this device.")
            db.session.commit()
            return {"pushed": 0, "confirmed": 0, "failed": len(items)}
        if not items:
            return {"pushed": 0, "confirmed": 0, "failed": 0}

        device_id = get_current_device_id()
        batch = [{
            "outbox_id": item.id,
            "table_name": item.table_name,
            "record_id": item.record_id,
            "payload": json.loads(item.payload_json),
        } for item in items]

        url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/") + "/api/sync/push"
        headers = {
            "X-Sync-Key": current_app.config["SYNC_API_KEY"],
            "X-Device-ID": device_id,
        }
        try:
            resp = requests.post(url, json={"device_id": device_id, "items": batch}, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.RequestException as exc:
            for item in items:
                item.attempt_count = (item.attempt_count or 0) + 1
                item.last_attempt_at = datetime.now(timezone.utc)
                item.last_error = str(exc)
            _set_state("last_sync_error", str(exc))
            db.session.commit()
            return {"pushed": 0, "confirmed": 0, "failed": len(items)}

        by_id = {r.get("outbox_id"): r for r in results}
        confirmed = failed = 0
        for item in items:
            result = by_id.get(item.id) or {}
            if result.get("status") == "ok":
                if item.table_name == "sales" and result.get("invoice_number"):
                    sale = db.session.get(Sale, item.record_id)
                    if sale and sale.invoice_number is None:
                        # Local SQLite can already contain another sale with the
                        # same invoice number from an earlier sync/replay. In that
                        # case, the worker must acknowledge the central insert
                        # without trying to overwrite the local row with a
                        # duplicate invoice value; a later pull will reconcile
                        # the authoritative invoice back into this device.
                        existing = (
                            Sale.query.filter(Sale.id != sale.id, Sale.invoice_number == result["invoice_number"])
                            .first()
                        )
                        if not existing:
                            sale.invoice_number = result["invoice_number"]
                db.session.delete(item)
                confirmed += 1
            else:
                item.attempt_count = (item.attempt_count or 0) + 1
                item.last_attempt_at = datetime.now(timezone.utc)
                item.last_error = result.get("error", "Unknown synchronization error")
                # Business-rule conflicts should stop retrying forever and
                # remain visible to the owner for reconciliation.
                if result.get("status") == "error" and item.attempt_count >= 3:
                    error_text = item.last_error.lower()
                    if any(x in error_text for x in ("exceeds", "unknown sale", "not enough stock", "conflict")):
                        item.status = "needs_review"
                failed += 1

        _set_state("last_sync_at", datetime.now(timezone.utc).isoformat())
        _set_state("last_sync_error", "")
        db.session.commit()
        return {"pushed": len(items), "confirmed": confirmed, "failed": failed}


def _upsert_transactions(data):
    # Remote sales are authoritative. Existing local sales keep their UUID;
    # central invoice/status/timestamp changes are applied without creating
    # duplicate rows. Relationships are loaded explicitly for speed.
    for raw in data.get("sales", []):
        sale = db.session.get(Sale, raw["id"])
        if not sale:
            sale = Sale(id=raw["id"])
            db.session.add(sale)
        sale.invoice_number = raw.get("invoice_number")
        sale.shop_id = raw.get("shop_id")
        sale.device_id = raw.get("device_id")
        sale.staff_id = raw.get("staff_id")
        sale.customer_name = raw.get("customer_name")
        sale.payment_method = raw.get("payment_method", "cash")
        sale.total_amount = raw.get("total_amount", 0)
        sale.created_at = _parse_datetime(raw.get("created_at"))
        sale.updated_at = _parse_datetime(raw.get("updated_at") or raw.get("created_at"))
        sale.server_received_at = _parse_datetime(raw.get("server_received_at"))
        sale.voided_at = _parse_datetime(raw.get("voided_at"))
        sale.voided_by_staff_id = raw.get("voided_by_staff_id")
        sale.void_reason = raw.get("void_reason")

    for raw in data.get("sale_items", []):
        if not db.session.get(Product, raw.get("product_id")):
            # Parent product must arrive in the same pull before child rows.
            continue
        item = db.session.get(SaleItem, raw["id"])
        if not item:
            item = SaleItem(id=raw["id"])
            db.session.add(item)
        item.sale_id = raw["sale_id"]
        item.product_id = raw["product_id"]
        item.quantity = raw["quantity"]
        item.unit_price = raw["unit_price"]
        item.subtotal = raw["subtotal"]
        item.unit_cost = raw.get("unit_cost", 0)

    for raw in data.get("payments", []):
        payment = db.session.get(SalePayment, raw["id"])
        if not payment:
            payment = SalePayment(id=raw["id"])
            db.session.add(payment)
        payment.sale_id = raw["sale_id"]
        payment.amount = raw["amount"]
        payment.device_id = raw.get("device_id")
        payment.staff_id = raw.get("staff_id")
        payment.created_at = _parse_datetime(raw.get("created_at"))
        payment.updated_at = _parse_datetime(raw.get("updated_at") or raw.get("created_at"))
        payment.server_received_at = _parse_datetime(raw.get("server_received_at"))

    for raw in data.get("stock_movements", []):
        if not db.session.get(Product, raw.get("product_id")):
            continue
        movement = db.session.get(StockMovement, raw["id"])
        if not movement:
            movement = StockMovement(id=raw["id"])
            db.session.add(movement)
        movement.product_id = raw["product_id"]
        movement.shop_id = raw.get("shop_id")
        movement.device_id = raw.get("device_id")
        movement.quantity_delta = raw["quantity_delta"]
        movement.reason = raw["reason"]
        movement.reference_id = raw.get("reference_id")
        movement.created_at = _parse_datetime(raw.get("created_at"))
        movement.updated_at = _parse_datetime(raw.get("updated_at") or raw.get("created_at"))
        movement.server_received_at = _parse_datetime(raw.get("server_received_at"))


def pull_reference_data_once(app) -> dict:
    with app.app_context():
        if not current_app.config.get("SYNC_API_KEY"):
            _set_state("last_pull_error", "Cloud synchronization is not configured on this device.")
            db.session.commit()
            return {"shops": 0, "staff": 0, "products": 0, "sales": 0, "payments": 0, "stock_movements": 0}
        state = db.session.get(SyncState, LAST_PULL_KEY)
        since = state.value if state else None
        url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/") + "/api/sync/pull"
        headers = {
            "X-Sync-Key": current_app.config["SYNC_API_KEY"],
            "X-Device-ID": get_current_device_id(),
        }
        params = {"since": since} if since else {}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _set_state("last_pull_error", str(exc))
            db.session.commit()
            return {"shops": 0, "staff": 0, "products": 0, "sales": 0, "payments": 0, "stock_movements": 0}

        for raw in data.get("shops", []):
            shop = db.session.get(Shop, raw["id"])
            if not shop:
                shop = Shop(id=raw["id"])
                db.session.add(shop)
            shop.name = raw["name"]
            shop.location = raw.get("location")
            shop.logo_data = raw.get("logo_data")

        skipped_staff = 0
        for raw in data.get("staff", []):
            staff = db.session.get(Staff, raw["id"])
            if not staff:
                # Authentication secrets are intentionally absent from sync
                # payloads. A new account must be provisioned through the
                # authenticated account flow before it can be used offline.
                skipped_staff += 1
                continue

            # Keep local staff rows stable when the central payload is unchanged.
            # Re-writing the same values would advance updated_at, which would
            # invalidate any already-issued JWTs because login_required rechecks
            # the token against the current staff row timestamp.
            desired = {
                "shop_id": raw.get("shop_id"),
                "name": raw["name"],
                "email": raw["email"],
                "role": raw["role"],
                "is_active": raw["is_active"],
            }
            if (
                staff.shop_id == desired["shop_id"] and
                staff.name == desired["name"] and
                staff.email == desired["email"] and
                staff.role == desired["role"] and
                staff.is_active == desired["is_active"]
            ):
                continue

            staff.shop_id = desired["shop_id"]
            staff.name = desired["name"]
            staff.email = desired["email"]
            # Authentication secrets are never synchronized. Existing local
            # credentials remain intact; central account changes require the
            # normal authenticated login/update flow.
            staff.role = desired["role"]
            staff.is_active = desired["is_active"]

        for raw in data.get("settings", []):
            setting = SystemSetting.query.filter_by(key=raw["key"]).first()
            if not setting:
                setting = SystemSetting(key=raw["key"])
                db.session.add(setting)
            setting.value = raw.get("value")

        for raw in data.get("products", []):
            product = db.session.get(Product, raw["id"])
            if not product:
                product = Product(id=raw["id"])
                db.session.add(product)
            product.sku = raw["sku"]
            product.name = raw["name"]
            product.category = raw.get("category")
            product.unit_price = raw["unit_price"]
            product.cost_price = raw.get("cost_price", 0)
            product.is_active = raw.get("is_active", True)

        _upsert_transactions(data)
        if not state:
            state = SyncState(key=LAST_PULL_KEY)
            db.session.add(state)
        # New central servers send a high-water-mark cursor captured before
        # querying. Fall back to the legacy server_time only while upgrading
        # an older central server.
        next_cursor = data.get("next_cursor") or data.get("server_time")
        state.value = next_cursor
        _set_state("last_pull_success", next_cursor)
        _set_state("last_pull_error", "")
        _set_state("last_pull_skipped_staff", str(skipped_staff))
        db.session.commit()
        return {
            "shops": len(data.get("shops", [])),
            "staff": len(data.get("staff", [])),
            "products": len(data.get("products", [])),
            "settings": len(data.get("settings", [])),
            "sales": len(data.get("sales", [])),
            "payments": len(data.get("payments", [])),
            "stock_movements": len(data.get("stock_movements", [])),
            "staff_needing_provisioning": skipped_staff,
        }


def trigger_sync_soon(app):
    thread = threading.Thread(target=lambda: (push_pending_once(app), pull_reference_data_once(app)), daemon=True)
    thread.start()