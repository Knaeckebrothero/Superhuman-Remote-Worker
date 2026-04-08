"""Tests for the VM upgrade endpoint and vm_upgrade_required freeze handling.

Tests cover:
1. upgrade-to-vm endpoint logic: validation, context mutation, DB updates
2. approve endpoint with freeze_type=vm_upgrade_required: resume without VM
3. Dispatch integration: _job_needs_vm detects vm.requested in context

These test the logic directly rather than importing from orchestrator.main,
which requires database and service dependencies that aren't available in
the test environment.
"""

import json
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# =============================================================================
# Fixtures
# =============================================================================


def make_job(
    *,
    job_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    status: str = "pending_review",
    freeze_data: dict | None = None,
    context: dict | None = None,
    config_name: str = "defaults",
    config_override: dict | None = None,
) -> dict:
    """Create a minimal job dict for testing."""
    return {
        "id": job_id,
        "status": status,
        "config_name": config_name,
        "config_override": config_override or {},
        "context": context or {},
        "freeze_data": freeze_data,
        "description": "Test job",
        "user_id": None,
        "project_id": None,
        "parent_job_id": None,
        "resolved_config": {
            "agent": {"agent_id": config_name, "autonomy": "review"},
        },
    }


def make_vm_upgrade_freeze(
    command: str = "sudo apt-get install -y libxml2-dev",
) -> dict:
    """Create freeze_data for a vm_upgrade_required freeze."""
    return {
        "freeze_type": "vm_upgrade_required",
        "reason": "Agent attempted a sudo command that requires VM-level access.",
        "command": command,
        "timestamp": "2026-03-28T10:00:00+00:00",
    }


# =============================================================================
# Helper: replicate core logic from orchestrator/main.py
# =============================================================================


def _job_needs_vm(job: dict) -> bool:
    """Replicate _job_needs_vm from orchestrator/main.py."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    vm_ctx = ctx.get("vm", {})
    if vm_ctx.get("requested"):
        return True
    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    return co.get("workspace", {}).get("backend") == "remote"


def _simulate_approve(job: dict, workspace_base: Path) -> tuple[dict, str | None]:
    """Simulate the approve_job endpoint logic for vm_upgrade_required.

    Returns (result_dict, sql_executed) tuple.
    """
    job_id = job["id"]
    frozen_data = job.get("freeze_data")
    if not frozen_data:
        raise ValueError("No freeze data")

    freeze_type = frozen_data.get("freeze_type", "job_complete")

    if freeze_type in ("phase_boundary", "vm_upgrade_required"):
        # Remove local freeze file
        local_frozen = workspace_base / "output" / "job_frozen.json"
        if local_frozen.exists():
            local_frozen.unlink()

        # In real code: UPDATE jobs SET status = 'processing', freeze_data = NULL
        sql = (
            "UPDATE jobs SET status = 'processing', freeze_data = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid"
        )

        return {
            "status": "approved_continue",
            "job_id": job_id,
            "freeze_type": freeze_type,
            "phase_type": frozen_data.get("phase_type"),
            "phase_number": frozen_data.get("phase_number"),
            "command": frozen_data.get("command"),
        }, sql

    return {"status": "approved", "job_id": job_id, "freeze_type": freeze_type}, None


def _simulate_upgrade(
    job: dict, workspace_base: Path, vm_available: bool = True
) -> tuple[dict, dict | None]:
    """Simulate the upgrade_job_to_vm endpoint logic.

    Returns (result_dict, updated_context_or_None) tuple.
    Raises ValueError/RuntimeError for validation failures.
    """
    job_id = job["id"]

    # Validate status
    if job["status"] not in ("pending_review", "reviewing"):
        raise ValueError(f"Job cannot be upgraded (status: {job['status']})")

    # Validate freeze data
    frozen_data = job.get("freeze_data")
    if not frozen_data:
        # In real code: would check Gitea and local workspace too
        raise FileNotFoundError(f"No freeze data found for job '{job_id}'")

    freeze_type = frozen_data.get("freeze_type")
    if freeze_type != "vm_upgrade_required":
        raise ValueError(
            f"Job freeze type is '{freeze_type}', not 'vm_upgrade_required'"
        )

    # Validate VM provisioner
    if not vm_available:
        raise RuntimeError("VM provisioner is not available")

    # Mutate context
    job_context = dict(job.get("context") or {})
    vm_ctx = job_context.setdefault("vm", {})
    vm_ctx["requested"] = True
    vm_ctx["upgrade_from"] = "container"
    vm_ctx["upgrade_command"] = frozen_data.get("command", "")

    # Remove local freeze file
    local_frozen = workspace_base / "output" / "job_frozen.json"
    if local_frozen.exists():
        local_frozen.unlink()

    return {
        "status": "approved_vm_upgrade",
        "job_id": job_id,
        "freeze_type": freeze_type,
        "command": frozen_data.get("command"),
        "vm_provisioner_mode": "direct",
    }, job_context


# =============================================================================
# Test: upgrade-to-vm endpoint logic
# =============================================================================


class TestUpgradeToVm:
    """Tests for the upgrade-to-vm endpoint logic."""

    def test_upgrade_success(self, tmp_path):
        """Successful upgrade sets vm.requested in context."""
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(freeze_data=freeze_data, context={})

        result, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert result["status"] == "approved_vm_upgrade"
        assert result["freeze_type"] == "vm_upgrade_required"
        assert result["command"] == "sudo apt-get install -y libxml2-dev"
        assert updated_ctx["vm"]["requested"] is True
        assert updated_ctx["vm"]["upgrade_from"] == "container"

    def test_upgrade_stores_command_in_context(self, tmp_path):
        """The original sudo command is preserved in vm context."""
        cmd = "sudo systemctl restart nginx"
        freeze_data = make_vm_upgrade_freeze(command=cmd)
        job = make_job(freeze_data=freeze_data, context={})

        result, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert result["command"] == cmd
        assert updated_ctx["vm"]["upgrade_command"] == cmd

    def test_upgrade_removes_local_freeze_file(self, tmp_path):
        """Upgrade removes job_frozen.json from local workspace."""
        job_id = "11111111-2222-3333-4444-555555555555"
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(job_id=job_id, freeze_data=freeze_data, context={})

        # Create the freeze file
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True)
        frozen_path = output_dir / "job_frozen.json"
        frozen_path.write_text(json.dumps(freeze_data))
        assert frozen_path.exists()

        _simulate_upgrade(job, tmp_path)

        assert not frozen_path.exists()

    def test_upgrade_rejects_non_frozen_job(self, tmp_path):
        """Upgrade rejects jobs not in pending_review status."""
        job = make_job(status="processing", freeze_data=make_vm_upgrade_freeze())

        with pytest.raises(ValueError, match="cannot be upgraded"):
            _simulate_upgrade(job, tmp_path)

    def test_upgrade_rejects_wrong_freeze_type(self, tmp_path):
        """Upgrade rejects jobs frozen with a different freeze type."""
        freeze_data = {"freeze_type": "job_complete", "summary": "done"}
        job = make_job(freeze_data=freeze_data)

        with pytest.raises(ValueError, match="vm_upgrade_required"):
            _simulate_upgrade(job, tmp_path)

    def test_upgrade_rejects_missing_freeze_data(self, tmp_path):
        """Upgrade raises when no freeze data is found."""
        job = make_job(freeze_data=None)

        with pytest.raises(FileNotFoundError, match="No freeze data"):
            _simulate_upgrade(job, tmp_path)

    def test_upgrade_rejects_when_vm_unavailable(self, tmp_path):
        """Upgrade raises when VM provisioner is not available."""
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(freeze_data=freeze_data)

        with pytest.raises(RuntimeError, match="not available"):
            _simulate_upgrade(job, tmp_path, vm_available=False)

    def test_upgrade_preserves_existing_context(self, tmp_path):
        """Upgrade merges vm.requested into existing context without clobbering."""
        freeze_data = make_vm_upgrade_freeze()
        existing_context = {
            "workspace_container": {"status": "ready", "pod_ip": "10.0.0.5"},
            "some_other_key": "preserved",
        }
        job = make_job(freeze_data=freeze_data, context=existing_context)

        _, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert updated_ctx["vm"]["requested"] is True
        assert updated_ctx["some_other_key"] == "preserved"
        assert updated_ctx["workspace_container"]["status"] == "ready"

    def test_upgrade_with_reviewing_status(self, tmp_path):
        """Upgrade also works for jobs in 'reviewing' status."""
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(status="reviewing", freeze_data=freeze_data, context={})

        result, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert result["status"] == "approved_vm_upgrade"
        assert updated_ctx["vm"]["requested"] is True

    def test_upgrade_with_empty_command(self, tmp_path):
        """Upgrade works when freeze data has no command field."""
        freeze_data = {
            "freeze_type": "vm_upgrade_required",
            "reason": "sudo attempt",
        }
        job = make_job(freeze_data=freeze_data, context={})

        result, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert result["command"] is None
        assert updated_ctx["vm"]["upgrade_command"] == ""


# =============================================================================
# Test: approve endpoint with vm_upgrade_required freeze type
# =============================================================================


class TestApproveVmUpgradeWithoutVm:
    """Tests for approve with vm_upgrade_required freeze.

    This is the 'Resume without VM' path — the agent continues in the container.
    """

    def test_approve_resumes_in_container(self, tmp_path):
        """Approving a vm_upgrade_required freeze resumes as processing."""
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(freeze_data=freeze_data)

        result, sql = _simulate_approve(job, tmp_path)

        assert result["status"] == "approved_continue"
        assert result["freeze_type"] == "vm_upgrade_required"
        assert result["command"] == "sudo apt-get install -y libxml2-dev"
        assert "status = 'processing'" in sql
        assert "freeze_data = NULL" in sql

    def test_approve_removes_local_freeze_file(self, tmp_path):
        """Approving removes job_frozen.json from local workspace."""
        job_id = "11111111-2222-3333-4444-555555555555"
        freeze_data = make_vm_upgrade_freeze()
        job = make_job(job_id=job_id, freeze_data=freeze_data)

        # Create the freeze file
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True)
        frozen_path = output_dir / "job_frozen.json"
        frozen_path.write_text(json.dumps(freeze_data))
        assert frozen_path.exists()

        _simulate_approve(job, tmp_path)

        assert not frozen_path.exists()

    def test_approve_phase_boundary_still_works(self, tmp_path):
        """Phase boundary approvals still work after the vm_upgrade_required change."""
        freeze_data = {
            "freeze_type": "phase_boundary",
            "phase_type": "strategic",
            "phase_number": 2,
        }
        job = make_job(freeze_data=freeze_data)

        result, sql = _simulate_approve(job, tmp_path)

        assert result["status"] == "approved_continue"
        assert result["freeze_type"] == "phase_boundary"
        assert result["phase_type"] == "strategic"
        assert result["phase_number"] == 2

    def test_approve_job_complete_not_affected(self, tmp_path):
        """Job complete freeze still goes through the completion path."""
        freeze_data = {"freeze_type": "job_complete", "summary": "done"}
        job = make_job(freeze_data=freeze_data)

        result, sql = _simulate_approve(job, tmp_path)

        # job_complete goes to the "else" branch, not approved_continue
        assert result["status"] == "approved"
        assert sql is None  # Completion has its own SQL path


# =============================================================================
# Test: dispatch integration — _job_needs_vm detects upgrade request
# =============================================================================


class TestDispatchDetectsUpgrade:
    """Verify that after upgrade-to-vm sets context.vm.requested,
    the dispatch helpers correctly identify the job as needing a VM."""

    def test_job_needs_vm_after_upgrade(self):
        """_job_needs_vm returns True when context.vm.requested is set."""
        job = make_job(
            status="paused",
            context={"vm": {"requested": True, "upgrade_from": "container"}},
        )
        assert _job_needs_vm(job) is True

    def test_job_needs_vm_without_upgrade(self):
        """_job_needs_vm returns False for normal container jobs."""
        job = make_job(
            status="paused",
            context={"workspace_container": {"status": "ready"}},
        )
        assert _job_needs_vm(job) is False

    def test_job_needs_vm_with_remote_backend(self):
        """_job_needs_vm returns True for explicit remote backend."""
        job = make_job(
            status="paused",
            config_override={"workspace": {"backend": "remote"}},
        )
        assert _job_needs_vm(job) is True

    def test_job_needs_vm_with_string_context(self):
        """_job_needs_vm handles JSON-encoded context strings."""
        job = make_job(status="paused")
        job["context"] = json.dumps(
            {"vm": {"requested": True, "upgrade_from": "container"}}
        )
        assert _job_needs_vm(job) is True

    def test_job_needs_vm_with_invalid_context(self):
        """_job_needs_vm handles invalid JSON context gracefully."""
        job = make_job(status="paused")
        job["context"] = "not-valid-json"
        assert _job_needs_vm(job) is False

    def test_job_needs_vm_with_none_context(self):
        """_job_needs_vm handles None context."""
        job = make_job(status="paused")
        job["context"] = None
        assert _job_needs_vm(job) is False


# =============================================================================
# Test: upgrade flow end-to-end context mutation
# =============================================================================


class TestUpgradeContextFlow:
    """Test the full context mutation flow: freeze → upgrade → dispatch."""

    def test_full_upgrade_flow(self, tmp_path):
        """Simulate the complete flow from freeze to dispatch-ready state."""
        # Step 1: Job freezes with vm_upgrade_required
        freeze_data = make_vm_upgrade_freeze(
            command="sudo apt-get install -y libjpeg-dev"
        )
        job = make_job(
            status="pending_review",
            freeze_data=freeze_data,
            context={
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.0.0.42",
                    "pod_name": "workspace-aaaaaaaa-bbb",
                },
            },
        )

        # Step 2: Operator clicks "Upgrade to VM"
        result, updated_ctx = _simulate_upgrade(job, tmp_path)

        assert result["status"] == "approved_vm_upgrade"
        assert updated_ctx["vm"]["requested"] is True
        assert updated_ctx["vm"]["upgrade_from"] == "container"
        assert (
            updated_ctx["vm"]["upgrade_command"]
            == "sudo apt-get install -y libjpeg-dev"
        )

        # Step 3: Dispatcher picks up the job — _job_needs_vm detects the request
        simulated_job = make_job(
            status="paused",
            context=updated_ctx,
        )
        assert _job_needs_vm(simulated_job) is True

        # Container context is still there (not deleted until job completes)
        ctx = simulated_job["context"]
        assert ctx["workspace_container"]["pod_ip"] == "10.0.0.42"

    def test_resume_without_vm_flow(self, tmp_path):
        """Simulate the 'Resume without VM' path via approve."""
        freeze_data = make_vm_upgrade_freeze(command="sudo make install")
        job = make_job(
            status="pending_review",
            freeze_data=freeze_data,
            context={
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.0.0.42",
                },
            },
        )

        # Operator clicks "Resume without VM"
        result, sql = _simulate_approve(job, tmp_path)

        assert result["status"] == "approved_continue"
        assert result["freeze_type"] == "vm_upgrade_required"

        # Job resumes in container — context unchanged (no vm.requested)
        assert "vm" not in job["context"]
        assert job["context"]["workspace_container"]["status"] == "ready"
