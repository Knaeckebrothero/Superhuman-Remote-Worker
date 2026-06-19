# No lightweight delegation mode — every delegated subagent is a full worker job

**Date:** 2026-06-18
**Status:** Open. Enhancement / design gap, **not** a regression — the existing
heavyweight delegation path works as designed. This documents a missing
*alternative* mode.
**Component:** `src/tools/delegation/delegate_work.py`, `src/graph.py` (worker
state machine), `src/api/orchestrator_client.py` (`create_delegation_job`),
`docs/features/subagent_delegation.md` (design doc that chose the heavyweight
model).

## Summary

`delegate_work` has exactly one mode: it spawns each subagent as a **full worker
job**. There is no "light" variant — no agent-as-tool call that runs a bounded
ReAct loop and returns a string result without the full job lifecycle.

Every delegated child today:

- is created as a real job via `create_delegation_job` → `POST /api/jobs`
  (`orchestrator_client.py:934`);
- is picked up by the auto-assign dispatcher and POSTed to an agent pod (subject
  to the 30 s agent cooldown), with its own git **worktree** branched off the
  parent's workspace snapshot;
- runs the complete `UniversalAgent` over `src/graph.py` — the 10-node state
  machine with full **strategic ↔ tactical phase alternation**, todo lifecycle,
  checkpointing, context compaction, and archiver;
- forces the parent to **freeze** (`freeze_type: "delegation"`,
  `delegate_work.py:370`) and be **re-dispatched** (checkpoint + wake) once all
  children reach a terminal state (`orchestrator/main.py:8011`), then **merge**
  results back via squash-merge.

That is the right design for substantial, multi-disciplinary decomposition. It
is disproportionate for small, well-scoped, often read-only subtasks — "summarize
these three files," "extract the public API surface," "verify this one claim,"
"classify these N documents." For those, standing up a job + pod dispatch +
worktree + a strategic planning phase + suspend/resume round-trip is pure
overhead.

## What exists today (so this isn't mistaken for a bug)

- **Single tool, single mode.** The delegation toolkit exposes only
  `delegate_work` and `resume_delegation_child` (`delegate_work.py:25-51`). No
  light/`as_tool` variant, no `mode` parameter.
- **Children are full jobs.** `delegate_work.py:328` calls
  `create_delegation_job(...)`; the child is an ordinary row with `parent_job_id`
  + `creation_order` set. `creation_order` is only a lineage marker for
  depth-counting and sibling-completion detection — it does **not** select a
  lighter execution path.
- **The full graph runs.** Any job runs `src/graph.py`. There is no
  delegation-specific reduced graph; a child executes the same
  `init_workspace → execute ↔ tools → check_todos → archive_phase →
  handle_transition → check_goal` loop as a top-level job, including at least one
  strategic planning phase.
- **The heavyweight cost was a deliberate design choice.**
  `docs/features/subagent_delegation.md` decisions #1 (synchronous only), #3
  (checkpoint + wake over long-polling), and #5 (git worktree isolation) commit
  every child to the branch → suspend → merge model. A lighter mode was never in
  scope for v1.

## Why a light mode is missing / why it matters

1. **Overhead per child.** Job-row creation, dispatcher poll + 30 s cooldown,
   worktree provisioning, full graph init, a strategic phase, checkpointing, and
   the merge path — all incurred even for a trivial subtask. The design doc's own
   survey cites ~50 K-token overhead per Claude Code subprocess spawn as a known
   cost of the heavyweight pattern.
2. **Latency.** The parent cannot get a quick answer: it must freeze, the
   orchestrator must re-dispatch it (suspend → wake) after children finish.
   There is no synchronous "ask a subagent, get a string back, keep going."
3. **Worktree + merge is wasted on read-only work.** A subagent that only reads
   and summarizes produces no workspace changes worth merging, yet still pays for
   a branch, worktree, and squash-merge.
4. **No fan-out over many small items.** Processing N homogeneous items (e.g. N
   documents) currently means N full jobs, which is impractical past a handful —
   the very `Send()` intra-phase case the design doc explicitly parked as a
   "future option" (`subagent_delegation.md`, design decision #8).
5. **Industry has the lighter pattern; we only built the heavy one.** The design
   doc surveyed agent-as-tool delegation — Claude Code's Agent tool (fresh
   context, task down / result up, optional worktree, background mode), OpenAI's
   `Agent.as_tool()` (sub-agent runs as a tool call, returns a string, parent
   retains control, no shared history), Google ADK's `AgentTool`, Kimi K2.5's
   context-sharding (only distilled outputs propagate back). We adopted the
   branch-and-merge model and left the agent-as-tool model unbuilt.

## What "delegation light mode" would mean

A second delegation primitive — either a new tool (`delegate_light`) or a `mode`
parameter on `delegate_work` — with roughly these properties:

- **Bounded ReAct, no phase machinery.** The light subagent runs an
  `execute ↔ tools` loop to completion (capped iterations/tokens) without the
  strategic/tactical alternation, todo lifecycle, or archive/transition nodes.
- **Result-as-string, no merge.** It returns its final text directly into the
  parent's conversation as the tool result (task down, result up), like
  `Agent.as_tool()`. No `delegation_results` injection, no worktree, no
  squash-merge.
- **Synchronous, no parent suspend.** The parent does not freeze/re-dispatch; it
  blocks on the light call and continues. (For long or many calls, a bounded
  concurrent fan-out rather than checkpoint + wake.)
- **Fresh context, scoped tools.** Same LLM-level isolation as today (only the
  task + shared context passed down), but a read-leaning default toolset since
  there is nothing to merge.

This sits *alongside* the existing heavyweight `delegate_work`, which remains the
right tool for substantial, workspace-mutating, multi-phase decomposition.

## Options / possible approaches

Ordered roughly by increasing implementation cost.

1. **`Send()`-based intra-phase fan-out (in-process).** Use LangGraph `Send()` to
   run parallel bounded sub-tasks *within the parent's own graph/process* — no
   child jobs, no orchestrator round-trip. Best fit for homogeneous fan-out
   (process N items). This is the doc's own deferred "future option" and is the
   lightest possible mode.
2. **Agent-as-tool (`delegate_light`).** A new tool that instantiates a minimal
   subagent (fresh context, capped ReAct loop, read-leaning toolset) in-process,
   runs it synchronously, and returns its string result. Mirrors OpenAI
   `Agent.as_tool()` / Claude Code's foreground Agent tool. Parent never
   suspends. Most general; needs a slimmed execution harness distinct from
   `graph.py`.
3. **Read-only / no-merge job variant.** Still a real job (keeps the dispatch +
   isolation machinery) but skips worktree + merge for declared read-only
   children. Lighter than full delegation, heavier than in-process; smallest
   change to the existing plumbing but keeps the dispatch/suspend latency.

Recommended next step: decide the **driving use case** first (quick scoped
lookup vs. parallel fan-out over many items), because it picks the approach —
fan-out points at (1), "ask one focused subagent and continue" points at (2),
and "isolated but cheap" points at (3).

## Scope / open questions

- **Where does a light subagent run** — truly in the parent's process/event loop
  (lightest, but shares resources and needs careful context isolation) or a
  short-lived ephemeral job?
- **Tool surface** for light mode — read-only by default? Allow opt-in writes
  even though there is no merge?
- **Limits** — iteration/token caps, concurrency cap (the heavy path caps at 5
  children; a light path may want a different bound), and whether light calls
  count toward `delegation.max_depth`.
- **Failure handling** — a failed light call presumably just returns an error
  string to the parent (no checkpoint/resume, no `resume_delegation_child`
  equivalent). Confirm that is acceptable.
- **Interaction with the model-selection work** — a per-delegation model setting
  (under discussion 2026-06-18) applies cleanly to either mode; a light mode
  would likely want its own (cheaper) default model.
- **Observability** — light subagents that are not jobs won't show up in the
  jobs UI / audit the way children do today; decide how their traces surface.

## References

- **Design doc (heavyweight model + the industry survey this gap draws on):**
  `docs/features/subagent_delegation.md` — esp. the "Cross-Provider Patterns"
  table and design decisions #1, #3, #5, #8.
- **Tool:** `src/tools/delegation/delegate_work.py` (single mode; child
  `config_override` at `:323`, freeze at `:370`).
- **Child creation:** `src/api/orchestrator_client.py:934`
  (`create_delegation_job` → `POST /api/jobs`).
- **Worker graph the child runs:** `src/graph.py`; depth/lineage typing in
  `src/core/loader.py:1516` (`DelegationConfig`).
- **Parent re-dispatch on children-complete:** `orchestrator/main.py:8011`
  (`_handle_delegation_child_completion`); freeze→`waiting` in
  `orchestrator/services/completion.py:321`.
- **Related:** `docs/issues/scholar_delegation_not_exercised.md` (whether the
  model *chooses* to delegate — orthogonal),
  `docs/issues/subjob_branch_merge_model.md`,
  `docs/issues/subjob_worktree_sharing.md`,
  `docs/done/subjob_merge_clobbers_parent_deliverables.md`. Naming precedent for
  "light": `docs/features/memory_light.md`.
