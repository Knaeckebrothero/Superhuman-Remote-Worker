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
    """In-memory stand-in for jobs.context.verification_rounds."""
    return {"rounds": []}


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
