"""What actually reaches MQTT.

The empty-payload rule has teeth: publishing "" with the retain flag *deletes*
the retained message, so a topic would disappear rather than report "nothing
yet". Every state topic must always carry a non-empty payload.
"""

from __future__ import annotations

from src.http_check import HttpCategory, HttpResult
from src.main import Watchdog
from src.mqtt_client import LoggerHealth
from src.state_machine import State, StateMachine

EXPECTED_TOPICS = {
    "state",
    "healthy",
    "http_status",
    "authenticated",
    "database_healthy",
    "logger_healthy",
    "last_check",
    "last_failure",
    "last_restart",
    "restart_count_24h",
    "recovery_status",
}


class FakeMqtt:
    def __init__(self, logger_healthy: bool | None = True):
        self.state: dict[str, str] = {}
        self.events: list[tuple[str, dict]] = []
        self._logger_healthy = logger_healthy

    def logger_health(self) -> LoggerHealth:
        return LoggerHealth(
            healthy=self._logger_healthy, stale=False, car_id="1", detail="fake"
        )

    def publish_state(self, suffix: str, payload: str) -> None:
        self.state[suffix] = payload

    def publish_event(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class FakeChecker:
    def __init__(self, result: HttpResult):
        self.result = result

    def check(self) -> HttpResult:
        return self.result


AUTHENTICATED = HttpResult(category=HttpCategory.AUTHENTICATED, status_code=200)
SIGNIN = HttpResult(category=HttpCategory.LOGGED_OUT, status_code=200)
UNREACHABLE = HttpResult(category=HttpCategory.UNREACHABLE, error="ConnectionError")


def build(make_config, http_result: HttpResult, logger_healthy: bool | None = True, **overrides):
    config = make_config(**overrides)
    mqtt = FakeMqtt(logger_healthy=logger_healthy)
    machine = StateMachine(config)
    watchdog = Watchdog(config, FakeChecker(http_result), mqtt, machine)
    return watchdog, mqtt, machine


def test_all_specified_topics_are_published(make_config):
    watchdog, mqtt, _ = build(make_config, AUTHENTICATED)
    watchdog.check_once()
    assert set(mqtt.state) == EXPECTED_TOPICS


def test_no_topic_is_ever_published_empty(make_config):
    """An empty retained payload deletes the topic — never publish one."""
    watchdog, mqtt, _ = build(make_config, UNREACHABLE)
    watchdog.check_once()
    empty = [topic for topic, payload in mqtt.state.items() if payload == ""]
    assert empty == []


def test_unset_timestamps_publish_none_sentinel(make_config):
    watchdog, mqtt, _ = build(make_config, AUTHENTICATED)
    watchdog.check_once()
    assert mqtt.state["last_failure"] == "None"
    assert mqtt.state["last_restart"] == "None"


def test_http_status_is_unknown_when_unreachable(make_config):
    watchdog, mqtt, _ = build(make_config, UNREACHABLE)
    watchdog.check_once()
    assert mqtt.state["http_status"] == "unknown"
    assert mqtt.state["authenticated"] == "unknown"


def test_authenticated_is_false_only_on_a_signin_page(make_config):
    watchdog, mqtt, _ = build(make_config, SIGNIN)
    watchdog.check_once()
    assert mqtt.state["authenticated"] == "false"


def test_database_health_is_unknown_before_stage_two(make_config):
    watchdog, mqtt, _ = build(make_config, AUTHENTICATED)
    watchdog.check_once()
    assert mqtt.state["database_healthy"] == "unknown"


def test_last_failure_is_stamped_on_entering_a_failure_state(make_config):
    watchdog, mqtt, machine = build(make_config, SIGNIN, FAILURE_CONFIRMATION_COUNT=2)
    watchdog.check_once()
    assert mqtt.state["last_failure"] == "None"

    watchdog.check_once()
    assert machine.state is State.LOGGED_OUT
    assert mqtt.state["last_failure"] != "None"


# --- events ----------------------------------------------------------------


def test_logged_out_emits_authentication_required_and_refuses_restart(make_config):
    watchdog, mqtt, machine = build(make_config, SIGNIN, FAILURE_CONFIRMATION_COUNT=2)
    watchdog.check_once()
    watchdog.check_once()

    assert machine.state is State.LOGGED_OUT
    emitted = {event for event, _ in mqtt.events}
    assert emitted == {"failure_detected", "authentication_required"}

    payload = next(p for e, p in mqtt.events if e == "authentication_required")
    assert payload["auto_restart"] is False
    assert "token" in payload["action_required"].lower()
    assert payload["teslamate_url"]


def test_logger_unhealthy_does_not_ask_for_authentication(make_config):
    watchdog, mqtt, machine = build(
        make_config,
        AUTHENTICATED,
        logger_healthy=False,
        LOGGER_UNHEALTHY_CONFIRMATION_COUNT=2,
    )
    watchdog.check_once()
    watchdog.check_once()

    assert machine.state is State.LOGGER_UNHEALTHY
    emitted = {event for event, _ in mqtt.events}
    assert emitted == {"failure_detected"}


def test_events_are_emitted_once_per_transition_not_per_check(make_config):
    watchdog, mqtt, _ = build(make_config, SIGNIN, FAILURE_CONFIRMATION_COUNT=2)
    for _ in range(6):
        watchdog.check_once()

    failure_events = [event for event, _ in mqtt.events if event == "failure_detected"]
    assert len(failure_events) == 1


def test_no_response_body_reaches_the_event_payload(make_config):
    watchdog, mqtt, _ = build(make_config, SIGNIN, FAILURE_CONFIRMATION_COUNT=1)
    watchdog.check_once()

    serialised = str(mqtt.events)
    assert "<form" not in serialised
    assert "<html" not in serialised
