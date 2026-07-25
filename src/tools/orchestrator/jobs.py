"""Orchestrator job management tools for persistent agents.

These tools call the orchestrator REST API to create, monitor, and manage
worker jobs. They enable the persistent agent to delegate heavy work to
the autonomous worker pool.

The orchestrator URL is read from the ORCHESTRATOR_URL environment variable
(same as the worker's orchestrator_client.py).
"""

import json
import logging
import os
from uuid import UUID
import uuid
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

ORCHESTRATOR_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "get_session_context": {
        "module": "orchestrator.jobs",
        "function": "get_session_context",
        "description": (
            "Summarize the current persistent session context: thread ID, user "
            "ID, project scope, workspace availability, backend capabilities, "
            "cloud mount status, knowledge/connector availability, the chat "
            "models this deployment routes, and the caller's effective grants."
        ),
        "category": "orchestrator",
        "short_description": "Show current session/project/workspace context.",
        "phases": ["strategic", "tactical"],
    },
    "create_worker_job": {
        "module": "orchestrator.jobs",
        "function": "create_worker_job",
        "description": (
            "Create a new worker job on the orchestrator. A worker agent will "
            "pick it up and execute it autonomously. Returns the job ID for "
            "monitoring. Use config_name to select an expert (developer, scholar, "
            "critic) or 'worker_base' for the framework fallback, expert_id for a "
            "DB-backed expert, and config_override to pin the job's model or "
            "workspace backend."
        ),
        "category": "orchestrator",
        "short_description": "Delegate work to a worker agent via the orchestrator.",
        "phases": ["strategic", "tactical"],
    },
    "list_worker_jobs": {
        "module": "orchestrator.jobs",
        "function": "list_worker_jobs",
        "description": (
            "List jobs on the orchestrator. Filter by status to find active, "
            "completed, or failed jobs. Returns job IDs, descriptions, statuses, "
            "and assigned agents."
        ),
        "category": "orchestrator",
        "short_description": "List jobs with optional status filter.",
        "phases": ["strategic", "tactical"],
    },
    "get_worker_job": {
        "module": "orchestrator.jobs",
        "function": "get_worker_job",
        "description": (
            "Get detailed status of a specific job including progress, current "
            "phase, assigned agent, and any error messages."
        ),
        "category": "orchestrator",
        "short_description": "Get job details and progress.",
        "phases": ["strategic", "tactical"],
    },
    "get_job_workspace_file": {
        "module": "orchestrator.jobs",
        "function": "get_job_workspace_file",
        "description": (
            "Read a file from a worker job's workspace. Use this to inspect "
            "the worker's output, plan, or workspace notes. Common files: "
            "plan.md, notes/, output/*.md"
        ),
        "category": "orchestrator",
        "short_description": "Read a file from a job's workspace.",
        "phases": ["strategic", "tactical"],
    },
    "approve_worker_job": {
        "module": "orchestrator.jobs",
        "function": "approve_worker_job",
        "description": (
            "Approve a frozen job that is pending review. This marks the job "
            "as completed. Use after reviewing the job's deliverables."
        ),
        "category": "orchestrator",
        "short_description": "Approve a frozen job.",
        "phases": ["strategic", "tactical"],
    },
    "resume_worker_job": {
        "module": "orchestrator.jobs",
        "function": "resume_worker_job",
        "description": (
            "Resume a paused or frozen job with optional feedback. The worker "
            "will incorporate your feedback when it resumes execution."
        ),
        "category": "orchestrator",
        "short_description": "Resume a job with optional feedback.",
        "phases": ["strategic", "tactical"],
    },
    "cancel_worker_job": {
        "module": "orchestrator.jobs",
        "function": "cancel_worker_job",
        "description": "Cancel a running or paused job.",
        "category": "orchestrator",
        "short_description": "Cancel a job.",
        "phases": ["strategic", "tactical"],
    },
    "pause_worker_job": {
        "module": "orchestrator.jobs",
        "function": "pause_worker_job",
        "description": (
            "Pause a running job. The worker will stop at the next safe point. "
            "Use resume_worker_job to continue."
        ),
        "category": "orchestrator",
        "short_description": "Pause a running job.",
        "phases": ["strategic", "tactical"],
    },
}


def _get_orchestrator_url() -> str:
    """Get orchestrator base URL from environment."""
    url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    return url.rstrip("/")


def _get_client(*, user_id: Optional[str] = None) -> httpx.AsyncClient:
    """Create an httpx client for orchestrator calls.

    Attaches ``X-Internal-Key`` when ``MCP_INTERNAL_KEY`` is set so the
    orchestrator's Track B (P4b) gates accept agent-tool calls. When
    ``user_id`` is supplied the client also sends ``X-MCP-User-Id`` so
    the orchestrator's ``_get_user_from_mcp_headers`` path can resolve
    the originating user — required by ``GET /api/jobs``,
    ``GET /api/jobs/{id}`` and any other endpoint guarded by
    ``require_approved_user`` / ``require_job_access``. Without the
    user header those endpoints 401 even with a valid internal key.

    Worker-mode callers (no session, no user identity) pass
    ``user_id=None`` and continue to authenticate as anonymous internal
    against the dual-callable / require_internal endpoints.
    """
    headers: dict[str, str] = {}
    internal_key = os.getenv("MCP_INTERNAL_KEY", "")
    if internal_key:
        headers["X-Internal-Key"] = internal_key
    if user_id:
        headers["X-MCP-User-Id"] = user_id
    return httpx.AsyncClient(timeout=30.0, headers=headers)


# Credential/transport keys an agent may never set on a job it creates. The
# capability PDP (src/core/capability_grants.py) gates the escalation axes —
# vm backend, shell, delegation, connectors, model allowlist, autonomy and
# permission ceilings — so a raw config_override cannot exceed its owner. It
# does NOT gate transport: redact_config_override deliberately preserves
# base_url, and dispatch only overwrites it when the pinned model resolves to a
# known endpoint. Left open, a prompt-injected session could point a spawned
# job's LLM at an arbitrary host. Mirrors the key semantics of
# orchestrator.security.access._is_secret_key, redeclared here because the
# agent image does not ship the orchestrator package.
_TRANSPORT_DENY = frozenset({"api_key", "base_url", "env_keys"})
_TRANSPORT_DENY_SUFFIX = "_api_key"


def _is_transport_key(key: str) -> bool:
    k = str(key).lower()
    return k in _TRANSPORT_DENY or k.endswith(_TRANSPORT_DENY_SUFFIX)


def _transport_key_paths(value: Any, prefix: str = "") -> List[str]:
    """Dotted paths of every transport/credential key inside a config_override.

    Recursive over dicts and lists. Returns [] for a clean override. Callers
    REJECT on a non-empty result rather than stripping — silently dropping the
    key would leave the agent believing its override applied.
    """
    found: List[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if _is_transport_key(k):
                found.append(path)
                continue
            found.extend(_transport_key_paths(v, path))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.extend(_transport_key_paths(item, f"{prefix}[{i}]"))
    return found


def _short_id(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "unknown"


def _truncate(value: Any, *, limit: int = 140) -> str:
    text = str(value or "").strip()
    if not text:
        return "N/A"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_dict_value(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _format_freeze_data(job: Dict[str, Any]) -> List[str]:
    freeze_data = job.get("freeze_data")
    if not isinstance(freeze_data, dict):
        context = job.get("context")
        if isinstance(context, dict):
            candidate = context.get("freeze_data")
            if isinstance(candidate, dict):
                freeze_data = candidate
    if not isinstance(freeze_data, dict):
        return []

    lines: List[str] = []
    freeze_type = _compact_dict_value(freeze_data, "freeze_type", "type")
    reason = _compact_dict_value(
        freeze_data,
        "reason",
        "message",
        "status_message",
        "review_reason",
        "pause_reason",
    )
    if freeze_type:
        lines.append(f"Freeze type: {freeze_type}")
    if reason:
        lines.append(f"Freeze reason: {_truncate(reason, limit=240)}")
    if freeze_data.get("requires_review") is not None:
        lines.append(f"Requires review: {freeze_data.get('requires_review')}")
    return lines


def _format_job_summary(job: Dict[str, Any]) -> str:
    """Format a job dict into a readable summary."""
    job_id = job.get("id", "unknown")
    lines = [
        f"Job ID: {job_id}",
        f"Short ID: {_short_id(job_id)}",
        f"Status: {job.get('status', 'unknown')}",
        f"Description: {_truncate(job.get('description'), limit=300)}",
    ]
    if job.get("config_name"):
        lines.append(f"Config: {job['config_name']}")
    if job.get("project_id"):
        lines.append(f"Project ID: {job['project_id']}")
    if job.get("user_id"):
        lines.append(f"Owner user ID: {job['user_id']}")
    if job.get("parent_job_id"):
        lines.append(f"Parent job ID: {job['parent_job_id']}")
    if job.get("priority") is not None:
        lines.append(f"Priority: {job['priority']}")
    if job.get("assigned_agent_id"):
        lines.append(f"Agent: {job['assigned_agent_id']}")
    if job.get("created_at"):
        lines.append(f"Created: {job['created_at']}")
    if job.get("updated_at"):
        lines.append(f"Updated: {job['updated_at']}")
    if job.get("repo_name"):
        lines.append(f"Repo: {job['repo_name']}")
    if job.get("branch_name"):
        lines.append(f"Branch: {job['branch_name']}")
    if job.get("current_phase"):
        lines.append(f"Current phase: {job['current_phase']}")
    if job.get("progress") is not None:
        lines.append(f"Progress: {job['progress']}")
    lines.extend(_format_freeze_data(job))
    if job.get("error_message"):
        lines.append(f"Error: {_truncate(job['error_message'], limit=300)}")
    return "\n".join(lines)


def _clean_job_id(job_id: str) -> str:
    cleaned = str(job_id or "").strip()
    while cleaned.endswith("...") or cleaned.endswith("\u2026"):
        cleaned = cleaned[:-3] if cleaned.endswith("...") else cleaned[:-1]
        cleaned = cleaned.strip()
    return cleaned


def _is_full_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (TypeError, ValueError):
        return False


async def _resolve_job_id(
    client: httpx.AsyncClient,
    base_url: str,
    job_id: str,
) -> str:
    """Resolve visible UUID prefixes while keeping full IDs as the contract."""
    cleaned = _clean_job_id(job_id)
    if not cleaned:
        return cleaned
    if _is_full_uuid(cleaned) or len(cleaned) < 8:
        return cleaned

    resp = await client.get(f"{base_url}/api/jobs", params={"limit": 500})
    resp.raise_for_status()
    data = resp.json()
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    matches = [
        str(job.get("id"))
        for job in jobs
        if job.get("id") and str(job.get("id")).startswith(cleaned)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sample = ", ".join(matches[:5])
        raise ValueError(
            f"Job ID prefix '{cleaned}' is ambiguous; matches include: {sample}"
        )
    return cleaned


def _format_job_list_item(job: Dict[str, Any]) -> List[str]:
    job_id = job.get("id", "unknown")
    lines = [
        f"--- {job_id} (short: {_short_id(job_id)}) ---",
        f"  Status: {job.get('status', '?')}",
        f"  Description: {_truncate(job.get('description'), limit=140)}",
    ]
    if job.get("config_name"):
        lines.append(f"  Config: {job['config_name']}")
    if job.get("project_id"):
        lines.append(f"  Project ID: {job['project_id']}")
    if job.get("parent_job_id"):
        lines.append(f"  Parent job ID: {job['parent_job_id']}")
    if job.get("assigned_agent_id"):
        lines.append(f"  Agent: {job['assigned_agent_id']}")
    if job.get("updated_at"):
        lines.append(f"  Updated: {job['updated_at']}")
    elif job.get("created_at"):
        lines.append(f"  Created: {job['created_at']}")
    for freeze_line in _format_freeze_data(job):
        lines.append(f"  {freeze_line}")
    if job.get("error_message"):
        lines.append(f"  Error: {_truncate(job['error_message'], limit=180)}")
    return lines


def _format_grants(capabilities: Dict[str, Any]) -> List[str]:
    """Render the caller's effective grants for the session-context report.

    Shape comes from ``GET /api/users/me/capabilities``: admins get
    ``grants: None`` (unrestricted). Only the capabilities that decide whether a
    ``create_worker_job`` override will be accepted are surfaced — the full
    catalog would bloat the tool result for no decision value.
    """
    if capabilities.get("is_admin"):
        return ["  Grants: admin (unrestricted)"]
    grants = capabilities.get("grants")
    if not isinstance(grants, dict):
        return []
    lines = ["  Grants:"]
    for key in ("vm_workspace", "shell_tools", "delegation", "datasource_tools"):
        if key in grants:
            lines.append(f"    {key}: {grants[key]}")
    allowed_models = grants.get("model_selection")
    if allowed_models is not None:
        lines.append(
            f"    model_selection: {', '.join(str(m) for m in allowed_models)}"
        )
    return lines


def _format_session_context(
    context: ToolContext,
    *,
    chat_models: Optional[List[str]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
) -> str:
    workspace = context.workspace_manager
    backend = getattr(workspace, "backend", None) if workspace else None
    cloud_mount = context.config.get("cloud_mount") or {}
    datasource_keys = sorted(context.datasources.keys())

    lines = [
        "Session context:",
        f"  Thread ID: {context.thread_id or 'none'}",
        f"  User ID: {context.user_id or 'none'}",
        f"  Primary project ID: {context.project_id or 'none'}",
        f"  Project IDs: {', '.join(context.project_ids) if context.project_ids else 'none'}",
        f"  Job/context ID: {context.job_id or 'none'}",
        f"  Workspace available: {bool(workspace)}",
    ]
    if workspace:
        lines.extend(
            [
                f"  Workspace path: {workspace.path}",
                f"  Workspace backend: {type(backend).__name__ if backend else 'unknown'}",
                f"  Supports shell: {bool(getattr(backend, 'supports_shell', False))}",
                f"  Git available: {context.has_git()}",
            ]
        )
    lines.extend(
        [
            f"  Shell manager available: {context.has_shell()}",
            f"  Knowledge available: {context.has_knowledge()}",
            f"  Connectors: {', '.join(datasource_keys) if datasource_keys else 'none'}",
            f"  Cloud mount active: {bool(cloud_mount.get('active'))}",
        ]
    )
    if cloud_mount.get("active"):
        lines.append(
            f"  Cloud workspace entry: {cloud_mount.get('workspace_entry', '/workspace/cloud')}"
        )
    # Everything a create_worker_job config_override needs to be written without
    # guessing: the model IDs this deployment actually routes, and the grants
    # that decide whether an override is accepted. Both are omitted (not faked)
    # when the lookup failed — see get_session_context.
    if chat_models:
        lines.append(f"  Available chat models: {', '.join(chat_models)}")
    if capabilities:
        lines.extend(_format_grants(capabilities))
    return "\n".join(lines)


def create_orchestrator_tools(context: ToolContext) -> List[Any]:
    """Create all orchestrator tools with injected context."""
    base_url = _get_orchestrator_url()

    @tool
    async def get_session_context() -> str:
        """Show the current session, project, workspace, and capability context.

        Use this before project/job/repository actions when you need to know
        which thread, user, project, and workspace backend this session is using.
        Also reports the chat models this deployment routes and your effective
        grants — read both before pinning a model or workspace backend in a
        create_worker_job config_override.
        """
        # Fail-soft: a context report must never break the session, so a models
        # or capabilities lookup that errors simply omits its line.
        chat_models: Optional[List[str]] = None
        capabilities: Optional[Dict[str, Any]] = None
        try:
            async with _get_client(user_id=context.user_id) as client:
                try:
                    resp = await client.get(f"{base_url}/api/models")
                    resp.raise_for_status()
                    groups = resp.json().get("groups") or []
                    models = [
                        str(model_id)
                        for group in groups
                        for model_id in (group.get("models") or [])
                    ]
                    chat_models = sorted(dict.fromkeys(models)) or None
                except Exception as e:
                    logger.debug("get_session_context: model lookup failed: %s", e)
                try:
                    resp = await client.get(f"{base_url}/api/users/me/capabilities")
                    resp.raise_for_status()
                    payload = resp.json()
                    if isinstance(payload, dict):
                        capabilities = payload
                except Exception as e:
                    logger.debug("get_session_context: capability lookup failed: %s", e)
        except Exception as e:
            logger.debug("get_session_context: orchestrator unreachable: %s", e)

        return _format_session_context(
            context, chat_models=chat_models, capabilities=capabilities
        )

    @tool
    async def create_worker_job(
        description: str,
        config_name: str = "worker_base",
        instructions: Optional[str] = None,
        priority: int = 5,
        project_id: Optional[str] = None,
        datasource_ids: Optional[List[str]] = None,
        config_override: Optional[Dict[str, Any]] = None,
        expert_id: Optional[str] = None,
    ) -> str:
        """Create a new worker job on the orchestrator.

        Args:
            description: What the worker should accomplish
            config_name: Expert config to use (worker_base, developer, scholar, critic)
            instructions: Additional instructions for the worker
            priority: Job priority 1-10, higher = more urgent (default: 5)
            project_id: Optional project to scope the job to
            datasource_ids: Optional explicit datasource selection. Omit to
                inherit this session's datasources; pass [] to attach none.
            config_override: Per-job config as JSON, merged last so it wins over
                project and expert defaults. Common knobs:
                {"llm": {"model": "<id>"}} to pin the worker's model, and
                {"workspace": {"backend": "vm"}} for a root VM instead of the
                default sandbox. Call get_session_context for valid model IDs
                and your grants; use_skill("delegate-a-job") for the full
                recipe. Credential/transport keys (api_key, base_url,
                env_keys) are rejected.
            expert_id: DB-backed expert UUID from list_experts. Carries its own
                model, backend, and prompts. Cannot be combined with a
                config_name other than worker_base.

        Returns:
            Job creation result with job ID
        """
        # Reject transport/credential keys before any HTTP: the PDP gates
        # capability escalation but not where the model is routed.
        offending = _transport_key_paths(config_override)
        if offending:
            return (
                "Refusing to create job: config_override may not set credential "
                f"or transport keys ({', '.join(sorted(offending))}). Routing is "
                "resolved server-side from the model ID — pass "
                '{"llm": {"model": "<id>"}} and drop these keys.'
            )
        # The API rejects this pairing with a 400 (one expert source at a time);
        # catch it here so the agent gets the reason instead of a bare status.
        if expert_id and config_name and config_name != "worker_base":
            return (
                f"Refusing to create job: expert_id cannot be combined with "
                f"config_name={config_name!r}. Pass expert_id alone (it selects "
                "a DB expert) or config_name alone (a bundled one)."
            )

        payload: Dict[str, Any] = {
            "description": description,
            "config_name": config_name,
            "priority": priority,
        }
        if instructions:
            payload["instructions"] = instructions
        if project_id:
            payload["project_id"] = project_id
        if config_override:
            payload["config_override"] = config_override
        if expert_id:
            payload["expert_id"] = expert_id
        # Explicit selection overrides inheritance; [] means "attach none".
        # Omitting it lets the orchestrator inherit the parent session/job's
        # selection (server-side, keyed off thread_id below).
        if datasource_ids is not None:
            payload["datasource_ids"] = datasource_ids
        # When invoked from a persistent session, carry the thread back so
        # the orchestrator can derive the owning user (and apply their model
        # preferences during dispatch). No-op for worker-mode callers.
        if context.thread_id:
            payload["thread_id"] = context.thread_id
        # Worker-mode callers must bind child identity/scope to the job they are
        # currently executing. X-Internal-Key/X-MCP-User-Id authenticate the
        # transport/user but do not prove that a model-selected project or
        # datasource belongs to this worker's job. Persistent sessions use the
        # thread_id above; contexts that legitimately carry both send both and
        # the orchestrator requires their scopes to agree.
        parent_job_id = context._job_metadata.get("job_id")
        try:
            if parent_job_id:
                payload["parent_job_id"] = str(UUID(str(parent_job_id)))
        except (ValueError, TypeError, AttributeError):
            pass
        if not project_id and context.project_id:
            payload["project_id"] = context.project_id

        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.post(f"{base_url}/api/jobs", json=payload)
                resp.raise_for_status()
                data = resp.json()
                job_id = data.get("id") or data.get("job_id", "unknown")
                lines = [
                    "Job created successfully.",
                    f"Job ID: {job_id}",
                    f"Config: {config_name}",
                ]
                if expert_id:
                    lines.append(f"Expert: {expert_id}")
                if config_override:
                    lines.append(f"Overrides: {json.dumps(config_override)}")
                lines.extend(
                    [
                        f"Priority: {priority}",
                        f"Description: {description}",
                        "",
                        "A worker agent will pick this up from the dispatch "
                        f"queue. Use get_worker_job('{job_id}') to check progress.",
                    ]
                )
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to create job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_worker_jobs(
        status: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """List jobs on the orchestrator.

        Args:
            status: Filter by status (created, processing, completed, failed, cancelled, paused, pending_review)
            limit: Maximum jobs to return (default: 20)

        Returns:
            Formatted list of jobs
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status

        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(f"{base_url}/api/jobs", params=params)
                resp.raise_for_status()
                data = resp.json()
                jobs = data if isinstance(data, list) else data.get("jobs", [])

                if not jobs:
                    filter_msg = f" with status='{status}'" if status else ""
                    return f"No jobs found{filter_msg}."

                lines = [f"Found {len(jobs)} job(s):\n"]
                for job in jobs:
                    lines.extend(_format_job_list_item(job))
                    lines.append("")

                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list jobs: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_worker_job(job_id: str) -> str:
        """Get detailed status of a worker job.

        Args:
            job_id: The job UUID

        Returns:
            Job details including status, progress, and any errors
        """
        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.get(f"{base_url}/api/jobs/{resolved_job_id}")
                resp.raise_for_status()
                job = resp.json()
                return _format_job_summary(job)
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Job '{job_id}' not found."
                return f"Failed to get job: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_job_workspace_file(job_id: str, path: str) -> str:
        """Read a file from a worker job's workspace.

        Args:
            job_id: The job UUID
            path: Relative file path (e.g., plan.md, notes/decisions.md, output/result.md)

        Returns:
            File contents or error message
        """
        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.get(
                    f"{base_url}/api/jobs/{resolved_job_id}/workspace/file",
                    params={"path": path},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("content", "")
                if not content:
                    return f"File '{path}' is empty or not found."
                return f"=== {path} ===\n{content}"
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"File '{path}' not found in job {job_id}."
                return f"Failed to read file: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def approve_worker_job(job_id: str) -> str:
        """Approve a frozen job that is pending review.

        Args:
            job_id: The job UUID to approve

        Returns:
            Approval result
        """
        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.post(
                    f"{base_url}/api/jobs/{resolved_job_id}/approve"
                )
                resp.raise_for_status()
                return f"Job {resolved_job_id} approved and marked as completed."
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                return f"Failed to approve job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def resume_worker_job(
        job_id: str,
        feedback: Optional[str] = None,
    ) -> str:
        """Resume a paused or frozen job with optional feedback.

        Args:
            job_id: The job UUID to resume
            feedback: Optional feedback message for the worker to incorporate

        Returns:
            Resume result
        """
        payload: Dict[str, Any] = {}
        if feedback:
            payload["feedback"] = feedback

        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.post(
                    f"{base_url}/api/jobs/{resolved_job_id}/resume",
                    json=payload,
                )
                resp.raise_for_status()
                msg = f"Job {resolved_job_id} resumed."
                if feedback:
                    msg += f" Feedback sent: {feedback[:100]}"
                return msg
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                return f"Failed to resume job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def cancel_worker_job(job_id: str) -> str:
        """Cancel a running or paused job.

        Args:
            job_id: The job UUID to cancel

        Returns:
            Cancellation result
        """
        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.put(f"{base_url}/api/jobs/{resolved_job_id}/cancel")
                resp.raise_for_status()
                return f"Job {resolved_job_id} cancelled."
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                return f"Failed to cancel job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def pause_worker_job(job_id: str) -> str:
        """Pause a running job. The worker stops at the next safe point.

        Args:
            job_id: The job UUID to pause

        Returns:
            Pause result
        """
        async with _get_client(user_id=context.user_id) as client:
            try:
                resolved_job_id = await _resolve_job_id(client, base_url, job_id)
                resp = await client.put(f"{base_url}/api/jobs/{resolved_job_id}/pause")
                resp.raise_for_status()
                return f"Job {resolved_job_id} pause requested. Worker will stop at next safe point."
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                return f"Failed to pause job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    return [
        get_session_context,
        create_worker_job,
        list_worker_jobs,
        get_worker_job,
        get_job_workspace_file,
        approve_worker_job,
        resume_worker_job,
        cancel_worker_job,
        pause_worker_job,
    ]
