"""The use_skill agent tool (Agent Skills, Slice 2).

Loads skills/<name>/SKILL.md (L2) from the workspace. Exercised against the
filesystem test backend, mirroring how other workspace-tool tests build a
WorkspaceManager + ToolContext.
"""

from tests._fs_backend import FilesystemTestBackend
from src.core.workspace import WorkspaceManager
from src.tools.context import ToolContext
from src.tools.workspace.skills import create_skill_tools


def _use_skill(tmp_path):
    ws = WorkspaceManager(
        job_id="t", base_path=tmp_path, backend=FilesystemTestBackend(tmp_path)
    )
    ctx = ToolContext(workspace_manager=ws)
    tools = {t.name: t for t in create_skill_tools(ctx)}
    return ws, tools["use_skill"]


def test_use_skill_returns_body(tmp_path):
    ws, use_skill = _use_skill(tmp_path)
    ws.backend.mkdir("skills/hello-skill")
    ws.write_file(
        "skills/hello-skill/SKILL.md", "---\nname: hello-skill\n---\nBODY-HERE"
    )
    out = use_skill.invoke({"skill_name": "hello-skill"})
    assert "BODY-HERE" in out
    assert "hello-skill" in out


def test_use_skill_missing_is_friendly(tmp_path):
    _ws, use_skill = _use_skill(tmp_path)
    out = use_skill.invoke({"skill_name": "nope"})
    assert "not found" in out.lower()


def test_use_skill_metadata_registered():
    from src.tools.workspace.skills import SKILL_TOOLS_METADATA

    assert "use_skill" in SKILL_TOOLS_METADATA
    assert SKILL_TOOLS_METADATA["use_skill"]["category"] == "workspace"


# --- Slice 4: script-availability note on shell-less tiers ---


class _ShellFsBackend(FilesystemTestBackend):
    """A test backend that reports a shell (mirrors RemoteBackend/sandbox)."""

    @property
    def supports_shell(self) -> bool:
        return True


def _use_skill_on(backend, tmp_path):
    ws = WorkspaceManager(job_id="t", base_path=tmp_path, backend=backend)
    ctx = ToolContext(workspace_manager=ws)
    tools = {t.name: t for t in create_skill_tools(ctx)}
    return ws, tools["use_skill"]


def _make_script_skill(ws):
    ws.backend.mkdir("skills/word-count/scripts")
    ws.write_file(
        "skills/word-count/SKILL.md",
        "---\nname: word-count\n---\nRun skills/word-count/scripts/wordcount.py",
    )
    ws.write_file("skills/word-count/scripts/wordcount.py", "print('x')")


def test_script_skill_on_lite_tier_appends_upgrade_note(tmp_path):
    # FilesystemTestBackend.supports_shell == False (the virtual-tier case)
    ws, use_skill = _use_skill_on(FilesystemTestBackend(tmp_path), tmp_path)
    _make_script_skill(ws)
    out = use_skill.invoke({"skill_name": "word-count"})
    assert "Run skills/word-count/scripts/wordcount.py" in out  # body still delivered
    assert "request_workspace_upgrade" in out  # the affordance is named
    assert "cannot be executed" in out.lower()


def test_script_skill_with_shell_has_no_note(tmp_path):
    ws, use_skill = _use_skill_on(_ShellFsBackend(tmp_path), tmp_path)
    _make_script_skill(ws)
    out = use_skill.invoke({"skill_name": "word-count"})
    assert "wordcount.py" in out  # body still delivered
    assert "request_workspace_upgrade" not in out  # shell present → no nag


def test_prompt_only_skill_on_lite_tier_has_no_note(tmp_path):
    ws, use_skill = _use_skill_on(FilesystemTestBackend(tmp_path), tmp_path)
    ws.backend.mkdir("skills/hello-skill")
    ws.write_file(
        "skills/hello-skill/SKILL.md", "---\nname: hello-skill\n---\nJust guidance."
    )
    out = use_skill.invoke({"skill_name": "hello-skill"})
    assert "Just guidance." in out
    assert "request_workspace_upgrade" not in out  # no scripts/ → no note
