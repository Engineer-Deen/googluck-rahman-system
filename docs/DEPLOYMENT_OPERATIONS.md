# Deployment Operations

This document records the deployment contract supported by the repository. It does not provision or rotate production resources.

## Runtime Modes

### Local desktop

- Tauri starts the bundled Flask sidecar on `http://127.0.0.1:5000`.
- `GLR_MODE=local` selects SQLite/SQLAlchemy and the local durable outbox.
- The frontend talks only to the local sidecar.
- `CENTRAL_SYNC_URL` points the sidecar at the central sync API.
- `SYNC_API_KEY` is read from the Windows process environment and is never placed in frontend code or Tauri configuration.

### Central service

- `GLR_MODE=central` selects Firestore through the Firebase Admin SDK.
- Central startup requires `CENTRAL_DATA_PROVIDER=firestore`, explicit `JWT_SECRET_KEY`, explicit `SYNC_API_KEY`, and Firebase credentials through `FIREBASE_SERVICE_ACCOUNT_FILE`, `FIREBASE_SERVICE_ACCOUNT_JSON`, or `GOOGLE_APPLICATION_CREDENTIALS`.
- Central startup does not initialize SQLAlchemy or SQLite.
- The repository documents the deployed sync endpoint as `https://goodluck-rahman-api.onrender.com` in `docs/managed-desktop-sync-provisioning.md`.

## CORS

`CORS_ALLOWED_ORIGINS` is an optional comma-separated list of exact origins.

- Leave it blank for local desktop/browser development. The existing permissive behavior is retained so localhost and Tauri development continue to work.
- Set it explicitly on a central deployment when a browser frontend is deployed. Do not guess an origin; use the actual deployed frontend origin.
- `http://tauri.localhost` is the packaged Tauri WebView origin observed in the repository's desktop acceptance tooling. The packaged desktop frontend normally calls the local sidecar, so it does not need the central CORS setting for sync traffic.

## Sync Key Rotation

`SYNC_API_KEY` is an environment-provided shared secret used by local devices in the `X-Sync-Key` header. It is not hard-coded by the application and is not returned in API responses.

Rotation procedure:

1. Generate a new secret outside the repository.
2. Update `SYNC_API_KEY` in the central Render environment.
3. Restart/redeploy the central service so it loads the new value.
4. Update the matching user-level `SYNC_API_KEY` on each managed Windows desktop.
5. Restart each desktop application so the sidecar inherits the new environment value.
6. Confirm synchronization status, then remove the old value from operational notes or terminals.

During a staggered rotation, devices using the old value receive authentication failures and retain their local outbox records for retry. The application does not log or expose the secret.

Central mode rejects missing and documented placeholder values such as `change-me` and `dev-sync-key-change-me`.

## Firestore Audit Query Readiness

The audit endpoint is restricted to owner/admin roles and preserves the current response shape, ordering, date filters, optional action/entity filters, and maximum limit of 200.

The current service scans the `audit_log` collection, applies optional filters in Python, sorts descending by `created_at`, and truncates to 200. No Firestore index is required by the current runtime because it does not issue a compound Firestore query.

A future indexed implementation would require a deliberate schema/query change for combinations of `action`, `entity_type`, `created_at`, and descending ordering. That optimization is intentionally outside Phase 3D.

## Historical Migration Tool

`backend/scripts/firestore_migration.py` is retained as a standalone, historical SQL-to-Firestore migration utility. It has no Flask startup caller and is not required by central or local runtime operation. It must not be treated as a central provider or deployment dependency.

## Release Checks

Before deployment, verify:

- central secrets are supplied through the deployment environment;
- `CORS_ALLOWED_ORIGINS` is set only when the real browser frontend origin is known;
- Firebase credentials are available to central startup;
- the health endpoint reports `database=firestore` in central mode;
- local desktop health reports `database=sqlite`;
- no real secrets are committed or copied into frontend/Tauri configuration.
