---
tags:
  - feature
  - architecture
  - agent
  - orchestration
  - sessions
  - autonomy
  - strategy
aliases:
  - centurion
  - officers
  - officer agent
  - primus pilus
  - pilus
  - autonomous coworker
  - manager agent
related:
  - "[[centurion_implementation_notes]]"
  - "[[agent_lifecycle]]"
  - "[[headless_persistent_sessions]]"
  - "[[automations]]"
  - "[[loop_campaign_scheduling]]"
  - "[[loop_unified_engine]]"
  - "[[no_workspace_agent_mode]]"
  - "[[memory_light]]"
  - "[[platform_for_agents]]"
---

# Centurion — the always-on project officer

> Every project can have a **Centurion**: one persistent agent that never terminates. It holds a *responsibility* instead of a goal: it owns the project's backlog, schedules and supervises worker jobs, notices when things wedge, asks the user or makes recorded assumptions, and pages the user when it genuinely needs them. The user is the **Legatus** — sets intent, makes the calls only a human can make. The centurion of the user's **default project** bears the title **Primus Pilus** — first among centurions; today a designation, later a command (§9).

**Status:** Design v2 (research-hardened). Filed 2026-07-28 from the consolidated officer notes (`Officers.md`); hardened the same day by a five-agent research fan-out — three codebase planners/verifiers, two web researchers (prior art: Letta sleep-time agents, LangGraph ambient agents, Devin/Cursor/Factory, OpenClaw heartbeat, SRE/PagerDuty, Vending-Bench and governance-decay literature). No core decision was refuted; the research added a guardrail layer and corrected several mechanism-level assumptions. Interaction surface (the conference model, §2) settled 2026-07-29. Anchored build plan: [[centurion_implementation_notes]].
**Scope:** v1 = exactly one centurion on one project, full backlog + job-creation authority, sleep-loop architecture. The wider legion (Tesserarius/Optio as separate agents, centuries, model tiering) and the Primus Pilus command extension are explicitly parked — see [Deferred](#9-deferred--the-legion).

---

## 1. Motivation — what a month of autonomous operation taught us

The RSI loop ran for over a month and executed ~150 jobs building a hotel ERP system. Two categories of failure, and neither was model capability:

**Silent mechanical stalls.** Every loop outage was trivially recoverable and stalled the system anyway, because nothing with judgment was watching: the headscale latch kept VMs from ever becoming ready; the critic-feedback wedge left parents paused-but-undispatchable; a stale-agent detector crash killed recovery sweeps for weeks. Each time, the remedy was one human action applied hours or days late. The loop is a cron with no supervisor.

**Task pressure without ownership.** Given "build a better ERP than Resavio," the loop produced a month of locally-successful jobs — modules, digging, refactors — and no UI and no deployment story. The mechanism: every actor optimizes locally. Workers complete phases. The scholar generates backlog from the codebase — inward-facing, so it proposes more modules forever. The critic QAs one output against its own job's goal. *Nobody's objective is the project's external value, and nobody feels time.* The missing actor is one who can be asked "what is the current state?" and told "we present next week — be prepared," and who turns a vague charge into questions, declared assumptions, and a re-prioritized backlog.

The centurion is both things at once: the supervisor that converts silent stalls into "I've parked the queue, you need to restart the controller," and the owner that converts task execution into outcome pursuit. The long-horizon literature independently names the second failure: goal drift under autonomy manifests mostly as *failing to act*, not acting wrongly — which is why idle-capacity detection is mechanical in this design (§4), not left to the officer's attention.

## 2. The role

### Charge, not goal

Workers receive goals and terminate on `goal_achieved`. The centurion receives a **charge** — "you are responsible for this project's success" — and never terminates. There is no `goal_achieved`, no phases, no todos, no completion signal. He runs on the persistent-session loop (`src/persistent_graph.py::run_persistent_loop`), which already has no terminal success state — its only current exit, the idle timeout, is replaced for officer sessions (§4).

This is commander's-intent doctrine applied to agents: intent = expanded purpose + key tasks + end state, *what and why, never how* — and it licenses **disciplined initiative**: depart from stale instructions when the situation changes, stay inside the charge. His prompt carries standing questions, evaluated every wake, instead of tasks:

- What would I demo today if the Legatus walked in?
- What is the largest gap between the current state and something presentable/valuable?
- What am I assuming that I should verify or ask?
- Is anything I'm responsible for stuck, failing, or waiting on me?
- Am I using my capacity, or is it idle while backlog exists?

Time is first-class state: deadlines ("presentation next Thursday") live in the charter and reorder everything downstream of them.

### Klug und faul — the one design constraint

The centurion must be **clever and lazy**. He never does object-level work — no coding, no research, no file editing. He observes, judges, delegates, escalates. This keeps his context clean over weeks, keeps his turns cheap, and keeps a bright line between supervision and execution.

The constraint is *structural*, not prompt-hoped: officer sessions run the `none` lite workspace backend — file tools aren't exposed, shell hard-refuses. His hands are the delegation tools, the KB tools, `sleep`, and `notify_user` — nothing else. Validation from the field: Claude Code's own agent-teams docs record team leads doing object-level work despite prompt-level "delegate only" instructions; the structural version cannot fail that way. When the centurion needs information that requires object-level work, he **schedules a recon job** — a worker whose deliverable is a `report` note into the KB. The manager pays for information.

The dumb-and-lazy tier below him stays mechanical: heartbeats, orphan sweeps, stale detection, the loop-advance sweeper remain code, not LLM. The centurion consumes their signals; a dumb watchdog watches the centurion (§4). And because long-horizon meltdowns are caused by *misinterpreting state*, not by context exhaustion (Vending-Bench), the DB stays the single source of truth: the sitrep is a computed view of it, and every officer action is validated orchestrator-side against DB state — a deluded officer gets loud tool errors in his next sitrep, not compounding fantasy.

### Talk, ask, page — the communication contract

Three distinct outputs, in the ambient-agent triad vocabulary, each with a different cost to the Legatus:

- **Log (talk).** Writing in the thread. Free. All routine narration, decisions, and the **micro-standup**: every substantive wake ends with ≤3 bullets — did / next / blocked. The officer's log thereby becomes a searchable async-standup archive.
- **Question (ask).** A thread message plus a parked assumption with a **response window and a default**: "Assuming A unless you answer by tomorrow 09:00." Unanswered questions convert to recorded assumptions mechanically — asks never block the loop. Questions surface in the daily digest (the *no-surprises rule*: the Legatus never learns of a material assumption only when it detonates).
- **Page (notify).** The explicit `notify_user` tool. Reserved for "action needed from you, now." Governed by the notify contract in §6.

### Ask or assume — with confidence and stakes

A manager's function is turning one vague sentence into clarifying questions and declared assumptions. The rule takes two inputs (Devin's empirically validated pattern — its self-rated confidence correlates 2× with merged-PR success): **ask when confidence is low AND stakes are high; otherwise assume, record, proceed.** Every assumption is a ledger entry carrying `{confidence, stakes, evidence refs (job ids / audit timestamps), response window, default}`. Entries are **bi-temporal — superseded, never deleted** (`active → confirmed | overturned | superseded`), so the history explains past decisions made under old assumptions, and the Legatus can review low-confidence entries first. An overturned assumption is a normal wake event and a memory-extraction trigger, not a crisis.

### One mind at a time — the log and the conference

A centurion has one *identity* but two *embodiments*, never active at once:

**The log** — his background session: the sleep-loop, his timeline, where sitreps, decisions, and micro-standups accrue. Quick fire-and-forget notes ("FYI — the demo moved to Tuesday") are typed straight into it and arrive as wake events in his loop. You read the log to watch him work.

**The conference** — how you actually *talk* to him: a **separate interactive session wearing his identity**. Because everything that *is* him lives in project-scoped stores (§5) and conversations are ephemeral by design, a session created with his persona and the project binding simply *is* him — same charter injected, same KB, same assumptions ledger, same memories. A conference session may have a workspace or VM: with the Legatus present, walking the code together beats screenshots — the machine serves *shared understanding*. The klug-und-faul constraint binds the autonomous embodiment structurally; in conference it is a persona norm — demonstrate, explore, explain; what leaves the room is updated intent (charter, backlog, KB) and dispatched jobs, not the officer's own commits.

**Serial, not concurrent.** While a conference is open, the background loop is **held** (§4): events *and his timer* queue durably in the outbox, and on conference end he wakes exactly once with the **session brief** plus everything that queued. This is the single-writer rule that makes two embodiments one mind: intent changes made in conference land in the stores *before* the background loop acts again, so it can never act on stale direction. Action-level conflicts were already bounded — both embodiments act through the same orchestrator API on the same DB, and capacity is enforced server-side — the serial hold closes the remaining surface: divergent intent. If the hold ever chafes, concurrent mode is a one-flag relaxation; deferred. The background officer never carries the conversation transcript — he gets the minutes, which is also the cheaper context.

Multiple *projects* each having their own centurion is not a conflict — scopes are disjoint. Concurrent sessions sharing one centurion's identity remain deferred.

### Naming and hierarchy

- **Centurion** — the per-project officer this document specifies. Every project can have one; the mechanism is project-scoped from day one.
- **Primus Pilus** — the *title* borne by the centurion of the user's default project. In v1 a name and nothing else; the planned extension (§9) gives the Pilus command authority — creating and managing entire projects with their own centurions, and delegating across officers. Personas and cockpit copy use the Roman names; code uses the mechanical term `officer`.
- **Legatus** — the user, across all projects.

This also settles the old note question "should projects be centuries?" — organizationally yes: a project is a century, the unit one centurion commands.

## 3. Economics — why while(true) is affordable

Measured reality (2026-07): the OpenAI $200 subscription sits at ~90% headroom; two jobs on `gpt-5.6-sol` ran 40+ hours and only moved it to ~50%. Resets routinely expire unused. The officer's pulse is a rounding error against structural surplus that already exists.

Routing is a config exercise: a catalog row for the subscription model anchored to the `codex-proxy` endpoint (`src/core/model_registry.py` — endpoint label selects the Responses-API factory; gpt-5.6 family normalization; 400k context clamp). Escalation to a top-tier metered model for hard decisions is a later refinement; v1 runs one model.

Two cost refinements from prior art:

- **Cache-aligned cadence** (OpenClaw's measured trick): set the default timer wake / `sleep_max` just under the codex-proxy model's prompt-cache TTL, so no-change wakes hit warm cache and cost near-nothing. ~48 wakes/day at warm-cache sitrep size is far below the surplus.
- **Precompute on quiet wakes** (Letta sleep-time compute: ~5× test-time reduction when queries are predictable — and standing questions are maximally predictable): on quiet timer wakes the officer maintains a small **current-state brief** KB note answering the standing questions, so "what's the current state?" is served from prepared context, not derived on demand.

This settles the old open question "OpenAI und GLM subscriptions could be the officers?" — yes, that's exactly what they're for.

## 4. The wake protocol — sleep as a tool

### The sleep tool

`sleep(minutes, reason)` is a **real registered tool** (category `core`, exempt from workspace gating), following the freeze-request seam precedent: executing it stores a sleep request on the ToolContext; the turn loop **peeks the flag after the tool batch completes and ends the turn** instead of calling the LLM again (`TurnResult.ended_by_sleep`). Bounds come from config (`sleep_min_minutes`/`sleep_max_minutes`, defaults ~5/60); the agent chooses within them — long when waiting on a 40-hour job, short after dispatching something risky.

**The timer is external and durable.** The sleep tool *files a wake-up call with the orchestrator*: it upserts a pending `timer` row in the event outbox with a `fire_at` timestamp — Postgres-persisted, so a pod crash or node downtime never loses the schedule; the next pod picks up the timer where it was. The drain claims due timer rows and injects the `[timer wake]` exactly like any other event (the in-house precedent is the automations cron dispatcher: `next_run_at` + claim-due-rows). This is the full durable-execution consensus (Temporal/Restate: timers belong to the durable layer, both storage *and* firing), and it buys three things at once: crash-safe resume, a **uniform conference hold** (the timer is just another outbox row the drain defers — no agent-local timer to suppress), and free participation in debounce/coalescing/urgency like every other wake source. If a turn ends *without* a sleep call (plain text — answering the user), **the watchdog files `sleep_max` on his behalf** — an officer session never parks indefinitely and never idle-archives, and absent a filing the system is lazy for him too. Anything arriving on the input queue still wakes him before the timer.

What stays agent-side is only a **backstop**: the input wait keeps a long, clearly-labeled timeout (~max(2×`sleep_max`, 2h)) that synthesizes a `[backstop wake]` — insurance for the one partial failure external timers can't cover (orchestrator timer path broken while the API is up). The terminal `IdleTimeoutError` is still never raised for officers. The trade-offs are honest and small: wake timing gains sweeper-tick granularity (irrelevant at 5–60 min sleeps), and the orchestrator dependency is not new — his hands are the orchestrator API already; a wake without an orchestrator could do nothing anyway.

Two corrections from the code (load-bearing):

- **The loop starts lazily.** A freshly booted or respawned officer pod restores its session and then parks — the loop only starts on first input or WS attach. The **synthetic respawn wake is therefore the loop bootstrap**, not a courtesy: every officer session attach ends with `_ensure_persistent_loop_started` + an injected `[wake: session restarted]` event. Wakes delivered durably while the pod was down restore as *history*, not as queue input — only the respawn wake makes a turn actually read them.
- **Eager/polite modes don't fight sleep** — neither auto-continues; the loop always blocks on the queue. What must change is downstream: officer sessions bypass both `awaiting_user` flips (input-park and sudo-gate), since sleeping is not awaiting a user. Polite mode is semantically contradictory for officers and is overridden, not honored.

### Wake sources (v1)

| Source | Mechanism |
|---|---|
| Timer (sleep deadline / implicit `sleep_max`) | **Orchestrator-fired**: pending `timer` outbox row with `fire_at`, claimed by the drain like any other event; sleep tool files it, watchdog files `sleep_max` when no filing exists; agent keeps only a long labeled backstop |
| User message | Existing thread input path (`POST /api/persistent/threads/{id}/input`) |
| Job transition — completed / failed / cancelled / pending_review / **paused** — any job in his project | Event outbox enqueue at the completion hooks + cancel/approve/escalate/pause endpoints |
| Sudo / VM-upgrade request opened for a job in his project | Enqueue in the sudo-gate pending path |
| Fleet events — agents offline, orphaned jobs recovered | Enqueue from the stale-agent sweep (`fleet` source, all officers) |
| Loop turn concluded (officer-scheduled project) | The `officer` scheduling branch (§7) |
| Respawn / boot | Synthetic wake at session attach (the loop bootstrap) |
| **Conference ended** | Conference-end hook: one `conference` wake carrying the session brief + everything held during the meeting |
| **Reflection** (new) | Accumulated event-weight threshold (~1–2/day) with a daily floor, plus one after every context compaction |

**Reflection wakes** carry no new events. Their instruction is the maintenance cadence that makes "KB gardener" real (Letta sleep-time agents; Stanford generative-agents reflection): distill the interval into insight notes (with evidence refs), garden the KB (merge/supersede/expire per §5), review open assumptions and the drift question ("do recent decisions still serve the charge? report drift candidates"), refresh the current-state brief, and ask one *generated* focal question ("what is the most important question about this project right now?"). The post-compaction reflection doubles as re-grounding — governance-decay research shows compaction silently strips in-context constraints; the pinned charter (§5) prevents the constraint loss, the reflection wake re-orients the working state.

Session-side *permission-gate* opens (distinct from sudo) have **no orchestrator write site** — the agent inserts them directly over its own DB connection — so they are not a v1 wake source; the sitrep's pending count covers visibility. Deferred cleanly.

### The event outbox — how wakes travel

The existing `session_wake` machinery is a **jobs-row outbox**: keyed on `jobs.created_by_thread_id`, four terminal statuses only, no `paused`, no non-job events. It cannot carry officer wakes; what *is* reusable is its delivery layer (live-inject with readiness probe, durable fallback, claim-then-send discipline). v1 therefore adds a small durable outbox table, `session_wake_events` (migration; schema sketch in [[centurion_implementation_notes]]):

- **Insert-time dedup**: partial unique index on `(thread_id, source, dedup_key) WHERE state='pending'`, `ON CONFLICT DO NOTHING`.
- **Per-source debounce ≥5 min** (PagerDuty's standard window; fleet sources 10 min), implemented in the claim query against recent `sent` rows — debounce state lives in the table, works across replicas.
- **Coalescing = the drain claims all pending rows per thread and renders ONE sitrep message.** This must be built — nothing coalesces today; one queued item is one paid turn, so a six-job fan-out would otherwise cost six turns.
- **Global throttle** distinct from per-source debounce: a minimum interval between officer wakes overall, so N misbehaving sources can't each claim their own wake. Late events land in the next sitrep — nothing is lost, because the sitrep is snapshot-diff based, not event based.
- **Urgency is never downgraded** when coalescing: the batch inherits the max urgency of its members.
- Officer-created jobs would otherwise fire *both* outboxes on completion; the legacy jobs-outbox delivery is suppressed for officer threads (it enqueues into the event outbox instead of injecting its own message). `paused` is **not** added to the legacy outbox's terminal set — officer paused-wakes ride the event outbox only.

### The conference hold

While a conference (§2) is open for the project, the background officer is **held**, and with the external timer the hold is **uniform**: every wake path — events *and* his timer — routes through the orchestrator's drain, which simply defers held threads (rows stay pending, timers included). A hold notice injected at conference start covers the one residual agent-local path (the long backstop timeout — if it fires mid-conference, the persona rule is "standing hold, no brief yet → sleep"), and a light mechanical fence on job actions from the held thread stays as belt-and-suspenders. Ending the conference generates the **session brief** via the existing teardown machinery (memory extraction + session summarizer), releases the hold, and enqueues a `conference` wake — the brief plus every event and timer that queued during the meeting, coalesced into one sitrep. Workers are unaffected by the hold: jobs keep running; only scheduling judgment waits, and the Legatus is present for anything urgent.

### The sitrep — wake message as computed delta

Assembled server-side (new `orchestrator/services/sitrep.py`) so the centurion never re-derives the world and never needs to remember previous numbers across compactions. Format: **numbered stable lines, delta-only, symptoms before causes** (military SITREP doctrine — "report only changes since the previous"):

1. **Wake reasons** — coalesced event list (or "timer"), max-urgency flag.
2. **Jobs delta since watermark** — transitions plus per-active-job progress fingerprints *with the previous wake's values alongside* ("job 4f3a: steps 132→132→132" makes stuckness a reading exercise, not a memory exercise). Fingerprints tiered: full lines for active/stuck, one-liners for healthy.
3. **Fleet & capacity** — agents ready/working/offline; capacity in use vs. cap; **a mechanical `capacity idle while backlog non-empty` flag** (drift-through-inaction is the documented long-horizon failure — it is not left to the officer's attention).
4. **Pending on you** — open sudo/permission requests, unanswered Legatus messages, assumptions awaiting review.
5. **Budget** — officer-turn tokens and project burn, today / this week.

Data-source reality (verified; full table in [[centurion_implementation_notes]]): the delta **cannot key on `jobs.updated_at`** (poisoned by trigger cascades) — it is a snapshot diff against fingerprints stored in thread metadata. Progress fingerprints and token burn live in the **audit DB** (`agent_audit` step counts/phase/last-write; `usage_events` tokens — `jobs.total_tokens_used` is a dead column, and the existing `get_job_progress` endpoint is a stub). The sitrep service is therefore cross-database and **degrades per-section** when the audit DB is down (per-step isolation pattern). Cost: ~8 indexed queries, 300–900 tokens at ~10 active jobs. The **watermark advances only on successful live delivery** — the durable fallback writes a compact notice into history but leaves the watermark, so the respawn wake's sitrep re-covers the gap.

### Officer-level loop guard

The manager analog of worker stuck-detection — absent from v1 designs of every framework that later documented runaway-manager incidents (AutoGen, CrewAI):

- **Per-wake action budget** (`max_actions_per_wake`, default ~10 tool calls) — a wake that wants more ends with a sleep and continues next wake.
- **Repeated-action fingerprint**: the same tool + target across N wakes with no sitrep delta → **forced escalation to the Legatus instead of another attempt** (e.g., resume-with-feedback on the same wedged job forever).
- **Daily officer-turn token ceiling** — on breach: force-sleep + digest notice. Three-layer cost defense: budgets (this), behavioral (persona), watchdog (below).

### Interaction with existing session machinery

Officer sessions are ordinary persistent sessions with exemptions, not a new kind — all additive, gated on the officer flag:

- Idle-timeout termination → replaced by the sleep deadline (above).
- Attention-sleep sweeper, both agent-side `awaiting_user` flips, and the orphaned-thread **`ended` sweep** exclude officer threads — the watchdog owns officer lifecycle.
- The per-park `"ready"` NATS mirror is suppressed for officers (otherwise ~48 `session.waiting` feed events/day).
- No new FSM state: the thread stays `active`; the sleep deadline lives in metadata + process state.
- **Officer-ness is denormalized into `threads.metadata.config_override.officer`** — a hard constraint: every orchestrator sweeper is SQL over thread metadata and cannot see resolved expert config. One shared SQL predicate, used by all sweeps.

### The watchdog watches the officer — and retires him honestly

Dumb code, not LLM (new leader-gated `officer_watchdog` sweeper, ~60s):

- **Liveness** via the existing agent-binding + readiness probe; **turn-activity** via the newest ai/tool row in `thread_messages` (not `threads.last_activity`, which every durable notice bumps). **Timer duty**: the watchdog files the implicit `sleep_max` wake when an unheld officer has no pending timer row, and treats a timer row overdue past `fire_at + grace` with a live pod as a delivery failure.
- Delivery failure with a live pod → force-inject an overdue wake. Injection failed, agent gone, or thread `suspended` → respawn (idle-pool first, else dedicated pod), rate-limited to one attempt per thread per N minutes; page the Legatus on repeated failure.
- **`suspended` is the officer's most common down-state, not crashes** — deploys drain-suspend parked sessions; the watchdog's respawn-from-suspended is the routine path (rainbow-deploy survival is a tested requirement, not an edge case).
- **Retirement is explicit.** "Never terminates" needs an off switch that isn't a crash: deliberately ending an officer thread also writes `officer.enabled=false`, so the watchdog stands down. A *crash*-`ended` officer whose flag is still true is **paged, not auto-resurrected** (default; see Decisions pending, §10). Watchdog scope is `active` + `suspended` only.

## 5. State — identity outside the context window

A 24/7 session compacts hundreds of times; everything that *is* him must survive that. No new storage system — route by state type. (The routing reproduces the Zep/Graphiti three-tier shape: DB rows = episodes/ground truth, KB notes = semantic layer, charter = the standing summary — which is the argument that no new store is needed.)

### The charter — pinned intent, split write authority

A pinned KB note (`note_type='charter'`, one per project), injected **unconditionally** every turn as its own block — *not* via relevance-ranked retrieval, where a small note can simply lose the top-5 ranking (verified: both existing KB injection routes are ranked retrieval; the charter needs a dedicated fetch by `(project_id, note_type='charter')`). This pinning is a **safety property, not a convenience**: governance-decay research measured constraints silently dropped by compaction going from 0% to 30–59% violation rates, and ~47 pinned tokens restoring 0%. The pinned content therefore includes the *governance* — authority bounds, ask-first list, capacity rules — not only identity.

Structure (commander's-intent skeleton, ≤200 lines total, ~15 standing orders, each carrying its **reason** — explanations generalize to novel situations where bare rules don't; negative orders included: "never do object-level work — your context is your judgment"; "never mirror DB state into notes — the DB is truth"):

| Block | Content | Writes |
|---|---|---|
| `identity` | Expanded purpose (why the project exists, for whom) · key tasks (3–5, e.g. "always demoable; external value over module count") · end state | **Legatus-owned** |
| `authority` | Bounds: capacity, budgets, ask-first list, page budget, **risk acceptance** (what may go wrong *without* a page — the calibration knob ask-or-assume needs) | **Legatus-owned**; centurion may *propose* edits via the permission gate |
| `preferences` | The Legatus's standing habits and review preferences | Legatus-owned; centurion proposes |
| `posture` | Current deadlines, priorities, focus | **Centurion-edited** freely |

The split closes the self-modification loophole (a compaction-degraded centurion must not be able to rewrite his own leash) — Letta's `read_only` blocks and Devin's suggest-and-approve, converged. Anything not needed *every* wake lives as ordinary triggered KB notes — models follow ~150–200 instructions reliably; charter bloat erodes exactly the orders that matter. Adding the `charter` (and `report`) note types is a vector migration **plus three code copies in lockstep** — the reindexer's fallback would otherwise silently rewrite unknown types to `learning` (anchors in [[centurion_implementation_notes]]).

### The project KB — gardened, provenance-tagged, selectively written

The centurion is the KB's **gardener** — which repairs the Resavio audit's "phantom session-KB steering" finding (the KB steered agents while nobody owned it). Gardening is concrete, done on reflection wakes:

- **Evolution** (A-MEM): writing a new note revisits linked notes — update, merge, supersede.
- **Decay by class, as re-ranking not deletion**: charter = infinite; deadlines = event-bound (auto-expire after the date + a review); assumptions = until reviewed; observations/status = days. Supersede, never delete.
- **Write selectivity** (persona rule): record decisions, assumptions, surprises — *not* routine status. Quantified case: an "add-all" agent degraded from 39% → 13% task accuracy as memory grew.
- **Provenance is a trust boundary.** Recon workers process untrusted external content and write notes the officer treats as ground truth — a prompt-injection chain from the object level into command decisions. Worker-authored notes are **`report`s, labeled with their origin job — never standing orders**; only Legatus- and officer-authored notes may steer the charter, priorities, or backlog. Ownership *plus* provenance completes the phantom-steering repair.
- The **current-state brief** (§3) is maintained here on quiet wakes.

### The rest of the state

| State | Store | Notes |
|---|---|---|
| **Assumptions ledger** | KB notes (bi-temporal per §2) | Evidence refs; response windows; Legatus reviews low-confidence first |
| **Backlog** | Existing typed backlog notes (`feature`/`issue`/`idea`, `status='active'`) — already rows, already indexed, already what loop kickoffs inject | The centurion curates and prioritizes; **"completed" is a claim, not a fact** — he verifies (critic verdict, recon check, or artifact presence) before closing a ticket. Today's only close-caller is the campaign disposition; officer mode gives closing judgment an owner. An officer-project "in progress" convention replaces the campaign-derived one |
| **Episodic memory** | RecallStore, project-scoped — extraction already runs in sessions (every 5 turns + pre-compaction + teardown) | Add an **importance score at extraction time** (generative-agents' load-bearing variable — it keeps "the demo is Thursday" retrievable over 300 routine wake summaries). Scored on the aux-LLM side, not officer turns. Overturned assumptions and Legatus overrides are explicit extraction triggers |
| **Jobs / fleet** | The `jobs` and `agents` tables, read through sitreps and tools | Never mirrored into notes — the DB is the truth, the sitrep is the view |

His conversation stays an ordinary compacting session. The stores are the safety net, not the working memory: any wake after any compaction (or a fresh respawn) reconstructs from charter + sitrep + KB search. The same project-keyed routing is what makes conference embodiments (§2) nearly free — the only thread-keyed officer state is the sleep deadline and the sitrep watermark, and both belong to the background loop.

## 6. Authority — capacity and budget, not a permission matrix

The Legatus delegates by resource bounds, not per-action approval (Factory's model: autonomy graded by risk class; the classes live in the charter's `authority` block, so widening autonomy is a charter edit, not a code change):

- **Capacity**: `officer.max_concurrent_workers`. Enforced **server-side in `POST /api/jobs`** — count of non-terminal jobs attributed via the existing `jobs.created_by_thread_id` column (attribution is already a first-class column, no JSONB convention needed), under an advisory lock (closes the parallel-tool-call TOCTOU), returning a 409 whose detail text the delegation tool relays verbatim to the model ("officer capacity 3/3 in use — wait or cancel"). **Capacity is a cap, not a target**: the persona prefers *sequencing* interdependent backlog items over filling slots — parallel workers with partial context make conflicting implicit decisions (Cognition's documented failure mode); interdependent items run sequentially, with decisions-already-made carried into each job spec.
- **Budget**: per-job hardcaps (existing) + the officer's own daily-token ceiling (§4 loop guard). A consolidated officer envelope lands with the deferred headless-budgets feature.
- **Ask-first**: anything external-facing or destructive — deleting jobs/projects/datasources, spending materially above pattern, credentials, infra. Rides the **existing session permission gate** (DB-backed, outlives transport, magic-link email approval already shipped). A blocked ask-first action parks that action, not the officer. (Check in implementation whether the gate can carry an *edited* payload — approve-with-modification is the LangGraph HITL pattern; if not, note as a gap, not a blocker.)
- **The job spec** — every dispatched job carries a template (the single biggest lever on worker output quality; Devin Playbooks + Anthropic's delegation lessons): objective · acceptance criteria · deliverable location · **decisions-already-made** (from the KB decisions log) · forbidden actions · effort class. The persona additionally carries a **routing procedure** (which expert type for which backlog class, when to do nothing, when to stop dispatching — the CrewAI lesson: purpose without a decision procedure degenerates) and **effort-scaling rules** (don't spawn a worker for what a sitrep read answers; recon job only when the KB can't answer; one worker per backlog item unless justified).

### The notify contract (paging the Legatus)

`notify_user` is a **new thin tool** — nothing repointable exists: the worker `send_message` tool is worker-only and its endpoint hard-requires a jobs row. The tool hits a new thread-scoped endpoint into the existing `NotificationService.dispatch()` (channels, quiet hours, digest queue — all shipped; rate-limit precedent already in the codebase). The contract mirrors SRE's three valid monitoring outputs:

| Output | Mechanism | Meaning | Bounds |
|---|---|---|---|
| **Log** | Thread text | Information; no notification | — |
| **Digest** | `notify_user(normal)` → daily digest | Act within a day; questions, absorbed risks, assumptions, the dispatch plan | Default **1/day at quiet-hours end** (configurable ≤3 — batching beyond 3×/day measurably buys nothing); **never zero** — a quiet week still gets "all quiet, N jobs completed" (total silence breeds distrust) |
| **Page** | `notify_user(urgent)` | Action needed from the Legatus **now** | **`max_pages_per_day` default 3** (SRE's ceiling is ~2 urgent incidents/shift; empirical agent-notification ceiling 3–5/day); overflow degrades to digest with "further pages suppressed"; only `urgent` bypasses quiet hours |

Persona rules, verbatim from the alerting literature: *"if there is no action you need from the Legatus, it is not a page"*; *"if a robot could do the response, do it yourself — you have the tools."* Page payloads are SBAR-shaped: situation (symptom, one line) · assessment · action-needed **with deadline and default** · urgency. The officer records page-acted-on outcomes in the KB and calibrates his own threshold over time.

**Job creation is in from v1.** That's the job: he owns the backlog, decides what runs next, waits, schedules, steers. The delegation toolset largely exists — the session-side `orchestrator` tool category has grown to **26 tools** (create/list/get/approve/resume/cancel/pause jobs, project + loop reads, expert/skill catalog) — but ships **opt-in and empty**; `centurion.yaml` must enable it explicitly. v1 adds four small tools: `sleep`, `notify_user`, a steer-running-job wrapper (endpoint + queued-replies plumbing exist; ~30 lines), and a stuck/fleet wrapper (`/api/stats/stuck` exists; ~30 lines).

## 7. Relationship to the existing RSI loop

The mechanical loop auto-creates the next job on completion, with `loop_plan` format compliance as a hard dependency — which MiniMax already fails to emit. The centurion replaces mechanical advance with judgment for the projects he manages, via a third scheduling mode:

- **`scheduling='officer'`**: the loop-advance path, after winning the existing exactly-once stage barrier (which becomes the wake's dedup guard), **enqueues an officer wake instead of materializing the next job** — keeping the per-member merge/retro and user-question notification, skipping iteration decrement, stop-reason evaluation, cooldown park, stage rotation, and the campaign step. He decides what runs next from backlog + sitrep + charter.
- **Three hard guards the code enforces** (all handled, none fatal): the scheduling CHECK constraint needs a migration to admit `'officer'`; the loop-budget CHECK (`max_iterations OR run_until`) is relaxed for officer rows (an officer loop is naturally unbounded); and `scheduling` is deliberately non-updatable after creation — converting a live loop needs a small guarded endpoint that requires an officer thread to exist and **no in-flight campaign** (flipping mid-campaign orphans the campaign cursor).
- **The sweeper's two halves diverge**: the stage-sweep stays — a missed completion hook re-runs the advance, hits the officer branch, and the barrier makes the wake exactly-once (the desired safety net). The torn-advance **heal must skip officer loops** — empty stage pointers are the officer's steady state, and the heal would otherwise restore an all-terminal stage and duplicate-wake him every ~10 minutes, forever.
- Non-officer projects keep the mechanical loop unchanged. One accepted trade-off: the KB-convergence TTL tick lives in the rotation the officer branch skips — officer projects lose cycle-based TTL decay, and the officer-as-gardener (§5) is the replacement mechanism.
- The scholar/critic/curator roles are unchanged and remain workers he can schedule. The critic stays inline QA; "is the critic the officer?" is settled — no: critic is a stage, centurion is a manager. The curator is the closest existing shape for recon jobs (KB-writing deliverable) — recon needs no new expert in v1, only pointed instructions and the `report` note type.

## 8. What v1 is (scope)

One centurion. One project as the first command — the Primus Pilus title attaches later, when a centurion sits on the user's default project and there is more than one centurion for the title to mean anything. Build slices (ordered; step-level anchors, schema sketches, and per-slice risks in [[centurion_implementation_notes]]):

> **Status (2026-07-30): v1 COMPLETE — every slice shipped.** S1–S6 + S5b landed 2026-07-29 (`daf00d80`, `64f51a91`, `6920a135`, `4276ae1e`); the overnight session of 2026-07-30 delivered the rest: **S7** charter + KB types (`ae045186`), **S8** officer scheduling + the `daily_token_ceiling` enforcement + quiet-hours page bypass (`97f2d673`), **S9** conference embodiment + hold + brief wake + officer summary endpoint (`773ad46e`) and the cockpit Centurion tab + session badges (`f7274da0`). k3d-verified live: both migrations applied, full officer→conference→hold→409-rival→end→hold-released→brief-wake-delivered cycle green first try, plain-session regression green. First command still standing: Better Resavio, officer `d67ee261`.
>
> Honest residuals (small, documented in [[centurion_implementation_notes]]): reattaching an idle-**suspended** conference resumes it unheld (the watchdog concluded the hold when it suspended — by design; only the ended→`/resume` path re-holds); the officer digest ring is now *visible* in the cockpit card but still has no daily email sender; charter authoring has no dedicated cockpit UI (it's a KB note — kb_write/kb_update).

1. **S1 — Officer substrate**: `OfficerConfig` (parsed in both loader paths; **denormalized into thread metadata** for SQL visibility); exemptions (attention-sleep, both `awaiting_user` flips, orphaned-`ended` sweep, `"ready"` NATS mirror); retirement semantics (`officer.enabled=false` on deliberate end); `officer_watchdog` sweeper (liveness, overdue force-inject, rate-limited respawn from `suspended`, page on repeated failure); boot self-wake at session attach (the loop bootstrap).
2. **S2 — Sleep tool + external timer**: registered `core` tool + ToolContext request/peek/consume; turn-loop break + `TurnResult.ended_by_sleep`; the tool files the wake-up call with the orchestrator (pending `timer` outbox row with `fire_at`); officer branch of the input wait reduces to a long labeled backstop (never `IdleTimeoutError`); watchdog files implicit `sleep_max` when no timer is pending.
3. **S3 — Event outbox + sitrep**: `session_wake_events` migration (insert-dedup, claim/debounce/GC); refactored delivery helpers + `notify_officer` / `notify_all_officers`; drain that coalesces per thread into one sitrep; `orchestrator/services/sitrep.py` (cross-DB, per-section degradation, snapshot-diff fingerprints, watermark-on-live-delivery-only); legacy-outbox double-wake suppression.
4. **S4 — Wake call sites**: completion hooks, cancel/approve/escalate/pause endpoints, sudo-gate pending path + VM-upgrade insert, stale-agent sweep fleet events.
5. **S5 — Toolset + authority**: enable the `orchestrator` category; `notify_user` tool + thread-scoped endpoint into NotificationService (three-output contract + page budget); steer + stuck/fleet wrappers; capacity enforcement in `POST /api/jobs` (advisory lock, actionable 409); officer loop guard (action budget, repeated-action fingerprint, daily token ceiling).
5b. **S5b — Slot roster** *(added + shipped 2026-07-29, user design)*: the officer's capacity as a typed kit rather than a scalar — `officer.slots: {name: {count, model, backend}}` assigned by the Legatus at provision (hard-validated; a typo'd kit 400s at create). The officer names a slot per dispatch (`create_worker_job(slot=...)`); the job funnel resolves it under the capacity lock, **stamps the slot's model/backend over the job config** (the assignment wins over whatever the tool call carried), records the slot on the job, and enforces per-slot counts with actionable 409s that list the roster with free counts. Sitrep capacity reads per-slot ("heavy 0/1, line 1/2"). The flat cap remains for roster-less officers. Slots are allocation, not privilege — the grants PDP (`model_selection`, `can_use_vm`) still ceilings the stamped config. Pure logic in `orchestrator/services/officer_slots.py`.
6. **S6 — Expert config + persona**: `config/experts/centurion.yaml` (`none` backend, officer block, orchestrator + kb toolset, codex-proxy catalog model); charge-not-goal persona (standing questions, ask-or-assume with confidence/stakes, communication contract, routing procedure, effort scaling, write selectivity, micro-standup).
7. **S7 — Charter + KB types**: vector migration for `charter` + `report` note types (three lockstep code sites); dedicated unconditional charter-injection block; charter template (intent skeleton, block ownership); provenance labeling for worker-authored notes.
8. **S8 — Scheduling mode `officer`**: CHECK migrations, advance-branch, sweeper heal guard, start-endpoint skip-first-spawn, guarded conversion endpoint.
9. **S9 — Conference surface**: conference sessions (a normal interactive session created with the centurion expert + project binding — identity attaches via the project-scoped stores; workspace tier user-selected at creation); the conference hold + session-brief wake; one-open-conference-per-project guard; cockpit routing (single-select project picker → the conference, replacing the legacy multi-project tap-in; officer badge; event-collapse rendering of the log; project-page Centurion enable/disable toggle → provision/retire).

Sequencing: S1+S2 first (agent-side substrate), S3 before S4 (call sites need the outbox), S5–S7 parallelizable, S8 last (needs an officer to wake), S9's brief-wake rides on S3 while its cockpit half can land anytime. Cockpit work in v1 is otherwise minimal: the officer thread renders in the existing session UI — that *is* the officer's log. The fleet view and a Legatus-facing "needs you" inbox (the validated ambient-agent UX) are polish, not gates.

### Acceptance scenarios (the doc's definition of "works")

1. **Stuck job.** A worker wedges (step count frozen). Within ≤3 wake cycles the centurion acts — resume-with-feedback, pause + reschedule, or page with a diagnosis — with no human prompt.
2. **Infra outage.** Workers stop becoming ready (headscale-latch class). He detects the pattern across wakes, parks new dispatches, and pages the Legatus with what he observed and what he needs.
3. **"What's the current state?"** — answered from charter + current-state brief + sitrep, grounded and current.
4. **"We present next week — be prepared."** — deadline enters the charter; backlog visibly re-prioritizes toward demo-critical gaps (UI, deployment); jobs dispatch accordingly; he reports the plan, his assumptions, and what he needs from the user.
5. **Idle capacity.** Backlog non-empty, capacity free → the sitrep flags it mechanically → he pulls the next ticket and schedules it, unprompted.
6. **Quiet operation.** Nothing wrong → timer wakes conclude in "sleep(max)" at warm-cache cost, no pages, and the daily digest still arrives ("all quiet").
7. **Hollow completion.** A worker reports `completed` but the deliverable is absent or wrong. The centurion treats the status as a claim — verifies via critic/recon/artifact before closing the backlog ticket, and reopens or re-dispatches on failure.
8. **Conference.** The Legatus opens a conference, walks the centurion through a pivot in the shared workspace, and ends the session. The background officer's next wake carries the session brief; his subsequent scheduling reflects the new direction with no re-explanation — and no event that arrived mid-conference is lost.

### Prerequisite (Phase 0) — resolved

The original prerequisite — the stale-agent detector SQL crash that killed recovery sweeps — is **already fixed** (per-step isolation in the sweep loop; `docs/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md`). Remaining Phase-0 work is verification only: the officer's signal paths (stale sweeps, orphan recovery, wake outbox) get a live k3d exercise before S4 lands call sites on them.

## 9. Deferred — the legion

Parked deliberately, revisited when scale demands (the notes' own staffing rule — "every ~20 agents get officers" — implies today's scale needs exactly one officer):

- **Primus Pilus command extension.** The centurion of the user's default project, extended beyond the title: authority to create and manage entire projects — each with its own centurion — and to delegate work across officers. The growth path for every cross-project concern (fleet health, infra outages, portfolio prioritization): they route up to the Pilus, not into per-project scope creep. Requires a first-class "default project" notion (§10) and an officer-to-officer messaging path.
- **Century structure** (Tesserarius / Optio as separate agents). In v1 the three command functions are sections of one centurion's prompt.
- **Model tiering** (small-model line centuries, top-tier command; Caesar/Centurion fine-tunes) — a compute-economics story, orthogonal to autonomy.
- **Multi-officer staffing per project**; **concurrent embodiments** (a live conference while the background loop keeps acting — a one-flag relaxation of the serial hold) and **multi-user conferences**; **fleet view UI** (the Warlords-Britannia overview); **Legatus "needs you" inbox**; **officer envelope budgets** (with headless-budgets); **`LifecycleEvent` bus** (automations v0.5 — wake call sites migrate onto it mechanically when it lands); **session permission-gate wake source** (needs a pg trigger or agent-side hook; sitrep pending-count covers v1).

## 10. Open questions & decisions pending

**Decisions pending (Legatus):**

1. **Crash-ended resurrection policy.** *(RESOLVED by implementation, 2026-07-29: page-and-wait shipped as recommended — graceful agent-side 'ended' maps to 'suspended' and respawns; true crash-'ended' with the flag raised pages instead of auto-resurrecting.)*
2. **First command & loop conversion timing.** *(RESOLVED 2026-07-30: the Legatus directed "implement and test all of them" — S8 is built. Both shapes now exist: the officer owns dispatch directly (Resavio today), and a century can run `scheduling='officer'` where each concluded loop turn wakes him instead of auto-advancing; a live loop converts one-way via `POST .../loop/scheduling`.)*
3. **Page budget default** — *(RESOLVED by implementation: 3/day default, over-budget pages downgrade to digest (2026-07-29); pages bypass quiet hours — the one urgency that does (2026-07-30).)*

**Open questions (implementation-time):**

4. **Verification depth for hollow completions** — always spawn the critic vs. artifact-presence check first with critic on suspicion. Cost/latency trade; decide from live data.
5. **Escalation model** — when does a centurion consult a stronger metered model? v1: never; revisit with transcripts.
6. **"Default project" definition** — needed only when the Primus Pilus designation gains behavior. Candidates: a flag on `projects`, or `users.settings`.
7. **Reflection-wake trigger tuning** — event-weight threshold and daily floor; start at ~1–2/day and calibrate.

*(Closed: sleep-timer durability — orchestrator-owned durable timer rows in the event outbox, firing included, not just storage (user decision 07-29); sitrep cost shape — tiered fingerprints, ~8 queries, 300–900 tokens; recon-job ergonomics — curator shape + `report` note type, no new expert.)*

## 11. Decision log

- **2026-07-28:** while(true) on the subscription model is the architecture, not a compromise — grounded in measured surplus (40h of gpt-5.6-sol ≈ 40 percentage points of a cycle; resets expire unused). (User decision.)
- **2026-07-28:** `sleep(minutes)` is a *tool the model calls*, bounded by settings; timer wakes fire even when no event arrives — never trust the event you're waiting for. Stuck detection = sitrep deltas (mechanical) + officer judgment. (User design.)
- **2026-07-28:** No new state system. Charter + working model + assumptions in the project KB (the centurion as KB gardener), episodes in RecallStore, backlog as the existing typed KB notes, jobs/fleet in the DB. Context is ephemeral by design. (Joint.)
- **2026-07-28:** The officer's defining trait is *absence of task pressure*: charge + standing questions + time-awareness + ask-or-assume, no terminal state. Direct response to the Resavio month. (User framing.)
- **2026-07-28:** Job creation and full backlog authority from v1, bounded by capacity + budget rather than a per-action permission matrix. A manager without scheduling authority is a dashboard. (User decision.)
- **2026-07-28:** One authoritative loop per centurion; user messages are wake events into it. Shared-identity multi-session deferred. (Recommendation, accepted.)
- **2026-07-28:** Naming hierarchy settled: per-project officer = **Centurion**; the default project's centurion bears the title **Primus Pilus** (designation now, command extension later); the user is the **Legatus**. Projects are centuries. Code uses `officer`. (User decision.)
- **2026-07-29:** **Timer wakes are orchestrator-fired and Postgres-durable** (user decision): the sleep tool files a pending `timer` outbox row with `fire_at`; the drain fires it like any other event; the watchdog files implicit `sleep_max`; the agent keeps only a long labeled backstop. Rationale: a pod crash or node downtime must not lose the schedule — the next pod picks up the timer where it was. Bonus: the conference hold becomes uniform across all wake paths, and timers inherit debounce/coalescing/urgency. Supersedes the agent-local deadline + metadata mirror from the 07-28 hardening.
- **2026-07-29:** Interaction surface settled (user design): you talk to a centurion in a **conference** — a separate interactive session wearing his identity through the project-scoped stores, with a workspace/VM allowed for shared code walkthroughs. The background loop is **held** during the conference (events durable-queue; scheduling actions mechanically fenced) and wakes once afterward with the session brief. Serial embodiments, single writer; quick notes may still be typed into the log as wake events. Supersedes the 07-28 one-loop note insofar as user *conversations* move out of the background thread; the log remains his timeline. Cockpit: the legacy multi-project session tap-in narrows to a single-select picker that routes to the conference; the project page owns Centurion enable/disable. (User design; hold-as-default refinement accepted.)
- **2026-07-29:** **Slot roster** (user design, S5b): the officer's capacity is a typed kit, not a scalar — named slots with count + model + backend assigned by the Legatus at provision; the officer names a slot per dispatch and the funnel stamps and enforces. The division of authority in one line: *he chooses which troops to send; the Legatus decides what they are made of.* Designed and shipped the same evening.
- **2026-07-30:** **v1 completed in one overnight session** (Legatus directive: "implement and test all of them; the order is yours"). S7: `charter`/`report` note types with the charter as an unconditionally injected, per-turn-re-pinned block; workers refused charter writes (trust boundary); one active charter per project enforced at the write path; lite-session KB writes now fail loud when pgvector is the sole store (risk 11). S8: officer scheduling per §7 with a one-way live-conversion endpoint; a failing turn never stops an officer loop — judgment does. Loop-guard layer 3: `daily_token_ceiling` enforced at the drain (defer to UTC reset + one digest notice; fail-open; Legatus input unaffected). §6 completed: pages cross quiet hours. S9: conference embodiment with a uniform hold (claim-query skip + watchdog stand-down + dispatch fence), brief wake on end with watchdog self-heal (incl. idle-suspended conferences — the officer is never held all night), `GET /api/projects/{id}/officer`, and the cockpit Centurion tab (provision with slot-roster builder, status/next-wake/pages/digest, conference + retire) + officer/conference badges on session rows.
- **2026-07-29:** **First command**: Better Resavio — the century whose month under the mechanical loop motivated this feature — placed under centurion `d67ee261` with kit `line: 2×MiniMax-M3/vm` + `heavy: 1×gpt-5.6-sol/vm`, bounds 10/45. His first watch, unprompted: an honest change-of-command state note (7/7 probes and 145/145 tests stand; the Demo Gap; doc drift; "the loop's shipped claims are unverified"), a queued digest, zero dispatches, and **the Demo Definition question** — the exact failure the feature was designed around, surfaced as a question to the Legatus rather than a guess. The Legatus's answer (delivered 2026-07-29): web UI **required** — 2–3 real front-desk flows over `kurort_engine`, one-command start with demo data, standing one-week demo readiness; truth-first sequencing (hygiene + claims-to-facts verification before construction) approved; heavy slot for the UI architecture choice, line for build-out.
- **2026-07-28 (research hardening):** Five-agent fan-out integrated. Added: event-outbox table (the existing session-wake outbox is job-keyed and cannot carry officer wakes), reflection wakes, charter block structure with split write authority and governance pinning, KB provenance boundary, officer loop guard, three-output notify contract with page budget, job-spec template, completed-is-a-claim verification, DB-authoritative sleep deadline, cache-aligned cadence, retirement semantics. Corrected: lazy loop start (respawn wake = bootstrap), `updated_at`/progress/token data sources (audit DB), sweeper-heal conflict, double-wake suppression, Phase 0 already fixed. No core decision refuted.

## 12. Prior art (compact)

- **Letta (MemGPT)** — memory blocks as pinned core memory; `read_only` blocks; sleep-time agents (background consolidation, trigger-on-compaction); sleep-time compute (~5× when queries are predictable). The charter and reflection-wake designs.
- **OpenClaw** — 30-min heartbeat into the main session, `HEARTBEAT.md` standing orders, suppressed no-op wakes, cache-TTL-aligned cadence. The closest open-source analog of the whole wake loop.
- **Dust wake-ups / Workflow SDK** — `sleep`/self-scheduling as a model-called tool with same-thread continuation; shipped commercial practice.
- **Devin** — sleep/@-wake in Slack, in-thread chatter vs. explicit notifications, confidence-scored ask-vs-proceed (2× merge-rate correlation), Knowledge (pinned vs. triggered), Playbooks (job-spec shape).
- **LangGraph ambient agents** — notify/question/review triad; triage before agent loop; agent-inbox UX. **Temporal/Restate/Inngest** — signals + durable timers + debounce/throttle/batch as the standard wake substrate.
- **Claude Code agent teams** — documented manager-does-work and hollow-completion failure modes; teammate-idle notifications. **CrewAI/AutoGen** — documented manager-degeneration and loop incidents; the case for routing procedures and action budgets. **Anthropic multi-agent research system** — delegation-spec and effort-scaling lessons; rainbow-deploy survival.
- **Vending-Bench** (arXiv 2502.15840) — long-horizon meltdowns from state misinterpretation, not context exhaustion → DB-truth sitreps + orchestrator-side action validation. **Governance-decay** (arXiv 2606.22528) — compaction strips constraints (0%→30–59%); pinning restores 0% → charter pins governance. **Goal-drift** (AIES 2025) — drift-through-inaction → mechanical idle-capacity flag. **Memory-injection** (arXiv 2607.05189) — KB provenance boundary.
- **Stanford generative agents** — reflection cadence, importance scoring, evidence-linked insights. **Zep/Graphiti, A-MEM, Mem0** — bi-temporal supersede-never-delete, memory evolution, TTL bands, write selectivity (39%→13% add-all degradation).
- **Google SRE / PagerDuty / interruption research** — actionable-page principle, symptom-first, 5-min grouping windows, urgency never downgraded, 2–3 pages/day ceilings, 3×/day batching optimum, never-zero visibility. **Mission command doctrine / SITREP / SBAR** — intent (purpose · key tasks · end state), disciplined initiative, risk acceptance, delta-only numbered sitreps, SBAR-shaped pages.

## Sources

- `Officers.md` (repo root) — consolidated officer notes: concept, legion roles, staffing rules, model tiering, the four-officer-types principle.
- [[centurion_implementation_notes]] — the anchored build plan distilled from the code-planner research (S1–S8 steps, schema sketches, risks, blast radius).
- `docs/features/agent_lifecycle.md` — persistent/worker two-mode architecture; delegation-tools decision.
- `docs/features/headless_persistent_sessions.md` — shipped substrate: transport-independent loop, event log, DB permission gates, notification fan-out + magic links, attention sleep (officer exemptions per §4).
- `docs/features/automations.md` / `automations_v0.md` — cron dispatcher (live) and the dormant event-trigger schema the wake bus eventually shares.
- `docs/features/loop_campaign_scheduling.md`, `docs/features/loop_unified_engine.md` — the mechanical scheduler `scheduling='officer'` replaces per-project.
- `docs/features/no_workspace_agent_mode.md` — the lite backend that structurally enforces klug-und-faul.
- `docs/features/notify_user_tool.md` — design-only predecessor of `notify_user`; superseded by §6's thread-scoped contract.
- `docs/features/platform_for_agents.md` — strategic frame; the officer is the first heavyweight consumer of the management surface.
