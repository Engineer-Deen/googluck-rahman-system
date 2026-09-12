"""Human-readable, structured audit entries."""
import json
from datetime import datetime, timezone
from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


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


def _human_details(action, details):
    details = details or {}
    if action == "product_created":
        return f"Added product {details.get('name', 'Unknown')} with SKU {details.get('sku', 'auto-assigned')} in {details.get('category', 'the selected category')}."
    if action == "product_deleted":
        return f"Removed {details.get('name', 'the product')} from the catalog."
    if action == "product_updated":
        return f"Updated {details.get('after', {}).get('name', details.get('before', {}).get('name', 'the product'))}."
    if action == "staff_created":
        return f"Created {details.get('role', 'staff')} account for {details.get('name', 'Unknown')} ({details.get('email', '')})."
    if action == "staff_updated":
        return f"Updated staff account for {details.get('target_name', 'the selected staff member')}."
    if action == "staff_password_reset":
        return f"Reset the password for {details.get('target_email', 'the staff account')}."
    if action == "admin_account_created":
        return f"Created administrator account for {details.get('name', 'Unknown')} ({details.get('email', '')})."
    if action == "sale_updated":
        return f"Corrected sale {details.get('after', {}).get('invoice_number', details.get('invoice_number', 'the sale'))}."
    if action == "sale_voided":
        return f"Voided sale {details.get('invoice_number', 'the sale')} for {details.get('customer_name', 'the customer')}."
    if action == "shop_settings_updated":
        return "Updated the shop name or logo."
    if action == "system_settings_updated":
        return f"Updated admin security settings (inactivity timeout: {details.get('timeout_minutes', 'unchanged')} minutes; maximum session: {details.get('full_login_hours', 'unchanged')} hours)."
    return action.replace("_", " ").capitalize() + "."


class AuditLogEntry(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (db.Index("ix_audit_created_at", "created_at"), db.Index("ix_audit_action_created", "action", "created_at"))

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor_staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=True)
    actor_name = db.Column(db.String(120), nullable=True)
    actor_role = db.Column(db.String(30), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(30), nullable=False)
    entity_id = db.Column(db.String(36), nullable=False)
    details_json = db.Column(db.Text, nullable=True)
    details = db.Column(db.Text, nullable=True)
    reason = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        raw = None
        if self.details_json:
            try:
                raw = json.loads(self.details_json)
            except (TypeError, ValueError):
                raw = None
        description = self.details or _human_details(self.action, raw)
        reason = self.reason or ((raw or {}).get("reason") if isinstance(raw, dict) else None)
        return {
            "id": self.id,
            "actor_staff_id": self.actor_staff_id,
            "actor_name": self.actor_name,
            "actor_role": self.actor_role,
            "action": ACTION_LABELS.get(self.action, self.action.replace("_", " ").title()),
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "details": description,
            "description": description,
            "reason": reason or "—",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
