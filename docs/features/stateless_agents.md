# Stateless Agents — Turn Execution as a Deployment

**Status:** v3.7 — **GATE 3 STEPS 1–5, SESSION HARDENING AND THE WORKER WEDGE FOLLOW-UPS ARE BUILT, LIVE-VERIFIED AND PUSHED (2026-08-14).** The session spine, its performance work, the S1 session lane, the S2 handoff substrate, the S3 worker driver, Gate 3 steps 1–5, the session final-memory/permission/wake hardening stack and the worker wedge follow-ups are all on `origin/develop` and green in CI. The deferred worker cloud soak ran with the follow-ups (five clean rotations, forced-422 recovery, specimen convergence), and the previously unproven executor-after-persist crash row is now proven live. Step 6 (opening worker admission) remains unshipped and default-off — it is a values flip plus opt-in probe jobs, not a build. **Where things stand is §9.1 Implementation status**, written against the code rather than intent.

In one paragraph: the shared queue/lease substrate is built, k3d-verified and genuinely kind-agnostic; **the session lane is functionally complete** — turns queue and run on any pod, survive a mid-generation kill, never double-answer, reconnect with no socket, and take durable control verbs, all without the cockpit ever learning a lane exists; **the worker driver is built behind an independent default-off admission gate** — exact Kubernetes workspaces can enqueue, rotate without calling the completion handler, resume on another pod with checkpoint/Todo/tmux state intact, and fence a stale completion report. VM jobs remain pinned. Gate 3 now durably accepts and finalizes pinned and stateless-worker reports, orders product delivery before terminal status, and returns HTTP 202 for a freshly accepted stateless-worker report so the existing background drain can finish it. The worker holds its exact tmux lifecycle until that command's stored outcome or a fail-closed handoff boundary: terminal outcomes retire it; human-facing/retry outcomes preserve it for reattachment. Admission-off leaves the stateless executor running to drain durable residue, with independent oldest-runnable-queue monitoring. The later session-hardening run durably covered final-memory obligations and claim-bound permission/wake convergence, and live-proved exact interrupt plus permission owner-loss on `MiniMax-M3`; its executor-after-persist crash row is now proven too — a fenced final-memory obligation survived deletion of the pod that minted it and drained exactly once against a deliberately blocked destination (§9.1). The worker wedge follow-ups then closed the last known worker-lane failure mode: a second LangGraph state update was consuming the pending successor task, so the graph ran no successor and the driver fell through to `/complete` with a continue-shaped report that the accept path used to honour. Routing and arming are now one durable update; a coded non-terminal 422 is a definitive pre-write refusal the worker releases from with its shell preserved; and a stateless job whose queue row is terminal with no unfinished command is parked for an operator rather than owned by nobody. The deferred worker cloud soak ran with them (§9.1). Step 6 still has not opened worker admission. Turn latency went 99.6 s → 5.4 s cold / 3.0 s warm (§5.3.3, §5.3.4). Build history, measurements and failures: `docs/research/stateless_agents/implementation_log.md`; the completion-path evidence base is `docs/research/stateless_agents/completion_path_side_effect_inventory.md`.

v1 2026-08-06 (initial proposal); v2 2026-08-07 after an 8-agent research fan-out; **v3 same night after a 6-lens adversarial panel** (10 critical + 28 major findings folded in). Raw research and critic reports: `docs/research/stateless_agents/`. Related implementation docs carrying pieces of this work: `no_workspace_agent_mode.md` §5.1 (op count is the cost — the virtual backend's scoped metadata index) and `cloud_collaboration_model.md` §4 (one `Depth: infinity` PROPFIND per turn boundary).
**Origin:** user proposal — an LLM turn is conversation JSON in, bigger conversation JSON out; so agents can be a Deployment, not pinned pods.
**Related:** `docs/go_rewrite.md` (names this flip; two sketches inverted by evidence — §5.1, §5.2), `docs/features/job_execution_lease.md` (shipped substrate), `docs/features/session_reliability_and_transport_simplification.md` (P5/P6 converge here; **§5.3.2's P5 blocker is satisfied, but P5 and P6 remain incomplete**), `docs/features/worker_runtime_strategy.md` (no-new-runtime decision — this is a driver/deployment change), `docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md` (D3).

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

**Reliability — a bug-class graveyard.** Structural pod-pinning bugs dissolve rather than get fixed: the stale-agent-detector class, drain-strips-workspace, the exit-137 wedge, idle-reap wipes, the fresh-vs-resume seeding asymmetry (one lane: every claim loads everything), and the dead-pod `awaiting_user` trap observed live during the S1 build — a pinned thread whose pod died mid-life is unreachable by both `/input` ("agent unreachable") and `/resume` (409 for any status but 'ended') until something flips its status; a queue-lane thread cannot enter it (no pod to die — the next input just enqueues). Two replica-unsafe spots were fixed as part of the build: the per-turn input lock dict (`main.py:30385` — "Single-instance orchestrator" was stale; dev runs 2 replicas) and `_threads_suspending`; the bench-sweeper double-submission race was the in-repo proof of unclaimed work under 2 replicas. The design also forced closure of the pre-S1 zombie-writer hole: final `thread_messages` reconciliation and queue completion now share the exact lease fence (§5.2).

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
- **`threads.events_epoch`** — the **client-visible writer-generation**, stable across ordinary clean claims and bumped only at an explicit system boundary that invalidates or replaces a writer, including reaper steal, rewind, terminal interrupt/Force-End, or the claim-bound permission-retirement rotation added in M3. A clean handoff (release → claim by a different pod) does **not** bump: with seq DB-allocated and monotonic (§5.3.2), a clean cross-pod handoff is *invisible to the client* — which matters because cross-pod re-claims are routine exactly at the utilization this design is sold on; "bump on writer change" (v2) would have partially re-imported the per-turn cascade under load. An already-rotated path must not re-bump. `leased_by` stays diagnostics-only.

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

The v2 finding was the decisive pre-S1 blocker: `events_epoch` then bumped on
every runtime attach, so per-turn attach would have caused ~2.0–2.2 s of
dead-epoch polling followed by `gone_beyond_horizon` → **IndexedDB thread-cache
wipe → full transcript refetch** → SSE reopen on every turn. **S1 shipped the
five-part contract below before stateless traffic opened.** The epoch blocker
for session-reliability P5 is therefore satisfied; P5's on-demand-SSE work is
still a separate, incomplete phase.

1. **Epoch stays stable across ordinary clean claims** (§5.2). It rotates only at an explicit writer-invalidating system boundary, including reaper steal, rewind, terminal interrupt/Force-End, or claim-bound permission retirement. Clean cross-pod handoffs don't bump *and stay client-invisible* because:
2. **Seq allocation moves DB-side** — monotonic *across* claims within an epoch. The former in-process `_next_seq` reset was what forced epoch-per-attach. **Implemented (S1) as DB-*seeded*, not per-event-allocated**: attach seeds from `GREATEST(threads.events_seq_hwm, MAX(seq))` — `events_seq_hwm` (migration 0116) is maintained in the writer's flush statement and survives retention pruning of the rows themselves (a bare MAX(seq) seed would drop below cached client cursors after the pruner runs); gap-free, zero per-event cost, sync stamping preserved. Block allocation on the threads row (the alternative above) was not needed at S1 concurrency.
3. **The journal writer is fenced**: the batch INSERT carries `WHERE threads.events_epoch = $claimed AND run_queue.lease_token = $token` (CTE); zero rows = lost lease, terminal-fail loudly. This shipped in S1, so a stale writer can no longer keep inserting into a dead epoch.
4. **A system-writer class exists for non-stream kinds.** The fence rule cannot mean "only the lease holder may ever write the journal": the reaper (`turn.interrupted`, `turn.parked`), claim-bound permission retirement, and outbox workers (`title.updated`) may write client-visible frames without holding a live turn lease. Designated non-stream kinds append under a transactionally selected current/new epoch; token/stream kinds stay lease-fenced. Durable scalar-control acknowledgements are different: the exact applying owner writes their journal receipt so it cannot collide with that owner's allocator.
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

**Final-memory outbox built (2026-08-14).** Each settled stateless turn now
mints one `producer_kind='session_turn'` / `final_memory_extraction` effect in
the same fenced transaction as its final transcript. The immutable
`turn_execution_id`, transcript boundary and destination survive executor loss;
an independent always-on orchestrator drain owns retry/pruning, and a vector-DB
execution receipt commits atomically with every durable memory mutation. A
fenced loser mints nothing. This closes the final 0–4-turn tail-loss gap, but is
not a claim that every background item listed above has moved to the outbox.

#### 5.3.6 Session semantics that must survive the model change

- **Mid-turn settings**: built as durable first-class thread scalars plus the
  owner-fenced 0119–0121 control inbox. `mode.set` and `narration.set` persist,
  the exact owner applies them in commit order and writes the journal receipt,
  and the permission gate reconciles the durable scalar before deciding the
  next tool call (including expiry of an incompatible pending card). Resume
  therefore cannot revert to a process-local value, and “next turn only” is not
  the contract.
- **Permission gates**: rows are DB-backed. The original release-at-gate target
  is not the implemented active-wait shape: while a healthy stateless turn is
  waiting, its independent heartbeat continues renewing the exact lease. New
  requests capture that accepted lease token under the thread→queue lock. At a
  proven writer-exclusive loss/rotation/Force-End boundary, pending rows from
  the old token retire as `expired` with one linked `permission.resolved`
  receipt; they are never re-armed on a successor that cannot reconstruct the
  tool call. Approval and retirement share one row-CAS race. There is
  deliberately **no blind `expires_at` sweeper**; legacy NULL-token rows move
  only at a proven boundary. M4 removed the long-lived permission LISTEN so
  control+interrupt listeners cannot starve the heartbeat in the supported
  three-connection agent pool.
- **Steal UX contract** (was undefined): the reaper writes `turn.interrupted` so the client renders a state instead of ≤ ~105 s of dead air indistinguishable from a long tool call; the partially-streamed answer **visibly vanishes and regenerates** (dead-epoch deltas are never replayed). S1 added an automatic re-run lane where the pre-S1 pinned behavior simply lost the turn and required user resend, so retry remains constrained for side-effectful tools: re-run at most up to the first tool call unless the call is covered by dedup (MCP writes like `create_job` are the sharp case).
- **Media fidelity**: `_serialize_message_row` flattens list content (images dropped) and restore excludes `thinking`. Per-turn reload makes this the steady state, and **byte-stable re-rendering (§5.6) is impossible for affected turns until fixed**. Either structured-content storage lands with S1, or the degraded option ships *with a visible UI notice* on media past their turn — silent degradation is not an option.
- **Presence**: re-homed in migration 0125 as a bounded database-clock
  attached-client TTL renewed by the owner-gated SSE stream. Stateless natural
  pause and permission expiry consult that durable oracle; pinned
  WebSocket/subscriber behavior remains unchanged.
- **Hard interrupt**: built as the 0127–0129 exact-turn request/receipt path.
  Owner-gated REST admits only against the exact open turn and live lease; the
  executor watcher signals that turn and writes the linked `interrupt.ack`,
  with reaper/terminal recovery settling admitted owner-loss cases. A new
  no-live-gate request returns 409 before INSERT; it is not retargeted and does
  not leave a dangling row. Pinned sessions have correlated REST parity; the
  uncorrelated control-WS verb remains compatibility-only.
- **Metrics**: the heartbeat's aux-health/RSS payload moves to the lease heartbeat / release report, or admin badges go dark.
- Small state promotions: SessionTaskManager todos, the interval extraction
  cursor and citation anchors are persisted; sandbox undo uses the durable
  control/Git turn→SHA ledger. Read-before-write stamps remain deliberately
  claim-local, forcing a fresh read after handoff (OQ §10.4 remains only for a
  stronger persisted-stamp design).

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

**Status: design 2026-08-08; scope corrected 2026-08-10 — no longer
admission-blocking (see below); step 1 shipped 2026-08-10 (`2f5307f0`);
adversarially reviewed 2026-08-10/11 and folded in here 2026-08-12. Steps 2–4
are already on `develop`; step 5 is built in the local, unpushed follow-on
milestone commits as of 2026-08-13; step 6 remains unshipped/default-off.**
Evidence base:
`docs/research/stateless_agents/completion_path_side_effect_inventory.md`.
Review of record: `docs/research/stateless_agents/gate3_adversarial_review.md`
(four independent critics; every seeded candidate confirmed; 10 blockers). That
file was authoritative over this section until this fold; **from 2026-08-12 this
section is authoritative again**, and the review file is retained as the
evidence and reasoning behind each change.

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
prevents recovery. Treat Gate 3 as repairing a fragile path, not as paying a
stateless tax.

**Three of them are now fixed, ahead of the protocol** — which is the point of
the rollout's step 1. `completed_at IS NULL` and S19's dispatcher-invisibility
window closed together via the Class A merge, and the duplicate-critic race
closed via the 0132 index (`2f5307f0`). The review then found a fourth and it
is also fixed: the agent used to heartbeat `ready` **before** reporting
completion, so `recover_orphaned_jobs` — which pauses any `processing` job whose
agent says ready, with no `current_job_id` check and no grace — could pause the
job mid-report. In the narrow variant the completion report is then discarded
with a 400 (its rescue path is `failed`-only) and a finished job is re-run; in
the wider variant the ready state spans the *entire* handler, so a sweep tick
minutes in re-dispatches while the handler still runs and the status write later
overwrites `paused`→`completed` on top of a second execution. That is a
mechanical explanation for the "job ran twice" symptom class, and the fix was a
two-line reordering (`ca25f98f`, 2026-08-11).

What remains genuinely blocked on the protocol is the workspace leak (S36) and
the crash-recovery story around it — the one-way-door problem itself.

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

Added after the adversarial review — each is a confirmed interleaving, with a
one-evening reproduction recipe implicit in its wording:

- **Park a command after the `reviewing` write for >30 min with the
  verification sweeper live, then unpark.** Exactly one critic exists
  afterwards and the target's status is coherent with whatever the human
  decided.
- **Park between the loop barrier claim and the advance for >10 min with the
  loop sweeper live.** Exactly one stage-N+1 spawn.
- **Complete a job with the teardown group blocked.** Exactly one snapshot
  object; the reconciler either waited or resumed the same effect rows — never
  raced them.
- **Kill the agent between the 202 and the queue release.** The unit is not
  requeued and no successor claims it.
- **Cancel between accept and finalize.** The command supersedes; no Class C
  effect runs.
- **Report `job_complete`, then crash so the error handler reports under the
  same token.** First-wins: the job stays completed.
- **Resume a job whose terminal command is still finalizing.** Refused (409),
  not claimed — and the workspace survives.
- **Kill the orchestrator handler mid-terminal-completion on the stateless
  lane.** Consume-or-benign-re-report; never park.

**"A pinned job's completion is behaviorally identical to today's" is not
testable as written**, because step 4 changes the observable order by design.
Split it: *contract* (assert) — same terminal status for identical payload and
DB state across the full stop matrix, same response shape to the agent, the
same **set** of side effects order-free (assertable directly from the effects
table), same guard responses, and p95 handler latency within a stated envelope.
*Accepted and documented* — intermediate status timeline, `completed_at` skew
bounded by delivery duration, the new command/effect rows, the new 4xx classes,
and delivery-failure parking. Anything observable that is in neither list fails
the gate; that closes the loophole "identical" leaves open.

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

**Then the whole section was attacked by four independent adversarial critics
(2026-08-10/11) and ten more blockers came out.** The protocol core — durable
command row, accept fence, background finalizer, effects-as-rows — survived all
four; nobody found a reason to change its direction. What did not survive was
the *periphery*. The corrections are folded into the decisions below; the
headline reframe is worth stating once, up front:

**Decision (6) integrated with three sweeps. The orchestrator has eleven
job-status-keyed autonomous actors, and they need four different treatments —
not one predicate.** An exclusion view is the right tool for exactly one of the
four classes:

1. **Re-dispatch rescuers** — `recover_orphaned_jobs`,
   `recover_expired_lease_jobs`, the stale-agent detector, plus the
   llm_outage / infra_transient / vm_upgrade-expiry redispatch sweeps for
   *pause* commands. The exclusion view + routing table works as designed.
2. **Status-consuming effect synthesizers** — `unstick_reviewing_parents` and
   its wallclock arm, the project-loop sweeper's heal, the lifecycle
   workspace/VM reaper, the session-wake status arm. Exclusion alone is
   insufficient **and wrong in one direction**: these need *bidirectional
   deference*. The sweep stands down while the command's lease is live; but
   once it has legitimately fired (expired or parked — it *is* the operator
   fallback), the **finalizer's own effects must CAS on world state and mark
   themselves `superseded` on miss**. The sweep checking the command row is
   only half the contract.
3. **The queue-side rescuer** — the run_queue reaper. Not fixable by any
   jobs-side predicate; fixed at accept (B4 below).
4. **Non-sweep concurrent verbs** — cancel, dispatcher preemption, drain,
   human approve. Fixed by a status predicate on the finalizer's *own* Class A
   write (B10 below). No view can provide this.

**The CI invariant generalizes accordingly.** It was scoped to `processing`;
every collision the review found occurs in `reviewing`, terminal, `paused` or
`waiting`. Correct form: *every job with an unfinalized command row is owned by
exactly one actor — a live agent lease, a live finalizer lease, or exactly one
routed sweep — whatever its status.*

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

    origin               TEXT NOT NULL DEFAULT 'agent',  -- agent | operator
    requested_by         TEXT NOT NULL,      -- actor, 0119's house pattern

    state                TEXT NOT NULL DEFAULT 'pending',
    attempts             INT  NOT NULL DEFAULT 0,
    max_attempts         INT  NOT NULL DEFAULT 5,
    run_after            TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at     TIMESTAMPTZ,        -- renewable; the liveness signal
    deadline_at          TIMESTAMPTZ NOT NULL,  -- absolute cap, see (3)
    finalizing_by        TEXT,               -- diagnostics only
    code_version         TEXT NOT NULL,      -- gates recovery across deploys

    outcome              JSONB,
    finalized_at         TIMESTAMPTZ,
    error_code           TEXT,

    CONSTRAINT uq_job_completion_seq    UNIQUE (job_id, report_seq),
    CONSTRAINT uq_job_completion_client UNIQUE (job_id, client_report_id),

    -- Scoped to agent-origin rows. approve_job and both Mode-A verdict paths
    -- already run the same terminal effect set with NO agent identity and no
    -- lease (4 call sites of apply_terminal_job_side_effects) — the original
    -- unconditional XOR made those rows unrepresentable, which would have left
    -- the approve-path crash windows orphanable while claiming to fix them.
    CONSTRAINT job_completion_fence_exactly_one CHECK (
        (origin = 'operator'
         AND accepted_lease_token IS NULL AND accepted_agent_id IS NULL)
     OR (origin <> 'operator'
         AND ((accepted_lease_token IS NOT NULL AND accepted_agent_id IS NULL)
           OR (accepted_lease_token IS NULL AND accepted_agent_id IS NOT NULL)))),

    -- 0119's bidirectional style: half-written states are unrepresentable in
    -- BOTH directions. The original constrained only 'done', which admitted a
    -- pending row carrying an outcome and a parked row carrying finalized_at.
    CONSTRAINT job_completion_state_value CHECK (state IN (
        'pending', 'finalizing', 'done', 'parked', 'superseded',
        'force_resolved')),
    CONSTRAINT job_completion_terminal_shape CHECK (
        (state IN ('pending', 'finalizing')
         AND outcome IS NULL AND finalized_at IS NULL AND error_code IS NULL)
     OR (state = 'done'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL
         AND error_code IS NULL)
     OR (state = 'force_resolved'
         AND outcome IS NOT NULL AND finalized_at IS NOT NULL)
     OR (state = 'superseded'
         AND finalized_at IS NOT NULL AND outcome IS NOT NULL)
     OR (state = 'parked'
         AND error_code IS NOT NULL
         AND outcome IS NULL AND finalized_at IS NULL))
);

-- The finalizer drain. run_queue's own idx_run_queue_claim precedent; churn is
-- BOUNDED here (a row transits the predicate once, retries capped by
-- max_attempts), unlike the mutable-jobs-status case rejected for the critic
-- index. Without it the drain seq-scans an unboundedly growing table.
CREATE INDEX idx_job_completion_drain
    ON job_completion_commands (run_after)
    WHERE state IN ('pending', 'finalizing');

-- The sweeps' NOT EXISTS is already served by the (job_id, …) prefix of
-- uq_job_completion_seq — deliberately no extra index for it.

-- Progress marker: ONE ROW PER EFFECT, never a JSONB log on the command.
-- POLYMORPHIC producer (run_queue's precedent) so the session lane shares this
-- substrate rather than growing a second one — see "shared with the session
-- lane" below. Deliberately NO foreign key, for the same reason run_queue has
-- none: the effect log outlives and predates its referents across kinds. The
-- cost is that retention is explicit rather than ON DELETE CASCADE.
CREATE TABLE completion_effects (
    producer_kind TEXT NOT NULL,         -- 'job_completion' | 'session_turn'
    -- job_completion: the command id. session_turn: a turn_execution_id minted
    -- INSIDE the fenced transaction that makes the turn durable — NOT the
    -- unit_id. A session unit's unit_id IS the thread_id (0115a: one durable
    -- row per unit, reused for the thread's life), so keying on it collides
    -- across every turn of a thread: with ON CONFLICT DO NOTHING every effect
    -- after turn 1 is silently swallowed; with a plain INSERT it is a 23505 per
    -- turn. Minting inside the fenced persist also makes stolen-attempt
    -- disposition structural — a fenced-out attempt never commits its effect
    -- rows, so they neither orphan nor double: they never exist.
    producer_id   UUID NOT NULL,
    scope_id      UUID,                  -- thread/job for joins; NOT the key
    effect_name   TEXT NOT NULL,         -- STABLE NAME, never an ordinal
    effect_group  TEXT NOT NULL,         -- independently retryable unit, see (4)
    state         TEXT NOT NULL DEFAULT 'pending',
    attempts      INT  NOT NULL DEFAULT 0,
    max_attempts  INT  NOT NULL DEFAULT 5,   -- per-GROUP budget lives here…
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- …and so does backoff
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- age alarms + pruning
    intent_at     TIMESTAMPTZ,           -- written BEFORE the external call
    -- Shorter than the CLAIM that owns this effect, not the producer's lease:
    -- a session turn's queue lease is gone by the time the finalizer runs.
    complete_by   TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. captured k8s UIDs
    error_code    TEXT,
    PRIMARY KEY (producer_kind, producer_id, effect_name)
);
```

**No partial index over `completion_effects.state`.** The command table can
carry one (bounded churn); this table cannot. Session producers dominate it —
roughly 3–4 effects per turn against workers' once-per-genuine-stop — so a
`WHERE state='pending'` predicate would have ~10⁴–10⁵ entries per day
transiting it and would make *every* state flip non-HOT, which is the table's
dominant write. Keep effect rows close to single-transition (fold `intent_at`
into the INSERT) and index only what the drain actually needs.

**Retention is owned, not asserted.** The reaper prunes; effects delete before
their command (the command id is the enumeration key); session effects are
age-pruned from `created_at` on terminal state; a small orphan sweep catches
effects whose producer vanished — necessary because `job_completion_commands`
CASCADEs on job deletion (a live endpoint) while the effects table
deliberately has no FK. All deletes are LIMIT-batched in short transactions:
one large DELETE is exactly the long-lived transaction that holds back
dead-tuple reclamation for this table and `run_queue` alike. Failures outlive
successes (River's 7 days vs 24 hours is the precedent).

**`detail` holds fixed-cardinality correctness capture only** — UIDs, SHAs,
status codes, counts — with an app-side cap (~8 kB, truncate-and-count).
Per-file inventories stay in their existing stashes (`context.loop_cloud_delivery`,
the cloud-diff rows); S15's unbounded walk would otherwise reintroduce the
TOAST problem this design just evicted from the command row.

Command states are `pending | finalizing | done | parked | superseded |
force_resolved` — **six, and CHECK-enforced**, not app-validated. The earlier
"no CHECK, matching `run_queue`'s convention" borrowed the wrong precedent:
0115a skipped the CHECK to avoid a NOT VALID/VALIDATE split on an existing hot
table, whereas this table is new and empty. The review also found the state
vocabulary internally inconsistent — `force_resolved` was assigned by the
liveness section but missing from the list and from the sweep predicate, and
nothing anywhere *set* `superseded`. It is set by **the finalizer, at winner
selection**, and by the day-one safety-net sweep when it reconciles a stale
command (see the rollback rules).

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

  **The id is minted randomly ONCE and persisted with its payload — never
  re-derived.** "Derives from the stop" was a trap: the `job_complete` freeze
  embeds `datetime.now()` and `head_commit`, so a crash-resume that re-derives
  the payload produces a *different* digest, and a deterministic id would then
  false-422 an honest retry — fail-closed on a legitimate completion. Mint the
  UUID once, persist id **and the exact payload** together in the
  freeze/checkpoint, and resend that payload verbatim; any re-derivation is a
  new stop with a new id. The digest is computed **server-side** over canonical
  JSON excluding transport/fence fields (an agent-computed digest false-422s
  across serializer versions).

  **The full retry matrix**, because three states were unspecified:
  `pending`/`finalizing` ⇒ 409; `done` ⇒ stored outcome + `Idempotent-Replayed`;
  **`parked` ⇒ 202 still-pending WITHOUT a retry promise** (409's documented
  contract is "retry, it will succeed", which is false for a command parked
  awaiting an operator for days); **`superseded` ⇒ terminal, naming the winning
  `report_seq`** (it has no outcome of its own to return);
  **`force_resolved` ⇒ terminal, reporting which effects were abandoned**.
- **Fence** — `accepted_lease_token` XOR `accepted_agent_id` for agent-origin
  rows, the same discriminated pair `thread_control_requests` uses, because the
  two lanes prove ownership differently and neither should be forced into the
  other's shape. Checked *at accept*, which is what makes it a real fence: the
  report is admitted or rejected in one short transaction, not after 1046 lines.

  **The pinned arm needed a credential that did not exist.** `JobCompleteRequest`
  carries no agent identity and the endpoint's auth is a fleet-shared
  `X-Internal-Key` — it proves "an agent", never "this agent" — so
  `accepted_agent_id` could only be copied from `jobs.assigned_agent_id`, which
  a sweep clears asynchronously. That made the pinned fence either vacuous or
  fail-closed on a live race. Three changes: `JobCompleteRequest` gains an
  optional `agent_id` (pydantic v2 ignores unknown fields, so agent-first is
  skew-safe) compared at accept; **the accept INSERT becomes the FIRST database
  write of `/complete`, before any guard**, so a routed sweep sees the command
  and stands off — making the race structurally impossible rather than merely
  narrow; and the underlying ordering bug was fixed at the source
  (`ca25f98f`, 2026-08-11) — the agent now reports completion *before*
  heartbeating ready. 0119's equivalent works only because the owner registers
  its own UUID in advance and clears it itself; the jobs analogue is cleared by
  a sweep, which is why it could not be copied.
- **Order** — `report_seq`, allocated by incrementing `jobs.completion_seq_hwm`
  under the jobs-row lock, never from an IDENTITY. This is the control inbox's
  rule and it exists for a reason: an IDENTITY value can be allocated by a later
  transaction that commits first, so it does not order commits.

  **Authority is finalization ORDER, not "highest seq wins".** The original
  rule manufactured a regression: a driver reports `job_complete` (seq N), then
  crashes during teardown, and the outer error handler reports the failure
  (seq N+1) under the *same still-valid token* — highest-wins crowns the error
  report and marks completed work `failed`, where today's status guard gives
  first-wins. Correct rule: **commands finalize in `report_seq` order, each
  applying against the state its predecessors produced.** The existing
  already-successful backstop in `determine_job_status` ("ignoring a coincident
  error on an already-successful job") then absorbs the trailing error as a
  no-op, preserving today's semantics for free. `report_seq` keeps its real
  jobs — ordering sequential pinned reports (where the fence value repeats
  across a pause→re-dispatch-to-the-same-agent lifecycle and cannot order
  them), commit-ordered audit, and deterministic tie-break.

  **Lock order is binding and inverts the 0119 precedent.** Any transaction
  locking both a `run_queue` row and its `jobs` row takes the **run_queue row
  first**. The claim's shape forces it — its CTE discovers `unit_id` from the
  queue scan, so jobs-first is impossible there — while a faithful copy of
  0119's parent-first ordering would make accept take jobs-then-queue and
  deadlock against every concurrent claim (40P01, ~1 s victim, landing on
  `/complete`, which does not retry). So accept validates the fence
  (`FOR SHARE` on run_queue) *before* taking the jobs row for the hwm bump.
  "Extend the proven pattern" explicitly does **not** apply to lock order —
  0119 is safe parent-first only because `claim_unit` never locks `threads`.

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

**And it must CAS on the expected entry status too.** The term guard defends
against a resurrected finalizer; it does nothing against a *legitimate*
concurrent writer, and background finalization widens that window from
milliseconds to seconds-or-more. Concretely: the report is accepted (202), the
user cancels or the dispatcher preempts, and the finalizer then commits
`completed` over it and runs merge/spawn effects for a job the user cancelled —
the inventory's destructive-interleaving #4 surviving the redesign verbatim. So
the write carries `AND status = ANY($expected_entry_statuses)`, and a miss
routes the command to `superseded`/`parked` rather than overwriting. Condition
4's lease-renewal cancel discovery covers only *mid-run* cancels; this closes
the post-report window.
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
Five corrections make it safe — the first two below, then the claim barrier, the
accept-side queue terminalization, and the bidirectional deference the effect
synthesizers need:

**Key on a live lease, not on presence — and cover `parked`.** The predicate is
`AND NOT EXISTS (… state IN ('pending','finalizing','parked') AND (lease_expires_at > now() OR state = 'parked'))`.
The first draft named only `pending|finalizing`, which left a hole the review
caught: a command can park **before** the status write (a delivery group
exhausting its attempts is this design's own dialect), leaving the job
`processing` with its agent gone and the exclusion FALSE — so the rescuer
pauses and re-dispatches a job whose half-applied command an operator is
holding. Parked rows are excluded from re-dispatch and routed to **alert-only**;
they already *are* the operator worklist.
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

**A pending terminal command must block a new claim of the same unit.** Nothing
in the first draft said so, and the review confirmed the consequence by tracing
code: terminal report accepted (202) → finalizer backlogged (its normal state)
→ the user resumes the `failed` job (`resume_job` explicitly allows `failed`;
`queue_job_for_resume` is `WHERE id = $1` with **no status predicate**) → a new
pod claims and runs on the same PVC → the finalizer then drains the *old*
command's Class C and **destroys the workspace under the running successor**,
with the "final" snapshot capturing half-mutated round-two state. The UID
precondition does not help — same object, legitimately captured UID. Three
layers, cheapest first: the lane-aware resume verb refuses with 409
("completion finalizing") while the newest command is live-or-within-deadline,
and on an expired lease kicks the resume-the-finalizer arm before proceeding;
the claim predicate excludes units whose job carries such a command (the
liveness answer transfers verbatim — live lease OR within `deadline_at`, so
"blocked forever" degrades to bounded wait); and S36 re-checks in its own
transaction that no higher `report_seq` exists before deleting — the authority
rule applied at the effect site, where it currently is not.

**Accept must terminal-ize the run_queue unit in the same transaction.** The
queue reaper is the one rescuer no jobs-side predicate can reach. Without this:
the agent reports terminal (202), dies before `complete_unit`, its lease
expires, the reaper requeues, and a successor **re-runs the final batch** —
paid tokens and external side effects — inside a workspace the finalizer may
already be archiving, then files a second report under a valid fence. So a
terminal-report accept runs `complete_unit` (or a terminal park) under the
report's own token, in the accept transaction; a fence failure rejects the
report.

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

**The effect synthesizers need deference in both directions** (class 2 of the
four). Three concrete collisions the review confirmed against code, each fixed
the same way — the sweep stands down while the command's lease is live, and
the finalizer's *own* effect CASes on world state and marks itself
`superseded` on a miss:

- **`unstick_reviewing_parents` vs the critic spawn.** Class B writes
  `reviewing`; the finalizer parks before Class C spawns the critic; at 30
  minutes the watchdog fires — legitimately, its predicate is vacuously
  satisfied with zero critic children — hands the target to a human, who
  decides. The resumed finalizer then spawns a critic anyway (the spawn path
  has **no parent-status predicate**), whose verdict is applied by writers that
  also have none: a `returned` verdict re-queues a human-approved, merged,
  torn-down job; an `approved` verdict overrides a human rejection. **The 0132
  index does not help** — this interleaving contains exactly one INSERT; the
  index closes only the crash-after-INSERT-before-marker case.
- **The project-loop heal vs the loop advance.** The heal's guard is
  emptiness + a 600 s age gate, and that gate's own code comment records the
  live incident it was built for (duplicate iter-14 critics). A finalizer that
  parks between the barrier claim and the advance makes that window routine,
  so the heal re-arms a barrier the finalizer already claimed and both spawn
  stage N+1. **Simplest fix, and preferred: move the barrier claim out of
  Class B into the same Class C transaction as the spawn** — nothing in the
  triage rationale requires the claim to precede the status write, since loop
  jobs resolve `completed` regardless.
- **The lifecycle reconciler vs Class C teardown.** It runs under a *different*
  leadership domain than the finalizer, so both run concurrently by
  construction; terminal statuses are reapable with no grace and `reviewing` is
  idle-reapable behind only a live-child guard — which is false in exactly the
  Class B→Class C window. Result: double snapshot-then-delete to the same key,
  or a parent pod reaped under a not-yet-spawned critic. It cannot simply be
  excluded — it is decision (8)'s leak backstop — so it is **routed**: skip
  while the lease is live; on expiry, *resume the same UID-keyed teardown
  effect rows* rather than run a parallel implementation. Note the VM path
  already passes `preconditions: {uid}`; pods and PVCs are the gap, and one
  shared implementation in `container_provisioner` serves both actors.

**One invariant, testable in CI:** every job carrying an unfinalized command
row is owned by exactly one actor — a live agent lease, a live finalizer lease,
or exactly one routed sweep — **whatever its status**. (Scoping this to
`processing`, as the first draft did, misses every collision above: they occur
in `reviewing`, terminal, `paused` and `waiting`.) A job matched by nothing is
the stuck namespace, rebuilt.

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
2. **0140+** — the command and effect tables, `jobs.completion_seq_hwm`, the
   leader lease row, and the sweep predicate view. Dead schema; zero behaviour
   change. The worker driver needed no schema; S2's close-out later took 0133
   from that range, leaving 0134–0139 free.
3. Accept writes the command for **both** lanes; the finalizer runs **inline**
   for both. Behaviour is identical to today, but every report is now durably
   recorded. This is the step that carries real risk — soak it behind a flag.
4. The status write moves after **Class B and the delivery effects only** (not
   after all 37 — see the step-0 triage), inside the inline finalizer, together
   with (6)'s sweep predicates. Pinned gains crash recovery here.
5. Background finalizer enabled for stateless units only.
6. Stateless worker admission opens.

**Step-4 delivery-set decision (2026-08-13).** The exact product deliveries
that gate a canonical terminal status (`completed`, `failed` or `cancelled`)
are S15 `loop_project_cloud_delivery`, S26 `subjob_output_graft`, and S33
`terminal_merge_change_record`. Their stable effect groups are respectively
`delivery`, `subjob_graft`, and `terminal_delivery`; group policy, rather than
call-site conditionals, carries the gate. S16 `mode_a_diff_capture` remains in
the already-shipped `delivery` group for resumable-row compatibility, but is a
status-decision capture rather than a product delivery and already runs before
S17. S27's transactional verdict core remains Class B and runs before a
reordered terminal write; its external follow-up remains Class C/D. M3 moved
S32's barrier and successor materialization into one atomic Class C effect, so
it remains after status. `reviewing`, `pending_review`, `paused` and waiting
states are not terminal and retain their existing order. Each accepted command
persists whether this reorder applies so a restart or configuration flip cannot
change the ordering of in-flight work.

**Gate 3 step-5 design (2026-08-13).** B4 terminalizes the exact stateless
`worker_batch` queue generation in the same transaction that accepts its
completion command, before the fresh worker response becomes HTTP 202. The
existing singleton finalizer drain owns everything after that response and its
two-second grace; pinned commands remain inline, and session units never call
`/complete`. This is routing over the shipped command/effect engine, not a new
background executor.

The worker uses the exact accepted command ID as its handoff key. Once it
observes B4 acceptance—after a successful report or through the renewal/B4
lookup when the HTTP result is ambiguous—it pins that exact command ID, closes
shell admission, and drains and retires the original generic RemoteBackend
(including already-admitted resource/SFTP calls), scrubs
claim-local credentials, graph, tools, datasources and checkpoint state, and
retains only an immutable cleanup capability fenced to the exact job,
workspace generation, runtime incarnation and worker token. It polls the
existing B4 lookup/renewal channel; it does not invent another completion
channel. A stored `done` reads `new_status`; `superseded` reads
`observed_status|observed_job_status`; and `force_resolved` reads
`terminal_status`. Only a decoded status of `completed`, `failed` or
`cancelled` triggers terminal tmux cleanup once. Every other decoded status
preserves the reattachable session. Park, deadline, lookup loss, cancellation
or driver crash also preserve it and issue no queue complete, release or
requeue verb; the durable command plus UID-fenced S36/lifecycle reconciliation
are the backstop. DB-derived
`run_after` and live-lease horizons bound the next poll; PostgreSQL
`deadline_at` is the absolute local hold bound, never a fabricated client
clock.

The rollback distinction is now executable: disabling worker admission does
not disable a pod launched as a stateless executor. Close admission first,
keep that Deployment claiming until existing `worker_batch` rows are terminal,
then scale it down. The independent monitor samples the oldest runnable worker
unit using one DB clock and `GREATEST(queued_at, run_after)` and emits the fixed
key `run_queue.worker_batch.oldest_runnable` at 300 seconds, even when
completion commands are off; completion-table alarms remain commands-gated.
The literal acceptance wording “kill between 202 and queue release” has no
post-step-5 window because B4 queue closure commits before the 202. Its honest
fault test is a kill after 202/B4 closure while finalization is held: the queue
must remain `done`, its token unchanged, and no successor may claim it.

**Revert rules, per step — "independently revertible" was asserted, not
demonstrated.** Step 1's image rollback cannot remove the 0132 index, and
pre-step-1 code has no `23505` handler, so the closed race re-manifests as an
unhandled 500 mid-`/complete` — fail-loud and acceptable, but name it. Steps
2/3 rolled back with rows already written: old code ignores the tables and
replays via the status guard, but commands stranded `pending` across the
rollback get found later by step 4's sweep, for jobs old code long since
re-completed — so the safety net's "reconcile" **must mean mark-superseded,
never execute**; executing a stale S36 months late is the workspace-kill above
through the back door. Step 4 inherits §5.4.1's discipline explicitly: **drain,
or flag off the reordering, before any orchestrator rollback during the soak**
— otherwise old sweeps, which know nothing of the routing table, see
"processing + agent gone" and re-dispatch mid-finalization. Step 5's max-age
alarm must live in the **sweep/monitoring path, not the finalizer loop**, or
disabling the finalizer disables the alarm that would report it. Step 6
re-closed means **admission-off ≠ executor-off**: stop enqueueing, keep the
claim loop until in-flight units drain terminal — a lane flip cannot rescue
them, because rotation never stashes `freeze_data` and pinned agents cannot
read the fenced PG checkpoint, so re-dispatch would silently restart from
phase 0. Add a stateless queued-age alarm too: scaling the worker Deployment
to zero leaves queued units with no claimant and no reaper coverage.

**One live-fuse constraint on step 3.** `report_completion` uses a 60 s client
timeout and starlette cancels the handler on client disconnect, so today's
completion pipeline already carries an undocumented upper bound whose failure
mode is duplicate execution. Step 3 must therefore split accept from finalize
so a cancellation can only land *between* them, and must ship the
finalizer-resume drain **with** step 3 rather than at steps 4/5 — otherwise
cancellation mints pending commands that nothing drains.

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

##### The completion-effects substrate is shared with the session lane (built 2026-08-14)

S2 correctly stopped before building a generic background-work outbox for
sessions: making it safe required enqueueing the effect inside the authoritative
final message-persist transaction. Gate 3 then supplied the polymorphic
`completion_effects` table, and session-hardening M1 built its first session
producer. Every settled stateless turn now commits a stable
`turn_execution_id`, immutable transcript boundary/destination and one
`final_memory_extraction` obligation inside the fenced persist transaction. An
always-on orchestrator drain settles it independently, with vector-side
destination receipts making durable mutations exactly-once. A fenced loser
mints no effect. This replaces the old final-memory lease-hold/teardown gap; it
does not claim that every session background item is now outboxed.

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

Current producer status: turn-end/final-memory capture is built. Exact
`llm_requests` archival and post-callback Git turn→SHA ordering remain candidate
producers on the same substrate rather than blockers to its existence.

**Migration numbering:** S1 used 0115–0121 (with `0115a_run_queue.sql` renamed
during the 2026-08-12 develop merge — develop had independently taken 0115;
the rename is byte-identical so recorded checksums stay valid); S2 used
**0122–0129** plus **0133** at close-out; Gate 3 step 1 used **0130–0132**;
**0134–0139 remain free**; Gate 3 steps 2–4 used **0140–0144**, and step 5
needed no migration. Session hardening then used app **0145–0155** and vector
**0019** for the final-memory/permission/wake work. The current heads are app
**0155** and vector **0019**; the next migrations are **0156** and **0020**.
Range allocation itself remains necessary: the earlier dev-cluster wedge came
from two tracks numbering independently against one shared database.

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

**S3 single-pool experiment ruling (updated 2026-08-11): use it for the guarded local proof, but do not ship it as the production topology yet.** The shared executor now polls sessions first and has run real `worker_batch` traffic in k3d, proving functional coexistence. It still has no pool-wide interactive-capacity reservation: a pod-local "leave N slots free" rule cannot preserve capacity through a rollout, pod loss or a temporarily unavailable executor. The Deployment also intentionally lacks the VM-mesh sidecar needed by one worker capability class, which is why VM jobs remain pinned. Keep the two-Deployment production design unless a coordinator-visible executor-presence/capacity contract enforces the reserve across failure states, capability-aware claims exist, and the loaded claim-wait benchmark passes. The current proof establishes correctness, not production availability or performance parity.

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
4. ~~**Epoch/seq/fence redesign** (§5.3.2) before any stateless session traffic
   and before P5.~~ **Satisfied by S1.** P5 must preserve the shipped stable-
   across-clean-claims contract and still build its own `/events/head`, activity
   wake and idle-close machinery.
5. **Path-A resume-compaction persistence** (§5.3.3) — else per-claim aux-LLM re-summarization.
6. **Freeze-type registry consolidation** into `src/shared/` (§5.4.1) — else version-skew phantom-completes loop jobs.
7. **Control-verb transport for S1 sessions**: the required precursor subset is
   shipped as owner-gated durable REST + journaled receipts for `mode.set`,
   `narration.set` and workspace undo, with dedicated REST for exact interrupt
   and permission decisions. This is not full P6: `compact`, `archive`,
   `rewind`, `config.update`, `upgrade-to-workspace`, the welcome-frame substitute,
   transport flag and WebSocket deletion remain in that phase.

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
- **S1 — lite/virtual sessions.** Build: run_queue + claim/heartbeat/reaper/completion protocol + fence (incl. object-store PUT fencing or the documented corruption window); epoch/seq/system-writer redesign; turn-request rows + watermarks; **cockpit workstream** (`/connection`+`/prepare` compat answering ready immediately for stateless threads, composer ungating, provisioning-card bypass) + **control-verb REST subset** (§6.7); persistence promotions (mode/narration, task manager, interval cursor, Path-A summary persist; media-fidelity decision *with UI notice if degraded*); scrub-on-claim; claim-bound permission-row retirement at proven lease loss; queued-turn UX + `fair_key`; lite agent-local-state inventory (§6.1); journal-writer coalescing tick; metering lease-interval ingestion (shadow). Acceptance: create-to-*accepted* < 1 s; TTFT p50 < 2 s / p95 < 4 s **including provider TTFT**, and p95 claim-wait bounded at 2× pod-count concurrency; cache_read > 0 on second same-turn call and on a <5-min cross-pod follow-up; pod deleted mid-turn → takeover ≤ ~105 s, `turn.interrupted` rendered, no duplicate answer (watermark), no interleaved histories (fence assert); **zombie's late persist rejected at the fence**; poison unit parks within max_attempts with a user-visible terminal frame + operator unpark; queued input drains FIFO across a steal; zero in-process claim state; epoch bumps ≤1 per steal and 0 per clean handoff; after tenant A → tenant B on one pod, no A-residue (env/singletons/clients).
- **S2 — workspace sessions.** SSH affinity + tmux reattach; §6.1's
  process-local state and backend-path bypasses are externalized or explicitly
  retired (the corrected inventory requires no agent PVC); cloud-push
  sync-generation fence; outbox re-homing (incl. llm_requests archive);
  resident daemons re-homed; hard-interrupt routing; presence re-home;
  permission-wait/heartbeat flow (the active claim stays live; it is not
  released at the gate). Acceptance: push(N)→pull(N+1) ordering holds across a
  forced pod handoff; tmux state survives handoff; mid-idle rclone token expiry
  heals on next claim; p95 approval-to-visible-resume < 3 s; canvas presence has
  no inter-turn flicker (or the flicker is named and accepted).
- **S3 — workers.** The default-off core driver is built: exact Kubernetes-pod-workspace admission with VM jobs pinned; `worker_batch` pull-claim plus the jobs-row CAS; canonical claim bundles; exact-token fenced checkpoint persistence; wall-clock `batch_boundary` arming; prescribed queue complete-and-requeue rotation; TodoManager/instruction-receipt hydration and S2 tmux handoff; terminal `/complete` entry fencing; and lane-aware resume, approval, feedback, cancel and steering. The current shared local pool polls sessions first. Remaining production work is the two-Deployment/KEDA split, job-log per-claim capture, the §10.2 job-journal decision and the Job Bench A/B rollout gate. Gate 3's multi-effect completion finalizer remains a separate cross-lane reliability program, now built through step 5 and still not a worker-rotation dependency. Acceptance still requires completion parity, tokens/wall within noise, measured overhead (target < 2% at wall-clock-sized batches; measure MCP-attached separately), KV-cache hit rate and fault injection: kill -9 mid-batch, steal during a tool call, stale `/complete` rejection and the phantom-complete skew test on a loop job.
- **S4 — optional, bill-driven**: in-process multiplexing; compile-once; workspace pause tier (§10.7); JetStream hatch.
- **Rollback**: per-class flag at every stage; pinned plane intact until lane retirement decisions (§10.9).

### 9.1 Implementation status (2026-08-14)

Written against `develop`, not against intent. Everything described here is
pushed and green in CI. The short version: **the shared substrate is
genuinely kind-agnostic; the session lane accepts exact virtual/none and
Kubernetes sandbox workspaces; the default-off worker driver is built end to
end; and Gate 3 steps 1–5 now provide durable completion, delivery-before-
terminal ordering, stateless background finalization, outcome-gated shell
handoff and independent queue-age monitoring. Session hardening now also makes
the final-memory obligation durable, retires stale claim-bound permission rows
with linked receipts, and distinguishes deleted-thread wakes as
`undeliverable`.** S2 sandbox admission passed its
fail-closed lifecycle and two-pod acceptance gates on 2026-08-11. Worker
rotation still obeys the 2026-08-10 §5.4.5 scope correction: it releases only
through `run_queue` and never calls `/complete`. The production two-Deployment/
KEDA shape, Job Bench rollout gate and step 6's worker-admission opening remain
open/default-off.

**Worker wedge follow-ups (2026-08-14) — the last known worker-lane failure
mode, closed at the source.** A step-5 hand-check produced a stateless worker
job that ran ~35 minutes and then reported `should_stop=false`. Accept honoured
the lease fence and terminalized the queue row as it does for every stateless
report, the finalizer correctly declined to dispose a non-stop, and the job sat
in `processing` forever — invisible to every rescuer, because a `done` command
escapes the exclusion view while the lane has neither an assigned agent nor a
jobs-row lease. Three changes, in order of authority:

1. **Source.** Stateless auto-continue selected a real successor task in one
   LangGraph state update, then `_arm_worker_batch` issued a second update that
   LangGraph read as *consuming* that pending task; the graph therefore ran no
   successor node and returned the armed `should_stop=false` state, which the
   executor reported. Routing and arming are now a single durable update, with
   an arm-envelope validator. Reproduced exactly against the pinned LangGraph.
2. **Accept.** Only a terminal report may close the unit — decision (6) always
   said so and the implementation had keyed on the lane alone. A non-terminal
   stateless payload is now refused before any write with a coded
   `completion_non_terminal_report` 422 (fail-closed on an absent
   `should_stop`); pinned still accepts continue reports, which are a real
   pinned path.
3. **Driver contract for that 422.** It is a definitive pre-write refusal: the
   worker treats the report as never accepted, never records an accepted
   generation, never enters the finalization hold, retires the local runtime
   with the durable tmux preserved, and fence-releases the same generation with
   linear backoff so a successor reattaches under token N+1. Every *other*
   failure stays ambiguous and keeps the renew/exact-acceptance lookup — only a
   success response or durable acceptance proof may enter the hold.

**The rescue route.** A stateless-lane job in a non-terminal status whose queue
row is terminal or absent, with no unfinished command, is owned by nobody. It is
routed to `pending_review` with the stable `stateless_terminal_queue_unowned`
marker and an operator alert. It is deliberately **never re-enqueued**: the
missing outcome may follow already-executed work, so replay would duplicate it.
Queue→jobs lock order and an exact jobs-status CAS preserve concurrent claim
authority, and the ownership census moves from zero owners to exactly one.

**Live verification (k3d, MiniMax-M3).** Five clean queue rotations across a
570 s job with zero `/complete` calls and zero 422s, the sixth token making the
sole terminal 202; a forced continue-report taking exactly one coded 422,
backing off ~5 s with zero commands and the same workspace/tmux, then the next
token reattaching and making the sole accepted 202; and the preserved wedge
specimen parking once, producing one snapshot and one UID-fenced workspace
release before a public delete. A separate hand-check on the public job API with
`context.worker_batch.target_wall_seconds=60` rotated **40+ times with zero
completion commands** while `plan.md` accumulated across rotations — checkpoint
continuity under sustained rotation. Independently, the session-hardening
final-memory crash row was proven by holding `ACCESS EXCLUSIVE` on the vector
execution ledger, force-deleting the executor that minted the obligation while
the ledger was still empty, then releasing: recovery converged to `done` with
exactly one execution receipt and one stored memory.

**Integration history (2026-08-12).** Before the direct-develop Gate 3 runs,
`origin/develop` was merged into `feature/stateless-agents` (`8a730c63`). The
migration collision that merge created is resolved by renaming ours to
`0115a_run_queue.sql` — byte-identical, so recorded checksums stay valid —
leaving develop's `0115_datasource_tombstones.sql` untouched; verify that
ordering with the runner's own `sorted()`, never with `sort(1)`, which reports
the opposite under locale collation. All four k3d smokes pass on the merged
tree: pinned job to `completed` with a non-null `completed_at` and zero
residue; a stateless session surviving a forced cross-pod handoff with file,
tmux environment and cwd intact; the worker driver rotating with
`complete_calls=0` and then making exactly **one** terminal `/complete`; and
suspension/resume restoring an exact file across a changed workspace Pod UID.
The post-merge pytest baseline is **17 known failures** (11 pre-existing
environment/py3.14 plus 6 VM-chart contract tests that reproduce on a clean
`origin/develop`), against ~17,060 passing. That branch arrived mostly dormant:
stateless session admission and worker admission are both default-off in the
chart, and every thread and job defaults to `pinned`. What originally went live
on dev was the
pinned-lane fix set — the Class A atomic status write, the ready-before-report
ordering fix, the critic dedupe index, the durable control/interrupt inboxes,
DB-backed session tasks and the workspace path-bypass fixes.

**Gate 3 documentation status.** §5.4.5 was adversarially reviewed on
2026-08-10/11 (four critics, ten blockers) and the findings were folded back
into it on 2026-08-12. That section is authoritative again;
`docs/research/stateless_agents/gate3_adversarial_review.md` is retained as the
evidence behind each change. **Its DDL is now the corrected one** — the
pre-fold version must not be built from.

**Gate 3 step-4 operations (2026-08-13).** The completion status reorder has
its own default-off `COMPLETION_STATUS_REORDER_ENABLED` gate and hard-requires
`COMPLETION_COMMANDS_ENABLED`; an invalid combination is a startup/config
error, never a partial activation. The mode is copied onto each new command.
Turning the reorder gate off therefore affects only new admissions: existing
reordered commands must continue through their persisted effect order. For an
image rollback, first disable new reordered admissions, then drain all
unfinished commands that have persisted reorder mode, and only then roll back
the image. Do not disable the command executor while those rows exist.

The independent monitoring/sweep path, not the finalizer loop, reports a
missing live finalizer leader and the **age** of the oldest unfinalized command
(including parked rows). Its day-one safety reconciliation is intentionally
dumb and non-executing: an old-mode command stranded on a terminal job, or a
stale command that never advanced beyond S1, is marked `superseded`; no effect
callback, snapshot, merge, spawn or teardown is invoked. A live command/effect
term, routed-action owner, control owner, or active S36 authorization always
holds. Persisted reorder-mode rows that wrote status and still have a tail are
resumed, never safety-superseded.

Operator `unpark` rearms both command and pending-effect attempt/deadline
budgets and then lets the ordinary finalizer claim the exact row. Operator
`force-resolve` requires an explicit terminal status and incident reason,
abandons and records remaining effects, writes the requested terminal status,
and refuses any active S36 authorization. These are incident operations, not
normal completion. During a soak, rollback remains **flag-off-or-drain**; raw
row edits are not a supported recovery path.

**Gate 3 step-5 operations (2026-08-13).** Fresh stateless worker completions
now return 202 only after B4 has durably closed the exact queue generation;
the commands-on background drain then owns the persisted workflow. Pinned
completion remains inline and session units are unchanged. While the exact
command remains observable and inside its deadline, the finalization-pending
hold is inert. A canonical stored terminal outcome attempts tmux cleanup once;
every nonterminal status, park, deadline, lookup loss, cancellation or crash
preserves it and hands authority to lifecycle/S36. On rollback, first close
worker admission, then keep the stateless executor Deployment up until worker
rows and completion commands drain. Do not equate admission-off with
executor-off. The independent oldest-runnable worker alarm remains active
outside the finalizer loop. The **Gate 3 step-5 worker** four-row cloud soak
remains not-started: the
disposable preflight resolved the selected OpenRouter route, but its first main
model call returned provider 401, so zero acceptance rows were begun or
claimed in the four acceptance scenarios. Mutable fixture state and resources
were exact-cleaned; only the documented append-only usage facts remain. The
protected review probe was untouched.

**Session-lane hardening status (2026-08-14).** M0 recon found exact-turn
interrupt already closed by S2: stateless admission is exact-live-gate only and
a new no-live request returns 409 before INSERT. M1 builds the final-memory
producer/drainer described in §5.3.5 on the existing polymorphic effect table,
with destination receipts in vector migration 0019. M3 binds permission rows
to accepted lease tokens and retires only at a proven writer-exclusive
boundary—never by blind TTL—while preserving the already-good ended-session
wake path and settling hard-delete races as `undeliverable`. App migrations
0145–0155 and vector 0019 are frozen; current heads are app **0155** and vector
**0019**, so the next numbers are **0156** and **0020**.

M4 used a separate, MiniMax-pinned **session** soak; it must not be conflated
with the unavailable Gate 3 worker soak above. Exact interrupt completed with
one correlated receipt. The first permission fixture exposed a three-slot pool
starvation P1: control, interrupt and permission LISTENs could consume every
connection and starve the lease heartbeat. Permission wait now polls through
short acquisitions instead of holding the third listener; the fixed live gate
showed the same permission pending while the exact lease renewed, and a
gracefully deleted owner then converged through the production reaper to one
`expired/system/lease_expired` row and one linked receipt without running the
tool. Normal final-memory effects drained once with vector receipts. The
brief's executor-after-final-persist crash row was **not proven**, and abrupt
Pod disappearance also reconfirmed a claimant-quiescence cleanup debt: public
Force-End correctly fails closed until process-zero evidence is durably
recorded. The disposable virtual fixture required a disclosed, exact-CAS
operator acknowledgement after Kubernetes/CRI/containerd/process/cgroup
absence proof; a general production receipt path remains follow-up work.

#### Is it shared between sessions and workers? The substrate and local pool are; the production split is not built.

"Shared" covers three different things in this design, and they landed differently:

| | Design | Built |
|---|---|---|
| Queue + lease contract | ONE `run_queue` table discriminated by `unit_kind` (`session_turn`/`worker_batch`/`bg_task`); one claim/heartbeat/fence/completion/reaper semantics | **BUILT, kind-agnostic.** `src/shared/run_queue/` retains its queue-only contract. `src/shared/worker_queue.py` composes it with the authoritative jobs row: a claim and CAS from `created`, `paused` or `processing` to `processing` commit together, and an ineligible row is consumed rather than poison-released. Rotation advances a synthetic input watermark and completes the old watermark in one transaction |
| Event journal | (implied shared) | **NOT shared — session-only by construction.** `src/shared/event_journal/` hardcodes `threads` and `thread_events` and keys on `thread_id`. Workers have no journal at all; that is the §10.2 job-journal decision, still open. The `src/shared/` path is about who *imports* it (agent + orchestrator), not about unit kinds |
| Pod pools | Production design remains TWO Deployments off ONE image (§5.8): interactive warm floor vs worker KEDA scaler at `minReplicaCount: 0`; the S3 brief permitted a guarded one-pool proof | **ONE shared local Deployment built.** The same executor polls `session_turn` first and only then `worker_batch`, with worker polling independently gated. This is the k3d/Tilt proof shape, not a production availability claim. The production worker Deployment and KEDA scaler remain unbuilt stretch work |
| Drivers | A session driver and a worker driver over the same queue | **BOTH BUILT.** The worker lifecycle is separate inside `src/api/turn_executor.py`, while sharing the local process, DB pool, queue substrate and S2 remote-shell handoff |

Worker admission is an explicit chart/API opt-in and defaults off. It accepts
only an exact in-cluster Kubernetes sandbox workspace; VM requests stay on the
pinned plane, omitted root lanes remain pinned, and an omitted child lane
inherits its authoritative parent's lane. The shared executor is still missing
the VM-mesh sidecar by design. Pinned dispatch and recovery predicates continue
to whitelist `pinned`, while an admitted stateless job is exposed as one
`worker_batch` row only after its Kubernetes workspace is ready.

#### S1 — session lane functionally complete

Rewritten 2026-08-09 at consolidation as one current picture, replacing the
layered dated appendices this section had accumulated. Everything below was
built on `feature/stateless-agents`, is now on local `develop`, and is
k3d-verified unless marked otherwise.

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

**Still open after S1/S2**: Path-A resume-compaction persistence; durable
queued-turn UX; controls beyond the scalar/undo subset (`compact`, `archive`,
connected `rewind`, `config.update`, `upgrade-to-workspace`); metering lease-interval
ingestion; the journal coalescing tick; and server-side conditional object-store
PUT fencing. S2 closed the healthy-owner permission/presence path and ordinary
ended-session wake, plus durable task/undo state, extraction cursor and
citation-anchor items from the original §6.1 inventory. Later M3 closed the
actual lease-loss permission gap with claim-bound retirement and linked
receipts, and closed the distinct hard-delete wake gap as `undeliverable`; it
did not add a blind TTL sweeper.

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

#### S2 sandbox and virtual lifecycle accepted; S3 core worker driver built

**S2** (workspace sessions): the fail-closed gate has passed for exact
Kubernetes sandbox workspaces. Stateless human input, control/interrupt,
upload, IDE/browser/Canvas and the internal claim-bundle credential boundary
admit `virtual`/`none` plus a Kubernetes-provisioned `sandbox` only when class,
binding generation, endpoint generation, runtime incarnation, host-key and
lifecycle markers are internally consistent. VM, Docker-backed physical,
protected-cloud, officer/conference, unknown and malformed classes remain
pinned or fail closed. Admission is rechecked on locked fresh rows at the final
credential/write boundary. Migration **0133** makes session tasks, the
memory-extraction cursor and per-path citation anchors durable; at S2 close-out
the app-schema head was 0133 and 0134–0139 remained unused.

The §6.1 dispositions are now explicit. Tasks hydrate from PostgreSQL on every
attach. The extraction cursor and citation anchors are durable and lease
fenced. Sandbox undo uses the Git/turn ledger plus one durable control request
and receipt; virtual and `none` workspaces reject undo as unsupported rather
than inventing Git authority. Read-before-write stamps and sync caches stay
deliberately claim-local, forcing a fresh read after handoff. WebDAV and
research downloads use operation-scoped staging and write through the
workspace backend.

Remote-shell ownership is bound to the authoritative backing id, workspace and
endpoint generations, runtime incarnation, SSH host-key fingerprint and exact
lease. One durable false-to-true creation attempt owns Pod actuation; every
continuation and retirement revalidates exact Pod/PVC/ConfigMap/Service
identity. Soft End retires claimant/resident/shell authority while retaining
resumable bytes. Permanent End converges snapshots, external resources and DB
anchors before deleting the thread. Claim loss is absorbing: the reaper parks
the successor behind the immutable old Pod UID until that exact claimant or an
exact terminal-runtime observation proves quiescence.

The SSH/tmux handoff substrate is directly k3d-verified across stateless pods:
environment and cwd survive, while a stale lease cannot mutate the successor.
The chart supplies the existing read-only workspace SSH key and allows only
SSH/CDP workspace ingress for the stateless Deployment. The remote tmux,
rclone and overlay markers remain writable by the workload user; this is an
accepted cooperative correctness protocol for sandbox v1, not a security
boundary against hostile same-user workspace code.

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

At S2 close, the generic §3.5 outbox was deliberately **not** built because
effect enqueue was not yet part of the authoritative final-persist
transaction. Gate 3 later supplied the polymorphic `completion_effects`
substrate, and session-hardening M1 built the first production session
producer: exact final-memory extraction. It does not make the entire generic
outbox inventory complete. Exact LLM audit and post-callback Git ordering,
Canvas revision snapshots, a pending-citation sweeper and
orchestrator-derived notifications remain separate future slices.

**Final S2 verification and live acceptance.** Repository-wide pytest completed
in **843.10 s** with **16,681 passed / 156 skipped / 11 established environment
failures / 53 warnings**. Ruff check and format check covered **1,092 files**;
both Helm value sets passed. The app schema replay applied all **133** migrations
(115 transactional plus 18 non-transactional), reproduced the **13,125-line**
snapshot, and passed 141 changed-SQL plus 3 ordering tests. Cockpit passed
**1,835 tests in 119 files**, its production build, and the **2,455-key** i18n
check.

The final sandbox fixture crossed two stateless executors with four tmux
windows, cwd/environment/files, two durable tasks and exact PVC bytes intact.
One undo request produced one receipt and restored the first of two 40-character
Git states. One hard interrupt produced one receipt and zero forbidden
successor answers. Safe workspace replacement retired UID 1 and created UID 2;
soft End/Resume created UID 3 over the same PVC. Soft End at token 13 resumed
exactly once at token 14. Permanent End at token 15 failed closed twice with
503 while physical/auxiliary retirement converged, then returned 200. All 28
`thread_id` relations, queue, Kubernetes resources, snapshot/virtual prefixes
and repository were zero or absent afterward.

A fresh virtual fixture persisted a 15-byte upload, completed exact turns at
tokens 1 and 3 around soft End/Resume, retained four durable messages, and then
permanently deleted. All **31** checked application relations, object prefix,
repository and Kubernetes fixture resources were zero; public state returned
404. A natural SIGTERM fixture delivered the signal to Python PID 1, kept its
heartbeat through bounded completion, and committed exactly one answer after
**99 s**. Its queue finished `done` at token 1 with
`input_seq=consumed_seq=2662` and no loss/hold. A rollback-only real-PostgreSQL
proof then materialized eviction provenance and restored a parked hold to
queued in **5.0 ms**, preserving token, attempts, `run_after` and `queued_at`;
rollback residue was zero.

Failure injection drove the final repairs: fenced resource scripts run under
Bash (`4594ea50`); rclone drain parsing, SFTP closure and retirement retry are
strict (`f5c4a471`); parked rows retain their non-null retry deadline
(`d4ed907e`); claim-loss JSON paths bind canonical text (`d34ada8f`); the Helm
command `exec`s Python as PID 1 (`5e60a644`); eviction provenance is created
before Pod deletion (`77d6909a`); and JSON-held deadlines cross asyncpg through
explicit text-to-timestamp casts (`e17418bd`).

**Residuals.** The exact final-memory tail is now covered by a durable
same-transaction effect/outbox plus destination receipt. The M4
executor-after-persist crash row is still unproven live, but the former final
**0–4-turn** design gap is closed. The remaining generic outbox producers,
durable Canvas mutation invalidations, full two-editor Monaco/auth-expiry UX,
and server-side
conditional object-store writes remain open. Passing the displayed tool-call
`id` rather than the UUID `approval_id` still returns 500 (P2 validation debt).
A pinned parity run exposed pre-existing pinned snapshot/agent-retention debt;
the exact disposable artifacts were removed, but the general pinned cleanup
path is unchanged. These residuals do not reopen S2 sandbox admission. S3
worker admission remains default off.

**S3** (workers): the core lane is now built, but remains **default off** in the
chart. Gate 1's migration **0118** still gives every job exactly one claim
authority: every legacy dispatcher/recovery path whitelists `pinned`, while the
worker queue owns `stateless`. Gate 2's consolidated freeze taxonomy still
makes `batch_boundary` a non-terminal, defense-in-depth pause and routes unknown
declared loop freezes to review rather than phantom completion.

**Admission and claim.** `agent.stateless.worker.enabled=false` is the fresh-
install default and requires the existing stateless executor Deployment. An
explicit stateless request is admitted only for a ready in-cluster Kubernetes
pod workspace; VM jobs stay pinned, roots omitted from the request stay pinned,
and child jobs omitted from the request inherit the parent lane. The shared
local executor always polls `session_turn` first. Only when that poll is empty
does it claim `worker_batch`; the queue lease and jobs-row
`created|paused|processing -> processing` CAS commit atomically, with
`assigned_agent_id` remaining NULL. The returned claim bundle reuses the pinned
dispatch builder and is delivered only after the slow credential assembly
rechecks the exact `(unit_id, lease_token)`.

**Checkpoint, rotation and reclaim.** Worker graph state uses one process-wide
PostgreSQL checkpointer pool and the exactly pinned LangGraph checkpoint stack.
Every saver write is fenced by the current worker lease token; the saver never
uses psycopg pipeline mode, and `LANGGRAPH_STRICT_MSGPACK=true` is mandatory.
Every checkpoint resume hydrates TodoManager and the checkpoint-safe
instruction-read receipts. Shell cwd, environment and panes survive through
S2's lease-token-fenced tmux substrate; the driver does not reintroduce
disconnect-time tmux destruction.

The driver arms the existing wall-clock-first boundary envelope (300-second
production floor; explicitly lowerable in local acceptance). A
`batch_boundary` exits the graph, performs bounded claim teardown, and uses the
prescribed **complete-and-requeue** composition: while holding the exact row
lock it advances the synthetic input watermark, then completes the old
watermark. That transaction leaves the unit runnable now, resets
`attempts_since_completion`, and preserves `last_leased_by` affinity; ordinary
`release` would preserve failure attempts, discard affinity and add backoff.
Rotation never calls `/complete`. The stable proof line is:

```text
worker_batch rotate: unit=<id> token=<n> queue_verb=complete_and_requeue queue_state=queued input_seq=<old> next_input_seq=<new> complete_calls=0 http_complete_calls=0
```

A successor may therefore claim a still-`processing` job, restore its
checkpoint/Todos/receipts and reattach the shell. Recoverable infrastructure
stops release through the queue with honest attempt/backoff accounting. A
genuine terminal or human-facing stop makes the one pinned-frequency
`/complete` report while still holding the lease; a successful report then
completes the exact queue watermark. The thin entry fence compares the request
token with the worker row's current token before every route side effect,
regardless of queue state. Missing or stale tokens fail closed for stateless
jobs with 409; pinned callers retain their historical tokenless behavior.

**Verbs, steering and lifecycle.** Resume, approve and feedback are lane-aware:
the stateless arm re-enqueues the worker unit in queue-before-jobs lock order
instead of parking it for a dispatcher which will never select it. Cancel
hard-closes the queue first, and a leased holder discovers the jobs-row stop on
its renewal cadence. Queued replies and guidance use durable delivery keys;
their acknowledgements are written only after the absorbing fenced checkpoint
commits. Permanent DELETE and cancel now use durable cleanup markers, exact
queue fencing and strict checkpoint pruning. A per-job advisory single-flight
owns cancel checkpoint/workspace cleanup, and DELETE proves the checkpoint
thread empty before atomically removing the queue/job anchor. The stale-
verification critic sweep uses the same stateless hard-close and strict-prune
path, so a parent cannot unblock while critic cleanup is merely pending.

**Schema and remaining work.** The S3 driver required **no migration**. Gate 3
steps 2–4 used app 0140–0144 and step 5 needed no schema. Session hardening
subsequently used app **0145–0155** and vector **0019**. The current heads are
app **0155** and vector **0019**; the next migrations are **0156** and
**0020**. The durable command table, polymorphic effect table, completion
high-water mark and finalizer are built through Gate 3 step 5, and the first
session producer (`final_memory_extraction`) now uses that substrate. Still
unbuilt are §5.8's production
two-Deployment/KEDA split, per-claim job-log
capture, the §10.2 job-journal decision and the Job Bench performance/rollout
gate. The current single-pool proof cannot guarantee interactive capacity
during a rollout or pod loss and does not make VM workspaces stateless.

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

1. **Queued-turn cancellation / partial-tool cleanup** — exact live-turn
   interrupt is built and a no-live gate returns 409 before INSERT. Decide
   separately whether users need a durable verb that cancels queued input, and
   whether interrupted partial tool calls require tool-specific compensation.
2. **Job journal** — extend `thread_events`' contract to job runs in S3, or accept poll-based job UX and defer?
3. ~~Permission-gate parking~~ — **superseded by the built active-wait
   contract**: the exact claim remains leased and independently heartbeating
   through the permission gate; claim loss retires, never re-arms, the old
   request. Only capacity and approval-latency tuning remain, not ownership
   semantics.
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
