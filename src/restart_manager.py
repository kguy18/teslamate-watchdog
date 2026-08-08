"""The restart guard, and restart history that survives container recreation.

History is persisted to the data volume and keyed on wall-clock time, not
monotonic time, precisely so that recreating the watchdog container cannot reset
a cooldown or wipe the daily cap. A watchdog that forgets it just restarted
TeslaMate is a watchdog that will restart it again immediately.

Every condition in `evaluate` must pass. The state allowlist is checked first
and comes from state_machine, so there is exactly one place in the codebase that
decides which states are restartable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .state_machine import State, restart_allowed_for_state

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400

#: Consecutive healthy database checks required before a TeslaMate restart.
#: Restarting TeslaMate while Postgres is down produces a crash loop, not a fix.
DATABASE_CONFIRMATION_COUNT = 2

#: History older than this is dropped; only the last 24h affects decisions, the
#: rest is kept for the diagnostics trail.
HISTORY_RETENTION_SECONDS = 7 * SECONDS_PER_DAY


@dataclass(frozen=True)
class RestartRecord:
    at: float
    state: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "iso": datetime.fromtimestamp(self.at, tz=timezone.utc).isoformat(),
            "state": self.state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason}


class RestartManager:
    def __init__(self, config: Config, *, history_path: Path | None = None) -> None:
        self._config = config
        self._path = history_path or Path(config.data_dir) / "restart_history.json"
        self._history: list[RestartRecord] = []
        self._load()

    # --- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error(
                "restart history unreadable; treating as empty. Cooldowns restart "
                "from now, so a restart may be permitted sooner than intended.",
                extra={"context": {"path": str(self._path), "error": str(exc)}},
            )
            return

        for entry in raw.get("restarts", []):
            try:
                self._history.append(
                    RestartRecord(
                        at=float(entry["at"]),
                        state=str(entry.get("state", "")),
                        reason=str(entry.get("reason", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                log.warning("skipping malformed restart history entry")
        self._history.sort(key=lambda record: record.at)
        log.info(
            "restart history loaded",
            extra={"context": {"entries": len(self._history), "path": str(self._path)}},
        )

    def _save(self) -> None:
        payload = {"restarts": [record.as_dict() for record in self._history]}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot truncate the history
            # into "no restarts have ever happened".
            with tempfile.NamedTemporaryFile(
                "w", dir=self._path.parent, delete=False, encoding="utf-8"
            ) as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            temp_path.replace(self._path)
        except OSError as exc:
            log.error(
                "could not persist restart history — cooldowns will not survive a "
                "container recreation",
                extra={"context": {"path": str(self._path), "error": str(exc)}},
            )

    # --- queries ----------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - HISTORY_RETENTION_SECONDS
        self._history = [record for record in self._history if record.at >= cutoff]

    def restarts_in_24h(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - SECONDS_PER_DAY
        return sum(1 for record in self._history if record.at >= cutoff)

    @property
    def last_restart(self) -> RestartRecord | None:
        return self._history[-1] if self._history else None

    def last_restart_iso(self) -> str | None:
        record = self.last_restart
        return None if record is None else record.as_dict()["iso"]  # type: ignore[return-value]

    def seconds_until_cooldown_expires(self, now: float | None = None) -> float:
        record = self.last_restart
        if record is None:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, self._config.restart_cooldown_seconds - (now - record.at))

    # --- the guard --------------------------------------------------------

    def evaluate(
        self,
        state: State,
        *,
        database_healthy_streak: int,
        now: float | None = None,
    ) -> Decision:
        now = time.time() if now is None else now
        config = self._config

        if not config.auto_restart_enabled:
            return Decision(False, "AUTO_RESTART_ENABLED is false")

        if not restart_allowed_for_state(state):
            return Decision(
                False,
                f"{state.value} is not a restartable state — a restart cannot fix it",
            )

        if database_healthy_streak < DATABASE_CONFIRMATION_COUNT:
            return Decision(
                False,
                f"database not confirmed healthy ({database_healthy_streak}/"
                f"{DATABASE_CONFIRMATION_COUNT} consecutive checks)",
            )

        remaining = self.seconds_until_cooldown_expires(now)
        if remaining > 0:
            return Decision(
                False, f"cooldown active for another {int(remaining)}s"
            )

        recent = self.restarts_in_24h(now)
        if recent >= config.max_restarts_per_24_hours:
            return Decision(
                False,
                f"daily cap reached ({recent}/{config.max_restarts_per_24_hours} "
                f"restarts in 24h)",
            )

        return Decision(True, f"{state.value} is restartable and all guards pass")

    def record(self, state: State, reason: str, now: float | None = None) -> RestartRecord:
        now = time.time() if now is None else now
        record = RestartRecord(at=now, state=state.value, reason=reason)
        self._history.append(record)
        self._prune(now)
        self._save()
        log.warning(
            "restart recorded",
            extra={"context": {"state": state.value, "restarts_24h": self.restarts_in_24h(now)}},
        )
        return record
