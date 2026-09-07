"""Pure raw-ingress sanitation shared by job validation and admission.

These fields can only be minted after authoritative repository/Officer/workspace
resolution. Transport authentication does not make submitted JSON authoritative.
Application state and public/internal caller policy remain with admission.
"""

from typing import Any

from shared.workspace_contract import (
    WORKSPACE_CONTRACT_CONTEXT_KEY,
    WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY,
    WORKSPACE_RUNTIME_CONTEXT_KEY,
)


_SERVER_OWNED_OFFICER_CONTEXT_KEYS = {
    "ticket_note_id",
    "officer_admission",
    "ticket_ready_at",
    "ready_generation_at",
    "ticket_claim_source",
    "claim_source",
    "officer_thread_id",
    "officer_incarnation",
    "provisioning_preflight",
}
_SERVER_OWNED_REPOSITORY_CONTEXT_KEYS = {
    "git_remote_url",
    "repo_name",
    "managed_repository_credentials",
    "managed_repository_authority",
    "repository_auth",
    "repository_credentials",
    "_managed_repository_authority_pending",
    "_managed_repository_process_zero",
    "_stateless_workspace_process_zero_observation",
}
_SERVER_OWNED_RAW_CREATE_CONTEXT_KEYS = (
    _SERVER_OWNED_OFFICER_CONTEXT_KEYS
    | _SERVER_OWNED_REPOSITORY_CONTEXT_KEYS
    | {
        "evidence_manifest",
        "pull_request",
        "deliverable_contract_provenance",
        "prior_deliverable_contract",
        "required_pr_repositories",
        "required_deliverables",
        WORKSPACE_CONTRACT_CONTEXT_KEY,
        WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY,
        WORKSPACE_RUNTIME_CONTEXT_KEY,
        "workspace_backend",
        "vm",
        "workspace_container",
    }
)


def _strip_raw_repository_authority(value: Any) -> Any:
    """Recursively remove server-owned Git transport from request JSON."""

    if isinstance(value, dict):
        return {
            key: _strip_raw_repository_authority(item)
            for key, item in value.items()
            if key not in _SERVER_OWNED_REPOSITORY_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_strip_raw_repository_authority(item) for item in value]
    return value
