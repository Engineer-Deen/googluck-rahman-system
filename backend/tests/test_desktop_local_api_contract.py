"""Desktop local API URL and sidecar readiness contract checks."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_APP_JS = ROOT / "frontend" / "app.js"
FRONTEND_INDEX = ROOT / "frontend" / "index.html"
TAURI_LIB = ROOT / "src-tauri" / "src" / "lib.rs"
CAPABILITIES = ROOT / "src-tauri" / "capabilities" / "default.json"


class DesktopLocalApiContractTests(unittest.TestCase):
    def test_frontend_api_base_uses_ipv4_loopback_not_localhost(self):
        text = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertIn('const API_BASE = "http://127.0.0.1:5000/api";', text)
        self.assertNotIn("goodluck-rahman-api.onrender.com", text)
        self.assertNotIn("http://localhost:5000/api", text)

    def test_frontend_waits_for_local_backend_before_boot_work(self):
        text = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertIn("async function waitForLocalBackend", text)
        self.assertIn('tauriInvoke("glr_local_backend_status")', text)
        self.assertIn('API_BASE + "/health"', text)
        self.assertRegex(text, r"\(async function boot\(\)")
        boot = text[text.index("(async function boot"):]
        self.assertIn("await waitForLocalBackend()", boot)
        self.assertLess(
            boot.index("await waitForLocalBackend()"),
            boot.index("loadLoginBranding()"),
        )

    def test_frontend_never_restores_cached_authentication_at_startup(self):
        text = FRONTEND_APP_JS.read_text(encoding="utf-8")
        startup = text[text.index("let authToken"):text.index("// ----------", text.index("let authToken"))]
        self.assertIn("let authToken = null", startup)
        self.assertIn("let currentStaff = null", startup)
        boot = text[text.index("(async function boot"):]
        self.assertNotIn("enterApp().catch", boot)

    def test_provisioning_does_not_request_an_offline_password(self):
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        script = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("local-enrollment-password", html)
        self.assertNotIn("local_password", script)
        self.assertNotIn("offline access", html.lower())

    def test_tauri_surfaces_sidecar_spawn_errors(self):
        text = TAURI_LIB.read_text(encoding="utf-8")
        self.assertIn("fn glr_local_backend_status", text)
        self.assertIn("spawn_error", text)
        self.assertIn("Local POS server failed to start", text)
        self.assertIn("allow-glr-local-backend-status", CAPABILITIES.read_text(encoding="utf-8"))
        # Ensure spawn failure is stored, not only printed.
        self.assertRegex(
            text,
            re.compile(
                r"Err\(error\)\s*=>\s*\{[\s\S]*spawn_error[\s\S]*Some\(message\)",
                re.MULTILINE,
            ),
        )

    def test_tauri_sidecar_uses_render_for_cloud_sync_and_keeps_local_pos(self):
        text = TAURI_LIB.read_text(encoding="utf-8")
        self.assertIn('.env("GLR_MODE", "local")', text)
        self.assertIn('.env("PORT", "5000")', text)
        self.assertIn(
            '.env("CENTRAL_SYNC_URL", "https://goodluck-rahman-api.onrender.com")',
            text,
        )
        self.assertNotIn("FIREBASE_SERVICE_ACCOUNT_JSON", text)
        self.assertNotIn("SYNC_API_KEY", text)

    def test_official_logo_is_static_login_and_header_branding(self):
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count("assets/goodluck-rahman-enterprise.png"), 2)
        self.assertNotIn("brand-icon-lg", html)
        self.assertNotIn("brand-icon\"", html)
        self.assertNotIn("shop-logo-input", html)

        script = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("saveShopSettings", script)
        self.assertNotIn("shop-logo-input", script)
        self.assertNotIn("logo_data", script)


if __name__ == "__main__":
    unittest.main()
