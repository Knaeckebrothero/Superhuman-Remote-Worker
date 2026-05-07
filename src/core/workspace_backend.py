"""Abstract workspace backend interface.

Defines the contract for workspace storage backends. All file paths are
relative to the workspace root. Implementations handle the transport
(local filesystem, SSH/SFTP, etc.) while managers provide higher-level logic.

See docs/features/vm_backend.md for the full design.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class WorkspaceUnavailableError(Exception):
    """Raised when the workspace backend cannot be reached.

    For RemoteBackend: SSH connection lost, VM unreachable, etc.
    The agent should report this to the orchestrator for VM recovery.
    """

    pass


class WorkspaceBackend(ABC):
    """Abstraction over workspace file storage and shell execution.

    All paths are relative to the workspace root. Implementations must
    validate that paths don't escape the workspace boundary.

    File operations (abstract): Every backend must implement these.
    Shell operations (non-abstract): Default to NotImplementedError.
    Override in backends that support shell execution (RemoteBackend).
    For local execution, ShellManager handles shell ops directly via libtmux.
    """

    # --- File operations ---

    @abstractmethod
    def read_file(self, path: str, binary: bool = False) -> str | bytes:
        """Read file contents.

        Args:
            path: Relative path within workspace.
            binary: If True, return bytes instead of str.

        Returns:
            File contents as str (default) or bytes.

        Raises:
            FileNotFoundError: If file doesn't exist.
            ValueError: If path escapes workspace boundary.
        """
        ...

    @abstractmethod
    def write_file(self, path: str, content: str | bytes) -> None:
        """Write file contents. Creates parent directories.

        Args:
            path: Relative path within workspace.
            content: Content to write (str or bytes).

        Raises:
            ValueError: If path escapes workspace boundary.
        """
        ...

    def write_home_file(self, relative_path: str, content: str | bytes) -> None:
        """Write a file under the agent user's home directory ($HOME).

        Reserved for one-time setup that legitimately lives outside the
        workspace tree — e.g. SSH keys/config under ~/.ssh — without
        relaxing the boundary check that protects normal write_file calls.
        The path is interpreted relative to $HOME, so callers cannot write
        to arbitrary absolute paths.

        Default raises NotImplementedError; backends with a home concept
        (RemoteBackend) override.

        Args:
            relative_path: Path relative to $HOME (e.g. ".ssh/repo_foo").
            content: Content to write (str or bytes).

        Raises:
            ValueError: If path is empty, absolute, or escapes $HOME.
            NotImplementedError: If the backend has no home concept.
        """
        raise NotImplementedError("write_home_file not supported by this backend")

    def resolve_home_path(self, relative_path: str) -> str:
        """Resolve a path under $HOME to its canonical absolute form.

        Companion to write_home_file: lets callers obtain the absolute
        path they need to pass to shell commands (chmod, SSH config
        IdentityFile, etc.) without hard-coding the home directory.

        Default raises NotImplementedError; backends with a home concept
        override.

        Args:
            relative_path: Path relative to $HOME.

        Returns:
            Absolute path string.

        Raises:
            ValueError: If path is empty, absolute, or escapes $HOME.
            NotImplementedError: If the backend has no home concept.
        """
        raise NotImplementedError("resolve_home_path not supported by this backend")

    @abstractmethod
    def append_file(self, path: str, content: str) -> None:
        """Append content to a file. Creates the file if it doesn't exist.

        Args:
            path: Relative path within workspace.
            content: Content to append.

        Raises:
            ValueError: If path escapes workspace boundary.
        """
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: Relative path within workspace.

        Returns:
            True if the path exists (file or directory).
        """
        ...

    @abstractmethod
    def is_file(self, path: str) -> bool:
        """Check if path is a file.

        Args:
            path: Relative path within workspace.
        """
        ...

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Check if path is a directory.

        Args:
            path: Relative path within workspace.
        """
        ...

    @abstractmethod
    def list_dir(self, path: str = "", pattern: str = "*") -> list[str]:
        """List directory contents matching a glob pattern.

        Args:
            path: Directory path relative to workspace root.
            pattern: Glob pattern to filter entries (default: "*").

        Returns:
            List of relative paths (from workspace root). Directories
            have a trailing "/".
        """
        ...

    @abstractmethod
    def search_files(
        self, query: str, path: str = "", case_sensitive: bool = False
    ) -> list[dict]:
        """Search for text in workspace files.

        Args:
            query: Text to search for.
            path: Directory to search in (default: entire workspace).
            case_sensitive: Whether search is case-sensitive.

        Returns:
            List of dicts with 'path', 'line_number', and 'line'.
        """
        ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Create directory (and parents).

        Args:
            path: Relative path within workspace.

        Raises:
            ValueError: If path escapes workspace boundary.
        """
        ...

    @abstractmethod
    def delete_file(self, path: str) -> bool:
        """Delete a file or empty directory.

        Args:
            path: Relative path within workspace.

        Returns:
            True if deleted, False if didn't exist.

        Raises:
            ValueError: If trying to delete non-empty directory.
        """
        ...

    @abstractmethod
    def delete_directory(self, path: str) -> bool:
        """Delete a directory and all its contents.

        Args:
            path: Relative path within workspace.

        Returns:
            True if deleted, False if didn't exist.

        Raises:
            ValueError: If path escapes workspace or is the workspace root.
        """
        ...

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Move a file or directory within the workspace.

        Args:
            src: Source path relative to workspace root.
            dst: Destination path relative to workspace root.

        Raises:
            FileNotFoundError: If source doesn't exist.
            ValueError: If paths escape workspace boundary.
        """
        ...

    @abstractmethod
    def copy(self, src: str, dst: str) -> None:
        """Copy a file within the workspace.

        Args:
            src: Source path relative to workspace root.
            dst: Destination path relative to workspace root.

        Raises:
            FileNotFoundError: If source doesn't exist.
            ValueError: If source is a directory or paths escape boundary.
        """
        ...

    @abstractmethod
    def stat(self, path: str) -> int:
        """Get size of a file or directory in bytes.

        Args:
            path: Relative path within workspace (empty string = root).

        Returns:
            Size in bytes. 0 if path doesn't exist.
        """
        ...

    @abstractmethod
    def resolve_path(self, relative_path: str) -> str:
        """Resolve a relative path to its canonical form and validate boundaries.

        This is the equivalent of the old WorkspaceManager.get_path() — it
        validates that the path stays within the workspace root.

        Args:
            relative_path: Path relative to workspace root. Empty string = root.

        Returns:
            The resolved absolute path as a string.

        Raises:
            ValueError: If path escapes workspace boundary.
        """
        ...

    # --- Properties ---

    @property
    def host(self) -> Optional[str]:
        """Remote host address, or None for local backends."""
        return None

    # --- Command execution ---

    def exec_command(self, command: str, timeout: int = 30) -> str:
        """Execute a command on the workspace host and return stdout.

        For remote backends, runs the command via SSH.

        Args:
            command: Shell command to execute.
            timeout: Timeout in seconds.

        Returns:
            Command stdout as string.
        """
        raise NotImplementedError("exec_command not supported by this backend")

    # --- Shell operations ---
    #
    # Non-abstract: default to NotImplementedError. Override in backends
    # that support remote shell execution (RemoteBackend). For local
    # execution, ShellManager uses libtmux directly — these methods are
    # not called.

    def shell_run(
        self,
        command: str,
        timeout: int = 120,
        tab_name: str = "default",
        working_dir: Optional[str] = None,
    ) -> str:
        """Execute a command synchronously with sentinel-based completion detection.

        Args:
            command: Shell command to execute.
            timeout: Timeout in seconds.
            tab_name: Tab to execute in.
            working_dir: Working directory (relative to workspace root).

        Returns:
            Formatted output string identical to ShellManager.run_sync().
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_send(
        self,
        tab_name: str,
        text: str,
        enter: bool = True,
    ) -> str:
        """Send keystrokes to a tab.

        Args:
            tab_name: Tab name.
            text: Text or tmux key names to send.
            enter: Whether to press Enter after sending.

        Returns:
            Confirmation message.
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_read(
        self,
        tab_name: str,
        lines: int = 50,
        since_cursor: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        """Read output from a tab's terminal buffer.

        Args:
            tab_name: Tab name.
            lines: Number of lines to read from the end.
            since_cursor: If True, read only lines added since last read.

        Returns:
            Tuple of (text, metadata_dict).
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_read_with_offset(
        self,
        tab_name: str,
        lines: int = 30,
        offset: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Read output from a tab with optional absolute offset.

        Args:
            tab_name: Tab name.
            lines: Number of lines to return.
            offset: Absolute line position to start from (None = tail).

        Returns:
            Tuple of (text, metadata_dict).
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_ensure_tab(self, name: str) -> None:
        """Get or auto-create a tab.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars).
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_open_tab(
        self,
        name: str,
        command: Optional[str] = None,
        tab_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a new named tab.

        Args:
            name: Tab name.
            command: Optional command to run on creation.
            tab_type: Tab type (shell, ssh, repl, process).

        Returns:
            Metadata dict for the new tab.
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_close_tab(self, name: str) -> str:
        """Close a tab.

        Args:
            name: Tab name.

        Returns:
            Confirmation message.
        """
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_list_tabs(self) -> List[Dict[str, Any]]:
        """Return metadata for all tabs."""
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_format_tab_header(self) -> str:
        """Return tab header string like [Shells: default | build]."""
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_cleanup(self) -> None:
        """Kill the entire shell session."""
        raise NotImplementedError("Shell operations not supported by this backend")

    def shell_is_alive(self) -> bool:
        """Check if the shell session exists."""
        raise NotImplementedError("Shell operations not supported by this backend")

    @property
    def supports_shell(self) -> bool:
        """Whether this backend supports shell operations.

        Returns True if shell_run() is implemented (not the default
        NotImplementedError). Used by ShellManager to decide whether to
        delegate or use local libtmux.
        """
        return False

    # --- Lifecycle ---

    @abstractmethod
    def connect(self) -> None:
        """Establish connection (no-op for local)."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Clean up connection."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if backend is connected and operational."""
        ...

    @property
    @abstractmethod
    def root(self) -> str:
        """Return the workspace root path as a string."""
        ...
