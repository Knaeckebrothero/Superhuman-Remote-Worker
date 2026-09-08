"""HTTP adapters for the agent-facing protected-cloud stage and lifecycle reads.

All three routes are **internal** (P4b) — they require ``X-Internal-Key`` and
the ingress strips these paths. The gate fires first on every one of them,
before the protected-cloud flag, before any store read, and before the
task-registry work.

Two properties are load-bearing and moved unchanged:

* **The stage trigger is fire-and-forget with a de-duped key.** The task key
  spans the thread, its runtime generation, its workspace generation and the
  expected staged epoch (or ``<thread>:vm`` for the VM tier), so a slow stage
  from one turn is not raced by a second ping landing before it finishes.
  ``stage_thread_cloud_diff`` debounces internally too; skipping the duplicate
  task here just avoids piling up redundant scheduled coroutines under a fast
  turn cadence. The registry (port contract §3.1) owns creation, de-duping and
  eviction.
* **A retiring pinned runtime gets exactly one narrow read.**
  ``agent_get_thread_lifecycle`` closes credential delivery while retiring but
  still lets the *exact* old process discover the immutable token and
  disposition, and it re-reads + re-checks ownership after the awaiting gate so
  a G1 caller cannot be handed a resumed G2 snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.security.access import require_internal
from orchestrator.services import cloud_stage_authority

router = APIRouter()


class CloudStageTaskRegistry(Protocol):
    """The application's ``CloudTaskRegistry`` (port contract §3.1).

    Only the ``stage_*`` half is used here; lane M owns the protected-engage
    half. ``stage_start`` is a de-duping create — it is a no-op when the key is
    already present — and the registry, not this module, pops the slot when the
    task finishes.
    """

    def stage_has(self, key: str) -> bool: ...

    def stage_start(
        self, key: str, factory: Callable[[], Coroutine[Any, Any, None]]
    ) -> None: ...


@dataclass(frozen=True)
class AgentCloudStageDependencies:
    """Collaborators for one agent cloud-stage request, resolved per invocation.

    ``store``, ``snapshots`` and ``vm_provisioner`` are application singletons
    that ``lifespan`` rebinds and tests replace wholesale; binding them at
    import would pin the pre-lifespan objects. ``cloud_tasks`` is the one
    application-owned task registry. The two flag/ownership callables stay
    owned by the application (the workspace-gate batch) and are injected.
    """

    store: Any
    snapshots: Any
    vm_provisioner: Any
    cloud_tasks: CloudStageTaskRegistry
    is_protected_cloud_mode_enabled: Callable[[], bool]
    require_pinned_workspace_credential_owner: Callable[..., Awaitable[Any]]
    capture_cloud_stage_authority: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any] | None
    ] = cloud_stage_authority._capture_cloud_stage_authority
    require_internal: Callable[..., Awaitable[Any]] = require_internal


def get_agent_cloud_stage_dependencies(
    request: Request,
) -> AgentCloudStageDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.agent_cloud_stage_dependencies_factory()


@router.post("/api/agents/threads/{thread_id}/cloud-stage")
async def agent_trigger_cloud_stage(
    request: Request,
    thread_id: str,
    *,
    dependencies: AgentCloudStageDependencies = Depends(
        get_agent_cloud_stage_dependencies
    ),
) -> dict[str, Any]:
    """Internal — agent turn-end ping. Fire-and-forget staging of the thread's
    protected-cloud upperdir to S3 (Slice C spec §5). Ingress strips this path.
    """
    await dependencies.require_internal(request)
    if not dependencies.is_protected_cloud_mode_enabled():
        return {"skipped": "flag_off"}
    from orchestrator.services.cloud_staging.stage import stage_thread_cloud_diff

    headers = getattr(request, "headers", {})
    thread = await dependencies.store.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    await dependencies.require_pinned_workspace_credential_owner(
        thread,
        headers.get("X-Agent-ID"),
        headers.get("X-Session-Runtime-Generation"),
        headers.get("X-Session-Runtime-Attach-Token"),
    )
    row = await dependencies.store.get_ro_mount_by_thread(thread_id)
    authority = dependencies.capture_cloud_stage_authority(thread, row or {})
    if (
        authority is None
        and not cloud_stage_authority._thread_selected_vm_workspace(
            thread, vm_provisioner=dependencies.vm_provisioner
        )
    ) or (
        authority is not None and authority.get("runtime_retirement_token") is not None
    ):
        raise HTTPException(
            status_code=409, detail={"code": "cloud_stage_authority_unavailable"}
        )
    task_key = cloud_stage_authority._cloud_stage_task_key(thread_id, authority)

    async def _run() -> None:
        # The registry pops ``task_key`` from its own done-callback, so this
        # coroutine deliberately drops main's ``finally``: evicting from inside
        # the task could clobber a newer registration for the same key.
        async with dependencies.store.thread_advisory_lock(thread_id):
            result = await stage_thread_cloud_diff(
                thread_id=thread_id,
                postgres_db=dependencies.store,
                snapshot_service=dependencies.snapshots,
                authority=authority,
                vm_provisioner=dependencies.vm_provisioner,
            )
        cloud_stage_authority._broadcast_cloud_stage_result(result)

    if not dependencies.cloud_tasks.stage_has(task_key):
        dependencies.cloud_tasks.stage_start(task_key, _run)
    return {"scheduled": True}


@router.get("/api/agents/threads/{thread_id}/lifecycle")
async def agent_get_thread_lifecycle(
    request: Request,
    thread_id: str,
    *,
    dependencies: AgentCloudStageDependencies = Depends(
        get_agent_cloud_stage_dependencies
    ),
) -> dict[str, Any]:
    """Return lifecycle fields the agent needs for self-cleanup polling.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips.

    Minimal projection so the agent's thread-status watchdog (PR 2) can
    decide whether to exit without dragging in the full thread payload.
    """
    await dependencies.require_internal(request)
    headers = getattr(request, "headers", {})
    presented_agent_id = headers.get("X-Agent-ID")
    presented_runtime_generation = headers.get("X-Session-Runtime-Generation")
    presented_attach_token = headers.get("X-Session-Runtime-Attach-Token")
    thread = await dependencies.store.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if str(thread.get("execution_lane") or "") == "pinned":
        if thread.get("runtime_retirement_token") is not None:
            # Credential delivery remains closed while retiring, but the
            # exact old process needs one narrow read-only channel to discover
            # the immutable token/disposition and finish strict local cleanup.
            try:
                parsed_thread = UUID(str(thread_id))
                parsed_agent = UUID(str(presented_agent_id))
                parsed_generation = UUID(str(presented_runtime_generation))
                parsed_attach = UUID(str(presented_attach_token))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "pinned_runtime_identity_required"},
                ) from exc
            async with dependencies.store.acquire() as conn:
                pending = await conn.fetchrow(
                    "SELECT t.status, t.agent_id, t.runtime_generation, "
                    "t.runtime_attach_token, t.runtime_retirement_token, "
                    "t.runtime_retirement_permanent, "
                    "t.runtime_retirement_authorized_at, "
                    "t.runtime_retirement_context, t.ended_at "
                    "FROM threads t WHERE t.id=$1::uuid "
                    "AND t.execution_lane='pinned' "
                    "AND t.runtime_generation=$2::uuid "
                    "AND t.agent_id=$3::uuid "
                    "AND t.runtime_attach_token=$4::uuid "
                    "AND t.runtime_retirement_token IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM agents a WHERE a.id=$3::uuid "
                    "AND a.thread_id=t.id) FOR SHARE",
                    parsed_thread,
                    parsed_generation,
                    parsed_agent,
                    parsed_attach,
                )
            if pending is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "pinned_runtime_identity_mismatch"},
                )
            thread = dict(pending)
        else:
            await dependencies.require_pinned_workspace_credential_owner(
                thread,
                presented_agent_id,
                presented_runtime_generation,
                presented_attach_token,
            )
            # The reciprocal query above awaits. Re-read and make its exact owner
            # query the final await so G1 cannot miss End and accept a resumed G2
            # lifecycle snapshot as its own.
            thread = await dependencies.store.get_thread(thread_id)
            if not thread:
                raise HTTPException(status_code=404, detail="Thread not found")
            await dependencies.require_pinned_workspace_credential_owner(
                thread,
                presented_agent_id,
                presented_runtime_generation,
                presented_attach_token,
            )
    retirement_context = thread.get("runtime_retirement_context") or {}
    if isinstance(retirement_context, str):
        try:
            retirement_context = json.loads(retirement_context)
        except (TypeError, ValueError):
            retirement_context = {}
    retirement_pending = thread.get("runtime_retirement_token") is not None
    retirement_authorized = bool(
        retirement_pending
        and thread.get("runtime_retirement_authorized_at") is not None
    )
    return {
        "status": "ending" if retirement_authorized else thread.get("status"),
        "agent_id": str(thread.get("agent_id")) if thread.get("agent_id") else None,
        "session_runtime_generation": (
            str(thread.get("runtime_generation"))
            if thread.get("runtime_generation")
            else None
        ),
        "session_runtime_attach_token": (
            str(thread.get("runtime_attach_token"))
            if thread.get("runtime_attach_token")
            else None
        ),
        "runtime_retirement_pending": bool(retirement_pending),
        "runtime_retirement_preflight": bool(
            retirement_pending and not retirement_authorized
        ),
        "runtime_retirement_authorized": retirement_authorized,
        "retirement_permanent": bool(
            retirement_authorized and thread.get("runtime_retirement_permanent") is True
        ),
        "retirement_disposition": (
            str(retirement_context.get("settle_status"))
            if retirement_authorized and isinstance(retirement_context, Mapping)
            else None
        ),
        "session_runtime_retirement_token": (
            str(thread.get("runtime_retirement_token"))
            if retirement_authorized
            else None
        ),
        "ended_at": thread.get("ended_at").isoformat()
        if thread.get("ended_at")
        else None,
    }


@router.get("/api/agents/threads/{thread_id}/retirement-outcome")
async def agent_get_thread_retirement_outcome(
    request: Request,
    thread_id: str,
    *,
    dependencies: AgentCloudStageDependencies = Depends(
        get_agent_cloud_stage_dependencies
    ),
) -> dict[str, Any]:
    """Read back one exact final retirement after a lost settlement response.

    This internal endpoint never returns the current thread/agent/workspace.
    The full old T/G/process/disposition tuple either names an append-only
    terminal receipt, names the same still-pending attempt, or proves nothing.
    A generic 404, generation change, or successor therefore cannot be
    mistaken for the caller's successful settlement.
    """

    await dependencies.require_internal(request)
    headers = getattr(request, "headers", {})
    agent_id = headers.get("X-Agent-ID")
    generation = headers.get("X-Session-Runtime-Generation")
    attach_token = headers.get("X-Session-Runtime-Attach-Token")
    retirement_token = headers.get("X-Session-Runtime-Retirement-Token")
    disposition = str(headers.get("X-Retirement-Disposition") or "")
    permanent_raw = str(headers.get("X-Retirement-Permanent") or "").lower()
    if permanent_raw not in {"true", "false"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "pinned_retirement_outcome_identity_required"},
        )
    outcome = await dependencies.store.get_pinned_thread_retirement_outcome(
        thread_id,
        runtime_generation=str(generation or ""),
        retirement_token=str(retirement_token or ""),
        agent_id=str(agent_id or ""),
        runtime_attach_token=str(attach_token or ""),
        disposition=disposition,
        permanent=permanent_raw == "true",
    )
    if outcome is not None:
        return outcome

    current = await dependencies.store.get_thread(thread_id)
    context = (current or {}).get("runtime_retirement_context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            context = {}
    if (
        isinstance(current, Mapping)
        and isinstance(context, Mapping)
        and str(current.get("runtime_generation") or "") == str(generation or "")
        and str(current.get("runtime_retirement_token") or "")
        == str(retirement_token or "")
        and str(current.get("agent_id") or "") == str(agent_id or "")
        and str(current.get("runtime_attach_token") or "") == str(attach_token or "")
        and str(context.get("settle_status") or "") == disposition
        and bool(current.get("runtime_retirement_permanent"))
        == (permanent_raw == "true")
        and current.get("runtime_retirement_authorized_at") is not None
    ):
        return {
            "status": "ending",
            "retirement_disposition": disposition,
            "retirement_permanent": permanent_raw == "true",
        }
    raise HTTPException(
        status_code=409,
        detail={"code": "pinned_retirement_outcome_unproven"},
    )
