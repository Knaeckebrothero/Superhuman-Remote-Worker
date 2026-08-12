"""Tests for PersistentSession._cloud_overlay_monitor_loop (Task 11).

ENOTCONN watchdog: every probe interval, when the overlay is active, it runs
overlay.health_check() and heals via overlay.heal(remount_lower=...) on a
dead probe. The loop must never die from an exception (heal or health_check
failures are logged and retried on the next tick).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.api import persistent_session as ps_module
from src.api.persistent_session import PersistentSession


def _make_session(**overrides) -> PersistentSession:
    return PersistentSession(
        thread_id=overrides.pop("thread_id", str(uuid.uuid4())),
        config=overrides.pop("config", MagicMock()),
        **overrides,
    )


class FakeOverlay:
    def __init__(
        self, *, healthy: bool = True, active: bool = True, heal_raises: bool = False
    ):
        self.active = active
        self.healthy = healthy
        self.heal_raises = heal_raises
        self.health_check_calls = 0
        self.heal_calls = 0

    def health_check(self) -> bool:
        self.health_check_calls += 1
        return self.healthy

    def heal(self, remount_lower) -> None:
        self.heal_calls += 1
        if self.heal_raises:
            raise RuntimeError("heal boom")
        remount_lower()


class FakeRclone:
    def __init__(self) -> None:
        self.restart_calls: list[str] = []

    def restart_mount(self, mount_id: str) -> None:
        self.restart_calls.append(mount_id)


async def _run_loop_briefly(
    session: PersistentSession,
    *,
    monkeypatch: pytest.MonkeyPatch,
    ticks: float = 2.5,
    interval: float = 0.01,
) -> None:
    """Start the monitor loop with a tiny probe interval, let it tick a few
    times, then cancel it cleanly (mirrors this repo's other create_task +
    sleep + cancel loop tests, e.g. tests/test_leader_election.py)."""
    monkeypatch.setattr(ps_module, "_CLOUD_OVERLAY_MONITOR_INTERVAL_SECONDS", interval)
    task = asyncio.create_task(session._cloud_overlay_monitor_loop())
    await asyncio.sleep(interval * ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_monitor_heals_on_dead_probe(monkeypatch):
    overlay = FakeOverlay(healthy=False)
    rclone = FakeRclone()
    session = _make_session(
        overlay_mount_manager=overlay,
        cloud_mount_manager=rclone,
        _protected_mount_id="protected-t",
    )

    await _run_loop_briefly(session, monkeypatch=monkeypatch)

    assert overlay.health_check_calls >= 1
    assert overlay.heal_calls >= 1
    # heal's remount_lower callback must call restart_mount(<protected mount_id>)
    assert rclone.restart_calls and rclone.restart_calls[0] == "protected-t"


@pytest.mark.asyncio
async def test_monitor_noop_when_healthy(monkeypatch):
    overlay = FakeOverlay(healthy=True)
    rclone = FakeRclone()
    session = _make_session(
        overlay_mount_manager=overlay,
        cloud_mount_manager=rclone,
        _protected_mount_id="protected-t",
    )

    await _run_loop_briefly(session, monkeypatch=monkeypatch)

    assert overlay.health_check_calls >= 1
    assert overlay.heal_calls == 0
    assert rclone.restart_calls == []


@pytest.mark.asyncio
async def test_monitor_survives_heal_exception(monkeypatch):
    overlay = FakeOverlay(healthy=False, heal_raises=True)
    rclone = FakeRclone()
    session = _make_session(
        overlay_mount_manager=overlay,
        cloud_mount_manager=rclone,
        _protected_mount_id="protected-t",
    )

    # Enough ticks that the loop would have died on the first heal exception
    # if the try/except were missing or misplaced.
    await _run_loop_briefly(session, monkeypatch=monkeypatch, ticks=4.5)

    assert overlay.heal_calls >= 2, "loop must keep probing after a heal exception"


class _RaisingActiveOverlay:
    """Simulates a failure reading `.active` itself (not just health_check/
    heal) — proves the guard covers the whole per-tick body, not just the
    two calls the brief names explicitly."""

    def __init__(self) -> None:
        self.probe_attempts = 0

    @property
    def active(self) -> bool:
        self.probe_attempts += 1
        raise RuntimeError(".active boom")

    def health_check(self) -> bool:  # pragma: no cover - never reached
        raise AssertionError("should not be called when .active raises")

    def heal(self, remount_lower) -> None:  # pragma: no cover - never reached
        raise AssertionError("should not be called when .active raises")


@pytest.mark.asyncio
async def test_monitor_survives_overlay_active_property_exception(monkeypatch):
    overlay = _RaisingActiveOverlay()
    session = _make_session(
        overlay_mount_manager=overlay,
        cloud_mount_manager=FakeRclone(),
        _protected_mount_id="protected-t",
    )

    # Enough ticks that the loop would have died on the first .active read
    # if the guard didn't cover that access too.
    await _run_loop_briefly(session, monkeypatch=monkeypatch, ticks=4.5)

    assert overlay.probe_attempts >= 2, (
        "loop must keep ticking past a non-heal exception"
    )


@pytest.mark.asyncio
async def test_monitor_skips_probe_when_overlay_inactive(monkeypatch):
    overlay = FakeOverlay(healthy=False, active=False)
    rclone = FakeRclone()
    session = _make_session(
        overlay_mount_manager=overlay,
        cloud_mount_manager=rclone,
        _protected_mount_id="protected-t",
    )

    await _run_loop_briefly(session, monkeypatch=monkeypatch)

    assert overlay.health_check_calls == 0
    assert overlay.heal_calls == 0


@pytest.mark.asyncio
async def test_monitor_noop_when_overlay_manager_none(monkeypatch):
    session = _make_session(
        overlay_mount_manager=None, cloud_mount_manager=FakeRclone()
    )

    # Must not raise even though overlay_mount_manager is None.
    await _run_loop_briefly(session, monkeypatch=monkeypatch)


@pytest.mark.asyncio
async def test_handoff_retires_claim_resource_between_cancelled_heal_steps(
    monkeypatch,
):
    """Cancelling ``to_thread`` does not stop its worker thread.

    Pause a heal after its first remote mutation, detach the physical claim,
    then let the stale thread continue.  Handoff cleanup must retire the
    separate claim-resource admission before it returns, so neither the lower
    restart nor the final overlay remount can mutate the successor.
    """

    class FencedBackend:
        def __init__(self):
            self._lock = threading.RLock()
            self._retired = False
            self.successful_mutations: list[str] = []
            self.rejected = threading.Event()

        def exec_claim_resource(self, operation: str) -> None:
            with self._lock:
                if self._retired:
                    self.rejected.set()
                    raise RuntimeError("old claim-resource owner retired")
                self.successful_mutations.append(operation)

        def retire_claim_resource_owner(self) -> None:
            with self._lock:
                self._retired = True

        def retire_shell_owner(self) -> None:
            return None

        def retire(self) -> None:
            return None

    class PausedOverlay:
        def __init__(self, backend):
            self.backend = backend
            self.active = True
            self.between_steps = threading.Event()
            self.continue_heal = threading.Event()
            self.detached = False

        def health_check(self) -> bool:
            return False

        def heal(self, remount_lower) -> None:
            self.backend.exec_claim_resource("overlay_unmount")
            self.between_steps.set()
            assert self.continue_heal.wait(timeout=5)
            try:
                remount_lower()
            except RuntimeError:
                return
            self.backend.exec_claim_resource("overlay_remount")

        def detach_local(self) -> None:
            self.detached = True
            self.active = False

    class LocalCloudController:
        def __init__(self, backend):
            self.backend = backend
            self.restart_calls = 0
            self.detached = False

        def restart_mount(self, _mount_id: str) -> None:
            self.restart_calls += 1
            self.backend.exec_claim_resource("rclone_restart")

        async def detach_for_handoff(self) -> None:
            self.detached = True
            self.backend.retire_claim_resource_owner()

    backend = FencedBackend()
    overlay = PausedOverlay(backend)
    cloud = LocalCloudController(backend)
    session = _make_session(
        shell_owner_token=73,
        workspace_manager=SimpleNamespace(backend=backend),
        overlay_mount_manager=overlay,
        cloud_mount_manager=cloud,
        _protected_mount_id="protected-t",
    )
    monkeypatch.setattr(
        ps_module,
        "_CLOUD_OVERLAY_MONITOR_INTERVAL_SECONDS",
        0.001,
    )
    session._cloud_overlay_monitor_task = asyncio.create_task(
        session._cloud_overlay_monitor_loop()
    )
    assert await asyncio.to_thread(overlay.between_steps.wait, 2)

    await session.cleanup(
        preserve_shell=True,
        preserve_workspace_daemons=True,
    )

    assert overlay.detached is True
    assert cloud.detached is True
    overlay.continue_heal.set()
    assert await asyncio.to_thread(backend.rejected.wait, 2)
    assert cloud.restart_calls == 1
    assert backend.successful_mutations == ["overlay_unmount"]
