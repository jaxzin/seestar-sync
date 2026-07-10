# seestar-sync v0.2 — MQTT / Home Assistant Device Integration

**Date:** 2026-07-10
**Status:** Approved design, pre-implementation
**Base spec:** 2026-07-09-seestar-nas-sync-design.md (v0.1, shipped and running)

## Problem

The v0.1 daemon reports via status.json, JSONL history, and an (unused) HA
webhook. The user wants seestar-sync to appear in Home Assistant as a proper
MQTT-discovered device: live progress during multi-hour passes, run/summary
sensors, and settings that can be changed from HA. The HA webhook reporter is
replaced; Uptime Kuma stays as the independent dead-man switch.

## Decisions (from brainstorming Q&A)

| Decision | Choice |
|---|---|
| Settings from HA | **Writable, runtime-applied**: DRY_RUN + RECLAIM_ENABLED switches, SEESTAR_HOST text. Applied at next pass boundary; persisted overrides |
| Progress | **Throttled live**: publish during passes, ≥10 s between publishes; phase changes and final event always publish immediately |
| Broker | HA Mosquitto add-on; env-var auth (`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`) |
| Old reporters | HA webhook **removed** (never deployed); Kuma **kept** (monitoring must not die with the MQTT stack) |
| Enablement | Entire feature opt-in: `MQTT_HOST` unset → no MQTT anywhere, v0.1 behavior |
| Modes | MQTT lives in `daemon` mode only (persistent connection, LWT). One-shot `run` skips MQTT but DOES honor SettingsStore overrides — effective config is mode-independent |
| Housekeeping | Third-party protocol loggers (`smbprotocol`, `spnego`, `paho`) pinned to WARNING unless `LOG_LEVEL=DEBUG` |

## Architecture

New module `mqtt.py` (an `MqttReporter`) behind the same
never-fail-the-run contract as `reporters.py`. Runs on paho-mqtt's background
thread with auto-reconnect. Three responsibilities:

1. **Discovery + state publishing** — on connect, publish HA MQTT Discovery
   configs (retained) for one device, then retained state topics.
2. **Progress relay** — subscribe to `run_sync`'s new observer callback,
   throttle, publish.
3. **Command handling** — subscribe to command topics for the three writable
   settings; validated writes go to the `SettingsStore`; effective values are
   echoed back (retained) so HA never displays un-applied intent as truth.

`paho-mqtt` becomes the second runtime dependency (pinned `>=2,<3`).

### Device & entities (HA MQTT Discovery)

Device: identifiers `seestar-sync`, name "Seestar Sync", sw_version from
package metadata. Topic root: `seestar-sync/` (single-instance; a
`MQTT_TOPIC_PREFIX` env var allows overriding if a second scope ever runs).

| Entity | Type | Source |
|---|---|---|
| State | sensor | `idle` / `listing` / `copying` / `reclaiming` / `unreachable` / `error` |
| Progress | sensor (%) | observer events; attributes: files done/total, bytes done/total, current file |
| Last run result | sensor | run summary (retained) |
| Files copied / bytes copied / files deleted / bytes reclaimed | sensors | run summary (retained) |
| Errors (count) | sensor | `len(errors)`; full list as attribute |
| Device fill level | sensor (%) | `device_used_percent` (retained) |
| Last run time | timestamp sensor | `last_run` (retained) |
| Daemon online | binary_sensor | availability topic + LWT (`online`/`offline`, retained) |
| DRY_RUN | switch (config category) | writable |
| RECLAIM_ENABLED | switch (config category) | writable |
| SEESTAR_HOST | text (config category) | writable |
| Sync interval / trigger % / target % / version | diagnostic sensors | read-only |

All entities share the availability topic, so everything greys out together
when the daemon dies (LWT) — HA-visible daemon liveness without polling.

### Runtime settings: precedence, persistence, safety

- `SettingsStore` at `<archive>/.seestar-sync/settings.json`, holding ONLY
  MQTT-written overrides (`dry_run`, `reclaim_enabled`, `seestar_host`), each
  with a last-modified timestamp.
- **Precedence: settings.json override > env default.** Resolved ONCE per
  pass, at pass start, into an effective-config snapshot — a mid-pass toggle
  never half-applies. Effective values are published retained after each
  resolution, including at daemon startup (override survives restart and
  still wins over env).
- Corrupt/missing settings.json → env defaults, log warning. The file is
  intent cache, not source of truth for safety: with it gone, reclaim falls
  back to the env value (default false). Fail-safe direction preserved.
- The MQTT switch flips the SAME `reclaim_enabled` gate as v0.1; all four
  delete-safety layers (opt-in gate → DRY_RUN → verified-manifest
  precondition → tier allowlist) are untouched. Every override write logs at
  WARNING with old→new values (audit trail in container logs).
- `seestar-sync status` shows effective settings and their source
  (`env` / `mqtt override`).
- SEESTAR_HOST override applies at next pass (new SMB session to new host);
  the device factory reads the effective host.

### Progress observer seam

- `run_sync(..., observer: Callable[[ProgressEvent], None] | None = None)` —
  same optional-callback pattern as `should_stop`.
- `ProgressEvent` (frozen dataclass in models.py): `phase`
  (`listing|copying|reclaiming|done`), `files_done`, `files_total`,
  `bytes_done`, `bytes_total`, `current_path: str | None`.
- Emitted at phase transitions, after each file copied/deleted, and once as
  `done` with final totals. Pure data out; no I/O inside the sync path.
- Throttling is the CONSUMER's job: `MqttReporter` publishes phase changes
  and `done` immediately, otherwise at most once per 10 s (monotonic clock,
  injectable for tests).
- Dry-run passes emit observer events too (planned counts) — the HA
  dashboard shows dry-run "progress" identically, labeled by the DRY_RUN
  switch state.

### Failure isolation

- All MQTT activity is guarded: connect failure at startup → warning +
  periodic background retry, daemon syncs normally throughout.
- Publish failures are swallowed with redacted warnings (no broker
  credentials in logs — same redaction discipline as reporters._guard).
- Command payloads are strictly validated (`ON`/`OFF` for switches; nonempty
  hostname for text). Invalid → warning, no state change, re-echo current
  state so HA snaps back.
- Broker down: LWT flips HA entities to unavailable; syncs continue; Kuma
  remains the independent alarm channel.

## Config surface (additions)

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_HOST` | *(unset = MQTT disabled)* | Broker hostname |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(unset)* | Broker auth (password is a secret: .env only, never logged) |
| `MQTT_TOPIC_PREFIX` | `seestar-sync` | Topic root + discovery node id |

`HA_WEBHOOK_URL` and `post_ha_webhook` are removed (never deployed; Kuma and
history JSONL unaffected). README/compose updated accordingly.

## Error handling

Inherits v0.1 rules: reporters never fail runs; unexpected exceptions crash
loud; ConfigError → exit 2 (bad MQTT_PORT, username without host, etc.).
MQTT-specific: reconnect with capped exponential backoff; discovery republish
on reconnect (HA restart safe); settings echo on every (re)connect.

## Testing

- Unit: discovery payload shapes (device block, availability, unique_ids),
  throttle behavior (fake clock), command→SettingsStore round-trip incl.
  invalid payloads, precedence resolution (env vs override), settings.json
  corruption fallback, redaction of credentials.
- Integration: run_sync with observer wired to a recording fake — event
  sequence for copy/reclaim/dry-run/unreachable passes; effective-config
  snapshot semantics (mid-pass override doesn't half-apply).
- Fake MQTT client seam (no broker in unit ring); optional gated live ring
  (`MQTT_LIVE_TEST=1`) against the real Mosquitto add-on.
- Live checklist: entity appears in HA, progress moves during a pass, toggle
  DRY_RUN from dashboard, LWT flips on docker stop.

## Out of scope (YAGNI)

- Writable number entities (interval/trigger/target) — read-only diagnostics.
- Multi-device support, HA custom integration/HACS, MQTT TLS (LAN broker;
  revisit if the broker moves off-LAN).
- Historical stats aggregation (HA's recorder handles history).
