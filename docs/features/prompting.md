---
tags:
  - agent-architecture
  - llm-configuration
  - context-management
  - tool-development
  - planning
---

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
- Available-skills menu — a fenced `name` + `description` listing of the in-scope skills; the agent loads a body on demand via the `use_skill` tool (Slice 2, shipped 2026-06-18; see [[agent_skills]])

Expert-specific system prompts are supported via the matrix resolver. System prompt composition details will be worked out after the refactor is testable.

### Layer 2: Kickoff Message — Task Brief

A compact HumanMessage injected once at job start. Contains a general start message — what needs to be accomplished, initial direction, or just "checkout files X and get to work".

This is a new field in the cockpit UI (below the job description, e.g., "Say something to the AI"). It serves as the opening prompt that kicks off the agent's workflow. The content is intentionally flexible — it's a placeholder for now. Future iterations may replace it with a more dynamic first-phase flow (e.g., collaboratively creating the plan with the agent).

Also written to a workspace file (e.g., `task_brief.md`) so the agent can re-read it if context compaction removes the original message.

This replaces the current pattern where the full `instructions.md` (identity + methodology + task) was sent as a HumanMessage and subsequently lost.

### Layer 3: Instruction Files — Triggered Guidance

Instruction files live in the workspace (written at job init, expert-customizable via the matrix system). They provide guidance for specific activities — how to formulate todos, how to conduct reviews, how to write retrospectives, etc.

**Key design principle: instruction files are auto-injected into the conversation based on trigger conditions, not left for the agent to discover.**

> **Skills — Layer 3 documents ARE skills now (Slice 3 shipped, 2026-06-19; [[agent_skills]]).** The two instruction files below have migrated to **bundled skills** (`todo-guide`, `research-guide`). An `instruction_files` entry now takes **either** `file:` (an arbitrary workspace file, as before) **or** `skill:` (a bundled skill, resolved to `skills/<name>/SKILL.md`) — same `trigger`/`enforce` semantics either way. Their deterministic bindings are **preserved**: `todo-guide` keeps the `before_tool:next_phase_todos` tool-gate, `research-guide` the `phase:tactical` injection — and the gate is now satisfied by **either** `read_file` **or** `use_skill` (both record the same path). The `model_invoked` catalog (Slice 2 — the agent loads a `SKILL.md` body on its **own judgment** from the Layer-1 menu) is the *optional-discovery* sibling of these deterministic bindings; a deterministically-bound skill is filtered out of that menu so it is never also offered as optional. Net: Layer 3 is now **one artifact** (`SKILL.md`), with the binding deciding activation.

#### Existing Implementation: the `todo-guide` skill (formerly `todo_guide.md`)

This pattern is proven in production with the todo guide — since Slice 3 (2026-06-19) the guide is the bundled **`todo-guide` skill**, bound by a `skill:` entry rather than a `file:` one. It uses **passive injection** — the system doesn't inject content directly, but creates conditions that force the agent to inject it into its own context. The content enters the conversation as a normal tool result.

It works at three levels:

1. **Materialized to the workspace** at job init as `skills/todo-guide/SKILL.md` — for a bound skill the body rides the frozen `instructions` blob (flag-independent of `SKILLS_DB_ENABLED`), written by `_deploy_instruction_files`
2. **Strategic todo templates** tell the agent to read `skills/todo-guide/SKILL.md` (`config/templates/strategic_todos_initial.yaml`, etc.) — soft guidance
3. **Tool enforces it** — the `apply_instruction_enforcement` wrapper (`src/tools/registry.py`) checks `context.was_recently_read("skills/todo-guide/SKILL.md")` and rejects `next_phase_todos` until the agent has read it (via `read_file` **or** `use_skill`) — hard enforcement

The enforcement mechanism uses `ToolContext._recent_reads` (a deque of the last 10 read paths). Both `read_file` and `use_skill` call `record_file_read()`, so either satisfies the gate. When a tool checks `was_recently_read()`, it looks in that deque and refuses to execute until the agent has read the guide — so the agent is forced to self-inject the content.

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
  - skill: todo-guide                     # bundled skill → skills/todo-guide/SKILL.md
    trigger: before_tool:next_phase_todos
    enforce: true   # passive: tool rejects until the skill is read (read_file or use_skill)

  - skill: research-guide                 # scholar: injected on tactical-phase entry
    trigger: phase:tactical
    enforce: false  # active: system injects the SKILL.md body on phase transition

  - file: instructions/plan_template.md   # a literal file still works — file: XOR skill:
    trigger: before_tool:next_phase_todos
    enforce: false  # active: system injects before tool call
```

Each entry names **either** a `skill:` (a bundled skill, resolved to `skills/<name>/SKILL.md`) **or** a `file:` (an arbitrary workspace-relative file) — exactly one. Experts override by providing their own `instruction_files` entries (and bundled/authored skills) in their config.

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

## Resolved Decisions (Phase 5)

### Active injection implementation
Resolved: `create_instruction_tool_messages()` in `workspace_injection.py` creates transient AIMessage+ToolMessage pairs (same pattern as workspace.md). Injected in the `execute` node after workspace injection, filtered by `get_phase_instruction_files()`.

### `instructions.md` content mapping

The current `instructions.md` (138 lines) mixes six content types. Here's where each now lives:

| instructions.md Section | Lines | New Home | Status |
|---|---|---|---|
| Identity ("skilled remote worker") | 1-10 | **Layer 1**: `persona.txt` (default agent) | Done — default persona written |
| Phase alternation model | 14-31 | **Layer 1**: `strategic.txt` / `tactical.txt` | Already existed |
| Key files and folders | 33-41 | **Layer 1**: `systemprompt.txt` (Memory Model) + workspace template | Already existed |
| Working principles | 43-71 | **Layer 1**: `systemprompt.txt` (Working Principles + Meta-Cognitive Guardrails) | Already existed |
| Working with source materials | 73-101 | **Layer 3**: the `research-guide` skill (scholar; was `research_guide.md`) | Done — scholar reference |
| Delivering results | 103-132 | **Layer 4**: Strategic todos (deliverable tracking in workspace.md + plan.md) | Done — conventions |
| Task placeholder | 134-137 | **Layer 2**: `task_brief.md` (kickoff message) | Done |

**Conclusion**: Everything in `instructions.md` is now covered by other layers. Phase 0 can safely remove the old injection path without losing content. The file remains on disk for backward compatibility until Phase 0 executes.

---

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| Matrix-based file resolution | Done | `PromptMatrixResolver`, `InstructionMatrixResolver` on `autonomy-focus` |
| Model-family-aware prompt selection | Done | `detect_model_family()` in loader.py |
| Resolved config snapshots | Done | PostgreSQL JSONB column, prevents config drift on resume |
| Expert reorganization | Done | developer, scholar, critic on `autonomy-focus` |
| Transient injection mechanism | Done | workspace.md pattern in `workspace_injection.py` |
| Expert-specific system prompts | **Done** | `persona.txt` per expert, injected via `{expert_identity}` in systemprompt.txt every call |
| Kickoff message rework | **Done** | `kickoff_message` field in API/UI, `task_brief.md` in workspace, HumanMessage includes task brief |
| Instruction file trigger system | **Done** | Config-driven triggers via `instruction_files`, passive enforcement wrappers, phase injection. **Slice 3 (2026-06-19)** extended entries with a `skill:` form (XOR `file:`, → `skills/<name>/SKILL.md`) and migrated `todo-guide`/`research-guide` to bundled skills ([[agent_skills]]) |
| Task file conventions | **Done** | Convention-based: `output/` for deliverables, `reference/` for domain material, Deliverables table in workspace.md and plan.md |
| Content authoring | **Done** | Scholar as reference expert. Default persona written. `research_guide.md` for scholar. `instructions.md` fully mapped to layers. |

---

## Implementation Roadmap

### Phase 0: Clean Up Old Injection Path

**Goal**: Remove the monolithic `instructions.md` injection once Layers 1-2 can replace it.

**Prerequisites**: Matrix infrastructure already exists on `autonomy-focus` (commits `52ad95f`–`fbc0c35`). No merge needed — we build here first, merge to `main` when the full rework is testable.

- [ ] Remove old `instructions.md` injection path from `src/graph.py:341` (the HumanMessage that persists in history)
- [ ] Remove old monolithic `config/prompts/instructions.md`
- [ ] Clean up `src/agent.py` `instructions.md` write/load logic (~15 references)

**Timing**: This runs *after* Phases 1-2 provide replacement content. Listed first for logical ordering but executed last before testing.

**Result**: Old instructions path is gone, new layers handle all content delivery.

### Phase 1: System Prompt — Expert Identity (Layer 1) [DONE]

**Goal**: Agent stops losing its identity on context compaction.

- [x] Define expert persona content format — `persona.txt` with `## Expert Identity`, principles, and core rules
- [x] Create expert persona files: `config/experts/{developer,scholar,critic}/persona.txt`
- [x] Add `persona` to `PromptMatrixResolver.HARDCODED_DEFAULTS` and `prompt_matrix.yaml`
- [x] Add `{expert_identity}` placeholder to `systemprompt.txt`
- [x] Modify `get_phase_system_prompt()` to load persona via matrix resolver and inject into template
- [x] Update `serialize_resolved_config()` to include `persona` in resolved JSONB
- [ ] Verify expert identity survives context compaction (requires a live job run — Phase 6)

**Result**: Each expert has its own identity in the system prompt. Framework identity + expert identity are both present every LLM call. Empty persona for default agents renders cleanly.

### Phase 2: Kickoff Message (Layer 2) [DONE]

**Goal**: Task brief enters cleanly and survives as a workspace file.

- [x] Add `kickoff_message` field to orchestrator `JobCreate` model (`orchestrator/main.py`)
- [x] Pass `kickoff_message` through `context` JSONB → flows to agent via `remaining_context`
- [x] Add `kickoff_message` to cockpit `JobCreateRequest` TypeScript model
- [x] Add "Opening Message" textarea in cockpit form (between description and expert selector)
- [x] Write `task_brief.md` (description + kickoff message) to workspace at job init (`src/agent.py`)
- [x] Rework `init_strategic_todos` HumanMessage to include task brief content first, instructions second (`src/graph.py`)
- [x] Add `task_brief.md` reference in strategic mode guidance so agent knows to re-read after compaction

**Note**: `kickoff_message` is a per-job field (API/UI), not a config schema field — config defines agent behavior, not per-job input. The old `instructions.md` HumanMessage is kept alongside for backward compat until Phase 0 removes it.

**Result**: Task brief enters cleanly, persists as `task_brief.md` in workspace. Agent can re-read it after compaction. Instructions still included for backward compatibility.

### Phase 3: Instruction File Triggers (Layer 3) [DONE]

**Goal**: Config-driven instruction delivery, generalizing the `todo_guide.md` pattern.

- [x] Add `InstructionFileEntry` dataclass to `src/core/loader.py` with `file`, `trigger`, `enforce` fields and computed `trigger_type`/`trigger_target` properties
- [x] Add `instruction_files` field to `AgentConfig` — parsed in both config parsers
- [x] Add `instruction_files` array to `config/defaults.yaml` with `todo_guide.md` entry
- [x] Add `instruction_files` schema to `config/schema.json` with trigger pattern validation
- [x] Add `_instruction_files` field and helper methods to `ToolContext` (`get_enforcement_files`, `check_tool_enforcement`, `get_phase_instruction_files`)
- [x] Implement passive enforcement (`enforce: true`): `apply_instruction_enforcement()` in `registry.py` wraps tool functions with `was_recently_read()` pre-checks
- [x] Wire enforcement into `agent.py` tool loading — called after `apply_description_overrides`
- [x] Implement active injection (`enforce: false`): `create_instruction_tool_messages()` in `workspace_injection.py` creates transient AIMessage+ToolMessage pairs for instruction file content
- [x] Implement `phase:strategic` / `phase:tactical` triggers in `graph.py` execute node — injects instruction file content after workspace.md injection
- [x] Copy instruction files to workspace at job init (generalized loop in `agent.py`)
- [x] Migrate hardcoded `todo_guide.md` enforcement in `todo.py` to fallback-only (active when no `instruction_files` configured)
- [x] Verify serialization roundtrip: `serialize_resolved_config()` → JSONB → `load_config_from_resolved()` preserves `instruction_files`

**Result**: Adding a new instruction file = add a config entry + write the file. No code changes needed.

### Phase 4: Task Files & Conventions (Layer 4) [DONE]

**Goal**: Deliverable specs and reference material have a defined structure.

**Decision**: Convention-based, not enforced. No `deliverable.json` schema — the agent defines deliverables naturally in `plan.md` and tracks them in `workspace.md`.

- [x] Add `output/` and `reference/` to default workspace structure (`config/defaults.yaml`)
- [x] Update `workspace_template.md` with `## Deliverables` tracking table (path, status)
- [x] Update `strategic_todos_initial.yaml`:
  - Todo 1 (EXPLORE): Read `task_brief.md` first, check `reference/` for domain material
  - Todo 2 (PLAN): Start plan.md with a `## Deliverables` section defining expected outputs in `output/`
  - Todo 3 (workspace.md): Populate the Deliverables table from plan.md
- [x] Update `strategic_todos_transition.yaml`:
  - Todo 1 (REVIEW): Check deliverable status against `output/` via `list_files`
  - Todo 2 (REFLECT): Update Deliverables table status in workspace.md
  - Todo 3 (ADAPT): Cross-check phases against plan's Deliverables section
  - Todo 4 (PLAN OR COMPLETE): Check workspace.md Deliverables table for completion
- [x] Update `strategic_todos_resume.yaml`: Reference `task_brief.md` alongside `instructions.md`
- [x] Update `config/schema.json` with directory convention documentation

**Conventions established**:
- `output/` — All agent deliverables go here. `job_complete` lists paths from this directory.
- `reference/` — Domain-specific reference material (keywords, patterns, source data). Experts can pre-populate via `workspace.initial_files` config.
- `plan.md ## Deliverables` — Defines expected outputs with format/quality criteria.
- `workspace.md ## Deliverables` — Compact tracking table (survives context compaction).

**Result**: Task-specific files have a home and are discoverable via the plan.

### Phase 5: Content Authoring [DONE]

**Goal**: One expert fully configured as the reference implementation.

**Reference expert**: Scholar — most general-purpose, exercises the most tools (research, citations, documents).

- [x] Pick reference expert: **Scholar**
- [x] Write default persona for non-expert agents (`config/prompts/persona.txt`) — "generalist remote worker" identity extracted from `instructions.md`
- [x] Expert personas already written in Phase 1 (developer, scholar, critic)
- [x] Kickoff message: runtime field in UI, not a static template — no file needed
- [x] Write `research_guide.md` for scholar (`config/experts/scholar/research_guide.md`) — research methodology, tool usage, citation requirements, output conventions
- [x] Add `instruction_files` to scholar config — `todo_guide.md` (passive, inherited) + `research_guide.md` (active, tactical phase trigger)
- [x] Add `reference/` to scholar workspace structure
- [x] Deliverable specs: convention-based per Phase 4 — scholar already has `output/ideas/` and `output/experiments/`
- [x] Map all `instructions.md` content to four layers (see Content Mapping table below)
- [x] Verify FileResolver finds scholar instruction files in expert directory
- [x] Verify config loads and serializes correctly

**Content mapping conclusion**: Everything in `instructions.md` is now covered by other layers. Phase 0 can safely remove the old injection path.

**Result**: Scholar works end-to-end with the four-layer system. Default agent has a proper persona.

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
Phase 1 (system prompt)  ──┐
       │                   │
Phase 2 (kickoff message)  │  ← Phases 1 & 2 can run in parallel
       │                   │
       ▼                   │
Phase 3 (instruction triggers) ← needs config schema from Phase 2
       │                   │
       ▼                   │
Phase 4 (task files)       │  ← can also start after Phase 1
       │                   │
       ▼                   │
Phase 0 (cleanup old path) ←── deferred until Layers 1-2 replace it
       │
       ▼
Phase 5 (content authoring)   ← needs all layers built
       │
       ▼
Phase 6 (testing)
       │
       ▼
Merge autonomy-focus → main
```

## Related

- [[context_management]]
- [[citation_engine_roadmap]]
- [[cloud_workspace]]
- [[advanced_job_configuration]]
- [[tool_issues]]
- [[obsidian]]
