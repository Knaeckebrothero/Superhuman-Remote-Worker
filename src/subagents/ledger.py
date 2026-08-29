"""``SubagentLedger`` — where a child's durable state goes.

The driver reports through this protocol only; WP1 ships the null
implementation. WP3 adds ``DbSubagentLedger`` (a ``threads`` row of
``kind='subagent'`` + its ``thread_messages`` transcript through
``src/core/thread_messages.py``).
"""

from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable


@runtime_checkable
class SubagentLedger(Protocol):
    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        """Upsert one transcript message the instant the loop produced it."""
        ...

    async def update(self, subagent_id: str, **fields: Any) -> None:
        """Record a status / outcome / counters change on the child row.

        Known keys: ``status`` (running|completed|capped|parked|interrupted|
        error|cancelled), ``turns``, ``tokens``, ``report_path``, ``error``.
        """
        ...


class NullLedger:
    """Persists nothing (tests, and parents that keep no child transcript)."""

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        return None

    async def update(self, subagent_id: str, **fields: Any) -> None:
        return None


class RecordingLedger:
    """In-memory ledger — the test double, and a debugging aid."""

    def __init__(self) -> None:
        self.messages: List[tuple[str, Any, int]] = []
        self.updates: List[tuple[str, dict]] = []

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        self.messages.append((subagent_id, msg, turn_number))

    async def update(self, subagent_id: str, **fields: Any) -> None:
        self.updates.append((subagent_id, dict(fields)))


__all__ = ["NullLedger", "RecordingLedger", "SubagentLedger"]
