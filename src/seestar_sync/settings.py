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
