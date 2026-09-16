import unittest
import os

from flask import Flask

from app import _configure_cors
from app.config import get_config


class DeploymentConfigTests(unittest.TestCase):
    def _app(self, origins, mode=None):
        app = Flask(__name__)
        app.config["CORS_ALLOWED_ORIGINS"] = origins
        if mode:
            app.config["GLR_MODE"] = mode
        _configure_cors(app)

        @app.get("/api/health")
        def health():
            return {"status": "ok"}

        return app

    def test_configured_cors_allows_listed_origin(self):
        client = self._app("https://approved.example,http://tauri.localhost").test_client()
        response = client.get(
            "/api/health",
            headers={"Origin": "https://approved.example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://approved.example")

    def test_configured_cors_blocks_unlisted_origin(self):
        client = self._app("https://approved.example").test_client()
        response = client.get(
            "/api/health",
            headers={"Origin": "https://unapproved.example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_blank_cors_configuration_preserves_local_development_behavior(self):
        client = self._app("").test_client()
        response = client.get(
            "/api/health",
            headers={"Origin": "http://localhost:5000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "http://localhost:5000")

    def test_blank_cors_configuration_does_not_enable_wildcard_central_cors(self):
        client = self._app("", mode="central").test_client()
        response = client.get(
            "/api/health",
            headers={"Origin": "https://unconfigured.example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_central_config_rejects_missing_firebase_credentials(self):
        names = (
            "GLR_MODE",
            "CENTRAL_DATA_PROVIDER",
            "JWT_SECRET_KEY",
            "SYNC_API_KEY",
            "FIREBASE_SERVICE_ACCOUNT_FILE",
            "FIREBASE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_APPLICATION_CREDENTIALS",
        )
        saved = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            os.environ.update({
                "GLR_MODE": "central",
                "CENTRAL_DATA_PROVIDER": "firestore",
                "JWT_SECRET_KEY": "configured-jwt",
                "SYNC_API_KEY": "configured-sync",
            })
            with self.assertRaisesRegex(RuntimeError, "Firestore mode requires"):
                get_config()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
