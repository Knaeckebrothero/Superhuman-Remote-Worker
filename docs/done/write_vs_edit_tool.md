# Agent Memory and File Write Safety

## Problem: Blind Overwrites

The current `write_file()` tool simply overwrites files without any safety checks:

```python
def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace.
    Overwrites the file if it already exists.
    """
    result_path = workspace.write_file(path, content)
    return f"Written: {path} ({size:,} bytes)"
```

This is dangerous for persistent files like `workspace.md` because the agent can accidentally overwrite all previous content.

## Why "Read Before Write" Doesn't Work Here

Claude Code enforces a "read before write" rule - you must read a file before editing it. However, this approach has a fundamental problem in our architecture:

**Context compaction removes tool results.** The `ContextManager` uses `trim_messages` to keep context within limits. When compaction triggers, older messages (including `read_file` results) are discarded.

```
┌─────────────────────────────────────────────────────────┐
│  The Problem                                             │
│                                                          │
│  Agent: write_file("workspace.md", "new stuff")          │
│  Tool:  "Error: read the file first"                     │
│  Agent: read_file("workspace.md")                        │
│  Tool:  "# Workspace Context\n## Current State\n..."     │
│  Agent: write_file("workspace.md", old + new)            │
│         ↑ Agent must hold entire file in context         │
│         ↑ If compaction happens, old content is gone     │
│         ↑ Even without compaction, agent may forget      │
│           to include the old content                     │
└─────────────────────────────────────────────────────────┘
```

The agent must:
1. Read the file
2. Hold the entire content in working memory
3. Manually merge old + new content
4. Write the combined result

This is error-prone and fails if context is compacted between steps.

## Solution: Atomic Edit Tools

Instead of relying on the agent to do read-modify-write correctly, provide atomic tools that handle the cycle internally:

```
┌─────────────────────────────────────────────────────────┐
│  Atomic Edit Tools                                       │
│                                                          │
│  edit_file(path, old_string, new_string)                 │
│  → Tool reads file internally                            │
│  → Tool does replacement                                 │
│  → Tool writes result                                    │
│  → Agent only needs to know WHAT to change               │
│                                                          │
│  append_to_file(path, content)                           │
│  → Tool reads file internally                            │
│  → Tool appends new content                              │
│  → Agent never sees original content                     │
└─────────────────────────────────────────────────────────┘
```

### Proposed Tools

#### 1. `edit_file(path, old_string, new_string)`

Like Claude Code's Edit tool. Performs targeted search/replace:

```python
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string in a file.

    The old_string must be unique in the file (or use replace_all=True).
    This is safer than write_file because it only changes what you specify.

    Args:
        path: File path in workspace
        old_string: Exact text to find and replace
        new_string: Text to replace it with

    Returns:
        Confirmation message
    """
    content = workspace.read_file(path)

    if old_string not in content:
        return f"Error: old_string not found in {path}"

    if content.count(old_string) > 1:
        return f"Error: old_string appears multiple times in {path}. Be more specific."

    new_content = content.replace(old_string, new_string, 1)
    workspace.write_file(path, new_content)

    return f"Edited {path}: replaced {len(old_string)} chars with {len(new_string)} chars"
```

Usage:
```
edit_file("workspace.md", "Status: Initializing", "Status: Phase 2 complete")
```

#### 2. `append_to_file(path, content)`

For adding new content without reading the file:

```python
def append_to_file(path: str, content: str) -> str:
    """Append content to the end of a file.

    Use this to add new sections, notes, or entries without
    needing to read the entire file first.

    Args:
        path: File path in workspace
        content: Content to append

    Returns:
        Confirmation message
    """
    existing = workspace.read_file(path) if workspace.exists(path) else ""
    new_content = existing + content
    workspace.write_file(path, new_content)

    return f"Appended {len(content)} chars to {path}"
```

Usage:
```
append_to_file("workspace.md", "\n## New Accomplishment\n- Extracted 47 requirements")
```

#### 3. `prepend_to_file(path, content)` (Optional)

For adding content to the beginning of a file.

#### 4. `update_section(path, section_header, new_content)` (Optional)

For markdown files, update a specific section by header:

```python
def update_section(path: str, section_header: str, new_content: str) -> str:
    """Update a specific section in a markdown file.

    Finds the section by header and replaces its content
    (everything until the next header of same or higher level).

    Args:
        path: File path in workspace
        section_header: Header text (e.g., "## Current State")
        new_content: New content for the section (without header)

    Returns:
        Confirmation message
    """
    # Implementation finds section, replaces content, preserves structure
```

Usage:
```
update_section("workspace.md", "## Current State", "Phase: 2\nStatus: Processing requirements")
```

## Tool Selection Guide

| Scenario | Tool |
|----------|------|
| Change a specific value | `edit_file` |
| Add new entry/section | `append_to_file` |
| Update markdown section | `update_section` |
| Create new file | `write_file` |
| Full file replacement (rare) | Read first, then `write_file` |

## Implementation Notes

1. **Keep `write_file` for new files** - Still useful for creating files from scratch

2. **Add read tracking anyway** - Track which files have been read in `ToolContext._read_files: Set[str]`. If `write_file` is called on an existing file that hasn't been read, issue a warning (not an error) suggesting atomic tools.

3. **Update workspace.md template** - Change the instruction to:
   ```markdown
   Update it with edit_file() or append_to_file() to persist important information.
   ```

4. **Agent instructions** - Add guidance about preferring atomic edits over full rewrites.

## How Claude Code Actually Handles This

Research into Claude Code's official implementation (as of 2025) reveals a sophisticated approach to context management that addresses the exact issues we identified.

### Server-Side Context Editing (`clear_tool_uses_20250919`)

Anthropic's API has a beta feature that automatically clears old tool results:

```python
context_management={
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": 30000},
            "keep": {"type": "tool_uses", "value": 5},  # Keep last 5
            "exclude_tools": ["read_file"],  # Never clear file reads
            "clear_at_least": {"type": "input_tokens", "value": 5000}
        }
    ]
}
```

Key features:
- **Trigger threshold**: Activates when context exceeds a token limit (default: 100k)
- **Keep recent results**: Preserves the **last N tool use/result pairs** (default: 3)
- **Clears oldest first**: Removes tool results chronologically, keeping recent ones
- **Placeholder text**: Replaces cleared results with placeholder text so Claude knows the result was removed
- **Exclude specific tools**: Can protect certain tools (like `read_file`) from being cleared

### Memory Tool Integration

The memory tool + context editing work together:

1. **Warning before clearing**: Claude receives an automatic warning when approaching the threshold
2. **Save to memory**: Claude can write essential info from tool results to memory files before they're cleared
3. **Access on demand**: Claude can look up previously cleared information from memory files when needed

This is exactly what our `workspace.md` pattern is designed to do - persistent memory outside the context window.

### Client-Side Compaction (SDK)

For very long sessions, the SDK can generate a summary that replaces the entire history:

```python
compaction_control={
    "enabled": True,
    "context_token_threshold": 100000,
    "model": "claude-haiku-4-5"  # Optional: use cheaper model for summaries
}
```

The compaction generates a structured summary with:
1. **Task Overview**: Core request, success criteria, constraints
2. **Current State**: What's completed, files modified, artifacts produced
3. **Important Discoveries**: Decisions made, errors resolved, failed approaches
4. **Next Steps**: Specific actions needed, blockers, priority order
5. **Context to Preserve**: User preferences, domain-specific details

## What We Already Implemented

Our `context.py` already implements most of Claude Code's approach:

### 1. Keep Recent Tool Results (Priority: High)

Instead of aggressively trimming all tool results:

```python
# Configuration
KEEP_LAST_N_TOOL_RESULTS = 5
PROTECTED_TOOLS = {"read_file", "list_files"}  # Never clear these

def _trim_messages(self, messages: List[BaseMessage]) -> List[BaseMessage]:
    # Keep the last N tool results intact
    # Only clear older tool results
    # Replace cleared results with placeholder text
    pass
```

### 2. Placeholder Text for Cleared Results (Priority: High)

Don't just delete tool results - replace them with a placeholder:

```python
CLEARED_PLACEHOLDER = "[Tool result cleared to save context. Re-run the tool if needed.]"
```

This lets the agent know:
- The tool was called
- The result existed but was cleared
- They can re-run the tool if needed

### 3. Warning Before Clearing (Priority: Medium)

Inject a system message when approaching the clearing threshold:

```python
CONTEXT_WARNING = """
⚠️ CONTEXT APPROACHING LIMIT

You are at {percentage}% of context capacity. Important tool results
will be cleared soon. Save any critical information to workspace.md now.
"""
```

### 4. Exclude Critical Tools (Priority: Medium)

Never clear results from certain tools:

```python
PROTECTED_TOOLS = {
    "read_file",      # File content is essential for edits
    "list_todos",     # Current task state
    "get_progress",   # Progress tracking
}
```

## Comparison with Claude Code

| Feature | Claude Code | Our System (Current) | Notes |
|---------|-------------|---------------------|-------|
| Read before write | Required for Edit tool | Not enforced | Could add for edit_file |
| Edit tool | `old_string` → `new_string` | `append_file` only | Need to add `edit_file` |
| Keep recent tool results | Last 3-5 tool uses | **Last 5** (`keep_recent_tool_results`) | ✅ Implemented! |
| Placeholder for cleared | Yes | **Yes** (`placeholder_text`) | ✅ Implemented! |
| Warning before clearing | Yes | No | TODO |
| Protected tools | Configurable | None | TODO |
| Memory/workspace file | Yes (memory tool) | Yes (workspace.md) | ✅ Implemented! |
| Context threshold | 100k tokens default | 80k/100k tokens | ✅ Implemented! |

**Actual implementation in `src/agent/context.py`:**

```python
@dataclass
class ContextConfig:
    compaction_threshold_tokens: int = 80_000
    summarization_threshold_tokens: int = 100_000
    keep_recent_tool_results: int = 5  # ← Keeps last 5 tool results intact
    keep_recent_messages: int = 20
    max_tool_result_length: int = 5000
    placeholder_text: str = "[Result processed - see workspace if needed]"  # ← Placeholder
```

The `clear_old_tool_results()` method (lines 472-524) replaces old tool results with placeholder while keeping recent ones intact - exactly like Claude Code.

## Remaining Implementation Priorities

Most features are already implemented in `src/agent/context.py`. Remaining work:

1. **Add `edit_file` tool** - `append_file` exists; need search/replace for targeted edits
2. **Warning before clearing** - Inject system message when approaching threshold
3. **Protected tools** - Add config to exclude specific tools (like `read_file`) from clearing

## Industry Research: How Major AI Tools Implement File Editing (January 2026)

Research into Claude Code, Google Gemini CLI, AWS Q CLI, and emerging tools like Morph reveals
a strong consensus around the `str_replace` pattern, with divergence only in how aggressively
implementations handle imprecise matches.

### Anthropic — Text Editor Tool (`str_replace_based_edit_tool`)

**Source**: [Text editor tool - Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool)

Anthropic's tool is a schema-less, model-integrated tool (the schema is baked into the model weights).
It exposes multiple commands through a single tool:

| Command | Parameters | Purpose |
|---------|-----------|---------|
| `view` | `path`, `view_range` (optional line range) | Read file or directory listing |
| `str_replace` | `path`, `old_str`, `new_str` | Exact search-and-replace |
| `create` | `path`, `file_text` | Create new file |
| `insert` | `path`, `insert_line`, `new_str` | Insert text after a line number |
| `undo_edit` | `path` | Revert last edit (Claude 3.7 only, removed in Claude 4) |

Key design decisions:
- **Exact match only** — no fuzzy matching, no regex. `old_str` must match the file content exactly
  including whitespace and indentation.
- **Single replacement** — if `old_str` matches 0 times → error ("no match found"); if >1 times →
  error ("found N matches, provide more context"). No `replace_all` option.
- **Implementation is on the caller** — the API defines the tool schema but the application must
  implement the actual file I/O, matching, and error handling.
- **Recommended error responses**: Return `is_error: true` with descriptive messages
  ("Found 3 matches for replacement text. Please provide more context to make a unique match.").
- **`undo_edit` was removed** in Claude 4 (`text_editor_20250728`) — Anthropic decided backups are
  the caller's responsibility.

Reference implementation from the docs:

```python
def safe_replace(file_path, old_text, new_text):
    """Replace text only if there's exactly one match."""
    with open(file_path, 'r') as f:
        content = f.read()

    count = content.count(old_text)
    if count == 0:
        return "Error: No match found"
    elif count > 1:
        return f"Error: Found {count} matches"
    else:
        new_content = content.replace(old_text, new_text)
        with open(file_path, 'w') as f:
            f.write(new_content)
        return "Successfully replaced text"
```

### Google — Gemini CLI (`replace` tool)

**Sources**:
- [Gemini CLI file system tools](https://google-gemini.github.io/gemini-cli/docs/tools/file-system.html)
- [Seeking Testers: New edit/replace tool](https://github.com/google-gemini/gemini-cli/discussions/7758)

Gemini CLI takes a more sophisticated approach with self-correction:

| Parameter | Type | Purpose |
|-----------|------|---------|
| `file_path` | string | Absolute path to file |
| `old_string` | string | Exact text to find |
| `new_string` | string | Replacement text |
| `expected_replacements` | number (optional, default 1) | How many occurrences to replace |

Key design decisions:
- **Multi-stage matching strategy**:
  1. Exact literal string matching
  2. Flexible match (ignoring leading/trailing whitespace per line)
  3. Regex-based matching (tolerant of variable whitespace between tokens)
  4. **LLM self-correction** — if all strategies fail, invokes the model again to refine `old_string`/`new_string`
- **`expected_replacements`** instead of boolean `replace_all` — more explicit about intent.
- **File creation via overloading** — empty `old_string` + nonexistent file = create; empty `old_string` +
  existing file = error.
- **User confirmation** — shows a diff and requires approval before writing.

The fuzzy matching adds robustness (~85-90% accuracy vs ~70% for exact-only) but also
adds significant complexity. The self-correction layer requires an additional LLM call on failure.

### AWS — Q CLI (`fs_write` with `str_replace` mode)

**Source**: [The hidden sophistication behind how AI agents edit files](https://sumitgouthaman.com/posts/file-editing-for-llms/)

The simplest implementation:
- Parameters: `old_str`, `new_str`, optional summary
- Error if 0 matches or >1 matches — no fuzzy logic, no self-correction
- Straightforward `content.count()` + `content.replace()` pattern

### Morph / Fast-Apply (Emerging Pattern)

**Source**: [Diff Format Explained - Morph](https://www.morphllm.com/edit-formats/diff-format-explained)

An alternative paradigm that avoids search/replace entirely:
- The reasoning LLM outputs only the new code + change instructions (not `old_str`/`new_str`)
- A specialized fine-tuned model (Llama 70B) applies the edits semantically
- Claims ~98% first-pass accuracy vs ~84% for search/replace
- Two-model architecture: planner decides, applier edits

This is relevant for IDE-level source code editing but overkill for workspace file management.

### Cross-Tool Comparison

| Feature | Anthropic | Gemini CLI | Q CLI | Our System |
|---------|-----------|------------|-------|------------|
| Core mechanism | `old_str`/`new_str` | `old_string`/`new_string` | `old_str`/`new_str` | `append_file` only |
| Default behavior | Single replacement | Single (configurable) | Single replacement | Append only |
| Multi-match handling | Error + count | Error + self-correct | Error + count | N/A |
| No-match handling | Error message | Fuzzy fallback + LLM retry | Error message | N/A |
| Replace-all | Not supported | `expected_replacements` param | Not supported | N/A |
| Fuzzy matching | No | Yes (3 tiers) | No | N/A |
| LLM self-correction | No | Yes | No | N/A |
| File creation | Separate `create` command | Empty `old_string` overload | Separate mode | `write_file` |
| Undo support | Removed in v4 | No | No | No |
| Accuracy (estimated) | ~70-80% | ~85-90% | ~60-70% | N/A |

### Design Recommendations for Our Implementation

Based on this research:

1. **Follow the Anthropic/Q pattern** (simple exact match). Our agent uses an OpenAI-compatible LLM
   without model-specific `str_replace` training, so keeping the tool simple and the error messages
   clear is more important than fuzzy matching.

2. **Parameters**: `path`, `old_string`, `new_string`. No `replace_all` — if the agent needs to
   replace multiple occurrences, it should call the tool multiple times or use `write_file`. This
   matches the industry consensus that single-replacement-by-default prevents accidental bulk changes.

3. **Error messages should be actionable**:
   - 0 matches: "old_string not found in {path}. Make sure it matches exactly (including whitespace)."
   - N>1 matches: "old_string appears {N} times in {path}. Include more surrounding context to make it unique."

4. **Skip fuzzy matching and self-correction** — these add complexity and latency (extra LLM call).
   Our workspace files are markdown/yaml where whitespace is predictable. If the agent gets it wrong,
   the clear error message lets it retry with better context.

5. **Keep `write_file` for file creation** — don't overload `edit_file` with creation semantics.
   Explicit is better than implicit.

## References

- [Text editor tool - Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool)
- [Gemini CLI file system tools](https://google-gemini.github.io/gemini-cli/docs/tools/file-system.html)
- [Seeking Testers: New edit/replace tool for Gemini CLI](https://github.com/google-gemini/gemini-cli/discussions/7758)
- [The hidden sophistication behind how AI agents edit files](https://sumitgouthaman.com/posts/file-editing-for-llms/)
- [Diff Format Explained - Morph](https://www.morphllm.com/edit-formats/diff-format-explained)
- [Anthropic API: Text editor tool (Simon Willison)](https://simonwillison.net/2025/Mar/13/anthropic-api-text-editor-tool/)
- [Context editing - Claude Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Managing context on the Claude Developer Platform](https://claude.com/blog/context-management)
- [How Claude Code Got Better by Protecting More Context](https://hyperdev.matsuoka.com/p/how-claude-code-got-better-by-protecting)
- `src/agent/context.py` - Context compaction logic (ContextManager class)
- `src/agent/tools/workspace_tools.py` - Current tool implementations
