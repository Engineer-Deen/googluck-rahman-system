"""
Staff accounts are reference data, same category as products (see
models/core.py for the full reasoning): only ever created or edited
CENTRALLY, then pulled down to every device automatically by the
existing sync worker. A new seller account, a password reset, or a
deactivation all just show up on every device within the normal pull
interval -- nothing extra needed for that part.

Only "owner" and "admin" roles can manage staff (not "manager" -- the
old system's structure was specifically "the shop owner manages
sellers", so this is scoped tighter than the general FINANCE_ROLES
used for viewing profit).

Creating a new owner/admin account through this endpoint is
deliberately not allowed -- only "cashier" and "manager" can be
assigned here. That's a safety boundary: a compromised staff session
should never be able to mint itself a rival owner account. Additional
owner/admin accounts are a deploy-time decision (seed.py or direct
database access), not a runtime one.
"""
from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.security import generate_password_hash

from app.audit import log_action
from app.auth import roles_required
from app.extensions import db
from app.models import Staff

staff_bp = Blueprint("staff", __name__, url_prefix="/api/staff")

ASSIGNABLE_ROLES = ("cashier", "manager")
ADMIN_ROLES = ("admin",)


def _require_central_mode():
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(
            error=(
                "Staff accounts can only be created or edited on the central "
                "server (while online), then they sync down to every device "
                "automatically."
            )
        ), 403
    return None


def serialize_staff(s: Staff):
    return {
        "id": s.id,
        "shop_id": s.shop_id,
        "name": s.name,
        "email": s.email,
        "role": s.role,
        "is_active": s.is_active,
    }


@staff_bp.get("")
@roles_required("owner", "admin")
def list_staff():
    staff = Staff.query.order_by(Staff.name).all()
    return jsonify([serialize_staff(s) for s in staff])


@staff_bp.post("")
@roles_required("owner", "admin")
def create_staff():
    blocked = _require_central_mode()
    if blocked:
        return blocked

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = data.get("role", "cashier")

    if not name or not email or not password:
        return jsonify(error="name, email, and password are required"), 400
    if role not in ASSIGNABLE_ROLES:
        return jsonify(
            error=f"role must be one of {ASSIGNABLE_ROLES} (owner/admin accounts aren't created here)"
        ), 400
    if len(password) < 6:
        return jsonify(error="password must be at least 6 characters"), 400
    if Staff.query.filter_by(email=email).first():
        return jsonify(error=f"An account with email '{email}' already exists"), 409

    staff = Staff(
        shop_id=data.get("shop_id", g.staff_shop_id),
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        is_active=True,
    )
    db.session.add(staff)
    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "staff_created", "staff", staff.id, {"name": staff.name, "email": staff.email, "role": staff.role},
    )

    return jsonify(serialize_staff(staff)), 201


@staff_bp.post("/admins")
@roles_required("owner")
def create_admin():
    """Owner-only creation of an administrator account."""
    blocked = _require_central_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not name or not email or not password:
        return jsonify(error="Name, email, and password are required"), 400
    if len(password) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    if Staff.query.filter_by(email=email).first():
        return jsonify(error=f"An account with email '{email}' already exists"), 409
    admin = Staff(
        shop_id=g.staff_shop_id, name=name, email=email,
        password_hash=generate_password_hash(password), role="admin", is_active=True
    )
    db.session.add(admin)
    db.session.commit()
    actor = Staff.query.get(g.staff_id)
    log_action(g.staff_id, actor.name if actor else None, g.staff_role,
                "admin_account_created", "staff", admin.id,
                {"name": admin.name, "email": admin.email})
    return jsonify(serialize_staff(admin)), 201


@staff_bp.put("/<int:staff_id>")
@roles_required("owner", "admin")
def update_staff(staff_id):
    blocked = _require_central_mode()
    if blocked:
        return blocked

    staff = Staff.query.get_or_404(staff_id)

    # Never let this endpoint touch an owner/admin account -- same
    # boundary as create: managing the people who can manage sellers
    # is not something this endpoint does.
    if staff.role in ("owner", "admin"):
        return jsonify(error="Owner/admin accounts can't be managed through this endpoint"), 403

    data = request.get_json(silent=True) or {}

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify(error="name cannot be empty"), 400
        staff.name = name
    if "role" in data:
        if data["role"] not in ASSIGNABLE_ROLES:
            return jsonify(error=f"role must be one of {ASSIGNABLE_ROLES}"), 400
        staff.role = data["role"]
    if "is_active" in data:
        staff.is_active = bool(data["is_active"])

    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "staff_updated", "staff", staff.id, {**data, "target_staff_id": staff.id, "target_name": staff.name},
    )

    return jsonify(serialize_staff(staff))


@staff_bp.post("/<int:staff_id>/reset-password")
@roles_required("owner", "admin")
def reset_password(staff_id):
    blocked = _require_central_mode()
    if blocked:
        return blocked

    staff = Staff.query.get_or_404(staff_id)
    if staff.role in ("owner", "admin"):
        return jsonify(error="Owner/admin accounts can't be managed through this endpoint"), 403

    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 6:
        return jsonify(error="password must be at least 6 characters"), 400

    staff.password_hash = generate_password_hash(new_password)
    db.session.commit()

    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "staff_password_reset", "staff", staff.id, {"target_email": staff.email},
    )

    return jsonify(serialize_staff(staff))