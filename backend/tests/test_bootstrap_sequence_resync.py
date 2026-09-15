"""Regression coverage for PostgreSQL serial drift and bootstrap atomicity."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.bootstrap import (
    BOOTSTRAP_CREDENTIAL_MARKER_KEY,
    apply_one_time_credential_bootstrap,
)
from app.db_compat import resync_postgres_serial_sequences
from app.extensions import db
from app.models import Shop, Staff, SystemSetting


class PostgresSerialResyncTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_resync_is_noop_on_sqlite(self):
        with self.app.app_context():
            self.assertEqual(resync_postgres_serial_sequences(), [])

    def test_resync_advances_stale_postgres_sequence_for_system_settings(self):
        """Reproduce the production failure mode: rows with explicit ids and a lagging sequence."""
        execute = MagicMock()

        def execute_side_effect(statement, params=None):
            sql = str(statement)
            result = MagicMock()
            if "pg_get_serial_sequence" in sql:
                table = (params or {}).get("table_name")
                result.scalar.return_value = (
                    "public.system_settings_id_seq" if table == "system_settings" else None
                )
            else:
                result.scalar.return_value = 2
            return result

        execute.side_effect = execute_side_effect
        mock_db = MagicMock()
        mock_db.engine.dialect.name = "postgresql"
        mock_db.session.execute = execute

        with patch("app.db_compat.db", mock_db):
            with patch("app.db_compat.inspect") as inspect_mock:
                inspect_mock.return_value.get_table_names.return_value = [
                    "system_settings",
                    "shops",
                ]
                adjusted = resync_postgres_serial_sequences()

        self.assertEqual(adjusted, ["system_settings"])
        setvals = [
            call
            for call in execute.call_args_list
            if "setval" in str(call.args[0]).lower()
            and "pg_get_serial_sequence" not in str(call.args[0])
        ]
        self.assertEqual(len(setvals), 1)
        self.assertEqual(setvals[0].args[1].get("sequence_name"), "public.system_settings_id_seq")


class BootstrapAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            GLR_MODE="central",
        )
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()
            db.session.add_all(
                [
                    Shop(id=1, name="Customer Shop"),
                    Staff(
                        id=1,
                        shop_id=1,
                        name="Demo Admin",
                        email="admin@glr.test",
                        password_hash=generate_password_hash("admin123"),
                        role="admin",
                    ),
                    # Existing settings with explicit ids — same shape as a
                    # Firestore-mirrored compatibility SQL table.
                    SystemSetting(id=1, key="timeout_minutes", value="15"),
                    SystemSetting(id=2, key="full_login_hours", value="8"),
                ]
            )
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _env(self):
        return {
            "BOOTSTRAP_CREDENTIAL_UPDATE_TOKEN": "once-token-abc",
            "BOOTSTRAP_TARGET_EMAIL": "admin@glr.test",
            "BOOTSTRAP_NEW_PASSWORD": "RealOwnerPass!99",
            "BOOTSTRAP_NEW_EMAIL": "owner@customer.shop",
            "BOOTSTRAP_NEW_NAME": "Shop Owner",
            "BOOTSTRAP_NEW_ROLE": "owner",
        }

    def test_marker_insert_integrity_error_rolls_back_credentials(self):
        with self.app.app_context():
            original_hash = db.session.get(Staff, 1).password_hash
            with patch.dict(os.environ, self._env(), clear=False):
                with patch.object(
                    db.session,
                    "commit",
                    side_effect=IntegrityError(
                        "INSERT INTO system_settings",
                        {},
                        Exception("Key (id)=(2) already exists."),
                    ),
                ):
                    with patch(
                        "app.db_compat.resync_postgres_serial_sequences",
                        return_value=["system_settings"],
                    ):
                        with self.assertRaises(IntegrityError):
                            apply_one_time_credential_bootstrap()

            staff = db.session.get(Staff, 1)
            self.assertEqual(staff.email, "admin@glr.test")
            self.assertEqual(staff.password_hash, original_hash)
            self.assertIsNone(
                SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).first()
            )

    def test_stale_sequence_integrity_error_retries_after_resync(self):
        with self.app.app_context():
            commit_calls = {"n": 0}
            real_commit = db.session.commit

            def flaky_commit():
                commit_calls["n"] += 1
                # Fail only the first credential+marker commit. The resync
                # commit and the retry commit must succeed.
                if commit_calls["n"] == 1:
                    raise IntegrityError(
                        "INSERT INTO system_settings",
                        {},
                        Exception("Key (id)=(2) already exists."),
                    )
                return real_commit()

            with patch.dict(os.environ, self._env(), clear=False):
                with patch.object(db.session, "commit", side_effect=flaky_commit):
                    with patch(
                        "app.db_compat.resync_postgres_serial_sequences",
                        return_value=["system_settings"],
                    ) as resync:
                        result = apply_one_time_credential_bootstrap()

            self.assertTrue(result["applied"])
            resync.assert_called_once()
            self.assertGreaterEqual(commit_calls["n"], 3)
            staff = Staff.query.filter_by(email="owner@customer.shop").one()
            self.assertTrue(check_password_hash(staff.password_hash, "RealOwnerPass!99"))
            self.assertIsNotNone(
                SystemSetting.query.filter_by(key=BOOTSTRAP_CREDENTIAL_MARKER_KEY).one()
            )
