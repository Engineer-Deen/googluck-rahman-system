"""Isolated offline/reconnect and provider-failure coverage using fakes only."""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from flask import Flask
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.firestore.service import FirestoreSyncService
from app.models import Device, Product, Sale, SaleItem, SalePayment, Shop, Staff, StockMovement, SyncOutboxItem, SyncState
from app.routes.auth import auth_bp
from app.routes.sales import sales_bp
from app.routes.sync import sync_bp
from app.sync.outbox import enqueue_outbox
from app.sync.worker import pull_reference_data_once, push_pending_once
from tests.test_firestore_provider import FakeFirestoreClient, _seed_catalog_product


class OfflineReconnectTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            JWT_SECRET_KEY="test-secret",
            CENTRAL_SYNC_URL="http://central.test",
            SYNC_API_KEY="sync-secret",
            DEVICE_ID_FILE=Path("offline-device-id.txt"),
        )
        db.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(sales_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Shop One"),
                Staff(
                    id=1,
                    shop_id=1,
                    name="Cashier",
                    email="cashier@offline.test",
                    password_hash=generate_password_hash("secret"),
                    role="cashier",
                ),
                Product(id=1, sku="P-1", name="Item", unit_price=10, cost_price=4),
                StockMovement(id="stock-seed", product_id=1, shop_id=1, quantity_delta=5, reason="restock"),
            ])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        device_path = self.app.config["DEVICE_ID_FILE"]
        if device_path.exists():
            device_path.unlink()

    def _login(self):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": "cashier@offline.test", "password": "secret", "role_group": "seller"},
        )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def test_local_sale_queues_outbox_without_central_contact(self):
        client, headers = self._login()
        with patch("app.sync.worker.trigger_sync_soon") as trigger, \
             patch("app.sync.worker.requests.post") as post:
            response = client.post(
                "/api/sales",
                headers=headers,
                json={
                    "id": "offline-sale-1",
                    "customer_name": "Offline Buyer",
                    "amount_paid": "10.00",
                    "payment_id": "offline-pay-1",
                    "items": [{
                        "id": "offline-item-1",
                        "product_id": 1,
                        "quantity": 1,
                        "unit_price": "10.00",
                        "stock_movement_id": "offline-move-1",
                    }],
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(trigger.called)
        self.assertFalse(post.called)
        with self.app.app_context():
            sale = db.session.get(Sale, "offline-sale-1")
            self.assertIsNotNone(sale)
            self.assertIsNone(sale.invoice_number)
            outbox = SyncOutboxItem.query.filter_by(record_id="offline-sale-1", status="pending").one()
            payload = json.loads(outbox.payload_json)
            self.assertEqual(payload["id"], "offline-sale-1")
            self.assertEqual(SaleItem.query.filter_by(sale_id="offline-sale-1").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(id="offline-move-1").count(), 1)

    def test_network_failure_keeps_outbox_and_does_not_corrupt_sale(self):
        with self.app.app_context():
            db.session.add(Sale(id="pending-sale", shop_id=1, staff_id=1, customer_name="Pending", total_amount=10))
            db.session.add(SyncOutboxItem(
                id=11,
                table_name="sales",
                record_id="pending-sale",
                status="pending",
                payload_json='{"id":"pending-sale","shop_id":1,"device_id":"device-one","items":[]}',
            ))
            db.session.commit()

        with patch("app.sync.worker.get_current_device_id", return_value="device-one"), \
             patch("app.sync.worker.requests.post", side_effect=requests.ConnectionError("offline")):
            result = push_pending_once(self.app)

        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(result["failed"], 1)
        with self.app.app_context():
            item = db.session.get(SyncOutboxItem, 11)
            self.assertEqual(item.status, "pending")
            self.assertEqual(item.attempt_count, 1)
            self.assertIn("offline", item.last_error)
            self.assertIsNotNone(db.session.get(Sale, "pending-sale"))

    def test_missing_sync_key_keeps_local_pos_and_reports_configuration(self):
        self.app.config["SYNC_API_KEY"] = ""
        with self.app.app_context():
            db.session.add(SyncOutboxItem(
                id=12,
                table_name="sales",
                record_id="pending-sale",
                status="pending",
                payload_json='{"id":"pending-sale","items":[]}',
            ))
            db.session.commit()

        with patch("app.sync.worker.requests.post") as post, patch("app.sync.worker.requests.get") as get:
            pushed = push_pending_once(self.app)
            pulled = pull_reference_data_once(self.app)

        self.assertEqual(pushed["failed"], 1)
        self.assertEqual(pulled["sales"], 0)
        self.assertFalse(post.called)
        self.assertFalse(get.called)
        with self.app.app_context():
            state = {row.key: row.value for row in SyncState.query.all()}
            self.assertEqual(state["last_sync_error"], "Cloud synchronization is not configured on this device.")
            self.assertEqual(state["last_pull_error"], "Cloud synchronization is not configured on this device.")

    def test_reconnect_confirms_outbox_idempotently_without_duplicate_rows(self):
        with self.app.app_context():
            db.session.add(Sale(id="reconnect-sale", shop_id=1, staff_id=1, customer_name="Reconnect", total_amount=10))
            db.session.add(SyncOutboxItem(
                id=21,
                table_name="sales",
                record_id="reconnect-sale",
                status="pending",
                payload_json='{"id":"reconnect-sale","shop_id":1,"device_id":"device-one","items":[]}',
            ))
            db.session.commit()

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{"outbox_id": 21, "status": "ok", "invoice_number": "INV-2026-1500"}]}

        with patch("app.sync.worker.get_current_device_id", return_value="device-one"), \
             patch("app.sync.worker.requests.post", return_value=_Response()) as post:
            first = push_pending_once(self.app)
            second = push_pending_once(self.app)

        self.assertEqual(first, {"pushed": 1, "confirmed": 1, "failed": 0})
        self.assertEqual(second, {"pushed": 0, "confirmed": 0, "failed": 0})
        self.assertEqual(post.call_count, 1)
        with self.app.app_context():
            self.assertIsNone(db.session.get(SyncOutboxItem, 21))
            self.assertEqual(db.session.get(Sale, "reconnect-sale").invoice_number, "INV-2026-1500")
            self.assertEqual(Sale.query.filter_by(id="reconnect-sale").count(), 1)


class FirestoreProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.client = FakeFirestoreClient()
        self.service = FirestoreSyncService(self.client)
        _seed_catalog_product(self.client, 10)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
            CENTRAL_DATA_PROVIDER="firestore",
            SYNC_API_KEY="sync-secret",
            FIRESTORE_SYNC_SERVICE=self.service,
        )
        db.init_app(self.app)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([Shop(id=1, name="Shop A"), Device(id="device-a", shop_id=1)])
            db.session.commit()
        self.client.collections["devices"]["device-a"] = {
            "id": "device-a", "shop_id": 1, "authorized": True,
        }

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_firestore_push_idempotent_under_retry(self):
        payload = {
            "id": "sale-retry",
            "shop_id": 1,
            "device_id": "device-a",
            "staff_id": 1,
            "customer_name": "Retry",
            "amount_paid": "10.00",
            "created_at": "2026-01-01T01:00:00+00:00",
            "items": [{"id": "item-retry", "product_id": 10, "quantity": 1, "unit_price": "10.00", "stock_movement_id": "move-retry"}],
        }
        headers = {"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"}
        body = {"device_id": "device-a", "items": [{"outbox_id": 1, "table_name": "sales", "payload": payload}]}
        first = self.app.test_client().post("/api/sync/push", headers=headers, json=body)
        second = self.app.test_client().post("/api/sync/push", headers=headers, json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["results"][0]["status"], "ok")
        self.assertEqual(second.get_json()["results"][0]["status"], "ok")
        self.assertEqual(first.get_json()["results"][0]["invoice_number"], second.get_json()["results"][0]["invoice_number"])
        self.assertEqual(len(self.client.collections["sales"]), 1)
        self.assertEqual(len(self.client.collections["sale_items"]), 1)
        self.assertEqual(len(self.client.collections["stock_movements"]), 1)

    def test_firestore_unavailable_on_pull_returns_503_without_false_success(self):
        class BoomService:
            def get_device(self, device_id):
                return {"id": device_id, "shop_id": 1, "authorized": True}

            def save_device(self, device_id, **fields):
                return {"id": device_id, "shop_id": 1, "authorized": True, **fields}

            def pull(self, shop_id, since):
                raise RuntimeError("quota exceeded")

        self.app.config["FIRESTORE_SYNC_SERVICE"] = BoomService()
        response = self.app.test_client().get(
            "/api/sync/pull",
            headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.get_json()["error"].lower())

    def test_firestore_push_item_failure_is_reported_per_item_with_rollback(self):
        class BoomPushService:
            def get_device(self, device_id):
                return {"id": device_id, "shop_id": 1, "authorized": True}

            def save_device(self, device_id, **fields):
                return {"id": device_id, "shop_id": 1, "authorized": True, **fields}

            def push_item(self, device, table_name, payload):
                raise RuntimeError("deadline exceeded")

        self.app.config["FIRESTORE_SYNC_SERVICE"] = BoomPushService()
        response = self.app.test_client().post(
            "/api/sync/push",
            headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"},
            json={
                "device_id": "device-a",
                "items": [{
                    "outbox_id": 9,
                    "table_name": "sales",
                    "payload": {
                        "id": "sale-fail",
                        "shop_id": 1,
                        "device_id": "device-a",
                        "items": [{"product_id": 1, "quantity": 1, "unit_price": "10.00"}],
                    },
                }],
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIn("deadline exceeded", result["error"])
        with self.app.app_context():
            self.assertIsNone(db.session.get(Sale, "sale-fail"))


class ConcurrentInvoiceAllocationTests(unittest.TestCase):
    def test_concurrent_firestore_invoice_allocation_is_unique(self):
        client = FakeFirestoreClient()
        services = [FirestoreSyncService(client), FirestoreSyncService(client), FirestoreSyncService(client)]
        results = []

        def allocate(service):
            results.append(service.allocate_invoice_number({"created_at": "2026-06-01T00:00:00+00:00"}))

        threads = [threading.Thread(target=allocate, args=(service,)) for service in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), ["INV-2026-1001", "INV-2026-1002", "INV-2026-1003"])
        self.assertEqual(len(set(results)), 3)


class LocalEnqueueIsolationTests(unittest.TestCase):
    def test_enqueue_outbox_is_noop_in_central_mode(self):
        app = Flask(__name__)
        app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
        )
        db.init_app(app)
        with app.app_context():
            db.create_all()
            enqueue_outbox("sales", "central-sale", {"id": "central-sale"})
            self.assertEqual(SyncOutboxItem.query.count(), 0)
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
