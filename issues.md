# Universal Agent Issues

Investigation date: 2026-01-08, updated 2026-01-09

## Summary

**Phase 1 (2026-01-08):** Initial issues stemmed from a **partial implementation** of the workspace-centric architecture. The context efficiency pattern was half-implemented: tools returned concise summaries but didn't persist full data to workspace files. Issues #1-8 resolved.

**Phase 2 (2026-01-09):** Testing revealed critical runtime issues. Job `19c4de92` ran for 53 minutes and produced zero requirements before hitting LangGraph's recursion limit. New issues discovered: context overflow loop (#10), agent reading full documents instead of chunks (#11), insufficient logging (#12), candidate extractor returning zero (#13), and MongoDB archiver missing data (#14).

**Phase 3 (2026-01-09 - deep dive):** Issue #10 investigated in depth. Server-side root cause (llama.cpp `--parallel` config dividing context window) **RESOLVED** by user. Code-side issues remain: 6 sub-issues identified (10.1-10.6) covering error classification, consecutive error tracking, context compaction, and summarization.

**Phase 4 (2026-01-09 - deep dive):** Issue #11 investigated in depth. Root cause: `extract_document_text` tool exposes full text file path and invites agent to read it. Contributing factors: default `max_read_size` of 100KB is too large, no pre-read file size warning, missing explicit instructions to avoid full text files. 4 sub-issues identified (11.1-11.4).

**Phase 5 (2026-01-09 - deep dive):** Issue #12 investigated in depth. Root cause: `tools_node` has two error paths (tool result errors and exceptions) but neither logs sufficient details. 184 tool errors occurred in job `19c4de92` with zero indication of which tool failed or why. 4 sub-issues identified (12.1-12.4).

**Phase 6 (2026-01-09 - deep dive):** Issue #13 investigated in depth. Root cause: **Fundamental architecture flaw** - the candidate extractor uses regex-based pattern matching which cannot handle the linguistic complexity of German legal text. German patterns exist but are overly restrictive (e.g., requiring "muss sein" but text uses "muss im" or "müssen...erhalten bleiben"). Testing showed 0% match rate on real GoBD sentences. **Recommended fix:** Replace regex extraction with LLM-based extraction where the agent reads each chunk and identifies requirements using its language understanding.

**Phase 7 (2026-01-09 - deep dive):** Issue #14 investigated in depth. Root cause: **Multiple data gaps** in archiver. Token counts missing (archiver stores chars, not tokens from `usage_metadata`). Tool calls empty due to format mismatch with local LLM response structure. **Recommended fix:** Create new `agent_audit` collection that captures complete agent execution history including LLM calls, tool executions, results, errors, and state changes. This addresses both the immediate archiver bugs and the broader observability gap.

**Phase 8 (2026-01-09 - implementation):** Issue #14 **RESOLVED**. Implemented the `agent_audit` MongoDB collection with comprehensive step tracking. Changes: Extended `LLMArchiver` with audit methods (`audit_step`, `audit_tool_call`, `audit_tool_result`), added audit hooks to all 4 graph nodes (initialize, process, tools, check), updated `init_mongodb.py` with new collection and indexes, added `--audit` flag to `view_llm_conversation.py` for viewing audit trails.

**Phase 9 (2026-01-09 - implementation):** Issue #9 **RESOLVED**. Refactored PostgreSQL schema to remove deprecated tables and dead code. Changes: Renamed `requirement_cache` to `requirements` (elevated to proper requirements table with `neo4j_id` field), removed `llm_requests`/`agent_checkpoints`/`candidate_workspace` tables, deleted `schema_vector.sql`, removed 6 dead functions from `postgres_utils.py`, deleted deprecated files (`checkpoint.py`, `workspace.py`, `vector.py`), updated all dependent code.

**Current status:** 10 issues resolved, 4 issues open (with 14 sub-issues total). Server-side context limit fixed. Code-side robustness improvements needed for #10, #11, and #12. Core extraction blocked by #13 - requires architectural change.

---

## Resolved Issues (Compacted)

### Critical (P0) - Fixed 2026-01-08/09

| # | Issue | Location | Problem | Fix Applied |
|---|-------|----------|---------|-------------|
| 1 | Async tool sync invocation | `graph.py:355-383` | `tools_node` used sync `invoke()` but some tools are async | Changed to `async def tools_node` with `await tool_node.ainvoke()` |
| 2 | Document tools don't persist | `document_tools.py` | `extract_document_text` and `chunk_document` returned previews but didn't write full content to workspace | Tools now write to `extracted/` and `chunks/` directories, return file paths |

### Medium (P1-P2) - Fixed 2026-01-08/09

| # | Issue | Location | Problem | Fix Applied |
|---|-------|----------|---------|-------------|
| 3 | Iterations counter shows 0 | `graph.py:294-350` | `check_node` didn't include `iteration` in return dict; streaming only gets partial state | Added `iteration` to all `check_node` return statements |
| 4 | Path resolution bug | `document_tools.py` | Relative paths not resolved to workspace | Added `resolve_path()` helper using workspace manager |
| 5 | Resume flag ignored | `run_universal_agent.py`, `agent.py` | `--resume` CLI flag accepted but never used | Implemented workspace preservation on resume |
| 6 | Premature completion | `graph.py:590-650` | Phrase matching triggered false completion on resume | Added `mark_complete` tool; checks `output/completion.json` file first |

### Low (P2-P3) - Fixed 2026-01-09

| # | Issue | Location | Problem | Fix Applied |
|---|-------|----------|---------|-------------|
| 7 | Candidate extractor empty | `candidate_extractor.py` | Running on placeholder text | Resolved by #2 (actual content now available) |
| 8 | No web search guidance | `system_prompt.md` | Agent didn't know about fallback tools | Added fallback strategies and tool documentation to system prompt |
| 9 | PostgreSQL dead code/tables | `schema.sql`, `postgres_utils.py` | 4 tables deprecated, dead code functions | Removed tables, renamed `requirement_cache` → `requirements`, deleted dead code |

---

## Open Issues

### ~~9. PostgreSQL Tables: Dead Code and Deprecated Tables~~ RESOLVED

**Investigation date:** 2026-01-09

**Problem:** The PostgreSQL database has 5 tables and 2 views, but most are empty or unused.

#### Database Table Status

| Table | Status | Explanation |
|-------|--------|-------------|
| `jobs` | Active | Working correctly - used by Universal Agent polling |
| `requirement_cache` | Active | Tool exists (`write_requirement_to_cache`) - populates when agent completes |
| `llm_requests` | **Dead code** | Function `log_llm_request()` exists but **nothing calls it** |
| `agent_checkpoints` | **Deprecated** | Old agents used it; Universal Agent uses LangGraph's built-in checkpointing |
| `candidate_workspace` | **Deprecated** | Old agents used it; Universal Agent uses filesystem workspaces |
| `workspace_embeddings` | **Never implemented** | Schema exists in `schema_vector.sql` but table was never created |

#### Root Cause: Architecture Transition

The Universal Agent architecture moved away from PostgreSQL for most state management:

| Function | Old Agents | Universal Agent |
|----------|-----------|-----------------|
| Workspaces | `candidate_workspace` table | Filesystem (`./workspace/job_<uuid>/`) |
| Checkpointing | `agent_checkpoints` table | LangGraph's `AsyncPostgresSaver` |
| LLM Logging | `llm_requests` table (never used) | MongoDB via `llm_archiver.py` |

Only `jobs` and `requirement_cache` are actively used by the new architecture.

#### LLM Metrics: Two Broken Systems

**System 1: PostgreSQL `llm_requests`**
- Location: `src/core/postgres_utils.py:log_llm_request()`
- Status: Function fully implemented but **nothing calls it**
- Reporter at `src/orchestrator/reporter.py` expects data here but gets nothing

**System 2: MongoDB via `LLMArchiver`**
- Location: `src/agents/shared/llm_archiver.py`
- Wired up in: `src/agents/universal/graph.py:222-226`
- Status: Works but requires `MONGODB_URL` environment variable
- If not set → **silently skipped** (no error, no logging)

**Result:** Unless MongoDB is configured, **no LLM metrics are recorded anywhere**.

```python
# graph.py lines 222-226 - only logs if MongoDB configured
archiver = get_archiver()
if archiver:  # Returns None if MONGODB_URL not set
    archiver.archive(...)  # Silently skipped otherwise
```

#### Code Locations

**Dead code (never called):**
- `src/core/postgres_utils.py:log_llm_request()` - LLM request logging
- `src/core/postgres_utils.py:save_checkpoint()` - Checkpoint saving
- `src/core/postgres_utils.py:get_latest_checkpoint()` - Checkpoint loading

**Deprecated but still has callers (old agents):**
- `src/agents/shared/checkpoint.py:CheckpointManager` - Used by old Creator/Validator
- `src/agents/shared/workspace.py:Workspace` - Uses `candidate_workspace` table

**Schema files:**
- `src/database/schema.sql` - Main schema (includes deprecated tables)
- `src/database/schema_vector.sql` - Vector search schema (never applied)

#### Fix Options

**Option A: Quick Cleanup (Recommended for now)**
1. Add comments to `schema.sql` noting which tables are deprecated
2. Delete dead code (`log_llm_request()`, checkpoint functions)
3. Enable MongoDB by uncommenting `MONGODB_URL` in `.env` if LLM logging needed

**Option B: Fix PostgreSQL Logging**
Wire up `log_llm_request()` in Universal Agent graph:
```python
# In graph.py process_node, after LLM response
from src.core.postgres_utils import log_llm_request
await log_llm_request(
    postgres_conn,
    job_id=state["job_id"],
    model=config.llm.model,
    prompt_tokens=response.usage_metadata.input_tokens,
    completion_tokens=response.usage_metadata.output_tokens,
    ...
)
```

**Option C: Keep Both (Production)**
Support both backends based on configuration - PostgreSQL for simple deployments, MongoDB for high-volume logging.

#### Impact

- **Reporter broken:** `src/orchestrator/reporter.py` has `LLMStatistics` class expecting data from `llm_requests` but gets nothing
- **No cost tracking:** Token usage not recorded unless MongoDB configured
- **Confusion:** Database has 5 tables, only 2 do anything
- **Technical debt:** Dead code and deprecated tables remain in codebase

---

### 10. Context Overflow Infinite Loop (P0)

**Investigation date:** 2026-01-09, updated 2026-01-09 (deep dive)

**Problem:** Agent gets stuck in infinite retry loop when context exceeds LLM's limit.

**Status:** Server-side issue **RESOLVED** (user fixed llama.cpp config). Code-side issues **OPEN** (6 sub-issues).

#### Observed Behavior

```
Process node iteration 302, 490 messages
Current token count: 132195
Cleared 239 old tool results
LLM invocation error: {'code': 400, 'message': 'the request exceeds the available context size',
  'n_prompt_tokens': 49104, 'n_ctx': 43776}
[Iteration ?] error=False    ← Error not propagated!
Process node iteration 303...  ← Immediately retries same failing request
```

The agent ran from iteration 302 to 377+ with no termination, eventually hitting LangGraph's recursion limit (1000).

#### Token Count Analysis

| Metric | Value | Source |
|--------|-------|--------|
| Local token estimate | 132,195 | `context_manager.get_token_count()` |
| Tokens after compaction | 49,104 | Server-reported `n_prompt_tokens` |
| Server context limit | 43,776 | Server-reported `n_ctx` (was misconfigured) |
| Expected limit | 128,000 | Actual model capacity |
| Gap (over limit) | 5,328 | Not recoverable by simple retry |

---

#### Server-Side Root Cause (RESOLVED)

**Problem:** llama.cpp was configured with `--parallel 3` which divided the 128k context window by 3, resulting in only ~43k per request.

**Fix Applied:** User adjusted server configuration to provide full 128k context per request.

**Result:** Context overflow errors from server should no longer occur at 49k tokens.

---

#### Code-Side Root Causes (6 Sub-Issues)

Even with the server fix, the code has fundamental problems that would cause infinite loops on any context overflow error. These need to be fixed for robustness.

##### 10.1 All LLM Errors Marked as Recoverable

**Location:** `graph.py:249-259` (`process_node` exception handler)

```python
except Exception as e:
    logger.error(f"LLM invocation error: {e}")
    return {
        "error": {
            "message": str(e),
            "type": "llm_error",
            "recoverable": True,  # ← ALL errors marked recoverable!
            "traceback": traceback.format_exc(),
        },
        "iteration": iteration + 1,
    }
```

**Problem:** Context overflow errors are NOT recoverable by retrying with the same context. The error type should be detected and handled differently.

**Error types that should NOT be recoverable:**
- Context overflow (`"exceeds the available context size"`)
- Invalid API key (`"authentication"`, `"401"`)
- Model not found (`"404"`, `"model not found"`)
- Rate limit exceeded (recoverable, but needs longer backoff)

##### 10.2 Recoverable Errors Cleared Without Limit

**Location:** `graph.py:348-350` (`check_node`)

```python
# Clear any recoverable errors
if error and error.get("recoverable"):
    return {"error": None, "iteration": iteration}  # ← Just clears it!
```

**Problem:** There's no counter tracking consecutive errors. If the same recoverable error keeps happening (e.g., transient network issue that persists), the agent loops forever.

**Missing mechanism:** Consecutive error counter that stops after N failures.

##### 10.3 `prepare_messages_for_llm()` Too Conservative

**Location:** `context.py:352-384`

```python
def prepare_messages_for_llm(self, messages, aggressive=False):
    ...
    # Step 1: Clear old tool results
    if should_be_aggressive:
        messages = self.clear_old_tool_results(messages)

    # Step 2: Truncate long results
    messages = self.truncate_long_tool_results(messages)

    return messages  # ← Never calls trim_messages() or summarize_and_compact()
```

**Problem:** Even in "aggressive" mode, compaction only:
1. Replaces old tool results with placeholders
2. Truncates long tool results

It does NOT use the more powerful methods that exist:
- `trim_messages()` (`context.py:386-432`) - Remove old messages entirely
- `summarize_and_compact()` (`context.py:503-553`) - LLM-based summarization

**Token impact:** `clear_old_tool_results()` reduced context from 132k to 49k in the observed run. That's good but not enough when the limit was 43k.

##### 10.4 Summarization Never Triggered

**Location:** `graph.py:677-724` (`run_graph_with_summarization`)

The summarization infrastructure exists and works:

```python
# context.py
def should_summarize(self, messages):
    return self.get_token_count(messages) > self.config.summarization_threshold_tokens  # 100k

async def summarize_and_compact(self, messages, llm, ...):
    # Full implementation exists - generates summary, keeps recent messages
    ...
```

**Problem:** The main execution path uses `run_graph_with_streaming()` which does NOT check for summarization:

```python
# graph.py:656-674
async def run_graph_with_streaming(graph, initial_state, config):
    async for state in graph.astream(initial_state, config=config):
        yield state  # ← No summarization check here
```

Meanwhile, `run_graph_with_summarization()` exists but is never called from the main entry point (`agent.py`).

##### 10.5 Token Check Happens After LLM Fails

**Location:** `graph.py:300-321` (`check_node`)

```python
# Check token limit - if extremely high, force stop
token_count = context_stats.get("current_token_count", 0)
if token_count > config.limits.context_threshold_tokens * 1.5:
    ...
```

**Problems:**

1. **Timing:** This check runs in `check_node`, which executes AFTER `process_node` has already tried (and failed) the LLM call. It's reactive, not preventive.

2. **Stale data:** `context_stats.current_token_count` is updated in `process_node` AFTER a successful LLM call. When the LLM call fails, the stats aren't updated - we go straight to `check_node` with the old value.

3. **Wrong value:** Uses pre-compaction estimate (132k in the observed case), not the actual tokens that would be sent to the LLM (49k after compaction).

##### 10.6 Error State Shows False After Errors

**Observation from logs:**

```
LLM invocation error: {'code': 400, 'message': 'the request exceeds...'}
[Iteration ?] error=False  ← Should be True!
```

**Analysis:** The streaming output shows `error=False` even after an LLM error occurred. This happens because:

1. `process_node` returns `{"error": {...}, "iteration": N}`
2. Graph routes to `check_node`
3. `check_node` clears recoverable errors: `return {"error": None, ...}`
4. Streaming sees the cleared state

The error IS being set and then cleared, but from the user's perspective it looks like errors aren't being tracked.

---

#### Code Locations Summary

| Location | Issue |
|----------|-------|
| `graph.py:249-259` | #10.1 - All errors marked recoverable |
| `graph.py:348-350` | #10.2 - Errors cleared without retry limit |
| `context.py:352-384` | #10.3 - `prepare_messages_for_llm()` too conservative |
| `context.py:231-242` | `should_summarize()` - exists but never used |
| `context.py:503-553` | `summarize_and_compact()` - exists but never called |
| `graph.py:677-724` | `run_graph_with_summarization()` - exists but not used |
| `graph.py:300-321` | #10.5 - Token check uses stale/wrong value |
| `context.py:386-432` | `trim_messages()` - exists but never called |

---

#### Fix Plan

##### Phase 1: Immediate (Prevent Infinite Loop)

**Fix 10.1 - Classify error types:**
```python
# In graph.py process_node exception handler
except Exception as e:
    error_str = str(e).lower()

    # Detect non-recoverable errors
    is_context_overflow = "context" in error_str and "exceed" in error_str
    is_auth_error = "401" in error_str or "authentication" in error_str or "api key" in error_str
    is_not_found = "404" in error_str or "model not found" in error_str

    recoverable = not (is_context_overflow or is_auth_error or is_not_found)
    error_type = "context_overflow" if is_context_overflow else \
                 "auth_error" if is_auth_error else \
                 "not_found" if is_not_found else "llm_error"

    return {
        "error": {
            "message": str(e),
            "type": error_type,
            "recoverable": recoverable,
            ...
        },
    }
```

**Fix 10.2 - Add consecutive error counter:**
```python
# In state.py - add to UniversalAgentState
consecutive_llm_errors: int = 0

# In graph.py process_node - on error
return {
    "consecutive_llm_errors": state.get("consecutive_llm_errors", 0) + 1,
    "error": {...},
}

# In graph.py process_node - on success
return {
    "consecutive_llm_errors": 0,  # Reset on success
    "messages": [response],
    ...
}

# In graph.py check_node - add limit check
consecutive_errors = state.get("consecutive_llm_errors", 0)
if consecutive_errors >= 3:
    return {
        "should_stop": True,
        "error": {
            "type": "consecutive_errors",
            "message": f"Stopped after {consecutive_errors} consecutive LLM errors",
            "recoverable": False,
        },
    }
```

##### Phase 2: Robustness (Better Context Management)

**Fix 10.3 - Enable aggressive compaction:**
```python
# In context.py prepare_messages_for_llm
def prepare_messages_for_llm(self, messages, aggressive=False):
    token_count = self.get_token_count(messages)

    # Step 1: Clear old tool results
    if aggressive or token_count > self.config.compaction_threshold_tokens:
        messages = self.clear_old_tool_results(messages)

    # Step 2: Truncate long results
    messages = self.truncate_long_tool_results(messages)

    # Step 3: If still too large, trim messages (NEW)
    if self.get_token_count(messages) > self.config.compaction_threshold_tokens:
        messages = self.trim_messages(messages)

    return messages
```

**Fix 10.4 - Wire up summarization:**

Option A: Check in `process_node` before LLM call
```python
# In graph.py process_node
if context_manager.should_summarize(messages):
    # Note: This requires passing LLM to context_manager
    messages = await context_manager.summarize_and_compact(messages, summarization_llm)
```

Option B: Use `run_graph_with_summarization()` in `agent.py`

##### Phase 3: Observability

**Fix 10.5 - Pre-emptive token check:**
```python
# In graph.py process_node, BEFORE LLM call
prepared_token_count = context_manager.get_token_count(prepared_messages)
if prepared_token_count > config.limits.context_threshold_tokens:
    logger.warning(f"Context too large ({prepared_token_count} tokens), need compaction")
    # Trigger emergency compaction or error out
    ...
```

**Fix 10.6 - Don't clear errors immediately:**
```python
# In check_node - track retry count before clearing
if error and error.get("recoverable"):
    # Could add: log that we're retrying, or increment a counter
    return {"error": None, "llm_retry_count": state.get("llm_retry_count", 0) + 1}
```

---

#### Impact

- **Critical:** Agent stuck in infinite loop, consuming resources for 53+ minutes
- **Data loss:** 300+ iterations of work potentially lost
- **No graceful degradation:** Should write partial results to workspace and stop
- **Resource waste:** LangGraph recursion limit (1000) is the only safety net

#### Server Fix Details (For Reference)

The user's llama.cpp server was configured with:
```bash
--ctx-size 131072 --parallel 3
```

This divided the KV cache by 3, giving each parallel slot only ~43k tokens instead of 128k.

**Fix:** Adjust `--parallel` or increase `--ctx-size` proportionally:
```bash
--ctx-size 393216 --parallel 3  # 131k per slot
# OR
--ctx-size 131072 --parallel 1  # Full 131k for single slot
```

---

### 11. Agent Reads Full Document Instead of Chunks (P1)

**Investigation date:** 2026-01-09, updated 2026-01-09 (deep dive)

**Problem:** Agent reads the entire extracted document file instead of individual chunks, causing a massive context spike.

**Status:** Root causes identified. Multiple contributing factors need fixes.

#### Observed Behavior

In job `19c4de92-85dd-4b92-84d6-b7934d152b2a`:

| Iteration | Token Count | Change |
|-----------|-------------|--------|
| 34 | 12,207 | +159 |
| 35 | **42,341** | **+30,134** |
| 36 | 42,501 | +160 |

The 30k token increase matches the size of `extracted/GoBD_example_full_text.txt` (105,809 bytes ≈ 30k tokens).

#### Evidence

```
2026-01-09 10:09:24 - Process node iteration 35, 72 messages
2026-01-09 10:09:24 - Current token count: 42341
2026-01-09 10:09:24 - Calling LLM (gpt-oss-120b)...
2026-01-09 10:11:08 - LLM response: 0 chars, 1 tool calls, 103187ms  ← 103 seconds!
```

The LLM took 103 seconds at this iteration (abnormally long), suggesting it was processing a very large tool result.

---

#### Root Cause Analysis (Deep Dive)

Investigation revealed **4 contributing factors** that together enabled this issue:

##### 11.1 Tool Design Exposes Full Text File Path

**Location:** `document_tools.py:136-153`

The `extract_document_text` tool writes the full document to a file AND tells the agent where it is:

```python
# document_tools.py:137-140
output_filename = f"extracted/{Path(file_path).stem}_full_text.txt"
if workspace is not None:
    workspace.write_file(output_filename, full_text)
    persist_msg = f"\nFull text written to: {output_filename}\nUse read_file(\"{output_filename}\") to access the content."
```

The tool's return message **explicitly invites** the agent to read the full text file:
```
Full text written to: extracted/GoBD_example_full_text.txt
Use read_file("extracted/GoBD_example_full_text.txt") to access the content.
```

This creates a "temptation" - the agent sees the file path and is told how to access it, even though chunks should be used instead.

##### 11.2 Default `max_read_size` Too Large

**Location:** `workspace_tools.py:37`

```python
max_read_size = context.get_config("max_read_size", 100_000)  # 100KB default
```

The default limit is 100KB, which is approximately **30,000 tokens** - nearly as large as the full document (105KB). This provides no practical protection against context overflow.

The `creator.json` config doesn't override this default, so the agent uses the 100KB limit.

##### 11.3 No File Size Warning in read_file

**Location:** `workspace_tools.py:41-74`

The `read_file` tool has truncation logic but **no pre-read warning**:

```python
@tool
def read_file(path: str) -> str:
    # ...
    content = workspace.read_file(path)  # Reads entire file first!

    # Truncate very large files to prevent context overflow
    if len(content) > max_read_size:
        truncated = content[:max_read_size]
        return (
            f"{truncated}\n\n"
            f"[TRUNCATED: File is {len(content):,} bytes, showing first {max_read_size:,}. ...]"
        )
```

**Problems:**
1. No warning BEFORE returning 100KB of content
2. Truncation message appears AFTER the damage is done
3. Agent might not even notice the truncation warning at the end
4. Returning 100KB is still way too much

##### 11.4 Missing Explicit Instruction in Agent Guidelines

**Locations:**
- `system_prompt.md` - No mention of avoiding full text files
- `creator_instructions.md` - Tells agent to use chunks but doesn't forbid reading full text

The `creator_instructions.md` says:
```markdown
## Phase 1: Document Preprocessing
1. Extract text using `extract_document_text`
2. ...
3. Chunk using appropriate strategy
4. Write chunks to `chunks/` folder
```

And:
```markdown
## Phase 2: Candidate Identification
For each chunk:
1. Run `identify_requirement_candidates`
```

This **implies** reading chunks but never **explicitly forbids** reading the full text file. An LLM might decide to read the full file for "context" or "overview" before processing chunks.

---

#### Workspace State at Failure

The job workspace shows proper preprocessing was completed:

```
workspace/job_19c4de92.../
├── extracted/
│   └── GoBD_example_full_text.txt  (105,809 bytes) ← Agent read THIS
├── chunks/
│   ├── manifest.json
│   ├── chunk_001.txt  (~1-2KB each)
│   ├── chunk_002.txt
│   └── ... (77 chunks total)
└── notes/
    └── preprocessing_summary.md
```

The chunks directory was correctly populated with 77 chunks averaging 1-2KB each (~500 tokens). The agent should have been reading these, not the 105KB full text file.

---

#### Why the Agent Made This Decision

At iteration 35, the agent likely called `read_file("extracted/GoBD_example_full_text.txt")` because:

1. **The tool invited it**: `extract_document_text` output said "Use read_file(...) to access the content"
2. **No explicit prohibition**: Instructions didn't say "never read the full text file"
3. **LLM reasoning**: Agent may have wanted to get an "overview" or verify extraction
4. **No size warning**: `read_file` didn't warn about file size before returning content

---

#### Code Locations Summary

| Location | Issue |
|----------|-------|
| `document_tools.py:137-140` | #11.1 - Exposes full text path in return message |
| `workspace_tools.py:37` | #11.2 - Default max_read_size = 100KB (too large) |
| `workspace_tools.py:41-74` | #11.3 - No pre-read file size check |
| `creator_instructions.md` | #11.4 - No explicit guidance to avoid full text |

---

#### Fix Plan

##### Fix 11.1 - Stop Exposing Full Text Path

**Option A (Recommended): Don't mention the file at all**
```python
# In document_tools.py extract_document_text
return f"""Document Extraction Complete

File: {file_path}
Page Count: {result.get('page_count', 'unknown')}
...
Total Characters: {result.get('char_count', 0)}

IMPORTANT: The full text has been extracted. Now use 'chunk_document' to split
it into processable chunks. Do NOT read the raw extracted text directly -
always work with chunks for efficient processing."""
```

**Option B: Don't write full text file at all**
Only write chunks, not the full text file. The full text can be reconstructed from chunks if needed.

##### Fix 11.2 - Reduce Default max_read_size

```python
# In workspace_tools.py
max_read_size = context.get_config("max_read_size", 10_000)  # 10KB ≈ 3k tokens
```

Also add to `creator.json`:
```json
"workspace": {
  "max_read_size": 8000
}
```

##### Fix 11.3 - Add Pre-Read File Size Check

```python
# In workspace_tools.py read_file
@tool
def read_file(path: str) -> str:
    try:
        # Check file size BEFORE reading
        full_path = workspace.get_path(path)
        file_size = full_path.stat().st_size

        # Warn for large files
        if file_size > 20000:  # 20KB warning threshold
            return (
                f"WARNING: File '{path}' is {file_size:,} bytes ({file_size // 300} estimated tokens).\n"
                f"This is too large to read efficiently.\n\n"
                f"For document content, use individual chunks in chunks/ directory instead.\n"
                f"Example: read_file('chunks/chunk_001.txt')"
            )

        # For extracted/ directory, always warn
        if path.startswith("extracted/") or "/extracted/" in path:
            return (
                f"WARNING: Reading from extracted/ directory is not recommended.\n"
                f"Use chunked files in chunks/ directory for efficient processing.\n"
                f"Example: read_file('chunks/chunk_001.txt')"
            )

        content = workspace.read_file(path)
        # ... rest of function
```

##### Fix 11.4 - Add Explicit Instruction

Add to `creator_instructions.md` in Phase 2:
```markdown
### IMPORTANT: Working with Document Content

After preprocessing:
- **ALWAYS** read from `chunks/chunk_XXX.txt` files
- **NEVER** read from `extracted/` directory - those files are too large
- Process chunks one at a time to manage context efficiently
- Check `chunks/manifest.json` to see all available chunks
```

Add to `system_prompt.md`:
```markdown
## File Size Guidelines

- Keep file reads small - prefer files under 10KB
- Never read full document extracts - use chunks instead
- If a file is too large, read specific sections or use search_files
```

---

#### Impact

- **Context overflow:** Single read pushed context from 12k to 42k tokens (+30k)
- **Wasted tokens:** 30k tokens used unnecessarily when chunks are ~500 tokens each
- **Performance:** 103 second LLM response time (vs typical 8-15 seconds)
- **Cascading failure:** Large context contributed to later context overflow (#10)

---

#### Fix Priority

| Sub-Issue | Priority | Effort | Impact |
|-----------|----------|--------|--------|
| 11.1 - Stop exposing path | P1 | Low | High - removes temptation |
| 11.2 - Reduce max_read_size | P1 | Low | High - limits damage |
| 11.3 - Pre-read size check | P2 | Medium | Medium - catches large reads |
| 11.4 - Explicit instructions | P1 | Low | Medium - guides LLM behavior |

**Recommended implementation order:** 11.1 → 11.4 → 11.2 → 11.3

---

### 12. Tool Error Logging Insufficient (P2)

**Investigation date:** 2026-01-09, updated 2026-01-09 (deep dive)

**Problem:** Tool errors are logged without error details, making debugging difficult.

**Status:** Root causes identified. 4 sub-issues discovered covering two distinct error paths.

#### Observed Behavior

In job `19c4de92-85dd-4b92-84d6-b7934d152b2a`, 184 tool errors occurred:

```
2026-01-09 10:06:20 - src.agents.universal.graph - WARNING - Tool error detected, retrying (1/3) after 2.0s
2026-01-09 10:06:22 - src.agents.universal.graph - WARNING - Tool error detected, retrying (2/3) after 4.3s
2026-01-09 10:06:30 - src.agents.universal.graph - WARNING - Tool error detected, retrying (1/3) after 2.0s
2026-01-09 10:06:32 - src.agents.universal.graph - WARNING - Tool error detected, retrying (2/3) after 4.1s
...
```

The log shows:
- That a tool error occurred ✓
- The retry attempt number ✓
- The backoff delay ✓

The log does NOT show:
- Which tool failed ✗
- What the error message/content was ✗
- What arguments were passed to the tool ✗
- Whether the retry succeeded or failed ✗

---

#### Root Cause Analysis (Deep Dive)

Investigation revealed **two distinct error paths** in `tools_node`, each with insufficient logging:

##### Error Path 1: Tool Result Errors (lines 387-406)

When a tool executes successfully but returns an error message in its content:

```python
# graph.py:387-406
tool_messages = result.get("messages", [])
has_errors = any(
    isinstance(m, ToolMessage) and _is_tool_error(m.content)
    for m in tool_messages
)

if has_errors and attempt < max_attempts - 1:
    attempt += 1
    retry_manager.record_retry()
    retry_state["total_retries"] = retry_manager._total_retries
    delay = retry_manager.get_retry_delay(attempt)
    logger.warning(
        f"Tool error detected, retrying ({attempt}/{max_attempts}) "  # ← No details!
        f"after {delay:.1f}s"
    )
```

**Information available but NOT logged:**
- `last_message.tool_calls` - contains tool names and arguments
- `tool_messages` - contains the actual error content (e.g., "Error: File not found: plans/missing.md")

##### Error Path 2: Tool Execution Exceptions (lines 413-450)

When a tool throws an exception:

```python
# graph.py:413-414
except Exception as e:
    logger.error(f"Tool execution error (attempt {attempt + 1}): {e}")  # ← Has exception, but no tool name!
```

**Information available but partially logged:**
- Exception `e` is logged ✓
- Tool names are extracted (line 417-418) but NOT included in the log message ✗
- Tool arguments are in `tool_call` but NOT logged ✗

---

#### Evidence from Log Analysis

Examined the log file from job `19c4de92` (338,000+ lines):

**Pattern observed:** All 184 tool error messages follow the same generic format, making it impossible to identify:
1. Which tool(s) were failing
2. What caused the failures
3. Whether failures were clustered on specific tools

**Log context around errors:**
```
2026-01-09 10:06:18 - LLM response: 0 chars, 1 tool calls, 3506ms
2026-01-09 10:06:18 - [Iteration ?] error=False
2026-01-09 10:06:20 - Tool error detected, retrying (1/3) after 2.0s    ← Which tool? What error?
2026-01-09 10:06:22 - Tool error detected, retrying (2/3) after 4.3s    ← Same tool? Different?
2026-01-09 10:06:26 - [Iteration ?] error=False                          ← Retry succeeded? No indication
```

The LLM archiver logs show "1 tool calls" but the tool error logs don't show which tool it was.

---

#### Sub-Issues

##### 12.1 Tool Result Errors Missing Tool Name

**Location:** `graph.py:401-404`

**Problem:** When tool returns error content, the warning doesn't include tool name.

**Information available:**
```python
last_message.tool_calls  # [{"name": "read_file", "args": {"path": "..."}, "id": "..."}]
tool_messages            # [ToolMessage(content="Error: File not found: ...", ...)]
```

##### 12.2 Tool Result Errors Missing Error Content

**Location:** `graph.py:401-404`

**Problem:** The actual error message from the tool is not logged.

**Example:** Tool returns "Error: File not found: plans/missing.md" but log only shows "Tool error detected".

##### 12.3 Exception Errors Missing Tool Name in Log

**Location:** `graph.py:414`

**Problem:** Exception is logged but the specific tool name is not included.

**Contradiction:** Tool name IS extracted (line 417-418) for failure tracking but NOT added to the log message.

##### 12.4 No Retry Outcome Logging

**Location:** `graph.py:383-411`

**Problem:** After retries, there's no indication whether the final attempt succeeded or failed.

---

#### Tool Error Patterns

Tools return error strings that trigger `_is_tool_error()` (graph.py:538-587):

| Tool | Error Pattern Example |
|------|----------------------|
| `read_file` | `Error: File not found: {path}` |
| `write_file` | `Error writing file: {exception}` |
| `add_todo` | `Error adding todo: {exception}` |
| `search_files` | `Error: {str(e)}` |
| `extract_document_text` | `Error: {str(e)}` |

The `_is_tool_error()` function checks for prefixes like:
- `error:`, `failed:`, `exception:`
- `file not found`, `permission denied`
- `cannot`, `unable to`, `invalid`

---

#### Code Locations Summary

| Location | Issue |
|----------|-------|
| `graph.py:401-404` | #12.1, #12.2 - Tool result error logging missing name and content |
| `graph.py:414` | #12.3 - Exception logging missing tool name |
| `graph.py:383-411` | #12.4 - No retry outcome indication |
| `graph.py:538-587` | `_is_tool_error()` - error detection function (working correctly) |

---

#### Fix Plan

##### Fix 12.1 + 12.2 - Enhanced Tool Result Error Logging

```python
# In graph.py tools_node, lines 395-406
if has_errors and attempt < max_attempts - 1:
    attempt += 1
    retry_manager.record_retry()
    retry_state["total_retries"] = retry_manager._total_retries
    delay = retry_manager.get_retry_delay(attempt)

    # NEW: Extract tool names and error content
    tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
    error_contents = [
        m.content[:200] for m in tool_messages
        if isinstance(m, ToolMessage) and _is_tool_error(m.content)
    ]

    logger.warning(
        f"Tool error detected for {tool_names}, retrying ({attempt}/{max_attempts}) "
        f"after {delay:.1f}s. Error: {error_contents[0] if error_contents else 'unknown'}"
    )
    await asyncio.sleep(delay)
    continue
```

##### Fix 12.3 - Include Tool Name in Exception Log

```python
# In graph.py tools_node, line 413-414
except Exception as e:
    # Extract tool names for logging
    tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
    logger.error(
        f"Tool execution error for {tool_names} (attempt {attempt + 1}): {e}"
    )
```

##### Fix 12.4 - Log Retry Outcomes

```python
# After retry loop succeeds (around line 408)
if attempt > 0:
    logger.info(f"Tool retry succeeded after {attempt} attempts for {tool_names}")

return {
    **result,
    "tool_retry_state": retry_state,
}

# After all retries exhausted (around line 429)
logger.error(
    f"Tool execution failed for {tool_names} after {max_attempts} attempts: {str(e)}"
)
```

---

#### Impact

- **Debugging difficulty:** Cannot identify which tools are failing without parsing LLM archives
- **Hidden patterns:** Cannot detect if specific tools are consistently failing
- **Wasted investigation time:** 184 errors occurred, zero indication of what failed
- **No correlation:** Cannot correlate tool failures with specific operations
- **Missing metrics:** `retry_manager` tracks failures per tool but this isn't visible in logs

---

#### Fix Priority

| Sub-Issue | Priority | Effort | Impact |
|-----------|----------|--------|--------|
| 12.1 - Add tool name | P2 | Low | High - identifies failing tools |
| 12.2 - Add error content | P2 | Low | High - reveals error cause |
| 12.3 - Exception tool name | P2 | Low | Medium - consistency |
| 12.4 - Retry outcomes | P3 | Low | Low - nice to have |

**Recommended implementation order:** 12.1 → 12.2 → 12.3 → 12.4

---

### 13. Candidate Extractor Returns Zero (P0 - Architecture Issue)

**Investigation date:** 2026-01-09, updated 2026-01-09 (deep dive)

**Problem:** The `identify_requirement_candidates` tool returns 0 candidates even for documents containing clear requirements.

**Status:** Root cause identified as **fundamental architecture flaw**. Regex-based extraction cannot handle real-world German legal text.

#### Observed Behavior

In job `19c4de92-85dd-4b92-84d6-b7934d152b2a`, every extraction attempt returned 0:

```
2026-01-09 10:07:48 - Identified 0 candidates from 2 sentences
2026-01-09 10:08:19 - Identified 0 candidates from 2 sentences
2026-01-09 10:08:43 - Identified 0 candidates from 4 sentences
2026-01-09 10:08:54 - Identified 0 candidates from 3 sentences
2026-01-09 10:11:50 - Identified 0 candidates from 4 sentences
2026-01-09 10:13:01 - Identified 0 candidates from 4 sentences
```

**Total candidates extracted: 0** from a 44-page GoBD document with 77 chunks.

---

#### Root Cause Analysis (Deep Dive)

##### 13.1 German Patterns Exist But Are Overly Restrictive

**Location:** `candidate_extractor.py:21-68`

The extractor DOES include German patterns, but they require exact word combinations that don't match real legal text:

```python
# Pattern from candidate_extractor.py:28-33
"modal_verbs_de": [
    r"\b(?:muss|soll|sollte|wird|darf)\s+(?:sein|haben|bereitstellen|sicherstellen|gewährleisten)",
    # ↑ Requires "muss" followed IMMEDIATELY by one of these specific words
]
```

**Actual GoBD text examples that DON'T match:**

| Sentence | Why It Fails |
|----------|--------------|
| "Jede Buchung muss **im Zusammenhang** mit einem Beleg stehen" | "im" not in allowed list |
| "Bücher geführt werden **müssen** und..." | "müssen" ≠ "muss" (different conjugation) |
| "hat organisatorisch **sicherzustellen**, dass..." | Words between "hat" and verb |
| "eine Sammlung der Belege **notwendig ist**" | Wrong word order (pattern expects "ist notwendig") |

##### 13.2 Regex Cannot Handle German Grammar Complexity

**Problem:** German legal text uses:

1. **Flexible word order:** "notwendig ist" vs "ist notwendig"
2. **Verb-final constructions:** "...geführt werden müssen" (verb at end)
3. **Separated verb phrases:** "hat...sicherzustellen" with many words in between
4. **Multiple conjugations:** muss/müssen/musste/müssten
5. **Passive voice:** "wird...vorgenommen", "werden...aufbewahrt"

Regex patterns would need to be exponentially complex to handle these variations.

##### 13.3 Test Results: 0% Match Rate

Testing the existing German patterns against actual GoBD chunk content:

```python
# Sentences from chunks/chunk_040.txt
test_sentences = [
    "Der Steuerpflichtige hat organisatorisch und technisch sicherzustellen...",
    "Jede Buchung oder Aufzeichnung muss im Zusammenhang mit einem Beleg stehen",
    "...müssen über alle nachfolgenden Prozesse erhalten bleiben",
    "...Sammlung und Aufbewahrung der Belege notwendig ist",
]

# Result: ALL sentences → NO MATCH against German modal patterns
```

These are clear requirement statements that any human (or LLM) would identify, but the regex patterns cannot match them.

##### 13.4 Sentence Splitting Issues

**Location:** `candidate_extractor.py:432-461`

The sentence splitter uses pattern `r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])"` which:
- Fails on German text with mid-sentence capitals (proper nouns, legal references)
- Produces "2 sentences" from chunks containing clear paragraph breaks
- Doesn't handle hyphenated line breaks ("Ordnungs-\nmäßigkeit")

##### 13.5 Confidence Threshold Too High

**Location:** `candidate_extractor.py:159, 231`

Even IF a pattern matched:
- Needs score ≥ 0.15 to pass threshold check (one pattern match = 0.15)
- Needs confidence ≥ 0.6 to be included in results (min_confidence default)
- This means at minimum 4 different indicator types must match

For German text, even with working patterns, this is too restrictive.

---

#### Fundamental Architecture Problem

**The regex-based approach is fundamentally wrong for this task.**

| Aspect | Regex Approach | LLM Approach |
|--------|---------------|--------------|
| Language support | Must manually add patterns per language | Understands 100+ languages natively |
| Grammar handling | Cannot handle flexible word order | Understands semantics, not just syntax |
| Context awareness | None - pattern matches in isolation | Understands paragraph/document context |
| Maintenance | Patterns must be tuned per document type | Works out-of-box on new document types |
| False negatives | High (as observed: 0% match rate) | Low - LLM understands meaning |

---

#### Recommended Fix: LLM-Based Extraction

**Remove the regex-based `identify_requirement_candidates` tool entirely.** Instead:

1. **Document chunking** (keep existing tool) - splits document into ~500-2000 char chunks
2. **LLM reads each chunk** - agent uses `read_file("chunks/chunk_001.txt")`
3. **LLM extracts requirements** - agent uses its language understanding to identify requirements
4. **Agent writes candidates** - stores extracted requirements in `candidates/` directory

##### Implementation Approach

**Option A: Direct LLM Analysis (Recommended)**

Remove `identify_requirement_candidates` tool. The agent's instructions should guide it to:
1. Read each chunk sequentially
2. Identify requirement-like statements using its understanding
3. Write findings to workspace files

This leverages the LLM's:
- Native German language understanding
- Semantic comprehension of requirement language
- Context awareness within and across chunks

**Option B: LLM-Based Tool**

Create a new tool that uses an LLM call to analyze text:
```python
@tool
def extract_requirements_llm(chunk_path: str) -> str:
    """Use LLM to extract requirements from a chunk."""
    content = workspace.read_file(chunk_path)
    # Use a focused LLM call to extract requirements
    # Returns structured list of requirements found
```

This adds token cost but provides structured output.

---

#### Code Locations

| Location | Issue |
|----------|-------|
| `candidate_extractor.py:21-68` | Regex patterns too restrictive for German |
| `candidate_extractor.py:262-273` | Pattern matching logic with threshold |
| `candidate_extractor.py:432-461` | Sentence splitting fails on German text |
| `document_tools.py:232-272` | Tool wrapper that calls extractor |

---

#### Impact

- **Critical:** Core extraction functionality completely broken
- **Wasted resources:** 53 minutes, 300+ iterations, 0 requirements extracted
- **Blocking:** All downstream processing (validation, graph integration) has no data
- **Language limitation:** System cannot process non-English compliance documents

---

#### Fix Priority

| Sub-Issue | Priority | Recommendation |
|-----------|----------|----------------|
| 13.1-13.5 | N/A | Do not fix - replace entire approach |
| Architecture change | P0 | Remove regex tool, use LLM-based extraction |

**Recommended approach:** Update `creator_instructions.md` to have the agent read chunks directly and extract requirements using its language understanding, rather than relying on the broken `identify_requirement_candidates` tool.

---

### 14. MongoDB Archiver Missing Data & Observability Gap (P2) - RESOLVED

**Investigation date:** 2026-01-09, updated 2026-01-09 (deep dive), **RESOLVED 2026-01-09**

**Problem:** The MongoDB LLM archiver stores requests but tool names and token counts are empty/missing. More broadly, the current architecture only captures LLM calls, not the complete agent execution history.

**Status:** **RESOLVED**. Implemented new `agent_audit` collection with comprehensive step tracking.

**Fix Applied:**
- Extended `LLMArchiver` class with audit methods: `audit_step()`, `audit_tool_call()`, `audit_tool_result()`, `get_job_audit_trail()`, `get_audit_stats()`
- Added audit hooks to all 4 graph nodes: `initialize_node`, `process_node`, `tools_node`, `check_node`
- Created `agent_audit` collection in MongoDB with 7 indexes for efficient querying
- Added `--audit`, `--step-type`, `--timeline` flags to `view_llm_conversation.py`
- Step types captured: `initialize`, `llm_call`, `llm_response`, `tool_call`, `tool_result`, `check`, `error`

**Files modified:** `llm_archiver.py`, `graph.py`, `init_mongodb.py`, `view_llm_conversation.py`

#### Observed Behavior

```python
# Query results from MongoDB
iter=0, tools=[], input=None, output=None
iter=1, tools=[], input=None, output=None
iter=243, tools=[], input=None, output=None
```

All 244 LLM requests have:
- `response.tool_calls`: empty array `[]`
- `input_tokens`: field doesn't exist (expected from `usage_metadata`)
- `output_tokens`: field doesn't exist (expected from `usage_metadata`)

But the log shows tool calls were made:
```
2026-01-09 10:05:41 - LLM response: 0 chars, 1 tool calls, 8494ms
```

---

#### Root Cause Analysis (Deep Dive)

Investigation revealed **6 sub-issues** across two categories: data capture bugs and architectural gaps.

##### Category A: Data Capture Bugs in LLM Archiver

##### 14.1 Token Counts Not Extracted from LLM Response

**Location:** `llm_archiver.py:226-237`

The archiver stores character counts, not actual token counts:

```python
# Current implementation (llm_archiver.py:226-237)
doc["metrics"] = {
    "input_chars": total_input_chars,      # Character-based approximation
    "output_chars": response_chars,         # Character-based approximation
    "tool_calls": len(response.tool_calls) if hasattr(response, "tool_calls") and response.tool_calls else 0,
}
```

**Problem:** LangChain AIMessage has `usage_metadata` with actual token counts from the LLM, but this is never extracted:

```python
# Available but not used:
response.usage_metadata.input_tokens   # Actual prompt tokens from LLM
response.usage_metadata.output_tokens  # Actual completion tokens from LLM
response.usage_metadata.total_tokens   # Total tokens used
```

**Note:** `usage_metadata` availability depends on the LLM backend. Local LLMs via llama.cpp may not populate this field, while OpenAI API always does.

##### 14.2 Tool Calls Format Mismatch

**Location:** `llm_archiver.py:58-66`

The `_message_to_dict` function expects tool_calls as dicts with `.get()`:

```python
# Current implementation (llm_archiver.py:58-66)
if hasattr(msg, "tool_calls") and msg.tool_calls:
    result["tool_calls"] = [
        {
            "id": tc.get("id", ""),     # Assumes tc is a dict
            "name": tc.get("name", ""),
            "args": tc.get("args", {}),
        }
        for tc in msg.tool_calls
    ]
```

**Problem:** LangChain's `AIMessage.tool_calls` can be:
1. List of `ToolCall` TypedDicts (works with `.get()`)
2. List of `ToolCall` objects with attributes (needs `.id`, `.name`, `.args`)
3. Empty or None depending on LLM backend

For local LLMs (llama.cpp via OpenAI-compatible API), the tool_calls format may differ from OpenAI's native format, causing `.get()` to fail silently and return empty values.

##### 14.3 No Tool Execution Archiving

**Location:** `graph.py:360-465` (`tools_node`)

Tool executions are NOT archived at all. The `tools_node` has rich data:

| Available Data | Currently Captured | Should Be Captured |
|----------------|-------------------|-------------------|
| Tool name | ✗ | ✓ |
| Tool arguments | ✗ | ✓ |
| Tool result | ✗ | ✓ |
| Execution time | ✗ | ✓ |
| Error/success | ✗ | ✓ |
| Retry attempts | ✗ | ✓ |

The only archiving happens in `process_node` for LLM calls.

---

##### Category B: Architectural Gaps

##### 14.4 Single Collection Insufficient for Full Audit

**Location:** `llm_archiver.py` (single `llm_requests` collection)

The current architecture stores only LLM requests. For complete agent observability, we need to capture:

| Step Type | Current | Needed |
|-----------|---------|--------|
| LLM request/response | ✓ (partial) | ✓ |
| Tool execution | ✗ | ✓ |
| Tool result | ✗ | ✓ |
| State transitions | ✗ | ✓ |
| Errors and retries | ✗ | ✓ |
| Check node decisions | ✗ | ✓ |

##### 14.5 No Unified Step Tracking

The agent graph has 4 nodes (`initialize`, `process`, `tools`, `check`) but only `process` archives anything. This makes it impossible to:
- Reconstruct exact agent behavior
- Understand why the agent made specific decisions
- Correlate LLM calls with tool executions
- Debug failures in tool execution

##### 14.6 Silent Failures on Archive

**Location:** `llm_archiver.py:255-257`

Archive failures are logged as warnings but don't surface to the caller:

```python
except Exception as e:
    logger.warning(f"Failed to archive LLM request: {e}")
    return None  # Silently fails
```

If MongoDB is unavailable or archiving fails, there's no indication in the agent's behavior or final report.

---

#### Recommended Solution: Agent Audit Collection

Instead of patching the existing archiver, create a new comprehensive audit system that captures every agent step.

##### New Collection: `agent_audit`

```javascript
// MongoDB document structure
{
    "job_id": "uuid",
    "iteration": 42,
    "step_number": 156,           // Global step counter within job
    "timestamp": ISODate(),
    "step_type": "llm_request" | "tool_execution" | "tool_result" | "state_change" | "error",

    // For LLM steps
    "llm": {
        "model": "gpt-oss-120b",
        "input_tokens": 15000,     // From usage_metadata if available
        "output_tokens": 500,
        "latency_ms": 8494,
        "tool_calls_requested": ["read_file", "write_file"],
        "content_preview": "Let me read the file..."  // First 200 chars
    },

    // For tool steps
    "tool": {
        "name": "read_file",
        "args": {"path": "chunks/chunk_001.txt"},
        "result_preview": "# Chunk 1\n...",  // First 500 chars
        "success": true,
        "latency_ms": 45,
        "attempt": 1,
        "max_attempts": 3
    },

    // For errors
    "error": {
        "type": "tool_error",
        "message": "File not found: plans/missing.md",
        "recoverable": true,
        "traceback": "..."
    },

    // Context snapshot (lightweight)
    "context": {
        "message_count": 72,
        "token_count": 42341,
        "phase": "identification"
    }
}
```

##### Audit Points in Graph

| Graph Node | Events to Audit |
|------------|-----------------|
| `initialize` | Job start, initial state, loaded tools |
| `process` | LLM request start, LLM response, token usage |
| `tools` | Each tool call start, result, errors, retries |
| `check` | Completion detection, error decisions, state changes |

##### Implementation Approach

**Option A: Extend LLMArchiver to AgentAuditor**

Add tool execution hooks alongside LLM archiving:

```python
class AgentAuditor:
    """Complete agent execution auditing."""

    def __init__(self, mongodb_url: str):
        self.llm_collection = db["llm_requests"]    # Keep for backwards compat
        self.audit_collection = db["agent_audit"]   # New comprehensive audit

    async def audit_llm_call(self, job_id, iteration, request, response, latency_ms):
        """Archive LLM call to both collections."""
        ...

    async def audit_tool_execution(self, job_id, iteration, tool_name, args, result, latency_ms, attempt):
        """Archive tool execution."""
        ...

    async def audit_state_change(self, job_id, iteration, change_type, details):
        """Archive state transitions."""
        ...

    async def audit_error(self, job_id, iteration, error_type, message, recoverable):
        """Archive errors."""
        ...
```

**Option B: Middleware Approach**

Create a LangGraph middleware that automatically audits all node transitions:

```python
def with_audit(node_fn: Callable) -> Callable:
    """Decorator to audit node execution."""
    async def wrapper(state):
        start_time = time.time()
        step_id = auditor.start_step(state["job_id"], node_fn.__name__)
        try:
            result = await node_fn(state)
            auditor.complete_step(step_id, result, time.time() - start_time)
            return result
        except Exception as e:
            auditor.fail_step(step_id, e)
            raise
    return wrapper

# Usage in graph.py
workflow.add_node("process", with_audit(process_node))
workflow.add_node("tools", with_audit(tools_node))
```

---

#### Code Locations Summary

| Location | Issue |
|----------|-------|
| `llm_archiver.py:226-237` | #14.1 - Token counts not extracted |
| `llm_archiver.py:58-66` | #14.2 - Tool calls format mismatch |
| `graph.py:360-465` | #14.3 - No tool execution archiving |
| `llm_archiver.py` | #14.4 - Single collection insufficient |
| `graph.py` all nodes | #14.5 - No unified step tracking |
| `llm_archiver.py:255-257` | #14.6 - Silent archive failures |

---

#### Existing View Script

`scripts/view_llm_conversation.py` can query the current `llm_requests` collection:

```bash
# View conversation for a job
python scripts/view_llm_conversation.py --job-id <uuid>

# Get statistics
python scripts/view_llm_conversation.py --job-id <uuid> --stats

# List all jobs
python scripts/view_llm_conversation.py --list
```

This script would need updates to also query the new `agent_audit` collection.

---

#### Impact

- **No complete audit trail:** Cannot reconstruct exact agent behavior
- **No tool metrics:** Cannot see which tools are slow, failing, or heavily used
- **Debugging blind spots:** 184 tool errors in job `19c4de92` with no MongoDB record
- **Cost tracking incomplete:** Character counts instead of actual token usage
- **Silent data loss:** Archive failures don't surface anywhere

---

#### Fix Priority

| Sub-Issue | Priority | Effort | Impact |
|-----------|----------|--------|--------|
| 14.1 - Extract token counts | P3 | Low | Medium - actual usage data |
| 14.2 - Fix tool calls format | P3 | Low | Medium - see tool calls in archive |
| 14.3 - Archive tool executions | P2 | Medium | High - complete execution trace |
| 14.4 - New audit collection | P2 | Medium | High - architectural improvement |
| 14.5 - Unified step tracking | P2 | Medium | High - full observability |
| 14.6 - Surface archive failures | P3 | Low | Low - nice to have |

**Recommended implementation order:**

```
1. Create agent_audit collection schema and indexes
2. Implement AgentAuditor class with audit methods
3. Add tool execution auditing in tools_node
4. Add state change auditing in check_node
5. Fix token count extraction (#14.1)
6. Fix tool calls format (#14.2)
7. Update view script to show audit data
```

---

## Test Jobs

| Job ID | Date | Status | Issues Encountered |
|--------|------|--------|-------------------|
| `da93d6ff-2a7f-462d-ace8-5732efe513e0` | 2026-01-08 | Completed (synthetic) | #4 Path resolution, missing instructions.md |
| `444d2524-5f43-4c53-91d1-863dd9e69247` | 2026-01-08 | Completed (incomplete) | #1 Async tool bug, #2 tools don't persist |
| `19c4de92-85dd-4b92-84d6-b7934d152b2a` | 2026-01-09 | **Failed** (recursion limit) | #10, #11, #12, #13, #14 - Context overflow, full doc read, tool errors, zero candidates |

---

## Fix Priority

### Resolved

| Tier | Issues | Status |
|------|--------|--------|
| **Tier 1: Pipeline Broken** | #1 Async invocation, #2 Document persistence | **FIXED** |
| **Tier 2: Reliability** | #3 Iterations, #5 Resume, #6 Completion | **FIXED** |
| **Tier 3: Quality** | #7 Candidate extractor, #8 Web search guidance | **FIXED** |
| **Tier 3: Observability** | #14 Agent audit collection | **FIXED** |
| **Tier 4: Infrastructure** | #9 PostgreSQL schema cleanup | **FIXED** |

### Open

| Tier | Issues | Status |
|------|--------|--------|
| **Tier 0: Architecture** | #13 Candidate extractor (5 sub-issues) | **Open - Architecture Change Required** |
| **Tier 1: Pipeline Broken** | #10 Context overflow (6 sub-issues) | **Open - Critical** |
| **Tier 2: Context Management** | #11 Full document read (4 sub-issues) | **Open - High** |
| **Tier 3: Observability** | #12 Tool error logging (4 sub-issues) | **Open - Medium** |

### Fix Order

```
#1 (async) → #2 (persist) → Test pipeline works          ✓ DONE
    ↓
#5 (resume) → #6 (completion) → Test resume works        ✓ DONE
    ↓
#3 (iterations) → #8 (guidance) → Polish                 ✓ DONE
    ↓
#10 (server config) → Fix llama.cpp parallel config      ✓ DONE (user fixed)
    ↓
#13 (ARCHITECTURE) → Remove regex extraction, use LLM    ☐ TODO (P0 - BLOCKING)
    ↓
#10.1-10.2 (error handling) → Prevent infinite loops     ☐ TODO (P0)
    ↓
#10.3-10.4 (compaction) → Better context management      ☐ TODO (P1)
    ↓
#11.1 (expose path) → Stop inviting agent to read full   ☐ TODO (P1)
    ↓
#11.4 (instructions) → Add explicit chunk guidance       ☐ TODO (P1)
    ↓
#11.2 (max_read_size) → Reduce from 100KB to 10KB        ☐ TODO (P1)
    ↓
#11.3 (pre-check) → Add file size warning                ☐ TODO (P2)
    ↓
#10.5-10.6 (observability) → Track token/error state     ☐ TODO (P2)
    ↓
#12.1-12.2 (tool logging) → Add tool name and error content  ☐ TODO (P2)
    ↓
#12.3 (exception logging) → Add tool name to exceptions  ☐ TODO (P2)
    ↓
#12.4 (retry outcome) → Log retry success/failure        ☐ TODO (P3)
    ↓
#14.4 (agent_audit) → Create new audit collection         ☐ TODO (P2)
    ↓
#14.3 (tool archiving) → Archive tool executions          ☐ TODO (P2)
    ↓
#14.5 (unified tracking) → Add step tracking to all nodes ☐ TODO (P2)
    ↓
#14.1 (token counts) → Extract actual token usage         ☐ TODO (P3)
    ↓
#14.2 (tool calls format) → Fix format mismatch           ☐ TODO (P3)
    ↓
#14.6 (silent failures) → Surface archive failures        ☐ TODO (P3)
    ↓
#9 (database cleanup) → Clean infrastructure             ✓ DONE
```

**Issues discovered 2026-01-09 from job 19c4de92:**
- #10: Context overflow infinite loop (P0) - server issue RESOLVED, 6 code sub-issues open
- #11: Agent reads full document instead of chunks (P1) - 4 sub-issues identified
- #12: Tool error logging insufficient (P2) - 4 sub-issues identified (two error paths in tools_node)
- #13: **Candidate extractor architecture flaw (P0)** - regex approach fundamentally broken, needs LLM-based extraction
- #14: **Agent audit gap (P2)** - 6 sub-issues identified (token counts, tool calls format, no tool archiving, single collection, no unified tracking, silent failures). Recommended new `agent_audit` collection for complete execution tracing.

Files modified for resolved issues:
- `graph.py`: #1, #3, #6
- `document_tools.py`: #2, #4
- `run_universal_agent.py`: #5
- `agent.py`: #5
- `system_prompt.md`: #8
- `schema.sql`: #9 (renamed `requirement_cache` → `requirements`, removed 4 deprecated tables)
- `postgres_utils.py`: #9 (removed 6 dead functions)
- `__init__.py`: #9 (updated exports)
- `reporter.py`: #9 (updated table references)
- `cache_writer.py`, `cache_reader.py`: #9 (updated table references)
- `cache_tools.py`: #9 (updated table references)
- `init_db.py`: #9 (updated table verification)
- Deleted: `schema_vector.sql`, `checkpoint.py`, `workspace.py`, `vector.py`: #9
