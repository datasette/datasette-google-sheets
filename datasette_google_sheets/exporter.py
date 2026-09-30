"""The export runner: read a table, view, stored query or SQL as an actor and
write it to a Google Sheets tab (D13, D14).

A production version of datasette-google-auth's
``samples/google_sheets_export.py``, extended to views, stored queries and
links (a stored spreadsheet id and tab gid).

**Reads** always go through ``datasette.client`` as the actor, so core
enforces view-table / view-query / execute-sql exactly as it would for the
actor's own JSON request (D5, D13). Tables and views are paged with
``_next`` (core pages views by offset) until the cell cap. A truncated query
result is refused, never exported partially.

**Writes** are ``RAW`` appends (``sheets.append``), so text like
``=IMPORTXML(...)`` stays literal. Replace is clear + append (D14); the tab's
gid never changes. A failure part-way through reports how many rows were
written (D14, D31).

No permission checks beyond reading as the actor, no status handling and no
run history: ``run_link`` (ticket 09) does those. Nothing here logs. Error
messages are for the link's owner (they may name the table, query or the
service account's email), never for logs, telemetry or events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from datasette_google_auth import GoogleAuthError

from . import sheets
from .config import get_config
from .sheets import SheetsError, SheetsErrorKind, Tab

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette_google_auth import Credential

    from .internal_db import Link

ExportErrorCode = Literal[
    "forbidden",
    "source_missing",
    "read_failed",
    "truncated",
    "too_large",
    "sa_cannot_create",
    "tab_missing",
    "partial",
]

# Title of a new spreadsheet for an SQL export with no title of its own.
DEFAULT_SQL_TITLE = "Datasette query"


class ExportError(Exception):
    """An export that failed for a reason with its own ``code``.

    ``message`` is for the link's owner. ``rows_written`` is how many data
    rows reached the sheet before a ``partial`` failure. For ``partial``,
    ``kind`` is the ``SheetsError`` kind when Google refused a chunk (None
    for a ``GoogleAuthError`` or transport error), and ``__cause__`` is the
    original exception, so ticket 09 can decide pause vs retry.
    ``share_with`` is the service account's email for ``sa_cannot_create``.

    ``SheetsError`` and ``GoogleAuthError`` raised before anything was
    written propagate unchanged, not as an ``ExportError``.
    """

    def __init__(
        self,
        code: ExportErrorCode,
        message: str,
        *,
        rows_written: int = 0,
        kind: SheetsErrorKind | None = None,
        share_with: str | None = None,
    ):
        super().__init__(message)
        self.code: ExportErrorCode = code
        self.message = message
        self.rows_written = rows_written
        self.kind = kind
        self.share_with = share_with

    def __repr__(self) -> str:
        # No message: it may name the table, query or an email.
        return f"<ExportError {self.code}{' ' + self.kind if self.kind else ''}>"


@dataclass(frozen=True)
class ExportResult:
    """A finished export. ``rows_read`` and ``rows_written`` count data rows
    (not the header); ``cells`` counts every cell written, header included.
    ``spreadsheet_id`` and ``gid`` are new after a ``new`` link's first run,
    for ticket 09 to persist; the titles are the latest seen, for display."""

    rows_read: int
    rows_written: int
    cells: int
    spreadsheet_id: str
    gid: int
    url: str
    spreadsheet_title: str | None
    sheet_title: str | None


# --- Values ------------------------------------------------------------------------


def cell_value(value: Any) -> Any:
    """A Datasette JSON value as a Sheets cell, safe to write ``RAW``: None is
    an empty cell, and a blob (``{"$base64": true, ...}``) or other JSON
    structure becomes its JSON text. Strings are never parsed as formulas."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float | str):
        return value
    return json.dumps(value)


def count_cells(columns: list[str], rows: list[list[Any]], *, header: bool) -> int:
    """Cells an export writes: every row times every column, counting the
    header row when there is one."""
    return (len(rows) + int(header)) * len(columns)


def check_cells(
    columns: list[str], rows: list[list[Any]], *, header: bool, max_cells: int
) -> int:
    """The cell count, or ``ExportError("too_large")`` over ``max_cells``."""
    cells = count_cells(columns, rows, header=header)
    if cells > max_cells:
        raise ExportError(
            "too_large",
            f"That's more than {max_cells:,} cells (rows × columns, counting the "
            "header row), the most an export writes (max_export_cells). "
            "Export fewer rows or columns.",
        )
    return cells


# --- Reading as the actor -------------------------------------------------------------

_SHAPE = {"_shape": "arrays", "_extra": "columns"}


def _source_name(link: Link) -> str:
    if link.source_kind in ("table", "view"):
        return f"{link.source_kind} {link.table_name!r}"
    if link.source_kind == "query":
        return f"query {link.query_name!r}"
    return "SQL query"


def _read_error(response: Any, link: Link) -> ExportError:
    """A failed read as an ``ExportError`` (D19: a 404 means the source is
    gone)."""
    what = _source_name(link)
    if response.status_code == 403:
        return ExportError(
            "forbidden",
            f"You don't have permission to read the {what} in {link.database_name!r}.",
        )
    if response.status_code == 404:
        return ExportError(
            "source_missing",
            f"The {what} in {link.database_name!r} no longer exists.",
        )
    try:
        detail = response.json().get("error")
    except (ValueError, AttributeError):
        detail = None
    return ExportError(
        "read_failed",
        f"Could not read the {what}: "
        + (
            detail
            if isinstance(detail, str) and detail
            else f"HTTP {response.status_code}"
        ),
    )


def _query_params(params: dict[str, Any] | None) -> dict[str, str]:
    """Named parameters as query-string args. ``sql`` and ``_``-prefixed
    keys are Datasette's own controls, never parameters."""
    return {
        key: "" if value is None else str(value)
        for key, value in (params or {}).items()
        if key != "sql" and not key.startswith("_")
    }


async def _get(datasette: Datasette, actor, link: Link, path: str, params: dict):
    response = await datasette.client.get(path, params=params, actor=actor)
    if response.status_code != 200:
        raise _read_error(response, link)
    return response.json()


async def read_rows(
    datasette: Datasette,
    actor: dict[str, Any] | None,
    link: Link,
    *,
    max_cells: int | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """``(columns, rows)`` of the link's source, read through
    ``datasette.client`` as ``actor`` so core enforces its permissions.

    Tables and views page with ``_next`` and stop once the rows read are past
    ``max_cells`` (default: ``max_export_cells``, header row counted when
    the link writes one); the caller then refuses the export. Queries
    return at most ``max_returned_rows`` rows, and a truncated result
    raises ``ExportError("truncated")`` (D13).

    Columns are what the JSON has: a rowid table includes ``rowid``, as in
    Datasette's CSV export.
    """
    if max_cells is None:
        max_cells = get_config(datasette).max_export_cells
    header = _header_row(link)
    if link.database_name not in datasette.databases:
        # datasette.urls raises KeyError for an unknown database.
        raise ExportError(
            "source_missing", f"The database {link.database_name!r} no longer exists."
        )

    if link.source_kind in ("table", "view"):
        assert link.table_name is not None
        path = datasette.urls.table(link.database_name, link.table_name, format="json")
        columns: list[str] = []
        rows: list[list[Any]] = []
        next_token = None
        while True:
            params = {**_SHAPE, "_size": "max"}
            if next_token is not None:
                params["_next"] = next_token
            data = await _get(datasette, actor, link, path, params)
            columns = data["columns"]
            rows.extend(data["rows"])
            next_token = data.get("next")
            if (
                next_token is None
                or count_cells(columns, rows, header=header) > max_cells
            ):
                return columns, rows

    if link.source_kind == "query":
        assert link.query_name is not None
        # A stored query is served at /<db>/<name>.json (core's table route
        # dispatches to QueryView), with named parameters as plain args.
        path = datasette.urls.query(link.database_name, link.query_name, format="json")
        params = {**_query_params(link.params), **_SHAPE}
    else:
        path = datasette.urls.database(link.database_name) + "/-/query.json"
        params = {**_query_params(link.params), **_SHAPE, "sql": link.sql or ""}
    data = await _get(datasette, actor, link, path, params)
    if data.get("truncated"):
        limit = datasette.setting("max_returned_rows")
        raise ExportError(
            "truncated",
            f"That query returned more than max_returned_rows ({limit:,}) rows, so "
            "the export would be incomplete. Save it as a SQL view to export it "
            f"in full, or raise max_returned_rows (currently {limit:,}).",
        )
    return data["columns"], data["rows"]


# --- Writing ---------------------------------------------------------------------------


def _header_row(link: Link) -> bool:
    return link.options.header_row if link.options is not None else True


def _new_title(link: Link) -> str:
    """A new spreadsheet's title: the one stored on the link, else the table,
    view or query name."""
    return (
        link.spreadsheet_title
        or link.table_name
        or link.query_name
        or DEFAULT_SQL_TITLE
    )


def needs_new_spreadsheet(link: Link) -> bool:
    """A ``new`` link whose spreadsheet hasn't been created yet (no id
    stored). Once ticket 09 stores the id, later runs replace that tab."""
    return link.mode == "new" and not link.spreadsheet_id


async def _append_counted(
    cred: Credential,
    spreadsheet_id: str,
    tab: Tab,
    values: list[list[Any]],
    *,
    chunk_rows: int,
    header: bool,
) -> int:
    """Append ``values`` chunk by chunk and return the sheet rows written.

    Counts chunks here (D31): ``SheetsError.rows_written`` covers a Google
    error part-way through, but a ``GoogleAuthError`` or transport error
    between chunks carries no count. Any failure after at least one data row
    landed raises ``ExportError("partial")``; before that, the original
    error propagates unchanged.
    """
    written = 0
    try:
        for chunk in sheets.split_rows(values, chunk_rows):
            # One request per chunk: chunk_rows=len(chunk) stops append()
            # re-splitting a chunk that was extended over blank rows.
            written += await sheets.append(
                cred, spreadsheet_id, tab, chunk, chunk_rows=len(chunk)
            )
    except Exception as error:
        if isinstance(error, SheetsError):
            written += error.rows_written
        data_rows = max(written - int(header), 0)
        if not data_rows:
            raise
        if isinstance(error, SheetsError):
            detail = error.message
        elif isinstance(error, GoogleAuthError):
            detail = str(error)
        else:
            # A transport error's text can carry the request URL.
            detail = "The connection to Google failed."
        raise ExportError(
            "partial",
            f"{data_rows:,} rows were written before the export failed. {detail}",
            rows_written=data_rows,
            kind=error.kind if isinstance(error, SheetsError) else None,
        ) from error
    return written


async def run_export(
    datasette: Datasette,
    link: Link,
    cred: Credential,
    actor: dict[str, Any] | None,
    *,
    chunk_rows: int = sheets.CHUNK_ROWS,
) -> ExportResult:
    """Export ``link``'s source to its tab, reading as ``actor``.

    - ``new`` before its spreadsheet exists: refuse a service account
      (``sa_cannot_create``, google-auth D28), else read, check the cap and
      create the spreadsheet, titled after the link. Afterwards a ``new``
      link behaves as ``replace`` on that tab.
    - ``replace``: clear the tab, then append (D14). ``append``: append only.
    - Make the header row bold and frozen when one was written, except in
      append mode.

    The cap is checked before any write. Raises ``ExportError`` (see its
    codes), or ``SheetsError`` / ``GoogleAuthError`` / transport errors
    unchanged when nothing was written.
    """
    header = _header_row(link)
    create = needs_new_spreadsheet(link)
    if create and cred.info.type == "service_account":
        email = cred.info.google_email
        raise ExportError(
            "sa_cannot_create",
            "Service accounts can't create new spreadsheets: the file would be "
            "owned by the service account, where you can't see it. Create the "
            f"spreadsheet yourself, share it with {email} as Editor, and export "
            "to that existing spreadsheet instead.",
            share_with=email,
        )

    max_cells = get_config(datasette).max_export_cells
    columns, rows = await read_rows(datasette, actor, link, max_cells=max_cells)
    cells = check_cells(columns, rows, header=header, max_cells=max_cells)
    values = [[cell_value(value) for value in row] for row in rows]
    if header:
        values.insert(0, list(columns))

    if create:
        spreadsheet_title = _new_title(link)
        spreadsheet_id, tab = await sheets.create_spreadsheet(cred, spreadsheet_title)
    else:
        spreadsheet_id = link.spreadsheet_id
        spreadsheet_title, tabs = await sheets.get_tabs(cred, spreadsheet_id)
        found = sheets.find_tab(tabs, link.sheet_gid)
        if found is None:
            raise ExportError(
                "tab_missing",
                "The tab this export writes to has been deleted from the spreadsheet.",
            )
        tab = found
        if link.mode != "append":
            await sheets.clear(cred, spreadsheet_id, tab)

    written = await _append_counted(
        cred, spreadsheet_id, tab, values, chunk_rows=chunk_rows, header=header
    )
    if header and link.mode != "append":
        await sheets.format_header(cred, spreadsheet_id, tab)

    return ExportResult(
        rows_read=len(rows),
        rows_written=max(written - int(header), 0),
        cells=cells,
        spreadsheet_id=spreadsheet_id,
        gid=tab.gid,
        url=sheets.spreadsheet_url(spreadsheet_id, tab.gid),
        spreadsheet_title=spreadsheet_title,
        sheet_title=tab.title,
    )
