"""The Sheets client (ticket 05) against the vendored mock Google."""

import ast
from pathlib import Path
from types import SimpleNamespace

import datasette_google_credentials
import httpx2
import pytest
from datasette_google_credentials import (
    CredentialBroken,
    GoogleCredentialsError,
    get_credential,
)
from fixtures_sheets import ALICE
from mock_google.errors import ERROR_INFO
from mock_google.oauth import SCOPE_SHEETS, SCOPE_SHEETS_RO
from mock_google.sheets import Formatted

from datasette_google_sheets import sheets
from datasette_google_sheets.sheets import (
    SheetsError,
    Tab,
    append,
    classify,
    clear,
    create_spreadsheet,
    find_tab,
    format_header,
    get_tabs,
    get_values,
    parse_sheet_url,
    quote_sheet_title,
    sheets_error,
    split_rows,
    spreadsheet_url,
)

SHEETS_MODULE = Path(sheets.__file__)
STUDENTS = Tab(gid=0, title="students", index=0, rows=1000, columns=26)
ASSIGNMENTS = Tab(gid=1001, title="assignments", index=1, rows=1000, columns=26)


@pytest.fixture
def cred(datasette, sa_credential):
    """A service-account ``Credential`` for alice with the read-write scope
    (``sa-test@`` is an editor on the fixture spreadsheets)."""

    async def make(scopes=(SCOPE_SHEETS,), key="test"):
        info = await sa_credential("alice", key=key)
        return await get_credential(
            datasette, info.id, actor=ALICE, scopes=list(scopes)
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


def test_uses_only_the_public_google_credentials_api():
    tree = ast.parse(SHEETS_MODULE.read_text())
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
    source = SHEETS_MODULE.read_text()
    assert "logging" not in source
    assert "print(" not in source


# --- Parsing (cases from google-credentials's tests/test_sample_importer.py) ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "https://docs.google.com/spreadsheets/d/abc_DEF-123/edit",
            ("abc_DEF-123", None),
        ),
        ("https://docs.google.com/spreadsheets/d/abc/edit#gid=1001", ("abc", 1001)),
        ("https://docs.google.com/spreadsheets/d/abc/edit?gid=7#gid=7", ("abc", 7)),
        (
            "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing&gid=3",
            ("abc", 3),
        ),
        ("https://docs.google.com/spreadsheets/u/1/d/abc/htmlview", ("abc", None)),
        (
            "  1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms ",
            (
                "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
                None,
            ),
        ),
    ],
)
def test_parse_sheet_url(text, expected):
    assert parse_sheet_url(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a url",
        "https://example.com/spreadsheets/d/abc/edit",
        "https://docs.google.com/document/d/abc",
    ],
)
def test_parse_sheet_url_rejects(text):
    with pytest.raises(ValueError):
        parse_sheet_url(text)


def test_quote_sheet_title():
    assert quote_sheet_title("students") == "'students'"
    assert quote_sheet_title("Bob's A1") == "'Bob''s A1'"


def test_spreadsheet_url():
    assert spreadsheet_url("abc", None) == (
        "https://docs.google.com/spreadsheets/d/abc/edit"
    )
    assert spreadsheet_url("abc", 1001) == (
        "https://docs.google.com/spreadsheets/d/abc/edit#gid=1001"
    )
    assert spreadsheet_url("a/b", 0).startswith(
        "https://docs.google.com/spreadsheets/d/a%2Fb/edit"
    )


# --- Tabs ------------------------------------------------------------------------


def test_tab_from_properties_defaults_omitted_zeros():
    # Google omits zero-valued fields: the first tab often has no sheetId/index.
    assert Tab.from_properties(
        {"title": "Sheet1", "gridProperties": {"rowCount": 5, "columnCount": 2}}
    ) == Tab(gid=0, title="Sheet1", index=0, rows=5, columns=2)


@pytest.mark.asyncio
async def test_get_tabs(cred, mock_google):
    title, tabs = await get_tabs(await cred(), "students")
    assert title == "Students"
    assert tabs == [STUDENTS, ASSIGNMENTS]
    (call,) = mock_google.calls("/v4/spreadsheets/students", method="GET")
    assert call.query["fields"] == ["properties.title,sheets.properties"]


@pytest.mark.asyncio
async def test_get_tabs_grid_properties_and_order(cred, mock_google):
    mock_google.sheets.add(
        "sized",
        {"first": [["a"]], "second": [["b"]]},
        title="Sized",
        sheet_ids=[7, 3],
        row_count=40,
        column_count=6,
    )
    # Listed out of order: get_tabs sorts by index.
    ss = mock_google.sheets.get("sized")
    ss.sheets.reverse()
    title, tabs = await get_tabs(await cred(), "sized")
    assert title == "Sized"
    assert tabs == [
        Tab(gid=7, title="first", index=0, rows=40, columns=6),
        Tab(gid=3, title="second", index=1, rows=40, columns=6),
    ]


def test_find_tab():
    tabs = [STUDENTS, ASSIGNMENTS]
    assert find_tab(tabs, 1001) is ASSIGNMENTS
    assert find_tab(tabs, 0) is STUDENTS
    assert find_tab(tabs, 5) is None


# --- Values ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_values_trimmed_as_google_returns_them(cred, mock_google):
    _, tabs = await get_tabs(await cred(), "ragged")
    values = await get_values(await cred(), "ragged", tabs[0])
    assert values == [
        ["name", "", "name", "score"],
        ["alice", "x", "a2", 10],
        ["bob"],
        [],
        ["carol", "", "", 30, "extra"],
    ]
    (call,) = mock_google.calls("/v4/spreadsheets/ragged/values/", method="GET")
    assert call.path == "/v4/spreadsheets/ragged/values/'data'"
    assert call.query["valueRenderOption"] == ["UNFORMATTED_VALUE"]
    assert call.query["dateTimeRenderOption"] == ["FORMATTED_STRING"]


@pytest.mark.asyncio
async def test_get_values_render_options_and_quoted_title(cred, mock_google):
    mock_google.sheets.add(
        "dates",
        {"Bob's A1": [["when", "price"], [Formatted(45292, "2024-01-01", True), 1.5]]},
    )
    tab = Tab(gid=0, title="Bob's A1", index=0, rows=1000, columns=26)
    # Numbers unformatted, dates as their formatted text.
    assert await get_values(await cred(), "dates", tab) == [
        ["when", "price"],
        ["2024-01-01", 1.5],
    ]


@pytest.mark.asyncio
async def test_get_values_empty_tab(cred, mock_google):
    mock_google.sheets.add("empty", {"Sheet1": []})
    tab = Tab(gid=0, title="Sheet1", index=0, rows=1000, columns=26)
    assert await get_values(await cred(), "empty", tab) == []


@pytest.mark.asyncio
async def test_clear(cred, mock_google):
    await clear(await cred(), "students", ASSIGNMENTS)
    ss = mock_google.sheets.get("students")
    assert ss.sheet_by_id(1001).rows() == []
    assert ss.sheet_by_id(0).rows()  # other tabs untouched
    (call,) = mock_google.calls("/v4/spreadsheets/students/values/", method="POST")
    assert call.path == "/v4/spreadsheets/students/values/'assignments':clear"


# --- Append ----------------------------------------------------------------------


def test_split_rows():
    rows = [[i] for i in range(5)]
    assert split_rows(rows, 2) == [[[0], [1]], [[2], [3]], [[4]]]
    assert split_rows(rows, 10) == [rows]
    assert split_rows([], 3) == []


def test_split_rows_never_ends_a_chunk_on_a_blank_row():
    rows = [["a"], ["b"], [""], ["", ""], ["c"], ["d"], [""], [""]]
    chunks = split_rows(rows, 2)
    assert chunks == [
        [["a"], ["b"]],
        [[""], ["", ""], ["c"]],
        [["d"], [""], [""]],  # the last chunk may end blank
    ]
    assert [row for chunk in chunks for row in chunk] == rows


@pytest.mark.asyncio
async def test_append_grows_the_grid_in_chunks(cred, mock_google):
    mock_google.sheets.add("grow", {"Sheet1": [["n"]]}, row_count=3, column_count=1)
    tab = Tab(gid=0, title="Sheet1", index=0, rows=3, columns=1)
    rows = [[i, f"row {i}"] for i in range(10)]
    written = await append(await cred(), "grow", tab, rows, chunk_rows=4)
    assert written == 10
    sheet = mock_google.sheets.get("grow").sheets[0]
    assert sheet.rows() == [["n"], *rows]
    assert sheet.row_count >= 11 and sheet.column_count == 2
    calls = mock_google.calls("/v4/spreadsheets/grow/values/", method="POST")
    assert [len(c.json["values"]) for c in calls] == [4, 4, 2]
    for c in calls:
        assert c.path == "/v4/spreadsheets/grow/values/'Sheet1':append"
        assert c.query["valueInputOption"] == ["RAW"]
        assert c.query["insertDataOption"] == ["INSERT_ROWS"]
        assert c.json["majorDimension"] == "ROWS"


@pytest.mark.asyncio
async def test_append_keeps_blank_rows_in_place(cred, mock_google):
    # With a naive split, the chunk after ["b"], [""] would be appended right
    # below "b": append finds the table's end by its last non-empty row.
    mock_google.sheets.add("blanks", {"Sheet1": []})
    tab = Tab(gid=0, title="Sheet1", index=0, rows=1000, columns=26)
    rows = [["a"], ["b"], [""], [""], ["c"], ["d"], [""]]
    assert await append(await cred(), "blanks", tab, rows, chunk_rows=2) == 7
    assert mock_google.sheets.get("blanks").sheets[0].rows() == [
        ["a"],
        ["b"],
        [],
        [],
        ["c"],
        ["d"],
    ]
    calls = mock_google.calls("/v4/spreadsheets/blanks/values/", method="POST")
    assert [len(c.json["values"]) for c in calls] == [2, 3, 2]


@pytest.mark.asyncio
async def test_append_nothing_makes_no_request(cred, mock_google):
    assert await append(await cred(), "students", STUDENTS, []) == 0
    assert not mock_google.calls("/v4/spreadsheets/students/values/")


@pytest.mark.asyncio
async def test_append_partial_failure_reports_rows_written(cred, mock_google):
    mock_google.sheets.add("partial", {"Sheet1": []})
    tab = Tab(gid=0, title="Sheet1", index=0, rows=1000, columns=26)
    mock_google.faults.fail("/v4/spreadsheets/partial/values/", 500, after=2)
    rows = [[i] for i in range(10)]
    with pytest.raises(SheetsError) as excinfo:
        await append(await cred(), "partial", tab, rows, chunk_rows=3)
    error = excinfo.value
    assert (error.status, error.kind, error.transient) == (500, "server", True)
    assert error.rows_written == 6
    assert mock_google.sheets.get("partial").sheets[0].rows() == rows[:6]


# --- Create and format -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_spreadsheet(oauth_cred, mock_google):
    spreadsheet_id, tab = await create_spreadsheet(await oauth_cred(), "Export")
    assert tab == Tab(gid=0, title="Sheet1", index=0, rows=1000, columns=26)
    ss = mock_google.sheets.get(spreadsheet_id)
    assert ss.title == "Export"
    assert ss.created_by == "user@example.com"
    (call,) = mock_google.calls("/v4/spreadsheets", method="POST")
    assert call.json == {"properties": {"title": "Export"}}


@pytest.mark.asyncio
async def test_format_header(cred, mock_google):
    await format_header(await cred(), "students", ASSIGNMENTS)
    ss = mock_google.sheets.get("students")
    assert ss.batch_updates == [
        [
            {
                "repeatCell": {
                    "range": {"sheetId": 1001, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": 1001,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
        ]
    ]
    assert ss.sheet_by_id(1001).frozen_row_count == 1
    assert ss.sheet_by_id(0).frozen_row_count == 0
    (call,) = mock_google.calls("/v4/spreadsheets/students:batchUpdate")
    assert call.method == "POST"


# --- Errors ----------------------------------------------------------------------


async def raises(coro) -> SheetsError:
    with pytest.raises(SheetsError) as excinfo:
        await coro
    return excinfo.value


@pytest.mark.asyncio
async def test_not_shared(cred):
    error = await raises(get_tabs(await cred(), "private"))
    assert (error.status, error.reason, error.kind) == (403, None, "not_shared")
    assert error.google_status == "PERMISSION_DENIED"
    assert error.message == "The caller does not have permission"
    assert str(error) == error.message
    assert not error.transient


@pytest.mark.asyncio
async def test_read_only_share_on_write_is_not_shared(cred):
    error = await raises(append(await cred(), "readonly", STUDENTS, [["x"]]))
    assert (error.status, error.kind, error.rows_written) == (403, "not_shared", 0)


@pytest.mark.asyncio
async def test_not_found(cred, mock_google):
    mock_google.sheets.delete("students")
    error = await raises(get_values(await cred(), "students", STUDENTS))
    assert (error.status, error.reason, error.kind) == (404, None, "not_found")
    assert not error.transient


@pytest.mark.asyncio
async def test_scope_insufficient(cred):
    # A read-only token trying to write: the mock's real scope check.
    read_only = await cred(scopes=[SCOPE_SHEETS_RO])
    error = await raises(clear(read_only, "students", STUDENTS))
    assert (error.status, error.reason, error.kind) == (
        403,
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "scope",
    )


@pytest.mark.asyncio
async def test_service_disabled_service_account(cred, mock_google):
    mock_google.faults.fail("/v4/", reason="SERVICE_DISABLED")
    error = await raises(get_tabs(await cred(), "students"))
    assert (error.status, error.reason, error.kind) == (
        403,
        "SERVICE_DISABLED",
        "api_disabled",
    )
    assert (
        "Enable the Google Sheets API in the Cloud project of this credential"
        in error.message
    )
    assert "the project the service account belongs to" in error.message
    # Google's message (with its console link) is kept.
    assert "Google said: Google Sheets API has not been used in project" in (
        error.message
    )
    assert not error.transient


@pytest.mark.asyncio
async def test_service_disabled_oauth_names_the_oauth_client_project(
    oauth_cred, mock_google
):
    mock_google.faults.fail("/v4/", reason="SERVICE_DISABLED")
    error = await raises(create_spreadsheet(await oauth_cred(), "Export"))
    assert error.kind == "api_disabled"
    assert "the project of the OAuth client behind Connect Google" in error.message


@pytest.mark.asyncio
async def test_rate_limited(cred, mock_google):
    mock_google.faults.fail("/v4/", 429)
    error = await raises(get_tabs(await cred(), "students"))
    assert (error.status, error.reason, error.kind) == (
        429,
        "RATE_LIMIT_EXCEEDED",
        "rate_limited",
    )
    assert error.google_status == "RESOURCE_EXHAUSTED"
    assert error.transient


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_server_error(cred, mock_google, status):
    mock_google.faults.fail("/v4/", status)
    error = await raises(format_header(await cred(), "students", STUDENTS))
    assert (error.status, error.kind, error.transient) == (status, "server", True)


@pytest.mark.asyncio
async def test_other(cred):
    missing = Tab(gid=9, title="no such tab", index=0, rows=1, columns=1)
    error = await raises(get_values(await cred(), "students", missing))
    assert (error.status, error.reason, error.kind) == (400, None, "other")
    assert error.message.startswith("Unable to parse range")
    assert not error.transient


@pytest.mark.parametrize(
    "code,status,reason,kind",
    [
        (403, "PERMISSION_DENIED", None, "not_shared"),
        (403, None, None, "not_shared"),
        (403, "PERMISSION_DENIED", "SOME_OTHER_REASON", "not_shared"),
        (403, "PERMISSION_DENIED", "SERVICE_DISABLED", "api_disabled"),
        (403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "scope"),
        (403, "FAILED_PRECONDITION", "SOME_OTHER_REASON", "other"),
        (404, "NOT_FOUND", None, "not_found"),
        (429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED", "rate_limited"),
        (429, None, None, "rate_limited"),
        (500, "INTERNAL", None, "server"),
        (502, None, None, "server"),
        (400, "INVALID_ARGUMENT", None, "other"),
        (401, "UNAUTHENTICATED", None, "other"),
    ],
)
def test_classify(code, status, reason, kind):
    assert classify(code, status, reason) == kind


def _stub_cred(type_="service_account"):
    return SimpleNamespace(info=SimpleNamespace(type=type_))


def test_classification_ignores_the_message_text():
    body = {
        "error": {
            "code": 403,
            "message": "SERVICE_DISABLED ACCESS_TOKEN_SCOPE_INSUFFICIENT not found",
            "status": "PERMISSION_DENIED",
        }
    }
    error = sheets_error(httpx2.Response(403, json=body), _stub_cred())
    assert (error.kind, error.reason) == ("not_shared", None)


def test_reason_comes_from_error_info_only():
    body = {
        "error": {
            "code": 403,
            "message": "m",
            "status": "PERMISSION_DENIED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.Help", "reason": "NOPE"},
                {"@type": ERROR_INFO, "reason": "SERVICE_DISABLED"},
            ],
        }
    }
    error = sheets_error(httpx2.Response(403, json=body), _stub_cred())
    assert (error.kind, error.reason) == ("api_disabled", "SERVICE_DISABLED")


def test_non_json_error_body():
    response = httpx2.Response(502, text="<html>Bad Gateway</html>")
    error = sheets_error(response, _stub_cred())
    assert (error.status, error.reason, error.kind) == (502, None, "server")
    assert error.message == "HTTP 502"
    assert error.google_status is None


def test_repr_has_no_message():
    error = SheetsError(
        status=403, reason=None, message="about sheet abc", kind="not_shared"
    )
    assert "abc" not in repr(error)


@pytest.mark.asyncio
async def test_google_credentials_errors_propagate_unchanged(oauth_cred, mock_google):
    cred = await oauth_cred()
    for refresh_token in mock_google.oauth.refresh_tokens():
        mock_google.oauth.revoke(refresh_token)
    with pytest.raises(GoogleCredentialsError) as excinfo:
        await get_tabs(cred, "students")
    assert isinstance(excinfo.value, CredentialBroken)
    assert not isinstance(excinfo.value, SheetsError)
