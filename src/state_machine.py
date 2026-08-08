"""The failure-state machine, and the allowlist that governs restarts.

Two rules in here are load-bearing and must not be "simplified" away:

1. **A logged-out TeslaMate is never restarted.** Its stored refresh token is
   dead; restarting brings the container back up still logged out. It burns the
   restart budget, delays the notification by POST_RESTART_WAIT_SECONDS, and
   fixes nothing. Logout goes straight to MQTT so a human re-enters tokens.
2. **A single bad check never changes state.** Failures need
   FAILURE_CONFIRMATION_COUNT consecutive agreeing checks (except a stopped
   container, which is unambiguous), and recovery needs
   RECOVERY_CONFIRMATION_COUNT consecutive healthy ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .config import Config
from .http_check import HttpCategory, HttpResult

log = logging.getLogger(__name__)


class State(str, Enum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    LOGGED_OUT = "LOGGED_OUT"
    LOGGER_UNHEALTHY = "LOGGER_UNHEALTHY"
    TESLAMATE_UNREACHABLE = "TESLAMATE_UNREACHABLE"
    DATABASE_UNHEALTHY = "DATABASE_UNHEALTHY"
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
    RECOVERING = "RECOVERING"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


#: The ONLY states a restart may ever be attempted from. This is an allowlist,
#: deliberately: a state added later is non-restartable until someone puts it
#: here on purpose.
RESTART_ALLOWED_STATES: Final[frozenset[State]] = frozenset(
    {State.LOGGER_UNHEALTHY, State.TESLAMATE_UNREACHABLE}
)

#: Spelled out so the intent survives refactoring, and cross-checked against the
#: allowlist below.
RESTART_FORBIDDEN_STATES: Final[frozenset[State]] = frozenset(
    {State.LOGGED_OUT, State.AUTH_REFRESH_FAILED, State.DATABASE_UNHEALTHY}
)

assert not (RESTART_ALLOWED_STATES & RESTART_FORBIDDEN_STATES), (
    "a state cannot be both restartable and restart-forbidden"
)

#: States that mean "something is wrong" — used to decide when to emit
#: failure_detected and to capture diagnostics.
FAILURE_STATES: Final[frozenset[State]] = frozenset(
    {
        State.LOGGED_OUT,
        State.LOGGER_UNHEALTHY,
        State.TESLAMATE_UNREACHABLE,
        State.DATABASE_UNHEALTHY,
        State.AUTH_REFRESH_FAILED,
        State.MANUAL_INTERVENTION_REQUIRED,
    }
)

#: States where the fix is "a human pastes fresh tokens into the TeslaMate UI".
AUTH_STATES: Final[frozenset[State]] = frozenset(
    {State.LOGGED_OUT, State.AUTH_REFRESH_FAILED}
)


def restart_allowed_for_state(state: State) -> bool:
    """State-level half of the restart guard.

    The other half (database healthy, cooldown, daily cap) lives with the
    restart executor. Both must pass.
    """
    return state in RESTART_ALLOWED_STATES


@dataclass(frozen=True)
class LogSignals:
    """What the TeslaMate container logs indicate. Populated in stage 2.

    ``refresh_success`` is informational only and deliberately never feeds the
    state decision — a "Refreshed api tokens" line must not be able to mask a
    sign-in page that is being served right now.
    """

    auth_refresh_failing: bool = False
    auth_lost: bool = False
    database_failure: bool = False
    token_decryption_failure: bool = False
    refresh_success: bool = False
    matches: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    """Everything one check cycle learned. ``None`` means "not known"."""

    http: HttpResult
    logger_healthy: bool | None = None
    logger_stale: bool = False
    logger_detail: str = ""
    database_healthy: bool | None = None
    teslamate_container_running: bool | None = None
    log_signals: LogSignals | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "http": self.http.as_dict(),
            "logger_healthy": self.logger_healthy,
            "logger_stale": self.logger_stale,
            "logger_detail": self.logger_detail,
            "database_healthy": self.database_healthy,
            "teslamate_container_running": self.teslamate_container_running,
            "log_signals": (
                {
                    "auth_refresh_failing": self.log_signals.auth_refresh_failing,
                    "auth_lost": self.log_signals.auth_lost,
                    "database_failure": self.log_signals.database_failure,
                    "token_decryption_failure": self.log_signals.token_decryption_failure,
                    "matches": self.log_signals.matches,
                }
                if self.log_signals
                else None
            ),
        }


@dataclass(frozen=True)
class Evaluation:
    state: State
    reason: str
    #: Skip confirmation counting — the evidence is unambiguous.
    immediate: bool = False
    #: The check told us nothing usable; hold position and count nothing.
    inconclusive: bool = False


@dataclass(frozen=True)
class Transition:
    previous: State
    current: State
    reason: str


def evaluate(observation: Observation) -> Evaluation:
    """Map one observation to a candidate state. First match wins."""
    signals = observation.log_signals

    if observation.database_healthy is False:
        return Evaluation(State.DATABASE_UNHEALTHY, "database reported unhealthy")

    if observation.teslamate_container_running is False:
        # A stopped container is not a judgement call.
        return Evaluation(
            State.TESLAMATE_UNREACHABLE,
            "teslamate container is not running",
            immediate=True,
        )

    if observation.http.category is HttpCategory.LOGGED_OUT:
        # Checked before any log signal: a recent "Refreshed api tokens" line
        # cannot override a sign-in page being served right now.
        return Evaluation(
            State.LOGGED_OUT,
            f"sign-in page detected ({observation.http.detail})",
        )

    if observation.http.category is HttpCategory.UNREACHABLE:
        return Evaluation(
            State.TESLAMATE_UNREACHABLE,
            f"http unreachable ({observation.http.error})",
        )

    if observation.http.category is HttpCategory.APPLICATION_ERROR:
        # Not in the original decision table, which has no state for 5xx. Folded
        # into TESLAMATE_UNREACHABLE because "app is up but erroring" is the
        # hung-process case a restart is meant for. If the 5xx is caused by the
        # database, the database check above outranks this, and the restart
        # guard independently requires a healthy database.
        return Evaluation(
            State.TESLAMATE_UNREACHABLE,
            f"http {observation.http.status_code} server error",
        )

    if signals is not None and signals.token_decryption_failure:
        # ENCRYPTION_KEY changed: the stored tokens can no longer be read. Needs
        # a human, never a restart — hence an auth state, not a hung state.
        return Evaluation(
            State.AUTH_REFRESH_FAILED, "logs show API tokens could not be decrypted"
        )

    if signals is not None and (signals.auth_refresh_failing or signals.auth_lost):
        return Evaluation(
            State.AUTH_REFRESH_FAILED, "logs show repeated token-refresh failure"
        )

    # Reached only when HTTP was 200-and-authenticated (or inconclusive): a
    # healthy web UI must not override a confirmed-unhealthy vehicle logger.
    if observation.logger_healthy is False:
        return Evaluation(
            State.LOGGER_UNHEALTHY,
            f"vehicle logger reports unhealthy ({observation.logger_detail})",
        )

    if observation.logger_stale:
        return Evaluation(
            State.LOGGER_UNHEALTHY, f"vehicle logger stale ({observation.logger_detail})"
        )

    if observation.http.category is not HttpCategory.AUTHENTICATED:
        # UNKNOWN: an unexpected status or a redirect somewhere we don't
        # recognise. Not evidence of health, not evidence of failure — refuse to
        # call it either way rather than counting it toward a recovery.
        return Evaluation(
            State.HEALTHY,
            f"inconclusive http result ({observation.http.detail})",
            inconclusive=True,
        )

    return Evaluation(State.HEALTHY, "all checks passing")


class StateMachine:
    def __init__(self, config: Config, *, initial: State = State.STARTING) -> None:
        self._config = config
        self.state = initial
        self.reason = "watchdog starting"
        self._candidate: State | None = None
        self._candidate_streak = 0
        self._healthy_streak = 0
        self.last_evaluation: Evaluation | None = None

    @property
    def candidate(self) -> State | None:
        return self._candidate

    @property
    def candidate_streak(self) -> int:
        return self._candidate_streak

    @property
    def healthy_streak(self) -> int:
        return self._healthy_streak

    def step(self, observation: Observation) -> Transition | None:
        """Feed one check cycle in. Returns a Transition only when state changed."""
        if self.state is State.RECOVERING:
            # The recovery controller owns the exit from RECOVERING; ordinary
            # checks must not race it back to HEALTHY mid-restart.
            return None

        evaluation = evaluate(observation)
        self.last_evaluation = evaluation

        if evaluation.inconclusive:
            log.debug(
                "inconclusive check; holding state",
                extra={"context": {"state": self.state.value, "reason": evaluation.reason}},
            )
            return None

        if evaluation.state is State.HEALTHY:
            return self._observe_healthy(evaluation)
        return self._observe_failure(evaluation)

    def force_state(self, state: State, reason: str) -> Transition:
        """Set state directly (used by the recovery controller for RECOVERING)."""
        previous = self.state
        self.state = state
        self.reason = reason
        self._candidate = None
        self._candidate_streak = 0
        self._healthy_streak = 0
        return Transition(previous=previous, current=state, reason=reason)

    # --- internals --------------------------------------------------------

    def _observe_healthy(self, evaluation: Evaluation) -> Transition | None:
        self._healthy_streak += 1
        self._candidate = None
        self._candidate_streak = 0

        if self.state is State.HEALTHY:
            return None
        if self._healthy_streak < self._config.recovery_confirmation_count:
            return None

        previous = self.state
        self.state = State.HEALTHY
        self.reason = evaluation.reason
        return Transition(previous=previous, current=State.HEALTHY, reason=evaluation.reason)

    def _observe_failure(self, evaluation: Evaluation) -> Transition | None:
        self._healthy_streak = 0

        if evaluation.state is self._candidate:
            self._candidate_streak += 1
        else:
            self._candidate = evaluation.state
            self._candidate_streak = 1

        confirmed = (
            evaluation.immediate
            or self._candidate_streak >= self._config.failure_confirmation_count
        )
        if not confirmed or self.state is evaluation.state:
            return None

        previous = self.state
        self.state = evaluation.state
        self.reason = evaluation.reason
        return Transition(
            previous=previous, current=evaluation.state, reason=evaluation.reason
        )
