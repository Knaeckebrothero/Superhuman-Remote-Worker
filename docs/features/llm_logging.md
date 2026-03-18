# Auxiliary LLM Call Logging

## Status: Design

## Problem

Only the main agent LLM calls (the ReAct loop in `src/graph.py`) are archived to MongoDB via `LLMArchiver`. Auxiliary calls — summarization, memory extraction, memory assembly, knowledge curation, and vision — bypass logging entirely. This creates blind spots in cost tracking and debugging:

- **Cost visibility**: No way to know how much token spend goes to auxiliary tasks vs. the main loop.
- **Debugging**: When summarization produces a bad summary or memory extraction misses something, there's no record of the input/output to investigate.
- **Capacity planning**: Can't measure how auxiliary call volume scales with job length.

## Current State

### What gets logged (main loop)

The `execute` node in `src/graph.py` archives every main LLM call via two-phase auditing:

1. **Pre-call**: `auditor.audit_llm_call()` → `agent_audit` collection (nulled response fields)
2. **Post-call**: `auditor.archive()` → `llm_requests` collection (full request/response + metrics)
3. **Response update**: `auditor.update_llm_response()` → fills response fields in `agent_audit`

Additionally, `chat_history` gets a clean conversation entry for each main call.

### What doesn't get logged

| Call Type | Location | Invocation |
|---|---|---|
| **Summarization** | `src/core/context.py` `_single_pass_summarize()` | `auxiliary_llm.chain(SummarizeTask(...))` |
| **Memory extraction** | `src/services/auxiliary.py` | `auxiliary_llm.chain(ExtractMemoriesTask(...))` |
| **Memory assembly** | `src/services/auxiliary.py` | `auxiliary_llm.agent(AssembleMemoriesTask(...))` |
| **Knowledge curation** | `src/services/auxiliary.py` | `auxiliary_llm.agent(CurateKnowledgeTask(...))` |
| **Vision** | `src/services/vision_helper.py` | `client.chat.completions.create(...)` (raw OpenAI) |
| **Embeddings** | `src/services/embedding_service.py` | `client.embeddings.create(...)` (raw OpenAI) |

Summarization and memory calls go through `AuxiliaryLLM.chain()` / `.agent()`. Vision uses a raw `AsyncOpenAI` client. Embeddings are a different API entirely (not chat completions).

## Design

### Approach: Same collections, tagged by `call_type`

Log auxiliary calls into the existing `llm_requests` collection with a `call_type` field to distinguish them from main loop calls. Skip `agent_audit` and `chat_history` — those are about the agent's decision-making trace and conversational flow, which auxiliary calls aren't part of.

**Why not a separate collection?**
- Single source of truth for all LLM spend per job.
- Existing aggregation queries and tooling (MongoDB viewer, MCP tools, cockpit) automatically pick up auxiliary costs.
- No need to union across collections for "total cost of job X".

### `call_type` values

| Value | Source | Mode |
|---|---|---|
| `main` | `execute` node in graph.py | Existing (default) |
| `summarization` | `ContextManager._single_pass_summarize()` | chain |
| `memory_extraction` | `extract_and_store_memories()` | chain |
| `memory_assembly` | `assemble_memories()` | agent (multiple calls) |
| `knowledge_curation` | `curate_and_store_knowledge()` | agent (multiple calls) |
| `vision` | `VisionHelper.describe_image()` / `describe_document_page()` | raw OpenAI |

Embeddings are excluded — they're a different API (not chat completions), low cost, and high volume. Logging them would add noise without proportional value.

### Schema addition to `llm_requests`

```javascript
{
  // ... existing fields ...
  "call_type": "summarization",      // NEW — defaults to "main" for existing docs
  "auxiliary_metadata": {             // NEW — optional, call-type-specific context
    "task_class": "SummarizeTask",    // AuxTask subclass name
    "trigger": "context_compaction",  // What triggered this call
    "iteration": 2,                   // For agent-mode: which iteration of the tool loop
    "total_iterations": 5,            // For agent-mode: total iterations in the loop
  }
}
```

### Changes needed

#### 1. `LLMArchiver.archive()` — add `call_type` parameter

Add optional `call_type: str = "main"` parameter. Written directly to the document. Skip `chat_history` when `call_type != "main"`.

```python
def archive(
    self,
    ...
    call_type: str = "main",
    auxiliary_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
```

#### 2. `AuxiliaryLLM` — accept and use archiver

Pass the archiver and job context into `AuxiliaryLLM` so it can log calls.

```python
class AuxiliaryLLM:
    def __init__(
        self,
        llm: BaseChatModel,
        max_iterations: int = 15,
        timeout: float = 120.0,
        archiver: Optional[LLMArchiver] = None,
        job_id: Optional[str] = None,
        agent_type: Optional[str] = None,
    ):
```

**`chain()` logging**: Single archive call after the LLM responds. Derives `call_type` from `task.__class__.__name__` → mapped value.

**`agent()` logging**: Archive each iteration of the tool loop individually. Each doc gets `auxiliary_metadata.iteration` and `auxiliary_metadata.total_iterations` (filled in after the loop completes, or on each call). The final structured-output call is logged as a separate entry with `auxiliary_metadata.trigger = "final_structured_output"`.

#### 3. `VisionHelper` — add archiver support

Vision uses raw `AsyncOpenAI`, not LangChain messages. Two options:

**Option A**: Construct synthetic LangChain messages from the OpenAI request/response and call `archiver.archive()`.

**Option B**: Add a lightweight `archiver.archive_raw()` method that accepts plain dicts instead of LangChain messages.

Prefer **Option A** — it keeps the archive format consistent and the conversion is trivial (one `HumanMessage` with the prompt text, one `AIMessage` with the response content). Image data should be excluded from the archive (just log `"[image: {mime_type}, {len(image_data)} bytes]"` as a placeholder).

#### 4. Existing queries — add default filter

Any query or aggregation that currently assumes all `llm_requests` docs are main loop calls needs a `call_type: "main"` filter (or `call_type: {"$exists": false}` for backwards compatibility with old docs).

Affected locations:
- `LLMArchiver.get_conversation()` — add `call_type: "main"` to query
- `LLMArchiver.get_job_stats()` — return breakdown by `call_type`, keep existing totals
- `DEPRECATED_scripts/view_llm_conversation.py` — filter by default, `--all` flag to include auxiliary
- Orchestrator MCP tools (`get_llm_request`, `list_llm_requests`) — add optional `call_type` filter
- Cockpit API endpoints — filter to `main` by default

#### 5. `get_job_stats()` — enhanced output

```javascript
{
  "total_requests": 150,
  "total_input_chars": 500000,
  "total_output_chars": 120000,
  "by_call_type": {
    "main": { "count": 120, "input_chars": 450000, "output_chars": 110000 },
    "summarization": { "count": 8, "input_chars": 40000, "output_chars": 8000 },
    "memory_extraction": { "count": 15, "input_chars": 6000, "output_chars": 1500 },
    "vision": { "count": 7, "input_chars": 4000, "output_chars": 500 }
  }
}
```

### Task class → call_type mapping

In `AuxiliaryLLM`, derive `call_type` from the task class:

```python
_TASK_CALL_TYPES = {
    "SummarizeTask": "summarization",
    "ExtractMemoriesTask": "memory_extraction",
    "AssembleMemoriesTask": "memory_assembly",
    "CurateKnowledgeTask": "knowledge_curation",
}
```

Unmapped task classes fall back to `"auxiliary"` as a catch-all.

### Backwards compatibility

- Old documents without `call_type` are treated as `"main"` (query filter: `{"$or": [{"call_type": "main"}, {"call_type": {"$exists": false}}]}`).
- No migration needed — the field is simply absent on old docs.
- `get_job_stats()` continues to return the same top-level totals; `by_call_type` is additive.

## Implementation Order

1. Add `call_type` + `auxiliary_metadata` to `LLMArchiver.archive()`, gate `chat_history` writes on `call_type == "main"`
2. Wire archiver + job context into `AuxiliaryLLM.__init__()`, log from `chain()` and `agent()`
3. Add archiver to `VisionHelper`, log from `describe_image()` and `describe_document_page()`
4. Update `get_conversation()` and `get_job_stats()` with `call_type` awareness
5. Update downstream consumers (viewer script, MCP tools, cockpit endpoints)

## Files to Modify

| File | Change |
|---|---|
| `src/core/archiver.py` | Add `call_type`, `auxiliary_metadata` params; update `get_conversation()`, `get_job_stats()` |
| `src/services/auxiliary.py` | Accept archiver in `AuxiliaryLLM`, log from `chain()` and `agent()` |
| `src/services/vision_helper.py` | Accept archiver, log from `describe_image()` and `describe_document_page()` |
| `src/graph.py` | Pass archiver + job context when constructing `AuxiliaryLLM` |
| `src/core/context.py` | No change (calls go through `AuxiliaryLLM.chain()` which now logs) |
| `DEPRECATED_scripts/view_llm_conversation.py` | Add `--all` / `--call-type` filter |
| `orchestrator/mcp/tools/debug.py` | Add `call_type` filter to LLM request tools |

## Non-Goals

- **Logging embeddings**: Different API, high volume, low cost. Not worth the noise.
- **Logging to `agent_audit`**: Auxiliary calls aren't agent decisions. Keep audit clean.
- **Logging to `chat_history`**: Auxiliary calls aren't part of the conversation. Keep chat clean.
- **Real-time streaming of auxiliary logs**: Nice-to-have, but out of scope. Standard async logging is sufficient.
