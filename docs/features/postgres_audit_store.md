# Replace MongoDB Audit Store with PostgreSQL

## Status: Proposed (revised 2026-05-02 after codebase + web research pass)

## Problem

MongoDB is used for exactly one job in this stack: archiving the agent's
runtime trace (audit events, LLM request/response pairs, conversational
chat history). It carries disproportionate operational and licensing cost
for what amounts to "append-mostly JSON logs queried by `job_id` and
time".

Specifically:

1. **License risk for commercial distribution**. MongoDB's SSPL is a real
   thorn for buyers shipping a managed offering and for resellers — AWS,
   Elastic, Redis, and others have all forked or rebuilt around it.
   PostgreSQL's license is BSD-style and unencumbered. If we want to sell
   this, "no Mongo" is a meaningful checkbox. **The case has hardened in
   2025**: MongoDB sued FerretDB in May 2025 (Delaware D. Ct.
   1:25-cv-00641, patent + trademark) and went after FerretDB's *partners*
   (SAP, Vultr, Scaleway) too. Microsoft + AWS launched the MIT-licensed
   DocumentDB Postgres extension in January 2025, which now backs FerretDB
   2.0 — the industry is actively neutralising MongoDB's wire-protocol
   moat. SSPL pressure on vendors and their distribution chains is no
   longer theoretical.
2. **Issue C** in `docs/issues/deployment_separation_of_concerns.md`:
   the homelab MongoDB runs without authentication. Replacing it with
   Postgres makes the auth problem go away by default — Postgres ships
   with auth on, ESO already provisions credentials for the existing
   Postgres instances.
3. **Operational duplication**. We already run two Postgres flavours
   (`srw-postgres`, `srw-pgvector`, soon `srw-keycloakdb`). A third
   Postgres for audit is the same operator playbook the team already
   knows: backups, monitoring, schema changes, ESO secret wiring. Mongo
   needs its own.
4. **Cross-store joins are awkward**. "Show me the audit trail for jobs
   that failed in the last 24h" requires correlating `jobs` (Postgres)
   with `agent_audit` / `llm_requests` (Mongo) at the application layer.
   In a unified store this is a single SQL join.
5. **Footprint**. The MongoDB layer is **two** classes, not one:
   `src/database/mongo_db.py` (338 LoC, sync, write-side) **and**
   `orchestrator/database/mongodb.py` (923 LoC, async motor, owns the
   cockpit-facing read API), plus `src/core/archiver.py` (1131 LoC).
   A Postgres-native adapter consolidates this into one async surface
   reusing the existing `PostgresDB` connection pool, retry semantics,
   and metrics — but realistic adapter LoC is closer to ~1500 than the
   ~600 originally estimated.

The use case is genuinely a fit for Postgres + JSONB:
- All writes are inserts. The current Mongo code uses a two-phase pattern
  on `agent_audit` (insert with NULL response, UPDATE shortly after).
  **This design replaces that with a second INSERT** to keep the table
  append-only and dodge the JSONB-UPDATE write-amplification class — see
  Design § "agent_audit append-only" below.
- Queries are: by `job_id` (always indexed), by time range, by
  `call_type` / `step_type`, with simple aggregations (`SUM`, `AVG`,
  `COUNT`, `GROUP BY`).
- No geospatial queries, no full-text search across documents, no
  sharded writes, no `$lookup`-heavy pipelines.

## Scope

### In scope (this feature)

The three internal collections that the agent and orchestrator write to:

| Mongo collection | Owner             | Writes per job | Purpose                                              |
|------------------|-------------------|----------------|------------------------------------------------------|
| `agent_audit`    | `LLMArchiver`     | High (per call) | Tool-call / decision trace, two-phase (pre/post)    |
| `llm_requests`   | `LLMArchiver`     | High (per call) | Full request + response + cost metrics              |
| `chat_history`   | `LLMArchiver`     | Medium         | Clean conversational delta per call                  |

These are read by the cockpit and by the following orchestrator
endpoints (full list, not just the two named in the original draft):

- `GET /api/jobs/{id}/audit` — paginated audit (offset/limit, page/pageSize, order, FilterCategory)
- `GET /api/requests/{doc_id}` — single LLM request by id (currently 24-hex ObjectId)
- `GET /api/jobs/{id}/audit/timerange` — first/last step timestamp
- `GET /api/jobs/{id}/chat` — paginated chat history
- `GET /api/jobs/{id}/audit/bulk` — bulk fetch (max 5000) for IndexedDB sync
- `GET /api/jobs/{id}/chat/bulk` — same shape for chat
- `GET /api/jobs/{id}/graph/bulk` — graph-delta tool calls
- `GET /api/jobs/{id}/version` — `{auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate}` cache-invalidation hash
- `GET /api/jobs/{id}/llm-requests` — paginated LLM request summaries
- `GET /api/graph/changes/{id}` (in `orchestrator/graph_routes.py`, reaches into `mongodb._db["agent_audit"]` directly)

Plus three indirect consumers that enrich job rows with `audit_count`
via per-job `count_documents` calls (N+1):
`/api/jobs` (`main.py:3261-3287`), `/api/jobs/{id}` (`main.py:3313-3317`),
`/api/projects/{id}/jobs` (`main.py:14422-14427`). The Postgres swap is
an opportunity to collapse these into a single `SELECT ... GROUP BY job_id`.

All reads are by `job_id` ± time range / step_type / tool name — no
schema-flexible queries that would justify a document store.

### Out of scope (stays on Mongo, or is independent)

- **`src/tools/mongodb/`** — these are tools the *agent* uses to query
  *customer-attached MongoDB datasources*. A user pointing the agent at
  their own production MongoDB still needs Mongo client libraries and
  the existing tool surface. This is unaffected by removing the
  internal audit Mongo. The `workspace-network-policy.yaml` egress to
  port 27017 must therefore stay (workspace shells need `mongosh`).
- **`mongoExpress` chart toggle** — the dev UI for the audit Mongo. Drop
  it (along with the audit Mongo, the `srw.mongoHost` helper, the
  `global.hostnames.mongo` value, the `ingress.yaml:278-315` mongo-express
  block, and the cockpit env-init `mongoExpressUrl` at
  `cockpit/deployment.yaml:17`) once cutover is complete. Replace with
  manual pgadmin registration of the new instance — the chart's
  `optional/pgadmin.yaml` only pre-populates `srw-postgres`, so adding
  `auditdb` there is optional polish, not a blocker.
- **NATS**, **Neo4j** — different stores, different problems, untouched.

## Design

### Where the data lives

A new dedicated PostgreSQL StatefulSet `srw-auditdb`, **not** co-tenant
with `srw-postgres`. Same isolation logic as Issue A's Keycloak split:
the audit trail's whole job is to survive when other things go wrong,
and a busy-loop on the audit table should not contend with orchestrator
latency-sensitive queries.

In Compose: a new `postgres-audit` service, mirroring the
`postgres-keycloak` pattern (which exists in all three compose files
already — verified). In Helm: a new
`databases.audit.{enabled,internal,externalUrl,...}` block and a new
`helm/templates/databases/postgres-audit.yaml` template, copied from
`postgres-keycloak.yaml`.

Component label: `auditdb` (7 chars — fits the 52-char StatefulSet name
budget with room to spare).

### Schema

Three tables, one per current collection. JSONB for the variable-shape
payloads, hard columns for the fields we actually filter and group by.
Schemas widened from the original draft to capture every field the
existing writer emits (verified against `src/core/archiver.py:328-726`
and `:609-626`).

```sql
-- llm_requests: one row per LLM call (main loop + auxiliary).
CREATE TABLE llm_requests (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL,
    agent_type      TEXT,                              -- writer always sets this
    agent_id        UUID,
    call_type       TEXT NOT NULL DEFAULT 'main',     -- main | summarization | memory_extraction | ...
    model           TEXT NOT NULL,
    iteration       INTEGER,                           -- used by audit-join logic
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latency_ms      INTEGER,
    request         JSONB NOT NULL,
    response        JSONB,
    metadata        JSONB,                             -- writer's free-form metadata dict
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- input_chars, output_chars, tool_calls, token_usage
    auxiliary_meta  JSONB                              -- task_class, trigger, iteration, file_name, ...
) PARTITION BY RANGE (timestamp);

-- Append-heavy tuning, applied to every partition via template.
ALTER TABLE llm_requests SET (
    fillfactor = 100,
    autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_vacuum_insert_threshold = 10000,
    autovacuum_analyze_scale_factor = 0.02,
    vacuum_freeze_min_age = 0
);
ALTER TABLE llm_requests ALTER COLUMN request  SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE llm_requests ALTER COLUMN metadata SET COMPRESSION lz4;

CREATE INDEX llm_requests_job_id_idx       ON llm_requests (job_id, timestamp);
CREATE INDEX llm_requests_call_type_idx    ON llm_requests (call_type, timestamp);
CREATE INDEX llm_requests_metrics_gin      ON llm_requests USING GIN (metrics jsonb_path_ops);

-- agent_audit: APPEND-ONLY trace. The previous two-phase UPDATE pattern
-- is replaced with a second INSERT carrying phase='post'. See
-- "agent_audit append-only" below for rationale.
CREATE TABLE agent_audit (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT REFERENCES llm_requests(id) ON DELETE CASCADE,
    job_id          UUID NOT NULL,
    agent_type      TEXT,
    iteration       INTEGER,
    node_name       TEXT,                              -- LangGraph node that emitted the step
    step_type       TEXT NOT NULL,                     -- llm|tool|check|initialize|warning|error|
                                                       -- phase_transition|phase_complete|feedback_resume|
                                                       -- memory_inject|memory_dedup|memory_store|memory_retrieve
    phase           TEXT,
    phase_number    INTEGER,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latency_ms      INTEGER,
    event_phase     TEXT NOT NULL DEFAULT 'pre',       -- 'pre' or 'post' (replaces the UPDATE)
    payload         JSONB NOT NULL,                    -- merged data dict from the writer
    metadata        JSONB
) PARTITION BY RANGE (timestamp);

ALTER TABLE agent_audit SET (
    fillfactor = 100,
    autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_vacuum_insert_threshold = 10000,
    autovacuum_analyze_scale_factor = 0.02,
    vacuum_freeze_min_age = 0
);
ALTER TABLE agent_audit ALTER COLUMN payload  SET COMPRESSION lz4;
ALTER TABLE agent_audit ALTER COLUMN metadata SET COMPRESSION lz4;

CREATE INDEX agent_audit_job_id_idx     ON agent_audit (job_id, timestamp);
CREATE INDEX agent_audit_job_type_idx   ON agent_audit (job_id, step_type, timestamp);
CREATE INDEX agent_audit_request_idx    ON agent_audit (request_id);
-- Expression index for the graph-delta tool-name filter (graph_routes.py:152).
CREATE INDEX agent_audit_tool_name_idx
    ON agent_audit ((payload -> 'tool' ->> 'name'))
    WHERE step_type = 'tool';

-- DO NOT add a GIN index on agent_audit.payload. It would re-introduce the
-- HOT-defeating JSONB-UPDATE bloat the append-only design avoids — and would
-- make it expensive to ever revert to a two-phase UPDATE shape.

-- chat_history: one row per turn. Shape preserved from the writer's
-- existing emission (one row carries multiple inputs and one response).
CREATE TABLE chat_history (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT REFERENCES llm_requests(id) ON DELETE CASCADE,
    job_id          UUID NOT NULL,
    agent_type      TEXT,
    iteration       INTEGER,
    model           TEXT,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latency_ms      INTEGER,
    phase           TEXT,
    phase_number    INTEGER,
    inputs          JSONB NOT NULL,                    -- list of {type, content, content_preview, tool_call_id?, tool_name?}
    response        JSONB NOT NULL,                    -- {content, content_preview, has_tool_calls, tool_calls?}
    reasoning       JSONB
) PARTITION BY RANGE (timestamp);

ALTER TABLE chat_history SET (
    fillfactor = 100,
    autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_vacuum_insert_threshold = 10000,
    autovacuum_analyze_scale_factor = 0.02,
    vacuum_freeze_min_age = 0
);
ALTER TABLE chat_history ALTER COLUMN inputs   SET COMPRESSION lz4;
ALTER TABLE chat_history ALTER COLUMN response SET COMPRESSION lz4;

CREATE INDEX chat_history_job_idx    ON chat_history (job_id, timestamp);
CREATE INDEX chat_history_request_id ON chat_history (request_id);
```

#### `agent_audit` append-only — rationale

The Mongo writer creates an `agent_audit` row at call dispatch with
`tool.result_*` / `llm.response_*` fields null, then `update_one`s it
with the result/response when the call returns
(`archiver.py:842, :955`). Naively translated to Postgres this becomes
an INSERT + UPDATE per call. **That triggers JSONB write
amplification**: per Adyen and the MongoDB-team writeup on dev.to,
JSONB UPDATEs in the presence of any expression / GIN index defeat HOT
updates, with measured WAL going from 71 B (no index) → 397 B (with
GIN), ~5.5× per UPDATE. We already have an expression index on
`payload->'tool'->>'name'` for the graph-delta query — that alone
defeats HOT.

Replacing the UPDATE with a second INSERT (`event_phase='post'`,
matching `request_id`) keeps the table strictly append-only, fits the
event-sourcing pattern, and lets `fillfactor=100` stand. Reads stitch
the pair with a `JOIN` or `DISTINCT ON (request_id) ... ORDER BY id
DESC` for "latest phase". The cockpit's audit pane already paginates by
step ordering, so the doubled row count is absorbed by the
`(job_id, timestamp)` index.

Trade-off: ~2× row count on `agent_audit`. Storage stays small (TOAST
+ LZ4 keeps payloads compact); the `id` BIGSERIAL is 8 bytes per row.
Acceptable.

#### Per-step ordering — no `seq` column

The original draft proposed an `audit_sequence` table or per-job
advisory lock + `MAX(seq)` to assign monotonic per-job seq numbers,
mirroring the Mongo writer's `_get_next_step_number`
(`archiver.py:235`). **That column is removed.** Use the global
`BIGSERIAL id` for ordering and compute per-job seq at read time:

```sql
SELECT id, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id) AS seq, ...
  FROM agent_audit
 WHERE job_id = $1
 ORDER BY id;
```

Reads always paginate by job, so the window is small. This eliminates
the only contended write path in the design. (Note: today's Mongo
writer uses an in-process `dict[job_id] += 1` counter, which is already
race-prone under `asyncio.gather` from the auxiliary archiver. The PG
version with monotonic IDs is *stricter* than what we have today, not
weaker.)

#### Partitioning

Monthly range partitions on `timestamp` for all three tables. Drop
old partitions on a retention schedule (90 days for `llm_requests` and
`agent_audit`; 365 days for `chat_history`). **No default partition** —
attaching a new partition while a default partition has rows requires
`ACCESS EXCLUSIVE` and stalls writes; pre-create N+2 months ahead and
alarm if the next partition is missing.

**Implementation choice — `pg_partman` is preferred** for production
workloads. The original draft leaned toward hand-rolled (~30 LoC) for
extension-freedom. Real-world reports (Crunchy, Percona, Hatchet's
post-mortem, the pg_partman issue tracker itself) show hand-rolled
needs ~60 LoC realistically — advisory-locked creation, N+2 month
lookahead, parent `ANALYZE` (autovacuum does *not* analyze partitioned
parents — Hatchet's exact production bug), `DETACH PARTITION
CONCURRENTLY` + `DROP TABLE` retention with a 7-day buffer, and a
"days until next partition needed" metric. If the team accepts the
extension, take pg_partman + jobmon and only own the parent ANALYZE.

#### Indexing

The Mongo indexes today are: `(job_id, step_number)` on `agent_audit`,
`(job_id, timestamp)` on `llm_requests` and `chat_history`. We mirror
with btree on `(job_id, timestamp)` for all three plus a covering
`(job_id, step_type, timestamp)` on `agent_audit` (heavily used filter
combo) and the expression index on `payload->'tool'->>'name'` for graph
deltas. The metrics GIN index is new but cheap and lets the cockpit's
"expensive jobs" queries use containment lookups.

### Adapter layer

New module `orchestrator/database/audit_store.py` (the production
asyncpg adapter, ~1200 LoC including the read API previously in
`orchestrator/database/mongodb.py`) and a small re-export shim under
`src/database/` for the sync archiver write path. Together the two
sides expose the union of:

**Write surface** (mirrors `src/database/mongo_db.py` + `LLMArchiver`):
- `connect()` / `disconnect()` / `is_available`
- `archive(...)` → INSERT into `llm_requests`, return BIGINT id
- `audit_step(...)` / `audit_llm_call(...)` / `audit_tool_call(...)` →
  INSERT into `agent_audit` with `event_phase='pre'`, return id
- `update_llm_response(audit_id, response)` /
  `update_tool_result(audit_id, result, ...)` → INSERT a *second* row
  with `event_phase='post'` and matching `request_id` (signature stays
  the same so callers in `graph.py` don't change)

**Read surface** (mirrors `orchestrator/database/mongodb.py`):
- `get_job_audit(job_id, limit, offset, order, filter)` — paginated
- `get_job_audit_bulk(job_id, limit, offset)`
- `get_chat_history(job_id, limit, offset)` and `_bulk` variant
- `get_graph_deltas_bulk(job_id, limit, offset)`
- `get_request(id)` — by BIGINT id (was 24-hex ObjectId)
- `get_audit_time_range(job_id)` — first/last timestamp
- `get_job_version(job_id)` — `{auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate}`
- `list_llm_requests(job_id, limit, offset)`
- `get_audit_count(job_id)` — single-row helper used by enrichers
- `iter_tool_calls(job_id)` — replaces the
  `mongodb._db["agent_audit"]` raw-cursor leak in
  `graph_routes.py:142`. Either expose this method or refactor the
  helper to use `get_graph_deltas_bulk`.
- `get_job_stats(job_id)` — aggregation SQL
- `get_audit_stats(job_id)` — second aggregation
- `db` property and `_db["..."]` collection accessor are **dropped** —
  graph_routes is the only direct consumer and gets `iter_tool_calls`
  instead.

**Connection pooling**: instantiate `PostgresDB` *twice* — one
transactional pool for the orchestrator's existing workloads, one
throughput-tuned pool for audit writes (smaller per-conn cost, larger
pool size, longer statement timeout). Decouples bloat and lets the
audit DSN point at `srw-auditdb` while the main DSN stays on
`srw-postgres`. ~10 lines of config, big operational win.

### Cockpit / API

The existing endpoints can keep their wire shape unchanged — they
already serialize Mongo documents to JSON. The adapter returns
dict-shaped rows that match the existing response schema.

**Breaking changes** the original draft underspecified:
- The `_id: string` ObjectId becomes `id: number` (BIGINT). The cockpit
  type tweak is in `audit.model.ts:65`, `chat.model.ts:50`,
  `request.model.ts:72`, plus `cache.model.ts:30`. These compile cleanly
  (template interpolation, `Set<number>`, Map keys all work with
  numbers).
- **Hard break**: `cockpit/src/app/debug/services/request.service.ts:60`
  validates IDs with `/^[a-fA-F0-9]{24}$/.test(docId)`. Must change to
  `/^\d+$/` (or be removed).
- **Cache invalidation**: `cache.model.ts` `version: number` field must
  bump so existing IndexedDB entries with string IDs are discarded
  cleanly post-cutover.
- **MCP server tool schemas**: `orchestrator/mcp/server.py` (lines 246,
  270, 348, 1476, 1502, 1605) and `orchestrator/mcp/client.py` (153,
  260, 558, 697, 1448) reference "MongoDB ObjectId (24 hex characters)"
  in tool descriptions. Update to integer.
- **Builder dispatch / tools** (`orchestrator/services/builder_dispatch.py`,
  `builder_tools.py`, `formatters.py`) bake the same ObjectId
  assumption into AI-builder tool schemas. Update.
- **Orchestrator route param** `/api/requests/{doc_id}` typed
  `doc_id: str` in `main.py:7461` — change to `int`.

The wire field name `_id` *could* be kept for compatibility, but it's
misleading once the value is a BIGINT. Recommend renaming to `id` in
the responses and updating the cockpit accordingly — same blast radius
as the type flip.

## Migration

User has confirmed the test deployment can be wiped, so this is a
clean cutover, not a rolling migration.

**Cutover plan**:

1. Land the new `audit_store.py` adapter behind a feature flag
   `AUDIT_BACKEND=postgres|mongodb` (env var, default `mongodb` until
   we flip the switch).
2. Land the helm chart additions (`databases.audit.*`,
   `postgres-audit.yaml`, `AUDIT_DB_URL` + `AUDIT_DB_PASSWORD` Secret
   keys).
3. Land the compose additions (`postgres-audit` service +
   `postgres_audit_data` volume, mirrored across the three compose
   files; `wait-for-auditdb` initContainer in orchestrator deployment).
4. Land the cockpit type updates and the `request.service.ts` regex
   fix; bump `cache.model.ts` version.
5. Stand up the new instance in dev, point `AUDIT_BACKEND=postgres`,
   run a job end-to-end, verify cockpit shows the audit trail and the
   IndexedDB sync (bulk + version endpoints) still works.
6. Flip the default to `postgres`. Drop `mongoExpress`,
   `databases.mongodb.*`, `srw.mongoHost`, `global.hostnames.mongo`,
   `src/database/mongo_db.py`, `orchestrator/database/mongodb.py`,
   `LLMArchiver`'s Mongo-specific code paths, and the
   `mongo_to_pg_audit` adapter shim.
7. Update `init.py` + `orchestrator/init.py` — replace
   `mongodump`/`mongorestore` shell-out with `pg_dump --jobs=N`/
   `pg_restore` (partitioned tables parallelize natively), drop
   `--skip-mongodb` CLI flag, rename `MONGODB_URL` references.
8. Wipe the test cluster, redeploy, sanity-check.

**No data migration**. If we ever need it, mongoexport + a Python
loader script translating each document to its row shape is ~100 LoC
and one-shot. Not worth pre-building.

**Backwards compatibility for at-rest data**: none required since the
test deployment is wiped. The user-facing `tools/mongodb/` for
attached customer datasources is independent of the internal store
and stays as-is.

## Tradeoffs

**What we gain**:
- One less database server to operate, monitor, back up, and patch.
- One less binary in the supply chain (helps the SSPL story for
  commercial distribution — see Problem § 1 update).
- Issue C (Mongo unauthenticated) becomes moot.
- Cross-store joins (`jobs` ⨝ `llm_requests`) are now plain SQL.
- Time-based retention via partition drops is cleaner than Mongo TTL
  indexes (no per-document overhead). Partitioned `pg_dump` parallelizes
  per-table-segment, dropping restore time materially.
- N+1 `audit_count` enrichment can be collapsed to a grouped query.
- Append-only `agent_audit` design eliminates JSONB-UPDATE write
  amplification entirely — cleaner than what the Mongo two-phase pattern
  costs us in WAL today.
- Strictly monotonic per-job ordering (BIGSERIAL > today's racy
  in-process `dict[job_id] += 1`).

**What we lose / what we pay**:
- Aggregation pipelines are simpler in Mongo's syntax than equivalent
  SQL, but the four pipelines we actually use (`archiver.py:457, :482,
  :1018, :1046`) are shallow `$match` / `$group` and translate
  directly. ~50 LoC of SQL total.
- Schema-on-read flexibility for new event types: gone. Adding a new
  `call_type` or a new `step_type` requires no schema change (it's
  just a string column), but adding new structured fields means
  evolving the JSONB payload schema. For an audit trail this is
  arguably a feature — schemas should be deliberate.
- Append-heavy table on Postgres needs autovacuum tuning. The schema
  DDL above sets the per-table knobs explicitly so this is
  set-once-and-forget rather than an ongoing concern. Partitioned
  parents still need a periodic explicit `ANALYZE` (autovacuum doesn't
  touch them).
- MongoDB's wire protocol streams documents efficiently; Postgres
  cursors over JSONB rows are slightly more memory-hungry on the
  client. For our query sizes (per-job pages of 100–1000 rows) this is
  imperceptible. For bulk endpoints (max 5000 rows) we may want to
  switch to `asyncpg.copy_records_to_table`-style binary fetch.
- ~2× row count on `agent_audit` from the append-only design. TOAST +
  LZ4 keeps payload bytes small; the BIGSERIAL is 8 bytes/row.
- Operational rule: **do not add a GIN index on `agent_audit.payload`**.
  It would re-introduce the JSONB-UPDATE bloat the append-only design
  avoids, and would make any future revert to a UPDATE pattern
  expensive.

## File-by-file change list

### New

- `orchestrator/database/audit_store.py` — async asyncpg audit adapter
  (writer + reader surface), ~1200 LoC.
- `orchestrator/database/audit_schema.sql` — DDL with idempotent
  `CREATE TABLE IF NOT EXISTS`, per-table autovacuum settings, LZ4
  compression, partition bootstrap (or `pg_partman` setup), expression
  index for tool-name lookups. Applied by `orchestrator/init.py`.
- `helm/templates/databases/postgres-audit.yaml` — StatefulSet, PVC,
  Service. Copy of `postgres-keycloak.yaml`.
- `requirements-dev.txt` — pin `testcontainers[postgres]` (the project
  has no dev-deps file today; tests run on bare pytest+pytest-asyncio
  in CI with no DB). Add to CI install line in `.github/workflows/
  develop.yml:370` and `main.yml:173`.
- `tests/_audit_db_fixture.py` — session-scoped `PostgresContainer`,
  function-scoped `TRUNCATE` reset. Mirrors the `_fs_backend.py`
  "test-only, never importable from src/" discipline.
- `tests/test_audit_store.py` — greenfield (~400-600 LoC across ~20
  tests). The original draft's "port the existing archiver tests"
  is wrong: `tests/test_archiver.py` does not exist and `mongomock`
  is not a dependency. The migration is what brings testability.

### Modified

- `src/core/archiver.py` — replace pymongo calls with `audit_store`
  calls. Class structure stays identical; the swap is at the
  collection-access layer (~17 internal sites; the broader caller
  surface is ~28 sites in `graph.py`/`agent.py`/`services/*.py`).
  Method signatures unchanged. The two-phase `update_*` methods become
  thin wrappers that INSERT a second `event_phase='post'` row instead
  of UPDATE'ing.
- `src/database/mongo_db.py` — keep the class until cutover (under the
  feature flag), delete after.
- `orchestrator/database/mongodb.py` — keep the class until cutover
  (under the feature flag), delete after. **The original draft missed
  this file entirely** (923 LoC, the read-side adapter the cockpit
  depends on).
- `src/database/__init__.py` — export `AuditStore` alongside `MongoDB`,
  remove `MongoDB` after cutover.
- `orchestrator/database/__init__.py` — same.
- `orchestrator/main.py` — replace `mongodb = MongoDB()` and the
  `mongodb.is_available` checks with `audit = AuditStore()` /
  `audit.is_available`. **~50 callsites** across imports, lifespan
  init/shutdown, 9 audit endpoints, 3 N+1 enrichers (lines 84, 152,
  162, 2846, 2864-2865, 3051, 3261-3287, 3313-3317, 7425, 7438, 7461,
  7468, 7488, 7492, 7513, 7524, 7857, 7868, 7892, 7903, 7927, 7938,
  7958, 7962, 10838, 10847, 14422-14427). Original draft's "~15
  callsites" was a 3× undercount.
- `orchestrator/graph_routes.py` — `set_mongodb()` becomes
  `set_audit_store()`. Line 142's raw `mongodb._db["agent_audit"]`
  access is replaced with `audit.iter_tool_calls(job_id)`.
- `orchestrator/init.py` — apply `audit_schema.sql` to `srw-auditdb`
  on startup. Mirror the existing pattern for `srw-postgres` and
  `srw-pgvector`. Replace `mongodump`/`mongorestore` shell-outs at
  lines 637, 674, 1395, 1423, 1608, 1655 with `pg_dump`/`pg_restore`.
  Drop `--skip-mongodb` CLI flag.
- `init.py` — same backup/restore swap.
- `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py` — drop
  ObjectId-string assumptions in tool schemas (lines listed in Cockpit
  / API section).
- `orchestrator/services/builder_dispatch.py`,
  `orchestrator/services/builder_tools.py`,
  `orchestrator/services/formatters.py` — same ObjectId cleanup.
- `helm/values.yaml` — new `databases.audit.{enabled,internal,
  externalUrl,image,storageClass,storageSize,resources}` block (mirror
  the existing `databases.keycloak.*` block exactly). Default
  `enabled: true, internal: true`. Remove `databases.mongodb.*` after
  cutover. Remove `global.hostnames.mongo`.
- `helm/values.example.yaml` — parallel external-postgres example.
  Replace `databases.mongodb` external block with `databases.audit`.
  Same change in `helm/ci/customer-external-values.yaml`.
- `helm/templates/_helpers.tpl` — **the original draft was wrong
  about `srw.vectorDbUrl`**: that helper does not exist. `DATABASE_URL`
  and `VECTOR_DB_URL` are read directly from Secret keys
  (`orchestrator/deployment.yaml:77-86`, `agent/deployment.yaml:64-73`).
  Adopt the same pattern: `AUDIT_DB_URL` is a Secret key, fully
  assembled (in internal mode by ESO/operator; in compose by env
  interpolation). Drop `srw.mongodbUrl` (lines 347-353) and
  `srw.mongoHost` (lines 325-327) after cutover; do **not** add a
  `srw.auditDbUrl` helper unless we want a thin host-only template
  for `wait-for-auditdb`.
- `helm/templates/configmap.yaml` — drop `MONGODB_URL` line (currently
  line 17). The new `AUDIT_DB_URL` lives in the Secret because it
  carries credentials, matching how `DATABASE_URL`/`VECTOR_DB_URL`
  are wired today.
- `helm/templates/orchestrator/deployment.yaml` — `MONGODB_URL` env
  var (lines 87-91) becomes `AUDIT_DB_URL` from Secret
  (`secretKeyRef`); `wait-for-mongodb` initContainer (lines 37-41)
  becomes `wait-for-auditdb` against `<fullname>-auditdb:5432`.
- `helm/templates/agent/deployment.yaml` — same env-var rename
  (lines 74-78). MCP deployment if present, same.
- `helm/templates/secret.yaml` / `external-secret.yaml` — `dataFrom:
  extract:` is bulk-projected, no per-key template change needed. Just
  document `AUDIT_DB_PASSWORD` and (external mode) `AUDIT_DB_URL` in
  the Secret schema in `helm/README.md` (lines 202-208).
- `helm/README.md` — update Secret schema and component table; remove
  mongo-express row.
- `helm/templates/cockpit/deployment.yaml:17` — drop `mongoExpressUrl`
  env-init field.
- `helm/templates/ingress.yaml:11, 278-315` — drop mongo-express host
  + ingress block.
- `docs/issues/deployment_separation_of_concerns.md` — once
  implemented, mark Issue C resolved with a forward reference here.
- `docker-compose.yaml` / `docker-compose.dev.yaml` /
  `docker-compose.local.yaml` — add `postgres-audit` service +
  `postgres_audit_data` volume (mirror `postgres-keycloak` shape;
  in dev expose port 5434). Replace `MONGODB_URL` env on orchestrator
  / agent / mcp services with inline-assembled `AUDIT_DB_URL`
  (matches existing `DATABASE_URL`/`VECTOR_DB_URL` compose pattern).
  Replace `depends_on: mongodb` with `depends_on: postgres-audit`.
  Drop `mongodb` and `mongo-express` services + `mongodb_data` volume
  after cutover.
- `cockpit/src/app/core/models/audit.model.ts`,
  `cockpit/src/app/debug/request.model.ts`,
  `cockpit/src/app/core/models/chat.model.ts`,
  `cockpit/src/app/core/models/cache.model.ts` — `_id: string` →
  `id: number`. Bump `cache.model.ts` version.
- `cockpit/src/app/debug/services/request.service.ts:60` — change
  ObjectId regex to `/^\d+$/` or remove.
- `.github/workflows/develop.yml`, `.github/workflows/main.yml` — add
  `-r requirements-dev.txt` to the test-deps install line.

### Deleted (post-cutover)

- `helm/templates/databases/mongodb.yaml`
- `helm/templates/optional/mongo-express.yaml` (or wherever it lives)
- `src/database/mongo_db.py`
- `orchestrator/database/mongodb.py`
- All `mongoExpress.*` chart values, `srw.mongodbUrl` /
  `srw.mongoHost` helpers, `global.hostnames.mongo`.

## Verification

1. **Unit tests** — greenfield `tests/test_audit_store.py` (~20 tests)
   against a `testcontainers` Postgres fixture. Cover: insert paths,
   append-only `agent_audit` two-phase (`pre` then `post` row),
   `get_job_stats` aggregation correctness, `chat_history` round-trip,
   pagination and bulk endpoints, the `is_available=False` no-op
   branch, and `iter_tool_calls` parity with the old raw-cursor query.
2. **Integration test** — run a full job in the dev compose stack
   with `AUDIT_BACKEND=postgres`, then with `=mongodb`, diff the
   cockpit's audit pane and the `/api/jobs/{id}/audit` JSON response
   between the two. Should be byte-equivalent except for the `id`
   field type (string ObjectId vs. integer) and the second
   `event_phase='post'` row appearing in the Postgres trace.
3. **Performance smoke** — run a 200-call job, measure the time spent
   in archiving. Target: within 20% of the Mongo baseline. JSONB
   inserts on Postgres with `fillfactor=100` and LZ4 are competitive
   per call. For bulk reads (5000-row endpoints) verify with
   asyncpg-binary fetch.
4. **Helm renders** — three shapes: internal audit DB, external
   audit DB, audit disabled (`databases.audit.enabled=false`, falls
   back to no-op archiver same as today's "MongoDB unavailable"
   path).
5. **Retention** — manually drop a partition older than the retention
   window via `DETACH PARTITION ... CONCURRENTLY` + `DROP TABLE`,
   verify subsequent queries succeed without it. Verify the
   maintenance hook's parent `ANALYZE` runs.
6. **Partition lookahead alarm** — verify the "days until next
   partition needed" metric / log surfaces if pre-creation falls
   behind.

## Out of scope for this feature

- Switching `tools/mongodb/` (customer datasource tooling) off Mongo —
  customers will keep attaching MongoDB datasources. Pymongo stays as
  a runtime dependency for that surface.
- Audit trail HA / replication — same scale argument as for the main
  Postgres; revisit when load demands it.
- Materialized views or pre-aggregated stats tables for cockpit
  dashboards — current aggregations are fast enough on partitioned
  tables; revisit if per-page latency degrades.
- Long-term archival to S3 — separate concern, lives under the snapshot
  strategy in `s3.*`. If `chat_history` analytics needs more than 365
  days, that lands here, not in the operational store.

## Open questions — resolved

1. **Retention default** — **resolved**. 90 days for `agent_audit` /
   `llm_requests` (operational logs convention), 365 days for
   `chat_history` (longer-than-operational, shorter-than-indefinite —
   if product analytics needs more, dump to S3/Parquet outside this
   store).
2. **`pg_partman` vs. hand-rolled partitions** — **resolved in favor of
   `pg_partman`**. Hand-rolled is viable but the original "30 LoC"
   estimate undercounts by ~2×: the realistic spec needs advisory-
   locked creation, N+2 month lookahead, parent `ANALYZE`,
   `DETACH ... CONCURRENTLY` retention, no default partition, and a
   "days until next partition needed" metric. If the team prefers
   extension-freedom, take the hand-rolled path with the full ~60 LoC
   spec — but pg_partman + jobmon is the lower-regret default.
3. **Cutover gating** — **resolved**. Single `AUDIT_BACKEND` flag.
   The test deployment can be wiped, which is the strongest possible
   coordination signal; per-collection flags are over-engineered.
