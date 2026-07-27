"""Verification ledger flow tests.

Covers the endpoint contract and the multi-round continuity that had zero
coverage before this work — including a regression test for the live incident
(job 52949749) where a fresh critic approved a byte-identical deliverable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ledger_state():
    """In-memory stand-in for jobs.context.verification_rounds.

    ``freeze_data`` defaults to ``None`` (matching a target row with no
    completion freeze yet); tests that need to pin the TARGET's HEAD commit
    set it explicitly.
    """
    return {"rounds": [], "freeze_data": None}


@pytest.fixture
def fake_db(ledger_state):
    db = MagicMock()

    async def _append(job_id, record):
        if any(
            r["critic_job_id"] == record["critic_job_id"]
            for r in ledger_state["rounds"]
        ):
            return 0
        ledger_state["rounds"].append(record)
        return len(ledger_state["rounds"])

    async def _get_job(job_id):
        return {
            "id": job_id,
            "context": {"verification_rounds": ledger_state["rounds"]},
            "freeze_data": ledger_state.get("freeze_data"),
        }

    db.append_verification_round = AsyncMock(side_effect=_append)
    db.get_job = AsyncMock(side_effect=_get_job)
    return db


class TestRecordVerificationRound:
    @pytest.mark.asyncio
    async def test_first_round_assigns_ids_and_computes_returned(
        self, fake_db, ledger_state
    ):
        from orchestrator.main import _record_verification_round_impl

        result = await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "missing source"}],
            dispositions=[],
            head_commit="aaa",
        )

        assert result["verdict"] == "returned"
        assert result["round"] == 1
        assert result["assigned"][0]["id"] == "F1"
        assert ledger_state["rounds"][0]["asserted_verdict"] == "returned"

    @pytest.mark.asyncio
    async def test_asserted_approved_loses_to_open_blocking_finding(
        self, fake_db, ledger_state
    ):
        """The rule that makes the incident impossible."""
        from orchestrator.main import _record_verification_round_impl

        ledger_state["rounds"].append(
            {
                "round": 1,
                "critic_job_id": "c1",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "head_commit": "aaa",
                "opened": [{"id": "F1", "severity": "high", "claim": "missing source"}],
                "dispositions": [],
                "ts": "t",
            }
        )

        result = await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c2",
            asserted_verdict="approved",
            opened=[],
            dispositions=[
                {"id": "F1", "disposition": "DISPUTED", "reason": "looks fine"}
            ],
            head_commit="aaa",
        )

        assert result["verdict"] == "returned"
        assert [f["id"] for f in result["open_findings"]] == ["F1"]

    @pytest.mark.asyncio
    async def test_returned_with_no_findings_raises_409(self, fake_db):
        from fastapi import HTTPException

        from orchestrator.main import _record_verification_round_impl

        with pytest.raises(HTTPException) as exc:
            await _record_verification_round_impl(
                postgres_db=fake_db,
                target_job_id="t1",
                critic_job_id="c1",
                asserted_verdict="returned",
                opened=[],
                dispositions=[],
                head_commit="aaa",
            )
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_disposition_raises_409(self, fake_db, ledger_state):
        from fastapi import HTTPException

        from orchestrator.main import _record_verification_round_impl

        ledger_state["rounds"].append(
            {
                "round": 1,
                "critic_job_id": "c1",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "head_commit": "aaa",
                "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
                "dispositions": [],
                "ts": "t",
            }
        )

        with pytest.raises(HTTPException) as exc:
            await _record_verification_round_impl(
                postgres_db=fake_db,
                target_job_id="t1",
                critic_job_id="c2",
                asserted_verdict="approved",
                opened=[],
                dispositions=[],
                head_commit="bbb",
            )
        assert exc.value.status_code == 409
        assert "F1" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_duplicate_append_returns_existing_verdict(
        self, fake_db, ledger_state
    ):
        from orchestrator.main import _record_verification_round_impl

        kwargs = dict(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "x"}],
            dispositions=[],
            head_commit="aaa",
        )
        first = await _record_verification_round_impl(**kwargs)
        second = await _record_verification_round_impl(**kwargs)

        assert second["verdict"] == first["verdict"]
        assert len(ledger_state["rounds"]) == 1

    @pytest.mark.asyncio
    async def test_empty_critic_job_id_raises_400(self, fake_db):
        """A falsy critic_job_id must never reach the ledger: the Task 1
        dedup guard keys on it, so two distinct empty-id rounds would
        collide and the second would be silently dropped as a "duplicate"
        of the first."""
        from fastapi import HTTPException

        from orchestrator.main import _record_verification_round_impl

        with pytest.raises(HTTPException) as exc:
            await _record_verification_round_impl(
                postgres_db=fake_db,
                target_job_id="t1",
                critic_job_id="",
                asserted_verdict="approved",
                opened=[],
                dispositions=[],
                head_commit="aaa",
            )
        assert exc.value.status_code == 400


class TestRecordVerificationRoundHeadCommitAuthority:
    """Amendment item 2: the recorded round's ``head_commit`` is
    server-authoritative from the TARGET's ``freeze_data``, not whatever the
    caller (critic) supplied.

    The critic runs on its own ``subjob/<id>/critic`` branch, so its own HEAD
    is a different thing from the target's — comparing it against the
    previous round's would be meaningless. The caller-supplied value is only
    a fallback for when the target has no freeze_data (or an older freeze
    that predates this field).
    """

    @pytest.mark.asyncio
    async def test_target_freeze_head_commit_wins_over_caller_supplied(
        self, fake_db, ledger_state
    ):
        from orchestrator.main import _record_verification_round_impl

        ledger_state["freeze_data"] = {"head_commit": "target-sha"}

        await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "x"}],
            dispositions=[],
            head_commit="critic-own-branch-sha",
        )

        assert ledger_state["rounds"][0]["head_commit"] == "target-sha"

    @pytest.mark.asyncio
    async def test_falls_back_to_caller_supplied_when_target_has_no_freeze_data(
        self, fake_db, ledger_state
    ):
        from orchestrator.main import _record_verification_round_impl

        ledger_state["freeze_data"] = None  # target hasn't completed yet

        await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "x"}],
            dispositions=[],
            head_commit="caller-sha",
        )

        assert ledger_state["rounds"][0]["head_commit"] == "caller-sha"

    @pytest.mark.asyncio
    async def test_falls_back_when_target_freeze_data_lacks_the_key(
        self, fake_db, ledger_state
    ):
        """An older freeze recorded before this field existed."""
        from orchestrator.main import _record_verification_round_impl

        ledger_state["freeze_data"] = {"summary": "pre-existing freeze, no head_commit"}

        await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "x"}],
            dispositions=[],
            head_commit="caller-sha-2",
        )

        assert ledger_state["rounds"][0]["head_commit"] == "caller-sha-2"


class TestVerificationGateDecision:
    def test_first_round_spawns(self):
        from orchestrator.main import _verification_gate_decision

        action, _ = _verification_gate_decision([], head_commit="aaa", max_rounds=3)
        assert action == "spawn"

    def test_unchanged_head_with_open_blocking_escalates(self):
        """THE INCIDENT REGRESSION TEST.

        Job 52949749 was returned twice, then re-submitted byte-identical and
        approved by a fresh critic. Identical HEAD + an open blocking finding
        must never reach a judge again.
        """
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {
                "round": 1,
                "critic_job_id": "c1",
                "head_commit": "aaa",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
                "dispositions": [],
                "ts": "t",
            }
        ]

        action, reason = _verification_gate_decision(
            rounds, head_commit="aaa", max_rounds=3
        )
        assert action == "escalate"
        assert "no progress" in reason.lower()

    def test_changed_head_with_open_blocking_spawns(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {
                "round": 1,
                "critic_job_id": "c1",
                "head_commit": "aaa",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
                "dispositions": [],
                "ts": "t",
            }
        ]

        action, _ = _verification_gate_decision(rounds, head_commit="bbb", max_rounds=3)
        assert action == "spawn"

    def test_cap_reached_with_open_blocking_escalates(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {
                "round": i,
                "critic_job_id": f"c{i}",
                "head_commit": f"h{i}",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "opened": [{"id": f"F{i}", "severity": "high", "claim": "c"}],
                "dispositions": [],
                "ts": "t",
            }
            for i in range(1, 4)
        ]

        action, reason = _verification_gate_decision(
            rounds, head_commit="h9", max_rounds=3
        )
        assert action == "escalate"
        assert "round limit" in reason.lower()

    def test_unlimited_rounds_never_hits_cap(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {
                "round": i,
                "critic_job_id": f"c{i}",
                "head_commit": f"h{i}",
                "verdict": "returned",
                "asserted_verdict": "returned",
                "opened": [{"id": f"F{i}", "severity": "high", "claim": "c"}],
                "dispositions": [],
                "ts": "t",
            }
            for i in range(1, 9)
        ]

        action, _ = _verification_gate_decision(rounds, head_commit="h9", max_rounds=0)
        assert action == "spawn"


class TestEscalateTarget:
    """``_escalate_target`` is status-aware: an ordinary job must never park a
    project-loop job on ``pending_review`` — the loop advance hook fires only
    on terminal statuses, so a parked loop job wedges the whole loop.
    """

    @pytest.mark.asyncio
    async def test_ordinary_job_escalates_to_pending_review(self, monkeypatch):
        import orchestrator.main as main_module
        from orchestrator.main import _escalate_target

        update_mock = AsyncMock()
        monkeypatch.setattr(main_module.postgres_db, "update_job_status", update_mock)

        job = {"id": "t1", "context": {}}
        status = await _escalate_target("t1", job, "no progress: still broken")

        assert status == "pending_review"
        update_mock.assert_awaited_once_with(
            "t1", status="pending_review", error_message="no progress: still broken"
        )

    @pytest.mark.asyncio
    async def test_loop_job_escalates_to_completed_not_pending_review(
        self, monkeypatch
    ):
        import orchestrator.main as main_module
        from orchestrator.main import _escalate_target

        update_mock = AsyncMock()
        monkeypatch.setattr(main_module.postgres_db, "update_job_status", update_mock)

        job = {"id": "t1", "context": {"loop_id": "loop-1"}}
        status = await _escalate_target("t1", job, "round limit reached")

        assert status == "completed"
        update_mock.assert_awaited_once_with(
            "t1", status="completed", error_message="round limit reached"
        )


def _make_completion_job(
    *,
    job_id: str = "target-1",
    freeze_head_commit,
    verification_rounds,
    max_rounds: int = 3,
    is_loop: bool = False,
):
    """A minimal job row that clears every guard in
    ``_trigger_verification_on_complete`` (not a subjob, verification
    enabled, freeze indicates job completion) with a controllable ledger and
    freeze ``head_commit`` — enough to exercise the REAL function end to end.
    """
    context: dict = {"verification_rounds": verification_rounds}
    if is_loop:
        context["loop_id"] = "loop-1"
    return {
        "id": job_id,
        "parent_job_id": None,
        "config_override": None,
        "resolved_config": {
            "verification": {"enabled": True, "max_rounds": max_rounds}
        },
        "freeze_data": {
            "freeze_type": "job_complete",
            "summary": "done",
            "deliverables": [],
            "confidence": 0.9,
            "head_commit": freeze_head_commit,
        },
        "context": context,
        "status": "reviewing",
        "description": "do the thing",
        "config_name": "developer",
        "project_id": None,
        "user_id": None,
        "repo_name": None,
        "branch_name": None,
    }


class TestTriggerVerificationHeadCommitWiring:
    """Amendment item 3: proves head_commit actually FLOWS from a completion
    freeze into the gate comparison inside the real
    ``_trigger_verification_on_complete`` — not just that the pure decision
    function behaves correctly when handed a value directly (that's what
    ``TestVerificationGateDecision`` above proves, and it stayed green while
    the wiring was dead in production, because nothing wrote ``head_commit``
    into a freeze at all).
    """

    @pytest.mark.asyncio
    async def test_same_head_commit_in_freeze_escalates_via_real_trigger(
        self, monkeypatch
    ):
        """THE INCIDENT REGRESSION TEST, exercised through the real trigger
        function instead of the decision function directly."""
        import orchestrator.main as main_module
        from orchestrator.main import _trigger_verification_on_complete

        update_mock = AsyncMock()
        monkeypatch.setattr(main_module.postgres_db, "update_job_status", update_mock)

        prior_round = {
            "round": 1,
            "critic_job_id": "c1",
            "head_commit": "aaa",
            "verdict": "returned",
            "asserted_verdict": "returned",
            "opened": [{"id": "F1", "severity": "high", "claim": "still broken"}],
            "dispositions": [],
            "ts": "t",
        }
        job = _make_completion_job(
            freeze_head_commit="aaa", verification_rounds=[prior_round]
        )
        actions: list[str] = []

        await _trigger_verification_on_complete(
            job, {"error": None, "should_stop": True}, actions
        )

        update_mock.assert_awaited_once()
        call_args = update_mock.call_args
        assert call_args.args[0] == "target-1"
        assert call_args.kwargs["status"] == "pending_review"
        assert "no progress" in call_args.kwargs["error_message"].lower()
        assert any("escalated" in a for a in actions)

    @pytest.mark.asyncio
    async def test_changed_head_commit_in_freeze_spawns_via_real_trigger(
        self, monkeypatch
    ):
        """Contrast case: proves the comparison is real, not a constant. An
        implementation that always escalates (or never reads head_commit at
        all) would pass the test above but must fail this one."""
        import orchestrator.main as main_module
        from orchestrator.main import _trigger_verification_on_complete

        update_mock = AsyncMock()
        monkeypatch.setattr(main_module.postgres_db, "update_job_status", update_mock)
        monkeypatch.setattr(
            main_module.postgres_db,
            "create_job",
            AsyncMock(return_value={"id": "critic-999"}),
        )
        monkeypatch.setattr(
            main_module, "_propagate_datasources_to_subjob", AsyncMock()
        )
        monkeypatch.setattr(main_module, "_trigger_dispatch", lambda: None)

        prior_round = {
            "round": 1,
            "critic_job_id": "c1",
            "head_commit": "aaa",
            "verdict": "returned",
            "asserted_verdict": "returned",
            "opened": [{"id": "F1", "severity": "high", "claim": "still broken"}],
            "dispositions": [],
            "ts": "t",
        }
        job = _make_completion_job(
            freeze_head_commit="bbb", verification_rounds=[prior_round]
        )
        actions: list[str] = []

        await _trigger_verification_on_complete(
            job, {"error": None, "should_stop": True}, actions
        )

        update_mock.assert_not_awaited()
        assert any("critic job" in a and "created" in a for a in actions)

    @pytest.mark.asyncio
    async def test_loop_job_no_progress_escalates_to_completed(self, monkeypatch):
        """Global constraint: a project-loop job must resolve ``completed``
        (never ``pending_review``) even on a no-progress escalation, or it
        wedges the loop's advance hook forever."""
        import orchestrator.main as main_module
        from orchestrator.main import _trigger_verification_on_complete

        update_mock = AsyncMock()
        monkeypatch.setattr(main_module.postgres_db, "update_job_status", update_mock)

        prior_round = {
            "round": 1,
            "critic_job_id": "c1",
            "head_commit": "aaa",
            "verdict": "returned",
            "asserted_verdict": "returned",
            "opened": [{"id": "F1", "severity": "high", "claim": "still broken"}],
            "dispositions": [],
            "ts": "t",
        }
        job = _make_completion_job(
            freeze_head_commit="aaa",
            verification_rounds=[prior_round],
            is_loop=True,
        )
        actions: list[str] = []

        await _trigger_verification_on_complete(
            job, {"error": None, "should_stop": True}, actions
        )

        update_mock.assert_awaited_once()
        assert update_mock.call_args.kwargs["status"] == "completed"
