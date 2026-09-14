"""Reading and enriching the workspace tier a config override declares.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane P, census group
``R_SESSION_POLICY``). Shared by BOTH preparation paths — worker dispatch and
session attach — which is why it is not folded into
``session_workspace_policy``: that module answers "which tiers may a *session*
pick", this one answers "what tier does this override declare, and what does the
lite tier need injected", for jobs and sessions alike.

Three properties are load-bearing and moved unchanged:

* **Copy semantics.** :func:`inject_lite_workspace_config` returns the caller's
  own object untouched for any non-lite backend (identity, including ``None``),
  and MUTATES the caller's dict in place for a lite one. Callers rely on both
  halves; a uniform copy or a uniform mutation is a behaviour change.
* **A malformed override reads as "no backend", never as an error.**
  :func:`backend_from_override` swallows ``WorkspaceContractError`` and answers
  ``None``, so a corrupt stored override degrades to the default tier instead
  of failing a dispatch.
* **``virtual`` without an object store is a refusal, not a silent downgrade.**
  :class:`LiteWorkspaceConfigError` carries the operator-actionable message and
  both callers surface it (dispatch fails the job, attach refuses).

The object-store spec is reached through the ``virtual_workspace`` MODULE
rather than a ``from``-import of the function, so a test that patches
``orchestrator.services.virtual_workspace.virtual_workspace_rclone_spec``
steers every consumer through the one authority §P5 names.
"""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.services import virtual_workspace
from shared.backend_kinds import LITE_BACKENDS
from shared.workspace_contract import (
    WorkspaceContractError,
    configured_workspace_backend,
)
from orchestrator.services.stateless_workspace_gate import (
    declared_thread_workspace_backend,
)


class LiteWorkspaceConfigError(Exception):
    """A ``virtual`` tier was requested but this deployment has no object store.

    Raised by :func:`inject_lite_workspace_config`; dispatch fails the job and
    session-attach refuses, both with the actionable message carried here.
    """


def backend_from_override(config_override: Any) -> Optional[str]:
    """Extract ``workspace.backend`` from a (dict | JSON-string | None) override.

    Mirrors the parsing the dispatch path already does for ``config_override``
    so every caller reads the backend the same way.
    """
    try:
        return configured_workspace_backend(config_override)
    except WorkspaceContractError:
        return None


def thread_workspace_backend(thread: Any) -> Optional[str]:
    """Extract the selected workspace backend from a thread row's stored
    ``metadata.config_override.workspace.backend`` (handles JSON-string metadata).

    Used by the session-start paths to size the readiness budget for a VM-backed
    thread on resume, where the caller may not carry the config_override.
    """
    return declared_thread_workspace_backend(thread)


def is_lite_config_override(config_override: Any) -> bool:
    """True if ``config_override`` selects a lite (``virtual``/``none``) backend.

    Lite tiers have no git/workspace, so the orchestrator's git-graft lifecycle
    subjobs (scholar/critic/curator) can neither hand their ``output/`` back to
    the parent nor read the parent's deliverables — that handoff is entirely
    Gitea-branch-based (see ``_graft_subjob_output``). They are therefore skipped
    for lite jobs; the main agent researches/curates inline. A lite-compatible
    handoff (object-store copy instead of git graft) is deferred to v2
    (no_workspace_agent_mode.md §8).
    """
    return backend_from_override(config_override) in LITE_BACKENDS


def inject_lite_workspace_config(
    config_override: Optional[dict[str, Any]], *, prefix: str
) -> Optional[dict[str, Any]]:
    """Enrich ``config_override.workspace`` for the lite tiers (``virtual``/``none``).

    No-op for any other backend. For ``virtual`` it attaches the object-store
    ``mounts`` (credentials sourced from deployment env and injected *in-flight*
    only — never persisted to the job/thread row, matching how dispatch keeps
    API keys inline). For ``none`` it strips any stray mounts. Both force
    ``git_versioning`` off (§8 — lite tiers have no git).

    ``prefix`` is the object-store key prefix for this owner
    (``jobs/<id>/`` for jobs, ``threads/<id>/`` for sessions).

    Raises:
        LiteWorkspaceConfigError: ``virtual`` requested but no object store is
            configured for this deployment.
    """
    backend = backend_from_override(config_override)
    if backend not in LITE_BACKENDS:
        return config_override

    config_override = config_override or {}
    ws = config_override.setdefault("workspace", {})
    ws["backend"] = backend
    ws["git_versioning"] = False

    if backend == "virtual":
        spec = virtual_workspace.virtual_workspace_rclone_spec()
        if spec is None:
            raise LiteWorkspaceConfigError(
                "workspace.backend='virtual' needs an object store, but this "
                "deployment has none configured. Set virtualWorkspace.rclone.type "
                "(+ .root) and, for s3, virtualWorkspace.s3.* plus the "
                "VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID / _SECRET_ACCESS_KEY secrets "
                "— or use backend='none' for a no-file-tools agent, or "
                "'sandbox'/'vm' for a full workspace."
            )
        ws["mounts"] = [
            {
                "name": "workspace",
                "rclone_spec": spec,
                "prefix": prefix,
                "access": "read_write",
            }
        ]
    else:  # "none" — no file tools, so no object-store mounts
        ws.pop("mounts", None)

    return config_override


__all__ = [
    "LiteWorkspaceConfigError",
    "backend_from_override",
    "inject_lite_workspace_config",
    "is_lite_config_override",
    "thread_workspace_backend",
]
