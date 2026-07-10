"""One sync pass: probe -> inventory -> copy -> verify -> reclaim -> summary.

Ordering guarantees (spec §Reclaim, §Error handling):
- A file is only deleted if manifest-verified; preconditions re-checked at
  delete time against a fresh stat.
- The manifest audit row commits BEFORE the SMB delete (write-ahead): any
  interruption yields a false "deleted" (self-healing), never an unrecorded
  true deletion.
- DeviceUnavailable mid-run aborts cleanly, keeping banked progress.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .archive import Archive, VerifyError
from .classify import active_folders, top_folder
from .config import Config
from .device import DeviceClient, DeviceUnavailable, FreeSpaceUnavailable
from .manifest import Manifest
from .models import RunSummary, Tier
from .planner import plan_copies
from .reclaim import plan_reclaim

log = logging.getLogger(__name__)

MAX_VERIFY_FAILURES = 3


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync(
    device: DeviceClient,
    archive: Archive,
    manifest: Manifest,
    config: Config,
    now: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunSummary:
    """Execute one sync pass and return its summary.

    should_stop is a cooperative shutdown check, polled between files (copy
    phase) and between deletions (reclaim phase) — it never tears an
    in-flight transfer or splits the audit→delete pair.
    """
    now = time.time() if now is None else now
    should_stop = should_stop or (lambda: False)
    summary = RunSummary(started_at=iso(now), result="ok", dry_run=config.dry_run)

    try:
        files = device.list_files()
    except DeviceUnavailable as e:
        log.info("device unreachable, skipping run: %s", e)
        summary.result = "skipped_unreachable"
        return summary

    active = active_folders(files, now=now, window_minutes=config.active_window_minutes)
    summary.active_folders_skipped = sorted(active)
    if active:
        log.info("session guard: skipping active folders %s", sorted(active))

    device_gone = _copy_phase(
        device, archive, manifest, config, files, active, summary, now, should_stop
    )

    if not device_gone and not should_stop():
        _reclaim_phase(device, manifest, config, files, active, summary, now, should_stop)

    if summary.errors:
        summary.result = "partial"
    return summary


def _copy_phase(
    device, archive, manifest, config, files, active, summary, now, should_stop
) -> bool:
    """Returns True if the device vanished mid-phase."""
    is_synced = lambda f: manifest.verified_ok(f.path, f.size, f.mtime)  # noqa: E731
    for f in plan_copies(files, is_synced, active):
        if should_stop():
            log.info("shutdown requested; stopping copy phase between files")
            break
        if manifest.failure_count(f.path) >= MAX_VERIFY_FAILURES:
            msg = f"{f.path}: {MAX_VERIFY_FAILURES} consecutive verify failures, skipping"
            log.warning(msg)  # errors[] alone is invisible in container logs
            summary.errors.append(msg)
            continue
        if config.dry_run:
            log.info("DRY_RUN would copy %s (%d bytes)", f.path, f.size)
            summary.files_copied += 1
            summary.bytes_copied += f.size
            continue
        try:
            with device.open_read(f.path) as reader:
                sha = archive.write_stream(f.path, reader, f.size, f.mtime)
        except FileNotFoundError:
            log.debug("%s vanished before copy (app-side removal); normal drift", f.path)
            continue
        except DeviceUnavailable as e:
            log.info("device vanished mid-copy (%s), keeping progress", e)
            return True
        except VerifyError as e:
            count = manifest.bump_failure(f.path)
            msg = f"verify failed ({count}/{MAX_VERIFY_FAILURES}): {e}"
            log.warning(msg)  # errors[] alone is invisible in container logs
            summary.errors.append(msg)
            continue
        manifest.record_verified(f.path, f.size, f.mtime, sha, iso(now))
        manifest.clear_failure(f.path)
        summary.files_copied += 1
        summary.bytes_copied += f.size
        log.info("copied %s (%d bytes)", f.path, f.size)
    return False


def _reclaim_phase(
    device, manifest, config, files, active, summary, now, should_stop
) -> None:
    try:
        usage = device.disk_usage()
        summary.device_used_percent = round(usage.used_percent, 1)
        plan = plan_reclaim(
            files,
            usage,
            config.reclaim_trigger_percent,
            config.reclaim_target_percent,
            is_verified=lambda f: manifest.verified_ok(f.path, f.size, f.mtime),
            active=active,
        )
        if not plan:
            log.info(
                "device at %.1f%% used (trigger %.0f%%): no reclaim needed",
                usage.used_percent, config.reclaim_trigger_percent,
            )
        emptied_sub_folders: set[str] = set()
        for d in plan:
            if should_stop():
                log.info("shutdown requested; stopping reclaim between deletions")
                break
            if config.dry_run:
                log.info("DRY_RUN would delete [tier %d] %s (%d bytes)",
                         d.tier, d.file.path, d.file.size)
                summary.files_deleted += 1
                summary.bytes_reclaimed += d.file.size
                continue
            # Preconditions re-checked at delete time, not plan time:
            try:
                fresh = device.stat(d.file.path)
            except FileNotFoundError:
                continue  # e.g. phone app removed it; normal drift
            if not manifest.verified_ok(fresh.path, fresh.size, fresh.mtime):
                log.warning("skipping delete of %s: changed since verification", d.file.path)
                continue
            manifest.mark_deleted(d.file.path, int(d.tier), iso(now))  # write-ahead
            try:
                device.delete_file(d.file.path)
            except FileNotFoundError:
                # vanished between stat and delete (app-side drift); the
                # audit row stands — self-healing false-delete, don't count
                log.debug("%s vanished before delete; audit stands", d.file.path)
                continue
            summary.files_deleted += 1
            summary.bytes_reclaimed += d.file.size
            log.info("deleted [tier %d] %s (%d bytes)", d.tier, d.file.path, d.file.size)
            if d.tier == Tier.SUBS:
                emptied_sub_folders.add(top_folder(d.file.path))
        for folder in sorted(emptied_sub_folders):
            if device.remove_dir_if_empty(folder):
                log.info("removed empty folder %s", folder)
    except FreeSpaceUnavailable as e:
        # NOT a routine device-off: reclaim is blinded by config/firmware.
        # Must surface as partial so Kuma goes silent and the operator is
        # alerted — otherwise the device fills while monitoring stays green.
        msg = f"free space unmeasurable, reclaim skipped: {e}"
        log.error(msg)
        summary.errors.append(msg)
    except DeviceUnavailable as e:
        log.info("device vanished during reclaim (%s), stopping cleanly", e)
