#!/usr/bin/env bash
# =============================================================================
# local-dev-tilt-up.sh — Tilt-aware bootstrap.
#
# Builds on top of scripts/local-dev-up.sh:
#   1. Runs the base bootstrap (cluster + cert-manager + ClusterIssuer +
#      namespace + srw-vm-ssh-key Secret).
#   2. Mints the srw-session-jwt Secret (required by the sessionRouter).
#   3. Syncs the current Traefik ClusterIP into values-local.yaml's
#      opencloud.hostAliases entry, since the IP changes after every
#      `k3d cluster delete && create`.
#   4. Runs `tilt up` (foreground, ^C to stop).
#
# Idempotent: re-runs are safe. Use it to bring a stopped cluster back up
# or after `k3d cluster delete`.
#
# Prereq: `tilt` binary on PATH. See docs/features/tilt_inner_loop_dev.md
# §Prerequisites for the install commands.
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

NAMESPACE="${NAMESPACE:-srw}"
KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-srw}"
VALUES_LOCAL="$REPO_ROOT/deployment/values-local.yaml"

log()  { printf '\033[1;34m[tilt-bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
skip() { printf '\033[1;33m[skip]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. Tilt-specific prereq -------------------------------------------------
command -v tilt >/dev/null \
  || die "tilt not on PATH — install per docs/features/tilt_inner_loop_dev.md"

# --- 1. Base bootstrap -------------------------------------------------------
log "running base bootstrap (cluster + cert-manager + namespace + vm-ssh-key)"
"$SCRIPT_DIR/local-dev-up.sh"

KCTL="kubectl --context=$KUBE_CONTEXT"

# --- 2. session-jwt Secret ---------------------------------------------------
if $KCTL -n "$NAMESPACE" get secret srw-session-jwt >/dev/null 2>&1; then
  skip "secret srw-session-jwt already exists"
else
  log "minting srw-session-jwt Secret"
  JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n' | head -c 64)
  $KCTL -n "$NAMESPACE" create secret generic srw-session-jwt \
    --from-literal=jwt-secret="$JWT_SECRET"
  ok "session-jwt Secret created"
fi

# --- 3. Sync Traefik ClusterIP into values-local.yaml ------------------------
NEW_IP=$($KCTL -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
if [[ -z "$NEW_IP" ]]; then
  log "Traefik Service not ready yet — skipping hostAliases sync (re-run later)"
elif [[ -f "$VALUES_LOCAL" ]] && grep -q 'auth.localhost' "$VALUES_LOCAL"; then
  CURRENT_IP=$(grep -E '^    - ip: "[0-9.]+"$' "$VALUES_LOCAL" | head -1 | sed -E 's/.*"([0-9.]+)".*/\1/' || true)
  if [[ "$CURRENT_IP" == "$NEW_IP" ]]; then
    skip "opencloud.hostAliases IP already current ($NEW_IP)"
  else
    log "updating opencloud.hostAliases IP: ${CURRENT_IP:-<unset>} → $NEW_IP"
    sed -i.bak -E "s|^    - ip: \"[0-9.]+\"$|    - ip: \"$NEW_IP\"|" "$VALUES_LOCAL"
    rm -f "${VALUES_LOCAL}.bak"
    ok "hostAliases IP synced"
  fi
elif [[ ! -f "$VALUES_LOCAL" ]]; then
  log "values-local.yaml not found — copy from the example then re-run this script:"
  log "  cp deployment/values-local.example.yaml deployment/values-local.yaml"
  log "  \$EDITOR deployment/values-local.yaml   # paste at least one LLM key"
  die "create values-local.yaml first"
fi

# --- 4. Run Tilt -------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32m✓ Cluster ready. Starting Tilt.\033[0m')

Tilt UI:      https://localhost:10350
Cockpit:      https://localhost   (test/test, after first build completes)

Press Ctrl-C to stop Tilt (cluster keeps running). To stop the cluster too:
  k3d cluster stop srw

EOF

exec tilt up
