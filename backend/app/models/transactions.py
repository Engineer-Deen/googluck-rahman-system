"""
The records that actually caused the old system's data loss: sales and
stock changes. Two rules fix that problem structurally:

1. The PRIMARY KEY is a UUID generated on the device that creates the
   record, not assigned by the server. Two offline devices can never
   collide, because neither is waiting for the other (or the server)
   to hand out an id.

2. These tables are APPEND-ONLY. A sale is never edited in place and
   never merged with another sale. "Syncing" a sale means "does this
   id exist on the server yet? No -> insert it. Yes -> do nothing."
   That single rule (an idempotent upsert keyed by the record's own
   id) is what makes it safe for PC A, B and C to all sync in any
   order, in any combination of offline/online, without anyone's
   sales disappearing.

Stock levels are DERIVED from stock_movements (a running total of
deltas), never stored as a single mutable "current count" -- that's
what made stock drift in the old system when two devices each thought
they owned the authoritative count.

The SAME principle applies to payments (SalePayment, below): how much
of a sale has been paid is derived from summing its payments, never
stored as a single mutable "amount_paid" field on the sale. See the
comment on SalePayment for why that matters even more for money than
it does for stock.
"""
import uuid
from datetime import datetime, timezone

from app.extensions import db
from sqlalchemy import Index


def utcnow():
    return datetime.now(timezone.utc)


def gen_uuid():
    return str(uuid.uuid4())


class Sale(db.Model):
    __tablename__ = "sales"
    __table_args__ = (
        Index("ix_sales_created_at", "created_at"),
        Index("ix_sales_shop_created", "shop_id", "created_at"),
        Index("ix_sales_staff_created", "staff_id", "created_at"),
        Index("ix_sales_updated_at", "updated_at"),
        Index("ix_sales_customer_created", "customer_name", "created_at"),
        Index("ix_sales_invoice_number", "invoice_number"),
    )

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)  # client-generated
    invoice_number = db.Column(db.String(40), unique=True, nullable=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id"), nullable=True)
    device_id = db.Column(db.String(36), db.ForeignKey("devices.id"), nullable=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=True)
    customer_name = db.Column(db.String(150))
    payment_method = db.Column(db.String(30), default="cash")
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # created_at is the moment of sale, set by the device, never changed.
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    # server_received_at is set by the central server the first time it
    # sees this id -- useful for auditing sync delay, never for conflict
    # resolution (there is no conflict resolution needed for an append-only
    # record; the id alone decides insert-vs-skip).
    server_received_at = db.Column(db.DateTime, nullable=True)

    # A voided sale is never deleted -- history stays complete for audit.
    # Voiding reverses its stock effect (see apply_void in routes/sales.py)
    # and excludes it from revenue/profit, but the record and everything
    # that happened on it (payments included) stays on file.
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=True)
    void_reason = db.Column(db.String(255), nullable=True)

    items = db.relationship("SaleItem", backref="sale", cascade="all, delete-orphan")
    payments = db.relationship("SalePayment", backref="sale", cascade="all, delete-orphan")


class InvoiceSequence(db.Model):
    __tablename__ = "invoice_sequences"

    year = db.Column(db.Integer, primary_key=True)
    next_number = db.Column(db.Integer, nullable=False, default=1001)


class SaleItem(db.Model):
    __tablename__ = "sale_items"
    __table_args__ = (Index("ix_sale_items_sale_id", "sale_id"), Index("ix_sale_items_product_id", "product_id"))

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    sale_id = db.Column(db.String(36), db.ForeignKey("sales.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    subtotal = db.Column(db.Numeric(12, 2), nullable=False)
    # Snapshot of the product's cost price AT THE MOMENT OF SALE, not a
    # live lookup -- so profit on an old sale stays accurate even if the
    # product's cost changes later. Same reasoning as unit_price already
    # being snapshotted here rather than always reading Product.unit_price.
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)


class SalePayment(db.Model):
    """
    Money paid toward a sale. Append-only, same pattern as StockMovement:
    a sale's amount_paid/balance is always DERIVED by summing its
    payments, never stored as one mutable number on the Sale itself.

    This matters even more for money than it does for stock. A sale's
    initial payment (e.g. a deposit taken at the moment of sale) is
    created in the SAME transaction as the sale, on the same device, so
    it's just as offline-safe as the sale itself.

    Any LATER payment -- someone coming back to pay off what they still
    owe -- is different: it might happen at a different shop, on a
    different device, than the one that recorded the original sale. Two
    devices independently accepting "the final payment" on the same
    balance without knowing about each other could cause exactly the
    kind of loss this whole rebuild exists to prevent (either double-
    crediting the business, or a customer being told they still owe
    money they already paid). So later payments are NOT queued through
    the offline outbox like sales are -- see apply_payment() and the
    payments route for how this is handled instead.
    """
    __tablename__ = "sale_payments"
    __table_args__ = (Index("ix_sale_payments_sale_id", "sale_id"), Index("ix_sale_payments_created_at", "created_at"), Index("ix_sale_payments_updated_at", "updated_at"))

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)  # client-generated
    sale_id = db.Column(db.String(36), db.ForeignKey("sales.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    device_id = db.Column(db.String(36), db.ForeignKey("devices.id"), nullable=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    server_received_at = db.Column(db.DateTime, nullable=True)


class StockMovement(db.Model):
    """
    Every stock change (restock, sale deduction, correction, transfer)
    is one row here. Current stock for a product = sum of quantity_delta
    across all its movements. Never store/overwrite a single "current
    quantity" field -- that field is exactly what two offline devices
    would race to overwrite.
    """
    __tablename__ = "stock_movements"
    __table_args__ = (Index("ix_stock_movements_product_id", "product_id"), Index("ix_stock_movements_created_at", "created_at"), Index("ix_stock_movements_updated_at", "updated_at"))

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)  # client-generated
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id"), nullable=True)
    device_id = db.Column(db.String(36), db.ForeignKey("devices.id"), nullable=True)
    quantity_delta = db.Column(db.Integer, nullable=False)  # +50 restock, -3 sale, etc.
    reason = db.Column(db.String(50), nullable=False)  # 'sale', 'restock', 'correction', 'transfer'
    reference_id = db.Column(db.String(36), nullable=True)  # e.g. the sale.id that caused it

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    server_received_at = db.Column(db.DateTime, nullable=True)