"""One-time credential bootstrap for existing production admin accounts."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

from app.bootstrap import (
    BOOTSTRAP_CREDENTIAL_MARKER_KEY,
    apply_one_time_credential_bootstrap,
)
from app.extensions import db
from app.models import Product, Sale, Shop, Staff, SystemSetting


class OneTimeCredentialBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
        )
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()
            shop = Shop(id=1, name="Customer Shop")
            demo_admin = Staff(
                id=1,
                shop_id=1,
                name="Demo Admin",
                email="admin@glr.test",
                password_hash=generate_password_hash("admin123"),
                role="admin",
            )
            other_staff = Staff(
                id=2,
                shop_id=1,
                name="Seller",
                email="cashier@customer.shop",
                password_hash=generate_password_hash("seller-secret"),
                role="cashier",
            )
            product = Product(
                id=10,
                sku="KEEP-1",
                name="Keep Product",
                category="General",
                unit_price=12,
                cost_price=5,
            )
            sale = Sale(
                id="sale-keep",
                shop_id=1,
                staff_id=2,
                customer_name="Keep Customer",
                total_amount=12,
                invoice_number="INV-KEEP-1",
            )
            db.session.add_all([shop, demo_admin, other_staff, product, sale])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _bootstrap_env(self, **overrides):
        env = {
            "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "once-token-abc",
            "BOOTSTRAP_TARGET_EMAIL": "admin@glr.test",
            "BOOTSTRAP_NEW_PASSWORD": "RealOwnerPass!99",
            "BOOTSTRAP_NEW_EMAIL": "owner@customer.shop",
            "BOOTSTRAP_NEW_NAME": "Shop Owner",
            "BOOTSTRAP_NEW_ROLE": "owner",
        }
        env.update(overrides)
        return env

    def test_existing_demo_account_updated_on_first_application(self):
        with self.app.app_context():
            with patch.dict(os.environ, self._bootstrap_env(), clear=False):
                result = apply_one_time_credential_bootstrap()
            self.assertTrue(result["applied"])
            staff = Staff.query.filter_by(email="owner@customer.shop").one()
            self.assertEqual(staff.id, 1)
            self.assertEqual(staff.name, "Shop Owner")
            self.assertEqual(staff.role, "owner")
            self.assertIsNone(Staff.query.filter_by(email="admin@glr.test").first())

    def test_new_email_applied_correctly(self):
        with self.app.app_context():
            with patch.dict(os.environ, self._bootstrap_env(), clear=False):
                apply_one_time_credential_bootstrap()
            self.assertEqual(db.session.get(Staff, 1).email, "owner@customer.shop")

    def test_password_is_hashed_and_never_stored_plaintext(self):
        plaintext = "RealOwnerPass!99"
        with self.app.app_context():
            with patch.dict(os.environ, self._bootstrap_env(BOOTSTRAP_NEW_PASSWORD=plaintext), clear=False):
                apply_one_time_credential_bootstrap()
            staff = db.session.get(Staff, 1)
            self.assertNotEqual(staff.password_hash, plaintext)
            self.assertNotIn(plaintext, staff.password_hash)
            self.assertTrue(check_password_hash(staff.password_hash, plaintext))
            marker = SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).one()
            self.assertNotIn(plaintext, marker.value or "")

    def test_same_token_second_startup_is_noop(self):
        with self.app.app_context():
            with patch.dict(os.environ, self._bootstrap_env(), clear=False):
                first = apply_one_time_credential_bootstrap()
                hash_after_first = db.session.get(Staff, 1).password_hash
                second = apply_one_time_credential_bootstrap()
            self.assertTrue(first["applied"])
            self.assertFalse(second["applied"])
            self.assertEqual(second["reason"], "already_applied")
            self.assertEqual(db.session.get(Staff, 1).password_hash, hash_after_first)

    def test_missing_or_invalid_bootstrap_config_does_nothing(self):
        with self.app.app_context():
            original_hash = db.session.get(Staff, 1).password_hash
            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "",
                    "BOOTSTRAP_TARGET_EMAIL": "admin@glr.test",
                    "BOOTSTRAP_NEW_PASSWORD": "ignored",
                },
                clear=False,
            ):
                for key in (
                    "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN",
                    "BOOTSTRAP_TARGET_EMAIL",
                    "BOOTSTRAP_NEW_PASSWORD",
                    "BOOTSTRAP_NEW_EMAIL",
                ):
                    os.environ.pop(key, None)
                result = apply_one_time_credential_bootstrap()
            self.assertEqual(result, {"applied": False, "reason": "missing_config"})
            self.assertEqual(db.session.get(Staff, 1).email, "admin@glr.test")
            self.assertEqual(db.session.get(Staff, 1).password_hash, original_hash)
            self.assertIsNone(
                SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).first()
            )

    def test_existing_customer_data_remains_untouched(self):
        with self.app.app_context():
            with patch.dict(os.environ, self._bootstrap_env(), clear=False):
                apply_one_time_credential_bootstrap()
            seller = db.session.get(Staff, 2)
            product = db.session.get(Product, 10)
            sale = db.session.get(Sale, "sale-keep")
            self.assertEqual(seller.email, "cashier@customer.shop")
            self.assertTrue(check_password_hash(seller.password_hash, "seller-secret"))
            self.assertEqual(product.sku, "KEEP-1")
            self.assertEqual(product.name, "Keep Product")
            self.assertEqual(sale.invoice_number, "INV-KEEP-1")
            self.assertEqual(sale.customer_name, "Keep Customer")
            self.assertEqual(db.session.get(Shop, 1).name, "Customer Shop")
