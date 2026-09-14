"""Idempotent setup for local and central first-run bootstrap."""

import os

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import Product, Shop, Staff


def _seed_value(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def ensure_initial_local_data() -> None:
    """Create the initial local accounts only when their rows are absent.

    This is intentionally limited to the packaged local first-run path. It
    never updates an existing account, so a user's password and local data are
    preserved on every subsequent startup.
    """
    shop = Shop.query.filter_by(name="Main Shop").first()
    if not shop:
        shop = Shop(name="Main Shop", location="Freetown")
        db.session.add(shop)
        db.session.flush()

    initial_accounts = (
        ("Admin", "admin@glr.test", "admin123", "admin"),
        ("Cashier", "cashier@glr.test", "cashier123", "cashier"),
    )
    for name, email, password, role in initial_accounts:
        if not Staff.query.filter_by(email=email).first():
            db.session.add(
                Staff(
                    shop_id=shop.id,
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role=role,
                )
            )

    db.session.commit()


def ensure_initial_central_data() -> None:
    """Create a safe initial central shop and accounts if the central database is empty."""
    shop_name = _seed_value("INITIAL_SHOP_NAME", "Main Shop")
    shop_location = _seed_value("INITIAL_SHOP_LOCATION", "Freetown")

    shop = Shop.query.filter_by(name=shop_name).first()
    if not shop:
        shop = Shop(name=shop_name, location=shop_location)
        db.session.add(shop)
        db.session.flush()

    account_specs = (
        (
            "owner",
            _seed_value("INITIAL_OWNER_EMAIL", "owner@glr.test"),
            _seed_value("INITIAL_OWNER_PASSWORD", "owner123"),
            "Owner",
        ),
        (
            "admin",
            _seed_value("INITIAL_ADMIN_EMAIL", "admin@glr.test"),
            _seed_value("INITIAL_ADMIN_PASSWORD", "admin123"),
            "Admin",
        ),
        (
            "cashier",
            _seed_value("INITIAL_CASHIER_EMAIL", "cashier@glr.test"),
            _seed_value("INITIAL_CASHIER_PASSWORD", "cashier123"),
            "Cashier",
        ),
    )

    for role, email, password, name in account_specs:
        staff = Staff.query.filter_by(email=email).first()
        if staff:
            if not staff.shop_id:
                staff.shop_id = shop.id
            continue
        db.session.add(
            Staff(
                shop_id=shop.id,
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
            )
        )

    product_sku = _seed_value("INITIAL_PRODUCT_SKU", "RICE-50KG")
    product_name = _seed_value("INITIAL_PRODUCT_NAME", "Bag of Rice (50kg)")
    product_category = _seed_value("INITIAL_PRODUCT_CATEGORY", "Grains")
    product_unit_price = _seed_value("INITIAL_PRODUCT_UNIT_PRICE", "450")
    product_cost_price = _seed_value("INITIAL_PRODUCT_COST_PRICE", "350")

    if not Product.query.filter_by(sku=product_sku).first():
        db.session.add(
            Product(
                sku=product_sku,
                name=product_name,
                category=product_category,
                unit_price=product_unit_price,
                cost_price=product_cost_price,
            )
        )

    db.session.commit()
