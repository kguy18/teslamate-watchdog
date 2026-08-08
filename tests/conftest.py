from __future__ import annotations

import os

import pytest

from src.config import Config

_MANAGED_PREFIXES = (
    "TESLAMATE_",
    "MQTT_",
    "CHECK_",
    "HTTP_",
    "FAILURE_",
    "RECOVERY_",
    "AUTO_RESTART",
    "RESTART_",
    "POST_RESTART",
    "MAX_RESTARTS",
    "DIAGNOSTIC_",
    "HA_DISCOVERY",
    "STALENESS_",
    "DOCKER_HOST",
    "DATABASE_CONTAINER",
    "LOG_LEVEL",
    "PATTERNS_FILE",
    "DATA_DIR",
)


@pytest.fixture
def make_config(monkeypatch):
    """Build a Config from a clean environment, with overrides applied."""

    def _make(**overrides) -> Config:
        for key in list(os.environ):
            if key.startswith(_MANAGED_PREFIXES):
                monkeypatch.delenv(key, raising=False)
        env = {"MQTT_HOST": "broker.invalid", **{k: str(v) for k, v in overrides.items()}}
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Config.from_env()

    return _make
