"""Prepare job context and authoritative scope before admission can write work.

The HTTP adapter authenticates transport/public callers and sanitizes the command
before invoking this operation. Forwarded-user authentication is a deferred port:
it must run only after context validation and only without a parent/thread origin.
No Request, credentials, application globals, pools or task lifecycle live here.
Existing HTTP exceptions remain the error compatibility contract for this slice.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

from fastapi import HTTPException

from orchestrator.schemas.job_create import JobCreate
from shared.deliverable_contract import parse_required_deliverables


_INTERNAL_JOB_SCOPE_DENIED = "Internal job origin scope is unavailable"
JobAdmissionOrigin = Literal["user_rest", "internal_rest"]


class JobScopeStore(Protocol):
    """Read-only authority needed here; the application owns the store lifecycle."""

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    async def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...


class AuthorizeUploadReference(Protocol):
    def __call__(
        self, caller: dict[str, Any] | None, upload_id: str, *, internal: bool = False
    ) -> Any: ...


@dataclass(frozen=True)
class JobAdmissionActor:
    """Server-authenticated public principal or internal forwarded identity hint.

    A forwarded ID alone is not a principal; the operation either binds it to
    an authoritative origin or calls the adapter's current-user authentication.
    """

    principal: dict[str, Any] | None = None
    forwarded_user_id: str | None = None


@dataclass(frozen=True)
class JobAdmissionScopeDependencies:
    store: JobScopeStore
    thread_project_ids: Callable[[str], Awaitable[list[str]]]
    revalidate_thread_project_ids: Callable[
        [dict[str, Any], list[str]], Awaitable[list[str]]
    ]
    authenticate_forwarded_user: Callable[
        [], Awaitable[tuple[dict[str, Any], str | None]]
    ]
    authorize_upload_reference: AuthorizeUploadReference


@dataclass(frozen=True)
class JobAdmissionScope:
    """Prepared inputs, not permission to INSERT; later admission checks still apply."""

    context: dict[str, Any]
    principal: dict[str, Any] | None
    user_id: str | None
    project_id: str | None
    origin_bound: bool


async def prepare_job_admission_scope(
    *,
    command: JobCreate,
    actor: JobAdmissionActor,
    origin: JobAdmissionOrigin,
    dependencies: JobAdmissionScopeDependencies,
) -> JobAdmissionScope:
    """Prepare context, resolve identity/project bounds, then authorize uploads.

    The caller must already have sanitized public-only delegation fields and
    validated tool overrides/readiness. Default projects, editor access, expert
    selection, grants and all persistence remain later admission stages.
    """
    if origin not in ("user_rest", "internal_rest"):
        raise ValueError(f"Unknown job admission origin: {origin}")
    job = command
    # Merge upload IDs into context
    context = dict(job.context) if job.context else {}
    if job.upload_id:
        context["upload_id"] = job.upload_id
    if job.config_upload_id:
        context["config_upload_id"] = job.config_upload_id
    if job.instructions_upload_id:
        context["instructions_upload_id"] = job.instructions_upload_id
    if job.instructions:
        context["instructions"] = job.instructions
    if job.kickoff_message:
        context["kickoff_message"] = job.kickoff_message
    if job.required_deliverables:
        # Deliverable contract (P1-C): normalize + dedupe into context.
        # The dispatcher forwards context to the agent's task brief and the
        # completion gate validates the seal against committed Gitea state.
        manifest = parse_required_deliverables(job.required_deliverables, strict=True)
        if manifest:
            context["required_deliverables"] = manifest

    if origin == "internal_rest":
        principal, user_id, project_id, origin_bound = await _resolve_internal_scope(
            actor, job, dependencies
        )
    else:
        if actor.principal is None:
            raise ValueError("Public job admission requires an authenticated principal")
        principal = actor.principal
        user_id = str(principal["id"])
        project_id = str(job.project_id) if job.project_id else None
        origin_bound = False

    # Authorize the merged references before any default-project/expert lookup,
    # persistence or dispatch. Only an authoritative ownerless child is exempt.
    for upload_key in ("upload_id", "config_upload_id", "instructions_upload_id"):
        referenced_upload = context.get(upload_key)
        if referenced_upload:
            dependencies.authorize_upload_reference(
                principal,
                str(referenced_upload),
                internal=origin == "internal_rest" and principal is None,
            )
    return JobAdmissionScope(context, principal, user_id, project_id, origin_bound)


async def _resolve_internal_scope(
    actor: JobAdmissionActor,
    job: JobCreate,
    dependencies: JobAdmissionScopeDependencies,
) -> tuple[dict[str, Any] | None, str | None, str | None, bool]:
    """Derive an internal job's user/project scope from authoritative context.

    ``X-Internal-Key`` authenticates the transport, not the user/project values
    in a request body. Agent-created jobs therefore inherit identity and allowed
    projects from ``thread_id`` and/or ``parent_job_id``. A valid MCP forwarded
    user header is the remaining user-authenticated internal path. Originless
    internal HTTP calls are rejected: the shared key reaches agent pods, so it
    cannot establish a privileged "system job" identity. A userless child is
    valid only when derived from an authoritative userless parent/thread.

    Returns ``(principal, user_id, project_id, origin_bound)``. ``origin_bound``
    suppresses the normal user-default-project fallback: an unscoped parent or
    thread must not silently widen into its owner's unrelated default project.
    """

    def denied() -> HTTPException:
        return HTTPException(status_code=403, detail=_INTERNAL_JOB_SCOPE_DENIED)

    thread: dict[str, Any] | None = None
    parent: dict[str, Any] | None = None
    thread_projects: list[str] = []

    if job.thread_id:
        try:
            thread = await dependencies.store.get_thread(str(job.thread_id))
        except Exception as exc:
            raise denied() from exc
        if thread is None:
            raise denied()
        try:
            thread_projects = await dependencies.thread_project_ids(str(job.thread_id))
            column_project = thread.get("project_id")
            if column_project and str(column_project) not in thread_projects:
                thread_projects.insert(0, str(column_project))
            # Acknowledged-but-still-unavailable project drift is narrowed
            # out INSIDE _revalidate_thread_project_ids now, same as the
            # warm-attach and cold-workspace call sites, so an
            # already-acknowledged revoked/deleted project does not
            # spuriously 403 an internal job scoped off this thread — while a
            # RECOVERED acknowledged project returns automatically.
            thread_projects = await dependencies.revalidate_thread_project_ids(
                thread, thread_projects
            )
        except HTTPException as exc:
            # An archived project is the one refusal worth naming here. The
            # generic ``denied()`` exists so a caller cannot learn WHY internal
            # scope resolution failed, but the archived 409 only ever reaches a
            # caller who is already a member of that project, so it discloses
            # nothing — and the agent on the other end can act on "unarchive
            # it" where "scope is unavailable" leaves it guessing.
            if exc.status_code == 409:
                raise
            raise denied() from exc
        except Exception as exc:
            raise denied() from exc

    if job.parent_job_id:
        try:
            parent = await dependencies.store.get_job(str(job.parent_job_id))
        except Exception as exc:
            raise denied() from exc
        if parent is None:
            raise denied()

    if thread is not None or parent is not None:
        thread_user_id = (
            str(thread["user_id"]) if thread and thread.get("user_id") else None
        )
        parent_user_id = (
            str(parent["user_id"]) if parent and parent.get("user_id") else None
        )
        if thread is not None and parent is not None:
            if thread_user_id != parent_user_id:
                raise denied()
        origin_user_id = parent_user_id if parent is not None else thread_user_id

        forwarded_user_id = actor.forwarded_user_id
        if forwarded_user_id and str(forwarded_user_id) != str(origin_user_id or ""):
            raise denied()
        if job.user_id and str(job.user_id) != str(origin_user_id or ""):
            raise denied()

        if parent is not None:
            parent_project = (
                str(parent["project_id"]) if parent.get("project_id") else None
            )
            allowed_projects = {parent_project} if parent_project else set()
            if thread is not None and parent_project not in set(thread_projects):
                # Includes an unscoped parent paired with a project-scoped
                # thread: the parent remains the stricter authority.
                if parent_project is not None or thread_projects:
                    raise denied()
            default_project = parent_project
        else:
            allowed_projects = set(thread_projects)
            column_project = thread.get("project_id") if thread else None
            default_project = (
                str(column_project)
                if column_project and str(column_project) in allowed_projects
                else (thread_projects[0] if thread_projects else None)
            )

        requested_project = str(job.project_id) if job.project_id else None
        if requested_project and requested_project not in allowed_projects:
            raise denied()
        effective_project = requested_project or default_project

        principal = None
        if origin_user_id:
            principal = await dependencies.store.get_user(origin_user_id)
            if principal is None:
                raise denied()
        return principal, origin_user_id, effective_project, True

    # MCP forwards a separately authenticated user identity. Resolve it through
    # the normal admission path; a bare internal key plus a body user_id is not
    # equivalent and is rejected below.
    forwarded_user_id = actor.forwarded_user_id
    if forwarded_user_id:
        principal, scoped_project = await dependencies.authenticate_forwarded_user()
        principal_id = str(principal["id"])
        if job.user_id and str(job.user_id) != principal_id:
            raise denied()
        requested_project = str(job.project_id) if job.project_id else None
        if scoped_project is not None:
            scoped_project_id = str(scoped_project)
            if requested_project and requested_project != scoped_project_id:
                raise denied()
            requested_project = requested_project or scoped_project_id
        return principal, principal_id, requested_project, False

    raise denied()
