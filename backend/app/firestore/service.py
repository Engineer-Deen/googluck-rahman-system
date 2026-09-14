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
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from flask import current_app
from google.cloud.firestore_v1.transaction import transactional
from werkzeug.security import generate_password_hash

from app.extensions import db


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

_CENTRAL_TABLES = (
    ("shops", "shops"),
    ("staff", "staff"),
    ("products", "products"),
    ("devices", "devices"),
    ("system_settings", "system_settings"),
    ("sales", "sales"),
    ("sale_items", "sale_items"),
    ("sale_payments", "sale_payments"),
    ("stock_movements", "stock_movements"),
    ("audit_log", "audit_log"),
)


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


class FirestoreSyncService:
    """Translate existing sync payloads to stable Firestore documents."""

    def __init__(self, client):
        self.client = client
        self.conflicts: list[dict[str, Any]] = []
        self._refresh_lock = threading.Lock()
        self._last_refresh_monotonic = 0.0
        self._last_refresh_generation = None
        self._reported_conflicts: set[str] = set()
        self._invoice_sequence_ready: set[int] = set()

    @staticmethod
    def _model_map():
        from app.models import (
            AuditLogEntry,
            Device,
            Product,
            Sale,
            SaleItem,
            SalePayment,
            Shop,
            Staff,
            StockMovement,
            SystemSetting,
        )

        return {
            "shops": Shop,
            "staff": Staff,
            "products": Product,
            "devices": Device,
            "system_settings": SystemSetting,
            "sales": Sale,
            "sale_items": SaleItem,
            "sale_payments": SalePayment,
            "stock_movements": StockMovement,
            "audit_log": AuditLogEntry,
        }

    @staticmethod
    def _parse_datetime(value):
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
            return datetime.fromisoformat(normalized)
        return value

    @staticmethod
    def _document_id(row):
        return str(getattr(row, "id"))

    @staticmethod
    def _public_row(row):
        sensitive = _SENSITIVE_STAFF_FIELDS if row.__tablename__ == "staff" else set()
        data = {}
        for column in row.__table__.columns:
            if column.name in sensitive:
                continue
            value = getattr(row, column.name)
            data[column.name] = _iso(value)
        return data

    def _record_conflict(self, collection, document_id, local, incoming):
        conflict_id = f"{collection}:{document_id}"
        if conflict_id in self._reported_conflicts:
            return
        conflict = {
            "id": conflict_id,
            "collection": collection,
            "document_id": str(document_id),
            "local": self._firestore_value(local),
            "firestore": self._firestore_value(incoming),
            "detected_at": _utcnow(),
        }
        self.conflicts.append(conflict)
        self._reported_conflicts.add(conflict_id)
        self._write("provider_conflicts", conflict["id"], conflict)

    @classmethod
    def _firestore_value(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._firestore_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._firestore_value(item) for item in value]
        return _iso(value)

    def refresh_sql_mirror(self, force=False):
        """Materialize Firestore's authoritative state for existing routes.

        Routes continue to use the established ORM transaction logic. In
        central Firestore mode this method makes that compatibility mirror
        reflect Firestore before a request, while keeping password and PIN
        hashes in the retained SQL credential store.
        """
        now = time.monotonic()
        refresh_seconds = max(0, int(current_app.config.get("FIRESTORE_MIRROR_REFRESH_SECONDS", 30)))
        generation_snapshot = self._collection("provider_metadata").document("central_state").get()
        generation = (generation_snapshot.to_dict() or {}).get("generation") if generation_snapshot.exists else None
        generation_changed = generation != self._last_refresh_generation
        if not force and not generation_changed and now - self._last_refresh_monotonic < refresh_seconds:
            return False
        with self._refresh_lock:
            now = time.monotonic()
            if not force and not generation_changed and now - self._last_refresh_monotonic < refresh_seconds:
                return False
            model_map = self._model_map()
            skipped_sales: set[str] = set()
            product_costs: dict[str, str] = {}
            with db.session.no_autoflush:
                for collection, _ in _CENTRAL_TABLES:
                    model = model_map[collection]
                    for snapshot in self._collection(collection).stream():
                        incoming = snapshot.to_dict() or {}
                        document_id = incoming.get("id") or getattr(snapshot, "id", None)
                        if document_id is None:
                            continue
                        if collection in {"shops", "staff", "products", "system_settings", "audit_log"}:
                            try:
                                document_id = int(document_id)
                            except (TypeError, ValueError):
                                continue
                        if collection == "products":
                            product_costs[str(document_id)] = str(incoming.get("cost_price", "0"))
                        if collection == "sale_items":
                            quantity = int(incoming.get("quantity", 0) or 0)
                            raw_unit_price = incoming.get("unit_price", "0")
                            if raw_unit_price in (None, ""):
                                raw_unit_price = "0"
                            try:
                                unit_price = Decimal(str(raw_unit_price))
                            except Exception:
                                unit_price = Decimal("0")
                            incoming.setdefault("subtotal", str((unit_price * quantity).quantize(Decimal("0.01"))))
                            if "unit_price" not in incoming or incoming.get("unit_price") in (None, ""):
                                incoming["unit_price"] = str(unit_price)
                            if "unit_cost" not in incoming or incoming.get("unit_cost") in (None, ""):
                                incoming["unit_cost"] = product_costs.get(str(incoming.get("product_id")), "0")
                        if collection == "sales" and incoming.get("invoice_number"):
                            invoice_owner = db.session.query(model).filter_by(invoice_number=incoming["invoice_number"]).first()
                            if invoice_owner and str(invoice_owner.id) != str(document_id):
                                self._record_conflict(
                                    collection,
                                    document_id,
                                    self._public_row(invoice_owner),
                                    {key: _iso(value) for key, value in incoming.items()},
                                )
                                skipped_sales.add(str(document_id))
                                continue
                        if collection in {"sale_items", "sale_payments"} and str(incoming.get("sale_id")) in skipped_sales:
                            continue
                        row = db.session.get(model, document_id)
                        if row is None:
                            if collection == "staff":
                                row = model(
                                    id=document_id,
                                    password_hash=generate_password_hash(uuid.uuid4().hex),
                                )
                            else:
                                row = model(id=document_id)
                            db.session.add(row)
                        local = self._public_row(row)
                        incoming_public = {key: _iso(value) for key, value in incoming.items() if key not in _SENSITIVE_STAFF_FIELDS}
                        ignored_conflict_fields = {"updated_at", "created_at", "last_seen_at"}
                        if row not in db.session.new and any(
                            local.get(key) is not None and incoming_public.get(key) is not None and str(local.get(key)) != str(incoming_public.get(key))
                            for key in incoming_public
                            if key in local and key not in ignored_conflict_fields
                        ):
                            self._record_conflict(collection, document_id, local, incoming_public)
                        for column in model.__table__.columns:
                            name = column.name
                            if name in _SENSITIVE_STAFF_FIELDS or name not in incoming:
                                continue
                            value = incoming[name]
                            if name.endswith("_at") or name in {"created_at", "updated_at", "server_received_at", "voided_at"}:
                                value = self._parse_datetime(value)
                            setattr(row, name, value)
            db.session.commit()
            self._last_refresh_monotonic = time.monotonic()
            self._last_refresh_generation = generation
            return True

    def mirror_sql_state(self):
        """Persist the compatibility mirror without copying staff secrets."""
        model_map = self._model_map()
        for collection, _ in _CENTRAL_TABLES:
            model = model_map[collection]
            for row in db.session.query(model).all():
                self._write(collection, self._document_id(row), self._public_row(row))

    def mirror_recent_sql_state(self, since: datetime):
        """Mirror only rows changed during the current central request."""
        since = self._parse_datetime(since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        else:
            since = since.astimezone(timezone.utc)
        model_map = self._model_map()
        changed_sales = set()
        for collection, _ in _CENTRAL_TABLES:
            model = model_map[collection]
            rows = db.session.query(model).all()
            for row in rows:
                timestamps = [getattr(row, name, None) for name in ("created_at", "updated_at")]
                if not any(
                    value
                    and (
                        self._parse_datetime(value).replace(tzinfo=timezone.utc)
                        if self._parse_datetime(value).tzinfo is None
                        else self._parse_datetime(value).astimezone(timezone.utc)
                    ) >= since
                    for value in timestamps
                ):
                    continue
                self._write(collection, self._document_id(row), self._public_row(row))
                if collection == "sales":
                    changed_sales.add(row.id)
        if changed_sales:
            for collection, model in (("sale_items", model_map["sale_items"]), ("sale_payments", model_map["sale_payments"])):
                for row in db.session.query(model).filter(model.sale_id.in_(changed_sales)).all():
                    self._write(collection, self._document_id(row), self._public_row(row))

    def mark_central_state_changed(self):
        self._write(
            "provider_metadata",
            "central_state",
            {"generation": uuid.uuid4().hex, "updated_at": _utcnow()},
        )

    def allocate_invoice_number(self, sale_payload: dict) -> str:
        """Allocate the next invoice number from Firestore's migrated sequence."""
        return self._allocate_invoice(sale_payload)

    @classmethod
    def from_config(cls):
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
            snapshot = transaction.get(sequence_ref)
            if not hasattr(snapshot, "to_dict"):
                snapshot = next(iter(snapshot), None)
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
            if payload.get("shop_id") not in (None, device.shop_id):
                raise ValueError(f"{table_name.title()} shop does not match the registered device shop")
            if payload.get("device_id") not in (None, device.id):
                raise ValueError(f"{table_name.title()} device does not match the registered device")
            payload["shop_id"] = device.shop_id
            payload["device_id"] = device.id
            return

        if table_name == "sale_payments":
            if payload.get("device_id") not in (None, device.id):
                raise ValueError("Payment device does not match the registered device")
            payload["device_id"] = device.id
            return

        raise ValueError(f"Unknown table_name '{table_name}'")

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

            invoice = payload.get("invoice_number") or self._allocate_invoice(payload)
            sale = {
                "id": sale_id,
                "shop_id": device.shop_id,
                "device_id": device.id,
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
            for item in payload.get("items", []):
                item_id = item.get("id") or f"{sale_id}:{item['product_id']}"
                product_snapshot = self._collection("products").document(str(item["product_id"])).get()
                product_data = product_snapshot.to_dict() or {}
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
                    "shop_id": device.shop_id,
                    "amount": str(payload["amount_paid"]),
                    "device_id": device.id,
                    "staff_id": payload.get("staff_id"),
                    "created_at": _utcnow(),
                    "updated_at": _utcnow(),
                }, merge=True)
            for item in payload.get("items", []):
                movement_id = item.get("stock_movement_id") or f"{sale_id}:{item['product_id']}:sale"
                batch.set(self._collection("stock_movements").document(movement_id), {
                    "id": movement_id,
                    "product_id": item["product_id"],
                    "shop_id": device.shop_id,
                    "device_id": device.id,
                    "quantity_delta": -int(item["quantity"]),
                    "reason": "sale",
                    "reference_id": sale_id,
                    "created_at": _utcnow(),
                    "updated_at": _utcnow(),
                }, merge=True)
            batch.commit()
            for item in payload.get("items", []):
                self._mark_product_shop(item["product_id"], device.shop_id)
            return {"invoice_number": invoice}

        if table_name == "sale_payments":
            sale = self._collection("sales").document(str(payload["sale_id"])).get()
            sale_data = sale.to_dict() or {}
            if not sale.exists or sale_data.get("shop_id") != device.shop_id:
                raise ValueError("Payment sale does not match the registered device shop")
            payment_id = payload["id"]
            self._write("sale_payments", payment_id, {
                **payload,
                "id": payment_id,
                "device_id": device.id,
                "updated_at": _utcnow(),
            })
            return {}

        if table_name == "stock_movements":
            movement_id = payload["id"]
            self._write("stock_movements", movement_id, {
                **payload,
                "id": movement_id,
                "shop_id": device.shop_id,
                "device_id": device.id,
                "updated_at": _utcnow(),
            })
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
