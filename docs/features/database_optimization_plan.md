# Database optimization plan — quick wins, harder fixes, deferred

**Status: Backlog / actionable plan (2026-06-22).** Findings from a 5-agent
read-only audit of the database surface (schema, consolidation, query/access
patterns) run after the usage-metering + LiteLLM monitoring tier landed.
Nothing here is executed yet; this is the prioritized work list. Items are
tiered by value-vs-effort and tagged with evidence, effort (S/M/L), and risk.

**Origin:** 2026-06-22 database-optimization sweep. Absorbs and extends the
open-items backlog in `../issues/db_schema_hygiene.md` (mapping at the end).

**Companion docs:** `database_architecture.md` (the store/tier rules every fix
here must respect), `../issues/db_schema_hygiene.md` (origin of the drift +
dead-code items), `unified_message_store.md` (the `chat_history` ↔
`thread_messages` convergence — referenced under Deferred, still undecided),
`observability_and_quotas.md` + `postgres_audit_store_implementation.md` (the
metering/audit tier these findings probe).

## Context: the store inventory grew to 7

The metering work added a **7th database** — `srw-litellmdb` (LiteLLM gateway's
own Prisma-owned Postgres). Current servers: `srw-postgres` (control plane),
`srw-vector`, `srw-auditdb`, `srw-keycloakdb`, `srw-litellmdb`, MongoDB
(decided 2026-06-23: **being removed** — QW-4 + D-5), Neo4j (on probation). The audit Mongo→Postgres cutover is complete
(`backend: postgres` default). See `database_architecture.md` for the tier
rules; this plan stays within them (it does **not** propose merging stores).

## Decisions

- **2026-06-23 — Remove MongoDB; Postgres is the single observability backend.**
  We evaluated keeping Mongo as a permanent, chart-selectable dual-path backend
  and rejected it. For this workload (append-only, partitioned, retention-bounded,
  job-scoped reads) Mongo offers no performance win over partitioned Postgres;
  the real scale-up path for the observability tier is columnar/TSDB
  (TimescaleDB/ClickHouse — already the named upgrade trigger in
  `database_architecture.md`, not a document store); and the metering roadmap
  (per-job cost, rollups, ledger consolidation — D-1/D-3) wants relational joins
  a dual-path would cap to a lowest-common-denominator. A live second backend is
  a permanent tax (two data models, two retention models, double testing) that
  **already rotted** — the QW-1 N+1 fix silently broke the Mongo reader because
  nothing tests it. Bundling Mongo (SSPL) also re-opens the
  redistribution-licensing question the cutover existed to close.
  - **Keep:** the `audit_reader` abstraction as an extension seam.
  - **Remove:** the live Mongo implementation and the `mongodb` backend value (QW-4 + D-5).
  - **Re-add rule:** only on a concrete customer mandate, and as BYO/external
    (customer-run, never bundled). Optionality is cheap to re-add through the seam
    and expensive to maintain live.

## At a glance

| ID | Item | Tier | Effort | Risk |
|---|---|---|---|---|
| QW-1 | `GET /api/jobs` audit-count N+1 → existing `get_audit_counts()` — ✅ done | Quick win | S | Low |
| QW-2 | `REQUIRED_TABLES` live false-warning + stale entries | Quick win | S | None |
| QW-3 | Drop two redundant indexes (`threads`, `thread_messages`) | Quick win | S | Low |
| QW-4 | MongoDB removal step 1 — disable + Postgres-only reader | Quick win | S | Low |
| QW-5 | Delete dead password-auth methods (+ drop columns) | Quick win | S | Low |
| QW-6 | `capability_grant_audit` write-only table (drop or wire) | Quick win | S | Low |
| HF-1 | Generated current-schema artifact (drift root-cause fix) | Harder | M | Low |
| HF-2 | Persistent-session write path (round-trips/turn) | Harder | M | Med |
| HF-3 | `update_job_context` → atomic merge (write-amp + race) | Harder | M | Med |
| HF-4 | Dispatcher poll partial index | Harder | S–M | Low |
| HF-5 | Per-store connection-pool env overrides | Harder | S | Low |
| HF-6 | Remaining N+1s (`/api/datasources`, threads mounts) | Harder | M | Low |
| HF-7 | Thread-read fat queries (cursorless load, COUNT, sort) | Harder | M | Med |
| D-1 | Phantom `usage_daily` / `quota_limits` — build or document | Deferred/decision | — | — |
| D-2 | Audit retention (`retire_partitions` no-op) — wire or accept | Deferred/decision | M | — |
| D-3 | Token-ledger consolidation (`llm_requests` vs `usage_events`) | Deferred/decision | L | — |
| D-4 | Messaging trio — owner or drop (product call) | Deferred/decision | — | — |
| D-5 | MongoDB removal step 2 — delete all Mongo code/chart/deps (after soak) | Committed | M | Low |
| D-6 | `chat_history` ↔ `thread_messages` convergence | Deferred/decision | L | — |
| D-7 | `mcp_tokens` naming residue over `auth_tokens` | Deferred | S | Low |
| D-8 | Dispatcher per-row recursive-CTE rewrite (at scale) | Deferred | M | Med |

---

## Tier 1 — Quick wins (low effort, low risk, mechanical)

### QW-1 · `GET /api/jobs` audit-count N+1 — ✅ done
**Shipped: commit `0c3ba669`, verified 2026-06-23.** Both list endpoints now
issue one batched `get_audit_counts([...])` (`main.py:5613-5621`, `:22484-22492`)
instead of a per-row loop; the implementation handles empty lists + non-UUID ids
and is exception-safe (`audit_store.py:306-332`). The remaining singular
`get_audit_count` at `main.py:5701` is correct — that's the single-job
`GET /api/jobs/{id}`, not a loop.
- Edge case found: the Mongo reader lacks `get_audit_counts`, so the batched call
  would 500 under `AUDIT_BACKEND=mongodb`. **Resolved by removing Mongo (QW-4/D-5),
  not by adding Mongo code** — see Decisions.

### QW-2 · `REQUIRED_TABLES` stale boot check
`verify_schema()`'s allow-list still contains `"sessions"` (dropped by migration
0009) → spurious `Missing tables: sessions` **every orchestrator boot**, and it
validates only 11 of 40 live tables (simultaneously wrong and nearly useless).
The vector equivalent lists runner-owned `schema_migrations`.
- **Fix:** remove `"sessions"` (min), drop `schema_migrations` from `VECTOR_REQUIRED_TABLES`; ideally regenerate or delete the relic check (HF-1 supersedes it). Evidence: `postgres.py:187-199`, `postgres.py:211`.

### QW-3 · Redundant indexes on the two hottest write tables
- `threads`: `idx_threads_user` (0001) and `idx_threads_user_id` (0012) are a **true duplicate** on `(user_id)` — drop `idx_threads_user_id`.
- `thread_messages`: `idx_thread_messages_thread` (0001, `(thread_id)`) is a prefix-subset of `idx_thread_messages_thread_turn_created` (0020) and `idx_thread_messages_thread_seq` (0023) — drop it.
- **Fix:** two small `DROP INDEX` migrations (`.notx.sql`, `CONCURRENTLY`). Confirm no index-name-specific code first. Evidence: `0001_initial.sql:861,897`, `0012_threads_user_id_index.notx.sql`.

### QW-4 · MongoDB removal, step 1 — disable + make Postgres the only reader
Post-cutover Mongo is already unreachable in the default `backend: postgres`
config (no dual-write; `connect()` is failure-tolerant so disabling can't break
boot). All four audit invariants verified intact (append-only `agent_audit`, no
GIN-on-payload, no cross-table FK, single-write-per-flag).
- **Fix:** (1) `databases.mongodb.enabled: false` in the values overlays + drop `MONGODB_URL` env/configmap; (2) flip the module default `audit_reader = mongodb` (`main.py:289`) to the Postgres store and remove the `mongodb` branch from reader selection, so `AUDIT_BACKEND=mongodb` is no longer a half-supported state (loud error or warn-and-use-Postgres, never a silent broken path); (3) guard/remove the unconditional `mongodb.connect()` / `ensure_indexes()` boot calls. Evidence: `helm/values.yaml:502`, `main.py:289,4864,4905`.
- This also closes the QW-1 dual-path edge case (the Mongo reader lacks `get_audit_counts`) by removing the path rather than adding Mongo code.
- **Precondition before destroying data:** if pre-cutover audit history matters, run the one-shot `import_mongo_audit_backup.py` backfill into Postgres before the Mongo PVC is reclaimed. Audit is non-load-bearing observability, so this is optional if the history isn't wanted.

### QW-5 · Delete dead password-auth family
Pre-Keycloak password methods with zero production callers (verified): `set_user_password`, `set_email_verified`, `create_user_with_password`, `get_user_by_email_with_auth` (test-only), **and `upsert_default_user`** (test-only — not in the hygiene doc; the last writer of `password_hash`/`email_verified` outside the relic block). Live user creation is `upsert_user_from_oidc` + `create_user_with_default_project`, neither touches passwords.
- **Fix:** delete the methods + their tests; **keep** `migrate_existing_users_verified` (live one-shot startup backfill at `postgres.py:7783`, or fold into a migration). Then a migration dropping `users.password_hash` / `users.email_verified`. Evidence: `postgres.py:4140-4256, 6921-7003`.

### QW-6 · `capability_grant_audit` write-only table
Two INSERTs (`set_grant`/`delete_grant`), **zero readers** — no SELECT, no
endpoint, no UI. Audit trail written but never consumed (new in 0030).
- **Fix (decide):** drop the table + the two INSERTs, **or** wire an admin audit view if the trail is wanted. Lean drop unless there's a near-term consumer. Evidence: `postgres.py:8600,8638`, `0030_capability_grants.sql:36`.

---

## Tier 2 — Harder fixes (medium effort, or need care/verification)

### HF-1 · Generated current-schema artifact (drift root-cause)
The frozen snapshots are badly stale and actively misleading: `schema.sql` is
**22 tables off** (4 phantom: `sessions`, `builder_sessions`,
`builder_messages`, `mcp_tokens`; + 18 live tables absent); `vector_schema.sql`
is byte-identical to its 0001 (missing all of 0002–0006). This drift is the
root cause that lets dead code hide (it's how the `sessions` methods survived).
- **Fix:** CI step / make target that migrates-from-zero against a scratch Postgres and commits `pg_dump --schema-only` as read-only, clearly-marked `schema_current.sql` / `vector_schema_current.sql` (audit family joins automatically). Auto-catches QW-2. This is hygiene-doc **item 1**, now quantified. ~half day.

### HF-2 · Persistent-session write path
~4 serial awaited round-trips per message: `save_thread_message` does
`INSERT…RETURNING` **plus** a separate per-message `UPDATE threads`
(last_activity/total_turns), and each AI/tool message is written twice
(incremental `persist_message` + turn-complete reconcile loop, per-row await).
A 10-tool-call turn (~21 msgs) ≈ ~80 serial round-trips on the hottest write
path.
- **Fix:** move the `threads` bump to once-per-turn (drop it from `save_thread_message`); collapse the reconcile into one `executemany`/multi-row upsert; skip rows already persisted unchanged. Evidence: `src/database/postgres_db.py:521-624`, `persistent_app.py:3961-4004`, `persistent_graph.py:574,727`.
- **Care:** must not regress message-granular persistence / faithful resume (see the persistence-slices work).

### HF-3 · `update_job_context` → atomic merge (write-amp + lost-update race)
~11 callers do `SET context = $1` (full re-serialized dict) instead of the
atomic `merge_job_context` (`context = COALESCE(context,'{}') || $1::jsonb`)
sitting right beside it. Worst offender: `_route_inbound_reply` (`main.py:6909`)
— an unbounded-growth read-modify-append of `queued_replies` with a genuine
concurrent-reply lost-update race. Several callers rewrite the large
`delegation_results` list.
- **Fix:** route callers to `merge_job_context` / `merge_vm_context` (and `context - 'key'` for pop paths); rewrite the inbound-reply append as atomic `jsonb_set(... || $1::jsonb)` with no read. Deprecate the raw `update_job_context`. Correctness **and** perf. Evidence: `postgres.py:1345`, callers at `main.py:6909,8747,8888,8622,9440,7834,8363,2274,8306`.

### HF-4 · Dispatcher poll partial index
`get_dispatchable_jobs` filters `status IN ('created','paused') AND
assigned_agent_id IS NULL AND freeze_data IS NULL` with only single-column
indexes, then sorts `priority DESC, created_at ASC`. Fires event-driven on every
job lifecycle transition + a 30s catch-all.
- **Fix:** partial index `ON jobs (priority DESC, created_at ASC) WHERE assigned_agent_id IS NULL AND freeze_data IS NULL AND status IN ('created','paused')` to make the candidate scan + sort index-only. Verify with `EXPLAIN`. Evidence: `postgres.py:2449-2503`. (The per-row recursive-CTE ancestor walk is the bigger half — D-8.)

### HF-5 · Per-store connection-pool env overrides
The 3 orchestrator Postgres pools (control/vector/audit) all read the **same**
`POSTGRES_MIN/MAX_CONNECTIONS` (default 2/10) — vector and audit silently get a
control-plane-sized pool and can't be tuned independently; 3×10/replica will
pressure `max_connections` as replicas + the LiteLLM DB are added.
- **Fix:** wire distinct envs (`VECTOR_POSTGRES_MAX_CONNECTIONS`, `AUDIT_POSTGRES_MAX_CONNECTIONS`) at the 3 call sites (constructor already accepts explicit `max_connections`). Evidence: `main.py:249,265,279`, `postgres.py:312-316`.

### HF-6 · Remaining N+1s
- `GET /api/datasources` — worst fan-out: per-row `user_can_access_datasource` → `list_datasource_projects` (1/ds) + nested `get_user_role_in_project` (M/ds) ≈ N×(1+M). Batch with `= ANY($1)` + resolve caller roles once via `user_visible_project_ids` → ~2 queries. Evidence: `main.py:11503,11526`, `security/access.py:746,763,777`.
- `GET /api/persistent/threads` — per-thread `list_thread_mounts` (capped at 50). Batch with `WHERE thread_id = ANY($1)`. Evidence: `main.py:14729,14742`.

### HF-7 · Thread-read fat queries
- Cursorless thread-open (`get_thread_messages_history(limit=None)`) loads the **entire** transcript with all 15 columns incl. `provider_raw`/6 JSONB, plus an unconditional full-scan `SELECT COUNT(*)` for a UI counter. Make the COUNT conditional/first-page-only; default to a server-side newest-N load. Evidence: `main.py:15505,15564`, `postgres.py:3134`.
- Display queries order by `created_at, turn_number, id` — no matching index (leading col of the composite is `turn_number`), forcing a Sort of wide rows. Switch to the tie-free `seq` keyset (already the resume cursor). Evidence: `postgres.py:3160,3207`.
- Resume SELECTs all 15 wide columns for up to 1000 rows; trim to what `_db_rows_to_lc_messages` consumes (verify first). Evidence: `src/database/postgres_db.py:386-393`.

---

## Tier 3 — Deferred / needs a decision

### D-1 · Phantom `usage_daily` / `quota_limits` tables
Both are referenced as existing app-DB tables in `database_architecture.md` and
4+ code comments, but **no migration creates them and nothing reads/writes
them**. `/api/usage` aggregates raw `usage_events` on every call; the enforcing
quota is env-driven (`LITELLM_QUOTA`), not a table.
- **Decision:** build the `usage_daily` rollup (the doc-mandated Cockpit surface + the safety precondition for D-2 retention), or correct the architecture doc/comments to reflect raw-event aggregation as reality. Evidence: grep returns docs/comments only; `usage_ledger.py:239-303`, `audit_partitions.py:56-60`.

### D-2 · Audit retention is a no-op stub
`retire_partitions()` is a documented no-op ("PR-1 lean cut") — the 90/90/365d
retention promised by the architecture doc + migration headers is **not
enforced**; partitions accumulate forever for all four audit parents. Creation /
ANALYZE / lookahead alarms are complete (incl. `usage_events`); only the drop
half is missing.
- **Decision:** wire `DETACH/DROP` per retention (no FKs block it — verified), or formally accept deferral at thesis/dev scale. Tied to D-1 (`usage_daily` rollup is the stated precondition for dropping `usage_events` raw). Don't justify the Mongo exit on "Postgres gives retention" until wired. Evidence: `audit_partitions.py:252-275`.

### D-3 · Token-ledger consolidation
The same token-usage fact lands in three places: LiteLLM spend log (transient,
enforcement-only), `usage_events` (canonical), and the still-written
`llm_requests.metrics.token_usage`. The first two are a clean intentional
two-tier design; `llm_requests` is the transitional overlap — and it's the
**only one carrying `job_id`** (the per-job LLM-cost attribution gap).
- **Decision:** converge `llm_requests` token data into `usage_events` (adding job attribution there), or keep `llm_requests` as the per-job audit record and accept the overlap. Relates to D-6. Evidence: `archiver.py:443`, `persistent_graph.py:1294`, `observability_and_quotas.md:286`.

### D-4 · Messaging trio — owner or drop
`message_log` / `external_contacts` / `notification_queue` are still written
only by the IMAP-poller path, **but** have live read endpoints
(`/api/projects/{id}/contacts`, `/api/notifications/{id}`) — so not
grep-removable. Product call: give email/messaging an owner, or drop the
subsystem (poller + 3 tables + ~15 methods + endpoints). Hygiene-doc item 3,
unchanged. Evidence: `services/imap_poller.py`, `postgres.py:7842-8526`.

### D-5 · MongoDB removal, step 2 — delete all Mongo code/chart/deps (committed; after QW-4 soak)
**Committed 2026-06-23 (see Decisions) — no longer pending a decision, just
sequenced after a Postgres-only soak.** Once QW-4 has soaked, delete the Mongo
surface entirely:
- **Code:** `src/database/mongo_db.py`, `orchestrator/database/mongodb.py` (~1060 lines), the archiver Mongo branches (`src/core/archiver.py`), and the now-dead `AUDIT_BACKEND` / `audit_reader` mongodb selection logic.
- **Chart:** the `databases.mongodb` block + StatefulSet/NetworkPolicy template, `mongo-express` (UI + ingress), and the `MONGODB_URL` env/configmap/secret keys.
- **Deps/tests:** `pymongo` from requirements, any Mongo-specific tests.
- **Docs:** make `database_architecture.md`'s "MongoDB disappears at audit-store cutover" literally true; drop Mongo from the store inventory (here, the README, CLAUDE.md).
- **Keep** the `audit_reader` abstraction (the extension seam — see Decisions re-add rule).

Nothing functional blocks any of this. Evidence: per Mongo-tier audit.

### D-6 · `chat_history` ↔ `thread_messages` convergence
Already captured as an undecided idea in `unified_message_store.md` (one message
model for worker jobs + sessions). Nothing in the audit tier contradicts it; the
byte-parity audit migration was kept to keep the door open. Referenced here for
completeness — see that doc; still undecided.

### D-7 · `mcp_tokens` naming residue
Methods/routes still speak "mcp_tokens" over the `auth_tokens` table (renamed in
migration 0010). Functional, just misnamed. Rename opportunistically. Hygiene
item 4. Evidence: `postgres.py:4258-4390`, `main.py:18757+`.

### D-8 · Dispatcher per-row recursive-CTE rewrite
`get_dispatchable_jobs` runs a per-row recursive CTE ancestor walk for the
blocked-by-parent check. Fine now; if the jobs table grows, materialize the
ancestor-blocked state instead. Verify with `EXPLAIN` on a large table. Pairs
with HF-4. Evidence: `postgres.py:2449-2503`.

---

## Owned elsewhere / no action

- **`srw-litellmdb`** — correctly earns its own server (vendor/Prisma-owned schema, the `keycloakdb` pattern). Not a consolidation target; do **not** put `usage_events` in it.
- **Vector tier** — halfvec/HNSW indexes, the inert pre-halfvec index DO-blocks, `find_similar` full-precision dedup, and bi-temporal columns are all owned by the **active memory-overhaul track**. Flagged, not touched.
- **CitationEngine dimension conflict** (flag to CE side, not SRW): `source_embeddings` is `vector(4096)` in SRW but `vector(1536)` in the CE package's own DDL — only `CREATE TABLE IF NOT EXISTS` + bootstrap order avoids a collision; CE's only ANN index also never builds (4096 > pgvector's 2000-dim plain-`vector` HNSW cap → seq scan). Low impact today (few rows/job, job-scoped).
- **Builder drop (0032)** — verified fully clean (child-first DROP, no orphaned columns/methods/FKs).

## Mapping to `db_schema_hygiene.md`

This plan absorbs that doc's open items: item 1 → **HF-1**; item 2 → **QW-5**
(extended with `upsert_default_user`); item 3 → **D-4**; item 4 → **D-7**. The
hygiene doc remains the origin record for items 1–4; new findings (QW-1/3/4/6,
HF-2…7, D-1/2/3/5/8) come from the 2026-06-22 sweep.
