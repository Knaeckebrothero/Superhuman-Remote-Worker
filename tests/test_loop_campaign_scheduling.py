"""Unit tests for loop campaign scheduling — the Critic as planner (P0).

Covers the three layers docs/features/loop_campaign_scheduling.md ships dark:

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
        "scheduling": "planner",
        "role_sequence": [["scholar", "product-qa"], "critic", "developer"],
        "seq_index": 1,  # the critic slot just completed
        "total_jobs_run": 10,
        "max_iterations": 30,
        "remaining_iterations": 20,
        "current_job_id": CRITIC_JOB_ID,
        "current_stage_jobs": [],
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
                _loop(scheduling="rotation"),
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
    """Minimal async-context-manager pool: acquire() → conn with fetchrow."""

    def __init__(self, fetchrow: AsyncMock):
        self._conn = MagicMock()
        self._conn.fetchrow = fetchrow

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

    # Rotation-scheduled loop → 409.
    job = _critic_job(None)
    with _intake_patches(_intake_db(job, _loop(scheduling="rotation"))):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 409

    # Not the in-flight job → 409.
    with _intake_patches(_intake_db(job, _loop(current_job_id=str(uuid.uuid4())))):
        with pytest.raises(HTTPException) as e:
            await file_loop_plan(req, CRITIC_JOB_ID, plan)
    assert e.value.status_code == 409

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
        scheduling="planner",
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
