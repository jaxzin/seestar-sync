"""CLI: daemon | run | status | rebuild-manifest. Exit codes per spec:
0 ok/skipped_unreachable, 1 run-with-errors, 2 fatal (config/archive)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping

from .archive import Archive, ArchiveError
from .config import Config, ConfigError
from .device import DeviceClient
from .manifest import Manifest
from .reporters import report_all
from .settings import OVERRIDE_KEYS, SettingsStore, resolve_effective
from .syncrun import iso, run_sync

log = logging.getLogger("seestar_sync")

_HASH_CHUNK_SIZE = 1024 * 1024  # matches archive.py's write/verify chunk size


def _default_device_factory(config: Config) -> DeviceClient:
    from .smb_client import SmbDeviceClient  # deferred: smbprotocol import is slow

    return SmbDeviceClient(config.seestar_host, capacity_gb=config.device_capacity_gb)


def _prepare(config: Config, init: bool) -> Archive:
    archive = Archive(config.archive_dir)
    if init:
        archive.init(allow_root_fs=config.archive_on_root_fs_ok)
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    return archive


def _one_run(config: Config, archive: Archive, device_factory,
             should_stop=None, mqtt=None) -> int:
    # The mount-drop trap is a MID-LIFE event, not just a startup one: in
    # daemon mode the archive volume can vanish between scheduled passes.
    # Re-check before every pass — otherwise Manifest.__init__'s mkdir would
    # silently rebuild a phantom archive on the container's own layer and
    # reclaim would delete device originals against it. ArchiveError
    # propagates to main's handler → exit 2 (crash-loud; the restart loop +
    # Kuma silence alert the operator).
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    store = SettingsStore(archive.meta_dir / "settings.json")
    effective, sources = resolve_effective(config, store)
    for key in OVERRIDE_KEYS:
        if sources[key] == "mqtt override":
            log.warning("effective %s comes from MQTT override", key)
    from .device import DeviceUnavailable

    observer = mqtt.handle_progress if mqtt is not None else None
    try:
        device = device_factory(effective)
    except DeviceUnavailable:
        from .models import RunSummary

        summary = RunSummary(started_at=iso(time.time()), result="skipped_unreachable",
                             dry_run=effective.dry_run,
                             reclaim_enabled=effective.reclaim_enabled)
        report_all(effective, archive.meta_dir, summary)
        if mqtt is not None:
            mqtt.publish_summary(summary, effective, sources)
        return 0
    with Manifest(archive.meta_dir / "manifest.db") as manifest:
        summary = run_sync(device, archive, manifest, effective,
                           should_stop=should_stop, observer=observer)
    report_all(effective, archive.meta_dir, summary)
    if mqtt is not None:
        mqtt.publish_summary(summary, effective, sources)
    return 0 if summary.result in ("ok", "skipped_unreachable") else 1


def _cmd_run(config: Config, args, device_factory) -> int:
    archive = _prepare(config, args.init)
    return _one_run(config, archive, device_factory)


def _cmd_daemon(config: Config, args, device_factory) -> int:
    archive = _prepare(config, args.init)
    interval = config.sync_interval_minutes * 60
    stop = threading.Event()

    def _handle_signal(signum, frame):
        log.info("received signal %d; finishing current file, then exiting", signum)
        stop.set()

    # The container runs this as PID 1: without a handler, docker stop's
    # SIGTERM would be ignored and Docker would SIGKILL after the grace
    # period. A mid-copy SIGKILL is safe (.part discipline) but graceless.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("daemon: syncing %s every %d min", config.seestar_host,
             config.sync_interval_minutes)

    from .mqtt import MqttReporter

    store = SettingsStore(archive.meta_dir / "settings.json")
    mqtt = MqttReporter(config, store) if config.mqtt_host else None
    if mqtt is not None:
        mqtt.start()
    try:
        while not stop.is_set():
            # stop.is_set threads through run_sync as should_stop, so a signal
            # mid-run ends the sync bounded by one file, not one full run
            code = _one_run(config, archive, device_factory,
                            should_stop=stop.is_set, mqtt=mqtt)
            if code != 0:
                log.warning("run finished with errors (exit %d); continuing", code)
            stop.wait(interval)  # interruptible, unlike time.sleep
    finally:
        # must run even when ArchiveError propagates out of a pass — LWT
        # otherwise leaves HA showing the dead daemon online forever
        if mqtt is not None:
            mqtt.stop()
    return 0


def _cmd_status(config: Config, args, device_factory) -> int:
    status_path = config.archive_dir / ".seestar-sync" / "status.json"
    if not status_path.exists():
        print("no status yet — has a sync run completed?")
        return 0
    data = json.loads(status_path.read_text())
    print(f"last run: {data['last_run']}")
    mode = ("  (dry run)" if data.get("dry_run") else "") + (
        "  (copy-only)" if not data.get("reclaim_enabled") else ""
    )
    print(f"result: {data['result']}{mode}")
    print(f"copied: {data['files_copied']} files / {data['bytes_copied']:,} bytes")
    print(f"reclaimed: {data['files_deleted']} files / {data['bytes_reclaimed']:,} bytes")
    if data.get("device_used_percent") is not None:
        print(f"device used: {data['device_used_percent']}%")
    if data.get("active_folders_skipped"):
        print(f"active folders skipped: {', '.join(data['active_folders_skipped'])}")
    for err in data.get("errors", []):
        print(f"error: {err}")
    store = SettingsStore(config.archive_dir / ".seestar-sync" / "settings.json")
    effective, sources = resolve_effective(config, store)
    print("settings:")
    for key in OVERRIDE_KEYS:
        print(f"  {key}: {getattr(effective, key)} ({sources[key]})")
    return 0


def _cmd_rebuild_manifest(config: Config, args, device_factory) -> int:
    archive = Archive(config.archive_dir)
    archive.check_ready(allow_root_fs=config.archive_on_root_fs_ok)
    myworks = config.archive_dir / "MyWorks"
    now = iso(time.time())
    count = 0
    with Manifest(archive.meta_dir / "manifest.db") as manifest:
        for p in sorted(myworks.rglob("*")):
            if not p.is_file() or p.name.endswith(".part"):
                continue
            rel = p.relative_to(myworks).as_posix()
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while chunk := f.read(_HASH_CHUNK_SIZE):
                    h.update(chunk)
            st = p.stat()
            manifest.record_verified(rel, st.st_size, st.st_mtime, h.hexdigest(), now)
            count += 1
    print(f"rebuilt manifest from archive: {count} files")
    return 0


def main(
    argv: list[str] | None = None,
    device_factory: Callable[[Config], DeviceClient] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="seestar-sync",
        description="Mirror Seestar media to a NAS over SMB and reclaim device space.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("daemon", _cmd_daemon, "scheduled sync loop (container default)"),
        ("run", _cmd_run, "one sync pass, then exit (for cron / k8s CronJob)"),
    ):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--init", action="store_true",
                       help="first run only: create archive sentinel and directories")
        p.set_defaults(handler=handler)
    status_help = "human-readable summary of the last run"
    sub.add_parser("status", help=status_help, description=status_help).set_defaults(
        handler=_cmd_status, init=False
    )
    rebuild_help = "re-scan the archive, recompute checksums, rebuild the manifest DB"
    sub.add_parser(
        "rebuild-manifest", help=rebuild_help, description=rebuild_help
    ).set_defaults(handler=_cmd_rebuild_manifest, init=False)

    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        config = Config.from_env(env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if config.log_level != "DEBUG":
        # wire chatter (one line per SMB packet) drowns the operator
        # narrative in docker logs; DEBUG re-enables it for protocol work
        for noisy in ("smbprotocol", "spnego", "paho"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    factory = device_factory or _default_device_factory
    try:
        return args.handler(config, args, factory)
    except ArchiveError as e:
        print(f"archive error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
        return 0


def entrypoint() -> None:  # console_scripts wrapper
    sys.exit(main())
