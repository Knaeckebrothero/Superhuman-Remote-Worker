# MCP Servers as Datasources — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users add external MCP servers as connectors (persisted internally as `type='mcp'` datasources); the worker agent connects as an MCP client at job/session start, discovers the server's tools, and binds them alongside native tools.

**Architecture:** MCP uses the existing internal datasource pipeline end to end: Cockpit connector form → `datasources` table (encrypted `credentials` JSONB) → `project_datasources` link → `_build_datasources_payload` → agent's `process_datasources()` → a new `MCPManager` (one per job, holding N servers, because `ToolContext` datasource slots are TYPE-keyed) → runtime registration into `TOOL_REGISTRY` under category `mcp` → normal `load_tools()`/`bind_tools()`. Tool names the orchestrator can't know ahead of time are expressed as a `"*"` wildcard in the category override and expanded agent-side after discovery.

**Tech Stack:** Python 3.11 (agent/orchestrator), `mcp` SDK + `langchain-mcp-adapters` (new deps), FastAPI, Angular 20 + transloco (cockpit), Helm.

**Spec:** `docs/features/mcp_datasources.md` (all decisions locked there).

## Global Constraints

- Work directly on `develop`; commit per task; **NEVER push without asking**.
- CI (Python 3.12) is the test gate; local pytest on Py3.14 is noisy — judge by the named test files passing, not unrelated warnings.
- Feature gates default **off**: `MCP_DATASOURCES_ENABLED`, `MCP_STDIO_ENABLED` (env, truthy = `true|1|yes`).
- Timeouts (from spec): 10s per-server connect, 60s per tool call.
- Tool names: `mcp__<server_slug>__<tool>`, ≤64 chars (OpenAI-compatible function-name limit), server slug ≤16 chars, 4-char hash suffix on overflow/collision.
- Credential values must never appear in `datasources.md`, logs, or error strings.
- One failing MCP server must never fail a job (graceful degradation everywhere).
- Cockpit tests: `npx vitest run` in `cockpit/` (fast, reliable); don't run `ng build`.
- anyio cancel-scope rule: MCP client contexts MUST be entered and exited by the same asyncio task (the per-server owner task in Task 3). Never enter a session in one task and close it in another.

---

### Task 1: Dependencies + naming helpers

**Files:**
- Modify: `requirements.txt`
- Create: `src/tools/mcp/__init__.py`
- Create: `src/tools/mcp/naming.py`
- Test: `tests/test_mcp_naming.py`

**Interfaces:**
- Produces: `mcp_server_slug(name: str, max_len: int = 16) -> str`; `namespace_mcp_tool(server_slug: str, tool_name: str, taken: set[str]) -> str` (deterministic, ≤64 chars, collision-safe). Package `src.tools.mcp` importable.

- [ ] **Step 1: Add pinned dependencies**

Append to `requirements.txt` after the `langgraph-checkpoint-postgres` line:

```
mcp>=1.9,<2.0  # MCP client SDK (stdio/streamable-http/sse transports) — spec docs/features/mcp_datasources.md
langchain-mcp-adapters>=0.1.9,<0.2  # load_mcp_tools: MCP tool -> LangChain BaseTool
```

Install locally: `pip install 'mcp>=1.9,<2.0' 'langchain-mcp-adapters>=0.1.9,<0.2'`

- [ ] **Step 2: Write the failing tests**

`tests/test_mcp_naming.py`:

```python
"""Tool-name namespacing for MCP-provided tools (docs/features/mcp_datasources.md).

Namespaced names must satisfy OpenAI-compatible function-name rules:
<=64 chars, [a-zA-Z0-9_-]. Server slug is capped at 16 chars; overflow or
collision appends a deterministic 4-char hash.
"""

import re

from src.tools.mcp.naming import mcp_server_slug, namespace_mcp_tool


class TestServerSlug:
    def test_basic(self):
        assert mcp_server_slug("GitHub MCP") == "github_mcp"

    def test_strips_special_chars_and_collapses(self):
        assert mcp_server_slug("My--Server!! (prod)") == "my_server_prod"

    def test_caps_at_16(self):
        slug = mcp_server_slug("a very long server name indeed")
        assert len(slug) <= 16 and not slug.endswith("_")

    def test_empty_falls_back(self):
        assert mcp_server_slug("!!!") == "server"


class TestNamespaceTool:
    def test_shape(self):
        name = namespace_mcp_tool("github", "create_issue", set())
        assert name == "mcp__github__create_issue"

    def test_valid_charset_and_length(self):
        name = namespace_mcp_tool(
            mcp_server_slug("Some Extremely Long Server Name"),
            "a_tool_with_an_extremely_long_name_beyond_all_reason_x" * 2,
            set(),
        )
        assert len(name) <= 64
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)

    def test_collision_gets_hash_suffix(self):
        taken = {"mcp__github__create_issue"}
        name = namespace_mcp_tool("github", "create_issue", taken)
        assert name != "mcp__github__create_issue"
        assert len(name) <= 64

    def test_deterministic(self):
        a = namespace_mcp_tool("srv", "toolname" * 20, set())
        b = namespace_mcp_tool("srv", "toolname" * 20, set())
        assert a == b
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_mcp_naming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.mcp'`

- [ ] **Step 4: Implement**

`src/tools/mcp/naming.py`:

```python
"""Deterministic naming for MCP-provided tools.

Namespace: ``mcp__<server_slug>__<tool>``. Provider function-name limits
(64 chars for OpenAI-compatible endpoints) are enforced here; the registry
metadata keeps the mapping back to the server's true tool name, so the
wire call always uses the original name (docs/features/mcp_datasources.md).
"""

import hashlib
import re

_MAX_TOOL_NAME = 64
_MAX_SLUG = 16


def mcp_server_slug(name: str, max_len: int = _MAX_SLUG) -> str:
    """Lowercase, non-alphanumerics to ``_``, collapsed, capped at max_len."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    slug = slug[:max_len].rstrip("_")
    return slug or "server"


def namespace_mcp_tool(server_slug: str, tool_name: str, taken: set) -> str:
    """Build a unique ``mcp__<slug>__<tool>`` name, <=64 chars.

    On overflow or collision, truncates and appends a deterministic 4-char
    hash of ``<slug>:<tool>`` so the same server+tool always maps to the
    same namespaced name.
    """
    clean_tool = re.sub(r"[^a-zA-Z0-9_-]+", "_", tool_name).strip("_") or "tool"
    base = f"mcp__{server_slug}__{clean_tool}"
    if len(base) <= _MAX_TOOL_NAME and base not in taken:
        return base
    digest = hashlib.sha1(f"{server_slug}:{tool_name}".encode()).hexdigest()[:4]
    trimmed = base[: _MAX_TOOL_NAME - 5].rstrip("_")
    candidate = f"{trimmed}_{digest}"
    while candidate in taken:  # pathological repeat collision
        digest = hashlib.sha1(candidate.encode()).hexdigest()[:4]
        candidate = f"{trimmed}_{digest}"
    return candidate
```

`src/tools/mcp/__init__.py`:

```python
"""MCP client toolkit — external MCP servers attached as datasources.

The worker agent acts as an MCP *client*: servers are added by users as
``type='mcp'`` datasources, connected at job/session start, and their
tools registered dynamically. See docs/features/mcp_datasources.md.
"""

from .naming import mcp_server_slug, namespace_mcp_tool

__all__ = ["mcp_server_slug", "namespace_mcp_tool"]
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_mcp_naming.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add requirements.txt src/tools/mcp/ tests/test_mcp_naming.py
git commit -m "feat(mcp-ds): add MCP client deps and tool-name namespacing"
```

---

### Task 2: Category map + wildcard override

**Files:**
- Modify: `src/core/datasource_setup.py` (`DATASOURCE_TOOL_MAP` ~line 93, `datasource_tool_categories` ~line 149)
- Test: `tests/test_datasource_tool_categories.py` (existing — assertions change)

**Interfaces:**
- Consumes: nothing new.
- Produces: `DATASOURCE_TOOL_MAP["mcp"] == {"category": "mcp", "dynamic": True}`; `datasource_tool_categories(...)` yields `{"mcp": ["*"]}` when any MCP datasource is attached, `{"mcp": []}` when none. The `"*"` sentinel is what Task 5's `expand_tool_wildcards` resolves agent-side.

- [ ] **Step 1: Extend the existing tests**

`tests/test_datasource_tool_categories.py` asserts exact dicts (e.g. `test_no_datasources_strips_all_categories` compares `== {...}`). Every exact-dict assertion gains `"mcp": []` (or `["*"]` where an mcp datasource is in the input). Then add:

```python
    def test_mcp_attached_yields_wildcard(self):
        cats = datasource_tool_categories([_ds("mcp")])
        assert cats["mcp"] == ["*"]

    def test_mcp_absent_strips_category(self):
        assert datasource_tool_categories([])["mcp"] == []

    def test_mcp_read_only_link_still_wildcard(self):
        # No read-only mode for MCP — the server is the access boundary.
        cats = datasource_tool_categories([_ds("mcp", read_only=True)])
        assert cats["mcp"] == ["*"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_datasource_tool_categories.py -v`
Expected: new tests FAIL (`KeyError: 'mcp'`); updated exact-dict tests FAIL until implementation.

- [ ] **Step 3: Implement**

In `DATASOURCE_TOOL_MAP` (after the `"email"` entry, keeping the closing brace):

```python
    # MCP servers expose tools discovered at runtime — no static name list.
    # The override emits the "*" wildcard; the agent expands it after
    # discovery (registry.expand_tool_wildcards). docs/features/mcp_datasources.md
    "mcp": {
        "category": "mcp",
        "dynamic": True,
    },
```

In `datasource_tool_categories`, insert a `dynamic` branch after the `if not ds_list:` branch and before the `elif "tiers"` branch:

```python
        elif tool_info.get("dynamic"):
            # Tool names unknown until the agent connects; read-only links
            # don't change this (the server enforces access).
            categories[category] = ["*"]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_datasource_tool_categories.py -v`
Expected: all PASS

- [ ] **Step 5: Check the other exact-dict consumers**

Run: `python -m pytest tests/test_live_datasource_update.py tests/test_datasource_redesign.py -v`
Expected: PASS. If any test asserts the full category dict, add `"mcp": []` there too.

- [ ] **Step 6: Commit**

```bash
git add src/core/datasource_setup.py tests/test_datasource_tool_categories.py tests/test_live_datasource_update.py tests/test_datasource_redesign.py
git commit -m "feat(mcp-ds): mcp entry in DATASOURCE_TOOL_MAP with dynamic wildcard override"
```

---

### Task 3: MCPManager — config parsing, owner-task lifecycle, discovery

**Files:**
- Create: `src/tools/mcp/manager.py`
- Modify: `src/tools/mcp/__init__.py`
- Test: `tests/test_mcp_manager.py`

**Interfaces:**
- Consumes: `mcp_server_slug`, `namespace_mcp_tool` (Task 1).
- Produces:
  - `parse_mcp_config(ds: dict) -> MCPServerConfig` — raises `ValueError` on bad shape.
  - `class MCPManager:` constructed with `list[dict]` (the raw ds config dicts, held by reference); `await connect_all()` (per-server 10s bound, never raises); `get_langchain_tools() -> list` (namespaced `StructuredTool`s across connected servers); `annotate_configs()` (writes `_mcp_status` / `_mcp_tools` onto the original ds dicts); `statuses -> dict[str, str]`; sync `close()` + `async aclose()`.

**The cancel-scope rule (why owner tasks):** `mcp`'s transport contexts use anyio cancel scopes, which raise if exited from a different task than entered them. Each server therefore gets ONE owner task that enters the transport + session contexts, signals readiness, parks on a shutdown `Event`, and exits its own contexts. Cross-task `session.call_tool(...)` is safe; cross-task context exit is not.

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_manager.py`:

```python
"""MCPManager lifecycle: parse, connect, discover, degrade, close.

Integration tests run a real stdio MCP server (FastMCP from the `mcp`
package) as a subprocess via sys.executable — no network, CI-safe.
"""

import asyncio
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


class TestParseConfig:
    def test_http_requires_url(self):
        with pytest.raises(ValueError, match="connection_url"):
            parse_mcp_config(
                {"type": "mcp", "name": "x", "connection_url": None,
                 "credentials": {"transport": "http"}}
            )

    def test_stdio_requires_command(self):
        with pytest.raises(ValueError, match="command"):
            parse_mcp_config(
                {"type": "mcp", "name": "x", "connection_url": None,
                 "credentials": {"transport": "stdio"}}
            )

    def test_unknown_transport_rejected(self):
        with pytest.raises(ValueError, match="transport"):
            parse_mcp_config(
                {"type": "mcp", "name": "x", "connection_url": "http://h",
                 "credentials": {"transport": "carrier-pigeon"}}
            )

    def test_bearer_auth_becomes_header(self):
        cfg = parse_mcp_config(
            {"type": "mcp", "name": "x", "connection_url": "https://h/mcp",
             "credentials": {"transport": "http",
                             "auth": {"type": "bearer", "token": "tok123"}}}
        )
        assert cfg.headers == {"Authorization": "Bearer tok123"}

    def test_defaults_to_http_transport(self):
        cfg = parse_mcp_config(
            {"type": "mcp", "name": "x", "connection_url": "https://h/mcp",
             "credentials": {}}
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
        names = {t.name for t in tools}
        assert "mcp__echo_server__echo" in names
        assert "mcp__echo_server__add" in names
        echo = next(t for t in tools if t.name.endswith("__echo"))
        result = await echo.coroutine(text="hi")
        assert "echo: hi" in str(result)
        manager.annotate_configs()
        assert ds["_mcp_status"] == "connected"
        assert "mcp__echo_server__echo" in ds["_mcp_tools"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unreachable_server_degrades_not_raises(tmp_path):
    ds = _stdio_ds(tmp_path / "nonexistent.py", name="Broken")
    good_script = tmp_path / "echo_server.py"
    good_script.write_text(ECHO_SERVER)
    good = _stdio_ds(good_script, name="Good")
    manager = MCPManager([ds, good])
    await manager.connect_all()  # must not raise
    try:
        manager.annotate_configs()
        assert ds["_mcp_status"].startswith("unavailable")
        assert good["_mcp_status"] == "connected"
        assert all(t.name.startswith("mcp__good__") for t in manager.get_langchain_tools())
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_invalid_config_marked_invalid():
    manager = MCPManager([{"type": "mcp", "name": "bad",
                           "connection_url": None, "credentials": {}}])
    await manager.connect_all()
    assert manager.statuses["bad"].startswith("unavailable")
    await manager.aclose()


@pytest.mark.asyncio
async def test_sync_close_inside_running_loop(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    manager = MCPManager([_stdio_ds(script)])
    await manager.connect_all()
    manager.close()  # sync close from async context: schedules aclose
    await asyncio.sleep(0.5)  # let the scheduled task run
    assert all(h.task.done() for h in manager._handles)
```

Note: if `pytest.mark.asyncio` isn't configured project-wide, check how other async tests declare it (`rg -l asyncio_mode pytest.ini pyproject.toml setup.cfg tests/conftest.py`) and mirror that.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: FAIL — `ImportError: cannot import name 'MCPManager'`

- [ ] **Step 3: Implement**

`src/tools/mcp/manager.py`:

```python
"""MCPManager — the agent's MCP client for user-attached MCP datasources.

One manager per job holds N servers because ToolContext datasource slots
are TYPE-keyed (last-one-wins): datasources_dict["mcp"] must be a single
object. Each server runs under a dedicated OWNER TASK that enters and
exits the anyio transport/session contexts (cancel scopes must open and
close in the same task); cross-task session.call_tool() is safe.

Spec: docs/features/mcp_datasources.md. Timeouts: 10s connect, 60s/call.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .naming import mcp_server_slug, namespace_mcp_tool

logger = logging.getLogger(__name__)

MCP_CONNECT_TIMEOUT = 10.0
MCP_CALL_TIMEOUT = 60.0
_TRANSPORTS = ("http", "sse", "stdio")


@dataclass
class MCPServerConfig:
    name: str
    transport: str  # http | sse | stdio
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


def parse_mcp_config(ds: Dict[str, Any]) -> MCPServerConfig:
    """Validate an mcp datasource dict into an MCPServerConfig.

    Raises ValueError with a message that NEVER contains credential values.
    """
    creds = ds.get("credentials") or {}
    transport = (creds.get("transport") or "http").lower()
    if transport not in _TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r} (expected one of {_TRANSPORTS})")

    name = ds.get("name") or "unnamed"
    if transport == "stdio":
        command = creds.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("stdio transport requires credentials.command")
        args = creds.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("credentials.args must be a list of strings")
        env = creds.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("credentials.env must be an object")
        return MCPServerConfig(
            name=name, transport="stdio", command=command,
            args=list(args), env={str(k): str(v) for k, v in env.items()},
        )

    url = ds.get("connection_url")
    if not url:
        raise ValueError(f"{transport} transport requires connection_url")
    headers: Dict[str, str] = {}
    auth = creds.get("auth") or {}
    if auth.get("type") == "bearer" and auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth.get("type") == "headers" and isinstance(auth.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in auth["headers"].items()})
    return MCPServerConfig(name=name, transport=transport, url=url, headers=headers)


@dataclass
class _ServerHandle:
    ds: Dict[str, Any]                 # original config dict (annotated later)
    config: Optional[MCPServerConfig]  # None => parse failed
    slug: str
    status: str = "pending"            # pending|connected|unavailable: <err>
    tools: List[Any] = field(default_factory=list)  # namespaced StructuredTools
    session: Any = None
    task: Optional[asyncio.Task] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    reconnected_once: bool = False

    @property
    def name(self) -> str:
        return self.ds.get("name") or "unnamed"


class MCPManager:
    """Holds all mcp-type datasources for one job/session."""

    def __init__(self, ds_configs: List[Dict[str, Any]]):
        self._handles: List[_ServerHandle] = []
        taken_slugs: set = set()
        for ds in ds_configs:
            slug = mcp_server_slug(ds.get("name") or "server")
            while slug in taken_slugs:  # two servers with the same name
                slug = mcp_server_slug(slug + "_2", max_len=18)
            taken_slugs.add(slug)
            try:
                config = parse_mcp_config(ds)
            except ValueError as e:
                handle = _ServerHandle(ds=ds, config=None, slug=slug,
                                       status=f"unavailable: invalid config ({e})")
                handle.ready.set()
                self._handles.append(handle)
                continue
            self._handles.append(_ServerHandle(ds=ds, config=config, slug=slug))

    # -- lifecycle ---------------------------------------------------------

    async def connect_all(self) -> None:
        """Connect every server concurrently; never raises. 10s bound each."""
        for h in self._handles:
            if h.config is None:
                continue
            h.task = asyncio.create_task(
                self._run_server(h), name=f"mcp-owner-{h.slug}"
            )
        await asyncio.gather(
            *(self._await_ready(h) for h in self._handles if h.config is not None)
        )
        connected = [h.name for h in self._handles if h.status == "connected"]
        failed = {h.name: h.status for h in self._handles if h.status != "connected"}
        logger.info("MCP connect_all: connected=%s failed=%s", connected, failed)

    async def _await_ready(self, h: _ServerHandle) -> None:
        try:
            await asyncio.wait_for(h.ready.wait(), timeout=MCP_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            h.status = f"unavailable: connect timed out after {int(MCP_CONNECT_TIMEOUT)}s"
            if h.task:
                h.task.cancel()

    async def _run_server(self, h: _ServerHandle) -> None:
        """Owner task: enters/exits all anyio contexts for one server."""
        cfg = h.config
        try:
            async with AsyncExitStack() as stack:
                if cfg.transport == "stdio":
                    from mcp import StdioServerParameters
                    from mcp.client.stdio import get_default_environment, stdio_client

                    params = StdioServerParameters(
                        command=cfg.command,
                        args=cfg.args,
                        # Server creds go ONLY into the subprocess env, layered
                        # over the minimal default (PATH/HOME) npx/uvx need.
                        env={**get_default_environment(), **cfg.env},
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif cfg.transport == "sse":
                    from mcp.client.sse import sse_client

                    read, write = await stack.enter_async_context(
                        sse_client(cfg.url, headers=cfg.headers or None)
                    )
                else:  # http (streamable-http)
                    from mcp.client.streamable_http import streamablehttp_client

                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(cfg.url, headers=cfg.headers or None)
                    )

                from mcp import ClientSession

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                h.session = session

                from langchain_mcp_adapters.tools import load_mcp_tools

                raw_tools = await load_mcp_tools(session)
                h.tools = self._namespace_and_wrap(h, raw_tools)
                h.status = "connected"
                h.ready.set()
                await h.shutdown.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # anyio ExceptionGroups included
            if h.status == "pending":
                h.status = f"unavailable: {type(e).__name__}: {str(e)[:200]}"
            logger.warning("MCP server %s failed: %s", h.name, h.status)
        finally:
            h.session = None
            h.ready.set()

    def close(self) -> None:
        """Sync close for close_datasource_connections()' hasattr protocol."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.create_task(self.aclose())
        else:
            asyncio.run(self.aclose())

    async def aclose(self) -> None:
        for h in self._handles:
            h.shutdown.set()
        tasks = [h.task for h in self._handles if h.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- surface -----------------------------------------------------------

    def get_langchain_tools(self) -> List[Any]:
        return [t for h in self._handles if h.status == "connected" for t in h.tools]

    @property
    def statuses(self) -> Dict[str, str]:
        return {h.name: h.status for h in self._handles}

    def annotate_configs(self) -> None:
        """Write discovery results onto the ORIGINAL ds dicts so
        inject_datasource_index() can render them without a signature change."""
        for h in self._handles:
            h.ds["_mcp_status"] = h.status
            h.ds["_mcp_tools"] = [t.name for t in h.tools]

    # -- tool wrapping (guarding filled in by Task 4) ----------------------

    def _namespace_and_wrap(self, h: _ServerHandle, raw_tools: List[Any]) -> List[Any]:
        from langchain_core.tools import StructuredTool

        taken = {t.name for hh in self._handles for t in hh.tools}
        out = []
        for tool in raw_tools:
            ns_name = namespace_mcp_tool(h.slug, tool.name, taken)
            taken.add(ns_name)
            out.append(
                StructuredTool(
                    name=ns_name,
                    description=tool.description or "",
                    args_schema=tool.args_schema,
                    coroutine=self._guarded(h, tool),
                    # BaseTool.metadata — read by registry.register_mcp_tools
                    metadata={"mcp_server": h.name},
                )
            )
        return out

    def _guarded(self, h: _ServerHandle, tool: Any):
        async def _call(**kwargs):
            return await tool.coroutine(**kwargs)

        return _call
```

Update `src/tools/mcp/__init__.py` exports:

```python
from .manager import MCPManager, parse_mcp_config
from .naming import mcp_server_slug, namespace_mcp_tool

__all__ = ["MCPManager", "parse_mcp_config", "mcp_server_slug", "namespace_mcp_tool"]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: all PASS. If `StructuredTool(...)` construction complains about the args_schema type, switch to `StructuredTool.from_function(coroutine=..., name=..., description=..., args_schema=tool.args_schema)` — same shape, both are current langchain-core API.

- [ ] **Step 5: Commit**

```bash
git add src/tools/mcp/ tests/test_mcp_manager.py
git commit -m "feat(mcp-ds): MCPManager with owner-task lifecycle and stdio/http/sse discovery"
```

---

### Task 4: Tool-call guarding — timeout, one reconnect, error strings

**Files:**
- Modify: `src/tools/mcp/manager.py` (`_guarded`, add `_restart_server`)
- Test: `tests/test_mcp_manager.py` (extend)

**Interfaces:**
- Produces: guarded tool coroutines that (a) bound each call at `MCP_CALL_TIMEOUT` (60s), (b) on a dead server attempt ONE reconnect then retry once, (c) always return an error **string** rather than raising (standard tool-error result into the graph — never kills the run).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mcp_manager.py`)

```python
@pytest.mark.asyncio
async def test_tool_error_returns_string_not_raise(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    manager = MCPManager([_stdio_ds(script)])
    await manager.connect_all()
    try:
        echo = next(t for t in manager.get_langchain_tools()
                    if t.name.endswith("__echo"))
        # Kill the server behind the tool, then call: must return an error
        # string (possibly after its one reconnect attempt), never raise.
        h = manager._handles[0]
        h.shutdown.set()
        await asyncio.sleep(0.3)
        h.ds["credentials"]["args"] = [str(tmp_path / "gone.py")]  # break reconnect
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
        echo = next(t for t in manager.get_langchain_tools()
                    if t.name.endswith("__echo"))
        h = manager._handles[0]
        h.shutdown.set()          # simulate server death; script still valid
        await asyncio.sleep(0.3)
        result = await echo.coroutine(text="revived")
        assert "echo: revived" in str(result)  # reconnect path worked
        assert h.reconnected_once is True
    finally:
        await manager.aclose()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_manager.py -k "reconnect or error_returns" -v`
Expected: FAIL (bare `_call` passthrough raises / no reconnect).

- [ ] **Step 3: Implement** — replace `_guarded` and add `_restart_server`:

```python
    async def _restart_server(self, h: _ServerHandle) -> bool:
        """One-shot revival: re-parse config, spawn a fresh owner task."""
        if h.reconnected_once:
            return False
        h.reconnected_once = True
        try:
            h.config = parse_mcp_config(h.ds)
        except ValueError as e:
            h.status = f"unavailable: invalid config ({e})"
            return False
        h.status = "pending"
        h.tools = []
        h.ready = asyncio.Event()
        h.shutdown = asyncio.Event()
        h.task = asyncio.create_task(self._run_server(h), name=f"mcp-owner-{h.slug}-r")
        await self._await_ready(h)
        return h.status == "connected"

    def _guarded(self, h: _ServerHandle, tool: Any):
        """Bound + degrade: 60s/call, one reconnect, string errors only.

        Captures the ORIGINAL tool by name so a reconnect rebinds to the
        fresh session's tool rather than the dead one.
        """
        orig_name = tool.name

        async def _call(**kwargs):
            target = tool
            if h.status != "connected":
                if await self._restart_server(h):
                    # Call through the fresh session directly — h.tools were
                    # re-wrapped on reconnect; re-entering them would double-wrap.
                    try:
                        result = await asyncio.wait_for(
                            h.session.call_tool(orig_name, kwargs),
                            timeout=MCP_CALL_TIMEOUT,
                        )
                        return _content_to_str(result)
                    except Exception as e:
                        h.status = f"unavailable: {type(e).__name__}"
                        return f"MCP tool error ({h.name}/{orig_name}): {e}"
                return (
                    f"MCP tool error ({h.name}/{orig_name}): server unavailable "
                    f"({h.status}). Continue without this tool."
                )
            try:
                return await asyncio.wait_for(
                    target.coroutine(**kwargs), timeout=MCP_CALL_TIMEOUT
                )
            except asyncio.TimeoutError:
                return (
                    f"MCP tool error ({h.name}/{orig_name}): timed out after "
                    f"{int(MCP_CALL_TIMEOUT)}s"
                )
            except Exception as e:
                h.status = f"unavailable: {type(e).__name__}: {str(e)[:200]}"
                if await self._restart_server(h):
                    try:
                        result = await asyncio.wait_for(
                            h.session.call_tool(orig_name, kwargs),
                            timeout=MCP_CALL_TIMEOUT,
                        )
                        h.status = "connected"
                        return _content_to_str(result)
                    except Exception as e2:
                        h.status = f"unavailable: {type(e2).__name__}"
                        return f"MCP tool error ({h.name}/{orig_name}): {e2}"
                return f"MCP tool error ({h.name}/{orig_name}): {e}"

        return _call
```

Add module-level helper:

```python
def _content_to_str(result: Any) -> str:
    """Flatten an mcp CallToolResult to text (mirrors what the adapter does)."""
    try:
        parts = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        return "\n".join(parts) if parts else str(result)
    except Exception:
        return str(result)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_mcp_manager.py -v`
Expected: all PASS (including Task 3's).

- [ ] **Step 5: Commit**

```bash
git add src/tools/mcp/manager.py tests/test_mcp_manager.py
git commit -m "feat(mcp-ds): guard MCP tool calls — 60s bound, one reconnect, string errors"
```

---

### Task 5: Registry — dynamic registration, load branch, wildcard expansion

**Files:**
- Modify: `src/tools/registry.py` (registration helper + `load_tools` branch + `expand_tool_wildcards`; category branches start ~line 352)
- Test: `tests/test_mcp_registry.py`

**Interfaces:**
- Consumes: `MCPManager.get_langchain_tools()`, `.statuses` (Tasks 3–4); `TOOL_REGISTRY`, `get_tools_by_category` (existing, registry.py:107).
- Produces:
  - `register_mcp_tools(manager) -> None` — purges stale `category=="mcp"` entries then registers each discovered tool: `{"category": "mcp", "phases": ["strategic", "tactical"], "description": ..., "mcp_server": <name>}`. Idempotent.
  - `expand_tool_wildcards(tool_names: List[str]) -> List[str]` — replaces `"*"` with all registered mcp-category names (deduped). MUST run before `load_tools` (which raises on unknown names, registry.py:~323) and before `filter_tools_by_phase` (which silently drops unknown names).
  - `load_tools` handles an `"mcp"` category group by pulling live tools from `context.get_datasource("mcp")` (warn-not-raise when absent, matching the sibling branches).

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_registry.py`:

```python
"""Dynamic MCP tool registration + wildcard expansion (registry side)."""

from types import SimpleNamespace

import pytest

from src.tools.registry import (
    TOOL_REGISTRY,
    expand_tool_wildcards,
    filter_tools_by_phase,
    load_tools,
    register_mcp_tools,
)
from src.tools.context import ToolContext


class FakeTool(SimpleNamespace):
    pass


class FakeManager:
    def __init__(self, names):
        self._tools = [
            FakeTool(name=n, description=f"desc {n}", args_schema=None,
                     coroutine=None)
            for n in names
        ]
        self.statuses = {"fake": "connected"}

    def get_langchain_tools(self):
        return self._tools


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in [n for n, m in TOOL_REGISTRY.items() if m.get("category") == "mcp"]:
        del TOOL_REGISTRY[name]


def test_register_and_purge_idempotent():
    register_mcp_tools(FakeManager(["mcp__a__one", "mcp__a__two"]))
    assert TOOL_REGISTRY["mcp__a__one"]["category"] == "mcp"
    register_mcp_tools(FakeManager(["mcp__a__three"]))  # rebind: purge stale
    assert "mcp__a__one" not in TOOL_REGISTRY
    assert "mcp__a__three" in TOOL_REGISTRY


def test_expand_wildcard():
    register_mcp_tools(FakeManager(["mcp__a__one"]))
    assert expand_tool_wildcards(["read_file", "*"]) == ["read_file", "mcp__a__one"]
    assert expand_tool_wildcards(["read_file"]) == ["read_file"]


def test_mcp_tools_pass_phase_filter():
    register_mcp_tools(FakeManager(["mcp__a__one"]))
    assert filter_tools_by_phase(["mcp__a__one"], "strategic") == ["mcp__a__one"]
    assert filter_tools_by_phase(["mcp__a__one"], "tactical") == ["mcp__a__one"]


def test_load_tools_pulls_from_manager():
    manager = FakeManager(["mcp__a__one", "mcp__a__two"])
    register_mcp_tools(manager)
    context = ToolContext(datasources={"mcp": manager})
    tools = load_tools(["mcp__a__one"], context)
    assert [t.name for t in tools] == ["mcp__a__one"]


def test_load_tools_without_manager_warns_not_raises():
    register_mcp_tools(FakeManager(["mcp__a__one"]))
    tools = load_tools(["mcp__a__one"], ToolContext())
    assert tools == []
```

Check `ToolContext`'s datasource-holding field name first: `rg -n "datasources" src/tools/context.py | head -5` — the constructor kwarg above must match (it backs `has_datasource`/`get_datasource`, context.py:221/232). Adjust the fixture if the field is named differently.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'register_mcp_tools'`

- [ ] **Step 3: Implement**

In `src/tools/registry.py`, after `get_categories()` (~line 127):

```python
def register_mcp_tools(manager: Any) -> None:
    """(Re)register discovered MCP tools into TOOL_REGISTRY.

    Called after MCPManager.connect_all(). Purges every existing
    category=="mcp" entry first so session rebinds and reconnects never
    leave stale names behind. Per-process mutation is safe — the agent
    process serves one job/thread.
    """
    for name in [n for n, m in TOOL_REGISTRY.items() if m.get("category") == "mcp"]:
        del TOOL_REGISTRY[name]
    for tool in manager.get_langchain_tools():
        TOOL_REGISTRY[tool.name] = {
            "category": "mcp",
            "phases": ["strategic", "tactical"],
            "description": (tool.description or "")[:200],
            "mcp_server": (getattr(tool, "metadata", None) or {}).get("mcp_server"),
        }
    logger.info(
        "Registered %d MCP tools (statuses: %s)",
        len(manager.get_langchain_tools()),
        getattr(manager, "statuses", {}),
    )


def expand_tool_wildcards(tool_names: List[str]) -> List[str]:
    """Expand the datasource_tool_categories() "*" sentinel for the mcp
    category into the discovered tool names. MUST run before
    filter_tools_by_phase (silently drops unknown names) and load_tools
    (raises on unknown names)."""
    if "*" not in tool_names:
        return tool_names
    out = [n for n in tool_names if n != "*"]
    for name in get_tools_by_category("mcp"):
        if name not in out:
            out.append(name)
    return out
```

In `load_tools`, insert an `mcp` branch alongside the sibling category branches (place it after the `webdav` branch; mirror its warn-posture):

```python
    # MCP tools (dynamic, from user-attached MCP servers)
    if "mcp" in tools_by_category:
        if not context.has_datasource("mcp"):
            logger.warning("MCP tools require an mcp datasource connection in ToolContext")
        else:
            manager = context.get_datasource("mcp")
            requested = set(tools_by_category["mcp"])
            for tool in manager.get_langchain_tools():
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded MCP tool: {tool.name}")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_mcp_registry.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/tools/registry.py tests/test_mcp_registry.py
git commit -m "feat(mcp-ds): dynamic MCP tool registration, load_tools branch, wildcard expansion"
```

---

### Task 6: process_datasources routing + datasources.md index section

**Files:**
- Modify: `src/core/datasource_setup.py` (`process_datasources` ~line 198, `inject_datasource_index` ~line 997)
- Modify: `docs/features/mcp_datasources.md` (truth-up, see Step 5)
- Test: `tests/test_mcp_datasource_setup.py`

**Interfaces:**
- Consumes: `MCPManager` (Task 3).
- Produces: `process_datasources` returns `datasources_dict["mcp"] = MCPManager(mcp_list)` (constructor only — **no I/O**; the async connect happens in Task 7's agent hook). `inject_datasource_index` renders an `### MCP Servers` section from the `_mcp_status`/`_mcp_tools` annotations.

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_datasource_setup.py`:

```python
"""mcp-type routing through process_datasources + index rendering."""

from src.core.datasource_setup import inject_datasource_index, process_datasources
from src.tools.mcp.manager import MCPManager


def _mcp_ds(name="GitHub", status=None, tools=None):
    ds = {
        "type": "mcp",
        "name": name,
        "connection_url": "https://example.com/mcp",
        "credentials": {"transport": "http"},
    }
    if status is not None:
        ds["_mcp_status"] = status
        ds["_mcp_tools"] = tools or []
    return ds


class FakeWS:
    def __init__(self):
        self.files = {}

    def read_file(self, path):
        return self.files.get(path, "")

    def write_file(self, path, content):
        self.files[path] = content


def test_process_datasources_creates_manager_without_io():
    conns, clients, _ = process_datasources([_mcp_ds()])
    assert isinstance(conns["mcp"], MCPManager)
    assert "mcp" not in clients


def test_process_datasources_groups_all_mcp_into_one_manager():
    conns, _, _ = process_datasources([_mcp_ds("A"), _mcp_ds("B")])
    assert len(conns["mcp"]._handles) == 2


def test_index_renders_connected_server():
    ws = FakeWS()
    inject_datasource_index(
        [_mcp_ds(status="connected",
                 tools=["mcp__github__create_issue", "mcp__github__get_issue"])],
        ws,
    )
    text = ws.files["datasources.md"]
    assert "### MCP Servers" in text
    assert "**GitHub** (mcp, 2 tools)" in text
    assert "`mcp__github__create_issue`" in text


def test_index_marks_unavailable_server():
    ws = FakeWS()
    inject_datasource_index([_mcp_ds(status="unavailable: connect timed out")], ws)
    text = ws.files["datasources.md"]
    assert "unavailable: connect timed out" in text
    assert "tools)" not in text  # no tool count implied


def test_index_caps_long_tool_lists_at_40():
    tools = [f"mcp__big__tool_{i}" for i in range(50)]
    ws = FakeWS()
    inject_datasource_index([_mcp_ds(status="connected", tools=tools)], ws)
    text = ws.files["datasources.md"]
    assert "mcp__big__tool_39" in text
    assert "mcp__big__tool_40" not in text
    assert "+10 more" in text
```

Check `inject_datasource_index`'s workspace-manager usage first (`sed -n '997,1010p' src/core/datasource_setup.py` shows `read_file`/`write_file` via the rewrite logic) — align `FakeWS` with the methods actually called.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_datasource_setup.py -v`
Expected: FAIL — no `"mcp"` key in connections; no MCP section in index output.

- [ ] **Step 3: Implement routing** — in `process_datasources`, add an `mcp_list` accumulator next to `generic_list` (~line 233), route in the type dispatch (after the `elif ds_type == "kb":` branch):

```python
        elif ds_type == "mcp":
            mcp_list.append(ds)
```

After the connector loop (before `return`):

```python
    # MCP servers: one manager holds ALL of them (ToolContext slots are
    # TYPE-keyed). Constructor only parses configs — the async connect
    # happens in the agent's setup hook (docs/features/mcp_datasources.md).
    if mcp_list:
        from src.tools.mcp import MCPManager

        datasources_dict["mcp"] = MCPManager(mcp_list)
```

- [ ] **Step 4: Implement index section** — in `inject_datasource_index`: add `mcps = [ds for ds in ds_configs if ds.get("type") == "mcp"]` next to the other buckets, add `"mcp"` to the `others` exclusion tuple, and render after the `databases` block:

```python
    if mcps:
        lines.append("### MCP Servers")
        for ds in mcps:
            name = ds.get("name", "Unnamed")
            status = ds.get("_mcp_status") or "not connected yet"
            tools = ds.get("_mcp_tools") or []
            if status == "connected":
                shown = ", ".join(f"`{t}`" for t in tools[:40])
                more = f" (+{len(tools) - 40} more)" if len(tools) > 40 else ""
                lines.append(f"- **{name}** (mcp, {len(tools)} tools) — {shown}{more}")
            else:
                lines.append(f"- **{name}** (mcp) — {status}")
        lines.append("")
```

- [ ] **Step 5: Truth-up the design doc** — in `docs/features/mcp_datasources.md`, "Surfacing to the LLM": no generic datasource→KB-entry machinery exists in the codebase (only OKF-kb reindex paths), so change the KB-entry bullet to a **Fast-Follows** item and state that v1 surfaces tools via the `datasources.md` index only (capped at 40 names per server).

- [ ] **Step 6: Run to verify pass**

Run: `python -m pytest tests/test_mcp_datasource_setup.py tests/test_datasource_tool_categories.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/core/datasource_setup.py tests/test_mcp_datasource_setup.py docs/features/mcp_datasources.md
git commit -m "feat(mcp-ds): route mcp datasources into MCPManager + index section"
```

---

### Task 7: Agent job path — connect, register, expand, cleanup

**Files:**
- Modify: `src/agent.py` (`_setup_job_tools` ~line 2557: after `process_datasources(...)` / before `inject_datasource_index(...)`; tool-name assembly ~line 2795)
- Test: `tests/test_mcp_agent_wiring.py`

**Interfaces:**
- Consumes: `MCPManager.connect_all/annotate_configs` (Task 3), `register_mcp_tools`, `expand_tool_wildcards` (Task 5), `get_all_tool_names` (`src/core/loader.py`, existing).
- Produces: a running job connects MCP servers before the index is written and binds discovered tools; `_close_datasource_connections` (agent.py:3374) needs **no change** — `MCPManager.close()` satisfies its `hasattr(conn, "close")` protocol.

- [ ] **Step 1: Write the failing test** — an end-to-end slice below the agent class: config dicts → process → connect → register → expand → load, using the Task 3 echo server.

`tests/test_mcp_agent_wiring.py`:

```python
"""Job-path slice: process_datasources -> connect_all -> register ->
expand wildcard -> load_tools, with a real stdio echo server."""

import sys
import textwrap

import pytest

from src.core.datasource_setup import process_datasources
from src.tools.context import ToolContext
from src.tools.registry import (
    TOOL_REGISTRY,
    expand_tool_wildcards,
    load_tools,
    register_mcp_tools,
)

ECHO_SERVER = textwrap.dedent(
    """
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("echo")

    @mcp.tool()
    def echo(text: str) -> str:
        \"\"\"Echo the input back.\"\"\"
        return f"echo: {text}"

    mcp.run(transport="stdio")
    """
)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in [n for n, m in TOOL_REGISTRY.items() if m.get("category") == "mcp"]:
        del TOOL_REGISTRY[name]


@pytest.mark.asyncio
async def test_full_job_path_slice(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER)
    ds = {
        "type": "mcp", "name": "Echo", "connection_url": None,
        "credentials": {"transport": "stdio", "command": sys.executable,
                        "args": [str(script)], "env": {}},
    }
    conns, _, _ = process_datasources([ds])
    manager = conns["mcp"]
    await manager.connect_all()
    try:
        register_mcp_tools(manager)
        manager.annotate_configs()
        assert ds["_mcp_status"] == "connected"

        # The orchestrator override delivers ["*"] for the mcp category.
        names = expand_tool_wildcards(["*"])
        assert names == ["mcp__echo__echo"]

        tools = load_tools(names, ToolContext(datasources={"mcp": manager}))
        result = await tools[0].coroutine(text="hi")
        assert "echo: hi" in str(result)
    finally:
        await manager.aclose()
```

(Adjust the `ToolContext(datasources=...)` kwarg to the field name confirmed in Task 5.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_agent_wiring.py -v`
Expected: PASS is actually possible here since Tasks 3–6 landed — if it passes, good: it pins the contract. The agent wiring itself (next step) is covered by this plus manual k3d (Task 14).

- [ ] **Step 3: Wire the agent** — in `_setup_job_tools`, immediately after the `self._datasource_connections.update(datasources_dict)` / `self._datasource_clients.update(client_registry)` pair and **before** `inject_datasource_index(ds_configs, ws)`:

```python
        # MCP servers: async connect + dynamic tool registration must happen
        # BEFORE inject_datasource_index() so the index shows discovered tool
        # names (or an unavailable marker). Graceful: connect_all never raises.
        mcp_manager = datasources_dict.get("mcp")
        if mcp_manager is not None:
            from .tools.registry import register_mcp_tools

            await mcp_manager.connect_all()
            register_mcp_tools(mcp_manager)
            mcp_manager.annotate_configs()
```

In the tool-name assembly (~line 2795), wrap with expansion:

```python
        from .tools.registry import expand_tool_wildcards, filter_tools_by_backend

        tool_names = expand_tool_wildcards(get_all_tool_names(self.config))
```

(`filter_tools_by_backend` is already imported there — merge the import line.)

- [ ] **Step 4: Verify cleanup path needs no change**

Read `src/agent.py:3374` `_close_datasource_connections`: the `hasattr(conn, "close")` loop covers the manager (sync `close()` schedules `aclose()` on the running loop). Confirm no separate client entry exists for `"mcp"` (Task 6 never fills `client_registry`).

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/test_mcp_agent_wiring.py tests/test_mcp_manager.py tests/test_mcp_registry.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/agent.py tests/test_mcp_agent_wiring.py
git commit -m "feat(mcp-ds): job-path wiring — connect MCP servers, register + expand tools"
```

---

### Task 8: Session paths (persistent thread start + live attach)

**Files:**
- Modify: `src/api/persistent_app.py` (thread-start datasource block)
- Modify: `src/api/persistent_session.py` (live-attach datasource block)
- Test: reuse `tests/test_mcp_agent_wiring.py` contract; session-specific assertions below.

**Interfaces:**
- Consumes: same as Task 7.
- Produces: sessions get MCP tools at thread start and on live datasource attach; live detach closes the manager.

- [ ] **Step 1: Locate the seams**

Run: `rg -n "process_datasources|get_all_tool_names|load_tools" src/api/persistent_app.py src/api/persistent_session.py`
Expected: one `process_datasources(...)` call in each file (persistent_app thread start; persistent_session `_apply_live_datasources`-style attach around its `new_conns, new_clients, ...` unpack), plus the session tool-loading site (`_load_tools_for_backend` in persistent_session.py).

- [ ] **Step 2: Apply the same three additions in BOTH files**

Directly after each `process_datasources(...)` result is stored (both call sites are in async functions — verify with the surrounding `async def`):

```python
        mcp_manager = <the-connections-dict>.get("mcp")
        if mcp_manager is not None:
            from src.tools.registry import register_mcp_tools

            await mcp_manager.connect_all()
            register_mcp_tools(mcp_manager)
            mcp_manager.annotate_configs()
```

At the session's tool-name assembly (wherever the flattened name list feeds `load_tools` / `filter_tools_by_phase`):

```python
        from src.tools.registry import expand_tool_wildcards

        tool_names = expand_tool_wildcards(tool_names)
```

For live detach: confirm the detach path funnels through `close_datasource_connections` (datasource_setup.py:300) or the same `hasattr(conn, "close")` idiom — if yes, no change (manager.close() handles it). If the session path closes by type-specific code, add the same `hasattr` treatment for the `"mcp"` key.

- [ ] **Step 3: Watch out for `_apply_datasource_enrichment_to_resolved`** (persistent_app.py:1241): it folds `ds_tool_categories` into the resolved config's `agent.tools` dict — the `["*"]` sentinel flows through it as data, no change needed. Confirm by reading it; if any code path validates tool names before the agent expands, apply `expand_tool_wildcards` there instead.

- [ ] **Step 4: Run session-adjacent suites**

Run: `python -m pytest tests/test_live_datasource_update.py tests/test_mcp_agent_wiring.py -v`
Expected: PASS (live-update tests confirm the category dict changes didn't break attach plumbing).

- [ ] **Step 5: Commit**

```bash
git add src/api/persistent_app.py src/api/persistent_session.py
git commit -m "feat(mcp-ds): session thread-start and live-attach MCP wiring"
```

---

### Task 9: Orchestrator API — gates, validation, payload passthrough

**Files:**
- Modify: `orchestrator/main.py` (feature-flag helpers near line 1292; `create_datasource` valid_types ~line 15849; the PUT update endpoint — locate: `rg -n '@app.put\("/api/datasources' orchestrator/main.py`)
- Test: `tests/test_mcp_datasource_api.py`

**Interfaces:**
- Consumes: env vars `MCP_DATASOURCES_ENABLED`, `MCP_STDIO_ENABLED`.
- Produces: `_mcp_datasources_enabled() -> bool`, `_mcp_stdio_enabled() -> bool`, `_validate_mcp_datasource(connection_url, credentials) -> None` (raises `HTTPException(400)`); type `mcp` accepted when gated on; `_build_datasources_payload` passes mcp credentials through untouched (mcp is NOT in `managed_types`, main.py:~15515 — verify, don't change).

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_datasource_api.py` — mirror the fixture/bootstrap pattern of `tests/test_kb_datasource_api.py` (read it first; reuse its app/client fixtures). Test bodies:

```python
class TestMcpValidation:
    def test_mcp_rejected_when_gate_off(self, client, monkeypatch):
        monkeypatch.delenv("MCP_DATASOURCES_ENABLED", raising=False)
        r = client.post("/api/datasources", json={
            "name": "GH", "type": "mcp",
            "connection_url": "https://example.com/mcp", "credentials": {}})
        assert r.status_code == 403

    def test_remote_mcp_created_when_gate_on(self, client, monkeypatch):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        r = client.post("/api/datasources", json={
            "name": "GH", "type": "mcp",
            "connection_url": "https://example.com/mcp",
            "credentials": {"transport": "http",
                            "auth": {"type": "bearer", "token": "t"}}})
        assert r.status_code in (200, 201)

    def test_remote_requires_url(self, client, monkeypatch):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        r = client.post("/api/datasources", json={
            "name": "GH", "type": "mcp", "connection_url": None,
            "credentials": {"transport": "http"}})
        assert r.status_code == 400

    def test_stdio_rejected_when_stdio_gate_off(self, client, monkeypatch):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        monkeypatch.delenv("MCP_STDIO_ENABLED", raising=False)
        r = client.post("/api/datasources", json={
            "name": "Local", "type": "mcp", "connection_url": None,
            "credentials": {"transport": "stdio", "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-github"]}})
        assert r.status_code == 400
        assert "stdio" in r.json()["detail"].lower()

    def test_stdio_requires_command(self, client, monkeypatch):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
        r = client.post("/api/datasources", json={
            "name": "Local", "type": "mcp", "connection_url": None,
            "credentials": {"transport": "stdio"}})
        assert r.status_code == 400

    def test_unknown_transport_rejected(self, client, monkeypatch):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        r = client.post("/api/datasources", json={
            "name": "GH", "type": "mcp",
            "connection_url": "https://example.com/mcp",
            "credentials": {"transport": "pigeon"}})
        assert r.status_code == 400
```

Add one payload test asserting mcp credentials survive `_build_datasources_payload` (import it directly):

```python
def test_payload_forwards_mcp_credentials():
    from orchestrator.main import _build_datasources_payload
    creds = {"transport": "stdio", "command": "npx", "args": [], "env": {"K": "v"}}
    payload = _build_datasources_payload([{
        "id": "x", "type": "mcp", "name": "Local", "connection_url": None,
        "credentials": creds, "project_read_only": False}])
    assert payload[0]["credentials"] == creds
```

(If importing `orchestrator.main` at module scope is heavy in the existing suite, mirror however `test_kb_datasource_api.py` imports orchestrator internals.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_datasource_api.py -v`
Expected: FAIL — `Invalid type 'mcp'` (400 instead of 403/201 paths).

- [ ] **Step 3: Implement** — next to the `SKILLS_DB_ENABLED` helper (main.py:1292 pattern):

```python
def _mcp_datasources_enabled() -> bool:
    """User-added MCP servers (docs/features/mcp_datasources.md)."""
    return os.getenv("MCP_DATASOURCES_ENABLED", "").lower().strip() in ("true", "1", "yes")


def _mcp_stdio_enabled() -> bool:
    """stdio-transport MCP servers run third-party code in the agent pod —
    separately gated so hosted deployments can stay remote-only."""
    return os.getenv("MCP_STDIO_ENABLED", "").lower().strip() in ("true", "1", "yes")


def _validate_mcp_datasource(connection_url: str | None, credentials: dict) -> None:
    """Shape-check an mcp datasource body. Raises HTTPException(400)."""
    transport = (credentials.get("transport") or "http").lower()
    if transport not in ("http", "sse", "stdio"):
        raise HTTPException(status_code=400,
                            detail=f"Invalid MCP transport '{transport}' (http, sse, stdio)")
    if transport == "stdio":
        if not _mcp_stdio_enabled():
            raise HTTPException(status_code=400,
                                detail="stdio MCP servers are disabled on this deployment")
        command = credentials.get("command")
        if not command or not isinstance(command, str):
            raise HTTPException(status_code=400,
                                detail="stdio MCP servers require credentials.command")
        args = credentials.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise HTTPException(status_code=400,
                                detail="credentials.args must be a list of strings")
    else:
        if not connection_url:
            raise HTTPException(status_code=400,
                                detail=f"{transport} MCP servers require connection_url")
```

In `create_datasource`: add `"mcp"` to `valid_types`, then after the existing kb/job_id check:

```python
    if body.type == "mcp":
        if not _mcp_datasources_enabled():
            raise HTTPException(status_code=403,
                                detail="MCP datasources are disabled on this deployment")
        _validate_mcp_datasource(body.connection_url, body.credentials or {})
```

In the PUT update endpoint: apply the same block when the (possibly updated) type is `mcp`, validating the merged connection_url/credentials.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_mcp_datasource_api.py tests/test_datasource_access.py tests/test_datasource_credentials_encryption.py -v`
Expected: all PASS (encryption tests confirm mcp credentials ride the existing encrypt-at-rest path with zero changes).

- [ ] **Step 5: Verify the capability grant covers MCP** — `rg -n "datasource_tools" src/core/capability_grants.py` and read the enforcement: it keys off datasource-derived tool categories generically. If any hardcoded type/category set exists there, add `"mcp"`; otherwise no change. Note the finding in the commit message.

- [ ] **Step 5b: Verify `connection_url` is nullable** — stdio datasources store `connection_url = NULL`. Run `rg -n "connection_url" orchestrator/init.py | head -5` and check the `datasources` CREATE TABLE / migration: if the column is `NOT NULL`, add a migration dropping the constraint (the datasource redesign spec already required this for `generic` — it may have landed; the Step 1 stdio-creation test will catch it either way).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py tests/test_mcp_datasource_api.py
git commit -m "feat(mcp-ds): orchestrator gates + mcp type validation (create/update)"
```

---

### Task 10: Connection-test endpoint

**Files:**
- Modify: `orchestrator/main.py` (`test_datasource`, line 16238 — add branch before the `("generic", "repository")` catch-all)
- Test: `tests/test_mcp_datasource_api.py` (extend)

**Interfaces:**
- Produces: `POST /api/datasources/{id}/test` for mcp returns `{"status": "ok", "message": "Connected: N tools (a, b, c…)"}` / `{"status": "error", ...}` / `{"status": "ok", "message": "…untested here…"}` for stdio when the runtime is absent on the orchestrator.

- [ ] **Step 1: Write the failing tests** (append; reuse Task 9 fixtures + the Task 3 echo-server constant):

```python
class TestMcpConnectionTest:
    def test_stdio_echo_server_lists_tools(self, client, tmp_path, monkeypatch, make_mcp_ds):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
        script = tmp_path / "echo_server.py"
        script.write_text(ECHO_SERVER)
        ds_id = make_mcp_ds(transport="stdio", command=sys.executable,
                            args=[str(script)])
        r = client.post(f"/api/datasources/{ds_id}/test")
        body = r.json()
        assert body["status"] == "ok"
        assert "echo" in body["message"]

    def test_unreachable_remote_returns_error_not_500(self, client, monkeypatch, make_mcp_ds):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        ds_id = make_mcp_ds(transport="http",
                            connection_url="http://127.0.0.1:9/mcp")
        r = client.post(f"/api/datasources/{ds_id}/test")
        assert r.status_code == 200
        assert r.json()["status"] == "error"

    def test_stdio_missing_runtime_reports_untested(self, client, monkeypatch, make_mcp_ds):
        monkeypatch.setenv("MCP_DATASOURCES_ENABLED", "true")
        monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
        ds_id = make_mcp_ds(transport="stdio", command="definitely-not-a-binary",
                            args=[])
        r = client.post(f"/api/datasources/{ds_id}/test")
        body = r.json()
        assert body["status"] == "ok"
        assert "untested" in body["message"].lower()
```

Write the `make_mcp_ds` factory fixture against the same DB/bootstrap the file's other fixtures use.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_mcp_datasource_api.py -k McpConnectionTest -v`
Expected: FAIL — mcp falls through to the generic branch.

- [ ] **Step 3: Implement** — insert before the `elif ds_type in ("generic", "repository"):` branch:

```python
        elif ds_type == "mcp":
            return await _test_mcp_datasource(url, creds)
```

And the helper (place near `test_datasource`):

```python
async def _test_mcp_datasource(url: str | None, creds: dict) -> dict[str, Any]:
    """Connect to an MCP server and list its tools (10s bound).

    stdio: only attempted when the command exists on the orchestrator —
    otherwise report untested (the agent pod is the real runtime).
    """
    import shutil
    from contextlib import AsyncExitStack

    transport = (creds.get("transport") or "http").lower()

    async def _probe() -> dict[str, Any]:
        async with AsyncExitStack() as stack:
            if transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import get_default_environment, stdio_client

                params = StdioServerParameters(
                    command=creds["command"], args=creds.get("args") or [],
                    env={**get_default_environment(),
                         **{str(k): str(v) for k, v in (creds.get("env") or {}).items()}},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                headers: dict[str, str] = {}
                auth = creds.get("auth") or {}
                if auth.get("type") == "bearer" and auth.get("token"):
                    headers["Authorization"] = f"Bearer {auth['token']}"
                elif auth.get("type") == "headers":
                    headers.update(auth.get("headers") or {})
                if transport == "sse":
                    from mcp.client.sse import sse_client

                    read, write = await stack.enter_async_context(
                        sse_client(url, headers=headers or None))
                else:
                    from mcp.client.streamable_http import streamablehttp_client

                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(url, headers=headers or None))
            from mcp import ClientSession

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
            names = [t.name for t in listing.tools]
            preview = ", ".join(names[:8]) + (", …" if len(names) > 8 else "")
            return {"status": "ok",
                    "message": f"Connected: {len(names)} tools ({preview})"}

    if transport == "stdio" and not shutil.which(creds.get("command") or ""):
        return {"status": "ok",
                "message": "stdio server untested here (runtime not on the "
                           "orchestrator); it will resolve on the agent at job start"}
    try:
        return await asyncio.wait_for(_probe(), timeout=10)
    except asyncio.TimeoutError:
        return {"status": "error", "message": "MCP connect timed out after 10s"}
    except Exception as e:
        return {"status": "error", "message": str(e)[-2000:]}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_mcp_datasource_api.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_mcp_datasource_api.py
git commit -m "feat(mcp-ds): MCP branch in the connection-test endpoint"
```

---

### Task 11: Helm gates

**Files:**
- Modify: `helm/values.yaml` (agent block, near `skillsDbEnabled` line ~199)
- Modify: `helm/templates/configmap.yaml` (next to the `SKILLS_DB_ENABLED` entry)
- Modify: `helm/templates/orchestrator/deployment.yaml` (env list, after `SKILLS_DB_ENABLED` ~line 114)

- [ ] **Step 1: values.yaml** — under the same block as `skillsDbEnabled`:

```yaml
  # User-added MCP servers as datasources (docs/features/mcp_datasources.md).
  # Both default off; flip mcpDatasourcesEnabled per environment. stdio runs
  # third-party code in the agent pod — keep off for hosted/multi-tenant.
  mcpDatasourcesEnabled: "false"
  mcpStdioEnabled: "false"
```

- [ ] **Step 2: configmap.yaml** — mirror the `SKILLS_DB_ENABLED` line's exact idiom:

```yaml
  MCP_DATASOURCES_ENABLED: {{ .Values.agent.mcpDatasourcesEnabled | default "false" | quote }}
  MCP_STDIO_ENABLED: {{ .Values.agent.mcpStdioEnabled | default "false" | quote }}
```

- [ ] **Step 3: orchestrator deployment env** — after the `SKILLS_DB_ENABLED` block:

```yaml
            - name: MCP_DATASOURCES_ENABLED
              valueFrom:
                configMapKeyRef:
                  name: {{ include "srw.configMapName" . }}
                  key: MCP_DATASOURCES_ENABLED
            - name: MCP_STDIO_ENABLED
              valueFrom:
                configMapKeyRef:
                  name: {{ include "srw.configMapName" . }}
                  key: MCP_STDIO_ENABLED
```

- [ ] **Step 4: Verify rendering**

Run: `helm template helm/ 2>/dev/null | grep -A3 MCP_DATASOURCES_ENABLED`
Expected: the configmap entry (`"false"`) and both env blocks render. (If `helm template` needs required values, use the same invocation the repo's chart CI/docs use — check `helm/README.md`.)

- [ ] **Step 5: Commit**

```bash
git add helm/values.yaml helm/templates/configmap.yaml helm/templates/orchestrator/deployment.yaml
git commit -m "feat(mcp-ds): helm gates MCP_DATASOURCES_ENABLED / MCP_STDIO_ENABLED"
```

Note: enabling on dev happens in the GitOps values overlay (deployment repo), not in chart defaults — leave defaults `"false"`.

---

### Task 12: Agent image runtimes (node/npx + uv/uvx)

**Files:**
- Modify: `docker/Dockerfile.agent` (production stage — the second `FROM python:3.11-slim`)

- [ ] **Step 1: Add runtimes** — in the production stage, alongside its existing `apt-get install` run (merge into it if one exists; otherwise add after the user-creation step):

```dockerfile
# MCP stdio runtimes (docs/features/mcp_datasources.md): npx for npm-ecosystem
# servers, uvx for python-ecosystem servers. Debian-pinned versions; the
# subprocess runs as the srw user with only the datasource's env vars.
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv
```

- [ ] **Step 2: Build + verify**

Run: `podman build -t srw-agent-mcp-test -f docker/Dockerfile.agent . && podman run --rm srw-agent-mcp-test sh -c 'npx --version && uvx --version && python -c "import mcp, langchain_mcp_adapters; print(\"deps ok\")"'`
Expected: three version/ok lines, exit 0. (Heavy pip installs can false-flag the shell stall detector — it keeps running; wait it out.)

- [ ] **Step 3: Commit**

```bash
git add docker/Dockerfile.agent
git commit -m "feat(mcp-ds): node/npx + uv/uvx in agent image for stdio MCP servers"
```

---

### Task 13: Cockpit — MCP form, rename, i18n

**Files:**
- Modify: `cockpit/src/app/views/datasources/datasource-list.component.ts` (2943 lines; inline template — type `<select>` optgroups, `formData`, `onTypeSelect`, `saveForm`)
- Modify: `cockpit/src/assets/i18n/en.json` (`"datasources": "Data Sources"` at lines 314 and 1065; `datasources.form` key block ~line 1714)
- Modify: `cockpit/src/assets/i18n/de-DE.json` (same keys)
- Test: `cockpit/src/app/views/datasources/datasource-list.component.spec.ts`

- [ ] **Step 1: Read the component first** — locate: the optgroup list (`typeGroupCli` etc.), the `formData` initializer + reset, `onTypeSelect`, `saveForm`'s credentials assembly per type, and whether a reusable key-value row editor exists (the generic type's env-var editor). Mirror those exact patterns; the snippets below follow the component's visible style (`app-form-field`/`app-input`/`app-select`, `@if` control flow, transloco pipes) — adapt names to what you find.

- [ ] **Step 2: Write failing specs** (mirror the existing spec file's harness):

```typescript
  it('offers the MCP type option', () => {
    // render creation form; assert an option with value "mcp" exists
  });

  it('builds http credentials with bearer auth on save', () => {
    // type=mcp, transport=http, url + token set
    // expect POST body credentials: {transport: 'http', auth: {type: 'bearer', token: 't'}}
  });

  it('builds stdio credentials with args split per line', () => {
    // transport=stdio, command 'npx', args textarea "-y\n@modelcontextprotocol/server-github"
    // expect credentials: {transport: 'stdio', command: 'npx',
    //                      args: ['-y', '@modelcontextprotocol/server-github'], env: {}}
  });
```

Fill the bodies using the spec file's existing render/interaction helpers.

Run: `cd cockpit && npx vitest run src/app/views/datasources/datasource-list.component.spec.ts`
Expected: new specs FAIL.

- [ ] **Step 3: Template additions**

New optgroup after the existing groups in the type select:

```html
<optgroup [label]="'datasources.form.typeGroupMcp' | transloco">
  <option value="mcp">{{ 'datasources.form.optMcp' | transloco }}</option>
</optgroup>
```

MCP form section (inside the form, alongside the per-type `@if` sections):

```html
@if (formData.type === 'mcp') {
  <app-form-field [label]="'datasources.form.mcpTransportLabel' | transloco" [required]="true">
    <app-select size="sm" [value]="formData.mcpTransport"
                (changed)="formData.mcpTransport = $event" [disabled]="isSaving()">
      <option value="http">{{ 'datasources.form.mcpTransportHttp' | transloco }}</option>
      <option value="sse">{{ 'datasources.form.mcpTransportSse' | transloco }}</option>
      <option value="stdio">{{ 'datasources.form.mcpTransportStdio' | transloco }}</option>
    </app-select>
  </app-form-field>

  @if (formData.mcpTransport !== 'stdio') {
    <!-- Server URL uses the existing connection_url field/input -->
    <app-form-field [label]="'datasources.form.mcpTokenLabel' | transloco">
      <app-input size="sm" type="password" [value]="formData.mcpToken"
                 (valueChange)="formData.mcpToken = $event" [disabled]="isSaving()"
                 [placeholder]="'datasources.form.mcpTokenPlaceholder' | transloco" />
    </app-form-field>
    <!-- headers key-value editor: reuse the generic env-var row editor pattern -->
  } @else {
    <app-form-field [label]="'datasources.form.mcpCommandLabel' | transloco" [required]="true">
      <app-input size="sm" [value]="formData.mcpCommand"
                 (valueChange)="formData.mcpCommand = $event" [disabled]="isSaving()"
                 [placeholder]="'datasources.form.mcpCommandPlaceholder' | transloco" />
    </app-form-field>
    <app-form-field [label]="'datasources.form.mcpArgsLabel' | transloco">
      <textarea rows="3" [value]="formData.mcpArgs" [disabled]="isSaving()"
                (input)="formData.mcpArgs = $any($event.target).value"
                [placeholder]="'datasources.form.mcpArgsPlaceholder' | transloco"></textarea>
    </app-form-field>
    <!-- env key-value editor: reuse the generic env-var row editor pattern -->
    <div class="form-hint">{{ 'datasources.form.mcpStdioWarning' | transloco }}</div>
  }
}
```

Ensure the connection-URL field's visibility condition includes mcp-with-http/sse and excludes mcp-with-stdio (extend `hasConnectionUrl()` or the surrounding `@if`).

- [ ] **Step 4: TS additions** — `formData` gains `mcpTransport: 'http'`, `mcpToken: ''`, `mcpHeaders: <kv-rows>`, `mcpCommand: ''`, `mcpArgs: ''`, `mcpEnv: <kv-rows>` (same defaults in the reset path). In `saveForm`'s credentials assembly:

```typescript
if (this.formData.type === 'mcp') {
  const creds: Record<string, unknown> = { transport: this.formData.mcpTransport };
  if (this.formData.mcpTransport === 'stdio') {
    creds['command'] = this.formData.mcpCommand.trim();
    creds['args'] = this.formData.mcpArgs.split('\n').map(a => a.trim()).filter(Boolean);
    creds['env'] = this.kvRowsToObject(this.formData.mcpEnv);  // reuse generic helper
  } else if (this.formData.mcpToken) {
    creds['auth'] = { type: 'bearer', token: this.formData.mcpToken };
  } else if (this.formData.mcpHeaders?.length) {
    creds['auth'] = { type: 'headers', headers: this.kvRowsToObject(this.formData.mcpHeaders) };
  }
  payload.credentials = creds;
}
```

(`kvRowsToObject` = whatever the generic env-var editor already uses — reuse, don't duplicate.)

- [ ] **Step 5: i18n** — in `en.json` `datasources.form` block:

```json
"typeGroupMcp": "MCP",
"optMcp": "MCP Server",
"mcpTransportLabel": "Transport",
"mcpTransportHttp": "Remote (HTTP)",
"mcpTransportSse": "Remote (SSE)",
"mcpTransportStdio": "Local command (stdio)",
"mcpTokenLabel": "Bearer Token",
"mcpTokenPlaceholder": "Optional — sent as Authorization: Bearer …",
"mcpCommandLabel": "Command",
"mcpCommandPlaceholder": "npx",
"mcpArgsLabel": "Arguments (one per line)",
"mcpArgsPlaceholder": "-y\n@modelcontextprotocol/server-github",
"mcpEnvLabel": "Environment Variables",
"mcpHeadersLabel": "Custom Headers",
"mcpStdioWarning": "This command runs inside the agent environment with the variables above. Only add servers you trust."
```

Use **Connectors** for the English section, project tab, form, picker, and
agent-setting labels; use **Konnektoren** in German. Keep `datasources`
translation keys, routes, and API/type names for compatibility. Translate the
new MCP keys in the file's existing tone (for example,
`"mcpStdioWarning": "Dieser Befehl wird innerhalb der Agent-Umgebung mit den obigen Variablen ausgeführt. Nur vertrauenswürdige Server hinzufügen."`).

- [ ] **Step 6: Run to verify pass**

Run: `cd cockpit && npx vitest run`
Expected: full suite PASS (~353 tests) including the three new specs.

- [ ] **Step 7: Commit**

```bash
git add cockpit/src/app/views/datasources/ cockpit/src/assets/i18n/
git commit -m "feat(mcp-ds): add Cockpit MCP connector form and i18n"
```

---

### Task 14: k3d live gate (manual checklist)

No code. Validates the deployed slice end to end on the local k3d stack (`tilt up`, memory: local_tilt_dev_stack_stinkpad + local_k3d_testing_via_orchestrator_api).

- [ ] Set `MCP_DATASOURCES_ENABLED=true` + `MCP_STDIO_ENABLED=true` in the local values overlay (`deployment/values-local.yaml`), `tilt up`, wait for orchestrator + agent images to rebuild (dev Dockerfiles drift — force rebuild if cached).
- [ ] Cockpit: section reads **Connectors**; create a **remote** MCP connector (any reachable streamable-HTTP server — e.g. a FastMCP echo server port-forwarded into the cluster) → **Test Connection** shows `Connected: N tools (…)`.
- [ ] Create a **stdio** MCP connector (`command: npx`, args `-y`/`@modelcontextprotocol/server-everything`) → Test reports untested-here (orchestrator) — expected.
- [ ] Link both to a project; dispatch a job: "List the tools you have from MCP servers, then call one of them."
- [ ] Verify via MCP/API: `datasources.md` in the job workspace has the `### MCP Servers` section with namespaced tool names; audit trail shows an `mcp__…` tool call succeeding.
- [ ] Break test: point a third MCP connector at an unreachable URL, dispatch a job → job runs normally, index shows `unavailable`.
- [ ] Session test: open a persistent session in a project with an MCP connector → tools available; detach the connector live → next turn has no mcp tools.
- [ ] Grants test: restrict `datasource_tools` for a test user → dispatched job binds no `mcp__…` tools.

---

## Execution order & dependencies

Tasks 1→7 are strictly ordered (each consumes the previous). Tasks 8–13 are independent of each other but all depend on ≤7 (9/10 only on 1; 11/12/13 on nothing — can interleave). Task 14 last, after a dev-stack deploy.

## Out of scope (per spec)

Per-server tool allow-list, OAuth for remote servers, MCP resources/prompts/sampling, stdio sandboxing beyond pod isolation, KB entries per server (truth-up in Task 6), session-scoped ad-hoc MCPs.
