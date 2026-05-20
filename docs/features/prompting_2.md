# Prompt & Instruction System Rework

This document captures the design problem, current state, and the chosen direction for reworking the agent's prompt and instruction architecture.

## Background

The original `docs/prompts.md` analysis identified token-level redundancies (~2,000 tokens/call wasted). While valid, those findings focused on efficiency rather than the root problem: **the agent loses its identity and gets lost mid-job**.

The real issue: `instructions.md` was a single blob mixing *who the agent is*, *how it should work*, and *what specific task to accomplish*. It got injected once as a `HumanMessage`, the task part got absorbed into `plan.md`, and then the identity/methodology parts got summarized away during context compaction. The agent forgot *what kind of agent it was supposed to be* — even with cutting-edge high-reasoning models.

Two refactors were completed on `origin/autonomy-focus` to build the delivery infrastructure:

1. **Analysis & Documentation** (`6cdddee`) — Identified all prompt assembly redundancies in `docs/prompts.md`.
2. **Matrix-Based Resolution** (`fbc0c35`) — Replaced the 2-location `PromptResolver` with a 4-level `(expert, model_family)` matrix. Added `PromptMatrixResolver`, `InstructionMatrixResolver`, resolved config snapshots in PostgreSQL, and reorganized experts (coder->developer, researcher->scholar, new critic).

The plumbing for delivering content to the right place at the right time is ready. **The unsolved question was: what are the distinct content types, and where does each one belong?**

---

## The Content Problem

Everything currently lives in `instructions.md` as a flat document. But there are fundamentally different *kinds* of information in there, each with different lifecycles:

| Content Type | Example | When Needed | What Happens Today |
|---|---|---|---|
| **Identity** | "You are a requirements analyst specializing in regulatory compliance" | Every single LLM call | Lost on compaction |
| **Methodology** | "Always cross-reference extracted requirements against source text" | Every call, possibly phase-filtered | Lost on compaction |
| **Task** | "Analyze this RFP and extract requirements into the database" | Persists as context, evolves over time | Absorbed into plan.md, original lost |
| **Phase guidance** | "In document analysis, chunk first, then scan for obligation indicators" | Only during that specific phase | Never existed as separate content |
| **Reference material** | GoBD keywords, GDPR indicators, German legal patterns | On-demand during specific work | Injected every call whether needed or not |
| **Output formats** | JSON schema for `add_requirement`, template structures | When producing output | Injected every call |

The core insight: **the agent needs its identity and methodology re-injected every call, not just once.** The system prompt (`systemprompt.txt` + `strategic.txt`/`tactical.txt`) already does this for the *framework* identity ("you are a phase-alternating agent"), but not for the *expert* identity ("you are a requirements analyst who works like X").

---

## Chosen Direction

### Four-Layer Architecture

```
1. System Prompt  (expert-specific, rebuilt every call, survives everything)
   "Who you are and how you think"

2. Kickoff Message  (HumanMessage, sent once + saved to workspace file)
   "What you need to accomplish"

3. Instruction Files  (workspace, auto-injected by trigger conditions)
   "How to do specific activities well"

4. Task Files  (workspace, referenced from plan, read on demand)
   "What the deliverables look like"
```

### Layer 1: System Prompt — Expert Identity

The system prompt becomes the home for expert identity, persona, and methodology. It is rebuilt every LLM call and survives context compaction by design.

Composition within the system prompt (exact structure deferred until refactor lands and testing can begin):
- Expert persona and principles
- Framework rules (phase alternation, workspace model, meta-cognition)
- Phase directive (strategic or tactical mode)
- Current todos

Expert-specific system prompts are supported via the matrix resolver. System prompt composition details will be worked out after the refactor is testable.

### Layer 2: Kickoff Message — Task Brief

A compact HumanMessage injected once at job start. Contains a general start message — what needs to be accomplished, initial direction, or just "checkout files X and get to work".

This is a new field in the cockpit UI (below the job description, e.g., "Say something to the AI"). It serves as the opening prompt that kicks off the agent's workflow. The content is intentionally flexible — it's a placeholder for now. Future iterations may replace it with a more dynamic first-phase flow (e.g., collaboratively creating the plan with the agent).

Also written to a workspace file (e.g., `task_brief.md`) so the agent can re-read it if context compaction removes the original message.

This replaces the current pattern where the full `instructions.md` (identity + methodology + task) was sent as a HumanMessage and subsequently lost.

### Layer 3: Instruction Files — Triggered Guidance

Instruction files live in the workspace (written at job init, expert-customizable via the matrix system). They provide guidance for specific activities — how to formulate todos, how to conduct reviews, how to write retrospectives, etc.

**Key design principle: instruction files are auto-injected into the conversation based on trigger conditions, not left for the agent to discover.**

#### Existing Implementation: `todo_guide.md`

This pattern is already proven in production with the todo guide. It uses **passive injection** — the system doesn't inject content into the conversation directly, but creates conditions that force the agent to inject it into its own context via `read_file`. The content enters the conversation as a normal tool result.

It works at three levels:

1. **File copied to workspace** at job init (`src/agent.py:1099-1104`) — the guide is available as a workspace file
2. **Strategic todo templates** tell the agent to read it (`strategic_todos_initial.yaml:91`, etc.) — soft guidance
3. **Tool enforces it** — `next_phase_todos` checks `context.was_recently_read("todo_guide.md")` and rejects the call if the agent hasn't read it (`src/tools/core/todo.py:115-118`) — hard enforcement

The enforcement mechanism uses `ToolContext._recent_reads` (a deque of the last 10 `read_file` paths). When the agent calls `read_file`, the path is recorded. When a tool checks `was_recently_read()`, it looks in that deque. The tool gate refuses to execute until the agent has read the guide — so the agent is forced to self-inject the content.

This is the "before tool call" trigger already working: the agent cannot stage todos without first reading the guide.

#### Injection Mechanisms

There are two distinct ways content reaches the agent, depending on the trigger:

| Mechanism | How Content Enters Conversation | When Used |
|---|---|---|
| **Passive injection** (enforce) | Tool gate rejects until agent calls `read_file` itself; content enters as a tool result | `enforce: true` — agent must read before tool executes |
| **Active injection** (system) | System injects content as transient message (like workspace.md) | `enforce: false` — content provided automatically on trigger |

Passive injection (the existing pattern) is preferred when a tool call is the natural trigger point — it keeps the agent in control and the content enters naturally. Active injection is needed for triggers that aren't gated on a tool call (e.g., phase transitions).

#### Generalizing the Pattern

Triggers determine when an instruction file gets injected or required:

| Trigger Type | Example | Injection Mechanism |
|---|---|---|
| **Before tool call** + enforce | Todo guide required before `next_phase_todos` (already implemented) | Passive: tool rejects until agent reads the file |
| **Before tool call** + no enforce | Plan template suggested before `next_phase_todos` | Active: system injects as transient message |
| **On phase transition** | Review methodology injected when entering strategic phase | Active: system injects as transient message |

Properties:
- **Optional to read manually** — the agent can always `read_file` any instruction file
- **Automatically injected or enforced** when the trigger fires — the agent doesn't need to remember
- **Expert-customizable** — each expert can have its own instruction files and trigger mappings
- **Additive** — new instruction files can be added without touching core code or the system prompt

#### Trigger Configuration

Instruction file triggers are defined as an array in the expert's config. Each entry specifies the file, the trigger condition, and whether enforcement is passive (agent must read) or active (system injects).

```yaml
instruction_files:
  - file: instructions/todo_guide.md
    trigger: before_tool:next_phase_todos
    enforce: true   # passive: tool rejects if not read

  - file: instructions/review_guide.md
    trigger: phase:strategic
    enforce: false  # active: system injects on phase transition

  - file: instructions/plan_template.md
    trigger: before_tool:next_phase_todos
    enforce: false  # active: system injects before tool call
```

Experts override by providing their own instruction files and/or trigger mappings in their config directory.

### Layer 4: Task Files — Deliverables & Reference

Files that define the expected output and domain-specific reference material. These live in the workspace and are referenced from `plan.md`, read on demand.

Examples:
- `deliverable.json` — schema/specification for what the agent should produce
- `reference/gobd_keywords.md` — domain knowledge for specific tasks
- `templates/requirement_schema.json` — output format specifications

These are never auto-injected. The agent reads them when it needs them, guided by references in the plan.

---

## What Each Layer Solves

| Problem | Layer | How |
|---|---|---|
| Agent forgets who it is | System Prompt | Expert identity rebuilt every call |
| Task description lost on compaction | Kickoff + File | Brief HumanMessage + `task_brief.md` for re-reading |
| Agent doesn't know how to do activities well | Instruction Files | Auto-injected when the activity trigger fires |
| Agent produces wrong output format | Task Files | `deliverable.json` and templates referenced in plan |
| Can't customize per expert | All layers | Matrix resolver for system prompt; expert-specific instruction files and triggers; expert-specific task templates |
| Can't A/B test different setups | All layers | Swap system prompt, instruction files, or task files independently |

---

## Resolved Decisions

- **Config migration**: Existing `config/` folder backed up; new configs will be created from scratch.
- **Trigger config location**: Lives in the expert's config as an `instruction_files` array (see trigger configuration above).
- **Kickoff message source**: New UI field in cockpit ("say something to the AI"), separate from the job description. Placeholder for now.
- **Initial strategic todos**: Left as-is for now (`strategic_todos_initial.yaml`); will be updated after the new system is testable.
- **Injection mechanisms**: Two types — passive (enforce via `was_recently_read` gate) and active (system injects as transient message). Decided per instruction file entry.

## Open Decisions (Deferred)

### System prompt composition
Exact ordering and structure of expert persona + framework rules + phase directive within the system prompt. To be worked out once the refactor lands and agent testing resumes.

### Active injection implementation
How actively-injected instruction files are delivered at runtime. Likely extends the transient message pattern from `workspace_injection.py`. Details to be worked out during implementation.

### `instructions.md` content mapping
How the content currently in `instructions.md` maps to the four layers. The current file mixes identity, methodology, phase model explanation, tool guidance, and a task placeholder. Each piece needs to be assigned to its new home. This happens during content authoring.

---

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| Matrix-based file resolution | Done | `PromptMatrixResolver`, `InstructionMatrixResolver` on `autonomy-focus` |
| Model-family-aware prompt selection | Done | `detect_model_family()` in loader.py |
| Resolved config snapshots | Done | PostgreSQL JSONB column, prevents config drift on resume |
| Expert reorganization | Done | developer, scholar, critic on `autonomy-focus` |
| Transient injection mechanism | Done | workspace.md pattern in `workspace_injection.py` |
| Expert-specific system prompts | **Not started** | Move expert identity into system prompt |
| Kickoff message rework | **Not started** | Slim down HumanMessage to task brief only, write to file |
| Instruction file trigger system | **Not started** | Config-driven auto-injection based on triggers |
| Task file conventions | **Not started** | `deliverable.json` format, reference file structure |
| Content authoring | **Not started** | Write the actual instruction files for each expert |

---

## Implementation Roadmap

### Phase 0: Merge & Clean Slate

**Goal**: Get the matrix infrastructure onto `main` and prepare for the config rebuild.

- [ ] Merge `autonomy-focus` → `main` (matrix resolver, expert reorg, model-family detection)
- [ ] Verify config backup exists (already done)
- [ ] Remove old `instructions.md` injection path from `src/graph.py` (the HumanMessage that persists in history)
- [ ] Remove old monolithic `config/prompts/instructions.md`

**Result**: Main branch has the matrix plumbing, old instructions path is gone.

### Phase 1: System Prompt — Expert Identity (Layer 1)

**Goal**: Agent stops losing its identity on context compaction.

- [ ] Define expert persona content format (what goes into an expert's system prompt fragment)
- [ ] Create expert system prompt files using matrix resolver paths (e.g., `config/experts/scholar/prompts/system.txt`)
- [ ] Modify `get_phase_system_prompt()` to compose: expert persona + framework rules + phase directive + todos
- [ ] Verify expert identity survives context compaction (it's rebuilt every call by design)

**Result**: Each expert has its own identity in the system prompt. Framework identity + expert identity are both present every LLM call.

### Phase 2: Kickoff Message (Layer 2)

**Goal**: Task brief enters cleanly and survives as a workspace file.

- [ ] Add kickoff message field to config schema (`schema.json`)
- [ ] Add kickoff UI field in cockpit (below description — "Say something to the AI")
- [ ] Rework `src/graph.py` job init to send kickoff as a compact HumanMessage
- [ ] Write kickoff content to `task_brief.md` in workspace at job init
- [ ] Wire orchestrator API to pass kickoff field through to agent

**Result**: The only HumanMessage at job start is the task brief. Agent can re-read `task_brief.md` after compaction.

### Phase 3: Instruction File Triggers (Layer 3)

**Goal**: Config-driven instruction delivery, generalizing the `todo_guide.md` pattern.

- [ ] Add `instruction_files` array to config schema
  ```yaml
  instruction_files:
    - file: instructions/todo_guide.md
      trigger: before_tool:next_phase_todos
      enforce: true
  ```
- [ ] Implement trigger evaluation in the tool execution pipeline
  - **Passive** (`enforce: true`): Generalize `was_recently_read()` gate — any tool can require any file
  - **Active** (`enforce: false`): Extend `workspace_injection.py` to inject instruction file content as transient messages when trigger fires
- [ ] Implement `phase:strategic` / `phase:tactical` triggers (inject on phase transition)
- [ ] Copy instruction files to workspace at job init (like `todo_guide.md` today)
- [ ] Migrate existing hardcoded `todo_guide.md` enforcement to use the config-driven system

**Result**: Adding a new instruction file = add a config entry + write the file. No code changes needed.

### Phase 4: Task Files & Conventions (Layer 4)

**Goal**: Deliverable specs and reference material have a defined structure.

- [ ] Define `deliverable.json` format (or decide it's just a convention, not enforced)
- [ ] Establish workspace directory conventions for task files (`reference/`, `templates/`)
- [ ] Ensure plan.md references task files so the agent knows where to find them

**Result**: Task-specific files have a home and are discoverable via the plan.

### Phase 5: Content Authoring

**Goal**: One expert fully configured as the reference implementation.

- [ ] Pick reference expert (default agent or scholar)
- [ ] Write expert persona for system prompt (Layer 1)
- [ ] Write kickoff template or examples (Layer 2)
- [ ] Write instruction files: todo guide, review guide, plan template, etc. (Layer 3)
- [ ] Write deliverable spec and reference files for a sample task (Layer 4)
- [ ] Map remaining `instructions.md` content to its new layer (anything not yet placed)

**Result**: One expert works end-to-end with the new four-layer system.

### Phase 6: Testing & Validation

**Goal**: Verify the agent maintains coherence across long jobs.

- [ ] Run a multi-phase job with the reference expert
- [ ] Check: does the agent maintain its identity after context compaction?
- [ ] Check: does it find and use instruction files at the right time?
- [ ] Check: does the kickoff message + `task_brief.md` pattern work for task recovery?
- [ ] Compare coherence and drift against the old `instructions.md` setup
- [ ] Iterate on content and trigger timing based on results

**Result**: Validated four-layer system ready for additional experts.

---

### Dependency Graph

```
Phase 0 (merge + clean slate)
  │
  ├──→ Phase 1 (system prompt)
  │       │
  ├──→ Phase 2 (kickoff message)    ← Phases 1 & 2 can run in parallel
  │       │
  │       ▼
  │    Phase 3 (instruction triggers) ← needs config schema from Phase 2
  │       │
  │       ▼
  │    Phase 4 (task files)          ← can also start after Phase 1
  │       │
  └──────►▼
       Phase 5 (content authoring)   ← needs all layers built
          │
          ▼
       Phase 6 (testing)
```
