# Loop Unified Engine — Phase 1 Implementation Plan

> **EXECUTED AND CLOSED (2026-07-25).** All 8 tasks implemented, reviewed, and merged to `develop` (`737d2888..65fea459` after the push rewrite), deployed to dev and live-validated. The Deploy Notes below were followed with one exception: running loops were NOT paused for the rolling window (no harm observed; the guarded CAS writes held). Task 8's k3d smoke was never run — live dev-loop validation replaced it. Current state lives in `docs/features/loop_unified_engine.md`; this file is kept as the execution record, not as instructions.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the parallel-stage barrier the ONLY advance path for project loops (width-1 turns included), thread campaign advance through it, delete the legacy single-job rotate path, and rename the scheduling modes (`rotation → standard`, `planner → campaign`) — with byte-identical observable loop behavior.

**Architecture:** Every spawned turn is written into `current_stage_jobs` (width 1 included); `current_job_id` becomes a display-only mirror for width-1 turns. Completion hooks route every loop job through the atomic `claim_project_loop_stage_barrier`; the barrier winner aggregates the turn outcome, threads its own job + context into `_rotate_loop_to_next_stage` (so the campaign step keeps firing), checks stops, and rotates. The sweeper heals exactly one wedge signature (`current_job_id IS NULL AND current_stage_jobs='[]'`) by restoring membership. Spec: `docs/features/loop_unified_engine.md` (Phase 1 + the [A1] work-item list).

**Tech Stack:** Python 3.12 (FastAPI orchestrator), asyncpg/PostgreSQL, pytest + AsyncMock, Angular/vitest (cockpit), SQL migrations under `orchestrator/database/migrations/app/`.

## Global Constraints

- Work directly on branch `develop`. No feature branches. NEVER `git push` — the user pushes explicitly.
- Every commit message ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Scope is spec **Phase 1 only** — behavior-preserving. Do NOT rename `max_iterations`/`remaining_iterations`, do NOT create a `loop_handovers` table, do NOT add an `overlap` column. Those are Phases 2–4 with their own migrations (0064+). The spec's single-migration "0063" line is therefore delivered phase-sliced; 0063 carries only the Phase-1 schema changes.
- Do NOT touch `knowledge_index.remaining_cycles` or the KB TTL SQL block inside `_rotate_loop_to_next_stage` (orchestrator/main.py, the `UPDATE knowledge_index SET remaining_cycles …` statement). Same word, different subsystem.
- Rename only mode **values** and user-facing strings (`'rotation'→'standard'`, `'planner'→'campaign'`). Internal identifiers keep their names (`planner_slots`, `_advance_planner_campaign`, `plannerIneligibility`, `_planner_critic_block`) — renaming them is churn with no behavior value.
- Line numbers below are anchors as of commit `24420fdf`; they shift as tasks land. Locate by symbol name, not line, when editing.
- Test gate: run the named pytest files with the system interpreter. Local Python is 3.14 and prints deprecation noise — that noise is expected; CI (Python 3.12) is the authoritative gate. A test *failure* is real and blocks the task.
- Run `ruff check orchestrator/ src/ tests/` before each commit (CI enforces it; the pre-push hook also rewrites via ruff).

## Deploy Notes (read before the k3d/live rollout, not needed for coding tasks)

- Migration 0063 must run before the new orchestrator serves traffic (standard boot ordering already guarantees this).
- During a rolling deploy an old replica can still advance a **campaign** loop through the old path; a backfilled width-1 row hitting the OLD parallel path skips the campaign step for that one advance. Pausing running loops for the deploy window is the RECOMMENDED procedure (accept the one-advance degradation only if you can't pause; dev currently runs at most one loop) — the guarded adopt below prevents the separate duplicate-turn case.
- A width-1 turn written by an old replica *after* the migration ran leaves a legacy-shaped row (pointer set, stage set empty). The sweeper's guarded `adopt_project_loop_pointer_turn` (Task 6) self-heals it within one tick; the guard no-ops if a concurrent old-replica advance already re-pointed the loop, so a stale read can never graft a finished job into a newer turn's membership.

---

### Task 1: Migration 0063 — mode rename + width-1 membership backfill

**Files:**
- Create: `orchestrator/database/migrations/app/0063_loop_unified_engine_phase1.sql`
- Regenerate: `orchestrator/database/schema_current.sql` (via `scripts/schema-snapshot.sh`)

**Interfaces:**
- Consumes: current schema (0050 added `scheduling` with CHECK `('rotation','planner')`; `current_stage_jobs` exists since 0048).
- Produces: `project_loops.scheduling ∈ ('standard','campaign')` with DEFAULT `'standard'`; every active loop's in-flight width-1 turn mirrored into `current_stage_jobs`. Tasks 2–6 assume both.

- [ ] **Step 1: Write the migration**

```sql
-- migration:     0063_loop_unified_engine_phase1.sql
-- description:   Loop unified engine, Phase 1 (docs/features/loop_unified_engine.md).
--                Renames the scheduling modes (rotation → standard,
--                planner → campaign) and backfills current_stage_jobs for
--                active loops' in-flight width-1 turns, so the generalized
--                stage barrier can become the ONLY advance path (a sequential
--                step is a width-1 stage; current_job_id becomes a
--                display-only mirror for width-1 turns).
-- depends-on:    0062_canvas_bootstrap_exchange.sql
-- expected:      < 1s (control-table UPDATEs on a handful of rows).
-- locks:         Brief ACCESS EXCLUSIVE on project_loops (tiny control table).
-- transactional: yes
-- ============================================================================

-- Mode value rename. Constraint swapped in the same transaction so no row can
-- hold an old name after commit.
ALTER TABLE project_loops
    DROP CONSTRAINT IF EXISTS project_loop_scheduling_known;

UPDATE project_loops
SET scheduling = CASE scheduling
    WHEN 'rotation' THEN 'standard'
    WHEN 'planner' THEN 'campaign'
    ELSE scheduling
END;

ALTER TABLE project_loops
    ALTER COLUMN scheduling SET DEFAULT 'standard';

ALTER TABLE project_loops
    ADD CONSTRAINT project_loop_scheduling_known
        CHECK (scheduling IN ('standard', 'campaign'));

-- Unified engine: every in-flight turn is barrier-tracked in
-- current_stage_jobs (width 1 included). Backfill active loops' width-1 turns
-- so the completion hook's membership check finds them after the deploy — a
-- running loop whose pointer-only turn the new code can't see would wedge.
-- Terminal loops are inert (never swept, never advanced) and stay untouched.
UPDATE project_loops
SET current_stage_jobs = jsonb_build_array(current_job_id::text)
WHERE current_job_id IS NOT NULL
  AND current_stage_jobs = '[]'::jsonb
  AND status IN ('running', 'paused');

COMMENT ON COLUMN project_loops.scheduling IS
    'Scheduling mode: standard (the role_sequence stage list, one stage per '
    'turn — subsumes the old rotation mode and its fan-out stages) or '
    'campaign (a checkpoint Critic may expand the execution slot into a '
    'multi-stage campaign via a filed plan; formerly planner). '
    'Start-time-only. docs/features/loop_unified_engine.md.';

COMMENT ON COLUMN project_loops.current_stage_jobs IS
    'In-flight members of the loop''s current turn — the jobs the loop '
    'barriers on before rotating, width 1 included (the unified engine''s '
    'only advance path). Populated by the advance/start spawn; drained to [] '
    'by the atomic last-member barrier, which also nulls current_job_id so '
    'the torn-advance signature stays current_job_id IS NULL AND '
    'current_stage_jobs = ''[]''. docs/features/loop_unified_engine.md.';

COMMENT ON COLUMN project_loops.current_job_id IS
    'Display-only mirror of the in-flight turn when its width is 1 (cockpit '
    'links, MCP formatters). NULL for fan-out turns and between turns. The '
    'engine''s advance/heal correctness keys on current_stage_jobs, never '
    'on this column. docs/features/loop_unified_engine.md.';
```

- [ ] **Step 2: Verify the migration applies cleanly and regenerate the schema snapshot**

Run: `bash scripts/schema-snapshot.sh`
Expected: exits 0 (it boots throwaway podman Postgres containers, applies every app/vector/audit migration in order — 0063 included — and rewrites `orchestrator/database/schema_current.sql`). Then `git diff orchestrator/database/schema_current.sql` must show: the new CHECK values, `DEFAULT 'standard'`, and the three column comments. No other hunks.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/database/migrations/app/0063_loop_unified_engine_phase1.sql orchestrator/database/schema_current.sql
git commit -m "feat(loop): migration 0063 — scheduling mode rename + width-1 barrier backfill

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Backend mode-value rename (standard/campaign)

**Files:**
- Modify: `orchestrator/database/postgres.py` (`create_project_loop`, ~11302)
- Modify: `orchestrator/main.py` (`_rotate_loop_to_next_stage` ~12580, `file_loop_plan` ~13023)
- Modify: `orchestrator/services/project_loops.py` (`build_loop_kickoff` ~657, `create_loop_job` ~771)
- Modify: `orchestrator/routers/project_loops.py` (`ProjectLoopStart` ~62–70, start handler ~123–139)
- Modify: `src/tools/loop/plan.py` (prose only, if it says "planner-scheduled"/"rotation")
- Test: `tests/test_loop_campaign_scheduling.py`, `tests/test_project_loops.py`

**Interfaces:**
- Consumes: migration 0063 (DB rows now hold `standard`/`campaign`).
- Produces: every read of `loop["scheduling"]` compares against `"campaign"` with fallback `"standard"`; the start API accepts `^(standard|campaign)$` only (breaking for old clients — cockpit updates in Task 7, same branch). Tasks 3–6 fixtures use the new values.

- [ ] **Step 1: Update the tests to the new mode values (failing first)**

In `tests/test_loop_campaign_scheduling.py`:
run `grep -n "rotation\|planner" tests/test_loop_campaign_scheduling.py` and update **value literals and message assertions only** (identifiers like `planner_slots`, `PLANNER_ROLES`, `_advance_planner_campaign` keep their names):
- the `_loop()` fixture: `"scheduling": "planner"` → `"scheduling": "campaign"`
- any test that sets `scheduling="rotation"` → `scheduling="standard"`, `scheduling="planner"` → `scheduling="campaign"`
- start-endpoint tests posting `scheduling='planner'` → `'campaign'`; tests posting `'rotation'` → `'standard'`
- assertions matching endpoint detail text (e.g. `match="rotation scheduling"`) → `match="standard scheduling"`

In `tests/test_project_loops.py`: run `grep -n "rotation\|planner" tests/test_project_loops.py` and update the same way (kickoff/`create_loop_job` tests that set `"scheduling": "planner"` on the loop dict → `"campaign"`).

- [ ] **Step 2: Run the suites to verify they fail**

Run: `python -m pytest tests/test_loop_campaign_scheduling.py tests/test_project_loops.py -q`
Expected: FAIL — the planner-duty/tool-injection/campaign tests break because the code still compares against `"planner"`.

- [ ] **Step 3: Rename mode values in the backend**

`orchestrator/database/postgres.py`, `create_project_loop` signature:

```python
        scheduling: str = "standard",
```

(and in its docstring, reword the 0050 note to "opt the loop into campaign-mode scheduling").

`orchestrator/main.py`, in `_rotate_loop_to_next_stage`:

```python
    if (loop.get("scheduling") or "standard") == "campaign" and completed_job:
```

`orchestrator/main.py`, in `file_loop_plan`:

```python
    if (loop.get("scheduling") or "standard") != "campaign":
        raise HTTPException(
            status_code=409,
            detail="This loop uses standard scheduling — plans are only "
            "accepted on campaign-scheduled loops",
        )
```

`orchestrator/services/project_loops.py`, in `build_loop_kickoff` (the campaign-context block) and in `create_loop_job` (the `tools.loop` injection):

```python
    if (loop.get("scheduling") or "standard") == "campaign":
```

```python
    if (
        (loop.get("scheduling") or "standard") == "campaign"
        and role == "critic"
        and not is_campaign_member
    ):
```

(update the neighbouring comments: "Planner-scheduled loops" → "Campaign-scheduled loops", "rotation loops never get it" → "standard loops never get it").

`orchestrator/routers/project_loops.py`, `ProjectLoopStart`:

```python
    # Scheduling mode: 'standard' (default — the role_sequence stage list,
    # one stage per turn) or 'campaign' (the checkpoint critic may expand the
    # execution slot into a multi-stage campaign via a filed plan). Start-time
    # only. docs/features/loop_unified_engine.md.
    scheduling: str = Field("standard", pattern="^(standard|campaign)$")
```

and in the start handler:

```python
    if body.scheduling == "campaign":
```

```python
    elif body.campaign_caps is not None:
        raise HTTPException(
            status_code=400,
            detail="campaign_caps only applies to scheduling='campaign'",
        )
```

`src/tools/loop/plan.py`: `grep -n "planner\|rotation" src/tools/loop/plan.py` — update prose in the tool description/docstrings the agent sees ("planner-scheduled loop" → "campaign-scheduled loop"); no code changes.

- [ ] **Step 4: Run the suites to verify they pass**

Run: `python -m pytest tests/test_loop_campaign_scheduling.py tests/test_project_loops.py -q`
Expected: PASS. Also run `grep -rn "\"planner\"\|'planner'\|\"rotation\"\|'rotation'" orchestrator/ src/ --include="*.py"` — remaining hits must be identifier names or historical comments only, no live mode-value comparisons.

- [ ] **Step 5: Commit**

```bash
git add -A orchestrator src/tools tests
git commit -m "refactor(loop): scheduling mode values rotation→standard, planner→campaign

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Width-1 turns become barrier-tracked (writeback + barrier claim)

**Files:**
- Modify: `orchestrator/main.py` (`_writeback_loop_stage`)
- Modify: `orchestrator/database/postgres.py` (`claim_project_loop_stage_barrier`)
- Create: `tests/test_loop_unified_advance.py`

**Interfaces:**
- Consumes: `postgres_db.update_project_loop` (unchanged).
- Produces: `_writeback_loop_stage` ALWAYS writes `current_stage_jobs=ids` and mirrors `current_job_id` (= `ids[0]` iff width 1, else `None`); the barrier claim drains the set AND nulls `current_job_id` in one UPDATE, so the post-drain wedge signature is always `current_job_id IS NULL AND current_stage_jobs='[]'`. Tasks 4–6 depend on both invariants.

- [ ] **Step 1: Write the failing tests (new file)**

Create `tests/test_loop_unified_advance.py`:

```python
"""Unified loop engine, Phase 1 (docs/features/loop_unified_engine.md).

Every turn — width 1 included — is barrier-tracked in ``current_stage_jobs``:
``_writeback_loop_stage`` writes the membership plus the width-1 display
mirror, ``_advance_project_loop`` routes every member through the atomic
barrier, the winner threads its own job + context into the rotate (so the
campaign step fires from the barrier path), and stop-writes clear BOTH
pointer columns. The legacy single-job rotate path is gone.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

LOOP_ID = "105a6f98-134c-4077-b7e1-6d08916650d7"


def _job(*, role: str = "scholar", status: str = "completed", **ctx_over) -> dict:
    ctx = {
        "loop_id": LOOP_ID,
        "loop_role": role,
        "loop_iteration": 1,
        "loop_seq_index": 0,
        "loop_remaining": 5,
    }
    ctx.update(ctx_over)
    return {"id": str(uuid.uuid4()), "status": status, "context": ctx}


def _loop(**over) -> dict:
    base = {
        "id": LOOP_ID,
        "status": "running",
        "scheduling": "standard",
        "role_sequence": ["scholar", "critic", "developer"],
        "seq_index": 0,
        "total_jobs_run": 1,
        "max_iterations": 6,
        "remaining_iterations": 6,
        "max_consecutive_failures": 3,
        "consecutive_failures": 0,
        "current_job_id": None,
        "current_stage_jobs": [],
        "campaign": None,
        "campaign_history": [],
        "run_until": None,
        "project_id": None,
    }
    base.update(over)
    return base


class TestWritebackLoopStage:
    @pytest.mark.asyncio
    async def test_width1_writes_membership_and_display_mirror(self):
        db = AsyncMock()
        with patch("main.postgres_db", db):
            from main import _writeback_loop_stage

            await _writeback_loop_stage(
                LOOP_ID,
                jobs=[{"id": "job-1"}],
                seq_index=1,
                remaining=4,
                total=2,
                consecutive=0,
                last_error=None,
            )
        kw = db.update_project_loop.call_args.kwargs
        assert kw["current_stage_jobs"] == ["job-1"]
        assert kw["current_job_id"] == "job-1"
        assert kw["seq_index"] == 1 and kw["total_jobs_run"] == 2

    @pytest.mark.asyncio
    async def test_fanout_writes_membership_with_null_mirror(self):
        db = AsyncMock()
        with patch("main.postgres_db", db):
            from main import _writeback_loop_stage

            await _writeback_loop_stage(
                LOOP_ID,
                jobs=[{"id": "a"}, {"id": "b"}],
                seq_index=0,
                remaining=4,
                total=3,
                consecutive=0,
                last_error=None,
            )
        kw = db.update_project_loop.call_args.kwargs
        assert kw["current_stage_jobs"] == ["a", "b"]
        assert kw["current_job_id"] is None
```

- [ ] **Step 2: Run to verify the width-1 test fails**

Run: `python -m pytest tests/test_loop_unified_advance.py -v`
Expected: `test_width1_writes_membership_and_display_mirror` FAILS (today width 1 writes `current_stage_jobs=[]`); the fan-out test passes.

- [ ] **Step 3: Implement — writeback writes both columns for every width**

Replace the tail of `_writeback_loop_stage` in `orchestrator/main.py` (the `if len(ids) == 1:` split) and its docstring:

```python
async def _writeback_loop_stage(
    loop_id: str,
    *,
    jobs: list[dict[str, Any]],
    seq_index: int,
    remaining: int | None,
    total: int,
    consecutive: int,
    last_error: str | None,
    campaign: Any = _WB_UNSET,
) -> dict[str, Any] | None:
    """Point a loop at a freshly-spawned turn.

    Every turn is barrier-tracked: ``current_stage_jobs`` holds the members
    (width 1 included) and the atomic barrier drains it when the last one
    finishes. ``current_job_id`` is a display-only mirror — the member's id
    for a width-1 turn (cockpit links, MCP formatters), NULL for fan-out
    turns. Mirrors the counters the advance always wrote.

    ``campaign`` (campaign-mode loops) rides the SAME row update as the
    pointer, so the queue-cursor/status mutation and the stage pointer can
    never tear apart from each other (docs/features/loop_campaign_scheduling.md).
    """
    ids = [str(j["id"]) for j in jobs]
    common = dict(
        seq_index=seq_index,
        remaining_iterations=remaining,
        consecutive_failures=consecutive,
        total_jobs_run=total,
        last_error=last_error,
    )
    if campaign is not _WB_UNSET:
        common["campaign"] = campaign
    return await postgres_db.update_project_loop(
        loop_id,
        current_job_id=(ids[0] if len(ids) == 1 else None),
        current_stage_jobs=ids,
        **common,
    )
```

- [ ] **Step 4: Implement — barrier claim also nulls the display mirror**

In `orchestrator/database/postgres.py`, `claim_project_loop_stage_barrier`, change the UPDATE's SET clause:

```python
                UPDATE project_loops
                SET current_stage_jobs = '[]'::jsonb,
                    current_job_id = NULL,
                    updated_at = now()
```

(everything else in the statement unchanged) and extend the docstring's last paragraph:

```python
        Membership is immutable for the stage's life (this is the only writer
        that empties it, in one shot, also nulling the width-1 display mirror
        ``current_job_id``), so the post-rotate state is the same single
        signature the torn-advance sweeper reasons about:
        ``current_job_id IS NULL AND current_stage_jobs = '[]'``.
```

- [ ] **Step 5: Run the new file plus the neighbouring suites**

Run: `python -m pytest tests/test_loop_unified_advance.py tests/test_loop_campaign_scheduling.py tests/test_project_loops.py -q`
Expected: PASS. If a campaign test asserts exact writeback kwargs for a single-member spawn (`current_job_id`/`current_stage_jobs`), update that assertion to the new invariant (membership `[job_id]` **and** mirror `job_id`) — do not delete the assertion.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py orchestrator/database/postgres.py tests/test_loop_unified_advance.py tests/test_loop_campaign_scheduling.py
git commit -m "feat(loop): width-1 turns are barrier-tracked; barrier drain clears both pointers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: One advance path — membership routing, campaign threading, legacy path deleted

**Files:**
- Modify: `orchestrator/main.py` (`_advance_project_loop`, `_advance_loop_parallel_member` → `_advance_loop_member`, `_rotate_loop_to_next_stage` docstring)
- Modify: `orchestrator/database/postgres.py` (delete `claim_project_loop_advance`)
- Test: `tests/test_loop_unified_advance.py`

**Interfaces:**
- Consumes: Task 3's invariants (membership always written; barrier drains both columns).
- Produces: `_advance_project_loop(job, result, actions)` — signature unchanged, body routes ONLY by membership in `current_stage_jobs`; `_advance_loop_member(job, result, actions, *, loop, ctx)` (renamed) always calls `_rotate_loop_to_next_stage(..., completed_job=job, completed_ctx=ctx, completed_failed=<member failed>)`. `postgres_db.claim_project_loop_advance` no longer exists. The sweeper (Task 6) and resume (Task 5) rely on this entry.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop_unified_advance.py`:

```python
def _advance_db(loop: dict, jobs: list[dict], *, barrier: bool = True) -> AsyncMock:
    db = AsyncMock()
    db.get_project_loop.return_value = loop
    db.claim_project_loop_stage_barrier.return_value = barrier
    db.get_loop_stage_member_statuses.return_value = {
        str(j["id"]): j["status"] for j in jobs
    }
    return db


def _advance_patches(stack: ExitStack, db: AsyncMock, *, rotate: AsyncMock | None):
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(
        patch("main._merge_and_retro_loop_job", AsyncMock(return_value=("skipped", None)))
    )
    stack.enter_context(patch("main._notify_loop_user_questions", AsyncMock()))
    if rotate is not None:
        stack.enter_context(patch("main._rotate_loop_to_next_stage", rotate))


class TestUnifiedAdvance:
    @pytest.mark.asyncio
    async def test_width1_member_advances_through_barrier(self):
        job = _job()
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        db.claim_project_loop_stage_barrier.assert_awaited_once_with(
            LOOP_ID, job["id"]
        )
        kw = rotate.await_args.kwargs
        assert kw["completed_job"] is job
        assert kw["completed_ctx"]["loop_id"] == LOOP_ID
        assert kw["completed_failed"] is False
        assert kw["next_remaining"] == 5  # 6 - 1, charged at the barrier
        assert kw["consecutive"] == 0

    @pytest.mark.asyncio
    async def test_nonmember_hook_is_a_noop(self):
        member, stray = _job(), _job()
        loop = _loop(current_stage_jobs=[member["id"]], current_job_id=member["id"])
        db = _advance_db(loop, [member, stray])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(stray, {}, [])
        db.claim_project_loop_stage_barrier.assert_not_awaited()
        rotate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pointer_only_row_is_a_noop(self):
        # Pre-0063 shape (pointer set, empty membership) is NOT advanced by
        # the engine — the migration backfill / sweeper adopt branch owns it.
        job = _job()
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        db.claim_project_loop_stage_barrier.assert_not_awaited()
        rotate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_barrier_race_backs_off_after_merge(self):
        job = _job()
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job], barrier=False)
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        rotate.assert_not_awaited()
        db.update_project_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_stop_clears_both_pointer_columns(self):
        job = _job()
        loop = _loop(
            remaining_iterations=1,
            current_stage_jobs=[job["id"]],
            current_job_id=job["id"],
        )
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        rotate.assert_not_awaited()
        kw = db.update_project_loop.call_args.kwargs
        assert kw["status"] == "completed" and kw["stop_reason"] == "budget"
        assert kw["current_job_id"] is None
        assert kw["current_stage_jobs"] == []

    @pytest.mark.asyncio
    async def test_width1_failure_keeps_specific_error_and_increments(self):
        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {"error": "kaboom"}, [])
        kw = rotate.await_args.kwargs
        assert kw["consecutive"] == 1
        assert kw["last_error"] == "kaboom"  # not the fan-out aggregate string
        assert kw["completed_failed"] is True

    @pytest.mark.asyncio
    async def test_fanout_partial_failure_resets_consecutive(self):
        ok = _job(role="scholar")
        bad = _job(role="product-qa", status="failed")
        loop = _loop(
            consecutive_failures=2,
            current_stage_jobs=[ok["id"], bad["id"]],
            current_job_id=None,
        )
        db = _advance_db(loop, [ok, bad])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(ok, {}, [])
        kw = rotate.await_args.kwargs
        assert kw["consecutive"] == 0 and kw["last_error"] is None

    @pytest.mark.asyncio
    async def test_campaign_step_fires_from_the_barrier_path(self):
        # The [A1] regression this whole task exists for: a campaign job
        # completing through the (now only) barrier path must reach
        # _advance_planner_campaign with its own job + context.
        job = _job(role="critic", loop_seq_index=1)
        loop = _loop(
            scheduling="campaign",
            role_sequence=[["scholar", "product-qa"], "critic", "developer"],
            seq_index=1,
            current_stage_jobs=[job["id"]],
            current_job_id=job["id"],
        )
        db = _advance_db(loop, [job])
        planner = AsyncMock(return_value=(True, None))  # handled: member spawned
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=None)
            stack.enter_context(patch("main._advance_planner_campaign", planner))
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        planner.assert_awaited_once()
        kw = planner.await_args.kwargs
        assert kw["completed_job"] is job
        assert kw["completed_ctx"]["loop_role"] == "critic"
        assert kw["completed_failed"] is False
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python -m pytest tests/test_loop_unified_advance.py -v`
Expected: `TestWritebackLoopStage` passes; `TestUnifiedAdvance` fails — today a width-1 loop with membership routes into `_advance_loop_parallel_member`, which passes NO `completed_job` to the rotate (`completed_job is job` assertion fails, campaign test fails), and `test_width1_failure_keeps_specific_error…` fails on `last_error == "all stage jobs failed"`.

- [ ] **Step 3: Implement the unified entry**

In `orchestrator/main.py`, replace the body of `_advance_project_loop` (delete the `current_job_id` branch, the `claim_project_loop_advance` call, and everything after it down to — and including — its trailing `_rotate_loop_to_next_stage(...)` call):

```python
async def _advance_project_loop(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
) -> None:
    """Advance a project self-improvement loop when one of its in-flight
    turn's jobs completes.

    Every turn is a barrier-tracked set of jobs in ``current_stage_jobs``
    (width 1 included) — the engine's ONLY advance path
    (docs/features/loop_unified_engine.md). Membership is the idempotency
    guard: a stale or re-delivered completion hook for a job outside the
    current turn is a no-op, and the atomic barrier claim inside
    ``_advance_loop_member`` guarantees exactly one rotate per turn. Loop
    jobs run bare, so this is the only completion hook that fires for them.
    """
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    loop_id = (ctx or {}).get("loop_id")
    if not loop_id:
        return

    loop = await postgres_db.get_project_loop(str(loop_id))
    if not loop or loop.get("status") != "running":
        return  # paused / stopped / terminal — leave the current job, don't advance

    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if str(job["id"]) not in stage_ids:
        return  # not a member of the in-flight turn
    await _advance_loop_member(job, result, actions, loop=loop, ctx=ctx or {})
```

- [ ] **Step 4: Implement the member advance (rename + threading + width-aware error)**

Rename `_advance_loop_parallel_member` → `_advance_loop_member` and replace its body:

```python
async def _advance_loop_member(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
    *,
    loop: dict[str, Any],
    ctx: dict[str, Any],
) -> None:
    """Advance a loop when a member of its in-flight turn completes.

    Each member merges + retros itself immediately (its artifact handling is
    independent), then hits the barrier: ``claim_project_loop_stage_barrier``
    drains the turn and returns True to exactly ONE caller — the member that
    finishes last (trivially, the job itself on a width-1 turn). Only that
    caller aggregates the turn outcome (a turn counts as a failure only if
    EVERY member failed; one success resets the consecutive counter), checks
    the stop conditions, and rotates to the next stage. Every earlier
    finisher just does its own merge and backs off.

    The barrier winner's job + decoded context feed the campaign step inside
    ``_rotate_loop_to_next_stage``. Campaign-relevant jobs (the checkpoint
    critic and campaign members) only ever occupy width-1 turns by planner
    grammar, so the winner IS the campaign job whenever it matters; for a
    fan-out turn the campaign step falls through as a no-op.
    docs/features/loop_unified_engine.md (Phase 1).
    """
    loop_id = str(loop["id"])
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]

    failed = bool(result.get("error")) or job.get("status") == "failed"
    member_error = (result.get("error") or "job failed") if failed else None

    # Per-member artifact handling: squash-merge, F29 flags, retro. Best
    # effort — never blocks the barrier.
    await _merge_and_retro_loop_job(
        job,
        ctx=ctx,
        loop=loop,
        loop_id=loop_id,
        actions=actions,
        failed=failed,
        last_error=member_error,
    )
    # Surface this member's `user-question` KB notes (every member passes
    # here regardless of who wins the barrier).
    await _notify_loop_user_questions(loop, job)

    # Barrier: only the last member to go terminal claims the rotate.
    if not await postgres_db.claim_project_loop_stage_barrier(loop_id, str(job["id"])):
        return  # an earlier finisher, a lost co-last race, or a stray hook

    # Last out. Aggregate the turn outcome from the members' final statuses
    # (captured from the pre-drain membership snapshot).
    statuses = await postgres_db.get_loop_stage_member_statuses(stage_ids)
    member_states = [statuses.get(mid, "failed") for mid in stage_ids]
    all_failed = bool(member_states) and all(s == "failed" for s in member_states)
    consecutive = (int(loop.get("consecutive_failures") or 0) + 1) if all_failed else 0
    # A width-1 turn keeps the member's specific error (the pre-unification
    # single-role behavior); a fan-out aggregate can only say everything failed.
    last_error = (
        (member_error if len(stage_ids) == 1 else "all stage jobs failed")
        if all_failed
        else None
    )

    remaining = loop.get("remaining_iterations")
    next_remaining = (remaining - 1) if remaining is not None else None

    stop_reason = _loop_stop_reason(
        loop, next_remaining=next_remaining, consecutive=consecutive
    )
    if stop_reason:
        await postgres_db.update_project_loop(
            loop_id,
            status=("failed" if stop_reason == "failures" else "completed"),
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=last_error,
            stop_reason=stop_reason,
            current_job_id=None,
            current_stage_jobs=[],
        )
        actions.append(f"project loop {str(loop_id)[:8]} stopped ({stop_reason})")
        return

    await _rotate_loop_to_next_stage(
        loop,
        seq_index_completed=int(loop.get("seq_index") or 0),
        base_total=int(loop.get("total_jobs_run") or 0),
        next_remaining=next_remaining,
        consecutive=consecutive,
        last_error=last_error,
        actions=actions,
        completed_job=job,
        completed_ctx=ctx,
        completed_failed=failed,
    )
```

- [ ] **Step 5: Delete the legacy claim and fix stale prose**

- `orchestrator/database/postgres.py`: delete the whole `claim_project_loop_advance` method.
- `orchestrator/main.py`, `_rotate_loop_to_next_stage` docstring: replace "Shared by the single-role advance and the parallel-stage last finisher" with "Called by the barrier winner (``_advance_loop_member``)"; delete the sentence about the parallel path "deliberately passing neither".
- `grep -rn "claim_project_loop_advance\|_advance_loop_parallel_member" orchestrator/ src/ tests/` — must return nothing (fix any stragglers; `tests/test_project_loop_sweeper.py` docstrings get rewritten in Task 6, but code references must be gone now).

- [ ] **Step 6: Run the suites**

Run: `python -m pytest tests/test_loop_unified_advance.py tests/test_loop_campaign_scheduling.py tests/test_project_loops.py tests/test_loop_merge.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/main.py orchestrator/database/postgres.py tests/test_loop_unified_advance.py
git commit -m "feat(loop): one advance path — membership routing, campaign threaded through the barrier, legacy rotate deleted

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Membership-shaped resume, plan-filing gate, and stop endpoint

**Files:**
- Modify: `orchestrator/main.py` (`_resume_project_loop`, `file_loop_plan`)
- Modify: `orchestrator/routers/project_loops.py` (`stop_project_loop`)
- Test: `tests/test_loop_campaign_scheduling.py` (intake gate fixtures), `tests/test_loop_unified_advance.py` (resume)

**Interfaces:**
- Consumes: Task 4's membership-only entry.
- Produces: resume re-advances terminal members of the in-flight turn (no dead pointer branch); `file_loop_plan` gates on membership in `current_stage_jobs`; stop clears both pointer columns.

- [ ] **Step 1: Update the intake-gate fixtures and write the failing tests**

In `tests/test_loop_campaign_scheduling.py`:
- the shared `_loop()` fixture: change `"current_job_id": CRITIC_JOB_ID,` + `"current_stage_jobs": [],` to `"current_job_id": CRITIC_JOB_ID,` + `"current_stage_jobs": [CRITIC_JOB_ID],`
- the "not the in-flight job" intake test (`_loop(current_job_id=str(uuid.uuid4()))`): change to `_loop(current_job_id=None, current_stage_jobs=[str(uuid.uuid4())])` and keep its 409 assertion (update its `match=` if it pins the old detail text; the new detail is "not one of the loop's in-flight jobs").

Append to `tests/test_loop_unified_advance.py`:

```python
class TestResume:
    @pytest.mark.asyncio
    async def test_resume_readvances_only_terminal_members(self):
        done = _job(status="completed")
        running = _job(status="processing")
        loop = _loop(current_stage_jobs=[done["id"], running["id"]])
        db = AsyncMock()
        db.update_project_loop.return_value = loop
        db.get_project_loop.return_value = loop
        by_id = {done["id"]: done, running["id"]: running}
        db.get_job.side_effect = lambda jid: by_id.get(str(jid))
        adv = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._advance_project_loop", adv))
            from main import _resume_project_loop

            await _resume_project_loop(LOOP_ID)
        adv.assert_awaited_once_with(done, {}, [])
```

Run: `python -m pytest tests/test_loop_unified_advance.py::TestResume tests/test_loop_campaign_scheduling.py -q`
Expected: the resume test PASSES already (the stage branch exists) — it pins the contract before the branch deletion. The intake test with the reworded 409 detail FAILS until Step 2.

- [ ] **Step 2: Implement**

`orchestrator/main.py` — replace `_resume_project_loop`:

```python
async def _resume_project_loop(loop_id: str) -> dict[str, Any] | None:
    """Resume a paused project loop.

    Sets status back to ``running``. The barrier is gated on
    ``status='running'``, so any member of the in-flight turn that went
    terminal while the loop was paused didn't advance it. Re-run the advance
    for each already-terminal member so the barrier can fire (the sweeper
    would eventually catch this too); members still running advance the loop
    naturally on completion.
    """
    loop = await postgres_db.update_project_loop(loop_id, status="running")
    if not loop:
        return None
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if stage_ids:
        for mid in stage_ids:
            mjob = await postgres_db.get_job(mid)
            if mjob and mjob.get("status") in ("completed", "failed", "cancelled"):
                await _advance_project_loop(mjob, {}, [])
        loop = await postgres_db.get_project_loop(loop_id)
    return loop
```

`orchestrator/main.py` — in `file_loop_plan`, replace the in-flight gate (`if str(loop.get("current_job_id") or "") != str(job["id"]):` block):

```python
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if str(job["id"]) not in stage_ids:
        raise HTTPException(
            status_code=409,
            detail="Job is not one of the loop's in-flight jobs",
        )
```

`orchestrator/routers/project_loops.py` — in `stop_project_loop`, the final update:

```python
    return await postgres_db.update_project_loop(
        str(loop["id"]),
        status="stopped",
        stop_reason="user",
        current_job_id=None,
        current_stage_jobs=[],
    )
```

- [ ] **Step 3: Run the suites**

Run: `python -m pytest tests/test_loop_unified_advance.py tests/test_loop_campaign_scheduling.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/main.py orchestrator/routers/project_loops.py tests/test_loop_unified_advance.py tests/test_loop_campaign_scheduling.py
git commit -m "feat(loop): membership-shaped resume, plan-filing gate, and stop endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Sweeper — one wedge signature, one heal

**Files:**
- Modify: `orchestrator/services/project_loop_sweeper.py` (`_sweep_tick`, `_sweep_parallel_stage` → `_sweep_stage`, `_heal_wedged_loop`, module docstring)
- Modify: `orchestrator/database/postgres.py` (extend `heal_project_loop_stage`, delete `heal_project_loop_pointer`)
- Test: `tests/test_project_loop_sweeper.py` (rewrite the heal/tick classes; derive classes untouched)

**Interfaces:**
- Consumes: Task 4's membership-only advance; barrier drain clearing both columns (Task 3).
- Produces: `heal_project_loop_stage(loop_id, member_job_ids, *, current_job_id, seq_index, total_jobs_run, remaining_iterations, min_wedge_age_seconds)` — note the NEW keyword-only `current_job_id: str | None` (the width-1 display mirror; pass `None` for fan-out). `_heal_wedged_loop(db, loop)` returns a representative job ONLY when the restored turn is fully terminal (advance now), else `None`. `heal_project_loop_pointer` no longer exists. `_sweep_tick` adopts legacy pointer-only rows into membership.

- [ ] **Step 1: Extend the stage heal and delete the pointer heal**

`orchestrator/database/postgres.py` — replace `heal_project_loop_stage`:

```python
    async def heal_project_loop_stage(
        self,
        loop_id: str,
        member_job_ids: List[str],
        *,
        current_job_id: str | None,
        seq_index: int,
        total_jobs_run: int,
        remaining_iterations: int | None,
        min_wedge_age_seconds: float = 600.0,
    ) -> bool:
        """Restore a torn turn's in-flight membership (width 1 included).

        The torn-advance recovery (docs/issues/loop_advance_nonatomic_wedges_loop.md):
        an advance that spawned the next turn's jobs — or drained the barrier —
        but lost its write-back leaves the loop wedged with
        ``current_job_id IS NULL AND current_stage_jobs='[]'`` while the
        spawned members run on (or sit terminal). This re-points
        ``current_stage_jobs`` at the members so the barrier can fire,
        restores the width-1 display mirror (``current_job_id`` — pass None
        for a fan-out turn), and reconciles the counters the lost write-back
        would have set.

        Guarded on ``current_job_id IS NULL AND status='running'`` and an
        empty stage set so concurrent sweeper replicas heal exactly once —
        the loser matches no row and backs off — AND on the wedge being at
        least ``min_wedge_age_seconds`` old on the DB clock. The age gate is
        what separates a *torn* advance from an advance *in flight*: every
        healthy advance also traverses the both-cleared state between its
        barrier claim and its write-back, and healing inside that window
        re-arms the claim and double-spawns the next turn (observed live:
        duplicate iter-14 critics).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE project_loops SET current_stage_jobs = $2::jsonb, "
                "current_job_id = $3, seq_index = $4, total_jobs_run = $5, "
                "remaining_iterations = $6, updated_at = now() "
                "WHERE id = $1 AND current_job_id IS NULL AND status = 'running' "
                "AND current_stage_jobs = '[]'::jsonb "
                "AND updated_at < now() - make_interval(secs => $7)",
                UUID(loop_id),
                json.dumps([str(j) for j in member_job_ids]),
                UUID(current_job_id) if current_job_id else None,
                seq_index,
                total_jobs_run,
                remaining_iterations,
                float(min_wedge_age_seconds),
            )
        return result.endswith(" 1")
```

Delete the whole `heal_project_loop_pointer` method. Then `grep -rn "heal_project_loop_pointer" orchestrator/ src/` — only the sweeper (fixed below) may still reference it; after Step 2 the grep must be empty outside tests, and after Step 3 empty everywhere.

- [ ] **Step 2: Rewrite the sweeper's tick and heal**

`orchestrator/services/project_loop_sweeper.py`:

Replace `_sweep_tick`:

```python
async def _sweep_tick(db: Any, advance_fn: AdvanceFn) -> int:
    """Recover any running loop whose in-flight turn stalled.

    Returns the number of loops recovered this tick.
    """
    recovered = 0
    for loop in await db.list_running_project_loops():
        # The in-flight turn is barrier-tracked in current_stage_jobs (width 1
        # included). The backstop only steps in once every member is terminal
        # (a missed barrier hook); the atomic claim makes the re-run idempotent.
        stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
        if stage_ids:
            recovered += await _sweep_stage(db, loop, stage_ids, advance_fn)
            continue

        cur = loop.get("current_job_id")
        if cur:
            # Transitional (a pre-0063 writer raced the deploy): a width-1
            # turn tracked only by the display pointer. Adopt it into the
            # barrier set so the unified advance can drive it; it is swept
            # as a normal stage next tick.
            logger.warning(
                "project loop %s: adopting legacy width-1 pointer %s into "
                "current_stage_jobs",
                str(loop.get("id"))[:8],
                str(cur)[:8],
            )
            await db.update_project_loop(
                str(loop["id"]), current_stage_jobs=[str(cur)]
            )
            continue

        # Both columns empty: a torn advance (write-back lost) or a crash
        # before the first spawn. The heal restores membership; it returns a
        # job only when the restored turn is already fully terminal, meaning
        # the rotate itself was lost — re-run it now.
        job = await _heal_wedged_loop(db, loop)
        if not job:
            continue
        logger.warning(
            "project loop %s: healed turn is fully terminal — recovering via "
            "barrier advance (lost rotate)",
            str(loop.get("id"))[:8],
        )
        try:
            # result={} → the advance derives failure from job.status, so
            # terminal-success and terminal-failure are both handled right.
            await advance_fn(job, {}, [])
            recovered += 1
        except Exception:
            logger.exception("project loop %s: sweeper advance failed", loop.get("id"))
    return recovered
```

Rename `_sweep_parallel_stage` → `_sweep_stage` (body unchanged except the docstring's first line: "Backstop for a loop with a turn in flight (any width)." and drop the word "PARALLEL" where it appears).

Replace `_heal_wedged_loop`:

```python
async def _heal_wedged_loop(db: Any, loop: dict[str, Any]) -> dict[str, Any] | None:
    """Restore membership for a running loop with both pointer columns empty.

    That state is either the transient window of a live advance (young — see
    the age gate) or a torn advance whose write-back was lost. The newest
    STAGE (all jobs sharing the max ``loop_iteration``) is what the lost
    write-back would have pointed the loop at; restore it as the barrier
    membership (width 1 included, with the display mirror for a single
    member):

      * some member still running → the barrier fires when they finish;
        returns None (nothing to advance now).
      * all members terminal (the rotate itself was lost) → returns a
        representative so the caller re-runs the advance; the atomic barrier
        claim makes the re-run idempotent.

    Guarded by the age gate + the DB-side heal guards so a live advance's
    transient window is never mistaken for a tear (the double-spawn
    incident, docs/issues/loop_advance_nonatomic_wedges_loop.md).
    """
    loop_id = str(loop.get("id"))
    age = _wedge_age_seconds(loop)
    if age is not None and age < HEAL_GRACE_SECONDS:
        # Freshly-cleared pointers = the claim of a live advance, not a tear.
        # Healing now would re-arm the claim and double-spawn the turn.
        logger.debug(
            "project loop %s: pointers cleared but only %.0fs old — advance "
            "likely in flight, deferring heal",
            loop_id[:8],
            age,
        )
        return None

    members = await db.get_newest_loop_stage(loop_id)
    ctx = members[0].get("context") if members else None
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    derived = _derive_loop_counters(loop, ctx or {}) if members else None
    if not derived:
        # A running loop should always have an in-flight turn (start/advance
        # set one). No job at all, or an unreadable iteration stamp — nothing
        # to re-point at safely.
        logger.warning(
            "project loop %s is running with no in-flight turn — needs attention",
            loop_id[:8],
        )
        return None

    seq_index, total_jobs_run, remaining = derived
    member_ids = [str(m["id"]) for m in members]
    if not await db.heal_project_loop_stage(
        loop_id,
        member_ids,
        current_job_id=(member_ids[0] if len(member_ids) == 1 else None),
        seq_index=seq_index,
        total_jobs_run=total_jobs_run,
        remaining_iterations=remaining,
        min_wedge_age_seconds=HEAL_GRACE_SECONDS,
    ):
        # Another replica healed first, or the DB-side age guard saw a
        # fresher row than our read — not ours.
        return None

    non_terminal = [m for m in members if m.get("status") not in _TERMINAL]
    logger.warning(
        "project loop %s: healed torn advance — restored %d-member turn "
        "(%d still running; seq_index %s, remaining %s)",
        loop_id[:8],
        len(members),
        len(non_terminal),
        seq_index,
        remaining,
    )
    if non_terminal:
        return None  # members' completion hooks / next tick fire the barrier
    return members[0]  # all terminal: the rotate was lost — advance now
```

Also rewrite the module docstring's recovery narrative (paragraphs 2–4) to the single-signature story: one wedge shape (`current_job_id IS NULL AND current_stage_jobs='[]'`), heal = restore membership (+width-1 mirror), advance only when the restored turn is fully terminal; keep the age-gate incident text as is; delete the "Parallel (fan-out) stages add one shape" paragraph (no longer a special case) and mention the transitional adopt branch in one sentence.

- [ ] **Step 3: Rewrite the sweeper tests**

`tests/test_project_loop_sweeper.py` — keep `TestDeriveLoopCounters` and `TestDeriveLoopCountersStamps` byte-identical, and keep the `_loop`/`_job`/`_iter_of`/`_newest_stage` helpers. Replace the import block, the `_db` helper, and the heal/tick/stage classes:

```python
from services.project_loop_sweeper import (
    HEAL_GRACE_SECONDS,
    _derive_loop_counters,
    _heal_wedged_loop,
    _sweep_stage,
    _sweep_tick,
)
```

```python
def _db(
    loops: list[dict],
    jobs: list[dict],
    *,
    stage_heal_wins: bool = True,
    barrier_wins: bool = True,
):
    """Fake of the DB methods the sweeper touches. ``jobs`` newest-first."""
    db = AsyncMock()
    db.list_running_project_loops.return_value = loops
    by_id = {str(j["id"]): j for j in jobs}
    db.get_job.side_effect = lambda job_id: by_id.get(str(job_id))
    db.get_newest_loop_stage.return_value = _newest_stage(jobs)
    db.heal_project_loop_stage.return_value = stage_heal_wins
    db.claim_project_loop_stage_barrier.return_value = barrier_wins
    db.get_loop_stage_member_statuses.side_effect = lambda ids: {
        str(i): by_id[str(i)]["status"] for i in ids if str(i) in by_id
    }
    return db
```

```python
class TestHealWedgedLoop:
    """Both pointer columns empty → restore the newest stage as membership."""

    @pytest.mark.asyncio
    async def test_width1_terminal_restores_membership_and_returns_job(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is orphan
        db.heal_project_loop_stage.assert_awaited_once_with(
            LOOP_ID,
            [orphan["id"]],
            current_job_id=orphan["id"],
            seq_index=0,
            total_jobs_run=10,
            remaining_iterations=24,
            min_wedge_age_seconds=HEAL_GRACE_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_width1_running_restores_membership_without_advance(self):
        orphan = _job(10, "scholar", status="processing")
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_awaited_once()
        kwargs = db.heal_project_loop_stage.await_args.kwargs
        assert kwargs["current_job_id"] == orphan["id"]

    @pytest.mark.asyncio
    async def test_fanout_all_terminal_returns_representative(self):
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is not None and healed["id"] in {a["id"], b["id"]}
        args, kwargs = db.heal_project_loop_stage.await_args
        assert set(args[1]) == {a["id"], b["id"]}
        assert kwargs["current_job_id"] is None  # fan-out: no display mirror
        assert kwargs["seq_index"] == 0
        assert kwargs["total_jobs_run"] == 2
        assert kwargs["remaining_iterations"] == 7

    @pytest.mark.asyncio
    async def test_fanout_some_running_restores_and_defers(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])
        assert await _heal_wedged_loop(db, _loop()) is None
        args, _ = db.heal_project_loop_stage.await_args
        assert set(args[1]) == {a["id"], b["id"]}  # FULL membership restored

    @pytest.mark.asyncio
    async def test_json_string_context_decoded(self):
        orphan = _job(10, "scholar")
        orphan["context"] = json.dumps(orphan["context"])
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is orphan

    @pytest.mark.asyncio
    async def test_no_jobs_no_heal(self):
        db = _db([_loop()], [])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_underivable_context_no_heal(self):
        orphan = _job(10, "scholar")
        orphan["context"] = {}
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_guard_backs_off(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan], stage_heal_wins=False)
        assert await _heal_wedged_loop(db, _loop()) is None


class TestHealAgeGate:
    """Empty pointers are only a tear once they're OLD — every healthy
    advance also traverses the both-cleared state between its barrier claim
    and its write-back (the live iter-14 double-spawn incident)."""

    @pytest.mark.asyncio
    async def test_fresh_wedge_is_deferred_silently(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is None
        db.get_newest_loop_stage.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_wedge_heals(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(hours=12))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is orphan

    @pytest.mark.asyncio
    async def test_naive_timestamp_treated_as_utc(self):
        orphan = _job(10, "scholar")
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
        fresh = _loop(updated_at=naive_now - timedelta(seconds=5))
        db = _db([fresh], [orphan])
        assert await _heal_wedged_loop(db, fresh) is None
        stale = _loop(updated_at=naive_now - timedelta(hours=12))
        db = _db([stale], [orphan])
        assert await _heal_wedged_loop(db, stale) is orphan

    @pytest.mark.asyncio
    async def test_unknown_age_defers_to_db_guard(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=None)
        db = _db([loop], [orphan], stage_heal_wins=False)
        assert await _heal_wedged_loop(db, loop) is None
        db.heal_project_loop_stage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_tick_skips_fresh_wedge_without_advance(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc))
        db = _db([loop], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()


class TestSweepStage:
    """Backstop for a loop with a turn in flight (any width): act only once
    every member is terminal (a missed barrier), idempotently (via the barrier)."""

    def _stage_loop(self, member_ids, **over):
        return _loop(
            current_job_id=(member_ids[0] if len(member_ids) == 1 else None),
            current_stage_jobs=list(member_ids),
            **over,
        )

    @pytest.mark.asyncio
    async def test_width1_terminal_member_advances(self):
        job = _job(9, "developer", status="failed", seq_index=2, remaining=25)
        db = _db([self._stage_loop([job["id"]])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once_with(job, {}, [])
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_width1_running_member_untouched(self):
        job = _job(9, "developer", status="processing", seq_index=2, remaining=25)
        db = _db([self._stage_loop([job["id"]])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fanout_all_terminal_advances_via_barrier(self):
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once()
        assert advance.await_args.args[0]["id"] in {a["id"], b["id"]}

    @pytest.mark.asyncio
    async def test_fanout_member_still_running_skips(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_terminal_states_still_advance(self):
        a = _job(2, "scholar", status="failed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="completed", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert (
            await _sweep_stage(
                db, self._stage_loop([a["id"], b["id"]]), [a["id"], b["id"]], advance
            )
            == 1
        )


class TestSweepTick:
    @pytest.mark.asyncio
    async def test_torn_advance_terminal_orphan_heals_then_advances(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        db.heal_project_loop_stage.assert_awaited_once()
        advance.assert_awaited_once_with(orphan, {}, [])

    @pytest.mark.asyncio
    async def test_torn_advance_running_orphan_heals_without_advance(self):
        orphan = _job(10, "scholar", status="processing")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.heal_project_loop_stage.assert_awaited_once()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pointer_only_row_is_adopted_into_membership(self):
        # Transitional: a pre-0063 writer left a width-1 turn tracked only by
        # current_job_id. The sweeper adopts it; no advance this tick.
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        db = _db([_loop(current_job_id=job["id"])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.update_project_loop.assert_awaited_once_with(
            LOOP_ID, current_stage_jobs=[job["id"]]
        )
        advance.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unhealable_loop_is_skipped(self):
        db = _db([_loop()], [])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_heal_race_does_not_advance(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan], stage_heal_wins=False)
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advance_exception_contained(self):
        job = _job(9, "developer", status="failed", seq_index=2, remaining=25)
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _db([loop], [job])
        advance = AsyncMock(side_effect=RuntimeError("boom"))
        assert await _sweep_tick(db, advance) == 0  # logged, not raised
```

Also update the file's module docstring to the single-signature story (mirror the sweeper module docstring rewrite), and delete the old `TestHealTornParallelStage` / `TestSweepParallelStage` / old `TestHealWedgedLoop` / old `TestSweepTick` classes wholesale — the classes above replace them.

- [ ] **Step 4: Run the suites**

Run: `python -m pytest tests/test_project_loop_sweeper.py tests/test_loop_unified_advance.py -q`
Expected: PASS. Then `grep -rn "heal_project_loop_pointer\|_sweep_parallel_stage" orchestrator/ src/ tests/` — empty.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/project_loop_sweeper.py orchestrator/database/postgres.py tests/test_project_loop_sweeper.py
git commit -m "feat(loop): sweeper heals one wedge signature — membership restore, pointer heal deleted

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Display consumers — MCP formatters + cockpit (rename + width-1 mirror)

**Files:**
- Modify: `src/tools/orchestrator/workflows.py` (`_format_loop`, `_explain_loop`)
- Modify: `cockpit/src/app/core/models/api.model.ts` (~997–1030)
- Modify: `cockpit/src/app/views/project-detail/project-loop.component.ts` (~237, ~246, ~474, ~477, ~658, ~885, ~901)
- Test: cockpit vitest suite; any pytest touching the workflow formatters

**Interfaces:**
- Consumes: width-1 loops now populate BOTH `current_job_id` and `current_stage_jobs`.
- Produces: displays show the single-job line for width-1 turns and the stage-set line only for width > 1; API types/values use `standard`/`campaign`.

- [ ] **Step 1: MCP formatters prefer the width-1 mirror**

`src/tools/orchestrator/workflows.py`, in `_format_loop`, replace the `if loop.get("current_stage_jobs"):` block:

```python
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if len(stage_ids) > 1:
        lines.append(f"  Current stage jobs: {', '.join(stage_ids)}")
```

(the `("Current job ID", "current_job_id")` row above it stays — width-1 turns render through it).

In `_explain_loop`, replace the stage/current-job block:

```python
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if len(stage_ids) > 1:
        lines.append("Current parallel stage jobs: " + ", ".join(stage_ids))
    elif loop.get("current_job_id"):
        lines.append(f"Current job: {loop['current_job_id']}")
```

Run `grep -rln "_format_loop\|_explain_loop" tests/` and run any hits: expected PASS (fix expectations only if a test pinned the both-lines-for-width-1 rendering).

- [ ] **Step 2: Cockpit — scheduling rename + width-1 mirror**

`cockpit/src/app/core/models/api.model.ts` (~997 and ~1026): update both comments and both fields:

```typescript
  scheduling?: 'standard' | 'campaign';
```

`cockpit/src/app/views/project-detail/project-loop.component.ts`:

~237 (running panel):

```html
                @if (l.scheduling === 'campaign') {
                  <div><span class="k">Scheduling</span><span class="v">Campaign — critic schedules campaigns</span></div>
                }
```

~246 (width-1 mirror — show the stage chip list only for real fan-outs):

```html
                @if ((l.current_stage_jobs?.length ?? 0) > 1) {
                  <div><span class="k">Stage jobs</span><span class="v mono">{{ stageJobsShort(l) }}</span></div>
                } @else if (l.current_job_id) {
                  <div><span class="k">Current job</span><span class="v mono">{{ l.current_job_id.slice(0, 8) }}</span></div>
                }
```

~474 (start form select):

```html
                <option value="">Standard — one stage per turn (default)</option>
                <option value="campaign">Campaign — critic schedules campaigns</option>
```

~477, ~885: `fScheduling() === 'planner'` → `fScheduling() === 'campaign'` (both sites).
~901: `if (this.fScheduling() === 'campaign') body.scheduling = 'campaign';`
~658: update the comment (`'' = standard (default), 'campaign' = the checkpoint critic may file multi-job campaigns`).
Also update the form-field `hint` text at ~468 ("Planner lets the critic…" → "Campaign lets the critic…").

Then `grep -n "planner\|rotation" cockpit/src/app/views/project-detail/project-loop.component.ts cockpit/src/app/views/project-detail/project-loop.component.spec.ts cockpit/src/app/core/models/api.model.ts` — remaining hits must be the `plannerIneligibility`/`plannerProblem` identifiers and their tests only; update any spec assertion that posts or asserts the literal `'planner'`/`'rotation'` scheduling values.

- [ ] **Step 3: Run the cockpit tests**

Run: `cd cockpit && npx vitest run`
Expected: PASS (suite is ~353 tests, ~4s). Fix any spec still pinning old literals.

- [ ] **Step 4: Commit**

```bash
git add src/tools/orchestrator/workflows.py cockpit/src/app/core/models/api.model.ts cockpit/src/app/views/project-detail/project-loop.component.ts cockpit/src/app/views/project-detail/project-loop.component.spec.ts
git commit -m "feat(loop): display consumers — standard/campaign vocabulary, width-1 mirror rendering

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Full gate, spec status, and the live smoke checklist

**Files:**
- Modify: `docs/features/loop_unified_engine.md` (Status section)
- No code changes.

**Interfaces:**
- Consumes: everything above.
- Produces: green full loop suite on the unified engine; spec marked Phase 1 implemented; a written k3d smoke checklist for the user-gated live AC.

- [ ] **Step 1: Run the full loop-adjacent gate**

Run:

```bash
python -m pytest tests/test_project_loops.py tests/test_project_loop_sweeper.py \
  tests/test_loop_campaign_scheduling.py tests/test_loop_merge.py \
  tests/test_critic_loop.py tests/test_loop_unified_advance.py -q
ruff check orchestrator/ src/ tests/
```

Expected: all PASS, ruff clean. (Python 3.14 deprecation noise is expected; failures are not.)

- [ ] **Step 2: Update the spec status**

In `docs/features/loop_unified_engine.md`, Status section, after the "PROPOSED" line add:

```markdown
**Phase 1 implemented on develop (2026-07-19)** — unified engine live in code: every turn barrier-tracked (width 1 included; `current_job_id` = display mirror), campaign advance threaded through the barrier, legacy rotate path + `claim_project_loop_advance` + pointer heal deleted, modes renamed `standard`/`campaign` (migration 0063). Phase-1 k3d smoke (sequential + campaign) pending. Phases 2–7 not started; their schema lands in later migrations (0064+), not 0063 as originally sketched.
```

- [ ] **Step 3: Commit**

```bash
git add docs/features/loop_unified_engine.md
git commit -m "docs(loop): mark unified-engine Phase 1 implemented

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: k3d live smoke (SKIP if no local cluster — report as not-run, do not fake it)**

Requires the local k3d `srw` stack (`tilt up`, memory: local_tilt_dev_stack_stinkpad). Drive via the orchestrator API in-pod with `X-Internal-Key: dev_mcp_internal_key` (memory: local_k3d_testing_via_orchestrator_api):

1. **Sequential smoke:** start a `standard` loop (`role_sequence=["scholar","critic"]`, `max_iterations=4`) on a test project. Verify after the first spawn: `current_stage_jobs = [<job>]` AND `current_job_id = <job>`. Let two advances happen; verify rotation order and counters match pre-unification semantics, and that between turns the row never shows a non-empty stage set with a stale member.
2. **Campaign smoke:** start a `campaign` loop with the canonical template (`[["scholar","product-qa"], "critic", "developer"]`), let the checkpoint critic file a plan (or inject one via the API), and verify the campaign member chain advances THROUGH the barrier: members spawn in order, `campaign.cursor` moves, disposition returns to the critic.
3. **Tear drill (width 1):** with a loop in flight, manually `UPDATE project_loops SET current_stage_jobs='[]'::jsonb, current_job_id=NULL, updated_at = now() - interval '20 minutes' WHERE id=...` and verify the sweeper restores membership + mirror within a tick and the loop continues.

---

## Self-Review Notes (already applied)

- **Spec coverage (Phase 1):** writeback width-1 → Task 3; barrier signature → Task 3; membership entry + campaign threading + stop-write symmetry + legacy deletion → Task 4; plan-filing gate + resume + stop endpoint → Task 5; sweeper/heal unification → Task 6; display consumers → Task 7; mode renames → Tasks 1–2, 7; tear drills → Task 6 tests + Task 8 live drill; k3d smokes → Task 8. Campaign invariants (single-row campaign+pointer write, `plan_job_id` guard, persist-before-spawn, `member_failures` separation, `seq_index=execution_slot`) are untouched by design — `_advance_planner_campaign`/`_spawn_campaign_member` bodies are not edited.
- **Consistency:** `_advance_loop_member` name is used consistently (Tasks 4–6); `heal_project_loop_stage`'s new `current_job_id` kwarg is threaded through sweeper + tests; `_sweep_stage` rename is reflected in imports.
- **Known intermediate states:** between Tasks 4 and 6 the sweeper still calls the deleted-in-Task-6 `heal_project_loop_pointer` — its unit tests keep passing (fake DB), and nothing deploys mid-plan. Task 6 closes the gap.
