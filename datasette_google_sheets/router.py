"""The shared router every module in ``routes/`` registers on.

Every view it hands to Datasette caps the request body at
``MAX_BODY_BYTES``, well under Datasette's own ``max_post_body_bytes``
(2 MB by default). Link create/update bodies (a URL, a tab, a column mapping)
are small. datasette-plugin-router reads and parses a ``Body()`` before the
handler runs, so the cap has to wrap the router's view rather than live in a
handler. Copied from datasette-google-credentials's ``router.py``.

It also enforces each route's HTTP method, which neither Datasette (the
first matching path wins, ``utils.resolve_routes``) nor the plugin router
checks. So ``GET`` and ``POST`` can share a path (``/api/links``), and a
POST-only mutation is never reachable by ``GET``, which core's CSRF check
(unsafe methods only) would let through cross-site. A wrong method gets a
JSON 405.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from datasette import Response
from datasette.utils.asgi import PayloadTooLarge
from datasette_plugin_router import Router

MAX_BODY_BYTES = 16 * 1024


def limit_body(view: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a router view so ``request.post_body()`` reads at most
    ``MAX_BODY_BYTES``; a larger body gets a JSON 413. Datasette's own
    setting still wins if it is lower."""

    @functools.wraps(view)
    async def limited(request, datasette=None, scope=None, receive=None, send=None):
        current = request.max_post_body_bytes
        request.max_post_body_bytes = (
            min(current, MAX_BODY_BYTES) if current else MAX_BODY_BYTES
        )
        try:
            return await view(
                request, datasette=datasette, scope=scope, receive=receive, send=send
            )
        except PayloadTooLarge:
            return Response.json(
                {
                    "ok": False,
                    "error": f"Request body is larger than {MAX_BODY_BYTES} bytes",
                    "code": "payload_too_large",
                },
                status=413,
            )

    return limited


def _method_not_allowed(allowed: list[str]) -> Response:
    return Response.json(
        {
            "ok": False,
            "error": f"Method not allowed (use {', '.join(allowed)})",
            "code": "method_not_allowed",
        },
        status=405,
        headers={"Allow": ", ".join(allowed)},
    )


def dispatch(views: dict[str, Callable[..., Any]]) -> Callable[..., Any]:
    """One view for a path: the view registered for the request's method
    (``HEAD`` counts as ``GET``), else a 405."""
    allowed = sorted(views)

    async def by_method(request, datasette=None, scope=None, receive=None, send=None):
        method = "GET" if request.method == "HEAD" else request.method
        view = views.get(method)
        if view is None:
            return _method_not_allowed(allowed)
        return await view(
            request, datasette=datasette, scope=scope, receive=receive, send=send
        )

    return by_method


class GoogleSheetsRouter(Router):
    """A ``Router`` whose views check their method and go through
    ``limit_body``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._methods: dict[Callable[..., Any], str] = {}

    def GET(self, path: str, *, output: type | None = None):
        return self._record("GET", super().GET(path, output=output))

    def POST(self, path: str, *, output: type | None = None):
        return self._record("POST", super().POST(path, output=output))

    def _record(self, method: str, decorator: Callable[..., Any]):
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            view = decorator(fn)
            self._methods[view] = method
            return view

        return register

    def routes(self) -> list[tuple[str, Callable[..., Any]]]:
        by_path: dict[str, dict[str, Callable[..., Any]]] = {}
        for path, view in super().routes():
            by_path.setdefault(path, {})[self._methods[view]] = view
        return [(path, limit_body(dispatch(views))) for path, views in by_path.items()]


# The one Router every module in routes/ registers on.
router = GoogleSheetsRouter(title="datasette-google-sheets")
