"""``DiffSource`` protocol + ``GiteaDiffSource`` — Mode A job diff seam.

The two ``/api/jobs/{job_id}/diff`` endpoints (main.py) originally fetched
diff data inline from Gitea. This module extracts that data-fetch behind a
small seam so a later task can add an ``UpperdirDiffSource`` (protected
cloud mode) implementation that the same endpoints can serve from, without
touching endpoint gate logic.

``GiteaDiffSource`` is the Mode A implementation: it diffs two Gitea tree
snapshots (baseline commit .. branch head) and reads per-file content via
Gitea's contents API — text only, per the v1 baseline-seed limitation (see
``job_cloud_baseline.py`` module docstring). ``binary``/``old_binary``/
``new_binary`` always read ``False`` here; a byte-aware source (upperdir)
sets them per-file.

See docs/done/job_cloud_export.md §5 and the Task 6 SDD brief
(.superpowers/sdd/task-6-brief.md) for the shape contract — Tasks 7/8/10
depend on these dataclasses staying exactly as defined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiffEntrySummary:
    path: str
    status: str  # "added" | "modified" | "deleted"
    binary: bool = False


@dataclass(frozen=True)
class DiffSummary:
    files: list[DiffEntrySummary]
    meta: dict[str, Any]  # source-specific: gitea -> baseline_commit/head_commit;
    # upperdir -> epoch/staged_at/counts


@dataclass(frozen=True)
class DiffFileContent:
    path: str
    status: str
    old_content: str | None
    new_content: str | None
    old_binary: bool = False
    new_binary: bool = False


class GiteaDiffSource:
    """Mode A diff source: Gitea trees at baseline..branch (text-only)."""

    def __init__(self, *, job: dict, gitea_client: Any):
        self._job = job
        self._gitea = gitea_client

    async def summary(self) -> DiffSummary | None:
        from services.job_cloud_baseline import get_diff_summary

        s = await get_diff_summary(job=self._job, gitea_client=self._gitea)
        if s is None:
            return None
        return DiffSummary(
            files=[
                DiffEntrySummary(path=f["path"], status=f["status"]) for f in s["files"]
            ],
            meta={
                "baseline_commit": s["baseline_commit"],
                "head_commit": s["head_commit"],
            },
        )

    async def file(self, path: str) -> DiffFileContent | None:
        s = await self.summary()
        if s is None:
            return None
        entry = next((f for f in s.files if f.path == path), None)
        if entry is None:
            return None
        repo = self._job.get("repo_name")
        baseline = self._job.get("cloud_diff_baseline_commit")
        branch = self._job.get("branch_name") or "main"
        old = new = None
        if entry.status in ("modified", "deleted"):
            old = await self._gitea.get_file_content(repo, path, ref=baseline)
        if entry.status in ("modified", "added"):
            new = await self._gitea.get_file_content(repo, path, ref=branch)
        return DiffFileContent(
            path=path, status=entry.status, old_content=old, new_content=new
        )
