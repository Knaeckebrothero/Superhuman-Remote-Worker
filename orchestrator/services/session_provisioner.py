"""Session-side workspace provisioning + reconcile — the dispatcher-equivalent
for persistent sessions. See docs/features/unified_workspace_provisioning.md.

Jobs get workspace reconcile from the main dispatcher loop; sessions never did,
so a workspace wedged at 'failed'/missing for an active session never recovered.
This module closes that gap with an idempotent ensure + a periodic safety-net.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from services.workspace_lifecycle import (
    EnsureResult,
    WorkspaceOwner,
    ensure_workspace,
)
from src.core.backends.factory import LITE_BACKENDS

logger = logging.getLogger(__name__)


def _thread_metadata(thread: dict) -> dict:
    md = thread.get("metadata") or {}
    if isinstance(md, str):
        # md is a non-empty string here ("" was already coerced to {} above).
        try:
            md = json.loads(md)
        except (json.JSONDecodeError, TypeError):
            md = {}
    return md if isinstance(md, dict) else {}


def _ws_status(thread: dict) -> Optional[str]:
    return (_thread_metadata(thread).get("workspace_container") or {}).get("status")


def _thread_backend(thread: dict) -> Optional[str]:
    """The thread's configured ``workspace.backend`` (stored in
    ``metadata.config_override``), or None when unset."""
    co = _thread_metadata(thread).get("config_override") or {}
    if not isinstance(co, dict):
        return None
    return (co.get("workspace") or {}).get("backend")


async def ensure_session_workspace(
    thread_id: str, *, db, provisioner, suspension
) -> Optional[EnsureResult]:
    """Idempotently drive a session's workspace toward ready. Returns None when
    the thread is gone or ended (nothing to do)."""
    thread = await db.get_thread(thread_id)
    if not thread or thread.get("status") == "ended":
        return None
    if _thread_backend(thread) in LITE_BACKENDS:
        # virtual/none sessions run with no workspace pod (no_workspace_agent_mode.md
        # §4) — nothing to provision or reconcile. Centralized here so both the
        # resume path and the periodic reconcile sweep skip lite threads.
        logger.debug(
            "session %s uses a lite workspace backend — no workspace to provision",
            thread_id,
        )
        return None
    return await ensure_workspace(
        WorkspaceOwner.session(thread_id),
        provisioner=provisioner,
        suspension=suspension,
        current_status=_ws_status(thread),
    )


async def reconcile_session_workspaces(*, db, provisioner, suspension) -> int:
    """Safety-net: re-ensure workspaces for active sessions whose workspace
    container exists but is not ready (e.g. 'failed'). Idempotent — ensure_workspace
    no-ops in-progress states. Returns the count of threads re-ensured. Never raises."""
    try:
        threads = await db.list_threads_needing_workspace()
    except Exception:
        logger.exception("session reconcile: failed to list threads needing workspace")
        return 0
    ensured = 0
    for t in threads:
        tid = t["id"] if isinstance(t, dict) else t
        try:
            res = await ensure_session_workspace(
                str(tid), db=db, provisioner=provisioner, suspension=suspension
            )
            if res is not None:
                ensured += 1
        except Exception:
            logger.exception("session reconcile: ensure failed for thread %s", tid)
    if ensured:
        logger.info("session reconcile: re-ensured %d thread workspace(s)", ensured)
    return ensured
