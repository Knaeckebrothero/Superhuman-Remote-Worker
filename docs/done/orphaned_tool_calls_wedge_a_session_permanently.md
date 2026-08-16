---
tags:
  - issue
  - sessions
  - officers
  - llm
  - liveness
status: resolved
priority: P0
created: 2026-08-15
resolved: 2026-08-16
aliases:
  - LF-5
  - orphaned tool call 400 wedge
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[persistent_compaction_tool_pairing_400]]"
---

# An assistant message persisted without its tool results wedges the session forever

**Status:** RESOLVED locally 2026-08-16. The exact shared-cluster pod interruption was not
repeated and is not claimed as a passed live gate.

## Corrected failure model

The live Resavio incident was initially blamed on durable `thread_messages`. That was
incorrect: tool results are separate `role='tool'` rows, and attach/restore already calls
`repair_tool_pairing`. The poison survived in the mutable in-process message list. The
immediate incident cause was also separately removed: a headless Officer had been
commissioned in supervised permission mode. LF-5 is the residual liveness invariant: no
interruption or provider translation fault may make a live process resend the same orphan
forever.

## Resulting architecture

- `src/persistent_graph.py::_execute_turn` repairs the actual mutable `messages` list
  before compaction and again inside `_provider_input()` immediately before every
  `astream` or `ainvoke`, including stream retries and fallback calls. The provider-bound
  copy is repaired/scrubbed too.
- `PersistentLoopCallbacks.after_assistant_tool_calls_persisted` is a deterministic test
  seam at the exact boundary after the assistant tool-call message is appended and
  incrementally persisted, but before permission announcement or tool execution.
- An interruption at that boundary persists an `event` message saying the calls were not
  executed and no side effects may be assumed. It does not manufacture `ToolMessage`
  results. The original assistant row remains truthful and append-only; the next provider
  boundary strips its unpaired calls only from the in-process view.
- Attach/restore repair in `src/api/persistent_app.py::_restore_session_messages` remains
  in place for pre-existing durable orphans. `repair_tool_pairing` now uses
  `AIMessage.model_copy` when stripping calls, preserving provider ids, reasoning, usage,
  and response metadata.
- Only HTTP 400s carrying narrow known strict-pairing phrases enter the liveness circuit.
  They bypass generic in-turn transient retries. A successful turn, a different pairing
  invariant, or a non-pairing error resets the consecutive streak.
- The second consecutive equivalent strict-pairing failure emits one actionable
  escalation through the production `on_error` callback and returns from the attached
  loop. `_loop_on_error` broadcasts `error` and `turn.error` and persists a `role='error'`
  row, so the halt is operator-visible and survives reload. A queued third turn performs
  no provider call. Reattach/resume is the explicit recovery; no history deletion or
  process restart is required for the repaired next-turn path.

No thread history is automatically deleted, no result is fabricated, pairing rules were
not weakened, and no interactive permission gate was added to the Officer.

## Acceptance evidence

Focused cases live in `tests/test_persistent_tool_pairing_liveness.py`:

- interruption after assistant persistence and before tool execution;
- next-turn repair in the same process with the tool never invoked;
- two equivalent errors with volatile call ids escalating exactly once after two provider
  calls, with a queued third input spending nothing;
- unrelated provider 400 behavior remaining on the ordinary error/retry path;
- ordinary paired assistant/tool traffic remaining unchanged.

Restore coverage remains in `tests/test_persistent_app.py::TestRestoreSessionToolPairing`,
and bidirectional/unchanged-pair coverage remains in `tests/test_context_methods.py`.

Local verification on 2026-08-16:

```text
test_persistent_tool_pairing_liveness.py + test_context_methods.py +
test_persistent_app.py + test_persistent_graph.py: 500 passed in 7.62s
```

## Bounded optional live-fault runbook

This is a confidence gate, not evidence claimed by this local closure. Run only in a
disposable local k3d namespace/build with `auto_pull=false`; never on a shared Officer.

1. Commission one disposable autonomous Officer and record its exact thread and pod UID.
2. Request a harmless multi-tool inspection. Watch the thread rows; when the assistant
   tool-call row commits and before any matching tool row appears, delete only that exact
   pod UID. If a tool row already exists, record a missed window and do not reinterpret it
   as the fault.
3. Allow the replacement to restore, then send one inspection turn. Require useful output,
   matched tool rows, and no strict-pairing 400. Do not delete or edit `thread_messages`.
4. In a disposable provider-stub build, return the same strict-pairing 400 on two turns.
   Require one durable error bubble after call two and zero provider requests for a queued
   third input.
5. Stop after three missed interruption windows or 15 minutes. Remove only the disposable
   post/project/pod artifacts.

