---
tags:
  - feature
  - architecture
  - llm
  - support-tasks
aliases:
  - auxiliary llm
  - auxiliary tasks
  - support llm
related:
  - "[[memory_light]]"
  - "[[project_knowledge_base]]"
  - "[[context_management]]"
---

# Auxiliary LLM — Unified Support Task System

## The Problem (pre-migration)

The agent system had several "support" LLM tasks scattered across different components, each with its own invocation pattern, error handling, and configuration:

| Task | Old Location | How It Ran |
|------|-------------|------------|
| Conversation summarization | `ContextManager.summarize_and_compact()` | Inline LLM call with main/strategic LLM |
| Memory extraction | `MemoryObserver.extract_memories()` | Async background task, configurable model |
| Knowledge curation | Curator subjob (`config/experts/curator/`) | Full agent job with phases, todos, workspace |
| Memory injection assembly | `RecallStore.retrieve()` | No LLM — vector search + ranking |
| Knowledge injection | `KnowledgeStore.hybrid_search()` | No LLM — vector search + ranking |

Problems:

1. **The curator subjob was overkill.** It ran the full agent loop — strategic/tactical phases, todo management, workspace.md, plan.md, git branching — just to read artifacts and call `kb_write` a few times. Massive overhead for a structured extraction task.

2. **Scattered configuration.** Summarization used the main LLM. The memory observer had `observer_model` / `observer_base_url`. The curator had its own expert config. Three different ways to configure what is conceptually the same thing: "use a support model for a background task."

3. **No reuse.** The observer's extraction prompt, the summarization call, and the curator's curation logic all followed the same pattern (system prompt + context → structured output) but shared no infrastructure.

4. **Wasted model capability.** The free `gpt-oss-120b` on the university servers has good reasoning capabilities but isn't great at long-running agent jobs. The old system either used it as a full agent (where it struggles) or didn't use it at all. What it's good at — structured reasoning over provided context — is exactly what support tasks need.

**All three LLM support tasks are now unified through `AuxiliaryLLM`.** Summarization, memory extraction, and knowledge curation all use the same class, same model config, and same error handling pattern.

## The Solution: AuxiliaryLLM

A unified class that exposes two modes for support tasks:

### Chain Mode (in → out)

Single LLM call. System prompt + context in, structured JSON out. No tools, no loop. For tasks that just need reasoning over provided context.

```
system_prompt + context  →  LLM  →  structured result (JSON)
```

### Agent Mode (in → tool calls → out)

Short-lived tool loop. The LLM can make a few tool calls (search KB, read files, write notes), then returns a structured result. Capped iterations. Not a full job — no workspace, no todos, no phases. Just a mini-agent that runs inline and returns.

```
system_prompt + context  →  LLM  →  tool_call  →  result
                              ↑                      │
                              └──────────────────────┘
                              (repeat until done or max_iterations)
                              → structured result (JSON)
```

The critical difference from the old curator subjob: no job creation, no workspace setup, no phase alternation, no git branching, no todo management. Just a prompt, optional tools, a tight loop with a hard cap, and a structured result.

## Task Mapping

### Chain Mode Tasks

These need no tool access — just reasoning over provided context:

| Task | Replaces | Input | Output Schema |
|------|----------|-------|---------------|
| **Summarize** | `ContextManager.summarize_and_compact()` | Conversation chunk | `{summary: str}` |
| **Extract Memories** | `MemoryObserver.extract_memories()` | Recent messages | `[{content, summary, keywords, importance, type}]` |
| **Rank Injection** | (new — optional) | Candidate notes + current context | `[{note_id, relevance_score}]` |
| **Classify Phase** | (new — optional) | Phase artifacts | `{decisions: [...], learnings: [...], questions: [...]}` |

### Agent Mode Tasks

These need a few tool calls to interact with the knowledge base or filesystem:

| Task | Replaces | Tools Needed | Typical Iterations |
|------|----------|--------------|--------------------|
| **Curate Knowledge** | Curator subjob | `kb_search`, `kb_write`, `kb_update`, `kb_read` | 5–15 |
| **Migrate Memories** | `memory_migrator.py` | `kb_search`, `kb_write` | 5–10 |
| **Convert Workspace** | `workspace_converter.py` | `kb_search`, `kb_write`, `read_file` | 5–10 |

## Interface (as implemented)

All code lives in `src/services/auxiliary.py`.

### AuxiliaryLLM

Both modes use `with_structured_output()` for reliable Pydantic model returns — no manual JSON parsing.

```python
class AuxiliaryLLM:
    """Unified support task execution with chain and agent modes."""

    def __init__(
        self,
        llm: BaseChatModel,
        max_iterations: int = 15,
        timeout: float = 120.0,
    ): ...

    async def chain(self, task: AuxTask) -> BaseModel:
        """Single LLM call: system prompt + context → Pydantic model.
        Uses with_structured_output(task.output_schema)."""

    async def agent(self, task: AuxAgentTask) -> BaseModel:
        """Tool loop → final structured-output call → Pydantic model.
        Runs tool loop with bind_tools(), then one final
        with_structured_output() call to produce the result."""
```

### Task Base Classes

```python
class AuxTask(ABC):
    """Base class for chain-mode tasks."""

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    @abstractmethod
    def build_context(self) -> str: ...

    @property
    @abstractmethod
    def output_schema(self) -> Type[BaseModel]: ...


class AuxAgentTask(AuxTask):
    """Base class for agent-mode tasks (adds tool access)."""

    @abstractmethod
    def get_tools(self) -> list: ...
```

Key difference from original design: tasks define `output_schema` (a Pydantic model class) instead of `parse_response()`. The `AuxiliaryLLM` handles structured output via `with_structured_output()` — no manual JSON parsing, no markdown fence stripping, no fallback extraction.

### Implemented Tasks

#### ExtractMemoriesTask (Chain Mode) ✅ Wired

Replaces `MemoryObserver.extract_memories()`. Uses `with_structured_output(ExtractedMemories)` instead of the old free-form JSON + manual parsing approach.

```python
class ExtractedMemory(BaseModel):
    content: str      # The insight (1-3 sentences)
    summary: str      # One-line summary
    keywords: List[str]
    importance: float  # 0.0-1.0
    type: str          # factual, procedural, error_solution, vocabulary, relational

class ExtractedMemories(BaseModel):
    memories: List[ExtractedMemory]

class ExtractMemoriesTask(AuxTask):
    output_schema = ExtractedMemories
```

**vs. old MemoryObserver:**

| Aspect | Old (MemoryObserver) | New (ExtractMemoriesTask) |
|--------|---------------------|--------------------------|
| LLM call | `self.llm.ainvoke([HumanMessage])` — prompt + context in one message | `with_structured_output()` — SystemMessage + HumanMessage |
| Output parsing | Free-form text → `_parse_extraction_response()` (JSON extraction, markdown fence strip, field validation, type/importance clamping) | `with_structured_output(ExtractedMemories)` → Pydantic model directly |
| Prompt | Includes JSON example (needed for free-form parsing) | No example needed — schema drives the output |
| Message windowing | `_get_message_segment()`: `min(window_size * 2, 40)` based on turn range since last observation | Caps at `_MAX_OBSERVATION_WINDOW = 40` from the end of the full message list |
| Message formatting | `_format_messages_for_extraction()` (identical in both) | Same function, copied to `auxiliary.py` |
| Interval tracking | `_last_observed_turn` instance variable on `MemoryObserver` | `last_observed_turn` field on `UniversalAgentState` |
| Store loop | Iterates `List[Dict]`, uses `.get()` with defaults | Iterates `List[ExtractedMemory]`, uses typed attributes |

**Known difference — message windowing:** The old observer sliced to roughly the messages *since last observation* (`window_size * 2`). The new code always sends the last 40 messages regardless of when extraction last ran. This means the new code may re-analyze previously extracted messages. This is acceptable because `RecallStore` deduplicates via `find_similar()`, but it does mean slightly more tokens per extraction call.

#### SummarizeTask (Chain Mode) ✅ Wired

Replaces inline summarization in `ContextManager._single_pass_summarize()`.

```python
# Output schema lives in src/core/context.py (tightly coupled with compaction formatting)
class ConversationSummary(BaseModel):
    completed_work: str
    key_decisions: str
    discovered_info: str
    current_state: str
    errors_blockers: str
    failed_approaches: str

class SummarizeTask(AuxTask):
    output_schema = ConversationSummary
```

Accepts an optional `summarization_prompt` override (rendered from the config's prompt matrix). Falls back to `_DEFAULT_SUMMARIZATION_PROMPT` if none provided.

#### CurateKnowledgeTask (Agent Mode) ✅ Wired

Replaces the curator subjob. Runs inline via `curate_and_store_knowledge()` in `archive_phase`.

```python
class CurationResult(BaseModel):
    notes_created: int
    notes_updated: int
    summary: str

class CurateKnowledgeTask(AuxAgentTask):
    output_schema = CurationResult
    # get_tools() returns all kb_* tools from create_kb_tools()
```

**vs. old Curator Subjob:**

| Aspect | Old (Curator Subjob) | New (CurateKnowledgeTask) |
|--------|---------------------|--------------------------|
| Execution | Full agent job: workspace, todos, phases, git | Inline `AuxiliaryLLM.agent()` call, ~5-15 iterations |
| Trigger | `curation_callback` → `_maybe_trigger_curation()` → POST to orchestrator API | `curate_and_store_knowledge()` called directly in `archive_phase` |
| Lifecycle | Spawned once, resumed on each phase via orchestrator | Stateless — runs fresh on each archive phase |
| Config | `curator.curator_config` → separate expert config dir | `curator.enabled` + `auxiliary.enabled` in main config |
| Infrastructure | `OrchestratorClient.create_curation_job()`, `_format_curation_instructions()`, callback wiring in `app.py` | `curate_and_store_knowledge()` helper (~60 lines in `auxiliary.py`) |
| Final pass | `_maybe_trigger_curation_final_pass()` after critic approval | Removed — incremental per-phase only |
| KB initialization | Curator was a separate job that initialized its own Neo4j/pgvector connections | Main agent initializes `KnowledgeGraphDB` + `KnowledgeStore` in `_setup_job_tools()` |
| Tools | Same `kb_*` tools, loaded by curator's own tool loading | Same `kb_*` tools via `create_kb_tools(tool_context)` |
| Output | Job completion with freeze_data | `CurationResult` Pydantic model (notes_created, notes_updated, summary) |
| Error handling | Job failure (visible in orchestrator) | Non-fatal — logged and swallowed, never blocks the main agent |

**Knowledge infrastructure wiring:** The main agent now initializes `KnowledgeGraphDB` (Neo4j) and `KnowledgeStore` (pgvector) on `ToolContext` when `curator.enabled` is true and the job has a `project_id`. Connection is cleaned up in `_close_datasource_connections()`. Previously these fields existed on `ToolContext` but were never populated — the old curator was a separate job that created its own connections.

### Knowledge Curation Helper

`curate_and_store_knowledge()` replaces the curator subjob infrastructure:

```python
async def curate_and_store_knowledge(
    auxiliary_llm: AuxiliaryLLM,
    tool_context: ToolContext,
    phase_data: str,
    workspace_md: str,
    plan_md: str,
) -> Optional[CurationResult]:
    """Run inline knowledge curation via AuxiliaryLLM agent mode."""
```

Called from `archive_phase` in `src/graph.py` as `asyncio.create_task()` (non-blocking). Guards:
- `tool_context.has_knowledge()` (Neo4j + pgvector available)
- `config.extra.curator.enabled` is true
- `config.auxiliary.enabled` is true

The helper fetches existing notes from Neo4j (for duplicate awareness), creates KB tools via `create_kb_tools()`, and runs `CurateKnowledgeTask` in agent mode.

### Memory Extraction Helper

`extract_and_store_memories()` replaces both `MemoryObserver.observe()` and `observe_phase_boundary()`:

```python
async def extract_and_store_memories(
    auxiliary_llm: AuxiliaryLLM,
    recall_store,
    messages: List[BaseMessage],
    phase: int = 0,
    source_turn_start: Optional[int] = None,
    source_turn_end: Optional[int] = None,
) -> int:
    """Extract memories via chain() and store in RecallStore. Returns stored count."""
```

Called from two places in `src/graph.py`:
1. **Execute node** — every N turns (interval from `config.memory.observer_interval`)
2. **Archive phase node** — at every phase boundary

Both fire as `asyncio.create_task()` (non-blocking), same as the old observer.

`_should_extract_memories(turn_count, interval, last_observed_turn)` is the pure-function replacement for `MemoryObserver.should_observe()`.

## Configuration (as implemented)

```yaml
# config/defaults.yaml

auxiliary:
  enabled: true
  model: null              # null = use main LLM; or "gpt-oss-120b"
  base_url: null           # null = use LLM_BASE_URL; or custom endpoint
  temperature: 0.0
  max_iterations: 15       # Cap for agent mode loops
  timeout: 120             # Seconds per LLM call
  tasks:
    extract_memories:
      enabled: true
    curate_knowledge:
      enabled: true

# Inline knowledge curation (requires Neo4j + project_id in job metadata)
curator:
  enabled: false           # Opt-in per config or per project
```

**Two flags gate curation:** `curator.enabled` (feature gate — is curation desired for this config?) and `auxiliary.enabled` (is the auxiliary LLM available?). Both must be true, plus the job must have a `project_id` and Neo4j must be reachable.

**Note:** The extraction interval is still read from `config.memory.observer_interval` (default: 5), not from `auxiliary.tasks.extract_memories`. The `memory.observer_model` / `memory.observer_base_url` keys are still in `defaults.yaml` but no longer used — the auxiliary model/base_url settings take precedence.

## What Changed and What Stays

### Memory Observer → ExtractMemoriesTask (chain mode)

**Replaced:**
- `MemoryObserver.observe()` / `observe_phase_boundary()` → `extract_and_store_memories()` + `ExtractMemoriesTask`
- `MemoryObserver.should_observe()` → `_should_extract_memories()` (pure function)
- `MemoryObserver._last_observed_turn` (instance variable) → `last_observed_turn` (state field on `UniversalAgentState`)
- `MemoryObserver._format_messages_for_extraction()` → `_format_messages_for_extraction()` in `auxiliary.py` (identical copy)
- `MemoryObserver._parse_extraction_response()` (manual JSON parsing) → `with_structured_output(ExtractedMemories)` (eliminated entirely)
- `MemoryObserver` initialization in `src/agent.py` (30 lines of LLM creation + observer setup) → removed, `AuxiliaryLLM` handles it
- `memory_observer` field on `ToolContext` → removed

### Summarization → SummarizeTask (chain mode)

**Replaced:**
- Inline summarization LLM call in `ContextManager._single_pass_summarize()` → `SummarizeTask` via `auxiliary_llm.chain()`

### Curator Subjob → CurateKnowledgeTask (agent mode)

**Replaced:**
- `curation_callback` parameter on `create_archive_phase_node()` and `build_phase_alternation_graph()` → `tool_context` + `workspace_manager` parameters
- `_maybe_trigger_curation()` in `src/api/app.py` (spawns/resumes curator via orchestrator API) → `curate_and_store_knowledge()` inline call in `archive_phase`
- `_maybe_trigger_curation_final_pass()` in `src/api/app.py` (final curation after critic approval) → removed entirely (incremental-only)
- `OrchestratorClient.create_curation_job()` + `_format_curation_instructions()` → removed (~170 lines)
- `_get_curation_config()` + `_is_curation_enabled()` in `src/api/app.py` → removed
- `self.curation_callback` attribute on `UniversalAgent` → removed
- Callback assignment in `app.py` startup (`_agent.curation_callback = _maybe_trigger_curation`) → removed

**Added:**
- `curate_and_store_knowledge()` in `src/services/auxiliary.py` — helper that runs `CurateKnowledgeTask` via `AuxiliaryLLM.agent()`
- `KnowledgeGraphDB` + `KnowledgeStore` initialization in `src/agent.py` `_setup_job_tools()` — populates `ToolContext.knowledge_graph`, `ToolContext.knowledge_store`, and `ToolContext.project_id`
- `self._knowledge_graph` on `UniversalAgent` — tracks Neo4j connection for cleanup in `_close_datasource_connections()`

**Kept as-is:**
- `config/experts/curator/` — still a valid agent config for manual curator runs, just no longer auto-spawned
- `curator.enabled` flag in `config/defaults.yaml` — still gates the feature, now controls inline curation instead of subjob spawning

### Dead code (not yet removed)

- `src/services/memory_observer.py` — no longer imported or called from the graph
- `tests/test_memory_observer.py` — tests the dead code
- `MemoryObserver` export from `src/services/__init__.py` — removed
- `memory.observer_model` / `memory.observer_base_url` config keys — still in `defaults.yaml`, unused

### Stays the same

- `RecallStore` — storage and retrieval unchanged, called by `extract_and_store_memories()` instead of the observer
- Free memory sources (todo completion, compaction, phase archive, tool errors) — unchanged, these are programmatic
- Memory injection / Knowledge injection — unchanged
- KB tools (`kb_write`, `kb_search`, etc.) — unchanged, now also used by the auxiliary curation agent
- `KnowledgeGraphDB` / `KnowledgeStore` services — unchanged, just now initialized by the main agent

## Why Two Modes

The free `gpt-oss-120b` has good reasoning capabilities but isn't great at long-running agent jobs. The two modes play to its strengths:

- **Chain mode** is pure reasoning — no tool calling complexity, no multi-turn management. The model gets context and produces structured output. This is where models like gpt-oss-120b excel.

- **Agent mode** is a short, bounded tool loop — 5-15 iterations max, focused tools, clear objective. The model doesn't need to maintain coherence over 50+ turns or manage a complex plan. It just needs to search, decide, and write. This is well within the capability of models that struggle as full agents.

The key insight: the distinction between "reasoning" and "agent" is about **loop length**, not capability. A model that fails at a 100-turn job with phase management can succeed at a 10-turn loop with 4 tools.

## LLM Call Audit — What's Unified, What's Not

### Through AuxiliaryLLM

| Task | Mode | Wired In | Replaces |
|------|------|----------|----------|
| **Summarization** | `chain()` → `SummarizeTask` | `src/core/context.py` | Inline summarization LLM call |
| **Memory extraction** | `chain()` → `ExtractMemoriesTask` | `src/graph.py` (execute + archive_phase) | `MemoryObserver.observe()` |
| **Knowledge curation** | `agent()` → `CurateKnowledgeTask` | `src/graph.py` (archive_phase) | Curator subjob |

### Intentionally Outside AuxiliaryLLM

These are **not** "support reasoning tasks" — they are specialized services with their own model configs and are out of scope for unification:

| Service | File | LLM Config | Why It's Separate |
|---------|------|-----------|-------------------|
| **Vision Helper** | `src/services/vision_helper.py:136,211` | `VISION_API_KEY`, `VISION_BASE_URL`, `VISION_MODEL` | Multimodal image→text service using the OpenAI API directly (not LangChain). Requires a vision-capable model, different from the support model. |
| **Browser Agent** | `src/tools/research/browser.py:175,244` | `BROWSER_LLM_MODEL`, `BROWSER_LLM_API_KEY`, `BROWSER_LLM_BASE_URL` | `browser-use` library with its own agent loop. This is a tool, not a support task — it runs when the agent explicitly calls `browse_website`. |

### Minor Loose End

| Item | File | Notes |
|------|------|-------|
| **Context compaction fallback** | `src/core/context.py` | When `SummarizeTask` structured output fails, falls back to `auxiliary.llm.ainvoke()` directly — bypasses the `chain()` API. Acceptable as an error-recovery path. |

## Remaining Work

### Dead Code Cleanup ❌

- `src/services/memory_observer.py` — no longer imported or called from the graph
- `tests/test_memory_observer.py` — tests the dead code
- `memory.observer_model` / `memory.observer_base_url` in `defaults.yaml` — unused
- Steps: delete `memory_observer.py` + its tests, remove unused config keys

### Config Consolidation (optional)

- Move extraction interval from `config.memory.observer_interval` to `auxiliary.tasks.extract_memories.interval`
- Remove `memory.observer_model` / `memory.observer_base_url` (superseded by `auxiliary.model` / `auxiliary.base_url`)

## Resolved Questions

1. ~~**Summarization migration**~~ — Resolved: summarization now uses `SummarizeTask` via `auxiliary_llm.chain()`.

2. ~~**Agent mode error handling**~~ — Resolved: skip the failed tool call (append error as ToolMessage), let the LLM decide how to proceed. Abort only on iteration cap. Implemented in `AuxiliaryLLM.agent()`.

3. ~~**Structured output vs. free-form**~~ — Resolved: all tasks use `with_structured_output()` via Pydantic models. The context compaction fallback uses free-form as an error-recovery path.

4. ~~**Curation scope**~~ — Resolved: incremental per-phase only. No final curation pass after critic approval. The old final pass (`_maybe_trigger_curation_final_pass`) was removed entirely.

## Open Questions

1. **Concurrency** — memory extraction and curation both run as `asyncio.create_task()` (non-blocking). If both fire at the same time (e.g., on archive phase), they'll compete for the same LLM endpoint. Current implementation: both fire independently as async tasks.

2. **Message windowing regression** — The old `MemoryObserver` sliced messages to the range since last observation (`window_size * 2`, capped at 40). The new `extract_and_store_memories()` always sends the last 40 messages. This may re-analyze previously extracted messages. Acceptable because `RecallStore` deduplicates, but costs extra tokens. Consider restoring windowed slicing if token cost becomes an issue.

3. **Curation without Neo4j** — The inline curation requires a running Neo4j instance (`NEO4J_URL` env var). If Neo4j isn't available, `KnowledgeGraphDB.connect()` fails and curation is silently disabled. This is the same behavior as the old curator subjob (it also needed Neo4j), but now the failure is at the main agent level rather than the subjob level.

## References

- [[memory_light]] — Memory extraction architecture (recall store, free sources, RRF search)
- [[project_knowledge_base]] — Knowledge base architecture (Neo4j, pgvector, KB tools)
- [[context_management]] — Summarization and compaction
