import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask

from app.auth import issue_token
from app.routes.shop import shop_bp
from app.routes.sync import sync_bp


class CentralBoundaryService:
    def __init__(self):
        self.shops = {1: {"id": 1, "name": "Main Shop", "location": "Town"}}
        self.devices = {}
        self.staff = {
            1: {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime.now(timezone.utc), "quick_pin_hash": "hash"},
        }
        self.settings = {}
        self.audit = []

    def get_staff(self, staff_id):
        return self.staff.get(int(staff_id))

    def get_shop(self, shop_id):
        return self.shops.get(int(shop_id))

    def get_first_shop(self):
        return next(iter(self.shops.values()), None)

    def save_device(self, device_id, **fields):
        self.devices.setdefault(device_id, {"id": device_id}).update(fields)
        return self.devices[device_id]

    def get_device(self, device_id):
        return self.devices.get(device_id)

    def save_shop(self, shop_id, **fields):
        self.shops[int(shop_id)] = {"id": int(shop_id), **fields}
        return self.shops[int(shop_id)]

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def save_setting(self, key, value):
        self.settings[key] = value
        return {"id": key, "key": key, "value": value}

    def write_audit(self, audit_id, **fields):
        self.audit.append({"id": audit_id, **fields})


class FirestoreDeviceShopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.service = CentralBoundaryService()
        self.app = Flask(__name__)
        self.app.config.update(
            GLR_MODE="central",
            CENTRAL_DATA_PROVIDER="firestore",
            JWT_SECRET_KEY="test-secret",
            SYNC_API_KEY="sync-secret",
        )
        self.app.register_blueprint(shop_bp)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            self.token = issue_token(self.service.staff[1])

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_device_registration_uses_firestore(self):
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch(
            "app.auth._central_staff", side_effect=self.service.get_staff
        ):
            response = self.app.test_client().post(
                "/api/sync/devices",
                headers=self.headers(),
                json={"device_id": "desktop-1", "shop_id": 1, "name": "Counter", "platform": "Windows"},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.service.devices["desktop-1"]["shop_id"], 1)
        self.assertTrue(self.service.devices["desktop-1"]["authorized"])

    def test_shop_and_settings_use_firestore(self):
        client = self.app.test_client()
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch(
            "app.auth._central_staff", side_effect=self.service.get_staff
        ):
            shop = client.get("/api/shop", headers=self.headers())
            self.assertEqual(shop.status_code, 200)
            updated = client.put("/api/shop", headers=self.headers(), json={"name": "Updated Shop"})
            settings = client.put("/api/shop/settings", headers=self.headers(), json={"timeout_minutes": 30, "full_login_hours": 8})
            loaded = client.get("/api/shop/settings", headers=self.headers())
        self.assertEqual(updated.get_json()["name"], "Updated Shop")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(loaded.get_json()["timeout_minutes"], 30)
        self.assertEqual(self.service.settings["admin_timeout_minutes"], "30")
        self.assertTrue(self.service.audit)


if __name__ == "__main__":
    unittest.main()
