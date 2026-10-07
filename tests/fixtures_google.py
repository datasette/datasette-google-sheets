"""Mock Google fixtures, shared by the whole suite (imported by conftest.py).

Vendored from datasette-google-credentials @ a2f4eee (see mock_google/__init__.py).
A Datasette with this plugin too, and credential helpers: fixtures_sheets.py.

    async def test_something(mock_google):
        datasette = mock_google.datasette()          # plugin config + transport wired
        await datasette.invoke_startup()
        ...
        assert mock_google.calls("/token", method="POST")

    mock_google.oauth.granted_scopes = {"openid", "email"}  # partial consent
    mock_google.oauth.deny = True                           # user clicks Cancel
    mock_google.oauth.rotate_refresh_tokens = True
    mock_google.faults.fail("/revoke", 500)
    mock_google.faults.fail("/v4/", reason="SERVICE_DISABLED")
    mock_google.sheets.add("sales", {"Q1": [["region", "total"], ["north", 10]]})
    mock_google.sheets.share("sales", SA_OTHER, READER)  # a write then 403s
    mock_google.sheets.delete("students")                 # then 404
    mock_google.tokens.lifetime = 5
    refresh_token = mock_google.oauth.issue_refresh_token()  # seed a grant

The suite never touches the network: ``_block_network`` (autouse, session)
makes any internet socket connect or DNS lookup raise ``NetworkBlocked``.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx2
import pytest
from datasette.app import Datasette

# The ONE non-public google-credentials import in this repo, test-only. There is no
# public way to route google-credentials's outbound HTTP to an in-process transport
# until `datasette_google_credentials.testing` exists (D22; upstream ticket
# sheets-consumer/03). Nothing under datasette_google_sheets/ may do this.
from datasette_google_credentials.http import set_transport
from mock_google import MOCK_BASE, SHEETS_BASE
from mock_google.app import MockState, create_app
from mock_google.keys import ServiceAccountKey, fixture_service_accounts
from mock_google.oauth import OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET

PLUGIN_NAME = "datasette-google-credentials"

GOOGLE_BASE_URLS = {
    "oauth_authorize": f"{MOCK_BASE}/o/oauth2/v2/auth",
    "oauth_token": f"{MOCK_BASE}/token",
    "oauth_revoke": f"{MOCK_BASE}/revoke",
    "userinfo": f"{MOCK_BASE}/v1/userinfo",
}

__all__ = [
    "GOOGLE_BASE_URLS",
    "SHEETS_BASE",
    "MockGoogle",
    "NetworkBlocked",
    "_block_network",
    "mock_google",
    "service_account_keys",
]


class NetworkBlocked(RuntimeError):
    """A test tried to open a real network connection."""


_INTERNET = (socket.AF_INET, socket.AF_INET6)


@pytest.fixture(autouse=True, scope="session")
def _block_network():
    """Fail any AF_INET/AF_INET6 connect or DNS lookup. Unix sockets (asyncio's
    self-pipe) still work; the ASGI transport never needs a socket."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse(address: object) -> NetworkBlocked:
        return NetworkBlocked(f"network access blocked in tests: {address!r}")

    def connect(self: socket.socket, address: Any) -> None:
        if self.family in _INTERNET:
            raise refuse(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in _INTERNET:
            raise refuse(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        raise refuse((host, port))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        yield


@pytest.fixture(scope="session")
def service_account_keys() -> dict[str, ServiceAccountKey]:
    """RSA service-account keys generated at session start, keyed
    ``test`` / ``other`` / ``unregistered`` / ``token_500`` (see mock_google.keys).
    ``key.key_json()`` is the downloadable key file."""
    return fixture_service_accounts()


class MockGoogle:
    """One test's mock Google server plus helpers to wire Datasette to it."""

    def __init__(self) -> None:
        self.app = create_app()
        self.state: MockState = self.app.state
        self.transport = httpx2.ASGITransport(app=self.app)

    # Knob shortcuts (the objects themselves document their knobs)
    @property
    def oauth(self):
        return self.state.oauth

    @property
    def tokens(self):
        return self.state.tokens

    @property
    def faults(self):
        return self.state.faults

    @property
    def sheets(self):
        return self.state.sheets

    @property
    def requests(self):
        """Every request the mock received, in order."""
        return self.state.requests

    def calls(self, path: str | None = None, **filters: str):
        """Recorded requests filtered by path prefix, ``method=`` and ``host=``."""
        return self.state.calls(path, **filters)

    def plugin_config(self, **overrides: Any) -> dict[str, Any]:
        """``datasette-google-credentials`` plugin config pointing at the mock."""
        config: dict[str, Any] = {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "google_base_urls": dict(GOOGLE_BASE_URLS),
        }
        config.update(overrides)
        return config

    def attach(self, datasette: Datasette) -> Datasette:
        """Route ``datasette``'s outbound Google HTTP to this mock."""
        set_transport(datasette, self.transport)
        return datasette

    def datasette(
        self, plugin_config: dict[str, Any] | None = None, **kwargs: Any
    ) -> Datasette:
        """A ``Datasette(memory=True)`` wired to the mock. ``plugin_config``
        entries override ``self.plugin_config()``; ``kwargs`` go to Datasette
        (a ``config`` there is merged, with our plugin block added)."""
        config = dict(kwargs.pop("config", None) or {})
        plugins = dict(config.get("plugins") or {})
        plugins[PLUGIN_NAME] = self.plugin_config(**(plugin_config or {}))
        config["plugins"] = plugins
        kwargs.setdefault("memory", True)
        return self.attach(Datasette(config=config, **kwargs))

    def client(self) -> httpx2.AsyncClient:
        """An httpx2 client that talks straight to the mock."""
        return httpx2.AsyncClient(transport=self.transport, base_url=MOCK_BASE)


@pytest.fixture
def mock_google(service_account_keys) -> MockGoogle:
    """A fresh mock Google (own tokens, grants, spreadsheets, log) per test."""
    return MockGoogle()
