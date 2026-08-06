# Supervised parallel tool calls: approve one, the rest are reported to the model as "User denied" (timeout fabricates a denial)


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Shipped `54e42626`, live-gated 07-29 — three-state PermissionOutcome end to end; the TurnResult.awaiting_permission detail was later superseded by `aa77433b` row-retirement with unchanged behavior.

**Status:** **Scope A SHIPPED — committed `54e42626`, deployed dev `sha-f0cd0e0`, LIVE GATE PASSED 2026-07-29** (evidence below). Root cause confirmed via live reproduction on the dev cluster. Fix 1 (three-state gate, park instead of fabricating a denial, tethered wait, interrupt-cancellable) + Fix 2 (pending gates re-surface on attach) are done with tests; see "Implementation" below. Out-of-scope items (Defect A stream, batch-approval UX, reload "completed" mislabel) remain open.
**Found:** 2026-07-24 (user report: *"When the AI sends multi tool calls but is on supervised we can only approve one — the rest fails"*). Root cause confirmed via live repro 2026-07-25, session `83dc7f7a-75b0-4141-ace6-0c5413a3e5cf` (dev `cockpit.srw.works`, user `operator@redacted.invalid`, model `MiniMax-M3`).
**Severity:** High. Silent and damaging: the model is told the user *refused* tool calls the user never saw, so it abandons real work. With a parallel tool batch the user approves the first and watches the rest "fail."
**Component:** agent gate loop (`src/persistent_graph.py`) · DB-backed permission gate + WS welcome frame (`src/api/persistent_app.py`) · cockpit approval card (`cockpit/.../persistent-chat.service.ts`, `persistent-chat.component.ts`).
**Related:** `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` — **this is the multi-tool amplification of that doc's Defect B**, plus a concrete fix plan · `session_silent_failure_audit.md`.

---

## Summary

A supervised tool gate models **a question to the user**, but the code collapses three distinct outcomes — *approved*, *explicitly-denied*, and *no-answer-yet* — into a boolean and maps **"no answer" onto "denied."** When the model emits several tool calls in one turn (parallel tool calls, which several models in `config/model_config_matrix.yaml` enable), the agent gates them **one at a time**. The user approves the first; each remaining gate then hits the **300 s TTL** and is written into the conversation as the literal **`"User denied this tool call."`** — a user decision that was never made. A broken live stream (the documented 425 `/connection` storm) makes it worse by preventing the later approval cards from ever reaching the browser, but the fabricated denial is a defect in its own right and fires regardless.

## Symptom (user-reported, then confirmed)

- Supervised session, the AI issues multiple tool calls.
- The user can approve **one**; the rest come back as **denied / failed** without the user denying them.

## Live reproduction (definitive)

Session `83dc7f7a`, Supervised, `MiniMax-M3`. Prompt asked for four parallel `web_search` calls. Backend message history (`get_persistent_thread_messages`) is the smoking gun:

```
[human]                     …issue FOUR parallel web_search tool calls at once…
[ai]                        Tools: web_search, web_search, web_search, web_search   ← 4 calls in ONE response
[tool] 15:40:28.842  #1 France  "Web Search Results for: capital of France…"        ← the one I approved
[tool] 15:45:28.887  #2 Japan   "User denied this tool call."                        ← I NEVER denied it
```

`15:45:28.887 − 15:40:28.842 = 300.04 s` — **exactly** `_PERMISSION_TIMEOUT_S`. The second gate was reported to the model as an explicit denial purely because it timed out.

Corroborating live evidence:
- **Broken stream (Defect A):** browser console showed repeated `425` on `GET https://api.srw.works/api/sessions/83dc7f7a…/connection`. The approval cards for calls #2–#4 never reliably reached the browser; the UI **froze on a stale "Japan pending" card**.
- **Sequential gating confirmed:** approving #1 caused #2's card to appear only afterward (never a simultaneous batch) — so a single-card UI is *not* itself the bug; the fabricated-denial-on-timeout is.
- **Secondary UI-fidelity bug:** after a reload, the transcript optimistically rendered all four searches as **"completed"** (green check) even though the backend had denied #2 and not yet run #3/#4. The reload reconstructs cards from the AI message's `tool_calls` and does not reflect true per-call status.

## Root cause (the chain)

1. Model emits N tool calls in one response (parallel; enabled per-model in `config/model_config_matrix.yaml`).
2. `src/persistent_graph.py` gates them **sequentially** — one `permission.request` at a time (`approved = await callbacks.permission_check(...)` at `persistent_graph.py:2124`, strictly blocking).
3. `_wait_for_permission_resolution` (`persistent_app.py:4118`) correctly returns 3-state `approved | denied | expired`, CAS-expiring the row at the **300 s** TTL (`_PERMISSION_TIMEOUT_S`, `persistent_app.py:4086`).
4. `_loop_permission_check` **flattens** it: `approved = final_status == "approved"` (`persistent_app.py:4325`) — `expired` and `denied` become indistinguishable.
5. The loop then writes the blanket `"User denied this tool call."` for anything not approved (`persistent_graph.py:2131`).
6. The pending gate is **never re-surfaced on (re)attach**: the WS welcome frame (`session.state`, `persistent_app.py:2762-2786`) carries `running_tool` but **not** the currently-pending permission request, and REST history doesn't carry it either. So once the live stream drops, a reload cannot recover the card — the gate just runs out its clock.

### The distinct defects
- **B — timeout fabricates a denial** (this doc's focus): steps 3–5. The model is told the user refused.
- **C — pending gate not recoverable on attach**: step 6. A reconnecting client can't re-render the card.
- **A — the live stream (`425` `/connection` storm)**: the trigger today; tracked separately in `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` (session-attach starvation). **Not** in scope here.
- **Secondary — reload mislabels denied/pending calls as "completed"** (cockpit reducer). Cosmetic; out of scope here.

---

## Fix plan (scope A: Fix 1 + Fix 2)

Guiding principle: **a gate is a question, not a deadline — never fabricate a user decision.** Represent the three outcomes faithfully, make "no answer yet" a durable, recoverable *waiting* state, and only ever change state on a real click. Fix 1 + Fix 2 together make the approval robust **even if the stream is never touched**, which is what makes this the root-cause fix rather than a patch.

### Fix 1 — an unanswered gate never becomes a denial

- **`persistent_app.py`**
  - `_wait_for_permission_resolution` (`:4118`): stop turning a timeout into a terminal denial. While the session is **tethered** (subscribers attached), wait on the real resolution (approve/deny) — **do not** CAS-expire to a denied-equivalent on a timer. Keep the wait **cancellable** by (a) real approve/deny, (b) user interrupt/stop, and (c) session end. When **untethered**, flip `awaiting_user` and sleep (the existing Phase-5 path) so an email/magic-link approval can still land later — again without fabricating a refusal.
  - `_loop_permission_check` (`:4229`, collapse at `:4325`): return a 3-state outcome to the loop instead of a bool (`approved | declined | pending/expired`). Record it faithfully in `tool_decisions` (distinguish `expired` from `denied` for audit).
- **`persistent_graph.py`** (gate loop, `:2124`–`:2140`): branch on the 3-state instead of the blanket write at `:2131`:
  - `approved` → run the tool (unchanged).
  - `declined` (explicit user deny) → write `"User declined this tool call."` and continue.
  - `pending`/no-answer → **do not write any ToolMessage.** Park the turn (`awaiting_user`), leaving the `thread_permission_requests` row `pending` and the tool un-run; it executes when the user answers (existing NOTIFY resume path). Remaining ungated calls stay pending too.
- **Interrupt-cancellability (sub-task, required):** since Fix 1 removes the 300 s ceiling that currently bounds the wait, the gate wait must observe the hard-interrupt event so **Stop** still works promptly while parked. (We already saw a related "Stopping…" delay live.)

### Fix 2 — a pending gate always re-surfaces on (re)attach

- **`persistent_app.py`** welcome frame (`session.state`, `:2762-2786`): include the current pending permission request(s) — e.g. `pending_permissions: [{id, approval_id, tool, args}]` from `SELECT … FROM thread_permission_requests WHERE thread_id=$1 AND status='pending'`.
- **cockpit** (`persistent-chat.service.ts`): on `session.state`, hydrate `pendingPermission` (and dispatch a `permission_request` into the reducer) from that array, so a reload/reconnect re-renders the approval card. Handles the durability gap noted in the related doc ("REST `/messages` history does not carry the pending gate").

With Fix 1 + Fix 2, the sequential one-card-at-a-time flow becomes reliable: no deadline to lose, and a reload always recovers the card. The stream fix (Defect A) then only affects *live* latency, not correctness.

### Tests (TDD — write first, watch fail, then fix)

1. **Core (RED today):** drive the gate loop with a mock `permission_check` whose resolution is `expired`/no-answer. Assert **no** `"User denied this tool call."` ToolMessage is appended and the turn parks (`awaiting_user`, pending row preserved). Currently the code writes the denial → fails. Add a sibling case: an explicit `denied` writes `"User declined…"` and continues.
2. **Re-surface:** unit-test the `session.state` builder with a `pending` row present → asserts `pending_permissions` is populated; cockpit spec: a `session.state` carrying `pending_permissions` sets `pendingPermission` and renders the card.
3. **Regression:** approved path unchanged (tool runs, real ToolMessage).

### Verification
- New unit tests green; full `pytest` + cockpit `vitest` suites unaffected.
- **Live gate:** repeat the repro (parallel calls, approve #1, leave the rest). Confirm via `get_persistent_thread_messages` that the remaining calls stay `pending` (no `"User denied"`), and that a **reload re-surfaces** the pending approval card and approving it runs the tool.

### Out of scope (separate, non-blocking tracks)
- **Defect A** — the `425 /connection` stream storm (session-attach reliability; the related doc).
- **Batch-approval UX** — one card per turn listing all N calls (*approve all / pick / deny*). Genuinely nice for parallel turns and matches the user's "approve the batch" instinct, but with Fix 1+2 it is **convenience, not correctness**. Good fast-follow.
- **Reload "completed" mislabel** — cockpit reducer should render true per-call status.

### Risks / trade-offs
- **Parked turn holds the loop** while awaiting approval. Acceptable: it is already mid-turn, the idle-sweeper/reaper handles a genuinely abandoned session, and the wait stays interrupt-cancellable. Confirm no turn-accounting/metrics or prompt-cache assumption breaks for a long-parked turn.
- **Untethered/headless semantics** must not regress: `awaiting_user` + later email approval should still resolve the same row; only the *fabricated denial on timeout* is removed.

---

## Implementation (2026-07-27, `develop`, uncommitted)

Done test-first: every behavior below had a failing test watched fail before the code existed.

**`src/persistent_graph.py`**
- New `PermissionOutcome` enum (`APPROVED` / `DECLINED` / `NO_ANSWER`) with `.coerce()` so legacy `bool`-returning callbacks keep working (`True`→APPROVED, `False`→DECLINED).
- `permission_check` is typed to return the outcome (or a legacy bool).
- Gate loop: `DECLINED` writes **`"User declined this tool call."`**; `NO_ANSWER` writes **nothing at all** and returns `TurnResult(awaiting_permission=True)`, leaving this call and every call after it un-run.
  - **Pairing is safe:** parking leaves the turn's `AIMessage.tool_calls` without matching `ToolMessage`s, which strict-pairing APIs 400 on. The pre-existing `repair_tool_pairing` (`src/core/context.py:286`) already runs before *every* live LLM call (`persistent_graph.py:1406`) and on resume, and strips orphaned calls bidirectionally — verified, this is the same class it already names ("an interrupted turn").
  - **Scope note — no auto-resume:** because that repair strips the orphaned call, a *late* approval does **not** replay the tool automatically; the parked turn simply ends without it and the model re-decides on the next turn. With Fix 1b below, a tethered user's gate no longer expires at all, so this path is reached mainly when untethered or interrupted — i.e. when the turn is over anyway. Wiring true replay-on-late-approval is deliberately out of scope.
- New `TurnResult.awaiting_permission` field.
- The string `"User denied this tool call."` **no longer exists anywhere in `src/`.**

**`src/api/persistent_app.py`**
- `_loop_permission_check` returns the three-state outcome instead of a bool. `expired`→`NO_ANSWER`; `denied`→`DECLINED`; dead session / DB-unavailable →`DECLINED` (a gate that can never be answered is a real stop, not a pending question). `tool_decisions` still records the **raw** status so audit can tell a timeout from a refusal.
- `_wait_for_permission_resolution`: `timeout` is now a **polling slice, not a deadline**. While **tethered** (subscribers attached) the gate is never CAS-expired — a slow-but-present user keeps their card, and their later click still works. While **untethered** it CAS-expires as before so the loop can't hang on a client that isn't coming back. A hard interrupt (Stop) breaks the wait promptly and returns `"interrupted"` (→`NO_ANSWER`), leaving the row `pending` — no decision is recorded that the user did not make.
- New `_pending_permission_requests()` → the pending gates for the thread, shaped like the `permission.request` payload (JSONB `tool_args` parsed, not passed through as a raw string). Soft-fails to `[]`.
- `session.state` welcome frame now carries `pending_permissions`.

**`cockpit/.../persistent-chat.service.ts`**
- `session.state` hydrates `pendingPermission` + dispatches `permission_request` per pending gate, so a reload/reconnect re-renders the card. Presence-checked (`'pending_permissions' in params`) so a metadata-only frame can't clobber a live card.
- Maps wire `approval_id`→`approvalId`. **A test caught this**: without the mapping the decision POST would fall back to "most-recent-pending" instead of targeting that specific gate.

**Tests** — new: `tests/test_persistent_graph_permission_outcomes.py` (6), `tests/test_persistent_app_permission_outcomes.py` (12), 2 cockpit specs. Updated to the new contract (intended behavior change, intent preserved): `test_permission_denied_tool` (wording), `tests/test_thread_permissions_phase3.py` (7), `tests/test_attention_sleep_phase5.py` (3).

**Verification run:** full backend suite **10965 passed**, 27 skipped, 3 failed — all 3 pre-existing and unrelated (`test_database_phase1` ×2 need a local Postgres on :5432; `test_endpoint_inventory` is stale from the in-flight `POST /api/jobs/{target_job_id}/verification/rounds` work in `orchestrator/main.py`, a file this change never touches). Full cockpit suite **1395 passed**; `tsc --noEmit` clean; `ruff check src/` clean.

### Live gate — PASSED 2026-07-29 (dev `sha-f0cd0e0`, agent pods confirmed on that tag)

Same repro, session `8d8fb61f-dc32-4545-8005-1212018706e1`, Supervised, `MiniMax-M3`. The AI response carried four parallel calls (`Tools: web_search, web_search, web_search, web_search`); only #1 was approved.

| | Before (07-25, session `83dc7f7a`) | After (07-29) |
|---|---|---|
| #1 France approved | ran 15:40:28 | ran 14:38:15 |
| #2 Japan, left unanswered | **`"User denied this tool call."`** at 15:45:28 — exactly **300.04 s** later | **nothing written**; still `pending` at **+6.5 min** |
| Reload | froze on a stale card | card **recovered**, buttons live |
| Approve recovered card | — | tool **ran** 14:45:50, real results |

Decisive evidence: at 14:44:13Z — **59 s past the old denial deadline** — `get_persistent_thread_messages` still returned exactly 3 messages with no denial. The final step also confirms the `approval_id`→`approvalId` mapping in production: a card rebuilt purely from the welcome frame resolved *that specific gate* and executed it.

**Observed unchanged:** the `425 /connection` console storm (Defect A) still fires on session creation — now a live-latency annoyance rather than something that corrupts the turn.

---

## Reproduce
1. Supervised session on a model that emits parallel tool calls (e.g. `MiniMax-M3`); prompt: *"issue four parallel `web_search` calls at once."*
2. Approve the first card; leave the rest.
3. Observe (backend `get_persistent_thread_messages`): ~300 s after each prior call resolves, the next unanswered gate is written as `"User denied this tool call."` A stale/425 live stream additionally prevents the later cards from rendering.

## Open questions
- Keep a (long) safety ceiling on the tethered wait, or wait strictly until a real answer / session end? Leaning: no timer while tethered; rely on interrupt + idle-reap.
- Should `awaiting_user` while parked mid-turn surface a distinct UI state ("waiting for your approval") vs. the generic idle state?
