"""Ownership of the request-spawned KB datasource reindex tasks.

One app-owned registry replaces ``main``'s two module globals: the set that
kept every scheduled rebuild strongly referenced until shutdown, and the
per-datasource index that lets a delete cancel and await only the source being
removed. Both structures survive here as internals, because they answer
different questions — "what must shutdown drain?" and "what must this delete
fence against?" — and collapsing them into one would lose the second.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class KbDatasourceTaskRegistry:
    """Track best-effort KB datasource reindexes for cancellation and drain."""

    def __init__(self) -> None:
        # Keep best-effort initial/update KB datasource reindexes strongly
        # referenced until completion. Failures are recorded in the vector
        # watermark and never roll back datasource CRUD.
        self._tasks: set[asyncio.Task] = set()
        # Per-datasource view of the same tasks lets deletion cancel and await
        # only the source being removed. The global set remains the shutdown
        # ownership set.
        self._tasks_by_id: dict[str, set[asyncio.Task]] = {}

    def schedule(
        self,
        datasource_id: str,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task:
        """Spawn ``coro`` and register it under both views until it finishes."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        datasource_tasks = self._tasks_by_id.setdefault(datasource_id, set())
        datasource_tasks.add(task)

        def forget_task(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            current = self._tasks_by_id.get(datasource_id)
            if current is None:
                return
            current.discard(done)
            if not current:
                self._tasks_by_id.pop(datasource_id, None)

        task.add_done_callback(forget_task)
        return task

    async def cancel_for_datasource(self, datasource_id: str) -> None:
        """Cancel and drain request-spawned rebuilds for one external source.

        ``asyncio.current_task()`` is excluded deliberately: the delete path
        calls this from inside a request task that may itself be registered,
        and cancelling yourself here would abort the delete instead of the
        writer it is fencing against.
        """
        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._tasks_by_id.get(datasource_id, ())
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def pending(self) -> list[asyncio.Task]:
        """Snapshot of every registered task, in shutdown ownership terms."""
        return list(self._tasks)

    async def drain(self) -> None:
        """Cancel and await every registered rebuild.

        Initial/manual datasource reindexes are request-spawned rather than
        loop tasks. Cancel them before closing git/vector clients; the source
        context removes temporary repositories and auth material in its
        cancellation path.
        """
        pending_kb_tasks = self.pending()
        for task in pending_kb_tasks:
            task.cancel()
        if pending_kb_tasks:
            await asyncio.gather(*pending_kb_tasks, return_exceptions=True)
