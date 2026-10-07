"""JSON API routes on the shared router (ticket 13), under
``/-/google-sheets/api/``.

Pydantic models in and out (``models.py``); the router turns them into the
OpenAPI document (``just openapi``). Handlers are thin: the rules live in
``service.py``. Every route needs a signed-in actor and checks permissions
as that actor. Links are owner-only; admins may view, pause, resume,
unlink and delete but never run or edit (D15). Someone else's link is the
same 404 as an unknown id, except to admins, who get a 403 when they try to
operate it.

Errors are ``{ok: false, error, code, ...}``, google-credentials's
``error_response`` shape. ``api_errors`` maps ``ApiError``,
``ScheduleError``, ``GoogleCredentialsError`` (via google-credentials's own
``error_response``) and Sheets/transport errors; nothing escapes a
handler, so no message reaches core's request span. A malformed body is the
router's 400 (``{error, errors}``).

CSRF: Datasette (>=1.0a41) checks ``Sec-Fetch-Site`` / ``Origin`` on every
POST before any route runs, so there is no token (google-credentials D30). Bodies
are capped at ``router.MAX_BODY_BYTES``.
"""

# No `from __future__ import annotations`: datasette-plugin-router inspects
# real annotation objects (Annotated[..., Body()], str) at decoration time.
import functools
import logging
from typing import Annotated, Any

import httpx2
from datasette import Response
from datasette_google_credentials import (
    GoogleCredentialsError,
    connect_url,
    error_response,
    get_credential,
    list_credentials,
)
from datasette_plugin_router import Body
from pydantic import BaseModel

from .. import service
from ..config import get_config
from ..importer import ImporterError, preview
from ..internal_db import InternalDB
from ..models import (
    ConvertToSyncedRequest,
    CreateLinkRequest,
    CredentialOption,
    CredentialsResponse,
    DeleteResponse,
    InspectRequest,
    InspectResponse,
    LinkInfo,
    LinkListResponse,
    LinkRunResponse,
    MappingRequest,
    PreviewRequest,
    PreviewResponse,
    ResumeRequest,
    RunListResponse,
    RunOutcomeInfo,
    RunRequest,
    SettingsRequest,
    StatusResponse,
    TabInfo,
)
from ..permissions import can_schedule, is_admin
from ..router import router
from ..runner import RunRefused, run_link
from ..schedule import ScheduleError, internal_db_is_persistent
from ..service import ApiError, require_actor
from ..sheets import SheetsError, get_tabs, parse_sheet_url, spreadsheet_url

logger = logging.getLogger(__name__)

API = r"^/-/google-sheets/api"
LINK = API + r"/links/(?P<link_id>[^/]+)"

_STATUSES = ("ok", "error", "paused")
_DIRECTIONS = ("import", "export")
_BOOLS = {"1": True, "true": True, "0": False, "false": False}


def api_errors(fn):
    """Turn every failure into a JSON error. ``functools.wraps`` keeps the
    handler's signature (``__wrapped__``), which the router reads."""

    @functools.wraps(fn)
    async def handler(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ApiError as error:
            return error.response()
        except ScheduleError as error:
            return ApiError(error.code, error.message, error.status).response()
        except GoogleCredentialsError as error:
            return error_response(error)
        except SheetsError as error:
            return ApiError(
                "sheets_error",
                f"Google Sheets returned an error (HTTP {error.status}).",
                502,
            ).response()
        except httpx2.TransportError:
            # Its text can carry the request URL (a spreadsheet id).
            return ApiError(
                "network", "The connection to Google failed.", 502
            ).response()
        except Exception as error:
            logger.error(
                "datasette-google-sheets: unexpected %s in %s",
                type(error).__name__,
                fn.__name__,
            )
            return ApiError(
                "internal_error", "Something went wrong. Try again.", 500
            ).response()

    return handler


def _json(model: BaseModel) -> Response:
    return Response.json(model.model_dump(mode="json"))


def _arg(request, name: str, allowed: tuple[str, ...]) -> str | None:
    value = request.args.get(name) or None
    if value is not None and value not in allowed:
        raise ApiError(
            f"invalid_{name}", f"{name} must be one of: {', '.join(allowed)}."
        )
    return value


# --- Setup ---------------------------------------------------------------------


@router.GET(API + r"/status$", output=StatusResponse)
@api_errors
async def api_status(datasette, request):
    actor = require_actor(request.actor)
    config = get_config(datasette)
    return _json(
        StatusResponse(
            internal_db_persistent=internal_db_is_persistent(datasette),
            oauth_configured=service.oauth_configured(datasette),
            can_schedule=await can_schedule(datasette, actor),
            is_admin=await is_admin(datasette, actor),
            min_interval_minutes=config.min_interval_minutes,
            default_interval_minutes=config.default_interval_minutes,
        )
    )


@router.GET(API + r"/credentials$", output=CredentialsResponse)
@api_errors
async def api_credentials(datasette, request):
    """``?direction=import|export`` (required), ``?return_to=/path``."""
    actor = require_actor(request.actor)
    direction = _arg(request, "direction", _DIRECTIONS)
    if direction is None:
        raise ApiError("invalid_direction", "direction must be import or export.")
    credentials = await list_credentials(
        datasette, actor=actor, scopes=[service.scope_for(direction)]
    )
    return_to = service.connect_return_to(datasette, request.args.get("return_to"))
    return _json(
        CredentialsResponse(
            credentials=[
                CredentialOption(
                    id=c.id,
                    type=c.type,
                    label=c.label,
                    google_email=c.google_email,
                    status=c.status,
                    status_detail=c.status_detail,
                )
                for c in credentials
            ],
            connect_url=connect_url(datasette, return_to=return_to),
        )
    )


@router.POST(API + r"/inspect$", output=InspectResponse)
@api_errors
async def api_inspect(datasette, request, body: Annotated[InspectRequest, Body()]):
    actor = require_actor(request.actor)
    try:
        spreadsheet_id, gid = parse_sheet_url(body.url)
    except ValueError as error:
        raise ApiError("invalid_url", str(error)) from None
    cred = await get_credential(
        datasette,
        body.credential_id,
        actor=actor,
        scopes=[service.scope_for(body.direction)],
    )
    try:
        title, tabs = await get_tabs(cred, spreadsheet_id)
    except SheetsError as error:
        raise service.sheets_api_error(error, cred, body.direction) from None
    return _json(
        InspectResponse(
            spreadsheet_id=spreadsheet_id,
            spreadsheet_title=title,
            spreadsheet_url=spreadsheet_url(spreadsheet_id, gid),
            tabs=[TabInfo.from_tab(tab) for tab in tabs],
            gid=gid,
        )
    )


@router.POST(API + r"/preview$", output=PreviewResponse)
@api_errors
async def api_preview(datasette, request, body: Annotated[PreviewRequest, Body()]):
    actor = require_actor(request.actor)
    config = get_config(datasette)
    cred = await get_credential(
        datasette,
        body.credential_id,
        actor=actor,
        scopes=[service.scope_for("import")],
    )
    try:
        result = await preview(
            cred,
            body.spreadsheet_id,
            body.gid,
            headers_row=body.headers_row,
            rows=config.preview_rows,
            max_cells=config.max_import_cells,
        )
    except SheetsError as error:
        raise service.sheets_api_error(error, cred, "import") from None
    except ImporterError as error:
        if error.code == "too_large":
            data = error.data or {}
            return _json(
                PreviewResponse(
                    too_large=True,
                    message=error.message,
                    cells=data.get("cells", 0),
                    max_cells=config.max_import_cells,
                )
            )
        raise ApiError(
            error.code, error.message, 404 if error.code == "tab_missing" else 400
        ) from None
    return _json(
        PreviewResponse(
            too_large=False,
            cells=result.cells,
            max_cells=config.max_import_cells,
            spreadsheet_title=result.spreadsheet_title,
            tab=TabInfo.from_tab(result.tab),
            headers=result.headers,
            rows=[[record[h] for h in result.headers] for record in result.records],
            total_rows=result.total_rows,
            types=result.types,
            key=result.key,
        )
    )


# --- Links ---------------------------------------------------------------------


@router.GET(API + r"/links$", output=LinkListResponse)
@api_errors
async def api_links(datasette, request):
    """``?owner=`` (admins: an actor id, or ``*`` for everyone; default:
    mine), ``?direction=``, ``?status=``, ``?scheduled=1|0``."""
    actor = require_actor(request.actor)
    admin = await is_admin(datasette, actor)
    me = str(actor["id"])
    owner = request.args.get("owner") or me
    if owner != me and not admin:
        raise ApiError(
            "forbidden", "Only administrators can list other people's links.", 403
        )
    direction = _arg(request, "direction", _DIRECTIONS)
    status = _arg(request, "status", _STATUSES)
    scheduled_arg = request.args.get("scheduled") or None
    if scheduled_arg is not None and scheduled_arg not in _BOOLS:
        raise ApiError("invalid_scheduled", "scheduled must be 1 or 0.")
    links = await InternalDB.for_datasette(datasette).list_links(
        owner_id=None if owner == "*" else owner
    )
    links = [
        link
        for link in links
        if (direction is None or link.direction == direction)
        and (status is None or link.status == status)
        and (scheduled_arg is None or link.scheduled == _BOOLS[scheduled_arg])
    ]
    return _json(
        LinkListResponse(
            links=await service.link_infos(datasette, actor, links, admin=admin)
        )
    )


@router.POST(API + r"/links$", output=LinkRunResponse)
@api_errors
async def api_create_link(
    datasette, request, body: Annotated[CreateLinkRequest, Body()]
):
    """Create a link, then run it once as the actor. A failed first run
    keeps the link, with its status (the outcome says why)."""
    actor = require_actor(request.actor)
    link = await service.create_link(datasette, actor, body)
    outcome = await run_link(datasette, link.id, trigger="manual", actor=actor)
    return await _link_run(datasette, actor, link.id, outcome)


async def _link_run(datasette, actor: dict[str, Any], link_id: str, outcome):
    link, admin = await service.load_link(datasette, actor, link_id)
    return _json(
        LinkRunResponse(
            link=await service.link_info(datasette, actor, link, admin=admin),
            run=RunOutcomeInfo.from_outcome(outcome),
        )
    )


@router.GET(LINK + r"$", output=LinkInfo)
@api_errors
async def api_link(datasette, request, link_id: str):
    actor = require_actor(request.actor)
    link, admin = await service.load_link(datasette, actor, link_id)
    return _json(await service.link_info(datasette, actor, link, admin=admin))


@router.GET(LINK + r"/runs$", output=RunListResponse)
@api_errors
async def api_link_runs(datasette, request, link_id: str):
    """``?limit=`` (default 20, at most ``runs_retain_per_link``)."""
    actor = require_actor(request.actor)
    link, _ = await service.load_link(datasette, actor, link_id)
    most = get_config(datasette).runs_retain_per_link
    raw = request.args.get("limit") or "20"
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if not 1 <= limit <= most:
        raise ApiError("invalid_limit", f"limit must be between 1 and {most}.")
    runs = await InternalDB.for_datasette(datasette).list_runs(link.id, limit=limit)
    return _json(RunListResponse(runs=runs))


@router.POST(LINK + r"/run$", output=LinkRunResponse)
@api_errors
async def api_run(
    datasette, request, link_id: str, body: Annotated[RunRequest, Body()]
):
    actor = require_actor(request.actor)
    link, admin = await service.load_link(datasette, actor, link_id)
    service.require_operate(actor, link, "run it")
    try:
        outcome = await run_link(
            datasette, link.id, trigger="manual", actor=actor, force=body.force
        )
    except RunRefused as refused:
        if refused.code == "paused":
            raise ApiError("paused", refused.message, 409) from None
        if refused.code == "not_owner" and admin:
            raise ApiError("forbidden", refused.message, 403) from None
        raise ApiError("not_found", "Link not found", 404) from None
    return await _link_run(datasette, actor, link.id, outcome)


async def _manageable(datasette, request, link_id: str):
    actor = require_actor(request.actor)
    link, admin = await service.load_link(datasette, actor, link_id)
    service.require_manage(actor, link, admin=admin)
    return actor, link, admin


async def _info(datasette, actor, link, admin: bool) -> Response:
    return _json(await service.link_info(datasette, actor, link, admin=admin))


@router.POST(LINK + r"/pause$", output=LinkInfo)
@api_errors
async def api_pause(datasette, request, link_id: str):
    actor, link, admin = await _manageable(datasette, request, link_id)
    link = await service.pause_link(datasette, actor, link)
    return await _info(datasette, actor, link, admin)


@router.POST(LINK + r"/resume$", output=LinkInfo)
@api_errors
async def api_resume(
    datasette, request, link_id: str, body: Annotated[ResumeRequest, Body()]
):
    actor, link, admin = await _manageable(datasette, request, link_id)
    link = await service.resume_link(datasette, link, recreate=body.recreate)
    return await _info(datasette, actor, link, admin)


@router.POST(LINK + r"/unlink$", output=LinkInfo)
@api_errors
async def api_unlink(datasette, request, link_id: str):
    actor, link, admin = await _manageable(datasette, request, link_id)
    link = await service.unlink(datasette, link)
    return await _info(datasette, actor, link, admin)


@router.POST(LINK + r"/delete$", output=DeleteResponse)
@api_errors
async def api_delete(datasette, request, link_id: str):
    _, link, _ = await _manageable(datasette, request, link_id)
    deleted = await service.delete_link(datasette, link)
    return _json(DeleteResponse(id=link.id, deleted=deleted))


async def _operable(datasette, request, link_id: str, what: str):
    actor = require_actor(request.actor)
    link, admin = await service.load_link(datasette, actor, link_id)
    service.require_operate(actor, link, what)
    return actor, link, admin


@router.POST(LINK + r"/settings$", output=LinkInfo)
@api_errors
async def api_settings(
    datasette, request, link_id: str, body: Annotated[SettingsRequest, Body()]
):
    actor, link, admin = await _operable(datasette, request, link_id, "change it")
    link = await service.update_settings(datasette, actor, link, body)
    return await _info(datasette, actor, link, admin)


@router.POST(LINK + r"/mapping$", output=LinkInfo)
@api_errors
async def api_mapping(
    datasette, request, link_id: str, body: Annotated[MappingRequest, Body()]
):
    actor, link, admin = await _operable(
        datasette, request, link_id, "change its mapping"
    )
    link = await service.update_mapping(datasette, actor, link, body.mapping)
    return await _info(datasette, actor, link, admin)


@router.POST(LINK + r"/convert-to-synced$", output=LinkInfo)
@api_errors
async def api_convert_to_synced(
    datasette, request, link_id: str, body: Annotated[ConvertToSyncedRequest, Body()]
):
    actor, link, admin = await _operable(datasette, request, link_id, "schedule it")
    link = await service.convert_to_synced(
        datasette, actor, link, body.interval_minutes
    )
    return await _info(datasette, actor, link, admin)
