# Model Selection & Prompt Optimization

> Last updated: 2026-03-05

## Context

The project needs to run agents at scale — potentially 3 simultaneous agents for the self-improving loop, running for days at a time. Economics is the primary constraint. Development alone consumed 8k requests / 250M tokens. At frontier pricing (Opus 4.6 at $5/$25 per M tokens), that would cost thousands of euros. We need models that deliver strong agentic coding performance at <$1/M tokens blended.

## Model Comparison (Budget Tier)

| Model | Input/M | Output/M | Cache | SWE-bench Verified | Tool Calling (BFCL) | Notes |
|---|---|---|---|---|---|---|
| **MiniMax M2.5** | $0.30 | $0.60 | Auto, free | **80.2%** | **76.8%** | Best value for agentic work |
| **MiniMax M2.5 Lightning** | $0.30 | $1.20 | Auto, free | 80.2% | 76.8% | 2x speed, 2x output cost |
| **DeepSeek V3.2** | $0.28 | $0.42 | $0.028 hit | 73.1% | 80.3% (Tau2) | Cheaper output, weaker SWE-bench |
| Gemini 2.5 Flash-Lite | $0.10 | $0.40 | — | ~45%* | Limited | Too weak for agentic tasks |
| Gemini 2.5 Flash | $0.30 | $2.50 | — | ~54% | Good | Expensive output for the quality |
| GPT-5 mini | $0.25 | $2.00 | $0.0625 | N/A | Good | Output cost too high |

### Frontier Reference (Out of Budget)

| Model | Input/M | Output/M | SWE-bench Verified | Notes |
|---|---|---|---|---|
| Claude Opus 4.6 | $5.00 | $25.00 | 80.8% | Gold standard, 40x the cost |
| GPT-5.2 Pro | $10.00 | $30.00 | 80.0% | Similar quality, even more expensive |
| Gemini 3 Pro | — | — | 78.0% | Competitive but priced at frontier |

## Decision: MiniMax M2.5 (Standard)

**MiniMax M2.5 standard** is the primary model for all agent work.

Rationale:
- **80.2% SWE-bench** — within 0.6% of Opus 4.6, ahead of GPT-5.2 and Gemini 3 Pro
- **76.8% BFCL multi-turn** — leads Opus 4.6 by 13 points on multi-turn tool calling
- **51.3% Multi-SWE-bench** — #1 among all models on multi-file engineering tasks
- **$0.30/$0.60 per M tokens** — ~40x cheaper than Opus 4.6
- **Automatic caching** — no configuration needed, further reduces cost on repeated system prompts
- **Standard over Lightning** — same quality at half the output cost; speed (50 vs 100 tok/s) doesn't matter for agents running for hours

### DeepSeek V3.2 as Fallback

DeepSeek V3.2 ($0.28/$0.42) is the backup option. It's slightly cheaper on output but scores 7 points lower on SWE-bench (73.1%). Its cache hit pricing ($0.028/M) is exceptionally cheap for workloads with stable system prompts. Consider for:
- Observer/memory extraction (lighter reasoning needed)
- Summarization tasks
- Fallback when MiniMax API has availability issues

## Prompt Engineering for MiniMax M2.5

M2.5 was trained via large-scale RL in real coding/agentic environments. It has specific characteristics that differ from Claude or GPT models.

### Recommended Inference Parameters

```yaml
temperature: 1.0    # Official recommendation — do NOT use 0.0
top_p: 0.95
top_k: 40
```

M2.5 was trained with these sampling parameters. Using `temperature: 0.0` fights against the trained distribution and degrades quality.

### What Works with M2.5

1. **Constraints over descriptions** — M2.5 responds to flat constraint lists ("Don't change public signatures; prefer dependency injection; show a minimal diff") better than prose explanations. Structure beats cleverness.

2. **Explain "why" before "what"** — When M2.5 understands the purpose behind a constraint, it follows it more reliably. "Your response will be evaluated by a critic agent, so ground every claim in evidence" is more effective than "Be accurate."

3. **Spec-first / acceptance criteria** — M2.5 was trained to architect before coding. Prompts that ask for explicit scope boundaries, acceptance criteria, and module relationships before implementation play to its strongest mode.

4. **Role-based framing** — Assigning a specific role changes tone and standards. "You are a senior code reviewer" causes it to flag type drift and naming conventions it otherwise ignores.

5. **Chain passes, don't mega-prompt** — Break work into: Planning pass, Execution pass, Critique pass, Repair pass. This maps cleanly to our strategic/tactical phase model.

6. **Mark uncertainty explicitly** — Adding "mark any uncertainty with '(?)' and keep going" surfaces weak spots without derailing output.

7. **Short system prompts** — M2.5 may terminate tasks early when approaching context thresholds. Keep system prompt token count lean; move reference material to files the agent reads on demand.

### Known Weaknesses to Compensate For

1. **Debugging passivity** — M2.5 tends to repeatedly modify code instead of switching strategies (adding logs, writing tests, narrowing the failure). Mitigate with explicit instructions:
   ```
   When stuck on an error after 2 attempts:
   - STOP modifying the same code path
   - Add logging/print statements to narrow the failure
   - Write a minimal reproduction if the error is unclear
   - If still stuck after 3 attempts, document the blocker and move on
   ```

2. **Docs-code sync drift** — M2.5 often updates implementation without updating documentation. Mitigate by pairing code tasks with explicit checklist items covering both.

3. **Context rot on long sessions** — Quality can degrade on very long contexts. Keep total input+output under 200K tokens per sequence. Our context compaction system (Layer 0/1/2) handles this.

4. **Third-party API integration** — Can be unreliable when assembling API calls from retrieval. Constrain inputs with official documentation examples rather than relying on the model's knowledge.

5. **Weak on abstract reasoning / math / trivia** — AIME 2025: 45%, SimpleQA: 44%. The model was narrowly optimized for coding and agentic tasks. Don't use it for general reasoning.

6. **Hype prompts don't work** — "Be creative", "think outside the box" have no effect or negative effect. Use tight instructions and clear acceptance criteria instead.

### Prompt Optimization Checklist

When writing or modifying prompts for M2.5:

- [ ] Are constraints stated as a flat list, not buried in prose?
- [ ] Does the prompt explain *why* before stating *what*?
- [ ] Are acceptance criteria explicit (what artifacts exist when done, how to verify)?
- [ ] Is the system prompt as short as possible? Can anything move to instruction files?
- [ ] Are debugging strategy pivots explicitly stated?
- [ ] Does the prompt avoid vague directives ("improve quality", "be thorough")?
- [ ] Is `temperature: 1.0` set? (Not 0.0)

## Concrete Changes for This Project

### High Priority

| Change | Impact | Location |
|---|---|---|
| Set `temperature: 1.0` for M2.5 | Fighting trained distribution at 0.0 | Expert config or defaults override |
| Add `top_p: 0.95` support | Part of official recommended params | `src/core/loader.py` if not supported |
| Add debugging pivot to `tactical.txt` | Addresses known passivity weakness | `config/prompts/tactical.txt` |

### Medium Priority

| Change | Impact | Location |
|---|---|---|
| Create M2.5 prompt matrix entry | Model-specific prompt optimization | `config/prompt_matrix.yaml` |
| Shorten `instructions.md` by ~40% | Reduce token waste per LLM call | `config/templates/instructions.md` |
| Rewrite system prompt constraint-first | Match M2.5's preferred prompt style | `config/prompts/systemprompt.txt` or M2.5 override |
| Add acceptance criteria to strategic prompt | Plays to M2.5's spec-first strength | `config/prompts/strategic.txt` |

### Low Priority

| Change | Impact | Location |
|---|---|---|
| Use DeepSeek V3.2 for observer/summarization | Minor cost savings | `config/defaults.yaml` observer_model |
| Evaluate M2.5 standard vs Lightning empirically | Confirm standard is sufficient speed | Runtime testing |

## Cost Projections

Based on development usage (250M tokens / ~50 EUR):

| Scenario | Tokens/day (est.) | Daily Cost (M2.5 std) | Monthly Cost |
|---|---|---|---|
| 1 agent, moderate workload | ~30M | ~$13.50 | ~$405 |
| 1 agent, heavy workload | ~80M | ~$36.00 | ~$1,080 |
| 3 agents (self-improving loop) | ~240M | ~$108.00 | ~$3,240 |

These are rough estimates. Automatic caching on repeated system prompts will reduce actual costs. The self-improving loop at scale requires careful phase sizing to avoid runaway token consumption.

For comparison, the same workloads on Opus 4.6 would cost **~40x more** ($4,320/day for 3 agents).

## Sources

- [MiniMax M2.5 Official Announcement](https://www.minimax.io/news/minimax-m25)
- [M2.5 Usage Tips — MiniMax API Docs](https://platform.minimax.io/docs/coding-plan/best-practices)
- [M2.5 Model Card — HuggingFace](https://huggingface.co/MiniMaxAI/MiniMax-M2.5)
- [M2.5 Practical Notes — iWeaver](https://www.iweaver.ai/blog/minimax-m2-5-highlight/)
- [DeepSeek V3.2 Pricing](https://api-docs.deepseek.com/quick_start/pricing)
- [M2.5 vs DeepSeek Benchmarks — DocsBot](https://docsbot.ai/models/compare/minimax-m2-5/deepseek-v3-2)
- [LLM Pricing Comparison — TLDL](https://www.tldl.io/resources/cheapest-llm-api-2026)
- [Gemini API Pricing](https://ai.google.dev/gemini-api/docs/pricing)
