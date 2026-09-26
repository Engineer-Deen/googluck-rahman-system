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
from app.central_proxy import forward_to_central
from app.extensions import db
from app.models import Product, StockMovement, Staff

products_bp = Blueprint("products", __name__, url_prefix="/api/products")

FINANCE_ROLES = ("owner", "admin", "manager")

PRODUCT_EDIT_REASONS = {
    "Correct product information",
    "Correct product name",
    "Correct category",
    "Correct cost price",
    "Correct data entry mistake",
    "Other approved reason",
}
PRODUCT_DEACTIVATE_REASONS = {
    "Product discontinued",
    "Product unavailable",
    "Product temporarily unavailable",
    "Product replaced",
    "Product entered in error",
    "Other approved reason",
}
PRODUCT_REACTIVATE_REASONS = {
    "Product available again",
    "Product returned to catalog",
    "Previous deactivation was incorrect",
    "Product replacement cancelled",
    "Other approved reason",
}
PRODUCT_DELETE_REASONS = {
    "Duplicate product",
    "Product created by mistake",
    "Product permanently removed from catalog",
    "Product replaced or merged",
    "Other approved reason",
}


def current_stock(product_id, shop_id=None):
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        return get_firestore_sync_service().stock_map([product_id], shop_id).get(int(product_id), 0)
    query = db.session.query(func.coalesce(func.sum(StockMovement.quantity_delta), 0))
    query = query.filter(StockMovement.product_id == product_id)
    if shop_id is not None:
        query = query.filter(StockMovement.shop_id == shop_id)
    total = query.scalar()
    return int(total)


def stock_map(product_ids=None, shop_id=None):
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        return get_firestore_sync_service().stock_map(product_ids, shop_id)
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
    value = lambda key, default=None: p.get(key, default) if isinstance(p, dict) else getattr(p, key, default)
    data = {
        "id": value("id"),
        "sku": value("sku"),
        "name": value("name"),
        "category": value("category"),
        "unit_price": str(value("unit_price", 0)),
        "is_active": value("is_active", True),
    }
    if include_stock:
        data["stock"] = current_stock(p.id) if stock_value is None else int(stock_value)
    # The sales desk needs the current cost price as read-only context when
    # setting a transaction selling price. This does not expose or calculate
    # profit for the caller; profit remains governed by the sales responses.
    data["cost_price"] = str(value("cost_price", 0))
    return data


PRODUCT_OFFLINE_MESSAGE = (
    "Products can only be added or edited while this shop computer is online. "
    "Connect to the internet and try again; the change will then appear here "
    "automatically."
)


def _mirror_product_locally(body):
    """
    Put the product central just saved into this PC's own database right away,
    so it shows up in Inventory and can be stocked and sold without waiting for
    the next sync. Central's reply only carries cost_price for finance roles;
    the sync pull fills in anything missing and remains the source of truth.
    """
    try:
        product_id = int(body["id"])
        product = db.session.get(Product, product_id)
        if product is None:
            product = Product(id=product_id, sku=body["sku"], name=body["name"])
            db.session.add(product)
        product.sku = body.get("sku", product.sku)
        product.name = body.get("name", product.name)
        product.category = body.get("category", product.category)
        if body.get("unit_price") is not None:
            product.unit_price = body["unit_price"]
        if body.get("cost_price") is not None:
            product.cost_price = body["cost_price"]
        product.is_active = bool(body.get("is_active", True))
        db.session.commit()
    except Exception:  # local cache only -- central already succeeded
        db.session.rollback()
    try:
        from app.sync.worker import trigger_sync_soon
        trigger_sync_soon(current_app._get_current_object())
    except Exception:
        pass


@products_bp.get("")
@login_required
def list_products():
    # Deactivated ("deleted") products are hidden from the normal list --
    # they shouldn't show up for a new sale -- unless explicitly asked
    # for with ?include_inactive=true (useful for an admin screen that
    # wants to see/reactivate them).
    include_inactive = request.args.get("include_inactive") == "true"
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        products = service.list_products(include_inactive=include_inactive)
        shop_id = None if g.staff_role == "owner" else g.staff_shop_id
        stocks = service.stock_map((product.get("id") for product in products), shop_id=shop_id)
        return jsonify([serialize_product(product, role=g.staff_role, stock_value=stocks.get(int(product["id"]), 0)) for product in products])
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
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        product = get_firestore_sync_service().get_product(product_id)
        if not product:
            return jsonify(error="Product not found"), 404
        shop_id = None if g.staff_role == "owner" else g.staff_shop_id
        return jsonify(serialize_product(product, role=g.staff_role, stock_value=current_stock(product_id, shop_id)))
    p = Product.query.get_or_404(product_id)
    shop_id = None if g.staff_role == "owner" else g.staff_shop_id
    return jsonify(serialize_product(p, role=g.staff_role, stock_value=current_stock(product_id, shop_id)))


@products_bp.post("")
@roles_required("owner", "admin", "manager")
def create_product():
    if current_app.config["GLR_MODE"] != "central":
        body, response = forward_to_central("POST", "/api/products", PRODUCT_OFFLINE_MESSAGE)
        if body is not None:
            _mirror_product_locally(body)
        return response

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "Other Electronics").strip()

    if not name:
        return jsonify(error="Product name is required"), 400
    if category not in CATEGORY_CODES:
        return jsonify(error="Please select a valid product category"), 400

    try:
        cost_price = float(data.get("cost_price", 0))
    except (TypeError, ValueError):
        return jsonify(error="cost_price must be a number"), 400
    # Selling price is intentionally NOT a catalog field anymore. It is
    # entered for each sale at the point of checkout because it can change
    # from customer to customer and transaction to transaction. Keep the
    # legacy database column at zero for compatibility with existing rows.
    unit_price = 0.0

    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        # Idempotency: if this exact submission (double-click, or a retry
        # after the response was lost to a dropped connection) already
        # created a product, return that product instead of creating a
        # second one. See _mirror_product_locally/app.js for the other half
        # of this -- the client sends the same client_request_id on retry.
        client_request_id = (data.get("client_request_id") or "").strip()
        if client_request_id:
            existing = service.get_product_by_client_request_id(client_request_id)
            if existing:
                shop_id = None if g.staff_role == "owner" else g.staff_shop_id
                return jsonify(serialize_product(
                    existing, role=g.staff_role,
                    stock_value=current_stock(existing["id"], shop_id),
                )), 200
        product_id = service._allocate_product_id()
        product = service.save_product(product_id, sku=f"GLR-{CATEGORY_CODES[category]}-{product_id:06d}", name=name, category=category, unit_price=str(unit_price), cost_price=str(cost_price), is_active=True, shop_ids=[g.staff_shop_id] if g.staff_shop_id else [], client_request_id=client_request_id or None)
        service.write_audit(f"product-created-{product_id}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="product_created", entity_type="product", entity_id=str(product_id), details={"sku": product["sku"], "name": name, "category": category})
        return jsonify(serialize_product(product, role=g.staff_role, stock_value=0)), 201
    product = Product(
        sku=f"TEMP-{uuid.uuid4().hex}", name=name, category=category,
        unit_price=unit_price, cost_price=cost_price,
    )
    db.session.add(product)
    db.session.flush()
    product.sku = f"GLR-{CATEGORY_CODES[category]}-{product.id:06d}"
    db.session.commit()

    actor = db.session.get(Staff, g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "product_created", "product", product.id,
        {"sku": product.sku, "name": product.name, "category": product.category},
    )

    return jsonify(serialize_product(product, role=g.staff_role, stock_value=0)), 201


@products_bp.put("/<int:product_id>")
@roles_required("owner", "admin", "manager")
def update_product(product_id):
    if current_app.config["GLR_MODE"] != "central":
        body, response = forward_to_central("PUT", f"/api/products/{product_id}", PRODUCT_OFFLINE_MESSAGE)
        if body is not None:
            _mirror_product_locally(body)
        return response

    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        product = service.get_product(product_id)
        if not product:
            return jsonify(error="Product not found"), 404
        data = request.get_json(silent=True) or {}
        reason = (data.get("reason") or "").strip()
        if not reason:
            return jsonify(error="A reason is required to update a product"), 400
        if set(data.keys()) == {"is_active", "reason"}:
            reason_set = PRODUCT_REACTIVATE_REASONS if bool(data.get("is_active")) else PRODUCT_DEACTIVATE_REASONS
            if reason not in reason_set:
                return jsonify(error="Please select a valid product state-change reason"), 400
        elif reason not in PRODUCT_EDIT_REASONS:
            return jsonify(error="Please select a valid product edit reason"), 400
        before = dict(product)
        updates = {key: data[key] for key in ("name", "is_active") if key in data}
        if "category" in data:
            if data["category"] not in CATEGORY_CODES:
                return jsonify(error="Please select a valid product category"), 400
            updates["category"] = data["category"]
        if "cost_price" in data:
            try:
                updates["cost_price"] = str(float(data["cost_price"]))
            except (TypeError, ValueError):
                return jsonify(error="cost_price must be a number"), 400
        product = service.save_product(product_id, **updates)
        service.write_audit(f"product-updated-{product_id}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="product_updated", entity_type="product", entity_id=str(product_id), details={"reason": reason, "before": before, "after": product})
        return jsonify(serialize_product(product, role=g.staff_role, stock_value=current_stock(product_id, g.staff_shop_id)))
    p = Product.query.get_or_404(product_id)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify(error="A reason is required to update a product"), 400
    if set(data.keys()) == {"is_active", "reason"}:
        reason_set = PRODUCT_REACTIVATE_REASONS if bool(data.get("is_active")) else PRODUCT_DEACTIVATE_REASONS
        if reason not in reason_set:
            return jsonify(error="Please select a valid product state-change reason"), 400
    elif reason not in PRODUCT_EDIT_REASONS:
        return jsonify(error="Please select a valid product edit reason"), 400
    before = {"sku": p.sku, "name": p.name, "category": p.category, "unit_price": str(p.unit_price), "cost_price": str(p.cost_price), "is_active": p.is_active}

    if "name" in data:
        p.name = data["name"]
    if "category" in data:
        if data["category"] not in CATEGORY_CODES:
            return jsonify(error="Please select a valid product category"), 400
        p.category = data["category"]
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

    actor = db.session.get(Staff, g.staff_id)
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
    if current_app.config["GLR_MODE"] != "central":
        body, response = forward_to_central("DELETE", f"/api/products/{product_id}", PRODUCT_OFFLINE_MESSAGE)
        if body is not None:
            _mirror_product_locally(body)
        return response

    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        product = service.get_product(product_id)
        if not product:
            return jsonify(error="Product not found"), 404
        data = request.get_json(silent=True) or {}
        reason = (data.get("reason") or "").strip()
        if reason not in PRODUCT_DELETE_REASONS:
            return jsonify(error="Please select a valid product deletion reason"), 400
        product = service.save_product(product_id, is_active=False)
        service.write_audit(f"product-deleted-{product_id}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="product_deleted", entity_type="product", entity_id=str(product_id), details={"sku": product.get("sku"), "name": product.get("name"), "reason": reason})
        return jsonify(serialize_product(product, role=g.staff_role, stock_value=current_stock(product_id, g.staff_shop_id)))
    p = Product.query.get_or_404(product_id)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if reason not in PRODUCT_DELETE_REASONS:
        return jsonify(error="Please select a valid product deletion reason"), 400
    p.is_active = False
    db.session.commit()

    actor = db.session.get(Staff, g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "product_deleted", "product", p.id,
        {"sku": p.sku, "name": p.name, "reason": reason or None},
    )

    return jsonify(serialize_product(p, role=g.staff_role))