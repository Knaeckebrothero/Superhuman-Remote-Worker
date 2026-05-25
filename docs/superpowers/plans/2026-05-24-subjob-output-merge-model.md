# Subjob Output Model (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the destructive whole-branch squash-merge of subjobs with a safe, uniform **extract-and-graft**: each subjob's `output/` is copied onto the parent as `outputs/<NNN>-<config>-<short_id>/` in a single commit, so a subjob can never modify or delete parent content and two subjobs can never collide.

**Architecture:** Orchestrator-side, on subjob completion, read the subjob branch's `output/` tree and write it under a unique namespaced path on the parent branch using a new Gitea batch-commit call (`change_files`). No PR, no squash, no pre-merge cleanup, no branch merge. Critic grafts nothing (verdict stays in the DB). The agent-side merge tooling (`git_merge_squash`/`git_worktree_cleanup`) and the old `_squash_merge_subjob`/`SUBJOB_CLEANUP_*` machinery are removed; the parent agent *reads* `outputs/*` instead of running git merges.

**Tech Stack:** Python 3.12, FastAPI orchestrator (`orchestrator/main.py`), Gitea HTTP API client (`orchestrator/services/gitea.py`), Postgres (`orchestrator/database/postgres.py`), pytest + pytest-asyncio (strict mode → every async test needs `@pytest.mark.asyncio`).

**Spec:** `docs/superpowers/specs/2026-05-24-subjob-output-merge-model-design.md`. **Issue/context:** `docs/issues/subjob_branch_merge_model.md`.

**Run tests with:** `.venv/bin/python -m pytest <path> -q --tb=short` (the local `.venv` is the only working interpreter here; the full suite has pre-existing SFTP/cloud_sync collection errors unrelated to this work — use `--continue-on-collection-errors` for full runs).

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `orchestrator/services/gitea.py` | Gitea HTTP client | **Modify** — add `change_files` (batch, bytes-faithful, one commit) |
| `orchestrator/main.py` | Orchestrator: graft + triggers + handlers | **Modify** — add `_graft_subjob_output`, `_next_output_ordinal`; rewire 3 trigger sites + scholar/delegation handlers; remove `_squash_merge_subjob`, `SUBJOB_CLEANUP_*` |
| `src/agent.py` | Parent resume injection | **Modify** — `_format_delegation_results` points at `outputs/*`, drops branch-merge instructions |
| `src/tools/git/git_tools.py` | Agent git tools | **Modify** — remove `git_merge_squash`, `git_worktree_cleanup` |
| `tests/test_per_job_repo.py` | Graft + ordinal tests | **Modify** — add graft tests + fake; remove old squash/cleanup tests |
| `tests/test_tools_git.py`, `tests/test_delegation.py` | Tool/delegation tests | **Modify** — drop references to the removed tools |

---

## Task 1: Gitea `change_files` batch-commit (bytes-faithful)

**Files:**
- Modify: `orchestrator/services/gitea.py` (add method after `create_or_update_file`, ~line 519)
- Test: `tests/test_gitea_change_files.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitea_change_files.py`:

```python
"""Unit test for GiteaClient.change_files (batch multi-file single commit)."""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_orch_dir = str(Path(__file__).parent.parent / "orchestrator")
if _orch_dir not in sys.path:
    sys.path.insert(0, _orch_dir)
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from services import gitea as gitea_mod  # noqa: E402


@pytest.mark.asyncio
async def test_change_files_posts_batch_create_payload():
    gc = gitea_mod.GiteaClient.__new__(gitea_mod.GiteaClient)  # bypass __init__
    gc._initialized = True
    gc._url = "http://gitea"
    gc._user = "srw"

    resp = MagicMock()
    resp.status_code = 201
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    gc._get_client = MagicMock(return_value=client)

    ok = await gc.change_files(
        "job-parent12",
        "main",
        [
            {"path": "outputs/001-scholar-abcd1234/a.md", "content_b64": "YQ=="},
            {"path": "outputs/001-scholar-abcd1234/b.bin", "content_b64": "Yg=="},
        ],
        message="Graft outputs/001-scholar-abcd1234",
    )

    assert ok is True
    client.post.assert_awaited_once()
    url = client.post.await_args.args[0]
    body = client.post.await_args.kwargs["json"]
    assert url == "http://gitea/api/v1/repos/srw/job-parent12/contents"
    assert body["branch"] == "main"
    assert body["message"] == "Graft outputs/001-scholar-abcd1234"
    assert body["files"] == [
        {"operation": "create", "path": "outputs/001-scholar-abcd1234/a.md", "content": "YQ=="},
        {"operation": "create", "path": "outputs/001-scholar-abcd1234/b.bin", "content": "Yg=="},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_gitea_change_files.py -q --tb=short`
Expected: FAIL with `AttributeError: ... has no attribute 'change_files'`.

- [ ] **Step 3: Add the method**

In `orchestrator/services/gitea.py`, immediately after `create_or_update_file` ends (before `delete_file`, ~line 519), add:

```python
    async def change_files(
        self,
        repo_name: str,
        branch: str,
        files: list[dict],
        message: str,
    ) -> bool:
        """Create multiple files in a SINGLE commit via Gitea's ChangeFiles API.

        Args:
            repo_name: Repository name.
            branch: Target branch.
            files: list of ``{"path": str, "content_b64": str}`` — each created
                (base64 content keeps binary files byte-faithful).
            message: Commit message.

        Returns:
            True on success, False otherwise.
        """
        if not self._initialized:
            return False
        if not files:
            return True

        client = self._get_client()
        payload = {
            "branch": branch,
            "message": message,
            "files": [
                {"operation": "create", "path": f["path"], "content": f["content_b64"]}
                for f in files
            ],
        }
        try:
            resp = await client.post(
                f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents",
                json=payload,
            )
            if resp.status_code in (200, 201):
                return True
            logger.warning(
                f"change_files failed for {repo_name}@{branch} "
                f"(status {resp.status_code}): {resp.text[:200]}"
            )
            return False
        except Exception as e:
            logger.warning(f"change_files failed for {repo_name}@{branch}: {e}")
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_gitea_change_files.py -q --tb=short`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/gitea.py tests/test_gitea_change_files.py
git commit -m "feat(gitea): add change_files batch single-commit write"
```

---

## Task 2: `_next_output_ordinal` helper

**Files:**
- Modify: `orchestrator/main.py` (add near the old `SUBJOB_CLEANUP_*` constants, ~line 318; ensure `import re` exists at top)
- Test: `tests/test_per_job_repo.py` (append a new test class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_job_repo.py`:

```python
# ===========================================================================
# _next_output_ordinal
# ===========================================================================


class _OutputsFake:
    """Minimal gitea fake exposing list_contents for an `outputs/` dir."""

    def __init__(self, outputs_dirs: list[str]):
        # outputs_dirs: directory names directly under outputs/, e.g. ["001-scholar-aa"]
        self._dirs = outputs_dirs
        self.is_initialized = True

    async def list_contents(self, repo, path="", ref=None):
        if path != "outputs":
            return []
        return [{"name": d, "path": f"outputs/{d}", "type": "dir"} for d in self._dirs]


class TestNextOutputOrdinal:
    @pytest.mark.asyncio
    async def test_first_ordinal_is_001(self):
        fake = _OutputsFake([])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "001"

    @pytest.mark.asyncio
    async def test_increments_past_highest(self):
        fake = _OutputsFake(["001-scholar-aa", "002-critic-bb", "010-developer-cc"])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "011"

    @pytest.mark.asyncio
    async def test_ignores_non_numbered_entries(self):
        fake = _OutputsFake(["notes", "003-scholar-dd"])
        with patch(f"{MODULE}.gitea_client", fake):
            assert await orch_main._next_output_ordinal("job-x", "main") == "004"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestNextOutputOrdinal -q --tb=short`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_next_output_ordinal'`.

- [ ] **Step 3: Implement the helper**

In `orchestrator/main.py`: confirm `import re` is present at the top of the file (it is used elsewhere; if not, add it). Then add, just above the (soon-to-be-removed) `SUBJOB_CLEANUP_FILES` block (~line 318):

```python
async def _next_output_ordinal(repo_name: str, base_branch: str) -> str:
    """Return the next zero-padded ordinal for `outputs/<n>-...` on base_branch.

    Per-repo, recency-ordered. Sequential (no async subjobs), so max+1 is race-free.
    """
    entries = await gitea_client.list_contents(repo_name, "outputs", ref=base_branch) or []
    nums = []
    for entry in entries:
        if entry.get("type") == "dir":
            m = re.match(r"(\d+)-", entry.get("name", ""))
            if m:
                nums.append(int(m.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    return f"{nxt:03d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestNextOutputOrdinal -q --tb=short`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "feat(orchestrator): add _next_output_ordinal for namespaced outputs"
```

---

## Task 3: `_graft_subjob_output` core

**Files:**
- Modify: `orchestrator/main.py` (add new function; keep `_squash_merge_subjob` for now — removed in Task 9)
- Test: `tests/test_per_job_repo.py` (append a new test class with a graft-aware fake)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_per_job_repo.py`:

```python
import base64 as _b64

# ===========================================================================
# _graft_subjob_output
# ===========================================================================


class _GraftFakeGitea:
    """Models per-branch trees as {branch: {path: bytes}} and the graft I/O.

    - list_tree(ref) -> [{path, type:"blob"}] for that branch
    - get_file_bytes(path, ref) -> bytes
    - list_contents("outputs", ref) -> dir entries under outputs/ on that branch
    - change_files(branch, files) -> add files (base64) to that branch's tree
    """

    def __init__(self, trees: dict[str, dict[str, bytes]]):
        self.trees = {b: dict(t) for b, t in trees.items()}
        self.is_initialized = True

    async def list_tree(self, repo, ref):
        return [{"path": p, "type": "blob"} for p in self.trees.get(ref, {})]

    async def get_file_bytes(self, repo, file_path, ref=None):
        return self.trees.get(ref, {}).get(file_path)

    async def list_contents(self, repo, path="", ref=None):
        if path != "outputs":
            return []
        names = set()
        for p in self.trees.get(ref, {}):
            if p.startswith("outputs/"):
                names.add(p.split("/")[1])
        return [{"name": n, "path": f"outputs/{n}", "type": "dir"} for n in names]

    async def change_files(self, repo, branch, files, message):
        tree = self.trees.setdefault(branch, {})
        for f in files:
            tree[f["path"]] = _b64.b64decode(f["content_b64"])
        return True


def _subjob(**over):
    base = {
        "id": "sub-uuid-1234abcd",
        "parent_job_id": "parent-uuid",
        "branch_name": "subjob/1234abcd/scholar",
        "repo_name": "job-parent12",
        "config_name": "scholar",
        "description": "research",
        "context": {},
    }
    base.update(over)
    return base


class TestGraftSubjobOutput:
    @pytest.mark.asyncio
    async def test_grafts_output_to_namespaced_dir_and_leaves_parent_untouched(self):
        fake = _GraftFakeGitea(
            {
                "main": {"documents/corpus.pdf": b"PARENT", "src/app.py": b"code"},
                "subjob/1234abcd/scholar": {
                    "documents/corpus.pdf": b"PARENT",   # inherited from fork
                    "src/app.py": b"code",
                    "output/ideas/idea.md": b"# idea",
                    "output/report.pdf": b"\x89PDFbytes",
                    "workspace.md": b"scratch",          # NOT under output/
                },
            }
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result["status"] == "grafted"
        assert result["output_path"] == "outputs/001-scholar-sub-uuid"
        # output/ contents relocated under the namespaced dir, prefix stripped:
        assert fake.trees["main"]["outputs/001-scholar-sub-uuid/ideas/idea.md"] == b"# idea"
        assert fake.trees["main"]["outputs/001-scholar-sub-uuid/report.pdf"] == b"\x89PDFbytes"
        # parent content untouched; scratch + inherited tree NOT propagated:
        assert fake.trees["main"]["documents/corpus.pdf"] == b"PARENT"
        assert "outputs/001-scholar-sub-uuid/workspace.md" not in fake.trees["main"]
        db.update_job_merge_status.assert_awaited_with("sub-uuid-1234abcd", merge_status="grafted")

    @pytest.mark.asyncio
    async def test_critic_grafts_nothing(self):
        fake = _GraftFakeGitea(
            {
                "main": {"src/app.py": b"code"},
                "subjob/1234abcd/critic": {"output/reviews/r.md": b"review"},
            }
        )
        critic = _subjob(
            config_name="critic",
            branch_name="subjob/1234abcd/critic",
            context={"verification_target": "parent-uuid"},
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": critic, "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result == {"status": "skipped", "reason": "critic-not-merged"}
        assert all(not k.startswith("outputs/") for k in fake.trees["main"])

    @pytest.mark.asyncio
    async def test_no_output_skipped(self):
        fake = _GraftFakeGitea(
            {"main": {}, "subjob/1234abcd/scholar": {"workspace.md": b"scratch"}}
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result == {"status": "skipped", "reason": "no-output"}

    @pytest.mark.asyncio
    async def test_ordinal_increments_when_outputs_exist(self):
        fake = _GraftFakeGitea(
            {
                "main": {"outputs/001-scholar-old/x.md": b"old"},
                "subjob/1234abcd/scholar": {"output/y.md": b"new"},
            }
        )
        with (
            patch(f"{MODULE}.postgres_db") as db,
            patch(f"{MODULE}.gitea_client", fake),
        ):
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sub-uuid-1234abcd": _subjob(), "parent-uuid": {"branch_name": None}}.get(j)
            )
            db.update_job_merge_status = AsyncMock()
            db.update_job_context = AsyncMock()

            result = await orch_main._graft_subjob_output("sub-uuid-1234abcd")

        assert result["output_path"] == "outputs/002-scholar-sub-uuid"
        assert fake.trees["main"]["outputs/002-scholar-sub-uuid/y.md"] == b"new"
```

> Note: `short_id = str(job_id)[:8]` and the job id is `"sub-uuid-1234abcd"`, so `<short_id>` = `"sub-uuid"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestGraftSubjobOutput -q --tb=short`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_graft_subjob_output'`.

- [ ] **Step 3: Implement `_graft_subjob_output`**

In `orchestrator/main.py`, add (next to `_next_output_ordinal`):

```python
async def _graft_subjob_output(job_id: str) -> dict[str, Any] | None:
    """Graft a completed subjob's ``output/`` onto its parent's branch.

    Copies the subjob branch's ``output/`` subtree to
    ``outputs/<n>-<config>-<short_id>/`` on the parent branch in a single
    commit. Purely additive — never modifies/deletes parent content, so
    collisions and clobbering are impossible. Critic subjobs graft nothing
    (verdict is consumed from the DB). Replaces ``_squash_merge_subjob``.
    See docs/superpowers/specs/2026-05-24-subjob-output-merge-model-design.md.
    """
    import base64

    job = await postgres_db.get_job(job_id)
    if not job or not job.get("parent_job_id"):
        return None
    if not job.get("branch_name") or not job.get("repo_name"):
        logger.debug(f"Subjob {job_id} has no branch/repo — skipping graft")
        return None
    if not gitea_client.is_initialized:
        logger.warning(f"Gitea not initialized — cannot graft subjob {job_id}")
        return None

    # Critic contributes nothing to the branch (verdict lives in the DB).
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    if isinstance(ctx, dict) and ctx.get("verification_target"):
        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "critic-not-merged"}

    repo_name = job["repo_name"]
    subjob_branch = job["branch_name"]
    short_id = str(job_id)[:8]
    config_name = job.get("config_name") or "subjob"

    parent = await postgres_db.get_job(str(job["parent_job_id"]))
    base_branch = (parent.get("branch_name") if parent else None) or "main"

    tree = await gitea_client.list_tree(repo_name, ref=subjob_branch) or []
    output_blobs = [
        e["path"]
        for e in tree
        if e.get("type") == "blob" and e["path"].startswith("output/")
    ]
    if not output_blobs:
        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "no-output"}

    ordinal = await _next_output_ordinal(repo_name, base_branch)
    dest = f"outputs/{ordinal}-{config_name}-{short_id}"

    files: list[dict] = []
    for path in output_blobs:
        data = await gitea_client.get_file_bytes(repo_name, path, ref=subjob_branch)
        if data is None:
            logger.warning(f"Graft {job_id}: failed to read {path}; aborting graft")
            await postgres_db.update_job_merge_status(job_id, merge_status="graft-failed")
            return {"status": "error", "reason": "read-failed", "path": path}
        rel = path[len("output/"):]
        files.append(
            {"path": f"{dest}/{rel}", "content_b64": base64.b64encode(data).decode("ascii")}
        )

    ok = await gitea_client.change_files(
        repo_name, base_branch, files, message=f"Graft {dest} from subjob {short_id}"
    )
    if not ok:
        await postgres_db.update_job_merge_status(job_id, merge_status="graft-failed")
        return {"status": "error", "reason": "write-failed"}

    await postgres_db.update_job_merge_status(job_id, merge_status="grafted")
    new_ctx = dict(ctx)
    new_ctx["graft_output_path"] = dest
    await postgres_db.update_job_context(job_id, new_ctx)

    logger.info(
        f"Grafted subjob {short_id}/{config_name} output ({len(files)} files) "
        f"to {base_branch}:{dest}"
    )
    return {
        "status": "grafted",
        "base_branch": base_branch,
        "output_path": dest,
        "ordinal": ordinal,
        "files": len(files),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestGraftSubjobOutput -q --tb=short`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "feat(orchestrator): add _graft_subjob_output (extract-and-graft, no clobber)"
```

---

## Task 4: Wire the graft into job completion (incl. delegation children)

**Files:**
- Modify: `orchestrator/main.py:7548-7553` (the completion-handler subjob-merge block)
- Test: `tests/test_per_job_repo.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_job_repo.py`:

```python
class TestCompletionGraftWiring:
    @pytest.mark.asyncio
    async def test_graft_fires_for_delegation_child(self):
        # A delegation child has creation_order set; the old gate skipped it.
        called = {}

        async def fake_graft(job_id):
            called["job_id"] = job_id
            return {"status": "grafted", "output_path": "outputs/001-developer-deadbeef"}

        child = {
            "id": "deadbeef-child", "parent_job_id": "p", "creation_order": 0,
            "branch_name": "subjob/deadbeef/developer", "repo_name": "job-p",
            "config_name": "developer",
        }
        with patch(f"{MODULE}._graft_subjob_output", side_effect=fake_graft):
            res = await orch_main._maybe_graft_completed_subjob(child)
        assert called["job_id"] == "deadbeef-child"
        assert res["status"] == "grafted"

    @pytest.mark.asyncio
    async def test_no_graft_for_root_job(self):
        with patch(f"{MODULE}._graft_subjob_output", new_callable=AsyncMock) as g:
            res = await orch_main._maybe_graft_completed_subjob({"id": "r", "parent_job_id": None})
        assert res is None
        g.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestCompletionGraftWiring -q --tb=short`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_maybe_graft_completed_subjob'`.

- [ ] **Step 3: Add a small wrapper and rewire the completion block**

In `orchestrator/main.py`, add this helper next to `_graft_subjob_output`:

```python
async def _maybe_graft_completed_subjob(job: dict[str, Any]) -> dict[str, Any] | None:
    """Graft any completed subjob's output onto its parent. Applies uniformly
    to scholar, delegation children, and any other subjob; critic is skipped
    inside _graft_subjob_output. Root jobs (no parent) are ignored."""
    if not job.get("parent_job_id"):
        return None
    return await _graft_subjob_output(str(job["id"]))
```

Then replace the completion-handler block at `orchestrator/main.py:7548-7553`:

```python
        # 2. Subjob merge (if this is a subjob with a branch)
        # Skip auto-merge for delegation children — parent reviews and merges
        if job.get("parent_job_id") and job.get("creation_order") is None:
            merge_result = await _squash_merge_subjob(job_id)
            if merge_result:
                actions.append("subjob branch merged")
```

with (the gate now covers delegation children too — they are grafted, not branch-merged):

```python
        # 2. Subjob output graft (uniform for all subjob types; critic skipped inside)
        if job.get("parent_job_id"):
            graft_result = await _maybe_graft_completed_subjob(job)
            if graft_result and graft_result.get("status") == "grafted":
                actions.append(f"subjob output grafted to {graft_result['output_path']}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestCompletionGraftWiring -q --tb=short`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "feat(orchestrator): graft subjob output on completion (uniform, incl delegation)"
```

---

## Task 5: Scholar points the parent at its grafted output

**Files:**
- Modify: `orchestrator/main.py` — `_handle_scholar_completion` (~6603-6674), the `scholar_output_dir` assignment (~6668)
- Test: `tests/test_per_job_repo.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestScholarOutputPointer:
    @pytest.mark.asyncio
    async def test_scholar_completion_sets_parent_output_dir_to_graft_path(self):
        scholar = {
            "id": "sch-1", "parent_job_id": "par-1", "status": "completed",
            "context": {"scholar_target": "par-1", "graft_output_path": "outputs/003-scholar-sch1"},
        }
        parent = {"id": "par-1", "status": "waiting", "context": {}}
        captured = {}

        async def upd_ctx(jid, ctx):
            captured[jid] = ctx

        with patch(f"{MODULE}.postgres_db") as db:
            db.get_job = AsyncMock(
                side_effect=lambda j: {"sch-1": scholar, "par-1": parent}.get(j)
            )
            db.update_job_context = AsyncMock(side_effect=upd_ctx)
            db.update_job_status = AsyncMock()
            with patch(f"{MODULE}._trigger_dispatch"):
                await orch_main._handle_scholar_completion(scholar, [])

        assert captured["par-1"]["scholar_output_dir"] == "outputs/003-scholar-sch1"
        assert captured["par-1"]["scholar_completed"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestScholarOutputPointer -q --tb=short`
Expected: FAIL — `scholar_output_dir` equals `"research"`, not the graft path.

- [ ] **Step 3: Update the scholar handler**

In `orchestrator/main.py`, in `_handle_scholar_completion`, the success branch currently reads:

```python
    else:
        parent_ctx["scholar_completed"] = True
        parent_ctx["scholar_output_dir"] = "research"
```

Replace the `scholar_output_dir` line so it uses the grafted path (the graft already ran earlier in `complete_job` and stored `graft_output_path` on the scholar's context):

```python
    else:
        parent_ctx["scholar_completed"] = True
        scholar_ctx = job.get("context") or {}
        if isinstance(scholar_ctx, str):
            try:
                scholar_ctx = json.loads(scholar_ctx)
            except (json.JSONDecodeError, ValueError):
                scholar_ctx = {}
        parent_ctx["scholar_output_dir"] = (scholar_ctx or {}).get("graft_output_path")
```

> The scholar `job` dict passed into the handler may predate the graft's context write; if `graft_output_path` is absent (e.g. empty output), `scholar_output_dir` becomes `None`, which downstream treats as "no research dir". If a fresh value is required, re-fetch with `await postgres_db.get_job(str(job["id"]))` — but in `complete_job` the graft runs in the same request before this handler, and tests assert on the in-context value.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestScholarOutputPointer -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "feat(orchestrator): point parent at scholar's grafted outputs dir"
```

---

## Task 6: Delegation results reference `outputs/*`; resume injection rewritten

**Files:**
- Modify: `orchestrator/main.py` — `_handle_delegation_child_completion` child_results (~6723-6751) and the equivalent block in `_check_delegation_timeouts` (~6861-6897): add `"output_path"`
- Modify: `src/agent.py` — `_format_delegation_results` (80-116)
- Test: `tests/test_agent_delegation_format.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_delegation_format.py`:

```python
"""Delegation resume message must reference grafted outputs, not branch merges."""
import os
import sys
from pathlib import Path

_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from agent import _format_delegation_results  # noqa: E402


def test_format_references_output_paths_not_branches():
    msg = _format_delegation_results(
        [
            {
                "creation_order": 0, "status": "completed", "job_id": "c0",
                "config_name": "scholar", "summary": "found X",
                "output_path": "outputs/001-scholar-c0aaaaaa",
            },
        ]
    )
    assert "outputs/001-scholar-c0aaaaaa" in msg
    assert "scholar" in msg                 # config_name rendered (was the 'config' bug)
    assert "git diff" not in msg.lower()    # no branch-merge instructions
    assert "squash-merg" not in msg.lower()
    assert "git_merge_squash" not in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent_delegation_format.py -q --tb=short`
Expected: FAIL — current message contains `git diff`/`squash-merg` and renders `config` (not `config_name`).

- [ ] **Step 3a: Rewrite `_format_delegation_results`**

Replace `src/agent.py:80-116` entirely with:

```python
def _format_delegation_results(delegation_results: list) -> str:
    """Format delegation child results as a human-readable message.

    Injected into the parent's graph state as a HumanMessage on delegation
    resume. Each child's deliverables live at its grafted ``outputs/<n>-...``
    folder on this branch — the parent READS them (and integrates anything
    that must become real code itself); there are no branches to merge.
    """
    lines = [
        "## Delegation Results",
        "",
        f"All {len(delegation_results)} subagent(s) have completed. "
        "Each one's deliverables have been added to this branch under its "
        "`outputs/` folder. Review them below, then integrate what you need.",
        "",
    ]
    for child in delegation_results:
        status = child.get("status", "unknown")
        lines.append(f"### Child {child.get('creation_order', '?')}: {status}")
        lines.append(f"- **Job ID**: {child.get('job_id', 'unknown')}")
        lines.append(f"- **Config**: {child.get('config_name', 'unknown')}")
        lines.append(f"- **Status**: {status}")
        if child.get("confidence") is not None:
            lines.append(f"- **Confidence**: {child['confidence']}")
        if child.get("output_path"):
            lines.append(f"- **Output**: `{child['output_path']}/`")
        if child.get("summary"):
            lines.append(f"- **Summary**: {child['summary']}")
        lines.append("")

    lines.append(
        "Use `read_file`/`list_files` on each child's `outputs/<n>-...` folder to "
        "review its deliverables, then integrate the parts you need into your own work."
    )
    return "\n".join(lines)
```

- [ ] **Step 3b: Add `output_path` to child_results (both paths)**

In `orchestrator/main.py`, in `_handle_delegation_child_completion`, the `child_results.append({...})` dict (~6736-6750) lists per-child fields. Add an `output_path` derived from the child's context. Insert this just before the `child_results.append(` call:

```python
        child_ctx = child.get("context") or {}
        if isinstance(child_ctx, str):
            try:
                child_ctx = json.loads(child_ctx)
            except (json.JSONDecodeError, ValueError):
                child_ctx = {}
        child_output_path = (child_ctx or {}).get("graft_output_path")
```

and add `"output_path": child_output_path,` to the appended dict. Apply the **same** insertion + field to the equivalent `child_results` construction inside `_check_delegation_timeouts` (~6861-6897).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_agent_delegation_format.py tests/test_per_job_repo.py -q --tb=short`
Expected: PASS (new test passes; existing graft/ordinal/scholar tests still pass).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py src/agent.py tests/test_agent_delegation_format.py
git commit -m "feat: delegation resume references grafted outputs, not branch merges"
```

---

## Task 7: Convert `approve_job` and the `subjob_merge` endpoint to graft

**Files:**
- Modify: `orchestrator/main.py:6222-6241` (`approve_job`) and `orchestrator/main.py:4201-4232` (`subjob_merge`)
- Test: `tests/test_per_job_repo.py` (replace the existing `TestSubjobMergeEndpoint::test_returns_merge_result` expectation)

- [ ] **Step 1: Update the failing test**

In `tests/test_per_job_repo.py`, in `TestSubjobMergeEndpoint`, replace `test_returns_merge_result`'s patch target and assertion to use the graft:

```python
    @pytest.mark.asyncio
    async def test_returns_graft_result(self):
        job = {
            "parent_job_id": "parent-id",
            "branch_name": "subjob/abc/scholar",
            "repo_name": "job-parent",
        }
        with (
            patch(f"{MODULE}.postgres_db") as mock_db,
            patch(f"{MODULE}._graft_subjob_output", new_callable=AsyncMock) as mock_graft,
            _bypass_require_internal(),
        ):
            mock_db.get_job = AsyncMock(return_value=job)
            mock_graft.return_value = {"status": "grafted", "output_path": "outputs/001-scholar-abc"}

            result = await orch_main.subjob_merge(_stub_request(), "subjob-id")
            assert result["status"] == "grafted"
            assert result["job_id"] == "subjob-id"
            assert result["output_path"] == "outputs/001-scholar-abc"
```

(Delete the old `test_returns_merge_result`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestSubjobMergeEndpoint -q --tb=short`
Expected: FAIL — `subjob_merge` still calls `_squash_merge_subjob`.

- [ ] **Step 3: Update both call sites**

In `subjob_merge` (`orchestrator/main.py:4222`), change `result = await _squash_merge_subjob(job_id)` to `result = await _graft_subjob_output(job_id)`. Update the docstring line "Performs pre-merge cleanup of job-scoped files, then squash merges." → "Grafts the subjob's output/ onto the parent as a namespaced outputs/ folder."

In `approve_job` (`orchestrator/main.py:6227`), change:

```python
        merge_result = None
        if job.get("parent_job_id"):
            merge_result = await _squash_merge_subjob(job_id)
```

to:

```python
        merge_result = None
        if job.get("parent_job_id"):
            merge_result = await _graft_subjob_output(job_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py::TestSubjobMergeEndpoint -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "refactor(orchestrator): approve_job + subjob-merge endpoint use graft"
```

---

## Task 8: Retire the agent-side merge tools

**Files:**
- Modify: `src/tools/git/git_tools.py` — remove `git_merge_squash` (242-283), `git_worktree_cleanup` (285-326), their `GIT_TOOLS_METADATA` entries (66-81), and the return-list entries (334-335)
- Modify: `tests/test_tools_git.py` (144,145,378,379) and `tests/test_delegation.py` (lines referencing the two tools) — remove those assertions

- [ ] **Step 1: Update the failing test**

In `tests/test_tools_git.py`, find the assertions that the git tool list contains `git_merge_squash`/`git_worktree_cleanup` (around lines 144-145, 378-379) and change them to assert their **absence**, e.g.:

```python
    names = [t.name for t in tools]
    assert "git_merge_squash" not in names
    assert "git_worktree_cleanup" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tools_git.py -q --tb=short`
Expected: FAIL — the tools are still present.

- [ ] **Step 3: Remove the tools**

In `src/tools/git/git_tools.py`:
1. Delete the two `@tool` functions `git_merge_squash` (242-283) and `git_worktree_cleanup` (285-326).
2. Delete their `GIT_TOOLS_METADATA` entries (66-81 — the `"git_merge_squash": {...}` and `"git_worktree_cleanup": {...}` blocks).
3. In the `create_git_tools` return list (328-336), remove the `git_merge_squash,` and `git_worktree_cleanup,` lines so it reads:

```python
    return [
        git_log,
        git_show,
        git_diff,
        git_status,
        git_tags,
    ]
```

(Leave `GitManager.merge_squash`/`worktree_remove`/`delete_branch` in `src/managers/git_manager.py` — they're harmless and out of scope to remove.) In `tests/test_delegation.py`, remove or rewrite the test cases that exercise `git_merge_squash`/`git_worktree_cleanup` (lines ~570-694) since the delegation flow no longer merges branches.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tools_git.py tests/test_delegation.py -q --tb=short`
Expected: PASS (no references to the removed tools remain).

- [ ] **Step 5: Commit**

```bash
git add src/tools/git/git_tools.py tests/test_tools_git.py tests/test_delegation.py
git commit -m "refactor: retire agent-side git_merge_squash/git_worktree_cleanup tools"
```

---

## Task 9: Remove `_squash_merge_subjob` + `SUBJOB_CLEANUP_*` + dead tests

**Files:**
- Modify: `orchestrator/main.py` — delete `_squash_merge_subjob` (353-470), `SUBJOB_CLEANUP_FILES` (319-327), `SUBJOB_CLEANUP_DIRS` (329-337)
- Modify: `tests/test_per_job_repo.py` — delete `TestSquashMergeSubjob`, `TestSubjobCleanupConstants`, and `TestSquashMergeDoesNotClobberParent` (all superseded by the graft tests)

- [ ] **Step 1: Delete the dead tests first**

In `tests/test_per_job_repo.py`, delete the three classes: `TestSquashMergeSubjob` (~162-426), `TestSquashMergeDoesNotClobberParent` (~the block added earlier), and `TestSubjobCleanupConstants` (~648-... including `test_cleanup_files_contains_workspace` and `test_cleanup_dirs_are_scratch_only`). Keep `TestResolveJobRepo`, `TestDeleteJobGiteaCleanup`, `TestSubjobMergeEndpoint`, and all the new `Test*` graft classes.

- [ ] **Step 2: Run to verify no references break**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py -q --tb=short`
Expected: FAIL at **collection** with `AttributeError`/`NameError` only if a remaining test references `SUBJOB_CLEANUP_*` or `_squash_merge_subjob`. If collection is clean and tests pass, the deletions in Step 3 are safe. (If green here, proceed.)

- [ ] **Step 3: Delete the production code**

In `orchestrator/main.py`, delete:
- `SUBJOB_CLEANUP_FILES = [...]` (319-327) and its preceding comment.
- `SUBJOB_CLEANUP_DIRS = [...]` (329-337) and its preceding comment.
- the entire `async def _squash_merge_subjob(job_id)` function (353-470).

Verify nothing else references them:

```bash
grep -rn "_squash_merge_subjob\|SUBJOB_CLEANUP_FILES\|SUBJOB_CLEANUP_DIRS" orchestrator/ src/ tests/
```
Expected: no matches (empty output).

- [ ] **Step 4: Run the file's tests**

Run: `.venv/bin/python -m pytest tests/test_per_job_repo.py -q --tb=short`
Expected: PASS (all remaining tests green).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_per_job_repo.py
git commit -m "refactor(orchestrator): remove _squash_merge_subjob + SUBJOB_CLEANUP_* (superseded by graft)"
```

---

## Task 10: Full-suite verification

- [ ] **Step 1: Run the targeted suites**

Run:
```bash
.venv/bin/python -m pytest tests/test_per_job_repo.py tests/test_gitea_change_files.py tests/test_agent_delegation_format.py tests/test_tools_git.py tests/test_delegation.py -q --tb=short
```
Expected: all PASS.

- [ ] **Step 2: Run the full suite (ignoring pre-existing env errors)**

Run:
```bash
.venv/bin/python -m pytest tests/ -q --tb=line --continue-on-collection-errors
```
Expected: the only failures/errors are the pre-existing, unrelated ones (`tests/test_workspace_backends.py` SFTP collection error; `tests/cloud_sync/*` ModuleNotFound; `tests/test_database_phase1.py` DB-connection tests). No failures referencing graft / subjob / delegation / git tools.

- [ ] **Step 3: Final commit (if any stragglers)**

```bash
git add -A
git commit -m "test: verify subjob output graft model end-to-end" || echo "nothing to commit"
```

---

## Self-review

**Spec coverage:**
- Extract-and-graft of `output/` → namespaced folder → Tasks 2, 3.
- One commit, bytes-faithful (binary-safe) → Task 1 (`change_files`).
- Uniform orchestrator-side, incl. delegation children → Task 4.
- Critic grafts nothing → Task 3 (`verification_target` guard), tested.
- Parent never modified/deleted; collisions impossible → Task 3 test `..._leaves_parent_untouched`.
- Recency-ordered, zero-padded ordinal → Task 2.
- Scholar consumption pointer → Task 5; delegation resume reads `outputs/*` → Task 6.
- Remove squash/cleanup/conflict machinery + agent merge tools → Tasks 8, 9.
- Subjob branch retained (we never touch it) → guaranteed by construction (no delete/merge of the subjob branch anywhere).

**Placeholder scan:** none — every code/test step has complete code; every run step has an exact command + expected outcome.

**Type/name consistency:** `_graft_subjob_output` returns `{"status","base_branch","output_path","ordinal","files"}` (Task 3) and callers read `status`/`output_path` (Tasks 4, 7). `_next_output_ordinal(repo_name, base_branch)->str` consistent (Tasks 2, 3). `change_files(repo_name, branch, files:[{path,content_b64}], message)` consistent between Task 1 (impl + test) and Task 3 (caller + fake). `graft_output_path` written in Task 3, read in Tasks 5 & 6. `_format_delegation_results` consumes `config_name`/`output_path` (Task 6) which the orchestrator now populates (Task 6 Step 3b).

**Note on test seam:** logic (ordinal, graft, handlers, formatter) is TDD'd against in-memory fakes; `change_files` is a thin httpx wrapper tested via a payload-shape unit test (Task 1) — matching the codebase convention of not integration-testing Gitea wrappers.

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-24-subjob-output-merge-model.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
