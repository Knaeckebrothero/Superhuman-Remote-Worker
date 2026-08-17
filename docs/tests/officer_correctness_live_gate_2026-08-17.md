---
tags:
  - test
  - verification
  - officers
  - backlog
  - knowledge
  - live-gate
status: failed-release-blocker
created: 2026-08-17
related:
  - "[[auto_pull_jobs_are_dispatchable_before_provisioning]]"
  - "[[kb_materialization_failure_reports_ready_or_closed]]"
  - "[[backlog_floor_wake_failure_consumes_debounce]]"
  - "[[knowledge_metadata_retry_commits_then_projection_fails]]"
  - "[[officer_control_plane_post_implementation_audit]]"
---

# Officer correctness tranche — main-dev live gate

**Status: FAIL 2026-08-17. Keep `auto_pull=false`.** BP-07 and BP-10 passed their
deployed live contracts. BP-08's failure boundary passed, but its recovery path exposed a
real type error after the canonical Git commit: the public metadata endpoint passes the
canonical ISO `ready_at` string directly to an asyncpg `timestamptz` parameter, returns
HTTP 500, and leaves projection state failed. The narrow repair is tracked in
[[knowledge_metadata_retry_commits_then_projection_fails]].

**Local follow-up:** BP-13 was implemented and passed real-pgvector direct and
sweeper-won retry tests later on 2026-08-17. It also passed a disposable end-to-end k3d
run through the real HTTP/Gitea/app-ledger/pgvector stack: direct READY returned 200, an
isolated forge fault returned 409 without projection drift, the production retry service
committed once, and the following client retry preserved the exact canonical generation.
Cleanup removed every fixture and left the local Officer/auto-pull baseline unchanged.
That Tilt image was built from the repaired working tree while `HEAD` and
`origin/develop` still pointed to `7b638b09`; it is not main-dev evidence. This document
therefore remains the truthful record of the deployed failed run until its BP-08 slice is
rerun against the replacement main-dev image.

## Scope and safety

The run targeted Kubernetes context `main`, namespace `superhuman-remote-worker`. The
global local context was not changed. It used one uniquely named disposable project,
one database-backed synthetic Officer incarnation, one ticket, and one strict job. The
existing commissioned Officer was observed only as a baseline identity: it was not
messaged, held, edited, restarted, or used for the probes.

`auto_pull` remained false on every post. The test created no worker or generative-LLM
turn; KB reindex used the configured embedding service. A current `last_respawn_at` guard
prevented the synthetic Officer from being respawned by the watchdog. Fault injection
changed only the disposable project's KB
repository binding and restored it before recovery.

## Deployment baseline

| Field | Evidence |
|---|---|
| UTC run | 2026-08-17 15:15:58–15:18:02 |
| Git/deploy revision | repository HEAD `7b638b09`; implementation `4d703be6` plus test repair `61d51e1e` |
| Serving image | two ready `srw-orchestrator-cdd8fd5f7-*` replicas on `sha-4d703be`, zero restarts |
| Rollout | 2 updated / 2 ready / 2 available, `NewReplicaSetAvailable` |
| Migration 0165 | success at `2026-08-17 14:33:11.371948+00`, 27 ms |
| Dirty migrations | zero |
| Initial commissioned posts | one existing post |
| Initial `auto_pull=true` posts | zero |
| Disposable IDs | project `e9fc383c-e465-4ac9-b0ad-e7035ea16e05`; thread `9b2366ae-d853-4ee0-96ba-efae85a20c67`; job `ada9b347-0512-441e-8d0f-122e8fb92f72` |

The serving pods also log an unrelated pre-existing infrastructure-metering configuration
error for the `workspace_vm` exact scope. This run used only the sandbox/researcher path
and did not exercise or alter that configuration.

## Scorecard

| Contract | Result | Live evidence |
|---|---|---|
| BP-07 born non-dispatchable | **PASS** | Claim and job committed together as `paused` with `officer_preflight` freeze; direct agent claim refused. |
| BP-07 crosses dispatcher poll | **PASS** | After 35 seconds, including a catch-all dispatcher interval and heartbeat nudges, job remained paused and unassigned. |
| BP-07 infrastructure truth | **PASS** | Injected repository failure became `retryable-failed`, retained claim/capacity, and produced zero breaker outcomes. |
| BP-07 real recovery | **PASS** | Real Gitea isolated repo provisioned; one activation cleared the freeze. The hook immediately cancelled the job before dispatch; no agent was assigned. |
| BP-08 failed write truth | **PASS** | Retryable forge/config fault returned 409 `pending_sync`; tags and `ready_at` remained absent from pgvector. Broken reindex returned 500 rather than inventing READY. |
| BP-08 retry endpoint | **FAIL** | Attempt 2 committed the canonical update, then returned 500 because asyncpg received an ISO string for `timestamptz`. |
| BP-08 generation stability | **PASS after repair path** | Canonical file had exactly one `ready_at`; two full reindexes preserved the same timestamp. Scoped production settlement helper converged the disposable ledger. |
| BP-10 failed outbox | **PASS** | Fault after outbox insert rolled the event back, retained `last_attempted_at`, left `last_queued_at` null, and consumed no six-hour policy debounce. |
| BP-10 retry/concurrency | **PASS** | Retry queued; two concurrent ticks for one pool produced exactly one queued event and one `policy_debounce`. |
| BP-10 delivery/decommission | **PASS** | Exact event settlement updated the episode to delivered; a queue/decommission race left no pending/sending event, active episode, or linked post. |

## BP-08 failure

The disposable binding was temporarily changed to an uncredentialed GitHub target. This
fails before canonical-file lookup and therefore exercises the retryable client/config
path rather than the correctly permanent “canonical file missing” path.

The first READY request behaved correctly:

- HTTP 409 with `canonical_state=pending_sync` and `retry_state=retryable`;
- durable intent present;
- pgvector tags and `ready_at` unchanged; and
- full reindex against the broken binding returned HTTP 500 without changing projection.

After restoring the exact Gitea binding and waiting for the durable retry deadline, the
second attempt updated the canonical file in Gitea. It then failed in
`orchestrator/main.py` while binding `materialization["canonical_ready_at"]` to the vector
UPDATE:

```text
invalid input for query argument $4: '2026-08-17T15:17:17.950894+00:00'
(expected a datetime.date or datetime.datetime instance, got 'str')
```

The endpoint returned 500 and `finish_knowledge_projection(..., synced=False)` left the
intent's projection state failed. A subsequent full reindex rebuilt the correct pgvector
row, and the same production settlement helper used by the scheduled sweep marked the
newest canonical intent synced. This bounded continuation existed only so BP-07 and BP-10
could be tested independently; it does not convert BP-08 into a pass.

A code follow-up found a second arm of the same retry contract: when the scheduled sweep
wins before a client retry, `materialize_knowledge_metadata_update()` returns
`already-canonical` without canonical tags or `ready_at`, while the endpoint currently
defaults those missing values to `[]` and `NULL`. That path needs a real-PostgreSQL
regression before release; mocked `fetchval` tests did not exercise either asyncpg's
timestamp codec or the destructive default.

## Cleanup and final state

Cleanup used the supported job and project deletion endpoints, decommissioned only the
synthetic Officer, and then removed its ended, detached thread. The job endpoint deleted
its isolated Gitea repository; project deletion removed the managed knowledge repository,
Keycloak group, cloud folder, vector rows, and project-scoped database state.

Final checks found:

- zero disposable projects and threads;
- zero claims, knowledge intents, floor-wake episodes, and vector notes for the project;
- no disposable pod;
- the original commissioned-post set unchanged;
- zero `auto_pull=true` posts;
- zero dirty migrations; and
- both orchestrator replicas ready with zero restarts.

Three harness-calibration fixtures before the authoritative run were also deleted. The
first established that a nonexistent Gitea repository is correctly classified as a
permanent missing canonical file, not a retryable outage. The second exposed that a fresh
in-process Gitea client must run its normal initialization before an independent canonical
read. Neither calibration changed the product verdict.

## Release decision

Do not enable auto-pull yet. BP-07 and BP-10 need no repair from this run. Repair the
public metadata retry/projection boundary, add real-PostgreSQL coverage for the direct and
sweeper-won retry paths, redeploy, and rerun only the BP-08 portion. A pass there returns
the release decision to the remaining umbrella blockers; it does not by itself close
BP-01, BP-11, ES-01, OC-07/OC-08/OC-10, or the remaining OC-05/OC-06 residues.

The requested repair and local real-pgvector coverage are now complete. The remaining
action from this failed run is deployment plus that bounded BP-08 rerun; `auto_pull` stays
false until it passes.
