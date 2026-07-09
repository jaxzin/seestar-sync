# seestar-sync — Design Spec

**Date:** 2026-07-09
**Status:** Approved design, pre-implementation

## Problem

The ZWO Seestar S30 Pro (128 GB eMMC) fills up with RAW subframes when
sub-frame saving is enabled. Media must be offloaded to a NAS automatically,
while stacked JPGs remain on the device so past captures stay browsable in
the Seestar app's gallery.

No existing tool does this. Prior art (researched 2026-07-09):

- [seestarpy](https://github.com/astronomyk/seestarpy) — Python SDK with
  list/download/delete primitives, but notebook-oriented, GPL-3.0, no
  daemon/policy layer. Used here as **protocol reference only** (share name,
  auth quirks, file layout), not as a dependency.
- [SSLM](https://github.com/AstroNoob-Tools/SSLM) — Windows GUI importer,
  deliberately never deletes on-device.
- [seestar_alp](https://github.com/smart-underworld/seestar_alp) — device
  control only, no file sync.

## Device facts (verified)

- SMB share **`EMMC Images`**, port 445, guest login (empty username/password),
  direct TCP. Read-**write**: deletes work. This is ZWO's documented end-user
  transfer interface — the most stable contract the device offers.
- The JSON-RPC API (port 4700) is auth-locked on firmware ≥ 7.18 (RSA
  handshake, key extracted from ZWO's APK). **Avoided entirely** — SMB and
  HTTP need no auth.
- Layout under `MyWorks/`:
  - `<Target>/` (e.g. `M 81/` — names contain spaces) holds stacked-result
    triplets: `*Stacked*.fit` + `.jpg` + `_thn.jpg`. Stacked-FIT prefix
    varies by firmware (`Stacked_...` older, `DSO_Stacked_...` on 7.7x) —
    parse defensively.
  - `<Target>_sub/` holds RAW subframes `Light_*.fit` (the bulk of storage).
  - Solar-system folders (`Sun/`, `Lunar/`, …) hold JPG/MP4/AVI
    (RAW AVIs ≈ 3.5 GB each).
- The app gallery enumerates the filesystem live (no database to orphan);
  deleting `_sub` folders is standard community practice and leaves stacked
  results browsable. The app's storage bar may not refresh until reboot
  (cosmetic).
- The phone app auto-removes solar/lunar JPGs from the device by design;
  the sync must not be surprised by files vanishing between runs.
- Seestar cannot join 10.0.0.x subnets (clashes with its internal AP range).

## Requirements (from brainstorming Q&A)

| Decision | Choice |
|---|---|
| Runtime | Portable Docker container; iterate on the Mac first, later NAS/k8s (PVC) |
| Connectivity | Seestar in station mode, always on the LAN when powered |
| Copy scope | **Everything** under `MyWorks/` is mirrored to the NAS |
| Delete policy | **Free-space driven** with hysteresis; nothing deleted until device usage exceeds a threshold |
| Delete tiers | 1: RAW subs → 2: videos → 3: stacked FITs. Stacked `.jpg`/`_thn.jpg` are never deleted |
| Trigger | Scheduled poll (default every 15 min); silently skip when device is off |
| NAS access | Container writes to a mounted volume (`/archive`); SMB mount on Mac, local path on NAS, PVC on k8s |
| Monitoring | Structured logs + status file + JSONL history, Home Assistant webhook, **Uptime Kuma** push (dead-man switch) |

## Architecture

One Python application (`smbprotocol` as the only protocol dependency), one
unprivileged container. A userspace SMB client — never a kernel CIFS mount —
so a device that powers off mid-transfer is a catchable socket error, not a
wedged filesystem.

```
Scheduler loop (SYNC_INTERVAL_MINUTES)
  └─ Sync run:
      1. Probe     TCP 445 reachable? No → result skipped_unreachable, done.
      2. Inventory Walk MyWorks/ over SMB; diff against manifest + archive.
      3. Copy      New files → /archive mirror, .part name, verify, atomic rename.
      4. Verify    Size + SHA-256 (device read vs. landed copy).
      5. Reclaim   If device usage > trigger: delete per policy engine (below).
      6. Report    status.json, JSONL history append, log, Kuma push, HA webhook.
```

### Components

- **DeviceClient** — interface wrapping all Seestar I/O (list, read, stat,
  delete, free-space). Production impl: `smbprotocol`. Test impl: in-memory
  fake. Policy code never learns SMB exists; this seam is also the migration
  point if ZWO changes protocols.
- **Inventory/diff** — pure functions: device listing + manifest → copy plan.
- **Policy engine** — pure functions: inventory + free space + manifest →
  deletion plan. All tier/hysteresis/session-guard logic lives here.
- **Archive writer** — mirror layout under `/archive/MyWorks/…`, `.part` +
  atomic-rename discipline.
- **Manifest** — SQLite at `/archive/.seestar-sync/manifest.db`. One row per
  file ever copied: device path, size, SHA-256, copied_at, verified flag,
  deleted_from_device_at, tier. Rebuildable from the archive
  (`rebuild-manifest`); the archive is authoritative, the manifest is a cache
  plus audit trail.
- **Reporters** — status file, JSONL history, Kuma, HA. Strictly fire-and-forget.

### Archive layout

Mirror of the device — no renaming or reorganizing (resumable, debuggable,
post-processable by existing community organizer tools):

```
/archive/
  MyWorks/M 81/DSO_Stacked_33_M 81_20.0s_20260311_213509.fit  (+ .jpg, _thn.jpg)
  MyWorks/M 81_sub/Light_M 81_10.0s_IRCUT_20250607-221746.fit
  MyWorks/Sun/…
  .seestar-sync/
    ARCHIVE_MARKER          (sentinel, written by --init)
    manifest.db
    status.json             (latest run snapshot)
    history/2026-07.jsonl   (one line per run; monthly files = free rotation)
```

## Operator interface

### Configuration (environment variables only)

| Variable | Default | Purpose |
|---|---|---|
| `SEESTAR_HOST` | *(required)* | Hostname/IP of the Seestar |
| `ARCHIVE_DIR` | `/archive` | Mounted destination volume |
| `SYNC_INTERVAL_MINUTES` | `15` | Poll cadence in daemon mode |
| `RECLAIM_TRIGGER_PERCENT` | `75` | Start deleting when device usage exceeds this |
| `RECLAIM_TARGET_PERCENT` | `60` | Stop deleting once usage falls below this |
| `ACTIVE_WINDOW_MINUTES` | `15` | Recent-write window marking a folder as active |
| `DEVICE_CAPACITY_GB` | *(unset)* | Only for free-space fallback mode (see Open items) |
| `KUMA_PUSH_URL` | *(unset)* | Uptime Kuma push URL, pinged after successful runs |
| `HA_WEBHOOK_URL` | *(unset)* | Home Assistant webhook, receives run-summary JSON |
| `DRY_RUN` | `false` | Log every copy/delete decision; execute none |
| `ARCHIVE_ON_ROOT_FS_OK` | `false` | Skip the foreign-storage (`st_dev`) check for uncontainerized setups |
| `LOG_LEVEL` | `INFO` | |

### CLI

```
seestar-sync daemon             # scheduled loop (container default CMD)
seestar-sync run                # one sync pass, then exit (cron / k8s CronJob)
seestar-sync status             # human-readable summary from status.json + manifest
seestar-sync rebuild-manifest   # re-scan archive, recompute checksums, rebuild DB
seestar-sync <daemon|run> --init   # first run only: create sentinel + dirs
```

Exit codes: `0` success (including `skipped_unreachable` — a powered-off
telescope is normal); `1` run completed with errors; `2` fatal
(bad config, missing archive sentinel).

### Status file / run summary

`status.json` is the latest line of the JSONL history, materialized:

```json
{
  "last_run": "2026-07-09T09:15:03Z",
  "result": "ok | partial | skipped_unreachable | error",
  "files_copied": 142, "bytes_copied": 3812000000,
  "files_deleted": 89,  "bytes_reclaimed": 2100000000,
  "device_used_percent": 71.2,
  "active_folders_skipped": ["M 81"],
  "errors": []
}
```

The same JSON is POSTed to `HA_WEBHOOK_URL`. Kuma is pinged only on
`ok`/`skipped_unreachable` (so `partial`/`error` and prolonged silence both
surface as Kuma alerts). **Reporters never affect the run result:** ~5 s
timeouts, all exceptions caught and logged as warnings. A failed history
write is also only a warning; only the archive volume itself is load-bearing.

## Reclaim policy engine

**Free-space measurement:** SMB2 `FsFullSizeInformation` query against the
share (validation spike — see Open items). Fallback: full-walk usage vs.
`DEVICE_CAPACITY_GB`.

**Hysteresis loop:** when usage > `RECLAIM_TRIGGER_PERCENT`, delete until
usage < `RECLAIM_TARGET_PERCENT`, then stop.

**Candidate selection is allowlist-based** — candidates come only from the
three tiers below; anything else (JPGs, thumbnails, unknown file types, files
introduced by future firmware) is structurally unreachable by the deleter.
A firmware rename therefore fails safe (nothing deleted), never catastrophic.

1. **Tier 1 — RAW subs:** files in `*_sub` folders, oldest session first
   (timestamp parsed from filename; folder mtime fallback). Emptied `_sub`
   folders are removed.
2. **Tier 2 — videos:** `.avi` / `.mp4` in solar-system folders, oldest first.
3. **Tier 3 — stacked FITs:** `*Stacked*.fit` in target folders, oldest
   first. The `.jpg` + `_thn.jpg` browsing triplet remnants always remain.

**Hard preconditions for each individual delete** (checked at delete time,
not plan time — all must hold):

- Device path + size + checksum match a `verified` manifest row.
- Current SMB size/mtime still match what was verified.
- The file's folder is not active (per-folder session guard).
- `DRY_RUN` is off.

**Per-folder session guard:** a folder with writes in the last
`ACTIVE_WINDOW_MINUTES` is skipped for both copy and reclaim; all other
folders remain fully eligible even while an imaging session runs elsewhere
on the device.

**Audit ordering (write-ahead):** the manifest audit row (`deleted_at`, tier,
bytes) is committed *before* the SMB delete is issued. Any interruption
yields a false "deleted" (detected and re-processed harmlessly next run),
never an unrecorded true deletion.

**Copy/reclaim separation:** a file is never deleted in the run phase that
copied it; deletion only ever happens under free-space pressure, against
files verified in this or any prior run.

## Error handling

- **Device powers off mid-run** (routine): every SMB op has a ~30 s timeout.
  Abort the run cleanly; verified copies are kept and recorded; the in-flight
  `.part` is discarded. Re-runs resume naturally. Result `partial` only if
  real errors occurred.
- **Torn/changing source files:** `.part` + verify + atomic rename means the
  archive can never contain a corrupt file. Verify mismatch → discard, retry
  next run; 3 consecutive mismatches on one file → flag in `errors[]` and
  skip, so one bad file can't wedge the pipeline.
- **Archive volume vanishes** (Mac SMB mount drop leaves an empty dir): the
  sentinel `ARCHIVE_MARKER` is required at startup; missing sentinel → exit 2,
  touch nothing. First-run init is explicit via `--init`.
- **No volume mounted at all** (the ephemeral-filesystem trap): without this
  guard, `--init` on an unmounted `ARCHIVE_DIR` would create the sentinel,
  manifest, and "archive" on the container's own writable layer — copies
  would "succeed," verification would pass, and reclaim could delete
  originals whose only remaining copy dies with the container. Therefore
  `--init` **and every startup** verify `ARCHIVE_DIR` is foreign storage:
  `st_dev` of the archive dir must differ from `st_dev` of `/` (volumes,
  bind mounts, SMB mounts, and PVCs always do). Same device → exit 2
  ("archive dir is on the container's own filesystem"). Escape hatch for
  legitimately uncontainerized setups: `ARCHIVE_ON_ROOT_FS_OK=true`.
- **Manifest lost/corrupt:** deletes require verified manifest rows, so a
  missing manifest disables all reclaiming (fail-safe). `rebuild-manifest`
  restores it from the archive.
- **Config errors:** validated at startup; exit 2 before any I/O.
- **Files deleted out from under us** (phone app removes solar JPGs): treated
  as normal inventory drift, logged at DEBUG, manifest row kept for history.

## Testing

TDD (superpowers:test-driven-development), three rings:

1. **Unit — policy engine (bulk of tests):** diffing, tier selection,
   hysteresis, session guard, filename parsing across both firmware naming
   generations. Pure functions over in-memory inventories.
2. **Integration — fake DeviceClient + tmpdir archive:** full sync loop,
   mid-run disconnects, torn writes, sentinel enforcement, the
   foreign-storage check, resume-after-crash, write-ahead audit ordering.
3. **Live smoke — real telescope,** gated by `SEESTAR_LIVE_TEST=1`:
   reachability, share layout assertions, `FsFullSizeInformation` spike,
   full `DRY_RUN` pass.

**Rollout on the Mac:** dry-run daemon for several days (watch JSONL) →
enable copying → enable reclaim only after eyeballing correct dry-run
deletion plans. Copy bugs waste bandwidth; delete bugs destroy data — they
graduate on different schedules.

## Open items (validation spikes, first implementation steps)

1. Does Seestar firmware answer `FsFullSizeInformation` sensibly? If not,
   implement the `DEVICE_CAPACITY_GB` fallback path.
2. Confirm guest SMB auth parameters against the S30 Pro specifically
   (research verified S50/S30 broadly; no S30 Pro counter-reports found).

## Out of scope (YAGNI)

- Post-processing/re-stacking, FITS organization beyond mirroring.
- Multi-device support (single `SEESTAR_HOST`; the design doesn't preclude
  running one container per scope later).
- Web UI. Device-online push triggers. k8s manifests (the container is
  k8s-ready; manifests come when the migration happens).
