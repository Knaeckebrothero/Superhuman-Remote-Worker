"""MCPManager lifecycle: parse, connect, discover, degrade, and close.

Integration tests run a real stdio MCP server as a subprocess via
``sys.executable``. They require no external network or service.
"""

import asyncio
import socket
import sys
import textwrap

import pytest

from src.tools.mcp.manager import MCPManager, parse_mcp_config

ECHO_SERVER = textwrap.dedent(
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("echo")

    @mcp.tool()
    def echo(text: str) -> str:
        \"\"\"Echo the input back.\"\"\"
        return f"echo: {text}"

    @mcp.tool()
    def add(a: int, b: int) -> int:
        \"\"\"Add two integers.\"\"\"
        return a + b

    mcp.run(transport="stdio")
    """
)

HTTP_ECHO_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.fastmcp import FastMCP

    port = int(sys.argv[1])
    transport = sys.argv[2]
    mcp = FastMCP(
        "echo",
        host="127.0.0.1",
        port=port,
        log_level="ERROR",
    )

    @mcp.tool()
    def echo(text: str) -> str:
        \"\"\"Echo the input back.\"\"\"
        return f"echo: {text}"

    mcp.run(transport=transport)
    """
)


def _stdio_ds(script_path, name="Echo Server"):
    return {
        "type": "mcp",
        "name": name,
        "connection_url": None,
        "credentials": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script_path)],
            "env": {},
        },
    }


async def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise AssertionError(f"test MCP server did not listen on port {port}")


class TestParseConfig:
    def test_http_requires_url(self):
        with pytest.raises(ValueError, match="connection_url"):
            parse_mcp_config(
                {
                    "type": "mcp",
                    "name": "x",
                    "connection_url": None,
                    "credentials": {"transport": "http"},
                }
            )

    def test_stdio_requires_command(self):
        with pytest.raises(ValueError, match="command"):
            parse_mcp_config(
                {
                    "type": "mcp",
                    "name": "x",
                    "connection_url": None,
                    "credentials": {"transport": "stdio"},
                }
            )

    def test_unknown_transport_rejected(self):
        with pytest.raises(ValueError, match="transport"):
            parse_mcp_config(
                {
                    "type": "mcp",
                    "name": "x",
                    "connection_url": "http://h",
                    "credentials": {"transport": "carrier-pigeon"},
                }
            )

    def test_bearer_auth_becomes_header(self):
        cfg = parse_mcp_config(
            {
                "type": "mcp",
                "name": "x",
                "connection_url": "https://h/mcp",
                "credentials": {
                    "transport": "http",
                    "auth": {"type": "bearer", "token": "tok123"},
                },
            }
        )
        assert cfg.headers == {"Authorization": "Bearer tok123"}

    def test_defaults_to_http_transport(self):
        cfg = parse_mcp_config(
            {
                "type": "mcp",
                "name": "x",
                "connection_url": "https://h/mcp",
                "credentials": {},
            }
        )
        assert cfg.transport == "http"


@pytest.mark.asyncio
async def test_connect_discover_call_close(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    ds = _stdio_ds(script)
    manager = MCPManager([ds])
    await manager.connect_all()
    try:
        tools = manager.get_langchain_tools()
        names = {tool.name for tool in tools}
        assert "mcp__echo_server__echo" in names
        assert "mcp__echo_server__add" in names
        echo = next(tool for tool in tools if tool.name.endswith("__echo"))
        result = await echo.coroutine(text="hi")
        assert "echo: hi" in str(result)
        manager.annotate_configs()
        assert ds["_mcp_status"] == "connected"
        assert "mcp__echo_server__echo" in ds["_mcp_tools"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unreachable_server_degrades_not_raises(tmp_path):
    broken = _stdio_ds(tmp_path / "nonexistent.py", name="Broken")
    good_script = tmp_path / "echo_server.py"
    good_script.write_text(ECHO_SERVER)
    good = _stdio_ds(good_script, name="Good")
    manager = MCPManager([broken, good])
    await manager.connect_all()
    try:
        manager.annotate_configs()
        assert broken["_mcp_status"].startswith("unavailable")
        assert good["_mcp_status"] == "connected"
        assert all(
            tool.name.startswith("mcp__good__")
            for tool in manager.get_langchain_tools()
        )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_transport", "server_transport", "path"),
    [
        ("http", "streamable-http", "/mcp"),
        ("sse", "sse", "/sse"),
    ],
)
async def test_remote_transport_discover_and_call(
    tmp_path,
    client_transport,
    server_transport,
    path,
):
    script = tmp_path / "http_echo_server.py"
    script.write_text(HTTP_ECHO_SERVER)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(port),
        server_transport,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    manager = None
    try:
        await _wait_for_port(port)
        manager = MCPManager(
            [
                {
                    "type": "mcp",
                    "name": f"Remote {client_transport}",
                    "connection_url": f"http://127.0.0.1:{port}{path}",
                    "credentials": {"transport": client_transport},
                }
            ]
        )
        await manager.connect_all()
        echo = next(
            tool
            for tool in manager.get_langchain_tools()
            if tool.name.endswith("__echo")
        )
        assert "echo: remote" in str(await echo.coroutine(text="remote"))
    finally:
        if manager is not None:
            await manager.aclose()
        process.terminate()
        await process.wait()


@pytest.mark.asyncio
async def test_invalid_config_marked_invalid():
    manager = MCPManager(
        [
            {
                "type": "mcp",
                "name": "bad",
                "connection_url": None,
                "credentials": {},
            }
        ]
    )
    await manager.connect_all()
    assert manager.statuses["bad"].startswith("unavailable")
    await manager.aclose()


@pytest.mark.asyncio
async def test_sync_close_inside_running_loop(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    manager = MCPManager([_stdio_ds(script)])
    await manager.connect_all()
    manager.close()
    await asyncio.sleep(0.5)
    assert all(handle.task.done() for handle in manager._handles)


@pytest.mark.asyncio
async def test_tool_error_returns_string_not_raise(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    manager = MCPManager([_stdio_ds(script)])
    await manager.connect_all()
    try:
        echo = next(
            tool
            for tool in manager.get_langchain_tools()
            if tool.name.endswith("__echo")
        )
        handle = manager._handles[0]
        handle.shutdown.set()
        await asyncio.sleep(0.3)
        handle.ds["credentials"]["args"] = [str(tmp_path / "gone.py")]

        result = await echo.coroutine(text="hi")

        assert isinstance(result, str)
        assert "MCP" in result and "error" in result.lower()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reconnect_once_revives_tool(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    manager = MCPManager([_stdio_ds(script)])
    await manager.connect_all()
    try:
        echo = next(
            tool
            for tool in manager.get_langchain_tools()
            if tool.name.endswith("__echo")
        )
        handle = manager._handles[0]
        handle.shutdown.set()
        await asyncio.sleep(0.3)

        result = await echo.coroutine(text="revived")

        assert "echo: revived" in str(result)
        assert handle.reconnected_once is True
    finally:
        await manager.aclose()
