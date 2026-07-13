"""Focused shared SSH transport contracts for Dynamic Canvas Slice 3A."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from services import canvas_ssh
from services.canvas_ssh import (
    CANVAS_LOOPBACK_HOST,
    CanvasDirectChannelUnavailable,
    CanvasSSHError,
    PinnedSSHTransportPool,
    RemoteWorkspaceTarget,
)

THREAD_ID = "a3333333-3333-3333-3333-333333333333"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NEXT_GENERATION = UUID("22222222-bbbb-4bbb-8bbb-222222222222")


def _thread(
    *,
    generation: UUID = GENERATION,
    host: str = "workspace.test",
    fingerprint: str = "SHA256:test",
) -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "metadata": {
            "_workspace_binding": {
                "generation": str(generation),
                "kind": "remote",
                "backing_id": "workspace-a",
                "ssh_host_key_fingerprint": fingerprint,
            },
            "workspace_container": {
                "status": "ready",
                "host": host,
                "port": 30022,
                "_canvas_workspace_generation": str(generation),
            },
        },
    }


class _Writer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.remote_closed = False
        self.close_calls = 0
        self.waited = False
        self.open_error: BaseException | None = None
        self.open_calls: list[tuple[str, int]] = []
        self.writers: list[_Writer] = []

    def is_closed(self) -> bool:
        return self.closed or self.remote_closed

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True

    async def open_connection(self, host: str, port: int):
        self.open_calls.append((host, port))
        if self.open_error is not None:
            raise self.open_error
        writer = _Writer()
        self.writers.append(writer)
        return object(), writer


def _target(generation: UUID = GENERATION) -> RemoteWorkspaceTarget:
    return RemoteWorkspaceTarget(
        thread_id=THREAD_ID,
        generation=generation,
        host="workspace.test",
        port=30022,
        fingerprint="SHA256:test",
    )


@pytest.mark.asyncio
async def test_direct_channel_uses_exact_pin_and_fixed_loopback(monkeypatch) -> None:
    if canvas_ssh.asyncssh is None:
        pytest.skip("asyncssh is unavailable")
    captured: dict[str, Any] = {}
    connection = _Connection()

    async def connect(host: str, **kwargs: Any):
        captured.update(host=host, **kwargs)
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)

    async def current() -> dict[str, Any]:
        return _thread()

    async with pool.open_loopback_connection(
        target=_target(),
        destination_port=8501,
        key_path="/tmp/id_ed25519",
        generation_resolver=current,
    ):
        pass

    assert captured["host"] == "workspace.test"
    assert captured["known_hosts"] == ((), (), (), (), (), (), ())
    assert captured["known_hosts"]
    assert captured["server_host_key_algs"] == ["ssh-ed25519"]
    assert connection.open_calls == [(CANVAS_LOOPBACK_HOST, 8501)]
    assert connection.writers[0].closed is True
    assert connection.writers[0].waited is True
    await pool.close_all()


@pytest.mark.asyncio
async def test_cold_concurrent_checkouts_singleflight_one_handshake(
    monkeypatch,
) -> None:
    connection = _Connection()
    connect_calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def connect(*args: Any, **kwargs: Any):
        nonlocal connect_calls
        del args, kwargs
        connect_calls += 1
        started.set()
        await release.wait()
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)

    async def use_transport() -> None:
        async with pool.checkout(target=_target(), key_path="/tmp/key") as current:
            assert current is connection

    tasks = [asyncio.create_task(use_transport()) for _ in range(10)]
    await started.wait()
    release.set()
    await asyncio.gather(*tasks)

    assert connect_calls == 1
    await pool.close_all()


@pytest.mark.asyncio
async def test_cancelled_candidate_before_insertion_is_closed(monkeypatch) -> None:
    connection = _Connection()
    started = asyncio.Event()
    release_connect = asyncio.Event()

    async def connect(*args: Any, **kwargs: Any):
        del args, kwargs
        started.set()
        await release_connect.wait()
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)

    async def use_transport() -> None:
        async with pool.checkout(target=_target(), key_path="/tmp/key"):
            pass

    task = asyncio.create_task(use_transport())
    await started.wait()
    await pool._lock.acquire()
    release_connect.set()
    await asyncio.sleep(0)
    task.cancel()
    pool._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.closed is True
    assert pool._entries == {}


@pytest.mark.asyncio
async def test_failed_reconnect_closes_removed_stale_entry(monkeypatch) -> None:
    connection = _Connection()
    calls = 0

    async def connect(*args: Any, **kwargs: Any):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return connection
        raise OSError("unreachable")

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)
    async with pool.checkout(target=_target(), key_path="/tmp/key"):
        pass
    connection.remote_closed = True

    with pytest.raises(CanvasSSHError) as error:
        async with pool.checkout(target=_target(), key_path="/tmp/key"):
            pass

    assert error.value.code == "workspace_unavailable"
    assert connection.close_calls == 1
    assert connection.waited is True


@pytest.mark.asyncio
async def test_domain_error_does_not_poison_concurrent_transport(monkeypatch) -> None:
    connection = _Connection()
    connect_calls = 0

    async def connect(*args: Any, **kwargs: Any):
        nonlocal connect_calls
        del args, kwargs
        connect_calls += 1
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)
    first = pool.checkout(target=_target(), key_path="/tmp/key")
    await first.__aenter__()

    with pytest.raises(ValueError, match="domain failure"):
        async with pool.checkout(target=_target(), key_path="/tmp/key"):
            raise ValueError("domain failure")

    assert connection.closed is False
    async with pool.checkout(target=_target(), key_path="/tmp/key") as reused:
        assert reused is connection
    assert connect_calls == 1
    await first.__aexit__(None, None, None)
    await pool.close_all()


@pytest.mark.asyncio
async def test_only_open_connect_failed_reports_starting(monkeypatch) -> None:
    if canvas_ssh.asyncssh is None:
        pytest.skip("asyncssh is unavailable")
    connection = _Connection()

    async def connect(*args: Any, **kwargs: Any):
        del args, kwargs
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)

    async def current() -> dict[str, Any]:
        return _thread()

    connection.open_error = canvas_ssh.asyncssh.ChannelOpenError(
        canvas_ssh.asyncssh.OPEN_CONNECT_FAILED, "closed"
    )
    with pytest.raises(CanvasDirectChannelUnavailable):
        async with pool.open_loopback_connection(
            target=_target(),
            destination_port=8501,
            key_path="/tmp/key",
            generation_resolver=current,
        ):
            pass

    connection.open_error = canvas_ssh.asyncssh.ChannelOpenError(
        canvas_ssh.asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, "denied"
    )
    with pytest.raises(CanvasSSHError) as denied:
        async with pool.open_loopback_connection(
            target=_target(),
            destination_port=8501,
            key_path="/tmp/key",
            generation_resolver=current,
        ):
            pass
    assert denied.value.code == "workspace_unavailable"

    connection.open_error = OSError("transport broke")
    with pytest.raises(CanvasSSHError) as transport:
        async with pool.open_loopback_connection(
            target=_target(),
            destination_port=8501,
            key_path="/tmp/key",
            generation_resolver=current,
        ):
            pass
    assert transport.value.code == "workspace_unavailable"
    assert connection.closed is True
    await pool.close_all()


@pytest.mark.asyncio
async def test_post_open_identity_failure_always_closes_writer(monkeypatch) -> None:
    connection = _Connection()

    async def connect(*args: Any, **kwargs: Any):
        del args, kwargs
        return connection

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=60)
    calls = 0

    async def rotated() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _thread() if calls == 1 else _thread(generation=NEXT_GENERATION)

    with pytest.raises(CanvasSSHError) as error:
        async with pool.open_loopback_connection(
            target=_target(),
            destination_port=8501,
            key_path="/tmp/key",
            generation_resolver=rotated,
        ):
            pass

    assert error.value.code == "workspace_generation_changed"
    assert connection.writers[0].closed is True
    assert connection.writers[0].waited is True
    await pool.close_all()


@pytest.mark.asyncio
async def test_idle_and_generation_eviction_close_only_intended_entries(
    monkeypatch,
) -> None:
    connections = [_Connection(), _Connection(), _Connection()]

    async def connect(*args: Any, **kwargs: Any):
        del args, kwargs
        return connections.pop(0)

    monkeypatch.setattr(canvas_ssh.asyncssh, "connect", connect)
    pool = PinnedSSHTransportPool(idle_timeout=0.01)
    old = pool.checkout(target=_target(), key_path="/tmp/key")
    new = pool.checkout(target=_target(NEXT_GENERATION), key_path="/tmp/key")
    old_connection = await old.__aenter__()
    new_connection = await new.__aenter__()

    await pool.evict_generation(THREAD_ID, GENERATION)
    assert old_connection.closed is True
    assert new_connection.closed is False
    await old.__aexit__(None, None, None)
    await new.__aexit__(None, None, None)
    await asyncio.sleep(0.03)
    assert new_connection.closed is True

    thread_pool = PinnedSSHTransportPool()
    assert thread_pool._idle_timeout == 60.0
    active = thread_pool.checkout(target=_target(), key_path="/tmp/key")
    active_connection = await active.__aenter__()
    await thread_pool.evict_thread(THREAD_ID)
    assert active_connection.closed is True
    await active.__aexit__(None, None, None)
