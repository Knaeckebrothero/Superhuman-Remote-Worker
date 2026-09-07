"""Automation and project-loop workflow tools for persistent sessions."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

from agent.tools.context import ToolContext
from agent.tools.orchestrator.bundle_support import (
    _safe_limit as _safe_limit,
    _canonical_json as _canonical_json,
    _bundle_hash as _bundle_hash,
    _pretty_json as _pretty_json,
    _http_error as _http_error,
    _write_workspace_json as _write_workspace_json,
    _read_bundle_payload as _read_bundle_payload,
    _unwrap_bundle as _unwrap_bundle,
)
from shared.orch_surface.formatters import (
    format_job_list_item as _format_job_list_item,
    truncate_text as _truncate,
)

from agent.tools.orchestrator.jobs import _get_client, _get_orchestrator_url

from shared.tool_catalog.definitions import (
    WORKFLOW_TOOLS_METADATA as WORKFLOW_TOOLS_METADATA,
)


_AUTOMATION_CREATE_FIELDS = {
    "name",
    "description",
    "cron_expr",
    "timezone",
    "catchup_window_seconds",
    "expert",
    "expert_id",
    "prompt",
    "config_override",
    "autonomy",
    "priority",
    "enabled",
    "max_chain_depth",
    "max_fires_per_day",
    "project_id",
}
_AUTOMATION_UPDATE_FIELDS = _AUTOMATION_CREATE_FIELDS - {"project_id"}
_AUTONOMY_VALUES = {"full", "review", "partial", "guided", "dependent"}


def _current_project_id(context: ToolContext) -> Optional[str]:
    return context.project_id or (
        context.project_ids[0] if context.project_ids else None
    )


def _normalize_automation_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = _unwrap_bundle(payload)
    return {key: raw[key] for key in _AUTOMATION_CREATE_FIELDS if key in raw}


def _automation_create_body(
    bundle: Dict[str, Any],
    *,
    allow_enabled: bool,
) -> Dict[str, Any]:
    body = {key: bundle[key] for key in _AUTOMATION_CREATE_FIELDS if key in bundle}
    if not allow_enabled:
        body["enabled"] = False
    return body


def _automation_update_body(
    bundle: Dict[str, Any],
    *,
    allow_enabled: bool,
) -> Dict[str, Any]:
    body = {key: bundle[key] for key in _AUTOMATION_UPDATE_FIELDS if key in bundle}
    if body.get("enabled") is True and not allow_enabled:
        raise ValueError("enabled=true requires allow_enabled=true.")
    return body


def _validate_automation_bundle(
    bundle: Dict[str, Any],
    *,
    require_create_fields: bool,
) -> List[str]:
    errors: List[str] = []
    if require_create_fields:
        for field in ("name", "cron_expr", "expert", "prompt"):
            if not str(bundle.get(field) or "").strip():
                errors.append(f"{field} is required.")
    if "name" in bundle and not str(bundle.get("name") or "").strip():
        errors.append("name cannot be empty.")
    if "cron_expr" in bundle and not str(bundle.get("cron_expr") or "").strip():
        errors.append("cron_expr cannot be empty.")
    if "timezone" in bundle and not str(bundle.get("timezone") or "").strip():
        errors.append("timezone cannot be empty.")
    if bundle.get("autonomy") and bundle["autonomy"] not in _AUTONOMY_VALUES:
        errors.append(
            "autonomy must be one of dependent, full, guided, partial, review."
        )
    for key, minimum, maximum in (
        ("priority", 0, 10),
        ("catchup_window_seconds", 0, 7 * 86400),
        ("max_chain_depth", 1, 100),
        ("max_fires_per_day", 1, 10_000),
    ):
        if key not in bundle or bundle[key] is None:
            continue
        try:
            value = int(bundle[key])
        except (TypeError, ValueError):
            errors.append(f"{key} must be an integer.")
            continue
        if value < minimum or value > maximum:
            errors.append(f"{key} must be between {minimum} and {maximum}.")
    if "config_override" in bundle and bundle["config_override"] is not None:
        if not isinstance(bundle["config_override"], dict):
            errors.append("config_override must be an object.")
    return errors


def _format_automation(item: Dict[str, Any]) -> List[str]:
    automation_id = item.get("id", "unknown")
    lines = [f"--- Automation: {automation_id} ---"]
    if item.get("name"):
        lines.append(f"  Name: {_truncate(item.get('name'), limit=180)}")
    if item.get("description"):
        lines.append(f"  Description: {_truncate(item.get('description'), limit=260)}")
    if item.get("enabled") is not None:
        lines.append(f"  Enabled: {item['enabled']}")
    if item.get("trigger_type"):
        lines.append(f"  Trigger: {item['trigger_type']}")
    if item.get("cron_expr"):
        timezone = item.get("timezone") or "UTC"
        lines.append(f"  Schedule: {item['cron_expr']} ({timezone})")
    if item.get("next_run_at"):
        lines.append(f"  Next run: {item['next_run_at']}")
    if item.get("expert"):
        lines.append(f"  Expert: {item['expert']}")
    if item.get("expert_id"):
        lines.append(f"  Expert ID: {item['expert_id']}")
    if item.get("autonomy"):
        lines.append(f"  Autonomy: {item['autonomy']}")
    if item.get("priority") is not None:
        lines.append(f"  Priority: {item['priority']}")
    if item.get("project_id"):
        lines.append(f"  Project ID: {item['project_id']}")
    if item.get("last_job_id"):
        lines.append(f"  Last job ID: {item['last_job_id']}")
    if item.get("prompt"):
        lines.append(f"  Prompt: {_truncate(item.get('prompt'), limit=320)}")
    return lines


def _format_loop(loop: Dict[str, Any]) -> List[str]:
    loop_id = loop.get("id", "unknown")
    lines = [f"--- Project Loop: {loop_id} ---"]
    for label, key in (
        ("Project ID", "project_id"),
        ("Status", "status"),
        ("Stop reason", "stop_reason"),
        ("Model", "model"),
        ("Workspace backend", "workspace_backend"),
        ("Current job ID", "current_job_id"),
        ("Run until", "run_until"),
        ("Created", "created_at"),
        ("Updated", "updated_at"),
    ):
        if loop.get(key):
            lines.append(f"  {label}: {loop[key]}")
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if len(stage_ids) > 1:
        lines.append(f"  Current stage jobs: {', '.join(stage_ids)}")
    if loop.get("role_sequence"):
        lines.append(f"  Role sequence: {_truncate(loop['role_sequence'], limit=240)}")
    if loop.get("seq_index") is not None:
        lines.append(f"  Sequence index: {loop['seq_index']}")
    if loop.get("remaining_iterations") is not None:
        lines.append(f"  Remaining iterations: {loop['remaining_iterations']}")
    if loop.get("total_jobs_run") is not None:
        lines.append(f"  Total jobs run: {loop['total_jobs_run']}")
    if loop.get("consecutive_failures") is not None:
        lines.append(f"  Consecutive failures: {loop['consecutive_failures']}")
    if loop.get("last_error"):
        lines.append(f"  Last error: {_truncate(loop['last_error'], limit=320)}")
    if loop.get("goal"):
        lines.append(f"  Goal: {_truncate(loop.get('goal'), limit=320)}")
    return lines


def _automation_payload(automation_id: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "automation_bundle",
        "id": str(automation_id),
        "bundle_hash": _bundle_hash(bundle),
        "bundle": bundle,
    }


def _dry_run_summary(
    *,
    mode: str,
    target_id: Optional[str],
    bundle: Dict[str, Any],
    allow_enabled: bool,
) -> str:
    enabled = bundle.get("enabled", True)
    if mode == "create" and not allow_enabled:
        enabled = False
    lines = [
        f"Dry run OK: would {mode} automation.",
        f"Name: {bundle.get('name') or '(unchanged)'}",
        f"Enabled after write: {enabled}",
        f"Bundle hash: {_bundle_hash(bundle)}",
        "No changes written. Call again with dry_run=false to write.",
    ]
    if target_id:
        lines.insert(2, f"Target ID: {target_id}")
    if mode == "create" and not allow_enabled:
        lines.append("Creation writes disabled automations unless allow_enabled=true.")
    return "\n".join(lines)


async def _fetch_automation_bundle(
    client: httpx.AsyncClient,
    base_url: str,
    automation_id: str,
) -> Dict[str, Any]:
    resp = await client.get(
        f"{base_url}/api/automations/{quote(str(automation_id), safe='')}"
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Automation detail did not return a JSON object.")
    return _normalize_automation_bundle(data)


async def _check_expected_hash(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    target_id: Optional[str],
    expected_hash: Optional[str],
) -> Optional[str]:
    if not expected_hash:
        return None
    if not target_id:
        return "expected_hash requires target_id so the current bundle can be checked."
    current = await _fetch_automation_bundle(client, base_url, target_id)
    current_hash = _bundle_hash(current)
    if current_hash != expected_hash:
        return (
            "Refusing to write: current automation bundle hash is "
            f"{current_hash}, not expected_hash {expected_hash}."
        )
    return None


def _explain_loop(loop: Dict[str, Any], jobs: List[Dict[str, Any]]) -> str:
    status = str(loop.get("status") or "unknown")
    lines = [
        f"Project loop {loop.get('id', 'unknown')} is {status}.",
    ]
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if len(stage_ids) > 1:
        lines.append("Current parallel stage jobs: " + ", ".join(stage_ids))
    elif loop.get("current_job_id"):
        lines.append(f"Current job: {loop['current_job_id']}")
    if loop.get("remaining_iterations") is not None:
        lines.append(f"Remaining iterations: {loop['remaining_iterations']}")
    if loop.get("run_until"):
        lines.append(f"Run-until deadline: {loop['run_until']}")
    if loop.get("last_error"):
        lines.append(f"Last error: {_truncate(loop['last_error'], limit=320)}")
    if loop.get("stop_reason"):
        lines.append(f"Stop reason: {loop['stop_reason']}")

    if status == "running":
        lines.append(
            "Next action: inspect the current loop job(s), structured delivery "
            "records, and project cloud files before deciding whether the user "
            "should intervene."
        )
    elif status == "paused":
        lines.append(
            "Next action: review the latest loop jobs and ask the user whether "
            "they want to resume from Cockpit."
        )
    elif status in {"failed", "stopped", "completed"}:
        lines.append(
            "Next action: review the terminal state, latest jobs, and project "
            "cloud output with the user."
        )

    if jobs:
        lines.append("")
        lines.append(f"Latest loop jobs shown: {len(jobs)}")
        for job in jobs[:5]:
            lines.extend(_format_job_list_item(job))
            lines.append("")
    return "\n".join(lines).strip()


def create_workflow_tools(context: ToolContext) -> List[Any]:
    """Create automation and project-loop workflow tools."""
    base_url = _get_orchestrator_url()

    @tool
    async def list_automations(
        project_id: Optional[str] = None,
        limit: int = 30,
    ) -> str:
        """List visible automations.

        Args:
            project_id: Optional project UUID. When omitted, lists the caller's
                own automations.
            limit: Maximum automations to display.
        """
        params: Dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        effective_limit = _safe_limit(limit, default=30, maximum=100)
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(f"{base_url}/api/automations", params=params)
                resp.raise_for_status()
                items = resp.json()
                if not isinstance(items, list):
                    items = (
                        items.get("automations", []) if isinstance(items, dict) else []
                    )
                shown = items[:effective_limit]
                if not shown:
                    return "No matching automations found."
                lines = [f"Found {len(items)} automation(s); showing {len(shown)}:\n"]
                for item in shown:
                    lines.extend(_format_automation(item))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list automations: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_automation(automation_id: str) -> str:
        """Inspect an automation by id."""
        if not automation_id:
            return "automation_id is required."
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/automations/{quote(str(automation_id), safe='')}"
                )
                resp.raise_for_status()
                return "\n".join(_format_automation(resp.json()))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Automation '{automation_id}' not found or not visible."
                return f"Failed to get automation: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_automation_runs(automation_id: str, limit: int = 30) -> str:
        """List jobs spawned by an automation.

        Args:
            automation_id: Automation UUID.
            limit: Maximum runs to display.
        """
        if not automation_id:
            return "automation_id is required."
        effective_limit = _safe_limit(limit, default=30, maximum=100)
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/automations/{quote(str(automation_id), safe='')}/runs",
                    params={"limit": effective_limit},
                )
                resp.raise_for_status()
                jobs = resp.json()
                if not isinstance(jobs, list):
                    jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else []
                if not jobs:
                    return f"No runs found for automation {automation_id}."
                lines = [f"Found {len(jobs)} run(s) for automation {automation_id}:\n"]
                for job in jobs:
                    lines.extend(_format_job_list_item(job))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list automation runs: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def propose_automation(
        name: str,
        prompt: str,
        expert: str,
        cron_expr: str,
        timezone: str = "UTC",
        project_id: Optional[str] = None,
        description: Optional[str] = None,
        autonomy: Literal[
            "full", "review", "partial", "guided", "dependent"
        ] = "review",
        priority: int = 5,
    ) -> str:
        """Draft a disabled automation bundle without writing it.

        Args:
            name: Automation display name.
            prompt: Job prompt to run on schedule.
            expert: Expert/config name for spawned jobs.
            cron_expr: Cron expression, such as 0 9 * * 1.
            timezone: IANA timezone name, defaults to UTC.
            project_id: Optional project UUID. Defaults to this session's project.
            description: Optional description.
            autonomy: Job autonomy for spawned jobs — one of: full, review,
                partial, guided, dependent (defaults to review).
            priority: Job priority, 0-10.
        """
        bundle = {
            "name": name,
            "description": description,
            "cron_expr": cron_expr,
            "timezone": timezone,
            "expert": expert,
            "prompt": prompt,
            "autonomy": autonomy,
            "priority": priority,
            "enabled": False,
            "project_id": project_id or _current_project_id(context),
        }
        bundle = {key: value for key, value in bundle.items() if value is not None}
        errors = _validate_automation_bundle(bundle, require_create_fields=True)
        if errors:
            return "Automation proposal is invalid:\n- " + "\n- ".join(errors)
        payload = {
            "kind": "automation_proposal",
            "bundle_hash": _bundle_hash(bundle),
            "bundle": bundle,
            "note": (
                "This is a proposal only. To create it, call "
                "set_automation_bundle with dry_run=false after user approval."
            ),
        }
        return _pretty_json(payload)

    @tool
    async def get_automation_bundle(
        automation_id: str,
        destination_path: Optional[str] = None,
    ) -> str:
        """Get a portable JSON automation bundle for editing.

        Args:
            automation_id: Automation UUID.
            destination_path: Optional workspace path to write JSON.
        """
        if not automation_id:
            return "automation_id is required."
        async with _get_client(user_id=context.user_id) as client:
            try:
                bundle = await _fetch_automation_bundle(client, base_url, automation_id)
                payload = _automation_payload(str(automation_id), bundle)
                written = _write_workspace_json(context, destination_path, payload)
                if written:
                    return (
                        f"Wrote automation bundle for '{automation_id}' to {written}.\n"
                        f"Bundle hash: {payload['bundle_hash']}"
                    )
                return _pretty_json(payload)
            except ValueError as e:
                return str(e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Automation '{automation_id}' not found or not visible."
                return f"Failed to get automation bundle: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def set_automation_bundle(
        mode: Literal["create", "update"],
        bundle: Optional[Dict[str, Any]] = None,
        bundle_path: Optional[str] = None,
        target_id: Optional[str] = None,
        expected_hash: Optional[str] = None,
        dry_run: bool = True,
        allow_enabled: bool = False,
    ) -> str:
        """Create or update an automation from JSON.

        Args:
            mode: create for a new automation, update for an existing one.
            bundle: Automation bundle JSON object, or the full object returned by
                get_automation_bundle or propose_automation.
            bundle_path: Optional workspace path containing the bundle JSON.
            target_id: Required for update.
            expected_hash: Optional hash from get_automation_bundle; checked
                before writing when target_id is supplied.
            dry_run: Defaults true. Set false to write.
            allow_enabled: Defaults false. Without it, creates are disabled and
                updates cannot set enabled=true.
        """
        try:
            payload = _read_bundle_payload(
                context, bundle=bundle, bundle_path=bundle_path
            )
            automation_bundle = _normalize_automation_bundle(payload)
        except ValueError as e:
            return str(e)

        if mode not in ("create", "update"):
            return "mode must be create or update."
        if mode == "update" and not target_id:
            return "target_id is required for update."

        errors = _validate_automation_bundle(
            automation_bundle, require_create_fields=mode == "create"
        )
        if errors:
            return "Automation bundle is invalid:\n- " + "\n- ".join(errors)

        try:
            if mode == "update":
                _automation_update_body(automation_bundle, allow_enabled=allow_enabled)
        except ValueError as e:
            return str(e)

        async with _get_client(user_id=context.user_id) as client:
            try:
                mismatch = await _check_expected_hash(
                    client=client,
                    base_url=base_url,
                    target_id=target_id,
                    expected_hash=expected_hash,
                )
                if mismatch:
                    return mismatch
                if dry_run:
                    return _dry_run_summary(
                        mode=mode,
                        target_id=target_id,
                        bundle=automation_bundle,
                        allow_enabled=allow_enabled,
                    )

                if mode == "create":
                    resp = await client.post(
                        f"{base_url}/api/automations",
                        json=_automation_create_body(
                            automation_bundle, allow_enabled=allow_enabled
                        ),
                    )
                else:
                    resp = await client.patch(
                        f"{base_url}/api/automations/{quote(str(target_id), safe='')}",
                        json=_automation_update_body(
                            automation_bundle, allow_enabled=allow_enabled
                        ),
                    )
                resp.raise_for_status()
                result = resp.json()
                result_id = result.get("id") if isinstance(result, dict) else None
                lines = [f"Automation {mode} succeeded."]
                if result_id:
                    lines.append(f"Automation ID: {result_id}")
                if mode == "create" and not allow_enabled:
                    lines.append("Created disabled by default.")
                lines.append(f"Bundle hash: {_bundle_hash(automation_bundle)}")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to {mode} automation: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"
            except ValueError as e:
                return str(e)

    @tool
    async def get_project_loop(project_id: Optional[str] = None) -> str:
        """Inspect the current or most recent project loop.

        Args:
            project_id: Optional project UUID. Defaults to this session's project.
        """
        effective_project_id = project_id or _current_project_id(context)
        if not effective_project_id:
            return (
                "No project_id was provided and this session is not scoped to "
                "a current project."
            )
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/projects/{effective_project_id}/loop"
                )
                resp.raise_for_status()
                return "\n".join(_format_loop(resp.json()))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"No loop found for project {effective_project_id}."
                return f"Failed to get project loop: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_project_loop_jobs(
        project_id: Optional[str] = None,
        limit: int = 30,
    ) -> str:
        """List jobs spawned by the active project loop.

        Args:
            project_id: Optional project UUID. Defaults to this session's project.
            limit: Maximum jobs to return.
        """
        effective_project_id = project_id or _current_project_id(context)
        if not effective_project_id:
            return (
                "No project_id was provided and this session is not scoped to "
                "a current project."
            )
        effective_limit = _safe_limit(limit, default=30, maximum=100)
        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/projects/{effective_project_id}/loop/jobs",
                    params={"limit": effective_limit},
                )
                resp.raise_for_status()
                jobs = resp.json()
                if not isinstance(jobs, list):
                    jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else []
                if not jobs:
                    return f"No loop jobs found for project {effective_project_id}."
                lines = [
                    f"Found {len(jobs)} loop job(s) for project {effective_project_id}:\n"
                ]
                for job in jobs:
                    lines.extend(_format_job_list_item(job))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"No active loop found for project {effective_project_id}."
                return f"Failed to list project loop jobs: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def explain_project_loop(
        project_id: Optional[str] = None,
        include_jobs: bool = True,
    ) -> str:
        """Explain project loop state and useful next actions.

        Args:
            project_id: Optional project UUID. Defaults to this session's project.
            include_jobs: Include up to five recent loop jobs.
        """
        effective_project_id = project_id or _current_project_id(context)
        if not effective_project_id:
            return (
                "No project_id was provided and this session is not scoped to "
                "a current project."
            )
        async with _get_client(user_id=context.user_id) as client:
            try:
                loop_resp = await client.get(
                    f"{base_url}/api/projects/{effective_project_id}/loop"
                )
                loop_resp.raise_for_status()
                loop = loop_resp.json()
                jobs: List[Dict[str, Any]] = []
                if include_jobs:
                    jobs_resp = await client.get(
                        f"{base_url}/api/projects/{effective_project_id}/loop/jobs",
                        params={"limit": 5},
                    )
                    if jobs_resp.status_code != 404:
                        jobs_resp.raise_for_status()
                        data = jobs_resp.json()
                        jobs = data if isinstance(data, list) else data.get("jobs", [])
                return _explain_loop(loop, jobs)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"No loop found for project {effective_project_id}."
                return f"Failed to explain project loop: {_http_error(e)}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    return [
        list_automations,
        get_automation,
        list_automation_runs,
        propose_automation,
        get_automation_bundle,
        set_automation_bundle,
        get_project_loop,
        list_project_loop_jobs,
        explain_project_loop,
    ]
