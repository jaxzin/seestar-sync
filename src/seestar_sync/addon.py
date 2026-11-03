"""Home Assistant add-on entrypoint.

Translates the add-on's options (``/data/options.json``, written by the
Supervisor) into the environment variables the daemon already understands, then
replaces itself with ``seestar-sync daemon --init``. The daemon, its safety
guards and its MQTT device are unchanged; this module only maps configuration.

MQTT: when the ``mqtt_host`` option is blank, the broker is taken from the
Supervisor's MQTT service (the Mosquitto add-on). That service is requested with
``services: [mqtt:want]`` in ``config.yaml``; if it is absent the daemon runs
without MQTT, because the sync itself does not need Home Assistant.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

log = logging.getLogger("seestar_sync.addon")

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
_SUPERVISOR_TIMEOUT_SEC = 10

# option name -> environment variable. Anything absent from this table is not
# forwarded, and the test suite asserts it covers every option in config.yaml.
_OPTION_ENV: dict[str, str] = {
    "seestar_host": "SEESTAR_HOST",
    "archive_dir": "ARCHIVE_DIR",
    "sync_interval_minutes": "SYNC_INTERVAL_MINUTES",
    "active_window_minutes": "ACTIVE_WINDOW_MINUTES",
    "dry_run": "DRY_RUN",
    "reclaim_enabled": "RECLAIM_ENABLED",
    "reclaim_trigger_percent": "RECLAIM_TRIGGER_PERCENT",
    "reclaim_target_percent": "RECLAIM_TARGET_PERCENT",
    "device_capacity_gb": "DEVICE_CAPACITY_GB",
    "kuma_push_url": "KUMA_PUSH_URL",
    "shadow_mode": "SHADOW_MODE",
    "mqtt_topic_prefix": "MQTT_TOPIC_PREFIX",
    "log_level": "LOG_LEVEL",
}
# MQTT connection options are resolved separately (see _mqtt_env).
_MQTT_OPTIONS = ("mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password")
MAPPED_OPTIONS = frozenset(_OPTION_ENV) | frozenset(_MQTT_OPTIONS)


class AddonError(Exception):
    """The add-on configuration cannot start the daemon. Message is user-facing."""


def _text(value: object) -> str:
    """Render an option value as the env-var string the daemon parses."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def fetch_mqtt_service(
    token: str,
    urlopen: Callable[..., object] = urllib.request.urlopen,
) -> Mapping[str, object] | None:
    """Return the Supervisor's MQTT service record, or None if no broker offers one."""
    request = urllib.request.Request(
        SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urlopen(request, timeout=_SUPERVISOR_TIMEOUT_SEC) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404, 503):  # no MQTT service registered
            return None
        raise AddonError(f"Supervisor MQTT lookup failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AddonError(f"Supervisor MQTT lookup failed: {exc}") from exc
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) and data.get("host") else None


def _mqtt_env(
    options: Mapping[str, object],
    service: Callable[[], Mapping[str, object] | None],
) -> dict[str, str]:
    """MQTT_* variables from the options, else from the Supervisor service."""
    pinned = _text(options.get("mqtt_host")).strip()
    if pinned:
        return {
            "MQTT_HOST": pinned,
            "MQTT_PORT": _text(options.get("mqtt_port") or 1883),
            "MQTT_USERNAME": _text(options.get("mqtt_username")),
            "MQTT_PASSWORD": _text(options.get("mqtt_password")),
        }

    record = service()
    if record is None:
        log.warning(
            "No MQTT broker: install the Mosquitto add-on or set mqtt_host. "
            "Syncing without Home Assistant entities."
        )
        return {}
    if record.get("ssl"):
        raise AddonError(
            "The Supervisor MQTT service requires TLS, which seestar-sync does not "
            "support. Set mqtt_host/mqtt_port/mqtt_username/mqtt_password to a "
            "plain-text listener (Mosquitto add-on default: port 1883)."
        )
    log.info("Using the Supervisor MQTT service (%s).", record["host"])
    return {
        "MQTT_HOST": _text(record["host"]),
        "MQTT_PORT": _text(record.get("port") or 1883),
        "MQTT_USERNAME": _text(record.get("username")),
        "MQTT_PASSWORD": _text(record.get("password")),
    }


def build_env(
    options: Mapping[str, object],
    service: Callable[[], Mapping[str, object] | None],
) -> dict[str, str]:
    """Environment for the daemon. Raises AddonError on unusable options."""
    if not _text(options.get("seestar_host")).strip():
        raise AddonError(
            "seestar_host is not set. Open the add-on Configuration tab and enter "
            "the telescope's IP address or hostname."
        )
    archive_dir = _text(options.get("archive_dir")).strip()
    if not archive_dir.startswith("/"):
        raise AddonError(
            f"archive_dir must be an absolute path such as /media/seestar, got {archive_dir!r}."
        )

    env: dict[str, str] = {}
    for option, variable in _OPTION_ENV.items():
        value = _text(options.get(option))
        # An unset optional (blank) must stay unset so the daemon's default applies
        # (e.g. DEVICE_CAPACITY_GB, KUMA_PUSH_URL).
        if value != "":
            env[variable] = value
    env.update(_mqtt_env(options, service))
    return env


def main(
    options_path: Path = OPTIONS_PATH,
    environ: Mapping[str, str] | None = None,
    exec_fn: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
) -> int:
    environ = os.environ if environ is None else environ
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        options = json.loads(options_path.read_text())
        token = environ.get("SUPERVISOR_TOKEN", "")
        env = build_env(options, lambda: fetch_mqtt_service(token) if token else None)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot read add-on options from %s: %s", options_path, exc)
        return 2
    except AddonError as exc:
        log.error("%s", exc)
        return 2

    child_env = {**environ, **env}
    log.info(
        "Starting seestar-sync (dry_run=%s, reclaim_enabled=%s, archive=%s).",
        env.get("DRY_RUN", "false"),
        env.get("RECLAIM_ENABLED", "false"),
        env["ARCHIVE_DIR"],
    )
    exec_fn("seestar-sync", ["seestar-sync", "daemon", "--init"], child_env)
    return 0  # unreachable with the real execvpe


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
