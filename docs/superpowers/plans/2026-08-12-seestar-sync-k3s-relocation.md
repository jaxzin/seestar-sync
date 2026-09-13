# seestar-sync k3s Relocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the `seestar-sync` daemon off hand-run docker-compose on a Mac and onto the k3s cluster, with an Uptime Kuma push monitor so a silent death is never again invisible.

**Architecture:** The NAS gains an NFS export for the `astrograph` share; the cluster binds it with a static PV and runs the daemon as a single-replica Deployment. The image is built by kaniko in Gitea Actions. Secrets arrive via External Secrets Operator from OpenBao. Cutover is a dry-run shadow on a separate MQTT topic prefix, then a hard switch once the Mac instance is confirmed dead.

**Tech Stack:** Ansible (DSM Web API), k3s + ArgoCD, External Secrets Operator + OpenBao, kaniko, Gitea Actions, Python 3.12 / uv / pytest.

**Spec:** `docs/superpowers/specs/2026-08-12-seestar-sync-k3s-relocation-design.md`

## Global Constraints

- **Branch + PR always.** Every repo change goes on a `claude/<topic>` branch with a PR to `main`. Never push to `main`. Brian reviews and merges.
- **Gitea Actions runner labels:** `ubuntu-latest` for general jobs, `ubuntu-latest-amd64` for the kaniko build. There is no runner labeled `instance` — a job requesting it queues forever.
- **`astral-sh/setup-uv` is unusable** on these runners: it requires node24, which the self-hosted runner does not support. Install uv with the shell installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- **Images are amd64 only.** kaniko builds amd64; `orin` and the Pis are arm64.
- **Never put a secret value in git.** Only `ExternalSecret` references.
- **NAS is `192.168.10.7`. Telescope is `seestar.local` / `192.168.10.50`. MQTT broker is `homeassistant.iot.jaxzin.com` / `192.168.10.240`.** All on one flat `192.168.10.0/24`.
- **The archive is irreplaceable.** No task may run the daemon with `DRY_RUN=false` against the NFS mount until Task 8.

---

## Task 1: Generalize `roles/nfs_share` to loop over exported shares

**Repo:** `fallen-leaf/nas-setup` — branch `claude/nfs-share-loop`

**Why this is first and separate:** this refactors the code path that converges `netboot`'s export — the NFS root every netbooted Pi pages at boot. A mistake breaks the fleet, not seestar-sync. It must be proven inert before any data changes.

**Files:**
- Modify: `ansible/roles/nfs_share/defaults/main.yml`
- Create: `ansible/roles/nfs_share/tasks/converge_share.yml`
- Modify: `ansible/roles/nfs_share/tasks/main.yml`

**Interfaces:**
- Consumes: `nas.storage.shares` from `nas.yml` (loaded in `ansible/group_vars/all.yml:6-7`).
- Produces: a role that converges *every* share carrying an `nfs_exports` key, rather than the single hardcoded `netboot`.

- [ ] **Step 1: Capture the pre-refactor baseline**

This is the safety net for the whole task. Run the existing role in validate mode and save the effective export list.

```bash
cd ~/Projects/nas-setup
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
export OPENBAO_TOKEN=$(bao token create -field=token -ttl=30m)
ansible-playbook ansible/playbook.yml --tags nfs 2>&1 | tee /tmp/nfs-baseline.txt
```

Expected: the play completes, `changed=0`, and the task "Verify the effective NFS rules EXACTLY match the desired rules" reports `ok`. Extract the SURFACE dump of the effective rules:

```bash
grep -A40 'SURFACE' /tmp/nfs-baseline.txt > /tmp/nfs-baseline-surface.txt
wc -l /tmp/nfs-baseline-surface.txt
```

If `changed` is not 0, **stop** — the live state already differs from `nas.yml` and that must be understood before refactoring.

- [ ] **Step 2: Move the role body into a looped include**

`git mv` preserves the history of a file that is otherwise "deleted and recreated":

```bash
cd ~/Projects/nas-setup
git checkout -b claude/nfs-share-loop
git mv ansible/roles/nfs_share/tasks/main.yml ansible/roles/nfs_share/tasks/converge_share.yml
```

- [ ] **Step 3: Write the new looping `main.yml`**

Create `ansible/roles/nfs_share/tasks/main.yml`:

```yaml
---
# Converge EVERY nas.yml share that declares nfs_exports.
#
# WHY THIS LOOPS. roles/dsm_shares selects the complement of this set
# (`rejectattr('nfs_exports', 'defined')`), so nas.yml's stated invariant — "the
# two roles partition this list by that key" — is only true if this role picks up
# every share carrying the key. It used to converge one hardcoded name, so adding
# nfs_exports to a second share would have removed it from dsm_shares WITHOUT
# adding it here, leaving it managed by neither role. Found while adding the
# astrograph export (seestar-sync k3s relocation, 2026-08-12).
#
# The per-share body is unchanged in converge_share.yml — same read -> diff ->
# apply, same exact-match read-back verify. Only the driver is new.
- name: Converge each NFS-exporting share
  ansible.builtin.include_tasks: converge_share.yml
  loop: "{{ nas.storage.shares | default([]) | selectattr('nfs_exports', 'defined') | list }}"
  loop_control:
    loop_var: nfs_share_entry
    label: "{{ nfs_share_entry.name }}"
```

- [ ] **Step 4: Repoint `converge_share.yml` at the loop variable**

The old body derived its share by *searching* `nas.yml` for `nfs_share_lookup`. The loop already hands it the entry, so that search is now redundant indirection. Replace the derivation task at the top of `ansible/roles/nfs_share/tasks/converge_share.yml` (was `main.yml:12-25`) with:

```yaml
- name: Derive this share's desired state from the loop entry
  ansible.builtin.set_fact:
    nfs_share_name: "{{ nfs_share_entry.name }}"
    nfs_share_volume: "{{ nfs_share_entry.volume }}"
    nfs_share_desc: "{{ nfs_share_entry.desc }}"
    nfs_share_rules: "{{ nfs_share_entry.nfs_exports }}"
    # Optional share-level DSM permission grants (SYNO.Core.Share.Permission).
    # Absent -> the permission concern below is a no-op (RULE 2: manage nothing
    # not declared). Present -> per-principal upsert; see converge_permission.yml.
    nfs_share_permissions: "{{ nfs_share_entry.share_permissions | default([]) }}"
```

Leave every other task in the file untouched — the guards, the 7-key projection, the diff, the save, and the exact-match verify all read the `nfs_share_*` facts this sets.

- [ ] **Step 5: Delete the now-dead lookup default**

Replace the whole of `ansible/roles/nfs_share/defaults/main.yml` with:

```yaml
---
# Role-local knobs only. Each share's IDENTITY and desired export state (name,
# volume, desc, export rule list) are sourced from nas.yml `storage.shares` in
# tasks/main.yml (RULE 5 — single source of truth) and are NOT duplicated here.
# The verified rule-object schema (client, privilege, root_squash, async,
# insecure, crossmnt, security_flavor) and the "why" behind each value live
# alongside each share entry in nas.yml and in converge_share.yml where the
# diff/verify consume them.
#
# `nfs_share_lookup` is gone: the role no longer converges one hardcoded share.
# tasks/main.yml loops every nas.yml share that declares nfs_exports.
```

- [ ] **Step 6: Lint**

```bash
cd ~/Projects/nas-setup && ansible-lint --profile production ansible/roles/nfs_share/
```

Expected: no findings.

- [ ] **Step 7: Prove the refactor is inert**

Re-run validate mode and diff against the baseline from Step 1:

```bash
cd ~/Projects/nas-setup
ansible-playbook ansible/playbook.yml --tags nfs 2>&1 | tee /tmp/nfs-after.txt
grep -A40 'SURFACE' /tmp/nfs-after.txt > /tmp/nfs-after-surface.txt
diff /tmp/nfs-baseline-surface.txt /tmp/nfs-after-surface.txt && echo "IDENTICAL — refactor is inert"
```

Expected: `diff` reports no differences, and the run reports `changed=0`.

**This is the gate. If the diff is non-empty, do not proceed — revert and investigate.** The role has no captured run-twice `changed=0` evidence today (`docs/migration-from-homelab-netboot.md:119` explicitly defers it), so this step establishes that baseline for the first time.

- [ ] **Step 8: Run it twice to prove idempotency**

```bash
cd ~/Projects/nas-setup && ansible-playbook ansible/playbook.yml --tags nfs | tail -3
```

Expected final line contains `changed=0`.

- [ ] **Step 9: Commit and open the PR**

```bash
cd ~/Projects/nas-setup
git add ansible/roles/nfs_share/
git commit -m "refactor(nfs_share): converge every share declaring nfs_exports

roles/dsm_shares selects the complement of this set, so nas.yml's stated
partition invariant only held while exactly one share had the key. Adding
nfs_exports to a second share would have removed it from dsm_shares without
adding it here, leaving it managed by neither role.

Per-share body is unchanged (git mv main.yml -> converge_share.yml); only the
driver is new. Proven inert: netboot's effective export list is byte-identical
before and after, changed=0 on a second run."
git push -u origin claude/nfs-share-loop
```

Open a PR to `main`. Paste into the description: the Step 7 `diff` output (empty) and the Step 8 `changed=0` line.

**STOP — Brian merges before Task 2.**

---

## Task 2: Export the `astrograph` share over NFS

**Repo:** `fallen-leaf/nas-setup` — branch `claude/astrograph-nfs-export`

**Files:**
- Modify: `nas.yml:153-156`

**Interfaces:**
- Consumes: the looping `roles/nfs_share` from Task 1.
- Produces: `192.168.10.7:/volume1/astrograph` mountable read-write from any cluster node, with uid 1026 unmapped.

- [ ] **Step 1: Add the export block**

In `nas.yml`, replace the `astrograph` entry:

```yaml
    - name: astrograph
      volume: /volume1
      purpose: "Seestar astrophotography FITS captures — irreplaceable"
      desc: "Seestar astrophotography FITS - salvaged 2026-07-26"
      # NFS export added 2026-08-12 for the seestar-sync k3s relocation. The
      # daemon's archive moves from a Finder SMB mount on a Mac to a static NFS
      # PV in the cluster.
      #
      # root_squash "root" = DSM No-mapping. REQUIRED, not cosmetic: the pod runs
      # as uid 1026 (jaxzin) so copied FITS land as jaxzin:users, exactly as the
      # SMB mount produced them. Any squashing writes them as nobody.
      #
      # client is the whole cluster subnet, not one node, because the mount is
      # performed by KUBELET on whatever node the pod lands on — the node's IP is
      # what the export ACL sees, never the pod's. Same widening platform/pano
      # already required for /volume1/media.
      #
      # crossmnt false: unlike netboot, whose nfsroot is a subfolder, nothing here
      # mounts across a nested mountpoint.
      nfs_exports:
        - client: "192.168.10.0/24"
          privilege: "rw"
          root_squash: "root"
          async: true
          insecure: true
          crossmnt: false
          security_flavor: {sys: true, kerberos: false, kerberos_integrity: false, kerberos_privacy: false}
```

Leave `purpose` and `desc` byte-identical to what is there now — `desc` is at 51 characters and DSM rejects anything over 64 with a bare error 3300.

- [ ] **Step 2: Validate before applying**

```bash
cd ~/Projects/nas-setup && ansible-playbook ansible/playbook.yml --tags nfs 2>&1 | tail -20
```

Expected: the run reports a pending change for `astrograph` and **no** change for `netboot`. In validate mode nothing is written; the exact-match verify will fail for `astrograph` because the export does not exist yet. That failure is expected at this step — confirm the failure names `astrograph` and not `netboot`.

- [ ] **Step 3: Commit and open the PR**

```bash
cd ~/Projects/nas-setup
git checkout -b claude/astrograph-nfs-export
git add nas.yml
git commit -m "feat(nas): export astrograph over NFS for the seestar-sync k3s move

root_squash \"root\" (DSM No-mapping) so the pod's uid 1026 is not squashed and
FITS land as jaxzin:users. Client is the cluster subnet because kubelet performs
the mount from whatever node the pod lands on."
git push -u origin claude/astrograph-nfs-export
```

**STOP — Brian merges. The merge to `main` IS the apply** (`.gitea/workflows/converge.yml` runs with `-e converge=true` on push to main).

- [ ] **Step 4: Verify the export from a cluster node — THE PHASE 0 GATE**

```bash
ssh nuc 'sudo mkdir -p /mnt/astro-probe && \
  sudo mount -t nfs -o nfsvers=3,nolock,proto=tcp 192.168.10.7:/volume1/astrograph /mnt/astro-probe && \
  echo "MOUNTED" && ls /mnt/astro-probe'
```

Expected: `MOUNTED`, then a listing containing `seestar`.

- [ ] **Step 5: Prove uid 1026 is not squashed**

```bash
ssh nuc 'sudo -u "#1026" touch /mnt/astro-probe/seestar/archive/.probe-1026 && \
  stat -c "%U:%G %u:%g" /mnt/astro-probe/seestar/archive/.probe-1026'
```

Expected exactly: `jaxzin:users 1026:100`

If it reports `nobody:nogroup` or `65534`, the export is squashing — **stop**, fix `root_squash`, and do not proceed.

- [ ] **Step 6: Clean up the probe**

```bash
ssh nuc 'sudo rm -f /mnt/astro-probe/seestar/archive/.probe-1026 && \
  sudo umount /mnt/astro-probe && sudo rmdir /mnt/astro-probe && echo CLEAN'
```

Expected: `CLEAN`, and no `.probe-1026` left behind.

---

## Task 3: Transfer the repo and add a test workflow

**Repo:** `jaxzin/seestar-sync` → `fallen-leaf/seestar-sync`

**Why the transfer:** org-level Actions secrets (`REGISTRY_HOSTNAME`, `REGISTRY_USER`, `REGISTRY_TOKEN`) reach only `fallen-leaf/*`. A kaniko build in a user repo cannot authenticate to the registry.

**Files:**
- Create: `.gitea/workflows/ci.yml`

- [ ] **Step 1: Transfer the repo (Brian, in the Gitea UI)**

Settings → Danger Zone → Transfer Ownership → `fallen-leaf`. This is an admin action; it cannot be scripted with the available token.

- [ ] **Step 2: Repoint the local remote**

```bash
cd ~/Projects/seestar-backup
git remote set-url origin git@gitea.forest-draconis.ts.net:fallen-leaf/seestar-sync.git
git remote -v
git fetch origin && echo "TRANSFER OK"
```

Expected: both remote lines show `fallen-leaf/seestar-sync`, and the fetch succeeds.

- [ ] **Step 3: Write the test workflow**

There is no pytest CI in this fleet to copy — `obsidian-mcp` has a test suite and no workflow that runs it. This establishes the pattern, following `obsidian-mcp/.gitea/workflows/scan.yml:40-76` for the uv setup.

Create `.gitea/workflows/ci.yml`:

```yaml
name: CI

# Runs the unit suite on every PR and on main. Gated jobs (the ones that touch
# the real telescope or the real MQTT broker) are excluded — they need hardware
# this runner cannot reach.
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    name: pytest + ruff
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      # Install uv via the official shell installer instead of astral-sh/setup-uv,
      # which requires node24 — a runtime the self-hosted runner does not support.
      # The shell installer is runner-version-agnostic. Same constraint documented
      # in obsidian-mcp's scan.yml.
      - name: Install uv
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          echo "$HOME/.local/bin" >> "$GITHUB_PATH"

      - name: Sync dependencies
        run: uv sync --frozen --extra dev

      - name: Lint
        run: uv run ruff check src/ tests/

      # -m "not live" excludes the 5 gated tests that need the real S30 Pro and
      # the real broker.
      - name: Test
        run: uv run pytest -m "not live" -q
```

- [ ] **Step 4: Verify the marker name before trusting the workflow**

The `-m "not live"` filter is only correct if that is the actual marker. Check:

```bash
cd ~/Projects/seestar-backup && grep -rn "markers" pyproject.toml && grep -rn "@pytest.mark" tests/ | grep -oE "@pytest\.mark\.[a-z_]+" | sort -u
```

If the marker is not `live`, edit `ci.yml` to match what is actually there. Do not guess.

- [ ] **Step 5: Run the suite locally first**

Reproduce CI locally before iterating through pushes:

```bash
cd ~/Projects/seestar-backup && uv run pytest -m "not live" -q 2>&1 | tail -5
```

Expected: `132 passed` (or the current count), 0 failures.

- [ ] **Step 6: Commit and open the PR**

```bash
cd ~/Projects/seestar-backup
git checkout -b claude/ci-workflow
git add .gitea/workflows/ci.yml
git commit -m "ci: run the unit suite on every PR

No pytest workflow existed in the fleet to copy. Follows obsidian-mcp scan.yml's
uv setup: the shell installer, not astral-sh/setup-uv, which needs node24 that
the self-hosted runner does not support."
git push -u origin claude/ci-workflow
```

Verify the run is picked up by the runner and goes green before asking for a merge.

---

## Task 4: Build and publish the image from CI

**Repo:** `fallen-leaf/seestar-sync` — branch `claude/kaniko-build`

**Files:**
- Create: `.gitea/workflows/build-images.yml`

**Interfaces:**
- Produces: `gitea.forest-draconis.ts.net/fallen-leaf/seestar-sync:<sha>` and `:latest`. Task 7's Deployment pins the `<sha>` tag.

- [ ] **Step 1: Write the build workflow**

Copied from `fallen-leaf/obsidian-mcp/.gitea/workflows/build-images.yml`, reduced to a single image (no matrix).

Create `.gitea/workflows/build-images.yml`:

```yaml
name: Build & push image

# DAEMONLESS build via kaniko. This fleet's runners deliberately give job
# containers NO docker daemon (a job that can drive the daemon is root on the
# node — homelab-cluster#100). So `docker build` cannot run here; kaniko builds
# AND pushes over plain registry HTTP with no daemon, running AS the job
# container (fallen-leaf/kaniko-runner). amd64 ONLY — the image runs on the NUC
# or tower1; orin and the Pis are arm64.
#
# kaniko's image has no node, so `actions/checkout` cannot run in it; the build
# uses kaniko's built-in authenticated git context instead (no checkout step —
# deliberate, not an omission).
on:
  push:
    branches: [main]
    paths:
      - 'src/**'
      - 'Dockerfile'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.gitea/workflows/build-images.yml'
  pull_request:
    paths:
      - 'src/**'
      - 'Dockerfile'
      - 'pyproject.toml'
      - 'uv.lock'
      - '.gitea/workflows/build-images.yml'
  workflow_dispatch:

concurrency:
  group: build-images-${{ github.ref }}
  cancel-in-progress: true

jobs:
  build:
    name: "Build & push seestar-sync (amd64, kaniko)"
    runs-on: ubuntu-latest-amd64
    container:
      image: ${{ secrets.REGISTRY_HOSTNAME }}/fallen-leaf/kaniko-runner:latest
      credentials:
        username: ${{ secrets.REGISTRY_USER }}
        password: ${{ secrets.REGISTRY_TOKEN }}
    steps:
      - name: Build and push
        env:
          REGISTRY: ${{ secrets.REGISTRY_HOSTNAME }}
          REGISTRY_USER: ${{ secrets.REGISTRY_USER }}
          REGISTRY_TOKEN: ${{ secrets.REGISTRY_TOKEN }}
          # kaniko reads these for an authenticated git context.
          GIT_USERNAME: ${{ secrets.REGISTRY_USER }}
          GIT_PASSWORD: ${{ secrets.REGISTRY_TOKEN }}
        run: |
          set -eu
          mkdir -p /kaniko/.docker
          # Written, never echoed. Do NOT add `set -x` to this step.
          auth=$(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_TOKEN" | base64 | tr -d '\n')
          printf '{"auths":{"%s":{"auth":"%s"}}}' "$REGISTRY" "$auth" > /kaniko/.docker/config.json

          img="$REGISTRY/fallen-leaf/seestar-sync"
          # PR builds validate the Dockerfile without publishing. `:latest` is
          # authoritative only from main; every other build publishes just the
          # immutable commit-sha tag so it never clobbers `:latest`.
          if [ "${{ gitea.event_name }}" = "pull_request" ]; then
            set -- --no-push
          elif [ "$GITHUB_REF" = "refs/heads/main" ]; then
            set -- --destination "$img:latest" --destination "$img:${{ gitea.sha }}"
          else
            set -- --destination "$img:${{ gitea.sha }}"
          fi

          /kaniko/executor \
            --context "git://$REGISTRY/fallen-leaf/seestar-sync.git#${{ gitea.sha }}" \
            --dockerfile "Dockerfile" \
            --digest-file /kaniko/digest.txt \
            "$@"

          if [ "${{ gitea.event_name }}" != "pull_request" ]; then
            printf '## seestar-sync\n- `%s@%s`\n' \
              "$img" "$(cat /kaniko/digest.txt)" \
              >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
          fi
```

- [ ] **Step 2: Commit and open the PR**

```bash
cd ~/Projects/seestar-backup
git checkout -b claude/kaniko-build
git add .gitea/workflows/build-images.yml
git commit -m "ci: build and push the image with kaniko

The runners have no docker daemon by design, so docker build cannot run here.
amd64 only; the pod is nodeSelector-pinned to amd64."
git push -u origin claude/kaniko-build
```

- [ ] **Step 3: Verify the PR build validates without publishing**

Expected: the job goes green and the log shows kaniko running with `--no-push`. Confirm no `seestar-sync` tag appeared in the registry.

- [ ] **Step 4: After merge, record the published tag**

```bash
cd ~/Projects/seestar-backup && git fetch origin && git rev-parse origin/main
```

Save that SHA — Task 7's Deployment pins it.

---

## Task 5: Grant ESO read access to the two new secret paths

**Repo:** `fallen-leaf/homelab-cluster` — branch `claude/seestar-sync-eso-grant`

**Files:**
- Modify: `platform/openbao-config.yaml` (the `policy-eso-openbao.hcl` block)

**⚠️ THE SINGLE MOST DANGEROUS STEP IN THIS PLAN.** The converger `PUT`s the whole policy to `sys/policies/acl/eso-openbao`, which **REPLACES** it. The block must carry the *complete* policy plus the new paths. Trimming it to "just the new bit" silently revokes five live consumers simultaneously: `argocd/argocd-oidc`, `headlamp/headlamp-oidc`, `open-webui/open-webui-oidc`, `uptime-kuma/uptime-kuma-outpost`, and `gitea-runner-bootstrap/gitea-runner-token-minter`.

Note also that `policies/eso-openbao.hcl` (3 paths) is **already out of sync** with the ConfigMap block (6 paths). The ConfigMap is what actually gets applied. Edit the ConfigMap; do not "fix" the file to match in this PR — that drift is tracked separately and conflating them here widens the blast radius.

**Interfaces:**
- Produces: ESO can read `secret/data/infra/seestar-sync/mqtt` and `secret/data/infra/uptime-kuma/seestar-sync-push`.

- [ ] **Step 1: Capture the live policy before touching anything**

```bash
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
bao policy read eso-openbao > /tmp/eso-openbao-before.hcl
grep -c '^path' /tmp/eso-openbao-before.hcl
```

Expected: `6`. If it is not 6, the ConfigMap and live state have drifted — investigate before editing.

- [ ] **Step 2: Append the two grants**

In `platform/openbao-config.yaml`, inside the existing `policy-eso-openbao.hcl: |` block, **after** the last existing `path` stanza and without altering any of the six already there, add:

```
    # seestar-sync (fallen-leaf/seestar-sync; k3s relocation 2026-08-12). Two reads:
    #   - infra/seestar-sync/mqtt → the broker username/password the daemon needs
    #     at startup (it refuses to run against an anonymous broker by design).
    #   - infra/uptime-kuma/seestar-sync-push → the push_url the daemon pings once
    #     per sync pass. Seeded by hand, like every other Kuma push token; the
    #     converge Job's own policy is deliberately read-only.
    #
    # ⚠️ Both sit under secret/data/infra/*, which the `ci-runner` AppRole wildcard
    # still covers — so today these are readable by every org repo's CI, not just
    # ESO. That is pre-existing exposure (openbao-infra#324/#335/#352; PR #351
    # narrows the wildcard to an explicit allowlist), NOT something this grant
    # introduces. When #351 lands, both paths must be put on the allowlist or left
    # off deliberately. The blast radius is bounded: a home MQTT account and a
    # heartbeat URL, neither of which grants access to the archive.
    path "secret/data/infra/seestar-sync/mqtt" {
      capabilities = ["read"]
    }
    path "secret/data/infra/uptime-kuma/seestar-sync-push" {
      capabilities = ["read"]
    }
```

No `roles.json` entry is needed. The `eso-openbao` kubernetes-auth role already exists and its params are correct; adding a role entry would `PUT` the role too, and any drift between those params and the live ones would silently re-scope ESO's login.

- [ ] **Step 3: Verify you added rather than replaced**

```bash
cd ~/Projects/homelab-cluster
awk '/policy-eso-openbao.hcl: \|/,/^  [a-z-]+\.(hcl|json): \|/' platform/openbao-config.yaml | grep -c '^    path'
```

Expected: `8` (the original 6 plus 2). **If it is 2, you replaced the policy instead of appending — revert.**

- [ ] **Step 4: Commit and open the PR**

```bash
cd ~/Projects/homelab-cluster
git checkout -b claude/seestar-sync-eso-grant
git add platform/openbao-config.yaml
git commit -m "feat(openbao): grant ESO the two seestar-sync read paths

The converger PUTs the whole policy, so this carries all six existing grants
plus the two new ones. Verified 8 path stanzas after the edit.

Both new paths sit under infra/*, still covered by the ci-runner wildcard —
pre-existing exposure tracked in openbao-infra#351, flagged in-line so they get
an allowlist decision when it lands."
git push -u origin claude/seestar-sync-eso-grant
```

- [ ] **Step 5: After merge, verify all eight grants are live**

```bash
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
bao policy read eso-openbao > /tmp/eso-openbao-after.hcl
grep -c '^path' /tmp/eso-openbao-after.hcl
diff /tmp/eso-openbao-before.hcl /tmp/eso-openbao-after.hcl
```

Expected: count is `8`, and the diff shows **only additions**, never a removal.

- [ ] **Step 6: Confirm no consumer was broken**

```bash
kubectl get externalsecrets -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,STATUS:.status.conditions[0].reason
```

Expected: every row reads `SecretSynced`. Any `SecretSyncedError` means a grant was lost — revert immediately.

---

## Task 6: Declare the Kuma push monitor

**Repo:** `fallen-leaf/uptime-kuma` — branch `claude/seestar-sync-monitor`

**Files:**
- Modify: `ansible/roles/uptime-kuma/defaults/main.yml:112-117`
- Modify: `deploy/monitors.yaml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: OpenBao secret `secret/data/infra/uptime-kuma/seestar-sync-push` with keys `token` and `push_url`, where `push_url` is `https://status.jaxzin.casa/api/push/<token>`.

- [ ] **Step 1: Add the token spec**

In `ansible/roles/uptime-kuma/defaults/main.yml`, extend `openbao_push_token_specs`:

```yaml
openbao_push_token_specs:
  - name: obsidian-vault-sync
    openbao_path: "infra/uptime-kuma/obsidian-vault-sync-push"
  - name: seestar-sync
    openbao_path: "infra/uptime-kuma/seestar-sync-push"
```

- [ ] **Step 2: Seed the token by hand — ADMIN-GATED**

The in-cluster converge Job's policy is read-only on purpose. From the deleted `openbao-role.sh`, verbatim: *"Granting write for that one-time-per-monitor case would give a daily CronJob standing write access to the whole prefix. Instead: seed push tokens by hand … Fail-closed beats standing privilege."*

Brian runs this — it needs a token with write capability:

```bash
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
TOKEN=$(openssl rand -hex 16)
bao kv put secret/infra/uptime-kuma/seestar-sync-push \
  token="$TOKEN" \
  push_url="https://status.jaxzin.casa/api/push/$TOKEN"
```

Verify without printing the value:

```bash
bao kv get -format=json secret/infra/uptime-kuma/seestar-sync-push \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]["data"]; print("keys:", sorted(d)); print("token len:", len(d["token"])); print("url host:", d["push_url"].split("/api/")[0])'
```

Expected: `keys: ['push_url', 'token']`, `token len: 32`, `url host: https://status.jaxzin.casa`.

- [ ] **Step 3: Declare the monitor**

In `deploy/monitors.yaml`, add under a suitable section:

```yaml
  # seestar-sync (fallen-leaf/seestar-sync, k3s). A PUSH monitor, not an HTTP
  # one: the daemon has no listening port, and the failure worth catching is it
  # dying silently — which is exactly what happened for 26 days from 2026-07-17
  # (Docker Desktop stopped on the Mac it used to run on; nothing noticed).
  #
  # It does not flap when the telescope is off. reporters.py pings for BOTH
  # `ok` and `skipped_unreachable`, so a pass that finds the S30 powered down
  # still reports healthy — deliberately avoiding the manufactured-red problem
  # documented on Ollama (Toothless) above.
  #
  # interval 1800 with the daemon on a 15-minute cycle: one missed pass is
  # tolerated, two is an alert.
  - name: "Seestar Sync"
    type: push
    push_token_name: seestar-sync
    description: "Telescope → NAS mirror daemon (k3s). Pings once per sync pass."
    interval: 1800
    retryInterval: 300
    maxretries: 1
    tags:
      - astro
      - k3s
```

Note `tags` is informational only — the role parses it but never applies it to Kuma. It is included to match the file's convention.

- [ ] **Step 4: Lint**

```bash
cd ~/Projects/uptime-kuma && ansible-lint --profile production && python3 -c "import yaml; yaml.safe_load(open('deploy/monitors.yaml'))" && echo "YAML OK"
```

Expected: no findings, `YAML OK`.

- [ ] **Step 5: Commit and open the PR**

```bash
cd ~/Projects/uptime-kuma
git checkout -b claude/seestar-sync-monitor
git add ansible/roles/uptime-kuma/defaults/main.yml deploy/monitors.yaml
git commit -m "feat(monitors): watch seestar-sync via a push heartbeat

A push monitor, not HTTP: the daemon has no port, and the failure worth
catching is silent death — which went unnoticed for 26 days from 2026-07-17.
Does not flap when the telescope is off; reporters.py pings green for
skipped_unreachable too."
git push -u origin claude/seestar-sync-monitor
```

- [ ] **Step 6: After merge, verify the monitor exists and is red**

The converge Job runs on sync. Once it completes, the monitor should exist and be **DOWN** — nothing is pinging it yet. That is correct at this stage; it goes green in Task 8.

---

## Task 7: Deploy the shadow pod (dry-run, isolated topics)

**Repo:** `fallen-leaf/homelab-cluster` — branch `claude/seestar-sync-platform`

**Files:**
- Create: `platform/seestar-sync-ns.yaml`
- Create: `platform/seestar-sync.app.yaml`
- Create: `platform/seestar-sync/seestar-sync.yaml`
- Create: `platform/seestar-sync/externalsecrets.yaml`

**Interfaces:**
- Consumes: the NFS export (Task 2), the image tag (Task 4), the ESO grants (Task 5), the push secret (Task 6).
- Produces: a running pod that **plans but never writes**, publishing to `seestar-sync-k8s/*`.

- [ ] **Step 1: Seed the MQTT credential — ADMIN-GATED**

The values are in the Mac's `.env` and nowhere else. Brian runs this; read the two values from `~/Projects/seestar-backup/.env` without echoing them:

```bash
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
cd ~/Projects/seestar-backup
bao kv put secret/infra/seestar-sync/mqtt \
  username="$(grep '^MQTT_USERNAME=' .env | cut -d= -f2-)" \
  password="$(grep '^MQTT_PASSWORD=' .env | cut -d= -f2-)"
```

Verify without printing the password:

```bash
bao kv get -format=json secret/infra/seestar-sync/mqtt \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]["data"]; print("keys:", sorted(d)); print("user:", d["username"]); print("pw len:", len(d["password"]))'
```

Expected: `keys: ['password', 'username']`, `user: seestar-sync`, and a non-zero password length.

- [ ] **Step 2: Write the namespace**

Create `platform/seestar-sync-ns.yaml`:

```yaml
# seestar-sync namespace + default ServiceAccount.
#
# Lives in platform/ (parent app-of-apps, recurse:false), NOT the service subdir
# — same reasoning as obsidian-mcp and home-docs: a Namespace grant must not sit
# in a machine-writable path. The SA mounts no token (the pod needs zero k8s API)
# and carries the gitea-pull imagePullSecret (Gitea rejects anonymous registry
# reads instance-wide).
#
# PSA restricted, not baseline: every container here runs non-root with a
# read-only rootfs and all caps dropped. Nothing needs the exemptions
# obsidian-mcp's tailscale sidecar forced.
apiVersion: v1
kind: Namespace
metadata:
  name: seestar-sync
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: default
  namespace: seestar-sync
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
automountServiceAccountToken: false
imagePullSecrets:
  - name: gitea-pull
```

- [ ] **Step 3: Write the ArgoCD Application**

Create `platform/seestar-sync.app.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: platform-seestar-sync
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "0"
spec:
  project: default
  source:
    repoURL: git@gitea.forest-draconis.ts.net:fallen-leaf/homelab-cluster.git
    targetRevision: refs/heads/main
    path: platform/seestar-sync
  destination:
    server: https://kubernetes.default.svc
    namespace: seestar-sync
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
    syncOptions:
      - CreateNamespace=false
```

- [ ] **Step 4: Write the ExternalSecrets**

Create `platform/seestar-sync/externalsecrets.yaml`:

```yaml
# seestar-sync ExternalSecrets — values from OpenBao via the `openbao`
# ClusterSecretStore. Only references live here; secret values never in git.
#
# gitea-pull: the kubelet's registry pull credential (Gitea rejects anonymous
# reads instance-wide). Built from the shared read:package PAT at
# secret/data/infra/gitea/ci-read — the same token obsidian-mcp and home-docs use.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: gitea-pull
  namespace: seestar-sync
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: gitea-pull
    template:
      type: kubernetes.io/dockerconfigjson
      data:
        .dockerconfigjson: >-
          {"auths":{"gitea.forest-draconis.ts.net":{"auth":"{{ printf "jaxzin:%s" .token | b64enc }}"}}}
  data:
    - secretKey: token
      remoteRef:
        key: infra/gitea/ci-read
        property: token
---
# The broker account. seestar-sync refuses to start against an anonymous broker
# (architect-ruled), so both keys are mandatory — a missing one is a hard config
# error at startup, not a silent downgrade.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: seestar-sync-mqtt
  namespace: seestar-sync
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: seestar-sync-mqtt
    template:
      data:
        MQTT_USERNAME: "{{ .username }}"
        MQTT_PASSWORD: "{{ .password }}"
  data:
    - secretKey: username
      remoteRef: { key: infra/seestar-sync/mqtt, property: username }
    - secretKey: password
      remoteRef: { key: infra/seestar-sync/mqtt, property: password }
---
# The Kuma push URL, seeded by hand alongside the monitor declaration in
# fallen-leaf/uptime-kuma. The URL embeds the token, so the whole value is the
# credential — hence OpenBao rather than a plain env var in git.
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: seestar-sync-kuma
  namespace: seestar-sync
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: seestar-sync-kuma
    template:
      data:
        KUMA_PUSH_URL: "{{ .push_url }}"
  data:
    - secretKey: push_url
      remoteRef: { key: infra/uptime-kuma/seestar-sync-push, property: push_url }
```

- [ ] **Step 5: Write the workload**

Substitute `<SHA>` with the commit SHA recorded in Task 4 Step 4.

Create `platform/seestar-sync/seestar-sync.yaml`:

```yaml
# seestar-sync — mirrors the Seestar S30 Pro's media library to the NAS over SMB
# and reclaims device space once copies verify. Moved here from hand-run
# docker-compose on a Mac after a 26-day silent outage (2026-07-17 → 08-12):
# Docker Desktop stopped, the container exited 0, and nothing was watching.
#
# Liveness is the Uptime Kuma PUSH monitor (fallen-leaf/uptime-kuma
# deploy/monitors.yaml, "Seestar Sync"), not a k8s probe — the daemon has no
# listening port, and a probe on this pod could not detect the failure that
# actually matters (the sync silently stopping while the process lives).
---
# Static NFS PV. In-tree nfs volume: the mount is performed by kubelet on the
# node, so the NODE's IP (not the pod's) must be in the NAS export ACL — which is
# why nas-setup exports astrograph to 192.168.10.0/24 rather than one address.
# storageClassName "" + explicit volumeName binds this PVC to this PV with no
# dynamic provisioner involved.
#
# NO sync-wave annotation on either object, matching platform/pano. The
# local-path deadlock warnings elsewhere in this repo are specific to
# WaitForFirstConsumer dynamic provisioning; this is a static bind. Leaving both
# unwaved keeps them at the default wave with their consumer regardless.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: seestar-archive
  labels:
    app: seestar-sync
spec:
  capacity:
    storage: 1Gi          # required by the API but ignored for NFS
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  mountOptions:
    - nfsvers=3           # NFSv4 is disabled on the Synology
    - nolock
    - proto=tcp
  nfs:
    server: 192.168.10.7
    path: /volume1/astrograph
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: seestar-archive
  namespace: seestar-sync
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ""
  volumeName: seestar-archive
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seestar-sync
  namespace: seestar-sync
spec:
  replicas: 1
  # Recreate is a SAFETY CONTROL here, not a rollout preference. Two daemons
  # against one archive with reclaim armed is the worst failure this design can
  # produce; RollingUpdate would briefly run exactly that. Do not "improve" this.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: seestar-sync
  template:
    metadata:
      labels:
        app: seestar-sync
    spec:
      # kaniko builds amd64 only; orin and the Pis are arm64.
      nodeSelector:
        kubernetes.io/arch: amd64
      automountServiceAccountToken: false
      securityContext:
        # uid 1026 / gid 100 = jaxzin:users on the NAS. The export uses
        # root_squash "root" (DSM No-mapping) so this uid passes through
        # unmapped and copied FITS land with the same ownership the old SMB
        # mount produced. The Dockerfile deliberately has no USER directive and
        # says so: "k8s deployments should set a securityContext/fsGroup".
        runAsUser: 1026
        runAsGroup: 100
        runAsNonRoot: true
        fsGroup: 100
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: seestar-sync
          image: gitea.forest-draconis.ts.net/fallen-leaf/seestar-sync:<SHA>
          envFrom:
            - secretRef: { name: seestar-sync-mqtt }
          env:
            - { name: SEESTAR_HOST, value: "seestar.local" }
            - { name: ARCHIVE_DIR, value: "/archive" }
            - { name: SYNC_INTERVAL_MINUTES, value: "15" }
            - { name: MQTT_HOST, value: "homeassistant.iot.jaxzin.com" }
            - { name: MQTT_PORT, value: "1883" }
            # ── SHADOW PHASE SETTINGS — changed in the cutover task ──
            # DRY_RUN=true: plan only, never write to the archive or the device.
            # The isolated topic prefix keeps this pod's discovery messages from
            # colliding with the live Mac daemon's device in Home Assistant.
            # KUMA_PUSH_URL is deliberately NOT wired yet — a shadow pod must not
            # keep the monitor green.
            - { name: DRY_RUN, value: "true" }
            - { name: MQTT_TOPIC_PREFIX, value: "seestar-sync-k8s" }
          securityContext:
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            # subPath: the share root is 755 root:root; seestar/archive is 777
            # jaxzin:users, so all writes land in the permissive directory.
            - name: archive
              mountPath: /archive
              subPath: seestar/archive
            - name: tmp
              mountPath: /tmp
          resources:
            requests: { cpu: 100m, memory: 128Mi }
            limits: { memory: 512Mi }
      volumes:
        - name: archive
          persistentVolumeClaim:
            claimName: seestar-archive
        - name: tmp
          emptyDir: {}
```

- [ ] **Step 6: Validate the manifests before pushing**

```bash
cd ~/Projects/homelab-cluster
kubectl apply --dry-run=client -f platform/seestar-sync/ -f platform/seestar-sync-ns.yaml
python3 scripts/lint_sync_waves.py platform/
```

Expected: every object validates, and the sync-wave linter passes.

- [ ] **Step 7: Commit and open the PR**

```bash
cd ~/Projects/homelab-cluster
git checkout -b claude/seestar-sync-platform
git add platform/seestar-sync-ns.yaml platform/seestar-sync.app.yaml platform/seestar-sync/
git commit -m "feat(platform): deploy seestar-sync (shadow, dry-run)

Static NFS PV onto astrograph, single replica with Recreate as a safety control
against two daemons sharing one archive. Ships in DRY_RUN with an isolated MQTT
topic prefix and no Kuma URL; the cutover flips all three."
git push -u origin claude/seestar-sync-platform
```

- [ ] **Step 8: After merge, verify the pod is Ready and the mount worked**

```bash
kubectl -n seestar-sync get pods -o wide
kubectl -n seestar-sync exec deploy/seestar-sync -- ls -la /archive/.seestar-sync/
```

Expected: pod `Running`/`Ready`, and the listing shows `ARCHIVE_MARKER`, `manifest.db`, `settings.json`, `status.json`. If the pod is `ContainerCreating` with a mount error, the export ACL is wrong — revisit Task 2.

- [ ] **Step 9: THE PHASE 2 GATE — compare the shadow's plan to the Mac's**

```bash
kubectl -n seestar-sync logs deploy/seestar-sync --tail=40
```

Expected: a line like `listed NNNN files in N.Ns`, then dry-run planning output. Compare `NNNN` against what the Mac daemon reports for the same period:

```bash
docker logs --tail 40 seestar-backup-seestar-sync-1 2>&1 | grep listed
```

**The two file counts must match.** A materially larger count from the pod means NFS-visible mtimes differ from the SMB ones and the manifest thinks files need re-copying — the ~86 GB re-copy storm the spec flags as Risk 3. Investigate before cutover.

- [ ] **Step 10: Prove the shadow wrote nothing**

```bash
ssh nas.local 'find /volume1/astrograph/seestar/archive -newermt "-1 hour" -type f | head'
```

Expected: no output beyond files the *Mac* daemon touched. A `.part` file or a new directory attributable to the pod means `DRY_RUN` is not taking effect — **stop**.

---

## Task 8: Cut over

**Repos:** `fallen-leaf/homelab-cluster` — branch `claude/seestar-sync-cutover`

**⚠️ Ordering is load-bearing.** The armed `settings.json` (`dry_run: false, reclaim_enabled: true`) lives in the shared archive, so the pod inherits deletion authority the instant it starts on the production prefix. The Mac must be confirmed dead first.

**Files:**
- Modify: `platform/seestar-sync/seestar-sync.yaml`

- [ ] **Step 1: Stop the Mac daemon**

```bash
cd ~/Projects/seestar-backup && docker compose down && docker ps -a --filter name=seestar-sync --format '{{.Names}}|{{.Status}}'
```

Expected: the container is `Exited`.

- [ ] **Step 2: Confirm the Mac is dead over MQTT — THE GATE**

The LWT is the authority; `docker ps` only proves the container stopped, not that the broker noticed.

```bash
cd ~/Projects/seestar-backup && .venv/bin/python - <<'EOF'
import pathlib, time, paho.mqtt.client as mqtt
env = dict(l.split("=", 1) for l in
           pathlib.Path(".env").read_text().splitlines()
           if l.strip() and not l.startswith("#") and "=" in l)
seen = {}
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.username_pw_set(env["MQTT_USERNAME"], env["MQTT_PASSWORD"])
c.on_connect = lambda cl, u, f, rc, p=None: cl.subscribe("seestar-sync/availability")
c.on_message = lambda cl, u, m: seen.update({m.topic: m.payload.decode()})
c.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)), 20)
c.loop_start(); time.sleep(6); c.loop_stop()
print(seen)
EOF
```

Expected exactly: `{'seestar-sync/availability': 'offline'}`

**If it still reads `online`, do not proceed.** Wait for the LWT, or find the second instance.

- [ ] **Step 3: Promote the pod**

In `platform/seestar-sync/seestar-sync.yaml`, replace the shadow-phase env block:

```yaml
            # ── PRODUCTION ──
            # DRY_RUN is deliberately ABSENT: the effective value comes from the
            # MQTT override in .seestar-sync/settings.json, which is how the Home
            # Assistant switches control this daemon. Setting it here would not
            # win anyway (override > env) and would mislead the next reader.
            - { name: MQTT_TOPIC_PREFIX, value: "seestar-sync" }
          envFrom:
            - secretRef: { name: seestar-sync-mqtt }
            - secretRef: { name: seestar-sync-kuma }
```

Ensure the container's `envFrom` now lists **both** secrets, and that `DRY_RUN` and the `seestar-sync-k8s` prefix are gone.

- [ ] **Step 4: Commit and open the PR**

```bash
cd ~/Projects/homelab-cluster
git checkout -b claude/seestar-sync-cutover
git add platform/seestar-sync/seestar-sync.yaml
git commit -m "feat(platform): cut seestar-sync over to production

Mac instance confirmed offline via LWT before this lands. Drops DRY_RUN so the
settings.json override governs again, restores the production topic prefix, and
wires the Kuma push URL."
git push -u origin claude/seestar-sync-cutover
```

- [ ] **Step 5: After merge, verify the device is live on the production prefix**

```bash
cd ~/Projects/seestar-backup && .venv/bin/python /private/tmp/claude-501/*/6747d03f-4441-41c4-932d-e4da8fede33f/scratchpad/probe_mqtt.py 2>&1 | tail -8
```

Expected: `seestar-sync/availability = online`, and `state` progressing through `idle`/`listing`/`copying`.

- [ ] **Step 6: Verify the Kuma monitor goes green**

Within one sync interval (15 min), "Seestar Sync" in Uptime Kuma should flip from DOWN to UP. If it stays DOWN, the push URL is not reaching the pod:

```bash
kubectl -n seestar-sync exec deploy/seestar-sync -- sh -c 'test -n "$KUMA_PUSH_URL" && echo SET || echo MISSING'
```

- [ ] **Step 7: Clear the shadow's retained topics**

The `seestar-sync-k8s/*` topics are retained and will haunt Home Assistant as a ghost device. Clear them by publishing empty retained payloads — the same cleanup used for the `seestar-sync-livetest` prefix on 2026-07-15:

```bash
cd ~/Projects/seestar-backup && .venv/bin/python - <<'EOF'
import pathlib, time, paho.mqtt.client as mqtt
env = dict(l.split("=", 1) for l in
           pathlib.Path(".env").read_text().splitlines()
           if l.strip() and not l.startswith("#") and "=" in l)
topics = set()
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.username_pw_set(env["MQTT_USERNAME"], env["MQTT_PASSWORD"])
c.on_connect = lambda cl, u, f, rc, p=None: cl.subscribe("seestar-sync-k8s/#")
c.on_message = lambda cl, u, m: topics.add(m.topic)
c.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)), 20)
c.loop_start(); time.sleep(6)
for t in sorted(topics):
    c.publish(t, payload=b"", retain=True); print("cleared", t)
time.sleep(2); c.loop_stop()
print(f"--- cleared {len(topics)} topics")
EOF
```

- [ ] **Step 8: Confirm zero shadow topics remain**

Re-run Step 7's script. Expected: `--- cleared 0 topics`.

---

## Task 9: Record it

**Repos:** `fallen-leaf/homelab-cluster`, plus the Obsidian vault.

A service the inventory does not know about is undiscoverable to the next agent.

- [ ] **Step 1: Add the platform README row**

In `platform/README.md`'s contents table, add:

| File | Lives in | What |
|---|---|---|
| `seestar-sync.yaml` | `platform/seestar-sync/` | Seestar S30 Pro → NAS mirror daemon. Static NFS PV onto `astrograph`; single replica with `Recreate` (two daemons sharing one archive with reclaim armed is the failure to avoid). No ingress, no OIDC — liveness is a Kuma push monitor. |
| `externalsecrets.yaml` | `platform/seestar-sync/` | `ExternalSecret`s for the `gitea-pull` dockerconfig, the MQTT broker account (`infra/seestar-sync/mqtt`), and the Kuma push URL. |

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/homelab-cluster
git checkout -b claude/seestar-sync-readme
git add platform/README.md
git commit -m "docs(platform): add seestar-sync to the contents table"
git push -u origin claude/seestar-sync-readme
```

- [ ] **Step 3: Update the vault service map**

Add `seestar-sync` to `02_reference/tools/home-infrastructure.md` (or the k3s service map, whichever currently lists cluster workloads), recording: runs in k3s, archive on the `astrograph` NFS export, watched by the "Seestar Sync" Kuma push monitor, and that it previously ran under docker-compose on a Mac until 2026-08-12.

- [ ] **Step 4: Re-check the tombstone issue**

`fallen-leaf/seestar-sync#1` predicts the `.smbdelete*` leak stops on NFS. Verify:

```bash
ssh nas.local 'ls -1 /volume1/astrograph/seestar/archive/.seestar-sync/.smbdelete* 2>/dev/null | wc -l'
```

Compare against the 113 recorded at filing. If no new ones appear after a few days of NFS operation, close the issue noting the SMB-only caveat for the local compose path. If the count grows, implement option 2 from the issue.

- [ ] **Step 5: Retire the Mac deployment cleanly**

`docker-compose.yml` stays in the repo — it is the local-development and disaster-recovery path. Remove only the stopped container and its image:

```bash
cd ~/Projects/seestar-backup && docker compose rm -f && docker image rm seestar-backup-seestar-sync:latest
```

Update the repo `README.md` to say the production deployment is k3s (`fallen-leaf/homelab-cluster` `platform/seestar-sync/`) and that the compose path is for local development, so the next reader does not restart a second production instance by following the quick-start.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| D1 NFS export + static PV | 2, 7 |
| D2 repo transfer | 3 |
| D3 shadow then hard cutover | 7, 8 |
| D4 generalize `nfs_share` | 1 |
| Image build (kaniko) | 4 |
| Secrets via ESO | 5, 7 |
| Kuma push monitor | 6 |
| Cutover phases 0a/0b/1/2/3/4 | 1, 2, 3–4, 7, 8, 8 |
| Cutover phase 5 (record it) | 9 |
| Risk 1 NFS squash | Task 2 Step 5 |
| Risk 2 widened export | Task 2 Step 1 comment; Task 5 in-line note |
| Risk 3 re-copy storm | Task 7 Step 9 |
| Risk 4 Pi fleet regression | Task 1 Steps 1, 7, 8 |
| Risk 5 NFS dependency | inherent; no mitigation task (accepted in spec) |

**Type/name consistency:** `seestar-archive` (PV and PVC) is used identically in Task 7 Steps 5. Secret names `seestar-sync-mqtt`, `seestar-sync-kuma`, `gitea-pull` match between `externalsecrets.yaml` and the Deployment's `envFrom`. OpenBao paths `infra/seestar-sync/mqtt` and `infra/uptime-kuma/seestar-sync-push` match between Task 5's policy grants, Task 6's spec entry, and Task 7's `remoteRef.key` values. `push_token_name: seestar-sync` in `monitors.yaml` matches `name: seestar-sync` in `openbao_push_token_specs`.

**Known gaps requiring a human:**
1. **Task 3 Step 1** — the repo transfer is a Gitea UI action.
2. **Task 6 Step 2 and Task 7 Step 1** — seeding two OpenBao secrets requires a write-capable token. These are hand-run steps against the no-manual-IaC rule; the push-token one is a ratified fail-closed design in `fallen-leaf/uptime-kuma`, the MQTT one is a one-time migration of a credential that exists only in a `.env` today.
3. **Task 4 Step 4 → Task 7 Step 5** — the `<SHA>` placeholder is filled from a real build. This is the one intentional substitution in the plan; it cannot be known before the build runs.
