# Seestar Sync

Backs up the photos on your ZWO Seestar S30 Pro to a NAS or other storage that
Home Assistant can reach, over the telescope's SMB share. Every copy is verified
(sha256, atomic rename), the folder the scope is currently writing is never
touched, and, only if you turn it on, space on the telescope can be reclaimed
once files are safely backed up. Stacked JPGs and thumbnails (what the Seestar
app browses) are never deleted.

With Home Assistant's MQTT integration it also shows up as a **Seestar Sync**
device with live progress and the last run's result.

## Before you start

- A **Seestar S30 Pro** on your network with a **fixed IP** (a DHCP reservation
  in your router). Only the S30 Pro has been tested.
- **Storage for the archive** that Home Assistant can reach (next section).
  A full library is hundreds of GB.
- Optional: the **Mosquitto broker** add-on (or any authenticated broker) for the
  Home Assistant device.

## 1. Give the add-on somewhere to put the archive

Use Home Assistant's network storage so the archive lands on your NAS, not on
Home Assistant's own disk:

1. **Settings → System → Storage → Add network storage.**
2. Name it `seestar`, set **Usage** to **Media**, choose SMB/CIFS or NFS, and
   enter your NAS server and share.
3. The share is now `/media/seestar`, which is this add-on's default
   `archive_dir`.

If `archive_dir` does not exist, the add-on stops with *"archive dir ... does not
exist — is the volume mounted?"* instead of writing anywhere. That is
intentional. A folder on Home Assistant's own disk works but is not protected
from filling it up, so prefer network storage.

## 2. First run: the rehearsal

The defaults are **`dry_run: true`** and **`reclaim_enabled: false`**. In this
state the add-on connects to the telescope, lists its files and **logs what it
would copy**, but copies nothing and deletes nothing.

1. Set `seestar_host` to the telescope's IP address and start the add-on.
2. Read the **Log** tab for a few runs. You should see the file counts and no
   errors. (If the telescope is powered off the run is simply skipped.)
3. Set `dry_run` to `false`. Real copies start landing in the archive, and
   deletion is still impossible because `reclaim_enabled` is `false`.
4. Let it run until the archive holds your whole library.

## 3. Optional: free space on the telescope

Do this last, and only when you trust the backup. Deleting from the telescope is
one-way.

1. Set `dry_run` to `true` and `reclaim_enabled` to `true`. The log now shows
   exactly which files would be deleted when the telescope passes
   `reclaim_trigger_percent` full. Nothing is deleted yet.
2. When the plan looks right, set `dry_run` back to `false`.

Only files already verified as copied are eligible, oldest first: raw
subframes, then videos, then stacked FITs.

## Options

| Option | Default | Notes |
|---|---|---|
| `seestar_host` | *(empty)* | **Required.** The telescope's IP address or hostname. |
| `archive_dir` | `/media/seestar` | Must be under `/media` or `/share`. |
| `sync_interval_minutes` | `15` | Time between sync passes. |
| `active_window_minutes` | `15` | A folder written to within this window is skipped, for copying and for deleting. |
| `dry_run` | `true` | Plan and log, but copy and delete nothing. |
| `reclaim_enabled` | `false` | Master switch for deleting from the telescope. |
| `reclaim_trigger_percent` | `75` | Telescope fullness that starts a reclaim pass. |
| `reclaim_target_percent` | `60` | Reclaim stops when the telescope is this full. Must be below the trigger. |
| `device_capacity_gb` | `128` | Used only if the firmware will not report free space. 128 for the S30 Pro. |
| `kuma_push_url` | *(empty)* | Optional Uptime Kuma push-monitor URL. |
| `shadow_mode` | `false` | Read-only observer of an archive that another instance owns. |
| `mqtt_host` | *(empty)* | Blank uses the Mosquitto add-on automatically. |
| `mqtt_port` | `1883` | Only used when `mqtt_host` is set. |
| `mqtt_username` | *(empty)* | Only used when `mqtt_host` is set. |
| `mqtt_password` | *(empty)* | Only used when `mqtt_host` is set. |
| `mqtt_topic_prefix` | `seestar-sync` | Also the name of the Home Assistant device. Changing it creates a new device and orphans the old one. |
| `log_level` | `info` | `debug`, `info`, `warning`, `error` or `critical`. |

## Home Assistant entities

With a broker available the add-on publishes a **Seestar Sync** device with 18
entities: live state (daemon online, current phase, progress), the last run's
result, time, files and bytes copied, three writable settings (**Dry run**,
**Reclaim enabled**, **Seestar host**) and diagnostics. The writable settings
are overrides stored in the archive; see the project README for how they combine
with the options above.

Anyone who can publish to this device's MQTT command topics can switch deletion
on. Use a broker that requires a username and password.

## Troubleshooting

- **Add-on stops immediately.** Read the Log tab; configuration errors are
  reported there in one line and the add-on stays stopped.
- **`archive dir ... does not exist`.** Mount the network storage (step 1) or fix
  `archive_dir`.
- **`requires TLS`.** The add-on speaks plain MQTT. Point `mqtt_host` at a
  plain-text listener (the Mosquitto add-on's port 1883).
- **No Home Assistant device.** The log says *No MQTT broker* when none was found.
  Install the Mosquitto add-on or set `mqtt_host`.
- **Telescope unreachable.** Runs are skipped while it is off. Check the IP and
  that the scope is on the same network as Home Assistant.

## Running more than one

Only one primary may write an archive. If you already run seestar-sync elsewhere
(Docker, Kubernetes) against the same archive, stop it first, or run this one
with `shadow_mode: true` and a different `mqtt_topic_prefix`.

A second instance, for example another telescope, needs **both**:

- its own `archive_dir`, and
- its own `mqtt_topic_prefix`.

The prefix is also the MQTT client ID, so two instances sharing one prefix
disconnect each other from the broker over and over. Both devices are named
"Seestar Sync" in Home Assistant, so tell them apart by prefix (the device
identifier), not by name. Entities of the second one get a `_2` suffix.
