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
    from app.sync.worker import start_background_sync
    start_background_sync(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000 if os.environ.get("GLR_MODE", "local") == "local" else 8000))
    app.run(host=server_host(app.config["GLR_MODE"]), port=port, debug=True)