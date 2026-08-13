"""Server-side knowledge-note materialisation (step 3 of §7,
docs/features/knowledge_base_repo_separation.md).

``materialize_knowledge_note`` replaces the agent's workspace write of
``knowledge/<slug>.md`` with a single Gitea commit into whichever repo
``resolve_kb_repo`` picks for the project. The contract that matters to its
caller is that it NEVER raises: a repo-less project, a refused commit and a
raising client all come back as a status dict so ``kb_write`` can
log-and-continue exactly as the old non-fatal file write did.

Gitea is mocked (pattern: tests/test_loop_merge.py). ``resolve_kb_repo`` is
patched at this module's import site — it is the reindexer's function, and
these tests are about what we do with its answer, not how it finds one.
"""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import security.access as access_module
from services.kb_materialize import (
    materialize_knowledge_note,
    note_repo_path,
    slug_error,
)
from services.kb_reindex import KbRepoRef

PROJECT = "1a387b4d-0000-0000-0000-000000000000"
JOB = "abcdef12-3456-7890-abcd-ef1234567890"
REPO = "project-1a387b4d-knowledge"
SLUG = "chose-jwt-over-oauth"
PATH = "knowledge/chose-jwt-over-oauth.md"
BODY = "---\nid: chose-jwt-over-oauth\n---\n\n# Chose JWT\n"


def _blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _make_gitea(
    *,
    tree_paths: dict[str, str] | None = None,
    tree_ok: bool = True,
    tree_raises: bool = False,
    change_results: list[bool] | None = None,
    change_raises: bool = False,
) -> MagicMock:
    """Mocked gitea surface.

    ``tree_paths`` maps blob path → blob sha on the target branch (drives the
    create-vs-update choice and the unchanged short-circuit);
    ``change_results`` is the per-attempt return of ``change_files`` so the
    flip-retry can be driven.
    """
    tree_paths = tree_paths if tree_paths is not None else {}
    results = list(change_results if change_results is not None else [True])

    g = MagicMock()

    async def _list_tree(repo: str, ref: str):
        if tree_raises:
            raise RuntimeError("gitea down")
        if not tree_ok:
            return None
        return [
            {"path": p, "type": "blob", "sha": sha} for p, sha in tree_paths.items()
        ]

    async def _change_files(repo, branch, files, message):
        if change_raises:
            raise RuntimeError("gitea down")
        return results.pop(0) if results else False

    g.list_tree = AsyncMock(side_effect=_list_tree)
    g.change_files = AsyncMock(side_effect=_change_files)
    return g


def _patch_resolve(value):
    return patch(
        "services.kb_materialize.resolve_kb_repo",
        AsyncMock(return_value=value),
    )


def _ref(branch: str | None = "main") -> KbRepoRef:
    return KbRepoRef(
        forge="gitea",
        repo_url="",
        owner="",
        repo=REPO,
        branch=branch,  # type: ignore[arg-type] - exercises runtime fallback
    )


async def _run(gitea, *, slug=SLUG, content=BODY, job_id=JOB, resolved=None):
    if resolved is None:
        resolved = _ref()
    db = AsyncMock()
    with _patch_resolve(resolved) as resolve:
        result = await materialize_knowledge_note(
            postgres_db=db,
            gitea_client=gitea,
            project_id=PROJECT,
            slug=slug,
            content=content,
            job_id=job_id,
        )
    return result, resolve, db


# =============================================================================
# Path + slug vocabulary
# =============================================================================


class TestNotePath:
    def test_path_uses_the_reindexers_vault_prefix(self):
        """One prefix, one source: the writer and the sweep must agree."""
        from services.kb_reindex import KNOWLEDGE_PREFIX

        assert note_repo_path(SLUG) == f"{KNOWLEDGE_PREFIX}{SLUG}.md"
        assert note_repo_path(SLUG) == PATH

    @pytest.mark.parametrize(
        "slug",
        ["", "   ", "../../etc/passwd", "nested/note", "back\\slash", ".hidden", "-x"],
    )
    def test_unsafe_slugs_are_refused(self, slug):
        assert slug_error(slug) is not None

    @pytest.mark.parametrize("slug", ["index", "LOG"])
    def test_reserved_okf_basenames_are_refused(self, slug):
        """``index.md``/``log.md`` are generated nav the reindexer skips — a
        note written there would be permanently invisible."""
        assert slug_error(slug) is not None

    @pytest.mark.parametrize(
        "slug", [SLUG, "kb_reindex_watermark_never_advances", "note.v2", "a"]
    )
    def test_real_world_slugs_pass(self, slug):
        assert slug_error(slug) is None


# =============================================================================
# The write path
# =============================================================================


class TestMaterializeCreate:
    @pytest.mark.asyncio
    async def test_absent_path_commits_as_create(self):
        g = _make_gitea(tree_paths={"knowledge/other.md": "beef" * 10})

        result, resolve, db = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "create"
        assert result["repo"] == REPO
        assert result["branch"] == "main"
        assert result["path"] == PATH
        assert result["reason"] is None
        # Resolution goes through the reindexer's helper, not a reimplementation.
        resolve.assert_awaited_once_with(db, PROJECT)
        g.list_tree.assert_awaited_once_with(REPO, "main")

    @pytest.mark.asyncio
    async def test_commit_payload_is_base64_and_single_file(self):
        g = _make_gitea()

        await _run(g)

        args = g.change_files.await_args
        assert args.args[0] == REPO
        assert args.args[1] == "main"
        files = args.args[2]
        assert len(files) == 1
        assert files[0]["path"] == PATH
        assert base64.b64decode(files[0]["content_b64"]).decode() == BODY
        assert files[0]["operation"] == "create"

    @pytest.mark.asyncio
    async def test_commit_message_attributes_the_writing_job(self):
        g = _make_gitea()

        await _run(g)

        message = g.change_files.await_args.kwargs["message"]
        assert message.startswith(f"kb: {SLUG} (job {JOB[:8]})")
        # Full UUID in the body keeps attribution machine-recoverable.
        assert f"job: {JOB}" in message

    @pytest.mark.asyncio
    async def test_commit_message_without_a_job(self):
        """Persistent sessions have no job — the note still materialises."""
        g = _make_gitea()

        result, _, _ = await _run(g, job_id=None)

        assert result["status"] == "committed"
        assert g.change_files.await_args.kwargs["message"] == f"kb: {SLUG}"

    @pytest.mark.asyncio
    async def test_unreadable_tree_still_attempts_a_create(self):
        """An empty/unreadable tree is a guess, not a stop — the flip-retry
        below is what covers a wrong guess."""
        g = _make_gitea(tree_ok=False)

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "create"

    @pytest.mark.asyncio
    async def test_tree_probe_raising_does_not_fail_the_write(self):
        g = _make_gitea(tree_raises=True)

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "create"


class TestMaterializeUpdate:
    @pytest.mark.asyncio
    async def test_existing_path_commits_as_update(self):
        """``create`` on an existing path is a Gitea 422 — the operation is
        chosen per file against the target tree (the curated-merge rule)."""
        g = _make_gitea(tree_paths={PATH: "stale" + "0" * 35})

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "update"
        file = g.change_files.await_args.args[2][0]
        assert file["operation"] == "update"
        assert file["sha"] == "stale" + "0" * 35

    @pytest.mark.asyncio
    async def test_github_descriptor_selects_external_client_not_gitea(self):
        gitea = _make_gitea()
        github = _make_gitea(tree_paths={PATH: "stale" + "0" * 35})
        ref = KbRepoRef(
            forge="github",
            repo_url="https://github.com/acme/design-vault.git",
            owner="acme",
            repo="design-vault",
            branch="main",
            credential_ref="55555555-6666-7777-8888-999999999999",
        )
        db = AsyncMock()
        with (
            _patch_resolve(ref),
            patch(
                "services.kb_materialize.kb_client_for_repo",
                AsyncMock(return_value=github),
            ) as select,
        ):
            result = await materialize_knowledge_note(
                postgres_db=db,
                gitea_client=gitea,
                project_id=PROJECT,
                slug=SLUG,
                content=BODY,
                job_id=JOB,
            )

        assert result["status"] == "committed"
        select.assert_awaited_once_with(db, gitea, ref)
        gitea.list_tree.assert_not_awaited()
        gitea.change_files.assert_not_awaited()
        github.change_files.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_identical_bytes_are_not_recommitted(self):
        """A no-op commit would pollute the very per-job history §3 wants
        readable — and the tree already tells us the blob sha."""
        g = _make_gitea(tree_paths={PATH: _blob_sha(BODY)})

        result, _, _ = await _run(g)

        assert result["status"] == "skipped"
        assert result["reason"] == "unchanged"
        assert result["repo"] == REPO
        assert result["path"] == PATH
        g.change_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_bytes_at_the_same_path_do_commit(self):
        g = _make_gitea(tree_paths={PATH: _blob_sha("something else entirely")})

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "update"


class TestOperationFlipRetry:
    @pytest.mark.asyncio
    async def test_refused_create_is_retried_as_update(self):
        """Concurrent writer (or an unreadable tree) landed the path between
        the probe and the commit: the 422 is recovered, not lost."""
        g = _make_gitea(change_results=[False, True])

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "update"
        assert g.change_files.await_count == 2
        ops = [c.args[2][0]["operation"] for c in g.change_files.await_args_list]
        assert ops == ["create", "update"]

    @pytest.mark.asyncio
    async def test_refused_update_is_retried_as_create(self):
        g = _make_gitea(
            tree_paths={PATH: "stale" + "0" * 35}, change_results=[False, True]
        )

        result, _, _ = await _run(g)

        assert result["status"] == "committed"
        assert result["operation"] == "create"
        ops = [c.args[2][0]["operation"] for c in g.change_files.await_args_list]
        assert ops == ["update", "create"]


# =============================================================================
# Skips and failures — none of them raise
# =============================================================================


class TestSkips:
    @pytest.mark.asyncio
    async def test_no_repo_skips_cleanly(self):
        """A repo-less project: the equivalent of the old ``has_git()`` skip."""
        g = _make_gitea()

        db = AsyncMock()
        with _patch_resolve(None):
            result = await materialize_knowledge_note(
                postgres_db=db,
                gitea_client=g,
                project_id=PROJECT,
                slug=SLUG,
                content=BODY,
                job_id=JOB,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "no-repo"
        assert result["path"] == PATH
        assert result["repo"] is None
        g.list_tree.assert_not_awaited()
        g.change_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_branch_falls_back_to_main(self):
        g = _make_gitea()

        result, _, _ = await _run(g, resolved=_ref(None))

        assert result["branch"] == "main"
        g.list_tree.assert_awaited_once_with(REPO, "main")


class TestFailures:
    @pytest.mark.asyncio
    async def test_both_operations_refused_reports_commit_refused(self):
        g = _make_gitea(change_results=[False, False])

        result, _, _ = await _run(g)

        assert result["status"] == "failed"
        assert result["reason"] == "commit-refused"
        assert result["repo"] == REPO
        assert result["path"] == PATH
        assert g.change_files.await_count == 2

    @pytest.mark.asyncio
    async def test_raising_gitea_reports_commit_error(self):
        g = _make_gitea(change_raises=True)

        result, _, _ = await _run(g)

        assert result["status"] == "failed"
        assert result["reason"] == "commit-error"

    @pytest.mark.asyncio
    async def test_resolution_failure_is_not_fatal(self):
        g = _make_gitea()
        db = AsyncMock()
        with patch(
            "services.kb_materialize.resolve_kb_repo",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            result = await materialize_knowledge_note(
                postgres_db=db,
                gitea_client=g,
                project_id=PROJECT,
                slug=SLUG,
                content=BODY,
                job_id=JOB,
            )

        assert result["status"] == "failed"
        assert result["reason"] == "resolve-error"
        g.change_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsafe_slug_never_reaches_gitea(self):
        g = _make_gitea()

        result, resolve, _ = await _run(g, slug="../../../etc/passwd")

        assert result["status"] == "failed"
        assert result["reason"] == "invalid-slug"
        resolve.assert_not_awaited()
        g.change_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_body_is_refused(self):
        """An empty note file parses as a frontmatter-less note and would be
        indexed as junk."""
        g = _make_gitea()

        result, _, _ = await _run(g, content="   \n")

        assert result["status"] == "failed"
        assert result["reason"] == "empty-content"
        g.change_files.assert_not_awaited()


# =============================================================================
# Endpoint — POST /api/projects/{project_id}/knowledge/materialize
# =============================================================================


class TestMaterializeEndpoint:
    @pytest.mark.asyncio
    async def test_rejects_calls_without_the_internal_key(self, fake_request):
        """Agent-internal (P4b): same gate as the job /complete callback."""
        from main import KnowledgeMaterializeRequest, materialize_knowledge_note

        fake_request.headers = {}
        body = KnowledgeMaterializeRequest(slug=SLUG, content=BODY, job_id=JOB)
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await materialize_knowledge_note(fake_request, PROJECT, body)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_internal_call_commits_and_returns_the_service_result(
        self, fake_request
    ):
        from main import KnowledgeMaterializeRequest, materialize_knowledge_note

        g = _make_gitea()
        fake_request.headers = {"X-Internal-Key": "secret"}
        body = KnowledgeMaterializeRequest(slug=SLUG, content=BODY, job_id=JOB)
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main.postgres_db", AsyncMock()),
            patch("main.gitea_client", g),
            _patch_resolve(_ref()),
        ):
            result = await materialize_knowledge_note(fake_request, PROJECT, body)

        assert result["status"] == "committed"
        assert result["path"] == PATH
        assert g.change_files.await_count == 1

    @pytest.mark.asyncio
    async def test_failure_is_a_200_body_not_an_http_error(self, fake_request):
        """The caller must be able to log-and-continue; a raise-for-status
        client would otherwise turn a KB hiccup into a tool failure."""
        from main import KnowledgeMaterializeRequest, materialize_knowledge_note

        g = _make_gitea(change_raises=True)
        fake_request.headers = {"X-Internal-Key": "secret"}
        body = KnowledgeMaterializeRequest(slug=SLUG, content=BODY, job_id=JOB)
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main.postgres_db", AsyncMock()),
            patch("main.gitea_client", g),
            _patch_resolve(_ref()),
        ):
            result = await materialize_knowledge_note(fake_request, PROJECT, body)

        assert result["status"] == "failed"
        assert result["reason"] == "commit-error"
