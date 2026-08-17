---
tags:
  - issue
  - officers
  - backlog
  - knowledge
  - data-integrity
  - postgresql
status: resolved-deployed-live-verified
priority: P0
created: 2026-08-17
aliases:
  - BP-13
  - BP-08 live retry residue
  - canonical ready projection type error
related:
  - "[[kb_materialization_failure_reports_ready_or_closed]]"
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
  - "[[officer_correctness_live_gate_2026-08-17]]"
---

# Knowledge metadata retry commits, then projection fails

**Status:** RESOLVED, DEPLOYED, AND MAIN-DEV VERIFIED 2026-08-17. Audit finding
**BP-13 / BP-08 live residue** passed its bounded direct and retry gate on the repaired
image with complete fixture cleanup.

## Problem

The BP-08 retry ledger correctly keeps a failed READY mutation ineligible and later
commits its canonical Git update. The public project-knowledge metadata endpoint cannot
finish that successful retry. The materializer returns the canonical `ready_at` parsed
from YAML as an ISO string; `update_knowledge_note()` passes it directly to asyncpg for a
`$n::timestamptz` parameter. Asyncpg requires a Python `datetime`, so the endpoint returns
500 after Git has already committed.

Main-dev evidence from the disposable live gate:

```text
invalid input for query argument $4: '2026-08-17T15:17:17.950894+00:00'
(expected a datetime.date or datetime.datetime instance, got 'str')
```

The exception path truthfully records projection failure, and a later reindex can rebuild
the vector row with the same `ready_at`. It is nevertheless not an acceptable success
path: the user receives a 500 after a durable commit, the intent remains unresolved until
the scheduled sweep settles it, and a retry cannot safely infer whether it should repeat
the operation.

## Second retry arm

There is a related destructive-default risk when the scheduled sweep wins before the
client retry. `materialize_knowledge_metadata_update()` returns `already-canonical` from
the durable intent without `canonical_tags`, `canonical_status`, or
`canonical_ready_at`. The endpoint then uses:

```text
canonical_tags or []
canonical_ready_at or NULL
```

for its projection update. A client retry can therefore clear the projected tags and
readiness generation while the canonical file still says READY, then report the
projection synced. A later reindex repairs the bytes, but the interval is exactly the
canonical/projection disagreement BP-08 was meant to close.

## Scope

The observed 500 is in the authenticated REST/Cockpit metadata route. The agent-side
`kb_update` path already converts canonical `ready_at` through `_canonical_ready_at()`
before calling `KnowledgeStore.upsert_note`, so this finding does not prove every Officer
knowledge write is broken. Backlog close currently writes status without a readiness
timestamp and is not the reproduced codec path. The public route and retry contract are
still part of the supported backlog-management surface and must be repaired before the
auto-pull release gate passes.

## Required direction

- Define one typed canonical metadata result. Convert `ready_at` at the materializer/API
  boundary once; do not rely on driver casts accepting ISO strings.
- An `already-canonical` result must carry the exact canonical status, complete tag set,
  and `ready_at`, or the projection layer must reread canonical truth. Never substitute an
  empty tag list or null generation for missing result fields.
- Make the public reindex endpoint settle the newest canonical materialization intent when
  it successfully rebuilds that project's projection, matching the scheduled sweep, or
  explicitly document and expose why it cannot.
- Preserve the BP-08 fail-closed behavior: no projection write before canonical success,
  and no Updated/READY response before both required legs and their ledger state agree.

## Local repair checkpoint — 2026-08-17

The local implementation now uses one exact canonical metadata snapshot for every
metadata projection:

- canonical YAML `ready_at` is parsed once into an aware Python `datetime` inside the
  materializer; the REST/vector boundary rejects any untyped result instead of relying on
  asyncpg casts;
- a caller that observes an intent another retry already canonicalized rereads the current
  note from the resolved project vault and returns its exact status, complete tags, and
  readiness generation;
- absent/malformed canonical metadata returns 409 and performs no vector mutation; request
  values, empty tags, and null timestamps are no longer substitutes for missing truth;
- a canonical null `ready_at` is still projected as null when READY has genuinely been
  removed, so old authorization cannot survive in pgvector;
- executor disposition uses the same complete snapshot and projects its canonical status,
  rather than accepting a ledger state without readable metadata;
- successful direct/post-write reindex now calls the same scoped latest-intent settlement
  helper as the scheduled sweep; partial reindex never claims convergence; and
- the rare conflict fallback in `begin_knowledge_materialization()` selects the newest
  matching intent deterministically.

Automated evidence includes the actual REST handler after its authorization seam against
the fully migrated pgvector schema. One case performs a first-attempt READY commit;
another injects a forge
failure, verifies 409 and an unchanged row, lets the durable retry win, and then performs
the idempotent client retry. Both prove an asyncpg-accepted `datetime`, full tags, one
canonical generation, and a synced intent. Unit coverage also pins remove/re-add,
status-only, combined status/tag, missing snapshots, malformed timestamps, executor close,
and manual-reindex settlement.

Local verification:

- broad knowledge/materialization/reindex/backlog suite: **1,165 passed**;
- adjacent campaign/loop/Officer-backlog suite: **223 passed**;
- real migrated pgvector direct + retry endpoint suite: **2 passed**;
- existing real app-PostgreSQL BP-08 lease/cyclic-payload suite: **2 passed**; and
- changed-file Ruff, format, and `git diff --check`: passed.

### Disposable k3d deployment gate — 2026-08-17

The repaired working tree also passed a bounded end-to-end run on Kubernetes context
`k3d-srw`, namespace `srw`, with `auto_pull=false` throughout. The ready
`srw-orchestrator` pod ran Tilt image `tilt-52defd435d572c3f` with zero restarts. This is
local deployment evidence only: repository `HEAD` and `origin/develop` were still
`7b638b09`, while the BP-13 implementation remained in the local working tree used by
Tilt. It is not evidence that the repair reached main dev.

The run used one disposable project and two notes against the real HTTP route, managed
Gitea vault, app PostgreSQL intent ledger, pgvector projection, and asyncpg timestamp
codec:

- a first-attempt READY request returned 200, made exactly one metadata commit, and left
  canonical tags, the aware `ready_at`, vector tags/timestamp, and the newest intent in
  exact agreement;
- an uncredentialed GitHub binding installed only on the disposable project returned
  409, changed neither canonical Git nor pgvector, and retained a retryable durable
  intent;
- after restoring the exact Gitea binding, the production retry service committed once;
  a following client retry returned 200 without another commit or `ready_at`, preserved
  the complete tag set and exact generation, and marked its projection synced; and
- supported project deletion removed the managed repository, project, vector rows, and
  intent rows. The pre-run Officer-post count and `auto_pull=true` count were unchanged,
  no migration was dirty, and the orchestrator remained ready with zero restarts.

Before the gate, local k3d contained an earlier checksum of migration 0165 from a
pre-final Tilt build. Both 0165-owned tables were verified empty, then only those two
empty local tables and that exact local migration-ledger row were reset so the current
migration could replay. This was disposable-cluster repair, not a production rollout
procedure. The current 0165 migration then applied successfully.

At the end of the k3d follow-up no shared cluster had been mutated. The historical
main-dev failure remains truthful; the successful deployed rerun is recorded below.

### Main-dev deployed rerun — PASS 2026-08-17

The bounded BP-08/BP-13 slice passed on Kubernetes context `main`, namespace
`superhuman-remote-worker`, from 17:02:17Z through 17:02:34Z. Both orchestrator replicas
were ready with zero restarts on repair image `sha-51d822b`. Migration 0165 was successful,
its required retry/lease columns were present, and the dirty-migration count was zero.

One disposable project (`69813842-304a-4ea7-82f6-08ce11137ba9`) and two notes exercised
the real authenticated HTTP route, managed Gitea vault, app intent ledger, pgvector, and
asyncpg codec:

- first-attempt READY returned 200, made one metadata commit, and left the canonical tags
  and aware `ready_at`, pgvector tags/timestamp, and newest intent exactly synchronized;
- a repository fault scoped to only the disposable project returned 409, changed neither
  Git nor pgvector, and retained a durable retryable intent;
- after restoring the exact binding, that one intent was leased explicitly and passed to
  the same production retry handler used by the periodic sweeper. It committed once; the
  following client retry returned 200, made no extra commit or readiness generation, and
  projected the complete canonical snapshot; and
- supported project deletion removed the Gitea repository, Keycloak group, Nextcloud
  folder, project row, intent rows, and vector rows. The Officer baseline returned to 56
  durable posts, one commissioned post, and zero `auto_pull=true`; both replicas remained
  ready with zero restarts, and no migration became dirty.

The periodic 900-second fleet-wide sweep itself was not accelerated on the shared cluster;
the run used its exact retry handler with a lease scoped to the disposable intent. Lease
ownership, scheduled claiming, and sweeper/client races remain covered by the real
PostgreSQL tests without making unrelated due intents part of this live gate.

## Acceptance

- Real PostgreSQL covers a first-attempt READY update and proves HTTP 200, canonical file,
  vector tags, typed `ready_at`, and synced intent all agree.
- A retryable forge failure returns 409 without projection change; the due retry commits
  once and returns 200 without manufacturing a second `ready_at`.
- A scheduled sweep winning before the client retry is followed by an idempotent client
  retry that preserves the complete tag set and exact generation.
- Removing and re-adding READY, status-only updates, and combined status/tag updates use
  the same typed contract.
- Projection failure after canonical commit remains explicit, and successful manual
  reindex settles the newest canonical intent rather than leaving eligibility blocked.
- Mocked endpoint tests are supplemented by asyncpg codec coverage; a mock accepting a
  string for `timestamptz` is not sufficient.
- **Passed:** the BP-08 slice of [[officer_correctness_live_gate_2026-08-17]] returned the
  expected 200/409/200 sequence on main dev, with `auto_pull=false` throughout and
  complete fixture cleanup.
