"""delegate_work tool — spawn 1-5 subagent child jobs.

The tool validates inputs, commits the current workspace state, creates child
jobs via the orchestrator API, writes a `.subagents.json` manifest, and requests
a freeze so the parent graph suspends cleanly.

Children branch off the parent workspace via git worktrees and run in parallel.
The parent resumes when all children reach a terminal state.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..context import ToolContext

logger = logging.getLogger(__name__)

MAX_SUBAGENTS = 5
PORT_RANGE_BASE = 8000
PORT_RANGE_SIZE = 100

DELEGATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "delegate_work": {
        "module": "delegation.delegate_work",
        "function": "delegate_work",
        "description": (
            "Delegate work to 1-5 subagent jobs that branch off your workspace. "
            "Each subagent gets its own git branch and worktree, runs in parallel, "
            "and you resume to review and merge their results. Always synchronous — "
            "you will suspend until all subagents complete."
        ),
        "category": "delegation",
        "short_description": "Spawn 1-5 parallel subagent jobs from your workspace.",
        "phases": ["strategic", "tactical"],
    },
    "resume_delegation_child": {
        "module": "delegation.delegate_work",
        "function": "resume_delegation_child",
        "description": (
            "Resume a delegation child with feedback after reviewing its work. "
            "The child will continue working, and you will suspend again until "
            "all children complete."
        ),
        "category": "delegation",
        "short_description": "Send a child back to work with feedback, then re-suspend.",
        "phases": ["strategic", "tactical"],
    },
}


def _build_subagent_env_block(
    creation_order: int, total: int, tasks: List[Dict[str, Any]]
) -> str:
    """Build the subagent environment awareness block.

    Injected into each child's delegation_context so it knows its port range
    and sibling information.
    """
    base_port = PORT_RANGE_BASE + (creation_order + 1) * PORT_RANGE_SIZE
    end_port = base_port + PORT_RANGE_SIZE - 1

    lines = [
        "=== SUBAGENT ENVIRONMENT ===",
        f"You are subagent {creation_order} of {total} "
        f"(branch: subagent/{creation_order}, worktree: .worktrees/subagent_{creation_order}).",
        f"Your assigned port range: {base_port}-{end_port}. Use these ports for any servers or services.",
        "Do NOT use ports outside your range — other subagents are running concurrently.",
        "",
    ]

    # Add sibling info
    siblings = []
    for i in range(total):
        if i == creation_order:
            continue
        sib_base = PORT_RANGE_BASE + (i + 1) * PORT_RANGE_SIZE
        sib_end = sib_base + PORT_RANGE_SIZE - 1
        desc = tasks[i].get("description", "")[:60]
        siblings.append(
            f"  - subagent/{i}: worktree .worktrees/subagent_{i}, "
            f"ports {sib_base}-{sib_end} ({desc})"
        )

    if siblings:
        lines.append(
            "Sibling subagents (do not interfere with their processes or files):"
        )
        lines.extend(siblings)

    lines.append("================================")
    return "\n".join(lines)


def _build_manifest(
    parent_job_id: str,
    tasks: List[Dict[str, Any]],
    child_job_ids: List[str],
) -> Dict[str, Any]:
    """Build the .subagents.json manifest."""
    subagents = []
    for i, task in enumerate(tasks):
        base_port = PORT_RANGE_BASE + (i + 1) * PORT_RANGE_SIZE
        subagents.append(
            {
                "creation_order": i,
                "branch": f"subagent/{i}",
                "worktree": f".worktrees/subagent_{i}",
                "port_range": [base_port, base_port + PORT_RANGE_SIZE - 1],
                "description": task.get("description", ""),
                "config": task.get("config", ""),
                "job_id": child_job_ids[i] if i < len(child_job_ids) else None,
            }
        )

    return {
        "parent_job_id": parent_job_id,
        "delegation_timestamp": datetime.now(timezone.utc).isoformat(),
        "subagents": subagents,
    }


def _validate_inputs(
    tasks: list,
    context_str: str,
    timeout: int,
    tool_context: ToolContext,
) -> str | None:
    """Validate delegate_work inputs. Returns error message or None."""
    # Check task count
    if not tasks or len(tasks) == 0:
        return "Error: tasks list cannot be empty. Provide 1-5 task dicts."
    if len(tasks) > MAX_SUBAGENTS:
        return f"Error: maximum {MAX_SUBAGENTS} tasks allowed, got {len(tasks)}."

    # Check task descriptions
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            return f"Error: task {i} must be a dict with 'description' key."
        desc = task.get("description", "").strip()
        if not desc:
            return f"Error: task {i} has empty description."

    # Check delegation config
    config = tool_context.config
    delegation_config = config.get("delegation", {})
    if not delegation_config.get("enabled", False):
        return "Error: delegation is not enabled in this agent's config."

    # Check timeout
    max_timeout = delegation_config.get("max_timeout", 14400)
    if timeout > max_timeout:
        return f"Error: timeout {timeout}s exceeds max_timeout {max_timeout}s."

    # Check allowed configs
    allowed = delegation_config.get("allowed_configs", [])
    if allowed:
        for i, task in enumerate(tasks):
            task_config = task.get("config", "")
            if task_config and task_config not in allowed:
                return (
                    f"Error: config '{task_config}' for task {i} is not in "
                    f"allowed_configs: {allowed}."
                )

    # Check git versioning
    ws = tool_context.workspace_manager
    if not ws or not ws.git_manager or not ws.git_manager.is_active:
        return (
            "Error: delegation requires git versioning. "
            "Ensure workspace.git_versioning is true in your config."
        )

    # Check orchestrator client
    if not tool_context.orchestrator_client:
        return "Error: no orchestrator client available. Delegation requires orchestrator connectivity."

    return None


async def _check_delegation_depth(
    tool_context: ToolContext, max_depth: int
) -> str | None:
    """Check delegation depth via orchestrator. Returns error or None."""
    job_id = tool_context._job_metadata.get("job_id")
    if not job_id:
        return "Error: no job_id available to check delegation depth."

    client = tool_context.orchestrator_client
    try:
        url = f"{client.orchestrator_url}/api/jobs/{job_id}/delegation-depth"
        response = await client._client.get(url, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            depth = data.get("depth", 0)
            if depth >= max_depth:
                return (
                    f"Delegation depth limit reached (depth={depth}, "
                    f"max_depth={max_depth}). Cannot spawn further subagents."
                )
            return None
        elif response.status_code == 404:
            # Endpoint not implemented — assume depth 0 (root job)
            logger.debug("Delegation depth endpoint not found, assuming depth 0")
            return None
        else:
            return f"Error checking delegation depth: HTTP {response.status_code}"
    except Exception as e:
        logger.warning(f"Failed to check delegation depth: {e}")
        # Fail open for connectivity issues — orchestrator validates on job creation
        return None


def create_delegation_tools(context: ToolContext) -> List[Any]:
    """Create delegation tools with injected ToolContext."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class DelegateWorkInput(BaseModel):
        tasks: list[dict[str, str]] = Field(
            description=(
                "1-5 task dicts. Each must have 'description' (str, required) and "
                "optionally 'config' (str, agent preset like 'scholar', 'developer'). "
                "Each task spawns one subagent."
            )
        )
        context: str = Field(
            default="",
            description=(
                "Shared context/instructions for all subagents. Background info "
                "they all need. Not the task assignment itself."
            ),
        )
        timeout: int = Field(
            default=7200,
            description="Max wait time in seconds (default 2h, max 4h).",
        )

    def _delegate_work_sync(
        tasks: list[dict[str, str]],
        context: str = "",
        timeout: int = 7200,
    ) -> str:
        """Synchronous wrapper — runs the async implementation."""
        tool_context = _outer_context

        # Validate inputs
        error = _validate_inputs(tasks, context, timeout, tool_context)
        if error:
            return error

        # Run async logic in the existing event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context (LangGraph) — use a nested approach
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        _delegate_work_async(tasks, context, timeout, tool_context),
                    ).result(timeout=60)
                return result
            else:
                return loop.run_until_complete(
                    _delegate_work_async(tasks, context, timeout, tool_context)
                )
        except Exception as e:
            logger.error(f"Delegation failed: {e}", exc_info=True)
            return f"Error: delegation failed — {e}"

    _outer_context = context

    async def _delegate_work_async(
        tasks: list[dict[str, str]],
        shared_context: str,
        timeout: int,
        tool_context: ToolContext,
    ) -> str:
        """Core async implementation of delegate_work."""
        job_id = tool_context._job_metadata.get("job_id", "unknown")
        delegation_config = tool_context.config.get("delegation", {})
        max_depth = delegation_config.get("max_depth", 1)

        # Check delegation depth
        depth_error = await _check_delegation_depth(tool_context, max_depth)
        if depth_error:
            return depth_error

        ws = tool_context.workspace_manager
        git = ws.git_manager
        client = tool_context.orchestrator_client

        # 1. Commit current workspace state as delegation snapshot
        git.commit(f"Delegation snapshot before spawning {len(tasks)} subagents")
        if git.has_remote():
            git.push()

        # 2. Create child jobs via orchestrator API
        parent_config = tool_context._job_metadata.get("config_name", "defaults")
        parent_project_id = tool_context._job_metadata.get("project_id")
        parent_priority = tool_context._job_metadata.get("priority", 5)

        child_job_ids = []
        child_summaries = []

        for i, task in enumerate(tasks):
            task_desc = task["description"]
            task_config = task.get("config", parent_config)

            # Build per-child environment block
            env_block = _build_subagent_env_block(i, len(tasks), tasks)

            # Combine shared context with env block
            full_delegation_context = env_block
            if shared_context:
                full_delegation_context += f"\n\n{shared_context}"

            # Config overrides: force full autonomy, disable nesting
            config_override = {
                "autonomy": "full",
                "delegation": {"enabled": False},
            }

            result = await client.create_delegation_job(
                description=task_desc,
                config_name=task_config,
                parent_job_id=job_id,
                creation_order=i,
                delegation_context=full_delegation_context,
                config_override=config_override,
                project_id=parent_project_id,
                priority=parent_priority,
            )

            if result and result.get("id"):
                child_job_ids.append(result["id"])
                child_summaries.append(
                    f"  - subagent/{i}: {task_desc[:80]} ({task_config}) "
                    f"→ job {result['id'][:8]}"
                )
            else:
                # Partial failure — clean up already-created jobs
                logger.error(
                    f"[{job_id}] Failed to create child job {i}, aborting delegation"
                )
                return (
                    f"Error: failed to create child job {i} ({task_desc[:50]}). "
                    f"Delegation aborted. {len(child_job_ids)} child jobs were "
                    f"already created and may need manual cleanup."
                )

        # 3. Write .subagents.json manifest
        manifest = _build_manifest(job_id, tasks, child_job_ids)
        try:
            ws.write_file(
                ".subagents.json",
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to write .subagents.json: {e}")

        # 4. Commit manifest
        git.commit(f"Delegation: spawned {len(tasks)} subagents")

        # 5. Request freeze — graph will suspend the parent
        tool_context.request_freeze(
            {
                "freeze_type": "delegation",
                "child_job_ids": child_job_ids,
                "child_count": len(tasks),
                "timeout": timeout,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": job_id,
            }
        )

        # 6. Return status message
        summary_lines = "\n".join(child_summaries)
        return (
            f"Created {len(tasks)} subagent jobs. Suspending to wait for results.\n\n"
            f"Children:\n{summary_lines}\n\n"
            f"Timeout: {timeout}s. You will resume when all children complete."
        )

    delegation_tool = StructuredTool.from_function(
        func=_delegate_work_sync,
        name="delegate_work",
        description=(
            "Delegate work to 1-5 subagent jobs that branch off your workspace. "
            "Each subagent gets its own git branch and worktree, runs in parallel, "
            "and you resume to review and merge their results. Always synchronous — "
            "you will suspend until all subagents complete."
        ),
        args_schema=DelegateWorkInput,
    )

    # --- resume_delegation_child tool ---

    class ResumeDelegationChildInput(BaseModel):
        job_id: str = Field(description="Job ID of the delegation child to resume.")
        feedback: str = Field(
            description=(
                "Feedback for the child explaining what needs to change. "
                "Be specific — the child resumes with this as its only new context."
            )
        )

    def _resume_child_sync(job_id: str, feedback: str) -> str:
        """Synchronous wrapper for resume_delegation_child."""
        tool_context = _outer_context

        if not tool_context.orchestrator_client:
            return "Error: no orchestrator client available."

        parent_job_id = tool_context._job_metadata.get("job_id", "unknown")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        _resume_child_async(
                            job_id, feedback, tool_context, parent_job_id
                        ),
                    ).result(timeout=30)
                return result
            else:
                return loop.run_until_complete(
                    _resume_child_async(job_id, feedback, tool_context, parent_job_id)
                )
        except Exception as e:
            logger.error(f"Resume delegation child failed: {e}", exc_info=True)
            return f"Error: failed to resume child — {e}"

    async def _resume_child_async(
        job_id: str,
        feedback: str,
        tool_context: ToolContext,
        parent_job_id: str,
    ) -> str:
        """Resume a delegation child and re-suspend the parent."""
        client = tool_context.orchestrator_client
        success = await client.resume_job(job_id, feedback=feedback)

        if not success:
            return (
                f"Error: failed to resume child {job_id}. "
                "The orchestrator may be unavailable or the job may not be resumable."
            )

        # Re-suspend: request freeze so parent goes back to waiting
        tool_context.request_freeze(
            {
                "freeze_type": "delegation",
                "child_job_ids": [job_id],
                "child_count": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": parent_job_id,
                "reason": f"Resumed child {job_id} with feedback",
            }
        )

        return (
            f"Resumed child {job_id} with feedback. "
            "Suspending again to wait for updated results."
        )

    resume_child_tool = StructuredTool.from_function(
        func=_resume_child_sync,
        name="resume_delegation_child",
        description=(
            "Resume a delegation child with feedback after reviewing its work. "
            "The child will continue working, and you will suspend again until "
            "all children complete."
        ),
        args_schema=ResumeDelegationChildInput,
    )

    return [delegation_tool, resume_child_tool]
