"""Diagnostics bundles: contents, redaction and pruning."""

from __future__ import annotations

import json
import os
import time

from src.diagnostics import DiagnosticsWriter, redact


def writer(tmp_path, retention_days=14) -> DiagnosticsWriter:
    return DiagnosticsWriter(tmp_path / "diagnostics", retention_days)


def test_bundle_contains_the_four_expected_files(tmp_path):
    incident = writer(tmp_path).capture(
        state="TESLAMATE_UNREACHABLE",
        reason="http dead",
        summary={"observation": {"http": {"category": "unreachable"}}},
        teslamate_log="line one\nline two",
        database_log="db line",
        docker_state={"teslamate": {"running": False}},
    )

    assert incident is not None
    names = sorted(p.name for p in incident.directory.iterdir())
    assert names == ["database.log", "docker-state.json", "summary.json", "teslamate.log"]


def test_summary_records_state_reason_and_timestamp(tmp_path):
    incident = writer(tmp_path).capture(
        state="LOGGED_OUT", reason="sign-in page detected", summary={"extra": 1}
    )

    body = json.loads(incident.summary_path.read_text())
    assert body["state"] == "LOGGED_OUT"
    assert body["trigger_reason"] == "sign-in page detected"
    assert body["extra"] == 1
    assert body["timestamp"]


def test_directory_name_encodes_the_state(tmp_path):
    incident = writer(tmp_path).capture(state="DATABASE_UNHEALTHY", reason="x", summary={})
    assert incident.directory.name.endswith("_database_unhealthy")


def test_separate_incidents_do_not_collide(tmp_path):
    instance = writer(tmp_path)
    first = instance.capture(state="LOGGED_OUT", reason="a", summary={})
    second = instance.capture(state="LOGGER_UNHEALTHY", reason="b", summary={})
    assert first.directory != second.directory


# --- redaction --------------------------------------------------------------


def test_jwt_shaped_values_are_redacted():
    text = "token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSM"
    assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in redact(text)
    assert "<jwt-redacted>" in redact(text)


def test_tesla_refresh_tokens_are_redacted():
    text = "refresh: qts-1234567890abcdefghijklmnop"
    assert "qts-1234567890abcdefghijklmnop" not in redact(text)


def test_labelled_token_values_are_redacted():
    text = "access_token=abcdefghijklmnopqrstuvwxyz123456"
    redacted = redact(text)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in redacted


def test_ordinary_log_text_survives_redaction():
    text = "13:00:00.000 [info] Vehicle 1 is now online"
    assert redact(text) == text


def test_logs_are_redacted_on_the_way_to_disk(tmp_path):
    incident = writer(tmp_path).capture(
        state="AUTH_REFRESH_FAILED",
        reason="refresh failing",
        summary={},
        teslamate_log="Token refresh failed eyJhbGciOiJIUzI1NiJ9.PAYLOADPART.SIGPART",
    )

    written = (incident.directory / "teslamate.log").read_text()
    assert "eyJhbGciOiJIUzI1NiJ9" not in written
    assert "<jwt-redacted>" in written


# --- pruning ----------------------------------------------------------------


def test_old_bundles_are_pruned(tmp_path):
    instance = writer(tmp_path, retention_days=14)
    old = instance.capture(state="LOGGED_OUT", reason="old", summary={})
    fresh = instance.capture(state="LOGGED_OUT", reason="fresh", summary={})

    ancient = time.time() - 20 * 86400
    os.utime(old.directory, (ancient, ancient))

    assert instance.prune() == 1
    assert not old.directory.exists()
    assert fresh.directory.exists()


def test_retention_of_zero_disables_pruning(tmp_path):
    instance = writer(tmp_path, retention_days=0)
    incident = instance.capture(state="LOGGED_OUT", reason="x", summary={})
    ancient = time.time() - 400 * 86400
    os.utime(incident.directory, (ancient, ancient))

    assert instance.prune() == 0
    assert incident.directory.exists()


def test_pruning_a_missing_directory_is_harmless(tmp_path):
    assert DiagnosticsWriter(tmp_path / "never-created", 14).prune() == 0


def test_capture_returns_none_when_the_path_is_unwritable(tmp_path):
    blocker = tmp_path / "diagnostics"
    blocker.write_text("I am a file, not a directory")

    assert DiagnosticsWriter(blocker, 14).capture(
        state="LOGGED_OUT", reason="x", summary={}
    ) is None
