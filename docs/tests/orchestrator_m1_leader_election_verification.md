# Orchestrator M1 (Leader Election) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M1 / Phase 1 (Track 2 Layer 1).
**Spec / plan:** `docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md` (research base: `docs/researches/orchestrator_leader_election.md`).

**Status (2026-06-26): Code complete; unit-verified, two-replica-verified on local k3d, AND live-verified on the dev cluster (`main`) under real traffic. `replicas: 2` is correctness-safe and now fails over gracefully. The live run caught + fixed two bugs k3d structurally couldn't (uvicorn graceful-shutdown hang; IMAP log-spam — see "Verified on the live dev cluster" below). Dev is left at `replicas: 2` to soak; the chart default stays `replicas: 1` until the soak completes (Phase 5 / M4).**

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

## Verified on the live dev cluster — two replicas (2026-06-26)

Cluster `main` (Rancher, 4 nodes), namespace `superhuman-remote-worker`, under **real traffic** (an active multi-hour job + a working agent). Flipped via Fleet — a top-level `orchestrator: {replicas: 2, pdb: {minAvailable: 1}}` block added to `deployment/values-experimental.yaml` (commit `14e504f6`); Fleet applied it on the existing image within ~1 min (a values-only change triggers no rebuild). The two replicas land on different nodes (pod anti-affinity ✓). Procedure: `docs/operations/orchestrator_m1_go_live.md`.

| Check | Result |
|---|---|
| **Exactly one leader (live)** | **PASS** — one pod logs `acquired leadership`; `pg_locks` shows exactly one granted advisory lock, key `classid 0x5352575F` ("SRW_") + `objid 0x4C454144` ("LEAD") = `SRW_LEAD`, held by the leader's `client_addr`. |
| **`run_when_leader` gating (live)** | **PASS** — 7 gated-loop starts on the leader, **0** on the follower. |
| **Graceful leader kill — correctness** | **PASS** — `locks` stays ≤1 throughout (never two holders); **zero serving blackout** (95/95 health probes `200` on a 1 s poll through the failover); the in-flight job kept processing. |
| **Hard kill (`--grace-period=0`)** | **PASS** — survivor re-acquired in **~4 s** (a clean RST on a healthy node frees the session lock immediately — *not* the ~40 s keepalive path, which only applies to node/network loss); zero blackout; job survived. |
| **In-flight job survives failover (§4c)** | **PASS** — job `d82ede69` rode through 3 leader kills + a full image rollout, still `processing` (agent pods are separate; the orchestrator gap is well under the 3-min agent heartbeat). |
| **PDB protects the last replica (§5)** | **PASS** — evicting the follower (eviction API via `kubectl drain --pod-selector`, **not** a node drain) is allowed, then `disruptionsAllowed` drops 1→0 so the leader is protected; the evicted pod reschedules onto another node; the leader (lock holder) is unaffected. |

### Two bugs the live run caught (k3d couldn't — it had no long-lived connections at shutdown)

The graceful kill first measured **~66 s** (not k3d's ~20 s) and the dying leader never logged `released leadership`:

- **Bug B — uvicorn graceful shutdown blocked indefinitely.** Its log showed `Shutting down` → `Waiting for connections to close.` and then hung: with no `--timeout-graceful-shutdown`, uvicorn waits forever for open connections (the active agent/SSE stream) to close, so lifespan teardown — and the leader-election `pg_advisory_unlock` in `run_as_leader`'s `finally` — never ran before the 60 s `terminationGracePeriodSeconds` SIGKILL. The lock then freed only via connection-drop. Correctness held throughout (one lock holder, zero blackout), but graceful failover was broken. *Ironically a hard kill (~4 s) was ~16× faster than the broken "graceful" path.*
- **Bug A — IMAP poller log-spam.** `imap_poll_loop` returns immediately when IMAP is unconfigured, and `run_when_leader` re-invokes a returning loop every `poll_seconds` (1 s), so the leader logged `IMAP poller not started (not configured)` ~once per second.

**Fix** (commit `a60c2efe`, shipped in image `sha-cb1f632`): add `--timeout-graceful-shutdown 10` to the uvicorn CMD in both orchestrator Dockerfiles; park `imap_poll_loop` on `shutdown_event` instead of returning. (CI for `a60c2efe` first failed on a pre-existing stale test, `test_headless_notifications_phase4.py::test_full_send_path`, left stale by the Task-5d claim-before-send reorder and surfaced because this was the first code change to run `test-python` since; fixed in `cb1f632b`.)

### Re-verified on the fixed image (graceful kill)

| Check | Result |
|---|---|
| **Graceful step-down restored** | **PASS** — dying leader now logs `Shutting down` → `Waiting for connections to close.` → `Waiting for application shutdown.` → **`leader_election: released leadership`** (~28 s after delete, after the 10 s connection-close cap), then terminates cleanly (no SIGKILL hang). |
| **Clean handoff** | **PASS** — `locks` 1→0→1; survivor acquired ~33 s after the delete (15 s preStop + 10 s graceful cap + teardown + poll); never two holders; **0/90 health probes non-200**. |
| **IMAP spam gone** | **PASS** — leader logs the "not configured" line exactly **once** (at acquisition), not 1/sec; `--timeout-graceful-shutdown 10` confirmed in the running pod's uvicorn cmdline. |

**Net:** `replicas: 2` is correctness-safe **and** now fails over gracefully — **~33 s** planned (clean step-down) / **~4 s** unplanned (hard kill), **zero serving blackout** either way. Dev is left at `replicas: 2` to soak.

> **Failover-speed note:** the predicted "~40 s keepalive" path is for **node/network loss** (no clean socket close), not a `kubectl delete --grace-period=0` on a healthy node (clean RST → ~4 s). True keepalive-bounded recovery would need a node kill, out of scope here. A real `kubectl drain` of an orchestrator node is **unsafe on this cluster** (the nodes also host the prod-private orchestrator + the data tier) — see the go-live runbook §5 for the surgical `--pod-selector` alternative used instead.

## Scope guard

M1 makes `replicas: 2` **correctness-safe**; it does **not** flip the default. The chart stays `replicas: 1` until the two-replica runs above pass and the count is bumped deliberately (Phase 5 / M4). `FOR UPDATE SKIP LOCKED` on the dispatch candidate scan is a throughput optimization deferred to M2 — the per-job CAS is the safety guard and it landed in M1.
