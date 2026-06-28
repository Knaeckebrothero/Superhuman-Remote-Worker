# Orchestrator M2-L4 — NATS Handler Replica-Safety

**Date:** 2026-06-28
**Status:** Design — pending review
**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone **M2, Layer 4** (NATS replica-safety). Builds directly on M1 (`docs/superpowers/plans/2026-06-25-orchestrator-m1-leader-election.md`) and reuses its two primitives: leader-gating (`services/leader_election.is_leader`) and claim-before-act (the migration-0038 insert-as-claim pattern).
**Verification (on implement):** to be recorded in `docs/tests/orchestrator_m2_l4_nats_replica_safety_verification.md`.

## Problem

With `replicas: 2` (M1, live on dev since 2026-06-26), the orchestrator's NATS subscriptions in `services/nats_bridge.py` use **plain `subscribe()` with no queue group**, and the handlers are **not leader-gated**. Core NATS pub/sub fans every message out to *all* subscribers — unlike HTTP, which the K8s Service load-balances to one replica — so **both** orchestrator replicas run **every** VM/sudo/session handler on **every** message.

Audit of the six subscriptions (all `{orchestratorId}`-scoped — that scoping separates *installs*, not *replicas*):

| Subject | Handler | Side effect when run twice | Verdict |
|---|---|---|---|
| `vm.lifecycle.status.{oid}` | `_on_vm_lifecycle_status` | idempotent `merge_vm_context` upsert | ✅ benign |
| `agent.vm.{oid}.*.register` | `_on_daemon_register` | context upsert (benign) + **IDE-config SSH seed** + dispatch trigger | ❌ **double SSH seed** (the trigger is already leader-gated) |
| `agent.vm.{oid}.*.heartbeat` | `_on_daemon_heartbeat` | idempotent timestamp / IDE-session upsert | ✅ benign |
| `agent.vm.{oid}.*.status` | `_on_daemon_status` | log only | ✅ benign |
| `sudo.request.{oid}.>` | `sudo_gate.on_sudo_request` | **fresh approval row + `_pending_msgs` + SSE prompt + `msg.respond`** | ❌ **duplicate approval prompts / rows / NATS replies** |
| `session.events.{oid}.>` | `_on_session_event` | in-process `notification_feed.broadcast` | ✅ benign — and *requires* fan-out |

Two handlers double-execute harmfully (`register`, `sudo`); the other four are benign or require fan-out.

**Currently latent on dev:** the harmful paths need VM-tier jobs *and* a human-approval sudo request, which the dev workload hasn't exercised — which is why the 2-day `replicas: 2` soak looked clean. This is a real correctness bug, not hypothetical.

## Goals / Non-goals

**Goal:** make the two harmful NATS handlers replica-safe under `replicas: 2`, reusing M1's shipped primitives, with **zero behavior change at `replicas: 1`**.

**Non-goals:**
- **No queue groups** — they are the wrong tool here (see Decision).
- `session.events` stays fan-out (it *needs* every replica to reach its own in-process SSE clients).
- The four benign handlers are untouched (YAGNI).
- **Cross-replica *live* SSE push for the sudo prompt is L3** (cross-replica fan-out) and explicitly out of scope; the residual is documented below and mitigated by the existing pending-list endpoint.

## Decision: why not queue groups

A NATS queue group delivers each message to exactly one group member. That is the textbook "process once" tool, and the HA design doc's draft says to queue-group all six subjects — but going subject-by-subject, that is wrong on three counts:

- **`register` needs the leader, not a random replica.** Its dispatch poke is `on_vm_ready = _trigger_dispatch` (main.py:5278), which is **leader-gated** (`is_leader.is_set()`, main.py:4281). Today the leader always receives the fan-out message and pokes dispatch immediately. Queue-grouping `register` would deliver it to the *follower* ~50% of the time, whose poke no-ops → the VM job waits up to the ~30 s dispatch-poll interval. **Regression.** The fix is to keep fan-out and leader-gate only the harmful side effect (the SSH seed).
- **`sudo` needs fan-out for the prompt.** The SSE prompt is an in-process broadcast (`_broadcast_sse` over `self._sse_queues`), so it only reaches operators connected to the *broadcasting* replica. Queue-grouping `sudo.request` would drop the live prompt for an operator pinned to the non-chosen replica. The fix is to keep fan-out and dedup only the *side effects* (row / reply) via a claim.
- **`session.events` needs fan-out** by design — queue-grouping it blacks out SSE clients on the non-chosen replica.

So the actual fix uses **no queue groups** — it is the two M1 primitives applied to exactly two handlers.

## Fix 1 — `register`: leader-gate the IDE seed

In `_on_daemon_register` (`nats_bridge.py:448`), the only harmful side effect is the fire-and-forget `_seed_vm_ide_config` SSH (line ~492). Gate it on leadership:

```python
# Seed IDE config once: the leader always receives the fan-out register, so
# leader-gating the seed makes it exactly-once across replicas without a queue
# group (which would break the leader-gated dispatch poke below).
from services.leader_election import is_leader   # flattened import (M1 lesson)
if ssh_host and is_leader.is_set():
    asyncio.create_task(self._seed_vm_ide_config(job_id, is_thread, ssh_host, ssh_port))
```

- The context `merge_*` upsert stays on both replicas (idempotent — harmless, and keeps VM state current even mid-failover).
- The dispatch trigger (`on_vm_ready`) is unchanged — it already self-gates on `is_leader`.
- **Import must be the flattened form** `from services.leader_election import is_leader` (not `from orchestrator.services…`) — the package-prefixed form passes unit tests but crashes the flattened `/app` image (the deploy-blocking bug M1's k3d run caught).

**Failover edge:** if a VM registers during a brief leadership gap, the seed is skipped — acceptable, since the seed is already best-effort (`_seed_vm_ide_config` never raises and the VM is fully usable without it). A transient dual-leader window double-seeds (same as today) — rare and harmless (idempotent file writes).

## Fix 2 — `sudo`: claim-before-act on the reply subject

Mirror the M1 Task-5 insert-as-claim pattern (migration 0038). The natural claim key is **`nats_reply_subject`**: NATS delivers the *same* reply inbox to both replicas for one request, and a *distinct, unique* inbox per request (NUID-based), so it dedups exact duplicates while keeping genuinely-distinct requests separate.

**Migration `0040_sudo_request_reply_subject_unique.sql`** (shape mirrors 0038): collapse any pre-existing duplicate rows sharing a `nats_reply_subject` (keep earliest) so the index can build, then add a partial unique index. The collapsed rows are bug-artifacts (one extra per request created under `replicas: 2`); confirm at implementation that no FK references `sudo_approval_requests.id` (none expected — it is a standalone request log):

```sql
DELETE FROM sudo_approval_requests a
USING sudo_approval_requests b
WHERE a.nats_reply_subject IS NOT NULL
  AND a.nats_reply_subject = b.nats_reply_subject
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sudo_request_reply_subject
    ON sudo_approval_requests (nats_reply_subject)
    WHERE nats_reply_subject IS NOT NULL;
```

`_insert_request` (`sudo_gate.py:519`, private — only called from `on_sudo_request`) becomes the claim. Add `ON CONFLICT (nats_reply_subject) WHERE nats_reply_subject IS NOT NULL DO NOTHING` to the `INSERT … RETURNING id` (the `WHERE` matches the partial-index arbiter so Postgres infers it).

**Disambiguate the two no-id outcomes** — today both collapse to `None`, but they must diverge:
- **Lost claim** (`ON CONFLICT` → no row): return `None` → caller drops silently; the winner owns the request.
- **DB error** (exception): must still **deny**, so the daemon isn't stranded (today's behaviour). The exception must propagate, not be swallowed into a `None`.

So the new contract is: `_insert_request` returns the id on success, `None` on lost-claim, and *raises* on DB error (drop the current `except → return None`).

`on_sudo_request` (`sudo_gate.py:48`) then branches **before** auto-rule evaluation, `msg.respond`, `_pending_msgs`, and the SSE broadcast:

```python
try:
    request_id = await self._insert_request(..., nats_reply_subject=reply_subject, ...)
except Exception:
    # genuine DB failure — deny so the daemon doesn't hang (preserves today's behaviour)
    await self._nats_reply(reply_subject, False, "internal error")
    return
if request_id is None:
    # lost the claim to the other replica — it owns the request; drop silently
    return
# ... winner only: auto-rule eval / respond+finalize, or store _pending_msgs + broadcast SSE ...
```

This refines the current `None`→deny branch (lines 97–100): a genuine error still denies, but a lost claim now drops silently (the winning replica already responded or will).

**Why approve still works on either replica:** `approve_request`/`deny_request` call `_finalize_request` (`sudo_gate.py:636`), which always calls `_nats_reply` → `self._nc.publish(nats_reply_subject, …)` (line 679) — it responds to the daemon via the *persisted* reply subject, independent of the in-memory `msg`. The `msg.respond()` in approve/deny is only a redundant fast-path. So even though only the claim-winner holds `_pending_msgs`, an operator approving on the *other* replica still notifies the daemon. (Verified in code — no change needed here.)

**The `NULL` reply-subject case:** the partial index excludes `NULL`, so it never constrains the non-NATS `insert_vm_upgrade_request` path (job-freeze driven, runs once on the leader, `nats_reply_subject = NULL`). NATS-originated sudo requests always carry a reply subject, so the claim always applies to them.

## Data flow (sudo, after the fix)

```
daemon ──publish sudo.request (reply=inbox-X)──▶ NATS ──fan-out──▶ both replicas
   replica A: INSERT … ON CONFLICT(reply) → id=R     (claim WON)
              → eval auto-rules → respond+finalize, OR store _pending_msgs[R] + broadcast SSE
   replica B: INSERT … ON CONFLICT(reply) → None      (claim LOST) → return
operator approves (HTTP → A or B) → _finalize_request → _nats_reply(inbox-X) → daemon ✔
```

## Residual / known limitation

After the fix, the *live* SSE prompt fires only on the claim-winner replica. An operator whose dashboard SSE is pinned to the other replica won't get the instant push — but the request row is in the DB and surfaced by `list_sudo_requests` (main.py:7794) on load/refresh/poll, so it is **seen, not lost**. Closing this fully (live push to operators on any replica) is **L3 cross-replica SSE fan-out** — a separate slice, deliberately out of scope. This is strictly better than today's behaviour (two prompts + an orphaned row).

## Failover & error edges

- **Claim-winner dies after claiming, before responding:** the row is `pending` in the DB; an operator can approve on the surviving replica (responds via `_nats_reply`), or `sweep_expired` denies after the 300 s TTL (also via `_nats_reply`). The daemon is never silently stranded.
- **Transient dual-leader / partition:** the claim is a DB unique constraint (fenced at the resource), so it holds even when leader election briefly doesn't — same guarantee as M1 Task-5.
- **`replicas: 1`:** one subscriber, claim always wins, seed always runs — behaviour identical to today.

## Files touched

- `orchestrator/database/migrations/app/0040_sudo_request_reply_subject_unique.sql` — new (mirror 0038).
- `orchestrator/services/sudo_gate.py` — `_insert_request` `ON CONFLICT DO NOTHING`; `on_sudo_request` early-return on lost claim.
- `orchestrator/services/nats_bridge.py` — `_on_daemon_register` leader-gate the seed (flattened import).
- `tests/test_sudo_request_claim.py` — new (mirror `tests/test_notify_dedup.py`).
- `tests/test_nats_register_seed_gate.py` — new (seed runs only when `is_leader` set).

## Test & verification plan

1. **Unit (PostgresContainer):** two concurrent `_insert_request` with the same `nats_reply_subject` → exactly one row, one non-`None` return; distinct subjects → both succeed; `NULL` subject → unconstrained (vm_upgrade path unaffected).
2. **Unit:** `_on_daemon_register` seed is invoked only when `is_leader` is set (patch `is_leader`, assert `_seed_vm_ide_config` called once / not called).
3. **k3d two-replica:** force a VM `register` → exactly one SSH seed in logs; issue a human-approval `sudo.request` → exactly one approval row + one prompt; approve on the **non-consuming** replica → daemon receives the response.
4. **Live dev (quiet window):** repeat (3) under real traffic; confirm no duplicate `sudo_approval_requests` rows accrue.

## Out of scope (tracked, not done here)

- L3 cross-replica SSE fan-out (closes the sudo live-prompt residual; also DB-backs `session.events`).
- L2 dispatch `SKIP LOCKED` (throughput; the safety CAS already shipped in M1).
- Queue-grouping the benign VM subjects (optional micro-efficiency, not needed).
