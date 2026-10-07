"""run_link (ticket 09): the acting actor, permissions, run history, link
status and auto-pause, against the vendored mock Google."""

import ast
import asyncio
import copy
import functools
import logging
import sqlite3
from pathlib import Path

import datasette_google_credentials
import httpx2
import pytest
import pytest_asyncio
from datasette import hookimpl
from datasette.permissions import PermissionSQL
from datasette.plugins import pm
from datasette.resources import DatabaseResource, TableResource
from datasette_google_credentials import (
    CredentialBroken,
    CredentialChanged,
    CredentialForbidden,
    CredentialNotFound,
    CredentialUndecryptable,
    EncryptionNotConfigured,
    GoogleTokenError,
    MissingScopes,
    connect_url,
)
from fixtures_export import seed_export_data
from fixtures_import import DATA_DB
from fixtures_sheets import ALICE, BOB, make_datasette
from mock_google.keys import SA_OTHER, SA_TEST

from datasette_google_sheets import exporter, runner
from datasette_google_sheets.exporter import ExportError
from datasette_google_sheets.importer import ImporterError
from datasette_google_sheets.internal_db import InternalDB
from datasette_google_sheets.runner import (
    RetryableRunError,
    RunRefused,
    classify,
    owner_actor,
    run_link,
    set_on_paused,
)
from datasette_google_sheets.sheets import SheetsError

RUNNER_MODULE = Path(runner.__file__)
WRITE_ACTIONS = ("create-table", "insert-row", "update-row", "delete-row")
# alice may write to `data`, at database level.
RUNNER_CONFIG = {
    "databases": {
        DATA_DB: {"permissions": {action: {"id": "alice"} for action in WRITE_ACTIONS}}
    }
}
STUDENTS = [
    ["id", "name", "grade_level", "email"],
    [1, "Alice Chen", 10, "alice@school.edu"],
    [2, "Bob Jones", 11, "bob@school.edu"],
    [3, "Clara Smith", 10, "clara@school.edu"],
]
CELL_VALUES = ["Alice Chen", "Bob Jones", "alice@school.edu", "Clara Smith"]


@pytest_asyncio.fixture
async def datasette(mock_google):
    return await make_datasette(mock_google, config=copy.deepcopy(RUNNER_CONFIG))


@pytest.fixture
def idb(datasette):
    return InternalDB.for_datasette(datasette)


@pytest.fixture
def paused_calls(datasette):
    """Links passed to the on_paused hook (ticket 10 wires it to cron)."""
    calls = []

    async def on_paused(ds, link):
        assert ds is datasette
        calls.append(link)

    set_on_paused(datasette, on_paused)
    return calls


@pytest.fixture
def link(idb, import_link, sa_credential):
    """``await link(table="students", key="other" SA key, **import_link
    kwargs)``: an import link owned by alice, using her service account."""

    async def make(table="students", *, sa_key="test", **kwargs):
        info = await sa_credential("alice", key=sa_key)
        created = await import_link(table, **kwargs)
        return await idb.update_link(created.id, credential_id=info.id)

    return make


def grants(datasette):
    return datasette.config["databases"][DATA_DB]["permissions"]


class DenyTableWrites:
    """What ticket 11 adds for synced tables (D7): a table-level deny of the
    write actions, for every actor."""

    def __init__(self, table):
        self.table = table

    @hookimpl
    def permission_resources_sql(self, datasette, actor, action):
        if action in ("insert-row", "update-row", "delete-row"):
            return PermissionSQL(
                sql="SELECT :db AS parent, :table AS child, 0 AS allow,"
                " 'synced table' AS reason",
                params={"db": DATA_DB, "table": self.table},
            )
        return None


class TeamActors:
    """actors_from_ids: alice gets a ``team``; unknown ids get nothing."""

    @hookimpl
    def actors_from_ids(self, datasette, actor_ids):
        return {
            actor_id: {"id": actor_id, "team": "sheets"}
            for actor_id in actor_ids
            if actor_id == "alice"
        }


@pytest.fixture
def plugin():
    registered = []

    def register(obj):
        name = f"test-runner-{len(registered)}"
        pm.register(obj, name=name)
        registered.append(name)

    yield register
    for name in registered:
        pm.unregister(name=name)


# --------------------------------------------------------------- guards


def test_uses_only_the_public_google_credentials_api():
    tree = ast.parse(RUNNER_MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("datasette_google_credentials"):
                assert node.module == "datasette_google_credentials", node.module
                imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            assert not any(
                a.name.startswith("datasette_google_credentials") for a in node.names
            )
    assert imported <= set(datasette_google_credentials.__all__)


# -------------------------------------------------------------- success


@pytest.mark.asyncio
async def test_manual_run_creates_the_table_and_persists_the_link(
    datasette, idb, link, data_db
):
    created = await link(key="id")
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.status, outcome.code, outcome.link_status) == (
        "success",
        None,
        "ok",
    )
    assert outcome.result.rows_written == 5

    stored = await idb.get_link(created.id)
    assert stored.created_table
    assert stored.created_schema.startswith("CREATE TABLE")
    assert stored.last_hash == outcome.result.hash
    assert (stored.spreadsheet_title, stored.sheet_title) == ("Students", "students")
    assert stored.status == "ok" and stored.consecutive_failures == 0
    assert stored.last_run_at and stored.last_success_at == stored.last_run_at

    [run] = await idb.list_runs(created.id)
    assert run.id == outcome.run_id
    assert (run.trigger, run.actor_id, run.status) == ("manual", "alice", "success")
    assert (run.rows_read, run.rows_written, run.added, run.cells) == (5, 5, 5, 20)
    assert run.hash == stored.last_hash

    # Unchanged sheet: no_change, until forced.
    again = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert again.status == "no_change"
    forced = await run_link(
        datasette, created.id, trigger="manual", actor=ALICE, force=True
    )
    assert forced.status == "success"
    assert [r.status for r in await idb.list_runs(created.id)] == [
        "success",
        "no_change",
        "success",
    ]


@pytest.mark.asyncio
async def test_run_rows_record_counts_and_warnings(datasette, idb, link, data_db):
    # "name" as INTEGER: 5 type mismatches, stored as they are (D10).
    created = await link(types={"id": "INTEGER", "name": "INTEGER"})
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert outcome.status == "success"
    [run] = await idb.list_runs(created.id)
    assert run.warnings == ["Column 'name': 5 values not INTEGER, stored as they are"]
    assert (run.rows_read, run.rows_written) == (5, 5)


# --------------------------------------------------------------- actors


@pytest.mark.asyncio
async def test_scheduled_run_acts_as_the_resolved_owner(
    datasette, idb, link, data_db, plugin
):
    """D5: the stored owner via actors_from_ids, with its extra attributes.
    The grants here need ``team``, which only the resolved actor has."""
    plugin(TeamActors())
    for action in WRITE_ACTIONS:
        grants(datasette)[action] = {"team": "sheets"}
    created = await link()
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert outcome.status == "success"
    [run] = await idb.list_runs(created.id)
    assert (run.trigger, run.actor_id) == ("scheduled", "alice")


@pytest.mark.asyncio
async def test_owner_actor_falls_back_to_the_id(datasette, plugin):
    plugin(TeamActors())
    assert await owner_actor(datasette, "alice") == {"id": "alice", "team": "sheets"}
    assert await owner_actor(datasette, "carol") == {"id": "carol"}


@pytest.mark.asyncio
async def test_manual_run_by_a_non_owner_is_refused(datasette, idb, link, data_db):
    created = await link()
    for actor in (BOB, None, {"id": "root"}):
        with pytest.raises(RunRefused) as excinfo:
            await run_link(datasette, created.id, trigger="manual", actor=actor)
        assert excinfo.value.code == "not_owner"
    with pytest.raises(RunRefused) as excinfo:
        await run_link(datasette, "nope", trigger="manual", actor=ALICE)
    assert excinfo.value.code == "not_found"
    assert await idb.list_runs(created.id) == []


@pytest.mark.asyncio
async def test_paused_link_skips_scheduled_runs_and_refuses_manual_ones(
    datasette, idb, link, data_db
):
    created = await link(interval_minutes=10)
    await idb.update_link(created.id, enabled=False, status="paused")
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert (outcome.status, outcome.run_id) == ("skipped", None)
    with pytest.raises(RunRefused) as excinfo:
        await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert excinfo.value.code == "paused"
    assert await idb.list_runs(created.id) == []
    # A deleted link (its cron task not yet removed) is skipped too.
    await idb.delete_link(created.id)
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert outcome.status == "skipped"


# ----------------------------------------------------------- permissions


@pytest.mark.asyncio
async def test_losing_create_table_pauses(datasette, idb, link, data_db, paused_calls):
    del grants(datasette)["create-table"]
    created = await link(interval_minutes=10)
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert (outcome.status, outcome.code, outcome.link_status) == (
        "error",
        "permission_lost",
        "paused",
    )
    stored = await idb.get_link(created.id)
    assert not stored.enabled and stored.status == "paused"
    assert stored.status_code == "permission_lost"
    assert stored.status_data == {"actions": ["create-table"]}
    assert [p.id for p in paused_calls] == [created.id]
    [run] = await idb.list_runs(created.id)
    assert (run.status, run.error_code) == ("error", "permission_lost")
    assert not await data_db.table_exists("students")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,key,revoke,denied",
    [
        ("append", None, "insert-row", ["insert-row"]),
        ("replace", None, "delete-row", ["delete-row"]),
        ("upsert", "id", "update-row", ["update-row"]),
    ],
)
async def test_losing_a_write_permission_pauses(
    datasette, idb, link, data_db, paused_calls, mode, key, revoke, denied
):
    await data_db.execute_write(
        "create table students (id integer, name, grade_level integer, email)"
    )
    created = await link(mode=mode, key=key)
    assert (
        await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    ).status == "success"
    del grants(datasette)[revoke]
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("permission_lost", "paused")
    assert (await idb.get_link(created.id)).status_data == {"actions": denied}
    # Not scheduled, so no cron task to disable.
    assert paused_calls == []


@pytest.mark.asyncio
async def test_synced_table_write_permission_is_checked_on_the_database(
    datasette, idb, link, data_db, plugin
):
    """The ``← verify`` item: a synced table has a table-level deny of the
    write actions (D7). ``allowed()`` with a DatabaseResource sees only the
    database-level and global rules, so the owner's database grant still
    counts; with a TableResource the deny wins."""
    plugin(DenyTableWrites("students"))
    table = TableResource(DATA_DB, "students")
    database = DatabaseResource(DATA_DB)
    for action in ("insert-row", "update-row", "delete-row"):
        assert not await datasette.allowed(action=action, resource=table, actor=ALICE)
        assert await datasette.allowed(action=action, resource=database, actor=ALICE)
        assert not await datasette.allowed(action=action, resource=database, actor=BOB)

    synced = await link(interval_minutes=10, key="id")
    first = await run_link(datasette, synced.id, trigger="scheduled")
    assert first.status == "success"
    # The sync of the created table needs insert/update/delete-row.
    again = await run_link(datasette, synced.id, trigger="scheduled", force=True)
    assert again.status == "success"

    # A one-shot append to the same table is checked on the table: denied.
    one_shot = await link(mode="append")
    outcome = await run_link(datasette, one_shot.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("permission_lost", "paused")

    # And losing the database grant pauses the sync.
    del grants(datasette)["delete-row"]
    lost = await run_link(datasette, synced.id, trigger="scheduled", force=True)
    assert lost.code == "permission_lost"


# ------------------------------------------------------------ references


@pytest.mark.asyncio
async def test_missing_or_immutable_database_pauses(
    datasette, idb, link, data_db, monkeypatch
):
    created = await link()
    monkeypatch.setattr(data_db, "is_mutable", False)
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("database_immutable", "paused")

    other = await link("elsewhere")
    await idb.update_link(other.id, database_name="gone")
    outcome = await run_link(datasette, other.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("database_missing", "paused")


@pytest.mark.asyncio
async def test_dropped_table_pauses_before_the_permission_check(
    datasette, idb, link, data_db
):
    created = await link(interval_minutes=10)
    await run_link(datasette, created.id, trigger="scheduled")
    await data_db.execute_write("drop table students")
    del grants(datasette)["insert-row"]
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert (outcome.code, outcome.link_status) == ("table_missing", "paused")
    assert not await data_db.table_exists("students")  # never re-created


@pytest.mark.asyncio
async def test_scheduled_create_into_an_existing_table_pauses(
    datasette, idb, link, data_db, paused_calls
):
    await data_db.execute_write("create table students (x)")
    created = await link(interval_minutes=10)
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert (outcome.code, outcome.link_status) == ("table_exists", "paused")
    stored = await idb.get_link(created.id)
    assert not stored.enabled and stored.status_code == "table_exists"
    assert [p.id for p in paused_calls] == [created.id]


# ------------------------------------------------------------ credentials


@pytest.mark.asyncio
async def test_broken_oauth_credential_pauses_with_a_reconnect_url(
    datasette, idb, import_link, oauth_credential, data_db, mock_google, paused_calls
):
    info = await oauth_credential("alice")
    created = await import_link("students", interval_minutes=10)
    created = await idb.update_link(created.id, credential_id=info.id)
    for refresh_token in mock_google.oauth.refresh_tokens():
        mock_google.oauth.revoke(refresh_token)
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    reconnect = connect_url(datasette, return_to=f"/-/google-sheets/links/{created.id}")
    assert (outcome.code, outcome.link_status) == ("credential_broken", "paused")
    assert outcome.reconnect_url == reconnect
    assert "Reconnect Google" in outcome.message
    stored = await idb.get_link(created.id)
    assert stored.status_data == {"reconnect_url": reconnect}
    assert [p.id for p in paused_calls] == [created.id]


@pytest.mark.asyncio
async def test_deleted_credential_pauses(datasette, idb, link, data_db):
    created = await link()
    await idb.update_link(created.id, credential_id="deleted")
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("credential_missing", "paused")
    assert outcome.reconnect_url is None


@pytest.mark.asyncio
async def test_service_account_not_shared_says_share_as_viewer(
    datasette, idb, link, data_db
):
    created = await link(sa_key="other")
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("not_shared", "paused")
    assert outcome.share_with == SA_OTHER
    assert f"share the sheet with {SA_OTHER} as Viewer" in outcome.message


# ------------------------------------------------------------------ drift


@pytest.mark.asyncio
async def test_headers_changed_pauses_with_the_diff(
    datasette, idb, link, data_db, mock_google
):
    created = await link(interval_minutes=10)
    assert (
        await run_link(datasette, created.id, trigger="scheduled")
    ).status == "success"
    mock_google.sheets.add(
        "students",
        {
            "students": [
                ["id", "name", "grade", "email", "phone"],
                [1, "A", 9, "a", "1"],
            ]
        },
        title="Students",
    )
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert (outcome.code, outcome.link_status) == ("headers_changed", "paused")
    stored = await idb.get_link(created.id)
    assert stored.status_code == "headers_changed"
    assert stored.status_data == {
        "added": ["grade", "phone"],
        "removed": ["grade_level"],
    }
    assert "grade_level" in stored.status_detail


# -------------------------------------------------------------- transient


@pytest.mark.asyncio
async def test_transient_500_is_an_error_and_raises_only_when_scheduled(
    datasette, idb, link, data_db, mock_google
):
    created = await link(interval_minutes=10)
    mock_google.faults.fail("/v4/spreadsheets/students", 500)
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.status, outcome.code, outcome.link_status) == (
        "error",
        "server",
        "error",
    )
    stored = await idb.get_link(created.id)
    assert stored.enabled and stored.status == "error"
    assert stored.consecutive_failures == 1

    mock_google.faults.fail("/v4/spreadsheets/students", 500)
    with pytest.raises(RetryableRunError) as excinfo:
        await run_link(datasette, created.id, trigger="scheduled")
    assert excinfo.value.code == "server"
    assert excinfo.value.__suppress_context__
    assert str(excinfo.value) == (
        f"Google Sheets link {created.id} failed with a transient error (server)"
    )
    stored = await idb.get_link(created.id)
    assert (stored.status, stored.consecutive_failures) == ("error", 2)
    runs = await idb.list_runs(created.id)
    assert [(r.status, r.error_code) for r in runs] == [("error", "server")] * 2


@pytest.mark.asyncio
async def test_five_failures_pause_and_a_success_resets_the_counter(
    datasette, idb, link, data_db, mock_google, paused_calls
):
    created = await link(interval_minutes=10)
    mock_google.faults.fail("/v4/spreadsheets/students", 503, times=4)
    for _ in range(4):
        with pytest.raises(RetryableRunError):
            await run_link(datasette, created.id, trigger="scheduled")
    assert (await idb.get_link(created.id)).consecutive_failures == 4
    assert (
        await run_link(datasette, created.id, trigger="scheduled")
    ).status == "success"
    stored = await idb.get_link(created.id)
    assert (stored.status, stored.consecutive_failures) == ("ok", 0)
    assert stored.status_code is None and stored.status_data is None

    mock_google.faults.fail("/v4/spreadsheets/students", 429, times=5)
    for _ in range(4):
        with pytest.raises(RetryableRunError):
            await run_link(datasette, created.id, trigger="scheduled", force=True)
    # The fifth pauses instead of raising: nothing left to retry.
    outcome = await run_link(datasette, created.id, trigger="scheduled", force=True)
    assert (outcome.code, outcome.link_status) == ("failing_repeatedly", "paused")
    stored = await idb.get_link(created.id)
    assert not stored.enabled
    assert stored.status_data == {"last_code": "rate_limited"}
    assert stored.consecutive_failures == 5
    assert [p.id for p in paused_calls] == [created.id]


@pytest.mark.asyncio
async def test_a_failing_pause_hook_still_pauses(datasette, idb, link, data_db, caplog):
    async def broken(ds, link):
        raise RuntimeError("cron is down")

    set_on_paused(datasette, broken)
    created = await link(interval_minutes=10, sa_key="other")
    outcome = await run_link(datasette, created.id, trigger="scheduled")
    assert outcome.link_status == "paused"
    assert "RuntimeError" in caplog.text


# ----------------------------------------------------------- concurrency


@pytest.mark.asyncio
async def test_the_lock_serializes_runs_of_one_link(
    datasette, idb, link, data_db, monkeypatch
):
    created = await link(interval_minutes=10, key="id")
    active = 0
    most = 0
    real = runner.run_import

    async def slow_import(*args, **kwargs):
        nonlocal active, most
        active += 1
        most = max(most, active)
        await asyncio.sleep(0.05)
        try:
            return await real(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(runner, "run_import", slow_import)
    scheduled, manual = await asyncio.gather(
        run_link(datasette, created.id, trigger="scheduled"),
        run_link(datasette, created.id, trigger="manual", actor=ALICE),
    )
    assert most == 1
    # The second saw the first's results: the table exists and the hash is
    # stored, so it upserts nothing new.
    assert {scheduled.status, manual.status} == {"success", "no_change"}


# ------------------------------------------------------------------ exports


@pytest_asyncio.fixture
async def export_db(datasette):
    await seed_export_data(datasette)
    return datasette.get_database("data")


@pytest.fixture
def export(idb, export_db, sa_credential, oauth_credential):
    async def make(*, oauth=False, sa_key="test", **fields):
        if oauth:
            info = await oauth_credential("alice")
        else:
            info = await sa_credential("alice", key=sa_key)
        defaults = {
            "direction": "export",
            "mode": "replace",
            "owner_id": "alice",
            "credential_id": info.id,
            "database_name": "data",
            "source_kind": "table",
            "table_name": "people",
            "spreadsheet_id": "export",
            "sheet_gid": 0,
        }
        return await idb.create_link(**{**defaults, **fields})

    return make


@pytest.mark.asyncio
async def test_new_spreadsheet_export_stores_its_id_and_gid(
    datasette, idb, export, mock_google
):
    """D33: the run that creates the spreadsheet stores it on the link."""
    created = await export(oauth=True, mode="new", spreadsheet_id="")
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert outcome.status == "success"
    stored = await idb.get_link(created.id)
    assert stored.spreadsheet_id == outcome.result.spreadsheet_id != ""
    assert stored.sheet_gid == outcome.result.gid
    assert (stored.spreadsheet_title, stored.sheet_title) == ("people", "Sheet1")
    [run] = await idb.list_runs(created.id)
    assert (run.rows_read, run.rows_written, run.cells) == (3, 3, 8)

    # The next run replaces that tab instead of creating another spreadsheet.
    count = len(mock_google.sheets.spreadsheets)
    again = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert again.result.spreadsheet_id == stored.spreadsheet_id
    assert len(mock_google.sheets.spreadsheets) == count


@pytest.mark.asyncio
async def test_export_not_shared_says_share_as_editor(datasette, export, mock_google):
    created = await export(spreadsheet_id="readonly")
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("not_shared", "paused")
    assert outcome.share_with == SA_TEST
    assert f"share the sheet with {SA_TEST} as Editor" in outcome.message


@pytest.mark.asyncio
async def test_export_read_permission_lost_pauses(datasette, idb, export, mock_google):
    mock_google.sheets.add("export", {"Sheet1": []})
    datasette.config["databases"]["data"]["tables"] = {
        "people": {"allow": {"id": "bob"}}
    }
    created = await export()
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("permission_lost", "paused")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,code,link_status",
    [
        ({"status": 500}, "server", "error"),
        ({"reason": "SERVICE_DISABLED"}, "api_disabled", "paused"),
    ],
)
async def test_partial_export_pauses_or_retries_by_its_cause(
    datasette, idb, export, mock_google, monkeypatch, fault, code, link_status
):
    mock_google.sheets.add("export", {"Sheet1": []})
    monkeypatch.setattr(
        runner, "run_export", functools.partial(exporter.run_export, chunk_rows=2)
    )
    created = await export()
    mock_google.faults.fail(
        # The clear and the first chunk (header + 1 row) go through.
        "/v4/spreadsheets/export/values/",
        method="POST",
        after=2,
        **fault,
    )
    outcome = await run_link(datasette, created.id, trigger="manual", actor=ALICE)
    assert (outcome.code, outcome.link_status) == ("partial", link_status)
    stored = await idb.get_link(created.id)
    assert stored.status_data == {"rows_written": 1, "cause": code}
    [run] = await idb.list_runs(created.id)
    assert (run.error_code, run.rows_written) == ("partial", 1)


# ----------------------------------------------------------- classification


class _Info:
    def __init__(self, type, google_email):
        self.type = type
        self.google_email = google_email


def _sheets(kind, status=403):
    return SheetsError(status=status, reason=None, message="Google said", kind=kind)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code,action",
    [
        (ImporterError("database_missing", "m"), "database_missing", "pause"),
        (ImporterError("tab_missing", "m"), "tab_missing", "pause"),
        (ImporterError("too_large", "m"), "too_large", "pause"),
        (ImporterError("headers_changed", "m"), "headers_changed", "pause"),
        (ImporterError("empty_sheet", "m"), "empty_sheet", "pause"),
        (ImporterError("bad_key", "m"), "bad_key", "pause"),
        (ImporterError("table_missing", "m"), "table_missing", "pause"),
        (ImporterError("table_changed", "m"), "table_changed", "pause"),
        (ImporterError("table_exists", "m"), "table_exists", "pause"),
        (
            ImporterError("write_failed", "m", data={"transient": True}),
            "write_failed",
            "retry",
        ),
        (
            ImporterError("write_failed", "m", data={"transient": False}),
            "write_failed",
            "error",
        ),
        (ExportError("forbidden", "m"), "permission_lost", "pause"),
        (ExportError("source_missing", "m"), "source_missing", "pause"),
        (ExportError("truncated", "m"), "truncated", "pause"),
        (ExportError("too_large", "m"), "too_large", "pause"),
        (ExportError("sa_cannot_create", "m"), "sa_cannot_create", "pause"),
        (ExportError("tab_missing", "m"), "tab_missing", "pause"),
        (ExportError("read_failed", "m"), "read_failed", "error"),
        (_sheets("not_shared"), "not_shared", "pause"),
        (_sheets("not_found", 404), "not_found", "pause"),
        (_sheets("api_disabled"), "api_disabled", "pause"),
        (_sheets("scope"), "scope", "pause"),
        (_sheets("rate_limited", 429), "rate_limited", "retry"),
        (_sheets("server", 503), "server", "retry"),
        (_sheets("other", 400), "sheets_error", "error"),
        (CredentialBroken("revoked"), "credential_broken", "pause"),
        (CredentialNotFound("c"), "credential_missing", "pause"),
        (CredentialForbidden(), "credential_missing", "pause"),
        (MissingScopes(["s"]), "missing_scopes", "pause"),
        (CredentialUndecryptable("c"), "credential_undecryptable", "pause"),
        (CredentialChanged("c"), "auth_error", "retry"),
        (GoogleTokenError(503), "auth_error", "retry"),
        (GoogleTokenError(None), "auth_error", "retry"),
        (GoogleTokenError(400, "invalid_scope"), "auth_error", "error"),
        (EncryptionNotConfigured(), "auth_error", "error"),
        (httpx2.ReadTimeout("t"), "timeout", "retry"),
        (httpx2.ConnectError("c"), "network", "retry"),
        (
            sqlite3.OperationalError("database is locked"),
            "database_locked",
            "retry",
        ),
        (ValueError("x"), "internal_error", "error"),
    ],
)
async def test_classify(datasette, link, data_db, error, code, action):
    created = await link()
    failure = classify(datasette, created, error, _Info("service_account", SA_TEST))
    assert (failure.code, failure.action) == (code, action)
    assert failure.message


@pytest.mark.asyncio
async def test_classify_partial_export_by_cause(datasette, link, data_db):
    created = await link()
    by_kind = ExportError("partial", "m", rows_written=3, kind="not_shared")
    assert classify(datasette, created, by_kind).action == "pause"
    by_cause = ExportError("partial", "m", rows_written=3)
    by_cause.__cause__ = httpx2.ReadTimeout("t")
    failure = classify(datasette, created, by_cause)
    assert (failure.code, failure.action, failure.data) == (
        "partial",
        "retry",
        {"rows_written": 3, "cause": "timeout"},
    )
    by_auth = ExportError("partial", "m", rows_written=3)
    by_auth.__cause__ = CredentialBroken("revoked")
    assert classify(datasette, created, by_auth).action == "pause"


# ------------------------------------------------------------------ privacy


@pytest.mark.asyncio
async def test_no_cell_values_emails_or_ids_in_logs(
    datasette, idb, link, data_db, mock_google, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    mock_google.sheets.add("students", {"students": STUDENTS}, title="Students")
    ok = await link(interval_minutes=10, key="id")
    await run_link(datasette, ok.id, trigger="scheduled")
    unshared = await link("other_table", sa_key="other")
    await run_link(datasette, unshared.id, trigger="manual", actor=ALICE)
    mock_google.faults.fail("/v4/spreadsheets/students", 500)
    with pytest.raises(RetryableRunError):
        await run_link(datasette, ok.id, trigger="scheduled", force=True)

    async def boom(*args, **kwargs):
        raise ValueError("Alice Chen alice@school.edu")

    monkeypatch.setattr(runner, "run_import", boom)
    outcome = await run_link(datasette, ok.id, trigger="manual", actor=ALICE)
    assert outcome.code == "internal_error"
    assert "ValueError" in caplog.text
    for secret in [*CELL_VALUES, SA_TEST, SA_OTHER, "Students"]:
        assert secret not in caplog.text
