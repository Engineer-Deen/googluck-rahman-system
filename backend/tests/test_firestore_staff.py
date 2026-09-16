import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

from app.auth import issue_token
from app.routes.staff import staff_bp


class StaffService:
    def __init__(self):
        self.staff = {
            1: {"id": 1, "shop_id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")},
            2: {"id": 2, "shop_id": 1, "name": "Cashier", "email": "cashier@test", "role": "cashier", "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("cashier")},
        }
        self.audit = []

    def get_staff(self, staff_id):
        return self.staff.get(int(staff_id))

    def get_staff_by_email(self, email):
        return next((staff for staff in self.staff.values() if staff["email"] == email), None)

    def list_staff(self):
        return sorted(self.staff.values(), key=lambda row: row["name"])

    def staff_email_exists(self, email, excluding_id=None):
        staff = self.get_staff_by_email(email)
        return bool(staff and staff["id"] != excluding_id)

    def allocate_staff_id(self):
        return max(self.staff) + 1

    def get_shop(self, shop_id):
        return {"id": int(shop_id), "name": "Shop"} if int(shop_id) == 1 else None

    def save_staff(self, staff_id, **fields):
        self.staff[int(staff_id)] = {**self.staff.get(int(staff_id), {"id": int(staff_id)}), **fields, "id": int(staff_id), "updated_at": datetime.now(timezone.utc)}
        return self.staff[int(staff_id)]

    def write_audit(self, audit_id, **fields):
        self.audit.append({"id": audit_id, **fields})


class FirestoreStaffTests(unittest.TestCase):
    def setUp(self):
        self.service = StaffService()
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(staff_bp)
        with self.app.app_context():
            self.token = issue_token(self.service.staff[1])

    def _patches(self):
        return patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff)

    def test_central_staff_crud_and_account_state_use_firestore(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with self._patches()[0], self._patches()[1]:
            client = self.app.test_client()
            listed = client.get("/api/staff", headers=headers)
            created = client.post("/api/staff", headers=headers, json={"name": "New Cashier", "email": "new@test", "password": "newpass", "role": "cashier", "shop_id": 1})
            staff_id = created.get_json()["id"]
            updated = client.put(f"/api/staff/{staff_id}", headers=headers, json={"name": "Inactive Cashier", "role": "manager", "is_active": False})
            reset = client.post(f"/api/staff/{staff_id}/reset-password", headers=headers, json={"new_password": "changed"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.get_json()["is_active"], False)
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(check_password_hash(self.service.staff[staff_id]["password_hash"], "changed"))
        self.assertTrue(self.service.audit)

    def test_owner_can_create_admin_and_admin_boundaries_remain(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with self._patches()[0], self._patches()[1]:
            response = self.app.test_client().post("/api/staff/admins", headers=headers, json={"name": "Admin", "email": "admin@test", "password": "adminpass"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["role"], "admin")


if __name__ == "__main__":
    unittest.main()
