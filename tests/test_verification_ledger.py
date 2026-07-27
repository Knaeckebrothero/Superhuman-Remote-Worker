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
    """The server may compute STRICTER than the model asserted, never laxer.

    Two independent grounds for 'returned': an open BLOCKING finding (which
    overrides an asserted approval — the rule that makes the original incident
    impossible), or the critic explicitly asserting 'returned' while anything
    is still open (which respects a deliberate medium/low-severity return
    instead of silently upgrading it to an approval).
    """

    def test_no_findings_approves(self):
        assert compute_verdict("approved", []) == "approved"

    def test_open_high_returns(self):
        # asserted='approved' on purpose: the BLOCKING rule does the work.
        assert compute_verdict("approved", [{"id": "F1", "severity": "high"}]) == "returned"

    def test_only_non_blocking_approves(self):
        assert compute_verdict("approved", [{"id": "F1", "severity": "medium"},
                                            {"id": "F2", "severity": "low"}]) == "approved"

    def test_mixed_returns(self):
        assert compute_verdict("approved", [{"id": "F1", "severity": "low"},
                                            {"id": "F2", "severity": "high"}]) == "returned"

    # -- the four (asserted) x (blocking) combinations -----------------------

    def test_asserted_approved_with_blocking_returns(self):
        assert compute_verdict(
            "approved", [{"id": "F1", "severity": "high"}]
        ) == "returned"

    def test_asserted_approved_without_blocking_approves(self):
        assert compute_verdict(
            "approved", [{"id": "F1", "severity": "medium"}]
        ) == "approved"

    def test_asserted_returned_with_blocking_returns(self):
        assert compute_verdict(
            "returned", [{"id": "F1", "severity": "high"}]
        ) == "returned"

    def test_asserted_returned_without_blocking_still_returns(self):
        """The defect: an explicit 'returned' at medium/low severity was
        silently recorded as 'approved', advancing the target."""
        assert compute_verdict(
            "returned", [{"id": "F1", "severity": "medium"}]
        ) == "returned"

    def test_asserted_returned_with_nothing_open_approves(self):
        """Nothing is open, so there is nothing to return on. Unreachable in
        practice (``validate_verdict_call`` rejects the call first), but the
        computation must still be total."""
        assert compute_verdict("returned", []) == "approved"

    def test_asserted_verdict_is_case_insensitive(self):
        assert compute_verdict(
            "RETURNED", [{"id": "F1", "severity": "low"}]
        ) == "returned"


from orchestrator.services.verification_ledger import (  # noqa: E402
    escalation_status,
    render_prior_findings,
    validate_dispositions,
    validate_verdict_call,
)

OPEN_HIGH = [{"id": "F1", "severity": "high", "claim": "missing source"}]


class TestValidateDispositions:
    def test_valid_resolved(self):
        assert validate_dispositions(
            [{"id": "F1", "disposition": "RESOLVED", "quote": "new text"}], OPEN_HIGH
        ) == []

    def test_resolved_without_quote_rejected(self):
        errors = validate_dispositions(
            [{"id": "F1", "disposition": "RESOLVED"}], OPEN_HIGH
        )
        assert len(errors) == 1
        assert "quote" in errors[0].lower()

    def test_disputed_without_reason_rejected(self):
        errors = validate_dispositions(
            [{"id": "F1", "disposition": "DISPUTED"}], OPEN_HIGH
        )
        assert len(errors) == 1
        assert "reason" in errors[0].lower()

    def test_missing_disposition_for_open_blocking_rejected(self):
        errors = validate_dispositions([], OPEN_HIGH)
        assert len(errors) == 1
        assert "F1" in errors[0]

    def test_unknown_id_rejected(self):
        errors = validate_dispositions(
            [{"id": "F1", "disposition": "STILL_OPEN"},
             {"id": "F99", "disposition": "RESOLVED", "quote": "q"}], OPEN_HIGH
        )
        assert any("F99" in e for e in errors)

    def test_unknown_disposition_value_rejected(self):
        errors = validate_dispositions(
            [{"id": "F1", "disposition": "PROBABLY_FINE"}], OPEN_HIGH
        )
        assert any("PROBABLY_FINE" in e for e in errors)

    def test_non_blocking_findings_need_no_disposition(self):
        # Otherwise low-severity nits accumulate and must be re-answered forever.
        open_low = [{"id": "F1", "severity": "low", "claim": "typo"}]
        assert validate_dispositions([], open_low) == []

    def test_duplicate_id_rejected(self):
        # Fix for review finding 1: a second disposition for an id already
        # processed in this call must be rejected, not silently accepted as
        # a last-write-wins override. Without the guard, a fabricated
        # RESOLVED riding after a genuine DISPUTED slips through with no
        # error at all.
        dupes = [
            {"id": "F1", "disposition": "DISPUTED", "reason": "still checking"},
            {"id": "F1", "disposition": "RESOLVED", "quote": "fixed it"},
        ]
        errors = validate_dispositions(dupes, OPEN_HIGH)
        assert len(errors) == 1
        assert "F1" in errors[0]

        # Because the call is rejected, a correct caller never persists these
        # dispositions into the ledger — so folding rounds that never
        # incorporated the invalid call must leave the finding open, not
        # silently closed by the fabricated RESOLVED.
        rounds = [_round(1, opened=OPEN_HIGH)]
        assert [f["id"] for f in fold_open_findings(rounds)] == ["F1"]


class TestValidateVerdictCall:
    def test_returned_with_nothing_open_at_all_rejected(self):
        # The incident: `issues: "[]"` recorded as "Issues: 0, Severity: high".
        errors = validate_verdict_call("returned", [], [])
        assert len(errors) == 1
        assert "no findings" in errors[0].lower()

    def test_returned_with_findings_ok(self):
        assert validate_verdict_call(
            "returned", [{"claim": "x", "severity": "high"}], []
        ) == []

    def test_approved_with_no_findings_ok(self):
        assert validate_verdict_call("approved", [], []) == []

    def test_returned_with_no_new_findings_but_prior_open_is_ok(self):
        """The most common round-2 shape: nothing NEW, but F1 is still open.

        Rejecting this made ``return_job_with_feedback`` uncallable for that
        critic and pushed it toward ``approve_job`` — the wrong pressure
        direction for a fail-closed gate.
        """
        assert validate_verdict_call("returned", [], OPEN_HIGH) == []

    def test_returned_with_no_new_findings_but_prior_open_nonblocking_is_ok(self):
        open_medium = [{"id": "F1", "severity": "medium", "claim": "nit"}]
        assert validate_verdict_call("returned", [], open_medium) == []

    def test_rejection_message_asks_for_a_finding_not_an_approval(self):
        """The error is the model's only correction signal. It must push
        toward describing the problem, not toward approving the job."""
        errors = validate_verdict_call("returned", [], [])
        assert "opened" in errors[0]
        assert "approve instead" not in errors[0].lower()


class TestRenderPriorFindings:
    def test_empty_states_none_open(self):
        assert "No open findings" in render_prior_findings([])

    def test_lists_ids_and_claims(self):
        text = render_prior_findings(
            [{"id": "F1", "severity": "high", "claim": "missing source",
              "evidence": "line 44", "opened_round": 1, "disputed": False}]
        )
        assert "F1" in text
        assert "missing source" in text
        assert "RESOLVED" in text  # the instruction block explains dispositions

    def test_marks_disputed(self):
        # Fix for review finding 2: assert the actual per-finding marker, not
        # "DISPUTED", which also appears unconditionally in the static
        # trailing instructions block whenever open_findings is non-empty —
        # that weaker assertion would still pass even with the per-finding
        # flag logic deleted entirely.
        text = render_prior_findings(
            [{"id": "F1", "severity": "high", "claim": "c", "opened_round": 1,
              "disputed": True, "dispute_reason": "disagree"}]
        )
        assert "*(you previously disputed this)*" in text

    def test_does_not_mark_undisputed(self):
        # Contrasting case: without this, test_marks_disputed alone can't
        # actually fail on a deleted/reversed flag condition.
        text = render_prior_findings(
            [{"id": "F1", "severity": "high", "claim": "c", "opened_round": 1,
              "disputed": False}]
        )
        assert "*(you previously disputed this)*" not in text


class TestEscalationStatus:
    def test_ordinary_job_goes_to_human_gate(self):
        assert escalation_status(is_loop_job=False) == "pending_review"

    def test_loop_job_must_not_park(self):
        # A pending_review loop job wedges the loop forever: the advance hook
        # only fires on terminal statuses.
        assert escalation_status(is_loop_job=True) == "completed"
