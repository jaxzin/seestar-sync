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
