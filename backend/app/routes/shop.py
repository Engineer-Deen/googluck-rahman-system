from flask import Blueprint, current_app, g, jsonify, request
from datetime import datetime, timezone
import json

from app.auth import login_required, roles_required
from app.audit import log_action
from app.extensions import db
from app.models import Shop, Staff, SystemSetting

shop_bp = Blueprint("shop", __name__, url_prefix="/api/shop")

DEFAULT_SETTINGS = {
    "timeout_minutes": 15,
    "full_login_hours": 8,
}


def _get_setting(key, default=None):
    row = SystemSetting.query.filter_by(key=key).first()
    if not row or row.value is None:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return row.value


def _set_setting(key, value):
    row = SystemSetting.query.filter_by(key=key).first()
    if not row:
        row = SystemSetting(key=key)
        db.session.add(row)
    row.value = json.dumps(value)



@shop_bp.get("/public")
def get_public_shop():
    """Minimal branding endpoint used before login so the login screen can show the logo."""
    shop = Shop.query.first()
    return jsonify(
        name=shop.name if shop else "Good Luck Rahman Enterprise",
        logo_data=shop.logo_data if shop else None,
    )

@shop_bp.get("")
@login_required
def get_shop():
    shop = Shop.query.get(g.staff_shop_id) if g.staff_shop_id else Shop.query.first()
    if not shop:
        return jsonify(id=None, name="Good Luck Rahman Enterprise", location=None, logo_data=None, central_only=True)
    return jsonify(
        id=shop.id,
        name=shop.name,
        location=shop.location,
        logo_data=shop.logo_data,
        central_only=True,
        mode=current_app.config["GLR_MODE"],
    )


@shop_bp.put("")
@roles_required("owner", "admin")
def update_shop():
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(error="Shop branding can only be saved on the central server. Connect to the internet and try again."), 403
    shop = Shop.query.get(g.staff_shop_id) if g.staff_shop_id else Shop.query.first()
    if not shop:
        return jsonify(error="Shop not found"), 404
    data = request.get_json(silent=True) or {}
    before = {"name": shop.name, "location": shop.location, "has_logo": bool(shop.logo_data)}
    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify(error="Shop name cannot be empty"), 400
        shop.name = name
    if "location" in data:
        shop.location = data["location"]
    if "logo_data" in data:
        logo = data["logo_data"]
        if logo and (not isinstance(logo, str) or len(logo) > 2_500_000):
            return jsonify(error="Logo is too large. Please choose an image smaller than about 2 MB."), 400
        shop.logo_data = logo
    db.session.commit()
    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "shop_settings_updated", "shop", shop.id,
        {"before": before, "after": {"name": shop.name, "location": shop.location, "has_logo": bool(shop.logo_data)}},
    )
    return jsonify(id=shop.id, name=shop.name, location=shop.location, logo_data=shop.logo_data, mode="central")


@shop_bp.get("/settings")
@roles_required("owner", "admin")
def get_system_settings():
    return jsonify(
        timeout_minutes=int(_get_setting("admin_timeout_minutes", DEFAULT_SETTINGS["timeout_minutes"])),
        full_login_hours=int(_get_setting("admin_full_login_hours", DEFAULT_SETTINGS["full_login_hours"])),
        pin_configured=bool(g.staff_id and Staff.query.get(g.staff_id).quick_pin_hash),
        mode=current_app.config["GLR_MODE"],
    )


@shop_bp.put("/settings")
@roles_required("owner", "admin")
def save_system_settings():
    if current_app.config["GLR_MODE"] != "central":
        return jsonify(error="System settings can only be saved on the central server. Connect to the internet and try again."), 403

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
        staff = Staff.query.get(g.staff_id)
        staff.quick_pin_hash = generate_password_hash(pin)
        staff.quick_pin_failed_attempts = 0
        staff.quick_pin_locked_until = None

    db.session.commit()
    actor = Staff.query.get(g.staff_id)
    log_action(
        g.staff_id, actor.name if actor else None, g.staff_role,
        "system_settings_updated", "system", g.staff_id,
        {"timeout_minutes": timeout_minutes, "full_login_hours": full_login_hours, "pin_changed": bool(pin)},
    )
    return jsonify(
        timeout_minutes=timeout_minutes,
        full_login_hours=full_login_hours,
        pin_configured=bool(staff.quick_pin_hash) if pin else bool(Staff.query.get(g.staff_id).quick_pin_hash),
        mode="central",
    )
