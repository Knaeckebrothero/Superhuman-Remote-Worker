"""Focused tests for the M2b server product-capability resolver."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from orchestrator.services.product_capabilities import (
    MAX_EXPLICIT_CAPABILITY_IDS,
    MAX_RESPONSE_BYTES,
    PRODUCT_CAPABILITIES_ENDPOINT_ENABLED_ENV,
    ProductCapabilityService,
    ResolutionRequest,
    product_capabilities_endpoint_enabled,
)
from orchestrator.services.shared_browser_canvas import BrowserCapabilityResponse
from shared.runtime.core.product_capabilities import (
    AgentAction,
    BuildEvaluation,
    BuildState,
    CAPABILITY_DEFINITIONS,
    CAPABILITY_REGISTRY,
    CapabilityResolverKey,
    CapabilityVisibility,
    Completeness,
    DeploymentState,
    EvaluationErrorCode,
    Freshness,
    LayerName,
    ProductComponent,
    ProvenanceStatus,
    ReasonCode,
    ScopeKind,
    SessionState,
    UserState,
    validate_response_against_registry,
)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_THREAD_ID = "22222222-2222-2222-2222-222222222222"
_PROJECT_ID = "33333333-3333-3333-3333-333333333333"
_UNRELATED_PROJECT_ID = "44444444-4444-4444-4444-444444444444"
_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _user(*, is_admin: bool = False) -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "is_admin": is_admin,
        "is_approved": True,
        "email": "private@example.test",
    }


def _thread() -> dict[str, Any]:
    return {
        "id": _THREAD_ID,
        "user_id": _USER_ID,
        "project_id": _PROJECT_ID,
        "metadata": {
            "workspace_backend": "virtual",
            "datasource_ids": ["private-datasource-id"],
        },
    }


def _request(
    *capability_ids: str,
    thread: bool = False,
    is_admin: bool = False,
    topic: str | None = None,
    limit: int = 20,
) -> ResolutionRequest:
    admitted_thread = _thread() if thread else None
    return ResolutionRequest.from_admitted(
        user=_user(is_admin=is_admin),
        thread=admitted_thread,
        expected_thread_id=(None if admitted_thread is None else UUID(_THREAD_ID)),
        topic=topic,
        capability_ids=tuple(capability_ids),
        limit=limit,
    )


def _all_grants() -> dict[str, Any]:
    return {
        "browser": True,
        "datasource_tools": True,
        "delegation": True,
        "email_autonomous_send": False,
        "permission_mode": "auto_accept",
    }


async def _grants_allowed(_db: Any, **_kwargs: Any) -> dict[str, Any]:
    return _all_grants()


def _service(**kwargs: Any) -> ProductCapabilityService:
    return ProductCapabilityService(
        object(),
        grants_resolver=_grants_allowed,
        environment={},
        clock=lambda: _NOW,
        **kwargs,
    )


def _only(response, capability_id: str):
    assert [item.id for item in response.capabilities] == [capability_id]
    return response.capabilities[0]


def test_resolution_request_is_immutable_bounded_and_canonical():
    request = ResolutionRequest(
        user_id=_USER_ID,
        is_admin=False,
        capability_ids=("jobs.review", "jobs.create", "jobs.review"),
    )
    assert request.capability_ids == ("jobs.create", "jobs.review")

    with pytest.raises(ValidationError, match="frozen"):
        request.limit = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ResolutionRequest(
            user_id=_USER_ID,
            is_admin=False,
            primary_project_id=_PROJECT_ID,
        )
    with pytest.raises(ValidationError):
        ResolutionRequest(
            user_id=_USER_ID,
            is_admin=False,
            topic="not exact",
        )
    with pytest.raises(ValidationError):
        ResolutionRequest(
            user_id=_USER_ID,
            is_admin=False,
            capability_ids=("INVALID",),
        )
    with pytest.raises(ValidationError, match="at most"):
        ResolutionRequest(
            user_id=_USER_ID,
            is_admin=False,
            capability_ids=tuple(
                f"feature.item-{index}"
                for index in range(MAX_EXPLICIT_CAPABILITY_IDS + 1)
            ),
        )


def test_request_factory_keeps_only_safe_admitted_scope():
    request = ResolutionRequest.from_admitted(
        user=_user(),
        thread=_thread(),
        expected_thread_id=UUID(_THREAD_ID),
        topic="email",
        capability_ids=("datasources.email",),
        limit=5,
    )

    assert str(request.thread_id) == _THREAD_ID
    assert str(request.primary_project_id) == _PROJECT_ID
    assert request.user_id == _USER_ID
    dumped = request.model_dump(mode="json")
    assert "email" not in dumped["user_id"]
    assert "metadata" not in dumped
    assert "datasource_ids" not in dumped

    with pytest.raises(ValueError, match="does not match"):
        ResolutionRequest.from_admitted(
            user=_user(),
            thread=_thread(),
            expected_thread_id=UUID("55555555-5555-5555-5555-555555555555"),
            topic=None,
            capability_ids=(),
            limit=20,
        )


@pytest.mark.asyncio
async def test_email_user_scope_resolves_build_catalog_grant_and_no_thread():
    grant_calls: list[dict[str, Any]] = []

    async def grants(_db: Any, **kwargs: Any) -> dict[str, Any]:
        grant_calls.append(kwargs)
        return _all_grants()

    response = await ProductCapabilityService(
        object(),
        grants_resolver=grants,
        environment={},
        clock=lambda: _NOW,
    ).resolve(
        _request("datasources.email"),
        admitted_thread=None,
    )
    capability = _only(response, "datasources.email")

    assert response.scope.kind is ScopeKind.USER
    assert response.scope.thread_id is None
    assert response.completeness is Completeness.COMPLETE
    assert response.truncated is False
    assert capability.build.state is BuildState.SUPPORTED
    assert capability.build.reason_code is ReasonCode.INCLUDED_IN_BUILD
    assert capability.deployment.state is DeploymentState.ENABLED
    assert capability.deployment.reason_code is ReasonCode.CONNECTOR_TYPE_AVAILABLE
    assert capability.user.state is UserState.ALLOWED
    assert capability.user.reason_code is ReasonCode.GRANT_ALLOWED
    assert capability.session.state is SessionState.NOT_APPLICABLE
    assert capability.session.reason_code is ReasonCode.THREAD_NOT_REQUESTED
    assert capability.agent_action is AgentAction.UNKNOWN
    assert grant_calls == [{"user_id": _USER_ID, "project_ids": []}]
    assert (
        response.product.components[ProductComponent.ORCHESTRATOR].provenance_status
        is ProvenanceStatus.UNAVAILABLE
    )
    validate_response_against_registry(response, CAPABILITY_DEFINITIONS)


@pytest.mark.asyncio
async def test_thread_uses_only_its_primary_project_grant_scope():
    grant_calls: list[dict[str, Any]] = []

    async def grants(_db: Any, **kwargs: Any) -> dict[str, Any]:
        grant_calls.append(kwargs)
        values = _all_grants()
        values["datasource_tools"] = False
        return values

    request = _request("datasources.email", thread=True)
    response = await ProductCapabilityService(
        object(),
        grants_resolver=grants,
        environment={},
        clock=lambda: _NOW,
    ).resolve(request, admitted_thread=_thread())
    capability = _only(response, "datasources.email")

    assert response.scope.kind is ScopeKind.THREAD
    assert str(response.scope.thread_id) == _THREAD_ID
    assert capability.user.state is UserState.DENIED
    assert capability.user.reason_code is ReasonCode.GRANT_DENIED
    assert capability.session.state is SessionState.UNKNOWN
    assert capability.session.reason_code is ReasonCode.LIVE_AGENT_OBSERVATION_REQUIRED
    assert capability.agent_action is AgentAction.UNKNOWN
    assert grant_calls == [
        {
            "user_id": _USER_ID,
            "project_ids": [_PROJECT_ID],
        }
    ]
    assert _UNRELATED_PROJECT_ID not in str(grant_calls)


@pytest.mark.asyncio
async def test_admin_is_allowed_without_reading_grants():
    async def must_not_resolve(*_args: Any, **_kwargs: Any):
        raise AssertionError("admin capability resolution must not read grants")

    response = await ProductCapabilityService(
        object(),
        grants_resolver=must_not_resolve,
        environment={},
        clock=lambda: _NOW,
    ).resolve(
        _request(
            "datasources.email",
            "sessions.permission-mode",
            is_admin=True,
        ),
        admitted_thread=None,
    )

    assert all(
        capability.user.reason_code is ReasonCode.ADMIN_ALLOWED
        for capability in response.capabilities
    )
    permission = next(
        item for item in response.capabilities if item.id == "sessions.permission-mode"
    )
    assert permission.user.qualifiers[0].key == "permission_mode.ceiling"
    assert permission.user.qualifiers[0].value == "autonomous"


@pytest.mark.asyncio
async def test_permission_mode_uses_the_effective_grant_ceiling():
    response = await _service().resolve(
        _request("sessions.permission-mode"),
        admitted_thread=None,
    )
    capability = _only(response, "sessions.permission-mode")

    assert capability.user.state is UserState.ALLOWED
    assert capability.user.reason_code is ReasonCode.GRANT_ALLOWED
    assert [
        qualifier.model_dump(mode="json") for qualifier in capability.user.qualifiers
    ] == [{"key": "permission_mode.ceiling", "value": "auto_accept"}]


@pytest.mark.asyncio
async def test_grant_failure_isolated_to_capabilities_that_need_grants():
    async def broken_grants(_db: Any, **_kwargs: Any):
        raise RuntimeError("postgresql://private-host/secret")

    response = await ProductCapabilityService(
        object(),
        grants_resolver=broken_grants,
        environment={},
        clock=lambda: _NOW,
    ).resolve(
        _request("datasources.email", "jobs.create"),
        admitted_thread=None,
    )
    by_id = {item.id: item for item in response.capabilities}

    assert response.completeness is Completeness.PARTIAL
    assert by_id["datasources.email"].user.state is UserState.UNKNOWN
    assert by_id["jobs.create"].user.state is UserState.ALLOWED
    assert by_id["jobs.create"].user.reason_code is ReasonCode.APPROVED_USER
    assert [
        (error.code, error.capability_id, error.layer)
        for error in response.evaluation_errors
    ] == [
        (
            EvaluationErrorCode.RESOLVER_ERROR,
            "datasources.email",
            LayerName.USER,
        )
    ]
    serialized = response.model_dump_json()
    assert "private-host" not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_catalog_drift_fails_closed_instead_of_claiming_email_available():
    response = await _service(datasource_types=frozenset()).resolve(
        _request("datasources.email"),
        admitted_thread=None,
    )
    capability = _only(response, "datasources.email")

    assert response.completeness is Completeness.PARTIAL
    assert capability.deployment.state is DeploymentState.UNKNOWN
    assert (
        capability.deployment.reason_code
        is ReasonCode.DEPLOYMENT_OBSERVATION_UNAVAILABLE
    )
    assert response.evaluation_errors[0].code is EvaluationErrorCode.RESOLVER_ERROR
    assert response.evaluation_errors[0].layer is LayerName.DEPLOYMENT


@pytest.mark.asyncio
async def test_resolver_exception_and_stale_observation_become_unknown():
    def raises_secret(_definition, _state):
        raise RuntimeError("password=hunter2")

    def stale_build(_definition, state):
        return BuildEvaluation(
            state=BuildState.SUPPORTED,
            reason_code=ReasonCode.INCLUDED_IN_BUILD,
            source_component=ProductComponent.REGISTRY,
            freshness=Freshness.STALE,
            observed_at=state.observed_at,
        )

    response = await _service(
        resolver_overrides={
            (
                CapabilityResolverKey.DATASOURCE_EMAIL,
                LayerName.DEPLOYMENT,
            ): raises_secret,
            (
                CapabilityResolverKey.DATASOURCE_EMAIL,
                LayerName.BUILD,
            ): stale_build,
        }
    ).resolve(
        _request("datasources.email"),
        admitted_thread=None,
    )
    capability = _only(response, "datasources.email")

    assert capability.build.state is BuildState.UNKNOWN
    assert capability.build.freshness is Freshness.STALE
    assert capability.deployment.state is DeploymentState.UNKNOWN
    assert {error.code for error in response.evaluation_errors} == {
        EvaluationErrorCode.RESOLVER_ERROR,
        EvaluationErrorCode.STALE_OBSERVATION,
    }
    serialized = response.model_dump_json()
    assert "hunter2" not in serialized
    assert "password" not in serialized


@pytest.mark.parametrize(
    ("observation", "deployment_state", "session_state"),
    [
        (
            BrowserCapabilityResponse(
                feature_enabled=False,
                can_open_browser=False,
                workspace_ready=False,
                reason="feature_disabled",
            ),
            DeploymentState.DISABLED,
            SessionState.UNKNOWN,
        ),
        (
            BrowserCapabilityResponse(
                feature_enabled=True,
                can_open_browser=False,
                workspace_ready=False,
                reason="workspace_required",
            ),
            DeploymentState.ENABLED,
            SessionState.NEEDS_UPGRADE,
        ),
        (
            BrowserCapabilityResponse(
                feature_enabled=True,
                can_open_browser=True,
                workspace_ready=True,
                reason=None,
            ),
            DeploymentState.ENABLED,
            SessionState.READY,
        ),
        (
            BrowserCapabilityResponse(
                feature_enabled=True,
                can_open_browser=False,
                workspace_ready=True,
                reason="transport_unavailable",
            ),
            DeploymentState.DEGRADED,
            SessionState.UNKNOWN,
        ),
    ],
)
@pytest.mark.asyncio
async def test_browser_adapter_reuses_one_existing_capability_observation(
    observation: BrowserCapabilityResponse,
    deployment_state: DeploymentState,
    session_state: SessionState,
):
    calls = 0

    def browser(_thread_payload: dict[str, Any]) -> BrowserCapabilityResponse:
        nonlocal calls
        calls += 1
        return observation

    response = await ProductCapabilityService(
        object(),
        grants_resolver=_grants_allowed,
        browser_resolver=browser,
        environment={"CANVAS_SHARED_BROWSER_ENABLED": "true"},
        clock=lambda: _NOW,
    ).resolve(
        _request("canvas.browser", thread=True),
        admitted_thread=_thread(),
    )
    capability = _only(response, "canvas.browser")

    assert capability.deployment.state is deployment_state
    assert capability.session.state is session_state
    assert capability.agent_action is AgentAction.UNKNOWN
    assert calls == 1


@pytest.mark.asyncio
async def test_real_deployment_flags_are_reported_without_synthetic_email_flag():
    response = await ProductCapabilityService(
        object(),
        grants_resolver=_grants_allowed,
        environment={
            "PROTECTED_CLOUD_MODE_ENABLED": "true",
            "EXPERTS_DB_ENABLED": "false",
            "MADE_UP_EMAIL_ENABLED": "false",
        },
        clock=lambda: _NOW,
    ).resolve(
        _request(
            "datasources.email",
            "experts.manage",
            "sessions.protected-cloud",
        ),
        admitted_thread=None,
    )
    by_id = {item.id: item for item in response.capabilities}

    assert by_id["datasources.email"].deployment.state is DeploymentState.ENABLED
    assert (
        by_id["datasources.email"].deployment.reason_code
        is ReasonCode.CONNECTOR_TYPE_AVAILABLE
    )
    assert by_id["experts.manage"].deployment.state is DeploymentState.DISABLED
    assert by_id["sessions.protected-cloud"].deployment.state is DeploymentState.ENABLED


@pytest.mark.asyncio
async def test_topic_and_id_filters_intersect_and_results_are_sorted():
    response = await _service().resolve(
        _request(
            "jobs.create",
            "datasources.email.send",
            "datasources.email",
            topic="email",
        ),
        admitted_thread=None,
    )

    assert [item.id for item in response.capabilities] == [
        "datasources.email",
        "datasources.email.send",
    ]
    assert not response.evaluation_errors


@pytest.mark.asyncio
async def test_conditional_visibility_and_hidden_unknown_requests_do_not_leak():
    public = CAPABILITY_REGISTRY["datasources.email"]
    conditional = CAPABILITY_REGISTRY["datasources.email.send"].model_copy(
        update={"visibility": CapabilityVisibility.CONDITIONAL}
    )
    hidden = CAPABILITY_REGISTRY["datasources.okf"].model_copy(
        update={"visibility": CapabilityVisibility.HIDDEN}
    )
    service = _service(
        definitions=(public, conditional, hidden),
        visibility_policy=lambda definition, _request: (
            definition.visibility is CapabilityVisibility.CONDITIONAL
        ),
    )

    visible = await service.resolve(_request(), admitted_thread=None)
    assert [item.id for item in visible.capabilities] == [
        "datasources.email",
        "datasources.email.send",
    ]

    hidden_result = await service.resolve(
        _request("datasources.okf"),
        admitted_thread=None,
    )
    unknown_result = await service.resolve(
        _request("unregistered.feature"),
        admitted_thread=None,
    )
    for result in (hidden_result, unknown_result):
        assert result.capabilities == ()
        assert result.completeness is Completeness.PARTIAL
        assert len(result.evaluation_errors) == 1
        error = result.evaluation_errors[0]
        assert error.code is EvaluationErrorCode.CAPABILITY_NOT_VISIBLE
        assert error.capability_id is None
        assert error.layer is None

    assert hidden_result.evaluation_errors == unknown_result.evaluation_errors


@pytest.mark.asyncio
async def test_visibility_policy_failure_fails_closed_without_error_text():
    conditional = CAPABILITY_REGISTRY["datasources.email"].model_copy(
        update={"visibility": CapabilityVisibility.CONDITIONAL}
    )

    def broken_policy(_definition, _request):
        raise RuntimeError("private discovery policy detail")

    result = await _service(
        definitions=(conditional,),
        visibility_policy=broken_policy,
    ).resolve(
        _request("datasources.email"),
        admitted_thread=None,
    )

    assert result.capabilities == ()
    assert (
        result.evaluation_errors[0].code is EvaluationErrorCode.CAPABILITY_NOT_VISIBLE
    )
    assert "private discovery" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_result_limit_and_encoded_byte_cap_truncate_deterministically():
    limited = await _service().resolve(
        _request(limit=1),
        admitted_thread=None,
    )
    assert [item.id for item in limited.capabilities] == ["automations.manage"]
    assert limited.truncated is True
    assert limited.completeness is Completeness.PARTIAL
    assert {error.code for error in limited.evaluation_errors} == {
        EvaluationErrorCode.RESULT_LIMIT
    }

    byte_limited_service = _service(max_response_bytes=5_000)
    first = await byte_limited_service.resolve(
        ResolutionRequest(user_id=_USER_ID, is_admin=False, limit=50),
        admitted_thread=None,
    )
    second = await byte_limited_service.resolve(
        ResolutionRequest(user_id=_USER_ID, is_admin=False, limit=50),
        admitted_thread=None,
    )

    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.model_dump_json().encode("utf-8")) <= 5_000
    assert first.truncated is True
    assert EvaluationErrorCode.RESPONSE_SIZE_LIMIT in {
        error.code for error in first.evaluation_errors
    }
    assert [item.id for item in first.capabilities] == sorted(
        item.id for item in first.capabilities
    )
    assert 5_000 < MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_service_emits_privacy_safe_shadow_metrics(caplog):
    def fails_with_private_data(_definition, _state):
        raise RuntimeError(
            "user=private@example.test password=secret host=imap.internal"
        )

    caplog.set_level(
        logging.INFO,
        logger="orchestrator.services.product_capabilities",
    )
    response = await _service(
        resolver_overrides={
            (
                CapabilityResolverKey.DATASOURCE_EMAIL,
                LayerName.DEPLOYMENT,
            ): fails_with_private_data,
        }
    ).resolve(
        _request("datasources.email", thread=True),
        admitted_thread=_thread(),
    )
    logs = caplog.text

    assert response.completeness is Completeness.PARTIAL
    assert "capability_id=datasources.email" in logs
    assert "deployment_state=unknown" in logs
    assert "latency_ms=" in logs
    for private_value in (
        _USER_ID,
        _THREAD_ID,
        _PROJECT_ID,
        "private@example.test",
        "password",
        "secret",
        "imap.internal",
        "private-datasource-id",
    ):
        assert private_value not in logs


@pytest.mark.asyncio
async def test_resolution_has_no_write_provision_or_datasource_side_effects():
    class ReadOnlySentinel:
        def __init__(self):
            self.grant_reads: list[dict[str, Any]] = []

        async def list_grants_for_scopes(self, **kwargs: Any):
            self.grant_reads.append(kwargs)
            return {"user": [], "project": [], "global": []}

        def __getattr__(self, name: str):
            raise AssertionError(f"unexpected database access: {name}")

    database = ReadOnlySentinel()
    response = await ProductCapabilityService(
        database,
        browser_resolver=lambda _thread: BrowserCapabilityResponse(
            feature_enabled=True,
            can_open_browser=False,
            workspace_ready=False,
            reason="workspace_required",
        ),
        environment={},
        clock=lambda: _NOW,
    ).resolve(
        _request("canvas.browser", "datasources.email", thread=True),
        admitted_thread=_thread(),
    )

    assert len(response.capabilities) == 2
    assert response.capabilities[0].agent_action is AgentAction.UNKNOWN
    assert response.capabilities[1].agent_action is AgentAction.UNKNOWN
    assert database.grant_reads == [
        {
            "user_id": _USER_ID,
            "project_ids": [_PROJECT_ID],
        }
    ]


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("unexpected", False),
    ],
)
def test_endpoint_rollout_gate_is_operator_owned_and_default_off(
    raw: str | None,
    enabled: bool,
):
    environment = {}
    if raw is not None:
        environment[PRODUCT_CAPABILITIES_ENDPOINT_ENABLED_ENV] = raw
    assert product_capabilities_endpoint_enabled(environment) is enabled
