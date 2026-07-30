"""VirtualOverlayBackend — agent-visible directories served from live state.

Wraps a real ``WorkspaceBackend`` and answers file operations for registered
virtual prefixes (``tools/``, ``contacts/``, ``instructions.md``) from
providers instead of the workspace filesystem. Everything else delegates.

Duck-typed proxy (not a ``WorkspaceBackend`` subclass), mirroring
``SubdirBackend``: anything not overridden passes through via ``__getattr__``.
No isinstance checks target backend types in the codebase, so this is safe.

See docs/features/virtual_directories.md.
"""

import logging
import posixpath
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


class VirtualPathError(ValueError):
    """An operation is not permitted on a virtual path.

    Subclasses ``ValueError`` so the file tools' existing path-error handling
    surfaces it to the agent as a readable message.
    """


@dataclass(frozen=True)
class EntryMeta:
    """Metadata for one virtual entry. ``mtime`` is unused in Slice 1."""

    size: int
    mtime: Optional[float] = None


class VirtualDirProvider(Protocol):
    """Serves one virtual prefix. Flat — no subdirectories."""

    prefix: str
    is_dir: bool
    writable: bool

    def entries(self) -> Dict[str, EntryMeta]: ...

    def read(self, name: str) -> Optional[str]: ...

    def write(self, name: str, content: str) -> None: ...


class VirtualOverlayBackend:
    """Routes registered prefixes to providers; delegates everything else."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._providers: Dict[str, Any] = {}

    # --- registry ---------------------------------------------------------

    @property
    def inner(self) -> Any:
        """The unwrapped backend. Use for probes that must bypass virtual paths."""
        return self._inner

    @property
    def providers(self) -> Dict[str, Any]:
        return dict(self._providers)

    def register(self, provider: Any) -> None:
        self._providers[provider.prefix] = provider
        logger.debug("Registered virtual provider: %s", provider.prefix)

    # --- routing ----------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        p = (path or "").strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.strip("/")
        if not p:
            return ""
        return posixpath.normpath(p)

    def _match(self, path: str) -> Optional[Tuple[Any, str]]:
        """Return ``(provider, entry_name)`` when *path* is virtual.

        ``entry_name`` is ``""`` for the directory itself. For a single-file
        provider the entry name is the prefix.
        """
        p = self._normalize(path)
        if not p:
            return None
        head = p.split("/", 1)[0]
        provider = self._providers.get(head)
        if provider is None:
            return None
        if not provider.is_dir:
            return (provider, p) if p == provider.prefix else (provider, "")
        return provider, p[len(head):].lstrip("/")

    # --- provider calls, never allowed to crash the agent loop ------------

    def _entries(self, provider: Any) -> Dict[str, EntryMeta]:
        try:
            return provider.entries()
        except Exception as e:  # degrade to empty listing, never explode
            logger.warning("virtual entries() failed for %s: %s", provider.prefix, e)
            return {}

    def _read(self, provider: Any, name: str, path: str) -> Optional[str]:
        try:
            return provider.read(name)
        except Exception as e:
            logger.warning("virtual read failed for %s: %s", path, e)
            raise VirtualPathError(f"{path} is temporarily unavailable: {e}") from e

    # --- read path --------------------------------------------------------

    def read_file(self, path: str, binary: bool = False) -> Any:
        m = self._match(path)
        if m is None:
            return self._inner.read_file(path, binary=binary)
        provider, name = m
        content = self._read(provider, name, path) if name else None
        if content is None:
            raise FileNotFoundError(f"File not found: {path}")
        return content.encode("utf-8") if binary else content

    def exists(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.exists(path)
        provider, name = m
        if not name:
            return bool(provider.is_dir)
        return name in self._entries(provider)

    def is_file(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.is_file(path)
        provider, name = m
        return bool(name) and name in self._entries(provider)

    def is_dir(self, path: str) -> bool:
        m = self._match(path)
        if m is None:
            return self._inner.is_dir(path)
        provider, name = m
        return bool(provider.is_dir) and not name

    def stat(self, path: str) -> int:
        m = self._match(path)
        if m is None:
            return self._inner.stat(path)
        provider, name = m
        entries = self._entries(provider)
        if not name:
            return sum(meta.size for meta in entries.values())
        meta = entries.get(name)
        if meta is None:
            raise FileNotFoundError(f"File not found: {path}")
        return meta.size

    # --- delegation -------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Private names are never delegated: this also stops __getattr__ from
        # recursing on self._inner before __init__ has run.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)
