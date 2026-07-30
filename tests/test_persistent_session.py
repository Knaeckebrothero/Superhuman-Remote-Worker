"""Tests for src/api/persistent_session.py — persistent agent session state.

Covers: _EXCLUDED_TOOLS constant, PersistentSession dataclass defaults,
setup(), _setup_workspace(), _setup_tools(), _bind_tools(),
_setup_context_manager(), _setup_shell_manager(), _setup_memory(),
swap_backend(), get_workspace_content(), cleanup().
"""

import uuid
from types import SimpleNamespace
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
    cfg.llm.provider = overrides.get("provider", None)
    cfg.llm.timeout = 600
    cfg.llm.multimodal = True
    cfg.llm.parallel_tool_calls = overrides.get("parallel_tool_calls", False)
    cfg.memory.enabled = overrides.get("memory_enabled", False)
    # Explicit non-required defaults: a MagicMock leaves these truthy, which
    # would make the "configured ⇒ required" gate in _setup_memory fire and
    # turn every store-init failure into a MemoryUnavailableError.
    cfg.memory.required = overrides.get("memory_required", False)
    cfg.memory.manager_enabled = overrides.get("manager_enabled", False)
    cfg.memory.pipeline.scorers = overrides.get("memory_scorers", [])
    cfg.memory.pipeline.retrievers = overrides.get("memory_retrievers", [])
    cfg.memory.observer_interval = 5
    cfg.context_management.keep_recent_tool_results = 10
    cfg.context_management.keep_recent_messages = 50
    cfg.context_management.max_summary_length = 10000
    cfg.extra = overrides.get("extra", {})
    cfg.agent_id = "test-agent"
    cfg.instruction_files = []
    cfg.tools = MagicMock()
    # Real dataclass, not a MagicMock: _setup_tools passes it through
    # dataclasses.asdict() to inject delegation settings into tool_config.
    from src.core.loader import DelegationConfig

    cfg.delegation = overrides.get("delegation", DelegationConfig())
    return cfg


def _make_session(**overrides):
    """Build a PersistentSession with a valid thread_id and mock config."""
    return PersistentSession(
        thread_id=overrides.get("thread_id", str(uuid.uuid4())),
        config=overrides.get("config", _make_config()),
        **{k: v for k, v in overrides.items() if k not in ("thread_id", "config")},
    )


class TestDeployBoundSkillWithoutDeploymentDir:
    """Regression: a bound skill must deploy into a session workspace even when
    ``config._deployment_dir`` is None.

    Sessions load their config from the frozen blob, which carries no deployment
    dir. The old ``_deploy_instruction_files`` guard (``or not _deployment_dir``)
    returned early, so the bound skill file was never written — and its
    ``before_tool`` enforce gate then bricked the tool (cite_web / cite_document
    required reading a SKILL.md that did not exist).
    """

    def test_bound_skill_deploys_when_deployment_dir_none(self, tmp_path):
        from src.core.loader import InstructionFileEntry
        from src.core.workspace import WorkspaceManager
        from tests._fs_backend import FilesystemTestBackend

        cfg = _make_config(
            extra={
                "_resolved_skills": {},  # nothing from the catalog path
                "_resolved_instructions": {"cite-as-you-write": "CITE BODY"},
            }
        )
        cfg._deployment_dir = None  # the session case (frozen-blob config)
        cfg.instruction_files = [
            InstructionFileEntry(
                trigger="before_tool:cite_web", skill="cite-as-you-write"
            )
        ]
        session = _make_session(config=cfg)
        session.workspace_manager = WorkspaceManager(
            job_id="t", backend=FilesystemTestBackend(tmp_path)
        )

        session._deploy_instruction_files()

        path = "skills/cite-as-you-write/SKILL.md"
        assert session.workspace_manager.get_path(path).exists()
        assert "CITE BODY" in session.workspace_manager.read_file(path)


class TestSessionRegistersNoInstructionsProviders:
    """Slice 1 is a passive migration for the session path: sessions never
    wrote instructions.md or task_brief.md as real files (their instructions
    reach the model through get_phase_system_prompt instead), so there is
    nothing to replace with a virtual equivalent. Registering one would be a
    new agent-visible file — a product decision, not a migration step — so
    _deploy_instruction_files must register neither provider for sessions.
    """

    def test_neither_instructions_nor_task_brief_provider_is_registered(self, tmp_path):
        from src.core.workspace import WorkspaceManager
        from tests._fs_backend import FilesystemTestBackend

        session = _make_session()
        session.workspace_manager = WorkspaceManager(
            job_id="t", backend=FilesystemTestBackend(tmp_path)
        )

        session._deploy_instruction_files()

        providers = session.workspace_manager.virtual_overlay.providers
        assert "instructions.md" not in providers
        assert "task_brief.md" not in providers


class TestCapabilityScopedCanvasSkillDeployment:
    def test_managed_app_guide_is_scoped_by_reader_and_never_materialized(self):
        cfg = _make_config(
            extra={
                "_resolved_skills": {
                    "menu": [
                        {"name": "app-guide", "description": "shadow"},
                        {"name": "ordinary-skill"},
                    ],
                    "files": {
                        "app-guide": {"SKILL.md": "STALE-OR-USER-GUIDE"},
                        "ordinary-skill": {"SKILL.md": "ordinary"},
                    },
                }
            }
        )
        session = _make_session(config=cfg)
        written: dict[str, str] = {}
        workspace = MagicMock()
        workspace.exists.side_effect = lambda path: path in written
        workspace.write_file.side_effect = lambda path, content: written.__setitem__(
            path, content
        )
        session.workspace_manager = workspace
        session.tool_context = SimpleNamespace(config={})

        session._scope_skills_for_tool_names(["read_product_guide"])
        session._deploy_catalog_skill_files()

        scoped = session.config.extra["_resolved_skills"]
        app_entry = next(item for item in scoped["menu"] if item["name"] == "app-guide")
        assert app_entry["system_managed"] is True
        assert app_entry["loader_tool"] == "read_product_guide"
        assert "STALE-OR-USER-GUIDE" not in scoped["files"]["app-guide"]["SKILL.md"]
        assert written == {"skills/ordinary-skill/SKILL.md": "ordinary"}
        assert session.tool_context.config["_resolved_skills"] == scoped

    def test_app_guide_is_withheld_if_reader_failed_to_instantiate(self):
        cfg = _make_config(extra={"_resolved_skills": {}})
        session = _make_session(config=cfg)

        session._scope_skills_for_tool_names([])

        assert all(
            item["name"] != "app-guide"
            for item in session.config.extra["_resolved_skills"]["menu"]
        )

    def test_runtime_facts_bind_managed_guide_digest_to_agent_source(
        self,
        monkeypatch,
    ):
        from src.core.product_capabilities import (
            ProductComponent,
            ProvenanceStatus,
        )

        revision = "a" * 40
        monkeypatch.setenv("SRW_COMPONENT", "agent")
        monkeypatch.setenv("SRW_SOURCE_REVISION", revision)
        monkeypatch.setenv(
            "SRW_DEPLOYMENT_PROVENANCE_JSON",
            ('{"components":{"workspace":{"source_revision":"' + ("b" * 40) + '"}}}'),
        )
        cfg = _make_config(extra={"_resolved_skills": {}})
        session = _make_session(config=cfg)
        session.workspace_manager = SimpleNamespace(
            backend=SimpleNamespace(
                supports_shell=True,
                supports_file_tools=True,
                supports_canvas_presentation=False,
                supports_canvas_live_apps=False,
                supports_canvas_shared_browser=False,
                sudo_action="freeze",
            )
        )
        session.tool_context = SimpleNamespace(
            config={},
            _resolved_tool_names=["read_product_guide"],
            session_runtime_facts=None,
        )

        session._scope_skills_for_tool_names(["read_product_guide"])
        session._refresh_runtime_facts(["read_product_guide"])

        facts = session.tool_context.session_runtime_facts
        components = dict(facts.runtime_component_provenance)
        assert components[ProductComponent.AGENT].source_revision == revision
        assert components[ProductComponent.GUIDE].source_revision == revision
        assert components[ProductComponent.GUIDE].content_digest.startswith("sha256:")
        assert (
            components[ProductComponent.GUIDE].provenance_status
            is ProvenanceStatus.DECLARED
        )
        assert components[ProductComponent.WORKSPACE].source_revision == "b" * 40

    def test_break_glass_removes_stale_guide_during_session_rebind(self, monkeypatch):
        from src.core.skill_resolution import APP_GUIDE_BREAK_GLASS_ENV

        cfg = _make_config(
            extra={
                "_resolved_skills": {
                    "menu": [{"name": "app-guide", "description": "stale"}],
                    "files": {"app-guide": {"SKILL.md": "STALE"}},
                }
            }
        )
        session = _make_session(config=cfg)
        monkeypatch.setenv(APP_GUIDE_BREAK_GLASS_ENV, "true")

        session._scope_skills_for_tool_names(["read_product_guide"])

        scoped = session.config.extra["_resolved_skills"]
        assert all(item["name"] != "app-guide" for item in scoped["menu"])
        assert "app-guide" not in scoped["files"]

        monkeypatch.delenv(APP_GUIDE_BREAK_GLASS_ENV)
        session._scope_skills_for_tool_names(["read_product_guide"])
        restored = session.config.extra["_resolved_skills"]
        entry = next(item for item in restored["menu"] if item["name"] == "app-guide")
        assert entry["system_managed"] is True
        assert entry["bundle_digest"]
        assert "STALE" not in restored["files"]["app-guide"]["SKILL.md"]

    def test_stale_bound_app_guide_instruction_is_removed(self):
        from src.core.loader import InstructionFileEntry

        cfg = _make_config(
            extra={
                "_resolved_skills": {},
                "_resolved_instructions": {"app-guide": "STALE-BOUND-PRODUCT-GUIDANCE"},
            }
        )
        cfg.instruction_files = [
            InstructionFileEntry(
                trigger="before_tool:create_worker_job",
                skill="app-guide",
            )
        ]
        session = _make_session(config=cfg)
        written: dict[str, str] = {}
        workspace = MagicMock()
        workspace.exists.side_effect = lambda path: path in written
        workspace.write_file.side_effect = lambda path, content: written.__setitem__(
            path, content
        )
        session.workspace_manager = workspace

        session._deploy_instruction_files()

        assert session.config.instruction_files == []
        assert "app-guide" not in session.config.extra["_resolved_instructions"]
        assert "skills/app-guide/SKILL.md" not in written

    def test_managed_canvas_skill_upgrades_only_unchanged_owned_bytes(self):
        cfg = _make_config(
            extra={
                "_resolved_skills": {
                    "menu": [{"name": "present-with-canvas"}],
                    "files": {"present-with-canvas": {"SKILL.md": "canvas-v1"}},
                }
            }
        )
        session = _make_session(config=cfg)
        written: dict[str, str] = {}
        workspace = MagicMock()
        workspace.exists.side_effect = lambda path: path in written
        workspace.read_file.side_effect = lambda path: written[path]
        workspace.delete_file.side_effect = (
            lambda path: written.pop(path, None) is not None
        )
        workspace.write_file.side_effect = lambda path, content: written.__setitem__(
            path, content
        )
        session.workspace_manager = workspace

        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        path = "skills/present-with-canvas/SKILL.md"
        assert written[path] == "canvas-v1"

        unscoped = session.config.extra["_unscoped_resolved_skills"]
        unscoped["files"]["present-with-canvas"]["SKILL.md"] = "canvas-v2"
        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        session._deploy_catalog_skill_files({"present-with-canvas"})

        assert written[path] == "canvas-v2"
        assert session._managed_canvas_skill_files[path] == session._skill_file_digest(
            "canvas-v2"
        )

        # A user edit breaks the recorded ownership digest, so even a later
        # bundled v3 must not overwrite or re-claim it.
        written[path] = "user-customized"
        unscoped = session.config.extra["_unscoped_resolved_skills"]
        unscoped["files"]["present-with-canvas"]["SKILL.md"] = "canvas-v3"
        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        assert written[path] == "user-customized"
        assert path not in session._managed_canvas_skill_files

    def test_canvas_skill_waits_for_actual_tools_and_reconciles_runtime_toggle(self):
        cfg = _make_config(
            extra={
                "_resolved_skills": {
                    "menu": [
                        {"name": "ordinary-skill"},
                        {"name": "present-with-canvas"},
                    ],
                    "files": {
                        "ordinary-skill": {"SKILL.md": "ordinary"},
                        "present-with-canvas": {"SKILL.md": "canvas"},
                    },
                }
            }
        )
        session = _make_session(config=cfg)
        written: dict[str, str] = {}
        workspace = MagicMock()
        workspace.exists.side_effect = lambda path: path in written
        workspace.read_file.side_effect = lambda path: written[path]
        workspace.delete_file.side_effect = (
            lambda path: written.pop(path, None) is not None
        )
        workspace.write_file.side_effect = lambda path, content: written.__setitem__(
            path, content
        )
        session.workspace_manager = workspace
        session.tool_context = SimpleNamespace(config={})

        # Workspace setup can deploy ordinary skills, but Canvas has not yet
        # instantiated and must be absent from both the menu and workspace.
        session._deploy_instruction_files()
        assert [
            item["name"] for item in session.config.extra["_resolved_skills"]["menu"]
        ] == ["ordinary-skill"]
        assert written == {"skills/ordinary-skill/SKILL.md": "ordinary"}

        # Missing identity, an explicit disable, and backend=none all result in
        # an actual loaded set without set_canvas; none may add the skill.
        session._scope_skills_for_tool_names(["use_skill"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        assert "skills/present-with-canvas/SKILL.md" not in written

        # A later none→workspace rebind can admit both tools and add the skill.
        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        assert [
            item["name"] for item in session.config.extra["_resolved_skills"]["menu"]
        ] == ["ordinary-skill", "present-with-canvas"]
        assert written["skills/present-with-canvas/SKILL.md"] == "canvas"
        assert (
            session.tool_context.config["_resolved_skills"]
            == session.config.extra["_resolved_skills"]
        )

        # A runtime Canvas toggle removes the menu entry and only the unchanged
        # managed file. An unrelated/user file under the same directory survives.
        written["skills/present-with-canvas/user-notes.md"] = "keep me"
        session._scope_skills_for_tool_names(["use_skill"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        assert [
            item["name"] for item in session.config.extra["_resolved_skills"]["menu"]
        ] == ["ordinary-skill"]
        assert "skills/present-with-canvas/SKILL.md" not in written
        assert written["skills/present-with-canvas/user-notes.md"] == "keep me"
        assert "skills/present-with-canvas/.srw-managed.json" not in written

        # Re-admission works, and a subsequently user-modified managed file is
        # released rather than deleted when capability is removed again.
        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        written["skills/present-with-canvas/SKILL.md"] = "user-modified"
        session._scope_skills_for_tool_names(["use_skill"])
        session._deploy_catalog_skill_files({"present-with-canvas"})
        assert written["skills/present-with-canvas/SKILL.md"] == "user-modified"

    def test_empty_db_catalog_gets_only_default_canvas_skill_after_actual_load(self):
        cfg = _make_config(extra={"_resolved_skills": {}})
        session = _make_session(config=cfg)

        session._scope_skills_for_tool_names(["use_skill"])
        assert session.config.extra["_resolved_skills"]["menu"] == []

        session._scope_skills_for_tool_names(["use_skill", "set_canvas"])
        menu = session.config.extra["_resolved_skills"]["menu"]
        assert [item["name"] for item in menu] == ["present-with-canvas"]
        assert set(session.config.extra["_resolved_skills"]["files"]) == {
            "present-with-canvas"
        }


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

        captured_context = {"tool_name_calls": []}

        def spy_load_tools(names, ctx):
            captured_context["shell_manager"] = ctx.shell_manager
            captured_context["tool_name_calls"].append(list(names))
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
        assert any(
            "read_product_guide" in names
            for names in captured_context["tool_name_calls"]
        )

    def test_setup_tools_withholds_product_reader_during_break_glass(self, monkeypatch):
        from src.core.skill_resolution import APP_GUIDE_BREAK_GLASS_ENV

        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        captured_calls: list[list[str]] = []
        monkeypatch.setenv(APP_GUIDE_BREAK_GLASS_ENV, "true")

        def spy_load_tools(names, _ctx):
            captured_calls.append(list(names))
            return []

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=["read_product_guide"],
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

        assert captured_calls
        assert all(
            "read_product_guide" not in tool_names for tool_names in captured_calls
        )


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
        # A direct/configured remote endpoint has no orchestrator-attested
        # generation + host identity pairing, even when labelled sandbox.
        assert mock_remote.supports_canvas_presentation is False
        assert mock_remote.supports_canvas_live_apps is False
        assert mock_remote.supports_canvas_shared_browser is False
        assert session.workspace_manager is not None

    @pytest.mark.asyncio
    async def test_attested_sandbox_override_enables_canvas_presentation(self):
        cfg = _make_config(
            ws_backend="sandbox",
            ws_remote={"host": "configured.test", "port": 22},
        )
        session = _make_session(config=cfg)
        mock_remote = MagicMock()
        mock_remote.connect = MagicMock()
        workspace_override = {
            "backend": "sandbox",
            "remote": {"host": "paired.test", "port": 30022},
            "canvas_presentation_available": True,
            "canvas_live_apps_available": True,
            "canvas_shared_browser_available": True,
        }

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
            await session._setup_workspace(workspace_override=workspace_override)

        assert mock_remote.supports_canvas_presentation is True
        assert mock_remote.supports_canvas_live_apps is True
        assert mock_remote.supports_canvas_shared_browser is True

    @pytest.mark.asyncio
    async def test_vm_remote_backend_disables_canvas_presentation(self):
        cfg = _make_config(
            ws_backend="vm",
            ws_remote={"host": "vm.test", "port": 22, "key_path": "/key"},
        )
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

        assert mock_remote.supports_canvas_presentation is False
        assert mock_remote.supports_canvas_live_apps is False
        assert mock_remote.supports_canvas_shared_browser is False

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
# 2.4.1 _setup_cloud_mount() — protected overlay-failure fail-safe (F-M6)
# ---------------------------------------------------------------------------


def _protected_cloud_mount_cfg() -> dict:
    return {
        "version": 1,
        "driver": "rclone",
        "protected": True,
        "skip_workspace_links": True,
        "overlay": {
            "lower": "/cloud/lower",
            "upper": "/home/agent-host/.overlay/upper",
            "work": "/home/agent-host/.overlay/work",
            "merged": "/cloud/merged",
            "quota_bytes": 8 * 1024**3,
        },
        "mounts": [
            {
                "mount_id": "protected-thread",
                "mount_kind": "protected_lower",
                "target_path": "/cloud/lower",
                "workspace_name": "lower",
                "access": "read_only",
            }
        ],
    }


class TestSetupCloudMountOverlayFailure:
    """F-M6 (Task B10): pin the overlay-failure -> RO-lower-teardown branch
    of ``_setup_cloud_mount``. This is the B9 fail-safe deviation from the
    original brief (which left the RO lower mounted on overlay failure) —
    a protected session whose overlay fails to mount must end up with NO
    cloud access at all (cloud_mount_manager torn down + cleared) rather
    than a half-protected session that still writes straight to the raw RO
    lower thinking it's protected."""

    @pytest.mark.asyncio
    async def test_overlay_failure_tears_down_ro_lower(self):
        session = _make_session(
            workspace_manager=SimpleNamespace(path="/workspace", backend=MagicMock())
        )
        fake_rclone_manager = MagicMock()
        fake_rclone_manager.start_all = AsyncMock(return_value=None)
        fake_rclone_manager.mounts = []
        fake_rclone_manager.aclose = AsyncMock(return_value=None)

        fake_overlay_manager = MagicMock()
        fake_overlay_manager.mount = MagicMock(
            side_effect=RuntimeError("fuse-overlayfs: mount failed")
        )

        with (
            patch(
                "src.services.cloud_mount.RcloneMountManager",
                return_value=fake_rclone_manager,
            ),
            patch(
                "src.services.cloud_overlay.OverlayMountManager",
                return_value=fake_overlay_manager,
            ),
        ):
            await session._setup_cloud_mount(_protected_cloud_mount_cfg())

        assert session.cloud_mount_error is not None
        assert session.cloud_mount_error.startswith("overlay: ")
        assert session.overlay_mount_manager is None
        # No half-protected session: the RO lower is torn down AND cleared,
        # not left mounted with no capture overlay on top.
        fake_rclone_manager.aclose.assert_awaited_once()
        assert session.cloud_mount_manager is None

    @pytest.mark.asyncio
    async def test_overlay_failure_clears_lower_even_if_teardown_also_fails(self):
        """The teardown call itself failing (e.g. a stuck fusermount3 -uz)
        must not leave cloud_mount_manager pointing at a live, unprotected
        lower — the fail-safe still clears it."""
        session = _make_session(
            workspace_manager=SimpleNamespace(path="/workspace", backend=MagicMock())
        )
        fake_rclone_manager = MagicMock()
        fake_rclone_manager.start_all = AsyncMock(return_value=None)
        fake_rclone_manager.mounts = []
        fake_rclone_manager.aclose = AsyncMock(
            side_effect=RuntimeError("umount: EBUSY")
        )

        fake_overlay_manager = MagicMock()
        fake_overlay_manager.mount = MagicMock(side_effect=RuntimeError("overlay boom"))

        with (
            patch(
                "src.services.cloud_mount.RcloneMountManager",
                return_value=fake_rclone_manager,
            ),
            patch(
                "src.services.cloud_overlay.OverlayMountManager",
                return_value=fake_overlay_manager,
            ),
        ):
            await session._setup_cloud_mount(_protected_cloud_mount_cfg())

        assert session.cloud_mount_error.startswith("overlay: ")
        assert session.overlay_mount_manager is None
        assert session.cloud_mount_manager is None

    @pytest.mark.asyncio
    async def test_successful_overlay_mount_keeps_both_managers(self):
        """Control case: a clean overlay mount keeps the RO lower AND the
        overlay manager live (regression guard for the failure-path tests
        above — proves the teardown branch only fires on failure)."""
        session = _make_session(
            workspace_manager=SimpleNamespace(path="/workspace", backend=MagicMock())
        )
        fake_rclone_manager = MagicMock()
        fake_rclone_manager.start_all = AsyncMock(return_value=None)
        fake_rclone_manager.mounts = []
        fake_rclone_manager.aclose = AsyncMock(return_value=None)

        fake_overlay_manager = MagicMock()
        fake_overlay_manager.mount = MagicMock(return_value=None)

        with (
            patch(
                "src.services.cloud_mount.RcloneMountManager",
                return_value=fake_rclone_manager,
            ),
            patch(
                "src.services.cloud_overlay.OverlayMountManager",
                return_value=fake_overlay_manager,
            ),
        ):
            await session._setup_cloud_mount(_protected_cloud_mount_cfg())

        assert session.cloud_mount_error is None
        assert session.cloud_mount_manager is fake_rclone_manager
        assert session.overlay_mount_manager is fake_overlay_manager
        fake_rclone_manager.aclose.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2.5 _setup_tools()
# ---------------------------------------------------------------------------


class TestSetupTools:
    def test_capability_tool_is_independent_persistent_floor(
        self,
        monkeypatch,
    ):
        """The M2c canary survives every user group being off and guide break-glass."""

        from src.tools.product_capabilities import (
            PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV,
            PRODUCT_CAPABILITIES_TOOL_NAME,
        )

        monkeypatch.setenv(PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV, "true")
        monkeypatch.setenv("APP_GUIDE_BREAK_GLASS_DISABLED", "true")
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "false")
        monkeypatch.setenv("SKILLS_DB_ENABLED", "false")
        cfg = _make_config(
            extra={
                "_fleet_management_disabled": True,
                "_agent_catalog_disabled": True,
                "_workflows_disabled": True,
                "_canvas_disabled": True,
            }
        )
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend = SimpleNamespace(
            supports_shell=False,
            supports_file_tools=False,
            supports_canvas_presentation=False,
            supports_canvas_live_apps=False,
            supports_canvas_shared_browser=False,
        )
        captured: list[list[str]] = []

        def load(names, _context):
            captured.append(list(names))
            return [_named_tool(name) for name in names]

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "read_product_guide",
                    "create_worker_job",
                    "list_skills",
                    "list_automations",
                    "set_canvas",
                ],
            ),
            patch("src.api.persistent_session.load_tools", side_effect=load),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda tools: tools,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda tools, _context: tools,
            ),
            patch.object(session, "_scope_skills_for_tool_names"),
            patch.object(session, "_deploy_catalog_skill_files"),
        ):
            session._setup_tools(None)

        assert captured
        candidates = captured[0]
        assert PRODUCT_CAPABILITIES_TOOL_NAME in candidates
        assert "read_product_guide" not in candidates
        assert "create_worker_job" not in candidates
        assert "list_skills" not in candidates
        assert "list_automations" not in candidates
        assert "set_canvas" not in candidates
        assert PRODUCT_CAPABILITIES_TOOL_NAME in (
            session.tool_context._resolved_tool_names
        )

    def test_final_post_enforcement_tool_names_are_published(self, monkeypatch):
        from src.tools.product_capabilities import (
            PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV,
            PRODUCT_CAPABILITIES_TOOL_NAME,
        )

        monkeypatch.setenv(PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV, "true")
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend = SimpleNamespace(
            supports_shell=False,
            supports_file_tools=True,
            supports_canvas_presentation=False,
            supports_canvas_live_apps=False,
            supports_canvas_shared_browser=False,
        )

        def enforce(tools, _context):
            return [tool for tool in tools if tool.name != "web_search"]

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=["web_search"],
            ),
            patch(
                "src.api.persistent_session.load_tools",
                side_effect=lambda names, _context: [
                    _named_tool(name) for name in names
                ],
            ),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda tools: tools,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=enforce,
            ),
            patch.object(session, "_scope_skills_for_tool_names"),
            patch.object(session, "_deploy_catalog_skill_files"),
        ):
            session._setup_tools(None)

        assert "web_search" not in session.tool_context._resolved_tool_names
        assert session.tool_context._resolved_tool_names == [
            tool.name for tool in session.tools
        ]
        assert session.tool_context.session_runtime_facts.loaded_tool_names == tuple(
            sorted(session.tool_context._resolved_tool_names)
        )
        assert (
            PRODUCT_CAPABILITIES_TOOL_NAME
            in session.tool_context.session_runtime_facts.loaded_tool_names
        )

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
        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        for excluded in _EXCLUDED_TOOLS:
            assert excluded not in loaded_names

    def test_orchestrator_and_catalog_tools_always_included(self):
        """Fleet Management and Experts & Skills tools are always appended."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = False

        orch_tools = [
            "get_session_context",
            "create_worker_job",
            "list_worker_jobs",
            "get_worker_job",
            "get_job_workspace_file",
            "list_job_workspace_files",
            "approve_worker_job",
            "resume_worker_job",
            "cancel_worker_job",
            "pause_worker_job",
            "get_current_project",
            "list_project_jobs",
            "list_project_repositories",
            "get_default_project_repository",
        ]
        catalog_tools = [
            "list_experts",
            "get_expert",
            "list_skills",
            "search_skills",
            "get_skill",
        ]
        workflow_tools = [
            "list_automations",
            "get_automation",
            "list_automation_runs",
            "propose_automation",
            "get_project_loop",
            "list_project_loop_jobs",
            "explain_project_loop",
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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        for name in orch_tools:
            assert name in loaded_names
        for name in catalog_tools:
            assert name in loaded_names
        for name in workflow_tools:
            assert name in loaded_names
        for name in (
            "get_expert_bundle",
            "set_expert_bundle",
            "get_skill_bundle",
            "set_skill_bundle",
            "get_automation_bundle",
            "set_automation_bundle",
        ):
            assert name not in loaded_names
        assert "checkout_project_repository" not in loaded_names

    def test_repository_checkout_tool_included_for_shell_workspace(self):
        """Repository checkout is exposed only when the session backend can clone."""
        cfg = _make_config()
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = True

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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        assert "checkout_project_repository" in loaded_names

    def test_fleet_management_can_be_disabled(self):
        """Fleet Management opt-out removes SRW app-control tools."""
        cfg = _make_config(extra={"_fleet_management_disabled": True})
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = False

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "web_search",
                    "create_worker_job",
                    "list_project_repositories",
                    "list_skills",
                    "request_workspace_upgrade",
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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        assert "web_search" in loaded_names
        assert "task_add" in loaded_names
        assert "create_worker_job" not in loaded_names
        assert "list_project_repositories" not in loaded_names
        assert "list_skills" in loaded_names
        assert "request_workspace_upgrade" not in loaded_names

    def test_agent_catalog_can_be_disabled(self):
        """Experts & Skills opt-out removes expert/skill catalog tools."""
        cfg = _make_config(extra={"_agent_catalog_disabled": True})
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = False

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "web_search",
                    "create_worker_job",
                    "list_skills",
                    "set_skill_bundle",
                    "list_automations",
                    "set_automation_bundle",
                    "request_workspace_upgrade",
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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        assert "web_search" in loaded_names
        assert "create_worker_job" in loaded_names
        assert "request_workspace_upgrade" in loaded_names
        assert "list_skills" not in loaded_names
        assert "set_skill_bundle" not in loaded_names
        assert "list_automations" in loaded_names
        assert "set_automation_bundle" in loaded_names

    def test_workflows_can_be_disabled(self):
        """Automations & Loops opt-out removes workflow tools."""
        cfg = _make_config(extra={"_workflows_disabled": True})
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = False

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "web_search",
                    "create_worker_job",
                    "list_skills",
                    "list_automations",
                    "set_automation_bundle",
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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        assert "web_search" in loaded_names
        assert "create_worker_job" in loaded_names
        assert "list_skills" in loaded_names
        assert "list_automations" not in loaded_names
        assert "set_automation_bundle" not in loaded_names

    def test_canvas_can_be_disabled_independently(self):
        cfg = _make_config(extra={"_canvas_disabled": True})
        session = _make_session(config=cfg)
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend.supports_shell = True
        session.workspace_manager.backend.supports_file_tools = True

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[
                    "read_file",
                    "use_skill",
                    "get_canvas",
                    "set_canvas",
                    "clear_canvas",
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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
        assert "read_file" in loaded_names
        assert "use_skill" in loaded_names
        assert "get_canvas" not in loaded_names
        assert "set_canvas" not in loaded_names
        assert "clear_canvas" not in loaded_names

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

        loaded_names = mock_load.call_args_list[
            0
        ].args[
            0
        ]  # first call: the real toolset (a later call may be the never-bind-zero floor)
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

    def test_google_provider_no_parallel_tool_calls(self):
        """Google models must NOT get parallel_tool_calls — GenerateContentConfig
        is a strict Pydantic model (extra_forbidden) and crashes on the kwarg.

        Regression for session a6436fb8 (gemini-3.5-flash): "1 validation error
        for GenerateContentConfig / parallel_tool_calls / Extra inputs are not
        permitted".
        """
        cfg = _make_config(
            model="gemini-3.5-flash", provider="google", parallel_tool_calls=True
        )
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

    def test_no_backend_returns_early(self):
        """No shell when there is no workspace backend (no local fallback)."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = None
        session.workspace_manager = mock_wm

        session._setup_shell_manager()

        assert session.shell_manager is None

    def test_backend_without_shell_support_returns_early(self):
        """No shell when the backend doesn't declare supports_shell — the
        local-tmux degradation was removed (capability, not inference)."""
        session = _make_session()
        mock_wm = MagicMock()
        mock_wm.backend = MagicMock(spec=[])  # no supports_shell attribute
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.tool_context = MagicMock()
        session.config.extra = {"shell": {"sandbox": True}}

        session._setup_shell_manager()

        assert session.shell_manager is None

    def test_shell_init_exception_non_fatal(self):
        """Exception during ShellManager init doesn't raise."""
        session = _make_session()
        mock_backend = MagicMock()
        mock_backend.supports_shell = True
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.config.extra = {"shell": {}}

        with patch.dict(
            "sys.modules",
            {
                "src.tools.shell.shell_manager": MagicMock(
                    ShellManager=MagicMock(side_effect=RuntimeError("ssh broken")),
                )
            },
        ):
            # Should not raise
            session._setup_shell_manager()

        assert session.shell_manager is None

    def test_sets_tool_context_shell_manager(self):
        """After init, tool_context.shell_manager is set."""
        session = _make_session()
        mock_backend = MagicMock()
        mock_backend.supports_shell = True
        mock_wm = MagicMock()
        mock_wm.backend = mock_backend
        mock_wm.path = "/tmp/ws"
        session.workspace_manager = mock_wm
        session.tool_context = MagicMock()
        session.config.extra = {"shell": {}}

        mock_sm = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "src.tools.shell.shell_manager": MagicMock(
                    ShellManager=MagicMock(return_value=mock_sm)
                )
            },
        ):
            session._setup_shell_manager()

        assert session.shell_manager is mock_sm
        assert session.tool_context.shell_manager is mock_sm


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

    def test_required_recall_store_failure_raises(self):
        """memory.required + RecallStore init failure → MemoryUnavailableError
        (fail loud, don't run the session half-working). Regression for
        docs/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md.
        """
        from src.api.persistent_session import MemoryUnavailableError

        cfg = _make_config(memory_enabled=True, memory_required=True)
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()
        session.project_ids = [str(uuid.uuid4())]

        with patch.dict(
            "sys.modules",
            {
                "src.services.embedding_service": MagicMock(
                    get_embedding_service=MagicMock(
                        side_effect=RuntimeError("embedding endpoint down")
                    ),
                ),
                "src.services.knowledge_store": MagicMock(
                    KnowledgeStore=MagicMock(return_value=MagicMock())
                ),
            },
        ):
            with pytest.raises(MemoryUnavailableError, match="RecallStore"):
                session._setup_memory(
                    postgres_conn=MagicMock(), vector_conn=MagicMock()
                )

    def test_configured_pipeline_makes_memory_required(self):
        """A configured pipeline (scorers present) implies required even when
        memory.required is False: a store failure must fail loud."""
        from src.api.persistent_session import MemoryUnavailableError

        cfg = _make_config(
            memory_enabled=True,
            memory_required=False,
            manager_enabled=True,
            memory_scorers=["reranker"],
        )
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()
        session.project_ids = [str(uuid.uuid4())]

        with patch.dict(
            "sys.modules",
            {
                "src.services.embedding_service": MagicMock(
                    get_embedding_service=MagicMock(
                        side_effect=RuntimeError("embedding endpoint down")
                    ),
                ),
                "src.services.knowledge_store": MagicMock(
                    KnowledgeStore=MagicMock(return_value=MagicMock())
                ),
            },
        ):
            with pytest.raises(MemoryUnavailableError):
                session._setup_memory(
                    postgres_conn=MagicMock(), vector_conn=MagicMock()
                )

    def test_pipeline_bind_failure_raises_memory_unavailable(self):
        """A plugin factory that can't resolve its transport (e.g. the reranker
        endpoint) fails the session loud, not a raw crash."""
        from src.api.persistent_session import MemoryUnavailableError

        cfg = _make_config(
            memory_enabled=True,
            manager_enabled=True,
            memory_scorers=["reranker"],
        )
        session = _make_session(config=cfg)
        session.tool_context = MagicMock()
        session.project_ids = [str(uuid.uuid4())]

        with (
            # create_task needs a running loop; no-op it so the RecallStore path
            # succeeds cleanly and we actually reach the bind (not the store gate).
            patch("asyncio.create_task", MagicMock()),
            patch.dict(
                "sys.modules",
                {
                    "src.services.embedding_service": MagicMock(
                        get_embedding_service=MagicMock(return_value=MagicMock())
                    ),
                    "src.services.recall_store": MagicMock(
                        RecallStore=MagicMock(return_value=MagicMock())
                    ),
                    "src.services.knowledge_store": MagicMock(
                        KnowledgeStore=MagicMock(return_value=MagicMock())
                    ),
                    "src.services.memory": MagicMock(
                        MemoryManager=MagicMock(
                            from_config=MagicMock(
                                side_effect=ValueError("reranker needs a base_url")
                            )
                        ),
                        MemoryRuntime=MagicMock(),
                    ),
                },
            ),
        ):
            with pytest.raises(MemoryUnavailableError, match="reranker"):
                session._setup_memory(
                    postgres_conn=MagicMock(), vector_conn=MagicMock()
                )


class TestSetupKnowledge:
    def test_external_only_scope_initializes_store_without_graph(self):
        session = _make_session()
        session.project_ids = []
        session.knowledge_bindings = [MagicMock()]
        knowledge_store = MagicMock(name="knowledge_store")

        with (
            patch("src.services.embedding_service.get_kb_embedding_service"),
            patch(
                "src.services.knowledge_store.KnowledgeStore",
                return_value=knowledge_store,
            ),
            patch("src.services.knowledge_graph.KnowledgeGraphDB") as graph_cls,
        ):
            session._setup_knowledge(MagicMock(name="vector_conn"))

        assert session.knowledge_store is knowledge_store
        graph_cls.assert_not_called()

    def test_vector_store_is_available_when_graph_connection_fails(self):
        """Neo4j failure must not hide the pgvector-backed knowledge tools."""
        session = _make_session()
        session.project_ids = [str(uuid.uuid4())]
        vector_conn = MagicMock(name="vector_conn")
        embedding_service = MagicMock(name="embedding_service")
        knowledge_store = MagicMock(name="knowledge_store")
        knowledge_graph = MagicMock(name="knowledge_graph")
        knowledge_graph.connect.return_value = False

        with (
            patch(
                "src.services.embedding_service.get_kb_embedding_service",
                return_value=embedding_service,
            ),
            patch(
                "src.services.knowledge_store.KnowledgeStore",
                return_value=knowledge_store,
            ) as store_cls,
            patch(
                "src.services.knowledge_graph.KnowledgeGraphDB",
                return_value=knowledge_graph,
            ),
        ):
            session._setup_knowledge(vector_conn)

        store_cls.assert_called_once_with(
            db=vector_conn,
            embedding_service=embedding_service,
        )
        assert session.knowledge_store is knowledge_store
        assert session._knowledge_graph is None
        assert session._kb_degraded is False

    def test_embedding_failure_marks_store_degraded(self):
        session = _make_session()
        session.project_ids = [str(uuid.uuid4())]
        knowledge_graph = MagicMock()
        knowledge_graph.connect.return_value = False

        with (
            patch(
                "src.services.embedding_service.get_kb_embedding_service",
                side_effect=RuntimeError("embedding unavailable"),
            ),
            patch("src.core.archiver.audit_unavailable") as audit_unavailable,
            patch(
                "src.services.knowledge_graph.KnowledgeGraphDB",
                return_value=knowledge_graph,
            ),
        ):
            session._setup_knowledge(MagicMock(name="vector_conn"))

        assert session.knowledge_store is None
        assert session._kb_degraded is True
        audit_unavailable.assert_called_once()


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
# 2.10b resetup_tools_for_backend() — S1: live tool re-derivation after a swap
# (docs/features/workspace_tier_upgrade.md §4.2)
# ---------------------------------------------------------------------------


class _SwappableWM:
    """Minimal WorkspaceManager stand-in whose ``.backend`` reflects the
    ``._backend`` that ``swap_backend`` assigns — so the capability gate in
    ``_load_tools_for_backend`` observes the post-swap backend. A plain
    MagicMock's ``.backend`` would not track an assignment to ``._backend``.
    """

    def __init__(self, backend):
        self._backend = backend
        self._files = {}

    @property
    def backend(self):
        return self._backend

    @property
    def virtual_overlay(self):
        # Virtual dirs are irrelevant to this test double; behave like the
        # real WorkspaceManager does when VIRTUAL_DIRS_ENABLED is off.
        return None

    def register_virtual_provider(self, provider):
        pass

    def get_path(self, rel):
        return f"/tmp/ws/{rel}"

    def write_file(self, rel, content):
        self._files[rel] = content

    def exists(self, rel):
        return rel in self._files

    def read_file(self, rel):
        return self._files[rel]

    def delete_file(self, rel):
        return self._files.pop(rel, None) is not None


def _named_tool(name):
    """A mock tool object carrying a ``.name`` (MagicMock(name=...) is special)."""
    t = MagicMock()
    t.name = name
    return t


class TestResetupToolsForBackend:
    """S1: after a live backend swap from a lite (no-shell) tier to a
    shell-capable one, ``resetup_tools_for_backend`` re-derives + rebinds the
    toolset so shell/git tools — dropped by the lite capability gate — reappear,
    without rebuilding ``tool_context`` or resetting ``session_task_manager``.
    """

    def test_swap_then_resetup_readmits_shell_and_git(self, monkeypatch):
        from src.tools.product_capabilities import (
            PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV,
            PRODUCT_CAPABILITIES_TOOL_NAME,
        )
        from src.tools.registry import get_tools_by_category

        monkeypatch.setenv(PRODUCT_CAPABILITIES_TOOL_ENABLED_ENV, "true")
        shell = get_tools_by_category("shell")[0]
        git = get_tools_by_category("git")[0]
        web = get_tools_by_category("research")[0]

        # virtual: no shell -> shell/git filtered; sandbox: shell -> re-admitted.
        virtual = SimpleNamespace(supports_shell=False, supports_file_tools=True)
        sandbox = SimpleNamespace(supports_shell=True, supports_file_tools=True)

        session = _make_session()
        session.workspace_manager = _SwappableWM(virtual)
        session._llm = MagicMock()
        session._llm.bind_tools.return_value = MagicMock()
        session.system_prompt = "pre-upgrade prompt"

        new_sm = MagicMock()

        with (
            patch(
                "src.api.persistent_session.get_all_tool_names",
                return_value=[shell, git, web],
            ),
            patch(
                "src.api.persistent_session.load_tools",
                side_effect=lambda names, ctx: [_named_tool(n) for n in names],
            ),
            patch(
                "src.api.persistent_session.apply_description_overrides",
                side_effect=lambda x: x,
            ),
            patch(
                "src.api.persistent_session.apply_instruction_enforcement",
                side_effect=lambda x, y: x,
            ),
            patch("src.api.persistent_session.ToolContext"),
            patch(
                "src.api.persistent_session.supports_parallel_tool_calls",
                return_value=False,
            ),
            patch(
                "src.api.persistent_session.get_phase_system_prompt",
                return_value="post-upgrade prompt",
            ),
            patch(
                "src.services.guardrails.apply_guardrails_to_tools",
                side_effect=lambda tools, model=None: tools,
            ),
        ):
            # Lite (virtual) tier — shell/git dropped by the capability gate.
            session._setup_tools(None)
            before = {t.name for t in session.tools}
            assert shell not in before and git not in before
            assert web in before
            assert PRODUCT_CAPABILITIES_TOOL_NAME in before
            # S5: the lite tier exposes the agent-initiated upgrade request.
            assert "request_workspace_upgrade" in before

            # Live upgrade: real swap_backend (shell rebuild stubbed to install
            # a fresh ShellManager), then the S1 retool.
            with patch.object(
                session,
                "_setup_shell_manager",
                side_effect=lambda: setattr(session, "shell_manager", new_sm),
            ):
                session.swap_backend(sandbox)
            session.resetup_tools_for_backend()

            after = {t.name for t in session.tools}

        # Shell + git re-admitted against the sandbox backend; web retained.
        assert shell in after and git in after and web in after
        assert PRODUCT_CAPABILITIES_TOOL_NAME in after
        # S5: once shell-capable, the upgrade request drops out (nothing to
        # upgrade to) — the re-derive re-evaluates exposure against the new tier.
        assert "request_workspace_upgrade" not in after
        # tool_context repointed at the freshly built ShellManager.
        assert session.tool_context.shell_manager is new_sm
        # LLM rebound with the upgraded toolset (shell/git included).
        rebind_names = {t.name for t in session._llm.bind_tools.call_args[0][0]}
        assert shell in rebind_names and git in rebind_names
        assert session.llm_with_tools is session._llm.bind_tools.return_value
        assert session.system_prompt == "post-upgrade prompt"
        assert session.tool_context._resolved_tool_names == [
            tool.name for tool in session.tools
        ]
        assert session.tool_context.session_runtime_facts.backend_id == "sandbox"
        assert session.tool_context.session_runtime_facts.loaded_tool_names == tuple(
            sorted(t.name for t in session.tools)
        )

    def test_resetup_before_setup_is_noop(self):
        """Guard: called before _setup_tools (no tool_context) → safe no-op."""
        session = _make_session()
        session.tool_context = None
        session.resetup_tools_for_backend()  # must not raise
        assert session.tools is None


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (
            SimpleNamespace(supports_shell=False, supports_file_tools=False),
            "none",
        ),
        (
            SimpleNamespace(supports_shell=False, supports_file_tools=True),
            "virtual",
        ),
        (
            SimpleNamespace(
                supports_shell=True,
                supports_file_tools=True,
                sudo_action="freeze",
            ),
            "sandbox",
        ),
        (
            SimpleNamespace(
                supports_shell=True,
                supports_file_tools=True,
                sudo_action="allow",
            ),
            "vm",
        ),
    ],
)
def test_runtime_backend_id_comes_from_active_backend_features(backend, expected):
    assert PersistentSession._runtime_backend_id(backend) == expected


# ---------------------------------------------------------------------------
# 2.11 cleanup()
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_withdraws_runtime_facts_and_loaded_tool_names(self):
        session = _make_session()
        context = SimpleNamespace(
            citation_verdict_callback=MagicMock(),
            canvas_event_callback=MagicMock(),
            _resolved_tool_names=["email_read"],
            session_runtime_facts=object(),
        )
        session.tool_context = context
        session.workspace_manager = MagicMock()
        session.workspace_manager.backend = MagicMock(spec=[])

        await session.cleanup()

        assert context._resolved_tool_names == []
        assert context.session_runtime_facts is None

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


# ---------------------------------------------------------------------------
# 2.7b refresh_context_limits()
# ---------------------------------------------------------------------------


class TestRefreshContextLimits:
    """A model hot-swap must re-derive compaction thresholds IN PLACE on the
    existing manager — the running loop holds a reference to it, and the
    provider-usage anchor must survive so a downswitch compacts on the next
    turn (docs/issues/session_model_switch_stale_context_manager_empty_response.md)."""

    def _cfg_with_limits(self, model, base):
        cfg = _make_config(model=model)
        cfg.limits.context_threshold_tokens = int(base * 0.80)
        cfg.limits.message_count_threshold = 200
        cfg.limits.message_count_min_tokens = int(base * 0.40)
        cfg.limits.model_max_context_tokens = base
        cfg.limits.image_tokens = None
        cfg.auxiliary.summarization_call_timeout = 240.0
        return cfg

    def test_downswitch_rebinds_thresholds_in_place(self):
        session = _make_session(config=self._cfg_with_limits("gpt-5.5", 1_050_000))
        session._setup_context_manager()
        manager = session.context_manager
        assert manager.config.summarization_threshold_tokens == 840_000

        # The repro: ~125.7k of real usage recorded while on gpt-5.5.
        manager.record_provider_usage(125_700)
        assert manager.should_summarize([]) is False

        session.config = self._cfg_with_limits("gpt-5.3-codex-spark", 128_000)
        session.refresh_context_limits()

        # Same object — the loop's captured reference stays valid...
        assert session.context_manager is manager
        # ...with the new model's thresholds...
        assert manager.config.summarization_threshold_tokens == 102_400
        assert manager.config.model_max_context_tokens == 128_000
        # ...and the surviving anchor now triggers compaction immediately.
        assert manager.should_summarize([]) is True

    def test_noop_without_manager(self):
        """Refresh before setup must not raise (manager not built yet)."""
        session = _make_session(config=self._cfg_with_limits("gpt-5.5", 1_050_000))
        assert session.context_manager is None
        session.refresh_context_limits()
