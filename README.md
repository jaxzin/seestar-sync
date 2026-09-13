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
   docker compose up -d --build
   ```

   (`--build` is a no-op on a fresh checkout — compose builds the image the first
   time anyway — but it's load-bearing when upgrading; see below.)

## Upgrading

Upgrading an existing deployment requires a rebuild:

```bash
docker compose up -d --build
```

Compose does **not** rebuild the image on its own — a plain `docker compose up -d`
restarts the *old* image, and any environment variables the old version never read
(e.g. the `MQTT_*` vars added in v0.2) are silently ignored: the container comes up
looking healthy while the new feature simply doesn't exist. Always pass `--build`
when upgrading.

## Rollout sequence

Two switches control how much the tool is allowed to do: `DRY_RUN` (plan vs. execute)
and `RECLAIM_ENABLED` (whether deletion exists at all). Graduate through four phases,
and let reclaim graduate last — deletes are one-way:

1. **`DRY_RUN=true`** (the compose default) — run for the first several days. The
   daemon logs every file it *would* copy and writes the planned totals into
   `status.json`, without touching the device or the archive. (The per-file plan
   appears only in the logs; `status.json` carries the counts.) Review the copy plans
   across a few runs.
2. **`DRY_RUN=false`, `RECLAIM_ENABLED=false`** (both env-var defaults) — real copies
   start landing on the NAS, but deletion is structurally impossible: with
   `RECLAIM_ENABLED=false` the reclaim phase never runs, no matter how full the device
   gets. Let this run until the archive has verified copies of your library.
3. **`DRY_RUN=true`, `RECLAIM_ENABLED=true`** — back to dry-run, now with reclaim
   switched on: the logs show the real per-file *deletion* plan without executing it.
   This review is only meaningful after phase 2 has landed verified copies — reclaim
   considers only NAS-verified files, so with an empty archive the deletion plan would
   be empty too. Eyeball the plan across a few runs; this is the step that earns your
   trust before anything is removed from the telescope.
4. **`DRY_RUN=false`, `RECLAIM_ENABLED=true`** — full operation: verified copies plus
   tiered space reclamation.

Reclaim behavior should be the last thing you trust, not the first.

## Configuration

All configuration is via environment variables (12-factor; the same variables work
under Docker, `docker compose`, or a bare `uv run seestar-sync`).

| Variable | Default | Notes |
|---|---|---|
| `SEESTAR_HOST` | *(required)* | Hostname or IP of the telescope. Missing/blank is a fatal config error. |
| `ARCHIVE_DIR` | `/archive` | Root of the archive mirror inside the container. Files land under `ARCHIVE_DIR/MyWorks/`. |
| `SYNC_INTERVAL_MINUTES` | `15` | Daemon loop interval. Must be an integer `>= 1`. |
| `RECLAIM_ENABLED` | `false` | Master switch for deletion: while `false`, the reclaim phase never runs — regardless of the trigger/target settings or how full the device is — and the tool is copy-only (device fill level is still reported for monitoring). Deletion is opt-in. Same boolean parsing as `DRY_RUN`. |
| `RECLAIM_TRIGGER_PERCENT` | `75` | Device-used-percent that triggers a reclaim pass (only when `RECLAIM_ENABLED=true`). |
| `RECLAIM_TARGET_PERCENT` | `60` | Reclaim deletes (oldest session first, by tier) until usage drops to this level. Must be `0 < target < trigger <= 100`. |
| `ACTIVE_WINDOW_MINUTES` | `15` | Per-folder session guard: a top-level folder with any write inside this window is skipped for both copy and reclaim. Must be `>= 1` — it's a delete-safety feature and can't be disabled. |
| `DEVICE_CAPACITY_GB` | *(unset)* | Fallback total capacity, in GB, used only if the device won't answer a free-space query (see below). Must be a positive number if set. |
| `KUMA_PUSH_URL` | *(unset)* | Uptime Kuma push-monitor URL. Must be `http://` or `https://` if set. |
| `MQTT_HOST` | *(unset)* | Home Assistant MQTT broker host. Unset disables MQTT entirely (byte-for-byte v0.1 behavior). Only meaningful in `daemon` mode. |
| `MQTT_PORT` | `1883` | Broker port. Must be `1`-`65535`. |
| `MQTT_USERNAME` | *(unset)* | Broker username. Required if `MQTT_HOST` is set (enforced at startup) — anonymous brokers are not supported (see Security note). |
| `MQTT_PASSWORD` | *(unset)* | Broker password. **Secret** — `.env` only, never logged, never in `status.json`/history. Required if `MQTT_USERNAME` is set. |
| `MQTT_TOPIC_PREFIX` | `seestar-sync` | Topic namespace and HA device identifier. Must be a single topic segment (no `/ # +`) — set a different value per scope if you run more than one instance against the same broker. |
| `DRY_RUN` | `false` | Accepts `1`/`true`/`yes`/`on` (true) or `0`/`false`/`no`/`off`/empty (false); anything else is a fatal config error. Plans copies/deletes but performs neither. |
| `SHADOW_MODE` | `false` | Read-only observer of an archive another instance owns (e.g. a second deployment validating against the live NAS archive). Same boolean parsing as `DRY_RUN`. When true: a hard dry run (`DRY_RUN` forced true, `RECLAIM_ENABLED` forced false, and `settings.json` / MQTT overrides are ignored, so nothing can re-arm deletion); `manifest.db` is opened read-only and must already exist (a missing manifest is exit 2, never a create); nothing is written to the archive (no `status.json`, history, or `settings.json`; MQTT `set/*` commands are refused with a logged warning; `--init` and `rebuild-manifest` exit 2). If the primary left `manifest.db` with a hot journal (crashed mid-transaction) or holds its lock, the pass is skipped with a WARNING and retried next pass: result `skipped_manifest_busy`, exit 0, Kuma still pinged `up` (like `skipped_unreachable`). MQTT state and the Kuma push still report. Give a shadow instance its own `MQTT_TOPIC_PREFIX` and `KUMA_PUSH_URL` so it doesn't impersonate the primary. |
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
device-original files — only files individually verified as copied to the NAS, oldest
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
- **Home Assistant (MQTT)** — if `MQTT_HOST` is set (daemon mode only), the daemon
  publishes live progress and per-run summaries as an MQTT-discovered device, and
  accepts writable settings back from HA. See "Home Assistant (MQTT)" below.

All reporters are fire-and-forget: a reporter failure (network error, bad URL, etc.) is
logged as a warning and never affects the run's exit code.

Third-party protocol libraries (`smbprotocol`, `spnego`, `paho`) are quieted to
`WARNING` unless `LOG_LEVEL=DEBUG`, since their per-packet chatter at `INFO`/`DEBUG`
drowns the operator-facing log narrative. Set `LOG_LEVEL=DEBUG` to re-enable their
verbose logging when debugging the SMB or MQTT wire protocol directly.

## Home Assistant (MQTT)

Setting `MQTT_HOST` (daemon mode only) makes `seestar-sync` appear in Home Assistant
as an MQTT-discovered device named **Seestar Sync**, with live progress and writable
runtime settings. Prerequisite: an MQTT broker HA can see — typically the Mosquitto
add-on — and a dedicated MQTT user for this tool (see Security note below; **do not**
point `MQTT_HOST` at an anonymous/unauthenticated broker).

### Entities

18 entities are published under the device, split across the areas HA groups by:

**Live state**
- **Daemon online** (`binary_sensor`) — the alertable liveness signal. A graceful stop
  (`docker stop` / `SIGTERM`) publishes `offline` explicitly before a clean disconnect;
  if the daemon dies *ungracefully* (crash, `SIGKILL`, network drop), MQTT's Last Will
  (LWT) has the broker flip it off automatically. Either way, every other entity greys
  out in HA because they share the same availability topic. Uptime Kuma remains the
  independent, MQTT-broker-agnostic alarm — losing MQTT doesn't lose your alerting.
- **State** — `listing` / `copying` / `reclaiming` / `idle` / `unreachable` / `error`.
- **Progress** (%) — updated at most once every 10 seconds while a phase is in
  progress; a *phase change* (e.g. `copying` → `reclaiming`) always publishes
  immediately regardless of the throttle. Attributes include file/byte counts and the
  current file path.
- **Device fill level** (%)

**Last run**
- **Last run result**, **Last run time**, **Files copied**, **Bytes copied**,
  **Files deleted**, **Bytes reclaimed**, **Errors**

**Settings (writable, config category)**
- **Dry run** (switch), **Reclaim enabled** (switch), **Seestar host** (text)

**Diagnostics**
- **Sync interval (min)**, **Reclaim trigger %**, **Reclaim target %**, **Version**

### Settings semantics

The three writable entities (**Dry run**, **Reclaim enabled**, **Seestar host**) are
overrides, not live config edits:

- A change from HA is written to `.seestar-sync/settings.json` in the archive's
  metadata directory and takes effect at the **start of the next pass** — never
  mid-pass, so a change made while a copy is running can't half-apply.
- Precedence is **override > env default**, resolved once per pass into a snapshot.
  An override survives daemon restarts *and* redeploys — it's a file on the archive
  volume, not in-memory state. To revert a setting to its env value, remove its key
  from `settings.json` (or delete the file — all overrides revert to env); the next
  pass picks it up, no restart needed. There is no "unset" gesture in HA today —
  setting the entity back to the env value also works, but `status` will keep
  reporting the source as `mqtt override`.
- Overrides are mode-independent: a one-shot `seestar-sync run` (cron/CronJob) applies
  the same `settings.json` overrides even though MQTT itself is daemon-only.
- `seestar-sync status` shows both the effective value and where it came from, e.g.:

  ```
  settings:
    dry_run: True (mqtt override)
    reclaim_enabled: False (env)
    seestar_host: seestar.local (env)
  ```

  (Python's native `True`/`False` — the CLI prints the values verbatim, not
  HA's `ON`/`OFF` strings.) The `settings:` block only appears once a run has produced
  `status.json` — `seestar-sync status` prints "no status yet" before the first run.
- **Watch out**: a plausible-but-wrong **Seestar host** override (e.g. a typo, or a
  stale IP after a DHCP lease change) doesn't surface as a settings error — the pass
  simply can't reach the device, and HA shows the **State** sensor stuck at
  `unreachable`. If sync looks stalled, check the **Seestar host** entity's source
  (`mqtt override` vs `env`) before suspecting the telescope itself.
- The **done** event that closes every pass (and the summary it drives) carries
  **copy totals only** — `files_copied`/`bytes_copied`, not reclaim counts. Reclaim
  totals (`files_deleted`/`bytes_reclaimed`) are only present in the **Last run**
  summary entities, not in the live **Progress** attributes.

## Security note

The trust boundary is your LAN: the Seestar's guest SMB share gives *anyone on the
network* full read/write/delete access to the telescope's storage — with or without this
tool. This tool's SMB transfers are unsigned, because guest sessions cryptographically
cannot sign (a device design constraint, not a configuration choice), so file integrity
in transit is only as trustworthy as the network itself. Keep the telescope on a trusted
network or its own VLAN.

MQTT broker credentials are effectively **admin credentials for this tool**, not just a
monitoring feed: the command topics let anyone who can publish to them arm deletion
(`Reclaim enabled` ON), disarm dry-run (`Dry run` OFF), and redirect the sync target to
an arbitrary host (`Seestar host`) — full control over what gets copied, what gets
deleted, and where from. Always set `MQTT_USERNAME`/`MQTT_PASSWORD` and point
`MQTT_HOST` at an authenticated broker — never an anonymous/open one. (Supplying
credentials is enforced at startup — `MQTT_HOST` without `MQTT_USERNAME` is rejected
as a fatal config error. What the tool *cannot* verify from its side is whether the
broker itself rejects anonymous clients, so configuring the broker to require
authentication remains your responsibility.)
Treat broker compromise as equivalent to compromising this tool directly.
`MQTT_PASSWORD` is a secret like the Kuma push URL: it belongs in `.env` only, is never
written to `status.json`/history, and is redacted from log output even in error paths.

## Tools

`tools/solar_stabilize.py` stabilizes a Seestar solar timelapse by locking the solar
disk to a fixed position (removes mount jitter and drift). Eclipse-safe — it fits a
circle to the solar limb rather than tracking the brightness centroid, so the lunar
bite doesn't pull the track off center — and cloud-robust: frames whose limb fit
fails quality gates are interpolated through. Requires `ffmpeg` on PATH; Python
dependencies resolve automatically via `uv`.

```bash
uv run tools/solar_stabilize.py "Solar-timelapse.mov" --report jitter.png --compare compare.mp4
```

Output defaults to `<input>-stabilized.mp4` beside the input. `--report` writes a
before/after center-track plot; `--compare` writes a side-by-side crop video.

`--stack [N]` (bare flag = 21-frame window) additionally enhances detail with
motion-adaptive temporal stacking: each frame becomes a weighted average of its
aligned neighbors, erasing sensor grain on the static solar disk while pixels near
the moving lunar limb keep full temporal resolution — the eclipse "bite" stays
crisp. A gentle luma-only unsharp (`--sharpen`, default 0.6) then lifts sunspot
and limb detail.

## Design spec

For the full design rationale (SMB client choice, manifest schema, safety properties,
open questions), see
[`docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md`](docs/superpowers/specs/2026-07-09-seestar-nas-sync-design.md).
