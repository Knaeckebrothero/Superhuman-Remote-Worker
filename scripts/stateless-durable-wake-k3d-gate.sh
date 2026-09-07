#!/usr/bin/env bash
# Bounded host wrapper for the disposable stateless durable-wake acceptance gate.
#
# Read-only preflight:
#   scripts/stateless-durable-wake-k3d-gate.sh
#
# Full disposable gate (a known approved local user UUID is required):
#   scripts/stateless-durable-wake-k3d-gate.sh --execute \
#     --run-id wake-gate-20260826-001 \
#     --owner-user-id 00000000-0000-0000-0000-000000000000 \
#     --confirm k3d-srw-disposable-stateless-wake
#
# Exact recovery cleanup after an interrupted run:
#   scripts/stateless-durable-wake-k3d-gate.sh --cleanup-only \
#     --run-id wake-gate-20260826-001 \
#     --confirm k3d-srw-disposable-stateless-wake
set -euo pipefail

EXPECTED_CONTEXT="k3d-srw"
NAMESPACE="srw"
SELECTOR="app.kubernetes.io/component=orchestrator"
STATELESS_SELECTOR="app.kubernetes.io/component=agent-stateless"
ORCHESTRATOR_DEPLOYMENT="srw-orchestrator"
STATELESS_DEPLOYMENT="srw-agent-stateless"

actual_context="$(kubectl config current-context)"
if [[ "$actual_context" != "$EXPECTED_CONTEXT" ]]; then
    echo "Refusing: current kubectl context is not $EXPECTED_CONTEXT." >&2
    exit 2
fi

cleanup_only=false
for argument in "$@"; do
    if [[ "$argument" == "--cleanup-only" ]]; then
        cleanup_only=true
    fi
done

require_dark_flag() {
    local key="$1"
    local value
    value="$(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" \
            get configmap srw-config -o "jsonpath={.data.${key}}"
    )"
    if [[ "$value" != "false" ]]; then
        echo "Refusing: $key is not false in srw-config." >&2
        exit 2
    fi
}

require_deployment_converged() {
    local deployment="$1"
    local selector="$2"
    local minimum="$3"
    local desired generation observed updated ready available unavailable
    desired="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.spec.replicas}')"
    generation="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.metadata.generation}')"
    observed="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.status.observedGeneration}')"
    updated="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.status.updatedReplicas}')"
    ready="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.status.readyReplicas}')"
    available="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.status.availableReplicas}')"
    unavailable="$(kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get deployment "$deployment" -o jsonpath='{.status.unavailableReplicas}')"
    unavailable="${unavailable:-0}"
    if [[ ! "$desired" =~ ^[0-9]+$ || ! "$generation" =~ ^[0-9]+$ || ! "$observed" =~ ^[0-9]+$ || \
          ! "$updated" =~ ^[0-9]+$ || ! "$ready" =~ ^[0-9]+$ || ! "$available" =~ ^[0-9]+$ || \
          ! "$unavailable" =~ ^[0-9]+$ || "$desired" -lt "$minimum" || "$observed" -lt "$generation" || \
          "$updated" -ne "$desired" || "$ready" -ne "$desired" || "$available" -ne "$desired" || \
          "$unavailable" -ne 0 ]]; then
        echo "Refusing: deployment $deployment is not fully converged." >&2
        exit 2
    fi
    local all_count
    all_count="$(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get pods \
            -l "$selector" -o name | wc -l
    )"
    if [[ "$all_count" -ne "$desired" ]]; then
        echo "Refusing: deployment $deployment still has an old or missing Pod." >&2
        exit 2
    fi
}

require_dark_flag WORKSPACE_CLEANUP_RECONCILIATION_ENABLED
require_dark_flag WORKSPACE_REATTACH_FRESH_FALLBACK
require_dark_flag OFFICER_AUTO_PULL_RELEASE_ENABLED
require_deployment_converged "$ORCHESTRATOR_DEPLOYMENT" "$SELECTOR" 1
if [[ "$cleanup_only" != true ]]; then
    require_deployment_converged "$STATELESS_DEPLOYMENT" "$STATELESS_SELECTOR" 2
fi

mapfile -t orchestrators < <(
    kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get pods \
        -l "$SELECTOR" --field-selector=status.phase=Running \
        -o name | LC_ALL=C sort
)
if [[ "${#orchestrators[@]}" -eq 0 ]]; then
    echo "Refusing: no running orchestrator pod was found in $NAMESPACE." >&2
    exit 2
fi
orchestrator_image_id=""

# Artifact truth is checked in every running orchestrator before any CLI
# argument can authorize mutation. A rollout event or image tag is not proof
# that the process serving this gate contains the repaired code.
for pod_ref in "${orchestrators[@]}"; do
    ready="$(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
            -o jsonpath='{.status.containerStatuses[?(@.name=="orchestrator")].ready}'
    )"
    if [[ "$ready" != "true" ]]; then
        echo "Refusing: $pod_ref is not Ready." >&2
        exit 2
    fi
    kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" \
        exec "$pod_ref" -c orchestrator -- sh -ceu '
            test -f /app/src/orchestrator/operator_cli/stateless_wake_acceptance.py
            test -f /app/src/orchestrator/database/migrations/app/0191_stateless_input_deliveries.sql
            test -f /app/src/orchestrator/database/migrations/app/0192_stateless_input_delivery_validate.sql
            test -f /app/src/orchestrator/database/migrations/app/0197_non_pinned_workspace_process_zero.sql
            test -f /app/src/orchestrator/database/migrations/app/0198_non_pinned_workspace_lifecycle_authority.sql
            test "$WORKSPACE_CLEANUP_RECONCILIATION_ENABLED" = false
            test "$WORKSPACE_REATTACH_FRESH_FALLBACK" = false
            test "$OFFICER_AUTO_PULL_RELEASE_ENABLED" = false
            grep -q "terminal_replay = await" /app/src/shared/persistent_input_delivery.py
            grep -q "K8s Pod IPs are not recipient authority" /app/src/orchestrator/services/session_wake.py
        '
    image_id="$(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
            -o jsonpath='{.status.containerStatuses[?(@.name=="orchestrator")].imageID}'
    )"
    if [[ -n "$orchestrator_image_id" && "$orchestrator_image_id" != "$image_id" ]]; then
        echo "Refusing: orchestrator Pods do not run one image digest." >&2
        exit 2
    fi
    orchestrator_image_id="$image_id"
    printf 'artifact-pass pod=%s image_id=%s\n' "${pod_ref#pod/}" "$image_id"
done

if [[ "$cleanup_only" != true ]]; then
    mapfile -t stateless_executors < <(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get pods \
            -l "$STATELESS_SELECTOR" --field-selector=status.phase=Running \
            -o name | LC_ALL=C sort
    )
    if [[ "${#stateless_executors[@]}" -lt 2 ]]; then
        echo "Refusing: fewer than two running stateless executor pods were found." >&2
        exit 2
    fi
    stateless_image_id=""
    for pod_ref in "${stateless_executors[@]}"; do
        ready="$(
            kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
                -o jsonpath='{.status.containerStatuses[?(@.name=="agent")].ready}'
        )"
        if [[ "$ready" != "true" ]]; then
            echo "Refusing: $pod_ref is not Ready." >&2
            exit 2
        fi
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" \
            exec "$pod_ref" -c agent -- sh -ceu '
                grep -q "claim_stateless_input_delivery" /app/src/agent/api/turn_executor.py
                grep -q "input_delivery_capable_lease_token" /app/src/shared/persistent_input_delivery.py
                grep -q "terminal_replay = await" /app/src/shared/persistent_input_delivery.py
                grep -q "workspace_runtime_incarnation" /app/src/agent/api/turn_executor.py
            '
        image_id="$(
            kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
                -o jsonpath='{.status.containerStatuses[?(@.name=="agent")].imageID}'
        )"
        if [[ -n "$stateless_image_id" && "$stateless_image_id" != "$image_id" ]]; then
            echo "Refusing: stateless Pods do not run one image digest." >&2
            exit 2
        fi
        stateless_image_id="$image_id"
        printf 'artifact-pass pod=%s image_id=%s\n' "${pod_ref#pod/}" "$image_id"
    done
fi

# One exact Ready pod owns the operator process. It talks to PostgreSQL and the
# Kubernetes API through the deployed service account. No DSN or credential is
# copied to the host or printed by the module.
operator_pod="${orchestrators[0]}"
kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" \
    exec "$operator_pod" -c orchestrator -- \
    env SRW_WAKE_GATE_CONTEXT="$EXPECTED_CONTEXT" \
    python -m orchestrator.operator_cli.stateless_wake_acceptance "$@"
