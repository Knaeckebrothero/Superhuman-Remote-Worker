"""``SubagentLedger`` — where a child's durable state goes.

The driver and the runtime report through this protocol only; WP1 shipped
the null implementation and WP2 the calls. WP3 adds ``DbSubagentLedger`` (a
``threads`` row of ``kind='subagent'`` + its ``thread_messages`` transcript
through ``src/core/thread_messages.py``).

Lifecycle, as the runtime drives it (``src/subagents/runtime.py``):

1. ``open(subagent_id, status="running", handle=…, subagent_type=…,
   parent_job_id=…, parent_thread_id=…, parent_tool_call_id=…, isolation=…,
   write_policy=…, brief_description=…)`` once the child is built — the DB
   ledger creates the row here.
2. ``persist_message`` per transcript message (the driver, WP1).
3. ``update(subagent_id, status=<terminal>, outcome=…, turns=…, tokens=…,
   report_path=…, error=…)`` after the envelope is rendered.

``status`` is always one of :data:`SUBAGENT_STATUSES`; the finer
classification (``capped:turns``, ``interrupted:drain``) rides in
``outcome`` verbatim.
"""

from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable

#: The status vocabulary the DB ledger stores in ``threads.subagent_status``.
#: ``running`` at spawn; the driver's ``SubagentResult.kind`` for a finished
#: child; ``cancelled`` when the parent's batch was cancelled mid-child.
SUBAGENT_STATUSES: tuple[str, ...] = (
    "running",
    "completed",
    "parked",
    "interrupted",
    "capped",
    "error",
    "cancelled",
)


@runtime_checkable
class SubagentLedger(Protocol):
    async def open(self, subagent_id: str, **fields: Any) -> None:
        """Record a new child (``status="running"`` + its identity fields)."""
        ...

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        """Upsert one transcript message the instant the loop produced it."""
        ...

    async def update(self, subagent_id: str, **fields: Any) -> None:
        """Record a status / outcome / counters change on the child row.

        Known keys: ``status`` (one of :data:`SUBAGENT_STATUSES`), ``outcome``
        (the driver's full status string), ``turns``, ``tokens``,
        ``report_path``, ``error``.
        """
        ...


class NullLedger:
    """Persists nothing (tests, and parents that keep no child transcript)."""

    async def open(self, subagent_id: str, **fields: Any) -> None:
        return None

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        return None

    async def update(self, subagent_id: str, **fields: Any) -> None:
        return None


class RecordingLedger:
    """In-memory ledger — the test double, and a debugging aid."""

    def __init__(self) -> None:
        self.opened: List[tuple[str, dict]] = []
        self.messages: List[tuple[str, Any, int]] = []
        self.updates: List[tuple[str, dict]] = []

    async def open(self, subagent_id: str, **fields: Any) -> None:
        self.opened.append((subagent_id, dict(fields)))

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        self.messages.append((subagent_id, msg, turn_number))

    async def update(self, subagent_id: str, **fields: Any) -> None:
        self.updates.append((subagent_id, dict(fields)))


__all__ = ["NullLedger", "RecordingLedger", "SUBAGENT_STATUSES", "SubagentLedger"]
