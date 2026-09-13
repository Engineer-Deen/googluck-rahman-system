"""
Direct stock adjustments that AREN'T caused by a sale: restocking new
inventory, or correcting a miscount. Sale-caused stock movements are
created automatically inside apply_sale() so they always stay in
lockstep with the sale that caused them.

Same idempotent-by-id pattern as sales: apply_stock_movement() is used
both by the HTTP route and by the central sync endpoint.
"""
from flask import Blueprint, current_app, g, jsonify, request

from app.auth import login_required, roles_required
from app.extensions import db
from app.models import Product, StockMovement
from app.models.transactions import gen_uuid
from app.routes.products import current_stock, serialize_product

stock_bp = Blueprint("stock", __name__, url_prefix="/api/stock-movements")

ALLOWED_REASONS = {"restock", "correction", "transfer_in", "transfer_out", "void_reversal"}


def apply_stock_movement(payload: dict):
    """
    payload shape:
    {
      "id": "...",              # required, client-generated
      "product_id": 1,
      "shop_id": 1,
      "device_id": "...",
      "quantity_delta": 50,
      "reason": "restock",
      "reference_id": None,
    }

    Returns (movement, created).
    """
    movement_id = payload.get("id")
    if not movement_id:
        raise ValueError("payload.id is required")

    existing = StockMovement.query.get(movement_id)
    if existing:
        return existing, False

    product_id = payload.get("product_id")
    reason = payload.get("reason")
    if not product_id or reason not in ALLOWED_REASONS:
        raise ValueError(
            f"product_id is required and reason must be one of {sorted(ALLOWED_REASONS)}"
        )

    if not Product.query.get(product_id):
        raise ValueError(f"Unknown product_id {product_id}")

    try:
        quantity_delta = int(payload["quantity_delta"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("quantity_delta must be a whole number")

    movement = StockMovement(
        id=movement_id,
        product_id=product_id,
        shop_id=payload.get("shop_id"),
        device_id=payload.get("device_id"),
        quantity_delta=quantity_delta,
        reason=reason,
        reference_id=payload.get("reference_id"),
    )
    db.session.add(movement)
    db.session.flush()
    if current_app.config.get("GLR_MODE") == "local":
        from app.sync.outbox import enqueue_outbox
        enqueue_outbox("stock_movements", movement.id, payload)
    db.session.commit()
    return movement, True


@stock_bp.post("")
@roles_required("owner", "admin", "manager")
def create_stock_movement():
    from flask import current_app

    from app.sync.outbox import enqueue_outbox

    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    if not device_id and current_app.config["GLR_MODE"] == "local":
        # See the matching comment in routes/sales.py create_sale() --
        # only local mode has a real device identity to auto-fill in.
        from app.sync.device import get_current_device_id
        device_id = get_current_device_id()

    payload = {
        "id": data.get("id") or gen_uuid(),
        "product_id": data.get("product_id"),
        "shop_id": g.staff_shop_id,
        "device_id": device_id,
        "quantity_delta": data.get("quantity_delta"),
        "reason": data.get("reason"),
        "reference_id": data.get("reference_id"),
    }

    try:
        movement, created = apply_stock_movement(payload)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    if created and current_app.config["GLR_MODE"] == "local":
        from app.sync.worker import trigger_sync_soon
        trigger_sync_soon(current_app._get_current_object())

    product = Product.query.get(payload["product_id"])
    shop_id = None if g.staff_role == "owner" else g.staff_shop_id
    return jsonify(
        id=movement.id,
        product=serialize_product(product, stock_value=current_stock(payload["product_id"], shop_id)),
        new_stock=current_stock(payload["product_id"], shop_id),
    ), 201 if created else 200


@stock_bp.get("/product/<int:product_id>")
@login_required
def stock_history(product_id):
    query = StockMovement.query.filter_by(product_id=product_id)
    if g.staff_role != "owner":
        query = query.filter_by(shop_id=g.staff_shop_id)
    movements = (
        query
        .order_by(StockMovement.created_at.desc())
        .limit(100)
        .all()
    )
    return jsonify([
        {
            "id": m.id,
            "quantity_delta": m.quantity_delta,
            "reason": m.reason,
            "reference_id": m.reference_id,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in movements
    ])