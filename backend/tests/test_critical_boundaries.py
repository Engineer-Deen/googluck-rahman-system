import os
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.config import get_config, server_host
from app.extensions import db
from app.models import Product, Sale, Shop, Staff, StockMovement
from app.routes.auth import auth_bp
from app.routes.products import products_bp
from app.routes.sales import sales_bp
from app.routes.shop import shop_bp
from app.routes.stock import stock_bp
from app.routes.staff import staff_bp


class CriticalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            JWT_SECRET_KEY="test-secret",
            DEVICE_ID_FILE=Path("device-id.txt"),
        )
        db.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(products_bp)
        self.app.register_blueprint(sales_bp)
        self.app.register_blueprint(shop_bp)
        self.app.register_blueprint(stock_bp)
        self.app.register_blueprint(staff_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Shop One"),
                Shop(id=2, name="Shop Two"),
                Staff(
                    id=1,
                    shop_id=1,
                    name="Cashier One",
                    email="one@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="cashier",
                ),
                Staff(
                    id=2,
                    shop_id=2,
                    name="Cashier Two",
                    email="two@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="cashier",
                ),
                Staff(
                    id=3,
                    shop_id=1,
                    name="Shop Admin",
                    email="admin@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="admin",
                ),
                Staff(
                    id=4,
                    shop_id=1,
                    name="Owner",
                    email="owner@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="owner",
                ),
                Product(id=1, sku="P-1", name="Shared Product", unit_price=10, cost_price=5),
                StockMovement(id="movement-one", product_id=1, shop_id=1, quantity_delta=3, reason="restock"),
                StockMovement(id="movement-two", product_id=1, shop_id=2, quantity_delta=7, reason="restock"),
                Sale(id="sale-two", shop_id=2, staff_id=2, customer_name="Other Shop", total_amount=10),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        device_path = self.app.config["DEVICE_ID_FILE"]
        if device_path.exists():
            device_path.unlink()

    def _cashier_client(self):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": "one@critical.test", "password": "secret", "role_group": "seller"},
        )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def _login(self, email, role_group):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": email, "password": "secret", "role_group": role_group},
        )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def test_cashier_isolated_from_other_shop(self):
        client, headers = self._cashier_client()

        products = client.get("/api/products", headers=headers)
        self.assertEqual(products.status_code, 200)
        self.assertEqual(products.get_json()[0]["stock"], 3)

        sales = client.get("/api/sales", headers=headers)
        self.assertEqual(sales.status_code, 200)
        self.assertEqual(sales.get_json(), [])
        self.assertEqual(client.get("/api/sales/sale-two", headers=headers).status_code, 404)

        history = client.get("/api/stock-movements/product/1", headers=headers)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([row["id"] for row in history.get_json()], ["movement-one"])
        payment = client.post(
            "/api/sales/sale-two/payments",
            headers=headers,
            json={"id": "payment-two", "amount": "1.00"},
        )
        self.assertEqual(payment.status_code, 404)

    def test_cashier_sale_cannot_override_shop(self):
        client, headers = self._cashier_client()
        with patch("app.sync.worker.trigger_sync_soon"):
            response = client.post(
                "/api/sales",
                headers=headers,
                json={
                    "id": "sale-one",
                    "shop_id": 2,
                    "device_id": "device-one",
                    "customer_name": "Local Customer",
                    "items": [{"product_id": 1, "quantity": 1, "unit_price": "10.00"}],
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["shop_id"], 1)

    def test_existing_tokens_revalidate_employee_state_and_logout(self):
        client, headers = self._cashier_client()

        with self.app.app_context():
            staff = db.session.get(Staff, 1)
            staff.is_active = False
            db.session.commit()
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

        with self.app.app_context():
            staff = db.session.get(Staff, 1)
            staff.is_active = True
            db.session.commit()
        client, headers = self._cashier_client()

        with self.app.app_context():
            db.session.get(Staff, 1).role = "manager"
            db.session.commit()
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

        with self.app.app_context():
            db.session.get(Staff, 1).role = "cashier"
            db.session.commit()
        client, headers = self._cashier_client()
        with self.app.app_context():
            db.session.get(Staff, 1).shop_id = 2
            db.session.commit()
        self.assertEqual(client.get("/api/shop", headers=headers).status_code, 401)

        with self.app.app_context():
            db.session.get(Staff, 1).shop_id = 1
            db.session.commit()
        client, headers = self._cashier_client()
        self.assertEqual(client.post("/api/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

    def test_non_owner_admin_cannot_assign_staff_to_another_shop(self):
        self.app.config["GLR_MODE"] = "central"
        admin, admin_headers = self._login("admin@critical.test", "owner")
        response = admin.post(
            "/api/staff",
            headers=admin_headers,
            json={
                "name": "Other Shop",
                "email": "other@critical.test",
                "password": "secret1",
                "role": "cashier",
                "shop_id": 2,
            },
        )
        self.assertEqual(response.status_code, 403)

        owner, owner_headers = self._login("owner@critical.test", "owner")
        response = owner.post(
            "/api/staff",
            headers=owner_headers,
            json={
                "name": "Other Shop",
                "email": "other@critical.test",
                "password": "secret1",
                "role": "cashier",
                "shop_id": 2,
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["shop_id"], 2)

    def test_local_host_and_central_defaults_are_safe(self):
        self.assertEqual(server_host("local"), "127.0.0.1")
        self.assertEqual(server_host("central"), "0.0.0.0")

        names = ("DATABASE_URL", "JWT_SECRET_KEY", "SYNC_API_KEY")
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            with patch.dict(os.environ, {"GLR_MODE": "central"}, clear=False):
                with self.assertRaises(RuntimeError):
                    get_config()
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value