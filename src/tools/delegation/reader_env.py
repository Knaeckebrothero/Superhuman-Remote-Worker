"""Reader environment for light subagents (Phase 2).

`acquire_reader_env` gives one light reader an isolated, pod-correct place to
work: its own git worktree on the WORKSPACE pod (created over the shared
connection), a `SubdirBackend` view re-rooted at that worktree, a reader
`WorkspaceManager`/`GitManager` pointed at it, its own namespaced shell, and a
`ToolContext` that mirrors the parent's — same `orchestrator_client` +
`_job_metadata` (so `cite_*` stays under the PARENT job_id → shared citation DB)
— but with the reader workspace/shell and the tool set **minus the delegation
category** (no `spawn_subagent`/`delegate_work` → enforces no-nesting).

`release_reader_env` tears the reader down: closes only its own shell tabs (never
the shared session) and removes the worktree. Best-effort, logged.

Pod separation: worktree creation and git run on the workspace pod via the
parent backend; blocking paramiko/git is offloaded with `run_in_executor`, and
concurrent worktree creation is serialized behind a module lock (one repo lock).

See knowledge-base/knowledge/issues/delegation_light_mode_missing.md (Phase 2).
"""

import asyncio
import copy
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from ...core.backends.subdir import SubdirBackend
from ..context import ToolContext

logger = logging.getLogger(__name__)

# Serializes `git worktree add` across concurrent readers (one repo lock).
_WORKTREE_LOCK = asyncio.Lock()

# Write tools withheld from read-leaning readers (they would author output the
# scholar is meant to author). Included only when allow_writes is True — and even
# then they land in the reader's worktree, never the parent root.
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# Port range per reader (mirrors delegate_work's convention).
_PORT_BASE = 8000
_PORT_SIZE = 100


@dataclass
class ReaderEnv:
    """Handle for one reader's isolated environment; pass to release_reader_env."""

    index: int
    context: ToolContext
    tools: List[Any]
    port_block: str
    worktree_path: Optional[str]  # relative path on the workspace pod, or None
    branch: Optional[str]
    _subdir_backend: Any
    _parent_git: Any  # parent GitManager (for worktree_remove); None if no git


def _reader_tool_names(
    parent_tool_names: List[str], *, allow_writes: bool
) -> List[str]:
    """Parent's tool names minus delegation (no nesting) and, unless writes are
    allowed, minus the file-write tools."""
    from ..registry import TOOL_REGISTRY

    out = []
    for name in parent_tool_names:
        meta = TOOL_REGISTRY.get(name, {})
        if meta.get("category") == "delegation":
            continue  # no grandchildren
        if not allow_writes and name in _WRITE_TOOLS:
            continue
        out.append(name)
    return out


def _build_reader_port_block(
    index: int, total: Optional[int], worktree_path: Optional[str]
) -> str:
    """Port-range + worktree awareness block for a reader (accurate wording).

    ``total`` is optional: iterative fan-out (one spawn_subagent call = one
    reader) does not know how many siblings run concurrently, so when total is
    None the block simply notes that siblings may be running.
    """
    base = _PORT_BASE + (index + 1) * _PORT_SIZE
    end = base + _PORT_SIZE - 1
    header = f"You are subagent #{index}" + (f" of {total}" if total else "") + "."
    lines = ["=== SUBAGENT ENVIRONMENT ===", header]
    if worktree_path:
        lines.append(
            f"Your isolated working directory: {worktree_path} (a git worktree)."
        )
    lines += [
        f"Your assigned port range: {base}-{end}. Use these ports for any "
        "servers/services you start. Do NOT use ports outside this range — "
        "other subagents may run concurrently.",
        "================================",
    ]
    return "\n".join(lines)


async def acquire_reader_env(
    parent_context: ToolContext,
    parent_tool_names: List[str],
    *,
    index: int,
    total: Optional[int] = None,
    allow_writes: bool = False,
    name: Optional[str] = None,
) -> ReaderEnv:
    """Build one reader's isolated worktree + tools. See module docstring.

    ``name`` (U3 subagents: the child's handle) names the worktree, its branch
    and the shell-tab namespace instead of the numeric ``index`` — so a
    worktree child is recognisable on the workspace pod as
    ``.worktrees/<handle>`` / ``sub/<handle>`` / ``<handle>__<tab>``.
    """
    loop = asyncio.get_running_loop()
    parent_ws = parent_context.workspace_manager
    if parent_ws is None:
        raise ValueError("acquire_reader_env requires a parent workspace_manager")
    parent_backend = parent_ws.backend
    parent_git = getattr(parent_ws, "git_manager", None)

    label = name if name else str(index)
    worktree_rel = f".worktrees/sub_{index}" if not name else f".worktrees/{name}"
    branch = f"sub/{label}"
    worktree_ready = False

    if parent_git is not None and getattr(parent_git, "is_active", False):
        async with _WORKTREE_LOCK:
            worktree_ready = await loop.run_in_executor(
                None, parent_git.worktree_add, worktree_rel, branch
            )
        if not worktree_ready:
            logger.warning(
                "reader %d: worktree_add failed; falling back to a scratch subdir",
                index,
            )

    if not worktree_ready:
        # No git (or add failed): a plain scratch subdir still isolates the
        # reader's files via SubdirBackend — just without a git branch.
        worktree_rel = (
            f".subagents/reader_{index}" if not name else f".subagents/{name}.tree"
        )
        branch = None
        try:
            await loop.run_in_executor(None, parent_backend.mkdir, worktree_rel)
        except Exception as e:
            logger.warning("reader %d: scratch mkdir failed (non-fatal): %s", index, e)

    # Re-rooted view over the shared connection + namespaced shell tabs.
    subdir_backend = SubdirBackend(
        parent_backend,
        worktree_rel,
        shell_tab_prefix=f"sub{index}__" if not name else f"{name}__",
    )

    # Reader workspace manager rooted at the worktree (no re-init / no rm -rf).
    from ..registry import load_tools  # lazy — avoids delegation<->registry cycle
    from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig

    reader_ws = WorkspaceManager(
        job_id=parent_ws.job_id,
        config=WorkspaceManagerConfig(
            structure=[], base_path=subdir_backend.root, git_versioning=False
        ),
        backend=subdir_backend,
    )
    reader_ws._initialized = True

    if worktree_ready:
        from src.managers.git_manager import GitManager

        reader_git = await loop.run_in_executor(
            None,
            lambda: GitManager.from_worktree(
                Path(subdir_backend.root),
                backend=parent_backend,
                remote_cwd=worktree_rel,
            ),
        )
        if reader_git is not None:
            reader_ws._git_manager = reader_git

    # Reader shell over the re-rooted backend (own namespaced tabs; commands
    # default their cwd to the worktree). Only if the backend supports shell.
    reader_shell = None
    if getattr(subdir_backend, "supports_shell", False):
        try:
            from src.tools.shell.shell_manager import ShellManager

            shell_cfg = (parent_context.config or {}).get("shell", {})
            reader_shell = ShellManager(
                job_id=f"{parent_ws.job_id}-s{label}",
                max_tabs=shell_cfg.get("max_tabs", 15),
                scrollback_limit=shell_cfg.get("scrollback_limit", 5000),
                default_timeout=shell_cfg.get("default_timeout", 120),
                blocked_commands=shell_cfg.get("blocked_commands"),
                sandbox_cwd=None,  # SubdirBackend already re-roots working_dir
                backend=subdir_backend,
                sudo_action=shell_cfg.get("sudo_action", "freeze"),
            )
        except Exception as e:
            logger.warning(
                "reader %d: shell manager init failed (non-fatal): %s", index, e
            )

    # Reader ToolContext: shallow-copy the parent (keeps orchestrator_client,
    # _job_metadata → parent job_id → shared citation DB, datasources, db pools,
    # config, llm_config, citation_engine) then swap workspace + shell and reset
    # per-reader state so the reader never touches the parent's undo/freeze/reads.
    reader_context = copy.copy(parent_context)
    reader_context.workspace_manager = reader_ws
    reader_context.shell_manager = reader_shell
    reader_context._snapshot_callback = None
    reader_context._freeze_request = None
    reader_context._recent_reads = deque(maxlen=10)
    reader_context._pinned_reads = set()
    reader_context._recent_read_versions = {}

    reader_names = _reader_tool_names(parent_tool_names, allow_writes=allow_writes)
    tools = load_tools(reader_names, reader_context)

    # Readers get their own overlay (own WorkspaceManager); give it a provider
    # bound to the reader's own tool list — not the parent's.
    from src.core.virtual_dirs import ToolsProvider

    reader_ws.register_virtual_provider(ToolsProvider(lambda: tools))

    port_block = _build_reader_port_block(index, total, worktree_rel)

    return ReaderEnv(
        index=index,
        context=reader_context,
        tools=tools,
        port_block=port_block,
        worktree_path=worktree_rel,
        branch=branch,
        _subdir_backend=subdir_backend,
        _parent_git=parent_git if worktree_ready else None,
    )


async def release_reader_env(env: ReaderEnv) -> None:
    """Tear down a reader: close its shell tabs + remove its worktree. Best-effort."""
    loop = asyncio.get_running_loop()

    # Close only this reader's tabs — NEVER shell_cleanup (kills the shared session).
    try:
        env._subdir_backend.close_reader_tabs()
    except Exception as e:
        logger.warning("reader %d: closing shell tabs failed: %s", env.index, e)

    if env._parent_git is not None and env.worktree_path:
        try:
            async with _WORKTREE_LOCK:
                await loop.run_in_executor(
                    None, env._parent_git.worktree_remove, env.worktree_path, True
                )
        except Exception as e:
            logger.warning(
                "reader %d: worktree_remove(%s) failed: %s",
                env.index,
                env.worktree_path,
                e,
            )
