"""Virtual directory providers. See docs/features/virtual_directories.md."""

import logging
from typing import Any, Callable, Dict, Optional

from ..backends.overlay import unwrap_backend
from .contacts_provider import ContactsProvider
from .single_file import SingleFileProvider
from .tools_provider import ToolsProvider

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
    instructions with the template (src/agent.py, 2026-07 comment).
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


def materialize_single_file_providers(workspace_manager, providers) -> Dict[str, str]:
    """Write file-prefix providers as real files. Kill-switch fallback only.

    ``VIRTUAL_DIRS_ENABLED=false`` installs no overlay and makes
    ``register_virtual_provider`` a no-op, while the old materialization path is
    deleted. Without this, ``instructions.md`` and ``task_brief.md`` exist
    nowhere, and ``src/graph.py`` — which composes the job's FIRST
    ``HumanMessage`` from both — starts the agent having never been told what
    its job is. An emergency lever whose failure mode is "the agent forgets the
    task" is not a rollback, so the disabled path materializes exactly the
    single-file providers.

    Directory providers (``tools/``, ``contacts/``) are deliberately NOT
    materialized: their renderers are the documented degradation (deferred
    tools fall back to short descriptions, contacts are simply absent), and
    resurrecting the write path is what this feature removed.

    Never raises — a failed write is logged and skipped, exactly like the
    materialization it replaces.

    Returns:
        ``{path: content}`` for what was written, so the caller can register
        them as agent seed files (re-asserted on SSH reconnect).
    """
    written: Dict[str, str] = {}
    for provider in providers:
        if getattr(provider, "is_dir", True):
            continue
        path = provider.prefix
        try:
            content = provider.read(path)
        except Exception as e:
            logger.warning("Kill-switch materialization: %s render failed: %s", path, e)
            continue
        if content is None:
            continue
        try:
            workspace_manager.write_file(path, content)
        except Exception as e:
            logger.warning("Kill-switch materialization: %s write failed: %s", path, e)
            continue
        written[path] = content
    if written:
        logger.info(
            "VIRTUAL_DIRS_ENABLED is off — materialized %s as real files",
            ", ".join(sorted(written)),
        )
    return written


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
    "materialize_single_file_providers",
    "sweep_legacy_tools_dir",
    "unwrap_backend",
]
