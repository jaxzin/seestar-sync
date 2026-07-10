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
    raise ConfigError(
        f"{key} must be a boolean (accepted: 1/true/yes/on or 0/false/no/off), got {raw!r}"
    )


def _number(env: Mapping[str, str], key: str, default: str, cast) -> float | int:
    raw = env.get(key, default)
    try:
        return cast(raw)
    except ValueError as e:
        kind = "an integer" if cast is int else "a number"
        raise ConfigError(f"{key} must be {kind}, got {raw!r}") from e


def _http_url(env: Mapping[str, str], key: str) -> str | None:
    """Webhook URLs must be http(s): urllib would happily open file:// etc."""
    url = env.get(key, "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{key} must be an http:// or https:// URL, got {url!r}")
    return url


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
            raise ConfigError(
                "SEESTAR_HOST is required — set it to the telescope's hostname or IP"
            )

        trigger = _number(env, "RECLAIM_TRIGGER_PERCENT", "75", float)
        target = _number(env, "RECLAIM_TARGET_PERCENT", "60", float)
        if not (0 < target < trigger <= 100):
            raise ConfigError(
                "RECLAIM_TARGET_PERCENT must be below RECLAIM_TRIGGER_PERCENT "
                f"and both in (0, 100]; got target={target}, trigger={trigger}"
            )

        capacity = (
            float(_number(env, "DEVICE_CAPACITY_GB", "0", float))
            if env.get("DEVICE_CAPACITY_GB", "").strip()
            else None
        )
        if capacity is not None and capacity <= 0:
            raise ConfigError(f"DEVICE_CAPACITY_GB must be positive, got {capacity}")

        interval = int(_number(env, "SYNC_INTERVAL_MINUTES", "15", int))
        if interval < 1:
            raise ConfigError(f"SYNC_INTERVAL_MINUTES must be >= 1, got {interval}")

        window = int(_number(env, "ACTIVE_WINDOW_MINUTES", "15", int))
        if window < 1:
            # the per-folder session guard is a delete-safety feature;
            # a zero/negative window would silently disable it
            raise ConfigError(f"ACTIVE_WINDOW_MINUTES must be >= 1, got {window}")

        log_level = env.get("LOG_LEVEL", "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(
                f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {log_level!r}"
            )

        return cls(
            seestar_host=host,
            archive_dir=Path(env.get("ARCHIVE_DIR", "/archive")),
            sync_interval_minutes=interval,
            reclaim_trigger_percent=trigger,
            reclaim_target_percent=target,
            active_window_minutes=window,
            device_capacity_gb=capacity,
            kuma_push_url=_http_url(env, "KUMA_PUSH_URL"),
            ha_webhook_url=_http_url(env, "HA_WEBHOOK_URL"),
            dry_run=_bool(env, "DRY_RUN"),
            archive_on_root_fs_ok=_bool(env, "ARCHIVE_ON_ROOT_FS_OK"),
            log_level=log_level,
        )
