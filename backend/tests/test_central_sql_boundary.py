import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.extensions import db
from app.routes.audit import audit_bp
from app.routes.auth import auth_bp
from app.routes.sales import sales_bp
from app.routes.shop import shop_bp
from app.routes.stock import stock_bp
from app.routes.sync import sync_bp


class CentralService:
    def __init__(self):
        self.staff = {
            "id": 1,
            "shop_id": 1,
            "name": "Owner",
            "email": "owner@test",
            "role": "owner",
            "is_active": True,
            "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "password_hash": generate_password_hash("secret"),
            "quick_pin_hash": None,
        }
        self.shop = {"id": 1, "name": "Shop One", "location": "Main"}
        self.device = {"id": "device-a", "shop_id": 1, "authorized": True}
        self.product = {
            "id": 10,
            "sku": "P-10",
            "name": "Product",
            "category": "Other Electronics",
            "unit_price": "10.00",
            "cost_price": "5.00",
            "is_active": True,
        }
        self.graph = {
            "sale": {
                "id": "sale-1",
                "shop_id": 1,
                "device_id": "device-a",
                "staff_id": 1,
                "customer_name": "Customer",
                "payment_method": "cash",
                "total_amount": "10.00",
                "created_at": "2026-01-01T00:00:00+00:00",
                "invoice_number": "INV-2026-1001",
            },
            "items": [{
                "id": "item-1",
                "product_id": 10,
                "quantity": 1,
                "unit_price": "10.00",
                "subtotal": "10.00",
                "unit_cost": "5.00",
            }],
            "payments": [],
        }

    def get_staff_by_email(self, email):
        return self.staff if email == self.staff["email"] else None

    def get_staff(self, staff_id):
        return self.staff if int(staff_id) == self.staff["id"] else None

    def update_staff_auth_state(self, staff_id, **fields):
        self.staff.update(fields)

    def list_sale_graphs(self, shop_id=None, limit=100, period="all", search="", status=None):
        if shop_id is not None and self.graph["sale"]["shop_id"] != shop_id:
            return []
        return [self.graph][:limit]

    def list_audit(self, **kwargs):
        return [{
            "id": "audit-1",
            "actor_staff_id": 1,
            "actor_name": "Owner",
            "actor_role": "owner",
            "action": "staff_created",
            "entity_type": "staff",
            "entity_id": "2",
            "details": {"name": "Cashier"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }]

    def get_setting(self, key, default=None):
        return {"admin_timeout_minutes": "15", "admin_full_login_hours": "8"}.get(key, default)

    def get_shop(self, shop_id):
        return self.shop if int(shop_id) == self.shop["id"] else None

    def get_first_shop(self):
        return self.shop

    def get_product(self, product_id):
        return self.product if int(product_id) == self.product["id"] else None

    def stock_map(self, product_ids=None, shop_id=None):
        return {10: 4} if shop_id in (None, 1) else {}

    def list_stock_movements(self, product_id, shop_id=None, limit=100):
        return [{
            "id": "movement-1",
            "quantity_delta": 5,
            "reason": "restock",
            "reference_id": None,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }]

    def get_device(self, device_id):
        return self.device if device_id == self.device["id"] else None

    def save_device(self, device_id, **fields):
        self.device.update({"id": device_id, **fields})
        return self.device

    def pull(self, shop_id, since):
        return {"next_cursor": None, "shops": [], "staff": [], "settings": [], "products": [], "sales": [], "sale_items": [], "payments": [], "stock_movements": []}


class CentralSqlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.service = CentralService()
        self.app = Flask(__name__)
        self.app.config.update(
            GLR_MODE="central",
            CENTRAL_DATA_PROVIDER="firestore",
            JWT_SECRET_KEY="test-secret",
            SYNC_API_KEY="sync-secret",
            FIRESTORE_SYNC_SERVICE=self.service,
        )
        for blueprint in (auth_bp, sales_bp, audit_bp, shop_bp, stock_bp, sync_bp):
            self.app.register_blueprint(blueprint)
        with self.app.app_context():
            self.token = issue_token(self.service.staff)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _central_patches(self):
        return (
            patch("app.firestore.get_firestore_sync_service", return_value=self.service),
            patch("app.auth._central_staff", side_effect=self.service.get_staff),
        )

    def test_central_routes_use_firestore_without_sqlalchemy(self):
        client = self.app.test_client()
        with self._central_patches()[0], self._central_patches()[1]:
            login = client.post("/api/auth/login", json={"email": "owner@test", "password": "secret"})
            authenticated_headers = {"Authorization": f"Bearer {login.get_json()['token']}"}
            sales = client.get("/api/sales?limit=1", headers=authenticated_headers)
            audit = client.get("/api/audit-log", headers=authenticated_headers)
            settings = client.get("/api/shop/settings", headers=authenticated_headers)
            stock = client.get("/api/stock-movements/product/10", headers=authenticated_headers)
            device = client.post("/api/sync/devices", headers=authenticated_headers, json={"device_id": "device-b", "shop_id": 1})
            pulled = client.get("/api/sync/pull", headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-b"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(sales.status_code, 200)
        self.assertEqual(sales.get_json()[0]["id"], "sale-1")
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(stock.status_code, 200)
        self.assertEqual(device.status_code, 201)
        self.assertEqual(pulled.status_code, 200)
        self.assertIsNone(self.app.config.get("SQLALCHEMY_DATABASE_URI"))
        with self.assertRaises(RuntimeError):
            db.session.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
