"""
Stateless JWT auth. No server-side session storage -- this matters
because it means any number of staff can log in from any number of
devices/shops at once without the server having to track "who is
logged in right now" in memory or a sessions table. Each request
carries its own proof of identity (the token), so concurrent logins
never contend with each other.
"""
import datetime
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from app.extensions import db

ALGORITHM = "HS256"


def _staff_value(staff, key, default=None):
    if isinstance(staff, dict):
        return staff.get(key, default)
    return getattr(staff, key, default)


def _central_staff(staff_id):
    from app.firestore import get_firestore_sync_service

    return get_firestore_sync_service().get_staff(staff_id)


def issue_token(staff) -> str:
    payload = {
        "staff_id": _staff_value(staff, "id"),
        "role": _staff_value(staff, "role"),
        "shop_id": _staff_value(staff, "shop_id"),
        "issued_at_ms": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=7),
        "iat": datetime.datetime.now(datetime.timezone.utc),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm=ALGORITHM)


def decode_token(token: str):
    return jwt.decode(token, current_app.config["JWT_SECRET_KEY"], algorithms=[ALGORITHM])


def _token_issued_after_staff_update(payload, staff) -> bool:
    issued_at_ms = payload.get("issued_at_ms")
    updated_at = _staff_value(staff, "updated_at")
    if issued_at_ms is None or updated_at is None:
        return True
    if isinstance(updated_at, str):
        updated_at = updated_at[:-1] + "+00:00" if updated_at.endswith("Z") else updated_at
        updated_at = datetime.datetime.fromisoformat(updated_at)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
    return issued_at_ms >= int(updated_at.timestamp() * 1000)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify(error="Missing or invalid Authorization header"), 401
        token = auth_header.split(" ", 1)[1]
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify(error="Session expired, please log in again"), 401
        except jwt.InvalidTokenError:
            return jsonify(error="Invalid token"), 401

        if current_app.config.get("GLR_MODE") == "central":
            staff = _central_staff(payload.get("staff_id"))
        else:
            from app.models import Staff
            staff = db.session.get(Staff, payload.get("staff_id"))
        if not staff or not _staff_value(staff, "is_active"):
            return jsonify(error="Account is inactive, please log in again"), 401
        if not _token_issued_after_staff_update(payload, staff):
            return jsonify(error="Session is no longer valid, please log in again"), 401
        if payload.get("role") != _staff_value(staff, "role") or payload.get("shop_id") != _staff_value(staff, "shop_id"):
            return jsonify(error="Authorization changed, please log in again"), 401

        g.staff_id = _staff_value(staff, "id", payload.get("staff_id"))
        g.staff_role = _staff_value(staff, "role")
        g.staff_shop_id = _staff_value(staff, "shop_id")
        return fn(*args, **kwargs)

    return wrapper


def roles_required(*allowed_roles):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if g.staff_role not in allowed_roles:
                return jsonify(error="You don't have permission for this action"), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator