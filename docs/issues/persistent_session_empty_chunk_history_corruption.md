---
tags:
  - persistent-sessions
  - bug
  - agent
  - streaming
  - message-state
related:
  - "[[langchain_responses_api_streaming]]"
  - "[[persistent_session_permission_check_race]]"
  - "[[persistent_session_restored_messages_no_ids]]"
  - "[[persistent_session_runaway_generation_context_explosion]]"
  - "[[persistent_chat_silent_disconnect]]"
  - "[[stuck_thread_workspace_pods]]"
---

# Persistent thread — empty streamed chunk retained in history → `TypeError: Got unknown type` ("malformed response")

**Reported**: 2026-05-24
**Status (2026-05-25)**: **I1/I2 fixed & deployed** — `src/llm/response_guards.py` (commit `fdcb5f97`); both `gemma-moe` and `gpt-5.5` sessions now stable on dev. The durable-source-of-truth refactor that structurally retires this whole class (I3–I6) is underway: **Plan 1 (component-store foundation) complete & deployed** (additive/dormant); **Plan 2 (authority inversion) pending** — see `docs/features/persistent_session_source_of_truth.md` → *Implementation status*. I9 root cause now identified (below).
**Severity**: High. Corrupts a live session permanently; **in-session retry can never recover** (the corruption lives in the agent's in-memory history, so "send your message again" re-fails every time).

This document enumerates *every* issue surfaced while investigating one incident. The headline bug (I1/I2) is **new**; several others (I3–I9) are pre-existing and already have their own docs — they are listed here because they **converged** in this incident and the upcoming fix plan should treat them together. Each issue is tagged **NEW** / **KNOWN** / **PARTIAL** accordingly.

## Incident

| | |
|---|---|
| Thread | `11bf220c-27e7-4f9c-a7f8-661bc0870102` ("Informationen über Aerogel aus Stadur Sued") |
| Environment | dev — ns `superhuman-remote-worker` |
| Agent pod | `srw-agent-j-25138eea` (still running; holds the poisoned in-memory state) |
| Workspace pod | `ws-thread-11bf220c-27e` (still running; built artifacts intact) |
| Model | `gpt-5.5` via `srw-codex-proxy:8317/v1` (OpenAI Responses API path) |
| Mode | supervised |
| User-visible symptom | Cockpit banner: **"The assistant returned a malformed response. Try sending your message again."** — reproduced on every retry. |

### Timeline (UTC, 2026-05-24)

| Time | Event |
|------|-------|
| 16:43 | Turn 1 ("what is Aerogel, cite the repo") completes cleanly. |
| 16:59 | Turn 2 ("build a RAG chatbot demo") starts — long tool-heavy streamed turn. |
| 17:14:20 | Agent flushes turn-2 messages to Postgres (burst of `POST …/messages 200`). |
| **17:17:01** | `WebSocket disconnected: thread=11bf220c… (loop continues)` — **client WS drop mid-turn**, then cockpit reconnects (`GET …/stream`, `GET …/connection`, `PUT …/status`). |
| 17:19:09 | User sends **"Continue please"** (`POST …/input 200`, message persisted). |
| **17:19:22** | Agent raises the traceback below; turn fails; loop survives and returns to idle. |

### Confirmed traceback (`srw-agent-j-25138eea`, agent container)

```
Traceback (most recent call last):
    result = await _execute_turn(
  File "/app/src/persistent_graph.py", line 532, in _execute_turn
    async for chunk in llm_with_tools.astream(prepared):
  … (langchain ChatOpenAI._create_message_dicts → list comp) …
    else _convert_message_to_dict(m)
  File ".../langchain_openai/chat_models/base.py", line 424, in _convert_message_to_dict
TypeError: Got unknown type content='' additional_kwargs={} response_metadata={} id='lc_run--019e5adc-0945-7b73-a0eb-810480570d0a'
```

The offending object: an **empty message carrying only a LangChain run-id** (`content=''`, `id='lc_run--…'`). It sits in the in-memory history; on the next turn the whole history is serialized for the request (`astream(prepared)`) and `_convert_message_to_dict` cannot classify its type.

---

## Issues

### I1 — Empty/partial streamed chunk is appended to history without normalization  — **NEW**

**Mechanism.** During a streamed turn the agent collects chunks and sums them:

- `persistent_graph.py:585-587` — `response = chunks[0]; for chunk in chunks[1:]: response = response + chunk`. The result stays a **raw streaming object** (an `AIMessageChunk`/`BaseMessageChunk`), never converted to a concrete `AIMessage`.
- `persistent_graph.py:685` (graceful mid-stream interrupt) and `persistent_graph.py:837` (normal completion) both do `messages.append(response)` with **no guard** for "empty content + no tool calls."

When the upstream Responses API stream yields nothing usable (see I-root below), `response` is an empty chunk. It is appended to the in-memory history and then re-serialized on the **next** turn (`prepared = list(messages)` at `:443` → `astream(prepared)` at `:532`), where `_convert_message_to_dict` rejects its type → `TypeError: Got unknown type`.

**Why it never self-heals:** the same object is reconstructed cleanly when loaded from Postgres (see I5) — but the in-memory list is never reloaded mid-session (I4), so only the in-memory copy is poisoned.

**Root of the empty chunk (KNOWN):** the Responses API streaming bugs against the Codex proxy — empty/dropped content and tool-call args — are documented in [[langchain_responses_api_streaming]] (Bugs 1 & 3, and the non-streaming empty-content mode). This incident is the **downstream consequence** that doc did not cover: the empty result is *retained in history* and breaks the *following* turn.

**Recurrence:** [[persistent_session_permission_check_race]] (Open Questions, final bullet) recorded the identical `Got unknown type content='' … id='lc_run--…'` on 2026-05-12 and said it "deserves its own issue if it recurs." This is that issue.

**Candidate fix (sketch — full plan TBD):** at the append sites, drop a `response` with empty content and no tool calls; and normalize `AIMessageChunk` → concrete `AIMessage` before it ever enters `messages`. Defense-in-depth: filter empty chunks out of `prepared` at `:443`.

### I2 — `persistent_graph.py` lacks the empty/no-tool-call guards that `graph.py` already has  — **NEW (gap)**

The worker graph (`graph.py`) has circuit breakers for exactly this failure family: `_check_empty_response_streak` (empty content + no tool calls) and `_check_no_tool_call_streak` (content but no tool calls, repeated). The **persistent** loop (`persistent_graph.py`) has **no equivalent** on its chunk-append path — the protections were never ported. That asymmetry is why the persistent path silently retains the bad message.

**Candidate fix:** port/share the guard logic so both graphs reject degenerate responses uniformly.

### I3 — In-memory working copy diverges from the Postgres projection (non-atomic persist)  — **PARTIAL** (related: [[persistent_session_restored_messages_no_ids]])

The agent maintains two representations: the live in-memory `messages` list (`persistent_graph.py:179`, authoritative working copy) and a Postgres projection written separately via `POST …/messages`. The interrupt path mutates memory (`:685`) **without** a matching persist, so the two drift. Confirmed here: no `POST …/messages` between 17:14:21 and the 17:19:09 user message, yet memory had the extra empty chunk.

**Candidate fix:** make "append to history" and "persist" a single atomic operation so the two can't diverge.

### I4 — In-memory history is never re-read mid-session; corruption is sticky  — **NEW (architectural)**

`run_persistent_loop` seeds `messages` **once** on attach and mutates it in place for the agent's lifetime; it is never re-materialized from the DB between turns. Consequence: any in-memory corruption persists until the agent re-attaches, and **every in-session retry re-fails**. This is the same "poisoned state → session unrecoverable" shape as [[persistent_session_runaway_generation_context_explosion]], via a different cause.

**Candidate fix:** treat the durable store as source-of-truth and re-materialize (or at least validate) the working set per turn; OR a recovery path that drops/repairs the offending message without a full re-attach.

### I5 — Postgres message projection is lossy → cannot be a faithful source of truth  — **NEW (architectural)**

`get_thread_messages_history` (`postgres_db.py:322`) selects only `role, content, tool_calls, turn_number, metrics` — **not** `additional_kwargs`, `response_metadata`, or reasoning blocks. Reconstruction (`persistent_app.py:1488-1518`, see [[persistent_session_restored_messages_no_ids]]) rebuilds concrete `AIMessage`/`HumanMessage`/`ToolMessage`. Two consequences:

1. The round-trip **normalizes** types — which is *why* a re-attached agent recovers from I1 (the chunk comes back as a clean empty `AIMessage`). Useful accident.
2. For `gpt-5.5` (reasoning via Responses API), dropping reasoning/metadata means resume re-materializes **degraded** context. So the projection can't currently *be* the authoritative replay store without schema widening (lossless `messages_to_dict`/`dumpd`).

**Candidate fix:** if we want DB-as-source-of-truth (the natural fix for I3/I4), widen the persisted schema to a lossless serialization first.

### I6 — Persistent loop has no durable per-step checkpointer  — **NEW (architectural)**

Unlike the job-side `graph.py` (a real LangGraph), the persistent loop is hand-rolled (`while True` + mutable list). There is no per-step durable checkpoint, so a mid-turn agent crash loses in-flight state and recovery is manual. This is the underlying reason I3/I4/I5 are even possible.

**Candidate fix:** evaluate moving the persistent loop onto a LangGraph `AsyncPostgresSaver` checkpointer (with the same normalization guard from I1 — a checkpointer would otherwise persist the poison durably and make recovery *harder*).

### I7 — Cockpit "Try sending your message again" is actively wrong for this failure mode  — **NEW (UX)**

`sanitizeError()` (`cockpit/src/app/core/services/persistent-chat.service.ts:1483-1485`) maps `/Got unknown type/` → "The assistant returned a malformed response. Try sending your message again." But retrying re-hits the same in-memory poison (I4), so the advice guarantees repeated failure. (The sanitiser itself shipped as Fix 7 in [[persistent_session_permission_check_race]] and is fine as a cosmetic layer; the *guidance* is the problem.)

**Candidate fix:** detect this class (or any non-recoverable turn error) and offer a real recovery action (reload-from-history / re-attach) instead of "try again."

### I8 — Recurring `snapshot_service` SSH failures for a stuck workspace  — **PARTIAL / peripheral** (related: [[stuck_thread_workspace_pods]])

The dev orchestrator logs an error every ~60s: `services.snapshot_service: SSH tar failed for job 692f00d5-…: ssh: connect to host 10.42.3.101 port 22: Connection refused`. This is a **different** workspace (`workspace-692f00d5`, not our thread). Note the failure shifted from the old "FileNotFoundError / ssh missing" (fixed by [[persistent_session_permission_check_race]] Fix 6) to "Connection refused" — i.e. ssh now exists but the target is unreachable, and the snapshot is retried forever. Likely a stuck/dead workspace pod whose snapshot loop never gives up. Confidence it's related to I1: low; flagged for triage.

**Candidate fix:** bound/back off snapshot retries; reconcile/garbage-collect dead workspaces.

### I9 — Turn-2 agent behavior: interactive-pip loop + redundant giant `shell_read` context bloat  — **PARTIAL / peripheral** (related: [[persistent_session_runaway_generation_context_explosion]])

In turn 2 the agent ran `pip install` (langchain/chromadb/torch/nvidia-cuda wheels) that repeatedly returned `Error: Command requires interactive input`, then issued ~30 `shell_read` calls each pulling ~1,300-line download dumps into context. This bloated the turn massively (helping make turn 2 the long, interruption-prone stream that triggered I1) and is wasteful regardless. Distinct from I1 but worth fixing.

**Root cause identified (2026-05-25):** the workspace shell's stall-detector reports "requires interactive input" when output is unchanged for ~5s (a last-20-lines heuristic). Heavy `pip install` downloads trip this false positive; the command keeps running and head-of-line-blocks the default tab, so the agent loops on `shell_read`. The fix belongs in the shell tool's stall heuristic (detect active download/progress, or raise the threshold), not in the agent.

**Candidate fix:** non-interactive install defaults; cap/dedupe `shell_read` of growing output; stream-tail rather than re-dump.

---

## How this converges with known issues

The incident is a chain, not a single bug:

```
Responses API empty/partial stream      ([[langchain_responses_api_streaming]])
   → empty chunk produced
client WS drop mid-stream (17:17:01)     ([[persistent_chat_silent_disconnect]])
   → graceful interrupt path taken
no empty-guard / no normalization (I1,I2)  ← NEW, the load-bearing gap
   → empty chunk appended to in-memory history
in-memory copy never reloaded (I4) + non-atomic persist (I3)
   → poison is sticky, not in Postgres
next turn serializes history → TypeError: Got unknown type
   → cockpit shows "malformed response" + wrong retry advice (I7)
```

The **single load-bearing fix** is I1+I2 (normalize/guard at the append boundary); it stops the crash regardless of the others. I3–I6 are the architectural cleanup that would make sessions self-healing and crash-recoverable; I7 is UX; I8–I9 are peripheral hygiene.

## Open questions

- **Exact type of the retained object.** Confirmed it fails every `isinstance` branch in `_convert_message_to_dict`, but the precise reason (raw `AIMessageChunk` subtype vs. a non-`AIMessage` chunk vs. `langchain_core` version skew) is unconfirmed. Cheapest probe: log `type(m).__mro__` at the append site, or repro locally by interrupting a Responses-API stream.
- **Which append produced it for this incident** — graceful interrupt (`:685`) vs. an empty normal completion (`:837`). Doesn't change the fix; does affect the repro.
- **Recovery safety.** Re-attaching the agent rebuilds from clean Postgres history, but the user-initiated *end* path may still aggressively tear down the workspace (see [[persistent_session_permission_check_race]] Fix 5 caveats); confirm the dev suspend/restore path is safe before recommending end+resume as the recovery.

## Recovery for the live session `11bf220c` (operational, not a code fix)

Poison is in `srw-agent-j-25138eea`'s RAM only. Force the agent to re-attach so it rebuilds `messages` from the (clean) Postgres history; **avoid the user-initiated End/DELETE path** until the workspace suspend/restore is confirmed safe (the built artifacts live in `ws-thread-11bf220c-27e`).
