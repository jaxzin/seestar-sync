import pytest

from seestar_sync import syncrun
from seestar_sync.archive import Archive
from seestar_sync.config import Config
from seestar_sync.device import DeviceUnavailable
from seestar_sync.manifest import Manifest
from seestar_sync.models import DeviceFile
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
    # most tests exercise the reclaim engine, so enable it here; the opt-in
    # default (reclaim_enabled=False) is pinned by its own dedicated test
    env_vars = {"SEESTAR_HOST": "h", "RECLAIM_ENABLED": "true"}
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
    assert s.bytes_copied == 303  # 300 (raw) + 3 (jpg) — exact-content bar
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
    assert s1.bytes_reclaimed == 800
    assert s1.device_used_percent == 0.0  # re-measured AFTER deletions (device now empty)
    assert d.list_files() == []  # gone from device
    row = manifest.get("M 81_sub/Light_a_20250101-000000.fit")
    assert row.deleted_from_device_at is not None and row.deleted_tier == 1
    # NAS copy retained
    assert archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()


def test_reclaim_remeasures_fill_after_deletions(env):
    # the reported fill must reflect freed space, not the pre-reclaim reading
    # the planner used — otherwise the HA fill sensor lags a whole reclaim
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)  # tier-1, deletable
    d.put("M 81/DSO_Stacked_1_M 81_10.0s_20250101_000000.jpg", b"j" * 100, mtime=1.0)  # kept
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.files_deleted == 1 and s.bytes_reclaimed == 800  # only the sub
    # pre-deletion fill was 90% (900/1000); after deleting the 800-byte sub the
    # retained 100-byte JPG leaves the device at 10% — that post value is reported
    assert s.device_used_percent == 10.0
    assert s.device_used_percent != 90.0  # not the stale pre-reclaim reading


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


def test_mid_read_disconnect_banks_progress(env):
    # the open succeeds, the STREAM dies (canonical mid-transfer power-off):
    # must abort cleanly with banked progress and no .part debris, not crash
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 10, mtime=1.0)
    d.fail_mid_read_paths.add("B_sub/Light_b_20250102-000000.fit")
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.result == "ok"
    assert s.files_copied == 1  # A banked before B's stream died
    assert not archive.dest_path("B_sub/Light_b_20250102-000000.fit").exists()
    assert not list((archive.root / "MyWorks" / "B_sub").glob("*.part"))


def test_unmeasurable_free_space_surfaces_as_error(env):
    # a blinded reclaim must alert (partial → Kuma silence), never report ok
    archive, manifest = env
    from seestar_sync.device import FreeSpaceUnavailable

    class BlindDevice(FakeDevice):
        def disk_usage(self):
            raise FreeSpaceUnavailable("stat_volume unsupported, DEVICE_CAPACITY_GB unset")

    d = BlindDevice(capacity=100_000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 10, mtime=1.0)
    s = run_sync(d, archive, manifest, config(), now=1_000_000.0)
    assert s.result == "partial"
    assert s.files_copied == 1  # copying still works; only reclaim is blinded
    assert any("free space" in e for e in s.errors)


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


def test_reclaim_disabled_by_default_is_copy_only(env):
    # user-ratified: deletion is opt-in. A DEFAULT config under storage
    # pressure with verified copies must still never delete anything.
    archive, manifest = env
    cfg = Config.from_env({"SEESTAR_HOST": "h"})
    assert cfg.reclaim_enabled is False
    d = FakeDevice(capacity=1000)
    path = "M 81_sub/Light_a_20250101-000000.fit"
    d.put(path, b"x" * 800, mtime=1.0)  # 80% used — past the trigger
    s = run_sync(d, archive, manifest, cfg, now=1_000_000.0)
    assert s.files_copied == 1  # copying proceeds normally
    assert s.files_deleted == 0
    assert len(d.list_files()) == 1  # nothing deleted
    assert s.device_used_percent == 80.0  # fill level still reported
    assert s.result == "ok"
    assert s.reclaim_enabled is False  # machine-readable copy-only signal
    assert s.to_json()["reclaim_enabled"] is False  # reaches status.json/HA


def test_dry_run_touches_nothing(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    s = run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_000.0)
    assert s.dry_run is True
    assert s.files_copied == 1  # planned counts, for visibility
    assert not archive.dest_path("M 81_sub/Light_a_20250101-000000.fit").exists()
    assert len(d.list_files()) == 1  # nothing deleted


def test_dry_run_reclaim_reports_plan_without_touching_anything(env):
    # the rollout graduation gate: the operator eyeballs dry-run DELETION
    # plans before enabling reclaim — this branch must report accurately
    # while executing nothing (including no audit rows)
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    path = "M 81_sub/Light_a_20250101-000000.fit"
    d.put(path, b"x" * 8000, mtime=1.0)
    run_sync(d, archive, manifest, config(), now=1_000_000.0)  # copy + verify
    d.capacity = 10_000  # now 80% used → reclaim pressure
    obs = RecordingObserver()
    s = run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_100.0,
                 observer=obs)
    assert s.dry_run is True
    assert s.files_deleted == 1 and s.bytes_reclaimed == 8000  # plan reported
    assert len(d.list_files()) == 1  # nothing actually deleted
    assert manifest.get(path).deleted_from_device_at is None  # no audit row
    # dashboard-visible: the dry-run deletion plan reaches observers too
    assert [e for e in obs.events if e.phase == "reclaiming"]


def test_changed_file_is_skipped_at_delete_time(env):
    # spec hard precondition: fresh stat must still match what was verified —
    # and the skip must happen BEFORE mark_deleted writes an audit row
    archive, manifest = env

    class DriftingDevice(FakeDevice):
        def stat(self, path):
            f = super().stat(path)
            return DeviceFile(f.path, f.size, f.mtime + 100.0)  # device rewrote it

    d = DriftingDevice(capacity=100_000)
    path = "M 81_sub/Light_a_20250101-000000.fit"
    d.put(path, b"x" * 8000, mtime=1.0)
    run_sync(d, archive, manifest, config(), now=1_000_000.0)
    d.capacity = 10_000
    s = run_sync(d, archive, manifest, config(), now=1_000_100.0)
    assert s.files_deleted == 0
    assert len(d.list_files()) == 1  # file survives
    assert manifest.get(path).deleted_from_device_at is None  # no audit row


class RecordingObserver:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def phases(self):
        return [e.phase for e in self.events]


def test_observer_sequence_for_copy_pass(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 20, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases()[0] == "listing"
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1  # exactly once, per contract
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[0].files_total == 2 and copying[0].bytes_total == 30
    assert copying[0].files_done == 0  # the plan-known announcement
    assert copying[-1].files_done == 2 and copying[-1].bytes_done == 30
    assert copying[1].current_path == "A_sub/Light_a_20250101-000000.fit"


def test_intrafile_progress_emits_growing_bytes_done(env, monkeypatch):
    # a single large file must produce multiple mid-file progress events with a
    # monotonically climbing bytes_done, not one jump at end-of-file. Shrink the
    # coalesce threshold so the test needn't allocate 64 MiB+.
    monkeypatch.setattr(syncrun, "_PROGRESS_COALESCE_BYTES", 2 * 1024 * 1024)
    size = 9 * 1024 * 1024  # 9 chunks; crosses the 2 MiB threshold several times
    archive, manifest = env
    d = FakeDevice(capacity=10_000_000_000)  # roomy: no reclaim pressure
    d.put("Solar_video/big.avi", b"z" * size, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)

    copying = [e for e in obs.events
               if e.phase == "copying" and e.current_path == "Solar_video/big.avi"]
    mid = [e for e in copying if e.bytes_done < size]  # strictly-partial emits
    # 9 MiB file, 2 MiB coalesce threshold -> emits cross at 2/4/6/8 MiB = exactly
    # 4 partial events. Exact count catches coalescing being removed (would give 8)
    # or its threshold drifting, not just "no mid-file emits at all".
    assert len(mid) == 4, f"expected 4 coalesced intra-file emits, got {len(mid)}"
    seq = [e.bytes_done for e in copying]
    assert seq == sorted(seq)  # monotonic non-decreasing across the whole copy
    # files_done stays 0 until the file completes; the final copying event is the
    # post-copy one (bytes fully banked, file counted)
    assert all(e.files_done == 0 for e in mid)
    assert copying[-1].bytes_done == size and copying[-1].files_done == 1


def test_intrafile_progress_carries_bytes_across_files(env, monkeypatch):
    # the feature's core promise: bytes_done = completed files + partial. For the
    # second file, mid-copy emits must include the first file's bytes (base
    # snapshot), not restart from 0 — a dropped base would make the HA bar jump
    # backward at each file boundary.
    monkeypatch.setattr(syncrun, "_PROGRESS_COALESCE_BYTES", 2 * 1024 * 1024)
    mib = 1024 * 1024
    archive, manifest = env
    d = FakeDevice(capacity=10_000_000_000)  # roomy: no reclaim pressure
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * (5 * mib), mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * (5 * mib), mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)

    copying = [e for e in obs.events if e.phase == "copying"]
    seq = [e.bytes_done for e in copying]
    assert seq == sorted(seq)  # monotonic across the file boundary
    # the second file (files_done == 1) has mid-copy emits above the 5 MiB base
    second = [e for e in copying
              if e.files_done == 1 and 5 * mib < e.bytes_done < 10 * mib]
    assert second, "second file's emits did not carry the first file's bytes"


def test_observer_exception_never_aborts_the_copy(env, monkeypatch):
    # _tick runs inside write_stream's teardown-on-exception scope; a raising
    # observer must NOT unlink the .part or abort the transfer. Only the guarded
    # intra-file emits raise here (files_done == 0, current_path set) — the
    # unguarded completion/listing/done sites are left alone so the pass survives.
    monkeypatch.setattr(syncrun, "_PROGRESS_COALESCE_BYTES", 2 * 1024 * 1024)
    archive, manifest = env
    d = FakeDevice(capacity=10_000_000_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * (5 * 1024 * 1024), mtime=1.0)

    def boom(event):
        if event.current_path is not None and event.files_done == 0:
            raise RuntimeError("observer is broken mid-file")

    s = run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=boom)
    assert s.result == "ok"
    assert s.files_copied == 1
    assert archive.dest_path("A_sub/Light_a_20250101-000000.fit").exists()
    assert not list((archive.root / "MyWorks" / "A_sub").glob("*.part"))


def test_on_summary_reports_running_totals_during_copy(env):
    # the summary snapshot climbs live per completed file, so HA's Bytes/Files
    # copied sensors move during a pass instead of staying Unknown until the end
    archive, manifest = env
    d = FakeDevice(capacity=10_000_000)  # roomy: no reclaim pressure
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 100, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 200, mtime=1.0)
    snaps: list[tuple[int, int]] = []
    run_sync(d, archive, manifest, config(), now=1_000_000.0,
             on_summary=lambda s: snaps.append((s.files_copied, s.bytes_copied)))
    assert [n for n, _ in snaps] == [1, 2]  # files_copied climbs one per file
    assert snaps[-1] == (2, 300)  # final running total = both files
    assert snaps[0][1] < snaps[1][1]  # bytes_copied strictly climbs


def test_on_summary_climbs_through_a_large_file(env, monkeypatch):
    # Bytes copied must climb continuously THROUGH a big file (including the
    # in-flight partial), not jump only at file completion — the whole point of
    # the fix for the 28 GB backlog video.
    monkeypatch.setattr(syncrun, "_PROGRESS_COALESCE_BYTES", 2 * 1024 * 1024)
    size = 9 * 1024 * 1024
    archive, manifest = env
    d = FakeDevice(capacity=10_000_000_000)
    d.put("Solar_video/big.avi", b"z" * size, mtime=1.0)
    snaps: list[int] = []
    run_sync(d, archive, manifest, config(), now=1_000_000.0,
             on_summary=lambda s: snaps.append(s.bytes_copied))
    partial = [b for b in snaps if b < size]  # mid-file, in-flight snapshots
    assert len(partial) >= 3, f"expected intra-file summary updates, got {snaps}"
    assert snaps == sorted(snaps)  # monotonic climb
    assert snaps[-1] == size  # finalizes at the true completed total


def test_bytes_copied_climbs_during_a_large_file(env, monkeypatch):
    # a multi-GB file must not freeze "Bytes copied" until it completes: the
    # in-flight total is reported mid-file (files_copied still 0), so the HA
    # sensor climbs continuously through the file, not just per completed file
    import seestar_sync.syncrun as sr

    monkeypatch.setattr(sr, "_PROGRESS_COALESCE_BYTES", 1)  # emit on every 1 MB chunk
    archive, manifest = env
    d = FakeDevice(capacity=100_000_000)
    d.put("Solar_video/big.avi", b"x" * (3 * 1024 * 1024), mtime=1.0)  # 3 chunks
    snaps: list[tuple[int, int]] = []
    run_sync(d, archive, manifest, config(), now=1_000_000.0,
             on_summary=lambda s: snaps.append((s.files_copied, s.bytes_copied)))
    mid = [(fc, bc) for fc, bc in snaps if fc == 0]  # in-flight (file not yet done)
    assert len(mid) >= 2  # bytes_copied moved DURING the file
    assert [bc for _, bc in mid] == sorted(bc for _, bc in mid)  # monotonic climb
    assert snaps[-1] == (1, 3 * 1024 * 1024)  # finalizes to the completed file


def test_heartbeat_line_formats_and_guards_zero_size():
    from seestar_sync.syncrun import _heartbeat_line

    line = _heartbeat_line("Solar_video/big.avi", 2_100_000_000, 28_800_000_000)
    assert "Solar_video/big.avi" in line
    assert "2.1/28.8 GB" in line
    assert "(7%)" in line
    # zero-size can't occur in practice but the guard must not divide by zero
    assert "(100%)" in _heartbeat_line("x", 0, 0)


def test_copy_logs_start_and_completion_lines(env, caplog):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("Solar_video/big.avi", b"z" * (5 * 1024 * 1024), mtime=1.0)
    with caplog.at_level("INFO"):
        run_sync(d, archive, manifest, config(), now=1_000_000.0)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("copying Solar_video/big.avi (" in m for m in msgs)  # start line
    assert any("copied Solar_video/big.avi (" in m for m in msgs)  # completion line


def test_observer_sequence_for_reclaim_and_done_totals(env):
    archive, manifest = env
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/Light_a_20250101-000000.fit", b"x" * 800, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    reclaiming = [e for e in obs.events if e.phase == "reclaiming"]
    assert reclaiming[0].files_total == 1 and reclaiming[0].files_done == 0
    assert reclaiming[-1].files_done == 1 and reclaiming[-1].bytes_done == 800
    done = obs.events[-1]
    assert done.phase == "done" and done.files_done == 1  # copies in done event
    assert obs.phases().count("done") == 1


def test_observer_unreachable_pass_emits_listing_then_done(env):
    archive, manifest = env

    class Dead(FakeDevice):
        def list_files(self):
            raise DeviceUnavailable("off")

    obs = RecordingObserver()
    run_sync(Dead(), archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases() == ["listing", "done"]  # exact — implies done once


def test_observer_mid_run_disconnect_still_emits_done(env):
    # done must close EVERY pass — a power-off mid-copy can't strand the
    # consumer's state topic in "copying" until the next pass
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    d.put("B_sub/Light_b_20250102-000000.fit", b"b" * 20, mtime=1.0)
    d.fail_after_reads = 1
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_000.0, observer=obs)
    assert obs.phases()[0] == "listing"
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[-1].files_done == 1  # banked progress visible
    assert copying[-1].current_path == "A_sub/Light_a_20250101-000000.fit"
    assert obs.events[-1].files_done == 1  # done carries banked totals


def test_observer_idle_pass_emits_only_listing_and_done(env):
    # the most common production pass (nothing new, every 15 min) must not
    # blip HA's state topic through "copying"
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    run_sync(d, archive, manifest, config(), now=1_000_000.0)  # syncs everything
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(), now=1_000_100.0, observer=obs)
    assert obs.phases() == ["listing", "done"]


def test_observer_dry_run_emits_planned_counts(env):
    archive, manifest = env
    d = FakeDevice(capacity=100_000)
    d.put("A_sub/Light_a_20250101-000000.fit", b"a" * 10, mtime=1.0)
    obs = RecordingObserver()
    run_sync(d, archive, manifest, config(DRY_RUN="true"), now=1_000_000.0, observer=obs)
    copying = [e for e in obs.events if e.phase == "copying"]
    assert copying[-1].files_done == 1  # planned count, same shape as real
    assert obs.phases()[-1] == "done"
    assert obs.phases().count("done") == 1


def test_disconnect_during_delete_leaves_self_healing_audit(env):
    # the real crash window (not forged state): device dies between the
    # write-ahead audit commit and the SMB delete → clean abort, audit
    # stands, file survives — exactly the self-healing false-delete state
    archive, manifest = env

    class DisconnectingOnDelete(FakeDevice):
        def delete_file(self, path):
            raise DeviceUnavailable("power-off mid-delete")

    d = DisconnectingOnDelete(capacity=100_000)
    path = "M 81_sub/Light_a_20250101-000000.fit"
    d.put(path, b"x" * 8000, mtime=1.0)
    run_sync(d, archive, manifest, config(), now=1_000_000.0)
    d.capacity = 10_000
    s = run_sync(d, archive, manifest, config(), now=1_000_100.0)
    assert s.result == "ok" and s.errors == []  # clean abort, not an error
    assert s.files_deleted == 0
    assert manifest.get(path).deleted_from_device_at is not None  # audit stands
    assert len(d.list_files()) == 1  # file still on device
