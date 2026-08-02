"""The tool-groups read serves the AGENT's answer, and says when it cannot.

D6: the resolved toolset comes from the agent, not from an orchestrator-side
re-implementation. These tests hold the two halves of that:

* when a pod has reported, its answer wins outright — including where it
  DISAGREES with the merged config, which is the whole point (the runtime
  injection layer is invisible to a config-only view);
* when no pod has reported, the answer is labelled a prediction and carries a
  reason, because a forecast served as a measurement is the original defect at
  a new seam.
"""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.session_tool_overrides import SESSION_TOOL_OVERRIDE_NAMES
from src.core.tool_report import (
    ORIGIN_AGENT,
    ORIGIN_PREDICTION,
    REPORT_VERSION,
    STATE_ON,
    STATE_UNAVAILABLE,
    report_categories,
)

_AGENT_ROW = {"id": "agent-1", "pod_ip": "10.0.0.9", "pod_port": 8001}


def _approved(user):
    """A stand-in with the REAL ``require_approved_user(request, db)`` arity.

    An ``AsyncMock`` swallows any signature, which is how the preview route
    shipped calling it with one argument and passed every unit test — the live
    gate caught it. A plain function does not.
    """

    async def _require_approved_user(request, db):
        return user

    return _require_approved_user


def _thread(metadata=None, agent_id=None, config_name=None):
    from tests.conftest import _TID_A, _UID_A

    return {
        "id": _TID_A,
        "user_id": _UID_A,
        "title": "thread A",
        "config_name": config_name,
        "project_id": None,
        "agent_id": agent_id,
        "metadata": metadata or {},
    }


class _Response:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Minimal ``httpx.AsyncClient`` stand-in with a per-path route table."""

    def __init__(self, routes, calls):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self._calls.append(url)
        for suffix, result in self._routes.items():
            if url.endswith(suffix):
                if isinstance(result, Exception):
                    raise result
                return result
        return _Response(status_code=404, payload={})


def _agent_http(routes):
    """Patch ``main.httpx.AsyncClient`` and return the recorded call list."""
    calls: list[str] = []
    return patch(
        "main.httpx.AsyncClient",
        MagicMock(side_effect=lambda **kw: _Client(routes, calls)),
    ), calls


def _report(categories, *, observed_at="2026-08-02T12:00:00Z", backend=None):
    tools = [n for names in categories.values() for n in names]
    return {
        "attached": True,
        "thread_id": "t",
        "report": {
            "version": REPORT_VERSION,
            "thread_id": "t",
            "observed_at": observed_at,
            "backend": backend,
            "tools": tools,
            "categories": categories,
        },
    }


async def _call(user, db, thread_row, fake_request, *, routes=None, grants=None):
    """Drive the endpoint with a fake agent transport and explicit grants."""
    from main import get_thread_tool_groups

    db.get_thread = AsyncMock(return_value=thread_row)
    db.get_agent = AsyncMock(return_value=_AGENT_ROW)
    http_patch, calls = _agent_http(routes or {})
    with ExitStack() as stack:
        stack.enter_context(patch("main.require_approved_user", _approved(user)))
        stack.enter_context(
            patch("security.access.require_approved_user", AsyncMock(return_value=user))
        )
        stack.enter_context(patch("main.postgres_db", db))
        stack.enter_context(
            patch("main._is_experts_db_enabled", MagicMock(return_value=True))
        )
        stack.enter_context(
            patch("main._user_experts_enabled", AsyncMock(return_value=True))
        )
        stack.enter_context(
            patch("main._resolve_runner_grants", AsyncMock(return_value=grants))
        )
        stack.enter_context(http_patch)
        result = await get_thread_tool_groups(str(thread_row["id"]), fake_request)
    return result, calls


# =============================================================================
# Measured
# =============================================================================
class TestMeasuredAnswer:
    @pytest.mark.asyncio
    async def test_agent_answer_is_labelled_and_timestamped(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload=_report({"research": ["web_search"]})
                )
            },
        )
        assert result["origin"] == ORIGIN_AGENT
        assert result["observed_at"] == "2026-08-02T12:00:00Z"
        assert result["prediction_reason"] is None
        assert result["categories"]["research"]["state"] == STATE_ON

    @pytest.mark.asyncio
    async def test_the_agent_overrules_the_merged_config(
        self, user_a, fake_db, fake_request
    ):
        """THE regression this task exists for.

        ``session_base`` ships ``tools.orchestrator: []``, so every
        config-derived view says the fleet group is off. If the agent bound
        fleet tools anyway — which the runtime injection layer does whenever
        the group is enabled — the endpoint must say ON.
        """
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload=_report({"orchestrator": ["create_worker_job"]})
                )
            },
        )
        assert result["tool_groups"]["orchestrator"] is True
        assert result["categories"]["orchestrator"]["state"] == STATE_ON
        # And the disagreement is visible rather than hidden.
        assert result["categories"]["orchestrator"]["configured"] == []

    @pytest.mark.asyncio
    async def test_tool_groups_is_derived_from_the_measurement(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload=_report({"canvas": ["set_canvas"]})
                )
            },
        )
        assert set(result["tool_groups"]) == set(SESSION_TOOL_OVERRIDE_NAMES)
        assert result["tool_groups"]["canvas"] is True
        assert result["tool_groups"]["workflows"] is False

    @pytest.mark.asyncio
    async def test_every_category_is_answered_not_just_the_closed_four(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={"/session/toolset": _Response(payload=_report({}))},
        )
        assert set(report_categories()) <= set(result["categories"])
        assert len(result["categories"]) >= 24


class TestBackendCapabilitiesRideTheReport:
    """The tier gate is a fact only the agent holds; carry it, do not re-derive.

    The live gate on k3d saw a no-shell session bind 40 tools where the
    config-only prediction said 60 — 14 of the 20 missing were exactly the
    ``browser_direct`` + ``git`` names ``filter_tools_by_backend`` drops. With
    the agent's reported capabilities in hand those read as *unavailable, wrong
    workspace tier* instead of a bare "off" the user would try to tick.
    """

    @pytest.mark.asyncio
    async def test_a_no_shell_tier_reads_unavailable_not_off(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload=_report(
                        {},
                        backend={
                            "supports_shell": False,
                            "supports_file_tools": True,
                            "supports_canvas_presentation": True,
                        },
                    )
                )
            },
            grants=None,
        )
        assert result["backend"]["supports_shell"] is False
        for category in ("shell", "git", "browser_direct"):
            entry = result["categories"][category]
            assert entry["state"] == STATE_UNAVAILABLE, category
            assert entry["decided_by"] == "backend", category
            assert "workspace tier" in entry["reason"], category

    @pytest.mark.asyncio
    async def test_a_shell_capable_tier_imposes_no_backend_reason(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload=_report(
                        {},
                        backend={
                            "supports_shell": True,
                            "supports_file_tools": True,
                            "supports_canvas_presentation": True,
                        },
                    )
                )
            },
            grants=None,
        )
        assert result["categories"]["git"]["reason"] is None

    @pytest.mark.asyncio
    async def test_a_prediction_reports_no_backend(self, user_a, fake_db, fake_request):
        """No workspace is provisioned yet, so claiming a tier would be a guess."""
        result, _ = await _call(user_a, fake_db, _thread(), fake_request)
        assert result["backend"] is None


# =============================================================================
# Prediction — and saying so
# =============================================================================
class TestPredictionIsLabelled:
    @pytest.mark.asyncio
    async def test_no_agent_means_prediction_with_a_reason(
        self, user_a, fake_db, fake_request
    ):
        result, calls = await _call(user_a, fake_db, _thread(), fake_request)
        assert result["origin"] == ORIGIN_PREDICTION
        assert result["observed_at"] is None
        assert "no agent" in result["prediction_reason"]
        assert calls == [], "must not probe when nothing is bound"

    @pytest.mark.asyncio
    async def test_unreachable_agent_degrades_to_a_labelled_prediction(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={"/session/toolset": RuntimeError("connection refused")},
        )
        assert result["origin"] == ORIGIN_PREDICTION
        assert "did not answer" in result["prediction_reason"]

    @pytest.mark.asyncio
    async def test_detached_agent_is_not_an_empty_measurement(
        self, user_a, fake_db, fake_request
    ):
        """A pod with no session bound has nothing to measure — say that."""
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload={"attached": False, "thread_id": None, "report": None}
                )
            },
        )
        assert result["origin"] == ORIGIN_PREDICTION
        assert "not attached" in result["prediction_reason"]

    @pytest.mark.asyncio
    async def test_a_terminal_agent_row_is_not_probed_at_all(
        self, user_a, fake_db, fake_request
    ):
        """A dead pod's row keeps its ``pod_ip``, and the pane blocks on us."""
        from main import get_thread_tool_groups

        fake_db.get_thread = AsyncMock(return_value=_thread(agent_id="agent-1"))
        fake_db.get_agent = AsyncMock(return_value={**_AGENT_ROW, "status": "offline"})
        http_patch, calls = _agent_http({})
        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch(
                "security.access.require_approved_user", AsyncMock(return_value=user_a)
            ),
            patch("main.postgres_db", fake_db),
            patch("main._is_experts_db_enabled", MagicMock(return_value=True)),
            patch("main._user_experts_enabled", AsyncMock(return_value=True)),
            patch("main._resolve_runner_grants", AsyncMock(return_value=None)),
            http_patch,
        ):
            result = await get_thread_tool_groups(str(_thread()["id"]), fake_request)

        assert result["origin"] == ORIGIN_PREDICTION
        assert "offline" in result["prediction_reason"]
        assert calls == [], "no HTTP to a pod that is known gone"

    @pytest.mark.asyncio
    async def test_a_freshly_attached_agent_is_still_probed(
        self, user_a, fake_db, fake_request
    ):
        """``ready`` persists for up to one heartbeat after attach.

        Gating the probe on ``status == "session"`` would report a prediction
        for the first minute of every session — the exact class of silently
        wrong answer this endpoint exists to remove.
        """
        from main import get_thread_tool_groups

        fake_db.get_thread = AsyncMock(return_value=_thread(agent_id="agent-1"))
        fake_db.get_agent = AsyncMock(return_value={**_AGENT_ROW, "status": "ready"})
        http_patch, calls = _agent_http(
            {
                "/session/toolset": _Response(
                    payload=_report({"research": ["web_search"]})
                )
            }
        )
        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch(
                "security.access.require_approved_user", AsyncMock(return_value=user_a)
            ),
            patch("main.postgres_db", fake_db),
            patch("main._is_experts_db_enabled", MagicMock(return_value=True)),
            patch("main._user_experts_enabled", AsyncMock(return_value=True)),
            patch("main._resolve_runner_grants", AsyncMock(return_value=None)),
            http_patch,
        ):
            result = await get_thread_tool_groups(str(_thread()["id"]), fake_request)

        assert result["origin"] == ORIGIN_AGENT
        assert calls

    @pytest.mark.asyncio
    async def test_unreadable_report_is_refused_not_half_read(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(
                    payload={
                        "attached": True,
                        "report": {"version": 999, "categories": {}},
                    }
                )
            },
        )
        assert result["origin"] == ORIGIN_PREDICTION
        assert "could not be read" in result["prediction_reason"]

    @pytest.mark.asyncio
    async def test_a_prediction_carries_no_configured_shadow(
        self, user_a, fake_db, fake_request
    ):
        """Nothing to compare against, so the field is absent by shape."""
        result, _ = await _call(user_a, fake_db, _thread(), fake_request)
        assert "configured" not in result["categories"]["research"]


class TestOlderAgentImageFallback:
    @pytest.mark.asyncio
    async def test_status_tools_are_still_a_measurement(
        self, user_a, fake_db, fake_request
    ):
        """``/status`` already publishes the bound names; use them.

        An agent image predating ``/session/toolset`` is not a reason to
        downgrade to a guess — its ``/status`` returns the same
        ``session.tools`` list with less structure.
        """
        result, calls = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(status_code=404, payload={}),
                "/status": _Response(payload={"tools": ["web_search", "read_file"]}),
            },
        )
        assert result["origin"] == ORIGIN_AGENT
        assert result["observed_at"] is None, "no timestamp is available on this path"
        assert result["categories"]["research"]["tools"] == ["web_search"]
        assert result["categories"]["workspace"]["tools"] == ["read_file"]
        assert any(u.endswith("/status") for u in calls)

    @pytest.mark.asyncio
    async def test_neither_endpoint_answering_is_a_prediction(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={
                "/session/toolset": _Response(status_code=404, payload={}),
                "/status": _Response(status_code=500, payload={}),
            },
        )
        assert result["origin"] == ORIGIN_PREDICTION


# =============================================================================
# Unavailable, with a reason
# =============================================================================
class TestUnavailableCarriesAReason:
    @pytest.mark.asyncio
    async def test_missing_shell_grant_reads_unavailable_not_merely_off(
        self, user_a, fake_db, fake_request
    ):
        """D4/1.4: a silent unticked box is what left the user with no way in."""
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={"/session/toolset": _Response(payload=_report({}))},
            grants={"shell_tools": False, "browser": True, "datasource_tools": True},
        )
        shell = result["categories"]["shell"]
        assert shell["state"] == STATE_UNAVAILABLE
        assert shell["settable"] is False
        assert "shell_tools" in shell["reason"]

    @pytest.mark.asyncio
    async def test_admin_bypass_imposes_no_grant_restriction(
        self, user_a, fake_db, fake_request
    ):
        result, _ = await _call(
            user_a,
            fake_db,
            _thread(agent_id="agent-1"),
            fake_request,
            routes={"/session/toolset": _Response(payload=_report({}))},
            grants=None,
        )
        assert result["categories"]["shell"]["reason"] is None

    @pytest.mark.asyncio
    async def test_a_failed_grant_lookup_never_fabricates_a_denial(
        self, user_a, fake_db, fake_request
    ):
        """The PDP enforces; this only explains. Inventing a denial is its own
        D1 violation, so a lookup failure must read as no restriction."""
        from main import get_thread_tool_groups

        fake_db.get_thread = AsyncMock(return_value=_thread())
        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch(
                "security.access.require_approved_user", AsyncMock(return_value=user_a)
            ),
            patch("main.postgres_db", fake_db),
            patch("main._is_experts_db_enabled", MagicMock(return_value=True)),
            patch("main._user_experts_enabled", AsyncMock(return_value=True)),
            patch(
                "main._resolve_runner_grants",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
        ):
            result = await get_thread_tool_groups(str(_thread()["id"]), fake_request)

        assert result["categories"]["shell"]["reason"] is None


# =============================================================================
# The error path keeps a measurement it already has
# =============================================================================
class TestResolveFailureWithAMeasurement:
    @pytest.mark.asyncio
    async def test_a_broken_resolve_does_not_discard_the_agent_answer(
        self, user_a, fake_db, fake_request
    ):
        """The resolve is only the EXPLANATION once a measurement exists.

        A session that is running has, by construction, already resolved — so
        a resolve failing now says nothing about what the agent holds.
        """
        from main import get_thread_tool_groups

        fake_db.get_thread = AsyncMock(return_value=_thread(agent_id="agent-1"))
        fake_db.get_agent = AsyncMock(return_value=_AGENT_ROW)
        http_patch, _calls = _agent_http(
            {
                "/session/toolset": _Response(
                    payload=_report({"research": ["web_search"]})
                )
            }
        )
        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch(
                "security.access.require_approved_user", AsyncMock(return_value=user_a)
            ),
            patch("main.postgres_db", fake_db),
            patch("main._is_experts_db_enabled", MagicMock(return_value=True)),
            patch("main._user_experts_enabled", AsyncMock(return_value=True)),
            patch("main._resolve_runner_grants", AsyncMock(return_value=None)),
            patch(
                "main._merged_session_tool_policy",
                MagicMock(side_effect=RuntimeError("boom")),
            ),
            http_patch,
        ):
            result = await get_thread_tool_groups(str(_thread()["id"]), fake_request)

        assert result["source"] == "error"
        assert result["origin"] == ORIGIN_AGENT
        assert result["categories"]["research"]["state"] == STATE_ON


# =============================================================================
# The creation form's read — a prediction by construction
# =============================================================================
class TestPreviewEndpoint:
    @pytest.mark.asyncio
    async def test_always_a_prediction(self, user_a, fake_db, fake_request):
        from main import ToolGroupPreviewRequest, preview_tool_groups

        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch("main.postgres_db", fake_db),
            patch("main._resolve_runner_grants", AsyncMock(return_value=None)),
        ):
            result = await preview_tool_groups(
                ToolGroupPreviewRequest(config_name="session_base"), fake_request
            )

        assert result["origin"] == ORIGIN_PREDICTION
        assert result["observed_at"] is None
        assert result["prediction_reason"]
        assert set(report_categories()) <= set(result["categories"])

    @pytest.mark.asyncio
    async def test_reflects_the_requested_override(self, user_a, fake_db, fake_request):
        from main import ToolGroupPreviewRequest, preview_tool_groups

        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch("main.postgres_db", fake_db),
            patch("main._resolve_runner_grants", AsyncMock(return_value=None)),
        ):
            on = await preview_tool_groups(
                ToolGroupPreviewRequest(
                    config_name="session_base",
                    config_override={"tools": {"canvas": ["get_canvas", "set_canvas"]}},
                ),
                fake_request,
            )
            off = await preview_tool_groups(
                ToolGroupPreviewRequest(
                    config_name="session_base",
                    config_override={"tools": {"canvas": []}},
                ),
                fake_request,
            )

        assert on["tool_groups"]["canvas"] is True
        assert on["categories"]["canvas"]["decided_by"] == "request"
        assert off["tool_groups"]["canvas"] is False

    @pytest.mark.asyncio
    async def test_an_unresolvable_config_is_refused_not_guessed(
        self, user_a, fake_db, fake_request
    ):
        from fastapi import HTTPException

        from main import ToolGroupPreviewRequest, preview_tool_groups

        with (
            patch("main.require_approved_user", _approved(user_a)),
            patch("main.postgres_db", fake_db),
            patch(
                "main._merged_session_tool_policy",
                MagicMock(side_effect=RuntimeError("boom")),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await preview_tool_groups(
                    ToolGroupPreviewRequest(config_name="session_base"), fake_request
                )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_requires_an_approved_user(self, fake_db, fake_request):
        from fastapi import HTTPException

        from main import ToolGroupPreviewRequest, preview_tool_groups

        with (
            patch(
                "main.require_approved_user",
                AsyncMock(side_effect=HTTPException(status_code=403)),
            ),
            patch("main.postgres_db", fake_db),
        ):
            with pytest.raises(HTTPException) as exc:
                await preview_tool_groups(ToolGroupPreviewRequest(), fake_request)
        assert exc.value.status_code == 403


# =============================================================================
# The agent's side of the wire
# =============================================================================
class TestAgentToolsetRoute:
    def test_reports_nothing_to_measure_when_unattached(self):
        import src.api.persistent_app as pa

        with patch.object(pa, "_session", None), patch.object(pa, "_thread_id", "t9"):
            payload = pa._session_toolset_report()
        assert payload["attached"] is False
        assert payload["report"] is None

    def test_reads_the_live_tool_objects(self):
        import src.api.persistent_app as pa

        session = MagicMock()
        session.thread_id = "t9"
        session.tools = [MagicMock(name="a"), MagicMock(name="b")]
        session.tools[0].name = "read_file"
        session.tools[1].name = "web_search"
        session.workspace_manager.backend = None

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "t9"),
        ):
            payload = pa._session_toolset_report()

        assert payload["attached"] is True
        assert payload["report"]["tools"] == ["read_file", "web_search"]
        assert payload["report"]["categories"]["workspace"] == ["read_file"]

    def test_round_trips_into_the_orchestrator_reader(self):
        """The two ends of the wire agree — no hand transcription between them."""
        import src.api.persistent_app as pa
        from src.core.tool_report import read_agent_toolset_report

        session = MagicMock()
        session.thread_id = "t9"
        tool = MagicMock()
        tool.name = "web_search"
        session.tools = [tool]
        session.workspace_manager.backend = None

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "t9"),
        ):
            payload = pa._session_toolset_report()

        assert read_agent_toolset_report(payload["report"]) == {
            "research": ["web_search"]
        }

    def test_payload_is_json_serialisable(self):
        import src.api.persistent_app as pa

        session = MagicMock()
        session.thread_id = "t9"
        tool = MagicMock()
        tool.name = "web_search"
        session.tools = [tool]
        session.workspace_manager.backend = None

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "t9"),
        ):
            payload = pa._session_toolset_report()

        assert json.loads(json.dumps(payload))["report"]["tools"] == ["web_search"]


class TestBothAgentAppsRegisterTheRoute:
    """Dedicated session pods run ``--mode persistent``; pool pods run ``dual``.

    ``/session/status`` exists only on the dual app, so
    ``_thread_turn_in_flight`` 404s against every dedicated session pod — a
    live bug. A toolset route missing from either app would silently downgrade
    that whole population to a prediction, which is the same class of quiet
    wrong answer this task exists to remove.
    """

    @pytest.mark.parametrize("factory", ["persistent", "dual"])
    def test_session_toolset_is_registered(self, factory):
        if factory == "persistent":
            from src.api.persistent_app import create_persistent_app

            app = create_persistent_app("config/session_base.yaml")
        else:
            from src.api.dual_app import create_dual_app

            app = create_dual_app()
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/session/toolset" in paths
