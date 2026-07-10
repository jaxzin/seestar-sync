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
