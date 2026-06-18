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
