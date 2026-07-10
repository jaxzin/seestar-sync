import pytest

from seestar_sync.device import DeviceUnavailable
from tests.fakes import FakeDevice


def test_fake_lists_stats_and_reads():
    d = FakeDevice(capacity=1000)
    d.put("M 81_sub/a.fit", b"12345", mtime=10.0)
    files = d.list_files()
    assert len(files) == 1 and files[0].size == 5
    assert d.stat("M 81_sub/a.fit").mtime == 10.0
    assert d.open_read("M 81_sub/a.fit").read() == b"12345"
    with pytest.raises(FileNotFoundError):
        d.stat("nope.fit")
    with pytest.raises(FileNotFoundError):
        d.open_read("nope.fit")  # same contract as production (app-side drift)


def test_fake_disk_usage_tracks_deletes():
    d = FakeDevice(capacity=100)
    d.put("a/x.fit", b"x" * 60, mtime=0.0)
    assert d.disk_usage().used_percent == 60.0
    d.delete_file("a/x.fit")
    assert d.disk_usage().used_percent == 0.0
    assert d.remove_dir_if_empty("a") is True


def test_fake_can_simulate_mid_run_disconnect():
    d = FakeDevice(capacity=100)
    d.put("a/x.fit", b"x", mtime=0.0)
    d.put("a/y.fit", b"y", mtime=0.0)
    d.fail_after_reads = 1
    d.open_read("a/x.fit")  # first read fine
    with pytest.raises(DeviceUnavailable):
        d.open_read("a/y.fit")


def test_fake_delete_contract_and_full_disconnect():
    # delete matches production (FileNotFoundError on missing); the
    # `disconnected` flag fails every op — needed for batch D's
    # audit-committed-but-delete-never-issued reconciliation test
    d = FakeDevice(capacity=100)
    d.put("a/x.fit", b"x", mtime=0.0)
    with pytest.raises(FileNotFoundError):
        d.delete_file("a/ghost.fit")
    d.disconnected = True
    for op in (
        lambda: d.delete_file("a/x.fit"),
        lambda: d.stat("a/x.fit"),
        lambda: d.list_files(),
        lambda: d.open_read("a/x.fit"),
        lambda: d.disk_usage(),
    ):
        with pytest.raises(DeviceUnavailable):
            op()
