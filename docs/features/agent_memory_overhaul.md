---
tags:
  - feature
  - architecture
  - memory
  - memory-manager
  - recallstore
  - knowledge-base
  - retrieval
  - evaluation
aliases:
  - memory overhaul
  - MemoryManager
  - memory abstraction layer
  - passive memory
  - RecallStore redesign
  - memory eval harness
related:
  - "[[agent_memory_current_state]]"
  - "[[headless_persistent_sessions]]"
  - "[[persistent_session_history_windowing_and_compaction]]"
  - "[[agent_open_source_split]]"
---

# Agent Memory Overhaul — the MemoryManager

> A **passive, model-independent memory layer**: one `MemoryManager` abstraction that
> assembles the right memories for every LLM call — the model is a *consumer*, never the
> manager. Build the seam first; everything else becomes a plugin behind it.

**Status:** Design v2 / **step 0 + pre-flight bug track complete, Phase 1 not
started** — restructured 2026-06-10 around the MemoryManager abstraction after
design alignment (v1 of 2026-06-07 was phase-first; superseded, phases preserved
below in new order). Every bug worth fixing *before* the seam is fixed
(B1/B2/B4 + logging); the remaining bugs are absorbed by phases by design.

**Implementation log:**
- **2026-06-10 — Step 0 (ground-clearing) merged to develop** as #111 + #112:
  - **B1 fixed** (#111): persistent extraction repaired — matrix-resolved prompt
    threaded from session setup into the loop + both teardown sites, real
    `observer_interval` cadence, re-resolution on `config.update`; 10 regression
    tests against the real `MemoryConfig` (`tests/test_persistent_memory_extraction.py`).
  - **B2 verified**: **zero HNSW indexes on dev AND prod** (pgvector 0.8.2) —
    all dense retrieval is a seq scan today; halfvec expression-index fix viable,
    not yet implemented. `ef_search` tuning (Phase 3) is gated on that migration.
  - **Dead-code sweep + B9 + B3 honesty** (#112, ~2,300 lines): memory_observer,
    memory_migrator, dead search helpers, `_load_query`, their tests; dead
    `MemoryConfig` knobs deleted from code + both YAMLs; persistent
    `assemble_memories.enabled: false` (config no longer lies).
- **2026-06-11 — B1 live-verified on k3d** (real session over the agent WS,
  5 turns + `/done`): in-loop trigger fired at `observer_interval`,
  `/done` teardown extracted+stored 5/5 `source='observer'` rows attributed
  to the thread, `Final memory extraction complete` logged for the first
  time ever. One aux-router timeout on the in-loop task (non-fatal, known
  flaky backend) — details + two small follow-ups in `memory_bugs.md` B1.
- **2026-06-11 — B2 fixed + k3d-verified** (migrations `vector/0002–0005`):
  dense channels of all five hybrid-search functions now order by
  `subvector(embedding, 1, 4000)::halfvec(4000)` with matching HNSW
  expression indexes (halfvec HNSW caps at 4000 dims — the doc's
  `halfvec(4096)` idea was not viable; qwen3's MRL training makes the
  4000-prefix rank-identical in testing). Premise correction: scoped
  btrees meant dense retrieval was btree+sort per scope, not a table seq
  scan — the indexes are the planner-gated hedge for scope growth.
  `hnsw.iterative_scan = relaxed_order` pinned per function. Phase 3's
  `ef_search` tuning is unblocked.
- **2026-06-11 — B4 fixed + pre-flight complete**: dimension guard inside
  `EmbeddingService` (per-response check vs `EMBEDDING_DIMENSIONS`, one
  ERROR + latched fail-fast `EmbeddingDimensionError`, background
  `verify_dimensions()` probe at both RecallStore init sites, health on
  both status endpoints; `TestDimensionGuard`, 8 cases). Plus the B1
  follow-up: the four memory catches in `auxiliary.py` log
  `type(e).__name__` so openai errors stop logging as empty strings. B9's
  enum nits deferred into the next migration touching the constraint
  (Phase 4 gc). Remaining bugs (B3/B5–B8/B10/B11) are deliberately left
  for their designed phases — the old code stays frozen so Phase-1
  equivalence fixtures pin unmodified behaviour (mapping in
  `memory_bugs.md` §Suggested order).
- **Next: Phase 1** (MemoryManager seam), starting with acceptance-criteria
  alignment.
**Companions:**
- [`agent_memory_current_state.md`](agent_memory_current_state.md) — ground truth: every
  current capability classified wired/dead/conceptual with `file:line` evidence.
- [`../issues/memory_bugs.md`](../issues/memory_bugs.md) — B1–B11, bugs fixable
  independently of this redesign. B1, B2, B4 fixed (2026-06-10/11); the
  remainder are mapped to overhaul phases in its §Suggested order.
- [`ai-memory-research/`](../../ai-memory-research) — five deep-research briefs + verified
  reports under `results/`.

---

## 1. Vision & principles

The current system was built from a correct first-principles instinct: *memories should be
discrete facts in a database, retrieved automatically by relevance — not a flat file the
model rewrites.* The research validated that instinct and exposed where the execution
drifted. This redesign commits to the original vision, properly:

**P0 — Passive. The model is a consumer, not a memory manager.** Memory "pops up"
associatively, like human recall — the model never has to *decide* to remember. This is a
hard requirement, not a preference: the platform is multi-model by design (the whole
`config/` prompt/settings matrix exists because gemma, gpt-oss, kimi, minimax, deepseek
behave differently), and any architecture that depends on the model calling a recall tool
gives frontier models a small win and weaker models amnesia, while taxing everyone's
task-focus with memory-management cognitive load. The field splits into two camps —
**model-managed** (MemGPT/Letta, frontier labs' built-in file memory: the model pages and
reflects) vs **system-managed** (Mem0, Zep: a platform pipeline extracts, stores, and
assembles). We are camp two. The proven findings transfer: Self-RAG's lesson was never
"the model must decide when to retrieve" — it was *"indiscriminate injection hurts;
retrieval must be gated."* The gate moves into the system. (The KB stays the model's
*active* notebook — deliberate `kb_write` notes when it chooses. Active note-taking is a
feature; mandatory memory self-management is a defect.)

**P1 — Foundations first. One seam before any behaviour change.** The first deliverable is
a unified `MemoryManager` interface shared by worker and persistent agents. Today the
assembly logic is forked across ~120 lines of `graph.py` and ~90 of `persistent_graph.py`
plus two store classes — and the fork is where the bugs bred (B1: persistent extraction
broken three ways; B3: assembler enabled-but-never-called on persistent). Get the
abstraction right and every idea in §4 becomes a plugin swap behind a stable interface;
get it wrong and every improvement is two-file surgery forever.

**P2 — Memories are discrete facts in shareable buckets, not files.** Individual pieces
assemble more flexibly than documents, and buckets (session / project / personal / shared)
can be attached, detached, toggled, and shared across agents and users — the same UX
pattern as datasource attachment. File-based memory is the competing paradigm and it loses
on our terms: it scales poorly, goes stale fast, and works for proprietary assistants only
because the model was trained for it.

**P3 — Staleness is a lifecycle problem, not a retrieval problem.** The
"washing-machine-ad" failure (one stale fact recalled forever — *"the homelab has L40s"*)
is the field's #1 unsolved problem, and a passive system is *more* exposed to it because
no one is curating by hand. The cure lives on the write path: ingestion verdicts
(ADD/UPDATE/MERGE/NOOP) + bi-temporal supersede (retire, don't delete). Non-negotiable
part of the target.

**P4 — Ensemble relevance, gated injection.** A memory is injected because multiple
independent signals agree it matters *for this call* — dense, sparse, trigger-phrase,
reranker, (later) learned scorer — and **nothing is injected when nothing qualifies**.
Below-threshold ≠ "inject the best of a bad lot."

**P5 — Measure, then change; measure, then cut.** No memory *behaviour* change ships
without a before/after on the eval harness (the Phase-1 refactor is behaviour-preserving
and is gated by equivalence tests instead). The existing consolidation layer
(assembler/TTL/dedup) is unproven by the field's own literature — ablate it and cut what
doesn't earn its keep.

**P6 — The manager is a kernel: config in, memories out.** Like an operating system —
essentially a collection of drivers behind one stable interface — the MemoryManager
contains almost no memory *opinion* of its own. It takes a declarative configuration
(which buckets, retrievers, scorers, policies, extensions, with what parameters), binds
the named components, and emits the memories that go into the conversation. Every
mechanism is a driver — **including the ones we currently believe aren't our future**:
model-driven paging and recall tools live on as *model-facing extensions*,
capability-gated per model family through the same matrix machinery that already varies
prompts and settings per family. Nothing is privileged; today's RecallStore behaviour is
itself just the first registered pipeline configuration, and a competing paradigm is a
config change away from an A/B, not a rewrite.

### Locked decisions (carried + new)

| # | Decision | Source |
|---|---|---|
| 1 | **System-managed (passive) memory**; recall tools are optional progressive enhancement, never load-bearing | design discussion 2026-06-10; brief 01 camp analysis |
| 2 | **MemoryManager seam first**, behaviour-preserving, shared by both graphs | design discussion; audit (fork = bug source) |
| 3 | **Eval-first for behaviour changes**; equivalence-tests for the refactor | briefs 05, 02 |
| 4 | Keep `qwen3-embedding-8b` + RRF (`rrf_k=50`); **add a reranker, don't fine-tune the embedder** | brief 03 (RMM collapse when retriever RL'd) |
| 5 | **Gate lives system-side** (ensemble score threshold), not model-side | briefs 03/05 + P0 |
| 6 | **Supersede, don't delete** — bi-temporal valid/transaction time, in pgvector columns | brief 04 (Graphiti); P3 |
| 7 | **Measure-then-cut** the consolidation layer (assembler@7 / TTL@10 / dedup@0.85) | brief 02 (efficacy refuted) |
| 8 | **Graph-keep is instrumentation-gated**; graph retrieval becomes a *plugin* we can A/B, not an architectural commitment | brief 04 + audit (graph not on hot path today) |
| 9 | No community summarization, no embedding-model swap, no parametric memory, no episodic/semantic store split in v1 | briefs 04/03/02 |
| 10 | v1 manager is an **in-process library** (`src/services/memory/`); a standalone memory service is a later option if cross-agent sharing demands it | per-call latency; simplest migration |
| 11 | **Composition is declarative config** (`memory.pipeline`: named plugins + params), resolved per expert/model-family via the existing `$extends`/matrix machinery; **model-facing extensions are a first-class, capability-gated plugin category** | P6; design discussion 2026-06-10 |

---

## 2. Target architecture

```
                       ┌─────────────────────── MemoryManager ───────────────────────┐
 before each LLM call: │  assemble(AssembleRequest) ──────────────► MemoryPayload    │
                       │                                                             │
                       │  BUCKETS     session │ project │ personal │ shared …        │
                       │              attachable · toggleable · shareable            │
                       │  RETRIEVERS  dense │ sparse │ trigger-phrase │ tags/regex   │
                       │              │ graph │ recency │ agentic-search   (plugins) │
                       │  SCORERS     RRF → cross-encoder rerank → ensemble →        │
                       │              learned model (later)                          │
                       │  POLICIES    always-inject flags · token budgets ·          │
                       │              relevance gate · latency tiers · dedup-on-     │
                       │              assemble · provenance labels                   │
                       └─────────────────────────────────────────────────────────────┘
 on lifecycle events:     capture(CaptureEvent)  ──► WRITERS (async, aux-LLM)
                          boundary extraction · trigger-phrase generation ·
                          ingestion verdicts (ADD/UPDATE/MERGE/NOOP) ·
                          bi-temporal supersede · curation · GC
```

### 2.1 The interface (sketch — Phase 1 refines)

```python
# src/services/memory/manager.py
class MemoryManager:
    """Single seam for all memory: assembly (read) + capture (write).
    Both graphs hold exactly one of these; neither touches stores directly.
    The manager is a binder (P6): from_config resolves named plugins from a
    registry and wires the pipeline — it holds no memory logic itself."""

    @classmethod
    def from_config(cls, cfg: MemoryConfig) -> "MemoryManager": ...
        # binds memory.pipeline entries via MEMORY_PLUGIN_REGISTRY
        # (same registration pattern as TOOL_REGISTRY, src/tools/registry.py)

    async def assemble(self, req: AssembleRequest) -> MemoryPayload: ...
    async def capture(self, event: CaptureEvent) -> None: ...        # fire-and-forget
    def extension_tools(self) -> list[BaseTool]: ...   # model-facing extensions, may be []

@dataclass
class AssembleRequest:
    query_text: str            # digest of the upcoming call (see §4 query formation)
    task_frame: TaskFrame | None   # todos/phase (worker) | None (persistent)
    budget_tokens: int         # total memory+KB budget for this call
    buckets: list[BucketRef]   # resolved from job/thread scope; toggleable

@dataclass
class MemoryPayload:
    blocks: list[InjectionBlock]   # ready-to-inject synthetic tool pairs
    stats: AssembleStats           # candidates/scores/tokens — harness + telemetry feed

@dataclass
class CaptureEvent:
    kind: Literal["turn_end", "phase_boundary", "compaction",
                  "session_end", "idle_archive", "todo_complete"]
    messages: list[BaseMessage]    # window to extract from
    # … scope refs, turn counters

# plugin protocols (src/services/memory/plugins/)
class Retriever(Protocol):
    async def retrieve(self, query: Query, bucket: Bucket, k: int) -> list[Candidate]: ...
class Scorer(Protocol):
    async def score(self, query: Query, cands: list[Candidate]) -> list[Scored]: ...
class Writer(Protocol):
    async def on_event(self, event: CaptureEvent, store: MemoryStore) -> None: ...
class MemoryExtension(Protocol):
    """Model-facing driver (optional): contributes tools so capable models can
    interact with memory directly — recall, pin, page, correct. Capability-gated
    per model family; the passive pipeline never depends on them (P0)."""
    def tools(self) -> list[BaseTool]: ...
```

Design notes:
- **`assemble()` is called by both graphs at the same point** (just before the LLM call,
  after context limits are ensured — unifying the current worker-after / persistent-before
  compaction divergence) and returns ready-to-inject blocks. Injection message *mechanics*
  (synthetic tool pairs, `memory_inject_`/`knowledge_inject_` prefixes, summarization
  stripping) are unchanged and move inside the manager.
- **`capture()` replaces every scattered extraction/curation call site** — the worker's
  interval + phase-boundary triggers, the persistent loop's interval trigger, the
  session-end/idle-archive calls in `persistent_app.py` (which have never worked — B1),
  and `todo_complete` queuing. One event vocabulary; writers subscribe to event kinds.
- **The KB is a bucket.** `kb_write`/`kb_update` tools keep writing as today (active
  notebook, P0); the manager *reads* the KB through the same retrieval plane with
  provenance labels — one budget, one gate, killing the separate uncapped KB block (B5).
- **`stats` is first-class.** Every assemble emits what was considered, scored, gated, and
  injected. This is simultaneously the eval-harness tap, the cockpit "why did it say
  that" surface, and (later) the learned scorer's training-data flywheel.
- **Composition is data-driven, and extensions ride existing hot-swap.** `from_config()`
  binds whatever `memory.pipeline` declares — so swapping a scorer, adding a retriever, or
  enabling a model-facing extension is a config change, not a code change, and resolves
  per expert/model family through the normal `$extends`/matrix path. Extension tools join
  the agent's tool set through the same per-turn `get_current_tools()` refresh that
  already serves model hot-swap and plan-mode toggles in persistent sessions — enabling a
  memory extension mid-session needs no new machinery.

### 2.2 Buckets

A bucket = a named, scoped memory collection with its own lifecycle policy.

| Bucket | Scope key today | Notes |
|---|---|---|
| `session` | `job_id` (threads reuse it) | per-conversation working memory |
| `project` | `project_id` | today's `project_scoped=true` behaviour |
| `personal` | *(new)* user-level | preferences, corrections — follows the user across projects |
| `shared/custom` | *(new)* arbitrary | team/org buckets, attach like datasources; on/off per agent |

v1 **layers buckets over the existing `job_id`/`project_id` columns** (no schema surgery
in the foundation phase); a real `bucket_id` migration comes with Phase 6 when
`personal`/`shared` materialize. Bucket attach/detach/toggle deliberately mirrors the
datasource-attachment UX — and bucket *sharing* is a product feature (multi-tenancy M1
tie-in), not just plumbing.

---

## 3. Current state → target mapping

Ground truth with evidence: [`agent_memory_current_state.md`](agent_memory_current_state.md).

| Today (audited 2026-06-10) | Becomes |
|---|---|
| Injection logic forked: `graph.py:888-1037` vs `persistent_graph.py:526-658` | both graphs call `manager.assemble()` |
| `RecallStore.retrieve()` two-tier: TTL-pinned tier = **whole non-expired set, relevance-blind, no LIMIT** (`recall_store.py:670`) + hybrid top-150 | always-inject = explicit **policy flag on few memories** (bounded); everything else earns injection through scoring + gate |
| KB block: hardcoded 5 notes, **no token cap** (`graph.py:971`, `persistent_graph.py:594`) | KB = one bucket on the shared plane; one budget |
| Query = top-todo+phase (worker) / last user msg (persistent) | unified **request digest** (recent window + task frame), §4 |
| Observer every 5 turns + phase boundary; persistent: ~~empty prompt + locked cadence~~ (**B1 — fixed 2026-06-10**, persistent now at worker parity for extraction) | `capture()` events; boundary-driven extraction with turn fallback |
| Assembler TTL boost/deprecate, **worker-only** (B3) | curation writer behind the seam, both paths, flag-controlled — and an explicit **ablation candidate** (P5) |
| Dedup = cosine≥0.85 merge-on-write (`recall_store.py:356`) | ingestion verdict: top-K → aux-LLM ADD/UPDATE/MERGE/NOOP |
| Trigger phrases (`retrieval_messages`, embedded, UNIONed in dense channel) — **our original idea, already wired** | kept and extended: the anticipatory channel gets a matching upgraded query side |
| Neo4j: write-mostly, never read on hot path; edges ~empty (curator default-off) | graph retrieval = optional **plugin**; pgvector self-sufficient (fixes B8 coupling); keep/cut verdict in Phase 7 |
| `MemoryConfig` knobs, several dead (B9 — **dead knobs deleted 2026-06-10**) | `memory.*` config maps 1:1 to manager components |
| Grow-only table, no GC (B6) | GC writer + retention policy |

---

## 4. The idea catalog (all of it — parked, plugin-shaped)

Everything we want the architecture to *permit*, tagged by evidence strength:
**[proven]** quantified + survived verification · **[convergent]** field-standard practice,
efficacy unproven · **[hypothesis]** ours/novel, needs the harness · **[ours-built]**
already implemented and wired today.

**Retrievers**
- Dense vector (qwen3-embedding-8b, 4096-d) **[ours-built]** — keep.
- Sparse keyword (Postgres tsvector) **[ours-built]** — keep; BM25/SPLADE upgrade optional later.
- **Trigger-phrase / anticipatory channel** **[ours-built, original]** — observer generates
  "in what future situation is this needed" phrases (e.g. login credentials → *"I need to
  log in to test this"*), embedded separately, matched against the incoming call. Neither
  Mem0 nor Zep nor A-MEM has this. Extend, don't replace.
- Recency channel **[ours-built]** — keep inside RRF.
- Tags / regex / exact-match retriever **[hypothesis]** — cheap, precise for IDs, env names, error codes.
- Graph traversal (multi-hop over KB edges) **[convergent, task-dependent]** — only wins for
  multi-hop/sensemaking recall (brief 04); ship as an A/B-able plugin, Phase 7 verdict.
- Agentic search (aux agent inspects the upcoming call and queries arbitrary sources)
  **[hypothesis]** — powerful, expensive; async/signal-gated tier only (§ latency).

**Scorers**
- RRF fusion (dense 0.6 / sparse 0.3 / recency 0.1, rrf_k=50) **[ours-built]** — keep as
  stage 1; lift hardcoded weights into config (B9 adjacent).
- **Cross-encoder reranker** over fused top-50 **[proven, +17pp MRR on a same-shaped stack]**
  — `bge-reranker-v2-m3` self-hosted; start **in-cluster CPU** (fails loudly local, no
  workstation-GPU dependency — the aux-outage lesson), promote to GPU if latency demands.
- **Ensemble gate** — inject only above a combined-score threshold; injecting *nothing* is a
  valid and common outcome **[proven as a principle — Self-RAG, gate relocated system-side]**.
- **Learned scorer** (xgboost / small NN: features = per-channel scores, memory metadata,
  request features → inject? TTL?) **[hypothesis]** — needs labeled pairs; fed by the
  `AssembleStats` flywheel + harness. Phase 7+.

**Policies**
- Always-inject flag (bounded "core": preferences, project constraints — relevance-independent
  by design) **[convergent]** — replaces TTL-pinning-as-injection; selection via explicit
  pins + curation writer, hard token cap.
- Per-call token budget across **all** memory+KB blocks (closes B5) **[ours, fixes audit gap]**.
- **Latency tiers** — fast path (vector + trigger + rerank, tens of ms) on every call;
  expensive paths (agentic search, graph walks) async or signal-gated, never blocking every
  turn. Hard `assemble()` deadline like persistent's existing 5 s guard.
- Dedup-on-assemble (don't inject near-duplicates in one payload) **[hypothesis]**.
- Provenance labels per injected item (bucket, source, age, score) **[hypothesis]** — lets
  the model weigh trust, and makes the cockpit memory panel possible.

**Writers (the passive capture side)**
- Boundary-driven extraction (phase end, compaction, session end, idle, topic shift) with
  turn-count fallback **[convergent — brief 05]**; completeness > precision: drop the
  write-time importance gate (audit: it's ~theatre anyway), gate at retrieval.
- Multi-pass / self-questioning extraction (second aux pass: "what did this window contain
  that we failed to capture?") **[convergent — brief 05]**.
- Trigger-phrase generation **[ours-built]** — extend to generate richer hypothetical
  contexts (the login-credentials pattern).
- **Ingestion verdicts**: new candidate → top-K similar → aux-LLM ADD / UPDATE / MERGE /
  NOOP **[convergent — Mem0/RMM pattern]**; replaces lossy cosine-0.85 merge; the write-side
  hook for supersede.
- **Bi-temporal supersede**: `valid_from/valid_to` + `ingested_at/retired_at` columns +
  `superseded_by` FK on `memories` (and `knowledge_index`); default retrieval filters to
  currently-valid; point-in-time queries keep history **[convergent — Graphiti; the
  washing-machine fix]**. pgvector columns first; **no graph required** (brief 04's
  decoupling insight).
- Curation writer (what's pinned in the always-on core; demotion housekeeping) — the
  assembler reborn with a different job; **ablation candidate from day one** (brief 02).
- GC / retention (closes B6: today rows below the 0.4 retrieval floor are stored forever
  yet permanently unreachable).

**Model-facing extensions (the model-managed camp, as drivers — per P6)**
Progressive enhancement for capable model families, never load-bearing (P0). Gated through
the model-family matrix exactly like prompts/settings; a weak model simply gets an empty
extension list and loses nothing.
- `recall_memory` tool — deep paging beyond the injected slice **[convergent — MemGPT]**.
- `remember` / `pin` tool — explicit writes into the always-on core or a chosen bucket
  **[convergent — frontier-lab pattern]**.
- Page/file-view extension (MemGPT-style virtual context) **[convergent]** — the
  abstraction subsumes the competing paradigm rather than fighting it; runnable as an A/B
  arm on the harness against the passive pipeline.
- Memory-correction tool ("that's outdated/wrong") — model-initiated supersede, feeding
  the same bi-temporal path **[hypothesis]**.

---

## 5. Roadmap

Phases 1–2 are **the commitment** (foundation + instrument). Phases 3+ are
evidence-driven: each lands behind a `memory.*` flag, defaults to current behaviour, and
flips only on a green harness delta (P5). Bug track B1–B11
([`memory_bugs.md`](../issues/memory_bugs.md)): **everything worth fixing before the
seam is done** — step 0 (2026-06-10): B1 + B3-honesty + B9 + vestigial-code sweep;
pre-flight (2026-06-11): B2 halfvec migrations shipped + k3d-verified (Phase 3's
`ef_search` tuning unblocked) and B4 dimension guard. The remaining bugs
(B3-wire/B5/B6/B7/B8/B10/B11) are absorbed by phases by design — the old code stays
frozen until Phase-1 equivalence fixtures pin it (mapping in `memory_bugs.md`
§Suggested order).

### Phase 1 — The foundation: MemoryManager seam · ~1–1.5 wk ← **start here**
Extract all assembly + capture logic from `graph.py` / `persistent_graph.py` /
`persistent_app.py` into `src/services/memory/` behind the §2.1 interface. Both graphs
hold one `MemoryManager`; neither touches `RecallStore`/`KnowledgeStore` directly. v1
internals = **current behaviour transplanted** (same RRF, same two-tier retrieve, same
budgets, same injection messages) — a strangler refactor, not an improvement. The
**plugin registry + `from_config()` composition ship in this phase** (P6), with current
behaviour as the sole registered pipeline — the kernel arrives first, drivers follow.

By construction this also:
- **absorbs the B1 fix** — B1 was fixed standalone 2026-06-10 (matrix prompt resolved at
  session setup + threaded into loop and teardown sites, real `observer_interval`), so
  Phase 1 transplants a *working* baseline; the session-end/idle-archive call sites
  become `capture(kind="session_end"|"idle_archive")`, and the B1 regression suite
  (`tests/test_persistent_memory_extraction.py`, pinned against the real `MemoryConfig`)
  doubles as Phase-1 equivalence collateral;
- **makes B3 truthful** — the assembler/curation writer is callable from both paths,
  flag-controlled (config honesty-fixed to `enabled: false` on persistent 2026-06-10;
  whether it *survives* is Phase 5's ablation);
- unifies query formation (request digest: recent window + task frame) — *flagged*, since
  it's a behaviour change: `memory.query.digest` default off until Phase 2 measures it;
- gives `AssembleStats` from day one (telemetry + future training data).

**Acceptance:** all existing memory/KB tests green; new equivalence tests assert
worker-path payloads are byte-stable vs pre-refactor fixtures; persistent path asserts
*parity with worker* (prompt loaded, cadence config-driven, teardown capture fires);
`graph.py`/`persistent_graph.py` contain zero direct store calls; lint+tests at file
granularity per CLAUDE.md verify loop.

### Phase 2 — Eval harness against the seam + baseline · ~1–1.5 wk
A standalone offline harness (`eval/memory/`) that drives **`MemoryManager.assemble()`
directly** — the seam makes this dramatically simpler than v1's plan (no graph spin-up).
Sessions are ingested **incrementally** (memories accrue through `capture()` as in
production, not batch-loaded). Reports per config arm:
- **Retrieval:** Recall@k / NDCG@k vs answer-location labels (LongMemEval_S first; a
  bespoke production-trace set second — no public benchmark matches a project-scoped
  coding agent; avoid LoCoMo as primary, ~6.4% wrong answer key).
- **End-task:** calibrated LLM-judge (>97% agreement target on a hand-labelled slice),
  scored **separately** from retrieval — reading is its own bottleneck (Chain-of-Note /
  answer-formatting swings QA up to +10 pt even at oracle recall, brief 05).
- **Contradiction-survival probe:** store fact → supersede → query: current or stale?
- **Cost:** tokens injected + assemble latency per arm.
- **Ablation switches:** every plugin/policy on/off via config.
Also: recall-shape instrumentation (single-hop vs multi-hop) starts accruing for the
Phase-7 graph verdict.
**Acceptance:** reproduces a published LongMemEval baseline within tolerance; A/Bs two
configs and emits deltas; **baseline numbers for the current system recorded.**

### Phase 3 — First plugin wave (measured): reranker + gate + bounded core · ~1–1.5 wk
`reranker_service` (bge-reranker-v2-m3, CPU in-cluster) as a Scorer; ensemble gate
threshold; always-inject core policy replacing Tier-1 TTL-flooding; request-digest query
on; unified token budget incl. KB (B5); `ef_search` measured-then-tuned (after B2 verdict
on whether HNSW exists at all).
**Acceptance (harness):** reranker arm lifts Recall@k and end-task; gating cuts injected
tokens with no end-task regression; bounded core + gated slice ≥ full-injection quality at
materially lower tokens/turn (baseline ceiling today: 10K-token memory budget + an
uncapped KB block on **every** call).

### Phase 4 — Lifecycle writers: verdicts + bi-temporal supersede · ~1–1.5 wk
Ingestion verdicts (ADD/UPDATE/MERGE/NOOP, aux-LLM, async); bi-temporal columns via
`migrations/vector/NNNN_bitemporal_memory.sql` (+`knowledge_index`); supersede policy
(retire-and-exclude, point-in-time queryable); boundary-driven extraction default-on;
write-gate dropped (completeness>precision). Cost guard: bound verdict calls per write.
**Acceptance (harness):** contradiction-survival probe flips to current-fact answers;
knowledge-update slice improves; no regression elsewhere; verdict-call budget held.

### Phase 5 — Ablate & cut · ~0.5 wk + data wait
A/B the inherited consolidation layer — curation writer (née assembler), TTL semantics,
dedup — via harness switches. **Remove what doesn't move the needle** (finding cargo-cult
is a win: simpler system, fewer background LLM calls). Also the vestigial-code sweep
(~1,600 lines cataloged in current-state §6).
**Acceptance:** documented keep/cut per op, each backed by a harness delta.

### Phase 6 — Buckets productized · ~1 wk
Real `bucket_id` schema (migration), `personal` + `shared` buckets, attach/toggle UX
(datasource-attachment pattern), sharing semantics (multi-tenancy M1 tie-in). Cockpit
memory panel fed by `AssembleStats` provenance (optional stretch).

### Phase 7 — Verdicts & frontier plugins · decision + experiments
- **Graph-keep verdict** from Phase-2 instrumentation: if recall is single-hop, fold KB
  source-of-truth into pgvector and retire Neo4j from the memory path (it already isn't on
  the hot path — audit); if multi-hop appears, wire the graph retriever plugin properly.
- **Learned scorer** experiments once the AssembleStats flywheel has data.
- Agentic-search retriever behind the async tier, if a use case demands it.

---

## 6. Config & rollout

- **The pipeline itself is config** (P6). Sketch — names resolve against the plugin
  registry; per-expert/model-family variation via the normal `$extends` + matrix path:

  ```yaml
  memory:
    manager:
      enabled: true            # Phase-1 cutover guard
    pipeline:
      retrievers: [dense, sparse, trigger_phrase, recency]
      scorers:    [rrf]                  # Phase 3 adds: reranker, gate
      policies:   [token_budget, ttl_pinning]   # Phase 3: core_always_inject replaces ttl_pinning
      writers:    [interval_extractor, phase_boundary_extractor,
                   trigger_phrase_gen, cosine_dedup]   # Phase 4: ingestion_verdict, bitemporal, gc
      extensions: []           # e.g. [recall_tool, pin_tool] for capable model families
  ```

- Further knobs under `memory.*` (parsed in `_parse_memory_config`, both
  `config/defaults.yaml` + `config/persistent_defaults.yaml`): `memory.query.digest`,
  `memory.reranker.{model,endpoint,candidates}`, `memory.gate.threshold`,
  `memory.core.{budget_tokens,pin_sources}`, `memory.budget_tokens` (unified, KB
  included), `memory.extraction.trigger` (`turns|boundary`), `memory.bitemporal.enabled`,
  `memory.gc.{enabled,retention_days}`, `memory.buckets.*` (Phase 6),
  `memory.hnsw.ef_search`.
- ~~Delete the dead knobs (B9)~~ — **done 2026-06-10** (`dense/sparse/recent_results`,
  `observer_model`, `observer_base_url`, `embedding_model`, `storage` removed from
  `MemoryConfig`, the parser, and both defaults YAMLs); the new interface starts from an
  honest config surface.
- Every flag defaults to current behaviour; flip per-expert via `$extends` after green
  harness runs. Migrations as numbered files under `migrations/vector/` (`.notx.sql` for
  `CREATE INDEX CONCURRENTLY`); never edit the frozen `vector_schema.sql`.

## 7. Risks

- **Refactor regression risk (Phase 1)** — mitigated by equivalence fixtures + the
  parity test suite; worker behaviour is the reference, persistent is *intentionally*
  upgraded to parity (its current behaviour is partly broken — B1).
- **Reranker latency** per turn — CPU-first sizing on top-50 candidates; the gate reduces
  reader load; measured on the harness cost arm before default-on.
- **Background-LLM cost** of verdicts/curation — async on the aux LLM, bounded calls;
  watch the aux-outage failure class (silent degradation — `AuxHealth` + B4 dim-guard).
- **Judge calibration** (>97% claim dips on preference/abstention slices) — calibrate on a
  hand-labelled slice, report per-category.
- **Benchmark mismatch** — LongMemEval is needles-in-filler; the production-trace set is
  the one that ultimately matters for a project-scoped coding agent.
- **Scope creep** — the catalog (§4) is deliberately bigger than the roadmap; only
  Phases 1–2 are committed, everything later must win on the harness.

## 8. Open questions

- Does consolidation/curation improve end-task or just save tokens? (Phase 5)
- Single-hop vs multi-hop recall split on real traces → graph verdict. (Phases 2, 7)
- Does a graph's multi-hop benefit come *entirely* from cross-document entity resolution
  — which our agent-authored Notes largely lack? (Phase 7; candidate for a targeted
  research re-run before the verdict.)
- Does forgetting/GC ever *raise* accuracy by removing distractors, or only save space? (Phase 5)
- Reranker on our latency/quality frontier: bge-reranker-v2-m3 CPU vs GPU vs API. (Phase 3)
- Bucket ACL model for `shared` (per-user grants? org-level?) — defer to Phase 6 + M1.
- Where does `personal` bucket data live for multi-tenant SaaS (per-user encryption?) — Phase 6 + M1.C.

## 9. References

- [`agent_memory_current_state.md`](agent_memory_current_state.md) — the audited baseline this doc designs against
- [`../issues/memory_bugs.md`](../issues/memory_bugs.md) — independent bug track (B1–B11; B1/B2/B4 fixed)
- [`ai-memory-research/results/01_frontier_labs_report.md`](../../ai-memory-research/results/01_frontier_labs_report.md) — bounded+on-demand norm; model- vs system-managed camps
- [`ai-memory-research/results/02_academic_sota_report.md`](../../ai-memory-research/results/02_academic_sota_report.md) — consolidation efficacy refuted; Self-RAG backs gating
- [`ai-memory-research/results/03_retrieval_report.md`](../../ai-memory-research/results/03_retrieval_report.md) — reranker #1 ROI; keep RRF/qwen3; gate by score
- [`ai-memory-research/results/04_graphs_temporal_report.md`](../../ai-memory-research/results/04_graphs_temporal_report.md) — graph task-dependent; bi-temporal in pgvector columns
- [`ai-memory-research/results/05_lifecycle_eval_report.md`](../../ai-memory-research/results/05_lifecycle_eval_report.md) — harness-first; boundary extraction; completeness>precision; conflict = #1 failure

> v1 of this doc (2026-06-07, "eval-first" phase ordering) is superseded by this
> restructure; its phase content survives above in new order. Its appendix of
> subsystem gaps moved to the current-state doc (§5–6) and the bug tracker.
