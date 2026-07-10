from seestar_sync.classify import (
    active_folders,
    classify_tier,
    filename_timestamp,
    session_key,
    top_folder,
)
from seestar_sync.models import DeviceFile, DiskUsage, Tier


def test_top_folder_handles_spaces():
    assert top_folder("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit") == "M 81_sub"
    assert top_folder("Sun/solar_video.avi") == "Sun"


def test_tier_allowlist():
    # Tier 1: FITs inside *_sub folders
    assert classify_tier("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit") == Tier.SUBS
    # Tier 2: videos anywhere
    assert classify_tier("Sun/Sun_20260101-102030.avi") == Tier.VIDEOS
    assert classify_tier("Lunar/Moon_20260101-102030.mp4") == Tier.VIDEOS
    # Tier 3: stacked FITs in target folders (both firmware naming generations)
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.fit") == Tier.STACKED_FITS
    assert classify_tier("M 81/Stacked_119_M 81_10.0s_20250607_221746.fit") == Tier.STACKED_FITS


def test_video_in_target_folder_is_tier2():
    # Conscious deviation from spec's "solar-system folders" (architect-ratified):
    # an extension allowlist avoids brittle folder-name coupling. Pin it so
    # nobody "fixes" it backwards.
    assert classify_tier("M 81/mosaic.mp4") == Tier.VIDEOS
    assert classify_tier("M 81_sub/clip.avi") is None  # _sub still admits only .fit
    assert DiskUsage(total_bytes=0, free_bytes=0).used_percent == 0.0  # fail-safe


def test_non_candidates_are_never_deletable():
    # JPGs and thumbnails: structurally unreachable
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.jpg") is None
    assert classify_tier("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509_thn.jpg") is None
    # non-stacked FIT in a target folder (unknown future naming): fail safe
    assert classify_tier("M 81/Mystery_M 81_20260311_213509.fit") is None
    # unknown extensions
    assert classify_tier("M 81/notes.txt") is None
    # non-FIT junk inside a _sub folder
    assert classify_tier("M 81_sub/thumbs.db") is None


def test_filename_timestamp_parses_both_separators():
    # Light frames use YYYYMMDD-HHMMSS
    ts1 = filename_timestamp("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit")
    # Stacked files use YYYYMMDD_HHMMSS
    ts2 = filename_timestamp("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.fit")
    assert ts1 is not None and ts2 is not None
    assert ts2 > ts1  # 2026 > 2025
    assert filename_timestamp("M 81/no_date_here.fit") is None


def test_session_key_falls_back_to_mtime():
    dated = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit", 1, mtime=999.0)
    undated = DeviceFile("M 81_sub/oddball.fit", 1, mtime=123.0)
    assert session_key(dated) != 999.0  # parsed from name, not mtime
    assert session_key(undated) == 123.0


def test_active_folders_is_per_folder():
    now = 10_000.0
    files = [
        DeviceFile("M 81_sub/a.fit", 1, mtime=now - 60),        # active (1 min ago)
        DeviceFile("IC 1318_sub/b.fit", 1, mtime=now - 7200),   # idle (2 h ago)
    ]
    assert active_folders(files, now=now, window_minutes=15) == {"M 81_sub"}


def test_disk_usage_percent():
    u = DiskUsage(total_bytes=1000, free_bytes=250)
    assert u.used_percent == 75.0
