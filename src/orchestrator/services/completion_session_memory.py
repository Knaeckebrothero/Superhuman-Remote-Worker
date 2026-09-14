"""Application-owned session-memory completion effect composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SessionMemoryDependencies:
    """Collaborators used to resolve and drain immutable memory effects."""

    store: Any
    vector_store: Any
    authorize_thread_project_ids: Callable[..., Awaitable[Any]]
    resolve_session_config: Callable[..., Awaitable[Any]]


class SessionMemoryRuntime:
    """Own one application's lazy session-memory effect drain."""

    def __init__(self, dependencies: SessionMemoryDependencies) -> None:
        self.dependencies = dependencies
        self._drain: Any | None = None

    async def resolve_effect_config(
        self,
        thread: Mapping[str, Any],
        memory_scope_kind: str,
        memory_scope_id: UUID,
    ) -> Mapping[str, Any]:
        """Resolve fresh credentials without changing the captured destination."""

        scoped_thread = dict(thread)
        metadata = scoped_thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(
                    "session memory thread metadata is malformed"
                ) from exc
        if not isinstance(metadata, dict):
            raise RuntimeError("session memory thread metadata is not an object")

        if memory_scope_kind == "project":
            project_id = str(memory_scope_id)
            if await self.dependencies.store.get_project(project_id) is None:
                raise RuntimeError("captured session memory project no longer exists")
            owner_id = scoped_thread.get("user_id")
            if not owner_id:
                raise RuntimeError(
                    "project-scoped session memory requires an owning user"
                )
            owner = await self.dependencies.store.get_user(str(owner_id))
            if owner is None:
                raise RuntimeError("session memory thread owner no longer exists")
            await self.dependencies.authorize_thread_project_ids(owner, [project_id])
            scoped_thread["project_id"] = memory_scope_id
        elif memory_scope_kind != "thread":
            raise RuntimeError("unsupported session memory scope kind")

        status: dict[str, Any] = {}
        resolved = await self.dependencies.resolve_session_config(
            scoped_thread,
            metadata,
            status=status,
            resolve_base_when_experts_disabled=True,
        )
        if resolved is None:
            raise RuntimeError(
                "session memory config resolution failed "
                f"(state={status.get('state', 'unknown')})"
            )
        return resolved

    def drain(self) -> Any:
        """Build the always-on drain independently of job command mode."""

        if self._drain is None:
            from orchestrator.services.session_memory_effects import (
                SessionMemoryEffectDrain,
            )
            from orchestrator.services.session_memory_executor import (
                SessionMemoryEffectExecutor,
            )

            executor = SessionMemoryEffectExecutor(
                self.dependencies.store,
                self.dependencies.vector_store,
                self.resolve_effect_config,
            )
            self._drain = SessionMemoryEffectDrain(
                self.dependencies.store,
                executor,
            )
        return self._drain

    def reset(self) -> None:
        """Release the cached drain during application shutdown/tests."""

        self._drain = None


__all__ = ["SessionMemoryDependencies", "SessionMemoryRuntime"]
