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

## Correction — the orphan lives in MEMORY, not in durable history

The first version of this note claimed the poison was durable and survived
restarts. That is wrong, and the correction narrows the bug usefully.

Two facts found after filing:

1. **`tool_results` being NULL on an `ai` row is normal, not a symptom.**
   On the healthy officer thread `d67ee261`: 613 `ai` rows carry tool calls
   and **all 613** have `tool_results` NULL. Results are persisted as
   separate `role='tool'` rows (890 of them there). The column is vestigial;
   checking it diagnoses nothing.
2. **`repair_tool_pairing` (`src/core/context.py`) already exists**, is
   bidirectional, and its docstring says it is shared by the live turn loop
   *and* the resume path — precisely to strip orphans before a strict-pairing
   API call. A re-attach therefore repairs a poisoned history on its own.

So the live sequence was: an interrupted turn left an orphaned call in the
**in-process LangGraph state**, and turns 2 and 3 replayed *that* — not the
database. The restart is what cured it; the row deletion was belt-and-braces.

**The real defect is narrower and still worth fixing:** the live loop went on
issuing the same rejected request turn after turn instead of repairing its
own state, so a session can wedge in-process indefinitely with no operator
signal. Whether `repair_tool_pairing` is not reached on that path, or runs
before the point where the orphan appears, is the open question.

## Manual repair (used live)

**Force a re-attach** — delete the agent pod. For an officer the watchdog
respawns onto a dedicated `persistent-<tid8>` pod within ~30 s. That alone
should suffice, since restore repairs the pairing.

If you also want the history clean (optional, and it discards the
interrupted turn):

```sql
DELETE FROM thread_messages
 WHERE thread_id = '<tid>' AND role = 'ai'
   AND tool_calls IS NOT NULL AND jsonb_array_length(tool_calls) > 0
   AND NOT EXISTS (SELECT 1 FROM thread_messages t
                    WHERE t.thread_id = '<tid>' AND t.role = 'tool');
DELETE FROM thread_messages WHERE thread_id = '<tid>' AND role = 'error';
```

## Adjacent observation (unexplained)

Thread `6ce5bc4c` has written **zero `role='tool'` rows** since commission,
including for a turn that executed seven tool calls successfully — while
`d67ee261` was writing them normally on the same deploy in the same hour
(21 rows in 19:00Z alone). Whatever the cause, a thread that never persists
tool rows loses each turn's tool context at the next attach, since
`repair_tool_pairing` drops the now-orphaned calls. Not proven to be the
same defect; recorded so it is not lost.

## Direction

- **Repair the live state, not just the restored state.** `repair_tool_pairing`
  must run against the in-process message list immediately before every
  provider call, so an orphan created by an interrupted turn cannot survive
  into the next one. This is the actual fix.
- **Fail loudly.** A session that raises the same provider 400 on N
  consecutive turns should stop retrying and page, not spend forever writing
  `error` rows. Two identical 400s is enough signal.
- **Treat an interrupted turn as an event.** A turn whose tool calls never
  executed should log/notify rather than complete silently with
  `0 tool calls`.

## Acceptance

- Killing an agent mid-turn, between the LLM response and tool execution,
  leaves a session that can still take its next turn.
- A thread whose history already contains an orphaned call self-repairs on
  the next attach.
- Repeated identical provider 400s escalate instead of looping.
