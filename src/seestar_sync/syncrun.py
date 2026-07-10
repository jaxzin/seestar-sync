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
from .models import ProgressEvent, RunSummary, Tier
from .planner import plan_copies
from .reclaim import plan_reclaim

log = logging.getLogger(__name__)

MAX_VERIFY_FAILURES = 3

# Emit at most one intra-file progress event per this many bytes written, so a
# ~29 GB video produces hundreds of events, not tens of thousands. The MQTT
# consumer still throttles by wall-clock; this bounds event volume at the source.
_PROGRESS_COALESCE_BYTES = 64 * 1024 * 1024
# Operator-visible heartbeat cadence for an in-flight copy (seconds). Sits just
# above the MqttReporter's 10 s throttle so the two do not oscillate.
_COPY_HEARTBEAT_SECONDS = 15.0


def _heartbeat_line(path: str, written: int, size: int) -> str:
    """Operator log line for an in-flight copy. size==0 can't happen in practice
    (a 0-byte file never enters the write loop), but the guard keeps it total."""
    pct = int(100 * written / size) if size else 100
    return f"copying {path}: {written / 1e9:.1f}/{size / 1e9:.1f} GB ({pct}%)"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync(
    device: DeviceClient,
    archive: Archive,
    manifest: Manifest,
    config: Config,
    now: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    observer: Callable[[ProgressEvent], None] | None = None,
    on_summary: Callable[[RunSummary], None] | None = None,
) -> RunSummary:
    """Execute one sync pass and return its summary.

    should_stop is a cooperative shutdown check, polled between files (copy
    phase) and between deletions (reclaim phase) — it never tears an
    in-flight transfer or splits the audit→delete pair.

    observer, if given, is invoked synchronously with ProgressEvent updates
    (see models.ProgressEvent). Consumers own throttling and must never
    raise — run_sync does NOT guard observer callbacks.
    """
    now = time.time() if now is None else now
    should_stop = should_stop or (lambda: False)
    notify = observer or (lambda e: None)
    # in-pass summary snapshots so "Bytes/Files copied" climb live; consumer
    # throttles and must not raise (same contract as observer)
    report_summary = on_summary or (lambda s: None)
    summary = RunSummary(
        started_at=iso(now), result="ok",
        dry_run=config.dry_run, reclaim_enabled=config.reclaim_enabled,
    )

    notify(ProgressEvent(phase="listing"))
    listing_started = time.monotonic()
    try:
        files = device.list_files()
    except DeviceUnavailable as e:
        log.info("device unreachable, skipping run: %s", e)
        summary.result = "skipped_unreachable"
        notify(ProgressEvent(phase="done"))
        return summary
    log.info("listed %d files in %.1fs", len(files), time.monotonic() - listing_started)

    active = active_folders(files, now=now, window_minutes=config.active_window_minutes)
    summary.active_folders_skipped = sorted(active)
    if active:
        log.info("session guard: skipping active folders %s", sorted(active))

    device_gone = _copy_phase(
        device, archive, manifest, config, files, active, summary, now, should_stop,
        notify, report_summary
    )

    if not device_gone and not should_stop():
        if config.reclaim_enabled:
            _reclaim_phase(
                device, manifest, config, files, active, summary, now, should_stop,
                notify, report_summary
            )
        else:
            # deletion is opt-in (user-ratified): copy-only mode still
            # reports fill level best-effort so monitoring sees the device
            _report_usage_only(device, summary)
            log.info("reclaim disabled (RECLAIM_ENABLED=false): copy-only mode")

    if summary.errors:
        summary.result = "partial"
    notify(ProgressEvent(
        phase="done",
        files_done=summary.files_copied,
        bytes_done=summary.bytes_copied,
        files_total=summary.files_copied,
        bytes_total=summary.bytes_copied,
    ))
    return summary


def _copy_phase(
    device, archive, manifest, config, files, active, summary, now, should_stop,
    notify, report_summary
) -> bool:
    """Returns True if the device vanished mid-phase."""
    is_synced = lambda f: manifest.verified_ok(f.path, f.size, f.mtime)  # noqa: E731
    plan = plan_copies(files, is_synced, active)
    files_total = len(plan)
    bytes_total = sum(f.size for f in plan)
    files_done = 0
    bytes_done = 0
    if plan:  # idle passes emit only listing/done — no "copying 0/0" state blip
        notify(ProgressEvent(phase="copying", files_total=files_total,
                             bytes_total=bytes_total))
    for f in plan:
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
            files_done += 1
            bytes_done += f.size
            notify(ProgressEvent(phase="copying", files_done=files_done,
                                 files_total=files_total, bytes_done=bytes_done,
                                 bytes_total=bytes_total, current_path=f.path))
            report_summary(summary)
            continue
        log.info("copying %s (%d bytes)", f.path, f.size)
        base = bytes_done  # bytes from files already completed this pass
        emit_state = {"last": base, "hb": time.monotonic()}

        def _tick(written: int, _f=f) -> None:
            # This runs inside write_stream's teardown-on-exception scope, where a
            # raise would unlink the .part and abort the transfer. Telemetry must
            # never corrupt a copy, so swallow any reporter/observer fault here.
            try:
                cum = base + written
                if cum - emit_state["last"] >= _PROGRESS_COALESCE_BYTES:
                    emit_state["last"] = cum
                    notify(ProgressEvent(
                        phase="copying", files_done=files_done, files_total=files_total,
                        bytes_done=cum, bytes_total=bytes_total, current_path=_f.path))
                t = time.monotonic()
                if written < _f.size and t - emit_state["hb"] >= _COPY_HEARTBEAT_SECONDS:
                    emit_state["hb"] = t
                    log.info("%s", _heartbeat_line(_f.path, written, _f.size))
            except Exception:  # noqa: BLE001 — telemetry must not fail a copy
                log.debug("progress reporting raised; copy continues", exc_info=True)

        try:
            with device.open_read(f.path) as reader:
                sha = archive.write_stream(f.path, reader, f.size, f.mtime, on_bytes=_tick)
        except FileNotFoundError:
            # INFO (not debug) so the "copying %s" start line above always has a
            # visible outcome; app-side deletion between inventory and copy is drift.
            log.info("%s vanished before copy (app-side removal); skipping", f.path)
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
        files_done += 1
        bytes_done += f.size
        notify(ProgressEvent(phase="copying", files_done=files_done,
                             files_total=files_total, bytes_done=bytes_done,
                             bytes_total=bytes_total, current_path=f.path))
        report_summary(summary)
        log.info("copied %s (%d bytes)", f.path, f.size)
    return False


def _report_usage_only(device, summary) -> None:
    """Best-effort fill level for monitoring; never fails a copy-only run."""
    try:
        summary.device_used_percent = round(device.disk_usage().used_percent, 1)
    except (DeviceUnavailable, FreeSpaceUnavailable) as e:
        log.debug("fill level unavailable in copy-only mode: %s", e)


def _reclaim_phase(
    device, manifest, config, files, active, summary, now, should_stop,
    notify, report_summary
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
        files_total = len(plan)
        bytes_total = sum(d.file.size for d in plan)
        files_done = 0
        bytes_done = 0
        if plan:
            notify(ProgressEvent(phase="reclaiming", files_total=files_total,
                                 bytes_total=bytes_total))
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
                files_done += 1
                bytes_done += d.file.size
                notify(ProgressEvent(phase="reclaiming", files_done=files_done,
                                     files_total=files_total, bytes_done=bytes_done,
                                     bytes_total=bytes_total, current_path=d.file.path))
                report_summary(summary)
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
            files_done += 1
            bytes_done += d.file.size
            notify(ProgressEvent(phase="reclaiming", files_done=files_done,
                                 files_total=files_total, bytes_done=bytes_done,
                                 bytes_total=bytes_total, current_path=d.file.path))
            report_summary(summary)
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
