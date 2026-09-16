"""
Viewing the audit log always means asking the central server -- entries
are only ever created there (see models/audit.py for why). A local
device's own request just proxies straight through to central and
returns whatever it says, the same read-through pattern used nowhere
else in this app because nothing else needs it: this is the one thing
that's pure read, no local write-then-mirror dance required, since
there's nothing local to keep fresh here.
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app.auth import roles_required

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit-log")

ACTION_LABELS = {
    "product_created": "Added a new product",
    "product_updated": "Updated product information",
    "product_deleted": "Removed a product from the catalog",
    "staff_created": "Created a staff account",
    "staff_updated": "Updated a staff account",
    "staff_password_reset": "Reset a staff password",
    "admin_account_created": "Created an administrator account",
    "sale_updated": "Corrected a sale",
    "sale_voided": "Voided a sale",
    "shop_settings_updated": "Updated shop branding",
    "system_settings_updated": "Updated system settings",
}


def _serialize_firestore_entry(entry):
    action = entry.get("action", "unknown")
    raw_details = entry.get("details") or entry.get("details_json") or {}
    if isinstance(raw_details, str):
        try:
            raw_details = json.loads(raw_details)
        except (TypeError, ValueError):
            raw_details = {}
    raw_details = raw_details if isinstance(raw_details, dict) else {}
    description = entry.get("description") or entry.get("details_text")
    if not description:
        descriptions = {
            "product_created": f"Added product {raw_details.get('name', 'Unknown')} with SKU {raw_details.get('sku', 'auto-assigned')} in {raw_details.get('category', 'the selected category')}.",
            "product_deleted": f"Removed {raw_details.get('name', 'the product')} from the catalog.",
            "product_updated": f"Updated {raw_details.get('after', {}).get('name', raw_details.get('before', {}).get('name', 'the product'))}.",
            "staff_created": f"Created {raw_details.get('role', 'staff')} account for {raw_details.get('name', 'Unknown')} ({raw_details.get('email', '')}).",
            "staff_updated": f"Updated staff account for {raw_details.get('target_name', 'the selected staff member')}.",
            "staff_password_reset": f"Reset the password for {raw_details.get('target_email', 'the staff account')}.",
            "admin_account_created": f"Created administrator account for {raw_details.get('name', 'Unknown')} ({raw_details.get('email', '')}).",
            "sale_updated": f"Corrected sale {raw_details.get('invoice_number', 'the sale')}.",
            "sale_voided": f"Voided sale {raw_details.get('invoice_number', 'the sale')} for {raw_details.get('customer_name', 'the customer')}.",
            "shop_settings_updated": "Updated the shop name or logo.",
        }
        description = descriptions.get(action, ACTION_LABELS.get(action, action.replace("_", " ").title()) + ".")
    reason = entry.get("reason") or raw_details.get("reason") or "—"
    created_at = entry.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at = created_at.isoformat()
    return {
        "id": entry.get("id"),
        "actor_staff_id": entry.get("actor_staff_id"),
        "actor_name": entry.get("actor_name"),
        "actor_role": entry.get("actor_role"),
        "action": ACTION_LABELS.get(action, action.replace("_", " ").title()),
        "entity_type": entry.get("entity_type"),
        "entity_id": str(entry.get("entity_id")) if entry.get("entity_id") is not None else None,
        "details": description,
        "description": description,
        "reason": reason,
        "created_at": created_at,
    }


@audit_bp.get("")
@roles_required("owner", "admin")
def list_audit_log():
    if current_app.config["GLR_MODE"] == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        try:
            limit = min(max(int(request.args.get("limit", 200)), 1), 200)
        except (TypeError, ValueError):
            limit = 200
        since = until = None
        for name in ("since", "until"):
            value = request.args.get(name)
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return jsonify(error=f"`{name}` must be an ISO timestamp"), 400
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if name == "since":
                    since = parsed
                else:
                    until = parsed
        entries = service.list_audit(limit=limit, action=request.args.get("action"), entity_type=request.args.get("entity_type"), since=since, until=until)
        return jsonify([_serialize_firestore_entry(entry) for entry in entries])

    import requests

    url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/") + "/api/audit-log"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": request.headers.get("Authorization", "")},
            timeout=10,
        )
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException:
        return jsonify(
            error="Can't reach the central server to load the audit log right now. Try again once connected."
        ), 503