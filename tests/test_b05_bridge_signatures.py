"""Every B05 bridge in ``main`` must match the operation it forwards to.

R1.B05 left ~80 thin wrappers in ``orchestrator.main`` because callers owned by
later batches still resolve those names. A wrapper is only a bridge while it
accepts what the original accepted and awaits what the original awaited, and
both halves of that failed during integration in ways nothing else caught:

* ``_validate_mcp_datasource`` lost a positional parameter, so every caller
  raised ``TypeError`` — loud, but only at the call site.
* ``_resume_missing_workspace`` gained an ``async``, so its synchronous caller
  stored a coroutine object and logged ``Failed to resume job …: <coroutine
  object …>``. Four more (``_account_defaults_layer``, ``_grant_project_ids``,
  ``_enforce_save_grants``, ``_strip_save_grants``) lost theirs.

An async/sync flip is the dangerous one: an un-awaited coroutine is falsy-ish,
never runs, and produces a plausible-looking failure far from its cause. These
cases compare each bridge against its target directly, so a future edit to
either side has to keep them in step.
"""

from __future__ import annotations

import inspect

import pytest

import orchestrator.main as main
from orchestrator.services import (
    agent_datasource_payload,
    agent_toolset_probe,
    dispatch_credentials,
    grant_enforcement,
    job_datasource_selection,
    job_dispatch_credentials,
    job_start_bundle,
    job_workspace_authority,
    job_workspace_runtime,
    session_attach_payload,
    session_class_policy,
    session_config_resolution,
    session_create_overrides,
    stateless_workspace_scheduler,
    thread_mount_rows,
    thread_workspace_delivery,
    vm_workspace_policy,
)

# main bridge name -> the operation it forwards to
BRIDGES = {
    "_resolve_default_models": session_config_resolution.resolve_default_models,
    "_prefetch_roster_refs": session_config_resolution.prefetch_roster_refs,
    "_resolve_session_account_defaults": (
        session_config_resolution.resolve_session_account_defaults
    ),
    "_account_defaults_layer": session_config_resolution.account_defaults_layer,
    "_acknowledged_grant_strip": session_config_resolution.acknowledged_grant_strip,
    "_resolve_session_config": session_config_resolution.resolve_session_config,
    "_require_supported_protected_session_class": (
        session_config_resolution.require_supported_protected_session_class
    ),
    "_session_grant_violations": session_config_resolution.session_grant_violations,
    "_session_endpoint_violations": (
        session_config_resolution.session_endpoint_violations
    ),
    "_agent_toolset_measurement": agent_toolset_probe.agent_toolset_measurement,
    "_resolve_thread_execution_lane": (
        session_class_policy.resolve_thread_execution_lane
    ),
    "_validated_post_owned_officer_create_fragment": (
        session_create_overrides.validated_post_owned_officer_create_fragment
    ),
    "_user_experts_enabled": grant_enforcement.user_experts_enabled,
    "_grant_project_ids": grant_enforcement.grant_project_ids,
    "_resolve_user_save_grants": grant_enforcement.resolve_user_save_grants,
    "_enforce_save_grants": grant_enforcement.enforce_save_grants,
    "_strip_save_grants": grant_enforcement.strip_save_grants,
    "_enforce_expert_save_prelude": grant_enforcement.enforce_expert_save_prelude,
    "_enforce_expert_save": grant_enforcement.enforce_expert_save,
    "_resolve_runner_grants": grant_enforcement.resolve_runner_grants,
    "_enforce_dispatch_grants": grant_enforcement.enforce_dispatch_grants,
    "_enforce_session_create_grants": (grant_enforcement.enforce_session_create_grants),
    "_enforce_job_create_grants": grant_enforcement.enforce_job_create_grants,
    "_check_vm_permission": vm_workspace_policy.check_vm_permission,
    "_enforce_workspace_upgrade_grants_for_config": (
        grant_enforcement.enforce_workspace_upgrade_grants_for_config
    ),
    "_enforce_workspace_upgrade_grants": (
        grant_enforcement.enforce_workspace_upgrade_grants
    ),
    "_enforce_job_workspace_upgrade_grants": (
        grant_enforcement.enforce_job_workspace_upgrade_grants
    ),
    "_seed_registry_model_overrides": (
        dispatch_credentials.seed_registry_model_overrides
    ),
    "_inject_model_credentials": dispatch_credentials.inject_model_credentials,
    "_inject_env_key_credentials": dispatch_credentials.inject_env_key_credentials,
    "_inject_search_credentials": dispatch_credentials.inject_search_credentials,
    "_inject_system_kb_embedding_profile": (
        dispatch_credentials.inject_system_kb_embedding_profile
    ),
    "_inject_thread_dispatch_credentials": (
        dispatch_credentials.inject_thread_dispatch_credentials
    ),
    "_build_datasource_tool_override": (
        agent_datasource_payload.build_datasource_tool_override
    ),
    "_build_datasources_payload": agent_datasource_payload.build_datasources_payload,
    "_mcp_datasource_runtime_allowed": (
        agent_datasource_payload.mcp_datasource_runtime_allowed
    ),
    "_inherit_parent_datasource_ids": (
        job_datasource_selection.inherit_parent_datasource_ids
    ),
    "_filter_implicit_lite_datasource_ids": (
        job_datasource_selection.filter_implicit_lite_datasource_ids
    ),
    "_revalidate_job_datasource_selection": (
        job_datasource_selection.revalidate_job_datasource_selection
    ),
    "_resolve_authorized_job_datasources": (
        job_datasource_selection.resolve_authorized_job_datasources
    ),
    "_revalidate_job_datasource_ids": (
        job_datasource_selection.revalidate_job_datasource_ids
    ),
    "_fail_vm_parked_job": job_workspace_runtime.fail_vm_parked_job,
    "_job_needs_sandbox": job_workspace_runtime.job_needs_sandbox,
    "_resolve_requested_job_execution_lane": (
        job_workspace_runtime.resolve_requested_job_execution_lane
    ),
    "_scholar_should_provision_parent_container": (
        job_workspace_runtime.scholar_should_provision_parent_container
    ),
    "_resume_missing_workspace": job_workspace_runtime.resume_missing_workspace,
    "_inject_matching_workspace_config": (
        job_workspace_runtime.inject_matching_workspace_config
    ),
    "_attest_pinned_k8s_job_workspace": (
        job_workspace_authority.attest_pinned_k8s_job_workspace
    ),
    "_pinned_k8s_job_workspace_authority_is_current": (
        job_workspace_authority.pinned_k8s_job_workspace_authority_is_current
    ),
    "_attest_stateless_worker_workspace": (
        job_workspace_authority.attest_stateless_worker_workspace
    ),
    "_attest_stateless_worker_vm_workspace": (
        job_workspace_authority.attest_stateless_worker_vm_workspace
    ),
    "_workspace_runtime_unchanged_before_delivery": (
        job_workspace_authority.workspace_runtime_unchanged_before_delivery
    ),
    "_resolve_subjob_inherited_workspace": (
        job_workspace_authority.resolve_subjob_inherited_workspace
    ),
    "_prepare_job_workspace_runtime": (
        job_workspace_authority.prepare_job_workspace_runtime
    ),
    "_fail_subjob_and_unblock_parent": (
        job_workspace_authority.fail_subjob_and_unblock_parent
    ),
    "_provision_parent_workspace_for_scholar": (
        job_workspace_authority.provision_parent_workspace_for_scholar
    ),
    "_inject_dispatch_credentials": (
        job_dispatch_credentials.inject_dispatch_credentials
    ),
    "_job_project_repositories": job_start_bundle.job_project_repositories,
    "_prepare_job_repository_before_claim": (
        job_start_bundle.prepare_job_repository_before_claim
    ),
    "_build_job_start_request": job_start_bundle.build_job_start_request,
    "_thread_project_ids": thread_mount_rows.thread_project_ids,
    "_should_skip_session_folder": thread_mount_rows.should_skip_session_folder,
    "_project_ids_from_mounts": thread_mount_rows.project_ids_from_mounts,
    "_build_default_project_mount_row": (
        thread_mount_rows.build_default_project_mount_row
    ),
    "_build_thread_mount_rows": thread_mount_rows.build_thread_mount_rows,
    "_resolve_thread_datasources": thread_mount_rows.resolve_thread_datasources,
    "_resolve_thread_repositories": thread_mount_rows.resolve_thread_repositories,
    "_agent_canvas_workspace_capabilities": (
        thread_workspace_delivery.agent_canvas_workspace_capabilities
    ),
    "_attest_pinned_thread_k8s_workspace": (
        thread_workspace_delivery.attest_pinned_thread_k8s_workspace
    ),
    "_require_pinned_workspace_credential_owner": (
        thread_workspace_delivery.require_pinned_workspace_credential_owner
    ),
    "_agent_get_thread_workspace_locked": (
        thread_workspace_delivery.agent_get_thread_workspace_locked
    ),
    "_assemble_session_attach_payload": (
        session_attach_payload.assemble_session_attach_payload
    ),
    "_schedule_stateless_workspace_ensure": (
        stateless_workspace_scheduler.schedule_stateless_workspace_ensure
    ),
}


@pytest.mark.parametrize("bridge_name", sorted(BRIDGES))
def test_a_bridge_awaits_what_its_target_awaits(bridge_name: str) -> None:
    """The dangerous half: a flipped ``async`` never raises at the boundary.

    A synchronous caller handed a coroutine stores it, never runs it, and fails
    somewhere else entirely — which is exactly how ``_resume_missing_workspace``
    surfaced, as ``Failed to resume job …: <coroutine object …>``.
    """
    bridge = getattr(main, bridge_name)
    target = BRIDGES[bridge_name]
    assert inspect.iscoroutinefunction(bridge) is inspect.iscoroutinefunction(target), (
        f"main.{bridge_name} is "
        f"{'async' if inspect.iscoroutinefunction(bridge) else 'sync'} but "
        f"{target.__module__}.{target.__name__} is "
        f"{'async' if inspect.iscoroutinefunction(target) else 'sync'}"
    )


@pytest.mark.parametrize("bridge_name", sorted(BRIDGES))
def test_a_bridge_accepts_what_its_target_accepts(bridge_name: str) -> None:
    """A bridge that spells its parameters out must spell out the same ones.

    ``dependencies`` is the one addition the extraction is allowed to make, and
    a ``*args, **kwargs`` bridge forwards everything by construction.
    """
    bridge = getattr(main, bridge_name)
    signature = inspect.signature(bridge)
    kinds = {p.kind for p in signature.parameters.values()}
    if {
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    } <= kinds:
        pytest.skip("forwards every argument by construction")
    target = inspect.signature(BRIDGES[bridge_name])
    expected = [n for n in target.parameters if n != "dependencies"]
    assert list(signature.parameters) == expected, (
        f"main.{bridge_name}{signature} does not accept what "
        f"{BRIDGES[bridge_name].__name__}{target} accepts"
    )
