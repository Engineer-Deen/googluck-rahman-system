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
import uuid

from app.audit import log_action
from app.auth import roles_required
from app.extensions import db
from app.models import Shop, Staff

staff_bp = Blueprint("staff", __name__, url_prefix="/api/staff")

ASSIGNABLE_ROLES = ("cashier", "manager")
ADMIN_ROLES = ("admin",)


def _require_central_mode():
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(
            error=(
                "Staff accounts can only be added or changed while this shop "
                "computer is online. Connect to the internet, make the change, "
                "and it will sync to your other devices automatically."
            )
        ), 403
    return None


def serialize_staff(s: Staff):
    if isinstance(s, dict):
        return {
            "id": s.get("id"), "shop_id": s.get("shop_id"), "name": s.get("name"),
            "email": s.get("email"), "role": s.get("role"), "is_active": s.get("is_active", True),
        }
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
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        return jsonify([serialize_staff(staff) for staff in get_firestore_sync_service().list_staff()])
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
    service = None
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        if service.staff_email_exists(email):
            return jsonify(error=f"An account with email '{email}' already exists"), 409
    elif Staff.query.filter_by(email=email).first():
        return jsonify(error=f"An account with email '{email}' already exists"), 409

    requested_shop_id = data.get("shop_id", g.staff_shop_id)
    try:
        requested_shop_id = int(requested_shop_id) if requested_shop_id is not None else None
    except (TypeError, ValueError):
        return jsonify(error="shop_id must be a valid shop id"), 400
    if service:
        shop_exists = service.get_shop(requested_shop_id) if requested_shop_id else None
    else:
        shop_exists = db.session.get(Shop, requested_shop_id) if requested_shop_id else None
    if not requested_shop_id or not shop_exists:
        return jsonify(error="The selected shop does not exist"), 400
    if g.staff_role != "owner" and requested_shop_id != g.staff_shop_id:
        return jsonify(error="Administrators can only create staff for their own shop"), 403

    if service:
        staff = service.save_staff(service.allocate_staff_id(), shop_id=requested_shop_id, name=name, email=email, password_hash=generate_password_hash(password), role=role, is_active=True, quick_pin_failed_attempts=0)
        service.write_audit(f"staff-created-{staff['id']}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="staff_created", entity_type="staff", entity_id=str(staff["id"]), details={"name": name, "email": email, "role": role})
        return jsonify(serialize_staff(staff)), 201
    staff = Staff(shop_id=requested_shop_id, name=name, email=email, password_hash=generate_password_hash(password), role=role, is_active=True)
    db.session.add(staff)
    db.session.commit()

    actor = db.session.get(Staff, g.staff_id)
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
    from app.firestore import get_firestore_sync_service
    service = get_firestore_sync_service() if current_app.config.get("GLR_MODE") == "central" else None
    if (service and service.staff_email_exists(email)) or (not service and Staff.query.filter_by(email=email).first()):
        return jsonify(error=f"An account with email '{email}' already exists"), 409
    if service:
        admin = service.save_staff(service.allocate_staff_id(), shop_id=g.staff_shop_id, name=name, email=email, password_hash=generate_password_hash(password), role="admin", is_active=True, quick_pin_failed_attempts=0)
        service.write_audit(f"admin-created-{admin['id']}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="admin_account_created", entity_type="staff", entity_id=str(admin["id"]), details={"name": name, "email": email})
        return jsonify(serialize_staff(admin)), 201
    admin = Staff(
        shop_id=g.staff_shop_id, name=name, email=email,
        password_hash=generate_password_hash(password), role="admin", is_active=True
    )
    db.session.add(admin)
    db.session.commit()
    actor = db.session.get(Staff, g.staff_id)
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

    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        staff = service.get_staff(staff_id)
        if not staff:
            return jsonify(error="Staff member not found"), 404
        if staff.get("role") in ("owner", "admin"):
            return jsonify(error="Owner/admin accounts can't be managed through this endpoint"), 403
        data = request.get_json(silent=True) or {}
        updates = {}
        if "name" in data:
            name = (data["name"] or "").strip()
            if not name:
                return jsonify(error="name cannot be empty"), 400
            updates["name"] = name
        if "role" in data:
            if data["role"] not in ASSIGNABLE_ROLES:
                return jsonify(error=f"role must be one of {ASSIGNABLE_ROLES}"), 400
            updates["role"] = data["role"]
        if "is_active" in data:
            updates["is_active"] = bool(data["is_active"])
        updated = service.save_staff(staff_id, **updates)
        service.write_audit(f"staff-updated-{staff_id}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="staff_updated", entity_type="staff", entity_id=str(staff_id), details={**data, "target_staff_id": staff_id, "target_name": updated.get("name")})
        return jsonify(serialize_staff(updated))
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

    actor = db.session.get(Staff, g.staff_id)
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

    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        staff = service.get_staff(staff_id)
        if not staff:
            return jsonify(error="Staff member not found"), 404
        if staff.get("role") in ("owner", "admin"):
            return jsonify(error="Owner/admin accounts can't be managed through this endpoint"), 403
        data = request.get_json(silent=True) or {}
        new_password = data.get("new_password") or ""
        if len(new_password) < 6:
            return jsonify(error="password must be at least 6 characters"), 400
        updated = service.save_staff(staff_id, password_hash=generate_password_hash(new_password))
        service.write_audit(f"staff-password-reset-{staff_id}-{uuid.uuid4().hex}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="staff_password_reset", entity_type="staff", entity_id=str(staff_id), details={"target_email": staff.get("email")})
        return jsonify(serialize_staff(updated))
    staff = Staff.query.get_or_404(staff_id)
    if staff.role in ("owner", "admin"):
        return jsonify(error="Owner/admin accounts can't be managed through this endpoint"), 403

    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 6:
        return jsonify(error="password must be at least 6 characters"), 400

    staff.password_hash = generate_password_hash(new_password)
    db.session.commit()

    actor = db.session.get(Staff, g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "staff_password_reset", "staff", staff.id, {"target_email": staff.email},
    )

    return jsonify(serialize_staff(staff))