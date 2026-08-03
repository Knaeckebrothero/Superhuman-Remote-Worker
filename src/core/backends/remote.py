"""Remote workspace backend via SSH/SFTP.

Connects to a VM over SSH for file operations (SFTP) and shell execution
(remote tmux over SSH). Implements the same sentinel-based completion
detection as the local ShellManager, but over SSH channels.

Requires: paramiko (pip install paramiko)
See docs/features/vm_backend.md for the full design.
"""

import base64
import errno
import fnmatch
import hashlib
import logging
import os
import posixpath
import re
import shlex
import socket
import stat as stat_module
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:
    paramiko = None  # Deferred — only needed when backend: remote is used

from ...tools.shell.shell_manager import (
    COLLIDING_COMMAND_TEMPLATE,
    HARD_TIMEOUT_CAP_SECONDS,
    INTERACTIVE_PROMPT_PATTERNS,
    INTERACTIVE_PROMPT_TEMPLATE,
    NO_CHANGE_TIMEOUT_SECONDS,
    NONINTERACTIVE_ENV_EXPORT,
    STILL_RUNNING_HARDCAP_TEMPLATE,
    STILL_RUNNING_TEMPLATE,
    SUDO_FREEZE_SENTINEL,
    build_sentinel_command,
    compute_no_change_state,
    prompt_is_ready,
)
from ..workspace_backend import (
    SEARCH_RESULT_HARD_CAP,
    RemoteChannelBusyError,
    RemoteCommandTimeoutError,
    WorkspaceAuthenticationError,
    WorkspaceBackend,
    WorkspaceUnavailableError,
)

logger = logging.getLogger(__name__)

# Stall/timeout constants, interactive-prompt patterns and message templates
# are shared from shell_manager (imported above) to keep the local and remote
# shell backends in lock-step.

# Connect-failure buckets → how many attempts each is worth.
_AMBIGUOUS_RETRY_CAP = 2
_GONE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN}
_AUTH_ERROR_MARKERS = (
    "authentication failed",
    "authentication methods available",
    "not a valid private key",
    "private key file is encrypted",
    "permission denied (publickey",
)

# _exec output cap: past this, output is dropped (marker appended) but the
# channel keeps draining so the remote command can finish. Guards agent RAM.
_EXEC_MAX_OUTPUT_BYTES = 5 * 1024 * 1024
_EXEC_POLL_SECONDS = 0.05

# Session-channel opens refused by sshd on a LIVE transport (MaxSessions) are
# transient concurrency refusals, not workspace death: retry briefly, then
# surface as RemoteChannelBusyError (an ordinary tool error).
# docs/issues/maxsessions_parallel_tools_false_workspace_death.md
_CHANNEL_OPEN_RETRIES = 3
_CHANNEL_OPEN_BACKOFF_SECONDS = 0.25

# shell_cancel: seconds to wait after a Ctrl+C before re-checking whether the
# tab returned to a prompt. Two attempts, then fall back to a tab reset.
_CANCEL_SETTLE_SECONDS = 0.5

# Transport-level keepalive + SFTP channel timeout: without these, a
# blackholed connection (network partition, silently-dead VM) hangs SFTP
# reads/writes forever while holding _sftp_lock, wedging ALL file ops.
_TRANSPORT_KEEPALIVE_SECONDS = 15
_SFTP_OP_TIMEOUT_SECONDS = 60.0
_TCP_KEEPALIVE_IDLE_SECONDS = 10
_TCP_KEEPALIVE_COUNT = 3
_TCP_KEEPALIVE_INTERVAL_SECONDS = 5
_TCP_USER_TIMEOUT_MILLIS = 10_000

# Explicit deadlines for heavy/recursive remote ops. Now that _exec's
# deadline actually binds (drain-loop fix), these need generous budgets or
# large trees would newly fail at the 30s default.
_HEAVY_OP_TIMEOUT_SECONDS = 300  # rm -rf, cp -a: recursive tree operations
_MEDIUM_OP_TIMEOUT_SECONDS = 120  # mv, du -sb: single-pass but can be slow


def _classify_connect_error(e: Exception) -> str:
    """Bucket an SSH connect failure to size the retry budget.

    'authentication' → the configured identity was rejected; retrying/recreating
                  the workspace with the same configuration cannot repair it.
    'gone'      → the workspace host is destroyed (DNS won't resolve / no route);
                  retrying is pointless, fail fast.
    'booting'   → host is up but sshd is not listening yet (ECONNREFUSED);
                  this is the boot window the retries exist for.
    'timeout'   → SYN/auth path timed out. For containers this stays a short
                  ambiguous retry; for VM backends it can mean the fresh
                  tailnet peer path is still converging.
    'ambiguous' → protocol / unknown; retry briefly then give up.
    """
    if paramiko is not None and isinstance(
        e, (paramiko.AuthenticationException, paramiko.PasswordRequiredException)
    ):
        return "authentication"
    if any(marker in str(e).lower() for marker in _AUTH_ERROR_MARKERS):
        return "authentication"
    if isinstance(e, socket.gaierror):
        return "gone"
    if isinstance(e, socket.timeout):
        return "timeout"
    if isinstance(e, ConnectionRefusedError):
        return "booting"
    if isinstance(e, OSError) and e.errno in _GONE_ERRNOS:
        return "gone"
    return "ambiguous"


def _validate_private_key(key_path: Optional[str]) -> str:
    """Validate the configured key as this worker UID; return public fingerprint."""
    if not key_path:
        raise WorkspaceAuthenticationError(
            "workspace.remote.key_path is missing; managed workspaces require "
            "an explicit private key"
        )
    if not os.path.isfile(key_path):
        raise WorkspaceAuthenticationError(
            f"workspace.remote.key_path does not exist: {key_path}"
        )
    try:
        with open(key_path, "rb"):
            pass
    except OSError as exc:
        raise WorkspaceAuthenticationError(
            f"workspace.remote.key_path is not readable by the worker: {key_path}"
        ) from exc
    try:
        private_key = paramiko.PKey.from_path(key_path)
    except (paramiko.SSHException, OSError, ValueError) as exc:
        raise WorkspaceAuthenticationError(
            "workspace.remote.key_path is invalid or passphrase-protected"
        ) from exc

    digest = hashlib.sha256(private_key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


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
        # Sentinel of a still-running command holding this tab (colliding guard).
        self.pending_sentinel: Optional[str] = None
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
    """Workspace on a remote host (sandbox container or VM), accessed via SSH/SFTP.

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
        no_change_timeout: float = NO_CHANGE_TIMEOUT_SECONDS,
        max_tabs: int = 15,
        blocked_commands: Optional[List[str]] = None,
        sandbox_cwd: Optional[str] = None,
        connect_timeout: int = 30,
        max_retries: int = 5,
        retry_timeouts_as_booting: bool = False,
        sudo_action: str = "freeze",
        sudo_block_message: Optional[str] = None,
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
        self._no_change_timeout = no_change_timeout
        self._max_tabs = max_tabs
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries
        self._retry_timeouts_as_booting = retry_timeouts_as_booting
        self._has_connected_once = False
        self._sudo_action = sudo_action
        # Optional custom message for sudo_action="block" — carries the
        # operator's denial reason after a denied vm_upgrade request, so the
        # agent gets a reasoned rejection instead of the generic block text.
        self._sudo_block_message = sudo_block_message

        if blocked_commands is None:
            self._blocked_commands = DEFAULT_BLOCKED_COMMANDS
        else:
            self._blocked_commands = frozenset(blocked_commands)

        # sandbox_cwd: if set, cd to this directory in each new tab
        self._sandbox_cwd = sandbox_cwd or workspace_path.rstrip("/")

        # SSH/SFTP handles
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._sftp_lock = (
            threading.RLock()
        )  # Guards all SFTP operations (not thread-safe)

        # Lazily-resolved $HOME on the remote (cached after first lookup).
        # Used by write_home_file / resolve_home_path for setup writes
        # outside the workspace tree (SSH key/config, etc.).
        self._home_dir: Optional[str] = None

        # Shell state
        self._session_name = f"agent_{job_id[:12]}" if job_id else "agent_remote"
        self._tabs: OrderedDict[str, _RemoteTab] = OrderedDict()
        self._sync_lock = threading.Lock()
        self._shell_initialized = False

        # Bound concurrent short-lived exec channels so a wide parallel tool
        # batch can never trip the workspace sshd's per-connection MaxSessions
        # limit (excess execs queue for the next free slot). Long-lived
        # channels (persistent SFTP, shell tabs) are the headroom between this
        # cap and MaxSessions.
        # docs/issues/maxsessions_parallel_tools_false_workspace_death.md
        self._channel_slots = threading.Semaphore(
            max(1, int(os.environ.get("WORKSPACE_SSH_MAX_CONCURRENT_CHANNELS", "10")))
        )

        # Reconnect hook: fired after a genuine reconnect in _ensure_connected
        # (NOT the initial connect()). Lets the agent re-assert files it wrote on
        # top of the pod's git clone (task_brief.md, bound skills) that a pod
        # tear-down + re-provision would have dropped. Kept generic — the backend
        # knows nothing about *what* gets re-seeded. See
        # docs/issues/reviewing_parent_pod_reaped_under_critic.md (Issue 4).
        self._on_reconnect: Optional[Callable[[], None]] = None
        self._reseeding = False  # re-entrancy guard: the hook writes via us

    @property
    def root(self) -> str:
        return self._remote_root

    @property
    def host(self) -> str | None:
        return self._host

    @property
    def supports_shell(self) -> bool:
        return True

    @property
    def sudo_action(self) -> str:
        """How this backend handles ``sudo`` (``"freeze"`` | ``"allow"`` |
        ``"block"``).

        Doubles as the tier discriminator for the live upgrade path: a ``vm``
        backend is built with ``sudo_action="allow"`` (its guest owns the sudo
        gate), a ``sandbox`` keeps ``"freeze"`` (sudo → VM-escalation). The
        workspace-upgrade handler reads this to tell sandbox from vm, since both
        report ``supports_shell == True`` (workspace_tier_upgrade.md Phase 2 /
        Q8).
        """
        return self._sudo_action

    def exec_command(self, command: str, timeout: int = 30) -> str:
        """Execute a command via SSH and return stdout.

        Public wrapper around _exec for use by tools that need to run
        commands on the workspace host (e.g., starting Chromium for CDP).
        """
        return self._exec(command, timeout=timeout)

    def open_forward_channel(self, dest_host: str = "127.0.0.1", dest_port: int = 8080):
        """Open a ``direct-tcpip`` channel to a loopback port on the workspace.

        Returns a paramiko ``Channel`` (socket-like) tunnelled over the existing
        authenticated SSH transport, for reaching a guest-loopback service that
        is not exposed on the pod/VM network — e.g. code-server for a live-VM
        IDE session (docs/features/vm_snapshots_and_ide.md, "Live-VM IDE Access
        via the Agent"). The workspace sshd permits exactly this via
        ``AllowTcpForwarding local`` + ``PermitOpen 127.0.0.1:*``.

        The returned channel is a *blocking* socket; a caller on an event loop
        must drive its ``recv``/``send`` off-thread (e.g. ``asyncio.to_thread``).
        It is independent of the shell/SFTP state, so an IDE session and the
        job's own workspace I/O multiplex over one transport without interfering.

        Raises:
            WorkspaceUnavailableError: if the SSH transport is not connected.
        """
        self._ensure_connected()
        transport = self._ssh.get_transport() if self._ssh else None
        if transport is None or not transport.is_active():
            raise WorkspaceUnavailableError(
                f"No active SSH transport to {self._host}:{self._port} "
                "for IDE port-forward"
            )
        # src_addr is informational (the notional channel origin); dest_addr is
        # the guest-loopback service we forward to.
        return transport.open_channel(
            "direct-tcpip", (dest_host, dest_port), ("127.0.0.1", 0)
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self) -> None:
        """Establish SSH connection and SFTP channel.

        Retries to tolerate the window between daemon registration (NATS) and
        SSHD readiness, but classifies the failure (``_classify_connect_error``)
        so a workspace that is *gone* (DNS won't resolve / no route) fails fast
        instead of burning the full boot-window budget.
        See docs/issues/agent_fast_freeze_on_dead_workspace.md.
        """
        fingerprint = _validate_private_key(self._key_path)
        logger.info(
            "Workspace SSH private key validated for %s:%d (fingerprint=%s)",
            self._host,
            self._port,
            fingerprint,
        )
        connect_kwargs = {
            "hostname": self._host,
            "port": self._port,
            "username": self._username,
            "timeout": self._connect_timeout,
            "key_filename": self._key_path,
            "allow_agent": False,
            "look_for_keys": False,
        }

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
                if bucket == "authentication":
                    raise WorkspaceAuthenticationError(
                        f"Workspace SSH authentication failed for "
                        f"{self._host}:{self._port} with key {fingerprint}: {e}"
                    ) from e
                if bucket == "gone":
                    effective_max = 1
                elif (
                    bucket == "timeout"
                    and self._retry_timeouts_as_booting
                    and not self._has_connected_once
                ):
                    effective_max = self._max_retries
                elif bucket in ("timeout", "ambiguous"):
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
                    attempt,
                    effective_max,
                    self._host,
                    self._port,
                    bucket,
                    e,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

        self._sftp = self._ssh.open_sftp()
        transport = self._ssh.get_transport()
        if transport is not None:
            transport.set_keepalive(_TRANSPORT_KEEPALIVE_SECONDS)
            try:
                sock = transport.get_socket()
            except Exception:
                sock = None
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    # Keepalive may be unsupported on custom transport stacks.
                    logger.debug(
                        "SO_KEEPALIVE unavailable for transport socket", exc_info=True
                    )
                if hasattr(socket, "TCP_KEEPIDLE"):
                    try:
                        sock.setsockopt(
                            socket.IPPROTO_TCP,
                            socket.TCP_KEEPIDLE,
                            _TCP_KEEPALIVE_IDLE_SECONDS,
                        )
                    except Exception:
                        logger.debug(
                            "TCP_KEEPIDLE unavailable for transport socket",
                            exc_info=True,
                        )
                if hasattr(socket, "TCP_KEEPINTVL"):
                    try:
                        sock.setsockopt(
                            socket.IPPROTO_TCP,
                            socket.TCP_KEEPINTVL,
                            _TCP_KEEPALIVE_INTERVAL_SECONDS,
                        )
                    except Exception:
                        logger.debug(
                            "TCP_KEEPINTVL unavailable for transport socket",
                            exc_info=True,
                        )
                if hasattr(socket, "TCP_KEEPCNT"):
                    try:
                        sock.setsockopt(
                            socket.IPPROTO_TCP,
                            socket.TCP_KEEPCNT,
                            _TCP_KEEPALIVE_COUNT,
                        )
                    except Exception:
                        logger.debug(
                            "TCP_KEEPCNT unavailable for transport socket",
                            exc_info=True,
                        )
                if hasattr(socket, "TCP_USER_TIMEOUT"):
                    try:
                        sock.setsockopt(
                            socket.IPPROTO_TCP,
                            socket.TCP_USER_TIMEOUT,
                            _TCP_USER_TIMEOUT_MILLIS,
                        )
                    except Exception:
                        logger.debug(
                            "TCP_USER_TIMEOUT unavailable for transport socket",
                            exc_info=True,
                        )
        sftp_chan = self._sftp.get_channel()
        if sftp_chan is not None:
            sftp_chan.settimeout(_SFTP_OP_TIMEOUT_SECONDS)
        self._has_connected_once = True
        logger.info(f"Connected to workspace {self._host}:{self._port}")

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

        logger.info(f"Disconnected from workspace {self._host}")

    def is_connected(self) -> bool:
        if self._ssh is None:
            return False
        transport = self._ssh.get_transport()
        return transport is not None and transport.is_active()

    def set_reconnect_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Register a callback fired after a genuine reconnect (see
        ``_ensure_connected``). Used by the agent to idempotently re-assert
        files it wrote on top of the pod's git clone, which a pod tear-down +
        re-provision drops (task_brief.md, bound skills)."""
        self._on_reconnect = hook

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
        # A reconnect means the SSH transport died — often because the workspace
        # pod was torn down and re-provisioned. The new pod has its git clone but
        # not the files the agent wrote on top, so re-assert them. Guard against
        # re-entrancy: the hook writes files through us, which re-enters
        # _ensure_connected (now live → no-op), but a drop mid-hook must not
        # recurse into the hook again.
        if self._on_reconnect is not None and not self._reseeding:
            self._reseeding = True
            try:
                self._on_reconnect()
            except Exception:
                logger.exception(
                    "Workspace re-seed hook failed after reconnect (non-fatal)"
                )
            finally:
                self._reseeding = False

    def _exec(self, command: str, timeout: int = 30) -> str:
        """Execute a command via SSH and return stdout.

        Drains stdout AND stderr while waiting for the exit status, so output
        larger than the SSH channel window cannot deadlock the command
        (docs/issues/remote_backend_indefinite_wait_deadlock.md), and enforces
        ``timeout`` as a wall-clock deadline on the whole command.

        Raises WorkspaceUnavailableError on connection failure,
        RemoteCommandTimeoutError when the deadline expires, and
        RemoteChannelBusyError when sshd keeps refusing session channels on a
        live transport (MaxSessions saturation — NOT workspace death; see
        docs/issues/maxsessions_parallel_tools_false_workspace_death.md).
        """
        self._ensure_connected()
        last_refusal: Optional[paramiko.ChannelException] = None
        for attempt in range(_CHANNEL_OPEN_RETRIES + 1):
            try:
                return self._exec_once(command, timeout)
            except paramiko.ChannelException as e:
                # A clean CHANNEL_OPEN_FAILURE arrives over a working
                # transport. Dead transport → genuine workspace loss; live
                # transport → per-connection session-limit refusal, retryable
                # in milliseconds once a running command finishes.
                transport = self._ssh.get_transport() if self._ssh else None
                if transport is None or not transport.is_active():
                    raise WorkspaceUnavailableError(
                        f"SSH command failed on {self._host}: {e}"
                    ) from e
                last_refusal = e
                if attempt < _CHANNEL_OPEN_RETRIES:
                    logger.warning(
                        f"sshd refused session channel on live transport to "
                        f"{self._host} (attempt {attempt + 1}/"
                        f"{_CHANNEL_OPEN_RETRIES + 1}) — retrying"
                    )
                    time.sleep(_CHANNEL_OPEN_BACKOFF_SECONDS * (2**attempt))
            except (paramiko.SSHException, socket.error, EOFError, OSError) as e:
                raise WorkspaceUnavailableError(
                    f"SSH command failed on {self._host}: {e}"
                ) from e
        raise RemoteChannelBusyError(
            f"sshd on {self._host} refused a session channel "
            f"{_CHANNEL_OPEN_RETRIES + 1} times while the transport stayed "
            f"active (too many concurrent SSH sessions). The workspace is "
            f"healthy — retry the command."
        ) from last_refusal

    def _exec_once(self, command: str, timeout: int) -> str:
        """Single exec attempt — open channel, drain, return stdout.

        Raises paramiko/socket errors raw; ``_exec`` owns classification.
        Holds a ``_channel_slots`` permit for the channel's whole lifetime and
        closes the channel explicitly so the server-side session slot frees
        as soon as the command is done.
        """
        with self._channel_slots:
            _, stdout, _ = self._ssh.exec_command(command, timeout=timeout)
            chan = stdout.channel
            try:
                return self._drain_exec_channel(chan, command, timeout)
            finally:
                chan.close()

    def _drain_exec_channel(self, chan, command: str, timeout: int) -> str:
        """Drain stdout/stderr until exit under a wall-clock deadline."""
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        out_size = 0
        err_size = 0
        truncated = False
        deadline = time.monotonic() + timeout
        while True:
            # Bound the inner drains by the deadline too: a producer
            # that keeps recv_ready() True (output arriving faster than
            # we can be pre-empted) would otherwise evade the timeout
            # forever, since the deadline check below only ran once
            # BOTH buffers went momentarily empty.
            while chan.recv_ready() and time.monotonic() <= deadline:
                chunk = chan.recv(65536)
                if out_size < _EXEC_MAX_OUTPUT_BYTES:
                    out_chunks.append(chunk)
                else:
                    truncated = True
                out_size += len(chunk)
            while chan.recv_stderr_ready() and time.monotonic() <= deadline:
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

    def _get_home_dir(self) -> str:
        """Lazily resolve and cache the agent user's $HOME via SFTP.

        SFTP's default cwd after connection is the user's home directory,
        so normalize(".") returns its canonical absolute path. Cached for
        the lifetime of the backend.
        """
        if self._home_dir is not None:
            return self._home_dir
        self._ensure_connected()
        with self._sftp_lock:
            self._home_dir = self._sftp.normalize(".")
        return self._home_dir

    def _resolve_home_path(self, relative_path: str) -> str:
        """Resolve a path under $HOME, rejecting empty/absolute/escaping inputs."""
        if not relative_path or relative_path == ".":
            raise ValueError("resolve_home_path requires a non-empty relative path")
        cleaned = posixpath.normpath(relative_path)
        if cleaned.startswith("..") or cleaned.startswith("/"):
            raise ValueError(f"Path '{relative_path}' escapes home directory")
        home = self._get_home_dir()
        full = posixpath.join(home, cleaned)
        if not full.startswith(home + "/") and full != home:
            raise ValueError(f"Path '{relative_path}' escapes home directory")
        return full

    def _remote_stat(self, remote_path: str) -> Optional[paramiko.SFTPAttributes]:
        """Get SFTP stat, returning None if path doesn't exist.

        socket.timeout is an IOError subclass, so it must be special-cased
        BEFORE the generic IOError handler — otherwise a stalled channel
        (see _SFTP_OP_TIMEOUT_SECONDS) reads as "path doesn't exist",
        which corrupts exists/is_file/is_dir/move/copy/stat/delete_directory
        semantics all built on top of this method.
        """
        self._ensure_connected()
        with self._sftp_lock:
            try:
                return self._sftp.stat(remote_path)
            except FileNotFoundError:
                return None
            except socket.timeout as e:
                raise RemoteCommandTimeoutError(
                    f"Workspace I/O timed out stat {remote_path}"
                ) from e
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

        with self._sftp_lock:
            for d in reversed(parts_to_create):
                try:
                    self._sftp.mkdir(d)
                except socket.timeout as e:
                    raise RemoteCommandTimeoutError(
                        f"Workspace I/O timed out mkdir {d}"
                    ) from e
                except IOError:
                    # Race condition or already exists
                    pass

    # =========================================================================
    # File operations
    # =========================================================================

    def read_file(self, path: str, binary: bool = False) -> str | bytes:
        self._ensure_connected()
        remote_path = self._resolve(path)
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

        if binary:
            return data
        return data.decode("utf-8")

    def write_file(self, path: str, content: str | bytes) -> None:
        self._ensure_connected()
        remote_path = self._resolve(path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_dir(parent)

        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with self._sftp_lock:
            try:
                with self._sftp.open(remote_path, "wb") as f:
                    f.write(data)
            except socket.timeout as e:
                raise RemoteCommandTimeoutError(
                    f"Workspace I/O timed out writing {path}"
                ) from e
        logger.debug(f"Wrote remote file: {path}")

    def write_home_file(self, relative_path: str, content: str | bytes) -> None:
        self._ensure_connected()
        remote_path = self._resolve_home_path(relative_path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_dir(parent)

        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with self._sftp_lock:
            try:
                with self._sftp.open(remote_path, "wb") as f:
                    f.write(data)
            except socket.timeout as e:
                raise RemoteCommandTimeoutError(
                    f"Workspace I/O timed out writing {relative_path}"
                ) from e
        logger.debug(f"Wrote remote home file: {relative_path}")

    def resolve_home_path(self, relative_path: str) -> str:
        return self._resolve_home_path(relative_path)

    def append_file(self, path: str, content: str) -> None:
        self._ensure_connected()
        remote_path = self._resolve(path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_dir(parent)

        with self._sftp_lock:
            try:
                with self._sftp.open(remote_path, "ab") as f:
                    f.write(content.encode("utf-8"))
            except socket.timeout as e:
                raise RemoteCommandTimeoutError(
                    f"Workspace I/O timed out writing {path}"
                ) from e

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

        with self._sftp_lock:
            try:
                entries = self._sftp.listdir_attr(remote_path)
            except socket.timeout as e:
                raise RemoteCommandTimeoutError(
                    f"Workspace I/O timed out listing {remote_path}"
                ) from e
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
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: list[str] | None = None,
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
        exclude_dir_flags = ""
        if exclude_dirs:
            # Use safe shell-quoting for single quotes inside directory names:
            # Keep this format aligned with query escaping.
            exclude_dir_flags = " ".join(
                "--exclude-dir='{}'".format(d.replace("'", "'\\''"))
                for d in exclude_dirs
            )

        cmd = (
            f"grep {flags} {excludes} {exclude_dir_flags} -- '{safe_query}' {remote_path} "
            "2>/dev/null "
            f"| head -n {SEARCH_RESULT_HARD_CAP} || true"
        )
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
        self._ensure_remote_dir(remote_path)  # Already holds _sftp_lock internally
        logger.debug(f"Created remote directory: {path}")

    def delete_file(self, path: str) -> bool:
        self._ensure_connected()
        remote_path = self._resolve(path)
        st = self._remote_stat(remote_path)
        if st is None:
            return False

        with self._sftp_lock:
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
        self._exec(f"rm -rf '{safe_path}'", timeout=_HEAVY_OP_TIMEOUT_SECONDS)
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
        self._exec(f"mv '{safe_src}' '{safe_dst}'", timeout=_MEDIUM_OP_TIMEOUT_SECONDS)
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
        self._exec(
            f"cp -a '{safe_src}' '{safe_dst}'", timeout=_HEAVY_OP_TIMEOUT_SECONDS
        )
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
        output = self._exec(
            f"du -sb '{safe_path}' 2>/dev/null || echo '0'",
            timeout=_MEDIUM_OP_TIMEOUT_SECONDS,
        )
        try:
            return int(output.split()[0])
        except (ValueError, IndexError):
            return 0

    # =========================================================================
    # Shell operations — remote tmux over SSH
    # =========================================================================

    def _send_and_wait(self, tab_name: str, command: str, timeout: float = 5.0) -> None:
        """Send a setup command and block until it has run (marker prints).

        Mirrors ShellManager._send_and_wait so tab-creation preamble (env, cd)
        settles before the first user command instead of racing it.
        """
        marker = f"__READY_{uuid.uuid4().hex[:8]}__"
        self._tmux_send_keys(tab_name, f"{command}; echo {marker}", enter=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                lines = self._tmux_capture(tab_name)
            except Exception:
                return
            if any(ln.strip() == marker for ln in lines):
                return
            time.sleep(0.1)

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

        # Non-interactive env + working directory; wait for it to settle so the
        # preamble doesn't fold into the first command's output.
        setup = NONINTERACTIVE_ENV_EXPORT
        if self._sandbox_cwd:
            setup += f"; cd {self._sandbox_cwd}"
        self._send_and_wait("default", setup)

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
        """Return error message if command is blocked, else None.

        Returns SUDO_FREEZE_SENTINEL when sudo_action is "freeze" and the
        command starts with sudo. The tool layer detects this sentinel and
        triggers a job freeze (VM upgrade prompt).
        """
        first_word = command.strip().split()[0] if command.strip() else ""
        if not first_word:
            return None

        # Sudo intercept (separate from blocked_commands)
        if first_word == "sudo":
            if self._sudo_action == "allow":
                return None  # VM-backed workspace: pass through to sudo gate
            elif self._sudo_action == "freeze":
                return SUDO_FREEZE_SENTINEL
            else:  # "block"
                return self._sudo_block_message or (
                    "Command blocked: 'sudo' is not available in this workspace. "
                    "System package installation requires a VM runtime."
                )

        # Standard blocked commands
        if self._blocked_commands and first_word in self._blocked_commands:
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
        if prompt_is_ready(all_lines):
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
        timeout: Optional[int] = None,
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

        # Explicit timeout disables the soft no-change timeout; only the hard
        # timeout bounds the command then.
        soft_enabled = timeout is None
        if timeout is None:
            timeout = self._default_timeout
        timeout = min(timeout, HARD_TIMEOUT_CAP_SECONDS)
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

            # Colliding-command guard: a previous command may still be running.
            if tab.pending_sentinel is not None:
                prev_done = any(
                    ln.strip().startswith(tab.pending_sentinel) for ln in pre_lines
                )
                if prev_done or prompt_is_ready(pre_lines):
                    tab.pending_sentinel = None
                else:
                    state_lines = [ln for ln in pre_lines if ln.strip()][-30:]
                    terminal_state = "\n".join(state_lines) or "(empty)"
                    return COLLIDING_COMMAND_TEMPLATE.format(
                        tab=tab_name, terminal_state=terminal_state
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

            # Build the sentinel-suffixed command. Multi-line commands get
            # wrapped in a bash heredoc so inner heredocs / multi-statement
            # scripts work correctly (BUG-5). See build_sentinel_command in
            # shell_manager.py for the rationale.
            full_cmd, start_marker = build_sentinel_command(command, sentinel)
            self._tmux_send_keys(tab_name, full_cmd, enter=True)

            # Poll for sentinel
            start_time = time.monotonic()
            output_text = ""
            exit_code = None
            resolved_cwd = None
            last_content_hash = None
            stall_start = None

            while time.monotonic() - start_time < timeout:
                time.sleep(0.3)  # Slightly longer poll interval for SSH latency
                try:
                    all_lines = self._tmux_capture(tab_name)
                except WorkspaceUnavailableError:
                    raise

                # Find sentinel output line
                sentinel_line_idx = None
                for i in range(len(all_lines) - 1, -1, -1):
                    stripped = all_lines[i].strip()
                    if stripped.startswith(sentinel):
                        sentinel_line_idx = i
                        break

                if sentinel_line_idx is not None:
                    line = all_lines[sentinel_line_idx]
                    sentinel_parts = line.strip().split(maxsplit=2)
                    try:
                        exit_code = int(sentinel_parts[1])
                    except (ValueError, IndexError):
                        exit_code = 1
                    if len(sentinel_parts) == 3:
                        resolved_cwd = sentinel_parts[2]

                    # Command finished — tab is no longer busy.
                    tab.pending_sentinel = None

                    if start_marker is not None:
                        # Multi-line wrap path: locate the start marker output
                        # line and extract everything between it and the sentinel.
                        start_idx = None
                        for i in range(sentinel_line_idx - 1, -1, -1):
                            if all_lines[i].strip().startswith(start_marker):
                                start_idx = i
                                break
                        if start_idx is not None:
                            new_lines = all_lines[start_idx + 1 : sentinel_line_idx]
                            output_lines = [
                                ol
                                for ol in new_lines
                                if start_marker not in ol and sentinel not in ol
                            ]
                        else:
                            new_lines = all_lines[pre_count:sentinel_line_idx]
                            output_lines = [
                                ol for ol in new_lines if sentinel not in ol
                            ]
                    else:
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

                # Genuine interactive prompt (waiting for input)
                elapsed = time.monotonic() - start_time
                if elapsed > 1.0:
                    prompt_type = self._detect_interactive_prompt(all_lines)
                    if prompt_type:
                        terminal_state = self._capture_terminal_state(
                            tab_name, sentinel, pre_count
                        )
                        # Command owns the pane; don't cwd-restore into it.
                        tab.pending_sentinel = sentinel
                        tab.last_activity = datetime.now(timezone.utc)
                        return INTERACTIVE_PROMPT_TEMPLATE.format(
                            prompt_type=prompt_type,
                            tab=tab_name,
                            terminal_state=terminal_state,
                        )

                    # Soft no-change timeout (command still running)
                    last_content_hash, stall_start, timed_out = compute_no_change_state(
                        all_lines,
                        last_content_hash,
                        stall_start,
                        time.monotonic(),
                        soft_enabled,
                        self._no_change_timeout,
                    )
                    if timed_out:
                        terminal_state = self._capture_terminal_state(
                            tab_name, sentinel, pre_count
                        )
                        # Leave it running; don't cwd-restore into a busy pane.
                        tab.pending_sentinel = sentinel
                        tab.last_activity = datetime.now(timezone.utc)
                        return STILL_RUNNING_TEMPLATE.format(
                            tab=tab_name,
                            elapsed=elapsed,
                            quiet=time.monotonic() - stall_start,
                            terminal_state=terminal_state,
                        )

            # Hard timeout: loop exited without the sentinel -> still running.
            if exit_code is None:
                terminal_state = self._capture_terminal_state(
                    tab_name, sentinel, pre_count
                )
                tab.pending_sentinel = sentinel
                tab.last_activity = datetime.now(timezone.utc)
                return STILL_RUNNING_HARDCAP_TEMPLATE.format(
                    tab=tab_name, elapsed=timeout, terminal_state=terminal_state
                )

            # Completed — restore working directory if it was changed.
            if working_dir and self._sandbox_cwd:
                self._tmux_send_keys(tab_name, f"cd {self._sandbox_cwd}", enter=True)
                time.sleep(0.1)

            tab.last_activity = datetime.now(timezone.utc)

            # Format output
            parts = [
                f"Exit code: {exit_code}",
                f"CWD: {resolved_cwd or '(unknown)'}",
            ]
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
        working_dir: Optional[str] = None,
    ) -> str:
        self._ensure_shell()

        if working_dir and not enter:
            raise ValueError("working_dir cannot be applied to raw keystrokes")

        if enter:
            blocked = self._check_blocked(text)
            if blocked:
                return blocked

        if tab_name not in self._tabs:
            raise KeyError(
                f"Tab '{tab_name}' not found. Available: {', '.join(self._tabs.keys())}"
            )

        if working_dir:
            full_dir = (
                posixpath.join(self._sandbox_cwd, working_dir)
                if self._sandbox_cwd
                else working_dir
            )
            restore = (
                f"; cd {shlex.quote(self._sandbox_cwd)}" if self._sandbox_cwd else ""
            )
            text = (
                f"cd {shlex.quote(full_dir)} && "
                "printf 'CWD: %s\\n' \"$PWD\" && "
                f"eval {shlex.quote(text)}{restore}"
            )

        self._tmux_send_keys(tab_name, text, enter=enter)
        self._tabs[tab_name].last_activity = datetime.now(timezone.utc)
        return f"Sent to '{tab_name}'"

    def shell_cancel(self, tab_name: str = "default") -> str:
        """Send Ctrl+C to a shell tab to abort a stuck/hung command.

        Mirrors the manual ``shell_execute(keys=True, command="C-c")`` recovery
        path, but as a first-class action for the stateless tool set (whose
        ``run_command`` has no keys mode). Ladder, each step re-checking whether
        the shell returned to a prompt (``prompt_is_ready``): idle -> no-op;
        otherwise up to two C-c sends; if the process still ignores SIGINT, reset
        the tab (close + reopen). Always leaves the tab runnable.

        The interrupted command's completion sentinel never prints, so we clear
        ``pending_sentinel`` here rather than waiting for the colliding-command
        guard to clear it on the next ``shell_run``.
        """
        self._ensure_shell()
        if tab_name not in self._tabs:
            raise KeyError(
                f"Tab '{tab_name}' not found. Available: {', '.join(self._tabs.keys())}"
            )
        tab = self._tabs[tab_name]

        with self._sync_lock:
            # Nothing running — already back at a prompt.
            if prompt_is_ready(self._tmux_capture(tab_name)):
                tab.pending_sentinel = None
                return f"Tab '{tab_name}': nothing to cancel (already at a prompt)."

            # Up to two Ctrl+C attempts (some REPLs/programs need a second).
            for _ in range(2):
                self._tmux_send_keys(tab_name, "C-c", enter=False)
                time.sleep(_CANCEL_SETTLE_SECONDS)
                if prompt_is_ready(self._tmux_capture(tab_name)):
                    tab.pending_sentinel = None
                    tab.last_activity = datetime.now(timezone.utc)
                    return (
                        f"Sent Ctrl+C to tab '{tab_name}'; the command was "
                        f"interrupted and the tab is free."
                    )

        # Still stuck (process ignoring SIGINT). Reset the tab outside the lock:
        # close + reopen yields a fresh _RemoteTab (pending_sentinel cleared by
        # construction) and leaves the tab immediately usable.
        self.shell_close_tab(tab_name)
        self.shell_ensure_tab(tab_name)
        return (
            f"Tab '{tab_name}' did not respond to Ctrl+C; the shell tab was "
            f"reset (shell-local state and background jobs were cleared)."
        )

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
                from src.tools.shell.shell_manager import COMMAND_TYPE_MAP

                first_word = command.strip().split()[0]
                base_cmd = first_word.rsplit("/", 1)[-1]
                tab_type = COMMAND_TYPE_MAP.get(base_cmd, "process")

        # Create new tmux window
        self._exec(f"tmux new-window -t {self._session_name} -n {name} -d")

        # Non-interactive env + working directory (shell/process tabs only).
        if tab_type in ("shell", "process"):
            setup = NONINTERACTIVE_ENV_EXPORT
            if self._sandbox_cwd:
                setup += f"; cd {self._sandbox_cwd}"
            self._send_and_wait(name, setup)

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
