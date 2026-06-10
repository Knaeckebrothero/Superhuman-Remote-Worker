# Brief 04 — Knowledge Graphs & Temporal Memory
## Synthesized findings report

**Provenance:** Deep-research workflow run on **2026-06-06** (run id `wf_cfca79af-aa6`,
task `wrnvxk0qr`) against `../04_knowledge_graphs_and_temporal_memory.md`.
**Run stats:** 5 search angles · 22 sources fetched · 107 claims extracted →
25 verified → **21 confirmed / 4 killed** → 9 synthesized findings · 104 agents · ~4M tokens.
**Raw output:** `04_graphs_temporal_raw.json`

> The core verdict (graphs are task-dependent, not graph-dominant) is **independently replicated
> across two academic groups** — the strongest evidence in this report. The flagship "graph memory
> beats vector" magnitude numbers (Zep, Microsoft) are **vendor-self-reported and unreplicated** —
> directional only. The decisive question for us turns out to hinge on a fact only our own logs can
> answer: **are our recall queries actually multi-hop, or single-hop?**

---

## 1. The core verdict — graph-vs-vector is task-dependent, NOT graph-dominant

Independently replicated (GraphRAG-Bench, ICLR'26, arXiv:2506.05690 · Han et al., arXiv:2502.11371 — *neither vendor*):
- **Graphs win** on multi-hop reasoning, corpus-wide "sensemaking"/summarization, creative synthesis.
- **Vector RAG matches or beats graphs on simple single-hop fact retrieval**, where graph structure
  injects redundant/noisy context that *actively degrades* answer quality.
- Quantified: HippoRAG hits 87.9–90.9% evidence recall on complex (L2–3) questions vs basic RAG's
  64.5% — **but on simple fact retrieval basic RAG (83.2%) beats HippoRAG (80.4%)**. On MultiHop-RAG,
  graph wins multi-hop by only **~3pt**, RAG wins single-hop NQ (64.8 vs 60.0), and **13.6% of queries
  are graph-only-answerable while 11.6% are RAG-only — neither subsumes the other.**
- Margins are modest and *method-dependent*: some KG-GraphRAG variants score 41–48% and **lose even on
  multi-hop**. Graphs are not uniformly strong even in their winning category.

→ **The right architecture is complementary/adaptive, not graph-everywhere.**

---

## 2. Bi-temporal modeling — the field's answer to our #1 failure mode

This is the most decision-relevant finding. **Zep/Graphiti resolves stale-fact/contradiction via a
bi-temporal model:** every edge carries **four timestamps** — *valid-time* (`t_valid`/`t_invalid` =
when the fact was true) + *transaction-time* (`t_created`/`t_expired` = when the system learned/retired
it). Contradictions are resolved by **LLM-detected edge INVALIDATION, not deletion**: set the old
edge's `t_invalid` to the new edge's `t_valid`, prioritize newer info. Superseded facts are **retained
with an end-date and remain point-in-time queryable** (non-lossy supersede).

This is *exactly* what our `CONTRADICTS`/`SUPERSEDES` edges gesture at but don't enforce. Two things
make it trustworthy: it's an **architecture/mechanism** claim (not a perf claim, so vendor-skepticism
doesn't apply), and it's **corroborated by a separate party (Neo4j) and the live open-source
`getzep/graphiti` code** (`valid_at`/`invalid_at`/`expired_at`). [arXiv:2501.13956]

**Key decoupling insight:** bi-temporal *supersede semantics* are the fix for Brief 05's #1 unsolved
failure (multi-hop conflict, ≤6–7% across all systems) — **and you can implement them with or without
a graph.** Timestamp columns + a "currently-valid" filter in pgvector deliver non-lossy supersede +
point-in-time filtering for *single-hop* recall. The **graph** is only *required* if you also traverse
**multi-hop** relations (`DEPENDS_ON`/`DERIVED_FROM` chains) at recall time. So the supersede fix is
separable from the graph-vs-vector question.

*Cost caveat:* Graphiti runs **many LLM calls per episode** for contradiction detection — needs sizing
for an agent that's already heavy on per-turn work.

---

## 3. Construction & ontology — our fixed single-prompt schema is the scaling-limited approach

- The field is shifting **away from fixed single-prompt schemas** toward staged open-extraction +
  post-hoc canonicalization (EDC, EMNLP 2024) and bottom-up/schema-free induction (LLM-KGC survey).
  A fixed schema must be embedded in the extractor prompt, and **a richer ontology easily exceeds the
  context window** — a hard ceiling for the approach we run. (Nuance: top-down ontology is still
  defended for precision/consistency; it's "two complementary directions," not abandonment.)
- **LLM extraction is noisy and model-dependent:** different LLMs produce structurally divergent graphs
  from identical input (one model emitted +63% nodes / +118% edges vs another; 1.66×/2.18× spread
  across 6 models). And reported KG-quality scores are often **LLM-judged (no gold standard) and
  inflated by the judge.** This is the engineering risk behind *any* agent-authored graph.
  [arXiv:2510.20345, 2510.11297]

---

## 4. Community summarization — default NO for memory

GraphRAG's community summaries (Leiden clustering + pregenerated per-community summaries) are motivated
by **global, corpus-wide "sensemaking"/QFS** questions ("what are the main themes?") — *not* retrieval,
and not the targeted single-hop recall that dominates agent memory. They carry a measurable
**token-inflation/noise liability** (MS-GraphRAG global inflates prompts to ~40k tokens; degrades
retrieval relevance on simpler queries). And the expensive indexing is largely avoidable — LazyGraphRAG
indexes at **vector-RAG cost (0.1% of full GraphRAG)** by deferring LLM use to query time.
→ Adding community summaries would only pay off if the agent routinely asks global "themes across all
my notes" questions — and even then, defer. [arXiv:2404.16130; MS LazyGraphRAG blog; arXiv:2506.05690]

---

## 5. Verdict for our Neo4j + pgvector stack (ranked)

1. **Implement bi-temporal supersede semantics — YES (highest leverage).** Add valid-time
   (`valid_from`/`valid_to`) + transaction-time (`ingested_at`/`retired_at`); on a detected supersede,
   set the old fact's `valid_to` = new fact's `valid_from` rather than deleting. Non-lossy,
   point-in-time queryable, and lets injection **filter to currently-valid facts**. This is the correct
   answer to Brief 05's #1 unsolved failure (stale-fact/contradiction). *Can live in Neo4j edges OR
   pgvector timestamp columns* (see #4 below).
2. **Wire `CONTRADICTS`/`SUPERSEDES` into a real invalidation policy — they're dead weight as-is.**
   `SUPERSEDES` should mechanically retire the old note (set `valid_to`/`retired_at`) and **exclude it
   from default injection**; `CONTRADICTS` should **surface a conflict for resolution**, not silently
   co-inject both sides. Prefer "invalidate (timestamp) over delete" so history is recoverable.
3. **Do NOT add community summarization** — wrong move for memory; it's a global-sensemaking tool with a
   noise/cost penalty on targeted recall.
4. **"Is the Neo4j graph earning its keep?" — CONDITIONAL, and answerable only from our own logs.**
   Graphs earn their keep when recall is **multi-hop/relational** AND cross-document entity connectivity
   matters. If our actual recall is mostly single-hop "find the relevant note(s)" — which the reranker +
   relevance-gated-recall direction (Brief 03) implies — then **pgvector + lightweight metadata (tags,
   timestamps, supersede pointers as columns) could absorb even the bi-temporal supersede**, and the
   graph edges closer to over-engineering. The graph clearly earns its keep **iff** (a) bi-temporal
   invalidation + multi-hop traversal (`DEPENDS_ON`/`DERIVED_FROM`) is actually *exercised at recall*,
   or (b) conflict-resolution is implemented as graph traversal.
   **VERDICT: don't rip out Neo4j; make it earn its keep by adding bi-temporal invalidation + a real
   retire-and-exclude policy — that single change converts the largest existing liability (stale-fact
   injection) into the graph's strongest justification.** First, instrument recall to learn the
   single-hop-vs-multi-hop split; that decides whether the supersede fix lives in the graph or in
   pgvector columns.

---

## 6. Verified-vs-claimed benchmark table

| Claim | Source | Status |
|---|---|---|
| Bi-temporal 4-timestamp edges + LLM edge-invalidation (mechanism) | Zep/Graphiti arXiv:2501.13956 + getzep/graphiti code + Neo4j blog | **Verified** (mechanism; independently corroborated) |
| Graphs win multi-hop / lose single-hop (task-dependent) | GraphRAG-Bench ICLR'26 + Han et al. | **Verified** (replicated, 2 academic groups) |
| HippoRAG 87.9–90.9% complex vs basic RAG 64.5%; basic RAG 83.2% > HippoRAG 80.4% simple | GraphRAG-Bench | **Verified** |
| 13.6% graph-only / 11.6% RAG-only answerable | Han et al. (MultiHop-RAG) | **Verified** |
| Zep LongMemEval +18.5% acc, ~90% latency, 115k→1.6k tokens | Zep arXiv:2501.13956 | **Vendor self-reported, unreplicated**; sibling LoCoMo claim corrected 84→58% |
| MS GraphRAG 72–83% comprehensiveness/diversity win | Microsoft arXiv:2404.16130 | **Vendor self-reported**, LLM-judged w/ position/length/trial bias; not QA accuracy |
| LazyGraphRAG 0.1% index cost (= vector RAG) | MS blog | Vendor; echoed not re-measured; mechanism sound |
| LazyGraphRAG wins global at 4% query cost | MS blog | **Refuted (0-3)** |

---

## 7. Open questions (settle from our own logs / a re-run)
- **Does the graph's benefit come *entirely* from cross-document entity resolution?** A Proposition-1
  claim (without entity resolution, Graph-RAG mathematically reduces to vanilla vector RAG) was *killed
  this round (1-2)* but is the single most decisive input to "earning its keep" — **our Notes are
  agent-authored with Tag/Keyword nodes, NOT cross-document entity-resolved**, so if true the graph may
  already be close to vector-equivalent. Deserves a dedicated re-verification.
- **What are our actual recall query patterns** (single-hop vs multi-hop traversal)? The whole verdict
  pivots on this — answerable from our own recall logs, not the literature.
- **Has any neutral party reproduced Graphiti's bi-temporal benefit** vs a strong vector + recency
  baseline on LongMemEval's multi-hop-conflict subset? None found — the key missing datapoint for
  sizing the ROI of bi-temporal.
- **Maintenance/latency cost** of bi-temporal edge invalidation at our scale (many LLM calls/episode)?

## 8. Killed / caveats
- Killed (4): three KG-quality claims from arXiv:2510.14271 (LLM KGs systematically noisy; pruning ~40%
  preserves/improves QA; the Proposition-1 reduction) all 1-2; LazyGraphRAG "wins global at 4% cost" 0-3.
- **Vendor-skepticism honored:** Zep/MS magnitude numbers are directional only; the *mechanisms*
  (bi-temporal, community summaries) are solid, the *magnitudes* of benefit over a well-tuned vector
  store are **not independently established**.
- Time-sensitivity: most sources Oct 2025–Mar 2026; Graphiti is actively developed; the two graph-vs-
  vector benchmarks are recent (ICLR'26) and not yet "textbook."

## 9. Citations (primary / strongest)
- EDC — staged open-extraction + canonicalization (EMNLP 2024) — https://aclanthology.org/2024.emnlp-main.548/
- LLM-KGC survey (schema-based vs schema-free) — https://arxiv.org/abs/2510.20345
- "Are LLMs Effective KG Constructors?" (model divergence) — https://arxiv.org/abs/2510.11297
- Zep / Graphiti (bi-temporal, edge invalidation) — https://arxiv.org/abs/2501.13956 · https://github.com/getzep/graphiti
- Neo4j Graphiti blog (independent corroboration) — https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/
- Microsoft GraphRAG ("From Local to Global") — https://arxiv.org/abs/2404.16130
- LazyGraphRAG — https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/
- GraphRAG-Bench / "When to use Graphs in RAG" (ICLR'26) — https://arxiv.org/abs/2506.05690
- Han et al. systematic graph-vs-vector eval — https://arxiv.org/abs/2502.11371
- LLM-as-judge bias critique — https://arxiv.org/abs/2506.06331
- KG-quality/pruning (killed here, flagged for re-verify) — https://arxiv.org/abs/2510.14271
