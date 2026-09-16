import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.firestore.service import FirestoreSyncService
from app.routes.audit import audit_bp
from tests.test_firestore_provider import FakeFirestoreClient


class FirestoreAuditTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        self.staff = {
            "id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1,
            "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "password_hash": generate_password_hash("secret"),
        }
        self.client.collections["staff"]["1"] = self.staff
        self.service = FirestoreSyncService(self.client)
        now = datetime(2026, 1, 3, tzinfo=timezone.utc)
        self.client.collections["audit_log"].update({
            "audit-old": {"id": "audit-old", "actor_staff_id": 1, "actor_name": "Owner", "actor_role": "owner", "action": "staff_created", "entity_type": "staff", "entity_id": "2", "details": {"name": "Cashier", "email": "cashier@test", "role": "cashier"}, "created_at": now - timedelta(days=2)},
            "audit-new": {"id": "audit-new", "actor_staff_id": 1, "actor_name": "Owner", "actor_role": "owner", "action": "product_updated", "entity_type": "product", "entity_id": "3", "details": {"after": {"name": "Phone"}}, "created_at": now},
        })
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(audit_bp)
        with self.app.app_context():
            self.token = issue_token(self.staff)

    def test_central_audit_listing_orders_and_serializes_firestore_documents(self):
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            response = self.app.test_client().get("/api/audit-log?limit=1", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()), 1)
        self.assertEqual(response.get_json()[0]["id"], "audit-new")
        self.assertEqual(response.get_json()[0]["action"], "Updated product information")
        self.assertEqual(response.get_json()[0]["details"], "Updated Phone.")

    def test_central_audit_filters_and_empty_results(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            client = self.app.test_client()
            filtered = client.get("/api/audit-log?action=staff_created&entity_type=staff", headers=headers)
            empty = client.get("/api/audit-log?action=missing", headers=headers)
            invalid = client.get("/api/audit-log?since=bad", headers=headers)
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([row["id"] for row in filtered.get_json()], ["audit-old"])
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json(), [])
        self.assertEqual(invalid.status_code, 400)

    def test_cashier_role_is_rejected_and_local_mode_keeps_proxy_contract(self):
        cashier = dict(self.staff, role="cashier")
        with self.app.app_context():
            cashier_token = issue_token(cashier)
        with patch("app.auth._central_staff", side_effect=lambda staff_id: cashier):
            denied = self.app.test_client().get("/api/audit-log", headers={"Authorization": f"Bearer {cashier_token}"})
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
