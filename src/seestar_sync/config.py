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
