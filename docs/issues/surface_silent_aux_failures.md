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

## Status (2026-06-11)

Built on `develop` (uncommitted), verified locally — `ruff check`/`format`
clean, unit tests green (`tests/test_aux_health.py`, `tests/test_auxiliary.py`):

- ✅ **Phase 1** — in-agent health tracker + escalation/recovery + `get_status()` exposure.
- ✅ **Phase 1.5** — failed aux calls archived to `llm_requests` (`status="error"`).
- ⏳ **Phase 1.6 (NEXT)** — surface aux + error rows in the debug view (read side)
  + wire the session-path archiver. *Not started.*
- ☐ **Phase 2** — central visibility (heartbeat → Cockpit agent badge). *Not started.*
- ☐ **Mitigation** — pin aux default off `gemma-4-moe`. *Not started (separate).*

Not yet exercised end-to-end on a cluster (needs a deploy + a forced aux
outage); the logic is unit-covered.

## Design

### Phase 1 — in-agent health + escalation ✅

*Implemented:* `AuxHealth` + `_TaskHealth` in `src/services/auxiliary.py`
(recorded from the four task helpers); snapshot surfaced via
`UniversalAgent.get_status()` in `src/agent.py`. Tests: `tests/test_aux_health.py`.

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

### Phase 1.5 — log failed aux calls (write side) ✅

*Implemented:* `LLMArchiver.archive_error()` in `src/core/archiver.py`;
`AuxiliaryLLM._invoke_aux()` + `_archive_error()` in `src/services/auxiliary.py`
(wraps the `chain()` + `agent()` LLM calls). Tests: `TestAuxErrorArchiving` +
`TestArchiveError` in `tests/test_auxiliary.py`.

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
alongside the main calls — on **worker jobs** (where the archiver is wired via
`set_job_context`). Sessions still need the archiver wired (Phase 1.6).

### Phase 1.6 (NEXT) — surface aux + error rows in the debug view (read side)

Phases 1 + 1.5 make the data *exist*; this makes it *visible*. The rows are
written but still filtered out on read:

- The agent's `LLMArchiver.get_conversation` defaults to `call_type="main"`.
- The orchestrator's `get_job_chat_history` (`orchestrator/main.py:8431`)
  exposes no `call_type`/`status` param, and `mongodb.get_chat_history` builds
  a main-only sequential view.
- Session aux calls aren't archived at all yet (no archiver on the
  persistent-session `AuxiliaryLLM`s at `persistent_app.py:877`, `:3400`), so
  there's nothing to show for sessions.

Plan:

1. **Orchestrator read path** — add optional `call_type` + `status` query
   params to `GET /api/jobs/{id}/chat-history` and thread them into
   `mongodb.get_chat_history`. Default unchanged (main-only sequential view);
   `call_type=all` includes aux rows; `status=error` returns only failures
   across call types.
2. **Session archiver** — wire `set_job_context` (or pass an archiver) for the
   persistent-session `AuxiliaryLLM`s so session aux failures also get rows;
   without it `_archive_error` no-ops on the session path.
3. **Cockpit** — debug/LLM-requests view gets a source filter (All / Main /
   Aux) + an "errors only" toggle; error rows render the `error.type/message`
   with a degraded badge.

Acceptance: a forced aux failure (worker *and* session) shows an error row in
the debug view filtered by `status=error`, with the model + error type.

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

- [x] 3 consecutive aux failures produce exactly one `AUXILIARY MODEL DEGRADED`
      ERROR; a 4th does not re-log (until `REPEAT_EVERY`).
- [x] A success after degradation logs `AUXILIARY MODEL RECOVERED` and resets.
- [x] `get_status()["auxiliary"]` reflects `degraded` + per-task counters
      (snapshot content unit-tested; `get_status()` wiring by inspection).
- [x] No change to the swallow-and-continue behaviour of any task.
- [x] Unit tests for threshold, repeat-throttle, recovery, snapshot.
- [x] A failed aux call (worker job) writes one `llm_requests` row with
      `status="error"`, the task `call_type`, and the error type/message.
- [x] `archive_error` no-ops without a Mongo connection; `_archive_error`
      no-ops without an archiver/job_id; the original exception still
      propagates and is still swallowed by the caller.
- [ ] Read-side: aux/error rows are surfaceable in the debug view
      (orchestrator `call_type`/`status` filter + Cockpit toggle) — follow-up.
- [ ] Session aux calls get an archiver wired — follow-up.
