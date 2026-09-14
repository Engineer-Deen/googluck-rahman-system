from flask import Flask
from werkzeug.security import generate_password_hash

from app.config import get_config, server_host
from app.extensions import db
from app.models import AuditLogEntry, Product, Sale, SaleItem, SalePayment, Shop, Staff, StockMovement, SyncOutboxItem
from app.sync.worker import pull_reference_data_once, push_pending_once
from app.routes.auth import auth_bp
from app.routes.audit import audit_bp
from app.routes.products import products_bp
from app.routes.products import current_stock
from app.routes.sales import apply_sale, sales_bp
from app.routes.shop import shop_bp
from app.routes.stock import stock_bp
from app.routes.staff import staff_bp
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import os
import unittest


class CriticalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="local",
            JWT_SECRET_KEY="test-secret",
            DEVICE_ID_FILE=Path("device-id.txt"),
        )
        db.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(audit_bp)
        self.app.register_blueprint(products_bp)
        self.app.register_blueprint(sales_bp)
        self.app.register_blueprint(shop_bp)
        self.app.register_blueprint(stock_bp)
        self.app.register_blueprint(staff_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                Shop(id=1, name="Shop One"),
                Shop(id=2, name="Shop Two"),
                Staff(
                    id=1,
                    shop_id=1,
                    name="Cashier One",
                    email="one@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="cashier",
                ),
                Staff(
                    id=2,
                    shop_id=2,
                    name="Cashier Two",
                    email="two@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="cashier",
                ),
                Staff(
                    id=3,
                    shop_id=1,
                    name="Shop Admin",
                    email="admin@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="admin",
                ),
                Staff(
                    id=4,
                    shop_id=1,
                    name="Owner",
                    email="owner@critical.test",
                    password_hash=generate_password_hash("secret"),
                    role="owner",
                ),
                Product(id=1, sku="P-1", name="Shared Product", unit_price=10, cost_price=5),
                StockMovement(id="movement-one", product_id=1, shop_id=1, quantity_delta=3, reason="restock"),
                StockMovement(id="movement-two", product_id=1, shop_id=2, quantity_delta=7, reason="restock"),
                Sale(id="sale-two", shop_id=2, staff_id=2, customer_name="Other Shop", total_amount=10),
            ])
            db.session.commit()

    def test_firestore_sale_retries_invoice_collision_without_duplicate_rows(self):
        class Allocator:
            def __init__(self):
                self.calls = 0
                self.requests = []

            def allocate_invoice_number(self, payload):
                self.calls += 1
                self.requests.append(dict(payload))
                if self.calls == 1:
                    return "INV-2026-1010"
                return f"INV-2026-{payload['minimum_next']}"

        allocator = Allocator()
        self.app.config["CENTRAL_DATA_PROVIDER"] = "firestore"
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            db.session.add(Sale(id="existing-invoice", invoice_number="INV-2026-1010", total_amount=10))
            payload = {
                "id": "new-invoice",
                "shop_id": 1,
                "staff_id": 1,
                "customer_name": "Invoice Collision Customer",
                "payment_method": "cash",
                "amount_paid": "10.00",
                "payment_id": "new-payment",
                "assign_invoice": True,
                "items": [{
                    "id": "new-item",
                    "product_id": 1,
                    "quantity": 1,
                    "unit_price": "10.00",
                    "stock_movement_id": "new-movement",
                }],
            }

            sale, warnings, created = apply_sale(payload)

            self.assertTrue(created)
            self.assertEqual(warnings, [])
            self.assertEqual(sale.invoice_number, "INV-2026-1011")
            self.assertEqual(allocator.calls, 2)
            self.assertEqual(allocator.requests[1]["minimum_next"], 1011)
            self.assertEqual(Sale.query.filter_by(id="new-invoice").count(), 1)
            self.assertEqual(SaleItem.query.filter_by(sale_id="new-invoice").count(), 1)
            self.assertEqual(SalePayment.query.filter_by(sale_id="new-invoice").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(reference_id="new-invoice").count(), 1)

    def test_sales_route_retries_invoice_collision_before_commit(self):
        class Allocator:
            def __init__(self):
                self.calls = 0
                self.requests = []

            def allocate_invoice_number(self, payload):
                self.calls += 1
                self.requests.append(dict(payload))
                if self.calls == 1:
                    return "INV-2026-1010"
                return f"INV-2026-{payload['minimum_next']}"

        allocator = Allocator()
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore")
        client, headers = self._cashier_client()
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            db.session.add(Sale(id="route-existing-invoice", invoice_number="INV-2026-1010", total_amount=10))
            db.session.commit()

        payload = {
            "id": "route-collision-sale",
            "customer_name": "Route Collision Customer",
            "payment_method": "cash",
            "amount_paid": "10.00",
            "items": [{"product_id": 1, "quantity": 1, "unit_price": "10.00"}],
        }
        with patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            response = client.post("/api/sales", headers=headers, json=payload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["invoice_number"], "INV-2026-1011")
        self.assertEqual(allocator.calls, 2)
        self.assertEqual(allocator.requests[1]["minimum_next"], 1011)
        with self.app.app_context():
            self.assertEqual(Sale.query.filter_by(id="route-collision-sale").count(), 1)
            self.assertEqual(SaleItem.query.filter_by(sale_id="route-collision-sale").count(), 1)
            self.assertEqual(SalePayment.query.filter_by(sale_id="route-collision-sale").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(reference_id="route-collision-sale").count(), 1)

        with patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            replay = client.post("/api/sales", headers=headers, json=payload)

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["invoice_number"], "INV-2026-1011")
        self.assertEqual(allocator.calls, 2)
        with self.app.app_context():
            self.assertEqual(Sale.query.filter_by(id="route-collision-sale").count(), 1)
            self.assertEqual(SaleItem.query.filter_by(sale_id="route-collision-sale").count(), 1)
            self.assertEqual(SalePayment.query.filter_by(sale_id="route-collision-sale").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(reference_id="route-collision-sale").count(), 1)

    def test_sales_route_starts_after_sql_invoice_floor_when_firestore_sequence_is_behind(self):
        class Allocator:
            def __init__(self):
                self.requests = []

            def allocate_invoice_number(self, payload):
                self.requests.append(dict(payload))
                return f"INV-2026-{payload.get('minimum_next', 1001)}"

        allocator = Allocator()
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore")
        client, headers = self._cashier_client()
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            db.session.add_all([
                Sale(id="floor-1008", invoice_number="INV-2026-1008", total_amount=10),
                Sale(id="floor-1009", invoice_number="INV-2026-1009", total_amount=10),
                Sale(id="floor-1010", invoice_number="INV-2026-1010", total_amount=10),
                Sale(id="floor-1011", invoice_number="INV-2026-1011", total_amount=10),
                Product(id=7, sku="P-7", name="Floor Product", unit_price=15, cost_price=10),
                StockMovement(id="floor-stock", product_id=7, shop_id=1, quantity_delta=2, reason="restock"),
            ])
            db.session.commit()

        payload = {
            "id": "floor-sale",
            "customer_name": "Floor Customer",
            "payment_method": "cash",
            "amount_paid": "15.00",
            "items": [{"product_id": 7, "quantity": 1, "unit_price": "15.00"}],
        }
        with patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            response = client.post("/api/sales", headers=headers, json=payload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["invoice_number"], "INV-2026-1012")
        self.assertEqual(allocator.requests[0]["minimum_next"], 1012)
        self.assertEqual(response.get_json()["amount_paid"], "15.00")
        with self.app.app_context():
            self.assertEqual(Sale.query.filter_by(id="floor-sale").count(), 1)
            self.assertEqual(SaleItem.query.filter_by(sale_id="floor-sale").count(), 1)
            self.assertEqual(SalePayment.query.filter_by(sale_id="floor-sale").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(reference_id="floor-sale").count(), 1)
            self.assertEqual(current_stock(7, 1), 1)

        with patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            replay = client.post("/api/sales", headers=headers, json=payload)

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["invoice_number"], "INV-2026-1012")
        with self.app.app_context():
            self.assertEqual(Sale.query.filter_by(id="floor-sale").count(), 1)
            self.assertEqual(SaleItem.query.filter_by(sale_id="floor-sale").count(), 1)
            self.assertEqual(SalePayment.query.filter_by(sale_id="floor-sale").count(), 1)
            self.assertEqual(StockMovement.query.filter_by(reference_id="floor-sale").count(), 1)
            self.assertEqual(current_stock(7, 1), 1)

    def test_exhausted_invoice_allocation_rolls_back_pending_sale_rows(self):
        class ExhaustedAllocator:
            def allocate_invoice_number(self, payload):
                return "INV-2026-1010"

        self.app.config["CENTRAL_DATA_PROVIDER"] = "firestore"
        allocator = ExhaustedAllocator()
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=allocator):
            db.session.add_all([
                Sale(id="exhausted-existing-1010", invoice_number="INV-2026-1010", total_amount=10),
                Sale(id="exhausted-existing-1011", invoice_number="INV-2026-1011", total_amount=10),
            ])
            db.session.commit()
            payload = {
                "id": "exhausted-sale",
                "shop_id": 1,
                "staff_id": 1,
                "customer_name": "Exhausted Allocation Customer",
                "amount_paid": "10.00",
                "payment_id": "exhausted-payment",
                "assign_invoice": True,
                "items": [{
                    "id": "exhausted-item",
                    "product_id": 1,
                    "quantity": 1,
                    "unit_price": "10.00",
                    "stock_movement_id": "exhausted-movement",
                }],
            }

            with self.assertRaisesRegex(RuntimeError, "Could not allocate a unique invoice number"):
                apply_sale(payload)
            db.session.rollback()

            self.assertEqual(Sale.query.filter_by(id="exhausted-sale").count(), 0)
            self.assertEqual(SaleItem.query.filter_by(sale_id="exhausted-sale").count(), 0)
            self.assertEqual(SalePayment.query.filter_by(sale_id="exhausted-sale").count(), 0)
            self.assertEqual(StockMovement.query.filter_by(reference_id="exhausted-sale").count(), 0)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        device_path = self.app.config["DEVICE_ID_FILE"]
        if device_path.exists():
            device_path.unlink()

    def _cashier_client(self):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": "one@critical.test", "password": "secret", "role_group": "seller"},
        )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def _login(self, email, role_group):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": email, "password": "secret", "role_group": role_group},
        )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def test_cashier_isolated_from_other_shop(self):
        client, headers = self._cashier_client()

        products = client.get("/api/products", headers=headers)
        self.assertEqual(products.status_code, 200)
        self.assertEqual(products.get_json()[0]["stock"], 3)

        sales = client.get("/api/sales", headers=headers)
        self.assertEqual(sales.status_code, 200)
        self.assertEqual(sales.get_json(), [])
        self.assertEqual(client.get("/api/sales/sale-two", headers=headers).status_code, 404)

        history = client.get("/api/stock-movements/product/1", headers=headers)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([row["id"] for row in history.get_json()], ["movement-one"])
        payment = client.post(
            "/api/sales/sale-two/payments",
            headers=headers,
            json={"id": "payment-two", "amount": "1.00"},
        )
        self.assertEqual(payment.status_code, 404)

    def test_cashier_sale_cannot_override_shop(self):
        client, headers = self._cashier_client()
        with patch("app.sync.worker.trigger_sync_soon"):
            response = client.post(
                "/api/sales",
                headers=headers,
                json={
                    "id": "sale-one",
                    "shop_id": 2,
                    "device_id": "device-one",
                    "customer_name": "Local Customer",
                    "items": [{"product_id": 1, "quantity": 1, "unit_price": "10.00"}],
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["shop_id"], 1)
        self.assertEqual(response.get_json()["items"][0]["product_name"], "Shared Product")

    def test_cashier_cannot_read_or_write_admin_system_settings_or_audit_log(self):
        client, headers = self._cashier_client()

        self.assertEqual(client.get("/api/shop/settings", headers=headers).status_code, 403)
        self.assertEqual(
            client.put(
                "/api/shop/settings",
                headers=headers,
                json={"timeout_minutes": 30, "full_login_hours": 8},
            ).status_code,
            403,
        )
        self.assertEqual(client.get("/api/audit-log", headers=headers).status_code, 403)

    def test_staff_management_creation_validation_duplicates_and_audit(self):
        self.app.config["GLR_MODE"] = "central"
        admin, headers = self._login("admin@critical.test", "owner")

        invalid = admin.post("/api/staff", headers=headers, json={"name": "", "email": "bad"})
        self.assertEqual(invalid.status_code, 400)

        created = admin.post(
            "/api/staff",
            headers=headers,
            json={"name": "New Cashier", "email": "new.cashier@test", "password": "secret123", "role": "cashier"},
        )
        self.assertEqual(created.status_code, 201)
        staff_id = created.get_json()["id"]

        duplicate = admin.post(
            "/api/staff",
            headers=headers,
            json={"name": "Duplicate", "email": "NEW.CASHIER@TEST", "password": "secret123", "role": "cashier"},
        )
        self.assertEqual(duplicate.status_code, 409)

        updated = admin.put(
            f"/api/staff/{staff_id}",
            headers=headers,
            json={"name": "Inactive Cashier", "role": "manager", "is_active": False},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.get_json()["is_active"])
        self.assertEqual(updated.get_json()["role"], "manager")

        reset = admin.post(
            f"/api/staff/{staff_id}/reset-password",
            headers=headers,
            json={"new_password": "newsecret123"},
        )
        self.assertEqual(reset.status_code, 200)

        with self.app.app_context():
            actions = {entry.action for entry in AuditLogEntry.query.filter(AuditLogEntry.entity_id == str(staff_id)).all()}
            self.assertTrue({"staff_created", "staff_updated", "staff_password_reset"}.issubset(actions))

    def test_cashier_cannot_manage_staff(self):
        self.app.config["GLR_MODE"] = "central"
        cashier, headers = self._cashier_client()
        self.assertEqual(cashier.get("/api/staff", headers=headers).status_code, 403)
        self.assertEqual(
            cashier.post(
                "/api/staff",
                headers=headers,
                json={"name": "Unauthorized", "email": "unauthorized@test", "password": "secret123"},
            ).status_code,
            403,
        )

    def test_admin_settings_persist_and_write_audit_entry(self):
        self.app.config["GLR_MODE"] = "central"
        client, headers = self._login("admin@critical.test", "owner")

        invalid = client.put(
            "/api/shop/settings",
            headers=headers,
            json={"timeout_minutes": 7, "full_login_hours": 8},
        )
        self.assertEqual(invalid.status_code, 400)

        response = client.put(
            "/api/shop/settings",
            headers=headers,
            json={"timeout_minutes": 30, "full_login_hours": 12, "pin": "2468"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["timeout_minutes"], 30)
        self.assertEqual(response.get_json()["full_login_hours"], 12)
        self.assertTrue(response.get_json()["pin_configured"])

        client, headers = self._login("admin@critical.test", "owner")
        settings = client.get("/api/shop/settings", headers=headers)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.get_json()["timeout_minutes"], 30)
        self.assertEqual(settings.get_json()["full_login_hours"], 12)

        with self.app.app_context():
            audit = AuditLogEntry.query.filter_by(action="system_settings_updated").order_by(AuditLogEntry.id.desc()).first()
            self.assertIsNotNone(audit)
            self.assertEqual(audit.actor_staff_id, 3)

    def test_push_pending_once_ignores_conflicting_invoice_reassignment(self):
        self.app.config["CENTRAL_SYNC_URL"] = "http://central.test"
        self.app.config["SYNC_API_KEY"] = "test-sync-key"

        with self.app.app_context():
            db.session.add(Sale(
                id="existing-sale",
                shop_id=1,
                staff_id=1,
                customer_name="Existing Sale",
                invoice_number="INV-2026-1001",
                total_amount=10,
            ))
            db.session.add(Sale(
                id="pending-sale",
                shop_id=1,
                staff_id=1,
                customer_name="Pending Sale",
                total_amount=20,
            ))
            db.session.add(SyncOutboxItem(
                id=1,
                table_name="sales",
                record_id="pending-sale",
                status="pending",
                payload_json='{"id": "pending-sale", "shop_id": 1, "device_id": "device-one", "staff_id": 1, "customer_name": "Pending Sale", "items": [{"id": "item-1", "product_id": 1, "quantity": 1, "unit_price": "20.00", "stock_movement_id": "movement-1"}]}',
            ))
            db.session.commit()

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [{"outbox_id": 1, "status": "ok", "invoice_number": "INV-2026-1001"}]
                }

        with patch("app.sync.worker.get_current_device_id", return_value="device-one"), \
             patch("app.sync.worker.requests.post", return_value=_Response()):
            result = push_pending_once(self.app)

        self.assertEqual(result, {"pushed": 1, "confirmed": 1, "failed": 0})
        with self.app.app_context():
            pending_item = db.session.get(SyncOutboxItem, 1)
            self.assertIsNone(pending_item)
            self.assertIsNone(db.session.get(Sale, "pending-sale").invoice_number)

    def test_existing_tokens_revalidate_employee_state_and_logout(self):
        client, headers = self._cashier_client()

        with self.app.app_context():
            staff = db.session.get(Staff, 1)
            staff.is_active = False
            db.session.commit()
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

        with self.app.app_context():
            staff = db.session.get(Staff, 1)
            staff.is_active = True
            db.session.commit()
        client, headers = self._cashier_client()

        with self.app.app_context():
            db.session.get(Staff, 1).role = "manager"
            db.session.commit()
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

        with self.app.app_context():
            db.session.get(Staff, 1).role = "cashier"
            db.session.commit()
        client, headers = self._cashier_client()
        with self.app.app_context():
            db.session.get(Staff, 1).shop_id = 2
            db.session.commit()
        self.assertEqual(client.get("/api/shop", headers=headers).status_code, 401)

        with self.app.app_context():
            db.session.get(Staff, 1).shop_id = 1
            db.session.commit()
        client, headers = self._cashier_client()
        self.assertEqual(client.post("/api/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

    def test_sync_pull_does_not_invalidate_active_tokens_on_noop_staff_updates(self):
        self.app.config["CENTRAL_SYNC_URL"] = "http://central.test"
        self.app.config["SYNC_API_KEY"] = "test-sync-key"

        client, headers = self._login("admin@critical.test", "owner")
        with self.app.app_context():
            original_updated_at = db.session.get(Staff, 3).updated_at

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "shops": [],
                    "staff": [{
                        "id": 3,
                        "shop_id": 1,
                        "name": "Shop Admin",
                        "email": "admin@critical.test",
                        "role": "admin",
                        "is_active": True,
                    }],
                    "settings": [],
                    "products": [],
                    "sales": [],
                    "sale_items": [],
                    "payments": [],
                    "stock_movements": [],
                    "next_cursor": "2026-09-15T00:00:00+00:00",
                }

        with patch("app.sync.worker.get_current_device_id", return_value="device-one"), \
             patch("app.sync.worker.requests.get", return_value=_Response()):
            result = pull_reference_data_once(self.app)

        self.assertEqual(result["staff"], 1)
        with self.app.app_context():
            staff = db.session.get(Staff, 3)
            self.assertEqual(staff.updated_at, original_updated_at)

        response = client.get("/api/products", headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_non_owner_admin_cannot_assign_staff_to_another_shop(self):
        self.app.config["GLR_MODE"] = "central"
        admin, admin_headers = self._login("admin@critical.test", "owner")
        response = admin.post(
            "/api/staff",
            headers=admin_headers,
            json={
                "name": "Other Shop",
                "email": "other@critical.test",
                "password": "secret1",
                "role": "cashier",
                "shop_id": 2,
            },
        )
        self.assertEqual(response.status_code, 403)

        owner, owner_headers = self._login("owner@critical.test", "owner")
        response = owner.post(
            "/api/staff",
            headers=owner_headers,
            json={
                "name": "Other Shop",
                "email": "other@critical.test",
                "password": "secret1",
                "role": "cashier",
                "shop_id": 2,
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["shop_id"], 2)

    def test_local_host_and_central_defaults_are_safe(self):
        self.assertEqual(server_host("local"), "127.0.0.1")
        self.assertEqual(server_host("central"), "0.0.0.0")

        names = ("DATABASE_URL", "JWT_SECRET_KEY", "SYNC_API_KEY")
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            with patch.dict(os.environ, {"GLR_MODE": "central"}, clear=False):
                with self.assertRaises(RuntimeError):
                    get_config()
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def _seed_history_sales(self):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=12, minute=0, second=0, microsecond=0)
        rows = [
            ("hist-today", 1, 1, "Today Customer", "INV-HIST-TODAY", today_start, "10.00", "5.00"),
            ("hist-yesterday", 1, 1, "Yesterday Customer", "INV-HIST-YDAY", today_start - timedelta(days=1), "20.00", "8.00"),
            ("hist-week", 1, 1, "Week Customer", "INV-HIST-WEEK", today_start - timedelta(days=3), "30.00", "12.00"),
            ("hist-month", 1, 1, "Month Customer", "INV-HIST-MONTH", today_start - timedelta(days=20), "40.00", "15.00"),
            ("hist-year", 1, 1, "Year Customer", "INV-HIST-YEAR", today_start - timedelta(days=200), "50.00", "20.00"),
            ("hist-old", 1, 1, "Old Customer", "INV-HIST-OLD", today_start - timedelta(days=400), "60.00", "25.00"),
            ("hist-shop-two", 2, 2, "Other Shop Customer", "INV-HIST-SHOP2", today_start, "70.00", "30.00"),
        ]
        for sale_id, shop_id, staff_id, customer, invoice, created_at, unit_price, unit_cost in rows:
            db.session.add(Sale(
                id=sale_id,
                shop_id=shop_id,
                staff_id=staff_id,
                customer_name=customer,
                invoice_number=invoice,
                total_amount=unit_price,
                created_at=created_at,
                updated_at=created_at,
            ))
            db.session.add(SaleItem(
                id=f"{sale_id}-item",
                sale_id=sale_id,
                product_id=1,
                quantity=1,
                unit_price=unit_price,
                subtotal=unit_price,
                unit_cost=unit_cost,
            ))
            db.session.add(SalePayment(
                id=f"{sale_id}-pay",
                sale_id=sale_id,
                amount=unit_price,
                staff_id=staff_id,
                created_at=created_at,
                updated_at=created_at,
            ))
        db.session.commit()

    def test_sales_history_period_filters(self):
        with self.app.app_context():
            self._seed_history_sales()
        client, headers = self._login("admin@critical.test", "owner")

        def ids(period):
            response = client.get(f"/api/sales?period={period}&limit=100", headers=headers)
            self.assertEqual(response.status_code, 200)
            return {row["id"] for row in response.get_json()}

        self.assertIn("hist-today", ids("today"))
        self.assertNotIn("hist-yesterday", ids("today"))
        self.assertEqual(ids("yesterday"), {"hist-yesterday"})

        week_ids = ids("7days")
        self.assertTrue({"hist-today", "hist-yesterday", "hist-week"}.issubset(week_ids))
        self.assertNotIn("hist-month", week_ids)

        month_ids = ids("30days")
        self.assertTrue({"hist-today", "hist-yesterday", "hist-week", "hist-month"}.issubset(month_ids))
        self.assertNotIn("hist-year", month_ids)

        year_ids = ids("year")
        self.assertTrue({"hist-today", "hist-year", "hist-month"}.issubset(year_ids))
        self.assertNotIn("hist-old", year_ids)

        all_ids = ids("all")
        self.assertTrue({"hist-today", "hist-old", "hist-year"}.issubset(all_ids))
        self.assertNotIn("hist-shop-two", all_ids)

    def test_sales_history_shop_visibility_and_profit_restrictions(self):
        with self.app.app_context():
            self._seed_history_sales()

        cashier, cashier_headers = self._cashier_client()
        cashier_sales = cashier.get("/api/sales?period=all&limit=100", headers=cashier_headers)
        self.assertEqual(cashier_sales.status_code, 200)
        cashier_rows = cashier_sales.get_json()
        cashier_ids = {row["id"] for row in cashier_rows}
        self.assertIn("hist-today", cashier_ids)
        self.assertNotIn("hist-shop-two", cashier_ids)
        self.assertTrue(all("profit" not in row for row in cashier_rows))
        self.assertTrue(all("realized_profit" not in row for row in cashier_rows))
        self.assertTrue(all("unit_cost" not in item for row in cashier_rows for item in row["items"]))

        admin, admin_headers = self._login("admin@critical.test", "owner")
        admin_sales = admin.get("/api/sales?period=today&limit=100", headers=admin_headers)
        self.assertEqual(admin_sales.status_code, 200)
        admin_today = {row["id"]: row for row in admin_sales.get_json()}
        self.assertIn("hist-today", admin_today)
        self.assertNotIn("hist-shop-two", admin_today)
        self.assertEqual(admin_today["hist-today"]["profit"], "5.00")
        self.assertEqual(admin_today["hist-today"]["realized_profit"], "5.00")
        self.assertEqual(admin_today["hist-today"]["items"][0]["unit_cost"], "5.00")

        owner, owner_headers = self._login("owner@critical.test", "owner")
        owner_sales = owner.get("/api/sales?period=today&limit=100", headers=owner_headers)
        self.assertEqual(owner_sales.status_code, 200)
        owner_ids = {row["id"] for row in owner_sales.get_json()}
        self.assertIn("hist-today", owner_ids)
        self.assertIn("hist-shop-two", owner_ids)

        searched = admin.get("/api/sales?period=all&search=Week%20Customer", headers=admin_headers)
        self.assertEqual(searched.status_code, 200)
        self.assertEqual([row["id"] for row in searched.get_json()], ["hist-week"])

    def test_audit_log_authorization_and_formatting(self):
        self.app.config["GLR_MODE"] = "central"
        with self.app.app_context():
            db.session.add(AuditLogEntry(
                actor_staff_id=3,
                actor_name="Shop Admin",
                actor_role="admin",
                action="staff_created",
                entity_type="staff",
                entity_id="99",
                details_json='{"name": "Temp", "email": "temp@test", "role": "cashier"}',
                details="Created cashier account for Temp (temp@test).",
            ))
            db.session.commit()

        cashier, cashier_headers = self._cashier_client()
        self.assertEqual(cashier.get("/api/audit-log", headers=cashier_headers).status_code, 403)

        admin, admin_headers = self._login("admin@critical.test", "owner")
        response = admin.get("/api/audit-log", headers=admin_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload)
        entry = next(row for row in payload if row["entity_id"] == "99")
        self.assertEqual(entry["action"], "Created a staff account")
        self.assertEqual(entry["actor_name"], "Shop Admin")
        self.assertEqual(entry["actor_role"], "admin")
        self.assertEqual(entry["details"], "Created cashier account for Temp (temp@test).")
        self.assertEqual(entry["description"], entry["details"])
        self.assertEqual(entry["reason"], "—")
        self.assertIn("created_at", entry)

