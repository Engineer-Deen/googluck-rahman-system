# Managed Desktop Sync Provisioning

The desktop application uses two separate endpoints:

- Local POS API: `http://127.0.0.1:5000/api`
- Cloud synchronization API: `https://goodluck-rahman-api.onrender.com`

The bundled backend sidecar receives `CENTRAL_SYNC_URL` from Tauri. It receives `SYNC_API_KEY` from the Windows process environment; the key is never placed in frontend code, Tauri configuration, the MSI, or the repository.

## Provision the first managed customer machine

1. Open PowerShell as the customer service account that will run the desktop application.
2. Set the sync key as a persistent user environment variable. Do not paste the value into source files or commit it:

```powershell
[Environment]::SetEnvironmentVariable('SYNC_API_KEY', '<matching Render SYNC_API_KEY>', 'User')
```

3. Close all Good Luck Rahman windows and restart the application. Windows environment variables are inherited when the Tauri process starts; restarting is required after changing the value.
4. Confirm synchronization from the application status indicator. The local POS remains usable if the key is absent or the cloud is unreachable; queued work remains local and retries later.

Do not print the key, include it in logs, place it in the WebView, pass it as a frontend value, or store it in the MSI.

## Render requirement

Render must define `SYNC_API_KEY` as a server-side environment variable. The customer machine's value must exactly match Render's value because the central sync routes validate the `X-Sync-Key` header.

This shared-key approach is suitable only for the first managed/trusted installation. A broader untrusted customer rollout should use per-device credentials and revocation instead of one shared key.
