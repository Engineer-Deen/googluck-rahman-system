"""Idempotent setup for local and central first-run bootstrap."""

from __future__ import annotations

import hashlib
import json
import os

from werkzeug.security import generate_password_hash

BOOTSTRAP_CREDENTIAL_MARKER_KEY = "bootstrap_credential_update_applied"
_ALLOWED_BOOTSTRAP_ROLES = frozenset({"owner", "admin", "manager", "cashier"})


def _seed_value(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _demo_seed_allowed() -> bool:
    """Demo accounts are opt-in for local development/tests only."""
    return os.environ.get("ALLOW_DEMO_SEED", "false").lower() in {"1", "true", "yes"}


def _resolve_account(role: str, demo_email: str, demo_password: str, demo_name: str):
    """Resolve bootstrap account credentials from environment.

    Production and customer installs must set INITIAL_* values. Demo
    emails/passwords are used only when ALLOW_DEMO_SEED=true.
    """
    role_key = role.upper()
    email = _seed_value(f"INITIAL_{role_key}_EMAIL")
    password = _seed_value(f"INITIAL_{role_key}_PASSWORD")
    name = _seed_value(f"INITIAL_{role_key}_NAME", demo_name)
    if email and password:
        return name, email.lower(), password, role
    if _demo_seed_allowed():
        return demo_name, demo_email, demo_password, role
    return None


def _ensure_shop() -> Shop:
    from app.extensions import db
    from app.models import Shop
    shop_name = _seed_value("INITIAL_SHOP_NAME", "Good Luck Rahman Enterprise")
    shop_location = _seed_value("INITIAL_SHOP_LOCATION", "")
    shop = Shop.query.filter_by(name=shop_name).first()
    if not shop:
        shop = Shop.query.first()
    if not shop:
        shop = Shop(name=shop_name, location=shop_location or None)
        db.session.add(shop)
        db.session.flush()
    else:
        if shop_name and shop.name != shop_name and not Shop.query.filter_by(name=shop_name).first():
            shop.name = shop_name
        if shop_location and not shop.location:
            shop.location = shop_location
    return shop


def _ensure_staff_accounts(shop: Shop, include_owner: bool) -> None:
    from app.extensions import db
    from app.models import Staff
    specs = []
    if include_owner:
        specs.append(_resolve_account("owner", "owner@glr.test", "owner123", "Owner"))
    specs.append(_resolve_account("admin", "admin@glr.test", "admin123", "Admin"))
    specs.append(_resolve_account("cashier", "cashier@glr.test", "cashier123", "Cashier"))

    for resolved in specs:
        if not resolved:
            continue
        name, email, password, role = resolved
        staff = Staff.query.filter_by(email=email).first()
        if staff:
            if not staff.shop_id:
                staff.shop_id = shop.id
            continue
        db.session.add(
            Staff(
                shop_id=shop.id,
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
            )
        )


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_credential_marker() -> dict:
    from app.extensions import db
    from app.models import SystemSetting
    row = SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).first()
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_credential_bootstrap_once(token_fp: str, target_email: str, new_password: str) -> dict:
    """Mutate the target staff row and durable marker in one transaction."""
    from app.extensions import db
    from app.models import Staff, SystemSetting
    marker = _read_credential_marker()
    if marker.get("token_fp") == token_fp:
        return {"applied": False, "reason": "already_applied"}

    staff = Staff.query.filter_by(email=target_email).first()
    if not staff:
        return {"applied": False, "reason": "target_not_found"}

    new_email = _seed_value("BOOTSTRAP_NEW_EMAIL").lower()
    if new_email and new_email != target_email:
        if Staff.query.filter_by(email=new_email).first():
            return {"applied": False, "reason": "new_email_taken"}
        staff.email = new_email

    new_name = _seed_value("BOOTSTRAP_NEW_NAME")
    if new_name:
        staff.name = new_name

    new_role = _seed_value("BOOTSTRAP_NEW_ROLE").lower()
    if new_role:
        if new_role not in _ALLOWED_BOOTSTRAP_ROLES:
            return {"applied": False, "reason": "invalid_role"}
        staff.role = new_role

    staff.password_hash = generate_password_hash(new_password)

    row = SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).first()
    if not row:
        row = SystemSetting(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY)
        db.session.add(row)
    row.value = json.dumps(
        {
            "token_fp": token_fp,
            "staff_id": staff.id,
            "email": staff.email,
        }
    )
    # Credential changes and the applied marker commit together so a failed
    # marker insert cannot leave updated credentials without a durable marker.
    db.session.commit()
    return {"applied": True, "staff_id": staff.id, "email": staff.email}


def apply_one_time_credential_bootstrap() -> dict:
    """Apply a one-shot owner/admin credential update when configured.

    Requires BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN, BOOTSTRAP_TARGET_EMAIL, and
    BOOTSTRAP_NEW_PASSWORD. After a successful apply, the token fingerprint is
    stored in system_settings so later restarts with the same token are no-ops.
    Missing or incomplete configuration is ignored safely.
    """
    token = _seed_value("BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN")
    target_email = _seed_value("BOOTSTRAP_TARGET_EMAIL").lower()
    new_password = _seed_value("BOOTSTRAP_NEW_PASSWORD")
    if not token or not target_email or not new_password:
        return {"applied": False, "reason": "missing_config"}

    token_fp = _token_fingerprint(token)

    if os.environ.get("GLR_MODE", "local") == "central":
        from app.firestore import get_firestore_sync_service
        service = get_firestore_sync_service()
        marker = service.get_setting(BOOTSTRAP_CREDENTIAL_MARKER_KEY, {})
        if isinstance(marker, str):
            try:
                marker = json.loads(marker)
            except (TypeError, ValueError):
                marker = {}
        if isinstance(marker, dict) and marker.get("token_fp") == token_fp:
            return {"applied": False, "reason": "already_applied"}
        staff = service.get_staff_by_email(target_email)
        if not staff:
            return {"applied": False, "reason": "target_not_found"}
        new_email = _seed_value("BOOTSTRAP_NEW_EMAIL").lower()
        if new_email and new_email != target_email and service.staff_email_exists(new_email, excluding_id=staff.get("id")):
            return {"applied": False, "reason": "new_email_taken"}
        updates = {"password_hash": generate_password_hash(new_password)}
        if new_email and new_email != target_email:
            updates["email"] = new_email
        new_name = _seed_value("BOOTSTRAP_NEW_NAME")
        if new_name:
            updates["name"] = new_name
        new_role = _seed_value("BOOTSTRAP_NEW_ROLE").lower()
        if new_role:
            if new_role not in _ALLOWED_BOOTSTRAP_ROLES:
                return {"applied": False, "reason": "invalid_role"}
            updates["role"] = new_role
        updated = service.save_staff(staff["id"], **updates)
        service.save_setting(BOOTSTRAP_CREDENTIAL_MARKER_KEY, json.dumps({"token_fp": token_fp, "staff_id": updated["id"], "email": updated.get("email")}))
        return {"applied": True, "staff_id": updated["id"], "email": updated.get("email")}

    from app.extensions import db

    try:
        return _apply_credential_bootstrap_once(token_fp, target_email, new_password)
    except Exception:
        db.session.rollback()
        raise


def ensure_initial_local_data() -> None:
    """Create the initial local accounts only when their rows are absent.

    This is intentionally limited to the packaged local first-run path. It
    never updates an existing account, so a user's password and local data are
    preserved on every subsequent startup.

    Customer installs should set INITIAL_ADMIN_EMAIL / INITIAL_ADMIN_PASSWORD
    (and optional cashier values). Demo accounts are created only when
    ALLOW_DEMO_SEED=true.
    """
    from app.extensions import db
    shop = _ensure_shop()
    _ensure_staff_accounts(shop, include_owner=False)
    db.session.commit()
    apply_one_time_credential_bootstrap()


def ensure_initial_central_data() -> None:
    """Create a safe initial central shop and accounts in Firestore."""
    from app.firestore import get_firestore_sync_service
    service = get_firestore_sync_service()
    shop_name = _seed_value("INITIAL_SHOP_NAME", "Good Luck Rahman Enterprise")
    shop = service.get_first_shop()
    if not shop:
        shop = service.save_shop(1, name=shop_name, location=_seed_value("INITIAL_SHOP_LOCATION") or None)
    elif shop_name and shop.get("name") != shop_name:
        shop = service.save_shop(shop["id"], name=shop_name, location=shop.get("location"))

    specs = [
        _resolve_account("owner", "owner@glr.test", "owner123", "Owner"),
        _resolve_account("admin", "admin@glr.test", "admin123", "Admin"),
        _resolve_account("cashier", "cashier@glr.test", "cashier123", "Cashier"),
    ]
    for resolved in specs:
        if not resolved:
            continue
        name, email, password, role = resolved
        existing = service.get_staff_by_email(email)
        if existing:
            if not existing.get("shop_id"):
                service.save_staff(existing["id"], shop_id=shop["id"])
            continue
        service.save_staff(service.allocate_staff_id(), shop_id=shop["id"], name=name, email=email, password_hash=generate_password_hash(password), role=role, is_active=True, quick_pin_failed_attempts=0)

    product_sku = _seed_value("INITIAL_PRODUCT_SKU")
    if not product_sku and _demo_seed_allowed():
        product_sku = "RICE-50KG"
    if product_sku and not any(product.get("sku") == product_sku for product in service.list_products(include_inactive=True)):
        product_id = service._allocate_product_id()
        service.save_product(product_id, sku=product_sku, name=_seed_value("INITIAL_PRODUCT_NAME", "Bag of Rice (50kg)"), category=_seed_value("INITIAL_PRODUCT_CATEGORY", "Grains"), unit_price=_seed_value("INITIAL_PRODUCT_UNIT_PRICE", "450"), cost_price=_seed_value("INITIAL_PRODUCT_COST_PRICE", "350"), is_active=True, shop_ids=[shop["id"]])
    if service.get_setting("admin_timeout_minutes") is None:
        service.save_setting("admin_timeout_minutes", "15")
    if service.get_setting("admin_full_login_hours") is None:
        service.save_setting("admin_full_login_hours", "8")
    apply_one_time_credential_bootstrap()
