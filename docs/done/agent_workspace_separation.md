# Agent/Workspace Pod Separation: Tool Locality Issues

> **Status**: All identified issues resolved. Browser uses CDP, paper
> downloads use local-temp-then-transfer, citation/KB/document tools use
> workspace backend abstraction. See individual sections for details.

## Problem

With the move to separate pods for the agent (LangChain loop + LLM API calls) and the workspace (sandboxed execution environment accessed via SSH/SFTP), several tools still assume they share a filesystem. The agent pod runs tools locally that produce side effects on the local disk, but the actual workspace lives on a different pod entirely.

This is a recurring class of issue. We already hit it with git tools (committing on the agent pod where no workspace files existed), and now the same pattern has surfaced with browser/Playwright tools.

## Architecture Context

```
┌─────────────────────┐         SSH/SFTP          ┌─────────────────────┐
│     Agent Pod        │ ◄──────────────────────►  │   Workspace Pod     │
│                      │    (RemoteBackend)         │                     │
│  - Python agent loop │                            │  - SSH server       │
│  - LangChain tools   │                            │  - tmux, git, node  │
│  - Playwright/Chrome │                            │  - code-server      │
│  - LLM API calls     │                            │  - Job files live   │
│                      │                            │    here             │
└─────────────────────┘                             └─────────────────────┘
```

The separation is intentional and correct: the "agent" is just a loop making API calls, the workspace is where commands execute and files live. But the LangChain tool ecosystem was built assuming a single-machine model.

## Known Affected Tools

### 1. Browser/Playwright Downloads (Current)

**File:** `src/tools/research/browser.py`

`download_from_website` sets Playwright's `downloads_path` to `workspace_manager.get_path("documents")`. When using `RemoteBackend`, this resolves to a remote path string (e.g., `/home/agent-host/workspace/job_<uuid>/documents`). But Playwright runs locally on the agent pod and interprets that string as a local filesystem path. The downloaded file either:

- Lands in an accidental local directory on the agent pod (if the path happens to be creatable)
- Errors out because the path doesn't exist locally

Either way, the file never reaches the workspace pod. The subsequent `_find_new_files` check also scans the wrong filesystem.

### 2. Git Tools (Previously Fixed)

Git operations were running on the agent pod where no workspace files existed. `git commit` would commit nothing because the working tree was empty from the agent pod's perspective. Fixed by routing git commands through `RemoteBackend`'s shell execution (runs on workspace pod via SSH).

### 3. Paper Downloads — arXiv, Unpaywall, URL-based (Resolved)

**Severity:** HIGH

**Files:**
- `src/tools/research/papers.py` (lines 69–73, 129, 415, 434)
- `src/tools/research/utils/arxiv_client.py` (lines 70–73)
- `src/tools/research/utils/unpaywall_client.py` (lines 119–121)
- `src/tools/research/workflow.py` (lines 249–250, 317–319)

`_get_documents_dir()` in `papers.py` returns `workspace_manager.get_path("documents")`, which resolves to a remote path on `RemoteBackend`. This path is then passed as `dest_dir` to the arXiv and Unpaywall download clients, which treat it as a local filesystem path:

- **arXiv**: `dest_dir.mkdir(parents=True, exist_ok=True)` + `result.download_pdf(dirpath=str(dest_dir))` — the `arxiv` library writes to a local path that doesn't exist on the agent pod.
- **Unpaywall**: `dest_dir.mkdir()` + `path.write_bytes(content)` — same pattern, HTTP response body written to a nonexistent local path.
- **workflow.py**: `_download_available_papers()` calls `dest_dir.mkdir()` and `_download_single_url()` calls `path.write_bytes(content)` — direct local writes.

Additionally, `_try_browser_download()` in `papers.py` passes `dest_dir` to `_get_browser_config()` (correctly ignored in remote CDP mode), but then calls local `_find_new_files(dest_dir)` on line 434 instead of `_find_new_files_remote()`. The browser fallback downloads to the workspace correctly via CDP, but the agent can't detect the file afterwards and reports failure.

**Impact:** `download_paper` and `research_topic` tools silently fail or write to nonexistent local paths on remote workspaces. Browser fallback downloads succeed but are reported as failures.

**Fix:** Download to a local `tempfile.TemporaryDirectory()`, then transfer via `backend.write_file()`. For the browser fallback, detect remote mode and use `_find_new_files_remote()`.

### 4. Citation Bibliography Generation (Resolved)

**Severity:** HIGH

**File:** `src/tools/citation/sources.py` (lines 976–1024)

`generate_bibliography()` calls `workspace.get_path(output_path)` and then performs direct local filesystem operations on the resolved path:

```python
resolved = workspace.get_path(output_path)
resolved.parent.mkdir(parents=True, exist_ok=True)    # local mkdir on remote path
if resolved.exists():                                   # local existence check
    existing_content = resolved.read_text(encoding="utf-8")  # local read
    with open(resolved, "a", encoding="utf-8") as f:   # local append
        f.write(append_text)
else:
    with open(resolved, "w", encoding="utf-8") as f:   # local write
        f.write(bibliography + "\n")
```

None of these go through the workspace backend.

**Impact:** `generate_bibliography` with an output file path fails on remote workspaces — the file is either created on the agent pod (if the path happens to be creatable) or errors out.

**Fix:** Build the content in memory, then use `workspace.write_file()` / `workspace.read_file()` / `workspace.file_exists()` instead of direct `Path` / `open()` operations.

### 5. Knowledge Base Export (Resolved)

**Severity:** HIGH

**File:** `src/tools/knowledge/knowledge_tools.py` (lines 730–780)

`kb_export()` writes exported notes as markdown files using direct `Path` operations:

```python
export_dir = Path(path)
export_dir.mkdir(parents=True, exist_ok=True)
# ...
(export_dir / filename).write_text(file_content)
```

The path argument comes from the agent and is interpreted as a workspace-relative path, but the actual file I/O runs locally on the agent pod.

**Impact:** Exported knowledge notes land on the agent pod filesystem, never reaching the workspace where the agent expects them.

**Fix:** Use `workspace.write_file()` for each exported note.

### 6. Document Chunking / Loading (Resolved)

**Severity:** MEDIUM

**File:** `src/tools/document/processing.py` (lines 33–107)

`_load_document()` passes workspace-resolved paths to LangChain document loaders that expect local files:

- `PyPDFLoader(str(path))` — opens PDF from local disk
- `Docx2txtLoader(str(path))` — opens DOCX from local disk
- `TextLoader(str(path))` — opens text file from local disk
- `open(path, "r")` — fallback plain-text read

The `chunk_document` tool resolves paths via `workspace.get_path()`, which returns a remote path on `RemoteBackend`. The loaders then fail because the file doesn't exist locally.

Note: the `defer_to_workspace: True` metadata flag on this tool is only used for description shortening in the tool registry — it does **not** route execution to the workspace.

**Impact:** `chunk_document` fails on any file that only exists on the workspace pod.

**Fix:** Read the file from the workspace via `backend.read_file()` (or `backend.read_file_bytes()` for PDFs) into a local `tempfile`, then pass the temp path to the LangChain loaders.

### 7. Remaining — Not Affected

The following tool groups were audited and are **not affected**:

- **Workspace tools** (`src/tools/workspace/`) — all operations route through the backend abstraction.
- **Shell / coding tools** (`src/tools/coding/`) — command execution routes through the backend.
- **Git tools** (`src/tools/git/`) — previously fixed to run on the workspace via `exec_command`.
- **Communication tools** (`src/tools/communication/`) — message persistence goes through the workspace.
- **Web content tools** (`src/tools/research/web.py`) — `save_web_content_to_disk()` uses `context` methods that respect the backend.
- **Core tools** (`src/tools/core/`) — todo and job management use workspace API correctly.
- **Database tools** (`src/tools/sql/`, `src/tools/mongodb/`, `src/tools/graph/`) — communicate with databases over the network, no local filesystem I/O.

## Root Cause

The core tension: `WorkspaceManager.get_path()` returns a `Path` object. When the backend is `RemoteBackend`, this path points to the remote filesystem. Any code that passes this path to a library running locally on the agent pod (Playwright, PDF tools, etc.) will operate on the wrong filesystem.

The `WorkspaceBackend` abstraction works correctly for file operations that go through it (`read_file`, `write_file`, `list_dir`, etc.). The problem is tools that bypass the backend and hand paths directly to third-party libraries.

## Open Question: Is LangChain the Right Boundary?

The separation makes sense conceptually, but the question is whether the LangChain tool stack is designed for this split. Specific concerns:

1. **Playwright/browser-use**: The `browser-use` library wraps Playwright and expects the browser to run in the same process. Can we decouple the browser from the LangChain agent and run it elsewhere? Options:
   - Run Playwright on the workspace pod (but the workspace intentionally excludes heavy dependencies)
   - Run a headless browser as a separate sidecar/service and interact via CDP (Chrome DevTools Protocol)
   - Use the existing Playwright MCP server (`mcp__plugin_playwright_playwright`) instead of the LangChain browser-use tool

2. **General pattern**: Every LangChain tool that does local I/O is a potential separation violation. The backend abstraction covers our own code, but third-party libraries (browser-use, PDF parsers, etc.) don't know about it.

3. **Architectural alternatives**:
   - **Thin agent, fat workspace**: Move more execution into the workspace pod. The agent pod becomes a pure API-call loop with no local tooling. Tools that need filesystem access run on the workspace pod (via shell or a tool-runner sidecar).
   - **Shared volume**: Mount a shared PVC between agent and workspace pods. Simple but defeats the isolation model and creates coupling.
   - **Transfer layer**: After any local tool produces a file, explicitly SFTP it to the workspace. Works but is fragile and needs to be added per-tool.
   - **Replace browser-use with MCP Playwright**: We already have a Playwright MCP server. If the agent dispatches browser tasks through MCP instead of LangChain's browser-use, the download path problem becomes the MCP server's responsibility.

## Immediate Impact

- ~~`download_from_website` tool is broken when using `RemoteBackend`~~ → **Fixed** (CDP remote browser)
- `download_paper` and `research_topic` tools silently fail on remote workspaces (paper downloads land nowhere)
- `generate_bibliography` with a file output path fails on remote workspaces
- `kb_export` writes notes to the agent pod, not the workspace
- `chunk_document` fails for any file that only exists on the workspace pod
- Browser fallback in `download_paper` downloads correctly via CDP but reports failure (wrong file detection path)

## Suggested Next Steps

1. ~~**Audit**: Grep for all uses of `workspace_manager.get_path()` where the result is passed to a third-party library~~ → **Done**
2. ~~**Short-term fix for browser**: Remote Chromium via CDP~~ → **Done** (see `browser.py`)
3. ~~**Fix paper downloads**~~ → **Done**: `papers.py` downloads to local tempdir, transfers via `backend.write_file()`. Browser fallback uses `_find_new_files_remote()` when remote.
4. ~~**Fix citation bibliography**~~ → **Done**: `generate_bibliography()` now uses `workspace.write_file()`/`read_file()`/`exists()`/`append_file()`.
5. ~~**Fix KB export**~~ → **Done**: `kb_export()` now uses `workspace.write_file()` and `workspace.create_directory()`.
6. ~~**Fix document chunking**~~ → **Done**: `_local_copy()` context manager fetches remote files to local tempfile before passing to LangChain loaders.
7. **Longer-term**: The `_local_copy()` pattern in `document/processing.py` could be promoted to a shared utility if more tools need the same fetch-to-tempfile pattern.
