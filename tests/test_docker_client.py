"""Docker access via the socket proxy.

The restart guards here are the last line of defence: even if every other check
were wrong, the client itself must refuse to restart anything that is not the
configured TeslaMate container, and must never touch the database.
"""

from __future__ import annotations

import socket

import pytest
import requests

from src import docker_client as dc
from src.docker_client import DockerClient, check_database, demultiplex


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Routes by substring so tests can describe intent rather than exact URLs."""

    def __init__(self, routes: dict[str, FakeResponse], post_response=None, exc=None):
        self.routes = routes
        self.post_response = post_response
        self.exc = exc
        self.posted: list[str] = []

    def get(self, url, **kwargs):
        if self.exc:
            raise self.exc
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(404, text="not found")

    def post(self, url, **kwargs):
        if self.exc:
            raise self.exc
        self.posted.append(url)
        return self.post_response or FakeResponse(204)


def container(name="teslamate", service="teslamate", cid="abc123def456"):
    return {"Id": cid, "Names": [f"/{name}"], "Labels": {"com.docker.compose.service": service}}


def inspect(running=True, health=None, oom=False, exit_code=0, restart_count=0):
    state = {
        "Running": running,
        "Status": "running" if running else "exited",
        "OOMKilled": oom,
        "ExitCode": exit_code,
        "StartedAt": "2026-08-08T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    }
    if health is not None:
        state["Health"] = {"Status": health}
    return {"State": state, "RestartCount": restart_count}


# --- log demultiplexing -----------------------------------------------------


def frame(payload: bytes, stream: int = 1) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def test_demultiplex_decodes_framed_output():
    raw = frame(b"first line\n") + frame(b"second line\n", stream=2)
    assert demultiplex(raw) == "first line\nsecond line\n"


def test_demultiplex_passes_through_raw_tty_output():
    assert demultiplex(b"plain tty output\n") == "plain tty output\n"


def test_demultiplex_handles_empty():
    assert demultiplex(b"") == ""


# --- resolution -------------------------------------------------------------


def test_resolves_by_exact_name(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [container()])})
    client = DockerClient(make_config(), session=session)

    ref = client.resolve("teslamate")
    assert ref is not None and ref.name == "teslamate"


def test_falls_back_to_the_compose_service_label(make_config):
    entry = container(name="myproject-teslamate-1", service="teslamate")
    session = FakeSession({"/containers/json": FakeResponse(200, [entry])})
    client = DockerClient(make_config(), session=session)

    ref = client.resolve("teslamate")
    assert ref is not None
    assert ref.name == "myproject-teslamate-1"
    assert ref.compose_service == "teslamate"


def test_exact_name_wins_over_a_label_match(make_config):
    entries = [
        container(name="other", service="teslamate", cid="label-match"),
        container(name="teslamate", service="something-else", cid="name-match"),
    ]
    session = FakeSession({"/containers/json": FakeResponse(200, entries)})
    client = DockerClient(make_config(), session=session)

    assert client.resolve("teslamate").id == "name-match"


def test_resolution_returns_none_when_nothing_matches(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [container("grafana", "grafana")])})
    client = DockerClient(make_config(), session=session)

    assert client.resolve("teslamate") is None


# --- status -----------------------------------------------------------------


def test_status_parses_container_state(make_config):
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container()]),
            "/json": FakeResponse(200, inspect(running=True, oom=True, restart_count=3)),
        }
    )
    client = DockerClient(make_config(), session=session)

    status = client.status("teslamate")
    assert status.exists is True
    assert status.running is True
    assert status.oom_killed is True
    assert status.restart_count == 3


def test_missing_container_is_reported_as_absent(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [])})
    client = DockerClient(make_config(), session=session)

    status = client.status("teslamate")
    assert status.exists is False
    assert status.running is None


def test_unreachable_proxy_degrades_instead_of_raising(make_config):
    session = FakeSession({}, exc=requests.ConnectionError("proxy down"))
    client = DockerClient(make_config(), session=session)

    assert client.ping() is False
    assert client.status("teslamate").exists is False
    assert client.logs("teslamate", since_seconds=60) == ""


# --- restart guards ---------------------------------------------------------


def test_restart_refuses_any_container_but_the_configured_one(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [container()])})
    client = DockerClient(make_config(), session=session)

    ok, detail = client.restart("database")
    assert ok is False
    assert "only the configured TeslaMate container" in detail
    assert session.posted == []


def test_restart_refuses_when_the_resolved_container_is_the_database(make_config):
    # A container literally named `teslamate` but carrying the database's
    # compose-service label: resolution succeeds, the guard must still refuse.
    entry = container(name="teslamate", service="database")
    session = FakeSession({"/containers/json": FakeResponse(200, [entry])})
    client = DockerClient(make_config(), session=session)

    ok, detail = client.restart("teslamate")
    assert ok is False
    assert "database" in detail
    assert session.posted == []


def test_restart_refuses_when_the_container_is_missing(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [])})
    client = DockerClient(make_config(), session=session)

    ok, _ = client.restart("teslamate")
    assert ok is False
    assert session.posted == []


def test_restart_posts_to_the_resolved_container(make_config):
    session = FakeSession(
        {"/containers/json": FakeResponse(200, [container(cid="deadbeef1234")])},
        post_response=FakeResponse(204),
    )
    client = DockerClient(make_config(), session=session)

    ok, detail = client.restart("teslamate")
    assert ok is True
    assert len(session.posted) == 1
    assert "deadbeef1234/restart" in session.posted[0]


def test_restart_uses_a_timeout_long_enough_for_a_graceful_stop(make_config):
    """Docker's restart blocks for the stop timeout, so it needs its own budget.

    Reusing the ordinary read timeout made real restarts of slow-stopping
    containers report failure while succeeding in the background.
    """
    captured = {}

    class RecordingSession(FakeSession):
        def post(self, url, **kwargs):
            captured.update(kwargs)
            return FakeResponse(204)

    session = RecordingSession({"/containers/json": FakeResponse(200, [container()])})
    client = DockerClient(make_config(DOCKER_TIMEOUT_SECONDS=10), session=session)

    ok, _ = client.restart("teslamate")
    assert ok is True
    assert captured["timeout"] == dc.RESTART_HTTP_TIMEOUT
    assert captured["timeout"] > 10
    assert captured["params"]["t"] == dc.RESTART_STOP_TIMEOUT


def test_a_restart_timeout_is_reported_as_possibly_in_flight(make_config):
    class TimingOutSession(FakeSession):
        def post(self, url, **kwargs):
            raise requests.Timeout("read timed out")

    session = TimingOutSession({"/containers/json": FakeResponse(200, [container()])})
    client = DockerClient(make_config(), session=session)

    ok, detail = client.restart("teslamate")
    assert ok is False
    assert "still be in progress" in detail


def test_restart_explains_a_proxy_denial(make_config):
    session = FakeSession(
        {"/containers/json": FakeResponse(200, [container()])},
        post_response=FakeResponse(403, text="Forbidden"),
    )
    client = DockerClient(make_config(), session=session)

    ok, detail = client.restart("teslamate")
    assert ok is False
    assert "ALLOW_RESTARTS" in detail


# --- database health --------------------------------------------------------


def test_database_prefers_the_docker_healthcheck(make_config, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not fall back to TCP when a healthcheck exists")

    monkeypatch.setattr(dc.socket, "create_connection", explode)
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=True, health="healthy")),
        }
    )
    client = DockerClient(make_config(), session=session)

    health = check_database(make_config(), client)
    assert health.healthy is True
    assert health.source == "healthcheck"


def test_unhealthy_healthcheck_reports_unhealthy(make_config):
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=True, health="unhealthy")),
        }
    )
    client = DockerClient(make_config(), session=session)
    assert check_database(make_config(), client).healthy is False


def test_starting_healthcheck_is_unknown_not_unhealthy(make_config):
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=True, health="starting")),
        }
    )
    client = DockerClient(make_config(), session=session)
    assert check_database(make_config(), client).healthy is None


def test_stopped_database_is_unhealthy(make_config):
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=False)),
        }
    )
    client = DockerClient(make_config(), session=session)

    health = check_database(make_config(), client)
    assert health.healthy is False


def test_falls_back_to_tcp_when_no_healthcheck_is_defined(make_config, monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dc.socket, "create_connection", lambda *a, **k: FakeSocket())
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=True, health=None)),
        }
    )
    client = DockerClient(make_config(), session=session)

    health = check_database(make_config(), client)
    assert health.healthy is True
    assert health.source == "tcp"


def test_tcp_refusal_is_unhealthy(make_config, monkeypatch):
    def refuse(*args, **kwargs):
        raise socket.error("connection refused")

    monkeypatch.setattr(dc.socket, "create_connection", refuse)
    session = FakeSession(
        {
            "/containers/json": FakeResponse(200, [container("database", "database")]),
            "/json": FakeResponse(200, inspect(running=True, health=None)),
        }
    )
    client = DockerClient(make_config(), session=session)

    health = check_database(make_config(), client)
    assert health.healthy is False
    assert health.source == "tcp"


def test_absent_database_container_is_unknown_not_unhealthy(make_config):
    session = FakeSession({"/containers/json": FakeResponse(200, [])})
    client = DockerClient(make_config(), session=session)

    assert check_database(make_config(), client).healthy is None
