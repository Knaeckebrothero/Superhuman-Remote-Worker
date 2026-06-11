# Scholar expert never delegates to subagents — model runs everything sequentially

**Date:** 2026-06-03
**Status:** Open. Behavioral issue (model instruction-following), **not** a config regression. The delegation capability is fully wired and was live for the affected job; the model declined to use it.
**Component:** `config/experts/scholar/` prompts (`strategic.txt`, `todo_guide.md`, `strategic_todos_initial.yaml`). Plumbing (`delegate_work` tool, `resume_delegation_child`, orchestrator graft path) is intact and out of scope.
**Affected model:** `gpt-5.5` (observed). Not yet checked on other families.

## Summary

The scholar expert is configured to spawn subagents for parallel research via
`delegate_work` (added in commit `66f1fd56`, "Enable task delegation and
parallelization for reviews and research"). On job
`111be2e8-5e86-4f3d-aaa8-df84070000cd` ("Research 01 (GPT-5.5, v3)") the agent
did **all** research itself, sequentially, never once calling `delegate_work`
across 118 LLM requests — even though the work decomposed cleanly into 3
independent topics (a textbook delegation case per the scholar's own guidance).

Investigation confirms this is **not** a tool-availability or config-resolution
bug: the `delegate_work` tool was in the resolved tool set offered to the model,
and the delegation guidance block in `strategic.txt` rendered into the prompt
(its `{% if has_tool("delegate_work") %}` gate was satisfied). The model simply
chose direct sequential execution over delegation.

The delegation guidance is **permissive, not directive** ("*When* the remaining
work has 2+ separable threads, use `delegate_work`"), and the project-wide
default bias is direct execution. Given an optional path, GPT-5.5 took the path
of least resistance. The fix, if we want reliable delegation, is in the prompt
wording — make delegation the default for multi-topic research instead of an
option — not in the plumbing.

## What works (so this isn't mistaken for a regression)

- **Toolset is granted.** `config/experts/scholar/config.yaml:81-83`:
  ```yaml
  delegation:
    - delegate_work
    - resume_delegation_child
  ```
- **Guidance is written into the prompts.** `strategic.txt:30-60` ("Parallel
  research via delegation"), `todo_guide.md:118-147` (delegation-phase todo
  structure: "1-2 delegation + 3-4 synthesis"), and the MiniMax variant
  `strategic_minimax.txt:41-68`.
- **The tool reached this model.** The new `config/experts/scholar/model_config_matrix.yaml`
  only swaps *prompts* for the `minimax` / `minimax-m3` families; `gpt-5.5`
  falls through to `default`, which uses the delegation-bearing `strategic.txt`.
  No tool-stripping occurs anywhere in the resolution chain.

## Evidence

Affected job at time of investigation (2026-06-03): status `processing`,
`get_job_progress` reported **0.0%** after **22m+** elapsed, 577 audit entries,
118 LLM requests — all `gpt-5.5`.

### 1. The tool was offered to the model at the planning decision point

LLM request at iteration 18 (doc_id `6a1f4f66a957ae8fe08e8ded`) — the exact
call where the agent invoked `next_phase_todos` to plan its tactical work —
listed 45 tool definitions, ending in:

```
=== Tool Definitions (45) ===
... kb_export, delegate_work, resume_delegation_child
```

Because `delegate_work` is present in the resolved tool set, the Jinja gate
`{% if has_tool("delegate_work") %}` in `strategic.txt:30` evaluated **true**,
so the "Parallel research via delegation" block rendered into the system prompt
the model received (prompt was 44,860 tokens). The model had both the tool and
the instructions in front of it.

### 2. Delegation was never invoked — 0 of 118 requests

Reconstructed execution trace (`list_llm_requests`):

| Iters | Phase | Activity |
|-------|-------|----------|
| 0–17 | strategic | coverage review — `kb_search`/`kb_read`/`kb_update`/`kb_list` of prior project knowledge |
| 18 | strategic | `next_phase_todos` — **planning decision: chose sequential exploration, not a delegation phase** |
| 19–59 | tactical | three research ideas done **one after another**: 001 OpenAI memory (iters 22–34), 002 Anthropic (35–45), 003 Google/MS (46–59) — each a `web_search → extract_webpage → cite_web → write_file → todo_complete` cycle |
| 60–117 | tactical | continued solo: `web_search`, `crawl_website`, `map_website`, `browser_navigate`, `run_command`, repeated `read_file`/`search_files` |

`delegate_work` / `resume_delegation_child` appear **zero** times in the entire
run. The three ideas (OpenAI vs Anthropic vs Google/MS memory systems) are
precisely the "comparison research where each option can be explored
independently → 2-3 subagents" case described in `strategic.txt:46-48`.

### 3. The decision was made at planning time

At iter 18 the agent created ordinary sequential exploration todos rather than
the delegation-phase structure described in `todo_guide.md:118-147` (which calls
for writing "1-2 delegation todos FIRST, synthesis todos AFTER"). Once it
planned a sequential phase, the rest of the run followed that plan.

## Root cause

**The delegation guidance is optional, and an optional path loses to the
default.** Two reinforcing factors:

1. **Permissive wording.** `strategic.txt:31` frames delegation as a conditional
   option ("*When* the remaining work has 2+ clearly separable research threads,
   use `delegate_work`…") presented side-by-side with "do it yourself"
   (`strategic.txt:34-36`). Nothing requires a delegation-first plan even when
   the task obviously qualifies.

2. **Project-wide direct-execution bias.** The explicit default across experts
   is direct implementation — see commit `59cb3d44` ("Clarify delegation usage
   and transition to execution-focused guidance"), which hard-coded "default
   direct implementation due to coordination and merge costs" for the developer
   expert. Scholar's guidance is more pro-delegation than developer's, but it's
   still framed as the exception.

Given an *optional* lever plus a default that says "just do it yourself,"
`gpt-5.5` did it itself. This is consistent instruction-following of a
permissive prompt, not a bug.

## Secondary observation — the solo run appears to be drifting

Separately worth flagging: after iter 59 the agent stopped calling
`todo_complete` entirely, yet kept issuing tool calls for ~58 more iterations
(60–117) — `crawl_website`, `map_website`, `browser_navigate`, `run_command`,
and repeated `read_file`/`search_files` poking around the workspace/repo — with
no phase transition (`next_phase_todos` was called only once, at iter 18) and
`get_job_progress` still at 0%. So the missing delegation compounds into a
single ~98-iteration tactical phase with no tracked progress. Whether this is
genuine deep research or a stuck/wandering loop is unconfirmed, but the shape
(no todo completions, no phase advance, broadening tool flailing) matches drift.
If this issue is picked up, check whether the absence of delegation correlates
with these over-long unstructured tactical phases.

## Options / recommended fixes

Ordered by increasing force. Fix is in the prompts under
`config/experts/scholar/`; plumbing needs no change.

1. **Make `todo_guide.md` / `strategic_todos_initial.yaml` delegation-first for
   multi-topic research.** Add an initial-phase directive: *"If the task spans
   2+ independent topics/options, your first tactical phase MUST be a delegation
   phase (1–2 `delegate_work` todos) — not sequential exploration. Sequential
   self-exploration is only for a single narrow thread."* This converts the
   default at the point where the plan is actually chosen (iter 18 equivalent).

2. **Strengthen `strategic.txt:30-60`** from option to default: change "*When*
   the work has 2+ separable threads, use `delegate_work`" to "If you identify
   2+ separable threads, **delegate them by default** — sequential
   self-execution is the exception, reserved for ≤1 narrow thread or work where
   each step needs the previous step's results." Mirror the same edit in
   `strategic_minimax.txt:41-68`.

3. **Treat it as model-dependent and don't over-engineer the prompt.** This was
   observed on `gpt-5.5` specifically (the user's "v3" experiment). Before
   rewording, run the same task on a model known to follow tool-use guidance
   well (e.g. a Kimi/MiniMax scholar run) to isolate *model behavior* from
   *prompt wording*. If other models delegate on the identical prompt, the lever
   is model selection, not the prompt.

Recommended: do (3) first as a cheap diagnostic, then (1) — making the planning
step delegation-first is the highest-leverage change because the decision is
demonstrably made at `next_phase_todos`, and it doesn't force delegation on
genuinely sequential single-topic tasks.

## How to verify a fix

- **Re-run the same task** on `gpt-5.5` after the prompt change and confirm
  `delegate_work` appears in `list_llm_requests` for the job (it should show up
  in the strategic→tactical planning iteration and spawn child jobs).
- **Cross-model control:** run the unchanged task on a different family; compare
  whether delegation fires. Isolates wording vs. model.
- **Check the trace shape:** a healthy delegated research phase shows a
  `delegate_work` call, a gap while children run, then `resume_delegation_child`
  + synthesis (`kb_write`, `write_file`) rather than 98 sequential tool calls in
  one tactical phase.

## References

- **Job:** `111be2e8-5e86-4f3d-aaa8-df84070000cd` — "Research 01 (GPT-5.5, v3)",
  created 2026-06-02T21:42:57Z, model `gpt-5.5`.
- **Decisive LLM request:** doc_id `6a1f4f66a957ae8fe08e8ded` (iter 18,
  `next_phase_todos`) — shows all 45 tools incl. `delegate_work`.
- **Config:** `config/experts/scholar/config.yaml:81-83` (tools),
  `strategic.txt:30-60`, `todo_guide.md:118-147`, `strategic_minimax.txt:41-68`
  (guidance), `model_config_matrix.yaml` (prompt resolution, no tool-stripping).
- **Commits:** `66f1fd56` (added scholar/critic delegation, 2026-03-23);
  `59cb3d44` (developer direct-execution-default bias, 2026-05-13).
- **Related:** `docs/issues/subjob_branch_merge_model.md`,
  `docs/done/subjob_merge_clobbers_parent_deliverables.md`,
  `docs/features/subagent_delegation.md` (delegation plumbing — all confirmed
  working here; this issue is purely about whether the model *chooses* to
  delegate).
