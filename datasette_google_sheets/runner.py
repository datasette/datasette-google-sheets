"""Run one link as its acting actor, with run history, link status and
auto-pause (D5, D17, D19).

``run_link()`` is the single entry point for the cron handler (ticket 10),
"Run now" (ticket 13) and the wizard's first run (ticket 17). It:

1. picks the acting actor (D5): the clicking owner for ``manual`` runs, the
   stored owner re-resolved through ``actors_from_ids`` for ``scheduled``;
2. skips (scheduled) or refuses (manual) a paused link;
3. records a run row, checks the references (D19) and the owner's Datasette
   permissions, gets the credential *as that actor* and calls the import or
   export engine (tickets 07, 08);
4. persists what the engine reports on the link and closes the run row
   (``finish_run`` touches only the run, D32);
5. sets the link status: ``ok``, ``error`` (transient, retried) or
   ``paused`` (needs a human), per D17.

Our own table writes never go through ``allowed()`` (D7): the owner's
permissions are checked here, before the engine writes.

Pausing calls the hook set with ``set_on_paused()``: ``schedule.start()``
wires it to cron's ``set_enabled(task, False)`` at startup (a no-op until
then), so this module doesn't depend on cron.

Messages and ``status_detail`` are for the link's owner: they may name the
sheet, tab, table or the service account's email, never cell values. Logs
get only the link id and an exception's type name.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx2
from datasette.resources import DatabaseResource, TableResource
from datasette_google_auth import (
    CredentialBroken,
    CredentialChanged,
    CredentialForbidden,
    CredentialNotFound,
    CredentialUndecryptable,
    GoogleAuthError,
    GoogleTokenError,
    MissingScopes,
    connect_url,
    get_credential,
)

from .config import get_config
from .exporter import ExportError, ExportResult, run_export
from .importer import ImporterError, ImportResult, check_table, run_import, strategy_for
from .internal_db import InternalDB, Link, LinkStatus, RunTrigger, _now
from .permissions import can_operate_link
from .sheets import SheetsError, SheetsErrorKind

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette_google_auth import CredentialInfo

logger = logging.getLogger(__name__)

# google-auth D27: imports only read, exports write.
SCOPE_IMPORT = "https://www.googleapis.com/auth/spreadsheets.readonly"
SCOPE_EXPORT = "https://www.googleapis.com/auth/spreadsheets"

# D17: the failures that need a human. Everything else is an error that the
# next run (and, if transient, cron's retry) may fix.
PAUSE_CODES = frozenset(
    {
        "credential_broken",
        "credential_missing",
        "credential_undecryptable",
        "missing_scopes",
        "permission_lost",
        "database_missing",
        "database_immutable",
        "table_missing",
        "table_changed",
        "tab_missing",
        "headers_changed",
        "empty_sheet",
        "bad_key",
        # A create link whose table name got taken: only a person can pick
        # another name or target (orchestrator, ticket 09 review).
        "table_exists",
        "truncated",
        "too_large",
        "source_missing",
        "sa_cannot_create",
        # SheetsError kinds (D17: a Sheets 403/404 or SERVICE_DISABLED).
        "not_shared",
        "not_found",
        "api_disabled",
        "scope",
    }
)
TRANSIENT_KINDS: frozenset[SheetsErrorKind] = frozenset({"rate_limited", "server"})
FAILING_REPEATEDLY = "failing_repeatedly"

Action = Literal["pause", "retry", "error"]
OutcomeStatus = Literal["success", "no_change", "error", "skipped"]
OnPaused = Callable[["Datasette", Link], Awaitable[None]]


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class RunOutcome:
    """What ``run_link`` did, for "Run now" and the wizard to show.

    ``status`` is the run's (``skipped`` when a scheduled run found the link
    disabled or gone, with no run row). ``code`` / ``message`` describe a
    failure; ``reconnect_url`` (OAuth credential problems) and
    ``share_with`` (a service account's email) are the fixes to offer.
    ``link_status`` is the link's status afterwards."""

    run_id: int | None
    status: OutcomeStatus
    code: str | None = None
    message: str | None = None
    reconnect_url: str | None = None
    share_with: str | None = None
    result: ImportResult | ExportResult | None = None
    link_status: LinkStatus | None = None


class RunRefused(Exception):
    """A manual run that may not start: ``not_found`` (no such link),
    ``not_owner`` (only the owner runs a link, D15) or ``paused`` (resume
    it first). Nothing was recorded."""

    def __init__(self, code: Literal["not_found", "not_owner", "paused"], message):
        super().__init__(message)
        self.code = code
        self.message = message


class RetryableRunError(Exception):
    """Raised by a *scheduled* run after recording a transient failure, so
    cron's retry policy applies (D4). The message has only the link id and
    the failure code: cron logs it and puts it on spans."""

    def __init__(self, link_id: str, code: str):
        super().__init__(
            f"Google Sheets link {link_id} failed with a transient error ({code})"
        )
        self.link_id = link_id
        self.code = code


@dataclass(frozen=True)
class Failure:
    """A classified failure: its ``code``, owner-facing ``message`` and what
    to do about it (``pause``, ``retry`` = transient, or plain ``error``)."""

    code: str
    message: str
    action: Action
    data: dict[str, Any] | None = None
    reconnect_url: str | None = None
    share_with: str | None = None
    rows_written: int | None = None


class _Stop(Exception):
    """A pre-check failed; carries its ``Failure``."""

    def __init__(self, failure: Failure):
        super().__init__(failure.code)
        self.failure = failure


# -------------------------------------------------------------------- hooks


async def _no_op(datasette: Datasette, link: Link) -> None:
    return None


def set_on_paused(datasette: Datasette, hook: OnPaused | None) -> None:
    """Call ``await hook(datasette, link)`` whenever a scheduled link is
    auto-paused (ticket 10: ``scheduler.set_enabled(task, False)``). None
    restores the no-op."""
    setattr(datasette, "_google_sheets_on_paused", hook)  # noqa: B010


def _on_paused(datasette: Datasette) -> OnPaused:
    return getattr(datasette, "_google_sheets_on_paused", None) or _no_op


def _lock(datasette: Datasette, link_id: str) -> asyncio.Lock:
    """One lock per link id per Datasette, so "Run now" during a scheduled
    run waits instead of interleaving. Cron's overlap ``skip`` covers
    scheduled vs scheduled."""
    locks: dict[str, asyncio.Lock] | None = getattr(
        datasette, "_google_sheets_run_locks", None
    )
    if locks is None:
        locks = {}
        setattr(datasette, "_google_sheets_run_locks", locks)  # noqa: B010
    return locks.setdefault(link_id, asyncio.Lock())


def link_path(datasette: Datasette, link_id: str) -> str:
    """The link detail page (D27): where a reconnect comes back to."""
    return datasette.urls.path(f"/-/google-sheets/links/{link_id}")


# --------------------------------------------------------------- the actor


async def owner_actor(datasette: Datasette, owner_id: str) -> dict[str, Any]:
    """The stored owner as a full actor (D5), re-resolved on every run.
    Falls back to ``{"id": owner_id}`` when no plugin knows the id."""
    actors = await datasette.actors_from_ids([owner_id])
    return (actors or {}).get(owner_id) or {"id": owner_id}


# ----------------------------------------------------------------- checks


def _check_database(datasette: Datasette, link: Link) -> None:
    """D19: the database must be attached, and mutable for imports."""
    name = link.database_name
    db = datasette.databases.get(name)
    if db is None:
        raise _Stop(
            Failure(
                "database_missing",
                f"The database {name!r} isn't attached any more.",
                "pause",
            )
        )
    if link.direction == "import" and not db.is_mutable:
        raise _Stop(
            Failure(
                "database_immutable",
                f"The database {name!r} is now immutable, so it can't be written.",
                "pause",
            )
        )


def _import_actions(link: Link) -> list[str]:
    """The Datasette actions an import run needs (D8)."""
    strategy = strategy_for(link)
    if strategy == "create":
        return ["create-table"]
    if link.mode == "create" or strategy == "upsert":
        # A re-run or sync of a created table upserts or replaces: allow
        # for both.
        return ["insert-row", "update-row", "delete-row"]
    if strategy == "replace":
        return ["insert-row", "delete-row"]
    return ["insert-row"]


async def _check_import_target(datasette: Datasette, link: Link) -> str | None:
    """The table must still exist with the mapped columns (D10, D19).
    Returns its stored name, or None before a ``create`` link's first run."""
    if strategy_for(link) == "create":
        return None
    assert link.table_name is not None and link.mapping is not None
    table = link.table_name
    columns = [c.name for c in link.mapping.columns if not c.skip]

    def check_import_target(conn) -> str | None:
        return check_table(conn, table, columns, new=False)

    try:
        return await datasette.get_database(link.database_name).execute_fn(
            check_import_target
        )
    except ImporterError as error:
        raise _Stop(
            Failure(error.code, error.message, "pause", data=error.data)
        ) from None


async def _check_import_permissions(
    datasette: Datasette, link: Link, actor: dict[str, Any], table: str | None
) -> None:
    """The acting actor's Datasette permissions for this write (D5, D8)."""
    actions = _import_actions(link)
    database = link.database_name
    if table is None or link.synced:
        # A synced table carries a table-level deny of the write actions for
        # every actor (D7, ticket 11), which our own writes ignore. So check
        # the owner's grant on the *database*: allowed() with a
        # DatabaseResource matches only database-level and global rules
        # (actions_sql.check_permissions_for_actions: a rule applies when
        # `child IS NULL OR child = :_check_child`, and the child is NULL),
        # so the table-level deny doesn't count. create-table is a database
        # action anyway.
        resource: DatabaseResource | TableResource = DatabaseResource(database)
    else:
        resource = TableResource(database, table)
    denied = [
        action
        for action in actions
        if not await datasette.allowed(action=action, resource=resource, actor=actor)
    ]
    if denied:
        where = f"the database {database!r}"
        if table is not None and not link.synced:
            where = f"the table {table!r} in {where}"
        raise _Stop(
            Failure(
                "permission_lost",
                "You no longer have permission to write to this link's table"
                f" ({', '.join(denied)} on {where}), so nothing was imported."
                " Ask an administrator to restore it, then resume the link.",
                "pause",
                data={"actions": denied},
            )
        )


# ---------------------------------------------------------- classification


def _share_hint(
    link: Link, info: CredentialInfo | None, kind: str, google_message: str
) -> Failure:
    """``not_shared`` / ``not_found``: say whom to share the sheet with."""
    role = "Editor" if link.direction == "export" else "Viewer"
    email = info.google_email if info is not None else None
    if info is not None and info.type == "service_account":
        # Google answers 404 for a sheet the service account can't see, so
        # the likely fix is the same for both: share it.
        return Failure(
            kind,
            "The service account can't open this spreadsheet. Check that it"
            f" still exists, and share the sheet with {email} as {role},"
            " then resume the link.",
            "pause",
            share_with=email,
        )
    who = f"The Google account {email}" if email else "This Google account"
    if kind == "not_found":
        message = (
            f"{who} can't find this spreadsheet. It may have been deleted, or"
            f" it's no longer shared with you. Google said: {google_message}"
        )
    else:
        message = (
            f"{who} can't {'edit' if role == 'Editor' else 'open'} this"
            f" spreadsheet. Ask its owner to share it with you as {role}, then"
            " resume the link."
        )
    return Failure(kind, message, "pause")


def classify(
    datasette: Datasette,
    link: Link,
    error: BaseException,
    info: CredentialInfo | None = None,
) -> Failure:
    """Map anything a run raised to a ``Failure`` (D17). ``info`` is the
    credential, when ``get_credential`` got that far."""
    reconnect = connect_url(datasette, return_to=link_path(datasette, link.id))

    if isinstance(error, _Stop):
        return error.failure

    if isinstance(error, ImporterError):
        if error.code == "write_failed":
            transient = bool((error.data or {}).get("transient"))
            return Failure(error.code, error.message, "retry" if transient else "error")
        action: Action = "pause" if error.code in PAUSE_CODES else "error"
        return Failure(error.code, error.message, action, data=error.data)

    if isinstance(error, ExportError):
        if error.code == "forbidden":
            # Reading as the owner was refused: a lost Datasette permission.
            return Failure("permission_lost", error.message, "pause")
        if error.code == "partial":
            # D14/D31: rows were written; pause or retry by what stopped it.
            if error.kind is not None:
                cause = _sheets_failure(link, error.kind, "", info, reconnect)
            elif error.__cause__ is not None:
                cause = classify(datasette, link, error.__cause__, info)
            else:
                cause = Failure("partial", "", "error")
            return Failure(
                "partial",
                error.message,
                cause.action,
                data={"rows_written": error.rows_written, "cause": cause.code},
                reconnect_url=cause.reconnect_url,
                share_with=cause.share_with,
                rows_written=error.rows_written,
            )
        action = "pause" if error.code in PAUSE_CODES else "error"
        return Failure(error.code, error.message, action, share_with=error.share_with)

    if isinstance(error, SheetsError):
        return _sheets_failure(link, error.kind, error.message, info, reconnect, error)

    if isinstance(error, GoogleAuthError):
        return _auth_failure(error, reconnect)

    if isinstance(error, httpx2.TimeoutException):
        return Failure("timeout", "The request to Google timed out.", "retry")
    if isinstance(error, httpx2.TransportError):
        return Failure("network", "The connection to Google failed.", "retry")

    if isinstance(error, sqlite3.OperationalError) and (
        "locked" in str(error).casefold() or "busy" in str(error).casefold()
    ):
        return Failure(
            "database_locked",
            "The database was busy. The run will be retried.",
            "retry",
        )

    return Failure(
        "internal_error",
        f"The run failed unexpectedly ({type(error).__name__}).",
        "error",
    )


def _sheets_failure(
    link: Link,
    kind: SheetsErrorKind,
    message: str,
    info: CredentialInfo | None,
    reconnect: str,
    error: SheetsError | None = None,
) -> Failure:
    if kind in ("not_shared", "not_found"):
        return _share_hint(link, info, kind, message)
    if kind == "api_disabled":
        # sheets.py already made this our actionable sentence (D31).
        return Failure(kind, message, "pause")
    if kind == "scope":
        oauth = info is not None and info.type == "google_oauth"
        return Failure(
            kind,
            "Google refused this credential for Google Sheets (insufficient"
            " scope)." + (" Reconnect Google, then resume the link." if oauth else ""),
            "pause",
            reconnect_url=reconnect if oauth else None,
        )
    status = f" (HTTP {error.status})" if error is not None else ""
    if kind == "rate_limited":
        return Failure(
            kind,
            "Google Sheets is rate-limiting requests. It will be retried.",
            "retry",
        )
    if kind == "server":
        return Failure(
            kind,
            f"Google Sheets had a server error{status}. It will be retried.",
            "retry",
        )
    return Failure(
        "sheets_error", f"Google Sheets returned an error{status}: {message}", "error"
    )


def _auth_failure(error: GoogleAuthError, reconnect: str) -> Failure:
    if isinstance(error, CredentialBroken):
        # google-auth sets reconnect_url only for OAuth credentials the actor
        # owns; ours points back at the link.
        fix = (
            "Reconnect Google, then resume the link."
            if error.reconnect_url
            else "Add a new key for the service account, or choose another"
            " credential, then resume the link."
        )
        return Failure(
            "credential_broken",
            f"Google rejected this link's credential ({error.detail}). {fix}",
            "pause",
            reconnect_url=reconnect if error.reconnect_url else None,
        )
    if isinstance(error, CredentialNotFound | CredentialForbidden):
        return Failure(
            "credential_missing",
            "This link's credential was deleted, or you can no longer use it."
            " Choose another of your credentials, then resume the link.",
            "pause",
        )
    if isinstance(error, MissingScopes):
        fix = (
            " Reconnect Google and allow Google Sheets access, then resume the link."
            if error.reconnect_url
            else ""
        )
        return Failure(
            "missing_scopes",
            f"{error}.{fix}",
            "pause",
            data={"missing": error.missing},
            reconnect_url=reconnect if error.reconnect_url else None,
        )
    if isinstance(error, CredentialUndecryptable):
        return Failure(
            "credential_undecryptable",
            "This link's credential can't be decrypted: the encryption key may"
            " have changed. Ask an administrator to restore the old key.",
            "pause",
        )
    if isinstance(error, CredentialChanged):
        return Failure("auth_error", str(error), "retry")
    if isinstance(error, GoogleTokenError) and (
        error.status is None or error.status == 429 or error.status >= 500
    ):
        return Failure("auth_error", str(error), "retry")
    return Failure("auth_error", str(error), "error")


# --------------------------------------------------------------------- run


async def _execute(
    datasette: Datasette,
    link: Link,
    actor: dict[str, Any],
    *,
    force: bool,
    got_credential: Callable[[CredentialInfo], None],
) -> ImportResult | ExportResult:
    _check_database(datasette, link)
    if link.direction == "import":
        table = await _check_import_target(datasette, link)
        await _check_import_permissions(datasette, link, actor, table)
        cred = await get_credential(
            datasette, link.credential_id, actor=actor, scopes=[SCOPE_IMPORT]
        )
        got_credential(cred.info)
        return await run_import(datasette, link, cred, actor=actor, force=force)
    # Exports: no pre-check. read_rows() reads as the actor, so a lost read
    # permission comes back as ExportError("forbidden").
    cred = await get_credential(
        datasette, link.credential_id, actor=actor, scopes=[SCOPE_EXPORT]
    )
    got_credential(cred.info)
    return await run_export(datasette, link, cred, actor)


def _success_fields(link: Link, result: ImportResult | ExportResult) -> dict[str, Any]:
    now = _now()
    fields: dict[str, Any] = {
        "status": "ok",
        "status_code": None,
        "status_detail": None,
        "status_data": None,
        "consecutive_failures": 0,
        "last_run_at": now,
        "last_success_at": now,
        # Titles are for display only; refresh them from what Google said.
        "spreadsheet_title": result.spreadsheet_title or link.spreadsheet_title,
        "sheet_title": result.sheet_title or link.sheet_title,
    }
    if isinstance(result, ImportResult):
        fields["last_hash"] = result.hash
        if result.created_table:
            fields["created_table"] = True
            fields["created_schema"] = result.created_schema
    elif result.spreadsheet_id != link.spreadsheet_id or result.gid != link.sheet_gid:
        # D33: the run that created a new spreadsheet stores it.
        fields["spreadsheet_id"] = result.spreadsheet_id
        fields["sheet_gid"] = result.gid
    return fields


async def run_link(
    datasette: Datasette,
    link_id: str,
    *,
    trigger: RunTrigger,
    actor: dict[str, Any] | None = None,
    force: bool = False,
) -> RunOutcome:
    """Run link ``link_id`` once.

    ``manual``: ``actor`` clicked "Run now" (or finished the wizard) and
    must be the owner; raises ``RunRefused`` otherwise, or if the link is
    paused. Failures are recorded and returned, never raised.

    ``scheduled``: acts as the stored owner (D5); a disabled, paused or
    deleted link is ``skipped``. A transient failure is recorded, then
    raised as ``RetryableRunError`` for cron to retry, unless it paused the
    link.

    ``force`` writes an unchanged sheet anyway (D12).
    """
    idb = InternalDB.for_datasette(datasette)
    if trigger == "manual":
        found = await idb.get_link(link_id)
        if found is None:
            raise RunRefused("not_found", "Link not found")
        if not can_operate_link(actor, found):
            raise RunRefused("not_owner", "Only the link's owner can run it")
    elif trigger != "scheduled":
        raise ValueError(f"Invalid trigger: {trigger!r}")

    async with _lock(datasette, link_id):
        # Re-read after waiting for the lock: a run we waited for may have
        # paused the link or changed its hash.
        link = await idb.get_link(link_id)
        paused = link is not None and (not link.enabled or link.status == "paused")
        if trigger == "manual":
            if link is None:
                raise RunRefused("not_found", "Link not found")
            if paused:
                raise RunRefused("paused", "This link is paused. Resume it first.")
            acting = actor
        else:
            if link is None or paused:
                return RunOutcome(run_id=None, status="skipped")
            acting = await owner_actor(datasette, link.owner_id)
        assert acting is not None and link is not None

        run = await idb.start_run(
            link.id, trigger=trigger, actor_id=str(acting.get("id"))
        )
        info: list[CredentialInfo] = []
        try:
            result = await _execute(
                datasette, link, acting, force=force, got_credential=info.append
            )
        except asyncio.CancelledError:
            await idb.finish_run(
                run.id,
                status="error",
                error_code="cancelled",
                error_message="The run was cancelled.",
            )
            raise
        except Exception as error:
            failure = classify(datasette, link, error, info[0] if info else None)
            if failure.code == "internal_error":
                logger.error(
                    "Google Sheets link %s run failed unexpectedly: %s",
                    link.id,
                    type(error).__name__,
                )
            return await _record_failure(datasette, idb, link, run.id, trigger, failure)

        status: Literal["success", "no_change"] = (
            result.status if isinstance(result, ImportResult) else "success"
        )
        await idb.update_link(link.id, **_success_fields(link, result))
        if isinstance(result, ImportResult):
            await idb.finish_run(
                run.id,
                status=status,
                rows_read=result.rows_read,
                rows_written=result.rows_written,
                added=result.added,
                changed=result.changed,
                removed=result.removed,
                cells=result.cells,
                warnings=result.warnings,
                hash=result.hash,
            )
        else:
            await idb.finish_run(
                run.id,
                status=status,
                rows_read=result.rows_read,
                rows_written=result.rows_written,
                cells=result.cells,
            )
        return RunOutcome(run_id=run.id, status=status, result=result, link_status="ok")


async def _record_failure(
    datasette: Datasette,
    idb: InternalDB,
    link: Link,
    run_id: int,
    trigger: RunTrigger,
    failure: Failure,
) -> RunOutcome:
    """Close the run, update the link's status (D17), pause if needed, and
    raise for cron on a scheduled transient failure."""
    await idb.finish_run(
        run_id,
        status="error",
        rows_written=failure.rows_written,
        error_code=failure.code,
        error_message=failure.message,
    )
    failures = link.consecutive_failures + 1
    code, detail = failure.code, failure.message
    data = dict(failure.data or {})
    pause = failure.action == "pause"
    if not pause and failures >= get_config(datasette).max_consecutive_failures:
        pause = True
        code = FAILING_REPEATEDLY
        detail = (
            f"The last {failures} runs failed, so the link was paused."
            f" The latest error: {failure.message}"
        )
        data["last_code"] = failure.code
    if failure.reconnect_url:
        data["reconnect_url"] = failure.reconnect_url
    if failure.share_with:
        data["share_with"] = failure.share_with

    link_status: LinkStatus = "paused" if pause else "error"
    fields: dict[str, Any] = {
        "status": link_status,
        "status_code": code,
        "status_detail": detail,
        "status_data": data or None,
        "consecutive_failures": failures,
        "last_run_at": _now(),
    }
    if pause:
        fields["enabled"] = False
    updated = await idb.update_link(link.id, **fields)
    if pause and updated is not None and updated.scheduled:
        try:
            await _on_paused(datasette)(datasette, updated)
        except Exception as error:
            # The link is disabled either way, so a scheduled run that still
            # fires is skipped.
            logger.error(
                "Google Sheets link %s: the on_paused hook failed: %s",
                link.id,
                type(error).__name__,
            )

    if trigger == "scheduled" and failure.action == "retry" and not pause:
        raise RetryableRunError(link.id, failure.code) from None
    return RunOutcome(
        run_id=run_id,
        status="error",
        code=code,
        message=detail,
        reconnect_url=failure.reconnect_url,
        share_with=failure.share_with,
        link_status=link_status,
    )
