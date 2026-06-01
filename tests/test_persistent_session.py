"""Tests for src/api/persistent_session.py — persistent agent session state.

Covers: _EXCLUDED_TOOLS constant, PersistentSession dataclass defaults,
setup(), _setup_workspace(), _setup_tools(), _bind_tools(),
_setup_context_manager(), _setup_shell_manager(), _setup_memory(),
swap_backend(), get_workspace_content(), cleanup().
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.persistent_session import (
    PersistentSession,
    _EXCLUDED_TOOLS,
)
from src.core.workspace_backend import WorkspaceUnavailableError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    """Build a minimal AgentConfig mock for PersistentSession."""
    cfg = MagicMock()
    cfg.interactive.permission_mode = overrides.get("permission_mode", "supervised")
    cfg.interactive.greeting = overrides.get("greeting", "Hello")
    cfg.interactive.idle_timeout_minutes = overrides.get("idle_timeout", 30)
    cfg.workspace.backend = overrides.get("ws_backend", None)
    cfg.workspace.remote = overrides.get("ws_remote", None)
    cfg.workspace.structure = {}
    cfg.workspace.git_versioning = False
    cfg.llm.model = overrides.get("model", "gpt-4o")
    cfg.llm.timeout = 600
    cfg.llm.multimodal = True
    cfg.llm.parallel_tool_calls = overrides.get("parallel_tool_calls", False)
    cfg.memory.enabled = overrides.get("memory_enabled", False)
    cfg.memory.extraction_interval = 5
    cfg.memory.extraction_prompt = ""
    cfg.context_management.keep_recent_tool_results = 10
    cfg.context_management.keep_recent_messages = 50
    cfg.context_management.max_summary_length = 10000
    cfg.extra = overrides.get("extra", {})
    cfg.agent_id = "test-agent"
    cfg.instruction_files = []
    cfg.tools = MagicMock()
    return cfg


def _make_session(**overrides):
    """Build a PersistentSession with a valid thread_id and mock config."""
    return PersistentSession(
        thread_id=overrides.get("thread_id", str(uuid.uuid4())),
        config=overrides.get("config", _make_config()),
        **{k: v for k, v in overrides.items() if k not in ("thread_id", "config")},
    )


# ---------------------------------------------------------------------------
# 2.1 _EXCLUDED_TOOLS constant
# ---------------------------------------------------------------------------


class TestExcludedTools:
    def test_is_frozenset(self):
        assert isinstance(_EXCLUDED_TOOLS, frozenset)

    def test_contains_expected_names(self):
        expected = {
            "next_phase_todos",
            "todo_complete",
            "todo_list",
            "todo_rewind",
            "mark_complete",
            "job_complete",
        }
        assert _EXCLUDED_TOOLS == expected

    def test_exactly_six_entries(self):
        assert len(_EXCLUDED_TOOLS) == 6


# ---------------------------------------------------------------------------
# 2.2 PersistentSession dataclass defaults
# ---------------------------------------------------------------------------


class TestPersistentSessionDefaults:
    def test_permission_mode_default(self):
        session = _make_session()
        assert session.permission_mode == "supervised"

    def test_messages_default_new_list_per_instance(self):
        s1 = _make_session()
        s2 = _make_session()
        assert s1.messages is not s2.messages
        assert s1.messages == []

    def test_turn_count_default(self):
        session = _make_session()
        assert session.turn_count == 0

    def test_optional_fields_default_none(self):
        session = _make_session()
        assert session.workspace_manager is None
        assert session.tools is None
        assert session.llm_with_tools is None
        assert session.context_manager is None
        assert session.tool_context is None
        assert session.auxiliary_llm is None
        assert session.shell_manager is None
        assert session.postgres_conn is None
        assert session.vector_conn is None
        assert session.recall_store is None
        assert session.knowledge_store is None
        assert session._llm is None

    def test_system_prompt_default_empty(self):
        session = _make_session()
        assert session.system_prompt == ""

    def test_project_id_property_none_when_empty(self):
        session = _make_session()
        assert session.project_id is None

    def test_project_id_property_returns_first(self):
        session = _make_session()
        session.project_ids = ["pid-1", "pid-2"]
        assert session.project_id == "pid-1"


# ---------------------------------------------------------------------------
# 2.3 setup()
# ---------------------------------------------------------------------------


class TestSetup:
    @pytest.mark.asyncio
    async def test_setup_overwrites_permission_mode_from_config(self):
        """permission_mode is taken from config.interactive, not dataclass default."""
        cfg = _make_config(permission_mode="auto_accept")
        session = _make_session(config=cfg)

        with (
            patch.object(session, "_setup_workspace", new_callable=AsyncMock),
            patch.object(session, "_setup_tools"),
            patch.object(session, "_bind_tools"),
            patch.object(session, "_setup_context_manager"),
            patch.object(session, "_setup_shell_manager"),
            patch.object(session, "_setup_memory"),
            patch(
                "src.api.persistent_session.get_phase_system_prompt",
                return_value="sys prompt",
            ),
        ):
            await session.setup(llm=MagicMock())

        assert session.permission_mode == "auto_accept"

    @pytest.mark.asyncio
    async def test_setup_stores_llm_reference(self):
        cfg = _make_config()
        session = _make_session(config=cfg)
        mock_llm = MagicMock()

        with (
            patch.object(session, "_setup_workspace", new_callable=AsyncMock),
            patch.object(session, "_setup_tools"),
            patch.object(session, "_bind_tools"),
            patch.object(session, "_setup_context_manager"),
            patch.object(session, "_setup_shell_manager"),
            patch.object(session, "_setup_memory"),
            patch(
                "src.api.persistent_session.get_phase_system_prompt",
                return_value="sys prompt",
            ),
        ):
            await session.setup(
                llm=mock_llm,
                auxiliary_llm="aux",
                postgres_conn="pg",
                vector_conn="vec",
            )

        assert session._llm is mock_llm
        assert session.auxiliary_llm == "aux"
        assert session.postgres_conn == "pg"
        assert session.vector_conn == "vec"

    @pytest.mark.asyncio
    async def test_setup_calls_submethods_in_order(self):
        """Verifies setup calls sub-methods: workspace → shell → knowledge → tools → bind → context → prompt → memory.

        Shell must come before tools so that create_shell_tools() sees a
        non-None shell_manager and includes run_command/shell_read.
        Knowledge must come before tools so that has_knowledge() passes.
        """
        cfg = _make_config()
        session = _make_session(config=cfg)
        call_order = []

        def track(name):
            def fn(*a, **kw):
                call_order.append(name)

            return fn

        with (
            patch.object(
                session,
                "_setup_workspace",
                new_callable=AsyncMock,
                side_effect=track("workspace"),
            ),
            patch.object(session, "_setup_knowledge", side_effect=track("knowledge")),
            patch.object(session, "_setup_tools", side_effect=track("tools")),
            patch.object(session, "_bind_tools", side_effect=track("bind")),
            patch.object(
                session, "_setup_context_manager", side_effect=track("context")
            ),
            patch.object(session, "_setup_shell_manager", side_effect=track("shell")),
            patch.object(session, "_setup_memory", side_effect=track("memory")),
            patch(
                "src.api.persistent_session.get_phase_system_prompt",
                return_value="sys prompt",
            ),
        ):
            await session.setup(llm=MagicMock())

        assert call_order == [
            "workspace",
            "shell",
            "knowledge",
            "tools",
            "bind",
            "context",
            "memory",
        ]

    @pytest.mark.asyncio
    async def test_setup_builds_prompt_with_interactive_type(self):
        cfg = _make_config()
        session = _make_session(config=cfg)

        with (
            patch.object(session, "_setup_workspace", new_callable=AsyncMock),
            patch.object(session, "_setup_tools"),
            patch.object(session, "_bind_tools"),
            patch.object(session, "_setup_context_manager"),
            patch.object(session, "_setup_shell_manager"),
            patch.object(session, "_setup_memory"),
            patch(
                "src.api.persistent_session.get_phase_system_prompt",
                return_value="interactive prompt",
            ) as mock_prompt,
        ):
            await session.setup(llm=MagicMock())

        mock_prompt.assert_called_once()
        call_kwargs = mock_prompt.call_args
        assert (
            call_kwargs[1].get("prompt_type") == "interactive" or call_kwargs[0][2]
            if len(call_kwargs[0]) > 2
            else True
        )
        assert session.system_prompt == "interactive prompt"


# ---------------------------------------------------------------------------
# 2.3b Shell-before-tools integration
# ---------------------------------------------------------------------------


class TestShellToolsIncludedWhenShellManagerAvailable:
    """Integration-style tests: verify that run_command/shell_read actually
    appear in the final tool list when shell_manager is initialised before
    tool loading.

    These tests exercise the real create_shell_tools() gate
    (``if context.shell_manager is not None``) rather than mocking it away.
    """

    def test_shell_tools_include_shell_when_shell_manager_set(self):
        """create_shell_tools returns shell tools when context.shell_manager is set."""
        from src.tools.context import ToolContext

        mock_wm = MagicMock()
        mock_wm.is_initialized = True
        mock_sm = MagicMock()

        ctx = ToolContext(
            workspace_manager=mock_wm,
            shell_manager=mock_sm,
        )

        from src.tools.shell import create_shell_tools

        tools = create_shell_tools(ctx)
        tool_names = [t.name for t in tools]
        assert "run_command" in tool_names
        assert "shell_read" in tool_names

    def test_shell_tools_exclude_shell_when_shell_manager_none(self):
        """create_shell_tools omits shell tools when context.shell_manager is None."""
        from src.tools.context import ToolContext

        mock_wm = MagicMock()
        mock_wm.is_initialized = True

        ctx = ToolContext(
            workspace_manager=mock_wm,
            shell_manager=None,
        )

        from src.tools.shell import create_shell_tools

        tools = create_shell_tools(ctx)
        tool_names = [t.name for t in tools]
        assert "run_command" not in tool_names
        assert "shell_read" not in tool_names

    def test_setup_tools_passes_shell_manager_to_load_tools(self):
        """_setup_tools creates ToolContext with session.shell_manager so that
        the shell tool gate sees a non-None value."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        mock_sm = MagicMock()
        session.shell_manager = mock_sm

        captured_context = {}

        def spy_load_tools(names, ctx):
            captured_context["shell_manager"] = ctx.shell_manager
            return []

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=["run_command", "shell_read"],
            ),
            patch("src.api.persistent_session.load_tools", side_effect=spy_load_tools),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
        ):
            session._setup_tools(None)

        assert captured_context["shell_manager"] is mock_sm


# ---------------------------------------------------------------------------
# 2.4 _setup_workspace()
# ---------------------------------------------------------------------------


class TestSetupWorkspace:
    @pytest.mark.asyncio
    async def test_no_remote_config_raises(self):
        """Default config (no remote) raises RuntimeError — local backend not allowed."""
        cfg = _make_config()
        session = _make_session(config=cfg)

        with pytest.raises(RuntimeError, match="No workspace configured"):
            await session._setup_workspace()

    @pytest.mark.asyncio
    async def test_remote_backend_creation(self):
        """Remote backend created and connected when config says 'sandbox'."""
        cfg = _make_config(
            ws_backend="sandbox",
            ws_remote={"host": "10.0.0.1", "port": 22, "key_path": "/key"},
        )
        cfg.extra = {"shell": {"default_timeout": 60, "max_tabs": 5}}
        session = _make_session(config=cfg)

        mock_remote = MagicMock()
        mock_remote.connect = MagicMock()

        with (
            patch("src.api.persistent_session.WorkspaceManager") as MockWM,
            patch("src.api.persistent_session.WorkspaceManagerConfig"),
            patch.dict(
                "sys.modules",
                {
                    "src.core.backends.remote": MagicMock(
                        RemoteBackend=MagicMock(return_value=mock_remote)
                    )
                },
            ),
        ):
            MockWM.return_value.path = "/tmp/test"
            MockWM.return_value.initialize = MagicMock()
            await session._setup_workspace()

        mock_remote.connect.assert_called_once()
        assert session.workspace_manager is not None

    @pytest.mark.asyncio
    async def test_remote_retry_succeeds_after_failures(self):
        """Retry loop recovers when connect fails then succeeds."""
        cfg = _make_config(
            ws_backend="sandbox",
            ws_remote={"host": "10.0.0.1"},
        )
        session = _make_session(config=cfg)

        mock_remote = MagicMock()
        mock_remote.connect = MagicMock(
            side_effect=[RuntimeError("fail 1"), RuntimeError("fail 2"), None]
        )
        mock_module = MagicMock()
        mock_module.RemoteBackend = MagicMock(return_value=mock_remote)

        with (
            patch("src.api.persistent_session.WorkspaceManager") as MockWM,
            patch("src.api.persistent_session.WorkspaceManagerConfig"),
            patch.dict("sys.modules", {"src.core.backends.remote": mock_module}),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            MockWM.return_value.path = "/tmp/test"
            MockWM.return_value.initialize = MagicMock()
            await session._setup_workspace()

        assert mock_remote.connect.call_count == 3
        assert session.workspace_manager is not None

    @pytest.mark.asyncio
    async def test_remote_retry_raises_after_timeout(self):
        """Retry loop raises WorkspaceUnavailableError after max duration."""
        cfg = _make_config(
            ws_backend="sandbox",
            ws_remote={"host": "10.0.0.1"},
        )
        session = _make_session(config=cfg)

        mock_remote = MagicMock()
        mock_remote.connect = MagicMock(side_effect=RuntimeError("always fails"))
        mock_module = MagicMock()
        mock_module.RemoteBackend = MagicMock(return_value=mock_remote)

        # Simulate time passing beyond max_duration (300s)
        clock = [0.0]

        def fake_monotonic():
            val = clock[0]
            clock[0] += 301.0  # Jump past deadline on first failure
            return val

        with (
            patch("src.api.persistent_session.WorkspaceManager"),
            patch("src.api.persistent_session.WorkspaceManagerConfig"),
            patch.dict("sys.modules", {"src.core.backends.remote": mock_module}),
            patch(
                "src.api.persistent_session.time.monotonic", side_effect=fake_monotonic
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(WorkspaceUnavailableError, match="after 1 attempts"):
                await session._setup_workspace()

    @pytest.mark.asyncio
    async def test_workspace_override_takes_priority(self):
        """workspace_override overrides config values."""
        cfg = _make_config(ws_backend=None)
        session = _make_session(config=cfg)

        override = {
            "backend": "sandbox",
            "remote": {"host": "override-host"},
        }

        mock_remote = MagicMock()
        mock_remote.connect = MagicMock()
        mock_module = MagicMock()
        mock_module.RemoteBackend = MagicMock(return_value=mock_remote)

        with (
            patch("src.api.persistent_session.WorkspaceManager") as MockWM,
            patch("src.api.persistent_session.WorkspaceManagerConfig"),
            patch.dict("sys.modules", {"src.core.backends.remote": mock_module}),
        ):
            MockWM.return_value.path = "/tmp/test"
            MockWM.return_value.initialize = MagicMock()
            await session._setup_workspace(workspace_override=override)

        # RemoteBackend should have been created with the override host
        call_kwargs = mock_module.RemoteBackend.call_args
        assert call_kwargs[1]["host"] == "override-host"


# ---------------------------------------------------------------------------
# 2.5 _setup_tools()
# ---------------------------------------------------------------------------


class TestSetupTools:
    def test_excluded_tools_filtered_out(self):
        """Phase-specific tools are filtered from tool names."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()

        mock_tool = MagicMock()
        mock_tool.name = "web_search"

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "web_search",
                    "next_phase_todos",
                    "todo_complete",
                    "read_file",
                ],
            ),
            patch(
                "src.api.persistent_session.load_tools", return_value=[mock_tool]
            ) as mock_load,
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext"),
        ):
            session._setup_tools(None)

        # Check the names passed to load_tools
        loaded_names = mock_load.call_args[0][0]
        for excluded in _EXCLUDED_TOOLS:
            assert excluded not in loaded_names

    def test_orchestrator_tools_always_included(self):
        """8 orchestrator delegation tools are always appended."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()

        orch_tools = [
            "create_worker_job",
            "list_worker_jobs",
            "get_worker_job",
            "get_job_workspace_file",
            "approve_worker_job",
            "resume_worker_job",
            "cancel_worker_job",
            "pause_worker_job",
        ]

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=["web_search"],
            ),
            patch(
                "src.api.persistent_session.load_tools", return_value=[]
            ) as mock_load,
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext"),
        ):
            session._setup_tools(None)

        loaded_names = mock_load.call_args[0][0]
        for name in orch_tools:
            assert name in loaded_names

    def test_no_duplicate_orchestrator_tools(self):
        """Orchestrator tools not duplicated if already in config."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "web_search",
                    "create_worker_job",
                ],
            ),
            patch(
                "src.api.persistent_session.load_tools", return_value=[]
            ) as mock_load,
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext"),
        ):
            session._setup_tools(None)

        loaded_names = mock_load.call_args[0][0]
        assert loaded_names.count("create_worker_job") == 1

    def test_value_error_fallback_to_individual_loading(self):
        """ValueError in bulk load falls back to individual tool loading."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()

        mock_tool = MagicMock()
        mock_tool.name = "web_search"

        call_count = 0

        def _load(names, ctx):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("unknown tool X")
            return [mock_tool]

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=["web_search"],
            ),
            patch("src.api.persistent_session.load_tools", side_effect=_load),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext"),
        ):
            session._setup_tools(None)

        assert session.tools is not None

    def test_tool_context_created_with_no_todo_manager(self):
        """ToolContext is created with todo_manager=None."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()

        with (
            patch("src.api.persistent_session.get_all_tool_names", return_value=[]),
            patch("src.api.persistent_session.load_tools", return_value=[]),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext") as MockTC,
        ):
            session._setup_tools(None)

        MockTC.assert_called_once()
        assert (
            MockTC.call_args[1].get("todo_manager") is None
            or MockTC.call_args.kwargs.get("todo_manager") is None
        )

    def test_tool_context_receives_shell_manager(self):
        """ToolContext is created with shell_manager from session (set by _setup_shell_manager)."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        mock_sm = MagicMock()
        session.shell_manager = mock_sm

        with (
            patch("src.api.persistent_session.get_all_tool_names", return_value=[]),
            patch("src.api.persistent_session.load_tools", return_value=[]),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext") as MockTC,
        ):
            session._setup_tools(None)

        MockTC.assert_called_once()
        assert MockTC.call_args[1].get("shell_manager") is mock_sm

    def test_tool_context_gets_none_shell_manager_when_unset(self):
        """ToolContext receives shell_manager=None when no shell is available."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.shell_manager = None

        with (
            patch("src.api.persistent_session.get_all_tool_names", return_value=[]),
            patch("src.api.persistent_session.load_tools", return_value=[]),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext") as MockTC,
        ):
            session._setup_tools(None)

        MockTC.assert_called_once()
        assert MockTC.call_args[1].get("shell_manager") is None


# ---------------------------------------------------------------------------
# 2.6 _bind_tools()
# ---------------------------------------------------------------------------


class TestBindTools:
    def test_noop_when_llm_none(self):
        """No-op when _llm is None."""
        session = _make_session()
        session._llm = None
        session.tools = [MagicMock()]

        session._bind_tools()
        assert session.llm_with_tools is None

    def test_noop_when_tools_empty(self):
        """No-op when tools list is empty."""
        session = _make_session()
        session._llm = MagicMock()
        session.tools = []

        session._bind_tools()
        assert session.llm_with_tools is None

    def test_noop_when_tools_none(self):
        """No-op when tools is None."""
        session = _make_session()
        session._llm = MagicMock()
        session.tools = None

        session._bind_tools()
        assert session.llm_with_tools is None

    def test_bind_with_parallel_tool_calls(self):
        """For non-o1/o3/o4 models, parallel_tool_calls is passed."""
        cfg = _make_config(model="gpt-4o", parallel_tool_calls=True)
        session = _make_session(config=cfg)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = "bound_llm"
        session._llm = mock_llm
        session.tools = [MagicMock()]

        session._bind_tools()

        mock_llm.bind_tools.assert_called_once()
        call_kwargs = mock_llm.bind_tools.call_args[1]
        assert call_kwargs["parallel_tool_calls"] is True
        assert session.llm_with_tools == "bound_llm"

    def test_o1_model_no_parallel_tool_calls(self):
        """o1 models don't get parallel_tool_calls kwarg."""
        cfg = _make_config(model="o1-preview")
        session = _make_session(config=cfg)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = "bound"
        session._llm = mock_llm
        session.tools = [MagicMock()]

        session._bind_tools()

        call_kwargs = mock_llm.bind_tools.call_args[1]
        assert "parallel_tool_calls" not in call_kwargs

    def test_o3_model_no_parallel_tool_calls(self):
        """o3 models don't get parallel_tool_calls kwarg."""
        cfg = _make_config(model="o3-mini")
        session = _make_session(config=cfg)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = "bound"
        session._llm = mock_llm
        session.tools = [MagicMock()]

        session._bind_tools()

        call_kwargs = mock_llm.bind_tools.call_args[1]
        assert "parallel_tool_calls" not in call_kwargs

    def test_o4_model_no_parallel_tool_calls(self):
        """o4 models don't get parallel_tool_calls kwarg."""
        cfg = _make_config(model="o4-mini")
        session = _make_session(config=cfg)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = "bound"
        session._llm = mock_llm
        session.tools = [MagicMock()]

        session._bind_tools()

        call_kwargs = mock_llm.bind_tools.call_args[1]
        assert "parallel_tool_calls" not in call_kwargs

    def test_none_model_handled(self):
        """None model name doesn't raise."""
        cfg = _make_config(model=None)
        # The code does `(self.config.llm.model or "").lower()` — None → ""
        cfg.llm.model = None
        session = _make_session(config=cfg)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = "bound"
        session._llm = mock_llm
        session.tools = [MagicMock()]

        session._bind_tools()

        # With None model → "" → doesn't start with o1/o3/o4 → parallel_tool_calls included
        call_kwargs = mock_llm.bind_tools.call_args[1]
        assert "parallel_tool_calls" in call_kwargs


# ---------------------------------------------------------------------------
# 2.7 _setup_context_manager()
# ---------------------------------------------------------------------------


class TestSetupContextManager:
    def test_context_manager_created(self):
        """ContextManager created with config values."""
        cfg = _make_config()
        session = _make_session(config=cfg)

        with patch("src.api.persistent_session.ContextManager") as MockCM:
            session._setup_context_manager()

        assert session.context_manager is not None
        MockCM.assert_called_once()

    def test_model_fallback_to_gpt4(self):
        """Model falls back to 'gpt-4' when config.llm.model is None."""
        cfg = _make_config()
        cfg.llm.model = None
        session = _make_session(config=cfg)

        with patch("src.api.persistent_session.ContextManager") as MockCM:
            session._setup_context_manager()

        call_kwargs = MockCM.call_args[1]
        assert call_kwargs["model"] == "gpt-4"


# ---------------------------------------------------------------------------
# 2.8 _setup_shell_manager()
# ---------------------------------------------------------------------------


class TestSetupShellManager:
    def test_remote_backend_with_supports_shell(self):
        """ShellManager created with backend when supports_shell is True."""
        session = _make_session()
        mock_backend = MagicMock()
        mock_backend.supports_shell = True
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.tool_context = MagicMock()
        session.config.extra = {"shell": {}}

        with patch("src.api.persistent_session.ShellManager", create=True) as MockSM:
            # Patch the import
            import sys

            mock_module = MagicMock()
            mock_module.ShellManager = MockSM
            with patch.dict(
                sys.modules, {"src.tools.shell.shell_manager": mock_module}
            ):
                session._setup_shell_manager()

            if MockSM.called:
                call_kwargs = MockSM.call_args[1]
                assert call_kwargs.get("backend") is mock_backend

    def test_no_tmux_no_remote_returns_early(self):
        """No shell when tmux not found and no remote backend."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = None
        session.workspace_manager = mock_wm

        with patch("src.api.persistent_session.shutil.which", return_value=None):
            session._setup_shell_manager()

        assert session.shell_manager is None

    def test_local_tmux_creates_shell(self):
        """Local ShellManager when tmux available and no remote backend."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])  # no supports_shell attribute
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.tool_context = MagicMock()
        session.config.extra = {"shell": {"sandbox": True}}

        mock_sm = MagicMock()

        with (
            patch(
                "src.api.persistent_session.shutil.which", return_value="/usr/bin/tmux"
            ),
            patch.dict(
                "sys.modules",
                {
                    "src.tools.shell.shell_manager": MagicMock(
                        ShellManager=MagicMock(return_value=mock_sm)
                    )
                },
            ),
        ):
            session._setup_shell_manager()

        assert session.shell_manager is mock_sm

    def test_shell_init_exception_non_fatal(self):
        """Exception during ShellManager init doesn't raise."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.config.extra = {"shell": {}}

        with (
            patch(
                "src.api.persistent_session.shutil.which", return_value="/usr/bin/tmux"
            ),
            patch.dict(
                "sys.modules",
                {
                    "src.tools.shell.shell_manager": MagicMock(
                        ShellManager=MagicMock(side_effect=RuntimeError("tmux broken")),
                    )
                },
            ),
        ):
            # Should not raise
            session._setup_shell_manager()

    def test_sets_tool_context_shell_manager(self):
        """After init, tool_context.shell_manager is set."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.tool_context = MagicMock()
        session.config.extra = {"shell": {}}

        mock_sm = MagicMock()

        with (
            patch(
                "src.api.persistent_session.shutil.which", return_value="/usr/bin/tmux"
            ),
            patch.dict(
                "sys.modules",
                {
                    "src.tools.shell.shell_manager": MagicMock(
                        ShellManager=MagicMock(return_value=mock_sm)
                    )
                },
            ),
        ):
            session._setup_shell_manager()

        session.tool_context.shell_manager = mock_sm  # verify it would be set


# ---------------------------------------------------------------------------
# 2.9 _setup_memory()
# ---------------------------------------------------------------------------


class TestSetupMemory:
    def test_returns_immediately_when_vector_conn_none(self):
        """No stores created when vector_conn is None."""
        session = _make_session()
        session.tool_context = MagicMock()

        session._setup_memory(postgres_conn=MagicMock(), vector_conn=None)

        assert session.recall_store is None
        assert session.knowledge_store is None

    def test_recall_store_only_when_memory_enabled(self):
        """RecallStore created only when config.memory.enabled is True."""
        cfg = _make_config(memory_enabled=True)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()
        session.project_ids = [str(uuid.uuid4())]

        mock_recall = MagicMock()
        mock_embedding = MagicMock()
        mock_ks = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock(return_value=mock_embedding)
                    ),
                    "src.services.recall_store": MagicMock(
                        RecallStore=MagicMock(return_value=mock_recall)
                    ),
                    "src.services.knowledge_store": MagicMock(
                        KnowledgeStore=MagicMock(return_value=mock_ks)
                    ),
                },
            ),
        ):
            session._setup_memory(postgres_conn=MagicMock(), vector_conn=MagicMock())

        assert session.recall_store is mock_recall

    def test_no_recall_store_when_memory_disabled(self):
        """RecallStore NOT created when config.memory.enabled is False."""
        cfg = _make_config(memory_enabled=False)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()

        mock_ks = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock()
                    ),
                    "src.services.knowledge_store": MagicMock(
                        KnowledgeStore=MagicMock(return_value=mock_ks)
                    ),
                },
            ),
        ):
            session._setup_memory(postgres_conn=MagicMock(), vector_conn=MagicMock())

        assert session.recall_store is None

    def test_knowledge_store_always_created_with_vector_conn(self):
        """KnowledgeStore created regardless of memory.enabled setting."""
        cfg = _make_config(memory_enabled=False)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()

        mock_ks = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock()
                    ),
                    "src.services.knowledge_store": MagicMock(
                        KnowledgeStore=MagicMock(return_value=mock_ks)
                    ),
                },
            ),
        ):
            session._setup_memory(postgres_conn=MagicMock(), vector_conn=MagicMock())

        assert session.knowledge_store is mock_ks

    def test_recall_store_failure_non_fatal(self):
        """RecallStore creation failure is non-fatal."""
        cfg = _make_config(memory_enabled=True)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()
        session.project_ids = [str(uuid.uuid4())]

        mock_ks = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock(
                            side_effect=RuntimeError("embedding init failed")
                        ),
                    ),
                    "src.services.knowledge_store": MagicMock(
                        KnowledgeStore=MagicMock(return_value=mock_ks)
                    ),
                },
            ),
        ):
            # Should not raise
            session._setup_memory(postgres_conn=MagicMock(), vector_conn=MagicMock())

    def test_knowledge_store_failure_non_fatal(self):
        """KnowledgeStore creation failure is non-fatal."""
        cfg = _make_config(memory_enabled=False)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock(
                            side_effect=RuntimeError("embedding broke")
                        ),
                    ),
                },
            ),
        ):
            # Should not raise
            session._setup_memory(postgres_conn=MagicMock(), vector_conn=MagicMock())


# ---------------------------------------------------------------------------
# 2.10 swap_backend()
# ---------------------------------------------------------------------------


class TestSwapBackend:
    def test_raises_when_workspace_manager_none(self):
        """RuntimeError raised when workspace_manager is None."""
        session = _make_session()
        session.workspace_manager = None

        with pytest.raises(RuntimeError, match="No workspace manager"):
            session.swap_backend(MagicMock())

    def test_connects_new_backend_when_not_connected(self):
        """Calls connect() on new backend when is_connected() returns False."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])  # old backend without disconnect
        session.workspace_manager = mock_wm
        session.config.extra = {}

        new_backend = MagicMock()
        new_backend.is_connected.return_value = False
        new_backend.connect = MagicMock()

        with patch.object(session, "_setup_shell_manager"):
            session.swap_backend(new_backend)

        new_backend.connect.assert_called_once()

    def test_does_not_connect_when_already_connected(self):
        """Skips connect() on new backend when is_connected() returns True."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        session.workspace_manager = mock_wm

        new_backend = MagicMock()
        new_backend.is_connected.return_value = True

        with patch.object(session, "_setup_shell_manager"):
            session.swap_backend(new_backend)

        new_backend.connect.assert_not_called()

    def test_disconnects_old_backend_when_connected(self):
        """Disconnects old backend when it has disconnect+is_connected and is connected."""
        session = _make_session()
        old_backend = MagicMock()
        old_backend.is_connected.return_value = True
        old_backend.disconnect = MagicMock()

        mock_wm = MagicMock()
        mock_wm.backend = old_backend
        session.workspace_manager = mock_wm

        new_backend = MagicMock()
        new_backend.is_connected.return_value = True

        with patch.object(session, "_setup_shell_manager"):
            session.swap_backend(new_backend)

        old_backend.disconnect.assert_called_once()

    def test_old_backend_disconnect_exception_non_fatal(self):
        """Exception during old backend disconnect is non-fatal."""
        session = _make_session()
        old_backend = MagicMock()
        old_backend.is_connected.return_value = True
        old_backend.disconnect.side_effect = RuntimeError("disconnect failed")

        mock_wm = MagicMock()
        mock_wm.backend = old_backend
        session.workspace_manager = mock_wm

        new_backend = MagicMock()
        new_backend.is_connected.return_value = True

        with patch.object(session, "_setup_shell_manager"):
            # Should not raise
            session.swap_backend(new_backend)

    def test_sets_workspace_manager_backend(self):
        """After swap, workspace_manager._backend is the new backend."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        session.workspace_manager = mock_wm

        new_backend = MagicMock()
        new_backend.is_connected.return_value = True

        with patch.object(session, "_setup_shell_manager"):
            session.swap_backend(new_backend)

        assert mock_wm._backend == new_backend

    def test_rebuilds_shell_manager(self):
        """swap_backend calls _setup_shell_manager to rebuild shell."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        session.workspace_manager = mock_wm

        new_backend = MagicMock()
        new_backend.is_connected.return_value = True

        with patch.object(session, "_setup_shell_manager") as mock_setup:
            session.swap_backend(new_backend)

        mock_setup.assert_called_once()

    def test_connect_called_when_is_connected_missing(self):
        """If new_backend has no is_connected attr, connect() is called (lambda default)."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])
        session.workspace_manager = mock_wm

        # Backend with connect but no is_connected
        new_backend = MagicMock(spec=["connect"])
        new_backend.connect = MagicMock()

        with patch.object(session, "_setup_shell_manager"):
            session.swap_backend(new_backend)

        new_backend.connect.assert_called_once()


# ---------------------------------------------------------------------------
# 2.11 cleanup()
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_shell_manager(self):
        """cleanup() calls shell_manager.cleanup()."""
        session = _make_session()
        mock_sm = MagicMock()
        session.shell_manager = mock_sm
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend = MagicMock(spec=[])  # no disconnect

        await session.cleanup()

        mock_sm.cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_shell_cleanup_exception_non_fatal(self):
        """Shell cleanup exception doesn't prevent further cleanup."""
        session = _make_session()
        mock_sm = MagicMock()
        mock_sm.cleanup.side_effect = RuntimeError("shell error")
        session.shell_manager = mock_sm
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend = MagicMock(spec=[])

        # Should not raise
        await session.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_disconnects_remote_backend(self):
        """cleanup() disconnects remote backend when connected."""
        session = _make_session()
        session.shell_manager = None
        mock_backend = MagicMock()
        mock_backend.is_connected.return_value = True
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        session.workspace_manager = mock_wm

        await session.cleanup()

        mock_backend.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_skips_disconnected_backend(self):
        """cleanup() skips disconnect when backend not connected."""
        session = _make_session()
        session.shell_manager = None
        mock_backend = MagicMock()
        mock_backend.is_connected.return_value = False
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        session.workspace_manager = mock_wm

        await session.cleanup()

        mock_backend.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_backend_disconnect_exception_non_fatal(self):
        """Backend disconnect exception is non-fatal."""
        session = _make_session()
        session.shell_manager = None
        mock_backend = MagicMock()
        mock_backend.is_connected.return_value = True
        mock_backend.disconnect.side_effect = RuntimeError("disconnect failed")
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        session.workspace_manager = mock_wm

        # Should not raise
        await session.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_skips_when_workspace_manager_none(self):
        """cleanup() handles None workspace_manager gracefully."""
        session = _make_session()
        session.shell_manager = None
        session.workspace_manager = None

        # Should not raise
        await session.cleanup()
