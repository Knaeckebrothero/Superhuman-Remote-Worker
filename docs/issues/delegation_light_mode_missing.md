# No lightweight delegation mode — every delegated subagent is a full worker job

**Date:** 2026-06-18
**Status:** Open. Enhancement / design gap, **not** a regression — the existing
heavyweight delegation path works as designed. This documents a missing
*alternative* mode.
**Update 2026-07-02:** a light ReAct subagent tool (approach ~2, agent-as-tool with mid-tier child models) is **in progress** in a parallel session.**Decision proposed 2026-07-02** (scholar-driven) — see
"Decision — Scholar-driven throwaway readers" at the end of this doc. Citation
handling resolved. Interface design revised 2026-07-02 against external
cross-model research — **flipped from batch to iterative parallel tool calls**.
Both forks resolved 2026-07-02: **runtime = in-process** (control loop on the
agent pod; all fs/git/shell on the workspace pod via the existing remote backend)
and **one agent-facing tool name** with the heavy/light backend selected by
config. Final name **`spawn_subagent`** (web research 2026-07-02, convergent
3-agent sweep). Consequence: the heavy `delegate_work` batch signature converges
onto the shared iterative schema. **Sequencing: light-first** — v1 ships light
mode for scholar under the shared name; heavy-backend convergence is a marked
fast-follow.
Note before calling this done: the heavy path has **never been invoked in production — 0 delegation children all-time** — so shipping the tool is only half the work; the adoption side (prompt/todo-scaffold wiring so models actually use it) is tracked in `subagents_never_used.md`.
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

- **One agent-facing tool, config-selected backend.** The model sees a *single*
  delegation tool with a *stable name* (**decided: `spawn_subagent`** — singular,
  one call spawns one subagent; the model fans out by calling it N times; see
  "Naming — DECIDED" below for the evidence). Whether a call resolves to a **light** in-process ReAct reader or a
  **heavy** full child job is chosen by **config, not by the agent** — a
  `delegation.mode` knob (default + per-expert override, like `max_depth`). The
  agent is mode-agnostic: same name, same flat schema, same prompt guidance
  whether it evokes 5 jobs or 5 react loops. Internally / in the cockpit the two
  backends may have distinct names; the *model-facing* name never changes.
  **Consequence:** one name ⇒ one schema, so the heavy backend converges onto the
  iterative single-task schema — its current `tasks: list[dict]` batch shape is
  retired *behind* this name (the heavy execution path stays; only its call
  signature changes).
- **Iterative parallel invocation, not batch** *(revised 2026-07-02 per external
  research — see reconciliation below).* The tool takes **one** subtask per call
  and returns one string; the model fans out by emitting **multiple
  `delegate_subtask` calls in a single turn**. We do **not** take a
  `tasks: list[...]`. Reason: our own fleet (MiniMax M3, GLM-5.2, Qwen) is
  exactly the set that mis-formats nested list-of-dict args; a flat scalar schema
  is near-universally valid, isolates per-call failures, and maps each result to
  its `tool_call_id`. **Verified this is nearly free for us:** the graph's tool
  node is LangGraph's prebuilt `ToolNode` (`graph.py:3770`), whose `_afunc`
  already runs a turn's calls concurrently via `asyncio.gather`
  (`langgraph/prebuilt/tool_node.py:845`) — so N parallel `delegate_subtask`
  calls execute concurrently with **no fan-out code of our own**. Still a
  **coroutine tool** (avoid `delegate_work`'s `ThreadPoolExecutor`+`asyncio.run`
  hack at `delegate_work.py:255`).
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
- **Smaller model as a first-class `subagent` model tier (DECIDED 2026-07-02).**
  The reader model is **not** a `delegation.*` string — it's a fourth phase-model
  tier `llm.subagent`, exactly parallel to the existing `llm.strategic` /
  `llm.tactical` / `llm.summarization` overrides (`LLMConfig`, `loader.py:1300`;
  resolved by `get_phase_config("subagent")`). This is what the user asked for:
  "so we have tactical, strategic **and subagent** model." Consequences that fall
  out for free:
  - **Full `PhaseLLMOverride` semantics**, not just a model name — provider,
    `base_url`, `api_key`, `reasoning_level`, context window, `multimodal`.
    Essential because a Sonnet/Haiku reader may live on a different
    endpoint/key/window than a Gemma base (a bare model string couldn't express
    that). Set only `model:` and the rest inherits base.
  - **Per-job override with zero new plumbing.** `config_override.llm.subagent`
    deep-merges at dispatch the same way `llm.strategic`/`llm.tactical` overrides
    already do — so "top-tier parent spawns mid-tier reader" is settable
    globally (defaults), per-expert, *or* per-job/per-loop.
  - **Kept out of the main graph.** `has_phase_overrides()` (`loader.py:1362`)
    intentionally does **not** include `subagent` — the reader LLM is built
    lazily by the light backend, not by the main graph's `_initialize_llms`, so a
    subagent-only config never forces strategic/tactical LLM splitting on the
    parent.
  - **Fallback chain:** `llm.subagent` → (if unset) `llm.tactical` → base. The
    light backend picks `get_phase_config("subagent")` when `llm.subagent` is
    set, else `get_phase_config("tactical")` (cheaper than strategic).

Config (light-runtime knobs stay under `delegation.light`; the **model** lives in
the `llm.subagent` tier):

```yaml
llm:
  model: claude-opus-4-8       # base / strategic-ish
  tactical:
    model: claude-sonnet-5
  subagent:                    # throwaway light readers (opus → haiku)
    model: claude-haiku-4-5-20251001
delegation:
  enabled: true
  max_depth: 1
  # ... existing heavy-path knobs ...
  mode: light                  # heavy (default) | light — operator-selected backend
  light:                       # runtime knobs only; model is llm.subagent
    enabled: true
    max_iterations: 10
    max_tokens: 40000
    max_parallel: 3
    allow_writes: false
tools:
  delegation:
    - delegate_work            # heavy, unchanged
    - spawn_subagent           # NEW — backend chosen by delegation.mode
    - resume_delegation_child
```

### External research reconciliation (2026-07-02)

An external deep-research report on cross-model delegation-tool design
(`Subagent Delegation Interface Design.md`) was run against our fleet (MiniMax
M3, GLM-5.2, GPT-5.5, Opus, Gemini, Kimi, Qwen, DeepSeek). Adopted:

- **Invocation pattern → iterative, not batch** (flips the earlier
  `tasks: list[...]` shape — see the "Iterative parallel invocation" bullet
  above). The report's headline call, and it holds *especially* for us: the
  weak-JSON models it warns about (MiniMax's XML→JSON regex bridge, Qwen arg
  truncation, GLM sequential-by-default) are our models, and — verified — our
  `ToolNode` already `asyncio.gather`s a turn's calls, so iterative is *less*
  code than batch, not more.
- **Flat single-task schema.** `task_description` (str, required — reader has no
  parent context, so it must be self-contained), + optional `role`/`config`
  (maps to our expert-preset concept = the report's `specialist_role`) and
  `expected_return_format` (natural-language shape of the returned string). Keep
  model, iteration caps, and timeouts **out** of the model-facing args — they're
  operator config knobs (`delegation.light.*`), matching both our earlier
  decision and the report.
- **Result packaging.** Return the reader's text verbatim (not JSON) as a normal
  ToolMessage on its `tool_call_id`, with a system-prepended header re-stating
  the task/role so the parent can attribute N parallel results when synthesizing.
- **Steering prompt (serial collapse + over-delegation).** The parent's
  system/expert prompt must (a) instruct the model to emit *multiple*
  `delegate_subtask` calls **in one turn** to parallelize — "parallel" alone is
  not enough, GLM especially defaults to sequential unless explicitly told — and
  (b) forbid delegating trivial work it can do itself. Concrete wording is in the
  report's "Standard System Prompt Injection".

New implementation caveat this introduces:

- **Concurrent worktree creation.** With iterative fan-out, N `delegate_subtask`
  calls run concurrently and each wants its own worktree, but `git worktree add`
  takes a repo lock. Serialize worktree creation behind an `asyncio.Lock` (create
  the N worktrees, then launch the readers) so concurrent adds don't contend.

Open decision this surfaces:

- **Naming — DECIDED `spawn_subagent` (web research, 2026-07-02).** Coexistence
  is resolved by one stable model-facing name with a config backend swap (see
  "One agent-facing tool" above), which dissolves the disambiguation problem the
  report couldn't see (it assumed a single tool). A 3-agent web sweep (literal
  harness tool names · cross-model naming best-practice · candidate ranking)
  converged on **`spawn_subagent`**:
  - *Cross-family-safe form:* verb-first `snake_case`, `[a-z0-9_]` only — no
    dots/dashes/spaces. The intersection that survives OpenAI/Anthropic's
    `^[a-zA-Z0-9_-]{1,64}$` (dots 400 out) *and* the delimiter-sensitive
    open-weight parsers (GLM XML-ish `<tool_call>`, MiniMax XML→JSON, Qwen
    Hermes); GLM's own `browser.search` dot-style is a portability trap.
  - *Native to training data:* "spawn"+"subagent" is Anthropic's own prose ("the
    lead agent spawns subagents") and Kimi's ("spin up sub-agents").
  - *Self-documenting iterative contract:* the singular concrete noun makes
    "5 calls ⇒ 5 subagents" unmistakable; a bare verb or work-unit noun doesn't.
  - *Avoids the collisions that matter:* `task`/`agent` are overloaded with prose
    + our first-class todos/agents (ToolScan, arXiv:2411.13547, links name/token
    collision to more wrong-tool + hallucinated-name picks); `dispatch_*`/
    `assign_*` echo this repo's orchestrator dispatch/assign verbs (leak toward
    heavyweight mode).
  The `delegate_` instinct placed second: "delegate" is CrewAI-flavored (hand to
  an *existing* coworker) and the OpenAI/LangGraph/AutoGen idiom is `transfer_to_*`
  / handoff — both fight our *fresh disposable child* semantics. `delegate_subtask`
  is the fallback if telemetry ever shows "spawn" misread as OS forking.
  **Migration:** existing `delegate_work` → `spawn_subagent` at heavy convergence;
  light ships under the new name first. One name ⇒ one schema, so the heavy batch
  signature converges onto the iterative single-task schema (not optional).

**Credibility caveat on the report:** its *directional* advice (flat > nested
schema, iterative parallel calls, string returns, keep infra params out, prompt
against serial collapse) is consistent with established tool-calling practice and
safe to adopt. Its *specific* claims — "DeepSeek V4 = 128 parallel calls," "Kimi
K2.6 = 300 subagents," several arXiv IDs, and a `NousResearch/hermes-agent` repo
— are unverified and some look synthetic; do **not** treat any specific
number/version as fact without checking. The architecture decision doesn't depend
on them.

### RESOLVED — citations just work

Subagents inherit the parent's `cite_web`/`cite_document` and cite normally into
the shared job-scoped CitationEngine DB as they read. No special contract, no
provenance round-trip. Per-reader worktrees remove the earlier concurrency worry
about scratch-file collisions; the citation DB handles concurrent inserts.

### RESOLVED — runtime: in-process, respecting pod separation (2026-07-02)

Light readers run **in-process on the agent pod**, not as child jobs. The parent
spins up N bounded ReAct loops, `gather`s them, collects the result strings — no
dispatch, no 30 s cooldown, no freeze/wake. (The rejected alternative, stripped
child jobs, would reuse more `delegate_work` plumbing but re-inherit exactly the
dispatch/freeze latency this feature exists to kill.)

**"In-process" is only the control loop.** LLM inference + web fetches run on the
agent pod's event loop; **all filesystem, git, and shell work stays on the
workspace pod** via the existing remote backend. This respects the agent-pod /
workspace-pod separation and reuses proven code — see cross-cutting requirements
3 and 4 below for the verified backend constraints (single locked SSH connection;
worktree creation over SSH; `run_in_executor` for blocking ops).

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
3. **Agent-pod / workspace-pod separation (verified — critical).** "In-process"
   means only the *control loop* (LLM inference + web fetches) runs on the agent
   pod. **All filesystem, git, and shell work stays on the workspace pod** via
   the existing remote backend — reuse the remote worktree path (`agent.py:1767`,
   `git worktree add` run over SSH) and bind each reader's tools to a
   `GitManager.from_worktree` (`git_manager.py:838`) / `worktree_add` (`:1057`)
   pointed at its worktree on the workspace pod. Never create worktrees or run
   shell on the agent pod.
4. **Concurrency: parallel for LLM/web, serialized for fs/shell (verified).** The
   parent's `RemoteBackend` (`src/core/backends/remote.py:87`) is a **single**
   SSH/SFTP connection guarded by a `threading` lock (`_sftp_lock:144`,
   `_sync_lock:156`). So N in-process readers parallelize on the expensive async
   work (model calls, web) but their SFTP/shell ops serialize behind that lock —
   fine for LLM/web-bound scholar readers, provided (a) blocking paramiko calls
   are offloaded via `run_in_executor` so one reader's fs op doesn't stall the
   whole event loop, and (b) the N `git worktree add`s are serialized behind an
   `asyncio.Lock` (they mutate one repo). If true parallel fs/shell is ever
   needed, give each reader its own `RemoteBackend` connection to the workspace
   pod — heavier, deferred. Bound fan-out with `max_parallel` + a token cap.

### Scholar prompt / config changes this entails

- `config/experts/scholar/config.yaml`: add `delegate_light` to
  `tools.delegation`.
- `config/experts/scholar/strategic.txt:30-60` and `todo_guide.md` "Delegated
  Parallel Research Phase": add a light-mode variant — "delegate the *reading*,
  you do the *writing/synthesis*" — distinct from the existing heavy
  "readers write files → you merge" guidance. It must teach **iterative fan-out**
  (emit one `delegate_subtask` call per source-cluster **in a single turn**), not
  the heavy tool's single-list-call pattern, and explicitly combat serial
  collapse (GLM in particular defaults to sequential). Gate on
  `has_tool("delegate_subtask")`.

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

---

## Implementation Plan — light-first (`spawn_subagent`)

Scope: ship the **light** backend for `spawn_subagent`, enabled for the scholar
expert, verified on k3d. Heavy convergence (`delegate_work` → `spawn_subagent`)
is a documented fast-follow (last section), out of v1 scope.

**Grounding anchors** (real symbols this plan builds on):
- Per-job tool/context build: `agent.py:2342 _setup_job_tools` → `ToolContext(...)`
  at `:2419` → the tool factory in `src/tools/registry.py`.
- LLM build: `create_llm(config, limits)` (`agent.py:399`), `.bind_tools(...)`
  (`agent.py:2702`).
- Tool-call fan-out: prebuilt `ToolNode` (`graph.py:3770`) gathers a turn's calls
  (`langgraph/prebuilt/tool_node.py:845`).
- Remote worktree: `agent.py:1767` (`git worktree add` over SSH),
  `GitManager.worktree_add`/`worktree_remove` (`git_manager.py:1057`/`:1085`),
  `from_worktree` (`git_manager.py:838`).
- Backend (single locked SSH/SFTP): `RemoteBackend` (`src/core/backends/remote.py:87`,
  `_sftp_lock:144`).
- LLM metering: `archiver.audit_llm_call` (`src/core/archiver.py:1192`) →
  `llm_requests` (+ `usage_events`).
- Port-range block to reuse: `_build_subagent_env_block` (`delegate_work.py:54`).

### Phase 0 — Config + registry scaffolding (no behavior)  ✅ DONE 2026-07-02
Goal: the knobs and the tool name exist; nothing runs yet.
- `config/defaults.yaml`: add `delegation.mode: heavy` (default) + a
  `delegation.light:` block (`enabled`, `max_iterations: 10`, `max_tokens: 40000`,
  `max_parallel: 3`, `allow_writes: false`). Mirror in `config/schema.json`.
- **Model tier (added at user request, same phase):** `llm.subagent` — a fourth
  `PhaseLLMOverride` on `LLMConfig` (`loader.py`), parsed in `_parse_phase_override`
  wiring, resolved by `get_phase_config("subagent")`, mirrored as
  `#/$defs/llmPhaseOverride` in `config/schema.json`. **Deliberately NOT** added to
  `has_phase_overrides()`. The reader model lives here, not in `delegation.light`.
- `src/tools/delegation/spawn_subagent.py` (new): `SPAWN_SUBAGENT_METADATA` +
  `create_spawn_subagent_tools(context)` — a coroutine `StructuredTool` with the
  flat schema, dispatching on `delegation.mode`: `light` → light stub (Phases 1-3);
  `heavy`/unset → "use delegate_work" stub. Wired through `delegation/__init__.py`
  (`create_delegation_tools` extends with it; `get_delegation_metadata` merges it).
  `delegate_work` / `resume_delegation_child` untouched.
- **Verified:** config load + schema validation; `spawn_subagent` in `TOOL_REGISTRY`
  (category delegation, phases strategic+tactical); flat 4-field schema; mode
  dispatch (light/heavy/unset); `llm.subagent` parses/resolves + tactical fallback +
  excluded from `has_phase_overrides`; 13 new unit tests in `tests/test_delegation.py`;
  `ruff` clean. (Registry filter is trivial name-match, so a light expert that lists
  `spawn_subagent` gets it — expert enablement itself is Phase 5.)

### Phase 1 — In-process ReAct harness (`run_light_subagent`)  ✅ DONE 2026-07-02
Goal: a bounded execute↔tools loop that runs ONE subagent and returns a string.
Pure, unit-testable with a fake LLM + fake tools.
- New `src/tools/delegation/light_runner.py`:
  `async def run_light_subagent(task, context, tools, llm, *, max_iterations=10, max_tokens=40000, port_block="", role="", expected_return_format="") -> str`.
  Loop: fresh messages (system preamble w/ `role`/`context`/`expected_return_format`/
  `port_block` + Human `task`) → `await llm.ainvoke(msgs)` → if `.tool_calls`,
  execute them → append `ToolMessage`s → repeat until no tool calls /
  `max_iterations` / `max_tokens` (token count via `count_tokens_approximate`,
  `context.py:518`). On a cap, one tool-free `_final_synthesis` call; falls back
  to last text, then a bounded-stop marker. Fresh context only — never the
  parent's history.
- **DEVIATION from plan (verified necessary):** the turn's tool calls are run via
  a local `_execute_tool_calls` + `asyncio.gather` helper, **not** LangGraph's
  prebuilt `ToolNode`. Reason: `ToolNode.ainvoke` standalone raises
  `Missing required config key 'N/A' for 'tools'` — it needs an ambient langgraph
  `Runtime`/store that only exists inside a compiled graph run (reproduced with
  no/empty/`configurable` config). The gather helper preserves the same
  concurrency property and per-call error isolation (a raised tool → error
  ToolMessage) with zero runtime coupling — strictly better for a pure harness.
  Tools serviced via `await tool.ainvoke(args)` (works for sync + coroutine
  StructuredTools). **Consequence for Phase 3:** the reader's LLM must still be
  `.bind_tools(tools)` (so the model emits tool_calls), but there is no ToolNode
  to construct.
- **Verified:** 14 unit tests in `tests/test_light_subagent.py` — final text;
  each cap (iteration + token) → synthesis; tool exec + result fed back;
  per-turn **concurrency** (deterministic `asyncio.Barrier(2)` — sequential would
  deadlock); tool-error isolation; fresh 2-message list (no parent history);
  empty-tools + tool-call-with-no-tools paths; empty-result marker; preamble
  contents. `ruff` clean.

### Phase 2 — Reader environment on the workspace pod  ✅ DONE 2026-07-02
Goal: give each reader its own isolated, pod-correct workspace + tools.

**KEY DISCOVERY + DECISION (worktree-rooting, user chose "build it now").** The
plan assumed a git worktree isolates a reader's files. It does not, on its own:
the workspace is **single-rooted at the backend** (`workspace.py:224` "the base
path IS the workspace"), and file tools resolve against `backend.root`
regardless of any git worktree (`files.py:934` → `workspace.write_file` →
`backend.write_file(relative_path)`). A worktree isolates *git* + shell-*cwd*
only; `write_file("x")` still lands in the **parent** root. Options were: (A)
read-leaning readers, no real file isolation; (B) a sub-rooted backend *view*
over the shared connection; (C) a second SSH connection (the design's deferred
"heavier" path). **User picked (B).**

Built:
- **`src/core/backends/subdir.py` `SubdirBackend`** — a duck-typed re-rooted VIEW
  over the parent backend (ONE shared SSH/SFTP connection, no second login).
  Prefixes workspace-relative *input* paths with the subdir; **strips** the
  prefix from *output* paths (`list_dir`/`search_files`/`walk`, which return
  root-relative). `shell_run` defaults `working_dir` to the subdir. Optional
  `shell_tab_prefix` **namespaces every tmux tab** so parallel readers never
  collide on the default tab, and `close_reader_tabs()` closes only the reader's
  tabs — the proxy deliberately never calls `shell_cleanup` (that would kill the
  session shared with the parent + siblings). HOME ops + connection lifecycle
  delegate via `__getattr__`. (16 unit tests, `tests/test_subdir_backend.py`.)
- **`src/tools/delegation/reader_env.py` `acquire_reader_env`/`release_reader_env`**:
  1. Under a module `asyncio.Lock`, `parent_git.worktree_add(.worktrees/sub_{i},
     sub/{i})` offloaded via `run_in_executor` (runs on the workspace pod; falls
     back to a plain `.subagents/reader_{i}` scratch subdir when the parent has
     no active git).
  2. `SubdirBackend(parent_backend, worktree, shell_tab_prefix="sub{i}__")`;
     reader `WorkspaceManager` rooted on it (`_initialized=True`, no re-init/rm
     -rf); reader `GitManager.from_worktree(..., remote_cwd=worktree)`.
  3. Own `ShellManager` over the SubdirBackend (namespaced tabs, cwd→worktree).
  4. Reader `ToolContext` = `copy.copy(parent)` (keeps `orchestrator_client` +
     `_job_metadata` → parent `job_id` → shared citation DB, datasources, db
     pools, config, llm_config, citation_engine) with workspace/shell swapped and
     `_snapshot_callback`/`_freeze_request`/`_recent_reads` reset per-reader.
  5. Tools = parent names **minus the `delegation` category** (`_reader_tool_names`,
     no nesting) and, unless `allow_writes`, minus `write_file`/`edit_file`.
  6. Accurate port/worktree awareness block (`_build_reader_port_block`).
  - `release_reader_env`: `close_reader_tabs()` + `worktree_remove` (best-effort).
- **Verified (9 integration tests, `tests/test_reader_env.py`, real git repo +
  `FilesystemTestBackend`):** worktree created/removed; reader tools exclude
  delegation; citation tools survive the filter; **reader `write_file` lands in
  the worktree, NOT the parent root**; reader shares orchestrator_client +
  job_metadata but gets a distinct worktree-rooted workspace; per-reader
  undo/freeze isolation; no-git scratch-subdir fallback.
- **Consequence for Phase 3:** `acquire_reader_env` needs the parent's resolved
  tool-name list — stash it on the ToolContext at `_setup_job_tools`
  (`agent.py:2419`) as e.g. `_resolved_tool_names` so the `spawn_subagent` light
  factory can pass it in.

### Phase 3 — The `spawn_subagent` light tool  ✅ DONE 2026-07-02
Goal: the glue the model calls; N parallel calls run concurrently for free.
- `spawn_subagent.py` light backend replaces the Phase-0 stub. `create_spawn_subagent_tools`
  dispatches on `delegation.mode`; `light` → `_make_light_spawn(context, light_config)`
  returns a **coroutine** `StructuredTool` (flat schema unchanged). Heavy branch
  keeps its "use delegate_work" stub.
- Per call (one call = one reader): validate `task_description` → `next(counter)`
  for a **unique index** (→ unique worktree + port range) under a shared
  `asyncio.Semaphore(max_parallel)` → `acquire_reader_env` (P2) → reader LLM
  (`create_llm(_resolve_subagent_config(_llm_config), limits=_limits).bind_tools(env.tools)`
  — subagent tier, tactical/base fallback) → `run_light_subagent` (P1) →
  `_format_result` header → `release_reader_env` in `finally`. **No** freeze.
- Parent tool-name list + limits come from `context._resolved_tool_names` /
  `context._limits`, stashed in `_setup_job_tools` (`agent.py`, right after
  `loaded_tool_names`); added as `ToolContext` fields.
- Coroutine + parent `ToolNode` gather ⇒ N parallel `spawn_subagent` calls run
  concurrently (bounded by the semaphore), no fan-out code of ours.
- **Verified (18 tests, `tests/test_spawn_subagent.py`):** subagent-tier
  resolution (subagent→tactical→base); header format; happy path builds the
  reader LLM from the **subagent tier** ("sonnet" not base) + wraps result;
  release-on-error (teardown despite a raised reader); **unique index per call**
  (3 concurrent → indices 0/1/2); empty-task rejected before acquire; **max_parallel
  bounds concurrency** (peak==2 with 5 concurrent); no-`_llm_config` fails closed;
  **integration** — real reader env (git repo + `FilesystemTestBackend`) + fake
  LLM runs end-to-end, returns the distilled string, worktree removed on teardown.
  Phase-0 stub test updated (light now dispatches for real). `ruff` clean.
- **Open (v1 deliberately simple):** `config` arg (expert-preset switch) is
  accepted but advisory — readers inherit the parent config (open question #3).

### Phase 4 — Metering  ✅ DONE 2026-07-02
Goal: reader LLM spend is not invisible.
- **Built as an LLM wrapper, not harness code** (keeps Phase 1 pure): `_MeteredLLM`
  in `spawn_subagent.py` wraps the reader's bound model; its `ainvoke` calls the
  inner model then archives via the module-level `archive_llm_request(...,
  call_type="subagent")` under the **parent** `job_id` — the same audit path the
  main loop + `AuxiliaryLLM._archive_call` use (`archiver.py` `archive`/module
  wrapper `:1489`; captures `metrics.token_usage` from `response.response_metadata`).
  The orchestrator's `audit_usage.py` materializer turns those `llm_requests` rows
  into `usage_events` under the parent job — no per-reader path needed.
  Fire-and-forget: a metering failure is logged, never breaks the reader. Meters
  EVERY turn of the ReAct loop (wrapper intercepts each `ainvoke`), incl. the
  final-synthesis call. `job_id` from `context._job_metadata`, `agent_type` from
  `context.config["agent_id"]`, `model` = the resolved subagent-tier model.
- **RESOLVED open question #1:** `create_llm` does NOT auto-attach an audit hook —
  the main loop archives explicitly around each invoke; the wrapper is the
  reader's equivalent.
- **Verified (5 tests, `tests/test_spawn_subagent.py::TestMetered*`):** each
  ainvoke archives under the parent job with `call_type="subagent"` + reader
  model; multiple turns → multiple rows; metering failure doesn't break the
  invoke; empty job_id skips; `bind_tools`/attrs delegate; **integration** — a
  real light-tool call archives one row under `parent-job`. `ruff` clean.

--- (superseded plan text below kept for reference) ---
- Ensure the reader's `create_llm` client carries the same audit/usage hook the
  main loop uses (`archiver.audit_llm_call`, `archiver.py:1192`), tagged with the
  PARENT `job_id` + a `subagent` marker. Rows must land in `llm_requests` +
  `usage_events`.
- **Verify:** a light fan-out → N readers' calls appear under the parent job in
  both tables.

### Phase 5 — Adoption engineering (scholar + critic + loop)  ✅ DONE 2026-07-02

**Scope upgraded** mid-build by [[subagents_never_used]] (fleet-wide audit:
0 all-time `delegate_work` invocations despite 100% tool availability and
rendered playbooks, both gpt-5.5 and MiniMax — prompts alone achieve nothing).
User decisions: mandatory *decision* at the planning point (not mandatory
delegation), remove `delegate_work` from scholar+critic entirely (no competing
instructions), full scope incl. critic + loop role blocks.

What shipped (all Jinja gates on `has_tool("spawn_subagent")`; delegate_work
prose fully removed from both experts — zero mentions left in either config
dir):
- **Configs:** scholar + critic `config.yaml`: `delegation.mode: light`;
  `tools.delegation: [spawn_subagent]` (delegate_work +
  resume_delegation_child dropped — 0 all-time invocations = nothing lost).
- **The keystone — todo scaffold** (`strategic_todos_initial.yaml`,
  unconditional text since scholar always grants the tool): PLAN todo (id 3)
  now requires a **fan-out decision per phase row** in plan.md's phase table —
  "fan-out (N subagents)" or "sequential: <reason>", default fan-out when 2+
  independent threads; completion criteria extended. CREATE todo (id 5) nudges
  fan-out-todo-first. Rationale: the decision point is `next_phase_todos`, and
  later phases follow plan.md — recording the decision there makes it durable
  past phase 0.
- **Strategic prompts** (scholar + critic, base **and** `_minimax` forks kept
  in sync — MiniMax is the loop driver): default-with-exception wording
  ("fanning out is the DEFAULT... sequential is the exception and needs a
  reason"), light semantics (returns a string, runs inline, fresh context,
  nothing suspends/merges), iterative fan-out (N calls in ONE turn), shared
  citation library note, parent-authors-synthesis / verdict-never-delegated.
- **Tactical prompts** (all 4 files): new short gated reminder block — the
  need becomes visible mid-work (doc mechanism #4, wrong-phase salience).
- **`todo_guide.md`:** §5 retargeted to "Fan-Out Research Phase" (subagents
  return STRINGS, don't write files; you author artifacts), numbering gate +
  quick-reference row flipped to `spawn_subagent`.
- **Loop `_ROLE_BLOCKS`** (`orchestrator/services/project_loops.py`): one
  fan-out sentence each for scholar (keep your context for synthesis) and
  critic (the verdict stays yours) — highest-salience task text loop roles see.
- **Tool description:** added "cheap and non-blocking... your own context
  stays small" framing (doc rec #4).

**REAL BUG found + fixed by the render check** (Phase 3's tests had masked it
by passing `context.config` dicts directly): `"delegation"` is a *known*
config field, so it's stripped from `config.extra` — and `tool_config` in
`agent.py` (+ `persistent_session.py`) is built from `extra`, so
`create_spawn_subagent_tools` saw **no `delegation` key at all** → silently
fell back to the heavy stub even with `mode: light` configured. Bonus: the
same gap means `delegate_work`'s call-time `enabled` check could NEVER pass —
had any model ever invoked it, it would have errored "delegation is not
enabled" (the 0-invocation stat hid a second, deeper layer of brokenness).
Fix: `DelegationConfig` gains `mode` + `light` fields (both parse sites);
`agent.py` + `persistent_session.py` inject `asdict(config.delegation)` into
`tool_config`. Regression-pinned in
`tests/test_spawn_subagent.py::TestDelegationConfigPlumbing` (real expert
configs → factory dispatches the light backend end-to-end).

- **Verified:** render-check script (49 checks: configs resolve light w/ knobs
  inherited; all 8 prompt files render fan-out guidance when granted / hide it
  when not; no stale delegate_work prose anywhere; no deterrent suspend
  vocabulary; seeded todos parse through the real loader with the fan-out
  decision present; todo_guide numbering flips 5↔6). 121 tests across the
  feature suite + project_loops; ruff clean.

### Phase 5b — post-k3d adoption measurement (queries from [[subagents_never_used]])
- Menu presence + invocation counts via audit DB; healthy trace shape = parent
  prompt size FLAT while N `call_type='subagent'` bursts appear. Heavy-path
  children stay 0.

### Phase 6 — k3d end-to-end (the CLAUDE.md gate)
- Scholar session/job whose phase fans out readers over several sources.
- Assert: multiple `spawn_subagent` calls in one turn run concurrently (agent-pod
  log timing); worktrees created/removed on the WORKSPACE pod (not the agent pod);
  citations land in the shared DB under the parent job; parent synthesizes +
  writes `output/ideas/*`; metering rows present; no stray tmux/worktrees left.

### Test matrix
- **unit:** harness caps/loop, env acquire/release, tool validation, no-nesting,
  result header.
- **integration:** parallel fan-out + failure isolation, `FilesystemTestBackend`.
- **e2e:** k3d scholar fan-out (Phase 6).

### Fast-follow — heavy convergence (out of v1 scope)
- Rename `delegate_work` → `spawn_subagent`; heavy backend accepts the flat
  single-task schema (one call = one child job).
- **Coalesce** N parallel heavy `spawn_subagent` calls in a turn into a *single*
  parent freeze (the tricky bit — today's batch tool issues one freeze for the
  whole list; iterative needs the tool node to collect the N child-creations,
  then freeze once).
- Update critic/developer/scholar heavy prompts off the batch pattern.
- Remove the Phase-0 `mode: heavy` error stub.

### Open implementation questions (resolve during build; not blockers)
1. Does `create_llm`'s client attach the audit hook automatically, or must the
   reader pass callbacks explicitly? (Phase 4.)
2. Concurrent `exec` over the single SSH transport — confirm channels don't
   serialize badly at `max_parallel` (Phase 2/6); if they do, a per-reader
   backend connection is the escalation.
3. `role`/`config` — which expert preset a reader may adopt, and whether to allow
   switching at all in v1 (safe default: inherit the parent's config only).
