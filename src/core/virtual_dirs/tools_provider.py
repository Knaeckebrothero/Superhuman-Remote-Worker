"""ToolsProvider — tools/ rendered from the currently loaded tool objects.

Renders through the canonical DescriptionManager helpers, so the virtual
content is byte-identical to what the deleted materialization wrote. The tool
list is read on every call, so mid-lifecycle changes (virtual->sandbox upgrade
re-derive, session tool-group overrides) are reflected with no regeneration.
"""

from typing import Callable, Dict, List, Optional

from ...tools.description_manager import DescriptionManager
from ..backends.overlay import EntryMeta


class ToolsProvider:
    prefix = "tools"
    is_dir = True
    writable = False

    def __init__(self, get_tools: Callable[[], List]):
        self._get_tools = get_tools
        self._manager = DescriptionManager()

    def _render_all(self) -> Dict[str, str]:
        tools = list(self._get_tools() or [])
        self._manager.extract_docstrings(tools)
        names = [t.name for t in tools]
        docs = {"README.md": self._manager.generate_tool_index(names)}
        for name in names:
            docs[f"{name}.md"] = self._manager.generate_tool_description(name)
        return docs

    def entries(self) -> Dict[str, EntryMeta]:
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self._render_all().items()
        }

    def read(self, name: str) -> Optional[str]:
        return self._render_all().get(name)

    def read_all(self) -> Dict[str, str]:
        """One render pass for the whole set — see ``VirtualOverlayBackend._read_all``.

        Without it, a root search costs ``entries()`` plus one full re-render
        per tool doc: ~1,700 ``generate_tool_description`` calls at 40 tools.
        """
        return self._render_all()
