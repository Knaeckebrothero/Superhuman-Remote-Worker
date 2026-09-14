"""Commissioning an Officer onto a dedicated Pod, and saying so when it fails.

Extracted verbatim from ``orchestrator.main`` (R1.B06 lane A, census group
``R_SESSION_POLICY``). Both functions run **after** their HTTP handler has
already answered 200, as detached ``asyncio`` tasks. That is the whole reason
they exist as named module-level functions rather than closures: an exception
in a detached task is swallowed, and the session then simply never becomes
ready and never reports why.

Two properties are load-bearing and moved unchanged:

* **A dead generation is never spoken for.**
  :func:`emit_session_provisioning_failure` re-reads the thread row and
  compares it against the runtime authority it captured before emitting. If
  the generation moved on, it stays silent rather than attributing a failure
  to whatever runtime holds the thread now. When no authority was captured at
  all it emits ungenerationed, exactly as before — that is the caller's
  choice, not a fallback this module invents.
* **The guard is total.**
  :func:`provision_commissioned_officer` wraps both the provisioner call and
  the unusable-result branch, and records through the same failure path in
  either case. A ``config_name`` the provisioner boundary refuses and a
  Kubernetes outage produce the same visible outcome: a
  ``session.lifecycle: failed`` on the owner's feed. The emit itself is
  additionally fire-and-forget — a publish failure is logged, never raised
  back into the task.

``orchestrator.services.session_lifecycle`` stays a function-local import, as
it was in main: the boundary suite patches ``session_lifecycle.emit`` on the
module and a module-level binding here would resolve past that patch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from orchestrator.services.session_runtime_admission import (
    same_thread_runtime_authority,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommissionedOfficerDependencies:
    """Collaborators for one commissioned-Officer provisioning, per invocation.

    * ``store`` — main's ``postgres_db``; the re-read that proves the captured
      generation is still current.
    * ``persistent_provisioner`` — the dedicated-Pod authority. Rebound on
      main by the write-boundary suite, so it is injected rather than
      imported.
    * ``emit_session_provisioning_failure`` — main's own bridge, injected so
      the failure path stays one patch point for every caller. Signature
      ``(thread_id, user_id, runtime_authority, reason) -> Awaitable[None]``.
    """

    store: Any
    persistent_provisioner: Any
    emit_session_provisioning_failure: Callable[..., Awaitable[None]]


async def emit_session_provisioning_failure(
    thread_id: str,
    user_id: str | None,
    runtime_authority: Any | None,
    reason: str,
    *,
    dependencies: CommissionedOfficerDependencies,
) -> None:
    """Record a fire-and-forget provisioning failure on the owner's feed.

    Same shape as ``routers/sessions.py::_do_prepare`` and
    ``services/provision_or_assign.py``: re-read the row, refuse to speak for a
    generation that has moved on, then emit ``session.lifecycle: failed`` with
    the reason. Without it a task that raises after the handler returned 200 is
    swallowed by ``asyncio`` and the session simply never becomes ready.
    """
    if not user_id:
        return
    from orchestrator.services.session_lifecycle import emit as lifecycle_emit

    try:
        if runtime_authority is not None:
            current = await dependencies.store.get_thread(thread_id)
            if not same_thread_runtime_authority(current, runtime_authority):
                return
            lifecycle_emit(
                str(user_id),
                thread_id,
                "failed",
                reason=reason,
                session_runtime_generation=runtime_authority.generation,
            )
            return
        lifecycle_emit(str(user_id), thread_id, "failed", reason=reason)
    except Exception:
        logger.exception(
            "Could not publish the provisioning failure for thread %s", thread_id
        )


async def provision_commissioned_officer(
    thread_id: str,
    *,
    user_id: str,
    config_name: str,
    runtime_authority: Any,
    dependencies: CommissionedOfficerDependencies,
) -> None:
    """Commission an Officer straight onto a dedicated persistent Pod.

    Scheduled by ``create_thread`` as its own task, after the handler has
    already answered 200 — so this is the last line before the work
    disappears. Anything raised here (a ``config_name`` the provisioner
    boundary refuses, a K8s outage) is otherwise swallowed by asyncio and the
    post never becomes ready and never reports why. Guarded whole, recording
    the failure the way ``services/provision_or_assign.py`` does.

    Module level rather than a closure so the guard is directly testable.
    """
    try:
        result = await dependencies.persistent_provisioner.create_agent_pod(
            thread_id,
            config_name=config_name,
            expected_runtime_generation=runtime_authority.generation,
        )
        if not result.usable:
            logger.warning(
                "Commissioned Officer %s: dedicated persistent provisioning is %s (%s)",
                thread_id,
                result.status.value,
                result.failure_class or "no-detail",
            )
            await dependencies.emit_session_provisioning_failure(
                thread_id,
                user_id,
                runtime_authority,
                f"officer runtime provisioning {result.status.value}"
                f" ({result.failure_class or 'no-detail'})",
            )
    except Exception as exc:
        logger.exception(
            "Commissioned Officer %s: dedicated persistent provisioning raised: %s",
            thread_id,
            exc,
        )
        await dependencies.emit_session_provisioning_failure(
            thread_id, user_id, runtime_authority, str(exc)
        )
