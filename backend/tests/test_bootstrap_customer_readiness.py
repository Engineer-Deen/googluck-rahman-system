"""Bootstrap credential and branding seeding behaviour."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.security import check_password_hash

from app.bootstrap import ensure_initial_central_data, ensure_initial_local_data
from app.extensions import db
from app.models import Product, Shop, Staff


class BootstrapCredentialTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
        )
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_local_bootstrap_uses_env_credentials_not_demo_defaults(self):
        env = {
            "ALLOW_DEMO_SEED": "false",
            "INITIAL_SHOP_NAME": "Customer Shop Ltd",
            "INITIAL_ADMIN_EMAIL": "owner@customer.shop",
            "INITIAL_ADMIN_PASSWORD": "SecurePass!42",
            "INITIAL_ADMIN_NAME": "Shop Owner",
            "INITIAL_CASHIER_EMAIL": "seller@customer.shop",
            "INITIAL_CASHIER_PASSWORD": "SellerPass!42",
            "INITIAL_CASHIER_NAME": "Front Seller",
        }
        with self.app.app_context():
            with patch.dict(os.environ, env, clear=False):
                ensure_initial_local_data()
            shop = Shop.query.filter_by(name="Customer Shop Ltd").one()
            admin = Staff.query.filter_by(email="owner@customer.shop").one()
            seller = Staff.query.filter_by(email="seller@customer.shop").one()
            self.assertEqual(admin.name, "Shop Owner")
            self.assertEqual(admin.role, "admin")
            self.assertEqual(admin.shop_id, shop.id)
            self.assertTrue(check_password_hash(admin.password_hash, "SecurePass!42"))
            self.assertEqual(seller.role, "cashier")
            self.assertIsNone(Staff.query.filter_by(email="admin@glr.test").first())

    def test_local_bootstrap_skips_accounts_without_env_or_demo_flag(self):
        with self.app.app_context():
            with patch.dict(
                os.environ,
                {
                    "ALLOW_DEMO_SEED": "false",
                    "INITIAL_SHOP_NAME": "Empty Cred Shop",
                    "INITIAL_ADMIN_EMAIL": "",
                    "INITIAL_ADMIN_PASSWORD": "",
                    "INITIAL_CASHIER_EMAIL": "",
                    "INITIAL_CASHIER_PASSWORD": "",
                    "INITIAL_OWNER_EMAIL": "",
                    "INITIAL_OWNER_PASSWORD": "",
                },
                clear=False,
            ):
                for key in (
                    "INITIAL_ADMIN_EMAIL",
                    "INITIAL_ADMIN_PASSWORD",
                    "INITIAL_CASHIER_EMAIL",
                    "INITIAL_CASHIER_PASSWORD",
                    "INITIAL_OWNER_EMAIL",
                    "INITIAL_OWNER_PASSWORD",
                ):
                    os.environ.pop(key, None)
                ensure_initial_local_data()
            self.assertEqual(Shop.query.filter_by(name="Empty Cred Shop").count(), 1)
            self.assertEqual(Staff.query.count(), 0)

    def test_demo_seed_opt_in_still_available_for_development(self):
        with self.app.app_context():
            with patch.dict(
                os.environ,
                {
                    "ALLOW_DEMO_SEED": "true",
                    "INITIAL_SHOP_NAME": "Demo Shop",
                    "INITIAL_ADMIN_EMAIL": "",
                    "INITIAL_ADMIN_PASSWORD": "",
                },
                clear=False,
            ):
                for key in (
                    "INITIAL_ADMIN_EMAIL",
                    "INITIAL_ADMIN_PASSWORD",
                    "INITIAL_CASHIER_EMAIL",
                    "INITIAL_CASHIER_PASSWORD",
                    "INITIAL_OWNER_EMAIL",
                    "INITIAL_OWNER_PASSWORD",
                ):
                    os.environ.pop(key, None)
                ensure_initial_local_data()
            self.assertIsNotNone(Staff.query.filter_by(email="admin@glr.test").first())
            self.assertIsNotNone(Staff.query.filter_by(email="cashier@glr.test").first())

    def test_central_bootstrap_creates_owner_from_env(self):
        class BootstrapFirestore:
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

        env = {
            "ALLOW_DEMO_SEED": "false",
            "INITIAL_SHOP_NAME": "Central Customer Shop",
            "INITIAL_OWNER_EMAIL": "owner@customer.shop",
            "INITIAL_OWNER_PASSWORD": "OwnerPass!42",
            "INITIAL_OWNER_NAME": "Owner",
            "INITIAL_ADMIN_EMAIL": "admin@customer.shop",
            "INITIAL_ADMIN_PASSWORD": "AdminPass!42",
            "INITIAL_PRODUCT_SKU": "CUST-001",
            "INITIAL_PRODUCT_NAME": "Customer Product",
            "INITIAL_PRODUCT_CATEGORY": "General",
            "INITIAL_PRODUCT_UNIT_PRICE": "25",
            "INITIAL_PRODUCT_COST_PRICE": "10",
        }
        service = BootstrapFirestore()
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=service):
            with patch.dict(os.environ, env, clear=False):
                ensure_initial_central_data()
            owner = service.get_staff_by_email("owner@customer.shop")
            self.assertEqual(owner["role"], "owner")
            self.assertTrue(check_password_hash(owner["password_hash"], "OwnerPass!42"))
            self.assertEqual(service.products[0]["sku"], "CUST-001")


class CustomerFacingMessageTests(unittest.TestCase):
    def setUp(self):
        from app.routes.staff import staff_bp

        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            JWT_SECRET_KEY="test-jwt",
        )
        db.init_app(self.app)
        self.app.register_blueprint(staff_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add(
                Staff(
                    id=1,
                    name="Admin",
                    email="admin@customer.shop",
                    password_hash="unused",
                    role="admin",
                    shop_id=1,
                )
            )
            db.session.add(Shop(id=1, name="Customer Shop"))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_staff_create_offline_message_is_customer_friendly(self):
        from app.auth import issue_token

        with self.app.app_context():
            token = issue_token(db.session.get(Staff, 1))
        response = self.app.test_client().post(
            "/api/staff",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "New Seller",
                "email": "new@customer.shop",
                "password": "secret12",
                "role": "cashier",
            },
        )
        self.assertEqual(response.status_code, 403)
        message = response.get_json()["error"]
        self.assertNotIn("UNAUTHORIZED", message)
        self.assertNotIn("central server", message.lower())
        self.assertIn("online", message.lower())
