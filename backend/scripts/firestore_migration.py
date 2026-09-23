from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("GLR_MODE", "central")
os.environ.setdefault("CENTRAL_DATA_PROVIDER", "firestore")

from app import create_app
from app.extensions import db
from app.firestore.service import FirestoreSyncService
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


TABLE_ORDER = [
    "shops",
    "staff",
    "products",
    "devices",
    "system_settings",
    "sales",
    "sale_items",
    "sale_payments",
    "stock_movements",
    "audit_log",
]


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


def _normalize_payload(value: Any) -> str:
    def normalize(item: Any):
        if isinstance(item, dict):
            return {str(k): normalize(v) for k, v in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.astimezone(timezone.utc).isoformat() if item.tzinfo else item.replace(tzinfo=timezone.utc).isoformat()
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), default=str)


def _record_decision(existing_payload: str | None, incoming_payload: str) -> str:
    if existing_payload is None:
        return "create"
    if existing_payload == incoming_payload:
        return "skip"
    return "conflict"


def _table_rows(table_name: str):
    model_map = {
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
    model = model_map[table_name]
    rows = db.session.query(model).all()
    return rows


def _row_to_payload(table_name: str, row: Any) -> dict[str, Any]:
    # NOTE: staff rows are intentionally migrated with their credential
    # fields intact (password_hash, quick_pin_hash, etc). _clean_staff()
    # exists to redact those fields on *outward-facing* reads (sync pull,
    # API responses) -- applying it here as well silently strips
    # password_hash before it ever reaches Firestore, so every migrated
    # account permanently loses its password and can never log in again,
    # no matter how many times the correct password is entered.
    raw = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in list(raw.items()):
        raw[key] = _iso(value)
    return raw


def _firestore_collection_name(table_name: str) -> str:
    return {
        "audit_log": "audit_log",
        "system_settings": "system_settings",
    }.get(table_name, table_name)


def _count_firestore_docs(service: FirestoreSyncService, collection_name: str) -> int:
    try:
        return len(list(service._collection(collection_name).stream()))
    except Exception:
        return 0


def _run_migration(dry_run: bool = True) -> dict[str, Any]:
    app = create_app()
    with app.app_context():
        inspector = inspect(db.engine)
        available_tables = set(inspector.get_table_names())

        report = OrderedDict()
        summary = {
            "dry_run": dry_run,
            "created": 0,
            "skipped": 0,
            "conflicts": 0,
            "errors": 0,
            "total_rows": 0,
        }

        service = FirestoreSyncService.from_config()

        for table_name in TABLE_ORDER:
            if table_name not in available_tables:
                report[table_name] = {
                    "pg_count": 0,
                    "firestore_count": _count_firestore_docs(service, _firestore_collection_name(table_name)),
                    "created": 0,
                    "skipped": 0,
                    "conflicts": 0,
                    "errors": 0,
                    "status": "missing_pg_table",
                }
                continue

            rows = _table_rows(table_name)
            doc_collection = _firestore_collection_name(table_name)
            pg_count = len(rows)
            summary["total_rows"] += pg_count
            report[table_name] = {
                "pg_count": pg_count,
                "firestore_count": _count_firestore_docs(service, doc_collection),
                "created": 0,
                "skipped": 0,
                "conflicts": 0,
                "errors": 0,
                "status": "pending",
            }

            for row in rows:
                payload = _row_to_payload(table_name, row)
                doc_id = str(payload["id"])
                ref = service._collection(doc_collection).document(doc_id)
                existing_doc = ref.get()
                existing_payload = None
                if existing_doc.exists:
                    existing_payload = existing_doc.to_dict() or {}
                    for key, value in list(existing_payload.items()):
                        existing_payload[key] = _iso(value)
                incoming = _normalize_payload(payload)
                existing_norm = _normalize_payload(existing_payload) if existing_payload is not None else None
                decision = _record_decision(existing_norm, incoming)

                if decision == "create":
                    report[table_name]["created"] += 1
                    summary["created"] += 1
                    if not dry_run:
                        try:
                            service._write(doc_collection, doc_id, payload)
                        except Exception as exc:  # pragma: no cover - runtime safety
                            report[table_name]["errors"] += 1
                            summary["errors"] += 1
                            report[table_name].setdefault("error_examples", []).append({"id": doc_id, "error": str(exc)})
                            report[table_name]["status"] = "error"
                            continue
                elif decision == "skip":
                    report[table_name]["skipped"] += 1
                    summary["skipped"] += 1
                elif decision == "conflict":
                    report[table_name]["conflicts"] += 1
                    summary["conflicts"] += 1
                    report[table_name].setdefault("conflicts_detail", []).append({
                        "id": doc_id,
                        "pg": payload,
                        "firestore": existing_payload,
                    })
                else:
                    report[table_name]["errors"] += 1
                    summary["errors"] += 1
                    report[table_name]["status"] = "error"

            if report[table_name]["errors"] == 0 and report[table_name]["conflicts"] == 0:
                report[table_name]["status"] = "ok"
            elif report[table_name]["conflicts"]:
                report[table_name]["status"] = "conflict"
            else:
                report[table_name]["status"] = "error"

        report["summary"] = summary
        return report


def _parse_args():
    parser = argparse.ArgumentParser(description="Copy PostgreSQL business data to Firestore safely.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write anything to Firestore, just compare and report.")
    parser.add_argument("--execute", action="store_true", help="Perform the actual Firestore backfill.")
    parser.add_argument("--json-output", action="store_true", help="Print formatted JSON summary.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    dry_run = True if args.dry_run or not args.execute else False
    report = _run_migration(dry_run=dry_run)
    if args.json_output or True:
        print(json.dumps(report, indent=2, default=str))
