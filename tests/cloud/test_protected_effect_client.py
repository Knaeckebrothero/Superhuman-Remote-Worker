from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from orchestrator.services.cloud.protected_effect_client import (
    ProtectedEffectUnavailable,
    ProtectedNextcloudEffectExecutor,
)
from orchestrator.services.cloud.protected_effect_contract import (
    NextcloudEffectCapability,
    NextcloudEffectFenceIntent,
    NextcloudEffectHorizon,
    sign_protected_effect_capability,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)


KEY = b"k" * 32
INSTANCE = "99999999-9999-4999-8999-999999999999"
THREAD = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"
ATTEMPT = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
CONFIG_SHA = "a" * 64
START = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _plan() -> ProtectedNextcloudReaderGrantPlan:
    return ProtectedNextcloudReaderGrantPlan(
        engage_attempt=ATTEMPT,
        backend_instance_id=INSTANCE,
        source=ProtectedMountSourceIdentity(
            backend_instance_id=INSTANCE,
            source_ref=PROJECT,
            target_path="cloud",
            native_id="7",
            mountpoint="Project",
        ),
    )


def _capability() -> NextcloudEffectCapability:
    return NextcloudEffectCapability(
        backend_instance_id=INSTANCE,
        config_sha256=CONFIG_SHA,
        queue_bound_seconds=30,
        handler_bound_seconds=10,
        clock_skew_bound_seconds=2,
        safety_margin_seconds=5,
        capability_max_age_seconds=5,
        server_time=START,
    )


class _DB:
    def __init__(self) -> None:
        self.times = iter(
            (
                START,
                START + timedelta(milliseconds=100),
                START + timedelta(milliseconds=200),
                START + timedelta(milliseconds=300),
            )
        )
        self.calls: list[tuple[str, Any]] = []
        self.intent: NextcloudEffectFenceIntent | None = None
        self.horizon: NextcloudEffectHorizon | None = None
        self.close_result = True

    async def get_protected_effect_database_time(self) -> datetime:
        return next(self.times)

    async def install_cloud_ro_effect_intent(self, **kwargs: Any) -> str:
        self.calls.append(("install", kwargs))
        self.intent = kwargs["intent"]
        return "55555555-5555-4555-8555-555555555555"

    async def close_cloud_ro_effect_intent(
        self, _intent_id: str, **kwargs: Any
    ) -> bool:
        self.calls.append(("close", kwargs))
        self.horizon = kwargs["horizon"]
        return self.close_result


class _Transport:
    backend_id = "nextcloud"
    backend_instance_id = INSTANCE
    protected_effect_config_sha256 = CONFIG_SHA
    protected_effect_hmac_key = KEY

    def __init__(self, db: _DB) -> None:
        self.db = db
        self.calls: list[tuple[str, Any]] = []
        self.error: BaseException | None = None

    async def fetch_protected_effect_capability(self):
        capability = _capability()
        self.calls.append(("capability", None))
        return capability.binding, sign_protected_effect_capability(
            capability,
            key=KEY,
        )

    async def dispatch_protected_effect(
        self,
        intent: NextcloudEffectFenceIntent,
        *,
        body: bytes,
    ) -> httpx.Response:
        assert self.db.intent is intent
        self.calls.append(("dispatch", (intent, body)))
        if self.error is not None:
            raise self.error
        return httpx.Response(
            200,
            json={"ocs": {"meta": {"status": "ok", "statuscode": 100}}},
        )


def _executor(db: _DB, transport: _Transport) -> ProtectedNextcloudEffectExecutor:
    return ProtectedNextcloudEffectExecutor(
        postgres_db=db,
        transport=transport,
        thread_id=THREAD,
        runtime_generation=GENERATION,
        plan=_plan(),
    )


@pytest.mark.asyncio
async def test_effect_commits_intent_before_dispatch_and_closes_exact_horizon() -> None:
    db = _DB()
    transport = _Transport(db)
    executor = _executor(db, transport)

    response = await executor("POST", "/ocs/v2.php/cloud/users", b"userid=reader")

    assert response.status_code == 200
    assert [name for name, _value in db.calls] == ["install", "close"]
    assert [name for name, _value in transport.calls] == ["capability", "dispatch"]
    assert db.intent is not None and db.horizon is not None
    assert db.horizon.intent is db.intent
    assert db.intent.request.engage_attempt == ATTEMPT
    assert db.intent.request.path == "/ocs/v2.php/cloud/users"
    assert db.horizon.safe_after > db.horizon.dispatch_closed_at


@pytest.mark.asyncio
async def test_dispatch_error_still_records_the_immutable_horizon() -> None:
    db = _DB()
    transport = _Transport(db)
    transport.error = RuntimeError("response lost")

    with pytest.raises(RuntimeError, match="response lost"):
        await _executor(db, transport)(
            "POST",
            "/ocs/v2.php/cloud/users",
            b"userid=reader",
        )

    assert db.intent is not None
    assert db.horizon is not None
    assert db.horizon.intent is db.intent


@pytest.mark.asyncio
async def test_horizon_publication_failure_never_returns_remote_success() -> None:
    db = _DB()
    db.close_result = False
    transport = _Transport(db)

    with pytest.raises(ProtectedEffectUnavailable, match="horizon was not recorded"):
        await _executor(db, transport)(
            "POST",
            "/ocs/v2.php/cloud/users",
            b"userid=reader",
        )


def test_executor_refuses_missing_hmac_or_configuration_before_io() -> None:
    db = _DB()
    transport = _Transport(db)
    transport.protected_effect_hmac_key = None

    with pytest.raises(ProtectedEffectUnavailable, match="not configured"):
        _executor(db, transport)


@pytest.mark.asyncio
async def test_invalid_capability_never_persists_or_dispatches() -> None:
    db = _DB()
    transport = _Transport(db)

    async def _bad_capability():
        capability = _capability()
        return capability.binding, "0" * 64

    transport.fetch_protected_effect_capability = _bad_capability  # type: ignore[method-assign]

    with pytest.raises(ProtectedEffectUnavailable, match="invalid or stale"):
        await _executor(db, transport)(
            "POST",
            "/ocs/v2.php/cloud/users",
            b"userid=reader",
        )

    assert db.calls == []
    assert transport.calls == []
