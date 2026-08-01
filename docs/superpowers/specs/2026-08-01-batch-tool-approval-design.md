# Batch tool-call approval — design

**Date:** 2026-08-01
**Status:** Design approved, not yet implemented.
**Component:** agent tool loop (`src/persistent_graph.py`) · gate transport (`src/api/persistent_app.py`) · cockpit approval card (`cockpit/src/app/core/services/persistent-chat.service.ts`, `views/persistent-chat/persistent-chat.component.ts`)
**Follows:** `docs/issues/supervised_parallel_gates_timeout_fabricates_denial.md` (scope A — shipped, live gate passed 2026-07-29). That fix made parallel-tool approval *correct*; this makes it *usable*. Listed there as the deferred "batch-approval UX" follow-up.

---

## Problem

When the model emits several tool calls in one response, Supervised mode gates them **one at a time**, and the loop is `gate → execute → gate → execute` (`persistent_graph.py:2156`). So approval card N+1 only appears **after tool N has finished running**. With four `web_search` calls the user approves one, waits ~7 s for the search to complete, approves the next, waits again — four separate decisions strung out across the whole turn.

The cockpit reinforces it: `pendingPermission` is a **scalar** signal (`persistent-chat.service.ts:584`) and the template renders exactly one card, so even if the backend offered all N the UI could only show the last.

**Goal:** see every tool call in the batch at once, decide once, then let the turn run.

## Decisions (agreed with the user)

| Question | Decision |
|---|---|
| What does the user see? | **One card for the whole batch**, listing every call, with `Approve all` / `Auto-accept` / `Stop`. Not N stacked cards, not per-call checkboxes. |
| How do approved tools run? | **Sequentially, exactly as today.** The stated pain is approval latency, not execution time. Parallel execution is explicitly rejected for now — it has bitten us before (shell tools over SSH exceeding sshd `MaxSessions`, misclassified as a dead workspace) and would need a concurrency cap plus an audit of shared tool state. |
| Mixed-risk batch (3 searches + `rm -rf`)? | **`Approve all` covers everything.** The card lists every call in full — including full args — so the dangerous one is visible before the click. Same trust model as today's per-call Approve. No per-tool risk classification to maintain. |

## Approach: pre-announce, then let the existing gate loop drain

Rather than restructure the tool loop into an explicit two-pass (gate-all, then execute-all), **insert all the permission rows before executing anything** and leave the per-call gate loop untouched.

This works because `_loop_permission_check` already contains a pre-check that short-circuits on an existing decided row for the same `tool_call_id` (the Phase-5 wake path, `persistent_app.py:4294`). Once the user hits `Approve all`, every row flips to `approved`, so gate calls #2…#N return **instantly** from that path instead of blocking.

Consequences:
- The gate loop keeps its three-state `PermissionOutcome` semantics, park-on-`NO_ANSWER`, tethered no-expiry, and interrupt handling — all shipped and live-verified on 2026-07-29. **No changes, no re-verification of that behavior.**
- The diff is small and concentrated in the announce step plus the cockpit.

### Backend — `src/persistent_graph.py`

- Add an optional callback `announce_permission_batch(tool_calls) -> Awaitable[None]` to `PersistentLoopCallbacks`. Optional so existing callers (worker graph, tests, light runner) need no change.
- In `_execute_turn`, call it **once** immediately before the per-call loop, when the response carries tool calls.
- The per-call gate loop is **unchanged**.

### Backend — `src/api/persistent_app.py`

- **`_loop_announce_permission_batch(tool_calls)`** (new): for each call that would actually be gated, INSERT a pending row; then `_broadcast` a **single** `permission.request_batch` frame whose payload is `{"requests": [...]}`, each entry carrying `id` (tool_call_id), `approval_id`, `tool`, `args` — the same per-entry shape `permission.request` and the welcome frame already use.
  - Calls that would not be gated get **no row**: `autonomous` mode, and `auto_accept` for non-shell tools. Mirrors the existing early-returns in `_loop_permission_check` so we never create a row for a call that auto-approves.
  - Soft-fails: if the announce breaks, the per-call path still inserts and gates exactly as it does today. The feature degrades to current behavior rather than blocking the turn.
- **`_loop_permission_check`**: extend the existing pre-check to also **claim a `pending` row** for this `thread_id` + `tool_call_id` — wait on that row instead of inserting a new one.
  - **Load-bearing.** `thread_permission_requests` has **no unique constraint** on `(thread_id, tool_call_id)` (migration `0005`), so without this we would double-insert: one row from the announce, one from the gate. The UI would then show a card whose `approval_id` nobody is waiting on.
  - Terminal rows (`approved`/`denied`) keep their current short-circuit. `expired` still deserves a fresh prompt, unchanged.
  - **When a row is claimed, the per-call path does NOT re-broadcast `permission.request`** — the batch frame already told the client about it, and a second frame would duplicate the entry. The single-gate path (no announced row, so it inserts its own) keeps broadcasting `permission.request` exactly as today, which is what SSE replay and non-batch turns rely on.

### Cockpit

- `pendingPermission: signal<PermissionRequest | null>` → **`pendingPermissions: signal<PermissionRequest[]>`**.
- Event handling:
  - `permission.request_batch` → replace the list with its `requests`.
  - `permission.request` (single-call turns, SSE replay) → append if absent.
  - `permission.resolved` → remove that `id` from the list.
  - `session.state` `pending_permissions` → replace the list. **Already delivers a list** (shipped with scope A Fix 2), so batch reload-recovery works with no extra backend work.
- Template: one card rendering the list — every call with tool name and full args.
  - `Approve all` → POST a decision for **each** `approval_id`.
  - `Auto-accept` → approve all, then flip mode to `auto_accept`.
  - `Stop` → deny all, then interrupt.
- A single-call batch renders the same card with one row.

## The trap: never resolve without an explicit id

`_resolve_pending_permission` falls back to *"the most-recent-pending row for this thread"* when no `approval_id` is supplied (`persistent_app.py:4326`). That was safe when only one gate could be open. With N rows pending it silently resolves **the wrong gate**.

Therefore: every decision the cockpit sends for a batch **must** carry its explicit `approval_id`. The existing `_resolvePermission` already prefers the id and only falls back when it is missing — the risk is a batch entry arriving without one. Pinned by a test.

Reusing the existing per-id REST endpoint (N POSTs) rather than adding a batch endpoint keeps this on the already-tested path; N is small (a handful of calls per turn).

## Testing

Backend:
1. Announce inserts one row per gate-needing call, and **zero** rows for calls that auto-approve (autonomous; auto-accept non-shell).
2. Announce emits exactly **one** broadcast carrying all entries.
3. `_loop_permission_check` **claims** the announced pending row — asserts no second INSERT, and that it waits on the announced row's id.
4. Approve-all-then-drain: with all rows pre-approved, every gate returns `APPROVED` without blocking.
5. Announce failure degrades to today's behavior (per-call insert + gate still works).

Cockpit:
6. Batch frame populates the list; the card renders every entry.
7. `Approve all` sends one decision per entry, **each with its explicit `approval_id`** (the trap above).
8. `permission.resolved` removes only its own entry.
9. `session.state` `pending_permissions` restores a multi-entry batch on reload.

Live gate (dev): four parallel `web_search` calls → **all four visible at once** in one card → one `Approve all` → all four run sequentially, no fabricated denials, and a mid-batch reload still shows the remaining batch.

## Out of scope

- **Parallel execution** of approved tools (rejected above; revisit only if execution time becomes the complaint).
- **Per-tool risk classification** / splitting shell calls out of the batch.
- **"Don't ask again for this tool"** persistence.
- The `425 /connection` stream storm (`resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md`) — separate, still open.

## Risks

- **Duplicate rows** if the claim logic is wrong → orphaned cards nobody waits on. Covered by test 3.
- **Wrong-gate resolution** via the most-recent-pending fallback. Covered by test 7.
- **Mixed auto-approve batches**: under `auto_accept`, a batch of 3 searches + 1 shell call announces only the shell call. The card must not imply the searches need approval. Covered by test 1.
- **Stop must deny every row**, not just the first, or the turn parks on a leftover pending gate.
