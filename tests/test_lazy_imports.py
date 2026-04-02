"""Tests for lazy-loading __init__.py modules.

Ensures that importing lightweight submodules (e.g. src.core.loader) does NOT
pull in heavy dependencies (e.g. langgraph) via eager __init__.py imports.
Also verifies that every entry in _LAZY_IMPORTS is resolvable.
"""

import importlib
import sys

import pytest


# ── src/core lazy imports ────────────────────────────────────────────────────


class TestSrcCoreLazyImports:
    """Verify src.core.__init__ lazy-loading behaviour."""

    def test_all_lazy_entries_resolve(self):
        """Every name in _LAZY_IMPORTS must resolve to a real attribute."""
        from src.core import _LAZY_IMPORTS

        for name, (module_path, attr) in _LAZY_IMPORTS.items():
            mod = importlib.import_module(module_path, package="src.core")
            assert hasattr(mod, attr), (
                f"_LAZY_IMPORTS[{name!r}] points to {module_path}.{attr} "
                f"which does not exist"
            )

    def test_loader_import_does_not_trigger_langgraph(self):
        """Regression: importing src.core.loader must not pull in langgraph.

        This was the root cause of the builder agent crash — the orchestrator
        container doesn't have langgraph installed, but the old eager
        __init__.py imported state.py which depends on it.
        """
        # Snapshot which modules are already loaded
        before = set(sys.modules.keys())

        # Force a fresh import of the core package + loader
        # (they may already be cached, so we check the delta)
        importlib.import_module("src.core.loader")

        after = set(sys.modules.keys())
        newly_loaded = after - before

        langgraph_modules = [m for m in newly_loaded if m.startswith("langgraph")]
        # If langgraph was already loaded by another test, we can't assert
        # absence in sys.modules — but we CAN assert it wasn't newly pulled in.
        assert langgraph_modules == [], (
            f"Importing src.core.loader pulled in langgraph: {langgraph_modules}"
        )

    def test_state_import_does_load_langgraph(self):
        """Importing state via the lazy mechanism should still work."""
        from src.core import UniversalAgentState

        assert UniversalAgentState is not None

    def test_unknown_attr_raises(self):
        """Accessing a non-existent attribute must raise AttributeError."""
        import src.core

        with pytest.raises(AttributeError, match="no attribute"):
            _ = src.core.this_does_not_exist

    def test_cached_after_first_access(self):
        """Lazy-loaded attrs are cached in module globals after first access."""
        import src.core

        _ = src.core.WorkspaceManager  # trigger lazy load
        # Second access should come from globals, not __getattr__
        assert "WorkspaceManager" in vars(src.core)


# ── src/ root lazy imports ───────────────────────────────────────────────────


class TestSrcRootLazyImports:
    """Verify src.__init__ lazy-loading behaviour."""

    def test_all_lazy_entries_resolve(self):
        """Every name in _LAZY_IMPORTS must resolve to a real attribute."""
        from src import _LAZY_IMPORTS

        for name, (module_path, attr) in _LAZY_IMPORTS.items():
            mod = importlib.import_module(module_path, package="src")
            assert hasattr(mod, attr), (
                f"_LAZY_IMPORTS[{name!r}] points to {module_path}.{attr} "
                f"which does not exist"
            )

    def test_unknown_attr_raises(self):
        """Accessing a non-existent attribute must raise AttributeError."""
        import src

        with pytest.raises(AttributeError, match="no attribute"):
            _ = src.this_does_not_exist
