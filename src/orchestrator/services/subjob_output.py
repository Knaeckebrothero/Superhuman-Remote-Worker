"""Resolve job repositories and graft completed subjob output.

The application supplies the persistence and forge authorities explicitly.  The
command-keyed reconciliation helpers remain the durable completion authority;
this module only composes them around the existing additive graft operation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Protocol

from fastapi import HTTPException

from orchestrator.services.completion_effect_reconciliation import (
    graft_commit_message,
    probe_graft_commit,
)

logger = logging.getLogger(__name__)


class SubjobOutputStore(Protocol):
    async def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    async def get_project_repositories(
        self, project_id: str, *, role: str
    ) -> list[dict[str, Any]]: ...

    async def update_job_merge_status(
        self, job_id: str, *, merge_status: str
    ) -> bool: ...

    async def merge_job_context(self, job_id: str, updates: dict[str, Any]) -> bool: ...


class SubjobOutputForge(Protocol):
    @property
    def is_initialized(self) -> bool: ...

    async def list_contents(
        self, repo_name: str, path: str, *, ref: str | None = None
    ) -> list[dict[str, Any]] | None: ...

    async def list_tree(
        self, repo_name: str, *, ref: str | None = None
    ) -> list[dict[str, Any]] | None: ...

    async def get_file_bytes(
        self, repo_name: str, path: str, *, ref: str | None = None
    ) -> bytes | None: ...

    async def change_files(
        self,
        repo_name: str,
        branch: str,
        files: list[dict[str, Any]],
        *,
        message: str,
    ) -> bool: ...


@dataclass(frozen=True)
class SubjobOutputDependencies:
    store: SubjobOutputStore
    forge: SubjobOutputForge


async def resolve_job_repo(
    job_id: str, *, dependencies: SubjobOutputDependencies
) -> tuple[str, str | None]:
    """Resolve the Gitea repository and branch for a job."""
    job = await dependencies.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if job.get("repo_name"):
        return job["repo_name"], job.get("branch_name")

    if job.get("parent_job_id"):
        parent = await dependencies.store.get_job(str(job["parent_job_id"]))
        if parent and parent.get("repo_name"):
            return parent["repo_name"], job.get("branch_name")

    if job.get("project_id"):
        repos = await dependencies.store.get_project_repositories(
            str(job["project_id"]), role="jobs"
        )
        if repos:
            return repos[0]["name"], job.get("branch_name")

    return f"job-{job_id}", None


async def next_output_ordinal(
    repo_name: str,
    base_branch: str,
    *,
    dependencies: SubjobOutputDependencies,
) -> str:
    """Return the next zero-padded ordinal for ``outputs/<n>-...``."""
    entries = (
        await dependencies.forge.list_contents(repo_name, "outputs", ref=base_branch)
        or []
    )
    nums = []
    for entry in entries:
        if entry.get("type") == "dir":
            match = re.match(r"(\d+)-", entry.get("name", ""))
            if match:
                nums.append(int(match.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    return f"{nxt:03d}"


async def graft_subjob_output(
    job_id: str,
    *,
    dependencies: SubjobOutputDependencies,
    completion_command_id: str | None = None,
) -> dict[str, Any] | None:
    """Graft a completed subjob's ``output/`` onto its parent's branch."""
    store = dependencies.store
    forge = dependencies.forge
    job = await store.get_job(job_id)
    if not job or not job.get("parent_job_id"):
        return None
    if not job.get("branch_name") or not job.get("repo_name"):
        logger.debug("Subjob %s has no branch/repo — skipping graft", job_id)
        return None
    if not forge.is_initialized:
        logger.warning("Gitea not initialized — cannot graft subjob %s", job_id)
        return None

    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    if isinstance(ctx, dict) and ctx.get("verification_target"):
        await store.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "critic-not-merged"}

    if isinstance(ctx, dict) and ctx.get("graft_output_path"):
        return {
            "status": "skipped",
            "reason": "already-grafted",
            "output_path": ctx["graft_output_path"],
        }

    repo_name = job["repo_name"]
    subjob_branch = job["branch_name"]
    short_id = str(job_id)[:8]
    config_name = job.get("config_name") or "subjob"

    parent = await store.get_job(str(job["parent_job_id"]))
    base_branch = (parent.get("branch_name") if parent else None) or "main"

    if completion_command_id is not None:
        prior_commit = await probe_graft_commit(
            forge,
            repo_name=repo_name,
            branch=base_branch,
            command_id=completion_command_id,
        )
        if prior_commit is not None:
            merge_status_recorded = await store.update_job_merge_status(
                job_id, merge_status="grafted"
            )
            path_recorded = await store.merge_job_context(
                job_id, {"graft_output_path": prior_commit.output_path}
            )
            if not merge_status_recorded or not path_recorded:
                raise RuntimeError(
                    "could not reconcile the command-keyed graft database markers"
                )
            return {
                "status": "grafted",
                "reason": "reconciled-command-trailer",
                "base_branch": base_branch,
                "output_path": prior_commit.output_path,
                "commit_sha": prior_commit.commit_sha,
            }

    tree_result = await forge.list_tree(repo_name, ref=subjob_branch)
    if tree_result is None and completion_command_id is not None:
        raise RuntimeError("could not read the subjob tree for durable graft")
    tree = tree_result or []
    output_blobs = [
        entry["path"]
        for entry in tree
        if entry.get("type") == "blob" and entry["path"].startswith("output/")
    ]
    if not output_blobs:
        await store.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "no-output"}

    ordinal = await next_output_ordinal(
        repo_name, base_branch, dependencies=dependencies
    )
    dest = f"outputs/{ordinal}-{config_name}-{short_id}"

    files: list[dict[str, Any]] = []
    for path in output_blobs:
        data = await forge.get_file_bytes(repo_name, path, ref=subjob_branch)
        if data is None:
            logger.warning("Graft %s: failed to read %s; aborting graft", job_id, path)
            if completion_command_id is not None:
                raise RuntimeError(f"could not read {path} for durable graft")
            await store.update_job_merge_status(job_id, merge_status="graft-failed")
            return {"status": "error", "reason": "read-failed", "path": path}
        rel = path[len("output/") :]
        files.append(
            {
                "path": f"{dest}/{rel}",
                "content_b64": base64.b64encode(data).decode("ascii"),
            }
        )

    commit_message = f"Graft {dest} from subjob {short_id}"
    if completion_command_id is not None:
        commit_message = graft_commit_message(
            output_path=dest,
            subjob_short_id=short_id,
            command_id=completion_command_id,
        )
    ok = await forge.change_files(repo_name, base_branch, files, message=commit_message)
    if not ok:
        if completion_command_id is not None:
            raise RuntimeError("durable graft write outcome is ambiguous")
        await store.update_job_merge_status(job_id, merge_status="graft-failed")
        return {"status": "error", "reason": "write-failed"}

    merge_status_recorded = await store.update_job_merge_status(
        job_id, merge_status="grafted"
    )
    path_recorded = await store.merge_job_context(job_id, {"graft_output_path": dest})
    if completion_command_id is not None and (
        not merge_status_recorded or not path_recorded
    ):
        raise RuntimeError("could not persist the command-keyed graft database markers")

    logger.info(
        "Grafted subjob %s/%s output (%d files) to %s:%s",
        short_id,
        config_name,
        len(files),
        base_branch,
        dest,
    )
    return {
        "status": "grafted",
        "base_branch": base_branch,
        "output_path": dest,
        "ordinal": ordinal,
        "files": len(files),
    }


async def maybe_graft_completed_subjob(
    job: dict[str, Any],
    *,
    dependencies: SubjobOutputDependencies,
    completion_command_id: str | None = None,
) -> dict[str, Any] | None:
    """Graft a completed subjob and ignore root jobs."""
    if not job.get("parent_job_id"):
        return None
    return await graft_subjob_output(
        str(job["id"]),
        dependencies=dependencies,
        completion_command_id=completion_command_id,
    )
