# Stateless Agents — Turn Execution as a Deployment

**Status:** DRAFT — design discussion, no implementation. Origin: user proposal 2026-08-06.
**Related:** `docs/go_rewrite.md` (names this exact flip: "the database drives the graph, pods are stateless turn executors"), `docs/features/agent_lifecycle.md` (the current pod-per-agent model), `docs/features/worker_runtime_strategy.md` (decision: no new runtime — this doc proposes a *driver/deployment* change, not a new engine), `docs/features/session_reliability_and_transport_simplification.md` (P5/P6 converge on the same endpoint), `docs/done/cross_pod_resume_cold_starts_checkpoint_not_replicated.md` (D3 — the enabling substrate, shipped).

## The idea

An LLM turn is a pure function: conversation JSON goes in, a bigger conversation JSON comes out. Everything else — files, shell, git — already lives on an external workspace pod that the agent reaches over SSH. So nothing about an "agent" actually needs to be a long-lived process bound to one conversation.

Today an agent *is* a pod: provisioned (or claimed from the warm pool) per session or per job, pinned to that thread/job for its lifetime, idle whenever the user is typing or the job is paused, reaped when done. The proposal flips it:

- Agent pods become a plain Kubernetes **Deployment** behind a Service. No identity, no registration, no pinning.
- A user message (or a job continuation) is routed to **any** pod. The pod reads the thread/job id, loads messages + config + tool bindings from Postgres, runs the turn exactly as a stateful agent would, executes tools against the external workspace pod, persists the new messages back to Postgres, and is free for the next request.
- For autonomous workers that would run thousands of steps, add a **batch budget**: the pod that claims a job runs up to N supersteps (or to the next phase boundary), checkpoints, releases, and the job re-enters the queue for any pod to continue.

"Agent" stops being a pod and becomes what it always was underneath: a config plus a thread of messages.

## Why

**Utilization and admission.** Today 10 concurrent conversations require 10 attached agent pods, ~300–500 MB RSS each, idle most of the time (a chat session's duty cycle is maybe 5–20%). A cluster that fits 10 agent pods caps out at 10 conversations regardless of how idle they are. Stateless pods are sized by *concurrent turns*, not by *open conversations*: 10 pods can comfortably serve 30–50 chat sessions. For workers the math is different and more modest — worker duty cycle is ~100%, so 15 jobs through 10 pods is genuine time-slicing at batch granularity: total throughput is still 10 pods' worth, each job runs at ~⅔ speed. The win there is **admission and fairness** (all 15 make progress; nothing waits behind a days-long job), not throughput.

**Operations.** Scaling becomes `kubectl scale` / HPA on queue depth. Deletes, in the limit: the warm-pool floor (`agent_provisioner.py ensure_warm_pool`), per-session agent provisioning, agent registration + heartbeat + 30s-cooldown matching, orphaned-job detection (a lease TTL replaces it), drain choreography on rolling deploys (a pod finishes its current batch and exits; the replacement picks up from the queue), idle-session reaping of agent pods (nothing to reap).

**Reliability — a bug-class graveyard.** A striking share of our open reliability bugs are structural consequences of pod-per-agent pinning, and dissolve rather than get fixed:

- Stale-agent-detector SQL crash killing orphan recovery (live on prod) — no agent identity, no orphan detector.
- Version-upgrade drain stripping k8s workspaces / drain masked by error — no drain choreography.
- exit-137 resume wedge; agent-drift drain killing idle sessions — pod death becomes a routine, invisible event.
- Session workspace wiped by idle reaping — agent pods are never reaped *because of a session*; workspace lifecycle decouples fully.
- `docs/issues/fresh_job_dispatched_as_resume_skips_seeding.md` — the empty-task-brief bug exists **only because there are two lanes** (fresh start assumes state gets seeded; resume assumes state is resident). Stateless has one lane: *every* batch loads everything from the DB. The fresh/resume asymmetry cannot exist.
- `docs/issues/session_turn_end_cloud_push_blocks_queued_input.md` — queued input lives in agent memory today; statelessness forces it into the DB, where a turn boundary can't block it.

**UX.** Session creation stops meaning "provision a pod and watch a spinner." A lite/virtual session becomes a DB row; the first turn hits an already-warm deployment. Time-to-first-token on a new session drops from tens of seconds to ~immediate.

## How much already exists

This is the surprising part: most of the substrate shipped over the last quarters, each piece for its own reason.

1. **Workspaces are already external.** The agent process never touches its own filesystem as the workspace; everything goes over SSH/SFTP to a workspace pod or VM. Shell state survives in tmux *on the workspace pod* (`get_shell_state` reads it back). Tool-side state was externalized from day one.
2. **Worker graph state is already in shared Postgres.** D3 replaced pod-local `AsyncSqliteSaver` with `AsyncPostgresSaver` keyed by `thread_id=job_id` (`src/agent.py _make_checkpointer`, `CHECKPOINTER_BACKEND=postgres`, chart default). **Cross-pod resume is verified live**: an agent pod was force-killed mid-phase and a *different* pod resumed the job from the PG checkpoint and continued. Retention (delete on terminal) works. The worker's state layer is done.
3. **Batch limits are a degenerate case of freeze/resume.** The freeze contract (`freeze_data` + `should_stop`, orchestrator as sole status authority) and the dispatcher's re-dispatch of `paused` unassigned jobs already implement "stop here, anyone may continue." A worker batch budget is a freeze with a new type (`batch_boundary`) that skips the heavyweight teardown and immediately re-queues. Phase boundaries — where compaction and archiving already happen — are the natural batch edge.
4. **Session conversation state is already message-granular in Postgres.** The persistent agent writes `thread_messages` directly after every append (mid-turn, not turn-batched), compaction records a `boundary_seq` cursor, and resume rebuilds context from the summary + message tail (`RESUME_MESSAGE_LIMIT` floor). Sessions have no LangGraph checkpointer to migrate — the DB *is* the state.
5. **The client transport is already pod-agnostic.** Cockpit SSE is served from the `thread_events` journal in Postgres (epoch + cursor replay); any orchestrator replica can serve it, and reopen is loss-free. The agent writes events through an ordered journal writer. Which pod produced an event is already invisible to the UI.
6. **Per-request config resolution exists at attach granularity.** Pool-mode `/session/attach` (`src/api/persistent_app.py`) re-resolves the thread's expert config over the pod's boot config — the exact "load config per request" machinery statelessness needs, currently run once per session instead of once per turn.
7. **Approvals and steering are already data, not process state.** Sudo approvals are DB rows; worker steering rides the heartbeat *response* (a pull); officer guidance is queued in the DB. Nothing about human-in-the-loop assumes a resident process — pausing a turn and re-entering later is the existing model.

What the go_rewrite doc sketches as the target architecture is, on the state layer, roughly **built**. What's missing is the control plane and the last in-memory residue.

## What's missing

### Both modes

- **Turn/batch lease.** Two pods must never run the same thread. A `lease` on the thread/job row: claim via `SELECT … FOR UPDATE SKIP LOCKED` (or advisory lock), short TTL, heartbeat extends while the batch runs, expiry re-queues. This *replaces* agent heartbeat/orphan machinery rather than adding to it. Sessions already have an epoch mechanism that can serve as the fencing token.
- **Routing.** Sessions are push-shaped: orchestrator receives the user message and POSTs a turn request to a pod (it already talks to agent pod IPs today). Workers are pull-shaped: pods claim continuations from a queue (Postgres `LISTEN/NOTIFY` + poll fallback; no new infra). These can be two small code paths over the same lease.
- **Soft affinity.** Keep a `last_pod` hint per thread; route there first, fall back to any pod. Correctness never depends on it; it just keeps warm caches (SSH connection, resolved config, message tail) hot. Cold cost ≈ SSH handshake (~0.3–0.5 s) + config resolve + state load (worst observed checkpoint load ≈ 600 kB) — amortized over a batch, <5% at batch ≥ ~25 supersteps.
- **Replay semantics.** Pod dies mid-turn → the turn re-runs from the last persisted point. v1 accepts today's crash-resume semantics (this is exactly what D3-verified resume already does; message-granular persistence keeps the loss window to a partial turn). v2 adds the go_rewrite idea: tool-call UUIDs + workspace-side dedup for at-least-once with safe replay.
- **Observability.** A job's log now spans pods. Logs must be keyed by thread/job id (the job-log-archive work already leans this way); "which pod" becomes a per-batch annotation.

### Sessions

- **Queued input and steering into the DB.** Kill the in-memory input queue; a turn request is a row, the next turn drains rows. This is also the structural fix for the cloud-push-blocks-queued-input issue.
- **Background tasks re-homed.** Turn-end cloud push, memory observer/assembler, aux-LLM tasks currently ride `asyncio.create_task()` in a process assumed to outlive the turn. They become queued work items (same queue, different work type) or run on the pod *after* the lease is released (pod busy, session free). Each needs an inventory pass.
- **De-globalization — or defer it.** `persistent_app.py` is built around module singletons (`_agent`, `_session`). v1 sidesteps this entirely with **one active turn per pod at a time** (the Deployment scales for concurrency). In-process multiplexing of several turns (asyncio is fine with it; the LLM call dominates) comes later, only if pod count becomes a real bill.
- **Session-scoped attachments.** Canvas awareness leases, IDE endpoints, browser streams assume a live session pod. Each needs a home: workspace pod, orchestrator, or a leased sidecar. Inventory required; likely the long tail of this project.

### Workers

- **Batch driver.** A superstep counter in the graph run loop; at budget or phase boundary, freeze(`batch_boundary`) with cheap teardown (skip workspace snapshot — the workspace pod persists; the checkpoint is already in PG) and clear the assignment so the queue re-offers it.
- **Dispatcher simplification.** For the stateless class: drop agent matching, cooldowns, readiness; enqueue instead. The registered-agent path stays for VM-backed and legacy modes during migration.
- **Long tool calls vs lease TTL.** A tmux command can run 10+ minutes; the lease heartbeat must ride the tool-wait loop.

## Python now, or wait for Go?

**Python now.** The reasons stack:

1. **There is no framework blocker.** This is LangGraph's native serverless shape: compile the graph once per process, invoke per request with `thread_id`, checkpointer loads/persists state per call — that is how LangGraph Platform itself serves graphs. The persistent loop needs it even less: its state layer is `thread_messages`, already ours. Nothing here waits on LangChain fixing anything.
2. **Every hard part is language-agnostic.** Leases, queues, state externalization, idempotency, event journals — control-plane work that looks identical in Python and Go. And ~70–80% of the state layer already shipped (D3, message-granular persistence, journal SSE, freeze/resume).
3. **Doing it in Python *defines the contract the Go rewrite would implement*.** Turn request shape, lease semantics, journal protocol, tool idempotency — once those are real and load-bearing, the Go rewrite shrinks from "re-architect the system" to "swap the turn executor behind the same queue," pursued when its actual justifications bite (per-pod density: 10–30 MB RSS and 50 workers/pod vs. our 300–500 MB and 1; cold start; scale-to-zero). go_rewrite.md itself says *not now* and lists the signals. Statelessness-first inverts the risk: the architecture migrates while the code stays familiar; the language migrates later behind a stable seam.
4. **It is consistent with the worker-runtime decision** (2026-08-03: no new runtime, evolve by subtraction). The graph, phases, prompts, tools are untouched; what changes is *who drives the loop and for how long* — a driver and deployment change. If anything it continues the subtraction: it deletes the agent lifecycle control plane.

The one honest caveat on Python: with one-turn-per-pod, capacity = replica count, and a Python replica is ~300–500 MB. That is the price of deferring de-globalization, and it's the first thing Go (or asyncio multiplexing) would improve. At current fleet sizes it is not the binding constraint; idle pinning is.

## Batch semantics

- **Sessions:** batch = one turn. The existing unit; nothing to invent.
- **Workers:** batch = min(N supersteps, next phase boundary), N per expert config (e.g. 25–50). Phase boundaries already compact and archive, so they are free batch edges; the amnesia-loop reforms (fewer, bigger phases) point the same direction. In-flight tool calls finish before release (grace period). Preemption and drain fall out: a draining pod just doesn't claim the next batch.
- **Priority:** interactive turns and worker batches should not share one deployment's queue blindly — separate deployments (or priority lanes) so a user's chat turn never waits behind a 50-step worker batch.

## Phasing

- **S0** — this doc; align on acceptance criteria.
- **S1 — lite/virtual sessions** (no workspace, no SSH: pure conversation + orchestrator tools). Biggest duty-cycle win, smallest blast radius, zero SSH complexity. Acceptance: create-to-first-token < 2 s; M sessions served per pod (M ≫ 1); pod deleted while idle → next turn transparently served by another pod; pod deleted mid-turn → clean client-visible retry, no wedge; queued input survives in DB.
- **S2 — workspace sessions.** Adds per-turn SSH with affinity cache, DB-queued input/steering, background tasks as queued work, attachment inventory (canvas/IDE/browser).
- **S3 — workers.** Batch driver + queue dispatch + lease; delete warm pool/heartbeat for the stateless class. Gate with the Job Bench harness: A/B batch-mode vs. baseline-02 on the pinned suite — overhead budget <5% tokens/wall, no completion regression.
- Rollback at every stage: per-expert/per-mode flag; the pinned-pod path remains intact until the stateless class has soaked.

## Open questions

1. **Lease TTL vs. long-running tools** — heartbeat from the tool-wait loop, or lease per superstep?
2. **Streaming terminus** — journal-only (client reads SSE from DB as today) is simplest and already pod-agnostic; is direct pod→client streaming ever needed, or does P5/P6 finish the job?
3. **Attachment homes** — canvas awareness, IDE proxy, browser streams: workspace pod vs. orchestrator vs. leased sidecar, per attachment.
4. **Does the workspace pod become the new capacity limit?** Probably yes for workspace-backed sessions — which argues for pushing the lite tier as default and lazy-provisioning workspaces.
5. **Provider prompt caching across pods** — content-keyed (Anthropic/OpenAI), so pod identity doesn't matter; only batch cadence vs. cache TTL does. Verify for the self-hosted endpoints (vLLM prefix cache is per-endpoint, shared by all pods anyway).
6. **Where does `--mode`/expert specialization land** — one deployment per expert config (image identical, env differs) vs. one generic deployment resolving config per turn (cleaner; needs the config cache).
