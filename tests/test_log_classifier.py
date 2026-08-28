"""Log classification against the shipped patterns.yaml.

Uses the real file rather than a fixture, so a typo in a shipped regex fails
here rather than in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.log_classifier import MAX_EXCERPT_CHARS, LogClassifier, truncate

PATTERNS_FILE = Path(__file__).resolve().parent.parent / "patterns.yaml"


@pytest.fixture
def classifier() -> LogClassifier:
    return LogClassifier.from_file(PATTERNS_FILE)


def test_the_shipped_patterns_file_loads(classifier):
    assert not classifier.is_empty


@pytest.mark.parametrize(
    ("line", "category"),
    [
        ("13:02:11.204 [error] Error / not_signed_in", "auth_lost"),
        ("Cannot refresh access token: :not_signed_in", "auth_lost"),
        ("Token refresh failed", "refresh_failure"),
        ("{'error': 'invalid_grant'}", "refresh_failure"),
        ("login_required", "refresh_failure"),
        ("Postgrex.Protocol (#PID<0.1.0>) disconnected", "database_failure"),
        ("DBConnection.ConnectionError: connection not available", "database_failure"),
        ("tcp recv (idle): closed", "database_failure"),
        ("could not connect to server", "database_failure"),
        ("Could not decrypt API tokens", "token_decryption"),
        ("Refreshed api tokens", "refresh_success"),
        ("Finch.TransportError: timeout", "network_failure"),
        ("Mint.TransportError", "network_failure"),
    ],
)
def test_known_lines_are_classified(classifier, line, category):
    counts = classifier.classify(line).counts
    assert counts.get(category, 0) >= 1


def test_ordinary_lines_match_nothing(classifier):
    text = "\n".join(
        [
            "13:00:00.000 [info] Starting TeslaMate",
            "13:00:01.000 [info] Vehicle 1 is now online",
            "13:00:02.000 [info] Drive started",
        ]
    )
    assert classifier.classify(text).counts == {}


def test_counts_accumulate_across_lines(classifier):
    text = "\n".join(["invalid_grant"] * 4)
    assert classifier.classify(text).counts["refresh_failure"] == 4


# --- thresholds -------------------------------------------------------------


def test_a_single_transient_line_does_not_confirm_a_refresh_failure(classifier):
    signals = classifier.signals(classifier.classify("invalid_grant"))
    assert signals.auth_refresh_failing is False


def test_repeated_refresh_failures_confirm(classifier):
    text = "\n".join(["invalid_grant"] * 3)
    signals = classifier.signals(classifier.classify(text))
    assert signals.auth_refresh_failing is True


def test_a_single_token_decryption_failure_confirms_immediately(classifier):
    signals = classifier.signals(classifier.classify("Could not decrypt API tokens"))
    assert signals.token_decryption_failure is True


def test_a_single_auth_lost_line_confirms_immediately(classifier):
    signals = classifier.signals(classifier.classify("Error / not_signed_in"))
    assert signals.auth_lost is True


def test_one_network_blip_does_not_confirm(classifier):
    signals = classifier.signals(classifier.classify("Finch.TransportError"))
    assert signals.matches.get("network_failure") == 1
    # network_failure has no state of its own; it must not raise an auth signal.
    assert signals.auth_refresh_failing is False


# --- security ---------------------------------------------------------------


def test_excerpts_are_truncated_so_a_token_cannot_survive(classifier):
    token = "x" * 400
    line = f"Token refresh failed for refresh_token={token}"
    classification = classifier.classify(line)

    excerpt = classification.examples["refresh_failure"]
    assert len(excerpt) <= MAX_EXCERPT_CHARS + 1
    assert token not in excerpt


def test_classification_output_carries_counts_not_content(classifier):
    line = "Token refresh failed: eyJhbGciOiJSUzI1NiJ9.PAYLOAD.SIGNATURE"
    signals = classifier.signals(classifier.classify(line))
    assert "eyJhbGciOiJSUzI1NiJ9" not in str(signals.matches)


def test_truncate_collapses_whitespace():
    assert truncate("  a\t\tb\n c  ") == "a b c"


# --- degradation ------------------------------------------------------------


def test_a_missing_patterns_file_disables_classification_without_raising(tmp_path):
    classifier = LogClassifier.from_file(tmp_path / "absent.yaml")
    assert classifier.is_empty
    assert classifier.classify("anything").counts == {}


def test_an_invalid_regex_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "patterns.yaml"
    path.write_text(
        "categories:\n"
        "  auth_lost:\n"
        "    - '[unclosed'\n"
        "    - 'not_signed_in'\n"
    )
    classifier = LogClassifier.from_file(path)
    assert classifier.classify("not_signed_in").counts["auth_lost"] == 1


def test_empty_log_text_is_handled(classifier):
    assert classifier.classify("").counts == {}
    assert classifier.classify("\n\n  \n").counts == {}


def test_a_blown_fuse_reports_an_unhealthy_logger_not_an_auth_failure(classifier):
    """The daily false alarm: a blown fuse is repeated API errors, not a logout.

    TeslaMate's `healthy?/1` IS the fuse state, so the same event already
    reaches us as healthy=false over MQTT. Classifying `fuse_blown` as auth
    outranked logger health, told the user to re-enter working tokens, and —
    because auth states are restart-forbidden — could never self-heal.
    """
    from src.http_check import HttpCategory, HttpResult
    from src.state_machine import Observation, State, evaluate

    signals = classifier.signals(classifier.classify("fuse_blown for :vehicle_1"))
    assert signals.auth_lost is False
    assert signals.auth_refresh_failing is False

    observation = Observation(
        http=HttpResult(category=HttpCategory.AUTHENTICATED, status_code=200),
        logger_healthy=False,
        database_healthy=True,
        log_signals=signals,
    )
    assert evaluate(observation).state is State.LOGGER_UNHEALTHY
