"""Restart guards and persisted history.

Every test here encodes a way the watchdog could make things worse: restarting a
logged-out instance, restarting into a dead database, hammering TeslaMate in a
loop, or forgetting it just restarted because its container was recreated.
"""

from __future__ import annotations

import json

import pytest

from src.restart_manager import (
    DATABASE_CONFIRMATION_COUNT,
    SECONDS_PER_DAY,
    RestartManager,
)
from src.state_machine import State

NOW = 1_800_000_000.0
HEALTHY_DB = DATABASE_CONFIRMATION_COUNT


@pytest.fixture
def manager(make_config, tmp_path):
    def _build(**overrides):
        config = make_config(DATA_DIR=str(tmp_path), **overrides)
        return RestartManager(config, history_path=tmp_path / "restart_history.json")

    return _build


def allow(manager, state=State.TESLAMATE_UNREACHABLE, streak=HEALTHY_DB, now=NOW):
    return manager.evaluate(state, database_healthy_streak=streak, now=now)


# --- the state allowlist ----------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [State.LOGGED_OUT, State.AUTH_REFRESH_FAILED, State.DATABASE_UNHEALTHY],
)
def test_non_restartable_states_are_refused(manager, state):
    decision = allow(manager(), state=state)
    assert decision.allowed is False
    assert "not a restartable state" in decision.reason


@pytest.mark.parametrize(
    "state", [State.LOGGER_UNHEALTHY, State.TESLAMATE_UNREACHABLE]
)
def test_restartable_states_are_permitted_when_guards_pass(manager, state):
    assert allow(manager(), state=state).allowed is True


def test_logged_out_is_refused_even_with_everything_else_perfect(manager):
    """The headline requirement: a restart cannot revive a dead refresh token."""
    instance = manager()
    decision = instance.evaluate(
        State.LOGGED_OUT, database_healthy_streak=99, now=NOW
    )
    assert decision.allowed is False


# --- the other guards -------------------------------------------------------


def test_disabled_auto_restart_blocks_everything(manager):
    decision = allow(manager(AUTO_RESTART_ENABLED="false"))
    assert decision.allowed is False
    assert "AUTO_RESTART_ENABLED" in decision.reason


def test_unconfirmed_database_blocks_a_restart(manager):
    decision = allow(manager(), streak=DATABASE_CONFIRMATION_COUNT - 1)
    assert decision.allowed is False
    assert "database not confirmed healthy" in decision.reason


def test_database_needs_two_consecutive_healthy_checks(manager):
    instance = manager()
    assert allow(instance, streak=0).allowed is False
    assert allow(instance, streak=1).allowed is False
    assert allow(instance, streak=2).allowed is True


def test_cooldown_blocks_a_second_restart(manager):
    instance = manager(RESTART_COOLDOWN_SECONDS=21600)
    instance.record(State.TESLAMATE_UNREACHABLE, "first", now=NOW)

    decision = allow(instance, now=NOW + 3600)
    assert decision.allowed is False
    assert "cooldown active" in decision.reason


def test_restart_is_permitted_once_the_cooldown_expires(manager):
    instance = manager(RESTART_COOLDOWN_SECONDS=21600, MAX_RESTARTS_PER_24_HOURS=5)
    instance.record(State.TESLAMATE_UNREACHABLE, "first", now=NOW)

    assert allow(instance, now=NOW + 21601).allowed is True


def test_daily_cap_blocks_further_restarts(manager):
    instance = manager(MAX_RESTARTS_PER_24_HOURS=2, RESTART_COOLDOWN_SECONDS=0)
    instance.record(State.TESLAMATE_UNREACHABLE, "first", now=NOW)
    instance.record(State.TESLAMATE_UNREACHABLE, "second", now=NOW + 100)

    decision = allow(instance, now=NOW + 200)
    assert decision.allowed is False
    assert "daily cap reached" in decision.reason


def test_the_cap_is_a_rolling_24_hours(manager):
    instance = manager(MAX_RESTARTS_PER_24_HOURS=2, RESTART_COOLDOWN_SECONDS=0)
    instance.record(State.TESLAMATE_UNREACHABLE, "old", now=NOW)
    instance.record(State.TESLAMATE_UNREACHABLE, "recent", now=NOW + 100)

    later = NOW + SECONDS_PER_DAY + 1
    assert instance.restarts_in_24h(later) == 1
    assert allow(instance, now=later).allowed is True


def test_guards_are_evaluated_before_the_cooldown_is_consumed(manager):
    """A refused restart must not count against the budget."""
    instance = manager()
    allow(instance, state=State.LOGGED_OUT)
    assert instance.restarts_in_24h(NOW) == 0
    assert instance.last_restart is None


# --- persistence ------------------------------------------------------------


def test_history_survives_container_recreation(make_config, tmp_path):
    """A recreated container must not forget it just restarted TeslaMate."""
    config = make_config(DATA_DIR=str(tmp_path), RESTART_COOLDOWN_SECONDS=21600)
    path = tmp_path / "restart_history.json"

    first = RestartManager(config, history_path=path)
    first.record(State.TESLAMATE_UNREACHABLE, "before recreation", now=NOW)

    # Simulate `docker compose up --force-recreate`: brand new process, same volume.
    second = RestartManager(config, history_path=path)

    assert second.restarts_in_24h(NOW + 60) == 1
    decision = second.evaluate(
        State.TESLAMATE_UNREACHABLE, database_healthy_streak=HEALTHY_DB, now=NOW + 60
    )
    assert decision.allowed is False
    assert "cooldown active" in decision.reason


def test_history_is_written_atomically_and_reloads(make_config, tmp_path):
    config = make_config(DATA_DIR=str(tmp_path))
    path = tmp_path / "restart_history.json"
    instance = RestartManager(config, history_path=path)
    instance.record(State.LOGGER_UNHEALTHY, "hung logger", now=NOW)

    body = json.loads(path.read_text())
    assert len(body["restarts"]) == 1
    assert body["restarts"][0]["state"] == "LOGGER_UNHEALTHY"
    assert "iso" in body["restarts"][0]
    # No stray temp files left behind by the write-then-rename.
    assert [p.name for p in tmp_path.iterdir()] == ["restart_history.json"]


def test_missing_history_file_is_not_an_error(make_config, tmp_path):
    config = make_config(DATA_DIR=str(tmp_path))
    instance = RestartManager(config, history_path=tmp_path / "absent.json")
    assert instance.restarts_in_24h(NOW) == 0


def test_corrupt_history_degrades_to_empty(make_config, tmp_path):
    path = tmp_path / "restart_history.json"
    path.write_text("{not valid json")
    config = make_config(DATA_DIR=str(tmp_path))

    instance = RestartManager(config, history_path=path)
    assert instance.restarts_in_24h(NOW) == 0


def test_malformed_entries_are_skipped_not_fatal(make_config, tmp_path):
    path = tmp_path / "restart_history.json"
    path.write_text(json.dumps({"restarts": [{"at": NOW}, {"nope": True}, {"at": "x"}]}))
    config = make_config(DATA_DIR=str(tmp_path))

    instance = RestartManager(config, history_path=path)
    assert instance.restarts_in_24h(NOW + 10) == 1


def test_old_history_is_pruned_on_write(make_config, tmp_path):
    config = make_config(DATA_DIR=str(tmp_path))
    path = tmp_path / "restart_history.json"
    instance = RestartManager(config, history_path=path)

    instance.record(State.TESLAMATE_UNREACHABLE, "ancient", now=NOW - 30 * SECONDS_PER_DAY)
    instance.record(State.TESLAMATE_UNREACHABLE, "now", now=NOW)

    body = json.loads(path.read_text())
    assert len(body["restarts"]) == 1
