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
_MIN_REDACT_LEN = 4  # shorter "secrets" (e.g. username "u") aren't secrets

_PHASE_TO_STATE = {
    "listing": "listing",
    "copying": "copying",
    "reclaiming": "reclaiming",
    "done": "idle",
}


def _paho_factory(client_id: str):
    import paho.mqtt.client as paho

    return paho.Client(
        callback_api_version=paho.CallbackAPIVersion.VERSION2,
        client_id=client_id,
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
        self._last_summary_publish = -THROTTLE_SECONDS
        self._last_phase: str | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not self._config.mqtt_host:
            return  # feature disabled: v0.1 behavior
        try:
            # client_id derives from the prefix so a second scope (different
            # MQTT_TOPIC_PREFIX) doesn't kick this one off the broker
            client = self._factory(self._prefix)
            if self._config.mqtt_username:
                client.username_pw_set(
                    self._config.mqtt_username, self._config.mqtt_password
                )
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.will_set(self._topic("availability"), "offline", retain=True)
            client.connect_async(self._config.mqtt_host, self._config.mqtt_port)
            # assign BEFORE loop_start: paho's network thread can fire
            # on_connect immediately, and the callbacks publish via self._client
            self._client = client
            client.loop_start()  # paho reconnects automatically from here on
        except Exception as e:  # noqa: BLE001 — reporter must never raise
            log.warning("mqtt start failed (non-fatal): %s", self._redact(e))
            self._client = None

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            # Canonical paho shutdown order. loop_stop-first would kill the
            # network thread with the offline packet still queued; the clean
            # DISCONNECT then suppresses LWT and HA shows the dead daemon
            # online forever. Flush the offline publish to the wire first.
            info = self._client.publish(self._topic("availability"), "offline",
                                        retain=True)
            try:
                info.wait_for_publish(timeout=2.0)
            except Exception:  # noqa: BLE001 — best-effort flush
                pass
            self._client.disconnect()
            self._client.loop_stop()
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt stop failed (non-fatal): %s", self._redact(e))

    # ---- publishing ------------------------------------------------------

    def handle_progress(self, event: ProgressEvent) -> None:
        """Observer consumer: phase changes and `done` publish immediately;
        otherwise ≥ THROTTLE_SECONDS apart (consumer-side throttling per spec)."""
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

    def handle_summary(self, summary: RunSummary) -> None:
        """Interim (in-pass) summary snapshot so Bytes/Files copied climb live.

        Publishes only the running-totals topic, throttled like progress; it
        deliberately does NOT touch state (progress owns that) or settings. The
        authoritative end-of-pass publish (with final result + state + settings)
        is still publish_summary. Never raises — telemetry must not fail a run."""
        if self._client is None:
            return
        try:
            now = self._clock()
            if now - self._last_summary_publish < THROTTLE_SECONDS:
                return
            self._last_summary_publish = now
            self._publish("summary", json.dumps(summary.to_json()), retain=True)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt interim summary publish failed (non-fatal): %s", self._redact(e))

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
                value = payload  # ALL host validation delegated to the store —
                # one validation authority (rejects empty/whitespace/control)
            else:
                log.warning("mqtt: command for unknown key %r ignored", key)
                return
            from .syncrun import iso

            old = self._store.load().get(key, "<unset>")
            try:
                self._store.set(key, value, iso(time.time()))
            except ValueError as e:
                # validator rejected: targeted warning + re-echo so HA snaps
                # back — spec §Failure isolation
                log.warning("mqtt: rejected %s command %r: %s", key, value, e)
                self._echo_current(key)
                return
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
            # replacing ultra-short substrings turns messages into soup
            # ("a<redacted>th failed") without hiding anything meaningful
            if secret and len(secret) >= _MIN_REDACT_LEN and secret in msg:
                msg = msg.replace(secret, "<redacted>")
        return msg

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": [self._prefix],  # prefix-derived: second scopes coexist
            "name": "Seestar Sync",
            "manufacturer": "seestar-sync",
            "sw_version": _version(),
        }

        def entity(component: str, object_id: str, extra: dict,
                   with_availability: bool = True) -> None:
            body = {
                "unique_id": f"{self._prefix}_{object_id}",
                "device": device,
                **extra,
            }
            if with_availability:
                body["availability_topic"] = self._topic("availability")
            self._client.publish(
                f"{DISCOVERY_PREFIX}/{component}/{self._prefix}/{object_id}/config",
                json.dumps(body), retain=True,
            )

        summary_t = self._topic("summary")
        # the alertable liveness entity (spec: "Daemon online" binary_sensor)
        # reads the availability topic directly, so it must NOT grey itself
        # out via that same topic — with_availability=False
        entity("binary_sensor", "online", {
            "name": "Daemon online",
            "state_topic": self._topic("availability"),
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
        }, with_availability=False)
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
            "name": "Last run time", "device_class": "timestamp",
            "state_topic": summary_t,
            "value_template": "{{ value_json.last_run }}",
        })
        for object_id, name, template in (
            ("files_copied", "Files copied", "{{ value_json.files_copied }}"),
            ("bytes_copied", "Bytes copied", "{{ value_json.bytes_copied }}"),
            ("files_deleted", "Files deleted", "{{ value_json.files_deleted }}"),
            ("bytes_reclaimed", "Bytes reclaimed", "{{ value_json.bytes_reclaimed }}"),
        ):
            entity("sensor", object_id, {
                "name": name, "state_topic": summary_t, "value_template": template,
            })
        entity("sensor", "errors", {
            "name": "Errors", "state_topic": summary_t,
            "value_template": "{{ value_json.errors | length }}",
            "json_attributes_topic": summary_t,  # spec: full list as attribute
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
            ("version", "Version", _version()),
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
