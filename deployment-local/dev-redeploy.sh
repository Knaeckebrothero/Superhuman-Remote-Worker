#!/usr/bin/env bash
# =============================================================================
# dev-redeploy.sh — Build, import, and restart SRW services on local K3s
# =============================================================================
#
# Usage:
#   ./deployment-local/dev-redeploy.sh                    # Rebuild all 4 app services
#   ./deployment-local/dev-redeploy.sh orchestrator        # Rebuild one component
#   ./deployment-local/dev-redeploy.sh cockpit mcp         # Rebuild specific components
#   ./deployment-local/dev-redeploy.sh --apply             # Rebuild all + re-apply kustomize
#   ./deployment-local/dev-redeploy.sh orchestrator --apply # Rebuild one + re-apply kustomize
#
# First-time setup:
#   ./deployment-local/create-secrets.sh
#   ./deployment-local/dev-redeploy.sh --apply
#
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="superhuman-remote-worker"
REGISTRY="ghcr.io/knaeckebrothero/superhuman-remote-worker"
TAG="local"

# Component -> Dockerfile mapping
declare -A DOCKERFILES=(
  [orchestrator]="docker/Dockerfile.orchestrator"
  [agent]="docker/Dockerfile.agent"
  [cockpit]="docker/Dockerfile.cockpit"
  [mcp]="docker/Dockerfile.mcp"
  [workspace]="docker/Dockerfile.workspace"
  [vpn]="docker/vpn/Dockerfile"
)

# Components with non-root build contexts (default: repo root ".")
declare -A BUILD_CONTEXTS=(
  [vpn]="docker/vpn"
)

# --- Parse arguments ---
APPLY=false
TARGETS=()
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --help|-h)
      sed -n '2,/^set /{ /^#/s/^# \?//p }' "$0"
      exit 0
      ;;
    *)
      if [[ -n "${DOCKERFILES[$arg]+x}" ]]; then
        TARGETS+=("$arg")
      else
        echo "Error: unknown component '$arg'"
        echo "Valid components: ${!DOCKERFILES[*]}"
        exit 1
      fi
      ;;
  esac
done

# Default: all components
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=(orchestrator cockpit agent mcp workspace vpn)
fi

# --- Detect container build tool ---
if command -v docker &>/dev/null; then
  BUILD="docker"
elif command -v podman &>/dev/null; then
  BUILD="podman"
else
  echo "Error: docker or podman is required to build images"
  exit 1
fi

# --- Build, import, restart ---
cd "$REPO_ROOT"

for comp in "${TARGETS[@]}"; do
  image="${REGISTRY}-${comp}:${TAG}"
  dockerfile="${DOCKERFILES[$comp]}"

  echo ""
  echo "===== ${comp} ====="

  context="${BUILD_CONTEXTS[$comp]:-.}"

  echo "  Building ${image}"
  $BUILD build -t "$image" -f "$dockerfile" "$context"

  echo "  Importing into K3s"
  $BUILD save "$image" | sudo k3s ctr images import -

  if [[ "$comp" == "vpn" ]]; then
    echo "  Restarting VPN deployments"
    kubectl rollout restart deployment/srw-vpn-cluster deployment/srw-vpn-research deployment/srw-vpn-workstation -n "$NAMESPACE" 2>/dev/null || true
  elif [[ "$comp" == "workspace" ]]; then
    echo "  Skipping restart (workspace pods are provisioned on demand)"
  else
    echo "  Restarting deployment/srw-${comp}"
    kubectl rollout restart "deployment/srw-${comp}" -n "$NAMESPACE"
  fi
done

# --- Optionally re-apply the full kustomize overlay ---
if [[ "$APPLY" == true ]]; then
  echo ""
  echo "===== Applying kustomize overlay ====="
  kubectl kustomize --load-restrictor=LoadRestrictionsNone deployment-local/ \
    | kubectl apply -f -
fi

echo ""
echo "Done. Watch pods come up:"
echo "  kubectl get pods -n ${NAMESPACE} -w"
