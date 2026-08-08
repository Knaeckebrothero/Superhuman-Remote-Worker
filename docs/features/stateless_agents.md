# Stateless Agents — Turn Execution as a Deployment

**Status:** v3.2 — **S1 spine IMPLEMENTED, k3d-verified, and PERFORMANCE-TUNED (2026-08-07/08)**: run_queue substrate (migrations 0115/0117 + `src/shared/run_queue/`, 81 real-PG tests), epoch/seq redesign (0116 + `src/shared/event_journal/`; live epoch-stable reattach), stateless turn executor (`src/api/turn_executor.py`, `--mode stateless`), orchestrator control plane (enqueue-on-input, claim-bundle, leader reaper, admin read model), `agent-stateless` Deployment (chart-gated, default off). Fault-injection matrix PASS (steal ≤105 s + `turn.interrupted`, zombie heartbeat-abort + fence, skip-if-answered, FIFO drain across a steal, exactly-once answers) and **re-verified after the claim-path change** (pod force-delete mid-turn → steal at t+97 s → exactly one answer, epoch +1, affinity hint cleared). **Turn latency 99.6 s → 5.4 s cold / 3.0 s warm** (§5.3.3 measured decomposition, §5.3.4 affinity): the attach-time and teardown cloud pulls were duplicate full-tree walks, `webdav3` listing cost ~2.5 s per directory where one `Depth: infinity` PROPFIND costs ~1 s, and virtual-tier setup spent 51 rclone process spawns answering existence probes that one scoped listing now answers. Build log, per-milestone results, found-and-fixed issues, commit map, and the S1 follow-up list: `docs/research/stateless_agents/implementation_log.md`. **Branch: `feature/stateless-agents`** — 10 commits, `e506fdfa`→`af5c38a0` (spine) and `c290f525`→`6a360848` (performance), not pushed; `develop` untouched. Gate: 15 013 tests pass, ruff clean; the 11 remaining local failures reproduce on `develop` (Python 3.14 env noise). Related implementation docs carrying pieces of this work: `no_workspace_agent_mode.md` §5.1 (op count is the cost — the virtual backend's scoped metadata index), `cloud_collaboration_model.md` §4 (one `Depth: infinity` PROPFIND per turn boundary). **Where we are and what is left: §9.1 Implementation status** — the substrate is shared and kind-agnostic, but only the session driver exists; workers (S3) and S2 are untouched. S1's server provisioning/transport-discovery gate is built on `feature/stateless-sessions-s1-completion`, while cockpit consumption, durable control transport, metering, and durable queued-turn status remain open. v1 2026-08-06 (initial proposal); v2 2026-08-07 after an 8-agent research fan-out (4 codebase deep-dives, 4 web sweeps); **v3 same night after a 6-lens adversarial panel** (concurrency, migration/ops, perf/cost, security, product/UX, completeness — 10 critical + 28 major findings folded in; the four sharpest code claims were independently re-verified). Raw research + critic reports with full evidence trails: `docs/research/stateless_agents/`. Largest v2→v3 changes: the queue got its **completion half** (§5.1), the epoch/lease-token unification was **split into two counters** (§5.2/§5.3.2), a **coexistence ruling** for the two lease systems (§5.4.4), a **security threat model** (§5.6), an honest **TTFT/DB-load budget** (§5.5, Appendix), and S1's scope grew a cockpit workstream plus the discovery that **lite sessions carry PVC-backed agent-local state today** (§6.1).
**Origin:** user proposal — an LLM turn is conversation JSON in, bigger conversation JSON out; so agents can be a Deployment, not pinned pods.
**Related:** `docs/go_rewrite.md` (names this flip; two sketches inverted by evidence — §5.1, §5.2), `docs/features/job_execution_lease.md` (shipped substrate), `docs/features/session_reliability_and_transport_simplification.md` (P5/P6 converge here; **P5 is blocked-by §5.3.2 — record it there**), `docs/features/worker_runtime_strategy.md` (no-new-runtime decision — this is a driver/deployment change), `docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md` (D3).

## 1. The idea

A turn is a pure function: `(thread state, input, config) → (new messages, events, effects)`. Files, shell, git live on an external workspace pod reached over SSH; conversation state lives in Postgres. Nothing about an "agent" needs to be a long-lived process bound to one conversation.

- Agent pods become a plain **Deployment**. No identity, no registration, no pinning.
- A user message (or a worker-job continuation) is a **row in a queue**. Any pod claims it under a lease, loads state + config from Postgres, runs the turn exactly as a stateful agent would, executes tools against the workspace pod, persists results, completes, releases.
- Workers get a **batch budget**: the claiming pod runs to the next phase boundary (or a wall-clock cap), checkpoints, releases; the job re-enters the queue for any pod.

"Agent" becomes what it always was underneath: a config plus a thread of messages.

## 2. Why

**Utilization — honest asymmetry, honestly sized.** Sessions are the prize: duty cycle ~5–20%, yet each pins a ~300–500 MB pod. But sizing is a *latency* problem, not a mean-utilization problem: 50 sessions × 20% duty = 10 erlangs offered — 10 one-turn-per-pod servers at ρ≈1 queue unboundedly. The right claim: **~13–15 pods serve 50 sessions with small P(wait)** (pods ≈ erlangs / 0.6–0.7), still a ~3.5× win over 50 pinned pods, and the HPA floor targets that formula, not average duty. Workers gain little throughput (duty ~100%; 15 jobs on 10 pods is time-slicing) but gain **admission, fairness, and preemption**. New trade to name: a pinned session never waits behind strangers at turn time; a pooled one can — §5.3.7 gives that wait a UX and an acceptance bound.

**The quadrant argument.** The credible alternative is the *hibernating actor* (Cloudflare Durable Objects hibernate at ~10 s idle, WebSockets held open, zero duration cost; Cognition built hypervisor snapshot + power-down at ">1 year of hypervisor engineering"). Pinning isn't wrong; **pinning-without-hibernation is the worst quadrant**, and Kubernetes sells no hibernation for a half-GB Python pod. What the actor model gives up teaches what we must build: its free per-entity serialization becomes our lease (§5.2), its in-memory hot state becomes our soft affinity (§5.3.4).

**Industry exhibit.** OpenAI deprecated the Assistants API — stateful `threads`+`runs` pinned executions — hard shutdown 2026-08-26, for stateless request/response over a server-side conversation store plus background mode with cursor-resumable streaming. LangGraph Platform runs the same shape (Postgres run queue, worker leases, ≤1 run/thread, resume-from-checkpoint). OpenHands v1 (`step(state) → action` over an event log, sandboxed executor split) is the closest open-source twin.

**Operations.** Scaling becomes replicas/HPA on queue depth. The agent-lifecycle control plane gets deleted **per lane, on lane retirement** — not at flag-flip (§7's delete-when column). The honest steady state during the soak is *dual control planes*, priced in §9.

**Reliability — a bug-class graveyard.** Structural pod-pinning bugs dissolve rather than get fixed: the stale-agent-detector class, drain-strips-workspace, the exit-137 wedge, idle-reap wipes, the fresh-vs-resume seeding asymmetry (one lane: every claim loads everything), and the dead-pod `awaiting_user` trap observed live during the S1 build — a pinned thread whose pod died mid-life is unreachable by both `/input` ("agent unreachable") and `/resume` (409 for any status but 'ended') until something flips its status; a queue-lane thread cannot enter it (no pod to die — the next input just enqueues). Two live replica-unsafe spots get fixed as a side effect: the per-turn input lock dict (`main.py:30385` — "Single-instance orchestrator" is stale; dev runs 2 replicas) and `_threads_suspending`; the bench-sweeper double-submission race is the in-repo proof of unclaimed work under 2 replicas. And the design *forces* fixing a hole that already exists: `thread_messages` writes are unfenced today — the zombie-writer window is live in current session persistence (§5.2).

**UX.** Session creation stops meaning "provision a pod, watch a spinner": create-to-*accepted* becomes sub-second; create-to-first-token is budgeted honestly in §5.5 (provider TTFT dominates — "sub-second TTFT" was v2 overclaim).

## 3. What the industry converged on (2024–2026)

Three consensus lines (OpenAI, LangGraph Platform, Temporal, Restate, Inngest, OpenHands independently): (1) **durable store owns state; stateless executors claim via queue + recorded lease**; (2) **journal results, never re-execute side effects** (nobody replays LLM calls); (3) **streaming = durable journal + sequence cursor** (OpenAI background mode's `sequence_number`+`starting_after` is shape-identical to our `thread_events`; even LangGraph Platform never streams worker→client directly). Two complications absorbed, not dismissed: LangGraph Platform needed Redis *as a ping/fan-out plane only* (data stays in PG — the ping-not-payload split, §5.5); and **warm pools migrate to the sandbox layer rather than vanish** (Cognition's hardest infra is workspace-tier provisioning; industry answer = a pause tier — E2B pause ≈4 s/GiB, resume ≈1 s — §10.7).

## 4. How much already exists

1. **Workspaces are external.** SSH to workspace pod/VM; browser executes workspace-side; IDE is code-server *in the workspace pod* behind `ide_proxy.py`; canvas skill files carry a workspace manifest. The v1 "attachment long tail" reduces to canvas presence (§10.8).
2. **Worker graph state is in shared Postgres** (D3): cross-pod resume live-verified; delete-on-terminal + keep-last-3 retention; checkpoint writes ~11 ms p50 flat; restore ≈600 kB worst.
3. **A job execution lease shipped** (migration `0054`): claim CAS + 180 s pickup lease (`claim_job_for_agent`), heartbeat-renewed 90 s fenced by `assigned_agent_id`, `recover_expired_lease_jobs` as primary orphan recovery. Missing: pull-claiming, claim-token renewal, the stage-4 completion CAS, batch release (which `PUT /api/jobs/{id}/agent-release` already implements). **Caveat found in review: the real heartbeat is 60 s** (several "5 s beats" docstrings are stale) — one beat >30 s late can expire a healthy lease; §5.2 replaces the transport.
4. **The batch break half-exists**: workers drive `astream(stream_mode="values")` with a per-superstep cooperative break in production (pause/cancel/preempt/drain); `/complete` already fires `_trigger_dispatch()`.
5. **Session conversation state is message-granular**, with **accept-time input durability** (user message persisted before the 200). Only the turn-trigger is process memory. `thread_messages.seq` is DB-side BIGSERIAL — the cross-pod-safe allocation pattern the event journal lacks.
6. **Journal-only token streaming is the shipped path**: per-token `thread_events` rows, batched ordered writer, SSE with `Last-Event-ID` cursor replay, epoch zombie guard, any-replica serving.
7. **Per-request config resolution is the rule**: sessions re-resolve on every attach ("there is no freeze"); job dispatch ships the frozen `resolved_config` blob (~127 kB); **every resume re-runs credential injection + grant PEP + datasource re-authorization** (`_resume_job_on_agent`).
8. **Approvals, steering, officer wakes are data** (DB rows + NOTIFY; DB-direct steering pull exists at phase boundaries; officer sleep = durable timer).
9. **The claim/fencing idioms are house style**: single-statement CAS transitions; SKIP LOCKED in `cron_dispatcher` + infra metering; and the closest template, `datasource_reconciliation.py` — SKIP LOCKED + lease + **never-reused sequence claim token checked on every completion write**. New lock keys go in `lock_ids.py`.

## 5. Design

### 5.1 Work queue — full lifecycle, admission, fairness

One small hot table (Solid Queue's lesson: poll a tiny indexed set — 5.6 M jobs/day at 1300 poll qps, 110 µs/query):

```sql
CREATE TABLE run_queue (
  unit_id       UUID PRIMARY KEY,            -- thread_id | job_id | bg task id (row is DURABLE per unit)
  unit_kind     TEXT NOT NULL,               -- 'session_turn' | 'worker_batch' | 'bg_task'
  dedup_key     TEXT,                        -- collapsible bg work, e.g. 'cloud_push:<thread>'
  state         TEXT NOT NULL DEFAULT 'queued',  -- queued | leased | done | parked
  priority      INT  NOT NULL DEFAULT 0,
  fair_key      TEXT,                        -- user id: per-user round-robin for session_turn (§5.3.7)
  run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempts_since_completion INT NOT NULL DEFAULT 0,
  max_attempts  INT NOT NULL DEFAULT 5,
  lease_token   BIGINT NOT NULL DEFAULT 0,   -- fencing token: MONOTONIC per unit; bumped on EVERY claim and steal
  leased_by     TEXT,                        -- pod name: diagnostics only, never correctness
  leased_until  TIMESTAMPTZ,
  input_seq     BIGINT,                      -- newest thread_messages.seq enqueued for this unit
  consumed_seq  BIGINT,                      -- newest seq a COMPLETED turn has answered
  queued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX  idx_run_queue_claim  ON run_queue (unit_kind, priority DESC, queued_at) WHERE state='queued';
CREATE INDEX  idx_run_queue_expiry ON run_queue (leased_until) WHERE state='leased';
CREATE UNIQUE INDEX idx_run_queue_dedup ON run_queue (unit_kind, dedup_key)
  WHERE dedup_key IS NOT NULL AND state = 'queued';   -- 'queued' ONLY — see below
```

**Rows are durable per unit** (never deleted while the thread/job lives) so `lease_token` stays monotonic — delete-and-reinsert would reset it to 0 and break fencing. The state machine is `queued → leased → {done | queued | parked}`.

**The completion half** (v2's missing quarter; review-critical). An LLM turn is maximally non-idempotent, so the lifecycle needs all four statements, not three:

- **Complete** — one *fenced* transaction: final message persist + `consumed_seq = input_seq`-as-read-at-claim + `state = CASE WHEN input_seq > $consumed THEN 'queued' ELSE 'done' END` + `attempts_since_completion = 0`. Re-queue-on-completion is how mid-turn input gets its next turn.
- **Skip-if-answered** — every claim compares `consumed_seq` to `input_seq` *before invoking the LLM*: a steal that lands between the final persist and completion must not re-answer an answered turn (the re-executor holds a *valid* new lease, so the fence alone cannot prevent this — the watermark does).
- **Voluntary error release** — the pod releases with `state='queued'`, `attempts_since_completion+1`, backoff on `run_after` (today v2 had backoff only in the reaper).
- **Input during a leased turn** — the input insert updates `input_seq` only (never touches `state`; flipping a leased row breaks the lease). Completion sees `input_seq > consumed_seq` and re-queues. This is the "enqueue" double-texting policy made precise.

**Admission — who enqueues** (v2 never said). Every wake path ends in **one shared enqueue helper**, replacing `_trigger_dispatch` for the stateless class: session input POST (same transaction as the message insert); job creation *after* the kept workspace pre-flight — i.e. **the leader dispatcher remains load-bearing as the worker enqueuer** (VM/sandbox provisioning gates stay where they are); automation cron fires; delegation merge (`claim_delegation_resume`'s waiting→paused CAS becomes wake→enqueue); blocking-message reply wake; approval/review resolution; `resume_job_with_feedback`; the llm-outage backoff sweeper. Consistency rule: **a `worker_batch` row exists iff the job is runnable** (`created`-and-workspace-ready or rotation-paused); `waiting`/`waiting_for_reply`/`pending_review` have no row. Queued-row disposition on user pause/cancel/priority change: the same helper CASes the row (`state='done'` on cancel; `run_after`/`priority` update otherwise).

**Ordering and fairness.** Claim order `priority DESC, queued_at ASC` with `queued_at` reset on every release = round-robin within priority (today's `created_at ASC` would let the oldest job win every cycle and starve the newest — a v1 fairness claim that was false without this). `session_turn` claims add a per-`fair_key` dimension so one user's 20 parallel sessions can't starve another user's single turn. `bg_task` never preempts interactive claims (separate `unit_kind` poll, lower priority, or the worker deployment serves it).

**Dedup for collapsible tasks is `state='queued'` only.** v2's `('queued','leased')` scope silently *swallowed* signals: a cloud-push enqueued while its predecessor is mid-walk would dedup into a push that may already be past the changed files — the exact silent-staleness class the 08-06 fix closed. With queued-only dedup, one pending + one running may coexist; the per-mount sync-generation fence (§5.3.5, now mandatory) serializes them.

**Poll-primary dispatch.** Idle pods run the claim every 250 ms–1 s (this **inverts `go_rewrite.md`'s LISTEN/NOTIFY sketch**: NOTIFY takes a global commit-serializing lock — recall.ai took three outages; it's lost to disconnected listeners; broken under PgBouncer transaction pooling). If a ping is ever added: payload-free, per-enqueue, post-commit, dedicated listener connection, never a correctness dependency.

### 5.2 Lease and fencing — two counters, four layers

The lease is **recorded state** (pgmq/SQS visibility-timeout model), never a held lock (advisory locks pin a backend per batch and break under transaction pooling; row locks held for minutes trip idle-in-transaction timeouts and pin vacuum xmin). SKIP LOCKED appears only inside the claim statement.

**Two counters, deliberately** (v2 tried to unify them and contradicted itself):

- **`lease_token`** — bumped **unconditionally on every claim and every steal**. Not client-visible, so per-turn bumps are free. This is the Kleppmann fencing token every persist checks. Same-pod re-claims get a *new* token — which is what invalidates stragglers from the previous claim (post-release background work may therefore touch **only unfenced stores**: vector DB, audit store, object storage, workspace — never `thread_messages`/`thread_events`/checkpoints/job rows; anything fenced must complete before release).
- **`threads.events_epoch`** — the **client-visible writer-generation**, bumped **only by the reaper's steal, by rewind, and by nothing else**. A clean handoff (release → claim by a different pod) does **not** bump: with seq DB-allocated and monotonic (§5.3.2), a clean cross-pod handoff is *invisible to the client* — which matters because cross-pod re-claims are routine exactly at the utilization this design is sold on; "bump on writer change" (v2) would have partially re-imported the per-turn cascade under load. The reaper is the only steal-bumper; the post-steal claim must not re-bump. `leased_by` stays diagnostics-only.

**Claim** (single statement; also CASes the `jobs` row for `worker_batch` in the same transaction — §5.4.4):

```sql
WITH c AS (
  SELECT unit_id FROM run_queue
  WHERE state='queued' AND unit_kind=$1 AND run_after<=now()
  ORDER BY priority DESC, queued_at LIMIT 1
  FOR UPDATE SKIP LOCKED)
UPDATE run_queue r SET state='leased', lease_token=lease_token+1,
  leased_by=$2, leased_until=now()+interval '60 seconds',
  attempts_since_completion=attempts_since_completion+1
FROM c WHERE r.unit_id=c.unit_id
RETURNING r.unit_id, r.lease_token, r.input_seq, r.consumed_seq;
```

**Heartbeat** — an async task per claim, every 20 s (TTL/3), *independent of* the graph loop and tool calls (an astream hook would starve during a 10-minute tmux command — this settles v1's lease-TTL-vs-long-tools question). `UPDATE … WHERE unit_id=$1 AND lease_token=$2 AND state='leased' RETURNING leased_until` — zero rows = lease lost = abort, no further persists. The renewal `RETURNING` also carries job status + pending guidance (§5.4.3).

**Reaper** — a leader-gated 15 s loop, but **per-row SKIP LOCKED form, not one bulk UPDATE**: v2's single UPDATE over all expired rows would block on any in-flight persist's `FOR SHARE` — one wedged persist transaction would stall *every* steal until its timeout. Instead: `SELECT unit_id … WHERE state='leased' AND leased_until < now()-interval '30 seconds' FOR UPDATE SKIP LOCKED`, then per-row: `lease_token+1`, `events_epoch+1` (sessions), `state = CASE WHEN attempts_since_completion>=max_attempts THEN 'parked' ELSE 'queued' END`, backoff-with-jitter on `run_after`, **plus a system-frame journal write** (`turn.interrupted` / `turn.parked` — §5.3.2's system-writer class) so the user never gets silent dead air. Leader election has a documented dual-leader window — the per-row CAS shape is what makes the reaper safe under it, not the election.

**Attempt semantics** — reset **only on full completion**, never on partial persist. v2's "any successful persist resets attempt" built an infinite hot crash-loop for a unit that streams three tokens and then dies at the same tool call forever, burning LLM spend each cycle. Parking keys on `attempts_since_completion`, plus a per-unit wall-clock/cost budget as the second bound. Parked is not a silent state: it emits the terminal journal frame and appears on an operator surface (§7 replacement inventory) with an unpark verb.

**Fencing at persist time.** Every persist *transaction* opens with `SELECT 1 FROM run_queue WHERE unit_id=$1 AND lease_token=$2 AND state='leased' FOR SHARE` — zero rows → abort; `FOR SHARE` blocks a concurrent steal until the persist commits. Two implementation truths v2 glossed:
1. **The checkpoint lane needs a thin saver subclass**: §5.7's pool is `autocommit=True`, so the stock `AsyncPostgresSaver.aput` has *no transaction to fence* — wrap fence+put in an explicit `conn.transaction()` in a subclass (this corrects §8's "framework: zero work" to "one saver subclass", and the subclass is also where the retry wrapper lives).
2. **A turn's durable footprint spans multiple fenced transactions** (checkpoint put; message inserts; completion). Each is individually fenced, but a steal *between* them leaves a torn turn. The accepted invariant, per lane: **workers rebuild from the checkpoint alone; sessions rebuild from `thread_messages` + the consumed watermark alone** — a checkpoint-ahead-of-messages tear is converged by the next claim's rebuild, and the skip-if-answered watermark (§5.1) prevents the double-answer. This invariant must be stated in code comments and tested (§9).

**Fencing boundary — two honest limits.** (a) The token protects Postgres only; a zombie's in-flight SSH/tmux side effect is out of DB reach (residual window ≈ grace + one tool call) — that's what v2 idempotency (tool-call UUIDs + workspace-side executed-calls dedup; Restate's `ctx.run` relocated) closes. **For S1's lite tier this is sharper than v2 admitted**: virtual-backend writes are read-modify-write against the object store with *no atomicity* and no workspace pod to host dedup — a zombie's in-flight edit during the grace window can interleave with the successor's writes on the same prefix. S1 must either fence object PUTs (re-check the lease token immediately before each PUT, or a per-prefix generation stamp in object metadata) or document and accept the bounded corruption window — and test the steal-with-writes case. (b) **Fencing is a correctness boundary among cooperating honest pods, not a security boundary** — §5.6.

### 5.3 Sessions

#### 5.3.1 Turn flow

`POST /threads/{id}/input` persists the message (already shipped), updates `input_seq`, and enqueues — all one transaction. The claim returns the input/consumed watermarks; the executor answers everything `> consumed_seq`. Completion follows §5.1. `/input` response parity is kept: `queue_depth` is recomputed from unconsumed rows (`input_seq`-vs-`consumed_seq`), no longer from the in-process asyncio queue.

#### 5.3.2 Epoch, seq, and the system-writer class

The v2 finding stands and got sharper: `events_epoch` bumps **unconditionally on every runtime attach** today, and per-turn attach would fire the client cascade — ~2.0–2.2 s dead-epoch polling, then `gone_beyond_horizon` → **IndexedDB thread-cache wipe → full transcript refetch** → SSE reopen — *every turn*. Required, before any stateless session serves traffic (and **before P5**, whose idle-close otherwise converts the delay into an at-open full reload per turn — record the dependency in the session-reliability doc):

1. **Epoch bumps only on reaper steal and rewind** (§5.2). Clean cross-pod handoffs don't bump *and stay client-invisible* because:
2. **Seq allocation moves DB-side** — monotonic *across* claims within an epoch. The in-process `_next_seq` global (reset per attach) is what forces epoch-per-attach today. **Implemented (S1) as DB-*seeded*, not per-event-allocated**: attach seeds from `GREATEST(threads.events_seq_hwm, MAX(seq))` — `events_seq_hwm` (migration 0116) is maintained in the writer's flush statement and survives retention pruning of the rows themselves (a bare MAX(seq) seed would drop below cached client cursors after the pruner runs); gap-free, zero per-event cost, sync stamping preserved. Block allocation on the threads row (the alternative above) was not needed at S1 concurrency.
3. **The journal writer is fenced**: the batch INSERT carries `WHERE threads.events_epoch = $claimed AND run_queue.lease_token = $token` (CTE); zero rows = lost lease, terminal-fail loudly. Today a stale writer inserts into a dead epoch forever.
4. **A system-writer class exists for non-stream kinds.** The fence rule cannot mean "only the lease holder may ever write the journal": the reaper (`turn.interrupted`, `turn.parked`), outbox workers (`title.updated` — a journal frame today), and P6's orchestrator-journaled control acks all write client-visible frames without holding a turn lease. Designated non-stream kinds may be appended by the orchestrator/outbox under the *current* epoch (epoch-read-and-stamp in the same transaction); token/stream kinds stay lease-fenced.
5. **Rewind = a fenced lease steal**, specified: one advisory-locked transaction bumps `events_epoch`, resets the run_queue lease (`state='queued'`, token+1), and initializes the seq allocator *above* the `rewind.done` row it writes — the shipped rewind assumes a detached thread and writes `(new_epoch, seq=1)`; without allocator init, the next writer's `seq=1` collides with the UNIQUE index and presents as a phantom lost-lease. A mid-turn rewind must not wait ~90 s for the reaper.

#### 5.3.3 Per-turn load — decompose attach, don't speed it up

Attach-as-built is seconds-class on both edges (MCP `connect_all`, repo clone probes, blocking recursive cloud `pull_all`, workspace-ready polling, officer boot-wake; detach: aux-LLM memory capture, cloud push+pull, git push). Classification:

| Class | Items | When |
|---|---|---|
| Per-turn core (~0.3–1 s cold S2; ~50–200 ms lite; **+ provider TTFT on top — it dominates**) | lease claim (1 rt) · config blob hydrate (ms; blob rides the claim like `JobStartRequest`) · LLM/tools/prompt build (~20–100 ms) · message tail (2 rt, ≤1000 rows; token-recount can be 100s of ms on big tails) · SSH `_ensure_connected` (~0.1–0.5 s cold) · journal writer start | every claim |
| Claim-scoped, affinity-cacheable | MCP connections · datasource clients · resolved config keyed `(thread_id, config_version)` · built graph/tools · SSH transport + tmux | first claim per pod |
| Edge-triggered | repo clone (needs an idempotent exists-probe) · cloud pull (turn-boundary contract §5.3.5) · workspace-ready (carried in the claim) | workspace lifecycle |
| Re-homed (§5.3.5) | memory capture · citation verification · cloud push · **llm_requests audit archive** · NATS mirror · title gen | outbox / at release |
| Deleted | watchdogs · drain choreography · heartbeat · status polling · **officer boot-wake entirely** (its reason — a lazily-started in-process loop that would otherwise park forever — doesn't exist when sleep = no-lease + durable timer; keying it on generation change, as v2 said, would still over-wake officers on every cross-pod re-claim) | — |

**Hidden repeat-cost fixed before S1** (verified): Path-A restore runs `ensure_within_limits(trigger="resume")` — which can invoke a blocking aux-LLM summarization — and **never persists the resulting summary** (only Path B writes a checkpoint row). Under per-turn reload an over-budget tail would re-summarize on *every claim*, seconds + aux tokens each. Fix: persist the resume compaction as a `role='summary'` boundary row exactly as Path B does.

**S1 measured decomposition (2026-08-08, `session_base` lite thread on k3d).** The table above was written from code reading; instrumenting the claim path put real numbers on it and the answer was not where the design expected. Server-side config resolution — the thing the table calls out as the expensive re-resolve — costs **70 ms** per claim (1 ms lease check + 10 ms credential injection + 60 ms assembly), so the persisted-resolved-config cache this section implies was measured and **not built**. The cost was somewhere else entirely:

| Where | Baseline | After | What it was |
|---|---|---|---|
| Cloud pull at attach | 41 s | 0 s | A full remote-tree walk on the claim's critical path, duplicating the turn-start pull that runs seconds later. Skipped in stateless mode; broken-mount surfacing moves to the turn's `_resilient_cloud_sync`, which already broadcasts `workspace_sync.error`. |
| Cloud pull/push tree walk | 39.9 s list / 7.8 s walk | 0.55 s / 0.20 s | `webdav3`'s per-directory `list()` runs a probe PROPFIND before the real one, ~2.5 s per directory. One `Depth: infinity` PROPFIND returns the whole tree in ~1 s. Capability-probed with a permanent per-instance fallback to the walk on any non-207. |
| Cloud pull at teardown | 41 s | 0 s | Same walk again on the claim-switch path, refreshing a workspace whose next consumer always pulls first. Pinned lane keeps it (its workspace may be browsed after detach). |
| Session setup store ops | 51 spawns / 7.2 s | 13 / 1.5 s | Every virtual-tier metadata probe is an rclone **process spawn**, and setup asks "does this exist?" dozens of times about one small tree. 36 of the 51 were listings. A scoped index (§ below) answers them from one listing; `mkdir` skips re-writing markers that already exist. |

**The scoped metadata index.** `VirtualWorkspaceBackend.begin_read_cache()` / `end_read_cache()` build a key→size map of the workspace prefix in one store op and serve `is_file`/`is_dir`/`list_dir`/`walk`/`stat` from it. It is deliberately **not ambient**: session setup opens it and closes it in a `finally`, so ordinary tool work always talks to the store directly and no caller has to reason about staleness. Inside the scope it stays exact — local mutations update it in place, since the backend knows precisely which keys moved. The one documented boundary is another process writing the same prefix mid-attach (a cockpit upload); that write is picked up by the first op after the scope closes, which is before any tool runs.

#### 5.3.4 Soft affinity

`last_pod` hint per thread; route there first, any pod on miss. Buys: SSH transport + tmux continuity, resident tail, built objects, hydrated config — ≈0.3–1 s/turn. Correctness never depends on it; provider caches don't need it (§5.6). Gate resumes (§5.3.6) should prefer it *strongly*.

**S1 implementation reality (2026-08-08):** affinity matters far more than the estimate above — the first build measured a full re-attach at ~100 s, not 0.3–1 s, and the v1 warm-session cache **never hit**. Two independent causes, both now fixed and re-measured the same day:

1. **The fingerprint was never equal.** It hashed the whole claim bundle, including `resolved_config.resolved_at` — a wall-clock stamp minted by every `serialize_resolved_config` call and read by nothing. Diagnosis came from logging the *changed key paths* on a miss (never values — bundles carry credentials), which printed exactly `['resolved_config.resolved_at']`. Fix: drop resolution metadata before hashing. Config *content* changes still force the re-attach they should.
2. **The warm pod lost the claim race.** Both pods poll on the same ~0.5 s cadence, so the cold one won about half the time and paid a full attach. Fix: `run_queue.last_leased_by` (migration 0117) plus an affinity grace in the general claim — for `AFFINITY_GRACE_SECONDS` (2 s) a freshly queued unit is claimable only by its last holder. The executor also refuses to enter poll backoff while it holds a warm session, so the warm pod is always in the fast cadence to use its head start.

The grace is a scheduling preference with no bearing on fencing: after it lapses any pod claims normally, so a dead holder costs exactly one window, once. **Release and reaper steal both clear `last_leased_by`** — a pod that said it cannot serve (bundle refused, attach failed, loop died, shutting down) or that missed its heartbeats must not make everyone else wait out a grace before failing over.

A warm session is dropped after `WARM_SESSION_IDLE_TTL_SECONDS` (300 s) of no claims, so an idle pod doesn't hold LLM clients, a workspace backend and knowledge stores for a thread nobody is talking to.

**Drain-in-lease was designed and then deliberately NOT built.** Once affinity fired, a 3-message burst already drained on one pod with every turn in `mode=reuse` (attach 0.00 s) and FIFO preserved, because the executor re-claims *immediately* after completing — there is no poll sleep while work is pending. The only cost drain-in-lease would remove is the claim-bundle fetch, measured at 0.05–0.09 s of a ~2.9 s turn. Not worth a second completion path through the exactly-once core. The reasoning is preserved because S3's worker batches face the same question with a very different ratio.

The "release the lease when idle, keep only the warm cache" rule stands as written: the lease is never held while a user types.

#### 5.3.5 Background work, outbox, and the two resident daemons

- **Drain hooks exist**: `MemoryManager.drain_background(timeout)`, `CitationEngine.await_pending_verifications(timeout)` — run before the pod rejoins the pool; post-release work may touch only unfenced stores (§5.2).
- **Transactional outbox**: follow-up work enqueued in the same transaction as the turn's final persist (turn committed ⟺ follow-up enqueued). Collapsible dedup is queued-only (§5.1).
- **Cloud-push ordering fence is mandatory** (not "either/or"): a per-mount sync-generation counter the next claim's pull must observe; with queued-only dedup, one running + one pending push serialize through it. Without it S2 reintroduces the concurrent-walk corruption class.
- **Two daemons cannot be queued work** — they keep *workspace-side* mounts alive: the rclone bearer-token refresh loop (pushes fresh Keycloak tokens over SSH on a TTL schedule) and the protected-overlay ENOTCONN heal loop. They need a resident home: workspace-side refresher, orchestrator cron, or refresh-on-claim + accepted idle rot. S2's hardest inventory item.

#### 5.3.6 Session semantics that must survive the model change

- **Mid-turn settings**: today `mode.set` applies to the *next tool call inside the running turn* (the gate re-reads it per call) and sweeps pending approval cards. "Write override, next turn resolves fresh" (v2) silently regressed the exact moment users reach for the control. Fix: persist mode/narration to the thread row (they're memory-only today — a live revert-on-resume bug) *and* the executor re-reads them at each gate decision (one indexed read per tool call, or piggybacked on the 20 s renewal). Any residual latency (≤ renewal interval) is named and accepted.
- **Permission gates**: rows already DB+NOTIFY. Decision (was OQ): **release the lease at the gate** — N pending approvals must not pin N pods. The spec that makes it acceptable: approval enqueues the continuation; gate resumes prefer `last_pod` strongly (avoid the cascade — though with §5.3.2 a clean cross-pod resume no longer bumps the epoch anyway); the approval card's row id stays stable across release/resume (no duplicate cards; approved card shows a resuming state); p95 approval-to-visible-resume < 3 s is an S2 acceptance criterion. Lease expiry runs the permission-row retire path (no sweeper exists today — pod death mid-gate strands live cards).
- **Steal UX contract** (was undefined): the reaper writes `turn.interrupted` so the client renders a state instead of ≤ ~105 s of dead air indistinguishable from a long tool call; the partially-streamed answer **visibly vanishes and regenerates** (dead-epoch deltas are never replayed) — named as accepted behavior; and because sessions gain an *automatic re-run* lane they never had (today a crashed turn just dies and the user resends), S1 constrains retry for side-effectful tools: re-run at most up to the first tool call unless the call is covered by dedup (MCP writes like `create_job` are the sharp case).
- **Media fidelity**: `_serialize_message_row` flattens list content (images dropped) and restore excludes `thinking`. Per-turn reload makes this the steady state, and **byte-stable re-rendering (§5.6) is impossible for affected turns until fixed**. Either structured-content storage lands with S1, or the degraded option ships *with a visible UI notice* on media past their turn — silent degradation is not an option.
- **Presence**: `_subscribers` is a load-bearing oracle (permission expiry, `awaiting_user` flip) — re-home to an orchestrator-side attached-clients signal.
- **Hard interrupt**: route to `leased_by` or NOTIFY into the executor; graceful interrupt = polled DB flag.
- **Metrics**: the heartbeat's aux-health/RSS payload moves to the lease heartbeat / release report, or admin badges go dark.
- Small state promotions (all verified memory-only today): SessionTaskManager todos → DB/workspace; memory-extraction interval cursor → persisted (else extraction fires *every* turn); file-undo RAM snapshots → the existing git turn→sha ledger; read-before-write stamps → thread metadata or per-turn re-arm (OQ §10.4).

#### 5.3.7 Capacity UX

A pooled session can wait. The design owes users and operators: a queued-turn client state ("queued, position N" — cheap: accept-time 200 + the turn-request row + a journal frame), per-user fairness (`fair_key`, §5.1), `bg_task` never delaying interactive claims, and a loaded-bench acceptance criterion: **p95 claim-to-first-token under 2× pod-count concurrent turns** (§9/S1). HPA floor per the Erlang sizing in §2.

### 5.4 Workers

#### 5.4.1 The batch edge is an in-graph freeze (Lane A)

Two resume lanes exist with different fidelity — an END-lane (in-graph freeze → `restore_todo_state` rehydrates todos) and a mid-loop lane (app-layer astream break → TodoManager **empty** on the successor, `check_todos` force-ends tactical phases — today's pause bug). The batch driver is a **`batch_boundary` freeze cloned from the `version_upgrade` drain check** at `handle_transition`, keyed on wall-clock/superstep budget in checkpointed state. Mid-phase caps must fire only when transient carriers are empty (`_replan_request` is consumed one superstep after it's set). Companion fix: hydrate TodoManager from checkpoint on *any* resume.

Wiring: four keep-in-sync freeze-type lists — and v3 upgrades this from "bounded edit" to **consolidation requirement**: one shared constants module under `src/shared/` (the `orch_surface` move is the precedent) consumed by agent + `completion.py`, parameterizing the `recover_orphaned_jobs` SQL literals. Reason: the lists span two images that roll separately, and the skew is not benign — **`determine_job_status` maps an unknown freeze type on a *loop* job to `('completed', None)`** ("so the loop advances"; verified) — an agent-first rollout or an orchestrator rollback mid-soak turns every batch boundary on a loop job into a phantom-COMPLETED job whose partial work the RSI loop merges. Hard constraints: **orchestrator (all replicas) understands `batch_boundary` before any agent emits it; disabling batch mode precedes any orchestrator rollback during the soak.** Non-loop jobs skew-fail soft (mass `pending_review`).

#### 5.4.2 Cheap teardown

Skip at batch boundary (verified skippable): `job_frozen.json`, git commit/push (ProgressCommitter owns its clock), todo archive, evidence push. **Conditional: the tmux kill** — `ShellManager.cleanup()` kills the workspace tmux on every job end including pause, so "shell survives in tmux" is false for workers today; deterministic session names make skip-the-kill = free cross-batch continuity. Keep: bounded memory drain, checkpointer/datasource close. Any surviving app-layer break must `await gen.aclose()` (checkpoint flush rides generator close).

#### 5.4.3 Steering, status sub-state, cost

- **Steering consumption must be checkpoint-coupled.** The DB-direct pull exists, but its dedup is the in-process `_delivered_reply_keys` set with a fire-and-forget ack — batch rotation re-opens both windows (re-delivery on the successor; loss when an ack outruns an un-checkpointed injection under `durability="async"`) at *every* boundary instead of once per crash. Fix: ack rows carry the checkpoint id/superstep watermark that absorbed them; consumed only when that checkpoint is durable; dedup set rebuilt from checkpointed state each claim.
- **`paused` gets a machine sub-state.** Rotation flips status every few minutes; `paused` is a human-actionable state today (resume-with-feedback, config edit gate on it) and the cockpit would flap. Add `paused_reason='batch_rotation'` (surfaced as "running" in user-facing views); human paused-actions CAS on the reason so a user pause and a rotation release are distinguishable.
- **Cost honesty**: per-claim setup is ~0.5–1.5 s *without* MCP; MCP `connect_all` is seconds-class, and the release→enqueue→claim→`/job/resume` handoff (credential re-injection ≈8–12 rt) adds ~1–5 s of dead time — at the fast end (25 supersteps ≈ 8–10 LLM calls ≈ 45–120 s wall) overhead recomputes to **3–15%, not <2%**. Therefore: **the batch budget is wall-clock-first** (e.g. ≥5 min/batch ⇒ overhead <2% by construction; also the lease-exposure and credential-revocation bound), superstep cap secondary; the Job Bench measures per-claim setup split by MCP-attached vs not, and the handoff, before N is fixed. Batch cadence vs Anthropic 5-min cache TTL: immediate re-queue keeps it warm; queue waits >5 min re-pay full input (consider `ttl:"1h"` at 2× write for long-wait experts).
- Fairness rotation key + cooldown waiver for batch continuations (§5.1); workspace paused-reap grace (~30 min) must exceed queue wait or `batch_boundary` gets a reap carve-out; retire the pod-local snapshot crash lane (PG checkpoint is strictly better; D3 only works because sweeps flip to `paused` first).

#### 5.4.4 Coexistence ruling — one claim authority per job

v2 stood up `run_queue` next to the shipped jobs-row lease and never said which wins; unpartitioned, the legacy sweeps *will* double-execute (verified chain: stateless pod holds a healthy `run_queue` lease → `recover_expired_lease_jobs` sees the un-renewed `jobs.lease_expires_at` expire → flips the job `paused`+unassigned → the leader dispatcher hands it to a pinned agent → two executors on one `thread_id`). Ruling:

1. **Partition on a job-class column** — but NOT `jobs.runner_kind`: implementation found it exists with *grant* semantics (`user|lifecycle|service`, owner-capability classes) and overloading it would tangle dispatch grants with runtime class. S1 shipped `threads.execution_lane` (`pinned|stateless`, migration 0115) as the session-side partition; S3 adds the jobs-side analog (e.g. `jobs.execution_lane`). `get_dispatchable_jobs`, `claim_job_for_agent`, `recover_expired_lease_jobs`, and `recover_orphaned_jobs` all exclude the stateless class; the run_queue reaper is its sole rescue authority.
2. **The stateless claim transaction CASes the `jobs` row** (status/assignment marker) atomically with the `run_queue` claim, so job-status readers stay coherent.
3. **The stage-4 completion CAS keys on `run_queue.lease_token`**, not `assigned_agent_id` — closing the fenced-out-`/complete`-still-wins hole for the stateless class.
4. The registered-agent plane keeps serving compose/bare-metal, officers, and the rollback path until each lane retires (§7 delete-when).

#### 5.4.5 Completion is a command, not a call (Gate 3)

**Status: design, 2026-08-08. Blocks the entire worker lane** — no `worker_batch`
unit can be claimed safely until this lands. Evidence base:
`docs/research/stateless_agents/completion_path_side_effect_inventory.md`.

##### What we thought this was, and what it is

§5.4.4 point 3 above says "the stage-4 completion CAS keys on
`run_queue.lease_token`, not `assigned_agent_id`", which reads like a predicate
change. **There is no completion CAS.** `assigned_agent_id` is not a guard
anywhere in the path. `POST /api/jobs/{job_id}/complete` is **1046 lines with 29
database awaits**, and it is the agent's single report-out for *every* stop, not
just goal-achieved — so on the stateless lane **every batch rotation runs it**.

Three structural facts make a guard insufficient:

1. **There is not one explicit transaction in the handler.** Every write
   autocommits; a simple job commits 8–12 times, a loop job with cloud delivery
   20–30, interleaved with unbounded WebDAV writes. There is no point at which
   the job is atomically "completed".
2. **The status write (`main.py:18060`) is a one-way door.** It puts the job in a
   state the early late-callback guard treats as already handled, so a crash
   anywhere after it permanently orphans `completed_at`, the graft, the critic
   verdict, the verification spawn, the loop advance, the terminal merge, the
   change record **and the workspace archive**.
3. **Every "already done?" check is check-then-act across seconds-to-minutes of
   external I/O** — critic spawn, subjob graft, cloud apply, terminal merge.

A fence at the entry protects only the entry: pod A can enter with a valid
lease, lose it to a reaper steal mid-way, and still run every effect. A final CAS
rejects A's last write and leaves everything before it applied.

##### This is not only a stateless prerequisite

Several of these are reachable **today on the pinned lane**; per-turn claiming
only raises their frequency. A crash between the status write and the archive
leaks the pod, PVC and VM with nothing recording it, and the replay guard
prevents recovery. A `completed` job can carry `completed_at IS NULL` forever.
The duplicate-critic race can drop a blocking finding and approve work that
should have been returned. Treat Gate 3 as repairing a fragile path, not as
paying a stateless tax.

##### The protocol

**Accept.** The agent's report becomes a durable *command row* written in one
transaction under the lease fence, keyed `(job_id, lease_token, stop_identity)`,
with `ON CONFLICT DO NOTHING`. Accept returns 202 immediately. A stolen
predecessor and its successor carry different lease tokens, so both reports are
recorded and the **finalizer**, not the HTTP handler, decides which is
authoritative — the one whose token matches the row's current lease. The loser is
retained as the audit trail of a steal. This also removes the 1–5 s handoff dead
time §5.4.3 charges against the batch budget, since the agent no longer waits out
the pipeline.

**Finalize.** A leader-elected finalizer drains commands and executes the
effects, mirroring `run_queue_reaper`'s shape: the advisory lock avoids duplicate
*work*, correctness comes from a per-command CAS, so the dual-leader window is
safe by construction. Each effect advances a progress marker **in the same
transaction as the effect it records**, so a crash resumes at the next
unexecuted effect rather than replaying from the top. Because
`report_completion` does not retry (it logs and returns `False`), the finalizer
is also the first thing that makes a crashed completion recoverable at all.

**Order.** The job's user-visible status flips **last**, not in the middle. Until
then the command row is the source of truth for "this job is finishing".

##### Effect taxonomy — what each class needs

The inventory classifies all ~37 effects; they reduce to four treatments.

| Class | Examples | Treatment |
|---|---|---|
| Already idempotent | change record (PK + `ON CONFLICT`), session wake (partial unique dedup index), `pause_job`/`claim_delegation_resume`/loop barrier (real CAS) | leave alone — **these are the house patterns to extend, not replace** |
| Blind writes, value-idempotent | status, `completed_at`, freeze stash, merge status | add the missing predicate; `completed_at` needs `COALESCE` like `failed_at` already has |
| Counters | `infra_transient.attempts`, `recovery_attempts`, gate `bounces`, `auto_continue_drains`, memory/LLM-outage attempts, KB TTL | key on the **completion attempt**, not the invocation. Today every replay silently consumes retry budget and enough replays turn a recoverable job into a terminal failure |
| Non-idempotent external effects | critic spawn, subjob graft, cloud apply, terminal merge, VM/pod/PVC delete, S3 snapshot, notifications | **write-ahead intent**: record `(job_id, effect_kind, attempt)` *before* doing it, so a crash between intent and effect is detectable and the effect is claimable exactly once. For spawns, prefer a natural unique key — a unique index on `(parent_job_id, verification_round)` would close the duplicate-critic hole outright |

##### Coexistence with the pinned lane

Not a flag day. The accept step writes the command for both lanes; for pinned
jobs the finalizer runs **inline and synchronously**, preserving today's
behavior, while stateless jobs let the background finalizer drain it. One
implementation, two execution timings, and the pinned lane gets crash recovery
as a side effect.

##### Acceptance

- Kill the orchestrator at each of the named windows (after the status write;
  between cloud apply and its stamp; between graft commit and marker; between
  merge and stamp; between barrier drain and write-back). The job reaches the
  same terminal state on restart, with **no duplicated subjob, graft, merge,
  cloud apply, or dispatch, and no leaked workspace**.
- Steal a lease mid-handler: exactly one report is authoritative, the loser is
  recorded and not executed.
- Replay one command row N times: every effect happens exactly once and **no
  retry counter advances**.
- A pinned job's completion is behaviorally identical to today's.

### 5.5 Streaming — terminus closed; budgets honest

**Journal-only is the terminus** (shipped path + industry contract; statelessness deletes the legacy direct-WS's reason to exist). Three v3 corrections:

- **Write regime, honestly**: the ordered writer drains-what's-queued, and with INSERT RTT (1–5 ms) ≪ inter-token gap (20–50 ms) it degenerates to ~1-row batches — **steady-state commit rate ≈ token rate (~1.5–4 k commits/s at 50–100 active streams)**, not v2's "≤25 INSERTs/s". Fix as an S1 tuning item: a 25–50 ms coalescing tick on the flush, converting it back to real batches for one tick of added latency. The DBOS 60 k/s figure was a dedicated box; ours shares the app PG.
- **An aggregate DB budget table is required** (Appendix): journal commits + SSE polls (100 streams pinned at 200 ms = 500 qps) + fence lookups (~10–25/turn sessions; ~75/batch workers) + queue polls (pods × 1–4/s) + heartbeats (pods × 3/min) — all on the instance serving ~180 REST endpoints. Cheap individually; the *sum* needs a measured capacity number before S1 exits.
- **The cross-replica wake was broken as sketched**: "the orchestrator receives the POST and wakes its own pollers" is an in-process signal, and dev runs 2 replicas — the POST and the SSE stream can land on different replicas, leaving the 1 s idle-backoff in the TTFT path. Use **P5's `thread.activity` DB trigger → `pg_notify`** (already designed, inherently replica-safe and stateless-compatible) to pin pollers to the 200 ms tier on turn start — or accept the 200 ms floor as the only guarantee.

**Workers have no journal** — `thread_events` is sessions-only. S3 decision (§10.2): extend the same epoch/seq/cursor contract to job runs, or accept poll-based job UX. Escape hatch unchanged: stay Postgres-only until trigger metrics fire (~10 k rows/s sustained, or poll fan-out load), then NATS JetStream (in-stack) over Redis.

### 5.6 Config, credentials, security — the threat model v2 lacked

**Any-pod-any-thread is a tenancy inversion and must be treated as one.**

- **The pod cannot re-run the resolution pipeline** (v2's category error): credential resolution is orchestrator-side and Vault/DB-backed; `jobs.resolved_config` is stored *redacted*; the agent process has no DB credential hooks. And the only pod↔orchestrator auth is a single shared internal key — "is-an-agent" proof, never "entitled-to-this-tenant" proof. Under naive pull-claim, one compromised pod serially harvests every tenant's credentials. **Design: pods pull *work*; the orchestrator delivers resolved credentials only against proof of the current lease** — `GET /internal/units/{unit_id}/claim-bundle` authenticated by `(unit_id, lease_token)` server-checked against `run_queue`, or a short-lived per-claim token minted at claim. Credential delivery stays orchestrator-mediated exactly as today; only the direction of the first hop changes.
- **Scrub-on-claim is a v1 requirement, not an S4 nicety.** One-turn-per-pod removes the *concurrency* hazard, not the *sequential residue* hazard: a warm pod serves many tenants in sequence, and the memory-embedding path writes `EMBEDDING_API_KEY` into process-global `os.environ` and **never pops it** (verified; the KB path was deliberately hardened pop-first for pod reuse — the memory path was not): a following tenant whose config omits `env_keys` skips the block and inherits the prior tenant's key + un-reset singleton. Requirement: at every claim, pop all embedding/KB env keys, null both singletons unconditionally, clear guidance inboxes and ToolContext caches. Acceptance: after serving tenant A then claiming for tenant B, no A-secret is reachable in env, singletons, or clients.
- **Fencing ≠ isolation.** The fence is a voluntary check honest pods run; a compromised pod skips it, and most stores (jobs.context, thread metadata, vector, audit) are unfenced regardless — reads are never gated at all. Real isolation remains the DB role + NetworkPolicy story, which statelessness *widens* routinely (every idle pod scans `run_queue` across all tenants; `dedup_key` enumerates thread ids). Give stateless pods a least-privilege DB role for claim/persist paths, and keep the compromised-pod blast radius in the multi-tenancy doc's terms.
- **Revocation**: effective at the next claim (the claim-bundle re-runs grant PEP + datasource re-authorization — an improvement over job-lifetime validity), **not mid-batch** — materialized secrets live until release; the wall-clock batch cap is also the revocation bound. Say both halves.
- **Session JWTs** are deleted only when P6 retires the direct WS (they're the one place a client authenticates to a *specific pod* — the end state is clients authenticate only to the orchestrator; state it, gate it).
- **Prompt caching** (unchanged from v2, scoped honestly): pod identity affects no provider; set `prompt_cache_key = thread_id/job_id` on the OpenAI path; multi-replica self-hosted endpoints need gateway-side cache-aware routing (orthogonal). **Byte-determinism is a worker-lane + fast-follow-up requirement** — for sessions the marginal loss is bounded by the 5-min TTL against human inter-turn gaps and by compaction rewrites (which zero the cache today too); the system prompt's date is already day-granularity *for exactly this reason*. Message fidelity (§5.3.6) is a hard prerequisite of determinism (lossy restore can never re-render byte-identically). Acceptance re-specified: cache_read > 0 on the second LLM call *within* a turn/batch, and on a <5-min same-thread follow-up served by a *different* pod.

### 5.7 LangGraph mechanics (worker lane)

Batch break: in-graph freeze primary; astream yield counting + explicit `aclose()` for any consumer-side break; default `durability="async"` (crash window ≈ 1 superstep); `pending_writes` bounds sibling re-runs; `recursion_limit` stays a backstop; `interrupt_before` considered and rejected (the freeze contract already carries status authority/redispatch/error-immunity). Connection layer: one process-wide psycopg `AsyncConnectionPool` (`autocommit=True, prepare_threshold=0`) + the **fence-and-retry saver subclass** (§5.2 — this is where both wrap); never `pipeline=True`. Dependency hygiene: pin `langgraph-checkpoint-postgres` 3.x exactly (repo pins `>=1.0.0`; **CVE-2025-64439** RCE in pre-3.0 deserialization) + `LANGGRAPH_STRICT_MSGPACK=true`; treat checkpoint-library bumps like DB migrations (documented cross-version resume breakage; long-paused jobs are our risk shape). Growth: delete-on-terminal + keep-last-3 exist; `ShallowPostgresSaver` is the worker-lane blobs option. Keep `thread_id=job_id` canonical; state msgpack-safe and artifact-free. Compile-once via the Runtime context API is S4 (compiled graphs are officially thread-safe; compile-per-job is our code's shape, not the framework's).

### 5.8 Deployment shape and autoscaling — closed, with the chart reality attached

One generic image; two Deployments (interactive: warm floor ≥ 2 sized per §2's Erlang formula; worker: KEDA `postgresql` scaler on runnable-unit count, `minReplicaCount: 0`, `cooldownPeriod` 300 s). What v2 waved away:

- **The chart has no agent Deployment today** — it was deliberately removed ("`agent.replicas` is no longer honored"); the agent Service/PDB select provisioner pods by shared labels. The new Deployments need **distinct class labels** so the kept `reap_pods` categories and PDB counts don't entangle both classes during coexistence.
- **KEDA is a new cluster-level dependency** — named as such: Fleet/homelab install, k3d bootstrap step, `TriggerAuthentication` + DB connection secret via Vault/ESO.
- **Capability dimension on claims**: VM-workspace jobs need the Tailscale-mesh sidecar (the "VM lane is workspace-only" correction stands, but *reaching* a VM workspace is agent-pod mesh infrastructure — and mesh membership per ephemeral scale-to-zero pod churns headscale). Units carry required capabilities (`vm-mesh`); either a third Deployment variant serves them or the worker Deployment ships the sidecar with the tunnel-dark health signal kept for that class.
- **Tilt**: the agent inner loop currently rebuilds an image for the *next provisioned pod*; with static Deployments it becomes a plain `live_update` target — an inner-loop *improvement* worth claiming, but it must be wired.
- Drain = preStop stops claiming, finishes the batch/turn, releases.

## 6. Prerequisites (corrected)

1. **Lite sessions have PVC-backed agent-local state — S1's problem, not S2's.** Verified: the provisioner PVC-backs `/workspace` for *all* sessions, and the manifest comment says outright that for lite sessions the agent-local copy "is the only copy… for lite sessions it is the fix." v2's "S1 scopes to lite (no PVC exists)" was false. S1 work item: inventory what lite sessions write under agent-local `/workspace` (uploads staging, memory/KB artifacts, canvas-adjacent files), externalize each to object store/DB or declare it disposable per item — only then does S1 avoid the PVC.
2. **tmux reattach-if-exists** (backend `_init_shell` kill-recreates; `disconnect()` kills) + tab-state rehydration from `list-windows`, or shell state declared batch-scoped.
3. **Message fidelity** (§5.3.6) — also a prerequisite of the byte-determinism requirement.
4. **Epoch/seq/fence redesign** (§5.3.2) before any stateless session traffic and **before P5** (dependency recorded in the session-reliability doc; if P5 lands first, the epoch change must still precede stateless traffic with `/events/head` kept stable).
5. **Path-A resume-compaction persistence** (§5.3.3) — else per-claim aux-LLM re-summarization.
6. **Freeze-type registry consolidation** into `src/shared/` (§5.4.1) — else version-skew phantom-completes loop jobs.
7. **Control-verb transport for S1 sessions**: `mode.set`, `narration.set`, `compact`, `archive`, `undo`, `rewind`, `config.update`, `upgrade-to-workspace` are **control-WS-only today** (verified) and a stateless thread has no pod to socket to. S1 ships the P6 §6a subset as orchestrator REST + journaled acks — this is S1 scope, not P6's someday.

## 7. Deletion ledger — now with delete-when

Format: component → replaced-by → **delete-when**.

- Registration endpoint + `agents` table + duplicate-bind 409 → nothing / lease rows → **after the last lane retires** (bare-metal decision, §10.9 — table-level artifacts cannot be deleted "per class").
- Heartbeat slot-liveness half → lease renewal → per-class at flag-flip; **keep** the payload channels re-homed (job-status backstop + steering via renewal `RETURNING`; graph-progress stall detector onto the job row; aux-health metrics via release report).
- `get_available_agents` matching + cooldown + SHA filter + provision-and-wait → queue claim → S3 flag-flip (class-partitioned from day one).
- `stale_agent_detector` sweeps (except lease-expiry + graph-progress) → run_queue reaper → per-lane retirement.
- Warm pool / `reap_pods` / `scale_down_idle` / reservation eviction (incl. the warm-pool-vs-scale-down oscillation guard) → Deployment/HPA → per-lane retirement; **reap_pods' label selectors must exclude the new class immediately** (§5.8).
- Drain-intent choreography → preStop + don't-claim → per-lane.
- Per-session Service/Ingress + `pod_uid` ownerRefs + 60 s session JWTs → journal SSE + orchestrator REST → **gated on P6 completion** (verify no other JWT consumer).
- Session pool attach/detach + `_max_sessions_per_process` valve; fresh-vs-resume dispatch lane split; live-settings mutation ladder (with §5.3.6's mid-turn re-read preserved); agent-driven VM/workspace upgrade handlers (→ orchestrator) → S1/S2.
- **New replacement inventory v2 missed** (each needs a successor before its lane cuts over):
  - **Metering**: compute attribution resolves pod→agent-row→job; stateless pods become permanently "shared platform capacity." Successor: **lease-interval attribution** — `run_queue` leases are exact (unit, pod, span) records, strictly better; the interval reconciler needs an ingestion path + cutover flag before paying traffic.
  - **Job-log archive**: sole trigger is orchestrator pod-deletion, attribution via the agents table; Deployment pods are deleted by the ReplicaSet and serve many units. Successor: per-claim log capture at lease release (or preStop upload) stamped from lease history. Acceptance: logs retrievable across N batches / M pods.
  - **Admin/fleet surface**: cockpit agents view + MCP `list_agents`/`get_agent_stats`/`get_agent_system_info`/`deregister_agent`/`assign_job` → a **leases/queue read model** (active leases: unit, pod, age, renewal health, parked units + unpark verb); `assign_job` deprecated or redefined as a priority/affinity hint.
- **Keep untouched**: the dispatcher's workspace pre-filter (VM/sandbox state machines — and the leader dispatcher stays load-bearing as the worker *enqueuer*, §5.1); workspace suspension/attention-sleep minus the delete-agent-pod step; freeze-blob shedding; officer fleet notifications (re-sourced from lease-expiry events).
- **Keep registered-agent plane during migration for**: compose/bare-metal, officer dedicated pods (migrate last; a wake-driven officer is the best stateless fit *later*), rollback.

## 8. Python now — and what the Go rewrite becomes

1. **Framework: one saver subclass, otherwise zero** (fence+retry wrapper — corrected from v2's "zero"). Compiled graphs are officially concurrency-safe; checkpointer-per-invoke is the vendor pattern; the vendor's own platform is this architecture.
2. **The hard parts are language-agnostic** — lease, fence, epoch, outbox, completion protocol — and most substrate is live (§4).
3. **De-globalization sized**: model clients = one shared `httpx.AsyncClient` change; app layer = ~15 module globals → SessionRuntime + ContextVar. v1 ships one-turn-per-pod and takes only the **scrub-on-claim** subset (§5.6 — that part is *not* deferrable); full multiplexing is S4.
4. **Statelessness first defines the contract the Go executor implements** — shrinking the rewrite to "swap the executor behind the same queue." `go_rewrite.md` corrections stand: poll-primary dispatch; recorded lease + persist-time fencing with token-bump-at-steal.

## 9. Phasing

The migration's steady state is **dual control planes** for the whole soak — an operational cost owned here, not a footnote: every dispatch/sweep/provisioning predicate becomes class-aware at S1/S3 flag-flip, and stays so until lane retirements.

- **S0 — this doc.** Align on design + acceptance.
- **S1 — lite/virtual sessions.** Build: run_queue + claim/heartbeat/reaper/completion protocol + fence (incl. object-store PUT fencing or the documented corruption window); epoch/seq/system-writer redesign; turn-request rows + watermarks; **cockpit workstream** (`/connection`+`/prepare` compat answering ready immediately for stateless threads, composer ungating, provisioning-card bypass) + **control-verb REST subset** (§6.7); persistence promotions (mode/narration, task manager, interval cursor, Path-A summary persist; media-fidelity decision *with UI notice if degraded*); scrub-on-claim; permission-row retire on lease expiry; queued-turn UX + `fair_key`; lite agent-local-state inventory (§6.1); journal-writer coalescing tick; metering lease-interval ingestion (shadow). Acceptance: create-to-*accepted* < 1 s; TTFT p50 < 2 s / p95 < 4 s **including provider TTFT**, and p95 claim-wait bounded at 2× pod-count concurrency; cache_read > 0 on second same-turn call and on a <5-min cross-pod follow-up; pod deleted mid-turn → takeover ≤ ~105 s, `turn.interrupted` rendered, no duplicate answer (watermark), no interleaved histories (fence assert); **zombie's late persist rejected at the fence**; poison unit parks within max_attempts with a user-visible terminal frame + operator unpark; queued input drains FIFO across a steal; zero in-process claim state; epoch bumps ≤1 per steal and 0 per clean handoff; after tenant A → tenant B on one pod, no A-residue (env/singletons/clients).
- **S2 — workspace sessions.** SSH affinity + tmux reattach; PVC externalization completes; cloud-push sync-generation fence; outbox re-homing (incl. llm_requests archive); resident daemons re-homed; hard-interrupt routing; presence re-home; gate-release flow. Acceptance: push(N)→pull(N+1) ordering holds across a forced pod handoff; tmux state survives handoff; mid-idle rclone token expiry heals on next claim; p95 approval-to-visible-resume < 3 s; canvas presence has no inter-turn flicker (or the flicker is named and accepted).
- **S3 — workers.** `batch_boundary` + consolidated freeze registry + TodoManager hydration + conditional tmux kill; pull-claim + claim-token renewal + stage-4 CAS keyed on lease_token; **coexistence partition on `runner_kind` everywhere** (§5.4.4); checkpoint-coupled steering acks; `paused_reason` sub-state; wall-clock batch budget; fairness + cooldown waiver; snapshot-lane retirement; job-journal decision executed (§10.2); job-log per-claim capture; deploy-order constraint enforced (orchestrator first; batch-off before rollback). Gate: Job Bench A/B vs pinned baseline — completion parity, tokens/wall within noise, measured overhead (target < 2% at wall-clock-sized batches; measure MCP-attached separately), KV-cache hit rate, **fault injection**: kill -9 mid-batch, steal during a tool call, fenced-out `/complete` rejected, phantom-complete skew test on a loop job.
- **S4 — optional, bill-driven**: in-process multiplexing; compile-once; workspace pause tier (§10.7); JetStream hatch.
- **Rollback**: per-class flag at every stage; pinned plane intact until lane retirement decisions (§10.9).

### 9.1 Implementation status (2026-08-08)

Written against the code on `feature/stateless-agents`, not against intent. The
short version: **the shared substrate is built and is genuinely kind-agnostic;
one of the three drivers exists; most of S1's surround does not.**

#### Is it shared between sessions and workers? The substrate yes, the pools deliberately never.

"Shared" covers three different things in this design, and they landed differently:

| | Design | Built |
|---|---|---|
| Queue + lease contract | ONE `run_queue` table discriminated by `unit_kind` (`session_turn`/`worker_batch`/`bg_task`); one claim/heartbeat/fence/completion/reaper semantics | **BUILT, kind-agnostic.** `src/shared/run_queue/` takes `unit_kind` as a parameter everywhere — claim, fence, completion, release, heartbeat, steal, unpark, read models — and no SQL in it knows what a session is. The reaper steals any expired lease and only *journals* for sessions, with an explicit `(S3)` skip. Only soft coupling: `consumed_seq = input_seq - 1` at row creation assumes a monotonic input counter, which a worker lane without an input stream would simply leave unused |
| Event journal | (implied shared) | **NOT shared — session-only by construction.** `src/shared/event_journal/` hardcodes `threads` and `thread_events` and keys on `thread_id`. Workers have no journal at all; that is the §10.2 job-journal decision, still open. The `src/shared/` path is about who *imports* it (agent + orchestrator), not about unit kinds |
| Pod pools | Deliberately TWO Deployments off ONE image (§5.8): interactive warm floor vs worker KEDA scaler at `minReplicaCount: 0` | **ONE built** — `helm/templates/agent/stateless-deployment.yaml`, session class, chart-gated default off, static `replicas: 2`. No worker Deployment; KEDA remains an uninstalled cluster dependency with no `ScaledObject` anywhere. Note the comparison is not Deployment-vs-Deployment: there is no pinned-pool Deployment either — pinned agents are one-shot Pods built imperatively by `agent_provisioner._build_pod_manifest`, which is why §5.8 says "the chart has no agent Deployment today" |
| Drivers | A session driver and a worker driver over the same queue | **Session only** (`src/api/turn_executor.py`) |

So the intent stands and the foundation is real: adding workers needs no schema
change and no new lease semantics. What is missing is the worker *driver* and
its coexistence partition — S3, never in this branch's scope. Concretely, today
nothing enqueues, claims, or completes a `worker_batch` unit; `jobs` has no
`execution_lane`; and job dispatch is still the legacy `JobStartRequest` POST to
a pod IP.

#### S1 — spine done and proven, surround largely not

Built and k3d-verified: run_queue + claim/heartbeat/reaper/completion + persist
fence (0115/0117, 81 real-PG tests); epoch/seq/system-writer redesign (0116,
which also removes the pinned lane's cache-wipe cascade); turn rows + watermarks
+ skip-if-answered; scrub-on-claim (this fixed a live cross-tenant env leak on
the *pinned* pool); the flag-gated Deployment; soft affinity + warm-session reuse;
and the fault matrix (takeover ≤105 s, no duplicate answer, zombie's late persist
fenced, FIFO drain across a steal, epoch ≤1 bump per steal and 0 per clean
handoff, zero in-process claim state).

**S1 completion pickup (2026-08-08,
`feature/stateless-sessions-s1-completion`): the server-side provisioning and
transport-discovery gate is now BUILT and live-verified.** `/resume` and
`/prepare` whitelist `execution_lane='pinned'`; a detached stateless thread's
`/connection` returns `200` with `state='ready'`,
`control_socket='none'`, and null socket fields. This reports admission
readiness only; it deliberately does **not** claim that a REST control plane has
been built. Unknown future lanes fail closed. Reading beyond the brief's named
entries found that list was not a complete safety boundary: create-thread
provisioning, the resume background refetch, warm-pool attach, slow
dedicated-pod registration, permission-link wake, and officer respawn could all
still bind or spawn a pinned executor. Those paths now whitelist pinned too.

Warm-pool attach reserves both sides before HTTP in one transaction: a
ready/unbound agent CAS plus a pinned/unbound thread CAS. After that reservation,
only HTTP 200 proves acceptance; **every** non-200 response (including 409) and
every transport failure is ambiguous and retains ownership, preferring a
recoverable stranded binding to a second executor. A same-thread 409 is real:
the ended-session sweeper can advertise a still-attached pod as ready during a
resume window.
Every pod fallback re-reads the lane after a failed warm reservation.
Persistent-agent registration checks the lane under the thread advisory lock
before mutation. A same-host restart updates only the exact snapshotted owner;
a genuinely new/replacement binding uses insert-only registration because
hostname is neither unique nor an ownership credential. Missing or different
live owners fail before mutation. The final bind is a checked lane-qualified
CAS against the snapshotted owner; lane rejection never deletes a possibly
pre-existing agent row. Live
probes: stateless connection ready + prepare/resume 409; synthetic persistent
registration 409 with zero surviving agent rows and no thread binding; fresh
pinned session ready in 14.43 s and one exact reply persisted by 17.48 s.

**Correction (same day, caught by a second audit pass): Path-A
resume-compaction persistence (§6.5) is NOT done, and it is the one live
functional bug in the shipped path.** An earlier version of this section
claimed otherwise on the strength of a grep that matched
`ensure_within_limits(trigger="resume")` rather than
`_record_compaction(trigger="resume")` — a lesson in reading the call, not the
keyword. What the code actually does: Path B (full load, no checkpoint)
persists its resume compaction (`persistent_app.py:6552`); **Path A
(checkpoint restore) calls `ensure_within_limits(..., trigger="resume")` at
`:6441` and returns at `:6473` without persisting**, and the comment at `:6543`
says so deliberately — Path A skips the write to avoid a live/history banner
double-render. That reasoning is about the *banner*; the discarded
*summarization work* is the problem. Any thread that has ever compacted takes
Path A, so if its post-boundary tail is over budget it pays a blocking
aux-LLM summarization **on every claim** and throws the result away. On the
pinned lane that was one call per pod restart; per-turn attach turns it into
one per turn. It did not show up in this session's measurements because the
test thread's tail sits under budget. The fix is not a blind copy of Path B's
call — it has to advance the boundary row without reintroducing the double
render.

Not built, all of them named in S1's own list above:

- **Cockpit workstream** — the server `/connection` contract is built, but the
  cockpit does not consume it yet: it will still try `new WebSocket(null)`
  for the no-socket marker and enter the reconnect ladder. The later claim
  that send/receive otherwise needed zero Cockpit changes was disproved:
  `session.state` is WebSocket-direct and is load-bearing on reload for
  in-flight-turn reconciliation, running-tool state, pending permission cards,
  modes and model settings. A lane-agnostic, transport-independent state
  snapshot for **both** lanes must precede the null-socket guard and REST
  control routing. Durable queued state, stateless ended-session wake, and all
  control routing remain unbuilt; the client still must not branch on
  `execution_lane`.
- **Control-verb REST subset** (§6.7) — **the transport decision is made, but
  nothing is scaffolded.** The proposed orchestrator `append_system_frame` ack is unsafe:
  a stateless pod keeps a warm attached journal writer for 300 s after a claim,
  and the system-writer helper explicitly forbids concurrent live-epoch writes
  because its DB seq allocation can collide with that writer's process-local
  `_next_seq`. The chosen replacement is migration 0119 with durable,
  commit-ordered control requests consumed and journaled by the serving owner;
  controls enter orchestrator REST for both lanes. It is gated first on the
  transport-independent `session.state` snapshot above. The transaction audit
  adds two non-optional details: stateless terminal acceptance fences on the
  current lease token while pinned acceptance fences on the exact current
  `threads.agent_id`; and `run_queue` needs control watermarks (or an equivalent
  unified cursor) because completion currently requeues only for newer human
  input and can otherwise strand a control committed during a lease. A terminal
  request CAS also needs proof that its result frame was durably flushed, not
  merely handed to `_broadcast`. Verb assumptions fail independently:
  detached rewind does not steal/fence `run_queue`; compact has no detached REST
  route and boundary IDs do not survive restore; attached rewind rebuilds the
  writer without the stateless lease; undo state is RAM-only; archive lacks an
  idempotent terminal finalizer; config mutation is not transactionally fenced;
  and upgrade enters unbuilt S2 workspace semantics. The safe initial subset is
  idempotent `mode.set` / `narration.set`, after mode is moved from memory-only
  mutation to grant-checked persistence. Interrupt remains a deliberate **501**
  on the lane.
- **Persistence promotions** — mode/narration, session task manager, memory-extraction interval cursor, media-fidelity decision, and Path-A resume-summary persistence (see the correction above).
- **Permission-row retire** on lease expiry — nothing sweeps
  `thread_permission_requests`, and a blanket thread-wide reaper UPDATE is
  unsafe: rows carry no lease identity, so post-steal cleanup can expire a
  successor's new gate. The steal commits before its contained journal step,
  so cleanup also needs a durable reaped-token retry source rather than a
  one-shot side effect. Implementation reality also differs from §5.3.6's
  target state: today's stateless executor keeps heartbeating and holds the
  lease while permission resolution waits; release-at-gate is not built, and
  agent-local `_subscribers` cannot see stateless SSE viewers. **Durable
  queued-turn UX** — the Cockpit already renders
  an accepted-send spinner via RAM-only `pendingTurnCount`, but it disappears on
  reload and other tabs see nothing. An orchestrator-written journal frame is
  forbidden by the live-writer collision; use a DB-authoritative queue snapshot
  plus current-state notification. Exact global “position N” is not truthful
  before `fair_key` rotation exists. **Stateless ended-session/system wake** is
  also not a status flip: current durable wakes persist `role='event'`, while
  the executor selects only `role='human'`; a safe wake needs stable-id atomic
  message+watermark admission and role-preserving restore/injection. **`fair_key`
  round-robin** — the column and its merge exist; no rotation CTE.
- **Lite agent-local-state inventory** (§6.1) — the PVC question S1 is supposed to answer is still open.
- **Journal-writer coalescing tick**; **object-store PUT fencing** (or the documented corruption window).
- **Metering lease-interval attribution** — not merely unbuilt, actively routed around: `stateless-deployment.yaml` labels the class `app: srw-agent-stateless` precisely so `classify_product_pod` won't claim it, which means **stateless pods are currently unattributed compute** ("shared platform capacity"). Any real traffic on this lane is unbilled until the interval reconciler lands.
- **Job-log capture per claim** — the only capture path is `_capture_agent_logs_before_reap`, triggered by *provisioner* pod-reap. A ReplicaSet-deleted stateless pod's logs are unrecoverable.
- **Admin/fleet read model** — PARTIAL. The queue half exists (`GET /api/admin/run-queue` + unpark), but API-only: no cockpit view, no MCP tool, and the registration surface it is meant to replace (`list_agents`, `get_agent_stats`, `deregister_agent`, `assign_job`) is untouched.

Acceptance criteria, honestly scored: met are create-to-accepted <1 s, takeover
≤105 s with no duplicate answer, the fence assert, FIFO-across-steal, the epoch
bump bounds, zero claim state, and the zombie's rejected persist. **Unmeasured or
unverified**: TTFT p50/p95 (we measured whole-turn wall time — 3.0 s warm, 5.4 s
cold — which is a different number and needs streaming instrumentation); p95
claim-wait under concurrency (never load-tested); `cache_read > 0` on a
cross-pod follow-up (`cached_tokens` is captured in metrics/audit, but the
`prompt_cache_key=thread_id` pin OQ5 requires is not set anywhere); the poison
unit's user-visible terminal frame and operator unpark end to end (parking
itself is proven in real-PG); and the tenant A→B residue probe, which needs a
second user identity.

#### S2 / S3 / S4 — untouched

**S2** (workspace sessions): nothing — S1 is lite/virtual only. SSH affinity,
tmux reattach-if-exists, PVC externalization, cloud-push generation fence,
outbox re-homing, the two resident daemons, presence re-home all outstanding.

**S3** (workers): **two of the three safety gates are built** on the parallel
branch `feature/stateless-workers-s3` (4 commits, unpushed, reviewed 2026-08-08;
no `worker_batch` unit has ever been enqueued and no job carries a non-pinned
lane). Migration **0118** adds `jobs.execution_lane` and §5.4.4's coexistence
partition is applied — `get_dispatchable_jobs`, `claim_job_for_agent`,
`recover_expired_lease_jobs`, `recover_orphaned_jobs` (four separate predicates)
and `register_agent` all whitelist `'pinned'` so an unknown future lane fails
closed, with defense-in-depth refusals on the three direct dispatch paths and
real-Postgres tests that fail without the partition. `src/shared/job_freeze_types.py`
consolidates the four keep-in-sync freeze lists, the orphan-recovery SQL now
takes the set as a parameter so it cannot drift, and `batch_boundary` joins
`CONTINUE_AS_NEW_FREEZE_TYPES` (non-terminal `paused`). The §5.4.1
phantom-COMPLETE hazard is **closed**: `determine_job_status` previously mapped
*any* non-completion stop on a loop job to `completed`; it now does so only when
no freeze type was declared, and routes an unknown *declared* freeze to
`pending_review` with an error log. The `batch_boundary` freeze itself exists in
the graph, **unarmed by construction** (it requires state fields nothing sets),
wall-clock-first with a 300 s floor, disarming its whole envelope in the same
checkpoint as the freeze so a resumed job cannot immediately re-freeze.

**Gate 3 is not built and is now known to be a design problem, not a
predicate** — see §5.4.5. Everything downstream (the worker driver, the pool
decision, all worker acceptance) is blocked behind it. Also unresolved: a
pod-local capacity reserve cannot guarantee interactive availability across a
rollout or pod loss, which is evidence *for* §5.8's two-Deployment split and
against a single shared pool; and the current Deployment lacks the VM-mesh
capability §5.8 requires for VM-workspace jobs. The rest of the list:
`batch_boundary` freeze + the consolidated freeze registry
(the phantom-COMPLETE skew hazard in §5.4.1 makes this a *correctness*
prerequisite, not a nicety), TodoManager hydration on any resume, conditional
tmux kill, pull-claim + claim-token renewal + stage-4 CAS keyed on
`lease_token`, `jobs.execution_lane` with all four legacy sweeps excluding the
class (§5.4.4), checkpoint-coupled steering acks, `paused_reason='batch_rotation'`,
the wall-clock batch budget, the worker Deployment + KEDA, job-log per-claim
capture, and the Job Bench A/B gate.

**S4**: not started, by design.

#### One gap that is cheap and not S3

The scoped metadata index (§5.3.3) is wired only into `PersistentSession.setup`.
Worker jobs construct the same `VirtualWorkspaceBackend` (`src/agent.py`) and
never open it, so a lite job still pays the full rclone spawn count during
workspace setup. The primitive is built and contract-tested; closing this needs
only the same open / `finally`-close pair around the job's setup burst, and it
is independent of everything else in S3. The cloud-sync speedups are in the
shared package but have no job-side effect today, because worker jobs do no
turn-boundary cloud sync at all — `persistent_app` is the package's only
consumer.

## 10. Open questions

1. **Interrupt-grade steering** — is `enqueue` enough, or do sessions/officers eventually need the Platform's `interrupt` policy (with partial-tool-call cleanup)?
2. **Job journal** — extend `thread_events`' contract to job runs in S3, or accept poll-based job UX and defer?
3. ~~Permission-gate parking~~ — **decided release-at-gate**, spec in §5.3.6; remaining question is only the approval-to-resume latency target's final number.
4. **Read-before-write gates** — persist stamps vs per-turn re-arm (token tax vs behavior change)?
5. **Media-fidelity scope** — structured-content storage in S1, or degraded-with-UI-notice first?
6. **Interactive saturation policy** — beyond the queued-turn UX: do interactive claims ever preempt `bg_task` leases, and does the worker deployment lend capacity to session bursts?
7. **Workspace pause tier** — run→paused→reaped with ~1 s resume (industry benchmark) vs scale-to-zero + PVC; the capacity limit migrates there (Cognition's lesson).
8. **Canvas presence** — orchestrator-side with the SSE consumers, or a small shared presence service, once P6 retires the per-session WS?
9. **Bare-metal/Compose**: remains supported? If yes — either it runs the stateless executor as plain polling processes (registration still dies) or registration is permanent and the §7 table-deletes never fully land. Decision needed for the ledger's delete-when column.

## Appendix — key numbers (v3-corrected)

| Item | Value | Basis |
|---|---|---|
| resolved_config blob | ~127 kB | measured (OOM postmortem) |
| Session re-resolve | ~8–12 DB rt + tens of ms | main.py trace |
| Message tail load | 2 rt, ≤1000 rows, ~1 kB/row (+100s ms recount CPU on big tails) | code + k3d measured |
| Checkpoint restore / write | ≈600 kB worst / ~11 ms p50, ~3 per superstep | audit doc, D3 probe |
| SSH cold connect | ~0.1–0.5 s | remote.py trace |
| Cold turn core: lite / S2 / worker batch | ~50–200 ms / ~0.3–1 s / ~0.5–1.5 s (excl. MCP connect_all: +seconds; excl. provider TTFT) | composed — **measure before gating** |
| Worker batch overhead | <2% only at wall-clock-sized batches (≥~5 min); 3–15% at 25-superstep fast end | recomputed §5.4.3 |
| Steal takeover | ≤ ~105 s worst (TTL 60 + grace 30 + reaper 15 + claim poll) | §5.2 parameters |
| Journal steady-state | commit rate ≈ token rate (~1.5–4 k/s at 50–100 streams) **until the coalescing tick lands** | writer mechanics, recomputed |
| SSE poll load | 100 pinned streams ≈ 500 qps + idle floor | §5.5 |
| Fence lookups | ~10–25/turn (sessions), ~75/batch (workers) | §5.5 |
| Sizing | pods ≈ offered erlangs / 0.6–0.7 → ~13–15 pods per 50 sessions at 20% duty | Erlang-C, §2 |
| Epoch-cascade cost (steals only, post-§5.3.2) | ~2.0–2.2 s + full refetch + cache wipe | main.py + service.ts trace |
| Lease TTL / heartbeat / reaper | 60 s / 20 s / 15 s per-row SKIP LOCKED | §5.2 |
| Agent pod | request 512Mi / limit 2Gi | helm/values.yaml |
| **S1 live (k3d, 2026-08-08)**: enqueue→claim | < 1 s | M6 run, implementation_log.md |
| S1 live: turn split on a `session_base` lite thread | 58 s bundle+setup / 42 s attach+restore / 52 s turn (Σ ~152 s) — affinity cache never hits (§5.3.4 note) | M6 turn-5 log timestamps |
| S1 live: steal takeover (kill −9 / cgroup freeze) | t+88 s / t+82–98 s (bound ≤ ~105 s holds) | M6 fault matrix |
| S1 live: skip-if-answered completion | < 8 s, no LLM call | M6 |
| S1 live: epoch bumps | +1 per steal, 0 per clean handoff (4 epochs over 3 steals + 1 terminal-resume) | M6 |
| **S1 optimized (k3d, 2026-08-08, same thread)**: cold turn | **5.4 s** total (0.06 bundle / 2.2 attach / 2.8 turn / 0.3 push) — was 99.6 s | turn-timing log line |
| S1 optimized: warm turn (affinity hit) | **3.0 s** total, attach **0.00 s** | turn-timing log line |
| S1 optimized: 3-message burst drain | **11 s**, one pod, all `mode=reuse`, FIFO preserved — was 3 full attaches | burst run |
| S1 optimized: claim-bundle server time | 70 ms (1 ms lease + 10 ms creds + 60 ms assemble) — why no persisted-config cache was built | orchestrator timing log |
| S1 optimized: session setup store ops (virtual tier) | 13 spawns / 1.5 s — was 51 / 7.2 s (36 listings → 4) | rclone op tally |
| S1 optimized: cloud tree listing (Nextcloud) | 0.55 s via one `Depth: infinity` PROPFIND — was 39.9 s via per-directory `webdav3` walk | pull-detail log |
| S1 optimized: steal + exactly-once after pod force-delete | steal t+97 s, 1 answer, epoch +1, `last_leased_by` cleared | fault re-verification |
