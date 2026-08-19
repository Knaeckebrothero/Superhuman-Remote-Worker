"""repo_* tools: thin, read_only-gated wrappers over GitManager + the forge adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

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
    git_mgr.rev_parse.return_value = None
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
async def test_repo_checkout_switches_to_an_existing_branch():
    context, git_mgr = make_context()
    git_mgr.rev_parse.return_value = "a" * 40  # branch exists locally
    git_mgr.checkout_branch.return_value = True
    git_mgr.current_branch.return_value = "feature/x"
    tool = get_tool(create_repo_tools(context), "repo_checkout")

    out = await tool.ainvoke({"repo": "widget", "branch": "feature/x"})

    git_mgr.checkout_branch.assert_called_once_with("feature/x", create=False)
    assert out == "Switched widget to branch 'feature/x'."


@pytest.mark.asyncio
async def test_repo_checkout_creates_a_missing_branch_when_asked():
    context, git_mgr = make_context()
    git_mgr.rev_parse.return_value = None  # branch does not exist yet
    git_mgr.checkout_branch.return_value = True
    git_mgr.current_branch.return_value = "job/new-branch"
    tool = get_tool(create_repo_tools(context), "repo_checkout")

    out = await tool.ainvoke(
        {"repo": "widget", "branch": "job/new-branch", "create": True}
    )

    git_mgr.checkout_branch.assert_called_once_with("job/new-branch", create=True)
    assert out == "Created and switched widget to branch 'job/new-branch'."


@pytest.mark.asyncio
async def test_repo_checkout_failure_points_at_create_flag():
    """The trapped-worker case: a checkout of a missing branch must say how
    to create it, not leave the agent guessing (job 12a0e92c)."""
    context, git_mgr = make_context()
    git_mgr.checkout_branch.return_value = False
    tool = get_tool(create_repo_tools(context), "repo_checkout")

    out = await tool.ainvoke({"repo": "widget", "branch": "gone"})

    assert "create=True" in out
    assert "'gone'" in out


@pytest.mark.asyncio
async def test_repo_checkout_create_failure_does_not_suggest_create_again():
    context, git_mgr = make_context()
    git_mgr.checkout_branch.return_value = False
    tool = get_tool(create_repo_tools(context), "repo_checkout")

    out = await tool.ainvoke({"repo": "widget", "branch": "bad..name", "create": True})

    assert "create=True" not in out
    assert "Could not create" in out


@pytest.mark.asyncio
async def test_repo_commit_commits_in_the_named_clone():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_commit")

    out = await tool.ainvoke({"repo": "widget", "message": "fix: thing"})

    git_mgr.commit.assert_called_once_with("fix: thing", allow_empty=False)
    assert "fix: thing" in out or "committed" in out.lower()


@pytest.mark.asyncio
async def test_repo_push_pushes_current_branch():
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.push.assert_called_once()


def track_remote_refs(git_mgr, refs):
    """Back rev_parse with ``refs`` and make push move origin/<branch>,
    mirroring how a real push updates the local remote-tracking ref."""
    git_mgr.rev_parse.side_effect = refs.get

    def fake_push(branch=None):
        refs[f"origin/{branch}"] = refs[branch]
        return True

    git_mgr.push.side_effect = fake_push


@pytest.mark.asyncio
async def test_repo_push_reports_noop_when_remote_is_already_up_to_date():
    """git exits 0 for 'Everything up-to-date'; the tool must not call
    that 'Pushed' — three confident no-op pushes hid a lost deliverable."""
    context, git_mgr = make_context()
    sha = "a" * 40
    git_mgr.rev_parse.side_effect = {
        "job/abc12345": sha,
        "origin/job/abc12345": sha,
    }.get
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "widget"})

    assert "NO-OP" in out
    assert sha[:12] in out
    assert "different branch" in out
    assert "Pushed" not in out


@pytest.mark.asyncio
async def test_repo_push_reports_old_and_new_sha_when_the_ref_advances():
    context, git_mgr = make_context()
    refs = {"job/abc12345": "b" * 40, "origin/job/abc12345": "a" * 40}
    track_remote_refs(git_mgr, refs)
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "widget"})

    assert f"{'a' * 12} -> {'b' * 12}" in out
    assert "NO-OP" not in out


@pytest.mark.asyncio
async def test_repo_push_reports_new_branch_when_remote_ref_did_not_exist():
    context, git_mgr = make_context()
    refs = {"job/abc12345": "b" * 40}
    track_remote_refs(git_mgr, refs)
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "widget"})

    assert f"(new branch) -> {'b' * 12}" in out
    assert "NO-OP" not in out


@pytest.mark.asyncio
async def test_repo_push_refspec_target_keeps_the_simple_message():
    """A raw refspec is a deliberate escape hatch; origin/<target> cannot
    resolve, so the tool skips verification instead of breaking the push."""
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke(
        {"repo": "widget", "branch": "+refs/heads/main:refs/heads/feature/x"}
    )

    git_mgr.rev_parse.assert_not_called()
    assert out == "Pushed +refs/heads/main:refs/heads/feature/x to widget's remote."


@pytest.mark.asyncio
async def test_repo_push_unresolvable_target_keeps_the_simple_message():
    context, git_mgr = make_context()
    git_mgr.rev_parse.return_value = None
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "widget"})

    assert out == "Pushed job/abc12345 to widget's remote."


@pytest.mark.asyncio
async def test_repo_commit_names_the_branch_it_committed_on():
    """A commit that silently lands on main must be visible in the reply."""
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_commit")

    out = await tool.ainvoke({"repo": "widget", "message": "fix: thing"})

    assert "on branch 'job/abc12345'" in out


@pytest.mark.asyncio
async def test_write_tools_refuse_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tools = create_repo_tools(context)

    for name in ("repo_checkout", "repo_commit", "repo_push", "repo_open_pr"):
        tool = get_tool(tools, name)
        kwargs = {"repo": "widget"}
        if name == "repo_checkout":
            kwargs["branch"] = "b"
        if name == "repo_commit":
            kwargs["message"] = "m"
        if name == "repo_open_pr":
            kwargs.update({"title": "T", "base": "develop"})
        out = await tool.ainvoke(kwargs)
        assert "read-only" in out.lower()

    git_mgr.checkout_branch.assert_not_called()
    git_mgr.commit.assert_not_called()
    git_mgr.push.assert_not_called()


@pytest.mark.asyncio
async def test_repo_pull_is_allowed_on_read_only_datasource():
    context, git_mgr = make_context(read_only=True)
    tool = get_tool(create_repo_tools(context), "repo_pull")

    await tool.ainvoke({"repo": "widget"})

    git_mgr.pull.assert_called_once()


@pytest.mark.asyncio
async def test_repo_pr_status_is_allowed_on_read_only_datasource():
    context, _ = make_context(read_only=True)
    tool = get_tool(create_repo_tools(context), "repo_pr_status")

    with patch(
        "src.tools.repo.repo_tools.get_pull_request_status",
        return_value={
            "number": 9,
            "url": "https://github.com/acme/widget/pull/9",
            "state": "open",
            "head": "job/abc12345",
            "base": "develop",
            "draft": False,
        },
    ) as mock_status:
        out = await tool.ainvoke({"repo": "widget", "number": 9})

    target = mock_status.call_args.args[0]
    assert target.forge == "github"
    assert target.owner == "acme"
    assert target.token == "tok"
    assert "open" in out.lower()
    assert "https://github.com/acme/widget/pull/9" in out


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
async def test_repo_open_pr_persists_structured_delivery_against_the_job():
    context, _ = make_context()
    context.job_id = "11111111-1111-1111-1111-111111111111"
    context.postgres_db = MagicMock()
    context.postgres_db.jobs.merge_context = AsyncMock(return_value=True)
    tool = get_tool(create_repo_tools(context), "repo_open_pr")

    with patch(
        "src.tools.repo.repo_tools.open_pull_request",
        return_value={"number": 9, "url": "https://github.com/acme/widget/pull/9"},
    ):
        out = await tool.ainvoke(
            {
                "repo": "widget",
                "title": "T",
                "base": "develop",
                "head": "feature/review-links",
            }
        )

    context.postgres_db.jobs.merge_context.assert_awaited_once()
    job_id, updates = context.postgres_db.jobs.merge_context.await_args.args
    assert str(job_id) == context.job_id
    assert updates == {
        "pull_request": {
            "forge": "github",
            "repo": "acme/widget",
            "number": 9,
            "url": "https://github.com/acme/widget/pull/9",
            "head": "feature/review-links",
            "base": "develop",
        }
    }
    assert "https://github.com/acme/widget/pull/9" in out


@pytest.mark.asyncio
async def test_repo_open_pr_reports_when_the_opened_pr_cannot_be_persisted():
    context, _ = make_context()
    context.job_id = "11111111-1111-1111-1111-111111111111"
    context.postgres_db = MagicMock()
    context.postgres_db.jobs.merge_context = AsyncMock(return_value=False)
    tool = get_tool(create_repo_tools(context), "repo_open_pr")

    with patch(
        "src.tools.repo.repo_tools.open_pull_request",
        return_value={"number": 9, "url": "https://github.com/acme/widget/pull/9"},
    ):
        out = await tool.ainvoke({"repo": "widget", "title": "T", "base": "develop"})

    assert "opened" in out.lower()
    assert "could not be recorded" in out.lower()


@pytest.mark.asyncio
async def test_repo_open_pr_does_not_hide_an_opened_pr_when_persistence_raises():
    context, _ = make_context()
    context.job_id = "11111111-1111-1111-1111-111111111111"
    context.postgres_db = MagicMock()
    context.postgres_db.jobs.merge_context = AsyncMock(
        side_effect=OSError("database unavailable")
    )
    tool = get_tool(create_repo_tools(context), "repo_open_pr")

    with patch(
        "src.tools.repo.repo_tools.open_pull_request",
        return_value={"number": 9, "url": "https://github.com/acme/widget/pull/9"},
    ):
        out = await tool.ainvoke({"repo": "widget", "title": "T", "base": "develop"})

    assert "https://github.com/acme/widget/pull/9" in out
    assert "do not open a duplicate" in out.lower()


@pytest.mark.asyncio
async def test_unknown_repo_name_is_a_clear_error():
    context, _ = make_context()
    tool = get_tool(create_repo_tools(context), "repo_push")

    out = await tool.ainvoke({"repo": "nope"})

    assert "nope" in out
    assert "widget" in out  # names the ones that DO exist


def make_context_without_metadata():
    """A clone that registered but whose forge metadata capture failed.

    Real and reachable: a malformed/legacy connection URL makes metadata
    resolution fail, the clone keeps working, and the repo lands in
    ``source_repos`` with no ``source_repo_meta`` entry at all.
    """
    ws = MagicMock()
    git_mgr = MagicMock()
    git_mgr.commit.return_value = True
    git_mgr.push.return_value = True
    git_mgr.pull.return_value = True
    git_mgr.current_branch.return_value = "job/abc12345"
    git_mgr.rev_parse.return_value = None
    ws.source_repos = {"widget": git_mgr}
    ws.source_repo_meta = {}
    return ToolContext(workspace_manager=ws), git_mgr


class TestMissingMetadataFailsClosed:
    """No metadata → no proof the repo is writable → refuse writes.

    Empty metadata previously read as "not read-only" and every write tool
    proceeded, which turns a metadata bug into an unguarded push.
    """

    @pytest.mark.asyncio
    async def test_write_tools_refuse_when_metadata_is_missing(self):
        context, git_mgr = make_context_without_metadata()
        tools = create_repo_tools(context)

        for name in ("repo_checkout", "repo_commit", "repo_push", "repo_open_pr"):
            tool = get_tool(tools, name)
            kwargs = {"repo": "widget"}
            if name == "repo_checkout":
                kwargs["branch"] = "b"
            if name == "repo_commit":
                kwargs["message"] = "m"
            if name == "repo_open_pr":
                kwargs.update({"title": "T", "base": "develop"})
            out = await tool.ainvoke(kwargs)
            assert "widget" in out
            # Must explain the situation, not just say "read-only".
            assert "metadata" in out.lower() or "recorded" in out.lower()

        git_mgr.checkout_branch.assert_not_called()
        git_mgr.commit.assert_not_called()
        git_mgr.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_repo_pull_still_works_without_metadata(self):
        context, git_mgr = make_context_without_metadata()
        tool = get_tool(create_repo_tools(context), "repo_pull")

        await tool.ainvoke({"repo": "widget"})

        git_mgr.pull.assert_called_once()


@pytest.mark.asyncio
async def test_repo_commit_does_not_create_empty_commits():
    """``GitManager.commit`` defaults to ``allow_empty=True``, which would
    manufacture an empty commit and report success — contradicting the
    tool's own "nothing to commit" fallback."""
    context, git_mgr = make_context()
    tool = get_tool(create_repo_tools(context), "repo_commit")

    await tool.ainvoke({"repo": "widget", "message": "fix: thing"})

    assert git_mgr.commit.call_args.kwargs.get("allow_empty") is False


@pytest.mark.asyncio
async def test_commit_failure_message_names_no_nonexistent_tool():
    """The fallback pointed at ``repo_status``, which does not exist."""
    context, git_mgr = make_context()
    git_mgr.commit.return_value = False
    tool = get_tool(create_repo_tools(context), "repo_commit")

    out = await tool.ainvoke({"repo": "widget", "message": "m"})

    assert "repo_status" not in out
    registered = {t.name for t in create_repo_tools(context)}
    for word in out.replace("(", " ").replace(")", " ").split():
        candidate = word.strip(".,;:'\"`")
        if candidate.startswith("repo_"):
            assert candidate in registered, candidate
