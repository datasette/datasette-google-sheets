"""Scheduled links as datasette-cron tasks (D3, D4, D16, D19).

One cron task per scheduled link: ``google-sheets:<link_id>``, handler
``google_sheets:run-link`` (cron prefixes our ``run-link`` with the module
name minus ``datasette_``), config ``{"link_id": ...}``, an interval of
``interval_minutes * 60`` seconds, overlap ``skip`` and two exponential
retries. Our internal DB is the source of truth; cron has no idea which
tasks are ours, so:

* ``sync_task()`` makes cron match one link row. Every link mutation calls
  it (ticket 13). It only ever uses cron's ``add_task`` (an idempotent
  upsert that keeps ``next_run_at`` unless the schedule changed),
  ``set_enabled`` and ``remove_task``. Never ``update_task`` or rrule
  schedules (D4: both are being deleted upstream).
* ``reconcile()`` runs from our ``startup`` hook: ``sync_task`` for every
  scheduled link, paused ones included (D32), then ``remove_task`` for every
  ``google-sheets:`` task with no scheduled link.
* The runner's auto-pause (D17) calls ``disable_task`` through
  ``runner.set_on_paused``.

**Startup ordering (verified).** Cron's ``startup`` is ``tryfirst=True``, and
core's ``invoke_startup`` awaits each plugin's startup coroutine in turn, in
pluggy's call order. So cron's has finished (scheduler built at
``datasette._cron_scheduler``, handlers collected, its loop registered with
``add_background_task``) before ours starts. Core launches background tasks
only after every startup hook, so our tasks are all in place before cron's
loop first ticks. ``tests/test_schedule.py`` checks both.

**Interval floor (D16).** ``min_interval_minutes`` is enforced when a
schedule is set (``validate_interval``). A link stored below a floor that
was raised later keeps its stored value, but its cron task is clamped to
the floor.

The helpers for the API (``validate_interval``,
``internal_db_is_persistent``, ``check_can_schedule``) raise
``ScheduleError`` with a code and an HTTP status.

Logs carry only link ids and exception type names.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .config import get_config
from .internal_db import InternalDB, Link
from .permissions import can_schedule
from .runner import run_link, set_on_paused

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette_cron.scheduler import Scheduler

logger = logging.getLogger(__name__)

TASK_PREFIX = "google-sheets:"
HANDLER_NAME = "run-link"
# Cron registers a plugin's handlers as "<module minus datasette_>:<name>"
# (datasette_cron/__init__.py, startup).
HANDLER_REF = f"google_sheets:{HANDLER_NAME}"
OVERLAP = "skip"
RETRY = {"max_retries": 2, "backoff": "exponential"}


class ScheduleError(Exception):
    """A schedule the API must refuse. ``code`` is the error code and
    ``status`` the HTTP status to answer with."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def task_name(link_id: str) -> str:
    return f"{TASK_PREFIX}{link_id}"


def get_scheduler(datasette: Datasette) -> Scheduler:
    """Cron's scheduler. Cron is a hard dependency (D3), so a missing one
    is a setup error, not something to skip quietly."""
    scheduler = getattr(datasette, "_cron_scheduler", None)
    if scheduler is None:
        raise RuntimeError(
            "datasette-google-sheets needs datasette-cron, but its scheduler"
            " isn't set up"
        )
    return scheduler


# ------------------------------------------------------------ API helpers


def validate_interval(datasette: Datasette, interval_minutes: int | None) -> int:
    """The interval for a new or changed schedule: the configured default
    when None, refused below ``min_interval_minutes`` (D16)."""
    config = get_config(datasette)
    if interval_minutes is None:
        return config.default_interval_minutes
    if interval_minutes < config.min_interval_minutes:
        raise ScheduleError(
            "interval_too_short",
            f"Scheduled links run at most every {config.min_interval_minutes} minutes.",
            400,
        )
    return interval_minutes


def internal_db_is_persistent(datasette: Datasette) -> bool:
    """google-auth's rule (its D30, ``routes/api.py`` status): without
    ``--internal`` the internal DB is a temp file deleted at exit
    (``is_temp_disk``) or in memory, so schedules would vanish (D19)."""
    internal = datasette.get_internal_database()
    return not (internal.is_memory or internal.is_temp_disk)


async def check_can_schedule(
    datasette: Datasette, actor: dict[str, Any] | None
) -> None:
    """Creating or enabling a schedule needs ``google-sheets-schedule``
    (D15) and a persistent internal DB (D19). Raises ``ScheduleError``."""
    if not await can_schedule(datasette, actor):
        raise ScheduleError(
            "forbidden", "You don't have permission to schedule links.", 403
        )
    if not internal_db_is_persistent(datasette):
        raise ScheduleError(
            "internal_db_not_persistent",
            "Scheduled links need a persistent internal database: start"
            " Datasette with --internal path/to/internal.db.",
            409,
        )


# ------------------------------------------------------------------- tasks


def effective_interval(datasette: Datasette, interval_minutes: int) -> int:
    """The interval cron runs a link at: its stored one, clamped up to
    ``min_interval_minutes`` if the floor was raised after it was set."""
    return max(interval_minutes, get_config(datasette).min_interval_minutes)


def _active(link: Link) -> bool:
    # The runner skips a link that's disabled or paused; so does cron.
    return link.enabled and link.status != "paused"


async def sync_task(
    datasette: Datasette, link: Link | None, *, link_id: str | None = None
) -> None:
    """Make cron's task for a link match the link row.

    A scheduled link gets ``add_task`` (create or change) then
    ``set_enabled`` (``add_task`` never touches ``enabled``, and cron itself
    disables a task whose handler went missing). A deleted link (``None``,
    with ``link_id``) or one without an interval loses its task."""
    if link is None:
        if link_id is None:
            raise ValueError("sync_task needs a link or a link_id")
        await remove_link_task(datasette, link_id)
        return
    if link.interval_minutes is None:
        await remove_link_task(datasette, link.id)
        return
    scheduler = get_scheduler(datasette)
    name = task_name(link.id)
    minutes = effective_interval(datasette, link.interval_minutes)
    if minutes != link.interval_minutes:
        logger.info(
            "Google Sheets link %s is scheduled below min_interval_minutes;"
            " running it every %d minutes",
            link.id,
            minutes,
        )
    await scheduler.add_task(
        name=name,
        handler=HANDLER_REF,
        schedule={"interval": minutes * 60},
        config={"link_id": link.id},
        overlap=OVERLAP,
        retry=dict(RETRY),
    )
    await scheduler.set_enabled(name, _active(link))


async def remove_link_task(datasette: Datasette, link_id: str) -> None:
    """Delete, unlink or unschedule: remove the task (a no-op if there's
    none). Cron also cancels its in-flight runs."""
    await get_scheduler(datasette).remove_task(task_name(link_id))


async def disable_task(datasette: Datasette, link: Link) -> None:
    """The runner's auto-pause hook (D17): stop cron firing the link."""
    await get_scheduler(datasette).set_enabled(task_name(link.id), False)


async def reconcile(datasette: Datasette) -> None:
    """Make cron's ``google-sheets:`` tasks match our scheduled links (D4).
    Other plugins' tasks are never touched. One link failing to sync is
    logged and doesn't stop the others."""
    scheduler = get_scheduler(datasette)
    links = await InternalDB.for_datasette(datasette).scheduled_links()
    for link in links:
        try:
            await sync_task(datasette, link)
        except Exception as error:
            logger.error(
                "Google Sheets link %s: couldn't sync its cron task: %s",
                link.id,
                type(error).__name__,
            )
    wanted = {task_name(link.id) for link in links}
    # scheduler.internal_db is documented in cron's README ("Data Models");
    # get_all_tasks is what cron's own task list API uses.
    for task in await scheduler.internal_db.get_all_tasks():
        if task.name.startswith(TASK_PREFIX) and task.name not in wanted:
            await scheduler.remove_task(task.name)


async def start(datasette: Datasette) -> None:
    """From our ``startup`` hook, after cron's: wire the auto-pause hook
    and reconcile."""
    set_on_paused(datasette, disable_task)
    await reconcile(datasette)


# ----------------------------------------------------------------- handler


def _pending_removals(datasette: Datasette) -> set[asyncio.Task]:
    pending: set[asyncio.Task] | None = getattr(
        datasette, "_google_sheets_pending_removals", None
    )
    if pending is None:
        pending = set()
        setattr(datasette, "_google_sheets_pending_removals", pending)  # noqa: B010
    return pending


def _remove_after_this_run(datasette: Datasette, link_id: str) -> None:
    """Remove a link's task once the current run is over. ``remove_task``
    cancels the task's in-flight runs, and the handler calling this *is*
    one, so removing it inline would cancel the run that asked."""
    current = asyncio.current_task()

    async def remove_orphan_task() -> None:
        if current is not None:
            await asyncio.wait({current})
        try:
            await remove_link_task(datasette, link_id)
        except Exception as error:
            logger.error(
                "Google Sheets link %s: couldn't remove its cron task: %s",
                link_id,
                type(error).__name__,
            )

    pending = _pending_removals(datasette)
    task = asyncio.get_running_loop().create_task(remove_orphan_task())
    pending.add(task)
    task.add_done_callback(pending.discard)


async def run_link_handler(datasette: Datasette, config: dict[str, Any]) -> None:
    """Cron's ``google_sheets:run-link``. A transient failure raises
    ``RetryableRunError`` so cron retries (D35). A task whose link is gone,
    or no longer scheduled, is an orphan: logged and removed."""
    link_id = config["link_id"]
    link = await InternalDB.for_datasette(datasette).get_link(link_id)
    if link is None or not link.scheduled:
        logger.info(
            "Google Sheets link %s has no schedule any more; removing its cron task",
            link_id,
        )
        _remove_after_this_run(datasette, link_id)
        return
    await run_link(datasette, link_id, trigger="scheduled")


def cron_handlers() -> dict[str, Any]:
    """For the ``cron_register_handlers`` hook."""
    return {HANDLER_NAME: run_link_handler}
