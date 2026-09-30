"""Data access for the links and runs tables in Datasette's internal DB.

Reads use ``db.execute()``; writes use ``execute_write_fn()`` with named
inner functions, because Datasette labels each write's ``db.query`` span with
the callback's ``__qualname__`` (a lambda would show up as ``<lambda>``).
This follows datasette-google-auth (its HANDOFF decision 1).

Rows come back as ``Link`` / ``Run`` Pydantic models with the JSON columns
parsed and the 0/1 columns as bools, at this boundary, so nothing downstream
has to ``json.loads`` a column (the lesson of datasette-cron's ticket 10).

No permission checks here: callers decide who may see or change a link
(``permissions.py``). No logging either: link rows hold spreadsheet ids and
titles, which must never reach logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator
from ulid import ULID

from .config import Config, get_config

if TYPE_CHECKING:
    import sqlite3

    from datasette.app import Datasette
    from datasette.database import Database

LINKS = "datasette_google_sheets_links"
RUNS = "datasette_google_sheets_runs"

Direction = Literal["import", "export"]
ImportMode = Literal["create", "append", "replace", "upsert"]
ExportMode = Literal["new", "replace", "append"]
SourceKind = Literal["table", "view", "query", "sql"]
LinkStatus = Literal["ok", "error", "paused"]
ColumnType = Literal["TEXT", "INTEGER", "REAL"]
RunTrigger = Literal["manual", "scheduled"]
RunStatus = Literal["running", "success", "no_change", "error"]
FinishedRunStatus = Literal["success", "no_change", "error"]

IMPORT_MODES: tuple[str, ...] = get_args(ImportMode)
EXPORT_MODES: tuple[str, ...] = get_args(ExportMode)

# Written by mark_abandoned_runs() for runs a crashed process left running.
ABANDONED = "abandoned"


def _now() -> str:
    """ISO 8601 UTC with milliseconds, the same shape as the schema's
    ``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` defaults."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------- JSON shapes


class ColumnMapping(BaseModel):
    """One sheet column's mapping onto the table (D11)."""

    model_config = ConfigDict(frozen=True)

    source: str
    """The normalised sheet header."""
    name: str
    """The table column it maps to."""
    type: ColumnType
    skip: bool = False


class ImportMapping(BaseModel):
    """An import link's ``mapping`` column (D10/D11)."""

    model_config = ConfigDict(frozen=True)

    headers_row: bool = True
    source_headers: list[str]
    """The normalised headers the mapping was built from; a run fails if the
    fetched headers differ (D10)."""
    columns: list[ColumnMapping]
    key: str | None = None
    """The key column's table name, or None for "no key (rowid)" (D9)."""


class ExportOptions(BaseModel):
    """An export link's ``options`` column."""

    model_config = ConfigDict(frozen=True)

    header_row: bool = True


# ---------------------------------------------------------------------- rows

# Columns stored as JSON text.
_LINK_JSON = ("params", "mapping", "options", "status_data")


class Link(BaseModel):
    """One row of ``datasette_google_sheets_links``. Satisfies
    ``permissions.LinkOwner``."""

    model_config = ConfigDict(frozen=True)

    id: str
    direction: Direction
    mode: ImportMode | ExportMode
    owner_id: str
    credential_id: str
    database_name: str
    table_name: str | None
    source_kind: SourceKind | None
    query_name: str | None
    sql: str | None
    params: dict[str, Any] | None
    spreadsheet_id: str
    sheet_gid: int
    spreadsheet_title: str | None
    sheet_title: str | None
    mapping: ImportMapping | None
    options: ExportOptions | None
    interval_minutes: int | None
    created_table: bool
    created_schema: str | None
    enabled: bool
    status: LinkStatus
    status_code: str | None
    status_detail: str | None
    status_data: dict[str, Any] | None
    consecutive_failures: int
    last_run_at: str | None
    last_success_at: str | None
    last_hash: str | None
    created_at: str
    updated_at: str

    @property
    def scheduled(self) -> bool:
        return self.interval_minutes is not None

    @property
    def synced(self) -> bool:
        """A scheduled import is a synced table (D8)."""
        return self.direction == "import" and self.interval_minutes is not None

    @field_validator(*_LINK_JSON, mode="before")
    @classmethod
    def _json_columns(cls, value):
        return _parse_json(value)


class Run(BaseModel):
    """One row of ``datasette_google_sheets_runs``."""

    model_config = ConfigDict(frozen=True)

    id: int
    link_id: str
    started_at: str
    finished_at: str | None
    trigger: RunTrigger
    actor_id: str | None
    status: RunStatus
    rows_read: int | None
    rows_written: int | None
    added: int | None
    changed: int | None
    removed: int | None
    cells: int | None
    warnings: list[str]
    error_code: str | None
    error_message: str | None
    hash: str | None

    @field_validator("warnings", mode="before")
    @classmethod
    def _json_warnings(cls, value):
        return _parse_json(value)


# ---------------------------------------------------------------- exceptions


class LinkNotFound(Exception):
    pass


class LinkConflict(Exception):
    """A write would break one of the one-link-per-thing rules. Messages
    never include spreadsheet ids or titles; ``existing_id`` is the link in
    the way."""

    def __init__(self, message: str, existing_id: str):
        super().__init__(message)
        self.existing_id = existing_id


class ExportTabTaken(LinkConflict):
    """The tab already has an export link (D14)."""


class TableAlreadySynced(LinkConflict):
    """The table already has a synced import (D8)."""


# ------------------------------------------------------------------- helpers

# Fields update_link() refuses: identity, the owner (no reassigning in v1,
# D15) and the timestamps it manages itself.
_IMMUTABLE = frozenset({"id", "direction", "owner_id", "created_at", "updated_at"})
_LINK_FIELDS = frozenset(Link.model_fields)


def _validate_link(link: Link) -> None:
    """Cross-field rules the column types can't express."""
    if link.direction == "import":
        if link.mode not in IMPORT_MODES:
            raise ValueError(f"Invalid import mode: {link.mode!r}")
        if link.source_kind is not None:
            raise ValueError("Import links have no source_kind")
        if not link.table_name:
            raise ValueError("Import links need a table_name")
    else:
        if link.mode not in EXPORT_MODES:
            raise ValueError(f"Invalid export mode: {link.mode!r}")
        if link.source_kind is None:
            raise ValueError("Export links need a source_kind")
        if link.source_kind in ("table", "view") and not link.table_name:
            raise ValueError("Table and view exports need a table_name")
        if link.source_kind == "query" and not link.query_name:
            raise ValueError("Query exports need a query_name")
        if link.source_kind == "sql" and not link.sql:
            raise ValueError("SQL exports need sql")
    if link.interval_minutes is not None:
        if link.interval_minutes < 1:
            raise ValueError("interval_minutes must be at least 1")
        # No scheduled append, for imports (D8) or exports (D14).
        if link.mode == "append":
            raise ValueError("Append links can't be scheduled")


def _link_values(link: Link) -> dict[str, Any]:
    """The link as column values: JSON columns as text, bools as 0/1."""
    values = link.model_dump(mode="json")
    for column in _LINK_JSON:
        if values[column] is not None:
            values[column] = json.dumps(values[column])
    values["created_table"] = int(link.created_table)
    values["enabled"] = int(link.enabled)
    return values


def _check_conflicts(conn: sqlite3.Connection, link: Link) -> None:
    """Raise a ``LinkConflict`` before the unique indexes would. Runs inside
    the write transaction (BEGIN IMMEDIATE), so it can't race another write."""
    # spreadsheet_id '' is a new-spreadsheet export before its first run
    # (D33): it has no tab yet, so it can't take one.
    if link.direction == "export" and link.spreadsheet_id != "":
        row = conn.execute(
            f"SELECT id FROM {LINKS} WHERE direction = 'export'"
            " AND spreadsheet_id = ? AND sheet_gid = ? AND id != ?",
            [link.spreadsheet_id, link.sheet_gid, link.id],
        ).fetchone()
        if row:
            raise ExportTabTaken("This tab already has an export link", row[0])
    if link.synced:
        row = conn.execute(
            f"SELECT id FROM {LINKS} WHERE direction = 'import'"
            " AND interval_minutes IS NOT NULL"
            " AND database_name = ? AND table_name = ? AND id != ?",
            [link.database_name, link.table_name, link.id],
        ).fetchone()
        if row:
            raise TableAlreadySynced("This table is already synced", row[0])


def _prune(conn: sqlite3.Connection, link_id: str, keep: int) -> int:
    cursor = conn.execute(
        f"DELETE FROM {RUNS} WHERE link_id = ? AND id NOT IN ("
        f"  SELECT id FROM {RUNS} WHERE link_id = ? ORDER BY id DESC LIMIT ?"
        ")",
        [link_id, link_id, keep],
    )
    return cursor.rowcount


class InternalDB:
    def __init__(
        self, internal_db: Database, *, runs_retain_per_link: int | None = None
    ):
        self.db = internal_db
        self.runs_retain_per_link = (
            runs_retain_per_link
            if runs_retain_per_link is not None
            else Config.model_fields["runs_retain_per_link"].default
        )

    @classmethod
    def for_datasette(cls, datasette: Datasette) -> InternalDB:
        """The internal DB with the plugin config's run retention."""
        return cls(
            datasette.get_internal_database(),
            runs_retain_per_link=get_config(datasette).runs_retain_per_link,
        )

    # ----------------------------------------------------------- link reads

    async def get_link(self, id: str) -> Link | None:
        result = await self.db.execute(f"SELECT * FROM {LINKS} WHERE id = ?", [id])
        row = result.first()
        return Link.model_validate(dict(row)) if row else None

    async def list_links(self, owner_id: str | None = None) -> list[Link]:
        """Links, newest first (ULIDs sort by time); only ``owner_id``'s if
        given."""
        sql = f"SELECT * FROM {LINKS}"
        params: list[Any] = []
        if owner_id is not None:
            sql += " WHERE owner_id = ?"
            params.append(owner_id)
        result = await self.db.execute(sql + " ORDER BY id DESC", params)
        return [Link.model_validate(dict(row)) for row in result.rows]

    async def synced_tables(self) -> set[tuple[str, str]]:
        """``(database_name, table_name)`` of every synced table, paused or
        not: the lock lasts until unlink (D7, D8)."""
        result = await self.db.execute(
            f"SELECT database_name, table_name FROM {LINKS}"
            " WHERE direction = 'import' AND interval_minutes IS NOT NULL"
        )
        return {(row[0], row[1]) for row in result.rows}

    async def scheduled_links(self) -> list[Link]:
        """Every link with an interval, enabled or not, oldest first. The
        startup reconcile (D4) needs the disabled ones too, to tell a paused
        link's task from an orphan."""
        result = await self.db.execute(
            f"SELECT * FROM {LINKS} WHERE interval_minutes IS NOT NULL ORDER BY id"
        )
        return [Link.model_validate(dict(row)) for row in result.rows]

    # ---------------------------------------------------------- link writes

    async def create_link(
        self,
        *,
        direction: Direction,
        mode: ImportMode | ExportMode,
        owner_id: str,
        credential_id: str,
        database_name: str,
        spreadsheet_id: str,
        sheet_gid: int,
        table_name: str | None = None,
        source_kind: SourceKind | None = None,
        query_name: str | None = None,
        sql: str | None = None,
        params: dict[str, Any] | None = None,
        spreadsheet_title: str | None = None,
        sheet_title: str | None = None,
        mapping: ImportMapping | dict[str, Any] | None = None,
        options: ExportOptions | dict[str, Any] | None = None,
        interval_minutes: int | None = None,
        created_table: bool = False,
        created_schema: str | None = None,
    ) -> Link:
        """Insert a link with a fresh ULID.

        Raises ``ValueError`` for an invalid combination, ``ExportTabTaken``
        if the tab already has an export link, and ``TableAlreadySynced`` if
        a synced import already targets the table.
        """
        now = _now()
        link = Link.model_validate(
            {
                "id": str(ULID()),
                "direction": direction,
                "mode": mode,
                "owner_id": owner_id,
                "credential_id": credential_id,
                "database_name": database_name,
                "table_name": table_name,
                "source_kind": source_kind,
                "query_name": query_name,
                "sql": sql,
                "params": params,
                "spreadsheet_id": spreadsheet_id,
                "sheet_gid": sheet_gid,
                "spreadsheet_title": spreadsheet_title,
                "sheet_title": sheet_title,
                "mapping": mapping,
                "options": options,
                "interval_minutes": interval_minutes,
                "created_table": created_table,
                "created_schema": created_schema,
                "enabled": True,
                "status": "ok",
                "status_code": None,
                "status_detail": None,
                "status_data": None,
                "consecutive_failures": 0,
                "last_run_at": None,
                "last_success_at": None,
                "last_hash": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        _validate_link(link)
        values = _link_values(link)

        def insert_link(conn):
            _check_conflicts(conn, link)
            conn.execute(
                f"INSERT INTO {LINKS} ({', '.join(values)})"
                f" VALUES ({', '.join(':' + name for name in values)})",
                values,
            )

        await self.db.execute_write_fn(insert_link)
        return link

    async def update_link(self, id: str, /, **fields: Any) -> Link | None:
        """Change any link fields except ``id``, ``direction``, ``owner_id``
        and the timestamps. Bumps ``updated_at``. Returns the updated link,
        or None if there is no such link.

        Validates the result as ``create_link`` does, raising ``ValueError``
        or a ``LinkConflict`` (e.g. converting to synced when the table
        already has a sync).
        """
        unknown = set(fields) - _LINK_FIELDS
        if unknown:
            raise ValueError(f"Unknown link fields: {sorted(unknown)}")
        immutable = set(fields) & _IMMUTABLE
        if immutable:
            raise ValueError(f"Link fields can't be changed: {sorted(immutable)}")

        def update_link_row(conn) -> Link | None:
            row = conn.execute(f"SELECT * FROM {LINKS} WHERE id = ?", [id]).fetchone()
            if row is None:
                return None
            current = Link.model_validate(dict(row))
            link = Link.model_validate(
                {**current.model_dump(), **fields, "updated_at": _now()}
            )
            _validate_link(link)
            _check_conflicts(conn, link)
            values = _link_values(link)
            columns = [*fields, "updated_at"]
            conn.execute(
                f"UPDATE {LINKS} SET"
                f" {', '.join(f'{name} = :{name}' for name in columns)}"
                " WHERE id = :id",
                {name: values[name] for name in [*columns, "id"]},
            )
            return link

        return await self.db.execute_write_fn(update_link_row)

    async def delete_link(self, id: str) -> bool:
        """Delete a link and its runs. Never touches the table (D19)."""

        def delete_link_and_runs(conn) -> bool:
            # The FK's ON DELETE CASCADE is inert without PRAGMA
            # foreign_keys=ON, which the internal DB doesn't set.
            conn.execute(f"DELETE FROM {RUNS} WHERE link_id = ?", [id])
            return conn.execute(f"DELETE FROM {LINKS} WHERE id = ?", [id]).rowcount > 0

        return await self.db.execute_write_fn(delete_link_and_runs)

    # ------------------------------------------------------------------ runs

    async def start_run(
        self, link_id: str, *, trigger: RunTrigger, actor_id: str | None
    ) -> Run:
        """Record a ``running`` run and prune the link's history to
        ``runs_retain_per_link`` rows, in one transaction. Raises
        ``LinkNotFound`` for an unknown link."""
        if trigger not in get_args(RunTrigger):
            raise ValueError(f"Invalid trigger: {trigger!r}")
        keep = self.runs_retain_per_link

        def insert_run(conn) -> int:
            if not conn.execute(
                f"SELECT 1 FROM {LINKS} WHERE id = ?", [link_id]
            ).fetchone():
                raise LinkNotFound(link_id)
            cursor = conn.execute(
                f'INSERT INTO {RUNS} (link_id, started_at, "trigger", actor_id)'
                " VALUES (?, ?, ?, ?)",
                [link_id, _now(), trigger, actor_id],
            )
            _prune(conn, link_id, keep)
            return cursor.lastrowid

        run_id = await self.db.execute_write_fn(insert_run)
        run = await self._get_run(run_id)
        if run is None:
            raise RuntimeError(f"Run {run_id} vanished after write")
        return run

    async def finish_run(
        self,
        run_id: int,
        *,
        status: FinishedRunStatus,
        rows_read: int | None = None,
        rows_written: int | None = None,
        added: int | None = None,
        changed: int | None = None,
        removed: int | None = None,
        cells: int | None = None,
        warnings: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        hash: str | None = None,
    ) -> Run | None:
        """Close a run. Returns None if it's gone (pruned or its link
        deleted meanwhile). ``warnings`` and ``error_message`` are shown to
        the owner: never put cell values or tokens in them."""
        if status not in get_args(FinishedRunStatus):
            raise ValueError(f"Invalid run status: {status!r}")

        def finish_run_row(conn) -> bool:
            cursor = conn.execute(
                f"UPDATE {RUNS} SET finished_at = ?, status = ?, rows_read = ?,"
                " rows_written = ?, added = ?, changed = ?, removed = ?, cells = ?,"
                " warnings = ?, error_code = ?, error_message = ?, hash = ?"
                " WHERE id = ?",
                [
                    _now(),
                    status,
                    rows_read,
                    rows_written,
                    added,
                    changed,
                    removed,
                    cells,
                    json.dumps(list(warnings or [])),
                    error_code,
                    error_message,
                    hash,
                    run_id,
                ],
            )
            return cursor.rowcount > 0

        if not await self.db.execute_write_fn(finish_run_row):
            return None
        return await self._get_run(run_id)

    async def list_runs(self, link_id: str, limit: int = 20) -> list[Run]:
        """The link's runs, newest first."""
        result = await self.db.execute(
            f"SELECT * FROM {RUNS} WHERE link_id = ? ORDER BY id DESC LIMIT ?",
            [link_id, limit],
        )
        return [Run.model_validate(dict(row)) for row in result.rows]

    async def prune_runs(self, link_id: str, keep: int) -> int:
        """Delete all but the newest ``keep`` runs of a link. Returns how
        many were deleted."""
        if keep < 0:
            raise ValueError("keep must not be negative")

        def prune_link_runs(conn) -> int:
            return _prune(conn, link_id, keep)

        return await self.db.execute_write_fn(prune_link_runs)

    async def mark_abandoned_runs(self) -> int:
        """Turn leftover ``running`` rows into errors (``error_code =
        'abandoned'``). Returns how many.

        Called from ``startup``, as datasette-cron does: cron's scheduler
        loop starts only after every plugin's startup hook, and no request
        is served before then, so no genuine run can be in flight and any
        ``running`` row is an orphan of a crashed process."""

        def mark_runs_abandoned(conn) -> int:
            cursor = conn.execute(
                f"UPDATE {RUNS} SET status = 'error', error_code = ?,"
                " error_message = ?, finished_at = ?"
                " WHERE status = 'running'",
                [
                    ABANDONED,
                    "The run was interrupted when Datasette stopped",
                    _now(),
                ],
            )
            return cursor.rowcount

        return await self.db.execute_write_fn(mark_runs_abandoned)

    async def _get_run(self, run_id: int) -> Run | None:
        result = await self.db.execute(f"SELECT * FROM {RUNS} WHERE id = ?", [run_id])
        row = result.first()
        return Run.model_validate(dict(row)) if row else None
