---
tags: [memory, knowledge-graph, recall-store, architecture, ground-truth]
aliases: [Memory Current State, Memory Ground Truth, What Memory Actually Does]
related:
  - "[[agent_memory_overhaul]]"
created: 2026-06-10
status: reference / ground-truth
---

> **What the memory subsystem *actually* does today — every capability classified
> as wired / dead / conceptual, with `file:line` evidence. The descriptive baseline
> we design the "optimal" system against.**

## Status

**Reference document.** Captured 2026-06-10 from a five-agent parallel code audit
(branch `develop`). Purely descriptive — no roadmap here. This is the "current
state" half of *ground-truth → optimal → roadmap*. The target design and the
migration plan live in [[agent_memory_overhaul]].

Classification legend:

- ✅ **WIRED** — present *and* called on the live runtime path
- 🔶 **PARTIAL / CONDITIONAL** — works only under certain config, or on one of the two graphs
- ⚠️ **DEAD** — implemented but nothing on the live path calls it (vestigial)
- ❌ **CONCEPTUAL** — declared in config/schema/docs but not implemented

---

## 0. The shape in one paragraph

There are **three stores and one injection path**. (1) **RecallStore** — auto-extracted
conversation/project memories in pgvector, hybrid dense+sparse+recency RRF. (2) The
**Knowledge Base** — agent-authored notes, source-of-truth in **Neo4j**, mirrored to a
pgvector `knowledge_index`. (3) The **auxiliary-LLM lifecycle** — a background *observer*
that mints memories and a background *assembler* that retunes their TTLs. Every turn, the
**execute node** re-retrieves a memory slice and a KB slice and injects them as synthetic
`recall_memories` / `kb_search` tool-call+result pairs, stripped before summarization. The
headline reality: **the Neo4j graph never influences retrieval** (it's a write-mostly store;
the pgvector mirror does all the reading), and **the curation half (assembler) only runs in
worker jobs, not interactive sessions.**

---

## 1. RecallStore (conversation + project memory, pgvector)

`src/services/recall_store.py` · table `memories` (`migrations/vector/0001_initial.sql:195`)

### Data model (columns that matter)
`id`, `content`, `embedding vector(4096)`, `sparse_keywords tsvector`, `importance`,
`remaining_turns` (TTL), `access_count`, `created_at`/`last_accessed`,
`job_id UUID NOT NULL`, `project_id UUID` (nullable), `agent_id VARCHAR(100)`,
`source` (enum). **No `thread_id` / `user_id` column** — persistent sessions reuse
`job_id` to carry the `thread_id` (`persistent_session.py:638`).

### Capability table

| Capability | Status | Evidence |
|---|---|---|
| Write via observer (aux LLM) | ✅ | `auxiliary.py:764` ← `graph.py:1554`, `persistent_graph.py:431` |
| Write via `todo_complete` (procedural) | ✅ (worker-only) | `tools/core/todo.py:225` → drained `graph.py:3442` |
| Write of compaction summary as memory | ✅ (worker-only) | `graph.py:851`, `source="compaction"` |
| Hybrid RRF read (dense+sparse+recency) | ✅ | `recall_store.py:760`; SQL `memory_hybrid_search` `0001_initial.sql:275` |
| Merge-on-write dedup (cosine ≥ 0.85) | ✅ | `recall_store.py:356` → `find_similar:518`; UPDATEs instead of inserting |
| `default_ttl=10` applied to new rows | ✅ | `recall_store.py:398`, reset on access `:837` |
| Two-tier injection (TTL-pinned + hybrid) | ✅ | `get_ttl_active:670` (tier 1) + `retrieve:857` (tier 2) |
| `retrieval_importance_floor=0.4` (read gate) | ✅ | `recall_store.py:791` — the *real* relevance control |
| `importance_threshold=0.3` (write gate) | 🔶 mostly inert | `recall_store.py:345`; every writer passes ≥0.5, so it rarely fires |
| Project-level scoping | ✅ (ON by default) | `_scope_filter:285`; `project_scoped=true` default |
| Cross-encoder reranker | ❌ | absent — grep `rerank`/`cross_encoder` = 0 hits in `src/` |
| `search_dense`/`search_sparse`/`get_recent` helpers | ⚠️ dead | `recall_store.py:558/603/637` — no callers; SQL func does its own CTEs |
| `dense/sparse/recent_results` config knobs | ⚠️ no-op | parsed (`:272`) but only feed the dead helpers above — tuning them changes nothing |

### Retrieval math (the live SQL function)
- **Dense** `embedding <=> q` over content **UNION** trigger-phrase embeddings; **Sparse**
  `ts_rank_cd` over `sparse_keywords`; **Recency** `ORDER BY created_at DESC`.
- **RRF fusion** weights **dense 0.6 / sparse 0.3 / recency 0.1**, `rrf_k=50`.
  Weights + `rrf_k` are **hardcoded** (`recall_store.py:765-767` + SQL default), *not* in `MemoryConfig`.
- `match_count` default = `max_memories_per_injection=150` (config-driven). **Not 5** — that
  number belongs to the KB block (§2).
- Importance floor `0.4` applied in every channel → memories in `[0.3, 0.4)` are **stored
  but never recalled** (write gate 0.3, read floor 0.4 — by design).

### Scoping — the definitive answer
Project memory is **real and on by default** (`project_scoped=true`). Filter is config-driven
(`_scope_filter`): if `project_scoped` and a `project_id` is present → `WHERE project_id = $1`
(spans all jobs in the project); else → `WHERE job_id = $1`. So a memory written in job A **is**
recalled in job B of the same project. **Asymmetry / latent trap:** `store()` always writes both
`job_id` and `project_id`, but a job that runs with no `project_id` (a loose job) silently
degrades to job-scope — no error, just no cross-job sharing. Project memory lives **only in the
vector store** — there is no project-memory tier in Neo4j (the graph is the *knowledge* base, §2).

---

## 2. Knowledge Base + Neo4j graph

`src/services/knowledge_store.py` (pgvector mirror) · `src/services/knowledge_graph.py` (Neo4j)
· tools in `src/tools/knowledge/knowledge_tools.py`

### Capability table

| Capability | Status | Evidence |
|---|---|---|
| Neo4j note CRUD | ✅ | `knowledge_graph.py:131/271/346/413` via `kb_write/update/read/list` |
| pgvector mirror write-through (`knowledge_index`) | ✅ | `knowledge_store.py:133` `upsert_note` |
| Hybrid RRF search over the mirror | ✅ | `knowledge_store.py:314`; SQL `knowledge_hybrid_search` `0001_initial.sql:536` |
| Auto-inject KB every turn (worker + persistent) | ✅ | `graph.py:968`, `persistent_graph.py:590` → `assemble_knowledge_block` |
| `Tag`/`Keyword` nodes + `TAGGED`/`HAS_KEYWORD` edges | ✅ written / ⚠️ never read by retrieval | `knowledge_graph.py:226-238` |
| 8 inter-note edge types (SUPERSEDES, CONTRADICTS, DERIVED_FROM, …) | 🔶 only if an LLM volunteers `links=` | declared `:65-76`; sole writer is dynamic `MERGE` `:257/:499` |
| Graph-traversal tools (`kb_related`/`kb_contradictions`/`kb_provenance`/`kb_unanswered`) | 🔶 explicit-call-only | `knowledge_graph.py:511-596`; never on the auto path |
| **Graph traversal as part of retrieval** | ❌ | no injection path touches `KnowledgeGraphDB` |
| Inline curator that would auto-write edges | 🔶 **default-OFF** | `curate_and_store_knowledge` gated on `curator.enabled=false` (`defaults.yaml:257`) |
| `Neo4jDB._load_query()` Cypher loader | ⚠️ dead + broken path | `neo4j_db.py:174`; `queries/neo4j/` dir doesn't exist; never called |

### How deep is the graph wired into retrieval? — **It isn't.**
The only two code paths that pull KB content into context (`graph.py:968`,
`persistent_graph.py:590`) both query the **pgvector RRF function** and inject the top
**`match_count=5`** notes (hardcoded at both sites). **Neither path ever calls Neo4j.** There is
zero edge traversal in retrieval — no multi-hop, no SUPERSEDES-following, no CONTRADICTS
surfacing. Stale-fact suppression happens only because the RRF function filters
`status='active'` on the **pgvector mirror** — a column filter, *not* a graph SUPERSEDES walk.
Neo4j's one irreplaceable capability (the edges) is exactly the thing retrieval ignores, and the
edges are probably mostly empty anyway because the only thing that writes them (the curator) is
default-off. **On current wiring, Neo4j is not earning its keep as a retrieval engine.**

### Other KB realities
- **Dual-write, not atomic:** `kb_write` writes Neo4j, then pgvector in a *separate* try/except;
  a pgvector failure is swallowed (`knowledge_tools.py:239`) → note exists in Neo4j but is
  invisible to all retrieval. No auto-reconciliation runs (`rebuild_from_notes` exists, no caller).
- **Neo4j-down disables pgvector-only injection too:** `has_knowledge()` requires *both* stores
  (`context.py:219`), so a Neo4j outage kills KB context injection that technically only needs
  Postgres.

---

## 3. Auxiliary-LLM lifecycle (observer + assembler)

`src/services/auxiliary.py`

| Component | Status | Evidence |
|---|---|---|
| **Observer** `extract_and_store_memories` | ✅ wired (both graphs) | `auxiliary.py:722` ← worker `graph.py:1553` + phase-boundary `:2046`; persistent `persistent_graph.py:430` |
| **Assembler** `assemble_memories` (TTL curation) | 🔶 **worker-only** | `auxiliary.py:912` ← `graph.py:1587`; **absent from `persistent_graph.py`/`persistent_app.py`** |
| Assembler effect = retune TTL (boost/deprecate), never DELETE | ✅ | `recall_store.py:714/737` — `UPDATE … remaining_turns` |
| Dedup (cosine ≥ 0.85) | ✅ on write, in `store()` | `recall_store.py:356` (not in the assembler) |
| `MemoryObserver` class | ⚠️ dead | `memory_observer.py:90` — no prod importers; superseded by `auxiliary.py` |

- **Observer:** fires every `observer_interval=5` turns (worker) as fire-and-forget
  `asyncio.create_task`; uses the **auxiliary model** (default `gemma-4-31B`, falls back to the
  summarization LLM). Extracts typed memories (factual/procedural/error_solution/vocabulary/
  relational) + synthetic `retrieval_messages` trigger phrases. **Failures are swallowed**
  (warning only); `AuxHealth` now escalates 3 consecutive failures to one alertable ERROR but
  does *not* change behaviour — the silent-drop happy-degraded path still loses the work.
- **Assembler:** runs every `assembler_interval=7` turns **in worker jobs only**. Uses an agent
  tool loop (`memory_search`/`memory_boost`/`memory_deprecate`/`memory_add_triggers`). "Removal"
  is **soft** — deprecate floors `remaining_turns` at 0 so the memory stops being injected, but
  the row persists. **Nothing in the live path ever DELETEs a memory row → the table is grow-only.**

---

## 4. Per-turn injection path — what actually reaches the LLM

`src/graph.py` execute node · `src/persistent_graph.py` · `src/core/memory_injection.py` ·
`src/core/knowledge_injection.py`

- **Shape:** both blocks injected as synthetic **tool-call + tool-result pairs** — a fake
  `recall_memories` call + `ToolMessage` (`memory_injection.py:37`) and a fake `kb_search` call
  + `ToolMessage` (`knowledge_injection.py:36`). IDs prefixed `memory_inject_` / `knowledge_inject_`.
- **Built fresh every turn**, into an **ephemeral** `prepared_messages` copy — never persisted to
  durable `state["messages"]`. Stripped before summarization via `is_workspace_injection_message`
  (`workspace_injection.py:104`).
- **The memory block is two tiers under a 10 000-token budget** (`budget_tokens`, `defaults.yaml:207`):
  - **Tier 1 — TTL-pinned: the *entire* non-expired set, relevance-blind, no row LIMIT**, ordered
    only by importance (`get_ttl_active:670`). This is the real "inject-everything" surface.
  - **Tier 2 — hybrid top-N** fills the remaining budget (`match_count=150`, floor 0.4, excludes
    pinned rows).
  - So the research's "auto-inject the full store" is **half-true**: Tier 2 *is* bounded and
    relevance-gated, but **Tier 1 force-injects every live memory regardless of relevance** until
    the budget fills. With `default_ttl=10` and the observer minting every 5 turns, the pinned set
    grows and can crowd out relevant Tier-2 hits.
- **KB block:** hardcoded **5 notes, no token budget at all** — only a fixed ~2500-token
  *estimate* reserved in compaction math, so large notes can blow past it.
- **Retrieval query differs by graph:** worker retrieves on **top pending todo + phase label**
  (never the running conversation); persistent retrieves on the **last user message** only
  (no assistant turn, no summary).

---

## 5. Bugs & latent risks (independent of the big overhaul)

> Now tracked with severities, fix sketches, and a suggested order in
> **`docs/issues/memory_bugs.md`** (B1–B10). Summary below kept for context.

1. **Persistent extraction is broken three ways by phantom config attributes.** `MemoryConfig`
   has no `extraction_interval`/`extraction_prompt` fields (the real key is `observer_interval`;
   the prompt loads via the prompt matrix, `loader.py:928` + `graph.py:3578`). (a) The in-loop
   path (`persistent_graph.py:347-351`) `getattr`-falls-through to a hardcoded 5-turn cadence
   **and an empty system prompt**. (b/c) Worse: `persistent_app.py:3732` (session teardown) and
   `:3821` (idle archive) access `.extraction_prompt` **directly** → `AttributeError` on every
   invocation, swallowed by the non-fatal except → **session-end and idle-archive extraction
   have never run, not once** — and `AuxHealth` never sees it because the failure fires before
   the helper it instruments is entered. Tracked as **B1 in `docs/issues/memory_bugs.md`**.
2. **Assembler/curator are worker-only despite config promising them.** `persistent_defaults.yaml:175`
   sets `assemble_memories.enabled: true`, but the persistent runtime never calls it. Interactive
   sessions never curate TTLs or knowledge → pinned memories only ever decrement mechanically.
3. **HNSW index is likely silently absent at 4096 dims.** Stock pgvector caps HNSW at 2000 dims;
   the index DDL is wrapped in `DO $$ … EXCEPTION … RAISE NOTICE 'Skipping'` (`vector_schema.sql:232`),
   so a build failure is swallowed and dense search silently degrades to a sequential scan. No
   `ef_search` is set anywhere. **Verify on the live DB before tuning anything.**
4. **No embedding-dimension guard.** A misconfigured embedding model that returns ≠4096 dims
   fails at INSERT, but the failure is swallowed by the non-fatal try/except at every call site →
   memory silently stops working with only a `logger.warning`.
5. **KB dual-write drift** (§2) and **Neo4j-down-kills-pgvector-injection** (§2).
6. **Dead config knobs:** `dense_results`/`sparse_results`/`recent_results` are no-ops (§1);
   `observer_model`, `observer_base_url`, `embedding_model`, `storage` are parsed into
   `MemoryConfig` but read by zero consumers (embedding model actually comes from the
   `EMBEDDING_MODEL` env var).

---

## 6. Vestigial-code catalog (safe-to-delete candidates)

| Item | Lines | Evidence |
|---|---|---|
| `MemoryObserver` class | ~469 | `src/services/memory_observer.py` — no prod importer; superseded by `auxiliary.py` |
| `MemoryManager` (+ `workspace_memory`/`workspace_template`) | ~273 | `src/managers/memory.py` self-deprecated; instantiated (`graph.py:3539`) but no method ever called |
| `memory_migrator.py` | ~427 | `src/tools/knowledge/memory_migrator.py` — not in `TOOL_REGISTRY`, imported nowhere |
| `Neo4jDB._load_query()` + `queries/neo4j/` | — | `neo4j_db.py:174` — phantom directory, never called |
| `phase_archive` + `tool_error` source enum values | — | in CHECK constraint + stats query, never written (`todo` **is** written — `tools/core/todo.py:229`) |
| `agent_id` column on `memories` | — | written (`recall_store.py:421`) but never used in any WHERE/scope clause |
| `search_dense`/`search_sparse`/`get_recent` | — | `recall_store.py:558/603/637` — superseded by the SQL RRF function |
| `tests/test_memory_observer.py` | — | exercises the dead class; passes green, misleadingly implies "observer is tested" |

---

## 7. Worker vs persistent — divergence summary

| Aspect | Worker (`graph.py`) | Persistent (`persistent_graph.py`) |
|---|---|---|
| Memory read/inject | shared `RecallStore.retrieve()` | identical (same code) |
| Retrieval query | top todo + phase label | last user message |
| Observer (extract) | every 5 turns, modulo gate | wired but **empty prompt + locked 5-turn** (bug #1) |
| Assembler (TTL curation) | ✅ every 7 turns | ❌ never runs |
| Knowledge curation | 🔶 default-off | ❌ never runs |
| Compaction summary → memory | ✅ stored as memory | ✗ |
| Retrieval timeout | none | 5 s `wait_for` (skips injection if embeddings hang) |

---

## 8. Implications for the optimal design

Carried into [[agent_memory_overhaul]] as the gap list the target design must close:

- The graph buys nothing on the hot path → **either wire traversal into retrieval or stop paying
  for Neo4j** (the "is Neo4j earning its keep" question is now answered for *current* wiring: no).
- Tier-1 relevance-blind pinning is the actual anti-pattern, not hybrid search → **bounded slice +
  relevance gate + reranker** (the triple-confirmed research direction) target Tier 1 specifically.
- The consolidation layer (assembler/TTL/dedup) is **worker-only, grow-only, and unproven** →
  prime ablation candidates once an eval harness exists.
- Persistent is the degraded path (bug #1, no curation) yet probably the most-used → fixing the
  persistent wiring may be the highest near-term ROI, independent of the overhaul.
