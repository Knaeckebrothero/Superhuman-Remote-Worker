# Agent Architecture Improvements

Improvements derived from the research report:
**"The Temporal Horizon: Engineering Long-Running Autonomous AI Agents for Durable Execution"**
(see `docs/Researching Long-Running Autonomous Agents.pdf`)

---

## High Priority

### 1. Prompt Caching Optimization
**Report ref: Section 8.1 — Token Economics & Optimization**

Place static context (system instructions, tool docs) at the **start** of the prompt so providers (Anthropic, OpenAI) can cache attention states across loop iterations. This can reduce input token costs by up to 90% and significantly cut latency.

**Action:** Verify message ordering in `src/core/workspace_injection.py` — ensure static content precedes dynamic content (tool results, conversation history). The transient `workspace.md` injection should come after cached static segments.

**Files:** `src/core/workspace_injection.py`, `src/graph.py`

---

### 2. Verification Gates on Job Completion
**Report ref: Section 3.4 — "Definition of Done" Protocol**

Structurally prevent `job_complete` from succeeding unless a verification tool was invoked in the current phase. This addresses "Premature Completion" — where the agent declares success without validating its work.

**Action:** Add a check in `job_complete` (or in the graph's transition logic) that inspects recent tool calls for verification actions (e.g., `validate_schema_compliance`, `execute_cypher_query` for read-back, or test commands). Reject completion if no verification was performed.

**Files:** `src/tools/registry.py`, `src/core/phase.py`, `src/graph.py`

---

### 3. Neurosymbolic Sandwich Pattern for Compliance
**Report ref: Section 6.4 — Neurosymbolic Guardrails**

For GoBD/GDPR compliance decisions, insert a deterministic rule engine between the LLM's plan and its execution:

1. **Neural (Planner):** LLM proposes an action (e.g., "mark requirement as GoBD-compliant")
2. **Symbolic (Verifier):** Rule engine validates against hard constraints (e.g., retention period >= 10 years, audit trail present)
3. **Neural (Executor):** If verified, LLM executes the action

This guarantees compliance decisions cannot be hallucinated regardless of model confidence.

**Action:** Define a constraint rule set for GoBD/GDPR requirements. Implement as a pre-execution check on graph mutation tools (`execute_cypher_query` when writing compliance nodes/edges).

**Files:** `src/tools/` (graph tools), new rules module

---

## Medium Priority

### 4. Tool Output Pruning During Compaction
**Report ref: Section 3.3 — Context Compaction and "Recency Bias"**

Current compaction keeps the 10 most recent tool results verbatim and discards older ones. The report suggests replacing older tool outputs with LLM-generated semantic summaries before discarding (e.g., `"Read data.csv → 500 rows, columns: ID, Name, Status"`). This preserves more context within the same token budget.

**Action:** During compaction in `ContextManager`, before discarding tool results beyond the recent window, generate one-line summaries and keep those as lightweight messages.

**Files:** `src/core/context.py`

---

### 5. Cross-Job Learning via Nested Learning Loop
**Report ref: Section 6.2 — Nested Learning & Continual Improvement**

Strategic phase retrospectives are written to `archive/` but not fed back into future jobs. Implement an **outer learning loop**:

- After job completion, a "Reflector" step mines the retrospectives for recurring patterns
- Distill findings into persistent rules (e.g., "OPTIONAL MATCH required for this schema pattern")
- Inject these rules into future jobs' system prompts or `workspace.md` templates

This creates a self-improving agent that gets better at its domain over time.

**Action:** Add a post-job hook that analyzes `archive/phase_*_retrospective.md` files and appends learned rules to a persistent `config/learned_rules.md` (or similar). Inject this file alongside `workspace.md`.

**Files:** `src/agent.py` (post-job), `src/core/workspace_injection.py`, `config/templates/`

---

### 6. Failed Phase Commit-as-Checkpoint
**Report ref: Section 3.1 — The "Initializer" and "Worker" Pattern**

Git auto-commits happen on todo completion, but failed tactical phases may leave uncommitted work. Ensure every phase boundary (including failures) produces a git commit or tag so damage can be precisely scoped and reverted during the next strategic review.

**Action:** In phase transition logic, trigger a git commit (with a descriptive message like `"phase N failed — partial work"`) even when a phase ends due to error or context overflow.

**Files:** `src/core/phase.py`, `src/managers/git_manager.py`

---

## Lower Priority / Future Consideration

### 7. SiriuS-Style Experience Library
**Report ref: Section 6.3 — Self-Improving Systems**

Mine successful job trajectories from the MongoDB audit trail to build an "Experience Library." Failed jobs get analyzed by a Critic agent, corrected trajectories are re-simulated, and successful patterns are preserved for future reference.

**Prerequisite:** Requires `MONGODB_URL` and sufficient historical job data.

---

### 8. Structured Observability / Tracing
**Report ref: Section 8.2 — Observability with LangSmith & AgentCore**

Add structured tracing where every LLM call, tool invocation, and outcome is linked in a trace tree. This enables diagnosing multi-phase failures by pinpointing exactly where the agent diverged (bad retrieval vs. hallucination vs. tool failure).

**Options:** LangSmith integration, or a lightweight custom trace format in the existing MongoDB audit trail.

---

### 9. Agent2Agent (A2A) Protocol
**Report ref: Section 5.2 — Agent2Agent Protocol**

If the system evolves to multi-framework orchestration (e.g., LangGraph agents delegating to specialized agents from other frameworks), Google's A2A protocol provides capability discovery via "Agent Cards" and cross-vendor task delegation. Not needed for the current single-framework setup.
