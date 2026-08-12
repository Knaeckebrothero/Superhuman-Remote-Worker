# agent-1

# How production agent platforms decouple conversation state from executors (2024–2026 survey)

Frame of reference: `docs/features/stateless_agents.md` proposes (1) stateless turn-executor Deployment, (2) all state in Postgres, (3) lease + queue, (4) journal-based SSE, (5) external workspace pods. Each platform below is graded on: (a) where conversation state lives, (b) executor↔conversation binding, (c) streaming, (d) crash/replay, (e) idle economics, (f) documented pain.

## 1. OpenAI Assistants API → Responses API (the largest natural experiment)

- (a) Server-side store: Assistants era = `threads` (messages only) + `assistants` (config objects). Responses era = `conversations` (heterogeneous *items*: messages, tool calls, outputs) + versioned `prompts` (config moved out of runtime code into dashboard objects with "versioning, snapshots, diffs, rollbacks"). Mapping: assistants→prompts, threads→conversations, runs→responses, run steps→items.
- (b) Assistants era binding: a `run` was a queued execution *on* a thread — client polled `queued`/`in_progress`/`requires_action` in a loop; one active run per thread (implicit thread lock). Responses era: **no run object at all** — a turn is a plain request/response ("provide a set of input items and get output items back"); long tasks use `background: true`.
- (c) Streaming: SSE; background mode composes with `stream: true`. Every event carries a `sequence_number`; after a dropped connection the client resumes with `starting_after=<cursor>`. This is *exactly* the thread_events epoch+cursor journal contract the doc describes as already shipped.
- (d) Crash/replay: background responses are polled by ID until terminal; "response data is temporarily stored to disk for roughly 10 minutes to enable asynchronous execution and polling" (even under ZDR). Cancel is idempotent.
- (e) Idle: pure API — an open conversation costs the caller nothing. The logical endpoint of "conversation = DB row".
- (f) Pain → deprecation: Assistants API announced deprecated 2025-08-26, hard shutdown **2026-08-26, no grace period** — every call to `/v1/assistants`, `/v1/threads`, `/v1/threads/runs` errors. OpenAI's stated reason: the Responses model is "a simpler and more flexible mental model"; the run-queue-with-polling pattern was operational complexity they killed. Threads storing only messages was too narrow (items generalize). No automated thread→conversation migration tool.

**Verdict for the doc**: the biggest agent-API vendor tried "stateful run objects pinned to threads," found the model too heavy, and re-shaped into stateless request + durable server-side conversation store + cursor-resumable background streaming. This is the doc's architecture, validated at maximum scale.

## 2. Anthropic — Claude Code on the web / cloud sessions (public info)

- (a/b) Each cloud session runs in an isolated Anthropic-managed VM (sandbox); the *conversation* is owned by Anthropic's control plane — a session can be started on web/mobile/CLI and picked up elsewhere (teleport), i.e. conversation is portable data, sandbox is the pinned executor. Self-hosted runner beta (2026) moves the sandbox into customer networks while the session abstraction stays.
- Credential pattern worth stealing: "sensitive credentials such as git credentials or signing keys are never inside the sandbox; authentication is handled through a secure proxy using scoped credentials" — matches the repo's existing `feedback_internal_creds_not_in_workspace` guardrail and argues for keeping it under statelessness (per-turn pods must not need long-lived creds either).
- Network egress restricted by default in the sandbox. Streaming/crash details not public.

## 3. OpenHands (ICLR'25 paper; v1 SDK paper arXiv:2511.03690; docs.openhands.dev)

- (a) **Event-sourced conversation state**: everything (user message, agent action, observation) is a typed event appended to one chronological EventStream; "every run replayable by construction." V1 SDK: event-sourced state + immutable config, explicitly to enable "cross-process pause/resume" and "seamless transition from local prototyping to production-scale deployments."
- (b) Split: agent-server (decision loop, `step(state) -> action` as a *pure mapping*) vs **action-execution-server inside the Docker sandbox**, reached over REST. UI, agent, runtime never call each other directly — all read/append the shared log. This is the strongest independent confirmation of "agent = pure function over an event log; executor = external sandbox."
- (c) Streaming: UI subscribes to the event stream (REST/WebSocket server in v1) — journal-first, pod-agnostic.
- (d) Replay: reconstruct state by re-reading the event log; agent is stateless w.r.t. the log.
- (f) Their v0→v1 rewrite was *toward* this design (event-sourced + immutable config + agent server) — i.e., an agent platform that already had the sandbox split still had to do a v1 to externalize state cleanly.

## 4. LangGraph Platform (the productized version of exactly this doc)

- (a) Postgres: checkpoints per thread — "because checkpoints live in Postgres, **any worker can pick up any thread**."
- (b) Leased, not pinned: workers block on Redis `BLPOP tasks:queue` (atomic handoff — task removed only on delivery); default up to 10 concurrent runs per worker; a sweeper force-times-out "any job exceeding 1 hour"; **Postgres `Runs.next` polling is the fallback path when Redis is down** — a deliberate two-layer recovery mechanism (fast queue + durable table scan).
- (c) Streaming: worker publishes events via Redis Pub/Sub → server → client SSE.
- (d) Crash: worker dies pre-ack → task stays queued; worker dies mid-run → sweeper/timeout re-queues; checkpointer resumes from last superstep.
- **Double-texting** is a first-class, per-run policy with four modes: `reject`, `enqueue`, `interrupt` (halt current run, keep progress, insert new input), `rollback` (delete first run entirely). This is the productized answer to the doc's "queued input and steering into the DB" — the doc should adopt this vocabulary and pick per-surface defaults.
- (f) Pain: needs Redis *in addition to* Postgres (queue + pub/sub); community reports of checkpoint write contention/large-state costs at scale (write-skew, oversized checkpoint rows) — argues for the doc's message-granular sessions path over big JSONB checkpoint blobs where possible.

## 5. Cloudflare Agents SDK on Durable Objects — the opposite bet, steelmanned

- (a/b) One **actor per agent/conversation**: each Agent instance is a Durable Object with its own embedded SQLite; `this.setState()` auto-persists (table `cf_agents_state`) and broadcasts to connected clients. Single-threaded per instance — requests serialize per agent, so **per-entity mutual exclusion is free**: no lease, no fencing token, no double-texting race by construction.
- (c) WebSockets terminate on the actor; **hibernation API**: connections stay open while the DO is evicted from memory; wakes on message or alarm.
- (e) Idle economics are the whole point: after ~10 s hibernatable-idle, **duration billing stops** while WebSockets remain connected; wake is transparent. Paid tier ≈ $12.50/M GB-s duration + $0.15/M requests; 100 WS messages billed as 5 requests. An idle pinned conversation costs ≈ zero.
- What pinning buys: in-memory hot state (no per-turn state load), per-entity ordering/serialization, zero-infra scheduling (alarms), resumable streams and state sync built in, no queue/lease/sweeper code at all.
- (f) Costs of the bet: single-threaded per object (no parallelism inside one conversation); object pinned to one datacenter; long-running work risks eviction — the SDK ships `keepAlive()/keepAliveWhile()` heartbeat alarms, i.e. **they reinvented lease-heartbeat inside the actor**; hung schedules get a 30 s force-reset; fan-out/enumeration across objects is awkward; total platform lock-in.
- **Key insight for the doc**: the stateful bet is only cheap because the *platform* provides hibernation of the pinned unit. Kubernetes has no hibernation primitive for a 300–500 MB Python pod — you pay full RSS for every idle conversation. The real choice is "stateless executor + durable store" vs "hibernating actor," and on K8s only the first is available at reasonable cost. The doc's Why section can state this explicitly: it's not that pinning is wrong, it's that pinning-without-hibernation is the worst quadrant, and that's where the current system sits.

## 6. Temporal for agents

- Core rule: **workflow code must be deterministic; every LLM call and tool call must be an Activity** whose result is journaled in event history on first execution and *never re-run on replay*. Naive replay of an LLM call yields a different token sequence/tool decision → replay divergence — this is exactly why "replay-based determinism" is a poor direct fit and everyone converges on *result journaling* instead.
- Documented pitfalls (multiple 2026 production writeups): (1) whole agent loop inside one Activity → "if the activity fails at iteration 47 of 60, everything restarts" — checkpoint granularity must be per-step (the doc's superstep checkpointing already is); (2) unbounded event history in long loops forces **continue-as-new** at loop boundaries — the direct precedent for the doc's batch/phase-boundary re-queue: mature durable-execution users *already* chunk long agent runs into bounded batches with a fresh history each time; (3) activity heartbeats required for long tool calls so the server can detect worker death — precedent for "lease heartbeat must ride the tool-wait loop" (doc open Q1: the answer is heartbeat-through-the-wait-loop, Temporal-style, not lease-per-superstep).
- Binding: workers are stateless, pull from task queues with server-managed leases/timeouts — same shape as the doc's worker path.

## 7. Restate & Inngest

- **Restate**: journal (`ctx.run()`) records each step's *result* before/at execution; crash → re-invoke handler, replay journal, skip completed steps — "idempotency for free," LLM calls never re-executed. **Virtual Objects** keyed by `session_id`: platform guarantees "a single instance exists per key, queues interactions, stores transactional state" — i.e., the lease + input queue + per-thread serialization the doc must build, offered as a primitive. The doc's v2 "tool-call UUIDs + workspace-side dedup" is precisely Restate's journaled-step model relocated to the workspace boundary — cite it as the contract: *journal results, never replay side effects*.
- **Inngest**: step memoization (each `step.run` result cached in the run's state; on retry, completed steps return cached results) rather than full deterministic replay; AgentKit layers a multi-agent framework on top. Same conclusion: the 2024–2026 convergence is memoized/journaled steps, not replayed nondeterminism.

## 8. Vercel AI SDK — resumable streams

- Architecture: `resumable-stream` package buffers the token stream in **Redis** keyed by a generated `streamId`; the app stores `activeStreamId` per chat in its DB; client `useChat({resume:true})` GETs `/api/chat/[id]/stream`, gets the buffered stream or 204. "Client-side aborts are treated as disconnects... should not cancel the underlying generation" — generation decoupled from delivery.
- Limitations (Ably's critique + docs): covers only reload-during-generation; no cross-device/session recovery; race if a new stream starts before `activeStreamId` clears; streams expire from Redis; multiple clients can attach to one stream.
- **Verdict**: our Postgres `thread_events` journal (durable, epoch+cursor, any-replica serve) is *stronger* than the ecosystem's standard Redis-buffer pattern. Answers doc open Q2: journal-only is not just adequate, it's ahead of the SOTA default; direct pod→client streaming buys nothing.

## 9. Modal & E2B — sandbox lifecycle economics (workspace-pod analog)

- **Modal memory snapshots**: gVisor checkpoint/restore of full container state; ~2.5× faster cold start (torch import 5 s→1 s p50; SD inference 13 s→3.5 s); **restore creates a NEW sandbox, no in-place resume** (snapshot-boot ≈ 2.3 s); GPU state not captured; network connections not restorable; snapshots sensitive to CPU/driver/runtime versions. Economics framing: snapshots exist to make scale-to-zero + aggressive reclaim viable.
- **E2B pause/resume**: full memory + filesystem + running processes preserved; **pause ≈ 4 s per GiB RAM, resume ≈ 1 s**; paused sandboxes "kept indefinitely" at (near-)zero cost; `onTimeout: 'pause'` auto-pauses instead of killing; external clients of in-sandbox services are disconnected on pause and must reconnect; runtime caps (24 h Pro) reset on resume. Firecracker-class snapshot-restore elsewhere quoted at 5–30 ms.
- **Relevance**: this is the answer to doc open Q4 ("does the workspace pod become the new capacity limit?"). The industry pattern for the *executor-adjacent stateful thing* (the sandbox) is not "keep it running" and not "reap it" — it's **pause/snapshot with ~1 s resume**. Our workspace pods currently only have run-or-reap. tmux state on the workspace pod survives idleness only while the pod runs; a pause tier (CRIU, or at minimum scale-to-zero + PVC with tmux-state loss accepted) is the missing third state.

## 10. Devin / Cognition — the stateful bet at maximum engineering cost

- "What We Learned Building Cloud Agents" (2026): sessions must survive "minutes-to-days-long pauses" (waiting on CI, review). Their solution: **snapshot full machine state at the hypervisor level — memory, process trees, filesystem — and power compute down**; resume "exactly where it left off" when the CI/review event arrives. Custom snapshot format **blockdiff**: only changed disk blocks; snapshot creation 30 min → ~200 ms for a 20 GB disk. MicroVM-per-session (own kernel) for isolation: "over a year of hypervisor engineering." Reliable snapshotting "took us longer than any other piece of infrastructure we have built to date." Orchestration (provisioning per-repo environments, routing sessions, **predicting demand for warm VM pools**) took "over three quarters of dedicated engineering"; now thousands of concurrent VMs.
- Interpretation: Cognition kept the *entire session* (agent process included) pinned and made the pinned unit hibernatable — the Cloudflare bet, self-built, at extreme cost. Two lessons: (1) idle-must-cost-zero is universally recognized as the core economic requirement — Cognition, Cloudflare, E2B, Modal all engineered specifically for it; the doc reaches the same end-state by making the agent stateless instead of hibernatable, which is *dramatically cheaper to build* given state was already externalized. (2) **Warm pools, environment provisioning, routing and demand prediction do not disappear — they migrate to the workspace layer.** The doc's claim "deletes the warm-pool floor" is true for agent pods but the workspace fleet inherits a smaller version of the same problem.

## 11. Manus — the cost model of per-turn/per-batch handoff

- "Context Engineering for AI Agents" (Yichao 'Peak' Ji, 2025-07): **KV-cache hit rate is "the single most important metric of a production agent"** — cached input $0.30/MTok vs $3/MTok uncached on claude-sonnet, 10×. Rules: keep the prompt prefix stable (a single differing token — e.g. a timestamp — invalidates the cache from that point), append-only context, never modify earlier turns, **mask tools instead of removing them** (tool schema changes bust the prefix), deterministic serialization. Filesystem as unlimited externalized context (like our workspace + notes/). Built through "four complete framework rebuilds."
- Relevance to doc open Q5: provider-side caches (Anthropic/OpenAI) are content-keyed, so pod identity is irrelevant — *but cadence and prefix stability are not*. A batch handoff that re-resolves config and re-renders the system prompt slightly differently per pod (ordering, timestamps, tool list drift) silently zeroes the cache. Also Anthropic cache TTL (5 min default) vs. handoff gaps: a job that waits in queue >TTL between batches re-pays full input cost every batch. Batch size N should be sanity-checked against this (bigger batches amortize better — same direction as the reform-batch findings).

## 12. Factory

- Sessions are "cloud-synced or local... with fork and share"; a task can start locally with high involvement and be **handed off to a remote droid to continue** — session-as-portable-data, executor fungible across local machine and cloud. Same conclusion as Claude Code teleport: the conversation is a document, the executor is interchangeable.

# Synthesis

**Confirms the design (strongly):**
1. **Every 2024–2026 platform that got to productize this converged on the doc's shape**: durable store owns conversation/step state (Postgres/threads/event log); stateless executors claim work via queue + lease/timeout (LangGraph Redis BLPOP + 1 h sweeper + Postgres fallback; Temporal task queues; Restate virtual objects); streaming via a durable journal with cursor resume (OpenAI `sequence_number`/`starting_after`; LangGraph Redis pub/sub; our thread_events). OpenHands's `step(state) -> action` pure-function agent over an event log is the doc's thesis verbatim.
2. **OpenAI deprecating Assistants** is the strongest single data point: stateful run objects pinned to threads, with polling, were abandoned for stateless request/response + server-side conversation store + background mode. Hard shutdown 2026-08-26.
3. **Nobody replays LLM calls.** Temporal (activities journaled), Restate (`ctx.run` journal), Inngest (step memoization) all converge on *journal results, never re-execute side effects*. The doc's v1 (checkpoint-resume) and v2 (tool-call UUID + workspace dedup) sit exactly on this consensus line.
4. **Batch boundaries have precedent**: Temporal's continue-as-new at loop boundaries (to bound event history) is the doc's phase-boundary batch edge; the "whole loop in one activity" anti-pattern validates superstep-granular checkpointing.

**Contradicts / complicates:**
1. **The stateful bet is viable — when the platform hibernates the pinned unit.** Cloudflare DOs (hibernate at 10 s idle, WS held open, zero duration cost) and Cognition (hypervisor snapshot + power-down, blockdiff 200 ms snapshots) both achieve idle≈zero *with* pinning. The honest framing: pinning-without-hibernation (our current K8s pod-per-agent) is the worst quadrant; K8s offers no cheap hibernation, so stateless is the right escape *for us* — but the doc should say this rather than implying pinning is inherently wrong. Note also what pinning buys that we give up: free per-entity serialization (no lease code), in-memory hot state, actor-local scheduling.
2. **Provisioning burden migrates, doesn't vanish.** Cognition's hardest infrastructure (warm pools, demand prediction, per-repo environment provisioning, routing) lives at the *sandbox* layer — which the doc keeps. Doc Q4's worry is validated by the best available evidence; the answer the industry uses is a **pause tier** (E2B: pause 4 s/GiB, resume ~1 s, kept indefinitely free; Modal: snapshot-boot new instance ~2.3 s, no in-place resume, network conns lost).
3. **Per-turn/batch handoff has a KV-cache cost dimension the doc underweights** (Manus: 10× cached-vs-uncached; prefix stability rules; provider cache TTL vs queue-wait gaps between batches).
4. **LangGraph Platform needed Redis alongside Postgres** (atomic queue handoff + pub/sub). The doc's LISTEN/NOTIFY-only plan is leaner but must consciously replicate the two-layer recovery (fast notify + durable table poll fallback + sweeper) — the fallback poll is not optional; it's the crash-correctness layer.

**Five strongest lessons to absorb:**
1. Adopt the **double-texting vocabulary** (reject/enqueue/interrupt/rollback) as the spec for DB-queued input — pick a default per surface (sessions: enqueue with interrupt available; workers: enqueue) and document rollback semantics explicitly.
2. Specify the lease as **three cooperating layers** (atomic claim; heartbeat that rides long tool-waits, Temporal-activity-heartbeat style — answering open Q1; sweeper + durable-table fallback scan, LangGraph-style) and state the replay contract as *journaled results, never re-executed side effects* (Restate's model = the doc's v2).
3. Answer open Q2 in the negative: **journal-only streaming with a sequence-number cursor is the industry contract** (OpenAI background mode is the exact same design); the Redis-buffer alternatives are strictly weaker than the existing thread_events journal.
4. Add a **workspace pause tier** to the roadmap as the Q4 answer: run → paused(snapshot/CRIU or scale-to-zero+PVC) → reaped, with E2B/Modal/Cognition numbers as the benchmark (resume ≈ 1 s is achievable; in-place resume vs new-instance restore is the key design fork; network/tmux clients disconnect on pause).
5. Make **KV-cache hit rate a first-class acceptance metric** for S3's A/B (alongside tokens/wall): stable append-only prefix across pod handoffs, tool-list masking not mutation, batch cadence ≥ provider cache TTL considerations, and the already-noted vLLM per-endpoint prefix cache check.

## design_implications
- Replace the generic 'queued input into the DB' bullet with an explicit double-texting policy spec using LangGraph Platform's four modes (reject / enqueue / interrupt / rollback), with per-surface defaults: sessions = enqueue + optional interrupt for steering, workers = enqueue; cite that this is a productized, named problem.
- Expand the lease section into a three-layer spec: (1) atomic claim (FOR UPDATE SKIP LOCKED), (2) heartbeat that rides the tool-wait loop (Temporal activity-heartbeat precedent — this answers open question 1: heartbeat-through-wait, not lease-per-superstep), (3) sweeper timeout + durable Postgres table poll as fallback when LISTEN/NOTIFY is lost (LangGraph runs a two-layer Redis+Postgres recovery for exactly this reason — the poll fallback is the correctness layer, not an optimization).
- State the replay contract explicitly as 'journal results, never re-execute side effects' — the v2 tool-call-UUID + workspace-side dedup is precisely Restate's ctx.run journal / Inngest step memoization; no production system replays LLM calls, so the doc's v1/v2 phasing sits on the industry consensus and can cite it.
- Close open question 2 (streaming terminus): journal-only with sequence-number cursor resume IS the industry contract — OpenAI background mode (sequence_number + starting_after) is an exact twin of the thread_events design, and the Vercel/Redis resumable-stream pattern is strictly weaker; drop direct pod→client streaming from consideration.
- Answer open question 4 with a workspace pause tier: add run → paused → reaped lifecycle for workspace pods to the roadmap (CRIU/snapshot or scale-to-zero + PVC), benchmarked against E2B (pause ~4s/GiB, resume ~1s, paused kept free indefinitely, auto-pause on idle timeout) and note the design fork Modal exposes (restore-as-new-instance vs in-place resume; network/tmux clients disconnect either way). Also concede, citing Cognition, that warm pools / demand prediction / environment provisioning migrate to the workspace layer rather than disappearing.
- Strengthen the Why section with the quadrant argument: the credible alternative is the hibernating actor (Cloudflare DOs hibernate at 10s idle with WebSockets held open at zero duration cost; Cognition built hypervisor snapshot + blockdiff at >1 year engineering cost) — pinning per se isn't wrong, pinning-WITHOUT-hibernation is the worst quadrant, and Kubernetes offers no cheap hibernation primitive for a 300–500MB Python pod, which is why stateless is the right escape here. Also note what is given up vs the actor model: free per-entity serialization (hence the lease machinery) and in-memory hot state (hence soft affinity).
- Upgrade open question 5 (prompt caching) from a checkbox to a metric: add KV-cache hit rate to the S3 A/B acceptance criteria; require append-only, byte-stable prompt prefix across pod handoffs (no timestamps, deterministic config/tool-list serialization, mask tools rather than mutate the list — Manus rules, 10x cached-vs-uncached cost); check batch cadence and queue-wait gaps against provider cache TTL (Anthropic 5-min default).
- Add the Assistants-API postmortem as a one-line exhibit: OpenAI deprecated threads+runs (hard shutdown 2026-08-26, no grace period) for a stateless request + server-side conversation-items store + background mode, explicitly for a 'simpler mental model' — the strongest external validation that turn-as-pure-function over a durable conversation store is the winning shape; also note their conversations store heterogeneous items (not just messages), matching the need to persist tool calls/steering/attachments as first-class rows.
- In the batch-semantics section, cite Temporal continue-as-new as the precedent for phase-boundary batch edges (bounding history/state size per batch) and the documented 'whole loop in one activity' anti-pattern as the argument for keeping checkpoint granularity at the superstep, not the batch.
- Add OpenHands v1 SDK (event-sourced conversation + immutable config + agent-server/action-execution-server split + cross-process pause-resume) as the closest open-source twin of the full design — useful both as citation and as a reference implementation to compare contracts against.

## surprises
- OpenAI is killing the Assistants API entirely (hard shutdown 2026-08-26, no degraded mode) — the industry's largest stateful threads+runs agent API was abandoned for stateless request/response + server-side conversation store, i.e. the exact flip this doc proposes.
- The stateful/pinned bet is NOT discredited — Cloudflare DOs get idle≈zero WITH pinning (hibernation after 10s, WebSockets held open, duration billing stops), and Cognition made whole-VM pinning viable via hypervisor snapshots (blockdiff: 30min → ~200ms for a 20GB disk). The doc's framing 'pinning is the bug' should be 'pinning-without-hibernation is the bug'; K8s just doesn't sell hibernation.
- Cognition says reliable snapshotting 'took longer than any other piece of infrastructure we have built to date' (>1 year hypervisor work + 3 quarters orchestration) — the price tag of the stateful alternative, and evidence that warm pools/demand prediction/provisioning migrate to the workspace layer rather than disappearing (contradicts the doc's clean 'deletes the warm-pool floor' claim at system level).
- Modal's snapshot restore creates a NEW sandbox — there is no in-place resume, GPU state and network connections are not restorable; 'snapshot/restore' and 'pause/resume' are different products (E2B does true in-place pause/resume at ~1s resume, paused kept free indefinitely).
- LangGraph Platform — the productized version of this doc — could not do it on Postgres alone: it runs Redis for atomic queue handoff and streaming pub/sub, with Postgres table-poll as the crash-recovery fallback and a 1-hour sweeper; the doc's LISTEN/NOTIFY-only plan must consciously replicate that two-layer recovery.
- Cloudflare's Agent SDK ships keepAlive()/keepAliveWhile() heartbeats to stop actors being evicted mid-operation — even the actor model ends up reinventing lease-heartbeat internally; the machinery is conserved across both bets.
- Manus quantifies a cost dimension the doc barely touches: KV-cache hit rate as 'the single most important metric of a production agent' (10x cached vs uncached input cost) — per-batch pod handoff is cache-safe only if the prompt prefix is byte-stable and batch cadence beats provider cache TTL.

## sources
- /home/ghost/Repositories/Superhuman-Remote-Worker/docs/features/stateless_agents.md:8-105
- /home/ghost/Repositories/Superhuman-Remote-Worker/docs/go_rewrite.md:1-60
- https://developers.openai.com/api/docs/assistants/migration
- https://developers.openai.com/api/docs/guides/background
- https://developers.openai.com/api/docs/deprecations
- https://code.claude.com/docs/en/claude-code-on-the-web
- https://www.infoq.com/news/2025/11/anthropic-claude-code-sandbox
- https://docs.openhands.dev/openhands/usage/architecture/runtime
- https://arxiv.org/pdf/2511.03690 (OpenHands Software Agent SDK)
- https://proceedings.iclr.cc/paper_files/paper/2025/file/a4b6ad6b48850c0c331d1259fc66a69c-Paper-Conference.pdf (OpenHands ICLR 2025)
- https://neuralware.github.io/posts/langgraph-redis/
- https://docs.langchain.com/langgraph-platform/double-texting
- https://tadeodonegana.com/posts/scaling-langgraph-postgres-checkpointer/
- https://developers.cloudflare.com/agents/concepts/agent-class/
- https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/docs/durable-objects/platform/pricing.mdx
- https://developers.cloudflare.com/durable-objects/best-practices/websockets/
- https://temporal.io/blog/build-durable-ai-agents-pydantic-ai-and-temporal
- https://www.xgrid.co/resources/temporal-ai-agent-orchestration-failure-patterns/
- https://zylos.ai/research/2026-04-24-durable-execution-agent-runtimes/
- https://www.restate.dev/blog/durable-ai-loops-fault-tolerance-across-frameworks-and-without-handcuffs
- https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents
- https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams
- https://ably.com/topic/ai-stack/vercel-ai-sdk-resumable-stream-what-it-covers-and-what-it-doesnt
- https://modal.com/blog/mem-snapshots
- https://e2b.dev/docs/sandbox/persistence
- https://northflank.com/blog/e2b-vs-modal
- https://cognition.com/blog/what-we-learned-building-cloud-agents
- https://cognition.ai/blog (blockdiff / microVM background)
- https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Manus (via https://rlancemartin.github.io/2025/10/15/manus/ and MarkTechPost summary)
- https://factory.ai/news/factory-is-ga
