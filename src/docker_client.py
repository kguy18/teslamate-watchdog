"""Docker Engine API access, exclusively via the socket proxy.

The watchdog never sees /var/run/docker.sock. It speaks plain HTTP to
`tecnativa/docker-socket-proxy`, which is granted only container reads and
`ALLOW_RESTARTS`. Config rejects a `unix://` DOCKER_HOST outright.

Container resolution is by exact name first, then by the
`com.docker.compose.service` label, so this works whether or not the user has
set `container_name:` in their compose file.

Every call degrades to "unknown" rather than raising: the proxy being down must
not take the watchdog down with it, and an unknown is treated by the state
machine as "no verdict" rather than "fine".
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Config

log = logging.getLogger(__name__)

#: Docker multiplexes non-TTY container output into 8-byte-headed frames.
_FRAME_HEADER = 8
_VALID_STREAM_TYPES = {0, 1, 2}

#: Seconds Docker waits for a graceful stop before sending SIGKILL. Passed
#: explicitly so the restart's worst-case duration is known rather than
#: inherited from the daemon default.
RESTART_STOP_TIMEOUT = 30

#: Docker's restart endpoint does not return until the container has stopped
#: (up to RESTART_STOP_TIMEOUT) and started again. Using the ordinary read
#: timeout here makes every restart of a slow-stopping container look like a
#: failure, while the restart actually succeeds in the background.
RESTART_HTTP_TIMEOUT = RESTART_STOP_TIMEOUT + 60


class DockerUnavailable(RuntimeError):
    """The proxy could not be reached or returned something unusable."""


@dataclass(frozen=True)
class ContainerRef:
    id: str
    name: str
    compose_service: str | None

    def matches(self, configured: str) -> bool:
        return self.name == configured or self.compose_service == configured


@dataclass(frozen=True)
class ContainerStatus:
    """A container's state. ``exists=False`` and ``running=None`` mean unknown."""

    exists: bool
    running: bool | None = None
    status: str | None = None
    #: Docker healthcheck verdict: healthy / unhealthy / starting / None if undefined.
    health: str | None = None
    restart_count: int | None = None
    oom_killed: bool | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    ref: ContainerRef | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "running": self.running,
            "status": self.status,
            "health": self.health,
            "restart_count": self.restart_count,
            "oom_killed": self.oom_killed,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "container_id": self.ref.id[:12] if self.ref else None,
            "container_name": self.ref.name if self.ref else None,
            "compose_service": self.ref.compose_service if self.ref else None,
            "error": self.error,
        }


UNKNOWN_STATUS = ContainerStatus(exists=False, error="not checked")


def demultiplex(raw: bytes) -> str:
    """Decode a Docker log stream.

    Containers without a TTY get 8-byte-framed output; containers with one get
    raw bytes. Detect by validating the frame header rather than by asking, so
    this works either way.
    """
    if not raw:
        return ""

    chunks: list[bytes] = []
    offset = 0
    while offset + _FRAME_HEADER <= len(raw):
        header = raw[offset : offset + _FRAME_HEADER]
        stream_type = header[0]
        if stream_type not in _VALID_STREAM_TYPES or header[1:4] != b"\x00\x00\x00":
            # Not framed — treat the whole payload as raw TTY output.
            return raw.decode("utf-8", errors="replace")
        size = int.from_bytes(header[4:8], "big")
        chunks.append(raw[offset + _FRAME_HEADER : offset + _FRAME_HEADER + size])
        offset += _FRAME_HEADER + size

    if offset == 0:
        return raw.decode("utf-8", errors="replace")
    return b"".join(chunks).decode("utf-8", errors="replace")


class DockerClient:
    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._base = config.docker_base_url
        self._timeout = config.docker_timeout_seconds
        self._session = session or requests.Session()

    # --- low level --------------------------------------------------------

    def _get(self, path: str, **params: Any) -> requests.Response:
        try:
            response = self._session.get(
                f"{self._base}{path}", params=params or None, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise DockerUnavailable(
                f"GET {path} -> {response.status_code} {response.text[:200]}"
            )
        return response

    def ping(self) -> bool:
        try:
            self._get("/containers/json", all="1", limit=1)
            return True
        except DockerUnavailable as exc:
            log.warning("docker proxy unreachable", extra={"context": {"error": str(exc)}})
            return False

    # --- resolution -------------------------------------------------------

    def resolve(self, configured: str) -> ContainerRef | None:
        """Find a container by exact name, then by compose-service label."""
        try:
            containers = self._get("/containers/json", all="1").json()
        except (DockerUnavailable, ValueError) as exc:
            log.warning(
                "could not list containers", extra={"context": {"error": str(exc)}}
            )
            return None

        by_label: ContainerRef | None = None
        for entry in containers:
            names = [name.lstrip("/") for name in entry.get("Names", [])]
            labels = entry.get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            ref = ContainerRef(
                id=entry.get("Id", ""), name=names[0] if names else "", compose_service=service
            )
            if configured in names:
                return ref
            if service == configured and by_label is None:
                by_label = ref
        return by_label

    # --- reads ------------------------------------------------------------

    def status(self, configured: str) -> ContainerStatus:
        ref = self.resolve(configured)
        if ref is None:
            return ContainerStatus(exists=False, error=f"no container matching {configured!r}")

        try:
            data = self._get(f"/containers/{ref.id}/json").json()
        except (DockerUnavailable, ValueError) as exc:
            return ContainerStatus(exists=True, ref=ref, error=str(exc))

        state = data.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        return ContainerStatus(
            exists=True,
            running=bool(state.get("Running")),
            status=state.get("Status"),
            health=health,
            restart_count=data.get("RestartCount"),
            oom_killed=bool(state.get("OOMKilled")),
            exit_code=state.get("ExitCode"),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
            ref=ref,
        )

    def logs(self, configured: str, *, since_seconds: int, tail: int = 2000) -> str:
        ref = self.resolve(configured)
        if ref is None:
            return ""
        since = int(time.time() - since_seconds)
        try:
            response = self._get(
                f"/containers/{ref.id}/logs",
                stdout="1",
                stderr="1",
                timestamps="1",
                since=str(since),
                tail=str(tail),
            )
        except DockerUnavailable as exc:
            log.warning(
                "could not read container logs",
                extra={"context": {"container": configured, "error": str(exc)}},
            )
            return ""
        return demultiplex(response.content)

    # --- the only write this client can perform ---------------------------

    def restart(self, configured: str) -> tuple[bool, str]:
        """Restart the named container after re-validating what it resolves to.

        Refuses anything that is not the configured TeslaMate service, and
        refuses the database unconditionally. Returns ``(ok, detail)``.
        """
        expected = self._config.teslamate_container
        if configured != expected:
            return False, (
                f"refusing to restart {configured!r}: only the configured TeslaMate "
                f"container ({expected!r}) may be restarted"
            )

        ref = self.resolve(configured)
        if ref is None:
            return False, f"no container matching {configured!r}"

        # Re-validate the resolved identity rather than trusting resolution.
        if not ref.matches(expected):
            return False, (
                f"resolved container {ref.name!r} (service {ref.compose_service!r}) "
                f"does not match {expected!r}"
            )
        database = self._config.database_container
        if ref.name == database or ref.compose_service == database:
            return False, f"refusing to restart the database container ({database!r})"

        try:
            response = self._session.post(
                f"{self._base}/containers/{ref.id}/restart",
                params={"t": RESTART_STOP_TIMEOUT},
                timeout=RESTART_HTTP_TIMEOUT,
            )
        except requests.Timeout:
            # The restart may well still be in flight. Report failure so the
            # caller escalates to a human rather than assuming success, but say
            # so precisely — this is not the same as "Docker refused".
            return False, (
                f"restart did not complete within {RESTART_HTTP_TIMEOUT}s; it may "
                f"still be in progress. Check the container before retrying."
            )
        except requests.RequestException as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if response.status_code in (204, 304):
            return True, f"restarted {ref.name} ({ref.id[:12]})"
        if response.status_code == 403:
            return False, (
                "socket proxy denied the restart (403) — the proxy needs POST=1 "
                "as well as CONTAINERS=1. ALLOW_RESTARTS on its own is not "
                "sufficient; see the security section of the README."
            )
        return False, f"restart returned {response.status_code}: {response.text[:200]}"


# --- database health --------------------------------------------------------


@dataclass(frozen=True)
class DatabaseHealth:
    healthy: bool | None
    source: str
    detail: str
    status: ContainerStatus = field(default=UNKNOWN_STATUS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "source": self.source,
            "detail": self.detail,
            "container": self.status.as_dict(),
        }


def check_database(config: Config, docker: DockerClient) -> DatabaseHealth:
    """Docker healthcheck if one is defined, otherwise a TCP connect.

    `pg_isready` would need EXEC on the socket proxy, which is close to
    arbitrary code execution in the database container. A TCP connect proves the
    listener is accepting connections, which is what gates a TeslaMate restart.
    """
    status = docker.status(config.database_container)

    if not status.exists:
        return DatabaseHealth(
            healthy=None,
            source="docker",
            detail=status.error or "database container not found",
            status=status,
        )

    if status.running is False:
        return DatabaseHealth(
            healthy=False,
            source="docker",
            detail=f"container not running (status={status.status})",
            status=status,
        )

    if status.health is not None:
        if status.health == "healthy":
            return DatabaseHealth(True, "healthcheck", "docker healthcheck healthy", status)
        if status.health == "starting":
            return DatabaseHealth(None, "healthcheck", "docker healthcheck starting", status)
        return DatabaseHealth(
            False, "healthcheck", f"docker healthcheck {status.health}", status
        )

    reachable, detail = tcp_probe(
        config.database_host, config.database_port, config.docker_timeout_seconds
    )
    return DatabaseHealth(reachable, "tcp", detail, status)


def tcp_probe(host: str, port: int, timeout: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"tcp connect to {host}:{port} succeeded"
    except OSError as exc:
        return False, f"tcp connect to {host}:{port} failed: {exc}"
