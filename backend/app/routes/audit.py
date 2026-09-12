"""
Viewing the audit log always means asking the central server -- entries
are only ever created there (see models/audit.py for why). A local
device's own request just proxies straight through to central and
returns whatever it says, the same read-through pattern used nowhere
else in this app because nothing else needs it: this is the one thing
that's pure read, no local write-then-mirror dance required, since
there's nothing local to keep fresh here.
"""
from flask import Blueprint, current_app, jsonify, request

from app.auth import roles_required
from app.models import AuditLogEntry

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit-log")


@audit_bp.get("")
@roles_required("owner", "admin")
def list_audit_log():
    if current_app.config["GLR_MODE"] == "central":
        entries = AuditLogEntry.query.order_by(AuditLogEntry.created_at.desc()).limit(200).all()
        return jsonify([e.to_dict() for e in entries])

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