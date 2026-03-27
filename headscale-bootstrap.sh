#!/usr/bin/env bash
# Headscale Bootstrap — run once after Headscale StatefulSet is ready.
#
# Creates the srw user, API key, and agent pre-auth key, then prints
# them for storage in Vault.
#
# Usage:
#   bash headscale-bootstrap.sh
#
# Then store in Vault:
#   vault kv patch secret/homelab/superhuman-remote-worker/srw-secrets \
#     TAILSCALE_AUTH_KEY="<agent-auth-key>" \
#     HEADSCALE_API_KEY="<api-key>"

set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-main}"
NS="headscale"
POD="headscale-0"

echo "Waiting for $POD to be ready..."
kubectl --context "$CONTEXT" -n "$NS" wait --for=condition=ready "pod/$POD" --timeout=120s

echo "Creating user 'srw'..."
kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale users create srw 2>&1 || echo "(already exists)"

# Get user ID (v0.28 API requires numeric ID, not name)
USER_ID=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale users list -o json 2>&1 | grep -A1 '"name": "srw"' | grep '"id"' | grep -o '[0-9]*')
echo "User ID: $USER_ID"

echo "Generating API key (8760h = 365 days)..."
API_KEY=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale apikeys create --expiration 8760h 2>&1)

echo "Generating agent pre-auth key (reusable, tag:agent)..."
AUTH_KEY=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- headscale preauthkeys create --user "$USER_ID" --reusable --expiration 8760h --tags tag:agent 2>&1)

echo ""
echo "=== Keys generated ==="
echo ""
echo "HEADSCALE_API_KEY=$API_KEY"
echo "TAILSCALE_AUTH_KEY=$AUTH_KEY"
echo ""
echo "Store in Vault:"
echo "  vault kv patch secret/homelab/superhuman-remote-worker/srw-secrets \\"
echo "    TAILSCALE_AUTH_KEY=\"$AUTH_KEY\" \\"
echo "    HEADSCALE_API_KEY=\"$API_KEY\""
echo ""
echo "VM controller (vms cluster):"
echo "  kubectl --context vms -n agent-vms create secret generic headscale-api-key --from-literal=api-key=\"$API_KEY\""
