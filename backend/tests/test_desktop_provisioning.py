"""First-run desktop enrollment and central-only authentication tests."""
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from flask import Flask
import requests

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

    def test_enrollment_registers_centrally_without_local_auth_credential(self):
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
            return _Response({"sync_api_key": "central-issued-key"}, 201)

        with patch("app.routes.sync.requests.get", side_effect=get_response), patch(
            "app.routes.sync.requests.post", side_effect=post_response
        ) as central_posts:
            response = self.app.test_client().post(
                "/api/sync/provisioning/enroll",
                json={
                    "central_email": "owner@example.test",
                    "central_password": "central-password",
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
            self.assertEqual(staff.password_hash, "")
            self.assertNotEqual(staff.password_hash, "local-only-password")
            self.assertEqual(db.session.get(SyncState, "provisioning_state").value, "READY")
        env_path = Path(self.temp_dir.name) / ".env"
        self.assertIn("SYNC_API_KEY=central-issued-key", env_path.read_text(encoding="utf-8"))
        self.assertEqual(self.app.config["SYNC_API_KEY"], "central-issued-key")

    def test_enrollment_fails_loudly_when_central_issues_no_sync_key(self):
        """A blank/missing key from central must stop enrollment, not silently

        continue into a device that will fail every pull afterward with no
        self-service way to recover (see the SYNC_API_KEY_MISSING handling
        in _effective_provisioning_state for the other half of this fix).
        """
        identity = {
            "id": 7, "name": "Central Owner", "email": "owner@example.test",
            "role": "owner", "shop_id": 1, "is_active": True,
        }

        def post_response(url, **_kwargs):
            if url.endswith("/api/auth/login"):
                return _Response({"token": "central-token", "staff": identity})
            return _Response({}, 201)  # no sync_api_key field, e.g. stale central build

        with patch("app.routes.sync.requests.post", side_effect=post_response):
            response = self.app.test_client().post(
                "/api/sync/provisioning/enroll",
                json={"central_email": "owner@example.test", "central_password": "central-password"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("did not issue a sync key", response.get_json()["error"])
        with self.app.app_context():
            # Nothing should have been provisioned locally -- this must be
            # retried from scratch (after central's misconfiguration is
            # fixed), not left in a half-enrolled state.
            self.assertEqual(Staff.query.count(), 0)
            self.assertIsNone(db.session.get(Device, self.device_path.read_text().strip()))

    def test_already_enrolled_device_with_missing_key_can_self_recover(self):
        """Simulates an install stuck by the OLD bug: already enrolled

        (has a device row + shop_id) but SYNC_API_KEY never got persisted,
        so every pull fails with the 'not configured' error. The device
        must fall back to NOT_ENROLLED so the owner can just re-run
        enrollment (no manual .env editing) once central is fixed.
        """
        with self.app.app_context():
            self.device_path.write_text("stuck-device-id", encoding="utf-8")
            db.session.add(Device(id="stuck-device-id", shop_id=1))
            db.session.add(SyncState(key="last_pull_error", value="Cloud synchronization is not configured on this device."))
            db.session.add(SyncState(key="provisioning_state", value="SYNC_ERROR"))
            db.session.commit()

        response = self.app.test_client().get("/api/sync/provisioning/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "NOT_ENROLLED")

    def test_invalid_central_credentials_do_not_provision_local_identity(self):
        with patch(
            "app.routes.sync.requests.post", return_value=_Response({"error": "invalid"}, 401)
        ) as central_login:
            response = self.app.test_client().post(
                "/api/sync/provisioning/enroll",
                json={
                    "central_email": "owner@example.test",
                    "central_password": "wrong-password",
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

    def test_local_login_uses_central_identity_not_sqlite_password(self):
        with self.app.app_context():
            db.session.add(Staff(
                id=7,
                shop_id=1,
                name="Central Owner",
                email="owner@example.test",
                password_hash="",
                role="owner",
            ))
            db.session.commit()
        with patch("app.routes.auth.requests.post", return_value=_Response({
            "token": "central-token",
            "staff": {
                "id": 7,
                "name": "Central Owner",
                "email": "owner@example.test",
                "role": "owner",
                "shop_id": 1,
                "is_active": True,
            },
        })) as central_login:
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "central-password", "role_group": "owner"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["token"], "central-token")
        self.assertEqual(central_login.call_args.kwargs["json"]["password"], "central-password")

    def test_local_password_cannot_authenticate(self):
        with self.app.app_context():
            db.session.add(Staff(
                id=8,
                shop_id=1,
                name="Local Only",
                email="local@example.test",
                password_hash="legacy-local-hash",
                role="owner",
            ))
            db.session.commit()
        with patch("app.routes.auth.requests.post", return_value=_Response({"error": "invalid"}, 401)):
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "local@example.test", "password": "legacy-password", "role_group": "owner"},
            )
        self.assertEqual(response.status_code, 401)

    def test_central_auth_timeout_is_distinct(self):
        with patch("app.routes.auth.requests.post", side_effect=requests.Timeout):
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "central-password"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "central_auth_unavailable")

    def test_central_auth_network_failure_is_distinct(self):
        with patch("app.routes.auth.requests.post", side_effect=requests.ConnectionError):
            response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "central-password"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "central_auth_network")

    def test_authenticated_session_requires_central_validation(self):
        with self.app.app_context():
            db.session.add(Staff(
                id=7, shop_id=1, name="Central Owner", email="owner@example.test",
                password_hash="", role="owner",
            ))
            db.session.commit()
        central_staff = {
            "id": 7, "name": "Central Owner", "email": "owner@example.test",
            "role": "owner", "shop_id": 1, "is_active": True,
        }

        # A local session is established only by an actual central login --
        # there is no other way into the cache _central_session_staff reads.
        with patch("app.routes.auth.requests.post", return_value=_Response({
            "token": "central-token", "staff": central_staff,
        })):
            login_response = self.app.test_client().post(
                "/api/auth/login",
                json={"email": "owner@example.test", "password": "central-password", "role_group": "owner"},
            )
        self.assertEqual(login_response.status_code, 200)
        token = login_response.get_json()["token"]

        # Once that session exists, local requests authenticate from the
        # in-memory cache and must NOT call central again -- that's the
        # entire point of _central_session_staff (see its docstring): calling
        # central on every request would turn routine UI polling into
        # repeated central auth traffic. This is why the old version of this
        # test (patching app.auth.requests.get and expecting 503s on network
        # failure) no longer matches reality: that code path is dead for
        # local mode.
        with patch("app.auth.requests.get") as central_me:
            response = self.app.test_client().get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        central_me.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["email"], "owner@example.test")

        # A token the local cache has never seen (forged, or from before a
        # local backend restart, which clears the in-memory cache) is
        # rejected locally as an expired session -- again without ever
        # asking central, since a cache miss is exactly what tells the
        # client to log in again.
        with patch("app.auth.requests.get") as central_me:
            response = self.app.test_client().get(
                "/api/auth/me",
                headers={"Authorization": "Bearer some-other-token"},
            )
        central_me.assert_not_called()
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
