# seestar-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dockerized Python daemon that mirrors a Seestar S30 Pro's media to a NAS volume over SMB and reclaims device space via a tiered, free-space-driven deletion policy, keeping stacked JPGs on-device.

**Architecture:** Userspace SMB client (`smbprotocol`) behind a `DeviceClient` interface; pure-function planners (copy diff + reclaim policy); SQLite manifest with write-ahead delete auditing; sentinel- and `st_dev`-guarded archive writer; fire-and-forget reporters (status.json, JSONL history, Uptime Kuma, Home Assistant). Spec: `docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md`.

**Tech Stack:** Python ≥3.12, `uv`, `smbprotocol` (only runtime dep), stdlib `sqlite3`/`argparse`/`urllib`, `pytest`, Docker.

## Global Constraints

- Python `>=3.12`; package/venv managed by `uv` (never pip/venv directly).
- Runtime dependencies: **`smbprotocol` only**. Everything else stdlib.
- Dev dependencies: `pytest` only.
- Names: PyPI-style project `seestar-sync`, module `seestar_sync`, CLI `seestar-sync`.
- License: MIT.
- Device paths are always **relative to `MyWorks/`, forward slashes**, e.g. `"M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit"`. Folder/file names contain spaces — never split on whitespace.
- Deletion is **allowlist-only** via the `Tier` enum (subs → videos → stacked FITs). `.jpg`/`_thn.jpg`/unknown types must be structurally unreachable by the deleter.
- Manifest audit row is committed **before** any SMB delete (write-ahead).
- Reporters (status file, history, Kuma, HA) must **never raise** out of their module and never affect the run result. HTTP timeouts 5 s.
- Exit codes: `0` = ok/skipped_unreachable, `1` = run finished with errors, `2` = fatal (config/archive).
- mtime comparisons use a `2.0` second tolerance (FAT-ish timestamp granularity).
- Commits are GPG/SSH-signed by user config — run `git commit` normally; if signing fails, stop and tell the user (do not `--no-gpg-sign`).
- Run all tests with `uv run pytest` from the repo root.

## File Structure

```
pyproject.toml                     Task 1
src/seestar_sync/__init__.py       Task 1
src/seestar_sync/config.py         Task 1   env parsing + validation
src/seestar_sync/models.py         Task 2   DeviceFile, DiskUsage, Tier, RunSummary
src/seestar_sync/classify.py       Task 2   tier allowlist, timestamps, active folders
src/seestar_sync/manifest.py       Task 3   SQLite manifest + failure counts
src/seestar_sync/archive.py        Task 4   sentinel, st_dev guard, .part+verify+rename
src/seestar_sync/device.py         Task 5   DeviceClient protocol + DeviceUnavailable
tests/fakes.py                     Task 5   FakeDevice (in-memory DeviceClient)
src/seestar_sync/planner.py        Task 6   copy plan (pure)
src/seestar_sync/reclaim.py        Task 7   reclaim plan (pure)
src/seestar_sync/reporters.py      Task 8   status/history/Kuma/HA
src/seestar_sync/syncrun.py        Task 9   single-run orchestrator
src/seestar_sync/smb_client.py     Task 10  SmbDeviceClient (smbprotocol)
src/seestar_sync/cli.py            Task 11  argparse subcommands + daemon loop
Dockerfile, docker-compose.yml     Task 12
README.md                          Task 12
tests/test_smb_live.py             Task 13  gated live smoke
```

---

### Task 1: Project scaffold + config module

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/seestar_sync/__init__.py`, `src/seestar_sync/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` frozen dataclass with fields `seestar_host: str`, `archive_dir: Path`, `sync_interval_minutes: int`, `reclaim_trigger_percent: float`, `reclaim_target_percent: float`, `active_window_minutes: int`, `device_capacity_gb: float | None`, `kuma_push_url: str | None`, `ha_webhook_url: str | None`, `dry_run: bool`, `archive_on_root_fs_ok: bool`, `log_level: str`; classmethod `Config.from_env(env: Mapping[str, str]) -> Config`; exception `ConfigError`.

- [ ] **Step 1: Scaffold the uv project**

```bash
cd /Users/jaxzin/Projects/seestar-backup
uv init --lib --name seestar-sync --python 3.12
uv add smbprotocol
uv add --dev pytest
```

Then replace `pyproject.toml` contents with:

```toml
[project]
name = "seestar-sync"
version = "0.1.0"
description = "Mirror Seestar S30 Pro media to a NAS over SMB and reclaim device space, keeping stacked JPGs on-device"
readme = "README.md"
license = "MIT"
requires-python = ">=3.12"
dependencies = ["smbprotocol>=1.13,<2"]

[project.scripts]
seestar-sync = "seestar_sync.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
dist/
```

Ensure `src/seestar_sync/__init__.py` exists and is empty (delete any `uv init` sample code like `py.typed` demo functions; keep `py.typed` if generated). Delete any generated sample test.

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:

```python
import pytest

from seestar_sync.config import Config, ConfigError


def base_env(**overrides):
    env = {"SEESTAR_HOST": "seestar.local"}
    env.update(overrides)
    return env


def test_minimal_env_uses_defaults():
    cfg = Config.from_env(base_env())
    assert cfg.seestar_host == "seestar.local"
    assert str(cfg.archive_dir) == "/archive"
    assert cfg.sync_interval_minutes == 15
    assert cfg.reclaim_trigger_percent == 75.0
    assert cfg.reclaim_target_percent == 60.0
    assert cfg.active_window_minutes == 15
    assert cfg.device_capacity_gb is None
    assert cfg.kuma_push_url is None
    assert cfg.ha_webhook_url is None
    assert cfg.dry_run is False
    assert cfg.archive_on_root_fs_ok is False
    assert cfg.log_level == "INFO"


def test_missing_host_is_fatal():
    with pytest.raises(ConfigError, match="SEESTAR_HOST"):
        Config.from_env({})


def test_target_must_be_below_trigger():
    env = base_env(RECLAIM_TRIGGER_PERCENT="60", RECLAIM_TARGET_PERCENT="75")
    with pytest.raises(ConfigError, match="RECLAIM_TARGET_PERCENT"):
        Config.from_env(env)


def test_bool_parsing_accepts_common_truthy_values():
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env(base_env(DRY_RUN=raw)).dry_run is True
    assert Config.from_env(base_env(DRY_RUN="false")).dry_run is False


def test_non_numeric_interval_is_fatal():
    with pytest.raises(ConfigError, match="SYNC_INTERVAL_MINUTES"):
        Config.from_env(base_env(SYNC_INTERVAL_MINUTES="soon"))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'seestar_sync.config'`

- [ ] **Step 4: Implement `src/seestar_sync/config.py`**

```python
"""Environment-variable configuration (12-factor; identical across Docker/k8s)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Invalid or missing configuration. CLI maps this to exit code 2."""


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _bool(env: Mapping[str, str], key: str) -> bool:
    raw = env.get(key, "false").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _number(env: Mapping[str, str], key: str, default: str, cast) -> float | int:
    raw = env.get(key, default)
    try:
        return cast(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from e


@dataclass(frozen=True)
class Config:
    seestar_host: str
    archive_dir: Path
    sync_interval_minutes: int
    reclaim_trigger_percent: float
    reclaim_target_percent: float
    active_window_minutes: int
    device_capacity_gb: float | None
    kuma_push_url: str | None
    ha_webhook_url: str | None
    dry_run: bool
    archive_on_root_fs_ok: bool
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        host = env.get("SEESTAR_HOST", "").strip()
        if not host:
            raise ConfigError("SEESTAR_HOST is required")

        trigger = _number(env, "RECLAIM_TRIGGER_PERCENT", "75", float)
        target = _number(env, "RECLAIM_TARGET_PERCENT", "60", float)
        if not (0 < target < trigger <= 100):
            raise ConfigError(
                "RECLAIM_TARGET_PERCENT must be below RECLAIM_TRIGGER_PERCENT "
                f"and both in (0, 100]; got target={target}, trigger={trigger}"
            )

        capacity_raw = env.get("DEVICE_CAPACITY_GB", "").strip()
        capacity = float(capacity_raw) if capacity_raw else None

        return cls(
            seestar_host=host,
            archive_dir=Path(env.get("ARCHIVE_DIR", "/archive")),
            sync_interval_minutes=int(_number(env, "SYNC_INTERVAL_MINUTES", "15", int)),
            reclaim_trigger_percent=trigger,
            reclaim_target_percent=target,
            active_window_minutes=int(_number(env, "ACTIVE_WINDOW_MINUTES", "15", int)),
            device_capacity_gb=capacity,
            kuma_push_url=env.get("KUMA_PUSH_URL") or None,
            ha_webhook_url=env.get("HA_WEBHOOK_URL") or None,
            dry_run=_bool(env, "DRY_RUN"),
            archive_on_root_fs_ok=_bool(env, "ARCHIVE_ON_ROOT_FS_OK"),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src/ tests/
git commit -m "feat: scaffold uv project with env-based Config"
```

---

### Task 2: Models + classification

**Files:**
- Create: `src/seestar_sync/models.py`, `src/seestar_sync/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Produces (`models.py`):
  - `DeviceFile` frozen dataclass: `path: str`, `size: int`, `mtime: float` (epoch seconds).
  - `DiskUsage` frozen dataclass: `total_bytes: int`, `free_bytes: int`, property `used_percent: float`.
  - `Tier(IntEnum)`: `SUBS = 1`, `VIDEOS = 2`, `STACKED_FITS = 3`.
  - `RunSummary` dataclass: `started_at: str`, `result: str`, `dry_run: bool`, `files_copied: int`, `bytes_copied: int`, `files_deleted: int`, `bytes_reclaimed: int`, `device_used_percent: float | None`, `active_folders_skipped: list[str]`, `errors: list[str]`; method `to_json() -> dict`.
- Produces (`classify.py`): `top_folder(path: str) -> str`, `classify_tier(path: str) -> Tier | None`, `filename_timestamp(path: str) -> float | None`, `session_key(f: DeviceFile) -> float`, `active_folders(files: Iterable[DeviceFile], now: float, window_minutes: int) -> set[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_classify.py`:

```python
from seestar_sync.classify import (
    active_folders,
    classify_tier,
    filename_timestamp,
    session_key,
    top_folder,
)
from seestar_sync.models import DeviceFile, DiskUsage, Tier


def test_top_folder_handles_spaces():
    assert top_folder("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit") == "M 81_sub"
    assert top_folder("Sun/solar_video.avi") == "Sun"


def test_tier_allowlist():
    # Tier 1: FITs inside *_sub folders
    assert classify_tier("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit") == Tier.SUBS
    # Tier 2: videos anywhere
    assert classify_tier("Sun/Sun_20260101-102030.avi") == Tier.VIDEOS
    assert classify_tier("Lunar/Moon_20260101-102030.mp4") == Tier.VIDEOS
    # Tier 3: stacked FITs in target folders (both firmware naming generations)
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.fit") == Tier.STACKED_FITS
    assert classify_tier("M 81/Stacked_119_M 81_10.0s_20250607_221746.fit") == Tier.STACKED_FITS


def test_non_candidates_are_never_deletable():
    # JPGs and thumbnails: structurally unreachable
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.jpg") is None
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509_thn.jpg") is None
    # non-stacked FIT in a target folder (unknown future naming): fail safe
    assert classify_tier("M 81/Mystery_M 81_20260311_213509.fit") is None
    # unknown extensions
    assert classify_tier("M 81/notes.txt") is None
    # non-FIT junk inside a _sub folder
    assert classify_tier("M 81_sub/thumbs.db") is None


def test_filename_timestamp_parses_both_separators():
    # Light frames use YYYYMMDD-HHMMSS
    ts1 = filename_timestamp("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit")
    # Stacked files use YYYYMMDD_HHMMSS
    ts2 = filename_timestamp("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.fit")
    assert ts1 is not None and ts2 is not None
    assert ts2 > ts1  # 2026 > 2025
    assert filename_timestamp("M 81/no_date_here.fit") is None


def test_session_key_falls_back_to_mtime():
    dated = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit", 1, mtime=999.0)
    undated = DeviceFile("M 81_sub/oddball.fit", 1, mtime=123.0)
    assert session_key(dated) != 999.0  # parsed from name, not mtime
    assert session_key(undated) == 123.0


def test_active_folders_is_per_folder():
    now = 10_000.0
    files = [
        DeviceFile("M 81_sub/a.fit", 1, mtime=now - 60),        # active (1 min ago)
        DeviceFile("IC 1318_sub/b.fit", 1, mtime=now - 7200),   # idle (2 h ago)
    ]
    assert active_folders(files, now=now, window_minutes=15) == {"M 81_sub"}


def test_disk_usage_percent():
    u = DiskUsage(total_bytes=1000, free_bytes=250)
    assert u.used_percent == 75.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/models.py`**

```python
"""Core value types shared across the sync pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


@dataclass(frozen=True)
class DeviceFile:
    """A file on the Seestar, path relative to MyWorks/ with forward slashes."""

    path: str
    size: int
    mtime: float  # epoch seconds


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return 100.0 * (self.total_bytes - self.free_bytes) / self.total_bytes


class Tier(IntEnum):
    """Deletion allowlist, in deletion order. Anything not in a tier is undeletable."""

    SUBS = 1
    VIDEOS = 2
    STACKED_FITS = 3


@dataclass
class RunSummary:
    started_at: str  # ISO 8601 UTC
    result: str  # ok | partial | skipped_unreachable | error
    dry_run: bool = False
    files_copied: int = 0
    bytes_copied: int = 0
    files_deleted: int = 0
    bytes_reclaimed: int = 0
    device_used_percent: float | None = None
    active_folders_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "last_run": self.started_at,
            "result": self.result,
            "dry_run": self.dry_run,
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "files_deleted": self.files_deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "device_used_percent": self.device_used_percent,
            "active_folders_skipped": self.active_folders_skipped,
            "errors": self.errors,
        }
```

- [ ] **Step 4: Implement `src/seestar_sync/classify.py`**

```python
"""Filename policy: deletion tiers, session timestamps, active-folder detection.

Firmware naming drifts between generations (`Stacked_...` vs `DSO_Stacked_...`),
so everything here parses defensively and unknown shapes classify as None
(= not deletable). See spec §Reclaim policy engine.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .models import DeviceFile, Tier

_TIMESTAMP_RE = re.compile(r"(20\d{6})[-_](\d{6})")
_VIDEO_SUFFIXES = {".avi", ".mp4"}


def top_folder(path: str) -> str:
    return path.split("/", 1)[0]


def classify_tier(path: str) -> Tier | None:
    """Allowlist classifier. None means the deleter can never touch this file."""
    name = path.rsplit("/", 1)[-1]
    suffix = PurePosixPath(name).suffix.lower()
    if top_folder(path).endswith("_sub"):
        return Tier.SUBS if suffix == ".fit" else None
    if suffix in _VIDEO_SUFFIXES:
        return Tier.VIDEOS
    if suffix == ".fit" and "stacked" in name.lower():
        return Tier.STACKED_FITS
    return None


def filename_timestamp(path: str) -> float | None:
    """Epoch seconds parsed from the filename's embedded capture timestamp.

    Handles both `20250607-221746` (light frames) and `20260311_213509`
    (stacked files). Timestamps are device-local; they are only used for
    *ordering*, so timezone fidelity doesn't matter — UTC keeps them stable.
    """
    m = _TIMESTAMP_RE.search(path.rsplit("/", 1)[-1])
    if m is None:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def session_key(f: DeviceFile) -> float:
    """Sort key for oldest-session-first ordering."""
    ts = filename_timestamp(f.path)
    return ts if ts is not None else f.mtime


def active_folders(files: Iterable[DeviceFile], now: float, window_minutes: int) -> set[str]:
    """Top-level folders with any write inside the recent window (per-folder
    session guard: skipped for both copy and reclaim; all others stay eligible)."""
    cutoff = now - window_minutes * 60
    return {top_folder(f.path) for f in files if f.mtime >= cutoff}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/models.py src/seestar_sync/classify.py tests/test_classify.py
git commit -m "feat: core models and allowlist tier classification"
```

---

### Task 3: SQLite manifest

**Files:**
- Create: `src/seestar_sync/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces: `ManifestRow` frozen dataclass (`path`, `size`, `mtime`, `sha256`, `copied_at`, `deleted_from_device_at: str | None`, `deleted_tier: int | None`); class `Manifest` with `__init__(db_path: Path)`, `close()`, context-manager support, `record_verified(path, size, mtime, sha256, copied_at)`, `get(path) -> ManifestRow | None`, `verified_ok(path, size, mtime) -> bool`, `mark_deleted(path, tier, deleted_at)`, `bump_failure(path) -> int`, `clear_failure(path)`, `failure_count(path) -> int`; module constant `MTIME_TOLERANCE = 2.0`.

- [ ] **Step 1: Write the failing tests**

`tests/test_manifest.py`:

```python
from seestar_sync.manifest import Manifest


def make(tmp_path):
    return Manifest(tmp_path / "manifest.db")


def test_record_and_verify(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("M 81_sub/a.fit", 100, 1000.0, "abc123", "2026-07-09T00:00:00Z")
        assert m.verified_ok("M 81_sub/a.fit", 100, 1000.0)
        assert m.verified_ok("M 81_sub/a.fit", 100, 1001.5)  # within 2s tolerance
        assert not m.verified_ok("M 81_sub/a.fit", 100, 1005.0)  # mtime drifted
        assert not m.verified_ok("M 81_sub/a.fit", 999, 1000.0)  # size changed
        assert not m.verified_ok("unknown.fit", 1, 0.0)


def test_record_is_upsert(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "old", "2026-07-09T00:00:00Z")
        m.record_verified("a.fit", 200, 2000.0, "new", "2026-07-09T01:00:00Z")
        row = m.get("a.fit")
        assert row.size == 200 and row.sha256 == "new"


def test_mark_deleted_is_audited(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T00:00:00Z")
        m.mark_deleted("a.fit", tier=1, deleted_at="2026-07-09T02:00:00Z")
        row = m.get("a.fit")
        assert row.deleted_from_device_at == "2026-07-09T02:00:00Z"
        assert row.deleted_tier == 1
        # deletion audit does not un-verify the file
        assert m.verified_ok("a.fit", 100, 1000.0)


def test_failure_counter(tmp_path):
    with make(tmp_path) as m:
        assert m.failure_count("bad.fit") == 0
        assert m.bump_failure("bad.fit") == 1
        assert m.bump_failure("bad.fit") == 2
        m.clear_failure("bad.fit")
        assert m.failure_count("bad.fit") == 0


def test_persistence_across_reopen(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T00:00:00Z")
    with make(tmp_path) as m2:
        assert m2.verified_ok("a.fit", 100, 1000.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/manifest.py`**

```python
"""SQLite manifest: cache of what's verified on the NAS + audit trail of deletes.

The archive is authoritative; this DB is rebuildable from it. Deletes require a
verified row here, so a lost manifest fail-safes to "no reclaiming".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

MTIME_TOLERANCE = 2.0  # seconds; FAT-ish filesystems have coarse timestamps

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    copied_at TEXT NOT NULL,
    deleted_from_device_at TEXT,
    deleted_tier INTEGER
);
CREATE TABLE IF NOT EXISTS failures (
    path TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ManifestRow:
    path: str
    size: int
    mtime: float
    sha256: str
    copied_at: str
    deleted_from_device_at: str | None
    deleted_tier: int | None


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record_verified(
        self, path: str, size: int, mtime: float, sha256: str, copied_at: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO files (path, size, mtime, sha256, copied_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 size = excluded.size, mtime = excluded.mtime,
                 sha256 = excluded.sha256, copied_at = excluded.copied_at,
                 deleted_from_device_at = NULL, deleted_tier = NULL""",
            (path, size, mtime, sha256, copied_at),
        )
        self._conn.commit()

    def get(self, path: str) -> ManifestRow | None:
        cur = self._conn.execute(
            "SELECT path, size, mtime, sha256, copied_at, deleted_from_device_at,"
            " deleted_tier FROM files WHERE path = ?",
            (path,),
        )
        row = cur.fetchone()
        return ManifestRow(*row) if row else None

    def verified_ok(self, path: str, size: int, mtime: float) -> bool:
        """True iff this exact device file (path+size+mtime) has a verified NAS copy."""
        row = self.get(path)
        return (
            row is not None
            and row.size == size
            and abs(row.mtime - mtime) <= MTIME_TOLERANCE
        )

    def mark_deleted(self, path: str, tier: int, deleted_at: str) -> None:
        """Write-ahead audit: MUST be committed before the SMB delete is issued."""
        self._conn.execute(
            "UPDATE files SET deleted_from_device_at = ?, deleted_tier = ? WHERE path = ?",
            (deleted_at, tier, path),
        )
        self._conn.commit()

    def bump_failure(self, path: str) -> int:
        self._conn.execute(
            """INSERT INTO failures (path, count) VALUES (?, 1)
               ON CONFLICT(path) DO UPDATE SET count = count + 1""",
            (path,),
        )
        self._conn.commit()
        return self.failure_count(path)

    def clear_failure(self, path: str) -> None:
        self._conn.execute("DELETE FROM failures WHERE path = ?", (path,))
        self._conn.commit()

    def failure_count(self, path: str) -> int:
        cur = self._conn.execute("SELECT count FROM failures WHERE path = ?", (path,))
        row = cur.fetchone()
        return row[0] if row else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/manifest.py tests/test_manifest.py
git commit -m "feat: SQLite manifest with write-ahead delete audit and failure counts"
```

---

### Task 4: Archive writer (sentinel, st_dev guard, .part + verify + rename)

**Files:**
- Create: `src/seestar_sync/archive.py`
- Test: `tests/test_archive.py`

**Interfaces:**
- Produces: exceptions `ArchiveError`, `VerifyError(ArchiveError)`; class `Archive` with `__init__(root: Path, dev_of: Callable[[Path], int] | None = None)` (`dev_of` is an injectable device-id getter for tests; default `lambda p: p.stat().st_dev`), property `meta_dir: Path` (= `root / ".seestar-sync"`), `init(allow_root_fs: bool)`, `check_ready(allow_root_fs: bool)`, `dest_path(device_path: str) -> Path`, `write_stream(device_path: str, reader: BinaryIO, expected_size: int, mtime: float) -> str` (returns sha256 hex).
- Consumes: nothing from other tasks (pure stdlib).

- [ ] **Step 1: Write the failing tests**

`tests/test_archive.py`:

```python
import hashlib
import io
import os

import pytest

from seestar_sync.archive import Archive, ArchiveError, VerifyError


def make_archive(tmp_path, dev_of=None):
    root = tmp_path / "archive"
    root.mkdir()
    if dev_of is None:
        # root tree on device 2, "/" on device 1 -> foreign storage, checks pass
        dev_of = lambda p: 2 if str(p).startswith(str(root)) else 1
    return Archive(root, dev_of=dev_of)


def test_init_creates_sentinel_and_meta(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    assert (a.meta_dir / "ARCHIVE_MARKER").exists()
    a.check_ready(allow_root_fs=False)  # must not raise


def test_check_ready_without_sentinel_fails(tmp_path):
    a = make_archive(tmp_path)
    with pytest.raises(ArchiveError, match="ARCHIVE_MARKER"):
        a.check_ready(allow_root_fs=False)


def test_init_on_container_root_fs_fails(tmp_path):
    # same st_dev as "/" -> ephemeral-filesystem trap -> refuse
    a = make_archive(tmp_path, dev_of=lambda p: 1)
    with pytest.raises(ArchiveError, match="own filesystem"):
        a.init(allow_root_fs=False)
    # escape hatch for legitimately uncontainerized setups
    a.init(allow_root_fs=True)
    a.check_ready(allow_root_fs=True)


def test_write_stream_verifies_and_renames_atomically(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    data = b"starlight" * 1000
    sha = a.write_stream("M 81_sub/a.fit", io.BytesIO(data), len(data), mtime=1234.0)
    dest = a.dest_path("M 81_sub/a.fit")
    assert dest.read_bytes() == data
    assert sha == hashlib.sha256(data).hexdigest()
    assert abs(os.stat(dest).st_mtime - 1234.0) < 2.0  # device mtime preserved
    assert not list(dest.parent.glob("*.part"))  # no temp debris


def test_short_read_raises_verify_error_and_leaves_no_part(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    data = b"torn"
    with pytest.raises(VerifyError):
        a.write_stream("M 81_sub/torn.fit", io.BytesIO(data), expected_size=999, mtime=0.0)
    assert not a.dest_path("M 81_sub/torn.fit").exists()
    assert not list((a.root / "MyWorks" / "M 81_sub").glob("*.part"))


def test_dest_path_rejects_traversal(tmp_path):
    # device filenames are device-controlled input: contain them under MyWorks
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    for evil in ("../escape.fit", "a/../../escape.fit", "/etc/passwd", "a\\..\\b.fit"):
        with pytest.raises(ArchiveError, match="unsafe device path"):
            a.dest_path(evil)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/archive.py`**

```python
"""Archive writer: mirror of the device under <root>/MyWorks/.

Safety properties (spec §Error handling):
- Foreign-storage guard: refuses to run when the archive dir shares a device
  id with "/" (that means no volume is mounted and copies would land on the
  container's ephemeral layer). Escape hatch: allow_root_fs.
- Sentinel: init() writes ARCHIVE_MARKER; every later start requires it, so a
  dropped SMB mount (empty dir) can never masquerade as the archive.
- Torn-write immunity: copies land as .part, are re-read and verified
  (size + sha256), then atomically renamed. The archive never holds a torn file.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """Archive volume unusable. CLI maps this to exit code 2."""


class VerifyError(ArchiveError):
    """Landed bytes don't match the device read; the copy was discarded."""


class Archive:
    MARKER = "ARCHIVE_MARKER"

    def __init__(self, root: Path, dev_of: Callable[[Path], int] | None = None):
        self.root = root
        self._dev_of = dev_of or (lambda p: p.stat().st_dev)

    @property
    def meta_dir(self) -> Path:
        return self.root / ".seestar-sync"

    def _check_foreign_storage(self, allow_root_fs: bool) -> None:
        if allow_root_fs:
            return
        if not self.root.exists():
            raise ArchiveError(f"archive dir {self.root} does not exist — is the volume mounted?")
        if self._dev_of(self.root) == self._dev_of(Path("/")):
            raise ArchiveError(
                f"archive dir {self.root} is on the container's own filesystem "
                "(no volume mounted?). Copies would die with the container. "
                "Mount a volume, or set ARCHIVE_ON_ROOT_FS_OK=true if this "
                "host path is intentionally on the root filesystem."
            )

    def init(self, allow_root_fs: bool) -> None:
        self._check_foreign_storage(allow_root_fs)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        (self.meta_dir / "history").mkdir(exist_ok=True)
        (self.root / "MyWorks").mkdir(exist_ok=True)
        (self.meta_dir / self.MARKER).touch()

    def check_ready(self, allow_root_fs: bool) -> None:
        self._check_foreign_storage(allow_root_fs)
        if not (self.meta_dir / self.MARKER).exists():
            raise ArchiveError(
                f"{self.meta_dir / self.MARKER} missing — archive not initialized "
                "(run with --init) or the volume mount dropped. Refusing to touch anything."
            )

    def dest_path(self, device_path: str) -> Path:
        """Map a device-relative path into the archive mirror.

        Device filenames are untrusted input (they come off the telescope):
        reject anything that could escape <root>/MyWorks.
        """
        base = self.root / "MyWorks"
        pure = Path(device_path)
        if (
            pure.is_absolute()
            or "\\" in device_path
            or ".." in pure.parts
            or not (base / pure).resolve().is_relative_to(base.resolve())
        ):
            raise ArchiveError(f"unsafe device path rejected: {device_path!r}")
        return base / pure

    def write_stream(
        self, device_path: str, reader: BinaryIO, expected_size: int, mtime: float
    ) -> str:
        """Copy reader -> .part, verify size + sha256 by re-reading, atomic rename.

        Returns the sha256 hex digest. Raises VerifyError (and removes the
        .part) on any mismatch, so a torn or corrupted transfer is invisible
        to the archive.
        """
        dest = self.dest_path(device_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        device_hash = hashlib.sha256()
        written = 0
        try:
            with open(part, "wb") as out:
                while chunk := reader.read(_CHUNK):
                    device_hash.update(chunk)
                    out.write(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())

            if written != expected_size:
                raise VerifyError(
                    f"{device_path}: read {written} bytes, expected {expected_size}"
                )

            landed_hash = hashlib.sha256()
            with open(part, "rb") as check:
                while chunk := check.read(_CHUNK):
                    landed_hash.update(chunk)
            if landed_hash.hexdigest() != device_hash.hexdigest():
                raise VerifyError(f"{device_path}: landed bytes differ from device read")

            os.utime(part, (mtime, mtime))  # preserve device mtime for rebuild-manifest
            os.replace(part, dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return device_hash.hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_archive.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/archive.py tests/test_archive.py
git commit -m "feat: archive writer with sentinel, foreign-storage guard, verified atomic writes"
```

---

### Task 5: DeviceClient protocol + FakeDevice

**Files:**
- Create: `src/seestar_sync/device.py`, `tests/fakes.py`
- Test: `tests/test_fakes.py`

**Interfaces:**
- Produces (`device.py`): exception `DeviceUnavailable(Exception)`; `DeviceClient(Protocol)` with methods `list_files() -> list[DeviceFile]`, `stat(path: str) -> DeviceFile` (raises `FileNotFoundError` for missing paths, `DeviceUnavailable` for connectivity), `open_read(path: str) -> BinaryIO`, `delete_file(path: str) -> None`, `remove_dir_if_empty(folder: str) -> bool`, `disk_usage() -> DiskUsage`.
- Produces (`tests/fakes.py`): `FakeDevice` implementing the protocol over an in-memory dict, with `put(path, data, mtime)`, attribute `capacity: int`, `fail_after_reads: int | None` (raise `DeviceUnavailable` on the Nth `open_read`), and `short_read_paths: set[str]` (deliver truncated data to trigger VerifyError downstream).

- [ ] **Step 1: Write the failing tests**

`tests/test_fakes.py`:

```python
import pytest

from seestar_sync.device import DeviceUnavailable
from tests.fakes import FakeDevice


def test_fake_lists_stats_and_reads():
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/a.fit", b"12345", mtime=10.0)
    files = d.list_files()
    assert len(files) == 1 and files[0].size == 5
    assert d.stat("M 81_sub/a.fit").mtime == 10.0
    assert d.open_read("M 81_sub/a.fit").read() == b"12345"
    with pytest.raises(FileNotFoundError):
        d.stat("nope.fit")


def test_fake_disk_usage_tracks_deletes():
    d = FakeDevice(capacity=100)
    d.put("a/x.fit", b"x" * 60, mtime=0.0)
    assert d.disk_usage().used_percent == 60.0
    d.delete_file("a/x.fit")
    assert d.disk_usage().used_percent == 0.0
    assert d.remove_dir_if_empty("a") is True


def test_fake_can_simulate_mid_run_disconnect():
    d = FakeDevice(capacity=100)
    d.put("a/x.fit", b"x", mtime=0.0)
    d.put("a/y.fit", b"y", mtime=0.0)
    d.fail_after_reads = 1
    d.open_read("a/x.fit")  # first read fine
    with pytest.raises(DeviceUnavailable):
        d.open_read("a/y.fit")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fakes.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/device.py`**

```python
"""DeviceClient: the seam between policy code and the physical Seestar.

Production impl: smb_client.SmbDeviceClient. Test impl: tests/fakes.FakeDevice.
Policy code never learns SMB exists; this is also the migration point if ZWO
ever changes protocols.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol

from .models import DeviceFile, DiskUsage


class DeviceUnavailable(Exception):
    """The Seestar is unreachable or vanished mid-operation (it's a telescope —
    powering off is routine, not an error)."""


class DeviceClient(Protocol):
    def list_files(self) -> list[DeviceFile]:
        """Recursive listing of MyWorks/. Raises DeviceUnavailable."""
        ...

    def stat(self, path: str) -> DeviceFile:
        """Fresh size/mtime. Raises FileNotFoundError or DeviceUnavailable."""
        ...

    def open_read(self, path: str) -> BinaryIO:
        """Streaming read. Raises DeviceUnavailable."""
        ...

    def delete_file(self, path: str) -> None:
        """Raises DeviceUnavailable."""
        ...

    def remove_dir_if_empty(self, folder: str) -> bool:
        """Remove a now-empty top-level folder; False if non-empty/missing."""
        ...

    def disk_usage(self) -> DiskUsage:
        """Raises DeviceUnavailable."""
        ...
```

- [ ] **Step 4: Implement `tests/fakes.py`**

```python
"""In-memory DeviceClient for the integration test ring."""

from __future__ import annotations

import io

from seestar_sync.device import DeviceUnavailable
from seestar_sync.models import DeviceFile, DiskUsage


class FakeDevice:
    def __init__(self, capacity: int = 1_000_000):
        self.capacity = capacity
        self._files: dict[str, tuple[bytes, float]] = {}
        self.fail_after_reads: int | None = None
        self.short_read_paths: set[str] = set()
        self._reads = 0

    def put(self, path: str, data: bytes = b"x" * 10, mtime: float = 0.0) -> None:
        self._files[path] = (data, mtime)

    def list_files(self) -> list[DeviceFile]:
        return [
            DeviceFile(path, len(data), mtime)
            for path, (data, mtime) in sorted(self._files.items())
        ]

    def stat(self, path: str) -> DeviceFile:
        if path not in self._files:
            raise FileNotFoundError(path)
        data, mtime = self._files[path]
        return DeviceFile(path, len(data), mtime)

    def open_read(self, path: str) -> io.BytesIO:
        if self.fail_after_reads is not None and self._reads >= self.fail_after_reads:
            raise DeviceUnavailable("simulated mid-run power-off")
        self._reads += 1
        data, _ = self._files[path]
        if path in self.short_read_paths:
            data = data[: max(0, len(data) - 1)]  # torn transfer
        return io.BytesIO(data)

    def delete_file(self, path: str) -> None:
        self._files.pop(path, None)

    def remove_dir_if_empty(self, folder: str) -> bool:
        prefix = folder + "/"
        return not any(p.startswith(prefix) for p in self._files)

    def disk_usage(self) -> DiskUsage:
        used = sum(len(data) for data, _ in self._files.values())
        return DiskUsage(total_bytes=self.capacity, free_bytes=self.capacity - used)
```

Also create an empty `tests/__init__.py` so `from tests.fakes import FakeDevice` resolves:

```bash
touch tests/__init__.py
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_fakes.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/device.py tests/fakes.py tests/__init__.py tests/test_fakes.py
git commit -m "feat: DeviceClient protocol and in-memory FakeDevice"
```

---

### Task 6: Copy planner

**Files:**
- Create: `src/seestar_sync/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Produces: `plan_copies(files: Iterable[DeviceFile], is_synced: Callable[[DeviceFile], bool], active: set[str]) -> list[DeviceFile]` — everything not yet synced, excluding active folders, ordered oldest-session-first.
- Consumes: `classify.top_folder`, `classify.session_key`, `models.DeviceFile`.

- [ ] **Step 1: Write the failing tests**

`tests/test_planner.py`:

```python
from seestar_sync.models import DeviceFile
from seestar_sync.planner import plan_copies


def test_plan_skips_synced_and_active_and_orders_oldest_first():
    old = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit", 10, 100.0)
    new = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20260101-000000.fit", 10, 200.0)
    synced = DeviceFile("IC 1318/DSO_Stacked_10_IC 1318_10.0s_20250101_000000.fit", 10, 50.0)
    live = DeviceFile("Sun/shot.avi", 10, 999.0)

    plan = plan_copies(
        [new, synced, old, live],
        is_synced=lambda f: f is synced,
        active={"Sun"},
    )
    assert plan == [old, new]  # synced skipped, active folder skipped, oldest first


def test_plan_copies_everything_including_jpgs():
    # copy scope is EVERYTHING; only deletion is tiered
    jpg = DeviceFile("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509_thn.jpg", 5, 1.0)
    assert plan_copies([jpg], is_synced=lambda f: False, active=set()) == [jpg]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/planner.py`**

```python
"""Copy planning: pure diff between device listing and what's already verified."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .classify import session_key, top_folder
from .models import DeviceFile


def plan_copies(
    files: Iterable[DeviceFile],
    is_synced: Callable[[DeviceFile], bool],
    active: set[str],
) -> list[DeviceFile]:
    """Everything on-device that lacks a verified NAS copy, oldest session
    first (so an interrupted run banks the oldest data), skipping folders the
    session guard marked active."""
    todo = [
        f for f in files if top_folder(f.path) not in active and not is_synced(f)
    ]
    return sorted(todo, key=session_key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/planner.py tests/test_planner.py
git commit -m "feat: pure copy planner (diff, session guard, oldest-first)"
```

---

### Task 7: Reclaim policy engine

**Files:**
- Create: `src/seestar_sync/reclaim.py`
- Test: `tests/test_reclaim.py`

**Interfaces:**
- Produces: `Deletion` frozen dataclass (`file: DeviceFile`, `tier: Tier`); `plan_reclaim(files: Iterable[DeviceFile], usage: DiskUsage, trigger_percent: float, target_percent: float, is_verified: Callable[[DeviceFile], bool], active: set[str]) -> list[Deletion]`.
- Consumes: `classify.classify_tier`, `classify.session_key`, `classify.top_folder`, `models.DeviceFile`, `models.DiskUsage`, `models.Tier`.

- [ ] **Step 1: Write the failing tests**

`tests/test_reclaim.py`:

```python
from seestar_sync.models import DeviceFile, DiskUsage, Tier
from seestar_sync.reclaim import plan_reclaim


def sub(name, size, mtime=0.0):
    return DeviceFile(f"M 81_sub/{name}", size, mtime)


def test_no_reclaim_below_trigger():
    usage = DiskUsage(total_bytes=100, free_bytes=50)  # 50% used
    plan = plan_reclaim(
        [sub("Light_a_20250101-000000.fit", 10)], usage, 75, 60,
        is_verified=lambda f: True, active=set(),
    )
    assert plan == []


def test_reclaim_frees_down_to_target_with_hysteresis():
    # 90% used of 1000 => 900 used; target 60% => free 300 bytes worth
    usage = DiskUsage(total_bytes=1000, free_bytes=100)
    files = [sub(f"Light_x_2025010{i}-000000.fit", 100, mtime=i) for i in range(1, 8)]
    plan = plan_reclaim(files, usage, 75, 60, is_verified=lambda f: True, active=set())
    assert len(plan) == 3  # 300 bytes freed, then stop — no over-deleting
    # oldest sessions first
    assert [d.file.path for d in plan] == [f.path for f in files[:3]]


def test_tier_order_subs_then_videos_then_stacked():
    usage = DiskUsage(total_bytes=100, free_bytes=1)  # 99% used, target 60 => free 39
    stacked = DeviceFile("M 81/DSO_Stacked_1_M 81_10.0s_20200101_000000.fit", 13, 1.0)
    video = DeviceFile("Sun/clip_20200101-000000.avi", 13, 1.0)
    subframe = sub("Light_a_20260101-000000.fit", 13, 999.0)  # newest, but tier 1
    plan = plan_reclaim(
        [stacked, video, subframe], usage, 75, 60,
        is_verified=lambda f: True, active=set(),
    )
    assert [d.tier for d in plan] == [Tier.SUBS, Tier.VIDEOS, Tier.STACKED_FITS]


def test_unverified_active_and_non_candidates_are_untouchable():
    usage = DiskUsage(total_bytes=100, free_bytes=0)
    jpg = DeviceFile("M 81/DSO_Stacked_1_M 81_10.0s_20200101_000000.jpg", 50, 1.0)
    unverified = sub("Light_unverified_20200101-000000.fit", 50, 1.0)
    in_active = DeviceFile("IC 1318_sub/Light_b_20200101-000000.fit", 50, 1.0)
    plan = plan_reclaim(
        [jpg, unverified, in_active], usage, 75, 60,
        is_verified=lambda f: f is not unverified, active={"IC 1318_sub"},
    )
    assert plan == []  # nothing eligible even at 100% full
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reclaim.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/reclaim.py`**

```python
"""Reclaim policy: free-space-driven, tiered, allowlist-only deletion planning.

Pure function — the orchestrator re-checks preconditions at delete time.
Candidates come ONLY from classify_tier's allowlist, so JPGs, thumbnails and
unknown file types are structurally unreachable (spec: a firmware rename must
fail safe, deleting nothing)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .classify import classify_tier, session_key, top_folder
from .models import DeviceFile, DiskUsage, Tier


@dataclass(frozen=True)
class Deletion:
    file: DeviceFile
    tier: Tier


def plan_reclaim(
    files: Iterable[DeviceFile],
    usage: DiskUsage,
    trigger_percent: float,
    target_percent: float,
    is_verified: Callable[[DeviceFile], bool],
    active: set[str],
) -> list[Deletion]:
    """Hysteresis loop: nothing until used% > trigger, then delete (tier, then
    oldest session) until projected used% < target."""
    if usage.used_percent <= trigger_percent:
        return []

    used_bytes = usage.total_bytes - usage.free_bytes
    bytes_to_free = used_bytes - usage.total_bytes * target_percent / 100.0

    candidates: list[Deletion] = []
    for f in files:
        tier = classify_tier(f.path)
        if tier is None:
            continue  # allowlist: not a candidate, ever
        if top_folder(f.path) in active:
            continue  # per-folder session guard
        if not is_verified(f):
            continue  # never delete without a verified NAS copy
        candidates.append(Deletion(f, tier))

    candidates.sort(key=lambda d: (d.tier, session_key(d.file)))

    plan: list[Deletion] = []
    freed = 0
    for d in candidates:
        if freed >= bytes_to_free:
            break
        plan.append(d)
        freed += d.file.size
    return plan
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reclaim.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/reclaim.py tests/test_reclaim.py
git commit -m "feat: tiered free-space reclaim planner with hysteresis"
```

---

### Task 8: Reporters

**Files:**
- Create: `src/seestar_sync/reporters.py`
- Test: `tests/test_reporters.py`

**Interfaces:**
- Produces: `write_status(meta_dir: Path, summary: RunSummary)`, `append_history(meta_dir: Path, summary: RunSummary)`, `ping_kuma(url: str, summary: RunSummary)`, `post_ha_webhook(url: str, summary: RunSummary)`, `report_all(config: Config, meta_dir: Path, summary: RunSummary)`. **None of these ever raise.**
- Consumes: `models.RunSummary`, `config.Config`.

- [ ] **Step 1: Write the failing tests**

`tests/test_reporters.py`:

```python
import json
from pathlib import Path

from seestar_sync.config import Config
from seestar_sync.models import RunSummary
from seestar_sync.reporters import append_history, report_all, write_status


def summary(result="ok"):
    return RunSummary(started_at="2026-07-09T09:15:03Z", result=result, files_copied=2)


def test_write_status_snapshot(tmp_path):
    write_status(tmp_path, summary())
    data = json.loads((tmp_path / "status.json").read_text())
    assert data["result"] == "ok"
    assert data["last_run"] == "2026-07-09T09:15:03Z"


def test_append_history_monthly_jsonl(tmp_path):
    append_history(tmp_path, summary())
    append_history(tmp_path, summary(result="partial"))
    lines = (tmp_path / "history" / "2026-07.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["result"] == "partial"


def test_reporters_never_raise(tmp_path, monkeypatch):
    # unwritable meta dir + unreachable endpoints: run must not blow up
    cfg = Config.from_env(
        {
            "SEESTAR_HOST": "h",
            "KUMA_PUSH_URL": "http://127.0.0.1:1/push/x",  # nothing listens
            "HA_WEBHOOK_URL": "http://127.0.0.1:1/api/webhook/x",
        }
    )
    report_all(cfg, Path("/nonexistent/definitely/not/writable"), summary())
    report_all(cfg, tmp_path, summary())  # endpoints down, files still written
    assert (tmp_path / "status.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reporters.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/reporters.py`**

```python
"""Observability sinks. Strictly fire-and-forget: a reporter failure is a log
warning, never a run failure (spec: only the archive volume is load-bearing)."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config
from .models import RunSummary

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 5.0
# Kuma alerts on silence; partial/error runs deliberately don't ping.
_KUMA_OK_RESULTS = {"ok", "skipped_unreachable"}


def _guard(name: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 — reporters must never raise
        log.warning("reporter %s failed (non-fatal): %s", name, e)


def write_status(meta_dir: Path, summary: RunSummary) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    tmp = meta_dir / "status.json.tmp"
    tmp.write_text(json.dumps(summary.to_json(), indent=2))
    tmp.replace(meta_dir / "status.json")


def append_history(meta_dir: Path, summary: RunSummary) -> None:
    history = meta_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    month = summary.started_at[:7]  # "2026-07" from ISO timestamp
    with open(history / f"{month}.jsonl", "a") as f:
        f.write(json.dumps(summary.to_json()) + "\n")


def ping_kuma(url: str, summary: RunSummary) -> None:
    if summary.result not in _KUMA_OK_RESULTS:
        return
    msg = urllib.parse.quote(f"{summary.result}: {summary.files_copied} copied")
    sep = "&" if "?" in url else "?"
    with urllib.request.urlopen(
        f"{url}{sep}status=up&msg={msg}", timeout=_HTTP_TIMEOUT
    ):
        pass


def post_ha_webhook(url: str, summary: RunSummary) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(summary.to_json()).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
        pass


def report_all(config: Config, meta_dir: Path, summary: RunSummary) -> None:
    _guard("status", write_status, meta_dir, summary)
    _guard("history", append_history, meta_dir, summary)
    if config.kuma_push_url:
        _guard("kuma", ping_kuma, config.kuma_push_url, summary)
    if config.ha_webhook_url:
        _guard("ha", post_ha_webhook, config.ha_webhook_url, summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reporters.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/reporters.py tests/test_reporters.py
git commit -m "feat: fire-and-forget reporters (status, JSONL history, Kuma, HA)"
```

---

### Task 9: Sync orchestrator (integration ring)

**Files:**
- Create: `src/seestar_sync/syncrun.py`
- Test: `tests/test_syncrun.py`

**Interfaces:**
- Produces: `run_sync(device: DeviceClient, archive: Archive, manifest: Manifest, config: Config, now: float | None = None) -> RunSummary`; helper `iso(ts: float) -> str`; module constant `MAX_VERIFY_FAILURES = 3`.
- Consumes: everything from Tasks 2–8 with the exact signatures defined there.

- [ ] **Step 1: Write the failing tests**

`tests/test_syncrun.py`:

```python
import pytest

from seestar_sync.archive import Archive
from seestar_sync.config import Config
from seestar_sync.manifest import Manifest
from seestar_sync.syncrun import run_sync
from tests.fakes import FakeDevice


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    archive = Archive(root, dev_of=lambda p: 2 if str(p).startswith(str(root)) else 1)
    archive.init(allow_root_fs=False)
    manifest = Manifest(archive.meta_dir / "manifest.db")
    yield archive, manifest
    manifest.close()


def config(**overrides):
    env_vars = {"SEESTAR_HOST": "h"}
    env_vars.update(overrides)
    return Config.from_env(env_vars)


def test_unreachable_device_is_a_skip_not_an_error(env):
    archive, manifest = env

    class DeadDevice(FakeDevice):
        def list_files(self):
            from seestar_sync.device import DeviceUnavailable

            raise DeviceUnavailable("off")

    s = run_sync(DeadDevice(), archive, manifest, config())
    assert s.result == "skipped_unreachable"
    assert s.files_copied == 0


def test_copies_everything_and_verifies(env):
    archive, manifest = env
    d = FakeDevice(capacity=10_000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"raw" * 100, mtime=1.0)
    d.put("M 81/DSO_Stacked_1_M 81_10.0s_20250101_000000.jpg", b"jpg", mtime=1.0)

    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.result == "ok"
    assert s.files_copied == 2
    assert archive.dest_path("M 81/DSO_Stacked_1_M 81_10.0s_20250101_000000.jpg").exists()
    # second run: nothing new to do
    s2 = run_sync(d, archive, manifest, config(), now=1_000_100.0)
    assert s2.files_copied == 0


def test_reclaim_deletes_only_verified_with_write_ahead_audit(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)  # 80% > trigger 75
    s1 = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s1.files_copied == 1
    assert s1.files_deleted == 1  # verified this run, then reclaimed under pressure
    assert d.list_files() == []  # gone from device
    row = manifest.get("M 81_sub/Light_a_20250101-000000.fit")
    assert row.deleted_from_device_at is not None and row.deleted_tier == 1
    # NAS copy retained
    assert archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()


def test_active_folder_is_left_alone(env):
    archive, manifest = env
    now = 1_000_000.0
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 900, mtime=now - 60)  # active!
    s = run_sync(d, archive, manifest, config(), now=now)
    assert s.files_copied == 0 and s.files_deleted == 0
    assert s.active_folders_skipped == ["M 81_sub"]


def test_mid_run_disconnect_keeps_partial_progress(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 10, mtime=1.0)
    d.fail_after_reads = 1
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.files_copied == 1  # first banked before the "power-off"
    assert s.result == "ok"  # clean disconnect, no errors
    assert s.files_deleted == 0  # reclaim skipped once device vanished


def test_verify_failure_counts_and_eventually_skips(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    path = "A_sub/Light_a_20250101-000000.fit"
    d.put(path, b"a" * 10, mtime=1.0)
    d.short_read_paths.add(path)  # every read is torn
    for i in range(3):
        s = run_sync(d, archive, manifest, config(), now=1_000_000.0 + i)
        assert s.result == "partial"
        assert any("verify" in e for e in s.errors)
    # 4th run: flagged and skipped, not retried forever
    s4 = run_sync(d, archive, manifest, config(), now=1_000_010.0)
    assert any("skipping" in e for e in s4.errors)
    assert manifest.failure_count(path) == 3


def test_dry_run_touches_nothing(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    s = run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_000.0)
    assert s.dry_run is True
    assert s.files_copied == 1  # planned counts, for visibility
    assert not archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()
    assert len(d.list_files()) == 1  # nothing deleted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncrun.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/syncrun.py`**

```python
"""One sync pass: probe -> inventory -> copy -> verify -> reclaim -> summary.

Ordering guarantees (spec §Reclaim, §Error handling):
- A file is only deleted if manifest-verified; preconditions re-checked at
  delete time against a fresh stat.
- The manifest audit row commits BEFORE the SMB delete (write-ahead): any
  interruption yields a false "deleted" (self-healing), never an unrecorded
  true deletion.
- DeviceUnavailable mid-run aborts cleanly, keeping banked progress.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .archive import Archive, VerifyError
from .classify import active_folders, top_folder
from .config import Config
from .device import DeviceClient, DeviceUnavailable
from .manifest import Manifest
from .models import RunSummary, Tier
from .planner import plan_copies
from .reclaim import plan_reclaim

log = logging.getLogger(__name__)

MAX_VERIFY_FAILURES = 3


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync(
    device: DeviceClient,
    archive: Archive,
    manifest: Manifest,
    config: Config,
    now: float | None = None,
) -> RunSummary:
    now = time.time() if now is None else now
    summary = RunSummary(started_at=iso(now), result="ok", dry_run=config.dry_run)

    try:
        files = device.list_files()
    except DeviceUnavailable as e:
        log.info("device unreachable, skipping run: %s", e)
        summary.result = "skipped_unreachable"
        return summary

    active = active_folders(files, now=now, window_minutes=config.active_window_minutes)
    summary.active_folders_skipped = sorted(active)
    if active:
        log.info("session guard: skipping active folders %s", sorted(active))

    device_gone = _copy_phase(device, archive, manifest, config, files, active, summary, now)

    if not device_gone:
        _reclaim_phase(device, manifest, config, files, active, summary, now)

    if summary.errors:
        summary.result = "partial"
    return summary


def _copy_phase(device, archive, manifest, config, files, active, summary, now) -> bool:
    """Returns True if the device vanished mid-phase."""
    is_synced = lambda f: manifest.verified_ok(f.path, f.size, f.mtime)  # noqa: E731
    for f in plan_copies(files, is_synced, active):
        if manifest.failure_count(f.path) >= MAX_VERIFY_FAILURES:
            summary.errors.append(
                f"{f.path}: {MAX_VERIFY_FAILURES} consecutive verify failures, skipping"
            )
            continue
        if config.dry_run:
            log.info("DRY_RUN would copy %s (%d bytes)", f.path, f.size)
            summary.files_copied += 1
            summary.bytes_copied += f.size
            continue
        try:
            with device.open_read(f.path) as reader:
                sha = archive.write_stream(f.path, reader, f.size, f.mtime)
        except FileNotFoundError:
            log.debug("%s vanished before copy (app-side removal); normal drift", f.path)
            continue
        except DeviceUnavailable as e:
            log.info("device vanished mid-copy (%s), keeping progress", e)
            return True
        except VerifyError as e:
            count = manifest.bump_failure(f.path)
            summary.errors.append(f"verify failed ({count}/{MAX_VERIFY_FAILURES}): {e}")
            continue
        manifest.record_verified(f.path, f.size, f.mtime, sha, iso(now))
        manifest.clear_failure(f.path)
        summary.files_copied += 1
        summary.bytes_copied += f.size
        log.info("copied %s (%d bytes)", f.path, f.size)
    return False


def _reclaim_phase(device, manifest, config, files, active, summary, now) -> None:
    try:
        usage = device.disk_usage()
        summary.device_used_percent = round(usage.used_percent, 1)
        plan = plan_reclaim(
            files,
            usage,
            config.reclaim_trigger_percent,
            config.reclaim_target_percent,
            is_verified=lambda f: manifest.verified_ok(f.path, f.size, f.mtime),
            active=active,
        )
        emptied_sub_folders: set[str] = set()
        for d in plan:
            if config.dry_run:
                log.info("DRY_RUN would delete [tier %d] %s (%d bytes)",
                         d.tier, d.file.path, d.file.size)
                summary.files_deleted += 1
                summary.bytes_reclaimed += d.file.size
                continue
            # Preconditions re-checked at delete time, not plan time:
            try:
                fresh = device.stat(d.file.path)
            except FileNotFoundError:
                continue  # e.g. phone app removed it; normal drift
            if not manifest.verified_ok(fresh.path, fresh.size, fresh.mtime):
                log.warning("skipping delete of %s: changed since verification", d.file.path)
                continue
            manifest.mark_deleted(d.file.path, int(d.tier), iso(now))  # write-ahead
            device.delete_file(d.file.path)
            summary.files_deleted += 1
            summary.bytes_reclaimed += d.file.size
            log.info("deleted [tier %d] %s (%d bytes)", d.tier, d.file.path, d.file.size)
            if d.tier == Tier.SUBS:
                emptied_sub_folders.add(top_folder(d.file.path))
        for folder in sorted(emptied_sub_folders):
            if device.remove_dir_if_empty(folder):
                log.info("removed empty folder %s", folder)
    except DeviceUnavailable as e:
        log.info("device vanished during reclaim (%s), stopping cleanly", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncrun.py -v`
Expected: 7 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests from Tasks 1–9 pass

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/syncrun.py tests/test_syncrun.py
git commit -m "feat: sync orchestrator with verify-before-delete and clean disconnect handling"
```

---

### Task 10: SMB device client

**Files:**
- Create: `src/seestar_sync/smb_client.py`
- Test: `tests/test_smb_client.py` (unit-testable parts only; live behavior is Task 13)

**Interfaces:**
- Produces: `SmbDeviceClient(host: str, capacity_gb: float | None = None, timeout: float = 30.0)` implementing `DeviceClient`; module constants `SHARE = "EMMC Images"`, `ROOT = "MyWorks"`.
- Consumes: `device.DeviceUnavailable`, `models.DeviceFile`, `models.DiskUsage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_smb_client.py`:

```python
import pytest

from seestar_sync.device import DeviceUnavailable
from seestar_sync.smb_client import SmbDeviceClient


def client():
    return SmbDeviceClient.__new__(SmbDeviceClient)  # skip __init__ (no network)


def test_unc_path_building():
    c = client()
    c._host = "seestar.local"
    assert c._unc() == r"\\seestar.local\EMMC Images\MyWorks"
    assert (
        c._unc("M 81_sub/Light_a.fit")
        == r"\\seestar.local\EMMC Images\MyWorks\M 81_sub\Light_a.fit"
    )


def test_rel_path_from_unc():
    c = client()
    c._host = "seestar.local"
    unc = r"\\seestar.local\EMMC Images\MyWorks\M 81_sub\Light_a.fit"
    assert c._rel(unc) == "M 81_sub/Light_a.fit"


def test_unc_rejects_traversal():
    # the delete side of path containment: never build a UNC that escapes MyWorks
    c = client()
    c._host = "h"
    for evil in ("../x.fit", "a/../x.fit", "a\\x.fit", "/abs.fit", "a//b.fit"):
        with pytest.raises(ValueError, match="unsafe device path"):
            c._unc(evil)


def test_error_mapping_wraps_connection_failures():
    c = client()
    c._host = "seestar.local"
    with pytest.raises(DeviceUnavailable):
        with c._map_errors():
            raise ConnectionResetError("gone")
    # FileNotFoundError must pass through untouched (stat contract)
    with pytest.raises(FileNotFoundError):
        with c._map_errors():
            raise FileNotFoundError("missing")


def test_disk_usage_fallback_uses_capacity(monkeypatch):
    c = client()
    c._host = "h"
    c._capacity_bytes = 1000
    monkeypatch.setattr(c, "_stat_volume", lambda: (_ for _ in ()).throw(OSError("no")))
    from seestar_sync.models import DeviceFile

    monkeypatch.setattr(c, "list_files", lambda: [DeviceFile("a/b.fit", 600, 0.0)])
    usage = c.disk_usage()
    assert usage.total_bytes == 1000 and usage.free_bytes == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smb_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/smb_client.py`**

```python
"""Production DeviceClient over SMB (smbprotocol's high-level smbclient API).

Protocol facts (verified via ZWO docs + seestarpy source; see spec §Device facts):
share "EMMC Images", guest auth with empty username/password, direct TCP 445.
The RPC port (4700) is auth-locked on fw >= 7.18 and deliberately unused here.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import BinaryIO

import smbclient
from smbprotocol.exceptions import SMBException

from .device import DeviceUnavailable
from .models import DeviceFile, DiskUsage

log = logging.getLogger(__name__)

SHARE = "EMMC Images"
ROOT = "MyWorks"


class SmbDeviceClient:
    def __init__(self, host: str, capacity_gb: float | None = None, timeout: float = 30.0):
        self._host = host
        self._capacity_bytes = (
            int(capacity_gb * 1024**3) if capacity_gb is not None else None
        )
        with self._map_errors():
            smbclient.register_session(
                host,
                username="guest",
                password="",
                connection_timeout=timeout,
                auth_protocol="ntlm",
            )

    def _unc(self, rel: str = "") -> str:
        base = rf"\\{self._host}\{SHARE}\{ROOT}"
        if not rel:
            return base
        # rel is device-controlled: refuse anything that could address outside
        # MyWorks (deletion is the high-blast-radius path through here).
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or ".." in parts or "" in parts:
            raise ValueError(f"unsafe device path rejected: {rel!r}")
        return base + "\\" + rel.replace("/", "\\")

    def _rel(self, unc: str) -> str:
        prefix = self._unc() + "\\"
        return unc.removeprefix(prefix).replace("\\", "/")

    @contextmanager
    def _map_errors(self):
        """Connectivity problems -> DeviceUnavailable; missing files pass through."""
        try:
            yield
        except FileNotFoundError:
            raise
        except (SMBException, ConnectionError, TimeoutError, OSError) as e:
            raise DeviceUnavailable(f"{self._host}: {e}") from e

    def list_files(self) -> list[DeviceFile]:
        out: list[DeviceFile] = []
        with self._map_errors():
            self._scan(self._unc(), "", out)
        return out

    def _scan(self, unc: str, rel_prefix: str, out: list[DeviceFile]) -> None:
        for entry in smbclient.scandir(unc):
            rel = f"{rel_prefix}{entry.name}"
            if entry.is_dir():
                self._scan(f"{unc}\\{entry.name}", f"{rel}/", out)
            else:
                st = entry.stat()  # cached from the directory listing
                out.append(DeviceFile(rel, st.st_size, st.st_mtime))

    def stat(self, path: str) -> DeviceFile:
        with self._map_errors():
            st = smbclient.stat(self._unc(path))
        return DeviceFile(path, st.st_size, st.st_mtime)

    def open_read(self, path: str) -> BinaryIO:
        with self._map_errors():
            return smbclient.open_file(self._unc(path), mode="rb")

    def delete_file(self, path: str) -> None:
        with self._map_errors():
            smbclient.remove(self._unc(path))

    def remove_dir_if_empty(self, folder: str) -> bool:
        # Deliberately NOT via _map_errors: a failed empty-dir cleanup is
        # cosmetic, and rmdir-on-non-empty arrives as an OSError we can't
        # distinguish from a connection error after wrapping.
        try:
            smbclient.rmdir(self._unc(folder))
            return True
        except (SMBException, OSError):
            return False  # not empty, already gone, or transient — all cosmetic

    def _stat_volume(self):
        return smbclient.stat_volume(self._unc())

    def disk_usage(self) -> DiskUsage:
        """Primary: SMB2 FsFullSizeInformation via stat_volume (live-smoke
        validated, spec Open item 1). Fallback: walk-derived usage against
        DEVICE_CAPACITY_GB."""
        try:
            v = self._stat_volume()
            return DiskUsage(total_bytes=v.total_size, free_bytes=v.actual_available_size)
        except (SMBException, OSError, NotImplementedError) as e:
            if self._capacity_bytes is None:
                raise DeviceUnavailable(
                    f"stat_volume failed ({e}) and DEVICE_CAPACITY_GB is not set — "
                    "cannot measure free space, refusing to reclaim blind"
                ) from e
            log.warning("stat_volume failed (%s); using walk-based fallback", e)
            used = sum(f.size for f in self.list_files())
            return DiskUsage(
                total_bytes=self._capacity_bytes,
                free_bytes=max(0, self._capacity_bytes - used),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smb_client.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/smb_client.py tests/test_smb_client.py
git commit -m "feat: SMB DeviceClient with guest auth and free-space fallback"
```

---

### Task 11: CLI + daemon loop

**Files:**
- Create: `src/seestar_sync/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None, device_factory: Callable[[Config], DeviceClient] | None = None, env: Mapping[str, str] | None = None) -> int` (returns exit code; the console script wraps it in `sys.exit`). Subcommands: `daemon`, `run` (both accept `--init`), `status`, `rebuild-manifest`.
- Consumes: `Config.from_env`, `ConfigError`, `Archive`, `ArchiveError`, `Manifest`, `run_sync`, `iso`, `report_all`, `SmbDeviceClient`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import json

from seestar_sync.cli import main
from tests.fakes import FakeDevice


def run_cli(tmp_path, *argv, device=None, extra_env=None):
    root = tmp_path / "archive"
    root.mkdir(exist_ok=True)
    env = {
        "SEESTAR_HOST": "h",
        "ARCHIVE_DIR": str(root),
        "ARCHIVE_ON_ROOT_FS_OK": "true",  # tmp dirs share st_dev with / on some systems
    }
    env.update(extra_env or {})
    return main(
        list(argv),
        device_factory=lambda cfg: device if device is not None else FakeDevice(),
        env=env,
    )


def test_missing_config_is_exit_2(tmp_path):
    assert main(["run"], env={}) == 2


def test_run_without_init_is_exit_2(tmp_path):
    assert run_cli(tmp_path, "run") == 2


def test_run_with_init_succeeds(tmp_path):
    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    status = json.loads((tmp_path / "archive" / ".seestar-sync" / "status.json").read_text())
    assert status["result"] == "ok" and status["files_copied"] == 1


def test_unreachable_device_is_exit_0(tmp_path):
    from seestar_sync.device import DeviceUnavailable

    class Dead(FakeDevice):
        def list_files(self):
            raise DeviceUnavailable("off")

    assert run_cli(tmp_path, "run", "--init", device=Dead()) == 0


def test_run_with_errors_is_exit_1(tmp_path):
    d = FakeDevice()
    d.put("A_sub/Light_a_20250101-000000.fit", b"aaaa", mtime=1.0)
    d.short_read_paths.add("A_sub/Light_a_20250101-000000.fit")
    assert run_cli(tmp_path, "run", "--init", device=d) == 1


def test_status_subcommand(tmp_path, capsys):
    d = FakeDevice()
    run_cli(tmp_path, "run", "--init", device=d)
    assert run_cli(tmp_path, "status") == 0
    assert "result: ok" in capsys.readouterr().out


def test_rebuild_manifest(tmp_path):
    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    run_cli(tmp_path, "run", "--init", device=d)
    db = tmp_path / "archive" / ".seestar-sync" / "manifest.db"
    db.unlink()  # simulate lost manifest
    assert run_cli(tmp_path, "rebuild-manifest") == 0
    from seestar_sync.manifest import Manifest

    with Manifest(db) as m:
        row = m.get("M 81_sub/Light_a_20250101-000000.fit")
        assert row is not None and row.size == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/seestar_sync/cli.py`**

```python
"""CLI: daemon | run | status | rebuild-manifest. Exit codes per spec:
0 ok/skipped_unreachable, 1 run-with-errors, 2 fatal (config/archive)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .archive import Archive, ArchiveError
from .config import Config, ConfigError
from .device import DeviceClient
from .manifest import Manifest
from .reporters import report_all
from .syncrun import iso, run_sync

log = logging.getLogger("seestar_sync")


def _default_device_factory(config: Config) -> DeviceClient:
    from .smb_client import SmbDeviceClient  # deferred: smbprotocol import is slow

    return SmbDeviceClient(config.seestar_host, capacity_gb=config.device_capacity_gb)


def _prepare(config: Config, init: bool) -> Archive:
    archive = Archive(config.archive_dir)
    if init:
        archive.init(allow_root_fs=config.archive_on_root_fs_ok)
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    return archive


def _one_run(config: Config, archive: Archive, device_factory) -> int:
    from .device import DeviceUnavailable

    try:
        device = device_factory(config)
    except DeviceUnavailable:
        from .models import RunSummary

        summary = RunSummary(started_at=iso(time.time()), result="skipped_unreachable",
                             dry_run=config.dry_run)
        report_all(config, archive.meta_dir, summary)
        return 0
    with Manifest(archive.meta_dir / "manifest.db") as manifest:
        summary = run_sync(device, archive, manifest, config)
    report_all(config, archive.meta_dir, summary)
    return 0 if summary.result in ("ok", "skipped_unreachable") else 1


def _cmd_run(config: Config, args, device_factory) -> int:
    archive = _prepare(config, args.init)
    return _one_run(config, archive, device_factory)


def _cmd_daemon(config: Config, args, device_factory) -> int:
    archive = _prepare(config, args.init)
    interval = config.sync_interval_minutes * 60
    stop = threading.Event()

    def _handle_signal(signum, frame):
        log.info("received signal %d; exiting after current run", signum)
        stop.set()

    # The container runs this as PID 1: without a handler, docker stop's
    # SIGTERM would be ignored and Docker would SIGKILL after the grace
    # period. A mid-copy SIGKILL is safe (.part discipline) but graceless.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("daemon: syncing %s every %d min", config.seestar_host,
             config.sync_interval_minutes)
    while not stop.is_set():
        code = _one_run(config, archive, device_factory)
        if code != 0:
            log.warning("run finished with errors (exit %d); continuing", code)
        stop.wait(interval)  # interruptible, unlike time.sleep
    return 0


def _cmd_status(config: Config, args, device_factory) -> int:
    status_path = config.archive_dir / ".seestar-sync" / "status.json"
    if not status_path.exists():
        print("no status yet — has a sync run completed?")
        return 0
    data = json.loads(status_path.read_text())
    print(f"last run: {data['last_run']}")
    print(f"result: {data['result']}" + ("  (dry run)" if data.get("dry_run") else ""))
    print(f"copied: {data['files_copied']} files / {data['bytes_copied']:,} bytes")
    print(f"reclaimed: {data['files_deleted']} files / {data['bytes_reclaimed']:,} bytes")
    if data.get("device_used_percent") is not None:
        print(f"device used: {data['device_used_percent']}%")
    if data.get("active_folders_skipped"):
        print(f"active folders skipped: {', '.join(data['active_folders_skipped'])}")
    for err in data.get("errors", []):
        print(f"error: {err}")
    return 0


def _cmd_rebuild_manifest(config: Config, args, device_factory) -> int:
    archive = Archive(config.archive_dir)
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    myworks = config.archive_dir / "MyWorks"
    now = iso(time.time())
    count = 0
    with Manifest(archive.meta_dir / "manifest.db") as manifest:
        for p in sorted(myworks.rglob("*")):
            if not p.is_file() or p.name.endswith(".part"):
                continue
            rel = p.relative_to(myworks).as_posix()
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            st = p.stat()
            manifest.record_verified(rel, st.st_size, st.st_mtime, h.hexdigest(), now)
            count += 1
    print(f"rebuilt manifest from archive: {count} files")
    return 0


def main(
    argv: list[str] | None = None,
    device_factory: Callable[[Config], DeviceClient] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="seestar-sync",
        description="Mirror Seestar media to a NAS over SMB and reclaim device space.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("daemon", _cmd_daemon), ("run", _cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--init", action="store_true",
                       help="first run only: create archive sentinel and directories")
        p.set_defaults(handler=handler)
    sub.add_parser("status").set_defaults(handler=_cmd_status, init=False)
    sub.add_parser("rebuild-manifest").set_defaults(handler=_cmd_rebuild_manifest, init=False)

    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        config = Config.from_env(env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    factory = device_factory or _default_device_factory
    try:
        return args.handler(config, args, factory)
    except ArchiveError as e:
        print(f"archive error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
        return 0


def entrypoint() -> None:  # console_scripts wrapper
    sys.exit(main())
```

Then update `pyproject.toml`'s script to point at the wrapper:

```toml
[project.scripts]
seestar-sync = "seestar_sync.cli:entrypoint"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 7 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add src/seestar_sync/cli.py tests/test_cli.py pyproject.toml
git commit -m "feat: CLI with daemon/run/status/rebuild-manifest and spec exit codes"
```

---

### Task 12: Docker + compose + README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md`

**Interfaces:**
- Consumes: the `seestar-sync` console script from Task 11.
- Produces: image whose `ENTRYPOINT` is `seestar-sync`, default `CMD ["daemon"]`.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Deliberately no USER directive: the archive volume's ownership varies by
# host (Mac SMB mounts, NAS bind mounts). k8s deployments should set a
# securityContext/fsGroup instead. Documented in the README.
ENTRYPOINT ["seestar-sync"]
CMD ["daemon"]
```

And `.dockerignore`:

```
.git/
.venv/
tests/
docs/
__pycache__/
```

- [ ] **Step 2: Write `docker-compose.yml`** (Mac iteration; NAS/k8s later reuse the same image)

```yaml
services:
  seestar-sync:
    build: .
    restart: unless-stopped
    stop_grace_period: 60s   # let an in-flight run wind down; SIGKILL is .part-safe anyway
    environment:
      SEESTAR_HOST: ${SEESTAR_HOST:?set SEESTAR_HOST in .env}
      DRY_RUN: ${DRY_RUN:-true}          # rollout: stays true until dry-run output is reviewed
      SYNC_INTERVAL_MINUTES: ${SYNC_INTERVAL_MINUTES:-15}
      KUMA_PUSH_URL: ${KUMA_PUSH_URL:-}
      HA_WEBHOOK_URL: ${HA_WEBHOOK_URL:-}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - ${ARCHIVE_HOST_PATH:?set ARCHIVE_HOST_PATH in .env}:/archive
```

- [ ] **Step 3: Build and smoke-test the image**

```bash
docker build -t seestar-sync .
docker run --rm seestar-sync run || echo "exit: $?"
```

Expected: `config error: SEESTAR_HOST is required` and `exit: 2` — config validation works in-container.

```bash
docker run --rm -e SEESTAR_HOST=h seestar-sync run || echo "exit: $?"
```

Expected: `archive error: ... does not exist — is the volume mounted?` (or the "own filesystem" message) and `exit: 2` — the ephemeral-filesystem trap is guarded in a real container.

- [ ] **Step 4: Write `README.md`**

Content requirements (write actual prose, not headings-only): what the tool does (one paragraph, mention keeping stacked JPGs on-device); quick start on a Mac (create `.env` with `SEESTAR_HOST`, `ARCHIVE_HOST_PATH`; mount the NAS share in Finder first; `docker compose up -d`); the first-run `--init` step (`docker compose run --rm seestar-sync run --init`); the rollout sequence (DRY_RUN=true → review logs/`status.json` → DRY_RUN=false); full env-var table copied from the spec §Operator interface; CLI subcommands and exit codes; how reclaim tiers work and what is never deleted (stacked JPGs/thumbnails); why the container runs as root by default (archive volume ownership varies across Mac SMB mounts and NAS bind mounts) and that k8s should use securityContext/fsGroup; graceful shutdown behavior (SIGTERM finishes the current run, .part discipline makes even SIGKILL safe); link to the spec at `docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml README.md
git commit -m "feat: Docker image, compose file for Mac iteration, README"
```

---

### Task 13: Gated live smoke test

**Files:**
- Create: `tests/test_smb_live.py`

**Interfaces:**
- Consumes: `SmbDeviceClient` (Task 10). Runs only when `SEESTAR_LIVE_TEST=1` and `SEESTAR_HOST` are set; otherwise every test skips.

- [ ] **Step 1: Write `tests/test_smb_live.py`**

```python
"""Live smoke ring — requires a powered-on Seestar on the LAN.

Run: SEESTAR_LIVE_TEST=1 SEESTAR_HOST=<ip-or-hostname> uv run pytest tests/test_smb_live.py -v -s
Validates spec Open items: guest SMB auth on the S30 Pro, share layout,
and the FsFullSizeInformation (stat_volume) spike. Read-only: no deletes.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SEESTAR_LIVE_TEST") != "1" or not os.environ.get("SEESTAR_HOST"),
    reason="live test: set SEESTAR_LIVE_TEST=1 and SEESTAR_HOST",
)


@pytest.fixture(scope="module")
def device():
    from seestar_sync.smb_client import SmbDeviceClient

    return SmbDeviceClient(os.environ["SEESTAR_HOST"])


def test_connects_and_lists(device):
    files = device.list_files()
    assert isinstance(files, list)
    print(f"\n{len(files)} files on device; first 10:")
    for f in files[:10]:
        print(f"  {f.path}  {f.size:,}B")


def test_layout_matches_expectations(device):
    from seestar_sync.classify import classify_tier

    files = device.list_files()
    if not files:
        pytest.skip("device is empty — capture something first")
    tiers = {f.path: classify_tier(f.path) for f in files}
    classified = [p for p, t in tiers.items() if t is not None]
    print(f"\n{len(classified)}/{len(files)} files are deletion candidates")
    # every .jpg must be un-deletable
    for path, tier in tiers.items():
        if path.lower().endswith(".jpg"):
            assert tier is None, f"JPG classified as deletable: {path}"


def test_stat_volume_spike(device):
    """Spec Open item 1: does Seestar firmware answer FsFullSizeInformation?"""
    usage = device.disk_usage()
    print(f"\ntotal={usage.total_bytes:,} free={usage.free_bytes:,} "
          f"used={usage.used_percent:.1f}%")
    assert 0 < usage.total_bytes < 256 * 1024**3  # sane for a 128 GB device
    assert 0 <= usage.used_percent <= 100


def test_read_one_file_streams(device):
    files = [f for f in device.list_files() if f.size > 0]
    if not files:
        pytest.skip("device is empty")
    smallest = min(files, key=lambda f: f.size)
    with device.open_read(smallest.path) as r:
        data = r.read()
    assert len(data) == smallest.size
```

- [ ] **Step 2: Verify the gate works without a device**

Run: `uv run pytest tests/test_smb_live.py -v`
Expected: 4 skipped (`live test: set SEESTAR_LIVE_TEST=1 and SEESTAR_HOST`)

- [ ] **Step 3: Run the full suite one last time**

Run: `uv run pytest`
Expected: all green, live tests skipped

- [ ] **Step 4: Commit**

```bash
git add tests/test_smb_live.py
git commit -m "test: gated live smoke ring for real-device validation"
```

- [ ] **Step 5 (manual, requires the telescope powered on — coordinate with the user):**

```bash
SEESTAR_LIVE_TEST=1 SEESTAR_HOST=<seestar-ip> uv run pytest tests/test_smb_live.py -v -s
```

Expected: 4 passed. If `test_stat_volume_spike` fails, set `DEVICE_CAPACITY_GB=128` in the runtime env (fallback path, already implemented) and file the finding in the README. If `register_session` fails auth, try `auth_protocol="negotiate"` and record what the S30 Pro accepted.

---

## Verification checklist (post-implementation)

- [ ] `uv run pytest` — full suite green
- [ ] `docker build` succeeds; containerized config/archive guards verified (Task 12 Step 3)
- [ ] Live smoke passes against the real S30 Pro (Task 13 Step 5)
- [ ] First real dry-run reviewed by the user before `DRY_RUN=false`
