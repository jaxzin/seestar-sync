import hashlib
import json

from seestar_sync.cli import main
from tests.fakes import FakeDevice


def run_cli(tmp_path, *argv, device=None, device_factory=None, extra_env=None):
    root = tmp_path / "archive"
    root.mkdir(exist_ok=True)
    env = {
        "SEESTAR_HOST": "h",
        "ARCHIVE_DIR": str(root),
        "ARCHIVE_ON_ROOT_FS_OK": "true",  # tmp dirs share st_dev with / on some systems
    }
    env.update(extra_env or {})
    factory = device_factory or (
        lambda cfg: device if device is not None else FakeDevice()
    )
    return main(list(argv), device_factory=factory, env=env)


def test_missing_config_is_exit_2(tmp_path):
    assert main(["run"], env={}) == 2


def test_run_without_init_is_exit_2(tmp_path):
    assert run_cli(tmp_path, "run") == 2


def test_run_with_init_succeeds(tmp_path):
    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    status = json.loads((tmp_path / "archive" / ".seestar-sync" / "status.json").read_text())
    assert status["result"] == "ok" and status["files_copied"] == 1


def test_unreachable_device_is_exit_0(tmp_path):
    from seestar_sync.device import DeviceUnavailable

    class Dead(FakeDevice):
        def list_files(self):
            raise DeviceUnavailable("off")

    assert run_cli(tmp_path, "run", "--init", device=Dead()) == 0


def test_unreachable_at_connect_is_exit_0_and_reported(tmp_path):
    # the PRODUCTION-primary path: SmbDeviceClient connects eagerly, so a
    # powered-off telescope raises at the device factory, not at list_files —
    # this branch is what keeps Kuma green every night the scope is off
    from seestar_sync.device import DeviceUnavailable

    def dead_factory(cfg):
        raise DeviceUnavailable("off")

    run_cli(tmp_path, "run", "--init")  # initialize the archive first
    assert run_cli(tmp_path, "run", device_factory=dead_factory) == 0
    status = json.loads(
        (tmp_path / "archive" / ".seestar-sync" / "status.json").read_text()
    )
    assert status["result"] == "skipped_unreachable"


def test_status_without_any_run_yet(tmp_path, capsys):
    assert run_cli(tmp_path, "status") == 0
    assert "no status yet" in capsys.readouterr().out


def test_one_run_refuses_when_sentinel_vanishes_mid_life(tmp_path):
    # architect finding (proven data-loss chain): the mount-drop trap is a
    # mid-life event — the daemon's per-pass check_ready must refuse before
    # a phantom manifest can be created and reclaim armed against it
    import pytest

    from seestar_sync.archive import Archive, ArchiveError
    from seestar_sync.cli import _one_run
    from seestar_sync.config import Config

    root = tmp_path / "archive"
    root.mkdir()
    cfg = Config.from_env(
        {"SEESTAR_HOST": "h", "ARCHIVE_DIR": str(root), "ARCHIVE_ON_ROOT_FS_OK": "true"}
    )
    archive = Archive(root)
    archive.init(allow_root_fs=True)
    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    assert _one_run(cfg, archive, lambda c: d) == 0

    (root / ".seestar-sync" / "ARCHIVE_MARKER").unlink()  # mount "drops"
    d.put("M 81_sub/Light_b_20250102-000000.fit", b"more", mtime=2.0)
    with pytest.raises(ArchiveError):
        _one_run(cfg, archive, lambda c: d)
    assert len(d.list_files()) == 2  # nothing deleted, nothing phantom-copied


def test_run_with_errors_is_exit_1(tmp_path):
    d = FakeDevice()
    d.put("A_sub/Light_a_20250101-000000.fit", b"aaaa", mtime=1.0)
    d.short_read_paths.add("A_sub/Light_a_20250101-000000.fit")
    assert run_cli(tmp_path, "run", "--init", device=d) == 1


def test_bad_log_level_is_exit_2(tmp_path):
    # SDET finding: logging.basicConfig raises ValueError on unknown levels;
    # that must surface as a config error (exit 2), not a traceback
    assert run_cli(tmp_path, "run", "--init", extra_env={"LOG_LEVEL": "banana"}) == 2


def test_status_subcommand(tmp_path, capsys):
    d = FakeDevice()
    run_cli(tmp_path, "run", "--init", device=d)
    assert run_cli(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "result: ok" in out
    assert "(copy-only)" in out  # RECLAIM_ENABLED defaults false → copy-only mode


def test_rebuild_manifest(tmp_path):
    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    run_cli(tmp_path, "run", "--init", device=d)
    db = tmp_path / "archive" / ".seestar-sync" / "manifest.db"
    db.unlink()  # simulate lost manifest
    assert run_cli(tmp_path, "rebuild-manifest") == 0
    from seestar_sync.manifest import Manifest

    with Manifest(db) as m:
        row = m.get("M 81_sub/Light_a_20250101-000000.fit")
        assert row is not None and row.size == 4
        # a rebuild that wrote sha256="" would re-arm reclaim on unverifiable
        # rows — pin the real recomputed checksum and restored eligibility
        assert row.sha256 == hashlib.sha256(b"data").hexdigest()
        assert m.verified_ok("M 81_sub/Light_a_20250101-000000.fit", 4, 1.0)


def test_settings_override_applies_next_pass(tmp_path):
    # write an override the way MQTT would, then run with a default env:
    # the pass must honor the override (mode-independent effective config)
    from seestar_sync.settings import SettingsStore

    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    assert run_cli(tmp_path, "run", "--init", device=d) == 0  # creates meta dir
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("dry_run", True, "2026-07-10T00:00:00Z")
    d.put("M 81_sub/Light_b_20250102-000000.fit", b"more", mtime=2.0)
    assert run_cli(tmp_path, "run", device=d) == 0
    status = json.loads(
        (tmp_path / "archive" / ".seestar-sync" / "status.json").read_text()
    )
    assert status["dry_run"] is True  # override applied despite env default false
    # file b not copied (dry run) — archive only has file a
    assert not (tmp_path / "archive" / "MyWorks" / "M 81_sub"
                / "Light_b_20250102-000000.fit").exists()


def test_status_shows_setting_sources(tmp_path, capsys):
    from seestar_sync.settings import SettingsStore

    run_cli(tmp_path, "run", "--init")
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    assert run_cli(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "reclaim_enabled: True (mqtt override)" in out
    assert "dry_run: False (env)" in out


def test_mqtt_reclaim_override_arms_and_disarms_deletion(tmp_path):
    # the highest-blast-radius HA write, end-to-end in BOTH directions
    from seestar_sync.settings import SettingsStore

    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)  # 80%
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    assert len(d.list_files()) == 1  # env default: reclaim off, file survives
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    assert run_cli(tmp_path, "run", device=d) == 0
    assert d.list_files() == []  # armed via MQTT override → really deleted

    store.set("reclaim_enabled", False, "2026-07-10T01:00:00Z")
    d.put("M 81_sub/Light_b_20250102-000000.fit", b"y" * 800, mtime=2.0)
    assert run_cli(tmp_path, "run", device=d, extra_env={"RECLAIM_ENABLED": "true"}) == 0
    assert len(d.list_files()) == 1  # override False beats env true → survives


def test_mid_pass_override_does_not_half_apply(tmp_path):
    # spec §Testing: effective config is a per-pass SNAPSHOT — an override
    # written mid-pass (as the MQTT thread would) never half-applies
    from seestar_sync.settings import SettingsStore

    store_box = {}

    class FlippingDevice(FakeDevice):
        def open_read(self, path):
            if not store_box.get("flipped"):
                store_box["flipped"] = True
                store_box["store"].set("dry_run", True, "2026-07-10T00:00:00Z")
            return super().open_read(path)

    d = FlippingDevice()
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 4, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 4, mtime=2.0)
    store_box["store"] = SettingsStore(
        tmp_path / "archive" / ".seestar-sync" / "settings.json"
    )
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    # flip happened after file A's read, yet BOTH files really copied
    assert (tmp_path / "archive" / "MyWorks" / "B_sub"
            / "Light_b_20250102-000000.fit").exists()
    d.put("C_sub/Light_c_20250103-000000.fit", b"c" * 4, mtime=3.0)
    assert run_cli(tmp_path, "run", device=d) == 0
    assert not (tmp_path / "archive" / "MyWorks" / "C_sub"
                / "Light_c_20250103-000000.fit").exists()  # next pass: dry run


def test_one_run_wires_mqtt_observer_and_summary(tmp_path):
    # the feature's delivery path: observer events flow through _one_run and
    # publish_summary fires exactly once per pass — INCLUDING the
    # factory-unreachable branch (what keeps HA's last-result sensor honest
    # on nights the telescope is off)
    from seestar_sync.archive import Archive
    from seestar_sync.cli import _one_run
    from seestar_sync.config import Config
    from seestar_sync.device import DeviceUnavailable

    class StubMqtt:
        def __init__(self):
            self.events = []
            self.summaries = []
            self.interim = []

        def handle_progress(self, event):
            self.events.append(event)

        def handle_summary(self, summary):
            # snapshot values (summary is mutated in place across the pass)
            self.interim.append((summary.files_copied, summary.bytes_copied))

        def publish_summary(self, summary, effective, sources):
            self.summaries.append((summary, effective, sources))

    root = tmp_path / "archive"
    root.mkdir()
    cfg = Config.from_env(
        {"SEESTAR_HOST": "h", "ARCHIVE_DIR": str(root), "ARCHIVE_ON_ROOT_FS_OK": "true"}
    )
    archive = Archive(root)
    archive.init(allow_root_fs=True)

    d = FakeDevice()
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"data", mtime=1.0)
    mqtt = StubMqtt()
    assert _one_run(cfg, archive, lambda c: d, mqtt=mqtt) == 0
    assert mqtt.events[0].phase == "listing"
    assert mqtt.events[-1].phase == "done"
    assert len(mqtt.summaries) == 1
    summary, _effective, sources = mqtt.summaries[0]
    assert summary.result == "ok"
    assert set(sources) == {"dry_run", "reclaim_enabled", "seestar_host"}
    # interim summary fired live during the pass (one 4-byte file completed)
    assert mqtt.interim == [(1, 4)]

    def dead_factory(c):
        raise DeviceUnavailable("off")

    mqtt2 = StubMqtt()
    assert _one_run(cfg, archive, dead_factory, mqtt=mqtt2) == 0
    assert mqtt2.events == []  # no pass ran
    assert mqtt2.interim == []  # ...so no interim summaries either
    assert len(mqtt2.summaries) == 1
    assert mqtt2.summaries[0][0].result == "skipped_unreachable"


def test_third_party_loggers_quieted(tmp_path):
    import logging

    run_cli(tmp_path, "run", "--init")
    assert logging.getLogger("smbprotocol").level == logging.WARNING
    assert logging.getLogger("paho").level == logging.WARNING


def test_debug_level_keeps_third_party_loggers_verbose(tmp_path):
    import logging

    logging.getLogger("smbprotocol").setLevel(logging.NOTSET)  # reset from prior test
    run_cli(tmp_path, "run", "--init", extra_env={"LOG_LEVEL": "DEBUG"})
    assert logging.getLogger("smbprotocol").level == logging.NOTSET


def _snapshot(root):
    return {
        p.relative_to(root).as_posix(): (
            p.stat().st_size, p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
        )
        for p in sorted(root.rglob("*"))
    }


def _shadow_fixture(tmp_path):
    """A NAS-daemon-populated archive whose settings.json arms deletion, and
    a device far over the reclaim trigger with every file verified."""
    from seestar_sync.settings import SettingsStore

    d = FakeDevice(capacity=100)
    for i in range(4):
        d.put(f"M 81_sub/Light_{i}_20250101-00000{i}.fit", b"x" * 20, mtime=float(i))
    assert run_cli(tmp_path, "run", "--init", device=d) == 0  # normal mode copies
    store = SettingsStore(tmp_path / "archive" / ".seestar-sync" / "settings.json")
    store.set("dry_run", False, "2026-07-10T00:00:00Z")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    d.put("M 42_sub/Light_new_20250201-000000.fit", b"y" * 5, mtime=9.0)  # uncopied
    return d


def test_shadow_mode_never_deletes_or_writes_archive(tmp_path):
    d = _shadow_fixture(tmp_path)
    root = tmp_path / "archive"
    before_device = d.list_files()
    before = _snapshot(root)

    def forbid(*a, **k):
        raise AssertionError("device delete attempted in shadow mode")

    d.delete_file = forbid
    d.remove_dir_if_empty = forbid
    for _ in range(2):  # the second pass would see a first-pass write
        assert run_cli(tmp_path, "run", device=d,
                       extra_env={"SHADOW_MODE": "true"}) == 0
    assert d.list_files() == before_device
    assert _snapshot(root) == before


def test_shadow_mode_missing_manifest_exits_nonzero(tmp_path, capsys):
    d = FakeDevice()
    assert run_cli(tmp_path, "run", "--init", device=d) == 0
    db = tmp_path / "archive" / ".seestar-sync" / "manifest.db"
    for p in db.parent.glob("manifest.db*"):
        p.unlink()
    assert run_cli(tmp_path, "run", device=d, extra_env={"SHADOW_MODE": "true"}) == 2
    assert "manifest.db" in capsys.readouterr().err
    assert not db.exists()


def test_shadow_mode_unreachable_device_writes_nothing(tmp_path):
    from seestar_sync.device import DeviceUnavailable

    _shadow_fixture(tmp_path)
    before = _snapshot(tmp_path / "archive")

    def dead(cfg):
        raise DeviceUnavailable("off")

    assert run_cli(tmp_path, "run", device_factory=dead,
                   extra_env={"SHADOW_MODE": "true"}) == 0
    assert _snapshot(tmp_path / "archive") == before


def test_shadow_mode_refuses_archive_mutating_commands(tmp_path, capsys):
    _shadow_fixture(tmp_path)
    before = _snapshot(tmp_path / "archive")
    assert run_cli(tmp_path, "run", "--init", extra_env={"SHADOW_MODE": "true"}) == 2
    assert run_cli(tmp_path, "rebuild-manifest", extra_env={"SHADOW_MODE": "true"}) == 2
    assert "SHADOW_MODE" in capsys.readouterr().err
    assert _snapshot(tmp_path / "archive") == before


def test_shadow_mode_hot_journal_skips_pass_cleanly(tmp_path, caplog, capsys):
    import logging

    from tests.fakes import strand_hot_journal

    d = _shadow_fixture(tmp_path)
    root = tmp_path / "archive"
    strand_hot_journal(root / ".seestar-sync" / "manifest.db")
    before = _snapshot(root)
    before_device = d.list_files()
    caplog.set_level(logging.WARNING, logger="seestar_sync")
    for _ in range(2):  # retried next pass, same clean skip
        assert run_cli(tmp_path, "run", device=d,
                       extra_env={"SHADOW_MODE": "true"}) == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "hot journal" in r.getMessage()]
    assert len(warnings) == 2
    assert all(r.exc_info is None for r in warnings)
    assert "Traceback" not in capsys.readouterr().err
    assert _snapshot(root) == before  # journal left for the primary to replay
    assert d.list_files() == before_device


def test_shadow_mode_hot_journal_reports_skip_to_mqtt_and_kuma(tmp_path, monkeypatch):
    from seestar_sync.archive import Archive
    from seestar_sync.cli import _one_run
    from seestar_sync.config import Config
    from tests.fakes import strand_hot_journal

    d = _shadow_fixture(tmp_path)
    root = tmp_path / "archive"
    strand_hot_journal(root / ".seestar-sync" / "manifest.db")
    pings = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("seestar_sync.reporters.urllib.request.urlopen",
                        lambda url, timeout=None: pings.append(url) or _Resp())

    class StubMqtt:
        handle_progress = handle_summary = None

        def __init__(self):
            self.summaries = []

        def publish_summary(self, summary, effective, sources):
            self.summaries.append(summary)

    cfg = Config.from_env({"SEESTAR_HOST": "h", "ARCHIVE_DIR": str(root),
                           "ARCHIVE_ON_ROOT_FS_OK": "true", "SHADOW_MODE": "true",
                           "KUMA_PUSH_URL": "http://kuma.local/api/push/t"})
    mqtt = StubMqtt()
    assert _one_run(cfg, Archive(root), lambda c: d, mqtt=mqtt) == 0
    assert [s.result for s in mqtt.summaries] == ["skipped_manifest_busy"]
    assert mqtt.summaries[0].dry_run is True
    assert len(pings) == 1 and "status=up" in pings[0]
