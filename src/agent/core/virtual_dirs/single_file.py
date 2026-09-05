"""SingleFileProvider — one virtual file rendered from a callable."""

from typing import Callable, Dict, Optional

from agent.core.backends.overlay import EntryMeta


class SingleFileProvider:
    """Serves exactly one virtual file at ``prefix``."""

    is_dir = False
    writable = False

    def __init__(self, prefix: str, render: Callable[[], str]):
        self.prefix = prefix
        self._render = render

    def entries(self) -> Dict[str, EntryMeta]:
        body = self._render() or ""
        return {self.prefix: EntryMeta(size=len(body.encode("utf-8")))}

    def read(self, name: str) -> Optional[str]:
        if name != self.prefix:
            return None
        return self._render()

    def read_all(self) -> Dict[str, str]:
        """One render pass instead of ``entries()`` + ``read()`` (see ``_read_all``)."""
        return {self.prefix: self._render() or ""}
