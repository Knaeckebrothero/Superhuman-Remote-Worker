"""Authoritative Officer Post job admission.

Every officer-created job, manual or automatic, linearizes on the durable
``project_officers`` row.  Preparation may resolve grants, datasources and
other expensive inputs without holding a database transaction; the final
boundary always does this, on one connection and in this lock order::

    project_officers post -> current threads row -> run queue/jobs -> wake/routes

Admission uses the post, thread and jobs stages. Lifecycle writers use the
same ordered prefix in :mod:`orchestrator.database.postgres`. The post key is
stable across decommission/recommission, unlike the former advisory lock keyed
by the current thread id.

The preparation fingerprint is deliberately admission-specific.  A roster,
durable kit, hold, enabled flag, auto-pull flag, or lineage change makes a
prepared request retry instead of inserting with a stale slot snapshot.
Runtime-only ``last_respawn_at`` does not affect admission and is excluded.

``admit_and_create_job_in_transaction`` is connection-aware by design. BP-05's
durable claim INSERT, capacity decision and exact preallocated job INSERT share
that caller-owned transaction. The historical partial jobs index remains a
second non-terminal backstop.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from services.officer_slots import SlotAdmissionError
from services.officer_slots import admit as admit_slot
from src.shared.workspace_contract import (
    WorkspaceContractError,
    configured_workspace_backend,
)

logger = logging.getLogger(__name__)

# Terminal means "this job no longer owns its work" — ticket claim and slot
# both released. Mirrors job_liveness.TERMINAL_STATUSES; kept local so changing
# liveness presentation cannot silently change capacity.
TERMINAL_JOB_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")
_TERMINAL_STATUSES_SQL = "(" + ", ".join(f"'{s}'" for s in TERMINAL_JOB_STATUSES) + ")"

# A paused job keeps its ticket claim but does NOT occupy a kit slot (owner
# ruling 2026-08-18): nothing is running, so the capacity is real — two paused
# zombies must never starve a pool the way they did on that date. A later
# resume may transiently push a pool over its cap; that is accepted, with no
# compensating machinery. Ticket dedup deliberately still counts paused — one
# live job per ticket, whatever its status — so this set widens only the
# capacity predicates, never `_validate_ticket_claim`.
SLOT_VACATING_STATUSES: tuple[str, ...] = ("paused",)
_SLOT_RELEASED_STATUSES_SQL = (
    "("
    + ", ".join(f"'{s}'" for s in TERMINAL_JOB_STATUSES + SLOT_VACATING_STATUSES)
    + ")"
)

OFFICER_HELD_MESSAGE = (
    "conference in progress — this officer is held; scheduling resumes with "
    "the session brief after the conference ends."
)


class OfficerAdmissionConflict(RuntimeError):
    """A normal, retryable/refused Officer Post admission outcome."""

    def __init__(self, code: str, detail: str, **fields: Any):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.fields = dict(fields)


@dataclass(frozen=True, slots=True)
class OfficerAdmissionPreparation:
    """Admission-relevant snapshot used for expensive pre-transaction work."""

    project_id: str
    thread_id: str
    requested_slot: str | None
    slot_name: str | None
    slot_patch: dict[str, Any]
    category: str | None
    config_fingerprint: str
    incarnation: int
    owner_user_id: str | None
    require_auto_pull: bool
    requested_model: str | None = None
    requested_backend: str | None = None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _truthy(value: Any) -> bool:
    return value in (True, "true", "True", 1)


def officer_is_held(officer_meta: dict[str, Any]) -> bool:
    """True while the current incarnation carries a runtime hold."""

    return bool(officer_meta.get("hold"))


def _officer_meta(thread_metadata: Any) -> dict[str, Any]:
    metadata = _as_dict(thread_metadata)
    return _as_dict(_as_dict(metadata.get("config_override")).get("officer"))


def _lineage(post: Mapping[str, Any], current_thread_id: str) -> list[str]:
    ids = [current_thread_id]
    for entry in _as_list(post.get("incarnations")):
        if not isinstance(entry, dict) or not entry.get("thread_id"):
            continue
        try:
            tid = str(UUID(str(entry["thread_id"])))
        except (TypeError, ValueError):
            continue
        if tid not in ids:
            ids.append(tid)
    return ids


def _fingerprint(
    post: Mapping[str, Any], officer_meta: dict[str, Any], current_thread_id: str
) -> str:
    runtime = dict(officer_meta)
    # A watchdog timestamp is not roster/config authority and must not force a
    # harmless retry.  Hold stays: it is an admission fence.
    runtime.pop("last_respawn_at", None)
    material = {
        "thread_id": current_thread_id,
        "post_config_override": _as_dict(post.get("config_override")),
        "runtime_officer": runtime,
        "lineage": _lineage(post, current_thread_id),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _slot_category(officer_meta: dict[str, Any], slot_name: str | None) -> str | None:
    if not slot_name:
        return None
    slots = officer_meta.get("slots")
    if not isinstance(slots, dict):
        return None
    spec = slots.get(slot_name)
    if not isinstance(spec, dict) or not spec.get("category"):
        return None
    return str(spec["category"])


def _deep_merge(base: Any, override: Any) -> dict[str, Any]:
    merged = dict(base) if isinstance(base, dict) else {}
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_prepared_slot_config(
    base_config_override: dict[str, Any] | None,
    preparation: OfficerAdmissionPreparation,
) -> dict[str, Any]:
    """Apply the prepared authoritative slot patch for preflight checks."""

    return _deep_merge(base_config_override, preparation.slot_patch)


def _conflict(code: str, detail: str, **fields: Any) -> OfficerAdmissionConflict:
    return OfficerAdmissionConflict(code, detail, **fields)


def _requested_model(config_override: Any) -> str | None:
    config = _as_dict(config_override)
    llm = _as_dict(config.get("llm"))
    model = llm.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _validate_slot_pins(
    *,
    slot_name: str | None,
    slot_patch: Mapping[str, Any],
    requested_model: str | None,
    requested_backend: str | None,
) -> None:
    """Refuse a caller choice that a typed slot would otherwise overwrite."""

    if slot_name is None:
        return
    pinned_backend = configured_workspace_backend(slot_patch)
    if (
        requested_backend is not None
        and pinned_backend is not None
        and requested_backend != pinned_backend
    ):
        raise _conflict(
            "slot_backend_conflict",
            f"Slot '{slot_name}' pins workspace backend '{pinned_backend}', "
            f"which conflicts with requested backend '{requested_backend}'. "
            "Select a compatible Officer slot instead of retrying the same override.",
            slot=slot_name,
            pinned_backend=pinned_backend,
            requested_backend=requested_backend,
        )
    pinned_model = _as_dict(slot_patch.get("llm")).get("model")
    if (
        requested_model is not None
        and isinstance(pinned_model, str)
        and requested_model != pinned_model
    ):
        raise _conflict(
            "slot_model_conflict",
            f"Slot '{slot_name}' pins model '{pinned_model}', which conflicts "
            f"with requested model '{requested_model}'. Select a compatible "
            "Officer slot instead of retrying the same override.",
            slot=slot_name,
            pinned_model=pinned_model,
            requested_model=requested_model,
        )


def _validate_live_incarnation(
    post: Mapping[str, Any],
    thread: Mapping[str, Any] | None,
    *,
    expected_thread_id: str,
    require_auto_pull: bool,
) -> dict[str, Any]:
    linked = str(post.get("thread_id") or "")
    if not linked:
        raise _conflict(
            "post_vacant", "The Officer Post is vacant; retry after commission."
        )
    if linked != expected_thread_id:
        raise _conflict(
            "stale_incarnation",
            "Officer Post incarnation changed while work was being prepared; retry.",
        )
    if thread is None or str(thread.get("id") or "") != expected_thread_id:
        raise _conflict(
            "missing_incarnation",
            "The Officer Post's current thread is missing; retry after lifecycle repair.",
        )
    if str(thread.get("project_id") or "") != str(post.get("project_id") or ""):
        raise _conflict(
            "project_mismatch", "The Officer Post/thread project binding is invalid."
        )
    if str(thread.get("status") or "") == "ended":
        raise _conflict("ended_incarnation", "The Officer Post incarnation has ended.")
    officer_meta = _officer_meta(thread.get("metadata"))
    if not _truthy(officer_meta.get("enabled")):
        raise _conflict("officer_disabled", "The Officer Post incarnation is disabled.")
    if officer_is_held(officer_meta):
        raise _conflict("officer_held", OFFICER_HELD_MESSAGE)
    if require_auto_pull and not _truthy(officer_meta.get("auto_pull")):
        raise _conflict("auto_pull_disabled", "Officer Post auto-pull is disabled.")
    return officer_meta


def _preparation_from_rows(
    post: Mapping[str, Any],
    thread: Mapping[str, Any] | None,
    *,
    expected_thread_id: str,
    requested_slot: str | None,
    require_auto_pull: bool,
    expected_category: str | None,
    requested_model: str | None = None,
    requested_backend: str | None = None,
) -> OfficerAdmissionPreparation:
    officer_meta = _validate_live_incarnation(
        post,
        thread,
        expected_thread_id=expected_thread_id,
        require_auto_pull=require_auto_pull,
    )
    slot_name, slot_patch = admit_slot(officer_meta, requested_slot, {})
    _validate_slot_pins(
        slot_name=slot_name,
        slot_patch=slot_patch,
        requested_model=requested_model,
        requested_backend=requested_backend,
    )
    category = _slot_category(officer_meta, slot_name)
    if expected_category is not None and category != expected_category:
        raise _conflict(
            "slot_category_changed",
            "Officer Post slot category changed while the ticket was prepared; retry.",
        )
    incarnations = _as_list(post.get("incarnations"))
    owner = thread.get("user_id") if thread is not None else None
    return OfficerAdmissionPreparation(
        project_id=str(post["project_id"]),
        thread_id=expected_thread_id,
        requested_slot=requested_slot,
        slot_name=slot_name,
        slot_patch=dict(slot_patch),
        category=category,
        config_fingerprint=_fingerprint(post, officer_meta, expected_thread_id),
        incarnation=len(incarnations),
        owner_user_id=str(owner) if owner else None,
        require_auto_pull=require_auto_pull,
        requested_model=requested_model,
        requested_backend=requested_backend,
    )


async def prepare_officer_admission(
    db: Any,
    *,
    project_id: str,
    thread_id: str,
    requested_slot: str | None,
    require_auto_pull: bool = False,
    expected_category: str | None = None,
    requested_config_override: Mapping[str, Any] | None = None,
) -> OfficerAdmissionPreparation:
    """Read a coherent preflight snapshot without holding a long transaction."""

    try:
        project_uuid = UUID(str(project_id))
        expected_thread_id = str(UUID(str(thread_id)))
    except (TypeError, ValueError) as exc:
        raise _conflict(
            "invalid_identity", "Officer Post identity is invalid."
        ) from exc

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT po.project_id, po.thread_id, po.config_override,
                   po.incarnations, po.updated_at AS post_updated_at,
                   t.id AS current_thread_id, t.project_id AS thread_project_id,
                   t.status AS thread_status, t.metadata AS thread_metadata,
                   t.user_id AS thread_user_id, t.created_at AS thread_created_at
              FROM project_officers po
              LEFT JOIN threads t ON t.id = po.thread_id
             WHERE po.project_id = $1
            """,
            project_uuid,
        )
    if row is None:
        raise _conflict("post_missing", "Officer Post does not exist.")
    post = {
        "project_id": row["project_id"],
        "thread_id": row["thread_id"],
        "config_override": row["config_override"],
        "incarnations": row["incarnations"],
        "updated_at": row["post_updated_at"],
    }
    thread = None
    if row["current_thread_id"] is not None:
        thread = {
            "id": row["current_thread_id"],
            "project_id": row["thread_project_id"],
            "status": row["thread_status"],
            "metadata": row["thread_metadata"],
            "user_id": row["thread_user_id"],
            "created_at": row["thread_created_at"],
        }
    try:
        requested_backend = configured_workspace_backend(requested_config_override)
    except WorkspaceContractError as exc:
        raise _conflict(exc.code, exc.detail) from exc
    return _preparation_from_rows(
        post,
        thread,
        expected_thread_id=expected_thread_id,
        requested_slot=requested_slot,
        require_auto_pull=require_auto_pull,
        expected_category=expected_category,
        requested_model=_requested_model(requested_config_override),
        requested_backend=requested_backend,
    )


async def count_in_flight_by_slot(
    conn: Any, capacity_lineage: Sequence[Any]
) -> dict[str | None, int]:
    """Slot-occupying jobs by slot over the complete post lineage.

    Non-terminal minus ``SLOT_VACATING_STATUSES``: a paused job is not in
    flight and holds no kit slot. This is the one count admission enforces
    and the officer card's kit view and the sitrep capacity line display —
    all three read it here so they can never disagree.
    """

    if not capacity_lineage:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT context->>'officer_slot' AS slot, COUNT(*) AS n
          FROM jobs
         WHERE created_by_thread_id = ANY($1::uuid[])
           AND status NOT IN {_SLOT_RELEASED_STATUSES_SQL}
         GROUP BY 1
        """,
        list(capacity_lineage),
    )
    return {row["slot"]: int(row["n"]) for row in rows}


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _lock_current_post(
    conn: Any, preparation: OfficerAdmissionPreparation
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Lock post then current thread — the repository-wide Officer lock order."""

    post_row = await conn.fetchrow(
        """
        SELECT project_id, thread_id, config_override, incarnations, updated_at
          FROM project_officers
         WHERE project_id = $1
         FOR UPDATE
        """,
        UUID(preparation.project_id),
    )
    if post_row is None:
        raise _conflict("post_missing", "Officer Post does not exist.")
    post = dict(post_row)
    linked = post.get("thread_id")
    thread_row = None
    if linked is not None:
        thread_row = await conn.fetchrow(
            """
            SELECT id, project_id, status, metadata, user_id, created_at
              FROM threads
             WHERE id = $1
             FOR UPDATE
            """,
            linked,
        )
    return post, dict(thread_row) if thread_row is not None else None


async def _validate_ticket_claim(
    conn: Any,
    *,
    project_id: str,
    note_id: str | None,
    ready_at: datetime | str | None,
) -> None:
    if not note_id:
        return
    generation = _aware(ready_at)
    if generation is None:
        raise _conflict(
            "ticket_not_ready",
            f"Backlog ticket '{note_id}' has no trusted ready generation.",
        )
    row = await conn.fetchrow(
        f"""
        SELECT MAX(claim.ready_generation_at) AS newest_generation,
               MAX(claim.claimed_at) FILTER (
                   WHERE claim.source = 'legacy_unversioned'
               ) AS legacy_rearm_after,
               COALESCE(bool_or(
                   (live.id IS NOT NULL AND
                    live.status NOT IN {_TERMINAL_STATUSES_SQL})
                   OR
                   (live.id IS NULL AND (
                       claim.job_deleted_at IS NULL
                       OR claim.job_status_at_delete IS NULL
                       OR claim.job_status_at_delete
                          NOT IN {_TERMINAL_STATUSES_SQL}
                   ))
               ), FALSE) AS has_non_terminal
          FROM officer_ticket_claims claim
          LEFT JOIN jobs live ON live.id = claim.job_id
         WHERE claim.project_id = $1
           AND claim.ticket_note_id = $2
        """,
        UUID(project_id),
        str(note_id),
    )
    newest = _aware(row["newest_generation"]) if row else None
    legacy_rearm_after = _aware(row["legacy_rearm_after"]) if row else None
    if row and bool(row["has_non_terminal"]):
        raise _conflict(
            "ticket_claimed",
            f"Backlog ticket '{note_id}' already has a non-terminal job.",
        )
    if legacy_rearm_after is not None and legacy_rearm_after >= generation:
        raise _conflict(
            "ticket_claimed",
            f"Backlog ticket '{note_id}' must be explicitly re-readied after "
            "the durable-claim cutover.",
        )
    if newest is not None and newest >= generation:
        raise _conflict(
            "ticket_claimed",
            f"Backlog ticket '{note_id}' is already claimed for this ready generation.",
        )


async def _validate_ticket_delivery_requirement(
    conn: Any,
    *,
    project_id: str,
    note_id: str | None,
    ready_at: datetime | str | None,
    delivery_contract: Mapping[str, Any] | None,
) -> None:
    """Refuse a same-generation downgrade recorded by a prior attempt.

    The Post row is already locked by the caller.  This check therefore
    linearizes with both requirement recording and the BP-05 claim/job insert;
    a concurrent rejected ``repos/`` attempt and a rewritten ``kb:`` attempt
    cannot both pass.
    """

    if not note_id:
        return
    generation = _aware(ready_at)
    if generation is None:
        return
    required = await conn.fetchval(
        """
        SELECT required_pr_repositories
          FROM officer_ticket_deliverable_requirements
         WHERE project_id = $1
           AND ticket_note_id = $2
           AND ready_generation_at = $3
        """,
        UUID(project_id),
        str(note_id),
        generation,
    )
    if required is None:
        return
    supplied = {
        str(value).strip().casefold()
        for value in list((delivery_contract or {}).get("pr_repositories") or [])
        if str(value).strip()
    }
    expected = {str(value).strip().casefold() for value in list(required)}
    if supplied != expected:
        raise _conflict(
            "deliverable_contract_downgrade",
            "This ticket generation previously requested publication in an "
            "attached repository. It must use the exact PR deliverable "
            "contract; no claim or job was created.",
            required_pr_deliverables=[
                f"pr:{repository}" for repository in sorted(expected)
            ],
        )


async def admit_and_create_job_in_transaction(
    db: Any,
    conn: Any,
    *,
    preparation: OfficerAdmissionPreparation,
    job_kwargs: dict[str, Any],
    ticket_note_id: str | None = None,
    ticket_ready_at: datetime | str | None = None,
    ticket_claim_source: str = "manual",
    strict_provisioning: bool = False,
) -> dict[str, Any]:
    """Revalidate, count, claim-check and INSERT on ``conn``'s transaction."""

    post, thread = await _lock_current_post(conn, preparation)
    current = _preparation_from_rows(
        post,
        thread,
        expected_thread_id=preparation.thread_id,
        requested_slot=preparation.requested_slot,
        require_auto_pull=preparation.require_auto_pull,
        expected_category=preparation.category,
        requested_model=preparation.requested_model,
        requested_backend=preparation.requested_backend,
    )
    if current.config_fingerprint != preparation.config_fingerprint:
        raise _conflict(
            "config_changed",
            "Officer Post configuration or lineage changed while work was prepared; retry.",
        )
    if current.owner_user_id != preparation.owner_user_id:
        raise _conflict(
            "owner_changed",
            "Officer Post ownership changed while work was prepared; retry.",
        )
    if (
        current.slot_name != preparation.slot_name
        or current.slot_patch != preparation.slot_patch
    ):
        raise _conflict(
            "slot_changed",
            "Officer Post slot selection changed while work was prepared; retry.",
        )

    lineage = _lineage(post, preparation.thread_id)
    in_flight = await count_in_flight_by_slot(conn, lineage)
    officer_meta = _officer_meta(thread.get("metadata"))
    slot_name, slot_patch = admit_slot(
        officer_meta, preparation.requested_slot, in_flight
    )
    _validate_slot_pins(
        slot_name=slot_name,
        slot_patch=slot_patch,
        requested_model=preparation.requested_model,
        requested_backend=preparation.requested_backend,
    )
    if slot_name != preparation.slot_name or slot_patch != preparation.slot_patch:
        raise _conflict(
            "slot_changed",
            "Officer Post slot selection changed while work was prepared; retry.",
        )

    await _validate_ticket_claim(
        conn,
        project_id=preparation.project_id,
        note_id=ticket_note_id,
        ready_at=ticket_ready_at,
    )
    await _validate_ticket_delivery_requirement(
        conn,
        project_id=preparation.project_id,
        note_id=ticket_note_id,
        ready_at=ticket_ready_at,
        delivery_contract=job_kwargs.get("delivery_contract"),
    )

    final_kwargs = dict(job_kwargs)
    context = dict(final_kwargs.get("context") or {})
    # Raw context is never claim authority, even when the caller legitimately
    # originates from the Officer thread. Replace the complete claim namespace
    # before stamping the final post-locked decision.
    for key in (
        "ticket_note_id",
        "officer_admission",
        "ticket_ready_at",
        "ready_generation_at",
        "ticket_claim_source",
        "claim_source",
        "officer_thread_id",
        "officer_incarnation",
        "provisioning_preflight",
    ):
        context.pop(key, None)
    if slot_name is not None:
        context["officer_slot"] = slot_name
    else:
        context.pop("officer_slot", None)
    if current.category is not None:
        context["work_category"] = current.category
    if ticket_note_id:
        context["ticket_note_id"] = str(ticket_note_id)
    context["officer_admission"] = {
        "project_id": preparation.project_id,
        "thread_id": preparation.thread_id,
        "incarnation": current.incarnation,
        "slot": slot_name,
        "category": current.category,
        "config_fingerprint": current.config_fingerprint,
        "lineage_size": len(lineage),
    }
    ready_generation = _aware(ticket_ready_at)
    if ready_generation is not None:
        context["officer_admission"]["ticket_ready_at"] = ready_generation.isoformat()
        context["officer_admission"]["ticket_claim_source"] = str(ticket_claim_source)
    if strict_provisioning:
        from services.officer_preflight import (
            initial_preflight_context,
            initial_preflight_freeze,
        )

        context["provisioning_preflight"] = initial_preflight_context(
            category=current.category
        )
        final_kwargs["status"] = "paused"
        final_kwargs["freeze_data"] = initial_preflight_freeze()
    final_kwargs["context"] = context
    final_kwargs["config_override"] = _deep_merge(
        final_kwargs.get("config_override"), slot_patch
    )
    final_kwargs["project_id"] = preparation.project_id
    final_kwargs["created_by_thread_id"] = preparation.thread_id
    if ticket_note_id:
        # The preallocated identity lets the immutable claim land first while
        # still referring to the exact job INSERT that follows. Both disappear
        # on any failure because the caller owns one transaction.
        admitted_job_id = uuid4()
        await db.insert_officer_ticket_claim(
            conn=conn,
            project_id=preparation.project_id,
            ticket_note_id=str(ticket_note_id),
            ready_generation_at=ready_generation,
            source=str(ticket_claim_source),
            officer_thread_id=preparation.thread_id,
            officer_incarnation=current.incarnation,
            officer_slot=slot_name,
            work_category=current.category,
            admission_config_fingerprint=current.config_fingerprint,
            admission_lineage_size=len(lineage),
            job_id=admitted_job_id,
        )
        final_kwargs["job_id"] = admitted_job_id
    final_kwargs["authoritative_officer_admission"] = True
    # The slot's configured tier is the assignment, not a caller request. This
    # stays ``None`` for auto-pull/defaulted jobs and preserves the explicit
    # request only for manual Officer calls that were checked for collision.
    final_kwargs["requested_workspace_backend"] = preparation.requested_backend
    if configured_workspace_backend(slot_patch) is not None:
        final_kwargs["workspace_assignment_source"] = f"officer_slot:{slot_name}"
    final_kwargs["conn"] = conn
    # Stamped at the last common funnel rather than in each caller: both the
    # backlog tick and the manual "pull this ticket" click land here, and both
    # are officer dispatches. If they ever need to differ, the stamp moves out
    # to each caller's job_kwargs — it must not become an inference here.
    final_kwargs["origin"] = "officer"
    return await db.create_job(**final_kwargs)


async def record_rejected_ticket_delivery_requirement(
    db: Any,
    *,
    preparation: OfficerAdmissionPreparation,
    ticket_note_id: str,
    ticket_ready_at: datetime | str,
    required_pr_repositories: Sequence[str],
) -> dict[str, Any]:
    """Durably record a rejected external-publication contract.

    This deliberately shares the authoritative Post -> thread lock order with
    admission.  It inserts neither a ticket claim nor a job.
    """

    generation = _aware(ticket_ready_at)
    repositories = sorted(
        {
            str(value).strip().casefold()
            for value in required_pr_repositories
            if str(value).strip()
        }
    )
    if generation is None or not repositories:
        raise _conflict(
            "invalid_deliverable_requirement",
            "The rejected publication contract could not be recorded safely.",
        )

    async with db.acquire() as conn:
        async with conn.transaction():
            post, thread = await _lock_current_post(conn, preparation)
            current = _preparation_from_rows(
                post,
                thread,
                expected_thread_id=preparation.thread_id,
                requested_slot=preparation.requested_slot,
                require_auto_pull=preparation.require_auto_pull,
                expected_category=preparation.category,
                requested_model=preparation.requested_model,
                requested_backend=preparation.requested_backend,
            )
            if current.config_fingerprint != preparation.config_fingerprint:
                raise _conflict(
                    "config_changed",
                    "Officer Post configuration or lineage changed while work "
                    "was prepared; retry.",
                )
            await _validate_ticket_claim(
                conn,
                project_id=preparation.project_id,
                note_id=ticket_note_id,
                ready_at=generation,
            )
            existing = await conn.fetchrow(
                """
                SELECT required_pr_repositories
                  FROM officer_ticket_deliverable_requirements
                 WHERE project_id = $1
                   AND ticket_note_id = $2
                   AND ready_generation_at = $3
                 FOR UPDATE
                """,
                UUID(preparation.project_id),
                str(ticket_note_id),
                generation,
            )
            if existing is not None:
                recorded = sorted(
                    str(value).casefold()
                    for value in existing["required_pr_repositories"]
                )
                if recorded != repositories:
                    raise _conflict(
                        "deliverable_requirement_conflict",
                        "This ticket generation already has a different "
                        "server-recorded publication requirement.",
                    )
                return {
                    "recorded": False,
                    "required_pr_repositories": repositories,
                }
            await conn.execute(
                """
                INSERT INTO officer_ticket_deliverable_requirements (
                    project_id, ticket_note_id, ready_generation_at,
                    required_pr_repositories, officer_thread_id,
                    officer_incarnation
                ) VALUES ($1, $2, $3, $4::text[], $5, $6)
                """,
                UUID(preparation.project_id),
                str(ticket_note_id),
                generation,
                repositories,
                UUID(preparation.thread_id),
                current.incarnation,
            )
            return {
                "recorded": True,
                "required_pr_repositories": repositories,
            }


async def admit_and_create_job(
    db: Any,
    *,
    preparation: OfficerAdmissionPreparation,
    job_kwargs: dict[str, Any],
    ticket_note_id: str | None = None,
    ticket_ready_at: datetime | str | None = None,
    ticket_claim_source: str = "manual",
    strict_provisioning: bool = False,
) -> dict[str, Any]:
    """Own the authoritative admission transaction and normalize contention."""

    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                return await admit_and_create_job_in_transaction(
                    db,
                    conn,
                    preparation=preparation,
                    job_kwargs=job_kwargs,
                    ticket_note_id=ticket_note_id,
                    ticket_ready_at=ticket_ready_at,
                    ticket_claim_source=ticket_claim_source,
                    strict_provisioning=strict_provisioning,
                )
    except OfficerAdmissionConflict:
        raise
    except Exception as exc:
        # The partial unique index remains a fail-closed backstop. Stable post
        # locking makes this exceptional, but another legacy/direct writer can
        # still race it; report ordinary ticket contention, never a 500.
        if getattr(exc, "constraint_name", None) in {
            "uq_jobs_active_ticket_claim",
            "uq_officer_ticket_claim_generation",
        }:
            raise _conflict(
                "ticket_claimed",
                f"Backlog ticket '{ticket_note_id}' was claimed concurrently.",
            ) from exc
        raise


__all__ = [
    "OFFICER_HELD_MESSAGE",
    "OfficerAdmissionConflict",
    "OfficerAdmissionPreparation",
    "SLOT_VACATING_STATUSES",
    "SlotAdmissionError",
    "TERMINAL_JOB_STATUSES",
    "admit_and_create_job",
    "admit_and_create_job_in_transaction",
    "apply_prepared_slot_config",
    "count_in_flight_by_slot",
    "officer_is_held",
    "prepare_officer_admission",
    "record_rejected_ticket_delivery_requirement",
]
