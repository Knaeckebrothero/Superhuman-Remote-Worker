# Never-started job dispatched via /job/resume → virtual task brief serves empty

**Status:** FIXED 2026-08-06 (batch fix session) — all three complementary
fixes landed:
(a) **Lane choice**: the dispatcher and the admin-assign route now dispatch a
paused job via `/job/resume` only when a LangGraph checkpoint proves it ran
(`dispatch_guards.resume_lane_applies` + `PostgresDB.job_has_checkpoint`;
note: there is no `jobs.started_at` column, so checkpoint presence is the
never-started probe). The probe fails OPEN to the resume lane (sqlite
checkpointer backend / probe error → today's behavior), so only a positive
"no checkpoint" verdict flips the lane; fixes (b)+(c) cover those deploys.
(b) **Brief hydration**: the virtual `task_brief.md` provider now reads
`self._job_metadata` LIVE (the old bound alias went stale on dict
replacement), and on resume with no description the agent backfills
description/required_deliverables/kickoff_message from the orchestrator's new
internal `GET /api/jobs/{id}/brief` (agent DB handle as fallback; both
non-fatal, never overwrites dispatch-provided fields).
(c) **Tripwire**: `resume=True` with no checkpoint AND no snapshot →
`_note_resume_without_checkpoint`: ERROR log + brief hydration + the Phase-0
seed commit the fresh path would have made.
**Tests**: `tests/test_dispatch_guards.py::TestResumeLaneApplies`,
`tests/test_manual_assign_workspace_preflight.py::TestAssignLaneChoice`,
`tests/test_workspace_phase0_seed.py::TestTaskBriefHydration` (incl. the
live-read regression and the literal resume-metadata shape) +
`TestResumeWithoutCheckpointTripwire`.
**Live k3d 2026-08-06**: paused never-started row `d8f004fa` → dispatcher log
"paused with no checkpoint — never started; dispatching via the fresh
/job/start lane"; its first LLM request contained the description, kickoff
and Task Brief header (audit `llm_requests` probe `t|t|t`). Then paused
mid-processing (checkpoint present) → resume lane, and the resuming agent
logged "hydrated task brief on resume (description=123 chars)".
**Originally:** Open — the one genuine product regression surfaced by `baseline-02`
(2026-08-05). Job `6cf03bf3-c1f5-44f2-8544-0a494faec08d` (bench `S4-csv-totals`
r2) emailed its owner "Task Brief is Empty - Action Required" and froze in
`waiting_for_reply`, despite `jobs.description` holding the full task text.

> **Correction from the first draft:** the mechanism is NOT "seeding skipped so
> `task_brief.md` never got written". Since virtual directories (Slice 1, live
> 2026-08-01) the brief is **never on disk for any job** — it is served live by
> a provider reading `metadata["description"]`
> (`agent.py::_deploy_instruction_files → _task_brief`). The failure is that
> the resume path *starves the provider*.

## Causal chain (each step evidenced)

1. Job created 15:59:04Z by the bench sweeper; `jobs.description` populated
   (verified via API). `started_at` remained NULL throughout.
2. Between creation and ~16:03 something moved it to a re-dispatchable state
   without it ever starting (dispatch-attempt failure path or a
   pause/backstop sweep — the orchestrator logs for that window were lost to
   the 16:08 rollout; this sub-question is open).
3. Auto-assign re-dispatched it down the **resume lane**: the agent's
   `POST /job/resume` (`dual_app.py::resume_job`). Its request model
   (`JobResumeRequest`, `src/api/models.py:355`) **has no `description` /
   `required_deliverables` / `kickoff_message` fields at all**, and the
   handler builds `resume_metadata` from just
   `{config_upload_id, config_override, datasources, project_id}`.
4. Agent: `process_job(resume=True)` → "Loaded frozen config for resumed job"
   (pod log). `_deploy_instruction_files` registers the virtual providers over
   that thin metadata → `task_brief.md` serves a bare
   `# Task Brief / ## Description` skeleton (no description, and no
   deliverable-contract block since `required_deliverables` was dropped too).
5. The checkpointer finds no checkpoint ("No phase snapshots found … starting
   fresh") — the job never ran — so the graph composes its FIRST message from
   the empty brief. The model read `task_brief.md` (served, empty) and
   `instructions.md` (served, template boilerplate — proving the overlay
   itself worked), concluded it had no task, and correctly sent a blocking
   message instead of inventing one (thread `2b7c76`, 16:21Z).

## The latent half — affects EVERY resumed job, not just misrouted ones

Pre-virtual-dirs, a genuinely resumed job re-read the on-disk brief written at
first start. Post-virtual-dirs, any resume serves an **empty** brief for the
rest of the job: usually invisible (the first HumanMessage lives in the
checkpoint), but every later brief re-read — post-compaction recovery, the
"task brief is saved to task_brief.md for reference" pointer the preamble
gives the model — now returns the skeleton. This is exactly the "same-pod
resume / git-less resume" scenario the virtual-dirs design doc lists as
**not exercised** at its live gate.

## Fixes (complementary, all small)

1. **Orchestrator (root):** a paused-but-never-started job
   (`started_at IS NULL`, no phases/checkpoint) must re-dispatch through the
   fresh `/job/start` lane, not `/job/resume`.
2. **Resume hydration (heals the latent case too):** make the brief provider
   self-sufficient — when `metadata["description"]` is empty, fetch the job
   row (description, required_deliverables, kickoff) from the orchestrator via
   the client the agent already holds; alternatively add those fields to
   `JobResumeRequest` and pass them through in `resume_metadata`.
3. **Guard (cheap tripwire):** if `resume=True` but the graph is about to
   start fresh (no checkpoint), log at ERROR and prefer the fresh-start path —
   that combination is always a routing bug.

## Bench bookkeeping

S4 r1 was the WAN-outage casualty, r2 is this bug → S4's baseline-02 numbers
rest on r3 alone. The job was cancelled after evidence capture (freeze
snapshot, archived pod log via `/api/jobs/{id}/logs`, message thread
`2b7c76`); the bench ledger's `waiting_for_reply` label for it is a stale
final_status recorded before the cancel — cosmetic.
