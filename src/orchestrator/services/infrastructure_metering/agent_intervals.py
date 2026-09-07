"""Agent Pod identity, attribution, and non-publishing interval foundations.

Kubernetes labels classify product-shaped Pods, but never establish customer
ownership.  Agent attribution is accepted only after the admitted Pod UID and
Pod name resolve to exactly one database-converged agent identity whose app
rows contain a mutual job or thread binding.  A registered, genuinely unbound
job worker is shared platform capacity; missing, duplicate, or torn identities
remain unknown.

This module deliberately does not replace the Slice 1 workspace reconciler.
Existing workspace and on-demand IDE Pods are classified so a caller can route
them explicitly, while :class:`AgentPodIntervalReconciler` mutates only
``agent_pod`` lifecycles.  It never writes usage events or publication plans.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import asyncpg

from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
    ComputeActivationError,
    lock_compute_scope_epoch_authority,
    read_compute_activation,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryContractError,
    InventoryItem,
    SnapshotAbsenceMutationContext,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    WatchDeletionMutationContext,
    WatchIntervalMutationContext,
    WatchMutationAction,
    WatchTerminalMutationContext,
)
from orchestrator.services.infrastructure_metering.lifecycle_start import (
    LifecycleStart,
    receipt_lifecycle_start,
    watch_lifecycle_start,
)


_AGENT_LIFECYCLE_NAMESPACE = UUID("5798021e-9c3c-5a4d-a246-31e7541d4afe")
_AGENT_RESOURCE = "agent_pod"
_MAX_BINDING_EVENTS_PER_RECONCILE = 256


class PodProductClass(StrEnum):
    """Bounded product classification for one normalized Pod."""

    EXISTING_WORKSPACE = "existing-workspace"
    IDE_WORKSPACE = "ide-session"
    DYNAMIC_AGENT = "dynamic-agent"
    PERSISTENT_AGENT = "persistent-agent"
    STATELESS_AGENT = "stateless-agent"
    VIRT_LAUNCHER = "virt-launcher"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PodProductClassification:
    product_class: PodProductClass
    resource: Literal["workspace_pod", "agent_pod"] | None
    reason_code: str
    purpose: Literal["job", "session"] | None = None
    identity_consistent: bool = True
    explicitly_excluded: bool = False

    @property
    def is_agent(self) -> bool:
        return self.product_class in {
            PodProductClass.DYNAMIC_AGENT,
            PodProductClass.PERSISTENT_AGENT,
            PodProductClass.STATELESS_AGENT,
        }

    @property
    def activation_key(self) -> str | None:
        if self.is_agent:
            return "agent_pod"
        if self.product_class == PodProductClass.IDE_WORKSPACE:
            return "ide_workspace_pod"
        return None


@dataclass(frozen=True, slots=True)
class AgentPodProjection:
    classification: PodProductClassification
    source_uid: str
    api_version: str
    resource_version: str | None
    namespace: str
    name: str
    accrues: bool
    terminal: bool
    cpu_millicores: int | None
    memory_bytes: int | None
    capacity_quality: str | None
    measurement_algorithm: str | None
    creation_timestamp: datetime | None
    start_time: datetime | None
    scheduled_transition_timestamp: datetime | None
    thread_hint: UUID | None
    valid_for_interval: bool

    @property
    def applies(self) -> bool:
        return self.classification.is_agent


@dataclass(frozen=True, slots=True)
class AgentPodAttribution:
    scope: Literal["customer", "shared-platform", "unknown"]
    owner_kind: Literal["job", "thread", "platform"] | None
    owner_id: UUID | None
    user_id: UUID | None
    project_id: UUID | None
    source: str
    quality: Literal["exact", "derived", "ambiguous"]
    reason_code: str
    agent_id: UUID | None = None
    binding_revision: int | None = None
    binding_effective_at: datetime | None = None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _uuid_value(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _positive_revision(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _database_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _microseconds_between(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def _details_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, Mapping):
            return decoded
    return {}


def _has_vmi_owner(payload: Mapping[str, Any]) -> bool:
    return any(
        _mapping(reference).get("kind") == "VirtualMachineInstance"
        for reference in _sequence(payload.get("owner_references"))
    )


def classify_product_pod(item: InventoryItem) -> PodProductClassification:
    """Classify an allowlisted Pod shape without treating labels as authority."""

    if item.source_kind != "pod":
        raise InventoryContractError("Pod product classifier received another kind")
    payload = _mapping(item.normalized_item)
    labels = _mapping(payload.get("labels"))
    app = labels.get("app")
    component = labels.get("srw/component")

    # A VMI controller reference is admitted Kubernetes identity and is
    # stronger than mutable workload labels. Excluding it prevents double
    # counting the launcher Pod when the VMI envelope is metered separately.
    if _has_vmi_owner(payload):
        return PodProductClassification(
            product_class=PodProductClass.VIRT_LAUNCHER,
            resource=None,
            reason_code="virt-launcher-excluded",
            explicitly_excluded=True,
        )

    if app == "srw-workspace" and component in {"workspace", "thread-workspace"}:
        return PodProductClassification(
            product_class=PodProductClass.EXISTING_WORKSPACE,
            resource="workspace_pod",
            reason_code="existing-workspace-slice-1",
        )

    if app == "srw-workspace" and component == "ide-session":
        owner = _canonical_uuid(labels.get("srw/job-id"))
        consistent = owner is not None and not labels.get("srw/thread-id")
        return PodProductClassification(
            product_class=PodProductClass.IDE_WORKSPACE,
            resource="workspace_pod",
            reason_code=("ide-workspace" if consistent else "ide-identity-conflict"),
            identity_consistent=consistent,
        )

    if app == "srw-agent" and component == "agent":
        purpose = labels.get("srw/purpose")
        managed = labels.get("srw/managed-by") == "agent-provisioner"
        has_job_hint = bool(labels.get("srw/job-id"))
        short_thread = labels.get("srw/thread-id")
        full_thread = labels.get("srw.io/thread-id")
        if purpose == "job":
            consistent = (
                managed and not has_job_hint and not short_thread and not full_thread
            )
            reason = (
                "dynamic-job-agent" if consistent else "dynamic-agent-identity-conflict"
            )
            typed_purpose: Literal["job", "session"] | None = "job"
        elif purpose == "session":
            thread_id = _canonical_uuid(full_thread)
            short_matches = (
                isinstance(short_thread, str)
                and thread_id is not None
                and short_thread == str(thread_id)[:12]
            )
            consistent = managed and not has_job_hint and short_matches
            reason = (
                "dynamic-session-agent"
                if consistent
                else "dynamic-agent-identity-conflict"
            )
            typed_purpose = "session"
        else:
            consistent = False
            reason = "dynamic-agent-purpose-invalid"
            typed_purpose = None
        return PodProductClassification(
            product_class=PodProductClass.DYNAMIC_AGENT,
            resource="agent_pod",
            reason_code=reason,
            purpose=typed_purpose,
            identity_consistent=consistent,
        )

    if app == "srw-agent-stateless" and component == "agent":
        # The shared stateless executor pool (stateless_agents.md §5.8). Pods
        # deliberately carry NO job/thread identity — they serve many units
        # over their lifetime, and per-tenant attribution is the lease-interval
        # workstream (§7). Until that lands, the pool is admitted as agent
        # capacity so it never disappears into "other"; identity labels on one
        # of these pods contradict the class and fail toward unknown.
        consistent = not any(
            labels.get(key)
            for key in ("srw/job-id", "srw/thread-id", "srw.io/thread-id")
        )
        return PodProductClassification(
            product_class=PodProductClass.STATELESS_AGENT,
            resource="agent_pod",
            reason_code=(
                "stateless-executor-pool"
                if consistent
                else "stateless-agent-identity-conflict"
            ),
            identity_consistent=consistent,
        )

    if app == "srw-persistent-agent" and component == "persistent-agent":
        thread_id = _canonical_uuid(labels.get("srw/thread-id"))
        consistent = thread_id is not None and not labels.get("srw/job-id")
        return PodProductClassification(
            product_class=PodProductClass.PERSISTENT_AGENT,
            resource="agent_pod",
            reason_code=(
                "persistent-agent"
                if consistent
                else "persistent-agent-identity-conflict"
            ),
            purpose="session",
            identity_consistent=consistent,
        )

    return PodProductClassification(
        product_class=PodProductClass.OTHER,
        resource=None,
        reason_code="other-pod",
    )


def project_agent_pod(item: InventoryItem) -> AgentPodProjection:
    """Project only admitted capacity and bounded agent identity hints."""

    classification = classify_product_pod(item)
    payload = _mapping(item.normalized_item)
    api_version = payload.get("api_version")
    resource_version = payload.get("resource_version")
    namespace = payload.get("namespace")
    name = payload.get("name")
    if not all(
        isinstance(value, str) and value for value in (api_version, namespace, name)
    ):
        raise InventoryContractError("normalized Pod identity projection is incomplete")
    if resource_version is not None and not isinstance(resource_version, str):
        raise InventoryContractError("normalized Pod resource version is invalid")

    labels = _mapping(payload.get("labels"))
    thread_hint = None
    if classification.product_class == PodProductClass.DYNAMIC_AGENT:
        if classification.purpose == "session":
            thread_hint = _canonical_uuid(labels.get("srw.io/thread-id"))
    elif classification.product_class == PodProductClass.PERSISTENT_AGENT:
        thread_hint = _canonical_uuid(labels.get("srw/thread-id"))

    lifecycle = _mapping(payload.get("lifecycle"))
    scheduled_condition = _mapping(lifecycle.get("pod_scheduled_condition"))
    capacity = _mapping(payload.get("capacity"))
    cpu = _nonnegative_int(capacity.get("cpu_millicores"))
    memory = _nonnegative_int(capacity.get("memory_bytes"))
    quality = capacity.get("capacity_quality")
    algorithm = capacity.get("measurement_algorithm") or payload.get(
        "measurement_algorithm"
    )
    capacity_valid = (
        cpu is not None
        and memory is not None
        and isinstance(quality, str)
        and bool(quality)
        and isinstance(algorithm, str)
        and bool(algorithm)
    )
    if item.valid_for_metering and not capacity_valid:
        raise InventoryContractError("valid normalized Pod lacks effective capacity")
    terminal = lifecycle.get("terminal") is True
    accrues = lifecycle.get("accrues") is True and not terminal
    return AgentPodProjection(
        classification=classification,
        source_uid=item.source_uid,
        api_version=api_version,
        resource_version=resource_version,
        namespace=namespace,
        name=name,
        accrues=accrues,
        terminal=terminal,
        cpu_millicores=cpu,
        memory_bytes=memory,
        capacity_quality=quality if isinstance(quality, str) else None,
        measurement_algorithm=algorithm if isinstance(algorithm, str) else None,
        creation_timestamp=_timestamp(lifecycle.get("creation_timestamp")),
        start_time=_timestamp(lifecycle.get("start_time")),
        scheduled_transition_timestamp=(
            _timestamp(scheduled_condition.get("last_transition_time"))
            if scheduled_condition.get("status") == "True"
            else None
        ),
        thread_hint=thread_hint,
        valid_for_interval=item.valid_for_metering and capacity_valid,
    )


def _unknown(
    reason: str,
    *,
    agent_id: UUID | None = None,
    binding_revision: int | None = None,
    binding_effective_at: datetime | None = None,
) -> AgentPodAttribution:
    return AgentPodAttribution(
        scope="unknown",
        owner_kind=None,
        owner_id=None,
        user_id=None,
        project_id=None,
        source="app-db-agent-identity-conflict",
        quality="ambiguous",
        reason_code=reason,
        agent_id=agent_id,
        binding_revision=binding_revision,
        binding_effective_at=binding_effective_at,
    )


def _shared(
    agent_id: UUID,
    *,
    binding_revision: int | None = None,
    binding_effective_at: datetime | None = None,
) -> AgentPodAttribution:
    return AgentPodAttribution(
        scope="shared-platform",
        owner_kind="platform",
        owner_id=None,
        user_id=None,
        project_id=None,
        source="app-db-agent-unbound",
        quality="exact",
        reason_code="warm-agent-unbound",
        agent_id=agent_id,
        binding_revision=binding_revision,
        binding_effective_at=binding_effective_at,
    )


def _customer(
    *,
    agent_id: UUID,
    owner_kind: Literal["job", "thread"],
    owner_id: UUID,
    user_id: UUID,
    project_id: UUID | None,
    binding_revision: int | None = None,
    binding_effective_at: datetime | None = None,
) -> AgentPodAttribution:
    return AgentPodAttribution(
        scope="customer",
        owner_kind=owner_kind,
        owner_id=owner_id,
        user_id=user_id,
        project_id=project_id,
        source="app-db-agent-mutual-binding",
        quality="exact",
        reason_code=f"{owner_kind}-agent-mutual-binding",
        agent_id=agent_id,
        binding_revision=binding_revision,
        binding_effective_at=binding_effective_at,
    )


_AGENT_IDENTITY_SQL = """
/* infrastructure-metering:resolve-agent-pod */
SELECT identity.agent_id, identity.agent_present, identity.pod_uid,
       identity.hostname, identity.identity_state,
       identity.attribution_scope, identity.owner_kind, identity.owner_id,
       identity.user_id, identity.project_id, identity.reason_code,
       identity.revision, identity.effective_at,
       thread.agent_id AS thread_agent_id,
       thread.metadata->'agent_pod'->>'pod_name' AS thread_pod_name,
       thread.metadata->'agent_pod'->>'namespace' AS thread_pod_namespace
FROM agent_metering_pod_identity_state identity
LEFT JOIN threads thread
  ON identity.owner_kind = 'thread' AND thread.id = identity.owner_id
WHERE identity.agent_present AND identity.pod_uid = $1
ORDER BY identity.agent_id
LIMIT 2
"""


async def resolve_agent_pod_attribution(
    conn: asyncpg.Connection,
    projection: AgentPodProjection,
) -> AgentPodAttribution:
    """Resolve one agent Pod through exact admitted UID and mutual DB binding."""

    if not projection.applies:
        return _unknown("not-agent-pod")
    if not projection.classification.identity_consistent:
        return _unknown(projection.classification.reason_code)

    if projection.classification.product_class == PodProductClass.STATELESS_AGENT:
        # The stateless pool never registers per-pod agent identity, so the
        # mutual-binding lookup below can only ever say "registration missing".
        # The capacity is real and shared: attribute it to the platform with no
        # binding cursor (the confirm path requires exactly that shape).
        # Per-lease customer attribution is the §7 workstream, not this one.
        return AgentPodAttribution(
            scope="shared-platform",
            owner_kind="platform",
            owner_id=None,
            user_id=None,
            project_id=None,
            source="chart-stateless-pool",
            quality="exact",
            reason_code="stateless-executor-pool",
        )

    rows = await conn.fetch(_AGENT_IDENTITY_SQL, projection.source_uid)
    if len(rows) == 0:
        return _unknown("agent-registration-missing")
    if len(rows) != 1:
        return _unknown("agent-pod-uid-ambiguous")
    row = rows[0]
    agent_id = _uuid_value(row["agent_id"])
    if agent_id is None:
        return _unknown("agent-id-invalid")
    binding_revision = _positive_revision(row["revision"])
    binding_effective_at = _database_timestamp(row["effective_at"])
    if binding_revision is None or binding_effective_at is None:
        return _unknown("agent-binding-cursor-invalid", agent_id=agent_id)
    if row["hostname"] != projection.name:
        return _unknown(
            "agent-pod-name-mismatch",
            agent_id=agent_id,
            binding_revision=binding_revision,
            binding_effective_at=binding_effective_at,
        )
    reason = row["reason_code"]
    if not isinstance(reason, str) or not reason:
        reason = "agent-metering-state-invalid"
    if row["identity_state"] != "valid":
        return _unknown(
            reason,
            agent_id=agent_id,
            binding_revision=binding_revision,
            binding_effective_at=binding_effective_at,
        )

    scope = row["attribution_scope"]
    owner_kind = row["owner_kind"]
    owner_id = _uuid_value(row["owner_id"])
    user_id = _uuid_value(row["user_id"])
    project_id = _uuid_value(row["project_id"])

    classification = projection.classification
    if (
        classification.product_class == PodProductClass.DYNAMIC_AGENT
        and classification.purpose == "job"
    ):
        if (
            scope == "shared-platform"
            and owner_kind is None
            and owner_id is None
            and user_id is None
            and project_id is None
        ):
            return _shared(
                agent_id,
                binding_revision=binding_revision,
                binding_effective_at=binding_effective_at,
            )
        if (
            scope != "customer"
            or owner_kind != "job"
            or owner_id is None
            or user_id is None
        ):
            return _unknown(
                reason,
                agent_id=agent_id,
                binding_revision=binding_revision,
                binding_effective_at=binding_effective_at,
            )
        return _customer(
            agent_id=agent_id,
            owner_kind="job",
            owner_id=owner_id,
            user_id=user_id,
            project_id=project_id,
            binding_revision=binding_revision,
            binding_effective_at=binding_effective_at,
        )

    thread_hint = projection.thread_hint
    thread_agent_id = _uuid_value(row["thread_agent_id"])
    if (
        thread_hint is None
        or scope != "customer"
        or owner_kind != "thread"
        or owner_id != thread_hint
        or thread_agent_id != agent_id
        or row["thread_pod_name"] != projection.name
        or row["thread_pod_namespace"] != projection.namespace
        or user_id is None
    ):
        return _unknown(
            "thread-agent-binding-conflict",
            agent_id=agent_id,
            binding_revision=binding_revision,
            binding_effective_at=binding_effective_at,
        )
    return _customer(
        agent_id=agent_id,
        owner_kind="thread",
        owner_id=owner_id,
        user_id=user_id,
        project_id=project_id,
        binding_revision=binding_revision,
        binding_effective_at=binding_effective_at,
    )


def _binding_attribution(
    projection: AgentPodProjection,
    row: Mapping[str, Any],
) -> AgentPodAttribution:
    """Snapshot one immutable binding revision without consulting mutable rows."""

    agent_id = _uuid_value(_row_value(row, "agent_id"))
    revision = _positive_revision(_row_value(row, "revision"))
    effective_at = _database_timestamp(_row_value(row, "effective_at"))
    if agent_id is None or revision is None or effective_at is None:
        raise InventoryConflictError("agent binding journal cursor is invalid")
    reason = _row_value(row, "reason_code")
    if not isinstance(reason, str) or not reason:
        raise InventoryConflictError("agent binding journal reason is invalid")
    present = _row_value(row, "agent_present")
    if not isinstance(present, bool):
        raise InventoryConflictError("agent binding journal presence is invalid")
    if not present or _row_value(row, "identity_state") != "valid":
        return _unknown(
            reason,
            agent_id=agent_id,
            binding_revision=revision,
            binding_effective_at=effective_at,
        )

    scope = _row_value(row, "attribution_scope")
    owner_kind = _row_value(row, "owner_kind")
    owner_id = _uuid_value(_row_value(row, "owner_id"))
    user_id = _uuid_value(_row_value(row, "user_id"))
    raw_project_id = _row_value(row, "project_id")
    project_id = _uuid_value(raw_project_id)
    if raw_project_id is not None and project_id is None:
        raise InventoryConflictError("agent binding journal project is invalid")

    classification = projection.classification
    if (
        classification.product_class == PodProductClass.DYNAMIC_AGENT
        and classification.purpose == "job"
    ):
        if (
            scope == "shared-platform"
            and owner_kind is None
            and owner_id is None
            and user_id is None
            and project_id is None
        ):
            return _shared(
                agent_id,
                binding_revision=revision,
                binding_effective_at=effective_at,
            )
        if (
            scope == "customer"
            and owner_kind == "job"
            and owner_id is not None
            and user_id is not None
        ):
            return _customer(
                agent_id=agent_id,
                owner_kind="job",
                owner_id=owner_id,
                user_id=user_id,
                project_id=project_id,
                binding_revision=revision,
                binding_effective_at=effective_at,
            )
        return _unknown(
            reason,
            agent_id=agent_id,
            binding_revision=revision,
            binding_effective_at=effective_at,
        )

    if (
        scope == "customer"
        and owner_kind == "thread"
        and owner_id is not None
        and owner_id == projection.thread_hint
        and user_id is not None
    ):
        return _customer(
            agent_id=agent_id,
            owner_kind="thread",
            owner_id=owner_id,
            user_id=user_id,
            project_id=project_id,
            binding_revision=revision,
            binding_effective_at=effective_at,
        )
    return _unknown(
        "thread-agent-binding-conflict",
        agent_id=agent_id,
        binding_revision=revision,
        binding_effective_at=effective_at,
    )


def _binding_cursor(details: Mapping[str, Any]) -> tuple[UUID, int, datetime] | None:
    """Read a new-format cursor; an old interval deliberately bootstraps."""

    if "binding_revision" not in details:
        return None
    raw_agent_id = details.get("agent_id")
    raw_revision = details.get("binding_revision")
    raw_effective_at = details.get("binding_effective_at")
    if raw_agent_id is None and raw_revision is None and raw_effective_at is None:
        return None
    agent_id = _uuid_value(raw_agent_id)
    revision = _positive_revision(raw_revision)
    effective_at = _timestamp(raw_effective_at)
    if agent_id is None or revision is None or effective_at is None:
        raise InventoryConflictError("agent interval binding cursor is invalid")
    return agent_id, revision, effective_at


def _interval_attribution(
    row: Mapping[str, Any],
    details: Mapping[str, Any],
) -> AgentPodAttribution:
    """Restore the immutable attribution snapshot carried by an open interval."""

    cursor = _binding_cursor(details)
    agent_id = None if cursor is None else cursor[0]
    revision = None if cursor is None else cursor[1]
    effective_at = None if cursor is None else cursor[2]
    scope = _row_value(row, "attribution_scope")
    owner_kind = _row_value(row, "owner_kind")
    owner_id = _uuid_value(_row_value(row, "owner_id"))
    user_id = _uuid_value(_row_value(row, "user_id"))
    project_id = _uuid_value(_row_value(row, "project_id"))
    source = _row_value(row, "attribution_source")
    quality = _row_value(row, "attribution_quality")
    reason = details.get("attribution_reason")
    if not isinstance(source, str) or not source or not isinstance(reason, str):
        raise InventoryConflictError("agent interval attribution snapshot is invalid")
    if quality not in {"exact", "derived", "ambiguous"}:
        raise InventoryConflictError("agent interval attribution quality is invalid")
    if scope == "customer":
        if owner_kind not in {"job", "thread"} or owner_id is None or user_id is None:
            raise InventoryConflictError("agent customer interval identity is invalid")
    elif scope == "shared-platform":
        if owner_kind != "platform" or owner_id is not None or user_id is not None:
            raise InventoryConflictError("agent shared interval identity is invalid")
    elif scope == "unknown":
        if owner_kind is not None or owner_id is not None or user_id is not None:
            raise InventoryConflictError("agent unknown interval identity is invalid")
    else:
        raise InventoryConflictError("agent interval attribution scope is invalid")
    return AgentPodAttribution(
        scope=scope,
        owner_kind=owner_kind,
        owner_id=owner_id,
        user_id=user_id,
        project_id=project_id,
        source=source,
        quality=quality,
        reason_code=reason,
        agent_id=agent_id,
        binding_revision=revision,
        binding_effective_at=effective_at,
    )


_BINDING_HEAD_SQL = """
/* infrastructure-metering:lock-agent-binding-head */
SELECT agent_id, agent_present, pod_uid, hostname, identity_state,
       attribution_scope, owner_kind, owner_id, user_id, project_id,
       reason_code, revision, effective_at
FROM agent_metering_pod_identity_state
WHERE agent_id = $1
FOR SHARE
"""


_BINDING_EVENTS_SQL = """
/* infrastructure-metering:replay-agent-bindings */
SELECT id, agent_id, revision, agent_present, pod_uid, hostname,
       identity_state, attribution_scope, owner_kind, owner_id,
       user_id, project_id, reason_code, transition_source, effective_at
FROM agent_metering_binding_events
WHERE agent_id = $1 AND revision >= $2 AND revision <= $3
ORDER BY revision
LIMIT $4
"""


async def read_compute_metering_activation(
    conn: asyncpg.Connection,
    activation_key: str = _AGENT_RESOURCE,
) -> ComputeActivation | None:
    """Read and lock activation, returning ``None`` when 0103 is unavailable."""

    try:
        activation = await read_compute_activation(conn, activation_key)
    except (
        ComputeActivationError,
        asyncpg.UndefinedColumnError,
        asyncpg.UndefinedTableError,
    ):
        return None
    if activation.activation_key != activation_key:
        return None
    if activation.state not in {"disabled", "shadow", "active"}:
        return None
    if activation.state == "active":
        if activation.activated_at is None or activation.database_time is None:
            return None
    elif activation.activated_at is not None:
        return None
    return activation


def _activation_permits_shadow(activation: ComputeActivation) -> bool:
    return activation.state in {"shadow", "active"}


def _activation_permits_intervals(activation: ComputeActivation) -> bool:
    return (
        activation.state == "active"
        and activation.activated_at is not None
        and activation.database_time is not None
        and activation.database_time >= activation.activated_at
    )


@dataclass(frozen=True, slots=True)
class _OpenAgentInterval:
    id: UUID
    source_lifecycle_id: UUID
    compute_scope_epoch_id: UUID
    source_revision: str
    started_at: datetime
    last_confirmed_at: datetime
    attribution: AgentPodAttribution


def _interval_details(
    projection: AgentPodProjection,
    attribution: AgentPodAttribution,
    *,
    transition_source: str | None = None,
    start_evidence_source: str | None = None,
) -> str:
    details: dict[str, Any] = {
        "agent_id": (
            None if attribution.agent_id is None else str(attribution.agent_id)
        ),
        "attribution_reason": attribution.reason_code,
        "binding_effective_at": (
            None
            if attribution.binding_effective_at is None
            else attribution.binding_effective_at.isoformat()
        ),
        "binding_revision": attribution.binding_revision,
        "classification_reason": projection.classification.reason_code,
        "identity_consistent": projection.classification.identity_consistent,
        "product_class": projection.classification.product_class.value,
        "purpose": projection.classification.purpose,
        "thread_hint": (
            None if projection.thread_hint is None else str(projection.thread_hint)
        ),
        "creation_timestamp": (
            None
            if projection.creation_timestamp is None
            else projection.creation_timestamp.isoformat()
        ),
        "start_time": (
            None if projection.start_time is None else projection.start_time.isoformat()
        ),
        "scheduled_transition_timestamp": (
            None
            if projection.scheduled_transition_timestamp is None
            else projection.scheduled_transition_timestamp.isoformat()
        ),
        "publication_enabled": False,
        "slice": "kubernetes-agent-shadow-v1",
    }
    if transition_source is not None:
        details["binding_transition_source"] = transition_source
    if start_evidence_source is not None:
        details["start_evidence_source"] = start_evidence_source
    return json.dumps(details, sort_keys=True, separators=(",", ":"))


def _projection_from_interval(
    row: Mapping[str, Any],
) -> AgentPodProjection | None:
    """Restore the immutable agent identity needed for terminal journal replay."""

    if _row_value(row, "resource") != _AGENT_RESOURCE:
        return None
    if _row_value(row, "source_kind") != "pod":
        raise InventoryConflictError("agent interval source kind is invalid")
    details = _details_object(_row_value(row, "details"))
    try:
        product_class = PodProductClass(str(details.get("product_class")))
    except ValueError as exc:
        raise InventoryConflictError("agent interval product class is invalid") from exc
    if product_class not in {
        PodProductClass.DYNAMIC_AGENT,
        PodProductClass.PERSISTENT_AGENT,
    }:
        raise InventoryConflictError("agent interval product class is inconsistent")

    purpose = details.get("purpose")
    if purpose is not None and purpose not in {"job", "session"}:
        raise InventoryConflictError("agent interval purpose is invalid")
    identity_consistent = details.get("identity_consistent")
    if not isinstance(identity_consistent, bool):
        raise InventoryConflictError("agent interval identity quality is invalid")
    classification_reason = details.get("classification_reason")
    if not isinstance(classification_reason, str) or not classification_reason:
        raise InventoryConflictError("agent interval classification reason is invalid")
    thread_hint = _uuid_value(details.get("thread_hint"))
    if identity_consistent:
        if product_class is PodProductClass.PERSISTENT_AGENT and purpose != "session":
            raise InventoryConflictError("persistent agent interval purpose is invalid")
        if purpose == "session" and thread_hint is None:
            raise InventoryConflictError("agent session interval thread is invalid")
        if purpose == "job" and thread_hint is not None:
            raise InventoryConflictError("agent job interval carries a thread")

    source_uid = _row_value(row, "source_uid")
    api_version = _row_value(row, "source_api_version")
    resource_version = _row_value(row, "source_resource_version")
    namespace = _row_value(row, "namespace")
    name = _row_value(row, "name")
    quality = _row_value(row, "capacity_quality")
    algorithm = _row_value(row, "measurement_algorithm")
    cpu = _nonnegative_int(_row_value(row, "cpu_millicores"))
    memory = _nonnegative_int(_row_value(row, "memory_bytes"))
    if not all(
        isinstance(value, str) and value
        for value in (source_uid, api_version, namespace, name, quality, algorithm)
    ) or (resource_version is not None and not isinstance(resource_version, str)):
        raise InventoryConflictError("agent interval projection is incomplete")
    if cpu is None or memory is None:
        raise InventoryConflictError("agent interval capacity is invalid")

    classification = PodProductClassification(
        product_class=product_class,
        resource=_AGENT_RESOURCE,
        reason_code=classification_reason,
        purpose=purpose,
        identity_consistent=identity_consistent,
    )
    return AgentPodProjection(
        classification=classification,
        source_uid=source_uid,
        api_version=api_version,
        resource_version=resource_version,
        namespace=namespace,
        name=name,
        accrues=True,
        terminal=False,
        cpu_millicores=cpu,
        memory_bytes=memory,
        capacity_quality=quality,
        measurement_algorithm=algorithm,
        creation_timestamp=_timestamp(details.get("creation_timestamp")),
        start_time=_timestamp(details.get("start_time")),
        scheduled_transition_timestamp=_timestamp(
            details.get("scheduled_transition_timestamp")
        ),
        thread_hint=thread_hint,
        valid_for_interval=True,
    )


class AgentPodIntervalReconciler:
    """Activated ``agent_pod`` interval mutations and shadow observations."""

    def __init__(
        self,
        *,
        shadow_enabled: bool,
        activation: ComputeActivation | None = None,
    ) -> None:
        self.shadow_enabled = shadow_enabled
        self._supplied_activation = activation

    async def _activation(self, conn: asyncpg.Connection) -> ComputeActivation | None:
        if self._supplied_activation is not None:
            if self._supplied_activation.activation_key != _AGENT_RESOURCE:
                return None
            return self._supplied_activation
        return await read_compute_metering_activation(conn)

    async def _interval_boundary(
        self,
        conn: asyncpg.Connection,
        context: (
            SnapshotIntervalMutationContext
            | WatchIntervalMutationContext
            | SnapshotAbsenceMutationContext
            | WatchDeletionMutationContext
            | WatchTerminalMutationContext
        ),
    ) -> datetime | None:
        if self._supplied_activation is not None:
            if (
                not _activation_permits_intervals(self._supplied_activation)
                or context.scope_epoch_id
                not in self._supplied_activation.authorized_scope_epoch_ids
            ):
                return None
            return self._supplied_activation.activated_at
        return await lock_compute_scope_epoch_authority(
            conn,
            activation_key=_AGENT_RESOURCE,
            inventory_scope_id=context.inventory_scope_id,
            inventory_scope_epoch_id=context.scope_epoch_id,
        )

    @staticmethod
    async def _initial_start(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: AgentPodProjection,
        activation_boundary: datetime,
    ) -> LifecycleStart:
        if isinstance(context, WatchIntervalMutationContext):
            return await watch_lifecycle_start(
                conn,
                context=context,
                item=item,
                source_kind="pod",
                authority_boundary=activation_boundary,
                creation_at=projection.creation_timestamp,
                scheduled_at=projection.scheduled_transition_timestamp,
                status_start_at=projection.start_time,
                scheduled_source="pod-scheduled-transition",
            )
        return receipt_lifecycle_start(
            received_at=context.received_at,
            authority_boundary=activation_boundary,
            creation_at=projection.creation_timestamp,
            scheduled_at=projection.scheduled_transition_timestamp,
            status_start_at=projection.start_time,
            scheduled_source="pod-scheduled-transition",
        )

    @staticmethod
    def _attribution_matches(
        row: Mapping[str, Any], attribution: AgentPodAttribution
    ) -> bool:
        expected = (
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
        )
        actual = (
            row["attribution_scope"],
            row["owner_kind"],
            row["owner_id"],
            row["user_id"],
            row["project_id"],
            row["attribution_source"],
            row["attribution_quality"],
        )
        return actual == expected

    @staticmethod
    async def _confirm_existing(
        conn: asyncpg.Connection, interval_id: UUID, received_at: datetime
    ) -> UUID:
        confirmed = await conn.fetchval(
            "UPDATE resource_intervals SET "
            "last_seen_at=GREATEST(last_seen_at,$2),"
            "last_confirmed_at=GREATEST(last_confirmed_at,$2),"
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND resource='agent_pod' AND ended_at IS NULL "
            "RETURNING id",
            interval_id,
            received_at,
        )
        if confirmed is None:
            raise InventoryConflictError("agent Pod confirmation lost its lock")
        return confirmed

    @staticmethod
    async def _close_existing(
        conn: asyncpg.Connection,
        interval_id: UUID | None,
        received_at: datetime,
        *,
        reason: str,
        time_source: str = "app-db-received",
        end_uncertainty_us: int | None = None,
    ) -> Mapping[str, Any] | None:
        if interval_id is None:
            return None
        closed = await conn.fetchrow(
            "UPDATE resource_intervals SET ended_at=$2,"
            "end_time_source=$4,"
            "end_uncertainty_us=COALESCE($5::bigint,"
            "floor(extract(epoch FROM ($2-last_confirmed_at))*1000000)::bigint),"
            "end_reason=$3,"
            "updated_at=statement_timestamp() "
            "WHERE id=$1 AND resource='agent_pod' AND ended_at IS NULL "
            "AND $2 >= last_confirmed_at RETURNING id,source_lifecycle_id",
            interval_id,
            received_at,
            reason,
            time_source,
            end_uncertainty_us,
        )
        if closed is None:
            raise InventoryConflictError("agent Pod interval closure lost its lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id=$2 RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError("agent Pod lifecycle head was inconsistent")
        return closed

    @staticmethod
    async def _open_interval(
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
        projection: AgentPodProjection,
        attribution: AgentPodAttribution,
        activated_at: datetime,
        start: LifecycleStart,
    ) -> UUID:
        if context.received_at < activated_at:
            raise InventoryConflictError("agent Pod observation precedes activation")
        if (
            attribution.binding_effective_at is not None
            and attribution.binding_effective_at > context.received_at
        ):
            raise InventoryConflictError(
                "agent binding state is newer than Pod receipt"
            )
        if not (activated_at <= start.started_at <= context.received_at):
            raise InventoryConflictError("agent Pod start exceeds its safe bounds")
        lifecycle_id = uuid5(
            _AGENT_LIFECYCLE_NAMESPACE,
            f"{context.source_cluster}:pod:{projection.source_uid}",
        )
        await conn.execute(
            "INSERT INTO resource_lifecycle_heads "
            "(source_lifecycle_id,latest_revision_no) VALUES ($1,0) "
            "ON CONFLICT (source_lifecycle_id) DO NOTHING",
            lifecycle_id,
        )
        revision_no = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET "
            "latest_revision_no=latest_revision_no+1,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING latest_revision_no",
            lifecycle_id,
        )
        if revision_no is None:
            raise InventoryConflictError("agent Pod lifecycle head is still open")
        if (
            projection.cpu_millicores is None
            or projection.memory_bytes is None
            or projection.capacity_quality is None
            or projection.measurement_algorithm is None
            or item.revision_hash is None
        ):
            raise InventoryContractError("agent Pod interval lacks admitted capacity")

        interval_id = uuid4()
        details = _interval_details(
            projection,
            attribution,
            start_evidence_source=start.evidence_source,
        )
        await conn.execute(
            "INSERT INTO resource_intervals ("
            "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
            "source_api_version,source_resource_version,source_lifecycle_id,"
            "revision_no,source_revision,namespace,name,category,resource,"
            "measurement_basis,cost_domain,resource_class,attribution_scope,"
            "owner_kind,owner_id,user_id,project_id,attribution_source,"
            "attribution_quality,lifecycle_confidence,cpu_millicores,"
            "memory_bytes,capacity_source,capacity_quality,measurement_algorithm,"
            "started_at,start_time_source,start_uncertainty_us,last_seen_at,"
            "last_confirmed_at,last_seen_snapshot_id,materialized_through,details"
            ",compute_scope_epoch_id"
            ") VALUES ("
            "$1,$2,$3,'pod',$4,$5,$6,$7,$8,$9,$10,$11,'compute','agent_pod',"
            "'scheduler-request','workload-allocation','kubernetes-pod',$12,"
            "$13,$14,$15,$16,$17,$18,'kubernetes-visible',$19,$20,"
            "'pod-effective-request',$21,$22,$23,$24,$25,$26,$26,"
            "NULL,$23,$27::jsonb,$28)",
            interval_id,
            context.inventory_scope_id,
            context.source_cluster,
            projection.source_uid,
            projection.api_version,
            projection.resource_version,
            lifecycle_id,
            revision_no,
            item.revision_hash,
            projection.namespace,
            projection.name,
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
            projection.cpu_millicores,
            projection.memory_bytes,
            projection.capacity_quality,
            projection.measurement_algorithm,
            start.started_at,
            start.time_source,
            start.uncertainty_us,
            context.received_at,
            details,
            context.scope_epoch_id,
        )
        linked = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=$2,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING TRUE",
            lifecycle_id,
            interval_id,
        )
        if not linked:
            raise InventoryConflictError("agent Pod lifecycle head link failed")
        return interval_id

    @staticmethod
    def _open_state(row: Mapping[str, Any]) -> _OpenAgentInterval:
        interval_id = _uuid_value(_row_value(row, "id"))
        lifecycle_id = _uuid_value(_row_value(row, "source_lifecycle_id"))
        scope_epoch_id = _uuid_value(_row_value(row, "compute_scope_epoch_id"))
        source_revision = _row_value(row, "source_revision")
        started_at = _database_timestamp(_row_value(row, "started_at"))
        last_confirmed_at = _database_timestamp(_row_value(row, "last_confirmed_at"))
        details = _details_object(_row_value(row, "details"))
        if (
            interval_id is None
            or lifecycle_id is None
            or scope_epoch_id is None
            or not isinstance(source_revision, str)
            or started_at is None
            or last_confirmed_at is None
            or last_confirmed_at < started_at
        ):
            raise InventoryConflictError("current agent Pod interval is invalid")
        return _OpenAgentInterval(
            id=interval_id,
            source_lifecycle_id=lifecycle_id,
            compute_scope_epoch_id=scope_epoch_id,
            source_revision=source_revision,
            started_at=started_at,
            last_confirmed_at=last_confirmed_at,
            attribution=_interval_attribution(row, details),
        )

    @staticmethod
    async def _clone_binding_interval(
        conn: asyncpg.Connection,
        *,
        previous: _OpenAgentInterval,
        projection: AgentPodProjection,
        attribution: AgentPodAttribution,
        started_at: datetime,
        transition_source: str,
    ) -> _OpenAgentInterval:
        revision_no = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET "
            "latest_revision_no=latest_revision_no+1,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING latest_revision_no",
            previous.source_lifecycle_id,
        )
        if revision_no is None:
            raise InventoryConflictError("agent Pod lifecycle head is still open")
        interval_id = uuid4()
        start_source = (
            "app-db-agent-binding-event"
            if attribution.binding_effective_at == started_at
            else "agent-binding-event-clamped"
        )
        inserted = await conn.fetchval(
            "INSERT INTO resource_intervals ("
            "id,inventory_scope_id,source_cluster,source_kind,source_uid,"
            "source_api_version,source_resource_version,source_lifecycle_id,"
            "revision_no,source_revision,namespace,name,category,resource,"
            "measurement_basis,cost_domain,resource_class,attribution_scope,"
            "owner_kind,owner_id,user_id,project_id,attribution_source,"
            "attribution_quality,backing_resource_uid,lifecycle_confidence,"
            "cpu_millicores,memory_bytes,storage_bytes,capacity_source,"
            "capacity_quality,measurement_algorithm,started_at,start_time_source,"
            "start_uncertainty_us,last_seen_at,last_confirmed_at,"
            "last_seen_snapshot_id,materialized_through,details,"
            "compute_scope_epoch_id) "
            "SELECT $1,prior.inventory_scope_id,prior.source_cluster,"
            "prior.source_kind,prior.source_uid,prior.source_api_version,"
            "prior.source_resource_version,prior.source_lifecycle_id,$2,"
            "prior.source_revision,prior.namespace,prior.name,prior.category,"
            "prior.resource,prior.measurement_basis,prior.cost_domain,"
            "prior.resource_class,$3,$4,$5,$6,$7,$8,$9,"
            "prior.backing_resource_uid,prior.lifecycle_confidence,"
            "prior.cpu_millicores,prior.memory_bytes,prior.storage_bytes,"
            "prior.capacity_source,prior.capacity_quality,"
            "prior.measurement_algorithm,$10,$11,0,$10,$10,NULL,$10,$12::jsonb,"
            "prior.compute_scope_epoch_id "
            "FROM resource_intervals prior WHERE prior.id=$13 "
            "AND prior.ended_at IS NOT NULL RETURNING id",
            interval_id,
            revision_no,
            attribution.scope,
            attribution.owner_kind,
            None if attribution.owner_id is None else str(attribution.owner_id),
            attribution.user_id,
            attribution.project_id,
            attribution.source,
            attribution.quality,
            started_at,
            start_source,
            _interval_details(
                projection,
                attribution,
                transition_source=transition_source,
            ),
            previous.id,
        )
        if inserted != interval_id:
            raise InventoryConflictError("agent binding interval clone failed")
        linked = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=$2,"
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id IS NULL "
            "RETURNING TRUE",
            previous.source_lifecycle_id,
            interval_id,
        )
        if not linked:
            raise InventoryConflictError("agent Pod lifecycle head link failed")
        return _OpenAgentInterval(
            id=interval_id,
            source_lifecycle_id=previous.source_lifecycle_id,
            compute_scope_epoch_id=previous.compute_scope_epoch_id,
            source_revision=previous.source_revision,
            started_at=started_at,
            last_confirmed_at=started_at,
            attribution=attribution,
        )

    async def _replay_binding_events(
        self,
        conn: asyncpg.Connection,
        *,
        context: (
            SnapshotIntervalMutationContext
            | WatchIntervalMutationContext
            | SnapshotAbsenceMutationContext
            | WatchDeletionMutationContext
            | WatchTerminalMutationContext
        ),
        projection: AgentPodProjection,
        interval: _OpenAgentInterval,
        activated_at: datetime,
    ) -> _OpenAgentInterval | None:
        """Replay one bounded contiguous journal prefix through this receipt."""

        attribution = interval.attribution
        if (
            attribution.agent_id is None
            or attribution.binding_revision is None
            or attribution.binding_effective_at is None
        ):
            return None
        if (
            context.received_at < interval.last_confirmed_at
            or context.received_at < interval.started_at
            or context.received_at < activated_at
        ):
            raise InventoryConflictError("agent binding replay receipt is stale")

        head = await conn.fetchrow(_BINDING_HEAD_SQL, attribution.agent_id)
        if head is None:
            raise InventoryConflictError("agent binding journal head is missing")
        if _uuid_value(_row_value(head, "agent_id")) != attribution.agent_id:
            raise InventoryConflictError("agent binding journal head changed identity")
        head_revision = _positive_revision(_row_value(head, "revision"))
        if head_revision is None or head_revision < attribution.binding_revision:
            raise InventoryConflictError("agent binding journal head moved backwards")
        events = await conn.fetch(
            _BINDING_EVENTS_SQL,
            attribution.agent_id,
            attribution.binding_revision,
            head_revision,
            _MAX_BINDING_EVENTS_PER_RECONCILE + 2,
        )
        if not events:
            raise InventoryConflictError("agent binding journal has a revision gap")

        expected_revision = attribution.binding_revision
        previous_effective_at: datetime | None = None
        validated: list[tuple[Mapping[str, Any], int, datetime]] = []
        for event in events:
            # Validate only immutable cursor ordering here. A transition newer
            # than this LIST/WATCH receipt must remain completely unapplied,
            # including a future registration onto another Pod identity.
            event_agent_id = _uuid_value(_row_value(event, "agent_id"))
            revision = _positive_revision(_row_value(event, "revision"))
            effective_at = _database_timestamp(_row_value(event, "effective_at"))
            if event_agent_id != attribution.agent_id or revision != expected_revision:
                raise InventoryConflictError(
                    "agent binding journal identity is invalid"
                )
            if effective_at is None or (
                previous_effective_at is not None
                and effective_at < previous_effective_at
            ):
                raise InventoryConflictError("agent binding journal time is invalid")
            validated.append((event, revision, effective_at))
            expected_revision += 1
            previous_effective_at = effective_at

        baseline_effective_at = validated[0][2]
        if baseline_effective_at != attribution.binding_effective_at:
            raise InventoryConflictError("agent binding journal baseline changed")
        if baseline_effective_at > context.received_at:
            raise InventoryConflictError("agent binding baseline exceeds Pod receipt")
        baseline = validated[0][0]
        baseline_matches_pod = (
            _row_value(baseline, "pod_uid") == projection.source_uid
            and _row_value(baseline, "hostname") == projection.name
        )
        if (
            not baseline_matches_pod
            and attribution.reason_code != "agent-pod-identity-moved"
        ):
            raise InventoryConflictError(
                "agent binding baseline does not identify the admitted Pod"
            )

        first_future = next(
            (
                index
                for index, (_, _, effective_at) in enumerate(validated[1:], start=1)
                if effective_at > context.received_at
            ),
            None,
        )
        due_count = sum(
            effective_at <= context.received_at for _, _, effective_at in validated[1:]
        )
        if due_count > _MAX_BINDING_EVENTS_PER_RECONCILE or (
            validated[-1][1] < head_revision and first_future is None
        ):
            raise InventoryConflictError("agent binding journal replay is too large")

        current = interval
        for event, revision, effective_at in validated[1:]:
            if effective_at > context.received_at:
                break
            event_matches_pod = (
                _row_value(event, "pod_uid") == projection.source_uid
                and _row_value(event, "hostname") == projection.name
            )
            # Re-registration is ordinary agent lifecycle, not inventory
            # corruption. Keep metering the Kubernetes-visible old Pod, but
            # stop assigning it to the agent's new owner until identity returns.
            next_attribution = (
                _binding_attribution(projection, event)
                if event_matches_pod
                else _unknown(
                    "agent-pod-identity-moved",
                    agent_id=attribution.agent_id,
                    binding_revision=revision,
                    binding_effective_at=effective_at,
                )
            )
            boundary = max(
                effective_at,
                activated_at,
                current.started_at,
                current.last_confirmed_at,
            )
            if boundary > context.received_at:
                raise InventoryConflictError("agent binding boundary exceeds receipt")
            exact_event_boundary = boundary == effective_at
            await self._close_existing(
                conn,
                current.id,
                boundary,
                reason=f"binding-revision-{revision}",
                time_source=(
                    "app-db-agent-binding-event"
                    if exact_event_boundary
                    else "agent-binding-event-clamped"
                ),
                end_uncertainty_us=(
                    0
                    if exact_event_boundary
                    else _microseconds_between(boundary, effective_at)
                ),
            )
            transition_source = _row_value(event, "transition_source")
            if not isinstance(transition_source, str) or not transition_source:
                raise InventoryConflictError(
                    "agent binding journal transition source is invalid"
                )
            current = await self._clone_binding_interval(
                conn,
                previous=current,
                projection=projection,
                attribution=next_attribution,
                started_at=boundary,
                transition_source=transition_source,
            )
        return current

    async def _mutate(
        self,
        conn: asyncpg.Connection,
        *,
        context: SnapshotIntervalMutationContext | WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        projection = project_agent_pod(item)
        if not projection.applies:
            return None
        activation_boundary = await self._interval_boundary(conn, context)
        if activation_boundary is None:
            return None
        if context.received_at < activation_boundary:
            raise InventoryConflictError("agent Pod observation precedes activation")
        if not projection.valid_for_interval:
            return None
        if context.existing_interval_id is not None:
            existing = await conn.fetchrow(
                "SELECT id,source_lifecycle_id,source_revision,started_at,"
                "last_confirmed_at,compute_scope_epoch_id,attribution_scope,"
                "owner_kind,owner_id,"
                "user_id,project_id,attribution_source,attribution_quality,details "
                "FROM resource_intervals WHERE id=$1 AND resource='agent_pod' "
                "AND ended_at IS NULL FOR UPDATE",
                context.existing_interval_id,
            )
            if existing is None:
                raise InventoryConflictError("current agent Pod interval disappeared")
            current = self._open_state(existing)
            if current.compute_scope_epoch_id != context.scope_epoch_id:
                raise InventoryConflictError(
                    "current agent Pod interval belongs to another inventory epoch"
                )
            replayed = await self._replay_binding_events(
                conn,
                context=context,
                projection=projection,
                interval=current,
                activated_at=activation_boundary,
            )
            if not projection.accrues:
                await self._close_existing(
                    conn,
                    current.id if replayed is None else replayed.id,
                    context.received_at,
                    reason="terminal-or-unscheduled",
                )
                return None
            if replayed is not None:
                if replayed.source_revision == item.revision_hash:
                    return await self._confirm_existing(
                        conn, replayed.id, context.received_at
                    )
                await self._close_existing(
                    conn,
                    replayed.id,
                    context.received_at,
                    reason="revision-changed",
                )
                return await self._open_interval(
                    conn,
                    context=context,
                    item=item,
                    projection=projection,
                    attribution=replayed.attribution,
                    activated_at=activation_boundary,
                    start=LifecycleStart(
                        started_at=context.received_at,
                        time_source="app-db-received",
                        uncertainty_us=0,
                        evidence_source="observed-revision-boundary",
                    ),
                )

            # Old intervals and unresolved registrations have no durable
            # cursor. They may confirm only while they remain unresolved;
            # the first authoritative cursor is snapshotted via a receipt split.
            attribution = await resolve_agent_pod_attribution(conn, projection)
            same_revision = current.source_revision == item.revision_hash
            same_attribution = self._attribution_matches(existing, attribution)
            if (
                same_revision
                and same_attribution
                and attribution.binding_revision is None
            ):
                return await self._confirm_existing(
                    conn, context.existing_interval_id, context.received_at
                )
            await self._close_existing(
                conn,
                context.existing_interval_id,
                context.received_at,
                reason=(
                    "binding-journal-bootstrap"
                    if same_revision and same_attribution
                    else (
                        "attribution-changed" if same_revision else "revision-changed"
                    )
                ),
            )
            return await self._open_interval(
                conn,
                context=context,
                item=item,
                projection=projection,
                attribution=attribution,
                activated_at=activation_boundary,
                start=LifecycleStart(
                    started_at=context.received_at,
                    time_source="app-db-received",
                    uncertainty_us=0,
                    evidence_source="observed-revision-boundary",
                ),
            )
        if not projection.accrues:
            return None
        attribution = await resolve_agent_pod_attribution(conn, projection)
        start = await self._initial_start(
            conn,
            context=context,
            item=item,
            projection=projection,
            activation_boundary=activation_boundary,
        )
        return await self._open_interval(
            conn,
            context=context,
            item=item,
            projection=projection,
            attribution=attribution,
            activated_at=activation_boundary,
            start=start,
        )

    async def apply_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        return await self._mutate(conn, context=context, item=item)

    async def apply_watch(
        self,
        conn: asyncpg.Connection,
        context: WatchIntervalMutationContext,
        item: InventoryItem,
    ) -> UUID | None:
        return await self._mutate(conn, context=context, item=item)

    async def _replay_before_terminal_close(
        self,
        conn: asyncpg.Connection,
        *,
        context: (
            SnapshotAbsenceMutationContext
            | WatchDeletionMutationContext
            | WatchTerminalMutationContext
        ),
        row: Mapping[str, Any],
        reason: str,
        time_source: str,
    ) -> UUID | None:
        projection = _projection_from_interval(row)
        if projection is None:
            return None
        current = self._open_state(row)
        if (
            current.compute_scope_epoch_id != context.scope_epoch_id
            or _uuid_value(_row_value(row, "inventory_scope_id"))
            != context.inventory_scope_id
            or _row_value(row, "source_cluster") != context.source_cluster
        ):
            raise InventoryConflictError(
                "agent terminal proof belongs to another inventory authority"
            )
        if isinstance(
            context, (WatchDeletionMutationContext, WatchTerminalMutationContext)
        ) and (
            context.source_kind != "pod" or context.source_uid != projection.source_uid
        ):
            raise InventoryConflictError("agent deletion identity is inconsistent")
        activation_boundary = await self._interval_boundary(conn, context)
        if activation_boundary is None or context.received_at < activation_boundary:
            raise InventoryConflictError("agent terminal proof lacks epoch authority")
        replayed = await self._replay_binding_events(
            conn,
            context=context,
            projection=projection,
            interval=current,
            activated_at=activation_boundary,
        )
        final = current if replayed is None else replayed
        closed = await self._close_existing(
            conn,
            final.id,
            context.received_at,
            reason=reason,
            time_source=time_source,
        )
        if closed is None:
            raise InventoryConflictError("agent terminal interval was not closed")
        return _uuid_value(_row_value(closed, "id"))

    async def apply_absence(
        self,
        conn: asyncpg.Connection,
        context: SnapshotAbsenceMutationContext,
        row: Mapping[str, Any],
    ) -> bool:
        """Replay app-DB bindings before a complete LIST closes an agent Pod."""

        affected = await self._replay_before_terminal_close(
            conn,
            context=context,
            row=row,
            reason="absent-from-complete-snapshot",
            time_source="complete-inventory-absence",
        )
        return affected is not None

    async def apply_deletion(
        self,
        conn: asyncpg.Connection,
        context: WatchDeletionMutationContext,
        row: Mapping[str, Any] | None,
    ) -> tuple[WatchMutationAction, UUID | None] | None:
        """Replay app-DB bindings before a trusted Pod DELETED receipt."""

        if row is None:
            return None
        affected = await self._replay_before_terminal_close(
            conn,
            context=context,
            row=row,
            reason="watch-deleted",
            time_source="watch-deleted",
        )
        if affected is None:
            return None
        return WatchMutationAction.CLOSE, affected

    async def apply_terminal(
        self,
        conn: asyncpg.Connection,
        context: WatchTerminalMutationContext,
        row: Mapping[str, Any] | None,
    ) -> tuple[WatchMutationAction, UUID | None] | None:
        """Replay app-DB bindings before a terminal Pod object receipt."""

        if row is None:
            return None
        affected = await self._replay_before_terminal_close(
            conn,
            context=context,
            row=row,
            reason="terminal-object-event",
            time_source="watch-terminal",
        )
        if affected is None:
            return None
        return WatchMutationAction.CLOSE, affected

    async def observe_snapshot(
        self,
        conn: asyncpg.Connection,
        context: SnapshotObservationContext,
        item: InventoryItem,
    ) -> None:
        """Record immutable agent shadow evidence once migration 0103 permits it."""

        if not self.shadow_enabled:
            return
        activation = await self._activation(conn)
        if activation is None or not _activation_permits_shadow(activation):
            return

        classification = classify_product_pod(item)
        projection: AgentPodProjection | None = None
        if item.valid_for_metering:
            projection = project_agent_pod(item)

        if not item.valid_for_metering:
            attribution = _unknown("invalid-observation")
            disposition = "invalid"
            cpu = memory = None
            reason = (
                item.item_error.code
                if item.item_error is not None
                else "invalid-observation"
            )
        elif projection is None or not projection.applies:
            attribution = _unknown("not-agent-pod")
            disposition = "not-applicable"
            cpu = memory = None
            reason = classification.reason_code
        elif not projection.valid_for_interval:
            attribution = _unknown("invalid-observation")
            disposition = "invalid"
            cpu = memory = None
            reason = "invalid-observation"
        elif not projection.accrues:
            attribution = _unknown("terminal-or-unscheduled")
            disposition = "not-applicable"
            cpu = memory = None
            reason = "terminal-or-unscheduled"
        else:
            attribution = await resolve_agent_pod_attribution(conn, projection)
            reason = attribution.reason_code
            if attribution.scope == "unknown":
                disposition = "identity-ambiguous"
                cpu = projection.cpu_millicores
                memory = projection.memory_bytes
            else:
                disposition = "eligible-unpriced"
                cpu = projection.cpu_millicores
                memory = projection.memory_bytes

        try:
            await conn.execute(
                "INSERT INTO compute_shadow_observations ("
                "activation_key,snapshot_id,inventory_scope_id,source_kind,"
                "source_uid,resource,product_class,cpu_millicores,memory_bytes,"
                "attribution_scope,owner_kind,owner_id,user_id,project_id,"
                "disposition,reason_code,observed_at) VALUES ("
                "'agent_pod',$1,$2,'pod',$3,'agent_pod',$4,$5,$6,$7,$8,$9,"
                "$10,$11,$12,$13,$14)",
                context.snapshot_id,
                context.inventory_scope_id,
                item.source_uid,
                classification.product_class.value,
                cpu,
                memory,
                attribution.scope,
                attribution.owner_kind,
                attribution.owner_id,
                attribution.user_id,
                attribution.project_id,
                disposition,
                reason,
                context.received_at,
            )
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
            # A mixed-version rollout must stay dark rather than falling back
            # to the legacy workspace comparison or opening an interval.
            return


__all__ = [
    "AgentPodAttribution",
    "AgentPodIntervalReconciler",
    "AgentPodProjection",
    "ComputeActivation",
    "PodProductClass",
    "PodProductClassification",
    "classify_product_pod",
    "project_agent_pod",
    "read_compute_metering_activation",
    "resolve_agent_pod_attribution",
]
