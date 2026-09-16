from flask import Flask, jsonify
from sqlalchemy import inspect, text
from flask_cors import CORS

from app.config import get_config
from app.extensions import db


def _configure_cors(app):
    configured_origins = app.config.get("CORS_ALLOWED_ORIGINS", "")
    origins = [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
    if origins:
        CORS(app, origins=origins)
    elif app.config.get("GLR_MODE") == "central":
        CORS(app, origins=[])
    else:
        CORS(app, origins="*")


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    config = get_config()
    app.config.from_object(config)

    is_local = app.config.get("GLR_MODE") == "local"
    if is_local:
        db.init_app(app)

    # The frontend (running as a Tauri desktop webview, or a plain browser
    # tab) talks to this server over plain HTTP, exactly like any other
    # web client -- so it needs normal CORS headers, same as any API that
    # serves a separate frontend origin.
    _configure_cors(app)

    from app import models  # noqa: F401  (ensures models are registered)

    with app.app_context():
        if is_local:
            db.create_all()
            _run_compat_migrations()
            if app.config.get("BOOTSTRAP_INITIAL_LOCAL_DATA"):
                from app.bootstrap import ensure_initial_local_data
                ensure_initial_local_data()
            else:
                # Allow one-time credential updates even when create/seed is skipped.
                from app.bootstrap import apply_one_time_credential_bootstrap
                apply_one_time_credential_bootstrap()
        else:
            from app.firestore import get_firestore_sync_service
            get_firestore_sync_service()
            from app.bootstrap import ensure_initial_central_data
            ensure_initial_central_data()

    @app.get("/api/health")
    def health():
        return jsonify(
            status="ok",
            mode=app.config["GLR_MODE"],
            database=("sqlite" if is_local else "firestore"),
        )

    from app.routes.audit import audit_bp
    from app.routes.auth import auth_bp
    from app.routes.diagnostics import diagnostics_bp
    from app.routes.products import products_bp
    from app.routes.sales import sales_bp
    from app.routes.staff import staff_bp
    from app.routes.shop import shop_bp
    from app.routes.stock import stock_bp
    from app.routes.sync import sync_bp

    app.register_blueprint(audit_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(shop_bp)
    app.register_blueprint(stock_bp)
    app.register_blueprint(sync_bp)

    return app


def _safe_db_label(uri: str) -> str:
    """Never echo credentials back in a health check response."""
    if uri.startswith("sqlite"):
        return "sqlite"
    return uri.split("://")[0]

def _run_compat_migrations():
    """Small non-destructive migration layer for existing SQLite installs."""
    inspector = inspect(db.engine)
    dialect = db.engine.dialect.name
    migrations = {
        "shops": [("logo_data", "TEXT")],
        "sales": [("invoice_number", "VARCHAR(40)"), ("updated_at", "TIMESTAMP")],
        "sale_payments": [("updated_at", "TIMESTAMP")],
        "stock_movements": [("updated_at", "TIMESTAMP")],
        "sync_outbox": [("status", "VARCHAR(20)")],
        "staff": [("quick_pin_hash", "VARCHAR(255)"), ("quick_pin_failed_attempts", "INTEGER"), ("quick_pin_locked_until", "TIMESTAMP")],
        "audit_log": [("details", "TEXT"), ("reason", "VARCHAR(500)")],
    }
    for table, columns in migrations.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in columns:
            if name not in existing:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}'))
                if name == "updated_at":
                    db.session.execute(text(f'UPDATE {table} SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL'))
                if name == "status":
                    db.session.execute(text(f'UPDATE {table} SET status = "pending" WHERE status IS NULL'))
                if name == "quick_pin_failed_attempts":
                    db.session.execute(text(f'UPDATE {table} SET quick_pin_failed_attempts = 0 WHERE quick_pin_failed_attempts IS NULL'))

    # Existing installations do not get model-declared indexes from create_all,
    # so create the performance-critical indexes explicitly and idempotently.
    indexes = (
        ("ix_sales_created_at", "sales", "created_at"),
        ("ix_sales_shop_created", "sales", "shop_id, created_at"),
        ("ix_sales_staff_created", "sales", "staff_id, created_at"),
        ("ix_sales_updated_at", "sales", "updated_at"),
        ("ix_sales_customer_created", "sales", "customer_name, created_at"),
        ("ix_sales_invoice_number", "sales", "invoice_number"),
        ("ix_sale_items_sale_id", "sale_items", "sale_id"),
        ("ix_sale_items_product_id", "sale_items", "product_id"),
        ("ix_sale_payments_sale_id", "sale_payments", "sale_id"),
        ("ix_sale_payments_updated_at", "sale_payments", "updated_at"),
        ("ix_stock_movements_product_id", "stock_movements", "product_id"),
        ("ix_stock_movements_created_at", "stock_movements", "created_at"),
        ("ix_stock_movements_updated_at", "stock_movements", "updated_at"),
        ("ix_products_category", "products", "category"),
        ("ix_products_active_name", "products", "is_active, name"),
        ("ix_products_updated_at", "products", "updated_at"),
        ("ix_shops_updated_at", "shops", "updated_at"),
        ("ix_staff_updated_at", "staff", "updated_at"),
        ("ix_staff_email_active", "staff", "email, is_active"),
        ("ix_system_settings_key", "system_settings", "key"),
        ("ix_audit_created_at", "audit_log", "created_at"),
        ("ix_audit_action_created", "audit_log", "action, created_at"),
    )
    for index_name, table, columns in indexes:
        if table in inspector.get_table_names():
            db.session.execute(text(f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({columns})'))

    db.session.commit()
