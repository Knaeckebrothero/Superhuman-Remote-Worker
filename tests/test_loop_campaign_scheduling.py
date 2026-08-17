"""Unit tests for loop campaign scheduling — the Critic as planner (P0).

Covers the three layers knowledge-base/knowledge/features/loop_campaign_scheduling.md ships dark:

1. **Validation** (services/project_loops.py): planner template grammar
   (``planner_slots``), cap overrides (``validate_campaign_caps`` /
   ``resolve_campaign_caps``), and the plan validator's rejection matrix
   (shape, caps, budget reserve, disposition/extend rules).
2. **Advance path** (main.py): plan application → campaign + first member;
   member completion → next member / review / abort; stale members; the
   K=1 rotation fallback when no plan is filed; and — the tear-window
   proofs — idempotent re-runs of the same advance after a lost write-back
   (the sweeper heals by re-point-and-re-advance, so idempotency here IS the
   campaign recovery story; no sweeper code changed).
3. **Intake endpoint** (``file_loop_plan``): checkpoint-only gating chain and
   the best-effort KB existence check.

Rotation-mode loops are regression-gated by the existing suites
(tests/test_project_loops.py, tests/test_project_loop_sweeper.py) passing
unmodified; here we additionally pin that a rotation loop never enters the
planner branch.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.project_loops import (
    LOOP_CAMPAIGN_CAPS_CEILING,
    LOOP_CAMPAIGN_DEFAULT_CAPS,
    LOOP_CAMPAIGN_HISTORY_LIMIT,
    create_loop_job,
    planner_slots,
    resolve_campaign_caps,
    validate_campaign_caps,
    validate_loop_plan,
)

LOOP_ID = "105a6f98-134c-4077-b7e1-6d08916650d7"
CRITIC_JOB_ID = "aaaaaaaa-0000-0000-0000-000000000001"
PLANNER_ROLES = [["scholar", "product-qa"], "critic", "developer"]


def _loop(**over) -> dict:
    base = {
        "id": LOOP_ID,
        "status": "running",
        "scheduling": "campaign",
        "role_sequence": [["scholar", "product-qa"], "critic", "developer"],
        "seq_index": 1,  # the critic slot just completed
        "total_jobs_run": 10,
        "max_iterations": 30,
        "remaining_iterations": 20,
        "current_job_id": CRITIC_JOB_ID,
        "current_stage_jobs": [CRITIC_JOB_ID],
        "campaign": None,
        "campaign_history": [],
        "campaign_caps": None,
        "goal": "Build a thing",
    }
    base.update(over)
    return base


def _plan(**over) -> dict:
    base = {
        "initiative": {"kb_note_id": "qa-finding-f5", "title": "F5 receptionist"},
        "stages": [{"role": "developer"}, {"role": "developer"}],
        "acceptance": ["pytest -x"],
    }
    base.update(over)
    return base


def _campaign(**over) -> dict:
    base = {
        "id": CRITIC_JOB_ID,
        "plan_job_id": CRITIC_JOB_ID,
        "initiative_note_id": "qa-finding-f5",
        "title": "F5 receptionist",
        "stages": [{"role": "developer"}, {"role": "developer"}, {"role": "bughunter"}],
        "acceptance": ["pytest -x"],
        "cursor": 1,
        "stages_done": 0,
        "member_failures": 0,
        "extensions_used": 0,
        "status": "active",
    }
    base.update(over)
    return base


def _member_job(campaign_id: str, index: int, *, status: str = "completed") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": status,
        "context": {
            "loop_id": LOOP_ID,
            "loop_role": "developer",
            "loop_iteration": 11,
            "loop_seq_index": 2,
            "loop_remaining": 19,
            "loop_campaign_id": campaign_id,
            "loop_campaign_index": index,
        },
    }


# =============================================================================
# 1. Validation layer
# =============================================================================


class TestPlannerSlots:
    def test_canonical_template(self):
        assert planner_slots(PLANNER_ROLES) == (1, 2)

    def test_critic_last_wraps_to_slot_zero(self):
        assert planner_slots(["developer", "scholar", "critic"]) == (2, 0)

    def test_no_critic_rejected(self):
        with pytest.raises(ValueError, match="exactly one 'critic'"):
            planner_slots(["scholar", "developer"])

    def test_two_critics_rejected(self):
        with pytest.raises(ValueError, match="exactly one 'critic'"):
            planner_slots(["critic", "developer", "critic"])

    def test_critic_inside_fanout_rejected(self):
        with pytest.raises(ValueError, match="single-role checkpoint"):
            planner_slots([["scholar", "critic"], "developer"])

    def test_lone_critic_rejected(self):
        with pytest.raises(ValueError, match="at least one non-critic stage"):
            planner_slots(["critic"])

    def test_fanout_execution_slot_rejected(self):
        with pytest.raises(ValueError, match="single-role"):
            planner_slots(["critic", ["scholar", "product-qa"], "developer"])


class TestCampaignCaps:
    def test_override_accepted(self):
        assert validate_campaign_caps({"max_stages": 8}) == {"max_stages": 8}

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown campaign_caps"):
            validate_campaign_caps({"max_jobs": 3})

    def test_above_ceiling_rejected_not_clamped(self):
        ceiling = LOOP_CAMPAIGN_CAPS_CEILING["max_stages"]
        with pytest.raises(ValueError, match=f"ceiling {ceiling}"):
            validate_campaign_caps({"max_stages": ceiling + 1})

    def test_zero_and_bool_rejected(self):
        with pytest.raises(ValueError, match="integer >= 1"):
            validate_campaign_caps({"max_extensions": 0})
        with pytest.raises(ValueError, match="integer >= 1"):
            validate_campaign_caps({"max_extensions": True})

    def test_resolve_defaults_when_unset(self):
        assert resolve_campaign_caps(_loop()) == LOOP_CAMPAIGN_DEFAULT_CAPS

    def test_resolve_merges_and_reclamps_hand_edited_rows(self):
        # A hand-edited row above the ceiling must still be clamped — the caps
        # gate spawning, so the runaway floor holds even past the API.
        caps = resolve_campaign_caps(_loop(campaign_caps={"max_stages": 99}))
        assert caps["max_stages"] == LOOP_CAMPAIGN_CAPS_CEILING["max_stages"]
        assert caps["max_extensions"] == LOOP_CAMPAIGN_DEFAULT_CAPS["max_extensions"]


class TestValidateLoopPlan:
    def test_happy_path_normalizes(self):
        out = validate_loop_plan(
            {
                "initiative": {"kb_note_id": " qa-finding-f5 ", "title": "F5"},
                "stages": ["developer", {"role": "bughunter"}],
                "acceptance": ["  pytest -x  ", ""],
            },
            _loop(),
        )
        assert out["initiative"]["kb_note_id"] == "qa-finding-f5"
        assert out["stages"] == [{"role": "developer"}, {"role": "bughunter"}]
        assert out["acceptance"] == ["pytest -x"]
        assert out["disposition"] is None

    def test_non_dict_and_missing_initiative_rejected(self):
        with pytest.raises(ValueError, match="JSON object"):
            validate_loop_plan([], _loop())
        with pytest.raises(ValueError, match="kb_note_id"):
            validate_loop_plan({"stages": ["developer"]}, _loop())

    def test_empty_and_oversized_stage_lists_rejected(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_loop_plan(_plan(stages=[]), _loop())
        too_many = ["developer"] * (LOOP_CAMPAIGN_DEFAULT_CAPS["max_stages"] + 1)
        with pytest.raises(ValueError, match="cap is"):
            validate_loop_plan(_plan(stages=too_many), _loop())

    def test_per_loop_cap_override_raises_the_limit(self):
        seven = ["developer"] * 7
        loop = _loop(campaign_caps={"max_stages": 7})
        assert len(validate_loop_plan(_plan(stages=seven), loop)["stages"]) == 7

    def test_budget_reserve_enforced(self):
        # remaining=4 → affordable = 4 - RESERVE(2) = 2.
        loop = _loop(remaining_iterations=4)
        assert validate_loop_plan(_plan(stages=["developer"] * 2), loop)
        with pytest.raises(ValueError, match="reserve"):
            validate_loop_plan(_plan(stages=["developer"] * 3), loop)

    def test_deadline_only_loops_have_no_budget_constraint(self):
        loop = _loop(remaining_iterations=None)
        assert validate_loop_plan(_plan(stages=["developer"] * 5), loop)

    def test_disposition_required_when_campaign_in_review(self):
        loop = _loop(campaign=_campaign(status="review"))
        with pytest.raises(ValueError, match="must be disposed first"):
            validate_loop_plan(_plan(), loop)

    def test_disposition_without_pending_campaign_rejected(self):
        plan = _plan(disposition={"outcome": "ship"})
        with pytest.raises(ValueError, match="no campaign is awaiting review"):
            validate_loop_plan(plan, _loop())

    def test_bad_outcome_rejected(self):
        loop = _loop(campaign=_campaign(status="review"))
        plan = _plan(disposition={"outcome": "party"})
        with pytest.raises(ValueError, match="outcome must be one of"):
            validate_loop_plan(plan, loop)

    def test_extend_caps_and_initiative_continuity(self):
        maxed = _campaign(
            status="review",
            extensions_used=LOOP_CAMPAIGN_DEFAULT_CAPS["max_extensions"],
        )
        plan = _plan(disposition={"outcome": "extend"})
        with pytest.raises(ValueError, match="extension"):
            validate_loop_plan(plan, _loop(campaign=maxed))

        switcher = _plan(
            initiative={"kb_note_id": "something-else"},
            disposition={"outcome": "extend"},
        )
        with pytest.raises(ValueError, match="same initiative"):
            validate_loop_plan(switcher, _loop(campaign=_campaign(status="review")))

        ok = validate_loop_plan(
            _plan(disposition={"outcome": "extend"}),
            _loop(campaign=_campaign(status="review")),
        )
        assert ok["disposition"]["outcome"] == "extend"

    def test_dispose_only_ship_and_kill_accepted(self):
        # Closing without opening: a bare disposition (no initiative, no
        # stages) is a legal filing while a campaign awaits review.
        loop = _loop(campaign=_campaign(status="review"))
        for outcome in ("ship", "kill"):
            out = validate_loop_plan(
                {"disposition": {"outcome": outcome, "notes": "  checked  "}}, loop
            )
            assert out["initiative"] is None
            assert out["stages"] == []
            assert out["acceptance"] == []
            assert out["disposition"] == {"outcome": outcome, "notes": "checked"}

    def test_dispose_only_extend_rejected(self):
        loop = _loop(campaign=_campaign(status="review"))
        with pytest.raises(ValueError, match="extend requires stages"):
            validate_loop_plan({"disposition": {"outcome": "extend"}}, loop)

    def test_dispose_only_without_pending_campaign_rejected(self):
        with pytest.raises(ValueError, match="no campaign is awaiting review"):
            validate_loop_plan({"disposition": {"outcome": "ship"}}, _loop())


class TestCampaignStamps:
    @pytest.mark.asyncio
    async def test_extra_context_stamps_land(self):
        db = AsyncMock()
        db.create_job = AsyncMock(return_value={"id": "job-1"})
        db.list_experts_visible = AsyncMock(return_value=[])
        db.list_project_datasources = AsyncMock(return_value=[])
        await create_loop_job(
            db,
            {"id": LOOP_ID, "project_id": None, "owner_id": None, "goal": "g"},
            role="developer",
            iteration=11,
            seq_index=2,
            remaining_iterations=19,
            extra_context={"loop_campaign_id": "c-1", "loop_campaign_index": 0},
        )
        ctx = db.create_job.call_args.kwargs["context"]
        assert ctx["loop_campaign_id"] == "c-1"
        assert ctx["loop_campaign_index"] == 0

    @pytest.mark.asyncio
    async def test_extra_context_cannot_shadow_reserved_keys(self):
        db = AsyncMock()
        db.create_job = AsyncMock(return_value={"id": "job-1"})
        db.list_experts_visible = AsyncMock(return_value=[])
        db.list_project_datasources = AsyncMock(return_value=[])
        await create_loop_job(
            db,
            {"id": LOOP_ID, "project_id": None, "owner_id": None, "goal": "g"},
            role="developer",
            iteration=11,
            seq_index=2,
            remaining_iterations=19,
            extra_context={"loop_id": "EVIL", "loop_seq_index": 99},
        )
        ctx = db.create_job.call_args.kwargs["context"]
        assert ctx["loop_id"] == LOOP_ID
        assert ctx["loop_seq_index"] == 2


# =============================================================================
# 2. Advance path (main.py) — plan application, member steps, tear re-runs
# =============================================================================


def _critic_job(plan: dict | None, *, seq_index: int = 1) -> dict:
    ctx = {
        "loop_id": LOOP_ID,
        "loop_role": "critic",
        "loop_iteration": 10,
        "loop_seq_index": seq_index,
        "loop_remaining": 20,
    }
    if plan is not None:
        ctx["loop_plan"] = plan
    return {"id": CRITIC_JOB_ID, "status": "completed", "context": ctx}


def _patched_main(db: AsyncMock, spawn: AsyncMock):
    stack = ExitStack()
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(patch("main._spawn_loop_stage", spawn))
    return stack


def _spawn_mock(job_id: str = "bbbbbbbb-0000-0000-0000-000000000001"):
    return AsyncMock(return_value=([{"id": job_id}], 11))


@pytest.mark.asyncio
async def test_rotation_loop_never_enters_planner_branch():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    exploding = MagicMock(side_effect=AssertionError("planner branch entered"))
    with _patched_main(db, spawn):
        with patch("main._advance_planner_campaign", exploding):
            await _rotate_loop_to_next_stage(
                _loop(scheduling="standard"),
                seq_index_completed=1,
                base_total=10,
                next_remaining=19,
                consecutive=0,
                last_error=None,
                actions=[],
                completed_job=_critic_job(_plan()),
                completed_ctx=_critic_job(_plan())["context"],
                completed_failed=False,
            )
    spawn.assert_awaited_once()
    assert spawn.call_args.kwargs["stage"] == "developer"  # plain rotation
    # And the write-back never touches the campaign column.
    assert "campaign" not in db.update_project_loop.call_args.kwargs


@pytest.mark.asyncio
async def test_planner_critic_without_plan_falls_back_to_rotation():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(),
            seq_index_completed=1,
            base_total=10,
            next_remaining=19,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(None),
            completed_ctx=_critic_job(None)["context"],
            completed_failed=False,
        )
    spawn.assert_awaited_once()
    assert spawn.call_args.kwargs["stage"] == "developer"
    assert spawn.call_args.kwargs.get("extra_context") is None


@pytest.mark.asyncio
async def test_plan_application_writes_campaign_then_spawns_stamped_member():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    actions: list[str] = []
    plan = _plan(stages=["developer", "developer", "bughunter"])
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(),
            seq_index_completed=1,
            base_total=10,
            next_remaining=19,
            consecutive=0,
            last_error=None,
            actions=actions,
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    # Campaign persisted BEFORE the spawn (own write, plan_job_id idempotency
    # anchor), then the member spawn stamped with campaign id + index 0.
    pre_spawn = db.update_project_loop.call_args_list[0]
    campaign = pre_spawn.kwargs["campaign"]
    assert campaign["plan_job_id"] == CRITIC_JOB_ID
    assert campaign["cursor"] == 0
    assert campaign["status"] == "active"
    assert [s["role"] for s in campaign["stages"]] == [
        "developer",
        "developer",
        "bughunter",
    ]
    spawn.assert_awaited_once()
    kw = spawn.call_args.kwargs
    assert kw["stage"] == "developer"
    assert kw["seq_index"] == 2  # the execution slot, not a rotation successor
    assert kw["extra_context"] == {
        "loop_campaign_id": CRITIC_JOB_ID,
        "loop_campaign_index": 0,
    }
    # The pointer write-back carries the post-spawn cursor.
    post_spawn = db.update_project_loop.call_args_list[-1]
    assert post_spawn.kwargs["campaign"]["cursor"] == 1
    assert any("campaign" in a for a in actions)


@pytest.mark.asyncio
async def test_member_success_spawns_next_stage_from_stamp():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign()  # 3 stages, cursor=1
    member = _member_job(camp["id"], 0)
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=11,
            next_remaining=18,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=False,
        )
    kw = spawn.call_args.kwargs
    assert kw["stage"] == "developer"
    assert kw["seq_index"] == 2
    assert kw["extra_context"]["loop_campaign_index"] == 1
    wb = db.update_project_loop.call_args_list[-1].kwargs
    assert wb["campaign"]["cursor"] == 2
    assert wb["campaign"]["stages_done"] == 1


@pytest.mark.asyncio
async def test_member_stamp_beats_stale_cursor_after_lost_writeback():
    """Tear window: member spawned but its write-back lost (cursor stale at the
    member's own index). The next-stage derivation must ride the completed
    member's stamp, not the row cursor — no double-spawn of the same stage."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(cursor=0)  # stale: write-back for member 0's spawn lost
    member = _member_job(camp["id"], 0)
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=11,
            next_remaining=18,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=False,
        )
    assert spawn.call_args.kwargs["extra_context"]["loop_campaign_index"] == 1


@pytest.mark.asyncio
async def test_last_member_flips_campaign_to_review_and_rotates():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(cursor=3, stages_done=2)
    member = _member_job(camp["id"], 2)  # last of 3
    actions: list[str] = []
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=13,
            next_remaining=16,
            consecutive=0,
            last_error=None,
            actions=actions,
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=False,
        )
    # Rotation resumed: seq 2 → wraps to the analysis fan-out at index 0…
    kw = spawn.call_args.kwargs
    assert kw["stage"] == ["scholar", "product-qa"]
    assert kw["seq_index"] == 0
    # …and the review flip rides the SAME write-back as the new stage pointer.
    wb = db.update_project_loop.call_args_list[-1].kwargs
    assert wb["campaign"]["status"] == "review"
    assert wb["campaign"]["stages_done"] == 3
    assert any("awaiting critic review" in a for a in actions)


@pytest.mark.asyncio
async def test_member_failure_below_threshold_continues_campaign():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign()  # member_failures=0, abort at 2
    member = _member_job(camp["id"], 0, status="failed")
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=11,
            next_remaining=18,
            consecutive=1,
            last_error="job failed",
            actions=[],
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=True,
        )
    kw = spawn.call_args.kwargs
    assert kw["extra_context"]["loop_campaign_index"] == 1
    wb = db.update_project_loop.call_args_list[-1].kwargs
    assert wb["campaign"]["member_failures"] == 1


@pytest.mark.asyncio
async def test_consecutive_member_failures_abort_campaign():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(member_failures=1)  # one more failure trips abort (2)
    member = _member_job(camp["id"], 1, status="failed")
    actions: list[str] = []
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=12,
            next_remaining=17,
            consecutive=2,
            last_error="job failed",
            actions=actions,
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=True,
        )
    # Queue flushed by rotation-fallthrough: the next spawn is the analysis
    # stage, not stage 2 of the campaign.
    kw = spawn.call_args.kwargs
    assert kw["stage"] == ["scholar", "product-qa"]
    wb = db.update_project_loop.call_args_list[-1].kwargs
    assert wb["campaign"]["status"] == "aborted"
    assert any("ABORTED" in a for a in actions)


@pytest.mark.asyncio
async def test_success_resets_member_failure_streak():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(member_failures=1)
    member = _member_job(camp["id"], 0, status="completed")
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
            seq_index_completed=2,
            base_total=11,
            next_remaining=18,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=member,
            completed_ctx=member["context"],
            completed_failed=False,
        )
    wb = db.update_project_loop.call_args_list[-1].kwargs
    assert wb["campaign"]["member_failures"] == 0


@pytest.mark.asyncio
async def test_stale_member_of_disposed_campaign_rotates_plainly():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    current = _campaign(id="ffffffff-0000-0000-0000-00000000000f")
    stale_member = _member_job("some-old-campaign", 0)
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=current, seq_index=2, current_job_id=stale_member["id"]),
            seq_index_completed=2,
            base_total=11,
            next_remaining=18,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=stale_member,
            completed_ctx=stale_member["context"],
            completed_failed=False,
        )
    assert spawn.call_args.kwargs["stage"] == ["scholar", "product-qa"]
    assert "campaign" not in db.update_project_loop.call_args_list[-1].kwargs


@pytest.mark.asyncio
async def test_healed_rerun_of_applied_plan_resumes_at_cursor():
    """Tear window: campaign written, first spawn lost. The heal re-points at
    the critic and re-advances; the plan_job_id guard must resume spawning at
    the persisted cursor instead of re-applying the plan (no duplicate
    campaign, no duplicate history entry)."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(cursor=0)  # written, nothing spawned yet
    plan = _plan(stages=["developer", "developer", "bughunter"])
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp),
            seq_index_completed=1,
            base_total=10,
            next_remaining=19,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    # Exactly one loop-row write: the spawn write-back. No fresh campaign
    # insert, no history append — the plan was NOT re-applied.
    assert len(db.update_project_loop.call_args_list) == 1
    wb = db.update_project_loop.call_args_list[0].kwargs
    assert wb["campaign"]["cursor"] == 1
    assert spawn.call_args.kwargs["extra_context"]["loop_campaign_index"] == 0


@pytest.mark.asyncio
async def test_healed_rerun_with_fully_spawned_campaign_rotates():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    camp = _campaign(cursor=3)  # everything already spawned
    plan = _plan(stages=["developer", "developer", "bughunter"])
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=camp),
            seq_index_completed=1,
            base_total=10,
            next_remaining=19,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    assert spawn.call_args.kwargs["stage"] == "developer"  # plain rotation


@pytest.mark.asyncio
async def test_disposition_archives_to_history_and_extend_carries_counter():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
    reviewed = _campaign(
        status="review",
        stages_done=3,
        extensions_used=0,
        id=prior_critic,
        plan_job_id=prior_critic,  # planned by the PREVIOUS critic, not this one
    )
    plan = _plan(
        stages=["developer"],
        disposition={"outcome": "extend", "notes": "one more push"},
    )
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=reviewed),
            seq_index_completed=1,
            base_total=13,
            next_remaining=16,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    pre_spawn = db.update_project_loop.call_args_list[0].kwargs
    history = pre_spawn["campaign_history"]
    assert len(history) == 1
    assert history[0]["outcome"] == "extend"
    assert history[0]["disposed_by"] == CRITIC_JOB_ID
    assert pre_spawn["campaign"]["extensions_used"] == 1
    assert pre_spawn["campaign"]["status"] == "active"


@pytest.mark.asyncio
async def test_history_is_capped():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    old = [{"id": f"old-{i}"} for i in range(LOOP_CAMPAIGN_HISTORY_LIMIT)]
    prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
    reviewed = _campaign(status="review", id=prior_critic, plan_job_id=prior_critic)
    plan = _plan(disposition={"outcome": "ship"})
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=reviewed, campaign_history=old),
            seq_index_completed=1,
            base_total=13,
            next_remaining=16,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    history = db.update_project_loop.call_args_list[0].kwargs["campaign_history"]
    assert len(history) == LOOP_CAMPAIGN_HISTORY_LIMIT
    assert history[0]["id"] == "old-1"  # oldest dropped
    assert history[-1]["disposed_by"] == CRITIC_JOB_ID


@pytest.mark.asyncio
async def test_dispose_only_plan_closes_campaign_and_rotates():
    """Ship/kill without a successor: history written, campaign cleared in the
    same pre-spawn write, then plain K=1 rotation — no new campaign opened."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    actions: list[str] = []
    prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
    reviewed = _campaign(
        status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
    )
    plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=reviewed),
            seq_index_completed=1,
            base_total=13,
            next_remaining=16,
            consecutive=0,
            last_error=None,
            actions=actions,
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    pre_spawn = db.update_project_loop.call_args_list[0].kwargs
    assert pre_spawn["campaign"] is None
    history = pre_spawn["campaign_history"]
    assert history[-1]["outcome"] == "ship"
    assert history[-1]["disposed_by"] == CRITIC_JOB_ID
    # Falls back to plain rotation with no campaign stamps...
    spawn.assert_awaited_once()
    assert spawn.call_args.kwargs["stage"] == "developer"
    assert spawn.call_args.kwargs.get("extra_context") is None
    # ...and the rotation write-back carries the clear too -- None, not
    # _WB_UNSET (fix: M7) -- so the very next spawn's kickoff (built from
    # loop_for_spawn, a snapshot of `loop` taken before this call, not from
    # this DB row) reflects "no campaign", not the just-disposed one.
    assert db.update_project_loop.call_args_list[-1].kwargs["campaign"] is None
    assert any("disposed" in a for a in actions)


@pytest.mark.asyncio
async def test_dispose_only_plan_next_spawn_sees_campaign_cleared():
    """M7 repro: the `loop` dict `_rotate_loop_to_next_stage` receives still
    carries the OLD (just-disposed) campaign -- it's a snapshot taken before
    this advance ran. Before the fix, `_advance_planner_campaign` returned
    `_WB_UNSET` ("unchanged") for campaign_update on the dispose-only path
    even though it had just persisted campaign=None, so
    `loop_for_spawn["campaign"]` kept the disposed campaign and the very
    next job's kickoff would assert an IN PROGRESS campaign that no longer
    exists -- in the one block agents are told to trust."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
    reviewed = _campaign(
        status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
    )
    plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(campaign=reviewed),
            seq_index_completed=1,
            base_total=13,
            next_remaining=16,
            consecutive=0,
            last_error=None,
            actions=[],
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    # _spawn_loop_stage received loop_for_spawn as its first positional arg.
    spawned_loop = spawn.call_args.args[0]
    assert spawned_loop["campaign"] is None


@pytest.mark.asyncio
async def test_skipped_review_is_loud_and_leaves_campaign_parked():
    """A checkpoint critic that files nothing while a campaign awaits review
    still falls back to rotation — but the skip is surfaced, not silent."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    notify = AsyncMock()
    actions: list[str] = []
    reviewed = _campaign(status="review", stages_done=3)
    with _patched_main(db, spawn):
        with patch("main._notify_loop_event", notify):
            await _rotate_loop_to_next_stage(
                _loop(campaign=reviewed),
                seq_index_completed=1,
                base_total=13,
                next_remaining=16,
                consecutive=0,
                last_error=None,
                actions=actions,
                completed_job=_critic_job(None),
                completed_ctx=_critic_job(None)["context"],
                completed_failed=False,
            )
    spawn.assert_awaited_once()
    assert spawn.call_args.kwargs["stage"] == "developer"
    assert any("awaits disposition" in a for a in actions)
    notify.assert_awaited_once()
    assert notify.call_args.kwargs["event_type"] == "loop_campaign_review_skipped"
    # The parked campaign itself is untouched.
    assert all(
        "campaign" not in c.kwargs for c in db.update_project_loop.call_args_list
    )


@pytest.mark.asyncio
async def test_apply_time_rejection_degrades_to_rotation():
    """The budget may shrink between intake and apply; a now-unaffordable plan
    must degrade to the K=1 rotation fallback, not wedge or spawn anyway."""
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = _spawn_mock()
    actions: list[str] = []
    plan = _plan(stages=["developer"] * 5)
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(remaining_iterations=3),  # affordable = 1
            seq_index_completed=1,
            base_total=10,
            next_remaining=2,
            consecutive=0,
            last_error=None,
            actions=actions,
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    assert spawn.call_args.kwargs["stage"] == "developer"  # rotation fallback
    assert "campaign" not in db.update_project_loop.call_args_list[-1].kwargs
    assert any("rejected at apply time" in a for a in actions)


@pytest.mark.asyncio
async def test_campaign_spawn_failure_marks_loop_failed():
    from main import _rotate_loop_to_next_stage

    db = AsyncMock()
    spawn = AsyncMock(side_effect=RuntimeError("gitea down"))
    actions: list[str] = []
    plan = _plan()
    with _patched_main(db, spawn):
        await _rotate_loop_to_next_stage(
            _loop(),
            seq_index_completed=1,
            base_total=10,
            next_remaining=19,
            consecutive=0,
            last_error=None,
            actions=actions,
            completed_job=_critic_job(plan),
            completed_ctx=_critic_job(plan)["context"],
            completed_failed=False,
        )
    final = db.update_project_loop.call_args_list[-1].kwargs
    assert final["status"] == "failed"
    assert "campaign spawn failed" in final["last_error"]
    assert any("stopped" in a for a in actions)


# =============================================================================
# 3. Intake endpoint — checkpoint-only gating chain
# =============================================================================


class _FakeAcquire:
    """Minimal async-context-manager pool: acquire() → conn with fetchrow/fetch."""

    def __init__(self, fetchrow: AsyncMock, fetch: AsyncMock | None = None):
        self._conn = MagicMock()
        self._conn.fetchrow = fetchrow
        self._conn.fetch = fetch or AsyncMock(return_value=[])

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _CM()


def _intake_db(job: dict | None, loop: dict | None) -> AsyncMock:
    db = AsyncMock()
    db.get_job = AsyncMock(return_value=job)
    db.get_project_loop = AsyncMock(return_value=loop)
    db.merge_job_context = AsyncMock(return_value=True)
    return db


def _intake_patches(db: AsyncMock, vector_db: Any = None):
    stack = ExitStack()
    stack.enter_context(patch("main.require_internal", AsyncMock()))
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(patch("main.vector_db", vector_db))
    return stack


@pytest.mark.asyncio
async def test_intake_happy_path_stores_normalized_plan():
    from main import LoopPlanRequest, file_loop_plan

    job = _critic_job(None)
    loop = _loop(project_id=None)
    db = _intake_db(job, loop)
    with _intake_patches(db):
        out = await file_loop_plan(
            MagicMock(), CRITIC_JOB_ID, LoopPlanRequest(plan=_plan())
        )
    assert out["status"] == "accepted"
    db.merge_job_context.assert_awaited_once()
    stored = db.merge_job_context.call_args.args[1]["loop_plan"]
    assert stored["stages"] == [{"role": "developer"}, {"role": "developer"}]


@pytest.mark.asyncio
async def test_intake_accepts_member_when_display_pointer_absent():
    # Pins the membership gate against a pointer-equality revert: the job IS
    # a stage member while the display-only current_job_id disagrees (null).
    from main import LoopPlanRequest, file_loop_plan

    job = _critic_job(None)
    loop = _loop(
        project_id=None, current_job_id=None, current_stage_jobs=[CRITIC_JOB_ID]
    )
    db = _intake_db(job, loop)
    with _intake_patches(db):
        out = await file_loop_plan(
            MagicMock(), CRITIC_JOB_ID, LoopPlanRequest(plan=_plan())
        )
    assert out["status"] == "accepted"
    db.merge_job_context.assert_awaited_once()
    stored = db.merge_job_context.call_args.args[1]["loop_plan"]
    assert stored["stages"] == [{"role": "developer"}, {"role": "developer"}]


@pytest.mark.asyncio
async def test_intake_accepts_dispose_only_plan_and_skips_kb_check():
    # A dispose-only filing has no initiative — the KB existence check must be
    # skipped (nothing to verify), not crash on initiative=None.
    from main import LoopPlanRequest, file_loop_plan

    job = _critic_job(None)
    loop = _loop(
        project_id="11111111-2222-3333-4444-555555555555",
        campaign=_campaign(status="review"),
    )
    db = _intake_db(job, loop)
    kb_fetchrow = AsyncMock(return_value=None)  # would reject if consulted
    with _intake_patches(db, vector_db=_FakeAcquire(kb_fetchrow)):
        out = await file_loop_plan(
            MagicMock(),
            CRITIC_JOB_ID,
            LoopPlanRequest(
                plan={"disposition": {"outcome": "kill", "notes": "dead end"}}
            ),
        )
    assert out["status"] == "accepted"
    kb_fetchrow.assert_not_awaited()
    stored = db.merge_job_context.call_args.args[1]["loop_plan"]
    assert stored["initiative"] is None
    assert stored["stages"] == []
    assert stored["disposition"]["outcome"] == "kill"


@pytest.mark.asyncio
async def test_intake_gating_chain():
    from main import LoopPlanRequest, file_loop_plan

    req = MagicMock()
    plan = LoopPlanRequest(plan=_plan())

    # Not a loop job → 400.
    job = {"id": CRITIC_JOB_ID, "context": {}}
    with _intake_patches(_intake_db(job, None)):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 400

    # Standard-scheduled loop → 409.
    job = _critic_job(None)
    with _intake_patches(_intake_db(job, _loop(scheduling="standard"))):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 409

    # Not the in-flight job → 409, pinning the membership-gate detail (the
    # old pointer-equality gate also raised 409 here, just for the wrong
    # reason — assert the message so a revert to pointer-equality is caught).
    with _intake_patches(
        _intake_db(
            job, _loop(current_job_id=None, current_stage_jobs=[str(uuid.uuid4())])
        )
    ):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 409
    assert "not one of the loop's in-flight jobs" in e.value.detail

    # Non-critic role → 403.
    dev = _member_job("c-1", 0)
    dev["id"] = CRITIC_JOB_ID
    with _intake_patches(_intake_db(dev, _loop())):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 403

    # A campaign-member critic (sub-critic: stamped at the execution slot,
    # not the checkpoint) → 403.
    sub_critic = _critic_job(None, seq_index=2)
    with _intake_patches(_intake_db(sub_critic, _loop())):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 403

    # Invalid plan body → 400 with the domain validator's message.
    with _intake_patches(_intake_db(_critic_job(None), _loop())):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(
                req, CRITIC_JOB_ID, LoopPlanRequest(plan={"stages": []})
            )
    assert e.value.status_code == 400
    assert "kb_note_id" in e.value.detail


@pytest.mark.asyncio
async def test_intake_kb_existence_check():
    from main import LoopPlanRequest, file_loop_plan

    req = MagicMock()
    job = _critic_job(None)
    loop = _loop(project_id=str(uuid.uuid4()))

    # Note present → accepted.
    vector = _FakeAcquire(AsyncMock(return_value={"?column?": 1}))
    db = _intake_db(job, loop)
    with _intake_patches(db, vector):
        out = await file_loop_plan(req, CRITIC_JOB_ID, LoopPlanRequest(plan=_plan()))
    assert out["status"] == "accepted"

    # Note missing → 400.
    vector = _FakeAcquire(AsyncMock(return_value=None))
    with _intake_patches(_intake_db(job, loop), vector):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, LoopPlanRequest(plan=_plan()))
    assert e.value.status_code == 400
    assert "not found in the project KB" in e.value.detail

    # Store down → accepted (best-effort, KB failures are non-fatal).
    vector = _FakeAcquire(AsyncMock(side_effect=RuntimeError("kb down")))
    db = _intake_db(job, loop)
    with _intake_patches(db, vector):
        out = await file_loop_plan(req, CRITIC_JOB_ID, LoopPlanRequest(plan=_plan()))
    assert out["status"] == "accepted"


# =============================================================================
# 4. Router start-request validation
# =============================================================================


@pytest.mark.asyncio
async def test_start_rejects_planner_with_invalid_template():
    from routers.project_loops import ProjectLoopStart, start_project_loop

    body = ProjectLoopStart(
        max_iterations=10,
        scheduling="campaign",
        role_sequence=["scholar", "developer"],  # no critic checkpoint
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "routers.project_loops.require_approved_user",
                AsyncMock(return_value={"id": str(uuid.uuid4())}),
            )
        )
        stack.enter_context(
            patch("routers.project_loops.require_project_member", AsyncMock())
        )
        stack.enter_context(patch("main.postgres_db", AsyncMock()))
        with pytest.raises(HTTPException) as e:
            await start_project_loop(MagicMock(), str(uuid.uuid4()), body)
    assert e.value.status_code == 400
    assert "critic" in e.value.detail


@pytest.mark.asyncio
async def test_start_rejects_campaign_caps_on_rotation():
    from routers.project_loops import ProjectLoopStart, start_project_loop

    body = ProjectLoopStart(max_iterations=10, campaign_caps={"max_stages": 3})
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "routers.project_loops.require_approved_user",
                AsyncMock(return_value={"id": str(uuid.uuid4())}),
            )
        )
        stack.enter_context(
            patch("routers.project_loops.require_project_member", AsyncMock())
        )
        stack.enter_context(patch("main.postgres_db", AsyncMock()))
        with pytest.raises(HTTPException) as e:
            await start_project_loop(MagicMock(), str(uuid.uuid4()), body)
    assert e.value.status_code == 400
    assert "campaign_caps" in e.value.detail


@pytest.mark.asyncio
async def test_start_rejects_project_without_cloud_folder():
    from routers.project_loops import ProjectLoopStart, start_project_loop

    project_id = str(uuid.uuid4())
    body = ProjectLoopStart(max_iterations=3)
    db = AsyncMock()
    db.get_active_project_loop.return_value = None
    db.get_project.return_value = {
        "id": project_id,
        "name": "No Cloud Yet",
        "main_cloud_backend": None,
        "main_cloud_folder_handle": None,
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "routers.project_loops.require_approved_user",
                AsyncMock(return_value={"id": str(uuid.uuid4())}),
            )
        )
        stack.enter_context(
            patch("routers.project_loops.require_project_member", AsyncMock())
        )
        stack.enter_context(patch("main.postgres_db", db))
        with pytest.raises(HTTPException) as e:
            await start_project_loop(MagicMock(), project_id, body)

    assert e.value.status_code == 409
    assert "cloud folder" in e.value.detail
    db.create_project_loop.assert_not_awaited()


# =============================================================================
# 5. P1 — checkpoint-only tool injection, kickoff blocks, notifications, tool
# =============================================================================


def _spawn_db() -> AsyncMock:
    db = AsyncMock()
    db.create_job = AsyncMock(return_value={"id": "job-1"})
    db.list_experts_visible = AsyncMock(return_value=[])
    db.list_project_datasources = AsyncMock(return_value=[])
    return db


def _bare_loop(**over) -> dict:
    base = {
        "id": LOOP_ID,
        "project_id": None,
        "owner_id": None,
        "goal": "Build a thing",
        "scheduling": "campaign",
        "role_sequence": [["scholar", "product-qa"], "critic", "developer"],
        "remaining_iterations": 20,
        "campaign": None,
        "campaign_caps": None,
    }
    base.update(over)
    return base


class TestLoopPlanToolInjection:
    @pytest.mark.asyncio
    async def test_checkpoint_critic_gets_loop_plan(self):
        db = _spawn_db()
        await create_loop_job(
            db, _bare_loop(), role="critic", iteration=10, seq_index=1
        )
        co = db.create_job.call_args.kwargs["config_override"]
        assert co["tools"] == {"loop": ["loop_plan"]}

    @pytest.mark.asyncio
    async def test_rotation_critic_gets_nothing(self):
        db = _spawn_db()
        await create_loop_job(
            db,
            _bare_loop(scheduling="standard"),
            role="critic",
            iteration=10,
            seq_index=1,
        )
        assert "tools" not in db.create_job.call_args.kwargs["config_override"]

    @pytest.mark.asyncio
    async def test_campaign_member_sub_critic_gets_nothing(self):
        db = _spawn_db()
        await create_loop_job(
            db,
            _bare_loop(campaign=_campaign()),
            role="critic",
            iteration=11,
            seq_index=2,
            extra_context={"loop_campaign_id": CRITIC_JOB_ID, "loop_campaign_index": 1},
        )
        assert "tools" not in db.create_job.call_args.kwargs["config_override"]

    @pytest.mark.asyncio
    async def test_planner_developer_gets_nothing(self):
        db = _spawn_db()
        await create_loop_job(
            db, _bare_loop(), role="developer", iteration=11, seq_index=2
        )
        assert "tools" not in db.create_job.call_args.kwargs["config_override"]


class TestPlannerKickoffBlocks:
    def test_checkpoint_critic_gets_planner_duties(self):
        from services.project_loops import build_loop_kickoff

        text = build_loop_kickoff(_bare_loop(), role="critic", iteration=10)
        assert "PLANNER DUTIES" in text
        assert "loop_plan" in text
        assert "user-question" in text  # the anti-fiction rule
        assert "DISPOSITION DUTY" not in text  # no campaign pending

    def test_pending_campaign_adds_disposition_duty_with_acceptance(self):
        from services.project_loops import build_loop_kickoff

        loop = _bare_loop(campaign=_campaign(status="review", stages_done=3))
        text = build_loop_kickoff(loop, role="critic", iteration=13)
        assert "DISPOSITION DUTY FIRST" in text
        assert "pytest -x" in text  # pre-registered acceptance surfaced
        assert "ship" in text and "extend" in text and "kill" in text

    def test_campaign_member_gets_campaign_context(self):
        from services.project_loops import build_loop_kickoff

        loop = _bare_loop(campaign=_campaign())
        text = build_loop_kickoff(
            loop,
            role="developer",
            iteration=11,
            extra_context={
                "loop_campaign_id": CRITIC_JOB_ID,
                "loop_campaign_index": 1,
            },
        )
        assert "CAMPAIGN CONTEXT: you are stage 2 of 3" in text
        assert "honest" in text  # the verification-repricing contract
        assert "PLANNER DUTIES" not in text

    def test_rotation_loop_kickoffs_unchanged(self):
        from services.project_loops import build_loop_kickoff

        text = build_loop_kickoff(
            _bare_loop(scheduling="standard"), role="critic", iteration=10
        )
        assert "PLANNER DUTIES" not in text
        assert "CAMPAIGN CONTEXT" not in text

    def test_planner_non_member_developer_gets_no_block(self):
        from services.project_loops import build_loop_kickoff

        text = build_loop_kickoff(_bare_loop(), role="developer", iteration=11)
        assert "CAMPAIGN CONTEXT" not in text
        assert "PLANNER DUTIES" not in text


class TestLoopNotifications:
    @pytest.mark.asyncio
    async def test_notify_persists_and_broadcasts(self):
        from main import _notify_loop_event

        db = AsyncMock()
        feed = MagicMock()
        loop = _loop(owner_id="cccccccc-0000-0000-0000-000000000001")
        with patch("main.postgres_db", db):
            with patch("services.notification_feed.notification_feed", feed):
                await _notify_loop_event(
                    loop,
                    job_id=CRITIC_JOB_ID,
                    event_type="loop_campaign_disposition",
                    subject="Loop campaign ship: F5",
                    message="done",
                )
        db.log_message.assert_awaited_once()
        kw = db.log_message.call_args.kwargs
        assert kw["direction"] == "outbound"
        assert kw["user_id"] == "cccccccc-0000-0000-0000-000000000001"
        # message_log.thread_id is varchar(12) NOT NULL — found live when the
        # first disposition event failed the constraint with thread_id=None.
        assert kw["thread_id"] and len(kw["thread_id"]) <= 12
        feed.broadcast.assert_called_once()
        assert (
            feed.broadcast.call_args.kwargs["event_type"] == "loop_campaign_disposition"
        )

    @pytest.mark.asyncio
    async def test_notify_skips_ownerless_loops(self):
        from main import _notify_loop_event

        db = AsyncMock()
        with patch("main.postgres_db", db):
            await _notify_loop_event(
                _loop(owner_id=None),
                job_id=CRITIC_JOB_ID,
                event_type="x",
                subject="s",
                message="m",
            )
        db.log_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_question_scan_emits_per_note(self):
        from main import _notify_loop_user_questions

        rows = [
            {"note_id": "q-1", "title": "Need lawyer budget?"},
            {"note_id": "q-2", "title": "Which domain name?"},
        ]
        vector = _FakeAcquire(AsyncMock(), fetch=AsyncMock(return_value=rows))
        notify = AsyncMock()
        loop = _loop(project_id=str(uuid.uuid4()))
        job = {"id": CRITIC_JOB_ID}
        with patch("main.vector_db", vector):
            with patch("main._notify_loop_event", notify):
                await _notify_loop_user_questions(loop, job)
        assert notify.await_count == 2
        first = notify.call_args_list[0].kwargs
        assert first["event_type"] == "loop_user_question"
        assert "Need lawyer budget?" in first["subject"]

    @pytest.mark.asyncio
    async def test_user_question_scan_silent_without_project_or_store(self):
        from main import _notify_loop_user_questions

        notify = AsyncMock()
        with patch("main.vector_db", None):
            with patch("main._notify_loop_event", notify):
                await _notify_loop_user_questions(
                    _loop(project_id=None), {"id": CRITIC_JOB_ID}
                )
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_abort_emits_disposition_event(self):
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        notify = AsyncMock()
        camp = _campaign(member_failures=1)
        member = _member_job(camp["id"], 1, status="failed")
        with _patched_main(db, spawn):
            with patch("main._notify_loop_event", notify):
                await _rotate_loop_to_next_stage(
                    _loop(campaign=camp, seq_index=2, current_job_id=member["id"]),
                    seq_index_completed=2,
                    base_total=12,
                    next_remaining=17,
                    consecutive=2,
                    last_error="job failed",
                    actions=[],
                    completed_job=member,
                    completed_ctx=member["context"],
                    completed_failed=True,
                )
        notify.assert_awaited_once()
        kw = notify.call_args.kwargs
        assert kw["event_type"] == "loop_campaign_disposition"
        assert "aborted" in kw["subject"]

    @pytest.mark.asyncio
    async def test_disposition_emits_event_with_outcome(self):
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        notify = AsyncMock()
        prior = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(status="review", id=prior, plan_job_id=prior)
        plan = _plan(disposition={"outcome": "ship", "notes": "evidence green"})
        with _patched_main(db, spawn):
            with patch("main._notify_loop_event", notify):
                await _rotate_loop_to_next_stage(
                    _loop(campaign=reviewed),
                    seq_index_completed=1,
                    base_total=13,
                    next_remaining=16,
                    consecutive=0,
                    last_error=None,
                    actions=[],
                    completed_job=_critic_job(plan),
                    completed_ctx=_critic_job(plan)["context"],
                    completed_failed=False,
                )
        notify.assert_awaited_once()
        kw = notify.call_args.kwargs
        assert "ship" in kw["subject"]
        assert "evidence green" in kw["message"]


class TestDispositionClosesBacklogTicket:
    """Task 6: the critic's verdict mirrors onto the ticket the campaign was
    built from. ship/kill close it (knowledge-base/knowledge/superpowers/specs/
    2026-07-26-project-backlog-pipeline-design.md); extend must NOT -- the
    continuing campaign still owns it, and getting this wrong would close a
    ticket that is still being worked. close_backlog_ticket itself (its
    file+index dual write, best-effort contract, and frontmatter rewrite) is
    covered at the unit level in test_project_backlog.py::
    TestCloseBacklogTicket -- these prove the *mapping* main.py performs
    before it ever gets there.
    """

    @pytest.mark.asyncio
    async def test_ship_closes_the_ticket_as_resolved(self):
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=True)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
        )
        plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
        pid = str(uuid.uuid4())
        with _patched_main(db, spawn):
            with patch("main.vector_db", MagicMock()):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    await _rotate_loop_to_next_stage(
                        _loop(campaign=reviewed, project_id=pid),
                        seq_index_completed=1,
                        base_total=13,
                        next_remaining=16,
                        consecutive=0,
                        last_error=None,
                        actions=[],
                        completed_job=_critic_job(plan),
                        completed_ctx=_critic_job(plan)["context"],
                        completed_failed=False,
                    )
        close.assert_awaited_once()
        args = close.await_args.args
        assert args[2] == pid  # project_id
        assert args[3] == "qa-finding-f5"  # _campaign()'s default initiative
        assert args[4] == "resolved"

    @pytest.mark.asyncio
    async def test_kill_closes_the_ticket_as_archived(self):
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=True)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
        )
        plan = {"disposition": {"outcome": "kill", "notes": "dead end"}}
        with _patched_main(db, spawn):
            with patch("main.vector_db", MagicMock()):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    await _rotate_loop_to_next_stage(
                        _loop(campaign=reviewed, project_id=str(uuid.uuid4())),
                        seq_index_completed=1,
                        base_total=13,
                        next_remaining=16,
                        consecutive=0,
                        last_error=None,
                        actions=[],
                        completed_job=_critic_job(plan),
                        completed_ctx=_critic_job(plan)["context"],
                        completed_failed=False,
                    )
        close.assert_awaited_once()
        assert close.await_args.args[4] == "archived"

    @pytest.mark.asyncio
    async def test_extend_does_not_touch_the_ticket(self):
        """The highest-stakes case: getting this wrong closes a ticket that
        is still being worked by the very campaign that just extended
        itself."""
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=True)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review",
            stages_done=3,
            extensions_used=0,
            id=prior_critic,
            plan_job_id=prior_critic,
        )
        plan = _plan(
            stages=["developer"],
            disposition={"outcome": "extend", "notes": "one more push"},
        )
        with _patched_main(db, spawn):
            with patch("main.vector_db", MagicMock()):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    await _rotate_loop_to_next_stage(
                        _loop(campaign=reviewed, project_id=str(uuid.uuid4())),
                        seq_index_completed=1,
                        base_total=13,
                        next_remaining=16,
                        consecutive=0,
                        last_error=None,
                        actions=[],
                        completed_job=_critic_job(plan),
                        completed_ctx=_critic_job(plan)["context"],
                        completed_failed=False,
                    )
        close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_failure_is_logged_not_discarded(self, caplog):
        """B1 finding 3 (main.py side): the return value of
        close_backlog_ticket used to be discarded entirely -- a close that
        reported failure (e.g. the decision-note-as-initiative bug, or any
        other mirror-write failure) left no trace anywhere in the loop's own
        logs."""
        import logging

        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=False)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
        )
        plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
        with _patched_main(db, spawn):
            with patch("main.vector_db", MagicMock()):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    with caplog.at_level(logging.WARNING, logger="main"):
                        await _rotate_loop_to_next_stage(
                            _loop(campaign=reviewed, project_id=str(uuid.uuid4())),
                            seq_index_completed=1,
                            base_total=13,
                            next_remaining=16,
                            consecutive=0,
                            last_error=None,
                            actions=[],
                            completed_job=_critic_job(plan),
                            completed_ctx=_critic_job(plan)["context"],
                            completed_failed=False,
                        )
        close.assert_awaited_once()
        assert any(
            "close_backlog_ticket reported failure" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_close_success_does_not_warn(self, caplog):
        """Non-regression companion: the ordinary successful path must not
        spuriously log a failure warning."""
        import logging

        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=True)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
        )
        plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
        with _patched_main(db, spawn):
            with patch("main.vector_db", MagicMock()):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    with caplog.at_level(logging.WARNING, logger="main"):
                        await _rotate_loop_to_next_stage(
                            _loop(campaign=reviewed, project_id=str(uuid.uuid4())),
                            seq_index_completed=1,
                            base_total=13,
                            next_remaining=16,
                            consecutive=0,
                            last_error=None,
                            actions=[],
                            completed_job=_critic_job(plan),
                            completed_ctx=_critic_job(plan)["context"],
                            completed_failed=False,
                        )
        assert not any(
            "close_backlog_ticket reported failure" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_vector_db_skips_the_close_call(self):
        """A KB/pgvector outage must cost the mirror, never the disposition
        itself -- the guard short-circuits before close_backlog_ticket, and
        the campaign_history entry still gets written."""
        from main import _rotate_loop_to_next_stage

        db = AsyncMock()
        spawn = _spawn_mock()
        close = AsyncMock(return_value=True)
        prior_critic = "aaaaaaaa-0000-0000-0000-00000000dead"
        reviewed = _campaign(
            status="review", stages_done=3, id=prior_critic, plan_job_id=prior_critic
        )
        plan = {"disposition": {"outcome": "ship", "notes": "acceptance passed"}}
        with _patched_main(db, spawn):
            with patch("main.vector_db", None):
                with patch("services.project_backlog.close_backlog_ticket", close):
                    await _rotate_loop_to_next_stage(
                        _loop(campaign=reviewed, project_id=str(uuid.uuid4())),
                        seq_index_completed=1,
                        base_total=13,
                        next_remaining=16,
                        consecutive=0,
                        last_error=None,
                        actions=[],
                        completed_job=_critic_job(plan),
                        completed_ctx=_critic_job(plan)["context"],
                        completed_failed=False,
                    )
        close.assert_not_awaited()
        pre_spawn = db.update_project_loop.call_args_list[0].kwargs
        assert pre_spawn["campaign_history"][-1]["outcome"] == "ship"


class TestLoopPlanTool:
    class _FakeResp:
        def __init__(self, status_code: int, payload: Any):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    @staticmethod
    def _client_returning(resp, calls: list):
        class _Client:
            def __init__(self, *a, **kw):
                self._kw = kw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                calls.append(
                    {"url": url, "json": json, "headers": self._kw.get("headers")}
                )
                return resp

        return _Client

    @pytest.mark.asyncio
    async def test_accepted_plan_round_trip(self, monkeypatch):
        from src.tools.context import ToolContext
        from src.tools.loop.plan import create_loop_plan_tools

        monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch:8085")
        monkeypatch.setenv("MCP_INTERNAL_KEY", "sekrit")
        calls: list = []
        resp = self._FakeResp(
            200,
            {
                "status": "accepted",
                "plan": {
                    "initiative": {"kb_note_id": "qa-finding-f5"},
                    "stages": [{"role": "developer"}, {"role": "developer"}],
                    "disposition": {"outcome": "ship"},
                },
            },
        )
        with patch(
            "src.tools.loop.plan.httpx.AsyncClient",
            self._client_returning(resp, calls),
        ):
            tool_fn = create_loop_plan_tools(ToolContext(_job_id="j-123"))[0]
            out = await tool_fn.ainvoke(
                {
                    "initiative_note_id": "qa-finding-f5",
                    "stages": ["developer", "developer"],
                    "disposition_outcome": "ship",
                }
            )
        assert "Plan ACCEPTED: 2-stage campaign" in out
        assert "disposed: ship" in out
        call = calls[0]
        assert call["url"] == "http://orch:8085/api/jobs/j-123/loop-plan"
        assert call["headers"]["X-Internal-Key"] == "sekrit"
        assert call["json"]["plan"]["stages"] == [
            {"role": "developer"},
            {"role": "developer"},
        ]

    @pytest.mark.asyncio
    async def test_rejection_surfaces_validator_detail(self):
        from src.tools.context import ToolContext
        from src.tools.loop.plan import create_loop_plan_tools

        calls: list = []
        resp = self._FakeResp(400, {"detail": "plan costs 9 iterations but..."})
        with patch(
            "src.tools.loop.plan.httpx.AsyncClient",
            self._client_returning(resp, calls),
        ):
            tool_fn = create_loop_plan_tools(ToolContext(_job_id="j-123"))[0]
            out = await tool_fn.ainvoke(
                {"initiative_note_id": "n", "stages": ["developer"]}
            )
        assert "Plan REJECTED" in out
        assert "plan costs 9 iterations" in out
        assert "nothing was stored" in out.lower()

    @pytest.mark.asyncio
    async def test_registry_and_loader_wiring(self):
        from src.core.loader import AgentConfig
        from src.core.loader import get_all_tool_names
        from src.tools.registry import TOOL_REGISTRY

        assert "loop_plan" in TOOL_REGISTRY
        assert TOOL_REGISTRY["loop_plan"]["category"] == "loop"

        cfg = AgentConfig(agent_id="test", display_name="Test")
        cfg.tools.loop = ["loop_plan"]
        assert "loop_plan" in get_all_tool_names(cfg)


class TestLoopJobsNeverPendingReview:
    """A pending_review loop job wedges its loop forever (the advance fires
    only on terminal statuses) — found live when a campaign member finished
    without declaring job_complete. Loop jobs with no declared freeze must map
    to completed instead; non-loop jobs keep the human-review gate. Legitimate
    pause freezes survive the exemption, while an unknown declared freeze must
    fail visibly instead of phantom-completing partial work."""

    @staticmethod
    def _job(loop: bool, **over) -> dict:
        base = {
            "id": str(uuid.uuid4()),
            "context": {"loop_id": LOOP_ID} if loop else {},
            "config_override": {"verification": {"enabled": False}},
            "freeze_data": None,
        }
        base.update(over)
        return base

    def test_loop_job_without_declaration_completes(self):
        from services.completion import determine_job_status

        status, err = determine_job_status(
            self._job(loop=True), {"should_stop": True, "goal_achieved": False}
        )
        assert (status, err) == ("completed", None)

    def test_loop_completion_without_goal_achieved_completes(self):
        from services.completion import determine_job_status

        job = self._job(loop=True, freeze_data={"freeze_type": "job_complete"})
        status, err = determine_job_status(
            job, {"should_stop": True, "goal_achieved": False}
        )
        assert (status, err) == ("completed", None)

    def test_non_loop_job_keeps_review_gate(self):
        from services.completion import determine_job_status

        status, _ = determine_job_status(
            self._job(loop=False), {"should_stop": True, "goal_achieved": False}
        )
        assert status == "pending_review"
        job = self._job(loop=False, freeze_data={"freeze_type": "job_complete"})
        status, _ = determine_job_status(
            job, {"should_stop": True, "goal_achieved": False}
        )
        assert status == "pending_review"

    @pytest.mark.parametrize("freeze_type", ["version_upgrade", "batch_boundary"])
    def test_loop_pause_freezes_still_pause(self, freeze_type):
        from services.completion import determine_job_status

        job = self._job(loop=True, freeze_data={"freeze_type": freeze_type})
        status, _ = determine_job_status(
            job, {"should_stop": True, "goal_achieved": False}
        )
        assert status == "paused"

    def test_loop_unknown_declared_freeze_never_completes(self):
        from services.completion import determine_job_status

        job = self._job(
            loop=True, freeze_data={"freeze_type": "future_boundary_from_new_agent"}
        )
        status, err = determine_job_status(
            job, {"should_stop": True, "goal_achieved": False}
        )
        assert (status, err) == ("pending_review", None)


class TestAgentLoaderBindsLoopCategory:
    """Agent-side half of the tool-injection contract.

    The P3 live flip on 2026-07-10 found the gap the classes above can't see:
    the orchestrator injected ``tools: {loop: [loop_plan]}`` correctly (per
    TestLoopPlanToolInjection), but the agent's ToolsConfig constructor calls
    silently dropped the ``loop`` key (and ``communication``, breaking
    ``send_message`` for every worker) — so the checkpoint critic held its
    PLANNER DUTIES kickoff while owning no loop_plan tool, and every campaign
    silently degraded to single-job rotation. These tests load a dispatched
    config dict the way agent.py does and assert the categories survive all
    the way into get_all_tool_names.
    """

    def _load(self, tools: dict):
        from src.core.loader import load_agent_config_from_dict

        return load_agent_config_from_dict(
            {"agent_id": "t", "display_name": "T", "tools": tools}
        )

    def test_loop_category_survives_from_dict(self):
        config = self._load({"loop": ["loop_plan"]})
        assert config.tools.loop == ["loop_plan"]

    def test_loop_plan_reaches_the_bound_tool_names(self):
        from src.core.loader import get_all_tool_names

        config = self._load({"loop": ["loop_plan"], "core": ["todo_complete"]})
        names = get_all_tool_names(config)
        assert "loop_plan" in names
        assert "todo_complete" in names

    def test_communication_category_survives_from_dict(self):
        from src.core.loader import get_all_tool_names

        config = self._load({"communication": ["send_message"]})
        assert config.tools.communication == ["send_message"]
        assert "send_message" in get_all_tool_names(config)

    def test_every_tools_config_field_round_trips(self):
        """Parity guard: adding a ToolsConfig field without wiring the
        constructor kwarg is exactly the bug class this file exists for."""
        import dataclasses

        from src.core.loader import ToolsConfig

        field_names = [f.name for f in dataclasses.fields(ToolsConfig)]
        sentinel = {name: [f"x_{name}"] for name in field_names}
        config = self._load(sentinel)
        for name in field_names:
            # `git` is suppressed by ToolsConfig.__post_init__ because this
            # sentinel also populates `shell`; its own round trip is asserted
            # below so the field keeps its parity coverage.
            if name == "git":
                continue
            assert getattr(config.tools, name) == [f"x_{name}"], (
                f"tools.{name} was dropped by the loader — add the kwarg to "
                "both ToolsConfig(...) construction sites in src/core/loader.py"
            )

        without_shell = dict(sentinel, shell=[])
        assert self._load(without_shell).tools.git == ["x_git"], (
            "tools.git was dropped by the loader — add the kwarg to both "
            "ToolsConfig(...) construction sites in src/core/loader.py"
        )


# =============================================================================
# 5. Agent-side tool (src/tools/loop/plan.py) — dispose-only filing
# =============================================================================


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_tool_supports_dispose_only_filing():
    """The critic must be able to close a reviewed campaign without opening a
    new one: no initiative, no stages, just the disposition."""
    from src.tools.loop.plan import create_loop_plan_tools

    context = MagicMock()
    context.job_id = CRITIC_JOB_ID
    (plan_tool,) = create_loop_plan_tools(context)

    posted: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted["body"] = json
            return _FakeResponse(
                payload={
                    "status": "accepted",
                    "plan": {
                        "initiative": None,
                        "stages": [],
                        "acceptance": [],
                        "disposition": {"outcome": "ship", "notes": "done"},
                    },
                }
            )

    with patch("src.tools.loop.plan.httpx.AsyncClient", _FakeClient):
        out = await plan_tool.ainvoke(
            {"disposition_outcome": "ship", "disposition_notes": "done"}
        )

    assert posted["body"]["plan"] == {
        "disposition": {"outcome": "ship", "notes": "done"}
    }
    assert "ACCEPTED" in out
    assert "dispose" in out.lower()
