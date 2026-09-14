"""Isolated SSH-access dependencies for router and service cases.

Mirrors the application's own composition — a store, the session secret, the
notifier, the logger, ONE host-key cache and the injected VM-tier predicate —
so a case drives the real router and the real service without an application
lifespan, a database pool, or the container/VM provisioner import chain.

``dependencies`` is a property, not a stored value: it rebuilds on every read,
exactly as ``main``'s factory does per invocation. That is what lets a case
flip ``secret`` (or the store, or a gate) mid-test and have the next call see
it, and it is also the behavior being characterized — a factory that captured
its collaborators once would bind ``None`` for anything ``lifespan`` assigns.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.routers import ssh_access as ssh_access_routes
from orchestrator.security.access import require_personal_scope
from orchestrator.services import ssh_access as ssh_access_operations

#: Non-empty by default. Cases that want the fail-closed (503 / False)
#: behavior set ``harness.secret = ""``.
SECRET = "test-only-ssh-challenge-secret"


class FakeStore:
    """Async store whose unset methods fail loudly instead of reaching a pool."""

    def __init__(self, **methods: Any) -> None:
        for name, method in methods.items():
            setattr(self, name, method)

    def set(self, name: str, method: Any) -> "FakeStore":
        setattr(self, name, method)
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def _unexpected(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"unexpected store call: {name}")

        return _unexpected


class RecordingNotifier:
    """Captures ``record`` kwargs so a case can assert on one notification."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _refuse_vm_tier(metadata, ws_ctx, vm_ctx):  # pragma: no cover - tripwire
    raise AssertionError("this case did not wire thread_is_vm_tier")


class SshAccessHarness:
    """One application's worth of SSH-access collaborators, all overridable."""

    def __init__(self, **store_methods: Any) -> None:
        self.store = FakeStore(**store_methods)
        self.notifier = RecordingNotifier()
        self.logger = logging.getLogger("tests.ssh_access")
        self.host_keys = ssh_access_operations.SshGatewayHostKeyCache(
            logger=self.logger
        )
        self.secret = SECRET
        self.user: dict[str, Any] | None = None
        self.thread_is_vm_tier = _refuse_vm_tier
        self.require_approved_user = self._approved_user
        self.require_internal = self._internal
        self.require_personal_scope = require_personal_scope
        self.user_can_access_ide_entity = self._ide_access

    async def _approved_user(self, request, db):
        if self.user is None:
            raise AssertionError("this case did not wire an authenticated user")
        return self.user

    async def _internal(self, request):
        return None

    async def _ide_access(self, user, db, entity_id):  # pragma: no cover - tripwire
        raise AssertionError("this case did not wire user_can_access_ide_entity")

    @property
    def operations(self) -> ssh_access_operations.SshAccessDependencies:
        return ssh_access_operations.SshAccessDependencies(
            store=self.store,
            session_jwt_secret=self.secret,
            notifier=self.notifier,
            logger=self.logger,
            host_keys=self.host_keys,
            thread_is_vm_tier=self.thread_is_vm_tier,
        )

    @property
    def dependencies(self) -> ssh_access_routes.SshAccessDependencies:
        return ssh_access_routes.SshAccessDependencies(
            store=self.store,
            operations=self.operations,
            require_approved_user=self.require_approved_user,
            require_internal=self.require_internal,
            require_personal_scope=self.require_personal_scope,
            user_can_access_ide_entity=self.user_can_access_ide_entity,
        )
