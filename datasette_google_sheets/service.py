"""Link operations behind the JSON API (``routes/api.py``, ticket 13).

Every function takes the *requesting* actor and checks it: links are
owner-only, and admins may view, pause, resume, unlink and delete any link
but never run or edit one (D15). The rules, in one place:

* **Not signed in:** 403 ``not_signed_in``.
* **Visibility (``load_link``):** an unknown id and someone else's link are
  the same 404 for a non-admin (``permissions`` module docstring). Admins
  see every link.
* **Operate (``require_operate``):** only the owner. An admin who can see
  the link gets a 403: it exists for them, they just may not act with the
  owner's credential (orchestrator note, ticket 03/09).

Failures raise ``ApiError`` (``{ok: false, error, code, ...}``, the same
shape as google-credentials's ``error_response``). ``ScheduleError`` (ticket 10)
and google-credentials's ``GoogleCredentialsError`` pass through to the route, which maps
them.

Every mutation ends with ``schedule.sync_task`` so cron matches the link row
(D4). Messages are for the link's owner (they may name tables and columns);
nothing here logs anything but exception type names.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import datasette_google_credentials
from datasette import Response
from datasette.events import AlterTableEvent
from datasette.resources import DatabaseResource, QueryResource, TableResource
from datasette.utils import escape_sqlite
from datasette_google_credentials import get_credential
from sqlite_utils import Database as SqliteUtilsDatabase

from .importer import ImporterError, check_table, stored_table_name
from .internal_db import (
    EXPORT_MODES,
    IMPORT_MODES,
    ExportTabTaken,
    ImportMapping,
    InternalDB,
    Link,
    LinkConflict,
    TableAlreadySynced,
)
from .models import CreateLinkRequest, LinkInfo, SettingsRequest
from .permissions import can_manage_link, can_operate_link, can_view_link, is_admin
from .runner import SCOPE_EXPORT, SCOPE_IMPORT
from .schedule import check_can_schedule, sync_task, validate_interval
from .sheets import SheetsError

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette.database import Database
    from datasette_google_credentials import Credential, CredentialInfo

logger = logging.getLogger(__name__)

DASHBOARD_PATH = "/-/google-sheets"
WRITE_ACTIONS = ("insert-row", "update-row", "delete-row")
# The actions a one-shot import into an existing table needs (D8).
_MODE_ACTIONS = {
    "append": ("insert-row",),
    "replace": ("insert-row", "delete-row"),
    "upsert": WRITE_ACTIONS,
}
# Actor keys that hold a display name, as google-credentials's admin view reads them.
_NAME_KEYS = ("display_name", "display", "name", "username", "login")


class ApiError(Exception):
    """A refused request: ``{ok: false, error: message, code, **extra}``
    with HTTP ``status``. ``message`` is shown to the requesting actor."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def response(self) -> Response:
        return Response.json(
            {"ok": False, "error": self.message, "code": self.code, **self.extra},
            status=self.status,
        )


def _not_found() -> ApiError:
    return ApiError("not_found", "Link not found", 404)


# ------------------------------------------------------------------ actors


def require_actor(actor: dict[str, Any] | None) -> dict[str, Any]:
    """The signed-in actor, or a 403 (as the dashboard page, ticket 15)."""
    if not actor or actor.get("id") is None:
        raise ApiError("not_signed_in", "Sign in to use Google Sheets.", 403)
    return actor


def scope_for(direction: str) -> str:
    """google-credentials D27: imports only read, exports write."""
    return SCOPE_IMPORT if direction == "import" else SCOPE_EXPORT


def oauth_configured(datasette: Datasette) -> bool | None:
    """google-credentials's ``oauth_configured(datasette)`` once it exports it; None
    (unknown: always offer Connect Google) until then (D21)."""
    check = getattr(datasette_google_credentials, "oauth_configured", None)
    if not callable(check):
        return None
    return bool(check(datasette))


def connect_return_to(datasette: Datasette, return_to: str | None) -> str:
    """A local path to come back to after Connect Google (google-credentials
    re-checks it with its own ``safe_return_to``); the dashboard otherwise."""
    if return_to and return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    return datasette.urls.path(DASHBOARD_PATH)


async def owner_names(datasette: Datasette, ids: list[str]) -> dict[str, str]:
    """Display names for actor ids via ``datasette.actors_from_ids()``, as
    google-credentials's admin API resolves them. Ids without a name other than
    the id are absent. A failing identity plugin means "no names"."""
    if not ids:
        return {}
    try:
        actors = await datasette.actors_from_ids(ids) or {}
    except Exception as error:
        logger.warning(
            "datasette-google-sheets: actors_from_ids failed (%s); showing ids",
            type(error).__name__,
        )
        return {}
    names: dict[str, str] = {}
    for key, actor in actors.items():
        if not isinstance(actor, dict):
            continue
        for name_key in _NAME_KEYS:
            name = actor.get(name_key)
            if isinstance(name, str) and name.strip():
                if name != str(key):
                    names[str(key)] = name
                break
    return names


# ------------------------------------------------------------------- links


async def load_link(
    datasette: Datasette, actor: dict[str, Any], link_id: str
) -> tuple[Link, bool]:
    """``(link, admin)`` if the actor may see it; otherwise the same 404 as
    an unknown id."""
    admin = await is_admin(datasette, actor)
    link = await InternalDB.for_datasette(datasette).get_link(link_id)
    if link is None or not can_view_link(actor, link, admin=admin):
        raise _not_found()
    return link, admin


def require_operate(actor: dict[str, Any], link: Link, what: str) -> None:
    """Owner only (D15). Only an admin reaches the 403: anyone else who
    isn't the owner already got ``load_link``'s 404."""
    if not can_operate_link(actor, link):
        raise ApiError("forbidden", f"Only the link's owner can {what}.", 403)


def require_manage(actor: dict[str, Any], link: Link, *, admin: bool) -> None:
    if not can_manage_link(actor, link, admin=admin):
        raise _not_found()


async def link_infos(
    datasette: Datasette, actor: dict[str, Any], links: list[Link], *, admin: bool
) -> list[LinkInfo]:
    """``LinkInfo`` for each link, with owner names for admins."""
    names = (
        await owner_names(datasette, sorted({link.owner_id for link in links}))
        if admin
        else {}
    )
    return [
        LinkInfo.from_link(
            link,
            can_operate=can_operate_link(actor, link),
            can_manage=can_manage_link(actor, link, admin=admin),
            owner_name=names.get(link.owner_id),
        )
        for link in links
    ]


async def link_info(
    datasette: Datasette, actor: dict[str, Any], link: Link, *, admin: bool
) -> LinkInfo:
    return (await link_infos(datasette, actor, [link], admin=admin))[0]


async def _update(datasette: Datasette, link: Link, **fields: Any) -> Link:
    """``update_link`` + ``sync_task``, mapping conflicts and a link deleted
    meanwhile."""
    try:
        updated = await InternalDB.for_datasette(datasette).update_link(
            link.id, **fields
        )
    except LinkConflict as error:
        raise _conflict(error) from None
    except ValueError as error:
        raise ApiError("invalid_link", str(error)) from None
    if updated is None:
        raise _not_found()
    await sync_task(datasette, updated)
    return updated


def _conflict(error: LinkConflict) -> ApiError:
    # existing_id is left out: the link in the way may be someone else's.
    code = "conflict"
    if isinstance(error, ExportTabTaken):
        code = "export_tab_taken"
    elif isinstance(error, TableAlreadySynced):
        code = "table_already_synced"
    return ApiError(code, str(error), 409)


# ------------------------------------------------------------- validation


def _data_database(datasette: Datasette, name: str) -> Database | None:
    return datasette.databases.get(name)


async def _require_database(
    datasette: Datasette, actor: dict[str, Any], name: str
) -> Database:
    db = _data_database(datasette, name)
    if db is None or not await datasette.allowed(
        action="view-database", resource=DatabaseResource(name), actor=actor
    ):
        raise ApiError("database_not_found", f"Database not found: {name}", 404)
    return db


async def _require_actions(
    datasette: Datasette,
    actor: dict[str, Any],
    actions: tuple[str, ...] | list[str],
    resource: DatabaseResource | TableResource | QueryResource,
    where: str,
) -> None:
    denied = [
        action
        for action in actions
        if not await datasette.allowed(action=action, resource=resource, actor=actor)
    ]
    if denied:
        raise ApiError(
            "permission_denied",
            f"You don't have permission to do this ({', '.join(denied)} on {where}).",
            403,
            actions=denied,
        )


def validate_mapping(mapping: ImportMapping, mode: str) -> None:
    """What ``apply_mapping`` and the table write rely on (D9, D11)."""

    def invalid(message: str) -> ApiError:
        return ApiError("invalid_mapping", message)

    sources = mapping.source_headers
    if len({s for s in sources}) != len(sources):
        raise invalid("The mapping's source headers must be unique.")
    known = set(sources)
    columns = [c for c in mapping.columns if not c.skip]
    if not columns:
        raise invalid("Map at least one column.")
    seen: set[str] = set()
    for column in mapping.columns:
        if column.source not in known:
            raise invalid(f"{column.source!r} isn't one of the sheet's headers.")
    for column in columns:
        if not column.name.strip():
            raise invalid(f"The column for {column.source!r} needs a name.")
        if column.name.casefold() in seen:
            raise invalid(f"Two columns are called {column.name!r}.")
        seen.add(column.name.casefold())
    if mapping.key is not None and mapping.key not in {c.name for c in columns}:
        raise invalid("The key must be one of the mapped columns.")
    if mode == "upsert" and mapping.key is None:
        raise ApiError("key_required", "Upserting needs a key column.")


def _importer_error(error: ImporterError, status: int) -> ApiError:
    return ApiError(error.code, error.message, status, **(error.data or {}))


_TARGET_STATUS = {"table_exists": 409, "table_missing": 404, "table_changed": 400}


async def _import_fields(
    datasette: Datasette,
    actor: dict[str, Any],
    db: Database,
    body: CreateLinkRequest,
    scheduled: bool,
) -> dict[str, Any]:
    if body.source_kind or body.query_name or body.sql or body.params or body.options:
        raise ApiError(
            "invalid_link",
            "Imports take a table_name and a mapping, not an export source.",
        )
    if not body.table_name:
        raise ApiError("invalid_link", "An import needs a table_name.")
    if not body.spreadsheet_id or body.gid is None:
        raise ApiError("invalid_link", "An import needs a spreadsheet_id and a gid.")
    if body.mapping is None:
        raise ApiError("invalid_link", "An import needs a mapping.")
    if not db.is_mutable:
        raise ApiError(
            "database_immutable",
            f"The database {body.database!r} is immutable, so it can't be imported into.",
        )
    validate_mapping(body.mapping, body.mode)
    table = body.table_name
    new = body.mode == "create"
    columns = [c.name for c in body.mapping.columns if not c.skip]

    def check_import_target(conn) -> str | None:
        return check_table(conn, table, columns, new=new)

    try:
        stored = await db.execute_fn(check_import_target)
    except ImporterError as error:
        raise _importer_error(error, _TARGET_STATUS.get(error.code, 400)) from None

    database = body.database
    if new:
        # A synced table's later runs upsert or replace, checked at database
        # level (D35); ask for that now rather than pausing on run two.
        actions = ("create-table", *WRITE_ACTIONS) if scheduled else ("create-table",)
        await _require_actions(
            datasette,
            actor,
            actions,
            DatabaseResource(database),
            f"the database {database!r}",
        )
    else:
        assert stored is not None
        await _require_actions(
            datasette,
            actor,
            _MODE_ACTIONS[body.mode],
            TableResource(database, stored),
            f"the table {stored!r}",
        )
        table = stored
    return {
        "table_name": table,
        "spreadsheet_id": body.spreadsheet_id,
        "sheet_gid": body.gid,
        "mapping": body.mapping,
    }


async def _export_fields(
    datasette: Datasette, actor: dict[str, Any], body: CreateLinkRequest
) -> dict[str, Any]:
    if body.mapping is not None:
        raise ApiError("invalid_link", "Exports have no mapping.")
    database = body.database
    kind = body.source_kind
    resource: DatabaseResource | TableResource | QueryResource
    if kind in ("table", "view"):
        if not body.table_name:
            raise ApiError("invalid_link", f"A {kind} export needs a table_name.")
        action, resource = "view-table", TableResource(database, body.table_name)
        where = f"the {kind} {body.table_name!r}"
    elif kind == "query":
        if not body.query_name:
            raise ApiError("invalid_link", "A query export needs a query_name.")
        action, resource = "view-query", QueryResource(database, body.query_name)
        where = f"the query {body.query_name!r}"
    elif kind == "sql":
        if not body.sql:
            raise ApiError("invalid_link", "An SQL export needs sql.")
        action, resource = "execute-sql", DatabaseResource(database)
        where = f"the database {database!r}"
    else:
        raise ApiError("invalid_link", "An export needs a source_kind.")
    # The run reads as the actor through datasette.client anyway (D13);
    # checking first avoids a link that pauses on its first run.
    await _require_actions(datasette, actor, (action,), resource, where)

    fields: dict[str, Any] = {
        "source_kind": kind,
        "table_name": body.table_name if kind in ("table", "view") else None,
        "query_name": body.query_name if kind == "query" else None,
        "sql": body.sql if kind == "sql" else None,
        "params": body.params if kind in ("query", "sql") else None,
        "options": body.options,
    }
    if body.mode == "new":
        if body.spreadsheet_id or body.gid is not None:
            raise ApiError(
                "invalid_link",
                "A new-spreadsheet export takes no spreadsheet_id or gid: its"
                " first run creates the spreadsheet.",
            )
        # D33: '' = create the spreadsheet on the next run.
        fields.update(
            spreadsheet_id="",
            sheet_gid=0,
            spreadsheet_title=(body.spreadsheet_title or "").strip() or None,
        )
    else:
        if not body.spreadsheet_id or body.gid is None:
            raise ApiError(
                "invalid_link", "An export needs a spreadsheet_id and a gid."
            )
        fields.update(spreadsheet_id=body.spreadsheet_id, sheet_gid=body.gid)
    return fields


def _sa_cannot_create(info: CredentialInfo) -> ApiError:
    """D14 / google-credentials D28, before anything is stored."""
    email = info.google_email
    return ApiError(
        "sa_cannot_create",
        "Service accounts can't create new spreadsheets: the file would be owned"
        " by the service account, where you can't see it. Create the spreadsheet"
        f" yourself, share it with {email} as Editor, and export to it instead.",
        400,
        share_with=email,
    )


def _pending_new(link: Link) -> bool:
    return link.mode == "new" and not link.spreadsheet_id


# ------------------------------------------------------------------ create


async def create_link(
    datasette: Datasette, actor: dict[str, Any], body: CreateLinkRequest
) -> Link:
    """Validate and store a link (the route then runs it once). Raises
    ``ApiError``, ``ScheduleError`` or a ``GoogleCredentialsError``."""
    direction = body.direction
    modes = IMPORT_MODES if direction == "import" else EXPORT_MODES
    if body.mode not in modes:
        raise ApiError(
            "invalid_mode",
            f"{body.mode!r} isn't an {direction} mode. Use one of: {', '.join(modes)}.",
        )
    interval = body.interval_minutes
    if interval is not None:
        # D8: a scheduled import is a synced table, which is always a table
        # the link creates. D14: append exports are one-shot; a new
        # spreadsheet is created by the first run, then can be scheduled.
        if direction == "import" and body.mode != "create":
            raise ApiError(
                "not_schedulable",
                "Only an import into a new table can be scheduled (a synced"
                " table). Append, replace and upsert imports are one-shot.",
            )
        if direction == "export" and body.mode != "replace":
            raise ApiError(
                "not_schedulable",
                "Only a replace export to an existing spreadsheet can be scheduled.",
            )
        await check_can_schedule(datasette, actor)
        interval = validate_interval(datasette, interval)

    db = await _require_database(datasette, actor, body.database)
    if direction == "import":
        fields = await _import_fields(datasette, actor, db, body, interval is not None)
    else:
        fields = await _export_fields(datasette, actor, body)

    cred = await get_credential(
        datasette, body.credential_id, actor=actor, scopes=[scope_for(direction)]
    )
    if direction == "export" and body.mode == "new":
        if cred.info.type == "service_account":
            raise _sa_cannot_create(cred.info)

    try:
        link = await InternalDB.for_datasette(datasette).create_link(
            direction=direction,
            mode=body.mode,
            owner_id=str(actor["id"]),
            credential_id=cred.info.id,
            database_name=body.database,
            interval_minutes=interval,
            **fields,
        )
    except LinkConflict as error:
        raise _conflict(error) from None
    except ValueError as error:
        raise ApiError("invalid_link", str(error)) from None
    # Before the first run: a run that pauses the link disables this task
    # (runner.set_on_paused). add_task schedules the first tick an interval
    # from now, so cron doesn't race the first run.
    await sync_task(datasette, link)
    return link


# ----------------------------------------------------------------- manage


async def pause_link(datasette: Datasette, actor: dict[str, Any], link: Link) -> Link:
    """Owner or admin. A link that's already paused keeps its reason (an
    auto-pause's diff and fix must survive a click on Pause)."""
    if link.status == "paused":
        if not link.enabled:
            await sync_task(datasette, link)
            return link
        return await _update(datasette, link, enabled=False)
    who = "its owner" if can_operate_link(actor, link) else "an administrator"
    return await _update(
        datasette,
        link,
        enabled=False,
        status="paused",
        status_code="paused_manually",
        status_detail=f"Paused by {who}.",
        status_data=None,
    )


RESUMED: dict[str, Any] = {
    "enabled": True,
    "status": "ok",
    "status_code": None,
    "status_detail": None,
    "status_data": None,
    "consecutive_failures": 0,
}


async def resume_link(datasette: Datasette, link: Link, *, recreate: bool) -> Link:
    """Owner or admin. The next run decides the status. A header change
    needs the mapping updated first (D10); ``recreate`` re-creates a
    missing table on the next run (D19)."""
    if link.status_code == "headers_changed":
        raise ApiError(
            "mapping_update_required",
            "The sheet's columns have changed. Update the mapping to resume this link.",
            409,
        )
    fields = dict(RESUMED)
    if recreate:
        if link.status_code != "table_missing":
            raise ApiError(
                "cannot_recreate",
                "Only a link whose table is missing can re-create it.",
                409,
            )
        if link.direction != "import" or link.mode != "create":
            raise ApiError(
                "cannot_recreate",
                "Only an import that created its table can re-create it.",
                409,
            )
        # strategy_for() creates the table again when created_table is off.
        fields.update(created_table=False, created_schema=None, last_hash=None)
    return await _update(datasette, link, **fields)


async def unlink(datasette: Datasette, link: Link) -> Link:
    """Owner or admin. Drop the schedule: a synced table becomes a normal,
    editable table (the lock reads ``interval_minutes``, D7/D8), and the
    cron task goes. The table and its data stay."""
    if not link.scheduled:
        raise ApiError("not_scheduled", "This link has no schedule.", 409)
    return await _update(datasette, link, interval_minutes=None)


async def delete_link(datasette: Datasette, link: Link) -> bool:
    """Owner or admin. The cron task, then the link and its runs. Never
    touches the table (D19)."""
    await sync_task(datasette, None, link_id=link.id)
    return await InternalDB.for_datasette(datasette).delete_link(link.id)


# ---------------------------------------------------------------- operate


async def update_settings(
    datasette: Datasette, actor: dict[str, Any], link: Link, body: SettingsRequest
) -> Link:
    """Owner only. Only the fields sent change."""
    sent = body.model_fields_set
    fields: dict[str, Any] = {}

    if "interval_minutes" in sent and body.interval_minutes != link.interval_minutes:
        interval = body.interval_minutes
        if interval is None:
            # Removing a synced table's schedule is an unlink.
            fields["interval_minutes"] = None
        else:
            if link.direction == "import" and not link.synced:
                raise ApiError(
                    "not_schedulable",
                    "Use convert-to-synced to schedule an import.",
                    409,
                )
            if link.direction == "export" and (
                link.mode == "append" or _pending_new(link)
            ):
                raise ApiError(
                    "not_schedulable",
                    "Append exports, and new-spreadsheet exports before their"
                    " first run, can't be scheduled.",
                )
            # Validated only when it changes (D36): an old value below a
            # raised floor stays until someone edits it.
            await check_can_schedule(datasette, actor)
            fields["interval_minutes"] = validate_interval(datasette, interval)

    if (
        "credential_id" in sent
        and body.credential_id is not None
        and body.credential_id != link.credential_id
    ):
        cred = await get_credential(
            datasette,
            body.credential_id,
            actor=actor,
            scopes=[scope_for(link.direction)],
        )
        if _pending_new(link) and cred.info.type == "service_account":
            raise _sa_cannot_create(cred.info)
        fields["credential_id"] = cred.info.id

    if "options" in sent and body.options is not None:
        if link.direction != "export":
            raise ApiError("invalid_settings", "Imports have no options.")
        fields["options"] = body.options

    if not fields:
        return link
    return await _update(datasette, link, **fields)


def _table_sql(conn, table: str) -> str | None:
    stored = stored_table_name(conn, table)
    if stored is None:
        return None
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", [stored]
    ).fetchone()
    return row[0] if row else None


def _table_missing(table: str) -> ApiError:
    return ApiError(
        "table_missing",
        f"The table {table!r} doesn't exist any more. Resume the link with"
        " re-create, or delete it.",
        409,
    )


def _missing_columns(conn, table: str, names: list[str]) -> list[str] | None:
    stored = stored_table_name(conn, table)
    if stored is None:
        return None
    have = {c.name.casefold() for c in SqliteUtilsDatabase(conn).table(stored).columns}
    return [name for name in names if name.casefold() not in have]


async def update_mapping(
    datasette: Datasette, actor: dict[str, Any], link: Link, mapping: ImportMapping
) -> Link:
    """Owner only, after a header change (D10) or a table missing mapped
    columns: store the new mapping, add its new columns to the table with
    ``ALTER TABLE ADD COLUMN`` (removed ones stay, as NULL), then resume."""
    if link.direction != "import":
        raise ApiError("not_an_import", "Only imports have a mapping.")
    if link.status_code not in ("headers_changed", "table_changed"):
        raise ApiError(
            "mapping_not_needed",
            "The mapping can only be changed after the sheet's columns (or the"
            " table's) have changed.",
            409,
        )
    validate_mapping(mapping, link.mode)
    fields: dict[str, Any] = {**RESUMED, "mapping": mapping}
    has_table = not (link.mode == "create" and not link.created_table)
    if has_table:
        assert link.table_name is not None
        db = _data_database(datasette, link.database_name)
        if db is None:
            raise ApiError(
                "database_missing",
                f"The database {link.database_name!r} isn't attached any more.",
                409,
            )
        table = link.table_name
        names = [c.name for c in mapping.columns if not c.skip]
        types = {c.name: c.type for c in mapping.columns if not c.skip}

        def missing_mapped_columns(conn) -> list[str] | None:
            return _missing_columns(conn, table, names)

        missing = await db.execute_fn(missing_mapped_columns)
        if missing is None:
            raise _table_missing(table)
        if missing:
            # Our own write, so the synced-table deny (D7) doesn't apply, but
            # the owner still needs the grant: at database level for a
            # synced table, as its sync runs check (D35).
            resource: DatabaseResource | TableResource = (
                DatabaseResource(link.database_name)
                if link.synced
                else TableResource(link.database_name, table)
            )
            await _require_actions(
                datasette,
                actor,
                ("alter-table",),
                resource,
                f"the table {table!r}",
            )
            database = link.database_name

            def add_mapped_columns(conn, track_event) -> tuple[str | None, str | None]:
                before = _table_sql(conn, table)
                added = _missing_columns(conn, table, names)
                stored = stored_table_name(conn, table)
                if before is None or added is None or stored is None:
                    return None, None
                for name in added:
                    conn.execute(
                        f"ALTER TABLE {escape_sqlite(stored)}"
                        f" ADD COLUMN {escape_sqlite(name)} {types[name]}"
                    )
                after = _table_sql(conn, table)
                if added and after is not None:
                    track_event(
                        AlterTableEvent(
                            actor=actor,
                            database=database,
                            table=stored,
                            before_schema=before,
                            after_schema=after,
                        )
                    )
                return before, after

            before, after = await db.execute_write_fn(add_mapped_columns)
            if before is None:
                raise _table_missing(table)
            # The link's own change: an unaltered created table stays
            # convertible to synced (D8).
            if link.created_table and link.created_schema == before:
                fields["created_schema"] = after
    return await _update(datasette, link, **fields)


async def convert_to_synced(
    datasette: Datasette, actor: dict[str, Any], link: Link, interval_minutes: int
) -> Link:
    """Owner only, with ``google-sheets-schedule``: a one-shot ``create``
    link whose table is unaltered since it created it (D8)."""
    if link.direction != "import" or link.mode != "create":
        raise ApiError(
            "not_convertible",
            "Only an import that created a new table can become a synced table.",
            409,
        )
    if link.synced:
        raise ApiError("already_synced", "This table is already synced.", 409)
    if not link.created_table or link.created_schema is None:
        raise ApiError(
            "not_convertible",
            "This link hasn't created its table yet. Run it first.",
            409,
        )
    await check_can_schedule(datasette, actor)
    interval = validate_interval(datasette, interval_minutes)
    assert link.table_name is not None
    db = _data_database(datasette, link.database_name)
    if db is None:
        raise ApiError(
            "database_missing",
            f"The database {link.database_name!r} isn't attached any more.",
            409,
        )
    table = link.table_name

    def current_table_sql(conn) -> str | None:
        return _table_sql(conn, table)

    current = await db.execute_fn(current_table_sql)
    if current is None:
        raise _table_missing(table)
    if current != link.created_schema:
        raise ApiError(
            "schema_changed",
            f"The table {table!r} has been altered since this link created it,"
            " so it can't become a synced table.",
            409,
        )
    # Sync runs check the owner's writes at database level (D35).
    await _require_actions(
        datasette,
        actor,
        WRITE_ACTIONS,
        DatabaseResource(link.database_name),
        f"the database {link.database_name!r}",
    )
    return await _update(datasette, link, interval_minutes=interval)


# ------------------------------------------------------------ sheets errors

_SHEETS_STATUS = {
    "not_shared": 403,
    "not_found": 404,
    "api_disabled": 403,
    "scope": 403,
    "rate_limited": 429,
    "server": 502,
}


def sheets_api_error(error: SheetsError, cred: Credential, direction: str) -> ApiError:
    """A Sheets error from inspect or preview. A service account gets
    ``share_with``: the email to share the sheet with (D18)."""
    info = cred.info
    role = "Editor" if direction == "export" else "Viewer"
    status = _SHEETS_STATUS.get(error.kind, 502)
    if error.kind in ("not_shared", "not_found"):
        if info.type == "service_account":
            # Google answers 404 for a sheet a service account can't see.
            return ApiError(
                error.kind,
                "The service account can't open this spreadsheet. Check the URL,"
                f" and share the sheet with {info.google_email} as {role}.",
                status,
                share_with=info.google_email,
            )
        who = (
            f"The Google account {info.google_email}"
            if info.google_email
            else "This Google account"
        )
        if error.kind == "not_found":
            message = (
                f"{who} can't find this spreadsheet. Check the URL, or ask its"
                " owner to share it with you."
            )
        else:
            message = (
                f"{who} can't open this spreadsheet. Ask its owner to share it"
                f" with you as {role}."
            )
        return ApiError(error.kind, message, status)
    if error.kind == "scope":
        return ApiError(
            "scope",
            "Google refused this credential for Google Sheets (insufficient scope).",
            status,
        )
    if error.kind in ("api_disabled", "rate_limited", "server"):
        return ApiError(error.kind, error.message, status)
    return ApiError(
        "sheets_error",
        f"Google Sheets returned an error (HTTP {error.status}): {error.message}",
        status,
    )
