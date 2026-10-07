"""OAuth for users: authorization code + PKCE, refresh, revoke, userinfo.

Knobs (attributes of ``OAuthServer``; a fresh server per test):

    user                  GoogleUser who approves the consent screen
    granted_scopes        None = grant everything requested; a set = grant only
                          the requested scopes that are in it (partial consent)
    deny                  True = the user clicks Cancel (error=access_denied)
    rotate_refresh_tokens True = refreshes return a new refresh_token and
                          invalidate the old one
    clients               client_id -> OAuthClient (secret, redirect URIs)

Helpers for tests that don't want to run the whole flow:
``issue_refresh_token()`` seeds a grant; ``is_revoked()`` checks one.

The authorize endpoint is stricter than Google on purpose: it insists on
``code_challenge_method=S256`` and ``access_type=offline``, which the plugin
always sends (wiki D8), so a regression fails loudly.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit, urlunsplit

import jwt

from .keys import ID_TOKEN_KID, id_token_key
from .tokens import Grant, Principal, TokenError, TokenStore

SCOPE_OPENID = "openid"
SCOPE_EMAIL = "email"
SCOPE_PROFILE = "profile"
SCOPE_SHEETS = "https://www.googleapis.com/auth/spreadsheets"
SCOPE_SHEETS_RO = "https://www.googleapis.com/auth/spreadsheets.readonly"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_DRIVE_RO = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
KNOWN_SCOPES = frozenset(
    {
        SCOPE_OPENID,
        SCOPE_EMAIL,
        SCOPE_PROFILE,
        SCOPE_SHEETS,
        SCOPE_SHEETS_RO,
        SCOPE_DRIVE,
        SCOPE_DRIVE_RO,
        SCOPE_DRIVE_FILE,
    }
)

OAUTH_CLIENT_ID = "mock-client-id.apps.googleusercontent.com"
OAUTH_CLIENT_SECRET = "mock-client-secret"
# What datasette.absolute_url() gives under datasette.client.
DEFAULT_REDIRECT_URI = "http://localhost/-/google-credentials/oauth/callback"

ISSUER = "https://accounts.google.com"
CODE_LIFETIME = 600
_PKCE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")

# Google's wording where known (sqlite-google-sheets verified the revoked
# one); the PKCE and auth-code ones are unverified.
MSG_REVOKED = "Token has been expired or revoked."
MSG_MALFORMED_CODE = "Malformed auth code."
MSG_MISSING_VERIFIER = "Missing code verifier."
MSG_BAD_VERIFIER = "Invalid code verifier."


@dataclass(frozen=True)
class GoogleUser:
    sub: str
    email: str


DEFAULT_USER = GoogleUser("100000000000000000001", "user@example.com")


@dataclass
class OAuthClient:
    secret: str
    redirect_uris: set[str] = field(default_factory=lambda: {DEFAULT_REDIRECT_URI})


@dataclass
class _AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    principal: Principal
    expires_at: float
    used: bool = False


def s256(verifier: str) -> str:
    """The PKCE S256 code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + urlencode(params)
    return urlunsplit(parts._replace(query=query))


class OAuthServer:
    def __init__(self, tokens: TokenStore) -> None:
        self.tokens = tokens
        self._lock = threading.Lock()
        self._codes: dict[str, _AuthCode] = {}
        self._grants: dict[str, Grant] = {}  # refresh_token -> grant
        # Knobs
        self.clients: dict[str, OAuthClient] = {
            OAUTH_CLIENT_ID: OAuthClient(OAUTH_CLIENT_SECRET)
        }
        self.user: GoogleUser = DEFAULT_USER
        self.granted_scopes: set[str] | None = None
        self.deny = False
        self.rotate_refresh_tokens = False

    # --- GET /o/oauth2/v2/auth ---------------------------------------------

    def authorize(self, params: dict[str, str]) -> str:
        """Validate an authorization request; return the URL to 302 to.

        Errors before ``redirect_uri`` is trusted are raised (Google shows an
        error page and never redirects); so are malformed requests, which
        Google also mostly shows as an error page.
        """
        client = self.clients.get(params.get("client_id", ""))
        if client is None:
            raise TokenError("invalid_client", "The OAuth client was not found.", 401)
        redirect_uri = params.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:
            raise TokenError("redirect_uri_mismatch", "Bad Request")
        if params.get("response_type") != "code":
            raise TokenError(
                "unsupported_response_type",
                "Invalid response_type: " + params.get("response_type", ""),
            )
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not challenge:
            raise TokenError(
                "invalid_request", "mock: PKCE with code_challenge_method=S256 required"
            )
        if params.get("access_type") != "offline":
            raise TokenError("invalid_request", "mock: access_type=offline required")
        requested = params.get("scope", "").split()
        if not requested:
            raise TokenError("invalid_request", "Missing required parameter: scope")
        unknown = sorted(set(requested) - KNOWN_SCOPES)
        if unknown:
            raise TokenError(
                "invalid_scope",
                "Some requested scopes were invalid. {invalid=["
                + ", ".join(unknown)
                + "]}",
            )

        state = params.get("state")
        if self.deny:
            reply = {"error": "access_denied"}
        else:
            granted = set(requested)
            if self.granted_scopes is not None:
                granted &= self.granted_scopes
            code = "4/mock-" + secrets.token_urlsafe(24)
            with self._lock:
                self._codes[code] = _AuthCode(
                    client_id=params["client_id"],
                    redirect_uri=redirect_uri,
                    code_challenge=challenge,
                    principal=Principal(
                        self.user.email, frozenset(granted), sub=self.user.sub
                    ),
                    expires_at=time.time() + CODE_LIFETIME,
                )
            reply = {"code": code, "scope": " ".join(sorted(granted))}
        if state is not None:
            reply["state"] = state
        return _with_query(redirect_uri, reply)

    # --- POST /token -----------------------------------------------------------

    def _check_client(self, form: dict[str, str]) -> str:
        client_id = form.get("client_id", "")
        client = self.clients.get(client_id)
        if client is None:
            raise TokenError("invalid_client", "The OAuth client was not found.", 401)
        if form.get("client_secret") != client.secret:
            raise TokenError("invalid_client", "Unauthorized", 401)
        return client_id

    def exchange_code(self, form: dict[str, str]) -> dict:
        """``grant_type=authorization_code``."""
        client_id = self._check_client(form)
        code = form.get("code")
        if not code:
            raise TokenError("invalid_request", "Missing required parameter: code")
        with self._lock:
            auth_code = self._codes.get(code)
            if auth_code is None:
                raise TokenError("invalid_grant", MSG_MALFORMED_CODE)
            if (
                auth_code.used
                or time.time() >= auth_code.expires_at
                or auth_code.client_id != client_id
            ):
                raise TokenError("invalid_grant", "Bad Request")
            if form.get("redirect_uri") != auth_code.redirect_uri:
                raise TokenError("redirect_uri_mismatch", "Bad Request")
            verifier = form.get("code_verifier")
            if not verifier:
                raise TokenError("invalid_grant", MSG_MISSING_VERIFIER)
            if not _PKCE.match(verifier) or not secrets.compare_digest(
                s256(verifier), auth_code.code_challenge
            ):
                raise TokenError("invalid_grant", MSG_BAD_VERIFIER)
            # Single use, and only once the exchange has succeeded.
            auth_code.used = True
        grant = self._new_grant(client_id, auth_code.principal)
        response = self.tokens.issue(grant.principal, grant)
        response["refresh_token"] = grant.refresh_token
        return self._add_id_token(response, client_id, grant.principal)

    def refresh(self, form: dict[str, str]) -> dict:
        """``grant_type=refresh_token``."""
        refresh_token = form.get("refresh_token")
        if not refresh_token:
            raise TokenError(
                "invalid_request", "Missing required parameter: refresh_token"
            )
        client_id = self._check_client(form)
        with self._lock:
            grant = self._grants.get(refresh_token)
        if grant is None:
            raise TokenError("invalid_grant", "Bad Request")
        if grant.revoked:
            raise TokenError("invalid_grant", MSG_REVOKED)
        if grant.client_id != client_id:
            raise TokenError("unauthorized_client", "Unauthorized")
        new_refresh_token = None
        if self.rotate_refresh_tokens:
            new_refresh_token = self._rotate(grant)
        response = self.tokens.issue(grant.principal, grant)
        if new_refresh_token is not None:
            response["refresh_token"] = new_refresh_token
        return self._add_id_token(response, client_id, grant.principal)

    def _rotate(self, grant: Grant) -> str:
        """Retire ``grant``'s refresh token for a new one (same consent)."""
        new_token = self._new_refresh_token()
        with self._lock:
            del self._grants[grant.refresh_token]
            grant.refresh_token = new_token
            self._grants[new_token] = grant
        return new_token

    # --- POST /revoke ----------------------------------------------------------

    def revoke(self, token: str) -> None:
        """Revoke a refresh token or access token, and its whole grant.

        A service-account access token just stops working.
        """
        with self._lock:
            grant = self._grants.get(token)
        if grant is not None and not grant.revoked:
            grant.revoked = True
            return
        issued = self.tokens.pop(token)
        if issued is not None:
            if issued.grant is not None:
                issued.grant.revoked = True
            return
        raise TokenError("invalid_token", "Token expired or revoked")

    # --- GET /v1/userinfo --------------------------------------------------------

    @staticmethod
    def userinfo(principal: Principal) -> dict | None:
        """OpenID claims for a user token; None if it lacks ``openid``."""
        if principal.sub is None or SCOPE_OPENID not in principal.scopes:
            return None
        info: dict = {"sub": principal.sub}
        if SCOPE_EMAIL in principal.scopes:
            info["email"] = principal.name
            info["email_verified"] = True
        return info

    # --- helpers for tests -------------------------------------------------------

    def issue_refresh_token(
        self,
        user: GoogleUser = DEFAULT_USER,
        scopes: frozenset[str] | set[str] = frozenset(
            {SCOPE_OPENID, SCOPE_EMAIL, SCOPE_SHEETS}
        ),
        client_id: str = OAUTH_CLIENT_ID,
    ) -> str:
        """Seed a grant without running the consent flow; return its refresh token."""
        principal = Principal(user.email, frozenset(scopes), sub=user.sub)
        return self._new_grant(client_id, principal).refresh_token

    def refresh_tokens(self) -> list[str]:
        """Every live grant's current refresh token (for leak checks)."""
        with self._lock:
            return list(self._grants)

    def is_revoked(self, refresh_token: str) -> bool:
        """True if revoked (directly or via one of its access tokens) or rotated away."""
        with self._lock:
            grant = self._grants.get(refresh_token)
        return grant is None or grant.revoked

    # --- internals ---------------------------------------------------------------

    @staticmethod
    def _new_refresh_token() -> str:
        return "1//mock-" + secrets.token_urlsafe(32)

    def _new_grant(self, client_id: str, principal: Principal) -> Grant:
        grant = Grant(client_id, principal, self._new_refresh_token())
        with self._lock:
            self._grants[grant.refresh_token] = grant
        return grant

    def _add_id_token(
        self, response: dict, client_id: str, principal: Principal
    ) -> dict:
        if SCOPE_OPENID not in principal.scopes or principal.sub is None:
            return response
        now = int(time.time())
        claims: dict = {
            "iss": ISSUER,
            "azp": client_id,
            "aud": client_id,
            "sub": principal.sub,
            "iat": now,
            "exp": now + 3600,
        }
        if SCOPE_EMAIL in principal.scopes:
            claims["email"] = principal.name
            claims["email_verified"] = True
        response["id_token"] = jwt.encode(
            claims, id_token_key(), algorithm="RS256", headers={"kid": ID_TOKEN_KID}
        )
        return response
