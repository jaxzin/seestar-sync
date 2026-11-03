#!/usr/bin/env bash
# Seed the two OpenBao secrets the k3s deployment of seestar-sync needs.
#
#   secret/infra/seestar-sync/mqtt              username, password
#       Migrated from the local compose .env — the only place they exist today.
#   secret/infra/uptime-kuma/seestar-sync-push  token, push_url
#       A fresh Kuma push token. Seeded here because the uptime-kuma converge
#       Job's policy is read-only by design ("fail-closed beats standing
#       privilege"), so it cannot create the token itself.
#
# SAFETY
#   - Never prints a secret value. Verification reports key names and lengths.
#   - Values travel via a JSON file on stdin (@-), never argv, so they do not
#     show up in `ps` or shell history.
#   - Idempotent: an existing secret is left untouched. Re-seeding the push
#     token would silently break the live Kuma monitor, so overwriting requires
#     --force and still refuses the push token unless --rotate-push-token.
#
# Needs a token that can create/update both paths (your human-readonly token
# cannot). Log in with an admin-capable method first; the script checks
# capabilities before writing anything.
#
# Usage: deploy/homelab/scripts/seed-openbao-secrets.sh [--env-file PATH] [--force] [--rotate-push-token] [--dry-run]
set -euo pipefail

export VAULT_ADDR="${VAULT_ADDR:-https://openbao.forest-draconis.ts.net}"
ENV_FILE="$(cd "$(dirname "$0")/../../.." && pwd)/.env"
FORCE=0 ROTATE=0 DRY=0
MQTT_PATH="secret/infra/seestar-sync/mqtt"
PUSH_PATH="secret/infra/uptime-kuma/seestar-sync-push"
PUSH_BASE="https://status.jaxzin.casa/api/push"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --rotate-push-token) ROTATE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
for bin in bao jq openssl; do command -v "$bin" >/dev/null || die "$bin not found"; done

# --- preflight: authenticated, and allowed to write both paths ---------------
bao token lookup >/dev/null 2>&1 || die "not logged in to OpenBao ($VAULT_ADDR). Run: bao login -method=oidc"
for p in "$MQTT_PATH" "$PUSH_PATH"; do
  data_path="${p/secret\//secret/data/}"
  caps=$(bao token capabilities "$data_path" 2>/dev/null || true)
  case " $caps " in
    *" create "*|*" update "*|*" root "*) ;;
    *) die "token cannot write $data_path (capabilities: ${caps:-none}). Log in with a write-capable role." ;;
  esac
done
echo "preflight ok: authenticated, write access to both paths"

exists() { bao kv get "$1" >/dev/null 2>&1; }

# Write a JSON object to a KV path via stdin. The JSON is built in-process and
# never lands on disk or in argv.
put_json() { # path json
  if [ "$DRY" = 1 ]; then echo "  [dry-run] would write $1"; return; fi
  printf '%s' "$2" | bao kv put "$1" - >/dev/null
}

verify() { # path expected-keys...
  local path="$1"; shift
  local data; data=$(bao kv get -format=json "$path" | jq -c '.data.data')
  for k in "$@"; do
    len=$(jq -r --arg k "$k" '.[$k] // "" | length' <<<"$data")
    [ "$len" -gt 0 ] || die "$path: key '$k' missing or empty after write"
    echo "  $path  $k: length $len"
  done
}

# --- 1. MQTT credentials, migrated from .env ----------------------------------
echo "[1/2] $MQTT_PATH"
if exists "$MQTT_PATH" && [ "$FORCE" = 0 ]; then
  echo "  exists — left untouched (use --force to overwrite)"
else
  [ -f "$ENV_FILE" ] || die ".env not found at $ENV_FILE (pass --env-file)"
  # Read only the two keys; strip optional surrounding quotes. Nothing echoed.
  getenv() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'; }
  user=$(getenv MQTT_USERNAME); pass=$(getenv MQTT_PASSWORD)
  [ -n "$user" ] || die "MQTT_USERNAME missing from $ENV_FILE"
  [ -n "$pass" ] || die "MQTT_PASSWORD missing from $ENV_FILE"
  put_json "$MQTT_PATH" "$(jq -nc --arg u "$user" --arg p "$pass" '{username:$u,password:$p}')"
  unset user pass
fi
[ "$DRY" = 1 ] || verify "$MQTT_PATH" username password

# --- 2. Kuma push token --------------------------------------------------------
echo "[2/2] $PUSH_PATH"
if exists "$PUSH_PATH" && [ "$ROTATE" = 0 ]; then
  echo "  exists — left untouched (rotating breaks the live monitor; use --rotate-push-token deliberately)"
else
  tok=$(openssl rand -hex 16)   # 32 hex chars, same shape the Kuma role generates
  put_json "$PUSH_PATH" "$(jq -nc --arg t "$tok" --arg u "$PUSH_BASE/$tok" '{token:$t,push_url:$u}')"
  unset tok
fi
if [ "$DRY" = 0 ]; then
  verify "$PUSH_PATH" token push_url
  host=$(bao kv get -format=json "$PUSH_PATH" | jq -r '.data.data.push_url | split("/api/")[0]')
  [ "$host" = "${PUSH_BASE%/api/push}" ] || die "push_url host is '$host', expected '${PUSH_BASE%/api/push}'"
  echo "  push_url host: $host"
fi

echo "done."
