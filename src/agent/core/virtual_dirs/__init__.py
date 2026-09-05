"""Virtual directory providers. See knowledge-base/knowledge/features/virtual_directories.md."""

import logging
from typing import Any, Callable, Optional

from agent.core.backends.overlay import unwrap_backend
from agent.core.virtual_dirs.contacts_provider import ContactsProvider
from agent.core.virtual_dirs.single_file import SingleFileProvider
from agent.core.virtual_dirs.tools_provider import ToolsProvider

logger = logging.getLogger(__name__)

# First line written by DescriptionManager.generate_tool_index(). Used as the
# marker that a real tools/ directory is a leftover from materialization and
# not a directory the user owns.
_GENERATED_TOOLS_MARKER = "# Available Tools"


def build_instruction_providers(
    *,
    uploaded: Callable[[], Optional[str]],
    template: Callable[[], str],
    brief: Callable[[], str],
) -> list:
    """Providers for instructions.md and task_brief.md.

    Precedence for instructions.md is resolved here, in one place: an upload or
    inline body from the job record wins; otherwise the rendered template. The
    materialized version resolved this with exists() probes across three call
    sites, which is how a remote-backend probe once clobbered user-provided
    instructions with the template (src/agent/agent.py, 2026-07 comment).
    """

    def _instructions() -> str:
        body = uploaded()
        if body and body.strip():
            return body
        return template()

    return [
        SingleFileProvider("instructions.md", _instructions),
        SingleFileProvider("task_brief.md", brief),
    ]


def sweep_legacy_tools_dir(backend: Any) -> bool:
    """Delete a leftover materialized ``tools/`` directory. Never raises.

    Old workspace snapshots still carry the generated docs. Serving them is
    impossible (the prefix is virtual now), but the shell would still show
    stale files, so converge them. A ``tools/`` directory the user created is
    left alone: only the generated marker authorises deletion.

    Returns:
        True when a generated directory was deleted.
    """
    try:
        if not backend.is_dir("tools"):
            return False
        readme = backend.read_file("tools/README.md")
        if not str(readme).lstrip().startswith(_GENERATED_TOOLS_MARKER):
            logger.info("Leaving user-owned tools/ directory in place")
            return False
        backend.delete_directory("tools")
        logger.info("Swept leftover materialized tools/ directory")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:  # never block boot
        logger.warning("tools/ sweep failed (non-fatal): %s", e)
        return False


__all__ = [
    "ContactsProvider",
    "SingleFileProvider",
    "ToolsProvider",
    "build_instruction_providers",
    "sweep_legacy_tools_dir",
    "unwrap_backend",
]
