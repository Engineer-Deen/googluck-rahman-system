"""
Run this once after setting up a fresh database (local or central) to
create a login you can actually test with.

Usage:
    python seed.py
"""
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import Shop, Staff, Product

app = create_app()

with app.app_context():
    shop = Shop.query.filter_by(name="Main Shop").first()
    if not shop:
        shop = Shop(name="Main Shop", location="Freetown")
        db.session.add(shop)
        db.session.commit()
        print(f"Created shop: {shop.name} (id={shop.id})")

    admin = Staff.query.filter_by(email="admin@glr.test").first()
    if not admin:
        admin = Staff(
            shop_id=shop.id,
            name="Admin",
            email="admin@glr.test",
            password_hash=generate_password_hash("admin123"),
            role="admin",
        )
        db.session.add(admin)
        db.session.commit()
        print("Created admin login -> email: admin@glr.test  password: admin123")
    else:
        print("Admin already exists -> email: admin@glr.test  password: admin123")

    cashier = Staff.query.filter_by(email="cashier@glr.test").first()
    if not cashier:
        cashier = Staff(
            shop_id=shop.id,
            name="Cashier",
            email="cashier@glr.test",
            password_hash=generate_password_hash("cashier123"),
            role="cashier",
        )
        db.session.add(cashier)
        db.session.commit()
        print("Created cashier login -> email: cashier@glr.test  password: cashier123")
    else:
        print("Cashier already exists -> email: cashier@glr.test  password: cashier123")

    product = Product.query.filter_by(sku="RICE-50KG").first()
    if not product:
        product = Product(sku="RICE-50KG", name="Bag of Rice (50kg)", category="Grains", unit_price=450, cost_price=350)
        db.session.add(product)
        db.session.commit()
        print(f"Created test product: {product.name} (id={product.id})")
    else:
        print(f"Test product already exists: {product.name} (id={product.id})")

print("Seed complete.")