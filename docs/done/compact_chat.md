# Agent Context Management - Research & Implementation Plan

## Problem Statement

The LangGraph agent crashed after 29 steps with `ValueError: No generations found in stream`. This occurred because the conversation context grew too large, causing the LLM to return an empty response.

**Error log:**
```
2025-12-29 22:56:50 - backend.services.agent - ERROR - Error in astream_full: No generations found in stream.
...
ValueError: No generations found in stream.
```

## Current Architecture

- **Model**: 128k context window
- **Agent**: LangGraph with 7 tools (Neo4j knowledge base + Tavily web search)
- **No context management**: Messages accumulate indefinitely until overflow

---

## Research: How Major AI Companies Handle This

### OpenAI Agents SDK

Uses **trimming + compression**:
- Session-based memory with automatic conversation history
- Context summarization: "compresses prior messages into structured, shorter summaries"
- In 100-turn tests: **84% token reduction** while maintaining coherence

Source: [OpenAI Agents SDK Session Memory](https://cookbook.openai.com/examples/agents_sdk/session_memory)

### Anthropic Claude

Uses **compaction + tool result clearing**:
- **Compaction**: When nearing context limit, summarize and restart with the summary
- **Tool Result Clearing**: "Once a tool has been called deep in the message history, the agent doesn't need to see the raw result again" - called "one of the safest, lightest touch forms of compaction"
- **Structured note-taking**: Agent writes notes to external files (like NOTES.md) for persistent memory outside context

Source: [Anthropic Context Engineering for Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### LangGraph/LangChain (LangMem)

Built-in summarization support:
- Triggers when messages exceed token threshold
- Creates running summary updated incrementally
- Separate state keys for full history vs. summarized context

Source: [LangMem Summarization Guide](https://langchain-ai.github.io/langmem/guides/summarization/)

---

## Available Strategies

### 1. Tool Result Clearing (Safest, Low Effort)

**What**: Strip tool call results from older messages in history.

**Pros**:
- Minimal information loss (agent already processed the results)
- Easy to implement
- No external dependencies

**Cons**:
- Limited savings (only helps with tool-heavy conversations)

**Implementation**: Modify message history to replace old `ToolMessage` content with a placeholder like `[Result processed]`.

---

### 2. Message Summarization (Recommended)

**What**: When approaching token limit (~80-100k), summarize older messages and continue with summary + recent messages.

**Pros**:
- Preserves important context
- Proven approach (used by OpenAI, Anthropic)
- LangMem provides ready-made implementation

**Cons**:
- Requires additional LLM call for summarization
- Adds latency when summarization triggers
- Needs token counting

**Implementation Options**:

#### Option A: LangMem Integration
```python
from langmem import summarize_messages

# In agent's call_model:
summarized = await summarize_messages(
    messages,
    max_tokens=80000,
    max_summary_tokens=2000,
    running_summary=state.get("summary")
)
```

#### Option B: Custom Summarization
```python
async def maybe_summarize(messages: list, max_tokens: int = 80000) -> list:
    token_count = estimate_tokens(messages)
    if token_count < max_tokens:
        return messages

    # Summarize older messages
    summary = await llm.ainvoke([
        SystemMessage(content="Summarize this conversation concisely..."),
        *messages[:-5]  # Keep last 5 messages intact
    ])

    return [
        SystemMessage(content=f"Previous conversation summary: {summary.content}"),
        *messages[-5:]
    ]
```

---

### 3. Message Trimming (Simple but Lossy)

**What**: Keep only the last N messages.

**Pros**:
- Simple to implement
- No LLM calls needed
- Predictable context size

**Cons**:
- Loses important early context
- May confuse agent about earlier decisions

**Implementation**:
```python
def trim_messages(messages: list, max_messages: int = 20) -> list:
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]
```

---

### 4. Recursion Limit (Safety Net)

**What**: Maximum number of agent iterations before forced termination.

**Pros**:
- Prevents infinite loops
- Catches bugs and adversarial inputs
- Simple to implement

**Cons**:
- Doesn't solve context overflow (just prevents it from getting worse)
- May terminate valid long-running queries

**Implementation**:
```python
# In graph invocation
config = {"recursion_limit": 25}
async for event in self.graph.astream_events(initial_state, config=config, version="v2"):
    ...
```

---

## Proposed Implementation Plan

### Phase 1: Quick Wins (Immediate)
1. Add recursion limit (25 steps) as safety net
2. Implement tool result clearing for messages older than N turns

### Phase 2: Summarization (Next)
1. Add token counting utility
2. Implement summarization trigger at ~80k tokens
3. Store running summary in agent state
4. Test with long conversations

### Phase 3: Optimization (Future)
1. Fine-tune summarization prompts
2. Consider sub-agent architecture for complex queries
3. Add metrics/logging for context usage

---

## Decision Points

- [ ] Use LangMem library or custom implementation?
- [ ] Token threshold for summarization trigger (80k? 100k?)
- [ ] How many recent messages to keep unsummarized?
- [ ] Should summarization use same LLM or smaller/faster model?
- [ ] Store summary in state or external file?

---

## Dependencies to Add

```txt
# For LangMem approach
langmem>=0.1.0

# For token counting (if not using LangMem)
tiktoken>=0.5.0
```

---

## References

- [LangMem Summarization Guide](https://langchain-ai.github.io/langmem/guides/summarization/)
- [OpenAI Agents SDK Session Memory](https://cookbook.openai.com/examples/agents_sdk/session_memory)
- [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [LangGraph Memory Documentation](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [Mem0 - Universal Memory Layer](https://github.com/mem0ai/mem0)