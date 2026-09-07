"""Visible job list and facet queries for an already approved principal.

HTTP adapters authenticate and validate transport parameter shapes. Application
composition supplies existing access policy, narrow read operations, optional
audit access, public projection and time. The status/origin vocabularies remain
owned by persistence and are passed here rather than copied or imported with
its application-sized facade. This module neither opens stores nor owns their
lifecycles.
"""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import HTTPException


#: The existing public list cap prevents an unbounded offset scan.
JOBS_MAX_OFFSET = 50_000
#: Repeated UUIDs approach the proxy URL ceiling beyond this count.
JOBS_MAX_PROJECT_FILTERS = 40


class JobQueryPage(Protocol):
    """Structural view of the existing persistence result; no second serializer."""

    jobs: list[dict[str, Any]]
    total: int | None
    total_is_capped: bool
    has_more: bool


class QueryJobs(Protocol):
    async def __call__(
        self,
        *,
        owner_user_id: str | None,
        visible_project_ids: list[str] | None,
        scope_project_id: str | None,
        statuses: list[str] | None,
        origins: list[str] | None,
        project_ids: list[str] | None,
        has_project: bool | None,
        include_archived_projects: bool,
        search: str | None,
        as_of: datetime,
        user_id: str | None,
        limit: int,
        offset: int,
        include_total: bool,
    ) -> JobQueryPage: ...


class GetJobStatistics(Protocol):
    async def __call__(
        self,
        *,
        owner_user_id: str | None = None,
        visible_project_ids: list[str] | None = None,
        scope_project_id: str | None = None,
        origins: list[str] | None,
        project_ids: list[str] | None,
        has_project: bool | None,
        include_archived_projects: bool,
        search: str | None,
        as_of: datetime | None,
    ) -> dict[str, Any]: ...


VisibleProjectIds = Callable[
    [dict[str, Any]], Awaitable[Iterable[UUID | str] | Literal["all"]]
]
ScopeProjectId = Callable[[dict[str, Any]], UUID | None]


@dataclass(frozen=True)
class JobQueryDependencies:
    query_jobs: QueryJobs
    get_job_statistics: GetJobStatistics
    visible_project_ids: VisibleProjectIds
    scope_project_id: ScopeProjectId
    audit_available: Callable[[], bool]
    audit_counts: Callable[[list[str]], Awaitable[dict[str, int]]]
    project_job: Callable[[dict[str, Any]], dict[str, Any]]
    now: Callable[[], datetime]
    status_filter_values: tuple[str, ...]
    known_origins: frozenset[str]


@dataclass(frozen=True)
class JobProjectFilters:
    """Parsed ``?project_id=`` for the jobs list and its facet counts."""

    project_ids: list[str]
    has_project: bool | None


def parse_job_project_filters(
    *,
    project_id: list[str] | None,
    has_project: bool | None,
    is_admin: bool,
    visible_project_ids: list[str] | None,
    scope_project_id: str | None,
) -> JobProjectFilters:
    """Validate and authorize the project filter for both jobs endpoints.

    Shared so ``GET /api/jobs`` and ``GET /api/stats/jobs`` reject the same
    inputs with the same status codes. Chip counts that accepted a filter the
    list refuses (or vice versa) would disagree with each other in a way no
    test of either endpoint alone would catch.

    ``visible_project_ids`` is the caller's already-resolved membership set;
    pass ``None`` for admins, who may filter by any project in the fleet.
    """
    # 'none' is the project-less bucket. A model will guess it, so accept it
    # rather than treating it as a malformed uuid.
    raw = list(dict.fromkeys(str(value) for value in (project_id or [])))
    wants_projectless = any(value.lower() in ("none", "null") for value in raw)
    ids = sorted(value for value in raw if value.lower() not in ("none", "null"))

    if len(ids) > JOBS_MAX_PROJECT_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too many project_id values ({len(ids)}); "
                f"the maximum is {JOBS_MAX_PROJECT_FILTERS}."
            ),
        )
    for value in ids:
        try:
            UUID(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"project_id is not a uuid: {value}",
            ) from exc

    effective_has_project = has_project
    if wants_projectless and not ids:
        effective_has_project = False
    elif wants_projectless and ids:
        # "these projects OR no project" is a union the AND-composed filter
        # set cannot express; say so instead of quietly returning one arm.
        raise HTTPException(
            status_code=422,
            detail=(
                "project_id=none cannot be combined with specific project "
                "ids; issue them as separate queries."
            ),
        )

    if scope_project_id is not None and ids and ids != [str(scope_project_id)]:
        # A project-scoped MCP token cannot widen itself by asking for another
        # project. The AND-combined scope would already return zero rows;
        # saying so is clearer than an empty page.
        raise HTTPException(
            status_code=403,
            detail="Access denied by MCP token scope",
        )

    if not is_admin and ids:
        # Refuse a project the caller cannot see rather than returning an
        # empty page. This is a narrowing filter, not an authorization
        # boundary — the visibility OR-clause already bounds the result — so
        # it must NOT run for admins, who legitimately filter by any project
        # in the fleet, and it is deliberately not `require_project_member`:
        # that gate is bypassed wholesale for X-Internal-Key callers and
        # would relabel these endpoints' security classification as
        # membership-gated, which they are not.
        #
        # Known gap: a caller who owns a job in a project they are not a
        # member of cannot filter by that project id. The OR-clause still
        # shows the job in the unfiltered list.
        unauthorized = sorted(set(ids) - set(visible_project_ids or []))
        if unauthorized:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Not authorized to filter by project(s): {', '.join(unauthorized)}"
                ),
            )

    return JobProjectFilters(project_ids=ids, has_project=effective_has_project)


async def visibility_kwargs_for_stats(
    user: dict[str, Any],
    *,
    visible_project_ids: VisibleProjectIds,
    scope_project_id: ScopeProjectId,
) -> dict[str, Any]:
    """Build the visibility kwargs G5 passes through to postgres stats methods.

    Admin without an MCP project: scope → empty dict (full fleet view).
    Admin with project scope → just ``scope_project_id`` (AND-narrowed).
    Non-admin → owner_user_id + visible_project_ids (+ scope_project_id).
    """
    scope_pid = scope_project_id(user)
    if user.get("is_admin"):
        if scope_pid is None:
            return {}
        return {"scope_project_id": str(scope_pid)}
    visible = await visible_project_ids(user)
    project_ids = [str(p) for p in visible] if visible != "all" else []
    return {
        "owner_user_id": str(user["id"]),
        "visible_project_ids": project_ids,
        "scope_project_id": str(scope_pid) if scope_pid else None,
    }


async def list_jobs(
    *,
    user: dict[str, Any],
    dependencies: JobQueryDependencies,
    status: list[str] | None = None,
    origin: list[str] | None = None,
    project_id: list[str] | None = None,
    has_project: bool | None = None,
    include_archived_projects: bool = False,
    search: str | None = None,
    as_of: datetime | None = None,
    user_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_total: bool = True,
) -> dict[str, Any]:
    """Query and project a page without narrowing a non-admin's self filter."""
    is_admin = bool(user.get("is_admin"))
    scope_pid = dependencies.scope_project_id(user)

    if user_id is not None and not is_admin and str(user_id) != str(user["id"]):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to query other users' jobs",
        )

    statuses = list(dict.fromkeys(status or []))
    unknown = [
        value for value in statuses if value not in dependencies.status_filter_values
    ]
    if unknown:
        # 422 rather than an empty page: a typo that silently returns zero
        # rows gets reported as data loss.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown job status(es): {', '.join(sorted(unknown))}. "
                f"Valid values: {', '.join(dependencies.status_filter_values)}"
            ),
        )

    origins = list(dict.fromkeys(origin or []))
    unknown_origins = [
        value for value in origins if value not in dependencies.known_origins
    ]
    if unknown_origins:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown job origin(s): {', '.join(sorted(unknown_origins))}. "
                f"Valid values: {', '.join(sorted(dependencies.known_origins))}"
            ),
        )

    if offset > JOBS_MAX_OFFSET:
        raise HTTPException(
            status_code=400,
            detail=(
                f"offset exceeds the maximum of {JOBS_MAX_OFFSET}. "
                "Narrow the filters instead of paging further."
            ),
        )

    if is_admin:
        owner_user_id = None
        visible_ids = None
    else:
        visible = await dependencies.visible_project_ids(user)
        # Non-admin always lands on a concrete set (never "all").
        owner_user_id = str(user["id"])
        visible_ids = [str(p) for p in visible] if visible != "all" else []

    filters = parse_job_project_filters(
        project_id=project_id,
        has_project=has_project,
        is_admin=is_admin,
        visible_project_ids=visible_ids,
        scope_project_id=str(scope_pid) if scope_pid else None,
    )

    # Freeze the window on first page so later pages see the same set. The
    # client carries it back; a shared "page 3" link therefore shows the
    # recipient what the sender saw.
    effective_as_of = as_of or dependencies.now()
    # Emit Zulu, not "+00:00". The offset form round-trips through a properly
    # encoded query string but breaks the moment anything concatenates it
    # naively, because '+' decodes as a space and the parse then 422s. The
    # value is meant to be pasted back verbatim, so hand out the form that
    # survives that.
    as_of_wire = effective_as_of.isoformat().replace("+00:00", "Z")

    try:
        result = await dependencies.query_jobs(
            owner_user_id=owner_user_id,
            visible_project_ids=visible_ids,
            scope_project_id=str(scope_pid) if scope_pid else None,
            statuses=statuses or None,
            origins=origins or None,
            project_ids=filters.project_ids or None,
            has_project=filters.has_project,
            include_archived_projects=include_archived_projects,
            search=search or None,
            as_of=effective_as_of,
            # Admin-only cross-user filter. A non-admin's ?user_id= is
            # validated above but deliberately NOT forwarded: the OR-clause
            # already bounds them, and AND-ing it on would *narrow* the
            # result to own-jobs-only, dropping their project rows.
            user_id=user_id if is_admin else None,
            limit=limit,
            offset=offset,
            include_total=include_total,
        )
        jobs = result.jobs

        if dependencies.audit_available():
            counts = await dependencies.audit_counts([str(job["id"]) for job in jobs])
            for job in jobs:
                job["audit_count"] = counts.get(str(job["id"]), 0)
        else:
            for job in jobs:
                job["audit_count"] = None

        return {
            "jobs": [dependencies.project_job(job) for job in jobs],
            "total": result.total,
            "total_is_capped": result.total_is_capped,
            "has_more": result.has_more,
            "limit": limit,
            "offset": offset,
            "as_of": as_of_wire,
            "filters": {
                "status": statuses,
                "origin": origins,
                "project_id": filters.project_ids,
                "has_project": filters.has_project,
                "include_archived_projects": include_archived_projects,
                "search": search,
                "user_id": user_id if is_admin else None,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def get_job_statistics(
    *,
    user: dict[str, Any],
    dependencies: JobQueryDependencies,
    origin: list[str] | None = None,
    project_id: list[str] | None = None,
    has_project: bool | None = None,
    include_archived_projects: bool = False,
    search: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Count disjunctive status facets with the same non-status list filters."""
    origins = list(dict.fromkeys(origin or []))
    unknown_origins = [
        value for value in origins if value not in dependencies.known_origins
    ]
    if unknown_origins:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown job origin(s): {', '.join(sorted(unknown_origins))}. "
                f"Valid values: {', '.join(sorted(dependencies.known_origins))}"
            ),
        )
    vis = await visibility_kwargs_for_stats(
        user,
        visible_project_ids=dependencies.visible_project_ids,
        scope_project_id=dependencies.scope_project_id,
    )
    filters = parse_job_project_filters(
        project_id=project_id,
        has_project=has_project,
        is_admin=bool(user.get("is_admin")),
        visible_project_ids=vis.get("visible_project_ids"),
        scope_project_id=vis.get("scope_project_id"),
    )
    try:
        return await dependencies.get_job_statistics(
            **vis,
            origins=origins or None,
            project_ids=filters.project_ids or None,
            has_project=filters.has_project,
            include_archived_projects=include_archived_projects,
            search=search or None,
            as_of=as_of,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
