"""Retire retained workspace claimants with immutable historical authority."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def retire_historical_claimant_pods(
    db: Any,
    *,
    claim: Mapping[str, str],
    current_pod: Mapping[str, Any],
    assert_current: Callable[[], Awaitable[None]],
    agent_provisioner: Any,
) -> None:
    """Remove old terminal claimant Pods before fencing their shared PVC.

    A published intent identifies a Pod, but does not prove its actor settled.
    Join it to the server-captured soft outcome or an exact recycle handoff.
    Neither a missing actor row nor a matching generation alone is authority.
    """

    await assert_current()
    intents = await db.fetch(
        "SELECT * FROM thread_agent_pod_provision_intents "
        "WHERE thread_id=$1::uuid AND workspace_claim_id=$2::uuid "
        "AND status='published' ORDER BY attempt_id",
        claim["thread_id"],
        claim["claim_id"],
    )
    outcomes = await db.fetch(
        "SELECT runtime_generation,agent_id,runtime_attach_token,retired_agent_pod "
        "FROM thread_runtime_retirement_outcomes WHERE thread_id=$1::uuid "
        "AND NOT permanent AND outcome='settled' "
        "AND retired_agent_pod->>'workspace_claim_id'=$2",
        claim["thread_id"],
        claim["claim_id"],
    )
    handoffs = await db.fetch(
        "SELECT * FROM thread_agent_pod_recycle_handoffs "
        "WHERE thread_id=$1::uuid AND workspace_claim_id=$2::uuid "
        "AND process_zero_protocol='finalized_exact_terminal_v1'",
        claim["thread_id"],
        claim["claim_id"],
    )
    # Validate the entire set before making the first historical mutation.
    candidates = []
    for intent in intents:
        attempt = str(intent["attempt_id"])
        if attempt == str(current_pod.get("provision_attempt") or ""):
            if not (
                str(intent["pod_uid"]) == str(current_pod.get("pod_uid") or "")
                and str(intent["pod_name"]) == str(current_pod.get("pod_name") or "")
                and str(intent["namespace"]) == str(current_pod.get("namespace") or "")
                and str(intent["runtime_generation"])
                == str(current_pod.get("runtime_generation") or "")
            ):
                raise RuntimeError(
                    "current claimant does not match its published intent"
                )
            continue
        if not (
            intent["protection_protocol"] == "finalizer_v1"
            and intent["provisioner"] == claim["provisioner"]
            and intent["namespace"] == claim["namespace"]
            and intent["pod_uid"]
        ):
            raise RuntimeError("historical claimant intent is incomplete")
        matches = []
        for outcome in outcomes:
            proof = _json(outcome["retired_agent_pod"])
            if not isinstance(proof, dict):
                continue
            expected = {
                "version": 1,
                "pod_name": str(intent["pod_name"]),
                "pod_uid": str(intent["pod_uid"]),
                "namespace": claim["namespace"],
                "provisioner": claim["provisioner"],
                "provision_attempt": attempt,
                "protection_protocol": "finalizer_v1",
                "workspace_claim_id": claim["claim_id"],
                "workspace_create_attempt": claim["create_attempt"],
                "workspace_created_runtime_generation": claim[
                    "created_runtime_generation"
                ],
                "pvc_name": claim["pvc_name"],
                "pvc_uid": claim["pvc_uid"],
            }
            if (
                proof == expected
                and str(outcome["runtime_generation"])
                == str(intent["runtime_generation"])
                and outcome["agent_id"] is not None
                and outcome["runtime_attach_token"] is not None
            ):
                matches.append(str(outcome["agent_id"]))
        recycle_proof = any(
            str(handoff["predecessor_attempt_id"]) == attempt
            and handoff["predecessor_pod_uid"] == intent["pod_uid"]
            and handoff["runtime_generation"] == intent["runtime_generation"]
            and handoff["namespace"] == intent["namespace"]
            and handoff["pod_name"] == intent["pod_name"]
            for handoff in handoffs
        )
        if len(set(matches)) > 1 or not (matches or recycle_proof):
            raise RuntimeError("historical claimant lacks immutable actor settlement")
        candidates.append((intent, matches[0] if matches else None))

    for intent, old_agent_id in candidates:
        await assert_current()
        actors = await db.fetch(
            "SELECT id,hostname,pod_uid,thread_id,current_job_id,status FROM agents "
            "WHERE pod_uid=$1 OR id=$2::uuid",
            str(intent["pod_uid"]),
            old_agent_id,
        )
        if any(
            str(actor["id"]) != old_agent_id
            or actor["hostname"] != intent["pod_name"]
            or actor["pod_uid"] != intent["pod_uid"]
            or actor["thread_id"] is not None
            or actor["current_job_id"] is not None
            or actor["status"] != "offline"
            for actor in actors
        ):
            raise RuntimeError("historical claimant has a live or ambiguous actor")
        labels = {
            "srw.io/runtime-generation": str(intent["runtime_generation"]),
            "srw.io/provision-attempt": str(intent["attempt_id"]),
        }
        if intent["provisioner"] == "agent":
            labels.update(
                {
                    "srw/managed-by": "agent-provisioner",
                    "srw/purpose": "session",
                    "srw.io/thread-id": claim["thread_id"],
                }
            )
        else:
            labels.update(
                {
                    "srw/component": "persistent-agent",
                    "srw/thread-id": claim["thread_id"],
                }
            )
        successors = frozenset(
            str(other["pod_uid"])
            for other in intents
            if other["pod_name"] == intent["pod_name"]
            and other["namespace"] == intent["namespace"]
            and other["attempt_id"] != intent["attempt_id"]
        )
        if not await agent_provisioner.retire_historical_claimant_pod_exact(
            pod_name=str(intent["pod_name"]),
            pod_uid=str(intent["pod_uid"]),
            namespace=claim["namespace"],
            expected_labels=labels,
            pvc_name=claim["pvc_name"],
            known_successor_uids=successors,
        ):
            raise RuntimeError("historical claimant Pod retirement is retryable")
