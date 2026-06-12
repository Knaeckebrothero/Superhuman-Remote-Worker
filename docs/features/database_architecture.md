# Database Architecture — stores, boundaries, and why

**Status: Accepted direction (2026-06-11).** Outcome of the database-infrastructure
review that ran alongside the audit-store work. This is the reference for
"where does X go?" — new tables, new features, new stores all get placed by the
rules here. Companion docs: `postgres_audit_store_implementation.md` (the
observability store's build package), `observability_and_quotas.md` (usage
metering — amended 2026-06-11 to match this doc), `high_availability_setup.md`
(per-tier HA), `../strategy/2026-06-09-release-package-and-licensing.md`
(licensing constraints that shaped two decisions here).

## Terminology (pin this down once)

PostgreSQL has three nesting levels, and the word "database" gets used for two
of them; "cluster" is worse:

1. **Server / instance** — one running `postgres` process tree: one data dir,
   one port, one WAL stream, one `shared_buffers`, one `max_connections`. In
   our stack: one StatefulSet pod (`srw-postgres-0` is one server). The PG
   docs confusingly call this a "database cluster" (nothing to do with k8s).
   A CloudNativePG `Cluster` = one such server replicated across pods.
2. **Logical database** — what `CREATE DATABASE` makes. Many can live on one
   server. A connection is to exactly one; **no cross-database joins** without
   FDW. WAL — and therefore **PITR — is per-server, not per-database**: you
   cannot rewind one logical DB and leave its neighbors alone.
3. **Schema** — a namespace inside one logical database. Joins work across
   schemas; one connection sees all of them.

When this doc says "store" it means a **server with exactly one logical
database** (the CloudNativePG-recommended shape).

## The layout

| Store (server) | Image | Contents | Character |
|---|---|---|---|
| `srw-postgres` | `postgres:15` | Control plane: users, jobs, agents, threads + `thread_messages`, projects, datasources, tokens, automations, approvals, settings, `usage_rates`, `quota_limits`, `usage_daily` rollups | Load-bearing OLTP. PITR-forever class. First in line for HA (CloudNativePG track). |
| `srw-pgvector` | `pgvector/pgvector:pg15` | Semantic: sources, embeddings, memories, citations, knowledge index | Extension-coupled; latency-sensitive reads; rebuildable in principle. Memory-overhaul work (HNSW/halfvec) lands here. |
| `srw-auditdb` *(new — the Mongo replacement)* | `postgres:15` (or 16, see package) | Observability tier: `agent_audit`, `llm_requests`, `chat_history` (90/90/365d retention) **+ `usage_events` metering ledger** | Append-only, monthly-partitioned, retention-dropped, `synchronous_commit=off` class. Non-load-bearing: product flow survives its outage. |
| `srw-keycloakdb` | `postgres:15` | Keycloak's own schema | Vendor-owned. Untouched. |
| Neo4j | neo4j | Project knowledge graph | Pending the "earning its keep" verdict — metering's `category='query', resource='neo4j'` rows are the instrumentation that answers it. |
| Homelab `analytics` (TimescaleDB/Spilo) | Spilo | pdu-scraper power data, ops analytics, $/vcpu-hour rate calibration | **Not part of the product.** Inputs to `usage_rates`, never the ledger of record. |

MongoDB disappears at audit-store cutover. Gitea/OpenCloud/NATS own their own
storage and are out of scope here.

## The rules (forcing functions)

A concern gets its **own server** only when at least one of these forces it:

1. **Failure domain / workload interference** — observability writes must not
   be able to take down the control plane (the 2026-05-12 cascade is the
   canonical incident). Cache eviction is the popularized argument but the
   *real* shared chokepoints on one server are: one WAL/fsync path, one
   checkpoint cycle, one autovacuum budget, one connection pool, one I/O
   budget. (Postgres actually protects `shared_buffers` from big sequential
   scans via ~256KB ring buffers, and hot pages survive eviction via usage
   counts — cache is the *least* of the problems.)
2. **Extension / image coupling** — pgvector (and TimescaleDB, if ever
   adopted) are *extensions*: a compiled `.so` + `CREATE EXTENSION` catalog
   objects per logical database. The `.so` must be baked into the image (some
   also need `shared_preload_libraries` at server start) — that's why
   extension-specific images exist; they're packaging, not a different
   product. One image *can* bundle many extensions (the homelab Spilo does),
   but a kitchen-sink image couples every concern to one upgrade cadence and
   one supply-chain blob. One specialized image per specialized server,
   vanilla everywhere else.
3. **Backup / retention / restore profile** — PITR is per-server. The control
   plane wants point-in-time-forever; audit wants partition-drop retention and
   can tolerate ~600ms loss windows; embeddings are re-derivable. Mixing
   profiles on one server means restoring one concern rewinds the others, and
   backup/restore time scales with the firehose rather than the crown jewels.

**Anti-rule: never split by topic.** "Settings", "billing config", "user
prefs" are not workload classes — they transact with the app and stay in the
control plane. Splitting them costs joins and transactions and buys nothing
(the database-per-service-orthodoxy mistake at miniature scale).

**What deliberately stays together:** `thread_messages` (persistent-session
conversation) stays in the control plane next to `threads`. It is load-bearing
session state (resume reads it), it shares lifecycle and transactions with
threads, and the message-granular persistence work (seq column, boundary
cursors) just landed there. Revisit only if measured bloat forces partitioning
— that would be partitioning *inside* the app DB, not a move.

**The packing knob:** these are four *logical* databases with clean ownership.
Physical packing is per-deployment: our defaults run four servers (isolation
is the point), but a minimal customer install can point all four `databases.*`
external-mode values (`externalHost/Port/Db`) at one managed server — four
`CREATE DATABASE`s on their RDS box — trading shared fate for footprint. The
architecture doesn't change; the topology does.

## TimescaleDB position

Usage metering's specialization is "append-only time-series with retention and
rollups" — which **plain Postgres satisfies**: the audit store's validated
partition machinery (monthly partitions, retention drops, lookahead alarms)
plus an orchestrator-timer rollup into app-DB `usage_daily` covers every v1
requirement. TimescaleDB would add columnar compression (~10x) and continuous
aggregates, at the cost of another non-vanilla image and a licensing check —
its Community features (compression, caggs) are TSL-licensed: fine for
internal SaaS use, but redistribution inside the customer-install chart needs
the licensing-doc treatment first (we are exiting Mongo over exactly this
class of question).

**Decision: plain PG now. Named upgrade trigger:** adopt TimescaleDB for
`srw-auditdb` only when usage-dashboard rollup latency hurts or audit disk
cost materially matters — and run the TSL redistribution analysis before it
enters the chart. The swap is deliberately cheap: the store is
retention-bounded and non-load-bearing, the schema/adapter don't care about
the engine, and at 90-day retention "migrate" can mean "start fresh".

## Industry grounding (researched 2026-06-11)

- PostgreSQL docs: unrelated projects → separate databases; interrelated →
  one database, schemas. https://www.postgresql.org/docs/current/manage-ag-overview.html
- CloudNativePG FAQ: **one database per cluster** — instance-level resource
  mapping, per-instance PITR/retention, fault isolation, independent upgrades.
  https://cloudnative-pg.io/documentation/1.20/faq/
- GitLab's main/CI decomposition: ran one DB to enormous scale, then split
  **by workload** (not topic) for capacity/connections/tuning; the retrofit
  required application-level table classification + multi-release choreography
  — boundaries are cheap early, brutal late.
  https://about.gitlab.com/blog/2022/06/02/splitting-database-into-main-and-ci/
- OLTP/analytics separation: pgEdge "The Scaling Ceiling", Springtail "When
  OLAP meets OLTP" — the fix is more instances, not bigger boxes.
- Microservices database-per-service vs shared: microservices.io + AWS
  prescriptive guidance — per-service DBs solve org-scale autonomy; small
  teams keep the shared transactional DB and split only on forcing functions.

## What this implies for in-flight work

- **Audit-store migration** (`postgres_audit_store_implementation.md`): stands
  up `srw-auditdb` = the observability tier. Unchanged by this doc; it *is*
  step one of this architecture.
- **Usage metering** (`observability_and_quotas.md`, amended): `usage_events`
  lands in `srw-auditdb` as migration `0002` in the `migrations/audit/`
  family when Slice 1 starts; reuses the audit store's pool, partition
  machinery, and write path. Rollups/rates/quotas stay in the app DB as
  designed.
- **HA track** (`high_availability_setup.md`): CloudNativePG adoption maps
  one CNPG `Cluster` per store; priority order = control plane → vector →
  auditdb (auditdb may stay single-replica longest; it's non-load-bearing).
- **chat_history → thread_messages convergence** ("one message store"): a
  *product* initiative (worker jobs gaining thread-like conversation
  semantics), explicitly decoupled from the storage migration. The
  byte-parity audit migration keeps that door open.
