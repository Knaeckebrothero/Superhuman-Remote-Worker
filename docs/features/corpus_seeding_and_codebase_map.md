---
tags:
  - feature
  - architecture
  - memory
  - knowledge-base
  - retrieval
  - codebase-map
  - ingestion
  - cost
aliases:
  - corpus seeding
  - codebase map
  - code map
  - memory seeding
  - cold-start memory
  - precomputed knowledge
  - FrankenCoder map
related:
  - "[[agent_memory_overhaul]]"
  - "[[agent_memory_current_state]]"
  - "[[memory_light]]"
  - "[[auxiliary]]"
  - "[[multi_datasource_support]]"
  - "[[no_workspace_agent_mode]]"
---

# Corpus Seeding & the Codebase Map

> **Pre-populate what the agent "knows" from an external corpus — a codebase, a
> wiki, a doc-set — without first spending weeks of real agent runs to extract it
> organically.** The corollary of the existing memory system: today the agent can
> only remember what it has *seen*; this lets it start a job already knowing things
> it has never seen. The twist that makes it cheap-and-correct: route each corpus
> into the *right* store, and amortize the one-time indexing cost across every job
> that shares the project scope.

**Status:** Design draft / **exploratory — no decision, nothing built.** Spun out
of a 2026-06-15 discussion sparked by FrankenCoder's "Codebase Map / File Summary"
feature. This is a Plan-stage doc: the goal is alignment on *which store gets which
corpus* and on the acceptance criteria, **before** any code. The good news up front:
most of the substrate already exists (see [[agent_memory_overhaul]] Phase 4), so
the first slice is small.

---

## 1. Motivation & origin

The idea arrived via a walkthrough of **FrankenCoder**, a local-first BYO-key AI
IDE. Its pitch: build a searchable semantic map of the whole codebase — every file
and method gets an AI-generated summary, plus aliases, concepts, and relationships —
exposed to the agent through `getCodeMap` / `getFileSummary` tools. The agent then
finds what it needs without aimlessly grepping, which means **fewer tool calls,
less wasted context, lower cost**. Their framing of the cost argument is the sharp
part and worth quoting in spirit:

> *"You can't control the models, but you can control the tooling. Smarter tools
> that feed the right data up front let even cheap or local models get to the
> answer fast — that's how you keep costs down on a bring-your-own-key tool where
> you're paying for every token."*

That philosophy maps cleanly onto our stack: we already run an **auxiliary LLM**
(`auxiliary.model`) for the cheap async background work, and we already scope
knowledge **per project** so it can be shared across jobs. Batch-indexing a corpus
with a cheap model and reusing it across many jobs is exactly the "control the
tooling" play, expressed in our existing seams.

**This is not a novel category** (worth saying so we don't over-credit one tool):
- **Cursor** — AST-aware chunking + embeddings in a remote vector store, re-indexed
  incrementally on every save; trained their own embedding + semantic-search model.
- **Aider** — a PageRank "repo map" over a symbol graph; structural, local, no
  embeddings.
- **Windsurf Codemaps** — AI-annotated structured maps of code.
- **OSS** — `repomix` / `gitingest` / `codebase-map` (digest the repo into one blob),
  `codebase-context` (semantic code map + conventions + memory).

Our *differentiated* angle is not "build a code map" — it's that we'd be plugging
corpus seeding into a **bi-temporal memory system that also does episodic recall**
(contradiction resolution, supersede, TTLs). None of the tools above unify a code
index with a lifecycle-managed memory. That's the position worth defending — and
it's also the trap, because it tempts us to pour everything into one store.

---

## 2. The core insight: three stores, not one

The single most important design decision here is **which store each corpus lands
in.** "Auto-create memories for a codebase/wiki" sounds like one feature; it is
actually *three distinct stores* with different properties, and putting the wrong
corpus in the wrong store is the main way this goes badly.

| | **Code Map** | **Knowledge Base** (`kb_write`) | **RecallStore** (episodic memory) |
|---|---|---|---|
| What it holds | Per-file/method summaries, symbol graph | Curated project facts, conventions, API surface | Experiential facts from agent runs |
| Defining property | **Derived state** — regenerate when source changes | Curated, **project-scoped**, injected each call | Bi-temporal, TTL'd, contradiction-resolved |
| On change you… | Re-index (no "supersede") | Re-curate / upsert | ADD / UPDATE / MERGE / NOOP / **supersede** |
| Right backend | **Its own index + tool** | `KnowledgeStore` (exists) | `RecallStore` (exists) |
| Tuned for | Structural lookup | On-demand retrieval (≈5 slots) | Episodic recall (R@5 1.0 @ ~111 tok) |

The trap to avoid: **do not pour a code map into RecallStore.** Two reasons:

1. **Retrieval pollution.** RecallStore's injection is tuned (reranker +
   relative-gate + bounded-10) for episodic recall at ~111 tokens/question. Dumping
   9,500 file-summaries into the same vector space swamps the thing that makes
   "the user prefers X" surface at the right moment.
2. **Cost & semantics.** A code summary is *derived state*, not an experiential
   fact. Running it through the Phase-4 verdict path means **one aux-LLM
   adjudication per candidate against its neighbours** — absurd at corpus scale —
   and bi-temporal supersede is the wrong model for "the file changed, re-summarize
   it." You regenerate an index; you don't retire a memory.

So the headline FrankenCoder feature (a whole-codebase map) belongs in **its own
index with its own `get_code_map` / `get_file_summary` tool and its own context
budget** — *not* in memory. Smaller, conceptual corpora (a wiki, onboarding docs,
team conventions) belong in the **Knowledge Base**, which is already exactly "facts
the agent didn't have to experience."

---

## 3. What already exists (the substrate)

Grounded in a code survey of the current memory subsystem. These seams mean the
first slice is days, not weeks. (Symbols from a 2026-06-15 read of `src/`; verify
before building.)

### RecallStore — episodic memory + the Phase 4 verdict path
`src/services/recall_store.py`
- `RecallStore.store(content, summary, keywords, importance, …)` — main write entry;
  routes to `_store_with_verdict()` when verdicts are wired, else legacy cosine-dedup.
- `_store_with_verdict()` — the ADD/UPDATE/MERGE/NOOP/supersede decision; fetches
  valid neighbours via `find_similar_many(embedding, k=5, min_similarity=0.6)`; cost
  guard ADDs with zero LLM calls when no neighbour clears the `review_floor`.
- `supersede(old_ids, new_id)` — sets bi-temporal markers (`valid_to`,
  `superseded_at`, `superseded_by`), zeroes TTL on retired rows (migration `vector/0006`).
- `_scope_where()` — applies `project_id` / `project_ids` scoping on read & write.
- `write_gate` — gates writes on `importance < threshold`.

### The ingestion seam / verdict service (currently inert)
`src/services/memory/ingestion.py`
- `maybe_attach_ingestion_verdict(recall_store, auxiliary_llm, memory_config)` — the
  glue; when `memory.ingestion.enabled` is true (default **off**), attaches an
  `IngestionVerdictService` to the store so every `store()` routes through adjudication.
- `IngestionVerdictService.adjudicate()` — calls the aux LLM via `IngestionVerdictTask`;
  never raises (degrades to ADD); prompt `config/prompts/memory_ingestion_verdict.txt`.
- **This seam is the natural home for a *curated, low-volume* episodic seed** and is
  currently consumed by nothing — seeding would be its first real consumer.

### The Observer — organic extraction from transcripts
`src/services/auxiliary.py` + `src/services/memory/plugins/legacy_writers.py`
- `extract_and_store_memories(auxiliary_llm, recall_store, messages, prompt, phase, …)`
  — windows a message list, runs `ExtractMemoriesTask`, stores each result via
  `recall_store.store()`.
- Writers (`WorkerIntervalExtractor`, `PersistentIntervalExtractor`,
  `PhaseBoundaryExtractor`, `TeardownExtractor`) fire on `turn_end` /
  `phase_boundary` / `session_end`, gated by `memory.observer_interval` (default 5).
- **Key reuse point:** this function takes a *messages window*. A corpus chunk can
  be wrapped as a synthetic messages window and run through the **same** extractor —
  "the observer, but pointed at a document instead of a transcript."

### Knowledge Base — curated, project-scoped, injected each call
`src/services/knowledge_store.py`
- Stored in PostgreSQL `knowledge_base` (Neo4j source of truth, pgvector
  write-through upsert); separate backend from RecallStore.
- `KnowledgeStore.hybrid_search(project_ids, query, match_count=5)` — retrieval; the
  KB is injected as its own "knowledge" bucket by `MemoryManager`.
- `upsert_note()` / the `kb_write` tool write directly (active-notebook pattern).
- **Already project-scoped and injected** — the cleanest seam for doc/wiki seeding.

### Project scoping & the aux LLM
- `MemoryConfig.project_scoped` (default true); both stores filter by
  `project_id` / `project_ids`. **This is the "shared pool" the idea anchored on** —
  seed once for a project, every job in that project benefits.
- Aux LLM: `auxiliary.model` / `base_url` / `api_key`; background tasks dispatched
  via `asyncio.create_task(capture(event))`, **per-turn today, no batch path** — a
  batch ingest runner is new infrastructure (see Open Questions).

---

## 4. The design constraint: staleness

The code-map category has one well-documented failure mode: **the index lags the
source.** Semantic indices "return code for renamed functions or deleted modules"
when re-indexing can't keep up with active editing. Cursor's answer is continuous
incremental re-indexing on every save; Aider's is to keep the map *structural and
cheap to regenerate* rather than embedding-heavy.

For us this has a concrete implication: **our agents edit isolated workspaces.** A
per-method summary map of the *hot workspace* goes stale within the same session and
is actively dangerous (the agent trusts a summary of code it already changed). So:

- A code map pays off on **read-mostly, shared** corpora — our own repo as a
  reference, a customer's repo at onboarding time, a stable docs set.
- It barely pays off, and can mislead, on the **mutating workspace** of a single
  job. Do **not** auto-build a map of the live workspace and trust it across edits.
- The payoff comes from **sharing** (project-scoped, reused across many
  jobs/sessions), not from one job's run — which is exactly why the project-scoped
  pool is the right substrate, as originally intuited. One job rarely earns back the
  indexing cost; fifty jobs against the same repo do.

---

## 5. Recommended approach (smallest-first)

### Slice A — Doc/wiki → Knowledge Base seeder  ·  *fast win, low risk*
Batch-ingest a conceptual corpus (conventions, onboarding docs, API surface, a wiki)
into `KnowledgeStore`, project-scoped, retrieved via the existing ~5-slot injection.
- **Why first:** reuses `KnowledgeStore` + the aux LLM end-to-end; no new index, no
  new tool (KB is already injected); project-scoped sharing is free; **zero risk to
  the tuned RecallStore retrieval space.**
- **Shape:** a runner that walks a source (uploaded docs, a `repository` datasource,
  a URL set), chunks it, has the aux LLM summarize each chunk into a KB note, and
  `upsert_note()`s it under the target `project_id`.
- **Watch:** KB injection is `match_count=5` — keep seeded volume conceptual
  (tens–hundreds of notes), not a 9,500-row firehose, or you dilute retrieval here too.

### Track B — The codebase map  ·  *bigger, separate, the headline feature*
A dedicated per-file/method summary index + `get_code_map` / `get_file_summary`
tools, batch-built with the aux/cheap model.
- **Its own store, its own tool, its own context budget** — not RecallStore, not the
  KB's 5-slot bucket.
- Batch generation maps onto `auxiliary.model` (the "9,500 files with a cheap model
  in 10 min" play).
- Must carry a **freshness/regeneration story** (§4): re-index on change, scope to
  read-mostly reference repos, never trust a stale map of the live workspace.
- Decide early: **structural (Aider/PageRank, cheap, robust) vs semantic
  (Cursor/embeddings, richer, staleness-prone)** — or a hybrid. Open question.

### Hold — Corpus-scale episodic seeding via the Phase 4 verdict seam
Tempting (`maybe_attach_ingestion_verdict` is *right there*), but **hold the
firehose.**
- A **curated, low-volume** seed of high-value episodic facts ("the team owns service
  Y", "prod deploys go through Fleet") *is* a great first real consumer of the
  verdict seam — because seeding is offline and controllable, far safer than flipping
  verdicts on the live observer.
- But corpus-scale ingest into RecallStore is the §2 trap: pollution + per-candidate
  adjudication cost. Keep bulk corpora out of episodic memory.

---

## 6. Acceptance criteria (to align on before building)

Slice A is the proposed first deliverable. Draft criteria:

- [ ] A documented entry point (CLI/API/orchestrator endpoint — TBD, see Open Qs)
      that takes `(source, project_id)` and seeds the KB.
- [ ] Seeded notes are **project-scoped** and retrieved by a *different* job in the
      same project via the normal KB injection path (no special-casing at read time).
- [ ] Seeding uses the **aux LLM**, runs out-of-band, and does not block any job.
- [ ] Re-running the seeder on the same source is **idempotent / upsert** (no
      duplicate notes).
- [ ] A measured before/after: a job that previously had to discover fact X by
      searching now has X injected from the seed (fewer tool calls / less context to
      the same outcome). Lean on the [[agent_memory_overhaul]] eval harness style.
- [ ] **No regression** to RecallStore retrieval metrics (the seed touches the KB,
      not episodic memory).

---

## 7. Open questions

1. **Trigger & ownership.** Who kicks off a seed — an admin action in Cockpit, an
   orchestrator endpoint, a job type, or a datasource-attach hook? (The
   `repository` datasource flow in [[multi_datasource_support]] is a natural input.)
2. **Batch runner.** Aux dispatch is per-turn today (`asyncio.create_task`). A
   corpus seed needs a bounded-concurrency batch runner with cost/rate limits — new
   infra. Reuse the aux client, new orchestration.
3. **Chunking.** Code wants AST/symbol-aware chunking; prose wants
   semantic/heading-aware. One pipeline with pluggable chunkers, or two?
4. **Code map: structural vs semantic vs hybrid** (§5 Track B). Drives the whole
   cost/staleness profile.
5. **Freshness.** Re-index trigger for Track B — on datasource refresh? on a
   schedule? never for live workspaces (§4).
6. **Budget guardrails.** Per-seed token ceiling; which model tier; local-model
   path for the privacy/cost-sensitive case (FrankenCoder's local-first angle).
7. **Provenance & trust.** Seeded knowledge should be distinguishable from
   agent-earned knowledge (a `source` marker) so a wrong seed can be found and
   pruned, and so the agent can weight it appropriately.
8. **Overlap with [[no_workspace_agent_mode]].** A no-workspace "answer over a
   corpus" agent and a seeded KB are adjacent — worth checking they don't reinvent
   each other.

---

## 8. Sources

- Cursor — [semantic search for the agent](https://cursor.com/blog/semsearch),
  [codebase indexing docs](https://cursor.com/docs/context/codebase-indexing)
- Aider-style — [PageRank repo map via symbol graph](https://github.com/NousResearch/hermes-agent/issues/535)
- [Windsurf Codemaps](https://cognition.ai/blog/codemaps)
- [codebase-context (OSS)](https://github.com/PatrickSys/codebase-context)
- [Agentic vs semantic code search debate (staleness)](https://wowelec.wordpress.com/2026/05/18/agentic-semantic-or-both-notes-from-the-code-search-debate/)
- FrankenCoder — `frankencoder.com` (origin of the prompt; local-first BYO-key AI IDE)
