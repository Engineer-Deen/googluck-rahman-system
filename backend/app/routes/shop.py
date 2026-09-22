from flask import Blueprint, current_app, g, jsonify, request
from datetime import datetime, timezone
import json

from app.auth import login_required, roles_required
from app.audit import log_action
from app.central_proxy import forward_to_central
from app.extensions import db
from app.models import Shop, Staff, SystemSetting

shop_bp = Blueprint("shop", __name__, url_prefix="/api/shop")

DEFAULT_SETTINGS = {
    "timeout_minutes": 15,
    "full_login_hours": 8,
}

SHOP_OFFLINE_MESSAGE = "Shop name and logo can only be saved while online. Connect to the internet and try again."
SETTINGS_OFFLINE_MESSAGE = "These system settings can only be saved while online. Connect to the internet and try again."


def _get_setting(key, default=None):
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        value = get_firestore_sync_service().get_setting(key, default)
        if value is None:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    row = SystemSetting.query.filter_by(key=key).first()
    if not row or row.value is None:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return row.value


def _set_setting(key, value):
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        get_firestore_sync_service().save_setting(key, json.dumps(value))
        return
    row = SystemSetting.query.filter_by(key=key).first()
    if not row:
        row = SystemSetting(key=key)
        db.session.add(row)
    row.value = json.dumps(value)



def _mirror_shop_locally(body):
    """Reflect the shop details central just saved in this PC's own copy."""
    try:
        shop = db.session.get(Shop, body.get("id")) if body.get("id") else Shop.query.first()
        if shop:
            shop.name = body.get("name", shop.name)
            shop.location = body.get("location", shop.location)
            db.session.commit()
    except Exception:  # local cache only -- central already succeeded
        db.session.rollback()


def _mirror_settings_locally(body, sent):
    """
    Settings are not part of the sync pull, and the quick-unlock PIN hash is
    checked locally by /api/auth/verify-pin, so after central accepts a save
    this PC records the same values for itself.
    """
    try:
        _set_setting("admin_timeout_minutes", int(body["timeout_minutes"]))
        _set_setting("admin_full_login_hours", int(body["full_login_hours"]))
        pin = str(sent.get("pin") or "").strip()
        if pin:
            from werkzeug.security import generate_password_hash
            staff = db.session.get(Staff, g.staff_id)
            if staff:
                staff.quick_pin_hash = generate_password_hash(pin)
                staff.quick_pin_failed_attempts = 0
                staff.quick_pin_locked_until = None
        db.session.commit()
    except Exception:  # local cache only -- central already succeeded
        db.session.rollback()


@shop_bp.get("/public")
def get_public_shop():
    """Minimal branding endpoint used before login so the login screen can show the logo."""
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        shop = get_firestore_sync_service().get_first_shop()
        return jsonify(name=(shop or {}).get("name", "Good Luck Rahman Enterprise"))
    shop = Shop.query.first()
    return jsonify(
        name=shop.name if shop else "Good Luck Rahman Enterprise",
    )

@shop_bp.get("")
@login_required
def get_shop():
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        shop = get_firestore_sync_service().get_shop(g.staff_shop_id) if g.staff_shop_id else get_firestore_sync_service().get_first_shop()
        if not shop:
            return jsonify(id=None, name="Good Luck Rahman Enterprise", location=None, logo_data=None, central_only=True)
        return jsonify(id=shop.get("id"), name=shop.get("name"), location=shop.get("location"), central_only=True, mode="central")
    shop = db.session.get(Shop, g.staff_shop_id) if g.staff_shop_id else Shop.query.first()
    if not shop:
        return jsonify(id=None, name="Good Luck Rahman Enterprise", location=None, logo_data=None, central_only=True)
    return jsonify(
        id=shop.id,
        name=shop.name,
        location=shop.location,
        central_only=True,
        mode=current_app.config["GLR_MODE"],
    )


@shop_bp.put("")
@roles_required("owner", "admin")
def update_shop():
    if current_app.config["GLR_MODE"] != "central":
        body, response = forward_to_central("PUT", "/api/shop", SHOP_OFFLINE_MESSAGE)
        if body is not None:
            _mirror_shop_locally(body)
        return response
    service = None
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        shop = service.get_shop(g.staff_shop_id) if g.staff_shop_id else service.get_first_shop()
    else:
        shop = db.session.get(Shop, g.staff_shop_id) if g.staff_shop_id else Shop.query.first()
    if not shop:
        return jsonify(error="Shop not found"), 404
    data = request.get_json(silent=True) or {}
    before = {"name": shop.get("name") if service else shop.name, "location": shop.get("location") if service else shop.location}
    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify(error="Shop name cannot be empty"), 400
        if service:
            shop["name"] = name
        else:
            shop.name = name
    if "location" in data:
        if service:
            shop["location"] = data["location"]
        else:
            shop.location = data["location"]
    if service:
        shop = service.save_shop(shop["id"], name=shop.get("name"), location=shop.get("location"), logo_data=shop.get("logo_data"))
        service.write_audit(f"shop-settings-{g.staff_id}-{datetime.now(timezone.utc).timestamp()}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="shop_settings_updated", entity_type="shop", entity_id=str(shop["id"]), details={"before": before, "after": {"name": shop.get("name"), "location": shop.get("location")}})
        return jsonify(id=shop.get("id"), name=shop.get("name"), location=shop.get("location"), mode="central")
    db.session.commit()
    actor = db.session.get(Staff, g.staff_id)
    log_action(g.staff_id, actor.name if actor else None, g.staff_role, "shop_settings_updated", "shop", shop.id, {"before": before, "after": {"name": shop.name, "location": shop.location}})
    return jsonify(id=shop.id, name=shop.name, location=shop.location, mode="central")


@shop_bp.get("/settings")
@roles_required("owner", "admin")
def get_system_settings():
    if current_app.config.get("GLR_MODE") == "central":
        from app.firestore import get_firestore_sync_service
        staff = get_firestore_sync_service().get_staff(g.staff_id)
        return jsonify(timeout_minutes=int(_get_setting("admin_timeout_minutes", DEFAULT_SETTINGS["timeout_minutes"])), full_login_hours=int(_get_setting("admin_full_login_hours", DEFAULT_SETTINGS["full_login_hours"])), pin_configured=bool((staff or {}).get("quick_pin_hash")), mode="central")
    return jsonify(
        timeout_minutes=int(_get_setting("admin_timeout_minutes", DEFAULT_SETTINGS["timeout_minutes"])),
        full_login_hours=int(_get_setting("admin_full_login_hours", DEFAULT_SETTINGS["full_login_hours"])),
        pin_configured=bool(g.staff_id and db.session.get(Staff, g.staff_id).quick_pin_hash),
        mode=current_app.config["GLR_MODE"],
    )


@shop_bp.put("/settings")
@roles_required("owner", "admin")
def save_system_settings():
    if current_app.config["GLR_MODE"] != "central":
        body, response = forward_to_central("PUT", "/api/shop/settings", SETTINGS_OFFLINE_MESSAGE)
        if body is not None:
            _mirror_settings_locally(body, request.get_json(silent=True) or {})
        return response

    data = request.get_json(silent=True) or {}
    try:
        timeout_minutes = int(data.get("timeout_minutes", DEFAULT_SETTINGS["timeout_minutes"]))
        full_login_hours = int(data.get("full_login_hours", DEFAULT_SETTINGS["full_login_hours"]))
    except (TypeError, ValueError):
        return jsonify(error="Session settings must be valid numbers."), 400

    if timeout_minutes not in (5, 10, 15, 30, 60):
        return jsonify(error="Invalid inactivity timeout."), 400
    if full_login_hours not in (2, 4, 8, 12):
        return jsonify(error="Invalid maximum session period."), 400

    _set_setting("admin_timeout_minutes", timeout_minutes)
    _set_setting("admin_full_login_hours", full_login_hours)

    pin = str(data.get("pin") or "").strip()
    if pin:
        if not pin.isdigit() or len(pin) != 4:
            return jsonify(error="The quick unlock PIN must be exactly 4 digits."), 400
        from werkzeug.security import generate_password_hash
        if current_app.config.get("GLR_MODE") == "central":
            from app.firestore import get_firestore_sync_service
            service = get_firestore_sync_service()
            service.update_staff_auth_state(g.staff_id, quick_pin_hash=generate_password_hash(pin), quick_pin_failed_attempts=0, quick_pin_locked_until=None, updated_at=datetime.now(timezone.utc))
        else:
            staff = db.session.get(Staff, g.staff_id)
            staff.quick_pin_hash = generate_password_hash(pin)
            staff.quick_pin_failed_attempts = 0
            staff.quick_pin_locked_until = None

    if current_app.config.get("GLR_MODE") == "central":
        service = service if "service" in locals() else __import__("app.firestore", fromlist=["get_firestore_sync_service"]).get_firestore_sync_service()
        service.write_audit(f"system-settings-{g.staff_id}-{datetime.now(timezone.utc).timestamp()}", actor_staff_id=g.staff_id, actor_role=g.staff_role, action="system_settings_updated", entity_type="system", entity_id=str(g.staff_id), details={"timeout_minutes": timeout_minutes, "full_login_hours": full_login_hours, "pin_changed": bool(pin)})
        staff = service.get_staff(g.staff_id)
        return jsonify(timeout_minutes=timeout_minutes, full_login_hours=full_login_hours, pin_configured=bool((staff or {}).get("quick_pin_hash")), mode="central")
    db.session.commit()
    actor = db.session.get(Staff, g.staff_id)
    log_action(g.staff_id, actor.name if actor else None, g.staff_role, "system_settings_updated", "system", g.staff_id, {"timeout_minutes": timeout_minutes, "full_login_hours": full_login_hours, "pin_changed": bool(pin)})
    return jsonify(
        timeout_minutes=timeout_minutes,
        full_login_hours=full_login_hours,
        pin_configured=bool(staff.quick_pin_hash) if pin else bool(db.session.get(Staff, g.staff_id).quick_pin_hash),
        mode="central",
    )
