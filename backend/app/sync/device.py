"""
Every local install (each shop PC) gets its own UUID the first time it
runs, saved to a small file next to the database. This id is what
makes sales/stock-movements from different offline devices
distinguishable and safely mergeable at the central server, unlike the
old system where every shop shared one login with no device identity.

Central mode doesn't need this -- it's the receiving end, not a
device.
"""
import uuid

from flask import current_app


def get_current_device_id() -> str:
    path = current_app.config["DEVICE_ID_FILE"]
    if path.exists():
        return path.read_text().strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid.uuid4())
    path.write_text(new_id)
    return new_id