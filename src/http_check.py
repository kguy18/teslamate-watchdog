"""Is TeslaMate up, and is it showing the sign-in page?

TeslaMate versions differ in how they present a logged-out instance: some
302-redirect ``/`` to ``/sign_in``, others render the token form at ``/`` with
HTTP 200. Both paths are checked on every request — never assume the instance
behaves the way it did during pre-flight, because that changes across upgrades.

Response bodies are scanned in memory and discarded. They are only ever written
anywhere when LOG_LEVEL=DEBUG, and even then only the marker that matched.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final

import requests

log = logging.getLogger(__name__)

# The sign-in page is a few KB. Cap the read so a wedged TeslaMate streaming
# an enormous body cannot balloon the watchdog's memory.
MAX_BODY_BYTES: Final = 512 * 1024

# Matches `<form action="/sign_in"` and Phoenix LiveView's `phx-submit="sign_in"`.
_SIGNIN_FORM_RE: Final = re.compile(r"<form[^>]*sign[_-]?in", re.IGNORECASE)


class HttpCategory(str, Enum):
    AUTHENTICATED = "authenticated"
    LOGGED_OUT = "logged_out"
    UNREACHABLE = "unreachable"
    APPLICATION_ERROR = "application_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HttpResult:
    category: HttpCategory
    status_code: int | None = None
    redirect_target: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    #: Why this category was chosen — safe to log and to put in diagnostics.
    detail: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "status_code": self.status_code,
            "redirect_target": self.redirect_target,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class HttpChecker:
    def __init__(
        self,
        url: str,
        *,
        timeout: int,
        signin_pattern: str,
        body_markers: tuple[str, ...],
        session: requests.Session | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._signin_re = re.compile(signin_pattern, re.IGNORECASE)
        self._body_markers = tuple(marker.lower() for marker in body_markers)
        self._session = session or requests.Session()

    def check(self) -> HttpResult:
        started = time.monotonic()
        try:
            with self._session.get(
                self._url,
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
                headers={"Accept": "text/html", "User-Agent": "teslamate-watchdog"},
            ) as response:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return self._classify(response, elapsed_ms)
        except (requests.ConnectionError, requests.Timeout) as exc:
            return HttpResult(
                category=HttpCategory.UNREACHABLE,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
        except requests.RequestException as exc:
            return HttpResult(
                category=HttpCategory.UNKNOWN,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # --- internals --------------------------------------------------------

    def _classify(self, response: requests.Response, elapsed_ms: int) -> HttpResult:
        status = response.status_code

        if 300 <= status < 400:
            location = response.headers.get("Location", "")
            if self._signin_re.search(location):
                return HttpResult(
                    category=HttpCategory.LOGGED_OUT,
                    status_code=status,
                    redirect_target=location,
                    elapsed_ms=elapsed_ms,
                    detail="redirect to sign-in path",
                )
            return HttpResult(
                category=HttpCategory.UNKNOWN,
                status_code=status,
                redirect_target=location,
                elapsed_ms=elapsed_ms,
                detail="redirect to an unrecognised location",
            )

        if status == 200:
            body = self._read_body(response)
            marker = self._find_signin_marker(body)
            if marker is not None:
                return HttpResult(
                    category=HttpCategory.LOGGED_OUT,
                    status_code=status,
                    elapsed_ms=elapsed_ms,
                    detail=f"sign-in marker in body: {marker}",
                )
            return HttpResult(
                category=HttpCategory.AUTHENTICATED,
                status_code=status,
                elapsed_ms=elapsed_ms,
                detail="200 with no sign-in markers",
            )

        if status >= 500:
            return HttpResult(
                category=HttpCategory.APPLICATION_ERROR,
                status_code=status,
                elapsed_ms=elapsed_ms,
                detail="server error response",
            )

        return HttpResult(
            category=HttpCategory.UNKNOWN,
            status_code=status,
            elapsed_ms=elapsed_ms,
            detail="unhandled status code",
        )

    def _read_body(self, response: requests.Response) -> str:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BODY_BYTES:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _find_signin_marker(self, body: str) -> str | None:
        lowered = body.lower()
        for marker in self._body_markers:
            if marker in lowered:
                return marker
        if _SIGNIN_FORM_RE.search(body):
            return "sign-in form element"
        return None
