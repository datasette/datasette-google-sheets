"""The import engine: turn a Google Sheets tab into table rows (D8-D12, D20).

``preview()`` feeds the import wizard's mapping step. ``run_import()`` runs
one import link: it fetches the tab, checks it against the link's stored
mapping, and writes the table in ONE ``execute_write_fn`` transaction.

This module is engine only. It does no permission checks and keeps no run
history or link status: ticket 09's ``run_link()`` does those as the acting
actor, persists what ``ImportResult`` reports (``last_hash``,
``created_schema``, refreshed titles) and decides whether a failure pauses
the link. Our writes never call ``allowed()`` (D7). ``actor`` is used only
to attribute the core events.

Failures raise ``ImporterError`` with a stable ``code``. ``SheetsError``
(``sheets.py``) and google-credentials's ``GoogleCredentialsError`` pass through
unchanged. Messages and ``data`` are for the link's owner: they may name the
table and its columns, never cell values. Nothing here logs.

Header normalisation and the records rule come from datasette-google-credentials's
``samples/google_sheets_import.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from datasette.events import (
    CreateTableEvent,
    DeleteRowEvent,
    InsertRowsEvent,
    UpsertRowsEvent,
)
from datasette.utils import escape_sqlite
from sqlite_utils import Database as SqliteUtilsDatabase
from sqlite_utils.utils import suggest_column_types

from .config import Config, get_config
from .internal_db import ColumnMapping, ColumnType, ImportMapping, Link
from .sheets import Tab, find_tab, get_tabs, get_values

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette_google_credentials import Credential

ImporterErrorCode = Literal[
    "database_missing",
    "tab_missing",
    "too_large",
    "headers_changed",
    "empty_sheet",
    "bad_key",
    "table_exists",
    "table_missing",
    "table_changed",
    "write_failed",
]

# How many sheet row numbers a bad_key error names, per problem.
MAX_ROWS_NAMED = 10

_PYTHON_TYPES: dict[ColumnType, type] = {"TEXT": str, "INTEGER": int, "REAL": float}


class ImporterError(Exception):
    """An import failed for a reason the link's owner can act on.

    ``code`` is stable (ticket 09 maps it to pause or retry, D17).
    ``message`` is owner-facing and never contains cell values. ``data`` is
    structured detail for the UI, e.g. the header diff for
    ``headers_changed``."""

    def __init__(
        self,
        code: ImporterErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code: ImporterErrorCode = code
        self.message = message
        self.data = data

    def __repr__(self) -> str:
        return f"<ImporterError {self.code}>"


# ----------------------------------------------------------------- the sheet


def column_names(header: list[Any], width: int) -> list[str]:
    """``width`` unique column names from a header row: trimmed, blanks become
    ``column_N`` (1-based position), duplicates (case-insensitive, as SQLite
    compares them) get ``_2``, ``_3``..."""
    names: list[str] = []
    seen: set[str] = set()
    for i in range(width):
        raw = header[i] if i < len(header) else None
        name = "" if raw is None else str(raw).strip()
        name = name or f"column_{i + 1}"
        candidate, n = name, 2
        while candidate.casefold() in seen:
            candidate, n = f"{name}_{n}", n + 1
        seen.add(candidate.casefold())
        names.append(candidate)
    return names


@dataclass(frozen=True)
class SheetData:
    """A tab's values as named records."""

    headers: list[str]
    """The normalised source headers, in sheet order."""
    records: list[dict[str, Any]]
    """Non-blank data rows keyed by header; empty cells are None."""
    row_numbers: list[int]
    """The 1-based sheet row number of each record, for error messages."""


def to_records(values: list[list[Any]], *, headers_row: bool) -> SheetData:
    """Normalise Sheets ``values``: empty cells -> None, short rows padded,
    blank rows dropped. Without a header row, columns are ``column_N``."""
    cells = [[None if value == "" else value for value in row] for row in values]
    header: list[Any] = []
    first = 1
    if headers_row and cells:
        header, cells = cells[0], cells[1:]
        first = 2
    width = max((len(row) for row in [header, *cells]), default=0)
    headers = column_names(header, width)
    records: list[dict[str, Any]] = []
    row_numbers: list[int] = []
    for i, row in enumerate(cells):
        if any(value is not None for value in row):
            padded = row + [None] * (width - len(row))
            records.append(dict(zip(headers, padded, strict=True)))
            row_numbers.append(first + i)
    return SheetData(headers, records, row_numbers)


# ------------------------------------------------------------------ coercion

_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_EXACT_FLOAT = 2**53


def coerce(value: Any, type: ColumnType) -> tuple[Any, bool]:
    """``(value, ok)``: ``value`` as ``type`` when that's lossless, else the
    value unchanged and ``ok=False`` (a type mismatch, D10). None is always
    fine.

    TEXT stores numbers as their text, which is what SQLite's TEXT affinity
    would do anyway; booleans become ``TRUE`` / ``FALSE`` as Sheets shows
    them. INTEGER takes whole numbers (and whole-number text); REAL takes
    any finite number (and numeric text)."""
    if value is None:
        return None, True
    if type == "TEXT":
        if isinstance(value, bool):
            return ("TRUE" if value else "FALSE"), True
        return (value if isinstance(value, str) else str(value)), True
    if isinstance(value, bool):
        return (int(value) if type == "INTEGER" else float(value)), True
    if type == "INTEGER":
        number: Any = value
        if isinstance(value, str):
            if not _INTEGER_TEXT.fullmatch(value):
                return value, False
            number = int(value)
        elif isinstance(value, float):
            if not value.is_integer():
                return value, False
            number = int(value)
        if isinstance(number, int) and _INT64_MIN <= number <= _INT64_MAX:
            return number, True
        return value, False
    # REAL
    if isinstance(value, int):
        if abs(value) <= _EXACT_FLOAT:
            return float(value), True
        return value, False
    if isinstance(value, float):
        return value, math.isfinite(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value, False
        return (number, True) if math.isfinite(number) else (value, False)
    return value, False


def suggest_types(
    headers: list[str], records: list[dict[str, Any]]
) -> dict[str, ColumnType]:
    """sqlite-utils' ``suggest_column_types`` over every record, as the
    mapping's TEXT / INTEGER / REAL. All-empty columns are TEXT."""
    suggested = suggest_column_types(records) if records else {}
    types: dict[str, ColumnType] = {}
    for header in headers:
        python_type = suggested.get(header, str)
        if python_type in (int, bool):
            types[header] = "INTEGER"
        elif python_type is float:
            types[header] = "REAL"
        else:
            types[header] = "TEXT"
    return types


def suggest_key(headers: list[str], records: list[dict[str, Any]]) -> str | None:
    """The first column named ``id`` (case-insensitive), else the first
    column, whose values are all non-blank and unique; else None."""
    ids = [h for h in headers if h.casefold() == "id"]
    for header in ids + [h for h in headers if h not in ids]:
        values = [record[header] for record in records]
        if (
            values
            and all(value is not None for value in values)
            and len(set(values)) == len(values)
        ):
            return header
    return None


# ------------------------------------------------------------------- preview


@dataclass(frozen=True)
class Preview:
    """What the import wizard shows before a mapping exists (D11)."""

    spreadsheet_title: str
    tab: Tab
    headers: list[str]
    """Normalised source headers, in sheet order."""
    records: list[dict[str, Any]]
    """The first ``rows`` records."""
    total_rows: int
    """Every non-blank data row on the tab."""
    types: dict[str, ColumnType]
    """Suggested type per header, from all rows."""
    key: str | None
    """Suggested key column (a header), or None."""
    cells: int
    """The tab's grid size (rows x columns): what D20's cap checks."""


def _check_size(tab: Tab, max_cells: int) -> None:
    cells = tab.rows * tab.columns
    if cells > max_cells:
        raise ImporterError(
            "too_large",
            f"This tab's grid is {tab.rows:,} rows by {tab.columns:,} columns"
            f" ({cells:,} cells), more than the import limit of {max_cells:,}"
            " cells. Delete unused rows and columns in the sheet, or ask an"
            " administrator to raise max_import_cells.",
            data={"cells": cells, "max_cells": max_cells},
        )


def _tab_missing() -> ImporterError:
    return ImporterError(
        "tab_missing",
        "The tab no longer exists in the spreadsheet. It may have been deleted.",
    )


async def preview(
    cred: Credential,
    spreadsheet_id: str,
    gid: int,
    *,
    headers_row: bool = True,
    rows: int | None = None,
    max_cells: int | None = None,
) -> Preview:
    """Fetch tab ``gid`` and suggest a mapping. ``rows`` defaults to the
    config default ``preview_rows`` (the wizard passes the configured one).
    With ``max_cells``, a tab over the cap raises ``too_large`` before its
    values are fetched (D20)."""
    if rows is None:
        rows = Config.model_fields["preview_rows"].default
    spreadsheet_title, tabs = await get_tabs(cred, spreadsheet_id)
    tab = find_tab(tabs, gid)
    if tab is None:
        raise _tab_missing()
    if max_cells is not None:
        _check_size(tab, max_cells)
    sheet = to_records(
        await get_values(cred, spreadsheet_id, tab), headers_row=headers_row
    )
    return Preview(
        spreadsheet_title=spreadsheet_title,
        tab=tab,
        headers=sheet.headers,
        records=sheet.records[:rows],
        total_rows=len(sheet.records),
        types=suggest_types(sheet.headers, sheet.records),
        key=suggest_key(sheet.headers, sheet.records),
        cells=tab.rows * tab.columns,
    )


# ---------------------------------------------------------------- the checks


def check_headers(headers: list[str], expected: list[str]) -> None:
    """Strict drift (D10): the normalised headers must be exactly the
    mapping's source headers. Order doesn't matter."""
    have, want = set(headers), set(expected)
    if have == want:
        return
    added = [h for h in headers if h not in want]
    removed = [h for h in expected if h not in have]
    parts = []
    if added:
        parts.append("new: " + ", ".join(added))
    if removed:
        parts.append("missing: " + ", ".join(removed))
    raise ImporterError(
        "headers_changed",
        "The sheet's columns have changed (" + "; ".join(parts) + ")."
        " Update the mapping to continue.",
        data={"added": added, "removed": removed},
    )


@dataclass(frozen=True)
class Mapped:
    columns: list[ColumnMapping]
    """The mapped (not skipped) columns, in mapping order."""
    records: list[dict[str, Any]]
    """Records keyed by table column name, values coerced."""
    warnings: list[str]


def _rows_text(rows: list[int]) -> str:
    shown = ", ".join(str(r) for r in rows[:MAX_ROWS_NAMED])
    return shown + (" and more" if len(rows) > MAX_ROWS_NAMED else "")


def apply_mapping(sheet: SheetData, mapping: ImportMapping) -> Mapped:
    """Skip, rename and coerce each record per the mapping. Type mismatches
    are stored as they are and counted per column (D10). With a key, a
    blank, duplicate or wrongly typed key fails with ``bad_key``, naming up
    to ``MAX_ROWS_NAMED`` sheet rows per problem (D9)."""
    columns = [c for c in mapping.columns if not c.skip]
    key_column = None
    if mapping.key is not None:
        key_column = next((c for c in columns if c.name == mapping.key), None)
        if key_column is None:
            raise ValueError("The mapping's key isn't one of its mapped columns")

    mismatches = dict.fromkeys((c.name for c in columns), 0)
    records: list[dict[str, Any]] = []
    blank: list[int] = []
    invalid: list[int] = []
    duplicate: set[int] = set()
    first_row_for_key: dict[Any, int] = {}
    for record, row_number in zip(sheet.records, sheet.row_numbers, strict=True):
        out: dict[str, Any] = {}
        for column in columns:
            value, ok = coerce(record.get(column.source), column.type)
            out[column.name] = value
            if column is key_column:
                if value is None:
                    blank.append(row_number)
                elif not ok:
                    invalid.append(row_number)
                else:
                    # Coerced keys of one column share a type, so plain
                    # equality is right.
                    seen = first_row_for_key.setdefault(value, row_number)
                    if seen != row_number:
                        duplicate.update((seen, row_number))
            elif not ok:
                mismatches[column.name] += 1
        records.append(out)

    if key_column is not None and (blank or invalid or duplicate):
        name = key_column.name
        problems = []
        if blank:
            problems.append(f"blank in sheet rows {_rows_text(blank)}")
        if duplicate:
            problems.append(f"repeated in sheet rows {_rows_text(sorted(duplicate))}")
        if invalid:
            problems.append(
                f"not {key_column.type} in sheet rows {_rows_text(invalid)}"
            )
        raise ImporterError(
            "bad_key",
            f"The key column {name!r} must be filled in and unique, but it is "
            + "; ".join(problems)
            + ".",
            data={
                "key": name,
                "blank_rows": blank[:MAX_ROWS_NAMED],
                "duplicate_rows": sorted(duplicate)[:MAX_ROWS_NAMED],
                "invalid_rows": invalid[:MAX_ROWS_NAMED],
            },
        )

    warnings = [
        f"Column {c.name!r}: {mismatches[c.name]}"
        f" {'value' if mismatches[c.name] == 1 else 'values'} not {c.type},"
        " stored as they are"
        for c in columns
        if mismatches[c.name]
    ]
    return Mapped(columns, records, warnings)


def data_hash(records: list[dict[str, Any]], mapping: ImportMapping) -> str:
    """sha256 of the canonical JSON of the mapped records plus the mapping
    (D12). Row order counts: it decides rowids."""
    payload = json.dumps(
        {"mapping": mapping.model_dump(mode="json"), "records": records},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- the target

Strategy = Literal["create", "append", "replace", "upsert"]


def strategy_for(link: Link) -> Strategy:
    """How this run writes (D8, D9). A ``create`` link creates its table on
    the first run; every later run (a re-run, or a sync) upserts by key, or
    replaces without one."""
    mapping = link.mapping
    assert mapping is not None
    if link.mode == "create":
        if not link.created_table:
            return "create"
        return "upsert" if mapping.key else "replace"
    if link.mode not in ("append", "replace", "upsert"):
        raise ValueError(f"Not an import mode: {link.mode!r}")
    return link.mode


def stored_table_name(conn: sqlite3.Connection, name: str) -> str | None:
    """The stored name of table ``name``: SQLite table names are
    case-insensitive, and sqlite-utils' ``Table.exists()`` is not."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name = ? COLLATE NOCASE",
        [name],
    ).fetchone()
    return row[0] if row else None


def check_table(
    conn: sqlite3.Connection, table: str, columns: list[str], *, new: bool
) -> str | None:
    """The target must match expectations. ``new``: it must not exist yet
    (``table_exists``). Otherwise it must exist (``table_missing``, D19) and
    have every mapped column, case-insensitively (``table_changed``, D10).
    Returns the stored table name (None for ``new``)."""
    stored = stored_table_name(conn, table)
    if new:
        if stored is not None:
            raise ImporterError(
                "table_exists",
                f"A table called {stored!r} already exists. Choose another name,"
                " or import into the existing table.",
                data={"table": stored},
            )
        return None
    if stored is None:
        raise ImporterError(
            "table_missing",
            f"The table {table!r} doesn't exist any more. It may have been"
            " dropped or renamed.",
            data={"table": table},
        )
    # Table.columns is PRAGMA table_info, behind a case-sensitive exists():
    # so look it up by its stored name, then compare casefolded.
    have = {c.name.casefold() for c in SqliteUtilsDatabase(conn).table(stored).columns}
    missing = [c for c in columns if c.casefold() not in have]
    if missing:
        raise ImporterError(
            "table_changed",
            f"The table {stored!r} has no column"
            f"{'s' if len(missing) != 1 else ''} " + ", ".join(missing) + ".",
            data={"table": stored, "missing_columns": missing},
        )
    return stored


# ----------------------------------------------------------------- the write


@dataclass(frozen=True)
class WriteCounts:
    table: str
    rows_written: int
    added: int
    changed: int
    removed: int
    created_schema: str | None = None


def _is_transient(error: sqlite3.Error) -> bool:
    message = str(error).casefold()
    return isinstance(error, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )


def table_writer(
    *,
    database: str,
    table: str,
    strategy: Strategy,
    mapped: Mapped,
    key: str | None,
    actor: dict[str, Any] | None,
):
    """The ``execute_write_fn`` callback. Everything happens in its one
    transaction (Datasette's ``BEGIN IMMEDIATE``; sqlite-utils >= 4 nests
    its own writes as savepoints), so a failure leaves the table as it was.
    Datasette sends the tracked events only after the commit."""
    columns = [c.name for c in mapped.columns]
    records = mapped.records

    def import_sheet_rows(conn, track_event) -> WriteCounts:
        db = SqliteUtilsDatabase(conn)
        if strategy == "create":
            check_table(conn, table, columns, new=True)
            target = db.table(table)
            target.create(
                {c.name: _PYTHON_TYPES[c.type] for c in mapped.columns}, pk=key
            )
            schema = target.schema
            track_event(
                CreateTableEvent(
                    actor=actor, database=database, table=target.name, schema=schema
                )
            )
            target.insert_all(records)
            _insert_event(track_event, actor, database, target.name, len(records))
            return WriteCounts(
                table=target.name,
                rows_written=len(records),
                added=len(records),
                changed=0,
                removed=0,
                created_schema=schema,
            )

        stored = check_table(conn, table, columns, new=False)
        assert stored is not None
        target = db.table(stored)
        if strategy == "append":
            target.insert_all(records)
            _insert_event(track_event, actor, database, stored, len(records))
            return WriteCounts(stored, len(records), len(records), 0, 0)
        if strategy == "replace":
            removed = conn.execute(f"DELETE FROM {escape_sqlite(stored)}").rowcount
            target.insert_all(records)
            _insert_event(track_event, actor, database, stored, len(records))
            return WriteCounts(stored, len(records), len(records), 0, removed)
        assert key is not None
        return _upsert(conn, target, database, key, mapped, actor, track_event)

    return import_sheet_rows


def _insert_event(track_event, actor, database: str, table: str, rows: int) -> None:
    track_event(
        InsertRowsEvent(
            actor=actor,
            database=database,
            table=table,
            num_rows=rows,
            ignore=False,
            replace=False,
        )
    )


def _upsert(conn, target, database: str, key: str, mapped: Mapped, actor, track_event):
    """Upsert by key, then delete the rows whose keys left the sheet (D9).

    Matching is on the key column (coerced as the mapping says), and rows
    are updated in place by rowid, so rowids and row URLs stay stable and no
    UNIQUE constraint is needed. ``changed`` counts only rows whose values
    differ: SQLite compares them, with the column's affinity applied to the
    new value (``IS`` handles NULLs). Deletes go one rowid per statement,
    so SQLite's bound-variable limit never applies."""
    table = target.name
    key_type = next(c.type for c in mapped.columns if c.name == key)
    pks = target.pks  # ["rowid"] for a rowid table
    select = ", ".join(escape_sqlite(c) for c in [key, *pks])
    existing: dict[Any, int] = {}
    pk_values: dict[int, list[Any]] = {}
    for rowid, stored_key, *pk in conn.execute(
        f"SELECT rowid, {select} FROM {escape_sqlite(table)}"
    ):
        pk_values[rowid] = pk
        # A table with repeated keys keeps the first; the rest are removed.
        existing.setdefault(coerce(stored_key, key_type)[0], rowid)

    others = [c for c in mapped.columns if c.name != key]
    updates: list[list[Any]] = []
    inserts: list[dict[str, Any]] = []
    kept: set[int] = set()
    for record in mapped.records:
        rowid = existing.get(record[key])
        if rowid is None:
            inserts.append(record)
            continue
        kept.add(rowid)
        values = [record[c.name] for c in others]
        updates.append([*values, rowid, *values])
    removed = [rowid for rowid in pk_values if rowid not in kept]

    quoted = escape_sqlite(table)
    conn.executemany(
        f"DELETE FROM {quoted} WHERE rowid = ?", [[rowid] for rowid in removed]
    )
    changed = 0
    if others and updates:
        assignments = ", ".join(f"{escape_sqlite(c.name)} = ?" for c in others)
        same = " AND ".join(f"{escape_sqlite(c.name)} IS ?" for c in others)
        changed = conn.executemany(
            f"UPDATE {quoted} SET {assignments} WHERE rowid = ? AND NOT ({same})",
            updates,
        ).rowcount
    if inserts:
        target.insert_all(inserts)

    for rowid in removed:
        track_event(
            DeleteRowEvent(
                actor=actor, database=database, table=table, pks=pk_values[rowid]
            )
        )
    if inserts or changed:
        track_event(
            UpsertRowsEvent(
                actor=actor,
                database=database,
                table=table,
                num_rows=len(inserts) + changed,
            )
        )
    return WriteCounts(
        table=table,
        rows_written=len(inserts) + changed,
        added=len(inserts),
        changed=changed,
        removed=len(removed),
    )


# ------------------------------------------------------------------- the run


@dataclass(frozen=True)
class ImportResult:
    """What a run did. Ticket 09 records it on the run row and persists
    ``hash`` (as ``last_hash``), ``created_table`` / ``created_schema`` and
    the refreshed titles on the link."""

    status: Literal["success", "no_change"]
    rows_read: int
    rows_written: int
    added: int
    changed: int
    removed: int
    cells: int
    """Cells read (data rows x columns)."""
    hash: str
    table: str
    """The table's stored name."""
    spreadsheet_title: str
    sheet_title: str
    warnings: list[str] = field(default_factory=list)
    created_table: bool = False
    """True when this run created the table."""
    created_schema: str | None = None
    """The new table's ``sqlite_master`` SQL, when this run created it."""


async def run_import(
    datasette: Datasette,
    link: Link,
    cred: Credential,
    *,
    actor: dict[str, Any] | None = None,
    force: bool = False,
) -> ImportResult:
    """Run import link ``link`` with credential ``cred``.

    ``actor`` (the acting actor, D5) is only the core events' actor. With
    ``force``, an unchanged sheet is written anyway (D12's "Force full
    sync"). Raises ``ImporterError``, ``SheetsError`` or ``GoogleCredentialsError``.
    """
    mapping = link.mapping
    if link.direction != "import" or mapping is None or not link.table_name:
        raise ValueError("Not an import link with a mapping")
    table = link.table_name
    strategy = strategy_for(link)
    if strategy == "upsert" and not mapping.key:
        raise ImporterError(
            "bad_key", "Upserting needs a key column. Choose one in the mapping."
        )
    try:
        db = datasette.get_database(link.database_name)
    except KeyError:
        raise ImporterError(
            "database_missing",
            f"The database {link.database_name!r} isn't attached any more.",
        ) from None

    # 1-2. The tab by gid, and the size cap before any values are fetched.
    spreadsheet_title, tabs = await get_tabs(cred, link.spreadsheet_id)
    tab = find_tab(tabs, link.sheet_gid)
    if tab is None:
        raise _tab_missing()
    _check_size(tab, get_config(datasette).max_import_cells)

    # 3-4. Values, normalised; strict headers; never import an empty sheet.
    values = await get_values(cred, link.spreadsheet_id, tab)
    if not values:
        raise _empty_sheet()
    sheet = to_records(values, headers_row=mapping.headers_row)
    check_headers(sheet.headers, mapping.source_headers)
    if not sheet.records:
        raise _empty_sheet()

    # 5-6. The mapping, then the key rules.
    mapped = apply_mapping(sheet, mapping)
    columns = [c.name for c in mapped.columns]
    digest = data_hash(mapped.records, mapping)

    def result(status, counts: WriteCounts, **extra) -> ImportResult:
        return ImportResult(
            status=status,
            rows_read=len(mapped.records),
            rows_written=counts.rows_written,
            added=counts.added,
            changed=counts.changed,
            removed=counts.removed,
            cells=len(mapped.records) * len(sheet.headers),
            hash=digest,
            table=counts.table,
            spreadsheet_title=spreadsheet_title,
            sheet_title=tab.title,
            warnings=mapped.warnings,
            **extra,
        )

    # 7. Skip unchanged data (D12). Still check the table, so a dropped or
    # altered table is noticed (D19, D10) even when the sheet is unchanged.
    if strategy != "create" and not force and digest == link.last_hash:

        def check_import_table(conn) -> str | None:
            return check_table(conn, table, columns, new=False)

        stored = await db.execute_fn(check_import_table)
        return result("no_change", WriteCounts(stored or table, 0, 0, 0, 0))

    # 8. One transaction.
    write = table_writer(
        database=link.database_name,
        table=table,
        strategy=strategy,
        mapped=mapped,
        key=mapping.key,
        actor=actor,
    )
    try:
        counts: WriteCounts = await db.execute_write_fn(write)
    except sqlite3.Error as error:
        # SQLite's messages name tables, columns and constraints, never the
        # bound values.
        raise ImporterError(
            "write_failed",
            f"Couldn't write to the table {table!r}, so it was left unchanged."
            f" SQLite said: {error}",
            data={"transient": _is_transient(error)},
        ) from None
    if strategy == "create":
        return result(
            "success",
            counts,
            created_table=True,
            created_schema=counts.created_schema,
        )
    return result("success", counts)


def _empty_sheet() -> ImporterError:
    return ImporterError(
        "empty_sheet",
        "The tab has no data rows. Nothing was imported, and the table was"
        " left as it was.",
    )
