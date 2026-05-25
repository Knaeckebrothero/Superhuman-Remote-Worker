# Persistent Session Component Store — Implementation Plan (Plan 1 of 4: Foundation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the durable, provider-agnostic message-component store (4 components + verbatim provider-raw) and a provider adapter that normalizes responses in and rebuilds provider requests out — **without yet changing runtime authority** (that's Plan 2).

**Architecture:** Extend `thread_messages` in place (migration `0019`, nullable columns — never a new table; see spec Q3). A new pure module `src/llm/session_components.py` converts a LangChain response → `MessageComponents` (reasoning/text/tool_calls/tool_results + provider + verbatim `provider_raw` + `additional_kwargs`/`response_metadata`) and back. The write path persists the new columns; the agent-side reader is fixed to be lossless. Rows are append-only/immutable.

**Tech Stack:** Python 3.11, langchain / langchain-openai, asyncpg + the repo's SQL migration runner (`orchestrator/database/migrate.py`), pytest. Reuses existing `src/llm/reasoning_chat.py` (`_dump_codex_raw_response`, `_extract_responses_api_reasoning`).

**Spec:** `docs/features/persistent_session_source_of_truth.md` (D1, D2, D6, Q3).

**Out of scope for Plan 1:** wiring the adapter into `_execute_turn`, authority inversion, compaction, streaming, frontend. Everything here is additive and dormant until Plan 2.

---

## Status: ✅ COMPLETE — implemented, committed, deployed to dev (2026-05-25)

All 8 tasks shipped on `develop` and are live on the dev cluster; migrations `0019`/`0020` applied + verified; both `gemma-moe` and `gpt-5.5` sessions confirmed working. The component store + adapter are **additive and dormant** — they change no agent behavior yet (that is Plan 2: authority inversion).

| Task | Commit |
|------|--------|
| 1 — real Chat-Completions fixture + shape guard | `09285426` |
| 2 — migration `0019` (component columns) | `00ff7e02` |
| 3 — migration `0020` (windowed-hydration index) | `9f9df81c` |
| 4 — `normalize_response` | `b5250474` |
| 5 — `components_to_provider_messages` (normalized CC reconstruction) | `27ffb0d5` |
| 6 — lossless agent-side reader | `3b1782f2` |
| 7 — write-path persistence (+ pre-existing 204 route fix) | `30c3143c` |
| — CI test-assertion fix (additive payload fields) | `e78db275` |

**Note:** the live *behavior* fix that makes both models stable is the I1/I2 `response_guards` (commit `fdcb5f97`), deployed alongside this foundation. See `docs/issues/persistent_session_empty_chunk_history_corruption.md`. **Key correction during execution (Task 1):** the live model speaks **Chat Completions, not the Responses API** — Tasks 4/5 and spec D2 were revised accordingly.

---

## File structure

- **Create** `tests/fixtures/provider_responses/openai_responses_reasoning_toolcall.json` — real captured gpt-5.5 Responses payload (characterization fixture).
- **Create** `orchestrator/database/migrations/app/0019_thread_messages_components.sql` — add 6 nullable columns.
- **Create** `orchestrator/database/migrations/app/0020_thread_messages_window_index.notx.sql` — `CREATE INDEX CONCURRENTLY`.
- **Create** `src/llm/session_components.py` — `MessageComponents`, `normalize_response`, `components_to_provider_messages`.
- **Create** `tests/test_session_components.py` — adapter round-trip tests.
- **Modify** `src/database/postgres_db.py:322-358` — fix lossy agent-side reader.
- **Modify** `orchestrator/database/postgres.py:2929-2974` — `save_thread_message` persists new columns.
- **Modify** `orchestrator/main.py` — `AgentThreadMessageRequest` (~:10513) + the POST handler (~:11200) carry new fields.
- **Modify** `src/api/orchestrator_client.py:246` — `save_thread_message` forwards new fields.
- **Test** `tests/test_session_components.py`, `tests/test_postgres_db_reader.py`.

---

## Task 1: Capture a real provider-response fixture (characterization)

> ✅ **DONE (2026-05-24) — and it changed the design.** Captured directly from `srw-codex-proxy:8317/v1/chat/completions` (no DEBUG flag / `kubectl cp` needed). The live `gpt-5.5` path is **OpenAI Chat Completions, not the Responses API**: reasoning is a flat `reasoning_content` string (was `null` here), tool calls use the standard CC shape, there is **no** `output[]`/`rs_`/`encrypted_content`/pairing. Fixture: `tests/fixtures/provider_responses/openai_chat_completions_toolcall.json`; guard test `test_codex_proxy_returns_chat_completions_shape` in `tests/test_session_components.py` (green, committed). **The Responses-API-shaped steps below are superseded** — Tasks 4 & 5 are revised to the CC shape; spec D2/D6 updated to mark Responses/Anthropic as forward-compat.

**Why first:** the adapter's correctness depends on the *actual* shape the `srw-codex-proxy` returns for `gpt-5.5` (a "still to verify" item in the spec). Capture it once, commit it, and build the adapter against the real bytes.

**Files:**
- Create: `tests/fixtures/provider_responses/openai_responses_reasoning_toolcall.json`

- [x] **Step 1: Capture a raw response from the dev cluster**

`src/llm/reasoning_chat.py` already dumps raw codex responses when `DEBUG_CODEX_RAW_RESPONSE=1` (see `_dump_codex_raw_response`, line ~58). Run one persistent-session turn that triggers a tool call against `gpt-5.5`, with the env var set on the agent. Retrieve the dumped file from the agent pod:

```bash
# On the dev agent pod (superhuman-remote-worker ns), the dump lands in a temp path
# logged at INFO by _dump_codex_raw_response. Copy it out:
kubectl -n superhuman-remote-worker cp <agent-pod>:/tmp/<dumped-file>.json \
  tests/fixtures/provider_responses/openai_responses_reasoning_toolcall.json
```

- [x] **Step 2: Sanity-assert the fixture has the load-bearing fields**

Add a guard test so the fixture can't silently rot:

```python
# tests/test_session_components.py  (first test in the file)
import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_responses"


def test_openai_fixture_has_reasoning_and_toolcall_items():
    raw = json.loads(
        (FIXTURE_DIR / "openai_responses_reasoning_toolcall.json").read_text()
    )
    output = raw["output"]
    kinds = [item["type"] for item in output]
    # The replay constraint we must preserve: a reasoning item precedes a function_call.
    assert "reasoning" in kinds
    assert "function_call" in kinds
    assert kinds.index("reasoning") < kinds.index("function_call")
    rs = next(i for i in output if i["type"] == "reasoning")
    assert rs["id"].startswith("rs_")  # reasoning item id is load-bearing for replay
```

- [x] **Step 3: Run it to verify it passes against the real capture**

Run: `python -m pytest tests/test_session_components.py::test_openai_fixture_has_reasoning_and_toolcall_items -v`
Expected: PASS. If it FAILS, the proxy is reshaping/stripping items — STOP and record findings in the spec's "Still to verify" before continuing (the adapter design depends on this).

- [x] **Step 4: Commit**

```bash
git add tests/fixtures/provider_responses/openai_responses_reasoning_toolcall.json tests/test_session_components.py
git commit -m "test(sessions): capture real gpt-5.5 Responses payload as adapter fixture"
```

---

## Task 2: Migration 0019 — add component columns

**Files:**
- Create: `orchestrator/database/migrations/app/0019_thread_messages_components.sql`

- [x] **Step 1: Write the migration** (mirrors the `0011` template exactly)

```sql
-- migration:     0019_thread_messages_components.sql
-- description:   Add provider-agnostic component columns to thread_messages so a
--                turn can be stored as normalized components (reasoning, text,
--                tool_calls, tool_results) PLUS the verbatim provider-raw payload
--                needed for faithful same-provider replay (OpenAI Responses
--                reasoning items, Anthropic thinking+signature). Supersedes the
--                lossy `thinking` column (kept for legacy reads). See
--                docs/features/persistent_session_source_of_truth.md (D2, Q3).
--
--                All columns nullable -> metadata-only, no row rewrite, no
--                backfill (dev-cutover; old rows return NULL and degrade
--                gracefully, same as 0011).
-- depends-on:    0011_thread_messages_tool_link_and_thinking.sql
-- expected:      < 1s on dev DB. ADD COLUMN with NULL is metadata-only.
-- locks:         AccessExclusiveLock briefly on thread_messages for ADD COLUMN.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE thread_messages
    ADD COLUMN IF NOT EXISTS reasoning         JSONB,
    ADD COLUMN IF NOT EXISTS tool_results      JSONB,
    ADD COLUMN IF NOT EXISTS provider          TEXT,
    ADD COLUMN IF NOT EXISTS provider_raw      JSONB,
    ADD COLUMN IF NOT EXISTS additional_kwargs JSONB,
    ADD COLUMN IF NOT EXISTS response_metadata JSONB;

COMMENT ON COLUMN thread_messages.reasoning IS
    'Normalized reasoning items (role=ai). Written going forward; supersedes the '
    'legacy `thinking` TEXT column (kept for historical reads).';
COMMENT ON COLUMN thread_messages.provider_raw IS
    'Verbatim, ORDER-PRESERVING provider response items for faithful same-provider '
    'replay (OpenAI Responses reasoning/function_call items; Anthropic thinking '
    'blocks incl. signature). Captured from the COMPLETED (non-streamed) response.';
COMMENT ON COLUMN thread_messages.provider IS
    'Provider tag for the row, e.g. openai-responses | anthropic. Selects the '
    'replay path in src/llm/session_components.py.';

COMMIT;
```

- [x] **Step 2: Dry-run the migration**

Run: `python -m orchestrator.database.migrate --dry-run`
Expected: `0019_thread_messages_components.sql` listed as pending, squawk/dry-run clean (nullable ADD COLUMN is metadata-only — no lock-duration findings).

- [x] **Step 3: Apply locally and verify columns exist**

Run: `python -m orchestrator.database.migrate` then
`psql "$APP_DATABASE_URL" -c "\d thread_messages" | grep -E 'reasoning|tool_results|provider|provider_raw|additional_kwargs|response_metadata'`
Expected: all six columns present, type `jsonb`/`text`.

- [x] **Step 4: Commit**

```bash
git add orchestrator/database/migrations/app/0019_thread_messages_components.sql
git commit -m "feat(db): 0019 add component columns to thread_messages"
```

---

## Task 3: Migration 0020 — windowed-hydration index (CONCURRENTLY)

**Files:**
- Create: `orchestrator/database/migrations/app/0020_thread_messages_window_index.notx.sql`

The `.notx` suffix runs the file outside a transaction (required for `CREATE INDEX CONCURRENTLY`). Current sole index is `idx_thread_messages_thread (thread_id)`; windowed hydration (spec D4) orders by `(thread_id, turn_number, created_at)`.

- [x] **Step 1: Write the migration**

```sql
-- migration:     0020_thread_messages_window_index.notx.sql
-- description:   Composite index for windowed hydration (spec D4): last-N and
--                scroll-back pagination order by (thread_id, turn_number,
--                created_at). CREATE INDEX CONCURRENTLY -> runs outside a txn
--                (.notx), no AccessExclusiveLock, safe on a populated table.
-- depends-on:    0019_thread_messages_components.sql
-- expected:      seconds on dev (small table).
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: no

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_thread_messages_thread_turn_created
    ON thread_messages (thread_id, turn_number, created_at);
```

- [x] **Step 2: Dry-run**

Run: `python -m orchestrator.database.migrate --dry-run`
Expected: `0020_...notx.sql` listed as a non-transactional migration; no squawk lock-duration finding (CONCURRENTLY is the squawk-approved form).

- [x] **Step 3: Apply + verify**

Run: `python -m orchestrator.database.migrate` then
`psql "$APP_DATABASE_URL" -c "\d thread_messages" | grep idx_thread_messages_thread_turn_created`
Expected: index present.

- [x] **Step 4: Commit**

```bash
git add orchestrator/database/migrations/app/0020_thread_messages_window_index.notx.sql
git commit -m "feat(db): 0020 add (thread_id,turn_number,created_at) index for windowed hydration"
```

---

## Task 4: `MessageComponents` + `normalize_response` (response → components + raw)

**Files:**
- Create: `src/llm/session_components.py`
- Test: `tests/test_session_components.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_session_components.py  (append)
from langchain_core.messages import AIMessage, ToolMessage
from src.llm.session_components import MessageComponents, normalize_response


def test_normalize_ai_message_extracts_four_components_and_raw():
    # Live shape (Task 1): the codex proxy returns Chat Completions; reasoning is
    # a flat reasoning_content string that langchain lands in additional_kwargs.
    raw_response = {
        "object": "chat.completion",
        "choices": [{"message": {
            "role": "assistant", "content": "Reading the file.",
            "reasoning_content": "Let me read it.",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "read_file",
                                         "arguments": '{"path": "a.txt"}'}}]}}],
    }
    msg = AIMessage(
        content="Reading the file.",
        tool_calls=[{"name": "read_file", "args": {"path": "a.txt"},
                     "id": "call_1", "type": "tool_call"}],
        additional_kwargs={"reasoning_content": "Let me read it."},
        response_metadata={"model_name": "gpt-5.5"},
    )
    comp = normalize_response(msg, provider="openai-chat", raw_output=raw_response)

    assert isinstance(comp, MessageComponents)
    assert comp.text == "Reading the file."
    assert comp.reasoning == "Let me read it."
    assert comp.tool_calls == [{"name": "read_file", "args": {"path": "a.txt"},
                                "id": "call_1", "type": "tool_call"}]
    assert comp.provider == "openai-chat"
    assert comp.provider_raw == raw_response        # stored verbatim (audit/forward-compat)
    assert comp.response_metadata == {"model_name": "gpt-5.5"}


def test_normalize_tool_message_becomes_tool_result_component():
    tm = ToolMessage(content="file contents", tool_call_id="call_1")
    comp = normalize_response(tm, provider="openai-chat", raw_output=None)
    assert comp.tool_results == [{"tool_call_id": "call_1", "content": "file contents"}]
    assert comp.text is None
    assert comp.tool_calls == []
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_session_components.py -k normalize -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.session_components'`.

- [x] **Step 3: Implement `session_components.py` (normalize half)**

```python
# src/llm/session_components.py
"""Provider-agnostic message components for the durable session transcript.

Converts a LangChain response into four normalized components (reasoning, text,
tool calls, tool results) PLUS the verbatim provider-raw payload, and back into a
provider request. See docs/features/persistent_session_source_of_truth.md (D2/D6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


@dataclass
class MessageComponents:
    role: str
    text: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: List[dict] = field(default_factory=list)
    tool_results: List[dict] = field(default_factory=list)
    provider: Optional[str] = None
    provider_raw: Optional[Any] = None
    additional_kwargs: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)


def _content_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "".join(parts)
        return joined or None
    return None


def normalize_response(
    message: BaseMessage,
    provider: str,
    raw_output: Optional[Any] = None,
) -> MessageComponents:
    """Normalize a LangChain message into components + verbatim raw.

    ``raw_output`` is the provider's verbatim COMPLETED response — the
    ``chat.completion`` object for the live Chat Completions path (the Responses
    ``output[]`` array if/when that API is adopted). Captured from the completed
    response, never reassembled from stream chunks. Stored for audit and
    forward-compat replay; the live CC path replays from normalized components.
    """
    if isinstance(message, ToolMessage):
        return MessageComponents(
            role="tool",
            tool_results=[{
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }],
            provider=provider,
        )

    if isinstance(message, AIMessage):
        ak = dict(message.additional_kwargs or {})
        return MessageComponents(
            role="ai",
            text=_content_text(message.content),
            reasoning=ak.get("reasoning_content"),
            tool_calls=list(message.tool_calls or []),
            provider=provider,
            provider_raw=raw_output,
            additional_kwargs=ak,
            response_metadata=dict(message.response_metadata or {}),
        )

    # human / system
    return MessageComponents(
        role=getattr(message, "type", "human"),
        text=_content_text(message.content),
        provider=provider,
    )
```

- [x] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_session_components.py -k normalize -v`
Expected: PASS (both normalize tests).

- [x] **Step 5: Commit**

```bash
git add src/llm/session_components.py tests/test_session_components.py
git commit -m "feat(sessions): normalize_response — response to 4 components + verbatim raw"
```

---

## Task 5: `components_to_provider_messages` (components/raw → provider request)

Replay for the live path = **normalized Chat Completions reconstruction** (assistant `content` + `tool_calls`; tool results as `role=tool`). `provider_raw` is audit/forward-compat and is **not** replayed here. Verbatim-raw replay for the real Responses API / Anthropic is forward-compat — `NotImplementedError` for now.

**Files:**
- Modify: `src/llm/session_components.py`
- Test: `tests/test_session_components.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_session_components.py  (append)
import pytest
from src.llm.session_components import components_to_provider_messages


def test_rebuild_chat_completions_request_from_components():
    comps = [
        MessageComponents(role="human", text="read a.txt", provider="openai-chat"),
        MessageComponents(role="ai", text="ok", provider="openai-chat",
            tool_calls=[{"name": "read_file", "args": {"path": "a.txt"},
                         "id": "call_1", "type": "tool_call"}]),
        MessageComponents(role="tool", provider="openai-chat",
            tool_results=[{"tool_call_id": "call_1", "content": "file body"}]),
    ]
    out = components_to_provider_messages(comps, target_provider="openai-chat")
    assert out == [
        {"role": "user", "content": "read a.txt"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]


def test_assistant_without_tool_calls_omits_key():
    comps = [MessageComponents(role="ai", text="hello", provider="openai-chat")]
    assert components_to_provider_messages(comps, target_provider="openai-chat") == \
        [{"role": "assistant", "content": "hello"}]


def test_non_cc_provider_replay_is_forward_compat():
    comps = [MessageComponents(role="ai", text="x", provider="anthropic")]
    with pytest.raises(NotImplementedError):
        components_to_provider_messages(comps, target_provider="anthropic")
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_session_components.py -k "rebuild or assistant_without or forward_compat" -v`
Expected: FAIL — `ImportError: cannot import name 'components_to_provider_messages'`.

- [x] **Step 3: Implement normalized CC reconstruction**

```python
# src/llm/session_components.py  (append)
import json


def _tool_calls_to_cc(tool_calls: List[dict]) -> List[dict]:
    """LangChain tool_calls ({name, args, id}) -> Chat Completions wire shape."""
    cc = []
    for tc in tool_calls:
        args = tc.get("args", {})
        cc.append({
            "id": tc.get("id"),
            "type": "function",
            "function": {
                "name": tc.get("name"),
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        })
    return cc


def components_to_provider_messages(
    components: List["MessageComponents"],
    target_provider: str = "openai-chat",
) -> list:
    """Rebuild a provider request (message array) from stored components.

    v1 implements the live Chat Completions path: normalized reconstruction
    (assistant content + tool_calls; tool results as role=tool). ``provider_raw``
    is audit/forward-compat and is NOT replayed here. Verbatim-raw replay for the
    real Responses API / Anthropic is forward-compat (raises NotImplementedError).
    """
    if target_provider != "openai-chat":
        raise NotImplementedError(
            f"verbatim-raw replay for provider {target_provider!r} is forward-compat; "
            "v1 supports normalized Chat Completions reconstruction only"
        )
    out: list = []
    for c in components:
        if c.role == "tool":
            for tr in c.tool_results:
                out.append({"role": "tool",
                            "tool_call_id": tr["tool_call_id"],
                            "content": tr["content"]})
        elif c.role == "ai":
            msg = {"role": "assistant", "content": c.text or ""}
            if c.tool_calls:
                msg["tool_calls"] = _tool_calls_to_cc(c.tool_calls)
            out.append(msg)
        else:
            out.append({"role": "user" if c.role in ("human", "user") else c.role,
                        "content": c.text or ""})
    return out
```

- [x] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_session_components.py -v`
Expected: PASS (all tests in the file, including the Task 1 fixture guard + Task 4 normalize).

- [x] **Step 5: Commit**

```bash
git add src/llm/session_components.py tests/test_session_components.py
git commit -m "feat(sessions): components_to_provider_messages — normalized Chat Completions reconstruction"
```

---

## Task 6: Fix the lossy agent-side reader

`src/database/postgres_db.py:322` omits `tool_call_id`, `thinking`, and (after 0019) the new columns — so the agent's resume rebuild is lossy (spec Q3 prerequisite). Make it select the full set.

**Files:**
- Modify: `src/database/postgres_db.py:322-358`
- Test: `tests/test_postgres_db_reader.py`

- [x] **Step 1: Write the failing test** (stub `fetch`; assert the mapped dict is lossless)

```python
# tests/test_postgres_db_reader.py
import pytest
from unittest.mock import AsyncMock
from src.database.postgres_db import PostgresDB


@pytest.mark.asyncio
async def test_history_includes_components_and_tool_link():
    db = PostgresDB.__new__(PostgresDB)  # bypass __init__/connection
    db.fetch = AsyncMock(return_value=[{
        "id": "11111111-1111-1111-1111-111111111111",
        "role": "ai",
        "content": "hi",
        "tool_calls": None,
        "turn_number": 1,
        "metrics": None,
        "tool_call_id": None,
        "thinking": "legacy reasoning",
        "reasoning": None,
        "tool_results": None,
        "provider": "openai-responses",
        "provider_raw": None,
        "additional_kwargs": None,
        "response_metadata": None,
        "created_at": None,
    }])
    rows = await db.get_thread_messages_history("t1")
    row = rows[0]
    for key in ("tool_call_id", "thinking", "reasoning", "tool_results",
                "provider", "provider_raw", "additional_kwargs", "response_metadata"):
        assert key in row, f"reader dropped {key}"
    assert row["provider"] == "openai-responses"
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_postgres_db_reader.py -v`
Expected: FAIL — `KeyError`/`assert` on `tool_call_id` (current reader omits it).

- [x] **Step 3: Implement the lossless reader**

Replace the body of `get_thread_messages_history` (`src/database/postgres_db.py:329-358`):

```python
        rows = await self.fetch(
            """
            SELECT id, role, content, tool_calls, turn_number, metrics,
                   tool_call_id, thinking, reasoning, tool_results,
                   provider, provider_raw, additional_kwargs, response_metadata,
                   created_at
            FROM thread_messages
            WHERE thread_id = $1
            ORDER BY turn_number ASC, created_at ASC
                LIMIT $2
            OFFSET $3
            """,
            thread_id,
            limit,
            offset,
        )

        def _j(v):
            return json.loads(v) if isinstance(v, (str, bytes)) else v

        result = []
        for row in rows:
            result.append({
                "id": str(row["id"]),
                "role": row["role"],
                "content": row["content"],
                "tool_calls": _j(row["tool_calls"]) if row["tool_calls"] else None,
                "turn_number": row["turn_number"],
                "metrics": _j(row["metrics"]) if row["metrics"] else None,
                "tool_call_id": row["tool_call_id"],
                "thinking": row["thinking"],
                "reasoning": _j(row["reasoning"]) if row["reasoning"] else None,
                "tool_results": _j(row["tool_results"]) if row["tool_results"] else None,
                "provider": row["provider"],
                "provider_raw": _j(row["provider_raw"]) if row["provider_raw"] else None,
                "additional_kwargs": _j(row["additional_kwargs"]) if row["additional_kwargs"] else None,
                "response_metadata": _j(row["response_metadata"]) if row["response_metadata"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            })
        return result
```

- [x] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_postgres_db_reader.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/database/postgres_db.py tests/test_postgres_db_reader.py
git commit -m "fix(sessions): lossless agent-side thread history reader (tool_call_id/thinking/components)"
```

---

## Task 7: Persist the new columns through the write path

Extend the writer end-to-end: `postgres.save_thread_message` (INSERT) → `AgentThreadMessageRequest` (HTTP model) → POST handler → `orchestrator_client.save_thread_message`. New params are all optional/nullable so existing callers are unaffected.

**Files:**
- Modify: `orchestrator/database/postgres.py:2929-2974`
- Modify: `orchestrator/main.py` (`AgentThreadMessageRequest` ~:10513; handler ~:11200)
- Modify: `src/api/orchestrator_client.py:246`
- Test: `tests/test_save_thread_message_columns.py`

- [x] **Step 1: Write the failing test** (assert the INSERT carries the new columns + values)

```python
# tests/test_save_thread_message_columns.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from orchestrator.database.postgres import PostgresDB


@pytest.mark.asyncio
async def test_save_thread_message_inserts_component_columns():
    db = PostgresDB.__new__(PostgresDB)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "abc"})
    conn.execute = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.acquire = MagicMock(return_value=cm)

    await db.save_thread_message(
        thread_id="t1", role="ai", content="hi", turn_number=1,
        provider="openai-responses",
        provider_raw=[{"type": "reasoning", "id": "rs_1"}],
        reasoning="thinking…",
    )

    sql = conn.fetchrow.call_args[0][0]
    assert "provider" in sql and "provider_raw" in sql and "reasoning" in sql
    # provider_raw is JSON-encoded for the JSONB column; assert the value reached the args
    assert any(
        isinstance(a, str) and "rs_1" in a for a in conn.fetchrow.call_args[0]
    )
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_save_thread_message_columns.py -v`
Expected: FAIL — current SQL lacks `provider`/`provider_raw`/`reasoning`.

- [x] **Step 3: Implement — extend `save_thread_message`**

Replace the signature + INSERT in `orchestrator/database/postgres.py:2929-2962`:

```python
    async def save_thread_message(
        self,
        thread_id: str,
        role: str,
        content: Optional[str],
        tool_calls: Optional[Any] = None,
        turn_number: Optional[int] = None,
        metrics: Optional[dict] = None,
        tool_call_id: Optional[str] = None,
        thinking: Optional[str] = None,
        reasoning: Optional[Any] = None,
        tool_results: Optional[Any] = None,
        provider: Optional[str] = None,
        provider_raw: Optional[Any] = None,
        additional_kwargs: Optional[dict] = None,
        response_metadata: Optional[dict] = None,
    ) -> str:
        """Save a message to thread_messages (append-only; never UPDATE a row).

        Component columns (reasoning/tool_results/provider/provider_raw/
        additional_kwargs/response_metadata) are nullable — see migration 0019.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_messages
                    (thread_id, role, content, tool_calls, turn_number,
                     metrics, tool_call_id, thinking, reasoning, tool_results,
                     provider, provider_raw, additional_kwargs, response_metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14) RETURNING id
                """,
                thread_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                turn_number,
                json.dumps(metrics) if metrics else None,
                tool_call_id,
                thinking,
                json.dumps(reasoning) if reasoning is not None else None,
                json.dumps(tool_results) if tool_results else None,
                provider,
                json.dumps(provider_raw) if provider_raw is not None else None,
                json.dumps(additional_kwargs) if additional_kwargs else None,
                json.dumps(response_metadata) if response_metadata else None,
            )
            await conn.execute(
                """
                UPDATE threads
                SET last_activity = CURRENT_TIMESTAMP,
                    total_turns   = GREATEST(total_turns, COALESCE($2, 0))
                WHERE id = $1
                """,
                thread_id,
                turn_number,
            )
        return str(row["id"])
```

- [x] **Step 4: Run the leaf test to verify it passes**

Run: `python -m pytest tests/test_save_thread_message_columns.py -v`
Expected: PASS.

- [x] **Step 5: Extend the HTTP model + handler + client (plumbing)**

In `orchestrator/main.py`, add the optional fields to `AgentThreadMessageRequest` (~:10513):

```python
    reasoning: Optional[Any] = None
    tool_results: Optional[Any] = None
    provider: Optional[str] = None
    provider_raw: Optional[Any] = None
    additional_kwargs: Optional[dict] = None
    response_metadata: Optional[dict] = None
```

In the `POST /api/agents/threads/{id}/messages` handler (~:11200), forward them into `save_thread_message(...)` (pass each `req.<field>`).

In `src/api/orchestrator_client.py:246` `save_thread_message`, add the same optional params and include them in the JSON body.

- [x] **Step 6: Contract test for the request model**

```python
# tests/test_save_thread_message_columns.py  (append)
from orchestrator.main import AgentThreadMessageRequest


def test_request_model_accepts_component_fields():
    m = AgentThreadMessageRequest(
        role="ai", content="hi", provider="openai-responses",
        provider_raw=[{"type": "reasoning", "id": "rs_1"}], reasoning="x",
    )
    assert m.provider == "openai-responses"
    assert m.provider_raw[0]["id"] == "rs_1"
```

Run: `python -m pytest tests/test_save_thread_message_columns.py -v`
Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add orchestrator/database/postgres.py orchestrator/main.py src/api/orchestrator_client.py tests/test_save_thread_message_columns.py
git commit -m "feat(sessions): persist component columns through the write path (additive)"
```

---

## Task 8: Full regression sweep

- [x] **Step 1: Run the touched-area suites**

Run:
```bash
python -m pytest tests/test_session_components.py tests/test_postgres_db_reader.py \
  tests/test_save_thread_message_columns.py tests/test_persistent_graph.py \
  tests/test_response_guards.py -q
```
Expected: all PASS. (The pre-existing `test_thread_events_phase2.py` FastAPI-204 collection error is unrelated — see `docs/issues/`.)

- [x] **Step 2: Confirm nothing runtime changed yet**

Grep that `session_components` is not yet imported by the loop (Plan 2 wires it):
Run: `rg -n "session_components" src/persistent_graph.py || echo "not wired (expected for Plan 1)"`
Expected: "not wired (expected for Plan 1)".

- [x] **Step 3: Commit (if any fixups)**

```bash
git commit -am "test(sessions): Plan 1 regression sweep green" --allow-empty
```

---

## Self-review notes (author)

- **Spec coverage:** D2 (normalized + raw, verbatim/ordered) → Tasks 4/5; Q3 (add columns, dev-cutover, index, reader fix) → Tasks 2/3/6; write path → Task 7. D1/D6/D3/D4 and authority inversion are explicitly **Plan 2+** (not this plan).
- **Known approximation:** Task 1 depends on real proxy output; if the capture shows the proxy strips `encrypted_content`/reasoning ids, STOP and update the spec before Task 5 (the verbatim-replay assumption breaks).
- **Anthropic replay** (thinking+signature) is represented by the same `provider_raw` mechanism; a dedicated Anthropic round-trip test is deferred to when an Anthropic fixture is captured (add alongside Task 1).
