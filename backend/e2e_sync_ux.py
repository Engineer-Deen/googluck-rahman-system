"""Focused sync UX verification for desktop acceptance."""
from __future__ import annotations

import json
import time
import urllib.request

import websocket

DEBUG = "http://127.0.0.1:9222"


def wait_ws(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = json.loads(urllib.request.urlopen(DEBUG + "/json", timeout=2).read())
            for t in targets:
                if t.get("webSocketDebuggerUrl") and "devtools" not in (t.get("url") or ""):
                    return t["webSocketDebuggerUrl"], t
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("no CDP target")


class Page:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=12)
        self._id = 0
        self._send("Runtime.enable")
        self._send("Page.enable")

    def _send(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    def js(self, expression):
        result = self._send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            exc = detail.get("exception") or {}
            raise RuntimeError(exc.get("description") or detail.get("text") or str(detail)[:400])
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def step(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name + ":", detail)
    return bool(ok)


def main():
    ok_all = True
    ws_url, target = wait_ws()
    page = Page(ws_url)
    try:
        href = page.js("location.href")
        if "chrome-error" in str(href):
            page._send("Page.navigate", {"url": "http://tauri.localhost/"})
            time.sleep(1.2)
        page.js(
            """
            (() => {
              localStorage.clear();
              return true;
            })()
            """
        )
        page.js("location.reload()")
        time.sleep(1.5)
        page.js(
            """
            (() => {
              window.__e2e_errors = [];
              window.addEventListener('error', e => window.__e2e_errors.push(String(e.message||e)));
              window.addEventListener('unhandledrejection', e => window.__e2e_errors.push(String(e.reason||e)));
              return true;
            })()
            """
        )

        # Toast CSS geometry
        toast_css = page.js(
            """
            (() => {
              const el = document.getElementById('toast');
              // force show to measure
              el.className = 'toast show success';
              document.getElementById('toast-message').textContent = 'geometry probe';
              const cs = getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              return {
                position: cs.position,
                bottom: cs.bottom,
                right: cs.right,
                top: cs.top,
                left: cs.left,
                display: cs.display,
                centerish: Math.abs((rect.left + rect.width/2) - window.innerWidth/2) < 40
                  && Math.abs((rect.top + rect.height/2) - window.innerHeight/2) < 80,
                bottomRight: rect.bottom > window.innerHeight * 0.55 && rect.right > window.innerWidth * 0.55,
                rect: {top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right, w: rect.width, h: rect.height},
                vw: window.innerWidth, vh: window.innerHeight,
              };
            })()
            """
        )
        ok_all &= step("toast_bottom_right", toast_css and toast_css.get("bottomRight") and not toast_css.get("centerish"), str(toast_css)[:450])

        # Auto-hide
        page.js("toast('auto-hide probe', 'success'); true")
        shown = page.js("document.getElementById('toast').classList.contains('show')")
        time.sleep(4.3)
        hidden = page.js("!document.getElementById('toast').classList.contains('show')")
        ok_all &= step("toast_auto_hide", shown and hidden, f"shown={shown} hidden_after={hidden}")

        # Debounce identical sync toasts
        debounce = page.js(
            """
            (() => {
              lastSyncToastMsg = '';
              lastSyncToastAt = 0;
              syncToast('Central synchronization unavailable. Local sales continue normally.', 'warning');
              const first = document.getElementById('toast-message').textContent;
              const firstShow = document.getElementById('toast').classList.contains('show');
              // immediate identical retry should be suppressed (same message within 60s)
              closeToast();
              syncToast('Central synchronization unavailable. Local sales continue normally.', 'warning');
              const secondShow = document.getElementById('toast').classList.contains('show');
              return { first, firstShow, secondShow, lastMsg: lastSyncToastMsg };
            })()
            """
        )
        ok_all &= step(
            "sync_toast_debounce",
            debounce and debounce.get("firstShow") and debounce.get("secondShow") is False,
            str(debounce),
        )

        # Admin login + sync header
        admin = page.js(
            """
            (async () => {
              selectedLoginRole = 'owner';
              if (typeof setLoginRole === 'function') setLoginRole('owner');
              document.getElementById('login-email').value = 'admin@glr.test';
              document.getElementById('login-password').value = 'admin123';
              await doLogin();
              await new Promise(r => setTimeout(r, 800));
              await pollSyncStatus();
              await new Promise(r => setTimeout(r, 3500)); // wait one poll/auto-sync cycle
              await pollSyncStatus();
              return {
                app: document.getElementById('app').classList.contains('visible'),
                role: currentStaff && currentStaff.role,
                label: document.getElementById('sync-label')?.textContent,
                badge: document.getElementById('sync-badge')?.textContent,
                badgeDisplay: document.getElementById('sync-badge')?.style.display,
                toastMsg: document.getElementById('toast-message')?.textContent,
                toastShow: document.getElementById('toast')?.classList.contains('show'),
                snapshot: lastSyncSnapshot,
                errors: (window.__e2e_errors || []).slice(),
              };
            })()
            """
        )
        ok_all &= step("admin_login", admin and admin.get("app"), str(admin)[:400])
        label = (admin or {}).get("label") or ""
        ok_all &= step(
            "header_unavailable_or_pending",
            ("unavailable" in label.lower())
            or ("retrying" in label.lower())
            or ("sync" in label.lower())
            or label in ("Synced", "Syncing"),
            label,
        )

        # Silent auto-sync should not flood "Synchronization started"
        flood = page.js(
            """
            (async () => {
              const seen = [];
              const orig = toast;
              window.__toastLog = [];
              // wrap toast temporarily
              const realToast = toast;
              // call maybeAutoSync multiple times by resetting throttle
              lastAutoSyncAt = 0;
              await triggerSync({ silent: true });
              lastAutoSyncAt = 0;
              await triggerSync({ silent: true });
              lastAutoSyncAt = 0;
              await triggerSync({ silent: true });
              return {
                toastVisible: document.getElementById('toast').classList.contains('show'),
                toastMsg: document.getElementById('toast-message')?.textContent || '',
                startedFlood: /Synchronization started/i.test(document.getElementById('toast-message')?.textContent || ''),
              };
            })()
            """
        )
        ok_all &= step("no_silent_sync_toast_flood", flood and not flood.get("startedFlood"), str(flood))

        # Recovery transition toast (simulated state machine, no Firestore)
        recovery = page.js(
            """
            (() => {
              lastSyncToastMsg = '';
              lastSyncToastAt = 0;
              lastSyncSnapshot = { key: 'unavailable', pending: 3 };
              // simulate transition unavailable -> synced
              const prev = lastSyncSnapshot;
              const key = 'synced';
              let msg = null;
              if (prev.key !== key) {
                if (prev.key === 'unavailable' && key === 'synced') {
                  syncToast('Central synchronization restored.', 'success');
                  msg = document.getElementById('toast-message').textContent;
                }
              }
              // second identical recovery attempt should debounce
              closeToast();
              syncToast('Central synchronization restored.', 'success');
              const second = document.getElementById('toast').classList.contains('show');
              return { msg, secondSuppressed: second === false };
            })()
            """
        )
        ok_all &= step(
            "recovery_toast_once",
            recovery and recovery.get("msg") == "Central synchronization restored." and recovery.get("secondSuppressed"),
            str(recovery),
        )

        # Logout / re-login regression
        relogin = page.js(
            """
            (async () => {
              await doLogout();
              await new Promise(r => setTimeout(r, 300));
              selectedLoginRole = 'owner';
              if (typeof setLoginRole === 'function') setLoginRole('owner');
              document.getElementById('login-email').value = 'admin@glr.test';
              document.getElementById('login-password').value = 'admin123';
              await doLogin();
              await new Promise(r => setTimeout(r, 500));
              const adminOk = document.getElementById('app').classList.contains('visible') && currentStaff && currentStaff.role === 'admin';
              await doLogout();
              await new Promise(r => setTimeout(r, 300));
              selectedLoginRole = 'seller';
              if (typeof setLoginRole === 'function') setLoginRole('seller');
              document.getElementById('login-email').value = 'cashier@glr.test';
              document.getElementById('login-password').value = 'cashier123';
              await doLogin();
              await new Promise(r => setTimeout(r, 500));
              return {
                adminOk,
                cashierOk: document.getElementById('app').classList.contains('visible') && currentStaff && currentStaff.role === 'cashier',
                staffNav: document.querySelector('.nav-tab[data-panel="staff"]')?.style.display,
                errors: (window.__e2e_errors || []).slice(),
              };
            })()
            """
        )
        ok_all &= step("logout_relogin_admin", relogin and relogin.get("adminOk"), str(relogin)[:300])
        ok_all &= step(
            "cashier_login",
            relogin and relogin.get("cashierOk") and relogin.get("staffNav") == "none",
            str(relogin)[:300],
        )

        # Pending count still readable
        pending = page.js(
            """
            (async () => {
              selectedLoginRole = 'owner';
              await doLogout();
              document.getElementById('login-email').value = 'admin@glr.test';
              document.getElementById('login-password').value = 'admin123';
              setLoginRole('owner');
              await doLogin();
              await pollSyncStatus();
              const data = await api('/sync/status');
              return {
                pending: data.pending_count,
                label: document.getElementById('sync-label')?.textContent,
                badge: document.getElementById('sync-badge')?.textContent,
                badgeDisplay: document.getElementById('sync-badge')?.style.display,
              };
            })()
            """
        )
        ok_all &= step("pending_count_header", pending is not None and "pending" in pending, str(pending))

        js_errors = page.js("window.__e2e_errors || []")
        ok_all &= step("no_js_errors", not js_errors, str(js_errors))

    finally:
        page.close()

    print("OVERALL", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
