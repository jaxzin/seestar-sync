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
    reclaim_enabled: bool = False  # machine-readable copy-only signal
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
            "reclaim_enabled": self.reclaim_enabled,
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "files_deleted": self.files_deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "device_used_percent": self.device_used_percent,
            "active_folders_skipped": self.active_folders_skipped,
            "errors": self.errors,
        }


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
