import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import jwt
from flask import Flask
from werkzeug.security import generate_password_hash

from app.routes.auth import auth_bp


class FirestoreAuthService:
    def __init__(self):
        self.staff = {
            "id": 7,
            "shop_id": 3,
            "name": "Owner",
            "email": "owner@example.test",
            "role": "owner",
            "is_active": True,
            "password_hash": generate_password_hash("owner-password"),
            "quick_pin_hash": generate_password_hash("1234"),
            "quick_pin_failed_attempts": 0,
            "quick_pin_locked_until": None,
            "updated_at": datetime.now(timezone.utc),
        }
        self.updates = []

    def get_staff_by_email(self, email):
        return dict(self.staff) if email == self.staff["email"] else None

    def get_staff(self, staff_id):
        return dict(self.staff) if int(staff_id) == self.staff["id"] else None

    def update_staff_auth_state(self, staff_id, **fields):
        self.assert_staff_id = staff_id
        self.staff.update(fields)
        self.updates.append(fields)


class FirestoreAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.service = FirestoreAuthService()
        self.app = Flask(__name__)
        self.app.config.update(
            GLR_MODE="central",
            CENTRAL_DATA_PROVIDER="firestore",
            JWT_SECRET_KEY="test-jwt-secret",
        )
        self.app.register_blueprint(auth_bp)

    def _service_patches(self):
        return (
            patch("app.routes.auth._get_central_service", return_value=self.service),
            patch("app.auth._central_staff", side_effect=self.service.get_staff),
        )

    def test_central_login_and_jwt_validation_use_firestore_staff(self):
        with self._service_patches()[0], self._service_patches()[1]:
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "owner-password", "role_group": "owner"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["staff"]["id"], 7)
            token = payload["token"]

            identity = self.app.test_client().get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
            )

        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.get_json()["email"], "owner@example.test")
        self.assertTrue(any("updated_at" in update for update in self.service.updates))

    def test_central_pin_verification_updates_firestore_state(self):
        with self._service_patches()[0], self._service_patches()[1]:
            login = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "owner-password"},
            )
            token = login.get_json()["token"]
            response = self.app.test_client().post(
                "/api/auth/verify-pin",
                json={"pin": "1234"},
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("quick_pin_failed_attempts" in update for update in self.service.updates))

    def test_central_login_rejects_inactive_staff_without_sqlalchemy(self):
        self.service.staff["is_active"] = False
        with patch("app.routes.auth._get_central_service", return_value=self.service):
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "owner-password"},
            )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()