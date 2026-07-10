# seestar-sync

`seestar-sync` mirrors the media library on a Seestar S30 Pro to a NAS volume over SMB
and reclaims space on the telescope once the copy is verified, using a tiered,
free-space-driven deletion policy. Deletion is allowlist-only: raw subframes go first,
then videos, then stacked FITs — the stacked JPGs and thumbnails the Seestar app uses
for in-app browsing are never touched, and never even reachable by the deleter.

## Quick start (Mac)

1. Mount the NAS share in Finder first (e.g. `smb://nas.local/astro`), so a real volume
   backs the archive path you're about to bind-mount. If the archive directory turns
   out to share a filesystem with the container's own root, the tool refuses to run
   rather than risk copying into the container's ephemeral layer.
2. Create a `.env` file next to `docker-compose.yml`:

   ```
   SEESTAR_HOST=seestar.local
   ARCHIVE_HOST_PATH=/Volumes/astro/seestar
   ```

3. Initialize the archive (first run only — creates the sentinel file and directory
   layout under `ARCHIVE_HOST_PATH`):

   ```bash
   docker compose run --rm seestar-sync run --init
   ```

4. Start the daemon:

   ```bash
   docker compose up -d
   ```

## Rollout sequence

`docker-compose.yml` defaults `DRY_RUN` to `true`. Leave it there for the first several
days: the daemon will log what it *would* copy and delete, and write the same plan into
`status.json`, without touching the device or the archive. Review the logs and
`status.json` across a few runs — especially the reclaim side, since deletes are
one-way — then set `DRY_RUN=false` in `.env` and restart. Reclaim behavior should be the
last thing you trust, not the first.

## Configuration

All configuration is via environment variables (12-factor; the same variables work
under Docker, `docker compose`, or a bare `uv run seestar-sync`).

| Variable | Default | Notes |
|---|---|---|
| `SEESTAR_HOST` | *(required)* | Hostname or IP of the telescope. Missing/blank is a fatal config error. |
| `ARCHIVE_DIR` | `/archive` | Root of the archive mirror inside the container. Files land under `ARCHIVE_DIR/MyWorks/`. |
| `SYNC_INTERVAL_MINUTES` | `15` | Daemon loop interval. Must be an integer `>= 1`. |
| `RECLAIM_TRIGGER_PERCENT` | `75` | Device-used-percent that triggers a reclaim pass. |
| `RECLAIM_TARGET_PERCENT` | `60` | Reclaim deletes (oldest session first, by tier) until usage drops to this level. Must be `0 < target < trigger <= 100`. |
| `ACTIVE_WINDOW_MINUTES` | `15` | Per-folder session guard: a top-level folder with any write inside this window is skipped for both copy and reclaim. Must be `>= 1` — it's a delete-safety feature and can't be disabled. |
| `DEVICE_CAPACITY_GB` | *(unset)* | Fallback total capacity, in GB, used only if the device won't answer a free-space query (see below). Must be a positive number if set. |
| `KUMA_PUSH_URL` | *(unset)* | Uptime Kuma push-monitor URL. Must be `http://` or `https://` if set. |
| `HA_WEBHOOK_URL` | *(unset)* | Home Assistant webhook URL. Must be `http://` or `https://` if set. |
| `DRY_RUN` | `false` | Accepts `1`/`true`/`yes`/`on` (true) or `0`/`false`/`no`/`off`/empty (false); anything else is a fatal config error. Plans copies/deletes but performs neither. |
| `ARCHIVE_ON_ROOT_FS_OK` | `false` | Escape hatch for the foreign-storage guard (see "Why the container runs as root", below, and the graceful-shutdown section for the related sentinel check). Same boolean parsing as `DRY_RUN`. |
| `LOG_LEVEL` | `INFO` | One of `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (case-insensitive); anything else is a fatal config error. |

## CLI

The image's `ENTRYPOINT` is `seestar-sync`; the default `CMD` is `daemon`, so
`docker compose up` runs the scheduled loop. Subcommands:

- `seestar-sync daemon [--init]` — scheduled sync loop, sleeping `SYNC_INTERVAL_MINUTES`
  between passes. This is the container default.
- `seestar-sync run [--init]` — a single sync pass, then exit. Useful for cron or a k8s
  `CronJob`.
- `seestar-sync status` — prints a human-readable summary of the last run, read from
  `status.json` in the archive's metadata directory. It does not touch the manifest
  database or the device. Because it still needs `SEESTAR_HOST` to pass config
  validation, run it the same way as the other commands rather than bare on the host:

  ```bash
  docker compose run --rm seestar-sync status
  # or, against the already-running daemon container:
  docker compose exec seestar-sync seestar-sync status
  ```

- `seestar-sync rebuild-manifest` — re-scans the archive, recomputes checksums, and
  rebuilds the manifest database from scratch. Use this if the manifest is lost or
  suspected corrupt; the archive on disk is the source of truth.

`--init` is only needed once, the first time an archive directory is used: it creates
the sentinel file and the `MyWorks/` directory layout. It's safe to pass on every run
thereafter (it's a no-op if the sentinel already exists).

### Exit codes

- `0` — the run completed OK, or the device was unreachable and the run was skipped
  (both are considered "nothing to alert on").
- `1` — the run finished but with errors (partial copy/reclaim failures).
- `2` — fatal: bad configuration, or the archive volume isn't usable (missing,
  unmounted, or the sentinel/foreign-storage checks failed).

## Reclaim tiers

When device usage crosses `RECLAIM_TRIGGER_PERCENT`, the reclaim pass deletes
device-original files — always from folders already verified copied to the NAS, oldest
session first — until usage drops to `RECLAIM_TARGET_PERCENT`, working through tiers in
order:

1. **Subs** — raw `.fit` subframes inside `*_sub` folders.
2. **Videos** — any `.avi` or `.mp4` file outside a `_sub` folder. This is deliberately
   broader than "solar-system folders" from the design spec: classifying by media type
   rather than by folder name avoids coupling to Seestar firmware's folder-naming
   choices, and it's still safe because deletion always requires a verified NAS copy
   first.
3. **Stacked FITs** — stacked `.fit` files (matching either firmware generation's
   naming, e.g. `Stacked_...` or `DSO_Stacked_...`) in target (non-`_sub`) folders.

Deletion is allowlist-only. Stacked JPGs, `_thn.jpg` thumbnails, and any file type not
listed above are structurally unreachable by the deleter — there is no code path that
can classify them as deletable, regardless of tier or age. Folders with a recent write
(within `ACTIVE_WINDOW_MINUTES`) are skipped entirely for that pass, whether for copy or
reclaim.

## Free-space measurement

Device usage is measured by querying the SMB share directly. Some Seestar firmware
versions don't answer that query. If it fails and `DEVICE_CAPACITY_GB` is unset, the run
completes as `partial` with a "free space unmeasurable" error recorded in `status.json`
— reclaim can't run without knowing usage, but the copy side is unaffected. Set
`DEVICE_CAPACITY_GB` (128 for the S30 Pro) to give the tool a fixed total capacity to
compute usage against instead.

## Why the container runs as root

The image has no `USER` directive, so the process runs as root inside the container.
The archive volume's ownership varies by deployment — Mac SMB mounts via Finder, NAS
bind mounts, etc. — and picking a fixed non-root UID/GID in the image would just as
often be wrong as right. For Kubernetes deployments, set a `securityContext`/`fsGroup`
on the pod instead of relying on the image's default.

## Graceful shutdown

On `SIGTERM` or `SIGINT` (e.g. `docker stop`, or `docker compose down`), the daemon
finishes the **current file** it's copying or deleting, then exits — not the entire
sync pass. `docker-compose.yml` sets `stop_grace_period: 60s` to give that a chance to
happen cleanly, but it isn't load-bearing: even a hard `SIGKILL` mid-transfer is safe,
because copies land as `.part` files that are verified and atomically renamed only on
success — a killed copy just leaves an orphaned `.part` that the next run ignores and
overwrites. Before every pass (not just on daemon startup), the tool re-checks the
archive's sentinel file and that the archive directory isn't on the container's own
filesystem, so a dropped SMB/NAS mount mid-daemon-lifetime is caught rather than
silently treated as an empty archive.

## Monitoring

- **`status.json`** — written after every run to the archive's metadata directory
  (`ARCHIVE_DIR/.seestar-sync/status.json`); read by `seestar-sync status`.
- **Monthly history** — every run is also appended as a JSON line to
  `ARCHIVE_DIR/.seestar-sync/history/YYYY-MM.jsonl`.
- **Uptime Kuma** — if `KUMA_PUSH_URL` is set, a push is sent only when the run result
  is `ok` or `skipped_unreachable`. Runs with errors deliberately don't ping, so silence
  from Kuma's side is the alert.
- **Home Assistant** — if `HA_WEBHOOK_URL` is set, every run POSTs the same JSON shape
  as `status.json` to that webhook, regardless of result.

All reporters are fire-and-forget: a reporter failure (network error, bad URL, etc.) is
logged as a warning and never affects the run's exit code.

## Design spec

For the full design rationale (SMB client choice, manifest schema, safety properties,
open questions), see
[`docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md`](docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md).
