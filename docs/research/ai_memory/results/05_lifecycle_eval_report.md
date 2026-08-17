# Brief 05 — Memory Lifecycle & Evaluation (the keystone)
## Synthesized findings report

**Provenance:** Deep-research workflow run on **2026-06-06** (run id `wf_8c56ceb0-aa6`,
task `w0kk3yame`) against `../05_lifecycle_and_evaluation.md`.
**Run stats:** 5 search angles · 24 sources fetched · 119 claims extracted →
25 verified → **24 confirmed / 1 killed** → 12 synthesized findings · 106 agents · ~4.2M tokens.
**Raw output:** `05_lifecycle_eval_raw.json`

> Highest-quality run of the series — load-bearing findings rest on **peer-reviewed academic
> work** (LongMemEval/ICLR 2025, RMM/ACL 2025, MemoryAgentBench, HaluMem) and third-party
> benchmarks, not vendor blogs. This is the **research→build pivot**: it names the eval harness to
> build first and validates the reranker + gating wins from Brief 03 with external evidence.
> **Caveat that frames everything:** *nothing* validates our specific constants (observer@5,
> assembler@7, TTL@10, dedup@0.85, single importance score). The literature validates the *shape*
> of better policy; only our own harness can tune the numbers — which is exactly why eval comes first.

---

## 1. The eval harness to build first — LongMemEval-style

**LongMemEval (ICLR 2025) is the best-fit benchmark and a turnkey template** for a long-running,
multi-session, project-scoped agent. It tests five abilities most others omit — information
extraction, multi-session reasoning, temporal reasoning, **knowledge updates**, and **abstention**.
Crucially for us:
- It ships **human-annotated answer-location labels → compute Recall@k / NDCG@k directly** (score
  our pgvector+Neo4j retrieval *in isolation*, before/after the reranker).
- Its **gpt-4o LLM-as-judge hit >97% agreement with humans** (dips to ~90% on preference/abstention;
  small per-category n → report per-category error bars).
- Sessions arrive **incrementally** (parse online, answer at the end) — mirrors how our agent ingests
  turns/jobs over time, unlike single-block long-context datasets.
- LongMemEval_S ≈ 115k tokens / ~40 sessions; _M ≈ 500 sessions / ~1.5M tokens.
[arXiv:2410.10813 · github.com/xiaowu0162/LongMemEval]

**Two metrics, not one — reading is a separate bottleneck.** Even with *oracle* recall, presentation
(Chain-of-Note + JSON/structured format) swings QA by **up to 10 absolute points** across three LLMs.
So the harness must score **Recall@k (did the right memory surface?)** *and* **end-task accuracy
(did the model use it?)** separately — a reranker can lift recall yet leave end-task flat if the read
step is poor. [arXiv:2410.10813 §5.5]

**Add a contradiction-survival probe.** Conflict resolution is the field's #1 unsolved failure mode
(see §3) and is exactly our stale-fact/supersede risk — so bake an explicit probe into the harness:
inject a fact, later supersede it, then query.

---

## 2. Evaluation playbook

**Benchmark comparison**
| Benchmark | Measures | Fit for us | Caveats |
|---|---|---|---|
| **LongMemEval** (ICLR 2025) | 5 abilities incl. knowledge-updates + abstention; answer-location labels | **Build first** | synthetic needles in filler sessions; limited topical diversity |
| **MemoryAgentBench** (Jul 2025) | 4 competencies incl. **Conflict Resolution** | Failure-mode probes (esp. conflict) | conflict set synthetic (MQUAKE counterfactuals) |
| **HaluMem** (2025/26) | extraction **integrity vs accuracy**, hallucination | Extraction-quality A/B | fully LLM-synthesized |
| **LoCoMo-Plus** (2026) | Level-2 cognitive/implicit memory (causal/state/goal/value) | Aspirational stage-two | author self-benchmark, metric shift, ~0.8 judge agreement |
| **LoCoMo** (2024) | explicit factual-recall QA | **Directional only — do NOT use as primary** | ~6.4% answer key wrong; judge accepts up to 63% of wrong answers; ~9K tokens (too short); no knowledge-update |

**Metric set:** Recall@k / NDCG@k (retrieval), end-task accuracy (separately), temporal-reasoning &
knowledge-update & abstention sub-scores, plus latency & token-cost per arm.

**A/B methodology (synthesized):** on the LongMemEval-style harness, hold value/query/reading fixed
and sweep one control point at a time — reranker on/off, rerank-k, gate threshold, ef_search —
reporting retrieval *and* end-task *and* cost per arm, with per-category breakdowns and the
>97%-calibrated judge. Benchmarks have **wide dynamic range** (LongMemEval_S: long-context LLMs drop
30–60%; commercial systems 30–70% accuracy), so real changes move the needle visibly.

**LLM-as-judge:** calibrate against a human-labeled slice (target the >97% LongMemEval reports);
expect lower agreement on open-ended/abstention; never trust a judge you haven't spot-checked.

**Discount vendor self-benchmarks.** On third-party MemoryAgentBench (same backbone), **Mem0 scored
4.8% on NIAH-MQ and 0.8% on long-range summarization; MemGPT 8.8%** — contradicting their 90%+ LoCoMo
self-numbers. Cause: extraction discards content + single-pass retrieval. [arXiv:2507.05257]

---

## 3. Lifecycle best-practices (per stage) — and what they say about our defaults

**Trigger — event/boundary-driven, not fixed-interval.** The converged pattern (RMM, ACL 2025)
extracts at **session END**, not every-N-turns. For us that maps to **job/thread/phase boundaries**.
→ *Challenges our observer@5-turns.* (Open: optimal granularity for a project agent is unestablished.)

**Extract — bias toward COMPLETENESS, not precision.** For downstream accuracy, **recall of facts
beats precision**: a conservative high-precision extractor scored *highest* on memory accuracy
(92.64%) but *lowest* on QA (50.60%); QA rises monotonically with completeness. Multi-pass /
self-questioning extraction more than doubled integrity. → *Our single-importance-score conservative
posture likely depresses end-task accuracy.* **Use importance to prioritize retrieval/forgetting, not
to gate what gets written.** [ProMem/HaluMem — arXiv:2601.04463 / 2511.03506; recall>precision is the
robust lesson, absolute numbers unreplicated]

**Dedup / resolve — LLM ADD-vs-MERGE over Top-K, not a pure cosine threshold.** Converged design:
for each new memory, retrieve Top-K similar existing memories and let an **LLM decide ADD vs
MERGE/supersede** (genuine update-in-place). → Our cosine≥0.85 dedup is a fine cheap *first pass* but
does nothing for supersede/contradiction; add the LLM decision step over neighbors.

**Conflict resolution — the universal unsolved failure mode.** Across 22 systems in MemoryAgentBench,
**all score ≤6–7% on multi-hop conflict**; only long-context reaches ~45–60% single-hop. Our
cosine-dedup + single importance do nothing for multi-hop contradictions (two old facts jointly
implying a now-false answer). This is the thing to *measure* and the hardest to fix. (A 2026
deterministic recipe later reaches ~50% multi-hop — hard, not impossible.) [arXiv:2507.05257]

**Retrieve / inject — relevance-gated atomic memories beat raw logs, long-context, and summaries.**
On LoCoMo, top-5 relevant **observations** beat raw logs (+~5% F1); long-context did **not** beat
base; **summaries did not help despite high recall** (info lost in dialog→summary). Benefit "falters
with more retrieved observations" → **reduce SNR with a gate threshold**, don't inject top-k of
everything. → Direct support for Brief 03's #2 (relevance-gated recall), and a caution against
summary-only memory. [arXiv:2402.17753, GPT-3.5-era, directional]

**Index — fact-augmented key expansion is a cheap retrieval win.** Concatenating LLM-extracted facts
onto the stored memory-as-key gave **+9.4pp recall@k and +5.4pp QA across all models** (must be
concatenation — facts-*only* keys underperform). Value granularity: a single user-assistant **round**
is the sweet spot; compressing to atomic facts loses detail overall but *helps* multi-session
reasoning. [LongMemEval ablations, arXiv:2410.10813]

**Consolidate / forget — genuine evidence gaps (see §5).** No verified study isolates whether
principled forgetting improves *end-task* accuracy (vs just saving tokens), and drift-free
consolidation (our assembler@7) is unproven. These are ours to settle on our own harness.

**Reranker — load-bearing; do NOT fine-tune the embedder.** RMM: removing the reranker and
RL-fine-tuning the retriever instead **collapsed** LongMemEval from R@5 58.8%/Acc 60.2% →
34.2%/31.0% (worse than plain RAG) via catastrophic forgetting. Strongest external validation of
Brief 03's #1 — *add a reranker over candidates, don't touch qwen3-embedding-8b.* (Nuance: RMM's
reranker is RL-trained linear over Top-K dense, not an off-the-shelf cross-encoder over RRF-fused
candidates; the direction transfers, the exact mechanism doesn't.) [ACL 2025 — aclanthology.org/2025.acl-long.413.pdf]

---

## 4. The unified build plan (briefs 01 + 03 + 05)

1. **Stand up the LongMemEval-style offline harness first.** Recall@k/NDCG@k + end-task accuracy
   (separately) + a >97%-calibrated LLM-judge + knowledge-update/abstention slices + a
   contradiction-survival probe. Feed sessions incrementally. *This is the gate for everything below.*
2. **A/B the Brief 03 wins against it:** reranker on/off, rerank-k, gate threshold, ef_search —
   reporting retrieval + end-task + latency/token-cost per arm.
3. **Lifecycle changes to test (ranked by confidence × impact):**
   - Move extraction trigger every-5-turns → **boundary/event-driven** (job/thread/phase). *(high)*
   - **Bias extraction toward completeness** (multi-pass); importance prioritizes retrieval/forgetting, not writes. *(high)*
   - Replace pure cosine≥0.85 dedup with **retrieve-Top-K → LLM ADD-vs-MERGE** (supersede/update-in-place). *(high)*
   - **Fact-augmented key expansion** on the index (concat extracted facts onto memory value). *(high, cheap: +9.4pp recall / +5.4pp QA)*
   - **Structured / Chain-of-Note reading format** as a separate A/B arm. *(medium, up to +10pt)*
   - **Forgetting policy** (TTL@10 vs decay vs usage-based) — genuine open question; A/B on our harness. *(unknown)*
4. **Stage two — bespoke eval from our own production traces** (real jobs/threads with known
   later-referenced facts), scored LongMemEval-style. No public benchmark perfectly matches a
   project-scoped coding agent; eventually layer in a LoCoMo-Plus-style cognitive/implicit slice.

---

## 5. Open questions (must be settled on our own harness)
- **Forgetting/decay:** no verified study isolates whether principled forgetting improves *end-task*
  accuracy vs merely saving tokens. Closest signal is indirect (over-retrieval hurts via SNR).
  A/B TTL@10 vs decay vs usage-based ourselves.
- **Consolidation / sleep-time reflection:** under-evidenced beyond RMM's per-session topic
  decomposition; summaries-don't-help is a *caution*, not a positive design. Our assembler@7 unproven.
- **Trigger granularity for a project/coding agent:** RMM validates end-of-session for *dialogue*;
  our boundaries are job/thread/phase — no benchmark targets a coding agent.
- **Reranker + gate A/B specifics:** no validated procedure for the gate threshold or jointly tuning
  ef_search + rerank-k + gate-threshold — sweep on the harness; the operating point is system-specific.

## 6. Killed / caveats
- Killed (1): a near-duplicate LongMemEval description with an over-specific "seven question types"
  framing — 1-2. (The validated LongMemEval description is Finding 1.)
- **All major benchmarks are synthetic** (LoCoMo LLM-generated; LongMemEval needles in borrowed
  filler; HaluMem fully synthetic; MemoryAgentBench conflict = MQUAKE) → bespoke production-trace eval
  as stage two.
- **Self-benchmark flags:** RMM, ProMem, LoCoMo-Plus are authors-propose-method-and/or-metric,
  unreplicated — directional conclusions trustworthy, absolute numbers not.
- **Model/stack transfer:** quantified wins are tied to specific reader models + retrieval stacks
  (Stella-1.5B, GTE, Contriever; GPT-4o/Gemini readers) — effect sizes will differ on ours, which is
  *why* eval-first is load-bearing.

## 7. Citations (primary / strongest)
- LongMemEval (ICLR 2025) — https://arxiv.org/abs/2410.10813 · https://github.com/xiaowu0162/LongMemEval
- RMM / Reflective Memory Management (ACL 2025) — https://aclanthology.org/2025.acl-long.413.pdf · https://arxiv.org/abs/2503.08026
- MemoryAgentBench (4 competencies; conflict resolution) — https://arxiv.org/abs/2507.05257
- ProMem / HaluMem (extraction completeness vs precision) — https://arxiv.org/abs/2601.04463 · https://arxiv.org/abs/2511.03506
- LoCoMo (+ critiques) — https://arxiv.org/abs/2402.17753
- LoCoMo-Plus (cognitive/implicit memory) — https://arxiv.org/abs/2602.10715
- Generative Agents (reflection / importance·recency·relevance) — https://arxiv.org/abs/2304.03442
- Letta sleep-time compute (consolidation) — https://www.letta.com/blog/sleep-time-compute
