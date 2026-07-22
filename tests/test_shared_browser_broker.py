"""Broker helper tests: codec mirror, readiness, and SSH resolution."""

import asyncio
from contextlib import asynccontextmanager
import logging
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services import browser_stream_broker as broker
from services.canvas_ssh import CanvasSSHError, PinnedSSHCommandResult


_WORKSPACE_GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")


def _bound_thread(*, generation: UUID = _WORKSPACE_GENERATION) -> dict:
    return {
        "id": "t1",
        "user_id": "u1",
        "metadata": {
            "_workspace_binding": {
                "generation": str(generation),
                "kind": "remote",
                "backing_id": "workspace-a",
                "ssh_host_key_fingerprint": "SHA256:test",
            },
            "workspace_container": {
                "status": "ready",
                "ssh_host": "workspace.test",
                "ssh_port": 30022,
                "_canvas_workspace_generation": str(generation),
            },
        },
    }


class TestCodecMirror:
    def test_roundtrip(self):
        async def run():
            wire = broker.encode_stream_frame(broker.T_STATE, b'{"a":1}')
            reader = asyncio.StreamReader()
            reader.feed_data(wire)
            reader.feed_eof()
            return await broker.read_stream_frame(reader)

        frame_type, payload = asyncio.run(run())
        assert (frame_type, payload) == (broker.T_STATE, b'{"a":1}')

    def test_length_covers_type_byte(self):
        wire = broker.encode_stream_frame(broker.T_HELLO, b"abc")
        assert wire[:4] == (4).to_bytes(4, "big")
        assert wire[4] == broker.T_HELLO


class TestWorkspaceResolution:
    def test_ready_container(self):
        thread = {
            "metadata": {
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "10.1.2.3",
                    "ssh_port": 2222,
                }
            }
        }
        assert broker.workspace_ready(thread) is True

    def test_not_ready(self):
        assert broker.workspace_ready({"metadata": {}}) is False


class TestExecStreamInfo:
    def test_uses_pinned_pool_and_parses_last_stdout_line(self, monkeypatch):
        captured = {}

        async def run_command(**kwargs):
            captured.update(kwargs)
            return PinnedSSHCommandResult(
                exit_status=0,
                stdout=(
                    b'[browser-exec] noise\n{"generation": "g1", '
                    b'"token": "t", "port": 38801, "baton": "user"}\n'
                ),
                stderr=b"",
            )

        async def forbidden_exec(*args, **kwargs):
            raise AssertionError((args, kwargs))

        monkeypatch.setattr(
            broker.PINNED_SSH_TRANSPORT_POOL, "run_command", run_command
        )
        monkeypatch.setattr(broker, "build_agent_ssh_cmd", forbidden_exec, raising=False)
        monkeypatch.setattr(broker.asyncio, "create_subprocess_exec", forbidden_exec)
        monkeypatch.setattr(broker, "resolve_ssh_key_path", lambda: "/tmp/key")
        monkeypatch.setattr(broker, "orchestrator_can_reach", lambda host: True)
        thread = _bound_thread()

        async def current():
            return thread

        info = asyncio.run(
            broker.exec_stream_info(
                thread,
                initial_baton="user",
                generation_resolver=current,
            )
        )

        assert info["generation"] == "g1"
        assert captured["target"].generation == _WORKSPACE_GENERATION
        assert captured["key_path"] == "/tmp/key"
        assert captured["command"] == (
            "browser-exec stream_info --json '{\"initial_baton\":\"user\"}'"
        )
        assert captured["generation_resolver"] is current
        assert captured["max_output_bytes"] == 64 * 1024

    def test_generation_change_maps_to_typed_unavailable(self, monkeypatch):
        async def run_command(**kwargs):
            await kwargs["generation_resolver"]()
            raise CanvasSSHError(
                409,
                "workspace_generation_changed",
                "sentinel private endpoint",
            )

        monkeypatch.setattr(
            broker.PINNED_SSH_TRANSPORT_POOL, "run_command", run_command
        )
        monkeypatch.setattr(broker, "resolve_ssh_key_path", lambda: "/tmp/key")
        monkeypatch.setattr(broker, "orchestrator_can_reach", lambda host: True)

        with pytest.raises(broker.BrowserStreamUnavailable) as error:
            asyncio.run(
                broker.exec_stream_info(
                    _bound_thread(),
                    generation_resolver=lambda: asyncio.sleep(
                        0, result=_bound_thread(generation=UUID(int=2))
                    ),
                )
            )

        assert error.value.status == 409
        assert "sentinel" not in error.value.detail

    def test_nonzero_result_redacts_remote_output(
        self, monkeypatch, caplog
    ):
        async def run_command(**kwargs):
            del kwargs
            return PinnedSSHCommandResult(
                exit_status=7,
                stdout=b"stdout-secret-sentinel",
                stderr=b"stderr-secret-sentinel",
            )

        monkeypatch.setattr(
            broker.PINNED_SSH_TRANSPORT_POOL, "run_command", run_command
        )
        monkeypatch.setattr(broker, "resolve_ssh_key_path", lambda: "/tmp/key")
        monkeypatch.setattr(broker, "orchestrator_can_reach", lambda host: True)
        caplog.set_level(logging.DEBUG)

        with pytest.raises(broker.BrowserStreamUnavailable) as error:
            asyncio.run(
                broker.exec_stream_info(
                    _bound_thread(),
                    generation_resolver=lambda: asyncio.sleep(
                        0, result=_bound_thread()
                    ),
                )
            )

        captured = error.value.detail + caplog.text
        assert "stdout-secret-sentinel" not in captured
        assert "stderr-secret-sentinel" not in captured


def _ws_app():
    app = FastAPI()

    @app.websocket("/stream/{thread_id}")
    async def stream(ws: WebSocket, thread_id: str):
        from main import postgres_db

        await broker.relay_browser_stream(ws, thread_id, db=postgres_db)

    return app


def _connect_and_capture_close(client, url) -> int:
    try:
        with client.websocket_connect(url) as ws:
            ws.receive_bytes()
        return 1000
    except WebSocketDisconnect as exc:
        return exc.code


class TestRelayAuthGates:
    def test_disabled_closes_4404(self, monkeypatch):
        monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", object(), raising=False)

        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4404

    def test_unauthenticated_closes_4401(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")

        async def no_user(ws, db):
            return None

        monkeypatch.setattr(broker, "resolve_ws_user", no_user)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", object(), raising=False)

        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4401

    def test_stale_generation_closes_4409(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")

        async def fake_user(ws, db):
            return {"id": "u1", "is_approved": True}

        async def fake_info(thread, **kwargs):
            return {"generation": "g-new", "token": "tok", "port": 38801}

        async def fake_record(db, thread_id):
            return SimpleNamespace(source=SimpleNamespace(browser_generation="g-old"))

        thread = {
            "id": "t1",
            "user_id": "u1",
            "metadata": {
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "h",
                    "ssh_port": 22,
                }
            },
        }

        class FakeDB:
            async def get_thread(self, thread_id):
                return dict(thread)

        monkeypatch.setattr(broker, "resolve_ws_user", fake_user)
        monkeypatch.setattr(broker, "exec_stream_info", fake_info)
        monkeypatch.setattr(broker, "_get_canvas_record", fake_record)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", FakeDB(), raising=False)

        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4409


class TestRelayHappyPath:
    def test_relays_state_frame_and_sends_hello(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
        written = []
        activity_touched = threading.Event()

        class FakeSSHReader:
            def __init__(self):
                state = broker.encode_stream_frame(
                    broker.T_STATE,
                    b'{"baton":"user"}',
                )
                self._chunks = [state[:4], state[4:]]

            async def readexactly(self, size):
                if not self._chunks:
                    await asyncio.sleep(3600)
                chunk = self._chunks.pop(0)
                assert len(chunk) == size
                return chunk

        class FakeSSHWriter:
            def write(self, data):
                written.append(bytes(data))

            async def drain(self):
                pass

        @asynccontextmanager
        async def fake_open(**kwargs):
            yield FakeSSHReader(), FakeSSHWriter()

        async def fake_user(ws, db):
            return {"id": "u1", "is_approved": True}

        async def fake_info(thread, **kwargs):
            return {"generation": "g1", "token": "tok", "port": 38801}

        async def fake_record(db, thread_id):
            return SimpleNamespace(source=SimpleNamespace(browser_generation="g1"))

        thread = {
            "id": "t1",
            "user_id": "u1",
            "metadata": {
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "h",
                    "ssh_port": 22,
                }
            },
        }

        class FakeDB:
            async def get_thread(self, thread_id):
                return dict(thread)

            async def merge_thread_workspace_context(self, thread_id, updates):
                activity_touched.set()
                return True

        monkeypatch.setattr(broker, "resolve_ws_user", fake_user)
        monkeypatch.setattr(broker, "exec_stream_info", fake_info)
        monkeypatch.setattr(broker, "_get_canvas_record", fake_record)
        monkeypatch.setattr(broker, "_resolve_target", lambda thread: object())
        monkeypatch.setattr(broker, "_resolve_key_path", lambda: "/tmp/key")
        monkeypatch.setattr(broker, "_open_loopback", fake_open)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", FakeDB(), raising=False)

        with TestClient(app).websocket_connect("/stream/t1") as ws:
            first = ws.receive_bytes()
            assert first[0] == broker.T_STATE
            assert first[1:] == b'{"baton":"user"}'
            assert activity_touched.wait(timeout=1)
            ws.send_bytes(bytes([broker.T_CONTROL]) + b'{"op":"take_baton"}')

        assert written[0][4] == broker.T_HELLO
        assert b'"token": "tok"' in written[0] or b'"token":"tok"' in written[0]
        assert any(frame[4] == broker.T_CONTROL for frame in written[1:])
