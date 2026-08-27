"""Server-owned, repository-scoped authority for managed Gitea workspaces.

Durable job/thread state carries only a credential-free canonical HTTP URL.
The exact runtime receives a short-lived internal payload containing one
encrypted-at-rest deploy key; the agent materializes it directly into the
remote workspace, removes the payload from metadata, and clones through a
credential-free SSH host alias.  This module never returns authority through a
public API, formatter, tool, transcript, or audit surface.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from security.access import vm_workspaces_on_pod_network

logger = logging.getLogger(__name__)

_MANAGED_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_RUNTIME_BUNDLE_VERSION = 1


class ManagedRepositoryAuthorityError(RuntimeError):
    """A managed repository cannot be authorized without weakening scope."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class ManagedRepositoryRuntimeCredential:
    authority_id: str
    generation: int
    access_mode: str
    repo_name: str
    repository_owner: str
    clone_url: str
    alias: str
    ssh_host: str
    ssh_port: int
    private_key: str
    public_key_fingerprint: str

    def to_payload(self) -> dict[str, Any]:
        """Internal-only payload.  Never log, persist, or expose this dict."""
        return {
            "version": _RUNTIME_BUNDLE_VERSION,
            "authority_id": self.authority_id,
            "generation": self.generation,
            "access_mode": self.access_mode,
            "repo_name": self.repo_name,
            "repository_owner": self.repository_owner,
            "clone_url": self.clone_url,
            "alias": self.alias,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "private_key": self.private_key,
            "public_key_fingerprint": self.public_key_fingerprint,
        }


def _deploy_keypair() -> tuple[str, str, str]:
    private = Ed25519PrivateKey.generate()
    private_text = private.private_bytes(
        Encoding.PEM,
        PrivateFormat.OpenSSH,
        NoEncryption(),
    ).decode("utf-8")
    public_bytes = private.public_key().public_bytes(
        Encoding.OpenSSH,
        PublicFormat.OpenSSH,
    )
    public_text = public_bytes.decode("ascii")
    digest = hashlib.sha256(base64.b64decode(public_text.split()[1])).digest()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
    return private_text, public_text, fingerprint


def _validate_repository_identity(repository_owner: str, repo_name: str) -> None:
    if not _MANAGED_REPO_NAME.fullmatch(repository_owner or ""):
        raise ManagedRepositoryAuthorityError("repository_owner_invalid")
    if not _MANAGED_REPO_NAME.fullmatch(repo_name or ""):
        raise ManagedRepositoryAuthorityError("repository_name_invalid")


def repository_url_has_credentials(value: Any) -> bool:
    """Return whether a Git URL embeds userinfo or scp-style identity."""

    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme:
        return parsed.username is not None or parsed.password is not None
    return bool(re.match(r"^[^/\\\s]+@[^:]+:", text))


def project_repository_access_mode(
    repository: Mapping[str, Any],
) -> str | None:
    """Return runtime authority required by one managed project attachment."""

    if not repository.get("is_managed"):
        return None
    role = str(repository.get("role") or "")
    if role == "knowledge":
        return None
    if role == "reference" or bool(repository.get("read_only")):
        return "read"
    return "write"


async def create_managed_repository(
    postgres_db: Any,
    gitea_client: Any,
    *,
    repo_name: str,
    authority_kind: str,
    authority_id: str,
    project_id: str | None,
    access_mode: str,
) -> tuple[str, dict[str, Any]]:
    """Create/re-adopt a repository through one durable exact intent."""

    repository_owner = str(gitea_client.repository_owner)
    _validate_repository_identity(repository_owner, repo_name)
    try:
        intent = await postgres_db.reserve_managed_repository_creation_intent(
            repository_owner=repository_owner,
            repo_name=repo_name,
            authority_kind=authority_kind,
            authority_id=str(authority_id),
            project_id=str(project_id) if project_id else None,
            access_mode=access_mode,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ManagedRepositoryAuthorityError("repository_creation_conflict") from exc

    clean_url = await gitea_client.create_repo(
        repo_name, intent_marker=str(intent["intent_marker"])
    )
    if not clean_url:
        # ``create_repo`` already performs the post/409 marker read. Re-read
        # once at this durable boundary so a proven foreign/name-only collision
        # becomes a terminal, non-owning audit result rather than a live intent
        # that wedges deletion of the job/thread which never received a repo.
        # Missing/unavailable remains pending and retryable because the POST
        # may have committed while both response and verification were lost.
        intent_status = await gitea_client.repository_creation_intent_status(
            repo_name, intent_marker=str(intent["intent_marker"])
        )
        if intent_status == "conflict":
            await postgres_db.conflict_managed_repository_creation_intent(
                str(intent["id"])
            )
            raise ManagedRepositoryAuthorityError("repository_creation_conflict")
        await postgres_db.fail_managed_repository_creation_intent(
            str(intent["id"]), failure_class="forge_create_or_adopt"
        )
        raise ManagedRepositoryAuthorityError("repository_creation_unavailable")
    created = await postgres_db.mark_managed_repository_created(
        str(intent["id"]), intent_marker=str(intent["intent_marker"])
    )
    if created is None:
        raise ManagedRepositoryAuthorityError("repository_creation_raced")
    return str(clean_url), created


async def ensure_managed_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    *,
    repo_name: str,
    authority_kind: str,
    authority_id: str,
    access_mode: str,
    project_id: str | None = None,
    creation_intent_id: str | None = None,
) -> dict[str, Any]:
    """Create/adopt one encrypted write deploy key and prove it before use."""
    repository_owner = str(gitea_client.repository_owner)
    _validate_repository_identity(repository_owner, repo_name)
    if access_mode not in {"read", "write"}:
        raise ManagedRepositoryAuthorityError("repository_access_mode_invalid")
    try:
        UUID(str(authority_id))
        if project_id is not None:
            UUID(str(project_id))
    except (TypeError, ValueError) as exc:
        raise ManagedRepositoryAuthorityError("authority_scope_invalid") from exc

    if not await postgres_db.managed_repository_scope_is_unambiguous(
        repo_name=repo_name,
        authority_kind=authority_kind,
        authority_id=str(authority_id),
        project_id=str(project_id) if project_id else None,
    ):
        raise ManagedRepositoryAuthorityError("repository_scope_ambiguous")

    private_key, public_key, fingerprint = _deploy_keypair()
    try:
        authority = await postgres_db.reserve_managed_repository_authority(
            repository_owner=repository_owner,
            repo_name=repo_name,
            authority_kind=authority_kind,
            authority_id=str(authority_id),
            project_id=str(project_id) if project_id else None,
            access_mode=access_mode,
            creation_intent_id=creation_intent_id,
            clean_repo_url=gitea_client.clean_repo_url(repo_name),
            public_key=public_key,
            public_key_fingerprint=fingerprint,
            private_key=private_key,
        )
    except RuntimeError as exc:
        raise ManagedRepositoryAuthorityError("repository_scope_conflict") from exc
    finally:
        # The durable row owns the encrypted value from this point. Drop the
        # proposed plaintext/key locals immediately; a concurrent replay may
        # have returned an older exact reservation instead.
        del private_key, public_key

    if authority.get("status") == "active":
        return authority

    title = f"srw-managed-{authority['id']}-{access_mode}"
    key_id = await gitea_client.ensure_repo_deploy_key(
        repo_name,
        title=title,
        public_key=authority["public_key"],
        access_mode=access_mode,
    )
    if key_id is None:
        await postgres_db.fail_managed_repository_authority(
            str(authority["id"]), failure_class="deploy_key_registration"
        )
        current = await postgres_db.get_managed_repository_authority(
            repo_name,
            repository_owner=repository_owner,
            include_private_key=True,
        )
        if current is not None and current.get("id") == authority.get("id"):
            return current
        raise ManagedRepositoryAuthorityError("repository_key_unavailable")

    # The forge mutation may have committed even if the later proof request or
    # this process is lost. Persist its exact ID against the immutable authority
    # generation before probing, so cleanup can recover and revoke that key
    # without repository-name inference or minting an untracked replacement.
    recorded = await postgres_db.record_managed_repository_authority_forge_key(
        str(authority["id"]),
        repository_owner=repository_owner,
        repo_name=repo_name,
        authority_kind=authority_kind,
        authority_scope_id=str(authority_id),
        project_id=str(project_id) if project_id else None,
        generation=int(authority["generation"]),
        access_mode=access_mode,
        public_key_fingerprint=str(authority["public_key_fingerprint"]),
        forge_key_id=int(key_id),
    )
    if recorded is None:
        raise ManagedRepositoryAuthorityError("repository_authority_raced")

    proven = await gitea_client.probe_repo_deploy_key(
        repo_name,
        private_key=authority["private_key"],
        access_mode=access_mode,
    )
    if not proven:
        await postgres_db.fail_managed_repository_authority(
            str(authority["id"]), failure_class="deploy_key_probe"
        )
        current = await postgres_db.get_managed_repository_authority(
            repo_name,
            repository_owner=repository_owner,
            include_private_key=True,
        )
        if current is not None and current.get("id") == authority.get("id"):
            return current
        raise ManagedRepositoryAuthorityError("repository_key_unproven")

    activated = await postgres_db.activate_managed_repository_authority(
        str(authority["id"]), forge_key_id=key_id, access_mode=access_mode
    )
    if activated is None:
        raise ManagedRepositoryAuthorityError("repository_authority_raced")
    # Re-read through the only secret-returning database seam.  The activation
    # projection deliberately omits ciphertext/private material.
    active = await postgres_db.get_managed_repository_authority(
        repo_name,
        repository_owner=repository_owner,
        include_private_key=True,
    )
    if active is None:
        raise ManagedRepositoryAuthorityError("repository_authority_unavailable")
    return active


async def _root_job(postgres_db: Any, job: Mapping[str, Any]) -> Mapping[str, Any]:
    current: Mapping[str, Any] = job
    seen: set[str] = set()
    while current.get("parent_job_id"):
        current_id = str(current.get("id") or "")
        if current_id in seen:
            raise ManagedRepositoryAuthorityError("job_lineage_invalid")
        seen.add(current_id)
        parent = await postgres_db.get_job(str(current["parent_job_id"]))
        if not parent:
            raise ManagedRepositoryAuthorityError("job_lineage_unavailable")
        current = parent
    return current


async def ensure_job_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    job: Mapping[str, Any],
    *,
    creation_intent_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a job's canonical managed repository scope and ensure its key."""
    repo_name = str(job.get("repo_name") or "").strip()
    if not repo_name:
        return None
    _validate_repository_identity(str(gitea_client.repository_owner), repo_name)

    project_id = str(job["project_id"]) if job.get("project_id") else None
    if project_id:
        repositories = await postgres_db.get_project_repositories(project_id)
        project_repo = next(
            (
                repo
                for repo in repositories
                if repo.get("is_managed")
                and repo.get("role") == "jobs"
                and str(repo.get("name")) == repo_name
            ),
            None,
        )
        if project_repo is not None:
            authority = await ensure_project_repository_authority(
                postgres_db,
                gitea_client,
                project_repo,
                creation_intent_id=creation_intent_id,
            )
            if authority is None or authority.get("access_mode") != "write":
                raise ManagedRepositoryAuthorityError(
                    "job_repository_requires_write_authority"
                )
            return authority

    root = await _root_job(postgres_db, job)
    root_id = str(root["id"])
    root_repo = str(root.get("repo_name") or "").strip()
    if root_repo != repo_name:
        raise ManagedRepositoryAuthorityError("job_repository_mismatch")
    # Current isolated repos are deterministic.  Historical loose jobs may
    # carry a different private repo name; project-attached unknown names are
    # ambiguous unless a managed project_repositories row proved them above.
    expected = f"job-{root_id[:8]}"
    if project_id and repo_name != expected:
        raise ManagedRepositoryAuthorityError("legacy_repository_ambiguous")
    return await ensure_managed_repository_authority(
        postgres_db,
        gitea_client,
        repo_name=repo_name,
        authority_kind="job",
        authority_id=root_id,
        project_id=(str(root["project_id"]) if root.get("project_id") else project_id),
        access_mode="write",
        creation_intent_id=creation_intent_id,
    )


async def ensure_job_primary_repository_authority(
    postgres_db: Any, gitea_client: Any, job: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Resolve isolated or exact historical shared-jobs primary authority."""

    authority = await ensure_job_repository_authority(postgres_db, gitea_client, job)
    if authority is not None:
        return authority
    root = await _root_job(postgres_db, job)
    project_id = str(root.get("project_id") or "").strip()
    job_project_id = str(job.get("project_id") or "").strip()
    # The previous release's shared-jobs shape is exact: a project job has a
    # branch but no job repo_name/URL and resolves role=jobs at dispatch. A
    # subjob inherits that exact root authority; a cross-project or partially
    # materialized lineage is not permission to select the child's project
    # repository instead.
    if not project_id:
        return None
    if job_project_id != project_id:
        raise ManagedRepositoryAuthorityError("job_repository_project_mismatch")
    if not root.get("branch_name"):
        return None
    repositories = await postgres_db.get_project_repositories(project_id, role="jobs")
    if len(repositories) != 1:
        raise ManagedRepositoryAuthorityError("legacy_jobs_repository_ambiguous")
    repository = repositories[0]
    if not repository.get("is_managed"):
        if repository_url_has_credentials(repository.get("repo_url")):
            raise ManagedRepositoryAuthorityError(
                "credentialed_repository_transport_refused"
            )
        return None
    authority = await ensure_project_repository_authority(
        postgres_db, gitea_client, repository
    )
    if authority is None or authority.get("access_mode") != "write":
        raise ManagedRepositoryAuthorityError("legacy_jobs_repository_unavailable")
    return authority


async def ensure_project_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    repository: Mapping[str, Any],
    *,
    creation_intent_id: str | None = None,
) -> dict[str, Any] | None:
    """Ensure authority for one exact managed project repository row."""

    if not repository.get("is_managed"):
        return None
    access_mode = project_repository_access_mode(repository)
    if access_mode is None:
        return None
    repo_name = str(repository.get("name") or "").strip()
    project_id = str(repository.get("project_id") or "").strip()
    repository_id = str(repository.get("id") or "").strip()
    _validate_repository_identity(str(gitea_client.repository_owner), repo_name)
    try:
        UUID(project_id)
        UUID(repository_id)
    except (TypeError, ValueError) as exc:
        raise ManagedRepositoryAuthorityError("authority_scope_invalid") from exc
    return await ensure_managed_repository_authority(
        postgres_db,
        gitea_client,
        repo_name=repo_name,
        authority_kind="project_repository",
        authority_id=repository_id,
        project_id=project_id,
        access_mode=access_mode,
        creation_intent_id=creation_intent_id,
    )


async def _scrub_job_url_after_proof(
    postgres_db: Any,
    job: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    context = job.get("context") or {}
    if isinstance(context, str):
        import json

        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            context = {}
    raw_observed = context.get("git_remote_url")
    observed = str(raw_observed) if raw_observed else None
    clean = str(authority["clean_repo_url"])
    if observed == clean and not context.get("_managed_repository_authority_pending"):
        return
    updated = await postgres_db.scrub_job_managed_repository_url(
        str(job["id"]),
        repo_name=str(authority["repo_name"]),
        observed_url=observed,
        clean_url=clean,
    )
    if updated:
        return
    current = await postgres_db.get_job(str(job["id"]))
    current_context = (current or {}).get("context") or {}
    if isinstance(current_context, str):
        import json

        try:
            current_context = json.loads(current_context)
        except (TypeError, ValueError):
            current_context = {}
    if (
        not current
        or str(current.get("repo_name") or "") != str(authority["repo_name"])
        or str(current_context.get("git_remote_url") or "") != clean
    ):
        raise ManagedRepositoryAuthorityError("repository_adoption_raced")


async def _scrub_project_url_after_proof(
    postgres_db: Any,
    repository: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    observed = str(repository.get("repo_url") or "")
    clean = str(authority["clean_repo_url"])
    if not observed or observed == clean:
        return
    updated = await postgres_db.scrub_project_managed_repository_url(
        str(repository["id"]),
        repo_name=str(authority["repo_name"]),
        project_id=str(repository["project_id"]),
        observed_url=observed,
        clean_url=clean,
    )
    if updated:
        return
    current = await postgres_db.get_project_repository(str(repository["id"]))
    if (
        not current
        or not current.get("is_managed")
        or str(current.get("name") or "") != str(authority["repo_name"])
        or str(current.get("repo_url") or "") != clean
    ):
        raise ManagedRepositoryAuthorityError("repository_adoption_raced")


async def prepare_project_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    repository: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Prove/adopt one managed project repository before job admission.

    Source/reference attachments are part of a job's workspace contract.  This
    pre-claim seam lets a new replica scrub a historical administrator URL only
    after live deploy-key proof; migration 0176 then prevents an old replica
    from crossing the processing boundary without the same durable authority.
    """

    authority = await ensure_project_repository_authority(
        postgres_db, gitea_client, repository
    )
    if authority is not None:
        await _scrub_project_url_after_proof(postgres_db, repository, authority)
    return authority


async def prepare_job_repository_authority(
    postgres_db: Any, gitea_client: Any, job: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Prove/adopt the primary repository before a processing-state claim.

    Migration 0176 makes active repository authority a prerequisite for the
    ``created|paused -> processing`` transition. New jobs normally have it
    from provisioning; historical rows reach this seam before the claim so a
    new replica can install and prove a scoped key, scrub the legacy URL, and
    only then make the job dispatchable. Old replicas lack this seam and fail
    closed at the database trigger instead of shipping administrator URLs.
    """

    authority = await ensure_job_repository_authority(postgres_db, gitea_client, job)
    if authority is not None:
        await _scrub_job_url_after_proof(postgres_db, job, authority)
    return authority


async def prepare_job_primary_repository_authority(
    postgres_db: Any, gitea_client: Any, job: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Prove and CAS-adopt whichever primary contract the job actually uses."""

    if "repo_name" not in job and job.get("id"):
        # Dispatcher rows are projections; one that omits repo_name would be
        # mistaken for a job without a managed scope and silently dispatched
        # with a credential-less remote. Read the full row before deciding.
        try:
            full = await postgres_db.get_job(str(job["id"]))
        except Exception:
            full = None
        if isinstance(full, Mapping) and full.get("repo_name") is not None:
            job = full
    if job.get("repo_name"):
        return await prepare_job_repository_authority(postgres_db, gitea_client, job)
    authority = await ensure_job_primary_repository_authority(
        postgres_db, gitea_client, job
    )
    if authority is None:
        return None
    repositories = await postgres_db.get_project_repositories(
        str(job["project_id"]), role="jobs"
    )
    if len(repositories) != 1:
        raise ManagedRepositoryAuthorityError("legacy_jobs_repository_ambiguous")
    await _scrub_project_url_after_proof(postgres_db, repositories[0], authority)
    return authority


async def authorize_job_repository_transport(
    postgres_db: Any,
    gitea_client: Any,
    job: Mapping[str, Any],
    repositories: list[dict[str, Any]] | None,
    *,
    backend: str,
) -> tuple[str | None, list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Build the one secret runtime transport for fresh and resume dispatch.

    The returned payload is internal-only. Durable rows are changed solely by
    exact CAS after the deploy key has been registered and proven.
    """

    context = job.get("context") or {}
    if isinstance(context, str):
        import json

        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            context = {}
    credentials: dict[str, ManagedRepositoryRuntimeCredential] = {}
    primary = await prepare_job_primary_repository_authority(
        postgres_db, gitea_client, job
    )
    if primary is not None:
        primary_credential = runtime_credential(primary, backend=backend)
        credentials[primary_credential.authority_id] = primary_credential
        git_remote_url: str | None = primary_credential.clone_url
    else:
        # Preserve a genuinely external repository contract. Managed Gitea
        # jobs are identified by repo_name/project rows and take the scoped
        # path above; this fallback never manufactures Gitea authority.
        git_remote_url = str(context.get("git_remote_url") or "") or None
        if git_remote_url:
            # Nothing on the workspace can authenticate this remote; pushes
            # fail and the workspace becomes the only copy of the work.
            logger.warning(
                "No managed repository authority for job %s; dispatching a "
                "credential-less remote (repo_name=%r)",
                job.get("id"),
                job.get("repo_name"),
            )
        if repository_url_has_credentials(git_remote_url):
            # Historical managed rows without enough durable identity to adopt
            # must not carry their old administrator bearer into a runtime.
            # External repositories use the separate project-repository
            # credential contract rather than this primary URL seam.
            raise ManagedRepositoryAuthorityError(
                "credentialed_repository_transport_refused"
            )

    rendered: list[dict[str, Any]] = []
    for raw_repository in repositories or []:
        repository = dict(raw_repository)
        if repository.get("is_managed"):
            repository["credentials"] = None
            role = str(repository.get("role") or "")
            is_primary_jobs = bool(
                role == "jobs"
                and primary is not None
                and primary.get("authority_kind") == "project_repository"
                and str(primary.get("authority_id")) == str(repository.get("id"))
            )
            should_deliver = role not in {"knowledge", "jobs"}
            if is_primary_jobs:
                should_deliver = True
            if should_deliver:
                authority = (
                    primary
                    if is_primary_jobs
                    else await prepare_project_repository_authority(
                        postgres_db, gitea_client, repository
                    )
                )
                if authority is None:
                    raise ManagedRepositoryAuthorityError(
                        "repository_authority_unavailable"
                    )
                credential = runtime_credential(authority, backend=backend)
                credentials[credential.authority_id] = credential
                repository["repo_url"] = credential.clone_url
            else:
                repository["repo_url"] = gitea_client.clean_repo_url(
                    str(repository["name"])
                )
            if is_primary_jobs:
                git_remote_url = credentials[str(primary["id"])].clone_url
        rendered.append(repository)

    return (
        git_remote_url,
        rendered or None,
        [credential.to_payload() for credential in credentials.values()],
    )


async def ensure_thread_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    thread: Mapping[str, Any],
    *,
    creation_intent_id: str | None = None,
) -> dict[str, Any] | None:
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    workspace = metadata.get("workspace_container") or {}
    repo_name = str(workspace.get("repo_name") or "").strip()
    if not repo_name:
        return None
    thread_id = str(thread["id"])
    if repo_name != f"thread-{thread_id[:8]}":
        raise ManagedRepositoryAuthorityError("thread_repository_mismatch")
    return await ensure_managed_repository_authority(
        postgres_db,
        gitea_client,
        repo_name=repo_name,
        authority_kind="thread",
        authority_id=thread_id,
        project_id=str(thread["project_id"]) if thread.get("project_id") else None,
        access_mode="write",
        creation_intent_id=creation_intent_id,
    )


async def _scrub_thread_url_after_proof(
    postgres_db: Any,
    thread: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    workspace = metadata.get("workspace_container") or {}
    raw_observed = workspace.get("git_remote_url")
    observed = str(raw_observed) if raw_observed else None
    clean = str(authority["clean_repo_url"])
    if observed == clean and not workspace.get("_managed_repository_authority_pending"):
        return
    updated = await postgres_db.scrub_thread_managed_repository_url(
        str(thread["id"]),
        repo_name=str(authority["repo_name"]),
        observed_url=observed,
        clean_url=clean,
    )
    if updated:
        return
    current = await postgres_db.get_thread(str(thread["id"]))
    current_metadata = (current or {}).get("metadata") or {}
    if isinstance(current_metadata, str):
        import json

        try:
            current_metadata = json.loads(current_metadata)
        except (TypeError, ValueError):
            current_metadata = {}
    current_workspace = current_metadata.get("workspace_container") or {}
    if (
        not current
        or str(current_workspace.get("repo_name") or "") != str(authority["repo_name"])
        or str(current_workspace.get("git_remote_url") or "") != clean
    ):
        raise ManagedRepositoryAuthorityError("repository_adoption_raced")


async def prepare_thread_repository_authority(
    postgres_db: Any, gitea_client: Any, thread: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Prove/adopt a persistent repository before binding an agent row."""

    authority = await ensure_thread_repository_authority(
        postgres_db, gitea_client, thread
    )
    if authority is not None:
        await _scrub_thread_url_after_proof(postgres_db, thread, authority)
    return authority


async def authorize_thread_repository_transport(
    postgres_db: Any,
    gitea_client: Any,
    thread: Mapping[str, Any],
    repositories: list[dict[str, Any]] | None,
    *,
    backend: str,
) -> tuple[str | None, list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Build exact internal Git transport for a persistent attach."""

    credentials: dict[str, ManagedRepositoryRuntimeCredential] = {}
    primary = await prepare_thread_repository_authority(
        postgres_db, gitea_client, thread
    )
    if primary is not None:
        primary_credential = runtime_credential(primary, backend=backend)
        credentials[primary_credential.authority_id] = primary_credential
        git_remote_url: str | None = primary_credential.clone_url
    else:
        git_remote_url = None

    rendered: list[dict[str, Any]] = []
    for raw_repository in repositories or []:
        repository = dict(raw_repository)
        if repository.get("is_managed"):
            # Persistent sessions currently do not clone project repository
            # attachments. Keep their established metadata contract, but do
            # not grant unused object-plane authority (especially the server-
            # side knowledge vault) to a session runtime.
            repository["repo_url"] = gitea_client.clean_repo_url(
                str(repository["name"])
            )
            repository["credentials"] = None
        rendered.append(repository)
    return (
        git_remote_url,
        rendered or None,
        [credential.to_payload() for credential in credentials.values()],
    )


def _runtime_ssh_endpoint(*, backend: str) -> tuple[str, int]:
    # A same-cluster VM is on the pod network and reaches the internal SSH
    # service exactly like a workspace container; only a tailnet VM needs the
    # externally routed endpoint (which may not exist on that cluster at all).
    if backend == "vm" and not vm_workspaces_on_pod_network():
        host = os.environ.get("GITEA_SSH_EXTERNAL_HOST", "").strip()
        port_raw = os.environ.get("GITEA_SSH_EXTERNAL_PORT", "22")
    else:
        host = os.environ.get("GITEA_SSH_INTERNAL_HOST", "").strip()
        if not host:
            parsed = urlparse(os.environ.get("GITEA_INTERNAL_URL", ""))
            host = parsed.hostname or ""
        port_raw = os.environ.get("GITEA_SSH_INTERNAL_PORT", "2222")
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ManagedRepositoryAuthorityError(
            "repository_ssh_endpoint_invalid"
        ) from exc
    if not host or not 1 <= port <= 65535:
        raise ManagedRepositoryAuthorityError("repository_ssh_endpoint_unavailable")
    return host, port


def runtime_credential(
    authority: Mapping[str, Any], *, backend: str
) -> ManagedRepositoryRuntimeCredential:
    """Bind one active durable authority to a clean runtime SSH alias."""
    if authority.get("status") != "active" or not authority.get("private_key"):
        raise ManagedRepositoryAuthorityError("repository_authority_inactive")
    access_mode = str(authority.get("access_mode") or "")
    if access_mode not in {"read", "write"}:
        raise ManagedRepositoryAuthorityError("repository_access_mode_invalid")
    host, port = _runtime_ssh_endpoint(backend=backend)
    authority_id = str(authority["id"])
    alias = f"srw-repo-{authority_id.replace('-', '')}"
    owner = str(authority["repository_owner"])
    repo_name = str(authority["repo_name"])
    return ManagedRepositoryRuntimeCredential(
        authority_id=authority_id,
        generation=int(authority["generation"]),
        access_mode=access_mode,
        repo_name=repo_name,
        repository_owner=owner,
        clone_url=f"ssh://{alias}/{owner}/{repo_name}.git",
        alias=alias,
        ssh_host=host,
        ssh_port=port,
        private_key=str(authority["private_key"]),
        public_key_fingerprint=str(authority["public_key_fingerprint"]),
    )


async def revoke_and_delete_managed_repository(
    postgres_db: Any, gitea_client: Any, repo_name: str
) -> bool:
    """Make the scoped key unusable before/with repository deletion.

    A repository deletion itself invalidates every deploy key. If deletion
    fails, an independently successful key DELETE still contains the leaked
    workspace credential. Only after one of those authoritative outcomes do
    we close the local audit generation and return ``True``. With no durable
    authority row (a genuinely legacy repository), deletion is the only proof
    of containment.
    """
    owner = str(gitea_client.repository_owner)
    claim = await postgres_db.claim_managed_repository_authority_revoke(
        repo_name, repository_owner=str(gitea_client.repository_owner)
    )
    creation = await postgres_db.claim_managed_repository_creation_cleanup(
        repo_name, repository_owner=owner
    )
    creation_already_contained = bool(
        creation is not None and creation.get("status") in {"deleted", "conflicted"}
    )
    key_revoked = False
    if claim is not None and claim.get("forge_key_id") is not None:
        key_revoked = await gitea_client.delete_repo_deploy_key(
            repo_name, int(claim["forge_key_id"])
        )
    repo_deleted = False
    if not creation_already_contained:
        repo_deleted = await gitea_client.delete_repo(
            repo_name,
            intent_marker=(str(creation["intent_marker"]) if creation else None),
        )
    authority_contained = claim is None or bool(repo_deleted or key_revoked)
    creation_contained = (
        creation is None or creation_already_contained or bool(repo_deleted)
    )
    if claim is not None and authority_contained:
        await postgres_db.finish_managed_repository_authority_revoke(str(claim["id"]))
    if creation is not None and repo_deleted and not creation_already_contained:
        await postgres_db.finish_managed_repository_creation_cleanup(
            str(creation["id"])
        )
    if claim is None and creation is None:
        # A genuine pre-0176 repository has no local key/intent ledger. The
        # repository deletion itself is the only containment proof.
        return bool(repo_deleted)
    return bool(authority_contained and creation_contained)


async def revoke_managed_repository_authority(
    postgres_db: Any, gitea_client: Any, repo_name: str
) -> bool:
    """Revoke the live deploy key without deleting the repository."""

    claim = await postgres_db.claim_managed_repository_authority_revoke(
        repo_name, repository_owner=str(gitea_client.repository_owner)
    )
    if claim is None:
        return True
    key_id = claim.get("forge_key_id")
    if key_id is None:
        return False
    revoked = await gitea_client.delete_repo_deploy_key(repo_name, int(key_id))
    if not revoked:
        return False
    return await postgres_db.finish_managed_repository_authority_revoke(
        str(claim["id"])
    )


async def rotate_project_repository_authority(
    postgres_db: Any,
    gitea_client: Any,
    repository: Mapping[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Revoke first, then mint the exact requested project-repo mode."""

    desired_mode = project_repository_access_mode(repository)
    repo_name = str(repository.get("name") or "")
    current = await postgres_db.get_managed_repository_authority(
        repo_name,
        repository_owner=str(gitea_client.repository_owner),
        include_private_key=False,
    )
    if current is not None and (
        force or str(current.get("access_mode")) != str(desired_mode)
    ):
        if not await revoke_managed_repository_authority(
            postgres_db, gitea_client, repo_name
        ):
            raise ManagedRepositoryAuthorityError(
                "repository_authority_revocation_failed"
            )
        current = None
    if desired_mode is None:
        return None
    if current is not None:
        return current
    return await ensure_project_repository_authority(
        postgres_db, gitea_client, repository
    )


__all__ = [
    "ManagedRepositoryAuthorityError",
    "ManagedRepositoryRuntimeCredential",
    "authorize_job_repository_transport",
    "authorize_thread_repository_transport",
    "create_managed_repository",
    "ensure_job_repository_authority",
    "ensure_job_primary_repository_authority",
    "ensure_managed_repository_authority",
    "ensure_project_repository_authority",
    "ensure_thread_repository_authority",
    "prepare_job_repository_authority",
    "prepare_job_primary_repository_authority",
    "prepare_project_repository_authority",
    "prepare_thread_repository_authority",
    "repository_url_has_credentials",
    "project_repository_access_mode",
    "revoke_and_delete_managed_repository",
    "revoke_managed_repository_authority",
    "rotate_project_repository_authority",
    "runtime_credential",
]
