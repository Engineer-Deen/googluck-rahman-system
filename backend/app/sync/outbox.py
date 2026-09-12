"""
The rule that guarantees no record is ever "lost" on the way to the
server: whenever a sale or stock movement is created locally,
enqueue_outbox() writes an outbox row IN THE SAME DATABASE COMMIT as
the record itself (call this before db.session.commit() in the
calling code's transaction, or right after -- see usage in
routes/sales.py and routes/stock.py, both call this only after the
record's own commit succeeded, using the record's own already-assigned
id, so there is no window where the record exists without also being
queued).

Central mode never calls this -- it's the receiving end.
"""
import json

from flask import current_app

from app.extensions import db
from app.models import SyncOutboxItem


def enqueue_outbox(table_name: str, record_id: str, payload: dict):
    if current_app.config["GLR_MODE"] != "local":
        return

    db.session.add(
        SyncOutboxItem(
            table_name=table_name,
            record_id=record_id,
            payload_json=json.dumps(payload, default=str),
        )
    )
    db.session.commit()