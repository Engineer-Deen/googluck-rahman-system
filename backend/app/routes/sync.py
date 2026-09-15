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

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from app.auth import login_required, roles_required
from app.extensions import db
from app.models import Device, Product, Sale, SaleItem, SalePayment, Shop, Staff, SystemSetting, StockMovement, SyncOutboxItem, SyncState
from app.routes.sales import apply_payment, apply_sale
from app.routes.stock import apply_stock_movement
from app.sync.device import get_current_device_id

sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")

_PULL_MODELS = (Shop, Staff, Product, Sale, SalePayment, StockMovement, SystemSetting)


def _check_sync_key() -> bool:
    key = request.headers.get("X-Sync-Key", "")
    expected = current_app.config.get("SYNC_API_KEY", "")
    return bool(expected) and key == expected


def _get_bound_device(device_id: str):
    if not device_id:
        return None
    device = Device.query.get(device_id)
    if not device or device.shop_id is None:
        return None
    device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return device


def _device_error():
    return jsonify(error="This device is not registered to an authorized shop"), 403


def _uses_firestore():
    return current_app.config.get("CENTRAL_DATA_PROVIDER", "postgres") == "firestore"


def _parse_cursor(value: str):
    """Parse the ISO-8601 cursor format returned by this endpoint."""
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _pull_high_watermark(shop_id):
    """Return the latest committed update timestamp across pulled entities.

    Capture this from persisted data before reading any result rows. It is a
    data boundary, rather than a later application-server wall-clock value.
    """
    scoped_queries = [
        (Shop, (Shop.id == shop_id,)),
        (Staff, (Staff.shop_id == shop_id,)),
        (Product, ()),
        (Sale, (Sale.shop_id == shop_id,)),
        (SalePayment, (SalePayment.sale_id.in_(db.session.query(Sale.id).filter(Sale.shop_id == shop_id)),)),
        (StockMovement, (StockMovement.shop_id == shop_id,)),
        (SystemSetting, ()),
    ]
    values = [
        db.session.query(func.max(model.updated_at)).filter(*filters).scalar()
        for model, filters in scoped_queries
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


@sync_bp.post("/push")
def push():
    if not _check_sync_key():
        return jsonify(error="Invalid or missing sync key"), 401

    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    items = data.get("items") or []

    device = _get_bound_device(device_id)
    if not device:
        return _device_error()

    firestore_service = None
    if _uses_firestore():
        from app.firestore import get_firestore_sync_service
        firestore_service = get_firestore_sync_service()

    results = []
    for item in items:
        outbox_id = item.get("outbox_id")
        table_name = item.get("table_name")
        payload = item.get("payload") or {}

        try:
            result_extra = {}
            if firestore_service is not None:
                if table_name == "sales":
                    if payload.get("device_id") not in (None, device.id) or payload.get("shop_id") != device.shop_id:
                        raise ValueError("Sale shop does not match the registered device shop")
                    payload["device_id"] = device.id
                elif table_name == "stock_movements":
                    if payload.get("device_id") not in (None, device.id) or payload.get("shop_id") != device.shop_id:
                        raise ValueError("Stock movement shop does not match the registered device shop")
                    payload["device_id"] = device.id
                result_extra = firestore_service.push_item(device, table_name, payload)
                results.append({"outbox_id": outbox_id, "status": "ok", **result_extra})
                continue
            if table_name == "sales":
                if payload.get("device_id") not in (None, device.id) or payload.get("shop_id") != device.shop_id:
                    raise ValueError("Sale shop does not match the registered device shop")
                payload["device_id"] = device.id
                payload["assign_invoice"] = True
                payload["validate_stock"] = False
                sale, _, _ = apply_sale(payload)
                result_extra = {"invoice_number": sale.invoice_number}
            elif table_name == "sale_payments":
                sale = Sale.query.get(payload.get("sale_id"))
                if not sale or sale.shop_id != device.shop_id:
                    raise ValueError("Payment sale does not match the registered device shop")
                if payload.get("device_id") not in (None, device.id):
                    raise ValueError("Payment device does not match the registered device")
                payload["device_id"] = device.id
                sale, _, _ = apply_payment(payload)
                result_extra = {}
            elif table_name == "stock_movements":
                if payload.get("device_id") not in (None, device.id) or payload.get("shop_id") != device.shop_id:
                    raise ValueError("Stock movement shop does not match the registered device shop")
                payload["device_id"] = device.id
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

    device = _get_bound_device(request.headers.get("X-Device-ID", ""))
    if not device:
        return _device_error()
    shop_id = device.shop_id

    since_raw = request.args.get("since")
    since = None
    if since_raw:
        try:
            since = _parse_cursor(since_raw)
        except ValueError:
            return jsonify(error="`since` must be an ISO timestamp"), 400

    if _uses_firestore():
        from app.firestore import get_firestore_sync_service
        try:
            return jsonify(get_firestore_sync_service().pull(shop_id, since))
        except Exception as exc:
            current_app.logger.exception("Firestore pull failed")
            return jsonify(error=f"Central Firestore unavailable: {exc}"), 503

    # This endpoint is currently an unpaginated, high-water-mark-bounded pull.
    # If pagination is added later, every page must retain this same upper bound
    # until the client has consumed the complete snapshot.
    high_watermark = _pull_high_watermark(shop_id)

    def changed(query, model):
        if since:
            # Inclusive lower bound deliberately replays the cursor boundary.
            # A row committed after the prior read can share its timestamp; the
            # existing UUID/key upserts make that replay safe and prevent a
            # strict `>` comparison from permanently missing the row.
            query = query.filter(model.updated_at >= since)
        if high_watermark:
            query = query.filter(model.updated_at <= high_watermark)
        return query

    shops = changed(Shop.query.filter(Shop.id == shop_id), Shop).all()
    staff = changed(Staff.query.filter(Staff.shop_id == shop_id), Staff).all()
    sales = changed(Sale.query.filter(Sale.shop_id == shop_id), Sale).all()
    sale_ids = [s.id for s in sales]
    sale_items = SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids)).all() if sale_ids else []
    payments = changed(
        SalePayment.query.join(Sale, Sale.id == SalePayment.sale_id).filter(Sale.shop_id == shop_id),
        SalePayment,
    ).all()
    movements = changed(StockMovement.query.filter(StockMovement.shop_id == shop_id), StockMovement).all()
    referenced_product_ids = {m.product_id for m in movements} | {i.product_id for i in sale_items}
    shop_product_ids = (
        db.session.query(StockMovement.product_id)
        .filter(StockMovement.shop_id == shop_id)
        .distinct()
    )
    products_by_id = {
        p.id: p
        for p in changed(Product.query.filter(Product.id.in_(shop_product_ids)), Product).all()
    }
    if referenced_product_ids:
        for product in Product.query.filter(Product.id.in_(referenced_product_ids)).all():
            products_by_id.setdefault(product.id, product)
    products = list(products_by_id.values())
    settings = changed(SystemSetting.query, SystemSetting).all()

    return jsonify(
        # New clients persist this data-derived high-water mark. Keep
        # server_time for compatibility and status display only.
        next_cursor=high_watermark.isoformat() if high_watermark else since_raw,
        server_time=datetime.now(timezone.utc).isoformat(),
        shops=[
            {"id": s.id, "name": s.name, "location": s.location, "logo_data": s.logo_data}
            for s in shops
        ],
        staff=[
            {
                "id": s.id,
                "shop_id": s.shop_id,
                "name": s.name,
                "email": s.email,
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


@sync_bp.post("/devices")
@roles_required("owner", "admin")
def register_device():
    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or "").strip()
    if not device_id:
        return jsonify(error="device_id is required"), 400
    try:
        requested_shop_id = int(data.get("shop_id"))
    except (TypeError, ValueError):
        return jsonify(error="shop_id must be a valid shop id"), 400
    if g.staff_role != "owner" and requested_shop_id != g.staff_shop_id:
        return jsonify(error="Administrators can only register devices for their own shop"), 403
    if not Shop.query.get(requested_shop_id):
        return jsonify(error="The selected shop does not exist"), 400

    device = Device.query.get(device_id)
    if not device:
        device = Device(id=device_id)
        db.session.add(device)
    device.shop_id = requested_shop_id
    device.name = data.get("name")
    device.platform = data.get("platform")
    device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(id=device.id, shop_id=device.shop_id, name=device.name, platform=device.platform), 201


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
