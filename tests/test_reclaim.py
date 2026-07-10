from seestar_sync.models import DeviceFile, DiskUsage, Tier
from seestar_sync.reclaim import plan_reclaim


def sub(name, size, mtime=0.0):
    return DeviceFile(f"M 81_sub/{name}", size, mtime)


def test_no_reclaim_below_trigger():
    usage = DiskUsage(total_bytes=100, free_bytes=50)  # 50% used
    plan = plan_reclaim(
        [sub("Light_a_20250101-000000.fit", 10)], usage, 75, 60,
        is_verified=lambda f: True, active=set(),
    )
    assert plan == []


def test_reclaim_frees_down_to_target_with_hysteresis():
    # 90% used of 1000 => 900 used; target 60% => free 300 bytes worth
    usage = DiskUsage(total_bytes=1000, free_bytes=100)
    files = [sub(f"Light_x_2025010{i}-000000.fit", 100, mtime=i) for i in range(1, 8)]
    plan = plan_reclaim(files, usage, 75, 60, is_verified=lambda f: True, active=set())
    assert len(plan) == 3  # 300 bytes freed, then stop — no over-deleting
    # oldest sessions first
    assert [d.file.path for d in plan] == [f.path for f in files[:3]]


def test_tier_order_subs_then_videos_then_stacked():
    usage = DiskUsage(total_bytes=100, free_bytes=1)  # 99% used, target 60 => free 39
    stacked = DeviceFile("M 81/DSO_Stacked_1_M 81_10.0s_20200101_000000.fit", 13, 1.0)
    video = DeviceFile("Sun/clip_20200101-000000.avi", 13, 1.0)
    subframe = sub("Light_a_20260101-000000.fit", 13, 999.0)  # newest, but tier 1
    plan = plan_reclaim(
        [stacked, video, subframe], usage, 75, 60,
        is_verified=lambda f: True, active=set(),
    )
    assert [d.tier for d in plan] == [Tier.SUBS, Tier.VIDEOS, Tier.STACKED_FITS]


def test_hysteresis_spills_across_tiers_and_stops_at_target():
    # SDET-required boundary case: tier 1 exhausts mid-loop, reclaim spills
    # into tier 2, and stops at target WITHOUT touching tier 3
    usage = DiskUsage(total_bytes=1000, free_bytes=100)  # 90% used; target 60 → free 300
    subs = [sub(f"Light_a_2025010{i}-000000.fit", 100, mtime=float(i)) for i in (1, 2)]
    videos = [
        DeviceFile("Sun/clip_20250101-000000.avi", 100, 1.0),
        DeviceFile("Sun/clip_20250102-000000.avi", 100, 1.0),
    ]
    stacked = DeviceFile("M 81/DSO_Stacked_1_M 81_10.0s_20200101_000000.fit", 100, 1.0)
    plan = plan_reclaim(
        [*videos, stacked, *subs], usage, 75, 60,
        is_verified=lambda f: True, active=set(),
    )
    assert [d.tier for d in plan] == [Tier.SUBS, Tier.SUBS, Tier.VIDEOS]
    assert sum(d.file.size for d in plan) == 300  # stops at target, no over-delete


def test_unverified_active_and_non_candidates_are_untouchable():
    usage = DiskUsage(total_bytes=100, free_bytes=0)
    jpg = DeviceFile("M 81/DSO_Stacked_1_M 81_10.0s_20200101_000000.jpg", 50, 1.0)
    unverified = sub("Light_unverified_20200101-000000.fit", 50, 1.0)
    in_active = DeviceFile("IC 1318_sub/Light_b_20200101-000000.fit", 50, 1.0)
    plan = plan_reclaim(
        [jpg, unverified, in_active], usage, 75, 60,
        is_verified=lambda f: f is not unverified, active={"IC 1318_sub"},
    )
    assert plan == []  # nothing eligible even at 100% full
