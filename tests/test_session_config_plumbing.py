"""Pins for the session config_name plumbing + detach-then-delete fixes.

Covers the three holes found during the memory-overhaul Phase-1 closure
step 1 (2026-06-11):

- Hole A: a bare ``POST /api/persistent/threads`` must land on the
  persistent base config, not the worker one
  (knowledge-base/knowledge/issues/session_config_name_plumbing.md).
- Hole B: the idle-pool ``/session/attach`` path must carry the thread's
  config_name (orchestrator side) and resolve it as the session base
  (agent side) — otherwise pool-attached sessions bind the worker memory
  pipeline and silently lose ``teardown_extractor``.
- B11 k8s route: the user-facing thread DELETE must give a live session
  agent the chance to terminate (final memory capture + git push) before
  the workspace and pod are torn down (memory_bugs.md B11 addendum).
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import orchestrator.main as orch_main
from src.api.persistent_app import _load_expert_config
from src.core.tool_policy import ToolPolicyError, validate_tool_override_fragment
from src.shared.runtime_actor import RuntimeActorContext


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = ""


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in recording the last POST."""

    calls: list = []
    response_status: int = 200
    response_text: str = ""
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
        response = _FakeResponse(_FakeAsyncClient.response_status)
        response.text = _FakeAsyncClient.response_text
        return response


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response_status = 200
    _FakeAsyncClient.response_text = ""
    _FakeAsyncClient.raise_on_post = None
    yield


@asynccontextmanager
async def _noop_thread_datasource_lock():
    yield


@asynccontextmanager
async def _owned_workspace_lifecycle_lock(*_args, **_kwargs):
    yield True


@pytest.fixture(autouse=True)
def _patch_thread_datasource_delivery_lock():
    with patch.object(
        orch_main.postgres_db,
        "thread_datasource_lock",
        side_effect=lambda _thread_id: _noop_thread_datasource_lock(),
    ):
        yield


_REAL_RESERVE_SESSION_ATTACH_BINDING = orch_main._reserve_session_attach_binding


@pytest.fixture(autouse=True)
def _patch_session_attach_reservation():
    """Payload-focused tests do not need a live DB reservation."""
    with (
        patch.object(
            orch_main,
            "_reserve_session_attach_binding",
            AsyncMock(return_value=True),
        ),
        patch.object(
            orch_main,
            "_release_session_attach_binding",
            AsyncMock(),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _patch_session_runtime_actor_mint():
    """Config-plumbing tests stop at the server-derived actor delivery seam."""

    actor = RuntimeActorContext(
        caller_kind="human",
        thread_id="tid-1",
        access_credential="sra_" + ("A" * 43),
        refresh_credential="srr_" + ("B" * 43),
    )
    with patch.object(
        orch_main,
        "mint_thread_runtime_actor",
        AsyncMock(return_value=actor),
    ):
        yield


class TestThreadCreateDefault:
    """Hole A: the request-model default."""

    def test_bare_thread_create_defaults_to_persistent_config(self):
        assert orch_main.ThreadCreateRequest().config_name == "session_base"

    def test_explicit_datasource_default_request_is_distinct_from_omission(self):
        request = orch_main.ThreadCreateRequest(use_datasource_defaults=True)

        assert request.use_datasource_defaults is True
        assert "datasource_ids" not in request.model_fields_set

    def test_datasource_defaults_and_explicit_selection_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            orch_main.ThreadCreateRequest(
                use_datasource_defaults=True,
                datasource_ids=[],
            )


class TestJobCreateDatasourceDefaults:
    def test_explicit_datasource_default_request_is_distinct_from_omission(self):
        request = orch_main.JobCreate(
            description="defaulted job",
            use_datasource_defaults=True,
        )

        assert request.use_datasource_defaults is True
        assert "datasource_ids" not in request.model_fields_set

    def test_datasource_defaults_and_explicit_selection_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            orch_main.JobCreate(
                description="ambiguous job",
                use_datasource_defaults=True,
                datasource_ids=[],
            )


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

    def test_vm_backend_accepted_at_create(self):
        # vm is now selectable at creation (operator-gated + provisioned in
        # create_thread). Validation lets it through with its sizing sub-dict;
        # the permission/provisioner gate lives downstream, not here.
        # knowledge-base/knowledge/features/session_create_on_vm.md
        ws = orch_main._validated_session_workspace_override(
            {"workspace": {"backend": "vm", "vm": {"cpu_cores": 4, "memory": "8Gi"}}}
        )
        assert ws == {"backend": "vm", "vm": {"cpu_cores": 4, "memory": "8Gi"}}

    def test_vm_not_in_default_chain_set(self):
        # vm must remain a per-session opt-in, never an implicit/saved default:
        # it is excluded from SESSION_WORKSPACE_BACKENDS (the default-chain +
        # settings-PATCH set) but present in the create-time allowlist.
        assert "vm" not in orch_main.SESSION_WORKSPACE_BACKENDS
        assert "vm" in orch_main.SESSION_CREATE_WORKSPACE_BACKENDS

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


class TestSessionReadyTimeout:
    """VM-aware readiness budget for the session-start paths
    (knowledge-base/knowledge/features/session_create_on_vm.md)."""

    def test_non_vm_uses_fast_default(self, monkeypatch):
        monkeypatch.delenv("WS_READY_TIMEOUT_S", raising=False)
        assert orch_main._session_ready_timeout_s("sandbox") == 180
        assert orch_main._session_ready_timeout_s("virtual") == 180
        assert orch_main._session_ready_timeout_s(None) == 180

    def test_vm_uses_extended_budget(self, monkeypatch):
        monkeypatch.delenv("VM_WS_READY_TIMEOUT_S", raising=False)
        assert orch_main._session_ready_timeout_s("vm") == 960

    def test_budgets_are_env_tunable(self, monkeypatch):
        monkeypatch.setenv("WS_READY_TIMEOUT_S", "200")
        monkeypatch.setenv("VM_WS_READY_TIMEOUT_S", "1200")
        assert orch_main._session_ready_timeout_s("sandbox") == 200
        assert orch_main._session_ready_timeout_s("vm") == 1200

    def test_vm_budget_exceeds_non_vm(self):
        # Nested-budget invariant: the server ready wait must outlast a cold VM
        # boot, so vm must be strictly larger than the sandbox default.
        assert orch_main._session_ready_timeout_s(
            "vm"
        ) > orch_main._session_ready_timeout_s("sandbox")


class TestThreadWorkspaceBackend:
    """_thread_workspace_backend extracts a thread's stored backend so the
    resume path (_do_prepare) can size the VM budget without the config_override."""

    def test_dict_metadata(self):
        thread = {"metadata": {"config_override": {"workspace": {"backend": "vm"}}}}
        assert orch_main._thread_workspace_backend(thread) == "vm"

    def test_json_string_metadata(self):
        import json

        thread = {
            "metadata": json.dumps(
                {"config_override": {"workspace": {"backend": "sandbox"}}}
            )
        }
        assert orch_main._thread_workspace_backend(thread) == "sandbox"

    def test_missing_or_malformed_returns_none(self):
        assert orch_main._thread_workspace_backend({}) is None
        assert orch_main._thread_workspace_backend({"metadata": "not-json"}) is None
        assert orch_main._thread_workspace_backend(None) is None
        assert (
            orch_main._thread_workspace_backend({"metadata": {"config_override": {}}})
            is None
        )


class TestSessionWorkspaceBackendDefaultChain:
    """Instant-landing defaults chain (knowledge-base/knowledge/features/instant_landing_session.md):
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
        tools = orch_main._validated_tool_overrides(
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
            orch_main._validated_tool_overrides(
                {"tools": {"agent_catalog": "disabled"}}
            )
        assert exc.value.status_code == 400
        assert "agent_catalog" in exc.value.detail

    def test_invalid_workflows_tools_override_rejected(self):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_tool_overrides({"tools": {"workflows": "disabled"}})
        assert exc.value.status_code == 400
        assert "workflows" in exc.value.detail

    @pytest.mark.parametrize(
        ("group", "injected"),
        [
            ("orchestrator", "run_command"),
            ("agent_catalog", "create_job"),
            ("workflows", "get_skill"),
            ("canvas", "run_command"),
        ],
    )
    def test_cross_category_session_tool_override_rejected(self, group, injected):
        with pytest.raises(orch_main.HTTPException) as exc:
            orch_main._validated_tool_overrides({"tools": {group: [injected]}})
        assert exc.value.status_code == 400
        assert group in exc.value.detail
        assert injected in exc.value.detail

    def test_known_session_tool_override_names_are_accepted(self):
        assert orch_main._validated_tool_overrides(
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
        with pytest.raises(ToolPolicyError, match="run_command"):
            validate_tool_override_fragment({"tools": {"canvas": ["run_command"]}})

    def test_shared_validator_honors_every_category_not_four(self):
        """Was ``..._ignores_non_session_categories``, and that was the defect.

        The boundary accepted a restriction on ``shell`` (and seven other
        categories the New Session form renders) and threw it away.
        """
        assert validate_tool_override_fragment(
            {
                "tools": {
                    "canvas": ["get_canvas"],
                    "shell": ["run_command"],
                    "research": [],
                }
            }
        ) == {"canvas": ["get_canvas"], "shell": ["run_command"], "research": []}

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
    @pytest.mark.parametrize("execution_lane", ["stateless", "future-lane"])
    async def test_attach_refuses_every_non_pinned_lane_before_http(
        self, execution_lane
    ):
        thread = {
            "id": "tid-1",
            "execution_lane": execution_lane,
            "user_id": None,
            "metadata": {},
        }
        with patch.object(
            orch_main.postgres_db,
            "get_thread",
            AsyncMock(return_value=thread),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is False
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_attach_reservation_miss_never_reaches_http(self):
        """A lost lane/detachment CAS cannot start an agent-side attach."""
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(
                orch_main,
                "_reserve_session_attach_binding",
                AsyncMock(return_value=False),
            ) as reserve,
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is False
        reserve.assert_awaited_once_with("a1", "tid-1")
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_thread_reservation_cas_miss_rolls_back_before_agent_or_http(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        tx = AsyncMock()
        tx.__aenter__.return_value = None
        tx.__aexit__.return_value = False
        conn.transaction = MagicMock(return_value=tx)
        acquire = AsyncMock()
        acquire.__aenter__.return_value = conn
        acquire.__aexit__.return_value = False

        with patch.object(
            orch_main.postgres_db,
            "acquire",
            MagicMock(return_value=acquire),
        ):
            reserved = await _REAL_RESERVE_SESSION_ATTACH_BINDING("a1", "tid-1")

        assert reserved is False
        assert conn.execute.await_count == 1
        assert "execution_lane = $3" in conn.execute.await_args.args[0]
        assert "control_admission_agent_id = NULL" in conn.execute.await_args.args[0]

    @pytest.mark.asyncio
    async def test_agent_cas_follows_thread_lock_in_same_reservation_transaction(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 0"])
        tx = AsyncMock()
        tx.__aenter__.return_value = None
        tx.__aexit__.return_value = False
        conn.transaction = MagicMock(return_value=tx)
        acquire = AsyncMock()
        acquire.__aenter__.return_value = conn
        acquire.__aexit__.return_value = False

        with patch.object(
            orch_main.postgres_db,
            "acquire",
            MagicMock(return_value=acquire),
        ):
            reserved = await _REAL_RESERVE_SESSION_ATTACH_BINDING("a1", "tid-1")

        assert reserved is False
        assert conn.execute.await_count == 2
        assert "execution_lane = $3" in conn.execute.await_args_list[0].args[0]
        assert "current_job_id IS NULL" in conn.execute.await_args_list[1].args[0]
        assert "status = 'ready'" in conn.execute.await_args_list[1].args[0]
        tx.__aexit__.assert_awaited_once()
        assert tx.__aexit__.await_args.args[0] is RuntimeError

    @pytest.mark.asyncio
    async def test_successful_reservation_precedes_http_delivery(self):
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        order: list[str] = []

        async def reserve(*_args):
            order.append("reserve")
            return True

        async def post(_self, url, json=None):
            assert order == ["reserve"]
            order.append("http")
            _FakeAsyncClient.calls.append({"url": url, "json": json})
            return _FakeResponse(200)

        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(
                orch_main,
                "_reserve_session_attach_binding",
                AsyncMock(side_effect=reserve),
            ),
            patch.object(_FakeAsyncClient, "post", post),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is True
        assert order == ["reserve", "http"]

    @pytest.mark.asyncio
    async def test_409_retains_reservation_because_same_thread_is_ambiguous(self):
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        _FakeAsyncClient.response_status = 409
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
            patch.object(
                orch_main,
                "_reserve_session_attach_binding",
                AsyncMock(return_value=True),
            ) as reserve,
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is True
        reserve.assert_awaited_once_with("a1", "tid-1")
        assert [call["url"] for call in _FakeAsyncClient.calls] == [
            "http://10.0.0.1:8001/session/attach"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [422, 500])
    async def test_ambiguous_http_failure_retains_reservation(self, status_code):
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        _FakeAsyncClient.response_status = status_code
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is True

    @pytest.mark.asyncio
    async def test_ambiguous_attach_log_does_not_echo_secret_response(self, caplog):
        private_material = "-----BEGIN OPENSSH PRIVATE KEY-----hidden"
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        _FakeAsyncClient.response_status = 422
        _FakeAsyncClient.response_text = private_material
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is True
        assert private_material not in caplog.text
        assert "ambiguous attach response 422" in caplog.text

    @pytest.mark.asyncio
    async def test_transport_failure_retains_reservation(self):
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
        _FakeAsyncClient.raise_on_post = TimeoutError("response lost")
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_assemble_session_attach_payload",
                AsyncMock(return_value={"thread_id": "tid-1"}),
            ),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
            )

        assert ok is True

    @pytest.mark.asyncio
    async def test_detach_save_cannot_finish_before_inflight_delivery(self):
        gate = asyncio.Lock()
        delivery_started = asyncio.Event()
        release_delivery = asyncio.Event()
        order: list[str] = []

        @asynccontextmanager
        async def shared_lock():
            async with gate:
                yield

        async def fake_delivery(*_args, **_kwargs):
            delivery_started.set()
            await release_delivery.wait()
            order.append("delivery")
            return True

        async def save_detach():
            async with orch_main.postgres_db.thread_datasource_lock("tid-1"):
                order.append("save")

        with (
            patch.object(
                orch_main.postgres_db,
                "thread_datasource_lock",
                side_effect=lambda _thread_id: shared_lock(),
            ),
            patch.object(
                orch_main,
                "_send_session_attach_locked",
                AsyncMock(side_effect=fake_delivery),
            ),
        ):
            attach_task = asyncio.create_task(
                orch_main._send_session_attach(
                    {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                    "tid-1",
                )
            )
            await delivery_started.wait()
            save_task = asyncio.create_task(save_detach())
            await asyncio.sleep(0)
            assert not save_task.done()

            release_delivery.set()
            assert await attach_task is True
            await save_task

        assert order == ["delivery", "save"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored_narration", "expected_narration"),
        [(None, "silent"), ("verbose", "verbose")],
        ids=["legacy-inherited", "materialized-control-scalar"],
    )
    async def test_attach_overlays_first_class_control_scalars_on_resolved_config(
        self, stored_narration, expected_narration
    ):
        thread = {
            "id": "tid-1",
            "user_id": "owner-1",
            "metadata": {},
            "permission_mode": "autonomous",
            "narration_mode": stored_narration,
        }
        resolved = {
            "agent": {
                "interactive": {
                    "permission_mode": "supervised",
                    "narration_mode": "silent",
                }
            }
        }
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_inject_lite_workspace_config",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(orch_main, "_thread_project_ids", AsyncMock(return_value=[])),
            patch.object(
                orch_main,
                "_revalidate_thread_project_ids",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main,
                "_resolve_authorized_thread_datasources",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main, "_resolve_session_config", AsyncMock(return_value=resolved)
            ),
        ):
            payload = await orch_main._assemble_session_attach_payload("tid-1")

        interactive = payload["resolved_config"]["agent"]["interactive"]
        assert interactive == {
            "permission_mode": "autonomous",
            "narration_mode": expected_narration,
        }

    @pytest.mark.asyncio
    async def test_attach_materializes_scalars_in_legacy_config_fallback(self):
        thread = {
            "id": "tid-1",
            "user_id": "owner-1",
            "metadata": {},
            "permission_mode": "auto_accept",
            "narration_mode": "auto",
        }
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_inject_lite_workspace_config",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(orch_main, "_thread_project_ids", AsyncMock(return_value=[])),
            patch.object(
                orch_main,
                "_revalidate_thread_project_ids",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main,
                "_resolve_authorized_thread_datasources",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main, "_resolve_session_config", AsyncMock(return_value=None)
            ),
        ):
            payload = await orch_main._assemble_session_attach_payload(
                "tid-1", config_override={"llm": {"model": "m"}}
            )

        assert payload["resolved_config"] is None
        assert payload["config_override"]["interactive"] == {
            "permission_mode": "auto_accept",
            "narration_mode": "auto",
        }

    @pytest.mark.asyncio
    async def test_payload_carries_config_name(self):
        _FakeAsyncClient.response_status = 500
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {},
        }
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
            patch.object(orch_main, "_is_experts_db_enabled", return_value=False),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {"llm": {"model": "m"}},
                ["p1"],
                datasources=None,
                config_name="session_base",
            )
        assert ok is True
        assert len(_FakeAsyncClient.calls) == 1
        call = _FakeAsyncClient.calls[0]
        assert call["url"] == "http://10.0.0.1:8001/session/attach"
        assert call["json"]["config_name"] == "session_base"
        assert call["json"]["thread_id"] == "tid-1"
        assert call["json"]["runtime_actor"]["caller_kind"] == "human"
        assert call["json"]["runtime_actor"]["access_credential"].startswith("sra_")

    @pytest.mark.asyncio
    async def test_attach_refuses_revoked_persisted_datasource_before_http(self):
        datasource_id = "11111111-2222-3333-4444-555555555555"
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "metadata": {"datasource_ids": [datasource_id]},
        }
        denied = HTTPException(
            status_code=403,
            detail="One or more selected connectors are unavailable",
        )

        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_datasource_selection",
                AsyncMock(side_effect=denied),
            ) as revalidate,
            patch.object(
                orch_main,
                "_thread_project_ids",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_project_ids",
                AsyncMock(return_value=[]),
            ),
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
        revalidate.assert_awaited_once_with(
            thread,
            [datasource_id],
            target_project_ids=[],
        )
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_attach_refuses_revoked_native_project_before_http(self):
        project_id = "99999999-2222-3333-4444-555555555555"
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
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
                "_revalidate_thread_datasource_selection",
                AsyncMock(return_value=([], {})),
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

    @pytest.mark.asyncio
    async def test_attach_discards_caller_payload_and_resolves_current_selection(self):
        stale_id = "11111111-2222-4333-8444-555555555555"
        current_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        project_id = "99999999-2222-4333-8444-555555555555"
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "metadata": {"datasource_ids": [current_id]},
        }
        current_row = {
            "id": current_id,
            "type": "kb",
            "name": "Current KB",
            "description": None,
            "connection_url": None,
            "credentials": {},
            "config": {},
            "project_read_only": True,
            "policy_revision": 2,
        }
        resolver = AsyncMock(return_value=[current_row])

        _FakeAsyncClient.response_status = 500
        with (
            patch.object(
                orch_main.postgres_db,
                "get_thread",
                AsyncMock(return_value=thread),
            ),
            patch.object(
                orch_main,
                "_thread_project_ids",
                AsyncMock(return_value=[project_id]),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_project_ids",
                AsyncMock(return_value=[project_id]),
            ),
            patch.object(
                orch_main,
                "_revalidate_thread_datasource_selection",
                AsyncMock(return_value=([current_id], {current_id: 2})),
            ),
            patch.object(
                orch_main.postgres_db,
                "resolve_datasources_for_thread",
                resolver,
            ),
            patch.object(orch_main, "_is_experts_db_enabled", return_value=False),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {},
                ["stale-project"],
                datasources=[{"type": "kb", "datasource_id": stale_id}],
                config_name="persistent_defaults",
            )

        assert ok is True
        resolver.assert_awaited_once_with(
            datasource_ids=[current_id],
            project_ids=[project_id],
        )
        payload = _FakeAsyncClient.calls[0]["json"]
        assert payload["project_ids"] == [project_id]
        assert payload["datasources"][0]["datasource_id"] == current_id
        assert stale_id not in str(payload["datasources"])


class TestColdSessionDatasourceDelivery:
    @pytest.mark.asyncio
    async def test_live_detach_commits_before_cold_handler_refetches(self):
        datasource_a = "11111111-2222-4333-8444-555555555555"
        state = {"datasource_ids": [datasource_a]}
        gate = asyncio.Lock()
        writer_entered = asyncio.Event()
        allow_writer_commit = asyncio.Event()

        @asynccontextmanager
        async def shared_lock():
            async with gate:
                yield

        async def current_thread(_thread_id):
            # Return a fresh row so a pre-lock read would retain A even after
            # the writer replaces the canonical metadata with [].
            return {
                "id": "tid-1",
                "execution_lane": "pinned",
                "user_id": None,
                "project_id": None,
                "metadata": {"datasource_ids": list(state["datasource_ids"])},
            }

        async def save_detach():
            async with orch_main.postgres_db.thread_datasource_lock("tid-1"):
                assert state["datasource_ids"] == [datasource_a]
                writer_entered.set()
                await allow_writer_commit.wait()
                state["datasource_ids"] = []

        get_thread = AsyncMock(side_effect=current_thread)
        with (
            patch.object(
                orch_main.postgres_db,
                "thread_datasource_lock",
                side_effect=lambda _thread_id: shared_lock(),
            ),
            patch.object(orch_main.postgres_db, "get_thread", get_thread),
            patch.object(
                orch_main.postgres_db,
                "list_thread_mounts",
                AsyncMock(return_value=[]),
            ),
            patch.object(orch_main, "require_internal", AsyncMock()),
            patch.object(orch_main, "_thread_project_ids", AsyncMock(return_value=[])),
            patch.object(
                orch_main,
                "_agent_canvas_workspace_capabilities",
                return_value=(False, False, False),
            ),
            patch.object(
                orch_main, "_build_agent_cloud_mount", AsyncMock(return_value=None)
            ),
            patch.object(orch_main, "_build_agent_cloud_sync", return_value=None),
            patch.object(
                orch_main, "_resolve_session_config", AsyncMock(return_value=None)
            ),
            patch.object(
                orch_main,
                "_inject_lite_workspace_config",
                side_effect=lambda value, **_kwargs: value,
            ),
        ):
            writer = asyncio.create_task(save_detach())
            await writer_entered.wait()
            cold_response = asyncio.create_task(
                orch_main.agent_get_thread_workspace(object(), "tid-1")
            )
            await asyncio.sleep(0)

            # The old implementation read A before waiting on any lock.  The
            # cold handler must not touch canonical state while Save owns it.
            get_thread.assert_not_awaited()

            allow_writer_commit.set()
            await writer
            response = await cold_response

        assert response["datasources"] is None
        assert datasource_a not in str(response)
        get_thread.assert_awaited_once_with("tid-1")

    @pytest.mark.asyncio
    async def test_attach_discards_stale_payload_after_detach_all(self):
        stale_id = "11111111-2222-4333-8444-555555555555"
        thread = {
            "id": "tid-1",
            "execution_lane": "pinned",
            "user_id": None,
            "metadata": {"datasource_ids": []},
        }

        _FakeAsyncClient.response_status = 500
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
            patch.object(
                orch_main.postgres_db,
                "resolve_datasources_for_thread",
                AsyncMock(return_value=[]),
            ),
            patch.object(orch_main, "_is_experts_db_enabled", return_value=False),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {},
                [],
                datasources=[{"type": "kb", "datasource_id": stale_id}],
                config_name="persistent_defaults",
            )

        assert ok is True
        assert _FakeAsyncClient.calls[0]["json"]["datasources"] is None

    def test_warm_resume_uses_canonical_thread_mount_projects(self):
        import inspect

        source = inspect.getsource(orch_main.resume_thread)
        assert "pids = await _thread_project_ids(tid)" in source
        assert 'pids = thread.get("project_ids")' not in source


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

        async def _archive(thread_id, entity_type, *, reclaim_volume=True):
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


class TestEndedSessionKeepsItsVolume:
    """THE session-durability invariant, end to end: **an ended thread is
    RESUMABLE**, so ending or idling a session must never delete its workspace
    volume. Only a hard delete reclaims it.

    Now that session workspaces are PVC-backed, every teardown path that used to
    be a harmless pod delete is a potential data-destruction path. Three callers
    reach the same teardown for entirely different reasons: the user-facing
    DELETE (soft end vs ?permanent=true), the agent's idle-archive after 30 idle
    minutes, and the stale-agent sweeper when a pod merely goes offline. Only
    the first of those, with ?permanent=true, means "destroy this user's data" —
    and ``resume_thread`` requires exactly the 'ended' status the other two
    write. The whole chain is pinned here (end_thread →
    _release_thread_resources → _archive_and_cleanup_workspace →
    release_workspace) because a default flipped at any one link silently
    deletes workspaces at the far end.
    """

    @staticmethod
    def _thread():
        return {"id": "t1", "user_id": "u1", "agent_id": None, "metadata": {}}

    async def _run_end_thread(self, *, permanent: bool):
        """Drive the real DELETE endpoint, capturing the reclaim decision.

        Returns ``(result, seen, db)`` — ``seen`` holds the kwargs
        ``_release_thread_resources`` was called with.
        """
        seen: dict = {}

        async def _release(thread_id, *, reclaim_volume=False):
            seen["thread_id"] = thread_id
            seen["reclaim_volume"] = reclaim_volume

        db = SimpleNamespace(
            end_thread=AsyncMock(),
            delete_thread=AsyncMock(),
            get_thread=AsyncMock(return_value=self._thread()),
            merge_thread_config_override=AsyncMock(),
        )
        with (
            patch.object(
                orch_main,
                "require_thread_owner",
                AsyncMock(return_value=({"sub": "u1"}, self._thread())),
            ),
            patch.object(
                orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
            ),
            patch.object(orch_main, "_conclude_conference_if_any", AsyncMock()),
            patch.object(orch_main, "_release_thread_resources", _release),
            patch.object(orch_main, "postgres_db", db),
            patch.object(
                orch_main, "snapshot_service", SimpleNamespace(is_available=False)
            ),
            patch.object(
                orch_main, "gitea_client", SimpleNamespace(is_initialized=False)
            ),
        ):
            result = await orch_main.end_thread(
                "t1", SimpleNamespace(), permanent=permanent, force=True
            )
        return result, seen, db

    @pytest.mark.asyncio
    async def test_soft_end_keeps_the_workspace_volume(self):
        """A soft end leaves the thread in 'ended', which /resume accepts — so
        reclaiming here would hand the user an empty workspace on a session they
        never deleted. Same reasoning that already keeps the Gitea repo."""
        result, seen, db = await self._run_end_thread(permanent=False)

        assert result == {"status": "ended"}
        assert seen["reclaim_volume"] is False
        db.end_thread.assert_awaited_once_with("t1")
        db.delete_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permanent_delete_reclaims_the_workspace_volume(self):
        """The other half — without this the invariant would be trivially
        satisfiable by never reclaiming anything, and every deleted session
        would leak a 10Gi volume until the namespace quota rejects new ones."""
        result, seen, db = await self._run_end_thread(permanent=True)

        assert result == {"status": "deleted"}
        assert seen["reclaim_volume"] is True
        db.delete_thread.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    async def test_stateless_end_refuses_unattested_workspace_before_cleanup(self):
        """Legacy rows cannot enter terminal cleanup without runtime authority."""
        thread = {
            "id": "t1",
            "user_id": "u1",
            "agent_id": None,
            "execution_lane": "stateless",
            "metadata": {
                "config_override": {"workspace": {"backend": "sandbox"}},
                "workspace_container": {
                    "status": "ready",
                    "provisioner": "k8s",
                },
            },
        }

        db = SimpleNamespace(
            begin_stateless_thread_workspace_retirement=AsyncMock(),
            get_thread=AsyncMock(return_value=thread),
            stateless_session_workspace_ensure_lock=MagicMock(
                side_effect=_owned_workspace_lifecycle_lock
            ),
        )
        with (
            patch.object(
                orch_main,
                "require_thread_owner",
                AsyncMock(return_value=({"sub": "u1"}, thread)),
            ),
            patch.object(orch_main, "_conclude_conference_if_any", AsyncMock()),
            patch.object(
                orch_main, "_release_thread_resources", AsyncMock()
            ) as release,
            patch.object(orch_main, "postgres_db", db),
            patch.object(
                orch_main, "snapshot_service", SimpleNamespace(is_available=False)
            ),
            patch.object(
                orch_main, "gitea_client", SimpleNamespace(is_initialized=False)
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await orch_main.end_thread(
                "t1", SimpleNamespace(), permanent=False, force=True
            )

        assert exc.value.status_code == 409
        db.begin_stateless_thread_workspace_retirement.assert_not_awaited()
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_waiting_duplicate_end_does_not_retire_freshly_resumed_thread(self):
        initial = {
            "id": "t1",
            "user_id": "u1",
            "agent_id": None,
            "status": "ended",
            "execution_lane": "stateless",
            "metadata": {
                "config_override": {"workspace": {"backend": "sandbox"}},
                "workspace_container": {
                    "status": "ready",
                    "provisioner": "k8s",
                },
            },
        }
        resumed = {**initial, "status": "created"}
        begin = AsyncMock(return_value=True)
        release = AsyncMock()
        db = SimpleNamespace(
            begin_stateless_thread_workspace_retirement=begin,
            finish_stateless_thread_workspace_retirement=AsyncMock(return_value=True),
            get_thread=AsyncMock(return_value=resumed),
            merge_thread_config_override=AsyncMock(),
            stateless_session_workspace_ensure_lock=MagicMock(
                side_effect=_owned_workspace_lifecycle_lock
            ),
        )
        with (
            patch.object(
                orch_main,
                "require_thread_owner",
                AsyncMock(return_value=({"sub": "u1"}, initial)),
            ),
            patch.object(
                orch_main, "_thread_turn_in_flight", AsyncMock(return_value=False)
            ),
            patch.object(orch_main, "_conclude_conference_if_any", AsyncMock()),
            patch.object(orch_main, "_release_thread_resources", release),
            patch.object(orch_main, "postgres_db", db),
            pytest.raises(HTTPException) as exc,
        ):
            await orch_main.end_thread(
                "t1", SimpleNamespace(), permanent=False, force=True
            )

        assert exc.value.status_code == 409
        begin.assert_not_awaited()
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_defaults_to_keeping_the_volume(self):
        """The default is the safe one, so a caller that forgets the argument
        leaks a volume (recoverable) rather than destroying one (not).

        The two live callers both pass it explicitly today — end_thread forwards
        ?permanent, the stale-agent sweeper hardcodes False — so this pins the
        fallback the next caller will inherit. It matters because that caller is
        most likely another "the agent went offline" path, which says nothing
        about the user's intent to keep their files."""
        captured: dict = {}

        async def _archive(thread_id, entity_type, *, reclaim_volume=True):
            captured["entity_type"] = entity_type
            captured["reclaim_volume"] = reclaim_volume

        with (
            patch.object(orch_main, "_detach_agent_session", AsyncMock()),
            patch.object(orch_main, "_archive_and_cleanup_workspace", _archive),
            patch.object(
                orch_main, "agent_provisioner", SimpleNamespace(is_available=False)
            ),
            patch.object(
                orch_main, "persistent_provisioner", SimpleNamespace(is_available=False)
            ),
        ):
            await orch_main._release_thread_resources("t1")

        assert captured == {"entity_type": "threads", "reclaim_volume": False}

    @pytest.mark.asyncio
    async def test_release_forwards_an_explicit_reclaim(self):
        captured: dict = {}

        async def _archive(thread_id, entity_type, *, reclaim_volume=True):
            captured["reclaim_volume"] = reclaim_volume

        with (
            patch.object(orch_main, "_detach_agent_session", AsyncMock()),
            patch.object(orch_main, "_archive_and_cleanup_workspace", _archive),
            patch.object(
                orch_main, "agent_provisioner", SimpleNamespace(is_available=False)
            ),
            patch.object(
                orch_main, "persistent_provisioner", SimpleNamespace(is_available=False)
            ),
        ):
            await orch_main._release_thread_resources("t1", reclaim_volume=True)

        assert captured["reclaim_volume"] is True

    @pytest.mark.asyncio
    async def test_archive_forwards_the_decision_to_the_provisioner(self):
        """The last link: the flag has to survive all the way to the k8s call
        that actually deletes the PVC, keyed on the session owner."""
        # Same import path main.py uses — `services.*` and `orchestrator.services.*`
        # load as distinct modules, and the dataclass equality below needs the
        # class identity to match.
        from services.workspace_lifecycle import WorkspaceOwner

        for reclaim in (False, True):
            thread = {
                "id": "t1",
                "metadata": {"workspace_container": {"status": "ready"}},
            }
            provisioner = SimpleNamespace(
                is_available=True, release_workspace=AsyncMock(return_value=True)
            )
            with (
                patch.object(
                    orch_main,
                    "postgres_db",
                    SimpleNamespace(get_thread=AsyncMock(return_value=thread)),
                ),
                patch.object(orch_main, "container_provisioner", provisioner),
                patch.object(
                    orch_main, "vm_provisioner", SimpleNamespace(is_available=False)
                ),
            ):
                await orch_main._archive_and_cleanup_workspace(
                    "t1", entity_type="threads", reclaim_volume=reclaim
                )

            provisioner.release_workspace.assert_awaited_once_with(
                WorkspaceOwner.session("t1"), reclaim_volume=reclaim
            )
