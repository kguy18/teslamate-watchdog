"""Structured (JSON-lines) logging to stdout.

No dependency on a logging library — one formatter is all this needs. Anything
passed via ``extra={"context": {...}}`` is merged into the JSON record.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = context

        # Anything else attached via extra= lands at the top level.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in ("context", "message", "asctime"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # urllib3 logs every connection attempt at DEBUG and retries at WARNING;
    # the HTTP checker already reports unreachability in a structured way.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
