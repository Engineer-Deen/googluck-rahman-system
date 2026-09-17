import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.firestore.service import FirestoreSyncService
from app.routes.sales import sales_bp
from tests.test_firestore_provider import FakeFirestoreClient, _seed_catalog_product


class FirestoreSaleCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        _seed_catalog_product(self.client, 10, [1])
        self.client.collections["staff"]["1"] = {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}
        self.client.collections["sales"]["sale-1"] = {"id": "sale-1", "shop_id": 1, "staff_id": 1, "customer_name": "Alice", "total_amount": "10.00", "created_at": datetime.now(timezone.utc)}
        self.client.collections["sale_items"]["item-1"] = {"id": "item-1", "sale_id": "sale-1", "product_id": 10, "quantity": 1, "unit_price": "10.00", "subtotal": "10.00", "unit_cost": "5.00"}
        self.client.collections["stock_movements"]["restock"] = {"id": "restock", "product_id": 10, "shop_id": 1, "quantity_delta": 5, "reason": "restock"}
        self.client.collections["stock_movements"]["sale"] = {"id": "sale", "product_id": 10, "shop_id": 1, "quantity_delta": -1, "reason": "sale", "reference_id": "sale-1"}
        self.service = FirestoreSyncService(self.client)
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(sales_bp)
        with self.app.app_context():
            self.token = issue_token(self.client.collections["staff"]["1"])

    def _patches(self):
        return patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff)

    def test_central_correction_replaces_items_and_adjusts_stock(self):
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().put("/api/sales/sale-1", headers={"Authorization": f"Bearer {self.token}"}, json={"reason": "Wrong quantity entered", "items": [{"product_id": 10, "quantity": 2, "unit_price": "10.00"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_amount"], "20.00")
        self.assertEqual(self.service.stock_map([10], 1)[10], 3)
        self.assertEqual(len(self.client.collections["sale_items"]), 1)

    def test_central_correction_uses_catalog_price_when_item_price_is_omitted(self):
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().put("/api/sales/sale-1", headers={"Authorization": f"Bearer {self.token}"}, json={"reason": "Wrong quantity entered", "items": [{"product_id": 10, "quantity": 2}]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_amount"], "20.00")
        self.assertEqual(len(self.client.collections["sale_items"]), 1)
        self.assertEqual(next(iter(self.client.collections["sale_items"].values()))["unit_price"], "10.00")

    def test_central_void_reverses_stock_writes_audit_and_is_idempotent(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with self._patches()[0], self._patches()[1]:
            first = self.app.test_client().post("/api/sales/sale-1/void", headers=headers, json={"reason": "Customer returned item"})
            second = self.app.test_client().post("/api/sales/sale-1/void", headers=headers, json={"reason": "Customer returned item"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.service.stock_map([10], 1)[10], 5)
        reversals = [value for value in self.client.collections["stock_movements"].values() if value.get("reason") == "void_reversal"]
        self.assertEqual(len(reversals), 1)
        self.assertTrue(self.client.collections["sales"]["sale-1"].get("voided_at"))
        self.assertTrue(self.client.collections["audit_log"])


if __name__ == "__main__":
    unittest.main()
