"""
Runs on the central server. Handles two directions of sync:

  PUSH (POST /push): receives transactional records (sales, stock
  movements) pushed up by local devices' sync workers, and applies
  each idempotently -- see apply_sale / apply_stock_movement.

  PULL (GET /pull): serves reference data (shops, staff, products)
  DOWN to local devices, so a device always has an up-to-date,
  read-only copy to log in against and sell from, even fully offline.
  Incremental via ?since=<ISO timestamp> -- a device only gets what
  changed, not the whole table every time.

Auth here is a shared secret (X-Sync-Key header), not a staff JWT --
sync happens on a schedule with no staff necessarily logged in at that
moment. This is deliberately simple for now; if you want per-device
credentials instead of one shared key later, that's a small change to
_check_sync_key() plus a devices table lookup, not a redesign.
"""
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app.auth import login_required
from app.extensions import db
from app.models import Device, Product, Sale, SaleItem, SalePayment, Shop, Staff, SystemSetting, StockMovement, SyncOutboxItem, SyncState
from app.routes.sales import apply_payment, apply_sale
from app.routes.stock import apply_stock_movement
from app.sync.device import get_current_device_id

sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")


def _check_sync_key() -> bool:
    key = request.headers.get("X-Sync-Key", "")
    expected = current_app.config.get("SYNC_API_KEY", "")
    return bool(expected) and key == expected


def _ensure_device_registered(device_id: str):
    if not device_id:
        return
    device = Device.query.get(device_id)
    if not device:
        # Postgres enforces the foreign key from sales/stock_movements to
        # devices, so a device must exist here before we can insert
        # anything referencing it. Auto-register with minimal info; a
        # richer registration endpoint can fill in shop/name/platform
        # later without breaking this.
        device = Device(id=device_id)
        db.session.add(device)
    device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()


@sync_bp.post("/push")
def push():
    if not _check_sync_key():
        return jsonify(error="Invalid or missing sync key"), 401

    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    items = data.get("items") or []

    _ensure_device_registered(device_id)

    results = []
    for item in items:
        outbox_id = item.get("outbox_id")
        table_name = item.get("table_name")
        payload = item.get("payload") or {}

        try:
            result_extra = {}
            if table_name == "sales":
                payload["assign_invoice"] = True
                payload["validate_stock"] = False
                sale, _, _ = apply_sale(payload)
                result_extra = {"invoice_number": sale.invoice_number}
            elif table_name == "sale_payments":
                sale, _, _ = apply_payment(payload)
                result_extra = {}
            elif table_name == "stock_movements":
                apply_stock_movement(payload)
            else:
                results.append(
                    {"outbox_id": outbox_id, "status": "error", "error": f"Unknown table_name '{table_name}'"}
                )
                continue

            results.append({"outbox_id": outbox_id, "status": "ok", **result_extra})

        except ValueError as e:
            db.session.rollback()
            results.append({"outbox_id": outbox_id, "status": "error", "error": str(e)})
        except Exception as e:
            db.session.rollback()
            results.append({"outbox_id": outbox_id, "status": "error", "error": f"Unexpected error: {e}"})

    return jsonify(results=results)


@sync_bp.get("/pull")
def pull():
    if not _check_sync_key():
        return jsonify(error="Invalid or missing sync key"), 401

    since_raw = request.args.get("since")
    since = None
    if since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
        except ValueError:
            return jsonify(error="`since` must be an ISO timestamp"), 400

    def changed(query, model):
        if since:
            return query.filter(model.updated_at > since)
        return query

    shops = changed(Shop.query, Shop).all()
    staff = changed(Staff.query, Staff).all()
    products = changed(Product.query, Product).all()
    sales = changed(Sale.query, Sale).all()
    sale_ids = [s.id for s in sales]
    sale_items = SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids)).all() if sale_ids else []
    payments = changed(SalePayment.query, SalePayment).all()
    movements = changed(StockMovement.query, StockMovement).all()
    settings = changed(SystemSetting.query, SystemSetting).all()

    return jsonify(
        server_time=datetime.now(timezone.utc).isoformat(),
        shops=[
            {"id": s.id, "name": s.name, "location": s.location, "logo_data": s.logo_data}
            for s in shops
        ],
        # password_hash is included deliberately -- it's an already-salted
        # hash (never the plaintext password), and local devices need it
        # to verify logins while fully offline. Same principle as any
        # offline-capable auth cache.
        staff=[
            {
                "id": s.id,
                "shop_id": s.shop_id,
                "name": s.name,
                "email": s.email,
                "password_hash": s.password_hash,
                "quick_pin_hash": s.quick_pin_hash,
                "role": s.role,
                "is_active": s.is_active,
            }
            for s in staff
        ],
        settings=[{"key": s.key, "value": s.value, "updated_at": s.updated_at.isoformat() if s.updated_at else None} for s in settings],
        products=[
            {
                "id": p.id,
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "unit_price": str(p.unit_price),
                "cost_price": str(p.cost_price),
                "is_active": p.is_active,
            }
            for p in products
        ],
        sales=[{
            "id": s.id, "invoice_number": s.invoice_number, "shop_id": s.shop_id, "device_id": s.device_id,
            "staff_id": s.staff_id, "customer_name": s.customer_name, "payment_method": s.payment_method,
            "total_amount": str(s.total_amount), "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat() if s.updated_at else s.created_at.isoformat(),
            "server_received_at": s.server_received_at.isoformat() if s.server_received_at else None,
            "voided_at": s.voided_at.isoformat() if s.voided_at else None,
            "voided_by_staff_id": s.voided_by_staff_id, "void_reason": s.void_reason
        } for s in sales],
        sale_items=[{
            "id": i.id, "sale_id": i.sale_id, "product_id": i.product_id, "quantity": i.quantity,
            "unit_price": str(i.unit_price), "subtotal": str(i.subtotal), "unit_cost": str(i.unit_cost)
        } for i in sale_items],
        payments=[{
            "id": p.id, "sale_id": p.sale_id, "amount": str(p.amount), "device_id": p.device_id,
            "staff_id": p.staff_id, "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat() if p.updated_at else p.created_at.isoformat(),
            "server_received_at": p.server_received_at.isoformat() if p.server_received_at else None
        } for p in payments],
        stock_movements=[{
            "id": m.id, "product_id": m.product_id, "shop_id": m.shop_id, "device_id": m.device_id,
            "quantity_delta": m.quantity_delta, "reason": m.reason, "reference_id": m.reference_id,
            "created_at": m.created_at.isoformat(), "updated_at": m.updated_at.isoformat() if m.updated_at else m.created_at.isoformat(),
            "server_received_at": m.server_received_at.isoformat() if m.server_received_at else None
        } for m in movements],
    )


@sync_bp.post("/trigger")
@login_required
def trigger():
    from app.sync.worker import trigger_sync_soon, push_pending_once, pull_reference_data_once
    if current_app.config["GLR_MODE"] != "local":
        return jsonify(error="Manual sync is only needed on local devices"), 400
    trigger_sync_soon(current_app._get_current_object())
    return jsonify(message="Synchronization started")


@sync_bp.get("/status")
@login_required
def status():
    """
    For the frontend's sync indicator -- how many local records are
    still waiting to reach the central server, and this device's own
    identity. Local mode only in practice (central has nothing to
    report here since it IS the destination), but harmless either way.
    """
    from flask import current_app

    pending = SyncOutboxItem.query.filter_by(status="pending").count() if current_app.config["GLR_MODE"] == "local" else 0
    needs_review = SyncOutboxItem.query.filter_by(status="needs_review").count() if current_app.config["GLR_MODE"] == "local" else 0
    device_id = get_current_device_id() if current_app.config["GLR_MODE"] == "local" else None

    states = {s.key: s.value for s in SyncState.query.all()} if current_app.config["GLR_MODE"] == "local" else {}
    return jsonify(
        mode=current_app.config["GLR_MODE"],
        pending_count=pending,
        needs_review_count=needs_review,
        device_id=device_id,
        last_sync_at=states.get("last_sync_at"),
        last_sync_error=states.get("last_sync_error") or states.get("last_pull_error"),
        last_pull_at=states.get("last_pull_success"),
    )