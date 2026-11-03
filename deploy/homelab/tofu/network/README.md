# deploy/homelab/tofu/network — the Seestar S30's UniFi fixed-IP reservation

| | |
|---|---|
| Client name | `seestar` |
| MAC | `40:fd:f3:9e:3e:64` (Wi-Fi station, OUI AMPAK Technology) |
| IP | `192.168.10.50` |
| Network | module default `Default` (`192.168.10.0/24`) — not IoT |

seestar-sync (on k3s) mounts the telescope's SMB share at `192.168.10.50`, so
that address is load-bearing. The reservation lives here because this repo
consumes it (Brian's decision, 2026-09-13); it consumes
`fallen-leaf/terraform-unifi-reservation` pinned to a commit SHA.

## Where the MAC and client id came from

A read-only lookup on 2026-09-13, no controller writes:

- `arp -n 192.168.10.50` on the LAN → `40:fd:f3:9e:3e:64`
- mDNS `SeeStar.local` → `192.168.10.50`
- `GET /proxy/network/api/s/default/rest/user`, filtered on that MAC →
  `_id 6a4029b1691cf0d3484b1c9b`, `hostname SeeStar`, `oui AMPAK Technology`,
  `use_fixedip true`, `fixed_ip 192.168.10.50`, `name null`, no `network_id`,
  no `note`. It is the only client holding `fixed_ip 192.168.10.50`.

## This adopts an existing record; it does not create one

A hand-made reservation already exists. `import.tf` adopts it declaratively
(import blocks go inert after the first apply), because a destroy of this
resource **forgets the client**. The design is the same as tower2-setup's; see
`import.tf`.

### Expected first plan

`1 to import, 0 to add, 1 to change, 0 to destroy`, in place:

```
~ name       = null/"" -> "seestar"
~ network_id = ""      -> "5f6bda4c52faff001369d666"   # Default
~ note       = ""      -> "managed by terraform-unifi-reservation"
  fixed_ip   = "192.168.10.50"                          # no change
  mac        = "40:fd:f3:9e:3e:64"                      # no change
```

**STOP if the plan shows a destroy/replace or any `mac`/`fixed_ip` change.**
`mac` is ForceNew, so a change there means replacing (forgetting) the client.
The name change is expected: `seestar.lan.jaxzin.com` starts resolving, and
`seestar.local` (mDNS) is unaffected.

## Credentials: one provisioned role, nothing to paste

The workflow logs in once, as `cip-seestar-sync`
(`SEESTAR_SYNC_APPROLE_ROLE_ID` / `_SECRET_ID`). openbao-infra's CI
provisioner mints that role and writes it into this repo's Actions secrets. It
is declared in `openbao/ci-provisioner-consumers.yml`, and its three
cross-namespace `(b)` grants are in `openbao/ci-provisioner-allowlist.yml`. Each
grant needs a maintainer 👍 on that PR's reaction gate.

| OpenBao path | Fields | Used by |
|---|---|---|
| `secret/infra/gitea/ci-read` | `token` | both jobs (`tofu init` clones the module) |
| `secret/infra/unifi/tofu` | `username`, `password` | plan/apply |
| `secret/infra/b2/tofu-state` | `key_id`, `application_key`, `endpoint`, `region`, `bucket` | state backend (`seestar-sync/network.tfstate`) |

Until those grants merge and the provisioner runs, both jobs fail closed at the
login gate with a message naming the fix.

The credential is not `unifi-ci` (homelab-cluster `policies/unifi-ci.hcl`)
because that role's credential is hand-delivered. tower2-setup and
unifi-gateway-setup, the two most recent consumers, both use the cip shape.

## Verification

1. After the openbao-infra PR merges and the provisioner runs, dispatch
   `network` (default plan) and confirm it matches the plan above.
2. Dispatch with `apply=true`, then run a plan again: exit 0 (import inert, no drift).
3. The telescope keeps `192.168.10.50` across a power cycle.
