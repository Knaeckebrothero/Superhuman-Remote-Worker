"""Classify manifest execution references before definition retirement.

Execution specifications and revision rows are immutable history.  Their mere
presence cannot be a lifetime resource lease, but a terminal status alone is
also not process-zero or release authority.  This module owns the one locked
classification used by Project, child-resource, standalone-resource, and user
retirement.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal
from uuid import UUID


async def lock_manifest_execution_catalog(conn) -> None:
    """Order definition retirement against execution admission and Resume."""

    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('srw-resource-catalog', 0))"
    )


@dataclass(frozen=True, slots=True)
class ExecutionRetirementAssessment:
    execution_id: UUID
    work_kind: str
    work_id: UUID
    state: Literal["live", "retirement_unsettled", "historical_settled"]
    reason: str

    @property
    def blocks_retirement(self) -> bool:
        return self.state != "historical_settled"


def _metadata_object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"_malformed": True}
    return dict(value) if isinstance(value, dict) else {"_malformed": True}


def _session_metadata_retains_authority(value) -> bool:
    metadata = _metadata_object(value)
    if metadata.get("_malformed") is True:
        return True
    if any(
        key in metadata
        for key in (
            "_stateless_workspace_retirement_pending",
            "_stateless_claim_retirement",
            "_stateless_claim_loss_hold",
            "_stateless_claim_losses",
        )
    ):
        return True
    for key in (
        "workspace_container",
        "_workspace_binding",
        "vm",
        "agent_pod",
    ):
        if metadata.get(key) not in (None, {}):
            return True
    return False


async def classify_execution_references(
    conn,
    *,
    owner_id: UUID | None = None,
    retiring_owner_id: UUID | None = None,
    project_id: UUID | None = None,
    resource_ids: tuple[UUID, ...] | list[UUID] = (),
    dependency_ids: tuple[str, ...] | list[str] = (),
) -> tuple[ExecutionRetirementAssessment, ...]:
    """Lock and classify every matching execution on ``conn``.

    The caller already owns the catalog transaction/advisory lock.  Locks are
    acquired in stable identity order and this function never commits the
    caller's transaction or raises an HTTP-layer error.
    """

    resources = [UUID(str(value)) for value in resource_ids]
    dependencies = [str(value) for value in dependency_ids]
    specs = await conn.fetch(
        """SELECT s.* FROM srw_execution_specs s
        WHERE ($1::uuid IS NOT NULL AND s.owner_id=$1)
           OR ($2::uuid IS NOT NULL AND $2=ANY(s.project_ids))
           OR s.resource_id=ANY($3::uuid[])
           OR EXISTS(SELECT 1 FROM jsonb_array_elements(s.dependencies) d
                     WHERE d->>'uid'=ANY($4::text[]))
        ORDER BY s.id FOR UPDATE""",
        owner_id,
        project_id,
        resources,
        dependencies,
    )
    if not specs:
        return ()

    execution_ids = [row["id"] for row in specs]
    job_ids = sorted(
        (row["work_id"] for row in specs if row["work_kind"] == "Job"),
        key=str,
    )
    thread_ids = sorted(
        (row["work_id"] for row in specs if row["work_kind"] == "Session"),
        key=str,
    )
    jobs = {
        row["id"]: dict(row)
        for row in await conn.fetch(
            "SELECT id,status FROM jobs WHERE id=ANY($1::uuid[]) "
            "ORDER BY id FOR UPDATE",
            job_ids,
        )
    }
    threads = {
        row["id"]: dict(row)
        for row in await conn.fetch(
            """SELECT id,status,user_id,agent_id,execution_lane,metadata,
            runtime_authority_exposed,runtime_attach_token,
            runtime_retirement_token,runtime_retirement_authorized_at,
            runtime_retirement_stage_receipt,runtime_retirement_local_quiescence,
            runtime_retirement_external_cleanup,control_admission_agent_id
            FROM threads WHERE id=ANY($1::uuid[]) ORDER BY id FOR UPDATE""",
            thread_ids,
        )
    }

    attempts_by_execution: dict[UUID, list[dict]] = {}
    for row in await conn.fetch(
        """SELECT execution_id,attempt,phase,cleaned_at
        FROM srw_execution_attempts WHERE execution_id=ANY($1::uuid[])
        ORDER BY execution_id,attempt FOR UPDATE""",
        execution_ids,
    ):
        attempts_by_execution.setdefault(row["execution_id"], []).append(dict(row))

    direct_workspaces: set[UUID] = set()
    for row in await conn.fetch(
        """SELECT id,execution_id,status,pod_uid FROM srw_workspace_instances
        WHERE execution_id=ANY($1::uuid[]) ORDER BY id FOR UPDATE""",
        execution_ids,
    ):
        if (
            row["execution_id"] is not None
            or row["status"] != "Released"
            or row["pod_uid"] is not None
        ):
            direct_workspaces.add(row["execution_id"])

    bound_workspaces: set[UUID] = set()
    for row in await conn.fetch(
        """SELECT b.execution_id,w.id,w.status,w.execution_id,w.pod_uid
        FROM srw_execution_workspace_bindings b
        JOIN srw_workspace_instances w ON w.id=b.instance_id
        WHERE b.execution_id=ANY($1::uuid[])
        ORDER BY b.execution_id,w.id FOR UPDATE OF b,w""",
        execution_ids,
    ):
        if (
            row["status"] != "Released"
            or row["execution_id"] is not None
            or row["pod_uid"] is not None
        ):
            bound_workspaces.add(row["execution_id"])

    claim_threads = {
        row["thread_id"]
        for row in await conn.fetch(
            """SELECT thread_id,status FROM thread_agent_workspace_claims
            WHERE thread_id=ANY($1::uuid[]) AND status<>'reclaimed'
            ORDER BY thread_id FOR UPDATE""",
            thread_ids,
        )
    }
    provision_threads = {
        row["thread_id"]
        for row in await conn.fetch(
            """SELECT thread_id,status FROM thread_workspace_provision_intents
            WHERE thread_id=ANY($1::uuid[]) AND status<>'retired'
            ORDER BY thread_id,attempt_id FOR UPDATE""",
            thread_ids,
        )
    }
    protected_threads = {
        row["thread_id"]
        for row in await conn.fetch(
            """SELECT thread_id FROM cloud_ro_mounts
            WHERE thread_id=ANY($1::uuid[]) AND status='active'
            ORDER BY thread_id FOR UPDATE""",
            thread_ids,
        )
    }
    docker_threads = {
        row["owner_id"]
        for row in await conn.fetch(
            """SELECT owner_id FROM docker_workspace_leases
            WHERE owner_kind='thread' AND owner_id=ANY($1::uuid[])
              AND status IN ('ready','releasing')
            ORDER BY owner_id,host,port FOR UPDATE""",
            thread_ids,
        )
    }
    pending_cleanup_threads = {
        row["owner_id"]
        for row in await conn.fetch(
            """SELECT owner_id FROM managed_repository_workspace_cleanup_intents
            WHERE owner_kind='thread' AND owner_id=ANY($1::uuid[])
              AND (phase NOT IN ('settled','superseded') OR result_kind IS NULL)
            ORDER BY owner_id,intent_generation FOR UPDATE""",
            thread_ids,
        )
    }
    active_queue_threads = {
        row["unit_id"]
        for row in await conn.fetch(
            """SELECT unit_id,state FROM run_queue
            WHERE unit_kind='session_turn' AND unit_id=ANY($1::uuid[])
              AND state<>'done'
            ORDER BY unit_id FOR UPDATE""",
            thread_ids,
        )
    }

    assessments: list[ExecutionRetirementAssessment] = []
    for raw_spec in specs:
        spec = dict(raw_spec)
        execution_id = spec["id"]
        work_id = spec["work_id"]
        attempts = attempts_by_execution.get(execution_id, [])
        if any(
            attempt["cleaned_at"] is None
            or attempt["phase"] not in {"Succeeded", "Failed", "Cancelled"}
            for attempt in attempts
        ):
            state = "retirement_unsettled"
            reason = "execution_attempt_unsettled"
        elif execution_id in direct_workspaces or execution_id in bound_workspaces:
            state = "retirement_unsettled"
            reason = "execution_workspace_unsettled"
        elif spec["work_kind"] == "Job":
            job = jobs.get(work_id)
            if job is not None and job["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                state = "live"
                reason = "job_nonterminal"
            else:
                state = "historical_settled"
                reason = "job_terminal_history"
        else:
            thread = threads.get(work_id)
            if thread is None:
                state = "historical_settled"
                reason = "session_owner_deleted"
            elif thread["status"] != "ended":
                state = "live"
                reason = "session_nonterminal"
            elif (
                spec["owner_id"] is not None or thread["user_id"] is not None
            ) and not (
                retiring_owner_id is not None
                and spec["owner_id"] in {None, retiring_owner_id}
                and thread["user_id"] in {None, retiring_owner_id}
            ):
                state = "live"
                reason = "session_resumable_owner"
            elif (
                any(
                    thread.get(key) is not None
                    for key in (
                        "agent_id",
                        "runtime_attach_token",
                        "runtime_retirement_token",
                        "runtime_retirement_authorized_at",
                        "runtime_retirement_stage_receipt",
                        "runtime_retirement_local_quiescence",
                        "runtime_retirement_external_cleanup",
                        "control_admission_agent_id",
                    )
                )
                or thread.get("runtime_authority_exposed") is True
            ):
                state = "retirement_unsettled"
                reason = "session_runtime_authority_unsettled"
            elif _session_metadata_retains_authority(thread.get("metadata")):
                state = "retirement_unsettled"
                reason = "session_metadata_authority_unsettled"
            elif work_id in claim_threads:
                state = "retirement_unsettled"
                reason = "session_agent_claim_unsettled"
            elif work_id in provision_threads:
                state = "retirement_unsettled"
                reason = "session_workspace_provision_unsettled"
            elif work_id in protected_threads:
                state = "retirement_unsettled"
                reason = "session_protected_reader_unsettled"
            elif work_id in docker_threads:
                state = "retirement_unsettled"
                reason = "session_docker_lease_unsettled"
            elif work_id in pending_cleanup_threads:
                state = "retirement_unsettled"
                reason = "session_workspace_cleanup_unsettled"
            elif work_id in active_queue_threads:
                state = "retirement_unsettled"
                reason = "session_queue_unsettled"
            else:
                state = "historical_settled"
                reason = "session_terminal_ownerless_history"
        assessments.append(
            ExecutionRetirementAssessment(
                execution_id=execution_id,
                work_kind=spec["work_kind"],
                work_id=work_id,
                state=state,
                reason=reason,
            )
        )
    return tuple(assessments)


async def execution_references_block_retirement(conn, **filters) -> bool:
    return any(
        assessment.blocks_retirement
        for assessment in await classify_execution_references(conn, **filters)
    )


__all__ = [
    "ExecutionRetirementAssessment",
    "classify_execution_references",
    "execution_references_block_retirement",
    "lock_manifest_execution_catalog",
]
