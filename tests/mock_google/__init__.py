"""In-process mock of the Google endpoints datasette-google-credentials talks to.

VENDORED from datasette-google-credentials (``~/work/simonw/datasette-google-credentials``,
``tests/mock_google/`` and ``tests/fixtures_google.py``) at commit
**a2f4eee** ("Equalise permission checks for unknown and invisible credential
ids"), per this repo's D22. To be replaced by ``datasette_google_credentials.testing``
once it exists (see ``../datasette-google-credentials/todos/sheets-consumer/
03-public-testing-module.md``, which pulls the Sheets extensions below back
upstream). Local changes since a2f4eee:

- sheets.py: ``fields`` masks on spreadsheet get, ``frozenRowCount``,
  ``Formatted`` cells (``valueRenderOption`` / ``dateTimeRenderOption``),
  ``insertDataOption=INSERT_ROWS`` grid growth, ``batchUpdate``
  (``repeatCell``, ``updateSheetProperties``, recorded), access-list and
  fixture helpers on ``SpreadsheetStore``.
- errors.py / faults.py: documented Google error bodies with ErrorInfo
  reasons (``SERVICE_DISABLED``, ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``,
  ``RATE_LIMIT_EXCEEDED``), injectable via ``faults.fail(..., reason=)``;
  ``faults.fail(..., after=N)`` lets N matching requests through first
  (ticket 05's partial-append test).
- fixtures_google.py: no ``datasette_google_credentials.config`` import.

Served by FastAPI and mounted with ``httpx2.ASGITransport`` (see
``tests/fixtures_google.py``): no uvicorn, no ports, no network.

Upstream's note: modelled on the sqlite-google-sheets mock
(``tests/mock_google/`` at commit 6413af08bfecd39ed539fd8cfb5ba9df0c872235),
which has the JWT-bearer token endpoint, Google-shaped errors, fault injection
and a Sheets read API but no authorization-code flow, PKCE, revoke or
userinfo. That package is an independent implementation of the same ideas,
not a copy of that code.

Modules:
    keys.py         RSA service-account keys and the id_token signing key,
                    generated per test session (no private keys committed)
    tokens.py       access-token store, JWT-bearer grant (service accounts)
    oauth.py        authorization code + PKCE, refresh, revoke, userinfo
    faults.py       injected failures ("the next POST /revoke returns 500",
                    "the next Sheets call is SERVICE_DISABLED")
    sheets.py       Sheets v4 model: spreadsheet get (with ``fields``), values
                    get / update / append / clear, create, batchUpdate
    errors.py       Google-shaped error bodies
    app.py          the FastAPI app and the request log
"""

# The OAuth endpoints live at http://mock-google/... (the plugin's
# google_base_urls point there). Sheets has no base URL in the plugin config,
# so consumers use the real one; the ASGI transport routes it here anyway.
MOCK_HOST = "mock-google"
MOCK_BASE = f"http://{MOCK_HOST}"
SHEETS_HOST = "sheets.googleapis.com"
SHEETS_BASE = f"https://{SHEETS_HOST}"
