"""Import the external ``mcp`` SDK despite the repository's name collision.

The orchestrator has a historical top-level ``orchestrator/mcp`` application
package. Its runtime and the test harness put ``orchestrator/`` on
``sys.path``, which otherwise shadows the third-party package also named
``mcp``. Resolve the installed distribution explicitly and install it under
its canonical module name before importing SDK submodules.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from importlib import metadata
from pathlib import Path
from types import ModuleType

_IMPORT_LOCK = threading.Lock()


def ensure_mcp_sdk() -> ModuleType:
    """Return the external MCP SDK, replacing a shadowing local ``mcp``."""
    existing = sys.modules.get("mcp")
    if existing is not None and hasattr(existing, "ClientSession"):
        return existing

    with _IMPORT_LOCK:
        existing = sys.modules.get("mcp")
        if existing is not None and hasattr(existing, "ClientSession"):
            return existing

        distribution = metadata.distribution("mcp")
        package_dir = Path(distribution.locate_file("mcp")).resolve()
        init_file = package_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "mcp",
            init_file,
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError("installed mcp SDK package could not be loaded")

        stale_modules = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "mcp" or name.startswith("mcp.")
        }
        for name in stale_modules:
            sys.modules.pop(name, None)

        module = importlib.util.module_from_spec(spec)
        sys.modules["mcp"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            for name in [
                key
                for key in list(sys.modules)
                if key == "mcp" or key.startswith("mcp.")
            ]:
                sys.modules.pop(name, None)
            sys.modules.update(stale_modules)
            raise
        return module
