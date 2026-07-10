# seestar-sync v0.2 — MQTT/HA Device Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** seestar-sync appears in Home Assistant as an MQTT-discovered device with live progress, run sensors, daemon liveness (LWT), and writable runtime settings (DRY_RUN, RECLAIM_ENABLED, SEESTAR_HOST).

**Architecture:** New `mqtt.py` (MqttReporter on paho's background thread, never fails a run) + new `settings.py` (SettingsStore: MQTT-written overrides in the archive meta dir, resolved over env once per pass) + a `ProgressEvent` observer seam in `run_sync`. HA webhook reporter removed; Kuma kept. Spec: `docs/superpowers/specs/2026-07-10-seestar-sync-mqtt-design.md`.

**Tech Stack:** Python ≥3.12, `uv`, `smbprotocol`, **`paho-mqtt>=2,<3`** (new), stdlib elsewhere, pytest.

## Global Constraints

- All v0.1 global constraints still apply (see `docs/superpowers/plans/2026-07-09-seestar-sync.md`), notably: pathspec commits, signed commits (retry with sandbox disabled on signing-agent errors, never `--no-gpg-sign`), `uv` for everything, reporters never raise, delete-safety layers untouched.
- MQTT is entirely opt-in: `MQTT_HOST` unset → byte-for-byte v0.1 behavior (no paho import cost at runtime is NOT required; importing is fine, connecting is not).
- MQTT lives in `daemon` mode only; SettingsStore overrides apply in EVERY mode (effective config is mode-independent).
- Precedence: settings.json override > env default, resolved ONCE per pass at pass start.
- MQTT credentials are secrets: never logged, never in status.json/history; redact broker host/user/pass from exception text in warnings.
- The existing shipped code may contain cosmetic Boy-Scout deviations from v0.1 plan snippets (e.g., named constants). When a Modify step's "old" anchor differs cosmetically, adapt to the current code and note it in your report — do not revert Boy-Scout improvements.
- The daemon is RUNNING in production during this work (docker compose, first big copy). Do not restart/stop it; deployment happens after final review.
- Task numbering continues v0.1: Tasks 14–20, Batches G=[14,15,16] (parallel), H=[17], I=[18], J=[19,20] (parallel).

## File Structure

```
src/seestar_sync/config.py     Task 14  MQTT env vars; ha_webhook_url removed
src/seestar_sync/reporters.py  Task 14  post_ha_webhook removed
src/seestar_sync/models.py     Task 15  ProgressEvent
src/seestar_sync/syncrun.py    Task 15  observer seam
src/seestar_sync/settings.py   Task 16  SettingsStore + resolve_effective (new)
src/seestar_sync/mqtt.py       Task 17  MqttReporter (new)
src/seestar_sync/cli.py        Task 18  wiring + third-party log quieting + status sources
pyproject.toml                 Task 17  paho-mqtt dependency
docker-compose.yml, README.md  Task 19
tests/test_mqtt_live.py        Task 20  gated live ring (new)
```

---

### Task 14: MQTT config surface + HA webhook removal

**Files:**
- Modify: `src/seestar_sync/config.py`
- Modify: `src/seestar_sync/reporters.py`
- Test: `tests/test_config.py`, `tests/test_reporters.py`

**Interfaces:**
- Produces: `Config` gains `mqtt_host: str | None`, `mqtt_port: int`, `mqtt_username: str | None`, `mqtt_password: str | None`, `mqtt_topic_prefix: str`; loses `ha_webhook_url`. `reporters` loses `post_ha_webhook`; `report_all(config, meta_dir, summary)` signature unchanged.
- Consumes: existing `_bool`/`_number`/`_http_url` helpers (keep `_http_url` — Kuma still uses it).

- [ ] **Step 1: Write the failing tests** — in `tests/test_config.py` add:

```python
def test_mqtt_defaults_and_parse():
    cfg = Config.from_env(base_env())
    assert cfg.mqtt_host is None and cfg.mqtt_port == 1883
    assert cfg.mqtt_username is None and cfg.mqtt_password is None
    assert cfg.mqtt_topic_prefix == "seestar-sync"
    cfg = Config.from_env(base_env(
        MQTT_HOST="homeassistant.local", MQTT_PORT="1884",
        MQTT_USERNAME="seestar", MQTT_PASSWORD="hunter2",
        MQTT_TOPIC_PREFIX="scope2",
    ))
    assert cfg.mqtt_host == "homeassistant.local" and cfg.mqtt_port == 1884
    assert cfg.mqtt_username == "seestar" and cfg.mqtt_password == "hunter2"
    assert cfg.mqtt_topic_prefix == "scope2"


def test_mqtt_validation_is_fatal():
    import pytest
    with pytest.raises(ConfigError, match="MQTT_PORT"):
        Config.from_env(base_env(MQTT_HOST="h", MQTT_PORT="70000"))
    with pytest.raises(ConfigError, match="MQTT_HOST"):
        Config.from_env(base_env(MQTT_USERNAME="u"))  # auth without host
    with pytest.raises(ConfigError, match="MQTT_USERNAME"):
        Config.from_env(base_env(MQTT_HOST="h", MQTT_PASSWORD="p"))
    with pytest.raises(ConfigError, match="MQTT_TOPIC_PREFIX"):
        Config.from_env(base_env(MQTT_HOST="h", MQTT_TOPIC_PREFIX="a/b"))


def test_ha_webhook_url_is_gone():
    assert not hasattr(Config.from_env(base_env()), "ha_webhook_url")
```

(`import pytest` is already at module top — do not duplicate.) Remove `test_webhook_urls_must_be_http`'s HA half: it becomes Kuma-only:

```python
def test_kuma_url_must_be_http():
    with pytest.raises(ConfigError, match="KUMA_PUSH_URL"):
        Config.from_env(base_env(KUMA_PUSH_URL="file:///etc/passwd"))
    cfg = Config.from_env(base_env(KUMA_PUSH_URL="https://kuma.local/api/push/x"))
    assert cfg.kuma_push_url == "https://kuma.local/api/push/x"
```

Also in `tests/test_config.py`: `test_minimal_env_uses_defaults` currently asserts `cfg.ha_webhook_url is None` — replace that line with `assert cfg.mqtt_host is None` (the field no longer exists).

In `tests/test_reporters.py`: delete the `HA_WEBHOOK_URL` key from `test_reporters_never_raise`'s env dict, and add:

```python
def test_ha_webhook_reporter_is_gone():
    import seestar_sync.reporters as reporters

    assert not hasattr(reporters, "post_ha_webhook")
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_config.py tests/test_reporters.py -v`
Expected: new tests FAIL (AttributeError / missing ConfigError); `test_ha_webhook_url_is_gone` and `test_ha_webhook_reporter_is_gone` FAIL (attrs still exist).

- [ ] **Step 3: Implement config changes** — in `src/seestar_sync/config.py`:

Replace the `ha_webhook_url: str | None` dataclass field with:

```python
    mqtt_host: str | None
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None = dataclasses.field(repr=False)  # never in tracebacks
    mqtt_topic_prefix: str
```

(add `import dataclasses` to config.py's imports; the field has no default —
`repr=False` only. Python permits `field(repr=False)` without a default in a
frozen dataclass as long as later fields also lack defaults, which holds here.)

Apply the same to the Kuma push URL (its token has the same repr-leak class):

```python
    kuma_push_url: str | None = dataclasses.field(repr=False)  # push token
```

In `from_env`, replace `ha_webhook_url=_http_url(env, "HA_WEBHOOK_URL"),` with `# (HA webhook removed in v0.2 — MQTT replaces it; Kuma stays)` and add before the `return cls(...)`:

```python
        mqtt_host = env.get("MQTT_HOST", "").strip() or None
        mqtt_port = int(_number(env, "MQTT_PORT", "1883", int))
        if not (1 <= mqtt_port <= 65535):
            raise ConfigError(f"MQTT_PORT must be 1-65535, got {mqtt_port}")
        mqtt_username = env.get("MQTT_USERNAME", "").strip() or None
        mqtt_password = env.get("MQTT_PASSWORD", "") or None
        if (mqtt_username or mqtt_password) and not mqtt_host:
            raise ConfigError("MQTT_USERNAME/MQTT_PASSWORD set but MQTT_HOST is not")
        if mqtt_password and not mqtt_username:
            raise ConfigError("MQTT_PASSWORD set without MQTT_USERNAME")
        mqtt_topic_prefix = env.get("MQTT_TOPIC_PREFIX", "seestar-sync").strip()
        if not mqtt_topic_prefix or any(c in mqtt_topic_prefix for c in "/#+"):
            raise ConfigError(
                "MQTT_TOPIC_PREFIX must be a single topic segment "
                f"(no / # +), got {mqtt_topic_prefix!r}"
            )
```

and the corresponding constructor args:

```python
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mqtt_topic_prefix=mqtt_topic_prefix,
```

Update `_http_url`'s docstring first line to "Outbound reporter URLs (Kuma push) must be http(s)..." (it now validates only Kuma).

- [ ] **Step 4: Implement reporters changes** — in `src/seestar_sync/reporters.py`: delete the entire `post_ha_webhook` function, and in `report_all` delete the two lines:

```python
    if config.ha_webhook_url:
        _guard("ha", post_ha_webhook, config.ha_webhook_url, summary)
```

Update the module docstring's sink list if it names the webhook.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_config.py tests/test_reporters.py -v`
Expected: ALL tests in both files pass (report exact counts — config gains 3, renames 1; reporters gains 1). Then FULL suite: expect failures ONLY if other files referenced `ha_webhook_url` — grep first: `grep -rn "ha_webhook\|HA_WEBHOOK" src/ tests/` and fix any stragglers the same way (there should be none in src besides config/reporters; `.env`/compose/README are Task 19's).

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/config.py src/seestar_sync/reporters.py tests/test_config.py tests/test_reporters.py
git commit -m "feat: MQTT config surface; remove HA webhook reporter (v0.2)" -- src/seestar_sync/config.py src/seestar_sync/reporters.py tests/test_config.py tests/test_reporters.py
```

---

### Task 15: ProgressEvent + observer seam in run_sync

**Files:**
- Modify: `src/seestar_sync/models.py`, `src/seestar_sync/syncrun.py`
- Test: `tests/test_syncrun.py` (and one model test in `tests/test_classify.py`'s file is NOT the place — put event tests in test_syncrun.py)

**Interfaces:**
- Produces: `ProgressEvent` frozen dataclass in models.py: `phase: str` (`"listing" | "copying" | "reclaiming" | "done"`), `files_done: int = 0`, `files_total: int = 0`, `bytes_done: int = 0`, `bytes_total: int = 0`, `current_path: str | None = None`. `run_sync(..., observer: Callable[[ProgressEvent], None] | None = None)`.
- Emission contract (later tasks rely on it): `listing` once at pass start; `copying` with totals once when the copy plan is known (announcement gated on a NON-EMPTY plan, same as reclaiming — idle passes emit only listing/done), then after EACH file copied (files_done/bytes_done cumulative, current_path set); `reclaiming` with totals once when the deletion plan is known, then after each delete; `done` exactly once as the LAST event of every pass (including unreachable and dry-run passes). Dry-run passes emit the same shape with planned counts. Observer callbacks are invoked synchronously; they must not raise (consumer's job — run_sync does NOT guard them; a raising observer crashes loud like any programming error).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_syncrun.py`:

```python
class RecordingObserver:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def phases(self):
        return [e.phase for e in self.events]


def test_observer_sequence_for_copy_pass(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 20, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases()[0] == "listing"
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1  # exactly once, per contract
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[0].files_total == 2 and copying[0].bytes_total == 30
    assert copying[0].files_done == 0  # the plan-known announcement
    assert copying[-1].files_done == 2 and copying[-1].bytes_done == 30
    assert copying[1].current_path == "A_sub/Light_a_20250101-000000.fit"


def test_observer_sequence_for_reclaim_and_done_totals(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    reclaiming = [e for e in obs.events if e.phase == "reclaiming"]
    assert reclaiming[0].files_total == 1 and reclaiming[0].files_done == 0
    assert reclaiming[-1].files_done == 1 and reclaiming[-1].bytes_done == 800
    done = obs.events[-1]
    assert done.phase == "done" and done.files_done == 1  # copies in done event
    assert obs.phases().count("done") == 1


def test_observer_unreachable_pass_emits_listing_then_done(env):
    archive, manifest = env

    class Dead(FakeDevice):
        def list_files(self):
            raise DeviceUnavailable("off")

    obs = RecordingObserver()
    run_sync(Dead(), archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases() == ["listing", "done"]  # exact — implies done once


def test_observer_mid_run_disconnect_still_emits_done(env):
    # done must close EVERY pass — a power-off mid-copy can't strand the
    # consumer's state topic in "copying" until the next pass
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 20, mtime=1.0)
    d.fail_after_reads = 1
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases()[0] == "listing"
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[-1].files_done == 1  # banked progress visible
    assert copying[-1].current_path == "A_sub/Light_a_20250101-000000.fit"
    assert obs.events[-1].files_done == 1  # done carries banked totals


def test_observer_idle_pass_emits_only_listing_and_done(env):
    # the most common production pass (nothing new, every 15 min) must not
    # blip HA's state topic through "copying"
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    run_sync(d, archive, manifest, config(), now=1_000_000.0)  # syncs everything
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_100.0, observer=obs)
    assert obs.phases() == ["listing", "done"]


def test_observer_dry_run_emits_planned_counts(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_000.0, observer=obs)
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[-1].files_done == 1  # planned count, same shape as real
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_syncrun.py -v`
Expected: 4 new tests FAIL with `TypeError: run_sync() got an unexpected keyword argument 'observer'`; existing 16 pass.

- [ ] **Step 3: Add ProgressEvent to `src/seestar_sync/models.py`** (after `RunSummary`):

```python
@dataclass(frozen=True)
class ProgressEvent:
    """Live pass telemetry for observers (e.g. the MQTT reporter).

    Emitted synchronously from inside run_sync; consumers own throttling
    and must never raise.
    """

    phase: str  # listing | copying | reclaiming | done
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_path: str | None = None
```

- [ ] **Step 4: Wire the observer through `src/seestar_sync/syncrun.py`.** Import `ProgressEvent` from `.models`. Change `run_sync`'s signature to add `observer: Callable[[ProgressEvent], None] | None = None` after `should_stop`, and at the top (next to the `should_stop` default) add `notify = observer or (lambda e: None)`, then `notify(ProgressEvent(phase="listing"))` immediately before the `device.list_files()` try block. In the unreachable except branch, before `return summary`, add `notify(ProgressEvent(phase="done"))`. Thread `notify` into both phases: `_copy_phase(..., notify)` and `_reclaim_phase(..., notify)` (append it to both call sites and signatures). At the end of `run_sync`, immediately before the final `return summary` (after the result computation), add:

```python
    notify(ProgressEvent(
        phase="done",
        files_done=summary.files_copied,
        bytes_done=summary.bytes_copied,
        files_total=summary.files_copied,
        bytes_total=summary.bytes_copied,
    ))
```

In `_copy_phase`, after the plan is computed, restructure the loop head to announce totals first:

```python
    plan = plan_copies(files, is_synced, active)
    files_total = len(plan)
    bytes_total = sum(f.size for f in plan)
    files_done = 0
    bytes_done = 0
    if plan:  # idle passes emit only listing/done — no "copying 0/0" state blip
        notify(ProgressEvent(phase="copying", files_total=files_total,
                             bytes_total=bytes_total))
    for f in plan:
```

and wherever `summary.files_copied += 1; summary.bytes_copied += f.size` occurs (BOTH the dry-run branch and the real branch), also:

```python
            files_done += 1
            bytes_done += f.size
            notify(ProgressEvent(phase="copying", files_done=files_done,
                                 files_total=files_total, bytes_done=bytes_done,
                                 bytes_total=bytes_total, current_path=f.path))
```

In `_reclaim_phase`, after `plan = plan_reclaim(...)` and the empty-plan log, add the announcement and mirror the same counters around both the dry-run and real delete branches:

```python
        files_total = len(plan)
        bytes_total = sum(d.file.size for d in plan)
        files_done = 0
        bytes_done = 0
        if plan:
            notify(ProgressEvent(phase="reclaiming", files_total=files_total,
                                 bytes_total=bytes_total))
```

and after each `summary.files_deleted += 1; summary.bytes_reclaimed += d.file.size` (both branches):

```python
                files_done += 1
                bytes_done += d.file.size
                notify(ProgressEvent(phase="reclaiming", files_done=files_done,
                                     files_total=files_total, bytes_done=bytes_done,
                                     bytes_total=bytes_total, current_path=d.file.path))
```

(`Callable` is already imported in syncrun from the should_stop work.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_syncrun.py -v`
Expected: 22 passed. Full suite: all green.

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/models.py src/seestar_sync/syncrun.py tests/test_syncrun.py
git commit -m "feat: ProgressEvent observer seam in run_sync (v0.2)" -- src/seestar_sync/models.py src/seestar_sync/syncrun.py tests/test_syncrun.py
```

---

### Task 16: SettingsStore + effective-config resolution

**Files:**
- Create: `src/seestar_sync/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `OVERRIDE_KEYS = ("dry_run", "reclaim_enabled", "seestar_host")`; class `SettingsStore(path: Path)` with `load() -> dict[str, object]` (validated overrides only; `{}` on missing/corrupt with warning log), `set(key: str, value: object, now_iso: str) -> None` (validates key+type, atomic tmp+replace write; raises `ValueError` on unknown key or wrong type); `resolve_effective(config: Config, store: SettingsStore) -> tuple[Config, dict[str, str]]` — returns a `dataclasses.replace`d Config and a source map `{key: "env" | "mqtt override"}` for all three keys.
- Consumes: `Config` (frozen dataclass — `dataclasses.replace` works).
- File format: `{"dry_run": {"value": true, "modified_at": "..."}, ...}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_settings.py`:

```python
import json

import pytest

from seestar_sync.config import Config
from seestar_sync.settings import OVERRIDE_KEYS, SettingsStore, resolve_effective


def cfg(**over):
    env = {"SEESTAR_HOST": "seestar.local"}
    env.update(over)
    return Config.from_env(env)


def test_missing_file_means_no_overrides(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == {}


def test_set_and_load_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    store.set("seestar_host", "scope2.local", "2026-07-10T00:01:00Z")
    assert store.load() == {"reclaim_enabled": True, "seestar_host": "scope2.local"}
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["reclaim_enabled"]["modified_at"] == "2026-07-10T00:00:00Z"


def test_set_rejects_unknown_keys_and_wrong_types(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="unknown settings key"):
        store.set("sync_interval_minutes", 5, "t")
    with pytest.raises(ValueError, match="dry_run"):
        store.set("dry_run", "yes", "t")  # must be bool, not str
    with pytest.raises(ValueError, match="seestar_host"):
        store.set("seestar_host", "", "t")  # nonempty string required
    with pytest.raises(ValueError, match="seestar_host"):
        store.set("seestar_host", "bad host", "t")  # no whitespace/control chars


def test_corrupt_file_falls_back_to_empty(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    assert SettingsStore(p).load() == {}
    p.write_text(json.dumps({"reclaim_enabled": {"value": "banana", "modified_at": "t"}}))
    assert SettingsStore(p).load() == {}  # wrong type inside is dropped too
    p.write_text(json.dumps({"seestar_host": {"value": "evil host", "modified_at": "t"}}))
    assert SettingsStore(p).load() == {}  # whitespace host dropped at LOAD too


def test_resolve_effective_precedence_and_sources(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    base = cfg()  # env: dry_run False, reclaim_enabled False, host seestar.local
    eff, sources = resolve_effective(base, store)
    assert eff == base
    assert sources == {k: "env" for k in OVERRIDE_KEYS}

    store.set("dry_run", True, "t")
    store.set("seestar_host", "scope2.local", "t")
    eff, sources = resolve_effective(base, store)
    assert eff.dry_run is True and eff.seestar_host == "scope2.local"
    assert eff.reclaim_enabled is False  # untouched key keeps env value
    assert sources["dry_run"] == "mqtt override"
    assert sources["reclaim_enabled"] == "env"
    # the original config is untouched (frozen + replace)
    assert base.dry_run is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'seestar_sync.settings'`

- [ ] **Step 3: Implement `src/seestar_sync/settings.py`**

```python
"""Runtime settings overrides (written via MQTT, user-ratified v0.2).

settings.json lives in the archive meta dir and holds ONLY overrides — env
vars remain the defaults. Precedence: override > env, resolved once per pass
into an effective-config snapshot so a mid-pass change never half-applies.
The file is intent cache, not safety truth: missing/corrupt → env defaults
(reclaim falls back to opt-in-false unless env says otherwise). Fail-safe.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

OVERRIDE_KEYS = ("dry_run", "reclaim_enabled", "seestar_host")

_VALIDATORS = {
    "dry_run": lambda v: isinstance(v, bool),
    "reclaim_enabled": lambda v: isinstance(v, bool),
    # printable, no whitespace/control chars: junk can't reach register_session
    "seestar_host": lambda v: (
        isinstance(v, str)
        and v != ""
        and all(ch.isprintable() and not ch.isspace() for ch in v)
    ),
}


class SettingsStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> dict[str, object]:
        """Validated overrides; {} on missing/corrupt (fail-safe to env)."""
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("settings.json unreadable (%s); using env defaults", e)
            return {}
        out: dict[str, object] = {}
        if not isinstance(raw, dict):
            log.warning("settings.json malformed (not an object); using env defaults")
            return {}
        for key, entry in raw.items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if key in OVERRIDE_KEYS and _VALIDATORS[key](value):
                out[key] = value
            else:
                log.warning("settings.json: dropping invalid entry %r", key)
        return out

    def set(self, key: str, value: object, now_iso: str) -> None:
        if key not in OVERRIDE_KEYS:
            raise ValueError(f"unknown settings key: {key!r}")
        if not _VALIDATORS[key](value):
            raise ValueError(f"invalid value for {key}: {value!r}")
        try:
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict):
                raw = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}
        raw[key] = {"value": value, "modified_at": now_iso}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.replace(tmp, self._path)


def resolve_effective(config: Config, store: SettingsStore) -> tuple[Config, dict[str, str]]:
    """Overrides > env, snapshotted. Returns (effective config, source map)."""
    overrides = store.load()
    sources = {
        key: ("mqtt override" if key in overrides else "env") for key in OVERRIDE_KEYS
    }
    effective = dataclasses.replace(config, **overrides) if overrides else config
    return effective, sources
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_settings.py -v`
Expected: 5 passed. Full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/settings.py tests/test_settings.py
git commit -m "feat: SettingsStore with env-over-override resolution (v0.2)" -- src/seestar_sync/settings.py tests/test_settings.py
```

---

### Task 17: MqttReporter

**Files:**
- Create: `src/seestar_sync/mqtt.py`
- Modify: `pyproject.toml` (add `"paho-mqtt>=2,<3"` to `dependencies`, then `uv lock`)
- Test: `tests/test_mqtt.py`

**Interfaces:**
- Consumes: `Config` (Task 14 fields), `SettingsStore` (Task 16), `ProgressEvent` (Task 15), `RunSummary`, `iso` from syncrun.
- Produces: `MqttReporter(config: Config, store: SettingsStore, client_factory=None, clock=time.monotonic)` with `start() -> None`, `stop() -> None`, `handle_progress(event: ProgressEvent) -> None`, `publish_summary(summary: RunSummary, effective: Config, sources: dict[str, str]) -> None`, `publish_settings(effective: Config, sources: dict[str, str]) -> None`. ALL public methods never raise (guarded, redacted warnings). Module constants: `THROTTLE_SECONDS = 10.0`, `DISCOVERY_PREFIX = "homeassistant"`.
- Topic layout (prefix `P` = `config.mqtt_topic_prefix`): availability `P/availability` (`online`/`offline`, retained, LWT=offline); state sensors `P/state`, `P/progress` (JSON: percent + attributes), `P/summary` (JSON of RunSummary.to_json, retained), `P/fill` (retained); settings state `P/settings/<key>` (retained); commands `P/set/<key>` (subscribed). Discovery configs at `homeassistant/<component>/{P}/<object_id>/config`, retained.

- [ ] **Step 1: Add the dependency**

```bash
uv add "paho-mqtt>=2,<3"
```

Verify `pyproject.toml` gained the pin and `uv.lock` updated. (Commit rides with Step 6.)

- [ ] **Step 2: Write the failing tests** — `tests/test_mqtt.py`:

```python
import json

from seestar_sync.config import Config
from seestar_sync.models import ProgressEvent, RunSummary
from seestar_sync.mqtt import DISCOVERY_PREFIX, THROTTLE_SECONDS, MqttReporter
from seestar_sync.settings import SettingsStore, resolve_effective


class FakeClient:
    """Duck-types the paho v2 surface MqttReporter uses."""

    def __init__(self):
        self.published = []  # (topic, payload, retain)
        self.subscribed = []
        self.will = None
        self.connected = False
        self.on_connect = None
        self.on_message = None

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port)

    def loop_start(self):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        self.connected = False

    def disconnect(self):
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(tmp_path, **env_over):
    env = {"SEESTAR_HOST": "seestar.local", "MQTT_HOST": "ha.local",
           "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p"}
    env.update(env_over)
    config = Config.from_env(env)
    store = SettingsStore(tmp_path / "settings.json")
    fake = FakeClient()
    clock = FakeClock()
    rep = MqttReporter(config, store, client_factory=lambda: fake, clock=clock)
    return rep, fake, clock, config, store


def topics(fake):
    return [t for (t, _, _) in fake.published]


def test_start_publishes_discovery_availability_and_settings(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    assert fake.auth == ("u", "p")
    assert fake.will == ("seestar-sync/availability", "offline", True)
    # availability online, retained
    assert ("seestar-sync/availability", "online", True) in fake.published
    # discovery configs: at least state, progress, the two switches, host text
    disc = [t for t in topics(fake) if t.startswith(f"{DISCOVERY_PREFIX}/")]
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/state/config" in disc
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/progress/config" in disc
    assert f"{DISCOVERY_PREFIX}/switch/seestar-sync/dry_run/config" in disc
    assert f"{DISCOVERY_PREFIX}/switch/seestar-sync/reclaim_enabled/config" in disc
    assert f"{DISCOVERY_PREFIX}/text/seestar-sync/seestar_host/config" in disc
    # every discovery payload shares the device block + availability topic
    for topic, payload, retain in fake.published:
        if topic.startswith(DISCOVERY_PREFIX):
            body = json.loads(payload)
            assert retain is True
            assert body["availability_topic"] == "seestar-sync/availability"
            assert body["device"]["identifiers"] == ["seestar-sync"]
            assert "unique_id" in body
    # command subscriptions
    assert "seestar-sync/set/dry_run" in fake.subscribed
    assert "seestar-sync/set/reclaim_enabled" in fake.subscribed
    assert "seestar-sync/set/seestar_host" in fake.subscribed
    # settings echoed retained (env defaults)
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published
    assert ("seestar-sync/settings/reclaim_enabled", "OFF", True) in fake.published
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published


def test_progress_throttle(tmp_path):
    rep, fake, clock, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()

    rep.handle_progress(ProgressEvent(phase="copying", files_total=100, bytes_total=1000))
    rep.handle_progress(ProgressEvent(phase="copying", files_done=1, files_total=100,
                                      bytes_done=10, bytes_total=1000, current_path="a"))
    # phase change published; second event throttled (same phase, <10s)
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 1

    clock.t += THROTTLE_SECONDS
    rep.handle_progress(ProgressEvent(phase="copying", files_done=2, files_total=100,
                                      bytes_done=20, bytes_total=1000, current_path="b"))
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 2
    body = json.loads(prog[-1][1])
    assert body["percent"] == 2.0 and body["current_path"] == "b"

    # done ALWAYS publishes regardless of throttle, and updates state topic
    rep.handle_progress(ProgressEvent(phase="done", files_done=2, files_total=2,
                                      bytes_done=20, bytes_total=20))
    assert ("seestar-sync/state", "idle", True) in fake.published


def test_command_round_trip_and_invalid_payloads(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    fake.on_message(fake, None, Msg("seestar-sync/set/reclaim_enabled", b"ON"))
    assert store.load() == {"reclaim_enabled": True}
    assert ("seestar-sync/settings/reclaim_enabled", "ON", True) in fake.published

    fake.published.clear()
    fake.on_message(fake, None, Msg("seestar-sync/set/dry_run", b"banana"))
    assert "dry_run" not in store.load()  # rejected
    # state re-echoed so HA snaps back
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published

    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b"scope2.local"))
    assert store.load()["seestar_host"] == "scope2.local"


def test_phase_change_publishes_immediately(tmp_path):
    # spec: phase changes bypass the throttle
    rep, fake, clock, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    rep.handle_progress(ProgressEvent(phase="copying", files_total=2, bytes_total=20))
    rep.handle_progress(ProgressEvent(phase="reclaiming", files_total=1, bytes_total=5))
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 2  # both published despite <10s gap
    assert ("seestar-sync/state", "reclaiming", True) in fake.published


def test_reconnect_republishes_discovery_and_settings(tmp_path):
    # HA/broker restart safety: on_connect fires again → full republish
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    fake.on_connect(fake, None, None, 0, None)
    disc = [t for (t, _, _) in fake.published if t.startswith(DISCOVERY_PREFIX)]
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/state/config" in disc
    assert ("seestar-sync/availability", "online", True) in fake.published
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published


def test_invalid_host_command_rejected_and_reechoed(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b""))
    assert "seestar_host" not in store.load()
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published


def test_stop_publishes_offline_retained(tmp_path):
    # LWT only fires on UNGRACEFUL death; clean shutdown must publish offline
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    rep.stop()
    assert ("seestar-sync/availability", "offline", True) in fake.published


def test_publish_summary_retains_and_never_raises(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    summary = RunSummary(started_at="2026-07-10T04:00:00Z", result="ok",
                         files_copied=3, bytes_copied=30, device_used_percent=81.4)
    effective, sources = resolve_effective(config, store)
    rep.publish_summary(summary, effective, sources)
    pub = {t: (p, r) for (t, p, r) in fake.published}
    assert json.loads(pub["seestar-sync/summary"][0])["files_copied"] == 3
    assert pub["seestar-sync/summary"][1] is True  # retained
    assert pub["seestar-sync/fill"] == ("81.4", True)

    # a broken client must never propagate
    fake.publish = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    rep.publish_summary(summary, effective, sources)  # must not raise


def test_disabled_without_mqtt_host(tmp_path):
    env = {"SEESTAR_HOST": "seestar.local"}
    config = Config.from_env(env)
    store = SettingsStore(tmp_path / "settings.json")
    rep = MqttReporter(config, store, client_factory=lambda: FakeClient())
    rep.start()  # no-op, no client created
    rep.handle_progress(ProgressEvent(phase="copying"))
    rep.stop()  # all safe no-ops
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'seestar_sync.mqtt'`

- [ ] **Step 4: Implement `src/seestar_sync/mqtt.py`**

```python
"""MQTT / Home Assistant Discovery reporter (v0.2).

Same contract as reporters.py: NOTHING here may fail a sync run. Every
public method is guarded; failures log redacted warnings. The paho client
runs on its own background thread with automatic reconnect; HA sees daemon
liveness via the availability topic's Last Will.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from .config import Config
from .models import ProgressEvent, RunSummary
from .settings import OVERRIDE_KEYS, SettingsStore

log = logging.getLogger(__name__)

THROTTLE_SECONDS = 10.0
DISCOVERY_PREFIX = "homeassistant"

_PHASE_TO_STATE = {
    "listing": "listing",
    "copying": "copying",
    "reclaiming": "reclaiming",
    "done": "idle",
}


def _paho_factory():
    import paho.mqtt.client as paho

    return paho.Client(
        callback_api_version=paho.CallbackAPIVersion.VERSION2,
        client_id="seestar-sync",
    )


class MqttReporter:
    def __init__(
        self,
        config: Config,
        store: SettingsStore,
        client_factory: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._store = store
        self._factory = client_factory or _paho_factory
        self._clock = clock
        self._client = None
        self._prefix = config.mqtt_topic_prefix
        self._last_progress_publish = -THROTTLE_SECONDS
        self._last_phase: str | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not self._config.mqtt_host:
            return  # feature disabled: v0.1 behavior
        try:
            client = self._factory()
            if self._config.mqtt_username:
                client.username_pw_set(
                    self._config.mqtt_username, self._config.mqtt_password
                )
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.will_set(self._topic("availability"), "offline", retain=True)
            client.connect_async(self._config.mqtt_host, self._config.mqtt_port)
            # assign BEFORE loop_start: paho's network thread can fire
            # on_connect immediately, and the callbacks publish via self._client
            self._client = client
            client.loop_start()  # paho reconnects automatically from here on
        except Exception as e:  # noqa: BLE001 — reporter must never raise
            log.warning("mqtt start failed (non-fatal): %s", self._redact(e))
            self._client = None

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._publish("availability", "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt stop failed (non-fatal): %s", self._redact(e))

    # ---- publishing ------------------------------------------------------

    def handle_progress(self, event: ProgressEvent) -> None:
        if self._client is None:
            return
        try:
            force = event.phase != self._last_phase or event.phase == "done"
            now = self._clock()
            if not force and now - self._last_progress_publish < THROTTLE_SECONDS:
                return
            self._last_phase = event.phase
            self._last_progress_publish = now
            percent = (
                round(100.0 * event.bytes_done / event.bytes_total, 1)
                if event.bytes_total else 0.0
            )
            self._publish("progress", json.dumps({
                "percent": percent,
                "files_done": event.files_done,
                "files_total": event.files_total,
                "bytes_done": event.bytes_done,
                "bytes_total": event.bytes_total,
                "current_path": event.current_path,
            }))
            self._publish("state", _PHASE_TO_STATE.get(event.phase, "idle"),
                          retain=True)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt progress publish failed (non-fatal): %s", self._redact(e))

    def publish_summary(self, summary: RunSummary, effective: Config,
                        sources: dict[str, str]) -> None:
        if self._client is None:
            return
        try:
            self._publish("summary", json.dumps(summary.to_json()), retain=True)
            if summary.device_used_percent is not None:
                self._publish("fill", str(summary.device_used_percent), retain=True)
            state = "unreachable" if summary.result == "skipped_unreachable" else (
                "error" if summary.errors else "idle"
            )
            self._publish("state", state, retain=True)
            self.publish_settings(effective, sources)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt summary publish failed (non-fatal): %s", self._redact(e))

    def publish_settings(self, effective: Config, sources: dict[str, str]) -> None:
        if self._client is None:
            return
        try:
            self._publish("settings/dry_run", "ON" if effective.dry_run else "OFF",
                          retain=True)
            self._publish("settings/reclaim_enabled",
                          "ON" if effective.reclaim_enabled else "OFF", retain=True)
            self._publish("settings/seestar_host", effective.seestar_host, retain=True)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt settings publish failed (non-fatal): %s", self._redact(e))

    # ---- paho callbacks --------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        try:
            self._publish_discovery()
            self._publish("availability", "online", retain=True)
            for key in OVERRIDE_KEYS:
                client.subscribe(self._topic(f"set/{key}"))
            from .settings import resolve_effective

            effective, sources = resolve_effective(self._config, self._store)
            self.publish_settings(effective, sources)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt on_connect failed (non-fatal): %s", self._redact(e))

    def _on_message(self, client, userdata, msg) -> None:
        try:
            key = msg.topic.rsplit("/", 1)[-1]
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            if key in ("dry_run", "reclaim_enabled"):
                if payload not in ("ON", "OFF"):
                    log.warning("mqtt: invalid payload %r for %s; re-echoing state",
                                payload, key)
                    self._echo_current(key)
                    return
                value: object = payload == "ON"
            elif key == "seestar_host":
                if not payload:
                    log.warning("mqtt: empty seestar_host rejected; re-echoing state")
                    self._echo_current(key)
                    return
                value = payload
            else:
                log.warning("mqtt: command for unknown key %r ignored", key)
                return
            from .syncrun import iso

            old = self._store.load().get(key, "<unset>")
            self._store.set(key, value, iso(time.time()))
            log.warning("settings override via MQTT: %s: %s -> %s (applies next pass)",
                        key, old, value)
            self._echo_current(key)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt command handling failed (non-fatal): %s", self._redact(e))

    # ---- internals -------------------------------------------------------

    def _echo_current(self, key: str) -> None:
        from .settings import resolve_effective

        effective, sources = resolve_effective(self._config, self._store)
        self.publish_settings(effective, sources)

    def _topic(self, suffix: str) -> str:
        return f"{self._prefix}/{suffix}"

    def _publish(self, suffix: str, payload: str, retain: bool = False) -> None:
        self._client.publish(self._topic(suffix), payload, retain=retain)

    def _redact(self, e: Exception) -> str:
        msg = str(e)
        for secret in (self._config.mqtt_password, self._config.mqtt_username,
                       self._config.mqtt_host):
            if secret and secret in msg:
                msg = msg.replace(secret, "<redacted>")
        return msg

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": ["seestar-sync"],
            "name": "Seestar Sync",
            "manufacturer": "seestar-sync",
            "sw_version": _version(),
        }

        def entity(component: str, object_id: str, extra: dict) -> None:
            body = {
                "unique_id": f"seestar-sync_{object_id}",
                "availability_topic": self._topic("availability"),
                "device": device,
                **extra,
            }
            self._client.publish(
                f"{DISCOVERY_PREFIX}/{component}/{self._prefix}/{object_id}/config",
                json.dumps(body), retain=True,
            )

        summary_t = self._topic("summary")
        entity("sensor", "state", {"name": "State", "state_topic": self._topic("state")})
        entity("sensor", "progress", {
            "name": "Progress", "unit_of_measurement": "%",
            "state_topic": self._topic("progress"),
            "value_template": "{{ value_json.percent }}",
            "json_attributes_topic": self._topic("progress"),
        })
        entity("sensor", "fill", {
            "name": "Device fill level", "unit_of_measurement": "%",
            "state_topic": self._topic("fill"),
        })
        entity("sensor", "last_result", {
            "name": "Last run result", "state_topic": summary_t,
            "value_template": "{{ value_json.result }}",
            "json_attributes_topic": summary_t,
        })
        entity("sensor", "last_run", {
            "name": "Last run", "device_class": "timestamp",
            "state_topic": summary_t,
            "value_template": "{{ value_json.last_run }}",
        })
        for object_id, name, template in (
            ("files_copied", "Files copied", "{{ value_json.files_copied }}"),
            ("bytes_copied", "Bytes copied", "{{ value_json.bytes_copied }}"),
            ("files_deleted", "Files deleted", "{{ value_json.files_deleted }}"),
            ("bytes_reclaimed", "Bytes reclaimed", "{{ value_json.bytes_reclaimed }}"),
            ("errors", "Errors", "{{ value_json.errors | length }}"),
        ):
            entity("sensor", object_id, {
                "name": name, "state_topic": summary_t, "value_template": template,
            })
        for key, name in (("dry_run", "Dry run"), ("reclaim_enabled", "Reclaim enabled")):
            entity("switch", key, {
                "name": name,
                "state_topic": self._topic(f"settings/{key}"),
                "command_topic": self._topic(f"set/{key}"),
                "entity_category": "config",
            })
        entity("text", "seestar_host", {
            "name": "Seestar host",
            "state_topic": self._topic("settings/seestar_host"),
            "command_topic": self._topic("set/seestar_host"),
            "entity_category": "config",
        })
        for object_id, name, value in (
            ("sync_interval", "Sync interval (min)", str(self._config.sync_interval_minutes)),
            ("trigger_percent", "Reclaim trigger %", str(self._config.reclaim_trigger_percent)),
            ("target_percent", "Reclaim target %", str(self._config.reclaim_target_percent)),
        ):
            entity("sensor", object_id, {
                "name": name, "state_topic": self._topic(f"diag/{object_id}"),
                "entity_category": "diagnostic",
            })
            self._client.publish(self._topic(f"diag/{object_id}"), value, retain=True)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("seestar-sync")
    except Exception:  # noqa: BLE001 — version is cosmetic
        return "unknown"
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: 9 passed. Full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/mqtt.py tests/test_mqtt.py pyproject.toml uv.lock
git commit -m "feat: MqttReporter with HA discovery, throttled progress, settings commands (v0.2)" -- src/seestar_sync/mqtt.py tests/test_mqtt.py pyproject.toml uv.lock
```

---

### Task 18: CLI wiring, effective config everywhere, log quieting

**Files:**
- Modify: `src/seestar_sync/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `SettingsStore`, `resolve_effective` (Task 16); `MqttReporter` (Task 17); observer param (Task 15).
- Produces: `_one_run(config, archive, device_factory, should_stop=None, mqtt=None) -> int` — resolves effective config at pass start; `main(...)` unchanged externally; third-party loggers quieted unless DEBUG.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_cli.py`:

```python
def test_settings_override_applies_next_pass(tmp_path):
    # write an override the way MQTT would, then run with a default env:
    # the pass must honor the override (mode-independent effective config)
    from seestar_sync.settings import SettingsStore

    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    assert run_cli(tmp_path, "run", "--init", device=d) == 0  # creates meta dir
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("dry_run", True, "2026-07-10T00:00:00Z")
    d.put("M 81_sub/Light_b_20250102-000000.fit", b"more", mtime=2.0)
    assert run_cli(tmp_path, "run", device=d) == 0
    status = json.loads(
        (tmp_path / "archive" / ".seestar-sync" / "status.json").read_text()
    )
    assert status["dry_run"] is True  # override applied despite env default false
    # file b not copied (dry run) — archive only has file a
    assert not (tmp_path / "archive" / "MyWorks" / "M 81_sub"
                / "Light_b_20250102-000000.fit").exists()


def test_status_shows_setting_sources(tmp_path, capsys):
    from seestar_sync.settings import SettingsStore

    run_cli(tmp_path, "run", "--init")
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    assert run_cli(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "reclaim_enabled: True (mqtt override)" in out
    assert "dry_run: False (env)" in out


def test_mqtt_reclaim_override_arms_and_disarms_deletion(tmp_path):
    # the highest-blast-radius HA write, end-to-end in BOTH directions
    from seestar_sync.settings import SettingsStore

    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)  # 80%
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    assert len(d.list_files()) == 1  # env default: reclaim off, file survives
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    assert run_cli(tmp_path, "run", device=d) == 0
    assert d.list_files() == []  # armed via MQTT override → really deleted

    store.set("reclaim_enabled", False, "2026-07-10T01:00:00Z")
    d.put("M 81_sub/Light_b_20250102-000000.fit", b"y" * 800, mtime=2.0)
    assert run_cli(tmp_path, "run", device=d, extra_env={"RECLAIM_ENABLED": "true"}) == 0
    assert len(d.list_files()) == 1  # override False beats env true → survives


def test_mid_pass_override_does_not_half_apply(tmp_path):
    # spec §Testing: effective config is a per-pass SNAPSHOT — an override
    # written mid-pass (as the MQTT thread would) never half-applies
    from seestar_sync.settings import SettingsStore

    store_box = {}

    class FlippingDevice(FakeDevice):
        def open_read(self, path):
            if not store_box.get("flipped"):
                store_box["flipped"] = True
                store_box["store"].set("dry_run", True, "2026-07-10T00:00:00Z")
            return super().open_read(path)

    d = FlippingDevice()
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 4, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 4, mtime=2.0)
    store_box["store"] = SettingsStore(
        tmp_path / "archive" / ".seestar-sync" / "settings.json"
    )
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    # flip happened after file A's read, yet BOTH files really copied
    assert (tmp_path / "archive" / "MyWorks" / "B_sub"
            / "Light_b_20250102-000000.fit").exists()
    d.put("C_sub/Light_c_20250103-000000.fit", b"c" * 4, mtime=3.0)
    assert run_cli(tmp_path, "run", device=d) == 0
    assert not (tmp_path / "archive" / "MyWorks" / "C_sub"
                / "Light_c_20250103-000000.fit").exists()  # next pass: dry run


def test_third_party_loggers_quieted(tmp_path):
    import logging

    run_cli(tmp_path, "run", "--init")
    assert logging.getLogger("smbprotocol").level == logging.WARNING
    assert logging.getLogger("paho").level == logging.WARNING


def test_debug_level_keeps_third_party_loggers_verbose(tmp_path):
    import logging

    logging.getLogger("smbprotocol").setLevel(logging.NOTSET)  # reset from prior test
    run_cli(tmp_path, "run", "--init", extra_env={"LOG_LEVEL": "DEBUG"})
    assert logging.getLogger("smbprotocol").level == logging.NOTSET
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 4 new FAIL (no settings resolution, no source lines, loggers not set); existing 11 pass.

- [ ] **Step 3: Implement in `src/seestar_sync/cli.py`**

Imports: add `from .settings import OVERRIDE_KEYS, SettingsStore, resolve_effective`.

In `main()`, after `logging.basicConfig(...)`, add:

```python
    if config.log_level != "DEBUG":
        # wire chatter (one line per SMB packet) drowns the operator
        # narrative in docker logs; DEBUG re-enables it for protocol work
        for noisy in ("smbprotocol", "spnego", "paho"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
```

Rework `_one_run` to resolve effective config at pass start (after the per-pass `check_ready`) and accept the reporter:

```python
def _one_run(config: Config, archive: Archive, device_factory,
             should_stop=None, mqtt=None) -> int:
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    store = SettingsStore(archive.meta_dir / "settings.json")
    effective, sources = resolve_effective(config, store)
    for key in OVERRIDE_KEYS:
        if sources[key] == "mqtt override":
            log.warning("effective %s comes from MQTT override", key)
    from .device import DeviceUnavailable

    observer = mqtt.handle_progress if mqtt is not None else None
    try:
        device = device_factory(effective)
    except DeviceUnavailable:
        from .models import RunSummary

        summary = RunSummary(started_at=iso(time.time()), result="skipped_unreachable",
                             dry_run=effective.dry_run,
                             reclaim_enabled=effective.reclaim_enabled)
        report_all(effective, archive.meta_dir, summary)
        if mqtt is not None:
            mqtt.publish_summary(summary, effective, sources)
        return 0
    with Manifest(archive.meta_dir / "manifest.db") as manifest:
        summary = run_sync(device, archive, manifest, effective,
                           should_stop=should_stop, observer=observer)
    report_all(effective, archive.meta_dir, summary)
    if mqtt is not None:
        mqtt.publish_summary(summary, effective, sources)
    return 0 if summary.result in ("ok", "skipped_unreachable") else 1
```

(The v0.1 per-pass `check_ready` moves from its old position to the first line here — do not duplicate it.)

In `_cmd_daemon`, construct and manage the reporter around the loop:

```python
    from .mqtt import MqttReporter

    store = SettingsStore(archive.meta_dir / "settings.json")
    mqtt = MqttReporter(config, store) if config.mqtt_host else None
    if mqtt is not None:
        mqtt.start()
    try:
        while not stop.is_set():
            code = _one_run(config, archive, device_factory,
                            should_stop=stop.is_set, mqtt=mqtt)
            if code != 0:
                log.warning("run finished with errors (exit %d); continuing", code)
            stop.wait(interval)
    finally:
        if mqtt is not None:
            mqtt.stop()
    return 0
```

In `_cmd_status`, after the existing prints, add:

```python
    store = SettingsStore(config.archive_dir / ".seestar-sync" / "settings.json")
    effective, sources = resolve_effective(config, store)
    print("settings:")
    for key in OVERRIDE_KEYS:
        print(f"  {key}: {getattr(effective, key)} ({sources[key]})")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 17 passed. Full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/cli.py tests/test_cli.py
git commit -m "feat: wire MQTT + effective settings into CLI; quiet protocol loggers (v0.2)" -- src/seestar_sync/cli.py tests/test_cli.py
```

---

### Task 19: Compose, README, spec-table alignment

**Files:**
- Modify: `docker-compose.yml`, `README.md`

- [ ] **Step 1: compose** — in the `environment:` block, delete `HA_WEBHOOK_URL: ${HA_WEBHOOK_URL:-}` and add after `KUMA_PUSH_URL`:

```yaml
      MQTT_HOST: ${MQTT_HOST:-}            # unset = MQTT disabled
      MQTT_PORT: ${MQTT_PORT:-1883}
      MQTT_USERNAME: ${MQTT_USERNAME:-}
      MQTT_PASSWORD: ${MQTT_PASSWORD:-}
      MQTT_TOPIC_PREFIX: ${MQTT_TOPIC_PREFIX:-seestar-sync}
```

- [ ] **Step 2: README** — remove the HA-webhook rows/paragraphs; add the five MQTT env-var rows to the table (marking MQTT_PASSWORD as a secret, .env only); add a "Home Assistant (MQTT)" section covering: Mosquitto add-on prerequisite + dedicated MQTT user; what appears (device "Seestar Sync", the entity list incl. the three writable settings); settings semantics (applied at next pass, persisted in `.seestar-sync/settings.json`, override > env, visible in `seestar-sync status` with sources); progress throttling (≤1 update/10 s); LWT liveness (entities grey out when the daemon dies — Kuma remains the independent alert). Note the third-party log quieting (and `LOG_LEVEL=DEBUG` to re-enable wire logs). Keep the Security note; extend it one sentence: MQTT credentials live in `.env` alongside the Kuma URL.

- [ ] **Step 3: Verify + commit**

```bash
docker compose config >/dev/null && echo compose-ok   # with your real .env present
uv run pytest 2>&1 | tail -1
git add docker-compose.yml README.md
git commit -m "docs: MQTT env vars in compose; HA/MQTT section in README (v0.2)" -- docker-compose.yml README.md
```

---

### Task 20: Gated live MQTT smoke

**Files:**
- Create: `tests/test_mqtt_live.py`

- [ ] **Step 1: Write the gated file**

```python
"""Live MQTT smoke — requires a reachable broker (e.g. HA Mosquitto add-on).

Run: MQTT_LIVE_TEST=1 MQTT_HOST=<broker> MQTT_USERNAME=u MQTT_PASSWORD=p \
     uv run pytest tests/test_mqtt_live.py -v -s
Publishes discovery + availability to the real broker under the
'seestar-sync-livetest' prefix, then cleans up. Manual checklist afterward:
device appears in HA MQTT integration; toggle DRY_RUN from the dashboard and
confirm settings.json in a scratch dir picks it up; docker stop flips the
availability entity to unavailable (LWT).
"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MQTT_LIVE_TEST") != "1" or not os.environ.get("MQTT_HOST"),
    reason="live test: set MQTT_LIVE_TEST=1 and MQTT_HOST",
)


def test_connect_discover_and_cleanup(tmp_path):
    from seestar_sync.config import Config
    from seestar_sync.mqtt import MqttReporter
    from seestar_sync.settings import SettingsStore

    env = {
        "SEESTAR_HOST": "seestar.local",
        "MQTT_HOST": os.environ["MQTT_HOST"],
        "MQTT_PORT": os.environ.get("MQTT_PORT", "1883"),
        "MQTT_USERNAME": os.environ.get("MQTT_USERNAME", ""),
        "MQTT_PASSWORD": os.environ.get("MQTT_PASSWORD", ""),
        "MQTT_TOPIC_PREFIX": "seestar-sync-livetest",
    }
    config = Config.from_env(env)
    rep = MqttReporter(config, SettingsStore(tmp_path / "settings.json"))
    rep.start()
    time.sleep(3)  # allow connect + on_connect discovery burst
    assert rep._client is not None
    rep.stop()
```

- [ ] **Step 2: Verify the gate**

Run: `uv run pytest tests/test_mqtt_live.py -v`
Expected: 1 skipped. Full suite: all green, 5 skipped total (4 SMB live + 1 MQTT live).

- [ ] **Step 3: Commit**

```bash
git add tests/test_mqtt_live.py
git commit -m "test: gated live MQTT smoke ring (v0.2)" -- tests/test_mqtt_live.py
```

---

## Verification checklist (post-implementation)

- [ ] Full suite green (expected ≈ 97 passed + 5 skipped)
- [ ] `docker compose config` renders the MQTT vars
- [ ] Live MQTT smoke against the Mosquitto add-on (user-coordinated)
- [ ] Live checklist: device in HA, progress moves during a pass, DRY_RUN toggle round-trips, LWT flips on `docker stop`
- [ ] Deploy: rebuild image, `docker compose up -d --build` (user-coordinated — a copy pass may be in flight; the daemon finishes its current file on SIGTERM)
