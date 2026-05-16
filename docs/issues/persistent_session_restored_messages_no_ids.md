# Persistent session — restored messages lack IDs, so compaction can never remove them

## Symptom (observed 2026-05-11)

Test session `7d845b7e-…` blew its context budget on turn 2 (a 1.5 M-token
PDF read — see `persistent_session_large_attachment_context_explosion.md`).
The session ended, the cockpit reconnected, and on resume the agent
immediately tried to compact and failed in a new way:

```
2026-05-11 10:18:10 - src.api.persistent_app - INFO  - Restored 12 messages for thread 7d845b7e-… (last turn: 3)
…
2026-05-11 10:18:25 - src.core.context     - INFO  - Context compaction triggered: 14 messages, 1535941 tokens
2026-05-11 10:18:25 - src.core.context     - INFO  - Starting single-pass summarization (113 tokens)
…
2026-05-11 10:20:26 - src.core.context     - INFO  - Generated summary (311 chars)
2026-05-11 10:20:26 - src.core.context     - WARNING - 13 messages without IDs cannot be removed
2026-05-11 10:20:26 - src.core.context     - INFO  - Compacted 14 messages to 12 (summarized 3 messages, removing 0, 13 without IDs)
…
2026-05-11 10:20:29 - src.llm.reasoning_chat - ERROR - Context overflow at HTTP layer: 1,523,091 tokens exceeds limit of 131,072
```

Read carefully: **summarized 3 messages, removing 0, 13 without IDs**.
The compactor wanted to delete 13 stale messages but every one of them
carried `id=None`, so the `RemoveMessage` markers were no-ops. Net
result: a summary message was *added*, no original messages were
removed, conversation grew by one. Next turn → same 1.5 M overflow as
before resume → identical retry loop → session permanently stuck.

## Root cause

`src/api/persistent_app.py:1488-1518` reconstructs LangChain messages
from DB rows when a session resumes:

```python
if role in ("human", "user"):
    restored.append(HumanMessage(content=content))
elif role in ("ai", "assistant"):
    …
    restored.append(AIMessage(content=content, tool_calls=lc_tool_calls))
elif role == "tool":
    …
    restored.append(ToolMessage(content=content, tool_call_id=tool_call_id))
```

None of these constructors set `id`. LangChain's `BaseMessage.id` is
`Optional[str]` defaulting to `None` — it is **not** auto-generated. So
every restored message arrives with `id=None`.

`src/core/context.py:1495-1509` builds the removal markers used to
shrink the LangGraph state during compaction:

```python
for msg in conversation:
    if hasattr(msg, "id") and msg.id:
        removal_markers.append(RemoveMessage(id=msg.id))
    else:
        messages_without_ids += 1
```

`RemoveMessage` works by ID — there's no positional or content-based
removal. A message with no ID cannot be deleted from the state. The
compactor logs a warning and proceeds, trusting the graph to eventually
flush the slate, but the graph never does.

## Impact

- **Resumed sessions can never recover from a context overrun.** The
  exact failure mode that made the user end the session (giant tool
  result poisoning the conversation) is the failure mode that becomes
  permanent after resume. Every subsequent turn fails the same way.
- **Even non-overrun resumes leak.** Restored messages accumulate
  forever — every compaction adds a summary but removes none of the
  pre-resume content. Long-running threads (multiple resume cycles) hit
  context limits much earlier than the same sessions running in one
  go.
- **The warning is silent in the cockpit.** The user sees
  "Disconnected — reconnecting…" then a working session that
  immediately stops responding. No explanation surfaces.

## Fix sketch

Three lines in `_restore_session_messages()`. Generate a UUID per
restored message:

```python
import uuid as _uuid

if role in ("human", "user"):
    restored.append(HumanMessage(content=content, id=str(_uuid.uuid4())))
elif role in ("ai", "assistant"):
    restored.append(AIMessage(
        content=content,
        tool_calls=lc_tool_calls,
        id=str(_uuid.uuid4()),
    ))
elif role == "tool":
    restored.append(ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        id=str(_uuid.uuid4()),
    ))
```

The IDs are LangGraph state keys, not anything user-facing or persisted
— freshly minted UUIDs at restore time are exactly the right thing.
RemoveMessage uses object identity via the ID, so the new UUIDs
correspond to the in-memory copies and removal becomes a no-op only
when the ID truly didn't exist (which then is a real bug worth
investigating).

A defensive belt-and-suspenders elsewhere — in `summarize_and_compact()`
itself — would be to assign IDs lazily when iterating:

```python
for msg in conversation:
    if not getattr(msg, "id", None):
        msg.id = str(uuid.uuid4())
    removal_markers.append(RemoveMessage(id=msg.id))
```

…but this mutates messages in place, which may confuse downstream
consumers; the restore-time fix is cleaner and more localised.

## Verification once fixed

- Run the same PDF-overrun test, end the session, reopen it.
- Send any small follow-up. Compaction should log
  `removing N` (non-zero) and the next inference call should succeed.
- A second-pass log line worth adding to the compaction code:
  `logger.info("Removed %d messages, %d without IDs", removed, skipped)` —
  promotes the existing WARNING from "13 without IDs" to a single info
  line that aggregates per compaction, so future regressions show up
  immediately.

## Related code

- `src/api/persistent_app.py:1488-1518` — `_restore_session_messages()`
  (where the IDs are not assigned)
- `src/core/context.py:1495-1521` — compaction's `RemoveMessage` loop
  (where the missing IDs become a no-op)
- `src/core/context.py:1496` — `messages_without_ids = 0` counter
- `src/core/context.py` line that emits "messages without IDs cannot be
  removed" warning (referenced by log timestamp 10:20:26)

## Related issues

- `persistent_session_runaway_generation_context_explosion.md` — the
  upstream cause of the bad state that triggered this. Even though
  that doc's primary fix (output-token cap in `loader.py`) shipped
  2026-05-11, restored sessions that were *already poisoned before
  the fix* remain unrecoverable until this one ships too.

## Decision

**Fixed 2026-05-11.** Three lines in `_restore_session_messages()`:
each restored `HumanMessage` / `AIMessage` / `ToolMessage` now gets a
fresh UUID assigned at restore time. Test coverage added in
`tests/test_persistent_app.py::TestRestoreSessionMessageIds`. Combined
with the oversized-message rule shipped under
`persistent_session_runaway_generation_context_explosion.md`,
poisoned-resume sessions can now self-heal on first compaction.
