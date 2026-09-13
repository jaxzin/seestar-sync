import datetime

import pytest

from seestar_sync.device import DeviceUnavailable
from seestar_sync.smb_client import SmbDeviceClient


def client():
    return SmbDeviceClient.__new__(SmbDeviceClient)  # skip __init__ (no network)


class _FakeDirInfo:
    """Mimics smbprotocol's SMBDirEntryInformation (size + tz-aware mtime)."""

    def __init__(self, size: int, mtime_epoch: float):
        self.end_of_file = size
        self.last_write_time = datetime.datetime.fromtimestamp(
            mtime_epoch, tz=datetime.UTC
        )


class _FakeStat:
    def __init__(self, size: int, mtime_epoch: float):
        self.st_size = size
        self.st_mtime = mtime_epoch


class _FakeEntry:
    """Mimics smbprotocol's SMBDirEntry. Tracks stat() calls so a test can prove
    the fast path never triggers a per-file SMB round-trip."""

    def __init__(self, name, is_dir, dir_info=None, stat_result=None):
        self.name = name
        self._is_dir = is_dir
        if dir_info is not None:
            self._dir_info = dir_info  # absent entirely when None (fallback case)
        self._stat_result = stat_result
        self.stat_calls = 0

    def is_dir(self):
        return self._is_dir

    def stat(self):
        self.stat_calls += 1
        return self._stat_result


def _fake_scandir(mapping):
    """Return a scandir(unc) that yields the entries mapped to each UNC."""
    return lambda unc: iter(mapping.get(unc, []))


def test_unc_path_building():
    c = client()
    c._host = "seestar.local"
    assert c._unc() == r"\\seestar.local\EMMC Images\MyWorks"
    assert (
        c._unc("M 81_sub/Light_a.fit")
        == r"\\seestar.local\EMMC Images\MyWorks\M 81_sub\Light_a.fit"
    )


def test_rel_path_from_unc():
    c = client()
    c._host = "seestar.local"
    unc = r"\\seestar.local\EMMC Images\MyWorks\M 81_sub\Light_a.fit"
    assert c._rel(unc) == "M 81_sub/Light_a.fit"


def test_unc_rejects_traversal():
    # the delete side of path containment: never build a UNC that escapes MyWorks
    c = client()
    c._host = "h"
    for evil in ("../x.fit", "a/../x.fit", "a\\x.fit", "/abs.fit", "a//b.fit"):
        with pytest.raises(ValueError, match="unsafe device path"):
            c._unc(evil)


def test_error_mapping_wraps_connection_failures():
    c = client()
    c._host = "seestar.local"
    with pytest.raises(DeviceUnavailable), c._map_errors():
        raise ConnectionResetError("gone")
    # smbprotocol wraps connect failures in ValueError — the routine
    # powered-off-telescope case must map to DeviceUnavailable, not crash
    with pytest.raises(DeviceUnavailable), c._map_errors():
        raise ValueError("Failed to connect to 'seestar.local:445': timed out")
    # FileNotFoundError must pass through untouched (stat contract)
    with pytest.raises(FileNotFoundError), c._map_errors():
        raise FileNotFoundError("missing")


def test_traversal_rejection_still_fails_loud():
    # the widened _map_errors tuple must NOT swallow _unc's containment
    # check — UNC paths are built before entering the mapping context
    c = client()
    c._host = "h"
    for op in (lambda: c.stat("../escape.fit"), lambda: c.delete_file("../escape.fit")):
        with pytest.raises(ValueError, match="unsafe device path"):
            op()


def test_disk_usage_fallback_uses_capacity(monkeypatch):
    c = client()
    c._host = "h"
    c._capacity_bytes = 1000
    monkeypatch.setattr(c, "_stat_volume", lambda: (_ for _ in ()).throw(OSError("no")))
    from seestar_sync.models import DeviceFile

    monkeypatch.setattr(c, "list_files", lambda: [DeviceFile("a/b.fit", 600, 0.0)])
    usage = c.disk_usage()
    assert usage.total_bytes == 1000 and usage.free_bytes == 400


def test_disk_usage_without_capacity_raises_free_space_unavailable(monkeypatch):
    # reachable device + unmeasurable space is NOT DeviceUnavailable — it
    # must surface as an alertable error, not read as a routine power-off
    from seestar_sync.device import FreeSpaceUnavailable

    c = client()
    c._host = "h"
    c._capacity_bytes = None
    monkeypatch.setattr(c, "_stat_volume", lambda: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(FreeSpaceUnavailable, match="DEVICE_CAPACITY_GB"):
        c.disk_usage()


def test_scan_reads_metadata_from_enumeration_not_stat(monkeypatch):
    # the perf fix: size+mtime come from the directory enumeration, so no
    # per-file stat() round-trip happens (that was the ~7-minute walk)
    from seestar_sync.models import DeviceFile

    c = client()
    c._host = "h"
    entry = _FakeEntry("a.fit", is_dir=False, dir_info=_FakeDirInfo(1234, 1782590487.22))
    monkeypatch.setattr(
        "seestar_sync.smb_client.smbclient.scandir",
        _fake_scandir({c._unc(): [entry]}),
    )
    out: list[DeviceFile] = []
    c._scan(c._unc(), "", out)
    assert len(out) == 1
    assert out[0].path == "a.fit" and out[0].size == 1234
    assert abs(out[0].mtime - 1782590487.22) < 1e-6
    assert entry.stat_calls == 0  # the whole point: zero per-file round-trips


def test_scan_falls_back_to_stat_when_dir_info_absent(monkeypatch):
    # defensive: a future smbprotocol without _dir_info stays correct via stat()
    c = client()
    c._host = "h"
    SmbDeviceClient._warned_slow_stat = False  # reset the process-wide warn-once
    entry = _FakeEntry("b.fit", is_dir=False, dir_info=None,
                       stat_result=_FakeStat(999, 42.0))
    monkeypatch.setattr(
        "seestar_sync.smb_client.smbclient.scandir",
        _fake_scandir({c._unc(): [entry]}),
    )
    out = []
    c._scan(c._unc(), "", out)
    assert out[0].size == 999 and out[0].mtime == 42.0
    assert entry.stat_calls == 1  # fallback used the (slow) stat() path


def test_scan_recurses_and_builds_relative_paths(monkeypatch):
    c = client()
    c._host = "h"
    sub = _FakeEntry("M 81_sub", is_dir=True)
    leaf = _FakeEntry("Light_a.fit", is_dir=False, dir_info=_FakeDirInfo(10, 100.0))
    monkeypatch.setattr(
        "seestar_sync.smb_client.smbclient.scandir",
        _fake_scandir({
            c._unc(): [sub],
            c._unc() + "\\M 81_sub": [leaf],
        }),
    )
    out = []
    c._scan(c._unc(), "", out)
    assert [f.path for f in out] == ["M 81_sub/Light_a.fit"]


def test_open_read_wraps_mid_stream_failures(monkeypatch):
    # the open is mapped; the READS must be too — a mid-transfer power-off
    # must surface as DeviceUnavailable, never a raw ConnectionResetError
    c = client()
    c._host = "h"

    class DyingRaw:
        def read(self, n=-1):
            raise ConnectionResetError("mid-read power-off")

        def close(self):
            pass

    monkeypatch.setattr(
        "seestar_sync.smb_client.smbclient.open_file", lambda *a, **k: DyingRaw()
    )
    reader = c.open_read("M 81_sub/a.fit")
    with pytest.raises(DeviceUnavailable):
        reader.read()
    reader.close()  # closing a dead handle must not raise
