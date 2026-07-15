---
tags:
  - feature
  - sessions
  - cockpit
  - agent
  - message-state
  - workspace
aliases:
  - rewind
  - /rewind
  - conversation rewind
  - restore checkpoint
  - undo a sent message
  - go back to an earlier message
related:
  - "[[persistent_session_source_of_truth]]"
  - "[[unified_message_store]]"
  - "[[persistent_session_history_windowing_and_compaction]]"
  - "[[database_roadmap]]"
  - "[[source_tree_unification]]"
  - "[[sessions]]"
  - "[[session_turn_rendering]]"
  - "[[context_summarization_rework]]"
---

# Session Rewind — go back to an earlier message

Concept document for adding a **rewind** capability to persistent (interactive)
sessions: let the user pick a message they sent earlier, revert the session to that
point, and re-drive from there with a different prompt. The model is Claude Code's
`/rewind`.

> **Status (2026-07-15): PARKED — concept capture only. No decisions taken.**
> This doc deliberately stops short of choosing an implementation. It exists to
> preserve the prior-art research and the architecture mapping so the eventual design
> call is fast, and to record *why* the call is being deferred.
>
> **Sequenced behind two pieces of work, both by owner decision (2026-07-15):**
> 1. [[source_tree_unification]] — the src-layout flatten. Every file anchor in this
>    doc is pre-flatten and will move.
> 2. **The message-model unification** — [[unified_message_store]], tracked as Phase 7
>    of [[database_roadmap]] (the last open phase). This is the load-bearing one:
>    rewind is a *transcript* operation, and unification decides what the transcript
>    model even is. See [Why this is sequenced](#why-this-is-sequenced-behind-the-unification).
>
> When those land, the follow-up is a design call over the [design space](#the-design-space--all-undecided)
> below, then an execution plan.

## Motivation

The user story is small and familiar to anyone who uses Claude Code:

> I sent a message. The session went down a bad path — bad reasoning, a wrong
> assumption, an edit I didn't want. I want to go back to *before* that message,
> fix how I asked, and run it again — without throwing the whole session away.

Today the only revert affordance in a session is the file-scoped `undo` (see
[Existing primitives](#existing-primitives-worth-reusing)), which restores files
touched in the last turn and does nothing to the conversation. There is **no way to
un-send a message**, no way to edit-and-resend, and no way to get back to an earlier
point in a long session. The alternatives are to argue the model out of its own
context (which leaves the bad turn in-context, still steering it) or to start a new
session (which throws away everything).

This matters more for us than for a CLI: our sessions are long-lived, durable, and
resumable across pods. A bad turn is a *persistent* liability, not something you drop
by closing a terminal.

---

## Prior art — what Claude Code's rewind actually does

Researched against the official docs (`code.claude.com/docs/en/checkpointing`,
`.../interactive-mode`, `.../context-window`) on 2026-07-14. Recording the real
mechanism here so we don't re-derive it later.

**Trigger.** Double-`Esc` on an empty prompt, or the `/rewind` command.

**Checkpoint creation.** Two triggers, both cheap and lazy — **before every user
prompt**, and **before every file edit**. Checkpoints are stored inline in the
session transcript (the JSONL), so they survive `--resume`. Cleaned up with the
session on a 30-day retention (`cleanupPeriodDays`).

**The picker** lists past **user prompts** chronologically. Selecting one offers six
actions:

| Action | Effect |
|---|---|
| Restore code and conversation | Reverts both file edits and messages to that point |
| Restore conversation | Messages only; current file state untouched |
| Restore code | File edits only; conversation untouched |
| Summarize from here | Compresses messages before the point into a summary |
| Summarize up to here | Same compression, different resulting cursor position |
| Never mind | Cancel |

Two observations that matter for us:

1. **The "summarize" pair is not undo — it's compaction** wearing the rewind picker's
   UI. We already have that capability ([[persistent_session_history_windowing_and_compaction]],
   [[context_summarization_rework]]); it's a separable question whether to surface it
   through the same affordance.
2. **Restoring drops the original prompt back into the input box**, so the user edits
   and resends. Small touch, most of the ergonomic value.

**Documented limitations** (worth adopting wholesale as our posture — they're
inherent, not sloppiness):

- Only files touched by **Claude's own edit tools** are reverted. Bash side effects
  (`rm`, `mv`, `cp`) are **permanent**.
- Files edited outside the session, or by a concurrent session, are not captured.
- Actions with external side effects (DB writes, API calls, deploys, SSH to a remote)
  cannot be checkpointed at all.
- It is **session-local and not a VCS replacement**.

---

## Where this lands in our architecture

Mapped 2026-07-14 against `develop`. The single most important finding:

> **Persistent sessions have no LangGraph checkpointer.** The transcript *is* the
> state.

The checkpointer (`AsyncSqliteSaver` / `AsyncPostgresSaver`, keyed by job id) is the
**worker-job** world (`src/graph.py`, `src/agent.py::_make_checkpointer`). Interactive
sessions (`src/persistent_graph.py`) are a plain async loop — no `StateGraph`, no
`compile`, no checkpointer. Their only durable state is the `thread_messages`
transcript, ordered by `seq`, with compaction persisted as `role='summary'` rows
carrying a `boundary_seq`.

This cuts both ways:

- **Good:** conversation rewind is a *transcript* operation, not checkpoint surgery.
  There's no opaque state blob to seek inside. "Go back to message X" reduces to
  "make the transcript and the live message list stop at X's `seq`."
- **Hard:** the transcript is **append-only by invariant**. Compaction never deletes
  rows; SSE replay depends on the ordering. "Truncate history" is therefore a
  genuinely new capability that has to be designed in deliberately — it is not a
  `DELETE`. This is the crux, and it's precisely what the unification will re-open.

### Two independent axes

Rewind is really two features that happen to share a picker:

1. **Conversation state** — the `thread_messages` transcript + the running agent's
   in-memory `_session.messages` + any compaction summary rows. Tractable.
2. **Workspace file state** — the remote SSH workspace. This is the hard half, and
   it's exactly where Claude Code draws its "we don't track bash" line too.

They can ship independently and probably should be decided independently.

### The in-memory authority wrinkle

Per [[persistent_session_source_of_truth]] (Plans 2–4 still pending), **the running
agent's in-memory message list is still the live authority** — the transcript is
durable, but it is not yet what the agent reasons from. Consequence: for an
*attached* session, a DB-side revert alone does nothing. The agent would keep
responding with the full in-context history and re-persist from it.

So rewind for a live session must mutate the agent's in-memory state, not just the
DB. (For a *detached/idle/ended* session, a DB-side revert alone is sufficient — the
next attach rehydrates from the transcript.) If `source_of_truth` Plans 2–4 land
first, this wrinkle shrinks considerably — another sequencing interaction worth
noting.

### Existing primitives worth reusing

| Primitive | Location | Fit |
|---|---|---|
| `get_seq_for_message_id` | `src/database/postgres_db.py:486` | **Direct fit.** Already resolves a message id → `seq` server-side, which is exactly the id→cursor lookup a rewind endpoint needs. |
| `seq` cursor (migration 0023) | `thread_messages.seq` | The natural revert cursor. Monotonic, tie-free, indexed `(thread_id, seq)`. **Not currently exposed to the cockpit.** |
| `UserTurn.id` = `thread_messages.id` | `cockpit/.../persistent-chat.service.ts:3257` | The picker needs no new identity model — frontend turns already carry the real DB row id. |
| `undo` WS verb | `src/api/persistent_app.py:2813` | The sibling pattern a `rewind` verb would follow. |
| `file_checkpoints` / `undo_turn` | `src/api/persistent_session.py:246` / `:1402` | Right *shape* for workspace revert (snapshot-before-write, restore-or-delete), wrong durability — see below. |
| `_restore_session_messages` | `src/api/persistent_app.py:4756` | The rehydrate-from-transcript path; the natural place a reverted state becomes authoritative on re-attach. |
| `GitManager` / `git_versioning` | `src/core/workspace.py:281` | Already wired; sessions already commit on auto-compaction. Per-turn commits would be an extension, not new infra. |

**On `undo_turn` specifically** (verified 2026-07-15): it is in-memory only
(`Dict[turn_id, List[snapshot]]`), **one-shot** (it `pop()`s the entry), covers only
files touched by write/edit tools, and is **lost on pod restart or resume**. It is a
useful sketch of the workspace-revert shape, not a foundation to build on as-is.

---

## The design space — all undecided

Recorded as options with trade-offs. **Nothing here is chosen.**

### Axis A — transcript revert semantics

The append-only invariant has to give somewhere. Three shapes:

- **A1 — Hard delete** rows with `seq >= target`. Simplest. Breaks the append-only
  invariant outright, destroys history (note Claude Code *keeps* the transcript even
  when restoring), and leaves `thread_events` epoch replay inconsistent. Cheap now,
  probably regretted.
- **A2 — Tombstone / soft-delete.** A nullable marker (`superseded_at`, or a
  `rewind_epoch` counter) on message rows; reads filter to live rows. Preserves
  history, auditable, reversible, and consistent with the codebase's existing
  "never delete, even on compaction" ethos. New post-rewind messages get higher `seq`
  values that sort correctly after the survivors, so the existing index still works.
  Needs a rule for summary rows whose `boundary_seq` sits past the revert point.
- **A3 — Branch / epoch dimension.** A rewind *forks* a navigable branch from the
  target. Most powerful — you could flip between attempts like git, which is arguably
  the better product. Most invasive: every read, write, compaction boundary, SSE
  epoch, resume path, and the frontend IndexedDB cache become branch-aware.

**This axis is the one most entangled with the unification** — see below.

### Axis B — where the revert is applied

- **B1 — Live agent verb.** A `rewind` WS control verb (sibling of `undo`) that
  interrupts any in-flight turn, truncates `_session.messages`, resets `turn_count`,
  drains the pending queue, and writes the DB-side revert.
- **B2 — DB-side + rehydrate.** Revert in the DB, then force the agent to reload via
  `_restore_session_messages`.
- Likely both, split by session state (attached → B1, detached → B2). Constrained by
  the in-memory-authority wrinkle above, which [[persistent_session_source_of_truth]]
  may dissolve first.

### Axis C — workspace revert

- **C1 — Don't.** Conversation-only rewind, explicitly labelled. Matches Claude
  Code's "restore conversation" and is arguably the 80% of the value.
- **C2 — Durable file snapshots.** Persist the `file_checkpoints` model keyed by
  `seq`/`turn_number` so it survives resume. Matches Claude Code exactly — limitations
  and all (write/edit tools only).
- **C3 — Git-per-turn.** Commit per user-turn with a `seq`-keyed ref; revert via
  `git reset --hard <ref>`. More robust (catches shell-created files if `git add -A`),
  survives restarts, reuses existing infra. Costs: doesn't exist for lite/virtual
  sessions (`git_versioning=False`), and `reset --hard` is itself destructive of
  concurrent uncommitted work.
- Under all three, bash side effects against external systems stay out of scope. That
  limitation is inherent; the question is only how loudly the UI says so.

### Axis D — UX surface

- **D1 — Inline affordance** on each user message ("rewind to here" / "edit &
  resend"). Chat-native; the frontend already has the row ids to anchor it.
- **D2 — A picker/list**, closer to Claude Code's TUI model. Possibly better for very
  long sessions where scrolling back is the real cost — note the render is already
  windowed (`visibleTurns`, `loadOlderTurns`).
- Sub-questions regardless of shape: restore the original prompt into the composer
  (Claude Code does; it's most of the ergonomics); which restore modes to expose and
  how to degrade them for lite sessions; whether compaction ("summarize up to here")
  shares the affordance.

### Axis E — interactions to design against

- **In-flight turns.** Rewind mid-stream must interrupt first; the frontend outbox is
  single-flight and would need draining/cancelling.
- **Compaction summaries.** A revert to a point *before* a `boundary_seq` invalidates
  that summary (it summarizes partly-reverted content). Superseding those summary rows
  appears self-consistent — the pre-target originals were never reverted, so replay
  still works — but this needs to be worked through properly, not asserted.
- **Multi-viewer resync.** The `events_epoch` / `GONE_BEYOND_HORIZON` path is the
  existing lever for forcing other viewers to repaint.
- **Ownership.** Rewind is destructive-ish; presumably owner-only, like resume.

---

## Why this is sequenced behind the unification

Not just scheduling — there's a real design dependency, and it runs through Axis A.

[[unified_message_store]] is currently *"Idea / exploration — NOT decided"*. Its two
directions bear directly on rewind:

- **Direction A** (converge the model + code, keep storage split) would establish
  **one canonical message shape**. Its open question #2 is literally *"what is the
  canonical message shape?"* — and a rewind tombstone/branch dimension (Axis A) is a
  field in exactly that shape. Deciding rewind's semantics first means either
  designing against a model that's about to change, or silently constraining the
  unification's answer.
- **Direction B** (worker jobs adopt `thread_messages` as conversational truth) is
  bigger still: it would make worker jobs *faithfully resumable like sessions*, on the
  same store. If that lands, **rewind stops being a session feature** and becomes a
  property of the unified message model — available to jobs too. A rewind designed
  narrowly around session-only assumptions would be the wrong shape the day B ships.

The honest summary: rewind wants a **version/branch dimension on the message model**.
The unification is the moment we decide what the message model *is*. Designing rewind
first is designing the extension before the thing it extends.

[[source_tree_unification]] is the lighter dependency — it doesn't change any
semantics, it just moves every file this doc cites. Hence: capture now, decide later.

---

## Open questions for the eventual design call

1. **Scope of v1** — conversation-only (Axis C1), or conversation + workspace in the
   first cut?
2. **Axis A** — tombstone (A2) or branch (A3)? Framed against whatever the unification
   settles on for the canonical shape. A3 is the better product and the bigger bill.
3. **Session-only or unified?** If [[unified_message_store]] takes Direction B, should
   rewind be specified against the unified model from the start, so worker jobs get it
   free?
4. **Does rewind subsume or share the compaction affordance** ("summarize up to
   here"), or stay strictly an undo?
5. **Axis C, if pursued** — git-per-turn (C3) or durable file snapshots (C2)? And what
   is the degraded story for lite/virtual sessions with no git?
6. **Retention** — Claude Code drops checkpoints at 30 days. Superseded/branched rows
   live in the control plane under PITR-forever. Does a rewound tail get reaped, and
   on what clock? (Interacts with the Phase 6 retention work in [[database_roadmap]].)

---

## Appendix — code anchors (as of 2026-07-15, pre-flatten)

Recorded so the mapping doesn't have to be redone. **All of these move in
[[source_tree_unification]].**

**Session loop / turn execution**
- `src/persistent_graph.py:533` — `run_persistent_loop` (outer loop)
- `src/persistent_graph.py:822` — `_execute_turn` (inner agentic loop)
- `src/persistent_graph.py:201` — `_ensure_msg_id` (stable per-message ids)
- `src/persistent_graph.py:305` — `PersistentLoopCallbacks`

**Input intake**
- `src/api/persistent_app.py:2503` — `handle_api_input`
- `src/api/persistent_app.py:2455` — `_accept_user_input` (persists user row before 200)

**Persistence / resume**
- `src/database/postgres_db.py:506` — `save_thread_message` (append-only upsert, `RETURNING id, seq`)
- `src/database/postgres_db.py:486` — `get_seq_for_message_id` (**the id→seq primitive**)
- `src/database/postgres_db.py:444` — `get_latest_compaction_checkpoint` (`boundary_seq`)
- `src/api/persistent_app.py:4756` — `_restore_session_messages` (rehydrate on attach)
- `src/api/persistent_app.py:4524` — `_record_compaction` (writes the `role='summary'` row)
- `orchestrator/database/postgres.py:5308` — `get_thread_messages_history` (cockpit read; **omits `seq`**)

**Revert primitives (existing)**
- `src/api/persistent_session.py:246` — `file_checkpoints` (in-memory)
- `src/api/persistent_session.py:1381` — `snapshot_file`
- `src/api/persistent_session.py:1402` — `undo_turn` (one-shot `pop`)
- `src/api/persistent_app.py:2813` — `undo` WS verb

**Schema**
- `orchestrator/database/schema.sql:861` — `thread_messages` (frozen snapshot; runtime uses `migrations/app/`)
- migration `0023` — `seq BIGSERIAL` + `idx_thread_messages_thread_seq`
- migration `0019` — normalized components (`reasoning`/`tool_results`/`provider_raw`/…) — **dormant, never written**
- migration `0004` — `thread_events` + `threads.events_epoch` (SSE replay; epochs are *not* on `thread_messages`)

**Frontend**
- `cockpit/src/app/core/services/persistent-chat.service.ts:1989` — `sendMessage` → outbox
- `cockpit/src/app/core/services/persistent-chat.service.ts:992` — `loadHistory` (IndexedDB-first, `?after=` cursor)
- `cockpit/src/app/core/services/persistent-chat.service.ts:3194` — `historyToTurns` (`UserTurn.id` = row id)
- `cockpit/src/app/core/services/persistent-chat.service.ts:477` — windowed render (`visibleTurns` / `loadOlderTurns`)
- `cockpit/src/app/core/services/turn-reducer.ts` — wire events → `ConversationState`
- `cockpit/src/app/core/models/turn.model.ts` — `UserTurn` / `AssistantTurn` / `CompactionTurn`
