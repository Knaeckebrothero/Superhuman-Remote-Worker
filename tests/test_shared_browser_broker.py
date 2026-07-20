"""Broker helper tests: codec mirror, readiness, and SSH resolution."""

import asyncio

import pytest

from services import browser_stream_broker as broker


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
        assert broker.ssh_endpoint(thread) == ("10.1.2.3", 2222)

    def test_vm_preferred_when_ready(self):
        thread = {
            "metadata": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "100.99.1.2",
                    "ssh_port": 22,
                },
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "10.1.2.3",
                    "ssh_port": 2222,
                },
            }
        }
        assert broker.ssh_endpoint(thread) == ("100.99.1.2", 22)

    def test_not_ready(self):
        assert broker.workspace_ready({"metadata": {}}) is False
        with pytest.raises(broker.BrowserStreamUnavailable):
            broker.ssh_endpoint({"metadata": {}})


class TestExecStreamInfo:
    def test_parses_last_stdout_line(self, monkeypatch):
        async def fake_exec(*cmd, **kwargs):
            class Proc:
                returncode = 0

                async def communicate(self):
                    return (
                        b'[browser-exec] noise\n{"generation": "g1", '
                        b'"token": "t", "port": 38801, "baton": "user"}\n',
                        b"",
                    )

                def kill(self):
                    pass

            return Proc()

        monkeypatch.setattr(broker.asyncio, "create_subprocess_exec", fake_exec)
        thread = {
            "metadata": {
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "h",
                    "ssh_port": 22,
                }
            }
        }
        info = asyncio.run(broker.exec_stream_info(thread, initial_baton="user"))
        assert info["generation"] == "g1"

    def test_error_payload_raises(self, monkeypatch):
        async def fake_exec(*cmd, **kwargs):
            class Proc:
                returncode = 0

                async def communicate(self):
                    return (
                        b'{"error": "could not reach browser-exec daemon"}\n',
                        b"",
                    )

                def kill(self):
                    pass

            return Proc()

        monkeypatch.setattr(broker.asyncio, "create_subprocess_exec", fake_exec)
        thread = {
            "metadata": {
                "workspace_container": {
                    "status": "ready",
                    "ssh_host": "h",
                    "ssh_port": 22,
                }
            }
        }
        with pytest.raises(broker.BrowserStreamUnavailable):
            asyncio.run(broker.exec_stream_info(thread))
