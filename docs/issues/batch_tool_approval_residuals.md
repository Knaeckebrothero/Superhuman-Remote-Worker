# Batch tool-call approval — residual findings after the shipped work

**Status:** Open (low severity). The feature and all four whole-branch-review defects are **shipped, pushed and live-gated**; this doc carries the findings that were deliberately deferred with a ruling, so they are not lost.
**Filed:** 2026-08-09, from the SDD ledger `.superpowers/sdd/2026-08-01-batch-tool-approval/progress.md`.
**Component:** `src/api/persistent_app.py` · `src/persistent_graph.py` · `cockpit/src/app/core/services/persistent-chat.service.ts` · `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`
**Related:** design `docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md` · plan `docs/superpowers/plans/2026-08-01-batch-tool-approval.md` · origin bug `docs/done/supervised_parallel_gates_timeout_fabricates_denial.md` · verification gaps `docs/issues/cockpit_verification_gaps_typecheck_noop_and_unmountable_component.md`

---

## What shipped (context)

When the model emits several tool calls in one response, Supervised mode used to gate them one at a time, so approval card N+1 only appeared *after* tool N finished running. Now `_loop_announce_permission_batch` pre-inserts a durable `pending` row per gate-needing call and broadcasts one `permission.request_batch`; the cockpit renders **one** card listing every call; `_loop_permission_check` **claims** the announced row instead of inserting a duplicate. Execution remains **sequential**.

Four defects found by the whole-branch review were fixed afterwards (`d6518b32` cockpit, `69e22f0c` backend) and verified — the backend in-pod against real Postgres on k3d (7/7 checks), and the UI through a real browser on k3d.

## Residual findings (each deferred with a ruling)

### R1 — `_resolve_pending_permission(approval_id=None)` still resolves "most-recent-pending"
`src/api/persistent_app.py`. When a decision arrives with no `approval_id`, it resolves the newest pending row for the thread. That was safe when only one gate could ever be open; with a batch, N are open, so a no-id decision resolves **the wrong gate**.

*Mitigated, not removed:* the cockpit now always sends an explicit `approval_id` (pinned by a mutation-proven test), and the orchestrator route binds the id from the path. But the fallback still exists on both the WS (`method: approve|deny`) and REST paths, so any future/alternate client that omits the id silently mis-resolves.
**Ruling:** deferred. **Suggested fix:** refuse the no-id fallback when more than one row is pending (409 or explicit error) rather than guessing.

### R2 — the claim SELECT matches on `thread_id + tool_call_id` only, not `tool_name`
A recycled or colliding provider `tool_call_id` would claim a row describing a *different* tool, so the card shown and the tool actually run could diverge. Pre-existing exposure for terminal rows; the batch work widened it to `pending` rows.
**Ruling:** deferred — provider tool_call ids are unique per thread in practice. **Suggested fix:** add `AND tool_name = $3` to the pending branch (a safe tightening).

### R3 — the claim-SELECT soft-fail path double-inserts silently
If the claim SELECT raises, the gate falls through and inserts a second row. The backend now reuses the remembered announced row id as belt-and-braces, and the cockpit converges on the authoritative `approval_id`, so the permanent hang this used to cause is closed. What remains is that the fallback is invisible in logs.
**Ruling:** deferred. **Suggested fix:** log a warning naming the risk when this path is taken.

### R4 — unbounded argument text node
Arguments are rendered in full **by design** — that is the safety condition under which "Approve all" was accepted (a destructive command must be visible before the single click). A `write_file`-style call therefore dumps its entire content into one DOM text node.
**Ruling:** correct as-is; do **not** reintroduce truncation. **Suggested improvement:** a per-row collapse whose full text is still present in the DOM and reachable without a second decision.

### R5 — `.permission-row app-icon` selects the element, not a class
Cosmetic inconsistency with the sibling `.mile-detail-icon` convention. **Ruling:** deferred.

### R6 — other truncations elsewhere in the same component
`formatToolArgs`, `toolLabelContext`, `previewResult` still truncate, and `.mile-args` still uses `ellipsis`/`nowrap`. These serve the compact transcript cards, **not** the approval card, and are not safety-critical. **Ruling:** deliberately untouched; noted so a future reader does not "fix" the approval path by copying them.

### R7 — test-hygiene items
- `test_persistent_app_permission_batch.py` asserts `is not APPROVED` where `is DECLINED` is strictly stronger.
- The cockpit `approveAll` id-test uses `.some()` rather than a call count, so a double-POST-per-entry regression would slip through (a duplicate POST 409s and is handled, hence low severity).
- `_thread_id`-is-None and missing-`id` skip paths in the announce are untested.
- Only `run_command` is exercised for `_SHELL_TOOLS`; `shell_execute` / `shell_read` never appear in a test (the drift-guard test patches the constant and iterates it, which is the load-bearing part).
- A Task-4 test leaves a fire-and-forget completion-handler task that runs after its patch context exits, calling the real `_terminate_session` against unpatched globals — safe today only because baseline `_session` is `None`.

**Ruling:** all deferred; none affect shipped behaviour.

## Not verified live

**Terminate-clears-the-ledger** (`_terminate_session_inner` awaiting the retire, then clearing `_announced_permission_rows` / `_gates_in_flight` / `_active_permission_request_id`) is **unit-tested only**. Verifying it live requires tearing down a session while it is in use, which was not done against a running session. Everything else in the fix wave was verified in-pod on k3d against real Postgres.

## Environment notes worth keeping

- The **k3d** cockpit is **HTTPS-only** (`env.js` → `apiUrl = https://localhost/api`); plain `http://localhost` 404s at traefik.
- k3d offers only `gemma-4-moe`, which will **not** emit a parallel tool batch, and has no outbound `web_search`. To exercise the multi-row card there, insert pending `thread_permission_requests` rows directly and reload — that drives the real `_pending_permission_requests` → `session.state` → hydration path.
- Verify a fix is actually deployed **by content** (`git grep <symbol>` inside the running image / `git grep <symbol> HEAD`), never by SHA ancestry: pushing rewrites commit SHAs in this repo.
