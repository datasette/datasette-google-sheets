"""Datasette + google-credentials + this plugin, wired to the mock Google, plus
credential helpers. Imported by conftest.py.

    async def test_something(datasette, mock_google, sa_credential, oauth_credential):
        sa = await sa_credential("alice")          # CredentialInfo, via the HTTP API
        oauth = await oauth_credential("alice")    # CredentialInfo, via Connect Google
        cred = await get_credential(datasette, sa.id, actor=ALICE, scopes=[SCOPE_SHEETS])
        response = await cred.request("GET", f"{SHEETS_BASE}/v4/spreadsheets/students")

Credentials are created only through google-credentials's public surface: its HTTP
API (``POST /-/google-credentials/api/service-accounts``) and its "Connect Google"
flow against the auto-approving mock consent screen. Never its tables.

This file is ours, not vendored: it stays when ``fixtures_google.py`` is
swapped for ``datasette_google_credentials.testing`` (D22).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from datasette.app import Datasette
from datasette_google_credentials import CredentialInfo, connect_url, list_credentials
from fixtures_google import MockGoogle
from mock_google.oauth import DEFAULT_REDIRECT_URI, DEFAULT_USER, GoogleUser

ALICE = {"id": "alice"}
BOB = {"id": "bob"}

SHEETS_PLUGIN = "datasette-google-sheets"

# google-credentials's actions: any signed-in actor may connect Google or add a
# service account in these tests (a test narrows them via `config=`).
DEFAULT_PERMISSIONS: dict[str, Any] = {
    "google-credentials-connect": {"id": "*"},
    "google-credentials-add-service-account": {"id": "*"},
}

__all__ = [
    "ALICE",
    "BOB",
    "SHEETS_PLUGIN",
    "datasette",
    "make_datasette",
    "oauth_credential",
    "sa_credential",
]


async def make_datasette(
    mock_google: MockGoogle,
    *,
    config: dict[str, Any] | None = None,
    google_credentials_config: dict[str, Any] | None = None,
    sheets_config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Datasette:
    """A started ``Datasette(memory=True)`` with google-credentials pointed at the
    mock (OAuth client, base URLs, transport, a fresh encryption key) and
    this plugin loaded (it's installed, so it always is).

    ``config`` is Datasette config; its ``permissions`` are merged over
    ``DEFAULT_PERMISSIONS``. ``google_credentials_config`` overrides google-credentials's
    plugin block, ``sheets_config`` sets ours."""
    config = dict(config or {})
    config["permissions"] = {**DEFAULT_PERMISSIONS, **(config.get("permissions") or {})}
    if sheets_config is not None:
        config["plugins"] = {
            **(config.get("plugins") or {}),
            SHEETS_PLUGIN: sheets_config,
        }
    datasette = mock_google.datasette(
        plugin_config={
            "encryption-key": Fernet.generate_key().decode(),
            **(google_credentials_config or {}),
        },
        config=config,
        **kwargs,
    )
    await datasette.invoke_startup()
    return datasette


@pytest_asyncio.fixture
async def datasette(mock_google: MockGoogle) -> Datasette:
    """The default wiring: see ``make_datasette``."""
    return await make_datasette(mock_google)


def _actor(owner: str | dict[str, Any]) -> dict[str, Any]:
    return owner if isinstance(owner, dict) else {"id": owner}


async def _credential(
    datasette: Datasette, actor: dict[str, Any], match: Callable[[Any], bool]
) -> CredentialInfo:
    found = [c for c in await list_credentials(datasette, actor=actor) if match(c)]
    assert found, "credential not visible to its owner"
    return found[-1]


@pytest.fixture
def sa_credential(
    datasette: Datasette, service_account_keys
) -> Callable[..., Awaitable[CredentialInfo]]:
    """``await sa_credential(owner, key="test", label=None)``: add a service
    account through google-credentials's HTTP API as ``owner`` (an id or actor).
    ``key`` names a ``service_account_keys`` entry: ``test`` is an editor on
    the fixture spreadsheets, ``other`` has access to none."""

    async def add(
        owner: str | dict[str, Any], key: str = "test", label: str | None = None
    ) -> CredentialInfo:
        actor = _actor(owner)
        body: dict[str, Any] = {
            "key_json": json.dumps(service_account_keys[key].key_json())
        }
        if label is not None:
            body["label"] = label
        response = await datasette.client.post(
            "/-/google-credentials/api/service-accounts",
            json=body,
            actor=actor,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 200, response.status_code
        credential_id = response.json()["id"]
        return await _credential(datasette, actor, lambda c: c.id == credential_id)

    return add


@pytest.fixture
def oauth_credential(
    datasette: Datasette, mock_google: MockGoogle
) -> Callable[..., Awaitable[CredentialInfo]]:
    """``await oauth_credential(owner, user=DEFAULT_USER, granted_scopes=None)``:
    run google-credentials's Connect Google flow as ``owner`` against the mock's
    auto-approving consent screen (as google-credentials's tests/test_oauth.py does).
    ``granted_scopes`` models partial consent (e.g. no ``spreadsheets``)."""

    async def connect(
        owner: str | dict[str, Any],
        user: GoogleUser = DEFAULT_USER,
        granted_scopes: set[str] | None = None,
    ) -> CredentialInfo:
        actor = _actor(owner)
        mock_google.oauth.user = user
        mock_google.oauth.granted_scopes = granted_scopes
        try:
            start = await datasette.client.get(
                connect_url(datasette, return_to="/"), actor=actor
            )
            assert start.status_code == 302, start.status_code
            async with mock_google.client() as google:
                consent = await google.get(start.headers["location"])
            assert consent.status_code == 302, consent.status_code
            callback_url = consent.headers["location"]
            assert callback_url.startswith(DEFAULT_REDIRECT_URI)
            parts = urlsplit(callback_url)
            done = await datasette.client.get(
                f"{parts.path}?{parts.query}",
                actor=actor,
                cookies=dict(start.cookies),
            )
            assert done.status_code == 302, done.status_code
        finally:
            mock_google.oauth.user = DEFAULT_USER
            mock_google.oauth.granted_scopes = None
        return await _credential(
            datasette,
            actor,
            lambda c: c.type == "google_oauth" and c.google_email == user.email,
        )

    return connect
