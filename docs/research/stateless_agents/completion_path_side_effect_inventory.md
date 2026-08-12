# Completion-path side-effect inventory

Evidence base for `stateless_agents.md` §5.4.5 (Gate 3). Produced 2026-08-08 by
reading the whole of `POST /api/jobs/{job_id}/complete` and everything it
dispatches into, on `feature/stateless-agents`. Handler span:
`orchestrator/main.py:17389–18432` — **1046 lines, 29 database awaits.**

Line numbers were taken on `feature/stateless-agents` before the 2026-08-09
consolidation and **will have drifted** — the merge brought in the S3 freeze
registry, which changed the import source for `AUTO_REDISPATCH_FREEZE_TYPES`,
and later session work touched `main.py`. Treat the numbers as anchors to
re-locate, not as coordinates to trust; the effect ORDER and the idempotency
classifications are what this document is for, and those are unchanged.

> Read this before proposing any completion change. Two design claims in the
> v3 doc about this path turned out to be wrong when checked against the code
> (a completion CAS that does not exist, and a phantom-COMPLETE mapping that
> had already been fixed). The path is large enough that reasoning about it
> from prose is unreliable.

---

## The three structural facts

**1. There is not a single explicit transaction in the handler.** Every write is
`async with postgres_db.acquire() as conn: await conn.execute(...)`, which
autocommits. The handler says so itself at `main.py:18415–18418`. The only
explicit `conn.transaction()` reachable from the path wraps one counter
(`postgres.py:2986`). A simple completed job commits 8–12 times; a loop job with
cloud delivery commits 20–30 times, interleaved with an unbounded number of
WebDAV PUT/DELETEs.

**2. The status write at `main.py:18060` is a one-way door.** It flips the job
into a state the early late-callback guard (`main.py:17434`) treats as "already
handled". Everything after it — `completed_at`, freeze notification, subjob
graft, critic verdict, verification spawn, curation, loop advance, terminal
merge, change record, session wake, **and the workspace archive** — is orphaned
by any crash in that window, permanently, because a replay returns at the guard.

**3. Every "already done?" check is check-then-act across a long external-I/O
window.** Critic spawn, subjob graft, cloud apply, and terminal merge all read a
marker, do seconds-to-minutes of external work, then write the marker.

---

## Ordered inventory

Kind: **DB**=database write · **EXT**=external I/O with no rollback ·
**SPAWN**=creates a job · **NOTIFY**=operator-visible message · **TASK**=detached asyncio task.

| # | Effect | Evidence | Kind | Idempotent? | Crash-after consequence |
|---|---|---|---|---|---|
| S1 | Late-callback early return (status in completed/reviewing/pending_review) | 17434–17445 | — | YES | guards *everything* below |
| S2 | Clear stale failure | 17470; `postgres.py:1753` | DB | YES by value, **no status predicate** | a concurrent real failure gets erased |
| S3 | Persist reported `freeze_data` | 17507–17518 | DB | YES | stale blob poisons a later report (`_parse_freeze_data` prefers DB) |
| S4 | Drop `queued_replies` | 17528–17537 | DB | YES (self-guarding) | — |
| S5 | infra_transient give-up → failed | 17573–17583 | DB + checkpoint delete | CONDITIONAL (counter read is not) | workspace never archived → pod/VM leak |
| S6 | infra_transient pause: counter, freeze, pause | 17605/17615/17632 | DB ×3 commits | **NO** — replay burns a retry | mid-window: counter advanced with no freeze, or freeze written while still `processing` → dispatcher-invisible wedge |
| S7 | Pod workspace recovery | `completion.py:479–620` | DB + **EXT** (k8s pod deletes ×2, SSH probe) | counters **NO**; `pause_job_shed_freeze` **YES** (CAS) | pod gone, row still `processing`; one strike poorer per replay |
| S8 | VM recovery: mark, delete VM, pause | 17699–17723 | DB + **EXT** (cloud VM delete) | CONDITIONAL — `recovering` guard is check-then-act | **permanent wedge**: `recovering=True` durable, VM alive, replay short-circuits and never deletes or pauses |
| S9 | Reset recovery strike counter | 17739–17741 | DB | YES | — |
| S11 | memory/kb retry increment + pause | 17781–17783 | DB | **NO** — replay walks toward the cap | retry consumed, job still `processing` |
| S12 | LLM-outage: increment, next_retry_at, pause | 17820/17854/17875 | DB ×3 | **NO** | freeze with future retry while still `processing` → sweeper won't claim |
| S13 | LLM give-up operator alert | 17895 | **NOTIFY** | **NO** (no dedup) | duplicate pages |
| S14 | Deliverable-contract gate + bounce | `deliverable_gate.py:400–560`; 14404 | DB + **EXT** (Gitea, vector reads) | bounces **NO**; bounce write has **no status predicate** | counted-but-not-bounced; also the one early return that skips S15–S37 |
| S15 | **Loop project-cloud delivery** | `job_cloud_baseline.py:722–769, 900–937` | **EXT: WebDAV PUT/DELETE per file** + DB | CONDITIONAL — guard read at entry, applied after the whole walk | **worst window**: files durably in the customer's cloud, row says `pending`; replay re-reads its own writes as external divergence → `cloud-conflict` → parks at `pending_review` |
| S16 | Mode A diff capture | 18035–18039 | DB + EXT (Gitea read) | YES | — |
| **S17** | **Main status write** | 18049–18060; `postgres.py:1663` | DB + checkpoint delete | value-YES, **no CAS at all** (`WHERE id=$N`) | **the one-way door — orphans S18–S37 including the workspace archive** |
| S18 | Clear `assigned_agent_id` on pause | 18070–18077 | DB | YES | — |
| S19 | Stash freeze to context, then null the column | 18104 then 18112 | DB ×2 | CONDITIONAL | mid-window: freeze in *both* places → `get_dispatchable_jobs` never selects it (partial index 0046) → **permanent invisibility** |
| S20 | Drain-stall counter + alert | 18136–18152 | DB + **NOTIFY** | **NO** | counter drift, spurious pages |
| S21 | `completed_at` | 18169–18173 | DB | **NO** — no `COALESCE` (unlike `failed_at`) | `completed` with `completed_at IS NULL`, unrecoverable by replay |
| S22 | Sudo approval request insert | `sudo_gate.py:620–709` | DB + SSE | **NO** — plain INSERT, no unique key | duplicate 24h approval requests per replay |
| S23 | Auto-deny resume | 14274–14369 | **EXT: local file unlink** + DB | CONDITIONAL (Python-side status check) | artifact deleted while row still frozen |
| S24 | Freeze workspace snapshot (detached) | 18267 → 14234–14271 | **TASK + EXT: SSH + S3** | **NO dedup** | task dies with the process; nothing records the attempt |
| S25 | Freeze notification | 18272–18278 | **NOTIFY** | **NO** | duplicate mail |
| S26 | **Subjob output graft** | 1271–1371 (commit at 1351, marker at 1359) | **EXT: Gitea commit** + DB | CONDITIONAL — check-then-act | **double-graft window**: commit on the parent branch, marker unset → replay grafts again under a fresh ordinal |
| S27 | Critic verdict → target job | 15396–15483 | DB + NOTIFY | status writes no-CAS; `completed_at` re-stamped; `returned` re-queues target | can drag a moved-on target back to `paused` |
| S28 | Scholar completion unblocks parent | 14791 read → 14826/14827 write | DB | CONDITIONAL — check-then-act, **no CAS** (sibling path has one) | parent has output but stays `waiting` forever |
| S29 | Delegation child unblocks parent | 14923 → `claim_delegation_resume` 14933 | DB | **YES — real CAS**, dispatch gated on it | replay completes cleanly. *The model to copy.* |
| S30 | **Verification critic spawn** | guard 15689 → INSERT 15828 → branch 15867 | **SPAWN + EXT: Gitea branch** | **CONDITIONAL, weakest link** — ~140 lines between check and act, no unique key on `(parent_job_id, round)` | documented at `postgres.py:5758–5764`: two critics → colliding finding ids → `fold_open_findings` drops a blocking finding → **unwarranted approval** |
| S31 | Curation final pass resume | 17216–17237 | DB + dispatch | **NO in effect** — `queue_job_for_resume` sets `paused`, so the same curator is re-selected next run | double dispatch |
| S32 | Loop advance | 16909–17162 | DB + **EXT** (KB reindex, Gitea repo create) + SPAWN | barrier claim (17046) **YES, real CAS**; retro record **YES** (PK+ON CONFLICT); reindex/notify/TTL-decrement/spawn **NO** | torn advance: barrier drained but loop points at nothing; replay cannot re-claim → needs the sweeper |
| S33 | Terminal merge + change record | `completion.py:843` merge, `:866` stamp | **EXT: Gitea merge to `main`** + DB | merge CONDITIONAL (backstop reads pre-merge row); record **YES** (PK + ON CONFLICT) | **double-merge window**; and if S17 already fired, the record is permanently lost |
| S34 | Session wake enqueue | `session_wake.py:121–169`; `postgres.py:6034` | DB | **YES** — guarded update + `ON CONFLICT` dedup key | — *the other model to copy.* |
| S35 | Dispatch trigger | 18400 | TASK | YES (dispatcher has its own lock) | — |
| **S36** | **Workspace archive + teardown** | 6497–6593; `container_provisioner.py:585–647` | **EXT: S3 upload, k8s pod + PVC + Service delete, VM release** | CONDITIONAL, and the guard depends on a status *nobody in this path writes* | **point of no return.** PVC deleted ⇒ working tree unrecoverable. And because S17 already fired, a replay never reaches here — **the workspace leaks until the reaper** |
| S37 | Session-wake drain kick | `session_wake.py:172–191, 401–433` | TASK + **EXT: HTTP to agent** | YES at this level — `claim_pending_job_wakes` dedups | — |

---

## The only real idempotency keys that already exist

Worth naming, because a redesign should extend these rather than invent a new style:

- `job_change_records.job_id` — PK + `ON CONFLICT DO NOTHING` (`postgres.py:2008`).
- `session_wake_events` — partial unique dedup index on `(thread_id, source, dedup_key) WHERE state='pending'` (`postgres.py:6246+`).
- Real CAS guards: `pause_job` / `pause_job_shed_freeze` (`WHERE status='processing'`), `claim_delegation_resume` (`WHERE status='waiting'`), `claim_project_loop_stage_barrier`, `mark_job_wake_pending`, `claim_pending_job_wakes`.

Everything else is either a blind write or a check-then-act.

---

## Counters: race-safe but not replay-safe

`infra_transient.attempts`, `recovery_attempts`, `deliverable_gate.bounces`,
`auto_continue_drains` are read-modify-write against the **entry-time job
snapshot**. `memory_retry_count`, `llm_outage.attempt` and
`knowledge_index.remaining_cycles` are atomic increments. Neither shape is
idempotent: **every replay silently consumes retry budget**, and enough replays
convert a recoverable job into a terminal failure.

---

## Destructive concurrent interleavings (not merely redundant)

1. **Duplicate critic spawn → unwarranted approval** (S30). The most severe.
2. **Double graft** onto the parent branch (S26).
3. **Double cloud apply**, or a spurious `cloud-conflict` park (S15).
4. `update_job_status` (S17) has no CAS — overwrites a concurrent approve/reject/pause.
5. `clear_job_failure` (S2) has no predicate — erases a concurrently recorded failure.
6. Scholar unblock (S28) is check-then-act where the delegation sibling uses a CAS.

---

## Live bugs today, independent of the stateless lane

These are not stateless-lane risks; they are reachable now on the pinned lane,
and per-turn/per-batch claiming only raises their frequency:

- A crash between S17 and S36 **leaks the workspace pod, PVC and VM** with no row recording it, and the replay guard prevents recovery.
- A `completed` job can carry `completed_at IS NULL` permanently (S17→S21 window).
- The duplicate-critic race (S30) can drop a blocking finding and approve work that should have been returned.
- The S8 VM-recovery guard can wedge a job permanently (`recovering=True`, VM alive, replay short-circuits).
- The S19 window leaves a paused job invisible to the dispatcher forever.

---

## External systems with no transactional rollback

Kubernetes (pod, PVC, Service deletes) · cloud VM API (delete/release) ·
WebDAV/Nextcloud (per-file PUT/DELETE, explicitly fail-soft) · Gitea (commit,
branch create, repo create, merge to `main`) · S3 (snapshot uploads) ·
orchestrator local disk (`unlink`) · embedding provider + vector DB (reindex,
TTL decrement) · SMTP/ntfy/SSE · HTTP to the agent pod.

No Neo4j and no NATS in this path.
