import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.config import get_config
from app.firestore.service import FirestoreSyncService, _clean_staff
from app.models import Device, Product, Sale, SaleItem, Staff, Shop, StockMovement
from app.routes.sync import sync_bp


class FakeFirestoreService:
    def __init__(self):
        self.pushed = []
        self.shop_ids = []
        self.devices = {"device-a": {"id": "device-a", "shop_id": 1, "authorized": True}}

    def get_device(self, device_id):
        return self.devices.get(device_id)

    def save_device(self, device_id, **fields):
        self.devices.setdefault(device_id, {"id": device_id}).update(fields)
        return self.devices[device_id]

    def push_item(self, device, table_name, payload):
        device_id = device.get("id") if isinstance(device, dict) else device.id
        shop_id = device.get("shop_id") if isinstance(device, dict) else device.shop_id
        self.pushed.append((device_id, shop_id, table_name, payload.copy()))
        return {"invoice_number": "INV-2026-1001"} if table_name == "sales" else {}

    def pull(self, shop_id, since):
        self.shop_ids.append((shop_id, since))
        return {
            "next_cursor": "2026-01-02T03:04:05+00:00",
            "server_time": "2026-01-02T03:04:05+00:00",
            "shops": [{"id": shop_id, "name": "Authorized Shop"}],
            "staff": [{"id": 1, "shop_id": shop_id, "name": "Cashier", "email": "cashier@test", "role": "cashier", "is_active": True}],
            "settings": [],
            "products": [{"id": 1, "sku": "P-1", "name": "Product", "unit_price": "10.00", "cost_price": "5.00", "is_active": True}],
            "sales": [],
            "sale_items": [],
            "payments": [],
            "stock_movements": [],
        }


class FakeDocumentSnapshot:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, client, collection_name, doc_id):
        self.client = client
        self.collection_name = collection_name
        self.doc_id = str(doc_id)

    def get(self):
        data = self.client.collections.get(self.collection_name, {}).get(self.doc_id)
        return FakeDocumentSnapshot(data is not None, dict(data) if data else None)

    def set(self, data, merge=False):
        bucket = self.client.collections.setdefault(self.collection_name, {})
        existing = dict(bucket.get(self.doc_id, {})) if bucket.get(self.doc_id) else {}
        if merge:
            existing.update(data)
            bucket[self.doc_id] = existing
        else:
            bucket[self.doc_id] = dict(data)

    def delete(self):
        self.client.collections.get(self.collection_name, {}).pop(self.doc_id, None)


class FakeCollection:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self._field = None
        self._op = None
        self._value = None
        self._limit = None

    def document(self, doc_id):
        return FakeDocRef(self.client, self.name, doc_id)

    def where(self, field, op, value):
        self._field = field
        self._op = op
        self._value = value
        return self

    def limit(self, count):
        self._limit = count
        return self

    def stream(self):
        self.client.stream_calls += 1
        docs = []
        for data in self.client.collections.get(self.name, {}).values():
            if self._field is None:
                matches = True
            elif self._op == "==":
                matches = data.get(self._field) == self._value
            elif self._op == "array_contains":
                field_value = data.get(self._field)
                matches = isinstance(field_value, (list, tuple)) and self._value in field_value
            else:
                matches = False
            if matches:
                docs.append(FakeDocumentSnapshot(True, dict(data)))
        if self._limit is not None:
            docs = docs[: self._limit]
        return docs


class FakeBatch:
    def __init__(self, client):
        self.client = client
        self._ops = []

    def set(self, ref, data, merge=False):
        self._ops.append((ref, data, merge))

    def commit(self):
        for ref, data, merge in self._ops:
            ref.set(data, merge=merge)


class FakeTransaction:
    def __init__(self, client):
        self.client = client
        self._lock = client.transaction_lock
        self._lock.acquire()
        self._writes = []
        self._deletes = []
        self._max_attempts = 1
        self._read_only = False
        self._id = None

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        self._id = "fake-transaction"

    def _commit(self):
        self.commit()

    def _rollback(self):
        self._lock.release()

    def get(self, ref):
        return ref.get()

    def set(self, ref, data, merge=False):
        self._writes.append((ref, data, merge))

    def delete(self, ref):
        self._deletes.append(ref)

    def commit(self):
        try:
            for ref in self._deletes:
                ref.delete()
            for ref, data, merge in self._writes:
                ref.set(data, merge=merge)
        finally:
            self._lock.release()


class FakeFirestoreClient:
    def __init__(self):
        self.collections = defaultdict(dict)
        self.transaction_lock = threading.RLock()
        self.stream_calls = 0

    def collection(self, name):
        return FakeCollection(self, name)

    def batch(self):
        return FakeBatch(self)

    def transaction(self):
        return FakeTransaction(self)


def _seed_catalog_product(client, product_id, shop_ids=None):
    client.collections["products"][str(product_id)] = {
        "id": product_id,
        "sku": f"P-{product_id}",
        "name": f"Product {product_id}",
        "category": "General",
        "unit_price": "10.00",
        "cost_price": "5.00",
        "is_active": True,
        "shop_ids": shop_ids or [1],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


class FirestoreAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        self.service = FirestoreSyncService(self.client)
        self.device = type("DeviceLike", (), {"id": "device-a", "shop_id": 1})()
        _seed_catalog_product(self.client, 10)

    def test_firestore_adapter_creates_sales_documents_and_initial_payment(self):
        payload = {
            "id": "sale-1",
            "shop_id": 1,
            "device_id": "device-a",
            "staff_id": 7,
            "customer_name": "Alice",
            "payment_method": "cash",
            "total_amount": "25.00",
            "amount_paid": "25.00",
            "created_at": "2026-01-01T01:00:00+00:00",
            "items": [
                {"id": "sale-item-1", "product_id": 10, "quantity": 2, "unit_price": "10.00", "stock_movement_id": "stock-1"}
            ],
        }

        result = self.service.push_item(self.device, "sales", payload)

        self.assertEqual(result["invoice_number"], "INV-2026-1001")
        self.assertEqual(self.client.collections["sales"]["sale-1"]["invoice_number"], "INV-2026-1001")
        self.assertEqual(self.client.collections["sale_items"]["sale-item-1"]["sale_id"], "sale-1")
        self.assertEqual(self.client.collections["sale_payments"]["sale-1:initial-payment"]["amount"], "25.00")
        self.assertEqual(self.client.collections["stock_movements"]["stock-1"]["quantity_delta"], -2)
        self.assertIn(1, self.client.collections["products"]["10"]["shop_ids"])

    def test_firestore_adapter_is_idempotent_for_duplicate_sale_push(self):
        payload = {
            "id": "sale-1",
            "shop_id": 1,
            "device_id": "device-a",
            "staff_id": 7,
            "customer_name": "Alice",
            "payment_method": "cash",
            "total_amount": "25.00",
            "created_at": "2026-01-01T01:00:00+00:00",
            "items": [
                {"id": "sale-item-1", "product_id": 10, "quantity": 2, "unit_price": "10.00"}
            ],
        }

        first = self.service.push_item(self.device, "sales", payload)
        second = self.service.push_item(self.device, "sales", payload)

        self.assertEqual(first["invoice_number"], second["invoice_number"])
        self.assertEqual(len(self.client.collections["sale_items"]), 1)
        self.assertEqual(len(self.client.collections["sales"]), 1)

    def test_firestore_adapter_continues_invoice_sequence_after_migration(self):
        self.client.collections["sales"]["migrated-sale"] = {
            "id": "migrated-sale",
            "invoice_number": "INV-2026-1008",
        }
        payload = {
            "id": "sale-after-migration",
            "shop_id": 1,
            "device_id": "device-a",
            "staff_id": 7,
            "customer_name": "Alice",
            "items": [{"id": "sale-item-2", "product_id": 10, "quantity": 1, "unit_price": "10.00"}],
            "created_at": "2026-01-01T01:00:00+00:00",
        }

        result = self.service.push_item(self.device, "sales", payload)

        self.assertEqual(result["invoice_number"], "INV-2026-1009")

    def test_invoice_allocator_advances_missing_or_behind_sequence_floor(self):
        self.client.collections["sales"]["migrated-sale"] = {
            "id": "migrated-sale",
            "invoice_number": "INV-2026-1009",
        }
        self.client.collections["sync_metadata"]["invoice_sequence_2026"] = {
            "next_number": 1005,
        }

        first = self.service.allocate_invoice_number({"created_at": "2026-01-01T01:00:00+00:00"})
        second = self.service.allocate_invoice_number({
            "created_at": "2026-01-01T01:00:00+00:00",
            "minimum_next": 1011,
        })

        self.assertEqual(first, "INV-2026-1010")
        self.assertEqual(second, "INV-2026-1011")
        self.assertEqual(self.client.collections["sync_metadata"]["invoice_sequence_2026"]["next_number"], 1012)

    def test_invoice_allocator_accepts_sdk_generator_transaction_read(self):
        class GeneratorTransaction(FakeTransaction):
            def get(self, ref):
                return iter([super().get(ref)])

        original_transaction = self.client.transaction
        self.client.transaction = lambda: GeneratorTransaction(self.client)
        try:
            self.assertEqual(
                self.service.allocate_invoice_number({"created_at": "2026-01-01T01:00:00+00:00"}),
                "INV-2026-1001",
            )
        finally:
            self.client.transaction = original_transaction

    def test_firestore_adapter_rejects_cross_shop_sales_and_wrong_devices(self):
        payload = {
            "id": "sale-cross",
            "shop_id": 2,
            "device_id": "device-a",
            "staff_id": 7,
            "customer_name": "Alice",
            "items": [{"product_id": 10, "quantity": 1, "unit_price": "10.00"}],
        }

        with self.assertRaisesRegex(ValueError, "shop does not match the registered device shop"):
            self.service.push_item(self.device, "sales", payload)

        bad_device = type("BadDevice", (), {"id": "device-b", "shop_id": 1})()
        payload["shop_id"] = 1
        payload["device_id"] = "device-b"

        with self.assertRaisesRegex(ValueError, "device does not match the registered device"):
            self.service.push_item(self.device, "sales", payload)

    def test_firestore_push_rejects_unknown_product_for_stock_movement(self):
        with self.assertRaisesRegex(ValueError, "Unknown product_id 999"):
            self.service.push_item(self.device, "stock_movements", {
                "id": "mov-bad",
                "product_id": 999,
                "quantity_delta": 1,
                "reason": "restock",
            })

    def test_firestore_push_rejects_unknown_product_for_sale(self):
        with self.assertRaisesRegex(ValueError, "Unknown product_id 999"):
            self.service.push_item(self.device, "sales", {
                "id": "sale-bad",
                "shop_id": 1,
                "device_id": "device-a",
                "staff_id": 7,
                "items": [{"id": "item-bad", "product_id": 999, "quantity": 1, "unit_price": "10.00"}],
            })

    def test_firestore_pull_includes_parent_product_for_incremental_child_rows(self):
        self.client.collections["products"]["7"] = {
            "id": 7,
            "sku": "P-7",
            "name": "Existing Product",
            "unit_price": "15.00",
            "cost_price": "8.00",
            "is_active": True,
            "shop_ids": [1],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        self.client.collections["stock_movements"]["mov-7"] = {
            "id": "mov-7",
            "product_id": 7,
            "shop_id": 1,
            "device_id": "device-a",
            "quantity_delta": -1,
            "reason": "sale",
            "reference_id": "sale-7",
            "updated_at": "2026-01-02T06:00:00+00:00",
        }
        since = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        payload = self.service.pull(1, since)
        product_ids = {row["id"] for row in payload["products"]}
        self.assertIn(7, product_ids)
        self.assertEqual(len(payload["stock_movements"]), 1)

    def test_firestore_pull_uses_next_cursor_and_sanitizes_staff_rows(self):
        self.client.collections["shops"]["1"] = {"id": 1, "name": "Shop A", "updated_at": "2026-01-02T01:00:00+00:00"}
        self.client.collections["staff"]["1"] = {
            "id": 1,
            "shop_id": 1,
            "name": "Cashier",
            "email": "cashier@test",
            "role": "cashier",
            "is_active": True,
            "password_hash": "secret-password",
            "quick_pin_hash": "secret-pin",
            "token": "secret-token",
            "updated_at": "2026-01-02T02:00:00+00:00",
        }
        self.client.collections["products"]["10"] = {
            "id": 10,
            "sku": "GLR-ACC-0001",
            "name": "Phone",
            "category": "Mobile Accessories",
            "unit_price": "10.00",
            "cost_price": "5.00",
            "is_active": True,
            "shop_ids": [1],
            "updated_at": "2026-01-02T03:00:00+00:00",
        }
        self.client.collections["sales"]["sale-1"] = {
            "id": "sale-1",
            "shop_id": 1,
            "device_id": "device-a",
            "staff_id": 1,
            "customer_name": "Alice",
            "payment_method": "cash",
            "total_amount": "25.00",
            "invoice_number": "INV-2026-1001",
            "created_at": "2026-01-01T01:00:00+00:00",
            "updated_at": "2026-01-02T04:00:00+00:00",
        }
        self.client.collections["sale_items"]["sale-item-1"] = {
            "id": "sale-item-1",
            "sale_id": "sale-1",
            "product_id": 10,
            "quantity": 2,
            "unit_price": "10.00",
            "subtotal": "20.00",
            "unit_cost": "5.00",
        }
        self.client.collections["sale_payments"]["payment-1"] = {
            "id": "payment-1",
            "sale_id": "sale-1",
            "shop_id": 1,
            "amount": "25.00",
            "device_id": "device-a",
            "staff_id": 1,
            "created_at": "2026-01-01T02:00:00+00:00",
            "updated_at": "2026-01-02T05:00:00+00:00",
        }
        self.client.collections["stock_movements"]["stock-1"] = {
            "id": "stock-1",
            "product_id": 10,
            "shop_id": 1,
            "device_id": "device-a",
            "quantity_delta": -2,
            "reason": "sale",
            "reference_id": "sale-1",
            "created_at": "2026-01-01T03:00:00+00:00",
            "updated_at": "2026-01-02T06:00:00+00:00",
        }
        self.client.collections["system_settings"]["timeout_minutes"] = {
            "key": "timeout_minutes",
            "value": "15",
            "updated_at": "2026-01-02T07:00:00+00:00",
        }

        payload = self.service.pull(1, None)

        self.assertEqual(payload["next_cursor"], "2026-01-02T07:00:00+00:00")
        self.assertNotIn("password_hash", payload["staff"][0])
        self.assertNotIn("quick_pin_hash", payload["staff"][0])
        self.assertNotIn("token", payload["staff"][0])
        self.assertEqual(payload["sales"][0]["invoice_number"], "INV-2026-1001")
        self.assertEqual(payload["sale_items"][0]["sale_id"], "sale-1")
        self.assertEqual(payload["payments"][0]["amount"], "25.00")
        self.assertEqual(payload["stock_movements"][0]["quantity_delta"], -2)


class FirestoreProviderTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
            CENTRAL_DATA_PROVIDER="firestore",
            SYNC_API_KEY="sync-secret",
            FIRESTORE_SYNC_SERVICE=FakeFirestoreService(),
        )
        db.init_app(self.app)
        self.app.register_blueprint(sync_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([Shop(id=1, name="Shop A"), Device(id="device-a", shop_id=1)])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_firestore_pull_uses_bound_shop_and_safe_staff_shape(self):
        response = self.app.test_client().get(
            "/api/sync/pull",
            headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["shops"][0]["id"], 1)
        self.assertNotIn("password_hash", payload["staff"][0])
        self.assertNotIn("quick_pin_hash", payload["staff"][0])
        self.assertEqual(self.app.config["FIRESTORE_SYNC_SERVICE"].shop_ids[0][0], 1)

    def test_firestore_push_preserves_device_shop_and_acknowledges(self):
        response = self.app.test_client().post(
            "/api/sync/push",
            headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"},
            json={
                "device_id": "device-a",
                "items": [{
                    "outbox_id": 1,
                    "table_name": "sales",
                    "payload": {
                        "id": "sale-a",
                        "shop_id": 1,
                        "device_id": "device-a",
                        "staff_id": 1,
                        "customer_name": "Customer",
                        "items": [{"product_id": 1, "quantity": 1, "unit_price": "10.00"}],
                    },
                }],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["status"], "ok")
        pushed = self.app.config["FIRESTORE_SYNC_SERVICE"].pushed[0]
        self.assertEqual(pushed[1], 1)

    def test_firestore_push_rejects_cross_shop_sale(self):
        response = self.app.test_client().post(
            "/api/sync/push",
            headers={"X-Sync-Key": "sync-secret", "X-Device-ID": "device-a"},
            json={
                "device_id": "device-a",
                "items": [{
                    "outbox_id": 2,
                    "table_name": "sales",
                    "payload": {
                        "id": "sale-b",
                        "shop_id": 2,
                        "device_id": "device-a",
                        "items": [],
                    },
                }],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["status"], "error")

    def test_staff_cleaner_removes_sensitive_fields(self):
        clean = _clean_staff({
            "id": 1,
            "name": "Cashier",
            "password_hash": "hash",
            "quick_pin_hash": "pin",
            "quick_pin_failed_attempts": 2,
            "quick_pin_locked_until": "2026-01-01T00:00:00+00:00",
            "api_key": "secret",
            "role": "cashier",
        })
        self.assertEqual(clean, {"id": 1, "name": "Cashier", "role": "cashier"})

    def test_central_config_accepts_service_account_file_and_local_stays_sqlite(self):
        names = (
            "GLR_MODE",
            "CENTRAL_DATA_PROVIDER",
            "DATABASE_URL",
            "LOCAL_DATABASE_URL",
            "JWT_SECRET_KEY",
            "SYNC_API_KEY",
            "FIREBASE_SERVICE_ACCOUNT_FILE",
            "FIREBASE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS",
        )
        saved = {name: os.environ.get(name) for name in names}
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as account_file:
                os.environ.update({
                    "GLR_MODE": "central",
                    "CENTRAL_DATA_PROVIDER": "firestore",
                    "DATABASE_URL": "postgresql+psycopg2://configured:test@localhost:5432/test",
                    "JWT_SECRET_KEY": "configured-jwt",
                    "SYNC_API_KEY": "configured-sync",
                    "FIREBASE_SERVICE_ACCOUNT_FILE": account_file.name,
                })
                os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                self.assertEqual(get_config().CENTRAL_DATA_PROVIDER, "firestore")

            os.environ["GLR_MODE"] = "local"
            os.environ.pop("LOCAL_DATABASE_URL", None)
            local_config = get_config()
            self.assertEqual(local_config.GLR_MODE, "local")
            self.assertTrue(local_config.SQLALCHEMY_DATABASE_URI.startswith("sqlite://"))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_central_config_rejects_documented_sync_key_placeholder(self):
        saved = {name: os.environ.get(name) for name in ("GLR_MODE", "CENTRAL_DATA_PROVIDER", "JWT_SECRET_KEY", "SYNC_API_KEY", "FIREBASE_SERVICE_ACCOUNT_FILE")}
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as account_file:
                os.environ.update({
                    "GLR_MODE": "central",
                    "CENTRAL_DATA_PROVIDER": "firestore",
                    "JWT_SECRET_KEY": "configured-jwt",
                    "SYNC_API_KEY": "change-me",
                    "FIREBASE_SERVICE_ACCOUNT_FILE": account_file.name,
                })
                with self.assertRaisesRegex(RuntimeError, "SYNC_API_KEY"):
                    get_config()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
