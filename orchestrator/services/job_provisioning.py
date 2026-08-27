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

import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from services.managed_repository_authority import (
    ManagedRepositoryAuthorityError,
    create_managed_repository,
    ensure_job_primary_repository_authority,
    ensure_managed_repository_authority,
    revoke_and_delete_managed_repository,
)

logger = logging.getLogger(__name__)


class JobProvisioningError(RuntimeError):
    """Machine-readable mandatory provisioning failure."""

    failure_class = "infrastructure"

    def __init__(self, message: str, *, phase: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.phase = phase
        self.retryable = retryable


# Job-scoped scratch + framework scaffolding that should not be committed to an
# isolated loop job's execution repo. Safe to ignore everywhere: todos
# restore from the LangGraph checkpoint (not disk), plan.md/workspace.md are read
# only for non-blocking curation (FileNotFoundError-tolerant), and archive/ is
# recovered from phase snapshots — not git. `skills/` (capability bundles
# materialized into the workspace when SKILLS_DB_ENABLED) and `notes/` are
# framework/job-scoped, not deliverables — a k3d E2E caught `skills/` leaking
# into its durable cloud diff. The agent's actual project files live below
# `projects/<slug>/` and are NOT floored. See project_jobs_repo_retirement.md.
_LOOP_JOB_GITIGNORE = "\n".join(
    [
        "# Loop execution floor — job-scoped scratch stays out of the job commit.",
        "workspace.md",
        "plan.md",
        "todos.yaml",
        "archive/",
        "tools/",
        "documents/",
        "reference/",
        "skills/",
        "notes/",
        "instructions.md",
        "task_brief.md",
        ".srw_seeded",
        "output/job_frozen.json",
        "output/job_completion.json",
        "repos/",
        ".env",
        "*.key",
        "secrets*",
        "",
    ]
)


async def _permit(authority_check: Callable[[], Awaitable[None]] | None) -> None:
    if authority_check is not None:
        await authority_check()


async def _ensure_loop_job_gitignore(
    gitea_client: Any,
    repo_name: str,
    *,
    authority_check: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Seed the scratch-floor ``.gitignore`` on an isolated loop repo (idempotent).

    Runs before the loop job works on its isolated repo's ``main`` so commits
    exclude job-scoped scratch. Idempotent via a sentinel: if ``.gitignore`` already
    carries the floor, do nothing. A loop treats ``False`` as a provisioning
    failure because the seed commit also establishes a readable ``main`` HEAD.
    """
    await _permit(authority_check)
    try:
        existing = await gitea_client.get_file_bytes(
            repo_name, ".gitignore", ref="main"
        )
        await _permit(authority_check)
        if existing is not None and b"todos.yaml" in existing:
            return True  # floor already present
        if existing is None:
            content_b64 = base64.b64encode(_LOOP_JOB_GITIGNORE.encode("utf-8")).decode(
                "ascii"
            )
            changed = await gitea_client.change_files(
                repo_name,
                "main",
                [{"path": ".gitignore", "content_b64": content_b64}],
                message="Add loop scratch .gitignore floor",
            )
            await _permit(authority_check)
            return bool(changed)
        else:
            # .gitignore exists without the floor (rare — seeding runs before the
            # workspace ever touches main). change_files is create-only, so leave
            # it rather than clobber; logged so the gap is visible.
            logger.warning(
                "Loop repo %s has a .gitignore without the scratch floor; "
                "scratch files may reach main",
                repo_name,
            )
            return False
    except Exception as e:
        if authority_check is not None:
            from services.project_loop_atomic import ProjectLoopHandoffAuthorityLost

            if isinstance(e, ProjectLoopHandoffAuthorityLost):
                raise
        logger.warning("Failed to seed loop .gitignore floor for %s: %s", repo_name, e)
        return False


async def provision_job_repo(
    *,
    job_row: dict[str, Any],
    gitea_client: Any,
    postgres_db: Any,
    main_cloud_router: Any,
    loop_floor: bool = False,
    require_repository: bool = False,
    authority_check: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Provision a job's Gitea repo/branch, grant creator access, seed baseline.

    Mutates ``job_row`` in place (sets ``repo_name`` / ``branch_name``) and
    returns it. Ordinary jobs preserve the best-effort Gitea behavior: an
    outage can leave them repo-less. ``require_repository=True`` makes the
    isolated repository mandatory; ``loop_floor=True`` additionally requires
    the complete project cloud baseline. No-ops without Gitea only for ordinary
    jobs.

    Behaviour matches the block previously inlined in the ``POST /api/jobs``
    handler, with one improvement folded in: the creator access-grant now
    passes ``username`` / ``full_name`` / ``sub`` so Gitea can pre-provision
    the user when they have never logged into Gitea directly (the old
    email-only call silently no-ops for such users).
    """
    await _permit(authority_check)
    if not gitea_client.is_initialized:
        if loop_floor or require_repository:
            raise JobProvisioningError(
                "Gitea is unavailable; cannot provision isolated job repository",
                phase="repository",
            )
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
        await _permit(authority_check)
        parent = await postgres_db.get_job(parent_job_id)
        await _permit(authority_check)
        if parent:
            # Resolve and prove the exact root/shared-jobs authority before
            # mutating Gitea. Historical parents can legitimately have no
            # job-level repo_name and obtain their primary from the project's
            # single managed role=jobs row.
            try:
                parent_authority = await ensure_job_primary_repository_authority(
                    postgres_db, gitea_client, parent
                )
            except ManagedRepositoryAuthorityError as exc:
                raise JobProvisioningError(
                    "could not authorize inherited job repository",
                    phase="repository",
                ) from exc
            if parent_authority is None:
                raise JobProvisioningError(
                    "inherited job repository has no scoped authority",
                    phase="repository",
                )
            parent_repo_name = str(parent_authority["repo_name"])

            from_branch = parent.get("branch_name") or "main"
            config_name_slug = config_name or "subjob"
            branch_name = f"subjob/{short_id}/{config_name_slug}"
            branch_ok = await gitea_client.create_branch(
                parent_repo_name, branch_name, from_branch=from_branch
            )
            await _permit(authority_check)
            if not branch_ok:
                logger.error(
                    f"Failed to create branch '{branch_name}' from '{from_branch}' "
                    f"in '{parent_repo_name}' for subjob {job_id_str}"
                )
                if loop_floor or require_repository:
                    raise JobProvisioningError(
                        f"could not create isolated job branch {branch_name}",
                        phase="repository",
                    )
            if not await postgres_db.bind_job_managed_repository(
                job_id_str,
                repo_name=parent_repo_name,
                clean_url=str(parent_authority["clean_repo_url"]),
            ):
                raise JobProvisioningError(
                    "could not bind inherited repository authority",
                    phase="repository",
                )
            await _permit(authority_check)
            async with postgres_db.acquire() as conn:
                await _permit(authority_check)
                await conn.execute(
                    "UPDATE jobs SET branch_name = $1 WHERE id = $2",
                    branch_name,
                    job_row["id"],
                )
            await _permit(authority_check)
            job_row["branch_name"] = branch_name
            job_row["repo_name"] = parent_repo_name
        elif loop_floor or require_repository:
            raise JobProvisioningError(
                "parent job is unavailable for isolated branch provisioning",
                phase="repository",
            )
    else:
        # Root job: always create an isolated repo. Project membership controls
        # shared resources (cloud, KB, source/reference attachments), never the
        # job's workspace lineage. Existing jobs keep their stored legacy repo;
        # only newly-created roots pass through this branch.
        repo_name = f"job-{short_id}"
        await _permit(authority_check)
        creation_intent: dict[str, Any] | None = None
        try:
            git_remote_url, creation_intent = await create_managed_repository(
                postgres_db,
                gitea_client,
                repo_name=repo_name,
                authority_kind="job",
                authority_id=job_id_str,
                project_id=project_id,
                access_mode="write",
            )
        except ManagedRepositoryAuthorityError:
            git_remote_url = None
        await _permit(authority_check)
        if git_remote_url:
            try:
                repository_authority = await ensure_managed_repository_authority(
                    postgres_db,
                    gitea_client,
                    repo_name=repo_name,
                    authority_kind="job",
                    authority_id=job_id_str,
                    access_mode="write",
                    creation_intent_id=str(creation_intent["id"]),
                    project_id=(
                        str(job_row["project_id"])
                        if job_row.get("project_id")
                        else None
                    ),
                )
                # Bind immediately after key proof. If any later provisioning
                # step fails, the durable job row remains the exact cleanup/
                # retry handle instead of leaving an active key known only to
                # an authority row.
                if not await postgres_db.bind_job_managed_repository(
                    job_id_str,
                    repo_name=repo_name,
                    clean_url=str(repository_authority["clean_repo_url"]),
                ):
                    raise JobProvisioningError(
                        "could not bind scoped job repository authority",
                        phase="repository",
                    )
                job_row["repo_name"] = repo_name
                gitignore_kwargs = (
                    {"authority_check": authority_check}
                    if authority_check is not None
                    else {}
                )
                if loop_floor and not await _ensure_loop_job_gitignore(
                    gitea_client,
                    repo_name,
                    **gitignore_kwargs,
                ):
                    raise JobProvisioningError(
                        f"could not initialize isolated job repository {repo_name}",
                        phase="repository",
                    )
                await _permit(authority_check)
            except Exception as exc:
                await revoke_and_delete_managed_repository(
                    postgres_db, gitea_client, repo_name
                )
                if isinstance(exc, JobProvisioningError):
                    raise
                if not isinstance(exc, ManagedRepositoryAuthorityError):
                    raise
                raise JobProvisioningError(
                    "could not establish scoped job repository authority",
                    phase="repository",
                ) from exc
        elif loop_floor or require_repository:
            raise JobProvisioningError(
                f"could not create isolated job repository {repo_name}",
                phase="repository",
            )

    # Grant job creator read access to the Gitea repo. Pass username +
    # full_name + sub so grant_user_repo_access can pre-provision the Gitea
    # user if they haven't visited Gitea directly yet; sub is used as
    # login_name so Gitea's OIDC matches this account on first direct login
    # instead of creating a duplicate.
    if job_row.get("repo_name") and user_id:
        try:
            await _permit(authority_check)
            creator = await postgres_db.get_user(user_id)
            await _permit(authority_check)
            if creator and creator.get("email"):
                email_local = creator["email"].split("@")[0]
                await gitea_client.grant_user_repo_access(
                    creator["email"],
                    job_row["repo_name"],
                    username=creator.get("preferred_username") or email_local,
                    full_name=creator.get("display_name"),
                    sub=creator.get("keycloak_sub"),
                )
                await _permit(authority_check)
        except Exception as e:
            if authority_check is not None:
                from services.project_loop_atomic import (
                    ProjectLoopHandoffAuthorityLost,
                )

                if isinstance(e, ProjectLoopHandoffAuthorityLost):
                    raise
            logger.warning(f"Failed to grant Gitea access for job {job_id_str}: {e}")

    # Mode A baseline seed (job_cloud_export.md §3.1). Fire-and-forget —
    # runs in a background task that walks the project's cloud folder and
    # pushes text files into the job's Gitea repo under projects/<slug>/.
    # While it runs, the job carries context.cloud_baseline.state='seeding'
    # and the dispatcher gate skips it. Ordinary jobs seed asynchronously and
    # retain the legacy fail-open behavior. Loops seed synchronously and fail
    # their spawn if the baseline is incomplete: an unattended loop must never
    # run without the durable file state it is meant to improve.
    # Only fires when the project actually has a cloud folder provisioned;
    # loose jobs and projects without a cloud_folder handle skip this path.
    if project_id and job_row.get("repo_name"):
        try:
            await _permit(authority_check)
            project_row = await postgres_db.get_project(project_id)
            await _permit(authority_check)
        except Exception as exc:
            if authority_check is not None:
                from services.project_loop_atomic import (
                    ProjectLoopHandoffAuthorityLost,
                )

                if isinstance(exc, ProjectLoopHandoffAuthorityLost):
                    raise
            if loop_floor:
                raise JobProvisioningError(
                    "project cloud configuration is unavailable",
                    phase="cloud",
                ) from exc
            project_row = None
        if project_row and project_row.get("main_cloud_folder_handle"):
            if loop_floor:
                from services.job_cloud_baseline import seed_project_folder_baseline

                baseline_kwargs: dict[str, Any] = {}
                if authority_check is not None:
                    baseline_kwargs["authority_check"] = authority_check
                try:
                    await seed_project_folder_baseline(
                        job_id=job_id_str,
                        project=project_row,
                        repo_name=job_row["repo_name"],
                        branch=job_row.get("branch_name"),
                        postgres_db=postgres_db,
                        gitea_client=gitea_client,
                        main_cloud_router=main_cloud_router,
                        require_complete=True,
                        **baseline_kwargs,
                    )
                except Exception as exc:
                    if authority_check is not None:
                        from services.project_loop_atomic import (
                            ProjectLoopHandoffAuthorityLost,
                        )

                        if isinstance(exc, ProjectLoopHandoffAuthorityLost):
                            raise
                    raise JobProvisioningError(
                        "project cloud baseline could not be seeded completely",
                        phase="cloud",
                    ) from exc
                await _permit(authority_check)
                refreshed = await postgres_db.get_job(job_id_str)
                await _permit(authority_check)
                if not refreshed or not refreshed.get("cloud_diff_baseline_commit"):
                    raise JobProvisioningError(
                        "project cloud baseline could not be seeded completely",
                        phase="cloud",
                    )
            else:
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
        elif loop_floor:
            raise JobProvisioningError(
                "project loop requires a provisioned cloud folder",
                phase="cloud",
            )

    return job_row
