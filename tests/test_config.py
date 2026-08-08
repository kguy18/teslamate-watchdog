from __future__ import annotations

import pytest

from src.config import ConfigError, TopicLayout, parse_duration


def test_topic_layout_derives_wildcards_and_car_id():
    layout = TopicLayout.derive("teslamate/cars/1/healthy")
    assert layout.health_filter == "teslamate/cars/+/healthy"
    assert layout.state_filter == "teslamate/cars/+/state"
    assert layout.primary_car_id == "1"


def test_topic_layout_honours_a_custom_root():
    layout = TopicLayout.derive("home/tesla/cars/2/healthy")
    assert layout.health_filter == "home/tesla/cars/+/healthy"
    assert layout.state_filter == "home/tesla/cars/+/state"
    assert layout.primary_car_id == "2"


def test_topic_layout_falls_back_for_unrecognised_shapes():
    layout = TopicLayout.derive("custom/health")
    assert layout.health_filter == "custom/health"
    assert layout.state_filter is None
    assert layout.primary_car_id is None


def test_car_id_is_extracted_from_an_incoming_topic():
    layout = TopicLayout.derive("teslamate/cars/1/healthy")
    assert layout.car_id_from_topic("teslamate/cars/7/healthy") == "7"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30), ("30s", 30), ("5m", 300), ("2h", 7200), ("7d", 604800), ("2H", 7200)],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw, field="X") == expected


def test_parse_duration_rejects_nonsense():
    with pytest.raises(ConfigError):
        parse_duration("soon", field="X")


def test_mqtt_host_is_required(monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    from src.config import Config

    with pytest.raises(ConfigError, match="MQTT_HOST"):
        Config.from_env()


def test_summary_never_exposes_the_password(make_config):
    config = make_config(MQTT_PASSWORD="hunter2", MQTT_USERNAME="watchdog")
    summary = config.summary()

    assert "hunter2" not in str(summary)
    assert summary["mqtt_password"] == "<set>"
    assert summary["mqtt_username"] == "<set>"


def test_summary_reports_unset_credentials(make_config):
    summary = make_config().summary()
    assert summary["mqtt_password"] == "<unset>"


def test_explicit_staleness_without_a_state_topic_is_an_error(make_config):
    with pytest.raises(ConfigError, match="STALENESS_DETECTION_ENABLED"):
        make_config(
            STALENESS_DETECTION_ENABLED="true",
            TESLAMATE_HEALTH_TOPIC="custom/health",
        )


def test_staleness_is_on_by_default(make_config):
    assert make_config().staleness_detection_enabled is True


def test_default_staleness_degrades_instead_of_crashing(make_config):
    """An unparseable health topic must not turn a working config into a crash."""
    config = make_config(TESLAMATE_HEALTH_TOPIC="custom/health")

    assert config.staleness_detection_enabled is False
    assert any("staleness detection disabled automatically" in note for note in config.notes)


def test_staleness_can_still_be_turned_off(make_config):
    assert make_config(STALENESS_DETECTION_ENABLED="false").staleness_detection_enabled is False


# --- log windows ------------------------------------------------------------


def test_state_and_diagnostic_log_windows_are_separate(make_config):
    config = make_config()
    assert config.log_analysis_lookback_seconds == 900
    assert config.diagnostic_log_lookback_seconds == 7200
    assert config.log_analysis_lookback_seconds < config.diagnostic_log_lookback_seconds


def test_state_window_may_not_exceed_the_diagnostic_window(make_config):
    with pytest.raises(ConfigError, match="LOG_ANALYSIS_LOOKBACK"):
        make_config(LOG_ANALYSIS_LOOKBACK="4h", DIAGNOSTIC_LOG_LOOKBACK="2h")


def test_log_windows_accept_duration_suffixes(make_config):
    config = make_config(LOG_ANALYSIS_LOOKBACK="30m", DIAGNOSTIC_LOG_LOOKBACK="6h")
    assert config.log_analysis_lookback_seconds == 1800
    assert config.diagnostic_log_lookback_seconds == 21600


# --- MQTT TLS ---------------------------------------------------------------


def test_tls_is_off_by_default(make_config):
    config = make_config()
    assert config.mqtt_tls is False
    assert config.mqtt_tls_insecure is False
    assert config.mqtt_tls_ca_cert == ""


def test_a_missing_ca_file_is_rejected_at_startup(make_config):
    with pytest.raises(ConfigError, match="MQTT_TLS_CA_CERT"):
        make_config(MQTT_TLS="true", MQTT_TLS_CA_CERT="/nonexistent/ca.crt")


def test_a_present_ca_file_is_accepted(make_config, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    config = make_config(MQTT_TLS="true", MQTT_TLS_CA_CERT=str(ca))
    assert config.mqtt_tls_ca_cert == str(ca)


def test_summary_reports_tls_settings_without_leaking_paths_as_secrets(make_config):
    summary = make_config().summary()
    assert summary["mqtt_tls_ca_cert"] == "<system CAs>"
    assert summary["mqtt_tls_insecure"] is False


def test_invalid_signin_regex_is_rejected(make_config):
    with pytest.raises(ConfigError, match="regex"):
        make_config(TESLAMATE_SIGNIN_PATTERN="/sign[_-?in")


def test_non_http_url_is_rejected(make_config):
    with pytest.raises(ConfigError, match="TESLAMATE_URL"):
        make_config(TESLAMATE_URL="teslamate:4000")


def test_defaults_match_the_specification(make_config):
    config = make_config()
    assert config.check_interval_seconds == 60
    assert config.failure_confirmation_count == 3
    assert config.recovery_confirmation_count == 2
    assert config.restart_cooldown_seconds == 21600
    assert config.post_restart_wait_seconds == 90
    assert config.max_restarts_per_24_hours == 2
    assert config.diagnostic_log_lookback_seconds == 7200
    assert config.ha_discovery_enabled is True
