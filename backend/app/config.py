"""
Configuration for the Good Luck Rahman backend.

The SAME Flask app runs in two modes, controlled by the GLR_MODE
environment variable:

  GLR_MODE=local    -> runs on each shop's PC (via Tauri), uses SQLite.
                        This is the offline-first local database.
  GLR_MODE=central  -> runs on the cloud server, uses PostgreSQL.
                        This is the single source of truth for the
                        whole business, fed by every device's sync push.

Nothing else in the app needs to know which mode it's in -- routes and
models are written against SQLAlchemy, which works the same either way.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _local_instance_dir() -> Path:
    """Return the writable directory for local SQLite and device state."""
    if not IS_FROZEN:
        # Keep direct source execution convenient for development.
        return BASE_DIR / "instance"

    # PyInstaller one-file executables extract modules into a temporary
    # _MEI* directory. Storing mutable data relative to __file__ would make
    # the SQLite database and device identity disappear on every restart.
    # LOCALAPPDATA is a stable, per-user writable location on Windows.
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "Good Luck Rahman Enterprise"


INSTANCE_DIR = _local_instance_dir()


def server_host(mode):
    return "127.0.0.1" if mode == "local" else "0.0.0.0"


class BaseConfig:
    GLR_MODE = os.environ.get("GLR_MODE", "local")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # This device's own identity when running in local mode. Each shop PC
    # gets a unique device id the first time it starts up (see device.py).
    DEVICE_ID_FILE = INSTANCE_DIR / "device_id.txt"

    # Where the central server lives, so local devices know where to push
    # their outbox. Can be a cloud URL or a LAN address for a shop server.
    CENTRAL_SYNC_URL = os.environ.get("CENTRAL_SYNC_URL", "http://localhost:8000")

    # Shared secret local devices send when pushing to the central
    # server's /api/sync/push endpoint. Must match on both sides.
    SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "dev-sync-key-change-me")

    # Firebase SQL Connect preparation. These are intentionally optional:
    # the Flask/PostgreSQL service remains authoritative until the Firebase
    # migration is explicitly enabled.
    FIREBASE_SQL_CONNECT_ENABLED = os.environ.get("FIREBASE_SQL_CONNECT_ENABLED", "false").lower() == "true"
    FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
    FIREBASE_SQL_CONNECT_SERVICE_ID = os.environ.get("FIREBASE_SQL_CONNECT_SERVICE_ID", "goodluck-rahman-sql")


class LocalConfig(BaseConfig):
    """
    Runs on a shop's PC. SQLite file lives next to the app.

    Deliberately does NOT read the generic DATABASE_URL variable here.
    DATABASE_URL is meant for CentralConfig (Postgres) only. If both
    variables shared the same name, any stray DATABASE_URL left over in
    the environment (from a previous project, a system-wide setting, an
    IDE auto-loading .env, etc.) could silently make "local" mode try to
    connect to Postgres -- which is exactly what must never happen on an
    offline shop PC. LOCAL_DATABASE_URL is a distinct, deliberate override
    for local mode only, and it is optional.
    """
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Used by create_app() to provision only a brand-new frozen local install.
    BOOTSTRAP_INITIAL_LOCAL_DATA = IS_FROZEN
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "LOCAL_DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'glr_local.sqlite'}"
    )


class CentralConfig(BaseConfig):
    """Runs on the cloud server. Postgres via Cloud SQL or self-hosted."""
    _default_pg = "postgresql+psycopg2://glr_user:glr_pass@localhost:5432/glr_central"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", _default_pg)
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "20")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "30")),
        "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "1800")),
    }


def get_config():
    mode = os.environ.get("GLR_MODE", "local")
    if mode == "central":
        insecure_defaults = {
            "DATABASE_URL": "postgresql+psycopg2://glr_user:glr_pass@localhost:5432/glr_central",
            "JWT_SECRET_KEY": "dev-jwt-secret-change-me",
            "SYNC_API_KEY": "dev-sync-key-change-me",
        }
        invalid = [
            name for name, default in insecure_defaults.items()
            if os.environ.get(name, default) == default
        ]
        if invalid:
            raise RuntimeError(
                "Central mode requires explicit production configuration for: "
                + ", ".join(invalid)
            )
        return CentralConfig
    return LocalConfig
