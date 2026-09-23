from flask import Blueprint, current_app, jsonify, request, g
from datetime import datetime, timedelta, timezone
import requests

from werkzeug.security import check_password_hash

from app.auth import issue_token, login_required, register_local_session, clear_local_session
from app.extensions import db

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# "Seller" login is cashiers only. "Shop Owner / Admin" covers owner,
# admin, and manager -- managers get finance-level access (profit,
# staff-adjacent trust) so they belong on the owner/admin side of the
# login screen, not lumped in with sellers.
ROLE_GROUPS = {
    "seller": ("cashier",),
    "owner": ("owner", "admin", "manager"),
}

_LOGIN_WINDOW = timedelta(minutes=5)
_LOGIN_LIMIT = 8
_LOGIN_FAILURES = {}

def _rate_key(email):
    return f"{email}|{request.remote_addr or 'unknown'}"

def _login_rate_limited(key):
    now = datetime.now(timezone.utc)
    row = _LOGIN_FAILURES.get(key)
    if not row or now - row["started"] > _LOGIN_WINDOW:
        _LOGIN_FAILURES[key] = {"started": now, "count": 0}
        return False
    return row["count"] >= _LOGIN_LIMIT

def _record_login_failure(key):
    now = datetime.now(timezone.utc)
    row = _LOGIN_FAILURES.get(key)
    if not row or now - row["started"] > _LOGIN_WINDOW:
        _LOGIN_FAILURES[key] = {"started": now, "count": 1}
    else:
        row["count"] += 1

def _clear_login_failures(key):
    _LOGIN_FAILURES.pop(key, None)


def _central_mode():
    return current_app.config.get("GLR_MODE") == "central"


def _staff_value(staff, key, default=None):
    if isinstance(staff, dict):
        return staff.get(key, default)
    return getattr(staff, key, default)


def _get_central_service():
    from app.firestore import get_firestore_sync_service

    return get_firestore_sync_service()


def _authenticate_against_central(email, password, role_group):
    central_url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/")
    try:
        response = requests.post(
            central_url + "/api/auth/login",
            json={"email": email, "password": password, "role_group": role_group},
            timeout=30,
        )
    except requests.ConnectionError:
        return None, (jsonify(
            error="An internet connection to the central server is required to log in.",
            code="central_auth_network",
        ), 503)
    except requests.Timeout:
        return None, (jsonify(
            error="We couldn't reach the central authentication server. Please try again.",
            code="central_auth_unavailable",
        ), 503)
    except requests.RequestException:
        return None, (jsonify(
            error="We couldn't reach the central authentication server. Please try again.",
            code="central_auth_unavailable",
        ), 503)

    if response.status_code in (401, 403):
        return None, (jsonify(error="Invalid email or password.", code="invalid_credentials"), 401)
    if response.status_code >= 500 or not 200 <= response.status_code < 300:
        return None, (jsonify(
            error="We couldn't reach the central authentication server. Please try again.",
            code="central_auth_unavailable",
        ), 503)

    try:
        payload = response.json()
    except ValueError:
        return None, (jsonify(
            error="The central authentication server returned an invalid response.",
            code="central_auth_unavailable",
        ), 503)
    staff = payload.get("staff")
    if not payload.get("token") or not isinstance(staff, dict):
        return None, (jsonify(
            error="The central authentication server returned an invalid response.",
            code="central_auth_unavailable",
        ), 503)
    return staff, payload["token"]


def _cache_central_identity(staff):
    from app.models import Staff

    staff_id = int(_staff_value(staff, "id"))
    email = str(_staff_value(staff, "email") or "").strip().lower()
    local_staff = db.session.get(Staff, staff_id)
    email_staff = Staff.query.filter_by(email=email).first()
    if email_staff and email_staff.id != staff_id:
        return None
    if not local_staff:
        local_staff = Staff(id=staff_id, name=email, email=email, password_hash="")
        db.session.add(local_staff)
    local_staff.shop_id = _staff_value(staff, "shop_id")
    local_staff.name = _staff_value(staff, "name") or email
    local_staff.email = email
    local_staff.role = _staff_value(staff, "role")
    local_staff.is_active = bool(_staff_value(staff, "is_active", True))
    db.session.commit()
    return local_staff


@auth_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role_group = data.get("role_group")  # "seller" | "owner" | omitted

    if not email or not password:
        return jsonify(error="Email and password are required"), 400

    key = _rate_key(email)
    if _login_rate_limited(key):
        return jsonify(error="Too many login attempts. Please wait 5 minutes and try again."), 429

    if _central_mode():
        # This IS the central server, so authentication happens here, against
        # this account's own stored password hash. There is no further server
        # to delegate to. (Local-mode desktop PCs take the other branch below,
        # which calls this same endpoint over HTTP on the real central server.)
        staff = _get_central_service().get_staff_by_email(email)
        central_token = None
        if not staff or not check_password_hash(_staff_value(staff, "password_hash") or "", password):
            _record_login_failure(key)
            return jsonify(error="Invalid email or password"), 401
    else:
        result = _authenticate_against_central(email, password, role_group)
        if isinstance(result[1], tuple):
            return result[1]
        staff, central_token = result
    if not staff or not _staff_value(staff, "is_active"):
        _record_login_failure(key)
        return jsonify(error="Invalid email or password"), 401

    if role_group in ROLE_GROUPS and _staff_value(staff, "role") not in ROLE_GROUPS[role_group]:
        _record_login_failure(key)
        other = "owner" if role_group == "seller" else "seller"
        label = "Seller" if role_group == "seller" else "Shop Owner / Admin"
        other_label = "Shop Owner / Admin" if role_group == "seller" else "Seller"
        return jsonify(
            error=f"These credentials aren't valid for a {label} login. Try {other_label} instead."
        ), 401

    if not _central_mode() and not _cache_central_identity(staff):
        return jsonify(error="The central identity conflicts with a local account"), 409

    _clear_login_failures(key)
    if _central_mode():
        _get_central_service().update_staff_auth_state(
            _staff_value(staff, "id"),
            quick_pin_failed_attempts=0,
            quick_pin_locked_until=None,
            updated_at=datetime.now(timezone.utc),
        )
    else:
        # Central authentication has already succeeded. The local database is
        # only used by the authenticated session/data layer in local mode.
        pass
    token = issue_token(staff) if _central_mode() else central_token
    if not _central_mode():
        register_local_session(token, staff)
        # Synchronization is event-driven: successful login is the explicit
        # reason to perform a central pull. The background worker does not pull
        # merely because the local Flask process started.
        import threading
        from app.sync.worker import pull_reference_data_once
        app_obj = current_app._get_current_object()
        threading.Thread(
            target=lambda: pull_reference_data_once(app_obj),
            daemon=True,
            name="glr-login-pull",
        ).start()
    return jsonify(
        token=token,
        staff={
            "id": _staff_value(staff, "id"),
            "name": _staff_value(staff, "name"),
            "email": _staff_value(staff, "email"),
            "role": _staff_value(staff, "role"),
            "shop_id": _staff_value(staff, "shop_id"),
            # Local-mode desktops re-run this same is_active check against
            # this exact payload after proxying here, so it must be included
            # or every local login is rejected even with a correct password.
            "is_active": bool(_staff_value(staff, "is_active", True)),
        },
    )


@auth_bp.post("/logout")
@login_required
def logout():
    """Invalidate the current token by advancing the staff update timestamp."""
    if _central_mode():
        _get_central_service().update_staff_auth_state(
            g.staff_id, updated_at=datetime.now(timezone.utc)
        )
    else:
        central_url = current_app.config["CENTRAL_SYNC_URL"].rstrip("/")
        try:
            response = requests.post(
                central_url + "/api/auth/logout",
                headers={"Authorization": request.headers["Authorization"]},
                timeout=30,
            )
            if response.status_code >= 400:
                return jsonify(error="Central session logout was rejected"), 401
        except requests.RequestException:
            return jsonify(
                error="Your session requires a connection to the central server.",
                code="central_session_unavailable",
            ), 503
    if not _central_mode():
        clear_local_session(request.headers["Authorization"].split(" ", 1)[1])
    return jsonify(ok=True)


@auth_bp.get("/me")
@login_required
def current_identity():
    """Return safe central identity metadata for one-time desktop enrollment."""
    if _central_mode():
        staff = _get_central_service().get_staff(g.staff_id)
    else:
        from app.models import Staff
        staff = db.session.get(Staff, g.staff_id)
    return jsonify(
        id=_staff_value(staff, "id", g.staff_id),
        name=_staff_value(staff, "name"),
        email=_staff_value(staff, "email"),
        role=_staff_value(staff, "role"),
        shop_id=_staff_value(staff, "shop_id"),
        is_active=_staff_value(staff, "is_active"),
    )

@auth_bp.post("/verify-pin")
@login_required
def verify_pin():
    """Verify the short admin unlock PIN on the backend, never in browser storage."""
    from werkzeug.security import check_password_hash
    from datetime import datetime, timezone, timedelta

    if g.staff_role not in ("owner", "admin"):
        return jsonify(error="PIN unlock is only available to administrators."), 403

    if _central_mode():
        staff = _get_central_service().get_staff(g.staff_id)
    else:
        from app.models import Staff
        staff = db.session.get(Staff, g.staff_id)
    if not staff or not _staff_value(staff, "quick_pin_hash"):
        return jsonify(error="No quick unlock PIN is configured.", force_login=True), 409

    now = datetime.now(timezone.utc)
    locked_until = _staff_value(staff, "quick_pin_locked_until")
    if locked_until and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until and locked_until > now:
        return jsonify(error="The quick PIN is disabled. Please sign in again.", force_login=True), 423

    pin = str((request.get_json(silent=True) or {}).get("pin") or "").strip()
    if not pin.isdigit() or len(pin) != 4:
        return jsonify(error="Enter your 4-digit PIN."), 400

    if not check_password_hash(_staff_value(staff, "quick_pin_hash"), pin):
        failed_attempts = (_staff_value(staff, "quick_pin_failed_attempts") or 0) + 1
        if failed_attempts >= 3:
            failed_attempts = 0
            locked_until = now + timedelta(minutes=15)
            if _central_mode():
                _get_central_service().update_staff_auth_state(
                    g.staff_id,
                    quick_pin_failed_attempts=failed_attempts,
                    quick_pin_locked_until=locked_until,
                    updated_at=now,
                )
            else:
                staff.quick_pin_failed_attempts = failed_attempts
                staff.quick_pin_locked_until = locked_until
                db.session.commit()
            return jsonify(error="Three incorrect PIN attempts. Please sign in again.", force_login=True), 423
        if _central_mode():
            _get_central_service().update_staff_auth_state(
                g.staff_id, quick_pin_failed_attempts=failed_attempts, updated_at=now
            )
        else:
            staff.quick_pin_failed_attempts = failed_attempts
            db.session.commit()
        remaining = 3 - failed_attempts
        return jsonify(error=f"Incorrect PIN. {remaining} attempt(s) remaining."), 401

    if _central_mode():
        _get_central_service().update_staff_auth_state(
            g.staff_id,
            quick_pin_failed_attempts=0,
            quick_pin_locked_until=None,
            updated_at=now,
        )
    else:
        staff.quick_pin_failed_attempts = 0
        staff.quick_pin_locked_until = None
        db.session.commit()
    return jsonify(ok=True)
