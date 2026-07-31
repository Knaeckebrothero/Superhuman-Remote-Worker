# Fail-Closed Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fail-open verification gate with a durable append-only findings ledger on the target job, so a lost verdict can never become an approval.

**Architecture:** All verification state moves to `jobs.context.verification_rounds` on the **target** job, written by a single-statement atomic append. Job status and `freeze_data` become projections. All pure logic (folding the open set, computing the verdict, validating dispositions, assigning IDs, rendering the brief) lives in one new I/O-free module so it is unit-testable without a database. A fresh critic is spawned every round and receives the open findings by ID; it must disposition each one, and the orchestrator computes the verdict from the resulting open set rather than trusting the model's assertion.

**Tech Stack:** Python 3.12 (CI gate), FastAPI, asyncpg, Postgres 15, pytest + pytest-asyncio, testcontainers for real-Postgres tests, LangGraph (agent side).

**Spec:** `docs/superpowers/specs/2026-07-27-verification-fail-closed-design.md` (commit `e20e9a4c`)

## Global Constraints

Copied from the spec. Every task's requirements implicitly include these.

- **No agent-graph changes.** `src/graph.py` and `src/core/state.py` are untouched by this plan.
- **Verdict tools stay strategic-phase-only** (`phases: ["strategic"]`). Changing this without changing the critic prompt reproduces a 631-turn deadlock.
- **`evaluation` tools are injected via `config_override` only** — never added to `config/experts/critic/config.yaml`.
- **The critic factory keeps its `autonomy: "full"` hardcode**; safety comes from `runner_kind='lifecycle'` elevating only `autonomy_ceiling`.
- **A `paused` critic is treated as live.** The sweeper extension in Task 11 adds `'waiting'` ONLY — never `'paused'`.
- **Critic output is never merged** into the parent's deliverable.
- **`context.verification_target` is the canonical critic discriminator.**
- **Severity taxonomy is closed and ordered:** `low` < `medium` < `high`. `BLOCKING_SEVERITY = "high"`.
- **asyncpg returns JSONB as `str`** on the app pool (no codec registered). Every read of `jobs.context` must coerce. Use exactly one helper; do not add a new ad-hoc parser.
- **Escalate** means: hand to a human without approving — `pending_review` for an ordinary job, `completed` with findings in `error_message` for a project-loop job. Never approval.
- Local pytest is noisy on Python 3.14; **CI (3.12) is the gate**. Run targeted tests locally.

---

## File Structure

**Create:**
- `orchestrator/services/verification_ledger.py` — all pure ledger logic, zero I/O. The testable core.
- `tests/test_verification_ledger.py` — unit tests for the above.
- `tests/test_verification_flow.py` — multi-round continuity + the incident regression test.

**Modify:**
- `orchestrator/database/postgres.py` — `append_verification_round`; watchdog + sweeper SQL.
- `orchestrator/main.py` — new endpoint; `_trigger_verification_on_complete`; `_handle_critic_verdict_on_complete`; critic `config_override`.
- `orchestrator/services/completion.py` — `format_verification_instructions` gains `prior_findings`.
- `config/experts/critic/verification_instructions.md` — `{prior_findings}` block.
- `src/tools/evaluation/evaluation_tools.py` — both verdict tools.
- `src/api/orchestrator_client.py` — `record_verification_round`.
- `src/core/phase.py` — returned verdict freezes `completed`; delete implicit approval.
- `tests/test_atomic_job_context.py` — append tests.
- `tests/test_critic_loop.py`, `tests/test_complete_job_endpoint.py` — repair broken tests.

The `verification_ledger.py` boundary is the key decision: the endpoint and the orchestrator hooks become thin I/O wrappers around pure functions, so the logic that decides whether work is approved is testable without a database, a network, or an LLM.

---

## Data Shapes

Every task below uses exactly these shapes. Do not vary them.

```python
# finding (as stored in a round's "opened" list)
{"id": "F1", "severity": "high", "claim": "...", "evidence": "..."}

# disposition
{"id": "F1", "disposition": "RESOLVED", "quote": "..."}    # RESOLVED requires quote
{"id": "F2", "disposition": "STILL_OPEN"}
{"id": "F3", "disposition": "DISPUTED", "reason": "..."}   # DISPUTED requires reason

# round record (one element of context.verification_rounds)
{"round": 2,
 "critic_job_id": "uuid-str",
 "head_commit": "a8117788" | None,
 "verdict": "returned",             # COMPUTED
 "asserted_verdict": "approved",    # what the model claimed
 "opened": [finding, ...],
 "dispositions": [disposition, ...],
 "ts": "2026-07-27T12:00:00+00:00"}
```

---

### Task 1: Atomic ledger append

**Files:**
- Modify: `orchestrator/database/postgres.py` (add after `append_queued_reply`, which ends at :1897)
- Test: `tests/test_atomic_job_context.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PostgresDB.append_verification_round(job_id: str, record: Dict[str, Any]) -> int` — returns the new array length (the round count), or `0` if the job was not found or the record was a duplicate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_atomic_job_context.py`:

```python
class TestAppendVerificationRound:
    @pytest.mark.asyncio
    async def test_appends_and_returns_length(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {})

        assert await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"}) == 1
        assert await db.append_verification_round(jid, {"round": 2, "critic_job_id": "c2"}) == 2
        rounds = (await _read_ctx(db, jid))["verification_rounds"]
        assert [r["critic_job_id"] for r in rounds] == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_null_column_creates_array(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, None)

        assert await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"}) == 1

    @pytest.mark.asyncio
    async def test_preserves_other_keys(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"keep": "me"})

        await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
        assert (await _read_ctx(db, jid))["keep"] == "me"

    @pytest.mark.asyncio
    async def test_duplicate_critic_id_is_noop(self, db):
        # A retried /complete must not append a second round for the same critic.
        jid = str(uuid4())
        await _insert_job(db, jid, {})

        assert await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"}) == 1
        assert await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"}) == 0
        assert len((await _read_ctx(db, jid))["verification_rounds"]) == 1

    @pytest.mark.asyncio
    async def test_concurrent_appends_all_land(self, db):
        # The property a mock cannot defend: N racing single-statement appends
        # are lost-update-immune under READ COMMITTED.
        jid = str(uuid4())
        await _insert_job(db, jid, {})
        n = 10

        await asyncio.gather(
            *(db.append_verification_round(jid, {"round": i, "critic_job_id": f"c{i}"})
              for i in range(n))
        )

        assert len((await _read_ctx(db, jid))["verification_rounds"]) == n

    @pytest.mark.asyncio
    async def test_missing_job_returns_zero(self, db):
        assert await db.append_verification_round(str(uuid4()), {"critic_job_id": "c1"}) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_atomic_job_context.py::TestAppendVerificationRound -v`
Expected: FAIL — `AttributeError: 'PostgresDB' object has no attribute 'append_verification_round'`

- [ ] **Step 3: Implement**

Add to `orchestrator/database/postgres.py` immediately after `append_queued_reply`:

```python
    async def append_verification_round(
        self, job_id: str, record: Dict[str, Any]
    ) -> int:
        """Atomically append one verification round record to the TARGET job.

        Single-statement ``jsonb_set(..., arr || $1)`` so two orchestrator
        replicas racing on the same target both land (same property as
        ``append_queued_reply``; see HF-3 in docs/features/database_roadmap.md).

        Idempotent on ``critic_job_id``: a retried ``/complete`` for the same
        critic is a no-op, because a duplicate round would corrupt the round
        counter (round number is the array length).

        Args:
            job_id: TARGET job UUID as string
            record: Round record (see the plan's Data Shapes)

        Returns:
            New array length, or 0 if the job is missing, the id is invalid,
            or the append was suppressed as a duplicate.
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return 0

        critic_job_id = str(record.get("critic_job_id") or "")

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "        COALESCE(context, '{}'::jsonb), "
            "        '{verification_rounds}', "
            "        COALESCE(context->'verification_rounds', '[]'::jsonb) || $1::jsonb"
            "    ), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2 "
            "  AND NOT (COALESCE(context->'verification_rounds', '[]'::jsonb) "
            "           @> $3::jsonb) "
            "RETURNING jsonb_array_length(context->'verification_rounds')"
        )
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                query,
                json_module.dumps([record]),
                uuid_val,
                json_module.dumps([{"critic_job_id": critic_job_id}]),
            )

        return int(row[0]) if row else 0
```

Note two traps this avoids: the payload is wrapped in a **list** (`json.dumps([record])`) because `||` splices an array but nests an object — appending a bare object works, but wrapping is explicit and matches the containment guard's shape. The guard uses `@>` (containment) against `[{"critic_job_id": ...}]`, which matches any array element having that key/value.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_atomic_job_context.py::TestAppendVerificationRound -v`
Expected: PASS (6 tests). Skips cleanly if no container runtime.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/postgres.py tests/test_atomic_job_context.py
git commit -m "feat(verification): atomic append_verification_round ledger helper"
```

---

### Task 2: Ledger fold, ID assignment, and verdict computation

**Files:**
- Create: `orchestrator/services/verification_ledger.py`
- Test: `tests/test_verification_ledger.py`

**Interfaces:**
- Consumes: nothing (pure module, no I/O, no imports from `main.py`).
- Produces:
  - `SEVERITY_ORDER: dict[str, int]`, `BLOCKING_SEVERITY: str`
  - `is_blocking(finding: dict) -> bool`
  - `fold_open_findings(rounds: list[dict]) -> list[dict]`
  - `next_finding_index(rounds: list[dict]) -> int`
  - `assign_ids(opened: list[dict], rounds: list[dict]) -> list[dict]`
  - `compute_verdict(open_findings: list[dict]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verification_ledger.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verification_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.verification_ledger'`

- [ ] **Step 3: Implement**

Create `orchestrator/services/verification_ledger.py`:

```python
"""Pure logic for the verification findings ledger.

No I/O. The ledger lives at ``jobs.context.verification_rounds`` on the TARGET
job and is the single source of truth for verification; job status and
``freeze_data`` are projections of it.

Findings are never mutated — the open set is a fold over rounds. A finding is
open unless a later round dispositioned it RESOLVED. DISPUTED records
disagreement without closing, so a fresh critic cannot close a predecessor's
finding by re-judging it.

Design: docs/superpowers/specs/2026-07-27-verification-fail-closed-design.md
Incident: docs/issues/verification_round_reset_spawns_blind_critic.md
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

SEVERITY_ORDER: Dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Named rather than inlined so it can become a `verification.blocking_severity`
# config key later. Adding that knob is out of scope for this work.
BLOCKING_SEVERITY = "high"

_FINDING_ID_RE = re.compile(r"^F(\d+)$")


def is_blocking(finding: Dict[str, Any]) -> bool:
    """True when a finding is severe enough to gate approval.

    Fails closed: an unrecognised or missing severity blocks. A gate whose
    unknown case passes is the defect this whole design exists to remove.
    """
    severity = str(finding.get("severity", "")).lower()
    if severity not in SEVERITY_ORDER:
        return True
    return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[BLOCKING_SEVERITY]


def fold_open_findings(rounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute the currently-open findings by folding all rounds in order.

    Returns copies, each carrying ``opened_round``; a finding left DISPUTED
    additionally carries ``disputed: True`` so the human gate can surface it.
    """
    open_by_id: Dict[str, Dict[str, Any]] = {}

    for rnd in rounds:
        for finding in rnd.get("opened") or []:
            fid = finding.get("id")
            if not fid:
                continue
            entry = dict(finding)
            entry["opened_round"] = rnd.get("round")
            entry["disputed"] = False
            open_by_id[fid] = entry

        for disp in rnd.get("dispositions") or []:
            fid = disp.get("id")
            if fid not in open_by_id:
                continue
            kind = str(disp.get("disposition", "")).upper()
            if kind == "RESOLVED":
                del open_by_id[fid]
            elif kind == "DISPUTED":
                open_by_id[fid]["disputed"] = True
                open_by_id[fid]["dispute_reason"] = disp.get("reason", "")

    return list(open_by_id.values())


def next_finding_index(rounds: List[Dict[str, Any]]) -> int:
    """Next free numeric suffix for a server-assigned finding ID."""
    highest = 0
    for rnd in rounds:
        for finding in rnd.get("opened") or []:
            match = _FINDING_ID_RE.match(str(finding.get("id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def assign_ids(
    opened: List[Dict[str, Any]], rounds: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Assign server-owned IDs to newly proposed findings.

    Any model-supplied ``id`` is discarded: the critic proposes claims, the
    server owns the namespace, so a critic cannot renumber or silently drop a
    predecessor's finding.
    """
    index = next_finding_index(rounds)
    out: List[Dict[str, Any]] = []
    for finding in opened:
        entry = dict(finding)
        entry["id"] = f"F{index}"
        severity = str(entry.get("severity", "")).lower()
        entry["severity"] = severity if severity in SEVERITY_ORDER else "high"
        out.append(entry)
        index += 1
    return out


def compute_verdict(open_findings: List[Dict[str, Any]]) -> str:
    """Derive the verdict from the open set. Never trusts a model assertion."""
    return "returned" if any(is_blocking(f) for f in open_findings) else "approved"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verification_ledger.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/verification_ledger.py tests/test_verification_ledger.py
git commit -m "feat(verification): pure ledger fold, ID assignment, verdict computation"
```

---

### Task 3: Disposition validation and brief rendering

**Files:**
- Modify: `orchestrator/services/verification_ledger.py`
- Test: `tests/test_verification_ledger.py`

**Interfaces:**
- Consumes: `fold_open_findings`, `is_blocking` from Task 2.
- Produces:
  - `validate_dispositions(dispositions: list[dict], open_findings: list[dict]) -> list[str]` — returns human-readable error strings; empty list means valid.
  - `validate_verdict_call(asserted: str, opened: list[dict]) -> list[str]`
  - `render_prior_findings(open_findings: list[dict]) -> str`
  - `escalation_status(is_loop_job: bool) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verification_ledger.py`:

```python
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


class TestValidateVerdictCall:
    def test_returned_with_no_findings_rejected(self):
        # The incident: `issues: "[]"` recorded as "Issues: 0, Severity: high".
        errors = validate_verdict_call("returned", [])
        assert len(errors) == 1
        assert "no findings" in errors[0].lower()

    def test_returned_with_findings_ok(self):
        assert validate_verdict_call("returned", [{"claim": "x", "severity": "high"}]) == []

    def test_approved_with_no_findings_ok(self):
        assert validate_verdict_call("approved", []) == []


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
        text = render_prior_findings(
            [{"id": "F1", "severity": "high", "claim": "c", "opened_round": 1,
              "disputed": True, "dispute_reason": "disagree"}]
        )
        assert "DISPUTED" in text


class TestEscalationStatus:
    def test_ordinary_job_goes_to_human_gate(self):
        assert escalation_status(is_loop_job=False) == "pending_review"

    def test_loop_job_must_not_park(self):
        # A pending_review loop job wedges the loop forever: the advance hook
        # only fires on terminal statuses.
        assert escalation_status(is_loop_job=True) == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verification_ledger.py -v`
Expected: FAIL — `ImportError: cannot import name 'validate_dispositions'`

- [ ] **Step 3: Implement**

Append to `orchestrator/services/verification_ledger.py`:

```python
_VALID_DISPOSITIONS = {"RESOLVED", "STILL_OPEN", "DISPUTED"}


def validate_dispositions(
    dispositions: List[Dict[str, Any]], open_findings: List[Dict[str, Any]]
) -> List[str]:
    """Validate a critic's dispositions against the currently-open findings.

    Returns human-readable errors (empty list = valid). These are surfaced
    verbatim to the model so it can correct itself, so they must name the
    offending finding ID.

    Disposition is required for BLOCKING findings only — non-blocking findings
    are advisory and would otherwise accumulate across rounds forever.
    """
    errors: List[str] = []
    open_ids = {f["id"] for f in open_findings if f.get("id")}
    blocking_ids = {f["id"] for f in open_findings if f.get("id") and is_blocking(f)}
    seen: set[str] = set()

    for disp in dispositions:
        fid = disp.get("id")
        if fid not in open_ids:
            errors.append(
                f"Unknown finding id {fid!r}: there is no open finding with that id."
            )
            continue
        seen.add(fid)
        kind = str(disp.get("disposition", "")).upper()
        if kind not in _VALID_DISPOSITIONS:
            errors.append(
                f"{fid}: unknown disposition {disp.get('disposition')!r}. "
                f"Use one of: {', '.join(sorted(_VALID_DISPOSITIONS))}."
            )
        elif kind == "RESOLVED" and not str(disp.get("quote", "")).strip():
            errors.append(
                f"{fid}: RESOLVED requires a `quote` from the NEW deliverable "
                f"showing the finding was addressed."
            )
        elif kind == "DISPUTED" and not str(disp.get("reason", "")).strip():
            errors.append(f"{fid}: DISPUTED requires a `reason`.")

    for fid in sorted(blocking_ids - seen):
        errors.append(
            f"{fid}: no disposition supplied. Every open blocking finding must be "
            f"marked RESOLVED, STILL_OPEN, or DISPUTED."
        )

    return errors


def validate_verdict_call(asserted: str, opened: List[Dict[str, Any]]) -> List[str]:
    """Reject internally inconsistent verdict calls at the tool boundary.

    A JSON schema cannot express this: ``{"issues": [], "severity": "high"}`` is
    a structurally valid document, and the live incident recorded it as
    "Issues: 0, Severity: high" without complaint.
    """
    if str(asserted).lower() == "returned" and not opened:
        return [
            "Cannot return a job with no findings. Supply at least one finding "
            "in `opened`, or approve instead."
        ]
    return []


def render_prior_findings(open_findings: List[Dict[str, Any]]) -> str:
    """Render the open findings block injected into a fresh critic's brief."""
    if not open_findings:
        return (
            "No open findings from previous rounds. This is a first review — "
            "evaluate the deliverables against the original requirements."
        )

    lines = [
        "The following findings were left OPEN by previous review rounds. "
        "You MUST disposition EVERY one of them by id.",
        "",
        "**You may not close a finding by re-judging it.** A finding closes only "
        "if you can quote text from the CURRENT deliverable that addresses it.",
        "",
    ]
    for finding in sorted(open_findings, key=lambda f: f.get("id", "")):
        flag = " *(you previously disputed this)*" if finding.get("disputed") else ""
        lines.append(
            f"- **{finding.get('id')}** [{finding.get('severity')}, opened round "
            f"{finding.get('opened_round')}]{flag}: {finding.get('claim', '')}"
        )
        evidence = str(finding.get("evidence", "")).strip()
        if evidence:
            lines.append(f"  - Evidence when opened: {evidence}")

    lines += [
        "",
        "For each, supply one disposition:",
        "- `RESOLVED` — include `quote`: the text in the CURRENT deliverable that "
        "addresses it.",
        "- `STILL_OPEN` — not addressed.",
        "- `DISPUTED` — include `reason`. **This does not close the finding**; it "
        "flags it for a human.",
    ]
    return "\n".join(lines)


def escalation_status(is_loop_job: bool) -> str:
    """Terminal status for an escalation (no verdict / cap / no progress).

    Project-loop jobs must NEVER land on ``pending_review``: the loop advance
    hook fires only on terminal statuses, so a parked loop job wedges the whole
    loop. They resolve to ``completed`` with the findings recorded in
    ``error_message`` for the retro instead.
    """
    return "completed" if is_loop_job else "pending_review"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verification_ledger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/verification_ledger.py tests/test_verification_ledger.py
git commit -m "feat(verification): disposition validation, brief rendering, escalation status"
```

---

### Task 4: The verification-rounds endpoint

**Files:**
- Modify: `orchestrator/main.py` (add near the other internal agent-facing endpoints; `store_citation_snapshot` at :17943 is the closest pattern)
- Test: `tests/test_verification_flow.py` (create)

**Interfaces:**
- Consumes: `append_verification_round` (Task 1); `fold_open_findings`, `assign_ids`, `compute_verdict` (Task 2); `validate_dispositions`, `validate_verdict_call` (Task 3).
- Produces: `POST /api/jobs/{target_job_id}/verification/rounds` returning `{"verdict": str, "round": int, "assigned": [finding], "open_findings": [finding]}`; HTTP 409 `{"detail": {"errors": [str]}}` on validation failure.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verification_flow.py`:

```python
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
        if any(r["critic_job_id"] == record["critic_job_id"] for r in ledger_state["rounds"]):
            return 0
        ledger_state["rounds"].append(record)
        return len(ledger_state["rounds"])

    async def _get_job(job_id):
        return {"id": job_id, "context": {"verification_rounds": ledger_state["rounds"]}}

    db.append_verification_round = AsyncMock(side_effect=_append)
    db.get_job = AsyncMock(side_effect=_get_job)
    return db


class TestRecordVerificationRound:
    @pytest.mark.asyncio
    async def test_first_round_assigns_ids_and_computes_returned(self, fake_db, ledger_state):
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
    async def test_asserted_approved_loses_to_open_blocking_finding(self, fake_db, ledger_state):
        """The rule that makes the incident impossible."""
        from orchestrator.main import _record_verification_round_impl

        ledger_state["rounds"].append({
            "round": 1, "critic_job_id": "c1", "verdict": "returned",
            "asserted_verdict": "returned", "head_commit": "aaa",
            "opened": [{"id": "F1", "severity": "high", "claim": "missing source"}],
            "dispositions": [], "ts": "t",
        })

        result = await _record_verification_round_impl(
            postgres_db=fake_db,
            target_job_id="t1",
            critic_job_id="c2",
            asserted_verdict="approved",
            opened=[],
            dispositions=[{"id": "F1", "disposition": "DISPUTED", "reason": "looks fine"}],
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
                postgres_db=fake_db, target_job_id="t1", critic_job_id="c1",
                asserted_verdict="returned", opened=[], dispositions=[],
                head_commit="aaa",
            )
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_disposition_raises_409(self, fake_db, ledger_state):
        from fastapi import HTTPException

        from orchestrator.main import _record_verification_round_impl

        ledger_state["rounds"].append({
            "round": 1, "critic_job_id": "c1", "verdict": "returned",
            "asserted_verdict": "returned", "head_commit": "aaa",
            "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
            "dispositions": [], "ts": "t",
        })

        with pytest.raises(HTTPException) as exc:
            await _record_verification_round_impl(
                postgres_db=fake_db, target_job_id="t1", critic_job_id="c2",
                asserted_verdict="approved", opened=[], dispositions=[],
                head_commit="bbb",
            )
        assert exc.value.status_code == 409
        assert "F1" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_duplicate_append_returns_existing_verdict(self, fake_db, ledger_state):
        from orchestrator.main import _record_verification_round_impl

        kwargs = dict(
            postgres_db=fake_db, target_job_id="t1", critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "x"}],
            dispositions=[], head_commit="aaa",
        )
        first = await _record_verification_round_impl(**kwargs)
        second = await _record_verification_round_impl(**kwargs)

        assert second["verdict"] == first["verdict"]
        assert len(ledger_state["rounds"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verification_flow.py -v`
Expected: FAIL — `ImportError: cannot import name '_record_verification_round_impl'`

- [ ] **Step 3: Implement**

Add to `orchestrator/main.py`. Put `_record_verification_round_impl` immediately above the route so it is importable and testable without HTTP:

```python
async def _record_verification_round_impl(
    *,
    postgres_db: Any,
    target_job_id: str,
    critic_job_id: str,
    asserted_verdict: str,
    opened: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    head_commit: str | None,
) -> dict[str, Any]:
    """Validate, compute, and durably append one verification round.

    Split out of the route so the gate logic is testable without HTTP. Raises
    HTTPException(409) with the model-facing errors on invalid input.
    """
    from services.verification_ledger import (
        assign_ids,
        compute_verdict,
        fold_open_findings,
        validate_dispositions,
        validate_verdict_call,
    )

    target = await postgres_db.get_job(target_job_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"Job {target_job_id} not found")

    rounds = _verification_rounds(target)
    open_before = fold_open_findings(rounds)

    errors = validate_verdict_call(asserted_verdict, opened)
    errors += validate_dispositions(dispositions, open_before)
    if errors:
        raise HTTPException(status_code=409, detail={"errors": errors})

    assigned = assign_ids(opened, rounds)
    record = {
        "round": len(rounds) + 1,
        "critic_job_id": critic_job_id,
        "head_commit": head_commit,
        "asserted_verdict": str(asserted_verdict).lower(),
        "opened": assigned,
        "dispositions": dispositions,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    open_after = fold_open_findings(rounds + [record])
    record["verdict"] = compute_verdict(open_after)

    if record["verdict"] != record["asserted_verdict"]:
        # Free, direct measure of critic quality: how often a critic tries to
        # approve over its own open findings. Previously unobservable.
        logger.warning(
            "Verification verdict divergence for target %s (critic %s): "
            "model asserted %r, computed %r from %d open finding(s)",
            target_job_id, critic_job_id,
            record["asserted_verdict"], record["verdict"], len(open_after),
        )

    appended = await postgres_db.append_verification_round(target_job_id, record)
    if appended == 0:
        # Duplicate (retried /complete) — return the stored verdict, idempotent.
        stored = await postgres_db.get_job(target_job_id)
        for existing in _verification_rounds(stored):
            if existing.get("critic_job_id") == critic_job_id:
                return {
                    "verdict": existing.get("verdict"),
                    "round": existing.get("round"),
                    "assigned": existing.get("opened", []),
                    "open_findings": fold_open_findings(_verification_rounds(stored)),
                }
        raise HTTPException(status_code=500, detail="Ledger append failed")

    return {
        "verdict": record["verdict"],
        "round": record["round"],
        "assigned": assigned,
        "open_findings": open_after,
    }


@app.post("/api/jobs/{target_job_id}/verification/rounds")
async def record_verification_round(
    request: Request, target_job_id: str
) -> dict[str, Any]:
    """Record one verification round on the TARGET job's durable ledger.

    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips this path.
    Called by the critic's verdict tools BEFORE they return, so the verdict is
    durable before anything observes it (journal-before-observe). The verdict in
    the response is COMPUTED from the open findings, not taken from the caller.
    """
    await require_internal(request)
    body = await request.json()
    return await _record_verification_round_impl(
        postgres_db=postgres_db,
        target_job_id=target_job_id,
        critic_job_id=str(body.get("critic_job_id") or ""),
        asserted_verdict=str(body.get("asserted_verdict") or ""),
        opened=body.get("opened") or [],
        dispositions=body.get("dispositions") or [],
        head_commit=body.get("head_commit"),
    )
```

Also add the shared coercion helper near the other `_parse_*` helpers in `orchestrator/main.py` — **this is the only place the ledger is parsed**; do not add another:

```python
def _verification_rounds(job: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read ``context.verification_rounds`` from a job row.

    asyncpg returns JSONB as a string on the app pool (no codec registered), so
    the context must be coerced at every read. This is the single coercion
    point for the ledger — see
    docs/issues/jsonb_isinstance_guard_without_parse_silent_dead_paths.md.
    """
    if not job:
        return []
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(ctx, dict):
        return []
    rounds = ctx.get("verification_rounds")
    return rounds if isinstance(rounds, list) else []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verification_flow.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_verification_flow.py
git commit -m "feat(verification): durable round-recording endpoint with computed verdict"
```

---

### Task 5: Verdict tools write durably before returning

**Files:**
- Modify: `src/api/orchestrator_client.py` (add near `save_citation_snapshot`)
- Modify: `src/tools/evaluation/evaluation_tools.py:84-251`
- Test: `tests/test_critic_loop.py`

**Interfaces:**
- Consumes: the endpoint from Task 4.
- Produces: `OrchestratorClient.record_verification_round(...) -> dict` (raises `VerdictRecordingError` on failure); both tools now require a durable write before returning.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_critic_loop.py`:

```python
class TestVerdictDurability:
    @pytest.mark.asyncio
    async def test_return_tool_fails_loudly_without_orchestrator_client(self):
        """A verdict that cannot be persisted must NOT report success.

        The house convention for orchestrator_client is best-effort
        (`if client is None: return None`). For a verdict that is exactly
        backwards — a silently-unrecorded rejection becomes an approval.
        """
        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import create_evaluation_tools

        ctx = ToolContext(job_id="c1", config={})
        ctx.orchestrator_client = None
        approve, return_with_feedback = create_evaluation_tools(ctx)

        result = await return_with_feedback.ainvoke({
            "job_id": "t1", "feedback": "bad",
            "findings": [{"claim": "x", "severity": "high"}],
        })

        assert "error" in result.lower()
        from src.tools.evaluation.evaluation_tools import get_verdict_data
        assert get_verdict_data("c1") is None

    @pytest.mark.asyncio
    async def test_tool_stores_server_computed_verdict_not_asserted(self):
        """The server's computed verdict wins over what the model claimed."""
        from unittest.mock import AsyncMock

        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import (
            create_evaluation_tools,
            get_verdict_data,
        )

        ctx = ToolContext(job_id="c2", config={})
        ctx.orchestrator_client = AsyncMock()
        ctx.orchestrator_client.record_verification_round = AsyncMock(
            return_value={"verdict": "returned", "round": 3,
                          "assigned": [], "open_findings": [{"id": "F1"}]}
        )
        approve, _ = create_evaluation_tools(ctx)

        await approve.ainvoke({"job_id": "t1", "report": "looks good",
                               "dispositions": [{"id": "F1", "disposition": "STILL_OPEN"}]})

        assert get_verdict_data("c2")["_verdict"] == "returned"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_critic_loop.py::TestVerdictDurability -v`
Expected: FAIL — the tools accept no `findings`/`dispositions` arguments yet.

- [ ] **Step 3: Implement**

Add to `src/api/orchestrator_client.py`:

```python
class VerdictRecordingError(Exception):
    """The verdict could not be durably recorded.

    Deliberately loud: a verdict that is not persisted must never be reported
    to the model as recorded, because every downstream loss path treats a
    missing verdict as approval.
    """


    async def record_verification_round(
        self,
        target_job_id: str,
        critic_job_id: str,
        asserted_verdict: str,
        opened: list,
        dispositions: list,
        head_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Durably record this round and return the SERVER-COMPUTED verdict.

        Journal-before-observe: the tool must not return to the model until the
        round is committed. Raises VerdictRecordingError on any failure —
        including a 409, whose ``errors`` list is model-facing and must be
        surfaced verbatim so the model can correct itself.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{target_job_id}/verification/rounds"
        payload = {
            "critic_job_id": critic_job_id,
            "asserted_verdict": asserted_verdict,
            "opened": opened,
            "dispositions": dispositions,
            "head_commit": head_commit,
        }
        try:
            response = await self._client.post(url, json=payload)
        except httpx.RequestError as e:
            raise VerdictRecordingError(f"network error: {e}") from e

        if response.status_code == 200:
            return response.json()
        if response.status_code == 409:
            detail = response.json().get("detail", {})
            errors = detail.get("errors") if isinstance(detail, dict) else None
            raise VerdictRecordingError(
                "verdict rejected:\n- " + "\n- ".join(errors or [str(detail)])
            )
        raise VerdictRecordingError(
            f"HTTP {response.status_code}: {response.text[:200]}"
        )
```

Rewrite both tools in `src/tools/evaluation/evaluation_tools.py`. Replace the bodies of `approve_job` and `return_job_with_feedback` (currently :84-251) with a shared helper plus two thin wrappers:

```python
    async def _submit_verdict(
        asserted: str,
        target_job_id: str,
        narrative: str,
        findings: Optional[List[Dict[str, Any]]],
        dispositions: Optional[List[Dict[str, Any]]],
    ) -> str:
        """Durably record a verdict, then mirror it into the local caches.

        Order matters: nothing is written to ``_verdict_data`` /
        ``_final_phase_data`` until the orchestrator has committed the round.
        """
        from ..core.job import _final_phase_data

        client = getattr(context, "orchestrator_client", None)
        if client is None:
            return (
                "Error: cannot record a verdict — no orchestrator client is "
                "available. The verdict was NOT recorded. Do not proceed as if "
                "the review is complete."
            )

        head_commit = None
        try:
            head_commit = context.workspace_manager.get_head_commit()
        except Exception:  # noqa: BLE001 — progress detection is best-effort
            pass

        try:
            result = await client.record_verification_round(
                target_job_id=target_job_id,
                critic_job_id=context.job_id,
                asserted_verdict=asserted,
                opened=findings or [],
                dispositions=dispositions or [],
                head_commit=head_commit,
            )
        except Exception as e:  # VerdictRecordingError and anything unexpected
            logger.error(f"Verdict recording failed for {target_job_id}: {e}")
            return (
                f"Error: the verdict was NOT recorded and must be corrected and "
                f"resubmitted.\n{e}"
            )

        verdict = result["verdict"]
        report_data = {
            "verdict": verdict,
            "asserted_verdict": asserted,
            "target_job_id": target_job_id,
            "narrative": narrative,
            "round": result["round"],
            "open_findings": result["open_findings"],
        }
        if context.has_workspace():
            context.workspace_manager.write_file(
                f"output/verification_report_round_{result['round']}.json",
                json.dumps(report_data, indent=2, ensure_ascii=False),
            )

        _verdict_data[context.job_id] = {
            "_verdict": verdict,
            "_target_job_id": target_job_id,
            "round": result["round"],
            "open_findings": result["open_findings"],
        }
        _final_phase_data[context.job_id] = {
            "summary": f"Verification round {result['round']}: {verdict} job {target_job_id}",
            "deliverables": [f"output/verification_report_round_{result['round']}.json"],
            "confidence": 1.0,
            "job_id": context.job_id,
        }

        open_ids = ", ".join(f["id"] for f in result["open_findings"]) or "none"
        divergence = (
            f"\nNOTE: you asserted {asserted!r} but the recorded verdict is "
            f"{verdict!r}, computed from the open findings."
            if verdict != asserted else ""
        )
        return (
            f"Verdict recorded (round {result['round']}): {verdict.upper()} "
            f"job {target_job_id}.\nOpen findings: {open_ids}.{divergence}\n\n"
            f"Complete your remaining todos to finalize."
        )

    @tool
    async def approve_job(
        job_id: str,
        report: str,
        dispositions: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Approve a target job that is pending review.

        Every open blocking finding from previous rounds must be dispositioned.
        A finding closes ONLY with a `quote` from the CURRENT deliverable — you
        cannot close one by judging it not to be a problem. If blocking findings
        remain open, the recorded verdict will be `returned` regardless of this
        call.

        Args:
            job_id: UUID of the target job to approve
            report: Summary of the review findings (2-5 sentences)
            dispositions: [{"id": "F1", "disposition": "RESOLVED", "quote": "..."}]
        """
        return await _submit_verdict("approved", job_id, report, [], dispositions)

    @tool
    async def return_job_with_feedback(
        job_id: str,
        feedback: str,
        findings: Optional[List[Dict[str, Any]]] = None,
        dispositions: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Return a target job to the original agent with feedback.

        `findings` are NEW problems you found this round; the server assigns
        each a stable id. `dispositions` answer findings opened in previous
        rounds. Returning with an empty `findings` list AND no previously-open
        findings is rejected.

        Args:
            job_id: UUID of the target job to return
            feedback: Detailed narrative feedback
            findings: [{"claim": "...", "severity": "high", "evidence": "..."}]
            dispositions: [{"id": "F1", "disposition": "STILL_OPEN"}]
        """
        return await _submit_verdict("returned", job_id, feedback, findings, dispositions)
```

If `WorkspaceManager` has no `get_head_commit`, add one that shells `git rev-parse HEAD` in the workspace root and returns `None` on any error — progress detection is a heuristic and must never break a verdict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_critic_loop.py -v`
Expected: PASS. Pre-existing tests that assert the old `issues`/`severity` shape will fail — update them to the new shape as part of this task; do not delete them.

- [ ] **Step 5: Commit**

```bash
git add src/api/orchestrator_client.py src/tools/evaluation/evaluation_tools.py tests/test_critic_loop.py
git commit -m "feat(verification): verdict tools persist durably before returning"
```

---

### Task 6: Ledger-driven critic spawn with no-progress and cap escalation

**Files:**
- Modify: `orchestrator/main.py:12302-12360` (`_trigger_verification_on_complete`)
- Test: `tests/test_verification_flow.py`

**Interfaces:**
- Consumes: `_verification_rounds` (Task 4), `fold_open_findings`, `is_blocking` (Task 2), `escalation_status` (Task 3).
- Produces: `_verification_gate_decision(rounds, head_commit, max_rounds) -> tuple[str, str]` returning `(action, reason)` where action ∈ `{"spawn", "escalate"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verification_flow.py`:

```python
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

        rounds = [{"round": 1, "critic_job_id": "c1", "head_commit": "aaa",
                   "verdict": "returned", "asserted_verdict": "returned",
                   "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
                   "dispositions": [], "ts": "t"}]

        action, reason = _verification_gate_decision(rounds, head_commit="aaa", max_rounds=3)
        assert action == "escalate"
        assert "no progress" in reason.lower()

    def test_changed_head_with_open_blocking_spawns(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [{"round": 1, "critic_job_id": "c1", "head_commit": "aaa",
                   "verdict": "returned", "asserted_verdict": "returned",
                   "opened": [{"id": "F1", "severity": "high", "claim": "c"}],
                   "dispositions": [], "ts": "t"}]

        action, _ = _verification_gate_decision(rounds, head_commit="bbb", max_rounds=3)
        assert action == "spawn"

    def test_cap_reached_with_open_blocking_escalates(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {"round": i, "critic_job_id": f"c{i}", "head_commit": f"h{i}",
             "verdict": "returned", "asserted_verdict": "returned",
             "opened": [{"id": f"F{i}", "severity": "high", "claim": "c"}],
             "dispositions": [], "ts": "t"}
            for i in range(1, 4)
        ]

        action, reason = _verification_gate_decision(rounds, head_commit="h9", max_rounds=3)
        assert action == "escalate"
        assert "round limit" in reason.lower()

    def test_unlimited_rounds_never_hits_cap(self):
        from orchestrator.main import _verification_gate_decision

        rounds = [
            {"round": i, "critic_job_id": f"c{i}", "head_commit": f"h{i}",
             "verdict": "returned", "asserted_verdict": "returned",
             "opened": [{"id": f"F{i}", "severity": "high", "claim": "c"}],
             "dispositions": [], "ts": "t"}
            for i in range(1, 9)
        ]

        action, _ = _verification_gate_decision(rounds, head_commit="h9", max_rounds=0)
        assert action == "spawn"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verification_flow.py::TestVerificationGateDecision -v`
Expected: FAIL — `ImportError: cannot import name '_verification_gate_decision'`

- [ ] **Step 3: Implement**

Add to `orchestrator/main.py` above `_trigger_verification_on_complete`:

```python
def _verification_gate_decision(
    rounds: list[dict[str, Any]],
    head_commit: str | None,
    max_rounds: int,
) -> tuple[str, str]:
    """Decide whether to spawn another critic or hand the job to a human.

    Returns ("spawn", "") or ("escalate", reason). Escalation never approves.
    """
    from services.verification_ledger import fold_open_findings, is_blocking

    if not rounds:
        return ("spawn", "")

    blocking = [f for f in fold_open_findings(rounds) if is_blocking(f)]
    if not blocking:
        return ("spawn", "")

    open_ids = ", ".join(f["id"] for f in blocking)

    previous_head = rounds[-1].get("head_commit")
    if head_commit and previous_head and head_commit == previous_head:
        return (
            "escalate",
            f"No progress since round {len(rounds)}: the deliverable is unchanged "
            f"(commit {head_commit[:8]}) while {len(blocking)} blocking finding(s) "
            f"remain open ({open_ids}).",
        )

    if max_rounds > 0 and len(rounds) >= max_rounds:
        return (
            "escalate",
            f"Round limit reached ({max_rounds}) with {len(blocking)} blocking "
            f"finding(s) still open ({open_ids}).",
        )

    return ("spawn", "")
```

Then replace the branch selector in `_trigger_verification_on_complete`. Delete the `SELECT ... status = 'waiting'` query at :12354-12360 and the entire `if critic_row:` resume branch at :12362-12390, replacing them with:

```python
    rounds = _verification_rounds(job)
    verification_config = get_verification_config(job)
    max_rounds = verification_config.get("max_rounds", 3)
    head_commit = (freeze_data or {}).get("head_commit")

    action, reason = _verification_gate_decision(rounds, head_commit, max_rounds)
    if action == "escalate":
        await _escalate_target(job_id, job, reason)
        actions.append(f"target {job_id} escalated: {reason}")
        return

    # Fall through to the (previously `else`) create-a-critic branch, which is
    # now the ONLY path. Round number comes from the ledger, not a counter on
    # the critic, so it cannot reset when a critic dies.
```

In the create branch, change the context stamp from `"verification_round": 0` to `"verification_round": len(rounds)` and delete `max_verification_rounds` (the cap now lives with the target's config, read at decision time).

Add the escalation helper near `_set_target_to_autonomy_status`:

```python
async def _escalate_target(
    job_id: str, job: dict[str, Any], reason: str
) -> str:
    """Hand a target to a human without approving it.

    Loop jobs must NOT park on ``pending_review`` — the loop advance hook fires
    only on terminal statuses, so a parked loop job wedges the whole loop. They
    resolve ``completed`` with the reason in ``error_message`` for the retro.
    """
    from services.project_loops import job_loop_id
    from services.verification_ledger import escalation_status

    status = escalation_status(is_loop_job=bool(job_loop_id(job)))
    await postgres_db.update_job_status(job_id, status=status, error_message=reason)
    logger.warning("Verification escalated target %s to %s: %s", job_id, status, reason)
    return status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verification_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_verification_flow.py
git commit -m "feat(verification): ledger-driven spawn, no-progress and cap escalation"
```

---

### Task 7: Deliver the critic brief with prior findings

**Files:**
- Modify: `orchestrator/services/completion.py:1125-1184`
- Modify: `config/experts/critic/verification_instructions.md`
- Modify: `orchestrator/main.py` (critic creation, ~:12399-12420)
- Test: `tests/test_completion_endpoint.py`

**Interfaces:**
- Consumes: `render_prior_findings` (Task 3).
- Produces: `format_verification_instructions(..., prior_findings: str = "")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_completion_endpoint.py`:

```python
class TestVerificationInstructionsDelivery:
    def test_prior_findings_rendered_into_template(self):
        from orchestrator.services.completion import format_verification_instructions

        text = format_verification_instructions(
            job_id="t1", description="d", freeze_data={}, config_name="worker_base",
            prior_findings="- **F1** [high, opened round 1]: missing source",
        )
        assert text is not None
        assert "F1" in text
        assert "missing source" in text

    def test_missing_prior_findings_does_not_abort(self):
        """A KeyError here returns None, which aborts critic spawn entirely."""
        from orchestrator.services.completion import format_verification_instructions

        assert format_verification_instructions(
            job_id="t1", description="d", freeze_data={}, config_name="worker_base"
        ) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_completion_endpoint.py::TestVerificationInstructionsDelivery -v`
Expected: FAIL — unexpected keyword argument `prior_findings`.

- [ ] **Step 3: Implement**

In `completion.py`, add the parameter with a default and pass it through:

```python
def format_verification_instructions(
    job_id: str,
    description: str,
    freeze_data: dict[str, Any],
    config_name: str,
    prior_findings: str = "",
) -> str | None:
```

and in the `template.format(...)` call add:

```python
            prior_findings=prior_findings
            or "No open findings from previous rounds. This is a first review.",
```

The default matters: `str.format` raises `KeyError` for a missing key, and the `except KeyError` branch returns `None`, which makes `main.py:12406` abort critic creation entirely.

Append to `config/experts/critic/verification_instructions.md`, immediately after the `### Agent's confidence` block:

```markdown
## Open Findings From Previous Rounds

{prior_findings}
```

Then in `main.py`, wire the rendered text into the critic's context — the fix for the template being rendered and discarded:

```python
        from services.verification_ledger import fold_open_findings, render_prior_findings

        instructions = format_verification_instructions(
            job_id=job_id,
            description=job.get("description", ""),
            freeze_data=freeze_data,
            config_name=config_name,
            prior_findings=render_prior_findings(fold_open_findings(rounds)),
        )
        if not instructions:
            logger.error(f"Failed to format verification instructions for job {job_id}")
            return
```

and add to the `context` dict built at :12413:

```python
            "instructions": instructions,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_completion_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/completion.py config/experts/critic/verification_instructions.md orchestrator/main.py tests/test_completion_endpoint.py
git commit -m "fix(verification): actually deliver the critic brief, with prior findings"
```

---

### Task 8: Fail-closed verdict handling

**Files:**
- Modify: `orchestrator/main.py:12186-12299` (`_handle_critic_verdict_on_complete`)
- Test: `tests/test_verification_flow.py`

**Interfaces:**
- Consumes: `_verification_rounds`, `_escalate_target` (Tasks 4, 6).
- Produces: no new public names; the handler now reads the ledger.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verification_flow.py`:

```python
class TestFailClosedVerdictHandling:
    def test_non_critic_subjob_is_ignored(self):
        """A delegation child has parent_job_id and freeze_data but no verdict.

        Without the verification_target discriminator it hits the implicit
        approval branch and advances its parent before siblings finish.
        """
        from orchestrator.main import _is_verification_critic

        assert _is_verification_critic({"context": {"verification_target": "t1"}}) is True
        assert _is_verification_critic({"context": {"scholar_target": "t1"}}) is False
        assert _is_verification_critic({"context": {}}) is False
        assert _is_verification_critic({"context": '{"verification_target": "t1"}'}) is True

    def test_completed_critic_without_ledger_record_escalates(self, ledger_state):
        """No verdict must never mean approval."""
        from orchestrator.main import _resolve_critic_outcome

        outcome, reason = _resolve_critic_outcome(
            critic_job_id="c1", critic_status="completed", rounds=[]
        )
        assert outcome == "escalate"
        assert "no verdict" in reason.lower()

    def test_failed_critic_with_verdict_still_escalates(self):
        """A critic that failed must not approve its target."""
        from orchestrator.main import _resolve_critic_outcome

        outcome, _ = _resolve_critic_outcome(
            critic_job_id="c1", critic_status="failed",
            rounds=[{"critic_job_id": "c1", "verdict": "approved"}],
        )
        assert outcome == "escalate"

    def test_completed_critic_with_record_uses_computed_verdict(self):
        from orchestrator.main import _resolve_critic_outcome

        outcome, _ = _resolve_critic_outcome(
            critic_job_id="c1", critic_status="completed",
            rounds=[{"critic_job_id": "c1", "verdict": "returned"}],
        )
        assert outcome == "returned"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verification_flow.py::TestFailClosedVerdictHandling -v`
Expected: FAIL — `ImportError: cannot import name '_is_verification_critic'`

- [ ] **Step 3: Implement**

Add to `orchestrator/main.py`:

```python
_CRITIC_TERMINAL_OK = {"completed"}


def _is_verification_critic(job: dict[str, Any]) -> bool:
    """True only for verification critics.

    ``parent_job_id`` alone is not enough: scholars and delegation children
    share it, and a delegation child completing normally would otherwise be
    read as a verdict-less critic and advance its parent.
    """
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            return False
    return bool(isinstance(ctx, dict) and ctx.get("verification_target"))


def _resolve_critic_outcome(
    critic_job_id: str, critic_status: str, rounds: list[dict[str, Any]]
) -> tuple[str, str]:
    """Resolve what a finished critic means for its target.

    Returns ("approved"|"returned"|"escalate", reason). Absence of a verdict is
    NOT approval — that conflation is the defect this design removes (CWE-636).
    """
    if critic_status not in _CRITIC_TERMINAL_OK:
        return (
            "escalate",
            f"Critic {critic_job_id} ended in status {critic_status!r}; "
            f"no trustworthy verdict.",
        )

    for rnd in rounds:
        if rnd.get("critic_job_id") == critic_job_id:
            return (rnd.get("verdict", "returned"), "")

    return (
        "escalate",
        f"Critic {critic_job_id} finished with no verdict recorded on the "
        f"verification ledger.",
    )
```

Rewrite `_handle_critic_verdict_on_complete` to: return early unless `_is_verification_critic(job)`; read `rounds = _verification_rounds(target_job)`; call `_resolve_critic_outcome`; then dispatch — `approved` → `_set_target_to_autonomy_status` + curation; `returned` → `_internal_resume_job` with the findings rendered into the feedback text; `escalate` → `_escalate_target`.

**Delete** the implicit-approval block at :12228-12236 and the round-limit auto-accept at :12266-12281 entirely.

For the `returned` feedback text, render the open findings with their IDs so the target sees what to fix and the next round can match them:

```python
        from services.verification_ledger import fold_open_findings

        open_findings = fold_open_findings(rounds)
        feedback_lines = [rnd_record.get("narrative", ""), "", "## Open findings", ""]
        for f in sorted(open_findings, key=lambda x: x.get("id", "")):
            feedback_lines.append(
                f"- **{f['id']}** [{f['severity']}]: {f.get('claim', '')}"
            )
        await _internal_resume_job(target_job_id, feedback="\n".join(feedback_lines))
```

Also delete the implicit-approval synthesis in `src/core/phase.py:803-830`; replace it with a logged refusal that leaves no verdict, so the orchestrator's `_resolve_critic_outcome` escalates:

```python
    metadata = state.get("metadata") or {}
    if metadata.get("verification_target"):
        logger.error(
            f"[{job_id}] Critic finalizing WITHOUT a recorded verdict. "
            f"NOT synthesizing an approval — the orchestrator will escalate "
            f"target {metadata['verification_target']} to a human."
        )
```

And in `_finalize_with_verdict` (`src/core/phase.py:684-697`), change the returned-verdict freeze from `"status": "waiting"` to `"status": "completed"` — critics no longer park between rounds.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verification_flow.py tests/test_critic_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py src/core/phase.py tests/test_verification_flow.py
git commit -m "feat(verification): fail-closed verdict handling, no implicit approvals"
```

---

### Task 9: Remove self-closing tools from the critic

**Files:**
- Modify: `orchestrator/main.py:12443-12448` (critic `config_override`)
- Test: `tests/test_worktree_sharing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worktree_sharing.py`:

```python
def test_critic_config_override_removes_self_closing_tools():
    """`deep_merge` merges dicts by key, so the override ADDS `evaluation` and
    narrows nothing — `core` still carried job_complete/mark_complete, the most
    likely LLM mistake and a direct path to a verdict-less completion.
    """
    from orchestrator.main import _critic_config_override

    override = _critic_config_override(parent_llm=None)
    core = override["tools"]["core"]
    assert "job_complete" not in core
    assert "mark_complete" not in core
    assert set(override["tools"]["evaluation"]) == {"approve_job", "return_job_with_feedback"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worktree_sharing.py::test_critic_config_override_removes_self_closing_tools -v`
Expected: FAIL — `ImportError: cannot import name '_critic_config_override'`

- [ ] **Step 3: Implement**

Extract the inline dict at `main.py:12443` into a named function and narrow `core`:

```python
def _critic_config_override(parent_llm: dict[str, Any] | None) -> dict[str, Any]:
    """Config override stamped onto every verification critic.

    ``core`` is spelled out explicitly because ``deep_merge`` replaces lists but
    merges dicts by key: without this, the critic inherits ``job_complete`` and
    ``mark_complete`` from worker_base and can close itself without a verdict.
    """
    override: dict[str, Any] = {
        "autonomy": "full",
        "tools": {
            "evaluation": ["approve_job", "return_job_with_feedback"],
            "core": ["next_phase_todos", "todo_complete", "todo_list", "todo_rewind"],
        },
    }
    if parent_llm:
        override["llm"] = parent_llm
    return override
```

Replace the inline construction at the call site with `config_override = _critic_config_override(parent_llm)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_worktree_sharing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_worktree_sharing.py
git commit -m "fix(verification): critic cannot close itself without a verdict"
```

---

### Task 10: Ledger-aware unstick watchdog and sweeper extension

**Files:**
- Modify: `orchestrator/database/postgres.py:4462-4517` (`unstick_reviewing_parents`), `:4427-4440` (`cancel_stale_verification_subjobs`)
- Test: `tests/test_stale_verification_sweeper.py`

**Interfaces:**
- Consumes: nothing.
- Produces: unchanged signatures; changed SQL.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stale_verification_sweeper.py`:

```python
def test_unstick_no_longer_requires_all_children_failed():
    """Every round now leaves a `completed` critic behind, so the old
    "all children failed/cancelled" condition could never be met again after
    round 1 — a target whose round-2 critic died would sit in `reviewing`
    forever.
    """
    from orchestrator.database.postgres import PostgresDB

    sql = PostgresDB._UNSTICK_REVIEWING_SQL
    assert "status NOT IN ('failed', 'cancelled')" not in sql
    assert "verification_rounds" in sql


def test_sweeper_reaps_waiting_critics_but_not_paused():
    """`waiting` critics are orphans of the retired parking mechanism.
    `paused` critics are legitimately re-dispatched by orphan recovery and must
    stay excluded — "paused too long ⇒ dead" was evaluated and rejected.
    """
    from orchestrator.database.postgres import PostgresDB

    sql = PostgresDB._CANCEL_STALE_VERIFICATION_SQL
    assert "'waiting'" in sql
    assert "'paused'" not in sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stale_verification_sweeper.py -v`
Expected: FAIL — `AttributeError: type object 'PostgresDB' has no attribute '_UNSTICK_REVIEWING_SQL'`

- [ ] **Step 3: Implement**

Hoist both queries to class-level constants so they are assertable, then change them.

`_CANCEL_STALE_VERIFICATION_SQL`: change `status IN ('created', 'paused')` to `status IN ('created', 'waiting')`. Removing `'paused'` is required — orphan recovery legitimately parks critics there.

`_UNSTICK_REVIEWING_SQL`: replace the `NOT EXISTS (... status NOT IN ('failed','cancelled'))` subquery with one that fires when **no non-terminal critic child exists** and the ledger holds no record for the current round:

```sql
UPDATE jobs p
SET status = 'pending_review',
    error_message = 'Verification did not complete; escalated for human review',
    updated_at = CURRENT_TIMESTAMP
WHERE p.status = 'reviewing'
  AND p.updated_at < NOW() - ($1 || ' minutes')::interval
  AND NOT EXISTS (
      SELECT 1 FROM jobs c
      WHERE c.parent_job_id = p.id
        AND c.context->>'verification_target' IS NOT NULL
        AND c.status IN ('created', 'processing', 'paused', 'waiting')
  )
  AND NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(
          COALESCE(p.context->'verification_rounds', '[]'::jsonb)) r
      WHERE (r->>'round')::int
            = jsonb_array_length(COALESCE(p.context->'verification_rounds', '[]'::jsonb))
        AND r->>'verdict' IS NOT NULL
  )
RETURNING p.id, p.user_id
```

The second `NOT EXISTS` is what replaces the `completed`-critic exclusion: if the newest round has a recorded verdict, the verdict handler owns the transition and the watchdog stays out of its way. If there is no record, no critic is alive, and the grace period has passed, the target is genuinely stranded and escalates.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stale_verification_sweeper.py tests/test_stale_verification_outage_exemption.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/postgres.py tests/test_stale_verification_sweeper.py
git commit -m "fix(verification): ledger-aware unstick watchdog, reap waiting critics"
```

---

### Task 11: Repair the two broken tests and add end-to-end continuity

**Files:**
- Modify: `tests/test_complete_job_endpoint.py:296-354`
- Modify: `tests/test_critic_loop.py:363-538`
- Modify: `tests/test_verification_flow.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Replace the tautological guard test**

`TestVerificationTriggerGuards` currently asserts the predicates directly (`assert result.get("error") is not None`) and would pass if the guards were deleted from `main.py`. Replace each case with one that **invokes** `_trigger_verification_on_complete` with a mocked `postgres_db` and asserts no critic was created:

```python
class TestVerificationTriggerGuards:
    @pytest.mark.asyncio
    async def test_no_critic_created_when_result_has_error(self, monkeypatch):
        from orchestrator import main

        created = []
        monkeypatch.setattr(main.postgres_db, "create_job",
                            AsyncMock(side_effect=lambda **kw: created.append(kw)))

        await main._trigger_verification_on_complete(
            job={"id": "t1", "status": "reviewing", "context": {}},
            result={"error": "boom"},
            actions=[],
        )

        assert created == []
```

Repeat for: `should_stop` false, `parent_job_id` set, verification disabled, lite backend. Each must call the real function.

- [ ] **Step 2: Un-skip and rewrite the round-limit tests**

`TestRoundLimitEnforcement` is `@pytest.mark.skip`ped with the note *"Agent-side `_handle_critic_verdict` removed; logic moved to orchestrator"* — and was never re-tested there. Remove the skip marker and rewrite against `_verification_gate_decision`, asserting the **escalate** outcome (never auto-accept), including the loop-job variant that must resolve `completed` rather than `pending_review`.

- [ ] **Step 3: Add the end-to-end continuity test**

Append to `tests/test_verification_flow.py`:

```python
class TestMultiRoundContinuity:
    @pytest.mark.asyncio
    async def test_round_two_critic_is_told_about_round_one_findings(self, fake_db, ledger_state):
        """The gap that made the incident possible: nothing carried findings
        from one round to the next."""
        from orchestrator.main import _record_verification_round_impl
        from orchestrator.services.verification_ledger import (
            fold_open_findings, render_prior_findings,
        )

        await _record_verification_round_impl(
            postgres_db=fake_db, target_job_id="t1", critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "missing walnut-shell source"}],
            dispositions=[], head_commit="aaa",
        )

        brief = render_prior_findings(fold_open_findings(ledger_state["rounds"]))
        assert "F1" in brief
        assert "missing walnut-shell source" in brief
        assert "may not close a finding by re-judging" in brief

    @pytest.mark.asyncio
    async def test_round_three_cannot_approve_over_open_finding(self, fake_db, ledger_state):
        """The incident itself, as an assertion."""
        from orchestrator.main import _record_verification_round_impl

        await _record_verification_round_impl(
            postgres_db=fake_db, target_job_id="t1", critic_job_id="c1",
            asserted_verdict="returned",
            opened=[{"severity": "high", "claim": "missing walnut-shell source"}],
            dispositions=[], head_commit="aaa",
        )
        result = await _record_verification_round_impl(
            postgres_db=fake_db, target_job_id="t1", critic_job_id="c3",
            asserted_verdict="approved",
            opened=[],
            dispositions=[{"id": "F1", "disposition": "DISPUTED",
                           "reason": "covered in the safety section"}],
            head_commit="bbb",
        )

        assert result["verdict"] == "returned"
```

- [ ] **Step 4: Run the full affected suite**

Run:
```bash
python -m pytest tests/test_verification_flow.py tests/test_verification_ledger.py \
  tests/test_critic_loop.py tests/test_complete_job_endpoint.py \
  tests/test_stale_verification_sweeper.py tests/test_atomic_job_context.py -v
```
Expected: PASS. Local Python 3.14 is noisy — CI (3.12) is the gate.

- [ ] **Step 5: Run ruff and commit**

```bash
ruff check orchestrator/ src/ tests/ --fix
ruff format orchestrator/services/verification_ledger.py tests/test_verification_ledger.py tests/test_verification_flow.py
git add tests/
git commit -m "test(verification): repair tautological + skipped tests, add continuity coverage"
```

---

## Self-Review

**Spec coverage.** Ledger data model → Task 1 (storage) + Task 2 (shapes). Fresh critic per round → Task 6. Evidence-only closure → Task 3 (`validate_dispositions`) + Task 2 (`fold_open_findings` DISPUTED). Computed verdict → Task 2 + Task 4. Divergence logging → Task 4. Progress via HEAD → Task 5 (capture) + Task 6 (decision). Escalate at cap → Task 6. Loop carve-out → Task 3 (`escalation_status`) + Task 6 (`_escalate_target`). Dead template → Task 7. Fail-closed handling + deleting both implicit approvals → Task 8. `job_complete` removal → Task 9. Ledger-aware watchdog + `waiting` sweep → Task 10. Test repairs → Task 11. Single JSONB coercion point → Task 4 (`_verification_rounds`).

**Rollout items from the spec not needing their own task:** mirroring the computed verdict into `freeze_data` happens naturally in Task 5 (`_finalize_with_verdict` already writes the freeze from `_verdict_data`, which now holds the server-computed verdict). In-flight jobs need no migration — an absent `verification_rounds` key folds to an empty list everywhere.

**Not covered by design, deliberately:** frozen rubrics; the drain-overwrite fix; `_final_phase_data` durability. All three are separate tracked issues.

**Type consistency.** `fold_open_findings` returns findings carrying `id`/`severity`/`claim`/`opened_round`/`disputed` and is consumed with exactly those keys in Tasks 3, 6, 7, 8. `_verification_gate_decision` returns `(action, reason)` in Tasks 6 and 11. `_resolve_critic_outcome` returns `(outcome, reason)` in Task 8. `record_verification_round` returns `{verdict, round, assigned, open_findings}` in Tasks 4, 5, 11.

**Known follow-up not in this plan:** `WorkspaceManager.get_head_commit()` is introduced in Task 5 with a "add one if absent" instruction. If it is absent, that is a ~10-line addition inside Task 5, not a separate task — progress detection degrades to `None` (always spawn) if it fails, which is the safe direction.
