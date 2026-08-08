# agent-0

# Postgres job-queue & lease engineering, state of the art (2024–2026) — research for `stateless_agents.md`

## 1. Production SKIP LOCKED queue designs — what each system actually does

### River (Go + Postgres, riverqueue.com / brandur)
- **Claim**: `FOR UPDATE SKIP LOCKED` fetch, but the differentiator is *batching everywhere*: producer consolidates locking for all in-process executors; bulk inserts via `COPY FROM`; pgx binary protocol. ~10k trivial jobs/s on commodity hardware (brandur.org/river).
- **Batched completer**: completions are amalgamated into batch UPDATEs; the batch completer alone raised throughput ~4.5x to ~46k jobs/s on a MacBook Air (riverqueue.com/blog/river-2024). Lesson: per-unit round trips (claim, complete, heartbeat) are the bottleneck, not SKIP LOCKED itself.
- **Leader election**: unlogged `river_leader` table; only the leader runs maintenance services (rescuer, reindexer, cleaner). Resigning leader NOTIFYs so re-election is fast (riverqueue.com/docs/leader-election).
- **Stuck-job rescue**: OSS rescuer is **purely time-based** — jobs stuck in `running` are rescued after `RescueStuckJobsAfter` (default **1 hour**), re-queued or discarded at max attempts (riverqueue.com/docs/maintenance-services). **River Pro v0.26 added "active job rescue" using queue heartbeats** to recover orphans from crashed clients quickly — i.e., heartbeat-based fast rescue is the premium feature, time-based slow rescue is the baseline.
- **Unique jobs**: originally transaction-scoped advisory locks + FNV hashing (riverqueue.com/blog/uniqueness-with-advisory-locks); later reworked to a **unique key + unique index upsert** because advisory-lock uniqueness was slow under load (riverqueue.com/docs/unique-jobs, discussion #346). Uniqueness dimensions: args, kind, period, queue, *state* — "unique by state" is exactly the dedup needed for collapsible background tasks ("one pending cloud-push per thread").
- **PgBouncer**: River documents that LISTEN requires a direct (non-transaction-pooled) connection (riverqueue.com/docs/pgbouncer).

### Graphile Worker (Node)
- LISTEN/NOTIFY for dispatch latency (avg **4.16 ms** add-to-execute), SKIP LOCKED for fetch, **plus a polling fallback** — notifications are treated as wakeups, not truth.
- Throughput: ~183k jobs/s with `localQueue` batching enabled; **~15.6k jobs/s without batching (12x drop)** (worker.graphile.org/docs/performance). Same lesson as River: batching claims/completions dominates.
- Documented trade-off of prefetch batching: jobs checked out but idle if the worker crashes — larger claim batches = larger stuck-job exposure. Maps directly to our batch-budget sizing.

### Solid Queue (Rails / 37signals)
- **Deliberately chose polling + `FOR UPDATE SKIP LOCKED` and skipped LISTEN/NOTIFY** — reasons: multi-DB support, notifications vanish for disconnected listeners, LISTEN breaks behind transaction-mode PgBouncer (dev.37signals.com/introducing-solid-queue).
- **Key structural idea: a separate tiny `ready_executions` table** — jobs being processed, scheduled-for-future, failed, or concurrency-blocked live in *other* tables so the polled table stays minimal. Production numbers: **5.6M jobs/day, ~1300 poll qps, 110 µs average query, 0.02 rows examined per query**. Polling a well-indexed small hot set is essentially free.
- Supervisor process registry + heartbeats; claimed executions of a dead process are released and retried.

### pgmq (Postgres extension, SQS semantics)
- `pgmq.read(queue, vt, qty)` — reading a message *sets a visibility timeout*; the message is invisible to other consumers until `vt` passes; success = explicit `delete()`/`archive()`; timeout expiry = automatic redelivery. "Exactly-once within a visibility timeout, at-least-once overall."
- Design rationale is the important part for us: **the visibility timeout removes the need for any external lock, held transaction, or background reaper for liveness** — the lease *is* row state, checked at read time (`vt_at < now()` makes it claimable again). No connection affinity, no idle-in-transaction, PgBouncer-safe (pgmq.github.io, tembo blog).

### Oban (Elixir)
- **Lifeline plugin**: rescues jobs stuck in `executing` after `rescue_after` (default **60 min**), *purely time-based*, and the docs explicitly warn it "may transition jobs that are genuinely executing and cause duplicate execution" (hexdocs.pm/oban/Oban.Plugins.Lifeline.html). Accurate rescue (DynamicLifeline) is Oban Pro.
- **Table-based leadership** (2.11+): one node per cluster runs plugins; "cluster" = nodes sharing a Postgres DB. Minimal chatter, no distributed-Erlang requirement.

### pg-boss (Node)
- `expireInSeconds` per job = max time in `active` before the monitor fails/requeues it (visibility-timeout equivalent).
- **Worker heartbeats**: with a heartbeat interval enabled, `work()` workers send periodic heartbeats; missed heartbeat ⇒ monitor fails/retries the job — the OSS system closest to the heartbeat-lease we need.
- Retry: exponential backoff `retryDelay * 2^retryCount` **with jitter** and a max cap (github.com/timgit/pg-boss issue #340).
- Maintenance is run opportunistically by any live instance (no leader needed at their scale).

### Cross-cutting patterns (all six systems)
1. **Claim is a short transaction; the lease is recorded state (leased_until / vt / active-since), never a held lock.** No production system holds `FOR UPDATE` or an advisory lock across job execution.
2. **Fast path (claim/complete) is separated from slow path (rescue/retry/cleanup) run by a singleton** — leader-elected in River/Oban. *We already have a singleton orchestrator; no leader election needed.*
3. Time-based rescue with long timeouts is the free tier; **heartbeat-based fast rescue is what the paid tiers add** — and what our long batches require from day one.
4. Batching claim/complete round trips is where all the throughput lives — irrelevant at our volume (units are LLM turns lasting seconds–minutes) but it means one-row-at-a-time SQL is *fine* for us.

## 2. LISTEN/NOTIFY production pitfalls

- **The global commit lock (recall.ai, July 2025)**: any transaction issuing NOTIFY takes a **global, instance-wide exclusive lock during commit** to guarantee queue entries appear in commit order — serializing *all* commits on the instance, not just notifying ones. Under heavy concurrent writes recall.ai saw the DB stall with CPU and I/O *plummeting* (lock-bound, not resource-bound), causing major downtime. Removing NOTIFY from their write path fixed it (recall.ai/blog/postgres-listen-notify-does-not-scale; confirmed against source comments in `async.c`; pgsql-docs thread notes the lock is undocumented).
- **The counterpoint (DBOS)**: LISTEN/NOTIFY scales fine *if* notifications are a ping, not a payload, and are issued from tiny transactions or batched flushes — they went 2.9k → 60k writes/s by buffering notifies and flushing in batch, with a low-frequency poll fallback for crash windows (dbos.dev/blog/postgres-listen-notify-scalability). The unsafe pattern is per-row trigger-based NOTIFY inside large/hot transactions.
- **PgBouncer transaction pooling**: LISTEN is session-scoped; in transaction mode the backend is reassigned after commit, so LISTEN is flat-broken. Only fixes: session pooling (kills pooling economics) or **one dedicated direct connection per listening process** (pgbouncer issue #655; jpcamara.com "PgBouncer is useful, important, and fraught with peril"). Cost model: 1 extra real backend per agent pod.
- **8000-byte payload cap** (Postgres docs, `NOTIFY`): payloads must be <8000 bytes — never carry job data in the payload; carry nothing (channel name is the signal) and re-query.
- **Missed notifications**: delivered only to *currently connected* listeners; disconnect = silent loss. Every serious user (Graphile, DBOS, que) pairs NOTIFY with a poll. Consensus 2026 phrasing (multiple sources): *"a jobs table drained with SKIP LOCKED, with NOTIFY used only to wake idle workers."*
- **Verdict for us**: our volume (one enqueue per turn/batch, not per row) is orders of magnitude below the danger zone, but the poll must be the correctness mechanism and NOTIFY an optional latency optimization — empty payload, issued post-commit or from its own tiny transaction, with a dedicated listener connection per pod if we use PgBouncer. Solid Queue's numbers prove 250 ms–1 s polling of a small indexed hot set costs ~nothing. **go_rewrite.md line 28 ("LISTEN/NOTIFY for low-latency dispatch") should be inverted to poll-primary.**

## 3. Lease correctness: visibility timeout + heartbeat + fencing tokens

### The Kleppmann argument, applied to our zombie writer
"How to do distributed locking" (martin.kleppmann.com, 2016 — still the canonical reference; 2025 follow-ups: surfingcomplexity.blog "Locks, leases, fencing tokens, FizzBee!", hackernoon "The Fencing Gap"): a lease alone **cannot** prevent a paused/slow holder from writing after expiry (GC pause, slow LLM call, network delay — the write arrives after another pod took over). The fix is a **fencing token**: a monotonically increasing number issued at every lock acquisition, attached to every write, with the *storage layer* rejecting any write whose token is lower than the highest seen. "The lock is only half the solution — without token generation, propagation, and storage-layer enforcement you have a best-effort mechanism that will eventually fail."

Our concrete zombie case: pod A leases thread T, stalls 3 min inside a slow LLM call; lease expires; pod B claims T, runs a turn, persists. Pod A's LLM call returns and A appends `thread_messages` — interleaving two divergent turn histories. **Today this write would succeed**: `thread_messages` inserts (orchestrator/database/postgres.py:7794, src/database/postgres_db.py:731,844) carry no epoch/token guard. `thread_events` is epoch-keyed with a unique `(thread_id, epoch, seq)` index (migrations/app/0004_thread_events.sql:51,60) so a zombie merely writes to a *stale epoch* that no client cursor follows — the journal is accidentally fence-adjacent already — but the message store and any job checkpoint write are unfenced.

The doc's instinct is right: **`threads.events_epoch` is already a fencing-token allocator** — `UPDATE threads SET events_epoch = events_epoch + 1 ... RETURNING events_epoch` (src/api/persistent_app.py:1488, orchestrator/database/postgres.py:7976) is exactly "increment on acquisition, atomic, monotonic." It just isn't checked at persist time.

### Advisory locks vs row locks vs recorded leases
- **Session advisory locks** auto-release on disconnect (attractive liveness signal) but: tie the lease to a *connection* — the pod must hold one dedicated backend per active lease for the whole batch; broken under PgBouncer transaction pooling (lock lands on a backend you don't own — snowinch.com writeup on advisory-lock/pool leaks); and connection death ≠ pod death detection you control (TCP keepalive granularity). River itself uses advisory locks only *transaction-scoped, for insert-time uniqueness*, never for execution-time ownership — and later moved off even that for speed.
- **Row locks held across execution** (`SELECT FOR UPDATE` for the whole batch): holds a transaction open for minutes → trips `idle_in_transaction_session_timeout`, pins xmin so vacuum can't clean anything written meanwhile (table bloat across the *whole* database), MultiXact contention with many workers racing (techcommunity.microsoft.com "Potential Consequences of Using Postgres as a Job Queue"; tucanoo.com long-transaction locking). Universally rejected for long work.
- **Recorded lease (pgmq/SQS model)**: claim = one short committed transaction writing `leased_until` + token; liveness = heartbeat UPDATEs; expiry = state visible to any observer. No held connection, no held lock, PgBouncer-safe, and the lease survives pod *and* orchestrator restarts. This is the right model for batches lasting seconds to hours. `FOR UPDATE SKIP LOCKED` is still used — but only *inside* the claim statement, for the microseconds of contention between racing claimers.
- One 2026 caveat worth a footnote (terrislinenbach.medium.com "Why FOR UPDATE SKIP LOCKED isn't enough"): under READ COMMITTED, a plain SKIP-LOCKED select can double-dispatch across *retry* races if the claim and the state flip aren't a single statement; fix is exactly the CTE-update form below (claim and mark in one statement) — which we should use anyway.

### Long tool calls vs lease TTL — heartbeat cadence
SQS's heartbeat pattern is the reference (docs.aws.amazon.com AboutVT; tecracer "of Heartbeats and Watchdogs"): set the timeout to the *typical* unit, not the max; a watchdog task extends visibility (`ChangeMessageVisibility`) every interval while work is in flight; a missed extension is the death signal. Standard cadence: **heartbeat every TTL/3** (a steal requires ~2–3 consecutive missed beats, tolerating one lost packet/GC pause without false steals). SQS caps total extension at 12 h — a nudge toward our batch budget as a hard upper bound too. bbc/sqs-consumer implements this as an automatic background extender — the model for our tool-wait loop: the heartbeat rides an async task *independent of* the tool call, so a 10-minute tmux command needs nothing special; the lease TTL stays 60 s regardless of tool duration. This answers open question 1: **heartbeat from the tool-wait loop; lease-per-superstep is wrong** (it would make lease traffic proportional to steps and make the long-tool case a special case again).

## 4. Autoscaling: KEDA postgresql scaler

- KEDA's `postgresql` scaler runs an arbitrary SQL query returning a number; HPA targets `targetQueryValue` (keda.sh/docs/2.19/scalers/postgresql). Query = queue depth: `SELECT count(*) FROM run_leases WHERE state='queued' AND unit_kind='worker_batch' AND run_after <= now()`.
- **Scale-to-zero**: `minReplicaCount: 0` supported; the 0↔1 transition is KEDA's (activation), 1↔N is plain HPA on the externalized metric.
- **Flap damping**: `cooldownPeriod` (default **300 s**) applies *only* to the →0 transition — all metrics must stay at zero that long. 1→N down-scaling damping is HPA's `behavior.scaleDown.stabilizationWindowSeconds` via `advanced.horizontalPodAutoscalerConfig` (keda.sh/docs/2.20/reference/scaledobject-spec). `pollingInterval` default 30 s — fine for worker batches, too slow to be the *dispatch* path (dispatch is the pods' own poll; KEDA only sizes the fleet).
- Practical guidance (oneuptime KEDA posts): 300 s cooldown as default anti-flap; tune down only if workloads start/stop cleanly in seconds. Needs a DB connection string in a TriggerAuthentication secret.
- Fit for us: KEDA on the **worker** deployment (pull-shaped, tolerant of cold start, scale-to-zero sensible); the **session** deployment keeps a warm floor (`minReplicaCount ≥ 1–2`) since time-to-first-token is the whole S1 win — or scales on a pending-turn-requests count with a floor. Drain-on-scale-down falls out of the design: preStop = stop claiming, finish current batch, release.

## 5. Idempotency and the transactional outbox

- **At-least-once + idempotency keys**: the lease design below is at-least-once by construction (steal + re-run). Canonical pattern (backendbytes idempotency-patterns; oneuptime exactly-once post): side-effectful steps carry a client-generated key; the executor records key→result in the *same transaction* as the effect's local state change; a replay returns the recorded result instead of re-executing. For us this is the doc's v2 tool-call-UUID + workspace-side dedup — the workspace pod keeps a small `executed_tool_calls` (uuid → result) table/file with a unique constraint; consumer-side dedup turns broker at-least-once into effective exactly-once (event-driven.io outbox/inbox writeup).
- **Transactional outbox** for background-tasks-as-queued-work: enqueue the background item (cloud push, memory observer, aux-LLM) **in the same Postgres transaction that persists the turn's messages** — atomicity means "turn committed ⟺ its follow-up work enqueued", no lost pushes when a pod dies between turn-end and task spawn (freecodecamp Go+Postgres outbox; milanjovanovic.tech; event-driven.io). Since our queue *is* Postgres, the "relay" degenerates away: the outbox row **is** the queue row; the same `run_leases`/work table drains it. Delivery is at-least-once (relay can crash after execute before mark) ⇒ consumers idempotent.
- **Unique-job dedup for collapsible tasks**: River's unique-by-(kind, args, state) upsert is the exact tool for "at most one pending cloud-push per thread" — repeated turn-ends collapse into one queued push instead of a pile (riverqueue.com/docs/unique-jobs, blog/idempotent-email-api-with-river). Implement as a partial unique index on the work table: `UNIQUE (unit_kind, dedup_key) WHERE state IN ('queued','leased')` + `ON CONFLICT DO NOTHING`.

## 6. RECOMMENDED lease design for this system

Synthesis of pgmq's visibility-timeout state model + pg-boss/SQS heartbeats + Kleppmann fencing + Solid Queue's tiny-hot-set indexing, unified with the existing `events_epoch`.

### Schema
```sql
CREATE TABLE run_queue (
  unit_id       UUID PRIMARY KEY,          -- thread_id or job_id
  unit_kind     TEXT NOT NULL,             -- 'session_turn' | 'worker_batch' | 'bg_task'
  dedup_key     TEXT,                      -- collapsible bg tasks (e.g. 'cloud_push:<thread>')
  state         TEXT NOT NULL DEFAULT 'queued',  -- queued | leased | parked
  priority      INT  NOT NULL DEFAULT 0,
  run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- scheduling + retry backoff
  attempt       INT  NOT NULL DEFAULT 0,
  max_attempts  INT  NOT NULL DEFAULT 5,
  lease_token   BIGINT NOT NULL DEFAULT 0, -- FENCING TOKEN, monotonic per unit
  leased_by     TEXT,                      -- pod name — diagnostics only, never correctness
  leased_until  TIMESTAMPTZ,               -- visibility timeout
  queued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Solid Queue lesson: the polled set must be tiny and index-only.
CREATE INDEX idx_run_queue_claim ON run_queue (unit_kind, priority DESC, queued_at)
  WHERE state = 'queued';
CREATE INDEX idx_run_queue_expiry ON run_queue (leased_until) WHERE state = 'leased';
CREATE UNIQUE INDEX idx_run_queue_dedup ON run_queue (unit_kind, dedup_key)
  WHERE dedup_key IS NOT NULL AND state IN ('queued','leased');
```
For sessions, `lease_token` can *be* `threads.events_epoch` (same allocator, one counter) — one fewer concept; the claim then bumps `events_epoch` exactly as `_resolve_event_journal_epoch` does today, and SSE epoch-fencing and write-fencing become the same token.

### Claim (single statement, short transaction — the only place SKIP LOCKED appears)
```sql
WITH c AS (
  SELECT unit_id FROM run_queue
  WHERE state = 'queued' AND unit_kind = $1 AND run_after <= now()
  ORDER BY priority DESC, queued_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE run_queue r SET
  state = 'leased',
  lease_token = lease_token + 1,
  leased_by = $2,
  leased_until = now() + interval '60 seconds',
  attempt = attempt + 1
FROM c WHERE r.unit_id = c.unit_id
RETURNING r.unit_id, r.lease_token;
```
Claim-and-mark in one statement closes the READ-COMMITTED double-dispatch race; commit immediately — no lock or transaction is held while the batch runs.

### Heartbeat (async task, every 20 s = TTL/3, riding the tool-wait loop)
```sql
UPDATE run_queue SET leased_until = now() + interval '60 seconds'
WHERE unit_id = $1 AND lease_token = $2 AND state = 'leased'
RETURNING leased_until;
```
**Zero rows returned = lease lost.** The pod must abort the batch, discard un-persisted work, and must not attempt further persists. A 10-minute tmux call needs nothing special — the heartbeat task is independent of the tool call.

### Steal rule (orchestrator reaper, every ~15 s; no leader election needed — orchestrator is already the singleton)
```sql
UPDATE run_queue SET
  state = CASE WHEN attempt >= max_attempts THEN 'parked' ELSE 'queued' END,
  lease_token = lease_token + 1,          -- fences the zombie even before re-claim
  leased_by = NULL, leased_until = NULL,
  run_after = now() + least(interval '1 second' * (2 ^ attempt) * (0.5 + random()), interval '5 minutes'),
  queued_at = now()
WHERE state = 'leased' AND leased_until < now() - interval '30 seconds';  -- grace ≈ 1.5 missed beats
```
Incrementing the token **at steal**, not only at claim, closes the window where a zombie writes after expiry but before any new claimant exists. Backoff is pg-boss's exponential-with-jitter; `parked` is Solid Queue's isolate-failed-jobs move (human attention, out of the hot set). Progress within a batch (any successful persist) should reset `attempt` to 0 so a job that keeps making partial progress is never parked.

### Fencing check at persist time (the Kleppmann half most systems skip)
Every persist — `thread_messages` insert, LangGraph checkpoint put, freeze/complete transition — runs inside a transaction that first executes:
```sql
SELECT 1 FROM run_queue
WHERE unit_id = $1 AND lease_token = $2 AND state = 'leased'
FOR SHARE;   -- abort the persist if zero rows
```
`FOR SHARE` makes it transactionally airtight: a concurrent steal (an UPDATE on that row) blocks until this persist commits, so "check token then write" cannot interleave with a steal. Cost: one index lookup per persist transaction. `thread_events` needs nothing — its `(thread_id, epoch, seq)` unique key already strands stale-epoch writers once epoch = token. **Explicitly note in the doc: fencing protects Postgres state only; a zombie's in-flight SSH/tmux side effect on the workspace pod is out of DB reach — that residual window is bounded by (grace + one tool call) and is what v2's tool-call-UUID workspace dedup addresses.**

### Wakeup and dispatch
- Poll-primary: each idle pod polls the claim statement every 250 ms–1 s. At our fleet size this is tens of qps against a partial index — Solid Queue runs 1300 qps at 110 µs/query on this pattern.
- Optional `NOTIFY run_queue_wake` (empty payload) post-commit on enqueue to cut idle latency; requires a dedicated non-pooled listener connection per pod under PgBouncer; never a correctness dependency. Drop it entirely for v1 — 250 ms poll latency is invisible next to an LLM call.
- Sessions stay push-shaped as the doc says (orchestrator POSTs the turn), but the POST handler still executes the claim SQL for the specific `unit_id` (same statement minus the ORDER BY, `WHERE unit_id = $1 AND state='queued'`) so push and pull share one lease authority.

### Parameter table
| Parameter | Value | Rationale |
|---|---|---|
| Lease TTL | 60 s | Short enough for fast takeover; >> heartbeat period |
| Heartbeat | 20 s (TTL/3) | 2 missed beats tolerated before expiry; SQS-standard cadence |
| Steal grace | +30 s past expiry | Absorbs one GC pause/network blip; total takeover ≤ ~90 s |
| Poll interval | 250 ms–1 s | Solid Queue evidence: negligible load on partial index |
| Reaper cadence | 15 s | Bounded stale-work latency, runs in orchestrator |
| Backoff | 1s·2^attempt·jitter, cap 5 min | pg-boss formula |
| KEDA | pollingInterval 30 s, cooldownPeriod 300 s, workers minReplica 0, sessions minReplica ≥ 1 | keda.sh defaults + anti-flap guidance |

## design_implications
- Rewrite the 'Turn/batch lease' bullet: the lease must be RECORDED STATE (leased_until + token, pgmq/SQS model), not a held lock. `FOR UPDATE SKIP LOCKED` appears only inside the one-statement claim; drop the '(or advisory lock)' alternative — advisory locks tie the lease to a connection, break under PgBouncer transaction pooling, and would force one held backend per active batch; row locks held across a batch trip idle_in_transaction timeouts and pin vacuum xmin.
- Add a fencing token to the design and check it AT PERSIST TIME: monotonic `lease_token` incremented on every claim AND every steal; every persist transaction (thread_messages insert, checkpoint put, freeze/complete) opens with `SELECT 1 FROM run_queue WHERE unit_id=$1 AND lease_token=$2 AND state='leased' FOR SHARE` and aborts on zero rows. Note explicitly that thread_messages inserts are unfenced today (postgres.py:7794, postgres_db.py:731) — the zombie-writer hole exists in current session persistence.
- Unify the token with `threads.events_epoch`: the atomic `events_epoch = events_epoch + 1 RETURNING` allocator (persistent_app.py:1488) is already a fencing-token mint, and thread_events' unique (thread_id, epoch, seq) index already strands stale-epoch journal writers. Make lease claim = epoch bump for sessions so SSE fencing and write fencing are one counter.
- Invert the notification stance from go_rewrite.md line 28: poll-primary (250 ms–1 s against a partial index on state='queued'), LISTEN/NOTIFY at most an optional empty-payload wakeup. Cite: NOTIFY takes a global commit-serializing lock (recall.ai outage, July 2025), LISTEN is broken under PgBouncer transaction pooling, notifications are lost while disconnected, 8k payload cap. Solid Queue proves the poll is free (5.6M jobs/day at 1300 qps, 110 µs/query). Recommend dropping NOTIFY entirely for v1.
- Answer open question 1 concretely: heartbeat from the tool-wait loop (an async task independent of the tool call), NOT lease-per-superstep. Spec the cadence: TTL 60 s, heartbeat 20 s (TTL/3), steal at expiry+30 s grace ≈ 90 s worst-case takeover; a 10-minute tmux command needs no special handling. Heartbeat returning zero rows = lease lost = abort batch, no further persists.
- Give the steal rule retry semantics the doc currently lacks: attempt counter + max_attempts, exponential backoff with jitter on run_after (pg-boss formula), a 'parked' terminal state for max-attempts jobs (Solid Queue's failed-execution isolation), and attempt reset on any successful persist so partial progress never parks a job.
- Use a dedicated small queue/lease table (or at minimum a partial index hot set), not scans over the jobs table — Solid Queue's central lesson; the current dispatcher's `status IN (...) AND assigned_agent_id IS NULL` scans (postgres.py:5423+) are the anti-pattern being replaced.
- Strengthen the 'background tasks re-homed' section with the transactional outbox: enqueue the bg work row IN THE SAME TRANSACTION as the turn's message persist (turn committed ⟺ follow-up enqueued — structurally fixes the cloud-push-lost-on-pod-death class), plus River-style unique-job dedup via partial unique index (unit_kind, dedup_key) WHERE state IN ('queued','leased') so repeated turn-ends collapse to one pending cloud-push per thread.
- Add an autoscaling subsection: KEDA postgresql scaler on queue depth (query = count of runnable units), pollingInterval 30 s, cooldownPeriod 300 s anti-flap (applies only to →0), scale-to-zero for the worker deployment only; session deployment keeps a warm floor since TTFT is the S1 win; scale-down drain = preStop stops claiming, finishes batch, releases.
- State that no leader election is needed: every surveyed system runs rescue/maintenance on an elected singleton (River river_leader, Oban leadership); our orchestrator already is that singleton — the reaper is a 15 s orchestrator loop, replacing the stale-agent detector (and dissolving its live SQL-crash bug).
- Add an honest fencing-boundary note: the token protects Postgres state only; a zombie's in-flight SSH/tmux side effect on the workspace pod cannot be fenced by the DB — residual double-execution window ≈ grace + one tool call, which is precisely what v2 tool-call UUIDs + workspace-side dedup (inbox pattern, unique-constraint dedup table) close.
- Justify batch-size bounds from prior art: Graphile documents that larger prefetch/claim = larger stuck-work exposure on crash, and SQS caps total visibility extension at 12 h — argue for the batch budget (25–50 supersteps / phase boundary) as also being the lease-exposure bound, and consider a hard wall-clock cap per batch.

## surprises
- NOTIFY takes a GLOBAL commit-serializing lock on the entire Postgres instance (undocumented; recall.ai production outage July 2025) — go_rewrite.md line 28 recommends LISTEN/NOTIFY as the primary dispatch path; the state of the art inverted to poll-primary with NOTIFY as optional wakeup, and 37signals skipped NOTIFY entirely.
- The doc's claim mechanism ('SELECT … FOR UPDATE SKIP LOCKED (or advisory lock), short TTL, heartbeat extends') conflates two models: no production system holds a row or advisory lock across execution — SKIP LOCKED is used only for the microseconds of the claim statement, and the lease is recorded row state (visibility timeout). Advisory locks specifically break under PgBouncer transaction pooling and pin a backend per batch.
- Heartbeat-based fast rescue is the PAID feature in both River (Pro v0.26 'active job rescue') and Oban (DynamicLifeline in Pro); the OSS defaults are purely time-based with 60-minute rescue windows that the Oban docs admit can double-execute genuinely-running jobs. Our long batches need day-one heartbeat rescue — we can't crib the free-tier design.
- The zombie-writer hole already exists in the CURRENT system: thread_messages inserts carry no epoch/token guard (postgres.py:7794, postgres_db.py:731,844) — only thread_events is epoch-fenced via its unique (thread_id, epoch, seq) index. Statelessness doesn't introduce the fencing problem; it forces fixing one we already have.
- Fencing must increment the token at STEAL, not just at claim — otherwise a zombie can write in the window after expiry but before any new claimant exists (its old token still matches). None of the doc's sketch or go_rewrite covers this window.
- events_epoch is already a textbook fencing-token allocator (atomic increment-and-return at attach) — the doc's hunch ('epoch mechanism can serve as the fencing token') is confirmed, but only if persists start checking it; today it fences reads (SSE cursors), not writes.
- Throughput is a non-issue by ~4 orders of magnitude: Postgres queues do 15k–196k jobs/s (Graphile) and 46k/s (River) when batched; our unit is an LLM turn lasting seconds-to-minutes. All the batching engineering in these systems is irrelevant to us — simple one-row SQL is fine, and the real design constraints are lease correctness and rescue latency, not qps.

## sources
- docs/features/stateless_agents.md:53 (lease sketch), :99 (open question 1)
- docs/go_rewrite.md:26-31 (LISTEN/NOTIFY + lease sketch)
- orchestrator/database/postgres.py:7794,7976-7984 (thread_messages insert, epoch bump, thread_events insert)
- src/api/persistent_app.py:1476-1510 (_resolve_event_journal_epoch atomic increment)
- src/database/postgres_db.py:731,844 (agent-side thread_messages inserts, unfenced)
- orchestrator/database/migrations/app/0004_thread_events.sql:51-60 (epoch column, unique (thread_id,epoch,seq))
- orchestrator/database/postgres.py:5423-5485,5937-5969 (current dispatcher scan predicates)
- https://brandur.org/river
- https://riverqueue.com/blog/river-2024
- https://riverqueue.com/docs/maintenance-services
- https://riverqueue.com/docs/leader-election
- https://riverqueue.com/blog/uniqueness-with-advisory-locks
- https://riverqueue.com/docs/unique-jobs
- https://riverqueue.com/docs/pgbouncer
- https://worker.graphile.org/docs/performance
- https://dev.37signals.com/introducing-solid-queue/
- https://github.com/rails/solid_queue/blob/main/README.md
- https://www.bigbinary.com/blog/solid-queue
- https://pgmq.github.io/pgmq/latest/
- https://legacy.tembo.io/blog/pgmq-self-regulating-queue/
- https://hexdocs.pm/oban/Oban.Plugins.Lifeline.html
- https://timgit.github.io/pg-boss/api/jobs
- https://github.com/timgit/pg-boss/issues/340
- https://www.recall.ai/blog/postgres-listen-notify-does-not-scale
- https://www.dbos.dev/blog/postgres-listen-notify-scalability
- https://github.com/pgbouncer/pgbouncer/issues/655
- https://jpcamara.com/2023/04/12/pgbouncer-is-useful.html
- https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
- https://surfingcomplexity.blog/2025/03/03/locks-leases-fencing-tokens-fizzbee/
- https://hackernoon.com/the-fencing-gap-why-your-distributed-lock-isnt-safe-and-how-to-fix-it
- https://terrislinenbach.medium.com/why-for-update-skip-locked-isnt-enough-using-pg-advisory-xact-lock-to-build-a-correct-postgresql-d3eb9db46473
- https://techcommunity.microsoft.com/blog/adforpostgresql/potential-consequences-of-using-postgres-as-a-job-queue/4514332
- https://www.snowinch.com/en/blog/postgres-advisory-lock-connection-pool-leak
- https://tucanoo.com/postgresql-locking-best-practices-long-transactions/
- https://keda.sh/docs/2.19/scalers/postgresql/
- https://keda.sh/docs/2.20/reference/scaledobject-spec/
- https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- https://www.tecracer.com/blog/2023/03/the-beating-heart-of-sqs-of-heartbeats-and-watchdogs.html
- https://github.com/bbc/sqs-consumer/issues/218
- https://event-driven.io/en/outbox_inbox_patterns_and_delivery_guarantees_explained/
- https://www.freecodecamp.org/news/how-to-implement-the-outbox-pattern-in-go-and-postgresql/
- https://milanjovanovic.tech/blog/implementing-the-outbox-pattern
- https://riverqueue.com/blog/idempotent-email-api-with-river
