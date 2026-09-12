import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.extensions import db
from app.models import Product, Shop, Staff, SyncState
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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(self.temp_dir.name) / 'sync.sqlite'}",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            SYNC_API_KEY="test-sync-key",
            CENTRAL_SYNC_URL="http://central.test",
        )
        db.init_app(self.app)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Main Shop"),
                Staff(id=1, name="Admin", email="admin@cursor.test", password_hash="unused", role="admin"),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp_dir.cleanup()

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
            db.session.commit()

        client = self.app.test_client()
        first = client.get("/api/sync/pull", headers={"X-Sync-Key": "test-sync-key"})
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
            db.session.commit()

        second = client.get(
            "/api/sync/pull", query_string={"since": cursor},
            headers={"X-Sync-Key": "test-sync-key"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual({row["sku"] for row in second.get_json()["products"]}, {"FIRST", "LATE"})

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
