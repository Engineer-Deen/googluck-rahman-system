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

    def test_frontend_restores_session_only_by_reverifying_with_local_backend(self):
        """A refresh is now allowed to restore a cached session (deliberate
        product decision), but ONLY by re-checking the cached token against
        this device's own local backend (/auth/me) -- it must never treat a
        cached localStorage token as sufficient on its own, since that's the
        difference between "resume this still-valid session" and "trust
        whatever a browser storage value claims"."""
        text = FRONTEND_APP_JS.read_text(encoding="utf-8")
        startup = text[text.index("let authToken"):text.index("// ----------", text.index("let authToken"))]
        self.assertIn('localStorage.getItem("glr_token")', startup)
        boot = text[text.index("(async function boot"):]
        self.assertIn('await api("/auth/me")', boot)
        self.assertIn("await enterApp()", boot)

    def test_admin_quick_lock_survives_a_page_refresh(self):
        """The admin quick-lock (PIN) state must be persisted, not just held
        in a JS variable -- otherwise, now that sessions survive a refresh,
        simply reloading the page while locked would bypass the PIN screen
        entirely. A fresh, explicit login must still always start unlocked,
        even if a stale lock flag was left over from a previous session."""
        text = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertIn('localStorage.setItem("glr_admin_locked", "1")', text)
        self.assertIn('localStorage.getItem("glr_admin_locked") === "1"', text)
        login = text[text.index("async function doLogin"):text.index("async function enterApp")]
        self.assertIn('localStorage.removeItem("glr_admin_locked")', login)

    def test_provisioning_does_not_request_an_offline_password(self):
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        script = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("local-enrollment-password", html)
        self.assertNotIn("local_password", script)
        self.assertNotIn("offline access", html.lower())

    def test_offline_banner_is_global_and_refreshes_silently_when_back_online(self):
        """A no-internet indicator confined to the login screen isn't enough
        -- it must show everywhere, and reconnecting should quietly refresh
        whatever's on screen rather than requiring the person to notice and
        act themselves."""
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        script = FRONTEND_APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="offline-banner"', html)
        # Not nested inside login-overlay or app -- must render regardless
        # of which screen is showing.
        login_idx = html.index('id="login-overlay"')
        banner_idx = html.index('id="offline-banner"')
        self.assertLess(banner_idx, login_idx)
        self.assertIn("function updateOfflineBanner", script)
        self.assertIn('addEventListener("offline", updateOfflineBanner)', script)
        self.assertIn("function silentlyRefreshActivePanel", script)
        online_handler = script[script.index('addEventListener("online"'):]
        online_handler = online_handler[:online_handler.index("\n  });")]
        self.assertIn("silentlyRefreshActivePanel", online_handler)

    def test_static_buttons_show_progress_while_their_action_is_in_flight(self):
        """logout / restock-apply / create-account / register-admin / and
        the admin quick-unlock must all disable themselves and show an
        in-progress label while their request is running, matching the
        pattern already used elsewhere (submitPayment, saveSystemSettings)
        -- otherwise a slow response looks exactly like a dead button."""
        script = FRONTEND_APP_JS.read_text(encoding="utf-8")

        def body_of(fn_name):
            start = script.index(f"async function {fn_name}")
            # crude but sufficient: take up to the next top-level function
            end = script.index("\nasync function ", start + 1)
            return script[start:end]

        for fn_name, btn_id in [
            ("doLogout", "logout-btn"),
            ("submitRestock", "restock-apply-btn"),
            ("createStaff", "st-create-btn"),
            ("createAdminAccount", "ad-register-btn"),
            ("saveStaffEdit", "st-save-edit-btn"),
            ("submitPasswordReset", "st-reset-submit-btn"),
            ("unlockAdminSession", "admin-unlock-btn"),
        ]:
            body = body_of(fn_name)
            self.assertIn(btn_id, body, f"{fn_name} should reference #{btn_id}")
            self.assertIn(".disabled = true", body, f"{fn_name} should disable its button while running")

    def test_dynamic_row_buttons_show_progress_while_their_action_is_in_flight(self):
        """The per-row action buttons (deactivate/reactivate/delete a product,
        deactivate/reactivate staff, void or edit a sale) are rendered
        dynamically with no fixed id, so they can't reference
        getElementById the way the static buttons above do. They must still
        disable themselves and show progress, using the click event's own
        target instead."""
        script = FRONTEND_APP_JS.read_text(encoding="utf-8")

        def body_of(fn_name):
            start = script.index(f"async function {fn_name}")
            end = script.index("\nasync function ", start + 1)
            return script[start:end]

        for fn_name in [
            "toggleInventoryProductState",
            "deleteInventoryProduct",
            "toggleStaffActive",
            "voidSale",
            "editSale",
        ]:
            body = body_of(fn_name)
            self.assertIn("window.event", body, f"{fn_name} should grab the clicked button via window.event")
            self.assertIn(".disabled = true", body, f"{fn_name} should disable its button while running")

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

    def test_tauri_sidecar_uses_configured_central_cloud_sync_and_keeps_local_pos(self):
        text = TAURI_LIB.read_text(encoding="utf-8")
        self.assertIn('.env("GLR_MODE", "local")', text)
        self.assertIn('.env("PORT", "5000")', text)
        self.assertIn(
            '.env("CENTRAL_SYNC_URL", "https://goodluck-rahman-api.vercel.app")',
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
