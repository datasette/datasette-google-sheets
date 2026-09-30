"""Scheduled links as datasette-cron tasks (ticket 10): the handler, the
task spec, sync/reconcile, startup ordering, the API helpers and one
end-to-end run through cron's ``trigger_task``."""

import asyncio
import copy
import json
import logging

import pytest
import pytest_asyncio
from fixtures_import import DATA_DB
from fixtures_sheets import ALICE, BOB, make_datasette

from datasette_google_sheets import schedule
from datasette_google_sheets.internal_db import InternalDB
from datasette_google_sheets.schedule import (
    HANDLER_REF,
    ScheduleError,
    check_can_schedule,
    internal_db_is_persistent,
    reconcile,
    sync_task,
    task_name,
    validate_interval,
)

WRITE_ACTIONS = ("create-table", "insert-row", "update-row", "delete-row")
# alice may write to `data`, at database level (as in test_runner.py).
CONFIG = {
    "databases": {
        DATA_DB: {"permissions": {action: {"id": "alice"} for action in WRITE_ACTIONS}}
    }
}
TASK_SPEC = {
    "handler": "google_sheets:run-link",
    "overlap": "skip",
    "retry": {"max_retries": 2, "backoff": "exponential"},
}


@pytest_asyncio.fixture
async def datasette(mock_google):
    return await make_datasette(mock_google, config=copy.deepcopy(CONFIG))


@pytest.fixture
def idb(datasette):
    return InternalDB.for_datasette(datasette)


@pytest.fixture
def scheduler(datasette):
    return datasette._cron_scheduler


@pytest.fixture
def link(idb, import_link, sa_credential):
    """``await link(table="students", sa_key="test", **import_link kwargs)``:
    an import link owned by alice, using her service account."""

    async def make(table="students", *, sa_key="test", **kwargs):
        info = await sa_credential("alice", key=sa_key)
        created = await import_link(table, **kwargs)
        return await idb.update_link(created.id, credential_id=info.id)

    return make


class SpyScheduler:
    """Only the four scheduler methods D4 allows: anything else (like
    ``update_task``) is an AttributeError."""

    def __init__(self):
        self.calls = []

    async def add_task(self, **kwargs):
        self.calls.append(("add_task", kwargs))

    async def set_enabled(self, name, enabled):
        self.calls.append(("set_enabled", name, enabled))

    async def remove_task(self, name):
        self.calls.append(("remove_task", name))

    async def trigger_task(self, name):
        self.calls.append(("trigger_task", name))


@pytest.fixture
def spy(datasette, monkeypatch):
    spy = SpyScheduler()
    monkeypatch.setattr(datasette, "_cron_scheduler", spy)
    return spy


async def task_state(scheduler, name):
    """(enabled, interval seconds, config) of a cron task, or None."""
    task = await scheduler.internal_db.get_task(name)
    if task is None:
        return None
    config = json.loads(task.config) if isinstance(task.config, str) else task.config
    return (task.enabled, json.loads(task.schedule_config)["seconds"], config)


# ------------------------------------------------------------ registration


@pytest.mark.asyncio
async def test_handler_is_registered_with_the_google_sheets_prefix(scheduler):
    assert HANDLER_REF == "google_sheets:run-link"
    assert "google_sheets:run-link" in scheduler.list_handlers()
    assert scheduler.get_handler(HANDLER_REF) is schedule.run_link_handler


@pytest.mark.asyncio
async def test_startup_reconciles_after_crons_startup_and_before_its_loop(
    mock_google, monkeypatch
):
    """Cron's startup (tryfirst) has finished when ours reconciles: the
    scheduler exists, our handler is registered and cron's loop is
    registered (its last step) but not launched."""
    seen = []
    original = schedule.reconcile

    async def spy_reconcile(datasette):
        scheduler = getattr(datasette, "_cron_scheduler", None)
        seen.append(
            {
                "scheduler": scheduler is not None,
                "handler": scheduler is not None
                and HANDLER_REF in scheduler.list_handlers(),
                "loop": [
                    t.state
                    for t in datasette._background_tasks.tasks()
                    if t.name == "datasette-cron"
                ],
            }
        )
        await original(datasette)

    monkeypatch.setattr(schedule, "reconcile", spy_reconcile)
    await make_datasette(mock_google)
    assert seen == [{"scheduler": True, "handler": True, "loop": ["registered"]}]


@pytest.mark.asyncio
async def test_restart_adds_tasks_before_the_loop_launches(mock_google, tmp_path):
    """A persistent internal DB across two Datasettes: the second one's
    startup reconciles the stored links before any background task runs."""
    internal = str(tmp_path / "internal.db")
    first = await make_datasette(mock_google, internal=internal)
    idb = InternalDB.for_datasette(first)
    kept = await idb.create_link(
        direction="import",
        mode="create",
        owner_id="alice",
        credential_id="unused",
        database_name=DATA_DB,
        table_name="students",
        spreadsheet_id="students",
        sheet_gid=0,
        mapping={"source_headers": ["id"], "columns": []},
        interval_minutes=10,
    )
    await first._cron_scheduler.add_task(
        name=task_name("gone"),
        handler=HANDLER_REF,
        schedule={"interval": 600},
        config={"link_id": "gone"},
    )
    first.close()

    second = await make_datasette(mock_google, internal=internal)
    scheduler = second._cron_scheduler
    assert [
        t.state for t in second._background_tasks.tasks() if t.name == "datasette-cron"
    ] == ["registered"]
    assert await task_state(scheduler, task_name(kept.id)) == (
        True,
        600,
        {"link_id": kept.id},
    )
    assert await task_state(scheduler, task_name("gone")) is None
    second.close()


# ------------------------------------------------------------ the mapping


@pytest.mark.asyncio
async def test_link_changes_map_to_add_set_enabled_and_remove(
    datasette, idb, link, spy
):
    created = await link(interval_minutes=10)
    name = task_name(created.id)
    add = (
        "add_task",
        {
            "name": name,
            "schedule": {"interval": 600},
            "config": {"link_id": created.id},
            **TASK_SPEC,
        },
    )

    # Create.
    await sync_task(datasette, created)
    assert spy.calls == [add, ("set_enabled", name, True)]

    # Change the interval: add_task again (an upsert), never update_task.
    spy.calls.clear()
    changed = await idb.update_link(created.id, interval_minutes=30)
    await sync_task(datasette, changed)
    assert spy.calls == [
        ("add_task", {**add[1], "schedule": {"interval": 1800}}),
        ("set_enabled", name, True),
    ]

    # Pause, then resume.
    spy.calls.clear()
    paused = await idb.update_link(created.id, enabled=False, status="paused")
    await sync_task(datasette, paused)
    assert spy.calls[-1] == ("set_enabled", name, False)
    spy.calls.clear()
    resumed = await idb.update_link(created.id, enabled=True, status="ok")
    await sync_task(datasette, resumed)
    assert spy.calls[-1] == ("set_enabled", name, True)

    # Unschedule (unlink), then delete.
    spy.calls.clear()
    unlinked = await idb.update_link(created.id, interval_minutes=None)
    await sync_task(datasette, unlinked)
    assert spy.calls == [("remove_task", name)]
    spy.calls.clear()
    await idb.delete_link(created.id)
    await sync_task(datasette, None, link_id=created.id)
    assert spy.calls == [("remove_task", name)]


@pytest.mark.asyncio
async def test_one_shot_links_have_no_task(datasette, link, spy):
    created = await link()
    await sync_task(datasette, created)
    assert spy.calls == [("remove_task", task_name(created.id))]


@pytest.mark.asyncio
async def test_sync_task_against_cron(datasette, idb, link, scheduler):
    created = await link(interval_minutes=10)
    name = task_name(created.id)
    await sync_task(datasette, created)
    task = await scheduler.internal_db.get_task(name)
    assert (task.handler, task.overlap_policy, task.retry_max, task.retry_backoff) == (
        "google_sheets:run-link",
        "skip",
        2,
        "exponential",
    )
    assert task.schedule_type == "interval"
    assert await task_state(scheduler, name) == (True, 600, {"link_id": created.id})

    paused = await idb.update_link(created.id, enabled=False, status="paused")
    await sync_task(datasette, paused)
    assert (await task_state(scheduler, name))[0] is False

    await idb.delete_link(created.id)
    await sync_task(datasette, None, link_id=created.id)
    assert await task_state(scheduler, name) is None


# ---------------------------------------------------------- reconciliation


@pytest.mark.asyncio
async def test_reconcile_adds_missing_removes_orphans_and_leaves_others(
    datasette, idb, link, scheduler
):
    active = await link(interval_minutes=10)
    paused = await link("paused_table", interval_minutes=15)
    paused = await idb.update_link(paused.id, enabled=False, status="paused")
    one_shot = await link("one_shot")
    for name, handler in [
        (task_name("orphan"), HANDLER_REF),
        (task_name(one_shot.id), HANDLER_REF),
        ("other-plugin:task", "other_plugin:thing"),
    ]:
        await scheduler.add_task(
            name=name, handler=handler, schedule={"interval": 60}, config={}
        )
    # Cron disables a task whose handler went missing: reconcile re-enables.
    await scheduler.add_task(
        name=task_name(active.id),
        handler=HANDLER_REF,
        schedule={"interval": 600},
        config={"link_id": active.id},
    )
    await scheduler.set_enabled(task_name(active.id), False)

    await reconcile(datasette)

    names = {t.name for t in await scheduler.internal_db.get_all_tasks()}
    assert names == {
        task_name(active.id),
        task_name(paused.id),
        "other-plugin:task",
    }
    assert await task_state(scheduler, task_name(active.id)) == (
        True,
        600,
        {"link_id": active.id},
    )
    assert await task_state(scheduler, task_name(paused.id)) == (
        False,
        900,
        {"link_id": paused.id},
    )


@pytest.mark.asyncio
async def test_reconcile_keeps_going_past_a_failing_link(
    datasette, link, scheduler, monkeypatch, caplog
):
    first = await link(interval_minutes=10)
    second = await link("second", interval_minutes=10)
    original = schedule.sync_task

    async def flaky(ds, link, **kwargs):
        if link.id == first.id:
            raise RuntimeError("boom")
        await original(ds, link, **kwargs)

    monkeypatch.setattr(schedule, "sync_task", flaky)
    await reconcile(datasette)
    assert await task_state(scheduler, task_name(second.id)) is not None
    assert f"{first.id}: couldn't sync its cron task: RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_interval_below_a_raised_floor_is_clamped_in_cron_only(mock_google):
    """A link stored at 5 minutes, then the floor raised to 15: cron runs it
    every 15, the stored interval stays 5 (orchestrator, ticket 02)."""
    datasette = await make_datasette(
        mock_google,
        sheets_config={"min_interval_minutes": 15, "default_interval_minutes": 15},
    )
    idb = InternalDB.for_datasette(datasette)
    created = await idb.create_link(
        direction="import",
        mode="create",
        owner_id="alice",
        credential_id="unused",
        database_name=DATA_DB,
        table_name="students",
        spreadsheet_id="students",
        sheet_gid=0,
        mapping={"source_headers": ["id"], "columns": []},
        interval_minutes=5,
    )
    await reconcile(datasette)
    scheduler = datasette._cron_scheduler
    assert (await task_state(scheduler, task_name(created.id)))[1] == 15 * 60
    assert (await idb.get_link(created.id)).interval_minutes == 5


# ------------------------------------------------------------ API helpers


@pytest.mark.asyncio
async def test_validate_interval(mock_google):
    datasette = await make_datasette(
        mock_google,
        sheets_config={"min_interval_minutes": 5, "default_interval_minutes": 10},
    )
    assert validate_interval(datasette, None) == 10
    assert validate_interval(datasette, 5) == 5
    assert validate_interval(datasette, 60) == 60
    for too_short in (4, 1, 0, -1):
        with pytest.raises(ScheduleError) as excinfo:
            validate_interval(datasette, too_short)
        assert (excinfo.value.code, excinfo.value.status) == ("interval_too_short", 400)
        assert "5 minutes" in excinfo.value.message


@pytest.mark.asyncio
async def test_internal_db_is_persistent(mock_google, tmp_path):
    temp = await make_datasette(mock_google)
    assert temp.get_internal_database().is_temp_disk
    assert not internal_db_is_persistent(temp)
    persistent = await make_datasette(
        mock_google, internal=str(tmp_path / "internal.db")
    )
    assert internal_db_is_persistent(persistent)
    persistent.close()


@pytest.mark.asyncio
async def test_check_can_schedule_needs_the_permission_then_a_persistent_db(
    mock_google, tmp_path
):
    grant = {"permissions": {"google-sheets-schedule": {"id": "alice"}}}

    async def code(datasette, actor):
        try:
            await check_can_schedule(datasette, actor)
        except ScheduleError as error:
            return error.code, error.status
        return None

    temp = await make_datasette(mock_google, config=copy.deepcopy(grant))
    assert await code(temp, BOB) == ("forbidden", 403)
    assert await code(temp, None) == ("forbidden", 403)
    assert await code(temp, ALICE) == ("internal_db_not_persistent", 409)

    persistent = await make_datasette(
        mock_google,
        config=copy.deepcopy(grant),
        internal=str(tmp_path / "internal.db"),
    )
    assert await code(persistent, BOB) == ("forbidden", 403)
    assert await code(persistent, ALICE) is None
    persistent.close()


# ----------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_handler_runs_the_link_as_scheduled(datasette, link, monkeypatch):
    created = await link(interval_minutes=10)
    calls = []

    async def fake_run_link(ds, link_id, **kwargs):
        calls.append((ds, link_id, kwargs))

    monkeypatch.setattr(schedule, "run_link", fake_run_link)
    await schedule.run_link_handler(datasette, {"link_id": created.id})
    assert calls == [(datasette, created.id, {"trigger": "scheduled"})]


@pytest.mark.asyncio
async def test_handler_removes_the_task_of_a_missing_or_unscheduled_link(
    datasette, idb, link, scheduler, caplog
):
    caplog.set_level(logging.INFO)
    unscheduled = await link(interval_minutes=10)
    await sync_task(datasette, unscheduled)
    await idb.update_link(unscheduled.id, interval_minutes=None)
    await scheduler.add_task(
        name=task_name("gone"),
        handler=HANDLER_REF,
        schedule={"interval": 600},
        config={"link_id": "gone"},
    )
    for link_id in ("gone", unscheduled.id):
        # As cron runs it: in its own task. The task is removed once that
        # run is over (removing it inline would cancel the run itself).
        await asyncio.create_task(
            schedule.run_link_handler(datasette, {"link_id": link_id})
        )
        await asyncio.gather(*datasette._google_sheets_pending_removals)
        assert await task_state(scheduler, task_name(link_id)) is None
        assert (
            f"Google Sheets link {link_id} has no schedule any more; removing"
            " its cron task"
        ) in caplog.text
    assert await idb.list_runs(unscheduled.id) == []


# ---------------------------------------------------------------- auto-pause


@pytest.mark.asyncio
async def test_auto_pause_disables_the_cron_task(datasette, idb, link, scheduler):
    # "other" can't open the sheet: not shared pauses the link (D17).
    created = await link(interval_minutes=10, sa_key="other")
    await sync_task(datasette, created)
    name = task_name(created.id)
    assert (await task_state(scheduler, name))[0] is True

    outcome = await schedule.run_link(datasette, created.id, trigger="scheduled")
    assert outcome.link_status == "paused"
    assert (await task_state(scheduler, name))[0] is False
    # Reconciling a paused link keeps its task disabled.
    await reconcile(datasette)
    assert (await task_state(scheduler, name))[0] is False


# ---------------------------------------------------------------- end to end


@pytest.mark.asyncio
async def test_end_to_end_run_through_cron(datasette, idb, link, scheduler, data_db):
    """The one test that touches cron internals: trigger the task, then
    await its in-flight execution (cron's own tests/test_cron.py pattern)."""
    created = await link(interval_minutes=10, key="id")
    await sync_task(datasette, created)
    name = task_name(created.id)

    await scheduler.trigger_task(name)
    in_flight = list(scheduler._running_tasks[name])
    await asyncio.wait_for(asyncio.gather(*in_flight), timeout=10)

    [run] = await idb.list_runs(created.id)
    assert (run.trigger, run.actor_id, run.status) == ("scheduled", "alice", "success")
    assert run.rows_written == 5
    [cron_run] = await scheduler.internal_db.get_runs(name)
    assert cron_run.status == "success"
    rows = await data_db.execute("select count(*) from students")
    assert rows.single_value() == 5
