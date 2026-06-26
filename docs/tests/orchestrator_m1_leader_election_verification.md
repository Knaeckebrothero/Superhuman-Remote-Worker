# Orchestrator M1 (Leader Election) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M1 / Phase 1 (Track 2 Layer 1).
**Spec / plan:** `docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md` (research base: `docs/researches/orchestrator_leader_election.md`).

**Status (2026-06-26): Code complete; unit/integration-verified AND two-replica-verified on local k3d. `replicas: 2` is correctness-safe. The k3d run caught and fixed a deploy-blocking bug (see below). The live (dev) cluster run is still PENDING — it wants a quiet window (overnight, no one else mid-test on the cluster).**

> **The k3d run earned its keep:** it caught a real, deploy-blocking bug the unit tests structurally could not — the lifespan leader-election imports used the package-prefixed form (`from orchestrator.services.leader_election …`), which resolves at the repo root (so all 16 unit tests passed) but **not** in the deployed flattened `/app` image, crashing the orchestrator at startup with `ModuleNotFoundError: No module named 'orchestrator'`. Fixed by the `fix(orchestrator): use flattened-image import paths for leader-election wiring (M1)` commit — plain top-level imports matching house convention. This is exactly the runtime-wiring class of bug a two-replica deploy exists to find.

## What M1 guarantees

Run `replicas: 2+` of the orchestrator safely: the ~9 singleton background loops run on exactly one **elected** replica (session-scoped Postgres advisory lock), and every correctness-critical path is additionally guarded **at the database** — because leader election has no fencing, two leaders can briefly coexist during a partition / Postgres failover, so the lock is treated as efficiency, not correctness.

## Verified via automated tests (2026-06-25)

pytest + `testcontainers.PostgresContainer("postgres:16")` over the podman socket — Python 3.12.10 (matches CI). **16 tests, all green.**

| Area | Test file | Tests | What it proves |
|---|---|---|---|
| Leader election + failover | `tests/test_leader_election.py` | 3 | Two asyncpg sessions on one DB → exactly one holds `LEADER_ID`; on the leader's session close the lock auto-releases and a follower acquires within the poll interval. `run_as_leader` sets/clears `is_leader`; `run_when_leader` runs a loop only while leadership is held and cancels it on loss. |
| Dispatch CAS claim | `tests/test_job_claim.py` | 2 | Two concurrent `claim_job_for_agent` for one job → exactly one wins (no double-assign); a non-dispatchable status is never claimed. |
| IMAP dedup (Task 5) | `tests/test_imap_dedup.py` | 3 | Two concurrent `claim_inbound_email` for the same Message-ID → exactly one wins (no double reply-inject); already-claimed and distinct IDs behave. |
| Delegation resume CAS (Task 5) | `tests/test_delegation_resume_claim.py` | 2 | Two concurrent `claim_delegation_resume` for one parent → exactly one re-queues it waiting→paused (agent cleared); a non-`waiting` parent is never re-queued. |
| Digest claim (Task 5) | `tests/test_digest_claim.py` | 2 | Two concurrent `claim_pending_notifications` split the pending set disjointly (no double-send); `unmark_notifications_delivered` releases a claim for retry. |
| Notify claim-before-send (Task 5) | `tests/test_notify_dedup.py` | 4 | Two concurrent `claim_sent_notification` for one (request_id, kind) → exactly one sends (partial unique index + `ON CONFLICT`); already-claimed / distinct requests / downgrade-frees-the-slot behave. |

**Run recipe** (the `.venv` `pip` wrapper shebang is stale; testcontainers needs the podman socket + ryuk disabled):

```bash
DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock" TESTCONTAINERS_RYUK_DISABLED=true \
  .venv/bin/python -m pytest \
    tests/test_leader_election.py tests/test_job_claim.py tests/test_imap_dedup.py \
    tests/test_delegation_resume_claim.py tests/test_digest_claim.py tests/test_notify_dedup.py -v
```

`ruff check` clean across all touched files. `python -c "import ast; ast.parse(...)"` confirms `orchestrator/main.py` parses after the surgical edits (the 23k-line monolith can't be meaningfully unit-tested for lifespan wiring — that's the two-replica test's job, below).

## Verified on local k3d — two replicas (2026-06-26)

Cluster `k3d-srw` (single node), full SRW stack via Tilt. Set `orchestrator.replicas: 2` + `orchestrator.pdb.minAvailable: 1` in `deployment/values-tilt.yaml`; Tilt rebuilt + rolled out the orchestrator with the M1 code baked.

| Check | Result |
|---|---|
| Orchestrator **boots** with M1 code at `replicas: 2` | **PASS** (after the import fix) — both pods reach Ready with 0 restarts; before the fix both crash-looped at `from orchestrator.… import` (the bug this run caught). |
| **Exactly one leader** | **PASS** — one pod logs `leader_election: acquired leadership (lock 6003957320051409220)` (`0x5352575F4C454144` = "SRW_LEAD"); the other does not. Postgres `pg_locks` shows exactly **one** granted advisory lock (`objid 0x4C454144`), held by the leader pod's backend (confirmed via `client_addr`). |
| **`run_when_leader` gates the singletons** | **PASS** — the leader logs all 7 leader-gated loop starts (auto-assign dispatcher, stale-agent detector, agent-pool reconciler, lifecycle reconciler, delegation-timeout sweeper, headless permission-notify sweeper, quota poll); the follower logs **none** of them, while both run the non-gated loops (cron dispatcher, project-loop sweeper, prune sweepers, …). Exactly the intended split. |
| **Failover on leader death** | **PASS** (×2) — `kubectl delete pod <leader>`: the advisory lock auto-releases and a surviving replica re-acquires it; never two holders. Wall-clock ~20 s, dominated by the M0 `preStopDrainSeconds: 15` drain (lock held until the leader's `finally` unlocks) + the ~10 s follower poll interval. |
| **Graceful step-down** | **PASS** — the dying leader logs `leader_election: released leadership` (the `finally` `pg_advisory_unlock` path), so failover is a clean handoff, not a timeout. |
| **Warm-follower loop is healthy** | **PASS** — isolated via `kubectl scale --replicas=1` (removing the competing replacement pod), the long-running follower acquired leadership ~26 s after the leader left. |
| **Exactly-once dispatch** | **PASS (mechanism level)** — the auto-assign dispatcher runs only on the leader (above) and the per-job CAS `claim_job_for_agent` is unit-tested for exactly-one-wins. A full end-to-end job-submission demo was **not** run (needs the internal-API job path + a healthy agent pipeline; it would mostly re-demonstrate the unit-tested CAS). |

**Observation (not a defect):** the freshly-booted *replacement* pod tends to win the lock over the warm follower, because its boot time (~18 s, init-containers) lands its first lock attempt right when the old leader's 15 s drain ends — a structural timing alignment, not a bug. Exactly-one-leader holds regardless. Failover speed (~20 s here) is tunable via `preStopDrainSeconds` + the leader poll interval if faster handoff is wanted.

## Still owed — the live (dev) cluster run

The k3d run proves the wiring on a single node. The live multi-node cluster under real traffic is still owed:

- **Live (dev) two-replica failover** — a mid-dispatch job ends `paused` then re-dispatched exactly once; the survivor's loops take over; no duplicate emails/replies during the dual-leader window; realistic failover numbers.
- **Hard-kill path** — k3d here exercised graceful delete (clean unlock). A `--grace-period=0` kill leaves the lock held by the dead Postgres session until TCP-keepalive detection (~40 s with the `10/10/3` tuning) — worth confirming on the live cluster.

### When & prerequisites

- **When:** overnight / a low-usage window, coordinated so no one is mid-test on the cluster. The test deletes the leader pod.
- **Prerequisite:** M1 deployed to the target. **M1 is pushed to `origin/develop` (2026-06-26)** — dev picks it up via Fleet GitOps sync; the live run can proceed once dev has rolled the new orchestrator image (confirm the running image carries the leader-election code, e.g. `acquired leadership` appears in a pod log).

## Scope guard

M1 makes `replicas: 2` **correctness-safe**; it does **not** flip the default. The chart stays `replicas: 1` until the two-replica runs above pass and the count is bumped deliberately (Phase 5 / M4). `FOR UPDATE SKIP LOCKED` on the dispatch candidate scan is a throughput optimization deferred to M2 — the per-job CAS is the safety guard and it landed in M1.
