from flask import Blueprint, current_app, g, jsonify, request
import uuid
from sqlalchemy import func

CATEGORY_CODES = {
    "Mobile Phones": "MOB",
    "Mobile Accessories": "ACC",
    "Computers & Laptops": "CMP",
    "Computer Accessories": "CPA",
    "Speakers & Audio": "AUD",
    "Televisions": "TV",
    "Home Appliances": "APP",
    "Networking Equipment": "NET",
    "Cameras & Security": "CAM",
    "Gaming": "GAM",
    "Wearables": "WRB",
    "Storage & Memory": "STR",
    "Cables & Chargers": "CBL",
    "Other Electronics": "OTH",
}

from app.audit import log_action
from app.auth import login_required, roles_required
from app.extensions import db
from app.models import Product, StockMovement, Staff

products_bp = Blueprint("products", __name__, url_prefix="/api/products")

FINANCE_ROLES = ("owner", "admin", "manager")


def current_stock(product_id, shop_id=None):
    query = db.session.query(func.coalesce(func.sum(StockMovement.quantity_delta), 0))
    query = query.filter(StockMovement.product_id == product_id)
    if shop_id is not None:
        query = query.filter(StockMovement.shop_id == shop_id)
    total = query.scalar()
    return int(total)


def stock_map(product_ids=None, shop_id=None):
    query = db.session.query(
        StockMovement.product_id,
        func.coalesce(func.sum(StockMovement.quantity_delta), 0),
    )
    if product_ids:
        query = query.filter(StockMovement.product_id.in_(list(product_ids)))
    if shop_id is not None:
        query = query.filter(StockMovement.shop_id == shop_id)
    return {int(pid): int(total or 0) for pid, total in query.group_by(StockMovement.product_id).all()}


def serialize_product(p, include_stock=True, role=None, stock_value=None):
    data = {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        "category": p.category,
        "unit_price": str(p.unit_price),
        "is_active": p.is_active,
    }
    if include_stock:
        data["stock"] = current_stock(p.id) if stock_value is None else int(stock_value)
    # Cost price (and therefore margin) is only visible to roles that
    # should see it -- a cashier gets the selling price and stock, same
    # as the old system kept cost data away from front-line staff.
    if role in FINANCE_ROLES:
        data["cost_price"] = str(p.cost_price)
    return data


def _require_central_mode():
    """
    Products are reference data, pulled down (read-only) to local
    devices via app/sync/worker.py rather than created there -- see the
    explanation in models/core.py for why. Returns an error response if
    this call is running in local mode, else None.
    """
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(
            error=(
                "Products can only be created or edited on the central server "
                "(while online), then they sync down to this device automatically. "
                "This keeps product ids consistent everywhere."
            )
        ), 403
    return None


@products_bp.get("")
@login_required
def list_products():
    # Deactivated ("deleted") products are hidden from the normal list --
    # they shouldn't show up for a new sale -- unless explicitly asked
    # for with ?include_inactive=true (useful for an admin screen that
    # wants to see/reactivate them).
    include_inactive = request.args.get("include_inactive") == "true"
    query = Product.query
    if not include_inactive:
        query = query.filter_by(is_active=True)
    products = query.order_by(Product.name).all()
    shop_id = None if g.staff_role == "owner" else g.staff_shop_id
    stocks = stock_map((p.id for p in products), shop_id=shop_id)
    return jsonify([serialize_product(p, role=g.staff_role, stock_value=stocks.get(p.id, 0)) for p in products])


@products_bp.get("/<int:product_id>")
@login_required
def get_product(product_id):
    p = Product.query.get_or_404(product_id)
    shop_id = None if g.staff_role == "owner" else g.staff_shop_id
    return jsonify(serialize_product(p, role=g.staff_role, stock_value=current_stock(product_id, shop_id)))


@products_bp.post("")
@roles_required("owner", "admin", "manager")
def create_product():
    blocked = _require_central_mode()
    if blocked:
        return blocked

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "Other Electronics").strip()

    if not name:
        return jsonify(error="Product name is required"), 400
    if category not in CATEGORY_CODES:
        return jsonify(error="Please select a valid product category"), 400

    try:
        unit_price = float(data.get("unit_price", 0))
        cost_price = float(data.get("cost_price", 0))
    except (TypeError, ValueError):
        return jsonify(error="unit_price and cost_price must be numbers"), 400

    product = Product(
        sku=f"TEMP-{uuid.uuid4().hex}",
        name=name,
        category=category,
        unit_price=unit_price,
        cost_price=cost_price,
    )
    db.session.add(product)
    db.session.flush()
    product.sku = f"GLR-{CATEGORY_CODES[category]}-{product.id:06d}"
    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "product_created", "product", product.id,
        {"sku": product.sku, "name": product.name, "category": product.category},
    )

    return jsonify(serialize_product(product, role=g.staff_role, stock_value=0)), 201


@products_bp.put("/<int:product_id>")
@roles_required("owner", "admin", "manager")
def update_product(product_id):
    blocked = _require_central_mode()
    if blocked:
        return blocked

    p = Product.query.get_or_404(product_id)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify(error="A reason is required to update a product"), 400
    before = {"sku": p.sku, "name": p.name, "category": p.category, "unit_price": str(p.unit_price), "cost_price": str(p.cost_price), "is_active": p.is_active}

    if "name" in data:
        p.name = data["name"]
    if "category" in data:
        if data["category"] not in CATEGORY_CODES:
            return jsonify(error="Please select a valid product category"), 400
        p.category = data["category"]
    if "unit_price" in data:
        try:
            p.unit_price = float(data["unit_price"])
        except (TypeError, ValueError):
            return jsonify(error="unit_price must be a number"), 400
    if "cost_price" in data:
        try:
            p.cost_price = float(data["cost_price"])
        except (TypeError, ValueError):
            return jsonify(error="cost_price must be a number"), 400
    if "is_active" in data:
        # Same field DELETE uses -- PUT with is_active:true is how a
        # deactivated product gets reactivated.
        p.is_active = bool(data["is_active"])

    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "product_updated", "product", p.id, {"reason": reason, "before": before, "after": {"sku": p.sku, "name": p.name, "category": p.category, "unit_price": str(p.unit_price), "cost_price": str(p.cost_price), "is_active": p.is_active}},
    )

    return jsonify(serialize_product(p, role=g.staff_role))


@products_bp.delete("/<int:product_id>")
@roles_required("owner", "admin", "manager")
def delete_product(product_id):
    """
    "Deleting" a product deactivates it -- see the is_active comment on
    the Product model for why a real row delete isn't safe here (old
    sales still reference this product's id). A deactivated product
    stops appearing for new sales but its full history stays intact,
    and it can be reactivated later via PUT with is_active:true.
    """
    blocked = _require_central_mode()
    if blocked:
        return blocked

    p = Product.query.get_or_404(product_id)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    p.is_active = False
    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "product_deleted", "product", p.id,
        {"sku": p.sku, "name": p.name, "reason": reason or None},
    )

    return jsonify(serialize_product(p, role=g.staff_role))