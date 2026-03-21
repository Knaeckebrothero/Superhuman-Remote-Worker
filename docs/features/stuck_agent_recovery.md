---
tags:
  - feature
  - design
  - reliability
  - agent-loop
aliases:
  - stuck agent recovery
  - loop detection v2
  - agent guardrails
related:
  - "[[prompting]]"
  - "[[project_knowledge_base]]"
  - "[[memory_light]]"
---

# Stuck Agent Recovery — Redesigning Loop Detection & Guardrails

## Problem Statement

The agent can enter stuck states where it burns iterations without making progress. The current guardrails — warning text injected into tool results — are ineffective. The LLM treats them as suggestions, not constraints, and they provide no diagnostic value.

### Current Implementation (`src/graph.py`, `create_audited_tool_node`)

Two mechanisms, both inside the audited tool node:

**1. Loop detection (P8 mitigation)** — Tracks the last 30 tool calls as `(name, args_hash)` tuples. After 3 identical calls (same tool + same arguments), injects a warning into the next `ToolMessage`:

```
⚠ Loop detected: you have called 'kb_list' with the same arguments 3 times recently.
Try a different approach.
```

**2. Strategic phase budget (P6 mitigation)** — Counts total tool calls per strategic phase. After 10 calls, injects a warning every 5 calls:

```
⚠ You have made 50 tool calls in strategic mode. Strategic review should be shorter
than tactical execution. Transition to tactical phase now.
```

Both inject their text by prepending to the first `ToolMessage.content` in the result.

### Why This Doesn't Work

**Observed failure** (job #51): The todo guide (`todo_guide.md`) referenced `kb_list` as an example. The agent created todos requiring `kb_list`. But the job had no knowledge infrastructure, so the tool wasn't loaded. The agent spent 50+ iterations in strategic mode trying to find a tool that didn't exist, ignoring the budget warnings entirely.

**Root causes:**

1. **Warnings don't diagnose the problem.** "You've made too many calls" doesn't tell the agent *why* it's stuck. The agent needs to know "the tool you're looking for doesn't exist — here's what you have."

2. **Warnings don't constrain behavior.** Text prepended to a tool result competes with the actual tool output and the system prompt. The LLM may acknowledge the warning and then proceed to do exactly the same thing. There is no structural enforcement.

3. **The strategic budget measures the wrong thing.** A strategic phase that makes 30 tool calls but completes its review (reads git history, writes retrospective, updates plan, creates todos) is working correctly. A phase that makes 5 calls but none of them advance the work is stuck. Iteration count is not a proxy for progress.

4. **The loop detector only catches exact repetition.** If the agent varies its arguments slightly — `kb_list()` then `kb_list(type="learning")` then `kb_list(tag="research")` — the fingerprint changes even though the behavior pattern is the same: searching for a tool category that isn't available.

5. **"Transition to tactical phase now" is dangerous advice when strategic work isn't done.** If the agent obeys and transitions with broken/incomplete todos, it fails harder in tactical mode.

### Failure Categories

Based on the MAST taxonomy (analysis of 1,642 multi-agent traces, 41-87% failure rate across 7 frameworks — [arXiv:2503.13657](https://arxiv.org/abs/2503.13657)), AgentDebug's modular failure classification ([arXiv:2509.25370](https://arxiv.org/abs/2509.25370)), and Graphectory's anti-pattern catalog (EMNLP 2025 — [arXiv:2512.02393](https://arxiv.org/html/2512.02393)), stuck states fall into distinct categories requiring different responses:

| Category | Signal | Current detection | Correct response |
|---|---|---|---|
| **Tool not found** | Agent calls nonexistent tool | None (ToolNode crashes or returns error) | Return available tools list |
| **Exact loop** | Same (tool, args) repeated 3+ times | Fingerprint detection (working) | Force reflection or mask tool |
| **Semantic loop** | Different args, same futile pattern | Not detected | Tool-category failure tracking |
| **Blocked dependency** | Tool exists but precondition unmet | Not detected | Diagnose precondition, suggest alternative |
| **Strategic drift** | Many calls, no todos created | Budget counter (wrong signal) | Progress-based detection |
| **Reflection paralysis** | Agent ruminates without acting | Not detected | Force action or escalate |

Graphectory formalizes several of these as named anti-patterns: **RepeatedView** (reading the same resource repeatedly), **UnresolvedRetry** (retrying the same failing action), **EditReversion** (writing changes then undoing them), **NoEffectEdit** (writes that don't change content). Their key finding: unresolved trajectories have more back-edges (cycles), longer phase lengths, and more anti-patterns compared to resolved ones.

---

## Proposed Solution: Layered Stuck Detection & Recovery

Replace the current warning-injection approach with a layered system that diagnoses the cause of stuck states and responds structurally rather than with advisory text.

### Layer 0: Root-Cause Prevention

Eliminate the most common stuck triggers before they become loops.

**Tool availability validation.** When the agent calls a tool name that doesn't exist in the bound tool set, return a structured `ToolMessage` instead of crashing:

```
Tool 'kb_list' is not available in this job.
Available tool categories: workspace, core, document, research, citation, coding.
If your current todo requires a tool that isn't available, mark it as blocked
and proceed with alternatives.
```

This is the single highest-impact change. The LangGraph `ToolNode` currently raises on unknown tools — we need to catch this and return a diagnostic message the agent can act on. Note: the error message should remain in context for subsequent turns (both Manus and Anthropic confirm that preserving failed attempts in context helps models avoid repeating the same mistake).

**Error classification at the tool boundary.** Not all tool failures should be retried. Before any retry/loop logic, classify the error:

| Error class | Examples | Action |
|---|---|---|
| **Not found** | Unknown tool name, missing file | Return diagnostic with alternatives, don't retry |
| **Validation** | Bad arguments, schema mismatch | Return what's wrong, don't retry |
| **Precondition** | Tool exists but infra not configured | Explain what's missing, suggest alternative |
| **Transient** | Timeout, connection error, 5xx | Retry with backoff, max 2-3 attempts |
| **Auth/permission** | 401, 403 | Escalate (freeze job), don't retry |

The critical insight from distributed systems: never retry non-retryable failures. Auth errors, validation errors, and tool-not-found will never improve with persistence.

**Conditional template content.** The todo guide and other instruction files should not reference tools that aren't available. Either template them based on the active tool set, or add a preamble listing available tool categories. *(Separate effort, already in progress.)*

### Layer 1: Progress-Based Detection

Replace iteration counting with measurable progress tracking. Measure what the agent is *accomplishing*, not how many calls it's making.

**Formalized metrics** (adapted from Graphectory):

| Metric | Definition | Stuck signal |
|---|---|---|
| **Loop Count (LC)** | Number of repeated `(tool, args_hash)` tuples in current phase | LC > 3 = looping |
| **Average Loop Length (ALL)** | Mean number of actions between repetitions | Short ALL = tight loop, agent not exploring |
| **Artifact Delta** | `git diff --stat` of workspace since last progress checkpoint (or file content hash delta) | Delta = 0 after N calls = no useful work produced |
| **Todo velocity** | `todo_complete` calls per N tool calls | 0 completions after 15+ calls = stuck |
| **Tool diversity** | Count of distinct tool names in last N calls | Diversity = 1-2 after 10+ calls = fixated |
| **Phase tool signal** | Whether `next_phase_todos` or `job_complete` has been called | Strategic phase with 0 phase tools after 20 calls = drifting |

**Stuck heuristic:** If the agent has made `N` tool calls (configurable, default 15) since the last progress signal (todo completion, meaningful file write, phase tool call), it is likely stuck. This replaces the flat "10 calls in strategic mode" counter.

The key difference: 30 tool calls with 4 todo completions = healthy. 10 tool calls with 0 progress signals = stuck. The threshold adapts to what's actually happening and works identically in both strategic and tactical phases.

**Reference baseline:** Manus reports their agents average ~50 tool calls per task. Our agents typically run 5-15 tool calls per todo, 3-7 todos per phase. A phase exceeding 100 calls without proportional todo completions is anomalous.

### Layer 2: Fingerprint-Based Loop Detection (Keep & Improve)

The existing exact-repetition detector is sound in principle. Improvements:

**Keep:** The `(tool_name, args_hash)` fingerprint in a sliding window (deque, maxlen=30). This catches the most obvious failure mode cheaply.

**Add: Result-aware fingerprinting.** Hash `(tool_name, args_hash, result_hash)` — if the agent is calling the same tool with the same args and getting the same result, that's a stronger loop signal than just matching on input. This detects the common case where the agent retries hoping for a different outcome.

**Add: Tool-category failure tracking.** If the agent calls 3+ different tools in the same category (e.g., `kb_list`, `kb_search`, `kb_read`) and all fail, detect the pattern: "you're trying to use knowledge tools but none of them are working." This catches the semantic loop case (varying arguments, same futile strategy) without requiring embedding-based similarity — which would add latency and complexity for a case that's better solved by category grouping.

**Threshold:** Keep at 3 identical calls, but change the response (see Layer 3).

### Layer 3: Structured Response (Replace Warning Injection)

When a stuck state is detected, don't inject warning text into tool results. Instead, take structural action. Three options applied in escalating order:

#### Option A: Forced Reflection Step

Insert a **system message** (not prepended to tool output) that forces the agent to articulate its situation before the next tool call. Based on the Reflexion pattern (Shinn et al., NeurIPS 2023 — [arXiv:2303.11366](https://arxiv.org/abs/2303.11366)) which showed that natural language self-critique stored in context is significantly more effective than scalar signals or warnings.

```
[SYSTEM — STUCK DETECTION]

You have made {N} tool calls without progress. Before your next action, answer:

1. What are you trying to accomplish right now?
2. What have you tried that hasn't worked?
3. What specific obstacle is blocking you?
4. What is a DIFFERENT approach you could take using your available tools?

Available tools: {tool_list}

If you cannot identify a different approach, call `mark_complete` with a note
explaining the blocker, or call `next_phase_todos` to move on with what you have.
```

The reflection is injected as a proper system message — not buried in tool output where it competes with the actual result. The agent must respond to it before the graph routes back to tool execution.

**One-shot reflection guard:** Reflection is allowed exactly once per stuck detection. If the agent's next action after reflection matches the same tool category or fingerprint pattern that triggered detection, immediately escalate to Option B. This prevents reflection paralysis — a documented failure mode where agents enter infinite self-critique loops without acting ([arXiv:2405.06682](https://arxiv.org/abs/2405.06682)).

#### Option B: Tool Masking (Graceful Degradation)

When reflection fails to break the loop, progressively constrain the agent's options.

**Important: mask, don't remove.** Manus's production experience shows that removing tools from the schema mid-run invalidates KV-cache and confuses the model when prior context references those tools. Instead, intercept at the execution layer — keep the tool in the schema but return a structured rejection when called:

```
Tool '{tool_name}' has been temporarily restricted for this phase due to repeated
failures. This tool requires {precondition} which is not available in this job.
Alternative approaches: {alternatives_list}
```

This preserves context coherence while preventing the agent from hammering the same failing path.

**Degradation sequence:**

1. **Mask the specific failing tool** — returns rejection with alternatives on call
2. **Mask the entire failing category** — if multiple tools in the same category have failed
3. **Force phase transition** — if the agent is in strategic mode and has been stuck, inject a system message requiring it to call `next_phase_todos` with whatever it has, or `job_complete` with a partial-completion note
4. **Freeze for human review** — if degradation doesn't resolve it, escalate to Option C

#### Option C: Structured Escalation (Freeze)

When the agent is genuinely stuck and no automated recovery works, freeze the job with actionable context. Use LangGraph's native `interrupt()` mechanism rather than a custom freeze implementation — it integrates with checkpointing automatically and supports structured resume payloads.

**Escalation payload:**

```json
{
  "schema_version": "1.0",
  "reason": "stuck_loop",
  "detection_layer": "progress_stall",
  "phase": "strategic",
  "phase_number": 3,
  "iterations_since_progress": 15,
  "progress_summary": {
    "phases_completed": 2,
    "todos_completed_this_phase": 0,
    "todos_remaining": ["Review knowledge base", "Create initial notes"],
    "artifact_delta": "0 files changed since phase start"
  },
  "stuck_diagnosis": {
    "pattern": "tool_category_failure",
    "repeated_actions": [
      {"tool": "kb_list", "count": 3, "last_error": "tool not available"},
      {"tool": "kb_search", "count": 2, "last_error": "tool not available"}
    ],
    "loop_fingerprint": "kb_*:not_found",
    "reflection_attempted": true,
    "reflection_result": "Agent repeated same tool category after reflection"
  },
  "recovery_attempts": [
    {"strategy": "reflection", "result": "no_change", "timestamp": "..."},
    {"strategy": "tool_mask:kb_list", "result": "agent_tried_kb_search", "timestamp": "..."}
  ],
  "suggested_actions": [
    "Attach knowledge datasources to this job (Neo4j + pgvector knowledge store)",
    "Update todos to use file-based alternatives (write_file for notes, read_file for review)",
    "Resume with: Command(resume={'action': 'skip_todo', 'todo_id': 'todo_4'})"
  ],
  "artifacts": {
    "workspace_path": "workspace/job_<uuid>/",
    "checkpoint_path": "workspace/checkpoints/job_<uuid>.db",
    "last_git_commit": "<hash>"
  }
}
```

This gives the human operator enough context to fix the root cause and resume — they don't need to investigate from scratch.

### Layer 4: Hard Budget Cap (Last Resort)

Keep a hard maximum as the absolute backstop — not for normal operation, but for genuine runaways. This is the seat belt, not the steering wheel.

| Budget | Default | Configurable | Purpose |
|---|---|---|---|
| Max tool calls per phase | 100 | `limits.max_tool_calls_per_phase` | Catch infinite loops |
| Max wall-clock time per phase | 30 min | `limits.max_phase_duration_seconds` | Catch slow infinite loops |
| Max total tool calls per job | 2000 | `limits.max_tool_calls_per_job` | Catch job-level runaways |

When a hard cap is hit: freeze the job immediately with a `budget_exceeded` reason and the full escalation payload. Don't warn. The $47k cautionary tale — two agents in a recursive conversation loop for 11 days, costs spiraling from $127/week to $47,000 — shows why hard caps are non-negotiable ([ZenML: 1,200 Production Deployments](https://www.zenml.io/blog/what-1200-production-deployments-reveal-about-llmops-in-2025)). But they should fire only on genuine runaways, not on legitimately complex work. Calibrate defaults based on observed task distributions, not arbitrary constants.

---

## Response Decision Tree

```
Tool call returns
    │
    ├─ Tool not found?
    │   └─ Return available tools list + keep error in context (Layer 0)
    │       └─ continue
    │
    ├─ Tool error?
    │   ├─ Classify: transient → retry (max 2-3)
    │   ├─ Classify: validation/not-found → return diagnostic, no retry
    │   └─ Classify: auth/permission → freeze immediately
    │
    ├─ Exact loop? (same fingerprint 3x in window)
    │   ├─ First trigger → forced reflection (Layer 3A, one-shot)
    │   ├─ Still looping after reflection → mask tool (Layer 3B)
    │   └─ Still looping after mask → freeze with payload (Layer 3C)
    │
    ├─ No progress for N calls? (Layer 1: artifact delta = 0, todo velocity = 0)
    │   ├─ First trigger → forced reflection (Layer 3A, one-shot)
    │   └─ Still no progress → freeze with payload (Layer 3C)
    │
    ├─ Category failure? (3+ tools in same category all failing)
    │   └─ Mask entire category (Layer 3B) + continue
    │
    └─ Hard cap hit? (Layer 4)
        └─ Freeze immediately with full payload
```

---

## What Gets Removed

- **Strategic phase budget counter** (`_strategic_tool_calls`, `_STRATEGIC_BUDGET_WARNING`). Replaced by progress-based detection which works in both phases and measures the right thing.
- **Warning text injection into ToolMessage content.** Replaced by system messages (for reflection) and structural actions (tool masking, freezing). Tool results should contain tool results, not guardrail warnings competing for the model's attention.

## What Gets Kept

- **Fingerprint-based loop detection** (`_tool_call_history`, deque). The detection logic is sound — only the response changes from warning injection to structural intervention.
- **Audit logging.** Unchanged, still logs all tool calls to MongoDB. Audit data also feeds the progress metrics (Layer 1).

---

## Anti-Patterns to Avoid

Patterns documented as failures in production agent systems that this design must not repeat:

**Don't remove tools from the schema mid-run.** Manus found this invalidates KV-cache and creates model confusion when prior context references undefined tools. Use execution-layer masking instead. ([Manus: Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus))

**Don't sanitize errors from context.** Failed tool calls and error messages are learning signals. If you remove them, the model loses information about what didn't work and is more likely to retry the same approach. Keep errors visible. ([Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents))

**Don't provide few-shot recovery examples.** Manus found that giving models example recovery trajectories causes them to become "brittle mimics, falling into repetitive rhythms" — pattern-matching the example rather than reasoning about the actual error. Use structured reflection prompts instead of recovery templates.

**Don't set hard caps too low.** The OpenAI Agents SDK defaults to 5 turns, which is far too low for research-heavy tasks. Aggressive guardrails cause premature termination of legitimate work. Calibrate caps based on observed task complexity distributions, and use progress-based detection (Layer 1) as the primary signal — hard caps are the last resort, not the first line.

**Don't rely on the agent to terminate itself.** LLM-based stop decisions are probabilistic and unreliable. The system running the agent — not the agent itself — must guarantee termination. The harness enforces limits; the agent operates within them. ([Anthropic: Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents))

**Don't trust agent self-assessment of completion.** Anthropic identifies "false success claims" as a major failure mode — agents declare success despite failures, especially when self-reviewing their own work. Completion checks should use measurable criteria (file exists, word count met, tests pass), not the agent's opinion. This is already partially addressed by the verification phase pattern in `todo_guide.md`.

---

## Implementation Considerations

**LangGraph `interrupt()` for freezing.** Rather than a custom freeze mechanism, use LangGraph's native `interrupt()` function for Option C. It integrates with the checkpointer automatically, preserves full state, and supports structured resume payloads via `Command(resume=value)`. The escalation payload becomes the interrupt value, and operators resume with an action directive.

**Where to implement.** The layered detection lives in `create_audited_tool_node` (replacing the current warning logic), but the responses may need graph-level support:
- Layer 0 (tool-not-found): Override or wrap `ToolNode` to catch unknown tool errors
- Layer 1 (progress tracking): State fields on `UniversalAgentState` tracking last progress timestamp and counters
- Layer 2 (fingerprinting): Stays in `audited_tools` closure, same location as current code
- Layer 3A (reflection): Inject system message into state, route to `execute` node to force LLM response before next tool call
- Layer 3B (tool masking): Runtime filter in `audited_tools` that intercepts masked tool calls before execution
- Layer 3C (freeze): `interrupt()` call from `audited_tools` with structured payload
- Layer 4 (hard caps): State field counters checked in `audited_tools`

**Progress tracking state.** Add to `UniversalAgentState`:
```python
last_progress_tool_call_index: int  # tool call index at last progress signal
stuck_reflection_count: int         # how many reflections triggered this phase
masked_tools: list[str]             # tools masked this phase (reset on phase change)
```

**Observability.** All detection events (stuck triggers, reflections, masks, freezes) should be logged to MongoDB audit trail alongside existing tool call auditing. This provides data to tune thresholds and validate that the system is working.

---

## References

### Academic

- Shinn et al., "Reflexion: Language Agents with Verbal Reinforcement Learning," NeurIPS 2023. [arXiv:2303.11366](https://arxiv.org/abs/2303.11366) — Self-reflection via episodic memory outperforms scalar signals.
- "Where LLM Agents Fail," AgentDebug, Sep 2025. [arXiv:2509.25370](https://arxiv.org/abs/2509.25370) — Modular failure taxonomy, 24% accuracy improvement via root-cause isolation + corrective feedback.
- "Why Do Multi-Agent LLM Systems Fail?" MAST, Mar 2025. [arXiv:2503.13657](https://arxiv.org/abs/2503.13657) — 1,642 execution traces, 41-87% failure rates, looping as dominant failure category.
- "Process-Centric Analysis of Agentic Software Systems," Graphectory, EMNLP 2025. [arXiv:2512.02393](https://arxiv.org/html/2512.02393) — Trajectory graph metrics (Loop Count, Average Loop Length), 9 named anti-patterns (RepeatedView, UnresolvedRetry, EditReversion, NoEffectEdit, etc.).
- "Self-Reflection in LLM Agents: Effects on Problem-Solving Performance," May 2024. [arXiv:2405.06682](https://arxiv.org/abs/2405.06682) — Reflection paralysis risk without iteration limits.
- "PALADIN: Self-Correcting Language Model Agents," ICLR 2026. [arXiv:2509.25238](https://arxiv.org/abs/2509.25238) — Training on failure-rich trajectories with annotated recovery paths: +13.6% recovery rate, +10.2% task success.
- "Budget-Aware Tool-Use Enables Effective Agent Scaling," Nov 2025. [arXiv:2511.17006](https://arxiv.org/abs/2511.17006) — Unified cost metric for token + tool budgets with verification-gated continuation.
- "VeriMAP: Verification-Aware Planning for Multi-Agent Systems," Oct 2025. [arXiv:2510.17109](https://arxiv.org/abs/2510.17109) — Subtask verification functions embedded in plans, verification-gated retry or replan.
- "Truly Self-Improving Agents Require Intrinsic Metacognitive Learning," ICLR 2026. [OpenReview](https://openreview.net/forum?id=4KhDd0Ozqe) — Position paper on metacognitive knowledge, planning, and evaluation.

### Industry

- Anthropic, "Effective Harnesses for Long-Running Agents." [Blog](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) — JSON progress tracking, startup rituals, incremental scope, harness-enforced termination.
- Anthropic, "Writing Tools for Agents." [Blog](https://www.anthropic.com/engineering/writing-tools-for-agents) — Tool namespacing, actionable error messages, "more tools don't always lead to better outcomes."
- Anthropic, "Effective Context Engineering for AI Agents." [Blog](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — Context as finite resource, preserve errors, sub-agent context isolation.
- Manus, "Context Engineering for AI Agents." [Blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) — Tool masking over removal, no few-shot recovery examples, ~50 calls/task baseline.
- Partnership on AI, "Prioritizing Real-Time Failure Detection in AI Agents," Sep 2025. [Report](https://partnershiponai.org/wp-content/uploads/2025/09/agents-real-time-failure-detection.pdf) — Action repetition monitoring, state change metrics, proactive detection.
- ZenML, "What 1,200 Production Deployments Reveal About LLMOps in 2025." [Blog](https://www.zenml.io/blog/what-1200-production-deployments-reveal-about-llmops-in-2025) — $47k runaway loop cautionary tale.

### Framework Documentation

- OpenAI Agents SDK — `max_turns`, tripwire guardrails, tool input/output guardrails. [Docs](https://openai.github.io/openai-agents-python/guardrails/)
- LangGraph — `interrupt()`, `Command(resume=...)`, `@wrap_model_call` for dynamic tool filtering. [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [Dynamic Tool Calling](https://changelog.langchain.com/announcements/dynamic-tool-calling-in-langgraph-agents)
- CrewAI delegation ping-pong — `max_iter` bypass via successful tool invocations, semantic loop detection via embedding similarity. [Analysis](https://azguards.com/technical/the-delegation-ping-pong-breaking-infinite-handoff-loops-in-crewai-hierarchical-topologies/)
