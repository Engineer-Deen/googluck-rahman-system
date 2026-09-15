import unittest
from datetime import datetime, timezone

from flask import Flask

from app.extensions import db
from app.models import Device, Product, Sale, SaleItem, SalePayment, Shop, Staff, StockMovement
from app.routes.sync import sync_bp


class SyncSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
            SYNC_API_KEY="sync-secret",
        )
        db.init_app(self.app)
        self.app.register_blueprint(sync_bp)
        baseline = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Shop A"),
                Shop(id=2, name="Shop B"),
                Device(id="device-a", shop_id=1),
                Staff(id=1, shop_id=1, name="A Cashier", email="a@test", password_hash="a-password-hash", quick_pin_hash="a-pin-hash", role="cashier"),
                Staff(id=2, shop_id=2, name="B Cashier", email="b@test", password_hash="b-password-hash", quick_pin_hash="b-pin-hash", role="cashier"),
                Product(id=1, sku="A-1", name="A Product", unit_price=10, cost_price=5, created_at=baseline, updated_at=baseline),
                Product(id=2, sku="B-1", name="B Product", unit_price=20, cost_price=10, created_at=baseline, updated_at=baseline),
                Sale(id="sale-a", shop_id=1, staff_id=1, customer_name="A Customer", total_amount=10, created_at=baseline, updated_at=baseline),
                Sale(id="sale-b", shop_id=2, staff_id=2, customer_name="B Customer", total_amount=20, created_at=baseline, updated_at=baseline),
                StockMovement(
                    id="move-a", product_id=1, shop_id=1, quantity_delta=5, reason="restock",
                    created_at=baseline, updated_at=baseline,
                ),
                StockMovement(
                    id="move-b", product_id=2, shop_id=2, quantity_delta=8, reason="restock",
                    created_at=baseline, updated_at=baseline,
                ),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _headers(self, device_id="device-a"):
        return {"X-Sync-Key": "sync-secret", "X-Device-ID": device_id}

    def test_unbound_device_is_rejected(self):
        response = self.app.test_client().get(
            "/api/sync/pull",
            headers=self._headers("unknown-device"),
        )
        self.assertEqual(response.status_code, 403)

    def test_bound_device_pushes_own_shop_and_rejects_cross_shop_payload(self):
        client = self.app.test_client()
        authorized = client.post(
            "/api/sync/push",
            headers=self._headers(),
            json={
                "device_id": "device-a",
                "items": [{
                    "outbox_id": 1,
                    "table_name": "sales",
                    "payload": {
                        "id": "pushed-a",
                        "shop_id": 1,
                        "device_id": "device-a",
                        "staff_id": 1,
                        "customer_name": "Pushed A",
                        "items": [{"product_id": 1, "quantity": 1, "unit_price": "10"}],
                    },
                }],
            },
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.get_json()["results"][0]["status"], "ok")

        cross_shop = client.post(
            "/api/sync/push",
            headers=self._headers(),
            json={
                "device_id": "device-a",
                "items": [{
                    "outbox_id": 2,
                    "table_name": "sales",
                    "payload": {
                        "id": "pushed-b",
                        "shop_id": 2,
                        "device_id": "device-a",
                        "staff_id": 2,
                        "customer_name": "Pushed B",
                        "items": [{"product_id": 2, "quantity": 1, "unit_price": "20"}],
                    },
                }],
            },
        )
        self.assertEqual(cross_shop.status_code, 200)
        self.assertEqual(cross_shop.get_json()["results"][0]["status"], "error")
        with self.app.app_context():
            self.assertIsNone(db.session.get(Sale, "pushed-b"))

    def test_pull_contains_only_bound_shop_data_and_no_credentials(self):
        response = self.app.test_client().get("/api/sync/pull", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual({row["id"] for row in payload["shops"]}, {1})
        self.assertEqual({row["id"] for row in payload["staff"]}, {1})
        self.assertEqual({row["id"] for row in payload["sales"]}, {"sale-a"})
        self.assertEqual({row["id"] for row in payload["stock_movements"]}, {"move-a"})
        self.assertNotIn("password_hash", payload["staff"][0])
        self.assertNotIn("quick_pin_hash", payload["staff"][0])
        serialized = str(payload)
        for secret in ("a-password-hash", "b-password-hash", "a-pin-hash", "b-pin-hash", "sync-secret"):
            self.assertNotIn(secret, serialized)

    def test_cursor_boundary_still_replays_authorized_shop_rows(self):
        boundary = datetime(2026, 1, 2, 3, 4, 5)
        with self.app.app_context():
            product = db.session.get(Product, 1)
            product.updated_at = boundary
            db.session.commit()

        client = self.app.test_client()
        first = client.get("/api/sync/pull", headers=self._headers())
        self.assertEqual(first.status_code, 200)
        cursor = first.get_json()["next_cursor"]
        cursor_time = datetime.fromisoformat(cursor)

        with self.app.app_context():
            db.session.add(Product(id=3, sku="A-LATE", name="A Late Product", unit_price=1, cost_price=1, updated_at=cursor_time, created_at=cursor_time))
            db.session.add(Product(id=4, sku="B-LATE", name="B Late Product", unit_price=1, cost_price=1, updated_at=cursor_time, created_at=cursor_time))
            db.session.add(StockMovement(id="move-a-late", product_id=3, shop_id=1, quantity_delta=1, reason="restock", updated_at=cursor_time, created_at=cursor_time))
            db.session.add(StockMovement(id="move-b-late", product_id=4, shop_id=2, quantity_delta=1, reason="restock", updated_at=cursor_time, created_at=cursor_time))
            db.session.commit()

        second = client.get("/api/sync/pull", query_string={"since": cursor}, headers=self._headers())
        self.assertEqual(second.status_code, 200)
        self.assertEqual({row["sku"] for row in second.get_json()["products"]}, {"A-LATE"})