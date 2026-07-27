"""Unit tests for the pure verification-ledger logic.

No database, no network, no LLM — this module decides whether work is
approved, so it must be testable in isolation.
"""

from __future__ import annotations

from orchestrator.services.verification_ledger import (
    assign_ids,
    compute_verdict,
    fold_open_findings,
    is_blocking,
    next_finding_index,
)


def _round(n, opened=None, dispositions=None, **kw):
    return {
        "round": n,
        "critic_job_id": f"c{n}",
        "opened": opened or [],
        "dispositions": dispositions or [],
        **kw,
    }


class TestIsBlocking:
    def test_high_blocks(self):
        assert is_blocking({"severity": "high"}) is True

    def test_medium_and_low_do_not_block(self):
        assert is_blocking({"severity": "medium"}) is False
        assert is_blocking({"severity": "low"}) is False

    def test_unknown_severity_blocks(self):
        # Fail closed: an unrecognised severity must not silently pass the gate.
        assert is_blocking({"severity": "banana"}) is True
        assert is_blocking({}) is True


class TestFoldOpenFindings:
    def test_single_round_all_open(self):
        rounds = [_round(1, opened=[{"id": "F1", "severity": "high"}])]
        assert [f["id"] for f in fold_open_findings(rounds)] == ["F1"]

    def test_resolved_closes(self):
        rounds = [
            _round(1, opened=[{"id": "F1", "severity": "high"}]),
            _round(2, dispositions=[{"id": "F1", "disposition": "RESOLVED", "quote": "q"}]),
        ]
        assert fold_open_findings(rounds) == []

    def test_still_open_keeps_open(self):
        rounds = [
            _round(1, opened=[{"id": "F1", "severity": "high"}]),
            _round(2, dispositions=[{"id": "F1", "disposition": "STILL_OPEN"}]),
        ]
        assert [f["id"] for f in fold_open_findings(rounds)] == ["F1"]

    def test_disputed_keeps_open(self):
        # The incident: a later critic must not close a finding by re-judging it.
        rounds = [
            _round(1, opened=[{"id": "F1", "severity": "high"}]),
            _round(2, dispositions=[{"id": "F1", "disposition": "DISPUTED", "reason": "r"}]),
        ]
        open_findings = fold_open_findings(rounds)
        assert [f["id"] for f in open_findings] == ["F1"]
        assert open_findings[0]["disputed"] is True

    def test_accumulates_across_rounds(self):
        rounds = [
            _round(1, opened=[{"id": "F1", "severity": "high"}]),
            _round(2,
                   opened=[{"id": "F2", "severity": "high"}],
                   dispositions=[{"id": "F1", "disposition": "RESOLVED", "quote": "q"}]),
        ]
        assert [f["id"] for f in fold_open_findings(rounds)] == ["F2"]

    def test_empty_ledger(self):
        assert fold_open_findings([]) == []


class TestNextFindingIndex:
    def test_empty_starts_at_one(self):
        assert next_finding_index([]) == 1

    def test_continues_from_max(self):
        rounds = [_round(1, opened=[{"id": "F1"}, {"id": "F2"}]),
                  _round(2, opened=[{"id": "F3"}])]
        assert next_finding_index(rounds) == 4

    def test_ignores_malformed_ids(self):
        rounds = [_round(1, opened=[{"id": "F1"}, {"id": "bogus"}])]
        assert next_finding_index(rounds) == 2


class TestAssignIds:
    def test_assigns_sequential_ids(self):
        rounds = [_round(1, opened=[{"id": "F1", "severity": "high"}])]
        out = assign_ids([{"severity": "high", "claim": "a"},
                          {"severity": "low", "claim": "b"}], rounds)
        assert [f["id"] for f in out] == ["F2", "F3"]

    def test_overwrites_model_supplied_ids(self):
        # The critic never owns the ID namespace.
        out = assign_ids([{"id": "HACKED", "severity": "high", "claim": "a"}], [])
        assert out[0]["id"] == "F1"

    def test_defaults_missing_severity_to_high(self):
        out = assign_ids([{"claim": "a"}], [])
        assert out[0]["severity"] == "high"


class TestComputeVerdict:
    def test_no_findings_approves(self):
        assert compute_verdict([]) == "approved"

    def test_open_high_returns(self):
        assert compute_verdict([{"id": "F1", "severity": "high"}]) == "returned"

    def test_only_non_blocking_approves(self):
        assert compute_verdict([{"id": "F1", "severity": "medium"},
                                {"id": "F2", "severity": "low"}]) == "approved"

    def test_mixed_returns(self):
        assert compute_verdict([{"id": "F1", "severity": "low"},
                                {"id": "F2", "severity": "high"}]) == "returned"
