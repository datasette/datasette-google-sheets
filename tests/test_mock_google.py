"""The vendored mock Google (ticket 04): its Sheets API, access lists and
error bodies, the network block, and the Datasette + credential fixtures."""

import socket

import httpx2
import mock_google as mock_google_package
import pytest
from datasette_google_auth import MissingScopes, get_credential
from fixtures_google import NetworkBlocked
from fixtures_sheets import ALICE, BOB, SHEETS_PLUGIN, make_datasette
from mock_google import SHEETS_BASE
from mock_google.errors import ERROR_INFO
from mock_google.keys import SA_OTHER, SA_TEST
from mock_google.oauth import (
    SCOPE_EMAIL,
    SCOPE_OPENID,
    SCOPE_SHEETS,
    SCOPE_SHEETS_RO,
    GoogleUser,
)
from mock_google.sheets import READER, WRITER, Formatted
from mock_google.tokens import Principal

USER = "user@example.com"


def token(mock_google, email=USER, scopes=(SCOPE_SHEETS,)) -> str:
    """An access token for ``email`` straight from the mock's token store."""
    issued = mock_google.tokens.issue(Principal(email, frozenset(scopes)))
    return issued["access_token"]


async def call(mock_google, method, path, *, as_=USER, scopes=(SCOPE_SHEETS,), **kw):
    headers = {"Authorization": f"Bearer {token(mock_google, as_, scopes)}"}
    async with httpx2.AsyncClient(
        transport=mock_google.transport, base_url=SHEETS_BASE
    ) as client:
        return await client.request(method, path, headers=headers, **kw)


async def values(mock_google, ss_id, range_, **params):
    response = await call(
        mock_google, "GET", f"/v4/spreadsheets/{ss_id}/values/{range_}", params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def assert_google_error(response, code, status, reason=None):
    """The google.rpc.Status envelope (AIP-193); ErrorInfo only with a reason."""
    assert response.status_code == code
    error = response.json()["error"]
    assert error["code"] == code
    assert error["status"] == status
    assert isinstance(error["message"], str) and error["message"]
    if reason is None:
        assert set(error) == {"code", "message", "status"}
    else:
        assert set(error) == {"code", "message", "status", "details"}
        (info,) = error["details"]
        assert info["@type"] == ERROR_INFO
        assert info["reason"] == reason
        assert info["domain"] == "googleapis.com"


# --- vendoring and the network block ------------------------------------------


def test_vendored_source_is_recorded():
    doc = mock_google_package.__doc__
    assert "datasette-google-auth" in doc
    assert "a2f4eee" in doc
    assert "datasette_google_auth.testing" in doc


def test_network_is_blocked():
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("sheets.googleapis.com", 443)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()


# --- spreadsheets.get ---------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_sheet_properties(mock_google):
    response = await call(
        mock_google,
        "GET",
        "/v4/spreadsheets/students",
        params={"fields": "sheets.properties"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "students",
                    "index": 0,
                    "sheetType": "GRID",
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            },
            {
                "properties": {
                    "sheetId": 1001,
                    "title": "assignments",
                    "index": 1,
                    "sheetType": "GRID",
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            },
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            "spreadsheetId,properties.title",
            {"spreadsheetId": "students", "properties": {"title": "Students"}},
        ),
        (
            "sheets(properties(sheetId,title))",
            {
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "students"}},
                    {"properties": {"sheetId": 1001, "title": "assignments"}},
                ]
            },
        ),
        (
            "sheets.properties.gridProperties",
            {
                "sheets": [
                    {"properties": {"gridProperties": grid}}
                    for grid in [{"rowCount": 1000, "columnCount": 26}] * 2
                ]
            },
        ),
    ],
)
async def test_metadata_field_masks(mock_google, fields, expected):
    response = await call(
        mock_google, "GET", "/v4/spreadsheets/students", params={"fields": fields}
    )
    assert response.json() == expected


@pytest.mark.asyncio
async def test_metadata_bad_field_mask(mock_google):
    response = await call(
        mock_google, "GET", "/v4/spreadsheets/students", params={"fields": "sheets("}
    )
    assert_google_error(response, 400, "INVALID_ARGUMENT")


@pytest.mark.asyncio
async def test_metadata_without_fields_is_everything(mock_google):
    response = await call(mock_google, "GET", "/v4/spreadsheets/students")
    data = response.json()
    assert set(data) == {"spreadsheetId", "properties", "sheets", "spreadsheetUrl"}
    assert data["sheets"][0]["properties"]["gridProperties"] == {
        "rowCount": 1000,
        "columnCount": 26,
    }


@pytest.mark.asyncio
async def test_grid_properties_reflect_seeded_size(mock_google):
    mock_google.sheets.add(
        "small", {"Data": [["a"], ["b"]]}, row_count=2, column_count=1
    )
    response = await call(
        mock_google, "GET", "/v4/spreadsheets/small", params={"fields": "sheets"}
    )
    props = response.json()["sheets"][0]["properties"]
    assert props["gridProperties"] == {"rowCount": 2, "columnCount": 1}


# --- values.get ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_values_trims_trailing_empties(mock_google):
    mock_google.sheets.add(
        "trim",
        {
            "Sheet1": [
                ["a", "b", None, None],
                ["c", None, None],
                [],  # empty middle row: kept as []
                [None, "d"],  # leading gap: kept as ""
                [],
                [None, None],
            ]
        },
    )
    data = await values(
        mock_google,
        "trim",
        "Sheet1",
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
    )
    assert data == {
        "range": "Sheet1!A1:Z1000",
        "majorDimension": "ROWS",
        "values": [["a", "b"], ["c"], [], ["", "d"]],
    }


@pytest.mark.asyncio
async def test_values_empty_sheet_has_no_values_key(mock_google):
    mock_google.sheets.add("empty", {"Sheet1": []})
    data = await values(mock_google, "empty", "Sheet1")
    assert data == {"range": "Sheet1!A1:Z1000", "majorDimension": "ROWS"}


@pytest.mark.asyncio
async def test_values_render_options(mock_google):
    mock_google.sheets.add(
        "types",
        {
            "Sheet1": [
                [
                    1,
                    2.5,
                    3.0,
                    True,
                    "text",
                    Formatted(45292, "2024-01-01", datetime=True),
                    Formatted(1.5, "$1.50"),
                ]
            ]
        },
    )
    unformatted = await values(
        mock_google,
        "types",
        "Sheet1",
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
    )
    assert unformatted["values"] == [[1, 2.5, 3.0, True, "text", "2024-01-01", 1.5]]
    serial = await values(
        mock_google, "types", "Sheet1", valueRenderOption="UNFORMATTED_VALUE"
    )
    assert serial["values"][0][5] == 45292
    formatted = await values(mock_google, "types", "Sheet1")
    assert formatted["values"] == [
        ["1", "2.5", "3", "TRUE", "text", "2024-01-01", "$1.50"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"valueRenderOption": "NOPE"}, {"dateTimeRenderOption": "NOPE"}],
)
async def test_values_rejects_unknown_render_options(mock_google, params):
    response = await call(
        mock_google, "GET", "/v4/spreadsheets/students/values/students", params=params
    )
    assert_google_error(response, 400, "INVALID_ARGUMENT")


# --- values.append / values.clear ---------------------------------------------


@pytest.mark.asyncio
async def test_append_insert_rows_grows_grid(mock_google):
    ss = mock_google.sheets.add(
        "grow",
        {"Sheet1": [["name", "n"], ["a", 1]]},
        row_count=3,
        column_count=2,
    )
    response = await call(
        mock_google,
        "POST",
        "/v4/spreadsheets/grow/values/Sheet1!A1:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [["b", 2], ["c", 3, "extra"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "spreadsheetId": "grow",
        "tableRange": "Sheet1!A1:B2",
        "updates": {
            "spreadsheetId": "grow",
            "updatedRange": "Sheet1!A3:C4",
            "updatedRows": 2,
            "updatedColumns": 3,
            "updatedCells": 5,
        },
    }
    sheet = ss.sheets[0]
    # INSERT_ROWS adds rows for the data even though row 3 was empty.
    assert (sheet.row_count, sheet.column_count) == (5, 3)
    assert sheet.rows() == [["name", "n"], ["a", 1], ["b", 2], ["c", 3, "extra"]]
    (request,) = mock_google.calls("/v4/spreadsheets/grow/values/", method="POST")
    assert request.query["valueInputOption"] == ["RAW"]
    assert request.query["insertDataOption"] == ["INSERT_ROWS"]


@pytest.mark.asyncio
async def test_append_overwrite_grows_only_when_needed(mock_google):
    ss = mock_google.sheets.add("ow", {"Sheet1": [["a"]]}, row_count=3, column_count=1)
    for _ in range(3):
        response = await call(
            mock_google,
            "POST",
            "/v4/spreadsheets/ow/values/Sheet1:append",
            params={"valueInputOption": "RAW"},
            json={"values": [["x"]]},
        )
        assert response.status_code == 200
    assert ss.sheets[0].row_count == 4
    assert ss.sheets[0].rows() == [["a"], ["x"], ["x"], ["x"]]


@pytest.mark.asyncio
async def test_append_rejects_unknown_insert_option(mock_google):
    response = await call(
        mock_google,
        "POST",
        "/v4/spreadsheets/students/values/students:append",
        params={"valueInputOption": "RAW", "insertDataOption": "SIDEWAYS"},
        json={"values": [["x"]]},
    )
    assert_google_error(response, 400, "INVALID_ARGUMENT")


@pytest.mark.asyncio
async def test_clear(mock_google):
    response = await call(
        mock_google, "POST", "/v4/spreadsheets/students/values/students:clear"
    )
    assert response.json() == {
        "spreadsheetId": "students",
        "clearedRange": "students!A1:Z1000",
    }
    data = await values(mock_google, "students", "students")
    assert "values" not in data
    # Other tabs are untouched.
    other = await values(mock_google, "students", "assignments")
    assert other["values"][0] == ["id", "title", "max_score"]


# --- spreadsheets.create ------------------------------------------------------


@pytest.mark.asyncio
async def test_create(mock_google):
    response = await call(
        mock_google,
        "POST",
        "/v4/spreadsheets",
        json={"properties": {"title": "Export"}},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["spreadsheetId"].startswith("mock")
    assert data["properties"]["title"] == "Export"
    assert data["sheets"] == [
        {
            "properties": {
                "sheetId": 0,
                "title": "Sheet1",
                "index": 0,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 1000, "columnCount": 26},
            }
        }
    ]
    # The creator can write to it.
    ss = mock_google.sheets.get(data["spreadsheetId"])
    assert ss.acl == {USER: WRITER}


@pytest.mark.asyncio
async def test_create_needs_write_scope(mock_google):
    response = await call(
        mock_google, "POST", "/v4/spreadsheets", json={}, scopes=(SCOPE_SHEETS_RO,)
    )
    assert_google_error(
        response, 403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
    )


# --- spreadsheets.batchUpdate -------------------------------------------------

BOLD_HEADER = {
    "repeatCell": {
        "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }
}
FREEZE_HEADER = {
    "updateSheetProperties": {
        "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }
}


async def batch_update(mock_google, ss_id, requests, **kw):
    return await call(
        mock_google,
        "POST",
        f"/v4/spreadsheets/{ss_id}:batchUpdate",
        json={"requests": requests},
        **kw,
    )


@pytest.mark.asyncio
async def test_batch_update_bold_and_frozen_header(mock_google):
    response = await batch_update(mock_google, "students", [BOLD_HEADER, FREEZE_HEADER])
    assert response.status_code == 200, response.text
    assert response.json() == {"spreadsheetId": "students", "replies": [{}, {}]}

    ss = mock_google.sheets.get("students")
    assert ss.batch_updates == [[BOLD_HEADER, FREEZE_HEADER]]
    body = BOLD_HEADER["repeatCell"]
    assert ss.sheets[0].formats == [(body["range"], body["cell"], body["fields"])]
    meta = await call(
        mock_google, "GET", "/v4/spreadsheets/students", params={"fields": "sheets"}
    )
    grids = [s["properties"]["gridProperties"] for s in meta.json()["sheets"]]
    assert grids[0]["frozenRowCount"] == 1
    assert "frozenRowCount" not in grids[1]


@pytest.mark.asyncio
async def test_batch_update_is_atomic(mock_google):
    bad = {"repeatCell": {**BOLD_HEADER["repeatCell"], "range": {"sheetId": 999}}}
    response = await batch_update(mock_google, "students", [FREEZE_HEADER, bad])
    assert_google_error(response, 400, "INVALID_ARGUMENT")
    assert "requests[1].repeatCell" in response.json()["error"]["message"]
    ss = mock_google.sheets.get("students")
    assert ss.sheets[0].frozen_row_count == 0  # the valid request wasn't applied
    assert ss.batch_updates == [[FREEZE_HEADER, bad]]  # but it was recorded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requests",
    [
        [],
        [{"addChart": {"fields": "*"}}],
        [{"updateSheetProperties": {"properties": {"sheetId": 0}}}],  # no fields
    ],
)
async def test_batch_update_rejects(mock_google, requests):
    response = await batch_update(mock_google, "students", requests)
    assert_google_error(response, 400, "INVALID_ARGUMENT")


@pytest.mark.asyncio
async def test_batch_update_needs_editor(mock_google):
    response = await batch_update(mock_google, "readonly", [FREEZE_HEADER])
    assert_google_error(response, 403, "PERMISSION_DENIED")


# --- access lists -------------------------------------------------------------

WRITES = [
    ("PUT", "/values/Sheet1!A1", {"valueInputOption": "RAW"}, {"values": [["x"]]}),
    (
        "POST",
        "/values/Sheet1:append",
        {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        {"values": [["x"]]},
    ),
    ("POST", "/values/Sheet1:clear", {}, None),
    (":batchUpdate", None, None, {"requests": [FREEZE_HEADER]}),
]


async def write(mock_google, ss_id, op, **kw):
    method, suffix, params, body = op
    if method == ":batchUpdate":
        method, suffix = "POST", ":batchUpdate"
    return await call(
        mock_google,
        method,
        f"/v4/spreadsheets/{ss_id}{suffix}",
        params=params,
        json=body,
        **kw,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op", WRITES, ids=["update", "append", "clear", "batch"])
async def test_read_only_share(mock_google, op):
    mock_google.sheets.add("shared", {"Sheet1": [["k"]]}, acl={SA_OTHER: READER})
    read = await call(mock_google, "GET", "/v4/spreadsheets/shared", as_=SA_OTHER)
    assert read.status_code == 200
    response = await write(mock_google, "shared", op, as_=SA_OTHER)
    assert_google_error(response, 403, "PERMISSION_DENIED")

    mock_google.sheets.share("shared", SA_OTHER, WRITER)
    response = await write(mock_google, "shared", op, as_=SA_OTHER)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_not_shared_then_shared_then_unshared(mock_google):
    mock_google.sheets.add("sales", {"Sheet1": [["k"]]}, acl={})
    path = "/v4/spreadsheets/sales/values/Sheet1"
    assert_google_error(
        await call(mock_google, "GET", path, as_=SA_TEST), 403, "PERMISSION_DENIED"
    )
    mock_google.sheets.share("sales", SA_TEST, READER)
    assert (await call(mock_google, "GET", path, as_=SA_TEST)).status_code == 200
    mock_google.sheets.unshare("sales", SA_TEST)
    assert (await call(mock_google, "GET", path, as_=SA_TEST)).status_code == 403


@pytest.mark.asyncio
async def test_deleted_spreadsheet_is_404(mock_google):
    mock_google.sheets.delete("students")
    for method, path in [
        ("GET", "/v4/spreadsheets/students"),
        ("GET", "/v4/spreadsheets/students/values/students"),
        ("POST", "/v4/spreadsheets/students:batchUpdate"),
    ]:
        response = await call(mock_google, method, path, json={"requests": []})
        assert_google_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_real_scope_check_gives_scope_insufficient(mock_google):
    response = await call(
        mock_google,
        "GET",
        "/v4/spreadsheets/students",
        scopes=(SCOPE_OPENID, SCOPE_EMAIL),
    )
    assert_google_error(
        response, 403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
    )
    assert "insufficient_scope" in response.headers["www-authenticate"]


# --- fault injection ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail,code,status,reason",
    [
        ({"reason": "SERVICE_DISABLED"}, 403, "PERMISSION_DENIED", "SERVICE_DISABLED"),
        (
            {"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"},
            403,
            "PERMISSION_DENIED",
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        ),
        ({"status": 403}, 403, "PERMISSION_DENIED", None),
        ({"status": 404}, 404, "NOT_FOUND", None),
        ({"status": 429}, 429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"),
        (
            {"reason": "RATE_LIMIT_EXCEEDED"},
            429,
            "RESOURCE_EXHAUSTED",
            "RATE_LIMIT_EXCEEDED",
        ),
        ({"status": 500}, 500, "INTERNAL", None),
    ],
)
async def test_injected_errors(mock_google, fail, code, status, reason):
    mock_google.faults.fail("/v4/spreadsheets/", **fail)
    path = "/v4/spreadsheets/students/values/students"
    response = await call(mock_google, "GET", path)
    assert_google_error(response, code, status, reason)
    # One-shot by default; the request is logged with the injected status.
    assert (await call(mock_google, "GET", path)).status_code == 200
    assert [r.status for r in mock_google.calls(path)] == [code, 200]


@pytest.mark.asyncio
async def test_service_disabled_metadata(mock_google):
    mock_google.faults.fail("/v4/", reason="SERVICE_DISABLED")
    response = await call(mock_google, "GET", "/v4/spreadsheets/students")
    (info,) = response.json()["error"]["details"]
    assert info["metadata"]["service"] == "sheets.googleapis.com"
    assert info["metadata"]["consumer"].startswith("projects/")


def test_fault_reason_and_status_must_agree(mock_google):
    with pytest.raises(ValueError):
        mock_google.faults.fail("/v4/", 500, reason="SERVICE_DISABLED")
    with pytest.raises(ValueError):
        mock_google.faults.fail("/v4/", reason="NOT_A_REASON")
    with pytest.raises(TypeError):
        mock_google.faults.fail("/v4/")


# --- Datasette + google-auth + this plugin ------------------------------------


@pytest.mark.asyncio
async def test_datasette_is_wired_to_the_mock(datasette, mock_google):
    response = await datasette.client.get("/-/plugins.json")
    names = {p["name"] for p in response.json()}
    assert {SHEETS_PLUGIN, "datasette-google-auth", "datasette-cron"} <= names
    config = datasette.plugin_config("datasette-google-auth")
    assert config["google_base_urls"] == mock_google.plugin_config()["google_base_urls"]
    assert config["encryption-key"]


@pytest.mark.asyncio
async def test_make_datasette_passes_our_plugin_config(mock_google):
    datasette = await make_datasette(mock_google, sheets_config={"preview_rows": 7})
    assert datasette.plugin_config(SHEETS_PLUGIN) == {"preview_rows": 7}


@pytest.mark.asyncio
async def test_service_account_reads_shared_sheet_only(datasette, sa_credential):
    info = await sa_credential("alice")
    assert (info.type, info.google_email, info.is_owner) == (
        "service_account",
        SA_TEST,
        True,
    )
    cred = await get_credential(
        datasette, info.id, actor=ALICE, scopes=[SCOPE_SHEETS_RO]
    )
    shared = await cred.request("GET", f"{SHEETS_BASE}/v4/spreadsheets/students")
    assert shared.status_code == 200
    assert shared.json()["properties"]["title"] == "Students"
    private = await cred.request("GET", f"{SHEETS_BASE}/v4/spreadsheets/private")
    assert_google_error(private, 403, "PERMISSION_DENIED")


@pytest.mark.asyncio
async def test_service_account_access_follows_the_acl(
    datasette, mock_google, sa_credential
):
    info = await sa_credential("bob", key="other", label="Other")
    assert (info.label, info.google_email) == ("Other", SA_OTHER)
    cred = await get_credential(datasette, info.id, actor=BOB, scopes=[SCOPE_SHEETS])
    url = f"{SHEETS_BASE}/v4/spreadsheets/students/values/students:append"
    params = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    body = {"values": [[6, "Fay"]]}

    denied = await cred.request("POST", url, params=params, json=body)
    assert_google_error(denied, 403, "PERMISSION_DENIED")
    mock_google.sheets.share("students", SA_OTHER, WRITER)
    allowed = await cred.request("POST", url, params=params, json=body)
    assert allowed.status_code == 200
    mock_google.sheets.delete("students")
    gone = await cred.request("POST", url, params=params, json=body)
    assert_google_error(gone, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_oauth_credential(datasette, mock_google, oauth_credential):
    info = await oauth_credential("alice")
    assert (info.type, info.google_email) == ("google_oauth", USER)
    assert SCOPE_SHEETS in info.scopes
    cred = await get_credential(datasette, info.id, actor=ALICE, scopes=[SCOPE_SHEETS])
    read = await cred.request(
        "GET", f"{SHEETS_BASE}/v4/spreadsheets/readonly/values/Sheet1"
    )
    assert read.json()["values"] == [["k", "v"], ["a", "1"]]
    write = await cred.request(
        "POST",
        f"{SHEETS_BASE}/v4/spreadsheets/readonly/values/Sheet1:clear",
    )
    assert_google_error(write, 403, "PERMISSION_DENIED")


@pytest.mark.asyncio
async def test_oauth_credential_other_user_and_partial_consent(
    datasette, oauth_credential
):
    other = GoogleUser("100000000000000000002", "other@example.com")
    info = await oauth_credential(
        "bob", user=other, granted_scopes={SCOPE_OPENID, SCOPE_EMAIL}
    )
    assert info.google_email == "other@example.com"
    assert SCOPE_SHEETS not in info.scopes
    with pytest.raises(MissingScopes):
        await get_credential(datasette, info.id, actor=BOB, scopes=[SCOPE_SHEETS])
