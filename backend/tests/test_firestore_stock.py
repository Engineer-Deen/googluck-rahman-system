import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.routes.stock import stock_bp
from tests.test_firestore_provider import FakeFirestoreClient, _seed_catalog_product
from app.firestore.service import FirestoreSyncService


class FirestoreStockTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        _seed_catalog_product(self.client, 10, [1])
        self.service = FirestoreSyncService(self.client)
        self.client.collections["staff"]["1"] = {
            "id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1,
            "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "password_hash": generate_password_hash("secret"),
        }
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(stock_bp)
        with self.app.app_context():
            self.token = issue_token(self.client.collections["staff"]["1"])

    def test_central_stock_adjustments_use_firestore_and_sum_totals(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch(
            "app.auth._central_staff", side_effect=self.service.get_staff
        ):
            client = self.app.test_client()
            restock = client.post("/api/stock-movements", headers=headers, json={"product_id": 10, "quantity_delta": 8, "reason": "restock"})
            correction = client.post("/api/stock-movements", headers=headers, json={"product_id": 10, "quantity_delta": -2, "reason": "correction"})
            history = client.get("/api/stock-movements/product/10", headers=headers)
        self.assertEqual(restock.status_code, 201)
        self.assertEqual(correction.status_code, 201)
        self.assertEqual(restock.get_json()["new_stock"], 8)
        self.assertEqual(correction.get_json()["new_stock"], 6)
        self.assertEqual(len(history.get_json()), 2)
        self.assertEqual(self.service.stock_map([10], 1)[10], 6)

    def test_central_stock_movement_is_idempotent_and_rejects_unknown_product(self):
        payload = {"id": "movement-1", "product_id": 10, "shop_id": 1, "quantity_delta": 5, "reason": "restock"}
        first, created = self.service.create_stock_movement(payload)
        second, duplicate = self.service.create_stock_movement(payload)
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "Unknown product_id"):
            self.service.create_stock_movement({**payload, "id": "missing", "product_id": 999})


if __name__ == "__main__":
    unittest.main()
