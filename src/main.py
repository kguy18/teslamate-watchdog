"""Entry point: run the check loop, publish state and events to MQTT.

One cycle gathers HTTP, MQTT, Docker and log evidence into a single Observation,
feeds it to the state machine, publishes the result, and — only for the two
restartable states, and only if every guard passes — hands off to recovery.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from types import FrameType
from typing import Any

from . import __version__, ha_discovery, logging_setup
from .config import Config, ConfigError
from .diagnostics import DiagnosticsWriter
from .docker_client import UNKNOWN_STATUS, DockerClient, check_database, tcp_probe
from .http_check import HttpCategory, HttpChecker
from .log_classifier import LogClassifier
from .mqtt_client import WatchdogMqtt
from .recovery import STATUS_IDLE, RecoveryController
from .restart_manager import RestartManager
from .state_machine import (
    AUTH_STATES,
    FAILURE_STATES,
    RESTART_NEEDS_TESLA_AUTH,
    Observation,
    State,
    StateMachine,
    Transition,
    restart_allowed_for_state,
)

log = logging.getLogger(__name__)

#: Published for timestamp topics that have no value yet. Home Assistant reads
#: this as an unknown state; an empty payload would clear the retained message.
PAYLOAD_UNSET = "None"

AUTH_ACTION_MESSAGE = (
    "TeslaMate is signed out and is not recording drives. The watchdog did not "
    "restart it — either Tesla's auth host is unreachable (in which case a "
    "restart cannot re-authenticate and this should clear when the network "
    "recovers), or a restart was already tried and did not help. If it does not "
    "clear, generate fresh Tesla API tokens with a token app and paste them "
    "into the TeslaMate UI."
)


def _tristate(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Watchdog:
    def __init__(
        self,
        config: Config,
        checker: HttpChecker,
        mqtt: WatchdogMqtt,
        machine: StateMachine,
        *,
        docker: DockerClient | None = None,
        classifier: LogClassifier | None = None,
        restarts: RestartManager | None = None,
        recovery: RecoveryController | None = None,
    ) -> None:
        self._config = config
        self._checker = checker
        self._mqtt = mqtt
        self._machine = machine
        self._docker = docker
        self._classifier = classifier
        self._restarts = restarts
        self._recovery = recovery
        self._stop = threading.Event()

        self._last_failure: str = ""
        self._database_healthy_streak = 0
        self._last_docker_state: dict[str, Any] = {}

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        interval = self._config.check_interval_seconds
        log.info(
            "watchdog running",
            extra={"context": {"version": __version__, "interval_seconds": interval}},
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                log.exception("check cycle failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, interval - elapsed))
        log.info("watchdog stopped")

    # --- one cycle --------------------------------------------------------

    def check_once(self) -> Observation:
        observation = self._observe()

        if self._recovery is not None and self._recovery.active:
            # Held at RECOVERING; the controller owns the exit.
            self._recovery.step(observation)
        else:
            transition = self._machine.step(observation)
            if transition is not None:
                self._on_transition(transition, observation)

        self._publish_status(observation)
        log.info(
            "check complete",
            extra={
                "context": {
                    "state": self._machine.state.value,
                    "http": observation.http.category.value,
                    "status_code": observation.http.status_code,
                    "logger_healthy": observation.logger_healthy,
                    "database_healthy": observation.database_healthy,
                    "container_running": observation.teslamate_container_running,
                    "candidate": (
                        self._machine.candidate.value if self._machine.candidate else None
                    ),
                    "candidate_streak": self._machine.candidate_streak,
                    "healthy_streak": self._machine.healthy_streak,
                }
            },
        )
        return observation

    def _observe(self) -> Observation:
        http_result = self._checker.check()
        logger_health = self._mqtt.logger_health()

        database_healthy: bool | None = None
        container_running: bool | None = None
        log_signals = None
        docker_state: dict[str, Any] = {}

        if self._docker is not None:
            teslamate_status = self._docker.status(self._config.teslamate_container)
            container_running = teslamate_status.running if teslamate_status.exists else None

            database = check_database(self._config, self._docker)
            database_healthy = database.healthy

            docker_state = {
                "teslamate": teslamate_status.as_dict(),
                "database": database.as_dict(),
            }
            self._last_docker_state = docker_state

            if self._classifier is not None and not self._classifier.is_empty:
                # The SHORT window. Using the diagnostic window here would keep a
                # resolved auth incident latched for its full duration after
                # everything else had recovered.
                text = self._docker.logs(
                    self._config.teslamate_container,
                    since_seconds=self._config.log_analysis_lookback_seconds,
                )
                if text:
                    classification = self._classifier.classify(text)
                    log_signals = self._classifier.signals(classification)
                    docker_state["log_classification"] = classification.as_dict()

        # Tracked here rather than in the guard so the streak reflects observed
        # history, not whatever a single restart-time check happened to see.
        if database_healthy is True:
            self._database_healthy_streak += 1
        else:
            self._database_healthy_streak = 0

        return Observation(
            http=http_result,
            logger_healthy=logger_health.healthy,
            logger_stale=logger_health.stale,
            logger_detail=logger_health.detail,
            database_healthy=database_healthy,
            teslamate_container_running=container_running,
            log_signals=log_signals,
        )

    # --- transitions ------------------------------------------------------

    def _on_transition(self, transition: Transition, observation: Observation) -> None:
        log.warning(
            "state changed",
            extra={
                "context": {
                    "from": transition.previous.value,
                    "to": transition.current.value,
                    "reason": transition.reason,
                }
            },
        )

        current = transition.current
        if current not in FAILURE_STATES:
            return

        self._last_failure = _now_iso()
        base = {
            "state": current.value,
            "previous_state": transition.previous.value,
            "reason": transition.reason,
            "observation": observation.as_dict(),
        }
        self._mqtt.publish_event("failure_detected", base)

        # Decided first: if a restart is being attempted, telling the user to
        # re-enter tokens is premature and usually wrong. If the restart fails,
        # recovery escalates to manual_intervention_required instead.
        restarting = self._consider_recovery(current, transition.reason, observation)

        if current in AUTH_STATES and not restarting:
            self._mqtt.publish_event(
                "authentication_required",
                {
                    **base,
                    "action_required": AUTH_ACTION_MESSAGE,
                    "teslamate_url": self._config.teslamate_url,
                    "auto_restart": False,
                },
            )
        elif current is State.DATABASE_UNHEALTHY:
            self._mqtt.publish_event(
                "database_unhealthy",
                {
                    **base,
                    "action_required": (
                        "Check the TeslaMate PostgreSQL container. The watchdog "
                        "never restarts the database."
                    ),
                },
            )

    def _consider_recovery(
        self, state: State, reason: str, observation: Observation
    ) -> bool:
        """Capture diagnostics and start a restart if every guard passes."""
        if self._recovery is None or self._restarts is None:
            return False

        # Diagnostics are captured for auth states too — those are exactly the
        # incidents a human has to act on — but they never lead to a restart.
        if not restart_allowed_for_state(state):
            self._recovery.capture(state, reason, observation, self._last_docker_state)
            log.info(
                "restart not permitted for this state",
                extra={"context": {"state": state.value}},
            )
            return False

        # Only probed when it matters — a logout may be TeslaMate failing to
        # reach Tesla, in which case restarting into the same outage is futile.
        reachable: bool | None = None
        if state in RESTART_NEEDS_TESLA_AUTH:
            reachable, detail = tcp_probe(
                self._config.tesla_auth_host,
                self._config.tesla_auth_port,
                self._config.http_timeout_seconds,
            )
            log.info("probed tesla auth host", extra={"context": {"detail": detail}})

        decision = self._restarts.evaluate(
            state,
            database_healthy_streak=self._database_healthy_streak,
            tesla_auth_reachable=reachable,
        )
        self._recovery.capture(state, reason, observation, self._last_docker_state)

        if not decision.allowed:
            log.warning(
                "restart withheld",
                extra={"context": {"state": state.value, "reason": decision.reason}},
            )
            return False

        return self._recovery.begin(state, reason)

    # --- publishing -------------------------------------------------------

    def _publish_status(self, observation: Observation) -> None:
        state = self._machine.state
        category = observation.http.category

        if category is HttpCategory.AUTHENTICATED:
            authenticated: bool | None = True
        elif category is HttpCategory.LOGGED_OUT:
            authenticated = False
        else:
            authenticated = None

        restart_count = self._restarts.restarts_in_24h() if self._restarts else 0
        last_restart = self._restarts.last_restart_iso() if self._restarts else None
        recovery_status = self._recovery.status if self._recovery else STATUS_IDLE

        values = {
            "state": state.value,
            "healthy": "true" if state is State.HEALTHY else "false",
            "http_status": (
                str(observation.http.status_code)
                if observation.http.status_code is not None
                else "unknown"
            ),
            "authenticated": _tristate(authenticated),
            "database_healthy": _tristate(observation.database_healthy),
            "logger_healthy": _tristate(observation.logger_healthy),
            "last_check": _now_iso(),
            # Explicit "None" rather than "": an empty retained payload *deletes*
            # the retained message, so the topic would silently vanish instead of
            # reporting "not happened yet".
            "last_failure": self._last_failure or PAYLOAD_UNSET,
            "last_restart": last_restart or PAYLOAD_UNSET,
            "restart_count_24h": str(restart_count),
            "recovery_status": recovery_status,
        }
        for suffix, payload in values.items():
            self._mqtt.publish_state(suffix, payload)


def build_watchdog(config: Config, mqtt: WatchdogMqtt) -> Watchdog:
    checker = HttpChecker(
        config.teslamate_url,
        timeout=config.http_timeout_seconds,
        signin_pattern=config.teslamate_signin_pattern,
        body_markers=config.signin_body_markers,
    )
    machine = StateMachine(config)

    docker = DockerClient(config)
    if not docker.ping():
        log.error(
            "docker socket proxy unreachable — container, database and log checks "
            "are disabled and no restart can occur. Check DOCKER_HOST and that the "
            "proxy service is running.",
            extra={"context": {"docker_host": config.docker_host}},
        )

    classifier = LogClassifier.from_file(config.patterns_file)
    if classifier.is_empty:
        log.warning(
            "no usable log patterns loaded; log classification disabled",
            extra={"context": {"path": config.patterns_file}},
        )

    restarts = RestartManager(config)
    diagnostics = DiagnosticsWriter(config.diagnostic_dir, config.diagnostic_retention_days)
    diagnostics.prune()

    recovery = RecoveryController(
        config, docker, restarts, diagnostics, machine, mqtt.publish_event
    )

    return Watchdog(
        config,
        checker,
        mqtt,
        machine,
        docker=docker,
        classifier=classifier,
        restarts=restarts,
        recovery=recovery,
    )


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        # Logging is not configured yet, and this must never dump the environment.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging_setup.configure(config.log_level)
    log.info(
        "teslamate-watchdog starting",
        extra={"context": {"version": __version__, **config.summary()}},
    )

    for note in config.notes:
        log.warning(note)

    if config.staleness_detection_enabled:
        log.info(
            "MQTT staleness detection is enabled",
            extra={
                "context": {
                    "driving_or_charging_limit_seconds": config.mqtt_stale_seconds,
                    "parked_limit_seconds": config.mqtt_parked_stale_seconds,
                    "note": (
                        "the parked limit must exceed TeslaMate's suspend_min "
                        "setting, during which it publishes nothing"
                    ),
                }
            },
        )
    else:
        log.info(
            "MQTT staleness detection is disabled — a vehicle logger that dies "
            "silently while the web UI still responds will not be detected."
        )

    mqtt = WatchdogMqtt(config)
    # Re-publish discovery on every connect so a broker that lost its retained
    # messages gets them back.
    mqtt.add_on_connected(lambda: ha_discovery.publish(config, mqtt.publish_raw))
    mqtt.start()

    watchdog = build_watchdog(config, mqtt)

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown signal received", extra={"context": {"signal": signum}})
        watchdog.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        watchdog.run()
    finally:
        mqtt.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
