# Brief 03 — The Retrieval Substrate: RAG, Hybrid Search, Reranking & Long-Context
## Synthesized findings report

**Provenance:** Deep-research workflow run on **2026-06-06** (run id `wf_61cd98b5-856`,
task `wn664c1t2`) against `../03_retrieval_rag_hybrid_longcontext.md`.
**Run stats:** 6 search angles · 30 sources fetched · 147 claims extracted →
25 verified → **23 confirmed / 2 killed** → 10 synthesized findings · 113 agents · ~4.2M tokens.
**Raw output:** `03_retrieval_raw.json`

> This run was notably higher-quality than Brief 01: most load-bearing findings rest on
> **independent academic sources** (Adobe/ICML, Univ. Bologna/ECIR, MTEB's own maintainers,
> an open-data retrieval benchmark) rather than vendor blogs. Vendor self-benchmarks are flagged inline.
> **Headline: the single highest-ROI change is adding a cross-encoder reranker** over the fused
> candidate set — and the #2 change (gate recall instead of injecting everything) **converges with
> Brief 01's #1 recommendation**, now with independent quantitative backing.

---

## 1. Technique survey (measured impact)

**Reranking — the standout.** On T2-RAGBench (financial text+table QA, 23k queries, embeddings =
text-embedding-3-large, reranker = Cohere Rerank), adding a cross-encoder reranker over a hybrid-RRF
stack *of the same shape as ours* was the **largest single improvement of any technique tested**:
**+17.2pp MRR@3** (0.433→0.605, +39.7% rel), **+12.1pp Recall@5** (0.695→0.816), and the top
**nDCG@10 of all ten strategies (0.683 vs 0.551 for hybrid-RRF alone, 0.515 for BM25)**.
[arXiv:2604.01733 — independent, open data]. *Caveat:* single domain with unusually strong lexical
signal; Apr-2026 preprint, not yet peer-reviewed. Direction corroborated independently by
arXiv:2504.19754, which finds reranking "crucial" to realize chunking gains.

**Embeddings.** qwen3-embedding-8b (4096-d) was **#1 on MTEB-multilingual as of June 5 2025 (70.58)**
— but that's Alibaba's own announcement (vendor self-benchmark) and was **overtaken by ~Oct 2025**
(NVIDIA Llama-Embed-Nemotron-8B reported topping multilingual MTEB). It natively supports **Matryoshka
(MRL)** truncation (0.6B=1024d, 4B=2560d, 8B=4096d), so 4096-d can be truncated if storage/latency
bite. Frontier peers deliberately ship **lower** default dims with trained-in truncations (Gemini
Embedding = 3072-d default + 768/1536 MRL), evidence that **4096-d is above the quality knee** for most
tasks — not wasted, but not load-bearing. [qwenlm.github.io; arXiv:2503.07891]

**MTEB skepticism (from MTEB's own maintainers).** The leaderboard suffers (a) *distributional*
training-data contamination (models trained on data resembling benchmark task distributions) and
(b) historically, breaking changes in minor/patch releases (since corrected). **Do not pick an
embedding model on MTEB rank deltas alone — validate on your own retrieval eval set.** [arXiv:2506.21182]

**Fusion.** RRF is the standard, robust, parameter-light default; field-default k=60 (Cormack 2009),
and performance is **empirically flat across k∈[40,80]** — so our **rrf_k=50 is fine and re-tuning it is
low-value**. RRF is *not* the relevance-maximizing choice though: in OpenSearch's own BEIR test it scored
**3.86% lower nDCG@10** than calibrated min-max score-normalization fusion. But switching needs score
calibration across cosine + ts_rank and yields only single-digit-% — far below a reranker in ROI.
[opensearch.org; cormacksigir09-rrf.pdf]

**Sparse channel.** tsvector is on the weak end, **but the lexical channel itself earns its keep** for
precise domain terminology (on financial QA, BM25 *beat* dense text-embedding-3-large: Recall@5 0.644 vs
0.587 — domain-specific, do NOT generalize). Upgrading tuned BM25 → learned sparse (SPLADE lineage) buys
only **~3–6 nDCG@10 points** on a BEIR subset (BM25 44.48 → SPLADE-v3-Doc 46.97 → inference-based
SPLADE++ 50.56), and inference-free learned-sparse still trails inference-based by ~4.7pp.
[arXiv:2411.04403 — *vendor flag:* OpenSearch/Amazon neural-sparse team, 13-dataset subset]

**Chunking.** Contextual retrieval (LLM-prepended per-chunk context) and late chunking yield only
**small, model- and reranker-dependent gains (≈+1.5–4.6% nDCG)** over naive chunking. Contextual
retrieval's gain **only materializes when a reranker is added** (without it, rank-fusion *hurt*).
Late chunking is inconsistent and model-dependent (one config collapsed: 0.070 late vs 0.246 early) but
is **free** (embedding-side, no extra LLM call). [arXiv:2504.19754 independent; arXiv:2409.04701 Jina (vendor)]

**Long-context vs retrieval.** The "just use a big context window" narrative **does not hold** once
literal lexical overlap is removed (the realistic case for memory recall): on NoLiMa, **11 of 13 models
claiming ≥128K context dropped below 50% of their own short-context baseline at just 32K tokens; GPT-4o
fell 99.3% → 69.7%.** [arXiv:2502.05167 — Adobe, ICML 2025, non-vendor]. **Strong evidence to keep
retrieval precision + relevance gating central** rather than dumping memories/notes into context.

---

## 2. Comparative tradeoff tables

**Sparse / lexical methods**
| Method | Quality | Cost / effort | Verdict for us |
|---|---|---|---|
| Postgres `tsvector` (current) | Weak end | Free, in-DB | Good enough for now |
| Tuned BM25 (`pg_search`/ext) | + modest over tsvector | Medium (ext or rewrite) | Later, only if exact-term recall is the proven bottleneck |
| SPLADE / learned sparse | +~3–6 nDCG over BM25 | High (inference) | Not worth it near-term |

**Fusion methods**
| Method | Relevance | Tuning burden | Verdict |
|---|---|---|---|
| RRF (current, k=50) | Robust baseline | None (k flat 40–80) | **Keep** |
| Weighted score-norm | ~+3.86% nDCG vs RRF (1 engine) | Needs cross-scale calibration | Skip — marginal & fiddly |
| Learned fusion | Potentially higher | High | Not now |

**Rerankers** (apply *after* RRF, on top ~50–100 fused candidates, return top-k)
| Reranker | Hosting | Notes |
|---|---|---|
| Cohere Rerank | Managed API | The benchmarked one (+17pp); external dependency |
| `bge-reranker-v2-m3` | Self-host (OSS) | No external call; GPU cost; fits our infra |
| Jina Reranker v2 | Self-host | Alternative OSS option |

**Retrieval vs long-context**
| Approach | Evidence | Verdict |
|---|---|---|
| Dump everything into long context | NoLiMa: ≥128K models collapse <50% at 32K w/o lexical overlap | Avoid |
| Bounded retrieval + relevance gating | NoLiMa + Brief 01 convergence | **Target architecture** |

---

## 3. Highest-ROI improvements — ranked

1. **Add a cross-encoder reranker over the fused top-N.** Highest ROI, the only change with a large
   directly-measured lift on a same-shaped stack (+17pp MRR / top nDCG). Moderate effort. Cohere
   (benchmarked) or self-hosted `bge-reranker-v2-m3` / Jina Reranker v2. Apply after RRF on ~50–100
   candidates; return top-k.
2. **Convert per-turn full top-k injection → relevance-gated recall tool + small bounded always-on
   slice.** Medium effort. Supported by NoLiMa degradation *and* Brief 01's outlier finding.
   **The reranker's relevance score can double as the gate** (drop candidates below a threshold instead
   of always injecting top-k) — so #1 and #2 compose into one change.
3. **Tune `ef_search` up from the pgvector default of 40 → ~100–200.** Low effort, low-risk recall
   insurance at 4096-d. ⚠️ *Not benchmarked here* — no verified source quantifies recall lost at
   ef_search=40 on 4096-d. **Measure recall@k vs a brute-force ground truth on your own query set** before
   committing; don't trust the rule of thumb.
4. **Keep RRF + rrf_k≈50** (flat across [40,80]); **keep qwen3-embedding-8b** (still strong; MRL-truncate
   only if storage/latency become the bottleneck).
5. **Treat tsvector→BM25/SPLADE and contextual/late chunking as later, smaller, conditional gains.**
   Late chunking is a free cheap A/B; contextual retrieval costs an LLM call per note and only pays off
   *with* a reranker — sequence it after #1, and only if short-note retrieval is measurably weak.

---

## 4. Your specific questions, answered
- **tsvector good enough, or BM25/SPLADE?** Good enough for now. The lexical channel matters, but the
  swap buys little (~3–6 nDCG) at high effort — do it later, only if exact-term recall is the proven gap.
- **Add a reranker, which, where?** Yes — the #1 move. Cohere (benchmarked) or self-hosted
  bge-reranker-v2-m3 / Jina v2. After RRF, on the top 50–100 fused candidates.
- **Tune ef_search?** Yes, cheaply, toward ~100–200 — but measure the recall curve on your own data first.
- **Per-turn auto-injection vs gated recall tool?** Gated tool + bounded slice. NoLiMa + Brief 01 agree;
  the reranker score is the natural gate.
- **Would contextual retrieval help our notes/memories?** Marginally, only with a reranker, at an
  LLM-call-per-note cost; gains may vanish for already-short notes. Low priority.
- **Is qwen3-embedding-8b still a good choice?** Yes — defensible, current-generation, no clearly-better
  drop-in worth a migration on quality alone. MRL-truncate if cost-bound. Don't trust MTEB rank alone.

---

## 5. Open questions (must be measured, not researched)
- Recall lost at **ef_search=40 on 4096-d** in our specific HNSW setup — measure recall@k vs brute-force NN.
- Do contextual/late chunking help **short atomic notes/memories** at all? (Benchmarks are on doc corpora.)
- **Recency as an RRF channel (current 0.1) vs a post-rerank time-decay multiplier** — no source compares these.
- Which **specific reranker** wins our quality/latency/cost frontier — no head-to-head in the verified set.

## 6. Killed this round (do NOT cite)
- "FIRE-2025: RRF beat weighted fusion on every metric" — 1-2 (refuted).
- "RRF gave +38% MAP@10 over BM25 alone" — 0-3 (refuted).

## 7. Citations (primary / strongest)
- Reranker on hybrid-RRF (T2-RAGBench) — https://arxiv.org/abs/2604.01733
- NoLiMa long-context degradation (Adobe, ICML 2025) — https://arxiv.org/abs/2502.05167
- Qwen3 Embedding — https://qwenlm.github.io/blog/qwen3-embedding/ · https://huggingface.co/Qwen/Qwen3-Embedding-8B
- Gemini Embedding (dims/MRL) — https://arxiv.org/abs/2503.07891
- "Maintaining MTEB" (contamination/versioning) — https://arxiv.org/abs/2506.21182
- OpenSearch RRF (formula, k=60, RRF vs score-norm) — https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/
- Cormack et al. 2009 (original RRF, k=60) — https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf
- Learned sparse vs BM25 (vendor-flagged) — https://arxiv.org/abs/2411.04403
- Chunking eval (independent, ECIR 2025) — https://arxiv.org/abs/2504.19754
- Late chunking (Jina, vendor) — https://arxiv.org/abs/2409.04701
- Anthropic contextual retrieval — https://www.anthropic.com/news/contextual-retrieval
- pgvector HNSW tuning — https://www.paradedb.com/learn/postgresql/tuning-pgvector
