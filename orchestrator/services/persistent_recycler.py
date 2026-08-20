"""Durable lifecycle owner for dedicated persistent-thread pods.

The Kubernetes pod is disposable; the thread (and, for Officers, the Post) is
the authority.  A recycle generation is stored in ``threads.metadata.agent_pod``
and every transition re-locks Post -> thread -> agent -> grant before changing
authority.  Kubernetes I/O happens outside those transactions and is fenced by
immutable pod UIDs plus the generation label on the replacement.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .persistent_provisioner import (
    PersistentPodCreateStatus,
    PersistentProvisioner,
)
from .runtime_actor import lock_current_officer_runtime_grant

logger = logging.getLogger(__name__)

_RECYCLE_KEY = "recycle"
_HOLD_GENERATION_KEY = "_persistent_recycle_generation"
_ACTIVE_PHASES = {
    "awaiting_old_pod_exit",
    "fencing_old_authority",
    "provisioning",
    "provisioning_claimed",
    "awaiting_replacement",
    "failed_retryable",
    "blocked",
}
_LIVE_THREAD_STATUSES = {"created", "active", "awaiting_user", "suspended"}
_NOTIFICATION_CLAIM_SECONDS = 300


@dataclass(frozen=True, slots=True)
class PersistentPodObservation:
    thread_id: str
    pod_name: str
    pod_uid: str
    build_sha: str | None
    phase: str
    ready: bool
    terminating: bool
    labels: dict[str, str]

    @classmethod
    def from_status(
        cls, thread_id: str, status: dict[str, Any] | None
    ) -> PersistentPodObservation | None:
        if not status or not status.get("pod_uid"):
            return None
        return cls(
            thread_id=str(thread_id),
            pod_name=str(status.get("pod_name") or f"persistent-{thread_id[:12]}"),
            pod_uid=str(status["pod_uid"]),
            build_sha=(str(status["build_sha"]) if status.get("build_sha") else None),
            phase=str(status.get("phase") or "Unknown"),
            ready=bool(status.get("ready")),
            terminating=bool(status.get("terminating")),
            labels={str(k): str(v) for k, v in (status.get("labels") or {}).items()},
        )

    @property
    def terminal(self) -> bool:
        return self.phase in {"Succeeded", "Failed"}


@dataclass(frozen=True, slots=True)
class PersistentRecycleResult:
    thread_id: str
    state: str
    phase: str
    generation: str | None = None
    failure_class: str | None = None

    def safe_view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "phase": self.phase,
            "failure_class": self.failure_class,
        }


@dataclass(frozen=True, slots=True)
class ParkedBoundaryAcknowledgement:
    """Tri-state result that prevents an active recycle falling into legacy suspend."""

    active_generation: bool
    acknowledged: bool
    reason: str


FailureNotifier = Callable[[str, str, str], Awaitable[bool] | bool]
CompletionCallback = Callable[[str, str], Awaitable[None] | None]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class PersistentThreadRecycler:
    """One idempotent state machine for drift, missing pods, and manual recycle."""

    def __init__(
        self,
        *,
        db: Any,
        provisioner: PersistentProvisioner,
        failure_notifier: FailureNotifier | None = None,
        on_complete: CompletionCallback | None = None,
    ) -> None:
        self._db = db
        self._provisioner = provisioner
        self._failure_notifier = failure_notifier
        self._on_complete = on_complete

    async def observe(self, thread_id: str) -> PersistentPodObservation | None:
        return PersistentPodObservation.from_status(
            thread_id, await self._provisioner.get_pod_status(thread_id)
        )

    async def request_and_reconcile(
        self,
        *,
        thread_id: str,
        reason: str,
        expected_build_sha: str | None,
        observation: PersistentPodObservation | None = None,
        expected_project_id: str | None = None,
    ) -> PersistentRecycleResult:
        """Create/reuse one generation and advance it through safe boundaries."""

        if observation is None:
            observation = await self.observe(thread_id)
        requested = await self._request(
            thread_id=thread_id,
            reason=reason,
            expected_build_sha=expected_build_sha,
            observation=observation,
            expected_project_id=expected_project_id,
        )
        current = await self._read_recycle(thread_id)
        if current.get("generation"):
            await self._notify_failure(
                str(current.get("project_id") or ""), thread_id, current
            )
        if requested.state in {"blocked", "cancelled"}:
            return requested

        # Several zero-I/O boundaries can be crossed in one tick. Never busy
        # wait for pod deletion/readiness; those return to the reconciler.
        result = requested
        for _ in range(4):
            next_result = await self._advance(
                thread_id=thread_id,
                expected_build_sha=expected_build_sha,
                observation=observation,
            )
            result = next_result
            if next_result.phase in {
                "awaiting_old_pod_exit",
                "awaiting_replacement",
                "provisioning_claimed",
                "failed_retryable",
                "complete",
                "blocked",
                "cancelled",
            }:
                break
        return result

    async def _request(
        self,
        *,
        thread_id: str,
        reason: str,
        expected_build_sha: str | None,
        observation: PersistentPodObservation | None,
        expected_project_id: str | None,
    ) -> PersistentRecycleResult:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(
                    conn,
                    thread_uuid=thread_uuid,
                    expected_project_id=expected_project_id,
                )
                if locked is None:
                    return PersistentRecycleResult(
                        str(thread_id), "cancelled", "cancelled"
                    )
                thread, post, agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                current = _json_object(agent_pod.get(_RECYCLE_KEY))
                if current.get("phase") in _ACTIVE_PHASES:
                    if (
                        current.get("phase") == "awaiting_old_pod_exit"
                        and observation is not None
                        and observation.pod_uid == current.get("old_pod_uid")
                        and agent is not None
                        and str(agent.get("thread_id") or "") == str(thread_id)
                        and str(agent.get("hostname") or "") == observation.pod_name
                        and str(agent.get("pod_uid") or "") == observation.pod_uid
                    ):
                        await conn.execute(
                            """
                            UPDATE agents
                               SET intents = COALESCE(intents, '{}'::jsonb)
                                   || $2::jsonb
                             WHERE id = $1 AND thread_id = $3
                            """,
                            agent["id"],
                            json.dumps(
                                {
                                    "should_drain": True,
                                    "drain_reason": (
                                        "persistent_recycle:"
                                        f"{current.get('generation')}"
                                    ),
                                }
                            ),
                            thread_uuid,
                        )
                    return self._result(thread_id, current)

                initial_failure = None
                expected_pod_name = f"persistent-{str(thread_id)[:12]}"
                if (
                    observation is None
                    and agent is not None
                    and str(agent.get("hostname") or "") != expected_pod_name
                ):
                    # This interim owner is intentionally limited to the
                    # legacy dedicated-pod substrate. A generic session pod
                    # is real Kubernetes authority even though the persistent
                    # provisioner cannot observe it. Treating that as a
                    # missing pod would revoke its agent and create a second
                    # runtime for one thread.
                    initial_failure = "unsupported_pod_authority"
                if observation is not None and not self._observation_names_thread(
                    observation, str(thread_id)
                ):
                    initial_failure = "pod_authority_mismatch"
                if (
                    initial_failure is None
                    and observation is not None
                    and agent is not None
                    and not (
                        str(agent.get("thread_id") or "") == str(thread_id)
                        and str(agent.get("hostname") or "") == observation.pod_name
                        and str(agent.get("pod_uid") or "") == observation.pod_uid
                    )
                ):
                    initial_failure = "reciprocal_binding_mismatch"

                generation = str(uuid.uuid4())
                target_image_ref = str(
                    getattr(self._provisioner, "image_ref", "") or ""
                )
                target_build_sha = getattr(
                    self._provisioner, "expected_build_sha", expected_build_sha
                )
                officer = self._officer_config(metadata)
                hold_owned = False
                preexisting_hold = False
                if post is not None:
                    hold = officer.get("hold")
                    if isinstance(hold, dict) and hold.get("thread_id"):
                        # An active conference owns the Officer. Drift remains
                        # visible, but no lifecycle intent is written mid-meeting.
                        return PersistentRecycleResult(
                            str(thread_id),
                            "blocked",
                            "blocked",
                            failure_class="conference_hold",
                        )
                    if hold:
                        preexisting_hold = True
                    else:
                        officer["hold"] = {
                            "kind": "maintenance",
                            "since": _iso(),
                            "note": "runtime recycle",
                            _HOLD_GENERATION_KEY: generation,
                        }
                        hold_owned = True
                        config = _json_object(metadata.get("config_override"))
                        config["officer"] = officer
                        metadata["config_override"] = config

                old_agent_id = str(agent["id"]) if agent is not None else None
                phase = (
                    "blocked"
                    if initial_failure
                    else "awaiting_old_pod_exit"
                    if observation is not None
                    else "fencing_old_authority"
                )
                recycle = {
                    "generation": generation,
                    "phase": phase,
                    "reason": str(reason)[:64],
                    "expected_build_sha": target_build_sha,
                    "target_image_ref": target_image_ref,
                    "observed_build_sha": (
                        observation.build_sha if observation is not None else None
                    ),
                    "old_pod_uid": (
                        observation.pod_uid if observation is not None else None
                    ),
                    "old_agent_id": old_agent_id,
                    "project_id": (
                        str(thread.get("project_id"))
                        if thread.get("project_id")
                        else None
                    ),
                    "hold_owned": hold_owned,
                    "preexisting_hold": preexisting_hold,
                    "attempt": 0,
                    "started_at": _iso(),
                    "drain_wait_started_at": _iso(),
                    "updated_at": _iso(),
                    "last_failure": (
                        {"class": initial_failure, "at": _iso()}
                        if initial_failure
                        else None
                    ),
                    "notification": {
                        "state": (
                            "pending"
                            if initial_failure and post is not None
                            else "none"
                        )
                    },
                }
                agent_pod[_RECYCLE_KEY] = recycle
                agent_pod["observed_build_sha"] = recycle["observed_build_sha"]
                agent_pod["expected_build_sha"] = target_build_sha
                metadata["agent_pod"] = agent_pod
                await self._write_thread_metadata(conn, thread_uuid, metadata)

                if post is not None and hold_owned:
                    # Same durable route transition as the public maintenance
                    # hold. Delivery remains post-commit/retryable; no worker
                    # is left blocked on an Officer deliberately stood down.
                    await conn.execute(
                        """
                        UPDATE job_message_routes
                           SET state = 'escalated_to_user',
                               transitions = transitions || jsonb_build_array(
                                   jsonb_build_object(
                                       'at', now()::text,
                                       'from', state,
                                       'to', 'escalated_to_user',
                                       'actor_kind', 'system',
                                       'actor_id', $3::text,
                                       'note', 'persistent_recycle')),
                               updated_at = now()
                         WHERE project_id = $1
                           AND officer_thread_id = $2
                           AND state = 'pending_officer'
                           AND blocking
                        """,
                        thread["project_id"],
                        thread_uuid,
                        f"recycle:{generation}",
                    )

                if (
                    initial_failure is None
                    and agent is not None
                    and observation is not None
                ):
                    await conn.execute(
                        """
                        UPDATE agents
                           SET intents = COALESCE(intents, '{}'::jsonb) || $2::jsonb
                         WHERE id = $1
                           AND thread_id = $3
                           AND hostname = $4
                        """,
                        agent["id"],
                        json.dumps(
                            {
                                "should_drain": True,
                                "drain_reason": f"persistent_recycle:{generation}",
                            }
                        ),
                        thread_uuid,
                        observation.pod_name,
                    )
                return self._result(thread_id, recycle)

    async def acknowledge_parked_boundary(
        self, *, thread_id: str, agent_id: str | None = None
    ) -> ParkedBoundaryAcknowledgement:
        """Accept the exact old runtime's parked-boundary drain handshake.

        This path performs no workspace snapshot and never ends the thread.
        The process has already settled its current turn and flushed the
        transcript; its subsequent deregistration revokes the old grant.
        """

        try:
            thread_uuid = uuid.UUID(str(thread_id))
        except (TypeError, ValueError):
            return ParkedBoundaryAcknowledgement(False, False, "invalid_thread")
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return ParkedBoundaryAcknowledgement(False, False, "not_current")
                thread, _post, agent = locked
                metadata = _json_object(thread.get("metadata"))
                recycle = _json_object(
                    _json_object(metadata.get("agent_pod")).get(_RECYCLE_KEY)
                )
                phase = str(recycle.get("phase") or "")
                if phase not in _ACTIVE_PHASES:
                    return ParkedBoundaryAcknowledgement(False, False, "inactive")
                if phase != "awaiting_old_pod_exit":
                    return ParkedBoundaryAcknowledgement(
                        True, False, "wrong_recycle_phase"
                    )
                try:
                    old_agent_uuid = uuid.UUID(str(recycle.get("old_agent_id") or ""))
                except (TypeError, ValueError):
                    return ParkedBoundaryAcknowledgement(
                        True, False, "old_agent_missing"
                    )
                if agent_id:
                    try:
                        asserted_agent_uuid = uuid.UUID(str(agent_id))
                    except (TypeError, ValueError):
                        return ParkedBoundaryAcknowledgement(
                            True, False, "agent_assertion_mismatch"
                        )
                    if asserted_agent_uuid != old_agent_uuid:
                        return ParkedBoundaryAcknowledgement(
                            True, False, "agent_assertion_mismatch"
                        )
                intents = _json_object(agent.get("intents")) if agent else {}
                generation = str(recycle.get("generation") or "")
                if (
                    agent is None
                    or agent.get("id") != old_agent_uuid
                    or agent.get("thread_id") != thread_uuid
                    or str(agent.get("hostname") or "")
                    != f"persistent-{str(thread_uuid)[:12]}"
                    or str(agent.get("pod_uid") or "")
                    != str(recycle.get("old_pod_uid") or "")
                    or intents.get("should_drain") is not True
                    or str(intents.get("drain_reason") or "")
                    != f"persistent_recycle:{generation}"
                ):
                    return ParkedBoundaryAcknowledgement(
                        True, False, "drain_authority_mismatch"
                    )
                result = await conn.execute(
                    """
                    UPDATE threads
                       SET status = 'suspended',
                           awaiting_user_since = NULL,
                           control_admission_agent_id = NULL
                     WHERE id = $1
                       AND agent_id = $2
                       AND status IN ('created', 'active', 'awaiting_user', 'suspended')
                    """,
                    thread_uuid,
                    old_agent_uuid,
                )
                if result != "UPDATE 1":
                    return ParkedBoundaryAcknowledgement(
                        True, False, "thread_status_changed"
                    )
                await conn.execute(
                    "UPDATE agents SET status='draining' WHERE id=$1 AND thread_id=$2",
                    old_agent_uuid,
                    thread_uuid,
                )
                return ParkedBoundaryAcknowledgement(True, True, "acknowledged")

    async def _advance(
        self,
        *,
        thread_id: str,
        expected_build_sha: str | None,
        observation: PersistentPodObservation | None,
    ) -> PersistentRecycleResult:
        # The caller's desired SHA is an observation for starting a generation,
        # never authority for one already in flight. Each generation accepts
        # only its durable target image/SHA.
        del expected_build_sha
        current = await self._read_recycle(thread_id)
        if not current:
            return PersistentRecycleResult(thread_id, "cancelled", "cancelled")
        phase = str(current.get("phase") or "")

        if phase == "failed_retryable":
            next_retry = self._parse_time(current.get("next_retry_at"))
            if next_retry is not None and next_retry > _now():
                return self._result(thread_id, current)
            current = await self._set_phase(
                thread_id,
                generation=str(current.get("generation")),
                expected_phase="failed_retryable",
                phase=str(current.get("resume_phase") or "provisioning"),
            )
            return (
                self._result(thread_id, current) if current else self._lost(thread_id)
            )

        if phase == "awaiting_old_pod_exit":
            old_uid = str(current.get("old_pod_uid") or "")
            if observation is not None and observation.pod_uid == old_uid:
                if observation.terminal:
                    await self._provisioner.delete_agent_pod_exact(
                        thread_id, expected_pod_uid=old_uid
                    )
                else:
                    started = self._parse_time(current.get("drain_wait_started_at"))
                    if started is not None and started < _now() - timedelta(minutes=5):
                        return await self._fail(
                            thread_id,
                            current,
                            "drain_boundary_timeout",
                            resume_phase="awaiting_old_pod_exit",
                        )
                return self._result(thread_id, current)
            if observation is not None:
                if observation.labels.get("srw/recycle-generation") == current.get(
                    "generation"
                ):
                    changed = await self._set_phase(
                        thread_id,
                        generation=str(current["generation"]),
                        expected_phase=phase,
                        phase="awaiting_replacement",
                        extras={"new_pod_uid": observation.pod_uid},
                    )
                    return (
                        self._result(thread_id, changed)
                        if changed
                        else self._lost(thread_id)
                    )
                return await self._fail(
                    thread_id,
                    current,
                    "replacement_authority_mismatch",
                    resume_phase="awaiting_old_pod_exit",
                )
            changed = await self._set_phase(
                thread_id,
                generation=str(current["generation"]),
                expected_phase=phase,
                phase="fencing_old_authority",
            )
            return (
                self._result(thread_id, changed) if changed else self._lost(thread_id)
            )

        if phase == "fencing_old_authority":
            changed = await self._fence_old_authority(thread_id, current)
            return (
                self._result(thread_id, changed) if changed else self._lost(thread_id)
            )

        if phase == "provisioning":
            claim_id = str(uuid.uuid4())
            claimed = await self._claim_provisioning(thread_id, current, claim_id)
            if claimed is None:
                return self._lost(thread_id)
            if claimed.get("provision_claim_id") != claim_id:
                return self._result(thread_id, claimed)
            result = await self._provisioner.create_agent_pod(
                thread_id,
                config_name=str(current.get("config_name") or "session_base"),
                lifecycle_generation=str(current["generation"]),
                target_image_ref=str(current.get("target_image_ref") or "") or None,
            )
            if result.status == PersistentPodCreateStatus.TERMINATING:
                return self._result(thread_id, current)
            if result.status not in {
                PersistentPodCreateStatus.CREATED,
                PersistentPodCreateStatus.ALREADY_CURRENT,
            }:
                return await self._fail(
                    thread_id,
                    claimed,
                    result.failure_class or result.status.value,
                    resume_phase="provisioning",
                )
            changed = await self._set_phase(
                thread_id,
                generation=str(current["generation"]),
                expected_phase="provisioning_claimed",
                expected_claim_id=claim_id,
                phase="awaiting_replacement",
                extras={
                    "new_pod_uid": result.pod_uid,
                    "replacement_wait_started_at": _iso(),
                    "provision_claim_id": None,
                    "provision_claim_expires_at": None,
                },
            )
            return (
                self._result(thread_id, changed) if changed else self._lost(thread_id)
            )

        if phase == "provisioning_claimed":
            expires = self._parse_time(current.get("provision_claim_expires_at"))
            if expires is None or expires > _now():
                return self._result(thread_id, current)
            changed = await self._set_phase(
                thread_id,
                generation=str(current["generation"]),
                expected_phase=phase,
                expected_claim_id=str(current.get("provision_claim_id") or ""),
                phase="provisioning",
                extras={
                    "provision_claim_id": None,
                    "provision_claim_expires_at": None,
                },
            )
            return (
                self._result(thread_id, changed) if changed else self._lost(thread_id)
            )

        if phase == "awaiting_replacement":
            if observation is None:
                started = self._parse_time(current.get("replacement_wait_started_at"))
                if started is not None and started < _now() - timedelta(minutes=5):
                    return await self._fail(
                        thread_id,
                        current,
                        "replacement_missing",
                        resume_phase="provisioning",
                    )
                return self._result(thread_id, current)
            if (
                observation.pod_uid == current.get("old_pod_uid")
                or observation.labels.get("srw/recycle-generation")
                != current.get("generation")
                or (
                    current.get("expected_build_sha") is not None
                    and observation.build_sha != current.get("expected_build_sha")
                )
            ):
                return await self._fail(
                    thread_id,
                    current,
                    "replacement_authority_mismatch",
                    resume_phase="awaiting_replacement",
                )
            if not self._replacement_matches(
                observation,
                current,
                expected_build_sha=current.get("expected_build_sha"),
            ):
                started = self._parse_time(current.get("replacement_wait_started_at"))
                if started is not None and started < _now() - timedelta(minutes=5):
                    return await self._fail(
                        thread_id,
                        current,
                        "replacement_not_ready",
                        resume_phase="awaiting_replacement",
                    )
                return self._result(thread_id, current)
            completed = await self._complete_if_authoritative(
                thread_id, current, observation
            )
            if completed is None:
                return self._result(thread_id, current)
            if completed.get("phase") == "complete" and self._on_complete is not None:
                value = self._on_complete(
                    str(completed.get("project_id") or ""), thread_id
                )
                if inspect.isawaitable(value):
                    await value
            return self._result(thread_id, completed)

        return self._result(thread_id, current)

    async def _fence_old_authority(
        self, thread_id: str, current: dict[str, Any]
    ) -> dict[str, Any] | None:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return None
                thread, _post, agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                if not self._same_state(recycle, current, "fencing_old_authority"):
                    return recycle or None

                old_agent_id = current.get("old_agent_id")
                if old_agent_id:
                    old_uuid = uuid.UUID(str(old_agent_id))
                    # Grant is locked after agent, preserving the runtime actor
                    # lifecycle order. DELETE's database trigger revokes it and
                    # retains the immutable agent UUID provenance snapshot.
                    await conn.fetch(
                        "SELECT id FROM runtime_actor_grants "
                        "WHERE agent_id = $1 AND revoked_at IS NULL FOR UPDATE",
                        old_uuid,
                    )
                    await conn.execute(
                        "UPDATE threads SET agent_id = NULL, "
                        "control_admission_agent_id = NULL "
                        "WHERE id = $1 AND agent_id = $2",
                        thread_uuid,
                        old_uuid,
                    )
                    await conn.execute("DELETE FROM agents WHERE id = $1", old_uuid)

                recycle.update(
                    {
                        "phase": "provisioning",
                        "updated_at": _iso(),
                        "config_name": str(thread.get("config_name") or "session_base"),
                    }
                )
                agent_pod[_RECYCLE_KEY] = recycle
                metadata["agent_pod"] = agent_pod
                result = await conn.execute(
                    """
                    UPDATE threads
                       SET metadata = $2::jsonb,
                           status = CASE WHEN status = 'suspended' THEN 'active'
                                         ELSE status END,
                           awaiting_user_since = NULL,
                           control_admission_agent_id = NULL
                     WHERE id = $1 AND status <> 'ended'
                    """,
                    thread_uuid,
                    json.dumps(metadata),
                )
                return recycle if result == "UPDATE 1" else None

    async def _complete_if_authoritative(
        self,
        thread_id: str,
        current: dict[str, Any],
        observation: PersistentPodObservation,
    ) -> dict[str, Any] | None:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return None
                thread, post, agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                if not self._same_state(recycle, current, "awaiting_replacement"):
                    return recycle or None
                if agent is None or not self._agent_matches(
                    agent, observation, thread_id
                ):
                    return None
                if post is not None:
                    grant = await lock_current_officer_runtime_grant(
                        conn,
                        post=post,
                        thread=thread,
                        agent=agent,
                    )
                else:
                    grant = await conn.fetchrow(
                        """
                        SELECT id
                          FROM runtime_actor_grants
                         WHERE thread_id = $1
                           AND agent_id = $2
                           AND revoked_at IS NULL
                           AND refresh_expires_at > now()
                         ORDER BY created_at DESC
                         LIMIT 1
                         FOR UPDATE
                        """,
                        thread_uuid,
                        agent["id"],
                    )
                if grant is None:
                    return None

                live_image_ref = str(getattr(self._provisioner, "image_ref", "") or "")
                live_build_sha = getattr(
                    self._provisioner,
                    "expected_build_sha",
                    recycle.get("expected_build_sha"),
                )
                target_changed = (
                    bool(live_build_sha)
                    and live_build_sha != recycle.get("expected_build_sha")
                ) or (
                    not live_build_sha
                    and bool(live_image_ref)
                    and live_image_ref != str(recycle.get("target_image_ref") or "")
                )
                if target_changed:
                    return await self._chain_target_generation_locked(
                        conn,
                        thread=thread,
                        post=post,
                        agent=agent,
                        metadata=metadata,
                        agent_pod=agent_pod,
                        recycle=recycle,
                        observation=observation,
                        target_image_ref=live_image_ref,
                        target_build_sha=live_build_sha,
                    )

                officer = self._officer_config(metadata)
                if post is not None and bool(recycle.get("hold_owned")):
                    hold = officer.get("hold")
                    if not isinstance(hold, dict) or hold.get(
                        _HOLD_GENERATION_KEY
                    ) != recycle.get("generation"):
                        return await self._mark_locked_failure(
                            conn,
                            thread_uuid,
                            metadata,
                            recycle,
                            "hold_ownership_lost",
                            resume_phase="awaiting_replacement",
                        )
                    officer["hold"] = None
                    config = _json_object(metadata.get("config_override"))
                    config["officer"] = officer
                    metadata["config_override"] = config

                recycle.update(
                    {
                        "phase": "complete",
                        "state": "current",
                        "completed_at": _iso(),
                        "updated_at": _iso(),
                        "observed_build_sha": observation.build_sha,
                        "new_pod_uid": observation.pod_uid,
                        "last_failure": None,
                        "project_id": (
                            str(thread.get("project_id"))
                            if thread.get("project_id")
                            else None
                        ),
                    }
                )
                agent_pod.update(
                    {
                        _RECYCLE_KEY: recycle,
                        "status": "ready",
                        "pod_name": observation.pod_name,
                        "pod_uid": observation.pod_uid,
                        "observed_build_sha": observation.build_sha,
                        "expected_build_sha": recycle.get("expected_build_sha"),
                    }
                )
                metadata["agent_pod"] = agent_pod
                result = await conn.execute(
                    "UPDATE threads SET metadata=$2::jsonb, status='active', "
                    "awaiting_user_since=NULL WHERE id=$1 AND status <> 'ended'",
                    thread_uuid,
                    json.dumps(metadata),
                )
                return recycle if result == "UPDATE 1" else None

    async def _chain_target_generation_locked(
        self,
        conn: Any,
        *,
        thread: dict[str, Any],
        post: dict[str, Any] | None,
        agent: dict[str, Any],
        metadata: dict[str, Any],
        agent_pod: dict[str, Any],
        recycle: dict[str, Any],
        observation: PersistentPodObservation,
        target_image_ref: str,
        target_build_sha: str | None,
    ) -> dict[str, Any]:
        """Keep the hold and start a new frozen target after one safe replacement."""

        generation = str(uuid.uuid4())
        officer = self._officer_config(metadata)
        if post is not None and bool(recycle.get("hold_owned")):
            hold = officer.get("hold")
            if not isinstance(hold, dict) or hold.get(
                _HOLD_GENERATION_KEY
            ) != recycle.get("generation"):
                return await self._mark_locked_failure(
                    conn,
                    thread["id"],
                    metadata,
                    recycle,
                    "hold_ownership_lost",
                    resume_phase="awaiting_replacement",
                )
            hold[_HOLD_GENERATION_KEY] = generation
            officer["hold"] = hold
            config = _json_object(metadata.get("config_override"))
            config["officer"] = officer
            metadata["config_override"] = config

        now = _iso()
        next_recycle = {
            "generation": generation,
            "phase": "awaiting_old_pod_exit",
            "reason": "desired_image_changed_during_recycle",
            "expected_build_sha": target_build_sha,
            "target_image_ref": target_image_ref,
            "observed_build_sha": observation.build_sha,
            "old_pod_uid": observation.pod_uid,
            "old_agent_id": str(agent["id"]),
            "project_id": (
                str(thread.get("project_id")) if thread.get("project_id") else None
            ),
            "hold_owned": bool(recycle.get("hold_owned")),
            "preexisting_hold": bool(recycle.get("preexisting_hold")),
            "attempt": 0,
            "started_at": now,
            "drain_wait_started_at": now,
            "updated_at": now,
            "last_failure": None,
            "notification": {"state": "none"},
            "previous_target": {
                "completed_at": now,
                "expected_build_sha": recycle.get("expected_build_sha"),
                "observed_build_sha": observation.build_sha,
            },
        }
        agent_pod.update(
            {
                _RECYCLE_KEY: next_recycle,
                "observed_build_sha": observation.build_sha,
                "expected_build_sha": target_build_sha,
                "pod_name": observation.pod_name,
                "pod_uid": observation.pod_uid,
            }
        )
        metadata["agent_pod"] = agent_pod
        await self._write_thread_metadata(conn, thread["id"], metadata)
        await conn.execute(
            """
            UPDATE agents
               SET intents = COALESCE(intents, '{}'::jsonb) || $2::jsonb
             WHERE id = $1 AND thread_id = $3
            """,
            agent["id"],
            json.dumps(
                {
                    "should_drain": True,
                    "drain_reason": f"persistent_recycle:{generation}",
                }
            ),
            thread["id"],
        )
        return next_recycle

    async def _fail(
        self,
        thread_id: str,
        current: dict[str, Any],
        failure_class: str,
        *,
        resume_phase: str,
    ) -> PersistentRecycleResult:
        thread_uuid = uuid.UUID(str(thread_id))
        notification_due = False
        project_id = ""
        changed: dict[str, Any] | None = None
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return self._lost(thread_id)
                thread, post, _agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                if recycle.get("generation") != current.get("generation"):
                    return self._result(thread_id, recycle)
                changed = await self._mark_locked_failure(
                    conn,
                    thread_uuid,
                    metadata,
                    recycle,
                    str(failure_class)[:128],
                    resume_phase=resume_phase,
                )
                project_id = str(thread.get("project_id") or "")
                notification_due = bool(
                    post is not None
                    and changed
                    and _json_object(changed.get("notification")).get("state")
                    == "pending"
                )

        if notification_due and changed is not None:
            await self._notify_failure(project_id, thread_id, changed)
        return self._result(thread_id, changed or current)

    async def _mark_locked_failure(
        self,
        conn: Any,
        thread_uuid: uuid.UUID,
        metadata: dict[str, Any],
        recycle: dict[str, Any],
        failure_class: str,
        *,
        resume_phase: str,
    ) -> dict[str, Any]:
        attempt = int(recycle.get("attempt") or 0) + 1
        delay = min(900, 60 * (2 ** min(attempt - 1, 4)))
        notification = _json_object(recycle.get("notification"))
        if notification.get("state") in {None, "", "none"}:
            notification = {"state": "pending"}
        recycle.update(
            {
                "phase": "failed_retryable",
                "resume_phase": resume_phase,
                "attempt": attempt,
                "next_retry_at": _iso(_now() + timedelta(seconds=delay)),
                "updated_at": _iso(),
                "last_failure": {"class": failure_class, "at": _iso()},
                "notification": notification,
            }
        )
        agent_pod = _json_object(metadata.get("agent_pod"))
        agent_pod[_RECYCLE_KEY] = recycle
        metadata["agent_pod"] = agent_pod
        await self._write_thread_metadata(conn, thread_uuid, metadata)
        return recycle

    async def _notify_failure(
        self, project_id: str, thread_id: str, recycle: dict[str, Any]
    ) -> None:
        if self._failure_notifier is None or not recycle.get("last_failure"):
            return
        generation = str(recycle.get("generation") or "")
        claim_id = str(uuid.uuid4())
        if not await self._claim_notification(thread_id, generation, claim_id):
            return
        delivered = False
        try:
            value = self._failure_notifier(
                project_id,
                thread_id,
                str(_json_object(recycle.get("last_failure")).get("class") or "failed"),
            )
            delivered = bool(await value) if inspect.isawaitable(value) else bool(value)
        except Exception:
            logger.warning(
                "Persistent recycle operator notification failed for project %s",
                project_id[:8],
            )
        await self._settle_notification(
            thread_id, generation, claim_id, delivered=delivered
        )

    async def _claim_notification(
        self, thread_id: str, generation: str, claim_id: str
    ) -> bool:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return False
                thread, _post, _agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                notification = _json_object(recycle.get("notification"))
                if recycle.get("generation") != generation:
                    return False
                state = str(notification.get("state") or "none")
                now = _now()
                claim_expires = self._parse_time(notification.get("claim_expires_at"))
                next_retry = self._parse_time(notification.get("next_retry_at"))
                claimable = (
                    state == "pending"
                    or state == "sending"
                    and (claim_expires is None or claim_expires <= now)
                    or state == "failed"
                    and (next_retry is None or next_retry <= now)
                )
                if not claimable:
                    return False
                attempt = int(notification.get("attempt") or 0) + 1
                notification = {
                    "state": "sending",
                    "claim_id": claim_id,
                    "attempt": attempt,
                    "attempted_at": _iso(now),
                    "claim_expires_at": _iso(
                        now + timedelta(seconds=_NOTIFICATION_CLAIM_SECONDS)
                    ),
                }
                recycle["notification"] = notification
                agent_pod[_RECYCLE_KEY] = recycle
                metadata["agent_pod"] = agent_pod
                await self._write_thread_metadata(conn, thread_uuid, metadata)
                return True

    async def _settle_notification(
        self,
        thread_id: str,
        generation: str,
        claim_id: str,
        *,
        delivered: bool,
    ) -> None:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return
                thread, _post, _agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                notification = _json_object(recycle.get("notification"))
                if (
                    recycle.get("generation") != generation
                    or notification.get("claim_id") != claim_id
                    or notification.get("state") != "sending"
                ):
                    return
                settled_at = _now()
                attempt = int(notification.get("attempt") or 1)
                settled = {
                    "state": "delivered" if delivered else "failed",
                    "attempt": attempt,
                    "attempted_at": notification.get("attempted_at"),
                    "settled_at": _iso(settled_at),
                }
                if not delivered:
                    delay = min(900, 60 * (2 ** min(attempt - 1, 4)))
                    settled["next_retry_at"] = _iso(
                        settled_at + timedelta(seconds=delay)
                    )
                recycle["notification"] = settled
                agent_pod[_RECYCLE_KEY] = recycle
                metadata["agent_pod"] = agent_pod
                await self._write_thread_metadata(conn, thread_uuid, metadata)

    async def _read_recycle(self, thread_id: str) -> dict[str, Any]:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM threads WHERE id=$1::uuid", str(thread_id)
            )
        metadata = _json_object(row["metadata"]) if row else {}
        return _json_object(_json_object(metadata.get("agent_pod")).get(_RECYCLE_KEY))

    async def _set_phase(
        self,
        thread_id: str,
        *,
        generation: str,
        expected_phase: str,
        phase: str,
        extras: dict[str, Any] | None = None,
        expected_claim_id: str | None = None,
    ) -> dict[str, Any] | None:
        thread_uuid = uuid.UUID(str(thread_id))
        async with self._db.acquire() as conn:
            async with conn.transaction():
                locked = await self._lock_authority(conn, thread_uuid=thread_uuid)
                if locked is None:
                    return None
                thread, _post, _agent = locked
                metadata = _json_object(thread.get("metadata"))
                agent_pod = _json_object(metadata.get("agent_pod"))
                recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
                if (
                    recycle.get("generation") != generation
                    or recycle.get("phase") != expected_phase
                    or (
                        expected_claim_id is not None
                        and recycle.get("provision_claim_id") != expected_claim_id
                    )
                ):
                    return recycle or None
                recycle.update(extras or {})
                recycle.update({"phase": phase, "updated_at": _iso()})
                agent_pod[_RECYCLE_KEY] = recycle
                metadata["agent_pod"] = agent_pod
                await self._write_thread_metadata(conn, thread_uuid, metadata)
                return recycle

    async def _claim_provisioning(
        self, thread_id: str, current: dict[str, Any], claim_id: str
    ) -> dict[str, Any] | None:
        return await self._set_phase(
            thread_id,
            generation=str(current.get("generation") or ""),
            expected_phase="provisioning",
            phase="provisioning_claimed",
            extras={
                "provision_claim_id": claim_id,
                "provision_claim_expires_at": _iso(_now() + timedelta(minutes=3)),
            },
        )

    async def _lock_authority(
        self,
        conn: Any,
        *,
        thread_uuid: uuid.UUID,
        expected_project_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None] | None:
        # Read only to discover the stable Post key; no row lock is taken out
        # of order. Every mutation then locks Post before thread.
        identity = await conn.fetchrow(
            "SELECT project_id FROM threads WHERE id=$1", thread_uuid
        )
        if identity is None:
            return None
        project_id = identity["project_id"]
        if expected_project_id is not None and str(project_id or "") != str(
            expected_project_id
        ):
            return None
        post_row = None
        if project_id is not None:
            post_row = await conn.fetchrow(
                "SELECT project_id, thread_id, incarnations, state "
                "FROM project_officers "
                "WHERE project_id=$1 FOR UPDATE",
                project_id,
            )
        thread_row = await conn.fetchrow(
            """
            SELECT id, project_id, user_id, status::text AS status,
                   execution_lane, agent_id, config_name, metadata
              FROM threads
             WHERE id=$1
             FOR UPDATE
            """,
            thread_uuid,
        )
        if thread_row is None:
            return None
        thread = dict(thread_row)
        if (
            str(thread.get("status")) not in _LIVE_THREAD_STATUSES
            or str(thread.get("execution_lane") or "pinned") != "pinned"
        ):
            return None
        metadata = _json_object(thread.get("metadata"))
        officer_enabled = (
            str(self._officer_config(metadata).get("enabled", False)).lower() == "true"
        )
        post = dict(post_row) if post_row is not None else None
        if officer_enabled and (
            post is None
            or str(post.get("thread_id") or "") != str(thread_uuid)
            or str(post.get("project_id") or "") != str(project_id)
        ):
            return None
        if not officer_enabled:
            post = None
        agent = None
        if thread.get("agent_id") is not None:
            agent_row = await conn.fetchrow(
                "SELECT id, hostname, status, thread_id, pod_uid, last_heartbeat, "
                "       intents "
                "FROM agents WHERE id=$1 FOR UPDATE",
                thread["agent_id"],
            )
            agent = dict(agent_row) if agent_row is not None else None
        return thread, post, agent

    @staticmethod
    async def _write_thread_metadata(
        conn: Any, thread_uuid: uuid.UUID, metadata: dict[str, Any]
    ) -> None:
        result = await conn.execute(
            "UPDATE threads SET metadata=$2::jsonb WHERE id=$1",
            thread_uuid,
            json.dumps(metadata),
        )
        if result != "UPDATE 1":
            raise RuntimeError("persistent recycle thread disappeared")

    @staticmethod
    def _officer_config(metadata: dict[str, Any]) -> dict[str, Any]:
        config = _json_object(metadata.get("config_override"))
        return _json_object(config.get("officer"))

    @staticmethod
    def _observation_names_thread(
        observation: PersistentPodObservation, thread_id: str
    ) -> bool:
        return (
            observation.labels.get("srw/component") == "persistent-agent"
            and observation.labels.get("srw/thread-id") == thread_id
            and observation.pod_name == f"persistent-{thread_id[:12]}"
        )

    @staticmethod
    def _agent_matches(
        agent: dict[str, Any], observation: PersistentPodObservation, thread_id: str
    ) -> bool:
        heartbeat = agent.get("last_heartbeat")
        if heartbeat is None or heartbeat < _now() - timedelta(minutes=3):
            return False
        return (
            str(agent.get("thread_id") or "") == thread_id
            and str(agent.get("hostname") or "") == observation.pod_name
            and str(agent.get("pod_uid") or "") == observation.pod_uid
            and str(agent.get("status") or "") in {"session", "ready"}
        )

    @staticmethod
    def _replacement_matches(
        observation: PersistentPodObservation,
        recycle: dict[str, Any],
        *,
        expected_build_sha: str | None,
    ) -> bool:
        return bool(
            observation.ready
            and not observation.terminating
            and observation.phase == "Running"
            and observation.pod_uid != recycle.get("old_pod_uid")
            and observation.labels.get("srw/recycle-generation")
            == recycle.get("generation")
            and (
                expected_build_sha is None
                or observation.build_sha == expected_build_sha
            )
        )

    @staticmethod
    def _same_state(
        actual: dict[str, Any], expected: dict[str, Any], phase: str
    ) -> bool:
        return (
            actual.get("generation") == expected.get("generation")
            and actual.get("phase") == phase
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _result(thread_id: str, recycle: dict[str, Any]) -> PersistentRecycleResult:
        failure = _json_object(recycle.get("last_failure")).get("class")
        phase = str(recycle.get("phase") or "unknown")
        return PersistentRecycleResult(
            thread_id=str(thread_id),
            state=(
                "current"
                if phase == "complete"
                else "blocked"
                if phase == "blocked"
                else "failed"
                if phase == "failed_retryable"
                else "recycling"
            ),
            phase=phase,
            generation=(
                str(recycle["generation"]) if recycle.get("generation") else None
            ),
            failure_class=str(failure) if failure else None,
        )

    @staticmethod
    def _lost(thread_id: str) -> PersistentRecycleResult:
        return PersistentRecycleResult(thread_id, "cancelled", "cancelled")


def persistent_recycle_view(metadata: Any) -> dict[str, Any]:
    """Safe server projection; excludes UIDs, agent IDs, generation, and claims."""

    root = _json_object(metadata)
    agent_pod = _json_object(root.get("agent_pod"))
    recycle = _json_object(agent_pod.get(_RECYCLE_KEY))
    failure = _json_object(recycle.get("last_failure"))
    observed = agent_pod.get("observed_build_sha") or recycle.get("observed_build_sha")
    expected = agent_pod.get("expected_build_sha") or recycle.get("expected_build_sha")
    if not recycle:
        phase = "idle"
    else:
        phase = str(recycle.get("phase") or "unknown")
    return {
        "observed_build_sha": observed,
        "expected_build_sha": expected,
        "drift_state": (
            "unknown"
            if not expected or not observed
            else "current"
            if observed == expected
            else "drifted"
        ),
        "recycle_phase": phase,
        "last_failure": str(failure.get("class")) if failure.get("class") else None,
    }
