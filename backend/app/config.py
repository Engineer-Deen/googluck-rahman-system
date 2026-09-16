"""
Configuration for the Good Luck Rahman backend.

The SAME Flask app runs in two modes, controlled by the GLR_MODE
environment variable:

  GLR_MODE=local    -> runs on each shop's PC (via Tauri), uses SQLite.
                        This is the offline-first local database.
    GLR_MODE=central  -> runs in the cloud server, uses Firestore through
                                                the Firebase Admin SDK.
                                                This is the single source of truth for the whole
                                                business, fed by every device's sync push.

Nothing else in the app needs to know which mode it's in -- routes and
models are written against SQLAlchemy, which works the same either way.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
IS_FROZEN = bool(getattr(sys, "frozen", False))

load_dotenv(BASE_DIR / ".env", override=False)


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
    SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")

    # Optional comma-separated exact origins for browser clients. Leave blank
    # for local desktop/browser development, which keeps the existing permissive
    # CORS behavior. Central deployments should set this explicitly.
    CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "")

    # Central persistence is Firestore. SQLAlchemy remains available only for
    # the local SQLite application.
    CENTRAL_DATA_PROVIDER = "firestore"
    FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
    FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_FILE",
        "/etc/secrets/firebase-service-account.json",
    )
    FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")


class LocalConfig(BaseConfig):
    """
    Runs on a shop's PC. SQLite file lives next to the app.

    Local mode accepts only the deliberate LOCAL_DATABASE_URL override and
    otherwise uses the packaged SQLite database path.
    """
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    # Used by create_app() to provision only a brand-new frozen local install.
    BOOTSTRAP_INITIAL_LOCAL_DATA = IS_FROZEN
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "LOCAL_DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'glr_local.sqlite'}"
    )


class CentralConfig(BaseConfig):
    """Runs on the cloud server with Firestore as its only central store."""


def _apply_runtime_config_values():
    """Refresh config classes from the current environment for tests and runtime reloads."""
    BaseConfig.GLR_MODE = os.environ.get("GLR_MODE", "local")
    BaseConfig.SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    BaseConfig.JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-me")
    BaseConfig.SQLALCHEMY_TRACK_MODIFICATIONS = False
    BaseConfig.DEVICE_ID_FILE = INSTANCE_DIR / "device_id.txt"
    BaseConfig.CENTRAL_SYNC_URL = os.environ.get("CENTRAL_SYNC_URL", "http://localhost:8000")
    BaseConfig.SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")
    BaseConfig.CORS_ALLOWED_ORIGINS = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    BaseConfig.CENTRAL_DATA_PROVIDER = "firestore"
    BaseConfig.FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
    BaseConfig.FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_FILE",
        "/etc/secrets/firebase-service-account.json",
    )
    BaseConfig.FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    BaseConfig.FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")

    LocalConfig.INSTANCE_DIR = INSTANCE_DIR
    LocalConfig.BOOTSTRAP_INITIAL_LOCAL_DATA = IS_FROZEN
    LocalConfig.SQLALCHEMY_DATABASE_URI = os.environ.get(
        "LOCAL_DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'glr_local.sqlite'}"
    )



def get_config():
    _apply_runtime_config_values()
    mode = os.environ.get("GLR_MODE", "local")
    if mode == "central":
        provider = os.environ.get("CENTRAL_DATA_PROVIDER", "firestore").lower()
        if provider != "firestore":
            raise RuntimeError("Central mode requires CENTRAL_DATA_PROVIDER=firestore")
        insecure_defaults = {
            "JWT_SECRET_KEY": {"dev-jwt-secret-change-me", "change-me-too"},
            "SYNC_API_KEY": {"dev-sync-key-change-me", "change-me"},
        }
        invalid = [
            name for name, defaults in insecure_defaults.items()
            if os.environ.get(name, next(iter(defaults))).strip() in defaults
        ]
        if invalid:
            raise RuntimeError(
                "Central mode requires explicit production configuration for: "
                + ", ".join(invalid)
            )
        firebase_file = os.environ.get(
            "FIREBASE_SERVICE_ACCOUNT_FILE",
            "/etc/secrets/firebase-service-account.json",
        )
        if provider == "firestore" and not (
            os.path.isfile(firebase_file)
            or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            raise RuntimeError(
                "Firestore mode requires the Render service-account file, "
                "FIREBASE_SERVICE_ACCOUNT_JSON, or "
                "GOOGLE_APPLICATION_CREDENTIALS"
            )
        return CentralConfig
    return LocalConfig
