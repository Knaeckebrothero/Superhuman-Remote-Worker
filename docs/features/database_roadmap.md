# Database roadmap — optimization + unification, sequenced

**Status: Active roadmap (created 2026-07-01; refined the same day by an
11-agent research sweep — 6 codebase recon + 5 web best-practices agents).**
This doc is the **sequencing and live status authority** for the database
work: it orders the item catalog from `database_optimization_plan.md` into
executable phases with acceptance criteria and researched implementation
notes, and folds in the unification track (`unified_message_store.md`). When
an item ships, its status updates **here** (with commit hash); the plan doc
stays the frozen evidence record.

All `file:line` refs below were re-verified 2026-07-01 (the plan doc's are
partially stale — code drifted). `main.py` always means `orchestrator/main.py`.

**Companion docs:**
- `database_optimization_plan.md` — the item catalog (QW-x / HF-x / D-x IDs
  used below). Read it for the original findings; read this for the *when,
  in what order, and exactly how*.
- `database_architecture.md` — the store/tier rules every phase must respect.
  (Its line 36 asserts `usage_daily`/`quota_limits` exist — they don't;
  Phase 6 fixes doc or builds tables.)
- `../issues/db_schema_hygiene.md` — origin record for the hygiene items.
- `unified_message_store.md` — the Phase 7 design seed. **Two of its load-
  bearing claims are now known false** (see its Corrections block + Phase 7).

## Where we stand (updated 2026-07-03; premises re-verified against code 2026-07-01)

**Phases 1, 2, and 3 are done on `develop`.** The mechanical debt (QW-1
`0c3ba669`, QW-4, and QW-2/3/5/6 in Phase 1 `6343852c`) and the drift keystone
HF-1 are shipped. The drift HF-1 targeted — **45 app / 7 vector / 2 audit**
migration files that had silently diverged from the frozen snapshots — is now
structurally prevented: `scripts/schema-snapshot.sh` regenerates committed
`*_current.sql` artifacts from a from-zero replay and CI fails on any drift
(Phase 2a, `be0540b4`; the artifact gate is verified green in CI). The two
install paths that still trusted the frozen snapshots were cut to the migration
chain in Phase 2b (`02599a5f`): `python init.py`'s vector path now runs
`apply_migrations()` (it had been loading the stale `vector_schema.sql`, which
left the vector DB missing halfvec/HNSW), and the docker-compose initdb mounts
were dropped in favour of the orchestrator's startup migrations. The frozen
`schema.sql`/`vector_schema.sql` remain only as test fixtures. Migration CI was
un-stuck in the same arc — the perpetually-red `squawk` job now lints only the
migrations a push changes rather than re-litigating the whole frozen history
(`9cae3180`).

Phase 3 (D-5) then stripped the retired MongoDB audit backend wholesale — the
two audit modules, the archiver's dual-backend split (now Postgres-only, the
`AUDIT_BACKEND` selector gone), the `init.py` Mongo family, the Helm
StatefulSet / mongo-express / NetworkPolicy / values plumbing, and the compose
services — keeping only the Mongo *datasource* connector. `helm template`
renders no Mongo resources; the audit trail is served entirely by the Postgres
`srw-auditdb`.

Remaining work is Phases 4–7. The research sweep's four premise corrections
still shape them:

1. **HF-3 undercounted**: 13 racy `jobs.context` writers, not 8 — five raw
   `SET context = $1::jsonb` statements bypass `update_job_context` with the
   identical lost-update pattern (Phase 4).
2. **G3's premise is stale**: `usage_events` already carries per-job/thread
   attribution (`ref_kind`/`ref_id`, populated by the live audit-sourced
   usage poll) — per-job LLM cost is *not* blocked on ledger consolidation.
3. **Sessions DO write `chat_history`** (since `986117f1`, 2026-06-12) —
   `unified_message_store.md`'s "the stores don't overlap" existence proof is
   wrong; the rows are written-but-never-read (Phase 7).
4. **The "0019 canonical message shape" is unbuilt**: the component columns
   are never written, never read; the intended builder
   (`src/llm/session_components.py`) has zero production callers. Phase 7-A
   *wires* the shape for the first time rather than adopting it.

## Phase overview

| Phase | Scope | Effort | Depends on | Status |
|---|---|---|---|---|
| 1 | Mechanical quick wins — QW-2, QW-3, QW-5, QW-6 | ~1 day | — (G1 decided: drop) | ✅ done `6343852c` — pushed + deployed on develop; migrate-from-zero + full suite verified |
| 2 | HF-1 generated schema artifact (keystone) | ~1 day | — | ✅ done — 2a `be0540b4` (script + 3 artifacts + CI gate) + 2b (init.py vector→migrations, compose initdb dropped) |
| 3 | D-5 Mongo code deletion | ~1–2 days | QW-4 soak ✅ (done, >1 week) | todo |
| 4 | HF-3 atomic context writes (correctness) | ~1–2 days | — | todo |
| 5 | Perf batch — HF-5, HF-4, HF-6, HF-7, HF-2 | ~3–4 days | — (HF-2 last) | todo |
| 6 | Usage rollup + no-deletion policy — D-1 build, D-2 closed by policy | ~1–2 days | — (decided 2026-07-02) | todo |
| 7 | Unification, direction A (one message model) | L (grew: 0019 is unbuilt) | Phases 1–5; G4 for direction B | gated |

Phases 1–4 ≈ one focused week; 1–6 ≈ two and a half. Phases 2 and 3 are
independent of Phase 1 and each other — parallelize freely. Phase 7 starts
as a design doc.

---

## Phase 1 — Mechanical quick wins (QW-2, QW-3, QW-5, QW-6)

Pure debt paydown; no design questions except the QW-6 default.

- **QW-2** — remove `"sessions"` from `REQUIRED_TABLES`
  (`orchestrator/database/postgres.py:189`) and `schema_migrations` from
  `VECTOR_REQUIRED_TABLES`. Minimal edit only — Phase 2 supersedes or
  regenerates this check.
- **QW-3** — one `.notx.sql` migration: `DROP INDEX CONCURRENTLY IF EXISTS
  idx_threads_user_id` (true duplicate of `idx_threads_user`) and
  `idx_thread_messages_thread` (prefix-subset of the 0020/0023 composites).
  Verified: no code references either index by name. (A third candidate,
  `idx_jobs_priority`, likely goes redundant once HF-4's partial index lands
  — check `pg_stat_user_indexes.idx_scan` after Phase 5 and fold into a
  later migration; don't drop it here.)
- **QW-5** — the full clean cut of the pre-Keycloak auth family:
  - Delete the 5 dead methods (`get_user_by_email_with_auth`,
    `set_user_password`, `set_email_verified`, `create_user_with_password`
    at `postgres.py:4587-4672`; `upsert_default_user` at `:7368`) + their
    tests.
  - Delete `migrate_existing_users_verified` (`postgres.py:4674`) **and its
    startup call** (`postgres.py:8230`, invoked from `apply_migrations`) —
    the backfill is moot once the column drops.
  - Migration dropping `users.password_hash` + `users.email_verified`.
  - Verified: the `email_verified` in `security/auth.py:60` is an **OIDC
    token claim**, not the DB column; cockpit has zero references.
    Re-confirm no column readers at implementation time before the drop.
- **QW-6** — per **G1** default: drop `capability_grant_audit` + its two
  INSERTs (`postgres.py:9113,9151`).

**Acceptance:** fresh k3d install boots with no `Missing tables` warning; all
migrations apply from zero and on an existing dev DB; full pytest green;
grep-clean for dropped methods/table; `EXPLAIN` on `threads WHERE user_id=…`
still index-backed via `idx_threads_user`.

**Status: ✅ shipped — commit `6343852c` on develop** (rebased from local
`ed68f610`, now an orphan reflog entry; pushed + deployed — deploy commits sit
on top, so 0042–0045 have already run on the dev cluster). Migrations 0042–0045
(one statement per `.notx.sql` — the runner executes each file as a single
simple-query message, so multi-statement CONCURRENTLY files would fail in an
implicit transaction). QW-6 went end-to-end: with the audit table gone, the
audit-only `reason`/`actor` plumbing was removed from `set_grant`/
`delete_grant`, `GrantSet`, and the cockpit "Reason (optional)" field (it
would otherwise silently discard admin input). Verified: full suite 7518
passed / 19 skipped (one pre-existing live-DB test needs localhost:5432, not
ours); cockpit 778 tests + prod build green; **migrate-from-zero on a
scratch pgvector:pg15**: all 45 migrations apply, `password_hash`/
`email_verified`/`capability_grant_audit`/both duplicate indexes gone,
`idx_threads_user` + the 0020/0023 composites + `capability_grants` intact.
Pending: k3d boot-log check (no `Missing tables` warning) on next cluster
deploy.

## Phase 2 — HF-1: generated current-schema artifact (the keystone)

The root-cause fix for the drift bug class. Migrate-from-zero is **verified
viable**: all three `0001` files are full baselines (`CREATE TABLE IF NOT
EXISTS` + extensions), nothing assumes `schema.sql`, and the four phantom
tables self-clean under from-zero replay (created then dropped/renamed by
0009/0010/0032).

**Implementation notes (researched 2026-07-01):**

- **Entry point**: `scripts/schema-snapshot.sh` — there is no root Makefile;
  `scripts/*.sh` + `python -m` is the repo convention. One script owns the
  whole lifecycle per family (start scratch container → `pg_isready` loop →
  `python -m orchestrator.database.migrate --dir … ` from zero → dump →
  normalize), so local == CI exactly.
- **Pin images per store, matching prod** (`helm/values.yaml`): app
  `pgvector/pgvector:pg15` (:452), vector `pgvector/pgvector:pg15` (:470),
  **audit `postgres:16`** (:499). The existing CI harness
  (`.github/workflows/db-migrations.yml:86`) runs `pgvector/pgvector:pg16`
  for app+vector — **wrong major for artifact purposes** (pg_dump output is
  major-version-sensitive); the artifact job pins its own images.
- **Dump inside the server container** (`docker exec <scratch> pg_dump …`) so
  client==server *by construction* — no runner-side pg_dump install/drift.
- **Flag set + normalization** (Rails-`structure.sql` pattern):
  `--schema-only --no-owner --no-privileges --no-comments
  --no-security-labels --no-tablespaces --restrict-key=<fixed>`, then sed:
  drop `\restrict`/`\unrestrict` lines and the `-- Dumped from/by … version`
  header lines; keep the `-- Name: …` per-object banners (review needs them).
  The `\restrict` token is **random per dump** since the Aug-2025 security
  release (CVE-2025-8714) — the single biggest determinism trap; pin *and*
  strip. `--exclude-extension` does not exist on PG15 — don't use it.
  pgvector dumps as one `CREATE EXTENSION` line (member objects excluded);
  `schema_migrations` appears in the dump — accept it, it *is* schema.
- **CI gate**: extend `db-migrations.yml` (it already owns the from-zero
  harness — dry-runs app+vector, squawk lint, prefix-uniqueness job). Add:
  the artifact job, **audit-family coverage (net-new — no `ci_audit` DB or
  audit dry-run exists today)**, and the freshness diff:
  `git add -A <artifacts> && git diff --cached --exit-code` (plain
  `--exit-code` misses brand-new untracked files), failing with the remedy
  printed (`run scripts/schema-snapshot.sh and commit`). Path-filter on
  `migrations/**` **plus the artifact paths** so hand-edits to artifacts
  alone also trip it. Expected job time ~1–2 min.
- **Artifact header** lists what a from-zero dump *cannot* contain:
  - LangGraph checkpointer tables (`checkpoints`, `checkpoint_blobs`,
    `checkpoint_writes`, `checkpoint_migrations`) — created at runtime by
    `AsyncPostgresSaver.setup()` at `src/agent.py:996-1002`, only under
    `CHECKPOINTER_BACKEND=postgres`, into the control-plane DB by default
    (dedicated `CHECKPOINT_DB_URL` supported — `src/utils/db_url.py:61-72`).
    The control-plane prune (`delete_checkpoint_thread`,
    `postgres.py:1045`) covers 3 of the 4 (not `checkpoint_migrations`).
  - Monthly audit partition children beyond the migration-seeded ones
    (runtime `CREATE TABLE … (LIKE parent)` in
    `services/audit_partitions.py:228`).
  - ~~CitationEngine DDL~~ — **correction**: the vendored engine ships *no*
    DDL and only uses SRW's migration-created `source_embeddings
    vector(4096)`; the earlier caveat is withdrawn.
- **Frozen snapshots stay, repointed**: `schema.sql`/`vector_schema.sql` are
  still load-bearing (compose initdb mounts `docker-compose.dev.yaml:53,86`;
  `init.py` vector path). In-scope follow-up: switch `orchestrator/init.py`'s
  vector init to `apply_migrations` (today it silently leaves vector at
  0001-equivalent, missing halfvec/HNSW/bitemporal/TTL) and point compose
  initdb mounts at the generated artifacts so those paths self-update.

**Acceptance:** three `*_current.sql` artifacts committed with GENERATED
headers + runtime-table list; CI fails on migration-without-regeneration
(prove once with a deliberate miss) **and** on hand-edited artifacts;
`init.py` vector path applies migrations; QW-2-class staleness structurally
impossible.

**Status (2a shipped 2026-07-03 on develop):**
`scripts/schema-snapshot.sh` generates all three `*_current.sql` artifacts
(app 3239 / vector 1187 / audit 1451 lines) — byte-deterministic (verified via
`--check`), Phase 1 end-state reflected, signatures captured (vector
halfvec×14 / HNSW×3 / `vector(4096)`; audit 4 partitioned parents). Each is
dumped inside a prod-major container (app/vector pg15, audit pg16) with the
random `\restrict` token (CVE-2025-8714) + version-header lines stripped for
determinism. CI: a new `artifact` job in `db-migrations.yml` does a REAL
from-zero apply (incl. the `.notx.sql` CONCURRENTLY files, which the existing
`--dry-run` skips) across all three families — audit coverage is net-new — and
freshness-gates via `git add -A … && git diff --cached --quiet`; the existing
`dry-run` job's wrong-major pg16→pg15 was corrected in passing. One deliberate
deviation from the flag set above: kept `COMMENT ON` (dropped `--no-comments`)
so comment-only migrations (e.g. 0036) still show up as drift.

**2b done (2026-07-03, runner-from-zero):** `init.py`'s `init_vector_db` now
builds the vector schema from the migration chain — `PostgresDB(url,
migrations_dir=MIGRATIONS_VECTOR_DIR).apply_migrations()`, and `reset_schema()`
on `--force-reset` (mirrors the app path) — instead of loading the frozen
`vector_schema.sql`, which had drifted to **0 halfvec/HNSW refs** vs the
migration-built schema's 14. The six stale initdb snapshot mounts were dropped
from all three `docker-compose*.yaml`: schema now comes from the orchestrator's
startup `apply_migrations()` (already run for all three DBs at `main.py:5414-16`)
and `python init.py`, matching k8s (which never used initdb —
`helm/.../postgres-vector.yaml`).

**Design chosen: runner-from-zero, NOT artifact+seed** — the orchestrator
already migrates every DB on startup, so the initdb mount was redundant, and a
pg_dump `--schema-only` artifact seeds no `schema_migrations` rows (which the
seed-the-ledger alternative would have to synthesize). **The frozen
`schema.sql`/`vector_schema.sql` files STAY** — tests (`test_recall_store`,
`test_user_management`, `test_schema_*`) load them directly; only their
init/deploy uses were removed. (This supersedes this doc's earlier "point
compose initdb at the generated artifacts" note — that path collides with the
`schema_migrations` ledger; runner-from-zero is cleaner.)

Verified: modified `init_vector_db` on a scratch `pgvector:pg15` →
`schema_migrations=7` (the full vector chain, tracked) on both fresh and
`--force-reset`; 33 init-importing tests pass; ruff clean; all three compose
files parse. Remaining smoke (non-blocking): a full `podman-compose up` +
`init.py` on an empty volume, when convenient.

## Phase 3 — D-5: Mongo code deletion

QW-4 shipped ~2026-06-23 and has soaked. The kill list below **supersedes the
plan's** (which undercounts the surface; the two modules alone are 1216
lines, and the archiver's Mongo code is ~half that file, not 3 lines).

**Kill list (verified 2026-07-01):**

- **Python:** `orchestrator/database/mongodb.py` (878 lines);
  `src/database/mongo_db.py` (338); the dead `mongodb = MongoDB()` global +
  import (`main.py:264,100` — zero method calls anywhere); the archiver's
  Mongo half (`src/core/archiver.py` — `_serialize_for_mongo:60-76`,
  constructor state `:258-276`, `from_env` branch `:278-320`,
  `_ensure_connected:322-368`, and the `_mongo` arm of every write method:
  `:487-545, 620-666, 879, 994-1045, 1149-1160, 1274-1287, 1422-1427`);
  the **`orchestrator/init.py` audit-Mongo family** (`get_mongodb_url`,
  `_parse_mongodb_url`, `init_mongodb`, `_create_mongodb_indexes`,
  `verify_mongodb`, `backup_mongodb`, `restore_mongodb`, `--skip-mongodb`);
  package exports (`orchestrator/database/__init__.py:44,57-58`,
  `src/database/__init__.py:41,47`); ~9 user-facing `"MongoDB not
  available"` strings in audit endpoints (`main.py:6135,11260,…,18646`).
- **The `AUDIT_BACKEND` selector dies entirely (decided 2026-07-02: "strip
  all mentions").** Postgres is the only backend, so the flag is dead
  config: delete the reader tombstone branch (`main.py:5387-5392`), the
  archiver's `from_env` backend branch (incl. its `"mongodb"` default at
  `archiver.py:290` — the local-dev footgun), and the `AUDIT_BACKEND`
  env/configmap/values plumbing (`values.yaml:487-492`, `configmap.yaml:
  29-31`, `deployment.yaml:174-178`). No tombstone: an env var nobody reads
  is noise, not UX. Keep the `audit_reader` code seam (abstraction, not a
  flag).
- **Helm/compose:** `templates/databases/mongodb.yaml`,
  `optional/mongo-express.yaml`, the internal-mongo NetworkPolicy + ingress
  slices (`databases/network-policies.yaml:145-171`, `ingress.yaml:450,480`),
  `MONGODB_URL` configmap/env (`configmap.yaml:75-76`,
  `deployment.yaml:224-228`), `srw.mongodbUrl` helper (`_helpers.tpl:533`),
  `values.yaml:556-575` + `mongoExpress` block `:1095-1097` (+
  `values.example.yaml`, `helm/ci/*-values.yaml`, `helm/README.md`), and the
  Mongo services/volumes/env in **all three** docker-compose files.
- **Ambiguous NetworkPolicies:** `agent/network-policy.yaml:116-120` and
  `workspace-network-policy.yaml:168-172` grant egress to the *internal*
  `component=mongodb` pod but are commented "datasource" — post-deletion they
  select nothing; delete or re-point deliberately.
- **Tests:** `test_database_phase1.py` (`TestMongoDB`),
  `test_llm_requests_filter.py`, `test_audit_pagination.py` (delete/rewrite);
  update Mongo-default assertions in `test_archiver_pg.py`,
  `test_auxiliary.py:734`, `test_audio_helper.py`, `test_job_access.py`.
- **Docs/docstrings:** MCP "MongoDB document ID" docstrings
  (`orchestrator/mcp/client.py:260,263,716,719`), `formatters.py:573`,
  dangling pattern-comments (`graph_routes.py:14,146`,
  `nats_bridge.py:9,55`), README + CLAUDE.md store inventory,
  `database_architecture.md:38,43,102`.
- **Keep (the datasource carve-out):** `pymongo` in requirements, the whole
  mongodb *datasource* feature (`src/tools/mongodb/`, registry entries,
  `main.py:12415,12577,12647,12820-12827,24447`, MCP ds-type list,
  `orchestrator/init.py:701,738-746` seed) and the `audit_reader` seam.
- **Pre-cutover history: decided 2026-07-02 — skip the backfill** ("we
  don't need it anymore"). Delete `scripts/import_mongo_audit_backup.py`
  with the rest; the homelab Mongo PVC can be reclaimed.

**Acceptance:** `grep -ri mongo src/ orchestrator/ helm/` returns **only**
the datasource carve-out (no tombstone, no selector, no stale docstrings);
`helm template` renders no Mongo resources on defaults; the archiver has no
backend branch; fresh k3d install green through the smoke path; docs
updated.

**✅ DONE — develop 2026-07-03** (explicit-staged away from the parallel
`config/experts/developer` + `project_loops.py` work live in the shared tree).
Deleted `orchestrator/database/mongodb.py` (878) + `src/database/mongo_db.py`
(338) + `scripts/import_mongo_audit_backup.py`; re-pointed
`FILTER_MAPPINGS`/`FilterCategory` to `audit_store` in the DB package
`__init__`. Archiver is now Postgres-only: dropped `_serialize_for_mongo`, the
Mongo constructor state + `_ensure_connected` arm + `from_env` `AUDIT_BACKEND`
branch (whose default was the local-dev footgun `mongodb`) + the `_mongo` arm of
all six write methods + the five dead pure-Mongo read methods. `init.py` lost
the whole `get/_parse/init/_create_indexes/verify/backup/restore_mongodb` family
+ `--skip-mongodb` + call sites (the datasource seed stays). `main.py` lost the
dead `mongodb = MongoDB()` global + import + the `AUDIT_BACKEND=mongodb`
tombstone; the "MongoDB not available" audit strings became "Audit store not
available". Helm: deleted `mongodb.yaml` + `mongo-express.yaml`, cut the
`databases.mongodb` / `mongoExpress` / `hosts.mongo` values, the
`AUDIT_BACKEND`/`MONGODB_URL` configmap+deployment env, the
`srw.mongodbUrl`/`srw.mongoHost` helpers, the Mongo + mongo-express
NetworkPolicy + ingress, and the two "datasource"-commented egress rules that
selected the now-gone internal pod; **`helm template` on defaults + all three CI
overlays renders 0 Mongo / 0 `AUDIT_BACKEND`, audit DB intact.** All three
compose files lost the mongo + mongo-express services/volumes/env. Tests:
deleted `test_llm_requests_filter.py`, repointed `test_audit_pagination`'s
contract onto `AuditStore.get_job_audit`, rewrote `test_auxiliary`'s
`TestArchiveError` onto a fake Postgres writer, stripped `TestMongoDB` from
`test_database_phase1`, de-staled `test_job_access`/`test_audio_helper`/
`test_archiver_pg`. 340+ pytest green, `ruff` clean, imports resolve (`MongoDB`
symbol gone from both packages). **Acceptance-grep caveat:** the frozen,
checksum-guarded migrations (`app/0001_initial.sql`, `audit/0001_initial.sql`),
the frozen `schema.sql`, and the *generated* `audit_schema_current.sql` retain
Mongo-parity `COMMENT`s by design — they document the audit wire contract's
origin and are uneditable (touching an applied migration trips the runner
checksum; hand-editing the artifact fails the Phase 2a drift gate). Everything
else the grep returns is the datasource carve-out. Pending: reclaim the homelab
Mongo PVC + a fresh-k3d smoke walk.

## Phase 4 — HF-3: atomic `jobs.context` writes (the correctness fix)

**Scope correction: 13 racy writers** — 8 `update_job_context` callers *plus
5 raw `SET context = $1::jsonb` statements* (`main.py:2639, 8410, 8882, 8939,
9823`) with the identical read-modify-write race. Helpers live at
`postgres.py:1391` (replace), `:1448` (shallow `||` merge), `:1597`
(nested-vm merge); atomic counters already exist
(`increment_job_memory_retry:1479`, `increment_job_llm_outage_attempt:1507`).

**Per-caller assignment (full table in the HF-3 research brief; the
non-obvious ones):**

- `_route_inbound_reply` (`main.py:7489-7498`) — the headline race (its RMW
  window spans an LLM triage call, i.e. seconds). Atomic append:
  ```sql
  UPDATE jobs SET context = jsonb_set(
    COALESCE(context,'{}'::jsonb), '{queued_replies}',
    COALESCE(context->'queued_replies','[]'::jsonb) || $1::jsonb)
  WHERE id = $2
  ```
  Validated end-to-end on PG 15.17 (incl. NULL column + missing key).
- **Fused-statement constraint**: `upgrade_job_to_vm` (`:8882`) and
  `_internal_resume_job` (`:8939`) fuse the context write with
  `status='paused'` (+`freeze_data`/`assigned_agent_id` resets) in ONE
  statement. A naive split into merge-helper + separate status UPDATE opens
  a dispatch window. Keep single fused UPDATEs with the merge expression
  inline (context must be visible when status flips).
- **vm-key semantics differ by caller**: the VM-recovery arm
  (`complete_job:10449`) intentionally *replaces* the whole `vm` object →
  `merge_job_context({"vm": …})`; `upgrade_job_to_vm` (`:8882`) must
  *preserve* `vm` siblings → `merge_vm_context` (or the fused inline
  equivalent). Neither helper is a universal drop-in.
- **Pop paths**: `_resume_job_on_agent` (`:2639`) pops
  `queued_feedback`/`delegation_results`. Two fixes: make it atomic
  (`context - 'queued_feedback' - 'delegation_results'`), **and move the pop
  after agent acceptance** — today the payload is deleted *before* the POST
  and permanently lost if the agent rejects (pre-existing data-loss bug). If
  the drained value is needed in the same statement, use the verified
  lock-first CTE (naive `RETURNING old.…` reads the statement snapshot and
  loses concurrent appends):
  ```sql
  WITH locked AS (SELECT id, COALESCE(context->'queued_feedback','[]'::jsonb) AS drained
                  FROM jobs WHERE id=$1 FOR UPDATE)
  UPDATE jobs j SET context = j.context - 'queued_feedback'
  FROM locked WHERE j.id = locked.id RETURNING locked.drained;
  ```
- **One new primitive**: `verification_round` (`:9823`) is the last
  non-atomic counter — add a `jsonb_set` increment cloning
  `increment_job_memory_retry` (`postgres.py:1493-1502`).
- `delegation_results` has exactly **two** writers (`:9330, :9475`), both
  bounded rebuilds from DB children — whole-key merge makes concurrent
  writers harmless duplicate writes (the plan's "unbounded growth" concern
  belongs to `queued_replies` only).
- **Retire `update_job_context` completely** — no caller is a legitimate
  whole-context replace; no `_replace_job_context` survivor needed.

**Verified SQL semantics to respect** (all empirically confirmed on PG
15.17): single-statement `SET col=f(col) WHERE id=$1` is lost-update-immune
under READ COMMITTED (EvalPlanQual); `||`/`-`/`jsonb_set` are **strict** —
the `COALESCE(context,'{}'::jsonb)` is mandatory in every expression (SQL
NULL otherwise nulls the column); `jsonb_set` with SQL-NULL `new_value`
**wipes the whole column** — validate app-side or use
`jsonb_set_lax(…,'raise_exception')`; `||` object-merges only when both
operands are objects (assert `jsonb_typeof($1)='object'` in the helper); an
array payload splices rather than nests (wrap in `jsonb_build_array` to
append-as-one). Optional hardening: migrate `jobs.context` to
`DEFAULT '{}'::jsonb NOT NULL`, and a bounded-append variant exists if
`queued_replies` ever needs a cap
(`jsonb_path_query_array(arr || $1, '$[last-N to last]')` — lax mode only).

**Tests:** mock renames in `tests/test_per_job_repo.py` (asserts survive —
they check keys the delta dicts still carry); `test_job_provisioning.py` is
the model (incl. the str-vs-UUID arg contract);
`tests/test_vm_upgrade_endpoint.py` **replays endpoint logic locally** — it
will NOT catch the `:8882` refactor unless updated in tandem. The
`queued_replies` append/pop have **zero existing tests** — the concurrency
test (two concurrent appends → both present) is greenfield.

**Acceptance:** no full-dict context writes anywhere (`update_job_context`
deleted); fused status+context sites remain single statements; pop-after-
accept ordering fixed; concurrency test green; job-lifecycle tests green.

## Phase 5 — Perf batch (HF-5 → HF-4 → HF-6 → HF-7 → HF-2)

Ordered cheapest/safest first; HF-2 last (touches the resume contract).

1. **HF-5 — per-store pools.** Wire `VECTOR_POSTGRES_MIN/MAX_CONNECTIONS`
   and `AUDIT_POSTGRES_MIN/MAX_CONNECTIONS` at the 3 constructor sites
   (`main.py:249,265,279`); plumb chart values. Suggested per-replica
   defaults: control 2/10, vector 1/5, audit 1/4 — and never let a store
   fall back to asyncpg's own defaults (`create_pool` is **min 10/max 10**).
   The real multiplier is **agent pods, not replicas**: per-agent pools
   should be min 0–1/max 2–3 with default `max_inactive_connection_lifetime`
   so idle jobs shed connections (30 agents × min-2 ≈ most of a default
   `max_connections=100` sitting idle). Budget check per server:
   Σ(pool maxes across replicas + agents) ≤ `max_connections` − 3 reserved −
   ~10 headroom. pgbouncer (transaction mode) only when agent growth breaks
   that budget — and then the advisory-lock migration runner and any LISTEN
   users stay on direct connections.
2. **HF-4 — dispatcher partial index.** The predicate risk is **cleared**:
   `get_dispatchable_jobs` (`postgres.py:2808-2862`) embeds
   `status IN ('created','paused')`, both `IS NULL`s as **literal SQL**;
   only `LIMIT $1` is bound — provable under generic plans too. DDL:
   ```sql
   -- 00NN_jobs_dispatchable_partial_idx.notx.sql
   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_dispatchable
       ON jobs (priority DESC, created_at ASC)
       WHERE assigned_agent_id IS NULL
         AND freeze_data IS NULL
         AND status IN ('created', 'paused');
   ```
   The mixed-direction key is required (a plain index can never serve
   `priority DESC, created_at ASC`). No query change; add a comment on the
   method: *statuses must stay literal — `status = ANY($1)` permanently
   disables this index.* Expectations: NOT index-only (13 projected columns;
   the `cloud_baseline <> 'seeding'` filter + correlated `NOT EXISTS` remain
   as post-index work) — the win is the pre-filtered, pre-sorted candidate
   scan with LIMIT-50 early exit. It serves *only* this query (the
   verification sweeper `postgres.py:2632-2633` lacks the `freeze_data`
   term). HOT: predicate columns count as indexed, but status flips are
   already non-HOT via `idx_jobs_status` — no regression. **SKIP LOCKED:
   rejected** — the PK-keyed CAS claim (`claim_job_for_agent:2659`, EPQ
   re-check) is already race-free and contention is ~nil; the one-statement
   SKIP LOCKED form is the documented upgrade path for a multi-dispatcher
   future. CIC failure leaves an INVALID index — drop and retry (runbook
   note in the migration).
3. **HF-6 — batch the two N+1s.** `/api/datasources` (`main.py:12570`,
   per-row check at `:12595` → up to N×(1+M) queries for non-admin
   non-creators): resolve caller visibility once via the **existing**
   `user_visible_project_ids` (`security/access.py:295`), batch
   `list_datasource_projects` over `datasource_id = ANY($1)` (mirror
   `get_audit_counts`, `audit_store.py:348`), intersect in Python.
   `/api/persistent/threads` (`main.py:16035`, per-thread
   `list_thread_mounts` at `:16054`, bounded by the LIMIT-50 in
   `list_threads`): one `thread_id = ANY($1)`. Acceptance: no query-count
   fixture exists — use the established `AsyncMock(...).call_count` idiom
   (`test_user_management.py:593` style) on the DB-method mocks.
4. **HF-7 — thread-read diet.** Correction to the plan: the display query
   (`get_thread_messages_history`, `postgres.py:3581`) selects **9 columns,
   not 15** — no `provider_raw`; the real costs are (a) `limit=None`
   full-transcript load on thread open (`main.py:16946-16950`), (b) a
   forced Sort — `ORDER BY created_at, turn_number, id` has no matching
   index (0020 leads `turn_number`) → switch to the tie-free
   `(thread_id, seq)` keyset (0023, already the resume ordering), (c) the
   unconditional `COUNT(*)` (`:3683`, called at `main.py:16954`) feeding a
   `total` field **the cockpit never reads**
   (`persistent-chat.service.ts:748` uses only `.messages`) — drop it or
   make it first-page-only. Default thread-open to server-side newest-N.
   The fat 15-column SELECT is the **agent resume query**
   (`src/database/postgres_db.py:386-393`): `_db_rows_to_lc_messages` +
   its caller consume only 5 (role, content, tool_calls, tool_call_id,
   turn_number — keep `turn_number`, the restore caller reads it) — trim
   the other 10.
5. **HF-2 — session write path.** Current shape (verified): per-message
   upsert (`ON CONFLICT (id) DO UPDATE … RETURNING id, seq`) + an
   unconditional `UPDATE threads` bump (`postgres_db.py:568-623`), fired
   incrementally after every append AND re-fired row-by-row (serial awaits,
   no skip logic) by the turn-complete reconcile
   (`persistent_app.py:4047-4090`) ≈ **~82 round-trips per 10-tool-call
   turn**. The refactor, per researched mechanics:
   - Per-message hot path → **one data-modifying CTE** (INSERT/upsert
     message + `threads` bump in a single statement, 1 round trip, atomic).
     Or drop the per-message bump to once-per-turn — verified safe:
     workspace suspension gates on thread `status='ended'`, not
     `last_activity`; only cosmetic mid-turn freshness is lost (SSE covers
     live display).
   - Turn-complete reconcile → **one `INSERT … SELECT unnest($1::…[], …) ON
     CONFLICT (id) DO UPDATE`** statement (asyncpg `executemany` ≥0.22 —
     pipelined, atomic — is the acceptable simpler fallback). Must preserve
     upsert semantics and `seq` stability (the resume cursor), and must
     still carry turn `metrics` + `tool_decisions` — AI rows genuinely
     change at reconcile; only decision-free tool rows are skippable.
   - **The incremental per-message persists STAY** — they are the mid-turn
     durability path (message-granular persistence). Batch only the
     reconcile. Optional: wrap mid-turn persists in
     `BEGIN; SET LOCAL synchronous_commit=off; …; COMMIT` (bounded ≤600ms
     *ordered* loss, server-crash-only); keep turn-boundary writes fully
     synchronous.
   - Note: a second, cold-path `save_thread_message` exists orchestrator-side
     (`postgres.py:3497-3564`, plain INSERT + same bump) — off the hot path;
     align opportunistically.
   - **Regression gate, corrected:** there is no automated mid-turn-kill
     test (deferred to k3d in the persistence-slices doc). The behavioral
     resume/`seq`-cursor/idempotency tests must stay green; the mechanical
     count assertions (`test_persistent_app.py:716,729`,
     `test_postgres_db_save_message.py:89` — coupled to the old write shape)
     get rewritten to the batched shape, deliberately.

**Acceptance (batch-wide):** EXPLAIN before/after for HF-4 (LIMIT 50, the
dispatcher's actual call — `main.py:4152`); call-count assertions for HF-6;
session smoke + resume tests + k3d kill-mid-turn drill for HF-7/HF-2.

## Phase 6 — Usage rollup + the no-deletion policy (D-1 build; D-2 closed by policy)

**Decided 2026-07-02: automatic data deletion is rejected as policy, not
deferred as work.** Nothing in the product deletes operational data on a
timer. Rationale (owner call): storage is abundant (homelab: 8TB NVMe +
12TB SATA SSD + 96TB HDD; ~32TB S3 backup on Garage even with erasure
coding), text is cheap, and audit history is worth more than the disk it
sits on. Deletion is always **manual and export-first** (detach → export to
a DWH/data-lake → only then remove from prod). Revisit only when SaaS/GDPR
makes deletion a compliance requirement.

What this changes versus the researched retention plan:

- **`retire_partitions` stays a stub — permanently, by policy.** Re-document
  the module docstring + `maintenance_pass` comment (`audit_partitions.py:
  252-275, 417`) and the `PARENTS` "retention matrix" as *no-auto-deletion
  policy*, not "deferred (PR-1 lean cut)". The retention-window numbers stop
  being promises.
- **The manual-path recipe is preserved here** for when export-first removal
  is ever wanted (it was researched and is sound): heal any
  `pg_inherits.inhdetachpending` straggler with `FINALIZE` first, pick
  candidates from `pg_get_expr(relpartbound, oid)` bounds (never names,
  never the last child, never the now() partition, alarm on any DEFAULT
  partition), `DETACH PARTITION … CONCURRENTLY` on a bare no-transaction
  connection, export the standalone table, then drop it (no parent lock).
- Partition **creation, ANALYZE, lookahead alarms, and `partition_status`**
  all stay — monthly partitions still buy query pruning and are the natural
  unit for a future manual export. Add per-parent **size reporting** to
  `partition_status` (`pg_total_relation_size` per parent) so growth is
  *visible* instead of managed; alert thresholds can come later.
- The `usage_events`-fenced-by-rollup-watermark trap is moot (nothing
  drops), but keep the fence as a code comment next to `PARENTS` so a future
  SaaS-era retention implementer inherits the warning.
- **Doc corrections become the D-2 deliverable**: `database_architecture.md`
  (the 90/90/365d retention promises + the "retention-dropped" store
  characterization), the audit migration headers, `observability_and_quotas.md`,
  and the `audit_partitions.py` docstrings all get the policy stated instead
  of unenforced windows.

**Rollup design (researched; confirmed build — "aggregation on top to speed
up queries" is exactly the approved shape):** `usage_daily` + a `rollup_state` watermark
row, both in the **app DB** (watermark and upsert commit in one transaction
— the cross-DB exactly-once trick). Daily task + startup catch-up:
re-aggregate from `last_closed_day` (inclusive — 1-day overlap re-closes the
boundary day) through today with ONE statement against the still-attached
`usage_events` parent (partition pruning handles it); **full-replace
upserts** (`ON CONFLICT (day,dims) DO UPDATE SET … = EXCLUDED.…` — never
additive, retries must be idempotent); advance the watermark only past days
older than a ~15min safety lag; weekly wide catch-up (re-close last 7 days)
covers late arrivals. Suggested dims: `(day, user_id, project_id, category,
resource, unit)` — **per-job cost queries stay on raw `usage_events`**,
which is already indexed for it (`usage_events_ref_idx (ref_id, ts)`) and
already carries attribution (see G3 correction). Point `/api/usage` (and
timeseries) at the rollup for closed days, raw for today.

**Also in scope:** sweep every phantom reference — `usage_daily`
(`audit_partitions.py:58`; `audit/0002:24,82,115`;
`database_architecture.md:36,97`; `observability_and_quotas.md` ×8; both
roadmap/plan docs) and `quota_limits` (`database_architecture.md:36`;
`observability_and_quotas.md` ×3; `docs/done/global_expert_management.md`
×4) — build or correct each. Correct the `usage_rates` framing while there:
it is **no longer ships-empty/admin-seeded** — `openrouter_pricing.
llm_pricing_sync_loop` auto-seeds effective-dated LLM rates.

**Test seeds:** generalize the private `_ensure_prev_month_partition`
(`tests/test_audit_store.py:744-777`, usage_events-only, previous-month-only)
into an N-months-back/any-parent helper — now for rollup catch-up drills
(seed backdated events → catch-up rolls them up correctly).

**Acceptance:** `usage_daily` reconciles with raw aggregation on a seeded
window including a backdated month; `/api/usage` serves the rollup for
closed days and raw for today; `retire_partitions` carries the policy
docstring; `partition_status` reports per-parent sizes; architecture doc
contains no phantom-table claims and no unenforced retention promises.

## Phase 7 — Unification track (direction A; B stays gated)

Two premises of `unified_message_store.md` fell to the research sweep (its
Corrections block records them):

1. **The stores already overlap at the write layer.** Since `986117f1`
   (2026-06-12), every session turn cascades into `chat_history`
   (`_loop_archive_llm_call` → `archiver.archive(call_type='main')` →
   `_archive_chat_entry` — no agent-type guard; rows keyed by thread_id,
   written but never read by the session UI). Direction A must decide these
   writes' fate (stop the cascade, or embrace it as the uniform audit
   projection — the latter actually *simplifies* convergence).
2. **The "0019 canonical shape" is unbuilt.** The component columns
   (`reasoning/tool_results/provider/provider_raw/…`) are never written
   (no `save_thread_message` caller passes them), never read (the display
   query omits them), and the intended builder
   (`src/llm/session_components.py` `MessageComponents`) has zero production
   callers. A-scope = **wire the shape for the first time** (writer + reader
   + renderer), not "adopt the existing substrate."

**The driver is confirmed and asymmetric:** the two reasoning-capture
implementations the 06-22 regression hit are the worker's single-source
capture (`archiver.py:867-877` — `additional_kwargs.reasoning_content` only)
vs the session's three-source `_extract_thinking`
(`persistent_app.py:3933-3971` — Anthropic thinking blocks + Responses-API
reasoning blocks + reasoning_content). The canonical extractor adopts the
session's 3-source logic. The duplicated cockpit logic to collapse:
reasoning render (`persistent-chat.service.ts:2563-2573` ⇄
`chat-history.component.ts:123-131`), tool-call pairing (id-Map `:2536-2544`
⇄ cross-entry scan `:772-787`), content-part/role handling. No shared types
exist today on either side — the canonical model grows from
`MessageComponents` (Py) + one `Turn`-shaped TS type.

Worth evaluating in the design doc: rendering worker job-chat from
`llm_requests` (full untruncated bodies; `chat_history.request_id` soft-refs
it; the cockpit already drills into it) instead of upgrading `chat_history`.

**B (gated on G4):** unchanged — only with a product driver (worker-resume
ask, or the self-improvement loop needing durable queryable job
conversation). B inherits A's model.

**Acceptance (A):** one capture path + one formatter exercised by both
surfaces; a provider-reasoning fixture test against the shared path (next
capture regression breaks one test, not two surfaces); 0019 columns written
and read in production; the session→`chat_history` write-path decision
recorded and implemented.

---

## Decision gates — ALL DECIDED 2026-07-02 (owner call; kept as the record)

| Gate | Question | Decision |
|---|---|---|
| **G1** | `capability_grant_audit`: drop or wire a reader? | **Drop** — table + both INSERTs go (Phase 1 / QW-6). Grants are re-derivable current state; re-add on a concrete compliance ask. |
| **G2** | Messaging trio / email subsystem (D-4) — owner or drop? | **Keep, owned.** Notifications, permission requests, and the AI ask-questions flow are on the feature list — the trio + IMAP poller are product surface, not an experiment. The M1 hardening (0037/0038) stands. |
| **G3** | Token-ledger consolidation (D-3) | **Closed — keep both.** `llm_requests` = request-level audit record; `usage_events` = the cost ledger (already job-attributed via `audit_usage.py:219-236`). Token overlap accepted; no consolidation work. |
| **G4** | Unification direction B (jobs adopt `thread_messages`)? | **Deferred** — stay on A; revisit when a product driver (worker resume, loop observability) is concrete. |
| **Phase 6 go/no-go** | Build retention + rollup, or document-only? | **Superseded by the no-deletion policy**: build the rollup (D-1); close retention (D-2) as policy, never as a timer. See Phase 6. |

Smaller defaults confirmed the same day: skip the pre-cutover Mongo backfill
("we don't need it anymore"); strip **all** Mongo mentions incl. the
`AUDIT_BACKEND` selector (Phase 3); QW-5 full clean cut, `jobs.context`
NOT-NULL hardening, and deferring `synchronous_commit=off` to measurement
all ride as previously proposed.

## Opportunistic / at-scale (no phase)

- **D-7** — `mcp_tokens` → `auth_tokens` naming residue: fold into whichever
  phase next touches those files. Never a standalone PR.
- **D-8** — dispatcher ancestor-walk: the recursive CTE is a **per-row
  correlated subplan** (re-executes per candidate; fine at LIMIT 50 +
  shallow trees). If EXPLAIN ever shows it hurting: invert to one
  closure-per-poll (seed from blocked jobs, descend via `parent_job_id`) or
  a denormalized blocked flag. Take the baseline while doing HF-4.
- **`idx_jobs_priority` drop** — after HF-4 soaks, if `idx_scan` confirms.

## Non-goals — owned elsewhere (unchanged from the plan)

- **Vector tier** internals (halfvec/HNSW, dedup, bi-temporal) — memory-overhaul track.
- **`srw-litellmdb`** — vendor/Prisma-owned; correctly separate.
- **CitationEngine dimension conflict** — upstream concern; note the
  vendored engine ships no DDL (earlier runtime-table caveat withdrawn).
- **HA/CloudNativePG adoption** — `high_availability_setup.md`; this roadmap
  feeds it HF-5 (pool tunability) only.
- **Checkpointer backend default** — the D3 cross-pod-resume track.

## New since the 2026-06-22 audit (fold into the next sweep)

- App migrations 0033–0041: `usage_rates` (now auto-seeded by the OpenRouter
  pricing sync), `workspace_intervals`, `project_loops` (+ per-loop
  `workspace_backend`), `processed_inbound_emails`,
  `thread_notifications_sent` unique, `sudo_request` reply-subject unique,
  account-model-defaults drop.
- **LangGraph checkpointer tables** in the control plane (runtime-created,
  see Phase 2). Operational note from research: ~100 rows per graph run,
  no built-in TTL — our terminal-job pruning is the vendor-recommended
  pattern; expect these tables to top control-plane autovacuum (delete
  churn) and watch dead-tuple ratios there first.

## Grounding (researched 2026-07-01, key sources)

- **Schema artifact**: pg_dump determinism + the random-`\restrict` trap
  (CVE-2025-8714; `--restrict-key` exists on PG15) —
  postgresql.org/docs/15/app-pgdump.html; Rails `structure.sql`
  post-processing; GitLab db:check-schema; migra is deprecated (2022) —
  byte-compare wins over semantic diff here.
- **Retention**: DETACH CONCURRENTLY two-transaction protocol + FINALIZE +
  no-DEFAULT-partition restriction — postgresql.org/docs/15/sql-altertable.html,
  postgres commit 71f4c8c6; pg_partman retention semantics (bounds-based,
  keep-table default); rollup watermark/idempotent-upsert pattern — Citus
  incremental aggregation, Crunchy pg_incremental.
- **JSONB atomicity**: READ COMMITTED/EvalPlanQual semantics —
  postgresql.org/docs/15/transaction-iso.html; strictness of `||`/`-`/
  `jsonb_set` (+ `jsonb_set_lax`) — docs §9.16 + the 2019 "jsonb_set
  strictness considered harmful" thread; all patterns in Phase 4 verified
  live on PG 15.17.
- **Partial index**: predicate-proof rules (`predtest.c` REL_15_STABLE —
  Const-only proofs, `MAX_SAOP_ARRAY_SIZE=100`, Params defeat generic-plan
  proof); mixed-direction ordering — docs/indexes-ordering.html; HOT +
  predicate columns — README.HOT; SKIP LOCKED queue canon (Ringer/2ndQuadrant,
  Crunchy) — rejected here on contention grounds.
- **Pools/batching**: HikariCP pool-sizing essay + PG wiki
  Number_Of_Database_Connections; Andres Freund's snapshot-scalability
  measurements (idle connections tax active queries; <2MiB each); asyncpg
  ≥0.22 `executemany` (pipelined, atomic), no pipeline mode (#839);
  `synchronous_commit=off` = bounded ordered loss ≤3×`wal_writer_delay` —
  docs/wal-async-commit.html; pgbouncer transaction-mode caveats (prepared
  statements, session advisory locks "Never").

Full per-claim URL lists live in the 11 research briefs (session transcript,
2026-07-01); the load-bearing facts are restated inline above.

## Maintenance protocol

- Status lives **here**: when an item ships, update the phase-overview row
  (and section) with the commit hash. Don't fork status into the plan doc.
- The plan doc stays the evidence record; append new findings there,
  sequence them here. Known-stale plan evidence (line numbers, the D-3
  job-attribution claim, HF-7's 15-column claim) is corrected inline above
  and annotated there.
- An item is "done" when its acceptance criteria are verified on k3d per the
  CLAUDE.md Plan → Develop → Verify loop — not when the code merges.

## End state (definition of done, phases 1–6)

- Orchestrator boots with a clean schema check driven by generated,
  CI-enforced artifacts; every install path (helm, compose, init.py) derives
  from migrations or generated artifacts — frozen snapshots can no longer
  mislead.
- One audit backend (Postgres) end to end — no selector flag, no tombstone —
  with the Mongo surface reduced to the datasource connector feature only.
- No lost-update races on `jobs.context`: every write is a single atomic
  statement, fused where status must flip in the same statement, with the
  drain/pop ordering fixed.
- The four hottest paths (dispatch poll, datasource list, thread open,
  session persist) are index-backed / batched / O(1)-query, verified by
  EXPLAIN or call-count assertions.
- The usage rollup runs and serves `/api/usage` for closed days; the
  no-deletion policy is stated wherever retention used to be promised;
  partition sizes are visible in `partition_status`; every table in the
  architecture doc exists, and every existing table has a reader or a
  dropped-by migration.
