"""Bounded migration of an existing bundled expert/job into manifest preview.

Internal only: the caller supplies already-selected workspace/connectors and the
same policy filter it uses for legacy resolution. This grants no authority and is
not wired to a public admission route. Unmapped job fields fail explicitly.
"""

from collections.abc import Callable
from copy import deepcopy
import re

from orchestrator.schemas.job_create import JobCreate
from orchestrator.security.access import redact_config_override
from orchestrator.services.config_resolver import resolve_config
from shared.manifests import API_VERSION, preview_documents
from shared.manifests.errors import fail


def preview_legacy_job(
    job: JobCreate,
    *,
    name: str,
    scope: dict,
    image: str,
    workspace: dict | None,
    connectors: dict,
    grant_strip: Callable[[dict], dict],
    base_defaults: dict | None = None,
    project_overrides: dict | None = None,
    db_overrides: dict | None = None,
    user_settings: dict | None = None,
    definitions: list[dict] | None = None,
    timeout_seconds: int = 3600,
) -> dict:
    """Translate the supported pre-dispatch legacy case without provisioning.

    The private JSON payload remains a serialized SRW resolved config, including
    prompts/instructions; the reference harness can hydrate it using its existing
    loader. An image entrypoint change or config-file consumption is NOT enabled
    here. Existing jobs keep their normal dispatch/completion implementation.
    """
    unmapped = job.model_fields_set - {"description", "config_name", "config_override"}
    if unmapped:
        fail(
            "UnsupportedLegacyInput",
            "This migration slice supports only description, config_name and config_override; other job inputs need an explicit mapping.",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job.config_name):
        fail(
            "UnsupportedLegacyConfig",
            "Select a bundled configuration name, not a filesystem path.",
        )
    if not callable(grant_strip):
        fail(
            "MissingLegacyPolicy",
            "Supply the existing legacy configuration policy filter.",
        )
    blob = resolve_config(
        base_config_name=job.config_name,
        request_override=deepcopy(job.config_override),
        expert_type="worker",
        grant_strip=grant_strip,
        base_defaults=deepcopy(base_defaults),
        project_overrides=deepcopy(project_overrides),
        db_overrides=deepcopy(db_overrides),
        user_settings=deepcopy(user_settings),
    )
    legacy_workspace = blob["agent"].get("workspace", {})
    if legacy_workspace.get("remote") or legacy_workspace.get("mounts"):
        fail(
            "UnsupportedLegacyDelivery",
            "Live workspace transport/mount delivery must be translated separately; supply a pre-dispatch configuration.",
        )
    config = redact_config_override(blob)
    metadata = {"name": name, "scope": deepcopy(scope)}
    expert = {
        "apiVersion": API_VERSION,
        "kind": "Expert",
        "metadata": deepcopy(metadata),
        "spec": {"runtime": {"image": image, "config": config}},
    }
    assignment = {
        "apiVersion": API_VERSION,
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "task": {"text": job.description},
            "execution": {
                "expert": {"ref": {"name": name}},
                "workspace": deepcopy(workspace),
                "connectors": deepcopy(connectors),
            },
            "completion": {"mode": "Reported"},
            "retry": {"maxAttempts": 1},
            "timeoutSeconds": timeout_seconds,
        },
    }
    result = preview_documents([*(definitions or []), expert, assignment])
    resolved_workspace = result["resolved"][-1]["spec"]["execution"]["workspace"]
    old_backend = legacy_workspace.get("backend", "sandbox")
    new_backend = (
        "none"
        if resolved_workspace is None
        else resolved_workspace.get("template", {}).get("inline", {}).get("backend")
    )
    if new_backend is not None and new_backend != old_backend:
        fail(
            "LegacyWorkspaceMismatch",
            "The selected workspace backend differs from the effective legacy configuration.",
        )
    result["legacy"] = {
        "configName": job.config_name,
        "policyFilterApplied": True,
        "privateConfigFormat": "srw-resolved-config",
        "executionEnabled": False,
    }
    return result
