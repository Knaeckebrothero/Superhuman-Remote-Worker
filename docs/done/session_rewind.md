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

> **Status (2026-08-07): DONE — implemented on develop, k3d live gate PASSED**
> (backend/API/WS surfaces; see [k3d results](#live-gate--k3d-results-2026-08-07)).
> Archived to `docs/done/` with the execution plan
> (`2026-08-07-session-rewind-plan.md`). At archive time the 20 commits are
> **on `develop`, unpushed**. Remaining after push + dev deploy: the cockpit-UI
> gate items (second-tab repaint, mid-stream interrupt, deep rewind past
> compaction, affordance/dialog/composer), listed under the dev checklist below.
> Code comments (incl. migration 0110's DB column comment, which is applied and
> immutable) reference this doc's old `docs/features/` path — intentional,
> not drift to "fix".
> **2026-08-08:** pushed + deployed to dev (chart `sha-fc10d83`, helm v535),
> verified live by owner. UI fast-follow shipped same day: `/rewind` slash
> command with target picker + action-sheet redesign — see
> [UX fast-follow](#ux-fast-follow-2026-08-08--rewind-command--dialog-redesign).
> Implementation complete across all tasks (Tasks 1–9 merged develop; Task 10 docs
> + live-gate checklist below). The design call happened 2026-08-07; the result is
> the [Decided design](#decided-design-2026-08-07) section below. The concept capture,
> prior art, and design space are kept beneath it as the record.
>
> **The 2026-07-15 parking was un-parked by owner decision, with both sequenced
> dependencies still unlanded** (re-verified 2026-08-07):
> 1. [[source_tree_unification]] — never happened; `src/` layout unchanged, so this
>    doc's anchors remain valid (line numbers drifted; key ones re-verified below).
> 2. [[unified_message_store]] / [[database_roadmap]] Phase 7 — still "NOT decided";
>    Phase 7 is the roadmap's only open phase. Rewind therefore designs against the
>    **current** `thread_messages` model, choosing semantics Phase 7 can adopt rather
>    than fight: a linear tombstone marker (`rewound_at`), not a branch dimension.
>    If Phase 7's canonical shape lands later, the marker column migrates with it.
> 3. [[persistent_session_source_of_truth]] Plans 2–4 — still pending, so the
>    in-memory-authority wrinkle below still binds: an attached session must mutate
>    agent memory, not just the DB. The design truncates in place (fidelity-
>    preserving) for the common case; only deep rewinds past the live compaction
>    boundary fall back to the lossy rehydrate.

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

## Decided design (2026-08-07)

Approved section-by-section in the 2026-08-07 brainstorm. Decisions against the
[design space](#the-design-space--resolved-2026-08-07-kept-for-the-record):

| Axis | Decision |
|---|---|
| Scope (v1) | **Full Claude Code parity**: independent restore modes (both / conversation / code) + a summarize action in one action sheet |
| A — transcript semantics | **A2 linear + tombstones** (`rewound_at` marker; one timeline in the UI; history preserved, un-tombstoning possible later). A3 branching rejected: exceeds parity, front-runs Phase 7, ~3–5× the bill |
| B — apply point | **Split by state**: attached → live WS verb, truncate in-memory in place; detached → DB-only, next attach rehydrates. Always-rehydrate (the Plan-2 shape) rejected for now: transcript round-trip is still lossy for live state |
| C — workspace revert | **C3 via the existing per-turn auto-commits** (`src/persistent_graph.py:1001` already commits after every tool-executing turn — the doc's assumed cost of C3 evaporated). Forward-restore commits, never `reset --hard` |
| D — UX surface | **D1 inline** "Rewind to here" on each user message → action sheet. Dedicated picker is a fast-follow if long-session navigation hurts |
| Summarize twin | **One action** ("Summarize up to here") instead of Claude Code's TUI-cursor-specific pair — deliberate, owner-approved parity deviation. Wired through the existing `compact` verb with an explicit boundary; not a rewind (no tombstones, no ledger row) |
| Ownership | Owner-only, both surfaces |
| Retention | Tombstones + ledger kept forever under the Phase-6 no-deletion policy; revisit with its retention clocks |

### Data model — one migration

Migration number picked at push time (collision hazard — duplicate prefixes
hard-fail the runner; re-check the free number right before pushing).

1. **`thread_messages.rewound_at TIMESTAMPTZ NULL`** + partial index
   `(thread_id, seq) WHERE rewound_at IS NULL`. "Rewind to message X" tombstones
   **X and everything after it** (`seq >= X.seq`) — X is being *un-sent*; its text
   refills the composer. Live readers add `WHERE rewound_at IS NULL`; audit/debug
   surfaces stay unfiltered. Post-rewind messages take higher `seq` and sort
   correctly; no renumbering. (In-place `UPDATE` is consistent with the existing
   write pattern — `save_thread_message` is already an `ON CONFLICT (id) DO UPDATE`
   upsert; the store's invariant is *never delete*, not *never update*.)
2. **`thread_rewinds` ledger** — one row per rewind: `thread_id, from_seq,
   mode ('both'|'conversation'|'code'), actor, abandoned_sha, restored_to_sha,
   restore_commit_sha, created_at`. Audit trail + un-rewind metadata + workspace
   record.
3. **`thread_turn_commits` map** — `(thread_id, seq, commit_sha)` PK
   `(thread_id, seq)`, written agent-side immediately after the existing per-turn
   auto-commit (and the compaction checkpoint commit) succeeds: "workspace state
   after transcript `seq <= N`". Restore target for a rewind to `S` = row with
   the largest `seq < S`. No backfill — code-restore coverage starts at deploy.

**Compaction self-consistency under seq-sweep** (the interaction the design space
flagged as "work through, don't assert"): a summary row written *after* X has
`seq > X.seq` → swept; the originals it summarized have `seq < X.seq` → survive;
rehydration falls back to them (or an older surviving summary). A summary written
*before* X survives and stays valid. No special-casing needed.

**Deep-rewind consequence:** if X predates the live compaction boundary, the
attached agent's in-memory list no longer contains X — truncate-in-place is
impossible and that case falls back to rehydrate-from-transcript (accepting the
fidelity cost on the rare deep rewind; shallow rewinds keep full fidelity).
Deep rewinds that cannot rehydrate the live context error out instead of acking
(the sweep stays durable; reattach heals).

**`seq` stays server-side:** the cockpit sends the message row id it already has
(`UserTurn.id`); the server resolves it via `get_seq_for_message_id`
(`src/database/postgres_db.py:489`).

### Flow — attached (WS `rewind` verb)

Sibling of `undo`/`interrupt` (`src/api/persistent_app.py:3169`/`:3072`), payload
`{message_id, mode}`, owner-only, serialized by the session loop:

1. **Resolve and validate the target** via `get_live_message` — resolve `message_id → seq`;
   reject unknown, tombstoned, or non-human messages without mutating the in-flight turn.
   (A pure validation error must not kill an in-flight turn — owner decision, 2026-08-07.)
2. **Hard-interrupt** any in-flight turn (existing tri-state machinery,
   `persistent_app.py:190–215`); drain the pending input queue.
3. **Code before conversation** — git is the fallible op, so it gates: if `mode`
   includes code, run the workspace restore; on failure abort with the error —
   nothing tombstoned, workspace left at a harmless snapshot commit.
4. If `mode` includes conversation: one transaction — single-statement sweep
   `UPDATE thread_messages SET rewound_at = now() WHERE thread_id = $1 AND
   seq >= $2 AND rewound_at IS NULL` + ledger insert. Then fix in-memory state:
   truncate in place (common case) or rehydrate via `_restore_session_messages`
   (`persistent_app.py:5787`) for deep rewinds. Reset `turn_count` to the
   surviving turns.
5. **Bump `events_epoch`** → all viewers take the existing `GONE_BEYOND_HORIZON`
   repaint (IndexedDB refreshes through the same path). The initiating client's
   ack carries the original prompt text for the composer refill.

### Flow — detached (orchestrator REST)

`POST /api/agents/threads/{id}/rewind`, owner-only, advisory-locked: same sweep +
ledger + epoch bump, orchestrator-side; next attach rehydrates from the filtered
transcript. **Code modes are rejected when detached** ("resume the session to
restore files") — no agent holds the workspace, and released workspaces may have
no filesystem at all. v1 carries no deferred-restore state machine.
The detached REST endpoint 409s only on a LIVE agent binding (`agent_id` set AND
status not in suspended/ended) — stale bindings on ended threads do not block
detached rewind.

### Workspace restore — always forward, never `reset --hard`

`reset --hard` would strand the branch behind Gitea and break the throttled
fast-forward push. Instead:

1. Commit the abandoned state as a **pre-rewind snapshot** (`add -A` + commit) —
   nothing is ever lost in git either.
2. Make worktree+index exactly the target tree — *including deleting files
   created since* (`git read-tree -u --reset <sha>` semantics; `checkout <sha>
   -- .` would leave them behind) — and commit as **"Rewind: restore to turn N"**.
3. History stays linear and pushable; both SHAs land in the ledger.

Degraded-mode matrix (the intended per-option availability; v1 enforces it
server-side and surfaces a clear error rather than pre-disabling — see §UX):

| Session state | Code restore |
|---|---|
| `git_versioning=False` (lite/virtual) | Unavailable — conversation-only, labeled |
| No mapped commit `< target seq` | Unavailable for that message |
| VM tier | Works (local git on the persistent rootdisk; push best-effort as today) |
| Detached | Rejected — resume first |

Known granularity caveat: a *failed* auto-commit bleeds that turn's file changes
into the next successful commit, so restore granularity degrades at commit
boundaries — it never restores a state that didn't exist. Bash side effects
against external systems remain out of scope (stated in the UI copy), same
posture as Claude Code.

### UX (cockpit)

Hover/kebab action **"Rewind to here"** on each user message → action sheet:
*Restore conversation and files / Restore conversation only / Restore files only
/ Summarize up to here / Cancel*. **v1 reality:** the degraded-mode matrix is
enforced server-authoritatively, not client-side — every option is offered, and
an unavailable mode (no version history, no mapped commit below the target,
detached) answers with a clear error surfaced in-dialog/banner rather than a
pre-disabled control. Client-side pre-disable with tooltip reasons, matching the
matrix, is a fast-follow. Confirmation copy: *"Messages after this point are
hidden from the conversation (kept in the audit trail). Files return to their
state after turn N. Commands with external effects can't be undone."* On
success: turns collapse via the epoch repaint, the composer prefills with the
original prompt, focus lands there. Non-owner viewers see no affordance and just
repaint. Rewind during streaming is allowed — the server interrupts first.

### UX fast-follow (2026-08-08) — `/rewind` command + dialog redesign

Owner-requested after first live use ("the current version needs to be
improved visually"). Commits: `18bdb8fa` (command + picker + redesign),
`9e32aa75` (usability pass below).

- **`/rewind` composer command** — added to the slash menu. Opens a target
  picker (user prompts newest first, time + first line, scrollable) instead of
  sending anything; picking a row opens the action sheet for that message.
  Same eligibility gate as the hover affordance (`historical && !outbox`), and
  it fixes discoverability on touch devices where hover doesn't exist. The
  picker lists the loaded transcript window — very old prompts need a scroll-up
  first (accepted v1 limit).
- **Action sheet redesign** — the five mismatched footer buttons
  (warning/warning/info/info/ghost) became an option list in the dialog body:
  icon + title + one-line description per row, restore group visually separated
  from *Summarize up to here*, caveat as small print, only *Cancel* left in the
  footer. The old `chat.rewind.body` paragraph was folded into the per-option
  descriptions (i18n key removed; `*Desc` + `picker*` keys added, en + de-DE).
- Pure helpers `isRewindCommand` / `pickRewindCandidates` exported from
  `persistent-chat.component.ts` for vitest (7 new cases; template wiring
  verified via Playwright on k3d, both themes, per the codebase's split).
- Plumbing unchanged: rows call the same `confirmRewind(mode)` /
  `confirmSummarizeUpTo()` as before.

**Usability pass (same day, owner: "the /rewind menu is very small"):** both
rewind dialogs went `sm`(320px)→`md`(480px). Picker: two-line message preview
(`-webkit-line-clamp`, full text via `title`), date-aware stamps
(`formatRewindStamp`: time-only today, localized "Yesterday HH:MM", short date
beyond — year only when it differs), type-to-filter input shown at
≥`REWIND_FILTER_MIN_CANDIDATES` (6) prompts (`filterRewindCandidates`,
substring, case-insensitive), initial focus lands in filter/first row past the
dialog's close-button auto-capture (50ms defer), Arrow/Home/End roving
(`onRewindPickerKeydown`; ArrowDown from the filter drops into the list), list
grows to `min(55vh, 460px)`. Action sheet: meta line under the quote —
target's stamp `· hides N later messages` (`countTurnsAfter`: user+assistant
turns after target in the loaded window; clause omitted at 0) — plus a
composer-refill hint line before the caveat. 12 more vitest cases on the new
pure helpers; verified live on k3d both themes incl. a 7-prompt seeded fixture
thread (`bbbbbbbb-1111-…`, local DB only) exercising filter + stamp variants.

### Failure containment

- Git-fail → abort pre-sweep; state unchanged (snapshot commit is harmless).
- Sweep-fail after git success → files restored, conversation intact; error
  surfaced; retry idempotent (re-checkout of the same tree is a no-op commit).
- Double-fire serialized (session loop / advisory lock).

### Testing

- **Unit:** sweep boundary semantics (X inclusive; summary rows after/before X;
  idempotent re-sweep), turn→commit resolution (gaps, no-commit turns,
  failed-commit drift), ledger writes.
- **Integration (CI Py3.12 is the gate):** attached shallow rewind (in-memory
  truncate, no lossy round-trip), deep rewind past the compaction boundary
  (rehydrate path), detached rewind + re-attach hydration, epoch-bump repaint,
  all three modes, owner enforcement, mid-stream interrupt-then-rewind.
- **Workspace:** forward-restore including deletion of since-created files,
  snapshot commit exists, push stays fast-forward, degraded matrix.
- **Live gate on dev** before calling it shipped: real session, rewind
  mid-conversation, Gitea history stays linear, other-viewer repaint observed.

## Live gate — k3d results (2026-08-07)

Executed against the local k3d stack (`srw` namespace, tilt-built images from
this branch), driving real provisioned sessions over the agent WS and the
orchestrator REST API. Per checklist item below:

1. **PASS (k3d).** Sandbox session, two file-edit turns (`story.txt` version
   A → B), WS rewind `mode=both` to the version-B prompt: ack carried the
   prompt + `restored_to_sha`, the file reverted to `version A`, git history
   stayed strictly linear (`Auto-commit turn 2 → Auto-commit turn 3 →
   Rewind: pre-rewind snapshot → Rewind: restore workspace`), and
   `thread_turn_commits` was re-pointed at the restore commit (the
   final-review Critical fix observed live: seq 566 `0bccb655 → 3ec23492`).
   In-memory truncate proven behaviorally on a second session: after
   rewinding away a "reply banana" exchange, the agent answered "pineapple"
   to "what was the last word I asked for?" and `turn_count` resumed at 1.
2. **Not run live** (needs a real cockpit browser session). Server mechanics
   verified: epoch bump + `rewind.done` journaled at `(new_epoch, 1)` on
   both paths; cockpit cache-clear paths are vitest-covered.
3. **Not run live** (timing-dependent); interrupt-then-rewind is unit-covered.
4. **Not run live**; deep-rewind rehydrate fallback is unit-covered.
5. **PASS (k3d).** Seeded ended thread: conversation rewind 200 (swept=2,
   prompt echoed), filtered reads shrink, re-target → 404, code mode → 400,
   `status=active`+agent → 409, ended thread with STALE `agent_id` → 200
   (the stale-binding fix observed live), epoch bumped per rewind with
   `rewind.done` rows at `(1,1)` and `(2,1)`, malformed id → 404.
6. **PASS (k3d).** Lite session `mode=code` → "no version history" error with
   `request_id`; conversation rewind on the same session works.
7. **PARTIAL (k3d).** Boundary accepted, keep-window computed, summarization
   plan logged (89-token prefix) — the summary LLM round-trip was still
   pending on a slow aux upstream when the gate closed (same pipeline as
   ordinary `/compact`; boundary math is unit-covered incl. injections).
   Bonus from the stall: a rewind sent mid-compaction was refused instantly —
   the final-review lock-serialization fix observed live.
8. **PASS (k3d).** Zero PREPARE/DataError/rewind-related tracebacks in
   orchestrator + agent logs across all scenarios.

Residual for the dev cockpit gate: items 2/3/4 and the cockpit UI surface
(affordance, dialog, composer refill, IndexedDB truncate-then-reload).

## Live gate checklist (dev)

Run against a real dev session before calling this shipped:

1. Sandbox session, ≥4 turns with file edits → rewind (both) to turn 2:
   transcript truncates, composer prefills, files revert, Gitea history is
   LINEAR (snapshot + restore commits, no force-push), turn_count resumes at 2.
2. Second browser tab on the same thread repaints (no stale tail from
   IndexedDB) after the rewind.
3. Rewind mid-stream: the in-flight turn interrupts first.
4. Deep rewind: force a compaction (/compact), then rewind past the boundary
   → rehydrate path, summary row superseded, no dangling banner.
5. Detached: end the session → POST /api/agents/threads/{id}/rewind
   (conversation) → 200; re-open the thread → truncated history; code mode →
   400; live thread → 409.
6. Lite session: code buttons answer with the no-version-history error;
   conversation rewind works.
7. Summarize up to here: banner appears, earlier turns fold into the summary,
   the chosen message and everything after stay verbatim.
8. asyncpg smoke: watch orchestrator + agent logs for PREPARE errors on the
   new statements during 1-7.

### Read-path filter checklist (plan-time)

Every `SELECT` on `thread_messages` gets classified **live** (add the filter) vs
**audit** (leave unfiltered) during implementation planning — grep-able by
design. Known live readers: the agent rehydrate history query, the cockpit
history read (`orchestrator/database/postgres.py` `get_thread_messages_history`),
the compaction-boundary reads (latest *live* summary), session-wake reads, and
the MCP thread-message reads. Known audit readers stay unfiltered: debug/audit
views, exports that promise completeness.

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

## The design space — RESOLVED 2026-08-07, kept for the record

Recorded as options with trade-offs. **Every axis is now decided** — see the
[Decided design](#decided-design-2026-08-07) table. Kept because the rejected
options' reasoning is part of the decision.

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

## Why this was sequenced behind the unification (historical — un-parked 2026-08-07)

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

## Open questions for the eventual design call (ANSWERED 2026-08-07 — see Decided design)

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
[[source_tree_unification]].** Re-verified 2026-08-07: layout unchanged, line
numbers drifted; current values for the load-bearing ones —
`get_seq_for_message_id` → `postgres_db.py:489`, `undo` verb →
`persistent_app.py:3169` (`interrupt` `:3072`, `compact` `:3151`),
`file_checkpoints` → `persistent_session.py:264`, `_restore_session_messages` →
`persistent_app.py:5787`, per-turn auto-commit → `persistent_graph.py:1001`
(compaction checkpoint commit `:1509`), `get_head_commit` → `workspace.py:419`.

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
