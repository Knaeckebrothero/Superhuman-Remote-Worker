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
