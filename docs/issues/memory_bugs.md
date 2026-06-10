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
| B1 | Persistent memory extraction broken 3 ways (phantom config attrs) | **HIGH** | ~1 h | open |
| B2 | 4096-dim HNSW indexes silently skipped → seq-scan retrieval | **HIGH** (verify first) | 0.5–1 d | open |
| B3 | Assembler enabled-but-never-called in persistent sessions | MED-HIGH | 5 min (honesty) / ~1 d (wire) | open |
| B4 | No embedding-dimension guard → silent total memory outage | MED | ~2 h | open |
| B5 | KB injection block has no token budget | MED | ~0.5 d | open |
| B6 | Memory table is grow-only — nothing ever deletes rows | MED (slow burn) | policy + ~0.5 d | open |
| B7 | KB dual-write drift, fail-open (note in Neo4j, invisible to retrieval) | MED-LOW | ~0.5 d | open |
| B8 | Neo4j-down kills pgvector-only KB injection too | LOW-MED | small | open |
| B9 | Dead/misleading config keys (tuning no-ops) | LOW (hygiene) | ~1 h | open |
| B10 | Injection-strip prefix registry is silently fragile | LOW (latent) | test guard | open |

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

~1,600 lines of confirmed-dead code (`MemoryObserver`, `MemoryManager`,
`memory_migrator.py`, `Neo4jDB._load_query` + phantom `queries/neo4j/` dir,
dead `search_*` helpers, orphaned `tests/test_memory_observer.py`) are
cataloged in `docs/features/agent_memory_current_state.md` §6. The
`MemoryManager`/workspace-template family is already tracked in
`docs/issues/remove_workspace_md_vestiges.md`; fold the rest into that sweep
rather than duplicating here.

## Suggested order

1. **B1** — highest value-per-hour in the repo right now; pure bug, no design
   dependency. Unblocks trustworthy extraction on the primary path.
2. **B2 step 1** (the two SQL queries) — 5 minutes against dev DB; determines
   whether B2 is a real perf cliff or already fine.
3. **B3 honesty fix + B9** — stop the config lying before any tuning work
   begins (otherwise the overhaul's A/Bs will "tune" dead knobs).
4. **B4** — cheap prevention for a failure class that has already bitten once.
5. B5/B6/B7/B8/B10 — batch opportunistically or alongside overhaul phases
   that touch the same files.
