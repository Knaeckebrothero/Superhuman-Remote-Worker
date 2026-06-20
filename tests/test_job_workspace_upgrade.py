"""Worker-job workspace tier upgrade — `virtual`/`none` → `sandbox` in process.

Covers Phase 3 (W1 + W2) of docs/features/workspace_tier_upgrade.md:

  - POST /api/jobs/{id}/provision-workspace  (orchestrator, §4.3 W2)
  - GET  /api/jobs/{id}/workspace-status     (orchestrator, agent's poll source)
  - _enforce_job_workspace_upgrade_grants    (shared Sec-1 gate, job wrapper)
  - UniversalAgent._poll_job_workspace_ready  (agent, §4.3 W1)
  - UniversalAgent._perform_inprocess_workspace_upgrade guards (agent)
  - UniversalAgent._process_job_streaming freeze interception (agent, §4.3 W1)

Orchestrator endpoint logic is REPLICATED here (not imported) — orchestrator.main
needs DB/service deps unavailable under test, the same constraint as
test_thread_endpoints.py. The real `_enforce_job_workspace_upgrade_grants` is
therefore only exercised live (k3d/dev); its PDP core is covered by
test_capability_grants.py. The job-wrapper EXTRACTION (config_override from the
top-level jobs column, not metadata) is what this file pins, against the real
`evaluate`.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.workspace_lifecycle import WorkspaceOwner
from src.core.capability_grants import CATALOG, evaluate

DEFAULTS = {k: v["default"] for k, v in CATALOG.items()}


# =============================================================================
# Replicated orchestrator endpoint logic — mirrors orchestrator/main.py
# =============================================================================


class _GateDenied(Exception):
    """Stand-in for the orchestrator's HTTPException(403) from the shared Sec-1
    gate (_enforce_job_workspace_upgrade_grants)."""


async def provision_job_workspace(
    db, provisioner, job_id, target_tier="sandbox", grant_gate=None
):
    """§4.3 W2: POST /api/jobs/{job_id}/provision-workspace — mirrors main.py.

    Provisions a sandbox container for a RUNNING lite job, in place. No status
    change, no re-dispatch. Idempotent on an in-flight/ready container. ``vm`` is
    refused (operator-gated; goes through /upgrade-to-vm). ``grant_gate`` mirrors
    the shared Sec-1 gate: an async ``(job, target_tier) -> None`` raising
    ``_GateDenied`` on refusal, run BEFORE provisioning (fail-closed).
    """
    target_tier = target_tier or "sandbox"
    if target_tier != "sandbox":
        return {"error": 400, "detail": f"unsupported target_tier {target_tier!r}"}

    job = await db.get_job(job_id)
    if not job:
        return {"error": 404, "detail": "Job not found"}

    if grant_gate is not None:
        try:
            await grant_gate(job, target_tier)
        except _GateDenied as exc:
            return {"error": 403, "detail": str(exc)}

    if not (provisioner.is_available and provisioner.in_cluster):
        return {
            "error": 503,
            "detail": "Workspace container provisioning not available",
        }

    context = job.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            context = {}
    wc = context.get("workspace_container") or {}
    if wc.get("status") in ("pending", "creating", "created", "ready"):
        return {
            "status": wc["status"],
            "job_id": job_id,
            "target_tier": "sandbox",
            "message": "Workspace container already provisioned or in progress",
        }

    await db.merge_workspace_container_context(job_id, {"status": "pending"})
    await provisioner.create_workspace(WorkspaceOwner.job(job_id))

    return {"status": "provisioning", "job_id": job_id, "target_tier": "sandbox"}


async def get_job_workspace_status(db, job_id, ssh_key_path=None):
    """GET /api/jobs/{job_id}/workspace-status — mirrors main.py.

    Surfaces context.workspace_container connection fields for the agent poller,
    mapping the provisioner's ``port`` -> ``pod_port`` (session-poller shape).
    """
    job = await db.get_job(job_id)
    if not job:
        return {"error": 404, "detail": "Job not found"}
    context = job.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            context = {}
    wc = context.get("workspace_container") or {}
    return {
        "status": wc.get("status", "none"),
        "pod_ip": wc.get("pod_ip") or wc.get("host"),
        "pod_port": wc.get("pod_port") or wc.get("port"),
        "pod_name": wc.get("pod_name"),
        "namespace": wc.get("namespace"),
        "ssh_key_path": ssh_key_path,
        "git_remote_url": wc.get("git_remote_url"),
    }


async def enforce_job_grants(job, target_tier, grants):
    """Mirror of _enforce_job_workspace_upgrade_grants' EXTRACTION + PDP, against
    the real `evaluate`: jobs carry config_override as a top-level column (not
    under metadata). Raises _GateDenied on violation."""
    config_override = job.get("config_override") or {}
    if isinstance(config_override, str):
        config_override = json.loads(config_override)
    post_upgrade = {
        **config_override,
        "workspace": {
            **(config_override.get("workspace") or {}),
            "backend": target_tier,
        },
    }
    violations = evaluate(post_upgrade, grants)
    if violations:
        raise _GateDenied("; ".join(violations))


# =============================================================================
# Mock helpers
# =============================================================================


def _mock_provisioner(available=True, in_cluster=True):
    prov = MagicMock()
    prov.is_available = available
    prov.in_cluster = in_cluster
    prov.create_workspace = AsyncMock(return_value=True)
    return prov


def _mock_db(job=None):
    db = MagicMock()
    db.get_job = AsyncMock(return_value=job)
    db.merge_workspace_container_context = AsyncMock(return_value=True)
    return db


def _make_job(
    job_id="job-1",
    user_id="user-1",
    config_override=None,
    context=None,
    status="processing",
):
    return {
        "id": job_id,
        "user_id": user_id,
        "status": status,
        "config_override": config_override,
        "context": context or {},
    }


# =============================================================================
# W2 — POST /api/jobs/{id}/provision-workspace
# =============================================================================


class TestProvisionJobWorkspace:
    @pytest.mark.asyncio
    async def test_vm_tier_returns_400(self):
        # vm is operator-gated and can't stay in-process — refused here.
        db = _mock_db(_make_job())
        prov = _mock_provisioner()
        r = await provision_job_workspace(db, prov, "job-1", target_tier="vm")
        assert r["error"] == 400
        prov.create_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_tier_returns_400(self):
        db = _mock_db(_make_job())
        prov = _mock_provisioner()
        r = await provision_job_workspace(db, prov, "job-1", target_tier="bogus")
        assert r["error"] == 400

    @pytest.mark.asyncio
    async def test_missing_job_returns_404(self):
        db = _mock_db(None)
        prov = _mock_provisioner()
        r = await provision_job_workspace(db, prov, "nope")
        assert r["error"] == 404

    @pytest.mark.asyncio
    async def test_grant_gate_refusal_returns_403_before_provisioning(self):
        db = _mock_db(_make_job())
        prov = _mock_provisioner()

        async def _deny(job, tier):
            raise _GateDenied("shell tools not permitted")

        r = await provision_job_workspace(db, prov, "job-1", grant_gate=_deny)
        assert r["error"] == 403
        # Fail-closed: no pending mark, no provisioning.
        prov.create_workspace.assert_not_called()
        db.merge_workspace_container_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_provisioner_returns_503(self):
        db = _mock_db(_make_job())
        prov = _mock_provisioner(available=True, in_cluster=False)
        r = await provision_job_workspace(db, prov, "job-1")
        assert r["error"] == 503

    @pytest.mark.asyncio
    async def test_idempotent_when_already_in_flight(self):
        job = _make_job(context={"workspace_container": {"status": "ready"}})
        db = _mock_db(job)
        prov = _mock_provisioner()
        r = await provision_job_workspace(db, prov, "job-1")
        assert r["status"] == "ready"
        prov.create_workspace.assert_not_called()
        db.merge_workspace_container_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_sandbox_provisions_in_place(self):
        db = _mock_db(_make_job())
        prov = _mock_provisioner()
        r = await provision_job_workspace(db, prov, "job-1")
        assert r["status"] == "provisioning"
        assert r["target_tier"] == "sandbox"
        # Marks pending, then provisions for the JOB owner kind (not session).
        db.merge_workspace_container_context.assert_awaited_once_with(
            "job-1", {"status": "pending"}
        )
        prov.create_workspace.assert_awaited_once()
        owner = prov.create_workspace.call_args.args[0]
        assert owner.kind == "job"
        assert owner.id == "job-1"


# =============================================================================
# GET /api/jobs/{id}/workspace-status
# =============================================================================


class TestGetJobWorkspaceStatus:
    @pytest.mark.asyncio
    async def test_ready_maps_port_to_pod_port(self):
        # Provisioner writes {pod_ip, port}; the endpoint exposes pod_port.
        job = _make_job(
            context={
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.1.2.3",
                    "port": 30022,
                    "pod_name": "workspace-job-1",
                    "namespace": "srw",
                }
            }
        )
        db = _mock_db(job)
        r = await get_job_workspace_status(db, "job-1", ssh_key_path="/k/key")
        assert r["status"] == "ready"
        assert r["pod_ip"] == "10.1.2.3"
        assert r["pod_port"] == 30022
        assert r["ssh_key_path"] == "/k/key"

    @pytest.mark.asyncio
    async def test_none_when_no_container(self):
        db = _mock_db(_make_job())
        r = await get_job_workspace_status(db, "job-1")
        assert r["status"] == "none"
        assert r["pod_ip"] is None

    @pytest.mark.asyncio
    async def test_missing_job_returns_404(self):
        db = _mock_db(None)
        r = await get_job_workspace_status(db, "nope")
        assert r["error"] == 404


# =============================================================================
# Shared Sec-1 gate — job wrapper EXTRACTION (top-level config_override column)
# =============================================================================


class TestJobGrantExtraction:
    @pytest.mark.asyncio
    async def test_sandbox_passes_by_default(self):
        # Default grants, no config restrictions → sandbox is ungated.
        job = _make_job(config_override={})
        await enforce_job_grants(job, "sandbox", DEFAULTS)  # no raise

    @pytest.mark.asyncio
    async def test_shell_restricted_owner_refused(self):
        # The job DECLARES shell tools in its top-level config_override column;
        # owner lacks the shell grant → the post-upgrade fragment trips the PDP.
        job = _make_job(config_override={"tools": {"shell": ["ls"]}})
        with pytest.raises(_GateDenied):
            await enforce_job_grants(job, "sandbox", {**DEFAULTS, "shell_tools": False})
        # Granted → clean.
        await enforce_job_grants(job, "sandbox", {**DEFAULTS, "shell_tools": True})

    @pytest.mark.asyncio
    async def test_config_override_as_json_string_is_parsed(self):
        # asyncpg usually returns JSONB as dict, but be robust to a string.
        job = _make_job(config_override=json.dumps({"tools": {"shell": ["ls"]}}))
        with pytest.raises(_GateDenied):
            await enforce_job_grants(job, "sandbox", {**DEFAULTS, "shell_tools": False})


# =============================================================================
# Agent side (§4.3 W1) — poller + upgrade guards + streaming interception
# =============================================================================


def _bare_agent():
    """A UniversalAgent shell (no __init__) for unit-testing instance methods."""
    from src.agent import UniversalAgent

    agent = UniversalAgent.__new__(UniversalAgent)
    agent._current_job_id = "job-1"
    agent._jobs_processed = 0
    return agent


class TestAgentJobWorkspacePoller:
    @pytest.mark.asyncio
    async def test_ready_returns_sandbox_remote_block(self):
        agent = _bare_agent()
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.get_job_workspace_status = AsyncMock(
            return_value={
                "status": "ready",
                "pod_ip": "10.0.0.5",
                "pod_port": 30022,
                "ssh_key_path": "/k/key",
            }
        )
        cfg = await agent._poll_job_workspace_ready("job-1", timeout=5)
        assert cfg["backend"] == "sandbox"
        assert cfg["remote"]["host"] == "10.0.0.5"
        assert cfg["remote"]["port"] == 30022
        assert cfg["remote"]["key_path"] == "/k/key"

    @pytest.mark.asyncio
    async def test_failed_returns_none(self):
        agent = _bare_agent()
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.get_job_workspace_status = AsyncMock(
            return_value={"status": "failed"}
        )
        assert await agent._poll_job_workspace_ready("job-1", timeout=5) is None

    @pytest.mark.asyncio
    async def test_none_status_returns_none(self):
        agent = _bare_agent()
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.get_job_workspace_status = AsyncMock(
            return_value={"status": "none"}
        )
        assert await agent._poll_job_workspace_ready("job-1", timeout=5) is None

    @pytest.mark.asyncio
    async def test_creating_then_ready_polls_through(self):
        agent = _bare_agent()
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.get_job_workspace_status = AsyncMock(
            side_effect=[
                {"status": "pending"},
                {"status": "creating"},
                {"status": "ready", "pod_ip": "10.0.0.9"},
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            cfg = await agent._poll_job_workspace_ready("job-1", timeout=30)
        assert cfg["remote"]["host"] == "10.0.0.9"
        assert agent._orchestrator_client.get_job_workspace_status.await_count == 3


class TestW1TriggerExposure:
    """The W1 trigger: `request_workspace_upgrade` must be (a) a real registered
    `core` tool and (b) exposed on a lite worker but NOT a shell-capable one.
    `_setup_job_tools` injects it lite-only (it's in no config's tool list), so
    without this a lite worker job could never request an upgrade. Mirrors the
    inline decision against the real registry + filter."""

    def test_tool_is_registered_core(self):
        from src.tools.registry import TOOL_REGISTRY

        assert "request_workspace_upgrade" in TOOL_REGISTRY
        assert TOOL_REGISTRY["request_workspace_upgrade"]["category"] == "core"

    def test_injected_on_lite_kept_through_filter(self):
        from src.tools.registry import filter_tools_by_backend

        class _Lite:
            supports_shell = False
            supports_file_tools = True

        # The inline _setup_job_tools logic: filter, then lite-only inject.
        names = filter_tools_by_backend(["run_command"], _Lite())
        if not getattr(_Lite(), "supports_shell", False):
            names.append("request_workspace_upgrade")
        assert "request_workspace_upgrade" in names
        # shell tool correctly dropped on the lite backend
        assert "run_command" not in names

    def test_not_injected_on_shell_backend(self):
        class _Sandbox:
            supports_shell = True
            supports_file_tools = True

        names = ["run_command"]
        if not getattr(_Sandbox(), "supports_shell", False):
            names.append("request_workspace_upgrade")
        # shell-capable backend → not injected (nothing left to upgrade to)
        assert "request_workspace_upgrade" not in names


class TestAgentInprocessUpgradeGuards:
    @pytest.mark.asyncio
    async def test_already_shell_backend_is_noop_true(self):
        agent = _bare_agent()
        agent._workspace_manager = MagicMock()
        agent._workspace_manager.backend.supports_shell = True
        agent._orchestrator_client = MagicMock()  # must not be called
        agent._orchestrator_client.request_job_workspace_upgrade = AsyncMock()
        assert await agent._perform_inprocess_workspace_upgrade("sandbox") is True
        agent._orchestrator_client.request_job_workspace_upgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_orchestrator_client_returns_false(self):
        agent = _bare_agent()
        agent._workspace_manager = MagicMock()
        agent._workspace_manager.backend.supports_shell = False
        agent._orchestrator_client = None
        assert await agent._perform_inprocess_workspace_upgrade("sandbox") is False

    @pytest.mark.asyncio
    async def test_provision_refused_returns_false(self):
        agent = _bare_agent()
        agent._workspace_manager = MagicMock()
        agent._workspace_manager.backend.supports_shell = False
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.request_job_workspace_upgrade = AsyncMock(
            return_value=False
        )
        assert await agent._perform_inprocess_workspace_upgrade("sandbox") is False

    @pytest.mark.asyncio
    async def test_poll_timeout_returns_false_before_backend_build(self):
        # provision accepted, but readiness never arrives -> upgrade aborts
        # cleanly (no RemoteBackend built).
        agent = _bare_agent()
        agent._workspace_manager = MagicMock()
        agent._workspace_manager.backend.supports_shell = False
        agent._orchestrator_client = MagicMock()
        agent._orchestrator_client.request_job_workspace_upgrade = AsyncMock(
            return_value=True
        )
        agent._orchestrator_client.get_job_workspace_status = AsyncMock(
            return_value={"status": "failed"}
        )
        assert await agent._perform_inprocess_workspace_upgrade("sandbox") is False


def _async_gen_from(states):
    async def _gen(*_args, **_kwargs):
        for s in states:
            yield s

    return _gen


class TestStreamingInterception:
    """The core-loop change: a workspace_upgrade_required(sandbox) freeze drives
    an in-process upgrade + resume; anything else streams as before."""

    def _agent_for_stream(self):
        agent = _bare_agent()
        agent._workspace_manager = MagicMock()
        agent._workspace_manager.backend.supports_shell = False
        agent._graph = MagicMock()
        agent._graph.aupdate_state = AsyncMock()
        agent._cleanup_shell_manager = MagicMock()
        agent._close_datasource_connections = MagicMock()
        agent._cleanup_checkpointer = AsyncMock()
        return agent

    @pytest.mark.asyncio
    async def test_normal_completion_does_not_upgrade(self):
        agent = self._agent_for_stream()
        agent._perform_inprocess_workspace_upgrade = AsyncMock(return_value=True)
        states = [{"iteration": 1}, {"should_stop": True, "goal_achieved": True}]
        with patch("src.agent.run_graph_with_streaming", _async_gen_from(states)):
            out = [s async for s in agent._process_job_streaming(None, {})]
        assert out == states
        agent._perform_inprocess_workspace_upgrade.assert_not_called()
        agent._graph.aupdate_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_matching_freeze_does_not_upgrade(self):
        agent = self._agent_for_stream()
        agent._perform_inprocess_workspace_upgrade = AsyncMock(return_value=True)
        states = [
            {"should_stop": True, "freeze_data": {"freeze_type": "budget_exceeded"}}
        ]
        with patch("src.agent.run_graph_with_streaming", _async_gen_from(states)):
            out = [s async for s in agent._process_job_streaming(None, {})]
        assert out == states
        agent._perform_inprocess_workspace_upgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_upgrade_freeze_triggers_upgrade_then_resumes(self):
        agent = self._agent_for_stream()
        agent._perform_inprocess_workspace_upgrade = AsyncMock(return_value=True)
        first = [
            {
                "should_stop": True,
                "freeze_data": {
                    "freeze_type": "workspace_upgrade_required",
                    "target_tier": "sandbox",
                },
            }
        ]
        second = [{"iteration": 2}, {"should_stop": True, "goal_achieved": True}]
        runs = iter([first, second])

        def _runner(*_a, **_k):
            return _async_gen_from(next(runs))()

        with patch("src.agent.run_graph_with_streaming", _runner):
            out = [s async for s in agent._process_job_streaming(None, {})]
        # Both streams' states surfaced; upgrade ran exactly once; resume primed.
        assert out == first + second
        agent._perform_inprocess_workspace_upgrade.assert_awaited_once_with("sandbox")
        agent._graph.aupdate_state.assert_awaited_once()
        # The resume update clears the stale freeze + stop flags.
        update_arg = agent._graph.aupdate_state.call_args.args[1]
        assert update_arg["freeze_data"] is None
        assert update_arg["should_stop"] is False

    @pytest.mark.asyncio
    async def test_upgrade_failure_surfaces_freeze(self):
        agent = self._agent_for_stream()
        agent._perform_inprocess_workspace_upgrade = AsyncMock(return_value=False)
        freeze_states = [
            {
                "should_stop": True,
                "freeze_data": {
                    "freeze_type": "workspace_upgrade_required",
                    "target_tier": "sandbox",
                },
            }
        ]
        with patch(
            "src.agent.run_graph_with_streaming", _async_gen_from(freeze_states)
        ):
            out = [s async for s in agent._process_job_streaming(None, {})]
        # Upgrade attempted once; failed → freeze surfaced unchanged, no resume.
        assert out == freeze_states
        agent._perform_inprocess_workspace_upgrade.assert_awaited_once()
        agent._graph.aupdate_state.assert_not_called()
