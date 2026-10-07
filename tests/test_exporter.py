"""The export runner (ticket 08) against the vendored mock Google."""

import ast
import json
from pathlib import Path

import datasette_google_credentials
import httpx2
import pytest
import pytest_asyncio
from datasette_google_credentials import CredentialBroken, get_credential
from fixtures_export import EXPORT_MAX_CELLS, FORMULA, JSON_TEXT
from fixtures_sheets import ALICE, BOB
from mock_google.oauth import SCOPE_SHEETS

from datasette_google_sheets import exporter, sheets
from datasette_google_sheets.exporter import (
    ExportError,
    cell_value,
    read_rows,
    run_export,
)

EXPORTER_MODULE = Path(exporter.__file__)
OLD = [["old", "content"], ["x", "y"], ["z", "w"]]
OTHER_TAB = [["keep", "me"]]


@pytest_asyncio.fixture
async def datasette(export_datasette):
    """Credentials (sa_credential / oauth_credential) live on the export
    Datasette."""
    return export_datasette


@pytest.fixture
def target(mock_google):
    """The ``export`` spreadsheet: tab ``Existing`` (gid 0) with old content,
    tab ``Other`` (gid 1001) that must never be touched."""
    return mock_google.sheets.add(
        "export", {"Existing": [row[:] for row in OLD], "Other": OTHER_TAB}
    )


@pytest.fixture
def sa_cred(datasette, sa_credential):
    async def make(key="test"):
        info = await sa_credential("alice", key=key)
        return await get_credential(
            datasette, info.id, actor=ALICE, scopes=[SCOPE_SHEETS]
        )

    return make


@pytest.fixture
def oauth_cred(datasette, oauth_credential):
    async def make():
        info = await oauth_credential("alice")
        return await get_credential(
            datasette, info.id, actor=ALICE, scopes=[SCOPE_SHEETS]
        )

    return make


def items_rows(ns):
    """``items`` rows as they land in the sheet (rowid, n, s, b)."""
    rows = []
    for n in ns:
        s = f"row {n}"
        if n == 2:
            s = ""
        elif n == 3:
            s = FORMULA
        elif n == 4:
            s = JSON_TEXT
        b = json.dumps({"$base64": True, "encoded": "AP8="}) if n == 1 else ""
        rows.append([n, n, s, b])
    return rows


def sheet_rows(mock_google, spreadsheet_id="export", index=0):
    return mock_google.sheets.get(spreadsheet_id).sheets[index].rows()


def trimmed(rows):
    """Rows as the mock returns them: trailing empty cells dropped."""
    out = []
    for row in rows:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        out.append(row)
    return out


def sheets_calls(mock_google, method=None):
    return mock_google.calls("/v4/", method=method)


# --- Guards ------------------------------------------------------------------------


def test_uses_only_the_public_google_credentials_api():
    tree = ast.parse(EXPORTER_MODULE.read_text())
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


def test_never_logs():
    source = EXPORTER_MODULE.read_text()
    assert "logging" not in source
    assert "print(" not in source


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        ("text", "text"),
        (FORMULA, FORMULA),
        (0, 0),
        (1.5, 1.5),
        (True, True),
        ({"$base64": True, "encoded": "AP8="}, '{"$base64": true, "encoded": "AP8="}'),
        ([1, 2], "[1, 2]"),
    ],
)
def test_cell_value(value, expected):
    assert cell_value(value) == expected


# --- Sources, read as the actor --------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_a_rowid_table(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link()
    result = await run_export(datasette, link, await sa_cred(), ALICE)

    # Paged (25 rows, max_returned_rows 10), rowid included, old content gone.
    expected = [["rowid", "n", "s", "b"], *items_rows(range(1, 26))]
    assert sheet_rows(mock_google) == trimmed(expected)
    # The formula stays literal text: written RAW, never USER_ENTERED.
    appends = mock_google.calls("/v4/spreadsheets/export/values/", method="POST")
    appends = [c for c in appends if c.path.endswith(":append")]
    assert appends
    assert {c.query["valueInputOption"][0] for c in appends} == {"RAW"}
    # Clear came first, then the appends, then one bold + frozen batchUpdate.
    posts = [c.path.rsplit(":", 1)[-1] for c in sheets_calls(mock_google, "POST")]
    assert posts == ["clear", "append", "batchUpdate"]
    spreadsheet = mock_google.sheets.get("export")
    assert spreadsheet.batch_updates == [sheets.header_format_requests(0)]
    assert spreadsheet.sheets[0].frozen_row_count == 1
    # Other tabs are never touched.
    assert sheet_rows(mock_google, index=1) == OTHER_TAB

    assert result.rows_read == 25
    assert result.rows_written == 25
    assert result.cells == 26 * 4
    assert (result.spreadsheet_id, result.gid) == ("export", 0)
    assert result.url == sheets.spreadsheet_url("export", 0)
    assert (result.spreadsheet_title, result.sheet_title) == ("export", "Existing")


@pytest.mark.asyncio
async def test_primary_key_table_has_no_rowid(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link(table_name="people")
    await run_export(datasette, link, await sa_cred(), ALICE)
    assert sheet_rows(mock_google) == [
        ["id", "name"],
        ["a", "Ann"],
        ["b", "Ben"],
        ["c", "Cat"],
    ]


@pytest.mark.asyncio
async def test_view_pages_by_offset(datasette, export_link):
    link = await export_link(source_kind="view", table_name="items_v")
    columns, rows = await read_rows(datasette, ALICE, link)
    assert columns == ["n", "s"]
    assert [row[0] for row in rows] == list(range(1, 24))


@pytest.mark.asyncio
async def test_stored_query_with_params(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link(
        source_kind="query",
        table_name=None,
        query_name="by_n",
        params={"min": 22, "_shape": "objects"},
    )
    result = await run_export(datasette, link, await sa_cred(), ALICE)
    assert sheet_rows(mock_google) == [
        ["n", "s"],
        [23, "row 23"],
        [24, "row 24"],
        [25, "row 25"],
    ]
    assert result.rows_read == 3


@pytest.mark.asyncio
async def test_sql_with_params(datasette, export_link, sa_cred, target, mock_google):
    link = await export_link(
        source_kind="sql",
        table_name=None,
        sql="select n, s from items where n between :lo and :hi",
        params={"lo": "3", "hi": "4"},
    )
    await run_export(datasette, link, await sa_cred(), ALICE)
    assert sheet_rows(mock_google) == [["n", "s"], [3, FORMULA], [4, JSON_TEXT]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {
            "source_kind": "query",
            "table_name": None,
            "query_name": "by_n",
            "params": {"min": 0},
        },
        {"source_kind": "sql", "table_name": None, "sql": "select * from items"},
    ],
)
async def test_truncated_query_is_refused_with_the_hint(
    datasette, export_link, sa_cred, target, mock_google, fields
):
    link = await export_link(**fields)
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE)
    error = excinfo.value
    assert error.code == "truncated"
    assert "Save it as a SQL view to export it in full" in error.message
    assert "raise max_returned_rows (currently 10)" in error.message
    assert not sheets_calls(mock_google)
    assert sheet_rows(mock_google) == OLD


# --- The cell cap --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cell_cap_counts_the_header_row_and_writes_nothing(
    datasette, export_link, sa_cred, target, mock_google
):
    # 100 rows x 10 columns = 1,000 cells; with the header, 1,010.
    link = await export_link(table_name="wide")
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE)
    assert excinfo.value.code == "too_large"
    assert f"{EXPORT_MAX_CELLS:,}" in excinfo.value.message
    assert not sheets_calls(mock_google)
    assert sheet_rows(mock_google) == OLD


@pytest.mark.asyncio
async def test_cell_cap_exactly_reached_without_a_header(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link(table_name="wide", options={"header_row": False})
    result = await run_export(datasette, link, await sa_cred(), ALICE)
    assert result.cells == EXPORT_MAX_CELLS
    assert result.rows_written == 100
    rows = sheet_rows(mock_google)
    assert len(rows) == 100
    assert rows[0] == list(range(10))
    # No header row: no header formatting either.
    assert mock_google.sheets.get("export").batch_updates == []


@pytest.mark.asyncio
async def test_reading_stops_once_past_the_cap(datasette, export_link):
    link = await export_link(table_name="wide")
    columns, rows = await read_rows(datasette, ALICE, link, max_cells=150)
    # Pages of 10 rows: the second page takes it past 150 cells.
    assert len(columns) == 10
    assert len(rows) == 20


# --- Modes -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_spreadsheet_with_oauth_then_replace(
    datasette, export_link, oauth_cred, mock_google
):
    cred = await oauth_cred()
    link = await export_link(mode="new", spreadsheet_id="", table_name="people")
    result = await run_export(datasette, link, cred, ALICE)

    created = mock_google.sheets.get(result.spreadsheet_id)
    assert created is not None
    assert created.title == "people"  # defaults to the table name
    assert result.spreadsheet_title == "people"
    assert result.gid == created.sheets[0].sheet_id
    assert result.url == sheets.spreadsheet_url(result.spreadsheet_id, result.gid)
    expected = [["id", "name"], ["a", "Ann"], ["b", "Ben"], ["c", "Cat"]]
    assert created.sheets[0].rows() == expected
    assert created.batch_updates == [sheets.header_format_requests(result.gid)]

    # Ticket 09 stores the id and gid; later runs replace that tab.
    link = link.model_copy(
        update={"spreadsheet_id": result.spreadsheet_id, "sheet_gid": result.gid}
    )
    again = await run_export(datasette, link, cred, ALICE)
    assert again.spreadsheet_id == result.spreadsheet_id
    assert created.sheets[0].rows() == expected
    creates = [c for c in mock_google.calls() if c.path == "/v4/spreadsheets"]
    assert len(creates) == 1


@pytest.mark.asyncio
async def test_new_spreadsheet_title_from_the_link(
    datasette, export_link, oauth_cred, mock_google
):
    link = await export_link(
        mode="new",
        spreadsheet_id="",
        source_kind="sql",
        table_name=None,
        sql="select 1 as one",
        spreadsheet_title="My export",
    )
    result = await run_export(datasette, link, await oauth_cred(), ALICE)
    assert mock_google.sheets.get(result.spreadsheet_id).title == "My export"


@pytest.mark.asyncio
async def test_service_account_cannot_create_a_spreadsheet(
    datasette, export_link, sa_cred, mock_google
):
    cred = await sa_cred()
    link = await export_link(mode="new", spreadsheet_id="")
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, cred, ALICE)
    error = excinfo.value
    assert error.code == "sa_cannot_create"
    assert error.share_with == cred.info.google_email
    assert cred.info.google_email in error.message
    assert "Editor" in error.message
    assert not sheets_calls(mock_google)


@pytest.mark.asyncio
async def test_append_keeps_existing_rows_and_skips_formatting(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link(mode="append", table_name="people")
    result = await run_export(datasette, link, await sa_cred(), ALICE)
    assert sheet_rows(mock_google) == [
        *OLD,
        ["id", "name"],
        ["a", "Ann"],
        ["b", "Ben"],
        ["c", "Cat"],
    ]
    paths = [c.path for c in sheets_calls(mock_google)]
    assert not any(p.endswith(":clear") or p.endswith(":batchUpdate") for p in paths)
    assert result.rows_written == 3


@pytest.mark.asyncio
async def test_append_without_header_row(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link(
        mode="append", table_name="people", options={"header_row": False}
    )
    await run_export(datasette, link, await sa_cred(), ALICE)
    assert sheet_rows(mock_google) == [*OLD, ["a", "Ann"], ["b", "Ben"], ["c", "Cat"]]


@pytest.mark.asyncio
async def test_tab_missing(datasette, export_link, sa_cred, target, mock_google):
    link = await export_link(sheet_gid=4242)
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE)
    assert excinfo.value.code == "tab_missing"
    assert sheet_rows(mock_google) == OLD


@pytest.mark.asyncio
async def test_sheets_errors_before_writing_propagate_unchanged(
    datasette, export_link, sa_cred, mock_google
):
    link = await export_link(spreadsheet_id="readonly")
    with pytest.raises(sheets.SheetsError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE)
    assert excinfo.value.kind == "not_shared"


# --- Partial failures ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_google_failure_reports_rows_written(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link()
    # Chunks of 10 sheet rows (header + 9, then 10, then 6): the third fails.
    mock_google.faults.fail(
        "/v4/spreadsheets/export/values/", 500, method="POST", after=3
    )
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE, chunk_rows=10)
    error = excinfo.value
    assert error.code == "partial"
    assert error.rows_written == 19
    assert error.kind == "server"
    assert isinstance(error.__cause__, sheets.SheetsError)
    assert "19 rows were written" in error.message
    assert len(sheet_rows(mock_google)) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ReadTimeout("timed out"),
        CredentialBroken("The Google credential is broken"),
    ],
)
async def test_partial_transport_or_auth_failure_counts_chunks(
    datasette, export_link, sa_cred, target, mock_google, monkeypatch, failure
):
    """D31: a GoogleCredentialsError or transport error between chunks carries no
    rows_written, so the runner counts the chunks itself."""
    real_append = sheets.append
    calls = 0

    async def flaky_append(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise failure
        return await real_append(*args, **kwargs)

    monkeypatch.setattr(sheets, "append", flaky_append)
    link = await export_link()
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE, chunk_rows=10)
    error = excinfo.value
    assert error.code == "partial"
    assert error.rows_written == 19
    assert error.kind is None
    assert error.__cause__ is failure
    assert "timed out" not in error.message
    assert len(sheet_rows(mock_google)) == 20


@pytest.mark.asyncio
async def test_failure_on_the_first_chunk_is_not_partial(
    datasette, export_link, sa_cred, target, mock_google
):
    link = await export_link()
    mock_google.faults.fail(
        "/v4/spreadsheets/export/values/", 500, method="POST", after=1
    )
    with pytest.raises(sheets.SheetsError) as excinfo:
        await run_export(datasette, link, await sa_cred(), ALICE)
    assert excinfo.value.kind == "server"


# --- Read errors -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,code",
    [
        ({"table_name": "secret"}, "forbidden"),
        (
            {"source_kind": "query", "table_name": None, "query_name": "alice_only"},
            "forbidden",
        ),
        ({"table_name": "missing"}, "source_missing"),
        ({"source_kind": "view", "table_name": "gone_v"}, "source_missing"),
        (
            {"source_kind": "query", "table_name": None, "query_name": "gone"},
            "source_missing",
        ),
        ({"database_name": "nope"}, "source_missing"),
        (
            {"source_kind": "sql", "table_name": None, "sql": "select nope"},
            "read_failed",
        ),
    ],
)
async def test_read_errors(
    datasette, export_link, sa_cred, target, mock_google, fields, code
):
    link = await export_link(**fields)
    with pytest.raises(ExportError) as excinfo:
        await run_export(datasette, link, await sa_cred(), BOB)
    assert excinfo.value.code == code
    assert not sheets_calls(mock_google)
    assert repr(excinfo.value) == f"<ExportError {code}>"


@pytest.mark.asyncio
async def test_owner_can_read_what_bob_cannot(datasette, export_link):
    link = await export_link(table_name="secret")
    assert await read_rows(datasette, ALICE, link) == (
        ["rowid", "x"],
        [[1, "hidden"]],
    )
