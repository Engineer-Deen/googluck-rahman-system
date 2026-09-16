import os
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import check_password_hash

from app.bootstrap import ensure_initial_central_data


class CentralBootstrapService:
    def __init__(self):
        self.shop = None
        self.staff = {}
        self.products = []
        self.settings = {}

    def get_first_shop(self):
        return self.shop

    def save_shop(self, shop_id, **fields):
        self.shop = {"id": shop_id, **fields}
        return self.shop

    def get_staff_by_email(self, email):
        return next((row for row in self.staff.values() if row["email"] == email), None)

    def allocate_staff_id(self):
        return len(self.staff) + 1

    def save_staff(self, staff_id, **fields):
        self.staff[staff_id] = {"id": staff_id, **fields}
        return self.staff[staff_id]

    def list_products(self, include_inactive=False):
        return list(self.products)

    def _allocate_product_id(self):
        return len(self.products) + 1

    def save_product(self, product_id, **fields):
        product = {"id": product_id, **fields}
        self.products.append(product)
        return product

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def save_setting(self, key, value):
        self.settings[key] = value
        return {"id": key, "key": key, "value": value}


class FirestoreBootstrapTests(unittest.TestCase):
    def test_central_bootstrap_uses_firestore_without_sqlalchemy(self):
        service = CentralBootstrapService()
        env = {
            "GLR_MODE": "central",
            "ALLOW_DEMO_SEED": "false",
            "INITIAL_SHOP_NAME": "Central Shop",
            "INITIAL_SHOP_LOCATION": "Town",
            "INITIAL_OWNER_EMAIL": "owner@test",
            "INITIAL_OWNER_PASSWORD": "owner-password",
            "INITIAL_OWNER_NAME": "Owner",
            "INITIAL_ADMIN_EMAIL": "admin@test",
            "INITIAL_ADMIN_PASSWORD": "admin-password",
            "INITIAL_ADMIN_NAME": "Admin",
            "INITIAL_PRODUCT_SKU": "P-1",
            "INITIAL_PRODUCT_NAME": "Product",
            "INITIAL_PRODUCT_CATEGORY": "General",
            "INITIAL_PRODUCT_UNIT_PRICE": "10",
            "INITIAL_PRODUCT_COST_PRICE": "6",
            "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "",
            "BOOTSTRAP_TARGET_EMAIL": "",
            "BOOTSTRAP_NEW_PASSWORD": "",
        }
        app = Flask(__name__)
        with app.app_context(), patch.dict(os.environ, env, clear=False), patch(
            "app.firestore.get_firestore_sync_service", return_value=service
        ), patch("app.db.init_app") as init_db, patch("app.db.create_all") as create_all, patch(
            "app.db.session"
        ) as session:
            ensure_initial_central_data()

        init_db.assert_not_called()
        create_all.assert_not_called()
        session.assert_not_called()
        self.assertEqual(service.shop["name"], "Central Shop")
        owner = service.get_staff_by_email("owner@test")
        self.assertTrue(check_password_hash(owner["password_hash"], "owner-password"))
        self.assertEqual(service.products[0]["sku"], "P-1")

    def test_central_bootstrap_is_idempotent_for_existing_records(self):
        service = CentralBootstrapService()
        service.shop = {"id": 1, "name": "Existing Shop"}
        service.staff[1] = {"id": 1, "email": "owner@test", "shop_id": 1, "role": "owner"}
        env = {
            "GLR_MODE": "central",
            "ALLOW_DEMO_SEED": "false",
            "INITIAL_SHOP_NAME": "Existing Shop",
            "INITIAL_OWNER_EMAIL": "owner@test",
            "INITIAL_OWNER_PASSWORD": "ignored-password",
            "INITIAL_ADMIN_EMAIL": "",
            "INITIAL_ADMIN_PASSWORD": "",
            "INITIAL_CASHIER_EMAIL": "",
            "INITIAL_CASHIER_PASSWORD": "",
            "INITIAL_PRODUCT_SKU": "",
            "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "",
            "BOOTSTRAP_TARGET_EMAIL": "",
            "BOOTSTRAP_NEW_PASSWORD": "",
        }
        app = Flask(__name__)
        with app.app_context(), patch.dict(os.environ, env, clear=False), patch(
            "app.firestore.get_firestore_sync_service", return_value=service
        ):
            ensure_initial_central_data()
        self.assertEqual(len(service.staff), 1)
        self.assertEqual(service.shop["name"], "Existing Shop")

    def test_partial_central_bootstrap_recovers_on_retry(self):
        service = CentralBootstrapService()
        original_save_staff = service.save_staff
        attempts = {"count": 0}

        def fail_once(staff_id, **fields):
            attempts["count"] += 1
            if attempts["count"] == 2:
                raise RuntimeError("simulated provisioning interruption")
            return original_save_staff(staff_id, **fields)

        service.save_staff = fail_once
        env = {
            "GLR_MODE": "central",
            "ALLOW_DEMO_SEED": "false",
            "INITIAL_SHOP_NAME": "Recoverable Shop",
            "INITIAL_OWNER_EMAIL": "owner@recover.test",
            "INITIAL_OWNER_PASSWORD": "owner-password",
            "INITIAL_ADMIN_EMAIL": "admin@recover.test",
            "INITIAL_ADMIN_PASSWORD": "admin-password",
            "INITIAL_CASHIER_EMAIL": "",
            "INITIAL_CASHIER_PASSWORD": "",
            "INITIAL_PRODUCT_SKU": "",
            "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "",
            "BOOTSTRAP_TARGET_EMAIL": "",
            "BOOTSTRAP_NEW_PASSWORD": "",
        }
        app = Flask(__name__)
        with app.app_context(), patch.dict(os.environ, env, clear=False), patch(
            "app.firestore.get_firestore_sync_service", return_value=service
        ):
            with self.assertRaisesRegex(RuntimeError, "provisioning interruption"):
                ensure_initial_central_data()
            service.save_staff = original_save_staff
            ensure_initial_central_data()

        self.assertEqual(len(service.staff), 2)
        self.assertIsNotNone(service.get_staff_by_email("owner@recover.test"))
        self.assertIsNotNone(service.get_staff_by_email("admin@recover.test"))
        self.assertEqual(service.settings["admin_timeout_minutes"], "15")
        self.assertEqual(service.settings["admin_full_login_hours"], "8")


if __name__ == "__main__":
    unittest.main()
