# Prompt Architecture: Analysis and Refactoring Plan

This document analyzes the current prompt assembly system, identifies issues, and proposes a refactoring plan.

---

## 1. Current Architecture

### Prompt Assembly Pipeline

The agent receives prompts assembled from multiple layered sources:

| Layer | Source | Injected Via | Lifetime |
|-------|--------|--------------|----------|
| System Prompt | `systemprompt.txt` + `strategic.txt`/`tactical.txt` | `get_phase_system_prompt()` | Rebuilt every LLM call |
| Workspace Memory | `workspace.md` | Transient synthetic tool result (`workspace_injection.py`) | Rebuilt every LLM call, survives compaction |
| Current Todos | `TodoManager.format_for_display()` | `{todos_content}` placeholder in system prompt | Rebuilt every LLM call |
| Task Instructions | `instructions.md` | First HumanMessage (persists in history) | Persists until context compaction |
| Tool Schemas | LangGraph tool binding | Automatic | Always present |

### Instruction Hierarchy (highest to lowest)

1. System prompt (`systemprompt.txt`)
2. Phase directive (`strategic.txt` / `tactical.txt`)
3. `workspace.md` (persistent memory)
4. `instructions.md` (expert instructions)
5. Tool results and conversation history

### Prompt File Locations

**Shared** (`config/`):

| File | Purpose |
|------|---------|
| `prompt_matrix.yaml` | Base prompt matrix mapping `(model_family, prompt_type)` → filename. Consulted as levels 3-4 of the resolution chain. |
| `prompts/systemprompt.txt` | Base template. Memory model, instruction hierarchy, working principles, meta-cognitive guardrails. Placeholders: `{oss_reasoning_level}` (auto-resolved via `reasoning_method`), `{agent_display_name}`, `{prompt_content}`, `{todos_content}` |
| `prompts/strategic.txt` | Generic strategic phase directive. Review-reflect-adapt cycle, phase sizing, workspace compaction. |
| `prompts/tactical.txt` | Generic tactical phase directive. Tunnel vision, per-todo workflow, atomicity. |
| `prompts/instructions.md` | Generalist "Remote Worker" identity. Phase alternation model, tool usage, working principles, citation guidance. |
| `prompts/summarization_prompt.txt` | Context compaction prompt for summarizing conversation history. |

**Per-expert** (`config/experts/<name>/`):

| Expert | `config.yaml` | `prompt_matrix.yaml` | `instruction_matrix.yaml` | `instructions.md` | `strategic.txt` | `tactical.txt` |
|--------|:-:|:-:|:-:|:-:|:-:|:-:|
| Critic | custom | custom | custom | custom | **shared** | **shared** |
| Developer | custom | custom | custom | custom | **custom** | **custom** |
| Scholar | custom | custom | custom | custom | **shared** | **shared** |

The system uses two parallel matrix resolvers, both inheriting from `MatrixResolver` (`src/core/loader.py`):

**`PromptMatrixResolver`** — resolves prompt types (`systemprompt`, `strategic`, `tactical`, `summarization`) to filenames via `prompt_matrix.yaml`. File search: expert directory → `config/prompts/`.

**`InstructionMatrixResolver`** — resolves instruction types (`instructions`, `strategic_todos_initial`, `strategic_todos_transition`, `strategic_todos_resume`, `workspace_template`, `todo_guide`) to filenames via `instruction_matrix.yaml`. File search: expert directory → `config/templates/`.

Both use the same 4-level resolution chain:

1. Expert matrix → model-specific key → type
2. Expert matrix → `"default"` key → type
3. Base matrix → model-specific key → type
4. Base matrix → `"default"` key → type

Once the filename is determined, `FileResolver` (base class, aliased as `PromptResolver`) locates the actual file — checking the expert directory first, then falling back to the framework directory. This means any expert can override any file by placing a same-named file in its directory, and model-specific variants can be configured via matrix YAML files without code changes.

**Resolved Config JSONB**: On first run, `serialize_resolved_config()` captures the fully resolved config (agent config + all prompt text + all instruction text) and stores it in the `resolved_config` JSONB column on the jobs table. On resume, `load_config_from_resolved()` reconstructs the config from this snapshot, bypassing disk resolution entirely. This prevents config drift when files change between runs.

### How System Prompt Is Assembled

`get_phase_system_prompt(config, is_strategic, phase_number, todos_content, model)` in `loader.py`:

1. Detect model family from `model` via `detect_model_family()` (defaults to `"default"`)
2. Create `PromptMatrixResolver` with the config's deployment dir and model family
3. Load base template via `load_base_system_prompt(resolver)` → resolves `"systemprompt"` type
4. Load phase component via `load_phase_component(is_strategic, resolver)` → resolves `"strategic"` or `"tactical"` type
5. Render `{phase_number}` in the phase component
6. Inject rendered phase component into `{prompt_content}` in base template
7. Render remaining placeholders (`{todos_content}`, `{agent_display_name}`, `{oss_reasoning_level}` — only populated when `reasoning_method == "prompt"`, otherwise stripped)

Similarly, `load_summarization_prompt(config, model)` creates its own `PromptMatrixResolver` internally. `load_instructions(config, model)` uses `InstructionMatrixResolver` instead, resolving via `instruction_matrix.yaml` and `config/templates/`.

### How instructions.md Is Used

`instructions.md` is loaded at job initialization and placed into the workspace directory. It's also injected as the first `HumanMessage` in the conversation. It persists in conversation history until context compaction summarizes it away.

---

## 2. Identified Issues

### Issue A: Expert Phase Prompt Inconsistency

**Problem**: Developer has custom `strategic.txt` and `tactical.txt`, but Critic and Scholar use the shared generic ones — even though their workflows are fundamentally different.

| Expert | Strategic workflow | Tactical workflow | Has custom phase prompts? |
|--------|-------------------|-------------------|:--:|
| Developer | Review Claude Code results, create delegation-ready todos | Delegate to Claude Code, verify via git, iterate | Yes |
| Critic | Prioritize review queue, plan which review modes to use | Read code, run tests, check evidence, write reports | No |
| Scholar | Assess exploration modes, plan next discovery batch | Explore, write idea artifacts, run experiments | No |

The shared `strategic.txt` talks about generic git-based review and workspace compaction — useful universal practices, but it misses expert-specific concerns like:
- Critic: review queue prioritization, severity classification planning, cross-job review scope
- Scholar: exploration mode selection, idea volume tracking, dead-end avoidance

**Impact**: Critic and Scholar get phase guidance that doesn't match their actual workflow. Their expert-specific phase guidance lives in `instructions.md` instead, which is *lower priority* in the instruction hierarchy.

### Issue B: Overlap Between instructions.md and Phase Prompts

**Problem**: Both Critic and Scholar `instructions.md` files contain "How to use Strategic vs Tactical Phases" sections that essentially duplicate what custom `strategic.txt`/`tactical.txt` would provide.

- `config/experts/critic/instructions.md` lines 199-221: "How to Use Strategic vs Tactical Phases"
- `config/experts/scholar/instructions.md` lines 122-134: "How to Use Strategic vs Tactical Phases"

These sections describe phase-specific behavior, but they're delivered via `instructions.md` (priority 4) instead of phase prompts (priority 2). When the shared `strategic.txt` (priority 2) says "review with git tools" and `instructions.md` (priority 4) says "prioritize the review queue," the hierarchy says the generic phase prompt wins.

### Issue C: instructions.md Persists in Conversation History

**Problem**: The full `instructions.md` is added as a `HumanMessage` at job init and persists in conversation history for all iterations:

- Phase-specific sections aren't needed during other phases
- Tool category descriptions duplicate LangGraph tool schemas
- Reference material (templates, keyword lists) could be read on-demand
- After context compaction, instructions.md gets summarized (lossy) rather than refreshed

**Impact**: ~1,000-2,000 tokens per request that could be avoided, plus information loss during compaction.

### Issue D: workspace.md Duplication

**Problem**: The first strategic todo asks the agent to "populate workspace.md with an overview of the environment, available tools, and any existing context." This causes the agent to copy content from instructions.md into workspace.md. Then both are sent to the LLM.

**Impact**: ~500+ tokens of pure duplication per request.

### Issue E: Tool Information Appears Multiple Times

**Problem**: Tool information is present in three places:
1. `instructions.md` — tool category descriptions and usage guidance
2. `tools/*.md` — auto-generated per-tool documentation files in workspace
3. LangGraph — full tool schemas with parameters (always present)

**Impact**: ~300 tokens wasted per request.

### Issue F: No Model-Aware Prompting

> **Status: Infrastructure implemented.** The `PromptMatrixResolver` and `detect_model_family()` are now the sole prompt resolution mechanism. All loading functions (`get_phase_system_prompt`, `load_instructions`, `load_summarization_prompt`) create a matrix resolver internally from the model name. The 4-level fallback chain (expert model-specific → expert default → base model-specific → base default) is fully operational. What remains is authoring model-specific prompt variants in `prompt_matrix.yaml` files and the corresponding prompt files.

**Problem**: Prompts are written assuming a specific model behavior (currently: reasoning-capable OSS models via vLLM), but experts can be configured to use any model via YAML. Swapping an expert's model to a different family changes how it interprets the same prompt — and the prompt system has no way to adapt.

The `{oss_reasoning_level}` placeholder in `systemprompt.txt` and `summarization_prompt.txt` is now conditionally rendered via `detect_reasoning_method()`. It is only populated when `reasoning_method == "prompt"` (auto-detected for `gpt-oss` models); for all other model families (Claude, Gemini, GPT-4o, o-series, etc.) the `Reasoning:` line is stripped entirely. The `reasoning_method` config field (`"prompt"`, `"api"`, `"none"`, or `null` for auto-detect) can be set in `llm:` or per-phase overrides. Beyond this, the prompt matrix system provides infrastructure to serve different prompt files per model family — but no model-specific variants have been authored yet. All families currently resolve to the same default filenames.

**Current model assignments:**

| Expert | Model | Type |
|--------|-------|------|
| Developer | `gpt-5.2` | Native reasoning (OpenAI) |
| Critic | `openai/gpt-oss-120b` (default) | OSS reasoning (vLLM) |
| Scholar | `openai/gpt-oss-120b` (default) | OSS reasoning (vLLM) |
| General Secretary | `openai/gpt-oss-120b` (default) | OSS reasoning (vLLM) |

**Model dimensions that affect prompting:**

| Dimension | Examples | Prompt Impact |
|-----------|----------|---------------|
| Reasoning vs chat | o1/o3/gpt-5/DeepSeek R1 vs gpt-4o/Claude Sonnet/Gemini | Reasoning models don't need step-by-step hand-holding; chat models benefit from explicit workflow breakdowns |
| Strong vs weak | Opus 4.6/gpt-5.2 vs Llama 8B/Haiku | Strong models handle dense 3000-token prompts; weak models get confused and need shorter, simpler instructions |
| Tool calling style | Models with native parallel tool calls vs sequential-only | `parallel_tool_calls` config handles the API side, but prompts could also reinforce behavior |
| System prompt adherence | Claude (very high priority) vs some OSS models (partially ignore) | Affects how much to put in system prompt vs conversation history |

**Impact**: When an expert is swapped to a significantly different model (e.g., Scholar on Claude Sonnet instead of gpt-oss-120b), the prompt style may be suboptimal — too verbose for strong reasoners, too complex for weak models, or structured in a way the model doesn't handle well.

**Implemented approach — Prompt Matrix (combines Options 1 & 2 from original analysis):**

The `PromptMatrixResolver` maps `(model_family, prompt_type)` to filenames via `prompt_matrix.yaml`. `detect_model_family()` auto-detects the family from the model name (e.g., `"claude-opus"`, `"deepseek"`, `"gpt-4o"`, `"default"`). This enables model-specific prompt files without code changes — just add entries to the matrix and create the corresponding files.

Example `config/prompt_matrix.yaml`:
```yaml
default:
  systemprompt: systemprompt.txt
  strategic: strategic.txt
  tactical: tactical.txt
claude-opus:
  systemprompt: systemprompt_claude.txt  # shorter, less hand-holding
deepseek:
  strategic: strategic_reasoning.txt     # no CoT instructions
```

**Remaining work**: Author model-specific prompt variants for key families (claude, deepseek, gpt-4o) and populate the matrix files. The infrastructure is ready.

### Issue G: Tool Descriptions Are Model-Agnostic

**Problem**: Different model families need fundamentally different tool description styles, but the system serves the same descriptions to every model. Research and vendor documentation confirm this is a real performance dimension — not just a theoretical concern.

**What the vendors say:**

- **Anthropic (Claude)**: Officially recommends *"extremely detailed descriptions"* — at least 3-4 sentences per tool, covering what it does, when to use it, parameter meanings, and caveats. Claude models are trained to leverage rich tool metadata. They've also added `input_examples` (beta) for complex tools.
- **OpenAI (GPT-5, o-series)**: Strong reasoning models can infer tool usage from minimal descriptions. Good naming + clean JSON Schema is often sufficient.
- **Small/OSS models**: Research from Microsoft and Berkeley shows SLMs *"struggle significantly with adhering to the given output format"* regardless of description detail. Shorter, focused descriptions with fewer tools loaded actually **improve** accuracy for weak models.

**How model class affects tool description needs:**

| Aspect | Strong Reasoner | Chat Model | Small/Weak Model |
|--------|----------------|------------|------------------|
| Description length | Concise OK | Medium detail (3-4 sentences) | Short + focused |
| Parameter descriptions | Schema sufficient | Add usage context | Minimal, clear types only |
| Tool count tolerance | 30-50 tools fine | 20-30 optimal | 5-15 max before accuracy degrades |
| `input_examples` | Rarely needed | Helpful for complex tools | Critical for format compliance |
| Parallel tool prompting | Minimal prompting needed | Moderate reinforcement | Explicit prompting or skip entirely |

**How the system currently handles this:**

The `DescriptionManager` (`src/tools/description_manager.py`) implements a two-tier system:
- **Core tools** (workspace, core): Full docstrings (2-8 lines) bound directly to LLM
- **Domain tools** (research, citation, etc.): Short 1-line `short_description` bound to LLM, with `defer_to_workspace: True` — full docs written to `workspace/tools/*.md` for on-demand reading

This is a good architecture, but it's **model-agnostic** — the same tier and same descriptions go to every model. The only model-specific logic is skipping `parallel_tool_calls` for OpenAI o-series models in `agent.py`.

**Token cost**: Tool definitions are part of context on every LLM call. Studies show 58 tools with full descriptions can consume ~55k tokens — more than half of many models' context windows. The `defer_to_workspace` pattern already mitigates this, but a weak model with 30+ tools may still struggle with tool selection accuracy even with short descriptions.

**Potential approaches:**

1. **Model-class-aware description tiers** — Extend `apply_description_overrides()` to accept a model class parameter. Strong reasoners get the current short descriptions; chat models get medium descriptions; weak models get minimal descriptions with fewer tools loaded. Builds on the existing `defer_to_workspace` architecture.

2. **Dynamic tool loading** — Adopt the "Tool Search Tool" pattern (used by Anthropic's agent SDK): instead of binding all tools upfront, provide a meta-tool that discovers relevant tools on demand. The agent only sees tools it actually needs for the current task. This would dramatically reduce tool count for weak models.

3. **Description templates per model class** — Store multiple description variants in `TOOL_REGISTRY` (e.g., `description`, `description_short`, `description_detailed`). The `DescriptionManager` selects the appropriate variant based on model class.

**Recommendation**: Option 1 is the pragmatic first step — it extends existing infrastructure (`apply_description_overrides` + `defer_to_workspace`) with model-class awareness. Option 2 (dynamic tool loading) is a more ambitious architectural change that would benefit all model classes but requires significant rework of the tool binding pipeline.

**Note**: This issue is closely related to Issue F (model-aware prompting). The model class detection proposed in Issue F would directly enable the tool description adaptation proposed here. These should be implemented together.

---

## 3. Proposed Refactoring

### Recommended Approach: Per-Expert Phase Prompts (Option A)

Give every expert custom `strategic.txt` and `tactical.txt`. This follows the pattern Developer already uses successfully. The shared prompts remain as the General Secretary / generalist defaults.

#### Target State: File Structure

```
config/
  prompts/                          # Shared defaults (used by General Secretary)
    systemprompt.txt                # Universal base template (unchanged)
    strategic.txt                   # Generic strategic phase prompt
    tactical.txt                    # Generic tactical phase prompt
    instructions.md                 # Generalist identity + working guidance
    summarization_prompt.txt        # Context compaction (unchanged)

  experts/
    critic/
      config.yaml                   # Tool selection, LLM settings
      instructions.md               # Identity + methodology (no phase overlap)
      strategic.txt                 # Critic-specific strategic phase prompt
      tactical.txt                  # Critic-specific tactical phase prompt

    developer/
      config.yaml
      instructions.md               # Identity + methodology (already clean)
      strategic.txt                 # Already exists
      tactical.txt                  # Already exists

    scholar/
      config.yaml
      instructions.md               # Identity + methodology (no phase overlap)
      strategic.txt                 # Scholar-specific strategic phase prompt
      tactical.txt                  # Scholar-specific tactical phase prompt
```

#### What Each File Should Contain

**`systemprompt.txt`** (shared, unchanged):
- Memory model, instruction hierarchy, working principles, meta-cognitive guardrails
- Universal across all experts — no changes needed

**`strategic.txt`** (per-expert):
- Expert-specific strategic phase workflow
- What to review, how to reflect, how to plan next phase
- Phase sizing guidance specific to the expert's work
- What NOT to do in strategic phase

**`tactical.txt`** (per-expert):
- Expert-specific tactical phase workflow
- Per-todo execution pattern for this expert's domain
- Expert-specific constraints and anti-patterns
- When-stuck guidance specific to the domain

**`instructions.md`** (per-expert):
- Expert identity and role description
- Domain methodology and modes (review modes for Critic, exploration modes for Scholar)
- Output formats and templates (review report format, idea artifact format)
- Anti-patterns
- **NO** "How to use Strategic vs Tactical Phases" sections (moved to phase prompts)
- **NO** tool category listings (redundant with LangGraph schemas)

### Content Migration Plan

#### Critic

**New `strategic.txt`** — extract from current `instructions.md` lines 199-210 + add:
- Review queue assessment (what needs reviewing, priority)
- Mode selection (code review vs proposal review vs audit vs test execution)
- Workspace memory guidance (verdicts issued, recurring issues, test infrastructure)
- Adapted review-reflect-adapt cycle specific to the Critic's concerns

**New `tactical.txt`** — extract review execution guidance:
- Per-review-todo workflow (read → investigate → verify evidence → write report)
- Evidence collection discipline
- Severity classification during execution
- Don't-fix-it-yourself constraint

**Trim `instructions.md`** — remove:
- "How to Use Strategic vs Tactical Phases" section (lines 199-221)
- Any tool listings that duplicate LangGraph schemas

#### Scholar

**New `strategic.txt`** — extract from current `instructions.md` lines 122-134 + add:
- Exploration mode assessment (which modes are productive for this job)
- Idea volume tracking (how many written, dead ends documented)
- Discovery planning (which areas, which questions, which modes)
- Workspace memory guidance (findings, idea index, dead ends)

**New `tactical.txt`** — extract exploration execution guidance:
- Per-exploration-todo workflow (research → discover → write artifact → mark complete)
- Volume-over-quality constraint
- Don't-go-deep constraint
- Breadth discipline

**Trim `instructions.md`** — remove:
- "How to Use Strategic vs Tactical Phases" section (lines 122-134)
- Any tool listings

#### Developer

Already has custom phase prompts. Minor cleanup:
- Review `instructions.md` for any remaining phase-overlap content
- Ensure phase prompts cover all developer-specific workflow guidance

#### General Secretary (shared prompts)

- Keep `strategic.txt` and `tactical.txt` as generic defaults
- Trim `instructions.md`:
  - Remove tool category block (redundant with LangGraph)
  - Keep generalist identity, working principles, output format guidance

### Additional Changes

#### Fix strategic_todos_initial.yaml

Change todo #1 from "populate workspace.md with tools/context" to something that doesn't encourage copying instructions:

```yaml
content: >-
  Explore the workspace and update workspace.md with the current task context,
  key decisions, and any blocking issues. Read plan.md if it exists.
```

#### Consider instructions.md Injection Method

Currently `instructions.md` is injected as a `HumanMessage` that persists in history. Consider:

1. **Inject as transient content** (like workspace.md) — rebuilt every call, not stored in state, survives compaction intact
2. **Move critical rules to workspace.md** — the system already tells agents to "pin critical rules to workspace.md under Pinned Instructions"
3. **Keep current approach** but trim instructions.md to be shorter (~50-80 lines max)

Option 3 is the least invasive. The instructions.md files are already much shorter for experts than the old monolithic generalist one, and context compaction handles the rest.

---

## 4. Implementation Priority

| Priority | Change | Effort | Impact | Status |
|----------|--------|--------|--------|--------|
| 1 | Create Critic `strategic.txt` + `tactical.txt` | Medium | Fixes expert workflow mismatch | |
| 2 | Create Scholar `strategic.txt` + `tactical.txt` | Medium | Fixes expert workflow mismatch | |
| 3 | Trim phase-overlap sections from Critic + Scholar `instructions.md` | Low | Removes hierarchy conflict | |
| 4 | Trim tool listings from all `instructions.md` files | Low | Reduces token waste | |
| 5 | Fix `strategic_todos_initial.yaml` todo #1 | Low | Prevents workspace.md duplication | |
| 6 | Trim shared `instructions.md` (generalist) | Low | Token savings | |
| 7 | Decompose instructions.md into context layers (Section 5) | High | Eliminates Issues C-E, reduces tokens ~60% | |
| 8 | Add auto-seeded Pinned Instructions from config | Medium | Constraints survive compaction without manual pinning | |
| 9 | Structured rejection feedback (Section 5.5) | Medium | Agents get actionable fix targets instead of freeform text | |
| 10 | Add model class detection + prompt adaptation | High | Enables model-agnostic expert configs | **Infrastructure done** — `PromptMatrixResolver` + `detect_model_family()` implemented; needs prompt variants |
| 11 | Add model-class-aware tool description tiers | High | Optimizes tool selection accuracy per model, defer | |

Priorities 1-6 can be done independently as quick wins. Priority 7 is the big architectural shift described in Section 5 — decomposing instructions.md into the five context layers. Priority 8 makes constraint pinning automatic. Priority 9 improves the feedback loop for rejected jobs. Priority 10's infrastructure (`PromptMatrixResolver`, `detect_model_family()`, matrix-only resolution in all loading functions) is complete — what remains is authoring model-specific prompt variants and populating `prompt_matrix.yaml` files. Priority 11 (model-class-aware tool description tiers) applies on top of the layered architecture.

---

## 5. The Bigger Question: How Should You Prompt an Autonomous Agent?

Sections 1-4 above treat the prompt system as a set of files to fix — move content here, trim content there. But the user raised a more fundamental question: **is the whole approach right?**

The current system gives the agent a personality blob (`instructions.md`), a workflow blob (phase prompts), a memory file (`workspace.md`), and a task description. Then it lets the agent run for hours across multiple context windows. Is that the right architecture — or should autonomous agents be prompted in a fundamentally different way?

### 5.1 What the Industry Is Converging On

The field has moved from "prompt engineering" (crafting a single good prompt) to **"context engineering"** — designing the full information environment that surrounds an LLM on every call. The term was popularized in mid-2025 by Shopify's CEO Tobi Lütke and Andrej Karpathy, and Anthropic has published detailed guidance on it.

The core insight: for autonomous agents, the prompt isn't a script — it's an operating system. You're loading a CPU (the LLM) with just the right code and data for the current task, not writing a letter to a person.

**Key principles from current research and vendor guidance:**

1. **Minimal, high-signal tokens** (Anthropic) — Find the smallest set of tokens that fully outlines expected behavior. Start with a minimal prompt on the best available model, then add instructions based on observed failure modes. More isn't better; the right information is better.

2. **The "right altitude"** (Anthropic) — Don't over-specify (brittle logic that breaks on edge cases) and don't be vague (high-level guidance without concrete signals). Strike a balance: *"specific enough to guide behavior effectively, yet flexible enough to provide the model with strong heuristics."*

3. **Dynamic assembly per request** (multiple sources) — Unlike a chatbot where the system prompt is static, an agent's context should be **rebuilt on every call** based on current state: which phase, what tools are relevant, what the agent knows now vs what it knew at the start.

4. **Separation of concerns** (multiple sources) — Identity, goals, constraints, workflow, and knowledge should be **separate layers**, not mixed in one blob. Treat the prompt like a program with explicit sections.

5. **Attention priority** (Augment Code) — Models pay most attention to: user messages > beginning of prompt > end of prompt > middle. Place critical instructions where they'll actually be read.

6. **Examples > rules** (Anthropic) — For an LLM, examples are the "pictures worth a thousand words." A set of diverse, canonical examples teaches behavior more effectively than exhaustive edge-case rules.

7. **Prompt caching awareness** (Augment Code, Claude docs) — Build prompts to append content during sessions rather than modify existing portions. Keep dynamic state in user messages, not system prompts, so cache remains valid.

### 5.2 What's Wrong with the Current Approach

The current system does some things well but has a structural problem: **`instructions.md` is a monolithic blob that tries to do everything, and it's delivered via the wrong mechanism.**

**What the current `instructions.md` files contain (using Critic as an example):**

| Content | Lines | Type | Should Be |
|---------|-------|------|-----------|
| Identity ("You are the last line of defense") | 1-6 | Persistent personality | System prompt (1-2 sentences) |
| Core Principles ("Every claim cites evidence") | 8-15 | Persistent constraints | System prompt or workspace.md Pinned Instructions |
| Review Modes (4 detailed modes) | 17-101 | Reference material | Workspace file, read on demand |
| Review Report Format (template) | 103-143 | Reference material | Workspace file, read on demand |
| Severity System (table + rules) | 145-161 | Reference material | Workspace file, read on demand |
| Diagnostic Methodology (table) | 163-174 | Reference material | Workspace file, read on demand |
| Anti-Patterns (6 items) | 176-197 | Persistent constraints | Phase prompt or workspace.md |
| Strategic vs Tactical guidance | 199-221 | Phase workflow | Phase prompts (Issue B) |
| Workspace Memory guidance | 213-221 | Phase workflow | Strategic prompt |
| Task placeholder | 223-226 | Job description | Already handled by job description |

Out of ~226 lines, roughly:
- **~8 lines** are identity (should be in system prompt)
- **~15 lines** are persistent constraints (should be in system prompt or workspace.md)
- **~150 lines** are reference material (should be workspace files, read on demand)
- **~30 lines** are phase workflow (should be in phase prompts)
- **~4 lines** are a task placeholder (already handled)

The reference material — review modes, report formats, severity tables, diagnostic methodology — is the bulk of the file. It's valuable content. But it's injected as a `HumanMessage` at job start, persists in conversation history consuming tokens on every call, and then gets summarized (lossy) during context compaction. The agent might need the severity table in phase 5, but by then it's been compressed to "the Critic uses a CRITICAL/HIGH/MEDIUM/LOW severity system."

**The fundamental problem**: `instructions.md` conflates "who you are" (persistent) with "how to do specific things" (reference) and delivers both via a mechanism that degrades over time.

### 5.3 The Context Engineering Approach

Instead of one blob, decompose into **five context layers**, each delivered via the right mechanism:

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: SYSTEM PROMPT (persistent, every call)            │
│                                                             │
│  Identity (1-2 sentences)                                   │
│  Memory model (how workspace.md/plan.md work)               │
│  Meta-cognitive guardrails                                  │
│  Core constraints that NEVER change                         │
│                                                             │
│  → systemprompt.txt (already mostly right)                  │
├─────────────────────────────────────────────────────────────┤
│  LAYER 2: PHASE PROMPT (dynamic, changes per phase)         │
│                                                             │
│  Phase-specific workflow                                    │
│  Phase-specific constraints and anti-patterns               │
│  What to do, what NOT to do in this phase                   │
│                                                             │
│  → strategic.txt / tactical.txt (proposed in Section 3)     │
├─────────────────────────────────────────────────────────────┤
│  LAYER 3: WORKSPACE MEMORY (persistent, every call)         │
│                                                             │
│  Current task context                                       │
│  Key decisions and progress                                 │
│  Pinned rules from instructions                             │
│  Dynamic state that evolves with the job                    │
│                                                             │
│  → workspace.md (already works well)                        │
├─────────────────────────────────────────────────────────────┤
│  LAYER 4: REFERENCE MATERIAL (on-demand, NOT every call)    │
│                                                             │
│  Output format templates                                    │
│  Domain methodology details (review modes, exploration      │
│  modes, delegation patterns)                                │
│  Anti-pattern catalogues                                    │
│  Tool usage examples                                        │
│                                                             │
│  → Workspace files the agent reads when needed              │
├─────────────────────────────────────────────────────────────┤
│  LAYER 5: TASK + FEEDBACK (provided at job start/resume)    │
│                                                             │
│  Job description (what to do)                               │
│  Success criteria (what "done" looks like)                  │
│  Rejection feedback (what to fix, if resuming)              │
│                                                             │
│  → Job description + feedback message                       │
└─────────────────────────────────────────────────────────────┘
```

**What changes vs the current system:**

| Layer | Current Mechanism | Proposed Mechanism | Change |
|-------|-------------------|-------------------|--------|
| 1. Identity + guardrails | `systemprompt.txt` | `systemprompt.txt` | Minor: absorb 1-2 identity sentences from instructions.md |
| 2. Phase workflow | `strategic.txt` / `tactical.txt` | Same, but per-expert | Already proposed (Section 3) |
| 3. Workspace memory | `workspace.md` | Same | No change |
| 4. Reference material | Mixed into `instructions.md` | Separate workspace files | **New**: decompose instructions.md |
| 5. Task + feedback | Job description + feedback msg | Same, possibly structured | Minor: structure feedback |

The big change is **Layer 4**: taking the ~150 lines of reference material out of `instructions.md` and putting them in workspace files that the agent reads on demand. This means:
- The severity table isn't in every LLM call — it's in `tools/severity_system.md` and the agent reads it when writing a review
- The review report template isn't consuming tokens during web exploration — it's in `tools/review_template.md` and read when needed
- The delegation prompt templates (Developer) aren't in every call — they're in `tools/delegation_patterns.md`

The agent already knows how to read workspace files. The phase prompts can say "read `tools/review_template.md` before writing your first review report" — a just-in-time retrieval pattern instead of upfront loading.

### 5.4 What Happens to instructions.md?

Under the context engineering approach, `instructions.md` gets decomposed:

**Critic example:**

| Current instructions.md content | New location |
|--------------------------------|--------------|
| "You are the last line of defense..." | `systemprompt.txt` identity line (or `config.yaml` display description) |
| Core principles (evidence, failing tests, no rubber-stamping, severity) | `workspace.md` Pinned Instructions (auto-seeded from config) |
| Review Modes 1-4 | `tools/review_modes.md` (workspace reference file) |
| Review Report Format | `tools/review_template.md` (workspace reference file) |
| Severity System | `tools/severity_system.md` (workspace reference file) |
| Diagnostic Methodology | Fold into `tactical.txt` (it's short and relevant during execution) |
| Anti-Patterns | Split: phase-universal ones → `workspace.md` Pinned Instructions; phase-specific → respective phase prompts |
| Strategic vs Tactical | Phase prompts (already proposed) |
| Workspace Memory guidance | `strategic.txt` (already proposed) |

**What's left of instructions.md?** Almost nothing. The identity is in the system prompt. The constraints are in workspace.md. The reference material is in workspace files. The workflow is in phase prompts. The task comes from the job description.

You could either:
1. **Keep a very thin instructions.md** (~20-30 lines) that just sets the agent's orientation: "You are the Critic. Your job is X. Your core constraints are Y. Reference materials are in `tools/`."
2. **Eliminate instructions.md entirely** and distribute everything to the appropriate layer.

Option 1 is safer for the transition. The thin instructions.md becomes a "bootstrap file" — it orients the agent and points it to resources, but doesn't try to be a comprehensive manual.

### 5.5 The Feedback Problem

The user also raised how the agent receives feedback on rejected jobs. Currently:
- A job is rejected with a feedback message (freeform text)
- The agent receives this as a message when it resumes

This works but is unstructured. The context engineering approach suggests structured feedback:

```yaml
# Structured rejection feedback
verdict: REJECTED
issues:
  - severity: CRITICAL
    location: "output/analysis.md, Section 3"
    problem: "Claims about EU AI Act Article 6 are incorrect"
    expected: "Article 6 defines high-risk AI systems, not general-purpose AI"
  - severity: HIGH
    location: "output/analysis.md"
    problem: "Missing coverage of GPAI provisions (Chapter V)"
    expected: "Include Articles 51-56 on general-purpose AI model obligations"
feedback: "The analysis confuses high-risk AI system requirements with GPAI obligations. Re-read the source document Chapters V and VI."
```

This gives the agent:
- **Specific locations** to fix (not "the analysis has errors")
- **Severity** so it can prioritize (CRITICAL first)
- **Expected behavior** so it knows what "fixed" looks like
- **General feedback** for context

The agent can then create targeted todos instead of re-reading the entire output trying to guess what was wrong.

**Implementation**: The cockpit UI could offer a structured feedback form when rejecting a job (issue list + general feedback), and the orchestrator injects it as a structured message at resume. This doesn't require changes to the prompt architecture — just better input formatting.

### 5.6 How This Relates to the Existing Issues

The context engineering decomposition **subsumes** several of the issues identified in Section 2:

| Issue | Status Under Context Engineering |
|-------|--------------------------------|
| A: Expert phase prompt inconsistency | Solved — per-expert phase prompts (Layer 2) |
| B: Overlap between instructions.md and phase prompts | Solved — phase content moves to phase prompts, not in instructions.md |
| C: instructions.md persists in history | Solved — reference material moves to workspace files (Layer 4), not injected as HumanMessage |
| D: workspace.md duplication | Solved — initial todo says "read reference files and orient" not "copy instructions" |
| E: Tool info appears multiple times | Solved — tool descriptions stay in LangGraph schemas + workspace `tools/*.md` only |
| F: No model-aware prompting | Separate concern — still needs model class detection in Layer 1/2 assembly |
| G: Tool descriptions model-agnostic | Separate concern — still needs model-class-aware description tiers |

Issues A-E become implementation details of the layered architecture. Issues F-G remain independent dimensions that apply on top of it.

### 5.7 What a Clean Implementation Looks Like

For the Critic expert, the final state would be:

**`systemprompt.txt`** (shared, ~40 lines, every call):
```
You are {agent_display_name}, an autonomous agent...
[memory model, instruction hierarchy, working principles, meta-cognitive guardrails]
{prompt_content}
{todos_content}
```
Unchanged from current. The identity sentence ("You are the Critic — the quality gatekeeper") comes from `{agent_display_name}` which is already set to "Critic" in config.yaml.

**`strategic.txt`** (Critic-specific, ~30 lines, strategic calls only):
```
You are in STRATEGIC PHASE — Review, reflect, plan.
1. Assess the review queue (job description, documents, code diffs, scholar ideas)
2. Prioritize by risk and wait time
3. Plan review approach: which modes, which areas, what to run
4. Rewrite workspace.md (compact, don't append)
5. Create todos: one todo = one review task
Read tools/review_modes.md if you need to refresh on available modes.
```

**`tactical.txt`** (Critic-specific, ~30 lines, tactical calls only):
```
You are in TACTICAL PHASE — Execute reviews.
Per todo:
1. Read the subject (code diff, idea artifact, codebase area)
2. Investigate: read full files for context, run tests
3. Collect evidence: every claim cites file:line
4. Write the review report to output/reviews/
Read tools/review_template.md for the report format.
Read tools/severity_system.md for severity classification.
```

**`workspace.md`** (persistent memory, every call):
```markdown
## Pinned Instructions
- Every claim cites evidence (file:line, test output, exact error)
- No approval with failing tests — REJECTED, no exceptions
- You review, you don't fix — document issues, Developer fixes them
- Severity by impact, not by size

## Current Context
[filled in by agent during strategic phases]

## Review Queue
[filled in by agent]

## Verdicts Issued
[filled in by agent]
```
The Pinned Instructions are auto-seeded from a config field (new) at job initialization — the agent doesn't need to read instructions.md and manually copy them.

**Workspace reference files** (read on demand, NOT every call):
- `tools/review_modes.md` — The 4 review modes with process and checklists (~80 lines)
- `tools/review_template.md` — The review report Markdown template (~40 lines)
- `tools/severity_system.md` — Severity table + rules (~20 lines)
- `tools/diagnostic_methodology.md` — Contradiction detection table (~15 lines)
- `tools/anti_patterns.md` — Anti-pattern catalogue (~25 lines)

These are auto-generated from config into the workspace at job initialization (like tool docs already are via `DescriptionManager`).

**Total token cost per call**: ~40 (systemprompt) + ~30 (phase prompt) + ~50 (workspace.md) = **~120 lines** of instruction context.
**Current cost per call**: ~40 (systemprompt) + ~15 (shared phase prompt) + ~50 (workspace.md) + ~226 (instructions.md in history) = **~330 lines**.

That's roughly a **60% reduction** in instruction tokens per LLM call, with the reference material still fully available when the agent actually needs it.

### 5.8 Summary: The Shift

| Dimension | Current ("Prompt Engineering") | Proposed ("Context Engineering") |
|-----------|-------------------------------|----------------------------------|
| Instructions delivery | One-time HumanMessage blob | Layered: system prompt + phase prompt + workspace + reference files |
| Reference material | Baked into every call | On-demand read from workspace files |
| Constraints | Mixed with methodology | Pinned to workspace.md (persistent across compaction) |
| Phase workflow | Shared generic prompts | Per-expert phase prompts (already proposed) |
| Identity | 5-6 line personality block | 1-2 sentences in system prompt or config display name |
| Feedback on rejection | Freeform text | Structured (severity, location, expected behavior) |
| Token cost | ~330 lines/call | ~120 lines/call |
| Survives compaction? | instructions.md gets summarized (lossy) | Constraints in workspace.md (persistent), reference in files (never in context to lose) |

The core shift: stop treating the agent like a person who needs to read a manual once, and start treating it like a CPU that needs the right data loaded for the current operation.

### Sources

- [Effective Context Engineering for AI Agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [How to Build Your Agent: 11 Prompting Techniques — Augment Code](https://www.augmentcode.com/blog/how-to-build-your-agent-11-prompting-techniques-for-better-ai-agents)
- [Prompt Engineering for AI Agents — PromptHub](https://www.prompthub.us/blog/prompt-engineering-for-ai-agents)
- [Agentic Prompt Engineering: Role-Based Formatting — Clarifai](https://www.clarifai.com/blog/agentic-prompt-engineering)
- [Prompting Best Practices: Claude 4.6 — Anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-4-best-practices)
- [Context Engineering: Bringing Engineering Discipline to Prompts — Addy Osmani](https://addyo.substack.com/p/context-engineering-bringing-engineering)
- [Context Engineering for Reliable AI Agents — Kubiya](https://www.kubiya.ai/blog/context-engineering-ai-agents)
- [Prompt Engineering for Agents: Roles, Goals, and Behaviors — bKlug](https://bklug.ai/blog/prompt-engineering-for-agents-designing-roles-goals-and-behaviors)
- [Context Engineering Guide — Prompting Guide](https://www.promptingguide.ai/guides/context-engineering-guide)

### Deep Research Reports

Four focused research reports were commissioned to validate and refine the proposals in this document. All reports are in `docs/prompting/`.

#### 1. Layered Prompt Architectures for Production LLM Agents

**File**: `docs/prompting/Layered Prompt Architectures for Production LLM Agents_ Context Engineering and Caching Strategies.pdf`
**Maps to**: Section 5 (5-layer architecture), Priorities 7-8

Key findings that affect our plan:
- **Production consensus validates the 5-layer model** — Claude Code, Manus, Cursor, OpenHands, Devin all converge on layered context with on-demand reference retrieval. The hybrid architecture (small persistent system prompt + on-demand reference files) is now industry standard.
- **On-demand retrieval works but agents forget to retrieve 56% of the time** (Vercel evaluation). Recommendation: use **system-triggered injection** for critical reference material (orchestrator loads it when entering a phase) rather than trusting the agent to read it. Agent-triggered retrieval only for exploratory context.
- **Three-tier progressive disclosure** is the emerging standard: Tier 1 (always loaded) = YAML frontmatter with name/description only; Tier 2 (loaded on activation) = full instruction body; Tier 3 (loaded on reference) = detailed scripts and data. Maps directly to our `defer_to_workspace` pattern.
- **Auto-seeded constraints need defense in depth** — no single mechanism guarantees survival. Recommended: (A) system prompt meta-instruction, (B) workspace.md pinned section with clear delimiters, (C) custom compaction prompt preserving pinned section verbatim, (D) re-injection from config after every compaction event, (E) post-generation output validation.
- **Prompt caching requires strict ordering**: tools → system prompt → reference material → phase prompt + workspace.md → task + feedback. Place Anthropic cache breakpoints between each transition.
- **Structured compaction with anchored sections** (Factory.ai pattern) scores 3.70/5 vs 3.35 for freeform — structure forces preservation of file paths, constraint lists, and specific numbers that freeform summarization drops.
- **Set compaction threshold at 50-60% of context capacity**, not 75-80% — research shows model performance degrades well before the window fills.

#### 2. Model-Aware Prompting for Multi-Model Agent Systems

**File**: `docs/prompting/Model-Aware Prompting for Multi-Model Agent Systems_ Dynamic Tool Loading and Cross-Provider Optimization.pdf`
**Maps to**: Issues F + G, Priorities 10-11

Key findings that affect our plan:
- **The single most impactful change is dynamic tool loading, not prompt wording.** Reducing the active tool set from 40 to 5-10 per query improves accuracy by 25+ percentage points across all model families. Tool count drives accuracy more than any other factor.
- **Vendor-specific tool count limits**: Claude 50+ with Tool Search / 10-15 without; GPT-5/o-series <100 in-distribution / 20-40 with clear descriptions; Gemini 10-20 recommended / 128 hard limit; small models (<70B) fewer than 10 per call.
- **Four model-family adaptations capture 80% of gains** without maintaining separate prompt variants: (1) format tags — XML for Claude, Markdown for OpenAI; (2) reasoning model detection — remove CoT instructions for o-series, R1, QwQ; (3) parallel tool calling guidance — explicit prompt block; (4) tool set sizing per model class.
- **Proposed `model_class` YAML schema** with four categories: `frontier_reasoning` (claude-opus, o3, o4, gpt-5), `standard` (claude-sonnet, claude-haiku, gpt-4o, gemini), `oss_reasoning` (deepseek-r1, qwq, qwen-thinking), `compact` (llama-8b, qwen-7b/14b). Each category sets tool_limit, parallel_tools, format, cot_instructions, and constrained_decoding.
- **DeepSeek R1 ignores system prompts** — all instructions must go in the user role. R1 also doesn't natively support function calling; requires ReAct-style prompting or routing to V3/Chat for tool execution. The `reasoning_method` config field now controls whether the `Reasoning:` directive is injected (`"prompt"` for gpt-oss), passed as an API parameter (`"api"`), or omitted (`"none"` for Claude/Gemini).
- **Universal prompts with runtime tool management outperform model-specific prompt variants** for most practical purposes. Invest engineering effort in dynamic tool loading first, format adaptation second, full prompt variants only for highest-stakes configs.
- **Comparison matrix** across Claude 4.x, GPT-5/o-series, Gemini 3/2.5, Qwen3/QwQ, DeepSeek R1, and Llama 70B covering description style, tool count tolerance, parallel calling, prompt format, system prompt adherence, reasoning control, temperature, context window, and BFCL ranking.

#### 3. Structured Feedback Loops for Fixing Agent Mistakes

**File**: `docs/prompting/Structured Feedback Loops for Fixing Agent Mistakes_ Diagnosis, Correction Patterns, and Optimal Architecture.pdf`
**Maps to**: Priority 9, Section 5.5

Key findings that affect our plan:
- **Diagnosis, not repair, is the bottleneck** — RefineBench (2025) showed frontier LLMs achieve near-perfect correction when given explicit, structured feedback but struggle to self-diagnose errors. The more precisely the critic localizes and categorizes problems, the fewer correction iterations needed.
- **Three highest-impact feedback fields** (ranked by evidence strength): error localization (where), error category (what type), and actionable fix instructions (how to correct). Severity enables routing but has no direct ablation study yet.
- **Structured feedback dramatically outperforms freeform** — MAF paper: 20% improvement in mathematical reasoning, 18% in logical entailment with decomposed per-error-type feedback. Self-Debugging: +12.2% on MBPP with unit test feedback vs +2.7% with simple feedback. Self-Refine: localization + actionable instruction is the minimum effective format.
- **Optimal correction pattern: 2-3 bounded iterations with strategic fresh starts.** Debugging Decay Index models effectiveness as E(t) = E₀ × e^(-λt) — models lose 60-80% of debugging capability within 2-3 attempts. After 3 failed cycles, regenerate from scratch rather than iterating further.
- **Prescriptive feedback with exemplars maximizes actionability** — educational psychology meta-analysis (435 studies, 61,000+ participants) confirms: the most effective feedback answers "Where am I going?" + "How am I doing?" + "Where to next?" Exemplar-based feedback ("See auth_handler.ts:87 for the correct pattern") is even stronger than purely prescriptive.
- **Separate judgment from critique** (SiriuS pattern) — a lightweight check determines *whether* correction is needed before invoking the full critic, preventing unnecessary modification of already-correct work.
- **Recommended critic output schema** with fields: verdict, confidence, iteration, max_iterations, issues (id, severity, category, phase, location, problem, expected, fix, exemplar), positive_observations, verification_checklist. Includes a 4-stage QA gate: automated pre-checks → targeted verification of flagged issues → regression test subset → confidence-based routing to human review.
- **Feedback injection across 4 layers**: system prompt (cross-cutting rules only, 2-3 lines max), graph state (active feedback list + iteration count, checkpointed), workspace file (`feedback.yaml` as ground truth, survives compaction), user message on resume (priority-sorted summary exploiting recency bias).

#### 4. Token Economics for Multi-Phase Autonomous Agent Systems

**File**: `docs/prompting/Token Economics for Multi-Phase Autonomous Agent Systems.pdf`
**Maps to**: Priorities 4, 6, 7, Issue C

Key findings that affect our plan:
- **Prompt caching is the single highest-leverage optimization** for 200+ call jobs — Anthropic's explicit caching offers 90% read discount with deterministic hits; OpenAI's automatic caching offers 50% with probabilistic routing (~50% hit rate). Combined with proper ordering, a well-architected system can cut total input costs by 40-55%.
- **Optimal prompt ordering by stability** (most static first): tool schemas (rarely change) → system prompt (never changes) → instructions.md (cached within compaction window) → phase prompt (changes every 10-50 calls) → workspace.md (changes most calls) → conversation history (every call, never cached from prefix).
- **Conversation history management is the biggest cost lever** — $4.80 savings vs $3.91 from prompt caching in the optimized model. Earlier compaction (at 50% capacity instead of 75-80%) with structured summarization is the single highest-dollar optimization.
- **Tiered context compaction outperforms any single strategy**: Tier 1 — observation masking at 50% capacity (hide tool output from older turns, 50-80% token savings on observation content, zero extra LLM cost); Tier 2 — structured summarization at 70% (Factory.ai scores 3.70/5 vs 3.35 unstructured); Tier 3 — full compaction with external backup at 85%.
- **On-demand reference retrieval still saves tokens even with caching**, but the margin shrinks: ~$0.07/job on Claude Sonnet (from ~291K to ~22.7K effective tokens). Keep critical reference material in the cached prefix; move to on-demand only if accessed <5% of calls or supplementary rather than normative.
- **Structured workspace.md formats resist bloat** — section headers with per-section token budgets + priority tags ([HIGH]/[MED]/[LOW]) enable automated pruning. Programmatic enforcement: count tokens after each rewrite, trigger a correction LLM call if budget exceeded.
- **Cost model**: current architecture (no optimization) = **$19.44/job** on Claude Sonnet 4 (200 calls). Optimized architecture (caching + tool compression + workspace budget + aggressive compaction) = **$10.87/job** — a **44.1% net savings**. On Claude Opus, the optimized architecture saves ~$42/job.
- **Practical implementation roadmap**: Week 1 (high impact, low effort) — reorder prompt components + enable caching, ~23% savings on input costs. Week 2 (medium impact, low effort) — compress tool descriptions + workspace.md budget, ~3-5% additional. Week 3 (high impact, medium effort) — tiered compaction starting at 50% capacity, ~15-25% additional. Month 2 — dynamic tool loading + model routing.
