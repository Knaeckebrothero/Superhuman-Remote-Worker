# Brief 02 — Academic State of the Art & Cognitive Architectures
## Synthesized findings report

**Provenance:** Deep-research workflow run on **2026-06-07** (run id `wf_3112fbac-d18`,
task `wrs56drrh`) against `../02_academic_sota_and_cognitive_architectures.md`.
**Run stats:** 6 search angles · 27 sources fetched · 131 claims extracted →
25 verified → **19 confirmed / 6 killed** → 10 synthesized findings · 110 agents · ~4.3M tokens.
**Raw output:** `02_academic_sota_raw.json`

> **The headline is an asymmetry.** Every claim that *survived* verification is **descriptive**
> (what a system's architecture *is* / what a paper *proposes*) or a **survey taxonomy definition**.
> Every claim that a higher-order memory operation (reflection / consolidation / memory-evolution /
> forgetting) *causally improves end-task performance* was **REFUTED** under adversarial verification —
> Generative Agents' reflection ablation (0-3), A-MEM's memory-evolution ablation ×2 (0-3), MemGPT's
> DMR numbers (1-2), Reflexion's HumanEval 91% (1-2). **The scaffolding of agent memory is well-
> established; the efficacy of the consolidation/reflection/sleep-time layer is not — and the papers
> most cited as proof don't hold up.** This confirms *and strengthens* Brief 05's flag.

---

## 1. Canonical works

| System | Core idea | Survived (proven-as-described) | Refuted (efficacy claim) |
|---|---|---|---|
| **MemGPT / Letta** (2310.08560) | OS-style virtual context: in-context "main" vs out-of-context "external", LLM **pages** via self-issued function calls (`request_heartbeat` chaining) | Architecture | DMR 32.1%→92.5% (1-2) |
| **Generative Agents** (2304.03442) | observe → synthesize **reflections** → retrieve to plan; `score = recency + importance + relevance` | Architecture **+ the retrieval formula** | reflection ablation (0-3) |
| **Reflexion** (2303.11366) | **verbal** reinforcement; episodic buffer of self-generated reflective text across trials | Mechanism | HumanEval 91% (1-2) |
| **Self-RAG** (2310.11511) | LM decides **when** to retrieve via reflection tokens; indiscriminate fixed retrieval *hurts* | **Mechanism AND efficacy (survived!)** | — |
| **A-MEM** (2502.12110) | Zettelkasten self-organizing notes + "memory evolution" | Mechanism | LoCoMo result + evolution ablation (both 0-3) |
| **CoALA** (2309.02427) | imports cognitive LTM taxonomy: working + **episodic/semantic/procedural** | The taxonomy (descriptive) | — (made no efficacy claim) |
| **Zhang et al. survey** (2404.13501) | 3-axis taxonomy: sources × forms × operations | The taxonomy | "universally inherited" sub-clause (2-1) |

---

## 2. The headline finding — consolidation/reflection/forgetting is UNPROVEN

The canonical survey (Zhang et al., ACM TOIS 2025) defines **"memory management" as exactly the
composite our system implements** — reflection (summarizing high-level concepts) + merging (reducing
redundancy) + forgetting (removing unimportant info) — but **justifies all three purely by analogy to
the human brain, with zero controlled end-task evidence** ("these operations *can* help the agents").
Forgetting appears in only **5 of 28** surveyed systems. And decisively, **the four strongest "proof
that consolidation/reflection helps" results in the literature all failed adversarial verification.**

→ **This is the most important finding for us.** Our **assembler-LLM ≈ reflection/consolidation,
cosine-0.85 dedup ≈ merging, TTL@10 ≈ forgetting** map *cleanly* onto the field's canonical management
operation — but the field has **not** demonstrated any of them lift end-task performance. They should
be treated as **hypotheses to test (and candidates to ablate)**, not foundations to extend.

---

## 3. Taxonomy → our system (what we embody, what we lack)

- **importance × recency × relevance** (Generative Agents) ≈ our **dense+sparse+recency RRF** — we're
  aligned with the canonical retrieval blend.
- **OS-paging + function-gated recall** (MemGPT) — we have the external store but **auto-inject it all**
  instead of paging on demand. (Brief 01's outlier finding, now with the canonical citation.)
- **episodic / semantic / procedural split** (CoALA) — we have a **single undifferentiated
  importance-scored store**; the split is the principled alternative... but see §6 (unproven it helps).
- **memory management** (reflection+merge+forget) — we implement all three (assembler/dedup/TTL); the
  literature says this composite is real practice but **efficacy-unproven**.

---

## 4. What 02 CONFIRMS for the build plan (proven layer)

- **Self-RAG is the academic backing for relevance-gated recall** (Brief 03 #2 / Brief 01 bounded
  injection). Its central result — *indiscriminate fixed-passage retrieval degrades quality and injects
  off-topic passages* — is **our exact anti-pattern**, and it's a **surviving, quantified** result,
  independently corroborated by 2024–2026 work ("adding unrelated documents often hurts, sometimes worse
  than no retrieval"). The bounded-slice + gated-recall direction is now triple-confirmed (01 / 03 / 02).
- **MemGPT** is the canonical citation for bounded in-context + function-gated recall — the architecture
  we should move toward.
- **The evaluation gap is real and confirmed:** as of April 2024 there was **no** open-source benchmark
  purpose-built for agent-memory modules; task-level eval confounds memory with everything else. Every
  module-level benchmark (LongMemEval, MemoryAgentBench, MemBench) postdates it and self-describes as a
  gap-filler. → **Reinforces Brief 05's "build the harness first."**

---

## 5. What 02 CHANGES — a skeptical lens on our consolidation layer

The reframe: **don't build *more* consolidation (sleep-time, reflection passes) until the harness shows
our *existing* consolidation helps.** The assembler@7, cosine-0.85 dedup, and TTL@10 are precisely the
operations the literature cannot show pay off. So the harness (Brief 05) has a second job beyond tuning
the reranker/gate: **audit whether our own management layer earns its keep**, by A/B-ing the agent
*with vs without* the assembler / TTL / dedup. Be prepared to find some of it is cargo-culted — that
would be a *win* (simpler system, fewer background LLM calls).

---

## 6. Frontier & open problems (none yet pilot-justified)

- **Episodic/semantic/procedural split** — principled (CoALA) but **no head-to-head shows it beats a
  single undifferentiated store**. A candidate to test, not a must-build.
- **Forgetting/decay efficacy** — rare (5/28), brain-analogy only; **no surviving evidence** that
  Ebbinghaus/spacing-style decay *improves* task accuracy (vs just saving tokens). Our TTL is in the
  unproven-benefit bucket. (Open question: does pruning ever raise accuracy by removing distractors —
  cf. Self-RAG's "off-topic passages hurt" — or only ever lose information?)
- **Parametric / non-parametric frontier** (memory-in-weights, memory layers, KV-cache reuse,
  "cartridges") — **no surviving claim establishes any as practical now**; CoALA warns writing to
  weight/procedural memory is "significantly riskier." Defer.

---

## 7. Killed this round (do NOT cite)
- MemGPT DMR (GPT-4 32.1%→92.5%) — 1-2
- Generative Agents observation/planning/reflection ablation — 0-3
- Reflexion HumanEval 91% pass@1 — 1-2
- A-MEM LoCoMo result (F1 45.85 vs MemGPT 25.52) — 0-3
- A-MEM memory-evolution ablation (45.85→31.24) — 0-3
- Generative Agents "reflection causally load-bearing, degrades within 48 sim hours" — 0-3

*Note: killed = did not survive adversarial verification this pass; not necessarily false. But the
strongest published "consolidation helps" numbers are not citable as proof.*

## 8. Coverage gaps
Voyager appears only via CoALA's secondhand classification; MemoryBank/RecurrentGPT only as survey table
entries. Bi-temporal/Graphiti (Brief 04) and conflict-resolution (Brief 05's #1 open problem) produced
**no surviving claim here** — they remain backed by those briefs, not this pass.

## 9. Citations (primary)
- MemGPT/Letta — https://arxiv.org/abs/2310.08560
- Generative Agents — https://arxiv.org/abs/2304.03442
- Reflexion — https://arxiv.org/abs/2303.11366
- Self-RAG — https://arxiv.org/abs/2310.11511
- A-MEM — https://arxiv.org/abs/2502.12110
- CoALA (cognitive taxonomy) — https://arxiv.org/abs/2309.02427
- Zhang et al. survey (3-axis taxonomy) — https://arxiv.org/abs/2404.13501
- Du et al. survey (revised 6-operation scheme) — https://arxiv.org/abs/2505.00675
- LongMemEval — https://arxiv.org/abs/2410.10813
- LoCoMo — https://arxiv.org/abs/2402.17753
