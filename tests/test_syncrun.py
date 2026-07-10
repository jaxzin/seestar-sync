import pytest

from seestar_sync.archive import Archive
from seestar_sync.config import Config
from seestar_sync.manifest import Manifest
from seestar_sync.syncrun import run_sync
from tests.fakes import FakeDevice


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    archive = Archive(root, dev_of=lambda p: 2 if str(p).startswith(str(root)) else 1)
    archive.init(allow_root_fs=False)
    manifest = Manifest(archive.meta_dir / "manifest.db")
    yield archive, manifest
    manifest.close()


def config(**overrides):
    env_vars = {"SEESTAR_HOST": "h"}
    env_vars.update(overrides)
    return Config.from_env(env_vars)


def test_unreachable_device_is_a_skip_not_an_error(env):
    archive, manifest = env

    class DeadDevice(FakeDevice):
        def list_files(self):
            from seestar_sync.device import DeviceUnavailable

            raise DeviceUnavailable("off")

    s = run_sync(DeadDevice(), archive, manifest, config())
    assert s.result == "skipped_unreachable"
    assert s.files_copied == 0


def test_copies_everything_and_verifies(env):
    archive, manifest = env
    d = FakeDevice(capacity=10_000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"raw" * 100, mtime=1.0)
    d.put("M 81/DSO_Stacked_1_M 81_10.0s_20250101_000000.jpg", b"jpg", mtime=1.0)

    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.result == "ok"
    assert s.files_copied == 2
    assert archive.dest_path("M 81/DSO_Stacked_1_M 81_10.0s_20250101_000000.jpg").exists()
    # second run: nothing new to do
    s2 = run_sync(d, archive, manifest, config(), now=1_000_100.0)
    assert s2.files_copied == 0


def test_reclaim_deletes_only_verified_with_write_ahead_audit(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)  # 80% > trigger 75
    s1 = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s1.files_copied == 1
    assert s1.files_deleted == 1  # verified this run, then reclaimed under pressure
    assert d.list_files() == []  # gone from device
    row = manifest.get("M 81_sub/Light_a_20250101-000000.fit")
    assert row.deleted_from_device_at is not None and row.deleted_tier == 1
    # NAS copy retained
    assert archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()


def test_active_folder_is_left_alone(env):
    archive, manifest = env
    now = 1_000_000.0
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 900, mtime=now - 60)  # active!
    s = run_sync(d, archive, manifest, config(), now=now)
    assert s.files_copied == 0 and s.files_deleted == 0
    assert s.active_folders_skipped == ["M 81_sub"]


def test_mid_run_disconnect_keeps_partial_progress(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 10, mtime=1.0)
    d.fail_after_reads = 1
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.files_copied == 1  # first banked before the "power-off"
    assert s.result == "ok"  # clean disconnect, no errors
    assert s.files_deleted == 0  # reclaim skipped once device vanished


def test_verify_failure_counts_and_eventually_skips(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    path = "A_sub/Light_a_20250101-000000.fit"
    d.put(path, b"a" * 10, mtime=1.0)
    d.short_read_paths.add(path)  # every read is torn
    for i in range(3):
        s = run_sync(d, archive, manifest, config(), now=1_000_000.0 + i)
        assert s.result == "partial"
        assert any("verify" in e for e in s.errors)
    # 4th run: flagged and skipped, not retried forever
    s4 = run_sync(d, archive, manifest, config(), now=1_000_010.0)
    assert any("skipping" in e for e in s4.errors)
    assert manifest.failure_count(path) == 3


def test_false_delete_audit_reconciles_next_run(env):
    # crash state: write-ahead audit committed but the SMB delete never
    # happened (spec: a false "deleted" must self-heal, never the reverse)
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    path = "M 81_sub/Light_a_20250101-000000.fit"
    d.put(path, b"x" * 8000, mtime=1.0)
    s1 = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s1.files_copied == 1 and s1.files_deleted == 0
    from seestar_sync.syncrun import iso

    manifest.mark_deleted(path, tier=1, deleted_at=iso(1_000_100.0))  # forged crash
    d.capacity = 10_000  # now 80% used → reclaim pressure
    s2 = run_sync(d, archive, manifest, config(), now=1_000_200.0)
    assert s2.files_deleted == 1
    assert d.list_files() == []  # reconciled: really deleted this time
    assert manifest.get(path).deleted_from_device_at == iso(1_000_200.0)
    assert archive.dest_path(path).exists()  # NAS copy untouched throughout


def test_write_ahead_audit_commits_before_delete_issued(env):
    # ordering asserted OBSERVABLY, not by end-state: the device refuses the
    # delete unless the audit row is already committed
    archive, manifest = env

    class OrderCheckingDevice(FakeDevice):
        def delete_file(self, path):
            row = manifest.get(path)
            assert row is not None and row.deleted_from_device_at is not None, (
                "delete issued before write-ahead audit committed"
            )
            super().delete_file(path)

    d = OrderCheckingDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.files_deleted == 1


def test_should_stop_bounds_shutdown_to_one_file(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 10, mtime=1.0)
    calls = iter([False, True])  # allow first file, then signal shutdown
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0,
                 should_stop=lambda: next(calls, True))
    assert s.files_copied == 1  # stopped between files, not mid-file
    assert s.result == "ok"


def test_dry_run_touches_nothing(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    s = run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_000.0)
    assert s.dry_run is True
    assert s.files_copied == 1  # planned counts, for visibility
    assert not archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()
    assert len(d.list_files()) == 1  # nothing deleted
