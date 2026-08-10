# Stateless Agents — Turn Execution as a Deployment

**Status:** v3.3 — **CONSOLIDATED 2026-08-09.** All three work streams are now on `feature/stateless-agents` (the session spine, its performance work, the S1 session-lane completion, and the S3 worker safety gates). Nothing is pushed; `develop` is untouched. **Where things stand is §9.1 Implementation status**, written against the code rather than intent.

In one paragraph: the shared queue/lease substrate is built, k3d-verified and genuinely kind-agnostic; **the session lane is functionally complete** — turns queue and run on any pod, survive a mid-generation kill, never double-answer, reconnect with no socket, and take durable control verbs, all without the cockpit ever learning a lane exists; **the worker lane is not enabled** — the two coexistence gates are built, Gate 3's step-1 fixes are live, and the 2026-08-10 scope correction (§5.4.5) established that rotation never calls the completion handler, so admission now waits on the worker driver (plus a thin entry fence), not on the completion redesign; Gate 3 continues as the completion-reliability program for both lanes. Turn latency went 99.6 s → 5.4 s cold / 3.0 s warm (§5.3.3, §5.3.4). Build history, measurements and failures: `docs/research/stateless_agents/implementation_log.md`; the completion-path evidence base is `docs/research/stateless_agents/completion_path_side_effect_inventory.md`.

v1 2026-08-06 (initial proposal); v2 2026-08-07 after an 8-agent research fan-out; **v3 same night after a 6-lens adversarial panel** (10 critical + 28 major findings folded in). Raw research and critic reports: `docs/research/stateless_agents/`. Related implementation docs carrying pieces of this work: `no_workspace_agent_mode.md` §5.1 (op count is the cost — the virtual backend's scoped metadata index) and `cloud_collaboration_model.md` §4 (one `Depth: infinity` PROPFIND per turn boundary).
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

Skip at batch boundary (verified skippable): `job_frozen.json`, git commit/push (ProgressCommitter owns its clock), todo archive, evidence push. **Conditional: the tmux kill** — `ShellManager.cleanup()` kills the workspace tmux on every job end including pause, so "shell survives in tmux" is false for workers today. Deterministic names are necessary but not sufficient: `RemoteBackend._init_shell()` also unconditionally kills an existing deterministic session, while tab/sentinel bookkeeping is process-local. Cross-batch continuity therefore needs reattach-if-exists plus tab/cursor reconstruction and a real SSH/tmux handoff test; merely skipping cleanup must not be described as continuity. Keep: bounded memory drain, checkpointer/datasource close. Any surviving app-layer break must `await gen.aclose()` (checkpoint flush rides generator close).

#### 5.4.3 Steering, status sub-state, cost

- **Steering consumption must be checkpoint-coupled.** The DB-direct pull exists, but its dedup is the in-process `_delivered_reply_keys` set with a fire-and-forget ack — batch rotation re-opens both windows (re-delivery on the successor; loss when an ack outruns an un-checkpointed injection under `durability="async"`) at *every* boundary instead of once per crash. Fix: ack rows carry the checkpoint id/superstep watermark that absorbed them; consumed only when that checkpoint is durable; dedup set rebuilt from checkpointed state each claim.
- **~~`paused` gets a machine sub-state~~ — dissolved by the §5.4.5 scope correction (2026-08-10).** Rotation releases through the queue and never touches `jobs.status`, so there is no flap to disguise and no sub-state to add. The residue of this item is real but different: the resume/approve/feedback verbs must be lane-aware (§5.4.5 condition 3), because `queue_job_for_resume` parks jobs as dispatcher-visible and the dispatcher never sees stateless jobs.
- **Cost honesty**: per-claim setup is ~0.5–1.5 s *without* MCP; MCP `connect_all` is seconds-class, and the release→enqueue→claim→`/job/resume` handoff (credential re-injection ≈8–12 rt) adds ~1–5 s of dead time — at the fast end (25 supersteps ≈ 8–10 LLM calls ≈ 45–120 s wall) overhead recomputes to **3–15%, not <2%**. Therefore: **the batch budget is wall-clock-first** (e.g. ≥5 min/batch ⇒ overhead <2% by construction; also the lease-exposure and credential-revocation bound), superstep cap secondary; the Job Bench measures per-claim setup split by MCP-attached vs not, and the handoff, before N is fixed. Batch cadence vs Anthropic 5-min cache TTL: immediate re-queue keeps it warm; queue waits >5 min re-pay full input (consider `ttl:"1h"` at 2× write for long-wait experts).
- Fairness rotation key + cooldown waiver for batch continuations (§5.1); workspace paused-reap grace (~30 min) must exceed queue wait or `batch_boundary` gets a reap carve-out; retire the pod-local snapshot crash lane (PG checkpoint is strictly better; D3 only works because sweeps flip to `paused` first).

#### 5.4.4 Coexistence ruling — one claim authority per job

v2 stood up `run_queue` next to the shipped jobs-row lease and never said which wins; unpartitioned, the legacy sweeps *will* double-execute (verified chain: stateless pod holds a healthy `run_queue` lease → `recover_expired_lease_jobs` sees the un-renewed `jobs.lease_expires_at` expire → flips the job `paused`+unassigned → the leader dispatcher hands it to a pinned agent → two executors on one `thread_id`). Ruling:

1. **Partition on a job-class column** — but NOT `jobs.runner_kind`: implementation found it exists with *grant* semantics (`user|lifecycle|service`, owner-capability classes) and overloading it would tangle dispatch grants with runtime class. S1 shipped `threads.execution_lane` (`pinned|stateless`, migration 0115) as the session-side partition; S3 adds the jobs-side analog (e.g. `jobs.execution_lane`). `get_dispatchable_jobs`, `claim_job_for_agent`, `recover_expired_lease_jobs`, and `recover_orphaned_jobs` all exclude the stateless class; the run_queue reaper is its sole rescue authority.
2. **The stateless claim transaction CASes the `jobs` row** (status/assignment marker) atomically with the `run_queue` claim, so job-status readers stay coherent.
3. **The stage-4 completion CAS keys on `run_queue.lease_token`**, not `assigned_agent_id` — closing the fenced-out-`/complete`-still-wins hole for the stateless class.
4. The registered-agent plane keeps serving compose/bare-metal, officers, and the rollback path until each lane retires (§7 delete-when).

#### 5.4.5 Completion is a command, not a call (Gate 3)

**Status: design 2026-08-08; step 1 shipped 2026-08-10 (`2f5307f0`); scope
corrected 2026-08-10 — no longer admission-blocking (see below).** Evidence base:
`docs/research/stateless_agents/completion_path_side_effect_inventory.md`.

##### Scope correction (2026-08-10): rotation never goes through `/complete`

"Every batch rotation runs it" was the load-bearing sentence behind "blocks the
entire worker lane", and it is wrong — it assumed the batch driver must route a
rotation through `/complete`, and nothing requires that. The session lane is the
existing disproof: `turn_executor.py` calls `/complete` **zero times**; a turn
ends its unit with `complete_unit` against the queue. Traced against the code:

- `/complete`'s pause path exists to make the job selectable again by the
  **jobs-table dispatcher** (status→`paused`, clear `assigned_agent_id`, stash
  the freeze, kick dispatch) and to record where it stopped for humans.
  `determine_job_status` maps every continue-as-new freeze to `paused` — a value
  whose only mechanical consumer is `get_dispatchable_jobs`, from which
  stateless jobs are already excluded (the lane partition is live in eight
  predicates).
- The queue replaces each piece with an S1-proven primitive: re-selection =
  release/requeue with watermarks; state carrier = the LangGraph checkpoint
  (`restore_todo_state`), not the DB freeze stash; liveness = lease + reaper
  (sole rescue authority, §5.4.4); the infra-outage counter arms (S5/S6, S11,
  S12) become the queue's `attempts`/backoff/`parked` — which is replay-safe,
  so the counter-burn bug class does not exist on this lane.
- Agent-side, the only orchestrator interaction at job end is
  `report_completion` itself plus a registered-agent heartbeat the stateless
  lane doesn't have. Steering acks are a separate endpoint. A rotation has
  nothing else to say.

So a `batch_boundary` releases through the queue and never reports; `/complete`
runs **once per job at a genuine stop — the pinned lane's frequency**. The
freeze registry's `batch_boundary` entry stays as defense-in-depth for version
skew (an agent that does report it gets a sane `paused`), not as the mechanism.

**Four conditions, all driver-scope, none protocol-scope:**

1. The driver arms the batch freeze and releases via the queue; it never
   reports a rotation.
2. `/complete` gains a **thin entry fence** for stateless-lane jobs: reject any
   report whose lease token is not the unit's current token (~20 lines). This
   closes the zombie-late-report lane outright — strictly stronger than the
   pinned status guard. The concurrent double-report window that remains is
   pinned parity, and its worst consequence (duplicate critic) is already
   closed by 0132.
3. Human-facing stops (blocking_message, budget review, dependent-autonomy
   pauses, give-up failures) keep `/complete` — humans act on those statuses.
   But the resume/approve/feedback verbs must become **lane-aware**:
   `queue_job_for_resume` parks a job as "`paused` (dispatchable)", and the
   same lane exclusion that protects a stateless job from the dispatcher
   strands it there forever. A resumed stateless job re-enqueues its unit.
4. Mid-batch cancel/preempt discovery rides lease-renewal `RETURNING` (already
   planned as §7's heartbeat re-homing) — a stateless worker never sees the
   heartbeat backstop.

**Consequences.** Worker admission now waits on the **driver** (§5.4.1 freeze
arming + TodoManager hydration, §5.7 fenced saver, §5.4.4 pt-2 claim CAS,
enqueue, lane-aware resume, renewal backstop, §5.8 deployment) — build work
with settled designs, no open questions. Gate 3 proceeds **unchanged in
content** as the completion-reliability program for *both* lanes — the
crash-window bugs it fixes are live on pinned today — but on its own schedule.
§5.4.3's `paused_reason='batch_rotation'` sub-state dissolves: rotation never
touches `jobs.status`, which is strictly better than flap-plus-substate.

##### What we thought this was, and what it is

§5.4.4 point 3 above says "the stage-4 completion CAS keys on
`run_queue.lease_token`, not `assigned_agent_id`", which reads like a predicate
change. **There is no completion CAS.** `assigned_agent_id` is not a guard
anywhere in the path. `POST /api/jobs/{job_id}/complete` is **1046 lines with 29
database awaits**, and it is the agent's single report-out for *every* stop, not
just goal-achieved — so, this section originally concluded, on the stateless
lane every batch rotation would run it. **Corrected above (2026-08-10): only if
the driver routes rotations through it, which nothing requires.**

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

*(This subsection is the shape. The settled design below refines the key —
`stop_identity` decomposes into three separate things — and replaces the
advisory lock and the progress-marker mechanics. Where they differ, the settled
design wins.)*

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
effects, mirroring `run_queue_reaper`'s shape: the election avoids duplicate
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
| External effects | critic spawn, subjob graft, cloud apply, terminal merge, VM/pod/PVC delete, S3 snapshot, notifications | **Superseded by (4) in the settled design below** — these do *not* form one class. Most are naturally idempotent once keyed correctly (k8s UID preconditions, deterministic S3 keys, WebDAV paths); a few genuinely need a reconcile probe; notifications can never be exactly-once and are declared at-least-once. A single universal intent table keyed `(job_id, effect_kind, attempt)` was the first draft's answer and is **not** the design |

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

Added after the external review — each of these is a failure mode the first
draft would have shipped:

- **Wedge the finalizer** (SIGSTOP it mid-effect). The job must become
  rescuable once its lease expires, and must reach a terminal state by
  `deadline_at`. A job that is stuck *and* matched by no sweep is the
  stuck-namespace bug and fails this gate.
- **Kill the leader ungracefully** (no clean shutdown, no lease release).
  Another finalizer must take over within seconds, not TCP-keepalive minutes.
- **Delete a workspace pod, recreate one with the same name, then resume the
  command.** The replacement must survive — this is the ABA case that a
  name-only delete gets wrong.
- **Block one effect group indefinitely** (point cloud sync at an unreachable
  host). Every other group must still complete, and the workspace must still be
  torn down.
- **Deploy a finalizer whose effect list changed while commands are in flight.**
  In-flight rows must either drain first or refuse loudly — never silently skip
  the wrong effects.
- **Run the same report twice concurrently** (not sequentially): one 202, one
  409, exactly one execution.

##### Settled design (2026-08-09, revised same day against external review)

The seven open questions are answered below, against the code as it stands after
the consolidation. An eighth decision the questions missed is recorded as **(6)**
— it is load-bearing, and moving the status write last is unsafe without it.

The first draft of this section was then checked against prior art (durable-
execution engines, the Postgres queue field, per-system idempotency
guarantees) and one local experiment. **Five things in it were wrong**, and are
corrected below rather than left for implementation to discover:

1. The progress marker was a JSONB column. Postgres has no partial JSONB
   update, so 37 sequential effects cost quadratic write volume and TOAST-rewrite
   the whole value for the back half of every completion. It is now its own table.
2. The reconcile probe assumed a crashed predecessor. The common case is a
   *slow* one, still alive — probing it returns "not done" and doubles the effect.
3. The workspace-teardown probe had an ABA hole: a controller can recreate a pod
   under the same name, and the probe would authorize deleting the replacement.
4. The effects were one linear chain, so one wedged effect blocked every later
   one — including the pod/PVC deletes, turning a customer's cloud outage into
   our resource leak.
5. The critic index silently did nothing for rows whose round key is absent,
   because NULLs are distinct in a unique index. Confirmed by experiment.

The liveness predicate in (6) also survived review only in shape: as first
written it reproduced Kubernetes' stuck-finalizer failure mode almost exactly.

**(1) The command row is its own table**, `job_completion_commands`, modelled on
`thread_control_requests` (migration 0119) rather than on `run_queue`. Three
reasons, in order of weight. `run_queue`'s module contract is explicitly "this
module touches ONLY run_queue" (`queries.py:849`) and its rows are *one per unit,
durable for the unit's life* precisely so `lease_token` stays monotonic — a
report is many-per-job, so the cardinality is wrong. And the session control
inbox (0119/0120/0121) already solved this exact problem on this branch: a
durable commit-ordered command, applied once by a fenced owner, with a
terminal-shape CHECK that makes half-written states unrepresentable. Extending a
pattern that is already proven here beats importing a new one.

```sql
CREATE TABLE job_completion_commands (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id               UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    report_seq           BIGINT NOT NULL,        -- commit-ordered, see (2)
    client_report_id     UUID   NOT NULL,        -- agent-supplied idempotency key
    payload              JSONB  NOT NULL,        -- the JobCompleteRequest, verbatim
    payload_digest       TEXT   NOT NULL,        -- sha256 of canonical payload
    reported_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    accepted_lease_token BIGINT,                 -- stateless fence   } exactly
    accepted_agent_id    UUID,                   -- pinned fence      } one

    state                TEXT NOT NULL DEFAULT 'pending',
    attempts             INT  NOT NULL DEFAULT 0,
    max_attempts         INT  NOT NULL DEFAULT 5,
    run_after            TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at     TIMESTAMPTZ,        -- renewable; the liveness signal
    deadline_at          TIMESTAMPTZ NOT NULL,  -- absolute cap, NEVER extended
    finalizing_by        TEXT,               -- diagnostics only
    code_version         TEXT NOT NULL,      -- gates recovery across deploys

    outcome              JSONB,
    finalized_at         TIMESTAMPTZ,
    error_code           TEXT,

    CONSTRAINT uq_job_completion_seq    UNIQUE (job_id, report_seq),
    CONSTRAINT uq_job_completion_client UNIQUE (job_id, client_report_id),
    CONSTRAINT job_completion_fence_exactly_one CHECK (
        (accepted_lease_token IS NOT NULL AND accepted_agent_id IS NULL)
     OR (accepted_lease_token IS NULL AND accepted_agent_id IS NOT NULL)),
    CONSTRAINT job_completion_terminal_shape CHECK (
        state <> 'done' OR (outcome IS NOT NULL AND finalized_at IS NOT NULL))
);

-- Progress marker: ONE ROW PER EFFECT, never a JSONB log on the command.
-- POLYMORPHIC producer (run_queue's precedent) so the session lane shares this
-- substrate rather than growing a second one — see "shared with the session
-- lane" below. Deliberately NO foreign key, for the same reason run_queue has
-- none: the effect log outlives and predates its referents across kinds. The
-- cost is that retention is explicit rather than ON DELETE CASCADE.
CREATE TABLE completion_effects (
    producer_kind TEXT NOT NULL,         -- 'job_completion' | 'session_turn'
    producer_id   UUID NOT NULL,         -- command id, or the turn's unit id
    effect_name   TEXT NOT NULL,         -- STABLE NAME, never an ordinal
    effect_group  TEXT NOT NULL,         -- independently retryable unit, see (4)
    state         TEXT NOT NULL DEFAULT 'pending',
    attempts      INT  NOT NULL DEFAULT 0,
    intent_at     TIMESTAMPTZ,           -- written BEFORE the external call
    complete_by   TIMESTAMPTZ,           -- strictly shorter than the owner's lease
    completed_at  TIMESTAMPTZ,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. captured k8s UIDs
    error_code    TEXT,
    PRIMARY KEY (producer_kind, producer_id, effect_name)
);
```

Command states are `pending | finalizing | done | parked | superseded`,
app-validated (no CHECK), matching `run_queue`'s convention.

**Why a table and not a JSONB column.** Postgres has no in-place partial update
of a JSONB value: `jsonb_set` assigns a whole new value and MVCC writes a whole
new tuple, and once the value passes the ~2 kB TOAST threshold *every* update
duplicates the entire value plus its WAL. Thirty-seven sequential updates is
therefore quadratic write volume, TOASTing for the back half of every
completion. Append-only rows are linear, immutable, and never need reclaiming.
This is also what DBOS does — `operation_outputs`, one row per step — and DBOS
is the closest existing analogue to this design (Postgres-backed, no replay,
skip-already-executed). Four of five surveyed engines are append-only.

**Why a stable name and not an ordinal.** DBOS keys steps by ordinal and has to
pin recovery to a code version to survive it. With an ordinal key, inserting an
effect at position 5 silently corrupts every in-flight command on the next
deploy — the resumed finalizer skips the wrong effects. `code_version` on the
command row still exists as a backstop, and **the rule for a version mismatch
must be written down before this ships**: drain-before-deploy, or
refuse-and-alert. Not left implicit.

**(2) `stop_identity` was conflating three things**, which is why it resisted
definition. They separate cleanly:

- **Identity** — `(job_id, client_report_id)`, a UUID the agent derives from the
  *stop* and stores alongside `freeze_data`, so an HTTP retry reuses it. Accept
  is `ON CONFLICT (job_id, client_report_id) DO NOTHING`; on conflict it compares
  `payload_digest`. Equal ⇒ return the recorded outcome (the exact-retry
  contract). Divergent payload ⇒ **422**. Same key while the first is still
  being finalized ⇒ **409**. Those two were collapsed onto 409 in the first
  draft, which is wrong because they carry *opposite* client retry policies: 409
  means retry, it will succeed; 422 means never retry, the payload is wrong.
  This follows `draft-ietf-httpapi-idempotency-key-header-07`, which also names
  the digest an "idempotency fingerprint". (Stripe uses 400 for the mismatch, so
  the ecosystem splits; the draft's split is the operationally useful one.) The
  digest must cover the operation identity, not just the body, and the response
  carries an `Idempotent-Replayed` marker so a caller can distinguish "I just did
  this" from "this was already done".
- **Fence** — `accepted_lease_token` XOR `accepted_agent_id`, the same
  discriminated pair `thread_control_requests` uses, because the two lanes prove
  ownership differently and neither should be forced into the other's shape.
  Checked *at accept*, which is what makes it a real fence: the report is
  admitted or rejected in one short transaction, not after 1046 lines.
- **Order** — `report_seq`, allocated by incrementing `jobs.completion_seq_hwm`
  under the jobs-row lock, never from an IDENTITY. This is the control inbox's
  rule and it exists for a reason: an IDENTITY value can be allocated by a later
  transaction that commits first, so it does not order commits. Authority when
  two reports race is the highest `report_seq` whose fence still validates.

`(job_id, lease_token)` alone is insufficient, which the original question
suspected but did not pin down: pinned jobs have no run_queue lease at all, so
every pinned report for a job would collapse onto one key.

**Retention is a correctness property here, not housekeeping.** Stripe prunes
idempotency keys at 24 h and states that a key reused after pruning produces a
*brand-new operation* — silently. A command can sit `parked` awaiting an operator
for days, so **`job_completion_commands` must never be pruned on a timer while
`pending`, `finalizing` or `parked`**, and retention for `done` rows must exceed
the maximum crash-to-resume lag. Retain failures far longer than successes
(River keeps discarded jobs 7 days against 24 h for completed — the asymmetry is
deliberate and worth copying).

**(3) The finalizer speaks `run_queue`'s dialect verbatim** — `attempts` /
`max_attempts` / `run_after` backoff / `parked`, with the same `5s × attempts ×
(1 + U(0, 0.2))` scale and the same per-row CAS. That dialect was checked against
the field and matches what River and pgmq converged on, down to pgmq keeping the
CAS guard *inside* the claiming UPDATE with a comment saying it is needed even
with `SKIP LOCKED`. `parked` is an operator worklist with an `unpark` verb.
Deliberately one dialect, not two.

Two additions the first draft lacked:

- **`deadline_at` is an absolute cap set once and never extended.** Without it, a
  finalizer that heartbeats but makes no progress wedges its job permanently and
  silently — trading a false-positive bug for a liveness bug, which is worse
  because nothing reports it. Step Functions caps task duration "regardless of
  the number of `SendTaskHeartbeat` requests received"; SQS caps at 12 h
  regardless of visibility extensions.
- **A global retry token bucket.** A mass failure (the Kubernetes API being
  briefly unavailable) must not let hundreds of parked commands retry-storm the
  very API that is failing.

**Leader election uses a lease row, not a session advisory lock.** This reverses
the first draft. A session-scoped `pg_advisory_lock` held by a hard-powered-off
or partitioned pod is not released until TCP keepalives expire — on Linux
defaults, **~2 h 11 m**, during which no job in the system completes and nothing
errors. Postgres defaults every keepalive knob to "OS default" and
`client_connection_check_interval` to disabled. Session advisory locks are also
silently broken by PgBouncer in transaction mode (its own matrix says "Never"),
and they are never replicated, so a replica promotion vaporizes all leader state
at once. River — solving this exact problem in Postgres — reserves advisory locks
for a deprecated slow path and uses an expiring `river_leader` row instead:
elect via `INSERT … ON CONFLICT DO NOTHING`, re-elect via an UPDATE keyed on
`(leader_id, elected_at)` so an old term cannot renew a newer one, reap via
`DELETE … WHERE expires_at < now()`. Failover becomes seconds, it survives
promotion, it works through any pooler, and it is observable with one SELECT.

The correctness argument is unchanged and was confirmed as the recognized one:
the election dedups *work*, fencing tokens plus per-row CAS provide
*correctness*. Note that client-go's leader election states outright that it
"does not guarantee that only one client is acting as a leader (a.k.a.
fencing)" — so a dual-leader window must be survivable by construction, never
by assuming the election prevents it.

**Alarm on zero leaders, not just on duplicates.** GitLab shipped an incident
where a transient disconnect left a component permanently non-leader after its
lease renewal failed; leader-only work simply stopped, silently. Here that means
no job completes. The alarms are: zero leaders, and **max age of the oldest
unfinalized command** — age, not count, because count looks healthy right up
until it doesn't.

Pinned jobs run the finalizer **inline in the request**, so the agent still gets
its `200` with `actions`. Stateless jobs get `202` and the background drain.
This means `report_completion` must learn that `202` is success — a one-line
agent change that is a hard ordering constraint in (7).

**(4) Per-effect keys split three ways, not one intent table.** The inventory's
own finding was that the codebase already has the right patterns and the failures
are where they were not applied; a universal intent table would be a fourth style
competing with three existing ones.

- *Already keyed* — change record, session wake, `claim_delegation_resume`, the
  loop barrier. Untouched. These are the reference implementations.
- *Cheaply keyable* — give the effect a natural unique key and the hole closes
  for both lanes with no protocol at all. The critic spawn is the case that
  matters, and it is viable: the child already carries `verification_round` in
  its context (`main.py:15905`). The shape has direct production precedent —
  River enforces unique jobs with a partial unique index over its mutable `state`
  column — but the first draft's DDL had a hole and needs four fixes. See
  **"The critic index, in full"** below; it is step 1 of the rollout and is the
  only part of Gate 3 shipping before the protocol.
- *Naturally idempotent once keyed correctly* — **most of the external set, which
  the first draft got wrong by lumping them under "needs a probe".** Under stated
  conditions these need no probe at all: S3 `PutObject` to a deterministic key
  (body byte-identical, bucket unversioned — a snapshot embedding mtimes breaks
  this), S3 `DeleteObject`, WebDAV `PUT`/`DELETE` (idempotent per RFC 9110 §9.2.2,
  though version history and trash are not — accept that churn), `git push` of an
  already-pushed ref, `git merge` of an already-merged commit, and cloud VM
  deletes. Kubernetes deletes belong here too, but only with the right key —
  see below.
- *Genuinely needs a probe* — the Gitea PR merge, Gitea branch creation, the
  subjob graft commit, Nextcloud chunked upload.
- *Cannot be made exactly-once, and is therefore declared at-least-once* —
  SMTP and push notifications. RFC 5321 concedes duplicate delivery rather than
  solving it, and `Message-ID` obliges no MTA to deduplicate. The first draft's
  rule — "an effect whose probe cannot be written parks for an operator" — is
  **wrong here**: parking a job because an email might send twice is far worse
  than the duplicate. Declare them at-least-once, set a deterministic Message-ID
  as cheap best-effort, and use the command key as ntfy's **sequence ID**, which
  makes a duplicate publish *replace* the notification rather than stack a second.

**Kubernetes deletes: capture the UID, don't probe.** The first draft's probe
("does the pod/PVC still exist") has an ABA hole — between crash and resume a
controller can recreate a Pod or PVC under the identical name, the probe says
"exists", and the finalizer deletes the *replacement*. Record `metadata.uid` in
the effect row's `detail` when planning the delete, then pass it as
`DeleteOptions.preconditions.uid`. All three outcomes are then unambiguous: 2xx
deleted, 404 already gone, **409 a different object holds the name, so mine is
already gone**. Note `kubectl delete` cannot do this; it must go through the
client's `V1Preconditions`. Note also that a 2xx delete means *accepted*, not
completed — `pvc-protection` holds objects in `Terminating` while a pod mounts
them, so any effect that depends on "gone" must still poll. Conversely, K8s
*creates* are natively keyed: `metadata.name` derived from the command key gives
409 `AlreadyExists`, which is a success signal.

**Probes must be written correctly, and two of the first draft's were not.**
Terminal merge is *not* "is the commit an ancestor of `main`" — that works for
merge-commit and fast-forward and **fails for squash and rebase**, where Gitea
mints new commits the original SHA never becomes an ancestor of. Use
`GET /repos/{o}/{r}/pulls/{i}/merge` → 204 merged / 404 not merged, and **key on
the status code, never the body** (that handler writes 204 then falls through, so
the body can carry a 404 payload). Bare 405 is not usable as "already merged" —
the same handler returns it for six other causes. For the graft, deterministic
commit SHAs are too brittle to dedup on, since any unrelated push changes the
parent and therefore the SHA; put a unique trailer carrying the command key in
the commit message and probe with `git log --grep`.

**A probe is only legal after the predecessor is provably gone.** This is the
subtlest correction. A probe assumes the previous attempt is dead; the common
case is that it is slow or partitioned — alive, and about to complete. Probing a
live predecessor returns "not done" and doubles the effect. So each effect
carries `complete_by`, strictly shorter than the command lease, and an executor
that passes its own `complete_by` **abandons and writes nothing — not even an
error** (the Scheduler-Agent-Supervisor discipline; writing an error is itself a
race with the successor). Probing becomes legal only after
`complete_by + max_clock_skew`.

**Effects are grouped, not chained.** The first draft ran all 37 in one sequence,
which means one wedged effect blocks every later one. If a customer's Nextcloud
is down for a week, the pod and PVC deletes never run — their outage becomes our
resource leak, and because the status write moved last, a user-visible stuck job
too. Nothing in the domain requires that ordering. `effect_group` partitions the
set into independently retryable units, each with its own attempts budget and
terminal state, ordered only where a real dependency exists. Irreversible steps
go last within their group, after every validation that could still fail.

**Register the compensation before performing the action**, where a compensation
exists at all. This is Restate's saga rule and it generalizes the intent marker:
a crash between an action succeeding and its compensation being recorded leaves
no undo. Compensations must themselves be idempotent and run in reverse order.

##### The critic index, in full

Shipping first and alone (migrations 0130–0132), because it fixes a live
correctness hole — the duplicate-critic race can drop a blocking finding and
produce an unwarranted approval — and needs none of the protocol above. Three
files are required by the runner's one-statement `.notx.sql` contract: a
transactional dedupe, a concurrent same-name-shell drop, and the concurrent
build itself.

**Decided 2026-08-09: one critic per round, ever — the predicate does not
reference `status`.** A round's slot is never freed for a re-spawn. This matches
what the system already does: when a critic dies without recording a verdict,
`unstick_reviewing_parents` sends the target to `pending_review` rather than
spawning a replacement, so nothing today depends on the slot reopening. Choosing
the immutable predicate removes an entire class of problems — rows never enter or
leave the index, so no status transition can raise a constraint violation, HOT
updates survive status churn, and the build avoids the slower cross-table wait
that partial+expression indexes opt into.

```sql
CREATE UNIQUE INDEX CONCURRENTLY jobs_verification_uniq
    ON jobs (parent_job_id, (context->>'verification_round'))
    WHERE context->>'verification_target' IS NOT NULL
      AND jsonb_exists(context, 'verification_round');
```

The `jsonb_exists` clause is not decoration — see the NULL hole below. The
remaining paragraphs on mutable predicates are retained as the *reason* this
shape was chosen, not as a description of what ships.

**The NULL hole — confirmed by experiment, and fatal as first written.**
`context->>'verification_round'` is SQL NULL when the key is absent, and NULLs
are distinct in a unique index, so any row missing the key enters the index and
collides with nothing. Verified locally: two critics with no round key both
inserted cleanly. Since the whole point is to close a race caused by a bug, "the
writer always populates the key" is exactly the assumption that cannot be made.
Fix with `jsonb_exists(context,'verification_round')` in the predicate (the `?`
operator collides with psycopg placeholders), or `NULLS NOT DISTINCT`, or
`COALESCE(…, '')`.

**Mutable predicates do check on UPDATE — verified locally, and it cuts both
ways.** A row moving *into* the index is uniqueness-checked at that moment:

```
UPDATE t SET status='created' WHERE id=2;
ERROR:  duplicate key value violates unique constraint "t_uq"
```

So the index is stronger than claimed — it blocks reviving a terminal critic into
a collision, not just duplicate inserts. The cost is that **a status transition
can now raise `23505` far from the INSERT that created the duplicate**: resume,
retry, un-cancel and the manual status flip can all fail in a way they never
could before, and every such path must handle it. A transaction touching two keys
in this index can also deadlock (`40P01`); insert multi-row spawns in sorted key
order, and treat both codes as "someone else won, back off and re-read".

**Two mechanical constraints.** `ON CONFLICT` cannot infer a partial index unless
the statement restates the predicate *exactly* — omit it and you get "no unique
or exclusion constraint matching". And a partial, expression-based index **can
never be promoted** to a named constraint via `ADD CONSTRAINT … USING INDEX`
(that requires a plain b-tree over plain columns), so there is no
`ON CONFLICT ON CONSTRAINT` and no `DEFERRABLE`. A **generated stored column**
holding the key, with a plain unique index over it, avoids all of this — it can
back a named constraint, keeps `ON CONFLICT` simple, preserves HOT on status
churn, and sidesteps the slower concurrent-build path that partial+expression
indexes opt into. Prefer it unless there is a reason not to.

**Implemented choice:** keep the exact partial expression index above. No caller
needs a named constraint, and changing every `jobs` row merely to simplify a
single conflict target would broaden the schema for no benefit. The critic
writer keeps its ordinary INSERT and handles only a `23505` whose named index is
`jobs_verification_uniq`; unrelated uniqueness failures still propagate. Thus
there is no `ON CONFLICT` inference clause to drift from the predicate.

**Build safely, and dedupe first.** A failed `CREATE UNIQUE INDEX CONCURRENTLY`
leaves an INVALID index that is *ignored for queries but still enforces
uniqueness* — no benefit, live write rejections. A unique index cannot be added
`NOT VALID`, so pre-existing duplicates must be resolved in an **earlier**
migration. **Correction at implementation:** status is deliberately absent from
the final predicate, so cancellation alone cannot remove a loser. Migration
0130 cancels and unassigns the loser, archives its original round and chosen
winner under `context.verification_dedupe`, then removes only the loser's
`verification_round` key. The critic identity and row history remain; the loser
leaves the exact index predicate. The mandatory pre-flight found 138 indexed
candidates / 138 keys and zero duplicate groups in the production workload;
k3d had 7 / 7 and zero duplicates. And
**`CREATE … IF NOT EXISTS` must not be the `.notx.sql` idempotency mechanism**:
it succeeds against a leftover INVALID index and records the migration green
while the index is permanently unusable and permanently rejecting writes. Check
`pg_index.indisvalid` explicitly and drop-then-recreate. The registered 0132
runner recovery does exactly that: it verifies the catalog shape, executes
0131's concurrent drop for an INVALID or unexpected shell, replays the
replay-safe 0130 dedupe, executes the immutable 0132 bytes, and records success
only after the rebuilt index is unique, valid, ready, live, and exact. The
CREATE deliberately has no `IF NOT EXISTS`.

**(5) The fence replaces the status guard, and is strictly stronger.** Today's
early return keys on a status the handler is itself about to change, which is why
it doubles as the thing that makes a crash unrecoverable. After Gate 3, a genuine
late callback — the k3d-reproduced case at `main.py:17595`, where a dead agent's
trailing `llm_unavailable` flipped `completed` → `paused` 15 s later — arrives
with a *stale fence* and is rejected at accept, on ownership rather than on
status. The status check stays during coexistence as a compatibility belt, with
its role downgraded from replay defence to lane-A behaviour preservation, and
retires when pinned adopts the command path.

**The terminal status write itself must also CAS on the finalizer's term.** A
fence at accept protects accept; it does not stop a resurrected attempt-N
finalizer from clobbering attempt N+1's outcome minutes later. The final write is
`UPDATE jobs SET status = $new WHERE id = $job AND current_term = $term_held`.
Checking lease expiry just before writing is explicitly *not* a substitute — a
process can be paused at exactly the wrong instant between the check and the
write.

**(6) The orphan sweeps must learn about pending commands.** This is the decision
the seven questions missed. Moving the status write last means a job sits in
`processing` with its agent already gone — which is precisely what
`recover_orphaned_jobs` hunts ("assigned agent is online but NOT working on this
job", `postgres.py:5439–5476`). Without a predicate change it would pause and
re-dispatch a job that is mid-finalization, re-executing the work the command was
about to record. This applies to the **pinned** lane too — it is not covered by
the `execution_lane = 'pinned'` partition, because the reordering is what pinned
adopts in step 4 below.

**But the obvious predicate is a Kubernetes finalizer, and it ships Kubernetes'
famous bug.** `AND NOT EXISTS (… state IN ('pending','finalizing'))` gates the
rescue path on the very thing that may be broken: a wedged finalizer leaves the
job stuck *and* invisible to the safety net that would have rescued it. That is
structurally identical to `JobTrackingWithFinalizers`, which cost Kubernetes four
years of bugs including pods stuck 91 days with no controller left to clear them.
Two corrections make it safe:

**Key on a live lease, not on presence.** The predicate is
`AND NOT EXISTS (… state IN ('pending','finalizing') AND lease_expires_at > now())`.
This is the whole fix in one clause — it converts "stuck forever" into "bounded
delay, then the safety net fires". It is Airflow's
`or_(Job.state != RUNNING, Job.latest_heartbeat < limit)` in our schema. Lease
should sit well above heartbeat (Airflow runs ~60×); against our existing 60 s
agent heartbeat, a 10 s finalizer heartbeat with a 120 s lease is proportionate.

**Route, don't filter.** The sweep now matches two failures needing opposite
remedies, so a boolean exclusion is the wrong instrument:

| What the sweep sees | Correct action |
|---|---|
| agent gone, **no** command row | pause + re-dispatch (today's behaviour) |
| command row, **live** lease | nothing |
| command row, **expired** lease | **resume the finalizer from its effect rows** — never re-dispatch the agent |
| past `deadline_at` or attempts cap | park, alert, operator verb |

Re-dispatching the agent in row three is the catastrophic outcome: it re-runs the
work *and* races a half-applied effect set. Airflow runs two sweeps at two
cadences for exactly this reason (worker death vs scheduler death); ours conflates
them today.

**Define the predicate exactly once** — one view or shared CTE, not replicated
across `recover_orphaned_jobs`, `recover_expired_lease_jobs` and the stale-agent
detector. Every bug in the Kubernetes parade is one code path forgetting the
invariant, and a predicate copied N times decays on the first sweep someone adds.

**The reap action needs its own dedup key**, `(job_id, attempt)`. Airflow has this
bug twice: the sweep re-fires every interval because nothing records that a reap
is already in flight, and in one case walked a task to `FAILED` with its retries
never consumed.

**Ship the redundant safety net on day one.** A second, dumber sweep that finds
command rows whose job is already terminal, or that never advanced past their
first effect, and reconciles them. Kubernetes added exactly this in v1.29 —
"guard against any possible bug where a Job is marked as Finished but not all pod
finalizers are removed" — after four years of incidents. There is no reason to
re-earn that lesson.

**The operator force verb must do the opposite of Kubernetes'.** Its `/finalize`
*skips* cleanup, which is how Red Hat demonstrated an orphaned Secret leaking
across tenants. Ours abandons the remaining effects but **still writes the
terminal status**, marks the command `force_resolved`, and records which effects
were skipped — state recorded, tail explicitly abandoned. Log it as an incident,
not a routine operation.

**One invariant, testable in CI:** for every job in `processing`, either a live
agent lease exists, or a live finalizer lease exists, or exactly one sweep
matches it. A job matched by nothing is the stuck namespace, rebuilt.

**(7) Rollout — one triage, then six steps, each independently revertible.**

**Step 0 — the triage, done 2026-08-09. It changes the shape of the design.**

The question was not "status first or last" but **which effects must land before
the status is user-visible-correct?** Working the inventory against that question
splits the ~37 into four classes, and the result is stronger than expected.

**Class A — same row, same UPDATE. Not "effects" at all.** The status write
(S17), `completed_at` (S21), the `assigned_agent_id` clear (S18), and the freeze
stash-then-null (S19) are four separate autocommitted writes **to the same row**.
`update_job_status` already accepts status, `assigned_agent_id`, `freeze_data`
and the error fields — it simply does not take `completed_at`, which is the
entire reason `completed` with `completed_at IS NULL` is reachable. Folding these
into one UPDATE kills two live bugs outright — the NULL `completed_at` (S21) and
S19's torn window that leaves a paused job permanently invisible to the
dispatcher — **with no protocol, no command row, and no finalizer.** This is the
cheapest correctness win in the whole of Gate 3 and it belongs in step 1.

**Class B — decides *what* the status is; must precede it, and is DB-only.** The
deliverable-gate outcome (S14), the verification-enabled decision that chooses
`reviewing` over `completed`, the critic-verdict consequence (S27), and the loop
barrier claim (S32's CAS). These are cheap decisions, not deliveries, and they
already run before the write. They join Class A's transaction.

**Class C — must follow, and must be durable. This is what the finalizer is
for.** Subjob graft (S26), verification critic spawn (S30), loop advance (S32),
terminal merge (S33), parent unblocks (S28/S29), and workspace archive and
teardown (S36) — which must *never* precede the status write, since it destroys
the workspace.

**Class D — may lag arbitrarily, at-least-once, nobody waits.** Notifications
(S13/S20/S25), the freeze workspace snapshot (S24), session wake (S34/S37), the
dispatch trigger (S35), KB reindex.

**The finding: the gating set contains no external I/O whatsoever.** Class A is
one row; Class B is four DB reads. Everything with a Kubernetes call, a Gitea
call, a WebDAV write or an SMTP send is Class C or D. That makes **Temporal's
inversion viable after all** — terminal status *and* the command row in one small
transaction, then drain the tail — which is a materially simpler design than
"status last", because the command row is the replay guard, so the status no
longer has to be.

**One honest exception, and it is the reason this is not a pure inversion.** For
a job whose product is a delivered artifact, `completed` before delivery is a
lie: the user looks for the output and it is not there. Today that boundary is
already inconsistent — cloud delivery (S15) precedes the status write while the
Gitea merge (S33) and graft (S26) follow it. So delivery effects gate the
*terminal* status; teardown, spawns, loop advance and notifications do not. This
is also exactly the trap Kubernetes hit in 1.31, where a premature `Complete`
broke downstream usage accounting; their compensation was interim conditions, and
ours is the command row, which is already queryable.

**Net effect on the rollout:** step 1 grows to include the Class A merge (a
strict improvement, no dependencies), and step 4 shrinks from "move the status
write last" to "move it after Class B and the delivery effects only". The
window that (6)'s liveness machinery has to defend shrinks with it.

1. **0130–0132** — the standalone fixes, all independent of the protocol and all
   fixing live pinned-lane bugs today: the **Class A merge** (status,
   `completed_at`, `assigned_agent_id`, freeze stash/null in one UPDATE — this
   subsumes the `COALESCE` patch and closes S19's invisibility window), and the
   critic index with its dedupe pre-flight as a separate earlier migration.
2. **0133+** — the command and effect tables, `jobs.completion_seq_hwm`, the
   leader lease row, and the sweep predicate view. Dead schema; zero behaviour
   change.
3. Accept writes the command for **both** lanes; the finalizer runs **inline**
   for both. Behaviour is identical to today, but every report is now durably
   recorded. This is the step that carries real risk — soak it behind a flag.
4. The status write moves after **Class B and the delivery effects only** (not
   after all 37 — see the step-0 triage), inside the inline finalizer, together
   with (6)'s sweep predicates. Pinned gains crash recovery here.
5. Background finalizer enabled for stateless units only.
6. Stateless worker admission opens.

The agent-side `client_report_id` must be **optional** with a server-side
fallback (synthesize from `(job_id, report_seq)`), so an orchestrator that
understands the field can run against an agent image that does not yet send it.
The `202` acceptance in `report_completion`, conversely, must ship in the agent
**before** any orchestrator returns `202` — the same "orchestrator understands it
first, agent emits it second" discipline §5.4.1 imposes on freeze types, in the
one direction where it inverts.

**(8) The live pinned-lane bugs are fixed sooner, not here** — with two
exceptions that genuinely need the protocol. The duplicate critic (S30) and
`completed_at IS NULL` (S21) are step 1 above and should not wait for Gate 3: the
first is a correctness hole in the review system that can approve work which
should have been returned. The workspace leak (S36) *is* the one-way-door problem
and needs Gate 3 proper — but the bleeding can be stopped independently with a
reconciler sweep over terminal jobs with un-archived workspaces, which is worth
building regardless because the finalizer needs exactly that backstop anyway. The
S8 VM-recovery wedge and the S19 dispatcher-invisible window are fixed properly by
the command's atomicity; each also has a cheap sweeper backstop if they bite
before then.

##### The finalizer is shared with the session lane (decided 2026-08-09)

S2 stopped before building a generic background-work outbox for sessions,
correctly: making it safe requires enqueueing the effect inside the final
message-persist transaction *and* having `complete_unit` block or reconcile on
it, which is a completion-protocol change. Today the session lane keeps that work
correct by **holding the lease** instead — `_await_cloud_push` blocks
`complete_unit` until the push reaches a terminal outcome, because "a live task
after `complete_unit` has no durable ownership and can race the next claimant".
An outbox replaces holding with releasing, and that is the same problem Gate 3 is
already solving.

**Ruling: one substrate, both lanes.** A session turn's background work is
another effect-group producer, not a second mechanism. Two consequences to build
in from the start rather than retrofit:

- **The effect table is polymorphic from day one**, following `run_queue`'s
  precedent (`unit_id` + `unit_kind`, "deliberately NO foreign key — the queue
  outlives and predates its referents across kinds"). Retrofitting a producer
  kind later means a migration on a table that is by then hot. The command row
  keeps its `job_id` FK for the worker lane; the *effects* table does not
  inherit it.
- **The fence generalizes cleanly** — a session turn's owner is always a
  `lease_token`, which is one arm of the `lease_token` XOR `agent_id` pair the
  command row already carries.

Sequencing: this lands **after** step 1 below, so the three items S2 has blocked
behind it (exact `llm_requests` archival, turn-end memory capture, post-callback
Git turn→SHA ordering) wait on the substrate rather than on all of Gate 3.

**Migration numbering:** S1 used 0115–0121; S2 used **0122–0129** (all consumed);
**Gate 3 owns 0130–0149**. The wider range is deliberate — S2 was allocated eight
numbers, used all eight, and stopped partly because it had none left, which was
an allocation error rather than a design constraint. Range allocation itself
remains necessary: the earlier dev-cluster wedge came from two tracks numbering
independently against one shared database.

##### Why not adopt a durable-execution engine

Worth stating explicitly, because this design is a hand-rolled workflow engine
and that deserves a defence rather than a silence.

Temporal and Restate both require a new stateful service — a server cluster, or
a replicated log with local RocksDB and an object store for snapshots. Not
proportionate for one endpoint. **DBOS is the one credible incremental option**:
a library, `pip install`, no infrastructure beyond Postgres, composes with
FastAPI. But adopting it would not retire the hard problem. Its exactly-once
guarantee is scoped to steps that are Postgres writes piggybacked onto the
checkpoint transaction; everything else is explicitly "tried at least once, never
re-executed after they complete". Thirty-five of our thirty-seven effects are not
Postgres writes, so DBOS would hand them all back as at-least-once and we would
still write every probe, intent marker and at-most-once path ourselves. It would
also stand up a second work queue beside our lease-fenced one, with a second
dispatcher, a second lease model and a second set of stuck-work alarms.

So: keep the hand-rolled finalizer, and **copy DBOS's schema decisions rather
than inventing new ones** — one row per effect keyed by stable name, an explicit
terminal exhausted-attempts state, an attempts counter incremented in the same
transaction as the effect marker, and a recorded code version that gates
recovery. That converts "we hand-rolled it" from a risk into a documented choice.

(One piece of folklore deliberately *not* cited here: the "we built our own
workflow engine and regretted it" retrospective genre has no first-party source —
it traces to vendor marketing. It is not evidence and should not be used as any.)

**Still unsettled, deliberately.** Three things are left to implementation
because the answer depends on measurements that do not exist yet. The finalizer's
poll cadence and batch size join the aggregate DB budget in §5.5, itself
unmeasured. Whether `payload` is pruned or archived after `done` needs a
retention rule before the worker lane runs at volume — a loop job's `freeze_data`
is not small and this table grows once per batch rotation — bounded by the
never-prune-while-unfinalized rule in (2). And the command table is a high-churn
status-driven queue living in the shared application database, which is the exact
structure that degrades when a long-lived transaction elsewhere in the cluster
holds back dead-tuple reclamation; `SKIP LOCKED` does not immunize against it, so
long-transaction monitoring is a prerequisite for running this at volume, not an
afterthought.

##### Independent confirmation from the S3 track

The worker track reached the same conclusion from the other direction, and added detail worth keeping: `JobCompleteRequest` carries no agent identity or lease token, `update_job_status()` ends in `UPDATE jobs ... WHERE id = ?`, and the completion route performs independently committed job mutations plus external delivery, critic/delegation/loop, notification, and workspace-cleanup effects before and after that update. A short entry fence can be stolen immediately after it commits; an end-only CAS permits stale side effects first; holding the queue lock across the route would put network I/O inside a long transaction and block reaping. Stateless reports must instead be accepted as immutable commands keyed by `(job_id, lease_token)` in one short queue-first transaction, then finalized by a durable visibility-timeout/retry worker. Exact retries return the stored command result; divergent payloads or unaccepted stale tokens fail closed. A `batch_boundary` can use a bounded atomic pause/stash/requeue disposition, but terminal stateless admission remains closed until every retry-dangerous completion effect has an idempotent or durably journaled finalization step. Gate 3 is not complete with only the intake table or boundary disposition.

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

**S3 single-pool experiment ruling (2026-08-08): do not ship one shared Deployment yet.** The requested experiment did not legally reach worker load: Gate 3 stopped admission before the first `worker_batch`. The implementation audit also found that a pod-local "leave N slots free" rule does not enforce a pool-wide reservation, and a static replica count cannot preserve that reservation through a rollout, pod loss, or a temporarily unavailable executor. The existing stateless Deployment also intentionally lacks the VM-mesh sidecar needed by one worker capability class. Keep the two-Deployment production design unless a coordinator-visible executor-presence/capacity contract enforces the interactive reserve across failure states, capability-aware claims are available, and the loaded claim-wait benchmark passes. This is a safety/availability no-go, not a measured latency regression; no worker traffic was admitted from which to claim performance evidence.

## 6. Prerequisites (corrected)

1. **Agent-local `/workspace` is not the persistent-session authority.** The
   S2 §6.1 inventory and two-pod live inspection corrected the earlier PVC
   premise: both stateless containers had an empty `/workspace`, while virtual
   files already live in object storage and sandbox/VM files and shells live on
   the remote workspace. The one session-path local file is a recomputable
   vision cache. The real gaps are process-local task/undo/extraction/anchor
   state and three tools that bypass the backend abstraction. Those must move
   to DB/object storage or become explicitly disposable; an agent PVC would not
   repair them. Full inventory and per-item decisions are in the implementation
   log; remediation is not DONE.
2. **Remote-shell handoff substrate is built, but physical admission remains
   closed.** `disconnect()` is transport-only, queue handoff preserves tmux,
   and a successor reconstructs validated tab/pane/setup/pending state. A
   workspace-side `active | creating | retired` record plus the queue lease
   token fences create, promotion, command submission and teardown; commands
   carry strict completion sentinels rather than trusting prompt text. A direct
   k3d exercise handed exported environment and cwd between two different
   stateless pods and rejected the stale claimant. This is deliberately more
   than the old "reattach-if-exists" prerequisite: before enabling sandbox,
   ownership still must bind to the authoritative workspace/runtime
   incarnation, lifecycle retirement failures must be reconciled, and the
   correctness lock/marker must move outside the workload user's writable
   authority (or that cooperative-only threat boundary must be accepted).
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
- **S2 — workspace sessions.** SSH affinity + tmux reattach; §6.1's
  process-local state and backend-path bypasses are externalized or explicitly
  retired (the corrected inventory requires no agent PVC); cloud-push
  sync-generation fence; outbox re-homing (incl. llm_requests archive);
  resident daemons re-homed; hard-interrupt routing; presence re-home;
  gate-release flow. Acceptance: push(N)→pull(N+1) ordering holds across a
  forced pod handoff; tmux state survives handoff; mid-idle rclone token expiry
  heals on next claim; p95 approval-to-visible-resume < 3 s; canvas presence has
  no inter-turn flicker (or the flicker is named and accepted).
- **S3 — workers.** `batch_boundary` + consolidated freeze registry + TodoManager hydration + conditional tmux kill; pull-claim + claim-token renewal + durable completion acceptance/finalization keyed on `lease_token`; **coexistence partition on `jobs.execution_lane` everywhere** (§5.4.4); checkpoint-coupled steering acks; `paused_reason` sub-state; wall-clock batch budget; fairness + cooldown waiver; snapshot-lane retirement; job-journal decision executed (§10.2); job-log per-claim capture; deploy-order constraint enforced (orchestrator first; batch-off before rollback). Gate: Job Bench A/B vs pinned baseline — completion parity, tokens/wall within noise, measured overhead (target < 2% at wall-clock-sized batches; measure MCP-attached separately), KV-cache hit rate, **fault injection**: kill -9 mid-batch, steal during a tool call, fenced-out `/complete` rejected, crash/retry convergence of the completion finalizer, and the phantom-complete skew test on a loop job.
- **S4 — optional, bill-driven**: in-process multiplexing; compile-once; workspace pause tier (§10.7); JetStream hatch.
- **Rollback**: per-class flag at every stage; pinned plane intact until lane retirement decisions (§10.9).

### 9.1 Implementation status (2026-08-10)

Written against the code on the consolidated `feature/stateless-agents`, not against
intent. The short version: **the shared substrate is built and genuinely
kind-agnostic; the session lane is functionally complete; the worker lane has two
of three safety gates and is not enabled, because Gate 3 (§5.4.5) is a design
problem rather than a predicate change — now designed, not yet built.**

#### Is it shared between sessions and workers? The substrate yes; a shared pool has not passed its gates.

"Shared" covers three different things in this design, and they landed differently:

| | Design | Built |
|---|---|---|
| Queue + lease contract | ONE `run_queue` table discriminated by `unit_kind` (`session_turn`/`worker_batch`/`bg_task`); one claim/heartbeat/fence/completion/reaper semantics | **BUILT, kind-agnostic.** `src/shared/run_queue/` takes `unit_kind` as a parameter everywhere — claim, fence, completion, release, heartbeat, steal, unpark, read models — and no SQL in it knows what a session is. The reaper steals any expired lease and only *journals* for sessions, with an explicit `(S3)` skip. Only soft coupling: `consumed_seq = input_seq - 1` at row creation assumes a monotonic input counter, which a worker lane without an input stream would simply leave unused |
| Event journal | (implied shared) | **NOT shared — session-only by construction.** `src/shared/event_journal/` hardcodes `threads` and `thread_events` and keys on `thread_id`. Workers have no journal at all; that is the §10.2 job-journal decision, still open. The `src/shared/` path is about who *imports* it (agent + orchestrator), not about unit kinds |
| Pod pools | Production design remains TWO Deployments off ONE image (§5.8): interactive warm floor vs worker KEDA scaler at `minReplicaCount: 0`; the S3 brief requested a guarded one-pool experiment | **ONE session-only Deployment built** — `helm/templates/agent/stateless-deployment.yaml`, chart-gated default off, static `replicas: 2`. It does not claim workers and intentionally lacks the VM-mesh sidecar. The one-pool experiment stopped before traffic because Gate 3 is open; no availability reservation or claim-wait result exists |
| Drivers | A session driver and a worker driver over the same queue | **Session only** (`src/api/turn_executor.py`) |

The queue intent stands and the foundation is real: worker leases need no
`run_queue` schema change and no new lease semantics. S3 has now added the
separate jobs-side safety partition and default-unarmed boundary contract.
Concretely, `jobs.execution_lane` exists and all audited legacy claims/recovery
paths admit only `pinned`; the shared freeze taxonomy knows `batch_boundary` and
unknown declared loop freezes cannot complete a job. Nothing yet enqueues,
claims, or completes a `worker_batch`, because the completion ownership gate is
still open. Legacy job dispatch remains `JobStartRequest` POST to a pod IP.

#### S1 — session lane functionally complete

Rewritten 2026-08-09 at consolidation as one current picture, replacing the
layered dated appendices this section had accumulated. Everything below is on
`feature/stateless-agents` and k3d-verified unless marked otherwise.

**The substrate.** `run_queue` with claim / heartbeat / reaper / completion and
the persist fence (0115/0117, 81 real-Postgres tests); the epoch/seq/
system-writer redesign (0116), which also removed the *pinned* lane's
cache-wipe cascade; turn rows, watermarks and skip-if-answered; scrub-on-claim,
which fixed a live cross-tenant env leak on the pinned pool; the flag-gated
Deployment; soft affinity with warm-session reuse. Fault matrix passed and was
re-verified after the claim path changed: takeover ≤105 s, no duplicate answer,
the zombie's late persist fenced out, FIFO drain across a steal, epoch bumps
≤1 per steal and 0 per clean handoff, zero in-process claim state.

**Performance.** Turn latency 99.6 s → **5.4 s cold / 3.0 s warm** (§5.3.3 for
the measured decomposition, §5.3.4 for affinity). The wins were duplicated work,
not algorithmic: two cloud pulls per turn were redundant full-tree walks, the
WebDAV listing cost ~2.5 s per directory where one `Depth: infinity` PROPFIND
costs ~1 s, and virtual-tier setup spent 51 rclone process spawns on existence
probes that one scoped listing now answers.

**Admission safety.** `/resume`, `/prepare`, create-thread provisioning, the
resume background refetch, warm-pool attach, slow dedicated-pod registration,
permission-link wake and officer respawn all whitelist `execution_lane='pinned'`
— unknown future lanes fail closed. The original brief named four entries; six
more were found by reading, and every one of them could otherwise have bound or
spawned a pinned executor against a queue-served thread. Warm-pool attach now
reserves both sides in one transaction before any HTTP, and treats **every**
non-200 (including 409) as ambiguous, retaining ownership rather than risking a
second executor. Registration no longer treats hostname as an ownership
credential.

**Client-facing transport, with the lane invisible.** A lane-agnostic
`GET /api/persistent/threads/{id}/state` returns a durable, owner-gated snapshot
(explicit allow-list — no config or credential surface) plus event and replay
cursors, so a cold load, a hard reload and a second tab all reconstruct state
without a socket. `/connection` is discriminated by `control_socket`, never by
lane. Control verbs (`mode.set`, `narration.set`) go over REST **for both
lanes**, through a durable commit-ordered inbox (0119–0121) whose requests are
consumed by the lease owner, which applies them and writes the journal receipt
with its own allocator — the orchestrator never writes the journal, which would
collide with a warm pod's in-process seq counter. `execution_lane` appears
nowhere in the cockpit. Verified under real BFF-cookie login: a supervised gate
survived a hard reload and a second tab, was denied over REST, was
journal-acknowledged, and the tool never executed.

**Still open in S1**: permission-row retirement on lease expiry; stateless
ended-session wake; Path-A resume-compaction persistence (a live bug — see
§5.3.3); durable queued-turn UX; control verbs beyond the two scalars;
metering lease-interval ingestion; the journal coalescing tick; object-store PUT
fencing; and the §6.1 inventory's durable task/undo/extraction/anchor fixes.

**Acceptance — met**: create-to-accepted <1 s; takeover ≤105 s with no duplicate
answer; the fence assert; FIFO drain across a steal; epoch bump bounds; zero
in-process claim state; the zombie's rejected persist. **Unmeasured or
unverified**: TTFT p50/p95 (whole-turn wall time is a different number and needs
streaming instrumentation); p95 claim-wait under concurrency (never load-tested);
`cache_read > 0` on a cross-pod follow-up (`cached_tokens` is captured but the
`prompt_cache_key` pin OQ5 requires is set nowhere); the poison-unit terminal
frame and operator unpark end to end; the tenant A→B residue probe, which needs
a second user identity; and live mid-turn/retention fault injection for the
control inbox, which is unit-tested rather than cluster-proven.

#### S2 safety gate landed; S3 has its safety foundation but is not enabled

**S2** (workspace sessions): the mandatory first fail-closed gate is built and
k3d-verified. Stateless human input, new durable controls and the internal
claim-bundle credential boundary admit only exact lite declarations
(`virtual`/`none`) with no physical sandbox/VM evidence. Admission is rechecked
on the locked thread row in the same message/control transaction; stale lite
declarations carrying `workspace_container`, `vm`, or a remote workspace
binding fail closed. Live workspace/VM upgrades and generic backend mutation
also refuse stateless rows before provisioning or persistence. A live
declared-virtual/physically-remote probe returned 409 for input and control with
message/control/queue counts unchanged. The gate stays until S2 acceptance
passes. The §6.1 inventory is recorded and corrects the agent-PVC premise, but
its RAM/path-bypass remediations are not built. The gate-closed SSH/tmux
handoff substrate is built and directly k3d-verified across two stateless pods:
environment and cwd survived, and the stale lease token could not mutate the
successor. The chart supplies the existing read-only workspace SSH key and
allows only SSH/CDP workspace ingress for the stateless Deployment.

The §3.3 cloud sync-generation fence is also built and two-pod verified for the
fallback push/pull driver. Migrations **0122–0124** bind a stable cloud scope,
workspace generation, queue generation and content baseline. Claim start
reconciles the predecessor before strict pull and ARM; turn completion cannot
release while its generation push is live. A forced pod-A→pod-B sandbox/real-
Nextcloud proof preserved one same-size local edit and one unrelated same-size
cloud edit, with the successor doing ACK-only recovery and zero replay uploads.
The DB fence is control-plane enforced among honest executors; WebDAV bytes and
the user-writable resource marker remain cooperative, so a PUT already in
flight can finish after lease loss without server-side conditional writes.

The §3.4 resident-daemon path is also built and live-verified. Stateless
physical handoff now retires the claim-scoped token-refresh and overlay-monitor
controllers while leaving their workspace-side rclone/overlay processes in
place. The next exact lease owner publishes a fresh bearer, forces a bounded
non-recursive VFS refresh, and either adopts the healthy resident or performs an
exact fenced heal. A live token-rotation handoff kept the same rclone PID and
generation, rejected both local and remote stale-owner writes, and adopted the
overlay with its upper data intact. A second fault run killed the exact lower
PID, observed the stale ENOTCONN mount-table entry, and healed in **1.212 s** to
a new PID and generation while preserving the overlay upperdir. The remote
resource marker and flock live in the workload user's home and are therefore a
**cooperative correctness protocol**, not a security boundary against arbitrary
shell mutation. Pinned cleanup keeps its historical destructive behavior.

The first §3.5 slice is built too. Migration **0125** adds a bounded,
database-clock attached-client TTL renewed by the existing owner-gated SSE
stream. The client remains lane-free and uses its normal BFF cookie; every
renewal revalidates current ownership. Stateless natural-pause and permission
expiry now consult that durable oracle, with exact queue fencing on the
irreversible permission CAS and a leader convergence pass after the final
client's reload grace expires. Pinned WebSocket/subscriber behavior is
unchanged. Live `expired`/`interrupted` permission outcomes retire the card
without being fabricated as user denials. A fresh migration replay took
**16 ms**; exact in-cluster presence renewal measured **22.37 ms cold** and
**6.47/7.19 ms warm**, and an unauthenticated SSE request returned 401 with no
presence row written.

The second §3.5 slice is now built as well. Migration **0126** adds one durable
Canvas-awareness row per browser editor, with a database-clock **15-second**
editing lease, idle tombstone, stable server sender id and monotonic client
sequence. A thread advisory lock makes the **256-row** cap exact under
concurrent first writes; equal retries do not refresh TTL, and exact
path/revision/source-version validation prevents a stale editor from presenting
against a newer Canvas. Both lanes use the same owner-gated PUT and dedicated
SSE stream. The SSE sends named, complete `canvas_awareness` snapshots (including
empty), comments for transport keepalive, and no `id:` or journal row; it
periodically reauthorizes the current BFF cookie. The Cockpit no longer sends
awareness over the control WebSocket, and a fresh popout rotates a copied
opener identity before its first sync while retaining that identity across its
own reload.

The third §3.5 slice, hard interrupt, is now built. Migrations **0127–0129** add
an exact-target interrupt inbox, a linked journal-receipt index and validated
constraints. A new Cockpit request carries one stable UUID plus the concrete
turn id it observed. Stateless admission locks the thread and queue in the
established order and accepts only while the exact lease owner has opened that
turn's interrupt window. Opening happens only after the consumer is ready;
completion closes admission, joins the watcher without cancellation, and
drains the committed tail before exposing the queue unit. Pinned legacy `{}`
requests still take the historical direct-agent path; a correlated pinned
request is checked against the exact active turn before it can set RAM state.

An admitted request is durable stop intent, not a best-effort signal. The live
owner signals its exact turn and writes one linked `interrupt.ack`; applied
sibling requests share one durable consumed-input marker so they cannot advance
multiple human rows. If the owner disappears before writing the receipt, the
reaper or exact successor settles the old request as an applied hard stop,
advances only the human row whose `turn_number` matches the target, rotates the
dead journal epoch once and never signals successor RAM. This ordering prevents
a turn which the user stopped from being silently re-executed after a pod loss.
The Cockpit correlates receipts and terminal frames by target/request, so a
replayed old acknowledgement cannot close or rename a newer recovered turn.

The Canvas server contract passed **7 real-Postgres tests** and the complete
scratch-Postgres substrate through 0126 passed **108/108**. The final frozen
interrupt integration group passed **502 tests with 116 environment skips**;
an independent adversarial review passed **419 with the same 116 skips**. Full
Cockpit Vitest passed **1,829 tests in 119 files** and its production build
passed. Repository-wide pytest finished with **15,481 passed / 142 skipped /
11 failed in 801.06 s**. The eleven failures are exactly the established
localhost-Postgres, MCP transport/wiring and optional arXiv/Semantic Scholar
environment baseline. The app schema replay passed **113 transactional plus 16
non-transactional migrations**; 0127–0129 are already applied and their bytes
and checksums are frozen. The non-behavioral 0127 comments still describe the
superseded “successor never settles” model and must not be repaired by changing
the applied migration.

The earlier authenticated Canvas proof used two independent BFF contexts. On a
stateless fixture, each observed the other's PUT through the dedicated SSE in
**1040 ms** and **649 ms**; hard reload preserved the editor/sender identity,
and expiry produced the complete empty snapshot in **14.897 s**. A pinned
fixture delivered the same snapshot in **916 ms**. A separate authenticated
hard-interrupt fallback accepted the correlated POST in **12.81 ms**, wrote and
settled exactly one applied receipt in **51.82 ms**, returned the exact duplicate
in **7.55 ms**, rejected both a fresh stale-turn UUID and an old UUID retargeted
to the next turn with 409, and left that next turn's gate open. It cleaned every
fixture, BFF, pre-auth and Keycloak row; the orchestrator stayed **1/1 Ready**
and the stateless Deployment **2/2 Ready**. The fallback proves the public/API,
database and replay fences, not a live executor's RAM unwind, LISTEN latency or
a forced-pod interrupt handoff. No live pinned executor existed, so pinned
interrupt behavior remains unit/integration-tested rather than live-smoked.

The generic §3.5 outbox is deliberately **not** built. Its design requires
effect enqueue in the same transaction as the turn's final message persist,
but that persist is currently non-fatal and may time out while `complete_unit`
still advances the input watermark. Making the enqueue authoritative therefore
changes the normal completion protocol. No safe generic payload carrier exists
in 0122–0129, and migrations 0130+ belong to Gate 3. Per the stop rule, this
pass did not repurpose the officer wake outbox or add a competing live-epoch
journal writer. Exact LLM audit, turn-end memory and post-callback Git ordering
remain blocked on that boundary; Canvas revision snapshots, a pending-citation
sweeper and orchestrator-derived notifications are separable future slices,
but were not started after the stop condition triggered.

This still does **not** remove the tier gate. Browser unload and ordinary
**Duplicate Tab** retain the accepted awareness limitations, and the full
rendered two-editor Monaco UX plus native EventSource auth-expiry recovery remain
unverified. Durable Canvas mutation invalidations, the generic outbox,
workspace/runtime-incarnation authority, durable terminal-retirement
acknowledgement and the deferred §6.1 RAM/path-bypass work remain open. None of
the direct proofs changed a sandbox thread's lane or created a worker batch.
The S2 sandbox tier gate remains **closed**.

**S3** (workers): **Gates 1 and 2 are built and merged**; worker admission stays
closed. No `worker_batch` unit has ever been enqueued and no job carries a
non-pinned lane.

*Gate 1 — the coexistence partition (§5.4.4).* Migration **0118** adds
`jobs.execution_lane NOT NULL DEFAULT 'pinned'`. `get_dispatchable_jobs`,
`claim_job_for_agent`, `recover_expired_lease_jobs`, `recover_orphaned_jobs`
(four separate predicates) and `register_agent` all whitelist `'pinned'`, so an
unknown future lane fails closed rather than inheriting pinned dispatch. The
three direct dispatch paths — fresh start, resume, manual admin assign — refuse
defensively on top of that, and real-Postgres tests fail without the partition.

*Gate 2 — the freeze contract (§5.4.1).* `src/shared/job_freeze_types.py`
consolidates the four keep-in-sync freeze lists that spanned two separately
rolling images, and the orphan-recovery SQL now takes the set as a parameter so
it cannot drift from status determination. `batch_boundary` joins
`CONTINUE_AS_NEW_FREEZE_TYPES`, so it resolves to a non-terminal `paused`. The
phantom-COMPLETE hazard is **closed**: `determine_job_status` used to map *any*
non-completion stop on a loop job to `completed`; it now does so only when no
freeze type was declared, and routes an unknown *declared* freeze to
`pending_review` with an error log. The `batch_boundary` freeze exists in the
graph and is **unarmed by construction** — it needs state fields nothing sets —
wall-clock-first with a 300 s floor, correctly placed at both phase and
mid-phase boundaries, and it clears its whole arming envelope in the same
checkpoint as the freeze so a resumed job cannot immediately re-freeze.

*Gate 3 — step 1 built 2026-08-10; protocol not built (§5.4.5).* The two
standalone pinned-lane fixes now ship in isolation. `/complete` writes status,
the first `completed_at`, paused-agent release and any auto-redispatch freeze
stash/null as one jobs-row UPDATE. Migrations 0130–0132 dedupe historical
critic keys, explicitly remove any valid/INVALID same-name shell, and build the
exact immutable one-critic-per-round index concurrently. The production
pre-flight found **138 candidates / 138 keys and zero duplicate groups**, so
there is no durable evidence that S30 fired there; it did find **one of 382
completed jobs with `completed_at IS NULL`**, confirming S21 fired. The command
table, polymorphic effects table, finalizer and every completion-protocol change
remain unbuilt and begin at 0133+.

The checksummed 0131/0132 SQL headers retain their initial manual-repair
runbook because those bytes were applied before automatic recovery was added.
They are historical only: `orchestrator/database/migration_recovery.py` and the
database migration runbook are the status of record for a standard dirty-0132
retry; no ledger-row surgery is required.

**What blocks worker admission is the driver, not Gate 3** (scope correction,
§5.4.5, 2026-08-10 — rotation releases through the queue and never calls
`/complete`). The driver work list, all with settled designs: the batch-freeze
driver with TodoManager hydration on any resume, generator-close discipline,
pull-claim with claim-token renewal, the §5.4.4 pt-2 claim CAS and enqueue,
lane-aware resume/approve/feedback verbs (§5.4.5 condition 3), the thin
`/complete` entry fence (condition 2), checkpoint-coupled steering acks, the
wall-clock batch budget, the worker Deployment and KEDA, job-log per-claim
capture, and the Job Bench A/B gate. Conditional tmux kill is superseded by
S2's ownership-fenced handoff substrate; `paused_reason='batch_rotation'`
dissolved with the scope correction.

**The single-pool question is also unresolved, with evidence against it.** A
pod-local capacity reserve cannot guarantee interactive availability across a
rollout or pod loss, which argues *for* §5.8's two-Deployment split; and the
current Deployment lacks the VM-mesh capability §5.8 requires for VM-workspace
jobs.

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
6. ~~**Interactive saturation policy / shared worker capacity**~~ — **S3 ruling: do not ship one pool yet.** Gate 3 stopped worker admission before a load result existed, and the proposed pod-local reserve is not a pool-wide availability guarantee during rollout or pod loss. Re-open only with coordinator-visible executor capacity, capability-aware claims, and a passing p95 session claim-wait benchmark; otherwise retain two Deployments.
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
