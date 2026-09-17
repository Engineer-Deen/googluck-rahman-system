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
from unittest.mock import Mock, patch
import os
import unittest
from tests.test_firestore_provider import FakeFirestoreClient
from app.firestore.service import FirestoreSyncService


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
        self.firestore_client = FakeFirestoreClient()
        self.firestore_service = FirestoreSyncService(self.firestore_client)
        self.firestore_client.collections["shops"].update({
            "1": {"id": 1, "name": "Shop One"},
            "2": {"id": 2, "name": "Shop Two"},
        })
        for staff in (
            (1, 1, "Cashier One", "one@critical.test", "cashier"),
            (2, 2, "Cashier Two", "two@critical.test", "cashier"),
            (3, 1, "Shop Admin", "admin@critical.test", "admin"),
            (4, 1, "Owner", "owner@critical.test", "owner"),
        ):
            staff_id, shop_id, name, email, role = staff
            self.firestore_client.collections["staff"][str(staff_id)] = {
                "id": staff_id, "shop_id": shop_id, "name": name, "email": email,
                "role": role, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "password_hash": generate_password_hash("secret"),
            }
        self.firestore_client.collections["products"]["1"] = {
            "id": 1, "sku": "P-1", "name": "Shared Product", "unit_price": "10.00",
            "cost_price": "5.00", "is_active": True, "shop_ids": [1, 2],
        }
        self.firestore_client.collections["stock_movements"].update({
            "movement-one": {"id": "movement-one", "product_id": 1, "shop_id": 1, "quantity_delta": 3, "reason": "restock"},
            "movement-two": {"id": "movement-two", "product_id": 1, "shop_id": 2, "quantity_delta": 7, "reason": "restock"},
        })
        self.firestore_client.collections["sales"]["sale-two"] = {
            "id": "sale-two", "shop_id": 2, "staff_id": 2, "customer_name": "Other Shop",
            "total_amount": "10.00", "created_at": datetime.now(timezone.utc),
        }
        self.app.config["FIRESTORE_SYNC_SERVICE"] = self.firestore_service
        self.central_session_patch = patch(
            "app.auth.requests.get", side_effect=self._central_session_response
        )
        self.central_identity_overrides = {}
        self.revoked_tokens = set()
        self.central_session_patch.start()

    def tearDown(self):
        self.central_session_patch.stop()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        device_path = self.app.config["DEVICE_ID_FILE"]
        if device_path.exists():
            device_path.unlink()

    def _central_session_response(self, _url, headers=None, **_kwargs):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if token in self.revoked_tokens:
            return Mock(status_code=401)
        staff_id = int(token.removeprefix("central-"))
        with self.app.app_context():
            staff = db.session.get(Staff, staff_id)
            identity = {
                "id": staff.id, "name": staff.name, "email": staff.email,
                "role": staff.role, "shop_id": staff.shop_id, "is_active": True,
            }
            identity.update(self.central_identity_overrides.get(staff_id, {}))
        response = Mock(status_code=200)
        response.json.return_value = identity
        return response

    def _cashier_client(self):
        client = self.app.test_client()
        central_response = Mock(status_code=200)
        central_response.json.return_value = {"token": "central-1", "staff": {
            "id": 1, "name": "Cashier One", "email": "one@critical.test",
            "role": "cashier", "shop_id": 1, "is_active": True,
        }}
        with patch("app.routes.auth.requests.post", return_value=central_response):
            response = client.post(
                "/api/auth/login",
                json={"email": "one@critical.test", "password": "secret", "role_group": "seller"},
            )
        self.assertEqual(response.status_code, 200)
        return client, {"Authorization": "Bearer " + response.get_json()["token"]}

    def _login(self, email, role_group):
        client = self.app.test_client()
        with self.app.app_context():
            staff = Staff.query.filter_by(email=email).first()
            identity = {
                "id": staff.id, "name": staff.name, "email": staff.email,
                "role": staff.role, "shop_id": staff.shop_id, "is_active": True,
            }
        central_response = Mock(status_code=200)
        central_response.json.return_value = {"token": f"central-{staff.id}", "staff": identity}
        with patch("app.routes.auth.requests.post", return_value=central_response):
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
            actions = {
                entry.get("action")
                for entry in self.firestore_client.collections["audit_log"].values()
                if entry.get("entity_id") == str(staff_id)
            }
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

        audit_rows = [row for row in self.firestore_client.collections["audit_log"].values() if row.get("action") == "system_settings_updated"]
        self.assertTrue(audit_rows)
        self.assertEqual(audit_rows[-1]["actor_staff_id"], 3)

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
        self.assertEqual(headers["Authorization"], "Bearer central-1")
        with patch("app.auth.requests.get", return_value=Mock(status_code=401)):
            self.assertEqual(client.get("/api/products", headers=headers).status_code, 401)

        client, headers = self._cashier_client()

        with patch("app.auth.requests.get", return_value=Mock(status_code=200)) as session_get:
            session_get.return_value.json.return_value = {
                "id": 1, "name": "Cashier One", "email": "one@critical.test",
                "role": "manager", "shop_id": 1, "is_active": True,
            }
            self.assertEqual(client.get("/api/products", headers=headers).status_code, 200)

        client, headers = self._cashier_client()
        with patch("app.auth.requests.get", return_value=Mock(status_code=200)) as session_get:
            session_get.return_value.json.return_value = {
                "id": 1, "name": "Cashier One", "email": "one@critical.test",
                "role": "cashier", "shop_id": 2, "is_active": True,
            }
            self.assertEqual(client.get("/api/shop", headers=headers).status_code, 200)

        client, headers = self._cashier_client()
        with patch("app.routes.auth.requests.post", return_value=Mock(status_code=200)):
            logout = client.post("/api/auth/logout", headers=headers)
        self.assertEqual(logout.status_code, 200)
        with patch("app.auth.requests.get", return_value=Mock(status_code=401)):
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
        self.firestore_service.write_audit(
            "audit-99", actor_staff_id=3, actor_name="Shop Admin", actor_role="admin",
            action="staff_created", entity_type="staff", entity_id="99",
            details={"name": "Temp", "email": "temp@test", "role": "cashier"},
        )

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

