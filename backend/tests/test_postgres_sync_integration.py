"""Real HTTP sync integration coverage against the dedicated test database only."""
import os
import shutil
import threading
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask
from sqlalchemy.engine import make_url
from werkzeug.serving import make_server

from app.extensions import db
from app.models import Device, Product, Sale, Shop, Staff, StockMovement, SyncOutboxItem, SyncState
from app.routes.sales import apply_sale
from app.routes.sync import sync_bp
from app.sync.device import get_current_device_id
from app.sync.outbox import enqueue_outbox
from app.sync.worker import LAST_PULL_KEY, pull_reference_data_once, push_pending_once


TEST_DATABASE_NAME = "glr_sync_integration_test"
SYNC_KEY = "postgres-integration-sync-key"


def _sqlite_url(path: Path) -> str:
    """Build a Windows-safe SQLite URL for a disk-backed restart test."""
    return f"sqlite:///{path.resolve()}"


@unittest.skipUnless(
    os.environ.get("GLR_POSTGRES_TEST_URL"),
    "GLR_POSTGRES_TEST_URL is required for PostgreSQL integration tests",
)
class PostgresSyncIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url = os.environ["GLR_POSTGRES_TEST_URL"]
        parsed_url = make_url(cls.database_url)
        if parsed_url.database != TEST_DATABASE_NAME:
            raise RuntimeError(
                "PostgreSQL integration tests refuse to run unless "
                f"GLR_POSTGRES_TEST_URL targets {TEST_DATABASE_NAME!r}."
            )

        cls.central_app = Flask("postgres_sync_integration_central")
        cls.central_app.config.update(
            SQLALCHEMY_DATABASE_URI=cls.database_url,
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
            SYNC_API_KEY=SYNC_KEY,
            SECRET_KEY="postgres-integration-secret",
            JWT_SECRET_KEY="postgres-integration-jwt-secret",
        )
        db.init_app(cls.central_app)
        cls.central_app.register_blueprint(sync_bp)
        with cls.central_app.app_context():
            # The explicit database-name guard above is the safety boundary
            # for this destructive test setup.
            db.drop_all()
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Integration Shop"),
                Staff(
                    id=1,
                    shop_id=1,
                    name="Integration Cashier",
                    email="cashier@integration.test",
                    password_hash="unused",
                    role="cashier",
                ),
                Product(
                    id=1,
                    sku="INTEGRATION-ITEM",
                    name="Integration Item",
                    unit_price=10,
                    cost_price=4,
                ),
                StockMovement(
                    id="integration-restock",
                    product_id=1,
                    shop_id=1,
                    quantity_delta=100,
                    reason="restock",
                ),
            ])
            db.session.commit()

        cls.server = make_server("127.0.0.1", 0, cls.central_app)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.central_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.workspace = Path.cwd() / ".glr-postgres-sync" / str(uuid.uuid4())
        cls.workspace.mkdir(parents=True, exist_ok=False)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server_thread.join(timeout=5)
            cls.server.server_close()
        if hasattr(cls, "central_app"):
            with cls.central_app.app_context():
                db.drop_all()
                db.session.remove()
                db.engine.dispose()
        if hasattr(cls, "workspace"):
            shutil.rmtree(cls.workspace, ignore_errors=True)

    def _make_local_app(self, name: str, central_url: str):
        database_path = self.workspace / f"{name}.sqlite"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        app = Flask(f"postgres_sync_integration_{name}")
        app.config.update(
            SQLALCHEMY_DATABASE_URI=_sqlite_url(database_path),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            SYNC_API_KEY=SYNC_KEY,
            CENTRAL_SYNC_URL=central_url,
            DEVICE_ID_FILE=self.workspace / f"{name}-device-id.txt",
        )
        db.init_app(app)
        with app.app_context():
            db.create_all()
        return app

    @staticmethod
    def _dispose(app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_offline_sale_round_trip_replay_cursor_restart_and_outage(self):
        device_a = self._make_local_app("device-a", self.central_url)
        sale_id = "c5815643-8a37-4c3d-a65b-785f2cb5f001"
        item_id = "0e2abcf9-882c-4cc9-94e5-458d1d351001"
        movement_id = "5b74c81d-973b-40b1-8fd7-65869222b001"
        sale_timestamp = "2026-01-02T05:04:05+02:00"

        try:
            with device_a.app_context():
                device_id = get_current_device_id()
            with self.central_app.app_context():
                db.session.add(Device(id=device_id, shop_id=1))
                db.session.commit()
            self.assertEqual(pull_reference_data_once(device_a)["products"], 1)
            device_a.config["CENTRAL_SYNC_URL"] = "http://127.0.0.1:1"
            with device_a.app_context():
                device_id = get_current_device_id()
                payload = {
                    "id": sale_id,
                    "shop_id": 1,
                    "device_id": device_id,
                    "staff_id": 1,
                    "customer_name": "Offline Customer",
                    "payment_method": "cash",
                    "created_at": sale_timestamp,
                    "items": [{
                        "id": item_id,
                        "product_id": 1,
                        "quantity": 2,
                        "unit_price": "10.00",
                        "stock_movement_id": movement_id,
                    }],
                }
                sale, _, created = apply_sale(payload)
                self.assertTrue(created)
                self.assertEqual(sale.id, sale_id)
                self.assertEqual(SyncOutboxItem.query.count(), 1)

            # This makes the real requests-based worker call the real central
            # HTTP endpoint; the sale was created before any central was reachable.
            device_a.config["CENTRAL_SYNC_URL"] = self.central_url
            pushed = push_pending_once(device_a)
            self.assertEqual(pushed, {"pushed": 1, "confirmed": 1, "failed": 0})

            with device_a.app_context():
                self.assertEqual(SyncOutboxItem.query.count(), 0)

            with self.central_app.app_context():
                self.assertEqual(Sale.query.filter_by(id=sale_id).count(), 1)
                central_sale = db.session.get(Sale, sale_id)
                self.assertIsNotNone(central_sale.invoice_number)

            # A second delivery of the exact UUID must get an acknowledgement
            # but leave central with one record.
            with device_a.app_context():
                enqueue_outbox("sales", sale_id, payload)
                self.assertEqual(SyncOutboxItem.query.count(), 1)
            replay = push_pending_once(device_a)
            self.assertEqual(replay, {"pushed": 1, "confirmed": 1, "failed": 0})
            with self.central_app.app_context():
                self.assertEqual(Sale.query.filter_by(id=sale_id).count(), 1)

            device_b = self._make_local_app("device-b", self.central_url)
            try:
                with device_b.app_context():
                    device_b_id = get_current_device_id()
                with self.central_app.app_context():
                    db.session.add(Device(id=device_b_id, shop_id=1))
                    db.session.commit()
                pulled = pull_reference_data_once(device_b)
                self.assertEqual(pulled["sales"], 1)
                with device_b.app_context():
                    pulled_sale = db.session.get(Sale, sale_id)
                    self.assertIsNotNone(pulled_sale)
                    # The source instant was 05:04:05+02:00. SQLite stores
                    # the normalized UTC wall time in its naive DateTime column.
                    self.assertEqual(pulled_sale.created_at.isoformat(), "2026-01-02T03:04:05")
                    cursor = db.session.get(SyncState, LAST_PULL_KEY).value

                boundary = datetime.fromisoformat(cursor)
                with self.central_app.app_context():
                    db.session.add(Product(
                        id=2,
                        sku="BOUNDARY-ITEM",
                        name="Boundary Item",
                        unit_price=1,
                        cost_price=1,
                        created_at=boundary,
                        updated_at=boundary,
                    ))
                    db.session.add(StockMovement(
                        id="BOUNDARY-MOVEMENT",
                        product_id=2,
                        shop_id=1,
                        quantity_delta=1,
                        reason="restock",
                        created_at=boundary,
                        updated_at=boundary,
                    ))
                    db.session.commit()

                pull_reference_data_once(device_b)
                with device_b.app_context():
                    self.assertIsNotNone(Product.query.filter_by(sku="BOUNDARY-ITEM").first())
            finally:
                self._dispose(device_b)

            # A rebuilt Flask app over the same SQLite sidecar sees both the
            # confirmed sale and persisted pull cursor without an outbox row.
            self._dispose(device_a)
            restarted_a = self._make_local_app("device-a", self.central_url)
            try:
                with restarted_a.app_context():
                    self.assertIsNotNone(db.session.get(Sale, sale_id))
                    self.assertEqual(SyncOutboxItem.query.count(), 0)

                    pending_payload = {
                        **payload,
                        "id": "4611639f-6a54-4ed1-8727-5cc1e2901001",
                        "created_at": "2026-01-03T03:04:05+00:00",
                        "items": [{
                            **payload["items"][0],
                            "id": "203f29a1-b9d3-4ad0-9e3c-8b5f94a31001",
                            "stock_movement_id": "5088f414-4795-4a2d-8f0c-8a8c6f231001",
                        }],
                    }
                    apply_sale(pending_payload)
                    self.assertEqual(SyncOutboxItem.query.count(), 1)

                restarted_a.config["CENTRAL_SYNC_URL"] = "http://127.0.0.1:1"
                outage = push_pending_once(restarted_a)
                self.assertEqual(outage, {"pushed": 0, "confirmed": 0, "failed": 1})
                with restarted_a.app_context():
                    pending_item = SyncOutboxItem.query.one()
                    self.assertEqual(pending_item.status, "pending")
                    self.assertEqual(pending_item.attempt_count, 1)
            finally:
                self._dispose(restarted_a)
        finally:
            # device_a may already have been disposed during the restart phase.
            if "restarted_a" not in locals():
                self._dispose(device_a)
