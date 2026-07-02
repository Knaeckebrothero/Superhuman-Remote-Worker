# No lightweight delegation mode — every delegated subagent is a full worker job

**Date:** 2026-06-18
**Status:** Open. Enhancement / design gap, **not** a regression — the existing
heavyweight delegation path works as designed. This documents a missing
*alternative* mode. **Decision proposed 2026-07-02** (scholar-driven) — see
"Decision — Scholar-driven throwaway readers" at the end of this doc. Citation
handling resolved (subagents inherit the citation tool and cite normally).
Remaining fork: runtime (in-process vs stripped child job) — recommendation
recorded, awaiting user confirmation.
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

---

## Decision — Scholar-driven throwaway readers (proposed 2026-07-02)

**Driving use case (settles the mechanism).** The scholar expert needs cheap,
disposable subagents that "read a bunch of sources and return a result" — N
parallel readers, each fetching/reading a subset of sources, distilling, and
handing back a string the scholar synthesizes. This is the
**parallel-read-and-distill fan-out**, not an "ask one focused subagent" call
and not workspace-mutating decomposition. It points squarely at **approach #2
(agent-as-tool), run as a bounded concurrent fan-out.**

**What "light" actually removes — the merge, not the worktree.** The expensive,
slow part of `delegate_work` for this use case is **review + squash-merge + the
parent's freeze → checkpoint → wake round-trip**, not the worktree. Keep the
worktrees: they are cheap (shared git object store) and they isolate parallel
readers' scratch files so N concurrent readers don't collide. But the readers
are **throwaway — they return a result string and their worktree is discarded,
never merged.** Citations don't need the merge either: subagents inherit
`cite_web`/`cite_document` and write to the shared **CitationEngine DB
(job-scoped, verified, persistent)** (`src/tools/citation/sources.py:334`), with
web content archived by source-id. So the whole review/merge apparatus is pure
tax; the worktree stays purely as lightweight isolation.

### Chosen shape

- **New tool `delegate_light`** in `src/tools/delegation/` — same `delegation`
  registry category; `delegate_work` stays untouched. Opt-in per expert via the
  existing `tools.delegation` YAML list (which is *already* a per-agent toggle).
  A cockpit "delegation mode: off / light / heavy / both" switch over that same
  list is a **later** pass, not v1. Heavy and light are **not** mutually
  exclusive at the model level — an expert can hold both and the model picks;
  the tool descriptions steer it.
- **Async tool, fan-out native.** Signature roughly
  `delegate_light(tasks: list[str], context: str = "") -> str` (a single task is
  just a length-1 fan-out). Make it a **coroutine tool** so the sub-loops
  `await` the LLM in the parent's own event loop and run concurrently via
  `asyncio.gather` — *avoid* `delegate_work`'s `ThreadPoolExecutor` +
  `asyncio.run` nesting hack (`delegate_work.py:255`).
- **Bounded ReAct, no graph.** Each reader is a capped `execute ↔ tools` loop —
  **not** a second `graph.py` instance. No strategic/tactical alternation, no
  todos, no archiver, no checkpoint. Caps: `max_iterations`, `max_tokens`.
- **Throwaway worktree per reader.** Each reader gets its own git worktree
  (`.worktrees/light_N`) via the existing `GitManager` worktree methods — cheap
  isolation + scratch space so parallel readers don't collide. The worktree is
  **discarded on return, never merged.**
- **Inherit the full parent tool set (minus delegation).** The reader's tools are
  the parent's — citation, web, read, **and shell** — rebound to its worktree via
  the existing tool factory. Only `delegate_work`/`delegate_light` are excluded
  (no grandchildren). Shell stays: most reader shell use is ephemeral CLI (`curl`
  an API, query a DB, `grep`/`jq`) that binds no ports, and dropping it would gut
  capability. Each reader gets its own tmux session (keyed to its worktree) + the
  existing port-range awareness block (`_build_subagent_env_block`,
  `delegate_work.py:54`), so the rare server-start case is steered by prompt, not
  policed. Teardown kills the session (reaping any lingering process) and discards
  the worktree.
- **Fresh context.** New message list (scoped preamble + shared `context` +
  task); the parent's history is **not** passed down — same LLM-level isolation
  the heavy path gets from a fresh pod.
- **Return-as-string, parent stays author.** Readers hand back distilled
  findings inline; the **scholar** writes the `output/ideas/NNN.md` artifacts and
  synthesis. This is the flow shift from today's "readers write files → parent
  merges" to "readers return → parent authors."
- **No suspend.** The tool blocks-and-returns like any other tool call — no
  `request_freeze()`, no `waiting` status, no orchestrator round-trip.
- **Smaller model as a configurable subagent default.** A cheaper model for
  throwaway readers, set the way `max_depth` is today — a `delegation.*` knob
  (see config below), falling back to a tactical default if unset.

Config (layered under the existing `delegation:` block, nothing else moves):

```yaml
delegation:
  enabled: true
  max_depth: 1
  # ... existing heavy-path knobs ...
  light:
    enabled: true
    model: null              # cheaper tactical fallback if unset
    max_iterations: 10
    max_tokens: 40000
    max_parallel: 3
    allow_writes: false
tools:
  delegation:
    - delegate_work          # heavy, unchanged
    - delegate_light         # NEW
    - resume_delegation_child
```

### RESOLVED — citations just work

Subagents inherit the parent's `cite_web`/`cite_document` and cite normally into
the shared job-scoped CitationEngine DB as they read. No special contract, no
provenance round-trip. Per-reader worktrees remove the earlier concurrency worry
about scratch-file collisions; the citation DB handles concurrent inserts.

### OPEN FORK — runtime (recommendation recorded, awaiting confirmation)

Where do the throwaway readers run? Both keep worktrees + inherited tools + the
small model; they differ only in execution substrate.

- **(A) In-process on the parent's pod** *(recommended)* — the parent spins up N
  bounded ReAct loops, each with a worktree + tool set rebound to it, `gather`s
  them, collects the result strings. No dispatch, no 30 s cooldown, no
  freeze/wake — genuinely light. Reuses the tool factory + `GitManager` worktree
  methods; the parallel mini-agent loop is the new code. Each reader gets its own
  tmux session (the heavy path already runs per-child tmux, so this is proven),
  so inheriting shell is not a blocker.
- **(B) Stripped-down child jobs** — reuse `delegate_work`'s worktree +
  child-job creation, but the child runs a reduced ReAct graph on the small
  model and its result is auto-collected without review/merge. Reuses the most
  existing code, but **re-inherits orchestrator dispatch + 30 s cooldown +
  freeze/wake latency** — the very overhead this feature exists to escape. So
  "lighter," not "light."

Recommendation: **(A)**. (B)'s reuse advantage is undercut by dragging back the
latency that motivated a light mode at all.

### Cross-cutting requirements (independent of the fork)

1. **No nesting.** Light readers must not receive `delegate_work` *or*
   `delegate_light` — exclude both from the sub-loop tool set (grandchildren are
   out of scope, same as heavy `max_depth: 1`).
2. **Metering / observability — a v1 requirement, not a follow-up.** A heavy
   child is a job row: it shows in the jobs UI and its tokens land in
   `usage_events`. A light reader is invisible by those means. Its LLM calls
   **must** route through the same audit + usage-metering path and fold into the
   parent job's usage, or it becomes an unmetered spend leak — exactly the class
   of gap flagged around gateway metering
   ([[reference_usage_view_gateway_metering_routing]]). Given the rate-limiting
   v2 work in flight, wire this from the start.
3. **Concurrency.** Per-reader worktrees isolate scratch-file writes, so parallel
   readers don't collide on the filesystem, and the CitationEngine DB handles
   concurrent inserts. Remaining check (if runtime (A)): confirm the parent's
   event loop cleanly runs `max_parallel` reader loops + their tool calls under
   one pod; bound it with `max_parallel` + the token cap.

### Scholar prompt / config changes this entails

- `config/experts/scholar/config.yaml`: add `delegate_light` to
  `tools.delegation`.
- `config/experts/scholar/strategic.txt:30-60` and `todo_guide.md` "Delegated
  Parallel Research Phase": add a light-mode variant — "delegate the *reading*,
  you do the *writing/synthesis*" — distinct from the existing heavy
  "readers write files → you merge" guidance. Gate on `has_tool("delegate_light")`.

### Build outline (files touched)

- **New:** `src/tools/delegation/delegate_light.py` (bounded async ReAct
  harness + tool factory), metadata entry in
  `DELEGATION_TOOLS_METADATA`.
- **Modify:** `src/tools/registry.py` (register — the `delegation` category
  loader at :544 already iterates the factory's tools, so mainly the metadata),
  `config/defaults.yaml` (add `delegation.light` block + schema entry in
  `config/schema.json`), `config/experts/scholar/{config.yaml,strategic.txt,
  todo_guide.md}`, and the metering hook (route sub-loop LLM calls through the
  existing audit/usage path).
- **Reuses (no change):** the parent's bound LLM + `ToolContext` with live tools;
  CitationEngine + `cite_web` (already async, DB-backed, job-scoped).
