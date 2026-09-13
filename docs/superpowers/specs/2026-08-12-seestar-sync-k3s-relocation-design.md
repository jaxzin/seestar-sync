# seestar-sync → k3s relocation design

**Date:** 2026-08-12
**Status:** approved, pending implementation plan
**Precipitating incident:** a 26-day silent outage (2026-07-17 → 2026-08-12)

## Why this exists

On 2026-07-17 19:39 UTC the `seestar-backup-seestar-sync-1` container received
SIGTERM and exited 0 — a graceful Docker Desktop shutdown, not a crash. It never
came back, because Docker Desktop is not in the Mac's login items. The daemon
stayed dead for 26 days. Nothing alerted.

The compose file already sets `restart: unless-stopped`, and that setting is
correct. It could not help: `unless-stopped` is a *daemon-scoped* guarantee — it
restores containers when the Docker daemon starts, and says nothing about
whether the daemon ever does. On a laptop that is a real gap.

Two properties of the current deployment caused the outage and its duration:

1. **It runs on a laptop**, behind a Finder-mounted SMB share that must be
   re-mounted by hand after every reboot.
2. **Nothing watches it.** The MQTT LWT flipped to `offline` and no one saw.

Both are fixed by moving the daemon onto the k3s cluster with a declared Uptime
Kuma monitor. The move also brings the deployment under the no-manual-IaC rule:
today it exists only as commands someone typed.

## Constraints established by investigation

Verified live on 2026-08-12, not assumed:

- **Network is flat.** Every cluster node is on `192.168.10.0/24`. From `nuc`:
  `ping 192.168.10.50` (telescope) 0% loss, TCP 445 open; `seestar.local`
  resolves; NAS is `192.168.10.7`; the MQTT broker
  `homeassistant.iot.jaxzin.com` resolves to `192.168.10.240` — same subnet
  despite the `.iot.` label. No VLAN to cross.
- **No SMB storage path exists.** `kubectl get csidrivers` returns nothing. One
  StorageClass: `local-path` (node-pinned, RWO). There is no `csi-driver-smb`,
  no NFS CSI, no Longhorn.
- **`astrograph` is SMB-only.** In `nas-setup/nas.yml:153` it is declared with
  no `nfs_exports` key, which is exactly what routes a share to
  `roles/dsm_shares` instead of `roles/nfs_share`.
- **Schedulable amd64 pool is `nuc` and `tower1`.** `orin` is arm64; the Pis are
  either arm64, tainted, or NotReady.
- **Permissions:** `/volume1/astrograph` is `755 root:root`;
  `/volume1/astrograph/seestar/archive` is `777 jaxzin:users` (uid 1026,
  gid 100).

## Decisions

Three forks were put to the user; all three are recorded here with the option
chosen and the cost accepted.

### D1 — Archive access: NFS export + static PV

**Chosen** over installing `csi-driver-smb` and over refactoring the Archive
layer to speak SMB natively.

`astrograph` gains an `nfs_exports` key in `nas-setup/nas.yml`, and the cluster
binds it with a static PV/PVC pair copied from `platform/pano/pano.yaml` — the
only working NAS-mount precedent in the repo, live and `Bound` today.

**Accepted cost:** NFS export ACLs are IP-scoped with no per-workload
authentication. The export must cover `192.168.10.0/24` (see the kubelet gotcha
below), so anything on the LAN can mount the archive read-write. This is the
irreplaceable share. `csi-driver-smb` would have authenticated per-workload
with credentials from OpenBao, at the price of a new cluster-wide storage
component owned for exactly one consumer.

### D2 — Repo placement: transfer to the `fallen-leaf` org

`jaxzin/seestar-sync` moves to `fallen-leaf/seestar-sync`.

Org-level Gitea Actions secrets (`REGISTRY_HOSTNAME`, `REGISTRY_USER`,
`REGISTRY_TOKEN`, and the OpenBao AppRole pair) reach only `fallen-leaf/*`
repos. A kaniko build job in a user repo cannot authenticate to the registry.
This is anti-pattern #1 in the homelab conventions — the same failure that
required a repo transfer on 2026-07-17.

**Accepted cost:** the clone URL changes; the local remote and every reference
must be updated in the same change.

### D4 — Generalize `roles/nfs_share` to loop over exported shares

Discovered during planning, after D1 was approved. `nas.yml`'s two share-managing
roles partition the list by the presence of `nfs_exports`:

- `ansible/roles/dsm_shares/tasks/main.yml:4-17` selects with
  `rejectattr('nfs_exports', 'defined')` — adding the key **removes `astrograph`
  from `dsm_shares`**.
- `ansible/roles/nfs_share/defaults/main.yml` hardcodes
  `nfs_share_lookup: "netboot"` — a single share name, not a list.

So D1's change as originally specified would have left `astrograph` managed by
**neither** role. The partition invariant that `nas.yml:143-152` asserts is only
half-implemented.

**Chosen:** replace the hardcoded lookup with a loop over every share carrying an
`nfs_exports` key, moving the role body into a looped `converge_share.yml`. This
makes the claimed invariant real and keeps the data driving the behavior.

**Accepted cost:** this refactors a safety-critical role. Its read-back verify
asserts the live export list *exactly* equals the desired one — deliberately, so
that a partially-applied save cannot converge green with the boot base writable
by the whole LAN. The `netboot` export is the Pi fleet's NFS root, so the fleet's
boot path is in the blast radius. The plan must prove `netboot`'s export is
byte-identical before and after the refactor, in validate mode, before any
`astrograph` export is added.

**Also settled by this investigation:** the squash value needed for uid 1026 is
`root_squash: "root"` — DSM's "No-mapping", already proven on both `netboot`
rules. The spec's earlier "no user squashing" prose resolves to that literal. Note
that `''` is the projection default and nothing supplies a safe fallback, so the
key must be declared explicitly on every rule.

### D3 — Cutover: shadow in dry-run, then hard cutover

The pod is deployed first with `DRY_RUN=true` and a distinct MQTT topic prefix,
verified against the Mac's numbers, and only then promoted. Detail in
"Cutover" below.

**Accepted cost:** two extra phases, and a brief window where two daemons walk
the telescope concurrently. That walk is read-only, so it is safe.

## Architecture

Four repos, split by the fleet boundaries. No repo takes on a neighbor's layer.

| Repo | Change | Ownership rationale |
|---|---|---|
| `fallen-leaf/nas-setup` | generalize `roles/nfs_share` to loop (D4); `nfs_exports` on the `astrograph` share | host layer — the NAS owns its exports |
| `fallen-leaf/seestar-sync` | `.gitea/workflows/build-images.yml` | the app owns how it is built |
| `fallen-leaf/homelab-cluster` | `platform/seestar-sync{-ns,.app}.yaml` + `platform/seestar-sync/` | the cluster owns *how a service runs* |
| `fallen-leaf/uptime-kuma` | one entry in `deploy/monitors.yaml` | the service repo owns *what it watches* — stated verbatim in `apps/uptime-kuma-converge.yaml` |

### Storage

Export declaration in `nas-setup/nas.yml`:

```yaml
- name: astrograph
  volume: /volume1
  purpose: "Seestar astrophotography FITS captures — irreplaceable"
  desc: "Seestar astrophotography FITS - salvaged 2026-07-26"
  nfs_exports:
    - client: "192.168.10.0/24"
      privilege: "rw"
      # "root" = DSM No-mapping. Required: the pod presents uid 1026 so files
      # land as jaxzin:users, matching what the SMB mount produced.
      root_squash: "root"
      async: true
      insecure: true
      crossmnt: false
      security_flavor: {sys: true, kerberos: false, kerberos_integrity: false, kerberos_privacy: false}
```

Schema confirmed against `ansible/roles/nfs_share/tasks/main.yml:250-261` (the
7-key projection) and the working `netboot` rules at `nas.yml:114-127`. Every key
is passed to the DSM API verbatim, so the YAML keys *are* the DSM field names.
`crossmnt` is `false` here because — unlike `netboot`, whose nfsroot is a
subfolder — nothing mounts across a nested mountpoint in this share.

`desc` must stay ≤64 characters; DSM rejects longer with a bare error 3300, and
the role asserts the limit before create.

Whether `astrograph` also needs a `share_permissions` block (as `netboot` does,
because a DSM share ACL default-DENY overrides the 777 POSIX mode) is a Phase 0
verification item, not an assumption: the share is already reachable over SMB as
`jaxzin`, so a grant may already exist.

Cluster binding, in `platform/seestar-sync/seestar-sync.yaml`:

```yaml
nfs: {server: 192.168.10.7, path: /volume1/astrograph}
mountOptions: [nfsvers=3, nolock, proto=tcp]   # NFSv4 is disabled on the Synology
accessModes: [ReadWriteOnce]
storageClassName: ""                            # static bind, no provisioner
```

Mounted with `subPath: seestar/archive` → `/archive`, so all writes land in the
`777` directory rather than the `755 root:root` share root.

**The kubelet gotcha**, quoted from `platform/pano/pano.yaml:14-19`: *the mount
is performed by kubelet on the node, so the node's IP (not the pod's) must be in
the NAS export ACL.* Pod egress to the LAN is SNAT'd to the node IP by flannel.
This is why the export is a `/24` rather than a single address — the same
widening `pano` already required.

### Workload

A `Deployment`, following `platform/gitea-runner/` (the closest existing
no-ingress, no-OIDC daemon).

- `replicas: 1`, `strategy: {type: Recreate}` — **a safety control, not a
  rollout preference.** Two daemons against one archive with reclaim armed is
  the worst failure this design can produce. `Recreate` guarantees the old pod
  terminates before the new one starts. A `RollingUpdate` would briefly run two.
- `nodeSelector: {kubernetes.io/arch: amd64}` — the kaniko workflow builds amd64
  only. Schedulable onto `nuc` or `tower1`.
- `securityContext: {runAsUser: 1026, runAsGroup: 100, fsGroup: 100}` — matching
  `jaxzin:users` on the NAS. The Dockerfile anticipates this at line 12:
  *"Deliberately no USER directive … k8s deployments should set a
  securityContext/fsGroup instead."*
- Namespace `seestar-sync`, PSA `restricted`, `automountServiceAccountToken:
  false`.
- Env: `SEESTAR_HOST=seestar.local`, `ARCHIVE_DIR=/archive`,
  `SYNC_INTERVAL_MINUTES=15`.

**No PVC for runtime state.** `settings.json`, `status.json`, and the history
JSONL all live under `.seestar-sync/` *inside the archive*, so they ride the NFS
mount. The Home Assistant switches keep working across pod restarts, and the
armed reclaim settings survive the migration untouched.

File layout follows the house three-part convention: `platform/<name>-ns.yaml`
(Namespace, sync-wave `-1`, deliberately in `platform/` because *"a Namespace
grant must not sit in a machine-writable path"*), `platform/<name>.app.yaml`
(ArgoCD Application, sync-wave `0`), and `platform/<name>/<name>.yaml` (the
workload).

### Image build

`.gitea/workflows/build-images.yml`, copied from
`fallen-leaf/obsidian-mcp`. The salient property, quoted from that workflow's
header: *"This fleet's runners deliberately give job containers NO docker daemon
(a job that can drive the daemon is root on the node) … so `docker build` cannot
run here."* The build therefore runs kaniko, in a `kaniko-runner` container,
with no `actions/checkout` (kaniko's image has no node — it uses its own
authenticated git context).

Tagging follows the same precedent: PR → `--no-push`; `main` → `:latest` plus
`:${{ gitea.sha }}`. The Deployment pins the SHA tag, as `obsidian-mcp` does.

### Secrets

One `ExternalSecret` (store `openbao`, kind `ClusterSecretStore`) supplying:

| Key | Source | Note |
|---|---|---|
| `MQTT_USERNAME` / `MQTT_PASSWORD` | `secret/seestar-sync/mqtt` | **new path** — these live only in the Mac `.env` today |
| `KUMA_PUSH_URL` | the push-token path written by the Kuma role | same mechanism obsidian-mcp's `vault-sync` sidecar uses |

Plus a `gitea-pull` dockerconfigjson secret in the namespace — Gitea rejects
anonymous registry reads. Use the ESO-managed style from
`platform/obsidian-mcp/externalsecrets.yaml` (OpenBao path
`infra/gitea/ci-read`), not the hand-applied `home-docs` bootstrap style.

The consumer read policy for `secret/seestar-sync/*` is declared in
`homelab-cluster`, not in the producer — `consumers-own-their-access-policies`.

### Monitoring

**No application code is required.** `KUMA_PUSH_URL` is already a config field
(`src/seestar_sync/config.py`, consumed at `src/seestar_sync/reporters.py:65`);
it is simply unset in the Mac `.env`.

A **push** monitor is correct here, and strictly better than polling the MQTT
LWT: it detects the daemon dying silently, which is the exact failure this
migration exists to prevent. A poll of an external surface cannot distinguish
"daemon dead" from "daemon fine, nothing to do."

It is also already flap-proof. `reporters.py:19` reads
`_KUMA_OK_RESULTS = {"ok", "skipped_unreachable"}` — a pass that finds the
telescope powered off still pings green. This is the failure mode
`deploy/monitors.yaml` documents at length for *Ollama (Toothless)*: a monitor
that manufactures red for a service that is intentionally down. Already solved
in code.

```yaml
- name: "Seestar Sync"
  type: push
  push_token_name: seestar-sync
  description: "Telescope → NAS mirror daemon (k3s). Pings once per sync pass."
  interval: 1800        # heartbeat every 15 min; alert after 30
  retryInterval: 300
  maxretries: 1
  tags: [astro, k3s]
```

## Cutover

| Phase | Action | Gate before proceeding |
|---|---|---|
| 0a | Generalize `roles/nfs_share` (D4), no data change | validate-mode run shows `netboot`'s effective export list byte-identical to before the refactor; a second run reports `changed=0` |
| 0b | NFS export lands on `astrograph` | `mount -t nfs` from `nuc` succeeds; write **and delete** a probe file as uid 1026; confirm it lands `jaxzin:users` |
| 1 | Repo transfer; kaniko workflow | build green on the NAS runner; image pullable by SHA tag |
| 2 | Deploy shadow: `DRY_RUN=true`, `MQTT_TOPIC_PREFIX=seestar-sync-k8s`, `KUMA_PUSH_URL` unset | pod Ready; file count matches the Mac's; planned bytes match; **zero writes to the archive** |
| 3 | `docker compose down` on the Mac, **then** promote the pod to the production prefix + Kuma URL | Mac `seestar-sync/availability` confirmed `offline` *before* the pod arms |
| 4 | Clear shadow retained topics | 0 topics under `seestar-sync-k8s/` |
| 5 | Update `platform/README.md` contents table and the vault service map | a service the inventory does not know is undiscoverable to the next agent |

**Phase 3's ordering is load-bearing.** The armed `settings.json`
(`dry_run: false, reclaim_enabled: true`) lives in the shared archive, so the
pod inherits deletion authority the moment it starts on the production prefix.
The Mac must be confirmed dead first, or two daemons delete against one archive.

Phase 4's cleanup has a precedent: the same programmatic clearing of retained
topics (publishing empty payloads to each) was used for the `seestar-sync-livetest`
prefix on 2026-07-15.

## Risks

1. **NFS squash mapping.** If the Synology export squashes uids, the pod writes
   as `nobody` — the copy fails, or produces files with wrong ownership.
   *Detector:* the Phase 0 probe-file gate, before any real data moves.
2. **Widened export.** `astrograph` becomes LAN-mountable read-write with no
   per-workload auth. Accepted under D1; recorded here so it is not rediscovered
   as a surprise.
3. **Re-copy storm.** If mtimes as seen over NFS differ from what the SMB mount
   reported, the manifest may judge files uncopied and re-copy ~86 GB.
   *Detector:* the Phase 2 dry-run count comparison against the Mac.
4. **Regressing the Pi fleet's boot mount.** The D4 refactor rewrites the code
   path that converges `netboot`'s export — the NFS root every netbooted Pi
   pages at boot. A mistake here does not break seestar-sync; it breaks the
   fleet. *Detector:* the Phase 0a byte-identical comparison, in validate mode,
   before any data change. Note the role has no captured run-twice `changed=0`
   evidence today (`docs/migration-from-homelab-netboot.md:119` explicitly
   defers it), so Phase 0a establishes that baseline for the first time.
5. **Single-node NFS dependency.** If the NAS is down when the pod starts, the
   mount fails and the pod will not become Ready. This is not a regression — the
   Mac deployment had the same dependency via the SMB mount, and the sentinel
   guard exits rather than writing into ephemeral storage.

## Out of scope

- **Retiring the Mac compose path.** `docker-compose.yml` stays in the repo as
  the local-development and disaster-recovery path. Only the *production*
  deployment moves.
- **Multi-arch images.** amd64 only, matching the existing kaniko precedent. The
  daemon does not need to run on the Pis or orin.
- **The orphaned `.smbdelete*` files** observed in
  `/volume1/astrograph/seestar/archive/.seestar-sync/` (dated 2026-07-12).
  Filed separately; unrelated to this migration.

## Verification

Per the homelab conventions, each claim below needs evidence pasted into its PR,
not assertion:

- Repo URL shows `fallen-leaf/`, and its first CI run resolves the org secrets.
- The kaniko workflow is picked up by the NAS runner (`runs-on: ubuntu-latest`)
  and goes green.
- A token from the new OpenBao role reads `secret/seestar-sync/*` and gets 403
  outside it.
- `ClusterSecretStore` = Valid; the `ExternalSecret` = SecretSynced.
- Each cutover phase's gate above, with its command output.
- Every PR targets `main` from a `claude/*` branch; `main` is never pushed to.
