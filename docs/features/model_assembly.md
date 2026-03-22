---
tags:
  - feature
  - architecture
  - llm
  - cost-optimization
aliases:
  - model assembly
  - tiered model strategy
  - phase model routing
related:
  - "[[auxiliary]]"
  - "[[model_selection]]"
  - "[[prompting]]"
---

# Model Assembly — Phase-Level Model Routing

> Last updated: 2026-03-20

## The Problem

Running frontier models for every LLM call is wasteful. A typical agent job alternates between two fundamentally different workloads:

| Phase | Workload | What matters |
|-------|----------|--------------|
| **Strategic** | Review changes, reflect on progress, adapt the plan, create todos | Judgment, information gathering, architectural reasoning, knowing what to look up |
| **Tactical** | Execute well-defined todos: write code, run commands, edit files | Following instructions, tool calling reliability, code generation quality |

The key insight: **the code a frontier model writes is nearly identical to what a mid-tier model produces**. The difference is that frontier models are smart enough to look up API endpoints before coding, check if a function already exists, or recognize that a component belongs in module A not module B. Coding is commodity; software engineering judgment is not.

Strategic phases are short and infrequent (~5-10% of total tokens). Tactical phases consume the bulk of the token budget. Routing a cheap model to tactical execution and reserving the frontier model for strategic decisions can cut total cost by 5-20x without degrading output quality.

## Current Infrastructure (Already Implemented)

Phase-level model routing is already supported in the codebase. The plumbing exists — this document defines the strategy for using it.

### Configuration

```yaml
# config/my_agent.yaml
llm:
  model: claude-opus-4-6               # Base model (used for strategic by default)
  temperature: 0.0
  multimodal: true

  strategic:                             # Override for strategic phases
    model: claude-opus-4-6
    reasoning_level: high

  tactical:                              # Override for tactical phases
    model: gemini-3-flash
    provider: google
    temperature: 0.0

  summarization:                         # Override for context compaction
    model: gpt-4o-mini
    provider: openai
```

Each phase override inherits all base fields and only replaces the fields it explicitly sets. `LLMConfig.get_phase_config()` handles the merge (`src/core/loader.py:655`).

### Code Path

1. `_create_phase_llms()` in `src/agent.py:210` creates separate `BaseChatModel` instances per phase
2. `build_phase_alternation_graph()` in `src/graph.py:2528` accepts `strategic_llm_with_tools` and `tactical_llm_with_tools`
3. `create_execute_node()` in `src/graph.py:410` switches between them based on `is_strategic_phase`:
   ```python
   llm_with_tools = strategic_llm_with_tools if is_strategic else tactical_llm_with_tools
   ```
4. Tool schemas and model kwargs are extracted separately for each LLM and swapped at execution time

### Auxiliary LLM (Support Tasks)

Background tasks (memory extraction, summarization, knowledge curation) already use a separate model via the `auxiliary` config key. See `docs/features/auxiliary.md`.

```yaml
auxiliary:
  enabled: true
  model: deepseek-v3.2    # Cheapest viable model for structured extraction
```

## Model Tiers (March 2026)

### Tier 1 — Strategic (Judgment + Information Gathering)

Models that systematically gather context before acting. They read existing code, check APIs, verify assumptions, and make architectural tradeoffs.

| Model | Input/1M | Output/1M | Context | Why |
|-------|----------|-----------|---------|-----|
| **Claude Opus 4.6** | $5.00 | $25.00 | 1M | #1 Arena ELO, Terminal-Bench, GDPval. Best meta-cognitive behavior — will look things up before coding. Adaptive thinking auto-calibrates reasoning effort. |
| **GPT-5.4 (high)** | $2.50 | $15.00 | 1.05M | Strong "look before you leap" at high reasoning. Cheaper than Opus with competitive judgment. |
| **Gemini 3.1 Pro** | $2.00 | $12.00 | 1M | 80.6% SWE-bench single-attempt, 94.3% GPQA Diamond. Best value frontier reasoning (still in preview). |

**What makes these models different**: On raw code generation benchmarks (Aider polyglot), the gap between frontier and mid-tier models is modest. But on **agentic SWE tasks** (Terminal-Bench, SWE-bench with agent harnesses, GDPval), the ranking shifts dramatically. The differentiator is not "can it write a function" but "does it know when to read the codebase, check docs, look at existing tests, and verify assumptions before writing."

### Tier 2 — Tactical (Reliable Execution)

Models that follow well-defined instructions, call tools reliably, and produce good code — but don't need frontier-level judgment because the strategic phase already decided *what* to do.

| Model | Input/1M | Output/1M | Context | Why |
|-------|----------|-----------|---------|-----|
| **Gemini 3 Flash** | $0.50 | $3.00 | 1M | 75.8% SWE-bench at fraction of frontier cost. Best price-performance for execution. |
| **o4-mini** | $1.10 | $4.40 | 200K | Strong reasoning for its price. Good for reasoning-heavy tactical tasks. |
| **Qwen3 Coder Plus** | $0.65 | $3.25 | 1M | Coding-focused, 1M context, excellent value. Worth testing for code-heavy tactical work. |
| **GPT-5 (low)** | $1.25 | $10.00 | 400K | 81.3% Aider at low reasoning — cheaper than high mode with good code quality. |
| **MiniMax M2.7** | $0.30 | $0.60 | 200K | 80.2% SWE-bench, 76.8% BFCL. Cheapest model with near-frontier code quality. Known weaknesses: debugging passivity, context rot on long sessions. |

### Tier 3 — Auxiliary (Background Tasks)

Cheapest viable models for structured extraction tasks (memory, summarization, curation). These don't need to "think" — they need to parse context and produce structured output.

| Model | Input/1M | Output/1M | Context | Why |
|-------|----------|-----------|---------|-----|
| **DeepSeek V3.2** | $0.28 | $0.42 | 128K | Cheapest option with adequate reasoning for structured extraction. |
| **gpt-oss-120b** | Free | Free | 128K | University-hosted. Good for structured tasks, poor for long agent loops. |
| **QwQ-32B** | $0.15 | $0.58 | 131K | Cheapest reasoning model. Adequate for summarization and memory extraction. |

## Assembly Configurations

### Premium Assembly (Best Quality)

Maximum reasoning quality for strategic decisions. Use when job correctness matters more than cost.

```yaml
llm:
  model: claude-opus-4-6
  multimodal: true
  parallel_tool_calls: true
  strategic:
    model: claude-opus-4-6
    reasoning_level: high
  tactical:
    model: gemini-3-flash
    provider: google
auxiliary:
  model: deepseek-v3.2
```

**Cost profile** (estimated per 1M total tokens, 10% strategic / 90% tactical):
- Strategic: ~$5 + $25 = $30/M blended → $3.00 for 100K tokens
- Tactical: ~$0.50 + $3 = $3.50/M blended → $3.15 for 900K tokens
- **Total: ~$6.15/M** (vs $30/M all-Opus = **~5x savings**)

### Balanced Assembly (Quality + Cost)

Good judgment at moderate cost. Suitable for most production workloads.

```yaml
llm:
  model: gpt-5.4
  strategic:
    model: gpt-5.4
    reasoning_level: high
  tactical:
    model: gemini-3-flash
    provider: google
auxiliary:
  model: deepseek-v3.2
```

**Cost profile** (estimated per 1M total tokens):
- Strategic: ~$17.50/M blended → $1.75 for 100K tokens
- Tactical: ~$3.50/M blended → $3.15 for 900K tokens
- **Total: ~$4.90/M** (vs $17.50/M all-GPT-5.4 = **~3.5x savings**)

### Budget Assembly (Maximum Savings)

For high-volume workloads where cost is the primary constraint. Trades some strategic judgment for dramatically lower costs.

```yaml
llm:
  model: minimax/MiniMax-M2.7-standard
  strategic:
    model: gemini-3.1-pro
    provider: google
  tactical:
    model: minimax/MiniMax-M2.7-standard
auxiliary:
  model: deepseek-v3.2
```

**Cost profile** (estimated per 1M total tokens):
- Strategic: ~$14/M blended → $1.40 for 100K tokens
- Tactical: ~$0.90/M blended → $0.81 for 900K tokens
- **Total: ~$2.21/M**

### Self-Hosted Assembly (University/On-Prem)

When you have GPU access and want near-zero marginal cost.

```yaml
llm:
  model: openai/gpt-oss-120b
  base_url: ${LLM_BASE_URL}
  strategic:
    model: claude-opus-4-6     # Pay for judgment only
    provider: anthropic
    base_url: null              # Use Anthropic API, not local
  tactical:
    model: openai/gpt-oss-120b  # Free local model
auxiliary:
  model: openai/gpt-oss-120b    # Free local model
```

**Cost profile**: Only pay for strategic phases (~$3/M for 100K strategic tokens). Tactical and auxiliary are free.

## Design Considerations

### Context Window Mismatch

When strategic and tactical models have different context windows, the `settings_matrix.yaml` applies per model family. The compaction system (Layer 0/1/2) adapts to the active model's limits. However, be aware:

- If strategic runs at 1M context and tactical at 200K, a phase transition from strategic to tactical may require immediate compaction
- The `limits` config applies globally — the smaller context window constrains both phases
- Consider setting `limits.context_threshold_tokens` to the **tactical** model's limit to avoid mid-phase compaction

### Tool Schema Compatibility

Different models have different tool calling capabilities. The graph already handles this — `strategic_tool_schemas` and `tactical_tool_schemas` are extracted separately (`src/graph.py:452-453`). Phase-specific tool filtering via `TOOL_REGISTRY` phases ensures each model only sees tools available in its phase.

### Provider Key Requirements

Each phase model may require a different API key. Ensure all required keys are set:

```bash
# Premium assembly example
ANTHROPIC_API_KEY=sk-ant-...    # Strategic (Opus 4.6)
GOOGLE_API_KEY=AIza...          # Tactical (Gemini 3 Flash)
OPENAI_API_KEY=sk-...           # Auxiliary (or DeepSeek key)
```

### Prompt Matrix Implications

The prompt matrix (`config/prompt_matrix.yaml`) resolves prompts by model family. When strategic and tactical use different model families, each phase automatically gets model-appropriate prompts. This is already handled by `get_phase_system_prompt()` which is called per-execution with the current phase context.

However, the instruction matrix and prompt matrix resolve once at config load time, not per phase. If strategic uses Claude and tactical uses Gemini, both phases currently get the same resolved prompts (from the base model family). For model-family-specific prompts per phase, you would need to extend the matrix resolution to accept a phase parameter. This is a potential future enhancement.

### Resolved Config Serialization

`serialize_resolved_config()` captures the full config including phase overrides. On resume, `load_config_from_resolved()` reconstructs the phase-specific LLMs. This means model assembly configurations survive job restarts — no config drift.

### KeyRing Rotation

Each phase model gets its own KeyRing rotation if multiple keys are configured. A quota failure on the tactical model's key doesn't affect the strategic model. This is handled naturally since `create_llm()` is called separately per phase.

## Benchmark Data Supporting This Approach

The argument for model assembly rests on one observation: **agentic SWE benchmarks measure different capabilities than code generation benchmarks**.

### Code Generation (Aider Polyglot) — Models Are Close

| Model | Accuracy | Cost per run |
|-------|----------|-------------|
| GPT-5 (high) | 88.0% | $29.08 |
| Gemini 2.5 Pro | 83.1% | $49.88 |
| Grok 4 (high) | 79.6% | $59.62 |
| DeepSeek-V3.2 Reasoner | 74.2% | ~$1.30 |
| MiniMax M2.7 | ~80% | ~$0.50 |

The spread from cheapest to most expensive is modest — ~14 percentage points for a 60x cost difference.

### Agentic SWE (Multi-Step Judgment) — Models Diverge

| Benchmark | Top Performer | What It Measures |
|-----------|--------------|------------------|
| Terminal-Bench 2.0 | Claude Opus 4.6 (#1) | Multi-step debugging, refactoring, tool use |
| GDPval-AA | Claude Opus 4.6 (#1, 144 ELO above GPT-5.2) | Economically valuable knowledge work |
| BrowseComp | Claude Opus 4.6 (#1) | Finding hard-to-locate information |
| SWE-bench (single agentic) | Gemini 3.1 Pro (80.6%) | Real-world software engineering |

On these benchmarks, the gap between frontier and mid-tier is much larger. The difference is **meta-cognitive**: frontier models know when to gather information before acting. Mid-tier models will confidently generate code without checking existing implementations.

### The Assembly Hypothesis

Our phase architecture already separates "decide what to do" (strategic) from "do it" (tactical). By routing frontier models to the decision phase and cost-effective models to the execution phase, we capture the judgment advantage where it matters without paying frontier prices for commodity code generation.

## Open Questions

1. **Empirical validation** — The cost savings are estimated from token distribution assumptions (10% strategic, 90% tactical). Real jobs may vary. Run a batch of jobs with assembly configs and measure actual token split and output quality vs. single-model baseline.

2. **Context handoff quality** — When the strategic model writes todos and the tactical model executes them, the quality of the todo descriptions becomes critical. If strategic writes vague todos, a weaker tactical model may struggle more than a frontier model would. Consider whether strategic prompts need to be adjusted to produce more explicit, self-contained todos when a weaker tactical model is configured.

3. **Per-phase prompt matrix resolution** — Currently both phases get prompts resolved from the base model family. If a strategic Claude and tactical Gemini need different prompt styles, the matrix system would need phase-aware resolution. This is a potential future enhancement.

4. **Dynamic model selection** — Could the strategic phase choose which tactical model to use based on task complexity? Simple file edits → cheapest model, complex refactors → stronger model. This would require runtime model switching, which the current `bind_tools` architecture doesn't support without re-binding.

5. **Summarization model choice** — When strategic and tactical use different models, which should handle summarization? Currently configurable via `llm.summarization`. The strategic model produces better summaries but costs more. Since compaction happens infrequently, the cost impact is small — defaulting to the strategic model is reasonable.

## References

- `docs/model_selection.md` — Single-model selection rationale and MiniMax M2.7 decision
- `docs/features/auxiliary.md` — Auxiliary LLM system for background support tasks
- `config/settings_matrix.yaml` — Model-family-specific inference params and context limits
- `src/core/loader.py:614` — `LLMConfig` with phase override support
- `src/agent.py:210` — `_create_phase_llms()` creates per-phase LLM instances
- `src/graph.py:410` — `create_execute_node()` switches LLM per phase
- `src/graph.py:2528` — `build_phase_alternation_graph()` accepts separate strategic/tactical LLMs
