# Surface silent auxiliary-task failures

## Problem

Auxiliary-LLM tasks — memory extraction (observer), knowledge curation,
memory assembly, and session **title** generation — are deliberately
**non-fatal**. Every caller wraps the work in `try/except` and, on failure,
logs at `WARNING` (`"… failed (non-fatal): {e}"`) and continues so a degraded
auxiliary model never fails a job or a chat turn.

The cost is **invisibility**. On **2026-06-03** the auxiliary model's backend
(the workstation GPU at `:18090` behind `ai.h4ll.app`, which serves
`gemma-4-moe` *and* `qwen3-embedding-8b`) went offline. For ~3 days:

- no memories were written (pgvector `memories` frozen `06-03 01:30` → `06-06 10:00`),
- new sessions got no title (a real 10-turn session stayed `"Untitled Session"`),

…and there was **zero** user-facing or operator-facing signal. The main model
runs on a *separate* endpoint (codex-proxy), so jobs/sessions looked completely
healthy. The outage was only discovered when a human noticed missing titles.

The failures *were* logged — at `WARNING`, interleaved with normal noise, on
per-pod agent logs that rotate in hours. Nothing aggregated them, escalated
them, or exposed them on a status surface.

> Note: this is purely an **observability** gap. The credential path is fine —
> `llm_endpoints.api_key` is encrypted at rest (`v1:` AES-GCM) and the dispatch
> path decrypts correctly. Don't go looking for a "bad key"; see
> `[[project_aux_tasks_router_outage]]`.

## Goal

Make a sustained auxiliary outage **loud** and **queryable**, without changing
the non-fatal contract (callers still swallow-and-continue).

## Design

### Phase 1 (this change) — in-agent health + escalation

`AuxHealth` tracker attached to the shared `AuxiliaryLLM` instance
(`src/services/auxiliary.py`). The four task helpers record success/failure on
it (they already have the `try/except`):

- `extract_and_store_memories` → `memory_extraction`
- `curate_and_store_knowledge` → `knowledge_curation`
- `assemble_memories` → `memory_assembly`
- `_generate_title` (persistent_app) → `title_generation`

Behaviour:

- **Escalation:** after `ESCALATE_AFTER` (=3) *consecutive* failures across any
  task, emit **one** `logger.ERROR` `AUXILIARY MODEL DEGRADED: model=… N
  consecutive failures (… : <ErrType>: <msg>) …` — a distinct, greppable,
  alertable line. Re-emit every `REPEAT_EVERY` (=20) further failures so the
  alert stays live without flooding.
- **Recovery:** the next success emits `AUXILIARY MODEL RECOVERED` and resets.
- **Snapshot:** `auxiliary` block added to the worker agent's `get_status()`
  (and the persistent `/status`), so `kubectl exec … curl /status` shows
  `degraded`, `consecutive_failures`, and per-task counters/last-error.

No control-flow change: failures still propagate to the existing `except` and
are still swallowed. The tracker is in-process and cannot itself fail the task.

### Phase 1.5 (this change) — log failed aux calls to the debug view

Main-loop LLM failures are visible because they flow into the job's
`error_message`/status. Auxiliary calls have **no** such surface: a successful
aux call is archived to `llm_requests` (distinct `call_type`), but the archive
call sits *after* the `await`, so a **failed** aux call is never recorded
anywhere — it just vanishes. (Empirically: `llm_requests` held only
`call_type="main"` rows; zero aux rows across the 3-day outage.)

Change:

- New **`LLMArchiver.archive_error()`** writes a failed call to `llm_requests`
  with `status="error"`, `error={type,message}`, `response=None`, and the
  task's `call_type` (`memory_extraction`, …). Mirrors `archive()`, never
  raises.
- `AuxiliaryLLM` routes `chain()` + `agent()` LLM calls through a small
  `_invoke_aux()` wrapper that calls `_archive_error()` on exception, then
  re-raises (so callers still swallow). At most one error row per call. No-ops
  when no archiver/job_id is wired, so it's safe on every path.

Now a degraded aux model leaves a per-request error row in `llm_requests`
alongside the main calls.

**Still open after this change** (call out so it isn't mistaken for done):

- **Session aux calls are still not archived at all** — the persistent-session
  `AuxiliaryLLM`s (`persistent_app.py:877`, `:3400`) are built without an
  archiver and never get `set_job_context`. Wiring that is a small follow-up.
- **The debug UI still defaults to `call_type="main"`** — the agent's
  `get_conversation` defaults to `"main"` and the orchestrator's
  `get_job_chat_history` exposes no `call_type` param, so the new aux/error
  rows are written but not yet *shown*. Surfacing them (a filter / an
  "errors" toggle) is part of the read-side work below.

### Phase 2 (follow-up, not in this change) — central visibility

- Include the `AuxHealth` snapshot in the agent → orchestrator **heartbeat**
  (`POST /api/agents/{id}/heartbeat`), persist `aux_degraded` on the agent row,
  and show a warning badge in the Cockpit admin agents view.
- Optional: a Prometheus gauge (`srw_aux_consecutive_failures`) once the
  observability stack (see `[[project_observability_and_quotas]]`) lands.

### Related, separately actionable

- The aux default resolves to `gemma-4-moe`, which lives on the *flaky*
  workstation backend. Pinning aux **chat** to `gemma-4-31b` (always-on
  `:18080`) would keep titles + extraction alive during workstation downtime.
  Memory *persistence* still needs `qwen3-embedding-8b` (workstation-only), so
  this is mitigation, not a full fix.

## Acceptance criteria

- [ ] 3 consecutive aux failures produce exactly one `AUXILIARY MODEL DEGRADED`
      ERROR; a 4th does not re-log (until `REPEAT_EVERY`).
- [ ] A success after degradation logs `AUXILIARY MODEL RECOVERED` and resets.
- [ ] `get_status()["auxiliary"]` reflects `degraded` + per-task counters.
- [ ] No change to the swallow-and-continue behaviour of any task.
- [ ] Unit tests for threshold, repeat-throttle, recovery, snapshot.
- [x] A failed aux call (worker job) writes one `llm_requests` row with
      `status="error"`, the task `call_type`, and the error type/message.
- [x] `archive_error` no-ops without a Mongo connection; `_archive_error`
      no-ops without an archiver/job_id; the original exception still
      propagates and is still swallowed by the caller.
- [ ] Read-side: aux/error rows are surfaceable in the debug view
      (orchestrator `call_type`/`status` filter + Cockpit toggle) — follow-up.
- [ ] Session aux calls get an archiver wired — follow-up.
