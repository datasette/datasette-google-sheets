"""Injected failures.

    mock_google.faults.fail("/revoke", 500)                    # next revoke fails
    mock_google.faults.fail("/v4/spreadsheets/", 401, times=1) # one 401, then OK
    mock_google.faults.fail("/token", 503, times=None)         # always
    mock_google.faults.fail("/v4/", reason="SERVICE_DISABLED") # 403 + ErrorInfo
    mock_google.faults.fail("/v4/", 500, after=2)              # 2 succeed, then 500

``path`` is a prefix of the request path; ``method`` optionally narrows it.
A matching request is still recorded in the request log, with the injected
status. OAuth paths get the flat OAuth error body, everything else a
``google.rpc.Status`` one (see ``errors.for_status``): with ``reason`` the
documented body for that ErrorInfo reason, whose status is implied.

The service account ``keys.SA_TOKEN_500`` also gets a 500 on its first token
exchange, without any setup.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import REASON_ERRORS


@dataclass
class Injected:
    status: int
    reason: str | None = None


@dataclass
class _Rule:
    path: str
    injected: Injected
    remaining: int | None  # None = forever
    method: str | None
    after: int = 0  # matching requests to let through first


class Faults:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[_Rule] = []
        self._token_500_seen: set[str] = set()

    def fail(
        self,
        path: str,
        status: int | None = None,
        *,
        reason: str | None = None,
        times: int | None = 1,
        method: str | None = None,
        after: int = 0,
    ) -> None:
        """Answer the next ``times`` matching requests (``None`` = all) with
        ``status``, or with the documented error for ErrorInfo ``reason``
        (``SERVICE_DISABLED``, ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``,
        ``RATE_LIMIT_EXCEEDED``). ``after`` lets that many matching requests
        through first (a failure part-way through a chunked append)."""
        if reason is not None:
            if reason not in REASON_ERRORS:
                raise ValueError(f"unknown reason {reason!r}")
            implied = REASON_ERRORS[reason][0]
            if status is not None and status != implied:
                raise ValueError(f"{reason} is a {implied}, not a {status}")
            status = implied
        if status is None:
            raise TypeError("fail() needs a status or a reason")
        with self._lock:
            self._rules.append(
                _Rule(
                    path,
                    Injected(status, reason),
                    times,
                    method.upper() if method else None,
                    after,
                )
            )

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()

    def take(self, method: str, path: str) -> Injected | None:
        """The injected failure for this request, consuming one use; else None."""
        with self._lock:
            for rule in self._rules:
                if not path.startswith(rule.path):
                    continue
                if rule.method is not None and rule.method != method:
                    continue
                if rule.after:
                    rule.after -= 1
                    continue
                if rule.remaining is not None:
                    rule.remaining -= 1
                    if rule.remaining == 0:
                        self._rules.remove(rule)
                return rule.injected
        return None

    def token_500_once(self, client_email: str, magic_email: str) -> bool:
        """True the first time ``magic_email`` asks for a token."""
        if client_email != magic_email:
            return False
        with self._lock:
            if client_email in self._token_500_seen:
                return False
            self._token_500_seen.add(client_email)
            return True
