"""Host-side unit tests for docker/browser-exec streaming additions.

The daemon file has no .py extension and must stay stdlib-importable
(browser-use imports are lazy), so we load it via SourceFileLoader.
"""

import asyncio
import importlib.machinery
import importlib.util
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
