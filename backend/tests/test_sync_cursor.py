import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.extensions import db
from app.models import Device, Product, Shop, Staff, StockMovement, SyncState
from app.routes.sync import sync_bp
from app.sync.worker import LAST_PULL_KEY, pull_reference_data_once


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class SyncCursorTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            SYNC_API_KEY="test-sync-key",
            CENTRAL_SYNC_URL="http://central.test",
            DEVICE_ID_FILE=Path("cursor-device-id.txt"),
        )
        db.init_app(self.app)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Main Shop"),
                Staff(id=1, name="Admin", email="admin@cursor.test", password_hash="unused", role="admin"),
                Device(id="cursor-device", shop_id=1),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        device_path = self.app.config["DEVICE_ID_FILE"]
        if device_path.exists():
            device_path.unlink()

    def test_boundary_timestamp_replays_rows_committed_after_prior_pull(self):
        boundary = datetime(2026, 1, 2, 3, 4, 5)
        with self.app.app_context():
            shop = db.session.get(Shop, 1)
            staff = db.session.get(Staff, 1)
            shop.created_at = shop.updated_at = boundary
            staff.created_at = staff.updated_at = boundary
            db.session.add(Product(
                id=1, sku="FIRST", name="First", unit_price=1, cost_price=1,
                created_at=boundary, updated_at=boundary,
            ))
            db.session.add(StockMovement(
                id="FIRST-MOVEMENT", product_id=1, shop_id=1, quantity_delta=1,
                reason="restock", created_at=boundary, updated_at=boundary,
            ))
            db.session.commit()

        client = self.app.test_client()
        headers = {"X-Sync-Key": "test-sync-key", "X-Device-ID": "cursor-device"}
        first = client.get("/api/sync/pull", headers=headers)
        self.assertEqual(first.status_code, 200)
        cursor = first.get_json()["next_cursor"]
        self.assertEqual(cursor, boundary.isoformat())

        # Simulate a central commit after the first pull's watermark was read,
        # with the exact same timestamp as that watermark.
        with self.app.app_context():
            db.session.add(Product(
                id=2, sku="LATE", name="Late boundary row", unit_price=1, cost_price=1,
                created_at=boundary, updated_at=boundary,
            ))
            db.session.add(StockMovement(
                id="LATE-MOVEMENT", product_id=2, shop_id=1, quantity_delta=1,
                reason="restock", created_at=boundary, updated_at=boundary,
            ))
            db.session.commit()

        second = client.get(
            "/api/sync/pull", query_string={"since": cursor},
            headers=headers,
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual({row["sku"] for row in second.get_json()["products"]}, {"FIRST", "LATE"})

    def test_pull_includes_unchanged_product_referenced_by_changed_movement(self):
        old = datetime(2026, 1, 1, 0, 0, 0)
        boundary = datetime(2026, 1, 2, 3, 4, 5)
        with self.app.app_context():
            shop = db.session.get(Shop, 1)
            staff = db.session.get(Staff, 1)
            shop.created_at = shop.updated_at = old
            staff.created_at = staff.updated_at = old
            db.session.add(Product(
                id=7, sku="P-7", name="Product Seven", unit_price=10, cost_price=5,
                created_at=old, updated_at=old,
            ))
            db.session.commit()

        client = self.app.test_client()
        headers = {"X-Sync-Key": "test-sync-key", "X-Device-ID": "cursor-device"}
        first = client.get("/api/sync/pull", headers=headers)
        self.assertEqual(first.status_code, 200)
        cursor = first.get_json()["next_cursor"]

        with self.app.app_context():
            db.session.add(StockMovement(
                id="MOV-7", product_id=7, shop_id=1, quantity_delta=-1,
                reason="sale", created_at=boundary, updated_at=boundary,
            ))
            db.session.commit()

        second = client.get(
            "/api/sync/pull", query_string={"since": cursor},
            headers=headers,
        )
        self.assertEqual(second.status_code, 200)
        payload = second.get_json()
        self.assertEqual({row["id"] for row in payload["products"]}, {7})
        self.assertEqual(len(payload["stock_movements"]), 1)

    def test_pull_creates_local_shadow_row_for_staff_unknown_to_this_device(self):
        """A staff account created on another PC (or the central dashboard)
        must show up here too, without that staff member first logging in
        on this specific device. See app/sync/worker.py's staff loop."""
        response = _Response({
            "next_cursor": "2026-01-02T03:04:05",
            "shops": [], "settings": [],
            "staff": [{
                "id": 2, "shop_id": 1, "name": "New Cashier",
                "email": "new.cashier@cursor.test", "role": "cashier", "is_active": True,
            }],
            "products": [], "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
        })
        with patch("app.sync.worker.requests.get", return_value=response):
            result = pull_reference_data_once(self.app)

        self.assertEqual(result["staff_needing_provisioning"], 0)
        with self.app.app_context():
            staff = db.session.get(Staff, 2)
            self.assertIsNotNone(staff)
            self.assertEqual(staff.name, "New Cashier")
            self.assertEqual(staff.email, "new.cashier@cursor.test")
            self.assertEqual(staff.role, "cashier")
            self.assertTrue(staff.is_active)
            # No credentials travel in the sync payload -- login on this
            # device still always re-verifies against central.
            self.assertEqual(staff.password_hash, "")
            self.assertIsNone(staff.quick_pin_hash)

    def test_worker_prefers_next_cursor_over_legacy_server_time(self):
        response = _Response({
            "next_cursor": "2026-01-02T03:04:05",
            "server_time": "2099-01-01T00:00:00+00:00",
            "shops": [], "staff": [], "products": [], "settings": [],
            "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
        })
        with patch("app.sync.worker.requests.get", return_value=response):
            pull_reference_data_once(self.app)

        with self.app.app_context():
            self.assertEqual(db.session.get(SyncState, LAST_PULL_KEY).value, "2026-01-02T03:04:05")
