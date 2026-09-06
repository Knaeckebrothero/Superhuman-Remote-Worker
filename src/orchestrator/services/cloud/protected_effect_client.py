"""I/O owner for one attested protected Nextcloud effect request.

The backend transport knows how to fetch the installation's signed capability
and how to submit a signed request to the exact target path.  This executor
owns the database-clock bracket and commits the sealed pre-effect intent before
calling that transport.  Its ``finally`` block closes the captured horizon for
every ordinary success, failure, or cancellation; a process loss leaves a
durable ``planned`` row for the leader to close conservatively on restart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

from orchestrator.services.cloud.protected_effect_contract import (
    NextcloudEffectFenceIntent,
    NextcloudEffectHorizon,
    NextcloudEffectRequestAuthority,
    adopt_protected_effect_capability,
    sign_protected_effect_request,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)


class ProtectedEffectUnavailable(RuntimeError):
    """The remote installation cannot provide the signed bounded lane."""


@runtime_checkable
class ProtectedNextcloudEffectTransport(Protocol):
    backend_id: str

    @property
    def backend_instance_id(self) -> str | None: ...

    @property
    def protected_effect_config_sha256(self) -> str | None: ...

    @property
    def protected_effect_hmac_key(self) -> bytes | None: ...

    async def fetch_protected_effect_capability(
        self,
    ) -> tuple[Mapping[str, Any], str]: ...

    async def dispatch_protected_effect(
        self,
        intent: NextcloudEffectFenceIntent,
        *,
        body: bytes,
    ) -> httpx.Response: ...


class ProtectedNextcloudEffectExecutor:
    """Callable dispatch adapter scoped to one exact T/G/A grant plan."""

    __slots__ = ("_postgres_db", "_transport", "_thread_id", "_generation", "_plan")

    def __init__(
        self,
        *,
        postgres_db,
        transport: ProtectedNextcloudEffectTransport,
        thread_id: str,
        runtime_generation: str,
        plan: ProtectedNextcloudReaderGrantPlan,
    ) -> None:
        if (
            not isinstance(plan, ProtectedNextcloudReaderGrantPlan)
            or not isinstance(transport, ProtectedNextcloudEffectTransport)
            or transport.backend_id != "nextcloud"
            or transport.backend_instance_id != plan.backend_instance_id
        ):
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect transport authority is incomplete"
            )
        key = transport.protected_effect_hmac_key
        config_sha256 = transport.protected_effect_config_sha256
        if (
            type(key) is not bytes
            or len(key) < hashlib.sha256().digest_size
            or not isinstance(config_sha256, str)
            or len(config_sha256) != 64
        ):
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect capability is not configured"
            )
        self._postgres_db = postgres_db
        self._transport = transport
        self._thread_id = thread_id
        self._generation = runtime_generation
        self._plan = plan

    async def __call__(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> httpx.Response:
        key = self._transport.protected_effect_hmac_key
        config_sha256 = self._transport.protected_effect_config_sha256
        if type(key) is not bytes or not isinstance(config_sha256, str):
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect capability disappeared"
            )

        db_before = await self._postgres_db.get_protected_effect_database_time()
        (
            binding,
            capability_signature,
        ) = await self._transport.fetch_protected_effect_capability()
        db_after = await self._postgres_db.get_protected_effect_database_time()
        capability = adopt_protected_effect_capability(
            binding,
            signature=capability_signature,
            key=key,
            db_before=db_before,
            db_after=db_after,
            expected_backend_instance_id=self._plan.backend_instance_id,
            expected_config_sha256=config_sha256,
        )
        if capability is None:
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect capability is invalid or stale"
            )

        db_dispatched_at = await self._postgres_db.get_protected_effect_database_time()
        try:
            effect_not_after = db_dispatched_at + timedelta(
                seconds=capability.capability.queue_bound_seconds
            )
        except OverflowError as exc:
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect deadline is outside UTC range"
            ) from exc
        request = NextcloudEffectRequestAuthority(
            backend_instance_id=self._plan.backend_instance_id,
            config_sha256=config_sha256,
            engage_attempt=self._plan.engage_attempt,
            method=method,
            path=path,
            body_sha256=hashlib.sha256(body).hexdigest(),
            effect_not_after=effect_not_after,
        )
        request_signature = sign_protected_effect_request(request, key=key)
        intent = NextcloudEffectFenceIntent.capture(
            capability=capability,
            request=request,
            request_signature=request_signature,
            key=key,
            db_dispatched_at=db_dispatched_at,
        )
        intent_id = await self._postgres_db.install_cloud_ro_effect_intent(
            thread_id=self._thread_id,
            expected_runtime_generation=self._generation,
            expected_engage_attempt=self._plan.engage_attempt,
            intent=intent,
        )
        if intent_id is None:
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect intent lost lifecycle authority"
            )

        response: httpx.Response | None = None
        dispatch_error: BaseException | None = None
        try:
            response = await self._transport.dispatch_protected_effect(
                intent,
                body=body,
            )
        except BaseException as exc:
            dispatch_error = exc
        finally:
            db_closed_at = await self._postgres_db.get_protected_effect_database_time()
            horizon = NextcloudEffectHorizon.capture(
                intent=intent,
                db_dispatch_closed_at=db_closed_at,
            )
            if not await self._postgres_db.close_cloud_ro_effect_intent(
                intent_id,
                expected_thread_id=self._thread_id,
                expected_runtime_generation=self._generation,
                expected_engage_attempt=self._plan.engage_attempt,
                horizon=horizon,
            ):
                raise ProtectedEffectUnavailable(
                    "protected Nextcloud effect horizon was not recorded"
                )

        if dispatch_error is not None:
            raise dispatch_error
        if not isinstance(response, httpx.Response):
            raise ProtectedEffectUnavailable(
                "protected Nextcloud effect returned no response"
            )
        return response


__all__ = [
    "ProtectedEffectUnavailable",
    "ProtectedNextcloudEffectExecutor",
    "ProtectedNextcloudEffectTransport",
]
