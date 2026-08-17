"""Port resolution: pod workspaces use 30022, not the VM-shaped default 22."""

from __future__ import annotations

from orchestrator.services.workspace_suspension import _resolve_ssh_port


def test_pod_context_resolves_30022_when_port_missing():
    ws_ctx = {"status": "ready", "pod_ip": "10.0.0.5"}  # no explicit port
    assert _resolve_ssh_port(ws_ctx, vm_ctx={}) == 30022


def test_pod_context_honors_explicit_port():
    ws_ctx = {"status": "ready", "pod_ip": "10.0.0.5", "port": 30022}
    assert _resolve_ssh_port(ws_ctx, vm_ctx={}) == 30022


def test_vm_context_uses_vm_ssh_port():
    assert _resolve_ssh_port(ws_ctx={}, vm_ctx={"ssh_port": 22}) == 22


def test_vm_context_defaults_22():
    assert _resolve_ssh_port(ws_ctx={}, vm_ctx={}) == 22


# Explicit-tier resolution (knowledge-base/knowledge/issues/
# workspace_suspension_infers_tier_from_metadata_presence.md).
#
# The cases above all have an empty-or-pod-shaped ws_ctx, so presence happens to
# give the right answer. A THREAD never has an empty ws_ctx: _setup_gitea writes
# git_remote_url/repo_name there for every tier, so a vm-tier thread would take
# the pod branch and get 30022. These pin the explicit override.


def test_vm_thread_with_git_only_ws_ctx_uses_vm_port():
    """The actual bug shape: truthy ws_ctx holding ONLY git coordinates."""
    ws_ctx = {
        "git_remote_url": "http://gitea/srw/thread-x.git",
        "repo_name": "thread-x",
    }
    vm_ctx = {"status": "ready", "ssh_host": "100.64.0.235", "ssh_port": 22}
    assert _resolve_ssh_port(ws_ctx, vm_ctx, is_vm=True) == 22


def test_vm_tier_defaults_22_even_with_a_pod_port_present():
    """is_vm wins outright — it must not be overridden by a stale pod port."""
    ws_ctx = {"port": 30022, "git_remote_url": "http://gitea/x.git"}
    assert _resolve_ssh_port(ws_ctx, vm_ctx={}, is_vm=True) == 22


def test_container_tier_uses_pod_port_even_with_a_vm_ctx_present():
    ws_ctx = {"status": "ready", "pod_ip": "10.42.2.32"}
    assert _resolve_ssh_port(ws_ctx, vm_ctx={"ssh_port": 22}, is_vm=False) == 30022


def test_omitted_is_vm_keeps_presence_behaviour_for_job_callers():
    """Job callers pass no tier; their workspace_container presence IS valid."""
    assert _resolve_ssh_port({"status": "ready"}, {"ssh_port": 22}) == 30022
    assert _resolve_ssh_port({}, {"ssh_port": 22}) == 22
