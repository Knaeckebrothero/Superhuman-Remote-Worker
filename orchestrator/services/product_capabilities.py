"""Server-side resolution for the M2 product-capability contract.

The service composes only authorities the orchestrator can truthfully observe:
the immutable build registry, deployment flags/catalogs, effective grants, and
safe facts from an already-admitted thread row. It deliberately does not load
connector credentials, trigger thread-mount backfills, provision resources, or
guess which tools a persistent agent actually instantiated.

Live attachment/tool readiness and final actionability are overlaid by the
agent in M2c. Every result from this module therefore keeps
``agent_action=unknown``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from services.grants_service import resolve_grants_for
from services.shared_browser_canvas import (
    BrowserCapabilityResponse,
    browser_capability,
)
from src.core.datasource_catalog import DATASOURCE_TYPES
from src.core.product_capabilities import (
    AgentAction,
    BuildEvaluation,
    BuildState,
    CAPABILITY_DEFINITIONS,
    CapabilityDefinition,
    CapabilityId,
    CapabilityQualifier,
    CapabilityResolverKey,
    CapabilityScope,
    CapabilityVisibility,
    Completeness,
    ComponentProvenance,
    DeploymentEvaluation,
    DeploymentState,
    EvaluationError,
    EvaluationErrorCode,
    Freshness,
    LayerEvaluation,
    LayerName,
    MAX_EVALUATION_ERRORS,
    ProductCapabilitiesResponse,
    ProductCapability,
    ProductComponent,
    ProductProvenance,
    ProvenanceStatus,
    REGISTRY_REVISION,
    ReasonCode,
    ScopeKind,
    SessionEvaluation,
    SessionState,
    TopicId,
    UserEvaluation,
    UserState,
    evaluate_layer_safely,
    validate_capability_against_definition,
    validate_capability_registry,
)
from src.core.runtime_provenance import (
    build_product_provenance,
    component_provenance_from_environment,
    inherited_content_provenance,
    unavailable_component_provenance,
)

logger = logging.getLogger(__name__)

PRODUCT_CAPABILITIES_ENDPOINT_ENABLED_ENV = "PRODUCT_CAPABILITIES_ENDPOINT_ENABLED"
DEFAULT_RESULT_LIMIT = 20
MAX_EXPLICIT_CAPABILITY_IDS = 20
MAX_RESPONSE_BYTES = 64 * 1024

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_UNSET = object()

_UserId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    ),
]

_GRANT_BY_RESOLVER: Mapping[CapabilityResolverKey, str] = {
    CapabilityResolverKey.CANVAS_BROWSER: "browser",
    CapabilityResolverKey.DATASOURCE_EMAIL: "datasource_tools",
    CapabilityResolverKey.DATASOURCE_EMAIL_SEND: "datasource_tools",
    CapabilityResolverKey.DATASOURCE_OKF: "datasource_tools",
    CapabilityResolverKey.SESSION_DELEGATE: "delegation",
}

_CATALOG_TYPE_BY_RESOLVER: Mapping[CapabilityResolverKey, str] = {
    CapabilityResolverKey.DATASOURCE_EMAIL: "email",
    CapabilityResolverKey.DATASOURCE_EMAIL_SEND: "email",
    CapabilityResolverKey.DATASOURCE_OKF: "kb",
}

_GENERIC_ENABLED_RESOLVERS = frozenset(
    {
        CapabilityResolverKey.AUTOMATIONS_MANAGE,
        CapabilityResolverKey.CANVAS_FILES,
        CapabilityResolverKey.EXPERTS_SELECT,
        CapabilityResolverKey.JOBS_CREATE,
        CapabilityResolverKey.JOBS_REVIEW,
        CapabilityResolverKey.PROJECT_KNOWLEDGE,
        CapabilityResolverKey.PROJECT_LOOPS,
        CapabilityResolverKey.PROJECTS_MANAGE,
        CapabilityResolverKey.SESSION_DELEGATE,
        CapabilityResolverKey.SESSION_PERMISSION_MODE,
        CapabilityResolverKey.WORKSPACE_SELECT,
    }
)


class ResolutionRequest(BaseModel):
    """Canonical immutable inputs after authentication and thread admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: _UserId
    is_admin: bool
    thread_id: UUID | None = None
    primary_project_id: UUID | None = None
    topic: TopicId | None = None
    capability_ids: tuple[CapabilityId, ...] = Field(
        default_factory=tuple,
        max_length=MAX_EXPLICIT_CAPABILITY_IDS,
    )
    limit: int = Field(default=DEFAULT_RESULT_LIMIT, ge=1, le=50)

    @field_validator("capability_ids", mode="before")
    @classmethod
    def validate_raw_capability_count(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and (
            len(value) > MAX_EXPLICIT_CAPABILITY_IDS
        ):
            raise ValueError(
                f"at most {MAX_EXPLICIT_CAPABILITY_IDS} capability IDs are allowed"
            )
        return value

    @field_validator("capability_ids")
    @classmethod
    def canonicalize_capability_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_scope(self) -> ResolutionRequest:
        if self.thread_id is None and self.primary_project_id is not None:
            raise ValueError(
                "primary_project_id is available only for an admitted thread"
            )
        return self

    @classmethod
    def from_admitted(
        cls,
        *,
        user: Mapping[str, Any],
        thread: Mapping[str, Any] | None,
        expected_thread_id: UUID | None,
        topic: str | None,
        capability_ids: tuple[str, ...],
        limit: int,
    ) -> ResolutionRequest:
        """Build a request without retaining the caller or thread payload."""

        raw_user_id = user.get("id")
        if raw_user_id is None:
            raise ValueError("admitted user is missing an ID")

        if thread is None:
            if expected_thread_id is not None:
                raise ValueError("admitted thread is required")
            thread_id = None
            primary_project_id = None
        else:
            raw_thread_id = thread.get("id")
            if raw_thread_id is None:
                raise ValueError("admitted thread is missing an ID")
            thread_id = UUID(str(raw_thread_id))
            if expected_thread_id is not None and thread_id != expected_thread_id:
                raise ValueError("admitted thread does not match the request")
            raw_project_id = thread.get("project_id")
            primary_project_id = UUID(str(raw_project_id)) if raw_project_id else None

        return cls(
            user_id=str(raw_user_id),
            is_admin=bool(user.get("is_admin")),
            thread_id=thread_id,
            primary_project_id=primary_project_id,
            topic=topic,
            capability_ids=capability_ids,
            limit=limit,
        )


GrantsResolver: TypeAlias = Callable[..., Awaitable[Mapping[str, Any]]]
BrowserResolver: TypeAlias = Callable[[dict[str, Any]], BrowserCapabilityResponse]
VisibilityPolicy: TypeAlias = Callable[[CapabilityDefinition, ResolutionRequest], bool]
LayerResolver: TypeAlias = Callable[
    [CapabilityDefinition, "_ResolutionState"], LayerEvaluation
]
LayerResolverOverrides: TypeAlias = Mapping[
    tuple[CapabilityResolverKey, LayerName], LayerResolver
]
AgentProvenanceResolver: TypeAlias = Callable[
    [Any, Mapping[str, Any] | None],
    Awaitable[ComponentProvenance],
]


async def registered_agent_provenance(
    postgres_db: Any,
    admitted_thread: Mapping[str, Any] | None,
) -> ComponentProvenance:
    """Read only the safe, validated provenance from the bound agent row."""

    if admitted_thread is None:
        return unavailable_component_provenance()
    agent_id = admitted_thread.get("agent_id")
    get_agent = getattr(postgres_db, "get_agent", None)
    if not agent_id or not callable(get_agent):
        return unavailable_component_provenance()
    try:
        agent = await get_agent(str(agent_id))
    except Exception:
        return unavailable_component_provenance()
    if not isinstance(agent, Mapping) or agent.get("status") in {"offline", "failed"}:
        return unavailable_component_provenance()

    metadata = agent.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, UnicodeError):
            return unavailable_component_provenance()
    if not isinstance(metadata, Mapping):
        return unavailable_component_provenance()
    try:
        provenance = ComponentProvenance.model_validate(
            metadata.get("product_provenance")
        )
    except (TypeError, ValidationError):
        return unavailable_component_provenance()
    # M2d accepts declarations only. Verified/SLSA provenance remains a later
    # trust-policy feature and cannot be self-asserted by a registering agent.
    if provenance.provenance_status is ProvenanceStatus.VERIFIED:
        return unavailable_component_provenance()
    return provenance


@dataclass(slots=True)
class _ResolutionState:
    request: ResolutionRequest
    admitted_thread: Mapping[str, Any] | None
    grants: Mapping[str, Any] | None
    grants_failed: bool
    observed_at: datetime
    environment: Mapping[str, str]
    browser_resolver: BrowserResolver
    _browser_observation: BrowserCapabilityResponse | BaseException | object = field(
        default=_UNSET,
        init=False,
        repr=False,
    )

    def browser_observation(self) -> BrowserCapabilityResponse:
        """Resolve the existing browser seam once without exposing its error."""

        if self.admitted_thread is None:
            raise RuntimeError("browser observation requires a thread")
        if self._browser_observation is _UNSET:
            try:
                self._browser_observation = self.browser_resolver(
                    dict(self.admitted_thread)
                )
            except Exception as exc:
                self._browser_observation = exc
        if isinstance(self._browser_observation, BaseException):
            raise RuntimeError("browser observation unavailable")
        assert isinstance(self._browser_observation, BrowserCapabilityResponse)
        return self._browser_observation


def product_capabilities_endpoint_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return the temporary operator-owned M2 endpoint rollout gate."""

    source = os.environ if environment is None else environment
    return (
        source.get(PRODUCT_CAPABILITIES_ENDPOINT_ENABLED_ENV, "").strip().lower()
        in _TRUTHY_ENV_VALUES
    )


def _env_enabled(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool = False,
) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


def _layer_source(layer: LayerName) -> ProductComponent:
    if layer is LayerName.BUILD:
        return ProductComponent.REGISTRY
    return ProductComponent.ORCHESTRATOR


def _unknown_layer(
    layer: LayerName,
    *,
    source_component: ProductComponent,
    observed_at: datetime,
    freshness: Freshness = Freshness.UNKNOWN,
) -> LayerEvaluation:
    common = {
        "source_component": source_component,
        "freshness": freshness,
        "observed_at": observed_at,
    }
    if layer is LayerName.BUILD:
        return BuildEvaluation(
            state=BuildState.UNKNOWN,
            reason_code=ReasonCode.BUILD_OBSERVATION_UNAVAILABLE,
            **common,
        )
    if layer is LayerName.DEPLOYMENT:
        return DeploymentEvaluation(
            state=DeploymentState.UNKNOWN,
            reason_code=ReasonCode.DEPLOYMENT_OBSERVATION_UNAVAILABLE,
            **common,
        )
    if layer is LayerName.USER:
        return UserEvaluation(
            state=UserState.UNKNOWN,
            reason_code=ReasonCode.USER_OBSERVATION_UNAVAILABLE,
            **common,
        )
    return SessionEvaluation(
        state=SessionState.UNKNOWN,
        reason_code=ReasonCode.SESSION_OBSERVATION_UNAVAILABLE,
        **common,
    )


def _is_unknown_state(result: LayerEvaluation) -> bool:
    return result.state.value == "unknown"


def _error_sort_key(
    error: EvaluationError,
) -> tuple[str, str, str, str, bool]:
    return (
        error.capability_id or "",
        error.layer.value if error.layer else "",
        error.code.value,
        error.source_component.value,
        error.retryable,
    )


class ProductCapabilityService:
    """Resolve a bounded capability observation from reviewed authorities."""

    def __init__(
        self,
        postgres_db: Any,
        *,
        definitions: tuple[CapabilityDefinition, ...] = CAPABILITY_DEFINITIONS,
        grants_resolver: GrantsResolver = resolve_grants_for,
        browser_resolver: BrowserResolver = browser_capability,
        visibility_policy: VisibilityPolicy | None = None,
        resolver_overrides: LayerResolverOverrides | None = None,
        datasource_types: frozenset[str] = DATASOURCE_TYPES,
        environment: Mapping[str, str] | None = None,
        agent_provenance_resolver: AgentProvenanceResolver = (
            registered_agent_provenance
        ),
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._db = postgres_db
        self._definitions = validate_capability_registry(definitions)
        self._grants_resolver = grants_resolver
        self._browser_resolver = browser_resolver
        self._visibility_policy = visibility_policy or (
            lambda _definition, _request: False
        )
        self._resolver_overrides = dict(resolver_overrides or {})
        self._datasource_types = frozenset(datasource_types)
        self._environment = os.environ if environment is None else environment
        self._agent_provenance_resolver = agent_provenance_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")
        self._max_response_bytes = max_response_bytes

    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        admitted_thread: Mapping[str, Any] | None,
    ) -> ProductCapabilitiesResponse:
        """Resolve one admitted, immutable request without mutating state."""

        started = self._monotonic()
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("capability service clock must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)
        self._validate_admitted_thread(request, admitted_thread)

        selected, selection_errors, truncated = self._select_definitions(request)
        grants, grants_failed = await self._resolve_grants(request, selected)
        product = await self._resolve_product_provenance(admitted_thread)
        state = _ResolutionState(
            request=request,
            admitted_thread=admitted_thread,
            grants=grants,
            grants_failed=grants_failed,
            observed_at=observed_at,
            environment=self._environment,
            browser_resolver=self._browser_resolver,
        )

        capabilities: list[ProductCapability] = []
        errors = list(selection_errors)
        for definition in selected:
            capability, capability_errors = self._resolve_capability(definition, state)
            validate_capability_against_definition(capability, definition)
            capabilities.append(capability)
            errors.extend(capability_errors)

        response = self._bounded_response(
            request=request,
            observed_at=observed_at,
            capabilities=capabilities,
            errors=errors,
            truncated=truncated,
            product=product,
        )
        self._emit_metrics(
            response,
            latency_ms=(self._monotonic() - started) * 1000,
        )
        return response

    @staticmethod
    def _validate_admitted_thread(
        request: ResolutionRequest,
        admitted_thread: Mapping[str, Any] | None,
    ) -> None:
        if request.thread_id is None:
            if admitted_thread is not None:
                raise ValueError("user-scoped request may not carry a thread")
            return
        if admitted_thread is None:
            raise ValueError("thread-scoped request requires an admitted thread")
        try:
            admitted_id = UUID(str(admitted_thread.get("id")))
        except (TypeError, ValueError) as exc:
            raise ValueError("admitted thread has an invalid ID") from exc
        if admitted_id != request.thread_id:
            raise ValueError("admitted thread does not match request scope")
        raw_project_id = admitted_thread.get("project_id")
        admitted_project_id = UUID(str(raw_project_id)) if raw_project_id else None
        if admitted_project_id != request.primary_project_id:
            raise ValueError(
                "admitted thread primary project does not match request scope"
            )

    def _definition_visible(
        self,
        definition: CapabilityDefinition,
        request: ResolutionRequest,
    ) -> bool:
        if definition.visibility is CapabilityVisibility.PUBLIC:
            return True
        try:
            return bool(self._visibility_policy(definition, request))
        except Exception:
            return False

    def _select_definitions(
        self,
        request: ResolutionRequest,
    ) -> tuple[list[CapabilityDefinition], list[EvaluationError], bool]:
        visible = [
            definition
            for definition in self._definitions
            if self._definition_visible(definition, request)
        ]
        visible_ids = {definition.id for definition in visible}
        requested_ids = set(request.capability_ids)

        errors: list[EvaluationError] = []
        if requested_ids - visible_ids:
            errors.append(
                EvaluationError(
                    code=EvaluationErrorCode.CAPABILITY_NOT_VISIBLE,
                    source_component=ProductComponent.ORCHESTRATOR,
                    retryable=False,
                )
            )

        selected = visible
        if requested_ids:
            selected = [
                definition for definition in selected if definition.id in requested_ids
            ]
        if request.topic is not None:
            selected = [
                definition
                for definition in selected
                if request.topic in definition.topics
            ]
        selected.sort(key=lambda definition: definition.id)

        truncated = len(selected) > request.limit
        if truncated:
            selected = selected[: request.limit]
            errors.append(
                EvaluationError(
                    code=EvaluationErrorCode.RESULT_LIMIT,
                    source_component=ProductComponent.ORCHESTRATOR,
                    retryable=False,
                )
            )
        return selected, errors, truncated

    async def _resolve_grants(
        self,
        request: ResolutionRequest,
        definitions: list[CapabilityDefinition],
    ) -> tuple[Mapping[str, Any] | None, bool]:
        if request.is_admin:
            return {}, False
        needs_grants = any(
            definition.resolver_key in _GRANT_BY_RESOLVER
            or definition.resolver_key is CapabilityResolverKey.SESSION_PERMISSION_MODE
            for definition in definitions
        )
        if not needs_grants:
            return {}, False
        project_ids = (
            [str(request.primary_project_id)]
            if request.primary_project_id is not None
            else []
        )
        try:
            grants = await self._grants_resolver(
                self._db,
                user_id=request.user_id,
                project_ids=project_ids,
            )
        except Exception:
            return None, True
        return grants, False

    async def _resolve_product_provenance(
        self,
        admitted_thread: Mapping[str, Any] | None,
    ) -> ProductProvenance:
        orchestrator = component_provenance_from_environment(
            self._environment,
            ProductComponent.ORCHESTRATOR,
            include_common=True,
        )
        try:
            agent = await self._agent_provenance_resolver(
                self._db,
                admitted_thread,
            )
        except Exception:
            agent = unavailable_component_provenance()
        if not isinstance(agent, ComponentProvenance):
            agent = unavailable_component_provenance()

        components = {
            ProductComponent.REGISTRY: inherited_content_provenance(
                orchestrator,
                content_digest=REGISTRY_REVISION,
            ),
            ProductComponent.ORCHESTRATOR: orchestrator,
            ProductComponent.AGENT: agent,
            ProductComponent.COCKPIT: component_provenance_from_environment(
                self._environment,
                ProductComponent.COCKPIT,
            ),
            ProductComponent.GUIDE: unavailable_component_provenance(),
            ProductComponent.WORKSPACE: component_provenance_from_environment(
                self._environment,
                ProductComponent.WORKSPACE,
            ),
            ProductComponent.MCP: component_provenance_from_environment(
                self._environment,
                ProductComponent.MCP,
            ),
        }
        return build_product_provenance(
            components,
            release_version=orchestrator.release_version,
        )

    def _resolve_capability(
        self,
        definition: CapabilityDefinition,
        state: _ResolutionState,
    ) -> tuple[ProductCapability, list[EvaluationError]]:
        results: dict[LayerName, LayerEvaluation] = {}
        errors: list[EvaluationError] = []

        builtin_resolvers: Mapping[LayerName, LayerResolver] = {
            LayerName.BUILD: self._resolve_build,
            LayerName.DEPLOYMENT: self._resolve_deployment,
            LayerName.USER: self._resolve_user,
            LayerName.SESSION: self._resolve_session,
        }
        for layer in (
            LayerName.BUILD,
            LayerName.DEPLOYMENT,
            LayerName.USER,
            LayerName.SESSION,
        ):
            resolver = self._resolver_overrides.get(
                (definition.resolver_key, layer),
                builtin_resolvers[layer],
            )
            result, error = evaluate_layer_safely(
                layer,
                lambda resolver=resolver: resolver(definition, state),
                capability_id=definition.id,
                source_component=_layer_source(layer),
                observed_at=state.observed_at,
            )
            if error is None:
                result, error = self._normalize_freshness(definition.id, layer, result)
            results[layer] = result
            if error is not None:
                errors.append(error)

        capability = ProductCapability(
            id=definition.id,
            visibility=definition.visibility,
            build=results[LayerName.BUILD],
            deployment=results[LayerName.DEPLOYMENT],
            user=results[LayerName.USER],
            session=results[LayerName.SESSION],
            agent_action=AgentAction.UNKNOWN,
            help_topic_id=definition.help_topic_id,
            open_action_id=definition.open_action_id,
            visual_id=definition.visual_id,
        )
        return capability, errors

    @staticmethod
    def _normalize_freshness(
        capability_id: str,
        layer: LayerName,
        result: LayerEvaluation,
    ) -> tuple[LayerEvaluation, EvaluationError | None]:
        if result.freshness is Freshness.STALE:
            return (
                _unknown_layer(
                    layer,
                    source_component=result.source_component,
                    observed_at=result.observed_at,
                    freshness=Freshness.STALE,
                ),
                EvaluationError(
                    code=EvaluationErrorCode.STALE_OBSERVATION,
                    capability_id=capability_id,
                    layer=layer,
                    source_component=result.source_component,
                    retryable=True,
                ),
            )
        if result.freshness is Freshness.UNKNOWN and not _is_unknown_state(result):
            return (
                _unknown_layer(
                    layer,
                    source_component=result.source_component,
                    observed_at=result.observed_at,
                ),
                EvaluationError(
                    code=EvaluationErrorCode.RESOLVER_ERROR,
                    capability_id=capability_id,
                    layer=layer,
                    source_component=result.source_component,
                    retryable=True,
                ),
            )
        return result, None

    @staticmethod
    def _resolve_build(
        _definition: CapabilityDefinition,
        state: _ResolutionState,
    ) -> BuildEvaluation:
        return BuildEvaluation(
            state=BuildState.SUPPORTED,
            reason_code=ReasonCode.INCLUDED_IN_BUILD,
            source_component=ProductComponent.REGISTRY,
            freshness=Freshness.FRESH,
            observed_at=state.observed_at,
        )

    def _resolve_deployment(
        self,
        definition: CapabilityDefinition,
        state: _ResolutionState,
    ) -> DeploymentEvaluation:
        common = {
            "source_component": ProductComponent.ORCHESTRATOR,
            "freshness": Freshness.FRESH,
            "observed_at": state.observed_at,
        }
        resolver_key = definition.resolver_key

        catalog_type = _CATALOG_TYPE_BY_RESOLVER.get(resolver_key)
        if catalog_type is not None:
            if catalog_type not in self._datasource_types:
                raise RuntimeError("connector inventory observation unavailable")
            return DeploymentEvaluation(
                state=DeploymentState.ENABLED,
                reason_code=ReasonCode.CONNECTOR_TYPE_AVAILABLE,
                **common,
            )

        if resolver_key is CapabilityResolverKey.CANVAS_BROWSER:
            if state.admitted_thread is None:
                enabled = _env_enabled(
                    state.environment, "CANVAS_SHARED_BROWSER_ENABLED"
                )
                return DeploymentEvaluation(
                    state=(
                        DeploymentState.ENABLED if enabled else DeploymentState.DISABLED
                    ),
                    reason_code=(
                        ReasonCode.FEATURE_ENABLED
                        if enabled
                        else ReasonCode.FEATURE_DISABLED
                    ),
                    **common,
                )
            observation = state.browser_observation()
            if not observation.feature_enabled:
                return DeploymentEvaluation(
                    state=DeploymentState.DISABLED,
                    reason_code=ReasonCode.FEATURE_DISABLED,
                    **common,
                )
            if observation.reason in {
                "transport_unavailable",
                "workspace_unroutable",
            }:
                return DeploymentEvaluation(
                    state=DeploymentState.DEGRADED,
                    reason_code=ReasonCode.SERVICE_DEGRADED,
                    **common,
                )
            return DeploymentEvaluation(
                state=DeploymentState.ENABLED,
                reason_code=ReasonCode.FEATURE_ENABLED,
                **common,
            )

        if resolver_key is CapabilityResolverKey.SESSION_PROTECTED_CLOUD:
            enabled = _env_enabled(state.environment, "PROTECTED_CLOUD_MODE_ENABLED")
            return DeploymentEvaluation(
                state=(
                    DeploymentState.ENABLED if enabled else DeploymentState.DISABLED
                ),
                reason_code=(
                    ReasonCode.FEATURE_ENABLED
                    if enabled
                    else ReasonCode.FEATURE_DISABLED
                ),
                **common,
            )

        if resolver_key is CapabilityResolverKey.EXPERTS_MANAGE:
            enabled = _env_enabled(
                state.environment,
                "EXPERTS_DB_ENABLED",
                default=True,
            )
            return DeploymentEvaluation(
                state=(
                    DeploymentState.ENABLED if enabled else DeploymentState.DISABLED
                ),
                reason_code=(
                    ReasonCode.FEATURE_ENABLED
                    if enabled
                    else ReasonCode.FEATURE_DISABLED
                ),
                **common,
            )

        if resolver_key is CapabilityResolverKey.MEMORY_RECALL:
            return DeploymentEvaluation(
                state=DeploymentState.UNKNOWN,
                reason_code=ReasonCode.DEPLOYMENT_OBSERVATION_UNAVAILABLE,
                source_component=ProductComponent.ORCHESTRATOR,
                freshness=Freshness.UNKNOWN,
                observed_at=state.observed_at,
            )

        if resolver_key in _GENERIC_ENABLED_RESOLVERS:
            return DeploymentEvaluation(
                state=DeploymentState.ENABLED,
                reason_code=ReasonCode.FEATURE_ENABLED,
                **common,
            )
        raise RuntimeError("deployment resolver is not registered")

    @staticmethod
    def _resolve_user(
        definition: CapabilityDefinition,
        state: _ResolutionState,
    ) -> UserEvaluation:
        common = {
            "source_component": ProductComponent.ORCHESTRATOR,
            "freshness": Freshness.FRESH,
            "observed_at": state.observed_at,
        }
        if state.request.is_admin:
            qualifiers: tuple[CapabilityQualifier, ...] = ()
            if definition.resolver_key is CapabilityResolverKey.SESSION_PERMISSION_MODE:
                qualifiers = (
                    CapabilityQualifier(
                        key="permission_mode.ceiling",
                        value="autonomous",
                    ),
                )
            return UserEvaluation(
                state=UserState.ALLOWED,
                reason_code=ReasonCode.ADMIN_ALLOWED,
                qualifiers=qualifiers,
                **common,
            )

        grant_key = _GRANT_BY_RESOLVER.get(definition.resolver_key)
        if grant_key is not None:
            if state.grants_failed or state.grants is None:
                raise RuntimeError("grant observation unavailable")
            allowed = bool(state.grants.get(grant_key, False))
            return UserEvaluation(
                state=UserState.ALLOWED if allowed else UserState.DENIED,
                reason_code=(
                    ReasonCode.GRANT_ALLOWED if allowed else ReasonCode.GRANT_DENIED
                ),
                **common,
            )

        if definition.resolver_key is CapabilityResolverKey.SESSION_PERMISSION_MODE:
            if state.grants_failed or state.grants is None:
                raise RuntimeError("grant observation unavailable")
            ceiling = state.grants.get("permission_mode")
            qualifier = CapabilityQualifier(
                key="permission_mode.ceiling",
                value=ceiling,
            )
            return UserEvaluation(
                state=UserState.ALLOWED,
                reason_code=ReasonCode.GRANT_ALLOWED,
                qualifiers=(qualifier,),
                **common,
            )

        return UserEvaluation(
            state=UserState.ALLOWED,
            reason_code=ReasonCode.APPROVED_USER,
            **common,
        )

    @staticmethod
    def _resolve_session(
        definition: CapabilityDefinition,
        state: _ResolutionState,
    ) -> SessionEvaluation:
        common = {
            "source_component": ProductComponent.ORCHESTRATOR,
            "observed_at": state.observed_at,
        }
        if state.request.thread_id is None:
            return SessionEvaluation(
                state=SessionState.NOT_APPLICABLE,
                reason_code=ReasonCode.THREAD_NOT_REQUESTED,
                freshness=Freshness.FRESH,
                **common,
            )

        if definition.resolver_key is CapabilityResolverKey.CANVAS_BROWSER:
            observation = state.browser_observation()
            if observation.reason == "workspace_required":
                return SessionEvaluation(
                    state=SessionState.NEEDS_UPGRADE,
                    reason_code=ReasonCode.WORKSPACE_REQUIRED,
                    freshness=Freshness.FRESH,
                    **common,
                )
            if observation.reason in {
                "workspace_unattested",
                "workspace_unroutable",
            }:
                return SessionEvaluation(
                    state=SessionState.NOT_READY,
                    reason_code=ReasonCode.WORKSPACE_NOT_READY,
                    freshness=Freshness.FRESH,
                    **common,
                )
            if observation.can_open_browser and observation.workspace_ready:
                return SessionEvaluation(
                    state=SessionState.READY,
                    reason_code=ReasonCode.WORKSPACE_READY,
                    freshness=Freshness.FRESH,
                    **common,
                )
            if observation.can_open_browser:
                return SessionEvaluation(
                    state=SessionState.NOT_READY,
                    reason_code=ReasonCode.WORKSPACE_NOT_READY,
                    freshness=Freshness.FRESH,
                    **common,
                )

        return SessionEvaluation(
            state=SessionState.UNKNOWN,
            reason_code=ReasonCode.LIVE_AGENT_OBSERVATION_REQUIRED,
            freshness=Freshness.UNKNOWN,
            **common,
        )

    def _bounded_response(
        self,
        *,
        request: ResolutionRequest,
        observed_at: datetime,
        capabilities: list[ProductCapability],
        errors: list[EvaluationError],
        truncated: bool,
        product: ProductProvenance,
    ) -> ProductCapabilitiesResponse:
        errors = sorted(set(errors), key=_error_sort_key)
        if len(errors) > MAX_EVALUATION_ERRORS:
            errors = errors[: MAX_EVALUATION_ERRORS - 1]
            errors.append(self._size_limit_error())
            errors.sort(key=_error_sort_key)
            truncated = True

        response = self._make_response(
            request=request,
            observed_at=observed_at,
            capabilities=capabilities,
            errors=errors,
            truncated=truncated,
            product=product,
        )
        if self._encoded_size(response) <= self._max_response_bytes:
            return response

        truncated = True
        if not any(
            error.code is EvaluationErrorCode.RESPONSE_SIZE_LIMIT for error in errors
        ):
            errors.append(self._size_limit_error())
            errors.sort(key=_error_sort_key)

        while capabilities:
            capabilities.pop()
            response = self._make_response(
                request=request,
                observed_at=observed_at,
                capabilities=capabilities,
                errors=errors,
                truncated=truncated,
                product=product,
            )
            if self._encoded_size(response) <= self._max_response_bytes:
                return response

        while len(errors) > 1:
            removable = next(
                (
                    index
                    for index in range(len(errors) - 1, -1, -1)
                    if errors[index].code is not EvaluationErrorCode.RESPONSE_SIZE_LIMIT
                ),
                None,
            )
            if removable is None:
                break
            errors.pop(removable)
            response = self._make_response(
                request=request,
                observed_at=observed_at,
                capabilities=capabilities,
                errors=errors,
                truncated=truncated,
                product=product,
            )
            if self._encoded_size(response) <= self._max_response_bytes:
                return response

        if self._encoded_size(response) > self._max_response_bytes:
            raise ValueError(
                "max_response_bytes cannot hold the capability response envelope"
            )
        return response

    @staticmethod
    def _size_limit_error() -> EvaluationError:
        return EvaluationError(
            code=EvaluationErrorCode.RESPONSE_SIZE_LIMIT,
            source_component=ProductComponent.ORCHESTRATOR,
            retryable=False,
        )

    @staticmethod
    def _make_response(
        *,
        request: ResolutionRequest,
        observed_at: datetime,
        capabilities: list[ProductCapability],
        errors: list[EvaluationError],
        truncated: bool,
        product: ProductProvenance,
    ) -> ProductCapabilitiesResponse:
        partial = bool(errors) or truncated
        return ProductCapabilitiesResponse(
            registry_revision=REGISTRY_REVISION,
            evaluated_at=observed_at,
            completeness=(Completeness.PARTIAL if partial else Completeness.COMPLETE),
            truncated=truncated,
            scope=CapabilityScope(
                kind=(
                    ScopeKind.THREAD
                    if request.thread_id is not None
                    else ScopeKind.USER
                ),
                thread_id=request.thread_id,
            ),
            product=product,
            capabilities=tuple(capabilities),
            evaluation_errors=tuple(errors),
        )

    @staticmethod
    def _encoded_size(response: ProductCapabilitiesResponse) -> int:
        return len(response.model_dump_json().encode("utf-8"))

    @staticmethod
    def _emit_metrics(
        response: ProductCapabilitiesResponse,
        *,
        latency_ms: float,
    ) -> None:
        for capability in response.capabilities:
            logger.info(
                "product_capability_observation "
                "capability_id=%s "
                "build_state=%s build_reason=%s "
                "deployment_state=%s deployment_reason=%s "
                "user_state=%s user_reason=%s "
                "session_state=%s session_reason=%s "
                "completeness=%s latency_ms=%.3f",
                capability.id,
                capability.build.state.value,
                capability.build.reason_code.value,
                capability.deployment.state.value,
                capability.deployment.reason_code.value,
                capability.user.state.value,
                capability.user.reason_code.value,
                capability.session.state.value,
                capability.session.reason_code.value,
                response.completeness.value,
                latency_ms,
            )
        logger.info(
            "product_capability_response "
            "capability_count=%d error_codes=%s completeness=%s "
            "truncated=%s latency_ms=%.3f",
            len(response.capabilities),
            ",".join(sorted({error.code.value for error in response.evaluation_errors}))
            or "none",
            response.completeness.value,
            str(response.truncated).lower(),
            latency_ms,
        )


__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "LayerResolver",
    "LayerResolverOverrides",
    "MAX_EXPLICIT_CAPABILITY_IDS",
    "MAX_RESPONSE_BYTES",
    "PRODUCT_CAPABILITIES_ENDPOINT_ENABLED_ENV",
    "ProductCapabilityService",
    "ResolutionRequest",
    "product_capabilities_endpoint_enabled",
    "registered_agent_provenance",
]
