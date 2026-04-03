# Persistent Session Logging — Assessment

How regular jobs and persistent sessions log data today, what the gap is, and options for closing it.

## Regular Jobs: Three MongoDB Collections

The `LLMArchiver` (`src/core/archiver.py`) writes to MongoDB from ~30 call sites in `src/graph.py`:

**`llm_requests`** — Full LLM request/response archive.
Every LLM call (main, summarization, memory extraction, vision) stores: all input messages, full response, tool schemas, model name, temperature, latency_ms, token usage breakdown (input/output/reasoning), call_type, phase, phase_number, iteration.

**`agent_audit`** — Sequential execution log.
Step-by-step record with step_number: initialize, llm_call, llm_response, tool_call, tool_result, check, routing, phase_complete, error. Each step links to the LLM request that triggered it. Tool steps include truncated args (200 chars) and result preview (500 chars).

**`chat_history`** — Condensed conversation deltas.
One doc per turn: new inputs (human/tool messages added since last turn), LLM response with content preview and tool call list. Links to `llm_requests` via request_id. Only for `call_type='main'`.

Additionally, PostgreSQL `jobs` stores: status, total_tokens_used, total_requests, resolved_config, error_message. SQLite checkpoints store serialized graph state for resume.

## Persistent Sessions: PostgreSQL Only

No MongoDB. All persistence flows through two PostgreSQL tables:

**`threads`** — Session metadata.
Fields: status (created/active/idle/ended), total_turns, total_tokens (aggregate), last_activity, config_name, permission_mode, created_at, ended_at.

**`thread_messages`** — Full conversation.
One row per message: role (user/assistant/tool), content (full text), tool_calls JSONB (on assistant messages: [{name, args, id}]), turn_number, created_at.

Additionally: Vector DB stores memories (RecallStore) and knowledge notes (KnowledgeStore) with embeddings.

## What's Already Covered

The `thread_messages` table stores the complete conversation — every user input, every assistant response (with tool_calls), every tool result. The cockpit session history page renders this directly. For an interactive session where a human is present, the conversation _is_ the primary audit trail.

Specifically, `thread_messages` covers what MongoDB `chat_history` does for regular jobs: the full sequence of inputs and outputs. It actually stores _more_ — full content, not truncated previews.

## What's Missing

| Data | Regular Jobs (MongoDB) | Persistent Sessions | Practical Impact |
|------|----------------------|-------------------|-----------------|
| Per-turn token breakdown | `llm_requests.metrics.token_usage` (input/output/reasoning) | Only `threads.total_tokens` aggregate | Can't attribute cost to individual turns |
| Request latency | `llm_requests.latency_ms` | Not stored | Can't identify slow turns or provider issues |
| Model name per request | `llm_requests.model` | Only `threads.config_name` | Minor — model rarely changes mid-session |
| Auxiliary call logging | Summarization, memory extraction, vision calls in `llm_requests` | Not stored | Can't debug compaction or memory quality |
| Tool schemas snapshot | `llm_requests.request.tools` | Not stored | Minor — tool set is static per session |
| Step-level sequencing | `agent_audit.step_number` | `turn_number` on messages | Turn-level ordering exists, not step-level |

**Not applicable to persistent sessions:** Phase transitions, graph routing decisions, todo checks, strategic/tactical distinction — these are worker-graph concepts that don't exist in the interactive loop.

## Options

### Option A: Do Nothing

The conversation is fully persisted in PostgreSQL. The cockpit renders it. Token totals exist on the thread. For interactive sessions this may be sufficient — the user was there, they saw what happened.

**Keeps:** Full message replay, session lifecycle, token totals.
**Loses:** Per-turn cost attribution, latency metrics, auxiliary call visibility.
**Best when:** Sessions are short, cost tracking isn't critical, debugging happens via conversation replay.

### Option B: Add `metrics` JSONB to `thread_messages`

Extend assistant-role messages with per-turn metrics:

```sql
ALTER TABLE thread_messages ADD COLUMN metrics JSONB;
-- Example value on an assistant message:
-- {"input_tokens": 1234, "output_tokens": 567, "reasoning_tokens": 890,
--  "latency_ms": 2100, "model": "openai/gpt-oss-120b"}
```

**Implementation:** ~15 lines. After each LLM response in `persistent_graph.py`, extract `response_metadata` (already available on the AIMessage) and pass it through `save_thread_message()`. The orchestrator's `POST /api/agents/threads/{id}/messages` endpoint adds the column value.

**Keeps:** Everything from Option A + per-turn token breakdown, latency, model name.
**Loses:** Auxiliary call logging (summarization, memory extraction).
**Best when:** You want cost/performance visibility without adding MongoDB as a dependency.

### Option C: Full LLMArchiver Integration

Pass the existing `LLMArchiver` into `PersistentSession` and call `archive()` + `audit_tool_call()` from the persistent loop, using `thread_id` as the job_id equivalent.

**Keeps:** Full parity with regular jobs.
**Concerns:**
- **Redundancy** — Messages stored in both PostgreSQL and MongoDB in different shapes.
- **New dependency** — Persistent sessions currently work without MongoDB. Adding it creates a new optional failure mode.
- **Poor fit** — Half the audit step types (initialize, routing, phase_complete, check) don't apply to the interactive loop. The archiver would log a subset of what it logs for regular jobs.

**Best when:** You need MongoDB queries across both jobs and sessions (e.g., unified cost dashboard, cross-system token reporting).

### Option D: Metrics Column + Selective Auxiliary Logging

Combine Option B (metrics on thread_messages) with MongoDB logging for background-only calls:

- **PostgreSQL:** Per-turn metrics on main conversation messages.
- **MongoDB:** Only log `call_type` = summarization, memory_extraction, memory_assembly, knowledge_curation — the invisible calls that aren't in the conversation.

No message duplication (main conversation stays in PostgreSQL only, background calls go to MongoDB only).

**Best when:** You want full visibility including compaction/memory debugging, but don't want to duplicate the conversation.

## Recommendation

**Option B** closes the practical gap. The conversation is already in PostgreSQL — what's missing is the per-turn numbers (tokens, latency). A `metrics` JSONB column on `thread_messages` adds that with minimal work and no new dependencies.

If auxiliary call debugging becomes important later (e.g., memory extraction producing bad results, compaction losing important context), Option D layers cleanly on top.
