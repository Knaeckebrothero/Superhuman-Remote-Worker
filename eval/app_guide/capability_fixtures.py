"""Validated synthetic capability snapshots for the held-out M2 model suite."""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.product_capabilities import (
    AgentAction,
    BuildEvaluation,
    BuildState,
    CapabilityScope,
    CapabilityVisibility,
    Completeness,
    ComponentProvenance,
    DeploymentEvaluation,
    DeploymentState,
    EvaluationError,
    EvaluationErrorCode,
    Freshness,
    LayerName,
    ProductCapabilitiesResponse,
    ProductCapability,
    ProductComponent,
    ProductProvenance,
    ProvenanceStatus,
    ReasonCode,
    SchemaCompatibility,
    ScopeKind,
    SessionEvaluation,
    SessionState,
    UserEvaluation,
    UserState,
)
from src.tools.product_capabilities import (
    ProductCapabilitiesToolOutput,
    _bounded_success_output,
)

CAPABILITY_FIXTURE_NAMES = frozenset(
    {
        "email_send_ready",
        "email_send_denied",
        "email_send_partial",
        "email_send_mixed",
    }
)

_OBSERVED_AT = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
_THREAD_ID = "22222222-2222-2222-2222-222222222222"
_REGISTRY_REVISION = "sha256:" + ("1" * 64)


def _layer_common(component: ProductComponent) -> dict[str, object]:
    return {
        "source_component": component,
        "freshness": Freshness.FRESH,
        "observed_at": _OBSERVED_AT,
    }


def _product(*, mixed: bool = False) -> ProductProvenance:
    if not mixed:
        return ProductProvenance()
    return ProductProvenance(
        mixed_build=True,
        components={
            ProductComponent.ORCHESTRATOR: ComponentProvenance(
                source_revision="a" * 40,
                provenance_status=ProvenanceStatus.DECLARED,
            ),
            ProductComponent.AGENT: ComponentProvenance(
                source_revision="b" * 40,
                provenance_status=ProvenanceStatus.DECLARED,
            ),
        },
    )


def _email_send_capability(
    *,
    user_state: UserState = UserState.ALLOWED,
    session_state: SessionState = SessionState.READY,
    action: AgentAction = AgentAction.CAN_EXECUTE,
) -> ProductCapability:
    user_reason = (
        ReasonCode.GRANT_ALLOWED
        if user_state is UserState.ALLOWED
        else ReasonCode.GRANT_DENIED
    )
    session_reason = (
        ReasonCode.TOOL_LOADED
        if session_state is SessionState.READY
        else ReasonCode.SESSION_OBSERVATION_UNAVAILABLE
    )
    session_freshness = (
        Freshness.FRESH if session_state is SessionState.READY else Freshness.UNKNOWN
    )
    return ProductCapability(
        id="datasources.email.send",
        visibility=CapabilityVisibility.PUBLIC,
        build=BuildEvaluation(
            state=BuildState.SUPPORTED,
            reason_code=ReasonCode.INCLUDED_IN_BUILD,
            **_layer_common(ProductComponent.REGISTRY),
        ),
        deployment=DeploymentEvaluation(
            state=DeploymentState.ENABLED,
            reason_code=ReasonCode.CONNECTOR_TYPE_AVAILABLE,
            **_layer_common(ProductComponent.ORCHESTRATOR),
        ),
        user=UserEvaluation(
            state=user_state,
            reason_code=user_reason,
            **_layer_common(ProductComponent.ORCHESTRATOR),
        ),
        session=SessionEvaluation(
            state=session_state,
            reason_code=session_reason,
            source_component=ProductComponent.AGENT,
            freshness=session_freshness,
            observed_at=_OBSERVED_AT,
        ),
        agent_action=action,
        help_topic_id="datasources-email",
    )


def capability_fixture(name: str) -> ProductCapabilitiesToolOutput:
    """Return one production-validated, privacy-safe synthetic observation."""

    if name not in CAPABILITY_FIXTURE_NAMES:
        raise ValueError(f"unknown capability fixture {name!r}")

    user_state = UserState.ALLOWED
    session_state = SessionState.READY
    action = AgentAction.CAN_EXECUTE
    completeness = Completeness.COMPLETE
    errors: tuple[EvaluationError, ...] = ()
    if name == "email_send_denied":
        user_state = UserState.DENIED
        action = AgentAction.UNAVAILABLE
    elif name == "email_send_partial":
        session_state = SessionState.UNKNOWN
        action = AgentAction.UNKNOWN
        completeness = Completeness.PARTIAL
        errors = (
            EvaluationError(
                code=EvaluationErrorCode.RESOLVER_ERROR,
                capability_id="datasources.email.send",
                layer=LayerName.SESSION,
                source_component=ProductComponent.AGENT,
                retryable=True,
            ),
        )

    response = ProductCapabilitiesResponse(
        registry_revision=_REGISTRY_REVISION,
        evaluated_at=_OBSERVED_AT,
        completeness=completeness,
        scope=CapabilityScope(kind=ScopeKind.THREAD, thread_id=_THREAD_ID),
        product=_product(mixed=name == "email_send_mixed"),
        capabilities=(
            _email_send_capability(
                user_state=user_state,
                session_state=session_state,
                action=action,
            ),
        ),
        evaluation_errors=errors,
    )
    return _bounded_success_output(
        response,
        compatibility=SchemaCompatibility.EXACT,
        error_code=None,
    )


def capability_fixture_json(name: str) -> str:
    """Serialize a fixture exactly as a model-facing tool result."""

    return capability_fixture(name).model_dump_json(
        exclude_none=True,
        by_alias=True,
    )
