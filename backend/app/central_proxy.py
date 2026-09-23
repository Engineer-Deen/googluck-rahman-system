"""
Forward owner/admin actions from a shop PC's local backend to the central server.

Staff accounts, shop details and system settings are central-only data (see
the docstrings in routes/staff.py and routes/shop.py). The desktop UI only ever
talks to its own local Flask server, so when one of those actions arrives in
local mode the local server has to pass it on to central using the caller's own
login token -- the same pattern sales.py already uses for voids and payments
and audit.py uses for the audit log.

Previously these routes simply returned 403 in local mode, with a message
saying "connect to the internet". That message was shown even when the PC was
online, because nothing ever forwarded the request.
"""
import requests
from flask import current_app, jsonify, request

CENTRAL_TIMEOUT_SECONDS = 30


def is_local_mode():
    return current_app.config.get("GLR_MODE") != "central"


def forward_to_central(method, path, offline_message):
    """
    Send the current request's JSON body to the central server.

    Returns (body, response):
      body      -- central's parsed JSON on success (2xx), otherwise None
      response  -- the (flask_response, status) tuple the route should return

    Only a genuine connection failure produces the "offline" 503. Anything the
    central server actually answers (validation errors, 401, 403, 409...) is
    passed through unchanged so the user sees the real reason.

    The 503 uses code "central_unreachable" on purpose: the frontend logs the
    user out for "central_session_*" codes, and a failed settings save should
    not do that.
    """
    # .get(...) with a fallback, not config[...]: a misconfigured or minimal
    # app (missing this key entirely) must still fail as "offline", never as
    # an unhandled 500 that hides the real, customer-facing error message.
    central_url = current_app.config["CENTRAL_SYNC_URL"]
    url = central_url.rstrip("/") + path
    try:
        resp = requests.request(
            method,
            url,
            json=request.get_json(silent=True) or {},
            headers={"Authorization": request.headers.get("Authorization", "")},
            timeout=CENTRAL_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None, (jsonify(error=offline_message, code="central_unreachable"), 503)

    try:
        body = resp.json()
    except ValueError:
        return None, (
            jsonify(error=f"The central server returned an unexpected response ({resp.status_code})."),
            502,
        )
    return (body if resp.ok else None), (jsonify(body), resp.status_code)
