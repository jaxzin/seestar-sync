"""Live smoke ring — requires a powered-on Seestar on the LAN.

Run: SEESTAR_LIVE_TEST=1 SEESTAR_HOST=<ip-or-hostname> uv run pytest tests/test_smb_live.py -v -s
Validates spec Open items: guest SMB auth on the S30 Pro, share layout,
and the FsFullSizeInformation (stat_volume) spike. Read-only: no deletes.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SEESTAR_LIVE_TEST") != "1" or not os.environ.get("SEESTAR_HOST"),
    reason="live test: set SEESTAR_LIVE_TEST=1 and SEESTAR_HOST",
)


@pytest.fixture(scope="module")
def device():
    from seestar_sync.smb_client import SmbDeviceClient

    return SmbDeviceClient(os.environ["SEESTAR_HOST"])


def test_connects_and_lists(device):
    files = device.list_files()
    assert isinstance(files, list)
    print(f"\n{len(files)} files on device; first 10:")
    for f in files[:10]:
        print(f"  {f.path}  {f.size:,}B")


def test_layout_matches_expectations(device):
    from seestar_sync.classify import classify_tier

    files = device.list_files()
    if not files:
        pytest.skip("device is empty — capture something first")
    tiers = {f.path: classify_tier(f.path) for f in files}
    classified = [p for p, t in tiers.items() if t is not None]
    print(f"\n{len(classified)}/{len(files)} files are deletion candidates")
    # every .jpg must be un-deletable
    for path, tier in tiers.items():
        if path.lower().endswith(".jpg"):
            assert tier is None, f"JPG classified as deletable: {path}"


def test_stat_volume_spike(device):
    """Spec Open item 1: does Seestar firmware answer FsFullSizeInformation?"""
    usage = device.disk_usage()
    print(f"\ntotal={usage.total_bytes:,} free={usage.free_bytes:,} "
          f"used={usage.used_percent:.1f}%")
    assert 0 < usage.total_bytes < 256 * 1024**3  # sane for a 128 GB device
    assert 0 <= usage.used_percent <= 100


def test_read_one_file_streams(device):
    files = [f for f in device.list_files() if f.size > 0]
    if not files:
        pytest.skip("device is empty")
    smallest = min(files, key=lambda f: f.size)
    with device.open_read(smallest.path) as r:
        data = r.read()
    assert len(data) == smallest.size
