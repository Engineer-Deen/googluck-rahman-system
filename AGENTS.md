# Repository Guidelines

## Project Structure & Module Organization

- `backend/` contains the Flask application. The factory is in `app/__init__.py`; HTTP endpoints live in `app/routes/`, database models in `app/models/`, and local-to-central synchronization in `app/sync/`.
- `frontend/` is the dependency-free web UI: `index.html`, `styles.css`, and `app.js`. It communicates with the local Flask API at `/api`.
- `src-tauri/` wraps the frontend and backend in the Tauri desktop application. Rust source is under `src-tauri/src/`; app permissions and packaging configuration live in `capabilities/` and `tauri.conf.json`.
- `dataconnect/` holds Firebase Data Connect schema and connector definitions. Keep schema changes and corresponding connector queries aligned.
- Start from `backend/.env.example` when configuring local environment variables. Never commit real `.env` files, databases, or generated build output.

## Build, Test, and Development Commands

Run the API from `backend/` after creating and activating a virtual environment:

```powershell
pip install -r requirements.txt
python run.py
python seed.py
```

`run.py` starts the local Flask server (normally port 5000); `seed.py` populates development data. Check it with `Invoke-WebRequest http://localhost:5000/api/health`.

For the desktop wrapper, run these from `src-tauri/`:

```powershell
cargo check
cargo test
```

Use the installed Tauri CLI for desktop development/builds when available (for example, `cargo tauri dev`).

## Coding Style & Naming Conventions

Use four spaces for Python and Rust’s standard `cargo fmt` formatting. Python modules and functions use `snake_case`; classes use `PascalCase`. Keep Flask routes grouped by domain and registered through blueprints. JavaScript uses two spaces, `camelCase` functions/variables, and `UPPER_SNAKE_CASE` constants. Preserve the existing explicit, defensive error handling around authentication, audit records, and sync outbox operations.

## Testing Guidelines

No Python test suite or coverage threshold is currently configured. Add focused tests alongside new backend behavior in a `backend/tests/` package, named `test_<feature>.py`, and use Flask’s application factory for setup. Run `cargo test` for Rust changes and manually verify affected API flows and the health endpoint before opening a pull request.

## Commit & Pull Request Guidelines

The available history uses short imperative summaries (for example, `Clean repository and add gitignore`). Follow that style: `Add stock adjustment validation`, not `added fixes`. Keep commits scoped. Pull requests should explain user-visible behavior, note database/schema or configuration changes, link related issues, include screenshots for frontend changes, and list the commands or manual flows verified.
