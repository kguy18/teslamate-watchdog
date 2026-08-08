"""Sign-in detection must work through BOTH the redirect and the body path.

TeslaMate versions differ on which one they exhibit, so a change that makes one
path pass while quietly breaking the other is exactly the regression these
tests exist to catch.
"""

from __future__ import annotations

import requests

from src.config import DEFAULT_SIGNIN_BODY_MARKERS
from src.http_check import MAX_BODY_BYTES, HttpCategory, HttpChecker

DASHBOARD_BODY = b"""
<html><head><title>TeslaMate</title></head>
<body><div id="dashboard">Drives 412</div><a href="/settings">Settings</a></body></html>
"""

SIGNIN_BODY_TOKEN_FORM = b"""
<html><body>
  <form action="/sign_in" method="post">
    <label>Refresh Token</label><input name="tokens[refresh_token]" type="text"/>
    <label>Access Token</label><input name="tokens[access_token]" type="text"/>
  </form>
</body></html>
"""

# A LiveView variant that omits the words "refresh token" / "access token" and
# is only identifiable by the form element itself.
SIGNIN_BODY_FORM_ONLY = b"""
<html><body><form phx-submit="sign_in"><input type="password"/></form></body></html>
"""


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None, body: bytes = b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeSession:
    def __init__(self, response=None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._response


def make_checker(session: FakeSession) -> HttpChecker:
    return HttpChecker(
        "http://teslamate:4000/",
        timeout=10,
        signin_pattern=r"/sign[_-]?in",
        body_markers=DEFAULT_SIGNIN_BODY_MARKERS,
        session=session,
    )


# --- redirect path ---------------------------------------------------------


def test_redirect_to_sign_in_is_logged_out():
    session = FakeSession(FakeResponse(302, {"Location": "/sign_in"}))
    result = make_checker(session).check()
    assert result.category is HttpCategory.LOGGED_OUT
    assert result.redirect_target == "/sign_in"


def test_redirect_hyphen_variant_is_logged_out():
    session = FakeSession(FakeResponse(302, {"Location": "http://host:4000/sign-in"}))
    assert make_checker(session).check().category is HttpCategory.LOGGED_OUT


def test_redirect_elsewhere_is_unknown_not_logged_out():
    session = FakeSession(FakeResponse(302, {"Location": "/settings"}))
    assert make_checker(session).check().category is HttpCategory.UNKNOWN


def test_checker_does_not_follow_redirects():
    session = FakeSession(FakeResponse(302, {"Location": "/sign_in"}))
    make_checker(session).check()
    assert session.calls[0]["allow_redirects"] is False


# --- body path -------------------------------------------------------------


def test_200_with_token_form_is_logged_out():
    session = FakeSession(FakeResponse(200, body=SIGNIN_BODY_TOKEN_FORM))
    result = make_checker(session).check()
    assert result.category is HttpCategory.LOGGED_OUT


def test_200_with_only_a_signin_form_element_is_logged_out():
    session = FakeSession(FakeResponse(200, body=SIGNIN_BODY_FORM_ONLY))
    assert make_checker(session).check().category is HttpCategory.LOGGED_OUT


def test_200_dashboard_is_authenticated():
    session = FakeSession(FakeResponse(200, body=DASHBOARD_BODY))
    assert make_checker(session).check().category is HttpCategory.AUTHENTICATED


def test_body_markers_are_case_insensitive():
    session = FakeSession(FakeResponse(200, body=b"<p>REFRESH TOKEN</p>"))
    assert make_checker(session).check().category is HttpCategory.LOGGED_OUT


def test_oversized_body_is_capped_and_still_classified():
    body = b"<html>" + (b"x" * (MAX_BODY_BYTES * 2)) + b"</html>"
    session = FakeSession(FakeResponse(200, body=body))
    result = make_checker(session).check()
    assert result.category is HttpCategory.AUTHENTICATED


# --- failure paths ---------------------------------------------------------


def test_connection_error_is_unreachable():
    session = FakeSession(exc=requests.ConnectionError("refused"))
    result = make_checker(session).check()
    assert result.category is HttpCategory.UNREACHABLE
    assert result.error == "ConnectionError"


def test_timeout_is_unreachable():
    session = FakeSession(exc=requests.Timeout("timed out"))
    assert make_checker(session).check().category is HttpCategory.UNREACHABLE


def test_500_is_application_error():
    session = FakeSession(FakeResponse(500, body=b"oops"))
    assert make_checker(session).check().category is HttpCategory.APPLICATION_ERROR


def test_unexpected_status_is_unknown():
    session = FakeSession(FakeResponse(418, body=b""))
    assert make_checker(session).check().category is HttpCategory.UNKNOWN


def test_result_carries_the_matched_marker_but_never_the_body():
    session = FakeSession(FakeResponse(200, body=SIGNIN_BODY_TOKEN_FORM))
    result = make_checker(session).check()
    serialised = str(result.as_dict())
    assert result.detail == "sign-in marker in body: refresh token"
    assert "<form" not in serialised
    assert "<html" not in serialised
