"""Apply workspace and capability admission before datasource selection.

The application binds the existing workspace classifiers, lane resolver and
permission checks. They remain the policy authorities shared with other callers;
this stage sequences those checks without owning stores or provisioning. Gates
and provisioner properties are read only when their original branches need them.
An unset result must reach persistence unchanged for child-lane inheritance.
"""

from dataclasses import dataclass
import logging
from typing import Any, Callable, Literal, Protocol


logger = logging.getLogger(__name__)

ExecutionLane = Literal["pinned", "stateless"]


class JobWorkspaceStore(Protocol):
    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...


class WorkspaceProvisionerCapabilities(Protocol):
    @property
    def is_available(self) -> bool: ...

    @property
    def in_cluster(self) -> bool: ...


class CheckVmPermission(Protocol):
    async def __call__(
        self, user: dict[str, Any] | None, *, job_needs_vm: bool
    ) -> None: ...


class ResolveExecutionLane(Protocol):
    def __call__(
        self,
        requested_lane: ExecutionLane | None,
        *,
        default_stateless: bool,
        needs_vm: bool,
        needs_sandbox: bool,
    ) -> ExecutionLane | None: ...


class EnforceJobGrants(Protocol):
    async def __call__(
        self,
        config_override: dict[str, Any] | None,
        *,
        user_id: str | None,
        project_ids: list[str],
    ) -> None: ...


@dataclass(frozen=True)
class JobAdmissionWorkspaceDependencies:
    store: JobWorkspaceStore
    needs_vm: Callable[[dict[str, Any]], bool]
    needs_sandbox: Callable[[dict[str, Any]], bool]
    check_vm_permission: CheckVmPermission
    resolve_execution_lane: ResolveExecutionLane
    stateless_default_enabled: Callable[[], bool]
    stateless_enabled: Callable[[], bool]
    vm_workspaces_on_pod_network: Callable[[], bool]
    provisioner: WorkspaceProvisionerCapabilities
    enforce_grants: EnforceJobGrants


async def prepare_job_admission_workspace(
    *,
    context: dict[str, Any],
    config_override: dict[str, Any] | None,
    effective_user_id: str | None,
    project_id: str | None,
    requested_lane: ExecutionLane | None,
    root_creation: bool,
    dependencies: JobAdmissionWorkspaceDependencies,
) -> ExecutionLane | None:
    # VM permission gate: refuse at submit time so the user gets a clear
    # 403 instead of a silent failure later in the dispatcher. The
    # dispatcher re-checks too (in case the grant is revoked after
    # submission) — defense in depth.
    needs_vm = dependencies.needs_vm(
        {"context": context, "config_override": config_override}
    )
    if needs_vm:
        creator = None
        if effective_user_id:
            try:
                creator = await dependencies.store.get_user(effective_user_id)
            except Exception:
                creator = None
        await dependencies.check_vm_permission(creator, job_needs_vm=True)

    # Stateless worker admission is independently gated. Explicit requests
    # keep their fail-closed admission errors; an omitted root lane may try
    # the stateless default and silently fall back to pinned when any
    # capability is absent. Omitted children do not enter that default:
    # PostgresDB.create_job resolves them from the authoritative parent.
    needs_sandbox = dependencies.needs_sandbox(
        {"context": context, "config_override": config_override}
    )
    execution_lane = dependencies.resolve_execution_lane(
        requested_lane,
        default_stateless=dependencies.stateless_default_enabled() and root_creation,
        needs_vm=needs_vm,
        needs_sandbox=needs_sandbox,
    )
    if (
        requested_lane is None
        and dependencies.stateless_default_enabled()
        and root_creation
    ):
        if execution_lane == "stateless":
            logger.info(
                "Job create: worker execution lane defaulted to stateless "
                "for a capable root job"
            )
        else:
            if needs_vm and not dependencies.vm_workspaces_on_pod_network():
                fallback_reason = "external VM jobs require pinned workers"
            elif not dependencies.stateless_enabled():
                fallback_reason = "stateless worker admission is disabled"
            elif not dependencies.provisioner.is_available:
                fallback_reason = "the Kubernetes workspace provisioner is unavailable"
            elif not dependencies.provisioner.in_cluster:
                fallback_reason = "the workspace provisioner is not in-cluster"
            elif not needs_sandbox:
                fallback_reason = "the job does not require a Kubernetes sandbox"
            else:
                fallback_reason = "the worker capability check declined stateless"
            logger.debug(
                "Job create: stateless worker lane default fell back to pinned (%s)",
                fallback_reason,
            )
    if requested_lane == "stateless" and execution_lane == "pinned":
        logger.info(
            "Job create: external VM request keeps job on pinned lane "
            "(stateless worker opt-in ignored)"
        )

    # Capability PEP on the merged override — same fail-fast rationale as
    # the VM gate above, so an over-reaching override is refused here
    # instead of dying at dispatch. See the helper for what it deliberately
    # does not cover.
    await dependencies.enforce_grants(
        config_override,
        user_id=str(effective_user_id) if effective_user_id else None,
        project_ids=[project_id] if project_id else [],
    )
    return execution_lane
