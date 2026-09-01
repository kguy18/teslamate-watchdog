"""Environment-driven configuration.

Every knob is an environment variable so the service is configurable purely
from docker-compose. Secrets are never logged: :meth:`Config.summary` returns a
redacted view for the startup log line, and that is the only config dump that
exists anywhere in this codebase.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final


class ConfigError(ValueError):
    """Raised when the environment is missing or malformed."""


# Substrings that, when found in a 200 response body, mean TeslaMate is
# rendering its sign-in form rather than the dashboard. Compared
# case-insensitively. Confirm during pre-flight that a *logged-in* instance
# matches none of these; override with TESLAMATE_SIGNIN_BODY_MARKERS if it does.
DEFAULT_SIGNIN_BODY_MARKERS: Final[tuple[str, ...]] = (
    "refresh token",
    "access token",
    "tokens[refresh_token]",
    "tokens[access_token]",
)

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"0", "false", "no", "off"})

_DURATION_RE: Final = re.compile(r"^(\d+)([smhd]?)$", re.IGNORECASE)
_DURATION_UNITS: Final = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_duration(raw: str, *, field: str) -> int:
    """Parse ``30s`` / ``5m`` / ``2h`` / ``7d`` (or a bare integer) into seconds."""
    match = _DURATION_RE.match(raw.strip())
    if not match:
        raise ConfigError(f"{field} must look like '90s', '5m', '2h' or '7d', got {raw!r}")
    amount, unit = match.groups()
    return int(amount) * _DURATION_UNITS[unit.lower()]


@dataclass(frozen=True)
class TopicLayout:
    """Car topics derived from ``TESLAMATE_HEALTH_TOPIC``.

    TeslaMate publishes under ``<root>/cars/<id>/<key>``. We subscribe with a
    ``+`` wildcard in the car-id position so a mis-set car id degrades to
    "watches every car" rather than "watches nothing".
    """

    health_filter: str
    state_filter: str | None
    primary_car_id: str | None

    @classmethod
    def derive(cls, health_topic: str) -> TopicLayout:
        parts = [segment for segment in health_topic.split("/") if segment]
        if len(parts) >= 3 and parts[-1] == "healthy":
            prefix = parts[:-2]
            car_id = parts[-2]
            return cls(
                health_filter="/".join([*prefix, "+", "healthy"]),
                state_filter="/".join([*prefix, "+", "state"]),
                primary_car_id=car_id,
            )
        # Unrecognised shape: watch exactly what we were told to watch, and
        # accept that staleness gating (which needs the state topic) is off.
        return cls(health_filter=health_topic, state_filter=None, primary_car_id=None)

    def car_id_from_topic(self, topic: str) -> str:
        parts = [segment for segment in topic.split("/") if segment]
        return parts[-2] if len(parts) >= 2 else topic


@dataclass(frozen=True)
class Config:
    # --- TeslaMate / Docker targets --------------------------------------
    teslamate_url: str
    teslamate_container: str
    database_container: str
    docker_host: str
    docker_timeout_seconds: int
    database_host: str
    database_port: int
    teslamate_signin_pattern: str
    #: Probed before restarting for a logout. A logout caused by DNS or a
    #: network outage cannot be fixed by restarting into the same outage.
    tesla_auth_host: str
    tesla_auth_port: int
    logged_out_restart_enabled: bool
    signin_body_markers: tuple[str, ...]

    # --- MQTT -------------------------------------------------------------
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_tls_ca_cert: str
    mqtt_tls_insecure: bool
    mqtt_base_topic: str
    teslamate_health_topic: str
    topics: TopicLayout

    # --- timing -----------------------------------------------------------
    check_interval_seconds: int
    http_timeout_seconds: int
    #: Heartbeat gap tolerated while the car is driving or charging.
    mqtt_stale_seconds: int
    #: Heartbeat gap tolerated in every other state. Must clear TeslaMate's
    #: `suspend_min` setting — while suspended it deliberately stops polling for
    #: that long so the car can fall asleep, and publishes nothing meanwhile.
    mqtt_parked_stale_seconds: int
    staleness_detection_enabled: bool

    # --- confirmation counters -------------------------------------------
    failure_confirmation_count: int
    recovery_confirmation_count: int
    #: LOGGER_UNHEALTHY needs far more confirmation than other failures.
    #: TeslaMate's `healthy` flag is its API-error fuse — a circuit breaker that
    #: trips on transient Tesla API timeouts and self-clears in minutes. Acting
    #: at the normal threshold restarts TeslaMate for blips it would have ridden
    #: out on its own, and a restart cannot fix a Tesla-side timeout anyway.
    logger_unhealthy_confirmation_count: int

    # --- restart policy ---------------------------------------------------
    auto_restart_enabled: bool
    restart_cooldown_seconds: int
    post_restart_wait_seconds: int
    max_restarts_per_24_hours: int

    # --- diagnostics ------------------------------------------------------
    diagnostic_dir: str
    diagnostic_log_lookback: str
    diagnostic_log_lookback_seconds: int
    #: Window used to decide *state* from logs. Deliberately much shorter than
    #: the diagnostic window: whatever period this covers is also how long a
    #: resolved auth incident keeps the state pinned after everything recovers.
    log_analysis_lookback: str
    log_analysis_lookback_seconds: int
    diagnostic_retention_days: int

    # --- Home Assistant discovery ----------------------------------------
    ha_discovery_enabled: bool
    ha_discovery_prefix: str

    # --- misc -------------------------------------------------------------
    log_level: str
    patterns_file: str
    data_dir: str
    #: Startup messages that could not be logged during parsing.
    notes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Config:
        health_topic = _env("TESLAMATE_HEALTH_TOPIC", "teslamate/cars/1/healthy")
        lookback = _env("DIAGNOSTIC_LOG_LOOKBACK", "2h")
        analysis_lookback = _env("LOG_ANALYSIS_LOOKBACK", "15m")

        mqtt_host = _env("MQTT_HOST", "")
        if not mqtt_host:
            raise ConfigError("MQTT_HOST is required — the watchdog has no other output channel")

        database_container = _env("DATABASE_CONTAINER", "database")
        topics = TopicLayout.derive(health_topic)
        notes: list[str] = []

        # Staleness is on by default: TeslaMate republishes `healthy` on every
        # vehicle summary, so silence while driving or charging is a real signal.
        # It needs the car's `state` topic for gating, though — and a health
        # topic we cannot parse should degrade to "staleness off", not to a
        # crash. Only an explicit opt-in is treated as an error.
        staleness_requested = _env_bool("STALENESS_DETECTION_ENABLED", True)
        staleness_explicit = "STALENESS_DETECTION_ENABLED" in os.environ
        staleness_enabled = staleness_requested
        if staleness_requested and topics.state_filter is None:
            if staleness_explicit:
                raise ConfigError(
                    "STALENESS_DETECTION_ENABLED requires a TESLAMATE_HEALTH_TOPIC of "
                    "the form <root>/cars/<id>/healthy so the car's state topic can be "
                    "derived for gating"
                )
            staleness_enabled = False
            notes.append(
                f"staleness detection disabled automatically: TESLAMATE_HEALTH_TOPIC "
                f"{health_topic!r} has no derivable car state topic to gate on"
            )

        config = cls(
            teslamate_url=_env("TESLAMATE_URL", "http://teslamate:4000/"),
            teslamate_container=_env("TESLAMATE_CONTAINER", "teslamate"),
            database_container=database_container,
            docker_host=_env("DOCKER_HOST", "tcp://socket-proxy:2375"),
            docker_timeout_seconds=_env_int("DOCKER_TIMEOUT_SECONDS", 10, minimum=1),
            # Only used for the TCP fallback when the database container has no
            # Docker healthcheck. Defaults to the container name, which resolves
            # on the compose network.
            database_host=_env("DATABASE_HOST", database_container),
            database_port=_env_int("DATABASE_PORT", 5432, minimum=1),
            teslamate_signin_pattern=_env("TESLAMATE_SIGNIN_PATTERN", r"/sign[_-]?in"),
            tesla_auth_host=_env("TESLA_AUTH_HOST", "auth.tesla.com"),
            tesla_auth_port=_env_int("TESLA_AUTH_PORT", 443, minimum=1),
            logged_out_restart_enabled=_env_bool("LOGGED_OUT_RESTART_ENABLED", True),
            signin_body_markers=_env_csv(
                "TESLAMATE_SIGNIN_BODY_MARKERS", DEFAULT_SIGNIN_BODY_MARKERS
            ),
            mqtt_host=mqtt_host,
            mqtt_port=_env_int("MQTT_PORT", 1883, minimum=1),
            mqtt_username=_env("MQTT_USERNAME", ""),
            mqtt_password=os.environ.get("MQTT_PASSWORD", ""),
            mqtt_tls=_env_bool("MQTT_TLS", False),
            mqtt_tls_ca_cert=_env("MQTT_TLS_CA_CERT", ""),
            mqtt_tls_insecure=_env_bool("MQTT_TLS_INSECURE", False),
            mqtt_base_topic=_env("MQTT_BASE_TOPIC", "teslamate/watchdog").rstrip("/"),
            teslamate_health_topic=health_topic,
            topics=topics,
            check_interval_seconds=_env_int("CHECK_INTERVAL_SECONDS", 60, minimum=5),
            http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", 10, minimum=1),
            mqtt_stale_seconds=_env_int("MQTT_STALE_SECONDS", 600, minimum=1),
            # 90 minutes: TeslaMate's suspend_min defaults to 21 (30 with the
            # streaming API) but is user-configurable up to an hour, and during
            # suspend no summary is published at all. This clears the whole
            # configurable range with headroom for a missed poll.
            mqtt_parked_stale_seconds=_env_int(
                "MQTT_PARKED_STALE_SECONDS", 5400, minimum=1
            ),
            staleness_detection_enabled=staleness_enabled,
            failure_confirmation_count=_env_int("FAILURE_CONFIRMATION_COUNT", 3, minimum=1),
            recovery_confirmation_count=_env_int("RECOVERY_CONFIRMATION_COUNT", 2, minimum=1),
            # Observed fuse recovery is ~7 min; 15 checks gives 2x margin while
            # still catching a genuinely wedged logger within 15 minutes.
            logger_unhealthy_confirmation_count=_env_int(
                "LOGGER_UNHEALTHY_CONFIRMATION_COUNT", 15, minimum=1
            ),
            auto_restart_enabled=_env_bool("AUTO_RESTART_ENABLED", True),
            restart_cooldown_seconds=_env_int("RESTART_COOLDOWN_SECONDS", 21600, minimum=0),
            post_restart_wait_seconds=_env_int("POST_RESTART_WAIT_SECONDS", 90, minimum=0),
            max_restarts_per_24_hours=_env_int("MAX_RESTARTS_PER_24_HOURS", 2, minimum=0),
            diagnostic_dir=_env("DIAGNOSTIC_DIR", "/data/diagnostics"),
            diagnostic_log_lookback=lookback,
            diagnostic_log_lookback_seconds=parse_duration(
                lookback, field="DIAGNOSTIC_LOG_LOOKBACK"
            ),
            log_analysis_lookback=analysis_lookback,
            log_analysis_lookback_seconds=parse_duration(
                analysis_lookback, field="LOG_ANALYSIS_LOOKBACK"
            ),
            diagnostic_retention_days=_env_int("DIAGNOSTIC_RETENTION_DAYS", 14, minimum=0),
            ha_discovery_enabled=_env_bool("HA_DISCOVERY_ENABLED", True),
            ha_discovery_prefix=_env("HA_DISCOVERY_PREFIX", "homeassistant").rstrip("/"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            patterns_file=_env("PATTERNS_FILE", "/app/patterns.yaml"),
            data_dir=_env("DATA_DIR", "/data"),
            notes=tuple(notes),
        )
        config.validate()
        return config

    def validate(self) -> None:
        try:
            re.compile(self.teslamate_signin_pattern)
        except re.error as exc:
            raise ConfigError(
                f"TESLAMATE_SIGNIN_PATTERN is not a valid regex: {exc}"
            ) from exc
        if not self.teslamate_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"TESLAMATE_URL must start with http:// or https://, got {self.teslamate_url!r}"
            )
        if not self.mqtt_base_topic:
            raise ConfigError("MQTT_BASE_TOPIC must not be empty")
        if self.staleness_detection_enabled and self.topics.state_filter is None:
            raise ConfigError(
                "STALENESS_DETECTION_ENABLED requires a TESLAMATE_HEALTH_TOPIC of the form "
                "<root>/cars/<id>/healthy so the car's state topic can be derived for gating"
            )
        if self.docker_host and not self.docker_host.startswith(("tcp://", "http://")):
            # Refusing unix:// is the point: mounting the raw Docker socket into
            # this container is root-equivalent access to the host. The socket
            # proxy is the supported path, so make the wrong one un-configurable.
            raise ConfigError(
                f"DOCKER_HOST must be tcp:// or http:// (the socket proxy), got "
                f"{self.docker_host!r}. Mounting /var/run/docker.sock directly is "
                f"not supported — see the security section of the README."
            )
        if self.teslamate_container == self.database_container:
            raise ConfigError(
                "TESLAMATE_CONTAINER and DATABASE_CONTAINER must differ — the "
                "watchdog must never be able to resolve a restart onto the database"
            )
        if self.mqtt_tls_ca_cert and not Path(self.mqtt_tls_ca_cert).is_file():
            # Fail here rather than with an opaque TLS handshake error later.
            raise ConfigError(
                f"MQTT_TLS_CA_CERT points at {self.mqtt_tls_ca_cert!r}, which is not "
                f"a readable file inside the container — check the volume mount"
            )
        if self.log_analysis_lookback_seconds > self.diagnostic_log_lookback_seconds:
            raise ConfigError(
                "LOG_ANALYSIS_LOOKBACK must not exceed DIAGNOSTIC_LOG_LOOKBACK — the "
                "state window is meant to be the shorter of the two"
            )

    @property
    def docker_base_url(self) -> str:
        """The socket proxy as an HTTP base URL."""
        return self.docker_host.replace("tcp://", "http://", 1).rstrip("/")

    # --- derived topics ---------------------------------------------------

    def topic(self, suffix: str) -> str:
        return f"{self.mqtt_base_topic}/{suffix}"

    @property
    def availability_topic(self) -> str:
        return self.topic("availability")

    @property
    def event_topic(self) -> str:
        return self.topic("event")

    def summary(self) -> dict[str, object]:
        """Redacted config view. The only config dump in the codebase."""
        return {
            "teslamate_url": self.teslamate_url,
            "teslamate_container": self.teslamate_container,
            "database_container": self.database_container,
            "docker_host": self.docker_host,
            "mqtt_host": self.mqtt_host,
            "mqtt_port": self.mqtt_port,
            "mqtt_username": "<set>" if self.mqtt_username else "<unset>",
            "mqtt_password": "<set>" if self.mqtt_password else "<unset>",
            "mqtt_tls": self.mqtt_tls,
            "mqtt_tls_ca_cert": self.mqtt_tls_ca_cert or "<system CAs>",
            "mqtt_tls_insecure": self.mqtt_tls_insecure,
            "mqtt_base_topic": self.mqtt_base_topic,
            "health_filter": self.topics.health_filter,
            "state_filter": self.topics.state_filter,
            "primary_car_id": self.topics.primary_car_id,
            "check_interval_seconds": self.check_interval_seconds,
            "failure_confirmation_count": self.failure_confirmation_count,
            "recovery_confirmation_count": self.recovery_confirmation_count,
            "logger_unhealthy_confirmation_count": self.logger_unhealthy_confirmation_count,
            "staleness_detection_enabled": self.staleness_detection_enabled,
            "mqtt_stale_seconds": self.mqtt_stale_seconds,
            "mqtt_parked_stale_seconds": self.mqtt_parked_stale_seconds,
            "log_analysis_lookback": self.log_analysis_lookback,
            "diagnostic_log_lookback": self.diagnostic_log_lookback,
            "auto_restart_enabled": self.auto_restart_enabled,
            "logged_out_restart_enabled": self.logged_out_restart_enabled,
            "restart_cooldown_seconds": self.restart_cooldown_seconds,
            "max_restarts_per_24_hours": self.max_restarts_per_24_hours,
            "ha_discovery_enabled": self.ha_discovery_enabled,
        }
