import os
import unittest
from unittest.mock import MagicMock, patch

from app import create_app


class CentralInitializationTests(unittest.TestCase):
    def test_central_mode_initializes_firestore_without_sqlalchemy(self):
        with patch.dict(
            os.environ,
            {
                "GLR_MODE": "central",
                "CENTRAL_DATA_PROVIDER": "firestore",
                "JWT_SECRET_KEY": "test-jwt-secret",
                "SYNC_API_KEY": "test-sync-key",
                "FIREBASE_SERVICE_ACCOUNT_FILE": __file__,
            },
            clear=False,
        ), patch("app.db.init_app") as init_db, patch("app.db.create_all") as create_tables, patch(
            "app._run_compat_migrations"
        ) as run_migrations, patch(
            "app.firestore.get_firestore_sync_service", return_value=MagicMock()
        ) as get_firestore:
            app = create_app()

        init_db.assert_not_called()
        create_tables.assert_not_called()
        run_migrations.assert_not_called()
        get_firestore.assert_called()
        self.assertEqual(app.config["GLR_MODE"], "central")
        self.assertEqual(app.config["CENTRAL_DATA_PROVIDER"], "firestore")
        self.assertIsNone(app.config.get("SQLALCHEMY_DATABASE_URI"))

        with app.test_client() as client:
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["database"], "firestore")

    def test_local_mode_keeps_sqlite_initialization(self):
        with patch.dict(
            os.environ,
            {
                "GLR_MODE": "local",
                "LOCAL_DATABASE_URL": "sqlite://",
            },
            clear=False,
        ), patch("app.db.init_app") as init_db, patch("app.db.create_all") as create_tables, patch(
            "app._run_compat_migrations"
        ) as run_migrations:
            app = create_app()

        init_db.assert_called_once_with(app)
        create_tables.assert_called_once_with()
        run_migrations.assert_called_once_with()
        self.assertTrue(app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite://"))


if __name__ == "__main__":
    unittest.main()
