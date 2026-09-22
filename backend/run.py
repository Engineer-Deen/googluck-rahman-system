import os

from app import create_app
from app.config import server_host

app = create_app()

# The background sync worker belongs here, not inside create_app(), and only
# starts ONCE for the actual running server -- not for every create_app()
# call (seed.py, one-off scripts, tests all call create_app() too, and none
# of those should spin up a competing background thread hitting the same
# outbox table).
#
# The WERKZEUG_RUN_MAIN check matters when debug=True: Flask's auto-reloader
# runs TWO processes (a watcher and the real child), and both would import
# this file. Without the check, the watcher process would start its own
# thread too, giving two threads racing over one outbox -- which is exactly
# the kind of bug this whole rebuild exists to eliminate.
def _should_start_worker():
    if app.config["GLR_MODE"] != "local":
        return False
    if not app.debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


if _should_start_worker():
    # Previously this started an infinite background thread that hit the
    # central server every 5-60 seconds for as long as the desktop app was
    # open, even if nobody touched it for hours. That alone burned through
    # the Firestore daily quota. Sync now happens only:
    #   1. Once here, when the app starts up (so a freshly opened app is
    #      current with central right away), and
    #   2. Whenever the user actually sends data -- sales/products/stock
    #      routes already call trigger_sync_soon() after a write, and the
    #      frontend can call POST /api/sync/trigger for a manual "sync now".
    # No timer keeps running in between.
    from app.sync.worker import trigger_sync_soon
    trigger_sync_soon(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000 if os.environ.get("GLR_MODE", "local") == "local" else 8000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=server_host(app.config["GLR_MODE"]), port=port, debug=debug)