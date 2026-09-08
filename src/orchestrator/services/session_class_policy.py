"""Session class and execution-lane admission.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane P, census group
``R_SESSION_POLICY``). Two questions live here:

* **Which class is this session?** ``officer`` and ``conference`` select a
  pinned-only background wake plane, so both booleans are materialized into the
  request layer at creation (:func:`materialized_session_class_override`) and
  re-read from the resolved stack by the protected-cloud boundary
  (:func:`protected_cloud_officer_active`).
* **Which execution plane may it run on?** :func:`resolve_thread_execution_lane`
  answers pinned vs stateless, and :func:`require_stateless_workspace` /
  :func:`require_stateless_end_workspace` are the HTTP boundary over the
  classifier in ``stateless_workspace_gate``.

Properties moved unchanged:

* **Nothing is decided by Python truthiness.** ``officer.enabled`` /
  ``officer.conference`` are compared with ``is True`` / ``is False`` and any
  other value is "malformed", a third outcome distinct from on and off.
  :func:`materialized_session_class_override` goes further and rejects a
  non-``bool`` with ``type(...) is not bool``, so ``1`` is not ``True`` here.
* **Fail closed.** Every unknown, future or malformed shape refuses rather than
  guessing. ``require_stateless_end_workspace`` admits exactly one extra state
  — an ended thread in ``retiring_process_zero`` with a structurally valid
  pending retirement authority — and falls back to the ordinary gate for
  everything else.
* **This module does not re-derive the workspace classification.**
  ``stateless_workspace_gate.stateless_session_workspace_check`` stays the one
  authority; the functions here translate its refusal reason into HTTP.

Naming note: the gate module owns a THREAD-shaped
``stateless_session_class_refusal(thread) -> "officer_requires_pinned" | ...``.
:func:`session_class_pinned_refusal` here is the CONFIG-shaped twin main called
``_stateless_session_class_refusal``; it returns operator-readable prose, not a
reason code. They are deliberately two functions, and the rename keeps them
from being confused for one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

from orchestrator.services.stateless_workspace_gate import (
    stateless_session_workspace_check,
    thread_metadata_object,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionLaneDependencies:
    """Collaborators for one execution-lane decision, resolved per invocation.

    ``stateless_session_enabled`` is a CALLABLE, not a value: the flag is a
    B11-owned import-time constant on ``orchestrator.main`` and a test that
    monkeypatches the name there must still steer this decision. Passing the
    bool would make such a test green but inert.

    ``container_provisioner`` and ``virtual_workspace_rclone_spec`` are the
    application's own, rebuilt per call rather than captured at import.
    """

    stateless_session_enabled: Callable[[], bool]
    container_provisioner: Any
    virtual_workspace_rclone_spec: Callable[[], dict[str, Any] | None]


def session_class_pinned_refusal(config: Any) -> str | None:
    """Return why this session class still requires its pinned wake plane."""
    if not isinstance(config, dict):
        return "session class configuration is malformed"
    officer = config.get("officer")
    if officer is None:
        return None
    if not isinstance(officer, dict):
        return "session class configuration is malformed"
    for field, reason in (
        ("conference", "conference sessions still use pinned lifecycle wakes"),
        ("enabled", "officer sessions still use the pinned watchdog and wake drain"),
    ):
        if field not in officer or officer[field] is False:
            continue
        if officer[field] is True:
            return reason
        return "session class configuration is malformed"
    return None


def materialized_session_class_override(
    effective_config: Any,
) -> dict[str, bool]:
    """Freeze lifecycle-affecting class bits into the request layer.

    Experts and account defaults are deliberately mutable for ordinary config,
    but ``officer`` and ``conference`` select a pinned-only background wake
    plane.  Materializing both booleans at creation makes that topology choice
    stable across later expert/account edits and gives every synchronous
    stateless admission boundary an authoritative value in thread metadata.
    """
    if not isinstance(effective_config, dict):
        raise HTTPException(status_code=400, detail="Session config is malformed")
    officer = effective_config.get("officer")
    if officer is None:
        officer = {}
    if not isinstance(officer, dict):
        raise HTTPException(status_code=400, detail="Officer config is malformed")
    for field in ("enabled", "conference"):
        if field in officer and type(officer[field]) is not bool:
            raise HTTPException(
                status_code=400,
                detail=f"officer.{field} must be a boolean",
            )
    return {
        "enabled": officer.get("enabled") is True,
        "conference": officer.get("conference") is True,
    }


def protected_cloud_officer_active(effective_config: Any) -> bool:
    """Whether an effective/resolved config selects the officer ceiling.

    Create-time merged fragments carry ``officer`` at the root; delivered
    resolved blobs carry it under ``agent``.  Accept both exact shapes without
    truthiness coercion so expert/account defaults are checked just like an
    explicit request override.  Conference-only sessions keep the ordinary
    interactive tool plane (``officer.enabled`` is false) and are therefore
    not rejected by this officer-specific boundary.
    """

    if not isinstance(effective_config, dict):
        raise ValueError("session class configuration is malformed")
    candidate = effective_config
    if "agent" in effective_config:
        candidate = effective_config.get("agent")
        if not isinstance(candidate, dict):
            raise ValueError("session class configuration is malformed")
    officer = candidate.get("officer")
    if officer is None:
        return False
    if not isinstance(officer, dict):
        raise ValueError("session class configuration is malformed")
    enabled = officer.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("session class configuration is malformed")
    return enabled is True


def require_stateless_workspace(thread: dict[str, Any]) -> str:
    """Require an exact stateless-supported workspace and session class.

    Lite workspaces remain supported. Sandbox support is intentionally narrow:
    the row must carry the orchestrator-owned Kubernetes lifecycle evidence,
    and a ready endpoint must be paired with its backing generation, pinned SSH
    identity, and runtime incarnation. VM, Docker and unknown/future states fail
    closed. Officer/conference sessions remain pinned until their background
    wake machinery becomes queue-aware.
    """
    backend, refusal_reason = stateless_session_workspace_check(thread)
    if refusal_reason is not None:
        logger.warning(
            "Stateless session refused before attach: thread=%s "
            "workspace_backend=%r reason=%s",
            thread.get("id"),
            backend,
            refusal_reason,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Stateless execution requires an attested Kubernetes sandbox "
                "or a supported lite workspace (virtual/none); this session's "
                f"workspace is unavailable ({refusal_reason})"
            ),
        )
    return backend


def require_stateless_end_workspace(thread: dict[str, Any]) -> str:
    """Require work admission or an exact in-progress End authority.

    ``retiring_process_zero`` is deliberately excluded from the ordinary
    stateless workspace gate: no new turn, control, upload, or Resume may use a
    runtime after terminal cleanup has begun.  End itself must nevertheless be
    able to retry that durable transition.  Admit only the exact intermediate
    status on an ended thread with a structurally valid pending retirement
    authority; every other refusal still uses the ordinary fail-closed gate.
    """

    backend, refusal_reason = stateless_session_workspace_check(thread)
    if refusal_reason is None:
        return backend

    metadata = thread_metadata_object(thread)
    workspace = metadata.get("workspace_container")
    if (
        refusal_reason == "workspace_status_unavailable"
        and thread.get("status") == "ended"
        and isinstance(workspace, dict)
        and workspace.get("status") == "retiring_process_zero"
    ):
        from shared.session_retirement import stateless_retirement_authority

        try:
            retirement = stateless_retirement_authority(metadata)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="Stateless retirement authority is malformed",
            ) from exc
        if retirement is not None:
            return backend

    return require_stateless_workspace(thread)


def resolve_thread_execution_lane(
    *,
    workspace_backend: str | None,
    effective_config: dict[str, Any],
    dependencies: ExecutionLaneDependencies,
) -> Literal["pinned", "stateless"]:
    """Resolve topology-neutral session admission with pinned fallback.

    The public create contract does not expose the execution plane. When the
    default-off pool gate is enabled, an ordinary supported workspace uses the
    stateless lane; unsupported infrastructure and pinned-only session classes
    retain the existing dedicated-agent path.
    """

    class_refusal = session_class_pinned_refusal(effective_config)
    if class_refusal is not None:
        return "pinned"

    if not dependencies.stateless_session_enabled():
        return "pinned"

    container_provisioner = dependencies.container_provisioner
    if workspace_backend == "sandbox":
        if container_provisioner.is_available and container_provisioner.in_cluster:
            return "stateless"
        return "pinned"

    if workspace_backend == "virtual":
        virtual_spec = dependencies.virtual_workspace_rclone_spec()
        if virtual_spec and virtual_spec.get("type") != "memory":
            return "stateless"
        return "pinned"

    if workspace_backend == "none":
        return "stateless"

    return "pinned"


__all__ = [
    "ExecutionLaneDependencies",
    "materialized_session_class_override",
    "protected_cloud_officer_active",
    "require_stateless_end_workspace",
    "require_stateless_workspace",
    "resolve_thread_execution_lane",
    "session_class_pinned_refusal",
]
