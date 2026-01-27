# Context Management Research

Research on conversation summarization thresholds and tool result redaction strategies from major AI providers, coding tools, and academic research.

## Summarization Thresholds

### Industry Consensus: Trigger at 70-80% of Context Window

| Provider / Tool              | Trigger Threshold                              | Recent Messages Kept          |
|------------------------------|------------------------------------------------|-------------------------------|
| **Anthropic (Claude Code)**  | Auto at ~95%, recommends manual at **70%**     | 5 files + compressed summary  |
| **Anthropic Claude SDK**     | Default **100,000 tokens** (configurable 50k-150k) | --                        |
| **OpenAI Cookbook**           | Every **3-5 turns** or on context shift        | Last 2-6 turns                |
| **Google Gemini CLI**        | **70%** of model limit                         | Structured XML snapshot       |
| **Goose (Block)**            | **80%** of token limit                         | --                            |
| **Forge Code**               | **80,000 tokens**                              | Configurable retention window |
| **Microsoft guidance**       | Every **5-10 turns**                           | Last 3-6 turns sliding window |

### Token-Based Thresholds

The consensus is **70-80%** of the model's context window. For a 128K model, that translates to roughly 90k-100k tokens. Most implementations agree that 95% is too late -- performance degrades noticeably at that point.

### Message-Count Thresholds

Most recommendations suggest **20-60 messages**, not 150+. Message-count triggers serve as a complement to token-based thresholds, catching cases where many short messages accumulate without hitting the token limit.

### Recent Messages to Preserve

| Source                                | Recent Messages Kept |
|---------------------------------------|----------------------|
| University of Washington NLP group    | 12 messages          |
| Strands Agents (default)              | 10 messages          |
| General practitioner consensus        | 8-12 messages        |
| Microsoft guidance                    | Last 3-6 turns       |
| OpenAI Cookbook                        | Last 2 turns         |

### What to Preserve in Summaries

Anthropic's guidance recommends preserving:
- Architectural decisions
- Unresolved bugs
- Implementation details
- Current plan/goals

While discarding:
- Redundant tool outputs
- Verbose intermediate messages
- Repeated information

### Summarization Strategy Comparison

| Strategy                        | Token Reduction | Information Retention       | Latency Impact |
|---------------------------------|-----------------|-----------------------------|----------------|
| Simple truncation               | Variable        | Poor (abrupt forgetting)    | None           |
| Rolling summarization           | 60-70%          | Good                        | Moderate       |
| Multi-level summarization       | 68%             | 91% of critical info        | Moderate       |
| Structured state extraction     | Very high       | Excellent for facts         | High           |
| Anchored/incremental summarization | Good         | Good                        | Low            |

### Risks of Summarization

- **Contextual drift**: Over multiple rounds of summarization, understanding gradually shifts from original meaning.
- **Context poisoning**: If a bad fact enters a summary, it persists and corrupts future responses.
- **Lost-in-the-middle effect**: Even with large context windows, LLMs miss information buried in the middle of long contexts.
- **Linear cost growth**: Naive summarization grows in cost linearly with conversation length. Incremental/anchored approaches avoid this.

---

## Tool Result Redaction

### Anthropic -- Server-Side Tool Result Clearing

Anthropic offers the most explicit API for this (`clear_tool_uses_20250919` beta). Key parameters:

- **`trigger`**: Token threshold to start clearing (default: 100k input tokens).
- **`keep`**: Number of recent tool use/result pairs to preserve (default: **3**).
- **`exclude_tools`**: Tool names whose results should never be cleared (e.g., web search).
- **`clear_tool_inputs`**: Optionally clear the call parameters too, not just results.
- **`clear_at_least`**: Minimum tokens to clear per activation (prevents trivial cache-breaking clears).

Cleared results are replaced with placeholder text server-side. The client keeps the full history untouched.

Core principle: *"Every token added to the context window competes for the model's attention. Signal gets drowned by accumulation."*

When context approaches the clearing threshold, Claude receives a warning and can proactively save important tool results to persistent memory files before they are cleared.

### OpenAI -- Tool Output Truncation

OpenAI's Agents SDK `LLMSummarizer` has a `tool_trim_limit` parameter (default: **600 characters**) that truncates verbose tool outputs. Their turn-based trimming drops entire turns (user message + assistant + tool calls) from the oldest end.

The newer stateful Responses API handles conversation state server-side with automatic pruning when the token window grows large.

### Google / Gemini

Google's approach leans toward using large context windows (1-2M tokens) and external memory (Agent Engine sessions) rather than providing explicit tool result trimming APIs. When summarization is needed, Gemini CLI uses structured XML snapshots preserving goal, key knowledge, file system state, and current plan.

### JetBrains Research (NeurIPS 2025) -- Observation Masking

The most rigorous comparative study. Two approaches tested:

| Strategy                | Effect                                                      |
|-------------------------|-------------------------------------------------------------|
| **Observation masking** | +2.6% solve rate, **52% cheaper**                           |
| **LLM summarization**  | Agents ran **13-15% longer**, summary cost 7%+ of budget    |

Key finding: A typical agent turn is dominated by tool output (observation), making it most efficient to reduce resolution of this specific element rather than all elements. This is what **Cursor** and **Warp** use in production.

### Manus AI -- Three-Tier Priority

Manus uses a strict hierarchy: **raw > compaction > summarization**.

1. **Keep raw** -- most recent tool calls stay untouched.
2. **Compaction** (reversible) -- replace old results with compact references (e.g., file path instead of content). Agent can re-fetch if needed. Typically compact the **oldest 50%**.
3. **Summarization** (lossy, last resort) -- only when compaction doesn't free enough space. Uses structured JSON schemas rather than free-form summaries.

Additional insights from Manus:
- **KV-cache hit rate** is the single most important cost metric. Cached tokens cost **10x less** on Claude Sonnet (0.30 USD/MTok vs 3 USD/MTok).
- Append-only contexts, deterministic serialization, and session routing maximize cache hits.
- **File system as infinite memory**: Agents write intermediate results to files and load only summaries into context.

### What Coding Tools Do in Practice

| Tool           | Strategy                                      |
|----------------|-----------------------------------------------|
| **Claude Code** | Auto-compact at 75-95%, summarize full trajectory, keep recent tool results |
| **Cursor**      | Observation masking (no LLM summarization)   |
| **Warp**        | Observation masking (proprietary)            |
| **OpenHands**   | Pioneered observation masking, also supports LLM summarization |
| **Forge Code**  | Automatic context compaction with summary    |

---

## Comparison with Our Implementation

### Current Settings

```json
{
  "limits": {
    "context_threshold_tokens": 60000,
    "message_count_threshold": 150,
    "message_count_min_tokens": 25000
  },
  "context_management": {
    "keep_recent_tool_results": 10,
    "keep_recent_messages": 10,
    "max_tool_result_length": 5000
  }
}
```

### Gap Analysis

| Aspect                          | Our System        | Industry Practice                     |
|---------------------------------|-------------------|---------------------------------------|
| Token threshold                 | 60,000            | 70-80% of window (90k-100k for 128K) |
| Message count threshold         | 150               | 20-60 messages                        |
| Recent results kept in full     | 10                | Anthropic default: 3, range: 3-10    |
| Tool result truncation limit    | 5,000 chars       | OpenAI default: 600 chars            |
| Exclude specific tools          | Not supported     | Anthropic supports this              |
| Reversible compaction (re-fetch)| Not supported     | Manus uses this                      |
| Recent messages kept            | 10                | 8-12 (in range)                      |

### Recommended Changes

**Summarization thresholds** (for 128K context model):
```json
{
  "limits": {
    "context_threshold_tokens": 90000,
    "message_count_threshold": 50,
    "message_count_min_tokens": 20000
  }
}
```

- Raise token threshold to ~70% of 128K so compaction fires at a meaningful point.
- Lower message count threshold to 50 so it actually triggers in practice.
- Lower min-tokens guard to 20k so the message-count path isn't blocked by a secondary gate.

**Tool result redaction**:
- Lower `keep_recent_tool_results` from 10 to **5**. Anthropic defaults to 3 and research shows observation masking works well with fewer retained results.
- Lower `max_tool_result_length` from 5,000 to **1,000-2,000 chars**. Current 5k is generous compared to OpenAI's 600 default. Tool results are the biggest context consumers.
- Add an `exclude_tools` list for high-value tools like `get_database_schema` or `web_search` whose results remain useful longer.
- Consider reversible compaction: store a reference the agent can re-fetch instead of a static placeholder, making aggressive clearing less risky.

---

## Sources

### Anthropic
- [Context Editing - Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Automatic Context Compaction Cookbook](https://platform.claude.com/cookbook/tool-use-automatic-context-compaction)
- [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [How Claude Code Got Better by Protecting More Context](https://hyperdev.matsuoka.com/p/how-claude-code-got-better-by-protecting)

### OpenAI
- [Agents SDK Session Memory Cookbook](https://cookbook.openai.com/examples/agents_sdk/session_memory)
- [Conversation State Guide](https://platform.openai.com/docs/guides/conversation-state)
- [Context Summarization with Realtime API](https://cookbook.openai.com/examples/context_summarization_with_realtime_api)

### Google
- [Gemini Long Context](https://ai.google.dev/gemini-api/docs/long-context)
- [Context Engineering in Gemini CLI](https://aipositive.substack.com/p/a-look-at-context-engineering-in)

### Research and Industry
- [JetBrains Research: Cutting Through the Noise (NeurIPS 2025)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Manus: Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Context Engineering for AI Agents Part 2 (Phil Schmid)](https://www.philschmid.de/context-engineering-part-2)
- [Mem0: LLM Chat History Summarization Guide 2025](https://mem0.ai/blog/llm-chat-history-summarization-guide-2025)

### Frameworks
- [LangGraph: Managing Conversation History](https://langchain-ai.github.io/langgraph/how-tos/create-react-agent-manage-message-history/)
- [LangChain Context Engineering Docs](https://docs.langchain.com/oss/python/langchain/context-engineering)
- [LangMem Summarization Guide](https://langchain-ai.github.io/langmem/guides/summarization/)

### Tools
- [Goose Smart Context Management](https://block.github.io/goose/docs/guides/sessions/smart-context-management/)
- [Forge Code Context Compaction](https://forgecode.dev/docs/context-compaction/)
- [Context Compaction Research Gist](https://gist.github.com/martinec/0d078c88b0bdc97fea21fc6d7d596af8)
- [Microsoft: Managing Chat History for LLMs](https://devblogs.microsoft.com/semantic-kernel/managing-chat-history-for-large-language-models-llms/)
