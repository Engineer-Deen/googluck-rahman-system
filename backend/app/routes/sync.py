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
import requests

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
PROVISIONING_STATE_KEY = "provisioning_state"


def _check_sync_key() -> bool:
    key = request.headers.get("X-Sync-Key", "")
    expected = current_app.config.get("SYNC_API_KEY", "")
    return bool(expected) and key == expected


DEVICE_LAST_SEEN_REFRESH_SECONDS = 300


def _seen_recently(value, now):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return 0 <= (now - value).total_seconds() < DEVICE_LAST_SEEN_REFRESH_SECONDS


def _get_bound_device(device_id: str):
    if not device_id:
        return None
    if _uses_firestore():
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        device = service.get_device(device_id)
        if not device or device.get("shop_id") is None or device.get("authorized", True) is False:
            return None
        # Only refresh last_seen_at every few minutes. Writing it on every
        # request cost a Firestore write plus a re-read per sync call.
        now = datetime.now(timezone.utc)
        if _seen_recently(device.get("last_seen_at"), now):
            return device
        return service.save_device(device_id, last_seen_at=now)
    device = db.session.get(Device, device_id)
    if not device or device.shop_id is None:
        return None
    device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return device


def _device_error():
    return jsonify(error="This device is not registered to an authorized shop"), 403


def _device_value(device, key, default=None):
    if isinstance(device, dict):
        return device.get(key, default)
    return getattr(device, key, default)


def _provisioning_state():
    if _uses_firestore():
        return "READY"
    row = db.session.get(SyncState, PROVISIONING_STATE_KEY)
    return row.value if row else "NOT_ENROLLED"


def _set_provisioning_state(value):
    if _uses_firestore():
        return
    row = db.session.get(SyncState, PROVISIONING_STATE_KEY)
    if not row:
        row = SyncState(key=PROVISIONING_STATE_KEY)
        db.session.add(row)
    row.value = value


def _effective_provisioning_state(device=None):
    if _uses_firestore():
        return "READY"
    state = _provisioning_state()
    if state in {"READY", "SYNC_ERROR"}:
        return state
    if device is None:
        device = db.session.get(Device, get_current_device_id())
    return "ENROLLED / PROVISIONING" if device and device.shop_id is not None else "NOT_ENROLLED"


def _sync_error_kind(message):
    if not message:
        return None
    lowered = message.lower()
    if "not configured on this device" in lowered:
        return "SYNC_API_KEY_MISSING"
    if "401" in lowered or "invalid or missing sync key" in lowered:
        return "SYNC_AUTH_FAILED"
    if "403" in lowered or "not registered to an authorized shop" in lowered:
        return "DEVICE_NOT_AUTHORIZED"
    if "timed out" in lowered or "connection" in lowered or "name or service" in lowered:
        return "RENDER_UNREACHABLE"
    return "SYNC_FAILED"


def _uses_firestore():
    return current_app.config.get("GLR_MODE") == "central"


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
                    if payload.get("device_id") not in (None, _device_value(device, "id")) or payload.get("shop_id") != _device_value(device, "shop_id"):
                        raise ValueError("Sale shop does not match the registered device shop")
                    payload["device_id"] = _device_value(device, "id")
                elif table_name == "stock_movements":
                    if payload.get("device_id") not in (None, _device_value(device, "id")) or payload.get("shop_id") != _device_value(device, "shop_id"):
                        raise ValueError("Stock movement shop does not match the registered device shop")
                    payload["device_id"] = _device_value(device, "id")
                result_extra = firestore_service.push_item(device, table_name, payload)
                results.append({"outbox_id": outbox_id, "status": "ok", **result_extra})
                continue
            if table_name == "sales":
                if payload.get("device_id") not in (None, _device_value(device, "id")) or payload.get("shop_id") != _device_value(device, "shop_id"):
                    raise ValueError("Sale shop does not match the registered device shop")
                payload["device_id"] = _device_value(device, "id")
                payload["assign_invoice"] = True
                payload["validate_stock"] = False
                sale, _, _ = apply_sale(payload)
                result_extra = {"invoice_number": sale.invoice_number}
            elif table_name == "sale_payments":
                sale = db.session.get(Sale, payload.get("sale_id"))
                if not sale or sale.shop_id != _device_value(device, "shop_id"):
                    raise ValueError("Payment sale does not match the registered device shop")
                if payload.get("device_id") not in (None, _device_value(device, "id")):
                    raise ValueError("Payment device does not match the registered device")
                payload["device_id"] = _device_value(device, "id")
                sale, _, _ = apply_payment(payload)
                result_extra = {}
            elif table_name == "stock_movements":
                if payload.get("device_id") not in (None, _device_value(device, "id")) or payload.get("shop_id") != _device_value(device, "shop_id"):
                    raise ValueError("Stock movement shop does not match the registered device shop")
                payload["device_id"] = _device_value(device, "id")
                apply_stock_movement(payload)
            else:
                results.append(
                    {"outbox_id": outbox_id, "status": "error", "error": f"Unknown table_name '{table_name}'"}
                )
                continue

            results.append({"outbox_id": outbox_id, "status": "ok", **result_extra})

        except ValueError as e:
            if not firestore_service:
                db.session.rollback()
            results.append({"outbox_id": outbox_id, "status": "error", "error": str(e)})
        except Exception as e:
            if not firestore_service:
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
    shop_id = _device_value(device, "shop_id")

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
    if _uses_firestore():
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        if not service.get_shop(requested_shop_id):
            return jsonify(error="The selected shop does not exist"), 400
        device = service.save_device(
            device_id,
            shop_id=requested_shop_id,
            name=data.get("name"),
            platform=data.get("platform"),
            authorized=True,
            registered_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        return jsonify(
            id=device.get("id", device_id), shop_id=device.get("shop_id"),
            name=device.get("name"), platform=device.get("platform")
        ), 201

    device = db.session.get(Device, device_id)
    if not device:
        device = Device(id=device_id)
        db.session.add(device)
    device.shop_id = requested_shop_id
    device.name = data.get("name")
    device.platform = data.get("platform")
    device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(id=device.id, shop_id=device.shop_id, name=device.name, platform=device.platform), 201


@sync_bp.get("/provisioning/status")
def provisioning_status():
    """Expose non-secret local enrollment state before local login."""
    if current_app.config["GLR_MODE"] != "local":
        return jsonify(state="READY", device_id=None, shop_id=None)

    device_id = get_current_device_id()
    device = db.session.get(Device, device_id)
    state = _effective_provisioning_state(device)
    return jsonify(
        state=state,
        device_id=device_id,
        shop_id=device.shop_id if device else None,
        last_pull_error=db.session.get(SyncState, "last_pull_error").value if db.session.get(SyncState, "last_pull_error") else None,
        staff_needing_provisioning=int((db.session.get(SyncState, "last_pull_skipped_staff").value if db.session.get(SyncState, "last_pull_skipped_staff") else "0") or "0"),
    )


@sync_bp.post("/provisioning/enroll")
def enroll_local_device():
    """Authorize this device centrally, then create its local offline identity."""
    if current_app.config["GLR_MODE"] != "local":
        return jsonify(error="Desktop enrollment is only available on local devices"), 400
    if not current_app.config.get("SYNC_API_KEY"):
        return jsonify(error="Cloud synchronization is not configured on this device"), 503

    data = request.get_json(silent=True) or {}
    central_email = str(data.get("central_email") or "").strip().lower()
    central_password = str(data.get("central_password") or "")
    if not central_email or not central_password:
        return jsonify(error="Central owner/admin authorization is required"), 400

    device_id = get_current_device_id()
    central_url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/")
    try:
        login_response = requests.post(
            central_url + "/api/auth/login",
            json={
                "email": central_email,
                "password": central_password,
                "role_group": "owner",
            },
            timeout=8,
        )
        if login_response.status_code in (401, 403):
            return jsonify(error="Central owner/admin authentication failed"), 401
        login_response.raise_for_status()
        login_data = login_response.json()
        identity = login_data.get("staff") or {}
        central_token = login_data.get("token")
        if not central_token:
            return jsonify(error="Central authentication did not return an authorization token"), 502
        if identity.get("role") not in ("owner", "admin") or not identity.get("shop_id"):
            return jsonify(error="Central enrollment requires an active owner or administrator"), 403

        registration_response = requests.post(
            central_url + "/api/sync/devices",
            headers={"Authorization": "Bearer " + central_token},
            json={
                "device_id": device_id,
                "shop_id": int(identity["shop_id"]),
                "name": data.get("name") or "Good Luck Rahman Main Device",
                "platform": data.get("platform") or "Unknown",
            },
            timeout=8,
        )
        registration_response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            return jsonify(error="Central owner/admin authorization was rejected"), 403
        current_app.logger.warning("Central desktop enrollment failed: %s", type(exc).__name__)
        return jsonify(error="Central enrollment service is unavailable"), 503

    staff_id = int(identity["id"])
    email = str(identity["email"]).strip().lower()
    shop_id = int(identity["shop_id"])
    local_staff = db.session.get(Staff, staff_id)
    email_staff = Staff.query.filter_by(email=email).first()
    if email_staff and email_staff.id != staff_id:
        return jsonify(error="The central identity conflicts with a local account"), 409
    if local_staff and (local_staff.email != email or local_staff.role not in ("owner", "admin")):
        return jsonify(error="The central identity conflicts with a local account"), 409
    if not local_staff:
        local_staff = Staff(id=staff_id, email=email)
        db.session.add(local_staff)

    local_staff.shop_id = shop_id
    local_staff.name = identity.get("name") or email
    local_staff.role = identity["role"]
    local_staff.is_active = bool(identity.get("is_active", True))
    # Authentication is central-only. Keep the legacy column inert for old
    # SQLite schemas; /api/auth/login never checks it in local mode.
    local_staff.password_hash = ""

    device = db.session.get(Device, device_id)
    if not device:
        device = Device(id=device_id)
        db.session.add(device)
    device.shop_id = shop_id
    device.name = data.get("name") or "Good Luck Rahman Main Device"
    device.platform = data.get("platform") or "Unknown"
    device.last_seen_at = datetime.now(timezone.utc)
    _set_provisioning_state("ENROLLED / PROVISIONING")
    db.session.commit()

    from app.sync.worker import pull_reference_data_once
    pull_reference_data_once(current_app._get_current_object())
    pull_error = db.session.get(SyncState, "last_pull_error")
    if pull_error and pull_error.value:
        _set_provisioning_state("SYNC_ERROR")
        db.session.commit()
        return jsonify(error="Initial provisioning could not complete", state="SYNC_ERROR"), 503

    _set_provisioning_state("READY")
    db.session.commit()
    return jsonify(
        state="READY",
        staff={"id": staff_id, "email": email, "role": identity["role"], "shop_id": shop_id},
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
    last_error = states.get("last_sync_error") or states.get("last_pull_error")
    return jsonify(
        mode=current_app.config["GLR_MODE"],
        provisioning_state=_effective_provisioning_state() if current_app.config["GLR_MODE"] == "local" else "READY",
        pending_count=pending,
        needs_review_count=needs_review,
        device_id=device_id,
        last_sync_at=states.get("last_sync_at"),
        last_sync_error=last_error,
        sync_error_kind=_sync_error_kind(last_error),
        last_pull_at=states.get("last_pull_success"),
        staff_needing_provisioning=int((states.get("last_pull_skipped_staff") or "0") or "0"),
    )
