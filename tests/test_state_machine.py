"""State machine and the restart allowlist.

The restart tests here encode a hard requirement, not a preference: a logged-out
TeslaMate must never be restarted, because a restart brings it back up still
logged out while burning the restart budget and delaying the notification.
"""

from __future__ import annotations

import pytest

from src.http_check import HttpCategory, HttpResult
from src.state_machine import (
    RESTART_ALLOWED_STATES,
    RESTART_FORBIDDEN_STATES,
    LogSignals,
    Observation,
    State,
    StateMachine,
    evaluate,
    restart_allowed_for_state,
)


def http(category: HttpCategory, status_code: int | None = None) -> HttpResult:
    return HttpResult(category=category, status_code=status_code)


AUTHENTICATED = http(HttpCategory.AUTHENTICATED, 200)
SIGNIN = http(HttpCategory.LOGGED_OUT, 200)
UNREACHABLE = http(HttpCategory.UNREACHABLE)


def observe(**kwargs) -> Observation:
    kwargs.setdefault("http", AUTHENTICATED)
    return Observation(**kwargs)


def feed(machine: StateMachine, observation: Observation, times: int) -> None:
    for _ in range(times):
        machine.step(observation)


# --- restart policy (hard requirements) ------------------------------------


@pytest.mark.parametrize("state", sorted(RESTART_FORBIDDEN_STATES, key=lambda s: s.value))
def test_restart_is_forbidden_for_auth_and_database_states(state):
    assert restart_allowed_for_state(state) is False


@pytest.mark.parametrize("state", sorted(RESTART_ALLOWED_STATES, key=lambda s: s.value))
def test_restart_is_allowed_only_for_hung_states(state):
    assert restart_allowed_for_state(state) is True


def test_restart_allowlist_is_exactly_the_two_hung_states():
    assert RESTART_ALLOWED_STATES == {State.LOGGER_UNHEALTHY, State.TESLAMATE_UNREACHABLE}


def test_no_other_state_is_restartable():
    for state in State:
        if state in RESTART_ALLOWED_STATES:
            continue
        assert restart_allowed_for_state(state) is False, state


# --- decision priority ------------------------------------------------------


def test_database_unhealthy_outranks_everything():
    result = evaluate(observe(http=SIGNIN, database_healthy=False))
    assert result.state is State.DATABASE_UNHEALTHY


def test_stopped_container_is_immediate_and_unreachable():
    result = evaluate(observe(http=UNREACHABLE, teslamate_container_running=False))
    assert result.state is State.TESLAMATE_UNREACHABLE
    assert result.immediate is True


def test_signin_page_beats_a_successful_token_refresh_in_the_logs():
    result = evaluate(
        observe(http=SIGNIN, log_signals=LogSignals(refresh_success=True))
    )
    assert result.state is State.LOGGED_OUT


def test_http_200_does_not_override_an_unhealthy_logger():
    result = evaluate(observe(http=AUTHENTICATED, logger_healthy=False))
    assert result.state is State.LOGGER_UNHEALTHY


def test_auth_refresh_failure_in_logs_outranks_logger_health():
    result = evaluate(
        observe(logger_healthy=False, log_signals=LogSignals(auth_refresh_failing=True))
    )
    assert result.state is State.AUTH_REFRESH_FAILED


def test_token_decryption_failure_is_an_auth_state_so_it_cannot_restart():
    result = evaluate(observe(log_signals=LogSignals(token_decryption_failure=True)))
    assert result.state is State.AUTH_REFRESH_FAILED
    assert restart_allowed_for_state(result.state) is False


def test_stale_logger_is_logger_unhealthy():
    result = evaluate(observe(logger_stale=True, logger_detail="stale while driving"))
    assert result.state is State.LOGGER_UNHEALTHY


def test_server_error_is_treated_as_unreachable():
    result = evaluate(observe(http=http(HttpCategory.APPLICATION_ERROR, 502)))
    assert result.state is State.TESLAMATE_UNREACHABLE


def test_everything_passing_is_healthy():
    assert evaluate(observe(logger_healthy=True)).state is State.HEALTHY


def test_unknown_http_is_inconclusive_not_healthy():
    result = evaluate(observe(http=http(HttpCategory.UNKNOWN, 418)))
    assert result.inconclusive is True


# --- confirmation counting --------------------------------------------------


def test_single_transient_failure_does_not_change_state(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3, RECOVERY_CONFIRMATION_COUNT=2)
    machine = StateMachine(config, initial=State.HEALTHY)

    assert machine.step(observe(http=UNREACHABLE)) is None
    assert machine.state is State.HEALTHY


def test_failure_confirms_on_the_third_consecutive_check(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3)
    machine = StateMachine(config, initial=State.HEALTHY)
    failing = observe(http=UNREACHABLE)

    assert machine.step(failing) is None
    assert machine.step(failing) is None
    transition = machine.step(failing)

    assert transition is not None
    assert transition.current is State.TESLAMATE_UNREACHABLE
    assert machine.state is State.TESLAMATE_UNREACHABLE


def test_alternating_failures_never_confirm(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3)
    machine = StateMachine(config, initial=State.HEALTHY)

    for _ in range(6):
        machine.step(observe(http=UNREACHABLE))
        machine.step(observe(http=SIGNIN))

    assert machine.state is State.HEALTHY


def test_stopped_container_confirms_immediately(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3)
    machine = StateMachine(config, initial=State.HEALTHY)

    transition = machine.step(
        observe(http=UNREACHABLE, teslamate_container_running=False)
    )

    assert transition is not None
    assert machine.state is State.TESLAMATE_UNREACHABLE


def test_recovery_requires_two_consecutive_healthy_checks(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3, RECOVERY_CONFIRMATION_COUNT=2)
    machine = StateMachine(config, initial=State.HEALTHY)
    feed(machine, observe(http=UNREACHABLE), 3)
    assert machine.state is State.TESLAMATE_UNREACHABLE

    healthy = observe(logger_healthy=True)
    assert machine.step(healthy) is None
    assert machine.state is State.TESLAMATE_UNREACHABLE

    transition = machine.step(healthy)
    assert transition is not None
    assert machine.state is State.HEALTHY


def test_one_healthy_check_mid_failure_resets_the_failure_streak(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3)
    machine = StateMachine(config, initial=State.HEALTHY)

    machine.step(observe(http=UNREACHABLE))
    machine.step(observe(http=UNREACHABLE))
    machine.step(observe(logger_healthy=True))
    machine.step(observe(http=UNREACHABLE))

    assert machine.state is State.HEALTHY


def test_starting_needs_confirmation_before_reporting_healthy(make_config):
    config = make_config(RECOVERY_CONFIRMATION_COUNT=2)
    machine = StateMachine(config)
    assert machine.state is State.STARTING

    machine.step(observe(logger_healthy=True))
    assert machine.state is State.STARTING

    machine.step(observe(logger_healthy=True))
    assert machine.state is State.HEALTHY


def test_inconclusive_checks_do_not_count_toward_recovery(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3, RECOVERY_CONFIRMATION_COUNT=2)
    machine = StateMachine(config, initial=State.HEALTHY)
    feed(machine, observe(http=UNREACHABLE), 3)

    unknown = observe(http=http(HttpCategory.UNKNOWN, 418))
    machine.step(unknown)
    machine.step(unknown)

    assert machine.state is State.TESLAMATE_UNREACHABLE


def test_logged_out_confirms_and_stays_non_restartable(make_config):
    config = make_config(FAILURE_CONFIRMATION_COUNT=3)
    machine = StateMachine(config, initial=State.HEALTHY)

    feed(machine, observe(http=SIGNIN), 3)

    assert machine.state is State.LOGGED_OUT
    assert restart_allowed_for_state(machine.state) is False


def test_recovering_state_ignores_ordinary_checks(make_config):
    config = make_config(RECOVERY_CONFIRMATION_COUNT=1)
    machine = StateMachine(config, initial=State.HEALTHY)
    machine.force_state(State.RECOVERING, "restart issued")

    assert machine.step(observe(logger_healthy=True)) is None
    assert machine.state is State.RECOVERING
