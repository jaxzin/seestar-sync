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
    assert "result: ok" in capsys.readouterr().out


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
