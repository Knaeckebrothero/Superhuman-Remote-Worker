"""Pins for the session config_name plumbing + detach-then-delete fixes.

Covers the three holes found during the memory-overhaul Phase-1 closure
step 1 (2026-06-11):

- Hole A: a bare ``POST /api/persistent/threads`` must land on the
  persistent base config, not the worker one
  (docs/issues/session_config_name_plumbing.md).
- Hole B: the idle-pool ``/session/attach`` path must carry the thread's
  config_name (orchestrator side) and resolve it as the session base
  (agent side) — otherwise pool-attached sessions bind the worker memory
  pipeline and silently lose ``teardown_extractor``.
- B11 k8s route: the user-facing thread DELETE must give a live session
  agent the chance to terminate (final memory capture + git push) before
  the workspace and pod are torn down (memory_bugs.md B11 addendum).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import orchestrator.main as orch_main
from src.api.persistent_app import _load_expert_config
from src.core.session_tool_overrides import (
    SessionToolOverrideError,
    validate_session_tool_overrides,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in recording the last POST."""

    calls: list = []
    response_status: int = 200
    raise_on_post: Exception | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        if _FakeAsyncClient.raise_on_post is not None:
            raise _FakeAsyncClient.raise_on_post
        _FakeAsyncClient.calls.append({"url": url, "json": json})
        return _FakeResponse(_FakeAsyncClient.response_status)


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response_status = 200
    _FakeAsyncClient.raise_on_post = None
    yield


class TestThreadCreateDefault:
    """Hole A: the request-model default."""

    def test_bare_thread_create_defaults_to_persistent_config(self):
        assert orch_main.ThreadCreateRequest().config_name == "persistent_defaults"


class TestSessionWorkspaceBackendOverride:
    """The New Session 'Backend' selector must reach create_thread.

    Regression pin for the dropped-backend bug (found 2026-06-20 live-testing
    the workspace-tier-upgrade feature): ThreadCreateRequest declared no
    config_override field, so the cockpit's
    ``{"config_override": {"workspace": {"backend": "virtual"}}}`` was silently
    discarded by Pydantic and every session booted the default (sandbox) —
    making lite/VM sessions uncreatable from the UI.
    """

    def test_request_model_accepts_config_override(self):
        req = orch_main.ThreadCreateRequest(
            config_override={"workspace": {"backend": "virtual"}}
        )
        assert req.config_override == {"workspace": {"backend": "virtual"}}

    def test_bare_request_has_no_config_override(self):
        assert orch_main.ThreadCreateRequest().config_override is None

    @pytest.mark.parametrize("backend", ["sandbox", "virtual", "none"])
    def test_creatable_backends_pass_through(self, backend):
        ws = orch_main._validated_session_workspace_override(
            {"workspace": {"backend": backend, "max_read_words": 5}}
        )
        assert ws == {"backend": backend, "max_read_words": 5}

    def test_vm_backend_rejected_at_create(self):
        # create_thread has no VM-provisioner wiring — vm must be reached by
        # starting lite and upgrading, not selected at creation.
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_workspace_override(
                {"workspace": {"backend": "vm"}}
            )
        assert exc.value.status_code == 400
        assert "upgrade" in exc.value.detail.lower()

    def test_unknown_backend_rejected(self):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_workspace_override(
                {"workspace": {"backend": "bogus"}}
            )
        assert exc.value.status_code == 400

    def test_absent_fragment_returns_none(self):
        assert orch_main._validated_session_workspace_override(None) is None
        assert orch_main._validated_session_workspace_override({}) is None
        assert (
            orch_main._validated_session_workspace_override({"llm": {"model": "m"}})
            is None
        )

    def test_workspace_without_backend_passes_through(self):
        # Word-limit-only tweaks (no tier change) are honored, not rejected.
        ws = orch_main._validated_session_workspace_override(
            {"workspace": {"max_read_words": 10}}
        )
        assert ws == {"max_read_words": 10}


class TestSessionWorkspaceBackendDefaultChain:
    """Instant-landing defaults chain (docs/features/instant_landing_session.md):
    explicit request > owner's saved ``persistent_agent.workspace_backend`` >
    platform default (virtual). Sessions are never implicitly sandbox."""

    def test_platform_default_is_virtual(self):
        assert orch_main.SESSION_DEFAULT_WORKSPACE_BACKEND == "virtual"
        assert (
            orch_main.SESSION_DEFAULT_WORKSPACE_BACKEND
            in orch_main.SESSION_WORKSPACE_BACKENDS
        )

    def test_no_settings_falls_back_to_platform_default(self):
        assert orch_main._default_session_workspace_backend({}) == "virtual"
        assert orch_main._default_session_workspace_backend(None) == "virtual"

    @pytest.mark.parametrize("backend", ["sandbox", "virtual", "none"])
    def test_saved_user_default_wins_over_platform_default(self, backend):
        assert (
            orch_main._default_session_workspace_backend({"workspace_backend": backend})
            == backend
        )

    @pytest.mark.parametrize("junk", ["vm", "bogus", "", 3, {"backend": "vm"}])
    def test_junk_saved_value_falls_back_to_platform_default(self, junk):
        # Legacy/hand-edited settings rows must not brick session creation.
        assert (
            orch_main._default_session_workspace_backend({"workspace_backend": junk})
            == "virtual"
        )

    def test_settings_patch_accepts_valid_workspace_backend(self):
        upd = orch_main.UserSettingsUpdate(
            persistent_agent={"workspace_backend": "sandbox", "model": "m"}
        )
        assert upd.persistent_agent == {"workspace_backend": "sandbox", "model": "m"}

    @pytest.mark.parametrize("bad", ["vm", "bogus", ""])
    def test_settings_patch_rejects_invalid_workspace_backend(self, bad):
        with pytest.raises(ValueError):
            orch_main.UserSettingsUpdate(persistent_agent={"workspace_backend": bad})

    def test_settings_patch_leaves_other_keys_free_form(self):
        # Phase 6 contract: persistent_agent stays a free dict for other keys.
        upd = orch_main.UserSettingsUpdate(
            persistent_agent={"headless_mode": "eager", "greeting": "hi"}
        )
        assert upd.persistent_agent == {"headless_mode": "eager", "greeting": "hi"}

    def test_resolved_preference_defaults_surface_workspace_backend(self):
        # The Settings UI shows the resolved system default as the placeholder;
        # it must match what create_thread will actually apply.
        import asyncio

        with patch.object(
            orch_main,
            "postgres_db",
            SimpleNamespace(
                resolve_default_for_capability=AsyncMock(return_value=None)
            ),
        ):
            resolved = asyncio.run(orch_main._resolve_preference_defaults())
        assert (
            resolved["persistent_agent"]["workspace_backend"]
            == orch_main.SESSION_DEFAULT_WORKSPACE_BACKEND
        )

    def test_fleet_management_tools_override_passes_through(self):
        tools = orch_main._validated_session_fleet_tools_override(
            {"tools": {"orchestrator": []}}
        )
        assert tools == []

    def test_session_tool_group_overrides_pass_through(self):
        tools = orch_main._validated_session_tool_overrides(
            {"tools": {"orchestrator": [], "agent_catalog": [], "workflows": []}}
        )
        assert tools == {"orchestrator": [], "agent_catalog": [], "workflows": []}

    def test_absent_fleet_management_tools_override_returns_none(self):
        assert orch_main._validated_session_fleet_tools_override(None) is None
        assert orch_main._validated_session_fleet_tools_override({}) is None
        assert (
            orch_main._validated_session_fleet_tools_override(
                {"tools": {"research": []}}
            )
            is None
        )

    def test_invalid_fleet_management_tools_override_rejected(self):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_fleet_tools_override(
                {"tools": {"orchestrator": "disabled"}}
            )
        assert exc.value.status_code == 400

    def test_invalid_agent_catalog_tools_override_rejected(self):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_tool_overrides(
                {"tools": {"agent_catalog": "disabled"}}
            )
        assert exc.value.status_code == 400
        assert "agent_catalog" in exc.value.detail

    def test_invalid_workflows_tools_override_rejected(self):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_tool_overrides(
                {"tools": {"workflows": "disabled"}}
            )
        assert exc.value.status_code == 400
        assert "workflows" in exc.value.detail

    @pytest.mark.parametrize(
        ("group", "injected"),
        [
            ("orchestrator", "run_command"),
            ("agent_catalog", "create_worker_job"),
            ("workflows", "get_skill"),
            ("canvas", "run_command"),
        ],
    )
    def test_cross_category_session_tool_override_rejected(self, group, injected):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_session_tool_overrides({"tools": {group: [injected]}})
        assert exc.value.status_code == 400
        assert group in exc.value.detail
        assert injected in exc.value.detail

    def test_known_session_tool_override_names_are_accepted(self):
        assert orch_main._validated_session_tool_overrides(
            {
                "tools": {
                    "orchestrator": ["get_session_context"],
                    "agent_catalog": ["list_skills"],
                    "workflows": ["list_automations"],
                    "canvas": ["get_canvas", "set_canvas", "clear_canvas"],
                }
            }
        ) == {
            "orchestrator": ["get_session_context"],
            "agent_catalog": ["list_skills"],
            "workflows": ["list_automations"],
            "canvas": ["get_canvas", "set_canvas", "clear_canvas"],
        }

    def test_shared_validator_rejects_cross_category_canvas_name(self):
        with pytest.raises(SessionToolOverrideError, match="run_command"):
            validate_session_tool_overrides({"tools": {"canvas": ["run_command"]}})

    def test_shared_validator_ignores_non_session_categories(self):
        assert validate_session_tool_overrides(
            {
                "tools": {
                    "canvas": ["get_canvas"],
                    "shell": ["run_command"],
                }
            }
        ) == {"canvas": ["get_canvas"]}

    def test_fleet_management_disabled_detection(self):
        assert orch_main._fleet_management_explicitly_disabled(
            {"tools": {"orchestrator": []}}
        )
        assert not orch_main._fleet_management_explicitly_disabled(
            {"tools": {"orchestrator": ["get_session_context"]}}
        )
        assert not orch_main._fleet_management_explicitly_disabled({})

    def test_agent_catalog_disabled_detection(self):
        assert orch_main._agent_catalog_explicitly_disabled(
            {"tools": {"agent_catalog": []}}
        )
        assert not orch_main._agent_catalog_explicitly_disabled(
            {"tools": {"agent_catalog": ["list_skills"]}}
        )
        assert not orch_main._agent_catalog_explicitly_disabled({})

    def test_workflows_disabled_detection(self):
        assert orch_main._workflows_explicitly_disabled({"tools": {"workflows": []}})
        assert not orch_main._workflows_explicitly_disabled(
            {"tools": {"workflows": ["list_automations"]}}
        )
        assert not orch_main._workflows_explicitly_disabled({})

    def test_session_tool_group_disabled_markers(self):
        markers = orch_main._session_tool_group_disabled_markers(
            {"tools": {"orchestrator": [], "agent_catalog": [], "workflows": []}}
        )
        assert markers == {
            "_fleet_management_disabled": True,
            "_agent_catalog_disabled": True,
            "_workflows_disabled": True,
        }


class TestSendSessionAttachPayload:
    """Hole B, orchestrator side: the attach payload carries config_name."""

    @pytest.mark.asyncio
    async def test_payload_carries_config_name(self):
        _FakeAsyncClient.response_status = 500  # skip the DB-binding branch
        thread = {"id": "tid-1", "user_id": None, "metadata": {}}
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_thread_project_ids",
                AsyncMock(return_value=[]),
            ),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {"llm": {"model": "m"}},
                ["p1"],
                datasources=None,
                config_name="persistent_defaults",
            )
        assert ok is False
        assert len(_FakeAsyncClient.calls) == 1
        call = _FakeAsyncClient.calls[0]
        assert call["url"] == "http://10.0.0.1:8001/session/attach"
        assert call["json"]["config_name"] == "persistent_defaults"
        assert call["json"]["thread_id"] == "tid-1"

    @pytest.mark.asyncio
    async def test_attach_refuses_revoked_persisted_datasource_before_http(self):
        datasource_id = "11111111-2222-3333-4444-555555555555"
        thread = {
            "id": "tid-1",
            "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "metadata": {"datasource_ids": [datasource_id]},
        }
        denied = HTTPException(
            status_code=403,
            detail="One or more selected datasources are unavailable",
        )

        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_datasource_ids",
                AsyncMock(side_effect=denied),
            ) as revalidate,
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {},
                [],
                datasources=[{"type": "kb", "datasource_id": datasource_id}],
                config_name="persistent_defaults",
            )

        assert ok is False
        revalidate.assert_awaited_once_with(thread, [datasource_id])
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_attach_refuses_revoked_native_project_before_http(self):
        project_id = "99999999-2222-3333-4444-555555555555"
        thread = {
            "id": "tid-1",
            "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "metadata": {},
        }
        denied = HTTPException(
            status_code=403,
            detail="One or more attached projects are unavailable",
        )

        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_datasource_ids",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main,
                "_thread_project_ids",
                AsyncMock(return_value=[project_id]),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_project_ids",
                AsyncMock(side_effect=denied),
            ) as revalidate,
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {},
                [project_id],
                datasources=None,
                config_name="persistent_defaults",
            )

        assert ok is False
        revalidate.assert_awaited_once_with(thread, [project_id])
        assert _FakeAsyncClient.calls == []


class TestAttachRoutesForwardConfigName:
    """Hole B: BOTH /session/attach routes must forward config_name.

    The job pool runs the dual app, dedicated session pods run the
    persistent app — each registers its own /session/attach. The first
    live verify missed the dual route (it answered 200 and silently
    dropped config_name), so pin both at source level.
    """

    def test_both_attach_routes_forward_config_name(self):
        import inspect

        import src.api.dual_app as dual_app
        import src.api.persistent_app as papp

        assert 'config_name=request.get("config_name")' in inspect.getsource(dual_app)
        assert 'config_name=request.get("config_name")' in inspect.getsource(papp)

    def test_dual_detach_uses_rest_detach_reason(self):
        """Both detach routes must terminate with the documented
        "rest_detach" reason — the dual route used the "legacy" shim,
        which broke the greppable Terminate(rest_detach) signal."""
        import inspect

        import src.api.dual_app as dual_app

        assert '_terminate_session("rest_detach")' in inspect.getsource(dual_app)


class TestLoadExpertConfig:
    """Hole B, agent side: the named config resolves with its own pipeline."""

    def test_persistent_name_yields_persistent_pipeline(self):
        cfg = _load_expert_config("persistent_defaults")
        assert cfg.memory.pipeline.writers == [
            "persistent_interval_extractor",
            "pre_compaction_extractor",
            "teardown_extractor",
        ]

    def test_worker_name_yields_worker_pipeline(self):
        cfg = _load_expert_config("defaults")
        assert "teardown_extractor" not in cfg.memory.pipeline.writers
        assert "interval_extractor" in cfg.memory.pipeline.writers

    def test_unknown_name_raises(self):
        with pytest.raises(Exception):
            _load_expert_config("no-such-config-xyz")


class TestDetachAgentSession:
    """B11 k8s route: detach-then-delete preconditions and outcomes."""

    def _db(self, thread, agent_row):
        return SimpleNamespace(
            get_thread=AsyncMock(return_value=thread),
            fetchrow=AsyncMock(return_value=agent_row),
        )

    @pytest.mark.asyncio
    async def test_no_bound_agent_skips_without_http(self):
        db = self._db({"id": "t1", "agent_id": None}, None)
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_non_session_agent_skips_without_http(self):
        """Offline/ready agents (the orphan-reaper case) must not stall."""
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "ready"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_live_session_agent_detaches(self):
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "session"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is True
        assert _FakeAsyncClient.calls[0]["url"] == "http://10.0.0.2:8001/session/detach"

    @pytest.mark.asyncio
    async def test_http_failure_is_contained(self):
        """Teardown must proceed even when the agent is unreachable."""
        _FakeAsyncClient.raise_on_post = ConnectionError("refused")
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "session"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False

    @pytest.mark.asyncio
    async def test_release_runs_detach_before_workspace_cleanup(self):
        """Ordering pin: the agent must get its terminate (git push needs
        the workspace alive) before the workspace archive/cleanup step."""
        order: list = []

        async def _detach(thread_id, timeout=150.0):
            order.append("detach")
            return True

        async def _archive(thread_id, entity_type):
            order.append("workspace")

        provisioner = SimpleNamespace(
            is_available=True,
            delete_agent_pod_by_thread=AsyncMock(
                side_effect=lambda tid: order.append("pod") or True
            ),
        )
        with (
            patch.object(orch_main, "_detach_agent_session", _detach),
            patch.object(orch_main, "_archive_and_cleanup_workspace", _archive),
            patch.object(orch_main, "agent_provisioner", provisioner),
        ):
            await orch_main._release_thread_resources("t1")
        assert order == ["detach", "workspace", "pod"]
