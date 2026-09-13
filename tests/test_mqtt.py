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
        self.calls = []  # ordered call log for sequencing assertions
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
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")

    class _MsgInfo:
        def __init__(self, owner):
            self._owner = owner

        def wait_for_publish(self, timeout=None):
            self._owner.calls.append("wait_for_publish")

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        self.calls.append(f"publish:{topic}")
        return self._MsgInfo(self)

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
    # factory takes the client_id arg (prefix-derived identity)
    rep = MqttReporter(config, store, client_factory=lambda *a: fake, clock=clock)
    return rep, fake, clock, config, store


def topics(fake):
    return [t for (t, _, _) in fake.published]


def test_start_publishes_discovery_availability_and_settings(tmp_path):
    rep, fake, *_ = make(tmp_path)
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
    assert f"{DISCOVERY_PREFIX}/binary_sensor/seestar-sync/online/config" in disc
    assert f"{DISCOVERY_PREFIX}/sensor/seestar-sync/version/config" in disc
    # every discovery payload shares the device block; all but the liveness
    # binary_sensor also share the availability topic (it must not grey
    # itself out via the topic it reports)
    for topic, payload, retain in fake.published:
        if topic.startswith(DISCOVERY_PREFIX):
            body = json.loads(payload)
            assert retain is True
            assert body["device"]["identifiers"] == ["seestar-sync"]
            assert "unique_id" in body
            if topic.endswith("/online/config"):
                assert "availability_topic" not in body
            else:
                assert body["availability_topic"] == "seestar-sync/availability"
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


def test_interim_summary_throttled_retained_and_totals_only(tmp_path):
    from seestar_sync.models import RunSummary

    rep, fake, clock, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()

    s = RunSummary(started_at="2026-07-10T00:00:00Z", result="ok")
    s.files_copied, s.bytes_copied = 1, 100
    rep.handle_summary(s)  # first snapshot publishes immediately
    s.files_copied, s.bytes_copied = 2, 300
    rep.handle_summary(s)  # throttled (<10s), dropped

    summ = [p for p in fake.published if p[0] == "seestar-sync/summary"]
    assert len(summ) == 1
    assert summ[0][2] is True  # retained
    assert json.loads(summ[0][1])["bytes_copied"] == 100

    clock.t += THROTTLE_SECONDS
    rep.handle_summary(s)  # now the updated totals go out
    summ = [p for p in fake.published if p[0] == "seestar-sync/summary"]
    assert len(summ) == 2
    assert json.loads(summ[-1][1])["bytes_copied"] == 300
    # interim publishes ONLY the summary topic — never state (progress owns it)
    # or settings; those belong to the authoritative end-of-pass publish_summary
    assert {t for t, _, _ in fake.published} == {"seestar-sync/summary"}


def test_command_round_trip_and_invalid_payloads(tmp_path):
    rep, fake, _, _config, store = make(tmp_path)
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
    rep, fake, *_ = make(tmp_path)
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
    rep, fake, _, _config, store = make(tmp_path)
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b""))
    assert "seestar_host" not in store.load()
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published

    # nonempty-but-invalid host: the strict validator rejects at store.set;
    # HA must still snap back (targeted rejection path, not the generic guard)
    fake.published.clear()
    fake.on_message(fake, None, Msg("seestar-sync/set/seestar_host", b"bad host"))
    assert "seestar_host" not in store.load()
    assert ("seestar-sync/settings/seestar_host", "seestar.local", True) in fake.published


def test_stop_publishes_offline_retained_before_disconnect(tmp_path):
    # LWT only fires on UNGRACEFUL death, and a clean DISCONNECT suppresses
    # it — so the ORDER is the contract: flush offline to the wire, THEN
    # disconnect, THEN stop the loop. A reorder must go red here.
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    fake.calls.clear()
    rep.stop()
    assert ("seestar-sync/availability", "offline", True) in fake.published
    off = fake.calls.index("publish:seestar-sync/availability")
    assert off < fake.calls.index("wait_for_publish")
    assert fake.calls.index("wait_for_publish") < fake.calls.index("disconnect")
    assert fake.calls.index("disconnect") < fake.calls.index("loop_stop")


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


def test_guard_warnings_fire_and_redact_readably(tmp_path, caplog):
    # SDET: never-raise tests must assert the warning FIRED (a silent no-op
    # can't masquerade as a caught failure), the secret is gone, and short
    # non-secrets (username "u") don't turn the message into soup
    import logging

    from seestar_sync.settings import resolve_effective

    rep, fake, _, config, store = make(tmp_path, MQTT_PASSWORD="hunter2secret")
    rep.start()

    def explode(*a, **k):
        raise OSError("auth failed for hunter2secret while connecting")

    fake.publish = explode
    with caplog.at_level(logging.WARNING):
        rep.publish_settings(*resolve_effective(config, store))
    assert "settings publish failed" in caplog.text  # guard observably fired
    assert "hunter2secret" not in caplog.text
    assert "<redacted>" in caplog.text
    assert "auth failed" in caplog.text  # username "u" didn't mangle the words


def test_progress_percent_zero_total(tmp_path):
    # division guard: a zero-byte plan must publish 0.0, not die silently
    rep, fake, *_ = make(tmp_path)
    rep.start()
    fake.published.clear()
    rep.handle_progress(ProgressEvent(phase="done"))
    prog = [p for p in fake.published if p[0] == "seestar-sync/progress"]
    assert json.loads(prog[-1][1])["percent"] == 0.0


def test_disabled_without_mqtt_host(tmp_path):
    env = {"SEESTAR_HOST": "seestar.local"}
    config = Config.from_env(env)
    store = SettingsStore(tmp_path / "settings.json")
    rep = MqttReporter(config, store, client_factory=lambda *a: FakeClient())
    rep.start()  # no-op, no client created
    rep.handle_progress(ProgressEvent(phase="copying"))
    rep.stop()  # all safe no-ops


def test_shadow_mode_refuses_settings_commands(tmp_path, caplog):
    rep, fake, _, _config, _store = make(tmp_path, SHADOW_MODE="true")
    rep.start()
    fake.published.clear()

    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload

    with caplog.at_level("WARNING"):
        for key, payload in (("dry_run", b"OFF"), ("reclaim_enabled", b"ON"),
                             ("seestar_host", b"scope2.local")):
            fake.on_message(fake, None, Msg(f"seestar-sync/set/{key}", payload))
    assert not (tmp_path / "settings.json").exists()
    assert "SHADOW_MODE" in caplog.text
    # state still published (re-echo) so HA snaps back to the forced values
    assert ("seestar-sync/settings/dry_run", "ON", True) in fake.published
    assert ("seestar-sync/settings/reclaim_enabled", "OFF", True) in fake.published
