"""First-run desktop enrollment and local credential provisioning tests."""
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from flask import Flask
from werkzeug.security import check_password_hash

from app.extensions import db
from app.models import Device, Shop, Staff, SyncState
from app.routes.auth import auth_bp
from app.routes.sync import sync_bp


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class DesktopProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.device_path = Path(self.temp_dir.name) / "device_id.txt"
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            JWT_SECRET_KEY="test-secret",
            DEVICE_ID_FILE=self.device_path,
            CENTRAL_SYNC_URL="https://central.test",
            SYNC_API_KEY="sync-secret",
        )
        db.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add(Shop(id=1, name="Authorized Shop"))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
        self.temp_dir.cleanup()

    def test_enrollment_registers_centrally_and_creates_local_hash(self):
        identity = {
            "id": 7,
            "name": "Central Owner",
            "email": "owner@example.test",
            "role": "owner",
            "shop_id": 1,
            "is_active": True,
        }
        pull = {
            "shops": [{"id": 1, "name": "Authorized Shop", "location": None}],
            "staff": [{**identity}],
            "products": [],
            "sales": [],
            "sale_items": [],
            "payments": [],
            "stock_movements": [],
            "settings": [],
            "next_cursor": "2026-09-15T00:00:00+00:00",
        }
        def get_response(url, **_kwargs):
            return _Response(pull)

        def post_response(url, **_kwargs):
            if url.endswith("/api/auth/login"):
                return _Response({"token": "central-token", "staff": identity})
            return _Response({}, 201)

        with patch("app.routes.sync.requests.get", side_effect=get_response), patch(
            "app.routes.sync.requests.post", side_effect=post_response
        ) as central_posts:
            response = self.app.test_client().post(
                "/api/sync/provisioning/enroll",
                json={
                    "central_email": "owner@example.test",
                    "central_password": "central-password",
                    "local_password": "local-only-password",
                    "name": "Test Desktop",
                    "platform": "Windows",
                },
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["state"], "READY")
        self.assertEqual(central_posts.call_count, 2)
        self.assertTrue(central_posts.call_args_list[0].args[0].endswith("/api/auth/login"))
        self.assertEqual(central_posts.call_args_list[1].kwargs["json"]["shop_id"], 1)
        with self.app.app_context():
            device_id = self.device_path.read_text().strip()
            device = db.session.get(Device, device_id)
            staff = db.session.get(Staff, 7)
            self.assertEqual(device.shop_id, 1)
            self.assertEqual(staff.email, "owner@example.test")
            self.assertTrue(check_password_hash(staff.password_hash, "local-only-password"))
            self.assertNotEqual(staff.password_hash, "local-only-password")
            self.assertEqual(db.session.get(SyncState, "provisioning_state").value, "READY")

    def test_invalid_central_credentials_do_not_provision_local_identity(self):
        with patch(
            "app.routes.sync.requests.post", return_value=_Response({"error": "invalid"}, 401)
        ) as central_login:
            response = self.app.test_client().post(
                "/api/sync/provisioning/enroll",
                json={
                    "central_email": "owner@example.test",
                    "central_password": "wrong-password",
                    "local_password": "local-only-password",
                },
            )

        self.assertEqual(response.status_code, 401)
        central_login.assert_called_once()
        with self.app.app_context():
            self.assertEqual(Staff.query.count(), 0)

    def test_unenrolled_status_is_explicit(self):
        response = self.app.test_client().get("/api/sync/provisioning/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "NOT_ENROLLED")
        self.assertTrue(response.get_json()["device_id"])


if __name__ == "__main__":
    unittest.main()
