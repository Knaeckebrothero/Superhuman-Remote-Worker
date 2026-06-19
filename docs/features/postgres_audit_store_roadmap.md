# Postgres Audit Store — Implementation Roadmap

Companion to `docs/features/postgres_audit_store.md` (the design doc).
This roadmap sequences the work into 8 phases gated by objective exit
criteria. Total estimated effort: **17-25 working days** (~3-4 calendar
weeks for one engineer familiar with the codebase); **~1-1.5 days less
if the Compose deprecation lands first** (gate G6).

> **2026-06-11 — implementation package exists**:
> `postgres_audit_store_implementation.md` closes every gate (G0
> hand-rolled, G6 skip-Compose), carries the live-validated DDL, the
> partition-module spec, the full adapter interface, and a 7-PR plan that
> refines P0-P8 below. Read that first; this file remains the
> phase/gate/exit-criteria reference.

> **Revision note (2026-06-10)** — re-verified against the current
> tree alongside the design doc. The two structural changes: (1) the
> audit DDL ships as a `migrations/audit/` family applied by the
> existing migration runner, not an `audit_schema.sql` applied by
> `init.py` (P1 + P5 reworked accordingly); (2) all Compose work is
> now conditional on gate G6 — `docs/issues/deprecate_docker_compose_stack.md`
> proposes deleting the Compose stack entirely (~2-2.5 eng-days, 5
> PRs), and sequencing it first deletes the files P5 would otherwise
> have to mirror. Validation venue is the local k3d cluster either
> way. Line refs throughout updated to the 2026-06-10 tree; expect
> further drift and re-grep at implementation time.

## Current status (2026-06-19) — SHIPPED, cutover live on dev

All phases implemented and deployed. Postgres (`srw-auditdb`) is the live audit
backend on the **dev cluster**; the irreversible P7 flip is done. Remaining work
is the soak + Mongo wipe + P8 cleanup. Full detail (verification, deploy gotcha,
backup) in `postgres_audit_store_implementation.md` § Current status.

| Phase | Work | PR(s) | State |
|-------|------|-------|-------|
| P0 Pre-flight | gates closed (G0 hand-rolled, G6 skip-Compose) | — | ✅ |
| P1 Foundation | `migrations/audit/0001` + partition module + lifespan | PR 1 | ✅ |
| P2 Writer | `SyncAuditWriter` + archiver branches | PR 2 | ✅ |
| P3 Reader | `AuditStore` (stitch + filters) | PR 3 | ✅ |
| P4 Wiring | orchestrator reader selector + cockpit id-normalization | PR 4a/4b | ✅ |
| P5 Infrastructure | Helm `srw-auditdb` + secret/NetPol/`wait-for-auditdb` | PR 5 | ✅ k3d-verified |
| P6 Validation | lean cut — component tests + k3d in-cluster e2e in place of a formal A/B; G3 perf gate not run as a phase | PR 6 | ✅ |
| P7 Cutover | chart default `mongodb`→`postgres` + `generator.mjs` drift fix; **deployed to dev** | PR 7 | ✅ live |
| P8 Cleanup | remove Mongo chart/modules/`motor`/`MONGODB_URL` + `_id` dual-reads | — | ⏳ pending soak |

**Post-flip tail:** ~24 h soak → fresh Mongo dump + wipe `srw_logs` (delete PVC) →
P8. Pre-wipe backup taken 2026-06-19 14:44 (`~/srw-audit-mongo-backup/…`,
276,543 docs, verified). **Deploy trap (hit + fixed on dev):** ESO-backed envs
need the Vault `AUDIT_POSTGRES_USER/PASSWORD` keys *before* the chart bump, else
`srw-auditdb` crash-loops on `CreateContainerConfigError` and blocks the
orchestrator's `wait-for-auditdb`.

---

## Critical path

```
P0 Pre-flight
    └─> P1 Foundation ──┬─> P2 Writer ──┐
                        └─> P3 Reader ──┴─> P4 Wiring ──> P5 Infrastructure ──> P6 Validation ─┐
                                                                                                ├─> P7 Cutover ──> P8 Cleanup
```

P2 and P3 are independent once P1 lands — single engineer does them
serially, two engineers can split. Everything else is strictly serial.
P5 (infrastructure) can start as soon as P1's schema is finalized
(parallel with P2/P3/P4) since it's chart/compose work, not Python.

## Decision gates

| Gate | Phase | Decision | Default if no answer |
|------|-------|----------|----------------------|
| G0 | P0 | pg_partman or hand-rolled? | pg_partman (lower regret) |
| G1 | P0 | testcontainers or alternative? | testcontainers[postgres] |
| G2 | P5 | Drop default partition? | Yes (avoids ATTACH stalls) |
| G3 | P6 | Performance within 20% of Mongo baseline? | Block cutover if no |
| G4 | P7 | Wipe test cluster before flip? | Yes (per user authorization) |
| G5 | P8 | Drop `pymongo` dep entirely? | No — customer datasource tools still need it |
| G6 | P0 | Land the Compose deprecation first? | Yes — `docs/issues/deprecate_docker_compose_stack.md` deletes the three compose files P5 would otherwise mirror |

## Rollback strategy

Every phase up to **P7 (cutover)** is feature-flag gated
(`AUDIT_BACKEND=mongodb|postgres`, default `mongodb`). Rollback at any
point P1-P6 is `git revert` of the phase commit + redeploy. No data
loss because Mongo stays the source of truth until G4.

P7 (the flip) is the irreversible gate. Once `AUDIT_BACKEND` defaults
to `postgres` and the test cluster is wiped, going back means
re-running Mongo cutover in reverse — feasible but costs a half-day.

P8 (Mongo deletion) is reversible only via `git revert` of the cleanup
commit; data written to Postgres after P7 stays in Postgres.

---

## Phase 0 — Pre-flight (~0.5 day)

**Goal**: settle the two open implementation choices and create the
working branch.

### Deliverables

- [ ] G0 decision recorded in this file: `pg_partman` or hand-rolled
- [ ] G1 decision recorded: `testcontainers[postgres]` confirmed (no
      alternative has precedent in the repo)
- [ ] G6 decision recorded: sequence the Compose deprecation
      (`docs/issues/deprecate_docker_compose_stack.md`) before P5, or
      keep the Compose mirroring in scope
- [ ] Branch `feature/postgres-audit-store` cut from `develop`
- [ ] Feature-flag plan documented (env var `AUDIT_BACKEND`, default
      `mongodb`, valid values `mongodb` / `postgres`, location:
      `orchestrator/main.py` config block, also read by
      `src/core/archiver.py`)

### Exit criteria

- Branch exists and CI is green on it (no behavior change yet)
- G0/G1 recorded so reviewers don't relitigate during code review

### Risks

- pg_partman familiarity gap. **Mitigation**: read the Crunchy
  partitioning post and the pg_partman README before starting P1; it's
  an hour. If still uncomfortable, switch G0 to hand-rolled and accept
  the ~60 LoC maintenance code.

---

## Phase 1 — Foundation (~2-3 days)

**Goal**: land the schema, the test fixture, the feature flag, and
empty adapter skeletons so subsequent phases can fill in methods
without wrestling scaffolding.

### Deliverables

- [ ] `orchestrator/database/migrations/audit/0001_initial.sql` — DDL
      per the design doc: 3 partitioned tables, per-table autovacuum
      settings, LZ4 compression, btree + expression indexes, the "no
      GIN on `agent_audit.payload`" comment. **(Reworked 2026-06-10:
      the repo now ships schema as checksum-tracked migrations applied
      by `orchestrator/database/migrate.py` — see
      `docs/db_migration.md`. No `audit_schema.sql` snapshot, no
      init.py application. Later audit-schema changes are new numbered
      files; `.notx.sql` for `CREATE INDEX CONCURRENTLY`-class ops.)**
- [ ] Runner wiring: add `MIGRATIONS_AUDIT_DIR` beside the app/vector
      constants (`orchestrator/database/postgres.py:169-170`) and run
      the audit family against the audit pool in the orchestrator
      lifespan (third `run_migrations()` invocation)
- [ ] If G0 = pg_partman: extension setup + `create_parent` calls in
      the migration; if hand-rolled: `partition_helper.py` with
      advisory-locked creation, N+2 lookahead, parent ANALYZE,
      `DETACH ... CONCURRENTLY` retention (~60 LoC)
- [ ] `requirements-dev.txt` (new file) with `testcontainers[postgres]`
- [ ] `tests/_audit_db_fixture.py` — session-scoped `PostgresContainer`,
      function-scoped `TRUNCATE` reset, migration bootstrap on container
      start (run the audit family via `run_migrations()` so tests cover
      the real DDL path). Mirrors `tests/_fs_backend.py` "test-only"
      discipline
- [ ] `.github/workflows/develop.yml:469` and
      `.github/workflows/main.yml:244` (the `uv pip install ... pytest
      pytest-asyncio` test-deps lines) — install
      `-r requirements-dev.txt`
- [ ] `orchestrator/database/audit_store.py` — empty `AuditStore`
      class with method stubs that `raise NotImplementedError`,
      `is_available` property, `connect()`/`disconnect()`
- [ ] Feature-flag plumbing: `AUDIT_BACKEND` env var read in
      `orchestrator/main.py` lifespan; selection logic that returns
      either the existing `MongoDB` or the new `AuditStore`
- [ ] Smoke test: `pytest tests/test_audit_store_smoke.py` that
      verifies the schema applies cleanly to a fresh container

### Exit criteria

- `pytest tests/` passes locally and in CI with no regressions
- Smoke test green: container boots, schema applies, partitions exist
- Setting `AUDIT_BACKEND=postgres` doesn't crash the orchestrator
  startup (it just uses the stub which returns `is_available=False`)

### Parallel work possible

- Cockpit type updates (P4) can start now — no runtime dependency on
  the adapter

### Risks

- **CI runner doesn't have docker.** Mitigation: GH Actions
  ubuntu-latest does. Local dev needs docker or podman.
- **Schema drift between design doc and DDL.** Mitigation: copy the
  DDL block from the design doc verbatim; no creative additions in P1.

---

## Phase 2 — Writer (~2-3 days)

**Goal**: implement the write surface and prove it round-trips
correctly against testcontainers.

### Deliverables

- [ ] `AuditStore.archive(...)` — INSERT into `llm_requests`, return
      BIGINT id
- [ ] `AuditStore.audit_step(...)` — INSERT into `agent_audit` with
      `event_phase='pre'`
- [ ] `AuditStore.audit_llm_call(...)` and `audit_tool_call(...)` —
      thin wrappers over `audit_step` with the right `step_type`
- [ ] `AuditStore.update_llm_response(audit_id, ...)` — **INSERT a
      second `agent_audit` row with `event_phase='post'`** referencing
      the same `request_id` (NOT an UPDATE; this is the design
      decision)
- [ ] `AuditStore.update_tool_result(audit_id, ...)` — same pattern
- [ ] `AuditStore._archive_chat_entry(...)` — INSERT into
      `chat_history`
- [ ] `LLMArchiver` dispatches on `AUDIT_BACKEND`: when `postgres`,
      route to `AuditStore`; when `mongodb`, unchanged
- [ ] Unit tests in `tests/test_audit_store.py`:
  - [ ] `test_archive_inserts_llm_request`
  - [ ] `test_audit_step_inserts_with_pre_phase`
  - [ ] `test_two_phase_creates_two_rows_with_matching_request_id`
  - [ ] `test_chat_history_round_trip`
  - [ ] `test_metrics_jsonb_roundtrip_preserves_types`
  - [ ] `test_is_available_false_when_pool_down`
  - [ ] `test_concurrent_writes_to_same_job_are_ordered_by_id`

### Exit criteria

- All P2 unit tests green against testcontainers
- Manual test: with `AUDIT_BACKEND=postgres` and a stub orchestrator
  endpoint, an `audit_step` call writes a row visible via `psql`
- `AUDIT_BACKEND=mongodb` (default) produces zero behavior change —
  full existing test suite still green

### Parallel work possible

- P3 (reader) can run in parallel — different methods, same class
- Cockpit type updates continue

### Risks

- **`asyncpg` JSONB quirks**: `asyncpg` returns JSONB as `str` by
  default unless you register a codec. Set up the codec in `connect()`
  and add a test that round-trips a nested dict.
- **Two-phase rows ordered by `id` not by `event_phase`**: reader must
  not assume `pre` always comes before `post` in scan order if rows
  arrive out of order; rely on `event_phase` column, not scan order,
  for "latest" semantics.

---

## Phase 3 — Reader (~3-4 days)

**Goal**: implement the read surface for all 9 endpoints (plus
internal-use helpers) and prove parity with the Mongo reader.

### Deliverables

- [ ] `get_job_audit(job_id, limit, offset, order, filter)` — paginated
      with the existing `FilterCategory` enum
- [ ] `get_job_audit_bulk(job_id, limit=5000, offset)`
- [ ] `get_chat_history(job_id, limit, offset)` and `_bulk` variant
- [ ] `get_graph_deltas_bulk(job_id, limit, offset)` — uses the
      `agent_audit_tool_name_idx` expression index
- [ ] `get_request(id: int)` — by BIGINT id (signature change from
      `doc_id: str`)
- [ ] `get_audit_time_range(job_id)` — `MIN`/`MAX` timestamp
- [ ] `get_job_version(job_id)` — single grouped query returning
      `{auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate}`
- [ ] `list_llm_requests(job_id, limit, offset)` — projection-only
      summary
- [ ] `get_audit_count(job_id)` — single-row helper
- [ ] `iter_tool_calls(job_id)` — async iterator that replaces the
      `mongodb._db["agent_audit"]` raw-cursor leak in
      `graph_routes.py:142`
- [ ] `get_job_stats(job_id)` and `get_audit_stats(job_id)` —
      aggregation SQL translating the four Mongo pipelines
      (`archiver.py:510, :530, :1066, :1093`)
- [ ] **Per-job seq computation** in read methods that need it: use
      `ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id)` rather
      than a stored `seq` column
- [ ] **Latest-phase deduplication**: where reads need the post-phase
      row only, use `DISTINCT ON (request_id) ... ORDER BY id DESC`
- [ ] Unit tests:
  - [ ] One round-trip test per endpoint method
  - [ ] `test_pagination_offset_and_limit`
  - [ ] `test_filter_category_messages_tools_errors`
  - [ ] `test_get_job_version_counts_match_inserted_rows`
  - [ ] `test_iter_tool_calls_matches_legacy_cursor_query`
  - [ ] `test_get_job_stats_matches_mongo_aggregation_shape`
  - [ ] `test_seq_via_row_number_is_monotonic_per_job`

### Exit criteria

- All P3 unit tests green
- Adapter is feature-complete (writer + reader); main.py still talks
  to MongoDB though

### Risks

- **Aggregation parity**: Mongo's `$addToSet` returns an unordered
  array; `array_agg(DISTINCT ...)` in Postgres returns ordered. Add a
  `sorted()` in the response serializer or accept the cosmetic
  difference. Test must assert as set, not list.
- **`get_job_version.lastUpdate`** must come from the same query that
  produces the counts, otherwise it's racy under concurrent writes.
  Use a single CTE.

---

## Phase 4 — Wiring (~3-4 days)

**Goal**: replace every Mongo callsite outside the adapter so the
orchestrator + cockpit work end-to-end with `AUDIT_BACKEND=postgres`
against a local testcontainer or dev DB.

### Deliverables

#### Orchestrator

- [ ] `orchestrator/main.py` — replace `mongodb = MongoDB()` (line
      237) with `audit = make_audit_store()` (factory dispatches on
      flag); update all ~50 callsites (re-verified 2026-06-10): 96/227
      (imports), 237 (init), 3325-3332 (lifespan connect +
      `ensure_indexes` — the `ensure_indexes` call is simply deleted
      on the Postgres path; the migration runner owns DDL), 3391
      (graph_routes share), 3672 (lifespan disconnect), 3933-3936 +
      3953-3954 + 18814-18816 (3 N+1 enrichers — collapse to single
      grouped query), 8418/8431 (`/audit`), 8444-8459
      (`/requests/{doc_id}` — change `doc_id: str` to `id: int`),
      8487/8491 (`/audit/timerange`), 8514/8525 (`/chat`), 9328/9339
      (`/audit/bulk`), 9365/9376 (`/chat/bulk`), 9402/9413
      (`/graph/bulk`), 9434/9438 (`/version`), 14623/14646
      (`/llm-requests`). Leave the "mongodb" hits that are
      customer-datasource surface (type enums, test-connection, tool
      catalogs)
- [ ] `orchestrator/graph_routes.py` — `set_mongodb()` (line 20) →
      `set_audit_store()`; line 142 raw-cursor leak →
      `audit.iter_tool_calls(job_id)`
- [ ] `src/database/__init__.py` and `orchestrator/database/__init__.py`
      — export `AuditStore` alongside `MongoDB` (keep both during flag
      window)

#### MCP + builder

- [ ] `orchestrator/mcp/server.py:354` and
      `orchestrator/mcp/client.py:263, :700` — drop ObjectId-string
      assumptions in tool descriptions; switch to integer.
      (Re-verified 2026-06-10: these three sites are all that remain —
      the longer original lists in `server.py`, `client.py`,
      `builder_dispatch.py`, and `formatters.py` have since been
      cleaned up independently.)
- [ ] `orchestrator/services/builder_tools.py:1072` — same (last
      remaining builder site)

#### Cockpit

- [ ] `cockpit/src/app/core/models/audit.model.ts:65` — `_id: string`
      → `id: number`
- [ ] `cockpit/src/app/core/models/chat.model.ts:50` — same
- [ ] `cockpit/src/app/debug/request.model.ts:72` — same
- [ ] `cockpit/src/app/core/models/cache.model.ts:30, 67` — comment
      update + bump `version: number`
- [ ] `cockpit/src/app/debug/services/request.service.ts:60` —
      regex `/^[a-fA-F0-9]{24}$/` → `/^\d+$/`
- [ ] Grep cockpit for `_id` and verify all uses (chat-history.component
      tab-state Map keys, agent-activity.component expand-state Set,
      request-viewer text rendering, *ngFor trackBy) work with numbers
- [ ] If wire field name changes from `_id` to `id`, update all
      response-shape consumers; otherwise keep `_id` as the wire name

### Exit criteria

- Local: `AUDIT_BACKEND=postgres uvicorn orchestrator.main:app`
  starts without errors against a testcontainer
- Local: cockpit (`npm start`) shows the audit pane for a synthetic
  job written via the new write path; pagination works; bulk endpoints
  feed IndexedDB
- `AUDIT_BACKEND=mongodb` (default) still works identically — full
  test suite + manual smoke test
- `pytest tests/` green
- `cd cockpit && npm test` green

### Risks

- **Cockpit IndexedDB has cached entries with string IDs**.
  Mitigation: bump `cache.model.ts` version. If users complain about
  stale cache, document `chrome://settings → clear site data`.
- **MCP tool schemas break Claude Code clients**. Mitigation: Claude
  Code re-fetches tool schemas on connect; old conversations in flight
  are the only risk. Schedule the cutover during low-traffic.

---

## Phase 5 — Infrastructure (~2-3 days)

**Goal**: stand up `srw-auditdb` in Helm (and Compose, only if G6
keeps it). Can run partly in parallel with P2-P4 since it touches
different files.

### Deliverables

#### Helm

- [ ] `helm/templates/databases/postgres-audit.yaml` — copy of
      `postgres-keycloak.yaml`, swap labels and values paths to
      `databases.audit.*`. Use the simpler 2-flag conjunction (no
      parent service to gate on)
- [ ] `helm/values.yaml` — new `databases.audit.{enabled,internal,
      externalUrl,image,storageClass,storageSize,resources}` block
      (mirror `databases.keycloak`); `storageSize: "10Gi"` (mirror
      pgvector for append-heavy)
- [ ] `helm/values.example.yaml` — replace `databases.mongodb`
      external block with `databases.audit`; same in
      `helm/ci/customer-external-values.yaml`
- [ ] `helm/templates/configmap.yaml:30-31` — drop the `MONGODB_URL`
      line; add `AUDIT_POSTGRES_{HOST,PORT,DB}` keys mirroring the
      existing `VECTOR_POSTGRES_*` trio. **(Convention changed since
      the original draft: Postgres DSNs are component parts assembled
      in-process by `utils/db_url.build_postgres_url(prefix,
      fallback_env=...)` — user/password from the Secret, host/port/db
      from the ConfigMap — not full-URL Secret keys.)**
- [ ] `helm/templates/orchestrator/deployment.yaml:37-40` —
      `wait-for-mongodb` initContainer → `wait-for-auditdb` against
      `<fullname>-auditdb:5432`
- [ ] `helm/templates/orchestrator/deployment.yaml:130-134` —
      `MONGODB_URL` env (`configMapKeyRef`) → five `AUDIT_POSTGRES_*`
      part envs (user/password via `secretKeyRef`, host/port/db via
      `configMapKeyRef`), mirroring the `VECTOR_POSTGRES_*` block at
      `:105-129`
- [ ] Agent side: **no template change** — `helm/templates/agent/`
      only holds `pdb.yaml`/`service.yaml` now; dynamic agent pods
      inherit ConfigMap + Secret via `envFrom`
      (`orchestrator/services/agent_provisioner.py:1060-1064`), so the
      new keys flow automatically. Code change instead:
      `src/core/archiver.py:209` gates archiving on `MONGODB_URL` —
      switch to `build_postgres_url("AUDIT_POSTGRES",
      fallback_env="AUDIT_DB_URL")`
- [ ] MCP deployment (`helm/templates/mcp/deployment.yaml`) — verified
      2026-06-10: no Mongo env, nothing to do
- [ ] `helm/README.md` § "Secret schema" (line 192) — document
      `AUDIT_POSTGRES_USER` / `AUDIT_POSTGRES_PASSWORD` (internal
      mode) and the `AUDIT_DB_URL` fallback (external mode)
- [ ] **Stage** mongo-express removal (don't delete yet — keep for
      P6 validation, delete in P8): mark `helm/templates/optional/
      mongo-express.yaml`, `srw.mongoHost` helper
      (`_helpers.tpl:408-409`), `srw.mongodbUrl`
      (`_helpers.tpl:481-487`), `global.hostnames.mongo`
      (`values.yaml:61`), `mongoExpress.*` (`values.yaml:881-890`),
      `helm/templates/cockpit/deployment.yaml:17` `mongoExpressUrl`,
      `helm/templates/ingress.yaml:11, 443-481` for deletion in P8

#### Compose (3 files) — **skip entirely if G6 = deprecation landed**

The migration runner applies the audit DDL at orchestrator startup, so
no `/docker-entrypoint-initdb.d/` mount is needed in any case — the
container just needs an empty database.

- [ ] `docker-compose.yaml` — new `postgres-audit` service block
      (mirror `postgres-keycloak` shape); new `postgres_audit_data`
      volume; orchestrator + agent + mcp `MONGODB_URL` envs →
      `AUDIT_POSTGRES_*` parts (or the `AUDIT_DB_URL` fallback);
      `depends_on: mongodb` → `depends_on: postgres-audit`
- [ ] `docker-compose.dev.yaml` — same; add
      `ports: - "${AUDIT_POSTGRES_PORT:-5434}:5432"` to expose for
      local debugging (mirrors pgvector dev port 5433)
- [ ] `docker-compose.local.yaml` — same
- [ ] **Keep** `mongodb` and `mongo-express` services through P6;
      delete in P8

#### init.py / orchestrator/init.py

- [ ] **No schema application here** — reworked 2026-06-10: the
      migration runner (P1) owns audit DDL at orchestrator startup.
      `init.py` keeps only seeding + backup/restore concerns
- [ ] `orchestrator/init.py` — replace audit-store `MONGODB_URL`
      references (1487, 1515, 1656, 1703) with the `AUDIT_POSTGRES_*`
      / `AUDIT_DB_URL` resolution; `DEFAULT_DS_MONGODB_URL` for
      customer datasources (701, 738) stays
- [ ] `orchestrator/init.py` and `init.py` — replace `mongodump`/
      `mongorestore` shell-out (URL parse 1491, dump 1646-1690,
      restore 1693-1740) with `pg_dump --jobs=N`/`pg_restore`
      (partitioned tables parallelize natively); preserve existing
      CLI surface (`--backup`, `--restore`, etc.)
- [ ] `init.py` — update or drop `--skip-mongodb` flag (lines 23,
      129-151, 177, 232-279, 514, 543, 630); rename to `--skip-audit`
      if keeping the escape hatch. The Mongo index-bootstrap path in
      `orchestrator/init.py:1576-1593` stays alive through the flag
      window (Mongo backend must keep working until P7) and is
      deleted in P8
- [ ] If pg_partman: ensure the extension is created before the
      migration runs (`CREATE EXTENSION IF NOT EXISTS pg_partman` as
      the first statement of `0001_initial.sql`, or a custom image —
      see Risks)

### Exit criteria

- `helm template` renders cleanly for all three values shapes
  (internal, external, disabled) — test in P6
- Local k3d: `helm upgrade srw ./helm -n srw -f
  deployment/values-local.yaml` brings up `srw-auditdb`, the
  orchestrator lifespan applies the audit migration family, and
  `psql` in the pod shows the three partitioned tables
- *(Only if G6 = Compose kept)* `podman-compose -f
  docker-compose.dev.yaml up -d postgres-audit` starts the new
  container and the orchestrator applies migrations against it
  (`psql -h localhost -p 5434`)
- Backup/restore round-trip on the new instance produces a usable
  snapshot

### Risks

- **`postgres-keycloak` 4-flag conjunction template**: don't blindly
  copy. The audit DB needs the simpler 2-flag (`enabled && internal`)
  shape since there's no parent service.
- **pg_partman extension availability**: the stock `postgres:15`
  image does NOT include pg_partman. Either switch image to
  `pgpartman/pgpartman:15` (or build a custom image) or fall back to
  hand-rolled. **Reconfirm G0 here** — this is the moment of truth.
- **Migration ordering on first boot**: the orchestrator must apply
  the audit migration family before the first archiver write and
  before serving audit endpoints. The lifespan already sequences
  app/vector migrations ahead of traffic — wire the audit family into
  the same block, gated on the audit pool being reachable.

---

## Phase 6 — Validation (~2-3 days)

**Goal**: prove the new path is correct, fast enough, and operationally
sound before flipping the default.

### Deliverables

- [ ] **Integration test**: run a full job on the local k3d cluster
      with `AUDIT_BACKEND=postgres`, then with `AUDIT_BACKEND=mongodb`
      (env override via `deployment/values-local.yaml`; Tilt gives the
      fast loop), diff the cockpit's audit pane and the JSON of all 9
      endpoints
      between the two. Differences expected: `id` field type
      (string ObjectId vs int), the second `event_phase='post'` row
      in the Postgres trace (cockpit must collapse to "latest" on
      display). Everything else should be byte-equivalent.
- [ ] **Performance smoke**: 200-call job, measure archiver overhead
      per call (p50, p95). **Target: within 20% of the Mongo
      baseline.** If outside: profile, swap bulk reads to
      `asyncpg.copy_records_to_table`-style binary fetch. **G3 GATE:
      block cutover if not within target.**
- [ ] **Concurrency smoke** (added 2026-06-10): ~100 concurrent
      simulated writers driving the `AuditStore` write surface at
      realistic per-agent cadence (~1 insert/sec each, mixed row
      sizes) against the k3d instance. Record p50/p95 insert latency
      and confirm the server-side backend count stays bounded (pooling
      works). This — not single-job speed — is the enterprise-scale
      scenario; see design doc § "Scale envelope".
- [ ] **Helm renders**: three shapes — internal audit DB, external
      audit DB (`databases.audit.internal=false`), audit disabled
      (`databases.audit.enabled=false`, falls back to no-op archiver
      same as today's "MongoDB unavailable" path). Each must produce
      valid YAML and pass `kubectl apply --dry-run`.
- [ ] **Retention**: manually create a partition with old timestamp,
      run the retention path (`DETACH PARTITION ... CONCURRENTLY` +
      `DROP TABLE`), verify subsequent queries succeed. Confirm
      autovacuum + parent ANALYZE runs.
- [ ] **Partition lookahead alarm**: simulate "next partition missing"
      condition; verify the metric/log surfaces it.
- [ ] **Cache invalidation**: with cockpit running the old version,
      upgrade to the new bundle, verify IndexedDB version bump
      triggers a clean re-sync (no broken UI from stale string IDs).
- [ ] **MCP server**: connect Claude Code, call `get_request_by_id`
      with the new integer schema, verify the tool description shows
      "integer" not "ObjectId".
- [ ] **Document any deltas** found in integration testing in this
      file before proceeding.

### Exit criteria

- All 7 verification deliverables pass
- G3 gate cleared (perf within 20%)
- Sign-off recorded in PR description

### Risks

- **Performance below 20% target**. Mitigation order: (1) swap to
  binary fetch for bulk reads; (2) check if a missing index is the
  culprit (`EXPLAIN ANALYZE` the slow paths); (3) verify LZ4
  compression actually applied (`SELECT pg_column_compression(...)`);
  (4) tune `effective_cache_size` and `shared_buffers` on the
  container; (5) accept the gap and document why.
- **Mongo and Postgres outputs differ in subtle ways** (timestamp
  precision, JSON key ordering, NULL vs absent). Mitigation: write
  a normalizer in the diff harness, document each acceptable delta.

---

## Phase 7 — Cutover (~0.5-1 day)

**Goal**: flip the default to Postgres.

### Deliverables

- [ ] Change `AUDIT_BACKEND` default in `orchestrator/main.py` from
      `mongodb` to `postgres`
- [ ] Update Helm/Compose env defaults to match
- [ ] Wipe the test cluster (G4): drop the K8s namespace (locally:
      `k3d cluster delete srw` + recreate; `podman-compose down -v`
      only if Compose still exists); redeploy from scratch
- [ ] Verify a fresh job runs end-to-end on the new default
- [ ] Verify all 9 endpoints serve correctly under load
- [ ] Mark Issue C (Mongo unauthenticated) resolved in
      `docs/issues/deployment_separation_of_concerns.md` with a
      forward reference to this plan
- [ ] Update `docs/features/postgres_audit_store.md` Status from
      "Proposed (revised ...)" to "Implemented YYYY-MM-DD"
- [ ] Tag the merge commit `audit-postgres-cutover-v1` for easy
      rollback reference

### Exit criteria

- Test cluster running with `AUDIT_BACKEND=postgres` as default for
  ≥24 hours without regressions
- No new errors in orchestrator/agent logs related to the audit path
- Cockpit usable end-to-end by the user

### Risks

- **An obscure code path still does `mongodb.is_available` directly
  bypassing the flag**. Mitigation: grep for `MongoDB`, `mongodb.`,
  `_db[`, `pymongo` across the entire codebase one more time before
  the flip. If found, fix and re-validate.
- **Customer-attached MongoDB datasource tools (`src/tools/mongodb/`)
  break because someone confused them with the audit store**.
  Mitigation: explicit test that creates a MongoDB datasource and
  runs a job using `mongodb_query` tool. Should be unaffected.

---

## Phase 8 — Cleanup (~1-2 days)

**Goal**: delete the Mongo code paths now that Postgres is canonical.

### Deliverables

- [ ] Delete `src/database/mongo_db.py`
- [ ] Delete `orchestrator/database/mongodb.py` (takes
      `MONGODB_INDEX_DECLARATIONS` + `ensure_indexes()` with it)
- [ ] Delete the Mongo index-bootstrap path in
      `orchestrator/init.py:1576-1593` (imports
      `MONGODB_INDEX_DECLARATIONS` from the deleted module) and the
      `ensure_indexes` lifespan call in `orchestrator/main.py:3327-3332`
- [ ] Delete `helm/templates/databases/mongodb.yaml`
- [ ] Delete `helm/templates/optional/mongo-express.yaml`
- [ ] Delete `srw.mongodbUrl` helper (`_helpers.tpl:481-487`)
- [ ] Delete `srw.mongoHost` helper (`_helpers.tpl:408-409`)
- [ ] Delete `global.hostnames.mongo` from `values.yaml:61`
- [ ] Delete `databases.mongodb.*` (`values.yaml:404-418`) and
      `mongoExpress.*` (`values.yaml:881-890`) from `values.yaml`,
      `values.example.yaml`, `customer-external-values.yaml`
- [ ] Delete `helm/templates/cockpit/deployment.yaml:17`
      `mongoExpressUrl` env-init field
- [ ] Delete `helm/templates/ingress.yaml:11, 443-481` mongo-express
      block
- [ ] *(Only if Compose still exists — G6)* Drop `mongodb` and
      `mongo-express` services from all 3 compose files; drop
      `mongodb_data` volume
- [ ] Drop the `AUDIT_BACKEND=mongodb` branch in
      `orchestrator/main.py`'s factory and the flag itself (it's now
      pinned to `postgres`); delete the corresponding env var
      documentation
- [ ] Drop `LLMArchiver`'s Mongo-specific dispatch logic in
      `src/core/archiver.py`; the class talks only to `AuditStore`
- [ ] Remove `mongo_to_pg_audit` adapter shim if any was added
- [ ] **G5 decision**: do NOT drop `pymongo` from `requirements.txt`
      — `src/tools/mongodb/` (customer datasource tools) still uses
      it. Add a comment in `requirements.txt` noting why pymongo is
      still pinned
- [ ] Remove `MONGODB_URL` references from any remaining docs:
      `cockpit/README.md:126`, `docker/Dockerfile.orchestrator:13`
      comment, etc. (Keep `DEFAULT_DS_MONGODB_URL` references — that's
      customer datasources)
- [ ] Update `helm/README.md` component table to drop the MongoDB row
- [ ] Squash any "TODO: remove after cutover" comments left during
      P1-P5

### Exit criteria

- `git grep -i 'mongodb\.' --` returns only customer-datasource hits
  in `src/tools/mongodb/` and `orchestrator/services/builder_tools.py`
  datasource blocks (verify each remaining hit is intentional)
- `git grep -i 'mongo_express\|mongoExpress'` returns nothing
- Full test suite green
- Helm render still passes for all three shapes
- Deploy a fresh cluster from the cleaned-up chart, run a job
  end-to-end

### Risks

- **Accidentally delete a customer-datasource Mongo path**.
  Mitigation: every grep hit gets a manual classify ("audit store" vs
  "customer datasource") before deletion. The two live in different
  directories (`src/tools/mongodb/` is customer; `src/database/` and
  `orchestrator/database/` are infrastructure) — if the tree
  discipline is honored, a delete in the latter two is always safe.

---

## Effort summary

| Phase | Effort | Cumulative | Gate |
|-------|--------|-----------|------|
| P0 Pre-flight | 0.5d | 0.5d | G0, G1 |
| P1 Foundation | 2-3d | 2.5-3.5d | — |
| P2 Writer | 2-3d | 4.5-6.5d | — |
| P3 Reader | 3-4d | 7.5-10.5d | — |
| P4 Wiring | 3-4d | 10.5-14.5d | — |
| P5 Infrastructure | 2-3d | 12.5-17.5d | G2 |
| P6 Validation | 2-3d | 14.5-20.5d | G3 |
| P7 Cutover | 0.5-1d | 15-21.5d | G4 |
| P8 Cleanup | 1-2d | 16-23.5d | G5 |
| Buffer (15%) | ~2d | ~18-25d | — |

**Calendar: 3-4 weeks** assuming normal context-switching, code review
turnaround, and one engineer.

With G6 = yes (Compose deprecation first), the three-compose-file
mirroring drops out of P5 and the compose deletions drop out of P8 —
roughly 1-1.5 days saved, plus one less topology to validate in P6.
The deprecation itself is a separate ~2-2.5-day effort with
independent value; it isn't charged to this project.

## Parallelization map (for two engineers)

```
Engineer A: P0 → P1 → P2 → P4 (orchestrator + MCP) → P6 → P7 → P8
Engineer B:           P3 → P4 (cockpit) → P5 → P6
```

Engineer B starts after P1 lands the schema and adapter skeleton.
Convergence point is P6, where both engineers regroup for validation.
This shaves ~5 days off calendar but adds coordination overhead.

## Where to slow down

If anything goes sideways, **slow down at G3 (perf gate)**, not at
the cutover gate. A perf miss is a fixable engineering problem; a
broken cutover is a user-visible incident. Spend an extra day on
profiling rather than shipping a known-slow path and "fixing it
later."

The other slow-down point is **P5 if pg_partman extension isn't in
the postgres image**. Don't fight the image — switch G0 to
hand-rolled and write the ~60 LoC partition helper. Better than a
custom Docker build inside this PR.

## What this roadmap deliberately omits

- **Materialized views for cockpit dashboards** — out of scope per
  the design doc; revisit if per-page latency degrades.
- **HA / replication for `srw-auditdb`** — out of scope; revisit
  when load demands.
- **Long-term S3 archival** — separate concern under `s3.*`.
- **Customer-facing MongoDB tooling** — `src/tools/mongodb/` stays
  exactly as-is. If you find yourself touching it during this work,
  stop and confirm it's actually needed.
