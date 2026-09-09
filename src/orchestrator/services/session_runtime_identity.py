"""Runtime identity predicates the pinned attach plane admits against.

Extracted verbatim from ``orchestrator.main`` (R1.B06 lane A, census group
``R_ATTACH``). Four predicates, no collaborators: every one of them decides
from values it is handed or from the process environment, so none takes a
dependency dataclass (port contract §P4 — pure helpers move as import
aliases).

They are grouped here rather than left inline because they are the *readers*
the rest of B06 compares against, and port contract §P6 makes that
single-reader property load-bearing:

* :func:`expected_agent_shas` is the single reader of ``AGENT_IMAGE`` /
  ``PERSISTENT_AGENT_IMAGE``. It reads the environment on every call rather
  than at import, so a test that rebinds those vars steers the pool skip and
  a redeploy that changes the tag is observed without a restart. No other
  module may reconstruct the expectation from parts.
* :func:`agent_sha_is_current` fails **open** when no SHA-tagged image is
  configured (local dev has none) and **closed** on every other unknown:
  absent metadata and absent ``build_sha`` are both stale. That asymmetry is
  deliberate and is preserved exactly.
* :func:`thread_uses_pinned_execution` is an exact whitelist against
  ``LANE_PINNED``. ``execution_lane`` is app-validated rather than
  CHECK-constrained, so a missing, corrupt or future value must never
  silently inherit the pinned provisioning plane. Truthiness is not a
  substitute for the equality test.
* :func:`thread_accepts_runtime` is only the lifecycle half of runtime
  admission; the lane is checked separately by
  :func:`thread_uses_pinned_execution`. Callers that need both must ask both.

``shared.run_queue`` stays a function-local import, as it was in main.
"""

from __future__ import annotations

import os
from typing import Any

from orchestrator.services.session_runtime_admission import (
    thread_runtime_is_preparable,
)


def expected_agent_shas() -> set[str]:
    """Extract short commit SHAs from configured agent image tags.

    Reads AGENT_IMAGE and PERSISTENT_AGENT_IMAGE env vars and extracts
    the SHA suffix from tags formatted as ``...:sha-<hash>``.
    """
    shas: set[str] = set()
    for var in ("AGENT_IMAGE", "PERSISTENT_AGENT_IMAGE"):
        tag = os.environ.get(var, "")
        if ":sha-" in tag:
            shas.add(tag.rsplit(":sha-", 1)[-1])
    return shas


def agent_sha_is_current(metadata: dict | None) -> bool:
    """Check if an agent's build SHA matches any expected image SHA."""
    expected = expected_agent_shas()
    if not expected:
        # No SHA-tagged images configured (local dev) — skip check
        return True
    if not metadata:
        return False
    build_sha = metadata.get("build_sha")
    if not build_sha:
        return False
    return build_sha in expected


def thread_uses_pinned_execution(thread: Any) -> bool:
    """Whether a thread may bind a registered/persistent agent.

    This is deliberately an exact whitelist. ``execution_lane`` is
    app-validated rather than CHECK-constrained, so missing, corrupt, or
    future values must not silently inherit the pinned provisioning plane.
    """
    from shared.run_queue import LANE_PINNED

    return bool(thread and thread.get("execution_lane") == LANE_PINNED)


def thread_accepts_runtime(thread: Any) -> bool:
    """Lifecycle half of runtime admission (lane is checked separately)."""

    return thread_runtime_is_preparable(thread)
