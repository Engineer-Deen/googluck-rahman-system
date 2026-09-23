"""Durable background synchronization for local/offline devices."""
import json
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from flask import current_app

from app.extensions import db
from app.models import (
    Product, Shop, Staff, SystemSetting, Sale, SaleItem, SalePayment, StockMovement,
    SyncOutboxItem, SyncState,
)
from app.sync.device import get_current_device_id

SYNC_INTERVAL_SECONDS = 5      # push cadence -- costs nothing while the outbox is empty
PULL_INTERVAL_SECONDS = 60     # every pull costs central database reads, so keep it slow
MAX_BACKOFF_SECONDS = 15 * 60  # ceiling when the central server keeps failing
BATCH_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 30
LAST_PULL_KEY = "last_pull_at"


def _parse_datetime(value):
    """Deserialize central timestamps for SQLAlchemy DateTime fields.

    The central API is supposed to emit ``datetime.isoformat()`` values, but
    some write paths store a raw ``datetime`` in Firestore that later gets
    JSON-serialized by Flask's default encoder instead of being normalized
    through an explicit ``.isoformat()`` call first. Flask's default encoder
    formats ``datetime`` objects as an RFC 1123 / HTTP-date string (e.g.
    ``"Thu, 17 Sep 2026 08:46:04 GMT"``), not ISO-8601. Left unhandled, that
    crashes this parser on every pull that includes such a record, which
    prevents the pull from ever committing -- so the sync cursor never
    advances, and every subsequent pull re-fetches the same expensive
    "everything since the cursor" range instead of just new changes.

    The existing models use timezone-naive ``DateTime`` columns but create
    timestamps in UTC, so normalize aware input to UTC before binding it.
    This preserves the instant even on SQLite, whose DateTime storage does
    not retain an offset. Naive legacy values remain naive. ``Z`` is
    normalized for Python versions where ``fromisoformat`` does not accept
    it.
    """
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("Expected an ISO-8601 timestamp string or null")

    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        # Fall back to RFC 1123 / HTTP-date (what Flask's default JSON
        # encoder produces for a raw datetime it wasn't told to isoformat).
        parsed = parsedate_to_datetime(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed


def _set_state(key, value):
    row = db.session.get(SyncState, key)
    if not row:
        row = SyncState(key=key)
        db.session.add(row)
    row.value = value


def _last_pull_failed(app) -> bool:
    with app.app_context():
        row = db.session.get(SyncState, "last_pull_error")
        return bool(row and row.value)


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

        if failed == 0 and confirmed == len(items):
            # Record a successful upload only when every item in this batch
            # was acknowledged by central. A partial batch failure must not
            # overwrite the last error with a false success state.
            _set_state("last_sync_at", datetime.now(timezone.utc).isoformat())
            _set_state("last_sync_error", "")
        else:
            # Preserve a real push error so the UI can distinguish a queued
            # record that is retrying from a queue that is merely waiting.
            errors = [
                item.last_error
                for item in items
                if item.last_error
            ]
            _set_state(
                "last_sync_error",
                errors[0] if errors else "One or more queued changes were not acknowledged by the central server.",
            )

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

        try:
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
        except Exception as exc:
            # A single malformed record (e.g. an unparseable timestamp) must
            # never be allowed to crash the thread mid-transaction and leave
            # partially-applied staff/product/sale changes pending. That both
            # (a) stops the sync cursor from ever advancing -- so every future
            # pull re-fetches the same expensive "everything since the old
            # cursor" range forever -- and (b) risks flushing a dirty,
            # invalidating staff.updated_at write on a later unrelated commit.
            db.session.rollback()
            _set_state("last_pull_error", f"{type(exc).__name__}: {exc}")
            db.session.commit()
            return {"shops": 0, "staff": 0, "products": 0, "sales": 0, "payments": 0, "stock_movements": 0}

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

        # A device may have been enrolled successfully but failed its initial
        # pull because the central service was temporarily unavailable. Once a
        # later pull succeeds, recover the provisioning state automatically so
        # the device does not remain stuck in SYNC_ERROR forever.
        provisioning = db.session.get(SyncState, "provisioning_state")
        if provisioning and provisioning.value in {"ENROLLED / PROVISIONING", "SYNC_ERROR"}:
            provisioning.value = "READY"

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


def backoff_delay(base_seconds, consecutive_failures):
    """Wait `base` normally; double per consecutive failure, capped."""
    if consecutive_failures <= 0:
        return base_seconds
    return min(MAX_BACKOFF_SECONDS, base_seconds * (2 ** consecutive_failures))


def _last_pull_failed(app):
    with app.app_context():
        row = db.session.get(SyncState, "last_pull_error")
        return bool(row and row.value)


def start_background_sync(app):
    """
    Push pending sales promptly (cheap -- costs nothing while the outbox is
    empty). A successful online login performs the initial reference-data
    pull. After that, further pulls are event-driven only: they happen
    when trigger_sync_soon() is called following an actual mutating action
    (a sale, a staff/product/settings change, etc.), not on a recurring
    timer. A fixed-interval pull loop -- even throttled to 60s -- still
    reads Firestore continuously whether or not anyone is using the system,
    which is unnecessary cost for a single-shop POS: nothing changes on the
    server unless someone here or on another device did something.

    The one exception is retrying after a pull failure. A failed pull leaves
    last_pull_error set, which the UI's sync status reads directly -- with
    pulls otherwise event-driven, nothing would ever run again to clear it,
    so the status would show "central unavailable" forever even after
    central recovers, until the next unrelated action happened to trigger a
    pull. So: only while the last pull is in a failed state, retry it on a
    growing backoff (capped at PULL_INTERVAL_SECONDS). This is bounded and
    self-limiting -- it stops entirely the moment a pull succeeds -- unlike
    a fixed recurring loop, which keeps polling forever regardless of
    whether anything is actually wrong.
    """
    def loop():
        push_failures = pull_failures = 0
        next_push = next_pull_retry = 0.0
        while True:
            try:
                if app.config.get("GLR_MODE") == "local":
                    from app.auth import has_local_session
                    # The sidecar may stay alive while nobody is logged in.
                    # Synchronization is therefore completely idle until a
                    # successful online login establishes a local session.
                    if not has_local_session():
                        push_failures = pull_failures = 0
                        next_push = next_pull_retry = 0.0
                    else:
                        now = time.monotonic()
                        if now >= next_push:
                            result = push_pending_once(app)
                            push_failures = push_failures + 1 if (result.get("failed") and not result.get("confirmed")) else 0
                            next_push = now + backoff_delay(SYNC_INTERVAL_SECONDS, push_failures)
                        if pull_failures and now >= next_pull_retry:
                            pull_reference_data_once(app)
                            pull_failures = pull_failures + 1 if _last_pull_failed(app) else 0
                            next_pull_retry = now + backoff_delay(PULL_INTERVAL_SECONDS, pull_failures)
            except Exception:
                pass
            time.sleep(SYNC_INTERVAL_SECONDS)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def trigger_sync_soon(app):
    """Push queued local changes without forcing a central pull.

    Pulls are performed explicitly after successful login (and retried only
    after an actual pull failure). This keeps the status/pending-change path
    from turning into repeated Firestore reads while the POS is otherwise idle.
    """
    from app.auth import has_local_session
    if not has_local_session():
        return None
    thread = threading.Thread(
        target=lambda: push_pending_once(app),
        daemon=True,
        name="glr-push-trigger",
    )
    thread.start()
    return thread
