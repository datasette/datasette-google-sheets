# Ports: Datasette 8022, Vite 5188 (D1; google-auth uses 8021/5187).

# === Frontend ===
# Svelte 5 + Vite, built into the package (manifest.json + static/gen/,
# both gitignored). frontend/ arrives in ticket 14; then run
# `npm install --prefix frontend` once.

frontend *flags:
  npm run build --prefix frontend {{flags}}

frontend-dev *flags:
  npm run dev --prefix frontend -- --port 5188 {{flags}}

frontend-check:
  npm run check --prefix frontend

frontend-format:
  npm run format --prefix frontend

frontend-format-check:
  npm run format:check --prefix frontend

# === Type Generation ===
# Print the JSON API's OpenAPI document (from the router's Pydantic models).
# Importing the router imports the package, which registers every route.
# `types-routes` (frontend/api.d.ts via openapi-typescript), `types` and
# `types-check-fresh` arrive with frontend/ in ticket 14: until then there is
# no package.json to pin openapi-typescript, and a frontend/ directory would
# switch on the frontend steps of `format` and `check`.
openapi:
  @uv run python -c 'from datasette_google_sheets.router import router; import json; print(json.dumps(router.openapi_document_json(), indent=2))'

# === Formatting ===
# The frontend steps are skipped until frontend/ exists (ticket 14).

format:
  uv run ruff check --fix --quiet
  uv run ruff format
  if [ -d frontend ]; then just frontend-format; fi

format-check:
  uv run ruff format --check
  if [ -d frontend ]; then just frontend-format-check; fi

# === Type Checking + Lint ===

check:
  uv run ty check
  uv run ruff check
  uv run ruff format --check
  if [ -d frontend ]; then just frontend-check; fi

# === Testing ===

# Never collects tests/live/ (pyproject `norecursedirs`) or contacts Google.
test *flags:
  uv run pytest {{flags}}

# === Development ===

# DATASETTE_GOOGLE_AUTH_KEY is passed through to google-auth's encryption-key.
# Keep it stable across restarts (generate once with
# `uv run datasette google-auth generate-key`): credentials stored in
# .tmp/internal.db under one key can't be decrypted under another. Unset, the
# server still starts but no credentials can be created.
dev *flags:
  mkdir -p .tmp
  DATASETTE_SECRET=abc123 uv run datasette \
    -s permissions.google-sheets-schedule true \
    -s permissions.google-sheets-admin true \
    -s permissions.google-auth-connect true \
    -s permissions.google-auth-add-service-account true \
    -s permissions.google-auth-admin true \
    -s permissions.datasette-cron-access true \
    -s permissions.permissions-debug true \
    -s plugins.datasette-google-auth.encryption-key '{"$env": "DATASETTE_GOOGLE_AUTH_KEY"}' \
    --internal .tmp/internal.db \
    -p 8022 \
    --create .tmp/tmp.db \
    {{flags}}

dev-with-hmr *flags:
  watchexec --stop-signal SIGKILL -e py,html --ignore '*.db' --restart --clear -- \
    just dev -s plugins.datasette-vite.dev_ports.datasette_google_sheets 5188 {{flags}}

clean-dev:
  rm -rf .tmp/
