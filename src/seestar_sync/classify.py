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
