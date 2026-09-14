# Desktop Auto-Update (GitHub Releases)

This app uses the official **Tauri 2 updater** with **signed** GitHub Release artifacts.

## Repository

- Origin: `https://github.com/Engineer-Deen/googluck-rahman-system.git`
- Update endpoint configured in `src-tauri/tauri.conf.json`:
  - `https://github.com/Engineer-Deen/googluck-rahman-system/releases/latest/download/latest.json`

## Signing keys

- Public key: embedded in `src-tauri/tauri.conf.json` → `plugins.updater.pubkey`
- Private key: **never commit**. Generated locally at:
  - `%USERPROFILE%\.glr-updater-keys\glr-updater.key`
  - `%USERPROFILE%\.glr-updater-keys\glr-updater.key.pub`

### Required GitHub Secrets

| Secret | Value |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | Full text of `glr-updater.key` |

Add it under: GitHub → Repository → Settings → Secrets and variables → Actions.

Do **not** create `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. This project’s signing key is not password-protected, and GitHub does not allow empty repository secrets. The release workflow sets `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` to a literal empty string in the job environment (not a secret) so Tauri can sign without prompting.

## Publish a new version (example 0.1.0 → 0.1.1)

1. Ensure backend sidecar builds cleanly (`backend/goodluck-backend.spec` present).
2. Ensure the `TAURI_SIGNING_PRIVATE_KEY` GitHub secret is set (no password secret).
3. Create and push a tag:

```powershell
git tag v0.1.1
git push origin v0.1.1
```

4. GitHub Actions workflow `.github/workflows/release.yml` will:
   - Set app version from the tag (`v0.1.1` → `0.1.1`)
   - Build the Python sidecar
   - Build the Tauri Windows app + signed updater artifacts
   - Create/update the GitHub Release including `latest.json`

5. An installed owner on **0.1.0** will, after app start (~8s):
   - Check `latest.json`
   - See a bottom-right non-modal notice: “New version 0.1.1 is available.”
   - Click **Update now** → download → signature verify → install → relaunch into **0.1.1**

## Notes

- Updater failures (offline / GitHub down) never block local POS / SQLite / outbox sync.
- Choosing **Later** suppresses the prompt for the rest of that app session only.
- Do not rotate/lose the private key: owners cannot verify future updates signed with a different key unless you also ship a new public key in a forced reinstall.
