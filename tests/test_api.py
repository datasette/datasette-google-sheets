"""The JSON API (ticket 13): auth, owner/admin rules and 404 equalisation,
every POST /links validation, the first run, the link lifecycle and the
OpenAPI document. Against the vendored mock Google; never the network."""

import ast
import copy
import json
import logging
from pathlib import Path

import datasette_google_credentials
import pytest
import pytest_asyncio
from datasette import hookimpl
from datasette.plugins import pm
from fixtures_import import DATA_DB, STUDENTS_HEADERS, STUDENTS_TYPES, make_mapping
from fixtures_sheets import ALICE, BOB, make_datasette
from mock_google.keys import SA_OTHER, SA_TEST
from mock_google.oauth import (
    DEFAULT_USER,
    SCOPE_EMAIL,
    SCOPE_OPENID,
)

from datasette_google_sheets import service
from datasette_google_sheets.internal_db import InternalDB
from datasette_google_sheets.router import MAX_BODY_BYTES, router
from datasette_google_sheets.schedule import task_name

API = "/-/google-sheets/api"
ADMIN = {"id": "admin"}
WRITE_ACTIONS = ("create-table", "insert-row", "update-row", "delete-row")
CONFIG = {
    "permissions": {
        "google-sheets-schedule": {"id": ["alice", "admin"]},
        "google-sheets-admin": {"id": "admin"},
    },
    "databases": {
        DATA_DB: {
            "permissions": {
                **{action: {"id": "alice"} for action in WRITE_ACTIONS},
                "alter-table": {"id": "alice"},
            }
        }
    },
}
PRIVATE_URL = "https://docs.google.com/spreadsheets/d/private/edit"
CELL_VALUES = ["Alice Chen", "Bob Jones", "alice@school.edu"]
ROUTES_MODULE = Path(service.__file__).parent / "routes" / "api.py"
SERVICE_MODULE = Path(service.__file__)


# ----------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def datasette(mock_google, tmp_path):
    """A persistent internal DB, so scheduled links are allowed (D19)."""
    ds = await make_datasette(
        mock_google,
        config=copy.deepcopy(CONFIG),
        internal=str(tmp_path / "internal.db"),
    )
    yield ds
    ds.close()


@pytest.fixture
def idb(datasette):
    return InternalDB.for_datasette(datasette)


@pytest.fixture
def scheduler(datasette):
    return datasette._cron_scheduler


@pytest.fixture
def plugin():
    registered = []

    def register(obj):
        name = f"test-api-{len(registered)}"
        pm.register(obj, name=name)
        registered.append(name)

    yield register
    for name in registered:
        pm.unregister(name=name)


class NamedActors:
    @hookimpl
    def actors_from_ids(self, datasette, actor_ids):
        return {i: {"id": i, "name": f"{i.title()} Person"} for i in actor_ids}


async def get(datasette, path, actor=ALICE):
    return await datasette.client.get(API + path, actor=actor)


async def post(datasette, path, body=None, actor=ALICE, **kwargs):
    return await datasette.client.post(API + path, json=body, actor=actor, **kwargs)


def import_body(credential_id, *, table="students", mode="create", **fields):
    body = {
        "direction": "import",
        "mode": mode,
        "credential_id": credential_id,
        "database": DATA_DB,
        "table_name": table,
        "spreadsheet_id": "students",
        "gid": 0,
        "mapping": make_mapping(
            STUDENTS_HEADERS, types=STUDENTS_TYPES, key="id"
        ).model_dump(),
    }
    body.update(fields)
    return body


def export_body(credential_id, **fields):
    body = {
        "direction": "export",
        "mode": "replace",
        "credential_id": credential_id,
        "database": DATA_DB,
        "source_kind": "table",
        "table_name": "items",
        "spreadsheet_id": "students",
        "gid": 1001,
    }
    body.update(fields)
    return body


@pytest.fixture
def alice_sa(sa_credential):
    async def make(key="test"):
        return (await sa_credential("alice", key=key)).id

    return make


@pytest.fixture
def create(datasette, alice_sa, data_db):
    """``await create(**import_body fields)``: POST /links as alice with her
    service account; returns the response JSON (asserting 200)."""

    async def make(*, actor=ALICE, credential_id=None, **fields):
        credential_id = credential_id or await alice_sa()
        response = await post(
            datasette, "/links", import_body(credential_id, **fields), actor=actor
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return make


async def items_table(data_db):
    def seed(conn):
        conn.execute("create table items (n integer, s text)")
        conn.executemany("insert into items values (?, ?)", [(1, "a"), (2, "b")])

    await data_db.execute_write_fn(seed)


async def columns_of(data_db, table):
    return [row[1] for row in (await data_db.execute(f"pragma table_info({table})"))]


# ------------------------------------------------------------------- guards


@pytest.mark.parametrize("module", [ROUTES_MODULE, SERVICE_MODULE])
def test_uses_only_the_public_google_credentials_api(module):
    tree = ast.parse(module.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("datasette_google_credentials"):
                assert node.module == "datasette_google_credentials", node.module
                imported |= {alias.name for alias in node.names}
    assert imported <= set(datasette_google_credentials.__all__)


def test_routes_module_has_no_future_annotations():
    tree = ast.parse(ROUTES_MODULE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            pytest.fail("routes/api.py must not use from __future__ imports")


ROUTES = [
    ("get", "/status"),
    ("get", "/credentials"),
    ("post", "/inspect"),
    ("post", "/preview"),
    ("get", "/links"),
    ("post", "/links"),
    ("get", "/links/{link_id}"),
    ("get", "/links/{link_id}/runs"),
    ("post", "/links/{link_id}/run"),
    ("post", "/links/{link_id}/pause"),
    ("post", "/links/{link_id}/resume"),
    ("post", "/links/{link_id}/settings"),
    ("post", "/links/{link_id}/mapping"),
    ("post", "/links/{link_id}/convert-to-synced"),
    ("post", "/links/{link_id}/unlink"),
    ("post", "/links/{link_id}/delete"),
]


def test_openapi_documents_every_route_deterministically():
    document = router.openapi_document_json()
    operations = {
        (method, path.removeprefix("/-/google-sheets/api"))
        for path, methods in document["paths"].items()
        for method in methods
    }
    assert operations == set(ROUTES)
    for methods in document["paths"].values():
        for operation in methods.values():
            assert "content" in operation["responses"]["200"]
    assert json.dumps(document, sort_keys=True) == json.dumps(
        router.openapi_document_json(), sort_keys=True
    )


# ----------------------------------------------------------------- auth


# Minimal valid bodies, so the router's body parsing isn't what refuses.
BODIES = {
    "/inspect": {"credential_id": "x", "url": "x" * 20},
    "/preview": {"credential_id": "x", "spreadsheet_id": "x", "gid": 0},
    "/links": import_body("x"),
    "/links/{link_id}/run": {},
    "/links/{link_id}/resume": {},
    "/links/{link_id}/settings": {},
    "/links/{link_id}/mapping": {
        "mapping": make_mapping(STUDENTS_HEADERS).model_dump()
    },
    "/links/{link_id}/convert-to-synced": {"interval_minutes": 10},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_needs_a_signed_in_actor(datasette, method, path):
    url = API + path.replace("{link_id}", "01ANY")
    if method == "get":
        response = await datasette.client.get(url)
    else:
        response = await datasette.client.post(url, json=BODIES.get(path))
    assert response.status_code == 403
    assert response.json() == {
        "ok": False,
        "error": "Sign in to use Google Sheets.",
        "code": "not_signed_in",
    }


@pytest.mark.asyncio
async def test_methods_are_enforced(datasette, create):
    link = (await create())["link"]
    # A GET must never reach a POST-only mutation (core's CSRF check covers
    # unsafe methods only).
    response = await get(datasette, f"/links/{link['id']}/delete")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert response.headers["allow"] == "POST"
    assert (await get(datasette, f"/links/{link['id']}")).status_code == 200
    assert (await post(datasette, "/status")).status_code == 405


@pytest.mark.asyncio
async def test_cross_site_posts_are_refused_by_core(datasette, create):
    link = (await create())["link"]
    response = await post(
        datasette,
        f"/links/{link['id']}/pause",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    response = await post(
        datasette,
        f"/links/{link['id']}/pause",
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_body_cap_applies(datasette):
    body = {"credential_id": "x", "url": "x" * (MAX_BODY_BYTES + 1)}
    response = await post(datasette, "/inspect", body)
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


# ----------------------------------------------------------------- status


@pytest.mark.asyncio
async def test_status_flags(datasette, monkeypatch):
    alice = (await get(datasette, "/status")).json()
    assert alice == {
        "internal_db_persistent": True,
        "oauth_configured": None,
        "can_schedule": True,
        "is_admin": False,
        "min_interval_minutes": 5,
        "default_interval_minutes": 10,
    }
    bob = (await get(datasette, "/status", actor=BOB)).json()
    assert (bob["can_schedule"], bob["is_admin"]) == (False, False)
    admin = (await get(datasette, "/status", actor=ADMIN)).json()
    assert (admin["can_schedule"], admin["is_admin"]) == (True, True)

    # D21: once google-credentials exports oauth_configured(datasette), it's used.
    calls = []

    def oauth_configured(ds):
        calls.append(ds)
        return False

    monkeypatch.setattr(
        datasette_google_credentials,
        "oauth_configured",
        oauth_configured,
        raising=False,
    )
    assert (await get(datasette, "/status")).json()["oauth_configured"] is False
    assert calls == [datasette]


@pytest.mark.asyncio
async def test_status_flags_a_temporary_internal_db(mock_google):
    temp = await make_datasette(
        mock_google,
        config=copy.deepcopy(CONFIG),
        sheets_config={"min_interval_minutes": 15, "default_interval_minutes": 30},
    )
    status = (await get(temp, "/status")).json()
    assert status["internal_db_persistent"] is False
    assert (status["min_interval_minutes"], status["default_interval_minutes"]) == (
        15,
        30,
    )


# ------------------------------------------------------------ credentials


@pytest.mark.asyncio
async def test_credentials_by_direction(datasette, sa_credential, oauth_credential):
    sa = await sa_credential("alice")
    oauth = await oauth_credential("alice")
    await sa_credential("bob", key="other")
    # Partial consent: no Sheets scope, so it's offered for neither direction.
    await oauth_credential("bob", granted_scopes={SCOPE_OPENID, SCOPE_EMAIL})
    for direction in ("import", "export"):
        bobs = await get(datasette, f"/credentials?direction={direction}", actor=BOB)
        assert [c["type"] for c in bobs.json()["credentials"]] == ["service_account"]

    imports = (await get(datasette, "/credentials?direction=import")).json()
    assert [c["id"] for c in imports["credentials"]] == [oauth.id, sa.id]
    assert set(imports["credentials"][1]) == {
        "id",
        "type",
        "label",
        "google_email",
        "status",
        "status_detail",
    }
    assert imports["credentials"][1]["google_email"] == SA_TEST
    exports = (await get(datasette, "/credentials?direction=export")).json()
    assert [c["id"] for c in exports["credentials"]] == [oauth.id, sa.id]

    assert imports["connect_url"] == (
        "/-/google-credentials/connect?return_to=%2F-%2Fgoogle-sheets"
    )
    back = await get(
        datasette,
        "/credentials?direction=import&return_to=/-/google-sheets/import/data",
    )
    assert back.json()["connect_url"].endswith(
        "return_to=%2F-%2Fgoogle-sheets%2Fimport%2Fdata"
    )
    offsite = await get(
        datasette, "/credentials?direction=import&return_to=//evil.example"
    )
    assert offsite.json()["connect_url"].endswith("return_to=%2F-%2Fgoogle-sheets")

    for query in ("", "?direction=sideways"):
        response = await get(datasette, "/credentials" + query)
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_direction"


# ---------------------------------------------------------- inspect/preview


@pytest.mark.asyncio
async def test_inspect_lists_tabs_and_the_urls_gid(datasette, alice_sa):
    credential_id = await alice_sa()
    response = await post(
        datasette,
        "/inspect",
        {
            "credential_id": credential_id,
            "url": "https://docs.google.com/spreadsheets/d/students/edit#gid=1001",
        },
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["spreadsheet_id"] == "students"
    assert data["spreadsheet_title"] == "Students"
    assert data["gid"] == 1001
    assert [(t["gid"], t["title"]) for t in data["tabs"]] == [
        (0, "students"),
        (1001, "assignments"),
    ]
    assert (
        data["tabs"][0]["cells"] == data["tabs"][0]["rows"] * data["tabs"][0]["columns"]
    )

    bad = await post(
        datasette, "/inspect", {"credential_id": credential_id, "url": "nope"}
    )
    assert (bad.status_code, bad.json()["code"]) == (400, "invalid_url")


@pytest.mark.asyncio
async def test_inspect_not_shared_says_whom_to_share_with(datasette, alice_sa):
    credential_id = await alice_sa()
    response = await post(
        datasette,
        "/inspect",
        {"credential_id": credential_id, "url": PRIVATE_URL, "direction": "export"},
    )
    data = response.json()
    assert response.status_code in (403, 404)
    assert data["ok"] is False
    assert data["code"] in ("not_shared", "not_found")
    assert data["share_with"] == SA_TEST
    assert f"share the sheet with {SA_TEST} as Editor" in data["error"]


@pytest.mark.asyncio
async def test_inspect_with_someone_elses_credential_is_googles_404(
    datasette, sa_credential
):
    bobs = await sa_credential("bob")
    response = await post(
        datasette, "/inspect", {"credential_id": bobs.id, "url": "students" * 2}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_preview_suggests_types_and_key(datasette, alice_sa):
    credential_id = await alice_sa()
    response = await post(
        datasette,
        "/preview",
        {"credential_id": credential_id, "spreadsheet_id": "students", "gid": 0},
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["too_large"] is False
    assert data["headers"] == STUDENTS_HEADERS
    assert data["rows"][0] == [1, "Alice Chen", 10, "alice@school.edu"]
    assert data["total_rows"] == 5
    assert data["types"] == {
        "id": "INTEGER",
        "name": "TEXT",
        "grade_level": "INTEGER",
        "email": "TEXT",
    }
    assert data["key"] == "id"
    assert data["tab"]["title"] == "students"
    assert data["max_cells"] == 1_000_000


@pytest.mark.asyncio
async def test_preview_flags_a_tab_over_the_cap(datasette, alice_sa, mock_google):
    credential_id = await alice_sa()
    mock_google.sheets.add(
        "big", {"t": [["a"], [1]]}, row_count=2000, column_count=1000
    )
    response = await post(
        datasette,
        "/preview",
        {"credential_id": credential_id, "spreadsheet_id": "big", "gid": 0},
    )
    data = response.json()
    assert response.status_code == 200
    assert data["too_large"] is True
    assert data["cells"] == 2_000_000
    assert "max_import_cells" in data["message"]
    assert data["rows"] == []
    # Nothing but the metadata was fetched (D20).
    assert not mock_google.calls("/v4/spreadsheets/big/values")


@pytest.mark.asyncio
async def test_preview_errors(datasette, alice_sa):
    credential_id = await alice_sa()
    private = await post(
        datasette,
        "/preview",
        {"credential_id": credential_id, "spreadsheet_id": "private", "gid": 0},
    )
    assert private.json()["share_with"] == SA_TEST
    assert f"{SA_TEST} as Viewer" in private.json()["error"]
    missing_tab = await post(
        datasette,
        "/preview",
        {"credential_id": credential_id, "spreadsheet_id": "students", "gid": 99},
    )
    assert (missing_tab.status_code, missing_tab.json()["code"]) == (404, "tab_missing")


# ------------------------------------------------------------ create links


@pytest.mark.asyncio
async def test_create_import_runs_it_once(datasette, create, data_db, idb):
    data = await create()
    run, link = data["run"], data["link"]
    assert run["status"] == "success"
    assert (run["rows_read"], run["added"]) == (5, 5)
    assert run["link_status"] == "ok"
    assert link["owner_id"] == "alice"
    assert link["created_table"] is True
    assert (link["scheduled"], link["synced"]) == (False, False)
    assert (link["can_operate"], link["can_manage"]) == (True, True)
    assert link["spreadsheet_url"].endswith("/d/students/edit#gid=0")
    assert "last_hash" not in link and "created_schema" not in link
    rows = await data_db.execute("select count(*) from students")
    assert rows.first()[0] == 5
    runs = await idb.list_runs(link["id"])
    assert [(r.trigger, r.actor_id) for r in runs] == [("manual", "alice")]


@pytest.mark.asyncio
async def test_a_failed_first_run_keeps_the_link(datasette, create, alice_sa):
    data = await create(credential_id=await alice_sa(key="other"))
    assert data["run"]["status"] == "error"
    assert data["run"]["code"] in ("not_shared", "not_found")
    assert data["run"]["share_with"] == SA_OTHER
    assert data["link"]["status"] == "paused"
    response = await get(datasette, f"/links/{data['link']['id']}")
    assert response.status_code == 200
    assert response.json()["status_code"] == data["run"]["code"]


@pytest.mark.asyncio
async def test_create_a_synced_table(datasette, create, scheduler):
    data = await create(interval_minutes=10)
    link = data["link"]
    assert (link["scheduled"], link["synced"], link["interval_minutes"]) == (
        True,
        True,
        10,
    )
    task = await scheduler.internal_db.get_task(task_name(link["id"]))
    assert task is not None and task.enabled


@pytest.mark.asyncio
async def test_create_an_export(datasette, alice_sa, data_db, mock_google):
    await items_table(data_db)
    response = await post(datasette, "/links", export_body(await alice_sa()))
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["run"]["status"] == "success"
    assert data["run"]["rows_written"] == 2
    assert data["run"]["spreadsheet_url"].endswith("/d/students/edit#gid=1001")
    assert data["link"]["direction"] == "export"


VALIDATIONS = [
    # (fields, status, code)
    ({"mode": "new"}, 400, "invalid_mode"),
    ({"mode": "append", "interval_minutes": 10}, 400, "not_schedulable"),
    ({"mode": "upsert", "interval_minutes": 10}, 400, "not_schedulable"),
    ({"mode": "replace", "interval_minutes": 10}, 400, "not_schedulable"),
    ({"interval_minutes": 4}, 400, "interval_too_short"),
    ({"database": "nope"}, 404, "database_not_found"),
    ({"mode": "append"}, 404, "table_missing"),
    ({"mode": "upsert", "table_name": "items"}, 400, "table_changed"),
    (
        {"mapping": make_mapping(STUDENTS_HEADERS, key="nope").model_dump()},
        400,
        "invalid_mapping",
    ),
    (
        {"mapping": make_mapping(STUDENTS_HEADERS, rename={"name": "id"}).model_dump()},
        400,
        "invalid_mapping",
    ),
    ({"source_kind": "table"}, 400, "invalid_link"),
    ({"gid": None}, 400, "invalid_link"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("fields", "status", "code"), VALIDATIONS)
async def test_create_import_validation(
    datasette, alice_sa, data_db, idb, fields, status, code
):
    await items_table(data_db)
    response = await post(datasette, "/links", import_body(await alice_sa(), **fields))
    assert response.status_code == status, response.json()
    assert response.json()["code"] == code
    assert response.json()["ok"] is False
    assert await idb.list_links() == []


@pytest.mark.asyncio
async def test_create_upsert_needs_a_key(datasette, alice_sa, data_db):
    await items_table(data_db)
    mapping = make_mapping(["n", "s"], types={"n": "INTEGER"}).model_dump()
    body = import_body(
        await alice_sa(), mode="upsert", table_name="items", mapping=mapping
    )
    response = await post(datasette, "/links", body)
    assert (response.status_code, response.json()["code"]) == (400, "key_required")


@pytest.mark.asyncio
async def test_create_refuses_an_existing_table(datasette, create, alice_sa):
    await create()
    response = await post(datasette, "/links", import_body(await alice_sa()))
    assert (response.status_code, response.json()["code"]) == (409, "table_exists")


@pytest.mark.asyncio
async def test_create_needs_the_actors_write_permissions(
    datasette, sa_credential, data_db, idb
):
    bobs = await sa_credential("bob")
    response = await post(datasette, "/links", import_body(bobs.id), actor=BOB)
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert response.json()["actions"] == ["create-table"]
    assert await idb.list_links() == []


@pytest.mark.asyncio
async def test_scheduling_needs_the_schedule_permission(
    datasette, sa_credential, data_db, monkeypatch
):
    bobs = await sa_credential("bob")
    response = await post(
        datasette, "/links", import_body(bobs.id, interval_minutes=10), actor=BOB
    )
    assert (response.status_code, response.json()["code"]) == (403, "forbidden")


@pytest.mark.asyncio
async def test_scheduling_needs_a_persistent_internal_db(mock_google, sa_credential):
    temp = await make_datasette(mock_google, config=copy.deepcopy(CONFIG))
    temp.add_memory_database("api_temp_data", name=DATA_DB)
    response = await post(temp, "/links", import_body("unused", interval_minutes=10))
    assert response.status_code == 409
    assert response.json()["code"] == "internal_db_not_persistent"


@pytest.mark.asyncio
async def test_create_needs_a_usable_credential(
    datasette, sa_credential, oauth_credential, data_db, idb
):
    bobs = await sa_credential("bob")
    response = await post(datasette, "/links", import_body(bobs.id))
    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    # A grant without the Sheets scope (partial consent).
    limited = await oauth_credential(
        "alice", granted_scopes={SCOPE_OPENID, SCOPE_EMAIL}
    )
    for body in (import_body(limited.id), export_body(limited.id)):
        response = await post(datasette, "/links", body)
        assert (response.status_code, response.json()["code"]) == (
            403,
            "missing_scopes",
        )
        assert response.json()["reconnect_url"]
    assert await idb.list_links() == []


@pytest.mark.asyncio
async def test_create_export_validation(datasette, alice_sa, data_db, idb):
    await items_table(data_db)
    credential_id = await alice_sa()
    cases = [
        ({"mode": "upsert"}, 400, "invalid_mode"),
        ({"mode": "append", "interval_minutes": 10}, 400, "not_schedulable"),
        (
            {
                "mode": "new",
                "spreadsheet_id": None,
                "gid": None,
                "interval_minutes": 10,
            },
            400,
            "not_schedulable",
        ),
        ({"source_kind": None}, 400, "invalid_link"),
        ({"source_kind": "sql", "table_name": None}, 400, "invalid_link"),
        ({"mode": "new"}, 400, "invalid_link"),
    ]
    for fields, status, code in cases:
        response = await post(datasette, "/links", export_body(credential_id, **fields))
        assert (response.status_code, response.json()["code"]) == (status, code), fields
    # D14: a service account can't create a spreadsheet.
    response = await post(
        datasette,
        "/links",
        export_body(credential_id, mode="new", spreadsheet_id=None, gid=None),
    )
    assert (response.status_code, response.json()["code"]) == (400, "sa_cannot_create")
    assert response.json()["share_with"] == SA_TEST
    assert await idb.list_links() == []


@pytest.mark.asyncio
async def test_create_export_needs_read_permission(
    mock_google, tmp_path, sa_credential
):
    config = copy.deepcopy(CONFIG)
    config["databases"][DATA_DB]["tables"] = {"items": {"allow": {"id": "bob"}}}
    ds = await make_datasette(mock_google, config=config)
    db = ds.add_memory_database("api_read_perm", name=DATA_DB)
    await items_table(db)
    response = await ds.client.post(
        API + "/links", json=export_body("unused"), actor=ALICE
    )
    assert response.status_code == 403
    assert response.json()["actions"] == ["view-table"]


@pytest.mark.asyncio
async def test_create_refuses_an_export_tab_already_linked(
    datasette, alice_sa, data_db
):
    await items_table(data_db)
    credential_id = await alice_sa()
    first = await post(datasette, "/links", export_body(credential_id))
    assert first.status_code == 200
    second = await post(datasette, "/links", export_body(credential_id))
    assert second.status_code == 409
    assert second.json()["code"] == "export_tab_taken"
    assert "existing_id" not in second.json()


# ----------------------------------------------------- visibility and roles


@pytest.mark.asyncio
async def test_others_links_are_404_like_unknown_ones(datasette, create):
    link_id = (await create())["link"]["id"]
    unknown = await get(datasette, "/links/01UNKNOWN", actor=BOB)
    assert unknown.status_code == 404
    requests = [
        ("get", f"/links/{link_id}", None),
        ("get", f"/links/{link_id}/runs", None),
        ("post", f"/links/{link_id}/run", {}),
        ("post", f"/links/{link_id}/pause", None),
        ("post", f"/links/{link_id}/resume", {}),
        ("post", f"/links/{link_id}/settings", {}),
        ("post", f"/links/{link_id}/mapping", BODIES["/links/{link_id}/mapping"]),
        ("post", f"/links/{link_id}/convert-to-synced", {"interval_minutes": 10}),
        ("post", f"/links/{link_id}/unlink", None),
        ("post", f"/links/{link_id}/delete", None),
    ]
    for method, path, body in requests:
        if method == "get":
            response = await get(datasette, path, actor=BOB)
        else:
            response = await post(datasette, path, body, actor=BOB)
        assert response.status_code == 404, path
        assert response.json() == unknown.json(), path
    assert (await get(datasette, "/links", actor=BOB)).json() == {"links": []}
    other = await get(datasette, "/links?owner=alice", actor=BOB)
    assert (other.status_code, other.json()["code"]) == (403, "forbidden")
    # Still there, untouched.
    assert (await get(datasette, f"/links/{link_id}")).json()["status"] == "ok"


@pytest.mark.asyncio
async def test_admins_see_every_link_with_owner_names(datasette, create, plugin):
    plugin(NamedActors())
    link_id = (await create())["link"]["id"]
    mine = (await get(datasette, "/links", actor=ADMIN)).json()
    assert mine == {"links": []}
    everyone = (await get(datasette, "/links?owner=*", actor=ADMIN)).json()
    assert [link["id"] for link in everyone["links"]] == [link_id]
    listed = everyone["links"][0]
    assert listed["owner_name"] == "Alice Person"
    assert (listed["can_operate"], listed["can_manage"]) == (False, True)
    by_owner = (await get(datasette, "/links?owner=alice", actor=ADMIN)).json()
    assert [link["id"] for link in by_owner["links"]] == [link_id]
    detail = (await get(datasette, f"/links/{link_id}", actor=ADMIN)).json()
    assert detail["owner_name"] == "Alice Person"
    # Non-admins get no names, even for their own links.
    assert (await get(datasette, f"/links/{link_id}")).json()["owner_name"] is None


@pytest.mark.asyncio
async def test_admins_manage_but_never_operate(datasette, create, idb, data_db):
    link_id = (await create(interval_minutes=10))["link"]["id"]
    refused = [
        (f"/links/{link_id}/run", {}),
        (f"/links/{link_id}/settings", {"interval_minutes": 20}),
        (f"/links/{link_id}/mapping", BODIES["/links/{link_id}/mapping"]),
        (f"/links/{link_id}/convert-to-synced", {"interval_minutes": 10}),
    ]
    for path, body in refused:
        response = await post(datasette, path, body, actor=ADMIN)
        assert response.status_code == 403, path
        assert response.json()["code"] == "forbidden"
    assert len(await idb.list_runs(link_id)) == 1

    paused = await post(datasette, f"/links/{link_id}/pause", actor=ADMIN)
    assert paused.status_code == 200
    assert paused.json()["status_detail"] == "Paused by an administrator."
    resumed = await post(datasette, f"/links/{link_id}/resume", {}, actor=ADMIN)
    assert resumed.json()["status"] == "ok"
    unlinked = await post(datasette, f"/links/{link_id}/unlink", actor=ADMIN)
    assert unlinked.json()["synced"] is False
    deleted = await post(datasette, f"/links/{link_id}/delete", actor=ADMIN)
    assert deleted.json() == {"id": link_id, "deleted": True}
    assert await idb.get_link(link_id) is None
    assert (await data_db.execute("select count(*) from students")).first()[0] == 5


@pytest.mark.asyncio
async def test_list_filters(datasette, create, alice_sa, data_db):
    synced = (await create(interval_minutes=10))["link"]["id"]
    await items_table(data_db)
    exported = (await post(datasette, "/links", export_body(await alice_sa()))).json()
    export_id = exported["link"]["id"]

    async def ids(query):
        response = await get(datasette, "/links" + query)
        assert response.status_code == 200, response.json()
        return [link["id"] for link in response.json()["links"]]

    assert await ids("") == [export_id, synced]
    assert await ids("?direction=import") == [synced]
    assert await ids("?scheduled=1") == [synced]
    assert await ids("?scheduled=0") == [export_id]
    assert await ids("?status=paused") == []
    for query, code in [
        ("?direction=x", "invalid_direction"),
        ("?status=x", "invalid_status"),
        ("?scheduled=maybe", "invalid_scheduled"),
    ]:
        response = await get(datasette, "/links" + query)
        assert (response.status_code, response.json()["code"]) == (400, code)


# ------------------------------------------------------------- operations


@pytest.mark.asyncio
async def test_run_now_and_history(datasette, create, mock_google):
    link_id = (await create())["link"]["id"]
    again = await post(datasette, f"/links/{link_id}/run", {})
    assert again.status_code == 200
    assert again.json()["run"]["status"] == "no_change"
    forced = await post(datasette, f"/links/{link_id}/run", {"force": True})
    assert forced.json()["run"]["status"] == "success"

    runs = (await get(datasette, f"/links/{link_id}/runs")).json()["runs"]
    assert [r["status"] for r in runs] == ["success", "no_change", "success"]
    one = (await get(datasette, f"/links/{link_id}/runs?limit=1")).json()["runs"]
    assert len(one) == 1
    for limit in ("0", "101", "x"):
        response = await get(datasette, f"/links/{link_id}/runs?limit={limit}")
        assert (response.status_code, response.json()["code"]) == (
            400,
            "invalid_limit",
        )

    await post(datasette, f"/links/{link_id}/pause")
    paused = await post(datasette, f"/links/{link_id}/run", {})
    assert (paused.status_code, paused.json()["code"]) == (409, "paused")


@pytest.mark.asyncio
async def test_pause_and_resume(datasette, create, idb, scheduler):
    link_id = (await create(interval_minutes=10))["link"]["id"]
    await idb.update_link(link_id, consecutive_failures=3, status="error")
    paused = (await post(datasette, f"/links/{link_id}/pause")).json()
    assert (paused["status"], paused["enabled"]) == ("paused", False)
    assert paused["status_code"] == "paused_manually"
    assert paused["status_detail"] == "Paused by its owner."
    assert not (await scheduler.internal_db.get_task(task_name(link_id))).enabled

    resumed = (await post(datasette, f"/links/{link_id}/resume", {})).json()
    assert (resumed["status"], resumed["enabled"]) == ("ok", True)
    assert resumed["status_code"] is None
    assert resumed["consecutive_failures"] == 0
    assert (await scheduler.internal_db.get_task(task_name(link_id))).enabled


@pytest.mark.asyncio
async def test_pausing_keeps_an_auto_pause_reason(datasette, create, idb):
    link_id = (await create())["link"]["id"]
    await idb.update_link(
        link_id, status="paused", enabled=False, status_code="table_missing"
    )
    paused = (await post(datasette, f"/links/{link_id}/pause")).json()
    assert paused["status_code"] == "table_missing"


def roster(extra_column=False):
    header = ["id", "name"] + (["team"] if extra_column else [])
    rows = [[1, "Ann"], [2, "Ben"]]
    if extra_column:
        rows = [row + ["red"] for row in rows]
    return {"people": [header, *rows]}


@pytest.mark.asyncio
async def test_mapping_update_adds_columns_and_resumes(
    datasette, create, mock_google, data_db, idb, scheduler
):
    mock_google.sheets.add("roster", roster())
    mapping = make_mapping(["id", "name"], types={"id": "INTEGER"}, key="id")
    data = await create(
        table="people",
        spreadsheet_id="roster",
        mapping=mapping.model_dump(),
        interval_minutes=10,
    )
    link_id = data["link"]["id"]
    assert data["run"]["status"] == "success"

    mock_google.sheets.add("roster", roster(extra_column=True))
    failed = (await post(datasette, f"/links/{link_id}/run", {"force": True})).json()
    assert failed["run"]["code"] == "headers_changed"
    assert failed["link"]["status"] == "paused"
    assert failed["link"]["status_data"]["added"] == ["team"]

    refused = await post(datasette, f"/links/{link_id}/resume", {})
    assert (refused.status_code, refused.json()["code"]) == (
        409,
        "mapping_update_required",
    )

    new = make_mapping(["id", "name", "team"], types={"id": "INTEGER"}, key="id")
    updated = await post(
        datasette, f"/links/{link_id}/mapping", {"mapping": new.model_dump()}
    )
    assert updated.status_code == 200, updated.json()
    link = updated.json()
    assert (link["status"], link["enabled"], link["status_code"]) == ("ok", True, None)
    assert link["mapping"]["source_headers"] == ["id", "name", "team"]
    assert await columns_of(data_db, "people") == ["id", "name", "team"]
    assert (await scheduler.internal_db.get_task(task_name(link_id))).enabled

    ran = (await post(datasette, f"/links/{link_id}/run", {})).json()
    assert ran["run"]["status"] == "success"
    rows = await data_db.execute("select team from people order by id")
    assert [row[0] for row in rows] == ["red", "red"]

    # Only after a header (or table) change.
    again = await post(
        datasette, f"/links/{link_id}/mapping", {"mapping": new.model_dump()}
    )
    assert (again.status_code, again.json()["code"]) == (409, "mapping_not_needed")


@pytest.mark.asyncio
async def test_mapping_update_needs_alter_table(mock_google, tmp_path, sa_credential):
    config = copy.deepcopy(CONFIG)
    del config["databases"][DATA_DB]["permissions"]["alter-table"]
    ds = await make_datasette(mock_google, config=config)
    ds.add_memory_database("api_alter_perm", name=DATA_DB)
    link = await InternalDB.for_datasette(ds).create_link(
        direction="import",
        mode="create",
        owner_id="alice",
        credential_id="unused",
        database_name=DATA_DB,
        table_name="students",
        spreadsheet_id="students",
        sheet_gid=0,
        mapping=make_mapping(STUDENTS_HEADERS),
    )
    db = ds.get_database(DATA_DB)
    await db.execute_write("create table students (id, name, grade_level, email)")
    await InternalDB.for_datasette(ds).update_link(
        link.id, created_table=True, status="paused", status_code="headers_changed"
    )
    new = make_mapping([*STUDENTS_HEADERS, "extra"])
    response = await ds.client.post(
        f"{API}/links/{link.id}/mapping",
        json={"mapping": new.model_dump()},
        actor=ALICE,
    )
    assert response.status_code == 403
    assert response.json()["actions"] == ["alter-table"]
    assert await columns_of(db, "students") == STUDENTS_HEADERS


@pytest.mark.asyncio
async def test_convert_to_synced(datasette, create, scheduler, idb):
    link_id = (await create())["link"]["id"]
    converted = await post(
        datasette, f"/links/{link_id}/convert-to-synced", {"interval_minutes": 15}
    )
    assert converted.status_code == 200, converted.json()
    assert (converted.json()["synced"], converted.json()["interval_minutes"]) == (
        True,
        15,
    )
    assert await scheduler.internal_db.get_task(task_name(link_id)) is not None
    already = await post(
        datasette, f"/links/{link_id}/convert-to-synced", {"interval_minutes": 15}
    )
    assert (already.status_code, already.json()["code"]) == (409, "already_synced")


@pytest.mark.asyncio
async def test_convert_to_synced_is_refused_after_an_alter(datasette, create, data_db):
    link_id = (await create())["link"]["id"]
    await data_db.execute_write("alter table students add column note text")
    response = await post(
        datasette, f"/links/{link_id}/convert-to-synced", {"interval_minutes": 15}
    )
    assert (response.status_code, response.json()["code"]) == (409, "schema_changed")
    short = await post(
        datasette, f"/links/{link_id}/convert-to-synced", {"interval_minutes": 1}
    )
    assert short.json()["code"] == "interval_too_short"


@pytest.mark.asyncio
async def test_convert_to_synced_needs_a_created_table(
    datasette, create, alice_sa, data_db, mock_google
):
    await items_table(data_db)
    mapping = make_mapping(["n", "s"], types={"n": "INTEGER"})
    mock_google.sheets.add("items", {"items": [["n", "s"], [3, "c"]]})
    appended = await create(
        mode="append",
        table="items",
        spreadsheet_id="items",
        mapping=mapping.model_dump(),
    )
    response = await post(
        datasette,
        f"/links/{appended['link']['id']}/convert-to-synced",
        {"interval_minutes": 15},
    )
    assert (response.status_code, response.json()["code"]) == (409, "not_convertible")


@pytest.mark.asyncio
async def test_unlink_lifts_the_lock_and_delete_keeps_the_table(
    datasette, create, idb, scheduler, data_db
):
    link_id = (await create(interval_minutes=10))["link"]["id"]
    assert (DATA_DB, "students") in await idb.synced_tables()

    unlinked = await post(datasette, f"/links/{link_id}/unlink")
    assert unlinked.status_code == 200
    assert (unlinked.json()["synced"], unlinked.json()["interval_minutes"]) == (
        False,
        None,
    )
    # Ticket 11's deny reads synced_tables(): the lock is gone.
    assert (DATA_DB, "students") not in await idb.synced_tables()
    assert await scheduler.internal_db.get_task(task_name(link_id)) is None
    again = await post(datasette, f"/links/{link_id}/unlink")
    assert (again.status_code, again.json()["code"]) == (409, "not_scheduled")

    deleted = await post(datasette, f"/links/{link_id}/delete")
    assert deleted.json() == {"id": link_id, "deleted": True}
    assert (await get(datasette, f"/links/{link_id}")).status_code == 404
    assert await idb.list_runs(link_id) == []
    assert (await data_db.execute("select count(*) from students")).first()[0] == 5


@pytest.mark.asyncio
async def test_delete_removes_the_cron_task(datasette, create, scheduler):
    link_id = (await create(interval_minutes=10))["link"]["id"]
    await post(datasette, f"/links/{link_id}/delete")
    assert await scheduler.internal_db.get_task(task_name(link_id)) is None


@pytest.mark.asyncio
async def test_resume_can_recreate_a_missing_table(datasette, create, data_db):
    link_id = (await create(interval_minutes=10))["link"]["id"]
    await data_db.execute_write("drop table students")
    failed = (await post(datasette, f"/links/{link_id}/run", {"force": True})).json()
    assert failed["link"]["status_code"] == "table_missing"

    resumed = await post(datasette, f"/links/{link_id}/resume", {"recreate": True})
    assert resumed.status_code == 200
    assert resumed.json()["created_table"] is False
    ran = (await post(datasette, f"/links/{link_id}/run", {})).json()
    assert ran["run"]["status"] == "success"
    assert ran["link"]["created_table"] is True
    assert (await data_db.execute("select count(*) from students")).first()[0] == 5

    refused = await post(datasette, f"/links/{link_id}/resume", {"recreate": True})
    assert (refused.status_code, refused.json()["code"]) == (409, "cannot_recreate")


@pytest.mark.asyncio
async def test_settings(datasette, create, idb, scheduler, sa_credential):
    link_id = (await create(interval_minutes=10))["link"]["id"]

    changed = await post(
        datasette, f"/links/{link_id}/settings", {"interval_minutes": 20}
    )
    assert changed.json()["interval_minutes"] == 20
    task = await scheduler.internal_db.get_task(task_name(link_id))
    assert json.loads(task.schedule_config)["seconds"] == 20 * 60

    # D36: an old value below the floor passes until it's changed.
    await idb.update_link(link_id, interval_minutes=3)
    same = await post(datasette, f"/links/{link_id}/settings", {"interval_minutes": 3})
    assert same.status_code == 200
    lower = await post(datasette, f"/links/{link_id}/settings", {"interval_minutes": 4})
    assert (lower.status_code, lower.json()["code"]) == (400, "interval_too_short")

    options = await post(
        datasette, f"/links/{link_id}/settings", {"options": {"header_row": False}}
    )
    assert (options.status_code, options.json()["code"]) == (400, "invalid_settings")

    bobs = await sa_credential("bob")
    other = await post(
        datasette, f"/links/{link_id}/settings", {"credential_id": bobs.id}
    )
    assert (other.status_code, other.json()["code"]) == (404, "not_found")
    mine = await sa_credential("alice", label="second")
    switched = await post(
        datasette, f"/links/{link_id}/settings", {"credential_id": mine.id}
    )
    assert switched.json()["credential_id"] == mine.id

    # Removing a synced table's interval is an unlink.
    removed = await post(
        datasette, f"/links/{link_id}/settings", {"interval_minutes": None}
    )
    assert removed.json()["synced"] is False
    assert await scheduler.internal_db.get_task(task_name(link_id)) is None
    readd = await post(
        datasette, f"/links/{link_id}/settings", {"interval_minutes": 10}
    )
    assert (readd.status_code, readd.json()["code"]) == (409, "not_schedulable")


@pytest.mark.asyncio
async def test_export_settings_schedule_and_options(
    datasette, alice_sa, data_db, scheduler
):
    await items_table(data_db)
    link_id = (await post(datasette, "/links", export_body(await alice_sa()))).json()[
        "link"
    ]["id"]
    scheduled = await post(
        datasette,
        f"/links/{link_id}/settings",
        {"interval_minutes": 30, "options": {"header_row": False}},
    )
    assert scheduled.status_code == 200, scheduled.json()
    assert scheduled.json()["options"] == {"header_row": False}
    assert await scheduler.internal_db.get_task(task_name(link_id)) is not None


@pytest.mark.asyncio
async def test_nothing_private_is_logged(
    datasette, create, alice_sa, mock_google, caplog
):
    caplog.set_level(logging.DEBUG)
    await create()
    await post(
        datasette, "/inspect", {"credential_id": await alice_sa(), "url": PRIVATE_URL}
    )
    await create(table="t2", credential_id=await alice_sa(key="other"))
    for secret in [*CELL_VALUES, SA_TEST, SA_OTHER, DEFAULT_USER.email, "Students"]:
        assert secret not in caplog.text
