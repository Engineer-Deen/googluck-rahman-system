"""
The outbox: only used in local (SQLite/device) mode.

The rule that guarantees no record is ever "lost" on the way to the
server: whenever a sale or stock movement is saved locally, an outbox
row is written IN THE SAME DATABASE TRANSACTION. If the outbox insert
fails, the whole save is rolled back -- so it is impossible for a
record to exist locally without also being queued to sync. There is
no separate "remember to sync this later" step that can be forgotten.

A background sync worker (see app/sync/) walks this table, pushes each
row to the central server, and only deletes the row after the central
server confirms it has stored the record. If the push fails or the
device is offline, the row just stays here and gets retried -- nothing
is ever silently dropped.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class SyncOutboxItem(db.Model):
    __tablename__ = "sync_outbox"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    table_name = db.Column(db.String(50), nullable=False)  # 'sales', 'stock_movements'
    record_id = db.Column(db.String(36), nullable=False)   # the UUID of the actual record
    payload_json = db.Column(db.Text, nullable=False)       # full record, ready to POST

    created_at = db.Column(db.DateTime, default=utcnow)
    attempt_count = db.Column(db.Integer, default=0)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
