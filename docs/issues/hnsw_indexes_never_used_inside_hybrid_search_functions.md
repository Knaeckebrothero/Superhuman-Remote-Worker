# HNSW indexes exist but are never used inside the hybrid-search SQL functions — every dense retrieval is still a scan

**Status:** **FIX BUILT + TESTED on k3d, NOT deployed, UNCOMMITTED** — migration `0017_hybrid_search_plpgsql_dynamic_execute.sql` (+ regenerated `vector_schema_current.sql`). Root cause bisected on the live main dev cluster; candidates A/B benchmarked on a production-scale k3d harness; fix applied through the real orchestrator migration runner and verified semantically identical on real data. KB path **fixed** (~10×). Memory path **partially fixed** (~6×) — the `memories` content arm has a *second, independent* defect (TOAST-blind costing) that 0017 cannot reach; see below, still open. Also open before prod: ANN-vs-exact recall at scale. Nothing deployed to the dev or prod clusters.
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

**Mechanism (best-supported, after the k3d experiment below).** The `SET`-clause /
inlining hypothesis is **disproven** — removing it changes nothing (Candidate B).
What fits all the evidence: a `LANGUAGE sql` function body is planned once against
parameter *placeholders*, and with a placeholder on the probe side the HNSW ordering
operator cannot be matched, so the index path is structurally absent. The top-level
`PREPARE` tests escape this because `EXECUTE` uses a **custom** plan for its first
executions, folding the parameters to constants. `plan_cache_mode = force_custom_plan`
does not rescue the function because it governs prepared statements and plpgsql
cached plans — a plain SQL function's body plan goes through a different path and is
not covered. A dynamic `EXECUTE` sidesteps the whole thing with a one-shot plan.
The last step is still inference; the *behaviour* is measured.

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

## Fix — BUILT: migration `0017_hybrid_search_plpgsql_dynamic_execute.sql`

**Converts all six functions to `plpgsql` with a dynamic `EXECUTE`.** Signatures,
argument names, defaults, return types and `SET` clauses unchanged, so no caller
moves. Bodies are the deployed text verbatim with named parameters mechanically
rewritten to positional `$n` (generated, not hand-transcribed).

**It fully fixes the knowledge/KB path and only partially fixes the memory path** —
see "Second defect" below, which the build surfaced and which 0017 cannot reach.

| function | before | after | dense-arm plan after |
|---|---|---|---|
| `knowledge_chunk_hybrid_search` | 5.0 / 5.5 / 5.9 s | **0.52 / 0.59 / 0.61 s** | `Index Scan` ✅ |
| `memory_project_hybrid_search` | 8.8 / 9.1 / 9.7 s | **1.3 / 1.6 / 1.9 s** | trigger arm `Index Scan` ✅ · content arm still `Seq Scan` ❌ |

### Verification performed (k3d, 2026-08-07)

- **Applies through the real runner.** Orchestrator boot path:
  `✓ 0017_hybrid_search_plpgsql_dynamic_execute.sql (3 ms)`, row in
  `schema_migrations` with checksum; all six functions report `plpgsql` in the live
  k3d `srw_vector`.
- **Semantically identical on real data.** Old shape recreated under `_old` names
  alongside the migrated ones on k3d's real `srw_vector` (142 chunks / 1,588
  memories — small enough that *both* shapes run exact scans, so any diff would be a
  rewrite bug): **0 ordered mismatches**, identical row counts and identical
  ordering, for both `knowledge_chunk_hybrid_search` and
  `memory_project_hybrid_search`. Temp functions dropped after.
- **Edge paths.** Omitted optional args (defaults) → 15 rows; `version_param = NULL`
  (the `IS NULL` branch) → 50 rows; non-matching kb + tsquery → 0 rows, no error.
- `ruff check` + `ruff format --check` clean; `pytest -k "knowledge or memory or
  recall or vector or migration"` → **1192 passed, 3 skipped**.
- `scripts/schema-snapshot.sh` re-run; `vector_schema_current.sql` regenerated.

### The measurement that picked this shape

Run on a purpose-seeded k3d scratch DB (see the harness below) at production
parity — 32,900 chunks in the primary kb + 980 in a second, same halfvec HNSW index
(`m=16, ef_construction=64`), pgvector 0.8.2. Three runs each:

| shape | latency | dense-arm plan |
|---|---|---|
| Control — dense arm as a top-level `PREPARE` | 21 ms | `Index Scan using idx_knowledge_chunks_embedding` |
| **Baseline — the deployed function, verbatim** | **5.0 / 5.5 / 5.9 s** | **`Parallel Seq Scan on knowledge_chunks`** |
| **Candidate A — `plpgsql` + dynamic `EXECUTE`** | **0.48 / 0.52 / 0.88 s** ✅ | **`Index Scan using idx_knowledge_chunks_embedding`** |
| Candidate B — same SQL body, `SET` clause removed | 4.8 / 4.9 / 5.9 s ❌ | `Parallel Seq Scan on knowledge_chunks` |

Two results worth carrying forward. **Candidate A works** — that is the fix, and it
is now measured rather than inferred. **Candidate B does not** — dropping the `SET`
clause so the function can inline changes nothing, which kills the leading hypothesis
and removes the "cheapest thing to try" from the list entirely. Don't spend time on
it.

Remaining option if A somehow disappoints on real data: **hoist the arms into the
application layer** (`search_chunks` + RecallStore issue them as ordinary
parameterized asyncpg statements and fuse RRF in Python). The control row above is
effectively that shape, so it is known-good — but it is 1–2 days and moves logic out
of SQL. Hold it as the fallback.

## Second defect — `memories` dense content arm, TOAST-blind costing (OPEN)

Surfaced while verifying 0017. It is **independent of the function wrapper** and
0017 cannot fix it. Filed here because it is the same investigation and the same
symptom, but it needs its own work.

After 0017 the memory functions are only ~6× better, and all of that comes from the
trigger-phrase arm. The content arm still scans:

```
->  Index Scan using idx_memory_retrieval_messages_embedding_halfvec on ... rm     ✅
->  Seq Scan on memories memories_1  (rows=29000, actual time=0.058..1085.245)     ❌
```

1,085 ms of a 1,108 ms call. It is not the wrapper: the **same arm, isolated at top
level, with a literal probe, no parameters and no filters** still seq-scans. Forced
with `enable_seqscan=off` the index works and takes **15.5 ms vs 1,143 ms — 74×**.

The cause is cost estimation, not availability. The planner prices the HNSW path at
`cost=3985.61` and the seq scan at `cost=2106`, so it picks the scan — but
`memories.embedding` is a 4096-dim vector stored out-of-line in TOAST, and the
seq-scan cost model counts only main-heap pages. It never sees the ~1.1 s of
detoasting 29,000 vectors. `knowledge_chunks` escapes this because its inline
`content` text makes the heap bigger, so scanning is priced high enough for the index
to win.

Levers tried and rejected (do not re-test):

- **`ALTER FUNCTION … SET enable_seqscan = off`** — measured, does **not** work. It
  does not force the *ordering* path; the planner just switches to the btree
  `idx_memories_project_valid` and still reads all 29,000 rows. 1386 → 1169 ms.
- **`hnsw.iterative_scan = off`** (to lower the estimated index cost) — still a seq
  scan. Also unsafe: B2 added `relaxed_order` deliberately so a filtered scan can't
  return short.
- No GUC combination selects HNSW here; `enable_indexscan=off` would kill the HNSW
  path too, since that is also an index scan.

Likely direction: restructure the content arm so the ANN lookup is its own statement
against real bounds, or hoist that one arm into the application layer (the
already-proven shape). Not attempted.

### Open: ANN recall at scale is NOT yet verified

Two different questions, only one of them closed.

**Closed — is the rewrite semantically faithful?** Yes. Proven on real k3d data where
both shapes run exact scans: 0 ordered mismatches (see Verification above).

**Still open — does approximate ANN return the same rows as the exact scan it
replaces?** The real-data test above cannot answer this, precisely *because* both
shapes ran exact there. At production scale the migrated function takes the HNSW
index and the old one did not, so on the main cluster this is genuinely
exact → approximate. That is what B2 intended when it built the indexes, but
intended ≠ measured.

The k3d harness cannot answer it either: its vectors are `random()` in `[0,1)`, so
every vector sits in the positive orthant and distances concentrate (observed spread
0.000–0.247, σ=0.035). Top-K ordering there is noise — a baseline-vs-migrated diff on
that data showed 24/50, which is meaningless, not alarming.

**Close it before this reaches prod**: on the main dev cluster, create the migrated
shape under a renamed copy alongside the deployed function (additive DDL, nothing
deployed changes, droppable in one statement) and diff top-50 `note_id` sets over a
sample of real queries at 30.7k chunks. Tune `hnsw.ef_search` if recall is short —
B2 noted it is unset everywhere and that is still true.

Whatever lands, gate it on the A/B in the measurement table — same call, before and
after, with `auto_explain` confirming an `Index Scan using idx_*_embedding`.
**Verify at production scale**: k3d *as it stands* (142 chunks) shows nothing, which
is how this got past B2 in June — but a seeded scratch DB reproduces it faithfully,
so the check does not require the live cluster. Migration hygiene: new numbered file
under `migrations/vector/`, never edit `vector_schema.sql`, regen
`schema_current.sql` after. Also worth setting `hnsw.ef_search` once an index path is
actually live; B2 noted it is absent everywhere and that stays true.

**Stopgap** while the real fix is pending — config, not code, and it treats the
symptom only: raise `delegation.light.timeout_seconds` above 240 and cap per-turn
tool-call concurrency, so a fan-out stops dying inside its first `kb_search` batch.

### Harness: reproduce the bug on k3d (no live-cluster access needed)

Left standing on k3d as database **`hnsw_exp`** (846 MB; `srw_vector` untouched at
142 chunks). Drop with
`kubectl --context=k3d-srw -n srw exec srw-pgvector-0 -- psql -U srw -d postgres -c 'DROP DATABASE hnsw_exp;'`.

To rebuild from scratch: create the DB, `CREATE EXTENSION vector` + `uuid-ossp`,
`pg_dump -s -t knowledge_chunks -t knowledge_index` from `srw_vector` and load it,
then insert ~4,700 notes and 7 chunks each with
`(SELECT array_agg(random()::real)::vector(4096) FROM generate_series(1,4096))`
(~5.6 s for 33 k rows). Two gotchas: drop the HNSW index before bulk insert and
recreate after, and set `max_parallel_maintenance_workers = 0` for the build — the
pod's small `/dev/shm` makes a parallel build die with *"could not resize shared
memory segment … No space left on device"*. Serial build takes ~43 s.
Then `ANALYZE` both tables and load the deployed function via
`pg_get_functiondef`.

**Always run the control first** — the dense arm as a top-level `PREPARE`. If the
control does not pick the index, the dataset is too small and any "fast" result below
is meaningless. That control is the guard B2's k3d verification lacked.

### Reproduce (live main cluster)

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
