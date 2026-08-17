---
tags:
  - issue
  - officers
  - backlog
  - knowledge
  - data-integrity
  - git
status: done-deployed-live-verified
priority: P1
created: 2026-08-15
aliases:
  - BP-08
  - false successful KB disposition
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_backlog_pools]]"
---

# KB materialization failure can report a ticket as ready or closed

**Status:** DEPLOYED AND LIVE-VERIFIED 2026-08-17. The original failure-truth boundary
passed, and BP-13's direct/retry recovery residue subsequently passed its bounded
main-dev rerun. Truthful-write blocker/K4 remains closed; `auto_pull` stays off for the
separate umbrella blockers.

## Problem

`knowledge_tools._materialize_note()` deliberately returns a failed result rather than
raising. `kb_write`/`kb_update` callers ignore it, continue updating projections, and
return success text. The officer can be told that a ticket is READY or closed while its
canonical OKF file was not changed.

The file and index then disagree. A later git reindex can restore stale tags/status and
resurrect work the officer believed dispositioned.

## Required direction

- Make materialization outcome part of every knowledge mutation result.
- Define which store is authoritative during degradation. The current feature contract says
  canonical project knowledge must not silently diverge from projections.
- For dispatch authorization and closure, fail closed or enter an explicit pending-sync
  state that the tick treats as ineligible/unresolved.
- Persist retry intent and expose it in the officer’s knowledge/SITREP availability section.
- Do not return “Updated,” “READY,” or “closed” unless the required durable boundary
  succeeded.

## Acceptance

- Inject missing writable binding, git unavailable, push conflict, malformed frontmatter,
  and materializer exception. Every tool result names the failure/degraded state.
- A failed ready write does not dispatch; a failed close does not release executor
  disposition or disappear from the backlog.
- Retry convergence updates file and projection once without bumping `ready_at` twice.
- Reindex during/after failure cannot resurrect or invent state silently.
- Cockpit/officer tools distinguish canonical, pending-sync, failed, and projection-only
  observations.
- Existing add-only/tag-removal semantics remain intact.

## Dependencies

Authorization from [[backlog_machine_tags_trust_any_persistent_session]] must be enforced
before the mutation begins; this issue governs truth after an authorized mutation.

## Implemented authority and convergence boundary

The project knowledge repository is the canonical durable store. The pgvector knowledge
index is its required searchable/eligibility projection. Neo4j remains an optional derived
graph: graph failure is reported to the tool caller but does not create an unrepairable
claim that Git and the reindexer disagree.

Migration `0165_officer_correctness_state.sql` adds
`knowledge_materialization_intents`. Every `kb_write`, `kb_update`, public metadata update,
and backlog close persists an intent before repository mutation and reports these distinct
dimensions:

- canonical: `pending_sync`, `canonical`, `failed`, or historical `superseded`;
- projection: `pending`, `synced`, `failed`, or `projection_only`;
- retry: `none`, `retryable`, or `permanent`.

Only a committed or byte-identical canonical file permits the pgvector mutation. Missing
writable binding/repository, unavailable Git, refused push/conflict, malformed
frontmatter, and unexpected materializer errors therefore cannot return Created, Updated,
READY, or closed. A failed READY intent is filtered by the Officer eligibility scan; a
failed close leaves the index row active and causes executor disposition to remain
unresolved. Authorization is checked before the materializer starts.

The sweep leases due canonical retries before reindex. A successful reindex then marks
only the newest canonical intent per note as projected, so stale retries cannot overwrite
newer truth. READY entry is timestamped in the canonical frontmatter; projection consumes
that exact timestamp. Retries and reindex do not manufacture a second readiness
generation. The partial unique index coalesces only unresolved equivalent attempts, so a
later legitimate `resolved -> active -> resolved` mutation is not suppressed by history.
Tool/API output names canonical and projection state, while SITREP and Cockpit expose the
latest per-note state including pending-sync, failed, and projection-only observations.

## Acceptance evidence

- Materializer tests cover absent repository/binding, unavailable/raising forge, both
  create/update conflict refusals, malformed frontmatter, unexpected exceptions,
  canonical metadata rendering, and exact READY timestamp reuse.
- Tool and endpoint tests prove failed canonical writes do not touch pgvector, do not
  report Created/Updated, and return an explicit pending-sync/error state. Closure tests
  prove repository failure or a missing note leaves the backlog projection unchanged.
- Retry/reindex tests prove canonical retry precedes reindex and latest-intent projection
  settlement. Real PostgreSQL tests prove one leased equivalent attempt, one projection
  settlement, and the valid reuse of an older payload after an intervening mutation.
- Focused materialization/project/tool suite: **360 passed** in **2.90 s**.
- Expanded knowledge/materialization/reindex/authorization suite: **1,081 passed** in
  **27.63 s**.
- Real PostgreSQL Officer/routing/pagination suite: **97 passed** in **232.40 s**.
- Cockpit Officer component: **64 passed** in **730 ms**.

The bounded integration above was subsequently deployed and exercised against a real
disposable main-dev Gitea vault. The failure half passed: a retryable forge/configuration
outage returned 409 `pending_sync`, wrote no READY tag or `ready_at` to pgvector, and a
broken reindex did not invent state. Retry committed the canonical file exactly once and
two reindexes preserved one stable generation.

The full gate nevertheless failed. The REST metadata route passed the canonical ISO
timestamp string to asyncpg's `timestamptz` codec, returned 500 after the Git commit, and
left projection state failed until reindex/settlement. Code audit also found that an
`already-canonical` retry omits exact canonical metadata while the route defaults missing
tags/readiness to `[]`/NULL. Those live/retry residues are tracked separately in
[[knowledge_metadata_retry_commits_then_projection_fails]]. See
[[officer_correctness_live_gate_2026-08-17]] for evidence and cleanup. `auto_pull`
remained false fleet-wide.

The BP-13 follow-up returns typed canonical metadata, rereads complete truth when another
retry won, rejects incomplete snapshots, makes executor disposition consume the same
status snapshot, and settles canonical intents after successful direct reindex. The
direct and sweeper-won paths pass against the real migrated pgvector schema. The deployed
main-dev rerun then proved HTTP 200 direct READY, a fail-closed 409, one scoped retry
commit, an idempotent 200 client retry, exact generation preservation, and complete
fixture cleanup; see [[knowledge_metadata_retry_commits_then_projection_fails]].
