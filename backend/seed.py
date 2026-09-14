"""
Run this once after setting up a fresh database (local or central) to
create a login you can actually test with.

Usage:
    python seed.py

This script now uses the shared bootstrap helpers so initial data is
created idempotently through the same code path used by runtime startup.
"""

from app import create_app
from app.bootstrap import ensure_initial_central_data, ensure_initial_local_data

app = create_app()

with app.app_context():
    if app.config["GLR_MODE"] == "central":
        ensure_initial_central_data()
    else:
        ensure_initial_local_data()

print("Seed complete.")