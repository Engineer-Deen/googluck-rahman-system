import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.firestore.service import FirestoreSyncService
from app.routes.sales import sales_bp
from tests.test_firestore_provider import FakeFirestoreClient, _seed_catalog_product


class FirestoreSalesTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        _seed_catalog_product(self.client, 10, [1])
        self.client.collections["shops"]["1"] = {"id": 1, "name": "Shop One"}
        self.client.collections["devices"]["device-a"] = {"id": "device-a", "shop_id": 1, "authorized": True}
        self.staff = {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}
        self.client.collections["staff"]["1"] = self.staff
        self.client.collections["stock_movements"]["seed"] = {"id": "seed", "product_id": 10, "shop_id": 1, "quantity_delta": 5, "reason": "restock"}
        self.service = FirestoreSyncService(self.client)
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(sales_bp)
        with self.app.app_context():
            self.token = issue_token(self.staff)

    def _patches(self):
        return patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff)

    def test_central_sale_and_items_are_stored_atomically_in_firestore(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"id": "sale-1", "device_id": "device-a", "customer_name": "alice", "items": [{"id": "item-1", "product_id": 10, "quantity": 2, "unit_price": "12.50"}]}
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().post("/api/sales", headers=headers, json=payload)
            detail = self.app.test_client().get("/api/sales/sale-1", headers=headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total_amount"], "25.00")
        self.assertEqual(detail.get_json()["items"][0]["quantity"], 2)
        self.assertIn("sale-1", self.client.collections["sales"])
        self.assertIn("item-1", self.client.collections["sale_items"])
        self.assertEqual(self.service.stock_map([10], 1)[10], 3)

    def test_duplicate_sale_is_idempotent_and_stock_is_not_deducted_twice(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"id": "sale-duplicate", "device_id": "device-a", "customer_name": "Alice", "items": [{"product_id": 10, "quantity": 1, "unit_price": "10"}]}
        with self._patches()[0], self._patches()[1]:
            first = self.app.test_client().post("/api/sales", headers=headers, json=payload)
            second = self.app.test_client().post("/api/sales", headers=headers, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.client.collections["sales"]), 1)
        self.assertEqual(self.service.stock_map([10], 1)[10], 4)

    def test_central_sale_rejects_insufficient_stock_before_writing(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().post("/api/sales", headers=headers, json={"id": "sale-over", "device_id": "device-a", "customer_name": "Alice", "items": [{"product_id": 10, "quantity": 99, "unit_price": "10"}]})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("sale-over", self.client.collections["sales"])

    def test_central_sales_listing_parses_limit_and_preserves_shop_scope(self):
        cashier = {"id": 2, "name": "Cashier", "email": "cashier@test", "role": "cashier", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}
        self.client.collections["staff"]["2"] = cashier
        self.client.collections["sales"].update({
            "sale-one": {"id": "sale-one", "shop_id": 1, "customer_name": "One", "total_amount": "10.00", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
            "sale-two": {"id": "sale-two", "shop_id": 1, "customer_name": "Two", "total_amount": "20.00", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            "sale-other": {"id": "sale-other", "shop_id": 2, "customer_name": "Other", "total_amount": "30.00", "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc)},
        })
        with self.app.app_context():
            token = issue_token(cashier)
        headers = {"Authorization": f"Bearer {token}"}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            default_response = self.app.test_client().get("/api/sales", headers=headers)
            limited_response = self.app.test_client().get("/api/sales?limit=1", headers=headers)
            invalid_response = self.app.test_client().get("/api/sales?limit=invalid", headers=headers)

        self.assertEqual(default_response.status_code, 200)
        self.assertEqual([sale["id"] for sale in default_response.get_json()], ["sale-one", "sale-two"])
        self.assertEqual(limited_response.status_code, 200)
        self.assertEqual([sale["id"] for sale in limited_response.get_json()], ["sale-one"])
        self.assertEqual(invalid_response.status_code, 200)
        self.assertEqual([sale["id"] for sale in invalid_response.get_json()], ["sale-one", "sale-two"])
        self.assertTrue(all(sale["shop_id"] == 1 for sale in default_response.get_json()))


if __name__ == "__main__":
    unittest.main()
