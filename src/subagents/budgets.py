"""Child budgets (U3 B.4): turns, tokens, return size and staleness.

Parsed from the RAW roster entry's ``limits`` block — ``LimitsConfig`` is a
closed dataclass, so the five child keys never reach the parsed config —
with per-built-in defaults. ``max_turns`` counts provider calls per brief,
``max_tokens`` the cumulative input+output spend across those calls.

There is deliberately no wall clock inside the loop (the note §1.3): the only
time-based bound is the :class:`StalenessWatcher`, which watches the driver's
activity stamps from outside and arms a graceful stop + one synthesis turn,
then escalates to a hard interrupt when the child still does not return.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

_FIELDS: Tuple[str, ...] = (
    "max_turns",
    "max_tokens",
    "return_budget_tokens",
    "stale_idle_s",
    "stale_in_tool_s",
)

#: Per-built-in defaults: (max_turns, max_tokens, return_budget_tokens,
#: stale_idle_s, stale_in_tool_s). B.4.
BUILTIN_DEFAULTS: Mapping[str, Tuple[int, int, int, int, int]] = {
    "explorer": (40, 200_000, 2000, 300, 600),
    "reader": (30, 150_000, 1500, 300, 600),
    "implementer": (150, 600_000, 3000, 450, 1200),
    "tester": (60, 300_000, 2500, 300, 1800),
    "reviewer": (60, 300_000, 3000, 300, 600),
    "verifier": (60, 300_000, 3000, 300, 1200),
    "probe": (60, 300_000, 4000, 300, 1200),
}
FALLBACK_DEFAULTS: Tuple[int, int, int, int, int] = (50, 250_000, 2000, 300, 900)


def default_budget_key(entry: Mapping[str, Any], subagent_type: Optional[str]) -> str:
    """Which built-in default row an entry falls on.

    A library ``$ref`` (``subagents/explorer``) keys on the library name even
    when the roster calls it something else; an inline entry on its roster
    name (``agent_id``); the caller's ``subagent_type`` is the last candidate.
    The first candidate present in :data:`BUILTIN_DEFAULTS` wins, else
    ``"fallback"``.
    """
    candidates = []
    ref = entry.get("_ref")
    if isinstance(ref, str) and ref:
        candidates.append(posixpath.basename(ref.rstrip("/")))
    agent_id = entry.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        candidates.append(agent_id)
    if subagent_type:
        candidates.append(str(subagent_type))
    for candidate in candidates:
        if candidate in BUILTIN_DEFAULTS:
            return candidate
    return "fallback"


@dataclass(frozen=True)
class ChildBudgets:
    """The five child bounds. ``stale_*`` are seconds of *no activity*."""

    max_turns: int
    max_tokens: int
    return_budget_tokens: int
    stale_idle_s: float
    stale_in_tool_s: float

    @property
    def stale_escalation_s(self) -> float:
        """Grace after the soft stale arm before the hard interrupt (B.4)."""
        return self.stale_in_tool_s / 2

    @classmethod
    def defaults_for(cls, key: Optional[str]) -> "ChildBudgets":
        row = BUILTIN_DEFAULTS.get(key or "", FALLBACK_DEFAULTS)
        return cls(*row)

    @classmethod
    def from_entry(
        cls, entry: Mapping[str, Any], subagent_type: Optional[str] = None
    ) -> "ChildBudgets":
        """Budgets of a RESOLVED roster entry: its ``limits`` over the defaults.

        A key that is absent, non-numeric or not positive falls back to the
        default for that field (logged) — a typo never disables a bound.
        """
        base = cls.defaults_for(default_budget_key(entry, subagent_type))
        limits = entry.get("limits")
        if not isinstance(limits, Mapping):
            return base
        values = {}
        for name in _FIELDS:
            default = getattr(base, name)
            raw = limits.get(name)
            if raw is None:
                values[name] = default
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = float("nan")
            if not (number > 0):
                logger.warning(
                    "subagent %s: limits.%s=%r is not a positive number — using %s",
                    entry.get("agent_id") or subagent_type,
                    name,
                    raw,
                    default,
                )
                values[name] = default
                continue
            values[name] = int(number) if isinstance(default, int) else float(number)
        return cls(**values)


class StalenessSubject(Protocol):
    """What the watcher reads on a driver (all stamps use ``clock``)."""

    clock: Callable[[], float]
    running: bool
    last_activity: float
    in_tool_since: Optional[float]
    stale_armed_at: Optional[float]

    def arm_stale(self, kind: str) -> None: ...

    def escalate_stale(self, kind: str) -> None: ...


class StalenessWatcher:
    """Per-child watcher task (B.4).

    ``check`` is one pure step so tests drive it with a patched clock; ``run``
    polls it. Soft stage: idle (no token/usage/tool activity while a brief
    runs and no tool is executing) for ``stale_idle_s``, or one tool call
    executing for ``stale_in_tool_s`` → ``arm_stale`` (graceful interrupt +
    synthesis). Hard stage: the child still has not returned
    ``stale_escalation_s`` after the arm → ``escalate_stale`` (hard
    interrupt event + cancel).
    """

    def __init__(
        self,
        subject: StalenessSubject,
        budgets: ChildBudgets,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self._subject = subject
        self._budgets = budgets
        self.poll_interval = poll_interval
        self._kind: Optional[str] = None

    def check(self) -> Optional[str]:
        subject = self._subject
        if not subject.running:
            return None
        now = subject.clock()
        armed_at = subject.stale_armed_at
        if armed_at is not None:
            if now - armed_at >= self._budgets.stale_escalation_s:
                subject.escalate_stale(self._kind or "idle")
                return "escalated"
            return None
        kind: Optional[str] = None
        if subject.in_tool_since is not None:
            if now - subject.in_tool_since >= self._budgets.stale_in_tool_s:
                kind = "in_tool"
        elif now - subject.last_activity >= self._budgets.stale_idle_s:
            kind = "idle"
        if kind is None:
            return None
        self._kind = kind
        subject.arm_stale(kind)
        return "armed"

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                self.check()
            except Exception:  # never let the watchdog die on a bookkeeping bug
                logger.warning("subagent staleness check failed", exc_info=True)


__all__ = [
    "BUILTIN_DEFAULTS",
    "FALLBACK_DEFAULTS",
    "ChildBudgets",
    "StalenessSubject",
    "StalenessWatcher",
    "default_budget_key",
]
