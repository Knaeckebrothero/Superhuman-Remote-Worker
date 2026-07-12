"""Tests for PersistentSession._cloud_overlay_monitor_loop (Task 11).

ENOTCONN watchdog: every probe interval, when the overlay is active, it runs
overlay.health_check() and heals via overlay.heal(remount_lower=...) on a
dead probe. The loop must never die from an exception (heal or health_check
failures are logged and retried on the next tick).
"""

from __future__ import annotations

import asyncio
import uuid
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
