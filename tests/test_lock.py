"""Synced tables are read-only (ticket 11, D7): a table-level deny of the six
write actions, for every actor, that our own sync writes don't see."""

import copy

import pytest
import pytest_asyncio
from datasette.permissions import PermissionSQL
from datasette.resources import DatabaseResource, TableResource
from fixtures_import import DATA_DB
from fixtures_sheets import ALICE, BOB, make_datasette

from datasette_google_sheets.internal_db import LINKS, InternalDB
from datasette_google_sheets.lock import LOCKED_ACTIONS, REASON, synced_table_deny
from datasette_google_sheets.runner import run_link

ROOT = {"id": "root"}
SYNCED = "students"
PLAIN = "plain"
ROW_ACTIONS = ("insert-row", "update-row", "delete-row")
# Every signed-in actor may write to `data` at database level (the runner
# checks a synced link's owner there, D35) and has a table-level config allow
# of every locked action on the synced table, which the lock must beat. They
# match root too: a config block that doesn't match an actor denies them, and
# a database-level deny would beat root's global allow on its own.
LOCK_CONFIG = {
    "databases": {
        DATA_DB: {
            "permissions": {
                action: {"id": "*"} for action in ("create-table", *ROW_ACTIONS)
            },
            "tables": {
                SYNCED: {
                    "permissions": {action: {"id": "*"} for action in LOCKED_ACTIONS}
                }
            },
        }
    }
}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest_asyncio.fixture
async def datasette(mock_google):
    datasette = await make_datasette(mock_google, config=copy.deepcopy(LOCK_CONFIG))
    datasette.root_enabled = True
    return datasette


@pytest.fixture
def idb(datasette):
    return InternalDB.for_datasette(datasette)


@pytest_asyncio.fixture
async def synced(datasette, idb, data_db, import_link, sa_credential):
    """A synced ``students`` table (created by its first scheduled run) plus
    a normal ``plain`` table in the same database."""
    info = await sa_credential("alice")
    created = await import_link(SYNCED, key="id", interval_minutes=10)
    link = await idb.update_link(created.id, credential_id=info.id)
    first = await run_link(datasette, link.id, trigger="scheduled")
    assert first.status == "success"
    await data_db.execute_write(f"create table {PLAIN} (id integer primary key, name)")
    return link


async def allowed(datasette, action, actor, table=SYNCED):
    return await datasette.allowed(
        action=action, resource=TableResource(DATA_DB, table), actor=actor
    )


@pytest.mark.asyncio
async def test_locked_actions_are_core_table_actions(datasette):
    """The ``← verify`` item: every name is a real table-level action in core
    (``datasette/default_actions.py``)."""
    for name in LOCKED_ACTIONS:
        assert datasette.actions[name].resource_class is TableResource, name
    assert len(LOCKED_ACTIONS) == 6


# ------------------------------------------------------------- the deny


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ROOT, BOB, ALICE, None])
async def test_every_actor_is_denied_every_locked_action(datasette, synced, actor):
    for action in sorted(LOCKED_ACTIONS):
        assert not await allowed(datasette, action, actor), action


@pytest.mark.asyncio
async def test_the_lock_is_scoped_to_the_synced_table(datasette, synced):
    # Root keeps every write action on the normal table next door...
    for action in LOCKED_ACTIONS:
        assert await allowed(datasette, action, ROOT, table=PLAIN), action
    assert await allowed(datasette, "insert-row", BOB, table=PLAIN)
    # Reading is unaffected.
    for actor in (ROOT, BOB, None):
        assert await allowed(datasette, "view-table", actor)


@pytest.mark.asyncio
async def test_the_deny_names_its_reason(datasette, synced):
    """The rule shows up in core's explanation (``/-/check``'s source)."""
    from datasette.utils.actions_sql import explain_permission_for_resource

    explanation = await explain_permission_for_resource(
        datasette=datasette,
        actor=ROOT,
        action="insert-row",
        parent=DATA_DB,
        child=SYNCED,
    )
    assert not explanation["allowed"]
    decisive = {r["reason"]: r["decisive"] for r in explanation["matched_rules"]}
    [ours] = [r for r in explanation["matched_rules"] if r["reason"] == REASON]
    assert (ours["scope"], ours["effect"], ours["decisive"]) == (
        "resource",
        "deny",
        True,
    )
    # It beats root's global allow and the table-level config allow.
    assert decisive["root user"] is False
    table_allow = f"config allow permissions for insert-row on {DATA_DB}/{SYNCED}"
    assert decisive[table_allow] is False


@pytest.mark.asyncio
async def test_table_names_match_case_insensitively(datasette, synced):
    # TableResource children compare NOCASE, as SQLite table names do.
    assert not await allowed(datasette, "insert-row", ROOT, table="STUDENTS")


# ------------------------------------------------------- JSON write API


def _row_path(pk=1):
    return f"/{DATA_DB}/{SYNCED}/{pk}"


# ``← verify``: the endpoint paths and bodies are from core docs/json_api.rst.
WRITE_REQUESTS = [
    (f"/{DATA_DB}/{SYNCED}/-/insert", {"row": {"id": 99, "name": "x"}}),
    (f"/{DATA_DB}/{SYNCED}/-/upsert", {"rows": [{"id": 1, "name": "x"}]}),
    (f"{_row_path()}/-/update", {"update": {"name": "x"}}),
    (f"{_row_path()}/-/delete", {}),
    (f"/{DATA_DB}/{SYNCED}/-/drop", {"confirm": True}),
    (
        f"/{DATA_DB}/{SYNCED}/-/alter",
        {"operations": [{"op": "add_column", "args": {"name": "x", "type": "text"}}]},
    ),
    (
        f"/{DATA_DB}/{SYNCED}/-/set-column-type",
        {"column": "name", "column_type": {"type": "email"}},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", WRITE_REQUESTS)
async def test_json_write_api_is_forbidden_even_for_root(
    datasette, synced, data_db, path, body
):
    before = (await data_db.execute(f"select * from {SYNCED}")).dicts()
    response = await datasette.client.post(
        path, json=body, actor=ROOT, headers=SAME_ORIGIN
    )
    assert response.status_code == 403, response.text
    assert (await data_db.execute(f"select * from {SYNCED}")).dicts() == before


@pytest.mark.asyncio
async def test_json_insert_into_a_normal_table_works(datasette, synced, data_db):
    response = await datasette.client.post(
        f"/{DATA_DB}/{PLAIN}/-/insert",
        json={"row": {"id": 1, "name": "x"}},
        actor=ROOT,
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 201, response.text


# -------------------------------------------------------------- write SQL


async def execute_write(datasette, sql, actor=ROOT):
    return await datasette.client.post(
        f"/{DATA_DB}/-/execute-write",
        json={"sql": sql},
        actor=actor,
        headers=SAME_ORIGIN,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        f"insert into {SYNCED} (id, name) values (99, 'x')",
        "INSERT INTO STUDENTS (id, name) VALUES (99, 'x')",
        f"update {SYNCED} set name = 'x'",
        f"delete from {SYNCED}",
        f"alter table {SYNCED} add column x text",
        f"create index students_name on {SYNCED} (name)",
        f"drop table {SYNCED}",
        f"insert into {SYNCED} (id, name) select id + 100, name from {PLAIN}",
    ],
)
async def test_write_sql_against_the_synced_table_is_forbidden(
    datasette, synced, data_db, sql
):
    count = (await data_db.execute(f"select count(*) from {SYNCED}")).single_value()
    response = await execute_write(datasette, sql)
    assert response.status_code == 403, response.text
    assert await data_db.table_exists(SYNCED)
    assert (await data_db.execute(f"select count(*) from {SYNCED}")).single_value() == (
        count
    )


@pytest.mark.asyncio
async def test_write_sql_against_a_normal_table_works(datasette, synced, data_db):
    response = await execute_write(
        datasette, f"insert into {PLAIN} (id, name) values (1, 'x')"
    )
    assert response.status_code == 200, response.text
    # Reading the synced table inside write SQL needs only view-table.
    response = await execute_write(
        datasette, f"insert into {PLAIN} (name) select name from {SYNCED}"
    )
    assert response.status_code == 200, response.text


# ------------------------------------------------------------ table page


EDIT_UI = (
    'data-table-action="insert-row"',
    'data-table-action="alter-table"',
    'data-row-action="edit"',
    'data-row-action="delete"',
)


@pytest.mark.asyncio
async def test_table_page_shows_no_edit_ui(datasette, synced):
    """``← verify``: core renders the insert button, the Alter table action
    and the per-row edit/delete buttons only when ``allowed()`` says so
    (``views/table.py`` ``_table_insert_ui`` / ``allowed_many``,
    ``default_table_actions.py``)."""
    plain = await datasette.client.post(
        f"/{DATA_DB}/{PLAIN}/-/insert",
        json={"row": {"id": 1, "name": "x"}},
        actor=ROOT,
        headers=SAME_ORIGIN,
    )
    assert plain.status_code == 201
    control = await datasette.client.get(f"/{DATA_DB}/{PLAIN}", actor=ROOT)
    for marker in EDIT_UI:
        assert marker in control.text, marker

    page = await datasette.client.get(f"/{DATA_DB}/{SYNCED}", actor=ROOT)
    assert page.status_code == 200
    assert "Alice Chen" in page.text  # still readable
    for marker in EDIT_UI:
        assert marker not in page.text, marker


# ------------------------------------------------------- our sync writes


@pytest.mark.asyncio
async def test_scheduled_sync_still_writes_under_the_lock(
    datasette, idb, synced, data_db, mock_google
):
    """Locked for root, bob's table allow, the JSON API and write SQL, yet a
    scheduled run as the owner (checked at database level, D35) writes."""
    assert not await allowed(datasette, "insert-row", ROOT)
    assert not await allowed(datasette, "insert-row", BOB)
    assert (await execute_write(datasette, f"delete from {SYNCED}")).status_code == 403
    for action in ROW_ACTIONS:
        assert await datasette.allowed(
            action=action, resource=DatabaseResource(DATA_DB), actor=ALICE
        )

    tab = mock_google.sheets.get("students").sheets[0]
    tab.write(1, 1, [["Alicia Chen"]])
    outcome = await run_link(datasette, synced.id, trigger="scheduled")
    assert (outcome.status, outcome.code) == ("success", None)
    assert outcome.result.changed == 1
    name = await data_db.execute(f"select name from {SYNCED} where id = 1")
    assert name.single_value() == "Alicia Chen"


# ----------------------------------------------- no stale window, unlink


@pytest.mark.asyncio
async def test_lock_follows_the_links_table_with_no_cache(datasette, idb, synced):
    # Paused: still locked (D32), the lock lasts until unlink.
    await idb.update_link(synced.id, enabled=False, status="paused")
    assert not await allowed(datasette, "insert-row", ROOT)

    # Unlink clears interval_minutes: the very next check is allowed.
    await idb.update_link(synced.id, interval_minutes=None)
    for action in LOCKED_ACTIONS:
        assert await allowed(datasette, action, ROOT), action
    assert await allowed(datasette, "insert-row", BOB)

    # Converting back to synced locks it again straight away.
    await idb.update_link(synced.id, interval_minutes=10)
    assert not await allowed(datasette, "insert-row", ROOT)

    # Deleting the link removes the lock (D19).
    await idb.delete_link(synced.id)
    assert await allowed(datasette, "insert-row", ROOT)


@pytest.mark.asyncio
async def test_after_unlink_root_can_insert_again(datasette, idb, synced, data_db):
    await idb.update_link(synced.id, interval_minutes=None)
    response = await datasette.client.post(
        f"/{DATA_DB}/{SYNCED}/-/insert",
        json={"row": {"id": 99, "name": "x"}},
        actor=ROOT,
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 201, response.text
    response = await execute_write(
        datasette, f"update {SYNCED} set name = 'y' where id = 99"
    )
    assert response.status_code == 200, response.text
    assert (
        await data_db.execute(f"select name from {SYNCED} where id = 99")
    ).single_value() == "y"


@pytest.mark.asyncio
async def test_one_shot_and_export_links_lock_nothing(
    datasette, idb, data_db, import_link
):
    await data_db.execute_write(f"create table {SYNCED} (id integer primary key)")
    await import_link(SYNCED, mode="append")
    await idb.create_link(
        direction="export",
        mode="replace",
        owner_id="alice",
        credential_id="unused",
        database_name=DATA_DB,
        table_name=SYNCED,
        source_kind="table",
        spreadsheet_id="sheet",
        sheet_gid=0,
        interval_minutes=10,
    )
    assert await allowed(datasette, "insert-row", ROOT)


# ------------------------------------------------------------ robustness


@pytest.mark.asyncio
async def test_no_rule_before_the_links_table_exists(mock_google):
    """Actions are registered before any startup hook runs, so another
    plugin's startup could check a locked action before ours creates the
    links table. Our rule must not hit "no such table" then; there's nothing
    to lock yet. Once the table exists the rule is always returned."""
    datasette = mock_google.datasette()
    internal = datasette.get_internal_database()
    assert not await internal.table_exists(LINKS)
    rule = synced_table_deny(datasette, "insert-row")
    assert rule is not None
    assert await rule() is None

    await datasette.invoke_startup()
    rule = synced_table_deny(datasette, "insert-row")
    assert rule is not None
    permission_sql = await rule()
    assert isinstance(permission_sql, PermissionSQL)
    # Core runs plugin rules against the internal DB, as here.
    rows = await internal.execute(permission_sql.sql, permission_sql.params)
    assert rows.rows == []


def test_other_actions_add_no_rule():
    datasette = object()
    for action in ("view-table", "execute-write-sql", "create-table", "drop-view"):
        assert synced_table_deny(datasette, action) is None  # type: ignore[arg-type]
