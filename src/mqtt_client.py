"""MQTT: subscribe to TeslaMate's car topics, publish watchdog state and events.

Connection handling is delegated to paho's own reconnect loop (``loop_start``),
so a broker restart heals itself. Subscriptions and the availability publish
both happen in ``on_connect`` so they survive reconnects.

Staleness deserves a note, because the usual assumption about it is wrong.
``healthy`` is listed in TeslaMate's ``@do_not_retain``, which means it is
published *unretained* and on *every* vehicle summary rather than on change — a
heartbeat, not a state value. A gap is therefore real evidence.

How big a gap is normal depends on what the car is doing (v4.0.1 source):
driving and charging publish seconds apart, an idle car about every 10s, asleep
or offline every ``@asleep_interval`` (30s), but a *suspended* car may publish
nothing for up to its ``suspend_min`` setting — 21 minutes by default, 30 with
the streaming API — because TeslaMate deliberately stops polling so the car can
fall asleep. The two thresholds here reflect that split.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import Config, ConfigError

log = logging.getLogger(__name__)

_TRUE_PAYLOADS: Final = frozenset({"true", "1", "on", "online"})
_FALSE_PAYLOADS: Final = frozenset({"false", "0", "off", "offline"})

#: Car states during which TeslaMate publishes every few seconds, so a short
#: staleness limit applies. Every other state (including unknown) gets the much
#: longer parked limit, which has to clear TeslaMate's suspend window.
ACTIVE_CAR_STATES: Final = frozenset({"driving", "charging"})

PAYLOAD_ONLINE: Final = "online"
PAYLOAD_OFFLINE: Final = "offline"


def parse_bool_payload(payload: str | None) -> bool | None:
    """Case-insensitive tri-state parse. Unrecognised payloads become ``None``."""
    if payload is None:
        return None
    normalised = payload.strip().lower()
    if normalised in _TRUE_PAYLOADS:
        return True
    if normalised in _FALSE_PAYLOADS:
        return False
    return None


@dataclass
class CarObservation:
    car_id: str
    healthy_payload: str | None = None
    healthy: bool | None = None
    healthy_monotonic: float | None = None
    healthy_at: str | None = None
    state_payload: str | None = None
    state_monotonic: float | None = None
    state_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "car_id": self.car_id,
            "healthy_payload": self.healthy_payload,
            "healthy": self.healthy,
            "healthy_at": self.healthy_at,
            "state_payload": self.state_payload,
            "state_at": self.state_at,
        }


@dataclass(frozen=True)
class LoggerHealth:
    """What the car topics say about the vehicle logger, right now."""

    healthy: bool | None
    stale: bool
    car_id: str | None
    detail: str
    cars: dict[str, dict[str, object]] = field(default_factory=dict)


class WatchdogMqtt:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._cars: dict[str, CarObservation] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        #: Monotonic time of the current connection. Staleness is measured from
        #: this at the earliest, so our own downtime never counts against
        #: TeslaMate.
        self._connected_since: float | None = None
        self._warned_no_messages = False
        self._on_connected_hooks: list[Callable[[], None]] = []

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"teslamate-watchdog-{int(time.time())}",
        )
        if config.mqtt_username:
            client.username_pw_set(config.mqtt_username, config.mqtt_password or None)
        if config.mqtt_tls:
            client.tls_set_context(self._build_tls_context(config))

        # Last will: if the watchdog dies without saying goodbye, the broker
        # tells Home Assistant on its behalf.
        client.will_set(
            config.availability_topic, PAYLOAD_OFFLINE, qos=1, retain=True
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client

    @staticmethod
    def _build_tls_context(config: Config) -> ssl.SSLContext:
        """Build the TLS context explicitly.

        A broker in a home lab usually presents a private or self-signed
        certificate, which the system CA store will reject. Point
        MQTT_TLS_CA_CERT at the CA that signed it — that keeps verification on.
        MQTT_TLS_INSECURE exists as a last resort and is deliberately loud.
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        if config.mqtt_tls_insecure:
            # Order matters: check_hostname must be cleared before verify_mode,
            # or Python raises.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            log.warning(
                "MQTT_TLS_INSECURE is set — the broker's certificate is NOT being "
                "verified. Prefer MQTT_TLS_CA_CERT so verification stays on."
            )
            return context

        if config.mqtt_tls_ca_cert:
            try:
                context.load_verify_locations(cafile=config.mqtt_tls_ca_cert)
            except ssl.SSLError as exc:
                # Config already checked the file exists; this means it is not a
                # usable PEM. Say so plainly instead of surfacing "[X509] PEM lib".
                raise ConfigError(
                    f"MQTT_TLS_CA_CERT {config.mqtt_tls_ca_cert!r} is not a readable "
                    f"PEM certificate ({exc}). It should be the CA that signed your "
                    f"broker's certificate, in PEM format."
                ) from exc
        else:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        return context

    # --- lifecycle --------------------------------------------------------

    def add_on_connected(self, hook: Callable[[], None]) -> None:
        """Run ``hook`` after every successful (re)connect."""
        self._on_connected_hooks.append(hook)

    def start(self, *, wait_seconds: float = 10.0) -> bool:
        self._client.connect_async(
            self._config.mqtt_host, self._config.mqtt_port, keepalive=60
        )
        self._client.loop_start()
        connected = self._connected.wait(timeout=wait_seconds)
        if not connected:
            log.warning(
                "MQTT not connected yet; continuing and letting paho retry",
                extra={"context": {"host": self._config.mqtt_host}},
            )
        return connected

    def stop(self) -> None:
        """Say goodbye properly — the LWT only fires on an *ungraceful* exit."""
        if self._connected.is_set():
            self.publish_raw(
                self._config.availability_topic, PAYLOAD_OFFLINE, retain=True
            )
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("error during MQTT disconnect", exc_info=True)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # --- paho callbacks ---------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error(
                "MQTT connection refused",
                extra={"context": {"reason": str(reason_code)}},
            )
            return

        self._connected.set()
        self._connected_since = time.monotonic()
        topics = [(self._config.topics.health_filter, 0)]
        if self._config.topics.state_filter:
            topics.append((self._config.topics.state_filter, 0))
        client.subscribe(topics)

        client.publish(
            self._config.availability_topic, PAYLOAD_ONLINE, qos=1, retain=True
        )
        log.info(
            "MQTT connected",
            extra={"context": {"subscribed": [topic for topic, _ in topics]}},
        )

        for hook in self._on_connected_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 - a bad hook must not kill the loop
                log.exception("on_connected hook failed")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()
        self._connected_since = None
        log.warning(
            "MQTT disconnected; paho will retry",
            extra={"context": {"reason": str(reason_code)}},
        )

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            payload = message.payload.decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            log.debug("undecodable MQTT payload", extra={"context": {"topic": message.topic}})
            return

        car_id = self._config.topics.car_id_from_topic(message.topic)
        now_monotonic = time.monotonic()
        now_wall = datetime.now(timezone.utc).isoformat()

        with self._lock:
            car = self._cars.setdefault(car_id, CarObservation(car_id=car_id))
            if message.topic.endswith("/healthy"):
                car.healthy_payload = payload
                car.healthy = parse_bool_payload(payload)
                car.healthy_monotonic = now_monotonic
                car.healthy_at = now_wall
            elif message.topic.endswith("/state"):
                # Note: `state` is a car state string (online/asleep/driving/...),
                # never a boolean. Do not run it through parse_bool_payload.
                car.state_payload = payload
                car.state_monotonic = now_monotonic
                car.state_at = now_wall

    # --- reads ------------------------------------------------------------

    def logger_health(self) -> LoggerHealth:
        """Summarise the car topics into a single logger verdict.

        Prefers the configured car id; falls back to "any known car reporting
        unhealthy wins" so a mis-set car id degrades to something useful.
        """
        config = self._config
        now = time.monotonic()

        with self._lock:
            cars = {car_id: car for car_id, car in self._cars.items()}
            snapshot = {car_id: car.as_dict() for car_id, car in cars.items()}

        if not cars:
            # Deliberately not a failure: silence from every car is far more
            # often a wrong topic than a dead logger, and LOGGER_UNHEALTHY is
            # restartable. Say so loudly instead, once, so a human can fix it.
            self._warn_if_persistently_silent(now)
            return LoggerHealth(
                healthy=None,
                stale=False,
                car_id=None,
                detail="no car messages received yet",
                cars=snapshot,
            )

        primary = config.topics.primary_car_id
        if primary is not None and primary in cars:
            selected = cars[primary]
        else:
            unhealthy = [car for car in cars.values() if car.healthy is False]
            selected = unhealthy[0] if unhealthy else next(iter(cars.values()))

        stale, stale_detail = self._staleness(selected, now)
        detail = stale_detail or f"car {selected.car_id} healthy={selected.healthy}"
        return LoggerHealth(
            healthy=selected.healthy,
            stale=stale,
            car_id=selected.car_id,
            detail=detail,
            cars=snapshot,
        )

    def _warn_if_persistently_silent(self, now: float) -> None:
        connected_since = self._connected_since
        if connected_since is None or self._warned_no_messages:
            return
        if now - connected_since < self._config.mqtt_parked_stale_seconds:
            return
        self._warned_no_messages = True
        log.warning(
            "no messages on the car health topic since connecting — TeslaMate "
            "should publish 'healthy' at least every few minutes. Check "
            "TESLAMATE_HEALTH_TOPIC matches your topic tree (including any "
            "MQTT_NAMESPACE set on TeslaMate) and that this MQTT user may "
            "subscribe to it. Logger health stays 'unknown' until one arrives.",
            extra={
                "context": {
                    "subscribed": self._config.topics.health_filter,
                    "connected_seconds": int(now - connected_since),
                }
            },
        )

    def _staleness(self, car: CarObservation, now: float) -> tuple[bool, str | None]:
        """Has the `healthy` heartbeat stopped for longer than this state allows?

        `healthy` is republished on every vehicle summary, so a gap is real
        evidence. The tolerated gap depends on what the car is doing: seconds
        apart while driving, but up to TeslaMate's `suspend_min` while suspended,
        during which it deliberately stops polling so the car can sleep.

        Two guards keep this from crying wolf:

        * A car we have never heard a heartbeat from is never called stale. That
          would be indistinguishable from a wrong topic or a missing subscribe
          permission — and LOGGER_UNHEALTHY is restartable, so a topic typo
          would otherwise restart TeslaMate every cooldown, forever.
        * The clock never runs while *we* are disconnected from the broker. Our
          own downtime is not TeslaMate's silence.
        """
        config = self._config
        if not config.staleness_detection_enabled:
            return False, None
        if car.healthy_monotonic is None:
            return False, None

        connected_since = self._connected_since
        if connected_since is None:
            # Not connected: no verdict is possible, and any gap is ours.
            return False, None

        # Restart the clock at reconnect — `healthy` is unretained, so nothing
        # arrives until the next summary and the pre-disconnect timestamp would
        # otherwise make a long outage look instantly stale.
        reference = max(car.healthy_monotonic, connected_since)
        state = (car.state_payload or "").strip().lower()
        active = state in ACTIVE_CAR_STATES
        limit = config.mqtt_stale_seconds if active else config.mqtt_parked_stale_seconds

        age = now - reference
        if age <= limit:
            return False, None

        described = car.state_payload or "in an unknown state"
        return True, (
            f"car {car.car_id} is {described} but 'healthy' has been silent for "
            f"{int(age)}s (limit {limit}s)"
        )

    # --- writes -----------------------------------------------------------

    def publish_raw(
        self, topic: str, payload: str, *, retain: bool = False, qos: int = 1
    ) -> None:
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning(
                "MQTT publish failed",
                extra={"context": {"topic": topic, "rc": int(info.rc)}},
            )

    def publish_state(self, suffix: str, payload: str) -> None:
        self.publish_raw(self._config.topic(suffix), payload, retain=True)

    def publish_event(self, event_type: str, payload: dict[str, Any]) -> None:
        body = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        self.publish_raw(
            self._config.event_topic, json.dumps(body, default=str), retain=False
        )
        log.info("event published", extra={"context": {"event": event_type}})
