"""Access tokens, and the JWT-bearer grant that service accounts use.

Principals are Google identities: an OAuth user (``sub`` + ``email``) or a
service account (its ``client_email``, no ``sub``). Every access token the
mock issues maps to one principal and its scopes. Tokens minted from an OAuth
grant die with it when the grant is revoked.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from . import MOCK_BASE
from .keys import GOOGLE_TOKEN_URI

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# A service-account JWT's `aud` must be the token endpoint. Google's own is
# always accepted; the mock's is too, so the plugin may use either.
TOKEN_AUDIENCES = frozenset({GOOGLE_TOKEN_URI, f"{MOCK_BASE}/token"})

DEFAULT_LIFETIME = 3599
MAX_ASSERTION_LIFETIME = 3600
CLOCK_SKEW = 300

# Wordings as real Google sends them (as recorded by the sqlite-google-sheets
# mock, which checked them against Google on 2026-09-29).
MSG_BAD_SIGNATURE = "Invalid JWT Signature."
MSG_BAD_AUDIENCE = "Invalid JWT: Failed audience check."
MSG_BAD_TIME = (
    "Invalid JWT: Token must be a short-lived token (60 minutes) and in a reasonable "
    "timeframe. Check your iat and exp values in the JWT claim."
)
MSG_UNKNOWN_ACCOUNT = "Invalid grant: account not found"
MSG_BAD_SCOPE = "Invalid OAuth scope or ID token audience provided."


class TokenError(Exception):
    """An OAuth error: rendered as ``{"error", "error_description"}``."""

    def __init__(self, error: str, description: str | None = None, status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class Principal:
    name: str  # email address: the user's, or the service account's client_email
    scopes: frozenset[str]
    sub: str | None = None  # OAuth users only


@dataclass(eq=False)
class Grant:
    """A user's consent to an OAuth client: one refresh token plus every
    access token minted from it."""

    client_id: str
    principal: Principal
    refresh_token: str
    revoked: bool = False


@dataclass(frozen=True)
class IssuedToken:
    principal: Principal
    expires_at: float
    grant: Grant | None = None


class TokenStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, IssuedToken] = {}
        self._service_accounts: dict[str, rsa.RSAPublicKey] = {}
        self.lifetime = DEFAULT_LIFETIME
        """Knob: ``expires_in`` of newly issued access tokens (seconds)."""

    def register_service_account(
        self, client_email: str, public_key: rsa.RSAPublicKey
    ) -> None:
        self._service_accounts[client_email] = public_key

    def issue(self, principal: Principal, grant: Grant | None = None) -> dict:
        token = "ya29.mock-" + secrets.token_urlsafe(24)
        lifetime = self.lifetime
        with self._lock:
            self._tokens[token] = IssuedToken(principal, time.time() + lifetime, grant)
        return {
            "access_token": token,
            "expires_in": lifetime,
            "scope": " ".join(sorted(principal.scopes)),
            "token_type": "Bearer",
        }

    def lookup(self, token: str) -> Principal | None:
        """The principal for a live token; None if unknown, expired or revoked."""
        with self._lock:
            issued = self._tokens.get(token)
        if issued is None or time.time() >= issued.expires_at:
            return None
        if issued.grant is not None and issued.grant.revoked:
            return None
        return issued.principal

    def issued_tokens(self) -> list[str]:
        """Every access token issued and not revoked (for leak checks)."""
        with self._lock:
            return list(self._tokens)

    def pop(self, token: str) -> IssuedToken | None:
        with self._lock:
            return self._tokens.pop(token, None)

    def expire_all(self) -> None:
        """Knob: every access token issued so far is now expired."""
        with self._lock:
            self._tokens = {
                token: IssuedToken(issued.principal, 0, issued.grant)
                for token, issued in self._tokens.items()
            }

    def jwt_bearer(self, assertion: str) -> Principal:
        """Check a service-account JWT assertion; return its principal."""
        try:
            claims = jwt.decode(assertion, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise TokenError("invalid_request", "Bad Request") from None
        iss = claims.get("iss")
        public_key = self._service_accounts.get(iss) if isinstance(iss, str) else None
        if public_key is None:
            raise TokenError("invalid_grant", MSG_UNKNOWN_ACCOUNT)
        try:
            jwt.decode(
                assertion,
                public_key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError:
            raise TokenError("invalid_grant", MSG_BAD_SIGNATURE) from None
        if claims.get("aud") not in TOKEN_AUDIENCES:
            raise TokenError("invalid_grant", MSG_BAD_AUDIENCE)
        iat, exp, now = claims.get("iat"), claims.get("exp"), time.time()
        if (
            not isinstance(iat, (int, float))
            or not isinstance(exp, (int, float))
            or exp - iat > MAX_ASSERTION_LIFETIME
            or exp <= now
            or iat > now + CLOCK_SKEW
        ):
            raise TokenError("invalid_grant", MSG_BAD_TIME)
        scope = claims.get("scope")
        if not isinstance(scope, str) or not scope.split():
            raise TokenError("invalid_scope", MSG_BAD_SCOPE)
        return Principal(iss, frozenset(scope.split()))
