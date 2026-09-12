"""Idempotent setup for a newly installed frozen local backend."""

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import Shop, Staff


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
