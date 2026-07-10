from seestar_sync.models import DeviceFile
from seestar_sync.planner import plan_copies


def test_plan_skips_synced_and_active_and_orders_oldest_first():
    old = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit", 10, 100.0)
    new = DeviceFile("M 81_sub/Light_M 81_10.0s_IRCUT_20260101-000000.fit", 10, 200.0)
    synced = DeviceFile("IC 1318/DSO_Stacked_10_IC 1318_10.0s_20250101_000000.fit", 10, 50.0)
    live = DeviceFile("Sun/shot.avi", 10, 999.0)

    plan = plan_copies(
        [new, synced, old, live],
        is_synced=lambda f: f is synced,
        active={"Sun"},
    )
    assert plan == [old, new]  # synced skipped, active folder skipped, oldest first


def test_plan_copies_everything_including_jpgs():
    # copy scope is EVERYTHING; only deletion is tiered
    jpg = DeviceFile("M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509_thn.jpg", 5, 1.0)
    assert plan_copies([jpg], is_synced=lambda f: False, active=set()) == [jpg]
