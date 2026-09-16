import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.firestore.service import FirestoreSyncService
from app.routes.sales import sales_bp
from tests.test_firestore_provider import FakeFirestoreClient, _seed_catalog_product


class FirestorePaymentTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        _seed_catalog_product(self.client, 10, [1])
        self.client.collections["sales"]["sale-1"] = {
            "id": "sale-1", "shop_id": 1, "device_id": "device-a", "staff_id": 1,
            "customer_name": "Alice", "total_amount": "100.00", "payment_method": "cash",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.client.collections["sale_items"]["item-1"] = {
            "id": "item-1", "sale_id": "sale-1", "product_id": 10,
            "quantity": 1, "unit_price": "100.00", "subtotal": "100.00", "unit_cost": "60.00",
        }
        self.staff = {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}
        self.client.collections["staff"]["1"] = self.staff
        self.service = FirestoreSyncService(self.client)
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(sales_bp)
        with self.app.app_context():
            self.token = issue_token(self.staff)

    def _patches(self):
        return patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff)

    def test_central_payment_uses_firestore_and_updates_balance(self):
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().post("/api/sales/sale-1/payments", headers={"Authorization": f"Bearer {self.token}"}, json={"id": "payment-1", "amount": "35.00"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["amount_paid"], "35.00")
        self.assertEqual(response.get_json()["balance"], "65.00")
        self.assertEqual(self.client.collections["sale_payments"]["payment-1"]["sale_id"], "sale-1")

    def test_duplicate_payment_is_idempotent(self):
        request = {"id": "payment-1", "amount": "35.00"}
        with self._patches()[0], self._patches()[1]:
            first = self.app.test_client().post("/api/sales/sale-1/payments", headers={"Authorization": f"Bearer {self.token}"}, json=request)
            second = self.app.test_client().post("/api/sales/sale-1/payments", headers={"Authorization": f"Bearer {self.token}"}, json=request)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.client.collections["sale_payments"]), 1)

    def test_invalid_payment_is_rejected_without_write(self):
        with self._patches()[0], self._patches()[1]:
            too_large = self.app.test_client().post("/api/sales/sale-1/payments", headers={"Authorization": f"Bearer {self.token}"}, json={"id": "payment-bad", "amount": "101.00"})
            invalid = self.app.test_client().post("/api/sales/sale-1/payments", headers={"Authorization": f"Bearer {self.token}"}, json={"id": "payment-negative", "amount": "-1"})
        self.assertEqual(too_large.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn("payment-bad", self.client.collections["sale_payments"])
        self.assertNotIn("payment-negative", self.client.collections["sale_payments"])


if __name__ == "__main__":
    unittest.main()
