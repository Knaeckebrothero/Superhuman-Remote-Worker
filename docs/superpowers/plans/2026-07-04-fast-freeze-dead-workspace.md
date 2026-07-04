# Fast-Freeze on a Dead Workspace — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a workspace (pod or VM) goes unreachable mid-run, every backend-touching tool must raise `WorkspaceUnavailableError` so the job/turn freezes cleanly as `workspace_unavailable` — in seconds, detected by exception type, on both the job graph and the chat graph.

**Architecture:** Two levers. (A) *Stop the flatten* — tool-layer `except Exception` handlers currently swallow `WorkspaceUnavailableError` into a plain string, so it never propagates; we guard each to re-raise, switch `graph.py`'s `ToolNode` to a callable that re-raises the type (replacing a fragile substring match), and patch the separate chat loop in `persistent_graph.py`. (B) *Make the freeze fast* — `RemoteBackend.connect()`/`_ensure_connected()` currently retry `max_retries²` (~15 min) identically for a *gone* pod and a *booting* one; we de-nest the retry and classify the cause to fail fast on "gone".

**Tech Stack:** Python 3.12 (CI gate), paramiko (SSH/SFTP), LangGraph 1.0.6 (`ToolNode`), LangChain tools, pytest + `unittest.mock`.

**Spec:** `docs/issues/agent_fast_freeze_on_dead_workspace.md` (Tier B). Canonical chain: `docs/issues/reviewing_parent_pod_reaped_under_critic.md`.

## Global Constraints

- Work directly on `develop`. Commit per task. **NEVER push** without explicit user authorization.
- CI (Python 3.12) is the correctness gate; the local env is Python 3.14 and env-noisy — run tests locally best-effort, but write them to pass on 3.12.
- `ruff format` + `ruff check` must be clean (the push workflow runs ruff and rewrites SHAs).
- Behavior contract (already wired downstream — do NOT rebuild): on the job path, a `WorkspaceUnavailableError` escaping `self._graph.ainvoke` is caught at `src/agent.py:1012` → `error_state{"type": "workspace_unavailable", "recoverable": True, "should_stop": True}` → consumed by `orchestrator/main.py:10589` (pod PVC-reattach, cap 3, then fail loud). On the chat path, the turn handler at `src/persistent_graph.py:645-671` catches it → `callbacks.on_error(...)`.
- `WorkspaceUnavailableError` lives at `src/core/workspace_backend.py:14`.
- Retry classification (exact, from spec): `socket.gaierror` / `OSError` errno `EHOSTUNREACH`|`ENETUNREACH`|`ENETDOWN` = **gone** → 0 retries (raise on first failure); `ConnectionRefusedError` = **booting** → full `max_retries`; `socket.timeout`/`TimeoutError`/`paramiko.SSHException`/other = **ambiguous** → cap at 2.

---

### Task 1: De-nest the connect retry (`_ensure_connected`)

Removes the `max_retries²` blow-up: `connect()` already owns a retry loop; `_ensure_connected()` must call it once, not wrap it in a second loop.

**Files:**
- Modify: `src/core/backends/remote.py:273-292` (`_ensure_connected`)
- Test: `tests/test_workspace_backends.py` (class `TestRemoteBackendEnsureConnected`)

**Interfaces:**
- Consumes: `RemoteBackend.connect()` (raises `WorkspaceUnavailableError` when its own retry budget is exhausted), `RemoteBackend.is_connected() -> bool`.
- Produces: `RemoteBackend._ensure_connected() -> None` — reconnects via a single `connect()` call; total `paramiko.SSHClient.connect` attempts on a dead host == `max_retries`, not `max_retries²`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workspace_backends.py` in `class TestRemoteBackendEnsureConnected`:

```python
def test_ensure_connected_does_not_multiply_retry_budget(self, remote_backend):
    """_ensure_connected must not wrap connect()'s own retry loop in a second
    loop — a dead host should cost max_retries attempts, not max_retries²."""
    backend, mock_ssh, mock_sftp = remote_backend  # fixture: max_retries=2
    backend._ssh = None  # force is_connected() False → reconnect path
    mock_ssh.connect.side_effect = socket.error("host down")

    with patch("time.sleep"):
        with pytest.raises(WorkspaceUnavailableError):
            backend._ensure_connected()

    # max_retries=2 → 2 attempts. The nested bug produced 2*2 = 4.
    assert mock_ssh.connect.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workspace_backends.py::TestRemoteBackendEnsureConnected::test_ensure_connected_does_not_multiply_retry_budget -v`
Expected: FAIL — `call_count == 4` (nested loop).

- [ ] **Step 3: Write minimal implementation**

Replace `_ensure_connected` (`src/core/backends/remote.py:273-292`) with:

```python
    def _ensure_connected(self) -> None:
        """Reconnect if the SSH connection is dead.

        connect() owns the ENTIRE retry budget (attempts, backoff, cause
        classification). This method must NOT wrap it in a second retry loop —
        the nested loops multiplied the budget to max_retries² and turned a
        dead-workspace call into a ~15-min stall.
        See docs/issues/agent_fast_freeze_on_dead_workspace.md.
        """
        if self.is_connected():
            return
        logger.warning(f"SSH connection to {self._host} lost, reconnecting...")
        self.connect()
        logger.info(f"Reconnected to {self._host}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workspace_backends.py::TestRemoteBackendEnsureConnected -v`
Expected: PASS (new test + existing ensure-connected tests).

- [ ] **Step 5: Commit**

```bash
git add src/core/backends/remote.py tests/test_workspace_backends.py
git commit -m "fix(remote): de-nest _ensure_connected retry (kill max_retries² connect storm)"
```

---

### Task 2: Classify connect errors + rename VM→workspace strings

Makes the freeze *fast*: fail immediately when the workspace is *gone* (DNS won't resolve / no route), keep the full boot-window budget only for `ConnectionRefused`. Also renames the transport-identity "VM" strings that misled triage (the connect error string, plus the two log lines).

**Files:**
- Modify: `src/core/backends/remote.py:197-239` (`connect`), `:265` (`disconnect` log), and add a module-level classifier + constant near the top of the class file.
- Test: `tests/test_workspace_backends.py` (class `TestRemoteBackendConnect`)

**Interfaces:**
- Consumes: `self._max_retries`, `self._connect_timeout`, module `socket`, `errno`, `paramiko`.
- Produces: `_classify_connect_error(e: Exception) -> str` returning `"gone"` | `"booting"` | `"ambiguous"`; `RemoteBackend.connect()` raising `WorkspaceUnavailableError` with a message containing `"workspace"` (never `"VM"`), after `1` attempt for gone / `max_retries` for booting / `min(max_retries, 2)` for ambiguous.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workspace_backends.py`. Put these imports at the top of the file if absent: `import errno`, `import socket` (socket is already imported per existing tests).

```python
class TestRemoteBackendConnectClassification:
    """connect() classifies the failure cause and sizes the retry budget."""

    def _backend_with_retries(self, retries):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        with patch("paramiko.SSHClient", return_value=mock_ssh):
            backend = RemoteBackend(
                host="10.0.0.42", workspace_path="/ws",
                connect_timeout=5, max_retries=retries,
            )
        return backend, mock_ssh

    def test_gone_dns_fails_fast_no_retry(self):
        """gaierror (NXDOMAIN) = pod gone → raise on the first attempt."""
        backend, mock_ssh = self._backend_with_retries(5)
        mock_ssh.connect.side_effect = socket.gaierror(
            socket.EAI_NONAME, "Name or service not known"
        )
        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
        assert mock_ssh.connect.call_count == 1

    def test_no_route_fails_fast(self):
        """OSError EHOSTUNREACH = no route → gone → fail fast."""
        backend, mock_ssh = self._backend_with_retries(5)
        mock_ssh.connect.side_effect = OSError(errno.EHOSTUNREACH, "No route to host")
        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
        assert mock_ssh.connect.call_count == 1

    def test_connection_refused_uses_full_boot_budget(self):
        """ECONNREFUSED = sshd booting → keep the full max_retries budget."""
        backend, mock_ssh = self._backend_with_retries(5)
        mock_ssh.connect.side_effect = ConnectionRefusedError(
            errno.ECONNREFUSED, "Connection refused"
        )
        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
        assert mock_ssh.connect.call_count == 5

    def test_timeout_capped_at_two(self):
        """Ambiguous (timeout) → short cap, not the full budget."""
        backend, mock_ssh = self._backend_with_retries(5)
        mock_ssh.connect.side_effect = socket.timeout("timed out")
        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
        assert mock_ssh.connect.call_count == 2

    def test_message_says_workspace_not_vm(self):
        """Renamed error string: 'workspace', never 'VM'."""
        backend, mock_ssh = self._backend_with_retries(1)
        mock_ssh.connect.side_effect = socket.gaierror(
            socket.EAI_NONAME, "Name or service not known"
        )
        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError) as exc:
                backend.connect()
        assert "workspace" in str(exc.value)
        assert "VM" not in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workspace_backends.py::TestRemoteBackendConnectClassification -v`
Expected: FAIL — gone cases retry 5× (no classification); message contains "VM".

- [ ] **Step 3: Write minimal implementation**

In `src/core/backends/remote.py`, add near the top of the module (after imports, before the class) a constant and classifier:

```python
import errno  # add to the existing import block if not present

# Connect-failure buckets → how many attempts each is worth.
_AMBIGUOUS_RETRY_CAP = 2
_GONE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN}


def _classify_connect_error(e: Exception) -> str:
    """Bucket an SSH connect failure to size the retry budget.

    'gone'      → the workspace host is destroyed (DNS won't resolve / no route);
                  retrying is pointless, fail fast.
    'booting'   → host is up but sshd is not listening yet (ECONNREFUSED);
                  this is the boot window the retries exist for.
    'ambiguous' → timeout / protocol / unknown; retry briefly then give up.
    """
    if isinstance(e, socket.gaierror):
        return "gone"
    if isinstance(e, ConnectionRefusedError):
        return "booting"
    if isinstance(e, OSError) and e.errno in _GONE_ERRNOS:
        return "gone"
    return "ambiguous"
```

Replace `connect()` (`src/core/backends/remote.py:197-239`) with:

```python
    def connect(self) -> None:
        """Establish SSH connection and SFTP channel.

        Retries to tolerate the window between daemon registration and sshd
        readiness, but classifies the failure (``_classify_connect_error``) so a
        workspace that is *gone* (DNS won't resolve / no route) fails fast
        instead of burning the full boot-window budget.
        See docs/issues/agent_fast_freeze_on_dead_workspace.md.
        """
        connect_kwargs = {
            "hostname": self._host,
            "port": self._port,
            "username": self._username,
            "timeout": self._connect_timeout,
        }
        if self._key_path:
            connect_kwargs["key_filename"] = self._key_path

        backoff = 2.0
        attempt = 0
        while True:
            attempt += 1
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                self._ssh.connect(**connect_kwargs)
                break
            except (paramiko.SSHException, socket.error, OSError) as e:
                bucket = _classify_connect_error(e)
                if bucket == "gone":
                    effective_max = 1
                elif bucket == "ambiguous":
                    effective_max = min(self._max_retries, _AMBIGUOUS_RETRY_CAP)
                else:  # booting
                    effective_max = self._max_retries
                if attempt >= effective_max:
                    raise WorkspaceUnavailableError(
                        f"Failed to connect to workspace "
                        f"{self._host}:{self._port} after {attempt} attempt(s) "
                        f"[{bucket}]: {e}"
                    ) from e
                logger.warning(
                    "SSH connect attempt %d/%d to %s:%d failed [%s] (%s), "
                    "retrying in %.0fs",
                    attempt, effective_max, self._host, self._port,
                    bucket, e, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

        self._sftp = self._ssh.open_sftp()
        logger.info(f"Connected to workspace {self._host}:{self._port}")
```

Then rename the `disconnect()` log line at `src/core/backends/remote.py:265`:

```python
        logger.info(f"Disconnected from workspace {self._host}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_workspace_backends.py::TestRemoteBackendConnectClassification tests/test_workspace_backends.py::TestRemoteBackendConnect -v`
Expected: PASS. Note the existing `test_connect_raises_after_max_retries` uses a bare `socket.error("no route to host")` (no errno) → classified `ambiguous`, cap `min(2, 2)=2`, so its `call_count == 2` assertion still holds.

- [ ] **Step 5: Update the existing rename-adjacent assertion**

If `test_connect_raises_after_max_retries` asserts `match="Failed to connect"`, that still matches the new string — no change required. Confirm no test asserts the literal `"VM"`. Run:

```bash
grep -n "to connect to VM\|from VM" tests/test_workspace_backends.py
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/core/backends/remote.py tests/test_workspace_backends.py
git commit -m "fix(remote): classify connect failures, fail fast on gone workspace; rename VM→workspace"
```

---

### Task 3: Un-swallow `WorkspaceUnavailableError` in the tool layer

Every tool that touches the backend wraps it in a blanket `except Exception → return "Error: {e}"`, flattening the workspace death into an ordinary tool result. Guard each so the type re-raises. With the *old* `graph.py` still in place (handle_tool_errors=True + substring match), a re-raised error is still caught (via `repr(e)`), so there is no broken window before Task 4.

**Files:**
- Modify: `src/tools/workspace/filesystem.py` (every `@tool` handler's terminal `except Exception`), `src/tools/workspace/files.py` (ditto), `src/tools/shell/shell_tools.py:~408` (`run_command`) and `:~550`,`~590` (`shell_execute`), `src/core/backends/remote.py:897` (`shell_run`).
- Test: `tests/test_workspace_tools_propagate_unavailable.py` (new)

**Interfaces:**
- Consumes: `WorkspaceUnavailableError`; the tool factories `create_filesystem_tools(context)`, `create_file_tools(context)` (both read `context.workspace_manager` + `context.get_config(key, default)`).
- Produces: each workspace/shell tool re-raises `WorkspaceUnavailableError` instead of returning a string; `RemoteBackend.shell_run()` propagates it instead of returning `"SSH connection lost during command execution: ..."`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workspace_tools_propagate_unavailable.py`:

```python
"""Workspace/shell tools must let WorkspaceUnavailableError propagate (not
flatten it into a string result), so a dead workspace freezes the job cleanly.
Regression guard for docs/issues/agent_fast_freeze_on_dead_workspace.md."""

import pytest

from src.core.workspace_backend import WorkspaceUnavailableError
from src.tools.workspace.filesystem import create_filesystem_tools
from src.tools.workspace.files import create_file_tools


class _DeadWorkspace:
    """Any method call raises WorkspaceUnavailableError (workspace gone)."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise WorkspaceUnavailableError("workspace gone")
        return _raise


class _FakeCtx:
    def __init__(self):
        self.workspace_manager = _DeadWorkspace()

    def get_config(self, key, default=None):
        return default


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_files_reraises_workspace_unavailable():
    tools = create_filesystem_tools(_FakeCtx())
    with pytest.raises(WorkspaceUnavailableError):
        _tool(tools, "list_files").invoke({"path": ""})


def test_read_file_reraises_workspace_unavailable():
    tools = create_file_tools(_FakeCtx())
    with pytest.raises(WorkspaceUnavailableError):
        _tool(tools, "read_file").invoke({"path": "x.txt"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workspace_tools_propagate_unavailable.py -v`
Expected: FAIL — tools return a string (`"Error ...: workspace gone"`), no exception raised. (If a tool needs a specific `get_config` default to reach the backend call, set it in `_FakeCtx.get_config`.)

- [ ] **Step 3: Add the import + guard, per file**

Add to the top of `src/tools/workspace/filesystem.py`, `src/tools/workspace/files.py`, and `src/tools/shell/shell_tools.py` (match each file's import style; `src.utils.pdf`-style absolute is used in these files):

```python
from src.core.workspace_backend import WorkspaceUnavailableError
```

Then, in **every** `@tool` handler in `create_filesystem_tools` and `create_file_tools`, insert this guard immediately **before** the terminal `except Exception as e:` (the ones that `return f"Error ...: {e}"`):

```python
        except WorkspaceUnavailableError:
            raise
```

Known sites (verify with `grep -n "except Exception as e" src/tools/workspace/filesystem.py src/tools/workspace/files.py`):
`filesystem.py` ≈ 296, 322, 388, 414, 440, 479, 505, 614, 636, 663; `files.py` ≈ 390, 418, 462, 528, 592, 661, 831, 938 (and the read/write/edit/append handlers).

In `src/tools/shell/shell_tools.py`, guard the `run_command` handler (the `except Exception as e:` before `return f"Error: {e}"` near line 408) and both `shell_execute` handlers (near 550 and 590) with the same two-line guard before the blanket `except`.

In `src/core/backends/remote.py:897`, change the flatten to a re-raise:

```python
                except WorkspaceUnavailableError:
                    raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workspace_tools_propagate_unavailable.py -v`
Expected: PASS.

- [ ] **Step 5: Guard against missed sites**

Run: `grep -rn "except Exception as e" src/tools/workspace/filesystem.py src/tools/workspace/files.py | wc -l` and confirm each precedes a paired `except WorkspaceUnavailableError: raise` (visually scan the diff). The two tests above prove the pattern; the sweep applies it everywhere.

- [ ] **Step 6: Commit**

```bash
git add src/tools/workspace/filesystem.py src/tools/workspace/files.py src/tools/shell/shell_tools.py src/core/backends/remote.py tests/test_workspace_tools_propagate_unavailable.py
git commit -m "fix(tools): re-raise WorkspaceUnavailableError instead of flattening to a string"
```

---

### Task 4: Type-based propagation in the job graph (`ToolNode`)

Replace the fragile `"WorkspaceUnavailableError" in msg.content` substring watchdog with a `ToolNode` error handler that re-raises the type. LangGraph 1.0.6 calls the handler at `flag(e)` with no surrounding try/except (`tool_node.py:429`), so a raising handler propagates; annotating `e: Exception` makes `_infer_handled_types` route all errors through it.

**Files:**
- Modify: `src/graph.py:3785` (`ToolNode(...)`), delete `:4057-4069` (substring watchdog), add a module-level handler + template constant.
- Test: `tests/test_graph_workspace_error_propagation.py` (new)

**Interfaces:**
- Consumes: `WorkspaceUnavailableError`, `langgraph.prebuilt.ToolNode`.
- Produces: `_handle_tool_errors_reraise_workspace(e: Exception) -> str` — raises `WorkspaceUnavailableError`, else returns the default template string. `ToolNode` built with it re-raises on a workspace-dead tool call and stringifies all other tool errors.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_workspace_error_propagation.py`:

```python
"""ToolNode must re-raise WorkspaceUnavailableError (so it propagates to the
job error path) while still stringifying ordinary tool errors."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from src.core.workspace_backend import WorkspaceUnavailableError
from src.graph import _handle_tool_errors_reraise_workspace


def test_handler_reraises_workspace_error():
    with pytest.raises(WorkspaceUnavailableError):
        _handle_tool_errors_reraise_workspace(WorkspaceUnavailableError("gone"))


def test_handler_stringifies_other_errors():
    msg = _handle_tool_errors_reraise_workspace(ValueError("bad arg"))
    assert "ValueError" in msg  # repr(e) in the template


def _state_with_call(tool_name):
    return {"messages": [AIMessage(content="", tool_calls=[
        {"name": tool_name, "args": {}, "id": "call_1"}
    ])]}


@pytest.mark.asyncio
async def test_toolnode_propagates_workspace_error():
    @tool
    def boom() -> str:
        """raises workspace unavailable"""
        raise WorkspaceUnavailableError("workspace gone")

    node = ToolNode([boom], handle_tool_errors=_handle_tool_errors_reraise_workspace)
    with pytest.raises(WorkspaceUnavailableError):
        await node.ainvoke(_state_with_call("boom"))


@pytest.mark.asyncio
async def test_toolnode_stringifies_ordinary_error():
    @tool
    def bad() -> str:
        """raises a normal error"""
        raise ValueError("nope")

    node = ToolNode([bad], handle_tool_errors=_handle_tool_errors_reraise_workspace)
    result = await node.ainvoke(_state_with_call("bad"))
    assert isinstance(result["messages"][0], ToolMessage)
    assert "ValueError" in result["messages"][0].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_graph_workspace_error_propagation.py -v`
Expected: FAIL — `ImportError: cannot import name '_handle_tool_errors_reraise_workspace'`.

- [ ] **Step 3: Write minimal implementation**

In `src/graph.py`, add near the top (with the other module-level constants) — inline the template so we don't import a langgraph-internal constant:

```python
# Mirrors langgraph.prebuilt.tool_node.TOOL_CALL_ERROR_TEMPLATE — the string
# handle_tool_errors=True produced. Inlined to avoid coupling to a private const.
_TOOL_CALL_ERROR_TEMPLATE = "Error: {error}\n Please fix your mistakes."


def _handle_tool_errors_reraise_workspace(e: Exception) -> str:
    """ToolNode error handler.

    Re-raise WorkspaceUnavailableError so a dead-workspace tool call propagates
    out of the graph → src/agent.py's isinstance check → a recoverable
    `workspace_unavailable` freeze. Every other exception is stringified exactly
    as handle_tool_errors=True did, so the model can fix its own mistakes.
    Annotating `e: Exception` makes ToolNode._infer_handled_types route ALL
    exceptions here (giving us the chance to re-raise ours).
    See docs/issues/agent_fast_freeze_on_dead_workspace.md.
    """
    if isinstance(e, WorkspaceUnavailableError):
        raise e
    return _TOOL_CALL_ERROR_TEMPLATE.format(error=repr(e))
```

Ensure `WorkspaceUnavailableError` is imported at module scope in `src/graph.py` (it is currently imported locally at ~4064 — add a top-level `from .core.workspace_backend import WorkspaceUnavailableError` and remove the now-redundant local import).

Change `src/graph.py:3785`:

```python
    tool_node = ToolNode(
        tools, handle_tool_errors=_handle_tool_errors_reraise_workspace
    )
```

Delete the substring watchdog block at `src/graph.py:4057-4069` (the
`# Check for workspace unavailable errors ...` comment through the
`raise WorkspaceUnavailableError(...)`). Propagation now happens by type inside
the node — the same path `agent.py:1012` already catches.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_graph_workspace_error_propagation.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Regression-check the graph image test**

Run: `pytest tests/test_graph_image_postprocessing.py -v`
Expected: PASS (it patches `ToolNode`; confirm the handler change didn't break its expectations).

- [ ] **Step 6: Commit**

```bash
git add src/graph.py tests/test_graph_workspace_error_propagation.py
git commit -m "fix(graph): ToolNode re-raises WorkspaceUnavailableError by type; drop substring watchdog"
```

---

### Task 5: Cover the chat path (`persistent_graph.py`)

The interactive session runs a separate hand-rolled loop with its own flatten at `1818`. Guard it to re-raise; the existing turn handler at `645-671` then surfaces a clean recovery message via `on_error`. Give `_user_facing_turn_error` a `WorkspaceUnavailableError` branch so the user sees an actionable message, not a raw string.

**Files:**
- Modify: `src/persistent_graph.py` — add import; guard the tool-exec `except` at `:1818`; add a branch to `_user_facing_turn_error` (`:216`).
- Test: `tests/test_persistent_graph_workspace_error.py` (new)

**Interfaces:**
- Consumes: `WorkspaceUnavailableError`; existing `_user_facing_turn_error(e) -> str`; existing turn handler at `645-671` (`except Exception as e: await callbacks.on_error(_user_facing_turn_error(e), turn_id=turn_id)`).
- Produces: `_user_facing_turn_error(WorkspaceUnavailableError(...))` returns the recovery message; the tool-exec loop re-raises `WorkspaceUnavailableError` instead of setting `result_str = "Tool execution error: ..."`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_persistent_graph_workspace_error.py`:

```python
"""Chat turn loop must surface WorkspaceUnavailableError cleanly, not flatten it
into a retryable ToolMessage. docs/issues/agent_fast_freeze_on_dead_workspace.md."""

from src.core.workspace_backend import WorkspaceUnavailableError
from src.persistent_graph import _user_facing_turn_error


def test_user_facing_message_for_workspace_unavailable():
    msg = _user_facing_turn_error(WorkspaceUnavailableError("gone"))
    assert "workspace" in msg.lower()
    assert "gone" not in msg  # actionable copy, not the raw exception text


def test_user_facing_message_for_wrapped_workspace_unavailable():
    """The turn handler often sees an exception whose __cause__ is the WUE."""
    try:
        try:
            raise WorkspaceUnavailableError("gone")
        except WorkspaceUnavailableError as inner:
            raise RuntimeError("turn failed") from inner
    except RuntimeError as e:
        msg = _user_facing_turn_error(e)
    assert "workspace" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistent_graph_workspace_error.py -v`
Expected: FAIL — `_user_facing_turn_error` returns `str(e)` (`"gone"`) for the direct case; the wrapped case returns `"turn failed"`.

- [ ] **Step 3: Write minimal implementation**

Add to the top of `src/persistent_graph.py` (module imports):

```python
from .core.workspace_backend import WorkspaceUnavailableError
```

In `_user_facing_turn_error` (`src/persistent_graph.py:216`), immediately after the
existing `cause = getattr(e, "__cause__", None)` line, add:

```python
    if isinstance(e, WorkspaceUnavailableError) or isinstance(
        cause, WorkspaceUnavailableError
    ):
        return (
            "Your workspace became unavailable and is being recovered. "
            "Resend your message in a moment to reconnect."
        )
```

Guard the tool-exec flatten at `src/persistent_graph.py:1815-1821`:

```python
            is_error = False
            try:
                result = await tool.ainvoke(tool_args)
                result_str = str(result) if result is not None else ""
            except WorkspaceUnavailableError:
                raise
            except Exception as e:
                logger.warning(f"Tool {tool_name} failed: {e}")
                result_str = f"Tool execution error: {e}"
                is_error = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistent_graph_workspace_error.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the guard end-to-end (turn surfaces via on_error)**

The unit tests above cover `_user_facing_turn_error`; the `1818` guard itself is a
two-line, semantically-obvious change (`except WorkspaceUnavailableError: raise`
before the blanket catch). Verify its end-to-end effect — a tool raising
`WorkspaceUnavailableError` reaches the turn handler and calls `on_error` rather than
looping — by driving the real chat loop. Two acceptable ways, in order of preference:

1. **Harness test.** In `tests/test_persistent_graph_workspace_error.py`, follow
   `tests/test_persistent_memory_extraction.py`'s `_make_callbacks` + `run_persistent_loop`
   harness. Build a fake `llm_with_tools` (a `langchain_core.language_models.fake_chat_models.GenericFakeChatModel`
   seeded with one `AIMessage` carrying a `tool_calls` entry, then an empty message to
   end the turn), a single `@tool` that raises `WorkspaceUnavailableError`, and an
   `on_error` spy via `_make_callbacks(on_error=spy)`. Assert the spy was called with a
   message containing "workspace". If the streaming contract makes the fake LLM emit no
   tool call, fall to (2) rather than shipping a flaky test.
2. **Live drive (verify skill).** During executing-plans, use the `verify` skill to
   drive an actual chat session against a workspace whose pod you delete mid-turn, and
   confirm the UI shows the recovery message instead of a spinning retry.

Record which path you used in the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/persistent_graph.py tests/test_persistent_graph_workspace_error.py
git commit -m "fix(persistent_graph): re-raise WorkspaceUnavailableError; actionable turn-error copy"
```

---

### Task 6: Full-suite + lint verification

**Files:** none (verification only).

- [ ] **Step 1: Run the affected suites**

Run:
```bash
pytest tests/test_workspace_backends.py tests/test_workspace_tools_propagate_unavailable.py tests/test_graph_workspace_error_propagation.py tests/test_persistent_graph_workspace_error.py tests/test_graph_image_postprocessing.py -v
```
Expected: all PASS. (Local env is Py3.14/noisy — if unrelated collection errors appear, confirm they predate this branch; CI on 3.12 is the gate.)

- [ ] **Step 2: Lint**

Run: `ruff format src/core/backends/remote.py src/graph.py src/persistent_graph.py src/tools/workspace/filesystem.py src/tools/workspace/files.py src/tools/shell/shell_tools.py && ruff check src/`
Expected: clean (or only pre-existing warnings).

- [ ] **Step 3: Confirm no "VM" leaks in agent-facing transport strings**

Run: `grep -n "connect to VM\|Connected to VM\|Disconnected from VM" src/core/backends/remote.py`
Expected: no output.

- [ ] **Step 4: Final commit (if lint reformatted anything)**

```bash
git add -A && git commit -m "chore: ruff format for fast-freeze changes"
```

---

## Self-Review

- **Spec coverage:** Part 1 Lever A → Tasks 3 (tools) + 4 (job graph) + 5 (chat graph). Part 2 Lever B → Tasks 1 (de-nest) + 2 (classify). Part 3 rename → Task 2 (connect/disconnect strings; comment-VMs intentionally kept). Tests T1a→Task 3, T1b→Task 4, T2→Task 1, T3/T4→Task 2, T5→Task 5. All spec sections mapped.
- **Out-of-scope respected:** no reaper change (P0), no watchdog (P2), no live-chat auto-reconnect (fast-follow) — only "stop spinning + surface cleanly".
- **Type consistency:** `_handle_tool_errors_reraise_workspace(e: Exception) -> str`, `_classify_connect_error(e: Exception) -> str`, `_user_facing_turn_error(e) -> str`, `WorkspaceUnavailableError` — used identically across tasks.
- **No broken window:** Task 3 (tools re-raise) lands while Task 4's old substring watchdog is still present, which catches the re-raised error via `repr(e)`; Task 4 then swaps to type propagation. Safe in this order.
