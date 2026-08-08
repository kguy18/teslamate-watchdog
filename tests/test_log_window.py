"""The log window used for state decisions must be the short one.

Regression guard for a real defect: classification originally ran over
DIAGNOSTIC_LOG_LOOKBACK (2h), so a burst of auth failures kept the state pinned
at AUTH_REFRESH_FAILED for two hours after tokens had been re-entered and
everything else was healthy again.
"""

from __future__ import annotations

from src.http_check import HttpCategory, HttpResult
from src.log_classifier import LogClassifier
from src.main import Watchdog
from src.mqtt_client import LoggerHealth
from src.state_machine import State, StateMachine

PATTERNS = "patterns.yaml"
AUTH_BURST = "\n".join(["10:00:00 invalid_grant"] * 3)


class FakeMqtt:
    def __init__(self):
        self.state: dict[str, str] = {}
        self.events: list[tuple[str, dict]] = []

    def logger_health(self):
        return LoggerHealth(healthy=True, stale=False, car_id="1", detail="fake")

    def publish_state(self, suffix, payload):
        self.state[suffix] = payload

    def publish_event(self, event_type, payload):
        self.events.append((event_type, payload))


class FakeChecker:
    def check(self):
        return HttpResult(category=HttpCategory.AUTHENTICATED, status_code=200)


class RecordingDocker:
    """Serves logs only inside a chosen window, recording what was requested."""

    def __init__(self, log_age_seconds: int):
        self.log_age_seconds = log_age_seconds
        self.requested_windows: list[int] = []

    def status(self, container):
        from src.docker_client import ContainerRef, ContainerStatus

        return ContainerStatus(
            exists=True,
            running=True,
            status="running",
            health="healthy" if container == "database" else None,
            ref=ContainerRef(id="abc", name=container, compose_service=container),
        )

    def logs(self, container, since_seconds):
        self.requested_windows.append(since_seconds)
        # The burst is only visible if the requested window reaches back far enough.
        return AUTH_BURST if since_seconds >= self.log_age_seconds else ""

    def ping(self):
        return True


def build(make_config, docker, **overrides):
    config = make_config(**overrides)
    mqtt = FakeMqtt()
    machine = StateMachine(config, initial=State.HEALTHY)
    watchdog = Watchdog(
        config,
        FakeChecker(),
        mqtt,
        machine,
        docker=docker,
        classifier=LogClassifier.from_file(PATTERNS),
    )
    return watchdog, mqtt, machine


def test_state_decisions_use_the_short_window(make_config):
    docker = RecordingDocker(log_age_seconds=0)
    watchdog, _, _ = build(make_config, docker)
    watchdog.check_once()

    assert docker.requested_windows == [900]


def test_a_stale_auth_burst_no_longer_latches_the_state(make_config):
    """The burst is 90 minutes old; the state window is 15 minutes."""
    docker = RecordingDocker(log_age_seconds=5400)
    watchdog, mqtt, machine = build(make_config, docker)

    for _ in range(4):
        watchdog.check_once()

    assert machine.state is State.HEALTHY
    assert mqtt.state["state"] == "HEALTHY"


def test_a_current_auth_burst_still_confirms(make_config):
    """The short window must not blind the detector to a live problem."""
    docker = RecordingDocker(log_age_seconds=0)
    watchdog, mqtt, machine = build(make_config, docker, FAILURE_CONFIRMATION_COUNT=2)

    watchdog.check_once()
    watchdog.check_once()

    assert machine.state is State.AUTH_REFRESH_FAILED
    assert "authentication_required" in [event for event, _ in mqtt.events]


def test_the_window_is_configurable(make_config):
    docker = RecordingDocker(log_age_seconds=0)
    watchdog, _, _ = build(make_config, docker, LOG_ANALYSIS_LOOKBACK="30m")
    watchdog.check_once()

    assert docker.requested_windows == [1800]


def test_widening_the_window_restores_the_old_latching_behaviour(make_config):
    """Confirms the fix is genuinely the window, not an unrelated change."""
    docker = RecordingDocker(log_age_seconds=5400)
    watchdog, _, machine = build(
        make_config, docker, LOG_ANALYSIS_LOOKBACK="2h", FAILURE_CONFIRMATION_COUNT=2
    )

    watchdog.check_once()
    watchdog.check_once()

    assert machine.state is State.AUTH_REFRESH_FAILED
