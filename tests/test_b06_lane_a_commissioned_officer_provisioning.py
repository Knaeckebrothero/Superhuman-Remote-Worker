"""Commissioned-Officer provisioning and its failure record.

R1.B06 lane A. ``emit_session_provisioning_failure`` had no direct coverage
before the extraction; its "never speak for a generation that moved on" rule
and its swallow-the-publish-error guard are characterized here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.commissioned_officer_provisioning import (
    CommissionedOfficerDependencies,
    emit_session_provisioning_failure,
    provision_commissioned_officer,
)
from orchestrator.services.session_runtime_admission import ThreadRuntimeAuthority

THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
GENERATION = "11111111-1111-4111-8111-111111111111"
OTHER_GENERATION = "22222222-2222-4222-8222-222222222222"
USER_ID = "user-a"


def _authority(generation=GENERATION):
    return ThreadRuntimeAuthority(thread_id=THREAD_ID, generation=generation)


def _thread(generation=GENERATION):
    return {
        "id": THREAD_ID,
        "status": "created",
        "runtime_generation": generation,
        "runtime_retirement_token": None,
    }


def _deps(*, store=None, provisioner=None, emit=None):
    return CommissionedOfficerDependencies(
        store=store or SimpleNamespace(get_thread=AsyncMock(return_value=_thread())),
        persistent_provisioner=provisioner or MagicMock(),
        emit_session_provisioning_failure=emit or AsyncMock(),
    )


class _Recorder:
    """Stand-in for ``session_lifecycle.emit`` that keeps every call."""

    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, user_id, thread_id, state, **kwargs):
        self.events.append((user_id, thread_id, state, kwargs))


class TestEmitSessionProvisioningFailure:
    @pytest.mark.asyncio
    async def test_a_live_generation_is_recorded_with_its_generation(self):
        recorder = _Recorder()
        with patch("orchestrator.services.session_lifecycle.emit", recorder):
            await emit_session_provisioning_failure(
                THREAD_ID,
                USER_ID,
                _authority(),
                "pvc_creation_failed",
                dependencies=_deps(),
            )
        assert recorder.events == [
            (
                USER_ID,
                THREAD_ID,
                "failed",
                {
                    "reason": "pvc_creation_failed",
                    "session_runtime_generation": GENERATION,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_generation_that_moved_on_is_never_spoken_for(self):
        recorder = _Recorder()
        store = SimpleNamespace(
            get_thread=AsyncMock(return_value=_thread(OTHER_GENERATION))
        )
        with patch("orchestrator.services.session_lifecycle.emit", recorder):
            await emit_session_provisioning_failure(
                THREAD_ID,
                USER_ID,
                _authority(),
                "boom",
                dependencies=_deps(store=store),
            )
        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_a_deleted_row_is_never_spoken_for(self):
        recorder = _Recorder()
        store = SimpleNamespace(get_thread=AsyncMock(return_value=None))
        with patch("orchestrator.services.session_lifecycle.emit", recorder):
            await emit_session_provisioning_failure(
                THREAD_ID,
                USER_ID,
                _authority(),
                "boom",
                dependencies=_deps(store=store),
            )
        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_no_captured_authority_emits_ungenerationed(self):
        recorder = _Recorder()
        store = SimpleNamespace(get_thread=AsyncMock())
        with patch("orchestrator.services.session_lifecycle.emit", recorder):
            await emit_session_provisioning_failure(
                THREAD_ID, USER_ID, None, "boom", dependencies=_deps(store=store)
            )
        assert recorder.events == [(USER_ID, THREAD_ID, "failed", {"reason": "boom"})]
        store.get_thread.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("user_id", [None, ""])
    async def test_an_ownerless_thread_has_no_feed_to_record_on(self, user_id):
        recorder = _Recorder()
        store = SimpleNamespace(get_thread=AsyncMock())
        with patch("orchestrator.services.session_lifecycle.emit", recorder):
            await emit_session_provisioning_failure(
                THREAD_ID,
                user_id,
                _authority(),
                "boom",
                dependencies=_deps(store=store),
            )
        assert recorder.events == []
        store.get_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_publish_failure_is_swallowed_not_raised_into_the_task(self):
        store = SimpleNamespace(get_thread=AsyncMock(side_effect=RuntimeError("down")))
        await emit_session_provisioning_failure(
            THREAD_ID, USER_ID, _authority(), "boom", dependencies=_deps(store=store)
        )


class TestProvisionCommissionedOfficer:
    @pytest.mark.asyncio
    async def test_a_usable_pod_records_nothing(self):
        provisioner = MagicMock()
        provisioner.create_agent_pod = AsyncMock(
            return_value=SimpleNamespace(usable=True)
        )
        emit = AsyncMock()
        await provision_commissioned_officer(
            THREAD_ID,
            user_id=USER_ID,
            config_name="centurion",
            runtime_authority=_authority(),
            dependencies=_deps(provisioner=provisioner, emit=emit),
        )
        provisioner.create_agent_pod.assert_awaited_once_with(
            THREAD_ID,
            config_name="centurion",
            expected_runtime_generation=GENERATION,
        )
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unusable_result_records_status_and_failure_class(self):
        provisioner = MagicMock()
        provisioner.create_agent_pod = AsyncMock(
            return_value=SimpleNamespace(
                usable=False,
                status=SimpleNamespace(value="failed"),
                failure_class="pvc_creation_failed",
            )
        )
        emit = AsyncMock()
        await provision_commissioned_officer(
            THREAD_ID,
            user_id=USER_ID,
            config_name="centurion",
            runtime_authority=_authority(),
            dependencies=_deps(provisioner=provisioner, emit=emit),
        )
        reason = emit.await_args.args[3]
        assert "failed" in reason and "pvc_creation_failed" in reason

    @pytest.mark.asyncio
    async def test_a_missing_failure_class_still_records_a_reason(self):
        provisioner = MagicMock()
        provisioner.create_agent_pod = AsyncMock(
            return_value=SimpleNamespace(
                usable=False,
                status=SimpleNamespace(value="timeout"),
                failure_class=None,
            )
        )
        emit = AsyncMock()
        await provision_commissioned_officer(
            THREAD_ID,
            user_id=USER_ID,
            config_name="centurion",
            runtime_authority=_authority(),
            dependencies=_deps(provisioner=provisioner, emit=emit),
        )
        assert "no-detail" in emit.await_args.args[3]

    @pytest.mark.asyncio
    async def test_a_refused_config_name_is_recorded_not_swallowed(self):
        provisioner = MagicMock()
        provisioner.create_agent_pod = AsyncMock(
            side_effect=ValueError("invalid config_name 'a; id'")
        )
        emit = AsyncMock()
        await provision_commissioned_officer(
            THREAD_ID,
            user_id=USER_ID,
            config_name="a; id",
            runtime_authority=_authority(),
            dependencies=_deps(provisioner=provisioner, emit=emit),
        )
        emit.assert_awaited_once()
        assert "config_name" in emit.await_args.args[3]
        assert emit.await_args.args[:3] == (THREAD_ID, USER_ID, _authority())

    @pytest.mark.asyncio
    async def test_the_guard_covers_the_unusable_branch_too(self):
        """An emit that itself raises must not escape the task."""
        provisioner = MagicMock()
        provisioner.create_agent_pod = AsyncMock(
            return_value=SimpleNamespace(
                usable=False,
                status=SimpleNamespace(value="failed"),
                failure_class="x",
            )
        )
        emit = AsyncMock(side_effect=[RuntimeError("feed down"), None])
        await provision_commissioned_officer(
            THREAD_ID,
            user_id=USER_ID,
            config_name="centurion",
            runtime_authority=_authority(),
            dependencies=_deps(provisioner=provisioner, emit=emit),
        )
        assert emit.await_count == 2
