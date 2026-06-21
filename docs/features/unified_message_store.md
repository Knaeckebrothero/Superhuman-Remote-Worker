# Unified message store — converging worker `chat_history` and session `thread_messages`

**Status: Idea / exploration — NOT decided (2026-06-21).** This is an
idea-capture from the database-schema-optimization discussion, not a plan. It
records the current state, why the obvious move ("merge the two tables") is
wrong, the two real directions, and the open questions. Nothing here is
committed; treat it as the seed for a future design call.

**Origin:** 2026-06-21 schema-optimization conversation, following the
database-architecture review. The convergence itself is already named (but
deferred) in `database_architecture.md` ("one message store") and listed as a
non-goal-owned-elsewhere in `../issues/db_schema_hygiene.md`. The byte-parity
audit-store migration was kept deliberately to keep this door open
(`postgres_audit_store_implementation.md`).

Companion docs: `database_architecture.md` (the tier rules this must respect),
`persistent_session_source_of_truth.md` (why `thread_messages` is shaped the
way it is), `../issues/persistent_session_midturn_message_loss.md` (the
message-granular persistence work that made `thread_messages` the rich store it
is today).

## The idea in one sentence

Agent conversation is currently stored two completely different ways depending
on *how* the agent ran (batch worker job vs. interactive session); this asks
whether they should converge on **one message model / store / render path**.

## Current state — there are three "message" things, not two

The split runs along two axes — *execution mode* and *role*:

| | Conversational **source of truth** | **Observability** projection |
|---|---|---|
| **Worker jobs** | LangGraph checkpoint (AsyncSqliteSaver — SQLite, per-pod, ephemeral, not queryable by cockpit) | `chat_history` (audit tier) |
| **Sessions** | `thread_messages` (control plane) | *— none —* (renders straight from the truth store) |

The naming in the prompt ("agent messages and chat history") maps to
`thread_messages` + `chat_history`. The **hidden third store is the
checkpoint** — and it matters, because for worker jobs `chat_history` is *not*
the truth, it's a lossy downstream copy. The execution truth is the checkpoint.

### Why these are not the same kind of object

| | `thread_messages` (session truth) | `chat_history` (worker audit) |
|---|---|---|
| Tier / store | Control plane (`srw-postgres`) — load-bearing | Observability (`srw-auditdb`) — non-load-bearing |
| Keyed by | `thread_id` | `job_id` |
| Granularity | **per message** (one row per LangChain iteration) | **per turn** (inputs = messages since last AIMessage) |
| Fidelity | full content + `provider_raw` verbatim + normalized components (`reasoning`/`tool_calls`/`tool_results`, migr. 0019) + `seq` resume cursor (0023) | truncated previews (`content_preview<=500`, `args_preview<=200`) |
| Retention | lifecycle of the thread (FK CASCADE), effectively forever | 365-day partition drop |
| Partitioned | No | Yes (monthly) |
| FKs | FK → `threads` | none (audit invariant) |
| Read by | **resume** (load-bearing live state) + cockpit | cockpit job-chat pane only |

**Write paths (verified 2026-06-21):**
- `thread_messages` — session path only: `src/persistent_graph.py`,
  `src/api/persistent_app.py`, `src/database/postgres_db.py`.
- `chat_history` — worker path only: `src/core/archiver.py` +
  `src/database/audit_writer.py`, via `archive(call_type='main')`. Explicitly
  worker-loop-only ("Only write to chat_history for main loop calls").
- The two stores **do not overlap** today. This is divergence, not duplication.

## Why "just merge the two tables" is wrong

They live in different tiers *by the architecture forcing functions*
(failure-domain isolation + retention profile, see `database_architecture.md`):

- Pull `chat_history` into the control plane → firehose volume + 365-day-drop
  data sitting next to the PITR-forever crown jewels.
- Push `thread_messages` into the audit tier → sessions now depend on a
  non-load-bearing store for resume.

So the table merge is a non-starter. The real question is about **the model and
the ownership**, not the physical rows.

## The existence proof: sessions already do this

Sessions **do not write `chat_history` at all.** They render the chat pane
directly from their source-of-truth store (`thread_messages`) because it's
richer than the audit projection. So convergence isn't hypothetical — it's
"make worker jobs work the way sessions already do."

(Note: worker `chat_history` *is* currently load-bearing for the **cockpit
job-chat render**, even though it's not load-bearing for execution — the
checkpoint is SQLite/per-pod and cockpit can't read it. So dropping
`chat_history` requires a replacement render source first.)

## The two real directions

### A — Converge the model + code, keep storage split (cheap)

One canonical Message representation (the 0019 normalized-components shape is
already the good candidate) + one writer interface + one cockpit renderer.
`thread_messages` stays session truth; `chat_history` stays the worker audit
projection — but they stop being separately-shaped.

- **Win:** kills the divergent render paths, the reasoning-capture forks, the
  formatter duplication (a recurring bug source — see the reasoning-capture and
  tool-pairing issue history).
- **Cost:** low; mostly a refactor.
- **Does not** give worker jobs resume, doesn't touch the tier rules.

### B — Worker jobs adopt `thread_messages` as conversational truth (the product prize)

Jobs get a thread (or the job/thread models converge). Worker conversation
persists per-message to the control-plane store → worker jobs become
**faithfully resumable like sessions**, one store, one render path.
`chat_history` then degrades to a pure derived view or is dropped (full bodies
already live in `llm_requests` at 90-day retention).

- **Win:** worker-job resumability + a single product surface (jobs and
  sessions as one thing). Subsumes A.
- **Cost:** high — firehose volume into the control plane, a real retention
  decision, and the job↔thread model merge. Gated on the tier/failure-domain
  call.

## Open questions (the undecided part)

1. **Which prize / what's the driver?** Code de-dup (A) or worker-resume +
   one-surface (B)? Is there concrete pain (a specific bug, a "resume a worker
   job" feature ask), or is this pure optimization opportunity? This decides
   the size of everything below.
2. **Canonical message shape.** Adopt the 0019 normalized-components shape
   (`reasoning`/`tool_calls`/`tool_results` + `provider_raw`) as the one true
   shape for live state, the session store, AND the audit projection? How is
   `chat_history`'s per-turn-delta granularity reconciled with
   `thread_messages`' per-message granularity?
3. **(B) Where do completed-job messages live?** Forever in the control plane?
   Partition `thread_messages`? Archive-to-audit on job completion and keep the
   control-plane copy short? This is the tier/retention tension that B can't
   dodge.
4. **(B) Job ↔ thread model.** Does a job *become* a thread, or does a job
   *get* a `thread_id`? What does that mean for the `jobs` vs `threads` tables,
   autonomy levels, the dispatcher, freeze/complete?
5. **(B) Failure domain.** Is it acceptable for worker write volume to land on
   the load-bearing control plane (vs. today's audit-tier / ephemeral
   checkpoint)? Does this change the HA priority order?
6. **Checkpoint relationship.** The LangGraph checkpoint carries *full graph
   state* (todos, phase_number, freeze_data, …), not just messages. Unifying
   the *message* store does **not** remove the checkpoint — at most it gives
   messages a durable, queryable home alongside it. Clarify the intended
   division of labor: checkpoint = execution state, unified store = the message
   record?
7. **Fate of `chat_history`.** Dropped, or kept as a derived 365-day
   observability view? `llm_requests` already holds full request/response
   bodies at 90 days — does anything actually need the conversational delta
   specifically at 365 days?
8. **Cockpit.** Collapse `historyToTurns` (session) and the job-chat render
   into one path. Likely cheap once the model is canonical, but needs its own
   pass.

## Tentative lean (not a decision)

Even if B is the eventual goal, **A looks worth doing first**: the canonical
message model is the shared substrate B needs anyway, and it pays for itself
immediately by collapsing duplicated render/capture code. B then becomes "give
worker jobs a durable home for that model + a thread," not a from-scratch
design. But this is undecided — revisit when there's a driver (Q1).
