# Memory eval harness

Offline evaluation of the agent memory subsystem against
**LongMemEval** (arXiv:2410.10813, ICLR 2025) — Phase 2 of
[`docs/features/agent_memory_overhaul.md`](../../docs/features/agent_memory_overhaul.md).
Research grounding: `ai-memory-research/results/05_lifecycle_eval_report.md`.

The harness drives the production seam directly — `MemoryManager.assemble()`
and `capture()` from `src/services/memory/` — with the production loader,
stores, embedding service, and prompts. No graph spin-up, no orchestrator.
What an arm measures is what a real session with that YAML would do.

## How LongMemEval maps onto the seam

| LongMemEval | Harness | Production analogue |
|---|---|---|
| one question instance | one `project_id` | a project |
| one haystack session | one `job_id` within the project | a thread/job of that project |
| evidence labels (`answer_session_ids`) | each memory row's `job_id` **is** its source session | — |
| asking the question | fresh reader scope + one `assemble(query=question)` | a new session in the project |

Ingestion is **incremental** (sessions replayed in haystack order, memories
accrue through `capture()`), not batch-loaded. Two ingestion modes:

- **seam** (`arms/persistent_current.yaml`) — production-faithful replay:
  per round `HumanMessage` → `assemble()` (the live per-turn read path, so
  TTL decrement/access-resets accrue) → `AIMessage` → `capture(turn_end)`;
  then `capture(session_end)` → teardown extraction. Costs real
  auxiliary-LLM calls.
- **verbatim** (`arms/flat_verbatim.yaml`) — no extraction LLM: every
  user-assistant round stored directly with `remaining_turns=0` (never
  TTL-pinned) and dedup disabled (`dedup_threshold: 1.01`), so question-time
  retrieval is pure hybrid RRF over a flat round-granularity index. This is
  the cheap smoke arm and the published-baseline reproduction arm.

Scoring: injected memories → source sessions (first-occurrence collapse) →
**Recall@k / NDCG@k / coverage@k / first-hit rank** vs the evidence labels,
overall and per question type, plus cost (tokens injected, assemble
latency). Abstention questions (`*_abs`) are excluded from retrieval
aggregates (they return with the end-task slice).

## Quick start

```bash
# 1. Dataset (not committed — ~250 MB). From the LongMemEval repo:
#    https://github.com/xiaowu0162/LongMemEval (HF: xiaowu0162/longmemeval)
#    Put longmemeval_s.json under eval/memory/data/.

# 2. A pgvector server. Dev-compose:
podman-compose -f docker-compose.dev.yaml up -d postgres-vector
export EVAL_VECTOR_DSN='postgresql://srw:srw_password@localhost:5433/srw_eval'
# (defaults to exactly that DSN; the runner creates the srw_eval database
#  and applies migrations/vector/ with --init-db)

# 3. Real embeddings (retrieval quality IS the measurement):
export EMBEDDING_BASE_URL=...   # e.g. the ai.h4ll.app router
export EMBEDDING_API_KEY=...
export EMBEDDING_MODEL=qwen3-embedding-8b   # 4096-d (schema dimension)

# 4. Smoke on the committed fixture (no LLM, ~20 embedding calls):
python -m eval.memory.run \
  --dataset eval/memory/fixtures/tiny_longmemeval.json \
  --arm eval/memory/arms/flat_verbatim.yaml --init-db \
  --out eval/memory/runs/smoke_flat

# 5. Real runs:
python -m eval.memory.run --dataset eval/memory/data/longmemeval_s.json \
  --arm eval/memory/arms/flat_verbatim.yaml --init-db          # full set OK
python -m eval.memory.run --dataset eval/memory/data/longmemeval_s.json \
  --arm eval/memory/arms/persistent_current.yaml --limit 20    # LLM cost!

# 6. Reports & A/B:
python -m eval.memory.report eval/memory/runs/<run>
python -m eval.memory.report eval/memory/runs/<runA> eval/memory/runs/<runB>
```

Seam-mode arms additionally need the extraction model: fill the
`auxiliary:` block in the arm (model + base_url + `api_key_env` naming an
env var). Results stream to `<out>/results.jsonl` per question — rerunning
with the same `--out`/`--run-id` resumes, so a flaky aux endpoint only
costs the in-flight questions. `run.log` in the run dir captures all
contained seam warnings; a seam arm with `stored=0` across many questions
means extraction is failing, not that the system has no memory.

### Borrowing the k3d cluster's routing

If the local k3d stack is up, the embedding/auxiliary endpoints + keys the
cluster dispatches with can be resolved from the orchestrator (they live
encrypted in the model catalog, not in env):

```bash
kubectl --context=k3d-srw -n srw port-forward svc/srw-pgvector 15433:5432 &
export EVAL_VECTOR_DSN="postgresql://srw:<VECTOR_POSTGRES_PASSWORD>@localhost:15433/srw_eval"
# In-pod, resolve_default_for_capability("embedding"/"auxiliary") +
# get_user_llm_endpoint() print the decrypted base_url/api_key — see the
# Phase-2 setup notes in the overhaul doc.
```

## Cost & scale guidance

- **verbatim**: one embedding call per round (+1 per assemble). Full
  LongMemEval_S (500 questions × ~50 sessions) is embedding-only — fine.
- **seam**: extraction fires per `observer_interval` turns **and** per
  session end → roughly `sessions × (1 + turns/interval)` aux calls per
  question (~50–80 for _S questions). Start with `--limit 10..20`
  (type-stratified, seeded) and scale once numbers stabilize.
- `--cleanup` deletes each question's memory rows after scoring (the
  result row keeps the ranking); without it, rows stay queryable per
  `run_id`-derived scopes and reruns never collide (fresh uuid5 scopes).

## Reproducing a published baseline (the Phase-2 acceptance anchor)

The paper's retrieval table reports session/round-granularity Recall@k /
NDCG@k for BM25 and dense retrievers (Stella, Contriever, GTE). Our
`flat_verbatim` arm is the closest stack-equivalent: round granularity,
our embedder, RRF-fused dense+sparse+recency. Run it on the full _S set
and compare `summary.json` to the paper's dense-retriever rows — the
check is *ballpark agreement* (our embedder is stronger than the paper's;
wild divergence means a harness bug, not a system difference). That
anchors the metric implementation; `persistent_current` on the same
questions is then the recorded **current-system baseline**.

## Deliberate deviations from production (all metric-motivated)

- `capture()` is awaited (production fire-and-forgets `turn_end`) — same
  writer effects, deterministic completion.
- `retrieval_timeout=None` (production: 5 s per store call, skip-on-timeout)
  — a silent skip would corrupt metrics; slowness should fail loud.
- No KB bucket (LongMemEval has no `kb_write`); `kb_notes` binds inert,
  same as a session without Neo4j.
- `created_at` is ingestion time, so the recency channel ranks by haystack
  order (which is chronological) rather than real timestamps;
  `ingestion.date_prefix: true` optionally embeds session dates in content
  for temporal-reasoning A/Bs.
- The extraction-window cap (`_MAX_OBSERVATION_WINDOW`) applies as in
  production.

## Layout

```
arms/        committed arm YAMLs (config variants under test)
fixtures/    tiny committed LongMemEval-schema dataset (tests + smoke)
data/        real datasets (gitignored)
runs/        results.jsonl / summary.json / report.md / run.log (gitignored)
datasets.py  loader + typed records + stratified subsetting
arms.py      ArmSpec + production-loader config resolution
infra.py     DB/migrations/embeddings/aux-LLM/manager builders + scope uuids
ingest.py    seam + verbatim session replay (capture events)
query.py     question-time assemble + provenance → session ranking
metrics.py   Recall@k / NDCG@k / coverage / aggregation
run.py       CLI runner (resume-safe, concurrent per question)
report.py    markdown rendering + arm-vs-arm deltas
```

Tests: `tests/test_memory_eval_harness.py` (fully offline — fakes via the
`HarnessHandles` factory seams).

## Phase-2 slices still to build here

- **End-task accuracy**: answer questions with a reader LLM over the
  injected block; calibrated LLM-judge (>97 % agreement target on a
  hand-labelled slice); abstention sub-score. Scored separately from
  retrieval by design (reading is its own bottleneck — brief 05).
- **Contradiction-survival probe**: store fact → supersede → query
  (current or stale?) — the Phase-4 acceptance metric.
- **Production-trace set**: bespoke second dataset from real jobs/threads
  once the LongMemEval loop is trusted.
