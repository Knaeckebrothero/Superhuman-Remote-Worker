"""Thread projection routes: detail, session state, controls, tool visibility.

Extracted from ``orchestrator.main`` (R1.B10). Owns the thread detail/state
projections, metadata redaction, durable control submission, tool-group
visibility and preview, citations, and message history. The database and
vector-store handles and the main-owned session/tool collaborators are
resolved per request from the owning application — this module imports no
application startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from orchestrator.schemas.thread_admission import ThreadUpdateRequest
from orchestrator.schemas.thread_projection import (
    ThreadControlRequest,
    ToolGroupPreviewRequest,
)
from orchestrator.security.access import (
    log_security_event,
    redact_config_override,
    require_thread_owner,
)
from orchestrator.security.auth import require_approved_user
from orchestrator.services import citations as citations_operations
from orchestrator.services import job_projection, session_config_resolution
from orchestrator.services.agent_toolset_probe import (
    origin_fields as _origin_fields,
    unmeasured as _unmeasured,
)
from orchestrator.services.config_overrides import (
    looks_like_uuid as _looks_like_uuid,
)
from orchestrator.services.container_provisioner import (
    WORKSPACE_RUNTIME_INCARNATION_KEY,
)
from orchestrator.services.deployment_gates import (
    is_experts_db_enabled as _is_experts_db_enabled,
    require_pinned_status_identity as _require_pinned_status_identity,
)
from orchestrator.services.grant_enforcement import GrantDenied
from orchestrator.services.session_class_policy import (
    require_stateless_workspace as _require_stateless_workspace,
)
from orchestrator.services.session_runtime_admission import (
    protected_cloud_marker_state,
)
from orchestrator.services.session_state_snapshot import (
    build_session_state_snapshot,
)
from orchestrator.services.session_tool_policy import (
    legacy_session_tool_policy as _legacy_session_tool_policy,
    merged_session_tool_policy as _merged_session_tool_policy,
)
from orchestrator.services.stateless_workspace_gate import thread_metadata_object
from orchestrator.services.thread_control_inbox import (
    ControlAdmissionError,
    ControlAdmissionNotReady,
    admit_thread_control,
    find_existing_thread_control,
)
from shared.runtime.core.loader import canonical_config_name
from shared.runtime.core.tool_policy import enumerate_only_members
from shared.runtime.core.tool_report import (
    compose_tool_view,
    tool_groups_from_view,
)
from shared.tool_catalog import TOOL_REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class ThreadProjectionDependencies:
    """Per-request collaborators resolved from the owning application."""

    store: Any
    vector_db: Any
    resolve_cloud_session_url: Any
    resolve_session_config: Any
    enforce_session_create_grants: Any
    acknowledged_grant_strip: Any
    agent_toolset_measurement: Any
    prefetch_roster_refs: Any
    resolve_runner_grants: Any
    session_config_dependencies: Any
    user_experts_enabled: Any


def get_thread_projection_dependencies(
    request: Request,
) -> ThreadProjectionDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.thread_projection_dependencies_factory()


def _redact_nested_workspace_state(
    record: dict[str, Any], *, field: str
) -> dict[str, Any]:
    """Shared thread/job redaction; policy lives in job_projection."""
    return job_projection.redact_nested_workspace_state(
        record, field=field, runtime_incarnation_key=WORKSPACE_RUNTIME_INCARNATION_KEY
    )


def _redact_thread_metadata(thread: dict[str, Any]) -> dict[str, Any]:
    """Parse and strip credential fields from a thread's ``metadata`` before
    it leaves over REST.

    ``metadata`` is a JSONB column asyncpg hands back as a JSON *string*.
    This helper used to "re-serialize to the original representation", which
    meant the owner-facing thread endpoints returned metadata as a string —
    silently breaking every Cockpit consumer typed against
    ``metadata?: Record<string, unknown>`` (settings-pane config/tools
    prefill, the attached-datasource default, and the REST model/temperature
    seeding — the long-standing "model shows the config name until the
    welcome frame" oddity). The contract is now: metadata always leaves as a
    parsed OBJECT (unparseable/absent → ``{}``).
    """
    raw_retirement_context = thread.get("runtime_retirement_context") or {}
    if isinstance(raw_retirement_context, str):
        try:
            raw_retirement_context = json.loads(raw_retirement_context)
        except (json.JSONDecodeError, TypeError):
            raw_retirement_context = {}
    # The token is installed before abortable turn/Officer preflight.  Only
    # the append-only authorized edge is a public `ending` state; exposing the
    # hidden preflight would make Cockpit retire control even when a non-force
    # End is about to abort as an observational no-op.
    retirement_pending = bool(
        thread.get("runtime_retirement_token") is not None
        and thread.get("runtime_retirement_authorized_at") is not None
    )
    retirement_disposition: str | None = None
    if retirement_pending and isinstance(raw_retirement_context, Mapping):
        candidate = str(raw_retirement_context.get("settle_status") or "")
        if candidate in {"ended", "suspended"}:
            retirement_disposition = candidate

    thread = _redact_nested_workspace_state(thread, field="metadata")
    md = thread.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (json.JSONDecodeError, TypeError):
            md = {}
    if not isinstance(md, dict):
        md = {}
    thread = dict(thread)
    md = dict(md)
    if "config_override" in md:
        md["config_override"] = redact_config_override(md["config_override"])
    md.pop("_workspace_binding", None)
    md.pop("_stateless_workspace_process_zero_observation", None)
    thread["metadata"] = md
    # These are internal capabilities or immutable physical cleanup evidence,
    # not owner API fields.  Never let a broad SELECT * list/detail response
    # leak them.  Cockpit gets only the durable, non-secret lifecycle shape.
    for internal_key in (
        "runtime_generation",
        "runtime_attach_token",
        "runtime_attach_abort_receipt",
        "runtime_authority_exposed",
        "runtime_retirement_token",
        "runtime_retirement_permanent",
        "runtime_retirement_started_at",
        "runtime_retirement_authorized_at",
        "runtime_retirement_context",
        "runtime_retirement_stage_receipt",
        "runtime_retirement_local_quiescence",
        "runtime_retirement_external_cleanup",
    ):
        thread.pop(internal_key, None)
    thread["runtime_retirement_pending"] = retirement_pending
    thread["retirement_disposition"] = retirement_disposition
    return thread


async def _session_tool_grants(
    dependencies: ThreadProjectionDependencies, thread: dict[str, Any]
) -> dict[str, Any] | None:
    """The owner's capability grants, for explaining an ``unavailable``.

    ``None`` means "impose no grant-based restriction" — both for an admin
    (``dependencies.resolve_runner_grants`` returns ``None``) and for a lookup failure. A
    read surface must never INVENT a denial: the PDP at attach and dispatch is
    the enforcement, this is only the explanation, and a fabricated
    "unavailable — needs the shell_tools grant" is its own D1 violation.
    """
    try:
        project_ids = [str(thread["project_id"])] if thread.get("project_id") else []
        return await dependencies.resolve_runner_grants(
            runner_user_id=str(thread.get("user_id"))
            if thread.get("user_id")
            else None,
            project_ids=project_ids,
        )
    except Exception:
        logger.warning(
            "Tool-group grant lookup failed for thread %s; reporting no "
            "grant-based restrictions",
            thread.get("id"),
        )
        return None


def _stamp_tool_categories(messages: list[dict[str, Any]]) -> None:
    """Annotate replayed tool calls with their registry category, in place.

    The live SSE ``tool.started`` frame carries ``category`` (see graph.py's
    ``_get_tool_category``), but the stored ``thread_messages.tool_calls`` JSONB
    never did. Without this the cockpit's folded-chip summary buckets every
    replayed call as "other", so one turn reads
    "19× citations · 12× searches" while streaming and "38× steps" after a
    reload — same turn, same data, different answer.

    Derived at read time rather than persisted so that re-categorising a tool
    doesn't need a backfill of historical rows. Unknown tools (renamed, removed,
    or from another deployment) simply get no category and fall back to the
    cockpit's "other" bucket, which is the honest answer.
    """
    for m in messages:
        for tc in m.get("tool_calls") or []:
            category = TOOL_REGISTRY.get(tc.get("name") or "", {}).get("category")
            if category:
                tc["category"] = category


@router.get("/api/persistent/threads/{thread_id}")
async def get_thread(
    thread_id: str,
    request: Request,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """Get thread status and metadata (auth: owner only).

    Phase 1 of cloud_collaboration_model.md §9 surfaces the thread's
    attached mounts here so the Cockpit "Project files" panel can render
    them without a second round-trip. ``project_ids`` is the derived
    list-of-strings view kept stable for callers that only need scoping.

    Threads created before migration 0202 have ``ssh_handle IS NULL``; mint
    one lazily here on first view rather than showing an empty SSH panel
    forever. Deliberately not done on the list endpoint — minting up to 50
    handles as a side effect of rendering a list is unwanted write
    amplification.

    The mint is guarded (M-1): it's a write on an otherwise read-only view,
    so a write failure here (a read-only replica, a full disk — this
    deployment has actually had one) must not turn the whole thread view
    into a 500 for the sake of one SSH-panel field. Caught broadly since
    ``ensure_thread_ssh_handle`` can raise asyncpg errors or its own
    exhausted-retries ``RuntimeError``; either way the response degrades to
    a null handle (the panel already renders "unavailable" for that).
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)
    result = _redact_thread_metadata(dict(thread))
    if not result.get("ssh_handle"):
        try:
            result["ssh_handle"] = await dependencies.store.ensure_thread_ssh_handle(
                thread_id
            )
        except Exception:
            logger.warning(
                "ensure_thread_ssh_handle failed for thread %s (non-fatal)",
                str(thread_id)[:8],
                exc_info=True,
            )
    mounts = await dependencies.store.list_thread_mounts(thread_id)
    result["cloud_session_url"] = dependencies.resolve_cloud_session_url(thread, mounts)
    result["mounts"] = [
        {
            "id": str(m["id"]),
            "mount_kind": m["mount_kind"],
            "target_path": m["target_path"],
            "source_kind": m["source_kind"],
            "source_ref": str(m["source_ref"]) if m.get("source_ref") else None,
            "backend_id": m.get("backend_id"),
        }
        for m in mounts
    ]
    result["project_ids"] = [
        str(m["source_ref"])
        for m in mounts
        if m.get("mount_kind") == "project" and m.get("source_ref")
    ]
    return result


@router.get("/api/persistent/threads/{thread_id}/state")
async def get_thread_session_state(
    thread_id: str,
    request: Request,
    response: Response,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """Lane-agnostic, owner-gated current state for a session Cockpit.

    This is the REST twin of the agent's direct ``session.state`` welcome
    frame.  It intentionally reads durable state for *both* execution lanes;
    no lane or pod identity crosses the wire.  Journal-derived fields are
    point-in-time values at ``event_cursor``.  A client must apply the snapshot
    before replaying the journal from ``replay_cursor`` so the latest logical
    turn is rebuilt before any not-yet-flushed agent edge advances it.
    """

    started = time.perf_counter()
    _user, _thread = await require_thread_owner(request, dependencies.store, thread_id)
    auth_done = time.perf_counter()

    # Model/temperature/narration are not all first-class thread columns yet.
    # Resolve from the exact thread row captured inside the snapshot's
    # repeatable-read transaction. A later config write then lands above the
    # returned event cursor and SSE replays it, instead of the cursor hiding a
    # scalar resolved from a different metadata revision.
    config_seconds = 0.0

    async def _resolve_snapshot_config(
        snapshot_thread: dict[str, Any], snapshot_metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        nonlocal config_seconds
        config_started = time.perf_counter()
        try:
            return await dependencies.resolve_session_config(
                snapshot_thread, snapshot_metadata
            )
        except GrantDenied:
            logger.warning(
                "Session-state config resolve denied for thread %s; using stored "
                "display fields",
                thread_id,
            )
            return None
        finally:
            config_seconds += time.perf_counter() - config_started

    snapshot = await build_session_state_snapshot(
        dependencies.store,
        thread_id,
        config_resolver=_resolve_snapshot_config,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Pending permissions include tool arguments. Never let a browser or an
    # intermediary retain one user's current control state for another read.
    response.headers["Cache-Control"] = "private, no-store"
    finished = time.perf_counter()
    logger.info(
        "session-state timing: thread=%s auth=%.3fs config=%.3fs "
        "snapshot=%.3fs total=%.3fs",
        thread_id,
        auth_done - started,
        config_seconds,
        max(0.0, finished - auth_done - config_seconds),
        finished - started,
    )
    return snapshot


@router.post(
    "/api/persistent/threads/{thread_id}/controls",
    status_code=202,
)
async def submit_thread_control(
    thread_id: str,
    body: ThreadControlRequest,
    request: Request,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """Admit an owner-authorized control for the exact serving owner.

    This endpoint serves both execution lanes and deliberately exposes neither
    one. It persists a commit-ordered request, but neither the desired scalar
    nor a journal frame: the current lease owner (or exact reciprocal pinned
    binding) applies the request and journals the result with its own allocator.
    """
    from shared.run_queue import LANE_STATELESS

    started = time.perf_counter()
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)
    thread_owner_id = thread.get("user_id")
    policy_user_id = str(thread_owner_id or user["id"])
    control_payload = body.control_payload()
    control_metadata = thread_metadata_object(thread)
    require_control_generation = bool(
        thread.get("execution_lane") == "pinned"
        and (
            protected_cloud_marker_state(control_metadata) != "off"
            or _require_pinned_status_identity()
        )
    )

    try:
        existing = await find_existing_thread_control(
            dependencies.store,
            thread_id=thread_id,
            owner_user_id=thread_owner_id,
            client_request_id=body.client_request_id,
            verb=body.method,
            payload=control_payload,
        )
    except ControlAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # A new stateless control can create a control-only queue claim just like
    # human input. Refuse unsupported workspace bindings before that durable
    # admission can wake an executor. Exact idempotent retries remain observable
    # even if the thread's lane/tier changed after their commit.
    if existing is None and thread.get("execution_lane") == LANE_STATELESS:
        _require_stateless_workspace(thread)

    if body.method == "mode.set" and existing is None:
        # Same PDP as create/attach/config.update. A stale or direct client
        # cannot persist a permission mode above the owner's current ceiling.
        # A retry of an already committed UUID bypasses mutable policy: a lost
        # 202 must stay observable even if grants changed afterward.
        try:
            await dependencies.enforce_session_create_grants(
                {"interactive": {"permission_mode": body.mode}},
                user_id=policy_user_id,
                project_ids=(
                    [str(thread["project_id"])] if thread.get("project_id") else []
                ),
            )
        except HTTPException:
            # Close the concurrent masked-commit race between the preflight
            # and PDP without weakening authorization for a genuinely new id.
            try:
                existing = await find_existing_thread_control(
                    dependencies.store,
                    thread_id=thread_id,
                    owner_user_id=thread_owner_id,
                    client_request_id=body.client_request_id,
                    verb=body.method,
                    payload=control_payload,
                )
            except ControlAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if existing is None:
                raise

    actor_id = str(user.get("id") or user.get("sub") or "rest_client")
    try:
        admitted = await admit_thread_control(
            dependencies.store,
            thread_id=thread_id,
            owner_user_id=thread_owner_id,
            client_request_id=body.client_request_id,
            verb=body.method,
            payload=control_payload,
            requested_by=actor_id,
            expected_runtime_generation=body.session_runtime_generation,
            require_pinned_runtime_generation=require_control_generation,
        )
    except ControlAdmissionNotReady as exc:
        # Registration intentionally keeps the exact pinned-owner capability
        # closed until its writer and first inbox drain are ready.  A control
        # clicked during that window is not a semantic conflict: 425 tells the
        # lane-free client to retry the same UUID after its bounded backoff.
        raise HTTPException(status_code=425, detail=str(exc)) from exc
    except ControlAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await log_security_event(
        dependencies.store,
        resource_type="thread",
        event_type="session_control_requested",
        user=user,
        resource_id=thread_id,
        detail=f"verb={body.method} request_seq={admitted.request_seq}",
        request=request,
    )
    logger.info(
        "session-control admission: thread=%s verb=%s seq=%d duplicate=%s total=%.3fs",
        thread_id,
        body.method,
        admitted.request_seq,
        admitted.duplicate,
        time.perf_counter() - started,
    )
    return {
        "accepted": True,
        "request_id": str(admitted.id),
        "client_request_id": str(admitted.client_request_id),
        "request_seq": admitted.request_seq,
        "method": admitted.verb,
        "state": admitted.state,
        "duplicate": admitted.duplicate,
        "session_runtime_generation": (
            str(admitted.runtime_generation)
            if admitted.runtime_generation is not None
            else None
        ),
    }


@router.get("/api/persistent/threads/{thread_id}/tool-groups")
async def get_thread_tool_groups(
    thread_id: str,
    request: Request,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """What toolset does this session's agent actually have? (auth: owner)

    D6: **the answer comes from the agent.** The orchestrator asks the bound
    pod what it bound and serves that; it does not recompute it. Only the agent
    sees the runtime injection layer (``persistent_session._load_tools_for_backend``
    appends the session-task trio, the product guide, the fleet/catalog/workflow
    lists, ``srw_cloud_status``, the officer pair, the datasource categories),
    ``filter_tools_by_backend``, and ``load_tools``'s per-tool fallback. A
    config-only view over-reports by dozens of names, and the divergence
    between two implementations of one fact is the original bug here.

    ``origin`` is the field that matters, and callers MUST branch on it:

    - ``agent`` — **measured, in full**. A running pod enumerated its bound
      tools and returned the structured report. ``observed_at`` and ``backend``
      are set.
    - ``agent_partial`` — **measured, names only**. The pod answered but its
      image predates ``GET /session/toolset``, so the bound names come from
      ``/status`` with no timestamp, no workspace capabilities and no
      agent-side categorisation. ``degraded_reason`` says so. The names are as
      trustworthy as ``agent``; do NOT render a workspace-tier explanation from
      this answer, and do NOT infer measured-ness from ``observed_at``, which
      is legitimately null here.
    - ``prediction`` — **forecast** from the merged config, because there is no
      agent to ask (a new session, a suspended one, an unreachable pod).
      ``prediction_reason`` says which. Structurally weaker, not merely older:
      it cannot see the three layers listed above. Rendering it as fact is D1
      violated at a new seam.

    ``categories`` answers for ALL of them (25, ``mcp`` included), each with
    ``state`` (``on``/``off``/``unavailable``), ``reason`` when not settable,
    ``settable``, ``decided_by`` (the layer that produced the answer) and
    ``tools``. Measured entries also carry ``configured``, so a caller can see
    the merge and the measurement disagree instead of having to trust one.

    ``off`` is a promise that ticking the box would work, and it is only made
    when it can be kept: on a measurement, a category whose merged config
    grants tools while the agent bound none is ``unavailable``. See
    ``compose_tool_view``.

    ``source`` is unchanged and still describes the PREDICTION's model —
    ``resolved`` / ``legacy`` / ``error``. It says nothing about ``origin``:
    a measured answer is a measured answer whichever path the config took.

    ``tool_groups`` (the closed groups, booleans) is retained for the
    cockpit and is now DERIVED from ``categories`` rather than computed beside
    it, so the endpoint cannot disagree with itself.

    ``enumerate_only`` answers the *write* half of the same question: which
    categories refuse ``tools.<c>: true`` at the write boundary, and the
    registry-derived enumeration a caller must send instead
    (``{"shell": ["cancel_command", ...]}``). Without it the only way for the
    New Session form to offer "shell on" would be a hand-maintained tool-name
    list in the cockpit — a fifth parallel list, in the change that deletes
    four. See :func:`src.core.tool_policy.enumerate_only_members`.

    Deliberately NOT a field on ``GET /api/persistent/threads/{id}``: that
    endpoint is hot and this answer costs a config resolve plus a pod probe.
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    request_override = metadata.get("config_override") or None

    base = canonical_config_name(thread.get("config_name") or "session_base")
    if _looks_like_uuid(base):
        # Sentinel / cockpit-conflated expert UUID → the real session base.
        base = "session_base"

    m = await dependencies.agent_toolset_measurement(thread)
    grants = await _session_tool_grants(dependencies, thread)

    from shared.runtime.core.subagent_roster import roster_summary

    source = "resolved"
    configured: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    # What a Delegation tick reaches: the expert's materialised roster, or an
    # empty one the pane can name as such. None only when the resolve failed.
    roster: dict[str, Any] | None = None

    if not _is_experts_db_enabled() or not await dependencies.user_experts_enabled():
        source = "legacy"
        configured, provenance = await asyncio.to_thread(
            _legacy_session_tool_policy, base, request_override
        )
        # The legacy path merges a public base only; none carries a roster.
        roster = roster_summary(None)
    else:
        try:
            expert_id = metadata.get("expert_id")
            expert_row = (
                await dependencies.store.get_expert_by_id(str(expert_id))
                if expert_id
                else None
            )
            project_id = str(thread["project_id"]) if thread.get("project_id") else None
            project_overrides = None
            if project_id and expert_id:
                link = await dependencies.store.get_project_expert_link(
                    project_id=project_id, expert_id=str(expert_id)
                )
                if link:
                    project_overrides = link.get("config_override") or None
                    if isinstance(project_overrides, str):
                        project_overrides = json.loads(project_overrides)
            # Owner-correct, same as _session_tool_grants below: an admin
            # viewing another user's thread must see THAT owner's
            # acknowledged grants, not their own (see dependencies.acknowledged_grant_strip
            # and the resume-time owner-vs-caller fix it mirrors).
            grant_strip = await dependencies.acknowledged_grant_strip(
                metadata,
                user_id=str(thread["user_id"]) if thread.get("user_id") else None,
                project_id=project_id,
            )
            # The same roster rows the attach prefetches: a DB `$ref` entry
            # the resolve cannot see is dropped, and the pane would then
            # report a roster the agent does bind as missing.
            db_refs = await dependencies.prefetch_roster_refs(
                expert_row=expert_row,
                overrides=[project_overrides, request_override],
                user_id=str(thread["user_id"]) if thread.get("user_id") else None,
                project_ids=[project_id] if project_id else [],
            )
            capture: dict[str, Any] = {}
            configured, provenance = await asyncio.to_thread(
                _merged_session_tool_policy,
                base_config_name=base,
                expert_row=expert_row,
                project_overrides=project_overrides,
                request_override=request_override,
                grant_strip=grant_strip,
                db_refs=db_refs,
                capture=capture,
            )
            roster = roster_summary(
                (capture.get("merged_fragment") or {}).get("subagents")
            )
        except Exception:
            logger.exception("Tool-group resolve failed for thread %s", thread_id)
            source = "error"
            if m.categories is None:
                # No measurement AND no resolve: there is nothing honest to
                # report. A resolve error REFUSES the attach (fail closed), so
                # there is no agent answer either.
                return {
                    "thread_id": thread_id,
                    "source": "error",
                    **_origin_fields(m),
                    "tool_groups": None,
                    "categories": None,
                    "subagents": None,
                }

    # Only a MEASURED answer carries backend capabilities: they come from the
    # agent's own report. A prediction has no provisioned workspace to inspect,
    # which is one of the three reasons it over-reports (the live gate saw it
    # over-report by 14 execution tools on a no-shell tier).
    view = compose_tool_view(
        measured=m.categories,
        configured=configured,
        provenance=provenance,
        backend_caps=m.backend,
        grants=grants,
    )
    return {
        "thread_id": thread_id,
        "source": source,
        **_origin_fields(m),
        "enumerate_only": enumerate_only_members(),
        "tool_groups": tool_groups_from_view(view),
        "categories": view,
        "subagents": roster,
    }


@router.post("/api/persistent/tool-groups/preview")
async def preview_tool_groups(
    body: ToolGroupPreviewRequest,
    request: Request,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """The New Session form's read. **Always a prediction, by construction.**

    There is no agent yet, so this endpoint can never return ``origin:
    "agent"`` — and that is the point of it being a separate route rather than
    a mode of the thread endpoint. D6's consequence is that the creation form
    forecasts while the live pane measures; making the difference structural
    (two routes, one of which cannot ever say "measured") is cheaper to keep
    honest than a flag someone forgets to read.

    ``source`` models the same three agent paths as the thread endpoint and is
    NOT hardcoded: with the experts feature or the per-user kill switch off, a
    created session takes the legacy path, where the compatibility groups are
    APPENDED unless explicitly disabled — the opposite of the resolved path for
    an unset group. Predicting "off" and labelling it ``resolved`` on such a
    deployment would be this series' own defect, rebuilt in the form that
    predicts it.

    Same ``categories`` shape as the thread endpoint, so one renderer serves
    both surfaces.
    """
    user = await require_approved_user(request, dependencies.store)
    is_worker = body.expert_type == "worker"
    default_base = "worker_base" if is_worker else "session_base"
    base = canonical_config_name(body.config_name or default_base)
    if _looks_like_uuid(base):
        base = default_base

    expert_row = None
    project_overrides = None
    legacy = (
        not _is_experts_db_enabled() or not await dependencies.user_experts_enabled()
    )
    try:
        if body.expert_id and not legacy:
            expert_row = await dependencies.store.get_expert_by_id(str(body.expert_id))
            if body.project_id:
                link = await dependencies.store.get_project_expert_link(
                    project_id=str(body.project_id), expert_id=str(body.expert_id)
                )
                if link:
                    project_overrides = link.get("config_override") or None
                    if isinstance(project_overrides, str):
                        project_overrides = json.loads(project_overrides)
    except Exception:
        logger.warning("Tool-group preview could not load the expert/project layer")

    from orchestrator.services.manifest_workspace_selection import (
        select_execution_workspace,
    )
    from shared.runtime.core.workspace_selection import bind_execution_workspace

    account = (
        await session_config_resolution.resolve_session_account_defaults(
            str(user["id"]), dependencies=dependencies.session_config_dependencies()
        )
        if not is_worker
        else {}
    )
    workspace_config, workspace_selection = await select_execution_workspace(
        dependencies.store,
        user,
        project_id=body.project_id,
        role=body.expert_type,
        workspace=body.workspace,
        supplied="workspace" in body.model_fields_set,
        config_override=body.config_override,
        account_defaults=account,
        request=request,
    )
    workspace_source = (
        "project"
        if workspace_selection and workspace_selection.get("project_revision")
        else "request"
        if "workspace" in body.model_fields_set
        or "backend" in ((body.config_override or {}).get("workspace") or {})
        else "default"
    )
    # A creation client can ask to preview its proposed recommendation. It must
    # materialize that choice in the submitted execution; admission never reads it.
    if workspace_source == "default" and body.workspace_preference is not None:
        workspace_config["backend"] = body.workspace_preference
        workspace_source = "recommendation"
    preview_override = bind_execution_workspace(
        body.config_override or {}, workspace_config
    )
    preview_workspace = {
        "backend": workspace_config["backend"],
        "source": workspace_source,
        "binding": workspace_selection["document"]
        if workspace_selection
        else (
            None
            if workspace_config["backend"] == "none"
            else {"template": {"inline": {"backend": workspace_config["backend"]}}}
        ),
    }

    # The legacy branch models ONE agent's behaviour: persistent_session's
    # re-adding of the closed group lists when no disable marker is present.
    # Worker jobs have no such step, so on the worker surface "experts off" only
    # means there is no expert layer to merge — the resolved path already answers
    # that correctly. Routing a worker preview through the session legacy policy
    # would predict appended session groups for a job that cannot hold them.
    use_legacy = legacy and not is_worker
    from shared.runtime.core.subagent_roster import roster_summary

    roster: dict[str, Any] = roster_summary(None)
    try:
        if use_legacy:
            configured, provenance = await asyncio.to_thread(
                _legacy_session_tool_policy, base, preview_override
            )
        else:
            # No grant_strip here: this is a not-yet-created session, so
            # there is no thread and no metadata.config_drift_ack to have
            # acknowledged anything against — unlike the thread endpoint
            # above, omitting it is not a gap to close, it is the correct
            # answer for a config that cannot yet have drifted.
            db_refs = await dependencies.prefetch_roster_refs(
                expert_row=expert_row,
                overrides=[project_overrides, body.config_override],
                user_id=str(user["id"]),
                project_ids=[str(body.project_id)] if body.project_id else [],
            )
            capture: dict[str, Any] = {}
            configured, provenance = await asyncio.to_thread(
                _merged_session_tool_policy,
                base_config_name=base,
                expert_row=expert_row,
                project_overrides=project_overrides,
                request_override=preview_override,
                expert_type=body.expert_type,
                db_refs=db_refs,
                capture=capture,
            )
            roster = roster_summary(
                (capture.get("merged_fragment") or {}).get("subagents")
            )
    except Exception:
        logger.exception("Tool-group preview resolve failed")
        raise HTTPException(
            status_code=422,
            detail="This configuration cannot be resolved, so its toolset "
            "cannot be predicted.",
        )

    try:
        grants = await dependencies.resolve_runner_grants(
            runner_user_id=str(user["id"]),
            project_ids=[str(body.project_id)] if body.project_id else [],
        )
    except Exception:
        logger.warning("Tool-group preview grant lookup failed")
        grants = None

    view = compose_tool_view(
        measured=None,
        configured=configured,
        provenance=provenance,
        backend_caps={
            "supports_shell": workspace_config["backend"] in ("sandbox", "vm"),
            "supports_file_tools": workspace_config["backend"] != "none",
            "supports_canvas_presentation": workspace_config["backend"] != "none",
        },
        grants=grants,
    )
    return {
        "workspace": preview_workspace,
        "source": "legacy" if use_legacy else "resolved",
        **_origin_fields(
            _unmeasured(
                "no agent exists for an unsaved job"
                if is_worker
                else "no agent exists for an unsaved session"
            )
        ),
        "enumerate_only": enumerate_only_members(),
        "tool_groups": tool_groups_from_view(view),
        "categories": view,
        "subagents": roster,
    }


@router.patch("/api/persistent/threads/{thread_id}")
async def update_thread(
    thread_id: str,
    body: ThreadUpdateRequest,
    request: Request,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, str]:
    """Rename a persistent thread (auth: owner only).

    The title was previously settable only at creation and auto-generated
    once by the LLM after the first turn; this lets the user rename a session
    inline from the Cockpit. A user-chosen title naturally blocks the
    auto-titler, which only overwrites empty / "Untitled Session" / "Local
    Session" titles (src/api/persistent_app.py).
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Title too long (max 200)")
    await dependencies.store.update_thread_title(thread_id, title)
    return {"status": "updated", "title": title}


@router.get("/api/persistent/threads/{thread_id}/citations")
async def get_thread_citations(
    thread_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """List citations created in a persistent session, for inline ``[N]`` rendering.

    The citation engine stores a session's citations with ``job_id = thread_id``
    (it maps ``CitationContext.session_id`` → ``job_id``), so the thread UUID *is*
    the ``job_id`` — there is no separate thread column. Owner-only (the by-job
    endpoint 404s for a thread since no ``jobs`` row exists). The marker the agent
    emits is the citation ``id``; the cockpit renumbers for display and resolves
    each ``[id]`` to a row returned here.
    """
    await require_thread_owner(request, dependencies.store, thread_id)
    try:
        async with dependencies.vector_db.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM citations WHERE job_id = $1::uuid",
                thread_id,
            )
            total = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """SELECT c.id, LEFT(c.claim, 300) AS claim, c.source_id,
                       s.name AS source_name, s.type::text AS source_type,
                       s.identifier AS source_identifier,
                       c.verification_status::text AS verification_status,
                       c.confidence::text AS confidence,
                       c.created_at, s.metadata
                FROM citations c
                JOIN sources s ON c.source_id = s.id
                WHERE c.job_id = $1::uuid
                ORDER BY c.id ASC
                LIMIT $2 OFFSET $3""",
                thread_id,
                limit,
                offset,
            )
            citations = []
            for r in rows:
                d = dict(r)
                # Cloud-document citations (cite_document with a snapshot-anchor)
                # can offer "view original" (/snapshot) + on-view drift (/drift);
                # web citations have neither. Surface the two flags so the cockpit
                # only renders those controls where they apply. The raw metadata
                # isn't returned (internal blob keys / anchor URLs).
                cloud = citations_operations._source_cloud_meta(d.pop("metadata", None))
                d["has_cloud_anchor"] = bool(cloud)
                d["has_snapshot"] = bool(cloud.get("snapshot_blob_key"))
                citations.append(d)
            return {
                "citations": citations,
                "total": total,
                "thread_id": thread_id,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/persistent/threads/{thread_id}/messages")
async def get_thread_messages_history(
    thread_id: str,
    request: Request,
    limit: Optional[int] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    offset: int = 0,
    *,
    dependencies: ThreadProjectionDependencies = Depends(
        get_thread_projection_dependencies
    ),
) -> dict[str, Any]:
    """Load message history for a persistent thread, ascending (chronological).

    Default (no params) returns the **entire** conversation — the cockpit caches
    the full thread client-side and windows the render itself, so the display
    must not be truncated. Cursor paging (mutually exclusive, ISO-8601):

    - ``before=<ts>``: backfill — newest messages at-or-before the cursor, up to
      ``limit``.
    - ``after=<ts>``:  catch-up — messages at-or-after the cursor, up to ``limit``.

    A bare ``limit`` with no cursor keeps the legacy oldest-first paged read
    (``offset`` honored) used by the MCP inspection tool. Returns
    ``{messages, total, has_more, thread_id}``.
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)

    def _parse_cursor(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid ISO-8601 timestamp: {value!r}"
            )

    before_dt = _parse_cursor(before)
    after_dt = _parse_cursor(after)
    if before_dt is not None and after_dt is not None:
        raise HTTPException(
            status_code=400, detail="Pass at most one of 'before' / 'after'"
        )

    capped_limit = min(limit, 500) if limit is not None else None

    if before_dt is not None or after_dt is not None:
        messages, has_more = await dependencies.store.get_thread_messages_page(
            thread_id=thread_id,
            before=before_dt,
            after=after_dt,
            limit=capped_limit,
        )
        # A cursor window carries no cheap true total; no consumer reads it here.
        total = len(messages)
    else:
        messages = await dependencies.store.get_thread_messages_history(
            thread_id=thread_id,
            limit=capped_limit,
            offset=offset,
        )
        # Legacy paged read: a full page implies there may be more.
        has_more = capped_limit is not None and len(messages) == capped_limit
        # `total` is otherwise unread (the cockpit uses only `.messages`,
        # persistent-chat.service.ts:748; the MCP tool doesn't read it). Skip the
        # per-open COUNT(*): a full load (no limit) returns the whole thread so
        # len(messages) IS the total; charge the COUNT only for the explicit
        # limit/offset paged read where a paginating client may want it.
        if capped_limit is None:
            total = len(messages)
        else:
            total = await dependencies.store.get_thread_message_count(thread_id)

    _stamp_tool_categories(messages)

    return {
        "messages": messages,
        "total": total,
        "has_more": has_more,
        "thread_id": thread_id,
    }
