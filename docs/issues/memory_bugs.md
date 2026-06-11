# Memory subsystem bugs (2026-06-10 audit)

## Context

Surfaced by the five-agent code audit behind
`docs/features/agent_memory_current_state.md` (the ground-truth map for the
memory overhaul), then hand-verified against source before being written down
here. This doc tracks **bugs and operational risks that are fixable
independently of the redesign**. Design-level flaws (relevance-blind Tier-1
TTL pinning, missing reranker/relevance gate, graph-not-wired-into-retrieval,
single-store taxonomy) are deliberately **not** here — they're the subject of
`docs/features/agent_memory_overhaul.md`.

Severity overview:

| # | Bug | Severity | Effort | Status |
|---|---|---|---|---|
| B1 | Persistent memory extraction broken 3 ways (phantom config attrs) | **HIGH** | ~1 h | **✅ fixed 2026-06-10** (PR #111) + **live-verified on k3d 2026-06-11** |
| B2 | 4096-dim HNSW indexes silently skipped → seq-scan retrieval | **HIGH** | 0.5–1 d | **✅ fixed 2026-06-11** (migrations vector/0002–0005, subvector-4000 halfvec — see section for premise corrections) + verified on k3d |
| B3 | Assembler enabled-but-never-called in persistent sessions | MED-HIGH | 5 min (honesty) / ~1 d (wire) | **honesty fix ✅ 2026-06-10** (PR #112) — wire-vs-retire deferred to overhaul Phase 5 |
| B4 | No embedding-dimension guard → silent total memory outage | MED | ~2 h | **✅ fixed 2026-06-11** (guard + probe + /status in EmbeddingService) |
| B5 | KB injection block has no token budget | MED | ~0.5 d | open |
| B6 | Memory table is grow-only — nothing ever deletes rows | MED (slow burn) | policy + ~0.5 d | open |
| B7 | KB dual-write drift, fail-open (note in Neo4j, invisible to retrieval) | MED-LOW | ~0.5 d | open |
| B8 | Neo4j-down kills pgvector-only KB injection too | LOW-MED | small | open |
| B9 | Dead/misleading config keys (tuning no-ops) | LOW (hygiene) | ~1 h | **✅ keys deleted 2026-06-10** (PR #112) — enum nits still open |
| B10 | Injection-strip prefix registry is silently fragile | LOW (latent) | test guard | open |
| B11 | End-Session button (detach path) skips final extraction — only `/done` + idle extract | MED | small now / Phase-1 `capture()` properly | open (found 2026-06-10) |

---

## B1 — Persistent memory extraction is broken three ways (phantom config attrs)

**The headline bug.** Three call sites read attributes off `MemoryConfig`
(`src/core/loader.py:1255-1280`) that **do not exist on the dataclass** — the
real fields are `observer_interval` and (for the prompt) the prompt-matrix
loader, not config attributes at all.

**(a) Session-end final extraction has NEVER run.**
`src/api/persistent_app.py:3732` (session teardown) and `:3821` (idle archive)
do a **direct** attribute access:

```python
memory_extraction_prompt=_session.config.memory.extraction_prompt or "",
```

`MemoryConfig` has no `extraction_prompt` → `AttributeError` on **every**
invocation, raised while evaluating the kwargs — *before*
`extract_and_store_memories` is even entered — and swallowed by the
surrounding `except Exception` as `"Final memory extraction failed
(non-fatal): ..."` at WARNING. Consequences:

- The "capture the conversation's memories before teardown" path has a 100 %
  failure rate since it was written.
- `AuxHealth` (docs/issues/surface_silent_aux_failures.md) never sees it,
  because `record_failure` lives *inside* the helper that never starts. The
  observability net added for aux failures has a structural blind spot for
  caller-side failures.

**(b) In-loop extraction runs with an empty system prompt.**
`src/persistent_graph.py:350-351` uses a `getattr` fallback:

```python
extraction_prompt = getattr(memory_config, "extraction_prompt", "") ...
```

→ always `""`. Interactive extraction runs with **no instructions**; only the
structured-output schema constrains it. Meanwhile the worker graph loads the
real prompt via the matrix: `load_auxiliary_prompt(config,
"memory_extraction", model=aux_model)` (`src/graph.py:3578`), mapped at
`loader.py:928` to `memory_extraction_prompt.txt`.

**(c) In-loop cadence ignores config.** `src/persistent_graph.py:347-348`
reads `getattr(memory_config, "extraction_interval", 5)` — the real key is
`observer_interval` — so the cadence is locked to the hardcoded 5 and the
YAML knob is dead on the persistent path.

**Fix sketch** (~1 h + test):
- `persistent_graph.py`: read `memory_config.observer_interval`; accept the
  extraction prompt as a parameter threaded from session setup.
- Session setup (`persistent_session.py` / wherever the aux LLM is built):
  load the prompt once via `load_auxiliary_prompt(config, "memory_extraction",
  model=aux_model)` exactly like `graph.py:3578`, pass it to both the loop and
  the two `persistent_app.py` teardown sites.
- Add a regression test that fails on phantom-attribute access (e.g. assert
  the kwargs construction works against a real `MemoryConfig()`); the
  `getattr`-with-default pattern is what let (b)/(c) hide.

**Why it matters:** persistent sessions are the primary interactive surface;
all three heads degrade or kill extraction exactly there.

**Status — FIXED 2026-06-10** (PR #111, merge `b9108b73` on develop):
- New `resolve_memory_extraction_prompt()` in `src/api/persistent_session.py`
  loads the prompt once at session setup via the prompt matrix (aux →
  summarization → main model precedence, mirroring `graph.py`), stored on
  `PersistentSession.memory_extraction_prompt`, and **re-resolved on runtime
  `config.update`** (a model swap can change the prompt-matrix family).
- Threaded as an explicit `memory_extraction_prompt` parameter into
  `run_persistent_loop` and used at both `persistent_app.py` teardown sites.
- The loop reads `memory_config.observer_interval` as a **direct attribute
  access** — a future phantom attribute now fails loudly at loop start.
- 10 regression tests in `tests/test_persistent_memory_extraction.py` pin the
  wiring against the **real** `MemoryConfig` dataclass (MagicMock fabricating
  phantom attrs is what let all three heads hide), incl. an in-process run of
  the real `extract_and_store_memories` asserting the prompt reaches the
  aux-LLM task.

**✅ LIVE-VERIFIED 2026-06-11 on k3d** (thread `0c887768-98a5-4148-8495-0730d0c6fe35`,
session driven over the agent WS exactly like the cockpit — 5 user turns with
distinct memorable facts, then `{method:"archive"}` = `/done`):

1. **in-loop trigger ✓** — `Memory extraction triggered at turn 5` (DEBUG) at
   the configured `observer_interval: 5`. The *task* it spawned then failed
   non-fatally after exactly 120 s (`Memory extraction failed (non-fatal): `
   from `src.services.auxiliary` — aux `gemma-4-moe-strix` via ai.h4ll.app
   timed out on that one call; same router served all 5 chat turns and the
   teardown extraction fine). Wiring verified; the flake is the known-flapping
   aux backend, not the fix.
2. **`/done` teardown ✓** — `Memory extraction: extracted 5, stored 5 (phase 0)`
   + `Final memory extraction complete` (INFO, first time it has ever logged)
   + `Session archived`.
3. **rows ✓** — 5 `source='observer'` rows in `memories` with
   `job_id=<thread_id>`, one per fact fed in (uv standardization, Postgres
   17.2/node4, <400-line PRs, v3.2 freeze, grafana URL). The k3d memories
   table was empty before the test, so attribution is unambiguous.

Idle-archive (signal `Idle archive: memory extraction complete`) was not
exercised — it shares the same fixed call path as the archive teardown and
needs a 30-min wait; acceptable to leave to soak.

Two small follow-ups noticed during the run:
- ~~`src/services/auxiliary.py`'s extraction catch logs bare `str(e)` → empty
  message for openai-style exceptions~~ **✅ fixed 2026-06-11**: the four
  memory-relevant catches (store-loop, extraction, curation, assembly) now
  log `type(e).__name__: e`, matching the `persistent_graph.py` retrieval
  handlers (see
  `docs/issues/persistent_graph_misleading_embedding_connection_error.md`).
- A failed in-loop extraction still advances `_last_extraction_turn`, so the
  next in-loop attempt is a full interval away; the teardown extraction is
  the safety net that recovered it here (fire-and-forget semantics, by design
  — worth keeping in mind for Phase-1 `capture()` retry policy).

NB the discovery below (B11) — ending via the cockpit ✕-button does NOT
exercise the teardown extraction; only `/done` and idle timeout do.

---

## B11 — Most real session endings skip final extraction (detach path has no capture)

Found during the B1 live-verify attempt. There are **three** ways a session
ends, and only two of them extract:

| Ending | Path | Final extraction? |
|---|---|---|
| `/done` slash command | cockpit WS `{method:"archive"}` → `_handle_archive` | ✅ (since B1 fix) |
| idle timeout | `IdleTimeoutError` → `_loop_completion_handler` → `_handle_idle_archive` | ✅ (since B1 fix) |
| **cockpit End-Session button** | `DELETE /api/persistent/threads/{id}` → agent `/session/detach` → `_terminate_session` | ❌ never |

`_terminate_session` (persistent_app.py) does git commit/push and resource
cleanup but has no memory-extraction step, and it's also the route for
drain, thread-status watchdog, boot-WS timeout, and shutdown. Since the
✕-button is arguably the *most common* deliberate ending, the
"capture the conversation before teardown" feature still misses most real
endings even after B1.

**Fix direction:** don't bolt extraction into `_terminate_session` ad-hoc —
this is exactly the overhaul's Phase-1 `capture(kind="session_end")` event;
route ALL terminate reasons through one capture call (with a
guard against double-extraction when archive already ran). If a quick win
is wanted earlier: call the same extraction block from `_terminate_session`
when the loop didn't already archive (~the B1 pattern, one more call site).

Related observation from the same investigation (cosmetic): `agents.status`
can read `offline` for a pod that is Running but whose session app has
exited — heartbeat bookkeeping, worth a glance when debugging "agent
unreachable" forwarding errors.

---

## B2 — All 4096-dim HNSW indexes are silently skipped (verify, then fix)

Every vector index on the 4096-dim columns is created inside:

```sql
DO $$ BEGIN
    CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (...);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Skipping HNSW index on ... (dimension > 2000 or other error): %', SQLERRM;
END $$;
```

The notice text itself admits the expectation: **stock pgvector caps HNSW at
2000 dims**, vectors are `vector(4096)`. Sites in
`orchestrator/database/migrations/vector/0001_initial.sql`:
`:184` (source_embeddings, ef_construction=64), `:247` (memories, 256),
`:267` (memory_retrieval_messages, 256), `:529` (knowledge_index, 256).

If the deployed pgvector can't index 4096 dims, **every dense retrieval —
memories, trigger phrases, KB notes, sources — is a sequential scan**, on
every turn, growing linearly with store size (compounded by B6's grow-only
table). Correct results, invisible latency cliff.

**Step 1 — verify on the live DB** (dev + prod):

```sql
SELECT tablename, indexname FROM pg_indexes
WHERE tablename IN ('memories','memory_retrieval_messages',
                    'knowledge_index','source_embeddings')
  AND indexdef ILIKE '%hnsw%';
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

**Step 2 — fix options** (decide after verification):
1. **halfvec expression index** — pgvector ≥ 0.7 supports HNSW over
   `halfvec(4096)`: `USING hnsw ((embedding::halfvec(4096))
   halfvec_cosine_ops)` + matching cast in the query. No schema change,
   negligible recall loss.
2. Reduce embedding dimensionality (qwen3-embedding supports MRL-style custom
   output dims) — bigger change, touches stored data; belongs to the overhaul
   if chosen.
3. Accept seq-scan but instrument retrieval latency so the cliff is visible.

Also: **no `ef_search` is set anywhere** (`SET hnsw.ef_search` absent from
`src/` and migrations) — moot until an index actually exists, then worth
tuning (research Brief 03: measure first).

Ships as a new migration under `migrations/vector/` (`.notx.sql` if using
`CREATE INDEX CONCURRENTLY`) — never edit `vector_schema.sql`.

**Step-1 verification result (2026-06-10) — CONFIRMED, worst case:**

| DB | HNSW indexes on the 4 tables | pgvector |
|---|---|---|
| dev (`superhuman-remote-worker/srw-pgvector-0`) | **0 rows** | 0.8.2 |
| prod (`srw-prod-private/srw-prod-pgvector-0`) | **0 rows** | 0.8.2 |

Every dense retrieval — memories, trigger phrases, KB notes, sources — is a
sequential scan on both clusters. pgvector 0.8.2 ≥ 0.7, so **fix option 1
(halfvec expression index) is viable** and is the chosen direction: new
`migrations/vector/` migration adding
`USING hnsw ((embedding::halfvec(4096)) halfvec_cosine_ops)` per table +
the matching `::halfvec(4096)` cast in the SQL search functions' ORDER BY.

**Step-2 fix ✅ SHIPPED 2026-06-11 + verified on k3d** — migrations
`vector/0002_hybrid_search_halfvec_casts.sql` (the five hybrid-search
functions re-created with casts) + `0003/0004/0005_*.notx.sql` (one
`CREATE INDEX CONCURRENTLY` per file — asyncpg runs a multi-statement
string as one implicit transaction, which CONCURRENTLY rejects).

Two premise corrections discovered during implementation:

1. **`halfvec(4096)` HNSW is NOT viable** — halfvec HNSW caps at **4000**
   dims (`ERROR: column cannot have more than 4000 dimensions for hnsw
   index` on 0.8.2). The shipped fix is pgvector's documented >4000-dim
   pattern: expression index + ORDER BY over
   `subvector(embedding, 1, 4000)::halfvec(4000)` (both operands cast).
   qwen3-embedding is MRL-trained, so prefix cosine tracks full cosine —
   rank-identical to exact ordering on live rows (5/5 agreement).
2. **"Every dense retrieval is a sequential scan" overstated the cliff.**
   Every dense query is scope-filtered (`job_id`/`project_id`) and those
   btrees exist (`idx_memories_job_type`, `idx_memories_project`,
   `idx_knowledge_project`), so the actual plan was btree scope-scan +
   sort — exact and *optimal at small scope sizes*. The real cliff is
   per-scope growth (compounded by B6 grow-only). The new HNSW indexes are
   the planner-gated hedge: small scopes keep btree+sort (planner verified
   to prefer it), large scopes flip to HNSW.

Also shipped: `SET hnsw.iterative_scan = relaxed_order` on all five
functions — with the default `off`, a filtered HNSW scan stops after
`ef_search` candidates and a scope filter can silently shrink/empty the
dense channel; `relaxed_order` closes that trap before the planner ever
flips. Deliberately skipped: `source_embeddings` index (0 rows on every
cluster and **no dense read path exists anywhere** — the 0001 index attempt
was speculative; add one when a read path appears) and `find_similar()`
in `recall_store.py` (dedup threshold wants exact full-precision distance;
not a hot path).

k3d verification: all four migrations applied via the real orchestrator
boot path (`✓ 0002 … (5 ms)` + 3 notx builds), 3 indexes `indisvalid=t`,
`EXPLAIN` shows `Index Scan using idx_memories_embedding_halfvec` for the
cast ORDER BY shape, and `memory_hybrid_search` returns the B1 thread's
rows correctly ranked (RRF intact). Dev/prod pick the migrations up at
next orchestrator deploy (builds are sub-second at 942/3332/169 rows).
`ef_search` tuning remains Phase 3 of the overhaul — now actually possible.

---

## B3 — Assembler is enabled-but-never-called in persistent sessions

`config/persistent_defaults.yaml:175` promises:

```yaml
tasks:
  assemble_memories:
    enabled: true
```

and `assembler_interval: 7` is parsed — but **no code path in
`persistent_graph.py` / `persistent_app.py` ever calls `assemble_memories`**
(grep = zero hits). TTL curation (`memory_boost` / `memory_deprecate` /
`memory_add_triggers`) runs only in the worker graph (`src/graph.py:1587`).
Interactive sessions extract memories but never curate them: TTLs only ever
decrement mechanically, nothing intelligently pins or fades, and the config
actively lies about it.

**Fix options:**
- *Honesty fix (5 min):* set `enabled: false` with a comment "worker-only —
  see docs/issues/memory_bugs.md B3", so the config stops lying.
- *Wire-up (~1 d):* mirror the worker — load `memory_assembler` prompt, gate
  on `assembler_interval`, fire-and-forget at turn end.

**Recommendation:** honesty fix now; defer the wire-up decision to the
overhaul (Phase 3/5 may replace or delete the assembler entirely — the
research found consolidation efficacy unproven, so don't invest in wiring a
component we may ablate away).

**Status — honesty fix shipped 2026-06-10** (PR #112):
`persistent_defaults.yaml` now sets `assemble_memories.enabled: false` with a
comment pointing here. Wire-vs-retire stays an overhaul Phase-5 (ablation)
decision.

---

## B4 — No embedding-dimension guard → silent total memory outage

`src/services/embedding_service.py:83-96` returns the provider's vector
as-is; columns are `vector(4096)`. A misconfigured endpoint returning a
different dimensionality fails at INSERT with a raw DB error — swallowed by
the non-fatal `try/except` at every call site (`agent.py:1799`,
`graph.py:859`, …) → **memory silently stops working**, WARNING-only. Same
failure class as the 2026-06-03 aux outage
(docs/issues/surface_silent_aux_failures.md).

Aggravator: the `memory.embedding_model` YAML key is **dead** — the actual
model comes from the `EMBEDDING_MODEL` env var (`embedding_service.py:51`).
The config promises a knob that does nothing, which is precisely how a
dimension mismatch gets introduced.

**Fix sketch** (~2 h): at agent startup (or first RecallStore init), embed a
probe string, compare `len(vector)` to the schema dim; on mismatch log ERROR,
mark memory degraded in `AuxHealth`/status, and disable the subsystem loudly
rather than letting every write fail quietly. Wire the same probe into
`/status`.

**✅ FIXED 2026-06-11** — guard lives in `EmbeddingService` itself so every
caller is covered: each `embed`/`embed_batch` response is dimension-checked
against `EMBEDDING_DIMENSIONS` (default 4096 = the schema); a mismatch logs
one ERROR, latches `degraded_reason` for the process lifetime, raises typed
`EmbeddingDimensionError`, and all subsequent calls fail fast without I/O
(call sites keep swallowing — non-fatal as designed, but now loud + visible).
`verify_dimensions()` background-probes at both RecallStore init sites
(worker `agent.py`, persistent `persistent_session.py`); connectivity
failures are deliberately inconclusive (no latch — transient ≠ misconfig).
`health_snapshot()` surfaced as `"embedding"` on both status endpoints
(worker `get_status()`, persistent `/status`) via `peek_embedding_service()`
(never constructs on a status poll). Tests: `TestDimensionGuard` in
`tests/test_embedding_service.py` (8 cases). The dead `memory.embedding_model`
YAML-key aggravator was already deleted in the B9 sweep (#112).

---

## B5 — KB injection block has no token budget

The memory block is budgeted (`budget_tokens=10000`, enforced loop-by-loop in
`RecallStore.retrieve`). The KB block is **not**: both injection sites pull a
hardcoded `match_count=5` notes (`graph.py:971`, `persistent_graph.py:594`)
of **arbitrary length**, and `assemble_knowledge_block`
(`knowledge_store.py:523`) formats all of them — the token figure in its
footer is display-only. The only protection is a **fixed ~2500-token
estimate** in the worker's compaction overhead (`graph.py:794`); five large
notes can blow far past it, under-triggering compaction and inflating every
turn (relevant to the exit-137 context-explosion incidents).

**Fix sketch** (~0.5 d): mirror the memory pattern — pass a
`kb_budget_tokens` (config, default ~2500 to match the reserved estimate)
into `assemble_knowledge_block`, drop/truncate notes at the budget, and make
the compaction overhead use the real assembled size instead of the constant.

---

## B6 — Memory table is grow-only; nothing ever deletes rows

- Assembler "removal" is soft: `deprecate` floors `remaining_turns` at 0
  (stops injection) but keeps the row (`recall_store.py:749-757`).
- Dedup merges on write (updates the existing row) — never removes.
- **No GC, retention, or archive job exists anywhere.**

The `memories` table grows monotonically per project. Compounds B2: if dense
search is a seq-scan, retrieval latency grows with every memory ever written.
Low-importance rows (stored at ≥0.3 but below the 0.4 retrieval floor) are
*permanently unreachable yet permanently stored*.

**Fix sketch:** minimal retention job (orchestrator-side or on session/job
teardown): delete `source='observer'` rows with `importance <
retrieval_importance_floor AND access_count = 0 AND last_accessed < now() -
interval 'N days'`. Anything smarter (decay, archival tiers) belongs to the
overhaul — but "unreachable rows live forever" needs no research to fix.

---

## B7 — KB dual-write drift, fail-open

`kb_write` writes Neo4j first, then the pgvector mirror in a **separate**
try/except; a mirror failure is swallowed with a "pgvector can be rebuilt"
warning (`knowledge_tools.py:239`). But retrieval reads **only the mirror**
(see current-state doc §2) — so the note exists in Neo4j and is **invisible
to all retrieval** until someone manually rebuilds.
`KnowledgeStore.rebuild_from_notes` (`knowledge_store.py:433`) exists and has
**no caller**.

**Fix sketch** (~0.5 d): (1) surface the mirror failure in the tool result so
the agent knows its note won't be retrievable; (2) call
`rebuild_from_notes` on agent/session startup when drift is detected (count
mismatch between Neo4j notes and mirror rows for the project).

---

## B8 — Neo4j-down disables pgvector-only KB injection too

`ToolContext.has_knowledge()` requires **both** the Neo4j handle and the
pgvector store (`context.py:217-219`); the registry refuses all `kb_*` tools
without it, and both injection paths skip. But auto-injection reads only
pgvector — a Neo4j outage needlessly kills the KB context that Postgres alone
could serve. Stricter availability coupling than the architecture requires.

**Fix sketch:** split the gate — injection + `kb_search` need only the
mirror; graph tools (`kb_related`, `kb_contradictions`, `kb_provenance`,
write tools) need Neo4j. Aligns with the "graceful degradation" convention
(CLAUDE.md): Neo4j is supposed to be optional.

---

## B9 — Dead / misleading config keys

Parsed into `MemoryConfig` but consumed by nothing — tuning them is a no-op,
which actively misleads exactly the optimization effort we're about to start:

| Key | Why dead |
|---|---|
| `dense_results` / `sparse_results` / `recent_results` | only feed `search_dense`/`search_sparse`/`get_recent` (`recall_store.py:558/603/637`) which have **no callers**; the live SQL RRF derives channel limits from `match_count` internally |
| `observer_model` / `observer_base_url` | never read; the observer uses `auxiliary.model` |
| `embedding_model` | never read; `EMBEDDING_MODEL` env wins (`embedding_service.py:51`) |
| `storage` | never read; Postgres hardwired |

Also truth-in-schema nits: `valid_memory_source` enum values
`phase_archive` and `tool_error` are never written by any code path
(`todo` **is** written — `tools/core/todo.py:229`); the stats query counts
`phase_archive` forever-zero.

**Fix:** delete the dead keys (or wire them), one comment per removal; drop
or implement the dead enum values next time the constraint is touched.

**Status — keys deleted 2026-06-10** (PR #112): all seven removed from the
`MemoryConfig` dataclass, `_parse_memory_config`, and both defaults YAMLs
(NOTE comment in `defaults.yaml` points at the real knobs:
`auxiliary.model` and the `EMBEDDING_*` env vars). `_parse_memory_config`
reads keys explicitly, so stray copies in stored `resolved_config` JSONB are
ignored harmlessly. Still open: the `phase_archive`/`tool_error` enum nits
(need a vector migration — fold into the next migration that touches the
constraint).

---

## B10 — Injection-strip prefix registry is silently fragile

Memory/KB/todo blocks are kept out of summaries solely by
`is_workspace_injection_message` matching tool-call-ID prefixes
(`workspace_injection.py:104-115`). The persistent path injects **before**
`ensure_within_limits`, so it depends entirely on this strip. A renamed
prefix or a new injection type that forgets to register would **silently
summarize injected memory blocks into durable history** — invisible context
poisoning.

**Fix:** a unit test that constructs every injection type (from their real
constructors) and asserts `is_workspace_injection_message` recognizes each;
fails loudly when someone adds a fourth injection type.

---

## Cleanup (not bugs) — tracked elsewhere

**✅ Swept 2026-06-10** (PR #112, ~2,300 lines): `MemoryObserver`,
`memory_migrator.py`, `Neo4jDB._load_query` + `QUERIES_DIR`, the dead
`search_*`/`get_recent` helpers, `tests/test_memory_observer.py`,
`tests/test_memory_migrator.py`, and the migrator/dead-helper test sections
in `test_knowledge_phase3.py`/`test_recall_store.py` — each re-verified
zero-callers by grep before deletion. Deliberately left: the
`MemoryManager`/workspace-template family (tracked in
`docs/issues/remove_workspace_md_vestiges.md` — NB its `MemoryManager` name
collides with the overhaul's new abstraction, so that removal should land
before Phase 1) and the equally-dead `_load_query` twins in
`src/database/postgres_db.py:250` / `orchestrator/database/postgres.py:443`
(outside the audited catalog; remove opportunistically).

## Suggested order

1. ~~**B1**~~ ✅ fixed 2026-06-10, live-verified on k3d 2026-06-11 (all
   signals green; see B1 status above).
2. ~~**B2**~~ ✅ complete — step 1 verified 2026-06-10; step 2 (subvector-4000
   halfvec migrations vector/0002–0005) shipped + k3d-verified 2026-06-11.
3. ~~**B3 honesty fix + B9**~~ ✅ shipped 2026-06-10.
4. ~~**B4**~~ ✅ fixed 2026-06-11 (dimension guard + startup probe + /status).
5. B5/B6/B7/B8/B10/B11 — absorbed by overhaul phases by design: B5 → Phase 1
   `token_budget` policy; B8 → Phase 1 plugin decoupling; B10 → Phase 1
   equivalence fixtures; B11 → Phase 1 `capture(kind="session_end")` hooking
   `_terminate_session`; B3 wire-vs-retire → Phase 5 ablation; B6 → Phase 4
   `gc` writer; B7 → graph-as-plugin restructure (Phase 1/7). Don't fix these
   standalone first — Phase 1 pins current behaviour with fixtures, so the
   old code should stay frozen until the seam lands.
