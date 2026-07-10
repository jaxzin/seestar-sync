"""Live MQTT smoke — requires a reachable broker (e.g. HA Mosquitto add-on).

Run: MQTT_LIVE_TEST=1 MQTT_HOST=<broker> MQTT_USERNAME=u MQTT_PASSWORD=p \
     uv run pytest tests/test_mqtt_live.py -v -s
Publishes discovery + availability to the real broker under the
'seestar-sync-livetest' prefix, then cleans up. Manual checklist afterward:
device appears in HA MQTT integration; toggle DRY_RUN from the dashboard and
confirm settings.json in a scratch dir picks it up; docker stop flips the
availability entity to unavailable (LWT).
"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MQTT_LIVE_TEST") != "1" or not os.environ.get("MQTT_HOST"),
    reason="live test: set MQTT_LIVE_TEST=1 and MQTT_HOST",
)


def test_connect_discover_and_cleanup(tmp_path):
    from seestar_sync.config import Config
    from seestar_sync.mqtt import MqttReporter
    from seestar_sync.settings import SettingsStore

    env = {
        "SEESTAR_HOST": "seestar.local",
        "MQTT_HOST": os.environ["MQTT_HOST"],
        "MQTT_PORT": os.environ.get("MQTT_PORT", "1883"),
        "MQTT_USERNAME": os.environ.get("MQTT_USERNAME", ""),
        "MQTT_PASSWORD": os.environ.get("MQTT_PASSWORD", ""),
        "MQTT_TOPIC_PREFIX": "seestar-sync-livetest",
    }
    config = Config.from_env(env)
    rep = MqttReporter(config, SettingsStore(tmp_path / "settings.json"))
    rep.start()
    time.sleep(3)  # allow connect + on_connect discovery burst
    assert rep._client is not None
    rep.stop()
