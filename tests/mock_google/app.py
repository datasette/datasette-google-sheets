"""The mock Google server: a FastAPI app wrapped in a request recorder.

    app = create_app()          # fresh state, fixture service accounts registered
    app.state.requests          # every request, in order (RecordedRequest)
    app.state.oauth.deny = True # knobs: see oauth.py, faults.py, tokens.py

Endpoints (paths as Google has them; the host is ignored except for the
allow-list check below):

    GET  /o/oauth2/v2/auth                     consent screen (auto-approves)
    POST /token                                jwt-bearer, authorization_code, refresh_token
    POST /revoke                               revoke a refresh or access token
    GET  /v1/userinfo                          OpenID userinfo
    GET  /v4/spreadsheets/{id}                 spreadsheet metadata (``fields=``)
    POST /v4/spreadsheets                      create a spreadsheet
    POST /v4/spreadsheets/{id}:batchUpdate     repeatCell, updateSheetProperties
    GET  /v4/spreadsheets/{id}/values/{range}  values.get (``valueRenderOption``,
                                               ``dateTimeRenderOption``)
    PUT  /v4/spreadsheets/{id}/values/{range}  values.update
    POST /v4/spreadsheets/{id}/values/{range}:append   (``insertDataOption``)
    POST /v4/spreadsheets/{id}/values/{range}:clear

Requests to a host outside ``MockState.allowed_hosts`` get a 421 (and are
logged), so a URL that doesn't come from config fails loudly instead of
quietly reaching the mock.
"""

from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import MOCK_HOST, SHEETS_HOST, errors
from .faults import Faults
from .keys import (
    SA_TOKEN_500,
    SA_UNREGISTERED,
    ServiceAccountKey,
    fixture_service_accounts,
)
from .oauth import (
    SCOPE_DRIVE,
    SCOPE_DRIVE_FILE,
    SCOPE_DRIVE_RO,
    SCOPE_SHEETS,
    SCOPE_SHEETS_RO,
    OAuthServer,
)
from .sheets import (
    WRITER,
    BatchUpdateError,
    FieldMaskError,
    Formatted,
    GridLimitError,
    RangeError,
    Spreadsheet,
    SpreadsheetStore,
    apply_fields,
    parse_fields,
)
from .tokens import JWT_BEARER, Principal, TokenError, TokenStore

OAUTH_PATHS = ("/token", "/revoke", "/o/oauth2/")

READ_SCOPES = frozenset({SCOPE_SHEETS, SCOPE_SHEETS_RO, SCOPE_DRIVE, SCOPE_DRIVE_RO})
WRITE_SCOPES = frozenset({SCOPE_SHEETS, SCOPE_DRIVE})
VALUE_INPUT_OPTIONS = ("RAW", "USER_ENTERED")
# Google's defaults first (spreadsheets.values.get reference).
VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
DATETIME_RENDER_OPTIONS = ("SERIAL_NUMBER", "FORMATTED_STRING")
INSERT_DATA_OPTIONS = ("OVERWRITE", "INSERT_ROWS")


@dataclass
class RecordedRequest:
    method: str
    host: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    form: dict[str, str] | None = None
    json: Any = None
    status: int | None = None

    @property
    def url(self) -> str:
        return f"{self.host}{self.path}"


@dataclass
class MockState:
    requests: list[RecordedRequest] = field(default_factory=list)
    allowed_hosts: set[str] = field(default_factory=lambda: {MOCK_HOST, SHEETS_HOST})
    faults: Faults = field(default_factory=Faults)
    tokens: TokenStore = field(default_factory=TokenStore)
    sheets: SpreadsheetStore = field(default_factory=SpreadsheetStore)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.oauth = OAuthServer(self.tokens)

    def calls(
        self,
        path: str | None = None,
        *,
        method: str | None = None,
        host: str | None = None,
    ) -> list[RecordedRequest]:
        """Recorded requests, optionally filtered (``path`` is a prefix)."""
        with self.lock:
            requests = list(self.requests)
        return [
            r
            for r in requests
            if (path is None or r.path.startswith(path))
            and (method is None or r.method == method.upper())
            and (host is None or r.host == host)
        ]


class RecorderMiddleware:
    """Raw ASGI wrapper: logs each request (with its form or JSON body and
    final status), enforces the host allow-list and applies injected faults."""

    def __init__(self, app: FastAPI, state: MockState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope["headers"]
        }
        query: dict[str, list[str]] = {}
        raw_query = scope.get("query_string", b"").decode("latin-1")
        for k, v in parse_qsl(raw_query, keep_blank_values=True):
            query.setdefault(k, []).append(v)
        host = headers.get("host", "").rsplit(":", 1)[0]
        entry = RecordedRequest(scope["method"], host, scope["path"], query, headers)
        with self.state.lock:
            self.state.requests.append(entry)

        body = bytearray()

        async def recv():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def snd(message):
            if message["type"] == "http.response.start":
                entry.status = message["status"]
                _record_body(entry, headers, bytes(body))
            await send(message)

        if host not in self.state.allowed_hosts:
            response: Response = JSONResponse(
                {"error": f"mock_google: unexpected host {host!r}"}, status_code=421
            )
            return await response(scope, recv, snd)
        injected = self.state.faults.take(scope["method"], scope["path"])
        if injected is not None:
            # Drain the body so the log still shows the form.
            while (await recv()).get("more_body"):
                pass
            if scope["path"].startswith(OAUTH_PATHS):
                response = errors.oauth_error(
                    "internal_failure", "mock_google: injected fault", injected.status
                )
            else:
                response = errors.for_status(injected.status, injected.reason)
            return await response(scope, recv, snd)
        await self.app(scope, recv, snd)


def _record_body(entry: RecordedRequest, headers: dict[str, str], body: bytes) -> None:
    content_type = headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        entry.form = {
            k: v[-1] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()
        }
    elif content_type.startswith("application/json") and body:
        try:
            entry.json = json.loads(body)
        except ValueError:
            entry.json = None


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, media_type=errors.JSON_UTF8)


def _bearer(request: Request, state: MockState) -> Principal | Response:
    auth = request.headers.get("authorization")
    if auth is None:
        return errors.missing_credentials()
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return errors.unauthenticated()
    principal = state.tokens.lookup(token.strip())
    if principal is None:
        return errors.unauthenticated()
    return principal


def _spreadsheet(
    request: Request, state: MockState, spreadsheet_id: str, *, write: bool
) -> tuple[Principal, Spreadsheet] | Response:
    """Authenticate, then check scope and sharing, in Google's order."""
    principal = _bearer(request, state)
    if isinstance(principal, Response):
        return principal
    if write:
        ss = state.sheets.get(spreadsheet_id)
        drive_file_ok = (
            ss is not None
            and SCOPE_DRIVE_FILE in principal.scopes
            and ss.created_by == principal.name
        )
        if not (principal.scopes & WRITE_SCOPES or drive_file_ok):
            return errors.insufficient_scope(SCOPE_SHEETS)
    elif not principal.scopes & READ_SCOPES:
        return errors.insufficient_scope(SCOPE_SHEETS_RO)
    ss = state.sheets.get(spreadsheet_id)
    if ss is None:
        return errors.not_found()
    role = ss.acl.get(principal.name)
    if role is None or (write and role != WRITER):
        return errors.permission_denied()
    return principal, ss


async def _form(request: Request) -> dict[str, str] | Response:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return errors.oauth_error(
            "invalid_request", "Invalid Content-Type: " + content_type
        )
    raw = (await request.body()).decode()
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def create_app(
    service_accounts: list[ServiceAccountKey] | None = None,
) -> RecorderMiddleware:
    """A fresh mock. ``service_accounts`` defaults to the session's fixture
    accounts (all but ``unregistered``). The result has ``.state``."""
    state = MockState()
    if service_accounts is None:
        service_accounts = [
            sa
            for sa in fixture_service_accounts().values()
            if sa.client_email != SA_UNREGISTERED
        ]
    for sa in service_accounts:
        state.tokens.register_service_account(sa.client_email, sa.public_key)

    kwargs: dict[str, Any] = {}
    if "telemetry" in inspect.signature(FastAPI).parameters:
        # FastAPI >= 0.142 traces itself. The mock stands in for Google, so
        # its server spans aren't Datasette's: keep them out of the telemetry
        # tests (and the privacy walk).
        kwargs["telemetry"] = {
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        }
    app = FastAPI(
        title="mock_google", docs_url=None, redoc_url=None, openapi_url=None, **kwargs
    )

    # --- OAuth ---------------------------------------------------------------

    @app.get("/o/oauth2/v2/auth")
    async def authorize(request: Request):
        try:
            location = state.oauth.authorize(dict(request.query_params))
        except TokenError as e:
            return errors.oauth_error(e.error, e.description, e.status)
        return RedirectResponse(location, status_code=302)

    @app.post("/token")
    async def token(request: Request):
        form = await _form(request)
        if isinstance(form, Response):
            return form
        grant_type = form.get("grant_type")
        try:
            if grant_type == JWT_BEARER:
                assertion = form.get("assertion")
                if not assertion:
                    raise TokenError("invalid_request", "Bad Request")
                principal = state.tokens.jwt_bearer(assertion)
                if state.faults.token_500_once(principal.name, SA_TOKEN_500):
                    return errors.oauth_error(
                        "internal_failure", "Internal error.", 500
                    )
                return _json(state.tokens.issue(principal))
            if grant_type == "authorization_code":
                return _json(state.oauth.exchange_code(form))
            if grant_type == "refresh_token":
                return _json(state.oauth.refresh(form))
            raise TokenError(
                "unsupported_grant_type", "Invalid grant_type: " + (grant_type or "")
            )
        except TokenError as e:
            return errors.oauth_error(e.error, e.description, e.status)

    @app.post("/revoke")
    async def revoke(request: Request):
        form: dict[str, str] = {}
        if request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            parsed = await _form(request)
            if isinstance(parsed, dict):
                form = parsed
        token_value = form.get("token") or request.query_params.get("token")
        if not token_value:
            return errors.oauth_error(
                "invalid_request", "Missing required parameter: token"
            )
        try:
            state.oauth.revoke(token_value)
        except TokenError as e:
            return errors.oauth_error(e.error, e.description, e.status)
        return _json({})

    @app.get("/v1/userinfo")
    async def userinfo(request: Request):
        principal = _bearer(request, state)
        if isinstance(principal, Response):
            return errors.oauth_error("invalid_token", "Invalid Credentials", 401)
        info = state.oauth.userinfo(principal)
        if info is None:
            return errors.oauth_error(
                "insufficient_scope", "The openid scope is required.", 403
            )
        return _json(info)

    # --- Sheets v4 -------------------------------------------------------------

    @app.post("/v4/spreadsheets")
    async def create_spreadsheet(request: Request):
        principal = _bearer(request, state)
        if isinstance(principal, Response):
            return principal
        if not principal.scopes & (WRITE_SCOPES | {SCOPE_DRIVE_FILE}):
            return errors.insufficient_scope(SCOPE_SHEETS)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        title = (body.get("properties") or {}).get("title") or "Untitled spreadsheet"
        sheet_titles = [
            s["properties"]["title"]
            for s in body.get("sheets") or []
            if (s.get("properties") or {}).get("title")
        ]
        ss = state.sheets.create(title, sheet_titles, principal.name)
        return _json(ss.metadata_json())

    @app.get("/v4/spreadsheets/{spreadsheet_id}")
    async def get_spreadsheet(spreadsheet_id: str, request: Request):
        found = _spreadsheet(request, state, spreadsheet_id, write=False)
        if isinstance(found, Response):
            return found
        if request.query_params.get("includeGridData", "false").lower() == "true":
            return errors.invalid_argument(
                "mock_google: includeGridData is not supported"
            )
        data = found[1].metadata_json()
        mask = request.query_params.get("fields")
        if mask is not None:
            try:
                data = apply_fields(data, parse_fields(mask))
            except FieldMaskError:
                return errors.invalid_argument(f"Invalid field selection {mask}")
        return _json(data)

    @app.post("/v4/spreadsheets/{spreadsheet_id}:batchUpdate")
    async def batch_update(spreadsheet_id: str, request: Request):
        found = _spreadsheet(request, state, spreadsheet_id, write=True)
        if isinstance(found, Response):
            return found
        _, ss = found
        try:
            body = await request.json()
        except ValueError:
            body = None
        requests = body.get("requests") if isinstance(body, dict) else None
        if not isinstance(requests, list):
            return errors.invalid_argument("mock_google: body must have requests")
        try:
            replies = ss.batch_update(requests)
        except BatchUpdateError as e:
            return errors.invalid_argument(str(e))
        return _json({"spreadsheetId": ss.spreadsheet_id, "replies": replies})

    @app.api_route(
        "/v4/spreadsheets/{spreadsheet_id}/values/{target:path}",
        methods=["GET", "PUT", "POST"],
    )
    async def values(spreadsheet_id: str, target: str, request: Request):
        method = request.method
        range_, operation = target, None
        if method == "POST":
            range_, _, operation = target.rpartition(":")
            if operation not in ("append", "clear"):
                return errors.not_found()
        found = _spreadsheet(request, state, spreadsheet_id, write=method != "GET")
        if isinstance(found, Response):
            return found
        _, ss = found
        params = request.query_params
        value_option = params.get("valueRenderOption", VALUE_RENDER_OPTIONS[0])
        datetime_option = params.get("dateTimeRenderOption", DATETIME_RENDER_OPTIONS[0])
        if method == "GET":
            if value_option not in VALUE_RENDER_OPTIONS:
                return errors.invalid_argument(
                    f"Invalid valueRenderOption: {value_option}"
                )
            if datetime_option not in DATETIME_RENDER_OPTIONS:
                return errors.invalid_argument(
                    f"Invalid dateTimeRenderOption: {datetime_option}"
                )
        try:
            sheet, box = ss.resolve(range_)
            if method == "GET":
                range_a1, rows = sheet.read(box)
                result: dict = {"range": range_a1, "majorDimension": "ROWS"}
                if rows:
                    result["values"] = _render(rows, value_option, datetime_option)
                return _json(result)
            if operation == "clear":
                cleared = sheet.clear(box)
                return _json(
                    {"spreadsheetId": ss.spreadsheet_id, "clearedRange": cleared}
                )
        except RangeError:
            return errors.unable_to_parse_range(range_)
        except GridLimitError as e:
            return errors.invalid_argument(str(e))

        # values.update / values.append
        option = request.query_params.get("valueInputOption")
        if option is None:
            return errors.invalid_argument(
                "'valueInputOption' is required but not specified"
            )
        if option not in VALUE_INPUT_OPTIONS:
            return errors.invalid_argument(f"Invalid valueInputOption: {option}")
        try:
            body = await request.json()
        except ValueError:
            return errors.invalid_argument("mock_google: body must be a ValueRange")
        if body.get("majorDimension", "ROWS") != "ROWS":
            return errors.invalid_argument("mock_google: only majorDimension=ROWS")
        rows = body.get("values") or []
        if operation == "append":
            insert = params.get("insertDataOption", INSERT_DATA_OPTIONS[0])
            if insert not in INSERT_DATA_OPTIONS:
                return errors.invalid_argument(f"Invalid insertDataOption: {insert}")
            table = None
            if sheet.cells:
                table = sheet.a1(0, 0, sheet.used_rows(), sheet.used_columns())
            if insert == "INSERT_ROWS":
                # "Rows are inserted for the new data": the grid grows by the
                # appended height even if it had empty rows (nothing sits
                # below the table in this model, so no cells shift).
                sheet.row_count += len(rows)
            updates = sheet.write(sheet.used_rows(), box.c0, rows)
            result = {
                "spreadsheetId": ss.spreadsheet_id,
                "updates": {"spreadsheetId": ss.spreadsheet_id, **updates},
            }
            if table is not None:
                result["tableRange"] = table
            return _json(result)
        # A single-cell range is just the start; a larger one is a limit.
        height = len(rows)
        width = max((len(row) for row in rows), default=0)
        single_cell = (box.r1, box.c1) == (box.r0 + 1, box.c0 + 1)
        if not single_cell and (
            (box.r1 is not None and box.r0 + height > box.r1)
            or (box.c1 is not None and box.c0 + width > box.c1)
        ):
            return errors.invalid_argument(
                f"Requested writing within range [{range_}], but tried writing "
                "beyond it"
            )
        return _json(
            {"spreadsheetId": ss.spreadsheet_id, **sheet.write(box.r0, box.c0, rows)}
        )

    wrapper = RecorderMiddleware(app, state)
    return wrapper


def _render(
    rows: list[list[Any]], value_option: str, datetime_option: str
) -> list[list[Any]]:
    """``valueRenderOption``: FORMATTED_VALUE (the default) turns every cell
    into display text; UNFORMATTED_VALUE and FORMULA return stored values.
    ``dateTimeRenderOption`` (default SERIAL_NUMBER) applies to date cells
    and "is ignored if valueRenderOption is FORMATTED_VALUE" (values.get
    reference)."""
    return [[_cell(v, value_option, datetime_option) for v in row] for row in rows]


def _cell(value: Any, value_option: str, datetime_option: str) -> Any:
    if isinstance(value, Formatted):
        return value.render(value_option, datetime_option)
    if value_option == "FORMATTED_VALUE":
        return _formatted(value)
    return value


def _formatted(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
