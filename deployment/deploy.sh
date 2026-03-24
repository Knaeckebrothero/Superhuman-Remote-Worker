#!/usr/bin/env bash
# deploy.sh — Update SRW image tags in K8s manifests and push
#
# Usage:
#   ./deploy.sh                              # Deploy latest main commit, all components
#   ./deploy.sh sha-a1b2c3d                  # Deploy specific tag, all components
#   ./deploy.sh sha-a1b2c3d orchestrator agent  # Deploy specific components only
#   ./deploy.sh latest                       # Reset to :latest tag (e.g. for debugging)
#
# Components: orchestrator, agent, cockpit, mcp, vpn, vm-controller, vm-base
#
# Run from anywhere — the script finds its own directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST_DIR="$SCRIPT_DIR"

# --- Resolve tag -----------------------------------------------------------

resolve_tag() {
    local input="${1:-}"
    if [[ -z "$input" ]]; then
        # Default: derive SHA tag from the current SRW repo HEAD
        local sha
        sha=$(git -C "$REPO_DIR" rev-parse --short=7 HEAD 2>/dev/null) || {
            echo "Error: Could not read git SHA from $REPO_DIR" >&2
            exit 1
        }
        echo "sha-${sha}"
    elif [[ "$input" == "latest" ]]; then
        echo "latest"
    elif [[ "$input" =~ ^sha- ]]; then
        echo "$input"
    elif [[ "$input" =~ ^[0-9a-f]{7,40}$ ]]; then
        echo "sha-${input:0:7}"
    else
        echo "Error: Invalid tag '$input'. Use 'sha-XXXXXXX', a git SHA, or 'latest'." >&2
        exit 1
    fi
}

TAG=$(resolve_tag "${1:-}")
shift || true

# --- Resolve components ----------------------------------------------------

ALL_COMPONENTS=(orchestrator agent cockpit mcp vpn vm-controller vm-base)

if [[ $# -gt 0 ]]; then
    COMPONENTS=("$@")
    for c in "${COMPONENTS[@]}"; do
        if [[ ! " ${ALL_COMPONENTS[*]} " =~ " ${c} " ]]; then
            echo "Error: Unknown component '$c'. Valid: ${ALL_COMPONENTS[*]}" >&2
            exit 1
        fi
    done
else
    COMPONENTS=("${ALL_COMPONENTS[@]}")
fi

# --- Update manifests ------------------------------------------------------

SED_PATTERN='[a-zA-Z0-9_.-]*'
CHANGED=0

update_image() {
    local component="$1"
    local file="$2"
    local image_name="$3"

    if [[ ! -f "$file" ]]; then
        echo "  SKIP $component — $file not found"
        return
    fi

    local before after
    before=$(cat "$file")
    sed -i "s|${image_name}:${SED_PATTERN}|${image_name}:${TAG}|g" "$file"
    after=$(cat "$file")

    if [[ "$before" != "$after" ]]; then
        echo "  UPDATED $component → $TAG"
        CHANGED=1
    else
        echo "  OK      $component (already $TAG)"
    fi
}

echo "Deploying tag: $TAG"
echo "Components: ${COMPONENTS[*]}"
echo ""

for component in "${COMPONENTS[@]}"; do
    case "$component" in
        orchestrator)
            update_image orchestrator \
                "$MANIFEST_DIR/20-orchestrator.yaml" \
                "superhuman-remote-worker-orchestrator"
            ;;
        agent)
            update_image agent \
                "$MANIFEST_DIR/21-agent.yaml" \
                "superhuman-remote-worker-agent"
            ;;
        cockpit)
            update_image cockpit \
                "$MANIFEST_DIR/22-cockpit.yaml" \
                "superhuman-remote-worker-cockpit"
            ;;
        mcp)
            update_image mcp \
                "$MANIFEST_DIR/23-mcp.yaml" \
                "superhuman-remote-worker-mcp"
            ;;
        vpn)
            update_image vpn \
                "$MANIFEST_DIR/15-vpn.yaml" \
                "superhuman-remote-worker-vpn"
            ;;
        vm-controller)
            update_image vm-controller \
                "$MANIFEST_DIR/../srw-vm-controller/vm-controller.yaml" \
                "superhuman-remote-worker-vm-controller"
            ;;
        vm-base)
            update_image vm-base \
                "$MANIFEST_DIR/../srw-vm-controller/vm-controller.yaml" \
                "superhuman-remote-worker-agent-vm-base"
            ;;
    esac
done

# --- Commit and push -------------------------------------------------------

echo ""

if [[ "$CHANGED" -eq 0 ]]; then
    echo "No changes — all manifests already at $TAG"
    exit 0
fi

cd "$REPO_DIR"

echo "Changes:"
git diff --stat

echo ""
read -rp "Commit and push? [Y/n] " confirm
confirm="${confirm:-Y}"

if [[ "$confirm" =~ ^[Yy]$ ]]; then
    git add deployments/superhuman-remote-worker/ deployments/srw-vm-controller/
    git commit -m "deploy: update SRW image tags to $TAG"
    git push
    echo ""
    echo "Pushed. Fleet will pick up changes within ~5 minutes."
else
    echo "Aborted. Manifest files are modified but not committed."
fi
