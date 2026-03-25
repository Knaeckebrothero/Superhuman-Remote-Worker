"""Remote workspace backend via SSH/SFTP.

Connects to a VM over SSH for file operations (SFTP) and shell execution
(remote tmux over SSH). Implements the same sentinel-based completion
detection as the local ShellManager, but over SSH channels.

Requires: paramiko (pip install paramiko)
See docs/features/vm_backend.md for the full design.
"""

import fnmatch
import logging
import posixpath
import re
import socket
import stat as stat_module
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:
    paramiko = None  # Deferred — only needed when backend: remote is used

from ..workspace_backend import WorkspaceBackend, WorkspaceUnavailableError

logger = logging.getLogger(__name__)

# Seconds of unchanged output before declaring a stall
STALL_DETECTION_SECONDS = 5.0

# Patterns that indicate the terminal is waiting for interactive input
INTERACTIVE_PROMPT_PATTERNS = [
    (
        re.compile(
            r"\[y/n\]|\[Y/n\]|\[y/N\]|\[N/y\]|\(yes/no\)|\(yes/no/\[fingerprint\]\)",
            re.IGNORECASE,
        ),
        "confirmation prompt",
    ),
    (re.compile(r"(?:password|passphrase)\s*:", re.IGNORECASE), "password prompt"),
    (
        re.compile(r"Are you sure you want to continue connecting", re.IGNORECASE),
        "SSH host key verification",
    ),
    (
        re.compile(r"Install package '.*?' to provide command", re.IGNORECASE),
        "package install prompt",
    ),
    (re.compile(r"\[sudo\] password for", re.IGNORECASE), "sudo password prompt"),
    (
        re.compile(r"press any key|press enter to continue|hit enter", re.IGNORECASE),
        "press key prompt",
    ),
    (re.compile(r"enter passphrase", re.IGNORECASE), "passphrase prompt"),
]

# Tab name validation
TAB_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,20}$")

# Default blocked commands
DEFAULT_BLOCKED_COMMANDS = frozenset(
    [
        "reboot",
        "shutdown",
        "poweroff",
        "halt",
        "init",
    ]
)


class _RemoteTab:
    """Tracks per-tab state for a remote tmux session."""

    def __init__(self, name: str, tab_type: str = "shell"):
        self.name = name
        self.tab_type = tab_type
        self.read_cursor: int = 0
        self.created_at: datetime = datetime.now(timezone.utc)
        self.last_activity: datetime = datetime.now(timezone.utc)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.tab_type,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
        }


class RemoteBackend(WorkspaceBackend):
    """Workspace on a remote VM, accessed via SSH/SFTP.

    File operations use SFTP. Shell operations manage a remote tmux session
    via SSH exec_command. Uses the same sentinel-based completion detection
    as the local ShellManager.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "agent-host",
        key_path: Optional[str] = None,
        workspace_path: str = "/home/agent-host/workspace",
        job_id: str = "",
        scrollback_limit: int = 5000,
        default_timeout: int = 120,
        max_tabs: int = 15,
        blocked_commands: Optional[List[str]] = None,
        sandbox_cwd: Optional[str] = None,
        connect_timeout: int = 30,
        max_retries: int = 5,
    ):
        if paramiko is None:
            raise ImportError(
                "paramiko is required for RemoteBackend. "
                "Install it: pip install paramiko"
            )

        self._host = host
        self._port = port
        self._username = username
        self._key_path = key_path
        self._remote_root = workspace_path.rstrip("/")
        self._job_id = job_id
        self._scrollback_limit = scrollback_limit
        self._default_timeout = default_timeout
        self._max_tabs = max_tabs
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries

        if blocked_commands is None:
            self._blocked_commands = DEFAULT_BLOCKED_COMMANDS
        else:
            self._blocked_commands = frozenset(blocked_commands)

        # sandbox_cwd: if set, cd to this directory in each new tab
        self._sandbox_cwd = sandbox_cwd or workspace_path.rstrip("/")

        # SSH/SFTP handles
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

        # Shell state
        self._session_name = f"agent_{job_id[:12]}" if job_id else "agent_remote"
        self._tabs: OrderedDict[str, _RemoteTab] = OrderedDict()
        self._sync_lock = threading.Lock()
        self._shell_initialized = False

    @property
    def root(self) -> str:
        return self._remote_root

    @property
    def supports_shell(self) -> bool:
        return True

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self) -> None:
        """Establish SSH connection and SFTP channel.

        Retries up to ``_max_retries`` times with exponential backoff to
        tolerate the window between daemon registration (NATS) and SSHD
        readiness inside the VM.
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
        for attempt in range(1, self._max_retries + 1):
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                self._ssh.connect(**connect_kwargs)
                break
            except (paramiko.SSHException, socket.error, OSError) as e:
                if attempt == self._max_retries:
                    raise WorkspaceUnavailableError(
                        f"Failed to connect to VM {self._host}:{self._port} "
                        f"after {self._max_retries} attempts: {e}"
                    ) from e
                logger.warning(
                    "SSH connect attempt %d/%d to %s:%d failed (%s), retrying in %.0fs",
                    attempt,
                    self._max_retries,
                    self._host,
                    self._port,
                    e,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

        self._sftp = self._ssh.open_sftp()
        logger.info(f"Connected to VM {self._host}:{self._port}")

    def disconnect(self) -> None:
        """Close SSH/SFTP connections and kill remote tmux session."""
        if self._shell_initialized:
            try:
                self._exec(f"tmux kill-session -t {self._session_name}")
            except Exception:
                pass
            self._shell_initialized = False
            self._tabs.clear()

        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None

        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

        logger.info(f"Disconnected from VM {self._host}")

    def is_connected(self) -> bool:
        if self._ssh is None:
            return False
        transport = self._ssh.get_transport()
        return transport is not None and transport.is_active()

    def _ensure_connected(self) -> None:
        """Reconnect with backoff if the SSH connection is dead."""
        if self.is_connected():
            return

        logger.warning(f"SSH connection to {self._host} lost, reconnecting...")
        backoff = 1.0
        for attempt in range(1, self._max_retries + 1):
            try:
                self.connect()
                logger.info(f"Reconnected to {self._host} on attempt {attempt}")
                return
            except WorkspaceUnavailableError:
                if attempt == self._max_retries:
                    raise
                logger.warning(
                    f"Reconnect attempt {attempt} failed, retrying in {backoff}s"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _exec(self, command: str, timeout: int = 30) -> str:
        """Execute a command via SSH and return stdout.

        Raises WorkspaceUnavailableError on connection failure.
        """
        self._ensure_connected()
        try:
            _, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            output = stdout.read().decode("utf-8", errors="replace")
            if exit_code != 0:
                err = stderr.read().decode("utf-8", errors="replace")
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

    # =========================================================================
    # Path utilities
    # =========================================================================

    def _resolve(self, relative_path: str) -> str:
        """Resolve relative path to absolute remote path, validate boundaries."""
        if not relative_path or relative_path == ".":
            return self._remote_root

        # Normalize: remove leading /, collapse .., etc.
        cleaned = posixpath.normpath(relative_path)

        # Security: reject paths that escape
        if cleaned.startswith("..") or cleaned.startswith("/"):
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")

        full = posixpath.join(self._remote_root, cleaned)

        # Double-check normalization didn't allow escape
        if not full.startswith(self._remote_root):
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")

        return full

    def resolve_path(self, relative_path: str) -> str:
        return self._resolve(relative_path)

    def _remote_stat(self, remote_path: str) -> Optional[paramiko.SFTPAttributes]:
        """Get SFTP stat, returning None if path doesn't exist."""
        self._ensure_connected()
        try:
            return self._sftp.stat(remote_path)
        except FileNotFoundError:
            return None
        except IOError:
            return None

    def _ensure_remote_dir(self, remote_path: str) -> None:
        """Recursively create directories on the remote."""
        # Walk up to find an existing ancestor, then create downward
        parts_to_create = []
        current = remote_path
        while current and current != "/":
            st = self._remote_stat(current)
            if st is not None:
                break
            parts_to_create.append(current)
            current = posixpath.dirname(current)

        for d in reversed(parts_to_create):
            try:
                self._sftp.mkdir(d)
            except IOError:
                # Race condition or already exists
                pass

    # =========================================================================
    # File operations
    # =========================================================================

    def read_file(self, path: str, binary: bool = False) -> str | bytes:
        self._ensure_connected()
        remote_path = self._resolve(path)
        try:
            with self._sftp.open(remote_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {path}")
        except IOError as e:
            raise FileNotFoundError(f"Cannot read {path}: {e}") from e

        if binary:
            return data
        return data.decode("utf-8")

    def write_file(self, path: str, content: str | bytes) -> None:
        self._ensure_connected()
        remote_path = self._resolve(path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_dir(parent)

        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with self._sftp.open(remote_path, "wb") as f:
            f.write(data)
        logger.debug(f"Wrote remote file: {path}")

    def append_file(self, path: str, content: str) -> None:
        self._ensure_connected()
        remote_path = self._resolve(path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_dir(parent)

        with self._sftp.open(remote_path, "ab") as f:
            f.write(content.encode("utf-8"))

    def exists(self, path: str) -> bool:
        return self._remote_stat(self._resolve(path)) is not None

    def is_file(self, path: str) -> bool:
        st = self._remote_stat(self._resolve(path))
        if st is None:
            return False
        return stat_module.S_ISREG(st.st_mode)

    def is_dir(self, path: str) -> bool:
        st = self._remote_stat(self._resolve(path))
        if st is None:
            return False
        return stat_module.S_ISDIR(st.st_mode)

    def list_dir(self, path: str = "", pattern: str = "*") -> list[str]:
        self._ensure_connected()
        remote_path = self._resolve(path)

        st = self._remote_stat(remote_path)
        if st is None:
            return []
        if not stat_module.S_ISDIR(st.st_mode):
            return [path]

        try:
            entries = self._sftp.listdir_attr(remote_path)
        except IOError:
            return []

        results = []
        for entry in entries:
            entry_remote = posixpath.join(remote_path, entry.filename)
            # Compute path relative to workspace root
            if entry_remote.startswith(self._remote_root + "/"):
                rel = entry_remote[len(self._remote_root) + 1 :]
            else:
                rel = entry.filename

            # Apply glob pattern
            if not fnmatch.fnmatch(entry.filename, pattern):
                continue

            if stat_module.S_ISDIR(entry.st_mode):
                results.append(rel + "/")
            else:
                results.append(rel)

        return sorted(results)

    def search_files(
        self, query: str, path: str = "", case_sensitive: bool = False
    ) -> list[dict]:
        """Search via server-side grep for efficiency."""
        self._ensure_connected()
        remote_path = self._resolve(path)

        # Build grep command
        flags = "-rn"
        if not case_sensitive:
            flags += "i"

        # Escape single quotes in query
        safe_query = query.replace("'", "'\\''")

        # Exclude binary file extensions
        excludes = " ".join(
            f"--exclude='*.{ext}'"
            for ext in ["pdf", "docx", "png", "jpg", "gif", "zip", "db"]
        )

        cmd = f"grep {flags} {excludes} -- '{safe_query}' {remote_path} 2>/dev/null || true"
        output = self._exec(cmd, timeout=60)

        results = []
        for line in output.splitlines():
            if not line.strip():
                continue
            # grep -n output: /path/to/file:linenum:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, line_num, content = parts[0], parts[1], parts[2]

            # Make path relative to workspace root
            if file_path.startswith(self._remote_root + "/"):
                rel_path = file_path[len(self._remote_root) + 1 :]
            else:
                rel_path = file_path

            try:
                results.append(
                    {
                        "path": rel_path,
                        "line_number": int(line_num),
                        "line": content.strip(),
                    }
                )
            except ValueError:
                continue

        return results

    def mkdir(self, path: str) -> None:
        self._ensure_connected()
        remote_path = self._resolve(path)
        self._ensure_remote_dir(remote_path)
        logger.debug(f"Created remote directory: {path}")

    def delete_file(self, path: str) -> bool:
        self._ensure_connected()
        remote_path = self._resolve(path)
        st = self._remote_stat(remote_path)
        if st is None:
            return False

        if stat_module.S_ISREG(st.st_mode):
            self._sftp.remove(remote_path)
            logger.debug(f"Deleted remote file: {path}")
            return True

        if stat_module.S_ISDIR(st.st_mode):
            # Check if empty
            entries = self._sftp.listdir(remote_path)
            if entries:
                raise ValueError(f"Cannot delete non-empty directory: {path}")
            self._sftp.rmdir(remote_path)
            logger.debug(f"Deleted remote directory: {path}")
            return True

        return False

    def delete_directory(self, path: str) -> bool:
        self._ensure_connected()
        remote_path = self._resolve(path)

        if remote_path == self._remote_root:
            raise ValueError("Cannot delete workspace root directory")

        st = self._remote_stat(remote_path)
        if st is None:
            return False
        if not stat_module.S_ISDIR(st.st_mode):
            raise ValueError(f"Not a directory: {path}")

        # Use rm -rf for recursive delete
        safe_path = remote_path.replace("'", "'\\''")
        self._exec(f"rm -rf '{safe_path}'")
        logger.debug(f"Deleted remote directory: {path}")
        return True

    def move(self, src: str, dst: str) -> None:
        self._ensure_connected()
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)

        st = self._remote_stat(src_path)
        if st is None:
            raise FileNotFoundError(f"Source not found: {src}")

        # Ensure parent of destination exists
        self._ensure_remote_dir(posixpath.dirname(dst_path))

        safe_src = src_path.replace("'", "'\\''")
        safe_dst = dst_path.replace("'", "'\\''")
        self._exec(f"mv '{safe_src}' '{safe_dst}'")
        logger.debug(f"Moved remote: {src} -> {dst}")

    def copy(self, src: str, dst: str) -> None:
        self._ensure_connected()
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)

        st = self._remote_stat(src_path)
        if st is None:
            raise FileNotFoundError(f"Source not found: {src}")
        if stat_module.S_ISDIR(st.st_mode):
            raise ValueError(f"Cannot copy directory: {src}. Use move for directories.")

        self._ensure_remote_dir(posixpath.dirname(dst_path))

        safe_src = src_path.replace("'", "'\\''")
        safe_dst = dst_path.replace("'", "'\\''")
        self._exec(f"cp -a '{safe_src}' '{safe_dst}'")
        logger.debug(f"Copied remote: {src} -> {dst}")

    def stat(self, path: str) -> int:
        self._ensure_connected()
        remote_path = self._resolve(path)
        st = self._remote_stat(remote_path)
        if st is None:
            return 0

        if stat_module.S_ISREG(st.st_mode):
            return st.st_size

        # Directory: use du -sb for total size
        safe_path = remote_path.replace("'", "'\\''")
        output = self._exec(f"du -sb '{safe_path}' 2>/dev/null || echo '0'")
        try:
            return int(output.split()[0])
        except (ValueError, IndexError):
            return 0

    # =========================================================================
    # Shell operations — remote tmux over SSH
    # =========================================================================

    def _init_shell(self) -> None:
        """Initialize the remote tmux session."""
        if self._shell_initialized:
            return

        self._ensure_connected()

        # Kill any stale session
        self._exec(f"tmux kill-session -t {self._session_name} 2>/dev/null || true")

        # Create new detached session
        self._exec(
            f"tmux new-session -d -s {self._session_name} -x 200 -y 30 -n default"
        )

        # Set history limit
        self._exec(
            f"tmux set-option -t {self._session_name} history-limit {self._scrollback_limit}"
        )

        # Set working directory
        if self._sandbox_cwd:
            self._tmux_send_keys("default", f"cd {self._sandbox_cwd}", enter=True)
            time.sleep(0.2)

        # Register default tab
        self._tabs["default"] = _RemoteTab(name="default", tab_type="shell")
        self._shell_initialized = True
        logger.info(f"Remote shell initialized: session={self._session_name}")

    def _ensure_shell(self) -> None:
        """Ensure shell is initialized before shell operations."""
        if not self._shell_initialized:
            self._init_shell()

    def _tmux_send_keys(self, tab_name: str, text: str, enter: bool = True) -> None:
        """Send keys to a remote tmux pane."""
        # Escape text for tmux send-keys
        safe_text = text.replace("'", "'\\''")
        enter_flag = " Enter" if enter else ""
        self._exec(
            f"tmux send-keys -t {self._session_name}:{tab_name} '{safe_text}'{enter_flag}"
        )

    def _tmux_capture(self, tab_name: str) -> List[str]:
        """Capture pane content from remote tmux."""
        output = self._exec(
            f"tmux capture-pane -t {self._session_name}:{tab_name} -p "
            f"-S -{self._scrollback_limit}"
        )
        lines = output.splitlines()
        # Strip trailing empty lines
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def _check_blocked(self, command: str) -> Optional[str]:
        """Return error message if command is blocked, else None."""
        if not self._blocked_commands:
            return None
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in self._blocked_commands:
            return (
                f"Command blocked: '{first_word}' is not allowed. "
                f"Blocked commands: {', '.join(sorted(self._blocked_commands))}"
            )
        return None

    def _detect_interactive_prompt(self, all_lines: List[str]) -> Optional[str]:
        """Check if the terminal appears to be waiting for interactive input."""
        check_lines = all_lines[-5:] if len(all_lines) >= 5 else all_lines
        text_to_check = "\n".join(check_lines)

        for pattern, description in INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(text_to_check):
                return description

        return None

    def _detect_blocked_tab(self, all_lines: List[str]) -> Optional[str]:
        """Pre-flight check: is the tab stuck on a prompt right now?"""
        last_nonblank = ""
        for line in reversed(all_lines):
            stripped = line.strip()
            if stripped:
                last_nonblank = stripped
                break

        if last_nonblank and last_nonblank[-1] in ("$", "#", "%"):
            return None

        check_lines = all_lines[-3:] if len(all_lines) >= 3 else all_lines
        text_to_check = "\n".join(check_lines)

        for pattern, description in INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(text_to_check):
                return description

        return None

    def shell_run(
        self,
        command: str,
        timeout: int = 120,
        tab_name: str = "default",
        working_dir: Optional[str] = None,
    ) -> str:
        """Execute a command synchronously with sentinel-based completion detection.

        Mirrors the logic of ShellManager.run_sync() but over SSH+remote tmux.
        """
        self._ensure_shell()

        blocked = self._check_blocked(command)
        if blocked:
            return blocked

        timeout = min(timeout, 600)
        sentinel = f"__DONE_{uuid.uuid4().hex[:12]}__"

        if tab_name not in self._tabs:
            self.shell_ensure_tab(tab_name)
        tab = self._tabs[tab_name]

        if tab.tab_type not in ("shell",):
            raise ValueError(
                f"Synchronous execution only works on shell-type tabs. "
                f"Tab '{tab_name}' is type '{tab.tab_type}'. "
                f"Use shell_send for interactive tabs."
            )

        with self._sync_lock:
            # Pre-flight check
            pre_lines = self._tmux_capture(tab_name)
            existing_prompt = self._detect_blocked_tab(pre_lines)
            if existing_prompt:
                state_lines = [ln for ln in pre_lines if ln.strip()][-30:]
                terminal_state = "\n".join(state_lines) or "(empty)"
                return (
                    f"Tab '{tab_name}' is blocked by a previous "
                    f"{existing_prompt}. Your command was NOT executed.\n"
                    f"Resolve the prompt first: send the expected input "
                    f"with keys mode (e.g. keys='N' or keys='yes'), "
                    f"send C-c to cancel, or use a different tab.\n"
                    f"--- terminal state ---\n{terminal_state}"
                )

            # Change directory if needed
            if working_dir:
                full_dir = (
                    posixpath.join(self._sandbox_cwd, working_dir)
                    if self._sandbox_cwd
                    else working_dir
                )
                self._tmux_send_keys(tab_name, f"cd {full_dir}", enter=True)
                time.sleep(0.1)

            pre_count = len(pre_lines)

            # Send command with sentinel
            full_cmd = f'{command}; echo "{sentinel} $?"'
            self._tmux_send_keys(tab_name, full_cmd, enter=True)

            # Poll for sentinel
            start_time = time.monotonic()
            output_text = ""
            exit_code = None
            last_content_hash = None
            stall_start = None

            while time.monotonic() - start_time < timeout:
                time.sleep(0.3)  # Slightly longer poll interval for SSH latency
                try:
                    all_lines = self._tmux_capture(tab_name)
                except WorkspaceUnavailableError:
                    return f"SSH connection lost during command execution: {command}"

                # Find sentinel output line
                sentinel_line_idx = None
                for i in range(len(all_lines) - 1, -1, -1):
                    stripped = all_lines[i].strip()
                    if stripped.startswith(sentinel):
                        sentinel_line_idx = i
                        break

                if sentinel_line_idx is not None:
                    line = all_lines[sentinel_line_idx]
                    parts = line.strip().split()
                    try:
                        exit_code = int(parts[-1]) if parts else 1
                    except (ValueError, IndexError):
                        exit_code = 1

                    new_lines = all_lines[pre_count:sentinel_line_idx]
                    output_lines = [ol for ol in new_lines if sentinel not in ol]
                    # Skip prompt/command echo lines
                    while output_lines and (
                        command.split()[0] in output_lines[0]
                        or output_lines[0].strip().endswith("$")
                    ):
                        output_lines = output_lines[1:]

                    output_text = "\n".join(output_lines).strip()
                    break

                # Interactive prompt detection
                elapsed = time.monotonic() - start_time
                if elapsed > 1.0:
                    prompt_type = self._detect_interactive_prompt(all_lines)
                    if prompt_type:
                        terminal_state = self._capture_terminal_state(
                            tab_name, sentinel, pre_count
                        )
                        if working_dir and self._sandbox_cwd:
                            self._tmux_send_keys(
                                tab_name, f"cd {self._sandbox_cwd}", enter=True
                            )
                            time.sleep(0.1)
                        tab.last_activity = datetime.now(timezone.utc)
                        return (
                            f"Interactive prompt detected ({prompt_type}). "
                            f"Use keys mode to respond.\n"
                            f"--- terminal state ---\n{terminal_state}"
                        )

                    # Stall detection
                    content_hash = hash(tuple(all_lines[-20:]))
                    if content_hash == last_content_hash:
                        if stall_start is None:
                            stall_start = time.monotonic()
                        elif time.monotonic() - stall_start >= STALL_DETECTION_SECONDS:
                            terminal_state = self._capture_terminal_state(
                                tab_name, sentinel, pre_count
                            )
                            if working_dir and self._sandbox_cwd:
                                self._tmux_send_keys(
                                    tab_name, f"cd {self._sandbox_cwd}", enter=True
                                )
                                time.sleep(0.1)
                            tab.last_activity = datetime.now(timezone.utc)
                            return (
                                f"Command appears to be waiting for input "
                                f"(no output change for {STALL_DETECTION_SECONDS:.0f}s). "
                                f"Use keys mode to respond or C-c to cancel.\n"
                                f"--- terminal state ---\n{terminal_state}"
                            )
                    else:
                        stall_start = None
                    last_content_hash = content_hash

            # Restore working directory
            if working_dir and self._sandbox_cwd:
                self._tmux_send_keys(tab_name, f"cd {self._sandbox_cwd}", enter=True)
                time.sleep(0.1)

            tab.last_activity = datetime.now(timezone.utc)

            if exit_code is None:
                terminal_state = self._capture_terminal_state(
                    tab_name, sentinel, pre_count
                )
                return (
                    f"Command timed out after {timeout}s: {command}\n"
                    f"--- terminal state ---\n{terminal_state}"
                )

            # Format output
            parts = [f"Exit code: {exit_code}"]
            if output_text:
                parts.append(f"--- stdout ---\n{output_text}")
            else:
                parts.append("(no output)")

            return "\n".join(parts)

    def _capture_terminal_state(
        self, tab_name: str, sentinel: str, pre_count: int
    ) -> str:
        """Capture terminal state for timeout/stall reporting."""
        try:
            all_lines = self._tmux_capture(tab_name)
            post_lines = all_lines[pre_count:]
            clean_lines = [line for line in post_lines if sentinel not in line]
            if not clean_lines:
                clean_lines = all_lines[-30:]
            if len(clean_lines) > 30:
                clean_lines = clean_lines[-30:]
            while clean_lines and not clean_lines[-1].strip():
                clean_lines.pop()
            return "\n".join(clean_lines)
        except Exception as e:
            logger.debug(f"Failed to capture terminal state: {e}")
            return "(failed to capture terminal state)"

    def shell_send(
        self,
        tab_name: str,
        text: str,
        enter: bool = True,
    ) -> str:
        self._ensure_shell()

        if enter:
            blocked = self._check_blocked(text)
            if blocked:
                return blocked

        if tab_name not in self._tabs:
            raise KeyError(
                f"Tab '{tab_name}' not found. Available: {', '.join(self._tabs.keys())}"
            )

        self._tmux_send_keys(tab_name, text, enter=enter)
        self._tabs[tab_name].last_activity = datetime.now(timezone.utc)
        return f"Sent to '{tab_name}'"

    def shell_read(
        self,
        tab_name: str,
        lines: int = 50,
        since_cursor: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        self._ensure_shell()

        if tab_name not in self._tabs:
            raise KeyError(
                f"Tab '{tab_name}' not found. Available: {', '.join(self._tabs.keys())}"
            )

        tab = self._tabs[tab_name]
        all_lines = self._tmux_capture(tab_name)
        total_lines = len(all_lines)

        if since_cursor:
            new_lines = all_lines[tab.read_cursor :]
            tab.read_cursor = total_lines
            text = "\n".join(new_lines) if new_lines else "(no new output)"
            metadata = {
                "tab": tab_name,
                "mode": "since_cursor",
                "lines_returned": len(new_lines),
                "total_lines": total_lines,
            }
        else:
            start = max(0, total_lines - lines)
            selected = all_lines[start:]
            tab.read_cursor = total_lines
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": tab_name,
                "mode": "tail",
                "lines_requested": lines,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }

        return text, metadata

    def shell_read_with_offset(
        self,
        tab_name: str,
        lines: int = 30,
        offset: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        self._ensure_shell()

        if tab_name not in self._tabs:
            raise KeyError(
                f"Tab '{tab_name}' not found. Available: {', '.join(self._tabs.keys())}"
            )

        all_lines = self._tmux_capture(tab_name)
        total_lines = len(all_lines)

        if offset is not None:
            start = max(0, min(offset, total_lines))
            end = min(start + lines, total_lines)
            selected = all_lines[start:end]
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": tab_name,
                "mode": "offset",
                "offset": start,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }
        else:
            start = max(0, total_lines - lines)
            selected = all_lines[start:]
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": tab_name,
                "mode": "tail",
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }

        return text, metadata

    def shell_ensure_tab(self, name: str) -> None:
        self._ensure_shell()
        if name in self._tabs:
            return
        self.shell_open_tab(name)

    def shell_open_tab(
        self,
        name: str,
        command: Optional[str] = None,
        tab_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_shell()

        if not TAB_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid tab name '{name}': must match {TAB_NAME_PATTERN.pattern}"
            )

        if name in self._tabs:
            raise ValueError(f"Tab '{name}' already exists")

        if len(self._tabs) >= self._max_tabs:
            tab_names = ", ".join(self._tabs.keys())
            raise ValueError(
                f"Maximum tabs ({self._max_tabs}) reached. Close unused tabs first. "
                f"Open tabs: {tab_names}"
            )

        # Auto-detect type
        if tab_type is None:
            tab_type = "shell"
            if command:
                from src.tools.coding.shell_manager import COMMAND_TYPE_MAP

                first_word = command.strip().split()[0]
                base_cmd = first_word.rsplit("/", 1)[-1]
                tab_type = COMMAND_TYPE_MAP.get(base_cmd, "process")

        # Create new tmux window
        self._exec(f"tmux new-window -t {self._session_name} -n {name} -d")

        # Set working directory
        if self._sandbox_cwd and tab_type in ("shell", "process"):
            self._tmux_send_keys(name, f"cd {self._sandbox_cwd}", enter=True)
            time.sleep(0.2)

        # Run initial command
        if command:
            self._tmux_send_keys(name, command, enter=True)

        tab = _RemoteTab(name=name, tab_type=tab_type)
        self._tabs[name] = tab
        logger.info(f"Opened remote tab '{name}' (type={tab_type})")
        return tab.to_metadata()

    def shell_close_tab(self, name: str) -> str:
        self._ensure_shell()

        if name not in self._tabs:
            raise KeyError(
                f"Tab '{name}' not found. Available: {', '.join(self._tabs.keys())}"
            )

        self._exec(f"tmux kill-window -t {self._session_name}:{name}")
        del self._tabs[name]
        logger.info(f"Closed remote tab '{name}'")
        return f"Tab '{name}' closed"

    def shell_list_tabs(self) -> List[Dict[str, Any]]:
        self._ensure_shell()
        return [tab.to_metadata() for tab in self._tabs.values()]

    def shell_format_tab_header(self) -> str:
        self._ensure_shell()
        names = list(self._tabs.keys())
        return f"[Shells: {' | '.join(names)}]"

    def shell_cleanup(self) -> None:
        if not self._shell_initialized:
            return
        try:
            self._exec(f"tmux kill-session -t {self._session_name}")
        except Exception as e:
            logger.warning(f"Error cleaning up remote tmux session: {e}")
        self._tabs.clear()
        self._shell_initialized = False

    def shell_is_alive(self) -> bool:
        if not self._shell_initialized:
            return False
        try:
            output = self._exec(
                f"tmux has-session -t {self._session_name} 2>/dev/null && echo yes || echo no"
            )
            return output.strip() == "yes"
        except Exception:
            return False
