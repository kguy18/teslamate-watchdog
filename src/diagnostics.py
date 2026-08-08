"""Incident bundles: what was true at the moment something went wrong.

Written before every restart, and on entry to LOGGED_OUT / AUTH_REFRESH_FAILED
even though those never restart — those are the incidents a human has to act on,
so they are the ones worth having a record of.

Nothing token-shaped reaches disk: log lines come through the classifier's
truncation, and the summary carries categories and counts rather than content.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Belt-and-braces redaction applied to any log text before it is written.
#: The classifier already truncates matched lines; this catches raw log dumps.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"), "<jwt-redacted>"),
    (re.compile(r"\b(qts-[A-Za-z0-9_-]{10,})"), "<refresh-token-redacted>"),
    (re.compile(r"(?i)\b(access[_ -]?token|refresh[_ -]?token|bearer)\b\s*[:=]?\s*[A-Za-z0-9._-]{12,}",),
     r"\1 <redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@dataclass(frozen=True)
class Incident:
    directory: Path
    summary_path: Path

    def as_dict(self) -> dict[str, str]:
        return {"directory": str(self.directory), "summary": str(self.summary_path)}


class DiagnosticsWriter:
    def __init__(self, base_dir: str | Path, retention_days: int) -> None:
        self._base = Path(base_dir)
        self._retention_days = retention_days

    def capture(
        self,
        *,
        state: str,
        reason: str,
        summary: dict[str, Any],
        teslamate_log: str = "",
        database_log: str = "",
        docker_state: dict[str, Any] | None = None,
    ) -> Incident | None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self._unique_directory(f"{stamp}_{state.lower()}")

        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error(
                "could not create diagnostics directory",
                extra={"context": {"path": str(directory), "error": str(exc)}},
            )
            return None

        body = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "trigger_reason": reason,
            **summary,
        }

        try:
            self._write(directory / "summary.json", json.dumps(body, indent=2, default=str))
            self._write(directory / "teslamate.log", redact(teslamate_log))
            self._write(directory / "database.log", redact(database_log))
            self._write(
                directory / "docker-state.json",
                json.dumps(docker_state or {}, indent=2, default=str),
            )
        except OSError as exc:
            log.error(
                "could not write diagnostics bundle",
                extra={"context": {"path": str(directory), "error": str(exc)}},
            )
            return None

        log.info("diagnostics captured", extra={"context": {"path": str(directory)}})
        self.prune()
        return Incident(directory=directory, summary_path=directory / "summary.json")

    def _unique_directory(self, base_name: str) -> Path:
        """Avoid two incidents in the same second overwriting each other.

        Timestamps have second resolution, so a same-state incident captured
        twice within one second would otherwise land in the same directory and
        silently replace the earlier bundle.
        """
        candidate = self._base / base_name
        suffix = 2
        while candidate.exists():
            candidate = self._base / f"{base_name}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def prune(self) -> int:
        if self._retention_days <= 0:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        removed = 0
        try:
            entries = list(self._base.iterdir())
        except OSError:
            return 0

        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(entry)
                removed += 1
            except OSError as exc:
                log.warning(
                    "could not prune diagnostics bundle",
                    extra={"context": {"path": str(entry), "error": str(exc)}},
                )
        if removed:
            log.info("pruned old diagnostics", extra={"context": {"removed": removed}})
        return removed
