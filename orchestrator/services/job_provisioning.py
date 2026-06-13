"""Shared Gitea/workspace provisioning for newly-created jobs.

Extracted from the ``POST /api/jobs`` handler so EVERY job-creation path —
the HTTP handler, cron automations, and the run-now button — provisions the
job's Gitea repo/branch, grants the creator access, and seeds the Mode A
cloud baseline identically. Before this existed, automation-spawned jobs
called ``db.create_job()`` directly and silently skipped all of it, leaving
``repo_name``/``branch_name``/``git_remote_url`` NULL and the workspace
button 404ing.

Dependencies are injected (``gitea_client``, ``postgres_db``,
``main_cloud_router``) so this module has no import cycle with ``main``.

IMPORTANT — asyncpg UUID coercion: ``job_row`` comes straight from
``postgres_db.create_job()``'s RETURNING row, so its ``id`` /
``parent_job_id`` / ``project_id`` / ``user_id`` are native ``uuid.UUID``
objects. The ``postgres_db`` helpers called here (``get_job``,
``get_project_repositories``, ``get_project``, ``get_user``,
``merge_job_context``) do ``UUID(arg)`` internally and raise on a ``UUID``
instance, so every id derived from ``job_row`` must be ``str()``-coerced
before use. The original handler dodged this by passing the request's
Pydantic strings.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def provision_job_repo(
    *,
    job_row: dict[str, Any],
    gitea_client: Any,
    postgres_db: Any,
    main_cloud_router: Any,
) -> dict[str, Any]:
    """Provision a job's Gitea repo/branch, grant creator access, seed baseline.

    Mutates ``job_row`` in place (sets ``repo_name`` / ``branch_name``) and
    returns it. Best-effort: a Gitea outage logs and leaves the job
    repo-less rather than raising — callers treat provisioning as
    non-fatal. No-ops when Gitea isn't initialized.

    Behaviour matches the block previously inlined in the ``POST /api/jobs``
    handler, with one improvement folded in: the creator access-grant now
    passes ``username`` / ``full_name`` / ``sub`` so Gitea can pre-provision
    the user when they have never logged into Gitea directly (the old
    email-only call silently no-ops for such users).
    """
    if not gitea_client.is_initialized:
        return job_row

    # asyncpg returns native UUIDs; str()-coerce before any postgres_db
    # helper that does UUID(arg) internally (see module docstring).
    job_id_str = str(job_row["id"])
    short_id = job_id_str[:8]
    parent_job_id = (
        str(job_row["parent_job_id"]) if job_row.get("parent_job_id") else None
    )
    project_id = str(job_row["project_id"]) if job_row.get("project_id") else None
    user_id = str(job_row["user_id"]) if job_row.get("user_id") else None
    config_name = job_row.get("config_name")

    if parent_job_id:
        # Subjob: branch on parent's repo
        parent = await postgres_db.get_job(parent_job_id)
        if parent:
            # Resolve parent's repo name (parent may be root or itself a subjob)
            parent_repo_name = parent.get("repo_name")
            if not parent_repo_name and parent.get("parent_job_id"):
                root = await postgres_db.get_job(str(parent["parent_job_id"]))
                if root:
                    parent_repo_name = root.get("repo_name")
            if not parent_repo_name:
                # Legacy fallback: try project jobs repo
                if parent.get("project_id"):
                    repos = await postgres_db.get_project_repositories(
                        str(parent["project_id"]), role="jobs"
                    )
                    if repos:
                        parent_repo_name = repos[0]["name"]
                if not parent_repo_name:
                    parent_repo_name = f"job-{str(parent['id'])}"

            from_branch = parent.get("branch_name") or "main"
            config_name_slug = config_name or "subjob"
            branch_name = f"subjob/{short_id}/{config_name_slug}"
            branch_ok = await gitea_client.create_branch(
                parent_repo_name, branch_name, from_branch=from_branch
            )
            if not branch_ok:
                logger.error(
                    f"Failed to create branch '{branch_name}' from '{from_branch}' "
                    f"in '{parent_repo_name}' for subjob {job_id_str}"
                )
            await postgres_db.merge_job_context(
                job_id_str,
                {
                    "git_remote_url": parent.get("context", {}).get(
                        "git_remote_url", ""
                    ),
                },
            )
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3",
                    branch_name,
                    parent_repo_name,
                    job_row["id"],
                )
            job_row["branch_name"] = branch_name
            job_row["repo_name"] = parent_repo_name
    elif project_id:
        # Project job: branch in project's shared jobs repo
        repos = await postgres_db.get_project_repositories(project_id, role="jobs")
        if repos:
            jobs_repo = repos[0]
            branch_name = f"job/{short_id}"
            branch_ok = await gitea_client.create_branch(
                jobs_repo["name"], branch_name, from_branch="main"
            )
            if not branch_ok:
                logger.error(
                    f"Failed to create branch '{branch_name}' in "
                    f"'{jobs_repo['name']}' — main branch may not exist"
                )
            await postgres_db.merge_job_context(
                job_id_str,
                {
                    "git_remote_url": jobs_repo["repo_url"],
                },
            )
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3",
                    branch_name,
                    jobs_repo["name"],
                    job_row["id"],
                )
            job_row["branch_name"] = branch_name
            job_row["repo_name"] = jobs_repo["name"]
        else:
            # Project has no jobs repo — fall back to per-job repo
            repo_name = f"job-{short_id}"
            git_remote_url = await gitea_client.create_repo(repo_name)
            if git_remote_url:
                await postgres_db.merge_job_context(
                    job_id_str,
                    {
                        "git_remote_url": git_remote_url,
                    },
                )
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET repo_name = $1 WHERE id = $2",
                        repo_name,
                        job_row["id"],
                    )
                job_row["repo_name"] = repo_name
    else:
        # Root job without project: create standalone per-job repo
        repo_name = f"job-{short_id}"
        git_remote_url = await gitea_client.create_repo(repo_name)
        if git_remote_url:
            await postgres_db.merge_job_context(
                job_id_str,
                {
                    "git_remote_url": git_remote_url,
                },
            )
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET repo_name = $1 WHERE id = $2",
                    repo_name,
                    job_row["id"],
                )
            job_row["repo_name"] = repo_name

    # Grant job creator read access to the Gitea repo. Pass username +
    # full_name + sub so grant_user_repo_access can pre-provision the Gitea
    # user if they haven't visited Gitea directly yet; sub is used as
    # login_name so Gitea's OIDC matches this account on first direct login
    # instead of creating a duplicate.
    if job_row.get("repo_name") and user_id:
        try:
            creator = await postgres_db.get_user(user_id)
            if creator and creator.get("email"):
                email_local = creator["email"].split("@")[0]
                await gitea_client.grant_user_repo_access(
                    creator["email"],
                    job_row["repo_name"],
                    username=creator.get("preferred_username") or email_local,
                    full_name=creator.get("display_name"),
                    sub=creator.get("keycloak_sub"),
                )
        except Exception as e:
            logger.warning(f"Failed to grant Gitea access for job {job_id_str}: {e}")

    # Mode A baseline seed (job_cloud_export.md §3.1). Fire-and-forget —
    # runs in a background task that walks the project's cloud folder and
    # pushes text files into the job's Gitea repo under projects/<slug>/.
    # While it runs, the job carries context.cloud_baseline.state='seeding'
    # and the dispatcher gate skips it. When done, it flips to 'ready' (or
    # 'failed' on error — we still dispatch so the job doesn't deadlock).
    # Only fires when the project actually has a cloud folder provisioned;
    # loose jobs and projects without a cloud_folder handle skip this path.
    if project_id and job_row.get("repo_name"):
        try:
            project_row = await postgres_db.get_project(project_id)
        except Exception:
            project_row = None
        if project_row and project_row.get("main_cloud_folder_handle"):
            from services.job_cloud_baseline import fire_baseline_seed

            fire_baseline_seed(
                job_id=job_id_str,
                project=project_row,
                repo_name=job_row["repo_name"],
                branch=job_row.get("branch_name"),
                postgres_db=postgres_db,
                gitea_client=gitea_client,
                main_cloud_router=main_cloud_router,
            )

    return job_row
