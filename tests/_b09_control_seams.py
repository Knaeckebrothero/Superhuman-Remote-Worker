"""Direct test adapters for R1.B09 router and operation owners.

These helpers preserve the former positional call shapes used by older tests
without adding compatibility wrappers back to the application module. Each
call resolves the real application dependency factory at invocation time, so
patches of app-owned collaborators still exercise the extracted boundary.
"""

from __future__ import annotations

from typing import Any

import orchestrator.main as main
from orchestrator.routers import job_controls, job_lifecycle, thread_lifecycle


async def create_job(request: Any, body: Any) -> Any:
    return await job_lifecycle.create_job(
        request,
        body,
        dependencies=main._job_lifecycle_route_dependencies(),
    )


async def agent_get_thread_workspace_locked(
    thread_id: str, **kwargs: Any
) -> dict[str, Any]:
    return await main.thread_workspace_delivery.agent_get_thread_workspace_locked(
        thread_id,
        dependencies=main._thread_workspace_delivery_dependencies(),
        **kwargs,
    )


async def subjob_merge(request: Any, job_id: str) -> Any:
    return await job_lifecycle.subjob_merge(
        request,
        job_id,
        dependencies=main._job_lifecycle_route_dependencies(),
    )


async def delete_job(request: Any, job_id: str) -> Any:
    return await job_lifecycle.delete_job(
        request,
        job_id,
        dependencies=main._job_mutation_route_dependencies(),
    )


async def cancel_job(request: Any, job_id: str) -> Any:
    return await job_lifecycle.cancel_job(
        request,
        job_id,
        dependencies=main._job_mutation_route_dependencies(),
    )


async def pause_job(request: Any, job_id: str) -> Any:
    return await job_lifecycle.pause_job(
        request,
        job_id,
        dependencies=main._job_mutation_route_dependencies(),
    )


async def agent_release_job(
    request: Any,
    job_id: str,
    agent_id: str | None = None,
    lease_token: int | None = None,
) -> Any:
    return await job_lifecycle.agent_release_job(
        request,
        job_id,
        agent_id,
        lease_token,
        dependencies=main._job_mutation_route_dependencies(),
    )


async def create_vm(request: Any, body: Any) -> Any:
    return await job_controls.create_vm(
        request,
        body,
        dependencies=main._job_control_route_dependencies(),
    )


async def list_vms(request: Any) -> Any:
    return await job_controls.list_vms(
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def get_vm_status(request: Any, job_id: str, live: bool = False) -> Any:
    return await job_controls.get_vm_status(
        request,
        job_id,
        live,
        dependencies=main._job_control_route_dependencies(),
    )


async def delete_vm(request: Any, job_id: str) -> Any:
    return await job_controls.delete_vm(
        request,
        job_id,
        dependencies=main._job_control_route_dependencies(),
    )


async def sudo_sse_events(request: Any) -> Any:
    return await job_controls.sudo_sse_events(
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def list_sudo_requests(request: Any, **kwargs: Any) -> Any:
    return await job_controls.list_sudo_requests(
        request,
        dependencies=main._job_control_route_dependencies(),
        **kwargs,
    )


async def get_sudo_request(request: Any, request_id: str) -> Any:
    return await job_controls.get_sudo_request(
        request,
        request_id,
        dependencies=main._job_control_route_dependencies(),
    )


async def approve_sudo_request(
    request_id: str, body: Any = None, request: Any = None
) -> Any:
    return await job_controls.approve_sudo_request(
        request_id,
        body,
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def deny_sudo_request(request_id: str, body: Any, request: Any) -> Any:
    return await job_controls.deny_sudo_request(
        request_id,
        body,
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def approve_sudo_vm_upgrade(
    request_id: str, body: Any = None, request: Any = None
) -> Any:
    return await job_controls.approve_sudo_vm_upgrade(
        request_id,
        body,
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def resume_sudo_without_vm(
    request_id: str, body: Any = None, request: Any = None
) -> Any:
    return await job_controls.resume_sudo_without_vm(
        request_id,
        body,
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def list_sudo_rules(request: Any) -> Any:
    return await job_controls.list_sudo_rules(
        request,
        dependencies=main._job_control_route_dependencies(),
    )


async def create_sudo_rule(request: Any, body: Any) -> Any:
    return await job_controls.create_sudo_rule(
        request,
        body,
        dependencies=main._job_control_route_dependencies(),
    )


async def delete_sudo_rule(request: Any, rule_id: str) -> Any:
    return await job_controls.delete_sudo_rule(
        request,
        rule_id,
        dependencies=main._job_control_route_dependencies(),
    )


async def resume_job(req: Any, job_id: str, body: Any = None) -> Any:
    return await job_controls.resume_job(
        req,
        job_id,
        body,
        dependencies=main._job_control_route_dependencies(),
    )


async def approve_job(req: Any, job_id: str, body: Any = None) -> Any:
    return await job_controls.approve_job(
        req,
        job_id,
        body,
        dependencies=main._job_control_route_dependencies(),
    )


async def upgrade_job_to_vm(request: Any, job_id: str) -> Any:
    return await job_controls.upgrade_job_to_vm(
        request,
        job_id,
        dependencies=main._job_control_route_dependencies(),
    )


async def end_thread(
    thread_id: str,
    request: Any,
    permanent: bool = False,
    force: bool = False,
) -> Any:
    return await thread_lifecycle.end_thread(
        thread_id,
        request,
        permanent,
        force,
        dependencies=main._thread_lifecycle_dependencies(),
    )


async def resume_thread(thread_id: str, request: Any, body: Any = None) -> Any:
    return await thread_lifecycle.resume_thread(
        thread_id,
        request,
        body,
        dependencies=main._thread_lifecycle_dependencies(),
    )


async def rewind_thread_detached(thread_id: str, request: Any, body: Any) -> Any:
    return await thread_lifecycle.rewind_thread_detached(
        thread_id,
        request,
        body,
        dependencies=main._thread_lifecycle_dependencies(),
    )


async def dispatch_job_to_agent(job: dict[str, Any], agent: dict[str, Any]) -> bool:
    return await main._job_delivery_operations().dispatch(job, agent)


async def resume_job_on_agent(job: dict[str, Any], agent: dict[str, Any]) -> bool:
    return await main._job_delivery_operations().resume(job, agent)


async def initiate_pause(job: dict[str, Any]) -> None:
    await main._job_delivery_operations().initiate_pause(job)


async def resume_job_internal(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().resume_job_internal(*args, **kwargs)


async def approve_job_internal(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().approve_job_internal(*args, **kwargs)


async def upgrade_job_to_vm_internal(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().upgrade_job_to_vm_internal(
        *args, **kwargs
    )


async def apply_vm_upgrade_decision(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().apply_vm_upgrade_decision(
        *args, **kwargs
    )


async def resume_job_without_vm_internal(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().resume_job_without_vm_internal(
        *args, **kwargs
    )


async def internal_resume_job(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().internal_resume_job(*args, **kwargs)


def job_frozen_for_vm_upgrade(job: dict[str, Any] | None) -> bool:
    return main._job_control_operations().job_frozen_for_vm_upgrade(job)


async def unmerged_pr_gate_reason(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().unmerged_pr_gate_reason(*args, **kwargs)


async def capture_workspace_snapshot_for_freeze(*args: Any, **kwargs: Any) -> Any:
    return await main._job_control_operations().capture_workspace_snapshot_for_freeze(
        *args, **kwargs
    )


async def fail_expired_vm_upgrade_jobs() -> int:
    return await main._job_control_operations().fail_expired_vm_upgrade_jobs()


async def cascade_pause_to_children(*args: Any, **kwargs: Any) -> Any:
    return await main._job_mutation_operations().cascade_pause_to_children(
        *args, **kwargs
    )


async def cascade_cancel_to_children(*args: Any, **kwargs: Any) -> Any:
    return await main._job_mutation_operations().cascade_cancel_to_children(
        *args, **kwargs
    )


async def wait_for_stateless_cancel_settle(*args: Any, **kwargs: Any) -> Any:
    return await main._job_mutation_operations().wait_for_stateless_cancel_settle(
        *args, **kwargs
    )


async def archive_and_cleanup_workspace(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_retirement_operations().archive_and_cleanup_workspace(
        *args, **kwargs
    )


async def detach_agent_session(*args: Any, **kwargs: Any) -> Any:
    return await main.thread_retirement_operations.detach_agent_session(
        *args,
        **kwargs,
        dependencies=main._thread_retirement_operations().dependencies,
    )


async def release_thread_resources(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_retirement_operations().release_thread_resources(
        *args, **kwargs
    )


async def suspend_thread_resources(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_retirement_operations().suspend_thread_resources(
        *args, **kwargs
    )


async def thread_turn_in_flight(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_retirement_operations().thread_turn_in_flight(
        *args, **kwargs
    )


def stateless_retirement_marker(*args: Any, **kwargs: Any) -> Any:
    return main._thread_retirement_operations().stateless_retirement_marker(
        *args, **kwargs
    )


async def reconcile_stateless_thread_retirement(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_retirement_operations().reconcile_stateless_thread_retirement(
        *args, **kwargs
    )


async def thread_config_drift(*args: Any, **kwargs: Any) -> Any:
    return await main.thread_resume_operations.thread_config_drift(
        *args,
        **kwargs,
        dependencies=main._thread_resume_operations().dependencies,
    )


async def await_late_cloud_setup(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_resume_operations().await_late_cloud_setup(
        *args, **kwargs
    )


def register_late_cloud_setup(*args: Any, **kwargs: Any) -> Any:
    return main._thread_resume_operations().register_late_cloud_setup(*args, **kwargs)


async def resolve_background_push_workspace(*args: Any, **kwargs: Any) -> Any:
    return await main._thread_resume_operations().resolve_background_push_workspace(
        *args, **kwargs
    )


async def suspend_thread_resources_inner(*args: Any, **kwargs: Any) -> Any:
    return await main.thread_retirement_operations.suspend_thread_resources_inner(
        *args,
        **kwargs,
        dependencies=main._thread_retirement_operations().dependencies,
    )
