"""The restart sequence, and confirming whether it worked.

    capture diagnostics -> recovery_started -> restart -> wait -> confirm
        -> recovered, or manual_intervention_required

Recovery is driven by the ordinary check loop rather than by sleeping: `begin`
issues the restart and returns, and `step` is called once per cycle afterwards.
That keeps MQTT publishing and shutdown responsive while a restart settles.

While recovery is active the state machine is held at RECOVERING and its normal
transitions are suppressed, so an early post-restart blip cannot race the
recovery to a conclusion.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .diagnostics import DiagnosticsWriter
from .docker_client import DockerClient
from .restart_manager import RestartManager
from .state_machine import Observation, State, StateMachine, evaluate

log = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_WAITING = "waiting_after_restart"
STATUS_CONFIRMING = "confirming_recovery"
STATUS_RECOVERED = "recovered"
STATUS_MANUAL = "manual_intervention_required"


@dataclass
class _Progress:
    trigger_state: State
    reason: str
    deadline: float
    healthy_streak: int = 0
    failure_streak: int = 0


class RecoveryController:
    def __init__(
        self,
        config: Config,
        docker: DockerClient,
        restarts: RestartManager,
        diagnostics: DiagnosticsWriter,
        machine: StateMachine,
        publish_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._docker = docker
        self._restarts = restarts
        self._diagnostics = diagnostics
        self._machine = machine
        self._publish_event = publish_event
        self._progress: _Progress | None = None
        self.status = STATUS_IDLE

    @property
    def active(self) -> bool:
        return self._progress is not None

    # --- capture ----------------------------------------------------------

    def capture(
        self,
        state: State,
        reason: str,
        observation: Observation,
        docker_state: dict[str, Any],
    ) -> None:
        """Write an incident bundle and announce it. Never raises."""
        incident = self._diagnostics.capture(
            state=state.value,
            reason=reason,
            summary={
                "observation": observation.as_dict(),
                "restart_count_24h": self._restarts.restarts_in_24h(),
                "last_restart": self._restarts.last_restart_iso(),
                "restart_attempted": state in (State.LOGGER_UNHEALTHY, State.TESLAMATE_UNREACHABLE),
            },
            teslamate_log=self._safe_logs(self._config.teslamate_container),
            database_log=self._safe_logs(self._config.database_container),
            docker_state=docker_state,
        )
        if incident is not None:
            self._publish_event(
                "diagnostics_captured",
                {"state": state.value, "reason": reason, **incident.as_dict()},
            )

    def _safe_logs(self, container: str) -> str:
        try:
            return self._docker.logs(
                container, since_seconds=self._config.diagnostic_log_lookback_seconds
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break recovery
            log.exception("could not collect logs for diagnostics")
            return ""

    # --- the sequence -----------------------------------------------------

    def begin(self, state: State, reason: str) -> bool:
        """Issue the restart. Returns True if recovery is now in progress."""
        self._publish_event(
            "recovery_started",
            {
                "state": state.value,
                "reason": reason,
                "container": self._config.teslamate_container,
                "wait_seconds": self._config.post_restart_wait_seconds,
            },
        )

        ok, detail = self._docker.restart(self._config.teslamate_container)
        self._publish_event(
            "restart_executed",
            {
                "state": state.value,
                "success": ok,
                "detail": detail,
                "container": self._config.teslamate_container,
            },
        )

        if not ok:
            log.error("restart failed", extra={"context": {"detail": detail}})
            self.status = STATUS_MANUAL
            self._machine.force_state(
                State.MANUAL_INTERVENTION_REQUIRED, f"restart failed: {detail}"
            )
            self._publish_event(
                "manual_intervention_required",
                {
                    "state": State.MANUAL_INTERVENTION_REQUIRED.value,
                    "reason": f"restart failed: {detail}",
                    "action_required": (
                        "The watchdog could not restart TeslaMate. Check the socket "
                        "proxy grants and the container name."
                    ),
                },
            )
            return False

        self._restarts.record(state, reason)
        self._progress = _Progress(
            trigger_state=state,
            reason=reason,
            deadline=time.monotonic() + self._config.post_restart_wait_seconds,
        )
        self.status = STATUS_WAITING
        self._machine.force_state(State.RECOVERING, f"restart issued: {reason}")
        log.warning(
            "recovery in progress",
            extra={"context": {"detail": detail, "wait_seconds": self._config.post_restart_wait_seconds}},
        )
        return True

    def step(self, observation: Observation) -> None:
        """Advance recovery by one check cycle."""
        progress = self._progress
        if progress is None:
            return

        if time.monotonic() < progress.deadline:
            self.status = STATUS_WAITING
            return

        self.status = STATUS_CONFIRMING
        result = evaluate(observation)

        if result.inconclusive:
            return

        if result.state is State.HEALTHY:
            progress.healthy_streak += 1
            progress.failure_streak = 0
            if progress.healthy_streak >= self._config.recovery_confirmation_count:
                self._finish_recovered(progress)
            return

        progress.failure_streak += 1
        progress.healthy_streak = 0
        if progress.failure_streak >= self._config.failure_confirmation_count:
            self._finish_manual(progress, result.reason)

    # --- outcomes ---------------------------------------------------------

    def _finish_recovered(self, progress: _Progress) -> None:
        self._progress = None
        self.status = STATUS_RECOVERED
        self._machine.force_state(State.HEALTHY, "recovered after restart")
        self._publish_event(
            "recovered",
            {
                "state": State.HEALTHY.value,
                "recovered_from": progress.trigger_state.value,
                "reason": progress.reason,
                "restart_count_24h": self._restarts.restarts_in_24h(),
            },
        )
        log.warning(
            "recovered after restart",
            extra={"context": {"from": progress.trigger_state.value}},
        )

    def _finish_manual(self, progress: _Progress, detail: str) -> None:
        self._progress = None
        self.status = STATUS_MANUAL
        self._machine.force_state(
            State.MANUAL_INTERVENTION_REQUIRED, f"still failing after restart: {detail}"
        )
        self._publish_event(
            "manual_intervention_required",
            {
                "state": State.MANUAL_INTERVENTION_REQUIRED.value,
                "recovered_from": progress.trigger_state.value,
                "reason": f"still failing after restart: {detail}",
                "action_required": (
                    "TeslaMate was restarted but is still unhealthy. Check the "
                    "container logs and the diagnostics bundle."
                ),
                "restart_count_24h": self._restarts.restarts_in_24h(),
            },
        )
        log.error(
            "still failing after restart",
            extra={"context": {"from": progress.trigger_state.value, "detail": detail}},
        )
