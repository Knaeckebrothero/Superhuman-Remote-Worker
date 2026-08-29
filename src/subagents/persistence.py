"""``DbSubagentLedger`` — the durable side of a subagent child (U3 WP3, B.1).

One ``threads`` row of ``kind='subagent'`` per child, its transcript in
``thread_messages`` through the same serialisers a session uses
(``src/core/thread_messages``), and the lifecycle on the row's own columns:

- ``open``  → ``POST /api/agents/jobs/{job_id}/subagents`` through the
  orchestrator client. The orchestrator owns thread creation and derives the
  row's ``user_id`` / ``project_id`` from the job, which is what lets the job
  owner read the child's transcript through the ordinary thread endpoints.
  The in-process ``subagent_id`` becomes the row id, so the audit rows, the
  ``llm_requests`` rows and the thread share one identity.
- ``persist_message`` → ``save_thread_message(thread_id, **row)`` on the
  agent-side pool (idempotent upsert by the message's stable id).
- ``update`` → ``update_subagent_thread`` on the agent-side pool (guarded by
  ``kind='subagent'``). The thread ``status`` stays ``active`` while the child
  runs and becomes ``ended`` on ANY terminal kind — ``valid_thread_status``
  is never widened; the outcome lives in ``subagent_status`` (the bare kind)
  and ``subagent_outcome`` (the driver's full classification), the error in
  ``subagent_error``. Columns rather than ``metadata.subagent.*`` so the
  terminal write never crosses the ``UPDATE OF metadata`` trigger surface on
  ``threads`` (managed-repository / process-zero fences) for nothing.
- ``lookup`` → ``get_subagent_thread_by_call`` on the agent-side pool: a
  terminal row for ``(parent_job_id, parent_tool_call_id)`` is the
  rotation-surviving idempotency record the runtime replays instead of
  spending again.

``parent_iteration`` (``metadata.subagent``) is the parent's checkpointed
LLM-turn counter at spawn, read off the parent ``ToolContext``
(``_current_turn_count``, stamped by the graph's execute / tools nodes) —
the runtime itself does not know the graph's iteration (WP2 §8.1).

Every write is best-effort: the runtime bounds each call and logs a
failure; a child never fails because its ledger did. A child whose row could
not be created keeps no durable state at all (no transcript, no update) —
the runtime's in-memory record still serves the parent for this process
life.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..core.thread_messages import _serialize_message_row
from .ledger import is_terminal_status

logger = logging.getLogger(__name__)


class DbSubagentLedger:
    """The DB-backed :class:`~src.subagents.ledger.SubagentLedger`."""

    def __init__(
        self,
        client: Any,
        postgres: Any,
        *,
        parent_context: Any = None,
    ) -> None:
        if client is None or postgres is None:
            raise ValueError(
                "DbSubagentLedger needs the orchestrator client (row creation) "
                "and the agent-side pool (transcript + lifecycle writes)"
            )
        self.client = client
        self.postgres = postgres
        self.parent_context = parent_context
        #: subagent_id → thread row id (the same value once the row exists).
        self._rows: Dict[str, str] = {}
        #: Children whose row creation definitively failed (no durable state).
        self._failed: set[str] = set()

    @classmethod
    def from_context(cls, context: Any) -> Optional["DbSubagentLedger"]:
        """The ledger of a parent ``ToolContext`` — ``None`` unless both the
        orchestrator client and the agent-side pool are on it (a test parent,
        a bare-metal agent without a DB: the runtime then uses ``NullLedger``)."""
        client = getattr(context, "orchestrator_client", None)
        postgres = getattr(context, "postgres_db", None)
        if client is None or postgres is None:
            return None
        return cls(client, postgres, parent_context=context)

    # ------------------------------------------------------------------
    # Introspection (tests, debugging)
    # ------------------------------------------------------------------

    @property
    def rows(self) -> Dict[str, str]:
        return dict(self._rows)

    @property
    def failed(self) -> set[str]:
        return set(self._failed)

    def thread_id_for(self, subagent_id: str) -> Optional[str]:
        return self._rows.get(str(subagent_id))

    def _parent_iteration(self, fields: Dict[str, Any]) -> Optional[int]:
        explicit = fields.get("parent_iteration")
        if explicit is not None:
            try:
                return int(explicit)
            except (TypeError, ValueError):
                return None
        value = getattr(self.parent_context, "_current_turn_count", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # The protocol
    # ------------------------------------------------------------------

    async def open(self, subagent_id: str, **fields: Any) -> None:
        subagent_id = str(subagent_id)
        job_id = str(fields.get("parent_job_id") or "").strip()
        if not job_id:
            logger.warning(
                "subagent ledger: no parent job id for %s — keeping no durable "
                "state for this child",
                fields.get("handle") or subagent_id,
            )
            self._failed.add(subagent_id)
            return
        # Optimistic: a create that times out client-side may still land, and
        # a later update against a row that never existed is a harmless no-op.
        self._rows[subagent_id] = subagent_id
        thread_id = await self.client.create_subagent_thread(
            job_id,
            subagent_id=subagent_id,
            handle=str(fields.get("handle") or ""),
            subagent_type=str(fields.get("subagent_type") or ""),
            parent_tool_call_id=fields.get("parent_tool_call_id") or None,
            parent_thread_id=fields.get("parent_thread_id") or None,
            isolation=str(fields.get("isolation") or "shared"),
            write_policy=str(fields.get("write_policy") or "none"),
            brief_description=str(fields.get("brief_description") or ""),
            parent_iteration=self._parent_iteration(fields),
            fork=bool(fields.get("fork", False)),
        )
        if thread_id is None:
            self._rows.pop(subagent_id, None)
            self._failed.add(subagent_id)
            logger.warning(
                "subagent ledger: the orchestrator did not create a thread row "
                "for %s (job %s) — no transcript will be kept for this child",
                fields.get("handle") or subagent_id,
                job_id,
            )
            return
        self._rows[subagent_id] = str(thread_id)

    async def persist_message(
        self, subagent_id: str, msg: Any, turn_number: int
    ) -> None:
        thread_id = self._rows.get(str(subagent_id))
        if thread_id is None:
            return
        row = _serialize_message_row(msg, int(turn_number or 0))
        await self.postgres.save_thread_message(thread_id=thread_id, **row)

    async def update(self, subagent_id: str, **fields: Any) -> None:
        thread_id = self._rows.get(str(subagent_id))
        if thread_id is None:
            return
        kwargs: Dict[str, Any] = {}
        status = fields.get("status")
        if status is not None:
            kind = str(status)
            terminal = is_terminal_status(kind)
            kwargs["subagent_status"] = kind
            kwargs["status"] = "ended" if terminal else "active"
            kwargs["ended"] = terminal
        for key in ("outcome", "report_path", "error"):
            value = fields.get(key)
            if value is not None:
                kwargs[key] = str(value)
        for key in ("turns", "tokens"):
            value = fields.get(key)
            if value is not None:
                try:
                    kwargs[key] = int(value)
                except (TypeError, ValueError):
                    continue
        if not kwargs:
            return
        await self.postgres.update_subagent_thread(thread_id, **kwargs)

    async def lookup(
        self, parent_job_id: str, parent_tool_call_id: str
    ) -> Optional[Dict[str, Any]]:
        row = await self.postgres.get_subagent_thread_by_call(
            str(parent_job_id), str(parent_tool_call_id)
        )
        if not row or not is_terminal_status(row.get("subagent_status")):
            return None
        return dict(row)


__all__ = ["DbSubagentLedger"]
