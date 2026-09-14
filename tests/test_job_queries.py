"""Query service contracts: visibility, failure order and optional enrichment.

Mounted-route and PostgreSQL tests retain the external wire and SQL contracts.
These tests exercise the new boundary using independently bound collaborators,
without importing application composition or a concrete persistence facade.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from fastapi import HTTPException
import pytest

from orchestrator.services.job_queries import (
    JOBS_MAX_OFFSET,
    JOBS_MAX_PROJECT_FILTERS,
    JobQueryDependencies,
    get_job_statistics,
    list_jobs,
    parse_job_project_filters,
    visibility_kwargs_for_stats,
)


USER = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
JOB = "44444444-4444-4444-8444-444444444444"
STAMP = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def user():
    return {"id": UUID(USER), "is_admin": False, "is_approved": True}


@pytest.fixture
def deps():
    return JobQueryDependencies(
        query_jobs=AsyncMock(
            return_value=SimpleNamespace(
                jobs=[{"id": UUID(JOB), "extension": {"nullable": None}}],
                total=None,
                total_is_capped=False,
                has_more=True,
            )
        ),
        get_job_statistics=AsyncMock(
            return_value={"total_jobs": 4, "by_status": {"extension": 2}}
        ),
        visible_project_ids=AsyncMock(return_value=[UUID(PROJECT)]),
        scope_project_id=Mock(return_value=None),
        audit_available=Mock(return_value=False),
        audit_counts=AsyncMock(return_value={}),
        project_job=Mock(side_effect=lambda row: {**row, "projected": True}),
        now=Mock(return_value=STAMP),
        status_filter_values=("processing", "failed", "blocked_undelivered"),
        known_origins=frozenset({"user", "bench"}),
    )


def test_import_does_not_load_application_or_runtime_collaborators():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_queries import list_jobs
for prefix in ('orchestrator.main', 'orchestrator.security',
               'orchestrator.database', 'agent', 'shared.runtime'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_self_filter_preserves_owner_or_project_visibility(deps, user, admin):
    user["is_admin"] = admin
    result = await list_jobs(user=user, dependencies=deps, user_id=USER)
    query = deps.query_jobs.call_args.kwargs
    assert query["owner_user_id"] == (None if admin else USER)
    assert query["visible_project_ids"] == (None if admin else [PROJECT])
    assert query["user_id"] == (USER if admin else None)
    assert result["filters"]["user_id"] == (USER if admin else None)
    assert deps.visible_project_ids.await_count == (0 if admin else 1)


@pytest.mark.asyncio
async def test_shadowed_admin_uses_effective_privilege_and_never_real_admin(deps, user):
    user["real_is_admin"] = True
    user["is_admin"] = False
    with pytest.raises(HTTPException) as error:
        await list_jobs(user=user, dependencies=deps, user_id=OTHER)
    assert error.value.status_code == 403
    deps.visible_project_ids.assert_not_awaited()
    deps.query_jobs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [[], "all"])
async def test_nonadmin_membership_sentinel_never_becomes_full_fleet(
    deps, user, visible
):
    deps.visible_project_ids.return_value = visible
    await list_jobs(user=user, dependencies=deps)
    assert deps.query_jobs.call_args.kwargs["owner_user_id"] == USER
    assert deps.query_jobs.call_args.kwargs["visible_project_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_scope_remains_an_additional_filter_and_cannot_be_widened(
    deps, user, admin
):
    user["is_admin"] = admin
    deps.scope_project_id.return_value = UUID(PROJECT)
    await list_jobs(user=user, dependencies=deps, project_id=[PROJECT])
    assert deps.query_jobs.call_args.kwargs["scope_project_id"] == PROJECT
    deps.query_jobs.reset_mock()
    with pytest.raises(HTTPException) as error:
        await list_jobs(user=user, dependencies=deps, project_id=[OTHER])
    assert error.value.status_code == 403
    assert error.value.detail == "Access denied by MCP token scope"
    deps.query_jobs.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_filters_empty_search_and_nullable_page_counts(deps, user):
    result = await list_jobs(
        user=user,
        dependencies=deps,
        status=["failed", "processing", "failed"],
        origin=["bench", "user", "bench"],
        project_id=[PROJECT, PROJECT],
        search="",
        limit=1,
        offset=JOBS_MAX_OFFSET,
        include_total=False,
    )
    query = deps.query_jobs.call_args.kwargs
    assert query["statuses"] == ["failed", "processing"]
    assert query["origins"] == ["bench", "user"]
    assert query["project_ids"] == [PROJECT]
    assert query["search"] is None
    assert query["include_total"] is False
    assert result["filters"]["search"] == ""
    assert result["total"] is None and result["total_is_capped"] is False
    assert result["has_more"] is True
    assert result["limit"] == 1 and result["offset"] == JOBS_MAX_OFFSET
    assert result["as_of"] == "2026-09-07T12:00:00Z"
    assert result["jobs"][0]["extension"] == {"nullable": None}
    assert result["jobs"][0]["projected"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp,wire",
    [
        (STAMP, "2026-09-07T12:00:00Z"),
        (STAMP.replace(tzinfo=None), "2026-09-07T12:00:00"),
        (
            STAMP.astimezone(timezone(timedelta(hours=2))),
            "2026-09-07T14:00:00+02:00",
        ),
    ],
)
async def test_explicit_watermark_is_preserved_without_reading_clock(
    deps, user, stamp, wire
):
    result = await list_jobs(user=user, dependencies=deps, as_of=stamp)
    assert result["as_of"] == wire
    assert deps.query_jobs.call_args.kwargs["as_of"] is stamp
    deps.now.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("available,count", [(False, None), (True, 0), (True, 8)])
async def test_audit_availability_and_empty_counts_stay_distinct(
    deps, user, available, count
):
    deps.audit_available.return_value = available
    deps.audit_counts.return_value = {JOB: count} if count else {}
    result = await list_jobs(user=user, dependencies=deps)
    assert result["jobs"][0]["audit_count"] == count
    assert deps.project_job.call_args.args[0]["audit_count"] == count
    if available:
        deps.audit_counts.assert_awaited_once_with([JOB])
    else:
        deps.audit_counts.assert_not_awaited()


@pytest.mark.asyncio
async def test_available_audit_still_receives_empty_page(deps, user):
    deps.query_jobs.return_value.jobs = []
    deps.audit_available.return_value = True
    result = await list_jobs(user=user, dependencies=deps)
    deps.audit_counts.assert_awaited_once_with([])
    deps.project_job.assert_not_called()
    assert result["jobs"] == []


@pytest.mark.asyncio
async def test_read_enrichment_and_projection_order_is_preserved(deps, user):
    events = Mock()
    for name in (
        "scope_project_id",
        "visible_project_ids",
        "now",
        "query_jobs",
        "audit_available",
        "audit_counts",
        "project_job",
    ):
        events.attach_mock(getattr(deps, name), name)
    deps.audit_available.return_value = True
    await list_jobs(user=user, dependencies=deps)
    assert [call[0] for call in events.mock_calls] == [
        "scope_project_id",
        "visible_project_ids",
        "now",
        "query_jobs",
        "audit_available",
        "audit_counts",
        "project_job",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters,code,fragment",
    [
        (
            {"user_id": OTHER, "status": ["typo"]},
            403,
            "other users' jobs",
        ),
        (
            {"status": ["typo"], "origin": ["typo"]},
            422,
            "Unknown job status",
        ),
        (
            {"origin": ["typo"], "offset": JOBS_MAX_OFFSET + 1},
            422,
            "Unknown job origin",
        ),
        (
            {"offset": JOBS_MAX_OFFSET + 1, "project_id": ["typo"]},
            400,
            "offset exceeds",
        ),
    ],
)
async def test_list_validation_precedence_before_membership_reads(
    deps, user, filters, code, fragment
):
    with pytest.raises(HTTPException) as error:
        await list_jobs(user=user, dependencies=deps, **filters)
    assert error.value.status_code == code
    assert fragment in error.value.detail
    deps.scope_project_id.assert_called_once_with(user)
    deps.visible_project_ids.assert_not_awaited()
    deps.now.assert_not_called()
    deps.query_jobs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [list_jobs, get_job_statistics])
async def test_membership_failure_precedes_project_validation_and_is_not_mapped(
    deps, user, operation
):
    failure = RuntimeError("membership unavailable")
    deps.visible_project_ids.side_effect = failure
    with pytest.raises(RuntimeError) as error:
        await operation(user=user, dependencies=deps, project_id=["typo"])
    assert error.value is failure
    deps.now.assert_not_called()
    deps.query_jobs.assert_not_awaited()
    deps.get_job_statistics.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_failure_precedes_list_user_filter_and_remains_unmapped(deps, user):
    failure = RuntimeError("scope unavailable")
    deps.scope_project_id.side_effect = failure
    with pytest.raises(RuntimeError) as error:
        await list_jobs(user=user, dependencies=deps, user_id=OTHER)
    assert error.value is failure
    deps.visible_project_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_clock_failure_stays_outside_query_error_mapping(deps, user):
    failure = RuntimeError("clock unavailable")
    deps.now.side_effect = failure
    with pytest.raises(RuntimeError) as error:
        await list_jobs(user=user, dependencies=deps)
    assert error.value is failure
    deps.query_jobs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "port", ["query_jobs", "audit_available", "audit_counts", "project_job"]
)
@pytest.mark.parametrize("http_error", [False, True])
async def test_query_and_enrichment_errors_keep_historical_mapping(
    deps, user, port, http_error
):
    deps.audit_available.return_value = True
    failure = (
        HTTPException(status_code=409, detail="existing conflict")
        if http_error
        else RuntimeError("synthetic read failure")
    )
    getattr(deps, port).side_effect = failure
    with pytest.raises(HTTPException) as error:
        await list_jobs(user=user, dependencies=deps)
    if http_error:
        assert error.value is failure
    else:
        assert error.value.status_code == 500
        assert error.value.detail == "synthetic read failure"
        assert error.value.__cause__ is failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,port",
    [(list_jobs, "query_jobs"), (get_job_statistics, "get_job_statistics")],
)
async def test_task_cancellation_propagates(deps, user, operation, port):
    getattr(deps, port).side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await operation(user=user, dependencies=deps)


@pytest.mark.parametrize("token", ["none", "NULL", "NoNe"])
def test_projectless_alias_overrides_explicit_has_project(token):
    result = parse_job_project_filters(
        project_id=[token],
        has_project=True,
        is_admin=False,
        visible_project_ids=[],
        scope_project_id=PROJECT,
    )
    # Scope still reaches persistence as an AND-filter; no projectless union.
    assert result.project_ids == [] and result.has_project is False


def test_project_cap_counts_distinct_specific_ids_and_checks_before_uuid_shape():
    result = parse_job_project_filters(
        project_id=[PROJECT] * (JOBS_MAX_PROJECT_FILTERS + 1),
        has_project=None,
        is_admin=True,
        visible_project_ids=None,
        scope_project_id=None,
    )
    assert result.project_ids == [PROJECT]
    with pytest.raises(HTTPException) as error:
        parse_job_project_filters(
            project_id=[str(index) for index in range(JOBS_MAX_PROJECT_FILTERS + 1)],
            has_project=None,
            is_admin=True,
            visible_project_ids=None,
            scope_project_id=None,
        )
    assert error.value.status_code == 422
    assert error.value.detail == "Too many project_id values (41); the maximum is 40."


@pytest.mark.parametrize(
    "values,scope,fragment",
    [
        (["none", "typo"], OTHER, "project_id is not a uuid: typo"),
        (["none", PROJECT], OTHER, "project_id=none cannot be combined"),
        ([PROJECT], OTHER, "Access denied by MCP token scope"),
        ([PROJECT], None, "Not authorized to filter by project(s)"),
    ],
)
def test_project_validation_error_precedence(values, scope, fragment):
    with pytest.raises(HTTPException) as error:
        parse_job_project_filters(
            project_id=values,
            has_project=None,
            is_admin=False,
            visible_project_ids=[],
            scope_project_id=scope,
        )
    assert fragment in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,scope,expected",
    [
        (True, None, {}),
        (True, PROJECT, {"scope_project_id": PROJECT}),
        (
            False,
            None,
            {
                "owner_user_id": USER,
                "visible_project_ids": [PROJECT],
                "scope_project_id": None,
            },
        ),
        (
            False,
            PROJECT,
            {
                "owner_user_id": USER,
                "visible_project_ids": [PROJECT],
                "scope_project_id": PROJECT,
            },
        ),
    ],
)
async def test_shared_stats_visibility_omits_unscoped_admin_keys(
    deps, user, admin, scope, expected
):
    user["is_admin"] = admin
    deps.scope_project_id.return_value = UUID(scope) if scope else None
    result = await visibility_kwargs_for_stats(
        user,
        visible_project_ids=deps.visible_project_ids,
        scope_project_id=deps.scope_project_id,
    )
    assert result == expected
    assert deps.visible_project_ids.await_count == (0 if admin else 1)


@pytest.mark.asyncio
async def test_facets_keep_nonstatus_filters_and_do_not_read_list_collaborators(
    deps, user
):
    result = await get_job_statistics(
        user=user,
        dependencies=deps,
        origin=["bench", "bench"],
        project_id=[PROJECT, PROJECT],
        has_project=True,
        include_archived_projects=True,
        search="",
        as_of=STAMP,
    )
    deps.get_job_statistics.assert_awaited_once_with(
        owner_user_id=USER,
        visible_project_ids=[PROJECT],
        scope_project_id=None,
        origins=["bench"],
        project_ids=[PROJECT],
        has_project=True,
        include_archived_projects=True,
        search=None,
        as_of=STAMP,
    )
    assert "status" not in inspect.signature(get_job_statistics).parameters
    assert "statuses" not in deps.get_job_statistics.call_args.kwargs
    assert result is deps.get_job_statistics.return_value
    deps.query_jobs.assert_not_awaited()
    deps.now.assert_not_called()
    deps.audit_available.assert_not_called()
    deps.audit_counts.assert_not_awaited()
    deps.project_job.assert_not_called()


@pytest.mark.asyncio
async def test_facets_do_not_synthesize_an_omitted_watermark(deps, user):
    await get_job_statistics(user=user, dependencies=deps)
    assert deps.get_job_statistics.call_args.kwargs["as_of"] is None
    deps.now.assert_not_called()


@pytest.mark.asyncio
async def test_facets_validate_origins_before_scope_or_visibility(deps, user):
    deps.scope_project_id.side_effect = RuntimeError("must not resolve scope")
    with pytest.raises(HTTPException) as error:
        await get_job_statistics(user=user, dependencies=deps, origin=["unknown"])
    assert error.value.status_code == 422
    deps.scope_project_id.assert_not_called()
    deps.visible_project_ids.assert_not_awaited()
    deps.get_job_statistics.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("http_error", [False, True])
async def test_facets_preserve_database_error_mapping(deps, user, http_error):
    failure = (
        HTTPException(status_code=503, detail="existing failure")
        if http_error
        else RuntimeError("synthetic read failure")
    )
    deps.get_job_statistics.side_effect = failure
    with pytest.raises(HTTPException) as error:
        await get_job_statistics(user=user, dependencies=deps)
    if http_error:
        assert error.value is failure
    else:
        assert error.value.status_code == 500
        assert error.value.detail == str(failure)
        assert error.value.__cause__ is failure


@pytest.mark.asyncio
async def test_parallel_bindings_keep_store_audit_projection_and_time_separate(
    deps, user
):
    other_deps = replace(
        deps,
        query_jobs=AsyncMock(
            return_value=SimpleNamespace(
                jobs=[{"id": UUID(OTHER)}],
                total=10_000,
                total_is_capped=True,
                has_more=False,
            )
        ),
        audit_available=Mock(return_value=True),
        audit_counts=AsyncMock(return_value={OTHER: 9}),
        project_job=Mock(side_effect=lambda row: {**row, "other_app": True}),
        now=Mock(return_value=STAMP + timedelta(days=1)),
    )
    first, second = await asyncio.gather(
        list_jobs(user=user, dependencies=deps),
        list_jobs(user=user, dependencies=other_deps),
    )
    assert first["jobs"][0]["id"] == UUID(JOB)
    assert first["jobs"][0]["audit_count"] is None
    assert first["jobs"][0]["projected"] is True
    assert first["as_of"] == "2026-09-07T12:00:00Z"
    assert second["jobs"] == [{"id": UUID(OTHER), "audit_count": 9, "other_app": True}]
    assert second["as_of"] == "2026-09-08T12:00:00Z"
    assert second["total"] == 10_000 and second["total_is_capped"] is True
    assert second["has_more"] is False
    deps.query_jobs.assert_awaited_once()
    other_deps.query_jobs.assert_awaited_once()
