# Postgres Audit Store — Implementation Package

**Status: ✅ SHIPPED — PR 1–7 merged on `develop` and deployed via Fleet; the Postgres cutover is LIVE on the dev cluster as of 2026-06-19.** Remaining: ~24 h soak → Mongo data wipe → PR 8 (Mongo code/chart removal). Full detail in [§ Current status (2026-06-19)](#current-status-2026-06-19--shipped-cutover-live-on-dev) below; the package text after it is the as-built reference.

Provenance: produced by an 8-agent discovery run — four code agents extracted
the definitive write/read/cockpit/infra contracts from the current tree, three
research agents pulled best practices with sources (partition management,
asyncpg/PgBouncer/tuning, prior-art migrations), and a synthesis agent produced
the final schema + adapter spec. The DDL below was then **applied and
smoke-tested on the live k3d cluster (PostgreSQL 15.18)**: single-transaction
apply, partitions/reloptions/indexes inspected, two-phase write + JOIN-merge
read + CHECK constraint exercised, scratch database dropped.

> **Addendum (2026-06-11)** — the database-architecture review
> (`database_architecture.md`) designates `srw-auditdb` as the product's
> **observability tier**: the usage-metering ledger (`usage_events`,
> `observability_and_quotas.md`) lands here too, as `migrations/audit/0002`
> when metering Slice 1 starts. Additive; no change to this package's DDL,
> adapter, or PR plan.

Companion documents:
- `postgres_audit_store.md` — the design doc (problem, rationale, scope). Its
  § Schema / § Adapter technical core is **superseded by this package** where
  they disagree (see § Errata).
- `postgres_audit_store_roadmap.md` — phase/gate structure. Still valid; the
  PR plan in § 8 refines its P0–P8 with what discovery found.
- `postgres_audit_store_contracts.md` — the four verbatim code-contract
  reports (write side, read side, cockpit, infra). Reference material for
  implementation; every claim carries file:line anchors.

How to review this in one pass: read § 1 (what was decided and why), § 2
(what discovery corrected — this is where the surprises are), skim § 3–5
(the artifacts: DDL, partition module spec, adapter interface), then § 8–10
(PR plan, day-1 checklist, the short list that needs your sign-off).

---

## Current status (2026-06-19) — SHIPPED, cutover live on dev

The migration is implemented end-to-end and **live on the dev cluster**. Postgres
(`srw-auditdb`) is the active audit backend; Mongo is no longer in the audit path.

**PRs (merged on `develop`, deployed via Fleet):**

| PR | Scope | State |
|----|-------|-------|
| 1 | `migrations/audit/0001` (3 partitioned tables) + partition module (`audit_partitions.py`) + audit-DB lifespan wiring | ✅ merged |
| 2 | Agent `SyncAuditWriter` (daemon thread + private loop) + archiver PG branches | ✅ merged |
| 3 | `AuditStore` reader (two-phase stitch, filters, `step_number`) | ✅ merged |
| 4a / 4b | Orchestrator reader selector + 13 read sites; Cockpit id-normalization (`_id`↔`id`) | ✅ merged |
| 5 | Helm `srw-auditdb` (StatefulSet, secret keys, NetworkPolicies, `wait-for-auditdb` init) | ✅ merged + k3d-verified |
| 6 | `AUDIT_BACKEND` chart flag + flip to `postgres` on k3d (in-cluster write+read e2e PASS) | ✅ merged |
| 7 | Cutover: chart default `mongodb`→`postgres` + `website/generator.mjs` secret-skeleton drift fix | ✅ merged + **deployed to dev** |

**Verified live on dev (2026-06-19):** `srw-auditdb-0` Ready; orchestrator logs
`Audit store (read) connected` → `Audit reads served by Postgres AuditStore
(AUDIT_BACKEND=postgres)` → `Database migrations applied` → `audit_partitions:
maintenance loop started`; `schema_migrations` shows `0001_initial.sql` succeeded;
the partition/index tree is present.

**Full-job e2e validation (2026-06-20) — closes PR 6's "no full live LLM job" caveat.**
PR 6 proved the write path by composition (agent gets the right env + writer unit
tests + a pod-with-env write/read), but had not driven a real agent job. That gap
is now closed on k3d:
- **Local suite 41/41:** `test_archiver_pg` (10, DB-free PG branch) +
  `test_audit_pagination`/`test_llm_requests_filter` (9, Mongo-fallback reader
  contract — still wired pre-PR 8) + testcontainers `test_audit_writer` /
  `test_audit_store_reader` / `test_audit_store` (22, real `postgres:16`
  writer→reader roundtrip: stitch, global `step_number`-under-filter, version,
  `token_usage`+status, `get_request`, partitioning/LZ4/CHECK/fail-loud, partition
  maintenance).
- **Live worker job** (`gemma-4-moe-strix`) wrote all three tables to `srw-auditdb`:
  `agent_audit` 140 (73 pre / 67 post — every post carries `pre_id`, llm-posts
  carry `request_id`), `llm_requests` 34, `chat_history` 34 (with `reasoning`
  captured).
- **All nine read endpoints HTTP 200** through the live orchestrator + the real
  BFF cookie auth: `/audit` (+ `filter=tools|messages|errors|all`, global
  `step_number` preserved), `/audit/timerange`, `/chat`, `/audit/bulk`,
  `/chat/bulk`, `/version` (counts match), `/llm-requests` (`token_usage` +
  `call_type` filter), `/requests/{id}`. Stitched logical rows carried merged
  post data (latency, `tool`/`llm` sub-objects); `step_number` strictly increasing.
- **MCP** reads audit via the same orchestrator REST API (`mcp/client.py` →
  `/jobs/{id}/audit|chat|…`) → transitively covered; MCP healthy. Error-row path
  (status folded into `metadata` + status filter) is covered by unit tests (the
  live job had zero errors). No regressions.

**Deploy gotcha hit + resolved (the ESO/Vault trap):** the chart's new
non-optional `AUDIT_POSTGRES_USER`/`AUDIT_POSTGRES_PASSWORD` secret keys were not
in Vault, so `srw-auditdb` sat in `CreateContainerConfigError` ("couldn't find key
AUDIT_POSTGRES_USER in Secret …/srw") for ~44 min and blocked the orchestrator's
`wait-for-auditdb` (no outage — the pre-cutover orchestrator pod kept serving).
Fixed by adding both keys to Vault path
`secret/homelab/superhuman-remote-worker/srw-secrets` (user `srw`, fresh password
— the DB was empty/uninitialized) → ESO sync → recovery. **Operational rule:** in
any ESO-backed env, add the Vault `AUDIT_POSTGRES_*` keys *before* bumping the
chart (ESO refresh is 1 h; force with the `force-sync` annotation).

**Pre-wipe Mongo backup (safety net):** dev `srw_logs` dumped 2026-06-19 14:44 →
`~/srw-audit-mongo-backup/srw_logs-20260619-144410.archive.gz` (1.4 GB,
276,543 docs: agent_audit 189,375 + llm_requests 43,584 + chat_history 43,584;
sha256 `987f86ab842daf2dacb07af15c4d6423298573c78574ea8d796a003dd03598e7`),
verified (mongodump exit 0 + `gzip -t` + `mongorestore --dryRun`). Restore:
`mongorestore --archive=<file> --gzip`. `srw-prod-private` deliberately not backed
up (never used).

**Remaining (the cutover tail):**
1. **~24 h soak** on dev — real jobs/sessions write to `srw-auditdb`; cockpit
   Audit/Chat/Graph render from Postgres (old jobs read empty — expected, their
   data is stranded in Mongo); partition maintenance stays healthy.
2. **Mongo wipe** — fresh incremental dump (14:44→flip tail) then drop `srw_logs`
   / delete its PVC (~2.7 GB reclaimed).
3. **PR 8 cleanup** — remove chart `mongodb`/`mongo-express`, the Mongo modules +
   `motor` (orchestrator image) + `MONGODB_URL`, and the transitional `_id`
   dual-reads. (G5: `pymongo` stays — customer datasource tools.)

**Scope deltas from the original plan (lean cut, agreed with the user):** P6's
formal A/B + perf gate (G3) was not run as a separate phase — validation was
component tests (writer 14/14, reader 40/40 against real `postgres:16`) + the k3d
in-cluster write+read e2e + the dev cutover checks above. Auto-retention
(`retire_partitions`) ships as a deferred no-op stub; partitions accumulate until
a later retention pass. The deferred Dockerfile `postgresql-client` + `init.py`
`pg_dump` backup were dropped (backup was already dead code; the orchestrator
talks to auditdb via asyncpg).

---

## 1. Decision record

Gates closed (roadmap G0–G6):

| Gate | Decision |
|------|----------|
| G0 partitioning | **Hand-rolled** (~200 LoC module, spec in § 4). Stock `postgres` images lack pg_partman; an extension + its scheduling adds more weight than the module. Reverses the design doc's pg_partman lean. |
| G1 test infra | `testcontainers[postgres]` via new `requirements-dev.txt`. |
| G2 default partition | **None, ever** (blocks `DETACH CONCURRENTLY`, parks misrouted rows). |
| G3 perf gate | Unchanged — within 20% of Mongo baseline, plus the concurrency smoke (~100 writers). Blocks cutover. |
| G4 cutover wipe | Authorized (you, earlier). |
| G5 pymongo | Stays (customer datasource tools). `motor` (orchestrator image) is deletable at P8. |
| G6 Compose | **Skip all Compose work.** Pre-cutover Compose runs the Mongo default; post-cutover it gets the no-op archiver, same as "Mongo unavailable" today. Nobody runs Compose; deprecation proceeds independently. |

Synthesis decisions (each grounded in the verified contracts or cited
research; full contract evidence in `postgres_audit_store_contracts.md`):

**D1 — Foreign keys between audit tables**
No FK constraints anywhere. agent_audit.request_id and chat_history.request_id become plain BIGINT soft references (kept as columns, commented as such, unindexed); orphans after llm_requests' 90-day window are by design.
*Rationale:* Both sides are partitioned and retention windows are inverted (chat_history 365d > llm_requests 90d): with real FKs, every llm_requests partition would still be referenced at detach time and DETACH/DROP errors by design (confirmed expected behavior by the feature's author) — retention becomes permanently unexecutable. Additionally detach of a referenced partition SHARE-locks every referencing (hot) table, and partitioned-FK ATTACH/DETACH is the exact code path patched for catalog corruption in 15.9/16.5/16.9. Soft references are the established log-store norm (GitLab loose FKs, TimescaleDB refuses hypertable-referencing FKs, Hatchet's FK-deletion post-mortem).

**D2 — Pre/post correlation column**
New agent_audit.pre_id BIGINT (NULL on pre rows, = pre row's id on post rows), with CHECK ((event_phase='pre') = (pre_id IS NULL)) and a partial index (pre_id) WHERE event_phase='post'. request_id remains exclusively the llm_requests link.
*Rationale:* The design doc said post rows correlate by 'matching request_id', conflating two different links: tool steps have no llm_requests row at all, so request_id cannot pair tool pre/post. A dedicated pre_id makes the stitch unambiguous for both chains; the CHECK also reproduces Mongo's failure shape when update_* is called with a None doc id (insert rejected, swallowed, False).

**D3 — Append-only read contract (resolves reader M5)**
Server-side JOIN-merge: every agent_audit read returns one logical row per pre row with the latest post row overlaid via LEFT JOIN LATERAL ... ORDER BY id DESC LIMIT 1 and a two-level jsonb merge (top level, then re-merge the 'tool'/'llm' subobject). totals, step_number, auditEntryCount, get_audit_count all count pre rows.
*Rationale:* Of the three contracts floating in the docs, DISTINCT-ON-latest is simply wrong (post rows lack tool.arguments) and wire-doubled+cockpit-collapse changes every count, the step_number cadence, IndexedDB sync semantics, and requires cockpit work. JOIN-merge is the only one that reproduces the Mongo wire shape and all counts exactly — zero cockpit logic changes. jsonb || is shallow, so the post payload carries only the $set delta nested under one key and the read merges that key explicitly; expression verified live to reproduce the merged Mongo document field-for-field.

**D4 — Post-row insert mechanism**
insert_audit_post is INSERT ... SELECT FROM agent_audit p WHERE p.id=$1 AND p.event_phase='pre', deriving job_id and the descriptive columns (agent_type, iteration, step_type, node_name, phase, phase_number) from the pre row; returns rowcount==1.
*Rationale:* update_tool_result/update_llm_response signatures carry only the audit doc id — no job_id — and job_id is NOT NULL on agent_audit. INSERT...SELECT closes that gap in one statement with no client-side state, and its 0-rows-inserted case restores Mongo's modified_count>0 False-when-missing semantics exactly.

**D5 — Primary keys on partitioned tables**
PRIMARY KEY (id, timestamp) on all three tables; id stays BIGSERIAL (not IDENTITY).
*Rationale:* Verified live: the design doc's `id BIGSERIAL PRIMARY KEY` is invalid SQL on a partitioned table ('unique constraint on partitioned table must include all partitioning columns'). id remains globally unique by sequence construction (single sequence, append-only). BIGSERIAL because identity-column support on partitioned tables only became complete in PG 17.

**D6 — Storage parameters placement + reloption name**
fillfactor/autovacuum settings are applied per leaf partition (bootstrap DO block + partition helper), never on the parents; the freeze knob is autovacuum_freeze_min_age=0.
*Rationale:* Verified live: ALTER TABLE <partitioned parent> SET (fillfactor/autovacuum_*) fails with 'unrecognized parameter' — the design doc's parent-level ALTERs would abort the migration. Its `vacuum_freeze_min_age` is also not a reloption (verified error); the per-table parameter is autovacuum_freeze_min_age, which with insert-driven autovacuum (PG13+) freezes append-only pages immediately.

**D7 — Index set**
Five indexes total beyond PKs: llm_requests (job_id,timestamp); agent_audit (job_id,id) WHERE pre, (job_id,step_type,id) WHERE pre, (pre_id) WHERE post; chat_history (job_id,timestamp). Dropped from the design doc: the metrics GIN, (call_type,timestamp), the global expression index on payload->'tool'->>'name', and chat_history(request_id).
*Rationale:* Every dropped index has zero consumers: the metrics-GIN/call_type consumers are the dead get_job_stats/get_audit_stats pipelines; the expression index can't outperform (job_id,step_type,id) for the only graph-delta queries that exist (all job-scoped); no reader joins chat→llm_requests. Partial WHERE event_phase clauses keep post rows out of the hot logical-row indexes. Fewer per-partition indexes also preserves the 16 fast-path lock slots per backend (the real partition-count ceiling on 15/16). Later additions use the documented parent-ONLY + per-partition-CONCURRENTLY + ATTACH procedure in a .notx.sql migration.

**D8 — llm_requests.agent_id column**
Dropped (design doc declared `agent_id UUID`).
*Rationale:* The write contract proves no writer emits such a field — agent_type already carries config.agent_id values (naming crossover documented in a COMMENT). A NULLable column with no writer is schema noise; adding one later is an instant ALTER on the parent.

**D9 — auxiliary_metadata column name**
Column named auxiliary_metadata (design doc had auxiliary_meta).
*Rationale:* /api/requests/{id} returns the row as the wire document; the Mongo key is auxiliary_metadata. Matching the column name means no per-field rename mapping in the reader.

**D10 — step_number synthesis**
ROW_NUMBER() OVER (ORDER BY id) computed over ALL pre rows of the job first, then step_type filtering/pagination applied outside the window; same CTE for paginated, bulk, graph and iter_tool_calls paths.
*Rationale:* Mongo documents keep their global per-job step_number when FilterCategory narrows the result — numbering the filtered subset would renumber 1..n and break parity (and graph-delta stepNumber would disagree with the audit pane). Window-then-filter is correct by construction; cost is bounded because every such query is job-scoped, exactly like Mongo's count+skip was.

**D11 — Ids on the wire**
_id:string(ObjectId) → id:int everywhere; archive()/audit_*() return raw int (≥1); payload.llm.request_id and chat_history.request_id become ints; graph-delta toolCallId stays str(id); event_phase/pre_id/hard request_id are omitted from wire docs.
*Rationale:* Follows the design doc's id:number + rename recommendation. Int return through the archiver is safe: the only consumers pass ids through opaquely and guard with truthiness (BIGSERIAL never yields 0). toolCallId is an opaque uniqueness key in the cockpit, so keeping it a string minimizes model churn. Internal stitch columns never existed in Mongo docs, so byte-parity demands omitting them.

**D12 — /api/requests/{doc_id} route param**
Param stays `doc_id: str`; the adapter int-parses and returns None on garbage → existing auth-then-404 flow. Overrides the design doc's `doc_id: int`.
*Rationale:* Reader contract M6: a typed int param makes FastAPI 422 non-numeric probes BEFORE any auth dependency runs — a wire-visible status change and an unauthenticated probe surface. str+parse keeps today's invalid==missing 404 semantics and auth ordering with one line of adapter code.

**D13 — /version computation**
Single SQL statement (counts via FILTER + scalar subqueries) replacing four sequential Mongo queries; exposed counts are logical (pre rows); the version hash gains a 4th component (raw row count incl. post rows): hash((audit, chat, graph, row_count)).
*Rationale:* Roadmap requires single-query race-freedom. Logical exposed counts keep auditEntryCount == /audit total == bulk row count (the consistency the reader contract demands). The 4th hash component fixes a real staleness hole at zero wire cost: under Mongo, in-place $set result backfills never changed the version, so IndexedDB could cache forever-running tool entries; post-row INSERTs now bump the (equality-compared-only) version.

**D14 — list_llm_requests token_usage (reader M4)**
Fix rather than preserve: emit token_usage from metrics->'token_usage'.
*Rationale:* The Mongo projection asked for a top-level field the writer never wrote — the endpoint docstring promises token usage and silently never delivered. The data exists nested; emitting it is additive (new key) and cannot break the cockpit.

**D15 — Agent-side write mechanism**
One daemon writer thread owning a private asyncio loop + asyncpg pool (min 1 / max 2); sync facade methods block via asyncio.run_coroutine_threadsafe(...).result(timeout=15). One-shot connect gate (first failure permanently disables, parity) and never-closed lifecycle kept.
*Rationale:* Write contract reality: sync methods called un-awaited from the event loop AND from ThreadPoolExecutor threads (vision/audio), with three id-returning pre-methods — so the seam must be sync, thread-safe, and synchronous-returning. Blocking one INSERT round-trip is exactly today's pymongo behavior. run_coroutine_threadsafe is the only mechanism that serves both calling contexts with one code path. Rejected: psycopg3 sync pool (second driver for the same SQL), per-call asyncio.run (connection churn), fire-and-forget queue (cannot return server-generated BIGINT ids), writes via orchestrator API (recouples the failure domains the dedicated instance separates). asyncpg already ships in the agent image.

**D16 — synchronous_commit scope**
server_settings={'synchronous_commit':'off'} on the write pool only; read pool and the migration-runner role default stay synchronous.
*Rationale:* Per-session scoping is the documented pattern; worst-case loss is ~3× wal_writer_delay (~600 ms) of audit trail with zero corruption risk — acceptable for a non-load-bearing log and a large latency win on the agent hot path (every write blocks the event loop). Not role-wide so migration bookkeeping keeps synchronous durability.

**D17 — statement_cache_size**
0 on both pools.
*Rationale:* The asyncpg-documented bulletproof setting for PgBouncer transaction mode; design invariant 2 plans PgBouncer in front of srw-auditdb beyond ~100 concurrent agents, and prepared statements buy almost nothing for single-table INSERTs and short job-scoped SELECTs. Avoids ever revisiting the __asyncpg_stmt_N__ failure class.

**D18 — Pool topology**
Read pool lives in the orchestrator (AuditStore, min1/max5); write pool lives in the agent process (SyncAuditWriter, min1/max2). The design doc's 'instantiate PostgresDB twice in the orchestrator' is corrected.
*Rationale:* The write contract proves the orchestrator process contains zero audit writes (grep-verified) — a throughput write pool there would serve nobody. The writer runs where the writes are: each agent pod.

**D19 — Partition management**
Hand-rolled module (orchestrator/services/audit_partitions.py): advisory-locked catalog-diff creation with CREATE(LIKE INCLUDING ALL)+CHECK+ATTACH, N+2 monthly lookahead, DETACH CONCURRENTLY → 3-day grace → DROP with FINALIZE recovery, parent ANALYZE cadence, lookahead/23514/detach-pending alarms; 6h ± 30min-jitter lifespan task. Reverses the design doc's pg_partman resolution.
*Rationale:* The chart's stock postgres images don't ship pg_partman, and an extension + its BGW/cron config is heavier than ~200 LoC for 3 tables at monthly cadence; every mechanism is research-verified and was live-tested (ATTACH path, reloptions, bound introspection, 23514). The doc's own fallback ('if the team prefers extension-freedom, take the hand-rolled path with the full spec') is what this delivers. Steady state ≈ 21 attached partitions — two orders of magnitude below planner limits.

**D20 — Retention granularity**
Whole-month retention: a partition is retired when its UPPER bound is older than the window, so effective retention is 90–120d / 365–396d.
*Rationale:* Partition-drop retention only works at partition granularity; erring on the keep side is the only safe direction for an audit trail. Documented so nobody files 'rows older than 90d still visible' as a bug.

**D21 — Dead code disposition**
Not ported: the four archiver aggregation pipelines (get_job_stats/get_audit_stats) and the seven dead orchestrator read members (get_page_for_timestamp, get_job_ids_with_audit, get_chat_history_count, get_job_audit_trail, get_llm_conversation, get_statistics, db property). One genuinely new method added instead: get_audit_counts(job_ids) for the N+1 enricher collapse.
*Rationale:* Write/read contracts both verified zero callers repo-wide for all of these (the MCP get_job_stats hits a Postgres job-queue endpoint, unrelated). Porting them would create an untested, uncalled API surface; the SQL translations are trivial to write later if a consumer appears.

**D22 — Timestamp precision and nested datetimes (reader M9)**
Top-level timestamps: tz-aware from asyncpg, serialized by the existing converters → millisecond precision on the wire (accepted change from µs-rendered). Nested datetimes (started_at/completed_at, etc.): the writer serializes them to UTC ISO-8601 'Z' strings (µs) at write time inside JSONB.
*Rationale:* Mongo/BSON only ever stored milliseconds — today's 6-digit rendering is zero-padded ms, so ms truncation loses nothing real and both existing converter paths already handle tz-aware values. JSONB has no datetime type, so nested values must be strings; writing them in the same ISO+Z shape the encoder produces today keeps the wire format identical without read-side special-casing.

**D23 — job_id typing**
UUID NOT NULL columns; adapters cast str→uuid on write and read, return str(uuid) on the wire; non-UUID job_id reads return the empty/None shape, non-UUID writes fail-soft into the standard swallow-and-warn path.
*Rationale:* Job ids are UUIDs everywhere in this system (jobs.id PK; /llm-requests already 400s non-UUIDs). UUID columns halve key width vs text and enable the jobs ⨝ llm_requests joins the design sells. Mongo accepted arbitrary strings, so the adapters must catch cast failures rather than 500 (reads) or raise (writes) — same blast radius as any swallowed write error.

**D24 — graph_routes access (reader M7)**
iter_tool_calls(job_id) replaces the raw _db['agent_audit'] cursor, chunked by id keyset (1000/row batches), and the route gains require_job_access.
*Rationale:* The contract flags /api/graph/changes as the only unauthenticated audit read and the design doc names the iter_tool_calls refactor as the natural moment to add the gate. Keyset chunking bounds memory on today's unbounded cursor without pinning a server-side cursor transaction.

**D25 — Migration determinism**
SET LOCAL timezone='UTC' at the top of 0001_initial.sql; partition bounds computed via date_trunc('month', now()), never hardcoded; bootstrap ANALYZE of the three parents at the end.
*Rationale:* Range bounds for timestamptz columns are interpreted in the session timezone at DDL time — pinning UTC makes month boundaries deterministic across differently-configured servers (SET LOCAL scopes to the runner's transaction). The seed ANALYZE establishes parent stats entries immediately since autovacuum never will (Hatchet's production bug class).

**D26 — Degraded-mode semantics (reader M8)**
is_available stays a startup-latched bool; runtime DB loss yields exceptions/500s; the per-endpoint 200+error / 200-null / 503 matrix is preserved verbatim in main.py.
*Rationale:* Exact parity with today's contract (the latched _available plus main.py pre-checks). Live pool-health probing would silently change endpoint behavior under partial outages — out of scope for a backend swap whose verification gate is byte-equivalence.


### Open items needing your sign-off (the only ones)

- Pin the srw-auditdb image version in helm values: recommend PostgreSQL 16, minor >= 16.5 (DETACH CONCURRENTLY crash fix + partitioned-FK catalog fixes shipped in 15.9/16.5). Everything delivered here was verified compatible on the cluster's 15.18, so 15.9+ also works if a 15.x image is preferred for fleet uniformity — human picks the tag.
- PgBouncer in front of srw-auditdb (design invariant 2) is deliberately deferred until a deployment approaches ~100 concurrent agents — operational trigger, not code; when it lands, transaction mode + the 1.24+ max_prepared_statements default are pre-cleared by statement_cache_size=0.
- The live A/B verification run (design doc Verification #2: same job under AUDIT_BACKEND=mongodb vs =postgres, diff the /api/jobs/{id}/audit JSON and cockpit panes) remains the human-run acceptance gate — byte-equivalent except id:int and ms-precision timestamps per the decisions log.

Plus four judgment calls bundled into the decisions above, called out for
explicit approval because they are user-visible behavior changes:

1. **`/api/graph/changes/{job_id}` gains `require_job_access`** — today it is
   the only unauthenticated audit read (and an unbounded scan). This is a
   security fix riding along; flagged in case anything external relies on it.
2. **`/api/jobs/{id}/llm-requests` starts emitting `token_usage`** — the Mongo
   projection asked for a top-level field the writer never wrote, so the
   docstring-promised field has been silently absent forever. Additive fix.
3. **Version hash gains a 4th component** (raw row count) — fixes a real
   IndexedDB staleness hole (under Mongo, in-place result backfills never
   bumped the version). Zero wire-shape change.
4. **Silent-failure parity is deliberately kept** (one-shot connect gate,
   swallow-all writes). This perpetuates the failure mode tracked in
   `docs/issues/surface_silent_aux_failures.md`; the writer now at least logs
   SQLSTATEs greppably (23514 = missing partition). Recommendation: keep
   parity for the swap, then address surfacing in that issue — not here.

---

## 2. Errata — what discovery corrected in the existing docs

Severity-ordered. The first three would each have **aborted the migration at
first run**; all were verified live against PostgreSQL 15.18.

1. **`id BIGSERIAL PRIMARY KEY` is invalid SQL on a partitioned table** (unique
   constraints must include the partition key). Fixed: `PRIMARY KEY (id,
   timestamp)`; `id` stays globally unique by sequence construction.
2. **Parent-level storage parameters fail** — `ALTER TABLE <partitioned parent>
   SET (fillfactor/autovacuum_*)` errors with "unrecognized parameter". Fixed:
   reloptions per leaf partition (bootstrap + partition module).
3. **`vacuum_freeze_min_age` is not a reloption** — the per-table knob is
   `autovacuum_freeze_min_age`. Fixed in DDL.
4. **The FK design was unbuildable for retention.** FKs between partitioned
   tables are supported (PG 12+), but detaching a referenced partition that
   still has referencing rows **fails by design** (confirmed by the feature's
   author) — and `chat_history` (365d) referencing `llm_requests` (90d) means
   llm_requests retention would be permanently impossible. This exact
   configuration also had catalog-corruption bugs patched as late as
   15.9/16.5. Fixed: **no FK constraints**; `request_id` columns are plain
   BIGINT soft references (the GitLab/Timescale/pg_partman-author norm for
   partitioned log stores).
5. **`request_id` cannot pair tool pre/post rows** (tool steps have no
   llm_requests row) — the design doc's "correlate by matching request_id" was
   wrong for half the two-phase traffic. Fixed: dedicated `pre_id` column +
   CHECK + partial index; `request_id` remains exclusively the llm_requests
   link.
6. **The "four pipelines we actually use" are dead code** — `get_job_stats` /
   `get_audit_stats` live in the *agent-side* archiver with **zero callers
   repo-wide** (the MCP tool of the same name hits a Postgres job endpoint).
   Seven more `mongodb.py` read members are equally dead. None are ported.
7. **The write pool belongs in the agent, not the orchestrator** — grep-proven:
   the orchestrator process performs zero audit writes. The design doc's
   "instantiate PostgresDB twice in the orchestrator" is replaced by read pool
   (orchestrator) + write pool (agent, daemon writer thread — see D-series).
   Corollary discovered: vision/audio archive calls run on **ThreadPoolExecutor
   threads**, so the write seam must be thread-safe sync, not loop-bound.
8. **Cockpit cache invalidation plan was a no-op** — `JobCacheMetadata.version`
   is written but **never read**; the only validity check is an
   `auditEntryCount` equality. The doc's "bump cache.model.ts version" does
   nothing. Fixed in PR 4: real version check (or Dexie upgrade-clear) + the
   cache-miss path gains a `clearJob` (today stale rows survive `bulkPut` and
   duplicate).
9. **Cockpit blast radius was undercounted**: `request_id` fields
   (`audit.model.ts:43`, `chat.model.ts:63`) are also stringified ObjectIds —
   two `.slice()` template calls are **runtime-fatal** with ints
   (`chat-history.component.ts:191`, `agent-activity.component.ts:156`), one
   untyped access feeds the 24-hex regex silently. And `formatters.py:491/:574`
   read `entry["_id"]` by name — silent display break under the rename (the
   docs claimed formatters were clean).
10. **`/api/requests/{doc_id}` must stay `str`** — the planned `int` type
    would make FastAPI 422 malformed ids *before auth runs* (new
    unauthenticated probe surface + wire change). Adapter int-parses; invalid
    == missing == 404-after-auth, exactly today's semantics.
11. **`mongodump` is not in the orchestrator image** — today's in-container
    backup path is already dead code. The pg_dump replacement needs
    `postgresql-client` added to `docker/Dockerfile.orchestrator` (runtime apt
    list) — missing from both docs.
12. **Workspace NetPol 27017 egress does NOT need to stay** — it's
    podSelector-scoped to the chart's mongodb component, so it becomes dead
    code post-cutover (customer Mongo on 27017 was never reachable through it;
    the tier allowlist blocks it anyway). Design doc said "must therefore
    stay" — wrong.
13. **Helm external-mode convention is `externalHost/Port/Db`**, not
    `externalUrl` — and `values.example.yaml` + `customer-external-values.yaml`
    are *already stale* for postgres/vector (they carry `externalUrl` keys the
    helpers ignore; masked because `helm lint` reports missing `required`
    values as INFO, i.e. **there is no real render gate in CI**).
14. **`is_available` is startup-latched** — the documented per-endpoint
    degraded shapes only engage when the store was down *at boot*; runtime
    loss = 500s. Parity is preserved deliberately (D-series); the degraded
    matrix is real but mostly-latent.
15. **Anchor drift fixed**: graph_routes raw access is `:142` (not 144);
    `/llm-requests` endpoint `main.py:14623` (handler call `:14646`); third
    enricher `main.py:18816`; `values.yaml` mongodb block `404-418`,
    mongoExpress `881-890`. (`main.py` is locally modified on this branch —
    re-grep anchors at implementation time as always.)
16. **Latent, out of scope, filed for awareness**: the fallback
    `persistent_provisioner.py` builds agent pods **without** the chart labels
    every DB NetworkPolicy selects on — those pods are blocked from all DBs
    today (pre-existing). The primary `agent_provisioner.py` path is correct.

---

## 3. The migration — `orchestrator/database/migrations/audit/0001_initial.sql`

Validation evidence: applied via `psql --single-transaction -v ON_ERROR_STOP=1`
on the cluster's PostgreSQL 15.18 → all statements succeeded; 3 partitioned
parents + 3 monthly leaves each (current + N+2); per-leaf reloptions verified;
index set verified; two-phase INSERT + `INSERT...SELECT` post row + JOIN-merge
read reproduced the exact Mongo wire shape; CHECK rejected a malformed post
row. Copy verbatim at PR 1.

```sql
-- ============================================================================
-- migrations/audit/0001_initial.sql — PostgreSQL audit store (replaces MongoDB
-- collections llm_requests / agent_audit / chat_history).
--
-- Runs inside the migration runner's single transaction (advisory-locked,
-- checksum-tracked — orchestrator/database/migrate.py). No CREATE DATABASE,
-- no CREATE EXTENSION required. Targets PostgreSQL 16 (15.9+ compatible).
-- Requires a server built --with-lz4 (true for official/PGDG images); the
-- SET COMPRESSION statements below fail loudly otherwise.
--
-- Design: docs/features/postgres_audit_store.md. Three invariants enforced
-- here and in review:
--   1. agent_audit is APPEND-ONLY. The Mongo two-phase insert+update becomes
--      two INSERTs (event_phase 'pre' then 'post', correlated by pre_id).
--      Never add an UPDATE path.
--   2. DO NOT add a GIN index on agent_audit.payload (or any payload/request
--      JSONB column). It would re-introduce the JSONB write-amplification
--      class this design exists to avoid, and "search arbitrary payload
--      fields" is explicitly a non-goal (derived columns or an external
--      index are the answer if that feature ever lands).
--   3. NO foreign keys between these tables — see the soft-reference note
--      on agent_audit.request_id below.
--
-- Partition maintenance (creation beyond the bootstrap below, retention,
-- parent ANALYZE) is owned by orchestrator/services/audit_partitions.py,
-- NOT by later migrations: DETACH PARTITION CONCURRENTLY cannot run inside
-- the runner's transaction.
-- ============================================================================

-- Deterministic month boundaries for partition bounds regardless of server
-- timezone config (SET LOCAL: scoped to the runner's transaction).
SET LOCAL timezone = 'UTC';

-- ============================================================================
-- llm_requests — one row per LLM call (main loop + auxiliary/vision/audio).
-- Mirrors LLMArchiver.archive() (src/core/archiver.py): every Mongo document
-- field has a column or a JSONB home. INSERT-only, no post phase.
-- ============================================================================
CREATE TABLE llm_requests (
    id                  BIGSERIAL,
    job_id              UUID         NOT NULL,
    agent_type          TEXT,
    call_type           TEXT         NOT NULL DEFAULT 'main',
    model               TEXT         NOT NULL,
    iteration           INTEGER,
    timestamp           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms          INTEGER,
    request             JSONB        NOT NULL,
    response            JSONB        NOT NULL,
    metadata            JSONB,
    auxiliary_metadata  JSONB,
    metrics             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- A bare BIGSERIAL PK is invalid on a partitioned table (unique
    -- constraints must include the partition key). id is still globally
    -- unique in practice: single sequence, append-only, no id reuse.
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE llm_requests IS
    'Full LLM request/response archive, one row per call. Written by the '
    'agent''s LLMArchiver only; read by /api/requests/{id} and '
    '/api/jobs/{id}/llm-requests. Monthly partitions, 90-day retention.';
COMMENT ON COLUMN llm_requests.agent_type IS
    'Naming crossover preserved from Mongo: carries config.agent_id values '
    '(e.g. "universal", "vision", "transcription"), not a pod identity. '
    'There is no agent_id column because the writer has no such field.';
COMMENT ON COLUMN llm_requests.call_type IS
    'main | summarization | memory_extraction | memory_assembly | '
    'knowledge_curation | auxiliary | vision | transcription (open set).';
COMMENT ON COLUMN llm_requests.timestamp IS
    'Writer-supplied UTC insert time (partition key). DEFAULT now() is a '
    'backstop only.';
COMMENT ON COLUMN llm_requests.request IS
    'Full untruncated message bodies: {messages:[...], message_count, '
    'tools?, tool_count?, model_kwargs?}. Dominates row size — LZ4 TOAST.';
COMMENT ON COLUMN llm_requests.metrics IS
    '{input_chars, output_chars, tool_calls, token_usage:{...}}. '
    'token_usage lives HERE (nested), not top-level — the reader surfaces '
    'it in /llm-requests.';

ALTER TABLE llm_requests ALTER COLUMN request            SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN response           SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN metadata           SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN metrics            SET COMPRESSION lz4;

-- Serves: list_llm_requests (job page ordered by timestamp) and per-job
-- maintenance queries. get_request(id) uses the PK's id prefix across the
-- few attached partitions. No call_type or GIN indexes: their only would-be
-- consumers (LLMArchiver.get_job_stats/get_audit_stats) are dead code with
-- zero callers. Add later via a .notx.sql migration if a consumer appears.
CREATE INDEX llm_requests_job_ts_idx ON llm_requests (job_id, timestamp);

-- ============================================================================
-- agent_audit — APPEND-ONLY step trace.
-- 'pre' rows are written at call dispatch (audit_step / audit_tool_call /
-- audit_llm_call); 'post' rows replace the old Mongo update_one (result
-- backfill) and carry only the delta payload. Readers stitch pairs via
-- pre_id and serve ONE logical row per pre row (counts, step_number and
-- wire shape therefore match the Mongo store exactly).
-- ============================================================================
CREATE TABLE agent_audit (
    id            BIGSERIAL,
    job_id        UUID         NOT NULL,
    agent_type    TEXT,
    iteration     INTEGER,
    step_type     TEXT         NOT NULL,
    node_name     TEXT,
    phase         TEXT,
    phase_number  INTEGER,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms    INTEGER,
    event_phase   TEXT         NOT NULL DEFAULT 'pre',
    pre_id        BIGINT,
    request_id    BIGINT,
    payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    metadata      JSONB,
    PRIMARY KEY (id, timestamp),
    CONSTRAINT agent_audit_event_phase_check
        CHECK (event_phase IN ('pre', 'post')),
    -- post rows must point at their pre row; pre rows must not.
    CONSTRAINT agent_audit_pre_id_check
        CHECK ((event_phase = 'pre') = (pre_id IS NULL))
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE agent_audit IS
    'Append-only agent step trace (tool calls, LLM calls, checks, errors, '
    'phase transitions, memory ops). Two-phase calls = two rows (pre/post) '
    'correlated by pre_id; one LOGICAL step per pre row. Monthly '
    'partitions, 90-day retention. NEVER UPDATE rows here and NEVER add a '
    'GIN index on payload (see file header).';
COMMENT ON COLUMN agent_audit.step_type IS
    'Open set; 13 values observed: initialize, llm, tool, check, warning, '
    'error, phase_transition, phase_complete, feedback_resume, '
    'memory_inject, memory_dedup, memory_store, memory_retrieve.';
COMMENT ON COLUMN agent_audit.event_phase IS
    '''pre'' = written at dispatch (tool/llm result fields null inside '
    'payload); ''post'' = second INSERT carrying the result delta that the '
    'Mongo store applied as an in-place $set.';
COMMENT ON COLUMN agent_audit.pre_id IS
    'On post rows: agent_audit.id of the pre row this completes. Soft '
    'self-reference, deliberately NOT a FK (self-referential FKs between '
    'partitions sit on the most corruption-prone partitioning code path '
    'of the 15.9/16.5/16.9 minor-release fixes, and add nothing here).';
COMMENT ON COLUMN agent_audit.request_id IS
    'Soft reference to llm_requests.id (set on llm post rows; surfaced to '
    'the wire inside payload.llm.request_id). DELIBERATELY NOT A FOREIGN '
    'KEY: partitioned-to-partitioned FKs make partition retention '
    'permanently unexecutable when windows differ (chat_history keeps 365d '
    'against llm_requests'' 90d, so referenced partitions would still be '
    'referenced at detach time and every DETACH errors), SHARE-lock the '
    'referencing tables during detach, and ride the same bug-ridden code '
    'path as pre_id above. Orphaned ids after the referenced row ages out '
    'are by design — treat as opaque correlation ids (GitLab loose-FK / '
    'TimescaleDB norm for log stores).';
COMMENT ON COLUMN agent_audit.payload IS
    'The writer''s free-form data dict (Mongo merged it at document top '
    'level; readers re-splat it over the row dict for wire parity). Known '
    'quirk preserved: at step_type=phase_complete the payload contains a '
    '"phase" OBJECT that shadows the phase TEXT column when splatted — '
    'readers must not assume phase=strategic|tactical on those rows. '
    'NO GIN INDEX — see file header.';

ALTER TABLE agent_audit ALTER COLUMN payload  SET COMPRESSION lz4;
ALTER TABLE agent_audit ALTER COLUMN metadata SET COMPRESSION lz4;

-- Logical-row (pre) indexes: every read path filters event_phase='pre' and
-- orders by id (the step_number source). Partial indexes keep post rows out.
CREATE INDEX agent_audit_job_id_idx
    ON agent_audit (job_id, id) WHERE event_phase = 'pre';
-- FilterCategory (step_type IN (...)) + graph-delta queries
-- (step_type='tool' AND payload->'tool'->>'name' = ANY(...)): the index
-- narrows to the job's tool rows; the name filter is applied per-row, which
-- is bounded because every such query is job-scoped. This replaces the
-- design doc's global expression index on payload->'tool'->>'name' (a
-- cross-job index cannot serve job-scoped queries better than this, and
-- fewer per-partition indexes preserves fast-path lock slots).
CREATE INDEX agent_audit_job_step_idx
    ON agent_audit (job_id, step_type, id) WHERE event_phase = 'pre';
-- Pre/post stitch join (post.pre_id = pre.id).
CREATE INDEX agent_audit_pre_id_idx
    ON agent_audit (pre_id) WHERE event_phase = 'post';

-- ============================================================================
-- chat_history — one row per main-loop turn (clean conversational delta).
-- Written only via the archive(call_type='main') cascade. 365-day retention.
-- ============================================================================
CREATE TABLE chat_history (
    id            BIGSERIAL,
    job_id        UUID         NOT NULL,
    agent_type    TEXT,
    iteration     INTEGER,
    model         TEXT,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    latency_ms    INTEGER,
    phase         TEXT,
    phase_number  INTEGER,
    request_id    BIGINT,
    inputs        JSONB        NOT NULL,
    response      JSONB        NOT NULL,
    reasoning     JSONB,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

COMMENT ON TABLE chat_history IS
    'Conversational delta per main-loop LLM turn: inputs since the last AI '
    'message + the response, with previews. Monthly partitions, 365-day '
    'retention (longer than llm_requests — a reason request_id is a soft '
    'reference, not a FK).';
COMMENT ON COLUMN chat_history.request_id IS
    'Soft reference to llm_requests.id (no FK — see '
    'agent_audit.request_id). Dangles by design once the llm_requests row '
    'ages out of its 90-day window.';
COMMENT ON COLUMN chat_history.iteration IS
    'Nullable but always written by the archiver (Mongo wrote null '
    'explicitly; readers treat null as absent-equivalent).';
COMMENT ON COLUMN chat_history.inputs IS
    'List of {type: human|tool, content, content_preview<=500, '
    'tool_call_id?, tool_name?} — messages after the last AIMessage, '
    'system messages excluded.';
COMMENT ON COLUMN chat_history.response IS
    '{content, content_preview<=500, has_tool_calls, '
    'tool_calls?:[{id, name, args_preview<=200}]}.';

ALTER TABLE chat_history ALTER COLUMN inputs    SET COMPRESSION lz4;
ALTER TABLE chat_history ALTER COLUMN response  SET COMPRESSION lz4;
ALTER TABLE chat_history ALTER COLUMN reasoning SET COMPRESSION lz4;

-- Serves the chat pane + bulk sync (ORDER BY timestamp, id). No request_id
-- index: no production reader joins chat->llm_requests today.
CREATE INDEX chat_history_job_ts_idx ON chat_history (job_id, timestamp);

-- ============================================================================
-- Partition bootstrap: current month + 2 months lookahead for each table,
-- computed — never hardcoded. NO DEFAULT partition, deliberately: a default
-- converts "partition missing" from a loud SQLSTATE 23514 insert error
-- (which the writer logs and the maintenance loop alarms on) into silent
-- data-parking that later blocks ATTACH and forbids DETACH CONCURRENTLY.
--
-- Storage parameters MUST be applied per leaf partition — partitioned
-- parents reject them ("unrecognized parameter", verified on 15.18/16).
-- orchestrator/services/audit_partitions.py applies the same settings to
-- every partition it creates; keep the two lists in sync.
--   fillfactor=100                       append-only, no HOT-update headroom
--                                        needed (explicit = documentation)
--   autovacuum_vacuum_insert_*           insert-driven vacuums run early and
--                                        incrementally (visibility map /
--                                        index-only scans stay effective)
--   autovacuum_analyze_scale_factor      fresher per-leaf stats on the
--                                        active month
--   autovacuum_freeze_min_age=0          insert vacuums freeze immediately;
--                                        no anti-wraparound storm on cold
--                                        partitions right before they drop
-- ============================================================================
DO $$
DECLARE
    parent TEXT;
    m      DATE;
    part   TEXT;
BEGIN
    FOREACH parent IN ARRAY ARRAY['llm_requests', 'agent_audit', 'chat_history'] LOOP
        FOR i IN 0..2 LOOP
            m    := (date_trunc('month', now()) + make_interval(months => i))::date;
            part := parent || '_p' || to_char(m, 'YYYY_MM');
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                part, parent, m, (m + interval '1 month')::date
            );
            EXECUTE format(
                'ALTER TABLE %I SET ('
                'fillfactor = 100, '
                'autovacuum_vacuum_insert_scale_factor = 0.05, '
                'autovacuum_vacuum_insert_threshold = 10000, '
                'autovacuum_analyze_scale_factor = 0.02, '
                'autovacuum_freeze_min_age = 0)',
                part
            );
        END LOOP;
    END LOOP;
END $$;

-- Partitioned parents are not autovacuum-analyzed; seed parent-level stats
-- now (empty is fine — establishes the entries) and let the maintenance
-- loop's periodic ANALYZE keep them fresh.
ANALYZE llm_requests;
ANALYZE agent_audit;
ANALYZE chat_history;

```

---

## 4. Partition maintenance — `orchestrator/services/audit_partitions.py`

# Partition maintenance module — `orchestrator/services/audit_partitions.py`

Hand-rolled (no `pg_partman` — reverses design-doc open-question #2: the stock
`postgres` images the chart deploys don't ship the extension, and the runner +
lifespan already provide the scheduling/locking primitives; honest budget is
~200 LoC, not the doc's 60). Verified mechanics against the live cluster
(PG 15.18; identical on 16): `CREATE TABLE (LIKE parent INCLUDING ALL)` copies
indexes/compression/defaults, ATTACH auto-attaches them, per-leaf reloptions
land, `pg_get_expr(relpartbound)` yields parseable bounds, missing-partition
inserts fail with SQLSTATE `23514` / `no partition of relation`.

**Hard boundary**: this module owns all partition DDL after `0001_initial.sql`.
`DETACH PARTITION ... CONCURRENTLY` commits internally and **cannot run in a
transaction block**, therefore it can never live in the migration runner
(which wraps files in one transaction) — it must be a runtime task.

## Constants

```python
PARENTS: dict[str, int] = {            # table -> retention days
    "llm_requests": 90,
    "agent_audit":  90,
    "chat_history": 365,
}
LOOKAHEAD_MONTHS = 2                   # current + 2 pre-created (N+2)
GRACE_DAYS = 3                         # detach -> drop buffer (operator inspection
                                       #  + clearance from the pre-16.5 detach/drop race)
MAINT_LOCK_ID = 0x5352575F41554454     # "SRW_AUDT" — advisory-lock key, distinct from
                                       #  migrate.py's 0x5352575F4D4947 ("SRW_MIG") so a
                                       #  slow retention pass can never block startup
                                       #  migrations on the audit instance
PART_SUFFIX_RE = r"_p(\d{4})_(\d{2})$" # partition naming: <parent>_pYYYY_MM (UTC months)
STORAGE_PARAMS = (                     # MUST stay in sync with 0001_initial.sql
    "fillfactor = 100, "
    "autovacuum_vacuum_insert_scale_factor = 0.05, "
    "autovacuum_vacuum_insert_threshold = 10000, "
    "autovacuum_analyze_scale_factor = 0.02, "
    "autovacuum_freeze_min_age = 0"
)
```

All month math in UTC (`SET LOCAL timezone = 'UTC'` inside creation
transactions; Python side uses `datetime.now(timezone.utc)`).

## Function signatures

```python
async def ensure_partitions(pool: asyncpg.Pool, *, lookahead_months: int = LOOKAHEAD_MONTHS) -> list[str]
async def retire_partitions(pool: asyncpg.Pool, *, retention: dict[str, int] = PARENTS,
                            grace_days: int = GRACE_DAYS) -> dict
async def analyze_parents(pool: asyncpg.Pool, *, max_age_days: int = 7, force: bool = False) -> list[str]
async def partition_status(pool: asyncpg.Pool) -> dict   # pure read, no locks — health/metrics source
async def maintenance_pass(pool: asyncpg.Pool) -> dict   # ensure -> retire -> analyze -> status+alarm
async def maintenance_loop(pool: asyncpg.Pool, *, interval_s: int = 6 * 3600,
                           jitter_s: int = 1800) -> None  # lifespan task body
```

### `ensure_partitions` — N+2 lookahead creation, idempotent under concurrency

One transaction per parent:

1. `SELECT pg_advisory_xact_lock(MAINT_LOCK_ID)` as the first statement —
   serializes concurrent creators (future HA orchestrator replicas, overlap
   with a slow previous pass). Transaction-scoped: a crashed pass can't leak
   the lock. This closes both the `CREATE TABLE IF NOT EXISTS` pg_type
   catalog race and duplicate-work DDL.
2. Read truth from catalogs, **never from names**: join `pg_inherits` →
   `pg_class`, parse each child's `pg_get_expr(relpartbound, oid)` for
   `FROM/TO` bounds. Idempotency is defined as *attached with the expected
   bounds* — `IF NOT EXISTS` is banned here (a detached-but-retained table
   with the same name would silently satisfy it and inserts would later 23514).
3. Desired set = months `date_trunc('month', now() UTC)` .. `+lookahead`.
   For each missing month, the low-lock recipe:
   ```sql
   CREATE TABLE <part> (LIKE <parent> INCLUDING ALL);            -- indexes, defaults, lz4 come along
   ALTER TABLE <part> SET (<STORAGE_PARAMS>);
   ALTER TABLE <part> ADD CONSTRAINT <part>_bound
       CHECK ("timestamp" >= '<from>' AND "timestamp" < '<to>'); -- lets ATTACH skip the validation scan
   SET LOCAL lock_timeout = '5s';                                -- ATTACH queues behind long queries;
   ALTER TABLE <parent> ATTACH PARTITION <part>                  --  fail fast, retry next pass
       FOR VALUES FROM ('<from>') TO ('<to>');
   ALTER TABLE <part> DROP CONSTRAINT <part>_bound;              -- redundant once attached
   ```
   CREATE+ATTACH takes only SHARE UPDATE EXCLUSIVE on the parent (vs ACCESS
   EXCLUSIVE for `CREATE ... PARTITION OF`) — writers never stall. The
   bootstrap in `0001_initial.sql` may use plain `PARTITION OF` because it
   owns the brand-new parent in the same transaction; runtime code must not.
4. Crash recovery: if the name already exists *standalone* (prior pass died
   between CREATE and ATTACH): empty → resume at the ADD CONSTRAINT/ATTACH
   step; non-empty → ERROR-log and skip (operator decision), counts toward
   the lookahead alarm.
5. `lock_timeout` expiry → WARN, return; the loop retries within the N+2
   buffer (that buffer exists precisely to absorb failed runs).

### `retire_partitions` — DETACH CONCURRENTLY, grace, DROP

Runs on one dedicated connection **outside any transaction**, guarded by a
session advisory lock (`pg_try_advisory_lock(MAINT_LOCK_ID)`; not acquired →
skip pass; `pg_advisory_unlock` in `finally`; a dropped connection releases it).
Session-scoped because the xact-scoped variant cannot span a command that
commits internally. Strictly serial per table — **at most one partition may be
pending detach per parent**.

Per parent, in order:

1. **FINALIZE first**: `SELECT ... FROM pg_inherits WHERE inhdetachpending` —
   any partition stuck mid-detach (cancelled/crashed first transaction) blocks
   all further detaches; run
   `ALTER TABLE <parent> DETACH PARTITION <part> FINALIZE` before new work.
2. Candidates: attached partitions whose **upper bound** `<= now() - retention`.
   With monthly partitions this makes effective retention `retention ..
   retention + ~31d` (a row ages out only when its whole month does) — accepted
   and documented; steady state ≈ 4 + 4 + 13 attached partitions.
3. For each candidate (oldest first):
   ```sql
   SET statement_timeout = '10min';   -- DETACH CONCURRENTLY waits out ALL transactions
                                      --  using the table; an idle-in-transaction session
                                      --  stalls it indefinitely. On timeout the partition
                                      --  is left pending-detach; step 1 of the next pass
                                      --  finalizes it. That is the documented recovery.
   ALTER TABLE <parent> DETACH PARTITION <part> CONCURRENTLY;
   COMMENT ON TABLE <part> IS 'audit_partitions: detached <utc-iso>';
   ```
   (Two-transaction protocol under the hood: SUE locks, wait for in-flight
   transactions, second transaction completes; legal only because the tables
   have **no DEFAULT partition** and **no FKs** — both load-bearing schema
   decisions.)
4. **DROP after grace**: scan for standalone tables matching
   `<parent>_pYYYY_MM` not present in `pg_inherits`, with a
   `audit_partitions: detached <ts>` comment older than `grace_days` →
   `DROP TABLE` (safe: detached tables are standalone; never DROP an attached
   partition — that takes ACCESS EXCLUSIVE on the parent). Detached tables
   without the comment (operator-made) are left alone and WARN-logged.

### `analyze_parents` — the autovacuum gap

Autovacuum **never ANALYZEs partitioned parents** (it only processes leaves),
and stale parent stats are a documented production failure (row estimates off
by 10^6, Hatchet's post-mortem). Run `ANALYZE <parent>` for the three parents
when: (a) `force=True` — `maintenance_pass` sets it whenever the pass attached
or detached anything, or (b) `pg_stat_user_tables.last_analyze` for the parent
is NULL or older than `max_age_days` (7). On PG 15/16 parent ANALYZE recurses
into every leaf (no `ONLY` until later majors) — fine at our retention-bounded
sizes; the 6 h cadence + 7-day staleness floor keeps it off the hot path.

### `partition_status` + alarms

Returns, per parent:

```python
{"attached": int,
 "newest_upper_bound": datetime,
 "days_until_unpartitioned": float,   # newest_upper - now(); THE control metric
 "detach_pending": int,               # pg_inherits.inhdetachpending
 "awaiting_drop": int,                # detached, inside grace window
 "last_parent_analyze": datetime | None}
```

Alarm policy (log-based, matching the stack's alerting):
- `days_until_unpartitioned < 45` → WARNING `audit_partitions.lookahead_low`;
  `< 31` → ERROR `audit_partitions.lookahead_critical` (with N+2 lookahead and
  a 6 h cadence these only fire after ~45 days of consecutive failures).
- Creator heartbeat: every `maintenance_pass` logs a structured
  `audit_partitions.pass_ok {created: [...], detached: [...], dropped: [...]}`
  INFO line; absence is itself the signal.
- `detach_pending > 0` for two consecutive passes → ERROR (needs FINALIZE).
- Last line of defense: the **writer** logs SQLSTATE `23514` /
  `no partition of relation` prominently (`SyncAuditWriter` must surface that
  SQLSTATE in its warning text so it's greppable) — that string in agent logs
  means the lookahead machinery has been broken for >2 months.
- `partition_status` is also merged into the orchestrator health payload
  (read-only, no locks) so the cockpit/ops can see it without log access.

### `maintenance_loop` — lifespan wiring

In `orchestrator/main.py` lifespan, **after** `run_migrations(audit_pool,
MIGRATIONS_AUDIT_DIR)` and `audit_store.connect()` succeed:

```python
app.state.audit_maint_task = asyncio.create_task(
    audit_partitions.maintenance_loop(audit_store.pool))
```

Loop body: run `maintenance_pass` immediately at startup (a pod that slept
across a month boundary self-heals on boot), then every **6 hours** with
**uniform jitter of 0–30 min** added per cycle (de-synchronizes future HA
replicas; the advisory locks make overlap safe regardless, jitter just avoids
lock-queue noise). Every exception inside a pass is caught, ERROR-logged, and
the loop continues — partition maintenance must never crash the orchestrator.
On shutdown the task is cancelled and awaited alongside the existing lifespan
teardown (before `audit_store.disconnect()`).

If the audit store is unconfigured/disabled (`databases.audit.enabled=false`
→ no DSN), the task is simply not started.

### What this module never does

- No DEFAULT partition, ever (would silently park misrouted rows, then block
  ATTACH validation and forbid DETACH CONCURRENTLY).
- No `DROP TABLE` of an attached partition; no plain (non-CONCURRENT) DETACH.
- No row-level DELETEs for retention (the entire point of partitioning).
- No DDL inside the migration runner's transaction.
- Never trusts `IF NOT EXISTS` / name existence as idempotency.


---

## 5. Adapter interface

# AuditStore adapter — final interface

Two modules, one schema. Method names/signatures are bit-compatible with the
verified write contract (`LLMArchiver`) and read contract
(`orchestrator/database/mongodb.py`) so **no caller changes** beyond the
mechanical `mongodb` → `audit` rename in `main.py`/`graph_routes.py`.

- `orchestrator/database/audit_store.py` — class `AuditStore`: async reader
  (the full cockpit-facing surface) on the orchestrator's audit **read pool**.
- `src/database/audit_writer.py` — class `SyncAuditWriter`: thread-safe,
  blocking write seam for the agent process. `src/core/archiver.py` keeps its
  entire public surface (all signatures unchanged) and swaps its pymongo
  internals for this seam. `_normalize_content` and `inflight_tool_call` stay
  in `archiver.py` (pure functions imported by tests).

## Pool configuration

| | read pool (orchestrator) | write pool (agent process) |
|---|---|---|
| module | `AuditStore` | `SyncAuditWriter` (on its writer thread) |
| DSN | `build_postgres_url("AUDIT_POSTGRES", fallback_env="AUDIT_DB_URL")` | same (agents inherit `AUDIT_POSTGRES_*` via `envFrom`) |
| size | `min_size=1, max_size=5` | `min_size=1, max_size=2` |
| `command_timeout` | 30 s (bulk 5000-row worst case) | 10 s |
| `statement_cache_size` | **0** | **0** |
| `server_settings` | `{"application_name": "srw-audit-read"}` | `{"application_name": "srw-audit-write", "synchronous_commit": "off"}` |
| codec init | jsonb text codec (below) | same |

- `statement_cache_size=0` on **both** pools: the asyncpg-documented
  bulletproof setting for PgBouncer transaction mode (design invariant 2 puts
  PgBouncer in front of `srw-auditdb` beyond ~100 concurrent agents; the cache
  buys ~nothing for single-table INSERTs and job-scoped SELECTs).
- `synchronous_commit=off` **write pool only**, via `server_settings` (exactly
  "per-session on the audit pool, not cluster-wide"). Bounded loss ≤ ~3×
  `wal_writer_delay` (~600 ms) of audit trail on a crash; zero corruption risk
  (categorically unlike `fsync=off`). Reads never commit; the read pool stays
  default. Not set role-wide — the migration runner shares the role and its
  bookkeeping should stay synchronous.
- JSONB codec, registered in each pool's `init` callback (mirrors the
  pgvector pattern in `PostgresDB.connect`):
  ```python
  async def _init(conn):
      await conn.set_type_codec(
          "jsonb",
          encoder=lambda v: json.dumps(v, default=_json_fallback),  # str(UUID/datetime/...) — writes must never raise
          decoder=json.loads,
          schema="pg_catalog",
          format="text",
      )
  ```
  Dicts go in, dicts come out — read methods get payloads ready to splat.
  (If `copy_records_to_table` is ever adopted for bulk paths, the codec must
  flip to binary with the `b'\x01' + payload` jsonb version-byte prefix; not
  needed for v1.)
- Migrations: lifespan runs `run_migrations(read_pool, MIGRATIONS_AUDIT_DIR)`
  (new constant beside `MIGRATIONS_APP_DIR`/`MIGRATIONS_VECTOR_DIR`) before
  `AuditStore` serves reads; then starts `audit_partitions.maintenance_loop`.

## Agent-side writer strategy (the sync/async seam)

**Chosen: one daemon writer thread owning a private asyncio loop + the asyncpg
write pool; sync facade methods submit coroutines with
`asyncio.run_coroutine_threadsafe(coro, writer_loop).result(timeout=15)`.**

Rationale, grounded in the write contract:
- Callers are sync methods invoked **without await** from async graph nodes
  *and* from real ThreadPoolExecutor threads (vision/audio via `run_async`).
  The seam must be a thread-safe **sync** API; `run_coroutine_threadsafe` is
  explicitly thread-safe and works identically from the main loop thread and
  worker threads.
- `archive()`/`audit_step()` must **return the inserted id synchronously**
  (graph threads `request_id` into `update_llm_response`; `audit_ids[call_id]`
  feeds `update_tool_result`) — this rules out fire-and-forget queues without
  switching to client-generated ids, which would upend the BIGINT id design.
- Blocking the caller for one INSERT round-trip is **exact parity** with
  today's sync pymongo behavior (same latency class), so no call-site or
  semantic change.
- No new dependency: asyncpg is already in the agent's `requirements.txt` and
  shares SQL/codec code with the orchestrator adapter. Rejected: psycopg3 sync
  pool (second driver dialect for the same tables); per-call `asyncio.run`
  (connection churn per write); routing writes through the orchestrator API
  (new failure coupling — exactly what the dedicated instance avoids).
- Error contract parity: every facade method wraps everything in
  `try/except Exception → logger.warning → None/False`. The warning text
  includes the SQLSTATE so `23514` (missing partition) is greppable. The
  **one-shot connect gate is kept**: first `_ensure_connected()` failure
  (pool create, 5 s timeout) permanently disables archiving for the process —
  same as `_connection_attempted` today. Post-connect failures are swallowed
  per call. The writer is **never closed** (parity); daemon thread dies with
  the process; no pending writes can exist at exit because every call blocks.
- Gating: `SyncAuditWriter.from_env() -> Optional[SyncAuditWriter]` returns
  `None` unless the `AUDIT_POSTGRES_*` parts (or `AUDIT_DB_URL`) are set —
  replacing the `MONGODB_URL` gate at `archiver.py:209` — so
  `get_archiver() -> None` and all 29 guarded call sites no-op. Graph tests
  that rely on "unconfigured under pytest → no DB" stay green. During cutover
  `AUDIT_BACKEND=postgres|mongodb` (default `mongodb`) selects which backend
  `LLMArchiver` instantiates.

## Write surface

`LLMArchiver` public signatures (unchanged — these ARE the caller contract):
`archive(job_id, agent_type, messages, response, model, latency_ms=None,
iteration=None, metadata=None, phase=None, phase_number=None,
tool_schemas=None, model_kwargs=None, call_type='main',
auxiliary_metadata=None) -> Optional[int]`,
`audit_step(job_id, agent_type, step_type, node_name, iteration, data=None,
latency_ms=None, metadata=None, phase=None, phase_number=None) ->
Optional[int]`, `audit_tool_call(...)`, `audit_llm_call(...)`,
`update_tool_result(audit_doc_id, result, success, latency_ms, error=None) ->
bool`, `update_llm_response(audit_doc_id, request_id, response_preview,
tool_calls, output_chars, latency_ms) -> bool`. Field assembly (message
dicts, ≤500/≤200 previews, metrics, delta extraction) stays in `archiver.py`
verbatim; `_serialize_for_mongo` becomes `_serialize_payload` and now also
converts `datetime` → UTC ISO-8601 string with `Z` (µs precision — matches
today's rendered output) since JSONB has no native datetime. `archive()`
returns the **int** id (was str ObjectId); sole consumer passes it through
opaquely and BIGSERIAL ids are ≥1, so existing truthiness guards
(`if llm_audit_id:`) hold. `_get_next_step_number` and its Mongo max-seed
query are **deleted** — ordering is the global `id` (resume monotonicity free).

`SyncAuditWriter` seam (each: thread-safe, blocking, swallows all exceptions):

### `insert_llm_request(row: dict) -> Optional[int]`
```sql
INSERT INTO llm_requests (job_id, agent_type, call_type, model, iteration,
                          timestamp, latency_ms, request, response, metadata,
                          auxiliary_metadata, metrics)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING id
```
Returns id or None. Degraded (unconfigured/never-connected/error): None.

### `insert_audit_pre(row: dict) -> Optional[int]`
```sql
INSERT INTO agent_audit (job_id, agent_type, iteration, step_type, node_name,
                         phase, phase_number, timestamp, latency_ms, payload,
                         metadata)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING id
```
`event_phase` defaults `'pre'`. Used by `audit_step` (and therefore
`audit_tool_call`/`audit_llm_call`, unchanged wrappers). Returns id or None.

### `insert_audit_post(pre_id: int, payload: dict, latency_ms: int, request_id: Optional[int] = None) -> bool`
```sql
INSERT INTO agent_audit (job_id, agent_type, iteration, step_type, node_name,
                         phase, phase_number, event_phase, pre_id, request_id,
                         latency_ms, payload)
SELECT p.job_id, p.agent_type, p.iteration, p.step_type, p.node_name, p.phase,
       p.phase_number, 'post', p.id, $2, $3, $4
FROM agent_audit p
WHERE p.id = $1 AND p.event_phase = 'pre'
```
INSERT…SELECT derives `job_id` (and descriptive columns) from the pre row —
necessary because `update_tool_result`/`update_llm_response` signatures don't
carry `job_id` — and returns `rowcount == 1`, which restores Mongo's
`modified_count > 0` semantics (False when the pre row is missing) for free.
`pre_id=None` → early-return False without touching the DB (parity with
`ObjectId(None)` raising into the swallow). Callers ignore the bool today;
keep it anyway.
- `update_tool_result` builds `payload = {"tool": {result_preview≤500,
  result_size_bytes, success, error?≤500}, "completed_at": iso}`.
- `update_llm_response` builds `payload = {"llm": {request_id,
  response_content_preview, tool_calls, metrics: {output_chars,
  tool_call_count}}, "completed_at": iso}` and passes `request_id` for the
  hard column too.

### `insert_chat_entry(row: dict) -> None`
```sql
INSERT INTO chat_history (job_id, agent_type, iteration, model, timestamp,
                          latency_ms, phase, phase_number, request_id, inputs,
                          response, reasoning)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
```
Called only from `archive()`'s `call_type=='main'` cascade. No return.

### `close() -> None`
Exists for tests; production never calls it (parity).

Not ported (dead code, zero callers): module-level `archive_llm_request`
convenience keeps delegating to `get_archiver().archive`; the three legacy
`src/database/mongo_db.py` writers and the archiver read methods
(`get_job_stats`, `get_audit_stats`, `get_job_audit_trail`,
`get_conversation`) are deleted with their files. The archiver's
connect-time `create_index` DDL disappears — migrations own DDL.

## Read surface — `AuditStore` (async)

Shared row→doc rules (exact wire parity with Mongo docs):
- `_id: str` becomes `id: int`; `job_id` returned as `str(uuid)`.
- `payload` dict is **splatted over the row dict** (Mongo top-level merge),
  which intentionally reproduces the `phase`-string-shadowed-by-phase-dict
  quirk on `phase_complete` rows.
- Internal columns `event_phase` / `pre_id` and the hard `request_id` on
  agent_audit are **omitted from wire docs** (the llm link reaches the wire
  inside `payload.llm.request_id`, as today, now an int).
- Conditional-field parity: drop None-valued keys exactly where Mongo omitted
  them — llm_requests: latency_ms, iteration, metadata, auxiliary_metadata;
  agent_audit: phase, phase_number, latency_ms, metadata; chat_history: phase,
  phase_number, reasoning — but **keep** chat's `iteration`/`latency_ms` even
  when None (Mongo always wrote them).
- `timestamp` is returned as the tz-aware datetime; existing `_to_iso_utc`
  (ported into this module) / `CustomJSONEncoder` handle serialization.
- Logical-row stitching (resolves contract M5): every agent_audit read serves
  **one row per `event_phase='pre'` row**, with the latest post row merged in:
  ```sql
  LEFT JOIN LATERAL (SELECT * FROM agent_audit post
                     WHERE post.pre_id = f.id AND post.event_phase = 'post'
                     ORDER BY post.id DESC LIMIT 1) p ON TRUE
  ```
  merged payload (two-level — jsonb `||` is shallow; verified live):
  ```sql
  f.payload || COALESCE(p.payload, '{}'::jsonb)
   || CASE WHEN p.payload ? 'tool' THEN jsonb_build_object('tool', (f.payload->'tool') || (p.payload->'tool')) ELSE '{}'::jsonb END
   || CASE WHEN p.payload ? 'llm'  THEN jsonb_build_object('llm',  (f.payload->'llm')  || (p.payload->'llm'))  ELSE '{}'::jsonb END
  ```
  plus `COALESCE(p.latency_ms, f.latency_ms) AS latency_ms`. Totals,
  `step_number`, version counts and `get_audit_count` all count pre rows, so
  every number matches the Mongo store and the cockpit needs **no collapse
  logic**.
- `step_number` synthesis (parity-critical): computed over **all** pre rows of
  the job, *then* filtered — Mongo docs keep their global step_number under
  FilterCategory; renumbering the filtered subset would be wrong:
  ```sql
  WITH numbered AS (
      SELECT a.*, ROW_NUMBER() OVER (ORDER BY a.id) AS step_number
      FROM agent_audit a
      WHERE a.job_id = $1::uuid AND a.event_phase = 'pre')
  SELECT ... FROM numbered f [WHERE f.step_type = ANY($2)]
  <lateral post join> ORDER BY f.id {ASC|DESC} OFFSET $3 LIMIT $4
  ```
- Non-UUID `job_id` strings: caught at cast, mapped to the empty/None result
  the endpoint would produce for an unknown job (Mongo matched nothing; PG
  must not 500).

### Lifecycle
- `connect()` — create pool, ping; failure → WARN, `_available=False`, never
  raises (parity with `MongoDB.connect`).
- `disconnect()` — close pool (lifespan shutdown).
- `is_available: bool` — **startup-latched**, exactly like today (M8):
  flipped only by connect/disconnect; runtime DB loss surfaces as exceptions
  → 500s, unchanged. main.py keeps its per-endpoint `is_available` pre-checks
  and degraded shapes verbatim (200+`error` key for /audit /chat /audit/bulk
  /chat/bulk /graph/bulk; 200 `null` for /timerange /version; **503** for
  /requests/{id}, /llm-requests, /api/graph/changes; `audit_count: None` for
  the three enrichers). The adapter's own degraded returns mirror mongodb.py's
  (empty shapes / None / 0) and remain unreachable behind those pre-checks.

### `get_job_audit(job_id, page=1, page_size=50, filter_category="all", offset=None, limit=None, order="asc") -> dict`
Pagination resolution ported verbatim (offset wins over page; `page==-1` →
last page from `total`; `effective_size = limit or page_size`;
`hasMore=(skip+size)<total`; `page` echoed as `skip//size+1`). `FILTER_MAPPINGS`
/`FilterCategory` move here unchanged. Two queries: count —
`SELECT count(*) FROM agent_audit WHERE job_id=$1::uuid AND event_phase='pre'
[AND step_type=ANY($2)]` — and the numbered+filtered+stitched page query
above. Returns `{entries, total, page, pageSize, offset, limit, hasMore}`.

### `get_request(doc_id: str|int) -> Optional[dict]`
`int(doc_id)` inside try/except → None (the route param **stays `str`**;
non-numeric input keeps today's 404-after-auth instead of FastAPI 422 —
resolves M6 with no wire change). SQL: `SELECT * FROM llm_requests WHERE
id = $1` (PK id-prefix probe across the few attached partitions). Full doc,
None-dropping per table rules. Degraded: endpoint pre-check → **503**.

### `get_audit_time_range(job_id) -> Optional[dict]`
Two index-tip probes for exact "first/last by step order" parity:
`SELECT timestamp FROM agent_audit WHERE job_id=$1::uuid AND
event_phase='pre' ORDER BY id ASC LIMIT 1` (+ DESC). → `{"start": iso,
"end": iso}` via `_to_iso_utc`, or None when empty. Degraded: 200 `null`.

### `get_chat_history(job_id, page=1, page_size=50) -> dict`
Count + `SELECT * FROM chat_history WHERE job_id=$1::uuid ORDER BY timestamp,
id OFFSET $2 LIMIT $3` (id tie-break; incremental sort over
`chat_history_job_ts_idx`). Shape `{entries, total, page, pageSize, hasMore}`
— **no offset/limit keys** (parity).

### `get_job_audit_bulk(job_id, offset=0, limit=5000) -> dict`
Re-clamp `limit=min(limit,5000)`; numbered CTE (no filter) + stitch, ASC only;
top-level `timestamp` pre-converted via `_to_iso_utc` (parity), nested values
already ISO strings from the writer. `{entries, total, offset, limit, hasMore}`.

### `get_chat_history_bulk(job_id, offset=0, limit=5000) -> dict`
Same as `get_chat_history`: `ORDER BY timestamp, id`, bulk shape/conversions as the audit bulk method.

### `get_graph_deltas_bulk(job_id, offset=0, limit=5000) -> dict`
Numbered CTE filtered `step_type='tool' AND payload->'tool'->>'name' =
ANY($2)` with the 3 cypher tool names (constant `GRAPH_TOOL_NAMES`); count
query with the same predicate. Pre rows only — arguments live there
(double-count guard is structural). Delta per row:
`{toolCallIndex: offset+i, timestamp: _to_iso_utc|None, cypherQuery:
payload->tool->arguments->query or "", toolCallId: str(id), stepNumber:
step_number}`. `{deltas, total, offset, limit, hasMore}`.

### `get_job_version(job_id) -> Optional[dict]`
Single statement (race-free, replaces 4 sequential queries):
```sql
SELECT count(*) FILTER (WHERE event_phase='pre')                          AS audit_count,
       count(*) FILTER (WHERE event_phase='pre' AND step_type='tool'
                        AND payload->'tool'->>'name' = ANY($2))           AS graph_count,
       count(*)                                                           AS row_count,
       (SELECT timestamp FROM agent_audit WHERE job_id=$1::uuid
         AND event_phase='pre' ORDER BY id DESC LIMIT 1)                  AS last_ts,
       (SELECT count(*) FROM chat_history WHERE job_id=$1::uuid)          AS chat_count
FROM agent_audit WHERE job_id = $1::uuid
```
`audit_count==0` → None. Returns `{version: hash((audit_count, chat_count,
graph_count, row_count)), auditEntryCount: audit_count, chatEntryCount,
graphDeltaCount, lastUpdate: _to_iso_utc(last_ts)}`. Exposed counts are
**logical** (consistent with `/audit` `total` and bulk sync); `row_count` as a
4th hash component makes post-row arrivals invalidate the cockpit cache —
fixing the silent staleness Mongo's in-place $set had, with zero wire-shape
change (version is equality-compared only).

### `get_audit_count(job_id) -> int`
`SELECT count(*) FROM agent_audit WHERE job_id=$1::uuid AND
event_phase='pre'`. 0 when unavailable.

### `get_audit_counts(job_ids: Sequence[str]) -> dict[str, int]`  *(new)*
`SELECT job_id, count(*) FROM agent_audit WHERE job_id = ANY($1::uuid[]) AND
event_phase='pre' GROUP BY job_id` — collapses the three N+1 enricher loops
(`/api/jobs`, `/api/jobs/{id}`, `/api/projects/{id}/jobs`) into one query;
missing ids → 0. Single-row callers may keep `get_audit_count`.

### `iter_tool_calls(job_id) -> AsyncIterator[dict]`  *(new — replaces `mongodb._db["agent_audit"]`)*
For `graph_routes.py`: keyset-chunked (id cursor, 1000/batch — today's call is
unbounded; never pin a server cursor):
```sql
SELECT * FROM (
  SELECT a.*, ROW_NUMBER() OVER (ORDER BY a.id) AS step_number
  FROM agent_audit a WHERE a.job_id=$1::uuid AND a.event_phase='pre') t
WHERE t.step_type='tool' AND t.id > $2 ORDER BY t.id LIMIT 1000
```
Stitched + splatted docs with `id`/`step_number`/`timestamp` — the cypher
regex parser consumes them unchanged. `set_mongodb()` becomes
`set_audit_store()`; the route additionally gains `require_job_access`
(closing M7 — today it is the only unauthenticated audit read). Degraded:
route keeps its 503 pre-check.

### `list_llm_requests(job_id, limit=20, offset=0) -> dict`
Endpoint keeps its `UUID(job_id)`→400 check. Re-clamp `min(limit,100)`.
```sql
SELECT id, job_id, timestamp, model, iteration, response,
       metrics->'token_usage' AS token_usage
FROM llm_requests WHERE job_id=$1::uuid ORDER BY timestamp, id
OFFSET $2 LIMIT $3
```
Python reduces `response` → `tool_calls: [{"name": ...}]` as today and now
emits `token_usage` from its real (nested) home — fixing M4: the Mongo
projection asked for a top-level field the writer never wrote, so the
docstring-promised key was silently absent. Additive key; cockpit-safe.
`{entries, total, offset, limit, hasMore}`; `timestamp` via `_to_iso_utc`.
Degraded: **503**.

### Explicitly dropped (M2 — dead, not silently ported)
`get_page_for_timestamp`, `get_job_ids_with_audit`, `get_chat_history_count`,
`get_job_audit_trail`, `get_llm_conversation`, `get_statistics`, the `db`
property, `ensure_indexes()` + `MONGODB_INDEX_DECLARATIONS` (DDL belongs to
migrations), and the archiver aggregations `get_job_stats`/`get_audit_stats`
(M1 — zero callers; the SQL translations live in the design doc's history if
a consumer ever materializes).


---

## 6. Verified integration inventory (condensed)

Full evidence with per-claim anchors: `postgres_audit_store_contracts.md`.

### Write side (agent process)

- WRITE ENTRY POINT: get_archiver() -> Optional[LLMArchiver] (src/core/archiver.py:1117-1126), process singleton, never closed; None when MONGODB_URL unset (from_env, :202-224); all 29 call sites guard with `if auditor:`
- SIGNATURE archive(self, job_id: str, agent_type: str, messages: Sequence[BaseMessage], response: AIMessage, model: str, latency_ms: Optional[int]=None, iteration: Optional[int]=None, metadata: Optional[Dict]=None, phase: Optional[str]=None, phase_number: Optional[int]=None, tool_schemas: Optional[List[Dict]]=None, model_kwargs: Optional[Dict]=None, call_type: str='main', auxiliary_metadata: Optional[Dict]=None) -> Optional[str] — llm_requests insert_one; returns str(ObjectId) or None; cascades _archive_chat_entry when call_type=='main'; phase/phase_number NOT written to llm_requests (archiver.py:309-441)
- SIGNATURE _archive_chat_entry(self, job_id, agent_type, messages, response, model, latency_ms, iteration, request_id, phase, phase_number) -> None — chat_history insert_one, private, sole caller archive() (archiver.py:546-668)
- SIGNATURE audit_step(self, job_id: str, agent_type: str, step_type: str, node_name: str, iteration: int, data: Optional[Dict]=None, latency_ms: Optional[int]=None, metadata: Optional[Dict]=None, phase: Optional[str]=None, phase_number: Optional[int]=None) -> Optional[str] — agent_audit insert_one; data dict merged at document TOP LEVEL via doc.update() (archiver.py:703-775)
- SIGNATURE audit_tool_call(self, job_id: str, agent_type: str, iteration: int, tool_name: str, call_id: str, arguments: Dict, metadata=None, phase=None, phase_number=None) -> Optional[str] — PRE insert via audit_step(step_type='tool', node_name='tools'); args truncated per-key to 200 chars; tool.result_* fields None (archiver.py:777-840)
- SIGNATURE update_tool_result(self, audit_doc_id: str, result: str, success: bool, latency_ms: int, error: Optional[str]=None) -> bool — agent_audit update_one filter {_id: ObjectId(audit_doc_id)}, $set {tool.result_preview(<=500), tool.result_size_bytes, tool.success, completed_at, latency_ms, tool.error?(<=500)}; returns modified_count>0, caller ignores (archiver.py:842-893; caller graph.py:3429)
- SIGNATURE audit_llm_call(self, job_id: str, agent_type: str, iteration: int, model: str, input_message_count: int, state_message_count: int, metadata=None, phase=None, phase_number=None) -> Optional[str] — PRE insert via audit_step(step_type='llm', node_name='execute'); llm.{request_id,response_content_preview,tool_calls,metrics}=None, state.message_count, started_at, completed_at=None (archiver.py:895-949)
- SIGNATURE update_llm_response(self, audit_doc_id: str, request_id: Optional[str], response_preview: str, tool_calls: List[Dict], output_chars: int, latency_ms: int) -> bool — agent_audit update_one $set {llm.request_id, llm.response_content_preview, llm.tool_calls, llm.metrics{output_chars,tool_call_count}, completed_at, latency_ms}; caller ignores bool (archiver.py:951-1006; caller graph.py:1492)
- SIGNATURE module fn archive_llm_request(job_id, agent_type, messages, response, model, latency_ms=None, iteration=None, metadata=None, phase=None, phase_number=None, tool_schemas=None, model_kwargs=None, call_type='main', auxiliary_metadata=None) -> Optional[str] (archiver.py:1129-1167) — ZERO production callers
- SIGNATURE legacy dead writers in src/database/mongo_db.py (NO callers): MongoDB.archive_llm_request(job_id, agent_type, messages: List[Dict], response: Dict, model, **metadata) :126; MongoDB.audit_tool_call(job_id, agent_type, tool_name, inputs, output=None, error=None, **metadata) :168 (event_type='tool_call'); MongoDB.audit_phase_transition(job_id, agent_type, from_phase, to_phase, **metadata) :214 (event_type='phase_transition')
- COLLECTION llm_requests fields: job_id(str), agent_type(str), timestamp(tz-aware utc datetime), model(str), call_type(str: main|summarization|memory_extraction|memory_assembly|knowledge_curation|auxiliary|vision|transcription), request{messages:[{type,content,role,tool_calls?,tool_call_id?,name?,additional_kwargs?,response_metadata?}], message_count, tools?, tool_count?, model_kwargs?}, response(same message-dict shape), latency_ms?(int), iteration?(int), metadata?(dict, UUIDs stringified), auxiliary_metadata?(dict), metrics{input_chars:int, output_chars:int, tool_calls:int, token_usage:dict}
- COLLECTION agent_audit fields: job_id(str), agent_type(str), iteration(int), step_number(int, per-job counter), step_type(str, 13 values: initialize|llm|tool|check|warning|error|phase_transition|phase_complete|feedback_resume|memory_inject|memory_dedup|memory_store|memory_retrieve), node_name(str), timestamp(utc), phase?(str), phase_number?(int), latency_ms?(int), metadata?(dict), + data merged TOP-LEVEL: tool{name,call_id,arguments,result_preview,result_size_bytes,success,error}|llm{model,input_message_count,request_id,response_content_preview,tool_calls,metrics}|state{message_count}|check{...}|transition{...}|phase{completed,archive_path}|workspace{...}|error{...}|warning{...}|count|total_tokens|started_at|completed_at|feedback_length|existing_id|id|type|source|importance|tokens|similarity
- COLLECTION chat_history fields: job_id(str), agent_type(str), timestamp(utc), iteration(int|None always written), model(str), latency_ms(int|None always written), inputs:[{type:'human'|'tool', content, content_preview<=500, tool_call_id?, tool_name?}], response{content, content_preview<=500, has_tool_calls:bool, tool_calls?:[{id,name,args_preview<=200}]}, request_id(str, llm_requests ObjectId), phase?(str), phase_number?(int), reasoning?{content, content_preview<=500}
- TWO-PHASE: Chain A (graph.py execute node :731): audit_llm_call :1119 -> await LLM :1145 -> archive :1450 (request_id) -> update_llm_response :1492; Chain B (audited_tools :3072): audit_tool_call per call :3246 (audit_ids[call_id]=doc_id) -> await tool_node.ainvoke :3262 -> update_tool_result per ToolMessage :3429 (latency = batch_ms // n, success = not _is_tool_error string heuristic graph.py:372-384). IDs are stringified ObjectIds. All sync calls, never awaited; awaits sit between pre and post
- ERROR HANDLING: every write try/except Exception -> logger.warning -> return None/False/{}/[]; _connection_attempted one-shot gate (archiver.py:235-236) = a single failed first connect silently disables archiving for process lifetime; serverSelectionTimeoutMS=5000 (mongo_db.py:92); callers never see exceptions; only return-value use is skipping the post-update when pre-insert returned None
- STEP_NUMBER: _get_next_step_number (archiver.py:271-301) in-process dict, first access seeds from Mongo max via find_one(sort step_number desc) so resumed jobs continue numbering; only audit_step consumes it; read side sorts agent_audit by step_number (orchestrator/database/mongodb.py:355,451,458,639,758,826, wire field stepNumber :774); single event-loop thread => no real race today; update phases reuse the pre doc's step_number
- AGGREGATIONS (all DEAD CODE, zero callers): get_job_stats pipeline1 :493-510 ($match job_id, $group by job_id: total_requests/total_input_chars/total_output_chars/total_tool_calls/avg_latency_ms/first_request/last_request/models_used $addToSet); pipeline2 :518-530 ($group by call_type: count/input_chars/output_chars); get_audit_stats pipeline3 :1054-1066 ($group by step_type: count/avg_latency_ms/total_latency_ms); pipeline4 :1082-1093 ($group _id None: first_step/last_step/max_iteration)
- CONCURRENCY: archiver writes block the event loop (sync pymongo in async nodes); background asyncio.create_task writers: extract_and_store_memories (graph.py:1553,2046), assemble_memories (:1587), curate_and_store_knowledge (:2150); REAL THREADS: vision/audio archive() runs in ThreadPoolExecutor via run_async (vision_helper.py:26-46, sync tool wrappers files.py:245,303) — new adapter must be thread-safe sync; no locks anywhere
- CALL SITES (29 writes): graph.py x23 (audit_step x18 at :530,:598,:922,:1189,:1255,:1354,:1416,:1687,:1753,:1812,:1892,:2102,:2383,:2505,:2548,:2581,:2609,:2895 + audit_llm_call :1119 + archive :1450 + update_llm_response :1492 + audit_tool_call :3246 + update_tool_result :3429); recall_store.py x3 (:370 memory_dedup, :448 memory_store, :725 memory_retrieve, iteration=0, no phase); auxiliary.py :697 (call_type from _TASK_CALL_TYPES :351-356); vision_helper.py :313; audio_helper.py :627. Wiring: src/agent.py:477-481 (aux set_job_context), :1777-1788 (RecallStore ctor). persistent_graph.py: ZERO archiver usage
- WRITE-SIDE DDL: archiver creates llm_requests index (job_id, call_type, timestamp) background=True at every first connect (archiver.py:256-262) — NOT in MONGODB_INDEX_DECLARATIONS (orchestrator/database/mongodb.py:80-119); deleted with the file at cutover
- TESTS affected: test_audio_helper.py TestArchiving (:385+, patches get_archiver, asserts archive kwargs); test_vision_helper.py (:54 stubs _archive_vision_call); test_responses_api.py (imports _normalize_content from archiver); test_persistent_app.py (imports inflight_tool_call); test_database_phase1.py TestMongoDB (:97+, dies with mongo_db.py); test_audit_pagination.py + test_job_access.py (orchestrator read side / is_available patch); test_graph_image_postprocessing.py + test_stuck_detection.py rely on get_archiver()->None under pytest (MONGODB_URL unset). No test_archiver.py, no mongomock — confirmed
- SUSPICIOUS SWEEP: only customer-datasource Mongo outside scope files — src/core/datasource_setup.py:586-588 (pymongo MongoClient for attached datasources), cleanup-registry comments (src/agent.py:177,2358; src/api/persistent_session.py:143), tool catalog persistent_app.py:1016-1024; src/database/__init__.py:41 re-exports MongoDB; orchestrator/ has zero insert_one/update_one

Write-side risks:

- Design doc calls the four archiver aggregation pipelines 'the four pipelines we actually use' — they have ZERO production callers (dead code); only port them if the unified adapter wants read parity, otherwise scope creep
- agent_audit data merge happens at document top level and at step_type='phase_complete' the data {'phase': {...}} dict OVERWRITES the phase string field (graph.py:2102 vs archiver.py:749/762) — the new phase TEXT + payload JSONB split changes observable shape for that step_type; read adapters/cockpit must not assume phase='strategic|tactical' exists on those rows
- Vision/audio archive writes execute on ThreadPoolExecutor threads (run_async spawning asyncio.run in a pool, vision_helper.py:37-44) — a loop-bound asyncpg shim under src/database/ would break these paths; the sync write seam must be thread-safe or marshal to a dedicated writer loop/queue
- The one-shot _connection_attempted gate means a transient audit-DB outage at agent start silently disables ALL auditing for the job with one warning line; replicating this exactly in the Postgres adapter perpetuates a silent-failure mode (links to the 'surface silent aux failures' issue), while 'fixing' it changes latency behavior when the DB is down mid-job (per-write timeout stalls block the event loop)
- Doc's llm_requests DDL adds agent_id UUID with no current source field — writer emits only agent_type, which actually carries config.agent_id values (naming crossover); decide derivation before the adapter freezes the column
- update_tool_result latency_ms is batch wall-clock divided evenly per tool call (graph.py:3433-3434) and success is a content-string heuristic — any new per-tool latency/success analytics on the Postgres schema would be built on approximate data
- step_number resume-seeding queries the store for max(step_number) on first per-job use; the PG design drops the column in favor of read-time ROW_NUMBER — the orchestrator read API exposes stepNumber on the wire (mongodb.py:774), so the read adapter must synthesize it or the cockpit field shape breaks
- graph tests (test_graph_image_postprocessing.py, test_stuck_detection.py) pass today only because get_archiver() returns None when MONGODB_URL is unset under pytest — the Postgres adapter must preserve an equivalent unconfigured->no-op path or these unrelated suites start requiring a database

### Read side (orchestrator)

- GET /api/jobs/{id}/audit (main.py:8390-8441 -> mongodb.get_job_audit:285-377): params page>=-1 def 1, pageSize alias 1..200 def 50, offset>=0 opt, limit 1..200 opt (offset/limit override page/pageSize), order asc|desc def asc, filter all|messages|tools|errors; Mongo find({job_id,[step_type $in]}).sort(step_number,+-1).skip.limit + count_documents; page=-1 = last page; resp {entries(full docs,_id str,datetimes raw->CustomJSONEncoder), total, page=skip//size+1, pageSize, offset, limit, hasMore=(skip+size)<total}; degraded 200+error key
- GET /api/requests/{doc_id} (main.py:8444-8476 -> get_request mongodb.py:407-432): ObjectId(doc_id) except InvalidId->None->404 (invalid==missing, no 400); find_one({_id}) on llm_requests, no projection; auth AFTER fetch (require_job_access on embedded job_id, admin if absent, require_approved_user before 404); degraded 503; doc keys job_id/agent_type/timestamp/model/call_type/request{messages,message_count,tools?,model_kwargs?}/response/metrics{input_chars,output_chars,tool_calls,token_usage}/latency_ms?/iteration?/metadata?/auxiliary_metadata?
- GET /api/jobs/{id}/audit/timerange (main.py:8479-8493 -> get_audit_time_range:434-469): two find_one sorted step_number asc/desc projection {timestamp:1}; resp {start,end} via _to_iso_utc; null if no rows; degraded 200 null; SQL = MIN/MAX(timestamp)
- GET /api/jobs/{id}/chat (main.py:8496-8531 -> get_chat_history:523-579): page>=-1/pageSize<=200 only (no offset style); find({job_id}).sort(timestamp,1).skip.limit; resp {entries,total,page,pageSize,hasMore} NO offset/limit keys; degraded 200+error; chat doc = inputs[{type,content,content_preview,tool_call_id?,tool_name?}], response{content,content_preview,has_tool_calls,tool_calls?[{id,name,args_preview}]}, request_id, phase?, phase_number?, reasoning?
- GET /api/jobs/{id}/audit/bulk + /chat/bulk (main.py:9311-9382 -> mongodb.py:599-710): offset>=0 def 0, limit 1..5000 def 5000 (adapter re-clamps min(limit,5000)); sort step_number asc (audit) / timestamp asc (chat); _id str + top-level timestamp _to_iso_utc; resp {entries,total,offset,limit,hasMore}; degraded 200+error
- GET /api/jobs/{id}/graph/bulk (main.py:9385-9419 -> get_graph_deltas_bulk:712-785): query {job_id, step_type:'tool', tool.name $in [cypher_query,cypher_execute,execute_cypher_query]}; sort step_number asc; delta={toolCallIndex:offset+i, timestamp iso|null, cypherQuery:tool.arguments.query or '', toolCallId:str(_id), stepNumber}; resp {deltas,total,offset,limit,hasMore}; degraded 200+error
- GET /api/jobs/{id}/version (main.py:9422-9440 -> get_job_version:787-843): auditEntryCount=count({job_id}) (0 -> 200 null), chatEntryCount=chat count, graphDeltaCount=count(tool+cypher-names filter), lastUpdate=_to_iso_utc(find_one sort step_number desc .timestamp), version=hash((a,c,g)) deterministic int-tuple hash, lastUpdate NOT hashed; 4 sequential queries (racy); degraded 200 null
- GET /api/jobs/{id}/llm-requests (main.py:14623-14649 -> list_llm_requests:849-932): limit 1..100 def 20, offset>=0 def 0; UUID(job_id) check -> 400 (unique); projection {_id,job_id,timestamp,model,token_usage,iteration,response}, sort timestamp asc; response popped -> tool_calls=[{name}]; BUG: token_usage projected top-level but writer nests metrics.token_usage (archiver.py:393-403) -> field always absent; degraded 503
- GET /api/graph/changes/{job_id} (graph_routes.py:39-130): NO AUTH (router bare at main.py:3796); 503 if unavailable; raw mongodb._db['agent_audit'] at :142, find({job_id,step_type:'tool'}).sort(step_number,1) UNBOUNDED; timestamps bare .isoformat() (no Z); Python-filter 3 cypher tool names; regex-parse query -> changes{nodesCreated/Deleted/Modified,relationshipsCreated/Deleted,matchedVariables}; snapshots every clamp(sqrt(n),50,100); resp {jobId,timeRange|null,summary,snapshots,deltas}
- Enrichers GET /api/jobs (main.py:3933-3939), GET /api/jobs/{id} (3953-3956), GET /api/projects/{id}/jobs (18814-18819, doc says 18740): N+1 await mongodb.get_audit_count(job_id) (count_documents) per row; unavailable -> audit_count=None; SQL collapse = GROUP BY job_id over ANY(uuid[])
- FILTER_MAPPINGS (mongodb.py:55-60): all->[] (no clause), messages->['llm'], tools->['tool'], errors->['error']; applied as step_type $in; FilterCategory Literal at :62; only get_job_audit + dead get_page_for_timestamp use it
- MONGODB_INDEX_DECLARATIONS (mongodb.py:80-119): 14 indexes — llm_requests 5 (job_id, agent_type, timestamp, model, (job_id,agent_type,timestamp DESC)), agent_audit 7 (job_id, step_type, node_name, timestamp, (job_id,step_number), (job_id,iteration,step_number), (job_id,agent_type,step_type)), chat_history 2 (job_id, (job_id,timestamp)); ensure_indexes (:228-274) idempotent create_index loop, ERROR-level failures, returns asserted count, called lifespan main.py:3332 + orchestrator/init.py:1590
- Aggregations are NOT in orchestrator mongodb.py: get_job_stats (archiver.py:479-544, llm_requests, $group totals+$addToSet models + by-call_type breakdown) and get_audit_stats (archiver.py:1041-1103, agent_audit, by-step_type counts/latency + first/last/max_iteration) — sync pymongo, ZERO callers repo-wide; MCP get_job_stats is unrelated (/api/stats/jobs, Postgres)
- Dead mongodb.py members (no callers, absent from design/roadmap): get_page_for_timestamp:471-517, get_job_ids_with_audit:394-405, get_chat_history_count:581-593, get_job_audit_trail:938-966, get_llm_conversation:968-996, get_statistics:998-1016, db property:1018-1025 (graph_routes uses private _db, not the property)
- Degraded matrix: 200+error-key = /audit /chat /audit/bulk /chat/bulk /graph/bulk; 200 null = /timerange /version; 503 = /requests/{id} /llm-requests /api/graph/changes; audit_count=None = 3 enrichers; error key added in main.py not adapter; _available latched at connect() (mongodb.py:207-217) -> runtime Mongo loss = 500s, not these shapes
- Ordering: agent_audit reads all sort step_number (racy in-process counter archiver.py:271); chat_history + llm_requests sort timestamp; Postgres translation = ORDER BY id + ROW_NUMBER() synthesized step_number (wire field must survive)
- Append-only contract ambiguity (must resolve pre-build): design says JOIN-merge OR DISTINCT ON latest (not equivalent — post row lacks tool.arguments), roadmap P6 expects doubled rows on wire + cockpit collapse; affects total/auditEntryCount/step_number/get_audit_count parity; graph-delta + iter_tool_calls queries must pin event_phase='pre' (arguments live there)
- Serialization: _id always str(ObjectId); _to_iso_utc (mongodb.py:21-36) and CustomJSONEncoder (main.py:3262-3281) identical logic — naive dt -> isoformat()+Z (microseconds), tz-aware -> millisecond truncation; asyncpg returns tz-aware -> cutover changes wire precision us->ms; /api/graph/changes timestamps currently lack Z suffix

Read-side risks:

- Append-only pre/post read semantics are unresolved across the docs (JOIN-merge vs DISTINCT-ON-latest vs wire-doubled+cockpit-collapse); DISTINCT ON latest alone is wrong because post rows lack tool.arguments — every count/total/step_number/version contract depends on this decision being made before P3
- get_job_version, get_audit_count, and /audit total must count logical steps (pre rows or DISTINCT request_id) consistently with each other post-migration, or cockpit cache invalidation and pagination totals drift apart
- /api/graph/changes/{job_id} currently has no auth gate and an unbounded full-collection scan per request — porting it verbatim reproduces both problems; add require_job_access + bounded iteration in iter_tool_calls
- list_llm_requests' top-level token_usage projection never matches the writer's metrics.token_usage — porting the projection verbatim preserves a silent dead field; fixing it changes the wire shape; either way it must be an explicit decision
- doc_id: str -> int changes invalid-input behavior from authed 404 to unauthenticated 422 on /api/requests/{id}; direct API consumers (MCP server.py:354, client.py:263/:700, builder_tools.py:1072) see a status-code and schema change
- is_available semantics: Mongo's flag is startup-latched so 'graceful degradation' never actually engages at runtime (500s instead); the AuditStore must define whether is_available reflects live pool health, or the degraded shapes remain mostly-dead code
- Design doc line refs for /llm-requests (14563/14572) and the project-jobs enricher (18740-18742) have drifted to 14623-14649 / 18814-18819 — re-grep all anchors at implementation time as both docs themselves advise
- Timestamp precision on the wire flips from microseconds to milliseconds once values come back tz-aware from asyncpg (both serializers truncate aware datetimes); harmless for JS Date parsing but will trip any byte-equivalence diff harness in P6

### Cockpit / API consumers

- audit.model.ts:65 AuditEntry._id: string -> number (or rename to id)
- audit.model.ts:43 AuditLLMInfo.request_id?: string|null — stringified llm_requests ObjectId (writer archiver.py:432/:980); becomes BIGINT; MISSING from design doc's cockpit list
- chat.model.ts:50 ChatEntry._id: string; chat.model.ts:63 ChatEntry.request_id?: string (writer archiver.py:654)
- request.model.ts:72 LLMRequest._id: string
- cache.model.ts:29-34 CachedChatEntry.id: string = Mongo _id; is the Dexie chatEntries PRIMARY KEY (indexed-db.service.ts:43, populated :250)
- graph.model.ts:73 GraphDelta.toolCallId: string = str(audit _id) (mongodb.py:773, graph_routes.py:100); NO cockpit reader — adapter can emit str(int), zero UI change
- request.service.ts:60 hard regex /^[a-fA-F0-9]{24}$/ + :61 error text; :53 loadRequest(docId: string); :14 currentDocId signal<string|null>
- request-viewer.component.ts:22 placeholder '24 hex chars'; :611 docIdInput; :67 renders ?._id
- agent-activity.component.ts:671 Set<string> expandedIds; :73 track entry._id; :77/:82/:95/:98 isExpanded/toggleExpanded(entry._id); :799/:812 entryId: string params
- agent-activity.component.ts:156 getRequestId(entry)!.slice(0,12) — RUNTIME TypeError if request_id becomes number; :943 untyped ['request_id'] access bypasses TS; :947 feeds the 24-hex regex (silent lookup failure)
- chat-history.component.ts:690 Map<string,string> selectedTabs keyed entry._id; :94 track entry._id; :143/:151 getSelectedTab/selectTab; :868-877 string params
- chat-history.component.ts:191 entry.request_id.slice(0,8) — RUNTIME TypeError with integer; :764 onRequestIdClick(requestId: string)
- api.service.ts:256 getRequest(docId: string); JobVersionInfo at :97-103 (version field = Python 64-bit hash mongodb.py:835, can exceed MAX_SAFE_INTEGER, unused by cockpit — drop it)
- indexed-db.service.ts:250 id: entry._id (chat cache PK); audit/graph cache keys are composite ${jobId}_${index} (:157/:318) — NOT _id, unaffected by type flip
- IndexedDB sync: DataService.fetchAndCacheJob loops /audit/bulk (:528) /chat/bulk (:548) /graph/bulk (:562); validity = ONLY metadata.auditEntryCount === versionInfo.auditEntryCount (data.service.ts:296-297)
- CACHE INVALIDATION MISMATCH: JobCacheMetadata.version (cache.model.ts:67) written at indexed-db.service.ts:236/:304/:377 (CACHE_VERSION=4 at :17) but NEVER READ — design doc's 'bump version to discard stale entries' is a no-op; need a version!==CACHE_VERSION check in loadJob or Dexie version(5).upgrade clear
- Cache-miss path data.service.ts:308-311 does NOT clearJob before re-fetch -> old string-keyed chat rows survive bulkPut and duplicate in [jobId+timestamp] reads; only refresh() :402 and autoRefreshTick :501-507 clear first
- main.py:8444-8445 /api/requests/{doc_id} param doc_id: str -> int (invalid-id becomes 422 instead of 404)
- mcp/server.py:354, mcp/client.py:263 + :700, builder_tools.py:1072 — the only 4 'MongoDB ObjectId (24 hex)' description sites in orchestrator/ (grep-verified exhaustive)
- formatters.py:491 entry.get('_id','?') and :574 request.get('_id','unknown') — consume the _id FIELD NAME; silent display break under rename-to-id; doc wrongly says formatters are clean; main.py:14633 docstring also says _id
- formatters.py:85-92 + builder_dispatch.py:354-357 read llm.request_id into f-strings — int-safe, name-stable
- N+1 audit_count enrichers: main.py:3936, :3954, :18816-18819 (doc said 18740 — local drift); audit_count stays number, no cockpit break (audit.model.ts:112)
- graph_routes.py:100 toolCallId=entry['_id']; :142 raw _db['agent_audit'] cursor; :157-161 isoformat() NO Z suffix
- Timestamps today = 3 formats: bulk/version/timerange/llm-requests via _to_iso_utc (mongodb.py:21-36; live naive branch :30 = 6-digit micros+Z); paginated /audit (:361-364) /chat (:568-571) /requests (:431) leave raw datetimes -> FastAPI naive ISO NO suffix (JS parses as LOCAL time — latent bug, cockpit unexposed); graph_routes no-Z
- PG timestamptz via asyncpg = aware -> '+00:00' 6-digit micros; all cockpit new Date() sites parse fine (data.service.ts:208/:211/:368/:375, chat-history:755, agent-activity:867)
- Lexicographic timestamp dependence: indexed-db.service.ts:512-520 string min/max + Dexie [jobId+timestamp] chat index (:43, read :267-272) — adapter must emit ONE canonical format; recommend UTC Z + 3-digit millis (today's mongodb.py:34 shape)
- visibleChatEntries join (data.service.ts:200-212) compares audit vs chat timestamps numerically — audit and chat endpoints must serialize identically or the pane filters on a TZ-shifted clock
- mongodb.py:471-523 get_page_for_timestamp: dead code, no main.py caller — drop from adapter surface
- Cockpit-dead endpoints: paginated /audit, /chat, /audit/timerange (api.service getJobAudit :225 / getChatHistory :302 / getAuditTimeRange :283 have zero callers); /llm-requests has NO cockpit consumer — MCP/builder text-only
- MCP client read surface: client.py:132/:511 /audit, :145/:549 timerange, :173/:578 /chat, :533/:600 bulk, :251/:687 /graph/changes, :268/:705 /requests, :1465 /llm-requests — all rendered via formatters f-strings (type-tolerant)
- Test fixtures with string _id: data.service.spec.ts:25/:40; indexed-db.service.spec.ts:67/:93/:267/:275/:285/:290
- Wire recommendation: rename _id->id + integer (Option B) — TS compiler enumerates all cockpit sites as hard errors; only silent breaks are formatters.py:491/:574 + main.py:14633 docstring; keep request_id NAME but flip to int and drop .slice() displays; emit toolCallId as str(id)

Cockpit risks:

- The design doc's cache-invalidation step (bump cache.model.ts version) does not work — JobCacheMetadata.version is never read. If the cutover relies on it without adding a real check or Dexie upgrade-clear, stale string-shaped cache entries persist; the cluster wipe masks this only because job IDs become disjoint.
- Two runtime-fatal template calls (.slice() on request_id at chat-history.component.ts:191 and agent-activity.component.ts:156) and one untyped access (agent-activity:943) will not all surface as compile errors — the untyped path silently feeds an integer into the 24-hex regex, breaking the audit->request-viewer drill-down without any error.
- request_id (audit.model.ts:43, chat.model.ts:63) is absent from the design doc's cockpit change list — if the new adapter emits integer request_id while only the documented _id sites are fixed, the request-viewer linkage breaks despite the doc's checklist being 'complete'.
- Renaming _id to id silently degrades formatters.py:491/:574 output (shows '?'/'unknown') — Python has no compiler to catch it; these two sites plus the main.py:14633 docstring must be in the P4 checklist.
- Timestamp serialization must be identical across audit/chat/graph endpoints in the new adapter — the cockpit's chat-pane join compares timestamps across endpoints; a mixed aware/naive emission shifts the join by the local TZ offset (today's paginated endpoints already have the naive-no-suffix bug, currently unexposed).
- orchestrator/main.py is locally modified (git status M) — all main.py line anchors here and in the design doc will drift; re-grep at implementation time.
- /api/requests/{id} param str->int changes malformed-id responses from 404 to FastAPI 422 — MCP/builder agent flows that probe ids get a new error shape.

### Infrastructure

- build_postgres_url signature (identical in orchestrator/utils/db_url.py:21-54 and src/utils/db_url.py:18-51): build_postgres_url(prefix='POSTGRES', *, fallback_env=None, default_host=None, default_port=5432, default_db=None) -> Optional[str]; reads <prefix>_USER/_PASSWORD (Secret) + _HOST/_PORT/_DB (ConfigMap), URL-quotes creds safe='', falls back to os.getenv(fallback_env), else None
- PostgresDB.__init__(connection_string=None, min_connections=None, max_connections=None, command_timeout=None, migrations_dir: Optional[Path]=None) at orchestrator/database/postgres.py:275; MIGRATIONS_APP_DIR/MIGRATIONS_VECTOR_DIR at postgres.py:169-170 (add MIGRATIONS_AUDIT_DIR here); apply_migrations() at postgres.py:7369-7407 wraps run_migrations(self._pool, self._migrations_dir); pool-size envs POSTGRES_MIN/MAX_CONNECTIONS are shared across instances — audit pool tuning must use constructor args
- Lifespan slot (orchestrator/main.py): instances 236-255 (vector pattern: _build_pg_url('VECTOR_POSTGRES', fallback_env='VECTOR_DB_URL') + PostgresDB(connection_string=..., migrations_dir=MIGRATIONS_VECTOR_DIR)); connects 3323-3325; mongodb.ensure_indexes() 3332 (no PG analogue, delete); apply_migrations app+vector 3339-3340 (third audit run slots here, pre-traffic); set_mongodb share 3391; disconnect 3672
- NetworkPolicy mechanics: each DB policy selects consumers by srw.componentSelectorLabels (app.kubernetes.io/name + instance + component, _helpers.tpl:133-136). Dynamic agent pods get those labels from agent_provisioner.py:1018-1035 via env AGENT_LABEL_NAME/AGENT_LABEL_INSTANCE (orchestrator/deployment.yaml:803-810) + component=agent. New auditdb policy = clone pgvector block (network-policies.yaml:53-78): ingress from orchestrator + agent podSelectors, port 5432. Agent allowance mandatory (archiver writes from agent pod).
- Agent env propagation: dynamic agent pods envFrom the whole ConfigMap + Secret (agent_provisioner.py:1060-1063; names from AGENT_CONFIGMAP/AGENT_SECRET envs, provisioner:66-67, deployment.yaml:788-791) — AUDIT_POSTGRES_* reaches agents with zero template work. Orchestrator uses an explicit env list: must add 5 AUDIT_POSTGRES_* entries replacing MONGODB_URL at orchestrator/deployment.yaml:130-134 (mirror VECTOR block 105-129); wait-for-mongodb initContainer at 37-41 -> wait-for-auditdb nc <fullname>-auditdb 5432
- ESO: external-secret.yaml:19-21 dataFrom extract bulk-projects the Vault bundle -> zero template changes for AUDIT_POSTGRES_USER/PASSWORD; target Secret name = srw.secretName (external-secret.yaml:17, _helpers.tpl:59-65); refreshInterval 1h (values.yaml:320). Reloader auto annotation on orchestrator deployment (deployment.yaml:7-10, reloader.enabled default true values.yaml:918) -> Secret/ConfigMap key changes bounce the orchestrator automatically; dynamic agent pods only pick up env on recreation
- Copy-template verdict: postgres-vector.yaml (not postgres-keycloak.yaml) is the correct source — 2-flag conditional (vector:1), Secret-driven user/password (VECTOR_POSTGRES_USER/PASSWORD secretKeyRef, vector:54-64), ConfigMap db (vector:65-69), PVC resource-policy keep. postgres-keycloak hardcodes user/db + KC_DB_PASSWORD + 4-flag conditional. Component label 'auditdb' (7 chars)
- ConfigMap: add AUDIT_POSTGRES_HOST/PORT/DB mirroring VECTOR trio (configmap.yaml:19-21); drop MONGODB_URL configmap.yaml:30-31. Helpers: add srw.auditPostgresHost/Port/Db beside _helpers.tpl:456-478; delete srw.mongoHost 408-410 + srw.mongodbUrl 480-489 post-cutover. values.yaml: add databases.audit{enabled,internal,externalHost,externalPort,externalDb,image,storageClass,storageSize,resources} (mirror vector 364-381); delete hostnames.mongo:61, databases.mongodb:404-418, mongoExpress:881-890
- CI: test-deps install lines main.yml:244 / develop.yml:469 (uv pip install -r requirements.txt -r orchestrator/requirements.txt pytest pytest-asyncio), pytest at 246/471, NO database/services. No migration dry-run gate anywhere (migrate.py:319 --dry-run CLI unused). develop.yml python-changed filter (302-305: src/ orchestrator/ tests/ config/ agent.py requirements.txt orchestrator/requirements.txt) already covers orchestrator/database/migrations/audit/; a new requirements-dev.txt is NOT covered and must be added. helm lint (main.yml:65-68 blocking) does NOT fail on missing required values (verified live: INFO only, 0 charts failed) — no real render gate exists
- orchestrator/init.py audit-Mongo inventory: MONGODB_URL read 1485-1487 (+1515/1656/1703 logs); _parse_mongodb_url 1490-1500; init_mongodb 1502-1571; _create_mongodb_indexes 1573-1609 (imports MONGODB_INDEX_DECLARATIONS at 1590); verify_mongodb 1612-1643; backup_mongodb 1645-1689; restore_mongodb 1692-1757; orchestration 1889/1910-1915, 1936/1953-1954, 1980-1985, 2013-2022; CLI 19/2041/2058-2060/2096/2109-2119/2170. DEFAULT_DS_MONGODB_* at 701,737-746 STAYS (customer datasource). Root init.py: 5,23,129-151,161,177-182,232,242-287,327,368-375,427,471-478,514,530,543-545,630
- Dockerfile.orchestrator runtime deps = libpq5+curl+openssh-client only (lines 50-55): mongodump/mongorestore are NOT installed (today's in-container backup is dead code) and pg_dump/pg_restore for the replacement requires adding postgresql-client — missing from both design doc and roadmap. MONGODB_URL doc comment at Dockerfile.orchestrator:13
- Post-cutover helm deletions with verified anchors: databases/mongodb.yaml (whole), databases/network-policies.yaml:80-110 (mongo policy), optional/mongo-express.yaml (whole, incl. its wait-for-mongodb 26-31), ingress.yaml:11 + 443-484, cockpit/deployment.yaml:17 (mongoExpressUrl), helm/ci/test-values.yaml:19-20 (mongoExpress enabled — blocking main.yml lint renders it), helm/ci/customer-external-values.yaml:45-48+74-75, values.example.yaml:80-83+126-127, helm/README.md:5,31,36,54,61,97,123,192-269
- Health/monitoring: /api/health (main.py:3843-3846) is static {'status':'ok'} — no DB checks, nothing to change; k8s probes use it (deployment.yaml:904-915). Endpoint anchors current tree: /audit@8390, /requests/{doc_id}@8444 (doc_id:str at 8445), /audit/timerange@8479, /chat@8496, /audit/bulk@9311, /chat/bulk@9348, /graph/bulk@9385, /version@9422, /llm-requests@14623 (avail 14637), enrichers 3933-3936/3953-3954/18814-18816; graph_routes.py:20 set_mongodb + :143 mongodb._db['agent_audit']
- Local dev: Tiltfile + scripts have zero mongo refs and no DB port-forwards (chart-managed via helm_resource, Tiltfile:282-288). values-local.example.yaml: add AUDIT_POSTGRES_USER/PASSWORD to secrets.values block 91-97 (secrets.create mode — missing keys wedge pods), optional databases.audit.storageSize in 196-206, remove databases.mongodb.storageSize 201-202 post-cutover; every dev's gitignored values-local.yaml needs the same manual edit
- Deps: orchestrator/requirements.txt:9 motor>=3.3.0 deletable P8 (only mongo dep in orchestrator image; pymongo arrives transitively via motor); root requirements.txt:62 pymongo>=4.6.0 STAYS for src/tools/mongodb/ (G5). asyncpg already in both (orch:8, root:5). Compose files all 3 still exist (G6 open)
- Doc mismatches found (code wins): (1) design doc values block says databases.audit.externalUrl — chart convention is externalHost/Port/Db; (2) values.example.yaml + customer-external-values.yaml already stale (postgres/vector externalUrl keys ignored by helpers, masked because helm lint treats required-failures as INFO); (3) workspace-network-policy.yaml:168-175 27017 egress is podSelector-scoped to chart's mongodb component — dead code post-cutover, not 'must stay' (external customer Mongo on 27017 is blocked by the tier internet-allowlist 80/443/22 anyway); (4) line drift: values.yaml mongodb 404-418 (not 404-409), mongoExpress 881-890 (not 866-868), main.py /llm-requests 14623/14637 (not 14563/14572), third enricher 18814-18816 (not 18740-18742), restore_mongodb ends ~1757 (not 1740); (5) root init.py inventory missing lines 161/280-287/327-375/427-478/530; (6) pgadmin does no server pre-population at all (optional/pgadmin.yaml — only reuses POSTGRES_PASSWORD as login), and pgadmin is NetPol-allowed only to main postgres, not pgvector (pre-existing) — auditdb-in-pgadmin needs both manual registration AND a NetPol allowance
- Latent gap: orchestrator/services/persistent_provisioner.py (fallback used at main.py:1936-1937) builds pods envFrom (502-509) but WITHOUT chart labels (no AGENT_LABEL/app.kubernetes.io hits) — its pods are blocked by all DB NetworkPolicies today and will be blocked from auditdb; agent_provisioner.py is the primary live path

Infra risks:

- Secret-provisioning order is the #1 first-deploy breaker: orchestrator/deployment.yaml's new AUDIT_POSTGRES_USER/PASSWORD secretKeyRefs (non-optional, mirroring the VECTOR pattern) + the auditdb StatefulSet's own secretKeyRefs wedge pods in CreateContainerConfigError if the keys aren't in the Secret yet. ESO mode: add keys in Vault BEFORE helm upgrade and force an ESO refresh (refreshInterval is 1h, values.yaml:320); chart-create dev mode: every dev must hand-edit their gitignored values-local.yaml (the example file update doesn't propagate). Decide explicitly: optional:true + build_postgres_url->None->no-op archiver (graceful, silent) vs required (loud, blocks rollout).
- NetworkPolicy gap modes: (a) forgetting the agent podSelector on the auditdb policy silently kills all audit writes (archiver runs in agent pods; failures are swallowed by design — the known 'surface silent aux failures' problem) while orchestrator reads still work; (b) pods from the persistent_provisioner fallback path carry no chart labels and are blocked from every DB including auditdb; (c) installs that set AGENT_LABEL_NAME/INSTANCE empty lose component labels entirely (agent_provisioner.py:1030-1035 conditional).
- Hard-fail vs degrade divergence: vector DSN missing raises RuntimeError at import time (main.py:246-251) but Mongo missing is non-fatal today. If the audit pool copies the vector hard-fail, every existing deployment (prod-private, dev, homelab) crash-loops on rollout until Vault/Secret keys land — sequence Vault first, or ship the no-op fallback during the AUDIT_BACKEND flag window.
- No CI render or migration gate: helm lint passes even with missing required values (verified live — INFO only), no helm template runs, no migrate.py --dry-run, and tests run with no database. The audit chart shapes (internal/external/disabled) and 0001_initial.sql are only exercised manually (roadmap P6) or via new testcontainers tests; a broken template or DDL ships green to the 30-min CI/CD round trip unless gates are added.
- Backup tooling: the planned pg_dump/pg_restore swap in init.py cannot work in-container without adding postgresql-client to docker/Dockerfile.orchestrator (runtime apt list, lines 50-55) — currently absent, and mongodump is absent too (today's backup path silently no-ops in k8s). Version-match pg_dump client to the auditdb server major.
- pg_partman (gate G0): stock postgres:15 image lacks the extension; choosing pg_partman forces an image swap (pgpartman/pgpartman:15 or custom build) in databases.audit.image, with pull-policy/supply-chain implications for the GHCR-only install story. Roadmap's own advice: fall back to hand-rolled rather than custom image.
- Migration-ordering on first boot: lifespan applies audit migrations after connect (main.py:3323-3341 block) but agent pods can start archiving as soon as the pool spins up agents — the wait-for-auditdb initContainer gates the orchestrator only. Window is small (migrations run before serve, agents are provisioned by the orchestrator) but a paused/HA second orchestrator or pre-existing agent pool could write before DDL exists; the adapter should tolerate missing-table errors gracefully during the flag window.
- P8 cleanup coupling: deleting mongoExpress values/templates without updating helm/ci/test-values.yaml:19-20 breaks main.yml's blocking helm lint (template references to deleted named templates DO fail lint, unlike required-misses); deleting srw.mongoHost while ingress.yaml:11 still references it is the same class of break.
- Cross-cutting deploys not in this repo: prod-private (HomeLab/deployments_managed/srw-prod-private) pins chart version + image tags via Fleet and its Vault path must gain the AUDIT keys before the chart bump rolls; deployment/values-experimental.yaml similarly. Reloader will bounce the orchestrator the moment ESO syncs new keys — harmless but expect one restart before the chart upgrade.
- requirements-dev.txt filter gap: develop.yml's python-changed paths (302-305) don't include a root requirements-dev.txt, so a PR touching only dev deps skips test-python entirely — add the path when creating the file.

---

## 7. Research digest

### Partition management (hand-rolled, PG 15/16)

- CREATE TABLE ... PARTITION OF takes ACCESS EXCLUSIVE on the parent; CREATE standalone + CHECK + ATTACH PARTITION needs only SHARE UPDATE EXCLUSIVE (PG 16 docs) — prefer the latter, with lock_timeout+retry
- CREATE TABLE IF NOT EXISTS is not concurrency-safe: simultaneous creators collide on pg_type_typname_nsp_index (Tom Lane); IF NOT EXISTS also only checks the name, not that the relation is an attached partition — serialize creators with pg_advisory_xact_lock and diff against pg_inherits
- DETACH PARTITION CONCURRENTLY (PG 14+): two internal transactions (txn1 SUE on parent+partition, marks inhdetachpending, waits for all transactions using the table; txn2 SUE on parent + ACCESS EXCLUSIVE on partition); cannot run in a transaction block, is forbidden if a default partition exists, max one pending detach per table; cancelled runs need DETACH ... FINALIZE
- Plain DETACH takes ACCESS EXCLUSIVE on the parent; DROP TABLE of an attached partition also takes ACCESS EXCLUSIVE on the parent — always detach first, then DROP the standalone table after a grace period (16.5 fixed crashes from DETACH CONCURRENTLY + immediate drop)
- FKs with a partitioned referenced table are supported since PG 12 (both sides may be partitioned), BUT detaching/dropping a referenced partition that still has referencing rows fails by design: ERROR 'removing partition ... violates foreign key constraint ... is still referenced from table ...' (confirmed expected by Álvaro Herrera on PG 16.6; PG 15 allowing it was 'probably a bug')
- With chat_history retention (365d) > llm_requests retention (90d), real FKs make llm_requests partition retention permanently impossible — and every detach SHARE-locks all referencing tables; 15.9/16.5 and 16.9/17.5 minor releases fixed catalog-corruption bugs in exactly this FK-to-partitioned-table detach path
- Standard log/event-store practice is plain BIGINT soft references, no FK: GitLab 'loose foreign keys' (async cleanup instead of constraints), TimescaleDB forbids FKs referencing hypertables outright, Hatchet's FK-laden deletes needed 24/7 chunked deletion pre-partitioning, pg_partman's author showed FK enforcement on partition sets impractical
- Autovacuum never runs ANALYZE on partitioned parents (PG 16 docs verbatim); cron a manual ANALYZE — Hatchet saw row estimates off by 6,100,000x and 10x slower queries without it; on 15/16 ANALYZE on the parent recurses into every leaf (ONLY-skip is a later-release feature)
- Insert-only tuning: PG 13+ insert-triggered autovacuum (defaults: threshold 1000, scale factor 0.2 — lower per partition), autovacuum_freeze_min_age=0 per table so insert-vacuums freeze tuples immediately (docs + CYBERTEC), fillfactor 100 is already the default and correct for append-only
- Partition counts: official guidance 'up to a few thousand partitions fairly well, provided ... pruning'; ~27 steady-state / ~360 total monthly partitions across 3 tables is trivial; the real 15/16 ceiling is the 16 fast-path lock slots per backend — generic plans or unpruned queries locking many partitions cause LWLock:LockManager waits (Midjourney's 1,002 daily partitions; fixed only in PG 18)
- No default partition means a missing partition makes inserts fail hard: ERROR 'no partition of relation ... found for row', SQLSTATE 23514 check_violation — alert on (1) max upper bound from pg_get_expr(relpartbound) minus now() under threshold, (2) creator-job heartbeat (Hatchet: 2-day lead 'so we have plenty of time to respond'), (3) SQLSTATE 23514 in logs, (4) stuck inhdetachpending partitions

Sources:

- [PostgreSQL 16 Documentation — 5.11 Table Partitioning](https://www.postgresql.org/docs/16/ddl-partitioning.html) — ATTACH needs only SHARE UPDATE EXCLUSIVE vs ACCESS EXCLUSIVE for CREATE...PARTITION OF; plain DETACH needs AEL on parent; inserts with no matching partition 'will cause an error'; planner handles 'up to a few thousand partitions fairly well, provided ... pruning'; per-session partition metadata memory growth.
- [PostgreSQL 16 Documentation — CREATE TABLE](https://www.postgresql.org/docs/16/sql-createtable.html) — PARTITION OF requires ACCESS EXCLUSIVE on the parent, as does DROP TABLE of a partition; IF NOT EXISTS gives 'no guarantee that the existing relation is anything like the one that would have been created'; fillfactor default is 100 (lower only helps updates/HOT).
- [PostgreSQL 16 Documentation — ALTER TABLE](https://www.postgresql.org/docs/16/sql-altertable.html) — Exact DETACH CONCURRENTLY two-transaction protocol and lock levels; cannot run in a transaction block; not allowed with a default partition; at most one pending detach; FINALIZE completes interrupted detaches; SHARE lock taken on FK-referencing tables; detached partition becomes a standalone table with no ties to the parent.
- [postgres/postgres commit 71f4c8c6 — ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY](https://github.com/postgres/postgres/commit/71f4c8c6) — Implementation detail: pg_inherits.inhdetachpending; snapshot-based visibility of pending-detach partitions; default partition disallowed because its constraint change would need an exclusive lock; FINALIZE waits for old snapshots (follow-up fix).
- [PostgreSQL 16 Documentation — 25.1 Routine Vacuuming](https://www.postgresql.org/docs/16/routine-vacuuming.html) — Verbatim: partitioned tables 'are not processed by autovacuum ... autovacuum does not run ANALYZE on partitioned tables'; insert-triggered vacuum formula; for insert-only tables 'it may be beneficial to lower the table's autovacuum_freeze_min_age' so tuples freeze earlier and pages go all-visible.
- [PostgreSQL 16 Documentation — ANALYZE](https://www.postgresql.org/docs/16/sql-analyze.html) — ANALYZE on a partitioned table samples all partitions AND recurses into each leaf; 'The autovacuum daemon does not process partitioned tables ... usually necessary to periodically run a manual ANALYZE'.
- [PostgreSQL 16 Documentation — 20.10 Automatic Vacuuming (runtime config)](https://www.postgresql.org/docs/16/runtime-config-autovacuum.html) — autovacuum_vacuum_insert_threshold default 1000; autovacuum_vacuum_insert_scale_factor default 0.2; -1 disables insert-triggered vacuum.
- [PostgreSQL 16 Documentation — Appendix A. Error Codes](https://www.postgresql.org/docs/16/errcodes-appendix.html) — 23514 = check_violation; no dedicated SQLSTATE exists for 'no partition of relation found for row' — it surfaces under class 23.
- [PostgreSQL 16.5 Release Notes (Nov 2024)](https://www.postgresql.org/docs/release/16.5/) — Fixed catalog-state corruption when ATTACH/DETACHing partitions with FKs to partitioned tables (detached table missing enforcement triggers → rows violating the FK); ships a detection query + drop/re-add repair; also fixed crashes from DETACH CONCURRENTLY + immediate drop, and disallowed ATTACH of a table with an FK referencing the partitioned table.
- [Álvaro Herrera — pgsql-general: 'PostgreSQL 15.9 Update: Partitioned tables with foreign key constraints'](https://www.postgresql.org/message-id/202411191634.vexoqghfj2aq@alvherre.pgsql) — Authoritative guidance on the 15.9/16.5 FK-detach corruption: constraints were broken by DETACH on old code; 'I'd advise against running ALTER TABLE DETACH until you have upgraded ... for partitioned tables that have foreign keys pointing to other partitioned tables'.
- [pgsql-general thread: 'PostgreSQL 16 - Detach partition with FK - Error'](https://postgrespro.com/list/thread-id/2731645) — On 16.6, detaching a referenced partition fails: ERROR 'removing partition ... violates foreign key constraint ... is still referenced from table ...'; Herrera: expected behavior, PG 15 permitting it was 'probably a bug'; workaround = detach child partition first, drop its FK, then detach parent partition.
- [Laurenz Albe — pgsql-general: 'Can not drop partition if exist foreign keys'](https://www.postgresql.org/message-id/4734b034a623e8fa9d7522e4b4b7995cd1f3728f.camel@cybertec.at) — 'That is working as designed. You cannot detach a partition of a table if a foreign key points to it' — recommended alternative is per-partition FK constraints rather than table-level FKs.
- [PostgreSQL 12.0 Release Notes](https://www.postgresql.org/docs/release/12.0/) — 'Allow foreign keys to reference partitioned tables' (Álvaro Herrera) — the support baseline for partitioned-to-partitioned FKs.
- [EDB blog (Álvaro Herrera) — PostgreSQL 12: Foreign Keys and Partitioned Tables](https://www.enterprisedb.com/blog/postgresql-12-foreign-keys-and-partitioned-tables) — Since PG 12 'you can have a partitioned table on either side of a foreign key constraint'; index referencing columns if referenced rows are ever modified; no partition-retention caveats addressed.
- [PostgreSQL 16.9 Release Notes (May 2025)](https://www.postgresql.org/docs/release/16.9/) — Second round of partitioned-FK catalog fixes: creating/attaching partitions failed to make required catalog entries for self-referential FKs on partitioned tables → constraint not fully enforced; users told to drop and recreate affected constraints.
- [Hatchet — The pitfalls of partitioning Postgres yourself](https://hatchet.run/blog/postgres-partitioning) — Production post-mortem: 2-day partition pre-creation lead time; DETACH CONCURRENTLY + FINALIZE runbook for orphaned detaches; autovacuum doesn't ANALYZE parents → row estimates off 6,100,000x and 10x slower queries; pre-partition FK-laden deletes required 24/7 chunked deletion.
- [Brandur Leach — Partitioning in Postgres, 2022 edition](https://brandur.org/fragments/postgres-partitioning-2022) — Idempotent creator job every 10 min, 3-day lookahead buffer, idempotency via listing existing partitions and diffing; staged retention: DETACH, keep 3 days for inspection, then DROP.
- [GitLab development docs — Loose foreign keys](https://docs.gitlab.com/development/database/loose_foreign_keys/) — Production-scale precedent for replacing FK constraints with plain columns + async cleanup ('delayed association cleanup without negatively affecting the application performance'); the deleted-records queue is itself a sliding-partition table whose old partitions are detached automatically.
- [Keith Fiske (pg_partman author) — Table Partitioning and Foreign Keys](https://www.keithf4.com/table-partitioning-and-foreign-keys/) — Demonstrates why FK enforcement against partition sets is impractical (checks don't propagate in old inheritance; full-tree scans would be needed); explores trigger-based pseudo-FK alternatives for partitioned log sets.
- [pg_partman documentation (development)](https://github.com/pgpartman/pg_partman/blob/development/doc/pg_partman.md) — premake default = 4 periods ahead ('how many additional partitions to always stay ahead'); check_default() exists to monitor rows leaking into default partitions; retention detaches by default (retention_keep_table=TRUE) with optional drop.
- [CYBERTEC (Laurenz Albe) — PostgreSQL v13: tuning autovacuum on insert-only tables](https://www.cybertec-postgresql.com/en/postgresql-autovacuum-insert-only-tables/) — PG 13 introduced insert-triggered autovacuum (threshold 1000 / scale factor 0.2, overridable per table); for insert-only tables set vacuum_freeze_min_age=0 so tuples are frozen at the first vacuum.
- [CYBERTEC (Laurenz Albe) — Automatic partition creation in PostgreSQL](https://www.cybertec-postgresql.com/en/automatic-partition-creation-in-postgresql/) — Survey of hand-rolled options: scheduler-driven pre-creation (CREATE + ATTACH) vs trigger-based on-demand creation (locking/race hazards) vs pg_partman; core PostgreSQL has no automatic partition creation.
- [Tom Lane — pgsql: duplicate key violates unique constraint on pg_type_typname_nsp_index](https://www.postgresql.org/message-id/11235.1268149874@sss.pgh.pa.us) — Two sessions creating the same table name simultaneously collide on pg_type catalog entries — the root cause of the CREATE TABLE IF NOT EXISTS concurrency race.
- [golang-migrate issue #55 — duplicate key value violates unique constraint 'pg_type_typname_nsp_index'](https://github.com/golang-migrate/migrate/issues/55) — Real-world reproduction of the concurrent CREATE TABLE IF NOT EXISTS catalog race by parallel migration runners — the failure your advisory lock prevents.
- [pgPedia — pg_advisory_xact_lock()](https://pgpedia.info/p/pg_advisory_xact_lock.html) — Transaction-level exclusive advisory lock, auto-released at transaction end — the right primitive for serializing partition-creation transactions without leak risk.
- [Jeremy Schneider (ardentperf) — Postgres Indexes, Partitioning and LWLock:LockManager Scalability](https://ardentperf.com/2024/03/03/postgres-indexes-partitioning-and-lwlocklockmanager-scalability/) — 16 fast-path lock slots per backend; generic plans bypass pruning and lock all partitions (benchmark: 75K TPS with 70% LWLock:LockManager vs 245K TPS without); Midjourney's 1,002 daily partitions caused contention that receded after moving to weekly partitions.
- [pganalyze — GitLab's challenge with Postgres LWLock lock_manager contention](https://pganalyze.com/blog/5mins-postgres-LWLock-lock-manager-contention) — Only 16 fast-path locks per connection/transaction; queries scanning multiple partitions (each with its own indexes) acquire more locks and trigger lock_manager LWLock contention at high QPS.
- [PostgresAI — Fast-path locking explained](https://postgres.ai/blog/20251008-postgres-marathon-2-004) — FP_LOCK_SLOTS_PER_BACKEND=16 on PG ≤17; PG 18 changed fast-path storage to scale with max_locks_per_transaction — i.e., on 15/16 the 16-slot limit is fixed.
- [Tiger Data — How to Fix 'No Partition of Relation Found for Row'](https://www.tigerdata.com/blog/how-to-fix-no-partition-of-relation-found-for-row) — Exact error text for unroutable inserts; mitigation tradeoffs: scheduler-based pre-creation (maintenance risk, locking) vs default partition (collects data that must later be moved — blocks clean operation).
- [Hussein Nasser — The Danger of Detaching Partitions in Postgres](https://medium.com/@hnasr/the-danger-of-detaching-partitions-in-postgres-020dd919d038) — Pre-PG14 plain DETACH took ACCESS EXCLUSIVE on parent + partition, blocking all reads/writes across the whole partitioned table (production incident); PG 14's CONCURRENTLY relaxes the parent lock; know your lock conflicts (pglocks.org).
- [Tiger Data / TimescaleDB docs — Limitations](https://docs.tigerdata.com/use-timescale/latest/limitations/) — 'Foreign key constraints referencing a hypertable are not supported' — the largest Postgres time-series product simply forbids inbound FKs to partitioned/chunked stores, reinforcing the no-FK norm for event tables.
- [tech-champion — Automated Partition Management in Core PostgreSQL](https://tech-champion.com/database/postgresql/automated-partition-management-in-core-postgresql-a-comprehensive-guide/) — Lookahead-based creation (e.g., 7 days ahead); alert 'if no new partitions have been created for a table within 24 hours of its lookahead threshold'; on-demand creation needs an immediate ACCESS EXCLUSIVE on the parent which can stall transactions.
- [Luca Ferrari (fluca1978) — A recursive CTE to get information about partitions](https://fluca1978.github.io/2019/06/12/PartitioningCTE.html) — Catalog-query pattern (pg_inherits + pg_class + pg_get_expr(relpartbound, oid)) to enumerate partitions and their bounds — the basis for a 'days until newest partition bound' alarm.
- [PostgreSQL commit 62ddf7ee9a — Add ONLY support for VACUUM and ANALYZE](https://git.postgresql.org/gitweb/?a=commit&h=62ddf7ee9a399e0b9624412fc482ed7365e38958&p=postgresql.git) — ANALYZE ONLY <partitioned parent> (skip recursing into leaves) is a post-16 feature — on PG 15/16 a parent ANALYZE always recurses into every leaf partition, so schedule it accordingly.
- [Sequin — Time-based retention strategies in Postgres](https://blog.sequinstream.com/time-based-retention-strategies-in-postgres/) — Partition drop is a metadata operation removing millions of rows with no dead-tuple/vacuum debt vs DELETE; rolling your own pg_partman-style maintenance is 'very feasible' and should integrate with existing monitoring/alerting.

### Append-heavy JSONB + asyncpg operations

- PgBouncer 1.21.0 (Oct 2023) added protocol-level prepared-statement tracking in transaction mode via max_prepared_statements; 1.22 added DEALLOCATE ALL/DISCARD ALL; 1.24.0 (Jan 2025) enabled it by default with max_prepared_statements=200; SQL-level PREPARE/EXECUTE/DEALLOCATE remain unsupported
- asyncpg's documented safe setting behind transaction-mode PgBouncer is statement_cache_size=0 on connect()/create_pool(); the cache default is 100 statements and asyncpg still uses (unnamed) protocol-level prepared statements even with the cache off
- synchronous_commit=off risks bounded data loss, never corruption: PostgreSQL docs state recovery replays WAL to the last flushed record into a self-consistent state; worst-case loss window is 3x wal_writer_delay (~600ms at the 200ms default)
- synchronous_commit is scopable per transaction (SET LOCAL), per session (SET), or per role (ALTER USER ... SET); Percona explicitly names logging/auditing as the canonical async-commit use case; DROP TABLE and 2PC always commit synchronously
- For batched JSONB inserts: executemany is atomic and supports ON CONFLICT/RETURNING semantics; copy_records_to_table (binary COPY) is fastest for pure appends but a custom text-format jsonb codec breaks it ('no binary format encoder for type jsonb OID 3802') — register a binary codec that prepends the \x01 jsonb version byte (orjson.dumps returns bytes, fitting format='binary')
- TOAST lz4 vs pglz on JSON: Fujitsu measured pglz 2.23x vs lz4 2.07x average ratio but lz4 compresses in ~20% of pglz's time; on real JSON datasets lz4 was actually smaller (depesz: 65 vs 73MB; credativ gharchive: 38 vs 41GB vs 98GB uncompressed) and 2x faster to load — default_toast_compression=lz4 with default EXTENDED storage is the right default
- Postgres TOAST compression only engages above the ~2KB threshold, so small audit events stay uncompressed; MongoDB WiredTiger compresses every block (snappy ~28%, zstd ~49% reduction in Percona's test) — head-to-head measurements show Mongo 1.4-2.23x smaller on disk including indexes; expect Mongo 1.4-2.5x smaller for audit-style data
- Throughput references: single-connection sync INSERTs are RTT-bound (~100-500/s); tuned laptop NVMe did ~17.4k small JSONB doc inserts/s and ~2.2k/s for complex docs; 50kB JSONB rows via COPY ran ~1.9k rows/s (~90MB/s) with lz4 TOAST (half that with pglz); a 96-vCPU/120k-IOPS RDS box ceilinged at 144k inserts/s with processes serialized on the WAL flush lock
- Bottleneck order for this workload: WAL fsync serialization (sync commit) -> WAL volume + TOAST/WAL compression CPU (async commit) -> checkpoint full-page-write bursts; mitigate with synchronous_commit=off, lz4, bigger max_wal_size and longer checkpoint_timeout
- Set-and-forget for a 2-4GB pod: shared_buffers 25% of RAM (max 40%), max_wal_size 4-8GB (default 1GB is checkpoint-trigger-prone; watch checkpoint_warning), checkpoint_timeout 15-30min, checkpoint_completion_target 0.9 (default), wal_compression=lz4 (PG15+), wal_buffers auto
- Kubernetes: fio-benchmark the storage class before trusting it (CloudNativePG guidance); Longhorn/GlusterFS show poor/variable fsync latency for WAL while Ceph RBD is consistent-but-slower; one Longhorn 'fast' result traced to a volatile write cache; async commit decouples commit latency from PV fsync latency
- k3d/local-path: hostPath PVs with no replication or capacity enforcement (node loss = data loss); in k3d the storage dir lives inside the node container and dies with k3d cluster delete unless mapped via --volume <hostdir>:/var/lib/rancher/k3s/storage@all

Sources:

- [asyncpg FAQ (PgBouncer section)](https://magicstack.github.io/asyncpg/current/faq.html) — Transaction/statement pool modes don't support asyncpg's prepared statements; official fixes are asyncpg's own pool, statement_cache_size=0 (and no Connection.prepare()), or session mode. [1]
- [asyncpg API Reference](https://magicstack.github.io/asyncpg/current/api/index.html) — statement_cache_size default 100 (0 disables), max_cached_statement_lifetime 300s, max_cacheable_statement_size 15KiB; executemany is atomic; copy_records_to_table uses binary COPY and accepts async iterables; codec format 'text' = str, 'binary' = bytes. [2]
- [asyncpg Usage docs — automatic JSON conversion](https://magicstack.github.io/asyncpg/current/usage.html) — Canonical set_type_codec('json'/'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog') pattern; default exchange type for json/jsonb is str. [3]
- [asyncpg issue #1058 — prepared statements used despite being disabled](https://github.com/MagicStack/asyncpg/issues/1058) — Even with statement_cache_size=0 asyncpg uses unnamed protocol-level prepared statements; edge-case errors persist with some poolers; error text embeds the PgBouncer transaction-mode warning. [4]
- [asyncpg issue #783 — no binary format encoder for jsonb in COPY](https://github.com/MagicStack/asyncpg/issues/783) — copy_records_to_table fails with a custom text-format jsonb codec because binary COPY needs a binary encoder; workaround is a binary codec or pre-serialized strings. [5]
- [asyncpg issue #140 — JSONB custom type codec (binary, \x01 prefix)](https://github.com/MagicStack/asyncpg/issues/140) — Worked example of a binary jsonb codec: encoder prepends b'\x01' to UTF-8 JSON, decoder strips it; format='binary'. [6]
- [PgBouncer config documentation](https://www.pgbouncer.org/config.html) — max_prepared_statements (default 200) enables protocol-level PS tracking in transaction/statement mode; SQL PREPARE/EXECUTE/DEALLOCATE pass through; memory math (~17MB for 1000 clients x 200 5kB queries); server_reset_query unused in transaction mode; default_pool_size 20, server_idle_timeout 600s. [7]
- [PgBouncer FAQ](https://www.pgbouncer.org/faq.html) — Since 1.21.0 PgBouncer tracks prepared statements in transaction pooling and prepares them on-the-fly on the linked server connection; requires max_prepared_statements > 0; session mode needs DISCARD ALL/DEALLOCATE ALL reset. [8]
- [PgBouncer changelog](https://www.pgbouncer.org/changelog.html) — 1.21.0 (2023-10-16) PS support + 15-250% gains; 1.22.0 (2024-01-31) DEALLOCATE ALL/DISCARD ALL; 1.24.0 (2025-01-10) enabled by default (max_prepared_statements=200); 1.25.0 (2025-11-09) latest line. [9]
- [pganalyze — PgBouncer 1.21 adds prepared statement support in transaction mode](https://pganalyze.com/blog/5mins-postgres-pgbouncer-prepared-statements-transaction-mode) — Release context (Oct 2023), how transparent tracking works, max_prepared_statements starting guidance, protocol-only limitation and DEALLOCATE driver friction. [10]
- [Crunchy Data — Prepared Statements in Transaction Mode for PgBouncer](https://www.crunchydata.com/blog/prepared-statements-in-transaction-mode-for-pgbouncer) — PgBouncer renames statements internally (PGBOUNCER_n) and re-prepares per backend; protocol-level only; planning savings example 170ms to 0.02ms; DBD::Pg pg_skip_deallocate workaround. [11]
- [PostgreSQL docs — 28.4 Asynchronous Commit](https://www.postgresql.org/docs/current/wal-async-commit.html) — Risk window = 3x wal_writer_delay max; 'data loss, not data corruption'; per-transaction selectable; SET LOCAL synchronous_commit TO OFF; DROP TABLE/2PC always sync; contrast with fsync=off corruption risk. [12]
- [PostgreSQL 16 docs — 19.5 Write Ahead Log (runtime-config-wal)](https://www.postgresql.org/docs/16/runtime-config-wal.html) — Defaults: synchronous_commit=on, wal_compression off (pglz/lz4/zstd options), wal_writer_delay 200ms, wal_buffers -1 (1/32 shared_buffers, max ~16MB), checkpoint_timeout 5min, checkpoint_completion_target 0.9, max_wal_size 1GB soft limit, commit_delay 0/commit_siblings 5. [13]
- [PostgreSQL 16 docs — 30.5 WAL Configuration](https://www.postgresql.org/docs/16/wal-configuration.html) — First post-checkpoint page modification logs the full page; checkpoints expensive; checkpoint_warning sanity check; timeout vs max_wal_size trigger; spread via completion_target 0.9; commit_delay tuning via pg_test_fsync. [14]
- [PostgreSQL 16 docs — 19.4 Resource Consumption (shared_buffers)](https://www.postgresql.org/docs/16/runtime-config-resource.html) — Dedicated server >=1GB RAM: start shared_buffers at 25% of RAM, unlikely >40% helps; larger shared_buffers usually require larger max_wal_size. [15]
- [Percona — PostgreSQL synchronous_commit options and synchronous standby replication](https://www.percona.com/blog/postgresql-synchronous_commit-options-and-synchronous-standby-replication/) — Loss usually <2x wal_writer_delay, worst case 3x; 'won't result in database corruption, unlike fsync'; explicitly recommends async commit for logging/auditing and bulk loads; ALTER USER ... SET synchronous_commit=off scoping. [16]
- [Fujitsu — What is the new LZ4 TOAST compression in PostgreSQL 14, and how fast is it?](https://www.postgresql.fastware.com/blog/what-is-the-new-lz4-toast-compression-in-postgresql-14) — Average ratio pglz 2.23x vs lz4 2.07x (~7% pglz edge); lz4 compression ~20% of pglz's time; SELECT ~20% faster; how to enable via --with-lz4, GUC, or column SET COMPRESSION. [17]
- [depesz — Using JSON: json vs jsonb, pglz vs lz4 (Nov 2025)](https://www.depesz.com/2025/11/29/using-json-json-vs-jsonb-pglz-vs-lz4-key-optimization-parsing-speed/) — 50kB JSON x4100 (~200MB): jsonb+pglz 73MB vs jsonb+lz4 65MB; COPY load 4.42s vs 2.19s — lz4 both smaller and 2x faster on this JSON; recommends lz4 for JSONB. [18]
- [credativ — TOASTed JSONB data in PostgreSQL: compression algorithm tests](https://www.credativ.de/en/blog/postgresql-en/toasted-jsonb-data-in-postgresql-performance-tests-of-different-compression-algorithms/) — gharchive JSONB week: uncompressed ~98GB, pglz ~41GB, lz4 ~38GB; lz4 sustains parallel query throughput where pglz collapses; conversion requires jsonb::text::jsonb rewrite. [19]
- [Percona — Compression methods in MongoDB: Snappy vs Zstd](https://www.percona.com/blog/compression-methods-in-mongodb-snappy-vs-zstd/) — 14.95GB of docs: snappy 10.75GB (~28% reduction, 16K ops/s), zstd L6 7.69GB (~49% reduction, 14.8K ops/s, ~7% throughput cost, comparable CPU). [20]
- [binaryigor — JSON Documents: MongoDB vs PostgreSQL (performance, storage, search)](https://binaryigor.com/json-documents-mongodb-vs-postgresql.html) — Tuned head-to-head on Ryzen/NVMe: PG 1,584MB vs Mongo 710MB incl. indexes (2.23x) on 3.4M docs; 1.4x on complex docs; insert throughput ~tied (17.4k vs 17.7k QPS small docs; ~2.2k QPS complex). [21]
- [Yuriy Ivon — Can PostgreSQL with its JSONB column type replace MongoDB?](https://medium.com/@yurexus/can-postgresql-with-its-jsonb-column-type-replace-mongodb-30dc7feffaf3) — 11M-row Azure benchmark: 'MongoDB requires much less storage space'; PG compresses only TOAST values above ~2KB while WiredTiger compresses everything; Mongo substantially faster on parallel inserts in this setup. [22]
- [Citus Data — Fermi estimates on Postgres performance](https://www.citusdata.com/blog/2017/09/29/what-performance-can-you-expect-from-postgres/) — Single-row INSERTs are RTT-bound (~100/s at 5ms, ~500/s at 1ms); thousands to tens of thousands with concurrency; COPY handles 100,000s of writes/s; indexes can add an order of magnitude. [23]
- [DBOS — Benchmarking workflow execution scalability on Postgres](https://dbos.dev/blog/benchmarking-workflow-execution-scalability-on-postgres) — RDS db.m7i.24xlarge (96 vCPU, 120k io2 IOPS): 144K raw inserts/s; profiled ceiling = serialized WAL flush ('exactly one process was flushing the WAL... others waiting on the WAL lock', group commit). [24]
- [Jacopo Farina — Insert data into Postgres fast](https://jacopofarina.eu/posts/ingest-data-into-postgres-fast/) — 5M several-KB Reddit comments into dockerized PG13: asyncpg prepared ~3.7k rows/s, psycopg2 execute_values ~3.8k, binary COPY ~3.7k — top methods within 30-50% run noise; UNLOGGED staging helps. [25]
- [Small Datum — The benefit of lz4 and zstd for Postgres WAL compression](http://smalldatum.blogspot.com/2022/05/the-benefit-of-lz4-and-zstd-for.html) — Insert Benchmark: index create ~1.7x faster with wal_compression=lz4 vs pglz; pglz ~2x lz4's CPU overhead; lz4/zstd reduce IO and checkpoint frequency; lz4 ~matches compression-off speed. [26]
- [Percona — WAL compression in PostgreSQL and improvements in version 15](https://www.percona.com/blog/wal-compression-in-postgresql-and-recent-improvements-in-version-15/) — PG15 adds lz4/zstd values for wal_compression; lz4 ~pglz ratio at much lower CPU; zstd ~30% better compression than lz4; enabling compression had significant positive I/O impact. [27]
- [EDB — WAL compression algorithms for Postgres 15: a comparison](https://www.enterprisedb.com/blog/you-can-now-pick-your-favorite-compression-algorithm-your-wals) — pglz slightly better ratio than lz4, zstd better than pglz; compression improved TPS in their runs; recommends benchmarking your own workload before enabling. [28]
- [CloudNativePG documentation — Storage](https://cloudnative-pg.io/documentation/current/storage/) — Benchmark storage with fio then pgbench before production; prefer dynamic provisioning; optional separate walStorage volume; resiliency via Postgres instances rather than volume-level replication. [29]
- [rancher/local-path-provisioner README](https://github.com/rancher/local-path-provisioner) — Creates hostPath/local PVs on the node: no replication, no snapshots, no migration between nodes, no quota — node failure loses the data; dev-grade only. [30]
- [kubedo — Fsync latency benchmark: Ceph vs DRBD vs Longhorn (etcd-style)](https://kubedo.com/fsync-latency-etcd-benchmark/) — GlusterFS and Longhorn showed high variance and poor sync latency ('not ideal for etcd, PostgreSQL, or raft-based systems'); Ceph RBD consistent but slower; stability beats raw speed for commit-latency-sensitive systems. [31]
- [vadosware — Everything I've seen on optimizing Postgres on ZFS (Longhorn aside)](https://vadosware.io/post/everything-ive-seen-on-optimizing-postgres-on-zfs-on-linux/) — Longhorn does synchronous remote writes; suspiciously fast pgbench results traced to a volatile write cache — disable the disk write cache for honest durability testing. [32]
- [k3d docs — K3s features in k3d (local-path storage mapping)](https://k3d.io/v5.1.0/usage/k3s/) — local-path default dir /var/lib/rancher/k3s/storage lives inside the k3d node container; map --volume <hostdir>:/var/lib/rancher/k3s/storage@all to persist PV data beyond k3d cluster delete. [33]
- [AKS Engineering Blog — Boosting PostgreSQL performance on AKS](https://blog.aks.azure.com/2025/07/09/postgresql-nvme) — Separating pgdata and WAL onto different Premium SSD volumes doubles the IOPS pool via two disks; with a single local NVMe pool, separate volumes add no performance. [34]
- [Supabase issue #39227 — asyncpg burst failures on both poolers](https://github.com/supabase/supabase/issues/39227) — Real-world report (2025): asyncpg under bursty load still hits prepared-statement errors on a transaction pooler and timeouts on session pooler — pooler-compatibility friction persists beyond PgBouncer itself. [35]
- [asyncpg PR #846 — specify a statement name in Connection.prepare()](https://github.com/MagicStack/asyncpg/pull/846) — asyncpg supports explicit prepared-statement names (Connection.prepare(name=...), PreparedStatement.get_name()), enabling unique-name strategies for pooler setups. [36]
- [postgresqlco.nf — synchronous_commit parameter notes](https://postgresqlco.nf/doc/en/param/synchronous_commit/) — Community-annotated reference: SET LOCAL synchronous_commit TO OFF for a single multistatement transaction; setting reverts at transaction end; async commit boosts small-transaction throughput. [37]
- [schinckel.net — asyncpg and upserting bulk data](https://schinckel.net/2019/12/13/asyncpg-and-upserting-bulk-data/) — copy_records_to_table is the best pure bulk-insert path but cannot express ON CONFLICT; upserts go through executemany or COPY-into-staging-then-INSERT...ON CONFLICT. [38]
- [asyncpg issue #420 — upsert best practice](https://github.com/MagicStack/asyncpg/issues/420) — Maintainer-adjacent discussion confirming executemany with tuples for ON CONFLICT workloads since binary COPY can't do upserts. [39]

### Prior art — Mongo→Postgres for audit/event workloads

- The Guardian migrated ~2.3M content items from self-managed MongoDB to PostgreSQL jsonb on RDS (Jul 2017–Apr 2018) via an API proxy with async dual-writes, response diffing, and GoReplay replay; drivers were two publishing-blocking outages, OpsManager pain, and ~2 months/yr of DBA toil; retrospective lessons: don't auto-generate indexes at app startup, don't break NTP, prefer managed databases, and expect mistakes (their integration tests broke un-noticed at cutover).
- Infisical (2024) migrated fully to relational Postgres; the hard parts were a persistent ObjectId-to-new-ID mapping store (LevelDB) for cross-references and recursive tree structures, not query rewriting; they chose a brief write-freeze over zero-downtime.
- Microsoft open-sourced DocumentDB (pg_documentdb_core BSON type + pg_documentdb_api) under MIT in Jan 2025 and donated it to the Linux Foundation in Aug 2025 with AWS, Google, Cockroach, Crunchy, Supabase and Yugabyte backing; FerretDB 2.x rebuilt on it claims 'up to 20x faster' (vendor claim, no methodology). Its existence endorses Postgres-as-document-platform while implicitly conceding vanilla JSONB's costs for full Mongo-API emulation — costs that don't apply to a plain append-only JSONB audit table.
- Benchmarks split by sponsor: EDB/OnGres 2019 (Postgres-sponsored, fully reproducible) found PG 4–15x faster on transactions; MongoDB's rebuttal published no reproducible code. Independent 2023 rerun (PG 14.6 vs Mongo 6.0.3): MongoDB substantially faster on parallel inserts and ~3x smaller on disk; Postgres composite B-tree indexes beat Mongo on range-reads-with-ordering. MongoDB's Jan 2026 vendor benchmark shows JSONB degrading under update-heavy load (full-value MVCC rewrites) but used unequal auto-scaling hardware and undisclosed versions — and update-heavy is irrelevant to append-only audit logs.
- Key porting pitfalls: Mongo {field: null} matches both null and missing while Postgres distinguishes JSON null / SQL NULL / absent key; jsonb drops duplicate keys, key order, and rewrites numerics (breaks naive round-trip diffing since BSON field order is semantic, e.g. $addToSet dedup is field-order-sensitive and unordered); BSON dates are ms vs PG µs; Mongo server cursors (10-min idle timeout, unordered without sort) should become keyset pagination on (timestamp, id); ObjectIds embed creation time — UUIDv7 (native uuidv7() in PostgreSQL 18) preserves the time-ordering/B-tree locality that UUIDv4 loses.
- MongoDB, Inc. v. FerretDB Inc. (1:25-cv-00641, D. Del., filed May 23, 2025, after a Nov 2023 C&D) pleads four patents, false advertising and trademark misuse — notably NOT SSPL enforcement, leaving SSPL untested in court; case still active as of Jan 6, 2026 filing; no partner suits found in public dockets. Legal pressure targets Mongo-API emulators/distributors, not plain Postgres/JSONB users.
- GitLab's audit_events was the first table GitLab partitioned (monthly date-range) because audit queries are inherently time-windowed; their playbook: partitioned copy + sync trigger → batched backfill → swap across releases; caveats: PK must include partition key, secondary indexes/FKs not auto-recreated, retention via partition detach only after swap.
- Append-only event stores on PG have one classic consumer bug: sequence IDs are not commit-ordered, so 'read past last seen id' pollers skip events — fix with pg_snapshot_xmin transaction-id watermarks or SHARE ROW EXCLUSIVE locks, plus LISTEN/NOTIFY and idempotent consumers (eugene-khyst reference impl; Marten ships hot/cold archived-event partitioning and quick-append mode).
- Sentry is the boundary condition: Postgres event search died from dead-row churn IO and moved to ClickHouse/Snuba in 2019 — the failure mode is high-cardinality analytical search over events, not append-only ingest with time-range reads, which is the GitLab-shaped (and our) workload.

Sources:

- [The Guardian — Bye bye Mongo, Hello Postgres (2018, via mirror; original blocked from direct fetch)](https://www.theguardian.com/info/2018/nov/30/bye-bye-mongo-hello-postgres) — Canonical Mongo→PG-jsonb migration: outage-driven, API-proxy dual-write mechanism, ~9 months, lessons on NTP, startup index auto-generation, and preferring managed databases.
- [InfoQ — The Guardian's Migration from MongoDB to PostgreSQL on Amazon RDS (2019)](https://www.infoq.com/news/2019/01/guardian-mongodb-postgresql/) — Independent summary confirming motivations (OpsManager, vendor support), jsonb choice for field indexing, GoReplay validation, and the failed-integration-tests surprise.
- [Infisical — The Great Migration from MongoDB to PostgreSQL (2024)](https://infisical.com/blog/postgresql-migration-technical) — Recent infra-product migration: LevelDB ObjectId→new-ID mapping, recursive tree structures as the hard part, Knex.js choice, write-freeze-over-downtime tradeoff.
- [Olery — Goodbye MongoDB, Hello PostgreSQL (2015, mirror)](https://sudonull.com/post/100407-Goodbye-MongoDB-Hello-PostgreSQL) — Classic case: schemaless drift as the core pain; staged migration (critical data, parallel run, delta copy); no regrets post-move.
- [Pulp project — Task #1803: Plan replacement of mongodb with postgres](https://pulp.plan.io/issues/1803) — Red Hat's Pulp 3 formally planned and executed dropping MongoDB for PostgreSQL, with a full 2to3 migration plugin ecosystem.
- [Medium (J. Holt) — I Migrated 847 Million Records from MongoDB to PostgreSQL](https://medium.com/@jholt1055/i-migrated-847-million-records-from-mongodb-to-postgresql-heres-what-i-learned-about-web-scale-84ceeceb87ab) — Unverifiable personal anecdote (flagged low-credibility) but illustrates recurring patterns: schema drift discovered at migration time, COPY-based bulk load, staged dual-write cutover.
- [The Register — Microsoft builds open source document database on PostgreSQL (Jan 27, 2025)](https://www.theregister.com/2025/01/27/microsoft_builds_open_source_document/) — DocumentDB = pg_documentdb_core (BSON type) + pg_documentdb_api, MIT-licensed, 'no gimmicks' positioning; FerretDB 2.0 as the Mongo-compatible front end.
- [InfoQ — FerretDB, an Open-Source Alternative to MongoDB, Releases Version 2.0 (Feb 2025)](https://www.infoq.com/news/2025/02/ferretdb-documentdb/) — FerretDB 2.x swapped its 1.x JSONB translation layer for Microsoft's MIT DocumentDB BSON extensions, claiming large performance gains.
- [FerretDB Blog — FerretDB Releases 2.0 (vendor)](https://blog.ferretdb.io/ferretdb-releases-v2-faster-more-compatible-mongodb-alternative/) — Vendor claim of 'more than 20x faster' on certain workloads with no methodology or baseline; Apache 2.0; PG+DocumentDB became the only backend.
- [Linuxiac — FerretDB 2.0 Open-Source Document Database Goes GA](https://linuxiac.com/ferretdb-2-0-open-source-document-database-goes-ga/) — GA confirmation and framing of FerretDB 2.0 as MongoDB drop-in on Postgres after year-long Microsoft collaboration.
- [The Register — Linux Foundation says yes to NoSQL via DocumentDB (Aug 25, 2025)](https://www.theregister.com/2025/08/25/linux_foundation_says_yes_to/) — Microsoft donated DocumentDB to the Linux Foundation under MIT at Open Source Summit Europe; gateway layer makes it MongoDB-compatible standalone.
- [VentureBeat — AWS, Microsoft and Google unite behind Linux Foundation DocumentDB](https://venturebeat.com/data-infrastructure/aws-microsoft-and-google-unite-behind-linux-foundation-documentdb-database-to-cut-enterprise-costs-and-limit-vendor-lock-in) — Hyperscaler coalition (plus Cockroach, Crunchy, Supabase, Yugabyte) framed as first vendor-neutral open-source MongoDB alternative, explicitly contrasted with SSPL.
- [AWS Open Source Blog — AWS joins the DocumentDB project](https://aws.amazon.com/blogs/opensource/aws-joins-the-documentdb-project-to-build-interoperable-open-source-document-database-technology/) — AWS's own confirmation of joining the LF DocumentDB project for interoperable open document database tech.
- [RedMonk (Stephen O'Grady) — DocumentDB and the Future of Open Source (Sep 2025)](https://redmonk.com/sogrady/2025/09/02/documentdb/) — Analyst take: post-Google-v-Oracle API copyright is gone; MongoDB's exclusivity strategy is precarious; MIT/multi-vendor Postgres document store mirrors how Postgres won relational; litigation may be tactical.
- [EDB — Performance Benchmark: PostgreSQL vs MongoDB (2019, sponsored)](https://www.enterprisedb.com/performance-benchmark-postgresql-vs-mongodb) — EDB-sponsored OnGres benchmark: PG 4–15x faster on transactions plus OLAP-on-JSON wins; sponsorship bias flagged.
- [OnGres — Benchmarking: Do it with transparency or don't do it at all](https://ongres.com/blog/benchmarking-do-it-with-transparency/) — OnGres's defense: 50-page whitepaper, open-sourced code and raw results; MongoDB's rebuttal claims (experimental drivers, tuning disparity, 240x counter-claims) made without reproducible methodology.
- [Hacker News — MongoDB Responds to PostgreSQL Benchmarks (2019)](https://news.ycombinator.com/item?id=20458702) — Neutral expert assessment of both sides: OnGres reproducible but imperfect; MongoDB's response 'only words and a single table' without code.
- [Yuriy Ivon (Medium) — Can PostgreSQL with its JSONB column type replace MongoDB? (2023)](https://medium.com/@yurexus/can-postgresql-with-its-jsonb-column-type-replace-mongodb-30dc7feffaf3) — Best independent rerun: Mongo 6.0.3 substantially faster on parallel inserts and ~3x smaller storage; PG composite B-trees win range+order reads; advice: real columns first, JSONB as fallback; updates untested.
- [MongoDB Blog — Evaluation of Update-Heavy Workloads With PostgreSQL JSONB and MongoDB BSON (Jan 2026, vendor)](https://www.mongodb.com/company/blog/technical/evaluation-update-heavy-workloads-postgresql-jsonb-and-mongodb-bson) — Vendor benchmark showing JSONB degradation under update-heavy load via non-HOT MVCC rewrites/WAL/dead tuples; undisclosed versions and MongoDB-only auto-scaling hardware undermine it; irrelevant to append-only logs.
- [TechBytes — PostgreSQL 17 JSON vs MongoDB: Benchmark Reality Check](https://techbytes.app/posts/postgresql-17-json-vs-mongodb-benchmark-reality-check/) — Third-party dissection of the update-heavy benchmark: unequal hardware, no index-tuning analysis, measures one architectural edge case, 'does not establish superiority for reads, analytics, joins, or mixed workloads'.
- [PostgreSQL Docs — 8.14 JSON Types (datatype-json)](https://www.postgresql.org/docs/current/datatype-json.html) — Primary source for jsonb normalization: duplicate keys last-wins, key order not preserved, whitespace dropped, numbers mapped to numeric (E-notation rewritten); jsonb slower to ingest but reparse-free and indexable.
- [OneUptime — How to Migrate from MongoDB to PostgreSQL (2026 guide)](https://oneuptime.com/blog/post/2026-03-31-mongodb-migrate-mongodb-to-postgresql/view) — Practical checklist: ObjectId→UUID via hex padding, normalized-vs-JSONB decision rule, TIMESTAMPTZ for dates, validate counts + run test suite against PG before cutover.
- [Neon — PostgreSQL 18 UUIDv7 Support](https://neon.com/postgresql/18/uuidv7-support) — PG 18's native uuidv7() (RFC 9562) gives time-ordered IDs with B-tree insert locality — the natural replacement for ObjectId's embedded-timestamp ordering, vs UUIDv4's random-insert fragmentation.
- [clarkdave.net — Null-safety with JSON and PostgreSQL](https://clarkdave.net/2015/03/navigating-null-safety-with-json-and-postgresql/) — The JSON-null vs SQL-NULL vs missing-key trichotomy and extraction-operator gotchas that diverge from Mongo's {field: null} matching both null and absent.
- [mbork.pl — PostgreSQL and null values in jsonb (2020)](https://mbork.pl/2020-02-15_PostgreSQL_and_null_values_in_jsonb) — Worked examples of distinguishing explicit JSON null from missing keys in jsonb queries (existence operator + 'null'::jsonb comparisons).
- [MongoDB Docs — $addToSet](https://www.mongodb.com/docs/manual/reference/operator/update/addToSet/) — Primary source: $addToSet gives no ordering guarantee, duplicate detection for documents is exact-match including field order, bare arrays append as one nested element without $each.
- [MongoDB Docs — Cursors](https://www.mongodb.com/docs/manual/core/cursors/) — Server-side cursor semantics to replace: 10-minute default idle timeout, getMore batching, no result-order guarantee without explicit sort — map to keyset pagination in PG.
- [google/go-cloud issue #1684 — mongo represents time in milliseconds](https://github.com/google/go-cloud/issues/1684) — Concrete example of the BSON ms vs µs precision pitfall: Go's BSON codec truncates time.Time to milliseconds, breaking round-trips with µs-precision systems like Postgres.
- [GitLab Docs — Date range partitioning (development/database)](https://docs.gitlab.com/development/database/partitioning/date_range/) — audit_events as the worked example: monthly range partitions because audit reads are time-windowed; multi-release copy/sync-trigger/backfill/swap process; PK-must-include-partition-key and index/FK recreation caveats.
- [GitLab issue #241267 — Swap base audit_events table with partitioned copy](https://gitlab.com/gitlab-org/gitlab/-/issues/241267) — Production artifact of the audit_events partition swap at GitLab.com scale — evidence this pattern shipped, not just design docs.
- [Marten — Optimizing for Performance and Scalability (event store)](https://martendb.io/events/optimizing.html) — Production event-store-on-PG guidance: hot/cold partitioning of archived events, quick-append mode tradeoffs, stream compaction, per-tenant partitioning, (type, seq_id) index.
- [eugene-khyst/postgresql-event-sourcing (reference implementation)](https://github.com/eugene-khyst/postgresql-event-sourcing) — Documents the sequence-gap commit-visibility anomaly for append-only consumers and fixes (pg_snapshot_xmin watermarks, SHARE ROW EXCLUSIVE locks), LISTEN/NOTIFY vs polling, idempotency, long-transaction stalls.
- [Eventide Blog — Announcing Message DB: Event Store and Message Store for PostgreSQL](https://blog.eventide-project.org/articles/announcing-message-db/) — Minimal Postgres-native event/message store positioned explicitly for teams avoiding Kafka/EventStore-scale operational overhead.
- [Sentry Blog — Introducing Snuba: Sentry's New Search Infrastructure](https://blog.sentry.io/introducing-snuba-sentrys-new-search-infrastructure/) — The boundary condition: Postgres event search died from dead-row churn IO ('IO was wasted on combing over dead rows'); ClickHouse columnar storage shrank terabytes to gigabytes — the failure mode is analytical search, not append-only ingest.
- [Justia Dockets — MongoDB, Inc. v. FerretDB Inc., 1:25-cv-00641 (D. Del.)](https://dockets.justia.com/docket/delaware/dedce/1:2025cv00641/89247) — Filed May 23, 2025: four patents (aggregation pipelines, write reliability), false compatibility claims, trademark misuse — notably not an SSPL claim.
- [Law.com Radar — MongoDB, Inc. v. FerretDB Inc.](https://www.law.com/radar/card/pm-58201415-mongodb-inc-v-ferretdb-inc) — Case remained active with last known filing Jan 6, 2026; no public settlement or dismissal found as of June 2026.
- [Blocks & Files — MongoDB cease-and-desist letter to FerretDB (Nov 3, 2023, PDF)](https://blocksandfiles.com/wp-content/uploads/2025/04/Letter-from-MongoDB-to-FerretDB_3-Nov-2023-signed.pdf) — The pre-suit pressure mechanism: a 2023 C&D over licensing/trademark/IP, published April 2025; FerretDB denied using any MongoDB code.
- [Chris Mellor (Substack) — MongoDB, FerretDB clash over open source definition](https://chrismellor.substack.com/p/mongodb-ferretdb-clash-over-open) — Context: OSI ruled SSPL not open source (2021); FerretDB founded by ex-Percona people in reaction to SSPL; before this case no supplier had obtained a court judgment on SSPL claims.
- [MongoDB Blog — Building for Developers—Not Imitators](https://www.mongodb.com/company/blog/building-for-developers-not-imitators) — MongoDB's public framing of the FerretDB suit as protecting innovation from 'imitators' — the vendor narrative side of the licensing fight.

---

## 8. PR plan

Seven PRs, one per roadmap phase-cluster, every one green behind
`AUDIT_BACKEND=mongodb` (default) until PR 7. Estimate ~15–17 working days
solo (Compose excluded per G6).

**PR 1 — Foundation (P0+P1, ~2d).** Branch `feature/postgres-audit-store` off
`develop`. `migrations/audit/0001_initial.sql` (verbatim from § 3);
`MIGRATIONS_AUDIT_DIR` beside the app/vector constants
(`orchestrator/database/postgres.py:169-170`); audit pool + `run_migrations`
in lifespan (after app+vector at `main.py:3339-3340`);
`orchestrator/services/audit_partitions.py` per § 4 + lifespan task;
`requirements-dev.txt` (`testcontainers[postgres]`) + CI install lines
(`main.yml:244`, `develop.yml:469`) + **add `requirements-dev.txt` to
develop.yml's python-changed path filter (302-305)**; testcontainers fixture +
schema-applies smoke + partition-module unit tests (create/idempotency/
detach-finalize/drop/status).

**PR 2 — Writer (P2, ~2d).** `src/database/audit_writer.py` (`SyncAuditWriter`
per § 5: daemon thread, private loop, asyncpg pool min1/max2,
`statement_cache_size=0`, `synchronous_commit=off`, jsonb codec);
`archiver.py` internals swap behind the flag (public surface byte-identical;
`_serialize_for_mongo` → `_serialize_payload` with datetime→ISO-Z);
`_get_next_step_number` + connect-time `create_index` deleted on the PG path.
Tests: round-trips, two-phase `INSERT...SELECT` semantics (False on missing
pre), thread-safety from executor threads, unconfigured→None gating parity
(keeps unrelated suites DB-free).

**PR 3 — Reader (P3, ~3d).** `orchestrator/database/audit_store.py` per § 5:
12 methods (incl. new `get_audit_counts` + `iter_tool_calls`),
`FILTER_MAPPINGS`/`FilterCategory` ported, `_to_iso_utc` ported, logical-row
stitch + window-then-filter `step_number`, single-query `/version`,
UUID-cast fail-soft. Tests per method, with parity cases for pagination
(`page=-1`), filter renumbering, version counts, conditional-field dropping.

**PR 4 — Wiring (P4, ~3d).** `main.py` ~50 sites via `make_audit_store()`
factory (anchors in § 6/infra; the `ensure_indexes` lifespan call at
`3325-3332` is deleted on the PG path); `graph_routes.py` →
`set_audit_store()` + `iter_tool_calls` + **`require_job_access`**; MCP/builder
ObjectId text (`mcp/server.py:354`, `mcp/client.py:263,:700`,
`builder_tools.py:1072`) + `main.py:14623` docstring + `formatters.py:491/:574`
`_id`→`id`; cockpit: `_id`→`id:number` across the 4 models + `request_id`
types, regex `/^\d+$/`, the two `.slice()` fixes + untyped access, IndexedDB
real version check + cache-miss `clearJob`, spec fixtures. Gate: cockpit
builds + full suite green under both backends.

**PR 5 — Infra (P5, ~1.5d).** Helm: `databases/postgres-audit.yaml` (copy
`postgres-vector.yaml`, component `auditdb`), `databases.audit.*` values
(internal + `externalHost/Port/Db`), ConfigMap trio, orchestrator deployment
5-env block + `wait-for-auditdb`, **NetworkPolicy block (orchestrator + agent
selectors, port 5432)**, `values-local.example.yaml` secrets keys,
`helm/README.md` secret schema; `docker/Dockerfile.orchestrator` +
`postgresql-client`; `init.py`/`orchestrator/init.py` backup/restore →
`pg_dump --jobs`/`pg_restore` (Mongo paths stay alive under the flag).

**PR 6 — Validation (P6, ~2-3d incl. soak).** A/B diff harness
(`AUDIT_BACKEND=postgres` vs `mongodb` on k3d; expected deltas: `id:int`,
ms-precision timestamps, `token_usage` present); perf smoke (200-call job,
±20% gate G3); concurrency smoke (~100 writers); retention drill
(backdated partition → detach → FINALIZE → grace → drop); lookahead alarm
check; cockpit IndexedDB invalidation check. **Vault first**: add
`AUDIT_POSTGRES_USER/PASSWORD` to the dev path before the chart bump (ESO
refresh is 1h; Reloader will bounce the orchestrator once — expected).

**PR 7 — Cutover + cleanup (P7+P8, ~1.5d + 24h soak).** Default flip, wipe,
soak, then the deletion sweep (mongo modules incl.
`MONGODB_INDEX_DECLARATIONS`, `orchestrator/init.py:1576-1593` index
bootstrap, chart mongodb/mongo-express + NetPol block + `helm/ci/test-values.yaml:19-20`
+ ingress + helpers + hostname + cockpit env-init, `motor` from
`orchestrator/requirements.txt`, doc sweep). Tag `audit-postgres-cutover-v1`.
prod-private rolls only after its Vault path has the keys.

---

## 9. Day-1 checklist

1. Add `AUDIT_POSTGRES_USER` / `AUDIT_POSTGRES_PASSWORD` to the dev Vault path
   (decoupled from any deploy; ESO picks them up within the hour).
2. `git checkout develop && git checkout -b feature/postgres-audit-store`.
3. Copy § 3 verbatim → `orchestrator/database/migrations/audit/0001_initial.sql`.
4. `MIGRATIONS_AUDIT_DIR` + lifespan pool/migrations/maintenance-task wiring.
5. `requirements-dev.txt` + CI install lines + develop.yml path filter.
6. Port the validated smoke (§ 3 evidence) as `tests/test_audit_store_smoke.py`
   against the testcontainers fixture.
7. Start `audit_partitions.py` against § 4.

## 10. Acceptance gates (unchanged from roadmap, sharpened)

- **G3 perf**: archiver overhead within 20% of Mongo baseline on a 200-call
  job, plus the 100-writer concurrency smoke (p50/p95 insert latency, bounded
  backend count).
- **A/B byte-parity**: same job under both backends; diffs limited to the
  approved list (id type, ms timestamps, `token_usage`, the version-hash
  internals).
- **Retention drill** passes on k3d (detach → finalize-recovery → drop).
- 24h soak on `AUDIT_BACKEND=postgres` before P8 deletions.
