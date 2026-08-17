# The session usage panel shows the previous session's token counters

**Status:** **FIXED + live-verified 2026-08-17** on k3d — a new session created
by in-app navigation from an answered one renders no panel at all instead of the
previous session's counters, a reload restores the open thread's own numbers
undoubled, and an in-app switch back to the first session restores its numbers
rather than the second's. Reported from a live session showing
`INPUT 154.6k · CTX 48%` next to `Turn 0` on a freshly created, never-answered
session.

**Residue (verification, not implementation):** the multi-frame aggregation — a
turn with more than one LLM call — is unit-tested and was exercised against the
cluster's real Postgres in a rolled-back transaction, but has not yet run
through a genuine tool-using turn live; the attempt hung on an unrelated stalled
LLM call. See the runbook §3.

**Verification runbook:** `docs/tests/session_usage_panel_isolation_verification.md`
**Panel's design of record:** `docs/features/context_summarization_rework.md` §4.6 (S5)

## What happens

Open a session, converse until the panel reads e.g. `INPUT 154.6k OUTPUT 118
REASONING 28 · CTX 48%`, then create a new session. The header correctly resets
to `Turn 0` / `Untitled Session`, but the usage panel above the composer keeps
rendering the *previous* session's numbers — including a CTX gauge at 48% on a
session whose real context is ~15k of system prompt.

The numbers are not wrong in themselves. They are last session's numbers,
rendered against this session.

## Root cause

`PersistentChatService` is `@Injectable({providedIn: 'root'})` — one instance for
the lifetime of the tab. Its `usage` signal models a **per-thread** quantity but
was written in exactly one place (the `usage.updated` frame handler) and reset in
**none**.

Every sibling per-thread signal is explicitly torn down. `disconnect()` clears
~30 of them (`sessionTitle`, `modelName`, `turnCount`, `compaction`, `tasks`,
`runningTool`, `citationsByCid`, the cloud-diff group…) and `connect()`'s cold
path clears them again on a genuine thread switch. `usage` was in neither list,
and `enterDraftSession()` did not touch it either.

That is the exact signature in the report: `Turn 0` (because `turnCount` *is*
reset) beside a stale `INPUT` (because `usage` was not).

This is the same class of defect LibreChat hit and fixed in
[PR #13670](https://github.com/danny-avila/LibreChat/pull/13670) — "a null-anchor
context snapshot was treated as active on every branch, leaking one generation's
granular breakdown onto sibling branches". Their fix was not *more reset calls*;
it was requiring the usage record to be anchored to the branch being viewed.

## What was already correct

Worth stating, because it bounds the fix. The agent already emits `usage.updated`
after **every main-model call** (`src/persistent_graph.py:2342`), built from the
provider's own `usage_metadata`: input, output, reasoning, and cached tokens,
plus `ctx_limit_tokens` and `compaction_threshold_tokens`. Not estimated, not
inferred — the numbers off the API response, on every call, including the
intermediate calls of a tool-using turn. The requested cadence ("update after a
message finishes, whatever kind it is") was already the shipped behaviour. Only
the client's handling of those frames was broken.

Every broadcast frame is also journaled to `thread_events`
(`_broadcast_frame`, `src/api/persistent_app.py:5157`), so the history needed to
reconstruct the panel already existed on disk.

## Design

Three invariants, in priority order.

**1 — Isolation. The panel shows this thread's numbers or nothing.**

Rather than adding a fourth, fifth and sixth `usage.set(null)` call and hoping
the next feature that switches threads remembers the seventh, `UsageState` now
carries the `threadId` it describes. Frames stamp it on write; readers require it
to equal the live `threadId()`. A stale value is unrenderable *by construction*,
so correctness no longer depends on any reset site being remembered. The explicit
resets are still there on the genuine transitions (cold connect,
`createAndConnect`, `enterDraftSession`) for hygiene and readable intent — they
are belt, not braces.

**2 — Restorability. A reload reproduces the same numbers.**

`build_session_state_snapshot` now folds the last known usage into the durable
`session.state` payload, reconstructed from `thread_events` with the same
aggregation rule the client uses: take the newest `usage.updated` frame at or
below `event_cursor`, then sum `output_tokens` / `reasoning_tokens` across every
frame of that same turn. DB-authoritative, so it works on the stateless lane with
no pod to ask, and it covers the two paths where SSE replay alone restores
nothing — a stranded `turn.started` on an idle runtime (`replay_seq = hwm`) and a
`gone_beyond_horizon` re-anchor.

The snapshot is presence-authoritative like `tasks` and `pending_permissions`: an
explicit `usage: null` clears the panel, which is a second, independent kill for
the leak (a fresh thread's snapshot always reports null).

Seeding from the snapshot and *then* replaying from `replay_cursor` would
double-count the latest turn's output/reasoning, since replay re-delivers the
very frames the snapshot aggregated. `_handleSseFrame` already computes
`coveredBySnapshot` for exactly this hazard, so the usage handler now ignores
snapshot-covered frames. No new machinery.

That drop is gated on `snapshotSeededUsage` rather than on `coveredBySnapshot`
alone. During a rolling deploy a new Cockpit can talk to an orchestrator whose
snapshot predates the `usage` key: nothing gets seeded, so there is nothing to
double-count, and dropping the covered frames anyway would leave the panel blank
after a reload until the next LLM call. Seeded means owned; unseeded means
replay still rebuilds.

Cost: one extra `thread_events` read on the connect path, in the same shape and
cost class as the `lifecycle` and `running_tool` queries beside it — bounded to a
single `(thread_id, epoch)` range by the existing
`idx_thread_events_thread_epoch_seq`. Measured on k3d, the whole snapshot builds
in **3 ms** against a 29 ms config resolve, so it is not the term that matters.

Only the durable REST snapshot carries usage — deliberately not the agent's
in-memory WS welcome frame. The welcome frame arrives *after* the REST snapshot
and after some replay, so applying usage from it could clobber correctly
accumulated live state. One restore path, no ordering hazard.

**3 — Freshness.** Unchanged, and already met: the panel settles on the last
call's provider numbers as soon as that call returns.

### Considered and rejected: a locally computed `ctx_used_tokens`

`INPUT` is the *last request's* prompt, so at rest it excludes the reply that
request produced — the panel is structurally one message behind, and the CTX
gauge under-reports by the same amount. The original S5 design
(`docs/features/context_summarization_rework.md` §4.6) specified a separate
`ctx_used_tokens` field, which the implementation dropped in favour of using
`input_tokens` as the proxy. Restoring it — as
`max(local_token_count, last_provider_input_tokens)`, the same number
`ContextManager._trigger_token_count` feeds to the compaction decision — would
make the gauge mean literally "how close to compaction".

Not done, for three reasons. It contradicts the stated requirement that the panel
show "the values we get from the API request"; it mixes a locally tokenized
estimate into a panel that is otherwise 100% provider truth, which is how a
number stops being trustworthy; and it costs a full-history tokenizer pass per
turn. The one-message lag is honest and self-heals on the next call. Filed here
as a known limitation rather than built.

## Changes

| Area | File | Change |
|---|---|---|
| Snapshot | `orchestrator/services/session_state_snapshot.py` | `_usage_snapshot` + a `usage.updated` query bounded by `hwm`; new `usage` key on the returned shape |
| Client state | `cockpit/src/app/core/services/persistent-chat.service.ts` | `UsageState.threadId`; `currentUsage` guarded read; thread-gated writes *and* sticky fallbacks; `snapshotSeededUsage` + `coveredBySnapshot` drop; `_usageFromSnapshot` seeding on `session.state`; resets on the three thread transitions |
| Render | `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` | panel and `usageCtxPct` read `currentUsage()` |
| Tests | `tests/test_session_state_snapshot.py` (+4), `cockpit/…/persistent-chat.service.spec.ts` (+9) | aggregation rule; isolation, snapshot seeding, no-double-count, legacy-peer tolerance |

## Verification

Unit gates plus a live k3d run — see
`docs/tests/session_usage_panel_isolation_verification.md`. Results at close:
cockpit **2173/2173**, `tests/test_session_state_snapshot.py` **27/27**, Python
suite **14,897 passed** (the single `test_arxiv_client` failure is a pre-existing
xdist ordering artifact — confirmed by re-running with these changes reverted).
Both new guards were proved to bite by reverting each in turn and re-running.
