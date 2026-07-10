# RemoteBackend Indefinite-Wait Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every wait in `RemoteBackend` bounded so no command or SFTP operation can wedge a job forever (spec: `docs/issues/remote_backend_indefinite_wait_deadlock.md`).

**Architecture:** All changes live in the agent's workspace-backend layer: `src/core/backends/remote.py` (drain loop, timeouts, keepalive), `src/core/workspace_backend.py` (new exception + shared constant), `src/tools/workspace/filesystem.py` (capped summary line). No orchestrator, config, or schema changes.

**Tech Stack:** Python 3.11, paramiko (mocked in tests — never a real SSH server), pytest with the existing `remote_backend` fixture in `tests/test_workspace_backends.py`.

## Global Constraints

- Work directly on `develop`. Commit per task; **NEVER push** (user pushes; push workflow re-runs ruff and rewrites SHAs).
- `RemoteCommandTimeoutError` must NOT subclass `WorkspaceUnavailableError` — subclassing it would trip the P1 fast-freeze path and freeze jobs on slow commands.
- Test runner: `python -m pytest tests/test_workspace_backends.py -v`. Local env can be noisy (Py3.14/missing deps) — if *unrelated* imports fail locally, CI is the gate; the targeted file must still be attempted first.
- Line numbers below are from the pre-change file; after Task 1 they shift. Match on content.

---

### Task 1: `RemoteCommandTimeoutError` + `_exec` drain loop

**Files:**
- Modify: `src/core/workspace_backend.py:14-21` (add exception below `WorkspaceUnavailableError`)
- Modify: `src/core/backends/remote.py:378-400` (`_exec`), plus module constants near line 48 and the import at line 44
- Test: `tests/test_workspace_backends.py` (new class `TestExecDrainLoop`)

**Interfaces:**
- Consumes: existing `remote_backend` fixture `(backend, mock_ssh, mock_sftp)`; `mock_ssh.exec_command.return_value = (stdin, stdout, stderr)` where `stdout.channel` is the mock channel.
- Produces: `RemoteCommandTimeoutError(Exception)` in `src/core/workspace_backend.py`; constants `_EXEC_MAX_OUTPUT_BYTES = 5 * 1024 * 1024` in `remote.py`; `_exec(command, timeout=30)` signature unchanged. Tasks 2–4 rely on these exact names.

- [x] **Step 1: Write the failing tests**

Add to `tests/test_workspace_backends.py` (after the existing helpers section):

```python
class _WindowedChannel:
    """Mock paramiko Channel with window-full deadlock semantics.

    Exit status only becomes ready once ALL pending output has been
    recv()'d — exactly like a remote command blocked on pipe_write until
    the reader drains the channel. recv_exit_status() raises if called
    while output is undrained, which is the deadlock the fix removes
    (on real paramiko it blocks forever instead of raising).
    """

    def __init__(self, stdout_data=b"", stderr_data=b"", exit_code=0,
                 never_exits=False):
        self._out = stdout_data
        self._err = stderr_data
        self._exit_code = exit_code
        self._never_exits = never_exits
        self.closed = False

    def recv_ready(self):
        return bool(self._out)

    def recv(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, n):
        chunk, self._err = self._err[:n], self._err[n:]
        return chunk

    def exit_status_ready(self):
        if self._never_exits:
            return False
        return not self._out and not self._err

    def recv_exit_status(self):
        if self._out or self._err:
            raise AssertionError(
                "recv_exit_status() called with output undrained — "
                "window-full deadlock (hangs forever on real paramiko)"
            )
        return self._exit_code

    def close(self):
        self.closed = True


def _wire_exec_channel(mock_ssh, channel):
    """Point mock_ssh.exec_command at a (stdin, stdout, stderr) triple
    whose stdout.channel is the given mock channel."""
    stdout = MagicMock()
    stdout.channel = channel
    mock_ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())


class TestExecDrainLoop:
    """_exec must drain the channel and bound every wait.

    Regression tests for docs/issues/remote_backend_indefinite_wait_deadlock.md
    (job 2dbe6854: grep output 2,319,835 B > 2 MiB window wedged a job 8 h).
    """

    def test_large_output_does_not_deadlock(self, remote_backend):
        """Output bigger than the 2 MiB channel window must be returned,
        not deadlock. Fails on pre-fix code (recv_exit_status first)."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        big = b"x" * (3 * 1024 * 1024)  # > 2 MiB window
        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=big))
        out = backend._exec("grep -rni role /ws")
        assert len(out) == 3 * 1024 * 1024

    def test_stderr_is_drained(self, remote_backend):
        """stderr shares the channel window; undrained stderr must not
        stall the loop even on a non-zero exit."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        chan = _WindowedChannel(
            stdout_data=b"ok", stderr_data=b"e" * 100_000, exit_code=1
        )
        _wire_exec_channel(mock_ssh, chan)
        assert backend._exec("cmd") == "ok"

    def test_timeout_raises_and_closes_channel(self, remote_backend):
        """A command that never exits must raise RemoteCommandTimeoutError
        (NOT WorkspaceUnavailableError) and close the channel."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        chan = _WindowedChannel(never_exits=True)
        _wire_exec_channel(mock_ssh, chan)
        with patch("time.sleep"):
            with pytest.raises(RemoteCommandTimeoutError):
                backend._exec("sleep 999", timeout=0)
        assert chan.closed
        assert not issubclass(RemoteCommandTimeoutError, WorkspaceUnavailableError)

    def test_output_truncated_at_cap(self, remote_backend):
        """Output beyond 5 MiB is dropped (marker appended), but the
        channel is still drained so the command can finish."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        big = b"y" * (6 * 1024 * 1024)
        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=big))
        out = backend._exec("cat huge")
        assert out.endswith("[output truncated at 5 MiB]")
        assert len(out) < 6 * 1024 * 1024
```

Add the import at the top of the test file next to the existing `WorkspaceUnavailableError` import:

```python
from src.core.workspace_backend import RemoteCommandTimeoutError, WorkspaceUnavailableError
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workspace_backends.py::TestExecDrainLoop -v`
Expected: import error `cannot import name 'RemoteCommandTimeoutError'` (or, once defined, `AssertionError: recv_exit_status() called with output undrained` from the first test — both prove the pre-fix behavior).

- [x] **Step 3: Implement**

In `src/core/workspace_backend.py`, directly below `WorkspaceUnavailableError`:

```python
class RemoteCommandTimeoutError(Exception):
    """A remote operation exceeded its wall-clock deadline.

    Deliberately NOT a WorkspaceUnavailableError: a slow command is not a
    dead workspace and must not trip the fast-freeze path — it surfaces to
    the model as an ordinary tool error instead. If the workspace is truly
    gone, the next operation's connect path classifies that and freezes.
    See docs/issues/remote_backend_indefinite_wait_deadlock.md.
    """

    pass
```

In `src/core/backends/remote.py`:

Extend the line-44 import:

```python
from ..workspace_backend import (
    RemoteCommandTimeoutError,
    WorkspaceBackend,
    WorkspaceUnavailableError,
)
```

Add near the stall/timeout constants (line ~48):

```python
# _exec output cap: past this, output is dropped (marker appended) but the
# channel keeps draining so the remote command can finish. Guards agent RAM.
_EXEC_MAX_OUTPUT_BYTES = 5 * 1024 * 1024
_EXEC_POLL_SECONDS = 0.05
```

Replace `_exec` (lines 378-400):

```python
def _exec(self, command: str, timeout: int = 30) -> str:
    """Execute a command via SSH and return stdout.

    Drains stdout AND stderr while waiting for the exit status, so output
    larger than the SSH channel window cannot deadlock the command
    (docs/issues/remote_backend_indefinite_wait_deadlock.md), and enforces
    ``timeout`` as a wall-clock deadline on the whole command.

    Raises WorkspaceUnavailableError on connection failure and
    RemoteCommandTimeoutError when the deadline expires.
    """
    self._ensure_connected()
    try:
        _, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
        chan = stdout.channel
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        out_size = 0
        err_size = 0
        truncated = False
        deadline = time.monotonic() + timeout
        while True:
            while chan.recv_ready():
                chunk = chan.recv(65536)
                if out_size < _EXEC_MAX_OUTPUT_BYTES:
                    out_chunks.append(chunk)
                else:
                    truncated = True
                out_size += len(chunk)
            while chan.recv_stderr_ready():
                chunk = chan.recv_stderr(65536)
                if err_size < _EXEC_MAX_OUTPUT_BYTES:
                    err_chunks.append(chunk)
                err_size += len(chunk)
            if (
                chan.exit_status_ready()
                and not chan.recv_ready()
                and not chan.recv_stderr_ready()
            ):
                break
            if time.monotonic() > deadline:
                chan.close()  # frees the remote side before we bail
                raise RemoteCommandTimeoutError(
                    f"Remote command timed out after {timeout}s on "
                    f"{self._host}: {command[:80]}"
                )
            time.sleep(_EXEC_POLL_SECONDS)
        exit_code = chan.recv_exit_status()  # ready — returns immediately
        output = b"".join(out_chunks).decode("utf-8", errors="replace")
        if truncated:
            output += "\n[output truncated at 5 MiB]"
        if exit_code != 0:
            err = b"".join(err_chunks).decode("utf-8", errors="replace")
            # Some commands (grep with no match, tmux has-session) use non-zero
            # exit codes for normal conditions — callers check output.
            logger.debug(
                f"Remote command exit {exit_code}: {command[:80]} | stderr: {err[:200]}"
            )
        return output
    except (paramiko.SSHException, socket.error, EOFError, OSError) as e:
        raise WorkspaceUnavailableError(
            f"SSH command failed on {self._host}: {e}"
        ) from e
```

Note: `RemoteCommandTimeoutError` is not in the `except` tuple, so the raise inside the `try` propagates untouched.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace_backends.py -v`
Expected: `TestExecDrainLoop` all PASS, and every pre-existing test in the file still PASS (the fixture's `MagicMock` channels satisfy the new loop: `recv_ready()` MagicMocks are truthy-then-consumed — if any legacy test hangs or fails on the loop, fix by wiring `_wire_exec_channel` with a `_WindowedChannel` in that test, not by weakening `_exec`).

- [x] **Step 5: Commit**

```bash
git add src/core/workspace_backend.py src/core/backends/remote.py tests/test_workspace_backends.py
git commit -m "fix(agent): drain SSH channel in _exec — kills window-full deadlock that wedged job 2dbe6854 for 8h"
```

---

### Task 2: Heavy-op timeout bumps

**Files:**
- Modify: `src/core/backends/remote.py` — the four call sites: `rm -rf` (line 692, `delete_directory`), `mv` (line 710, `move`), `cp -a` (line 728, `copy`), `du -sb` (line 743, `stat`)
- Test: `tests/test_workspace_backends.py` (new class `TestHeavyOpTimeouts`)

**Interfaces:**
- Consumes: `_exec(command, timeout=)` from Task 1.
- Produces: nothing new — behavior only.

- [x] **Step 1: Write the failing test**

```python
class TestHeavyOpTimeouts:
    """Timeouts now actually bind (Task 1), so heavy ops need explicit
    generous deadlines or big trees would newly fail at the 30s default."""

    @pytest.mark.parametrize(
        "call,expected_timeout",
        [
            (lambda b: b.delete_directory("big"), 300),
            (lambda b: b.copy("a", "b"), 300),
            (lambda b: b.move("a", "b"), 120),
            (lambda b: b.stat("big"), 120),
        ],
    )
    def test_heavy_ops_pass_generous_timeouts(
        self, remote_backend, call, expected_timeout
    ):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        # delete_directory/get_size stat the path first; make it a directory
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        with patch.object(backend, "_exec", return_value="0\t/ws") as ex:
            call(backend)
        assert ex.call_args.kwargs.get("timeout") == expected_timeout
```

Adjust the mocked pre-steps if a method errors before reaching `_exec` (e.g. `move_file`/`copy_file` may `_remote_stat` the source): set `mock_sftp.stat.return_value` as above — it satisfies both file and dir checks used on these paths.

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workspace_backends.py::TestHeavyOpTimeouts -v`
Expected: FAIL — `call_args.kwargs.get("timeout")` is `None` (call sites don't pass a timeout today).

- [x] **Step 3: Implement**

At the four call sites, pass explicit timeouts:

```python
self._exec(f"rm -rf '{safe_path}'", timeout=300)
```
```python
self._exec(f"mv '{safe_src}' '{safe_dst}'", timeout=120)
```
```python
self._exec(f"cp -a '{safe_src}' '{safe_dst}'", timeout=300)
```
```python
output = self._exec(f"du -sb '{safe_path}' 2>/dev/null || echo '0'", timeout=120)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace_backends.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add src/core/backends/remote.py tests/test_workspace_backends.py
git commit -m "fix(agent): generous explicit timeouts for rm/cp/mv/du now that _exec deadlines bind"
```

---

### Task 3: `search_files` server-side output cap

**Files:**
- Modify: `src/core/workspace_backend.py` (shared constant)
- Modify: `src/core/backends/remote.py:615-616` (grep command)
- Modify: `src/tools/workspace/filesystem.py:383-387` (summary line)
- Test: `tests/test_workspace_backends.py` (new class `TestSearchFilesCap`)

**Interfaces:**
- Consumes: `_exec` from Task 1.
- Produces: `SEARCH_RESULT_HARD_CAP = 2000` in `src/core/workspace_backend.py` — imported by both `remote.py` (grep pipeline) and `filesystem.py` (summary rendering).

- [x] **Step 1: Write the failing test**

```python
class TestSearchFilesCap:
    def test_grep_command_is_head_capped(self, remote_backend):
        """search_files must bound grep output server-side: the display cap
        is 50 matches, yet uncapped grep shipped 2.2 MB in the incident."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        with patch.object(backend, "_exec", return_value="") as ex:
            backend.search_files("role")
        cmd = ex.call_args.args[0]
        assert "| head -n 2000" in cmd
        assert cmd.rstrip().endswith("|| true")
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workspace_backends.py::TestSearchFilesCap -v`
Expected: FAIL — `'| head -n 2000' not in` the built command.

- [x] **Step 3: Implement**

In `src/core/workspace_backend.py` (module level, below the exceptions):

```python
# Server-side cap on search_files matches. The display layer shows at most
# max_search_results (default 50); this bounds what backends ship over the
# wire. filesystem.py renders "N+ (capped)" when a result set hits this.
SEARCH_RESULT_HARD_CAP = 2000
```

In `src/core/backends/remote.py`, add `SEARCH_RESULT_HARD_CAP` to the line-44 import block, and change the command build in `search_files`:

```python
cmd = (
    f"grep {flags} {excludes} -- '{safe_query}' {remote_path} 2>/dev/null "
    f"| head -n {SEARCH_RESULT_HARD_CAP} || true"
)
output = self._exec(cmd, timeout=60)
```

(`head` exits 0 and its status is the pipeline's; grep's SIGPIPE death when
capped is harmless under `|| true`.)

In `src/tools/workspace/filesystem.py`, import the constant
(`from ...core.workspace_backend import SEARCH_RESULT_HARD_CAP` — match the
module's existing relative-import depth) and replace the summary block:

```python
if total >= SEARCH_RESULT_HARD_CAP:
    lines.append("")
    lines.append(
        f"[Showing {max_search_results} of {SEARCH_RESULT_HARD_CAP}+ "
        f"matches (server-side capped)]"
    )
elif total > max_search_results:
    lines.append("")
    lines.append(f"[Showing {max_search_results} of {total} matches]")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace_backends.py tests/test_scratch_backend.py tests/test_subdir_backend.py tests/test_virtual_workspace_backend.py -v`
Expected: all PASS (the shared `_fs_backend.py` suites exercise `search_files` summaries across backends — confirm none asserted the old summary string for ≥2000 results; fix test data, not the feature, if one did).

- [x] **Step 5: Commit**

```bash
git add src/core/workspace_backend.py src/core/backends/remote.py src/tools/workspace/filesystem.py tests/test_workspace_backends.py
git commit -m "fix(agent): cap search_files grep server-side at 2000 lines (display cap is 50)"
```

---

### Task 4: SFTP timeout + transport keepalive + honest read_file timeout error

**Files:**
- Modify: `src/core/backends/remote.py:304-306` (connect tail), constants near line 48, `read_file` (line 490)
- Test: `tests/test_workspace_backends.py` (new class `TestConnectionHardening`)

**Interfaces:**
- Consumes: `RemoteCommandTimeoutError` from Task 1; `remote_backend` fixture's `mock_transport` (already wired via `mock_ssh.get_transport`).
- Produces: constants `_TRANSPORT_KEEPALIVE_SECONDS = 15`, `_SFTP_OP_TIMEOUT_SECONDS = 60.0`.

- [x] **Step 1: Write the failing tests**

```python
class TestConnectionHardening:
    def test_connect_sets_keepalive_and_sftp_timeout(self, remote_backend):
        """A blackholed connection must eventually error, not wait forever:
        transport keepalive + a socket timeout on the shared SFTP channel
        (which serializes ALL file ops behind _sftp_lock)."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_ssh.get_transport.return_value.set_keepalive.assert_called_with(15)
        mock_sftp.get_channel.return_value.settimeout.assert_called_with(60.0)

    def test_read_file_timeout_is_not_file_not_found(self, remote_backend):
        """socket.timeout is an OSError; without special-casing it,
        read_file reports a hung workspace as 'file not found'."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.open.side_effect = socket.timeout("timed out")
        with pytest.raises(RemoteCommandTimeoutError, match="timed out reading"):
            backend.read_file("some/file.md")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workspace_backends.py::TestConnectionHardening -v`
Expected: FAIL — `set_keepalive` not called; second test gets `FileNotFoundError` instead of `RemoteCommandTimeoutError`.

- [x] **Step 3: Implement**

Constants near line 48:

```python
_TRANSPORT_KEEPALIVE_SECONDS = 15
_SFTP_OP_TIMEOUT_SECONDS = 60.0
```

In `connect()`, after `self._sftp = self._ssh.open_sftp()`:

```python
transport = self._ssh.get_transport()
if transport is not None:
    transport.set_keepalive(_TRANSPORT_KEEPALIVE_SECONDS)
sftp_chan = self._sftp.get_channel()
if sftp_chan is not None:
    sftp_chan.settimeout(_SFTP_OP_TIMEOUT_SECONDS)
```

In `read_file`, add the timeout catch BEFORE the `IOError` conversion
(`socket.timeout` is an `OSError` subclass and would otherwise masquerade
as a missing file):

```python
with self._sftp_lock:
    try:
        with self._sftp.open(remote_path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}")
    except socket.timeout as e:
        raise RemoteCommandTimeoutError(
            f"Workspace I/O timed out reading {path}"
        ) from e
    except IOError as e:
        raise FileNotFoundError(f"Cannot read {path}: {e}") from e
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace_backends.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add src/core/backends/remote.py tests/test_workspace_backends.py
git commit -m "fix(agent): transport keepalive + SFTP op timeout; read_file timeout no longer reports file-not-found"
```

---

### Task 5: Full verification + spec status flip

**Files:**
- Modify: `docs/issues/remote_backend_indefinite_wait_deadlock.md` (Status line)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing — verification + bookkeeping.

- [x] **Step 1: Run the full backend + workspace-tool test surface**

Run: `python -m pytest tests/test_workspace_backends.py tests/test_scratch_backend.py tests/test_subdir_backend.py tests/test_virtual_workspace_backend.py tests/test_workspace_tools_propagate_unavailable.py -v`
Expected: all PASS. `test_workspace_tools_propagate_unavailable.py` matters specifically: it pins that `WorkspaceUnavailableError` still propagates out of tools (freeze path intact) — `RemoteCommandTimeoutError` must NOT appear in that file's propagation list.

- [x] **Step 2: Update the spec status line**

In `docs/issues/remote_backend_indefinite_wait_deadlock.md` change:

```markdown
**Status:** Designed 2026-07-10, not yet implemented. Work on `develop`.
```

to:

```markdown
**Status:** Implemented on `develop` 2026-07-10 (Tasks 1-4: drain loop,
heavy-op timeouts, search cap, SFTP/keepalive hardening). Detection net and
drain watchdog remain open — see Non-goals.
```

- [x] **Step 3: Commit**

```bash
git add docs/issues/remote_backend_indefinite_wait_deadlock.md docs/superpowers/plans/2026-07-10-remote-backend-indefinite-wait-hardening.md
git commit -m "docs(issues): RemoteBackend indefinite-wait deadlock spec -> implemented"
```
