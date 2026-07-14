#!/usr/bin/env bash
# Verify the chart-managed development Secret without displaying its values.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_DIR="${1:-$ROOT_DIR/helm}"
VALUES_FILE="${2:-$ROOT_DIR/helm/ci/eval-values.yaml}"
manifest="$(mktemp)"
runtime_manifest="$(mktemp)"
trap 'rm -f "$manifest" "$runtime_manifest"' EXIT
chmod 600 "$manifest" "$runtime_manifest"

helm template srw "$CHART_DIR" \
  --values "$VALUES_FILE" \
  --show-only templates/secret.yaml >"$manifest"

if ! grep -Fq '"helm.sh/resource-policy": keep' "$manifest"; then
  echo "ERROR: chart-managed Secret would be deleted during uninstall" >&2
  exit 1
fi

required_keys=(
  APP_ENCRYPTION_KEY
  MCP_INTERNAL_KEY
  GARAGE_ADMIN_TOKEN
  GARAGE_RPC_SECRET
  SNAPSHOT_S3_ACCESS_KEY_ID
  SNAPSHOT_S3_SECRET_ACCESS_KEY
  VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID
  VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY
  CI_SECRET_RENDER_SENTINEL
)

for key in "${required_keys[@]}"; do
  count="$(grep -Ec "^  ${key}:[[:space:]]" "$manifest" || true)"
  if [[ "$count" != "1" ]]; then
    echo "ERROR: expected exactly one indented stringData entry for ${key}; found ${count}" >&2
    exit 1
  fi
done

if grep -Eq "^($(IFS='|'; echo "${required_keys[*]}")):[[:space:]]" "$manifest"; then
  echo "ERROR: a chart-managed Secret key escaped stringData indentation" >&2
  exit 1
fi

for key in SNAPSHOT_S3_ACCESS_KEY_ID VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID; do
  if ! grep -Eq "^  ${key}: \"GK[0-9A-Fa-f]{24}\"$" "$manifest"; then
    echo "ERROR: ${key} is not in Garage native format" >&2
    exit 1
  fi
done

for key in GARAGE_RPC_SECRET SNAPSHOT_S3_SECRET_ACCESS_KEY VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY; do
  if ! grep -Eq "^  ${key}: \"[0-9A-Fa-f]{64}\"$" "$manifest"; then
    echo "ERROR: ${key} is not in Garage native format" >&2
    exit 1
  fi
done

expect_render_failure() {
  local scenario="$1"
  shift
  if helm template srw "$CHART_DIR" --values "$VALUES_FILE" "$@" \
      >/dev/null 2>&1; then
    echo "ERROR: chart accepted ${scenario}" >&2
    exit 1
  fi
}

expect_render_failure \
  "a partial Garage S3 credential pair" \
  --set-string secrets.values.VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID=GK111111111111111111111111
expect_render_failure \
  "a malformed Garage S3 secret" \
  --set-string secrets.values.SNAPSHOT_S3_SECRET_ACCESS_KEY=not-garage-format

helm template srw "$CHART_DIR" \
  --values "$VALUES_FILE" \
  --show-only templates/objectstore/garage-bootstrap-job.yaml \
  --show-only templates/objectstore/garage-statefulset.yaml >"$runtime_manifest"

for contract in \
  'showSecretKey=true' \
  'existing Garage key $keyname has a different secret' \
  'removed stale chart-managed Garage key' \
  '$ADMIN/v1/key?id=$stale_id' \
  'umask 077' \
  'reloader.stakater.com/auto: "true"'; do
  if ! grep -Fq "$contract" "$runtime_manifest"; then
    echo "ERROR: Garage runtime credential contract is missing: ${contract}" >&2
    exit 1
  fi
done

echo "Chart-created Secret render contract passed"
