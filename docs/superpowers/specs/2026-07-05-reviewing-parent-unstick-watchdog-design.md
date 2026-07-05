# `reviewing` Parent Un-stick Watchdog — Design Spec

**Status:** Implemented (2026-07-05) on `develop`; unit-tested (mock-connection contract + tick wiring). Behavioral predicate matrix pending dev-cluster verification.
**Parent issue:** [`docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`](../../issues/critic_failure_leaves_parent_job_stuck_reviewing.md) — fix item **#4** ("`reviewing` watchdog"). This is **P2** in the 2026-07-04 research-loop incident ranking (see [`docs/issues/reviewing_parent_pod_reaped_under_critic.md`](../../issues/reviewing_parent_pod_reaped_under_critic.md)). P0 (live-child guard) and P1 (fast-freeze) are handled separately.
**Goal:** Guarantee that a parent job never stays wedged in `reviewing` forever when its critic dies by any path other than a clean `/complete`. Surface the wedge as an actionable `pending_review` + a notification instead of a silent stall.
**Scope:** Orchestrator only. One new DB method + one new step in an existing background sweeper. **No pod/workspace-lifecycle changes** (P0 already reclaims the pod).

---

## Why

When verification is enabled, a completed job goes `status='reviewing'` and the completion fan-out spawns a **critic** verification subjob (priority 10, sharing the parent's workspace) to check its deliverables. The critic un-sticks the parent by reaching its **own** `/complete`, which runs `_handle_critic_verdict_on_complete` (`orchestrator/main.py:9811`) → approve / return-with-feedback / implicit-approve.

**That handler is the only thing that ever moves a parent out of `reviewing`.** It has two holes:

1. A critic that ends **`failed`** hits the bare `return` at `main.py:9862-9864` — no fallback, no notification.
2. A critic that is **orphaned → `paused` → later reaped** (the real 2026-06-22 and 2026-07-04 signature) never reaches the handler at all.

In both cases the parent sits in `reviewing` **indefinitely**; manual `cancel` is the only exit. For an unattended loop this is a silent wedge per bad iteration.

Two adjacent mechanisms do **not** close the hole:

- **The existing sweeper cleans up the critic, not the parent.** `stale_verification_sweeper` → `PostgresDB.cancel_stale_verification_subjobs` (`orchestrator/database/postgres.py:2742`) cancels the dead critic row so it stops parasitically preempting the dispatcher, but deliberately never touches the parent (`postgres.py:2749`: "`reviewing` is deliberately not a cancel trigger").
- **P0 reclaims the pod only once the critic is *terminal*.** The live-child guard keys on `status NOT IN ('completed','failed','cancelled')` (`orchestrator/services/lifecycle/workspace_manager.py:673`); `reviewing` is in `_IDLE_JOB_STATUSES` (`:45`) but `is_idle`/`is_reapable` short-circuit on `has_live_shared_child` (`:212`, `:236`). So a **`paused` orphaned critic is still "live"** and keeps both the parent wedged **and its pod alive** until the 6 h critic-cancel makes the critic terminal.

Net today:

| Critic death mode | Pod | Parent |
|---|---|---|
| failed / cancelled | freed fast (P0) | wedged **forever** |
| orphaned → `paused` | freed **~6 h** (P0, after step-1 cancel) | wedged **forever** |

This spec fixes the **parent** column.

---

## Design

Fold a second, ordered step into the **existing** `stale_verification_sweeper` — not a new loop, not new event handlers. Rationale for a status-of-parent **watchdog** over patching the verdict handler: the orphaned-`paused` critic never reaches any handler, so only a parent-status sweep catches every death mode uniformly, in one place.

`_sweep_tick(db, stale_hours, grace_minutes)` becomes two ordered steps:

1. **(existing, unchanged)** `cancel_stale_verification_subjobs(stale_hours)` — cancel dead/orphaned **critics**. This is also what turns a `paused` orphan terminal at the 6 h horizon, making its parent eligible for step 2 on a later tick.
2. **(new)** `unstick_reviewing_parents(grace_minutes)` — flip parents wedged in `reviewing` whose critic pipeline is dead → `pending_review`, then notify.

Ordering matters: step 1 can make a lingering critic terminal within the same tick before step 2 evaluates the parent.

### The predicate (`unstick_reviewing_parents`)

Un-stick parent `P` when **all** hold:

- `P.status = 'reviewing'`, **and**
- `P.updated_at` is older than `grace_minutes` (grace floor), **and**
- `P` has **no critic child in any status other than `failed`/`cancelled`** — i.e. every verification subjob of `P` is terminal-failed/cancelled, or none exists.

```sql
UPDATE jobs AS p
   SET status = 'pending_review',
       error_message = 'Automated verification did not complete '
                       '(critic pipeline died); returned to manual review.',
       updated_at = CURRENT_TIMESTAMP
  WHERE p.status = 'reviewing'
    AND p.updated_at < CURRENT_TIMESTAMP - make_interval(mins => $1::int)
    AND NOT EXISTS (
          SELECT 1 FROM jobs c
           WHERE c.parent_job_id = p.id
             AND c.context->>'verification_target' IS NOT NULL
             AND c.status NOT IN ('failed', 'cancelled')
        )
RETURNING p.id, p.user_id;
```

Two clauses do the load-bearing work:

- **The `NOT EXISTS … status NOT IN ('failed','cancelled')` clause is the real gate.** It excludes both **live** critics (`processing`/`created`/`paused`/`waiting…`) *and* **`completed`** critics from triggering. Excluding live critics is what makes a genuinely long review safe — while a critic is `processing`, the parent is never touched, no matter how long it runs. Excluding `completed` critics is what prevents racing `_handle_critic_verdict_on_complete`: a `completed` critic is the verdict handler's job (incl. its "completed without verdict → implicit approval" path at `main.py:9856`), so the watchdog stays out of it. What remains — all-critics-failed/cancelled, or no critic at all — is exactly the wedge set.
- **The `grace_minutes` floor** covers the two sub-second windows the predicate would otherwise race: (a) the gap between parent→`reviewing` and critic-row insert (the "never-spawned" false trigger), and (b) an in-flight `/complete` whose verdict write hasn't landed. `P.updated_at` is normally the entry-to-`reviewing` timestamp; pod-reclamation bookkeeping may bump it, which only **delays** the un-stick, never causes a premature one.

`RETURNING p.id, p.user_id` feeds the notification loop; the DB method stays pure (no I/O beyond the UPDATE).

### The flip

- **CAS via `WHERE p.status='reviewing'`.** Idempotent and dual-leader-safe: a second sweeper (or the verdict handler) that already moved the parent finds it no longer `reviewing`, so the UPDATE no-ops. Mirrors `claim_delegation_resume` (`postgres.py:2831`).
- **`status → 'pending_review'`** — the human/loop-actionable review state. The approve endpoint already accepts `pending_review`, and the pod is (or will be) freed by P0, so review needs only the Gitea branch.
- **`error_message` marker** so cockpit shows *why* the state changed rather than a silent flip.
- **Notification** — for each `RETURNING` row, the sweeper calls `queue_notification(user_id, job_id, thread_id=None, subject, message, channels)` (`postgres.py:2880`, the headless digest queue). `channels` shape mirrored from existing callers at implementation time. The `pending_review` state itself also surfaces in cockpit as a backstop.

### Horizons / config

| Knob | Default | Meaning |
|---|---|---|
| `STALE_VERIFICATION_SWEEP_SECONDS` | 300 (existing) | Sweeper tick cadence. |
| `STALE_VERIFICATION_HOURS` | 6 (existing) | Step-1 critic-cancel horizon. **Unchanged.** |
| `REVIEWING_STUCK_GRACE_MINUTES` | 30 (**new**) | Step-2 grace floor on `P.updated_at`. |

Resulting latency after the fix:

| Critic death mode | Parent un-stuck | Pod freed |
|---|---|---|
| failed / cancelled | **~grace (≈30 min)** | fast (P0, already) |
| orphaned → `paused` | **~6 h** (rides step-1 cancel → terminal → step 2) | ~6 h (P0, already) |
| critic never spawned | ~grace (≈30 min) | fast (P0 — no live child ever shared it) |

---

## The orphaned-`paused` horizon: decision & rationale

The one real design fork was whether to beat the **6 h** latency for the orphaned-`paused` case. **Decision: accept 6 h in v1.** The 6 h is *inherited* from existing mechanisms (step-1 cancel + P0 pod reclamation), not something this spec builds — the watchdog's only new contribution to the `paused` case is un-sticking the **parent** (today: forever) once the critic is terminal.

Options considered and rejected:

- **(c) Treat "agentless + `paused` past a short grace" as dead** (cancel the critic + un-stick immediately). **Rejected — unsafe.** A `paused` critic is not necessarily dead: orphan-recovery sets it `paused` and re-dispatches it (`get_dispatchable_jobs` re-runs a `paused` job once `freeze_data IS NULL` and no ancestor is `paused`/`cancelled`/`failed`; a `reviewing` parent does **not** block it — `postgres.py:3004`). So a critic that is `paused` and about to resume-and-succeed would be wrongly killed, aborting a legitimate review. The existing 6 h horizon exists precisely to outlast any transient pause.
- **(b) Lower `STALE_VERIFICATION_HOURS` globally.** **Rejected** — same "might be recovering" risk as (c), applied to every orphaned critic instead of one.

**Is preemption a risk here?** No. Critics spawn at `priority=10` (`main.py:10092`), which is the job ceiling (`CreateJob` is `Field(5, ge=0, le=10)`, `main.py:4956`), and preemption fires only on a **strictly higher** priority (`pending_priority <= candidate_priority → skip`, `main.py:4698`). A running critic is in the preemption candidate set (`get_preemption_candidates` returns all `processing`+assigned jobs, `postgres.py:3886`) but can never be selected — nothing outranks 10. So `paused` critics come from **orphan recovery, not preemption**. (The `le=1000` "priority" elsewhere is on `SudoRuleCreateRequest`, not a job.) This is why the **base predicate** — which treats `paused` as live — is safe against every pause path.

**Faster-than-6 h path (explicit follow-up, out of scope):** beating 6 h *safely* needs a **positive deadness signal** — e.g. an orphan re-dispatch/thrash counter ("this critic has been re-dispatched and re-orphaned N times on a dead workspace") — not "paused too long." No such general counter exists today (only narrow bounded-re-dispatch caps for LLM-outage/memory/VM-snapshot). Adding it, then letting step 2 treat a thrashing critic as dead, is a separate change.

---

## Scope boundaries

- **In:** the parent DB-status un-stick (new DB method + sweeper step + notification).
- **Out — no new idle-suspend for long reviews.** The parent issue's tail item is **redundant**: P0's existing `is_idle`/`is_reapable` path already snapshots+frees a `reviewing` parent's pod once no live child references it. This spec does not touch pod lifecycle.
- **Out — no verdict-handler patch.** Deliberately watchdog-only (see Design rationale). Adding a fast event-path for the `failed` critic case is a possible later latency optimization, not v1.
- **Out — the faster-than-6 h orphan path** (needs the thrash counter above).

---

## Testing (TDD)

Failing test first, then implementation.

1. **DB predicate** (Postgres-backed, e.g. alongside `tests/test_workspace_suspension.py` / the lifecycle suites), parametrized over the critic end-state of a `reviewing` parent past grace:
   - critic `failed` → **un-stuck** to `pending_review`.
   - critic `cancelled` → **un-stuck**.
   - no critic row (never spawned) → **un-stuck**.
   - critic `processing` → **NOT** touched (long-review safety).
   - critic `paused` → **NOT** touched (recovering-critic safety).
   - critic `completed` → **NOT** touched (verdict handler's job; anti-race).
   - parent `reviewing` but **within** grace → **NOT** touched (spawn-window / in-flight-verdict safety).
   - CAS: second invocation is a no-op (already `pending_review`).
   - `RETURNING` yields `(id, user_id)` for the notification.
2. **`_sweep_tick`** unit test: step 1 then step 2 ordering; a critic cancelled by step 1 makes its parent eligible in step 2 the same tick; `queue_notification` called once per un-stuck parent.

---

## Files touched

- `orchestrator/database/postgres.py` — new `unstick_reviewing_parents(grace_minutes)`.
- `orchestrator/services/stale_verification_sweeper.py` — add `REVIEWING_STUCK_GRACE_MINUTES`, call the new method in `_sweep_tick`, iterate `RETURNING` rows → `queue_notification`.
- Tests as above.
- `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md` — mark fix item #4 as designed/implemented, link this spec.
