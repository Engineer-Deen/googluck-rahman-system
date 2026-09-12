"""
Local-only bookkeeping. Currently used to remember the timestamp of
this device's last successful pull-sync, so the next pull only asks
the central server for "what changed since then" instead of
re-downloading the entire staff/product/shop tables every time.

Central mode has no use for this table (it's the source of truth
being pulled from, not a puller).
"""
from app.extensions import db


class SyncState(db.Model):
    __tablename__ = "sync_state"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(255), nullable=True)