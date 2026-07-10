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
            # Assign before loop_start(): paho may fire on_connect from its
            # network thread as soon as the loop starts, and the callbacks
            # publish via self._client.
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
