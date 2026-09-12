import json
from app.extensions import db
from app.models import AuditLogEntry


def log_action(actor_staff_id, actor_name, actor_role, action, entity_type, entity_id, details=None):
    details = details or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    entry = AuditLogEntry(
        actor_staff_id=actor_staff_id,
        actor_name=actor_name,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details_json=json.dumps(details, default=str) if details else None,
        details=None,
        reason=str(reason) if reason else None,
    )
    # The readable description is generated centrally so the UI never
    # needs to expose raw JSON to a business user.
    try:
        entry.details = entry.to_dict()["description"]
    except Exception:
        entry.details = action.replace("_", " ").capitalize() + "."
    db.session.add(entry)
    db.session.commit()
