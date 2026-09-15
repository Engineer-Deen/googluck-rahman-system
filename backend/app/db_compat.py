"""Non-destructive SQL compatibility helpers for existing installs."""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.extensions import db

# Integer PK tables that may receive explicit IDs (Firestore SQL mirror / sync).
# Explicit inserts do not advance PostgreSQL SERIAL/IDENTITY sequences, so
# later ORM inserts without an id can collide (e.g. Key (id)=(2) already exists).
_POSTGRES_SERIAL_TABLES = (
    "shops",
    "staff",
    "products",
    "system_settings",
    "audit_log",
)


def resync_postgres_serial_sequences() -> list[str]:
    """Align PostgreSQL serial/identity sequences with MAX(id) for known tables.

    No-op on SQLite and other dialects. Safe to run repeatedly on startup.
    Returns the table names whose sequences were adjusted.
    """
    if db.engine.dialect.name != "postgresql":
        return []

    inspector = inspect(db.engine)
    existing = set(inspector.get_table_names())
    adjusted: list[str] = []

    for table in _POSTGRES_SERIAL_TABLES:
        if table not in existing:
            continue
        sequence_name = db.session.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": table},
        ).scalar()
        if not sequence_name:
            continue

        # setval(..., max, true) => next nextval is max+1.
        # Empty table: setval(..., 1, false) => next nextval is 1.
        # Table name is taken only from the fixed whitelist above.
        db.session.execute(
            text(
                f"""
                SELECT setval(
                    :sequence_name,
                    COALESCE((SELECT MAX(id) FROM {table}), 1),
                    (SELECT EXISTS (SELECT 1 FROM {table}))
                )
                """
            ),
            {"sequence_name": sequence_name},
        )
        adjusted.append(table)

    return adjusted
