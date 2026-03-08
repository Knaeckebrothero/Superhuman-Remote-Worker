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

## The Problem

The agent system has several "support" LLM tasks scattered across different components, each with its own invocation pattern, error handling, and configuration:

| Task | Current Location | How It Runs |
|------|-----------------|-------------|
| Conversation summarization | `ContextManager.summarize_and_compact()` | Inline LLM call with main/strategic LLM |
| Memory extraction | `MemoryObserver.extract_memories()` | Async background task, configurable model |
| Knowledge curation | Curator subjob (`config/experts/curator/`) | Full agent job with phases, todos, workspace |
| Memory injection assembly | `RecallStore.retrieve()` | No LLM — vector search + ranking |
| Knowledge injection | `KnowledgeStore.hybrid_search()` | No LLM — vector search + ranking |

Problems with the status quo:

1. **The curator subjob is overkill.** It runs the full agent loop — strategic/tactical phases, todo management, workspace.md, plan.md, git branching — just to read artifacts and call `kb_write` a few times. That's massive overhead for a structured extraction task.

2. **Scattered configuration.** Summarization uses the main LLM. The memory observer has `observer_model` / `observer_base_url`. The curator has its own expert config. Three different ways to configure what is conceptually the same thing: "use a support model for a background task."

3. **No reuse.** The observer's extraction prompt, the summarization call, and the curator's curation logic all follow the same pattern (system prompt + context → structured output) but share no infrastructure.

4. **Wasted model capability.** The free `gpt-oss-120b` on the university servers has good reasoning capabilities but isn't great at long-running agent jobs. The current system either uses it as a full agent (where it struggles) or doesn't use it at all. What it's good at — structured reasoning over provided context — is exactly what support tasks need.

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

The critical difference from the current curator subjob: no job creation, no workspace setup, no phase alternation, no git branching, no todo management. Just a prompt, optional tools, a tight loop with a hard cap, and a structured result.

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

## Interface

```python
class AuxiliaryLLM:
    """Unified support task execution with chain and agent modes."""

    def __init__(
        self,
        llm: BaseChatModel,          # The support model (e.g. gpt-oss-120b)
        config: Optional[dict] = None,
    ):
        self.llm = llm
        self.max_agent_iterations = config.get("max_iterations", 15) if config else 15
        self.timeout = config.get("timeout", 120) if config else 120

    async def chain(self, task: AuxTask) -> dict:
        """Single LLM call: system prompt + context → structured JSON.

        For tasks that need reasoning but no tool access.
        """
        messages = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]
        response = await asyncio.wait_for(
            self.llm.ainvoke(messages),
            timeout=self.timeout,
        )
        return task.parse_response(response.content)

    async def agent(self, task: AuxAgentTask) -> dict:
        """Short-lived agent loop: system prompt + tools → structured result.

        For tasks that need a few tool calls before producing output.
        Capped at max_iterations to prevent runaway.
        """
        tools = task.get_tools()
        llm_with_tools = self.llm.bind_tools(tools)
        messages = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]

        for _ in range(self.max_agent_iterations):
            response = await llm_with_tools.ainvoke(messages)
            messages.append(response)

            if not response.tool_calls:
                # LLM is done — parse final response
                return task.parse_response(response.content)

            # Execute tool calls
            for tool_call in response.tool_calls:
                result = await execute_tool(tools, tool_call)
                messages.append(ToolMessage(
                    content=result,
                    tool_call_id=tool_call["id"],
                ))

        # Hit iteration cap — parse whatever we have
        return task.parse_response(messages[-1].content)
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

    @abstractmethod
    def parse_response(self, raw: str) -> dict: ...


class AuxAgentTask(AuxTask):
    """Base class for agent-mode tasks (adds tool access)."""

    @abstractmethod
    def get_tools(self) -> list: ...
```

### Example: ExtractMemoriesTask (Chain Mode)

Replaces `MemoryObserver.extract_memories()`:

```python
class ExtractMemoriesTask(AuxTask):
    """Extract memories from a conversation segment."""

    def __init__(self, messages: list[BaseMessage], phase: int = 0):
        self.messages = messages
        self.phase = phase

    @property
    def system_prompt(self) -> str:
        return EXTRACTION_PROMPT  # Same prompt as current MemoryObserver

    def build_context(self) -> str:
        return format_messages_for_extraction(self.messages)

    def parse_response(self, raw: str) -> dict:
        memories = parse_json_array(raw)  # Reuse MemoryObserver._parse_extraction_response
        return {"memories": memories, "phase": self.phase}
```

### Example: CurateKnowledgeTask (Agent Mode)

Replaces the curator subjob:

```python
class CurateKnowledgeTask(AuxAgentTask):
    """Extract knowledge notes from phase artifacts."""

    def __init__(
        self,
        phase_data: str,            # Retrospective content, todo summaries
        workspace_md: str,          # Current workspace.md content
        plan_md: str,               # Current plan.md content
        existing_notes: list[str],  # Pre-fetched KB note summaries for dedup context
        kb_tools: list,             # kb_search, kb_write, kb_update, kb_read
    ):
        self.phase_data = phase_data
        self.workspace_md = workspace_md
        self.plan_md = plan_md
        self.existing_notes = existing_notes
        self._kb_tools = kb_tools

    @property
    def system_prompt(self) -> str:
        return CURATION_SYSTEM_PROMPT  # Derived from curator/instructions.md

    def build_context(self) -> str:
        parts = [
            "## Phase Artifacts",
            self.phase_data,
            "",
            "## Current Workspace",
            self.workspace_md,
            "",
            "## Current Plan",
            self.plan_md,
        ]
        if self.existing_notes:
            parts.extend([
                "",
                "## Existing Knowledge (check before writing duplicates)",
                "\n".join(self.existing_notes),
            ])
        return "\n".join(parts)

    def get_tools(self) -> list:
        return self._kb_tools

    def parse_response(self, raw: str) -> dict:
        return {"status": "completed", "summary": raw}
```

## Configuration

```yaml
# config/defaults.yaml
auxiliary:
  enabled: true
  model: null              # null = use main LLM; or "gpt-oss-120b"
  base_url: null           # null = use LLM_BASE_URL; or custom endpoint
  temperature: 0.0
  max_iterations: 15       # Cap for agent mode loops
  timeout: 120             # Seconds per LLM call

  # Task-specific overrides
  tasks:
    summarize:
      enabled: true
    extract_memories:
      enabled: true
      interval: 5          # Every N turns (replaces memory.observer_interval)
    curate_knowledge:
      enabled: true        # Replaces curator.enabled
```

This replaces:
- `memory.observer_model` / `memory.observer_base_url` → `auxiliary.model` / `auxiliary.base_url`
- `curator.enabled` / `curator.curator_config` → `auxiliary.tasks.curate_knowledge.enabled`

One model config, one class, all support tasks.

## Integration Points

### Archive Phase (replaces curator subjob spawn)

```python
# In archive_phase node (src/graph.py), replaces curation_callback:
if auxiliary_llm and config.auxiliary.tasks.curate_knowledge.enabled:
    # Pre-fetch existing KB context (so the LLM can dedup)
    existing = await knowledge_store.hybrid_search(project_id, phase_summary, match_count=20)
    existing_summaries = [f"- {n.note_id}: {n.title} ({n.note_type})" for n in existing]

    task = CurateKnowledgeTask(
        phase_data=curation_phase_data,
        workspace_md=workspace_content,
        plan_md=plan_content,
        existing_notes=existing_summaries,
        kb_tools=kb_tool_instances,
    )
    asyncio.create_task(auxiliary_llm.agent(task))
```

### Execute Node (replaces MemoryObserver trigger)

```python
# In execute node (src/graph.py), replaces memory_observer.observe():
if auxiliary_llm and should_extract_memories(state["turn_count"]):
    task = ExtractMemoriesTask(
        messages=state["messages"],
        phase=state.get("phase_number", 0),
    )
    result = await auxiliary_llm.chain(task)
    for mem in result["memories"]:
        await recall_store.store(**mem)
```

### Context Compaction (replaces inline summarization LLM call)

```python
# In ContextManager.summarize_and_compact(), optionally:
if auxiliary_llm:
    task = SummarizeTask(messages=chunk, max_length=max_summary_length)
    result = await auxiliary_llm.chain(task)
    summary = result["summary"]
```

## What Changes and What Stays

**Replaced:**
- `MemoryObserver` class → `ExtractMemoriesTask` (chain mode)
- Curator subjob (`config/experts/curator/`, `create_curation_job()`, `curation_callback`) → `CurateKnowledgeTask` (agent mode)
- `memory.observer_model` / `memory.observer_base_url` config → `auxiliary.model` / `auxiliary.base_url`
- `curator.enabled` / `curator.curator_config` config → `auxiliary.tasks.curate_knowledge.enabled`

**Stays the same:**
- `RecallStore` — storage and retrieval unchanged, just called by the task runner instead of the observer
- `KnowledgeGraphDB` / `KnowledgeStore` — unchanged, called by `CurateKnowledgeTask` via tools
- Knowledge tools (`kb_write`, `kb_search`, etc.) — unchanged, passed to agent-mode tasks
- Memory injection (`memory_injection.py`) — unchanged
- Knowledge injection (`knowledge_injection.py`) — unchanged
- Free memory sources (todo completion, compaction, phase archive, tool errors) — unchanged, these are programmatic, not LLM tasks

**Optional migration:**
- Summarization in `ContextManager` could use `auxiliary_llm.chain(SummarizeTask(...))` instead of calling the main LLM directly. Not required — the current inline approach works fine. But it would centralize all support LLM usage and let the free model handle compaction too.

## Why Two Modes

The free `gpt-oss-120b` has good reasoning capabilities but isn't great at long-running agent jobs. The two modes play to its strengths:

- **Chain mode** is pure reasoning — no tool calling complexity, no multi-turn management. The model gets context and produces structured output. This is where models like gpt-oss-120b excel.

- **Agent mode** is a short, bounded tool loop — 5-15 iterations max, focused tools, clear objective. The model doesn't need to maintain coherence over 50+ turns or manage a complex plan. It just needs to search, decide, and write. This is well within the capability of models that struggle as full agents.

The key insight: the distinction between "reasoning" and "agent" is about **loop length**, not capability. A model that fails at a 100-turn job with phase management can succeed at a 10-turn loop with 4 tools.

## Implementation Plan

### Phase 1: Core Class + Memory Extraction

1. Implement `AuxiliaryLLM` with `chain()` method
2. Implement `AuxTask` base class and `ExtractMemoriesTask`
3. Wire into execute node (replace `MemoryObserver.observe()` call)
4. Add `auxiliary` config section to `defaults.yaml`
5. Test: verify memory extraction produces same quality as current observer

### Phase 2: Agent Mode + Knowledge Curation

6. Implement `agent()` method with tool loop
7. Implement `AuxAgentTask` base class and `CurateKnowledgeTask`
8. Wire into `archive_phase` (replace `curation_callback` / curator subjob)
9. Remove curator subjob infrastructure (`create_curation_job`, `curation_callback`, curator config section)
10. Test: verify knowledge notes are created correctly after archive phases

### Phase 3: Summarization + Cleanup

11. Optionally migrate summarization to `SummarizeTask` (chain mode)
12. Remove `MemoryObserver` class (fully replaced by `ExtractMemoriesTask`)
13. Remove `memory.observer_model` / `memory.observer_base_url` config (replaced by `auxiliary.*`)
14. Remove `curator.*` config section (replaced by `auxiliary.tasks.curate_knowledge.*`)
15. Deprecate curator expert config (`config/experts/curator/`)

## Open Questions

1. **Summarization migration** — should `ContextManager.summarize_and_compact()` use the auxiliary LLM, or keep using the main LLM? Using the auxiliary model saves cost but may reduce summary quality. The main LLM is already "paid for" in the conversation. Recommendation: make it optional, default to main LLM, allow `auxiliary.tasks.summarize.use_auxiliary: true` override.

2. **Agent mode error handling** — if a tool call fails mid-loop (e.g., `kb_write` hits a Neo4j error), should the loop retry, skip, or abort? Recommendation: skip the failed call (append error as ToolMessage), let the LLM decide how to proceed. Abort only on iteration cap.

3. **Concurrency** — the current memory observer and curation callback both run as `asyncio.create_task()` (non-blocking). The auxiliary LLM should maintain this pattern. But if both a memory extraction and a knowledge curation task fire at the same time (e.g., on archive phase), they'll compete for the same LLM endpoint. Recommendation: sequential execution with a simple queue, or accept the concurrency if the model endpoint can handle it.

4. **Structured output vs. free-form** — should chain mode use the LLM's structured output / JSON mode if available? This would eliminate parsing failures but not all models support it. Recommendation: use JSON mode when available (`response_format: {type: "json_object"}`), fall back to prompt-based JSON extraction.

## References

- [[memory_light]] — Current memory extraction architecture (observer, free sources, RRF search)
- [[project_knowledge_base]] — Knowledge base architecture (Neo4j, pgvector, curator subjob, tools)
- [[context_management]] — Summarization and compaction
