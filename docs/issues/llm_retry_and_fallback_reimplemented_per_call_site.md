---
tags:
  - issue
  - agent
  - llm
  - retry
  - delegation
  - auxiliary
  - refactor
---

# LLM retry/fallback is reimplemented at every call site — two sites have none, one misfires, four backoff schedules disagree

**Status:** DIAGNOSED 2026-07-29, UNBUILT. Design agreed (mechanism/disposition
split, see "Proposed shape"); no code written.
**Severity:** medium-high — one confirmed live job damaged
(`37c418d2`, 2026-07-28), a standing cost leak on every auxiliary blip, and a
false-positive health signal on the aux heartbeat. Nothing is currently
job-fatal, but the same class of bug has been fixed three times in three
places and will be fixed a fourth unless the mechanism is shared.
**Component:** `src/graph.py`, `src/persistent_graph.py`,
`src/core/summarizer.py`, `src/services/auxiliary.py`,
`src/services/memory/extraction_engine.py`,
`src/tools/delegation/light_runner.py`

## Triggering incident

Critic job `37c418d2-1208-4df8-b8d3-d5c75b1f9413` (branch
`subjob/37c418d2/critic`, model gpt-5.6-sol, dev cluster), verifying designer
job `4435994d-b029-444d-8a3c-26c64abd456a`.

Audit entry [221] shows one LLM turn (doc_id 82074) emitting
`read_file, read_file, kb_list, spawn_subagent, spawn_subagent` — two genuinely
parallel subagent spawns. Entries [225] ("contract archaeologist") and [226]
("git provenance auditor") are **both** `Tool [FAIL]`, both bodies matching
`408` + `stream disconnected`.

Blast radius was contained: the critic wrote KB note
`verification-4435994d-subagent-delegation-unavailable` ([259], tagged
blocker/failed-approach), abandoned delegation, fell back to `shell_execute` +
git, and kept running. Cost was two wasted readers and a slower path — not the
job. Intermittent, not systemic: `4119f03c`, `acf757b5`, `a5766ee0` show no
`subagent failed`, and `4119f03c` completed four successful spawns.

**This is not the 120 s watchdog bug.** That one
(`spawn_subagent_fanout_trips_delegation_batch_watchdog.md`) is fixed and
verified live: `tool_category_timeouts.delegation: 600`
(`config/worker_base.yaml:253`), `delegation.light.timeout_seconds: 240`
(`:429`), wall-clock deadline at `light_runner.py:209-283`. Note
`config/defaults.yaml` was renamed to `config/worker_base.yaml` in `57430a2a`,
so line refs in that older doc are stale.

## Inventory — six LLM call sites, six different answers

| site | classify | retry loop | backoff | terminal disposition |
|---|---|---|---|---|
| `graph.py` worker execute (ainvoke) | ✅ `_classify_llm_error:517` | ✅ `ToolRetryManager("llm_invoke")` `:4901` | exp, `limits.llm_inproc_retries` | freeze → pause+backoff re-dispatch; streak cap `:321`; emergency compaction |
| `persistent_graph.py` session (astream `:1479`,`:1525`) | ✅ shared, lazy import `:558` | ✅ own `:1503` | `2.0 * 2**attempt` `:511`, max 3 `:509` | surface to cockpit, session survives |
| `core/summarizer.py` fold `:315` | ⚠️ own non-retryable check `:114` | ✅ own | `(5.0, 15.0)` `:51`, max 3 `:50` | raise → caller degrades to trimming |
| `memory/extraction_engine.py` `:267` | ⚠️ own | ✅ own | `(5.0, 15.0)` `:51` | swallow, non-fatal |
| `services/auxiliary.py` | ❌ none | ❌ none | — | model fallback aux→main `:1074` |
| `delegation/light_runner.py` `:226` | ❌ none | ❌ none | — | dies |

Four backoff schedules, two sharing a classifier, two with nothing.

## Finding 1 — subagent readers have no LLM retry at all

`light_runner.py:226` is
`ai = await asyncio.wait_for(llm.ainvoke(messages), timeout=remaining)` and
catches **only `asyncio.TimeoutError`** — that is the wall-clock deadline, not
provider errors. `grep -rn "retry\|backoff\|_classify_llm_error"
src/tools/delegation/` returns nothing.

So any 408/429/5xx propagates out of `run_light_subagent`, is swallowed by the
blanket `except Exception` at `spawn_subagent.py:197`, and becomes the string
`result = f"Error: subagent failed — {e}"`.

The 408→transient fix (`transient_408_stream_disconnect_misclassified_as_permanent.md`,
shipped 2026-07-14) lives in `_classify_llm_error` (`graph.py:517`) and is
consumed at `graph.py:2644` inside the parent execute node's `attempt` loop —
**parent-only**. Readers are a separate in-process ReAct harness with no graph,
so they inherit none of it. One transport blip, zero attempts, reader dead.

Because a fan-out's readers run concurrently against the same provider, a
single blip takes out the *entire batch* — the 2/2 seen above.

**Corollary:** the "owed live confirmation" on that 408 doc is still owed.
`37c418d2` does not confirm it, because these 408s never reached the classifier.

### 1b — failures are labelled `[subagent done]`

`_format_result` (`spawn_subagent.py:130-133`) stamps the header
`[subagent done]` on the error string too, so the parent model reads "done"
followed by an error body. The audit `[FAIL]` marking is correct; the text
handed to the LLM actively misleads.

## Finding 2 — auxiliary has fallback but no retry, so fallback is doing retry's job

The fallback itself is the best-factored piece in the inventory and is **not**
the problem. All three entrypoints — `ainvoke:1196`, `chain:1273`, `agent:1344` —
route through `_ainvoke_fallback:1074` (at `:1210`, `:1316`, `:1375`, `:1433`).
The old bypass is gone: `ainvoke:1196` exists specifically for call sites that
"used to reach past the wrapper into `auxiliary_llm.llm.ainvoke(...)` directly
(e.g. session title generation)". Every external caller uses `.chain()` or
`.agent()` (`persistent_app.py:7080`, `summarizer.py:368`,
`extraction_engine.py:250`, `memory/ingestion.py:71`,
`knowledge/ingestion.py:99`). No bypasses remain.

What aux has none of is **retry or classification** — grepping `auxiliary.py`
for `classify|for attempt|backoff|asyncio.sleep` returns nothing. Four
consequences:

1. **One transient blip escalates straight to the expensive model.** There is
   no attempt 2 on the aux model. A single 408 reroutes memory extraction,
   title generation and compaction onto the main top-tier model. Standing cost
   leak; this is the mechanism behind the known aux-misroute problem.
2. **No classification — permanent and transient are identical.** A 404
   model-not-found from a bad aux config burns the fallback exactly like a
   stream disconnect, and since the config is what is wrong, the escalated call
   is paid for and fails too.
3. **The escalated path is itself unprotected.** `:1171`
   `await asyncio.wait_for(build_runnable(self.fallback_llm, ...).ainvoke(...))`
   sits outside any try/except. If the main model also blips, it raises straight
   out — and per the wrapper's own docstring that becomes
   `SummarizationFailed('aux_unavailable')`, where "the caller then fails the
   turn/session". The last-resort path is the one with no retry.
4. **The health signal false-positives.** `mark_aux_unreachable:894` has no
   threshold — the first failure flips `_aux_reachable = False`, logs
   `AUXILIARY MODEL UNREACHABLE`, and lights the heartbeat `aux_degraded` flag.
   One stream disconnect declares the model down.

Aux is the clearest proof that **fallback and retry are different axes that
must compose**, not competing implementations of one thing.

## Finding 3 — the classifier is in the wrong home

`persistent_graph.py:558` does a function-local
`from .graph import _classify_llm_error`, commented: "Sessions and worker jobs
deliberately call the *same* `_classify_llm_error` so a given provider failure
gets one verdict product-wide" and "Imported lazily: `src.graph` is a heavy
module". `_session_llm_retry_delay:563` does the same dance for
`_extract_rate_limit_delay`.

That is a circular-import dodge around a 5264-line module. The sharing instinct
was already correct; it stopped at the classifier and never reached the loop.

## Proposed shape — shared mechanism, local disposition

What is genuinely common is the **mechanism**: classify the exception, decide
retryable, sleep with backoff floored by the provider's Retry-After, cap
attempts. Identical everywhere, copy-pasted four times.

What is genuinely different — and must stay local — is the **terminal
disposition** after retries are exhausted: worker freezes for checkpoint
re-dispatch; session surfaces an error and stays alive; summarizer raises so
the caller degrades to trimming; reader returns partial synthesis; aux falls
back to the main model. That divergence is correct policy, not accident.
Unifying it is what would make a shared helper unusable and get it bypassed.

New module `src/core/llm_retry.py`, owning:

- `classify_llm_error(exc)` — moved from `graph.py:517`
- `extract_rate_limit_delay(exc)` — moved from `graph.py:150`
- `summarize_llm_error(exc, model)` — moved from `graph.py:486`
- `RetryPolicy` dataclass — max_attempts, base delay or explicit schedule,
  retryable classifications
- `async def invoke_with_retry(fn, *, policy, on_attempt_failed=None)` — the
  loop; returns or raises the last error. Callers keep their own `except`.

**It must be a genuine move out of `graph.py`, not an import from it.** Two
independent constraints force this. `light_runner.py`'s docstring commits to
being "deliberately pure and infra-free" — that is what makes it unit-testable
with a fake LLM and no SSH; importing `src.graph` kills the property. And
`persistent_graph.py` already resorts to a lazy import precisely because
`src.graph` is heavy. The move is mechanical: `_classify_llm_error`,
`_summarize_llm_error` and `_extract_rate_limit_delay` are pure exception
inspection (`status_code`, class-name matching, regex over message text) with
no graph state, config or I/O.

Falls out for free: `graph.py:4901` currently drives LLM retries through
`ToolRetryManager(...)` with a magic `"llm_invoke"` category string — a *tool*
retry manager doing double duty. A real `RetryPolicy` retires that.

## Migration order

1. **Move the three pure helpers** to `src/core/llm_retry.py`, leaving thin
   re-export shims in `graph.py`. Zero behaviour change, independently
   valuable, kills the lazy import, unblocks everything else.
2. **Add `RetryPolicy` + `invoke_with_retry`; adopt in `auxiliary.py` first.**
   Ahead of `light_runner` because aux is actively misfiring in production on
   every blip, and the fix is additive — insert retry beneath an escalation
   that already exists and is already universally routed. Target shape: retry
   transient on aux → on permanent-or-exhausted fall back to main → retry
   transient on main → raise. `mark_aux_unreachable` moves to *after* retries
   are exhausted, which is when the model is genuinely unreachable rather than
   momentarily rude.
3. **`light_runner.py`.** Pure upside, no behaviour to preserve, closes
   Finding 1. Bound to ~2 attempts so it stays inside
   `delegation.light.timeout_seconds: 240`. Also fix Finding 1b — stop
   labelling failures `[subagent done]`. Proves the purity constraint holds.
4. **`summarizer.py` + `extraction_engine.py`.** Straight swaps of an identical
   `(5.0, 15.0)` / max-3 loop for the shared one.
5. **`graph.py` — optional, last, lowest priority.** It has ~1200 lines of
   error handling because it owns the richest disposition (freeze / pause /
   backoff / audit); the retry loop is a small part of that. Highest risk,
   lowest reward. Leave it consuming the shared classifier after step 1 and
   revisit only if it stops being the odd one out.

## Explicitly out of scope

- **SSH/clone backoff** — `core/workspace.py:46` (`_CLONE_BACKOFF_SECONDS`) and
  `core/backends/remote.py:73` (`_CHANNEL_OPEN_BACKOFF_SECONDS`) are a
  different failure domain with different signals. Leave them.
- **Aux model fallback as a concept** — a different axis from retry, not a
  competing implementation. It should stack on top, not merge in.

## Related

- `docs/issues/spawn_subagent_fanout_trips_delegation_batch_watchdog.md` — the
  *other* delegation failure mode; fixed and verified live 2026-07-29.
- `docs/done/transient_408_stream_disconnect_misclassified_as_permanent.md` —
  the parent-side 408 fix that Finding 1 shows does not reach readers.
- `docs/done/agent_infinite_retry_on_permanent_llm_errors.md` — the inverse
  failure direction; read together they are the two directions of one
  classifier. Bias for retry, but do not retry blindly.
- `docs/features/llm_outage_pause_and_backoff_redispatch.md` — the worker
  terminal disposition that must stay local.
