# TeslaMate Watchdog

Monitors a self-hosted TeslaMate instance, publishes what it finds to MQTT, and
(from stage 2) restarts TeslaMate for the narrow class of failures a restart can
actually fix.

It sends no notifications of its own. Home Assistant consumes the MQTT topics
and owns all notification logic.

## Status

| Stage | Scope | State |
| --- | --- | --- |
| 1 | Config, HTTP sign-in detection, MQTT publish/subscribe, state machine, HA discovery | **Complete** |
| 2 | Socket proxy, container + database checks, log classification, restarts, diagnostics | **Complete** |
| 3 | Extended tests, troubleshooting guide | Complete (220 tests); troubleshooting below |

## Pre-flight status

| # | Check | Status |
| --- | --- | --- |
| 1 | Compose service names | **Assumed** — defaults `teslamate` / `database`, resolved by name then by `com.docker.compose.service` label. Verify below. |
| 2 | Sign-in response shape | **Not yet run** — both detection paths implemented, so this is confirmation, not a blocker. Verify below. |
| 3 | MQTT `healthy` republish behaviour | **Answered from source** — it is an unretained per-summary heartbeat. See [Staleness detection](#staleness-detection). |
| 4 | Car ID | **Assumed `1`** — subscription uses a `+` wildcard, so a wrong id degrades to "watches every car". |
| 5 | TeslaMate version | **Answered — you are on 4.0.1, which already contains the fix.** |

On (5): the handoff pointed at PR #5390, which was **closed unmerged** on
13 June in favour of #5391. The TLS 1.3 / HTTP-2 change for `TESLA_AUTH_HOST`
shipped in **v4.0.1** as PR #5406, alongside the refresh-token fix. No update
needed.

### Still worth confirming

**Compose service names.** If yours differ, set `TESLAMATE_CONTAINER` and
`DATABASE_CONTAINER`. A wrong name is safe but inert — the watchdog logs
`no container matching …` and refuses to restart rather than guessing.

```bash
docker compose config --services && docker compose ps
```

**Sign-in body markers.** Run this while **logged in** — it must print `0`.
Anything else means the markers would false-positive on a healthy instance, and
`TESLAMATE_SIGNIN_BODY_MARKERS` needs narrowing.

```bash
curl -s http://<host>:4000/ | grep -icE 'refresh token|access token'
```

**Restart capability.** The one thing worth proving before you need it. Watch
for `restart_executed` with `"success": true`:

```bash
mosquitto_sub -h <broker> -v -t 'teslamate/watchdog/event'
```

If it reports 403, the socket proxy is missing `POST=1` — see [Security](#security).

## Quick start

```bash
cp .env.example .env
```

Put your broker details in `.env`, then merge the services from
`docker-compose.example.yml` into the compose file that already runs TeslaMate,
and:

```bash
docker compose up -d --build watchdog
```

Adding the services to TeslaMate's own compose file means they join its default
network automatically, so `http://teslamate:4000/` resolves with no extra
wiring. To keep them in a separate compose file instead, declare TeslaMate's
network as `external: true` and attach both services to it.

Watch it work:

```bash
docker compose logs -f watchdog
```

```bash
mosquitto_sub -h <broker> -v -t 'teslamate/watchdog/#'
```

## How it decides

Each cycle produces one candidate state. First match wins:

| Condition | State |
| --- | --- |
| Database unhealthy | `DATABASE_UNHEALTHY` |
| TeslaMate container stopped | `TESLAMATE_UNREACHABLE` (immediate) |
| HTTP shows the sign-in page | `LOGGED_OUT` |
| HTTP unreachable, or 5xx | `TESLAMATE_UNREACHABLE` |
| Logs show repeated auth-refresh failure | `AUTH_REFRESH_FAILED` |
| Vehicle logger unhealthy or stale | `LOGGER_UNHEALTHY` |
| Otherwise | `HEALTHY` |

A candidate must repeat `FAILURE_CONFIRMATION_COUNT` times (default 3) before it
becomes the state — a single blip changes nothing. Returning to `HEALTHY` needs
`RECOVERY_CONFIRMATION_COUNT` consecutive clean checks (default 2). A stopped
container is the one exception and applies immediately.

Two guarantees are enforced by tests: an HTTP 200 never overrides a
confirmed-unhealthy vehicle logger, and a "Refreshed api tokens" log line never
overrides a sign-in page being served right now.

**5xx is a documented extension.** The original spec's decision table has no
state for an application error, and mapping it to `HEALTHY` would silently
defeat the watchdog. It is folded into `TESLAMATE_UNREACHABLE` — "up but
erroring" is the hung case a restart is meant for. If the 5xx is caused by the
database, the database check outranks it *and* the restart guard independently
requires a healthy database.

**An unrecognised response is inconclusive, not healthy.** An unexpected status
code or a redirect somewhere unfamiliar holds the current state and counts
toward neither failure nor recovery, rather than being scored as a healthy
check.

## Restart policy

Restarts are permitted **only** for `LOGGER_UNHEALTHY` and
`TESLAMATE_UNREACHABLE`, and are forbidden for `LOGGED_OUT`,
`AUTH_REFRESH_FAILED`, `DATABASE_UNHEALTHY`, and token-decryption failures.

This is not tunable, and it is not an oversight. When TeslaMate is logged out
its stored refresh token is dead. Restarting brings the container back up still
logged out — it burns the restart budget, delays the notification by
`POST_RESTART_WAIT_SECONDS`, and fixes nothing. Logout goes straight to MQTT so
a human can paste in fresh tokens. Restarts only help hung processes and
transient network faults.

The allowlist lives in [`src/state_machine.py`](src/state_machine.py) as an
allowlist specifically so that a state added later is non-restartable until
someone opts it in deliberately.

Every other condition must also hold: auto-restart enabled, database confirmed
healthy over 2 consecutive checks, failure confirmed, outside the 6-hour
cooldown, and under 2 restarts per 24 hours. PostgreSQL is never restarted.

The sequence is: capture diagnostics → `recovery_started` → restart via the
Docker API → wait `POST_RESTART_WAIT_SECONDS` → require 2 consecutive healthy
checks → `recovered`, or `manual_intervention_required` if it is still failing.
While that runs the state is held at `RECOVERING` so a post-restart blip cannot
race the outcome.

Restart history is persisted to `/data` keyed on wall-clock time, so recreating
the watchdog container cannot reset a cooldown or wipe the daily cap. **`/data`
must be a real volume** — otherwise a recreated container forgets it just
restarted TeslaMate and may immediately do it again.

## MQTT topics

Retained state under `MQTT_BASE_TOPIC` (default `teslamate/watchdog`):

| Topic | Values |
| --- | --- |
| `state` | `STARTING` `HEALTHY` `LOGGED_OUT` `LOGGER_UNHEALTHY` `TESLAMATE_UNREACHABLE` `DATABASE_UNHEALTHY` `AUTH_REFRESH_FAILED` `RECOVERING` `MANUAL_INTERVENTION_REQUIRED` |
| `healthy` | `true` / `false` |
| `authenticated` | `true` / `false` / `unknown` |
| `database_healthy` | `true` / `false` / `unknown` |
| `logger_healthy` | `true` / `false` / `unknown` |
| `http_status` | status code, or `unknown` |
| `last_check`, `last_failure`, `last_restart` | ISO 8601, or `None` |
| `restart_count_24h` | integer |
| `recovery_status` | `idle` / recovery progress |
| `availability` | `online` / `offline` (last will) |

No topic is ever published with an empty payload: an empty retained payload
*deletes* the retained message, so the topic would silently disappear instead of
reporting "nothing yet".

JSON events on `teslamate/watchdog/event` (not retained): `failure_detected`,
`authentication_required`, `database_unhealthy`, and from stage 2
`diagnostics_captured`, `recovery_started`, `restart_executed`, `recovered`,
`manual_intervention_required`.

`recovered` is only emitted after a watchdog-initiated restart. A spontaneous
return to health is signalled by `state` alone.

## Home Assistant

With `HA_DISCOVERY_ENABLED=true` (default) one device appears with eight
entities, all tied to the availability topic so they go unavailable if the
watchdog dies:

- `sensor.teslamate_watchdog_state`
- `binary_sensor.teslamate_watchdog_healthy` — `on` = healthy
- `binary_sensor.teslamate_watchdog_authenticated`
- `binary_sensor.teslamate_watchdog_database_healthy`
- `binary_sensor.teslamate_watchdog_logger_healthy`
- `sensor.teslamate_watchdog_last_failure`
- `sensor.teslamate_watchdog_last_restart`
- `sensor.teslamate_watchdog_restart_count_24h`

Binary sensors go to `unknown` rather than `off` when the answer isn't known, so
"we can't tell" never reads as "fine".

### Pointing your existing automations at it

Your current package uses a curl-based sign-in check. You can trigger off the
watchdog's state instead:

```yaml
automation:
  - alias: "TeslaMate signed out"
    trigger:
      - platform: state
        entity_id: sensor.teslamate_watchdog_state
        to: "LOGGED_OUT"
      - platform: state
        entity_id: sensor.teslamate_watchdog_state
        to: "AUTH_REFRESH_FAILED"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "TeslaMate needs new tokens"
          message: >-
            TeslaMate is signed out and is not recording drives.
            Generate fresh tokens and paste them into the TeslaMate UI.
          data:
            url: "http://teslamate.local:4000/"

  - alias: "TeslaMate watchdog died"
    trigger:
      - platform: state
        entity_id: binary_sensor.teslamate_watchdog_healthy
        to: "unavailable"
        for: "00:05:00"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "The TeslaMate watchdog stopped reporting."
```

To act on the richer event payloads instead, trigger on the MQTT topic directly:

```yaml
trigger:
  - platform: mqtt
    topic: teslamate/watchdog/event
    value_template: "{{ value_json.event }}"
    payload: "authentication_required"
```

## Configuration

All configuration is environment variables. Defaults shown.

| Variable | Default | Notes |
| --- | --- | --- |
| `TESLAMATE_URL` | `http://teslamate:4000/` | |
| `TESLAMATE_CONTAINER` | `teslamate` | Verify against your compose file |
| `DATABASE_CONTAINER` | `database` | Verify against your compose file |
| `DOCKER_HOST` | `tcp://socket-proxy:2375` | Must be `tcp://`/`http://`; `unix://` is rejected |
| `DOCKER_TIMEOUT_SECONDS` | `10` | Reads only — restarts get their own 90s budget |
| `DATABASE_HOST` | *(the `DATABASE_CONTAINER` name)* | TCP fallback when no healthcheck is defined |
| `DATABASE_PORT` | `5432` | TCP fallback only |
| `TESLAMATE_SIGNIN_PATTERN` | `/sign[_-]?in` | Redirect-path regex |
| `TESLAMATE_SIGNIN_BODY_MARKERS` | see `.env.example` | Comma-separated body markers |
| `MQTT_HOST` | *(required)* | No default — the watchdog has no other output |
| `MQTT_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TLS` | `1883` / — / — / `false` | |
| `MQTT_TLS_CA_CERT` | *(system CAs)* | CA that signed your broker's cert; keeps verification on |
| `MQTT_TLS_INSECURE` | `false` | Disables certificate verification — last resort |
| `MQTT_BASE_TOPIC` | `teslamate/watchdog` | |
| `TESLAMATE_HEALTH_TOPIC` | `teslamate/cars/1/healthy` | Wildcard filters are derived from this |
| `CHECK_INTERVAL_SECONDS` | `60` | Minimum 5 |
| `HTTP_TIMEOUT_SECONDS` | `10` | |
| `MQTT_STALE_SECONDS` | `600` | Heartbeat gap tolerated while driving/charging |
| `MQTT_PARKED_STALE_SECONDS` | `5400` | Gap tolerated otherwise; must exceed TeslaMate's `suspend_min` |
| `STALENESS_DETECTION_ENABLED` | `true` | Gated to driving/charging; see below |
| `FAILURE_CONFIRMATION_COUNT` | `3` | |
| `RECOVERY_CONFIRMATION_COUNT` | `2` | |
| `AUTO_RESTART_ENABLED` | `true` | Set `false` to monitor without ever restarting |
| `RESTART_COOLDOWN_SECONDS` | `21600` | 6 hours |
| `POST_RESTART_WAIT_SECONDS` | `90` | |
| `MAX_RESTARTS_PER_24_HOURS` | `2` | |
| `DIAGNOSTIC_DIR` | `/data/diagnostics` | |
| `DIAGNOSTIC_LOG_LOOKBACK` | `2h` | Log context kept in bundles. Accepts `30s` `5m` `2h` `7d` |
| `LOG_ANALYSIS_LOOKBACK` | `15m` | Log window used to **decide state**. Must not exceed the above |
| `DIAGNOSTIC_RETENTION_DAYS` | `14` | |
| `HA_DISCOVERY_ENABLED` | `true` | |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | |
| `LOG_LEVEL` | `INFO` | |

### Log windows

There are two, and the distinction matters:

- `LOG_ANALYSIS_LOOKBACK` (default `15m`) — the window used to **decide state**.
- `DIAGNOSTIC_LOG_LOOKBACK` (default `2h`) — the window captured into
  **diagnostic bundles** for a human to read.

They were originally one setting, which was a defect: a burst of `invalid_grant`
lines kept the state pinned at `AUTH_REFRESH_FAILED` for the full two hours
after tokens had been re-entered and everything else was healthy. Whatever
`LOG_ANALYSIS_LOOKBACK` is set to is also how long a resolved auth incident
stays latched, so keep it short.

The trade-off in the other direction: log signals need enough lines inside the
window to cross their threshold (`refresh_failure` needs 3). If your instance
retries token refresh slowly enough that three failures never land within 15
minutes, raise it to `30m`. A true logout is caught by the HTTP sign-in check
regardless — the log path is a backstop.

### Staleness detection

**On by default.** The reasoning changed once the TeslaMate source was read —
and it is the opposite of the usual assumption.

`healthy` is listed in TeslaMate's `@do_not_retain`, which has two consequences
(verified against the v4.0.1 tag, `lib/teslamate/mqtt/pubsub/vehicle_subscriber.ex`):

1. **It is a heartbeat, not a state value.** The change-detection filter ends
   with `or key in @do_not_retain`, and a separate `publish_values` clause fires
   when nothing changed at all. So `healthy` is republished on *every* vehicle
   summary, not only when it flips.
2. **It is published unretained**, unlike the rest of `teslamate/cars/<id>/*`.
   A subscriber therefore receives nothing on connect.

So absence of `healthy` messages is genuine evidence, not normal quiet. Without
staleness detection there is a real blind spot: if TeslaMate keeps serving HTTP
200 while its vehicle process dies, no summaries are published, `logger_healthy`
sits at `unknown` forever, and an unknown never triggers a failure — so the
state reads `HEALTHY` while drives silently go unrecorded.

**How big a gap is normal depends on what the car is doing.** From the v4.0.1
source:

| Car state | Heartbeat cadence | Applied limit |
| --- | --- | --- |
| `driving`, `charging` | every few seconds | `MQTT_STALE_SECONDS` (600) |
| `online` (idle) | ~10s | `MQTT_PARKED_STALE_SECONDS` (5400) |
| `asleep`, `offline` | 30s (`@asleep_interval`) | `MQTT_PARKED_STALE_SECONDS` |
| `suspended` | **nothing for up to `suspend_min`** | `MQTT_PARKED_STALE_SECONDS` |
| unknown | — | `MQTT_PARKED_STALE_SECONDS` |

`suspended` is the reason the parked limit is 90 minutes rather than something
tighter. TeslaMate deliberately stops polling while suspended so the car can
fall asleep, publishing nothing for that whole window — `suspend_min` minutes,
21 by default and 30 with the streaming API. **If you raise `suspend_min` in
TeslaMate's settings, raise `MQTT_PARKED_STALE_SECONDS` to match**, or you will
get false alarms every time the car settles.

### Two guards against crying wolf

Both matter, because `LOGGER_UNHEALTHY` is a **restartable** state — a false
positive here restarts TeslaMate every cooldown, forever.

1. **A car we have never heard a heartbeat from is never called stale.** Total
   silence is far more often a wrong topic or a missing subscribe permission
   than a dead logger. Instead of failing, the watchdog logs a warning naming
   `TESLAMATE_HEALTH_TOPIC` once, and `logger_healthy` stays `unknown`. The
   consequence is that a logger already dead *before* the watchdog started is
   not detected — that is the deliberate trade.
2. **The clock never runs while the watchdog is disconnected from the broker.**
   Staleness is measured from the later of the last heartbeat and the current
   connection, so a long broker outage does not make every car look instantly
   stale on reconnect (`healthy` is unretained, so nothing arrives until the
   next summary).

Set `STALENESS_DETECTION_ENABLED=false` to turn it all off. If your
`TESLAMATE_HEALTH_TOPIC` is a shape with no derivable car `state` topic, the
default quietly disables staleness and logs why rather than refusing to start;
setting it to `true` explicitly in that situation is an error.

Consequence of the unretained publish: after a watchdog restart,
`logger_healthy` reads `unknown` until the next vehicle summary. That is
expected, not a fault — and it is why an unknown never counts as a failure.

## Security

**No Tesla tokens, ever.** The watchdog never generates, stores, reads, logs or
submits Tesla API tokens. It does not touch TeslaMate's database. Response
bodies are scanned in memory and discarded; only the name of the marker that
matched is recorded. Log lines are truncated to 120 characters before storage,
and diagnostics are additionally passed through a JWT/token redactor. The only
config dump in the codebase is `Config.summary()`, which reports credentials as
`<set>`/`<unset>` and never their values.

**The Docker socket is root-equivalent.** Anyone who can reach
`/var/run/docker.sock` can start a privileged container and own the host. The
watchdog therefore never mounts it — `DOCKER_HOST` rejects a `unix://` value
outright — and all access goes through `tecnativa/docker-socket-proxy`.

### The grant, and what it actually permits

Measured against proxy 0.3.0 with a real daemon, not assumed:

| Proxy config | restart | exec create | exec **start** | delete |
| --- | --- | --- | --- | --- |
| `CONTAINERS=1 ALLOW_RESTARTS=1` | **403** | 403 | 403 | 403 |
| `CONTAINERS=1 POST=1` | 204 | 201 | **403** | 204 |
| `CONTAINERS=1 POST=1 ALLOW_RESTARTS=1` | 204 | 201 | **403** | 204 |

Two results worth stating plainly:

- **`POST=1` is required.** Without it the restart is refused and the watchdog
  can never recover anything. `ALLOW_RESTARTS` does not substitute for it, and
  adds nothing measurable on top of it in this version. It is set anyway so the
  intent is explicit if the proxy's semantics ever tighten.
- **`POST=1` is broad.** It permits restart, stop, kill and delete on every
  container the proxy can see — not just TeslaMate.

Three things contain that, and all three matter:

1. **`EXEC=0` blocks code execution.** Exec *creation* is under the CONTAINERS
   section and is permitted, but `POST /exec/<id>/start` returns 403. So the
   grant cannot become a shell inside the database container. This is also why
   `pg_isready` is not used — database health comes from the Docker healthcheck
   if one is defined, otherwise a TCP connect to the Postgres port.
2. **The proxy sits on its own `internal: true` network** shared only with the
   watchdog. A compromise of TeslaMate, Grafana or the database cannot reach it.
   Verified: a container on TeslaMate's network cannot open a connection to it.
3. **The watchdog validates before it writes.** `DockerClient.restart` refuses
   any name but the configured TeslaMate service, re-resolves the container, and
   refuses outright if the resolved container carries the database's name or
   compose-service label.

Residual risk: an attacker who compromises the watchdog container itself can
restart, stop or delete containers in the TeslaMate stack. They cannot execute
code in them, read images or volumes, or reach the host.

Two more things worth knowing:

- The `:ro` flag on the socket mount protects the socket *file*, not the API
  behind it. The proxy's allowlist and the internal network are what constrain
  access.
- The proxy has no `ports:` stanza and must never be given one.

**Want it tighter?** A proxy that allowlists method+path regexes (for example
`wollomatic/socket-proxy`) can be restricted to exactly
`POST /containers/<id>/restart`, eliminating the stop/kill/delete surface. That
is a stricter posture than anything tecnativa's flags can express; it is not the
default here only because tecnativa is the widely-deployed, spec-named choice.

The container runs as uid 10001. A named volume inherits that ownership; a bind
mount must be chowned to 10001 on the host.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest tests/ -q
```

220 tests, no network or Docker required — HTTP, MQTT, Docker and the clock are
all faked. The suites that matter most:

- `test_restart_manager.py` — the allowlist, cooldown, daily cap, and history surviving container recreation
- `test_state_machine.py` — confirmation counting and the restart allowlist
- `test_recovery.py` — the restart sequence and both of its outcomes
- `test_docker_client.py` — container resolution, restart guards, log demultiplexing
- `test_publishing.py` — exactly what reaches MQTT, including the empty-payload rule
- `test_http_check.py` — both sign-in detection paths

Unit tests alone were not sufficient here. Two bugs surfaced only against a real
daemon and a real broker, and both would have shipped: the socket-proxy grant
that silently refused every restart, and a restart HTTP timeout shorter than
Docker's own stop timeout. If you change the Docker or MQTT layer, test it
against the real thing.

## Troubleshooting

### Recognising the common failures

| What you see | What it means | What to do |
| --- | --- | --- |
| `LOGGED_OUT`, `authenticated=false` | Tokens are dead. TeslaMate is serving the sign-in page and recording nothing. | Generate fresh tokens in a token app, paste them into the TeslaMate UI. No restart will help. |
| `AUTH_REFRESH_FAILED` with `token_decryption` in the bundle | `ENCRYPTION_KEY` changed, so the stored tokens can no longer be read. | Restore the original `ENCRYPTION_KEY`, or re-enter tokens. |
| `DATABASE_UNHEALTHY` | Postgres is down or failing its healthcheck. | Fix the database. The watchdog never restarts it, and it blocks TeslaMate restarts while this holds. |
| `TESLAMATE_UNREACHABLE`, container running | Hung process or a 5xx loop. | This is the case auto-restart exists for; it should self-heal. |
| `TESLAMATE_UNREACHABLE`, `oom_killed: true` in `docker-state.json` | The container was OOM-killed. | Raise the memory limit — restarting just defers it. |
| `LOGGER_UNHEALTHY` while the web UI works | The vehicle logger is unhealthy but the app is up. | Restartable; check the bundle's log classification for a cause. |
| `MANUAL_INTERVENTION_REQUIRED` | A restart happened and did not fix it. | Read the newest bundle under `DIAGNOSTIC_DIR`. |
| `restart withheld … cooldown active` in the log | Working as designed — a restart happened within the last 6 hours. | Wait, or lower `RESTART_COOLDOWN_SECONDS` if you truly want it more aggressive. |
| Two TeslaMate instances polling one account | Tesla invalidates tokens when a second client refreshes them. Shows up as repeated `LOGGED_OUT` that returns after re-entering tokens. | Shut down the duplicate — the watchdog cannot detect this for you. |

### Operational symptoms

| Symptom | Likely cause |
| --- | --- |
| State stuck at `STARTING` | Fewer than 2 clean checks so far, or MQTT never connected — check `docker compose logs watchdog` |
| `restart_executed` reports 403 | Socket proxy needs `POST=1` as well as `CONTAINERS=1` |
| `no container matching 'teslamate'` | `TESLAMATE_CONTAINER` doesn't match a container name or `com.docker.compose.service` label |
| `database_healthy` reads `unknown` | The proxy is unreachable, or the database container was not found |
| `logger_healthy` reads `unknown` after a restart | Expected — `healthy` is unretained, so it arrives on the next vehicle summary |
| `logger_healthy` stuck at `unknown` + a topic warning in the log | No car messages at all: wrong `TESLAMATE_HEALTH_TOPIC` (check TeslaMate's `MQTT_NAMESPACE`) or the MQTT user cannot subscribe |
| `LOGGER_UNHEALTHY` shortly after the car settles | `MQTT_PARKED_STALE_SECONDS` is below TeslaMate's `suspend_min` — raise it |
| `docker socket proxy unreachable` at startup | `DOCKER_HOST` wrong, or the watchdog is not on the proxy's `internal` network |
| Restart history reset | `/data` is not a persistent volume — cooldowns will not survive recreation |
| HA entities never appear | `HA_DISCOVERY_PREFIX` doesn't match HA's discovery prefix, or HA's MQTT integration isn't configured |
| HA entities show `unavailable` | The watchdog is down — the availability topic and last will doing their job |
| `LOGGED_OUT` on a working instance | Body markers matched a logged-in page; narrow `TESLAMATE_SIGNIN_BODY_MARKERS` |
