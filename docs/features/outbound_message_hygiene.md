# Outbound message hygiene — no malformed tool calls in durable state, no infinite retries on deterministic errors

## Status

IMPLEMENTED 2026-07-12 (same-day as the design), uncommitted. The "F5 /
next" item from
[`docs/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md`](../done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md)
(roadmap section). As-built deltas from the original proposal (both make the
design *simpler*, see the layer descriptions):

- **No synthetic ToolMessage on drop.** LangChain parks unparseable calls in
  ``invalid_tool_calls`` — they never enter ``tool_calls``, so no result
  message ever existed and pairing cannot break. Dropping needs only a scrub
  of ``invalid_tool_calls`` + the raw ``additional_kwargs`` entry, plus a
  note appended to the message *content* as the model's feedback channel.
- **``persistent_graph.py:1381`` kept as-is.** That ``invalid_tool_calls=[]``
  is the *interrupt* path deliberately stripping incomplete calls from a
  partial response — correct behavior, not the ingestion band-aid this doc
  originally assumed. The ingestion repair landed on the normal finalize
  path (before ``messages.append`` + persist) instead.
- **Fingerprint gate narrowed to request-shaped 4xx** (excluding 429/rate):
  a genuine outage repeats identical generic "connection error" text across
  cycles and must keep pausing — fingerprint-failing those would destroy the
  outage feature. Implemented orchestrator-side only (no agent change):
  ``llm_outage_fingerprint`` in ``services/completion.py``, stored via
  ``increment_job_llm_outage_attempt``, decided in ``determine_job_status``.

Verified: `tests/test_context_methods.py` (repair/scrub, incident-A fixture)
+ `tests/test_llm_outage_resilience.py` (fingerprint + fail-fast) — 82
tests green, plus full graph suites (283).

## Problem — two incidents, one missing invariant

**Incident A (2026-07-11, worker, MiniMax-M3, loop job `6a186c76`):** after a
441 s generation, the model emitted a tool call whose `arguments` string was
not valid JSON. It was checkpointed verbatim in the AIMessage. Every
subsequent LLM request replays that message, and MiniMax rejects its *own
output* on input validation: `400 bad_request_error: invalid params, invalid
function arguments json string, tool_call_id: call_E7U6… (2013)` — the same
`tool_call_id` across pause/resume cycles proved the poison is durable. The
classifier fix (F3) makes this fail *fast* now, but the iteration still dies.

**Incident B (2026-06-02, persistent, gpt-5.5):** compaction thrash left a
`ToolMessage` without its `AIMessage` partner → Responses-API 400 "No tool
call found for function call output". Fixed by `repair_tool_pairing`
(`src/core/context.py:283`), wired into `persistent_graph.py:1150` and the
resume paths.

**Current state of the seams** (verified 2026-07-12):

| Seam | Worker (`graph.py`) | Persistent (`persistent_graph.py`) |
|---|---|---|
| Orphaned-result sanitize before send | `sanitize_message_history` (`graph.py:1161`) | `repair_tool_pairing` (`:1150`) |
| Invalid tool-call handling at ingestion | **nothing** | `response.invalid_tool_calls = []` (`:1381` — drops the parse error, keeps nothing) |
| Tool-call **argument** validation | **nothing** | **nothing** |

The missing invariant: **message history sent to a provider must be
well-formed by construction** — arguments parseable, pairing intact — because
checkpoints replay forever and at least one provider (MiniMax) validates
history tool calls on input.

## Design

Three layers, cheapest-first. All shared helpers live in `src/core/context.py`
next to `repair_tool_pairing` so worker and persistent graphs use one
implementation.

### 1. Ingestion-time repair (the real fix)

New `repair_tool_call_arguments(msg: AIMessage) -> AIMessage`, called at
response finalization in BOTH graphs (worker: where the execute node receives
the model response; persistent: replacing the bare `invalid_tool_calls = []`
at `persistent_graph.py:1381`):

- For each entry in `msg.invalid_tool_calls` (LangChain's parse failures) and
  each `additional_kwargs["tool_calls"]` entry whose `function.arguments`
  fails `json.loads`:
  1. **Repair**: attempt lightweight JSON repair (trailing-comma strip,
     unescaped-control-char fix, truncation-close). If the repaired string
     parses, rewrite the tool call with the repaired arguments and log a
     WARNING with the tool name + what was repaired.
  2. **Drop**: if unrepairable, remove the tool call from the AIMessage
     (`tool_calls`, `invalid_tool_calls`, AND the raw
     `additional_kwargs["tool_calls"]` entry — the raw dict is what gets
     re-serialized to the provider) and append a synthetic `ToolMessage`
     (`status="error"`, "model emitted unparseable arguments for <tool>; the
     call was discarded — re-issue it") so the model sees *why* and pairing
     stays intact. If the AIMessage ends up with zero tool calls and empty
     content, give it a one-line content stub so providers that reject empty
     assistant messages don't 400.
- Repair library: start with a ~30-line internal fixer, not a new dependency;
  `json-repair` (pypi) as a fallback discussion point if the internal one
  proves insufficient.

Nothing malformed reaches the checkpoint. This is the layer that would have
made incident A a logged non-event.

### 2. Send-time argument sanitize (backstop)

Extend `sanitize_message_history` (worker) and the pre-`astream` repair
(persistent) to also run the argument check from layer 1 over the whole
outbound window. Catches poison already sitting in old checkpoints (e.g. any
currently-frozen job with a bad message resumes clean instead of dying at the
first LLM call) and anything a future ingestion-path bug lets through.
Cost: one `json.loads` per historical tool call per send — negligible against
an LLM round-trip; can memoize by message id if profiling ever disagrees.

### 3. Determinism fingerprint on LLM errors (retry-policy hardening)

Orthogonal to classification (`_classify_llm_error`): in the execute-node
retry loop, fingerprint each failure (`status_code` + error type + normalized
message, truncated). If the SAME fingerprint occurs on every attempt of a
retry group AND matches the fingerprint of the previous `llm_unavailable`
pause (persisted in `context.llm_outage.fingerprint` by the orchestrator's
existing outage bookkeeping), reclassify as permanent regardless of the
enum verdict. Bounds the damage of ANY future classifier gap to one
pause/resume cycle (~minutes) instead of forever. The graph already
fingerprints tool-loop stuck detection — same pattern, applied to LLM errors.

Touchpoint: `src/graph.py` execute-node retry loop (classification at
`~:2353`); orchestrator side: include the fingerprint in the `/complete`
payload's `freeze_data` and thread it into `context.llm_outage` next to
`attempt` (`postgres.py:increment_job_llm_outage_attempt`).

## Non-goals

- Fixing WHY MiniMax emitted broken JSON after a 441 s generation (stream
  truncation / provider bug — not controllable from here).
- Schema-validating arguments against the tool's parameter schema (the tool
  node already surfaces those errors to the model as normal tool errors).
- Any change to compaction (incident B's trigger) — pairing repair already
  covers the send path.

## Test plan

- Unit: `repair_tool_call_arguments` — parseable passthrough (zero-copy),
  repairable (trailing comma, truncated string, control chars), unrepairable
  → dropped + synthetic error ToolMessage + raw `additional_kwargs` cleaned,
  multi-call message with one bad call keeps the good ones.
- Unit: send-time sanitize catches a poisoned message planted mid-history.
- Unit: fingerprint — same error 2 cycles → permanent; different errors →
  normal classification; fingerprint survives freeze/resume round-trip.
- Regression fixture: an AIMessage shaped like incident A's (unparseable
  `arguments` in `additional_kwargs.tool_calls[0]`) goes through ingestion →
  outbound payload contains no malformed arguments.

## Acceptance criteria

1. A model response with unparseable tool-call arguments produces a WARNING
   (repair) or a synthetic error ToolMessage (drop) — never a checkpointed
   malformed call. Verified by unit fixture shaped like incident A.
2. A checkpoint that already contains a malformed call (pre-fix job) resumes
   and its first LLM request contains only valid JSON arguments.
3. An LLM error with an identical fingerprint across two pause/resume cycles
   fails the job permanently on the second cycle even if
   `_classify_llm_error` says transient.
4. Persistent sessions: `invalid_tool_calls = []` at
   `persistent_graph.py:1381` is replaced by the shared repair (same
   behavior for parseable calls, better behavior for broken ones).
5. No behavior change for well-formed histories (the common case) — verified
   by the existing graph/persistent test suites staying green.
