"""Classify recent TeslaMate log lines into failure categories.

Patterns live in patterns.yaml so they can be tuned without a rebuild.

SECURITY: matched lines are truncated before they are stored or published, so a
line that happens to carry a token fragment cannot leak into diagnostics or
MQTT. Nothing in here extracts a value out of a log line — only which category
matched and how many times.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from .state_machine import LogSignals

log = logging.getLogger(__name__)

#: Matched lines are recorded only as evidence, never as data. Keep the excerpt
#: short enough that a token cannot survive in it.
MAX_EXCERPT_CHARS = 120

DEFAULT_THRESHOLDS: dict[str, int] = {
    "auth_lost": 1,
    "refresh_failure": 3,
    "database_failure": 3,
    "token_decryption": 1,
    "network_failure": 5,
    "refresh_success": 1,
}


@dataclass(frozen=True)
class Classification:
    counts: dict[str, int] = field(default_factory=dict)
    #: One truncated example per category, for the diagnostics bundle.
    examples: dict[str, str] = field(default_factory=dict)
    lines_scanned: int = 0

    def confirmed(self, category: str, thresholds: dict[str, int]) -> bool:
        threshold = thresholds.get(category, 1)
        return self.counts.get(category, 0) >= threshold

    def as_dict(self) -> dict[str, object]:
        return {
            "counts": self.counts,
            "examples": self.examples,
            "lines_scanned": self.lines_scanned,
        }


def truncate(line: str) -> str:
    collapsed = " ".join(line.split())
    if len(collapsed) <= MAX_EXCERPT_CHARS:
        return collapsed
    return collapsed[:MAX_EXCERPT_CHARS] + "…"


class LogClassifier:
    def __init__(
        self,
        patterns: dict[str, list[re.Pattern[str]]],
        thresholds: dict[str, int] | None = None,
    ) -> None:
        self._patterns = patterns
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    @classmethod
    def from_file(cls, path: str | Path) -> LogClassifier:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error(
                "could not load log patterns; log classification disabled",
                extra={"context": {"path": str(path), "error": str(exc)}},
            )
            return cls({}, {})

        compiled: dict[str, list[re.Pattern[str]]] = {}
        for category, expressions in (raw.get("categories") or {}).items():
            patterns = []
            for expression in expressions or []:
                try:
                    patterns.append(re.compile(expression, re.IGNORECASE))
                except re.error as exc:
                    log.error(
                        "skipping invalid log pattern",
                        extra={"context": {"category": category, "error": str(exc)}},
                    )
            compiled[category] = patterns

        return cls(compiled, raw.get("thresholds") or {})

    @property
    def thresholds(self) -> dict[str, int]:
        return dict(self._thresholds)

    @property
    def is_empty(self) -> bool:
        return not any(self._patterns.values())

    def classify(self, text: str | Iterable[str]) -> Classification:
        lines = text.splitlines() if isinstance(text, str) else list(text)
        counts: dict[str, int] = {}
        examples: dict[str, str] = {}

        for line in lines:
            if not line.strip():
                continue
            for category, patterns in self._patterns.items():
                if any(pattern.search(line) for pattern in patterns):
                    counts[category] = counts.get(category, 0) + 1
                    examples.setdefault(category, truncate(line))

        return Classification(counts=counts, examples=examples, lines_scanned=len(lines))

    def signals(self, classification: Classification) -> LogSignals:
        """Turn counts into the booleans the state machine consumes.

        `refresh_success` is carried for diagnostics only — see the note in
        state_machine.LogSignals about why it never influences state.
        """
        confirmed = lambda category: classification.confirmed(category, self._thresholds)  # noqa: E731
        return LogSignals(
            auth_lost=confirmed("auth_lost"),
            auth_refresh_failing=confirmed("refresh_failure"),
            database_failure=confirmed("database_failure"),
            token_decryption_failure=confirmed("token_decryption"),
            refresh_success=confirmed("refresh_success"),
            matches=dict(classification.counts),
        )
