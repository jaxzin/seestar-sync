import pytest

from seestar_sync.device import DeviceUnavailable
from seestar_sync.smb_client import SmbDeviceClient


def client():
    return SmbDeviceClient.__new__(SmbDeviceClient)  # skip __init__ (no network)


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
    with pytest.raises(DeviceUnavailable):
        with c._map_errors():
            raise ConnectionResetError("gone")
    # FileNotFoundError must pass through untouched (stat contract)
    with pytest.raises(FileNotFoundError):
        with c._map_errors():
            raise FileNotFoundError("missing")


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
