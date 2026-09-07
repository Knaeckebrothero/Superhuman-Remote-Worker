"""Credential-safe git content sources for OKF knowledge-base indexing.

The indexer needs only three git operations: resolve a branch HEAD, list the
tree at that immutable commit, and read selected Markdown blobs.  This module
keeps those operations behind a small protocol so the native project Gitea
repository and an arbitrary external git remote share the same reindex path.

External repositories are never checked out into an agent workspace.  A
``RemoteKnowledgeGitSource`` creates one disposable bare/partial repository per
snapshot context and removes the repository and all authentication material on
every exit path.  Secrets are supplied to git only through temporary askpass /
SSH files and environment variables; they are never added to a URL or argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import tarfile
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import AsyncContextManager, Protocol
from urllib.parse import quote, quote_plus, urlsplit

logger = logging.getLogger(__name__)


class KnowledgeGitSnapshot(Protocol):
    """An immutable view of a knowledge repository at one git ref."""

    async def list_tree(self) -> list[dict[str, str]]:
        """Return safe Markdown blobs as ``{path, type, sha}`` entries."""

    async def get_file(self, path: str) -> str | None:
        """Return a UTF-8 Markdown blob, or ``None`` when it is not in the tree."""


class KnowledgeGitSource(Protocol):
    """Provider-neutral source used by the KB reindexer."""

    async def get_head(self) -> str | None:
        """Resolve the configured branch (or remote default) to a commit SHA."""

    def snapshot(self, ref: str) -> AsyncContextManager[KnowledgeGitSnapshot]:
        """Open one immutable, automatically cleaned snapshot at ``ref``."""

    @property
    def label(self) -> str:
        """Return a non-secret diagnostic label."""


class KnowledgeGitSourceError(RuntimeError):
    """A bounded, credential-redacted git source failure."""


class GitCommandError(KnowledgeGitSourceError):
    """A git subprocess failed or exceeded its resource bounds."""


@dataclass(frozen=True)
class GitCommandTimeouts:
    """Timeouts in seconds for remote and local git operations."""

    head: float = 30.0
    fetch: float = 120.0
    tree: float = 30.0
    blob: float = 30.0


@dataclass(frozen=True)
class GitOutputLimits:
    """Maximum bytes retained from each subprocess stream."""

    head: int = 1024 * 1024
    fetch: int = 1024 * 1024
    tree: int = 16 * 1024 * 1024
    blob: int = 8 * 1024 * 1024


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_URL_USERINFO_RE = re.compile(r"((?:https?|ssh|git)://)[^/@\s]+@", re.IGNORECASE)
_SCP_PASSWORD_RE = re.compile(r"(?<!\w)[^\s/:@]+:[^\s/@]+@(?=[^\s/:]+[:/])")
_SAFE_SCP_REMOTE_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@"
    r"(?P<host>(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])):"
    r"(?P<path>[^\s?#]+)$"
)
_SAFE_SSH_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_SAFE_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FILTER_UNSUPPORTED_MARKERS = (
    "filtering not recognized",
    "does not support filter",
    "does not support --filter",
    "filter is not supported",
    "server does not support",
)
_AUTH_METHODS = {"public", "token", "password", "ssh"}
_DEFAULT_TRUSTED_GIT_HOSTS = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
)
_DEFAULT_GIT_PORTS = {"http": 80, "https": 443, "ssh": 22}


def redact_git_error(value: str, secrets: Sequence[str] = ()) -> str:
    """Remove credential material from git diagnostics.

    Exact secret values and their common URL-encoded forms are redacted.  URL
    userinfo is scrubbed independently so even a remote that echoes a malformed
    credential-bearing URL cannot put it into an exception or log record.
    """

    redacted = value
    variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        variants.add(secret)
        variants.add(quote(secret, safe=""))
        variants.add(quote_plus(secret, safe=""))
        # Private-key diagnostics sometimes echo a single header/body line,
        # rather than the complete multiline value.
        variants.update(line for line in secret.splitlines() if len(line) >= 8)
    for secret in sorted(variants, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", redacted)
    redacted = _SCP_PASSWORD_RE.sub("[REDACTED]@", redacted)
    return redacted


class _OutputLimitExceeded(Exception):
    pass


async def _read_limited(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _OutputLimitExceeded
        chunks.append(chunk)


def _verified_process_group(process: asyncio.subprocess.Process) -> int | None:
    """Capture the child-led group while its new session leader is alive."""
    if os.name != "posix":
        return None
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        process_group = os.getpgid(pid)
        if process_group != pid:
            return None
        return process_group
    except (AttributeError, OSError):
        return None


def _signal_process_group(process_group: int, signal_number: int) -> bool:
    """Signal a previously verified child-led process group."""
    try:
        os.killpg(process_group, signal_number)
        return True
    except (AttributeError, OSError):
        return False


def _signal_single_process(
    process: asyncio.subprocess.Process, *, terminate: bool
) -> None:
    method = getattr(process, "terminate" if terminate else "kill", None)
    if method is None and terminate:
        method = getattr(process, "kill", None)
    if method is not None:
        with contextlib.suppress(ProcessLookupError):
            method()


async def _stop_process(
    process: asyncio.subprocess.Process,
    process_group: int | None,
) -> None:
    """Terminate the whole Git helper group, then force-kill and reap it."""

    if process.returncode is not None and process_group is None:
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        return

    group_signalled = process_group is not None and _signal_process_group(
        process_group, signal.SIGTERM
    )
    if not group_signalled:
        _signal_single_process(process, terminate=True)
    loop = asyncio.get_running_loop()
    grace_deadline = loop.time() + 0.1
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=0.1)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    # The Git leader may exit promptly while ssh/git-remote-http/askpass ignores
    # TERM. Preserve the captured PGID for the entire grace before force-killing
    # that group; leader exit alone is not a cleanup success signal.
    remaining_grace = grace_deadline - loop.time()
    if group_signalled and remaining_grace > 0:
        await asyncio.sleep(remaining_grace)

    group_killed = bool(
        group_signalled
        and process_group is not None
        and _signal_process_group(process_group, signal.SIGKILL)
    )
    if process.returncode is None:
        if not group_killed:
            _signal_single_process(process, terminate=False)
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


async def _run_git(
    *args: str,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int,
    operation: str,
    secrets: Sequence[str] = (),
    check: bool = True,
) -> _CommandResult:
    """Run git without a shell, bounding time, stdout, stderr, and errors."""

    operation_env = dict(env)
    ssh_command = operation_env.get("GIT_SSH_COMMAND")
    if ssh_command:
        operation_env["GIT_SSH_COMMAND"] = re.sub(
            r"\bConnectTimeout=\d+\b",
            f"ConnectTimeout={max(1, int(timeout))}",
            ssh_command,
        )
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=operation_env,
        start_new_session=True,
    )
    process_group = _verified_process_group(process)
    stdout_task = asyncio.create_task(_read_limited(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, output_limit))
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        stdout, stderr, returncode = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=max(0.001, timeout)
        )
    except asyncio.TimeoutError as exc:
        await _stop_process(process, process_group)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise GitCommandError(f"Git {operation} timed out") from exc
    except _OutputLimitExceeded as exc:
        await _stop_process(process, process_group)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise GitCommandError(f"Git {operation} exceeded its output limit") from exc
    except BaseException:
        await _stop_process(process, process_group)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    result = _CommandResult(returncode, stdout, stderr)
    if check and returncode != 0:
        detail = redact_git_error(
            stderr.decode("utf-8", errors="replace").strip(), secrets
        )
        # Keep operational diagnostics useful but bounded independently of the
        # subprocess stream cap.
        detail = detail[-2000:] if detail else f"exit status {returncode}"
        raise GitCommandError(f"Git {operation} failed: {detail}")
    return result


def _clean_git_env() -> dict[str, str]:
    """Return an inherited environment without ambient credential hooks."""

    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    for key in (
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
    ):
        env.pop(key, None)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            # Git's HTTP transport otherwise follows an initial redirect to a
            # different host, bypassing the exact datasource host allowlist.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.followRedirects",
            "GIT_CONFIG_VALUE_0": "false",
            "LC_ALL": "C.UTF-8",
        }
    )
    return env


def _write_private_file(path: Path, value: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


class _GitAuthContext:
    """Temporary git authentication environment with no secrets in argv."""

    def __init__(
        self,
        credentials: Mapping[str, object],
        *,
        parent: Path | None,
        ssh_transport: bool,
        connect_timeout: float,
    ) -> None:
        self._credentials = credentials
        self._parent = parent
        self._ssh_transport = ssh_transport
        self._connect_timeout = max(1, int(connect_timeout))
        self._directory: Path | None = None
        self.env: dict[str, str] = {}
        self.secrets: tuple[str, ...] = ()

    async def __aenter__(self) -> _GitAuthContext:
        auth_method = str(self._credentials.get("auth_method") or "").lower()
        ssh_key = str(self._credentials.get("ssh_key") or "")
        password = str(
            self._credentials.get("token") or self._credentials.get("password") or ""
        )
        if not auth_method:
            auth_method = "ssh" if ssh_key else ("token" if password else "public")
        if auth_method == "ssh" and not ssh_key:
            raise KnowledgeGitSourceError("SSH authentication requires an SSH key")

        parent = str(self._parent) if self._parent else None
        self._directory = Path(tempfile.mkdtemp(prefix="srw-kb-auth-", dir=parent))
        try:
            env = _clean_git_env()
            env["HOME"] = str(self._directory)
            env["XDG_CONFIG_HOME"] = str(self._directory)

            secrets: list[str] = []
            known_hosts: Path | None = None
            if self._ssh_transport:
                pinned_known_hosts = os.getenv("KB_GIT_SSH_KNOWN_HOSTS", "")
                if (
                    "\x00" in pinned_known_hosts
                    or len(pinned_known_hosts) > 1024 * 1024
                ):
                    raise KnowledgeGitSourceError(
                        "KB_GIT_SSH_KNOWN_HOSTS is invalid or exceeds 1 MiB"
                    )
                known_hosts = self._directory / "known_hosts"
                _write_private_file(
                    known_hosts,
                    pinned_known_hosts,
                    stat.S_IRUSR | stat.S_IWUSR,
                )

            if auth_method == "ssh":
                key_path = self._directory / "identity"
                _write_private_file(
                    key_path,
                    ssh_key if ssh_key.endswith("\n") else f"{ssh_key}\n",
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                secrets.append(ssh_key)
            elif password:
                askpass_path = self._directory / "askpass.sh"
                _write_private_file(
                    askpass_path,
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  *sername*) printf '%s\\n' \"$SRW_GIT_USERNAME\" ;;\n"
                    "  *) printf '%s\\n' \"$SRW_GIT_PASSWORD\" ;;\n"
                    "esac\n",
                    stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                )
                env["GIT_ASKPASS"] = str(askpass_path)
                env["SRW_GIT_USERNAME"] = str(
                    self._credentials.get("username") or "oauth2"
                )
                env["SRW_GIT_PASSWORD"] = password
                secrets.append(password)

            if self._ssh_transport:
                if auth_method != "ssh" or not ssh_key or known_hosts is None:
                    raise KnowledgeGitSourceError(
                        "SSH repository access requires an explicit deploy key"
                    )
                env["GIT_SSH_COMMAND"] = " ".join(
                    (
                        "ssh",
                        "-F",
                        "/dev/null",
                        "-i",
                        shlex.quote(str(key_path)),
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "IdentityAgent=none",
                        "-o",
                        f"ConnectTimeout={self._connect_timeout}",
                        "-o",
                        "PasswordAuthentication=no",
                        "-o",
                        "KbdInteractiveAuthentication=no",
                        "-o",
                        "PreferredAuthentications=publickey",
                        "-o",
                        "ProxyCommand=none",
                        "-o",
                        "ProxyJump=none",
                        "-o",
                        (
                            "StrictHostKeyChecking=yes"
                            if pinned_known_hosts.strip()
                            else "StrictHostKeyChecking=accept-new"
                        ),
                        "-o",
                        "GlobalKnownHostsFile=/dev/null",
                        "-o",
                        f"UserKnownHostsFile={shlex.quote(str(known_hosts))}",
                    )
                )

            self.env = env
            self.secrets = tuple(secrets)
            return self
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, self._directory, True)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._directory is not None:
            await asyncio.to_thread(shutil.rmtree, self._directory, True)


def _validate_git_remote_host(host: str) -> None:
    """Reject option-shaped or syntactically ambiguous Git remote hosts."""
    if not host or host.startswith("-") or len(host) > 253:
        raise ValueError("A valid git remote host is required")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if not labels or any(not _SAFE_DNS_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("A valid git remote host is required")


def _is_ip_literal(host: str) -> bool:
    """Recognize canonical and legacy numeric IP spellings without DNS."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def _trusted_git_endpoints() -> set[tuple[str, int | None]]:
    """Return exact admin-trusted host/port entries, rejecting wildcards."""
    endpoints = {(host, None) for host in _DEFAULT_TRUSTED_GIT_HOSTS}
    configured = os.getenv("KB_GIT_ALLOWED_HOSTS", "")
    for raw_entry in configured.split(","):
        entry = raw_entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if any(char in entry for char in ("*", "/", "@", "?", "#", "[", "]")):
            raise ValueError("KB_GIT_ALLOWED_HOSTS requires exact hostnames")
        host, separator, raw_port = entry.rpartition(":")
        if separator:
            if not host or not raw_port.isdigit():
                raise ValueError("KB_GIT_ALLOWED_HOSTS contains an invalid endpoint")
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError("KB_GIT_ALLOWED_HOSTS contains an invalid port")
        else:
            host, port = entry, None
        _validate_git_remote_host(host)
        if _is_ip_literal(host):
            raise ValueError("KB_GIT_ALLOWED_HOSTS does not accept IP literals")
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("KB_GIT_ALLOWED_HOSTS does not accept localhost")
        endpoints.add((host, port))
    return endpoints


def _git_remote_endpoint(remote_url: str) -> tuple[str, str, int] | None:
    """Return ``(transport, canonical_host, effective_port)`` for a remote."""
    scp_match = _SAFE_SCP_REMOTE_RE.fullmatch(remote_url)
    if scp_match:
        return "ssh", scp_match.group("host").strip("[]").lower(), 22
    parsed = urlsplit(remote_url)
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_GIT_PORTS or not parsed.hostname:
        return None
    return (
        scheme,
        parsed.hostname.lower().rstrip("."),
        parsed.port or _DEFAULT_GIT_PORTS[scheme],
    )


def validate_git_remote_trust(remote_url: str, *, allow_local: bool = False) -> None:
    """Require an exact trusted Git endpoint before any network operation."""
    validated_url = validate_git_remote_url(remote_url, allow_local=allow_local)
    endpoint = _git_remote_endpoint(validated_url)
    if endpoint is None:
        if allow_local:
            return
        raise ValueError("Local git remotes are not trusted connectors")
    transport, host, port = endpoint
    if _is_ip_literal(host):
        raise ValueError("Git connector IP literals are not allowed")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Git connector localhost targets are not allowed")

    allowed = _trusted_git_endpoints()
    default_port = _DEFAULT_GIT_PORTS[transport]
    if (host, port) not in allowed and not (
        port == default_port and (host, None) in allowed
    ):
        raise ValueError("Git connector host/port is not trusted")


def validate_git_remote_url(remote_url: str, *, allow_local: bool = False) -> str:
    """Allow only non-executable repository transports at public boundaries.

    Git supports arbitrary remote helpers (for example ``ext::``). Those are
    inappropriate for a user-authored connector URL because the orchestrator
    executes Git with that value. Public KBs therefore accept only HTTP(S),
    SSH, and strict ``user@host:path`` SCP syntax. Local/file remotes are an
    explicit test/internal opt-in.
    """
    value = str(remote_url or "").strip()
    if (
        not value
        or value.startswith("-")
        or any(char in value for char in ("\x00", "\n", "\r"))
    ):
        raise ValueError("A valid git remote URL is required")

    scp_match = _SAFE_SCP_REMOTE_RE.fullmatch(value)
    if scp_match:
        _validate_git_remote_host(scp_match.group("host").strip("[]"))
        if scp_match.group("path").startswith("-"):
            raise ValueError("A valid SSH git remote is required")
        return value
    if "@" in value and ":" in value and not urlsplit(value).scheme:
        # SCP-looking strings that miss the grammar include embedded-password
        # forms such as ``user:password@host:path``.
        raise ValueError("Git remote credentials must not be embedded in the URL")

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https", "ssh"}:
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise ValueError("A valid git remote repository path is required")
        _validate_git_remote_host(parsed.hostname)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("A valid git remote port is required") from exc
        if port == 0:
            raise ValueError("A valid git remote port is required")
        if parsed.query or parsed.fragment:
            raise ValueError("Git remote URL must not contain a query or fragment")
        if scheme in {"http", "https"} and parsed.username is not None:
            raise ValueError("Git remote credentials must not be embedded in the URL")
        if scheme == "ssh":
            if parsed.password is not None:
                raise ValueError(
                    "Git remote credentials must not be embedded in the URL"
                )
            if parsed.username and not _SAFE_SSH_USERNAME_RE.fullmatch(parsed.username):
                raise ValueError("A valid SSH username is required")
        return value

    if allow_local and (scheme == "file" or not scheme):
        return value
    raise ValueError("Git remote must use https://, http://, ssh://, or user@host:path")


def _git_transport(remote_url: str) -> str:
    """Classify an already validated remote without interpreting credentials."""
    if _SAFE_SCP_REMOTE_RE.fullmatch(remote_url):
        return "ssh"
    scheme = urlsplit(remote_url).scheme.lower()
    if scheme in {"http", "https", "ssh"}:
        return scheme
    return "local"


def validate_git_auth_configuration(
    remote_url: str,
    credentials: Mapping[str, object] | None,
    *,
    allow_local: bool = False,
) -> str:
    """Validate the repository transport/auth boundary and return auth method.

    Tokens and passwords are askpass credentials and may travel only over
    HTTPS. SSH/SCP is deliberately deploy-key-only: ambient agents, default
    identities, password auth, and public unauthenticated SSH are not part of
    the datasource trust boundary. HTTP remains available for genuinely public
    repositories, never for a request carrying secret material.
    """
    validated_url = validate_git_remote_url(remote_url, allow_local=allow_local)
    validate_git_remote_trust(validated_url, allow_local=allow_local)
    transport = _git_transport(validated_url)
    raw = dict(credentials or {})
    declared_method = str(raw.get("auth_method") or "").strip().lower()
    if declared_method and declared_method not in _AUTH_METHODS:
        raise ValueError("Unsupported git auth_method")

    ssh_key = str(raw.get("ssh_key") or "")
    token = str(raw.get("token") or "")
    password = str(raw.get("password") or "")
    has_password_secret = bool(token or password)
    if token and password:
        raise ValueError("Configure only one token or password credential")
    if ssh_key and has_password_secret:
        raise ValueError("SSH keys and token/password credentials cannot be combined")

    auth_method = declared_method
    if not auth_method:
        if ssh_key:
            auth_method = "ssh"
        elif has_password_secret:
            auth_method = "token"
        else:
            auth_method = "public"

    if auth_method == "public":
        if ssh_key or has_password_secret:
            raise ValueError("Public git auth must not include hidden credentials")
        if transport == "ssh":
            raise ValueError("SSH repository access requires an explicit deploy key")
        return auth_method

    if auth_method == "ssh":
        if not ssh_key:
            raise ValueError("SSH authentication requires an explicit deploy key")
        if has_password_secret:
            raise ValueError("SSH authentication cannot include a token or password")
        if transport not in {"https", "ssh"}:
            raise ValueError("SSH deploy-key authentication requires HTTPS or SSH URL")
        return auth_method

    if not has_password_secret:
        raise ValueError("Token/password authentication requires a credential")
    if ssh_key:
        raise ValueError("Token/password authentication cannot include an SSH key")
    if transport != "https":
        raise ValueError("Token/password authentication requires an HTTPS URL")
    return auth_method


def _ssh_remote_url(remote_url: str) -> str:
    """Convert an HTTP provider URL for SSH-key authentication, if needed."""

    parsed = urlsplit(remote_url)
    if parsed.scheme not in {"http", "https"}:
        return remote_url
    if not parsed.hostname:
        raise ValueError("A valid git remote host is required")
    path = parsed.path.strip("/")
    if not path:
        raise ValueError("A valid git remote repository path is required")
    if not path.endswith(".git"):
        path = f"{path}.git"
    return f"git@{parsed.hostname}:{path}"


def _normalize_root_path(root_path: str) -> str:
    root = root_path.replace("\\", "/")
    if root.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", root):
        raise ValueError("KB root path must be repository-relative")
    root = root.strip("/")
    parts = [part for part in root.split("/") if part and part != "."]
    if any(part == ".." for part in parts) or "\x00" in root_path:
        raise ValueError("KB root path must stay within the repository")
    return "/".join(parts)


def _safe_markdown_path(path: str, root_path: str) -> bool:
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        return False
    pure_path = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in pure_path.parts):
        return False
    if pure_path.suffix != ".md":
        return False
    if not root_path:
        return True
    return path.startswith(f"{root_path}/")


class _GiteaSnapshot:
    """Snapshot over the native ``GiteaClient``.

    Reads default to one REST ``contents/`` call per file. ``prefetch()``
    switches bulk reads to a single ``archive/<ref>.tar.gz`` download served
    from a local temp file — callers with a large planned batch (the
    reindexer) use it to avoid the per-file N+1 that was the measured
    dominant Gitea load (~11k sequential ``contents/`` calls/day). Single
    or few-file readers skip it and keep the cheap per-file path.
    """

    def __init__(self, client: object, repo_name: str, ref: str) -> None:
        self._client = client
        self._repo_name = repo_name
        self._ref = ref
        self._archive: tarfile.TarFile | None = None
        self._archive_members: dict[str, tarfile.TarInfo] | None = None
        self._archive_path: str | None = None

    async def list_tree(self) -> list[dict[str, str]]:
        tree = await self._client.list_tree(self._repo_name, self._ref)
        if tree is None:
            raise KnowledgeGitSourceError("Gitea tree read failed")
        return tree

    async def prefetch(self) -> bool:
        """Bulk-load this snapshot's tree as one archive download.

        Never raises and is idempotent; on failure subsequent ``get_file``
        calls simply stay on the per-file REST path.
        """
        if self._archive_members is not None:
            return True
        fd, path = tempfile.mkstemp(prefix="kb-archive-", suffix=".tar.gz")
        os.close(fd)
        ok = False
        try:
            ok = await self._client.download_repo_archive(
                self._repo_name, self._ref, path
            )
            if ok:
                members = await asyncio.to_thread(self._index_archive, path)
                ok = members is not None
                if ok:
                    self._archive_path = path
                    self._archive_members = members
        except Exception:
            logger.warning(
                "kb archive prefetch failed for %s@%s — falling back to per-file reads",
                self._repo_name,
                self._ref,
                exc_info=True,
            )
            ok = False
        finally:
            if not ok:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        return ok

    def _index_archive(self, path: str) -> dict[str, tarfile.TarInfo] | None:
        """Open the tarball and map repo-relative paths to members."""
        try:
            archive = tarfile.open(path, mode="r:gz")
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                # Entries carry one top-level directory (the repo name);
                # strip it to get repo-relative paths.
                _, _, rel = member.name.partition("/")
                if rel:
                    members[rel] = member
            self._archive = archive
            return members
        except Exception:
            logger.warning(
                "kb archive unreadable for %s@%s — falling back to per-file reads",
                self._repo_name,
                self._ref,
                exc_info=True,
            )
            return None

    async def get_file(self, path: str) -> str | None:
        if self._archive_members is not None:
            return await asyncio.to_thread(self._read_from_archive, path)
        reader = getattr(self._client, "get_file_content", None)
        if callable(reader):
            return await reader(self._repo_name, path, ref=self._ref)
        # GitHub's approved KB API surface uses tree + tarball and deliberately
        # does not add one Contents-API GET per changed note. Download lazily
        # for a small incremental run; large runs already call prefetch().
        if await self.prefetch():
            return await asyncio.to_thread(self._read_from_archive, path)
        return None

    def _read_from_archive(self, path: str) -> str | None:
        member = (self._archive_members or {}).get(path)
        if member is None or self._archive is None:
            return None
        try:
            fh = self._archive.extractfile(member)
            if fh is None:
                return None
            with fh:
                # Same contract as the REST path (get_file_content): a file
                # that isn't valid UTF-8 reads as None, never an exception.
                return fh.read().decode("utf-8")
        except Exception as e:
            logger.warning(
                "kb archive read failed for %s in %s@%s: %s",
                path,
                self._repo_name,
                self._ref,
                e,
            )
            return None

    def close(self) -> None:
        """Release the archive file handle and delete the temp file."""
        if self._archive is not None:
            with contextlib.suppress(Exception):
                self._archive.close()
            self._archive = None
        self._archive_members = None
        if self._archive_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._archive_path)
            self._archive_path = None


class GiteaKnowledgeGitSource:
    """Thin source adapter over the native-project ``GiteaClient``."""

    def __init__(
        self,
        gitea_client: object,
        repo_name: str,
        *,
        branch: str = "main",
        label: str | None = None,
    ) -> None:
        self._client = gitea_client
        self._repo_name = repo_name
        self._branch = branch
        self._label = label or repo_name

    @property
    def label(self) -> str:
        return self._label

    async def get_head(self) -> str | None:
        return await self._client.get_branch_head_sha(self._repo_name, self._branch)

    @contextlib.asynccontextmanager
    async def snapshot(self, ref: str) -> AsyncIterator[KnowledgeGitSnapshot]:
        snap = _GiteaSnapshot(self._client, self._repo_name, ref)
        try:
            yield snap
        finally:
            snap.close()


class _RemoteSnapshot:
    def __init__(
        self,
        repository: Path,
        root_path: str,
        env: Mapping[str, str],
        secrets: Sequence[str],
        timeouts: GitCommandTimeouts,
        limits: GitOutputLimits,
    ) -> None:
        self._repository = repository
        self._root_path = root_path
        self._env = env
        self._secrets = secrets
        self._timeouts = timeouts
        self._limits = limits
        self._tree: list[dict[str, str]] | None = None
        self._blob_paths: set[str] = set()

    async def list_tree(self) -> list[dict[str, str]]:
        if self._tree is not None:
            return list(self._tree)
        result = await _run_git(
            "-C",
            str(self._repository),
            "ls-tree",
            "-rz",
            "--full-tree",
            "FETCH_HEAD",
            env=self._env,
            timeout=self._timeouts.tree,
            output_limit=self._limits.tree,
            operation="tree read",
            secrets=self._secrets,
        )
        tree: list[dict[str, str]] = []
        for raw_entry in result.stdout.split(b"\x00"):
            if not raw_entry:
                continue
            metadata, separator, raw_path = raw_entry.partition(b"\t")
            if not separator:
                continue
            fields = metadata.split()
            if len(fields) != 3:
                continue
            mode, object_type, raw_sha = fields
            # 120000 is a symlink and 160000 is a submodule.  Only ordinary
            # blobs can enter the OKF parser.
            if object_type != b"blob" or mode == b"120000":
                continue
            try:
                path = raw_path.decode("utf-8", errors="strict")
                sha = raw_sha.decode("ascii", errors="strict")
            except UnicodeDecodeError:
                continue
            if not _SHA_RE.fullmatch(sha) or not _safe_markdown_path(
                path, self._root_path
            ):
                continue
            tree.append({"path": path, "type": "blob", "sha": sha.lower()})
        self._tree = tree
        self._blob_paths = {entry["path"] for entry in tree}
        return list(tree)

    async def get_file(self, path: str) -> str | None:
        if self._tree is None:
            await self.list_tree()
        if path not in self._blob_paths:
            return None
        result = await _run_git(
            "-C",
            str(self._repository),
            "show",
            "--no-ext-diff",
            "--no-textconv",
            f"FETCH_HEAD:{path}",
            env=self._env,
            timeout=self._timeouts.blob,
            output_limit=self._limits.blob,
            operation="blob read",
            secrets=self._secrets,
        )
        try:
            return result.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise KnowledgeGitSourceError("Knowledge note is not valid UTF-8") from exc


class RemoteKnowledgeGitSource:
    """Read an external git repository through disposable bare snapshots."""

    def __init__(
        self,
        connection_url: str,
        *,
        branch: str | None = None,
        credentials: Mapping[str, object] | None = None,
        root_path: str = "",
        label: str | None = None,
        temp_parent: str | os.PathLike[str] | None = None,
        timeouts: GitCommandTimeouts | None = None,
        output_limits: GitOutputLimits | None = None,
        allow_local: bool = False,
    ) -> None:
        connection_url = validate_git_remote_url(
            connection_url, allow_local=allow_local
        )
        self._credentials = dict(credentials or {})
        auth_method = validate_git_auth_configuration(
            connection_url,
            self._credentials,
            allow_local=allow_local,
        )
        self._credentials["auth_method"] = auth_method
        if auth_method == "ssh":
            connection_url = _ssh_remote_url(connection_url)
            validate_git_remote_trust(connection_url, allow_local=allow_local)
        self._connection_url = connection_url
        self._ssh_transport = _git_transport(connection_url) == "ssh"
        self._branch = branch or None
        self._root_path = _normalize_root_path(root_path)
        self._label = label or self._default_label(connection_url)
        self._temp_parent = Path(temp_parent) if temp_parent is not None else None
        self._timeouts = timeouts or GitCommandTimeouts()
        self._limits = output_limits or GitOutputLimits()

    @staticmethod
    def _default_label(connection_url: str) -> str:
        parsed = urlsplit(connection_url)
        if parsed.scheme:
            return f"{parsed.hostname or 'git'}{parsed.path}".rstrip("/")
        # SCP-like SSH and local paths have no query parsing ambiguity; retain
        # only a short, non-secret repository basename for diagnostics.
        return PurePosixPath(connection_url.rstrip("/")).name or "git-remote"

    @property
    def label(self) -> str:
        return self._label

    def _auth(
        self,
        *,
        parent: Path | None = None,
        connect_timeout: float,
    ) -> _GitAuthContext:
        return _GitAuthContext(
            self._credentials,
            parent=parent if parent is not None else self._temp_parent,
            ssh_transport=self._ssh_transport,
            connect_timeout=connect_timeout,
        )

    async def get_head(self) -> str | None:
        async with self._auth(connect_timeout=self._timeouts.head) as auth:
            args = ["ls-remote"]
            if self._branch:
                args.append("--exit-code")
            else:
                args.append("--symref")
            args.append(self._connection_url)
            target = f"refs/heads/{self._branch}" if self._branch else "HEAD"
            args.append(target)
            result = await _run_git(
                *args,
                env=auth.env,
                timeout=self._timeouts.head,
                output_limit=self._limits.head,
                operation="HEAD read",
                secrets=auth.secrets,
                check=False,
            )
            if result.returncode == 2 and self._branch:
                return None
            if result.returncode != 0:
                detail = redact_git_error(
                    result.stderr.decode("utf-8", errors="replace").strip(),
                    auth.secrets,
                )
                detail = (
                    detail[-2000:] if detail else f"exit status {result.returncode}"
                )
                raise GitCommandError(f"Git HEAD read failed: {detail}")
            for line in result.stdout.decode("ascii", errors="ignore").splitlines():
                sha, separator, ref_name = line.partition("\t")
                if separator and ref_name == target and _SHA_RE.fullmatch(sha):
                    return sha.lower()
            return None

    async def _initialize_bare_repository(
        self,
        repository: Path,
        auth: _GitAuthContext,
    ) -> None:
        repository.mkdir(parents=True, exist_ok=False)
        await _run_git(
            "init",
            "--bare",
            "--quiet",
            str(repository),
            env=auth.env,
            timeout=self._timeouts.tree,
            output_limit=self._limits.fetch,
            operation="snapshot initialization",
            secrets=auth.secrets,
        )
        await _run_git(
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            self._connection_url,
            env=auth.env,
            timeout=self._timeouts.tree,
            output_limit=self._limits.fetch,
            operation="snapshot initialization",
            secrets=auth.secrets,
        )

    async def _fetch_snapshot(
        self,
        repository: Path,
        ref: str,
        auth: _GitAuthContext,
        *,
        partial: bool,
    ) -> None:
        args = [
            "-C",
            str(repository),
            "-c",
            "protocol.version=2",
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=1",
        ]
        if partial:
            args.append("--filter=blob:none")
        args.extend(("origin", ref))
        await _run_git(
            *args,
            env=auth.env,
            timeout=self._timeouts.fetch,
            output_limit=self._limits.fetch,
            operation="snapshot fetch",
            secrets=auth.secrets,
        )

    @contextlib.asynccontextmanager
    async def snapshot(self, ref: str) -> AsyncIterator[KnowledgeGitSnapshot]:
        if not _SHA_RE.fullmatch(ref):
            raise ValueError("Knowledge git snapshot ref must be a commit SHA")

        parent = str(self._temp_parent) if self._temp_parent else None
        snapshot_root = Path(tempfile.mkdtemp(prefix="srw-kb-snapshot-", dir=parent))
        repository = snapshot_root / "repository.git"
        try:
            async with self._auth(
                parent=snapshot_root,
                connect_timeout=self._timeouts.fetch,
            ) as auth:
                await self._initialize_bare_repository(repository, auth)
                try:
                    await self._fetch_snapshot(repository, ref, auth, partial=True)
                except GitCommandError as partial_error:
                    # Servers predating protocol-v2 filtering may reject the
                    # first fetch.  Reinitialize so an incomplete promisor pack
                    # cannot contaminate the unfiltered fallback.
                    error_text = str(partial_error).lower()
                    if not any(
                        marker in error_text for marker in _FILTER_UNSUPPORTED_MARKERS
                    ):
                        raise
                    await asyncio.to_thread(shutil.rmtree, repository, True)
                    await self._initialize_bare_repository(repository, auth)
                    await self._fetch_snapshot(repository, ref, auth, partial=False)
                yield _RemoteSnapshot(
                    repository,
                    self._root_path,
                    auth.env,
                    auth.secrets,
                    self._timeouts,
                    self._limits,
                )
        finally:
            await asyncio.to_thread(shutil.rmtree, snapshot_root, True)


__all__ = [
    "GitCommandError",
    "GitCommandTimeouts",
    "GitOutputLimits",
    "GiteaKnowledgeGitSource",
    "KnowledgeGitSnapshot",
    "KnowledgeGitSource",
    "KnowledgeGitSourceError",
    "RemoteKnowledgeGitSource",
    "redact_git_error",
    "validate_git_auth_configuration",
    "validate_git_remote_url",
    "validate_git_remote_trust",
]
