import json

from seestar_sync.config import Config
from seestar_sync.models import ProgressEvent, RunSummary
from seestar_sync.mqtt import DISCOVERY_PREFIX, THROTTLE_SECONDS, MqttReporter
from seestar_sync.settings import SettingsStore, resolve_effective


class FakeClient:
    """Duck-types the paho v2 surface MqttReporter uses."""

    def __init__(self):
        self.published = []  # (topic, payload, retain)
        self.subscribed = []
        self.will = None
        self.connected = False
        self.on_connect = None
        self.on_message = None

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port)

    def loop_start(self):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        self.connected = False

    def disconnect(self):
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(tmp_path, **env_over):
    env = {"SEESTAR_HOST": "seestar.local", "MQTT_HOST": "ha.local",
           "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p"}
    env.update(env_over)
    config = Config.from_env(env)
    store = SettingsStore(tmp_path / "settings.json")
    fake = FakeClient()
    clock = FakeClock()
    rep = MqttReporter(config, store, client_factory=lambda: fake, clock=clock)
    return rep, fake, clock, config, store


def topics(fake):
    return [t for (t, _, _) in fake.published]


def test_start_publishes_discovery_availability_and_settings(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    assert fake.auth == ("u", "p")
    assert fake.will == ("seestar-sync/availability", "offline", True)
    # availability online, retained
    assert ("seestar-sync/availability", "online", True) in fake.published
    # discovery configs: at least state, progress, the two switches, host text
    disc = [t for t in topics(fake) if t.startswith(f"{DISCOVERY_PREFIX}/")]
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/state/config" in disc
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/progress/config" in disc
    assert f"{DISCOVERY_PREFIX}/switch/seestar-sync/dry_run/config" in disc
    assert f"{DISCOVERY_PREFIX}/switch/seestar-sync/reclaim_enabled/config" in disc
    assert f"{DISCOVERY_PREFIX}/text/seestar-sync/seestar_host/config" in disc
    # every discovery payload shares the device block + availability topic
    for topic, payload, retain in fake.published:
        if topic.startswith(DISCOVERY_PREFIX):
            body = json.loads(payload)
            assert retain is True
            assert body["availability_topic"] == "seestar-sync/availability"
            assert body["device"]["identifiers"] == ["seestar-sync"]
            assert "unique_id" in body
    # command subscriptions
    assert "seestar-sync/set/dry_run" in fake.subscribed
    assert "seestar-sync/set/reclaim_enabled" in fake.subscribed
    assert "seestar-sync/set/seestar_host" in fake.subscribed
    # settings echoed retained (env defaults)
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published
    assert ("seestar-sync/settings/reclaim_enabled", "OFF", True) in fake.published
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published


def test_progress_throttle(tmp_path):
    rep, fake, clock, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()

    rep.handle_progress(ProgressEvent(phase="copying", files_total=100, bytes_total=1000))
    rep.handle_progress(ProgressEvent(phase="copying", files_done=1, files_total=100,
                                      bytes_done=10, bytes_total=1000, current_path="a"))
    # phase change published; second event throttled (same phase, <10s)
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 1

    clock.t += THROTTLE_SECONDS
    rep.handle_progress(ProgressEvent(phase="copying", files_done=2, files_total=100,
                                      bytes_done=20, bytes_total=1000, current_path="b"))
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 2
    body = json.loads(prog[-1][1])
    assert body["percent"] == 2.0 and body["current_path"] == "b"

    # done ALWAYS publishes regardless of throttle, and updates state topic
    rep.handle_progress(ProgressEvent(phase="done", files_done=2, files_total=2,
                                      bytes_done=20, bytes_total=20))
    assert ("seestar-sync/state", "idle", True) in fake.published


def test_command_round_trip_and_invalid_payloads(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    fake.on_message(fake, None, Msg("seestar-sync/set/reclaim_enabled", b"ON"))
    assert store.load() == {"reclaim_enabled": True}
    assert ("seestar-sync/settings/reclaim_enabled", "ON", True) in fake.published

    fake.published.clear()
    fake.on_message(fake, None, Msg("seestar-sync/set/dry_run", b"banana"))
    assert "dry_run" not in store.load()  # rejected
    # state re-echoed so HA snaps back
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published

    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b"scope2.local"))
    assert store.load()["seestar_host"] == "scope2.local"


def test_phase_change_publishes_immediately(tmp_path):
    # spec: phase changes bypass the throttle
    rep, fake, clock, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    rep.handle_progress(ProgressEvent(phase="copying", files_total=2, bytes_total=20))
    rep.handle_progress(ProgressEvent(phase="reclaiming", files_total=1, bytes_total=5))
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert len(prog) == 2  # both published despite <10s gap
    assert ("seestar-sync/state", "reclaiming", True) in fake.published


def test_reconnect_republishes_discovery_and_settings(tmp_path):
    # HA/broker restart safety: on_connect fires again → full republish
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    fake.on_connect(fake, None, None, 0, None)
    disc = [t for (t, _, _) in fake.published if t.startswith(DISCOVERY_PREFIX)]
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/state/config" in disc
    assert ("seestar-sync/availability", "online", True) in fake.published
    assert ("seestar-sync/settings/dry_run", "OFF", True) in fake.published


def test_invalid_host_command_rejected_and_reechoed(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b""))
    assert "seestar_host" not in store.load()
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published


def test_stop_publishes_offline_retained(tmp_path):
    # LWT only fires on UNGRACEFUL death; clean shutdown must publish offline
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    rep.stop()
    assert ("seestar-sync/availability", "offline", True) in fake.published


def test_publish_summary_retains_and_never_raises(tmp_path):
    rep, fake, _, config, store = make(tmp_path)
    rep.start()
    summary = RunSummary(started_at="2026-07-10T04:00:00Z", result="ok",
                         files_copied=3, bytes_copied=30, device_used_percent=81.4)
    effective, sources = resolve_effective(config, store)
    rep.publish_summary(summary, effective, sources)
    pub = {t: (p, r) for (t, p, r) in fake.published}
    assert json.loads(pub["seestar-sync/summary"][0])["files_copied"] == 3
    assert pub["seestar-sync/summary"][1] is True  # retained
    assert pub["seestar-sync/fill"] == ("81.4", True)

    # a broken client must never propagate
    fake.publish = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    rep.publish_summary(summary, effective, sources)  # must not raise


def test_disabled_without_mqtt_host(tmp_path):
    env = {"SEESTAR_HOST": "seestar.local"}
    config = Config.from_env(env)
    store = SettingsStore(tmp_path / "settings.json")
    rep = MqttReporter(config, store, client_factory=lambda: FakeClient())
    rep.start()  # no-op, no client created
    rep.handle_progress(ProgressEvent(phase="copying"))
    rep.stop()  # all safe no-ops
