"""The links and runs tables in the internal DB (ticket 06)."""

import asyncio
import json
import sqlite3

import pytest
import pytest_asyncio
from datasette.app import Datasette
from fixtures_sheets import make_datasette
from sqlite_utils import Database as SqliteUtilsDatabase

from datasette_google_sheets.internal_db import (
    LINKS,
    RUNS,
    ColumnMapping,
    ExportOptions,
    ExportTabTaken,
    ImportMapping,
    InternalDB,
    Link,
    LinkNotFound,
    TableAlreadySynced,
)
from datasette_google_sheets.internal_migrations import internal_migrations
from datasette_google_sheets.permissions import can_operate_link, can_view_link

MAPPING = {
    "headers_row": True,
    "source_headers": ["Name", "Age"],
    "columns": [
        {"source": "Name", "name": "name", "type": "TEXT", "skip": False},
        {"source": "Age", "name": "age", "type": "INTEGER", "skip": False},
    ],
    "key": "name",
}


@pytest_asyncio.fixture
async def ds() -> Datasette:
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    return datasette


@pytest.fixture
def idb(ds: Datasette) -> InternalDB:
    return InternalDB.for_datasette(ds)


async def import_link(idb: InternalDB, **overrides) -> Link:
    fields = {
        "direction": "import",
        "mode": "create",
        "owner_id": "alice",
        "credential_id": "cred1",
        "database_name": "data",
        "table_name": "people",
        "spreadsheet_id": "sheet1",
        "sheet_gid": 0,
        "mapping": MAPPING,
    }
    return await idb.create_link(**{**fields, **overrides})


async def export_link(idb: InternalDB, **overrides) -> Link:
    fields = {
        "direction": "export",
        "mode": "replace",
        "owner_id": "alice",
        "credential_id": "cred1",
        "database_name": "data",
        "table_name": "people",
        "source_kind": "table",
        "spreadsheet_id": "sheet1",
        "sheet_gid": 0,
        "options": {"header_row": True},
    }
    return await idb.create_link(**{**fields, **overrides})


# ------------------------------------------------------------------ schema


def test_migrations_apply_twice_idempotently():
    conn = sqlite3.connect(":memory:")
    db = SqliteUtilsDatabase(conn)
    internal_migrations.apply(db)
    schema = db.schema
    internal_migrations.apply(db)
    assert db.schema == schema
    assert {LINKS, RUNS} <= set(db.table_names())
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "datasette_google_sheets_links_owner",
        "datasette_google_sheets_links_table",
        "datasette_google_sheets_links_export_tab",
        "datasette_google_sheets_links_sync_table",
        "datasette_google_sheets_runs_link",
    } <= indexes


@pytest.mark.asyncio
async def test_startup_applies_migrations_and_restart_is_idempotent(tmp_path):
    internal = str(tmp_path / "internal.db")
    for _ in range(2):
        datasette = Datasette(memory=True, internal=internal)
        await datasette.invoke_startup()
        tables = await datasette.get_internal_database().table_names()
        assert {LINKS, RUNS} <= set(tables)


# -------------------------------------------------------------------- CRUD


@pytest.mark.asyncio
async def test_create_and_get_link_round_trips(idb: InternalDB):
    link = await import_link(idb, interval_minutes=10, created_table=True)
    fetched = await idb.get_link(link.id)
    assert fetched == link
    assert len(link.id) == 26  # ULID
    assert link.enabled is True
    assert link.created_table is True
    assert link.status == "ok"
    assert link.consecutive_failures == 0
    assert link.created_at == link.updated_at
    assert link.created_at.endswith("Z")
    assert link.synced and link.scheduled
    # JSON columns are parsed at the boundary.
    assert isinstance(link.mapping, ImportMapping)
    assert link.mapping.key == "name"
    assert link.mapping.columns[1] == ColumnMapping(
        source="Age", name="age", type="INTEGER"
    )
    assert link.mapping.model_dump() == MAPPING


@pytest.mark.asyncio
async def test_json_columns_are_stored_as_json_text(ds: Datasette, idb: InternalDB):
    link = await export_link(
        idb,
        source_kind="sql",
        table_name=None,
        sql="select * from t where x = :x",
        params={"x": "1"},
    )
    await idb.update_link(link.id, status_data={"added": ["C"], "removed": []})
    row = (
        await ds.get_internal_database().execute(
            f"SELECT params, options, status_data, mapping FROM {LINKS} WHERE id = ?",
            [link.id],
        )
    ).first()
    assert json.loads(row["params"]) == {"x": "1"}
    assert json.loads(row["options"]) == {"header_row": True}
    assert json.loads(row["status_data"]) == {"added": ["C"], "removed": []}
    assert row["mapping"] is None
    fetched = await idb.get_link(link.id)
    assert fetched is not None
    assert fetched.params == {"x": "1"}
    assert fetched.options == ExportOptions(header_row=True)
    assert fetched.status_data == {"added": ["C"], "removed": []}


@pytest.mark.asyncio
async def test_get_unknown_link(idb: InternalDB):
    assert await idb.get_link("nope") is None
    assert await idb.update_link("nope", enabled=False) is None
    assert await idb.delete_link("nope") is False


@pytest.mark.asyncio
async def test_list_links_newest_first_and_by_owner(idb: InternalDB):
    a1 = await import_link(idb, table_name="t1")
    b1 = await import_link(idb, owner_id="bob", table_name="t2")
    a2 = await export_link(idb)
    assert [link.id for link in await idb.list_links()] == [a2.id, b1.id, a1.id]
    assert [link.id for link in await idb.list_links(owner_id="alice")] == [
        a2.id,
        a1.id,
    ]
    assert await idb.list_links(owner_id="carol") == []


@pytest.mark.asyncio
async def test_update_link_bumps_updated_at(idb: InternalDB):
    link = await import_link(idb)
    await asyncio.sleep(0.01)  # timestamps have millisecond resolution
    updated = await idb.update_link(
        link.id,
        enabled=False,
        status="paused",
        status_code="headers_changed",
        sheet_title="Renamed",
        consecutive_failures=3,
    )
    assert updated is not None
    assert updated.enabled is False
    assert updated.status == "paused"
    assert updated.sheet_title == "Renamed"
    assert updated.consecutive_failures == 3
    assert updated.updated_at > link.updated_at
    assert updated.created_at == link.created_at
    assert await idb.get_link(link.id) == updated


@pytest.mark.asyncio
async def test_update_link_accepts_models_for_json_columns(idb: InternalDB):
    link = await import_link(idb)
    mapping = ImportMapping.model_validate({**MAPPING, "key": None})
    updated = await idb.update_link(link.id, mapping=mapping)
    assert updated is not None and updated.mapping == mapping
    fetched = await idb.get_link(link.id)
    assert fetched is not None and fetched.mapping == mapping


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"id": "x"},
        {"owner_id": "bob"},
        {"direction": "export"},
        {"created_at": "x"},
        {"updated_at": "x"},
        {"not_a_column": 1},
        {"status": "bogus"},
        {"mode": "new"},  # an export mode on an import
        {"interval_minutes": 0},
    ],
)
async def test_update_link_rejects(idb: InternalDB, fields):
    link = await import_link(idb)
    with pytest.raises(ValueError):
        await idb.update_link(link.id, **fields)
    assert await idb.get_link(link.id) == link


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"direction": "import", "mode": "new"},
        {"direction": "import", "source_kind": "table"},
        {"direction": "import", "table_name": None},
        {"direction": "export", "mode": "upsert"},
        {"direction": "export", "source_kind": None},
        {"direction": "export", "source_kind": "query"},  # no query_name
        {"direction": "export", "source_kind": "sql", "table_name": None},  # no sql
        {"direction": "import", "mode": "append", "interval_minutes": 10},
        {"direction": "export", "mode": "append", "interval_minutes": 10},
        {"direction": "import", "mapping": {"columns": []}},  # no source_headers
    ],
)
async def test_create_link_rejects_invalid_combinations(idb: InternalDB, overrides):
    make = import_link if overrides.pop("direction") == "import" else export_link
    with pytest.raises(ValueError):
        await make(idb, **overrides)
    assert await idb.list_links() == []


@pytest.mark.asyncio
async def test_delete_link_deletes_its_runs(ds: Datasette, idb: InternalDB):
    link = await import_link(idb)
    other = await import_link(idb, table_name="other")
    await idb.start_run(link.id, trigger="manual", actor_id="alice")
    await idb.start_run(other.id, trigger="manual", actor_id="alice")
    assert await idb.delete_link(link.id) is True
    assert await idb.get_link(link.id) is None
    assert await idb.list_runs(link.id) == []
    assert len(await idb.list_runs(other.id)) == 1
    count = (
        await ds.get_internal_database().execute(
            f"SELECT count(*) FROM {RUNS} WHERE link_id = ?", [link.id]
        )
    ).single_value()
    assert count == 0


@pytest.mark.asyncio
async def test_link_satisfies_link_owner(idb: InternalDB):
    link = await import_link(idb)
    assert can_operate_link({"id": "alice"}, link)
    assert not can_view_link({"id": "bob"}, link, admin=False)


# ------------------------------------------------------------- constraints


@pytest.mark.asyncio
async def test_one_export_link_per_tab(idb: InternalDB):
    first = await export_link(idb)
    with pytest.raises(ExportTabTaken) as info:
        await export_link(idb, table_name="other")
    assert info.value.existing_id == first.id
    assert "sheet1" not in str(info.value)
    # Another tab, another spreadsheet, or an import from the tab are fine.
    await export_link(idb, sheet_gid=1)
    await export_link(idb, spreadsheet_id="sheet2")
    await import_link(idb)
    # Retargeting an existing export onto the taken tab is refused too.
    moved = await export_link(idb, sheet_gid=2)
    with pytest.raises(ExportTabTaken):
        await idb.update_link(moved.id, sheet_gid=0)


@pytest.mark.asyncio
async def test_one_sync_per_table(idb: InternalDB):
    synced = await import_link(idb, interval_minutes=10)
    with pytest.raises(TableAlreadySynced) as info:
        await import_link(idb, mode="replace", interval_minutes=5, sheet_gid=9)
    assert info.value.existing_id == synced.id
    # One-shot imports into the table and syncs of other tables are fine.
    one_shot = await import_link(idb, mode="replace")
    await import_link(idb, database_name="other", interval_minutes=10)
    await import_link(idb, table_name="other", interval_minutes=10)
    # Converting a one-shot link to synced is refused while the sync exists...
    with pytest.raises(TableAlreadySynced):
        await idb.update_link(one_shot.id, interval_minutes=10)
    # ...and allowed once it's unlinked.
    await idb.update_link(synced.id, interval_minutes=None)
    converted = await idb.update_link(one_shot.id, interval_minutes=10)
    assert converted is not None and converted.synced
    assert await idb.synced_tables() == {
        ("data", "people"),
        ("other", "people"),
        ("data", "other"),
    }


@pytest.mark.asyncio
async def test_unique_indexes_hold_without_the_python_check(
    ds: Datasette, idb: InternalDB
):
    export = await export_link(idb)
    synced = await import_link(idb, interval_minutes=10)

    def duplicate(conn, source_id, new_id):
        conn.execute(
            f"INSERT INTO {LINKS} SELECT ? AS id, direction, mode, owner_id,"
            " credential_id, database_name, table_name, source_kind, query_name,"
            " sql, params, spreadsheet_id, sheet_gid, spreadsheet_title,"
            " sheet_title, mapping, options, interval_minutes, created_table,"
            " created_schema, enabled, status, status_code, status_detail,"
            " status_data, consecutive_failures, last_run_at, last_success_at,"
            f" last_hash, created_at, updated_at FROM {LINKS} WHERE id = ?",
            [new_id, source_id],
        )

    db = ds.get_internal_database()
    for source_id in (export.id, synced.id):
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute_write_fn(
                lambda conn, source_id=source_id: duplicate(conn, source_id, "dup")
            )


@pytest.mark.asyncio
async def test_pending_new_spreadsheet_exports_coexist(idb: InternalDB):
    """D33: spreadsheet_id '' marks a new-spreadsheet export before its first
    run. It holds no tab, so several can exist at once."""
    first = await export_link(idb, mode="new", spreadsheet_id="")
    second = await export_link(idb, mode="new", spreadsheet_id="", table_name="t2")
    assert {link.id for link in await idb.list_links()} == {first.id, second.id}
    # Once created, the spreadsheet's tab is taken like any other.
    await idb.update_link(first.id, spreadsheet_id="created", sheet_gid=0)
    with pytest.raises(ExportTabTaken) as info:
        await idb.update_link(second.id, spreadsheet_id="created", sheet_gid=0)
    assert info.value.existing_id == first.id


@pytest.mark.asyncio
async def test_export_tab_index_skips_only_pending_new_spreadsheets(
    ds: Datasette, idb: InternalDB
):
    """The partial unique index, without the Python pre-check: '' rows may
    repeat, real (spreadsheet_id, sheet_gid) pairs may not."""
    pending = await export_link(idb, mode="new", spreadsheet_id="")
    real = await export_link(idb)

    def duplicate(conn, source_id, new_id):
        conn.execute(
            f"INSERT INTO {LINKS} SELECT ? AS id, direction, mode, owner_id,"
            " credential_id, database_name, table_name, source_kind, query_name,"
            " sql, params, spreadsheet_id, sheet_gid, spreadsheet_title,"
            " sheet_title, mapping, options, interval_minutes, created_table,"
            " created_schema, enabled, status, status_code, status_detail,"
            " status_data, consecutive_failures, last_run_at, last_success_at,"
            f" last_hash, created_at, updated_at FROM {LINKS} WHERE id = ?",
            [new_id, source_id],
        )

    db = ds.get_internal_database()
    await db.execute_write_fn(lambda conn: duplicate(conn, pending.id, "dup-pending"))
    assert await idb.get_link("dup-pending") is not None
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute_write_fn(lambda conn: duplicate(conn, real.id, "dup-real"))


@pytest.mark.asyncio
async def test_scheduled_links_includes_disabled(idb: InternalDB):
    await import_link(idb)
    synced = await import_link(idb, table_name="t2", interval_minutes=10)
    export = await export_link(idb, interval_minutes=30)
    await idb.update_link(export.id, enabled=False, status="paused")
    assert [link.id for link in await idb.scheduled_links()] == [synced.id, export.id]
    assert await idb.synced_tables() == {("data", "t2")}


# -------------------------------------------------------------------- runs


@pytest.mark.asyncio
async def test_run_lifecycle(idb: InternalDB):
    link = await import_link(idb)
    run = await idb.start_run(link.id, trigger="scheduled", actor_id=None)
    assert run.status == "running"
    assert run.trigger == "scheduled"
    assert run.finished_at is None
    assert run.warnings == []
    finished = await idb.finish_run(
        run.id,
        status="success",
        rows_read=3,
        rows_written=3,
        added=1,
        changed=1,
        removed=1,
        cells=6,
        warnings=["2 values in age are not INTEGER"],
        hash="abc",
    )
    assert finished is not None
    assert finished.status == "success"
    assert finished.finished_at is not None
    assert finished.warnings == ["2 values in age are not INTEGER"]
    assert (finished.rows_read, finished.added, finished.cells) == (3, 1, 6)
    assert await idb.list_runs(link.id) == [finished]


@pytest.mark.asyncio
async def test_error_run(idb: InternalDB):
    link = await import_link(idb)
    run = await idb.start_run(link.id, trigger="manual", actor_id="alice")
    finished = await idb.finish_run(
        run.id,
        status="error",
        error_code="headers_changed",
        error_message="The sheet's headers changed",
    )
    assert finished is not None
    assert (finished.status, finished.error_code) == ("error", "headers_changed")
    assert finished.actor_id == "alice"


@pytest.mark.asyncio
async def test_run_validation(idb: InternalDB):
    link = await import_link(idb)
    with pytest.raises(LinkNotFound):
        await idb.start_run("nope", trigger="manual", actor_id="alice")
    with pytest.raises(ValueError):
        await idb.start_run(link.id, trigger="cron", actor_id=None)  # type: ignore[arg-type]
    run = await idb.start_run(link.id, trigger="manual", actor_id="alice")
    with pytest.raises(ValueError):
        await idb.finish_run(run.id, status="running")  # type: ignore[arg-type]
    assert await idb.finish_run(999_999, status="success") is None


@pytest.mark.asyncio
async def test_start_run_prunes_to_configured_retention(mock_google):
    ds = await make_datasette(mock_google, sheets_config={"runs_retain_per_link": 3})
    idb = InternalDB.for_datasette(ds)
    assert idb.runs_retain_per_link == 3
    link = await import_link(idb)
    other = await import_link(idb, table_name="other")
    other_run = await idb.start_run(other.id, trigger="manual", actor_id="a")
    ids = [
        (await idb.start_run(link.id, trigger="manual", actor_id="a")).id
        for _ in range(5)
    ]
    assert [run.id for run in await idb.list_runs(link.id)] == ids[::-1][:3]
    # Other links' history is untouched.
    assert [run.id for run in await idb.list_runs(other.id)] == [other_run.id]
    assert [run.id for run in await idb.list_runs(link.id, limit=2)] == ids[::-1][:2]


@pytest.mark.asyncio
async def test_default_retention_is_100(idb: InternalDB):
    assert idb.runs_retain_per_link == 100


@pytest.mark.asyncio
async def test_prune_runs(idb: InternalDB):
    link = await import_link(idb)
    ids = [
        (await idb.start_run(link.id, trigger="manual", actor_id="a")).id
        for _ in range(4)
    ]
    assert await idb.prune_runs(link.id, keep=1) == 3
    assert [run.id for run in await idb.list_runs(link.id)] == [ids[-1]]
    assert await idb.prune_runs(link.id, keep=1) == 0


@pytest.mark.asyncio
async def test_abandoned_runs_recovered_on_startup(tmp_path):
    internal = str(tmp_path / "internal.db")
    first = Datasette(memory=True, internal=internal)
    await first.invoke_startup()
    idb = InternalDB.for_datasette(first)
    link = await import_link(idb)
    done = await idb.start_run(link.id, trigger="manual", actor_id="alice")
    await idb.finish_run(done.id, status="success")
    orphan = await idb.start_run(link.id, trigger="scheduled", actor_id=None)

    # A new process on the same internal DB, as after a crash.
    second = Datasette(memory=True, internal=internal)
    await second.invoke_startup()
    runs = {
        run.id: run for run in await InternalDB.for_datasette(second).list_runs(link.id)
    }
    assert runs[orphan.id].status == "error"
    assert runs[orphan.id].error_code == "abandoned"
    assert runs[orphan.id].finished_at is not None
    assert runs[done.id].status == "success"
    assert runs[done.id].error_code is None


@pytest.mark.asyncio
async def test_mark_abandoned_runs_counts(idb: InternalDB):
    link = await import_link(idb)
    await idb.start_run(link.id, trigger="manual", actor_id="a")
    await idb.start_run(link.id, trigger="manual", actor_id="a")
    assert await idb.mark_abandoned_runs() == 2
    assert await idb.mark_abandoned_runs() == 0
