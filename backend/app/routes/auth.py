from flask import Blueprint, jsonify, request, g
from datetime import datetime, timedelta, timezone
from werkzeug.security import check_password_hash

from app.auth import issue_token, login_required
from app.models import Staff
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
    now = datetime.utcnow()
    row = _LOGIN_FAILURES.get(key)
    if not row or now - row["started"] > _LOGIN_WINDOW:
        _LOGIN_FAILURES[key] = {"started": now, "count": 0}
        return False
    return row["count"] >= _LOGIN_LIMIT

def _record_login_failure(key):
    now = datetime.utcnow()
    row = _LOGIN_FAILURES.get(key)
    if not row or now - row["started"] > _LOGIN_WINDOW:
        _LOGIN_FAILURES[key] = {"started": now, "count": 1}
    else:
        row["count"] += 1

def _clear_login_failures(key):
    _LOGIN_FAILURES.pop(key, None)


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

    staff = Staff.query.filter_by(email=email, is_active=True).first()
    if not staff or not check_password_hash(staff.password_hash, password):
        _record_login_failure(key)
        return jsonify(error="Invalid email or password"), 401

    if role_group in ROLE_GROUPS and staff.role not in ROLE_GROUPS[role_group]:
        _record_login_failure(key)
        other = "owner" if role_group == "seller" else "seller"
        label = "Seller" if role_group == "seller" else "Shop Owner / Admin"
        other_label = "Shop Owner / Admin" if role_group == "seller" else "Seller"
        return jsonify(
            error=f"These credentials aren't valid for a {label} login. Try {other_label} instead."
        ), 401

    _clear_login_failures(key)
    staff.quick_pin_failed_attempts = 0
    staff.quick_pin_locked_until = None
    db.session.commit()
    token = issue_token(staff)
    return jsonify(
        token=token,
        staff={
            "id": staff.id,
            "name": staff.name,
            "email": staff.email,
            "role": staff.role,
            "shop_id": staff.shop_id,
        },
    )


@auth_bp.post("/logout")
@login_required
def logout():
    """Invalidate the current token by advancing the staff update timestamp."""
    staff = Staff.query.get(g.staff_id)
    staff.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(ok=True)

@auth_bp.post("/verify-pin")
@login_required
def verify_pin():
    """Verify the short admin unlock PIN on the backend, never in browser storage."""
    from werkzeug.security import check_password_hash
    from datetime import datetime, timezone, timedelta

    if g.staff_role not in ("owner", "admin"):
        return jsonify(error="PIN unlock is only available to administrators."), 403

    staff = Staff.query.get(g.staff_id)
    if not staff or not staff.quick_pin_hash:
        return jsonify(error="No quick unlock PIN is configured.", force_login=True), 409

    now = datetime.now(timezone.utc)
    locked_until = staff.quick_pin_locked_until
    if locked_until and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until and locked_until > now:
        return jsonify(error="The quick PIN is disabled. Please sign in again.", force_login=True), 423

    pin = str((request.get_json(silent=True) or {}).get("pin") or "").strip()
    if not pin.isdigit() or len(pin) != 4:
        return jsonify(error="Enter your 4-digit PIN."), 400

    if not check_password_hash(staff.quick_pin_hash, pin):
        staff.quick_pin_failed_attempts = (staff.quick_pin_failed_attempts or 0) + 1
        if staff.quick_pin_failed_attempts >= 3:
            staff.quick_pin_failed_attempts = 0
            staff.quick_pin_locked_until = now + timedelta(minutes=15)
            db.session.commit()
            return jsonify(error="Three incorrect PIN attempts. Please sign in again.", force_login=True), 423
        db.session.commit()
        remaining = 3 - staff.quick_pin_failed_attempts
        return jsonify(error=f"Incorrect PIN. {remaining} attempt(s) remaining."), 401

    staff.quick_pin_failed_attempts = 0
    staff.quick_pin_locked_until = None
    db.session.commit()
    return jsonify(ok=True)
