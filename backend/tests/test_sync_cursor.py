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

    def test_pull_syncs_quick_pin_so_it_works_on_a_new_device(self):
        """A PIN set on one PC must work as the same PIN on any other device
        logged into that account -- it's checked locally on each shop PC, so
        it has to travel here, unlike password_hash which never does."""
        response = _Response({
            "next_cursor": "2026-01-02T03:04:05",
            "shops": [], "settings": [],
            "staff": [{
                "id": 2, "shop_id": 1, "name": "Owner",
                "email": "owner@cursor.test", "role": "owner", "is_active": True,
                "quick_pin_hash": "scrypt:32768:8:1$fakehash",
                "quick_pin_failed_attempts": 1,
                "quick_pin_locked_until": "2026-01-02T03:04:05+00:00",
            }],
            "products": [], "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
        })
        with patch("app.sync.worker.requests.get", return_value=response):
            pull_reference_data_once(self.app)

        with self.app.app_context():
            staff = db.session.get(Staff, 2)
            self.assertEqual(staff.quick_pin_hash, "scrypt:32768:8:1$fakehash")
            self.assertEqual(staff.quick_pin_failed_attempts, 1)
            self.assertIsNotNone(staff.quick_pin_locked_until)

    def test_concurrent_pull_calls_do_not_run_at_the_same_time(self):
        """Login spawns nothing extra anymore (see routes/auth.py), but the
        routine background loop and a manual "Sync Now" click can still both
        try to pull at once. Two threads calling pull_reference_data_once
        concurrently must never both reach the network/DB work at the same
        time -- concurrent, uncoordinated access to the same SQLite session
        from two threads is what used to crash the process outright, not
        just respond slowly."""
        import threading
        import time as time_module

        release = threading.Event()
        call_count = {"n": 0}
        lock = threading.Lock()

        class _SlowResponse:
            def raise_for_status(self):
                return None

            def json(self):
                with lock:
                    call_count["n"] += 1
                release.wait(timeout=2)
                return {
                    "next_cursor": "2026-01-02T03:04:05",
                    "shops": [], "staff": [], "products": [], "settings": [],
                    "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
                }

        results = []

        def run():
            with patch("app.sync.worker.requests.get", return_value=_SlowResponse()):
                results.append(pull_reference_data_once(self.app))

        t1 = threading.Thread(target=run)
        t1.start()
        time_module.sleep(0.1)  # let t1 reach and start blocking inside json()
        t2 = threading.Thread(target=run)
        t2.start()
        time_module.sleep(0.1)  # give t2 a chance to try (and be turned away)
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

        self.assertEqual(call_count["n"], 1, "only one thread should have reached the network call")
        self.assertEqual(len(results), 2)
        # The turned-away caller gets the same "nothing happened" shape as
        # every other early-return in pull_reference_data_once.
        skipped = [r for r in results if r == {"shops": 0, "staff": 0, "products": 0, "sales": 0, "payments": 0, "stock_movements": 0}]
        self.assertEqual(len(skipped), 1)

    def test_last_pull_changed_at_only_advances_when_something_actually_changed(self):
        """The frontend uses this timestamp to decide whether to silently
        refresh the screen. It must not advance on a pull that brought in
        nothing (the common case, thanks to the marker short-circuit in
        _pull_incremental) -- otherwise the UI would re-fetch every cycle
        regardless of whether there was ever anything new to show."""
        empty_response = _Response({
            "next_cursor": "2026-01-02T03:04:05",
            "shops": [], "staff": [], "products": [], "settings": [],
            "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
        })
        with patch("app.sync.worker.requests.get", return_value=empty_response):
            pull_reference_data_once(self.app)
        with self.app.app_context():
            self.assertIsNone(db.session.get(SyncState, "last_pull_changed_at"))

        changed_response = _Response({
            "next_cursor": "2026-01-02T04:04:05",
            "shops": [], "staff": [], "settings": [],
            "products": [{
                "id": 99, "sku": "P-99", "name": "New Product", "unit_price": "5.00",
                "cost_price": "3.00", "is_active": True, "shop_ids": [1],
            }],
            "sales": [], "sale_items": [], "payments": [], "stock_movements": [],
        })
        with patch("app.sync.worker.requests.get", return_value=changed_response):
            pull_reference_data_once(self.app)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(SyncState, "last_pull_changed_at"))

    def test_manual_sync_now_pushes_and_pulls(self):
        """The SYNC NOW button used to only push, so a PC could show
        "Synced" while still missing what the owner had just added. It has
        to do both directions."""
        from app.auth import register_local_session
        from app.sync import worker

        calls = []
        with patch.object(worker, "push_pending_once", side_effect=lambda app: calls.append("push") or {}), \
             patch.object(worker, "pull_reference_data_once", side_effect=lambda app: calls.append("pull") or {}):
            with self.app.app_context():
                register_local_session("tok", {"id": 1, "role": "admin", "shop_id": 1, "is_active": True})
                thread = worker.trigger_full_sync_now(self.app)
            self.assertIsNotNone(thread)
            thread.join(timeout=3)
        self.assertEqual(calls, ["push", "pull"])

    def test_manual_sync_now_does_nothing_without_a_session(self):
        from app.sync import worker

        with patch.object(worker, "push_pending_once") as push, \
             patch.object(worker, "pull_reference_data_once") as pull:
            with self.app.app_context():
                self.assertIsNone(worker.trigger_full_sync_now(self.app))
        push.assert_not_called()
        pull.assert_not_called()

    def test_sync_trigger_route_starts_a_full_sync(self):
        from app.auth import register_local_session
        from app.sync import worker

        with patch.object(worker, "push_pending_once", return_value={}), \
             patch.object(worker, "pull_reference_data_once", return_value={}) as pull:
            with self.app.app_context():
                register_local_session("route-token", {"id": 1, "role": "admin", "shop_id": 1, "is_active": True})
            response = self.app.test_client().post(
                "/api/sync/trigger", headers={"Authorization": "Bearer route-token"},
            )
            self.assertEqual(response.status_code, 200)
            for t in __import__("threading").enumerate():
                if t.name == "glr-full-sync":
                    t.join(timeout=3)
        pull.assert_called()

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
