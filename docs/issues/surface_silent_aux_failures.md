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

## Status (2026-06-15)

Built on `develop` (uncommitted), verified locally — `ruff check`/`format`
clean, unit tests green (`tests/test_aux_health.py`, `tests/test_auxiliary.py`,
`tests/test_llm_requests_filter.py`, `tests/test_agent_heartbeat.py`):

- ✅ **Phase 1** — in-agent health tracker + escalation/recovery + `get_status()` exposure.
- ✅ **Phase 1.5** — failed aux calls archived to `llm_requests` (`status="error"`).
- 🟡 **Phase 1.6** — read side: ✅ backend (orchestrator `call_type`/`status`
  filter + projection on the **llm_requests** path) + ✅ session-path archiver
  wired; ⏳ **Cockpit UI toggle still open** — NB the debug "LLM-requests view"
  is the single-doc `request-viewer`; `ApiService` has no job-scoped
  llm-requests *list* method, so this is list-surface + ApiService wiring, not
  just a toggle. Tracked separately.
- ✅ **Phase 2** — central visibility (heartbeat → `agents.aux_degraded` →
  Cockpit admin badge). **Shipped + k3d-verified 2026-06-15** (see below).
- ☐ **Mitigation** — pin aux default off `gemma-4-moe`. *Not started (separate).*

Phase 2 verified end-to-end on k3d:

- Migration `app/0027` applied on the dev DB (survives orchestrator re-init).
- **Orchestrator/DB/UI:** a heartbeat with `metrics.aux.degraded=true` through
  the **real** internal endpoint set `agents.aux_degraded=t` + stored the
  compact summary in `metadata.aux`; `degraded=false` cleared both (recovery);
  the exact `list_agents`/`get_agent` SELECTs project the column; the admin
  Agents panel renders a red "⚠ Aux degraded" badge (with the `metadata.aux`
  tooltip) only for the degraded row.
- **Organic agent emission:** a freshly-provisioned session agent
  (`0bf257ff`, on the rebuilt image) emitted `metrics.aux` on its own 60s
  heartbeat — `{model: gemma-4-moe-strix, degraded: false, consecutive_failures:
  0, failing_tasks: {}}` — which the orchestrator persisted to
  `aux_degraded=false` + `metadata.aux`. So the full agent→orchestrator→DB pipe
  is confirmed live, not just injected.

The one path not exercised live is a real agent *failing* aux calls to flip
`degraded=true` itself (would need a forced aux-model outage); that transition
is unit-covered (`AuxHealth` escalation + `heartbeat_summary` shape) and the
healthy organic emit + the injected degraded round-trip together cover the
mechanism. It'll surface for real during the memory-overhaul soak.

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

### Phase 1.6 — surface aux + error rows in the debug view (read side)

Phases 1 + 1.5 make the data *exist*; this makes it *visible*.

> **Correction (found during impl):** the right read surface is the
> **`llm_requests`** path (`get_job_llm_requests` → `mongodb.list_llm_requests`),
> **not** `chat_history`/`get_job_chat_history` — that collection is main-loop
> only by construction (the conversational turn view) and aux calls never land
> there. `list_llm_requests` already returned *all* call types (`query =
> {job_id}`); the gap was its projection dropped `call_type`/`status`/`error`.

1. ✅ **Orchestrator read path** — `mongodb.list_llm_requests` gained optional
   `call_type` + `status` filters and now projects `call_type`/`status`/`error`
   (pre-`call_type` rows default to `"main"`); `GET /api/jobs/{id}/llm-requests`
   (`orchestrator/main.py`) exposes both as query params. `call_type=all`/omitted
   = main + aux; `status=error` = failures only. Tests:
   `tests/test_llm_requests_filter.py`.
2. ✅ **Session archiver** — `_wire_session_aux_archiver()` in
   `src/api/persistent_app.py` points the (shared) session `AuxiliaryLLM` at the
   default archiver with `job_id=_thread_id`, `agent_type="persistent"`; called
   from `_loop_on_turn_complete` (every turn) and after the aux hot-swap. Without
   it `_archive_error` no-ops on the session path.
3. ⏳ **Cockpit (NEXT)** — debug/LLM-requests view gets a source filter
   (All / Main / Aux) + an "errors only" toggle; error rows render the
   `error.type/message` with a degraded badge. Wire to the new query params.

Acceptance: a forced aux failure (worker *and* session) shows an error row in
the debug view filtered by `status=error`, with the model + error type.

### Phase 2 — central visibility ✅

*Implemented 2026-06-15:*

- `AuxHealth.heartbeat_summary()` (`src/services/auxiliary.py`) — a compact
  projection (degraded flag, aggregate failure count, model, per-failing-task
  last-error-type) smaller than `snapshot()`. Always includes `degraded` so a
  recovered agent clears the persisted flag.
- Both heartbeat loops fold it into `metrics.aux` (`_aux_health_for_heartbeat`
  + `_get_agent_metrics` in `src/api/app.py` for workers and
  `src/api/persistent_app.py` for sessions). Best-effort; None before the aux
  LLM is wired so the orchestrator leaves a stale flag untouched.
- Migration `app/0027_agents_aux_degraded.sql` adds `agents.aux_degraded`
  (BOOLEAN NOT NULL DEFAULT FALSE).
- `agent_heartbeat` (`orchestrator/main.py`) extracts `metrics.aux.degraded`;
  `PostgresDB.heartbeat()` persists it (the two-branch UPDATE became a dynamic
  SET-clause builder so adding a column isn't a `$N` index-shuffling hazard);
  `list_agents`/`get_agent` project the column. The compact summary rides in
  `agents.metadata.aux` (existing metrics → metadata merge) for the tooltip.
- Cockpit: `Agent.aux_degraded` model field + a red "⚠ Aux degraded" badge in
  the admin Agents view (`agent-list.component.ts`), gated on `aux_degraded`,
  tooltip built from `metadata.aux`. i18n in en + de-DE.
- Tests: `TestAuxHeartbeatSummary` (`test_aux_health.py`),
  `TestHeartbeatAuxDegraded` (`test_agent_heartbeat.py`, incl. a param-index
  alignment guard), `auxTooltip` specs (`agent-list.component.spec.ts`).

- Optional/deferred: a Prometheus gauge (`srw_aux_consecutive_failures`) once
  the observability stack (see `[[project_observability_and_quotas]]`) lands.

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
- [x] Read-side backend: `list_llm_requests` filters by `call_type`/`status`
      and projects `call_type`/`status`/`error`; `/api/jobs/{id}/llm-requests`
      exposes the params. (`tests/test_llm_requests_filter.py`)
- [x] Session aux calls get an archiver wired (`_wire_session_aux_archiver`,
      called from `_loop_on_turn_complete` + after the aux hot-swap).
- [ ] Cockpit: debug-view source filter (All/Main/Aux) + "errors only" toggle
      that renders `error.type/message` — follow-up.
- [ ] End-to-end on a cluster: a forced aux failure (worker *and* session)
      shows an error row filtered by `status=error` — pending a deploy.
