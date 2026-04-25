#!/usr/bin/env bash
# Headscale Bootstrap — run once after the Headscale StatefulSet is ready.
#
# Creates a user, an admin API key, and a tagged reusable pre-auth key,
# then prints the values plus ready-to-paste Vault/ESO commands. The script
# does not talk to headscale over the network — it execs the headscale
# binary inside the running pod, so the public URL of the server is
# irrelevant here.
#
# Override any default by exporting the matching env var before running.
#
#   KUBE_CONTEXT          kubectl context for the headscale cluster (main)
#   HEADSCALE_NAMESPACE   namespace headscale runs in                (headscale)
#   HEADSCALE_POD_LABEL   label selector for the pod                 (app=headscale)
#   HEADSCALE_USER        user to create / look up                   (srw)
#   API_KEY_TTL           --expiration for `apikeys create`          (8760h)
#   PREAUTH_KEY_TTL       --expiration for `preauthkeys create`      (8760h)
#   PREAUTH_KEY_TAGS      comma-separated tags for the pre-auth key  (tag:agent)
#   VAULT_SRW_PATH        Vault KV path for the SRW main-cluster blob
#                         (secret/homelab/superhuman-remote-worker/srw-secrets)
#   VAULT_VMS_PATH        Vault KV path for the VMs-cluster API-key blob
#                         (secret/homelab/agent-vms/headscale-api-key)
#
# Usage:
#   bash headscale-bootstrap.sh              # use defaults
#   HEADSCALE_USER=acme bash headscale-bootstrap.sh
#   bash headscale-bootstrap.sh --help

set -euo pipefail

# -----------------------------------------------------------------------------
# Defaults (override via env)
# -----------------------------------------------------------------------------
CONTEXT="${KUBE_CONTEXT:-main}"
NS="${HEADSCALE_NAMESPACE:-headscale}"
POD_LABEL="${HEADSCALE_POD_LABEL:-app=headscale}"
HS_USER="${HEADSCALE_USER:-srw}"
API_TTL="${API_KEY_TTL:-8760h}"
PREAUTH_TTL="${PREAUTH_KEY_TTL:-8760h}"
PREAUTH_TAGS="${PREAUTH_KEY_TAGS:-tag:agent}"
VAULT_SRW_PATH="${VAULT_SRW_PATH:-secret/homelab/superhuman-remote-worker/srw-secrets}"
VAULT_VMS_PATH="${VAULT_VMS_PATH:-secret/homelab/agent-vms/headscale-api-key}"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,30p' "$0"
  exit 0
fi

# -----------------------------------------------------------------------------
# Discover the headscale pod by label so this works regardless of how the
# StatefulSet is named (legacy `headscale-0`, chart-rendered fullname-prefixed,
# or whatever any future deployment produces).
# -----------------------------------------------------------------------------
POD=$(kubectl --context "$CONTEXT" -n "$NS" get pod \
  -l "$POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "No pod with label $POD_LABEL in namespace $NS on context $CONTEXT" >&2
  exit 1
fi

echo "Headscale pod: $POD (ns=$NS, ctx=$CONTEXT)"
kubectl --context "$CONTEXT" -n "$NS" wait --for=condition=ready "pod/$POD" --timeout=120s

# -----------------------------------------------------------------------------
# User
# -----------------------------------------------------------------------------
echo "Ensuring user '$HS_USER' exists..."
kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale users create "$HS_USER" \
  || echo "(user '$HS_USER' already exists, continuing)"

# Headscale 0.28's preauthkey command requires the numeric ID.
# JSON shape: [{"id":"1","name":"<HS_USER>","createdAt":"..."}]. Parse with
# python rather than line-oriented grep, since "id" appears *before* "name".
USERS_JSON=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale users list -o json)
USER_ID=$(printf '%s' "$USERS_JSON" | python3 -c "
import json, sys
target = '$HS_USER'
for u in json.load(sys.stdin):
    if u.get('name') == target:
        print(u['id'])
        break
")
if [ -z "$USER_ID" ]; then
  echo "Failed to resolve numeric ID for user '$HS_USER'. Raw users list:" >&2
  echo "$USERS_JSON" >&2
  exit 1
fi
echo "User ID: $USER_ID"

# -----------------------------------------------------------------------------
# Keys
# -----------------------------------------------------------------------------
echo "Generating API key (TTL=$API_TTL)..."
API_KEY=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- \
  headscale apikeys create --expiration "$API_TTL")

echo "Generating pre-auth key (reusable, tags=$PREAUTH_TAGS, TTL=$PREAUTH_TTL)..."
AUTH_KEY=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- \
  headscale preauthkeys create \
  --user "$USER_ID" \
  --reusable \
  --expiration "$PREAUTH_TTL" \
  --tags "$PREAUTH_TAGS")

# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
cat <<EOF

=== Keys generated ===

HEADSCALE_API_KEY=$API_KEY
TAILSCALE_AUTH_KEY=$AUTH_KEY

Store the agent pre-auth key in Vault (consumed by SRW agent pods on the
main cluster via the existing 'srw' ExternalSecret):

  vault kv patch $VAULT_SRW_PATH \\
    TAILSCALE_AUTH_KEY="$AUTH_KEY"

Store the admin API key in Vault (consumed by the vm-controller on the
VMs cluster via the existing 'headscale-api-key' ExternalSecret):

  vault kv put $VAULT_VMS_PATH \\
    api-key="$API_KEY"

After patching Vault, force ESO to refresh both ExternalSecrets:

  kubectl --context $CONTEXT annotate externalsecret srw \\
    -n superhuman-remote-worker \\
    force-sync=\$(date +%s) --overwrite

  kubectl --context vms annotate externalsecret headscale-api-key \\
    -n agent-vms \\
    force-sync=\$(date +%s) --overwrite

EOF
