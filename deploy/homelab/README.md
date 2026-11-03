# Maintainer's homelab deployment

Site-specific material for the maintainer's own setup. **You do not need any of
this to run `seestar-sync`**; start from the top-level README and
[`examples/kubernetes/`](../../examples/kubernetes/seestar-sync.yaml) instead.

## How it runs

Production runs in k3s: the `platform/seestar-sync` ArgoCD app in
`fallen-leaf/homelab-cluster` (cut over from a Mac on 2026-09-14). The archive is the
NAS `astrograph` export mounted over NFS, credentials come from OpenBao via External
Secrets, and a "Seestar Sync" Uptime Kuma push monitor watches it. Change the
deployment in that repo, not here.

The `docker-compose.yml` at the repository root is for local development and disaster
recovery only. Never run it against the production archive while the k3s pod is up:
two primaries writing one archive fight over `manifest.db`. Use `SHADOW_MODE=true`
(with its own `MQTT_TOPIC_PREFIX` and `KUMA_PUSH_URL`) for any second instance.

## Contents

| Path | What it is |
|---|---|
| `tofu/network/` | OpenTofu config that codifies the telescope's UniFi fixed-IP reservation. Applied by the `network` Gitea workflow. See its README. |
| `scripts/seed-openbao-secrets.sh` | Seeds the MQTT and Kuma push secrets into OpenBao. Run from the repo root: `deploy/homelab/scripts/seed-openbao-secrets.sh --dry-run`. |
| `docs/plans/`, `docs/specs/` | Historical implementation plans, the k3s relocation spec, and the progress ledger. They name this maintainer's hosts and paths and are kept for history. |

## CI that stays at the repository root

Gitea only reads workflows from `.gitea/workflows/`, so they are not under this
directory:

- `ci.yml` — pytest and ruff on every PR and on `main`.
- `build-images.yml` — builds and pushes the production image with kaniko to the
  maintainer's registry.
- `network.yml` — validates and drift-checks `deploy/homelab/tofu/network`.
