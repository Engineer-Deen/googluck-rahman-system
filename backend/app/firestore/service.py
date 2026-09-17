"""Firestore-backed central synchronization adapter.

This module is intentionally isolated from Flask routes and SQLAlchemy models.
The local device still writes SQLite and the existing outbox; Flask remains the
security boundary and chooses this adapter only when CENTRAL_DATA_PROVIDER is
set to ``firestore``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from flask import current_app
from google.cloud.firestore_v1.transaction import transactional
_SENSITIVE_STAFF_FIELDS = {
    "password_hash",
    "quick_pin_hash",
    "quick_pin_failed_attempts",
    "quick_pin_locked_until",
    "password",
    "token",
    "secret",
    "api_key",
}
_service_lock = threading.Lock()
_service_cache: dict[tuple[str, str], "FirestoreSyncService"] = {}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _clean_staff(raw: dict) -> dict:
    return {
        key: value
        for key, value in raw.items()
        if key.lower() not in _SENSITIVE_STAFF_FIELDS
    }


def _transaction_get(transaction, reference):
    result = transaction.get(reference)
    if hasattr(result, "to_dict"):
        return result
    return next(iter(result), None)


class FirestoreSyncService:
    """Translate existing sync payloads to stable Firestore documents."""

    def __init__(self, client):
        self.client = client
        self._invoice_sequence_ready: set[int] = set()

    @staticmethod
    def _parse_datetime(value):
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
            return datetime.fromisoformat(normalized)
        return value


    def allocate_invoice_number(self, sale_payload: dict) -> str:
        """Allocate the next invoice number from Firestore's migrated sequence."""
        return self._allocate_invoice(sale_payload)

    @classmethod
    def from_config(cls, validate=True):
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except ImportError as exc:
            raise RuntimeError("firebase-admin is required for Firestore mode") from exc

        project_id = current_app.config.get("FIREBASE_PROJECT_ID", "")
        account_file = current_app.config.get(
            "FIREBASE_SERVICE_ACCOUNT_FILE",
            "/etc/secrets/firebase-service-account.json",
        )
        account_json = current_app.config.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        options = {"projectId": project_id} if project_id else None
        with _service_lock:
            try:
                app = firebase_admin.get_app()
            except ValueError:
                if os.path.isfile(account_file):
                    credential = credentials.Certificate(account_file)
                elif account_json:
                    try:
                        info = json.loads(account_json)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON must be valid JSON") from exc
                    credential = credentials.Certificate(info)
                else:
                    credential = credentials.ApplicationDefault()
                app = firebase_admin.initialize_app(credential, options=options)

            database = current_app.config.get("FIRESTORE_DATABASE", "(default)")
            cache_key = (getattr(app, "project_id", ""), database)
            service = _service_cache.get(cache_key)
            if service is None:
                service = cls(firestore.client(app=app, database_id=database))
                if validate:
                    try:
                        service.validate_connection()
                    except Exception as exc:
                        raise RuntimeError("Firestore service account could not be validated") from exc
                _service_cache[cache_key] = service
            return service

    def validate_connection(self):
        """Run a lightweight Firestore query to verify the configured service account works."""
        try:
            list(self._collection("_glr_probe").limit(1).stream())
            return True
        except Exception as exc:
            raise RuntimeError("Firestore connection validation failed") from exc

    def _collection(self, name):
        return self.client.collection(name)

    @staticmethod
    def _device_value(device, key):
        return device.get(key) if isinstance(device, dict) else getattr(device, key)

    def get_staff_by_email(self, email: str) -> dict | None:
        """Return one central staff credential document by normalized email."""
        snapshots = self._collection("staff").where("email", "==", email).limit(1).stream()
        snapshot = next(iter(snapshots), None)
        if snapshot is None or not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    def get_staff(self, staff_id) -> dict | None:
        """Return one central staff document, including private credential fields."""
        snapshot = self._collection("staff").document(str(staff_id)).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    def update_staff_auth_state(self, staff_id, **fields):
        """Update only authentication state on a central staff document."""
        allowed = {
            "quick_pin_failed_attempts",
            "quick_pin_locked_until",
            "password_hash",
            "quick_pin_hash",
            "updated_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported staff auth fields: {sorted(unknown)}")
        self._collection("staff").document(str(staff_id)).set(fields, merge=True)

    def list_staff(self) -> list[dict]:
        staff = [snapshot.to_dict() or {} for snapshot in self._collection("staff").stream()]
        return sorted(staff, key=lambda row: str(row.get("name", "")).lower())

    def allocate_staff_id(self) -> int:
        sequence_ref = self._collection("sync_metadata").document("staff_sequence")
        transaction = self.client.transaction()

        @transactional
        def allocate(transaction):
            snapshot = _transaction_get(transaction, sequence_ref)
            current = (snapshot.to_dict() or {}).get("next_id", 1) if snapshot and snapshot.exists else 1
            transaction.set(sequence_ref, {"next_id": int(current) + 1, "updated_at": _utcnow()}, merge=True)
            return int(current)

        return allocate(transaction)

    def save_staff(self, staff_id, **fields) -> dict:
        data = {"id": int(staff_id), "updated_at": _utcnow(), **fields}
        self._collection("staff").document(str(staff_id)).set(data, merge=True)
        return self.get_staff(staff_id) or data

    def staff_email_exists(self, email: str, excluding_id=None) -> bool:
        staff = self.get_staff_by_email(email)
        return bool(staff and str(staff.get("id")) != str(excluding_id))

    def get_device(self, device_id: str) -> dict | None:
        snapshot = self._collection("devices").document(str(device_id)).get()
        return snapshot.to_dict() if snapshot.exists else None

    def save_device(self, device_id: str, **fields) -> dict:
        fields = {"id": str(device_id), **fields}
        self._collection("devices").document(str(device_id)).set(fields, merge=True)
        return self.get_device(device_id) or fields

    def get_shop(self, shop_id) -> dict | None:
        snapshot = self._collection("shops").document(str(shop_id)).get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_first_shop(self) -> dict | None:
        snapshot = next(iter(self._collection("shops").limit(1).stream()), None)
        return snapshot.to_dict() if snapshot and snapshot.exists else None

    def save_shop(self, shop_id, **fields) -> dict:
        fields = {"id": int(shop_id), **fields, "updated_at": _utcnow()}
        self._collection("shops").document(str(shop_id)).set(fields, merge=True)
        return self.get_shop(shop_id) or fields

    def get_setting(self, key: str, default=None):
        snapshot = self._collection("system_settings").document(key).get()
        if not snapshot.exists:
            snapshots = self._collection("system_settings").where("key", "==", key).limit(1).stream()
            snapshot = next(iter(snapshots), None)
        if not snapshot or not snapshot.exists:
            return default
        return (snapshot.to_dict() or {}).get("value", default)

    def save_setting(self, key: str, value) -> dict:
        data = {"id": key, "key": key, "value": value, "updated_at": _utcnow()}
        self._collection("system_settings").document(key).set(data, merge=True)
        return data

    def write_audit(self, audit_id: str, **fields):
        self._collection("audit_log").document(str(audit_id)).set(
            {"id": str(audit_id), "created_at": _utcnow(), **fields}, merge=True
        )

    def list_audit(self, limit=200, action=None, entity_type=None, since=None, until=None):
        """Return bounded audit results while preserving legacy document compatibility.

        Audit filters remain in Python because existing documents and optional
        filters do not share one guaranteed Firestore index/query shape. The
        result is always bounded to 200 entries after filtering; indexed query
        optimization belongs to a future schema/index phase.
        """
        entries = []
        for snapshot in self._collection("audit_log").stream():
            entry = snapshot.to_dict() or {}
            if action and entry.get("action") != action:
                continue
            if entity_type and entry.get("entity_type") != entity_type:
                continue
            created_at = entry.get("created_at")
            comparable_created_at = self._parse_datetime(created_at) if created_at else None
            if comparable_created_at and comparable_created_at.tzinfo is None:
                comparable_created_at = comparable_created_at.replace(tzinfo=timezone.utc)
            if since and comparable_created_at and comparable_created_at < since:
                continue
            if until and comparable_created_at and comparable_created_at > until:
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: _iso(entry.get("created_at")) or "", reverse=True)
        return entries[:max(1, min(int(limit), 200))]

    def list_products(self, include_inactive=False) -> list[dict]:
        products = [snapshot.to_dict() or {} for snapshot in self._collection("products").stream()]
        if not include_inactive:
            products = [product for product in products if product.get("is_active", True)]
        return sorted(products, key=lambda product: str(product.get("name", "")).lower())

    def get_product(self, product_id) -> dict | None:
        snapshot = self._collection("products").document(str(product_id)).get()
        return snapshot.to_dict() if snapshot.exists else None

    def _allocate_product_id(self) -> int:
        sequence_ref = self._collection("sync_metadata").document("product_sequence")
        transaction = self.client.transaction()

        @transactional
        def allocate(transaction):
            snapshot = _transaction_get(transaction, sequence_ref)
            current = (snapshot.to_dict() or {}).get("next_id", 1) if snapshot and snapshot.exists else 1
            transaction.set(sequence_ref, {"next_id": int(current) + 1, "updated_at": _utcnow()}, merge=True)
            return int(current)

        return allocate(transaction)

    def save_product(self, product_id, **fields) -> dict:
        data = {"id": int(product_id), "updated_at": _utcnow(), **fields}
        self._collection("products").document(str(product_id)).set(data, merge=True)
        return self.get_product(product_id) or data

    def stock_map(self, product_ids=None, shop_id=None) -> dict[int, int]:
        wanted = {int(product_id) for product_id in product_ids} if product_ids else None
        totals: dict[int, int] = {}
        for snapshot in self._collection("stock_movements").stream():
            movement = snapshot.to_dict() or {}
            product_id = movement.get("product_id")
            if product_id is None or (wanted is not None and int(product_id) not in wanted):
                continue
            if shop_id is not None and movement.get("shop_id") != shop_id:
                continue
            product_id = int(product_id)
            totals[product_id] = totals.get(product_id, 0) + int(movement.get("quantity_delta", 0) or 0)
        return totals

    def get_stock_movement(self, movement_id: str) -> dict | None:
        snapshot = self._collection("stock_movements").document(str(movement_id)).get()
        return snapshot.to_dict() if snapshot.exists else None

    def create_stock_movement(self, payload: dict) -> tuple[dict, bool]:
        movement_id = str(payload["id"])
        movement_ref = self._collection("stock_movements").document(movement_id)
        product_ref = self._collection("products").document(str(payload["product_id"]))
        transaction = self.client.transaction()

        @transactional
        def create(transaction):
            existing = _transaction_get(transaction, movement_ref)
            if existing and existing.exists:
                return existing.to_dict() or {}, False
            product = _transaction_get(transaction, product_ref)
            if not product or not product.exists:
                raise ValueError(f"Unknown product_id {payload['product_id']}")
            movement = {
                **payload,
                "id": movement_id,
                "quantity_delta": int(payload["quantity_delta"]),
                "created_at": _utcnow(),
                "updated_at": _utcnow(),
                "server_received_at": _utcnow(),
            }
            transaction.set(movement_ref, movement, merge=True)
            return movement, True

        return create(transaction)

    def list_stock_movements(self, product_id: int, shop_id=None, limit=100) -> list[dict]:
        movements = []
        for snapshot in self._collection("stock_movements").stream():
            movement = snapshot.to_dict() or {}
            if int(movement.get("product_id", -1)) != int(product_id):
                continue
            if shop_id is not None and movement.get("shop_id") != shop_id:
                continue
            movements.append(movement)
        movements.sort(key=lambda movement: _iso(movement.get("created_at")) or "", reverse=True)
        return movements[:limit]

    def get_sale_graph(self, sale_id: str) -> dict | None:
        sale_snapshot = self._collection("sales").document(str(sale_id)).get()
        if not sale_snapshot.exists:
            return None
        sale = sale_snapshot.to_dict() or {}
        items = []
        for snapshot in self._collection("sale_items").stream():
            item = snapshot.to_dict() or {}
            if str(item.get("sale_id")) == str(sale_id):
                items.append(item)
        payments = []
        for snapshot in self._collection("sale_payments").stream():
            payment = snapshot.to_dict() or {}
            if str(payment.get("sale_id")) == str(sale_id):
                payments.append(payment)
        return {"sale": sale, "items": items, "payments": payments}

    def list_sale_graphs(self, shop_id=None, limit=100) -> list[dict]:
        sales = []
        for snapshot in self._collection("sales").stream():
            sale = snapshot.to_dict() or {}
            if shop_id is not None and sale.get("shop_id") != shop_id:
                continue
            sales.append(sale)
        sales.sort(key=lambda sale: _iso(sale.get("created_at")) or "", reverse=True)
        result = []
        for sale in sales[:limit]:
            graph = self.get_sale_graph(sale.get("id"))
            if graph:
                result.append(graph)
        return result

    def create_sale(self, payload: dict, device=None) -> tuple[dict, bool]:
        sale_id = payload.get("id")
        if not sale_id:
            raise ValueError("payload.id is required")
        existing = self.get_sale_graph(sale_id)
        if existing:
            return existing, False
        customer_name = re.sub(r"\s+", " ", (payload.get("customer_name") or "").strip())
        customer_name = " ".join(word[:1].upper() + word[1:].lower() for word in customer_name.split(" ") if word)
        if not customer_name:
            raise ValueError("Customer name is required")
        items = payload.get("items") or []
        if not items:
            raise ValueError("A sale needs at least one item")
        products = {}
        requested = {}
        total = Decimal("0.00")
        normalized_items = []
        for item in items:
            product_id = int(item.get("product_id"))
            product = self.get_product(product_id)
            if not product:
                raise ValueError(f"Unknown product_id(s): [{product_id}]")
            try:
                quantity = int(item["quantity"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Item quantity must be a whole number")
            if quantity <= 0:
                raise ValueError("Item quantity must be greater than zero")
            raw_unit_price = item.get("unit_price")
            if raw_unit_price is None:
                raw_unit_price = product.get("unit_price", 0)
            unit_price = Decimal(str(raw_unit_price))
            if unit_price <= 0:
                raise ValueError("Item selling price must be greater than zero")
            subtotal = (unit_price * quantity).quantize(Decimal("0.01"))
            total += subtotal
            requested[product_id] = requested.get(product_id, 0) + quantity
            products[product_id] = product
            normalized_items.append({
                "id": item.get("id") or f"{sale_id}:{product_id}",
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": str(unit_price),
                "subtotal": str(subtotal),
                "unit_cost": str(product.get("cost_price", 0)),
                "stock_movement_id": item.get("stock_movement_id"),
            })
        shop_id = payload.get("shop_id")
        if payload.get("validate_stock", False):
            available = self.stock_map(requested, shop_id)
            for product_id, quantity in requested.items():
                if quantity > available.get(product_id, 0):
                    raise ValueError(f"Not enough stock for {products[product_id].get('name', product_id)}: only {available.get(product_id, 0)} available, {quantity} requested")
        initial_paid = Decimal(str(payload.get("amount_paid", 0) or 0))
        if initial_paid > total:
            raise ValueError("Amount paid cannot exceed the sale total")
        sale_payload = {
            "id": sale_id,
            "shop_id": shop_id,
            "device_id": payload.get("device_id"),
            "staff_id": payload.get("staff_id"),
            "customer_name": customer_name,
            "payment_method": payload.get("payment_method", "cash"),
            "total_amount": str(total.quantize(Decimal("0.01"))),
            "created_at": payload.get("created_at") or _utcnow(),
            "items": normalized_items,
            "amount_paid": str(initial_paid),
        }
        result = self.push_item(device or {"id": payload.get("device_id"), "shop_id": shop_id}, "sales", sale_payload)
        graph = self.get_sale_graph(sale_id)
        graph["sale"]["invoice_number"] = result.get("invoice_number")
        return graph, True

    def create_payment(self, payload: dict) -> tuple[dict, dict, bool]:
        payment_id = payload.get("id")
        if not payment_id:
            raise ValueError("payload.id is required")
        sale_id = payload.get("sale_id")
        sale_ref = self._collection("sales").document(str(sale_id))
        payment_ref = self._collection("sale_payments").document(str(payment_id))
        payment_snapshots = list(self._collection("sale_payments").stream())
        transaction = self.client.transaction()

        @transactional
        def create(transaction):
            sale_snapshot = _transaction_get(transaction, sale_ref)
            if not sale_snapshot or not sale_snapshot.exists:
                raise ValueError("Unknown sale_id")
            existing = _transaction_get(transaction, payment_ref)
            if existing and existing.exists:
                return sale_snapshot.to_dict() or {}, existing.to_dict() or {}, False
            try:
                amount = Decimal(str(payload.get("amount", 0)))
            except Exception:
                raise ValueError("Payment amount must be a number")
            if amount <= 0:
                raise ValueError("Payment amount must be greater than zero")
            paid = Decimal("0.00")
            for snapshot in payment_snapshots:
                payment = snapshot.to_dict() or {}
                if str(payment.get("sale_id")) == str(sale_id):
                    paid += Decimal(str(payment.get("amount", 0)))
            total = Decimal(str((sale_snapshot.to_dict() or {}).get("total_amount", 0)))
            balance = total - paid
            if amount > balance:
                raise ValueError(f"Payment of {amount} exceeds the outstanding balance of {balance}")
            payment = {
                "id": str(payment_id), "sale_id": str(sale_id), "amount": str(amount),
                "device_id": payload.get("device_id"), "staff_id": payload.get("staff_id"),
                "created_at": _utcnow(), "updated_at": _utcnow(), "server_received_at": _utcnow(),
            }
            transaction.set(payment_ref, payment, merge=True)
            return sale_snapshot.to_dict() or {}, payment, True

        sale, payment, created = create(transaction)
        graph = self.get_sale_graph(sale_id) or {"sale": sale, "items": [], "payments": []}
        return graph, payment, created

    def correct_sale(self, sale_id: str, data: dict, staff_id) -> dict:
        graph = self.get_sale_graph(sale_id)
        if not graph:
            raise ValueError("Sale not found")
        sale = graph["sale"]
        if sale.get("voided_at"):
            raise ValueError("A voided sale cannot be edited")
        old_items = graph.get("items", [])
        old_by_product = {}
        for item in old_items:
            product_id = int(item["product_id"])
            old_by_product[product_id] = old_by_product.get(product_id, 0) + int(item["quantity"])
        incoming = data.get("items")
        normalized = []
        new_by_product = {}
        new_total = Decimal("0.00")
        if incoming is not None:
            if not incoming:
                raise ValueError("A sale must contain at least one item")
            for item in incoming:
                product_id = int(item["product_id"])
                product = self.get_product(product_id)
                if not product:
                    raise ValueError("One or more products do not exist")
                quantity = int(item["quantity"])
                if quantity <= 0:
                    raise ValueError("Item quantity must be greater than zero")
                raw_unit_price = item.get("unit_price")
                if raw_unit_price is None:
                    raw_unit_price = product.get("unit_price", 0)
                price = Decimal(str(raw_unit_price))
                if price <= 0:
                    raise ValueError("Item selling price must be greater than zero")
                subtotal = (price * quantity).quantize(Decimal("0.01"))
                new_total += subtotal
                new_by_product[product_id] = new_by_product.get(product_id, 0) + quantity
                normalized.append({"id": item.get("id") or f"{sale_id}:correction:{product_id}", "sale_id": sale_id, "product_id": product_id, "quantity": quantity, "unit_price": str(price), "subtotal": str(subtotal), "unit_cost": str(product.get("cost_price", 0))})
            paid = sum((Decimal(str(payment.get("amount", 0))) for payment in graph.get("payments", [])), Decimal("0.00"))
            if paid > new_total:
                raise ValueError(f"Existing payments ({paid}) exceed the corrected sale total ({new_total})")
            for product_id in set(old_by_product) | set(new_by_product):
                delta = old_by_product.get(product_id, 0) - new_by_product.get(product_id, 0)
                if delta < 0 and self.stock_map([product_id], sale.get("shop_id")).get(product_id, 0) < -delta:
                    product = self.get_product(product_id) or {}
                    raise ValueError(f"Not enough stock for {product.get('name', product_id)}")
        sale_updates = {key: data[key] for key in ("customer_name", "payment_method") if key in data}
        if "items" in data:
            sale_updates["total_amount"] = str(new_total)
        sale_updates["updated_at"] = _utcnow()
        sale_ref = self._collection("sales").document(str(sale_id))
        transaction = self.client.transaction()

        @transactional
        def update(transaction):
            current = _transaction_get(transaction, sale_ref)
            if not current or not current.exists:
                raise ValueError("Sale not found")
            for item in old_items:
                transaction.delete(self._collection("sale_items").document(str(item["id"])))
            for item in normalized:
                transaction.set(self._collection("sale_items").document(str(item["id"])), item, merge=True)
            transaction.set(sale_ref, sale_updates, merge=True)

        update(transaction)
        if "items" in data:
            for product_id in set(old_by_product) | set(new_by_product):
                delta = old_by_product.get(product_id, 0) - new_by_product.get(product_id, 0)
                if delta:
                    movement_id = f"{sale_id}:correction:{product_id}:{uuid.uuid4().hex}"
                    self._write("stock_movements", movement_id, {"id": movement_id, "product_id": product_id, "shop_id": sale.get("shop_id"), "quantity_delta": delta, "reason": "sale_correction", "reference_id": sale_id, "created_at": _utcnow(), "updated_at": _utcnow()})
        return self.get_sale_graph(sale_id)

    def void_sale(self, sale_id: str, reason: str, staff_id: int, device_id=None, reversal_ids=None) -> tuple[dict, bool]:
        graph = self.get_sale_graph(sale_id)
        if not graph:
            raise ValueError("Sale not found")
        sale = graph["sale"]
        if sale.get("voided_at"):
            return graph, False
        reversal_ids = reversal_ids or {}
        sale_ref = self._collection("sales").document(str(sale_id))
        transaction = self.client.transaction()

        @transactional
        def void(transaction):
            current = _transaction_get(transaction, sale_ref)
            if not current or not current.exists:
                raise ValueError("Sale not found")
            current_sale = current.to_dict() or {}
            if current_sale.get("voided_at"):
                return
            for item in graph.get("items", []):
                movement_id = reversal_ids.get(item["id"]) or f"{sale_id}:void:{item['id']}"
                transaction.set(self._collection("stock_movements").document(movement_id), {"id": movement_id, "product_id": item["product_id"], "shop_id": sale.get("shop_id"), "device_id": device_id, "quantity_delta": int(item["quantity"]), "reason": "void_reversal", "reference_id": sale_id, "created_at": _utcnow(), "updated_at": _utcnow()}, merge=True)
            transaction.set(sale_ref, {"voided_at": _utcnow(), "voided_by_staff_id": staff_id, "void_reason": reason, "updated_at": _utcnow()}, merge=True)

        void(transaction)
        return self.get_sale_graph(sale_id), True

    def _write(self, collection: str, record_id: str, data: dict):
        self._collection(collection).document(str(record_id)).set(data, merge=True)

    def _mark_product_shop(self, product_id: int, shop_id: int):
        product_ref = self._collection("products").document(str(product_id))
        snapshot = product_ref.get()
        current = (snapshot.to_dict() or {}).get("shop_ids", []) if snapshot.exists else []
        if shop_id not in current:
            product_ref.set({"id": product_id, "shop_ids": [*current, shop_id], "updated_at": _utcnow()}, merge=True)

    def _allocate_invoice(self, sale_payload: dict) -> str:
        created_at = sale_payload.get("created_at")
        if created_at:
            year = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).year
        else:
            year = _utcnow().year
        sequence_ref = self.client.collection("sync_metadata").document(f"invoice_sequence_{year}")
        migrated_max = 1000
        if year not in self._invoice_sequence_ready:
            for document in self._collection("sales").stream():
                invoice_number = (document.to_dict() or {}).get("invoice_number", "")
                match = re.fullmatch(rf"INV-{year}-(\d+)", str(invoice_number))
                if match:
                    migrated_max = max(migrated_max, int(match.group(1)))
            self._invoice_sequence_ready.add(year)
        initial_next = migrated_max + 1 if year in self._invoice_sequence_ready else 1001
        requested_next = int(sale_payload.get("minimum_next", initial_next))
        transaction = self.client.transaction()
        @transactional
        def allocate(transaction):
            snapshot = _transaction_get(transaction, sequence_ref)
            current = (snapshot.to_dict() or {}).get("next_number", initial_next) if snapshot else initial_next
            number = max(int(current), initial_next, requested_next)
            transaction.set(
                sequence_ref,
                {"next_number": number + 1, "updated_at": _utcnow()},
                merge=True,
            )
            return number

        number = allocate(transaction)
        return f"INV-{year}-{number}"

    def _validate_device_scope(self, device, payload: dict, table_name: str):
        if table_name in {"sales", "stock_movements"}:
            if payload.get("shop_id") not in (None, self._device_value(device, "shop_id")):
                raise ValueError(f"{table_name.title()} shop does not match the registered device shop")
            if payload.get("device_id") not in (None, self._device_value(device, "id")):
                raise ValueError(f"{table_name.title()} device does not match the registered device")
            payload["shop_id"] = self._device_value(device, "shop_id")
            payload["device_id"] = self._device_value(device, "id")
            return

        if table_name == "sale_payments":
            if payload.get("device_id") not in (None, self._device_value(device, "id")):
                raise ValueError("Payment device does not match the registered device")
            payload["device_id"] = self._device_value(device, "id")
            return

        raise ValueError(f"Unknown table_name '{table_name}'")

    def _require_product_document(self, product_id) -> dict:
        """Reject sync writes that reference a product Firestore does not know."""
        if product_id in (None, ""):
            raise ValueError("product_id is required")
        snapshot = self._collection("products").document(str(product_id)).get()
        if not snapshot.exists:
            raise ValueError(f"Unknown product_id {product_id}")
        data = snapshot.to_dict() or {}
        # Stub shop-membership docs without catalog fields are not sellable products.
        if not data.get("sku") or not data.get("name"):
            raise ValueError(f"Unknown product_id {product_id}")
        return data

    def push_item(self, device, table_name: str, payload: dict) -> dict:
        """Idempotently write one existing outbox item and acknowledge it."""
        self._validate_device_scope(device, payload, table_name)

        if table_name == "sales":
            sale_id = payload["id"]
            sale_ref = self._collection("sales").document(sale_id)
            existing = sale_ref.get()
            if existing.exists:
                existing_data = existing.to_dict() or {}
                return {"invoice_number": existing_data.get("invoice_number")}

            items = payload.get("items", [])
            if not items:
                raise ValueError("Sale items are required")
            product_data_by_id = {
                item["product_id"]: self._require_product_document(item["product_id"])
                for item in items
            }

            invoice = payload.get("invoice_number") or self._allocate_invoice(payload)
            sale = {
                "id": sale_id,
                "shop_id": self._device_value(device, "shop_id"),
                "device_id": self._device_value(device, "id"),
                "staff_id": payload.get("staff_id"),
                "customer_name": payload.get("customer_name"),
                "payment_method": payload.get("payment_method", "cash"),
                "total_amount": str(payload.get("total_amount", "0")),
                "invoice_number": invoice,
                "created_at": payload.get("created_at") or _utcnow(),
                "updated_at": _utcnow(),
                "server_received_at": _utcnow(),
            }
            batch = self.client.batch()
            batch.set(sale_ref, sale, merge=True)
            for item in items:
                item_id = item.get("id") or f"{sale_id}:{item['product_id']}"
                product_data = product_data_by_id[item["product_id"]]
                quantity = int(item["quantity"])
                unit_price = Decimal(str(item.get("unit_price", product_data.get("unit_price", "0"))))
                batch.set(
                    self._collection("sale_items").document(item_id),
                    {
                        **item,
                        "id": item_id,
                        "sale_id": sale_id,
                        "unit_price": str(unit_price),
                        "subtotal": str((unit_price * quantity).quantize(Decimal("0.01"))),
                        "unit_cost": str(item.get("unit_cost", product_data.get("cost_price", "0"))),
                    },
                    merge=True,
                )
            if payload.get("amount_paid") not in (None, 0, "0", "0.00"):
                payment_id = payload.get("payment_id") or f"{sale_id}:initial-payment"
                batch.set(self._collection("sale_payments").document(payment_id), {
                    "id": payment_id,
                    "sale_id": sale_id,
                    "shop_id": self._device_value(device, "shop_id"),
                    "amount": str(payload["amount_paid"]),
                    "device_id": self._device_value(device, "id"),
                    "staff_id": payload.get("staff_id"),
                    "created_at": _utcnow(),
                    "updated_at": _utcnow(),
                }, merge=True)
            for item in items:
                movement_id = item.get("stock_movement_id") or f"{sale_id}:{item['product_id']}:sale"
                batch.set(self._collection("stock_movements").document(movement_id), {
                    "id": movement_id,
                    "product_id": item["product_id"],
                    "shop_id": self._device_value(device, "shop_id"),
                    "device_id": self._device_value(device, "id"),
                    "quantity_delta": -int(item["quantity"]),
                    "reason": "sale",
                    "reference_id": sale_id,
                    "created_at": _utcnow(),
                    "updated_at": _utcnow(),
                }, merge=True)
            batch.commit()
            for item in items:
                self._mark_product_shop(item["product_id"], self._device_value(device, "shop_id"))
            return {"invoice_number": invoice}

        if table_name == "sale_payments":
            sale = self._collection("sales").document(str(payload["sale_id"])).get()
            sale_data = sale.to_dict() or {}
            if not sale.exists or sale_data.get("shop_id") != self._device_value(device, "shop_id"):
                raise ValueError("Payment sale does not match the registered device shop")
            payment_id = payload["id"]
            self._write("sale_payments", payment_id, {
                **payload,
                "id": payment_id,
                "device_id": self._device_value(device, "id"),
                "updated_at": _utcnow(),
            })
            return {}

        if table_name == "stock_movements":
            movement_id = payload["id"]
            self._require_product_document(payload.get("product_id"))
            self._write("stock_movements", movement_id, {
                **payload,
                "id": movement_id,
                "shop_id": self._device_value(device, "shop_id"),
                "device_id": self._device_value(device, "id"),
                "updated_at": _utcnow(),
            })
            self._mark_product_shop(payload["product_id"], self._device_value(device, "shop_id"))
            return {}

        raise ValueError(f"Unknown table_name '{table_name}'")

    def pull(self, shop_id: int, since: datetime | None) -> dict:
        """Return the existing pull response shape, filtered to one shop."""
        collections = {
            "shops": list(self._collection("shops").where("id", "==", shop_id).stream()),
            "staff": list(self._collection("staff").where("shop_id", "==", shop_id).stream()),
            "products": list(self._collection("products").where("shop_ids", "array_contains", shop_id).stream()),
            "sales": list(self._collection("sales").where("shop_id", "==", shop_id).stream()),
            "payments": list(self._collection("sale_payments").where("shop_id", "==", shop_id).stream()),
            "stock_movements": list(self._collection("stock_movements").where("shop_id", "==", shop_id).stream()),
            "settings": list(self._collection("system_settings").stream()),
        }
        rows = {name: [doc.to_dict() or {} for doc in docs] for name, docs in collections.items()}
        all_products = list(rows["products"])
        if since:
            for name, values in rows.items():
                rows[name] = [value for value in values if _iso(value.get("updated_at")) and _iso(value["updated_at"]) >= _iso(since)]

        all_values = [value.get("updated_at") for values in rows.values() for value in values if value.get("updated_at")]
        high_watermark = max((_iso(value) for value in all_values), default=None)
        sale_ids = {sale["id"] for sale in rows["sales"]}
        sale_items = []
        for doc in self._collection("sale_items").stream():
            value = doc.to_dict() or {}
            if value.get("sale_id") in sale_ids:
                sale_items.append(value)

        # Incremental pulls must still include product parents for any child
        # rows that crossed the cursor, even when the product itself is unchanged.
        needed_product_ids = {
            str(value.get("product_id"))
            for value in (*rows["stock_movements"], *sale_items)
            if value.get("product_id") is not None
        }
        products_by_id = {str(product.get("id")): product for product in rows["products"]}
        for product in all_products:
            product_id = str(product.get("id"))
            if product_id in needed_product_ids and product_id not in products_by_id:
                products_by_id[product_id] = product
        rows["products"] = list(products_by_id.values())

        rows["staff"] = [
            _clean_staff({**staff, "shop_id": shop_id})
            for staff in rows["staff"]
        ]

        return {
            "next_cursor": high_watermark or _iso(since),
            "server_time": _iso(_utcnow()),
            "shops": rows["shops"],
            "staff": rows["staff"],
            "settings": rows["settings"],
            "products": rows["products"],
            "sales": rows["sales"],
            "sale_items": sale_items,
            "payments": rows["payments"],
            "stock_movements": rows["stock_movements"],
        }


def get_firestore_sync_service():
    configured = current_app.config.get("FIRESTORE_SYNC_SERVICE")
    if configured is not None:
        return configured
    return FirestoreSyncService.from_config()
