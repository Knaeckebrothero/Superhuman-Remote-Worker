"""Asking a running agent what it ACTUALLY bound, and saying so honestly.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane P, census group
``R_POLICY``). This is the D6 seam: the orchestrator does not recompute an
agent's tool binding, it asks. Everything that decides the final toolset (the
runtime injection layer, ``filter_tools_by_backend``, ``load_tools``'s per-tool
fallback) lives in the agent process, and a second implementation here would
drift — which was the original bug in this series.

Properties moved unchanged:

* **"We could not measure this" and "the agent has no such tools" are different
  facts.** :class:`Measurement` carries ``categories is None`` for the former
  with a ``reason`` in words, and :func:`origin_fields` renders the
  discriminator: ``prediction`` (nothing measured, ``prediction_reason`` set),
  ``agent`` (full structured report) and ``agent_partial`` (bound names only,
  ``degraded_reason`` set). ``prediction_reason`` and ``degraded_reason`` are
  deliberately different keys.
* **ONE deadline across BOTH hops.** :data:`AGENT_TOOLSET_BUDGET_S` is enforced
  with ``asyncio.wait_for`` around :func:`agent_toolset_probe`, which is why the
  ``/status`` fallback is inside that function rather than beside it: httpx's
  timeout is per-operation, so a 404 followed by the fallback would otherwise
  cost two full budgets.
* **Stale bindings are skipped, ``ready`` is not.** The terminal set exists
  because an agent row keeps its ``pod_ip`` after the pod is gone; the pre-ready
  set is separate so the reported reason is the true one. ``ready`` is
  deliberately probed — an agent stays ``ready`` for up to one heartbeat
  interval after attach.

The store arrives per invocation through :class:`AgentToolsetDependencies`
rather than being captured at import, because ``orchestrator.main.postgres_db``
is what tests rebind.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx

from shared.runtime.core.tool_report import (
    ORIGIN_AGENT,
    ORIGIN_AGENT_PARTIAL,
    ORIGIN_PREDICTION,
    ToolReportError,
    categorize_tool_names,
    read_agent_toolset_report,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentToolsetDependencies:
    """Collaborators for one toolset probe, resolved per invocation.

    ``store`` is main's ``postgres_db``. It is rebuilt per call rather than
    captured at import so a test that rebinds the name on ``orchestrator.main``
    still steers the agent lookup.
    """

    store: Any


#: TOTAL wall-clock budget for the agent toolset probe, both hops included.
#: Enforced with ``asyncio.wait_for``, not with httpx's timeout: httpx's is
#: per-operation, so a 404 on ``/session/toolset`` followed by the ``/status``
#: fallback would otherwise cost two full budgets, and a pod trickling bytes
#: could exceed either without ever tripping one. The cockpit blocks its
#: settings pane on this endpoint, so the bound has to be a real deadline.
AGENT_TOOLSET_BUDGET_S = 3.0

#: Agent rows in these states keep a ``pod_ip`` that no longer routes.
AGENT_TERMINAL_STATUSES = frozenset({"offline", "failed", "completed"})

#: Registered but not yet serving: nothing is bound, so there is nothing to
#: measure and the probe would only spend budget discovering that. Kept apart
#: from the terminal set so the reason we report is the true one.
AGENT_PREREADY_STATUSES = frozenset({"booting"})

#: NOTE on what is deliberately NOT skipped: ``ready``. An agent stays ``ready``
#: for up to one heartbeat interval (60s) after attach, so gating the probe on
#: ``status == "session"`` would report a prediction for the first minute of
#: every session — the exact silently-wrong answer this endpoint removes.


class Measurement(NamedTuple):
    """What a running agent said, or why it could not be asked.

    ``categories`` is ``None`` whenever there is nothing to measure, and
    ``reason`` then says why in words a caller can show — "we could not measure
    this" and "the agent has no such tools" are different facts, and conflating
    them is D1 all over again.
    """

    categories: dict[str, list[str]] | None
    observed_at: str | None
    backend: dict[str, Any] | None
    reason: str | None
    #: True when the agent gave us bound tool NAMES but none of the structure
    #: around them — no timestamp, no workspace capabilities, no agent-side
    #: categorisation. Surfaced as ``origin: "agent_partial"`` so a caller
    #: cannot read a missing ``backend`` as "this tier gates nothing".
    partial: bool = False


def unmeasured(reason: str) -> "Measurement":
    return Measurement(None, None, None, reason)


def origin_fields(m: "Measurement") -> dict[str, Any]:
    """The provenance block every tool-groups answer carries.

    One function so the thread endpoint and the preview route cannot describe
    their own trustworthiness differently. ``origin`` is the discriminator:

    - ``agent``          full structured report; ``observed_at`` + ``backend`` set
    - ``agent_partial``  bound names only (an image predating the route); the
                         names are trustworthy, everything around them absent
    - ``prediction``     no measurement at all

    ``degraded_reason`` is set only on ``agent_partial`` and says what is
    missing. It is deliberately a different key from ``prediction_reason`` —
    "measured but thin" and "not measured" are different facts, and a caller
    that conflates them will render a workspace-tier explanation it does not
    have.
    """
    if m.categories is None:
        return {
            "origin": ORIGIN_PREDICTION,
            "observed_at": None,
            "prediction_reason": m.reason,
            "degraded_reason": None,
            "backend": None,
        }
    return {
        "origin": ORIGIN_AGENT_PARTIAL if m.partial else ORIGIN_AGENT,
        "observed_at": m.observed_at,
        "prediction_reason": None,
        "degraded_reason": m.reason if m.partial else None,
        "backend": m.backend,
    }


async def agent_toolset_measurement(
    thread: dict[str, Any], *, dependencies: "AgentToolsetDependencies"
) -> "Measurement":
    """Ask the bound agent what it ACTUALLY bound.

    This is the D6 seam. The orchestrator does NOT recompute the agent's
    binding: it asks. Everything that decides the final toolset (the runtime
    injection layer, ``filter_tools_by_backend``, ``load_tools``'s per-tool
    fallback) lives in the agent process, and a second implementation of that
    here would drift — which is the original bug in this series.

    Hard total deadline and no retry: this backs a settings pane, and a slow
    answer that is *labelled* a prediction beats a fast pane that stalls on a
    dead pod.
    """
    agent_id = thread.get("agent_id")
    if not agent_id:
        return unmeasured("no agent is attached to this session")
    try:
        agent = await dependencies.store.get_agent(str(agent_id))
    except Exception:
        logger.warning("Toolset probe could not load agent %s", agent_id)
        return unmeasured("the bound agent could not be looked up")
    if not agent or not agent.get("pod_ip"):
        return unmeasured("the bound agent has no reachable address")
    status = agent.get("status")
    if status in AGENT_TERMINAL_STATUSES:
        # A stale binding: the row keeps its pod_ip after the pod is gone, so
        # probing it burns the whole budget on the settings pane's critical
        # path. Observed on k3d — an ended session left `offline` + a pod_ip
        # that no longer routes.
        return unmeasured(f"the bound agent is {status}")
    if status in AGENT_PREREADY_STATUSES:
        return unmeasured(f"the bound agent is still {status}")

    # ONE deadline across both hops. See AGENT_TOOLSET_BUDGET_S.
    try:
        return await asyncio.wait_for(
            agent_toolset_probe(agent), AGENT_TOOLSET_BUDGET_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("Toolset probe to agent %s exceeded its budget", agent_id)
        return unmeasured("the agent did not answer within the probe budget")


async def agent_toolset_probe(agent: dict[str, Any]) -> "Measurement":
    """The network half of :func:`agent_toolset_measurement`.

    Split out so ONE ``asyncio.wait_for`` bounds the whole thing — including
    the ``/status`` fallback, which is the normal path against any agent image
    predating ``/session/toolset``.
    """
    url = f"http://{agent['pod_ip']}:{agent.get('pod_port') or 8001}/session/toolset"
    try:
        async with httpx.AsyncClient(timeout=AGENT_TOOLSET_BUDGET_S) as client:
            response = await client.get(url)
    except Exception as exc:
        logger.info("Toolset probe to %s failed: %s", url, exc)
        return unmeasured("the agent did not answer the toolset probe")

    if response.status_code == 404:
        # An agent image that predates this route. Its `/status` already
        # publishes `session.tools`, which is the same measurement with less
        # structure — categorise it here rather than downgrade to a guess.
        return await agent_toolset_from_status(agent)
    if response.status_code != 200:
        return unmeasured(f"the agent answered {response.status_code}")

    try:
        payload = response.json()
    except Exception:
        return unmeasured("the agent's toolset answer was not JSON")
    if not payload.get("attached"):
        return unmeasured("the agent is not attached to a session")
    report = payload.get("report") or {}
    try:
        categories = read_agent_toolset_report(payload.get("report"))
    except ToolReportError as exc:
        logger.warning("Toolset probe returned an unreadable report: %s", exc)
        return unmeasured("the agent's toolset report could not be read")
    backend = report.get("backend")
    return Measurement(
        categories=categories,
        observed_at=report.get("observed_at"),
        backend=backend if isinstance(backend, dict) else None,
        reason=None,
    )


async def agent_toolset_from_status(agent: dict[str, Any]) -> "Measurement":
    """Fallback measurement for an agent image without ``/session/toolset``.

    Still the agent's own answer — ``/status`` returns ``[t.name for t in
    _session.tools]`` — so this stays a measurement, not a prediction. The one
    loss is MCP tools: they are registered into the AGENT's process registry at
    attach, so categorising a flat name list here files them under
    ``unclassified`` instead of ``mcp``.
    """
    url = f"http://{agent['pod_ip']}:{agent.get('pod_port') or 8001}/status"
    try:
        async with httpx.AsyncClient(timeout=AGENT_TOOLSET_BUDGET_S) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return unmeasured("the agent has no toolset endpoint")
        names = (response.json() or {}).get("tools")
    except Exception as exc:
        logger.info("Toolset /status fallback to %s failed: %s", url, exc)
        return unmeasured("the agent has no toolset endpoint")
    if not isinstance(names, list):
        return unmeasured("the agent has no toolset endpoint")
    return Measurement(
        categories=categorize_tool_names([n for n in names if isinstance(n, str)]),
        observed_at=None,
        backend=None,
        reason=(
            "this agent image predates GET /session/toolset, so only the bound "
            "tool names are available — no observation time, no workspace "
            "capabilities, and MCP tools cannot be categorised"
        ),
        partial=True,
    )


__all__ = [
    "AGENT_PREREADY_STATUSES",
    "AGENT_TERMINAL_STATUSES",
    "AGENT_TOOLSET_BUDGET_S",
    "AgentToolsetDependencies",
    "Measurement",
    "agent_toolset_from_status",
    "agent_toolset_measurement",
    "agent_toolset_probe",
    "origin_fields",
    "unmeasured",
]
