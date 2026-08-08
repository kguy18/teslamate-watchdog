"""Home Assistant MQTT discovery payloads.

Per-entity discovery (``<prefix>/<component>/<node>/<object>/config``) rather
than the newer single-device format, because per-entity works on every HA
version the user might be running.

Every entity carries the watchdog's availability topic, so if the watchdog dies
its entities go unavailable rather than freezing on a stale "HEALTHY".

Binary sensors are published as ``true``/``false``/``unknown`` on the wire and
mapped to ON/OFF/None by a value template, so "we don't know yet" shows as
unknown in HA instead of silently reading as "off" (which would look healthy).
"""

from __future__ import annotations

import json
import logging

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

NODE_ID = "teslamate_watchdog"

#: true -> ON, false -> OFF, anything else (including "unknown") -> unknown.
_TRISTATE_TEMPLATE = (
    "{% if value == 'true' %}ON{% elif value == 'false' %}OFF{% else %}None{% endif %}"
)
#: Empty payload -> unknown rather than an invalid timestamp.
_TIMESTAMP_TEMPLATE = "{% if value %}{{ value }}{% else %}None{% endif %}"


def _device() -> dict[str, object]:
    return {
        "identifiers": [NODE_ID],
        "name": "TeslaMate Watchdog",
        "manufacturer": "teslamate-watchdog",
        "model": "TeslaMate Watchdog",
        "sw_version": __version__,
    }


def _binary_sensor(config: Config, object_id: str, name: str, icon: str) -> dict[str, object]:
    return {
        "name": name,
        "unique_id": f"{NODE_ID}_{object_id}",
        "object_id": f"{NODE_ID}_{object_id}",
        "state_topic": config.topic(object_id),
        "value_template": _TRISTATE_TEMPLATE,
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": icon,
    }


def _timestamp_sensor(config: Config, object_id: str, name: str, icon: str) -> dict[str, object]:
    return {
        "name": name,
        "unique_id": f"{NODE_ID}_{object_id}",
        "object_id": f"{NODE_ID}_{object_id}",
        "state_topic": config.topic(object_id),
        "device_class": "timestamp",
        "value_template": _TIMESTAMP_TEMPLATE,
        "icon": icon,
    }


def build_entities(config: Config) -> list[tuple[str, str, dict[str, object]]]:
    """Return ``(component, object_id, payload)`` for every discovered entity."""
    entities: list[tuple[str, str, dict[str, object]]] = [
        (
            "sensor",
            "state",
            {
                "name": "State",
                "unique_id": f"{NODE_ID}_state",
                "object_id": f"{NODE_ID}_state",
                "state_topic": config.topic("state"),
                "icon": "mdi:shield-search",
            },
        ),
        ("binary_sensor", "healthy", _binary_sensor(config, "healthy", "Healthy", "mdi:heart-pulse")),
        (
            "binary_sensor",
            "authenticated",
            _binary_sensor(config, "authenticated", "Authenticated", "mdi:key"),
        ),
        (
            "binary_sensor",
            "database_healthy",
            _binary_sensor(config, "database_healthy", "Database healthy", "mdi:database"),
        ),
        (
            "binary_sensor",
            "logger_healthy",
            _binary_sensor(config, "logger_healthy", "Logger healthy", "mdi:car-connected"),
        ),
        (
            "sensor",
            "last_failure",
            _timestamp_sensor(config, "last_failure", "Last failure", "mdi:alert-circle-outline"),
        ),
        (
            "sensor",
            "last_restart",
            _timestamp_sensor(config, "last_restart", "Last restart", "mdi:restart"),
        ),
        (
            "sensor",
            "restart_count_24h",
            {
                "name": "Restarts in 24h",
                "unique_id": f"{NODE_ID}_restart_count_24h",
                "object_id": f"{NODE_ID}_restart_count_24h",
                "state_topic": config.topic("restart_count_24h"),
                "state_class": "measurement",
                "icon": "mdi:counter",
            },
        ),
    ]

    device = _device()
    availability = {
        "availability_topic": config.availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    for _component, _object_id, payload in entities:
        payload.update(availability)
        payload["device"] = device
    return entities


def publish(config: Config, publish_raw) -> None:
    """Publish retained discovery configs. ``publish_raw(topic, payload, retain=)``."""
    if not config.ha_discovery_enabled:
        log.info("HA discovery disabled")
        return

    entities = build_entities(config)
    for component, object_id, payload in entities:
        topic = f"{config.ha_discovery_prefix}/{component}/{NODE_ID}/{object_id}/config"
        publish_raw(topic, json.dumps(payload), retain=True)

    log.info(
        "HA discovery published",
        extra={"context": {"entities": len(entities), "prefix": config.ha_discovery_prefix}},
    )
