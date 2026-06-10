#!/usr/bin/env bash
# =============================================================================
# Local development bootstrap: k3d cluster + cert-manager + mkcert ClusterIssuer
# + namespace + vm-ssh-key Secret.
#
# Idempotent: re-runs are safe. Skips anything that already exists.
#
# After this, copy the values template and `helm install`:
#   cp deployment/values-local.example.yaml deployment/values-local.yaml
#   $EDITOR deployment/values-local.yaml      # paste at least one LLM key
#   helm install srw ./helm -n srw -f deployment/values-local.yaml
#
# Prerequisites (must be done once on the host BEFORE running this):
#   - docker + k3d + kubectl + helm + mkcert installed
#   - `mkcert -install` (user-level trust)
#   - `sudo CAROOT="$HOME/.local/share/mkcert" mkcert -install` (system + Chrome trust)
# =============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-srw}"
NAMESPACE="${NAMESPACE:-srw}"
KUBE_CONTEXT="k3d-${CLUSTER_NAME}"
MKCERT_CAROOT="${MKCERT_CAROOT:-$HOME/.local/share/mkcert}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.16.2}"

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
skip() { printf '\033[1;33m[skip]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- Prereq checks ----------------------------------------------------------
for bin in docker k3d kubectl helm mkcert ssh-keygen; do
  command -v "$bin" >/dev/null || die "missing required binary: $bin"
done

[[ -f "$MKCERT_CAROOT/rootCA.pem" && -f "$MKCERT_CAROOT/rootCA-key.pem" ]] \
  || die "mkcert CA not found at $MKCERT_CAROOT — run 'mkcert -install' first"

# --- 1. k3d cluster ---------------------------------------------------------
if k3d cluster list "$CLUSTER_NAME" >/dev/null 2>&1; then
  skip "k3d cluster '$CLUSTER_NAME' already exists"
else
  log "creating k3d cluster '$CLUSTER_NAME'"
  k3d cluster create "$CLUSTER_NAME" \
    --servers 1 \
    --port "80:80@loadbalancer" \
    --port "443:443@loadbalancer" \
    --registry-create "${CLUSTER_NAME}-registry:0.0.0.0:5000"
  ok "cluster created"
fi

# Ensure kubectl is pointed at it for this script's commands
KCTL="kubectl --context=$KUBE_CONTEXT"

# --- 2. cert-manager --------------------------------------------------------
if $KCTL -n cert-manager get deploy cert-manager >/dev/null 2>&1; then
  skip "cert-manager already installed"
else
  log "installing cert-manager $CERT_MANAGER_VERSION"
  helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
  helm repo update >/dev/null
  helm install cert-manager jetstack/cert-manager \
    --kube-context "$KUBE_CONTEXT" \
    --namespace cert-manager --create-namespace \
    --version "$CERT_MANAGER_VERSION" --set crds.enabled=true >/dev/null
  log "waiting for cert-manager to be ready"
  $KCTL -n cert-manager rollout status deploy/cert-manager --timeout=180s
  $KCTL -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
  $KCTL -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=180s
  ok "cert-manager ready"
fi

# --- 3. mkcert CA Secret + ClusterIssuer ------------------------------------
if $KCTL -n cert-manager get secret mkcert-ca-key-pair >/dev/null 2>&1; then
  skip "secret mkcert-ca-key-pair already exists"
else
  log "uploading mkcert CA to cert-manager namespace"
  $KCTL -n cert-manager create secret tls mkcert-ca-key-pair \
    --cert="$MKCERT_CAROOT/rootCA.pem" --key="$MKCERT_CAROOT/rootCA-key.pem"
  ok "CA secret created"
fi

if $KCTL get clusterissuer mkcert-issuer >/dev/null 2>&1; then
  skip "ClusterIssuer mkcert-issuer already exists"
else
  log "creating mkcert ClusterIssuer"
  $KCTL apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: mkcert-issuer
spec:
  ca:
    secretName: mkcert-ca-key-pair
EOF
  ok "ClusterIssuer created"
fi

# --- 4. SRW namespace + vm-ssh-key Secret -----------------------------------
$KCTL create namespace "$NAMESPACE" --dry-run=client -o yaml | $KCTL apply -f - >/dev/null

if $KCTL -n "$NAMESPACE" get secret srw-vm-ssh-key >/dev/null 2>&1; then
  skip "secret srw-vm-ssh-key already exists in namespace $NAMESPACE"
else
  log "generating dummy VM SSH keypair (not used locally — orchestrator + workspace pods mount this Secret)"
  TMPDIR=$(mktemp -d)
  trap 'rm -rf "$TMPDIR"' EXIT
  ssh-keygen -t ed25519 -f "$TMPDIR/key" -N "" -C "srw-local@dev" -q
  # Key names must match what the chart expects: ssh-privatekey (orchestrator,
  # mounted via subPath) + ssh-publickey (workspace pod authorized_keys seed).
  $KCTL -n "$NAMESPACE" create secret generic srw-vm-ssh-key \
    --from-file=ssh-privatekey="$TMPDIR/key" \
    --from-file=ssh-publickey="$TMPDIR/key.pub"
  ok "vm-ssh-key Secret created"
fi

# --- 5. In-cluster DNS for *.localhost ingress hosts -------------------------
# Pods cannot resolve cloud.localhost / auth.localhost / git.localhost (no
# DNS exists for .localhost), but several in-cluster flows must reach the
# ingress hostnames: OpenCloud's ocdav forwards every upload to its public
# data-gateway URL (https://cloud.localhost/data), rclone tus uploads PATCH
# the same URL from workspace pods, and OpenCloud's OIDC verifier fetches
# JWKS from auth.localhost. Map them to Traefik's ClusterIP via the k3s
# coredns-custom hook so every pod resolves them — supersedes per-pod
# hostAliases workarounds. Idempotent; the ClusterIP is stable for the life
# of the cluster (re-run this script after `k3d cluster delete && create`).
TRAEFIK_IP=$($KCTL -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
if [ -z "$TRAEFIK_IP" ]; then
  skip "traefik svc not up yet — re-run this script later to install the *.localhost DNS override"
else
  log "installing coredns-custom override: *.localhost ingress hosts -> $TRAEFIK_IP"
  $KCTL apply -f - <<COREDNS_EOF >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns-custom
  namespace: kube-system
data:
  srw-localhost.server: |
    cloud.localhost auth.localhost git.localhost:53 {
        hosts {
            $TRAEFIK_IP cloud.localhost
            $TRAEFIK_IP auth.localhost
            $TRAEFIK_IP git.localhost
            fallthrough
        }
    }
COREDNS_EOF
  $KCTL -n kube-system rollout restart deploy/coredns >/dev/null
  ok "coredns-custom override applied (cloud/auth/git.localhost -> $TRAEFIK_IP)"
fi

# --- Done -------------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32m✓ Local cluster ready.\033[0m')

Next:
  cp deployment/values-local.example.yaml deployment/values-local.yaml
  \$EDITOR deployment/values-local.yaml      # paste at least one LLM key
  helm install srw ./helm -n $NAMESPACE -f deployment/values-local.yaml

Then open https://localhost/ and log in as test / test.

Cluster lifecycle:
  k3d cluster stop  $CLUSTER_NAME
  k3d cluster start $CLUSTER_NAME
EOF
