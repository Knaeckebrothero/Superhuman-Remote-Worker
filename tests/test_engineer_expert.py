"""The engineer expert — the universal software worker.

Design: knowledge-base/knowledge/features/engineer_expert.md. Pins the grant
shape (shell + the full workspace edit set, no TDD scaffolding), the prose
contract (every guarded tool name disappears with its grant; no Jinja
residue), the bootstrap todos, and the routing surface (the description
carves strict TDD out to the developer).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from shared.runtime.core.loader import (
    load_and_merge_config,
    load_strategic_todos_template,
    render_instruction_content,
    resolve_config_path,
)

_CONFIG = Path(__file__).resolve().parents[1] / "config"
_DIR = _CONFIG / "experts" / "engineer"

_BASE_TOOLS = [
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "search_files",
    "file_exists",
    "get_document_info",
    "use_skill",
    "next_phase_todos",
    "todo_complete",
    "request_replan",
    "job_complete",
    "web_search",
    "extract_webpage",
    "research_topic",
]
_SHELL_TOOLS = ["run_command", "shell_read", "cancel_command"]
_REPO_TOOLS = ["repo_checkout", "repo_commit", "repo_push", "repo_open_pr"]
_KB_TOOLS = ["kb_write", "kb_search", "kb_update", "kb_list", "kb_contradictions"]
_DELEGATION_TOOLS = [
    "delegate_agent",
    "wait_agent",
    "message_agent",
    "stop_agent",
    "list_agents",
]
_ALL = _BASE_TOOLS + _SHELL_TOOLS + _REPO_TOOLS + _KB_TOOLS + _DELEGATION_TOOLS


def _merged() -> dict:
    path, _ = resolve_config_path("engineer")
    return load_and_merge_config(path) or {}


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


# --- config ---------------------------------------------------------------


def test_engineer_is_a_worker_with_shell_and_the_full_edit_set():
    tools = _merged()["tools"]
    assert set(tools["shell"]) == {"run_command", "cancel_command", "shell_read"}
    # The developer's write_file-only grant was a handicap; the engineer edits.
    for name in (
        "read_file",
        "write_file",
        "edit_file",
        "create_directory",
        "move_file",
        "copy_file",
        "delete_file",
        "search_files",
    ):
        assert name in tools["workspace"], name
    assert "todo_list" in tools["core"]
    assert set(_DELEGATION_TOOLS) <= set(tools["delegation"])
    assert tools["citation"] == []
    assert tools["graph"] == []


def test_engineer_pins_sandbox_and_git_versioning():
    ws = _merged()["workspace"]
    assert ws["backend"] == "sandbox"
    assert ws["git_versioning"] is True
    assert "repo" not in [str(s).strip("/") for s in ws["structure"]]


def test_engineer_delegates_to_the_library_roster():
    merged = _merged()
    assert merged["delegation"]["enabled"] is True
    assert set(merged["subagents"]["roster"]) == {
        "explorer",
        "implementer",
        "tester",
        "reviewer",
    }
    assert merged["subagents"]["default"] == "explorer"


def test_engineer_description_routes_code_work_and_carves_out_tdd():
    raw = yaml.safe_load(_read("config.yaml"))
    desc = raw["description"].lower()
    for word in ("frontend", "backend", "script", "install", "shell"):
        assert word in desc, word
    assert "tdd" in desc and "developer" in desc
    assert raw["$extends"] == "worker_base"
    assert set(raw["tags"]) >= {"coding", "engineering", "ops", "delegation"}


# --- prose ----------------------------------------------------------------


def _assert_clean(text: str, label: str) -> None:
    assert text.strip(), f"{label}: empty"
    assert "{%" not in text and "{{" not in text, f"{label}: Jinja residue"


def test_no_tdd_scaffolding_anywhere():
    assert not (_DIR / "skills").exists(), "engineer inherits the generic phase skills"
    for path in sorted(_DIR.rglob("*")):
        if path.is_file() and path.suffix in (".md", ".txt", ".yaml"):
            text = path.read_text(encoding="utf-8")
            for token in ("spec_lock", "tdd_phase", "EARS", "spec.yaml"):
                assert token not in text, f"{path.name} still carries {token}"


def test_persona_is_content_not_a_template():
    persona = _read("persona.txt")
    assert "{" not in persona and "}" not in persona
    for section in ("<role>", "<goal>", "<operating_rules>", "<identity_anchors>"):
        assert section in persona
    assert "never weaken" in persona.lower()


def test_instructions_render_clean_with_every_grant_combination():
    content = _read("instructions.md")
    full = render_instruction_content(content, _ALL)
    _assert_clean(full, "full")
    for needle in (
        "repo_open_pr(",
        "delegate_agent",
        "run_command",
        "kb_write",
        "repos/<name>/",
        "test-driven-development",
        "ONE execution phase",
    ):
        assert needle in full, needle

    no_repo = render_instruction_content(
        content, [t for t in _ALL if t not in _REPO_TOOLS]
    )
    _assert_clean(no_repo, "no_repo")
    assert "repo_open_pr" not in no_repo and "repo_checkout" not in no_repo
    assert "repos/<name>/" in no_repo

    no_delegation = render_instruction_content(
        content, [t for t in _ALL if t not in _DELEGATION_TOOLS]
    )
    _assert_clean(no_delegation, "no_delegation")
    assert "delegate_agent" not in no_delegation
    assert "`explorer`" not in no_delegation and "`implementer`" not in no_delegation

    no_shell = render_instruction_content(
        content, [t for t in _ALL if t not in _SHELL_TOOLS]
    )
    _assert_clean(no_shell, "no_shell")
    for name in _SHELL_TOOLS:
        assert name not in no_shell, name

    no_kb = render_instruction_content(content, [t for t in _ALL if t not in _KB_TOOLS])
    _assert_clean(no_kb, "no_kb")
    assert "kb_write" not in no_kb
    assert "notes/" in no_kb


def test_instructions_stay_short():
    lines = [ln for ln in _read("instructions.md").splitlines() if ln.strip()]
    assert len(lines) <= 110, len(lines)


def test_workspace_template_tracks_the_repository_not_a_spec():
    text = _read("workspace_template.md")
    for heading in ("## Repository", "## Deliverables", "## Failed Approaches"):
        assert heading in text
    assert "Test command" in text


def test_bootstrap_todos_parse_and_follow_the_grants():
    raw = yaml.safe_load(_read("strategic_todos_initial.yaml"))
    assert [t["id"] for t in raw["todos"]] == [1, 2, 3, 4]

    on = load_strategic_todos_template(
        "strategic_todos_initial.yaml", deployment_dir=str(_DIR), tool_names=_ALL
    )
    on_text = "\n".join(t["content"] for t in on)
    assert len(on) == 4
    assert "delegate_agent" in on_text and "`explorer`" in on_text
    assert "working_dir=" in on_text and "kb_write" in on_text
    assert "next_phase_todos" in on_text and "ONE execution phase" in on_text

    off = load_strategic_todos_template(
        "strategic_todos_initial.yaml",
        deployment_dir=str(_DIR),
        tool_names=[t for t in _BASE_TOOLS if t != "use_skill"],
    )
    off_text = "\n".join(t["content"] for t in off)
    assert len(off) == 4
    assert "delegate_agent" not in off_text and "`explorer`" not in off_text
    assert "working_dir=" not in off_text and "run_command" not in off_text
    assert "kb_write" not in off_text and "notes/" in off_text


def test_model_matrix_pins_the_engineers_own_files():
    matrix = yaml.safe_load(_read("model_config_matrix.yaml"))
    default = matrix["default"]
    assert default["prompts"]["persona"] == "persona.txt"
    assert default["instructions"] == {
        "instructions": "instructions.md",
        "strategic_todos_initial": "strategic_todos_initial.yaml",
        "workspace_template": "workspace_template.md",
    }
    for name in (
        "persona.txt",
        "instructions.md",
        "strategic_todos_initial.yaml",
        "workspace_template.md",
    ):
        assert (_DIR / name).is_file(), name
