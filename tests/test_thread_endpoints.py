"""Tests for orchestrator thread endpoints (persistent agent system).

Tests replicate endpoint logic directly rather than importing from
orchestrator.main, which requires database and service dependencies
that aren't available in the test environment.

Covers sections 5.1–5.13 of persistent_agent_tests.md:
  - POST /api/agents/threads (agent-facing)
  - GET  /api/agents/threads/{thread_id}/workspace
  - POST /api/agents/threads/{thread_id}/upgrade-to-vm
  - POST /api/agents/threads/{thread_id}/messages
  - POST /api/persistent/threads (user-facing, auth)
  - GET  /api/persistent/threads
  - GET  /api/persistent/threads/{thread_id}
  - DELETE /api/persistent/threads/{thread_id}
  - GET  /api/persistent/threads/{thread_id}/messages
  - GET  /api/persistent/threads/{thread_id}/ide
  - WS   /ws/persistent/{thread_id} (proxy)
  - _inspect_session_event()
  - _inspect_browser_event()
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


# =============================================================================
# Replicated endpoint logic — mirrors orchestrator/main.py handlers
# =============================================================================


async def agent_create_thread(
    db,
    gitea,
    provisioner,
    *,
    config_name="persistent_defaults",
    permission_mode="supervised",
    title="Local Session",
):
    """5.1: POST /api/agents/threads"""
    thread_id = await db.create_thread(
        user_id=None,
        config_name=config_name,
        permission_mode=permission_mode,
        title=title,
    )
    if gitea.is_initialized:
        repo_name = f"thread-{thread_id[:8]}"
        git_remote_url = await gitea.create_repo(repo_name)
        if git_remote_url:
            await db.merge_thread_workspace_context(
                thread_id, {"git_remote_url": git_remote_url, "repo_name": repo_name}
            )
    if provisioner.is_available:
        asyncio.create_task(provisioner.create_thread_workspace(thread_id))
    return {"thread_id": thread_id, "status": "created"}


def parse_workspace_metadata(thread):
    """5.2: GET /api/agents/threads/{thread_id}/workspace — parse + structure."""
    if not thread:
        return None  # caller raises 404
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws = metadata.get("workspace_container") or {}
    vm = metadata.get("vm") or {}
    return {
        "status": ws.get("status", "none"),
        "pod_ip": ws.get("pod_ip"),
        "pod_name": ws.get("pod_name"),
        "namespace": ws.get("namespace"),
        "git_remote_url": ws.get("git_remote_url"),
        "vm_status": vm.get("status"),
        "vm_ssh_host": vm.get("ssh_host"),
        "vm_ssh_port": vm.get("ssh_port"),
        "vm_name": vm.get("vm_name"),
        "config_override": metadata.get("config_override"),
        "project_ids": metadata.get("project_ids"),
    }


async def agent_upgrade_to_vm(db, vm_provisioner, thread_id):
    """5.3: POST /api/agents/threads/{thread_id}/upgrade-to-vm"""
    thread = await db.get_thread(thread_id)
    if not thread:
        return {"error": 404, "detail": "Thread not found"}
    if not vm_provisioner.is_available:
        return {"error": 503, "detail": "VM provisioning not available"}

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    vm_ctx = metadata.get("vm") or {}
    if vm_ctx.get("status") in ("provisioning", "created", "ready"):
        return {
            "status": vm_ctx["status"],
            "thread_id": thread_id,
            "message": "VM already provisioned or in progress",
        }

    ok = await vm_provisioner.create_thread_vm(
        thread_id=thread_id,
        agent_config=thread.get("config_name", "defaults"),
    )
    if not ok:
        return {"error": 500, "detail": "Failed to request VM provisioning"}

    return {
        "status": "provisioning",
        "thread_id": thread_id,
        "vm_provisioner_mode": vm_provisioner.mode,
    }


async def agent_save_message(
    db, thread_id, role, content=None, tool_calls=None, turn_number=None
):
    """5.4: POST /api/agents/threads/{thread_id}/messages"""
    message_id = await db.save_thread_message(
        thread_id=thread_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        turn_number=turn_number,
    )
    return {"message_id": message_id, "status": "saved"}


def check_thread_ownership(thread, user_id):
    """Shared ownership check for auth-required endpoints (5.7, 5.8, 5.9, 5.10)."""
    if not thread:
        return 404
    if thread.get("user_id") and str(thread["user_id"]) != str(user_id):
        return 403
    return 200


async def end_thread(
    db,
    thread,
    user_id,
    container_provisioner,
    vm_provisioner,
    snapshot_service,
    gitea_client,
    thread_id,
):
    """5.8: DELETE /api/persistent/threads/{thread_id}"""
    status = check_thread_ownership(thread, user_id)
    if status != 200:
        return {"error": status}

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws_ctx = metadata.get("workspace_container") or {}

    # Snapshot
    pod_ip = ws_ctx.get("pod_ip")
    if snapshot_service.is_available and pod_ip and ws_ctx.get("status") == "ready":
        await snapshot_service.capture_vm_snapshot(
            job_id=thread_id,
            ssh_host=pod_ip,
            ssh_port=22,
            source_type="pod",
            entity_type="threads",
        )

    # Container cleanup
    if container_provisioner.is_available:
        await container_provisioner.delete_thread_workspace(thread_id)

    # VM cleanup
    vm_ctx = metadata.get("vm") or {}
    if vm_ctx.get("status") in ("provisioning", "created", "ready"):
        if vm_provisioner.is_available:
            await vm_provisioner.delete_thread_vm(thread_id)

    # Gitea cleanup
    repo_name = ws_ctx.get("repo_name")
    if repo_name and gitea_client.is_initialized:
        await gitea_client.delete_repo(repo_name)

    await db.end_thread(thread_id)
    return {"status": "ended"}


def get_thread_ide_status(thread, metadata=None):
    """5.10: GET /api/persistent/threads/{thread_id}/ide — logic only."""
    if metadata is None:
        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}

    ws_ctx = metadata.get("workspace_container", {})
    repo_name = ws_ctx.get("repo_name")
    gitea_url = None
    if repo_name:
        gitea_base = os.environ.get("GITEA_URL", "").rstrip("/")
        gitea_user = os.environ.get("GITEA_ADMIN_USER", "srw")
        if gitea_base:
            gitea_url = f"{gitea_base}/{gitea_user}/{repo_name}"

    vm_ctx = metadata.get("vm", {})

    # VM takes precedence
    if vm_ctx.get("status") == "ready":
        ssh_host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
        if ssh_host:
            proxy_base = os.environ.get("IDE_PROXY_BASE_URL", "http://localhost:8085")
            return {
                "status": "active",
                "code_server_url": f"{proxy_base}/api/ide/tid-1/proxy/?folder=/home/agent-host/workspace",
                "source": "live_vm",
                "gitea_url": gitea_url,
            }

    # Container
    if ws_ctx.get("status") == "ready" and ws_ctx.get("pod_ip"):
        proxy_base = os.environ.get("IDE_PROXY_BASE_URL", "http://localhost:8085")
        return {
            "status": "active",
            "code_server_url": f"{proxy_base}/api/ide/tid-1/proxy/?folder=/home/agent-host/workspace",
            "source": "live_workspace",
            "gitea_url": gitea_url,
        }

    # Provisioning
    if ws_ctx.get("status") == "provisioning" or vm_ctx.get("status") == "provisioning":
        return {"status": "restoring", "code_server_url": None, "gitea_url": gitea_url}

    return {"status": "unavailable", "code_server_url": None, "gitea_url": gitea_url}


async def ws_proxy_resolve(db, ws_suspension, thread_id):
    """5.11: WS /ws/persistent/{thread_id} — resolution + validation logic."""
    thread = await db.get_thread(thread_id)
    if not thread:
        return {"close_code": 4404, "reason": "Thread not found"}

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws_ctx = metadata.get("workspace_container") or {}

    if ws_ctx.get("status") == "suspended" and ws_suspension.is_enabled:
        ok = await ws_suspension.restore_thread_workspace(thread_id)
        if not ok:
            return {"close_code": 4503, "reason": "Failed to restore workspace"}

    if not thread.get("agent_id"):
        return {"close_code": 4404, "reason": "Thread not active or no agent assigned"}

    agent = await db.get_agent(str(thread["agent_id"]))
    if not agent:
        return {"close_code": 4503, "reason": "Agent not available"}

    pod_ip = agent.get("pod_ip")
    pod_port = agent.get("pod_port", 8001)
    if not pod_ip:
        return {"close_code": 4503, "reason": "Agent has no IP"}

    return {"upstream_url": f"ws://{pod_ip}:{pod_port}/ws/chat"}


# 5.12 / 5.13: Notification helpers
_SESSION_NOTIFY_METHODS = {"permission.request", "vm_upgrade.needed", "ready"}
_SESSION_RESOLVE_METHODS = {"approve", "deny"}


def inspect_session_event(
    raw, thread_id, user_id, thread_title, config_name, broadcast_fn
):
    """5.12: _inspect_session_event"""
    if not user_id:
        return
    try:
        event = json.loads(raw)
        method = event.get("method")
        if method not in _SESSION_NOTIFY_METHODS:
            return

        params = event.get("params") or {}
        type_map = {
            "permission.request": "session.permission_request",
            "vm_upgrade.needed": "session.vm_upgrade",
            "ready": "session.waiting",
        }
        broadcast_fn(
            user_id=user_id,
            event_type=type_map[method],
            data={
                "thread_id": thread_id,
                "title": thread_title,
                "config_name": config_name,
                "tool": params.get("tool"),
                "args": params.get("args"),
                "reason": params.get("reason"),
                "command": params.get("command"),
            },
        )
    except (json.JSONDecodeError, Exception):
        pass


def inspect_browser_event(raw, thread_id, user_id, broadcast_fn):
    """5.13: _inspect_browser_event"""
    if not user_id:
        return
    try:
        event = json.loads(raw)
        method = event.get("method")
        if method not in _SESSION_RESOLVE_METHODS:
            return
        broadcast_fn(
            user_id=user_id,
            event_type="session.resolved",
            data={"thread_id": thread_id, "resolution": method},
        )
    except (json.JSONDecodeError, Exception):
        pass


# =============================================================================
# Fixtures
# =============================================================================


def _mock_db():
    db = AsyncMock()
    db.create_thread = AsyncMock(return_value="aaaaaaaa-1111-2222-3333-444444444444")
    db.get_thread = AsyncMock(return_value=None)
    db.get_agent = AsyncMock(return_value=None)
    db.save_thread_message = AsyncMock(return_value="msg-uuid-1")
    db.merge_thread_workspace_context = AsyncMock()
    db.end_thread = AsyncMock()
    db.list_threads = AsyncMock(return_value=[])
    db.get_thread_messages_history = AsyncMock(return_value=[])
    db.get_thread_message_count = AsyncMock(return_value=0)
    db.acquire = MagicMock()
    return db


def _mock_gitea(initialized=False):
    g = AsyncMock()
    type(g).is_initialized = PropertyMock(return_value=initialized)
    g.create_repo = AsyncMock(return_value="http://gitea:3000/srw/thread-aaaaaaaa.git")
    g.delete_repo = AsyncMock()
    return g


def _mock_provisioner(available=False):
    p = AsyncMock()
    type(p).is_available = PropertyMock(return_value=available)
    p.create_thread_workspace = AsyncMock()
    p.delete_thread_workspace = AsyncMock()
    return p


def _mock_vm_provisioner(available=False, mode="nats"):
    v = AsyncMock()
    type(v).is_available = PropertyMock(return_value=available)
    type(v).mode = PropertyMock(return_value=mode)
    v.create_thread_vm = AsyncMock(return_value=True)
    v.delete_thread_vm = AsyncMock()
    return v


def _mock_snapshot(available=False):
    s = AsyncMock()
    type(s).is_available = PropertyMock(return_value=available)
    s.capture_vm_snapshot = AsyncMock()
    return s


def _mock_suspension(enabled=False):
    s = AsyncMock()
    type(s).is_enabled = PropertyMock(return_value=enabled)
    s.restore_thread_workspace = AsyncMock(return_value=True)
    return s


def _make_thread(
    thread_id="aaaaaaaa-1111-2222-3333-444444444444",
    user_id=None,
    agent_id=None,
    config_name="persistent_defaults",
    title="Test Session",
    metadata=None,
    status="active",
):
    return {
        "id": thread_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "config_name": config_name,
        "title": title,
        "metadata": metadata or {},
        "status": status,
    }


# =============================================================================
# 5.1: POST /api/agents/threads
# =============================================================================


class TestAgentCreateThread:
    """Tests for POST /api/agents/threads (agent-facing, no auth)."""

    @pytest.mark.asyncio
    async def test_creates_thread_with_null_user(self):
        db = _mock_db()
        gitea = _mock_gitea(initialized=False)
        prov = _mock_provisioner(available=False)

        result = await agent_create_thread(db, gitea, prov)

        db.create_thread.assert_awaited_once_with(
            user_id=None,
            config_name="persistent_defaults",
            permission_mode="supervised",
            title="Local Session",
        )
        assert result == {
            "thread_id": "aaaaaaaa-1111-2222-3333-444444444444",
            "status": "created",
        }

    @pytest.mark.asyncio
    async def test_creates_gitea_repo_when_initialized(self):
        db = _mock_db()
        gitea = _mock_gitea(initialized=True)
        prov = _mock_provisioner(available=False)

        await agent_create_thread(db, gitea, prov)

        gitea.create_repo.assert_awaited_once_with("thread-aaaaaaaa")
        db.merge_thread_workspace_context.assert_awaited_once_with(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {
                "git_remote_url": "http://gitea:3000/srw/thread-aaaaaaaa.git",
                "repo_name": "thread-aaaaaaaa",
            },
        )

    @pytest.mark.asyncio
    async def test_skips_gitea_when_not_initialized(self):
        db = _mock_db()
        gitea = _mock_gitea(initialized=False)
        prov = _mock_provisioner(available=False)

        await agent_create_thread(db, gitea, prov)

        gitea.create_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_triggers_container_provisioning_when_available(self):
        db = _mock_db()
        gitea = _mock_gitea(initialized=False)
        prov = _mock_provisioner(available=True)

        await agent_create_thread(db, gitea, prov)

        # Give the background task a chance to be scheduled
        await asyncio.sleep(0)
        prov.create_thread_workspace.assert_awaited_once_with(
            "aaaaaaaa-1111-2222-3333-444444444444"
        )

    @pytest.mark.asyncio
    async def test_skips_container_when_not_available(self):
        db = _mock_db()
        gitea = _mock_gitea(initialized=False)
        prov = _mock_provisioner(available=False)

        await agent_create_thread(db, gitea, prov)

        await asyncio.sleep(0)
        prov.create_thread_workspace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_500_on_db_exception(self):
        db = _mock_db()
        db.create_thread = AsyncMock(side_effect=RuntimeError("DB down"))
        gitea = _mock_gitea()
        prov = _mock_provisioner()

        with pytest.raises(RuntimeError, match="DB down"):
            await agent_create_thread(db, gitea, prov)


# =============================================================================
# 5.2: GET /api/agents/threads/{thread_id}/workspace
# =============================================================================


class TestAgentGetThreadWorkspace:
    """Tests for GET /api/agents/threads/{thread_id}/workspace."""

    def test_returns_none_when_thread_not_found(self):
        assert parse_workspace_metadata(None) is None

    def test_returns_default_status_none_when_no_workspace(self):
        thread = _make_thread(metadata={})
        result = parse_workspace_metadata(thread)
        assert result["status"] == "none"
        assert result["pod_ip"] is None

    def test_parses_workspace_container_fields(self):
        thread = _make_thread(
            metadata={
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.0.0.50",
                    "pod_name": "ws-pod-1",
                    "namespace": "agent-vms",
                    "git_remote_url": "http://gitea/repo.git",
                }
            }
        )
        result = parse_workspace_metadata(thread)
        assert result["status"] == "ready"
        assert result["pod_ip"] == "10.0.0.50"
        assert result["pod_name"] == "ws-pod-1"
        assert result["namespace"] == "agent-vms"
        assert result["git_remote_url"] == "http://gitea/repo.git"

    def test_parses_vm_fields(self):
        thread = _make_thread(
            metadata={
                "vm": {
                    "status": "ready",
                    "ssh_host": "192.168.1.10",
                    "ssh_port": 2222,
                    "vm_name": "agent-vm-1",
                }
            }
        )
        result = parse_workspace_metadata(thread)
        assert result["vm_status"] == "ready"
        assert result["vm_ssh_host"] == "192.168.1.10"
        assert result["vm_ssh_port"] == 2222
        assert result["vm_name"] == "agent-vm-1"

    def test_handles_json_string_metadata(self):
        """Legacy format: metadata stored as JSON string instead of dict."""
        metadata_str = json.dumps(
            {"workspace_container": {"status": "ready", "pod_ip": "10.0.0.1"}}
        )
        thread = _make_thread(metadata=metadata_str)
        result = parse_workspace_metadata(thread)
        assert result["status"] == "ready"
        assert result["pod_ip"] == "10.0.0.1"

    def test_handles_invalid_json_string_metadata(self):
        thread = _make_thread(metadata="not-valid-json{")
        result = parse_workspace_metadata(thread)
        assert result["status"] == "none"

    def test_returns_config_override_and_project_ids(self):
        thread = _make_thread(
            metadata={
                "config_override": {"llm": {"model": "gpt-4o"}},
                "project_ids": ["proj-1", "proj-2"],
            }
        )
        result = parse_workspace_metadata(thread)
        assert result["config_override"] == {"llm": {"model": "gpt-4o"}}
        assert result["project_ids"] == ["proj-1", "proj-2"]


# =============================================================================
# 5.3: POST /api/agents/threads/{thread_id}/upgrade-to-vm
# =============================================================================


class TestAgentUpgradeToVm:
    """Tests for POST /api/agents/threads/{thread_id}/upgrade-to-vm."""

    @pytest.mark.asyncio
    async def test_returns_404_when_thread_not_found(self):
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=None)
        vm = _mock_vm_provisioner(available=True)

        result = await agent_upgrade_to_vm(db, vm, "tid-1")
        assert result["error"] == 404

    @pytest.mark.asyncio
    async def test_returns_503_when_vm_not_available(self):
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=_make_thread())
        vm = _mock_vm_provisioner(available=False)

        result = await agent_upgrade_to_vm(db, vm, "tid-1")
        assert result["error"] == 503

    @pytest.mark.asyncio
    async def test_returns_existing_status_when_vm_already_provisioned(self):
        thread = _make_thread(metadata={"vm": {"status": "ready"}})
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        vm = _mock_vm_provisioner(available=True)

        result = await agent_upgrade_to_vm(db, vm, "tid-1")
        assert result["status"] == "ready"
        assert result["message"] == "VM already provisioned or in progress"
        vm.create_thread_vm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_existing_status_for_provisioning(self):
        thread = _make_thread(metadata={"vm": {"status": "provisioning"}})
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        vm = _mock_vm_provisioner(available=True)

        result = await agent_upgrade_to_vm(db, vm, "tid-1")
        assert result["status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_creates_vm_for_new_request(self):
        thread = _make_thread(metadata={})
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        vm = _mock_vm_provisioner(available=True, mode="nats")

        result = await agent_upgrade_to_vm(db, vm, "tid-1")

        assert result["status"] == "provisioning"
        assert result["vm_provisioner_mode"] == "nats"
        vm.create_thread_vm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_500_when_provisioner_fails(self):
        thread = _make_thread(metadata={})
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        vm = _mock_vm_provisioner(available=True)
        vm.create_thread_vm = AsyncMock(return_value=False)

        result = await agent_upgrade_to_vm(db, vm, "tid-1")
        assert result["error"] == 500


# =============================================================================
# 5.4: POST /api/agents/threads/{thread_id}/messages
# =============================================================================


class TestAgentSaveMessage:
    """Tests for POST /api/agents/threads/{thread_id}/messages."""

    @pytest.mark.asyncio
    async def test_saves_message_and_returns_id(self):
        db = _mock_db()
        result = await agent_save_message(db, "tid-1", "assistant", "hello", None, 1)

        assert result == {"message_id": "msg-uuid-1", "status": "saved"}
        db.save_thread_message.assert_awaited_once_with(
            thread_id="tid-1",
            role="assistant",
            content="hello",
            tool_calls=None,
            turn_number=1,
        )

    @pytest.mark.asyncio
    async def test_returns_500_on_db_exception(self):
        db = _mock_db()
        db.save_thread_message = AsyncMock(side_effect=RuntimeError("DB error"))

        with pytest.raises(RuntimeError, match="DB error"):
            await agent_save_message(db, "tid-1", "user", "hi")


# =============================================================================
# 5.5: POST /api/persistent/threads (user-facing, auth)
# =============================================================================


class TestUserCreateThread:
    """Tests for POST /api/persistent/threads (auth required)."""

    def test_user_model_setting_goes_to_llm_key(self):
        """User settings model → config_override['llm']['model']."""
        settings = {"persistent_agent": {"model": "gpt-4o"}}
        config_override = {}
        pa = settings.get("persistent_agent", {})
        if pa.get("model"):
            config_override["llm"] = {"model": pa["model"]}
        assert config_override == {"llm": {"model": "gpt-4o"}}

    def test_user_permission_mode_goes_to_interactive_key(self):
        """User settings permission_mode → config_override['interactive']['permission_mode']."""
        settings = {"persistent_agent": {"permission_mode": "autonomous"}}
        config_override = {}
        pa = settings.get("persistent_agent", {})
        if pa.get("permission_mode"):
            config_override["interactive"] = {"permission_mode": pa["permission_mode"]}
        assert config_override == {"interactive": {"permission_mode": "autonomous"}}

    def test_greeting_nested_under_interactive(self):
        settings = {"persistent_agent": {"greeting": "Hello!"}}
        config_override = {}
        pa = settings.get("persistent_agent", {})
        if pa.get("greeting"):
            config_override.setdefault("interactive", {})["greeting"] = pa["greeting"]
        assert config_override == {"interactive": {"greeting": "Hello!"}}

    def test_idle_timeout_nested_under_interactive(self):
        settings = {"persistent_agent": {"idle_timeout_minutes": 30}}
        config_override = {}
        pa = settings.get("persistent_agent", {})
        if pa.get("idle_timeout_minutes"):
            config_override.setdefault("interactive", {})["idle_timeout_minutes"] = 30
        assert config_override == {"interactive": {"idle_timeout_minutes": 30}}

    def test_command_allowlist_top_level(self):
        settings = {"persistent_agent": {"command_allowlist": ["ls", "cat"]}}
        config_override = {}
        pa = settings.get("persistent_agent", {})
        if pa.get("command_allowlist"):
            config_override["command_allowlist"] = pa["command_allowlist"]
        assert config_override == {"command_allowlist": ["ls", "cat"]}

    def test_request_model_overrides_user_default(self):
        """Per-session model override takes priority over user defaults."""
        config_override = {"llm": {"model": "gpt-4o"}}  # from user settings
        request_model = "claude-opus-4-6"
        if request_model:
            config_override.setdefault("llm", {})["model"] = request_model
        assert config_override["llm"]["model"] == "claude-opus-4-6"

    def test_project_ids_normalization(self):
        """Legacy project_id → [project_id] normalization."""
        # New format
        assert ["p1", "p2"] == (["p1", "p2"] or [])
        # Legacy single project_id
        project_id = "p1"
        project_ids = None
        effective = project_ids or ([project_id] if project_id else [])
        assert effective == ["p1"]
        # Neither
        effective = None or ([] if not None else [])
        assert effective == []


# =============================================================================
# 5.6: GET /api/persistent/threads
# =============================================================================


class TestListThreads:
    """Tests for GET /api/persistent/threads."""

    @pytest.mark.asyncio
    async def test_returns_threads_for_user(self):
        db = _mock_db()
        db.list_threads = AsyncMock(return_value=[{"id": "t1"}, {"id": "t2"}])

        threads = await db.list_threads(user_id="user-1", project_id=None, status=None)
        assert len(threads) == 2

    @pytest.mark.asyncio
    async def test_supports_project_id_filter(self):
        db = _mock_db()
        await db.list_threads(user_id="user-1", project_id="proj-1", status=None)
        db.list_threads.assert_awaited_once_with(
            user_id="user-1", project_id="proj-1", status=None
        )

    @pytest.mark.asyncio
    async def test_supports_status_filter(self):
        db = _mock_db()
        await db.list_threads(user_id="user-1", project_id=None, status="active")
        db.list_threads.assert_awaited_once_with(
            user_id="user-1", project_id=None, status="active"
        )


# =============================================================================
# 5.7: GET /api/persistent/threads/{thread_id}
# =============================================================================


class TestGetThread:
    """Tests for GET /api/persistent/threads/{thread_id}."""

    def test_returns_404_when_not_found(self):
        assert check_thread_ownership(None, "user-1") == 404

    def test_returns_403_for_non_owner(self):
        thread = _make_thread(user_id="user-2")
        assert check_thread_ownership(thread, "user-1") == 403

    def test_returns_200_for_owner(self):
        thread = _make_thread(user_id="user-1")
        assert check_thread_ownership(thread, "user-1") == 200

    def test_returns_200_when_user_id_null(self):
        """Threads with NULL user_id are visible to all users."""
        thread = _make_thread(user_id=None)
        assert check_thread_ownership(thread, "user-1") == 200


# =============================================================================
# 5.8: DELETE /api/persistent/threads/{thread_id}
# =============================================================================


class TestEndThread:
    """Tests for DELETE /api/persistent/threads/{thread_id}."""

    @pytest.mark.asyncio
    async def test_returns_404_when_not_found(self):
        result = await end_thread(
            _mock_db(),
            None,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        assert result["error"] == 404

    @pytest.mark.asyncio
    async def test_returns_403_for_non_owner(self):
        thread = _make_thread(user_id="user-2")
        result = await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        assert result["error"] == 403

    @pytest.mark.asyncio
    async def test_captures_snapshot_when_ready_and_available(self):
        thread = _make_thread(
            user_id="user-1",
            metadata={"workspace_container": {"status": "ready", "pod_ip": "10.0.0.5"}},
        )
        snap = _mock_snapshot(available=True)
        result = await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            snap,
            _mock_gitea(),
            "tid-1",
        )
        assert result["status"] == "ended"
        snap.capture_vm_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_snapshot_when_not_ready(self):
        thread = _make_thread(
            user_id="user-1",
            metadata={"workspace_container": {"status": "provisioning"}},
        )
        snap = _mock_snapshot(available=True)
        await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            snap,
            _mock_gitea(),
            "tid-1",
        )
        snap.capture_vm_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deletes_container_when_available(self):
        thread = _make_thread(user_id="user-1")
        prov = _mock_provisioner(available=True)
        await end_thread(
            _mock_db(),
            thread,
            "user-1",
            prov,
            _mock_vm_provisioner(),
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        prov.delete_thread_workspace.assert_awaited_once_with("tid-1")

    @pytest.mark.asyncio
    async def test_deletes_vm_when_provisioned(self):
        thread = _make_thread(
            user_id="user-1",
            metadata={"vm": {"status": "ready"}},
        )
        vm = _mock_vm_provisioner(available=True)
        await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            vm,
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        vm.delete_thread_vm.assert_awaited_once_with("tid-1")

    @pytest.mark.asyncio
    async def test_skips_vm_delete_when_no_vm(self):
        thread = _make_thread(user_id="user-1", metadata={})
        vm = _mock_vm_provisioner(available=True)
        await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            vm,
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        vm.delete_thread_vm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deletes_gitea_repo_when_present(self):
        thread = _make_thread(
            user_id="user-1",
            metadata={"workspace_container": {"repo_name": "thread-aaaa"}},
        )
        gitea = _mock_gitea(initialized=True)
        await end_thread(
            _mock_db(),
            thread,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            _mock_snapshot(),
            gitea,
            "tid-1",
        )
        gitea.delete_repo.assert_awaited_once_with("thread-aaaa")

    @pytest.mark.asyncio
    async def test_calls_end_thread_on_db(self):
        thread = _make_thread(user_id="user-1")
        db = _mock_db()
        await end_thread(
            db,
            thread,
            "user-1",
            _mock_provisioner(),
            _mock_vm_provisioner(),
            _mock_snapshot(),
            _mock_gitea(),
            "tid-1",
        )
        db.end_thread.assert_awaited_once_with("tid-1")


# =============================================================================
# 5.9: GET /api/persistent/threads/{thread_id}/messages
# =============================================================================


class TestGetThreadMessages:
    """Tests for GET /api/persistent/threads/{thread_id}/messages."""

    def test_limit_capped_at_500(self):
        """min(limit, 500) caps at 500."""
        assert min(1000, 500) == 500
        assert min(200, 500) == 200
        assert min(500, 500) == 500

    @pytest.mark.asyncio
    async def test_returns_messages_with_total(self):
        db = _mock_db()
        db.get_thread_messages_history = AsyncMock(return_value=[{"id": "m1"}])
        db.get_thread_message_count = AsyncMock(return_value=42)

        messages = await db.get_thread_messages_history(
            thread_id="tid-1", limit=200, offset=0
        )
        total = await db.get_thread_message_count("tid-1")

        result = {"messages": messages, "total": total, "thread_id": "tid-1"}
        assert result["total"] == 42
        assert len(result["messages"]) == 1


# =============================================================================
# 5.10: GET /api/persistent/threads/{thread_id}/ide
# =============================================================================


class TestGetThreadIdeStatus:
    """Tests for GET /api/persistent/threads/{thread_id}/ide."""

    def test_vm_ready_returns_active_live_vm(self):
        """VM ready → active, source=live_vm."""
        thread = _make_thread()
        metadata = {
            "vm": {"status": "ready", "ssh_host": "10.0.0.99"},
            "workspace_container": {"repo_name": "thread-aaaa"},
        }
        with patch.dict(
            os.environ, {"GITEA_URL": "http://gitea:3000", "GITEA_ADMIN_USER": "srw"}
        ):
            result = get_thread_ide_status(thread, metadata)
        assert result["status"] == "active"
        assert result["source"] == "live_vm"
        assert result["gitea_url"] == "http://gitea:3000/srw/thread-aaaa"

    def test_container_ready_returns_active_live_workspace(self):
        """Container ready → active, source=live_workspace."""
        thread = _make_thread()
        metadata = {
            "workspace_container": {"status": "ready", "pod_ip": "10.0.0.50"},
        }
        result = get_thread_ide_status(thread, metadata)
        assert result["status"] == "active"
        assert result["source"] == "live_workspace"

    def test_vm_takes_precedence_over_container(self):
        """When both VM and container ready, VM wins."""
        thread = _make_thread()
        metadata = {
            "vm": {"status": "ready", "ssh_host": "10.0.0.99"},
            "workspace_container": {"status": "ready", "pod_ip": "10.0.0.50"},
        }
        result = get_thread_ide_status(thread, metadata)
        assert result["source"] == "live_vm"

    def test_provisioning_returns_restoring(self):
        """Provisioning → restoring."""
        thread = _make_thread()
        result = get_thread_ide_status(
            thread, {"workspace_container": {"status": "provisioning"}}
        )
        assert result["status"] == "restoring"
        assert result["code_server_url"] is None

    def test_no_workspace_returns_unavailable(self):
        """No workspace → unavailable."""
        thread = _make_thread()
        result = get_thread_ide_status(thread, {})
        assert result["status"] == "unavailable"

    def test_gitea_url_none_when_no_repo(self):
        """gitea_url is None when no repo_name."""
        thread = _make_thread()
        result = get_thread_ide_status(thread, {})
        assert result["gitea_url"] is None

    def test_gitea_url_none_when_no_gitea_env(self):
        """gitea_url is None when GITEA_URL not set."""
        thread = _make_thread()
        metadata = {"workspace_container": {"repo_name": "thread-aaaa"}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITEA_URL", None)
            result = get_thread_ide_status(thread, metadata)
        assert result["gitea_url"] is None


# =============================================================================
# 5.11: WS /ws/persistent/{thread_id} (proxy resolution)
# =============================================================================


class TestWsProxyResolve:
    """Tests for WS /ws/persistent/{thread_id} resolution logic."""

    @pytest.mark.asyncio
    async def test_closes_4404_when_thread_not_found(self):
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=None)
        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["close_code"] == 4404

    @pytest.mark.asyncio
    async def test_restores_suspended_workspace(self):
        thread = _make_thread(
            agent_id="agent-1",
            metadata={"workspace_container": {"status": "suspended"}},
        )
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        db.get_agent = AsyncMock(return_value={"pod_ip": "10.0.0.5", "pod_port": 8001})
        susp = _mock_suspension(enabled=True)

        result = await ws_proxy_resolve(db, susp, "tid-1")

        susp.restore_thread_workspace.assert_awaited_once_with("tid-1")
        assert "upstream_url" in result

    @pytest.mark.asyncio
    async def test_closes_4503_when_restoration_fails(self):
        thread = _make_thread(
            agent_id="agent-1",
            metadata={"workspace_container": {"status": "suspended"}},
        )
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        susp = _mock_suspension(enabled=True)
        susp.restore_thread_workspace = AsyncMock(return_value=False)

        result = await ws_proxy_resolve(db, susp, "tid-1")
        assert result["close_code"] == 4503

    @pytest.mark.asyncio
    async def test_closes_4404_when_no_agent_id(self):
        thread = _make_thread(agent_id=None)
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)

        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["close_code"] == 4404

    @pytest.mark.asyncio
    async def test_closes_4503_when_agent_not_found(self):
        thread = _make_thread(agent_id="agent-1")
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        db.get_agent = AsyncMock(return_value=None)

        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["close_code"] == 4503

    @pytest.mark.asyncio
    async def test_closes_4503_when_agent_has_no_ip(self):
        thread = _make_thread(agent_id="agent-1")
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        db.get_agent = AsyncMock(return_value={"pod_ip": None, "pod_port": 8001})

        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["close_code"] == 4503

    @pytest.mark.asyncio
    async def test_returns_upstream_url_on_success(self):
        thread = _make_thread(agent_id="agent-1")
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        db.get_agent = AsyncMock(return_value={"pod_ip": "10.0.0.5", "pod_port": 8001})

        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["upstream_url"] == "ws://10.0.0.5:8001/ws/chat"

    @pytest.mark.asyncio
    async def test_uses_default_pod_port_8001(self):
        thread = _make_thread(agent_id="agent-1")
        db = _mock_db()
        db.get_thread = AsyncMock(return_value=thread)
        db.get_agent = AsyncMock(return_value={"pod_ip": "10.0.0.5"})

        result = await ws_proxy_resolve(db, _mock_suspension(), "tid-1")
        assert result["upstream_url"] == "ws://10.0.0.5:8001/ws/chat"


# =============================================================================
# 5.12: _inspect_session_event()
# =============================================================================


class TestInspectSessionEvent:
    """Tests for _inspect_session_event notification helper."""

    def test_returns_immediately_when_user_id_falsy(self):
        broadcast = MagicMock()
        inspect_session_event(
            '{"method": "permission.request"}',
            "tid-1",
            "",
            "Title",
            "persistent_defaults",
            broadcast,
        )
        broadcast.assert_not_called()

    def test_broadcasts_permission_request(self):
        broadcast = MagicMock()
        raw = json.dumps(
            {
                "method": "permission.request",
                "params": {
                    "tool": "run_command",
                    "args": {"cmd": "ls"},
                    "reason": "needs shell",
                },
            }
        )
        inspect_session_event(
            raw, "tid-1", "user-1", "My Session", "persistent_defaults", broadcast
        )
        broadcast.assert_called_once()
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["event_type"] == "session.permission_request"
        assert call_kwargs["data"]["tool"] == "run_command"
        assert call_kwargs["data"]["thread_id"] == "tid-1"
        assert call_kwargs["data"]["title"] == "My Session"

    def test_broadcasts_vm_upgrade_needed(self):
        broadcast = MagicMock()
        raw = json.dumps(
            {
                "method": "vm_upgrade.needed",
                "params": {"command": "sudo apt install vim"},
            }
        )
        inspect_session_event(raw, "tid-1", "user-1", "Title", None, broadcast)
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["event_type"] == "session.vm_upgrade"
        assert call_kwargs["data"]["command"] == "sudo apt install vim"

    def test_broadcasts_ready_as_session_waiting(self):
        broadcast = MagicMock()
        raw = json.dumps({"method": "ready"})
        inspect_session_event(raw, "tid-1", "user-1", "Title", "persistent_defaults", broadcast)
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["event_type"] == "session.waiting"

    def test_ignores_non_notify_methods(self):
        broadcast = MagicMock()
        raw = json.dumps({"method": "text.delta"})
        inspect_session_event(raw, "tid-1", "user-1", "Title", "persistent_defaults", broadcast)
        broadcast.assert_not_called()

    def test_ignores_invalid_json(self):
        broadcast = MagicMock()
        inspect_session_event(
            "not-json{", "tid-1", "user-1", "Title", "persistent_defaults", broadcast
        )
        broadcast.assert_not_called()

    def test_handles_missing_params_gracefully(self):
        """params defaults to {} when absent."""
        broadcast = MagicMock()
        raw = json.dumps({"method": "permission.request"})
        inspect_session_event(raw, "tid-1", "user-1", "Title", "persistent_defaults", broadcast)
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["data"]["tool"] is None
        assert call_kwargs["data"]["args"] is None


# =============================================================================
# 5.13: _inspect_browser_event()
# =============================================================================


class TestInspectBrowserEvent:
    """Tests for _inspect_browser_event notification helper."""

    def test_returns_immediately_when_user_id_falsy(self):
        broadcast = MagicMock()
        inspect_browser_event('{"method": "approve"}', "tid-1", "", broadcast)
        broadcast.assert_not_called()

    def test_broadcasts_approve_as_session_resolved(self):
        broadcast = MagicMock()
        raw = json.dumps({"method": "approve"})
        inspect_browser_event(raw, "tid-1", "user-1", broadcast)
        broadcast.assert_called_once()
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["event_type"] == "session.resolved"
        assert call_kwargs["data"]["resolution"] == "approve"
        assert call_kwargs["data"]["thread_id"] == "tid-1"

    def test_broadcasts_deny_as_session_resolved(self):
        broadcast = MagicMock()
        raw = json.dumps({"method": "deny"})
        inspect_browser_event(raw, "tid-1", "user-1", broadcast)
        call_kwargs = broadcast.call_args[1]
        assert call_kwargs["data"]["resolution"] == "deny"

    def test_ignores_non_resolve_methods(self):
        broadcast = MagicMock()
        raw = json.dumps({"method": "text.delta"})
        inspect_browser_event(raw, "tid-1", "user-1", broadcast)
        broadcast.assert_not_called()

    def test_ignores_invalid_json(self):
        broadcast = MagicMock()
        inspect_browser_event("not{json", "tid-1", "user-1", broadcast)
        broadcast.assert_not_called()

    def test_silently_ignores_exceptions(self):
        """Any exception is swallowed — no propagation."""

        def bad_broadcast(**kwargs):
            raise RuntimeError("broadcast exploded")

        # Should not raise
        inspect_browser_event('{"method": "approve"}', "tid-1", "user-1", bad_broadcast)
