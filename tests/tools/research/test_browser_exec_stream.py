"""Host-side unit tests for docker/browser-exec streaming additions.

The daemon file has no .py extension and must stay stdlib-importable
(browser-use imports are lazy), so we load it via SourceFileLoader.
"""

import asyncio
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "browser_exec_mod", str(REPO / "docker" / "browser-exec")
    )
    spec = importlib.util.spec_from_loader("browser_exec_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


BE = _load()


class TestFramingCodec:
    def test_roundtrip(self):
        async def run():
            wire = BE.encode_stream_frame(BE.T_STATE, b'{"baton":"agent"}')
            reader = asyncio.StreamReader()
            reader.feed_data(wire)
            reader.feed_eof()
            return await BE.read_stream_frame(reader)

        ftype, payload = asyncio.run(run())
        assert ftype == BE.T_STATE
        assert payload == b'{"baton":"agent"}'

    def test_length_covers_type_byte(self):
        wire = BE.encode_stream_frame(BE.T_HELLO, b"abc")
        assert wire[:4] == (4).to_bytes(4, "big")
        assert wire[4] == BE.T_HELLO

    def test_oversize_encode_rejected(self):
        with pytest.raises(ValueError):
            BE.encode_stream_frame(BE.T_FRAME, b"x" * (BE.MAX_STREAM_FRAME + 1))

    def test_oversize_read_rejected(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(
                (BE.MAX_STREAM_FRAME + 100).to_bytes(4, "big") + b"\x02"
            )
            reader.feed_eof()
            await BE.read_stream_frame(reader)

        with pytest.raises(ValueError):
            asyncio.run(run())

    def test_frame_payload_roundtrip(self):
        header = {"generation": "g1", "w": 1280, "h": 720, "ts": 1.5}
        jpeg = b"\xff\xd8fakejpeg"
        header2, jpeg2 = BE.decode_frame_payload(
            BE.encode_frame_payload(header, jpeg)
        )
        assert header2 == header
        assert jpeg2 == jpeg


class _StubFiles:
    def url_for(self, url):
        if url.startswith("file://"):
            return "http://127.0.0.1:45678/mock.html"
        return url


class TestValidateUserNav:
    def test_https_passes(self):
        assert (
            BE.validate_user_nav("https://example.com/x", _StubFiles())
            == "https://example.com/x"
        )

    def test_schemeless_gets_https(self):
        assert (
            BE.validate_user_nav("example.com", _StubFiles())
            == "https://example.com"
        )

    def test_javascript_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("javascript:alert(1)", _StubFiles())

    def test_data_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("data:text/html,<b>x</b>", _StubFiles())

    def test_metadata_host_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav(
                "http://metadata.google.internal/", _StubFiles()
            )

    def test_k8s_internal_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav(
                "http://orchestrator.default.svc.cluster.local/",
                _StubFiles(),
            )

    def test_file_translated_then_allowed(self):
        assert (
            BE.validate_user_nav(
                "file:///home/agent-host/workspace/mock.html", _StubFiles()
            )
            == "http://127.0.0.1:45678/mock.html"
        )

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("   ", _StubFiles())


class TestStreamHub:
    def test_mint_generation_sets_identity_and_initial_baton(self):
        hub = BE.StreamHub()
        assert hub.baton == "agent"
        hub.mint_generation("user")
        assert hub.baton == "user"
        assert hub.generation and hub.token and len(hub.token) == 64

    def test_state_payload_shape(self):
        hub = BE.StreamHub()
        hub.mint_generation()
        state = hub.state_payload()
        assert set(state) >= {
            "generation",
            "baton",
            "viewport",
            "url",
            "title",
            "loading",
        }

    def test_broadcast_drops_oldest_for_laggards(self):
        async def run():
            hub = BE.StreamHub()
            queue = asyncio.Queue(maxsize=2)
            hub.add_viewer(queue)
            for frame in (b"f1", b"f2", b"f3"):
                hub.broadcast(frame)
            assert queue.qsize() == 2
            assert await queue.get() == b"f2"
            assert await queue.get() == b"f3"

        asyncio.run(run())

    def test_auto_release_after_last_viewer_leaves(self, monkeypatch):
        monkeypatch.setattr(BE, "BATON_GRACE_S", 0.05)

        async def run():
            hub = BE.StreamHub()
            hub.mint_generation("user")
            viewer_id = hub.add_viewer(asyncio.Queue(maxsize=4))
            hub.remove_viewer(viewer_id)
            assert hub.baton == "user"
            await asyncio.sleep(0.15)
            assert hub.baton == "agent"

        asyncio.run(run())

    def test_reconnect_cancels_auto_release(self, monkeypatch):
        monkeypatch.setattr(BE, "BATON_GRACE_S", 0.05)

        async def run():
            hub = BE.StreamHub()
            hub.mint_generation("user")
            viewer_id = hub.add_viewer(asyncio.Queue(maxsize=4))
            hub.remove_viewer(viewer_id)
            hub.add_viewer(asyncio.Queue(maxsize=4))
            await asyncio.sleep(0.15)
            assert hub.baton == "user"

        asyncio.run(run())


class TestBatonRefusal:
    def test_mutating_action_refused_while_user_drives(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")
        daemon.hub.state_extra["url"] = "https://example.com/form"
        response = asyncio.run(
            daemon.handle({"action": "click", "args": {"ref": 1}})
        )
        assert response["error"] == "user_is_driving"
        assert response["url"] == "https://example.com/form"
        assert "release control" in response["message"]

    def test_refusal_never_touches_the_browser(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")

        async def boom():
            raise AssertionError("session must not be touched for a refusal")

        daemon._get_session = boom
        response = asyncio.run(
            daemon.handle(
                {
                    "action": "navigate",
                    "args": {"url": "https://x.dev"},
                }
            )
        )
        assert response["error"] == "user_is_driving"

    def test_unknown_action_beats_baton_check(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")
        response = asyncio.run(daemon.handle({"action": "bogus"}))
        assert "unknown action" in response["error"]


class TestStreamListener:
    @staticmethod
    async def _client(port):
        return await asyncio.open_connection("127.0.0.1", port)

    @staticmethod
    async def _send(writer, frame_type, obj):
        writer.write(
            BE.encode_stream_frame(frame_type, json.dumps(obj).encode())
        )
        await writer.drain()

    @staticmethod
    async def _recv(reader):
        frame_type, payload = await asyncio.wait_for(
            BE.read_stream_frame(reader), timeout=2
        )
        return frame_type, json.loads(payload.decode())

    def _daemon(self, monkeypatch, port):
        monkeypatch.setattr(BE, "STREAM_PORT", port)
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")

        async def no_op(*_args, **_kwargs):
            return None

        # Listener protocol tests stay host-only. The real adapter is exercised
        # by docker/check-browser-stream.py inside the workspace image.
        daemon.ensure_screencast = no_op
        daemon.stop_screencast = no_op
        daemon.dispatch_user_input = no_op
        return daemon

    def test_bad_token_gets_error_and_close(self, monkeypatch):
        async def run():
            daemon = self._daemon(monkeypatch, 38899)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38899)
                await self._send(
                    writer,
                    BE.T_HELLO,
                    {"token": "wrong", "min_protocol": 1},
                )
                frame_type, error = await self._recv(reader)
                assert frame_type == BE.T_ERROR
                assert error["code"] == "unauthorized"
                assert await reader.read(1) == b""
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())

    def test_good_token_gets_state_then_baton_flip_broadcast(
        self, monkeypatch
    ):
        async def run():
            daemon = self._daemon(monkeypatch, 38898)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38898)
                await self._send(
                    writer,
                    BE.T_HELLO,
                    {"token": daemon.hub.token, "min_protocol": 1},
                )
                frame_type, state = await self._recv(reader)
                assert (frame_type, state["baton"]) == (BE.T_STATE, "user")
                assert state["generation"] == daemon.hub.generation
                await self._send(
                    writer, BE.T_CONTROL, {"op": "release_baton"}
                )
                frame_type, state = await self._recv(reader)
                assert (frame_type, state["baton"]) == (BE.T_STATE, "agent")
                response = await daemon.handle({"action": "bogus"})
                assert "unknown action" in response["error"]
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())

    def test_disconnect_removes_viewer(self, monkeypatch):
        async def run():
            daemon = self._daemon(monkeypatch, 38897)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38897)
                await self._send(
                    writer,
                    BE.T_HELLO,
                    {"token": daemon.hub.token, "min_protocol": 1},
                )
                await self._recv(reader)
                assert len(daemon.hub.viewers) == 1
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0.1)
                assert len(daemon.hub.viewers) == 0
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())
