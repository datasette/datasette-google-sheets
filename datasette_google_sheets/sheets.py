"""A thin, typed client for the Google Sheets v4 REST API (D6).

Every call takes a google-auth ``Credential`` (from ``get_credential``) and
goes through ``cred.request()``: this module never mints tokens and never
builds its own HTTP client. ``GoogleAuthError``s raised by ``cred.request()``
(a broken or revoked credential, missing scopes, ...) propagate unchanged;
callers handle them. A non-2xx response from Sheets raises ``SheetsError``.

URL parsing, title quoting, the chunking rule and the URL builder come from
datasette-google-auth's ``samples/google_sheets_import.py`` and
``samples/google_sheets_export.py``.

**Errors keep Google's reason (D6, D30).** Google APIs answer with the
``google.rpc.Status`` envelope (AIP-193, https://google.aip.dev/193)::

    {"error": {"code": 403, "message": "...", "status": "PERMISSION_DENIED",
               "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "SERVICE_DISABLED",
                            "domain": "googleapis.com", "metadata": {...}}]}}

``status`` is the canonical code name. The machine-readable cause is the
ErrorInfo ``reason`` in ``details[]``. ``message`` is a debug string that may
change, so ``SheetsError.kind`` is decided by HTTP code, ``status`` and
``reason`` only, never by the message text. A plain "not shared with you" 403
or a 404 has no ErrorInfo at all.

Nothing here logs. Messages never carry tokens. They may name the
spreadsheet (Google's own messages sometimes include its id or the Cloud
project number), so they're for the link's owner and never for logs,
telemetry or events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    import httpx2
    from datasette_google_auth import Credential

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
CHUNK_ROWS = 5_000

ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
# ErrorInfo reasons (google/api/error_reason.proto) we map to a kind.
SERVICE_DISABLED = "SERVICE_DISABLED"
ACCESS_TOKEN_SCOPE_INSUFFICIENT = "ACCESS_TOKEN_SCOPE_INSUFFICIENT"

SheetsErrorKind = Literal[
    "not_shared",
    "not_found",
    "api_disabled",
    "scope",
    "rate_limited",
    "server",
    "other",
]

API_DISABLED_MESSAGE = (
    "The Google Sheets API isn't enabled for this credential. Enable the "
    "Google Sheets API in the Cloud project of this credential{project}, "
    "then try again."
)
# Which project that is, by credential type (google-auth wiki/TODO.md "Now":
# an OAuth import 403s until the API is on in the OAuth client's project).
_API_DISABLED_PROJECT = {
    "google_oauth": " (the project of the OAuth client behind Connect Google)",
    "service_account": " (the project the service account belongs to)",
}


# --- Parsing -------------------------------------------------------------------

# /spreadsheets/d/ID/edit, or /spreadsheets/u/1/d/ID/... when signed in to
# several Google accounts.
_SPREADSHEET_ID = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
_BARE_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
_GID = re.compile(r"(?:^|[&#?])gid=(\d+)")


def parse_sheet_url(text: str) -> tuple[str, int | None]:
    """``(spreadsheet_id, gid)`` from a Sheets URL or a bare spreadsheet id.

    The gid comes from ``#gid=N`` (what the browser shows) or ``?gid=N``.
    Raises ``ValueError`` if no spreadsheet id can be found.
    """
    text = text.strip()
    if _BARE_ID.fullmatch(text):
        return text, None
    parts = urlsplit(text)
    match = _SPREADSHEET_ID.search(parts.path)
    if parts.hostname != "docs.google.com" or not match:
        raise ValueError("That doesn't look like a Google Sheets URL")
    gid = _GID.search("#" + parts.fragment) or _GID.search("?" + parts.query)
    return match.group(1), int(gid.group(1)) if gid else None


def quote_sheet_title(title: str) -> str:
    """A1 notation for a whole sheet: always quoted, so a title like ``A1``
    can't be read as a cell reference."""
    return "'" + title.replace("'", "''") + "'"


def spreadsheet_url(spreadsheet_id: str, gid: int | None) -> str:
    """The browser URL of a spreadsheet, opened at tab ``gid`` if given."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{quote(spreadsheet_id, safe='')}/edit"
    )
    return url + (f"#gid={gid}" if gid is not None else "")


# --- Tabs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Tab:
    """One tab (sheet) of a spreadsheet. ``gid`` is Google's ``sheetId``, the
    stable identity links track (D10). ``rows`` and ``columns`` are the grid
    size from ``gridProperties``, not the used range."""

    gid: int
    title: str
    index: int
    rows: int
    columns: int

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> Tab:
        """From a ``SheetProperties`` object. Google omits zero-valued
        fields (the first tab's ``sheetId`` and ``index`` are often absent)."""
        grid = props.get("gridProperties") or {}
        return cls(
            gid=int(props.get("sheetId", 0)),
            title=props.get("title", ""),
            index=int(props.get("index", 0)),
            rows=int(grid.get("rowCount", 0)),
            columns=int(grid.get("columnCount", 0)),
        )


def find_tab(tabs: list[Tab], gid: int) -> Tab | None:
    """The tab with this gid, or None. By gid only: titles can change (D10)."""
    for tab in tabs:
        if tab.gid == gid:
            return tab
    return None


# --- Errors --------------------------------------------------------------------


class SheetsError(Exception):
    """A non-2xx response from the Sheets API.

    ``status`` is the HTTP status code and ``reason`` Google's ErrorInfo
    reason (or None). ``message`` is safe to show the link's owner: Google's
    own message, with an actionable sentence in front for ``api_disabled``.
    ``kind`` is what went wrong, decided by code, ``error.status`` and
    ``reason`` (never the message text, D30). ``rows_written`` is how many
    rows an ``append`` wrote before it failed (0 for everything else).
    """

    def __init__(
        self,
        *,
        status: int,
        reason: str | None,
        message: str,
        kind: SheetsErrorKind,
        google_status: str | None = None,
        rows_written: int = 0,
    ):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.kind: SheetsErrorKind = kind
        self.google_status = google_status
        self.rows_written = rows_written

    @property
    def transient(self) -> bool:
        """True when retrying later may succeed (a 429 or a 5xx)."""
        return self.kind in ("rate_limited", "server")

    def __repr__(self) -> str:
        # No message: it may name the spreadsheet.
        return (
            f"<SheetsError {self.status} {self.kind}"
            f"{' ' + self.reason if self.reason else ''}>"
        )


def classify(
    code: int, google_status: str | None, reason: str | None
) -> SheetsErrorKind:
    """The ``SheetsError.kind`` for an HTTP code, ``error.status`` and
    ErrorInfo ``reason``. Reasons win over codes: ``SERVICE_DISABLED`` and
    ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` are both 403 ``PERMISSION_DENIED``."""
    if reason == SERVICE_DISABLED:
        return "api_disabled"
    if reason == ACCESS_TOKEN_SCOPE_INSUFFICIENT:
        return "scope"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "server"
    if code == 404:
        return "not_found"
    if code == 403 and (google_status == "PERMISSION_DENIED" or reason is None):
        return "not_shared"
    return "other"


def _error_body(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _reason(error: dict[str, Any]) -> str | None:
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if isinstance(detail, dict) and detail.get("@type") == ERROR_INFO:
            reason = detail.get("reason")
            return reason if isinstance(reason, str) and reason else None
    return None


def sheets_error(
    response: httpx2.Response, cred: Credential, rows_written: int = 0
) -> SheetsError:
    """The ``SheetsError`` for a failed Sheets response."""
    error = _error_body(response)
    google_status = error.get("status")
    if not isinstance(google_status, str):
        google_status = None
    reason = _reason(error)
    kind = classify(response.status_code, google_status, reason)
    message = error.get("message")
    if not isinstance(message, str) or not message:
        message = f"HTTP {response.status_code}"
    if kind == "api_disabled":
        project = _API_DISABLED_PROJECT.get(cred.info.type, "")
        message = (
            f"{API_DISABLED_MESSAGE.format(project=project)} Google said: {message}"
        )
    return SheetsError(
        status=response.status_code,
        reason=reason,
        message=message,
        kind=kind,
        google_status=google_status,
        rows_written=rows_written,
    )


# --- Calls ---------------------------------------------------------------------


def _spreadsheet(spreadsheet_id: str) -> str:
    return f"{SHEETS_API}/{quote(spreadsheet_id, safe='')}"


def _values(spreadsheet_id: str, tab: Tab, operation: str = "") -> str:
    return (
        f"{_spreadsheet(spreadsheet_id)}/values/"
        + quote(quote_sheet_title(tab.title), safe="")
        + operation
    )


async def _call(cred: Credential, method: str, url: str, **kwargs: Any) -> Any:
    response = await cred.request(method, url, **kwargs)
    if not response.is_success:
        raise sheets_error(response, cred)
    return response.json()


async def get_tabs(cred: Credential, spreadsheet_id: str) -> tuple[str, list[Tab]]:
    """``(spreadsheet title, tabs in display order)``."""
    data = await _call(
        cred,
        "GET",
        _spreadsheet(spreadsheet_id),
        params={"fields": "properties.title,sheets.properties"},
    )
    tabs = [
        Tab.from_properties(s.get("properties") or {}) for s in data.get("sheets", [])
    ]
    tabs.sort(key=lambda tab: tab.index)
    return (data.get("properties") or {}).get("title", ""), tabs


async def get_values(
    cred: Credential, spreadsheet_id: str, tab: Tab
) -> list[list[Any]]:
    """Every value on the tab, as Google returns them: trailing empty cells
    and rows trimmed, empty cells inside a row as ``""``. Numbers stay
    numbers (``UNFORMATTED_VALUE``) and dates arrive as their formatted text
    (``FORMATTED_STRING``) rather than serial numbers (D11)."""
    data = await _call(
        cred,
        "GET",
        _values(spreadsheet_id, tab),
        params={
            "valueRenderOption": "UNFORMATTED_VALUE",
            "dateTimeRenderOption": "FORMATTED_STRING",
        },
    )
    return data.get("values", [])


async def clear(cred: Credential, spreadsheet_id: str, tab: Tab) -> None:
    """Clear every value on the tab (formatting and the gid are kept)."""
    await _call(cred, "POST", _values(spreadsheet_id, tab, ":clear"), json={})


def split_rows(rows: list[list[Any]], size: int) -> list[list[list[Any]]]:
    """Split ``rows`` into appends of about ``size`` rows.

    A chunk never ends on a blank row (unless it's the last): ``values:append``
    finds the end of the existing table by its last non-empty row, so the next
    chunk would land on top of (or before) trailing blank rows.
    """
    chunks: list[list[list[Any]]] = []
    start = 0
    while start < len(rows):
        end = min(start + size, len(rows))
        while end < len(rows) and not any(v != "" for v in rows[end - 1]):
            end += 1
        chunks.append(rows[start:end])
        start = end
    return chunks


async def append(
    cred: Credential,
    spreadsheet_id: str,
    tab: Tab,
    values: list[list[Any]],
    *,
    chunk_rows: int = CHUNK_ROWS,
) -> int:
    """Append ``values`` below the tab's data, ``chunk_rows`` at a time, and
    return the number of rows written.

    ``valueInputOption=RAW``: values are stored as given, never parsed as
    formulas (no formula injection). ``insertDataOption=INSERT_ROWS`` grows
    the grid instead of overwriting anything. A failure part-way raises
    ``SheetsError`` with ``rows_written`` set to the rows already appended.
    """
    url = _values(spreadsheet_id, tab, ":append")
    written = 0
    for chunk in split_rows(values, chunk_rows):
        response = await cred.request(
            "POST",
            url,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": chunk},
        )
        if not response.is_success:
            raise sheets_error(response, cred, rows_written=written)
        written += len(chunk)
    return written


async def create_spreadsheet(cred: Credential, title: str) -> tuple[str, Tab]:
    """A new spreadsheet in the credential's Drive: ``(id, its first tab)``.
    The first tab's title is locale-dependent ("Sheet1", "Feuille 1", ...).
    Service accounts can't usefully do this (google-auth D28): callers
    refuse them before calling."""
    data = await _call(cred, "POST", SHEETS_API, json={"properties": {"title": title}})
    return data["spreadsheetId"], Tab.from_properties(data["sheets"][0]["properties"])


def header_format_requests(gid: int) -> list[dict[str, Any]]:
    """The ``batchUpdate`` requests that make row 1 bold and frozen (D14)."""
    return [
        {
            "repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]


async def format_header(cred: Credential, spreadsheet_id: str, tab: Tab) -> None:
    """Make the tab's header row bold and frozen, in one ``batchUpdate``."""
    await _call(
        cred,
        "POST",
        f"{_spreadsheet(spreadsheet_id)}:batchUpdate",
        json={"requests": header_format_requests(tab.gid)},
    )
