---
tags:
  - issue
  - sessions
  - officers
  - llm
  - liveness
status: open
priority: P0
created: 2026-08-15
aliases:
  - LF-5
  - orphaned tool call 400 wedge
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[persistent_compaction_tool_pairing_400]]"
---

# An assistant message persisted without its tool results wedges the session forever

**Status:** OPEN — P0 liveness. Found live 2026-08-15, five minutes after
commissioning a fresh officer.

## Observed

Thread `6ce5bc4c` (officer, live Better Resavio) took its boot turn, issued
three `kb_list` calls, and persisted an `ai` row carrying
`tool_calls = [3 entries]` with `tool_results IS NULL` and **no following
`tool` rows**. The turn produced no further output.

Every subsequent turn then replayed that history to the Responses API and
died before doing any work:

```
openai.BadRequestError: 400 - No tool output found for function call
call_KDNsjebEHPZNclckLcsoVLex
```

Turns 2 and 3 each burned an LLM round-trip, wrote an `error` row, and
completed with `0 tool calls`. The session was permanently unable to act:
the poison is in durable history, so it survives restarts, and every wake —
timer, sitrep, fleet event, Legate directive — re-triggers it.

Known relative: `persistent_compaction_tool_pairing_400`. That one is a
compaction bug. **This is the same 400 with no compaction anywhere near
it** — a brand-new thread, six messages, first turn. So the failure is not
"compaction drops the pairing"; it is "an assistant message with
`tool_calls` can be committed without its results, and nothing repairs it".

## Why it is P0

- **Silent and total.** Nothing pages. The card shows an ACTIVE officer with
  a healthy heartbeat and a pending timer. He simply never does anything.
- **Self-perpetuating.** Each wake burns tokens producing another error row.
- **Unrecoverable by any normal operation.** Restarting the pod re-attaches
  and re-hydrates the same poisoned history.

## Manual repair (used live)

Delete the unmatched assistant row (and the accumulated error rows), then
force a re-attach — the agent holds the restored history in memory, so a DB
fix alone is not enough:

```sql
DELETE FROM thread_messages
 WHERE thread_id = '<tid>' AND role = 'ai'
   AND tool_calls IS NOT NULL AND jsonb_array_length(tool_calls) > 0
   AND tool_results IS NULL;
DELETE FROM thread_messages WHERE thread_id = '<tid>' AND role = 'error';
```

Nothing of value is lost: the calls never returned, so the message carries
no results and (in this instance) no content.

## Direction

- **Never persist an assistant message carrying `tool_calls` without, in the
  same transaction, either its results or a synthetic
  "tool call did not complete" result per call.** The pairing is a protocol
  invariant of the Responses API; a half-written turn must not be able to
  break it.
- **Repair on restore.** At attach, drop or complete any assistant message
  whose `tool_calls` have no matching results, rather than faithfully
  replaying a history the provider will reject.
- **Fail loudly.** A session that raises the same provider 400 on N
  consecutive turns should stop retrying and page, not spend forever.

## Acceptance

- Killing an agent mid-turn, between the LLM response and tool execution,
  leaves a session that can still take its next turn.
- A thread whose history already contains an orphaned call self-repairs on
  the next attach.
- Repeated identical provider 400s escalate instead of looping.
