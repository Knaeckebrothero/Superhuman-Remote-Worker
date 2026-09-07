"""Prepare a job's project and expert configuration before write admission.

The caller supplies an authenticated/upload-authorized scope and a sanitized
command. This stage performs no writes or provisioning; all later Officer,
datasource, grant and transactional authority checks still apply. The application
owns the lazy catalogue cache, current feature gates and collaborator lifecycles.
HTTP exceptions remain the compatibility contract for this extraction.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException

from orchestrator.schemas.job_create import JobCreate
from orchestrator.services.config_overrides import (
    deep_merge_dicts,
    validated_config_name,
)
from orchestrator.services.default_experts import (
    DefaultExpertUnavailable,
    ExpertSelection,
    ExpertSelectionError,
)
from orchestrator.services.job_admission_scope import (
    _INTERNAL_JOB_SCOPE_DENIED,
    JobAdmissionOrigin,
    JobAdmissionScope,
)
from orchestrator.services.project_status import (
    PROJECT_ARCHIVED_DETAIL,
    project_is_archived,
)
from shared.expert_reference import ExpertReferenceConflict, resolve_expert_selection
from shared.runtime.core.loader import canonical_config_name
from shared.workspace_contract import (
    WorkspaceContractError,
    configured_workspace_backend,
)

logger = logging.getLogger(__name__)


class JobConfigStore(Protocol):
    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...

    async def get_project(self, project_id: str) -> dict[str, Any] | None: ...


class RequireJobProjectAccess(Protocol):
    async def __call__(
        self,
        principal: dict[str, Any] | None,
        project_id: str | None,
        *,
        denial_detail: str,
    ) -> None: ...


class ResolveWorkerExpert(Protocol):
    async def __call__(
        self,
        *,
        user_id: str,
        project_id: str | None,
        explicit_expert_id: str | None,
        is_admin: bool,
    ) -> ExpertSelection: ...


@dataclass(frozen=True)
class JobAdmissionConfigDependencies:
    store: JobConfigStore
    require_project_access: RequireJobProjectAccess
    bundled_expert_exists: Callable[[str], bool]
    experts_db_enabled: Callable[[], bool]
    user_experts_enabled: Callable[[], Awaitable[bool]]
    resolve_worker_expert: ResolveWorkerExpert


@dataclass(frozen=True)
class JobAdmissionConfig:
    """Prepared configuration, not permission to INSERT a job."""

    context: dict[str, Any]
    project_id: str | None
    config_name: str
    config_override: dict[str, Any] | None
    expert_id: str | None
    request_config_override: dict[str, Any] | None
    requested_workspace_backend: str | None
    root_creation: bool


async def prepare_job_admission_config(
    *,
    command: JobCreate,
    scope: JobAdmissionScope,
    origin: JobAdmissionOrigin,
    dependencies: JobAdmissionConfigDependencies,
) -> JobAdmissionConfig:
    if origin not in ("user_rest", "internal_rest"):
        raise ValueError(f"Unknown job admission origin: {origin}")
    job = command
    context = dict(scope.context)
    effective_user_id = scope.user_id
    # Resolve project_id: authoritative internal origin / public request,
    # then the user's default only when no thread/parent constrained scope.
    project_id = scope.project_id
    if not project_id and effective_user_id and not scope.origin_bound:
        try:
            user = await dependencies.store.get_user(effective_user_id)
            if user and user.get("default_project_id"):
                project_id = str(user["default_project_id"])
        except Exception as e:
            logger.warning(
                f"Failed to resolve default project for user {effective_user_id}: {e}"
            )

    await dependencies.require_project_access(
        scope.principal,
        project_id,
        denial_detail=(
            _INTERNAL_JOB_SCOPE_DENIED
            if origin == "internal_rest"
            else "Project role 'editor' or higher required"
        ),
    )

    # Resolve project config fallback plus the authoritative DB expert
    # selection.  Root jobs persist a concrete expert id; internal
    # children/specialists keep their explicit/inherited selector and never
    # silently acquire a user's current default.
    project = None
    # One catalogue, one selector: `expert` takes a bundled slug or a DB
    # expert UUID and resolves to the (base config, DB overlay) pair this
    # funnel persists. The deprecated aliases go through the same helper,
    # so the "two experts in one call" refusal is stated once — see
    # knowledge-base/knowledge/issues/experts_one_catalogue_two_selection_paths.md.
    try:
        expert_choice = resolve_expert_selection(
            expert=job.expert,
            config_name=job.config_name,
            expert_id=job.expert_id,
        )
    except ExpertReferenceConflict as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if expert_choice.kind == "bundled" and job.expert:
        # `expert` means "an entry from the catalogue", so a slug that is
        # not in it is a typo, not a deployment config. Refuse now: the
        # alternative is a job that provisions and only fails when the
        # agent cannot load its config. `config_name` keeps accepting
        # non-catalogue deployment configs, unvalidated, as it always did.
        if not dependencies.bundled_expert_exists(expert_choice.config_name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown expert '{expert_choice.config_name}'. Use "
                    "list_experts (GET /api/experts) to see the selectable "
                    "experts; pass a bundled expert id or a DB expert UUID."
                ),
            )
    explicit_expert_id = expert_choice.expert_id
    # Write boundary for the pod entrypoint's one caller-controlled word.
    # `config_name` still accepts non-catalogue deployment configs (the
    # branch above only vets `expert`), so this is the only charset check
    # a job's stored selector ever gets — and jobs.config_name is read back
    # by dispatch, resume, subjob grafting and every recovery path.
    config_name = canonical_config_name(
        validated_config_name(expert_choice.config_name) or "worker_base"
    )
    # The resolver can never emit both halves; assert it rather than trust
    # it, because "a DB expert layered over someone else's bundled base"
    # is a config nobody reviewed.
    if explicit_expert_id and config_name != "worker_base":
        raise HTTPException(
            status_code=400,
            detail=(
                "expert_id cannot be combined with a bundled worker "
                "config_name; select one expert source"
            ),
        )
    request_config_override = job.config_override
    try:
        requested_workspace_backend = configured_workspace_backend(
            request_config_override
        )
    except WorkspaceContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.detail},
        ) from exc
    project_default_override: dict[str, Any] | None = None
    if project_id:
        project = await dependencies.store.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=404, detail=f"Project '{project_id}' not found"
            )
        # Layer 2 of the archived-project refusal (§4.3 of
        # knowledge-base/knowledge/features/project_and_job_list_filtering.md).
        # It has to be HERE and not on the guard above: an X-Internal-Key
        # caller — all MCP traffic, all agent delegation, the bench
        # sweeper — skips require_project_member entirely, so the flag
        # would only ever cover the cockpit. This load is unconditional
        # across both paths. Critic/scholar/curator subjobs call
        # postgres_db.create_job directly and stay exempt by construction:
        # finishing in-flight work is not new work.
        if project_is_archived(project):
            raise HTTPException(status_code=409, detail=PROJECT_ARCHIVED_DETAIL)
        project_default_override = project.get("default_config_override")
        if project_default_override:
            # asyncpg may return JSONB as a string — parse it
            if isinstance(project_default_override, str):
                project_default_override = json.loads(project_default_override)

    config_override = project_default_override
    resolved_expert_id = explicit_expert_id
    # A worker launched from an interactive thread is still a user-level
    # root job (the thread supplies scope/datasources, not a worker parent).
    # Only actual worker children/specialists carry parent_job_id.
    root_creation = not job.parent_job_id
    should_resolve_default = (
        root_creation
        and bool(effective_user_id)
        and config_name == "worker_base"
        and dependencies.experts_db_enabled()
        and await dependencies.user_experts_enabled()
    )
    should_validate_explicit = (
        bool(explicit_expert_id)
        and bool(effective_user_id)
        and dependencies.experts_db_enabled()
    )
    selection = None
    try:
        if should_resolve_default or should_validate_explicit:
            principal = scope.principal
            selection = await dependencies.resolve_worker_expert(
                user_id=str(effective_user_id),
                project_id=project_id,
                explicit_expert_id=explicit_expert_id,
                is_admin=bool((principal or {}).get("is_admin")),
            )
            resolved_expert_id = str(selection.expert["id"])
            config_name = "worker_base"
            if selection.project_override:
                config_override = deep_merge_dicts(
                    config_override or {}, selection.project_override
                )
            context["expert_selection"] = {
                "source": selection.source,
                "expert_id": resolved_expert_id,
            }
        elif (
            root_creation
            and config_name == "worker_base"
            and project
            and project.get("default_config_name")
            and not dependencies.experts_db_enabled()
        ):
            # Emergency compatibility mode only.  In normal operation the
            # typed project_experts pointer supersedes this legacy slug.
            config_name = canonical_config_name(project["default_config_name"])
    except ExpertSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DefaultExpertUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if selection is None and expert_choice.kind == "bundled":
        # Same field the DB path stamps, so "who did this dispatcher pick,
        # and did it pick at all?" has one answer whichever store the
        # expert lives in. Reading exactly this key across eight jobs is
        # how the two-path defect was diagnosed.
        context["expert_selection"] = {
            "source": "bundled",
            "expert": expert_choice.reference,
        }

    if request_config_override:
        config_override = deep_merge_dicts(
            config_override or {}, request_config_override
        )

    return JobAdmissionConfig(
        context=context,
        project_id=project_id,
        config_name=config_name,
        config_override=config_override,
        expert_id=resolved_expert_id,
        request_config_override=request_config_override,
        requested_workspace_backend=requested_workspace_backend,
        root_creation=root_creation,
    )
