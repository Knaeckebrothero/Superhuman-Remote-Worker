"""repo_* tools: thin, read_only-gated wrappers over GitManager + the forge adapter."""

from unittest.mock import MagicMock, patch

import pytest

from src.tools.context import ToolContext
from src.tools.repo import create_repo_tools


def make_context(read_only=False, forge="github"):
    ws = MagicMock()
    git_mgr = MagicMock()
    git_mgr.commit.return_value = True
    git_mgr.push.return_value = True
    git_mgr.pull.return_value = True
    git_mgr.current_branch.return_value = "job/abc12345"
    ws.source_repos = {"widget": git_mgr}
    ws.source_repo_meta = {
        "widget": {
            "forge": forge,
            "api_base": "https://api.github.com",
            "owner": "acme",
            "repo": "widget",
            "token": "tok",
            "read_only": read_only,
            "default_branch": "develop",
        }
    }
    return ToolContext(workspace_manager=ws), git_mgr


def get_tool(tools, name):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_repo_commit_commits_in_the_named_clone():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_commit")

    out = await tool.ainvoke({"repo": "widget", "message": "fix: thing"})

    git_mgr.commit.assert_called_once_with("fix: thing")
    assert "fix: thing" in out or "committed" in out.lower()


@pytest.mark.asyncio
async def test_repo_push_pushes_current_branch():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.push.assert_called_once()


@pytest.mark.asyncio
async def test_write_tools_refuse_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tools = create_repo_tools(context)

    for name in ("repo_commit", "repo_push", "repo_open_pr"):
        tool = get_tool(tools, name)
        kwargs = {"repo": "widget"}
        if name == "repo_commit":
            kwargs["message"] = "m"
        if name == "repo_open_pr":
            kwargs.update({"title": "T", "base": "develop"})
        out = await tool.ainvoke(kwargs)
        assert "read-only" in out.lower()

    git_mgr.commit.assert_not_called()
    git_mgr.push.assert_not_called()


@pytest.mark.asyncio
async def test_repo_pull_is_allowed_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tool = get_tool(create_repo_tools(context), "repo_pull")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.pull.assert_called_once()


@pytest.mark.asyncio
async def test_repo_open_pr_calls_the_forge_adapter():
    context, _ = make_context()
    tool = get_tool(create_repo_tools(context), "repo_open_pr")

    with patch(
        "src.tools.repo.repo_tools.open_pull_request",
        return_value={"number": 9, "url": "https://gh/pr/9"},
    ) as mock_pr:
        out = await tool.ainvoke(
            {"repo": "widget", "title": "T", "base": "develop", "body": "B"}
        )

    target = mock_pr.call_args[0][0]
    assert target.forge == "github"
    assert target.owner == "acme"
    assert target.token == "tok"
    # head defaults to the branch currently checked out in that clone.
    assert mock_pr.call_args[1]["head"] == "job/abc12345"
    assert "https://gh/pr/9" in out


@pytest.mark.asyncio
async def test_unknown_repo_name_is_a_clear_error():
    context, _ = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "nope"})

    assert "nope" in out
    assert "widget" in out  # names the ones that DO exist
