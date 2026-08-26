"""Expert prompts must describe the attached-repository layout the platform builds.

A repository datasource is cloned to ``repos/<name>/``
(``src/core/datasource_setup.py``, ``clone_repository_datasources``) and the
workspace ``README.md`` lists each one by that clone name with its base branch.
Nothing populates a singular ``repo/``. The developer prompts hard-coded one
anyway (``cd repo && ...``, ``list_files on repo/``, ``repo/tests/...``) and
``workspace.structure`` even created an empty ``repo/`` at init, so the model
was pointed at a directory that never held code while the real checkout sat
next to it. The audit trail shows the follow-on: a ``cd`` in the default tab,
then ``git -C repos/<name>`` failing with "cannot change to 'repos/<name>'".

Pinned here:

- no expert's merged ``workspace.structure`` creates ``repo/``;
- no prose file under any expert directory names the singular ``repo/`` path or
  tells the model to ``cd repo`` (``repos/...`` is the correct spelling);
- the developer's delivery steps (``repo_checkout`` .. ``repo_open_pr``) render
  only when the repo tools are bound, through the same ``has_tool`` seam
  ``agent.py`` renders instruction files with;
- every developer prose file still renders as a Jinja template with and
  without the repo tools, and the bootstrap todo file still parses as YAML.
"""

import re
from pathlib import Path

import pytest
import yaml

from src.core.loader import (
    load_and_merge_config,
    render_instruction_content,
    resolve_config_path,
)

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_DEVELOPER_DIR = _CONFIG_DIR / "experts" / "developer"

# The singular path. ``repos/`` must not match (``repo`` followed by ``s``),
# and neither must ``my_repo/`` (the lookbehind excludes ``[a-z_]``).
_SINGULAR_REPO_RE = re.compile(r"(?<![a-z_])repo/")
_CD_REPO_RE = re.compile(r"\bcd repo\b")

_PROSE_SUFFIXES = (".txt", ".md", ".yaml")

_BASE_TOOLS = ["read_file", "write_file", "list_files", "run_command", "kb_write"]
_REPO_TOOLS = ["repo_checkout", "repo_commit", "repo_push", "repo_open_pr"]


def _expert_names() -> list[str]:
    return sorted(
        p.parent.name
        for p in _CONFIG_DIR.glob("experts/*/config.yaml")
        if p.parent.name != "__pycache__"
    )


def _prose_files(expert: str) -> list[Path]:
    return sorted(
        p
        for p in (_CONFIG_DIR / "experts" / expert).rglob("*")
        if p.is_file() and p.suffix in _PROSE_SUFFIXES
    )


@pytest.mark.parametrize("expert", _expert_names())
def test_expert_workspace_structure_does_not_create_repo_dir(expert):
    path, _ = resolve_config_path(expert)
    merged = load_and_merge_config(path) or {}
    structure = ((merged.get("workspace") or {}).get("structure")) or []
    offenders = [
        entry
        for entry in structure
        if isinstance(entry, str) and entry.strip("/") == "repo"
    ]
    assert not offenders, (
        f"expert '{expert}' workspace.structure creates {offenders} — repository "
        "datasources are cloned to repos/<name>/, nothing ever fills repo/"
    )


@pytest.mark.parametrize("expert", _expert_names())
def test_expert_prose_never_names_singular_repo_path(expert):
    offenders: list[str] = []
    for path in _prose_files(expert):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _SINGULAR_REPO_RE.search(line) or _CD_REPO_RE.search(line):
                rel = path.relative_to(_CONFIG_DIR)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"expert '{expert}' prose points the model at a singular repo/ that no "
        "code path populates (the checkout is repos/<name>/):\n  "
        + "\n  ".join(offenders)
    )


def test_singular_pattern_flags_repo_but_not_repos():
    """Teeth: the regexes catch the bug shape and spare the correct spelling."""
    assert _SINGULAR_REPO_RE.search("Use list_files on repo/ first")
    assert _SINGULAR_REPO_RE.search("in `repo/tests/test_x.py`")
    assert _CD_REPO_RE.search("Use `cd repo && pytest` for repo work")
    assert not _SINGULAR_REPO_RE.search("cloned at repos/<name>/")
    assert not _SINGULAR_REPO_RE.search("git -C repos/app status")
    assert not _SINGULAR_REPO_RE.search("my_repo/ is a different thing")
    assert not _CD_REPO_RE.search("cd repos/<name>")


def test_developer_delivery_steps_render_only_when_repo_tools_bound():
    content = (_DEVELOPER_DIR / "instructions.md").read_text(encoding="utf-8")

    with_repo = render_instruction_content(content, _BASE_TOOLS + _REPO_TOOLS)
    for step in ("repo_checkout(", "repo_commit(", "repo_push(", "repo_open_pr("):
        assert step in with_repo, f"delivery step {step} missing with repo tools bound"
    assert "Do **not** merge" in with_repo

    without_repo = render_instruction_content(content, _BASE_TOOLS)
    assert "repo_open_pr" not in without_repo
    assert "repo_checkout" not in without_repo
    # The surrounding sections survive the guard in both renderings.
    for rendered in (with_repo, without_repo):
        assert "## 7. PR / Final Review" in rendered
        assert "## Tool Reference" in rendered
        assert "## Working Directories" in rendered


def test_developer_todo_guide_integration_example_is_guarded():
    content = (_DEVELOPER_DIR / "todo_guide.md").read_text(encoding="utf-8")
    with_repo = render_instruction_content(content, _BASE_TOOLS + _REPO_TOOLS)
    assert "repo_open_pr(" in with_repo
    assert "Push to origin <branch>" not in with_repo
    without_repo = render_instruction_content(content, _BASE_TOOLS)
    assert "repo_open_pr" not in without_repo
    assert "Push to origin <branch>" in without_repo


def test_developer_working_directories_table_names_the_clone_path():
    content = (_DEVELOPER_DIR / "instructions.md").read_text(encoding="utf-8")
    assert "| `README.md` |" in content
    assert "| `repos/<name>/` |" in content
    assert "| `repos/<name>/tests/` |" in content
    assert "| `repos/<name>/src/`" in content
    for kept in ("| workspace root |", "| `documents/` |", "| `output/` |"):
        assert kept in content


def test_developer_bootstrap_todo_reads_readme_before_exploring():
    data = yaml.safe_load(
        (_DEVELOPER_DIR / "strategic_todos_initial.yaml").read_text(encoding="utf-8")
    )
    todos = {t["id"]: t["content"] for t in data["todos"]}
    explore = todos[2]
    assert "(1) Read README.md" in explore
    assert "repos/<name>/" in explore
    # Honest failure mode: no repository attached means say so, not invent one.
    assert "lists no repository" in explore


@pytest.mark.parametrize(
    "path",
    sorted(p for p in _prose_files("developer") if p.name != "config.yaml"),
    ids=lambda p: p.name,
)
def test_developer_prose_files_render_with_and_without_repo_tools(path):
    """A Jinja syntax slip in any prompt file would break every developer job."""
    content = path.read_text(encoding="utf-8")
    assert render_instruction_content(content, _BASE_TOOLS + _REPO_TOOLS)
    assert render_instruction_content(content, _BASE_TOOLS)
