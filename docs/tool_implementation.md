# Tool Description System

This document describes how tool descriptions are managed and provided to agents in the Universal Agent system.

## Overview

Agents have access to 20-40 tools depending on their configuration. Each tool has documentation that helps the agent understand how to use it. The system supports two mechanisms for providing tool descriptions:

1. **LangChain Binding** - Tool docstrings are automatically included in every LLM request via `bind_tools()`
2. **Workspace Documentation** - Detailed markdown files generated in the agent's workspace for on-demand reference

## Token Usage

Tool descriptions consume context tokens on every LLM request:

| Agent | Tools | Docstring Tokens | Schema Overhead | Total per Request |
|-------|-------|-----------------|-----------------|-------------------|
| Creator | 27 | ~2,000 | ~4,600 | **~6,600 tokens** |
| Validator | 40 | ~2,700 | ~6,800 | **~9,500 tokens** |

For a job with 84 iterations, this means ~550,000 tokens spent just on tool definitions.

## Workspace Documentation

### Generated Files

When a job starts, the workspace is populated with a `tools/` directory containing:

```
tools/
├── README.md                      # Index of all available tools
├── read_file.md                   # Detailed doc for each tool
├── write_file.md
├── write_requirement_to_cache.md
└── ... (one file per tool)
```

### README.md Structure

The index groups tools by category with quick reference:

```markdown
# Available Tools

## Quick Reference

### Workspace Tools
- **[read_file](read_file.md)** - Read content from a file in the workspace
- **[write_file](write_file.md)** - Write content to a file in the workspace
...

### Todo Tools
- **[add_todo](add_todo.md)** - Add a task to the todo list
...

### Domain Tools
- **[extract_document_text](extract_document_text.md)** - Extract text from documents
...
```

### Individual Tool Docs

Each tool file contains:

```markdown
# write_requirement_to_cache

**Category:** domain

Write a requirement to the PostgreSQL cache for validation.

This is how you submit completed requirements for the Validator Agent to process.
The requirement will be validated and integrated into the knowledge graph.

**Arguments:**
- `text` (str): Full requirement text
- `name` (str): Short name/title (max 80 chars)
- `req_type` (str, optional): Type - 'functional', 'compliance', 'constraint', or 'non_functional'
- `priority` (str, optional): Priority - 'high', 'medium', or 'low'
...

**Returns:** Requirement ID and confirmation

**Example:**
\`\`\`
write_requirement_to_cache(
    text="The system must retain all invoice records for at least 10 years...",
    name="GoBD Invoice Retention",
    req_type="compliance",
    priority="high",
    gobd_relevant=True
)
\`\`\`
```

## Implementation

### Key Files

| File | Purpose |
|------|---------|
| `src/agents/shared/tools/description_generator.py` | Generates markdown documentation |
| `src/agents/universal/agent.py` | Calls generator during workspace setup |
| `config/agents/creator.json` | Includes `tools/` in workspace structure |
| `config/agents/validator.json` | Includes `tools/` in workspace structure |

### Generator Functions

```python
from src.agents.shared.tools import (
    generate_workspace_tool_docs,  # Main entry point
    generate_tool_description,      # Single tool doc
    generate_tool_index,            # README.md content
)

# Generate all docs for a list of tools
generate_workspace_tool_docs(tool_names, output_dir)
```

### Integration Point

In `UniversalAgent._setup_job_workspace()`:

```python
# Generate tool documentation in workspace
tool_names = get_all_tool_names(self.config)
tools_dir = self._workspace_manager.get_path("tools")
generate_workspace_tool_docs(tool_names, tools_dir)
```

## Agent Usage

The agent can read tool documentation on demand:

```python
# Read the tool index
read_file("tools/README.md")

# Get detailed info about a specific tool
read_file("tools/write_requirement_to_cache.md")
```

## Future: On-Demand Loading

### Problem Statement

Tool descriptions consume significant context tokens on every LLM request. This creates several issues:

| Issue | Impact |
|-------|--------|
| **Token overhead** | 27 tools × ~250 tokens each = ~6,600 tokens per request |
| **Cumulative cost** | 84 iterations × 6,600 tokens = ~550,000 tokens per job just for tool definitions |
| **Scaling concerns** | Adding more tools (graph tools, new domain tools) will push this toward 10-15k tokens |
| **Context pressure** | Tool definitions compete with actual work content for the ~128k context window |
| **Redundancy** | Same definitions sent on every request, even when tools aren't needed |

At scale (50+ tools with detailed descriptions), this could reach 20-50k tokens per request—a significant portion of the context window before any work begins.

### Current State

The current implementation is **redundant** - tool descriptions are both:
1. Included via LangChain's `bind_tools()` on every request (~6,600 tokens)
2. Available as workspace files for reference (not yet utilized)

### Solution Direction

**On-demand tool loading**: Only include minimal tool information in the base prompt, and load detailed descriptions when the agent actually needs them.

### Research: Identified Approaches

We searched the web for established patterns to solve this problem. The following approaches were identified:

---

#### Approach 1: Anthropic Tool Search Tool (defer_loading)

**Source:** [Claude Tool Search Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)

**How it works:**
- Mark tools with `defer_loading: true` in the API request
- Deferred tools are excluded from initial context entirely
- Include a special "tool search tool" that Claude can invoke
- When Claude needs capabilities, it searches (regex or BM25)
- API returns 3-5 matching `tool_reference` blocks
- References are automatically expanded to full definitions

**Example:**
```python
tools=[
    {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
    {
        "name": "write_requirement_to_cache",
        "description": "Submit requirement for validation",
        "input_schema": {...},
        "defer_loading": True  # Excluded from initial context
    }
]
```

**Token savings reported:**
> "Consider a five-server setup with 58 tools consuming approximately 55K tokens before the conversation even starts... At Anthropic, they've seen tool definitions consume 134K tokens before optimization."

**Pros:**
- Server-side implementation - minimal client changes
- Automatic tool expansion handled by API
- Works with prompt caching
- Proven at scale (100+ tools)

**Cons:**
- Beta feature (header: `advanced-tool-use-2025-11-20`)
- Only Claude Sonnet 4.5+ and Opus 4.5+
- Requires direct Anthropic API (unclear LangChain support)
- Vendor lock-in to Anthropic

**LangChain integration note:**
> "The LangChain `extras` parameter supports: `defer_loading` (bool) for loading tools on-demand"

---

#### Approach 2: LangGraph Dynamic Tool Selection (Vector Store)

**Source:** [LangGraph - How to Handle Large Numbers of Tools](https://langchain-ai.github.io/langgraph/how-tos/many-tools/)

**How it works:**
- Index tool descriptions in a vector store (embeddings)
- Add a `select_tools` node before the agent node in the graph
- Query vector store based on user message/context
- Bind only top-k relevant tools to the LLM for that call

**Example:**
```python
# Index tools
tool_documents = [
    Document(page_content=tool.description, metadata={"tool_name": tool.name})
    for tool in all_tools
]
vector_store = InMemoryVectorStore(embedding=OpenAIEmbeddings())
vector_store.add_documents(tool_documents)

# Select tools node
def select_tools(state: State):
    query = state["messages"][-1].content
    docs = vector_store.similarity_search(query, k=5)
    return {"selected_tools": [doc.metadata["tool_name"] for doc in docs]}

# Agent node - bind only selected tools
def agent(state: State):
    selected = [tool_registry[name] for name in state["selected_tools"]]
    llm_with_tools = llm.bind_tools(selected)
    return {"messages": [llm_with_tools.invoke(state["messages"])]}
```

**Pros:**
- Works with any LLM (OpenAI, local models, Anthropic, etc.)
- Full control over selection logic
- Can use semantic search, grouping, or hybrid approaches
- No vendor-specific features required
- Integrates with existing LangGraph workflow

**Cons:**
- Requires embedding model (additional API calls or local model)
- Selection might miss relevant tools (accuracy depends on embeddings)
- More complex graph structure
- Need to handle re-selection when tools fail

**Alternatives mentioned:**
> "Grouping tools and retrieve over groups" or "use a chat model to select tools or groups of tools"

---

#### Approach 3: Workspace-Based On-Demand Loading (Our Design)

**How it works:**
- Bind tools with minimal descriptions (name + one-line summary)
- Full documentation lives in `workspace/tools/<name>.md`
- Agent reads documentation file when it needs detailed usage info
- System prompt instructs: "For detailed tool usage, read `tools/<tool_name>.md`"

**Example minimal binding:**
```python
# Instead of full docstrings, use abbreviated descriptions
minimal_tools = [
    create_minimal_tool("write_requirement_to_cache", "Submit requirement to validation pipeline"),
    create_minimal_tool("cite_document", "Create verified citation for document"),
    # ...
]
llm_with_tools = llm.bind_tools(minimal_tools)
```

**Pros:**
- Simple implementation - no vector store needed
- Agent has full control over when to load docs
- Documentation already generated in workspace
- Works with any LLM
- No additional API calls

**Cons:**
- Agent must learn to read docs before using unfamiliar tools
- Extra tool calls to read documentation files
- May slow down execution if agent reads docs frequently
- Relies on agent following instructions

---

#### Approach 4: Prompt Caching (Cost Reduction, Not Token Reduction)

**Source:** [Claude Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

**How it works:**
- Mark static content (tool definitions, system prompt) with `cache_control`
- Cached content is reused across requests
- Reduces cost and latency, but tokens still count against context

**Example:**
```python
tools = [
    {
        "name": "my_tool",
        "description": "...",
        "cache_control": {"type": "ephemeral"}
    }
]
```

**Savings reported:**
> "Reducing costs by up to 90% and latency by up to 85% for long prompts"

**Pros:**
- Easy to implement (just add cache_control)
- Significant cost savings
- Reduces latency

**Cons:**
- Tokens still consume context window
- Anthropic-specific feature
- Doesn't solve the core scaling problem

---

#### Approach 5: Token-Efficient Tools Beta

**Source:** [Anthropic Token-Saving Updates](https://claude.com/blog/token-saving-updates)

**How it works:**
- Beta header enables more compact tool call output format
- Reduces output token consumption by up to 70%

**Header:** `token-efficient-tools-2025-02-19`

**Pros:**
- Simple to enable
- No changes to tool definitions

**Cons:**
- Only reduces output tokens, not input
- Anthropic-specific
- Beta feature

---

### Comparison Matrix

| Approach | LLM Agnostic | Complexity | Token Savings | Maturity |
|----------|--------------|------------|---------------|----------|
| Anthropic defer_loading | No (Claude only) | Low | High (~90%) | Beta |
| LangGraph Vector Store | Yes | Medium | High (~80%) | Stable |
| Workspace Docs | Yes | Low | Medium (~50%) | Custom |
| Prompt Caching | No (Claude only) | Low | Cost only | Stable |
| Token-Efficient Beta | No (Claude only) | Low | Output only | Beta |

---

### Conceptual: Hybrid Short/Long Description System

> **Status:** Experimental concept - saved for future implementation

This approach combines aspects of workspace-based loading with configurable description levels, tailored to our infrastructure.

#### Core Concept

Each tool has two description levels:

| Level | Purpose | Location | Token Cost |
|-------|---------|----------|------------|
| **Short** | "When would I use this?" | Bound via `bind_tools()` | ~20-30 tokens |
| **Long** | Full docs with parameters, examples, edge cases | `workspace/tools/<name>.md` | ~200-500 tokens |

#### Behavior

1. **Default tools** (majority): Agent receives only short description in context
   - System prompt instructs: "Before using an unfamiliar tool, read its documentation from `tools/<tool_name>.md`"
   - Agent must explicitly read the long description before first use

2. **Core tools** (frequently used): Agent receives full long description in context
   - No need to read workspace docs
   - Examples: `read_file`, `write_file`, `add_todo`, `complete_todo`, `list_files`

#### Configuration

Tools would be configured in the registry with a `description_mode` flag:

```python
TOOL_REGISTRY = {
    "read_file": {
        "category": "workspace",
        "factory": create_workspace_tools,
        "description_mode": "full",  # Always include long description
    },
    "write_requirement_to_cache": {
        "category": "domain",
        "factory": create_cache_tools,
        "description_mode": "deferred",  # Short description only, read docs before use
    },
}
```

#### Example Descriptions

**Short description (bound to LLM):**
```
write_requirement_to_cache: Submit a requirement to the validation pipeline
```

**Long description (in workspace):**
```markdown
# write_requirement_to_cache

Write a requirement to the PostgreSQL cache for validation.

This is how you submit completed requirements for the Validator Agent to process.
The requirement will be validated and integrated into the knowledge graph.

**Arguments:**
- `text` (str): Full requirement text
- `name` (str): Short name/title (max 80 chars)
- `req_type` (str, optional): Type - 'functional', 'compliance', 'constraint', or 'non_functional'
- `priority` (str, optional): Priority - 'high', 'medium', or 'low'
- `gobd_relevant` (bool, optional): Whether this is GoBD-relevant

**Returns:** Requirement ID and confirmation

**Example:**
write_requirement_to_cache(
    text="The system must retain all invoice records for at least 10 years...",
    name="GoBD Invoice Retention",
    req_type="compliance",
    priority="high",
    gobd_relevant=True
)
```

#### Token Savings Estimate

| Scenario | Current | With Hybrid | Savings |
|----------|---------|-------------|---------|
| Creator (27 tools) | ~6,600 | ~2,500 | ~62% |
| Validator (40 tools) | ~9,500 | ~3,200 | ~66% |

Assumptions:
- 8-10 core tools with full descriptions (~200 tokens each)
- Remaining tools with short descriptions (~25 tokens each)

#### Implementation Steps

1. Add `short_description` field to tool definitions
2. Add `description_mode` to `TOOL_REGISTRY` entries
3. Modify `bind_tools()` call to use short descriptions for deferred tools
4. Update system prompt with instruction to read docs before using unfamiliar tools
5. Define list of "core tools" that always get full descriptions

#### Trade-offs

**Pros:**
- Simple to implement with existing infrastructure
- No external dependencies (embeddings, vector store)
- Agent retains full control
- Configurable per-tool

**Cons:**
- Extra `read_file` calls for first-time tool usage
- Relies on agent following instructions
- May slow initial tool usage

#### Why Not Implemented Yet

Current token overhead (~6,600) is acceptable for now. This concept is saved for when:
- Tool count grows beyond 40-50
- Descriptions become more detailed (500+ tokens each)
- Context pressure becomes a bottleneck

---

### Open Questions

1. **LLM compatibility**: We use `gpt-oss-120b` - which approaches work with OpenAI-compatible APIs?
2. **LangChain support**: Does LangChain's `bind_tools` support `defer_loading` for non-Anthropic models?
3. **Hybrid approach**: Can we combine workspace docs with vector-based selection?
4. **Accuracy trade-off**: How much tool selection accuracy do we lose with dynamic selection?
5. **Embedding costs**: Is the overhead of embedding queries worth the token savings?

### Next Steps

- [ ] Decide on primary approach based on LLM compatibility requirements
- [ ] Prototype selected approach
- [ ] Measure actual token savings vs. baseline
- [ ] Evaluate tool selection accuracy (if using dynamic selection)

## Tool Categories

### Workspace Tools
File operations for the agent's isolated workspace.

| Tool | Description |
|------|-------------|
| `read_file` | Read file content |
| `write_file` | Write/overwrite file |
| `append_file` | Append to file |
| `list_files` | List directory contents |
| `delete_file` | Delete file/empty directory |
| `search_files` | Search for text in files |
| `file_exists` | Check if path exists |
| `get_workspace_summary` | Get workspace statistics |

### Todo Tools
Task management with archiving support.

| Tool | Description |
|------|-------------|
| `add_todo` | Add task to list |
| `complete_todo` | Mark task complete |
| `start_todo` | Mark task in-progress |
| `list_todos` | List all todos |
| `get_progress` | Get completion statistics |
| `archive_and_reset` | Archive and clear todos |
| `get_next_todo` | Get highest priority pending task |

### Document Tools (Creator)
Document processing and analysis.

| Tool | Description |
|------|-------------|
| `extract_document_text` | Extract text from PDF/DOCX/TXT/HTML |
| `chunk_document` | Split document into chunks |
| `identify_requirement_candidates` | Find requirement-like statements |
| `assess_gobd_relevance` | Check GoBD compliance relevance |
| `extract_entity_mentions` | Find entity references |

### Search Tools
Web and graph search capabilities.

| Tool | Description |
|------|-------------|
| `web_search` | Search web via Tavily |
| `query_similar_requirements` | Find similar requirements in graph |

### Citation Tools
Source tracking and citation management.

| Tool | Description |
|------|-------------|
| `cite_document` | Create document citation |
| `cite_web` | Create web citation |
| `list_sources` | List registered sources |
| `get_citation` | Get citation details |

### Cache Tools (Creator)
Requirement submission to validation pipeline.

| Tool | Description |
|------|-------------|
| `write_requirement_to_cache` | Submit requirement for validation |

### Graph Tools (Validator)
Neo4j knowledge graph operations.

| Tool | Description |
|------|-------------|
| `execute_cypher_query` | Run Cypher query |
| `get_database_schema` | Get graph schema |
| `find_similar_requirements` | Find similar requirements |
| `check_for_duplicates` | Check for duplicates (95% threshold) |
| `resolve_business_object` | Match mention to BusinessObject |
| `resolve_message` | Match mention to Message |
| `validate_schema_compliance` | Run metamodel checks |
| `create_requirement_node` | Create Requirement node |
| `create_fulfillment_relationship` | Create fulfillment relationship |
| `generate_requirement_id` | Generate new R-XXXX ID |
| `get_entity_relationships` | Get entity relationships |
| `count_graph_statistics` | Get graph statistics |

### Completion Tools
Job completion signaling.

| Tool | Description |
|------|-------------|
| `mark_complete` | Signal task completion |
