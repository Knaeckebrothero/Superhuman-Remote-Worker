"""``SubagentLedger`` — where a child's durable state goes.

The driver and the runtime report through this protocol only; WP1 shipped
the null implementation, WP2 the calls, WP3 ``DbSubagentLedger``
(``src/subagents/persistence.py``: a ``threads`` row of ``kind='subagent'``
plus its ``thread_messages`` transcript through ``src/core/thread_messages``).

Lifecycle, as the runtime drives it (``src/subagents/runtime.py``):

1. ``open(subagent_id, status="running", handle=…, subagent_type=…,
   parent_job_id=…, parent_thread_id=…, parent_tool_call_id=…, isolation=…,
   write_policy=…, brief_description=…, fork=…)`` once the child is built —
   the DB ledger creates the row here.
2. ``persist_message`` per transcript message (the driver, WP1).
3. ``update(subagent_id, status=<terminal>, outcome=…, turns=…, tokens=…,
   report_path=…, error=…)`` after the envelope is rendered.
4. ``lookup(parent_job_id, parent_tool_call_id)`` BEFORE a spawn: a terminal
   row for the same key means the child already ran (a parent re-running
   its tools node after a hard kill) and the runtime replays the stored
   report instead of spending. Optional on the protocol — a ledger without
   it simply never replays across a restart.

``status`` is always one of :data:`SUBAGENT_STATUSES`; the finer
classification (``capped:turns``, ``interrupted:drain``) rides in
``outcome`` verbatim. ``queued`` and ``running`` are live; every other
non-empty kind is terminal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

#: The status vocabulary the DB ledger stores in ``threads.subagent_status``.
#: ``running`` at spawn; the driver's ``SubagentResult.kind`` for a finished
#: child; ``cancelled`` when the parent's batch was cancelled mid-child.
SUBAGENT_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "parked",
    "interrupted",
    "capped",
    "error",
    "cancelled",
)


def is_terminal_status(status: Any) -> bool:
    """Every status but ``queued``/``running`` ends the child (open set: an
    unknown kind is terminal too — a row must never stay live on a typo)."""
    text = str(status or "").strip()
    return bool(text) and text not in {"queued", "running"}


@runtime_checkable
class SubagentLedger(Protocol):
    async def open(self, subagent_id: str, **fields: Any) -> Optional[Dict[str, str]]:
        """Record a child and return its durable run lease when available."""
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

    async def lookup(
        self, parent_job_id: str, parent_tool_call_id: str
    ) -> Optional[Dict[str, Any]]:
        return None


class RecordingLedger:
    """In-memory ledger — the test double, and a debugging aid.

    ``rows`` is what :meth:`lookup` answers from: a test seeds
    ``rows[(parent_job_id, tool_call_id)] = {<threads row>}`` to simulate a
    child that finished before a restart.
    """

    def __init__(self) -> None:
        self.opened: List[tuple[str, dict]] = []
        self.messages: List[tuple[str, Any, int]] = []
        self.updates: List[tuple[str, dict]] = []
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.lookups: List[Tuple[str, str]] = []

    async def open(self, subagent_id: str, **fields: Any) -> None:
        self.opened.append((subagent_id, dict(fields)))

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        self.messages.append((subagent_id, msg, turn_number))

    async def update(self, subagent_id: str, **fields: Any) -> None:
        self.updates.append((subagent_id, dict(fields)))

    async def lookup(
        self, parent_job_id: str, parent_tool_call_id: str
    ) -> Optional[Dict[str, Any]]:
        key = (str(parent_job_id), str(parent_tool_call_id))
        self.lookups.append(key)
        row = self.rows.get(key)
        if row is None or not is_terminal_status(row.get("subagent_status")):
            return None
        return dict(row)


__all__ = [
    "NullLedger",
    "RecordingLedger",
    "SUBAGENT_STATUSES",
    "SubagentLedger",
    "is_terminal_status",
]
