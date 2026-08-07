# HNSW indexes exist but are never used inside the hybrid-search SQL functions — every dense retrieval is still a scan

**Status:** Filed — root cause bisected and measured on the live main dev cluster (`superhuman-remote-worker/srw-pgvector-0`, `srw_vector`). Unfixed. No code changed. **No configuration fix exists** — every GUC lever has been tried and failed, and the indexes are present and valid; the fix must change the SQL or the call path (~half a day, DB-only, after 1–2 h of experiment).
**Found:** 2026-08-07, investigating why the 3-way `spawn_subagent` fan-out in job `204d0ed1-e997-4fa3-9de2-2526c2b26eea` (Loop iter 4 · SCHOLAR) timed out with zero deliverables.
**Severity:** **HIGH.** `kb_search` costs ~12–14 s and a memory retrieval ~22–46 s on this cluster, against ~1–3.5 s for the identical query when it reaches the index. This is a per-turn tax on *every* job and session, and it is what actually killed the fan-out. It grows with store size.
**Component:** `orchestrator/database/migrations/vector/0002_hybrid_search_halfvec_casts.sql` (the six re-created hybrid-search functions) · `src/services/knowledge_store.py:1666` `search_chunks` · the RecallStore retrieval path.
**Related:** [`memory_bugs.md`](memory_bugs.md) **B2** — this is B2's *unfinished half*: B2 built the indexes and verified they exist, but the deployed functions never choose them, so B2's "planner-gated hedge" has never fired. · [`docs/features/agent_memory_current_state.md`](../features/agent_memory_current_state.md) (§ B2 verification) · [`delegation_light_mode_missing.md`](delegation_light_mode_missing.md) (the light-reader harness whose deadline this blew).

---

## Symptom

Three light subagents spawned in one turn all hit their 240 s wall clock
(`delegation.light.timeout_seconds`) and returned `BLOCKED`, having registered zero
citations. The parent read the bodies, invoked its two-strike rule, and redid the
research itself serially over the following 20 minutes.

The readers were not stuck on the network, the workspace, or the LLM. They spent
80% of their life inside their **first** tool batch — 14 concurrent `kb_search`
calls — and were killed before the `web_search` work the task was actually about
could return.

## Timeline (2026-08-07 UTC, agent `srw-agent-j-c40b6c3a`)

| time | event |
|---|---|
| 10:57:55.978 | parent (MiniMax-M3, iter 23, `doc_id=103478`) emits 3 × `spawn_subagent` |
| 10:57:56.6 / 10:57:58.2 / 10:58:00.7 | reader worktrees `.worktrees/sub_0|1|2` created |
| 10:58:03.9 / 10:58:08.9 / 10:58:10.9 | readers' turn 1 returns — **4 + 4 + 6 = 14 concurrent `kb_search`**; deadlines land at 11:01:59 / 11:02:02 / 11:02:04 |
| 10:58:03.9 – 10:58:11.1 | all 14 `POST /v1/embeddings` return 200 — the embedding step is **not** the cost |
| 10:58:11 → 11:01:15 | **~190 s of dead air.** No HTTP in the pod log at all, only heartbeats and `/ready`. This is the `kb_search` batch. |
| 11:01:19.2 / 11:01:19.4 / 11:01:22.5 | turn 2 — 5 + 4 + 4 `kb_read`, effectively free (~0.2 s) |
| 11:01:22.7 / 11:01:23.9 / 11:01:48.2 | turn 3 — **6 + 8 + 8 = 22 concurrent `web_search`**, with only 12–38 s of budget left |
| 11:01:59.4 / 11:02:01.9 / 11:02:04.3 | ×3 `light subagent tool calls exceeded wall-clock limit (240.0s); forcing synthesis` (`light_runner.py:301`) — all 22 `web_search` calls cancelled |
| 11:02:07.9 / 11:02:12.1 / 11:02:17.6 | forced syntheses return `BLOCKED: … 0 verbatim cite_web anchors registered` |
| 11:03:58 | parent resumes (`doc_id=103499`): *"3 subagents hit time limits."* |
| 11:23:59 | `todo_1` finally closed after serial parent rework |

Cost: ~4.5 min wall clock and **~324 k tokens** (318,783 prompt + 5,333 completion
across 12 reader LLM calls) for zero delivered work, plus ~20 min of parent rework.

## Root cause

`kb_search` → `KnowledgeStore.search_chunks` → `knowledge_chunk_hybrid_search(...)`.
The dense arm orders by the exact expression the HNSW index is built on, and the
function even pins `SET hnsw.iterative_scan = 'relaxed_order'`. **The index is
still never chosen.** `auto_explain` on a live call:

```
duration: 10098.964 ms
  ...
  ->  Sort  Sort Key: (((subvector(c.embedding, 1, 4000))::halfvec(4000)
                        <=> (subvector($2, 1, 4000))::halfvec(4000)))
        ->  Parallel Seq Scan on knowledge_chunks c  (rows=10233 loops=3)
```

30,696 chunks scanned and sorted per call, three workers, ~9.2 s of the ~10 s.
`memory_project_hybrid_search` is the same story on `memories` — its dense arm runs
on the btree `idx_memories_project_importance`, never on
`idx_memories_embedding_halfvec`.

### Measurements (live, warm, `srw_vector`)

| query | time | plan |
|---|---|---|
| deployed `knowledge_chunk_hybrid_search` | **12.3–13.9 s** (28.0 s cold) | Parallel Seq Scan + Sort |
| deployed `memory_project_hybrid_search` | **21.6–46.5 s** (auto_explain: 29.8 s) | btree scan, distance per row |
| dense arm hoisted to a top-level `PREPARE` — same param, `subvector()` probe, `ROW_NUMBER()`, `LIMIT $n*4`, same filters | **1.9–2.4 s** | `Index Scan using idx_knowledge_chunks_embedding` |
| **the exact function body** (CTE + `GROUP BY`) as a top-level `PREPARE` | **3.5 s** | `Index Scan using idx_knowledge_chunks_embedding` |
| dense arm with a constant-folded literal probe | **0.9 s** | index scan |

The last two rows are the finding: **the query shape is fine — it is the function
wrapper that loses the index.** Lift the identical SQL out of
`knowledge_chunk_hybrid_search` and run it as a prepared statement with the same
parameters and it uses the index at ~4× to ~10× the speed.

### Ruled out (don't re-test these)

- **`subvector()` applied to a parameter.** A top-level `PREPARE` whose probe is
  `subvector($1,1,4000)::halfvec(4000)` uses the index (1.9 s). Not the cause.
- **The parameterized `LIMIT match_count * 4`.** `PREPARE` with `LIMIT $2*4` uses
  the index (2.4 s).
- **The `ROW_NUMBER() OVER (ORDER BY …)` window.** `PREPARE` with the window uses
  the index (2.3 s).
- **Generic vs custom plan.** `SET plan_cache_mode = force_custom_plan` → 12.9 s,
  unchanged.
- **Parallelism.** `SET max_parallel_workers_per_gather = 0` → 24.1 s, i.e. *worse*
  (serial seq scan). The parallel plan is a consequence, not the cause.
- **Planner cost preference.** `SET enable_seqscan = off` → 23.4 s against a 21.3 s
  baseline in the same session, and `memory_project_hybrid_search` went to 66.8 s.
  See below — this one is not just a negative result.
- **Filter selectivity.** `kb_id` matches 30,707 of 31,622 chunks and
  `embedding_version` matches all of them — the scope filter is not pruning anything.

### The index path is not considered, not merely out-costed

`enable_seqscan = off` does not forbid a sequential scan, it attaches a large cost
penalty to one. If the HNSW path were being *evaluated and rejected on cost*, that
penalty would flip the choice. It doesn't — the function keeps the scan even when the
scan is priced punitively. So inside the function the index path is not losing a
cost comparison; it is **never entering the plan search at all**.

That is the load-bearing consequence: **no configuration fix exists.** Every GUC
lever has now been tried and failed (`plan_cache_mode`,
`max_parallel_workers_per_gather`, `enable_seqscan`), and the indexes themselves are
present and valid, so there is nothing to rebuild or `ANALYZE`. The fix has to change
the SQL or the call path.

**Still not pinned:** *why* the path is unavailable in this context. Leading
candidate is that the `SET` clause makes the `LANGUAGE sql` function non-inlinable,
so the body is planned through the plan cache against parameter placeholders and the
index's ordering operator can never be matched. That is a hypothesis, not a
measurement — but note it now has to explain *unavailability*, not mis-costing, which
rules out any explanation that reduces to "the planner thought the scan was cheaper."

### Why B2 recorded this as fixed

`memory_bugs.md` B2 shipped the halfvec expression indexes (`vector/0003–0005`) plus
the matching casts in the functions (`vector/0002`), verified the indexes exist, and
recorded premise correction 2:

> The new HNSW indexes are the planner-gated hedge: small scopes keep btree+sort
> (planner verified to prefer it), large scopes flip to HNSW.

**The flip never happens** — not at 30 k chunks, not at any size, because the
function context never considers the index at all. The k3d verification passed
because at small scope btree+sort genuinely *is* fast, so the defect is invisible
until the store grows. It has now grown.

## Blast radius

All six vector-search functions carry the same shape:

`knowledge_chunk_hybrid_search`, `knowledge_hybrid_search`,
`knowledge_multi_project_hybrid_search`, `memory_hybrid_search`,
`memory_project_hybrid_search`, `memory_multi_project_hybrid_search`.

Live row counts: `source_embeddings` 352,488 · `memory_retrieval_messages` 116,652 ·
`knowledge_chunks` 31,622 · `memories` 29,147 · `knowledge_index` 4,699.
(`source_embeddings` has **no** vector index at all — separate, lower-priority item;
see `database_optimization_plan.md` on the CitationEngine dimension conflict.)

The memory path is the expensive one because it runs on *every* turn. In job
`204d0ed1` the gap between two consecutive parent LLM calls is ~60 s of
`memory_retrieve` → `memory_inject` → `knowledge_inject` (audit entries 566–571,
12:44:43 → 12:45:43), which matches the measured 22–46 s and 12–14 s directly.
Across ~45 iterations that is a large fraction of the job's 165-minute runtime spent
scanning pgvector.

## Fix

Rough cost: **~half a day, DB-only**, after **1–2 h** of experiment. There is no
one-line fix — see the `enable_seqscan` result above — but there is very likely no
application change either.

Spend the first 1–2 h pinning the mechanism rather than guessing: create renamed
copies of *one* function on the dev DB in each shape below and time them. That is
decisive and cheap, and it picks the option for you.

1. **Convert to `plpgsql` with a dynamic `EXECUTE`** — *recommended first try.* A
   dynamic `EXECUTE` is planned as its own top-level statement, and a top-level
   statement with these exact parameters is the configuration measured at 1.9–3.5 s.
   Signatures stay identical, so `search_chunks` and the RecallStore call sites do
   not change: one new migration, no application code, no image rebuild, revertible
   by a follow-up migration. Small blast radius for a ~10× win.
2. **Hoist the arms into the application layer** — *fallback.* The only option
   actually **proven** to work, since the top-level `PREPARE` measured above is
   literally what the app would issue. But 1–2 days: touches `search_chunks` and the
   RecallStore, and the RRF fusion has to be reimplemented and tested in Python.
3. **Drop the `SET hnsw.iterative_scan` clause so the function can inline** —
   cheapest to try, most speculative. Carries a trap: B2 added that clause
   deliberately, because with the default `off` a filtered HNSW scan can silently
   return short. It must be **moved** onto the connection (an asyncpg pool `init`
   callback), not deleted, or you trade a latency bug for a correctness bug.

Whatever lands, gate it on the A/B in the measurement table — same call, before and
after, with `auto_explain` confirming an `Index Scan using idx_*_embedding`.
**Verify at production scale on the dev cluster**: k3d at ~1–3 k rows will show
nothing, which is exactly how this got past B2 in June. Migration hygiene: new
numbered file under `migrations/vector/`, never edit `vector_schema.sql`, regen
`schema_current.sql` after. Also worth setting `hnsw.ef_search` once an index path is
actually live; B2 noted it is absent everywhere and that stays true.

**Stopgap** while the real fix is pending — config, not code, and it treats the
symptom only: raise `delegation.light.timeout_seconds` above 240 and cap per-turn
tool-call concurrency, so a fan-out stops dying inside its first `kb_search` batch.

### Reproduce

```bash
kubectl --context=main -n superhuman-remote-worker exec -i srw-pgvector-0 -- \
  psql -U srw -d srw_vector -q <<'SQL'
LOAD 'auto_explain';
SET auto_explain.log_min_duration = 1000;
SET auto_explain.log_nested_statements = on;
SET auto_explain.log_analyze = on;
SELECT embedding::text AS emb FROM knowledge_chunks WHERE embedding IS NOT NULL LIMIT 1
\gset
SELECT count(*) FROM knowledge_chunk_hybrid_search(
  'any query', :'emb'::vector(4096),
  ARRAY['68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a']::uuid[],
  'qwen3-embedding-8b:4096:c1:pf-456c46c9ad8d639a', 50, 0.6, 0.3, 0.1, 60);
SQL
kubectl --context=main -n superhuman-remote-worker logs srw-pgvector-0 --tail=200 \
  | grep -E "Seq Scan|Index Scan|duration:"
```

## Secondary findings from the same incident

- **A timed-out reader is announced as `[subagent done]`.** The deadline path in
  `light_runner.run_light_subagent` returns *normally* with a partial synthesis, so
  no exception reaches `spawn_subagent._format_result` and `failed` stays `False`
  (`src/tools/delegation/spawn_subagent.py:128-140`). This is deliberate — a timeout
  is a partial result, not a failure — and the parent did correctly read the
  `BLOCKED` body. Worth revisiting only because a reader that produces *nothing*
  usable is indistinguishable from one that succeeded, at exactly the moment the
  parent decides whether to re-delegate. Low priority.
- **`delegation.light.max_parallel` bounds readers, not tool calls.** Three readers
  issued 14 concurrent `kb_search` and then 22 concurrent `web_search`;
  `_execute_tool_calls` gathers a turn's calls with an unbounded `asyncio.gather`.
  With a healthy `kb_search` this is fine, but it means fan-out amplifies
  N-readers × M-calls onto shared backends with no ceiling.
- **Memory curation toolchain broken in-session** (11:15:51, `auxiliary.py:2088`):
  `memory_search` errored on every invocation and `memory_boost`/`memory_deprecate`
  rejected the injected display indices (`'4'`, `'7'`, `'10'`) as invalid UUIDs, so
  zero TTL adjustments were applied. Pre-existing and independent of the above;
  belongs with [`memory_bugs.md`](memory_bugs.md) if not already tracked there.
