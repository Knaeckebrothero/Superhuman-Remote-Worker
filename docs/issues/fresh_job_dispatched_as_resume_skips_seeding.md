# Fresh job dispatched as resume → workspace seeding skipped → agent starts brief-less

**Status:** Open — the one genuine product regression surfaced by `baseline-02`
(2026-08-05). Job `6cf03bf3-c1f5-44f2-8544-0a494faec08d` (bench `S4-csv-totals`
r2) emailed its owner "Task Brief is Empty - Action Required" and parked in
`waiting_for_reply`, despite `jobs.description` holding the full task text.

## Evidence chain

Agent pod log (`srw-agent-j-5b64491d`, job created 15:59:04Z, dispatched ~16:03):

1. `Processing job 6cf03bf3…`
2. **`Loaded frozen config for resumed job 6cf03bf3…`** — `agent.py:1912`, fires
   only when the dispatch carried `resume=True` *and* a resolved config existed
   in the DB. This job had never run: no checkpoint, no phases, no snapshots.
3. **`VM workspace is fresh — seeding from last snapshot for job …`** —
   `_reseed_from_snapshot_if_fresh` (agent.py:1786): the resume path's
   recovery arm. A fresh job has no snapshots, so nothing was restored — and
   because the flow was in resume mode, the *fresh* seeding path
   (instructions, `task_brief.md`, README) never ran either.
4. Graph init read `task_brief.md` → `FileNotFoundError` → silently `""`
   (`graph.py:471-474` — tolerant by design).
5. The model worked the 4 predefined strategic todos for ~17 min / 38 LLM
   requests with no task text, then did the *right* thing under the new
   reliability behavior: `send_message` (blocking) to the owner instead of
   inventing a task. Freeze `blocking_message` at 16:21, snapshot, pod reaped.

The workspace pod was per-job (`workspace-6cf03bf3-c1f`) — not a recycled pod.
The duplicate-A1 cancellation minutes earlier is unrelated (that job died
pre-assignment). The dispatch-side logs were lost to an orchestrator rollout
at 16:08 (old pods' logs gone), so *why* the dispatcher set `resume=True` for
a `created` job that had never started is the open question. Candidates:
dispatch retry after a provisioning hiccup marking the second attempt a
resume, or a pause/redispatch path (auto-assign polls `created` AND `paused`)
that treats every `paused` job as resumable regardless of whether it ever
started.

## Why this matters beyond bench

Any fresh job that hits this path starts with no brief. Full-autonomy jobs
now (correctly) block and email the owner rather than hallucinating a task —
good guard, but the failure appears to users as "the agent says my task is
empty", and every occurrence strands a job + burns the strategic-phase spend
that led up to the message.

## Fix sketches (both cheap, do both)

1. **Orchestrator (root cause):** only dispatch `resume=True` when the job
   actually has prior execution state (`started_at IS NOT NULL`, or
   phase_number > 0, or an existing checkpoint/freeze record). A never-started
   `paused`/retried job dispatches as fresh.
2. **Agent (belt + braces):** in the resume path, when workspace has no
   seeded-marker AND no snapshots AND no checkpoint exists, fall through to
   the fresh-seed path instead of proceeding brief-less
   (`_reseed_from_snapshot_if_fresh` already computes everything needed for
   this decision).

## Bench bookkeeping

S4 r1 was the WAN-outage casualty, r2 is this bug → S4's baseline-02 numbers
rest on r3 alone. Job left cancelled; evidence preserved (snapshot,
archived pod log via `/api/jobs/{id}/logs`, message thread `2b7c76`).
