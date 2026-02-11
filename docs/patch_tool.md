# Patch Tool Research: Multi-File Content Operations

## Problem

Moving content between files (e.g., extracting a function from file A into file B) currently requires the agent to:

1. `read_file` source — content enters context window
2. `read_file` destination — more content in context
3. `edit_file` destination — content passes through context again as `new_string`
4. `edit_file` source — content passes through context again as `old_string`

That's 4 tool calls and the content traverses the context window multiple times. For large blocks of code or text, this is expensive and error-prone.

## Industry Survey

### Key Finding

**No major AI coding assistant has a dedicated "move content between files" tool.** All decompose it into read + edit steps. The differences lie in how edits are represented.

### Three Edit Paradigms

#### 1. Exact String Replacement (Claude Code, Cline)

- Find `old_string`, replace with `new_string`
- Safe due to uniqueness enforcement
- Content must pass through context as verbatim strings
- Claude Code enforces read-before-write

**Claude Code tools:** `Read`, `Edit` (exact string match), `Write` (full file)

**Cline tools:** `replace_in_file` (SEARCH/REPLACE blocks), `write_to_file`
- Cline developed an **order-invariant multi-diff apply algorithm** to handle LLMs returning diffs out of sequence, improving success rate by 10%

#### 2. Unified Diff / Patch (OpenAI `apply_patch`, Aider, Cursor)

- Standard git diff format with `+`/`-` lines and context
- Content never enters context as a full blob — only changed lines

**OpenAI `apply_patch`** (GPT-5.1+):
- Single tool call can patch multiple files
- Moving a function = one operation with diffs for both source and destination
- Uses standard unified diff format
- Best practice: read file first, generate diff relative to exact content, only call once per edit

**Aider** supports multiple edit formats per model:
- **Whole format**: Returns entire updated file (simple but expensive)
- **SEARCH/REPLACE format**: Merge-conflict-style blocks (efficient)
- **Unified diff format**: Standard patch format — GPT-4 Turbo is 3X less lazy with this format
- **Architect mode**: Separates reasoning (architect model) from editing (editor model)

**Cursor**:
- Two-stage: main model generates semantic diff, apply model writes actual file
- Linter integration for self-correction
- Composer mode for multi-file atomic previews

#### 3. Whole File Rewrite (Claude `Write`, Aider "whole" format)

- Return entire new file content
- Simplest to implement, most expensive in tokens
- Used as fallback when models struggle with diff syntax

### Multi-File Coordination Strategies

| Strategy | Tools | Pros | Cons |
|----------|-------|------|------|
| **Single operation** | OpenAI `apply_patch`, Jules | Atomic, one tool call | Complex format |
| **Sequential operations** | Claude Code, Cline, Amazon Q | Granular control | Risk of partial completion |
| **Working Set** | GitHub Copilot Edits | Explicit scope, human-in-loop | Requires IDE integration |
| **VM-based** | Google Jules | Full repo context, safe | Async, cloud-dependent |

### Tool Comparison

| Tool | Edit Format | Multi-File in One Call | Content in Context |
|------|------------|----------------------|-------------------|
| Claude Code | Exact string | No | Yes (full strings) |
| OpenAI Codex | Unified diff (`apply_patch`) | Yes | No (only diff lines) |
| Cursor | Semantic diff + apply model | Yes (Composer) | No (only diff) |
| Aider | SEARCH/REPLACE or unified diff | Yes (multiple blocks) | Partial |
| GitHub Copilot | Working Set + diffs | Yes | No |
| Amazon Q | `fs_read`/`fs_write` | No | Yes |
| Google Jules | VM-based multi-file | Yes (PR creation) | N/A (runs in VM) |
| Windsurf | Placeholder `{{ ... }}` | Yes | No (only changes) |
| Cline | SEARCH/REPLACE blocks | No | Partial |

## Options for Our Agent

### Option A: `transfer_content` Tool (Pragmatic)

A line-range-based tool that moves content between files without it passing through the LLM context.

```python
transfer_content(
    source_path="module_a.py",
    source_start_line=45,
    source_end_line=78,
    dest_path="module_b.py",
    dest_position=12,           # insert after line 12, or "start"/"end"
    remove_from_source=True     # True = cut, False = copy
)
```

**Pros:**
- Simple to implement, fits existing tool architecture
- Content never enters LLM context — direct file-to-file byte transfer
- Agent only needs to know line numbers (from `read_file` output)

**Cons:**
- Line numbers are brittle (can shift between reads)
- No transformation of content (e.g., adjusting indentation, imports)
- Agent still needs to read both files to determine line numbers

### Option B: `apply_patch` Tool (OpenAI-style)

Accept unified diffs for one or more files in a single tool call.

```python
apply_patch(patch="""
--- a/module_a.py
+++ b/module_a.py
@@ -45,34 +45,0 @@
-def extracted_function():
-    ...
-    (34 lines removed)

--- a/module_b.py
+++ b/module_b.py
@@ -12,0 +12,34 @@
+def extracted_function():
+    ...
+    (34 lines added)
""")
```

**Pros:**
- Industry standard format (git-compatible)
- Multi-file atomic operations
- Only changed lines pass through context
- Models are already trained on this format

**Cons:**
- Bigger architectural change — need diff parsing and application logic
- Diff application can fail (fuzzy matching needed for context drift)
- More complex error handling (partial apply, conflicts)
- Our models may not generate clean diffs reliably

### Option C: SEARCH/REPLACE Blocks (Aider-style)

Multiple SEARCH/REPLACE blocks in one tool call.

```python
multi_edit(edits="""
module_a.py
<<<<<<< SEARCH
def extracted_function():
    ...
=======
>>>>>>> REPLACE

module_b.py
<<<<<<< SEARCH
# Insert point
=======
# Insert point

def extracted_function():
    ...
>>>>>>> REPLACE
""")
```

**Pros:**
- Simpler than unified diff parsing
- Models handle this format well (merge-conflict-like)
- Multi-file in one call

**Cons:**
- Still requires content in context (the SEARCH/REPLACE strings)
- Exact match requirement (same problem as current `edit_file`)
- Custom format, not standard

## Recommendation

**Option A (`transfer_content`)** for immediate value — it solves the core problem (content bypassing context) with minimal complexity. Can be implemented in a single file addition to `src/tools/workspace/`.

**Option B (`apply_patch`)** as a future evolution if we find that models consistently need to transform content during moves (adjust imports, fix indentation, rename references). This is a bigger investment but aligns with industry direction.

## Sources

- [Claude Code tools](https://gist.github.com/bgauryy/0cdb9aa337d01ae5bd0c803943aa36bd)
- [Claude text editor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool)
- [OpenAI apply_patch](https://platform.openai.com/docs/guides/tools-apply-patch)
- [Aider edit formats](https://aider.chat/docs/more/edit-formats.html)
- [Aider unified diffs](https://aider.chat/docs/unified-diffs.html)
- [How Cursor AI IDE works](https://blog.sshh.io/p/how-cursor-ai-ide-works)
- [Code Surgery: How AI Assistants Make Precise Edits](https://fabianhertwig.com/blog/coding-assistants-file-edits/)
- [Cline: Improving diff edits by 10%](https://cline.bot/blog/improving-diff-edits-by-10)
- [GitHub Copilot Edits](https://code.visualstudio.com/blogs/2024/11/12/introducing-copilot-edits)
- [Windsurf Cascade](https://docs.windsurf.com/windsurf/cascade/cascade)
- [Amazon Q Developer built-in tools](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-built-in-tools.html)
- [Google Jules](https://blog.google/technology/google-labs/jules/)
