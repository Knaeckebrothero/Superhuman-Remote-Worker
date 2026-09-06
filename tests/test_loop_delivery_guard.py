"""The loop's delivery guard: did this execution turn land anything?

knowledge-base/knowledge/features/better_resavio_restart_status.md §6a.

The 2026-08-06 Better Resavio run finished 12 jobs with zero failures and
delivered nothing, because delivery was measured on one path only. The
replacement asks whether ANY path delivered. Two questions it deliberately
does not ask:

* "Did the project cloud folder change?" — for a project whose code
  compounds into a source repository the honest answer is *no*, forever.
* "Did ``main`` move?" — review-based delivery leaves ``main`` alone on
  purpose. Job 29c28492 shipped 1,348 reviewed lines and moved ``main`` not
  at all.

These call the real hook rather than asserting on its source, so a revert
that keeps the shape but drops the behaviour still fails.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

LOOP_ID = "105a6f98-134c-4077-b7e1-6d08916650d7"

PR_RECORD = {
    "forge": "github",
    "repo": "Knaeckebrothero/KurortEngine",
    "number": 1,
    "url": "https://github.com/Knaeckebrothero/KurortEngine/pull/1",
    "head": "design/hotel-rheinland-theme",
    "base": "main",
}


def _job(*, context: object | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": "completed",
        "repo_name": "job-29c28492",
        "branch_name": "job/29c28492",
        "project_id": None,
        "freeze_data": {"notes": "Shipped the theme studies."},
        "context": {} if context is None else context,
    }


def _ctx(role: str = "developer") -> dict:
    return {"loop_id": LOOP_ID, "loop_role": role, "loop_iteration": 3}


async def _run(job: dict, *, role: str = "developer") -> list[str]:
    """Invoke the real hook and return the loop-visible action lines."""
    from orchestrator.main import _record_loop_job_outcome

    db = MagicMock()
    db.create_job_change_record = AsyncMock(return_value=True)
    actions: list[str] = []
    with (
        patch("orchestrator.main.postgres_db", db),
        patch("orchestrator.main.vector_db", None),
    ):
        await _record_loop_job_outcome(
            job,
            ctx=_ctx(role),
            loop={"id": LOOP_ID, "project_id": None},
            loop_id=LOOP_ID,
            actions=actions,
            failed=False,
            last_error=None,
        )
    return actions


def _delivery_lines(actions: list[str]) -> list[str]:
    return [a for a in actions if "delivered" in a]


@pytest.mark.asyncio
async def test_no_cloud_changes_and_no_pull_request_is_flagged() -> None:
    actions = await _run(_job())
    assert any("delivered nothing" in a for a in actions)


@pytest.mark.asyncio
async def test_a_pull_request_is_not_flagged_as_empty() -> None:
    """The regression this guard exists to prevent."""
    actions = await _run(_job(context={"pull_request": PR_RECORD}))
    assert not any("delivered nothing" in a for a in actions)


@pytest.mark.asyncio
async def test_a_pull_request_is_reported_as_delivery() -> None:
    actions = await _run(_job(context={"pull_request": PR_RECORD}))
    line = next(iter(_delivery_lines(actions)), "")
    assert "Knaeckebrothero/KurortEngine" in line
    assert "#1" in line
    assert "design/hotel-rheinland-theme" in line


@pytest.mark.asyncio
async def test_jsonb_string_context_still_counts_as_delivery() -> None:
    """asyncpg hands ``context`` back as text; the guard must not misread it."""
    import json

    actions = await _run(_job(context=json.dumps({"pull_request": PR_RECORD})))
    assert not any("delivered nothing" in a for a in actions)


@pytest.mark.asyncio
async def test_agent_prose_does_not_buy_a_clean_bill() -> None:
    """A claim under the same key must not suppress the alarm."""
    actions = await _run(
        _job(context={"pull_request": "opened https://github.com/x/y/pull/9"})
    )
    assert any("delivered nothing" in a for a in actions)


@pytest.mark.asyncio
async def test_analysis_roles_are_not_expected_to_deliver_files() -> None:
    actions = await _run(_job(), role="scholar")
    assert _delivery_lines(actions) == []
