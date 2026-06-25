# Orchestrator M1 (Leader Election) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M1 / Phase 1 (Track 2 Layer 1).
**Spec / plan:** `docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md` (research base: `docs/researches/orchestrator_leader_election.md`).

**Status (2026-06-25): Code complete and unit/integration-verified. `replicas: 2` is correctness-safe. The two-replica failover run (local k3d + live cluster) is PENDING — the local k3d stack is currently cold, and the live run wants a quiet window (overnight, no one else mid-test on the cluster).**

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

## Still owed — the two-replica failover run

The automated tests prove the **DB-level correctness primitives** (lock exclusivity + failover, every claim/CAS). What they cannot prove is the **runtime wiring** — that `lifespan` actually starts the leader task, that the 9 real loops are gated, and that a pod kill promotes the survivor. That needs a real two-replica deploy:

1. **Local k3d two-replica failover** — `orchestrator.replicas: 2` + `orchestrator.pdb.minAvailable: 1` in `deployment/values-tilt.yaml`; rebuild via Tilt. Confirm: exactly one pod logs `leader_election: acquired leadership`; `kubectl delete pod <leader>`; the survivor logs `acquired leadership` within ~one poll interval and the dispatcher/loops resume. Submit a job during the window → dispatched exactly once. _(As of this record the k3d cluster is cold — context `k3d-srw` is stale, no orchestrator pods. This is a full cold-start: `k3d` cluster up → `tilt up` (rebuilds all images with the M1 code) → readiness seeding. Run when the stack is next up.)_
2. **Live (dev) two-replica failover** — same, on the multi-node cluster under real traffic: a mid-dispatch job ends `paused` then re-dispatched exactly once; the survivor's loops take over; no duplicate emails/replies during the ~40s dual-leader window.

### When & prerequisites

- **When:** overnight / a low-usage window, coordinated so no one is mid-test on the cluster. The test deletes the leader pod.
- **Prerequisite:** M1 deployed to the target. As of this record the M1 commits are **local-only on `develop` (unpushed)** — the live run needs a `develop` push (Fleet sync) or a manual `helm upgrade`. The local k3d run builds from the working tree (no push needed) but needs the cold stack brought up first.
- **Keepalive note:** failover speed depends on the Postgres TCP-keepalive tuning (`tcp_keepalives_idle/interval/count = 10/10/3`, migration in `helm/templates/databases/postgres.yaml`) — a hard pod death (no clean unlock) is detected in ~40s; a clean shutdown releases the lock instantly.

## Scope guard

M1 makes `replicas: 2` **correctness-safe**; it does **not** flip the default. The chart stays `replicas: 1` until the two-replica runs above pass and the count is bumped deliberately (Phase 5 / M4). `FOR UPDATE SKIP LOCKED` on the dispatch candidate scan is a throughput optimization deferred to M2 — the per-job CAS is the safety guard and it landed in M1.
