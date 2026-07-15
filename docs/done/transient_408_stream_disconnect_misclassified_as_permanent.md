---
tags:
  - issue
  - bug
  - agent
  - llm-routing
  - retry
  - resilience
  - self-improvement-loop
  - resolved
related:
  - "[[agent_infinite_retry_on_permanent_llm_errors]]"
  - "[[llm_outage_pause_and_backoff_redispatch]]"
  - "[[stale_agent_detector_sql_crash_disables_recovery_sweeps]]"
  - "[[coincident_infra_error_overrides_reported_job_outcome]]"
---

# A transient 408 stream-disconnect is misclassified `permanent` and hard-fails jobs

**Filed:** 2026-07-14, while triaging two "failed" jobs the user flagged on the
Better-Resavio ERP self-improvement loop (project `68137e29`, main cluster).
**Status:** ✅ **RESOLVED — shipped to `develop` 2026-07-14.** Commits:
`73a545c8` (classifier fix + first 4 test cases) and `3fbff814` (the status-gate
generalisation + the 499/422 cases, bundled with the cloud-diff work). Both are
carried by image `sha-5b41346` (deploy commit `bcf43638`). Verified: 171 tests
pass across `tests/test_graph_helpers.py` + `tests/test_graph.py`, ruff clean;
change is purely additive. Not yet observed against a live 408 in the loop —
see Follow-ups. Symbols/line numbers current as of this date.

This is the **exact inverse** of [[agent_infinite_retry_on_permanent_llm_errors]]
(the 2026-05-12 cluster outage). That incident taught the classifier to fail fast
on non-retriable errors; this one is the overcorrection biting back. Read the two
together — they are the two failure directions of one function.

## TL;DR

`_classify_llm_error` (`src/graph.py:407`) classified a **transient** 408
stream-disconnect as `permanent`, so the execute node hard-failed the job on
**attempt #1 with zero retries**, destroying 3.5 hours of scholar research.

The classifier already defaults to `transient` (final line; pinned by
`test_no_status_no_class_no_keyword_is_transient`). The bug was **not** a missing
retryable-error entry — it was an over-greedy *denylist* rule: a text fallback
written for "a stringified 400 that lost its exception class" was gated on
**wording, not status**, so it claimed a 408.

## Observed behavior

Scholar subjob `35b23256-0606-470e-8a76-cc626be2d983` ("Research phase for:
Design the UI theme and complete mockup suite for Hotel Rheinland ERP",
model `gpt-5.6-sol`, Responses API via the Codex proxy):

- Created `2026-07-14T16:27:56Z`, died `19:59:07Z` — **~3.5h**, **259 LLM
  requests**, **1824 audit entries** of healthy `kb_search`/`kb_write`/
  `read_file` work.
- Final error:

```
Error code: 408 - {'error': {'message': 'stream error: stream disconnected
before completion: stream closed before response.completed',
'type': 'invalid_request_error'}}
```

- Audit entry `[1823]` — the smoking gun:

```
ERROR: {'type': 'llm_error', 'message': "Error code: 408 - ...",
        'attempts': 1, 'recoverable': False, 'classification': 'permanent'}
```

`attempts: 1` means the inner retry loop never ran. The consumer
(`src/graph.py:2517`) returns `should_stop=True` + an `error` key, which
short-circuits `determine_job_status`
(`orchestrator/services/completion.py:597`) straight to `failed`.

**Blast radius was smaller than it looked:** the scholar's durable output was
`kb_write`, which is project-scoped in pgvector, not workspace-local. Six notes
survived the teardown. Workspace files would not have.

## Root cause

Trace of the 408 through `_classify_llm_error`:

1. **Status branch** — 408 is not 429, not in `_PERMANENT_STATUS =
   {400,401,403,404}`, not 5xx. **Falls through.** (No 408 case existed.)
2. **Class-name branch** — 408 has no dedicated OpenAI SDK subclass (it surfaces
   as a generic `APIStatusError`). **No match.**
3. **Text fallback** — `str(exc)` contains `invalid_request_error` and not
   `tool_use_failed` → **`return "permanent"`.**

Step 3 is the defect. That rule exists for stringified 400s (real production
errors do lose their exception class — see
`test_stringified_bad_request_error_is_permanent`), but nothing constrained it to
400s. The Codex/CLIProxyAPI proxy stamps the **`invalid_request_error` label on a
transport failure**, so a rule shaped for deterministic input rejection swallowed
a dropped socket.

## Why NOT "just retry everything"

Recorded because it is the obvious next suggestion, and it is wrong here.

Retry is **not free**: exhausted inner retries feed the pause+backoff+re-dispatch
path ([[llm_outage_pause_and_backoff_redispatch]]), so a wrongly-`transient`
error does not cost 5 attempts — it can loop indefinitely. Both directions have
burned us:

| Direction | Incident | Cost |
|---|---|---|
| Too eager `transient` | 2026-05-12: 404 model-not-found looped 70+ iterations against a guaranteed-failure endpoint | Cluster outage (audit write storm) |
| Too eager `transient` | 2026-07-11: MiniMax deterministic `invalid function arguments json string` 400 classified transient | pause/backoff-looped **forever** ([[stale_agent_detector_sql_crash_disables_recovery_sweeps]], Finding 3) |
| Too eager `permanent` | **This issue** | 3.5h of work destroyed, 0 retries |

Backstop for the first row is `_LLM_ERROR_STREAK_CAP = 5` (`src/graph.py:294`).
`permanent` still earns its keep for genuinely non-retriable input rejections and
for actionable operator messaging (`quota_exhausted`, `cooldown`).

## Resolution

Fix the **rule**, not the symptom — a 400-shaped rule may only claim a 400-shaped
error. All changes in `src/graph.py::_classify_llm_error`:

1. **`_TEXT_INPUT_REJECTION_STATUS = frozenset({"400", "422"})`**
   (`src/graph.py:404`) + a status gate in the text fallback
   (`src/graph.py:571`):
   `m_status = re.search(r"error code:\s*(\d{3})", error_str)` → if a status is
   present and is **not** an input-rejection status, `return "transient"`.
   This generalises past 408 to **every** future transport status (409, 425, 499,
   5xx …) with **no new marker required**. 422 stays `permanent` — it genuinely
   *is* an input rejection — so this is a principled gate, not a blanket
   "retry everything that isn't 400".
2. **`status_code == 408` → `transient`** in the status branch (408 Request
   Timeout is retryable by definition).
3. **`_is_stream_disconnect()`** (`src/graph.py:394`) +
   `_STREAM_DISCONNECT_MARKERS` (`src/graph.py:383`) — belt-and-braces for
   status-less text, and guards in both body branches (400-status and
   `BadRequestError`) so a disconnect surfaced *as* a 400 also stays transient.

Net effect: this class now **retries with backoff**, and on the Postgres
checkpointer **pauses + resumes from checkpoint** instead of dying. A single blip
like the incident clears on attempt #2.

## Verification

`tests/test_graph_helpers.py::TestClassifyLlmError`, 6 added cases:

- `test_408_stream_disconnect_is_transient` — the incident, as an SDK error.
- `test_stringified_408_stream_disconnect_is_transient` — the incident, in the
  production audit shape (class lost).
- `test_stringified_novel_status_with_rejection_label_is_transient` — **the
  anti-whack-a-mole proof**: a 499 with an `invalid_request_error` label and *no*
  stream wording, which no marker list would catch.
- `test_stringified_422_stays_permanent` — the gate is principled, not blanket.
- `test_400_stream_disconnect_is_transient` — disconnect surfaced as a 400.
- Existing `test_400_invalid_request_is_permanent`,
  `test_400_minimax_bad_request_error_is_permanent`,
  `test_stringified_bad_request_error_is_permanent` all still pass — genuine
  input rejections are unaffected.

`171 passed` (`test_graph_helpers.py` + `test_graph.py`), `ruff check` +
`ruff format --check` clean. Change is purely additive (no deletions).

## Follow-ups (not in this change)

1. **The asymmetry is the real bug** (highest leverage, UNBUILT). Even with a
   perfect classifier, being wrong toward `permanent` destroys hours while being
   wrong toward `transient` is ~bounded by the circuit breaker. Make giving up
   **non-destructive**: route `permanent` to `pending_review` (checkpoint
   preserved, operator fixes the model/key/quota, resume) instead of `failed`.
   Then a misclassification costs a resume, not a run, and the classifier stops
   being load-bearing. **Caveat:** `paused` is auto-polled by the dispatcher
   (`status IN ('created','paused')`), so a naive pause re-dispatches straight
   back into the same error — the 2026-05-12 shape. `pending_review` is the right
   target because it is not auto-dispatched. Touches
   `orchestrator/services/completion.py::determine_job_status`.
2. **Sibling failure, same 18:27 batch, independent and still OPEN:** designer
   parent `ab8680c5-209e-4a38-a037-47ae290c1f15` failed at init cloning
   `project-68137e29-jobs` — *"refusing to fall back to a disconnected git init"*.
   The guard is **correct** (it prevented silent work loss); the underlying
   backend→Gitea reachability is the real issue. Recurred: critic `42bbf782` on
   2026-07-10 against `project-1feeb7b8-jobs`, so it is systemic, not a fluke.
   The clone already retries 3× (`src/core/workspace.py:44`), so this is not a
   momentary blip. Needs a live-cluster check: is Gitea up, and can the VM /
   workspace backend resolve + reach the jobs-repo URL (NetworkPolicy?).
3. **Live confirmation still owed.** The fix is shipped but has not yet been
   observed catching a real 408 in the loop. Next occurrence should show
   `classification: transient` + `attempts > 1` in the audit trail (and, on the
   Postgres checkpointer, a `freeze_type: llm_unavailable` pause instead of a
   `failed`). Worth a grep of the audit store after the next multi-hour scholar
   run rather than a synthetic test.
4. **Doc-path drift:** `_classify_llm_error`'s docstring pointed at
   `docs/issues/agent_infinite_retry_on_permanent_llm_errors.md`; that doc lives
   in `docs/done/`. Repointed, and the docstring now names this doc as the
   inverse case. (Uncommitted at time of writing.)
