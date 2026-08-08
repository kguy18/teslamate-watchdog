"""The restart sequence end to end, with Docker faked.

Covers the ordering the spec requires (diagnostics -> recovery_started ->
restart -> wait -> confirm) and the two ways it can end.
"""

from __future__ import annotations

import pytest

from src.diagnostics import DiagnosticsWriter
from src.http_check import HttpCategory, HttpResult
from src.recovery import (
    STATUS_CONFIRMING,
    STATUS_MANUAL,
    STATUS_RECOVERED,
    STATUS_WAITING,
    RecoveryController,
)
from src.restart_manager import RestartManager
from src.state_machine import Observation, State, StateMachine

AUTHENTICATED = HttpResult(category=HttpCategory.AUTHENTICATED, status_code=200)
UNREACHABLE = HttpResult(category=HttpCategory.UNREACHABLE, error="ConnectionError")

HEALTHY = Observation(http=AUTHENTICATED, logger_healthy=True, database_healthy=True)
FAILING = Observation(http=UNREACHABLE, database_healthy=True)


class FakeDocker:
    def __init__(self, restart_ok=True, detail="restarted"):
        self.restart_ok = restart_ok
        self.detail = detail
        self.restarts: list[str] = []

    def restart(self, container):
        self.restarts.append(container)
        return self.restart_ok, self.detail

    def logs(self, container, since_seconds):
        return f"log lines for {container}"


@pytest.fixture
def build(make_config, tmp_path):
    def _build(restart_ok=True, **overrides):
        settings = {
            "DATA_DIR": str(tmp_path),
            "DIAGNOSTIC_DIR": str(tmp_path / "diagnostics"),
            "POST_RESTART_WAIT_SECONDS": 0,
            "RECOVERY_CONFIRMATION_COUNT": 2,
            "FAILURE_CONFIRMATION_COUNT": 3,
        }
        settings.update(overrides)
        config = make_config(**settings)
        docker = FakeDocker(restart_ok=restart_ok)
        restarts = RestartManager(config, history_path=tmp_path / "history.json")
        diagnostics = DiagnosticsWriter(config.diagnostic_dir, config.diagnostic_retention_days)
        machine = StateMachine(config, initial=State.TESLAMATE_UNREACHABLE)
        events: list[tuple[str, dict]] = []
        controller = RecoveryController(
            config,
            docker,
            restarts,
            diagnostics,
            machine,
            lambda event, payload: events.append((event, payload)),
        )
        return controller, docker, machine, events, restarts

    return _build


# --- the happy path ---------------------------------------------------------


def test_begin_restarts_the_teslamate_container(build):
    controller, docker, machine, events, _ = build()

    assert controller.begin(State.TESLAMATE_UNREACHABLE, "http dead") is True
    assert docker.restarts == ["teslamate"]
    assert machine.state is State.RECOVERING


def test_event_order_is_recovery_started_then_restart_executed(build):
    controller, _, _, events, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    names = [event for event, _ in events]
    assert names == ["recovery_started", "restart_executed"]


def test_restart_is_recorded_against_the_budget(build):
    controller, _, _, _, restarts = build()
    assert restarts.restarts_in_24h() == 0

    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")
    assert restarts.restarts_in_24h() == 1


def test_two_healthy_checks_confirm_recovery(build):
    controller, _, machine, events, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    controller.step(HEALTHY)
    assert machine.state is State.RECOVERING
    assert controller.active is True

    controller.step(HEALTHY)
    assert machine.state is State.HEALTHY
    assert controller.active is False
    assert controller.status == STATUS_RECOVERED
    assert "recovered" in [event for event, _ in events]


def test_recovery_waits_before_confirming(build):
    controller, _, machine, _, _ = build(POST_RESTART_WAIT_SECONDS=600)
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    for _ in range(5):
        controller.step(HEALTHY)

    assert controller.status == STATUS_WAITING
    assert machine.state is State.RECOVERING


def test_confirming_status_is_reported_after_the_wait(build):
    controller, _, _, _, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")
    controller.step(HEALTHY)
    assert controller.status == STATUS_CONFIRMING


# --- failure paths ----------------------------------------------------------


def test_still_failing_after_a_restart_needs_a_human(build):
    controller, _, machine, events, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    for _ in range(3):
        controller.step(FAILING)

    assert machine.state is State.MANUAL_INTERVENTION_REQUIRED
    assert controller.status == STATUS_MANUAL
    assert "manual_intervention_required" in [event for event, _ in events]


def test_a_single_post_restart_blip_does_not_doom_recovery(build):
    controller, _, machine, _, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    controller.step(FAILING)
    controller.step(HEALTHY)
    controller.step(HEALTHY)

    assert machine.state is State.HEALTHY


def test_a_failed_restart_goes_straight_to_manual(build):
    controller, _, machine, events, restarts = build(restart_ok=False)

    assert controller.begin(State.TESLAMATE_UNREACHABLE, "http dead") is False
    assert machine.state is State.MANUAL_INTERVENTION_REQUIRED
    assert "manual_intervention_required" in [event for event, _ in events]
    # A restart that never happened must not consume the budget.
    assert restarts.restarts_in_24h() == 0


def test_inconclusive_checks_neither_confirm_nor_condemn(build):
    controller, _, machine, _, _ = build()
    controller.begin(State.TESLAMATE_UNREACHABLE, "http dead")

    unknown = Observation(
        http=HttpResult(category=HttpCategory.UNKNOWN, status_code=418),
        database_healthy=True,
    )
    for _ in range(5):
        controller.step(unknown)

    assert machine.state is State.RECOVERING


def test_step_is_a_noop_when_recovery_is_not_active(build):
    controller, _, machine, events, _ = build()
    controller.step(HEALTHY)
    assert machine.state is State.TESLAMATE_UNREACHABLE
    assert events == []


# --- diagnostics ------------------------------------------------------------


def test_capture_writes_a_bundle_and_announces_it(build, tmp_path):
    controller, _, _, events, _ = build()
    controller.capture(
        State.LOGGED_OUT, "sign-in page", HEALTHY, {"teslamate": {"running": True}}
    )

    assert "diagnostics_captured" in [event for event, _ in events]
    bundles = list((tmp_path / "diagnostics").iterdir())
    assert len(bundles) == 1
    assert (bundles[0] / "summary.json").exists()
    assert (bundles[0] / "teslamate.log").exists()
    assert (bundles[0] / "database.log").exists()
    assert (bundles[0] / "docker-state.json").exists()


def test_capture_records_that_no_restart_was_attempted_for_auth_states(build, tmp_path):
    import json

    controller, _, _, _, _ = build()
    controller.capture(State.LOGGED_OUT, "sign-in page", HEALTHY, {})

    bundle = next((tmp_path / "diagnostics").iterdir())
    summary = json.loads((bundle / "summary.json").read_text())
    assert summary["restart_attempted"] is False
    assert summary["state"] == "LOGGED_OUT"
