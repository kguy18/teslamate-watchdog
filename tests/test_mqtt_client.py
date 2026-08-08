"""Car-topic bookkeeping and the staleness gate.

The gate matters: TeslaMate publishes on-change, so a parked car legitimately
goes silent for hours. Ungated staleness would false-alarm every night.
"""

from __future__ import annotations

import time

import pytest

from src.mqtt_client import CarObservation, WatchdogMqtt, parse_bool_payload


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", True),
        ("on", True),
        ("online", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("off", False),
        ("offline", False),
        ("  true  ", True),
    ],
)
def test_boolean_payloads_parse_case_insensitively(payload, expected):
    assert parse_bool_payload(payload) is expected


@pytest.mark.parametrize("payload", ["maybe", "", "asleep", None])
def test_unrecognised_payloads_become_unknown(payload):
    assert parse_bool_payload(payload) is None


def make_client(make_config, **overrides) -> WatchdogMqtt:
    return WatchdogMqtt(make_config(**overrides))


def test_no_messages_yet_reports_unknown(make_config):
    health = make_client(make_config).logger_health()
    assert health.healthy is None
    assert health.stale is False


def test_healthy_payload_is_reported(make_config):
    client = make_client(make_config)
    client._cars["1"] = CarObservation(
        car_id="1", healthy_payload="true", healthy=True, healthy_monotonic=time.monotonic()
    )
    health = client.logger_health()
    assert health.healthy is True
    assert health.car_id == "1"


def test_the_configured_car_is_preferred(make_config):
    client = make_client(make_config, TESLAMATE_HEALTH_TOPIC="teslamate/cars/2/healthy")
    client._cars["1"] = CarObservation(car_id="1", healthy=False)
    client._cars["2"] = CarObservation(car_id="2", healthy=True)

    assert client.logger_health().car_id == "2"


def test_an_unhealthy_car_wins_when_the_configured_id_is_absent(make_config):
    client = make_client(make_config, TESLAMATE_HEALTH_TOPIC="teslamate/cars/9/healthy")
    client._cars["1"] = CarObservation(car_id="1", healthy=True)
    client._cars["2"] = CarObservation(car_id="2", healthy=False)

    health = client.logger_health()
    assert health.healthy is False
    assert health.car_id == "2"


# --- staleness gating -------------------------------------------------------


def connected(client, seconds_ago: float = 100_000) -> None:
    """Pretend the broker connection has been up for a long time."""
    client._connected_since = time.monotonic() - seconds_ago


def car_silent_for(seconds: float, state: str | None) -> CarObservation:
    then = time.monotonic() - seconds
    return CarObservation(
        car_id="1",
        healthy_payload="true",
        healthy=True,
        healthy_monotonic=then,
        state_payload=state,
        state_monotonic=then,
    )


def test_staleness_is_ignored_when_disabled(make_config):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="false")
    connected(client)
    client._cars["1"] = car_silent_for(50_000, "driving")

    assert client.logger_health().stale is False


# --- active states: short limit --------------------------------------------


@pytest.mark.parametrize("state", ["driving", "charging"])
def test_silence_while_active_is_stale(make_config, state):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(5000, state)

    health = client.logger_health()
    assert health.stale is True
    assert state in health.detail


def test_recent_message_while_driving_is_not_stale(make_config):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(5, "driving")

    assert client.logger_health().stale is False


# --- parked states: long limit ---------------------------------------------


@pytest.mark.parametrize("state", ["asleep", "online", "suspended", "offline", "updating"])
def test_a_parked_car_is_stale_only_after_the_long_limit(make_config, state):
    """The gap that matters while parked is much larger, but it is not infinite."""
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(7200, state)  # 2h, limit is 90m

    assert client.logger_health().stale is True


@pytest.mark.parametrize("state", ["asleep", "online", "suspended", "offline"])
def test_a_normal_suspend_window_is_not_stale(make_config, state):
    """TeslaMate suspends polling for up to ~30 min; that must not alarm."""
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(1800, state)

    assert client.logger_health().stale is False


def test_the_active_limit_is_not_applied_to_a_parked_car(make_config):
    """700s would be stale while driving, but is unremarkable while asleep."""
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(700, "asleep")

    assert client.logger_health().stale is False


def test_an_unknown_car_state_gets_the_lenient_limit(make_config):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = car_silent_for(1800, None)

    assert client.logger_health().stale is False


def test_both_limits_are_configurable(make_config):
    client = make_client(
        make_config,
        STALENESS_DETECTION_ENABLED="true",
        MQTT_PARKED_STALE_SECONDS=600,
    )
    connected(client)
    client._cars["1"] = car_silent_for(900, "asleep")

    assert client.logger_health().stale is True


# --- the two guards against crying wolf ------------------------------------


def test_never_received_healthy_is_not_treated_as_stale(make_config):
    """Indistinguishable from a wrong topic — and LOGGER_UNHEALTHY restarts."""
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)
    client._cars["1"] = CarObservation(car_id="1", state_payload="driving")

    assert client.logger_health().stale is False


def test_our_own_downtime_does_not_count_as_teslamate_silence(make_config):
    """A long disconnect must not make the car look instantly stale on reconnect.

    `healthy` is unretained, so nothing arrives until the next summary.
    """
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    client._cars["1"] = car_silent_for(50_000, "driving")
    # Reconnected a moment ago after a long outage.
    client._connected_since = time.monotonic() - 5

    assert client.logger_health().stale is False


def test_while_disconnected_no_staleness_verdict_is_made(make_config):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    client._cars["1"] = car_silent_for(50_000, "driving")
    client._connected_since = None

    assert client.logger_health().stale is False


def test_staleness_resumes_once_the_reconnect_has_aged(make_config):
    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    client._cars["1"] = car_silent_for(50_000, "driving")
    client._connected_since = time.monotonic() - 5000

    assert client.logger_health().stale is True


def test_persistent_silence_warns_but_never_reports_a_failure(make_config, caplog):
    """A wrong topic must produce a log line, not a restart."""
    import logging

    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)

    with caplog.at_level(logging.WARNING):
        health = client.logger_health()

    assert health.healthy is None
    assert health.stale is False
    assert "TESLAMATE_HEALTH_TOPIC" in caplog.text


def test_the_silence_warning_is_not_repeated_every_cycle(make_config, caplog):
    import logging

    client = make_client(make_config, STALENESS_DETECTION_ENABLED="true")
    connected(client)

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            client.logger_health()

    assert caplog.text.count("TESLAMATE_HEALTH_TOPIC") == 1
