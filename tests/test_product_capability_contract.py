"""Contract tests for the M2 product-capability vocabulary and build registry."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.core.product_capabilities import (
    AgentAction,
    BuildEvaluation,
    BuildState,
    CAPABILITY_DEFINITIONS,
    CAPABILITY_REGISTRY,
    CapabilityComponents,
    CapabilityDefinition,
    CapabilityQualifier,
    CapabilityScope,
    CapabilityVisibility,
    Completeness,
    ComponentProvenance,
    DeploymentEvaluation,
    DeploymentState,
    EvaluationError,
    EvaluationErrorCode,
    Freshness,
    INITIAL_CAPABILITY_IDS,
    LayerName,
    MAX_CAPABILITIES_PER_RESPONSE,
    MAX_EVALUATION_ERRORS,
    ProductCapabilitiesResponse,
    ProductCapability,
    ProductComponent,
    ProductProvenance,
    ProvenanceStatus,
    PUBLIC_QUALIFIER_KEYS,
    REASON_CODE_COMPATIBILITY,
    REGISTRY_REVISION,
    ReasonCode,
    SCHEMA_VERSION,
    SchemaCompatibility,
    ScopeKind,
    SessionEvaluation,
    SessionState,
    UserEvaluation,
    UserState,
    capability_registry_revision,
    derive_agent_action,
    evaluate_layer_safely,
    is_supported_schema_version,
    parse_schema_version,
    schema_compatibility,
    validate_capability_against_definition,
    validate_capability_registry,
    validate_registry_guide_links,
    validate_response_against_registry,
)

_ROOT = Path(__file__).resolve().parents[1]
_GUIDE_REFERENCES = _ROOT / "config" / "skills" / "app-guide" / "references"
_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
_THREAD_ID = "01234567-89ab-cdef-0123-456789abcdef"
_EXPECTED_CAPABILITY_IDS = (
    "automations.manage",
    "canvas.browser",
    "canvas.files",
    "datasources.email",
    "datasources.email.send",
    "datasources.okf",
    "experts.manage",
    "experts.select",
    "jobs.create",
    "jobs.review",
    "memory.recall",
    "projects.knowledge",
    "projects.loops",
    "projects.manage",
    "sessions.delegate",
    "sessions.permission-mode",
    "sessions.protected-cloud",
    "workspaces.select",
)


def _guide_metadata() -> dict[str, dict[str, Any]]:
    metadata_by_ref: dict[str, dict[str, Any]] = {}
    for path in sorted(_GUIDE_REFERENCES.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        metadata_text, _ = text.removeprefix("---\n").split("\n---\n", 1)
        metadata = yaml.safe_load(metadata_text)
        assert isinstance(metadata, dict), path
        metadata_by_ref[f"references/{path.name}"] = metadata
    return metadata_by_ref


def _build(
    *,
    state: BuildState = BuildState.SUPPORTED,
    reason_code: ReasonCode = ReasonCode.INCLUDED_IN_BUILD,
) -> BuildEvaluation:
    return BuildEvaluation(
        state=state,
        reason_code=reason_code,
        source_component=ProductComponent.REGISTRY,
        freshness=Freshness.FRESH,
        observed_at=_NOW,
    )


def _deployment(
    *,
    state: DeploymentState = DeploymentState.ENABLED,
    reason_code: ReasonCode = ReasonCode.CONNECTOR_TYPE_AVAILABLE,
) -> DeploymentEvaluation:
    return DeploymentEvaluation(
        state=state,
        reason_code=reason_code,
        source_component=ProductComponent.ORCHESTRATOR,
        freshness=Freshness.FRESH,
        observed_at=_NOW,
    )


def _user(
    *,
    state: UserState = UserState.ALLOWED,
    reason_code: ReasonCode = ReasonCode.GRANT_ALLOWED,
) -> UserEvaluation:
    return UserEvaluation(
        state=state,
        reason_code=reason_code,
        source_component=ProductComponent.ORCHESTRATOR,
        freshness=Freshness.FRESH,
        observed_at=_NOW,
    )


def _session(
    *,
    state: SessionState = SessionState.NEEDS_ATTACHMENT,
    reason_code: ReasonCode = ReasonCode.DATASOURCE_NOT_ATTACHED,
    qualifiers: tuple[CapabilityQualifier, ...] = (),
) -> SessionEvaluation:
    return SessionEvaluation(
        state=state,
        reason_code=reason_code,
        source_component=ProductComponent.AGENT,
        freshness=Freshness.FRESH,
        observed_at=_NOW,
        qualifiers=qualifiers,
    )


def _capability(
    capability_id: str = "datasources.email",
    *,
    session: SessionEvaluation | None = None,
    agent_action: AgentAction = AgentAction.CAN_GUIDE,
) -> ProductCapability:
    definition = CAPABILITY_REGISTRY[capability_id]
    return ProductCapability(
        id=definition.id,
        visibility=definition.visibility,
        build=_build(),
        deployment=_deployment(),
        user=_user(),
        session=session or _session(),
        agent_action=agent_action,
        help_topic_id=definition.help_topic_id,
        open_action_id=definition.open_action_id,
        visual_id=definition.visual_id,
    )


def _response(
    *capabilities: ProductCapability,
    completeness: Completeness = Completeness.COMPLETE,
    evaluation_errors: tuple[EvaluationError, ...] = (),
) -> ProductCapabilitiesResponse:
    return ProductCapabilitiesResponse(
        registry_revision=REGISTRY_REVISION,
        evaluated_at=_NOW,
        completeness=completeness,
        scope=CapabilityScope(kind=ScopeKind.THREAD, thread_id=_THREAD_ID),
        product=ProductProvenance(),
        capabilities=capabilities,
        evaluation_errors=evaluation_errors,
    )


def test_schema_1_1_is_closed_and_machine_readable():
    schema = ProductCapabilitiesResponse.model_json_schema()

    assert SCHEMA_VERSION == "1.1"
    assert schema["properties"]["schema_version"]["const"] == "1.1"
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ProductCapability"]["additionalProperties"] is False
    assert json.loads(json.dumps(schema)) == schema


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("1.0", SchemaCompatibility.SAME_MAJOR),
        ("1.1", SchemaCompatibility.EXACT),
        ("1.999", SchemaCompatibility.SAME_MAJOR),
        ("2.0", SchemaCompatibility.UNSUPPORTED_MAJOR),
        ("0.9", SchemaCompatibility.UNSUPPORTED_MAJOR),
        ("1", SchemaCompatibility.INVALID),
        ("01.0", SchemaCompatibility.INVALID),
        ("1.0.0", SchemaCompatibility.INVALID),
        ("latest", SchemaCompatibility.INVALID),
    ],
)
def test_schema_compatibility_has_major_minor_semantics(
    observed: str, expected: SchemaCompatibility
):
    assert schema_compatibility(observed) is expected
    assert is_supported_schema_version(observed) is (
        expected in {SchemaCompatibility.EXACT, SchemaCompatibility.SAME_MAJOR}
    )


def test_schema_parser_rejects_non_strings_and_noncanonical_versions():
    assert parse_schema_version("1.20") == (1, 20)
    with pytest.raises(ValueError, match="must be a string"):
        parse_schema_version(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical"):
        parse_schema_version("1.00")


def test_registry_contains_exactly_the_reviewed_guide_ids_and_is_immutable():
    assert INITIAL_CAPABILITY_IDS == _EXPECTED_CAPABILITY_IDS
    assert tuple(CAPABILITY_REGISTRY) == _EXPECTED_CAPABILITY_IDS
    assert len(CAPABILITY_DEFINITIONS) == 18
    assert len({item.resolver_key for item in CAPABILITY_DEFINITIONS}) == 18
    assert all(
        item.visibility is CapabilityVisibility.PUBLIC
        for item in CAPABILITY_DEFINITIONS
    )

    with pytest.raises(TypeError):
        CAPABILITY_REGISTRY["other.feature"] = CAPABILITY_DEFINITIONS[0]  # type: ignore[index]
    with pytest.raises(ValidationError, match="frozen"):
        CAPABILITY_DEFINITIONS[0].summary = "Changed"  # type: ignore[misc]


def test_registry_revision_is_canonical_and_changes_with_semantics():
    reversed_definitions = tuple(reversed(CAPABILITY_DEFINITIONS))
    changed = CAPABILITY_DEFINITIONS[0].model_copy(
        update={"summary": CAPABILITY_DEFINITIONS[0].summary + " Updated."}
    )
    changed_definitions = (changed, *CAPABILITY_DEFINITIONS[1:])

    assert re.fullmatch(r"sha256:[a-f0-9]{64}", REGISTRY_REVISION)
    assert capability_registry_revision(reversed_definitions) == REGISTRY_REVISION
    assert capability_registry_revision(changed_definitions) != REGISTRY_REVISION


def test_registry_rejects_duplicate_ids_and_resolvers():
    with pytest.raises(ValueError, match="duplicate capability IDs"):
        validate_capability_registry(
            (*CAPABILITY_DEFINITIONS, CAPABILITY_DEFINITIONS[0])
        )

    duplicate_resolver = CAPABILITY_DEFINITIONS[1].model_copy(
        update={"resolver_key": CAPABILITY_DEFINITIONS[0].resolver_key}
    )
    with pytest.raises(ValueError, match="duplicate capability resolver keys"):
        validate_capability_registry(
            (
                CAPABILITY_DEFINITIONS[0],
                duplicate_resolver,
                *CAPABILITY_DEFINITIONS[2:],
            )
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"id": "Email"},
        {"topics": ("z-topic", "a-topic")},
        {"topics": ("email", "email")},
        {"summary": " leading whitespace"},
        {"guide_ref": "references/overview.md"},
        {"resolver_key": "arbitrary.import.path"},
        {"public_qualifier_keys": ("private.detail",)},
    ],
)
def test_definition_model_rejects_invalid_or_unsafe_metadata(updates: dict[str, Any]):
    payload = CAPABILITY_DEFINITIONS[3].model_dump(mode="python")
    payload.update(updates)
    with pytest.raises(ValidationError):
        CapabilityDefinition.model_validate(payload)


def test_definition_model_rejects_extra_fields_and_bounds():
    payload = CAPABILITY_DEFINITIONS[0].model_dump(mode="python")
    with pytest.raises(ValidationError, match="Extra inputs"):
        CapabilityDefinition.model_validate({**payload, "internal_flag": True})
    with pytest.raises(ValidationError):
        CapabilityDefinition.model_validate({**payload, "summary": "x" * 241})


def test_registry_links_every_definition_to_live_app_guide_metadata():
    metadata = _guide_metadata()
    validate_registry_guide_links(CAPABILITY_DEFINITIONS, metadata)

    documented_ids = {
        capability_id
        for item in metadata.values()
        for capability_id in item.get("capability_ids", [])
    }
    assert documented_ids == set(INITIAL_CAPABILITY_IDS)
    assert {definition.guide_ref for definition in CAPABILITY_DEFINITIONS} <= set(
        metadata
    )


def test_guide_link_validation_fails_closed_for_drift():
    metadata = _guide_metadata()
    missing_primary = dict(metadata)
    missing_primary.pop(CAPABILITY_DEFINITIONS[0].guide_ref)
    with pytest.raises(ValueError, match="missing primary guide"):
        validate_registry_guide_links(CAPABILITY_DEFINITIONS, missing_primary)

    unknown_id = {key: dict(value) for key, value in metadata.items()}
    automations = unknown_id["references/automations.md"]
    automations["capability_ids"] = [
        *automations["capability_ids"],
        "unregistered.feature",
    ]
    with pytest.raises(ValueError, match="unregistered capability ID"):
        validate_registry_guide_links(CAPABILITY_DEFINITIONS, unknown_id)

    duplicate_id = {key: dict(value) for key, value in metadata.items()}
    automations = duplicate_id["references/automations.md"]
    automations["capability_ids"] = [
        *automations["capability_ids"],
        "automations.manage",
    ]
    with pytest.raises(ValueError, match="capability_ids must be unique"):
        validate_registry_guide_links(CAPABILITY_DEFINITIONS, duplicate_id)


def test_every_registered_state_has_at_least_one_valid_reason():
    model_by_layer = {
        LayerName.BUILD: BuildEvaluation,
        LayerName.DEPLOYMENT: DeploymentEvaluation,
        LayerName.USER: UserEvaluation,
        LayerName.SESSION: SessionEvaluation,
    }
    source_by_layer = {
        LayerName.BUILD: ProductComponent.REGISTRY,
        LayerName.DEPLOYMENT: ProductComponent.ORCHESTRATOR,
        LayerName.USER: ProductComponent.ORCHESTRATOR,
        LayerName.SESSION: ProductComponent.AGENT,
    }

    for layer, reasons_by_state in REASON_CODE_COMPATIBILITY.items():
        assert reasons_by_state
        for state, reasons in reasons_by_state.items():
            assert reasons
            for reason in reasons:
                result = model_by_layer[layer](
                    state=state,
                    reason_code=reason,
                    source_component=source_by_layer[layer],
                    freshness=Freshness.FRESH,
                    observed_at=_NOW,
                )
                assert result.reason_code is reason


@pytest.mark.parametrize(
    ("model", "state", "reason"),
    [
        (BuildEvaluation, BuildState.SUPPORTED, ReasonCode.NOT_IN_BUILD),
        (
            DeploymentEvaluation,
            DeploymentState.ENABLED,
            ReasonCode.FEATURE_DISABLED,
        ),
        (UserEvaluation, UserState.ALLOWED, ReasonCode.GRANT_DENIED),
        (
            SessionEvaluation,
            SessionState.READY,
            ReasonCode.DATASOURCE_NOT_ATTACHED,
        ),
    ],
)
def test_layer_models_reject_contradictory_state_reason_pairs(
    model: type[Any], state: Any, reason: ReasonCode
):
    with pytest.raises(ValidationError, match="invalid for"):
        model(
            state=state,
            reason_code=reason,
            source_component=ProductComponent.ORCHESTRATOR,
            freshness=Freshness.FRESH,
            observed_at=_NOW,
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("email.access_tier", "send"),
        ("permission_mode.ceiling", "auto_accept"),
        ("workspace.backend", "sandbox"),
        ("workspace.supports_shell", True),
    ],
)
def test_qualifiers_accept_only_registered_key_specific_values(
    key: str, value: str | bool
):
    qualifier = CapabilityQualifier(key=key, value=value)
    assert qualifier.key in PUBLIC_QUALIFIER_KEYS
    assert qualifier.value == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("private.detail", "safe-looking"),
        ("email.access_tier", "admin"),
        ("permission_mode.ceiling", "root"),
        ("workspace.supports_shell", "true"),
        ("workspace.backend", "internal.example"),
        ("workspace.backend", "contains spaces"),
        ("workspace.backend", "https://internal.example"),
        ("workspace.backend", "line\nbreak"),
        ("workspace.backend", {}),
        ("workspace.backend", []),
        ("workspace.backend", ["same", "same"]),
        ("workspace.backend", 1_000_000_001),
    ],
)
def test_qualifiers_reject_unknown_free_form_or_wrong_typed_values(
    key: str, value: Any
):
    with pytest.raises(ValidationError):
        CapabilityQualifier(key=key, value=value)


def test_qualifier_and_layer_models_are_closed_sorted_and_unique():
    with pytest.raises(ValidationError, match="Extra inputs"):
        CapabilityQualifier.model_validate(
            {"key": "email.access_tier", "value": "read", "message": "secret"}
        )

    shell = CapabilityQualifier(key="workspace.supports_shell", value=True)
    backend = CapabilityQualifier(key="workspace.backend", value="sandbox")
    with pytest.raises(ValidationError, match="sorted"):
        _session(qualifiers=(shell, backend))
    with pytest.raises(ValidationError, match="at most once"):
        _session(qualifiers=(backend, backend))


def test_capability_results_enforce_definition_qualifier_allowlists():
    tier = CapabilityQualifier(key="email.access_tier", value="read_write")
    capability = _capability(session=_session(qualifiers=(tier,)))
    validate_capability_against_definition(
        capability, CAPABILITY_REGISTRY["datasources.email"]
    )

    workspace = CapabilityQualifier(key="workspace.backend", value="sandbox")
    unsafe_for_email = _capability(session=_session(qualifiers=(workspace,)))
    with pytest.raises(ValueError, match="undeclared qualifier"):
        validate_capability_against_definition(
            unsafe_for_email, CAPABILITY_REGISTRY["datasources.email"]
        )


def test_component_roles_and_provenance_require_consistent_evidence():
    with pytest.raises(ValidationError, match="runtime authority"):
        CapabilityComponents(
            authority=ProductComponent.REGISTRY,
            execution=ProductComponent.AGENT,
            presentation=ProductComponent.COCKPIT,
            guidance=ProductComponent.GUIDE,
        )
    with pytest.raises(ValidationError, match="guidance component"):
        CapabilityComponents(
            authority=ProductComponent.ORCHESTRATOR,
            execution=ProductComponent.AGENT,
            presentation=ProductComponent.COCKPIT,
            guidance=ProductComponent.COCKPIT,
        )
    with pytest.raises(ValidationError, match="may not carry evidence"):
        ComponentProvenance(
            source_revision="a" * 40,
            provenance_status=ProvenanceStatus.UNAVAILABLE,
        )
    with pytest.raises(ValidationError, match="requires identity evidence"):
        ComponentProvenance(provenance_status=ProvenanceStatus.DECLARED)
    with pytest.raises(ValidationError, match="absolute HTTPS"):
        ComponentProvenance(
            source_revision="a" * 40,
            source_url="http://example.invalid/repository",
            provenance_status=ProvenanceStatus.DECLARED,
        )
    with pytest.raises(ValidationError, match="artifact_digest"):
        ComponentProvenance(
            source_revision="a" * 40,
            provenance_status=ProvenanceStatus.VERIFIED,
        )
    declared = ComponentProvenance(
        source_revision="a" * 40,
        source_url="https://github.com/example/srw",
        release_version="v1.2.3",
        documentation_url="https://docs.example.test/srw",
        provenance_status=ProvenanceStatus.DECLARED,
    )
    assert declared.release_version == "v1.2.3"
    assert declared.documentation_url == "https://docs.example.test/srw"
    with pytest.raises(ValidationError, match="may not carry evidence"):
        ComponentProvenance(
            release_version="v1.2.3",
            provenance_status=ProvenanceStatus.UNAVAILABLE,
        )


def test_product_provenance_preserves_mixed_and_indeterminate_builds():
    unavailable = ProductProvenance()
    assert unavailable.mixed_build is None

    mixed = ProductProvenance(
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
    assert mixed.mixed_build is True

    with pytest.raises(ValidationError, match="conflicting revisions"):
        ProductProvenance(
            mixed_build=True,
            components={
                ProductComponent.ORCHESTRATOR: ComponentProvenance(
                    source_revision="a" * 40,
                    provenance_status=ProvenanceStatus.DECLARED,
                ),
                ProductComponent.AGENT: ComponentProvenance(
                    source_revision="a" * 40,
                    provenance_status=ProvenanceStatus.DECLARED,
                ),
            },
        )


def test_scope_errors_and_response_completeness_are_bounded():
    with pytest.raises(ValidationError, match="may not include thread_id"):
        CapabilityScope(kind=ScopeKind.USER, thread_id=_THREAD_ID)
    with pytest.raises(ValidationError, match="requires thread_id"):
        CapabilityScope(kind=ScopeKind.THREAD)
    with pytest.raises(ValidationError, match="Extra inputs"):
        EvaluationError.model_validate(
            {
                "code": "resolver_error",
                "capability_id": "datasources.email",
                "layer": "deployment",
                "source_component": "orchestrator",
                "retryable": True,
                "message": "private provider failure",
            }
        )
    with pytest.raises(ValidationError, match="require capability_id and layer"):
        EvaluationError(
            code=EvaluationErrorCode.RESOLVER_ERROR,
            source_component=ProductComponent.ORCHESTRATOR,
            retryable=True,
        )
    with pytest.raises(ValidationError, match="may not reveal"):
        EvaluationError(
            code=EvaluationErrorCode.CAPABILITY_NOT_VISIBLE,
            capability_id="hidden.feature",
            source_component=ProductComponent.ORCHESTRATOR,
            retryable=False,
        )

    bounded_error = EvaluationError(
        code=EvaluationErrorCode.RESULT_LIMIT,
        source_component=ProductComponent.ORCHESTRATOR,
        retryable=False,
    )
    with pytest.raises(ValidationError, match="completeness=partial"):
        _response(evaluation_errors=(bounded_error,))
    partial = _response(
        completeness=Completeness.PARTIAL,
        evaluation_errors=(bounded_error,),
    )
    assert partial.completeness is Completeness.PARTIAL


def test_response_rejects_duplicate_unsorted_and_naive_observations():
    email = _capability("datasources.email")
    email_send = _capability("datasources.email.send")
    with pytest.raises(ValidationError, match="duplicate"):
        _response(email, email)
    with pytest.raises(ValidationError, match="sorted"):
        _response(email_send, email)
    with pytest.raises(ValidationError, match="timezone"):
        ProductCapabilitiesResponse(
            registry_revision=REGISTRY_REVISION,
            evaluated_at=_NOW.replace(tzinfo=None),
            completeness=Completeness.COMPLETE,
            scope=CapabilityScope(kind=ScopeKind.USER),
            product=ProductProvenance(),
        )


def test_response_collection_bounds_are_enforced():
    assert MAX_CAPABILITIES_PER_RESPONSE == 50
    assert MAX_EVALUATION_ERRORS == 64

    capability = _capability()
    with pytest.raises(ValidationError):
        _response(*(capability for _ in range(MAX_CAPABILITIES_PER_RESPONSE + 1)))

    bounded_error = EvaluationError(
        code=EvaluationErrorCode.RESULT_LIMIT,
        source_component=ProductComponent.ORCHESTRATOR,
        retryable=False,
    )
    with pytest.raises(ValidationError):
        _response(
            completeness=Completeness.PARTIAL,
            evaluation_errors=(bounded_error,) * (MAX_EVALUATION_ERRORS + 1),
        )


def test_response_validation_binds_results_to_registry_revision_and_metadata():
    email = _capability()
    response = _response(email)
    validate_response_against_registry(response, CAPABILITY_DEFINITIONS)

    wrong_revision = response.model_copy(
        update={"registry_revision": "sha256:" + ("0" * 64)}
    )
    with pytest.raises(ValueError, match="registry_revision"):
        validate_response_against_registry(wrong_revision, CAPABILITY_DEFINITIONS)

    unknown_capability = ProductCapability(
        **{
            **email.model_dump(mode="python"),
            "id": "unregistered.feature",
        }
    )
    with pytest.raises(ValueError, match="unregistered capability"):
        validate_response_against_registry(
            _response(unknown_capability), CAPABILITY_DEFINITIONS
        )

    wrong_help = ProductCapability(
        **{
            **email.model_dump(mode="python"),
            "help_topic_id": "overview",
        }
    )
    with pytest.raises(ValueError, match="help topic"):
        validate_response_against_registry(
            _response(wrong_help), CAPABILITY_DEFINITIONS
        )


def test_layer_resolver_exceptions_and_wrong_types_fail_closed():
    expected_unknown_states = {
        LayerName.BUILD: BuildState.UNKNOWN,
        LayerName.DEPLOYMENT: DeploymentState.UNKNOWN,
        LayerName.USER: UserState.UNKNOWN,
        LayerName.SESSION: SessionState.UNKNOWN,
    }

    def raises_private_error():
        raise RuntimeError("credential=do-not-serialize")

    for layer, expected_state in expected_unknown_states.items():
        result, error = evaluate_layer_safely(
            layer,
            raises_private_error,
            capability_id="datasources.email",
            source_component=ProductComponent.ORCHESTRATOR,
            observed_at=_NOW,
        )
        assert result.state is expected_state
        assert result.freshness is Freshness.UNKNOWN
        assert error is not None
        assert error.code is EvaluationErrorCode.RESOLVER_ERROR
        assert "credential" not in json.dumps(error.model_dump(mode="json"))

    wrong_type, error = evaluate_layer_safely(
        LayerName.BUILD,
        lambda: _deployment(),
        capability_id="datasources.email",
        source_component=ProductComponent.REGISTRY,
        observed_at=_NOW,
    )
    assert wrong_type.state is BuildState.UNKNOWN
    assert error is not None


def test_unknown_or_negative_layers_never_derive_positive_actionability():
    ready = _session(
        state=SessionState.READY,
        reason_code=ReasonCode.DATASOURCE_ATTACHED,
    )
    assert (
        derive_agent_action(
            build=_build(),
            deployment=_deployment(),
            user=_user(),
            session=ready,
            has_execute_tool=True,
            has_proposal_tool=True,
            has_guide=True,
        )
        is AgentAction.CAN_EXECUTE
    )
    assert (
        derive_agent_action(
            build=_build(),
            deployment=_deployment(),
            user=_user(),
            session=ready,
            has_execute_tool=False,
            has_proposal_tool=True,
            has_guide=True,
        )
        is AgentAction.CAN_PROPOSE
    )

    unknown_build = _build(
        state=BuildState.UNKNOWN,
        reason_code=ReasonCode.BUILD_OBSERVATION_UNAVAILABLE,
    )
    assert (
        derive_agent_action(
            build=unknown_build,
            deployment=_deployment(),
            user=_user(),
            session=ready,
            has_execute_tool=True,
            has_proposal_tool=True,
            has_guide=True,
        )
        is AgentAction.CAN_GUIDE
    )
    assert (
        derive_agent_action(
            build=unknown_build,
            deployment=_deployment(),
            user=_user(),
            session=ready,
            has_execute_tool=True,
            has_proposal_tool=True,
            has_guide=False,
        )
        is AgentAction.UNKNOWN
    )

    denied = _user(state=UserState.DENIED, reason_code=ReasonCode.GRANT_DENIED)
    assert (
        derive_agent_action(
            build=_build(),
            deployment=_deployment(),
            user=denied,
            session=ready,
            has_execute_tool=True,
            has_proposal_tool=True,
            has_guide=False,
        )
        is AgentAction.UNAVAILABLE
    )

    no_thread = _session(
        state=SessionState.NOT_APPLICABLE,
        reason_code=ReasonCode.THREAD_NOT_REQUESTED,
    )
    assert (
        derive_agent_action(
            build=_build(),
            deployment=_deployment(),
            user=_user(),
            session=no_thread,
            has_execute_tool=True,
            has_proposal_tool=True,
            has_guide=True,
        )
        is AgentAction.UNKNOWN
    )


def test_result_model_rejects_impossible_positive_actions():
    with pytest.raises(ValidationError, match="can_execute requires"):
        _capability(agent_action=AgentAction.CAN_EXECUTE)

    denied = _capability().model_dump(mode="python")
    denied["user"] = _user(
        state=UserState.DENIED,
        reason_code=ReasonCode.GRANT_DENIED,
    )
    denied["agent_action"] = AgentAction.CAN_PROPOSE
    with pytest.raises(ValidationError, match="can_propose requires"):
        ProductCapability.model_validate(denied)

    no_thread = _capability().model_dump(mode="python")
    no_thread["session"] = _session(
        state=SessionState.NOT_APPLICABLE,
        reason_code=ReasonCode.THREAD_NOT_REQUESTED,
    )
    with pytest.raises(ValidationError, match="agent_action=unknown"):
        ProductCapability.model_validate(no_thread)
