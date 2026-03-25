"""Tests for instruction matrix resolution: InstructionMatrixResolver."""

import textwrap
from unittest.mock import patch

import pytest

from src.core.loader import (
    AgentConfig,
    FileResolver,
    InstructionMatrixResolver,
    MatrixResolver,
    PromptMatrixResolver,
    PromptResolver,
    load_instructions,
    serialize_resolved_config,
    load_config_from_resolved,
)


# =============================================================================
# FileResolver tests (generalized from PromptResolver)
# =============================================================================


class TestFileResolver:
    """Tests for FileResolver (generalized PromptResolver)."""

    def test_backward_compat_alias(self):
        """PromptResolver is an alias for FileResolver."""
        assert PromptResolver is FileResolver

    def test_default_framework_dir(self):
        """Without framework_dir, defaults to config/prompts/."""
        resolver = FileResolver()
        assert resolver.framework_dir.name == "prompts"

    def test_custom_framework_dir(self, tmp_path):
        """Custom framework_dir is used for fallback."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "test.md").write_text("template content")

        resolver = FileResolver(framework_dir=templates_dir)
        assert resolver.load("test.md") == "template content"

    def test_deployment_dir_takes_priority(self, tmp_path):
        """Deployment dir files override framework dir files."""
        deploy_dir = tmp_path / "deploy"
        framework_dir = tmp_path / "framework"
        deploy_dir.mkdir()
        framework_dir.mkdir()
        (deploy_dir / "test.md").write_text("deploy version")
        (framework_dir / "test.md").write_text("framework version")

        resolver = FileResolver(
            deployment_dir=str(deploy_dir),
            framework_dir=framework_dir,
        )
        assert resolver.load("test.md") == "deploy version"

    def test_falls_back_to_framework(self, tmp_path):
        """Falls back to framework when deployment dir doesn't have the file."""
        deploy_dir = tmp_path / "deploy"
        framework_dir = tmp_path / "framework"
        deploy_dir.mkdir()
        framework_dir.mkdir()
        (framework_dir / "test.md").write_text("framework version")

        resolver = FileResolver(
            deployment_dir=str(deploy_dir),
            framework_dir=framework_dir,
        )
        assert resolver.load("test.md") == "framework version"

    def test_file_not_found(self, tmp_path):
        """Raises FileNotFoundError when file not found anywhere."""
        resolver = FileResolver(framework_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            resolver.load("nonexistent.md")


# =============================================================================
# MatrixResolver base class tests
# =============================================================================


class TestMatrixResolver:
    """Tests for the MatrixResolver base class mechanics."""

    def test_subclass_constants(self):
        """PromptMatrixResolver and InstructionMatrixResolver have correct constants."""
        assert PromptMatrixResolver.MATRIX_FILENAME == "prompt_matrix.yaml"
        assert PromptMatrixResolver.FRAMEWORK_DIR == "config/prompts"
        assert "systemprompt" in PromptMatrixResolver.HARDCODED_DEFAULTS
        assert "instructions" not in PromptMatrixResolver.HARDCODED_DEFAULTS

        assert InstructionMatrixResolver.MATRIX_FILENAME == "instruction_matrix.yaml"
        assert InstructionMatrixResolver.FRAMEWORK_DIR == "config/templates"
        assert "instructions" in InstructionMatrixResolver.HARDCODED_DEFAULTS
        assert "workspace_template" in InstructionMatrixResolver.HARDCODED_DEFAULTS

    def test_shared_load_matrix_from_path(self, tmp_path):
        """_load_matrix_from_path is inherited and works for both subclasses."""
        matrix_path = tmp_path / "test_matrix.yaml"
        matrix_path.write_text(
            textwrap.dedent("""\
            default:
              foo: bar.txt
        """)
        )

        # Both subclasses can use the static method
        result_prompt = PromptMatrixResolver._load_matrix_from_path(matrix_path)
        result_instr = InstructionMatrixResolver._load_matrix_from_path(matrix_path)
        assert result_prompt == result_instr == {"default": {"foo": "bar.txt"}}


# =============================================================================
# InstructionMatrixResolver tests
# =============================================================================


class TestInstructionMatrixResolver:
    """Tests for InstructionMatrixResolver fallback chain."""

    def test_default_fallback_no_matrix_files(self, tmp_path):
        """Without any matrix files, hardcoded defaults are used."""
        resolver = InstructionMatrixResolver(
            deployment_dir=str(tmp_path),
            model_family="claude-opus",
        )
        assert resolver.resolve_filename("instructions") == "instructions.md"
        assert (
            resolver.resolve_filename("strategic_todos_initial")
            == "strategic_todos_initial.yaml"
        )
        assert (
            resolver.resolve_filename("workspace_template") == "workspace_template.md"
        )
        assert resolver.resolve_filename("todo_guide") == "todo_guide.md"

    def test_base_matrix_default_resolution(self, tmp_path):
        """Base instruction matrix default entries are used."""
        base_matrix = tmp_path / "config" / "instruction_matrix.yaml"
        base_matrix.parent.mkdir(parents=True)
        base_matrix.write_text(
            textwrap.dedent("""\
            default:
              instructions: custom_instructions.md
              workspace_template: custom_workspace.md
        """)
        )

        with patch.object(MatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = InstructionMatrixResolver.__new__(InstructionMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "default"
            resolver._file_resolver = FileResolver(framework_dir=tmp_path)
            resolver._expert_matrix = {}
            resolver._base_matrix = InstructionMatrixResolver._load_matrix_from_path(
                base_matrix
            )

        assert resolver.resolve_filename("instructions") == "custom_instructions.md"
        assert resolver.resolve_filename("workspace_template") == "custom_workspace.md"
        # Not in matrix, falls to hardcoded
        assert resolver.resolve_filename("todo_guide") == "todo_guide.md"

    def test_expert_override(self, tmp_path):
        """Expert instruction matrix entries override base matrix entries."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              instructions: base_instructions.md
              workspace_template: base_workspace.md
        """)
        )

        expert_matrix_path = tmp_path / "expert_matrix.yaml"
        expert_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              instructions: expert_instructions.md
        """)
        )

        with patch.object(MatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = InstructionMatrixResolver.__new__(InstructionMatrixResolver)
            resolver.deployment_dir = tmp_path
            resolver.model_family = "default"
            resolver._file_resolver = FileResolver(
                deployment_dir=str(tmp_path),
                framework_dir=tmp_path,
            )
            resolver._expert_matrix = InstructionMatrixResolver._load_matrix_from_path(
                expert_matrix_path
            )
            resolver._base_matrix = InstructionMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

        # Expert overrides instructions
        assert resolver.resolve_filename("instructions") == "expert_instructions.md"
        # Base provides workspace_template (expert doesn't override it)
        assert resolver.resolve_filename("workspace_template") == "base_workspace.md"

    def test_model_specific_entry(self, tmp_path):
        """Model-specific entries take priority over default entries."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              instructions: default_instructions.md
            claude-opus:
              instructions: claude_instructions.md
        """)
        )

        with patch.object(MatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = InstructionMatrixResolver.__new__(InstructionMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "claude-opus"
            resolver._file_resolver = FileResolver(framework_dir=tmp_path)
            resolver._expert_matrix = {}
            resolver._base_matrix = InstructionMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

        assert resolver.resolve_filename("instructions") == "claude_instructions.md"

    def test_load_from_expert_directory(self, tmp_path):
        """Expert file takes priority in file search."""
        expert_dir = tmp_path / "expert"
        expert_dir.mkdir()
        (expert_dir / "instructions.md").write_text("expert instructions content")
        (expert_dir / "instruction_matrix.yaml").write_text(
            textwrap.dedent("""\
            default:
              instructions: instructions.md
        """)
        )

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            # Also create framework files
            config_templates = tmp_path / "config" / "templates"
            config_templates.mkdir(parents=True)
            (config_templates / "instructions.md").write_text(
                "framework instructions content"
            )

            resolver = InstructionMatrixResolver(
                deployment_dir=str(expert_dir),
                model_family="default",
            )
            result = resolver.load("instructions")
            assert result == "expert instructions content"

    def test_load_falls_through_to_framework(self, tmp_path):
        """When expert dir doesn't have the file, framework dir is used."""
        expert_dir = tmp_path / "expert"
        expert_dir.mkdir()

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_templates = tmp_path / "config" / "templates"
            config_templates.mkdir(parents=True)
            (config_templates / "instructions.md").write_text("framework instructions")

            resolver = InstructionMatrixResolver(
                deployment_dir=str(expert_dir),
                model_family="default",
            )
            result = resolver.load("instructions")
            assert result == "framework instructions"


# =============================================================================
# load_instructions() with InstructionMatrixResolver
# =============================================================================


class TestLoadInstructions:
    """Test that load_instructions uses InstructionMatrixResolver."""

    def test_load_instructions_from_framework(self, tmp_path):
        """load_instructions resolves via instruction matrix."""
        config = AgentConfig(agent_id="test", display_name="Test Agent")

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_templates = tmp_path / "config" / "templates"
            config_templates.mkdir(parents=True)
            (config_templates / "instructions.md").write_text("framework instructions")

            result = load_instructions(config, model="gpt-4o")
            assert result == "framework instructions"

    def test_load_instructions_from_resolved(self):
        """load_instructions uses pre-resolved content when available."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test Agent",
            extra={"_resolved_instructions": {"instructions": "frozen instructions"}},
        )

        result = load_instructions(config, model="gpt-4o")
        assert result == "frozen instructions"


# =============================================================================
# Resolved Config Serialization tests
# =============================================================================


class TestResolvedConfigSerialization:
    """Tests for serialize_resolved_config and load_config_from_resolved."""

    def test_serialize_captures_prompts_and_instructions(self, tmp_path):
        """serialize_resolved_config captures all prompts and instructions."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test Agent",
            _deployment_dir=str(tmp_path),
        )

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            # Create prompt files
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text("system prompt content")
            (config_prompts / "strategic.txt").write_text("strategic content")
            (config_prompts / "tactical.txt").write_text("tactical content")
            (config_prompts / "summarization_prompt.txt").write_text(
                "summarization content"
            )

            # Create instruction files
            config_templates = tmp_path / "config" / "templates"
            config_templates.mkdir(parents=True)
            (config_templates / "instructions.md").write_text("instructions content")
            (config_templates / "workspace_template.md").write_text(
                "workspace template"
            )
            (config_templates / "todo_guide.md").write_text("todo guide")
            (config_templates / "strategic_todos_initial.yaml").write_text(
                "todos:\n  - id: 1\n    content: test todo content here"
            )
            (config_templates / "strategic_todos_transition.yaml").write_text(
                "todos:\n  - id: 1\n    content: transition todo content"
            )
            (config_templates / "strategic_todos_resume.yaml").write_text(
                "todos:\n  - id: 1\n    content: resume todo content here"
            )

            result = serialize_resolved_config(config, model="gpt-4o")

        # Check structure
        assert "agent" in result
        assert "prompts" in result
        assert "instructions" in result
        assert "model_family" in result
        assert "resolved_at" in result

        # Check prompts captured
        assert result["prompts"]["systemprompt"] == "system prompt content"
        assert result["prompts"]["strategic"] == "strategic content"
        assert result["prompts"]["tactical"] == "tactical content"
        assert result["prompts"]["summarization"] == "summarization content"

        # Check instructions captured
        assert result["instructions"]["instructions"] == "instructions content"
        assert result["instructions"]["workspace_template"] == "workspace template"
        assert result["instructions"]["todo_guide"] == "todo guide"

        # Check API keys stripped
        assert "api_key" not in result["agent"].get("llm", {})

        # Check internal fields stripped
        assert "_deployment_dir" not in result["agent"]

    def test_load_config_from_resolved(self, tmp_path):
        """load_config_from_resolved reconstructs config with pre-resolved content."""
        resolved = {
            "agent": {
                "agent_id": "test",
                "display_name": "Test Agent",
                "description": "",
                "llm": {"model": "gpt-4o", "temperature": 0.0},
                "workspace": {},
                "tools": {},
                "connections": {},
                "limits": {},
                "context_management": {},
                "phase_settings": {},
                "extra": {},
            },
            "prompts": {
                "systemprompt": "frozen system prompt",
                "strategic": "frozen strategic",
            },
            "instructions": {
                "instructions": "frozen instructions",
                "workspace_template": "frozen workspace template",
            },
            "model_family": "gpt-4o",
            "resolved_at": "2026-02-16T00:00:00+00:00",
        }

        config = load_config_from_resolved(resolved)

        assert config.agent_id == "test"
        assert config.display_name == "Test Agent"
        assert (
            config.extra["_resolved_prompts"]["systemprompt"] == "frozen system prompt"
        )
        assert (
            config.extra["_resolved_instructions"]["instructions"]
            == "frozen instructions"
        )

    def test_round_trip_serialize_deserialize(self, tmp_path):
        """Serialized config can be deserialized and used."""
        config = AgentConfig(
            agent_id="round_trip",
            display_name="Round Trip Agent",
            _deployment_dir=str(tmp_path),
        )

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text("base {prompt_content}")
            (config_prompts / "strategic.txt").write_text("strategic {phase_number}")
            (config_prompts / "tactical.txt").write_text("tactical {phase_number}")
            (config_prompts / "summarization_prompt.txt").write_text("summarize")

            config_templates = tmp_path / "config" / "templates"
            config_templates.mkdir(parents=True)
            (config_templates / "instructions.md").write_text("instructions")
            (config_templates / "workspace_template.md").write_text("workspace")
            (config_templates / "todo_guide.md").write_text("todo guide")
            (config_templates / "strategic_todos_initial.yaml").write_text(
                "todos:\n  - id: 1\n    content: initial todo content"
            )
            (config_templates / "strategic_todos_transition.yaml").write_text(
                "todos:\n  - id: 1\n    content: transition todo go"
            )
            (config_templates / "strategic_todos_resume.yaml").write_text(
                "todos:\n  - id: 1\n    content: resume todos content"
            )

            serialized = serialize_resolved_config(config, model="gpt-4o")

        # Deserialize
        restored = load_config_from_resolved(serialized)
        assert restored.agent_id == "round_trip"
        assert restored.extra["_resolved_prompts"]["systemprompt"] is not None
        assert (
            restored.extra["_resolved_instructions"]["instructions"] == "instructions"
        )

        # Use with load_instructions
        result = load_instructions(restored, model="gpt-4o")
        assert result == "instructions"
