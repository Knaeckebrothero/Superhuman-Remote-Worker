---
tags:
  - issue
  - jobs
  - agent
  - verification
  - phases
---

# Issue — feedback-resume correction session runs with the closure/verdict toolset and cannot edit anything

**Status:** Observed 2026-07-26 on dev (job `52949749`, round-2 correction).
Not yet root-caused. Work on `develop`.

**One line:** a worker resumed to remediate critic feedback came up in its
*closure* phase with the restricted closure/verdict toolset — no `edit_file`,
`write_file`, `browser_*`, or research tools — so the correction session could
plan corrections but not perform a single one, burning a full verification
round.

## Observed behavior (evidence)

Job `52949749`, round-2 FEEDBACK_RESUME (2026-07-25/26). From the job's own
freeze notes (frozen 2026-07-26T00:19:48, confidence 0.45):

> "Phase 19 Tactical NOT executed (toolset RESTRICTED to closure/verdict
> tools; full toolset required for corrective phase). Worker correction
> session needed for round-3 with full toolset (browser_navigate, edit_file,
> read_file, write_file, next_phase_todos, kb_write, list_files, file_exists,
> search_files, kb_search, kb_update, kb_read, kb_list)."

The session behaved impeccably *around* the limitation: Phase 18 Strategic
completed 4/4 (process feedback / evaluate / adapt plan / create corrective
todos), staged the corrective phases (C1/C2/C3 + H1–H3 work, todo lists for
three phases), wrote the remediation roadmap to the KB
(`med-v2-feedback-summary-for-feedbackresume-round-2-2026-07-26`), recovered
from the mismatch via `todo_rewind` + `mark_complete`, and froze at
confidence 0.45 explicitly requesting a full-toolset round-3. The deliverable
was untouched — round 3's critic re-reviewed byte-identical content and
necessarily returned it again.

**Cost:** one full verification round (of `max_rounds: 5`) + a critic run,
for zero content change.

## Hypothesis (to verify before fixing)

The job froze for review out of its final/closure phase (phase-19-tactical in
the plan's numbering). The feedback-resume path re-enters the graph at the
checkpointed phase — i.e. the *closure* phase — and phase-scoped tool binding
(`filter_tools_by_phase` / the phase-template tool lists) legitimately
restricts closure phases to verdict/wrap-up tools. Nothing re-opens a
tactical work phase on a RETURNED verdict, so the corrective todos the
strategic phase stages have no phase with an editing toolset to execute in.

Supporting detail: the round-1 correction (2026-07-25, after the workspace
rebuild) did *not* hit this — that session re-initialized its phase state
from scratch (blank workspace, "Loaded 3 predefined strategic todos") rather
than resuming into a late closure phase. The bug likely needs the
resume-into-late-phase shape to manifest.

## Investigation pointers

- Where the feedback-resume decides the re-entry phase: the verdict-resume
  arm (`queue_job_for_resume` callers) and the graph's
  `route_entry`/`restore_todo_state` path on resume.
- Phase→toolset binding: `src/tools/registry.py` (`filter_tools_by_phase`),
  phase templates under `config/templates/` (the closure/verdict phase's tool
  list), and how `next_phase_todos` transitions choose the next phase's
  toolset.
- The job's own archives are a ready-made repro record:
  `archive/todos_phase_18_strategic_20260726_001406.md` and
  `archive/todos_phase_19_tactical_20260726_001626.md` (the toolset-mismatch
  failure note) on the job workspace/repo.

## Fix direction (sketch, pending root-cause)

On a RETURNED verdict, the resume should land the worker in a phase whose
toolset can actually remediate: either (a) re-open a tactical work phase
(fresh phase with the full worker toolset, seeded with the staged corrective
todos), or (b) make the feedback-resume path force the standard tactical
toolset regardless of the checkpointed phase. (a) fits the existing
phase-alternation model best — the strategic "process feedback" phase already
stages corrective todos; they just need a real phase to run in.

## Related

- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — parent
  incident chain (this bug consumed verification round 2→3 of job `52949749`).
- `docs/issues/recovery_pause_repersists_stale_freeze_invisible_job.md` — the
  wedge that then stalled the round-3 dispatch.
