"""
Core reference data: shops, staff accounts, registered devices, products.

Unlike sales/stock-movements, these are only ever created or edited
CENTRALLY (see the roles_required + GLR_MODE guard in routes/products.py).
Local devices only ever receive them via pull-sync (see app/sync/worker.py)
and treat them as read-only. That's a deliberate choice: these tables use
plain auto-incrementing integer ids, and if two offline devices could both
invent new products/staff with their own locally-assigned ids, those ids
could collide once synced to the same central database -- the exact class
of bug this rebuild exists to eliminate. Sales and stock movements avoid
this by using device-generated UUIDs instead; reference data avoids it by
only ever being writable in one place.

updated_at on each table drives incremental pull-sync: a device only
needs to ask for "everything changed since my last pull", not the whole
table every time.
"""
from datetime import datetime, timezone

from app.extensions import db
from sqlalchemy import Index


def utcnow():
    return datetime.now(timezone.utc)


class Shop(db.Model):
    __tablename__ = "shops"
    __table_args__ = (Index("ix_shops_updated_at", "updated_at"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    logo_data = db.Column(db.Text, nullable=True)


class Staff(db.Model):
    __tablename__ = "staff"
    __table_args__ = (Index("ix_staff_updated_at", "updated_at"),)

    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id"), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    quick_pin_hash = db.Column(db.String(255), nullable=True)
    quick_pin_failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    quick_pin_locked_until = db.Column(db.DateTime, nullable=True)
    role = db.Column(db.String(30), nullable=False, default="cashier")
    # roles: owner, admin, manager, cashier
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    __table_args__ = (Index("ix_system_settings_key", "key"),)

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class Device(db.Model):
    """
    A registered PC/desktop install. Every local Flask+SQLite instance
    generates its own device_id (UUID) on first run and registers it
    here the first time it connects to the central server. This id is
    what makes sales/stock-movements from different offline devices
    distinguishable and safely mergeable, unlike the old system where
    everyone shared a single account with no device identity.
    """
    __tablename__ = "devices"

    id = db.Column(db.String(36), primary_key=True)  # UUID, generated on device
    shop_id = db.Column(db.Integer, db.ForeignKey("shops.id"), nullable=True)
    name = db.Column(db.String(120))
    platform = db.Column(db.String(50))
    registered_at = db.Column(db.DateTime, default=utcnow)
    last_seen_at = db.Column(db.DateTime, default=utcnow)


class Product(db.Model):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_category", "category"), Index("ix_products_active_name", "is_active", "name"), Index("ix_products_updated_at", "updated_at"))

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100))
    unit_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    # What the business paid for this item -- used to compute profit at
    # sale time. Deliberately separate from unit_price (the selling
    # price). Visible only to admin/manager/owner roles in the API, same
    # as the old system kept margin data away from cashiers.
    cost_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    # "Deleting" a product is really deactivating it (is_active=False),
    # never a real row delete -- old sales reference this product's id,
    # and a true delete would either break those historical records or
    # force a cascade that erases them. A deactivated product just stops
    # appearing as sellable; its full sales history stays intact.
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)