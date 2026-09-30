"""Google-shaped error responses.

Two wire formats:

- Google APIs (Sheets, and userinfo's 403) use the ``google.rpc.Status``
  envelope: ``{"error": {"code", "message", "status", "details"?}}``.
- The OAuth endpoints (``/token``, ``/revoke``, ``/o/oauth2/v2/auth``) use the
  flat RFC 6749 shape: ``{"error": "invalid_grant", "error_description": "..."}``.

Sources for the ``google.rpc.Status`` shape (checked 2026-09-30):

- AIP-193 "Errors" (https://google.aip.dev/193, which is where
  https://cloud.google.com/apis/design/errors now redirects): the HTTP body is
  ``{"error": {"code": 429, "message": ..., "status": "RESOURCE_EXHAUSTED",
  "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
  "reason": ..., "domain": ..., "metadata": {...}}]}}``. ``status`` is the
  canonical code name. The machine-readable cause is ErrorInfo's ``reason``
  (UPPER_SNAKE_CASE, at most 63 characters).
- ``google/api/error_reason.proto`` (github.com/googleapis/googleapis):
  ``SERVICE_DISABLED``, ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` and
  ``RATE_LIMIT_EXCEEDED`` are ErrorInfo reasons with
  ``domain: "googleapis.com"`` and the metadata keys used below.

``PERMISSION_DENIED``, ``NOT_FOUND`` and ``RESOURCE_EXHAUSTED`` are ``status``
values, not reasons: a plain "not shared with you" 403 or a 404 carries no
ErrorInfo at all, and a quota 429 has status ``RESOURCE_EXHAUSTED`` with
reason ``RATE_LIMIT_EXCEEDED``. The ``message`` wording is illustrative only:
consumers must key off ``code``, ``status`` and ``reason``.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse

JSON_UTF8 = "application/json; charset=UTF-8"
ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
ERROR_DOMAIN = "googleapis.com"
SHEETS_SERVICE = "sheets.googleapis.com"
SCOPE_SHEETS = "https://www.googleapis.com/auth/spreadsheets"
# The mock's stand-in Cloud project, for the ``consumer`` metadata.
CONSUMER = "projects/123456789012"

# ErrorInfo reasons the mock can send (error_reason.proto).
SERVICE_DISABLED = "SERVICE_DISABLED"
ACCESS_TOKEN_SCOPE_INSUFFICIENT = "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"

UNAUTHENTICATED_MESSAGE = (
    "Request had invalid authentication credentials. Expected OAuth 2 access token, "
    "login cookie or other valid authentication credential. "
    "See https://developers.google.com/identity/sign-in/web/devconsole-project."
)
MISSING_CREDENTIALS_MESSAGE = (
    "Method doesn't allow unregistered callers (callers without established identity). "
    "Please use API Key or other form of API consumer identity to call this API."
)

# status code -> google.rpc status name, for injected faults.
STATUS_NAMES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


def google_error(
    code: int,
    status: str,
    message: str,
    reason: str | None = None,
    headers: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict = {"code": code, "message": message, "status": status}
    if reason is not None:
        info: dict = {"@type": ERROR_INFO, "reason": reason, "domain": ERROR_DOMAIN}
        if metadata:
            info["metadata"] = metadata
        error["details"] = [info]
    return JSONResponse(
        {"error": error}, status_code=code, headers=headers, media_type=JSON_UTF8
    )


def unauthenticated() -> JSONResponse:
    """401 for an unknown, expired or revoked bearer token."""
    return google_error(
        401,
        "UNAUTHENTICATED",
        UNAUTHENTICATED_MESSAGE,
        headers={
            "WWW-Authenticate": 'Bearer realm="https://accounts.google.com/", error="invalid_token"'
        },
    )


def missing_credentials() -> JSONResponse:
    return google_error(403, "PERMISSION_DENIED", MISSING_CREDENTIALS_MESSAGE)


def insufficient_scope(scope: str = SCOPE_SHEETS) -> JSONResponse:
    return google_error(
        403,
        "PERMISSION_DENIED",
        "Request had insufficient authentication scopes.",
        reason=ACCESS_TOKEN_SCOPE_INSUFFICIENT,
        headers={
            "WWW-Authenticate": 'Bearer realm="https://accounts.google.com/", '
            f'error="insufficient_scope", scope="{scope}"'
        },
        metadata={"service": SHEETS_SERVICE},
    )


def service_disabled() -> JSONResponse:
    """403: the Sheets API isn't enabled in the caller's Cloud project."""
    project = CONSUMER.removeprefix("projects/")
    return google_error(
        403,
        "PERMISSION_DENIED",
        f"Google Sheets API has not been used in project {project} before or it "
        "is disabled. Enable it by visiting https://console.developers.google.com"
        f"/apis/api/{SHEETS_SERVICE}/overview?project={project} then retry.",
        reason=SERVICE_DISABLED,
        metadata={"consumer": CONSUMER, "service": SHEETS_SERVICE},
    )


def rate_limit_exceeded() -> JSONResponse:
    """429: the per-minute request quota is used up."""
    return google_error(
        429,
        "RESOURCE_EXHAUSTED",
        "Quota exceeded for quota metric 'Read requests' and limit 'Read "
        f"requests per minute per user' of service '{SHEETS_SERVICE}'.",
        reason=RATE_LIMIT_EXCEEDED,
        metadata={
            "consumer": CONSUMER,
            "service": SHEETS_SERVICE,
            "quota_metric": f"{SHEETS_SERVICE}/read_requests",
            "quota_limit": "ReadRequestsPerMinutePerUser",
        },
    )


def permission_denied() -> JSONResponse:
    return google_error(403, "PERMISSION_DENIED", "The caller does not have permission")


def not_found() -> JSONResponse:
    return google_error(404, "NOT_FOUND", "Requested entity was not found.")


def internal_error() -> JSONResponse:
    return google_error(500, "INTERNAL", "Internal error encountered.")


def invalid_argument(message: str) -> JSONResponse:
    return google_error(400, "INVALID_ARGUMENT", message)


def unable_to_parse_range(range_: str) -> JSONResponse:
    return invalid_argument(f"Unable to parse range: {range_}")


# reason -> (the HTTP status Google pairs it with, body builder)
REASON_ERRORS: dict[str, tuple[int, Callable[[], JSONResponse]]] = {
    SERVICE_DISABLED: (403, service_disabled),
    ACCESS_TOKEN_SCOPE_INSUFFICIENT: (403, insufficient_scope),
    RATE_LIMIT_EXCEEDED: (429, rate_limit_exceeded),
}


def for_status(status: int, reason: str | None = None) -> JSONResponse:
    """A Google API error for an injected fault.

    With ``reason``, the documented body for that ErrorInfo reason. Without
    one, what Google sends for the status: 403 and 404 with no ErrorInfo, 429
    as ``RATE_LIMIT_EXCEEDED``, 500 as ``INTERNAL``.
    """
    if reason is not None:
        return REASON_ERRORS[reason][1]()
    if status == 401:
        return unauthenticated()
    if status == 403:
        return permission_denied()
    if status == 404:
        return not_found()
    if status == 429:
        return rate_limit_exceeded()
    if status == 500:
        return internal_error()
    name = STATUS_NAMES.get(status, "UNKNOWN")
    return google_error(status, name, f"mock_google: injected {status}")


def oauth_error(
    error: str, description: str | None = None, status: int = 400
) -> JSONResponse:
    body = {"error": error}
    if description is not None:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, media_type=JSON_UTF8)
