"""Officer preparation ports preserve policy without application startup."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from orchestrator.schemas.job_create import JobCreate
from orchestrator.services.job_admission_config import JobAdmissionConfig
from orchestrator.services.job_admission_officer import (
    JobAdmissionOfficerDependencies,
    prepare_job_admission_officer,
)
from orchestrator.services.officer_admission import (
    OfficerAdmissionConflict,
    OfficerAdmissionPreparation,
    SlotAdmissionError,
)
from orchestrator.services.officer_metadata import (
    officer_meta_enabled,
    thread_officer_meta,
)

PROJECT = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"
STAMP = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return JobAdmissionConfig(
        context={
            "instructions": "Keep instructions",
            "kickoff_message": "Original brief",
        },
        project_id=PROJECT,
        config_name="worker_base",
        config_override={"llm": {"model": "project", "temperature": 0.2}},
        expert_id=None,
        request_config_override={"llm": {"model": "requested"}},
        requested_workspace_backend=None,
        root_creation=True,
    )


@pytest.fixture
def deps():
    return JobAdmissionOfficerDependencies(
        store=SimpleNamespace(
            get_thread=AsyncMock(
                return_value={
                    "metadata": {"config_override": {"officer": {"enabled": True}}}
                }
            ),
            get_project_officer_lineage=AsyncMock(return_value=[THREAD]),
        ),
        prepare_officer=AsyncMock(
            return_value=OfficerAdmissionPreparation(
                project_id=PROJECT,
                thread_id=THREAD,
                requested_slot=None,
                slot_name="researchers",
                slot_patch={"llm": {"model": "slot"}},
                category="researcher",
                config_fingerprint="snapshot",
                incarnation=1,
                owner_user_id=None,
                require_auto_pull=False,
            )
        ),
        fetch_ticket=AsyncMock(
            return_value={
                "project_id": PROJECT,
                "status": "active",
                "note_type": "feature",
                "tags": ["ready", "category:researcher"],
                "ready_at": STAMP,
            }
        ),
    )


async def prepare(config, deps, **fields):
    return await prepare_job_admission_officer(
        command=JobCreate(
            description="Officer fixture", **{"thread_id": THREAD, **fields}
        ),
        config=config,
        dependencies=deps,
    )


def test_officer_import_does_not_load_auth_database_or_application_startup():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_admission_officer import prepare_job_admission_officer
for prefix in ('orchestrator.main', 'orchestrator.security.auth',
               'orchestrator.database', 'agent', 'orchestrator.services.project_backlog'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root,thread,ticket",
    [
        (False, THREAD, None),
        (True, None, None),
        (False, THREAD, "fixture"),
        (True, None, "fixture"),
    ],
)
async def test_only_thread_origin_roots_can_prepare_officer_admission(
    config, deps, root, thread, ticket
):
    config = replace(config, root_creation=root)
    if ticket:
        with pytest.raises(HTTPException) as exc:
            await prepare(config, deps, thread_id=thread, ticket=ticket)
        assert exc.value.status_code == 409
    else:
        result = await prepare(config, deps, thread_id=thread)
        assert result.preparation is None and result.ticket_ready_at is None
        assert result.context is config.context
        assert result.config_override is config.config_override
    deps.store.get_thread.assert_not_awaited()
    deps.store.get_project_officer_lineage.assert_not_awaited()
    deps.prepare_officer.assert_not_awaited()
    deps.fetch_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_officer_without_project_is_refused_before_snapshot(config, deps):
    with pytest.raises(HTTPException) as exc:
        await prepare(replace(config, project_id=None), deps)
    assert (exc.value.status_code, exc.value.detail) == (
        409,
        "Officer dispatch requires its durable project post.",
    )
    deps.store.get_project_officer_lineage.assert_not_awaited()
    deps.prepare_officer.assert_not_awaited()
    deps.fetch_ticket.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,detail",
    [
        (SlotAdmissionError("No capacity"), "No capacity"),
        (
            OfficerAdmissionConflict(
                "slot_model_conflict", "Pinned model", slot="researchers"
            ),
            {
                "code": "slot_model_conflict",
                "message": "Pinned model",
                "slot": "researchers",
            },
        ),
    ],
)
async def test_snapshot_errors_preserve_wire_detail_and_precede_ticket(
    config, deps, error, detail
):
    deps.prepare_officer.side_effect = error
    with pytest.raises(HTTPException) as exc:
        await prepare(config, deps, ticket="fixture")
    assert (exc.value.status_code, exc.value.detail) == (409, detail)
    assert exc.value.__cause__ is error
    deps.fetch_ticket.assert_not_awaited()
    assert "officer_slot" not in config.context


@pytest.mark.asyncio
async def test_snapshot_gets_original_request_and_context_keeps_existing_category(
    config, deps, caplog
):
    config.context.update(officer_slot="researchers", work_category="tester")
    with caplog.at_level("INFO"):
        result = await prepare(config, deps, work_category="executor")
    deps.prepare_officer.assert_awaited_once_with(
        project_id=PROJECT,
        thread_id=THREAD,
        requested_slot="researchers",
        requested_config_override=config.request_config_override,
    )
    assert result.preparation is deps.prepare_officer.return_value
    assert result.context is config.context
    assert result.context["work_category"] == "tester"
    assert result.context["instructions"] == "Keep instructions"
    assert result.context["kickoff_message"].endswith("Original brief")
    assert "cross-category dispatch" in caplog.text
    assert result.config_override == {"llm": {"model": "slot", "temperature": 0.2}}
    assert config.config_override["llm"]["model"] == "project"
    assert config.request_config_override["llm"]["model"] == "requested"
    deps.fetch_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_ticket_is_refused_before_untrusted_ready_generation(
    config, deps
):
    deps.fetch_ticket.return_value.update(
        tags=["ready", "category:researcher", "category:executor"],
        ready_at=None,
    )
    with pytest.raises(HTTPException) as exc:
        await prepare(config, deps, ticket="fixture")
    assert (exc.value.status_code, exc.value.detail) == (
        409,
        "Backlog ticket 'fixture' is ambiguous: multiple category: tags (executor, researcher)",
    )


@pytest.mark.asyncio
async def test_concurrent_preparations_keep_each_dependency_and_snapshot(config, deps):
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = deps.prepare_officer.return_value

    async def blocked_snapshot(**kwargs):
        entered.set()
        await release.wait()
        return snapshot

    deps.prepare_officer.side_effect = blocked_snapshot
    other = replace(
        deps,
        prepare_officer=AsyncMock(
            side_effect=OfficerAdmissionConflict("post_missing", "Second store")
        ),
    )
    first = asyncio.create_task(prepare(config, deps, ticket="first-ticket"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(HTTPException) as exc:
            await prepare(replace(config, context={}), other, ticket="second-ticket")
        assert exc.value.detail == {"code": "post_missing", "message": "Second store"}
        deps.fetch_ticket.assert_not_awaited()
    finally:
        release.set()
        result = await asyncio.wait_for(first, 2)
    assert result.preparation is snapshot
    assert result.context["ticket_note_id"] == "first-ticket"
    deps.fetch_ticket.assert_awaited_once_with(PROJECT, "first-ticket")


@pytest.mark.parametrize(
    "metadata,expected",
    [
        (None, {}),
        ("not json", {}),
        ('{"config_override":{"officer":{"enabled":"true"}}}', {"enabled": "true"}),
        ({"config_override": {"officer": [True]}}, {}),
    ],
)
def test_metadata_reader_retains_jsonb_compatibility(metadata, expected):
    assert thread_officer_meta({"metadata": metadata}) == expected


@pytest.mark.parametrize("metadata", ["[]", {"config_override": [True]}])
def test_metadata_reader_does_not_silently_reclassify_invalid_structures(metadata):
    with pytest.raises(AttributeError):
        thread_officer_meta({"metadata": metadata})


@pytest.mark.parametrize(
    "value,enabled",
    [
        (True, True),
        ("true", True),
        ("True", True),
        (1, True),
        ("TRUE", False),
        ("1", False),
        (False, False),
    ],
)
def test_officer_enabled_values_remain_compatible(value, enabled):
    assert officer_meta_enabled({"enabled": value}) is enabled
