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

from app.models import Staff

ALGORITHM = "HS256"


def issue_token(staff: Staff) -> str:
    payload = {
        "staff_id": staff.id,
        "role": staff.role,
        "shop_id": staff.shop_id,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=7),
        "iat": datetime.datetime.now(datetime.timezone.utc),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm=ALGORITHM)


def decode_token(token: str):
    return jwt.decode(token, current_app.config["JWT_SECRET_KEY"], algorithms=[ALGORITHM])


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

        g.staff_id = payload["staff_id"]
        g.staff_role = payload["role"]
        g.staff_shop_id = payload["shop_id"]
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