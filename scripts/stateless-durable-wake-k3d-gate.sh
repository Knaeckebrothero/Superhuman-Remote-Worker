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

mapfile -t orchestrators < <(
    kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get pods \
        -l "$SELECTOR" --field-selector=status.phase=Running \
        -o name | LC_ALL=C sort
)
if [[ "${#orchestrators[@]}" -eq 0 ]]; then
    echo "Refusing: no running orchestrator pod was found in $NAMESPACE." >&2
    exit 2
fi

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
            test -f /app/operator_cli/stateless_wake_acceptance.py
            test -f /app/database/migrations/app/0185_stateless_input_deliveries.sql
            test -f /app/database/migrations/app/0186_stateless_input_delivery_validate.sql
            grep -q "terminal_replay = await" /app/src/shared/persistent_input_delivery.py
        '
    image_id="$(
        kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
            -o jsonpath='{.status.containerStatuses[?(@.name=="orchestrator")].imageID}'
    )"
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
                grep -q "claim_stateless_input_delivery" /app/src/api/turn_executor.py
                grep -q "input_delivery_capable_lease_token" /app/src/shared/persistent_input_delivery.py
                grep -q "terminal_replay = await" /app/src/shared/persistent_input_delivery.py
            '
        image_id="$(
            kubectl --context="$EXPECTED_CONTEXT" -n "$NAMESPACE" get "$pod_ref" \
                -o jsonpath='{.status.containerStatuses[?(@.name=="agent")].imageID}'
        )"
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
    python -m operator_cli.stateless_wake_acceptance "$@"
