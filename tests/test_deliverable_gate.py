"""P1-C deliverable-contract gate at the seal (orchestrator side).

docs/issues/officer_blind_reads_and_worker_bureaucracy.md §4 P1-C / §7 annex E.

Verifier #1 sealed "26/27 todos done" with 0/7 required deliverables and
consumed a human-priced officer review cycle; F14's validator rejected a
COMPLETE job's correct deliverable list over a missing ``repo/`` prefix.

Covered here:
  - manifest parsing + path normalization (both sides tolerate ``repo/``
    and ``./`` — the F14 regression shape)
  - gate pass → seal proceeds, ``context.deliverable_gate`` stamped
  - missing → bounce through the P1-A resume-with-feedback lane with a
    PRECISE missing/present reason; no seal, caller told to early-return
  - bounce cap → stop bouncing, ``completed`` demotes to ``pending_review``
    (loop jobs keep ``completed`` — the pending_review loop-wedge rule)
  - Gitea unavailable / repo unresolvable → fail-open skip with stamp
  - no manifest / no completion claim → no-op
  - ``kb:<slug>`` entries: verified against the knowledge_index when the
    vector store answers; fail-open (never "missing") when it can't
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.deliverable_gate import (  # noqa: E402
    DELIVERABLE_GATE_BOUNCE_CAP,
    evaluate_deliverable_gate,
    gate_applies,
    normalize_deliverable_path,
    parse_required_deliverables,
    run_deliverable_gate,
)

# =============================================================================
# Fixtures
# =============================================================================

JOB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SHA = "c0ffee1234deadbeef5678"


def make_job(
    *,
    manifest: list[str] | None = None,
    context_extra: dict | None = None,
    repo_name: str | None = "job-aaaaaaaa",
    branch_name: str | None = None,
    parent_job_id: str | None = None,
    project_id: str | None = None,
    freeze_data: dict | None = None,
) -> dict:
    context: dict = dict(context_extra or {})
    if manifest is not None:
        context["required_deliverables"] = manifest
    return {
        "id": JOB_ID,
        "status": "processing",
        "repo_name": repo_name,
        "branch_name": branch_name,
        "parent_job_id": parent_job_id,
        "project_id": project_id,
        "context": context,
        "freeze_data": freeze_data,
    }


def completion_result(goal_achieved: bool = True) -> dict:
    return {
        "should_stop": True,
        "goal_achieved": goal_achieved,
        "freeze_data": {"freeze_type": "job_complete", "summary": "done"},
    }


def make_gitea(tree_paths: list[str] | None, *, initialized: bool = True):
    gitea = MagicMock()
    gitea.is_initialized = initialized
    gitea.list_tree = AsyncMock(
        return_value=(
            None
            if tree_paths is None
            else [{"path": p, "type": "blob", "sha": "x"} for p in tree_paths]
        )
    )
    gitea.get_branch_head_sha = AsyncMock(return_value=SHA)
    return gitea


def make_db():
    db = AsyncMock()
    db.merge_job_context = AsyncMock(return_value=True)
    db.get_job = AsyncMock(return_value=None)
    return db


def make_vector_db(existing_slugs: set[str]):
    """vector_db.acquire() async-context whose fetchrow knows those slugs."""
    conn = AsyncMock()

    async def fetchrow(_query, _project_id, slug):
        return {"ok": 1} if slug in existing_slugs else None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    vector_db = MagicMock()
    vector_db.acquire = MagicMock(return_value=ctx)
    return vector_db


def stamped(db) -> dict:
    """The last context.deliverable_gate stamp written through the mock db."""
    assert db.merge_job_context.await_count >= 1
    args = db.merge_job_context.await_args_list[-1][0]
    assert args[0] == JOB_ID
    return args[1]["deliverable_gate"]


# =============================================================================
# Normalization (F14)
# =============================================================================


class TestNormalization:
    def test_repo_prefix_stripped(self):
        assert normalize_deliverable_path("repo/output/a.md") == "output/a.md"

    def test_dot_slash_and_leading_slash_stripped(self):
        assert normalize_deliverable_path("./output/a.md") == "output/a.md"
        assert normalize_deliverable_path("./repo/output/a.md") == "output/a.md"
        assert normalize_deliverable_path("/output/a.md") == "output/a.md"

    def test_kb_entries_keep_prefix(self):
        assert normalize_deliverable_path("kb: my-note ") == "kb:my-note"
        assert normalize_deliverable_path("kb:") is None

    def test_garbage_dropped(self):
        assert normalize_deliverable_path("") is None
        assert normalize_deliverable_path("   ") is None
        assert normalize_deliverable_path(42) is None
        assert normalize_deliverable_path(None) is None

    def test_parse_dedupes_across_spellings(self):
        manifest = parse_required_deliverables(
            {
                "required_deliverables": [
                    "repo/output/a.md",
                    "./output/a.md",
                    "output/b.md",
                    "",
                    17,
                ]
            }
        )
        assert manifest == ["output/a.md", "output/b.md"]

    def test_parse_accepts_json_string_context_and_bare_values(self):
        assert parse_required_deliverables(
            '{"required_deliverables": ["output/a.md"]}'
        ) == ["output/a.md"]
        assert parse_required_deliverables("not json") == []
        assert parse_required_deliverables(
            {"required_deliverables": "output/a.md"}
        ) == ["output/a.md"]
        assert parse_required_deliverables({}) == []
        assert parse_required_deliverables(None) == []


# =============================================================================
# Trigger predicate
# =============================================================================


class TestGateApplies:
    def test_applies_to_claimed_completion_with_manifest(self):
        job = make_job(manifest=["output/a.md"])
        assert gate_applies(job, completion_result(), "completed") is True
        assert gate_applies(job, completion_result(), "pending_review") is True
        assert gate_applies(job, completion_result(), "reviewing") is True

    def test_no_manifest_is_noop(self):
        job = make_job(manifest=None)
        assert gate_applies(job, completion_result(), "completed") is False

    def test_honest_incomplete_stop_is_not_gated(self):
        """pending_review WITHOUT a completion claim must never bounce."""
        job = make_job(manifest=["output/a.md"])
        result = {"should_stop": True, "goal_achieved": False, "freeze_data": None}
        assert gate_applies(job, result, "pending_review") is False

    def test_row_freeze_claim_counts(self):
        """The handler wrote the job_complete freeze to the row already."""
        job = make_job(
            manifest=["output/a.md"],
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": False}
        assert gate_applies(job, result, "pending_review") is True

    def test_non_gated_statuses_pass_through(self):
        job = make_job(manifest=["output/a.md"])
        for status in ("paused", "failed", "waiting", None):
            assert gate_applies(job, completion_result(), status) is False


# =============================================================================
# Evaluation against the Gitea tree
# =============================================================================


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_prefix_tolerant_both_sides(self):
        """F14 regression shape: unprefixed manifest ↔ prefixed tree and
        vice versa must both count as present."""
        job = make_job(manifest=["repo/output/x.md", "./output/y.md"])
        gitea = make_gitea(["output/x.md", "repo/output/y.md"])
        report = await evaluate_deliverable_gate(job, db=make_db(), gitea=gitea)
        assert report["passed"] is True
        assert report["missing"] == []
        assert sorted(report["present"]) == ["output/x.md", "output/y.md"]
        assert report["commit_sha"] == SHA

    @pytest.mark.asyncio
    async def test_missing_listed_precisely(self):
        job = make_job(manifest=["output/a.md", "output/b.md"])
        gitea = make_gitea(["output/a.md", "unrelated.txt"])
        report = await evaluate_deliverable_gate(job, db=make_db(), gitea=gitea)
        assert report["passed"] is False
        assert report["missing"] == ["output/b.md"]
        assert report["present"] == ["output/a.md"]

    @pytest.mark.asyncio
    async def test_gitea_uninitialized_skips(self):
        job = make_job(manifest=["output/a.md"])
        report = await evaluate_deliverable_gate(
            job, db=make_db(), gitea=make_gitea([], initialized=False)
        )
        assert report["skipped"] is True

    @pytest.mark.asyncio
    async def test_unresolvable_repo_skips(self):
        job = make_job(manifest=["output/a.md"], repo_name=None)
        report = await evaluate_deliverable_gate(
            job, db=make_db(), gitea=make_gitea(["output/a.md"])
        )
        assert report["skipped"] is True

    @pytest.mark.asyncio
    async def test_subjob_resolves_parent_repo(self):
        job = make_job(
            manifest=["output/a.md"],
            repo_name=None,
            branch_name="job/aaaaaaaa",
            parent_job_id="99999999-8888-7777-6666-555555555555",
        )
        db = make_db()
        db.get_job = AsyncMock(return_value={"repo_name": "parent-repo"})
        gitea = make_gitea(["output/a.md"])
        report = await evaluate_deliverable_gate(job, db=db, gitea=gitea)
        assert report["passed"] is True
        gitea.list_tree.assert_awaited_once_with("parent-repo", "job/aaaaaaaa")

    @pytest.mark.asyncio
    async def test_unreadable_tree_skips(self):
        job = make_job(manifest=["output/a.md"])
        report = await evaluate_deliverable_gate(
            job, db=make_db(), gitea=make_gitea(None)
        )
        assert report["skipped"] is True

    @pytest.mark.asyncio
    async def test_kb_entries_verified_and_fail_open(self):
        job = make_job(
            manifest=["kb:present-note", "kb:absent-note"],
            project_id="11111111-2222-3333-4444-555555555555",
        )
        gitea = make_gitea([])
        report = await evaluate_deliverable_gate(
            job,
            db=make_db(),
            gitea=gitea,
            vector_db=make_vector_db({"present-note"}),
        )
        assert report["present"] == ["kb:present-note"]
        assert report["missing"] == ["kb:absent-note"]

        # No vector store → unverifiable, NEVER missing (fail-open, logged).
        report = await evaluate_deliverable_gate(
            job, db=make_db(), gitea=gitea, vector_db=None
        )
        assert report["passed"] is True
        assert report["missing"] == []
        assert sorted(report["unverified"]) == ["kb:absent-note", "kb:present-note"]


# =============================================================================
# The gate — pass / bounce / cap / skip
# =============================================================================


class TestRunGate:
    @pytest.mark.asyncio
    async def test_pass_seals_and_stamps(self):
        job = make_job(manifest=["output/a.md"])
        db = make_db()
        queue_resume = AsyncMock()
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea(["output/a.md"]),
            queue_resume=queue_resume,
        )
        assert (new_status, bounced) == ("completed", False)
        assert any("deliverable gate passed" in a for a in actions)
        queue_resume.assert_not_awaited()
        stamp = stamped(db)
        assert stamp["passed"] is True
        assert stamp["commit_sha"] == SHA
        assert stamp["bounces"] == 0

    @pytest.mark.asyncio
    async def test_missing_bounces_with_precise_reason(self):
        job = make_job(manifest=["output/a.md", "output/b.md"])
        db = make_db()
        queue_resume = AsyncMock()
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea(["output/a.md"]),
            queue_resume=queue_resume,
        )
        assert bounced is True
        assert new_status is None
        queue_resume.assert_awaited_once()
        args, kwargs = queue_resume.await_args
        assert args[0] == JOB_ID
        feedback = args[1]
        # The precise listing: missing AND present, at the checked sha.
        assert "output/b.md" in feedback
        assert "output/a.md" in feedback
        assert "MISSING (1)" in feedback
        assert "PRESENT (1)" in feedback
        assert SHA[:12] in feedback
        reason = kwargs["reason"]
        assert "1 of 2" in reason
        assert "bounce 1/2" in reason
        stamp = stamped(db)
        assert stamp["passed"] is False
        assert stamp["bounces"] == 1
        assert stamp["missing"] == ["output/b.md"]

    @pytest.mark.asyncio
    async def test_second_bounce_increments(self):
        job = make_job(
            manifest=["output/b.md"],
            context_extra={"deliverable_gate": {"passed": False, "bounces": 1}},
        )
        db = make_db()
        queue_resume = AsyncMock()
        _, _, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea([]),
            queue_resume=queue_resume,
        )
        assert bounced is True
        assert stamped(db)["bounces"] == 2

    @pytest.mark.asyncio
    async def test_cap_falls_through_to_pending_review_with_report(self):
        job = make_job(
            manifest=["output/b.md"],
            context_extra={
                "deliverable_gate": {
                    "passed": False,
                    "bounces": DELIVERABLE_GATE_BOUNCE_CAP,
                }
            },
        )
        db = make_db()
        queue_resume = AsyncMock()
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea([]),
            queue_resume=queue_resume,
        )
        assert bounced is False
        assert new_status == "pending_review"
        queue_resume.assert_not_awaited()
        stamp = stamped(db)
        assert stamp["cap_reached"] is True
        assert stamp["missing"] == ["output/b.md"]
        assert any("cap reached" in a for a in actions)

    @pytest.mark.asyncio
    async def test_cap_keeps_loop_job_terminal(self):
        """pending_review wedges a project loop — cap resolves it completed."""
        job = make_job(
            manifest=["output/b.md"],
            context_extra={
                "loop_id": "some-loop",
                "deliverable_gate": {
                    "passed": False,
                    "bounces": DELIVERABLE_GATE_BOUNCE_CAP,
                },
            },
        )
        new_status, _, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=make_db(),
            gitea=make_gitea([]),
            queue_resume=AsyncMock(),
        )
        assert bounced is False
        assert new_status == "completed"

    @pytest.mark.asyncio
    async def test_cap_leaves_reviewing_untouched(self):
        """At the cap a verification-enabled job proceeds to its critic,
        which reads the stamped report."""
        job = make_job(
            manifest=["output/b.md"],
            context_extra={
                "deliverable_gate": {
                    "passed": False,
                    "bounces": DELIVERABLE_GATE_BOUNCE_CAP,
                }
            },
        )
        new_status, _, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "reviewing",
            db=make_db(),
            gitea=make_gitea([]),
            queue_resume=AsyncMock(),
        )
        assert (new_status, bounced) == ("reviewing", False)

    @pytest.mark.asyncio
    async def test_reviewing_bounce_prevents_critic_spawn(self):
        """A bounced seal must not spawn the critic — the gate intercepts
        the reviewing lane (deterministic checks first, critic LLM second)."""
        job = make_job(manifest=["output/b.md"])
        queue_resume = AsyncMock()
        new_status, _, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "reviewing",
            db=make_db(),
            gitea=make_gitea([]),
            queue_resume=queue_resume,
        )
        assert bounced is True
        assert new_status is None
        queue_resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gitea_down_fails_open(self):
        job = make_job(manifest=["output/a.md"])
        db = make_db()
        queue_resume = AsyncMock()
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea([], initialized=False),
            queue_resume=queue_resume,
        )
        assert (new_status, bounced) == ("completed", False)
        queue_resume.assert_not_awaited()
        stamp = stamped(db)
        assert stamp["skipped"] is True
        assert "gitea" in stamp["reason"]
        assert any("skipped" in a for a in actions)

    @pytest.mark.asyncio
    async def test_no_manifest_is_a_noop(self):
        job = make_job(manifest=None)
        db = make_db()
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea([]),
            queue_resume=AsyncMock(),
        )
        assert (new_status, actions, bounced) == ("completed", [], False)
        db.merge_job_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_bounce_queue_falls_through_sealed(self):
        """A broken resume queue must not strand the job refused-but-unbounced."""
        job = make_job(manifest=["output/b.md"])
        queue_resume = AsyncMock(side_effect=RuntimeError("db down"))
        new_status, actions, bounced = await run_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=make_db(),
            gitea=make_gitea([]),
            queue_resume=queue_resume,
        )
        assert bounced is False
        assert new_status == "completed"
        assert any("FAILED to queue" in a for a in actions)

    @pytest.mark.asyncio
    async def test_completion_hook_delegates(self):
        """services.completion.apply_deliverable_gate is the thin hook the
        /complete handler calls — same result as the module entry point."""
        from orchestrator.services.completion import apply_deliverable_gate

        job = make_job(manifest=["output/a.md"])
        db = make_db()
        new_status, actions, bounced = await apply_deliverable_gate(
            job,
            completion_result(),
            "completed",
            db=db,
            gitea=make_gitea(["repo/output/a.md"]),
            queue_resume=AsyncMock(),
        )
        assert (new_status, bounced) == ("completed", False)
        assert stamped(db)["passed"] is True


# =============================================================================
# Create-path plumbing (JobCreate → context)
# =============================================================================


class TestCreatePlumbing:
    def test_jobcreate_accepts_and_normalizes(self):
        """The REST model carries the field; the create path stores the
        normalized manifest into context (both spellings collapse)."""
        import main as orchestrator_main

        body = orchestrator_main.JobCreate(
            description="ship it",
            required_deliverables=["repo/output/a.md", "./output/a.md", "kb:note"],
        )
        assert body.required_deliverables == [
            "repo/output/a.md",
            "./output/a.md",
            "kb:note",
        ]
        # The exact merge the endpoint performs:
        assert parse_required_deliverables(body.required_deliverables) == [
            "output/a.md",
            "kb:note",
        ]
