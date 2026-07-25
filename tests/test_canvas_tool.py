"""Slice-1 agent contract for the file-backed Dynamic Canvas tools."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from src.api.orchestrator_client import CanvasClearResult, CanvasSetResult
from src.tools.canvas import create_canvas_tools, get_canvas_metadata
from src.tools.context import ToolContext
from src.tools.registry import filter_tools_by_backend, load_tools


@pytest.fixture(autouse=True)
def _canvas_internal_service_auth(monkeypatch):
    monkeypatch.setenv("MCP_INTERNAL_KEY", "canvas-test-key")


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "canvas_id": "main",
        "source": {"type": "workspace_file", "path": "output/report.md"},
        "title": "Report",
        "renderer": "markdown",
        "editable": False,
        "alt_text": None,
        "presentation_revision": 3,
        "source_version": "sha256:abc",
        "status": "ready",
        "capabilities": {
            "can_edit": False,
            "can_pop_out": True,
            "can_take_control": False,
        },
        "updated_at": "2026-07-13T10:00:00Z",
    }
    state.update(overrides)
    return state


def _tool(tools, name: str):
    return next(tool for tool in tools if tool.name == name)


class _CanvasClient:
    def __init__(self):
        self.get_result: dict[str, Any] | None = _state()
        self.set_result = CanvasSetResult(state=_state(), changed=True)
        self.clear_result: CanvasClearResult | None = None
        self.set_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []
        self.clear_calls: list[str] = []
        self.closed = False

    async def get_thread_canvas(self, thread_id: str):
        self.get_calls.append(thread_id)
        return self.get_result

    async def set_thread_canvas(self, thread_id: str, payload: dict[str, Any]):
        self.set_calls.append((thread_id, payload))
        return self.set_result

    async def clear_thread_canvas(self, thread_id: str):
        self.clear_calls.append(thread_id)
        return self.clear_result

    async def close(self):
        self.closed = True


@pytest.fixture
def canvas_client(monkeypatch):
    clients: list[_CanvasClient] = []
    identities: list[tuple[str, str | None]] = []

    def factory(config_name: str, *, user_id: str | None = None):
        identities.append((config_name, user_id))
        client = _CanvasClient()
        clients.append(client)
        return client

    monkeypatch.setattr("src.tools.canvas._new_orchestrator_client", factory)
    return clients, identities


def _context(callback=None) -> ToolContext:
    return ToolContext(
        _thread_id="thread-1",
        user_id="user-1",
        config={"agent_id": "persistent-test"},
        canvas_event_callback=callback,
    )


def _live_app_context(callback=None) -> ToolContext:
    manager = SimpleNamespace(
        is_initialized=True,
        backend=SimpleNamespace(supports_canvas_live_apps=True),
    )
    return ToolContext(
        workspace_manager=manager,
        _thread_id="thread-1",
        user_id="user-1",
        config={"agent_id": "persistent-test"},
        canvas_event_callback=callback,
    )


def _capability_context(*, live: bool, browser: bool, callback=None) -> ToolContext:
    manager = SimpleNamespace(
        is_initialized=True,
        backend=SimpleNamespace(
            supports_canvas_live_apps=live,
            supports_canvas_shared_browser=browser,
        ),
    )
    return ToolContext(
        workspace_manager=manager,
        _thread_id="thread-1",
        user_id="user-1",
        config={"agent_id": "persistent-test"},
        canvas_event_callback=callback,
    )


def test_canvas_metadata_is_a_separate_category():
    metadata = get_canvas_metadata()
    assert set(metadata) == {"get_canvas", "set_canvas", "clear_canvas"}
    assert {entry["category"] for entry in metadata.values()} == {"canvas"}


def test_set_schema_is_flat_file_only_and_exposes_conditional_editing():
    set_canvas = _tool(create_canvas_tools(_context()), "set_canvas")
    schema = set_canvas.args_schema.model_json_schema()
    assert set(schema["properties"]) == {
        "source_type",
        "path",
        "title",
        "renderer",
        "editable",
        "alt_text",
    }
    assert schema["properties"]["source_type"]["const"] == "workspace_file"
    assert "html-interactive" in schema["properties"]["renderer"]["enum"]
    assert "office" in schema["properties"]["renderer"]["enum"]
    assert "collabora" in schema["properties"]["editable"]["description"].lower()
    assert "office" in schema["properties"]["editable"]["description"].lower()
    assert "office" in schema["properties"]["alt_text"]["description"].lower()
    parsed = set_canvas.args_schema.model_validate(
        {
            "source_type": "workspace_file",
            "path": "output/report.md",
            "editable": True,
        }
    )
    assert parsed.editable is True
    with pytest.raises(ValidationError):
        set_canvas.args_schema.model_validate(
            {"source_type": "workspace_port", "path": "output/report.md"}
        )


def test_set_schema_adds_flat_port_fields_only_for_attested_backend(monkeypatch):
    # The agent consumes the orchestrator-attested backend bit; it must not
    # independently inspect the deployment environment and disagree with it.
    monkeypatch.delenv("CANVAS_LIVE_PREVIEW_ENABLED", raising=False)
    set_canvas = _tool(create_canvas_tools(_live_app_context()), "set_canvas")
    schema = set_canvas.args_schema.model_json_schema()
    assert set(schema["properties"]) == {
        "source_type",
        "path",
        "port",
        "entry_path",
        "title",
        "renderer",
        "editable",
        "alt_text",
        "new_app",
    }
    assert schema["properties"]["source_type"]["enum"] == [
        "workspace_file",
        "workspace_port",
    ]
    parsed = set_canvas.args_schema.model_validate(
        {
            "source_type": "workspace_port",
            "port": 8501,
            "entry_path": "/demo",
            "new_app": True,
        }
    )
    assert parsed.port == 8501
    assert parsed.entry_path == "/demo"
    assert parsed.new_app is True


@pytest.mark.parametrize(
    ("live", "browser", "expected_properties", "expected_source_types"),
    [
        (
            False,
            False,
            {"source_type", "path", "title", "renderer", "editable", "alt_text"},
            ["workspace_file"],
        ),
        (
            True,
            False,
            {
                "source_type",
                "path",
                "port",
                "entry_path",
                "title",
                "renderer",
                "editable",
                "alt_text",
                "new_app",
            },
            ["workspace_file", "workspace_port"],
        ),
        (
            False,
            True,
            {
                "source_type",
                "path",
                "browser_id",
                "title",
                "renderer",
                "editable",
                "alt_text",
            },
            ["workspace_file", "browser"],
        ),
        (
            True,
            True,
            {
                "source_type",
                "path",
                "port",
                "entry_path",
                "browser_id",
                "title",
                "renderer",
                "editable",
                "alt_text",
                "new_app",
            },
            ["workspace_file", "workspace_port", "browser"],
        ),
    ],
)
def test_set_schema_matches_both_attested_backend_capabilities(
    live, browser, expected_properties, expected_source_types
):
    set_canvas = _tool(
        create_canvas_tools(_capability_context(live=live, browser=browser)),
        "set_canvas",
    )
    schema = set_canvas.args_schema.model_json_schema()
    assert set(schema["properties"]) == expected_properties
    source_schema = schema["properties"]["source_type"]
    advertised = (
        [source_schema["const"]] if "const" in source_schema else source_schema["enum"]
    )
    assert advertised == expected_source_types


@pytest.mark.parametrize(
    "payload",
    [
        {"source_type": "browser"},
        {"source_type": "browser", "browser_id": "current", "path": "a.md"},
        {
            "source_type": "browser",
            "browser_id": "current",
            "alt_text": "browser",
        },
        {
            "source_type": "browser",
            "browser_id": "current",
            "renderer": "html",
        },
        {
            "source_type": "browser",
            "browser_id": "current",
            "editable": True,
        },
        {
            "source_type": "browser",
            "browser_id": "current",
            "port": 8501,
        },
        {
            "source_type": "browser",
            "browser_id": "current",
            "entry_path": "/",
        },
        {
            "source_type": "browser",
            "browser_id": "current",
            "new_app": True,
        },
        {
            "source_type": "workspace_file",
            "path": "a.md",
            "browser_id": "current",
        },
        {
            "source_type": "workspace_port",
            "port": 8501,
            "browser_id": "current",
        },
    ],
)
def test_browser_set_schema_rejects_missing_or_cross_kind_fields(payload):
    set_canvas = _tool(
        create_canvas_tools(_capability_context(live=True, browser=True)),
        "set_canvas",
    )
    with pytest.raises(ValidationError):
        set_canvas.args_schema.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"source_type": "workspace_port"},
        {"source_type": "workspace_port", "port": 8501, "path": "index.html"},
        {"source_type": "workspace_port", "port": 8501, "renderer": "html"},
        {"source_type": "workspace_port", "port": 8501, "editable": True},
        {"source_type": "workspace_port", "port": 8501, "alt_text": "app"},
        {
            "source_type": "workspace_file",
            "path": "index.html",
            "port": 8501,
        },
        {
            "source_type": "workspace_file",
            "path": "index.html",
            "entry_path": "/",
        },
        {
            "source_type": "workspace_file",
            "path": "index.html",
            "new_app": True,
        },
    ],
)
def test_live_set_schema_rejects_missing_or_cross_kind_fields(payload):
    set_canvas = _tool(create_canvas_tools(_live_app_context()), "set_canvas")
    with pytest.raises(ValidationError):
        set_canvas.args_schema.model_validate(payload)


@pytest.mark.asyncio
async def test_get_uses_delegated_identity_and_strips_browser_internal_fields(
    canvas_client, monkeypatch
):
    clients, identities = canvas_client
    get_canvas = _tool(create_canvas_tools(_context()), "get_canvas")
    client = _CanvasClient()
    client.get_result = _state(
        content_url="/api/persistent/secret-content-url",
        can_create_viewer_session=True,
        workspace_generation="workspace-secret",
        source={
            "type": "workspace_file",
            "path": "output/report.md",
            "host": "10.0.0.9",
            "fingerprint": "ssh-secret",
        },
    )

    def factory(config_name: str, *, user_id: str | None = None):
        identities.append((config_name, user_id))
        clients.append(client)
        return client

    monkeypatch.setattr("src.tools.canvas._new_orchestrator_client", factory)
    result = await get_canvas.ainvoke({})

    assert identities[0] == ("persistent-test", "user-1")
    assert result["source"] == {
        "type": "workspace_file",
        "path": "output/report.md",
    }
    assert "content_url" not in result
    assert "can_create_viewer_session" not in result
    assert "workspace_generation" not in result
    assert "host" not in result["source"]
    assert "fingerprint" not in result["source"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_get_app_state_exposes_only_logical_type_and_entry_path(
    canvas_client, monkeypatch
):
    clients, identities = canvas_client
    get_canvas = _tool(create_canvas_tools(_live_app_context()), "get_canvas")
    client = _CanvasClient()
    client.get_result = _state(
        source={
            "type": "workspace_app",
            "entry_path": "/demo",
            "manifest_path": ".srw/canvas.yaml",
            "entry_port": 8501,
            "routes": [{"path_prefix": "/api", "port": 8000}],
            "origin_generation": "origin-secret",
            "workspace_generation": "workspace-secret",
            "host": "10.0.0.9",
        },
        renderer="auto",
        source_version=None,
        content_url="https://secret-viewer.example",
    )

    def factory(config_name: str, *, user_id: str | None = None):
        identities.append((config_name, user_id))
        clients.append(client)
        return client

    monkeypatch.setattr("src.tools.canvas._new_orchestrator_client", factory)
    result = await get_canvas.ainvoke({})

    assert identities == [("persistent-test", "user-1")]
    assert result["source"] == {"type": "workspace_app", "entry_path": "/demo"}
    assert "content_url" not in result
    assert client.closed is True


@pytest.mark.asyncio
async def test_set_sends_exact_flat_payload_then_emits_invalidation(canvas_client):
    clients, identities = canvas_client
    events: list[tuple[str, dict[str, Any]]] = []
    set_canvas = _tool(
        create_canvas_tools(
            _context(lambda method, params: events.append((method, params)))
        ),
        "set_canvas",
    )

    result = await set_canvas.ainvoke(
        {
            "source_type": "workspace_file",
            "path": "output/report.md",
            "title": "Research report",
            "renderer": "markdown",
        }
    )

    assert identities == [("persistent-test", "user-1")]
    assert clients[0].set_calls == [
        (
            "thread-1",
            {
                "source_type": "workspace_file",
                "path": "output/report.md",
                "title": "Research report",
                "renderer": "markdown",
                "editable": False,
            },
        )
    ]
    assert clients[0].closed is True
    assert result["canvas_id"] == "main"
    assert events == [
        (
            "canvas.updated",
            {
                "canvas_id": "main",
                "presentation_revision": 3,
                "updated_at": "2026-07-13T10:00:00Z",
                "source_type": "workspace_file",
            },
        )
    ]


@pytest.mark.asyncio
async def test_set_port_sends_exact_flat_payload_and_redacts_returned_source(
    canvas_client, monkeypatch
):
    clients, identities = canvas_client
    events: list[tuple[str, dict[str, Any]]] = []
    client = _CanvasClient()
    client.set_result = CanvasSetResult(
        state=_state(
            source={
                "type": "workspace_app",
                "entry_path": "/demo",
                "entry_port": 8501,
                "origin_generation": "origin-secret",
                "workspace_generation": "workspace-secret",
                "host": "10.0.0.9",
            },
            title="Prototype",
            renderer="auto",
            source_version=None,
            status="starting",
        ),
        changed=True,
    )

    def factory(config_name: str, *, user_id: str | None = None):
        identities.append((config_name, user_id))
        clients.append(client)
        return client

    monkeypatch.setattr("src.tools.canvas._new_orchestrator_client", factory)
    set_canvas = _tool(
        create_canvas_tools(
            _live_app_context(lambda method, params: events.append((method, params)))
        ),
        "set_canvas",
    )

    result = await set_canvas.ainvoke(
        {
            "source_type": "workspace_port",
            "port": 8501,
            "entry_path": "/demo",
            "title": "Prototype",
            "new_app": True,
        }
    )

    assert identities == [("persistent-test", "user-1")]
    assert client.set_calls == [
        (
            "thread-1",
            {
                "source_type": "workspace_port",
                "port": 8501,
                "entry_path": "/demo",
                "title": "Prototype",
                "renderer": "auto",
                "editable": False,
                "new_app": True,
            },
        )
    ]
    assert result["source"] == {"type": "workspace_app", "entry_path": "/demo"}
    assert result["status"] == "starting"
    assert client.closed is True
    assert events == [
        (
            "canvas.updated",
            {
                "canvas_id": "main",
                "presentation_revision": 3,
                "updated_at": "2026-07-13T10:00:00Z",
                "source_type": "workspace_app",
            },
        )
    ]


@pytest.mark.asyncio
async def test_set_browser_sends_exact_payload_redacts_identity_and_emits_once(
    monkeypatch,
):
    events: list[tuple[str, dict[str, Any]]] = []
    browser_state = _state(
        source={
            "type": "browser",
            "browser_generation": "private-generation",
            "stream_port": 9223,
            "token": "private-token",
        },
        title="Research browser",
        renderer="auto",
        source_version=None,
        capabilities={
            "can_edit": False,
            "can_pop_out": True,
            "can_take_control": True,
            "can_stream_browser": True,
        },
    )
    changed_client = _CanvasClient()
    changed_client.set_result = CanvasSetResult(state=browser_state, changed=True)
    repeated_client = _CanvasClient()
    repeated_client.set_result = CanvasSetResult(state=browser_state, changed=False)
    clients = iter((changed_client, repeated_client))
    monkeypatch.setattr(
        "src.tools.canvas._new_orchestrator_client", lambda *a, **kw: next(clients)
    )
    set_canvas = _tool(
        create_canvas_tools(
            _capability_context(
                live=False,
                browser=True,
                callback=lambda method, params: events.append((method, params)),
            )
        ),
        "set_canvas",
    )
    arguments = {
        "source_type": "browser",
        "browser_id": "current",
        "title": "Research browser",
    }

    first = await set_canvas.ainvoke(arguments)
    repeated = await set_canvas.ainvoke(arguments)

    expected_payload = {
        "source_type": "browser",
        "browser_id": "current",
        "renderer": "auto",
        "editable": False,
        "title": "Research browser",
    }
    assert changed_client.set_calls == [("thread-1", expected_payload)]
    assert repeated_client.set_calls == [("thread-1", expected_payload)]
    assert first["source"] == {"type": "browser"}
    assert repeated["source"] == {"type": "browser"}
    assert "can_stream_browser" not in first["capabilities"]
    assert events == [
        (
            "canvas.updated",
            {
                "canvas_id": "main",
                "presentation_revision": 3,
                "updated_at": "2026-07-13T10:00:00Z",
                "source_type": "browser",
            },
        )
    ]
    assert changed_client.closed is True
    assert repeated_client.closed is True


@pytest.mark.asyncio
async def test_set_failure_never_emits_an_invalidation(monkeypatch):
    events: list[tuple[str, dict[str, Any]]] = []

    class FailingClient(_CanvasClient):
        async def set_thread_canvas(self, thread_id: str, payload: dict[str, Any]):
            raise RuntimeError("set rejected")

    client = FailingClient()
    monkeypatch.setattr(
        "src.tools.canvas._new_orchestrator_client", lambda *a, **kw: client
    )
    set_canvas = _tool(
        create_canvas_tools(
            _context(lambda method, params: events.append((method, params)))
        ),
        "set_canvas",
    )

    with pytest.raises(RuntimeError, match="set rejected"):
        await set_canvas.ainvoke(
            {"source_type": "workspace_file", "path": "output/report.md"}
        )

    assert events == []
    assert client.closed is True


@pytest.mark.asyncio
async def test_clear_emits_only_for_a_real_transition(canvas_client):
    clients, _ = canvas_client
    events: list[tuple[str, dict[str, Any]]] = []
    clear_canvas = _tool(
        create_canvas_tools(
            _context(lambda method, params: events.append((method, params)))
        ),
        "clear_canvas",
    )

    assert await clear_canvas.ainvoke({}) is None
    assert events == []

    # A new short-lived client is created for each call.
    transition = _state(
        source=None,
        title=None,
        renderer="auto",
        source_version=None,
        status="cleared",
        presentation_revision=4,
    )
    import src.tools.canvas as canvas_module

    transition_client = _CanvasClient()
    transition_client.clear_result = CanvasClearResult(state=transition, changed=True)
    original_factory = canvas_module._new_orchestrator_client
    canvas_module._new_orchestrator_client = lambda *a, **kw: transition_client
    try:
        result = await clear_canvas.ainvoke({})
    finally:
        canvas_module._new_orchestrator_client = original_factory

    assert result["status"] == "cleared"
    assert events == [
        (
            "canvas.cleared",
            {
                "canvas_id": "main",
                "presentation_revision": 4,
                "updated_at": "2026-07-13T10:00:00Z",
            },
        )
    ]
    assert all(client.closed for client in clients)
    assert transition_client.closed is True

    # An existing already-cleared row remains observable, but it is not a new
    # transition and must not generate a duplicate invalidation.
    repeated_client = _CanvasClient()
    repeated_client.clear_result = CanvasClearResult(state=transition, changed=False)
    canvas_module._new_orchestrator_client = lambda *a, **kw: repeated_client
    try:
        repeated = await clear_canvas.ainvoke({})
    finally:
        canvas_module._new_orchestrator_client = original_factory

    assert repeated["presentation_revision"] == 4
    assert len(events) == 1
    assert repeated_client.closed is True


def test_registry_exposes_canvas_only_to_authenticated_persistent_context():
    assert [tool.name for tool in load_tools(["get_canvas"], _context())] == [
        "get_canvas"
    ]
    assert load_tools(["get_canvas"], ToolContext()) == []


@pytest.mark.parametrize("missing_value", [None, "", "   "])
def test_registry_and_skill_fail_closed_without_internal_key(
    monkeypatch, missing_value
):
    if missing_value is None:
        monkeypatch.delenv("MCP_INTERNAL_KEY", raising=False)
    else:
        monkeypatch.setenv("MCP_INTERNAL_KEY", missing_value)

    with pytest.raises(
        RuntimeError, match="internal service authentication is unavailable"
    ):
        create_canvas_tools(_context())

    loaded = load_tools(
        ["get_canvas", "set_canvas", "clear_canvas"],
        _context(),
    )
    assert loaded == []

    from src.core.skill_resolution import (
        add_default_canvas_skill,
        scope_skills_for_tools,
    )

    # PersistentSession scopes against tools that actually loaded, so the
    # unavailable set_canvas also withdraws the companion skill.
    scoped = scope_skills_for_tools(
        add_default_canvas_skill({}),
        {"use_skill", *(tool.name for tool in loaded)},
    )
    assert scoped["menu"] == []
    assert "present-with-canvas" not in scoped["files"]


def test_none_backend_drops_the_file_only_canvas_category():
    backend = SimpleNamespace(supports_shell=False, supports_file_tools=False)
    assert (
        filter_tools_by_backend(["get_canvas", "set_canvas", "clear_canvas"], backend)
        == []
    )


def test_unknown_backend_must_explicitly_attest_canvas_materialization():
    backend = SimpleNamespace(supports_shell=True, supports_file_tools=True)

    assert filter_tools_by_backend(
        ["read_file", "get_canvas", "set_canvas", "clear_canvas"], backend
    ) == ["read_file"]


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (
            SimpleNamespace(
                supports_shell=True,
                supports_file_tools=True,
                supports_canvas_presentation=True,
            ),
            {"get_canvas", "set_canvas", "clear_canvas"},
        ),
        (
            SimpleNamespace(
                supports_shell=True,
                supports_file_tools=True,
                supports_canvas_presentation=False,
            ),
            set(),
        ),
        (
            SimpleNamespace(
                supports_shell=False,
                supports_file_tools=True,
                supports_canvas_presentation=True,
            ),
            {"get_canvas", "set_canvas", "clear_canvas"},
        ),
        (
            SimpleNamespace(
                supports_shell=False,
                supports_file_tools=True,
                supports_canvas_presentation=False,
            ),
            set(),
        ),
    ],
    ids=["sandbox", "vm", "durable-virtual", "process-local-virtual"],
)
def test_canvas_registration_follows_materializable_backend_capability(
    backend, expected
):
    from src.core.skill_resolution import (
        add_default_canvas_skill,
        scope_skills_for_tools,
    )

    names = ["get_canvas", "set_canvas", "clear_canvas"]
    loaded = filter_tools_by_backend(
        ["use_skill", "get_canvas", "set_canvas", "clear_canvas"], backend
    )
    assert set(loaded) & set(names) == expected
    scoped = scope_skills_for_tools(add_default_canvas_skill({}), loaded)
    skill_names = {item["name"] for item in scoped["menu"]}
    assert ("present-with-canvas" in skill_names) is bool(expected)


def test_real_virtual_backend_marks_process_local_memory_unpresentable():
    from src.core.backends.object_store import InMemoryObjectStore
    from src.core.backends.rclone import RcloneObjectStore
    from src.core.backends.virtual import VirtualWorkspaceBackend

    process_local = VirtualWorkspaceBackend(InMemoryObjectStore())
    durable = VirtualWorkspaceBackend(RcloneObjectStore("s3", root="bucket"))

    assert process_local.supports_canvas_presentation is False
    assert durable.supports_canvas_presentation is True
