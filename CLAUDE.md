# datasette-google-sheets

Import Google Sheets into Datasette tables and export tables, views and queries
to Google Sheets, one-shot or on a schedule. Every import, export and synced
table is a "link" row in the internal DB (D2). Credentials come from
datasette-google-auth, schedules from datasette-cron.

## Local-only planning files

- `wiki/` and `todos/` are local notes, excluded via `.git/info/exclude`. Never
  commit them or add them to `.gitignore`.
- **Decisions are binding and live in `wiki/01-decisions.md` (D1–D27+).** Read it
  before starting work. If a ticket conflicts with it, the decisions file wins;
  anything that contradicts it stops the work and goes to Alex. Record new
  decisions there as D28+ (who, when) and follow-ups in `wiki/90-future-ideas.md`.
- Tickets for v1 are in `todos/v1/` (index and shared context in `todos/v1/README.md`).
  One commit per ticket on branch `v1`.

## Architecture

- **Backend:** Python, Datasette >=1.0a41, datasette-plugin-router, Pydantic
- **Credentials:** datasette-google-auth, **public API only** (names in its `__all__`:
  `list_credentials`, `get_credential`, `Credential.request()` / `.info`, `connect_url`,
  `error_response`, the `GoogleAuthError` subclasses). Never import its private modules
  or touch its tables.
- **Schedules:** datasette-cron (hard dependency, D3). One task per scheduled link,
  `google-sheets:<link_id>`, handler `google_sheets:run-link` (D4). Never use
  `update_task` or rrule schedules.
- **Frontend:** Svelte 5 (runes), TypeScript, Vite, openapi-fetch, served via datasette-vite (ticket 14)
- **Database:** sqlite-migrate for internal.db schema management (ticket 06)
- **Build:** Just (Justfile), uv (Python), npm (frontend)

Sibling checkouts are editable path sources (`[tool.uv.sources]`; uv sources
aren't transitive, so acl and acl-share are repeated): `../datasette-google-auth`,
`../datasette-cron` (branch `otel`), `../datasette-acl` (`grant-event` or `main`),
`../datasette-acl-share`.

## Commands

Always go through `just`.

| Command | What it does |
|---------|-------------|
| `just dev` | Datasette on port **8022** (`.tmp/internal.db`, `.tmp/tmp.db`), all google-sheets/google-auth/cron permissions granted |
| `just dev-with-hmr` | Datasette + Vite HMR (restarts on .py/.html changes) |
| `just frontend-dev` | Vite dev server on port **5188** |
| `just frontend` | Build frontend into the package (`manifest.json`, `static/gen/`; gitignored) |
| `just format` / `format-check` | ruff fix + format, prettier (frontend, once it exists) |
| `just check` | ty + ruff lint + ruff format check + svelte-check (skipped until `frontend/` exists) |
| `just test` | Python tests (pytest, asyncio strict); never collects `tests/live/` |
| `just clean-dev` | Delete `.tmp/` (dev databases) |

Later tickets add `just types`, `types-check-fresh`, `openapi`, `shots`, `telemetry-doc`
and `test-live`. **Agents never run `just test-live`** (Alex does).

When stopping dev servers, kill only your own PIDs. Never `pkill -f vite`.

## Project Structure

```
datasette_google_sheets/
├── __init__.py              # Plugin hooks only
├── config.py                # Pydantic plugin config (extra="forbid"); get_config(datasette)
├── exporter.py              # Export runner: read as actor → caps → clear+append → bold frozen header
├── importer.py              # Import runner: size cap → fetch → strict headers → mapping → hash → one-txn write
├── internal_migrations.py   # sqlite-migrate schema: links + runs tables (append-only)
├── internal_db.py           # InternalDB: typed Link / Run rows, link CRUD, run history + pruning
├── permissions.py           # D15 actions + can_schedule / is_admin / can_{view,manage,operate}_link
├── router.py                # Shared Router; every view's request body capped at 16 KB (JSON 413)
├── runner.py                # run_link(): acting actor (D5), permission checks, run history, status + auto-pause (D17)
├── sheets.py                # Thin Sheets client over cred.request(); SheetsError keeps Google's reason
└── routes/
    ├── pages.py             # Page routes (render HTML)
    └── api.py               # JSON API (Pydantic in/out, OpenAPI)
tests/
├── mock_google/             # Vendored from google-auth @ a2f4eee, Sheets extended (D22)
├── conftest.py              # Network block + fixture imports
├── fixtures_*.py            # google (vendored), sheets, import, export fixtures
├── test_config.py           # defaults, overrides, unknown keys and bounds → StartupError
├── test_exporter.py         # sources, caps, truncation, modes, partial writes
├── test_importer.py         # modes, strict headers, keys, hash skip, atomicity
├── test_internal_db.py      # migrations, link CRUD, unique tab/sync, run pruning, abandoned runs
├── test_mock_google.py      # the mock's Sheets endpoints and error shapes
├── test_permissions.py      # actions default-deny, config grants, link helper truth table
├── test_runner.py           # acting actor, permissions (synced → database level), pause/error/retry, lock
├── test_sheets.py           # URL parsing, client calls, error classification
└── test_smoke.py            # google-sheets, google-auth and cron are all registered
```

## Hooks Used

- `startup()` — validates the plugin config; a bad key or value raises `StartupError`
  naming the field. Read it anywhere with `config.get_config(datasette)`. Then applies the
  internal-DB migrations and marks runs a crashed process left `running` as `abandoned` errors
- `register_actions()` — `google-sheets-schedule` and `google-sheets-admin`, both global and
  default deny (D15). Links are owner-only, checked in code (`permissions.py`), never `allowed()`
- `register_routes()` — registers all routes from the shared router
- `extra_template_vars()` — `datasette_google_sheets_vite_entry` (datasette-vite; safe
  without a built frontend, it only raises when called with an unknown entrypoint)

## Environment Variables

- `DATASETTE_SECRET` — required for the dev server (`just dev` sets it)
- `DATASETTE_GOOGLE_AUTH_KEY` — google-auth's Fernet key; `just dev` passes it through
  as `encryption-key`. Keep it stable: credentials in `.tmp/internal.db` can't be
  decrypted under a different key. Generate with `uv run datasette google-auth generate-key`.

## Invariants

- No tokens, keys, **cell values**, emails, spreadsheet ids or titles in logs,
  errors-to-logs, telemetry or events. User-facing errors may name the sheet or
  tab to the link's owner.
- The default test suite never touches the network. Use the vendored mock
  (`tests/mock_google/`, ticket 04) and the socket block. `tests/live/` is opt-in
  and agents never run it.
- Background work always acts as the stored owner, re-resolved on each run (D5).
  Our sync writes never go through `allowed()`; everything else does.
- Synced tables are read-only via a table-level permission deny only, no triggers (D7).
- **No cross-repo edits (D21):** never edit `../datasette-google-auth`,
  `../datasette-cron`, `../datasette-acl` or Datasette core. Upstream needs become
  tickets in the sibling's local `todos/`.

## Key Conventions

- **`__init__.py` is hooks only.** Logic goes in its own module.
- **`datasette.allowed(...)` is keyword-only.**
- **No `from __future__ import annotations` in `routes/`**: datasette-plugin-router reads real
  annotation objects (`Annotated[Model, Body()]`, `str` path params); string annotations silently
  drop the request body and path params.
- **httpx2, not httpx** (Sheets calls go through `cred.request()`).
- `@dataclass` is fine in the package.
- **Svelte 5 runes**: `$state()`, `$derived()`, `$effect()`, `$props()`
- **IDs**: `python-ulid`
- **Internal DB access**: reads use `db.execute()`; writes use `execute_write_fn()` with named functions
- **Template**: one template for all pages; routes vary `entrypoint` and `page_data`
- **Tests**: `asyncio_mode = "strict"`; async tests use `@pytest.mark.asyncio`, async
  fixtures `@pytest_asyncio.fixture`; shared fixtures in `tests/conftest.py` or `tests/fixtures_*.py`.
- **Commits**: short imperative subject, no Conventional-Commits prefix; the body explains why.
- **Reference implementation:** `~/work/simonw/datasette-google-auth` (@ a2f4eee). Copy its
  patterns; never import its private modules.
