import unittest
from datetime import timezone

from flask import Flask

from app.extensions import db
from app.models import Product, Shop, Staff
from app.models.transactions import Sale, SalePayment, StockMovement
from app.sync.worker import _parse_datetime, _upsert_transactions


class SyncTimestampParsingTests(unittest.TestCase):
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
            db.session.add_all([
                Shop(id=1, name="Main Shop"),
                Staff(id=1, name="Admin", email="admin@example.test", password_hash="unused", role="admin"),
                Product(id=1, sku="TEST-1", name="Test Product", unit_price=10, cost_price=5),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_parse_iso8601_preserves_offset_and_null(self):
        utc_value = _parse_datetime("2026-01-02T03:04:05Z")
        offset_value = _parse_datetime("2026-01-02T05:04:05+02:00")

        self.assertEqual(utc_value.tzinfo, timezone.utc)
        self.assertEqual(offset_value.tzinfo, timezone.utc)
        self.assertEqual(offset_value.isoformat(), "2026-01-02T03:04:05+00:00")
        self.assertIsNone(_parse_datetime(None))

    def test_upsert_skips_stock_movement_when_product_missing(self):
        payload = {
            "sales": [],
            "sale_items": [],
            "payments": [],
            "stock_movements": [{
                "id": "orphan-movement",
                "product_id": 99,
                "shop_id": 1,
                "device_id": None,
                "quantity_delta": -1,
                "reason": "sale",
                "reference_id": "sale-x",
                "created_at": "2026-01-02T03:04:05Z",
                "updated_at": "2026-01-02T03:04:05Z",
                "server_received_at": None,
            }],
        }
        with self.app.app_context():
            _upsert_transactions(payload)
            db.session.commit()
            self.assertIsNone(db.session.get(StockMovement, "orphan-movement"))

    def test_upsert_commits_iso_timestamps_and_nullable_fields(self):
        payload = {
            "sales": [{
                "id": "sale-1", "invoice_number": "INV-2026-1001", "shop_id": 1,
                "device_id": None, "staff_id": 1, "customer_name": "Customer",
                "payment_method": "cash", "total_amount": "10.00",
                "created_at": "2026-01-02T03:04:05Z",
                "updated_at": "2026-01-02T05:04:05+02:00",
                "server_received_at": None, "voided_at": None,
                "voided_by_staff_id": None, "void_reason": None,
            }],
            "sale_items": [{
                "id": "item-1", "sale_id": "sale-1", "product_id": 1,
                "quantity": 1, "unit_price": "10.00", "subtotal": "10.00", "unit_cost": "5.00",
            }],
            "payments": [{
                "id": "payment-1", "sale_id": "sale-1", "amount": "10.00",
                "device_id": None, "staff_id": 1,
                "created_at": "2026-01-02T03:04:05+00:00",
                "updated_at": "2026-01-02T03:05:05+00:00", "server_received_at": None,
            }],
            "stock_movements": [{
                "id": "movement-1", "product_id": 1, "shop_id": 1, "device_id": None,
                "quantity_delta": -1, "reason": "sale", "reference_id": "sale-1",
                "created_at": "2026-01-02T03:04:05Z",
                "updated_at": "2026-01-02T03:04:05Z", "server_received_at": None,
            }],
        }

        with self.app.app_context():
            _upsert_transactions(payload)
            db.session.commit()
            sale = db.session.get(Sale, "sale-1")
            payment = db.session.get(SalePayment, "payment-1")
            movement = db.session.get(StockMovement, "movement-1")
            self.assertIsNotNone(sale.created_at)
            self.assertIsNotNone(payment.created_at)
            self.assertIsNotNone(movement.created_at)
            # SQLite reloads the project's timezone-naive DateTime columns,
            # so the stored UTC wall time must represent the original instant.
            self.assertEqual(sale.created_at.isoformat(), "2026-01-02T03:04:05")
            self.assertEqual(sale.updated_at.isoformat(), "2026-01-02T03:04:05")
            self.assertIsNone(sale.server_received_at)
            self.assertIsNone(sale.voided_at)
            self.assertIsNone(payment.server_received_at)
            self.assertIsNone(movement.server_received_at)
