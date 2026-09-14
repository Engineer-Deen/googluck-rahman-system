
from datetime import datetime, timezone
from decimal import Decimal
import re

from flask import Blueprint, current_app, g, jsonify, request

from app.auth import login_required
from app.audit import log_action
from app.extensions import db
from app.models import InvoiceSequence, Product, Sale, SaleItem, SalePayment, StockMovement, Staff
from sqlalchemy.orm import selectinload
from app.models.transactions import gen_uuid
from app.routes.products import current_stock

sales_bp = Blueprint("sales", __name__, url_prefix="/api/sales")

FINANCE_ROLES = ("owner", "admin", "manager")
ADMIN_ROLES = ("owner", "admin")
VOID_REASONS = {
    "Customer returned item",
    "Wrong product or quantity entered",
    "Wrong price entered",
    "Duplicate sale",
    "Payment issue",
    "Customer cancelled order",
    "Other approved reason",
}


def assign_invoice_number(sale):
    """Assign a central, human-readable invoice number once only."""
    if sale.invoice_number:
        return sale.invoice_number
    if current_app.config.get("CENTRAL_DATA_PROVIDER") == "firestore":
        from app.firestore import get_firestore_sync_service

        service = get_firestore_sync_service()
        year = (sale.created_at or datetime.now(timezone.utc)).year
        invoice_floor = 1001
        invoice_prefix = f"INV-{year}-"
        for (invoice_number,) in Sale.query.with_entities(Sale.invoice_number).filter(
            Sale.invoice_number.like(f"{invoice_prefix}%")
        ).all():
            match = re.fullmatch(rf"INV-{year}-(\d+)", str(invoice_number))
            if match:
                invoice_floor = max(invoice_floor, int(match.group(1)) + 1)
        payload = {
            "created_at": sale.created_at.isoformat() if sale.created_at else None,
            "minimum_next": invoice_floor,
        }
        for _ in range(5):
            invoice_number = service.allocate_invoice_number(payload)
            conflict = Sale.query.filter(
                Sale.invoice_number == invoice_number,
                Sale.id != sale.id,
            ).first()
            if not conflict:
                sale.invoice_number = invoice_number
                return sale.invoice_number
            match = re.fullmatch(r"INV-(\d+)-(\d+)", str(invoice_number))
            if match:
                payload["minimum_next"] = int(match.group(2)) + 1
        raise RuntimeError("Could not allocate a unique invoice number")
    year = (sale.created_at or datetime.now(timezone.utc)).year
    seq = InvoiceSequence.query.filter_by(year=year).with_for_update().first()
    if not seq:
        seq = InvoiceSequence(year=year, next_number=1001)
        db.session.add(seq)
        db.session.flush()
    number = seq.next_number
    seq.next_number = number + 1
    sale.invoice_number = f"INV-{year}-{number}"
    return sale.invoice_number


def amount_paid(sale: Sale) -> Decimal:
    return sum((p.amount for p in sale.payments), Decimal("0.00")).quantize(Decimal("0.01"))


def serialize_sale(sale: Sale, role: str = None, product_map=None):
    paid = amount_paid(sale)
    total = sale.total_amount
    balance = total - paid
    if balance < 0:
        balance = Decimal("0")

    data = {
        "id": sale.id,
        "invoice_number": sale.invoice_number,
        "shop_id": sale.shop_id,
        "device_id": sale.device_id,
        "staff_id": sale.staff_id,
        "customer_name": sale.customer_name,
        "payment_method": sale.payment_method,
        "total_amount": str(total),
        "amount_paid": str(paid),
        "balance": str(balance),
        "status": "voided" if sale.voided_at else ("completed" if balance <= 0 else "incomplete"),
        "voided_at": sale.voided_at.isoformat() if sale.voided_at else None,
        "voided_by_staff_id": sale.voided_by_staff_id,
        "void_reason": sale.void_reason,
        "created_at": sale.created_at.isoformat() if sale.created_at else None,
        "server_received_at": sale.server_received_at.isoformat()
        if sale.server_received_at
        else None,
        "items": [
            {
                "product_id": item.product_id,
                "product_name": ((product_map or {}).get(item.product_id).name if (product_map or {}).get(item.product_id) else f"Product #{item.product_id}"),
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "subtotal": str(item.subtotal),
            }
            for item in sale.items
        ],
    }

    if role in FINANCE_ROLES:
        # Voided sales never contribute to profit or revenue reporting --
        # the transaction is cancelled, not just discounted.
        if sale.voided_at:
            data["profit"] = "0.00"
            data["realized_profit"] = "0.00"
            for item_data in data["items"]:
                item_data["unit_cost"] = "0.00"
            return data

        item_profits = [
            (item.unit_price - item.unit_cost) * item.quantity for item in sale.items
        ]
        profit = sum(item_profits, Decimal("0"))
        data["profit"] = str(profit)
        # Realized profit is PROPORTIONAL to how much has actually been
        # paid -- half paid means half the profit counts as realized so
        # far, not "zero until the very last cent arrives." This matches
        # the old system's accounting exactly. Because we always derive
        # this fresh from the current total/paid rather than storing and
        # incrementing it, we get the same result their incremental
        # per-payment formula produces, without needing to replicate the
        # incremental bookkeeping.
        if total > 0:
            realized = (profit * min(paid, total) / total).quantize(Decimal("0.01"))
        else:
            realized = Decimal("0")
        data["realized_profit"] = str(realized)
        for item_data, item in zip(data["items"], sale.items):
            item_data["unit_cost"] = str(item.unit_cost)
            item_data["profit"] = str((item.unit_price - item.unit_cost) * item.quantity)

    return data


def apply_sale(payload: dict):
    """
    payload shape:
    {
      "id": "...",                 # required, client-generated
      "shop_id": 1,
      "device_id": "...",
      "staff_id": 3,
      "customer_name": "...",
      "payment_method": "cash",
      "created_at": "2026-...",    # optional, ISO string; defaults to now
      "amount_paid": 500,          # optional; defaults to 0 (nothing paid
                                    # yet) -- a caller must say explicitly
                                    # that a sale was paid in full
      "payment_id": "...",         # optional client-generated id for the
                                    # initial SalePayment row this creates
      "items": [
        {"id": "...", "product_id": 1, "quantity": 2, "unit_price": 450,
         "stock_movement_id": "..."},
        ...
      ]
    }

    Returns (sale, stock_warnings, created) where created is False if
    the sale already existed (idempotent no-op).
    """
    sale_id = payload.get("id")
    if not sale_id:
        raise ValueError("payload.id is required")

    existing = Sale.query.get(sale_id)
    if existing:
        return existing, [], False

    # Matches the old system's rule: every sale needs a customer name.
    # With balance/part-payment tracking, an anonymous sale with an open
    # balance would be unrecoverable -- there'd be no way to know who
    # still owes money.
    customer_name = re.sub(r"\s+", " ", (payload.get("customer_name") or "").strip())
    customer_name = " ".join(word[:1].upper() + word[1:].lower() for word in customer_name.split(" ") if word)
    if not customer_name:
        raise ValueError("Customer name is required")

    items_data = payload.get("items") or []
    if not items_data:
        raise ValueError("A sale needs at least one item")

    product_ids = [i.get("product_id") for i in items_data]
    products = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}
    missing = [pid for pid in product_ids if pid not in products]
    if missing:
        raise ValueError(f"Unknown product_id(s): {missing}")

    if payload.get("validate_stock", False):
        requested = {}
        for item in items_data:
            requested[item["product_id"]] = requested.get(item["product_id"], 0) + int(item["quantity"])
        for pid, qty in requested.items():
            available = current_stock(pid, payload.get("shop_id"))
            if qty > available:
                raise ValueError(f"Not enough stock for {products[pid].name}: only {available} available, {qty} requested")

    # Compute the total BEFORE touching the session at all, so an
    # overpay (or any other validation failure below) never leaves a
    # half-built sale sitting uncommitted.
    total_amount = Decimal("0")
    for item in items_data:
        product = products[item["product_id"]]
        try:
            quantity = int(item["quantity"])
        except (TypeError, ValueError):
            raise ValueError("Item quantity must be a whole number")
        if quantity <= 0:
            raise ValueError("Item quantity must be greater than zero")
        unit_price = Decimal(str(item.get("unit_price", product.unit_price)))
        if unit_price <= 0:
            raise ValueError("Item selling price must be greater than zero")
        total_amount += (unit_price * quantity).quantize(Decimal("0.01"))
    total_amount = total_amount.quantize(Decimal("0.01"))

    # Omitted amount_paid means "nothing paid yet", not "assume paid in
    # full" -- a caller (the frontend, or a future integration) has to
    # say explicitly that a sale was paid in full, the same as it has
    # to say explicitly how much of a deposit was taken. Silently
    # assuming full payment when nothing was specified is exactly the
    # kind of guess that could misstate what a customer actually owes.
    initial_paid = payload.get("amount_paid", Decimal("0"))
    initial_paid = Decimal(str(initial_paid))
    if initial_paid > total_amount:
        raise ValueError("Amount paid cannot exceed the sale total")

    sale = Sale(
        id=sale_id,
        shop_id=payload.get("shop_id"),
        device_id=payload.get("device_id"),
        staff_id=payload.get("staff_id"),
        customer_name=customer_name,
        payment_method=payload.get("payment_method", "cash"),
        total_amount=total_amount,
    )
    if payload.get("created_at"):
        sale.created_at = datetime.fromisoformat(payload["created_at"])
    db.session.add(sale)

    for item in items_data:
        product = products[item["product_id"]]
        quantity = int(item["quantity"])
        unit_price = Decimal(str(item.get("unit_price", product.unit_price)))
        subtotal = (unit_price * quantity).quantize(Decimal("0.01"))

        db.session.add(
            SaleItem(
                id=item.get("id") or gen_uuid(),
                sale_id=sale.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                unit_cost=product.cost_price,
            )
        )
        db.session.add(
            StockMovement(
                id=item.get("stock_movement_id") or gen_uuid(),
                product_id=product.id,
                shop_id=sale.shop_id,
                device_id=sale.device_id,
                quantity_delta=-quantity,
                reason="sale",
                reference_id=sale.id,
            )
        )

    # Central assigns the human-facing invoice number. Local/offline sales remain
    # pending until the first successful central acknowledgement.
    if payload.get("assign_invoice", False):
        assign_invoice_number(sale)
    sale.server_received_at = datetime.now(timezone.utc)

    # The initial payment -- already validated against the total above,
    # before any of this was added to the session.
    if initial_paid > 0:
        db.session.add(
            SalePayment(
                id=payload.get("payment_id") or gen_uuid(),
                sale_id=sale.id,
                amount=initial_paid,
                device_id=sale.device_id,
                staff_id=sale.staff_id,
                server_received_at=datetime.now(timezone.utc),
            )
        )

    if current_app.config.get("GLR_MODE") == "local":
        from app.sync.outbox import enqueue_outbox
        enqueue_outbox("sales", sale.id, payload)
    db.session.commit()

    stock_warnings = []
    for item in items_data:
        remaining = current_stock(item["product_id"], sale.shop_id)
        if remaining < 0:
            stock_warnings.append(
                {"product_id": item["product_id"], "stock_after_sale": remaining}
            )

    return sale, stock_warnings, True


def apply_payment(payload: dict):
    """
    A LATER payment against an existing sale's balance -- see the
    SalePayment model docstring for why this is deliberately NOT queued
    through the offline outbox the way sales/stock movements are.

    Returns (sale, payment, created).
    """
    payment_id = payload.get("id")
    if not payment_id:
        raise ValueError("payload.id is required")

    existing = SalePayment.query.get(payment_id)
    if existing:
        return existing.sale, existing, False

    sale = Sale.query.get(payload.get("sale_id"))
    if not sale:
        raise ValueError("Unknown sale_id")

    amount = Decimal(str(payload.get("amount", 0)))
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")

    current_balance = sale.total_amount - amount_paid(sale)
    if amount > current_balance:
        raise ValueError(
            f"Payment of {amount} exceeds the outstanding balance of {current_balance}"
        )

    payment = SalePayment(
        id=payment_id,
        sale_id=sale.id,
        amount=amount,
        device_id=payload.get("device_id"),
        staff_id=payload.get("staff_id"),
        server_received_at=datetime.now(timezone.utc),
    )
    db.session.add(payment)
    if current_app.config.get("GLR_MODE") == "local":
        from app.sync.outbox import enqueue_outbox
        enqueue_outbox("sale_payments", payment.id, payload)
    db.session.commit()

    return sale, payment, True


def apply_void(payload: dict):
    """
    Voids a sale by reversing its stock effect and flagging it -- never
    by deleting anything, so the audit trail stays complete.

    Idempotency here works differently than sales/payments: there's no
    growing list to check an id against, just one voided_at field. So
    "already voided" (voided_at already set) IS the idempotent no-op
    condition, rather than checking for an existing row by id. The
    reversal stock movements still use ids the caller generated up
    front (see the route below), so if this function somehow runs
    twice before voided_at commits, those inserts stay safe too.

    No permission check happens in here -- that's deliberate, same
    pattern as apply_payment. The route below is where role/ownership/
    same-day rules are enforced, using the caller's authenticated
    identity (g.staff_id / g.staff_role), which only exists in a
    request context. When local mode mirrors an already-approved void
    onto its own database (see the route), it calls this function
    directly, replaying a decision central already made -- not
    re-deciding it.

    Returns (sale, created) where created is False if this sale was
    already voided.
    """
    sale = Sale.query.get(payload.get("sale_id"))
    if not sale:
        raise ValueError("Unknown sale_id")

    if sale.voided_at:
        return sale, False

    reversal_ids = payload.get("reversal_movement_ids") or {}

    for item in sale.items:
        db.session.add(
            StockMovement(
                id=reversal_ids.get(item.id) or gen_uuid(),
                product_id=item.product_id,
                shop_id=sale.shop_id,
                device_id=payload.get("device_id"),
                quantity_delta=item.quantity,  # adds back what the sale deducted
                reason="void_reversal",
                reference_id=sale.id,
            )
        )

    sale.voided_at = datetime.now(timezone.utc)
    sale.voided_by_staff_id = payload.get("voided_by_staff_id")
    sale.void_reason = (payload.get("reason") or "").strip() or None

    db.session.commit()
    return sale, True


@sales_bp.get("")
@login_required
def list_sales():
    from datetime import timedelta
    q = Sale.query
    if g.staff_role != "owner":
        q = q.filter(Sale.shop_id == g.staff_shop_id)
    period = (request.args.get("period") or "all").lower()
    search = (request.args.get("search") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 100) or 100), 1), 200)
    except (TypeError, ValueError):
        limit = 100
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(Sale.created_at >= start)
    elif period == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(Sale.created_at >= end - timedelta(days=1), Sale.created_at < end)
    elif period in ("7days", "7_days"):
        q = q.filter(Sale.created_at >= now - timedelta(days=7))
    elif period in ("30days", "month", "30_days"):
        q = q.filter(Sale.created_at >= now - timedelta(days=30))
    elif period == "year":
        q = q.filter(Sale.created_at >= now - timedelta(days=365))
    if search:
        term = f"%{search}%"
        from sqlalchemy import or_
        q = q.filter(or_(Sale.customer_name.ilike(term), Sale.invoice_number.ilike(term)))
    status_filter = (request.args.get("status") or "").lower()
    if status_filter == "incomplete":
        q = q.filter(Sale.voided_at.is_(None))
        # Keep only balances still open without forcing a Python scan over
        # the entire sales table. This correlated aggregate is backed by
        # the sale_payments.sale_id index.
        from sqlalchemy import select, func
        paid_subquery = (select(func.coalesce(func.sum(SalePayment.amount), 0))
                         .where(SalePayment.sale_id == Sale.id)
                         .scalar_subquery())
        q = q.filter(Sale.total_amount > paid_subquery)
    sales = (q.options(selectinload(Sale.items), selectinload(Sale.payments))
               .order_by(Sale.created_at.desc()).limit(limit).all())
    product_ids = {item.product_id for sale in sales for item in sale.items}
    product_map = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    return jsonify([serialize_sale(s, role=g.staff_role, product_map=product_map) for s in sales])


@sales_bp.put("/<sale_id>")
@login_required
def update_sale(sale_id):
    """Controlled, audited correction of a sale on the authoritative DB."""
    from flask import current_app
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify(error="A reason is required to update a sale"), 400
    sale = Sale.query.get_or_404(sale_id)
    if g.staff_role != "owner" and sale.shop_id != g.staff_shop_id:
        return jsonify(error="Sale not found"), 404
    if sale.voided_at:
        return jsonify(error="A voided sale cannot be edited"), 400
    now = datetime.now(timezone.utc)
    if g.staff_role not in FINANCE_ROLES:
        if sale.staff_id != g.staff_id:
            return jsonify(error="You can only edit sales you created yourself"), 403
        if sale.created_at.date() != now.date():
            return jsonify(error="Sellers can only edit sales recorded today"), 403
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(error="Sale corrections require a live connection to the central server"), 409

    before = serialize_sale(sale, role="owner")
    if "customer_name" in data:
        customer_name = (data.get("customer_name") or "").strip()
        if not customer_name:
            return jsonify(error="Customer name is required"), 400
        sale.customer_name = customer_name
    if "payment_method" in data:
        sale.payment_method = data["payment_method"]

    # Optional complete item correction. This keeps the common case simple
    # while allowing Admin to correct quantity/price/product when necessary.
    if "items" in data:
        incoming = data.get("items") or []
        if not incoming:
            return jsonify(error="A sale must contain at least one item"), 400
        old_items = list(sale.items)
        old_by_product = {i.product_id: i.quantity for i in old_items}
        product_ids = [int(i["product_id"]) for i in incoming]
        products = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).with_for_update().all()}
        if len(products) != len(set(product_ids)):
            return jsonify(error="One or more products do not exist"), 400

        new_rows = []
        new_total = Decimal("0")
        new_by_product = {}
        for item in incoming:
            pid = int(item["product_id"])
            qty = int(item["quantity"])
            if qty <= 0:
                return jsonify(error="Item quantity must be greater than zero"), 400
            price = Decimal(str(item.get("unit_price", products[pid].unit_price)))
            if price <= 0:
                return jsonify(error="Item selling price must be greater than zero"), 400
            new_by_product[pid] = new_by_product.get(pid, 0) + qty
            subtotal = (price * qty).quantize(Decimal("0.01"))
            new_total += subtotal
            new_rows.append((item, products[pid], qty, price, subtotal))

        paid = amount_paid(sale)
        if paid > new_total:
            return jsonify(error=f"Existing payments ({paid}) exceed the corrected sale total ({new_total})"), 400

        # Calculate the stock effect of changing the sale. Positive delta adds
        # stock back; negative delta consumes additional stock.
        for pid in set(old_by_product) | set(new_by_product):
            old_qty = old_by_product.get(pid, 0)
            new_qty = new_by_product.get(pid, 0)
            delta = old_qty - new_qty
            if delta < 0 and current_stock(pid, sale.shop_id) < -delta:
                product = products.get(pid) or Product.query.get(pid)
                return jsonify(error=f"Not enough stock for {product.name if product else pid}: {current_stock(pid, sale.shop_id)} available"), 400
            if delta:
                db.session.add(StockMovement(product_id=pid, shop_id=sale.shop_id, device_id=None, quantity_delta=delta, reason="sale_correction", reference_id=sale.id))

        for old in old_items:
            db.session.delete(old)
        for item, product, qty, price, subtotal in new_rows:
            db.session.add(SaleItem(id=gen_uuid(), sale_id=sale.id, product_id=product.id, quantity=qty, unit_price=price, subtotal=subtotal, unit_cost=product.cost_price))
        sale.total_amount = new_total

    actor = Staff.query.get(g.staff_id)
    db.session.commit()
    after = serialize_sale(sale, role="owner")
    log_action(g.staff_id, actor.name if actor else None, g.staff_role, "sale_updated", "sale", sale.id, {"reason": reason, "before": before, "after": after})
    return jsonify(serialize_sale(sale, role=g.staff_role))

@sales_bp.get("/<sale_id>")
@login_required
def get_sale(sale_id):
    sale = (Sale.query.options(selectinload(Sale.items), selectinload(Sale.payments)).get_or_404(sale_id))
    if g.staff_role != "owner" and sale.shop_id != g.staff_shop_id:
        return jsonify(error="Sale not found"), 404
    product_ids = {i.product_id for i in sale.items}
    product_map = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    return jsonify(serialize_sale(sale, role=g.staff_role, product_map=product_map))


@sales_bp.post("")
@login_required
def create_sale():
    from flask import current_app

    from app.sync.outbox import enqueue_outbox

    data = request.get_json(silent=True) or {}

    device_id = data.get("device_id")
    if not device_id and current_app.config["GLR_MODE"] == "local":
        # Auto-generating a device_id only makes sense on a local
        # install -- it's this device's own persistent identity. In
        # central mode there's no "device" to invent one for, and
        # inventing one anyway would insert a device_id that was never
        # registered in the devices table (registration only happens
        # via the sync push endpoint), which Postgres correctly rejects
        # as a foreign key violation. SQLite never enforces that FK by
        # default, so this stayed invisible in local-only testing until
        # tested directly against a real Postgres central server.
        from app.sync.device import get_current_device_id
        device_id = get_current_device_id()

    payload = {
        "id": data.get("id") or gen_uuid(),
        "shop_id": data.get("shop_id", g.staff_shop_id) if g.staff_role == "owner" else g.staff_shop_id,
        "device_id": device_id,
        "staff_id": g.staff_id,
        "customer_name": data.get("customer_name"),
        "payment_method": data.get("payment_method", "cash"),
        "amount_paid": data.get("amount_paid"),
        "payment_id": data.get("payment_id"),
        "items": [
            {
                "id": item.get("id"),
                "product_id": item["product_id"],
                "quantity": item["quantity"],
                "unit_price": item.get("unit_price"),
                "stock_movement_id": item.get("stock_movement_id"),
            }
            for item in (data.get("items") or [])
        ],
    }
    payload["assign_invoice"] = current_app.config["GLR_MODE"] == "central"
    payload["validate_stock"] = True

    # Leaving amount_paid out entirely now means "nothing paid yet" (a
    # part payment of 0), NOT "assume paid in full" -- apply_sale()'s
    # own default is 0. A genuinely fully-paid sale must say so
    # explicitly (the frontend's "Paid in Full" checkbox fills in the
    # total amount for this reason).
    if payload["amount_paid"] is None:
        del payload["amount_paid"]

    # Lock product rows centrally while checking stock so two online
    # cashiers cannot both sell the same final unit at the same time.
    if current_app.config["GLR_MODE"] == "central":
        ids = list({int(i["product_id"]) for i in payload["items"]})
        Product.query.filter(Product.id.in_(ids)).with_for_update().all()

    # Stock is checked HERE, at the point of sale, using whichever
    # database is actually handling this request (this device's own
    # local stock if local, central's if central) -- deliberately NOT
    # inside apply_sale() itself. apply_sale() is also what replays an
    # already-accepted local sale onto central during sync; if IT
    # refused on insufficient stock, a sale a cashier legitimately made
    # could get stuck failing to sync forever the moment central's
    # stock changed for any other reason. Blocking only here means: a
    # sale is refused at most once, up front, using the best
    # information available at that exact moment -- and once accepted,
    # it's guaranteed to sync successfully no matter what.
    #
    # The one thing this still can't catch: two devices, both offline,
    # both showing the same last unit in stock, each independently
    # selling it before either has seen the other's sale. No offline-
    # capable system can fully prevent that without giving up offline
    # capability -- the stock_warning already returned by apply_sale()
    # below is what surfaces that rare case after the fact, for an
    # admin to reconcile.
    stock_shop_id = None if g.staff_role == "owner" else payload["shop_id"]
    for item in payload["items"]:
        available = current_stock(item["product_id"], stock_shop_id)
        if item["quantity"] > available:
            product = Product.query.get(item["product_id"])
            name = product.name if product else f"product #{item['product_id']}"
            return jsonify(
                error=f"Not enough stock for {name}: only {available} available, {item['quantity']} requested"
            ), 400

    try:
        sale, stock_warnings, created = apply_sale(payload)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except RuntimeError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 503

    if created and current_app.config["GLR_MODE"] == "local":
        from app.sync.worker import trigger_sync_soon
        trigger_sync_soon(current_app._get_current_object())

    product_ids = {i.product_id for i in sale.items}
    product_map = (
        {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}
        if product_ids
        else {}
    )
    response = serialize_sale(sale, role=g.staff_role, product_map=product_map)
    if stock_warnings:
        response["stock_warning"] = stock_warnings

    return jsonify(response), 201 if created else 200


@sales_bp.post("/<sale_id>/payments")
@login_required
def add_payment(sale_id):
    """
    Pays down an existing balance. Deliberately requires being able to
    reach the database that holds the true current balance:
      - in central mode, that's this server's own database -- apply
        directly.
      - in local mode, that's the CENTRAL server's database, not this
        device's local copy (sales aren't pulled down to devices, only
        pushed up -- see models/core.py for the same reasoning applied
        to reference data). So local mode proxies this request to
        central synchronously and returns whatever central says,
        rather than queuing it blindly through the offline outbox.
        If central can't be reached right now, the honest answer is
        that this payment can't be safely recorded yet -- not a silent
        local acceptance that might conflict with a payment taken
        elsewhere.
    """
    from flask import current_app

    data = request.get_json(silent=True) or {}
    sale = Sale.query.get(sale_id)
    if sale and g.staff_role != "owner" and sale.shop_id != g.staff_shop_id:
        return jsonify(error="Sale not found"), 404
    try:
        amount = Decimal(str(data.get("amount", 0)))
    except Exception:
        return jsonify(error="amount must be a number"), 400

    payload = {
        "id": data.get("id") or gen_uuid(),
        "sale_id": sale_id,
        "amount": str(amount),
        "device_id": data.get("device_id"),
        "staff_id": g.staff_id,
    }

    if current_app.config["GLR_MODE"] == "central":
        try:
            sale, payment, created = apply_payment(payload)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(serialize_sale(sale, role=g.staff_role)), 201 if created else 200

    # Local mode is offline-first: accept the payment against this device's
    # latest synchronized balance, persist it locally, and queue it.
    try:
        sale, payment, created = apply_payment(payload)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if created:
        from app.sync.worker import trigger_sync_soon
        trigger_sync_soon(current_app._get_current_object())
    return jsonify(serialize_sale(sale, role=g.staff_role)), 201 if created else 200

@sales_bp.post("/<sale_id>/void")
@login_required
def void_sale(sale_id):
    """
    Voids a sale -- reverses its stock, flags it, never deletes it.

    Permission rule: owner/admin/manager can void any sale, anytime.
    A cashier can only void a sale THEY created, and only on the same
    calendar day it was made -- older corrections need a manager/admin.

    Same connectivity requirement as payments, and for the same reason:
    this device needs to see the sale's true current state (who made
    it, when, whether it's already voided) before making an
    irreversible-in-spirit decision about it.
    """
    from flask import current_app

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if reason not in VOID_REASONS:
        return jsonify(error="Please select a valid reason for voiding this sale"), 400

    if current_app.config["GLR_MODE"] == "central":
        sale = Sale.query.get(sale_id)
        if not sale:
            return jsonify(error="Sale not found"), 404
        if g.staff_role != "owner" and sale.shop_id != g.staff_shop_id:
            return jsonify(error="Sale not found"), 404
        if sale.voided_at:
            return jsonify(serialize_sale(sale, role=g.staff_role)), 200

        if g.staff_role not in FINANCE_ROLES:
            if sale.staff_id != g.staff_id:
                return jsonify(error="You can only void sales you created yourself"), 403
            if sale.created_at.date() != datetime.now(timezone.utc).date():
                return jsonify(
                    error=(
                        "Sales can only be voided the same day they were made. "
                        "Ask a manager or the shop owner to void an older sale."
                    )
                ), 403

        payload = {
            "sale_id": sale_id,
            "reason": reason,
            "voided_by_staff_id": g.staff_id,
            "device_id": data.get("device_id"),
            "reversal_movement_ids": {item.id: gen_uuid() for item in sale.items},
        }
        sale, created = apply_void(payload)

        if created:
            actor = Staff.query.get(g.staff_id)
            log_action(
                g.staff_id, actor.name if actor else None, g.staff_role,
                "sale_voided", "sale", sale.id,
                {"reason": reason, "total_amount": str(sale.total_amount), "customer_name": sale.customer_name, "invoice_number": sale.invoice_number},
            )

        return jsonify(serialize_sale(sale, role=g.staff_role)), 200 if created else 200

    # local mode: proxy live to central for the authoritative decision,
    # same reasoning as payments.
    import requests

    local_sale = Sale.query.get(sale_id)
    if local_sale and g.staff_role != "owner" and local_sale.shop_id != g.staff_shop_id:
        return jsonify(error="Sale not found"), 404
    reversal_ids = {item.id: gen_uuid() for item in local_sale.items} if local_sale else {}

    payload = {
        "sale_id": sale_id,
        "reason": reason,
        "device_id": data.get("device_id"),
        "reversal_movement_ids": reversal_ids,
    }

    url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/") + f"/api/sales/{sale_id}/void"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": request.headers.get("Authorization", ""),
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        body = resp.json()

        if resp.status_code == 404:
            return jsonify(
                error=(
                    "This sale is still syncing to the central server (usually "
                    "takes a few seconds). Wait a moment and try voiding it again."
                )
            ), 409

        if resp.ok and local_sale:
            # Mirror the same reversal onto this device's own copy, using
            # the SAME reversal_movement_ids central just used -- keeps
            # this device's own view consistent. If this device never had
            # the sale locally (voiding a sale made elsewhere), there's
            # nothing to mirror -- that's fine, this device was never
            # showing a stale view of it to begin with.
            try:
                apply_void(payload)
            except ValueError:
                pass

        return jsonify(body), resp.status_code
    except requests.RequestException:
        return jsonify(
            error=(
                "Can't void this sale right now -- this device needs to be online "
                "to check the sale's true current status first. Try again once "
                "connected."
            )
        ), 503