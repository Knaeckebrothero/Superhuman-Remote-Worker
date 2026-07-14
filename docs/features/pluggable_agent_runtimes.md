---
tags:
  - feature
  - architecture
  - agents
  - runtime
  - react
  - orchestration
  - subagents
  - project-loop
aliases:
  - pluggable agent runtimes
  - agent runtime types
  - react runtime
  - runtime profiles
related:
  - "[[agent_lifecycle]]"
  - "[[auxiliary]]"
  - "[[subagent_delegation]]"
  - "[[delegation_light_mode_missing]]"
  - "[[persistent_agent_assessment]]"
  - "[[no_workspace_agent_mode]]"
  - "[[loop_control_plane_assessment]]"
  - "[[project_self_improvement_loop]]"
---

# Pluggable Agent Runtimes

> Separate **who an agent is** from **how it executes**. An expert supplies its
> role, instructions, model, and capabilities; a runtime supplies the control
> loop; a lifecycle supplies the ownership and durability boundary; and an
> output contract defines what counts as successful completion.

**Status:** PROPOSED — design draft, 2026-07-14. No runtime behavior has been
changed by this document.

**Primary decision:** add an operator-controlled execution-engine choice to
resolved agent configuration, with three initial engines:

- `structured` — one bounded, schema-constrained reasoning call;
- `react` — a context-managed `LLM -> tools -> LLM` loop without phase/todo
  machinery;
- `phase_graph` — the existing strategic/tactical graph, unchanged and the
  backward-compatible default.

The first consumers should be typed project-loop control stages and capable
subagents. The existing Developer remains on `phase_graph`.

## Contents

- [1. Executive summary](#1-executive-summary)
- [2. Problem statement](#2-problem-statement)
- [3. Goals](#3-goals)
- [4. Non-goals](#4-non-goals)
- [5. Terminology and independent axes](#5-terminology-and-independent-axes)
- [6. Proposed runtime engines](#6-proposed-runtime-engines)
- [7. Configuration model](#7-configuration-model)
- [8. Runtime interface](#8-runtime-interface)
- [9. Output contracts](#9-output-contracts-and-control-plane-handoff)
- [10. ReAct context and durability](#10-react-context-management-and-durability)
- [11. Tools, permissions, and workspace](#11-tools-permissions-and-workspace-safety)
- [12. Lifecycle integration](#12-lifecycle-integration)
- [13. Project-loop adoption](#13-project-loop-adoption)
- [14. Observability](#14-observability)
- [15. Failure semantics](#15-failure-semantics)
- [16. Compatibility and migration](#16-backward-compatibility-and-migration)
- [17. Implementation plan](#17-implementation-plan)
- [18. Test plan](#18-test-plan)
- [19. Acceptance criteria](#19-acceptance-criteria)
- [20. Risks and mitigations](#20-risks-and-mitigations)
- [21. Decisions and open questions](#21-decisions-and-open-questions)
- [22. Source map](#22-source-map)

---

## 1. Executive summary

Superhuman Remote Worker has configurable experts but does not yet have
configurable job runtimes. An expert can change its persona, model, prompts,
tools, workspace, memory, and limits; every ordinary worker job nevertheless
enters the same strategic/tactical LangGraph runtime.

That coupling causes two apparently different problems:

1. **Small control decisions pay full job overhead.** Scholar, Critic, and
   verification jobs create strategic todos, plan tactical work, archive
   phases, negotiate transitions, and use completion tools even when their real
   responsibility is to return one typed decision.
2. **Light subagents are intentionally too small for deep work.** The current
   light reader is a clean graph-free ReAct loop, but it stops and synthesizes
   at 10 iterations or about 40,000 running-context tokens. It does not compact
   and continue. Raising those caps without adding context management,
   deadlines, and aligned parent timeouts does not turn it into a durable
   long-horizon agent.

The repository already contains the required primitives, but they are exposed
through four separate execution paths:

| Existing path | Strength | Limitation as a general runtime |
|---|---|---|
| Strategic/tactical graph | Deep work, phases, checkpoints, recovery, context management | Mandatory for every ordinary job; high overhead for narrow decisions |
| `AuxiliaryLLM.chain()` / `.agent()` | Structured output, recovery, timeout, focused support tasks | Inline support service, not an independently schedulable job runtime; agent mode is deliberately short |
| Light subagent runner | Fresh context, isolated tool loop, concurrent tool calls, simple limits | Returns free text; no compaction/checkpoint; folded into parent identity |
| Persistent execution loop | Mature tool loop, compaction, memory injection, incremental message durability | Coupled to threads, user-input waiting, session callbacks, permissions, and persistent history |

The missing abstraction is therefore not another expert. It is a runtime seam
that lets the same expert configuration execute under an appropriate engine.

The target decomposition is:

```text
Expert / role       Runtime engine       Lifecycle / scope       Output contract
---------------     ----------------     ------------------      -----------------
Scholar             react                ordinary loop job       CandidateDelta
Critic              structured           ordinary loop job       SelectionDecision
Developer           phase_graph          ordinary worker job     DeliveryOutcome
Verifier            react                ordinary loop job       VerificationDisposition
Investigator        react                nested subagent          InvestigationResult
Interactive expert  react                persistent session       conversational turn
```

This is not a proposal to remove the phase graph. It makes that graph one
deliberate execution engine instead of the implicit meaning of "agent job."

---

## 2. Problem statement

### 2.1 Expert identity and runtime behavior are coupled

`AgentConfig` has no execution field. `UniversalAgent.process_job()` sets up the
workspace and tools, creates a checkpointer and phase snapshot manager, then
unconditionally calls `build_phase_alternation_graph()`.

Consequences:

- Adding a new `critic-lite` or `loop-selector` expert changes prompts and
  tools, but it still enters the full phase graph.
- Removing or shrinking a strategic-todo template does not create a lean
  agent. Missing initial templates fall back to hard-coded planning todos, and
  archive/transition/completion nodes remain wired.
- Invoking `spawn_subagent` from inside a phased Critic does not remove the
  Critic's overhead. It wraps a small runtime inside the same expensive parent.
- Dispatching a persistent-flavoured expert as an ordinary job does not select
  persistent execution; it still follows the worker path.

### 2.2 The system already has multiple runtime kernels

This duplication is useful evidence that the abstraction is real:

- `src/graph.py` contains the strategic/tactical engine.
- `src/services/auxiliary.py` contains single-call structured execution and a
  short tool loop with structured completion.
- `src/tools/delegation/light_runner.py` contains a pure fresh-context ReAct
  loop for inline subagents.
- `src/persistent_graph.py` contains a context-managed interactive ReAct loop.

Each path made reasonable local trade-offs. The problem is that there is no
common interface through which a job, loop stage, subagent, or session can ask
for the appropriate engine.

### 2.3 Fixed small caps are being mistaken for the subagent model

The light reader's 10-iteration and 40,000-token limits are appropriate for
its original use case: a throwaway reader receives a narrow task, gathers a
small amount of evidence, synthesizes, and returns to the parent.

They should remain available as a cheap **resource profile**, not define what
all subagents are. A deep investigation needs:

- fresh isolated context;
- context compaction that continues execution rather than terminating it;
- a wall-clock and cumulative-spend envelope;
- cancellation and deadline propagation;
- audited tool execution;
- an explicit output contract;
- and, for sufficiently long or mutating work, checkpoint or job-level
  durability.

### 2.4 Optional tools are a weak workflow boundary

The live project-loop assessment showed that telling a model it *may* call a
workflow tool does not guarantee that the decision enters the control plane.
A Critic can reason correctly, write a prose verdict, and finish without filing
the plan or disposition that orchestration needs.

Runtime completion must therefore be separate from discretionary tool use. If
a stage promises a `SelectionDecision`, the runtime must return and validate
that schema. The orchestrator, not the model, applies the state transition.

---

## 3. Goals

1. **Make runtime selection explicit and typed.** Missing configuration keeps
   current behavior; unknown runtime names fail loudly.
2. **Preserve expert reuse.** The same expert can have a execution default and be
   overridden by a trusted job or loop-stage configuration.
3. **Provide a real long-horizon ReAct engine.** Context compaction and
   continuing execution replace tiny hard stops as the normal operating mode.
4. **Make stage success schema-based.** A technically completed LLM loop is not
   automatically a successful Scholar, Critic, or verifier stage.
5. **Reuse existing infrastructure.** Extract and compose the graph,
   persistent, auxiliary, and light-runner primitives rather than writing a
   fifth independent loop.
6. **Keep the orchestrator authoritative.** Agent runtimes return results,
   freeze requests, and failure information; they do not directly persist
   final job status.
7. **Keep tools and workspace policy orthogonal.** Choosing `react` must not
   implicitly grant shell, write, delegation, network, or repository access.
8. **Support multiple lifecycles.** The same ReAct kernel should eventually
   serve an ordinary job, nested subagent, or persistent turn without making
   those lifecycles the same thing.
9. **Make cost and stopping behavior observable.** Every call, compaction,
   contract repair, and terminal reason must be attributable.

---

## 4. Non-goals

- Replacing or simplifying the strategic/tactical phase graph in this feature.
- Making the model choose its own runtime or budget.
- Moving LLM/tool execution into the orchestrator.
- Treating persistent sessions as synthetic loop workers.
- Giving all subagents unlimited execution or recursive delegation.
- Claiming exact implementation or limit parity with a proprietary agent; the
  target is the capability class: fresh context, long-horizon tool use,
  compaction, cancellation, durability, and explicit resource envelopes.
- Building a general DAG, planner/executor, swarm, or team protocol in v1.
- Reusing `jobs.runner_kind` or adding a second expert-type taxonomy for
  cognitive execution.
- Loading arbitrary user-provided runtime classes or JSON schemas in v1.
- Removing LangGraph merely to make the React engine "graph free."
- Migrating every `AuxiliaryLLM` support task onto the new runtime immediately.
- Changing existing expert behavior merely because the execution field exists.
- Equating a larger turn allowance with reliability; long-running execution
  also requires context, timeout, cancellation, and durability design.

---

## 5. Terminology and independent axes

The design deliberately avoids a single overloaded `mode` enum.

### 5.1 Expert

The expert defines **who the agent is and what it can do**:

- persona and role instructions;
- model tiers and reasoning settings;
- tool allowlist;
- memory/knowledge configuration;
- workspace policy;
- autonomy and domain-specific settings.

Examples: `developer`, `scholar`, `critic`, `product-qa`.

### 5.2 Runtime engine

The runtime defines **how one unit of work executes**:

- control loop;
- context handling;
- stopping rules;
- checkpoint behavior;
- terminal-output production.

Initial engines: `structured`, `react`, `phase_graph`.

### 5.3 Lifecycle / execution scope

The lifecycle defines **who owns the execution and how long it lives**:

- ordinary job with its own job row;
- inline nested subagent whose usage folds into its parent;
- child job with independent status/workspace/merge;
- persistent session turn with thread history.

Lifecycle is chosen by the caller and orchestration path, not by expert YAML.
A persistent session is therefore not a fourth runtime engine. It is a
long-lived lifecycle that currently hosts a ReAct-like turn engine.

### 5.4 Workspace policy

Workspace policy remains independent:

- `none` / virtual scratch;
- read-only or throwaway worktree;
- writable isolated branch;
- persistent session workspace.

A structured Critic may need no repository workspace. A React verifier may
need a read-only repository. A phased Developer generally needs a writable
branch. Runtime selection must not infer these privileges.

### 5.5 Output contract

The output contract defines **what success means**. It is a versioned Pydantic
schema and semantic validator, not only a prompt instruction.

Examples:

- `candidate_delta.v1`;
- `selection_decision.v1`;
- `verification_disposition.v1`;
- `investigation_result.v1`.

The same runtime can serve different contracts.

### 5.6 Resource profile

A resource profile supplies operator-owned defaults for time, cumulative
tokens, compactions, concurrency, and emergency limits. Profiles such as
`reader`, `investigator`, and `controller` are policies layered on a runtime,
not new agent types.

---

## 6. Proposed runtime engines

### 6.1 `structured`

One schema-constrained reasoning operation over a complete provided context.
V1 has no general tool loop.

```text
system prompt + immutable input snapshot
                    |
                    v
       schema-constrained LLM call
                    |
                    v
       parse / recover / validate
                    |
                    v
             RuntimeResult
```

Use it when the information set can be assembled deterministically before the
call:

- selecting from a complete backlog snapshot;
- classifying a result;
- rendering a disposition;
- applying a policy or quality gate;
- extracting a typed delta from bounded evidence.

Required behavior:

- input-context preflight against the selected model window;
- `with_structured_output()` using the model-family method;
- existing structured-recovery fallback;
- bounded repair attempt with validation errors when recovery cannot satisfy
  the semantic contract;
- per-call timeout, audit, usage accounting, cancellation, and fallback policy;
- no loop advancement unless the contract validates.

This should initially build on `AuxiliaryLLM.chain()` semantics without making
all runtime jobs auxiliary support tasks.

### 6.2 `react`

A fresh-context, phase-free tool loop:

```text
task + context
      |
      v
  LLM response ---- no tools / finish ----> structured completion
      |
      | tool calls
      v
audited tool execution
      |
      +-------------------------------> LLM response

At context threshold:
messages -> prune tool payloads -> summarize evicted history -> continue
```

`react` means an **unphased context-managed tool loop**. The existing phase
graph also contains ReAct internally, but adds strategic/tactical planning,
todo, archive, transition, and goal nodes; those are absent here.

Required behavior:

1. Start with a fresh message list for each job or nested invocation unless a
   persistent lifecycle explicitly supplies history.
2. Bind only the resolved, runtime-compatible tools.
3. Execute same-turn tool calls concurrently where the tool runner declares it
   safe, preserving one `ToolMessage` per call and isolating failures.
4. Audit every LLM call and tool batch, not only the terminal synthesis call.
5. Compact and continue when context occupancy reaches the configured
   threshold. Context occupancy is not cumulative spend.
6. Stop on valid completion, cancellation, deadline, cumulative-spend limit,
   unrecoverable provider failure, or an emergency safety rail.
7. On a budget stop, permit one tool-free synthesis/contract call if the
   remaining deadline and token reserve allow it.
8. When a contract is configured, perform a schema-constrained terminal call
   and semantic validation. A free-text no-tool response alone is not stage
   success.
9. Surface `stop_reason`, calls, tool calls, compactions, token usage, elapsed
   time, and contract status in `RuntimeResult`.

#### Limits are safety envelopes, not the operating model

The current light reader stops at a small iteration or context-token count.
The production ReAct runtime instead distinguishes:

- **context threshold:** compact current messages and continue;
- **cumulative token budget:** cap total billed/generated work;
- **wall-clock deadline:** cap elapsed execution;
- **emergency iteration/tool-call ceiling:** catch pathological loops;
- **compaction ceiling:** catch an agent that repeatedly fills context without
  converging;
- **parent deadline:** nested execution must finish before its caller's
  watchdog.

No limit is literally infinite. A capable subagent can nevertheless make
dozens or hundreds of tool rounds when its resource envelope permits it.

### 6.3 `phase_graph`

The existing `build_phase_alternation_graph()` engine:

- workspace initialization;
- strategic todo planning;
- tactical execution;
- audited tools;
- phase archives and snapshots;
- strategic/tactical transitions;
- goal/completion checks;
- recovery and review freezes.

V1 wraps current behavior behind the runtime interface without changing graph
semantics. It remains the default for all existing configurations.

`phase_settings` applies only to this engine. Supplying meaningful
phase-specific configuration to another runtime should produce a clear warning
or validation error once migration data shows which rule is least disruptive.

---

## 7. Configuration model

### 7.0 Naming: `execution`, not `runtime` or `runner_kind`

This document uses **runtime** as the architectural term, but the configuration
key should be `execution`:

- `docs/features/pod_runtime.md` already uses `runtime` for compute placement
  (`pod | vm | local`);
- `jobs.runner_kind` identifies dispatch/authority provenance
  (`user | lifecycle | service`), not cognitive execution;
- `agent_mode` and worker/persistent mode describe process/lifecycle hosting.

Using `execution.engine` keeps reasoning behavior distinct from where a process
runs and who authorized it. Neither existing field should be overloaded.

### 7.1 Expert default

Proposed initial shape:

```yaml
execution:
  engine: phase_graph       # structured | react | phase_graph
  profile: null             # optional operator-defined resource profile
  model_role: base          # base | strategic | tactical | subagent

  limits:
    max_wall_seconds: null  # null inherits profile / system policy
    max_total_tokens: null  # cumulative usage, not live context size
    max_iterations: null    # emergency ceiling for react
    max_tool_calls: null
    max_compactions: null

  completion:
    contract: null          # e.g. selection_decision.v1
    repair_attempts: 1

  checkpoint:
    policy: auto            # auto | none | turn | phase
```

The initial typed `ExecutionConfig` should remain small. Context thresholds,
retained messages, summarization prompts, tool timeouts, model windows, and
workspace behavior already have homes in `limits`, `context_management`, tool
configuration, and `workspace`. The runtime should reference those settings
rather than create parallel copies.

For `structured` and `react`, `model_role` selects an already resolved expert
model tier; it does not introduce another model registry. `phase_graph` keeps
its existing strategic/tactical selection. V1 defaults unphased engines to
`base`. Selecting `subagent` is allowed only after its credential injection and
secret-redaction paths have the same coverage as every other LLM slot.

### 7.2 Resource-profile examples

Exact values require measured canaries; these examples illustrate semantics,
not committed production defaults.

```yaml
execution_profiles:
  reader:
    engine: react
    max_wall_seconds: 180
    max_total_tokens: 60000
    max_iterations: 20
    max_compactions: 0

  investigator:
    engine: react
    max_wall_seconds: 1800
    max_total_tokens: 500000
    max_iterations: 500
    max_compactions: 8

  loop_controller:
    engine: structured
    max_wall_seconds: 180
    max_total_tokens: 50000
```

The current ten-turn light reader can survive as a cheap `reader` profile. It
is no longer the only available definition of a nested subagent.

Profile storage is deliberately left open for the first implementation slice:
it may begin as named framework defaults before gaining an Admin-managed
registry. Profile selection remains operator-controlled.

### 7.3 Precedence and authority

```text
trusted job / loop-stage config_override.execution
                        ↓
             selected expert default
                        ↓
             defaults.yaml execution
                        ↓
           implicit phase_graph fallback
```

Rules:

- Existing configs without `execution` resolve to `phase_graph`.
- An unknown engine or contract fails configuration validation; it never
  silently falls back.
- Execution configuration is frozen into `resolved_config` at dispatch.
- The model cannot choose or upgrade its runtime through a tool call.
- Orchestrator-created jobs may narrow runtime privileges or budgets but may
  only widen them within an explicit server-side policy.
- The loop stage should override runtime without globally changing the normal
  `critic` or `scholar` expert. A manual deep Critic job may still need phases.

### 7.4 Example loop overrides

```yaml
# Scholar / backlog groomer stage
execution:
  engine: react
  profile: investigator
  completion:
    contract: candidate_delta.v1

# Critic / selector stage
execution:
  engine: structured
  profile: loop_controller
  completion:
    contract: selection_decision.v1

# Developer stage
execution:
  engine: phase_graph
```

### 7.5 Model-slot credential and redaction parity

Model-role handling is currently enumerated in several places. In particular,
resolved-config secret stripping does not cover every declared LLM tier, and
dispatch credential injection/validation is not yet symmetric for
`llm.subagent`. Promoting that tier into a general React runtime before fixing
the traversal can produce both failure directions: a secret may survive into
persisted resolved configuration, or a remote model may arrive without its own
transport credentials.

Before non-base roles are runtime-selectable:

- enumerate model slots from the dataclass/schema rather than repeated literal
  lists where practical;
- strip secrets from every slot during resolved-config serialization;
- inject endpoint/key/provider data for every selected slot at dispatch;
- include every slot in capability-grant validation and unrouted-model checks;
- add a parity test that fails when a model tier is added to one path but not
  the others.

---

## 8. Runtime interface

The worker should perform common setup, then delegate execution to a runtime
selected from a registry.

```python
@dataclass
class RuntimeContext:
    job_id: str
    config: AgentConfig
    metadata: dict
    llms: RuntimeLLMs
    tools: list
    tool_context: ToolContext | None
    workspace: WorkspaceManager | None
    auxiliary_llm: AuxiliaryLLM | None
    cancellation: CancellationToken
    archiver: LLMArchiver
    events: RuntimeEventSink


@dataclass
class RuntimeResult:
    output: dict | str | None
    contract: str | None
    contract_valid: bool
    stop_reason: str
    usage: dict
    metrics: dict
    freeze_data: dict | None = None
    error: dict | None = None


class AgentRuntime(Protocol):
    async def run(self, context: RuntimeContext) -> RuntimeResult: ...
```

The exact types may evolve during implementation. The invariants matter:

- runtimes do not update final job status directly;
- every terminal path produces a reason;
- typed outputs remain typed until the API serialization boundary;
- cancellation and audit are supplied by the host rather than reimplemented;
- runtime-specific state does not leak into the next reused worker job.

`RuntimeEventSink` (or an equivalent async-yield interface) is not optional
plumbing. Current worker pause/cancel handling observes streamed graph states;
a monolithic direct loop could otherwise ignore cancellation until the outer
hard-kill timeout. Every engine must emit/check at safe boundaries:

- before and after each LLM call;
- after each complete assistant/tool-call/tool-result group;
- before and after compaction;
- while waiting for nested execution;
- and before terminal contract production.

The boundary after a complete tool-result group is also the minimum safe point
for drain, checkpoint, and model/workspace upgrade. A context summary by itself
is not a durable execution checkpoint.

### 8.1 Runtime registry

Suggested layout:

```text
src/runtimes/
  __init__.py
  base.py
  registry.py
  structured.py
  react.py
  phase_graph.py
  contracts.py
```

```python
RUNTIME_REGISTRY = {
    "structured": StructuredRuntime,
    "react": ReactRuntime,
    "phase_graph": PhaseGraphRuntime,
}
```

Registration is code-owned in v1. Expert YAML selects a known engine; it does
not import arbitrary runtime classes.

`ReactRuntime` may be implemented as a direct async loop or as a minimal
LangGraph. "Unphased" is the requirement; "contains no graph library" is not.
A minimal graph may be preferable if it preserves streaming, checkpoints, and
safe interruption without importing todo/archive/transition semantics. Resolve
this from a spike rather than treating graph removal as a goal.

### 8.2 Worker execution seam

Target flow:

```text
resolve + freeze config
          |
initialize common LLM/audit dependencies
          |
prepare workspace only as required by workspace policy
          |
resolve tools + runtime compatibility gates
          |
RuntimeRegistry.create(config.execution.engine)
          |
runtime.run(RuntimeContext)
          |
adapt RuntimeResult to existing agent completion payload
          |
orchestrator validates/persists final status and stage result
```

For a low-risk first slice, runtime selection can occur after current workspace
and tool setup but before checkpointer/phase-graph construction. That removes
phase-token overhead while reusing mature initialization. A later slice can
move no-workspace structured jobs earlier to avoid unnecessary workspace
provisioning.

The code below this seam is currently graph-shaped as well: streaming, resume,
freeze propagation, cleanup, and result normalization all inspect graph state.
Moving construction behind a registry is therefore necessary but not
sufficient. Those behaviors must be expressed through `RuntimeResult` and
runtime events before `react` is production-enabled.

### 8.3 Upgrade and resume paths

The current workspace-upgrade path rebuilds the phase graph after changing
backends. Runtime selection must also apply there:

- `phase_graph` rebuilds the existing graph;
- `react` rebinds its tools/workspace and resumes from its runtime checkpoint
  when supported;
- `structured` should normally restart from its immutable input snapshot.

No code path should silently rebuild `phase_graph` merely because a workspace
changed.

---

## 9. Output contracts and control-plane handoff

### 9.1 Contract registry

Contracts should be versioned and code-owned:

```python
OUTPUT_CONTRACTS = {
    "candidate_delta.v1": CandidateDelta,
    "selection_decision.v1": SelectionDecision,
    "verification_disposition.v1": VerificationDisposition,
    "investigation_result.v1": InvestigationResult,
}
```

Schema validity alone is insufficient. Each contract may also have a semantic
validator, for example:

- selected candidate exists in the supplied snapshot;
- decision references the same snapshot/generation id;
- `start` includes an objective and success criteria;
- `continue` references the current initiative;
- `close` includes outcome evidence;
- CandidateDelta identities are unique and resolve aliases;
- cited evidence belongs to the job's allowed project scope.

### 9.2 Example `SelectionDecision`

```python
class SelectionDecision(BaseModel):
    snapshot_id: UUID
    action: Literal["start", "continue", "defer", "kill", "research"]
    candidate_id: UUID | None
    current_initiative_id: UUID | None
    objective: str
    rationale: str
    success_criteria: list[str]
    budget_hint: int | None
    verification_required: bool
    evidence_ids: list[UUID]
```

The model returns the decision. The orchestrator applies it transactionally.
The model does not need to call `loop_plan`, construct an opaque note id, or
mutate campaign state directly.

### 9.3 Role success versus technical completion

The runtime may finish its LLM loop while its contract remains invalid. Those
are separate facts:

```text
runtime stopped cleanly + valid contract      -> stage can succeed
runtime stopped cleanly + invalid contract    -> repair/retry/fail; do not advance
runtime deadline + valid partial disposition  -> policy decides whether acceptable
runtime error                                 -> orchestrator retry/backoff policy
```

The agent reports the facts in `RuntimeResult`. The orchestrator remains the
authority that maps them to `completed`, `failed`, `paused`, or
`pending_review` and decides whether a project loop advances.

### 9.4 Persistence and idempotency

- Store stage outputs in authoritative application state, not only in the KB
  or workspace prose.
- Persist the input snapshot id and contract version with the result.
- Use an idempotency key derived from job id + contract + attempt so a retried
  completion cannot apply the same selection twice.
- Continue writing human-readable KB notes when valuable, but treat them as
  evidence/projections rather than workflow authority.
- Use existing JSONB merge helpers for `jobs.context`; do not introduce a
  read-modify-write race.

### 9.5 Completion transport must gain an explicit result field

The current agent-completion transport is shaped around graph termination
(`should_stop`, `goal_achieved`, `error`, and `freeze_data`). A new typed domain
result would be dropped unless the API is extended. Do **not** overload
`freeze_data`: it already carries review, infrastructure, waiting, upgrade, and
completion semantics.

Add an explicit versioned payload, for example:

```json
{
  "should_stop": true,
  "goal_achieved": true,
  "execution_result": {
    "engine": "structured",
    "contract": "selection_decision.v1",
    "contract_valid": true,
    "payload": {},
    "stop_reason": "contract_complete"
  }
}
```

The orchestrator validates the contract and legal domain transition, persists
it transactionally, and only then marks the lifecycle stage eligible to
advance. Expert-owned configuration may suggest a default contract, but a
server-created loop stage owns its mandatory contract and an expert fragment
cannot weaken or replace it.

---

## 10. ReAct context management and durability

### 10.1 Compaction must continue execution

The live message list should use the existing `ContextManager` and
summarization infrastructure:

1. count live context against the active model window;
2. preserve the system/task contract and recent working set;
3. clear or truncate large old tool payloads;
4. summarize the evicted slice;
5. repair tool-call/tool-result pairing;
6. reinsert the summary with provenance;
7. continue the tool loop.

The summary is a context checkpoint, not the final result. Reaching a context
threshold must not automatically force synthesis and terminate.

### 10.2 Cumulative budget accounting

Compaction reduces live context but does not erase cost. The runtime tracks:

- cumulative input/output/cache tokens;
- LLM calls;
- tool calls and tool wall time;
- compaction calls and summarized tokens;
- elapsed wall time;
- fallback-model use;
- nested-agent spend charged to the parent envelope.

### 10.3 Checkpoint policy

Different scopes require different durability:

| Scope | Initial policy | Rationale |
|---|---|---|
| Structured loop controller | restart whole call from immutable snapshot | Cheap, deterministic, normally read-only |
| Short read-only React controller | restart whole stage in v1 | Avoid premature checkpoint complexity; side-effect free tools required |
| Long React investigator | turn-boundary message checkpoint | Prevent losing many calls/compactions |
| Mutating React job | turn/tool idempotency plus checkpoint | Restarting blindly can repeat external effects |
| Phase graph | existing graph/phase checkpoint | Preserve current behavior |
| Persistent session | existing incremental thread-message durability | Lifecycle-specific history remains outside v1 runtime migration |

`checkpoint.policy: auto` resolves based on runtime, lifecycle, mutability, and
configured duration. An unsupported unsafe combination must fail at dispatch,
not degrade silently.

### 10.4 Deadline hierarchy

Nested work needs one source of truth:

```text
orchestrator/job deadline
        > parent runtime deadline
            > nested subagent deadline
                > individual LLM/tool-call timeout
```

Every inner deadline must leave enough reserve for cancellation, terminal
schema production, result transport, and parent cleanup. The current failure
class where a parent watchdog expires before a child can finish must be covered
by validation and tests.

### 10.5 Memory and transient context

The phase graph and persistent loop currently own different memory injection,
extraction, curation, and transient-message behavior. React v1 must state its
support explicitly rather than accidentally inheriting whichever path is
easiest to call.

Minimum rules:

- immutable task/contract input is always present;
- project memory/KB injection is opt-in through the resolved expert config;
- transient retrieval blocks are refreshed after compaction and are not folded
  into durable summaries as if they were model-authored facts;
- pre-compaction extraction, if enabled, observes the evicted source slice;
- curation/observer work is metered and cannot silently outlive cancellation;
- unsupported phase-boundary writers are disabled or remapped to explicit
  React turn, compaction, or completion boundaries.

---

## 11. Tools, permissions, and workspace safety

### 11.1 Runtime selection is not a capability grant

The selected expert/tool config remains authoritative. A React runtime only
drives tools already resolved for the job and permitted by the workspace and
security policy.

### 11.2 Phase-control tools

An unphased runtime must not receive tools whose sole purpose is controlling
the phase graph. At minimum, review:

- todo creation/completion tools;
- phase transition and archive tools;
- `mark_complete` / `job_complete` rituals;
- freeze paths that assume a LangGraph checkpoint;
- delegation resume tools that assume a suspended phased parent.

React completion is runtime-owned and contract-driven. The model should not
need to perform a two-stage completion ritual to escape the loop.

Tool metadata currently describes strategic/tactical phase availability. The
runtime work must add an explicit compatibility gate or a resolved tool
profile for unphased execution; absence of a current phase must not accidentally
mean "allow everything."

### 11.3 Tool execution

Do not promote the light runner's direct invocation helper wholesale. The
production runtime must preserve:

- audit and usage events;
- guardrails and model-specific tool adaptations;
- category timeouts;
- permission and network policy;
- cancellation checks;
- per-tool error isolation;
- `ToolContext` freeze-request consumption and lifecycle propagation;
- snapshot/freeze semantics where supported;
- progress/heartbeat events at tool boundaries;
- concurrency only where tool metadata declares it safe.

Extract a graph-independent audited tool executor from the current graph tool
node or provide a shared adapter used by both runtimes.

The current light runner gathers every same-turn call concurrently. A shared
executor must serialize mutating or ordering-sensitive calls unless their
metadata explicitly opts into parallel safety. Concurrent file edits, SQL/API
mutations, git operations, messages, or lifecycle tools are not safe merely
because the model emitted them together.

Cancellation also follows tool semantics. LLM generation and compaction may be
hard-interrupted when their clients support it. A side-effecting tool should
normally finish, persist its attributable result, and stop at the next safe
boundary rather than be killed in an unknown half-applied state. Tools may opt
into stronger cancellation only when they define their own cleanup/idempotency
contract.

### 11.4 Nested-agent restrictions

- Default nesting depth remains bounded.
- Runtime/profile choice is operator-controlled; the parent model selects a
  task or allowed expert, not an arbitrary larger budget.
- Child resource envelopes are debited from or capped by the parent/job
  envelope.
- Read-only profiles must remove all mutating surfaces, not only
  `write_file`/`edit_file`; shared KB, database, shell, network, and external
  tools require explicit review.
- Writable nested work requires a merge/reconciliation lifecycle; inline
  throwaway readers must not make authoritative product changes.

---

## 12. Lifecycle integration

### 12.1 Ordinary worker jobs

This is the first supported scope. Jobs retain normal dispatch, job rows,
project linkage, audit, status callbacks, cancellation, and Cockpit visibility.
Only the execution engine changes.

### 12.2 Inline subagents

The current `spawn_subagent` light path becomes an adapter around the shared
React engine:

- fresh messages;
- isolated/throwaway reader workspace;
- parent-owned metering and deadline;
- configurable resource profile;
- structured or free-text result;
- no independent job status in the initial inline form.

This permits both a small `reader` and a deeper `investigator` profile. It also
eliminates a separate light-loop implementation once behavior is equivalent.

### 12.3 Child jobs

Heavy delegation remains appropriate for independently durable, mutating work:

- own job row and status;
- own writable branch/worktree;
- parent freeze/wake;
- review and squash merge;
- phase or React runtime chosen through the child's resolved configuration.

Runtime choice and heavy/light execution scope are different axes. A child job
may use `react` without becoming an inline light reader, and a phased child
remains available for major implementation work.

### 12.4 Persistent sessions

Do not make a fake persistent thread for loop control. Persistent execution
currently includes:

- waiting for user input;
- thread/session provisioning and callbacks;
- durable cross-turn conversation;
- permission interaction;
- memory/KB injection and compaction;
- incremental message persistence;
- session workspace auto-commit behavior.

Those are lifecycle concerns. A later convergence slice may make one
persistent turn call the shared React kernel, but persistent history,
transport, and session durability remain owned by the session layer.

---

## 13. Project-loop adoption

The runtime feature and the loop control-plane work reinforce each other but
should remain separable.

### 13.1 Scholar / backlog groomer

```text
Input:  ProductDirection + BacklogSnapshot + evidence gaps
Engine: react
Tools:  KB/backlog reads, web/research, optional nested investigators
Output: CandidateDelta
```

The Scholar may add, enrich, merge, refresh, or explicitly make no change. It
does not need a full strategic/tactical implementation plan merely to maintain
candidate quality.

### 13.2 Critic / selector

```text
Input:  complete deterministic BacklogSnapshot + current initiative state
Engine: structured by default
Tools:  none in the primary path; request research when evidence is insufficient
Output: SelectionDecision
```

This is intentionally not ReAct-first. Allowing the Critic to reconstruct a
sample through semantic search reintroduces the coverage problem. When the
snapshot is incomplete, the correct output is `research`, which schedules a
bounded evidence-gathering stage and then re-runs selection on a new snapshot.

### 13.3 Developer

```text
Input:  exact ExecutionMission derived from SelectionDecision
Engine: phase_graph
Tools:  full resolved implementation surface
Output: existing job result plus DeliveryOutcome projection
```

The feature does not force multiple Developers or smaller implementation
tasks. One deep Developer can retain the strategic/tactical machinery that is
useful for long work.

### 13.4 Verification

```text
Input:  mission + attempt + changed artifacts + acceptance criteria
Engine: react
Tools:  read/test/evidence tools; mutation disabled by default
Output: VerificationDisposition
```

Verification returns `accept`, `continue`, `retry`, `blocked`, or `close` with
evidence. The orchestrator applies the outcome state machine.

### 13.5 Campaign scheduling

Campaigns become an optional orchestration decision derived from a validated
selection, not a tool the Critic may or may not call. The first runtime rollout
should prove single-mission control-plane correctness before reintroducing
multi-stage campaign optimization.

---

## 14. Observability

Every runtime invocation should expose a common envelope:

```json
{
  "execution_run_id": "...",
  "engine": "react",
  "engine_version": 1,
  "lifecycle": "job",
  "profile": "investigator",
  "stop_reason": "contract_complete",
  "contract": "candidate_delta.v1",
  "contract_valid": true,
  "llm_calls": 47,
  "tool_calls": 81,
  "compactions": 3,
  "input_tokens": 1234567,
  "output_tokens": 45678,
  "elapsed_ms": 842000,
  "fallback_calls": 0
}
```

Required metrics:

- jobs and stage successes by runtime/role/model;
- execution run id/version and top-level versus nested lifecycle;
- contract-valid rate and repair count;
- stop-reason distribution;
- LLM/tool calls, tokens, time, and cost by runtime;
- context compactions and tokens evicted/summarized;
- deadline/cancellation latency;
- nested-agent depth, fan-out, and parent-attributed spend;
- retry/restart count and repeated-side-effect prevention;
- loop advancement blocked by invalid/missing output;
- outcome value per role/runtime, not just terminal job count.

Unphased calls should carry a nullable phase plus their execution-engine label;
do not falsely report every React call as tactical merely to satisfy existing
audit dimensions. Nested invocations need a stable sub-run id on LLM, tool,
compaction, and usage records while still aggregating spend to the parent job.

The Cockpit should eventually display runtime, output contract, stop reason,
and compaction count in job diagnostics. This is not required for the first
agent-side slice if the audit/API data is already available.

---

## 15. Failure semantics

| Failure | Runtime responsibility | Orchestrator responsibility |
|---|---|---|
| Transient LLM outage | bounded in-process retry; return retryable error/freeze request | pause/backoff/re-dispatch within policy |
| Invalid structured output | recovery + bounded repair; return validation details | do not advance stage; retry/fail/review |
| Tool error | append attributable error result; continue when safe | none unless runtime declares terminal failure |
| Tool timeout | cancel tool, preserve audit, decide whether task can continue | retry job only according to idempotency policy |
| Context threshold | compact and continue | none |
| Cumulative budget exhausted | stop tools; attempt reserved terminal result | map partial/invalid result according to contract policy |
| Wall deadline reached | cancel inner work and synthesize only within reserve | enforce outer deadline and cleanup |
| User/job cancellation | interrupt cancellable work; let unsafe-to-interrupt tools record their result; cascade to nested agents | persist authoritative cancelled state |
| Worker crash | resume from supported checkpoint or restart immutable stage | redispatch and enforce retry ceiling |
| Unsafe resume combination | fail loudly before execution | reject dispatch/configuration |

No runtime should translate a failure into `completed` merely because it can
produce a prose summary.

---

## 16. Backward compatibility and migration

1. Add `ExecutionConfig` to both loader construction paths and the JSON schema.
2. Default missing `execution.engine` to `phase_graph`.
3. Include execution fields in resolved-config serialization and parity tests so
   a new field cannot be silently dropped at construction.
4. Wrap current graph execution in `PhaseGraphRuntime` without changing its
   inputs, checkpoint ids, state, or terminal payload.
5. Keep `AuxiliaryLLM`, persistent sessions, and existing delegation behavior
   unchanged until their explicit migration slices.
6. Reject unknown engine/contract names at load or dispatch.
7. Log the resolved execution engine/profile at job start and attach it to audit
   metadata.
8. Roll back any canary by removing the stage override; the default phase graph
   remains intact.

Existing `phase_settings`, graph checkpoints, delegation-heavy jobs, and
expert configs continue to work. No database migration is required merely to
store the execution default if it lives in resolved/config JSONB. Typed loop
outputs may require their own application schema as part of the separate
control-plane feature.

### 16.1 Mixed-version deployment

An old worker treats an unknown top-level configuration key as `extra` and
would continue into `phase_graph`. That is safe for storage compatibility but
unsafe for a job that requires `structured` or `react`: it would silently run
the wrong engine.

Deployment order:

1. Deploy orchestrator support for accepting, validating, and persisting
   `execution_result`, while no worker emits it.
2. Deploy workers that parse `execution`, advertise supported engine/version
   capabilities in registration/heartbeat, and still default to `phase_graph`.
3. Restrict non-default execution jobs to compatible workers.
4. Enable one read-only structured canary.
5. Expand engine/role overrides only after capability and result gates pass.

In-flight jobs continue using their frozen resolved configuration. Never infer
a new engine from the role name when resuming an old job. Rollback stops
stamping non-default `execution` overrides; runtime-capable workers must remain
available until non-phase checkpoints/jobs have drained or been explicitly
cancelled.

---

## 17. Implementation plan

### Phase 0 — Baseline and invariants

- Capture current phased-job behavior and result payload in regression tests.
- Record current loop role cost/calls and light-subagent stopping behavior.
- Define the runtime/config/output-contract terminology in code comments.
- Decide the v1 runtime ids and fail-loud validation behavior.
- Fix data-driven LLM-slot credential injection and secret-redaction parity,
  including `llm.subagent`, before that role can be selected.

**Exit:** the current behavior is pinned well enough to refactor without an
accidental execution change.

### Phase 1 — Config and runtime seam, no behavior change

- Add `ExecutionConfig` to `AgentConfig`, both loader paths, schema, defaults,
  resolved-config serialization tests, and dataclass-field parity tests.
- Add `RuntimeContext`, `RuntimeResult`, registry, and base protocol.
- Implement `PhaseGraphRuntime` as an adapter over current construction/run
  behavior.
- Select the runtime in `process_job()` before graph-specific setup.
- Route workspace-upgrade rebuild through the same runtime factory.
- Add the shared runtime event/cancellation interface while preserving current
  graph streaming behavior.
- Include runtime metadata in logs/audit.

**Exit:** every existing test remains green; all existing jobs resolve to and
behave as `phase_graph`.

### Phase 2 — Structured runtime and first loop canary

- Implement `StructuredRuntime` using the existing model-factory,
  structured-output method resolution, recovery, timeout, fallback, and audit.
- Add output-contract registry and semantic validation.
- Build deterministic `BacklogSnapshot` input and `SelectionDecision` output.
- Run a Critic selector canary without general tools or workspace mutation.
- Store the validated result authoritatively; prevent loop advance on missing
  or invalid output.

**Exit:** three consecutive canary selection stages return valid decisions,
reference the exact input generation, avoid phase nodes/completion tools, and
advance only through the validated contract.

### Phase 3 — Production ReAct job runtime

- Extract a graph-independent audited tool executor.
- Implement fresh-context LLM/tool iteration.
- Transplant `ContextManager` compaction and summarization; compact and continue.
- Add cumulative tokens, wall deadline, emergency limits, cancellation, and
  terminal reserve.
- Emit progress and cooperative-stop boundaries after every LLM, complete tool
  group, compaction, and checkpoint.
- Add structured terminal completion and semantic validation.
- Define React-compatible tool filtering and phase-control exclusions.
- Initially support restart-from-input for read-only jobs; fail unsafe
  mutating/resume combinations.

**Exit:** a test job can exceed the current light reader's ten turns, compact
at least twice, continue using tools, return a valid typed result, and expose
complete audit/usage data.

### Phase 4 — Scholar and verifier adoption

- Add `CandidateDelta` and `VerificationDisposition` contracts.
- Run loop Scholar/groomer and verifier stages on `react` through job-level
  overrides.
- Keep Developer on `phase_graph`.
- Measure cost, contract validity, backlog quality, and product outcomes against
  the assessment baseline.

**Exit:** role success is determined by typed outputs, and no downstream stage
has to rediscover its upstream instruction through the KB.

### Phase 5 — Shared capable subagent runtime

- Make inline `spawn_subagent` call the shared React engine.
- Replace the single light definition with allowed operator profiles such as
  `reader` and `investigator`.
- Align parent/child deadlines and cumulative budgets.
- Support typed nested results where the parent requests a known contract.
- Preserve fresh context, worktree isolation, no-nesting default, metering, and
  teardown.
- Deprecate the duplicate light runner only after equivalence tests pass.

**Exit:** an investigator subagent can run well beyond ten tool rounds, compact
and continue, terminate within its parent envelope, and return an attributable
result without a full phase graph.

### Phase 6 — Durability and lifecycle convergence

- Add turn-boundary checkpoints for long React jobs.
- Add idempotency support for approved mutating React tools.
- Evaluate making persistent `_execute_turn()` an adapter over the shared React
  kernel while preserving thread callbacks and incremental persistence.
- Allow heavy child jobs to choose `react` or `phase_graph` through their expert
  and job override.
- Remove duplicated loop code only after production parity.

**Exit:** ordinary jobs, nested subagents, and persistent turns reuse the same
core execution semantics where appropriate without sharing lifecycle state.

---

## 18. Test plan

### 18.1 Configuration

- missing runtime -> `phase_graph`;
- valid engine/profile/contract parsing in both loader paths;
- unknown engine/contract fails loudly;
- expert default and job override precedence;
- resolved-config round trip preserves execution-engine configuration;
- secrets remain stripped;
- every model role, including `subagent`, receives credential injection and
  secret-redaction parity;
- dataclass/parser/schema parity catches new fields.

### 18.2 Runtime selection

- phased config builds the current graph exactly once;
- structured/react configs never construct phase checkpointer, todo, archive,
  or transition machinery unless explicitly required by shared setup;
- workspace upgrade/resume re-enters the original runtime;
- reused worker clears previous runtime state.
- incompatible/old worker capability cannot receive a non-default engine job.

### 18.3 Structured runtime

- valid Pydantic response;
- recoverable malformed response;
- semantic-validation failure and repair;
- timeout/fallback behavior;
- cancellation;
- input-too-large preflight;
- invalid result prevents loop advancement;
- idempotent repeated result delivery.

### 18.4 React runtime

- final answer without tools;
- multi-round tool use;
- concurrent same-turn reads;
- per-tool error isolation;
- audited tool gates and category timeouts;
- unknown/phase-only tool rejection;
- compaction occurs and execution continues;
- multiple compactions preserve task/contract and recent tool pairing;
- cumulative budget and wall deadline are distinct;
- cancellation reaches LLM, tools, and nested agents;
- emergency loop ceiling produces an attributable stop;
- terminal schema call and repair;
- every inner LLM call appears in audit/usage.

### 18.5 Lifecycle and subagents

- inline child receives no parent conversation history;
- child deadline fits within parent deadline;
- child spend is parent-attributed and bounded;
- no nesting by default;
- reader workspace is discarded and never merged;
- writable child job keeps independent status and merge behavior;
- parent cancellation reaps child tool/process/worktree resources.

### 18.6 End-to-end canaries

- structured Critic selects only from a complete seeded backlog;
- missing candidate id is rejected;
- React Scholar returns a deduplicated CandidateDelta after compaction;
- phased Developer behavior is unchanged;
- React verifier returns evidence-linked disposition;
- loop cannot rotate past an invalid role result;
- Cockpit/audit can distinguish runtime completion from role-contract success.

---

## 19. Acceptance criteria

The feature is complete when:

1. Existing expert configurations run unchanged on `phase_graph`.
2. Runtime selection is a typed, frozen, auditable config value.
3. A structured job completes without strategic/tactical nodes and returns a
   semantically validated contract.
4. A React job can run beyond ten iterations, compact and continue, and stop by
   completion or an explicit resource reason.
5. React jobs use the same security, audit, usage, cancellation, and tool-timeout
   guarantees as normal worker execution.
6. No unphased agent receives phase-control tools by accident.
7. Loop stages advance only after their output contracts validate.
8. Inline subagents can select an approved deeper profile without becoming
   full phased child jobs.
9. Parent/child deadlines cannot reproduce a child-outlives-parent watchdog
   failure.
10. Persistent sessions remain operational throughout migration.
11. The runtime and contract are visible in job diagnostics and audit data.
12. Rollback to existing behavior is one config override removal, not a data
   migration.

---

## 20. Risks and mitigations

### 20.1 A generic runtime abstraction becomes a framework rewrite

**Mitigation:** start with a small protocol and adapt the phase graph unchanged.
Do not migrate auxiliary or persistent paths until a real consumer proves the
shared seam.

### 20.2 `react` duplicates persistent behavior badly

**Mitigation:** transplant tested context/tool primitives, not the session
lifecycle. Add behavior-parity tests before deleting either implementation.

### 20.3 Runtime config becomes a privilege escalation

**Mitigation:** runtime never grants tools/workspace/network. Server policy
validates overrides and resource widening. Model-facing tools cannot select a
runtime.

### 20.4 Long subagents become unbounded cost sinks

**Mitigation:** cumulative job/parent budgets, wall deadlines, compaction and
emergency ceilings, nesting caps, cancellation, and per-profile policy. "Runs
until done" always means "until done within an explicit resource envelope."

### 20.5 Compaction loses decisive evidence or contract instructions

**Mitigation:** pin immutable task/contract messages, retain recent tool pairs,
store provenance in summaries, test repeated compaction, and permit the agent
to reread source evidence.

### 20.6 Restart repeats mutations

**Mitigation:** v1 React control stages are read-only except for final
authoritative result persistence. Mutating React requires idempotency and
turn-boundary checkpoints before enablement.

### 20.7 Typed output gives false confidence

**Mitigation:** pair schema validation with semantic validation, snapshot ids,
evidence references, and downstream outcome measurement.

### 20.8 Runtime proliferation recreates expert sprawl

**Mitigation:** code-owned registry, three initial engines, independent resource
profiles, and an evidence requirement before adding a new engine.

### 20.9 Workspace initialization remains expensive for lean jobs

**Mitigation:** accept current setup in the first slice to isolate runtime risk;
then integrate with no-workspace/read-only policy after the execution seam is
proven.

### 20.10 Tool metadata assumes strategic/tactical phases

**Mitigation:** define explicit runtime compatibility and fail closed. Do not
treat `phase=None` as universal access.

---

## 21. Decisions and open questions

### Proposed decisions

1. Execution engine is a typed `AgentConfig.execution` field with a trusted
   per-job override; `runtime` remains available for compute placement.
2. The public engine ids are `structured`, `react`, and `phase_graph`.
3. Missing execution defaults to `phase_graph`; unknown values fail loudly.
4. Persistent is a lifecycle, not a runtime engine.
5. Runtime choice never grants capabilities.
6. Workflow stages complete through typed contracts, not optional state-mutating
   model tools.
7. Critic selection starts with `structured`; Scholar and verification use
   `react`; Developer remains `phase_graph`.
8. The current ten-turn light reader becomes a resource profile, not the
   definition of all subagents.
9. A shared production React engine is extracted from existing primitives; a
   fifth independent loop is not introduced.
10. The orchestrator remains authoritative for status and workflow state.

### Open questions

1. Should `execution.profile` initially resolve from code-owned defaults,
   deployment YAML, or an Admin-managed table?
2. Should React terminal structured output be a dedicated final model call,
   a forced terminal tool, or provider-dependent? The default recommendation is
   a dedicated structured call with recovery because optional tools failed as
   workflow boundaries.
3. Which existing audited-tool implementation should become the
   graph-independent executor?
4. Where should runtime checkpoints live: the existing LangGraph checkpointer
   tables, a generic job-runtime store, or job context plus message rows?
5. What minimum duration/mutability threshold requires turn checkpoints rather
   than restart-from-input?
6. Which tools are explicitly compatible with unphased execution, and how is
   that metadata represented without duplicating tool-category configuration?
7. Should `phase_settings` on a non-phase expert warn or fail after the
   migration period?
8. How are partial but valid contract results represented at a deadline?
9. Should inline investigator subagents remain invisible as job rows, or gain a
   lightweight execution/audit identity in the Cockpit?
10. When the persistent turn loop converges on the React kernel, which session
    behaviors remain wrappers and which become runtime hooks?

---

## 22. Source map

Primary implementation surfaces as of 2026-07-14:

- `src/core/loader.py` — `AgentConfig`, limit/context/delegation parsing, both
  config-construction paths, and resolved-config serialization.
- `config/defaults.yaml` and `config/schema.json` — public config defaults and
  validation.
- `src/agent.py` — common job setup, unconditional phase-graph construction,
  run/resume handling, workspace upgrade, LLM/tool initialization, and runtime
  result handoff.
- `src/graph.py` — strategic/tactical runtime, context-manager construction,
  audited tool node, phase/goal/completion flow.
- `src/services/auxiliary.py` — structured chain/agent execution, recovery,
  timeout, fallback, and auxiliary audit behavior.
- `src/tools/delegation/light_runner.py` — fresh-context graph-free ReAct loop
  and current iteration/token stop behavior.
- `src/tools/delegation/spawn_subagent.py` — inline reader lifecycle, parent
  metering, model selection, resource knobs, and result wrapping.
- `src/tools/delegation/reader_env.py` — throwaway reader workspace and tool
  rebinding.
- `src/persistent_graph.py` — persistent tool loop, context injection,
  compaction, callbacks, interruption, and incremental durability.
- `src/api/persistent_app.py` and `src/api/persistent_session.py` — persistent
  thread/session lifecycle and workspace setup.
- `orchestrator/services/project_loops.py` — loop role/job construction and the
  future per-stage runtime override seam.
- `orchestrator/main.py` — job-result persistence, project-loop advancement,
  authoritative status transitions, model credential injection, and the
  completion payload boundary.
- `src/api/orchestrator_client.py` and `src/api/dual_app.py` — completion
  transport, streamed progress, cooperative cancellation, and outer hard-kill
  behavior.
- `orchestrator/database/migrations/app/0053_jobs_runner_kind.sql` — confirms
  `runner_kind` is dispatch/authority provenance and must not be reused for the
  execution engine.

Design trail:

- `docs/issues/loop_control_plane_assessment.md` — why loop control roles need
  deterministic inputs and typed outputs rather than more campaign stages.
- `docs/features/auxiliary.md` — precedent for replacing a full agent subjob
  with structured chain/agent execution.
- `docs/issues/delegation_light_mode_missing.md` — rationale and implementation
  record for the current bounded inline ReAct reader.
- `docs/features/subagent_delegation.md` — durable child-job delegation and
  worktree/merge lifecycle.
- `docs/features/agent_lifecycle.md` and `docs/features/dual_mode_agent.md` —
  worker versus persistent lifecycle architecture.
- `docs/features/pod_runtime.md` — existing use of `runtime` for compute
  placement, motivating the `execution.engine` configuration name.
- `docs/features/no_workspace_agent_mode.md` — workspace policy as a separate
  capability axis.
