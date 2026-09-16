import re

from flask import Blueprint, current_app, jsonify, request

from app.firestore.service import FirestoreSyncService


diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnostics")


def _safe_error_message(exc):
    message = str(exc)
    message = re.sub(
        r"(?i)(private_key|client_email|access_token|token|authorization|password|secret)[^,;\n]*",
        "<redacted>",
        message,
    )
    message = re.sub(r"([A-Za-z]:\\[^\s,;]+|/[^\s,;]+\.json)", "<path-redacted>", message)
    return message[:500]


def _rpc_status(exc):
    status = getattr(exc, "code", None)
    return getattr(status, "name", None) or getattr(status, "value", None) or str(status or "UNKNOWN")


def _sync_key_is_valid():
    expected = current_app.config.get("SYNC_API_KEY", "")
    return bool(expected) and request.headers.get("X-Sync-Key", "") == expected


@diagnostics_bp.get("/firestore")
def firestore_diagnostic():
    if not _sync_key_is_valid():
        return jsonify(error="Invalid or missing sync key"), 401
    if current_app.config.get("GLR_MODE") != "central":
        return jsonify(error="Firestore diagnostics require central mode"), 409

    operation = 'client.collection("_glr_probe").limit(1).stream()'
    try:
        service = FirestoreSyncService.from_config(validate=False)
        client = service.client
        api = getattr(client, "_firestore_api", None)
        transport = getattr(api, "_transport", None) if api else None
        response = {
            "status": "PASS",
            "database_id": current_app.config.get("FIRESTORE_DATABASE", "(default)"),
            "endpoint": getattr(transport, "_host", "UNKNOWN"),
            "operation": operation,
        }
        database_string = getattr(client, "_database_string", "")
        response["project_id"] = database_string.split("/")[1] if database_string.startswith("projects/") else "UNKNOWN"
        list(client.collection("_glr_probe").limit(1).stream())
        return jsonify(response)
    except Exception as exc:
        service = locals().get("service")
        client = getattr(service, "client", None)
        database_string = getattr(client, "_database_string", "") if client else ""
        api = getattr(client, "_firestore_api", None) if client else None
        transport = getattr(api, "_transport", None) if api else None
        response = {
            "status": _rpc_status(exc),
            "project_id": database_string.split("/")[1] if database_string.startswith("projects/") else "UNKNOWN",
            "database_id": current_app.config.get("FIRESTORE_DATABASE", "(default)"),
            "endpoint": getattr(transport, "_host", "UNKNOWN"),
            "operation": operation,
            "exception_class": f"{type(exc).__module__}.{type(exc).__name__}",
            "rpc_status": _rpc_status(exc),
            "message": _safe_error_message(exc),
        }
        return jsonify(response), 503
