"""S3 — capability-gated tool binding for the lite workspace tiers.

``docs/features/no_workspace_agent_mode.md`` §3.2/§7: tools that need a
workspace-backed execution environment (shell / browser / git) — and, for the
``none`` tier, file tools — are dropped by *backend capability*, not by hand-
trimmed config tool lists. This is the enforcement that replaces the originally
sketched "lite presets": any config dispatched onto a lite backend gets the
right toolset by construction.

The filter (``filter_tools_by_backend``) is what the worker (``agent.py``) and
session (``persistent_session.py``) bind seams both call after
``get_all_tool_names``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.tools.registry import (  # noqa: E402
    filter_tools_by_backend,
    get_tools_by_category,
)


def _stub(*, shell: bool, files: bool) -> SimpleNamespace:
    return SimpleNamespace(supports_shell=shell, supports_file_tools=files)


def _one(category: str) -> str:
    """A representative tool name from a registry category."""
    names = get_tools_by_category(category)
    assert names, f"registry has no tools in category {category!r}"
    return names[0]


# ---------------------------------------------------------------------------
# filter_tools_by_backend
# ---------------------------------------------------------------------------
class TestFilterToolsByBackend:
    def test_full_backend_drops_nothing(self):
        # sandbox / vm — supports_shell + file tools → unchanged.
        backend = _stub(shell=True, files=True)
        names = [
            _one("shell"),
            _one("git"),
            _one("browser_direct"),
            _one("workspace"),
            _one("research"),
            _one("knowledge"),
        ]
        assert filter_tools_by_backend(names, backend) == names

    def test_virtual_drops_execution_keeps_files_and_web(self):
        # virtual — no shell → shell/browser/git gone; file + web + datasource stay.
        backend = _stub(shell=False, files=True)
        shell, git, browser = _one("shell"), _one("git"), _one("browser_direct")
        wfile, web, sql = _one("workspace"), _one("research"), _one("sql")
        out = filter_tools_by_backend([shell, git, browser, wfile, web, sql], backend)
        assert shell not in out and git not in out and browser not in out
        assert wfile in out and web in out and sql in out

    def test_none_drops_execution_and_files(self):
        # none — no shell + no file tools → only web/datasource/kb/core remain.
        backend = _stub(shell=False, files=False)
        shell, wfile = _one("shell"), _one("workspace")
        web, kb, core = _one("research"), _one("knowledge"), _one("core")
        out = filter_tools_by_backend([shell, wfile, web, kb, core], backend)
        assert shell not in out and wfile not in out
        assert web in out and kb in out and core in out

    def test_none_backend_is_noop(self):
        # No backend in hand (non-agent caller) → full set passes through.
        names = [_one("shell"), _one("workspace")]
        assert filter_tools_by_backend(names, None) == names

    def test_unknown_tool_passes_through(self):
        # Not in the registry → no category → kept (load_tools handles unknowns).
        backend = _stub(shell=False, files=True)
        assert filter_tools_by_backend(["totally_made_up_tool"], backend) == [
            "totally_made_up_tool"
        ]

    def test_order_preserved(self):
        backend = _stub(shell=False, files=True)
        web1, web2 = get_tools_by_category("research")[:2]
        wfile = _one("workspace")
        assert filter_tools_by_backend([web1, wfile, web2], backend) == [
            web1,
            wfile,
            web2,
        ]


# ---------------------------------------------------------------------------
# the real backends declare the capabilities the filter reads
# ---------------------------------------------------------------------------
class TestBackendCapabilityFlags:
    def test_scratch_backend_is_shell_and_file_less(self):
        from src.core.backends.scratch import ScratchBackend

        b = ScratchBackend(job_id="t")
        try:
            assert b.supports_shell is False
            assert b.supports_file_tools is False  # `none` → no file tools
        finally:
            b.disconnect()

    def test_virtual_backend_has_files_but_no_shell(self):
        from src.core.backends.factory import create_lite_backend

        mount = {
            "name": "workspace",
            "rclone_spec": {"type": "memory", "config": {}, "root": ""},
            "prefix": "jobs/t/",
            "access": "read_write",
        }
        cfg = SimpleNamespace(backend="virtual", mounts=[mount])
        b = create_lite_backend(cfg, job_id="t")
        b.connect()
        try:
            assert b.supports_shell is False
            assert b.supports_file_tools is True  # virtual → file tools exposed
        finally:
            b.disconnect()
