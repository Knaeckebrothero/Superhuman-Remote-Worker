"""Closed helpers for declared SRW runtime/component provenance.

This module accepts already-trusted process/deployment environment mappings,
validates only the bounded public fields in the product-capability contract,
and never reads an image tag as a source revision or artifact digest.

OCI labels are declarations embedded by the image build. Runtime processes
receive the same common ``SRW_*`` fields from their own image plus an optional
deployment JSON document for independently deployed components. Neither form
is treated as verified provenance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from shared.runtime.core.product_capabilities import (
    ComponentProvenance,
    ProductComponent,
    ProductProvenance,
    ProvenanceStatus,
)

DEPLOYMENT_PROVENANCE_ENV = "SRW_DEPLOYMENT_PROVENANCE_JSON"
MAX_DEPLOYMENT_PROVENANCE_BYTES = 16 * 1024

_INDEPENDENT_COMPONENTS = frozenset(
    {
        ProductComponent.ORCHESTRATOR,
        ProductComponent.AGENT,
        ProductComponent.COCKPIT,
        ProductComponent.WORKSPACE,
        ProductComponent.MCP,
    }
)


def unavailable_component_provenance() -> ComponentProvenance:
    """Return the canonical evidence-free provenance value."""

    return ComponentProvenance(provenance_status=ProvenanceStatus.UNAVAILABLE)


def _deployment_document(environment: Mapping[str, str]) -> Mapping[str, Any]:
    raw = environment.get(DEPLOYMENT_PROVENANCE_ENV, "")
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw.encode("utf-8")) > (MAX_DEPLOYMENT_PROVENANCE_BYTES)
    ):
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _candidate_values(
    environment: Mapping[str, str],
    component: ProductComponent,
    *,
    include_common: bool,
) -> dict[str, list[str]]:
    """Collect high-to-low-priority candidates without validating them."""

    document = _deployment_document(environment)
    component_document: Mapping[str, Any] = {}
    components = document.get("components")
    if isinstance(components, dict):
        candidate = components.get(component.value)
        if isinstance(candidate, dict):
            component_document = candidate

    prefix = f"SRW_{component.value.upper()}_"
    values: dict[str, list[str]] = {
        "source_revision": [],
        "source_url": [],
        "artifact_digest": [],
        "release_version": [],
        "documentation_url": [],
    }

    direct_names = {
        "source_revision": f"{prefix}SOURCE_REVISION",
        "source_url": f"{prefix}SOURCE_URL",
        "artifact_digest": f"{prefix}ARTIFACT_DIGEST",
        "release_version": f"{prefix}RELEASE_VERSION",
        "documentation_url": f"{prefix}DOCUMENTATION_URL",
    }
    document_names = {
        "source_revision": "source_revision",
        "source_url": "source_url",
        "artifact_digest": "artifact_digest",
        "release_version": "release_version",
        "documentation_url": "documentation_url",
    }
    global_document_names = {
        "source_url": "source_url",
        "release_version": "release_version",
        "documentation_url": "documentation_url",
    }

    if include_common and environment.get("SRW_COMPONENT") == component.value:
        common_names = {
            "source_revision": "SRW_SOURCE_REVISION",
            "source_url": "SRW_SOURCE_URL",
            "artifact_digest": "SRW_ARTIFACT_DIGEST",
            "release_version": "SRW_RELEASE_VERSION",
            "documentation_url": "SRW_DOCUMENTATION_URL",
        }
        for field_name, environment_name in common_names.items():
            value = _nonempty_string(environment.get(environment_name))
            if value is not None:
                values[field_name].append(value)

    for field_name, environment_name in direct_names.items():
        value = _nonempty_string(environment.get(environment_name))
        if value is not None:
            values[field_name].append(value)

    for field_name, document_name in document_names.items():
        value = _nonempty_string(component_document.get(document_name))
        if value is not None:
            values[field_name].append(value)

    for field_name, document_name in global_document_names.items():
        value = _nonempty_string(document.get(document_name))
        if value is not None:
            values[field_name].append(value)

    return values


def _first_valid_field(
    candidates: list[str],
    field_name: str,
) -> str | None:
    """Return the first candidate accepted by ComponentProvenance."""

    for value in candidates:
        payload: dict[str, Any] = {
            "provenance_status": ProvenanceStatus.DECLARED,
            field_name: value,
        }
        # URL fields are supplemental, so give them the minimum identity
        # evidence required to exercise their own validators.
        if field_name in {"source_url", "documentation_url"}:
            payload["release_version"] = "validation"
        if field_name == "source_url":
            payload["source_revision"] = "0" * 40
        try:
            parsed = ComponentProvenance.model_validate(payload)
        except ValidationError:
            continue
        return getattr(parsed, field_name)
    return None


def component_provenance_from_environment(
    environment: Mapping[str, str],
    component: ProductComponent,
    *,
    include_common: bool = False,
    content_digest: str | None = None,
) -> ComponentProvenance:
    """Build one safe declared/unavailable component observation.

    Common ``SRW_*`` variables are consumed only when ``SRW_COMPONENT`` exactly
    names the requested component. Component-prefixed fields and the bounded
    deployment JSON remain available for orchestrator-side declarations.
    """

    candidates = _candidate_values(
        environment,
        component,
        include_common=include_common,
    )
    source_revision = _first_valid_field(
        candidates["source_revision"],
        "source_revision",
    )
    source_url = (
        _first_valid_field(candidates["source_url"], "source_url")
        if source_revision is not None
        else None
    )
    artifact_digest = _first_valid_field(
        candidates["artifact_digest"],
        "artifact_digest",
    )
    release_version = _first_valid_field(
        candidates["release_version"],
        "release_version",
    )
    documentation_url = _first_valid_field(
        candidates["documentation_url"],
        "documentation_url",
    )
    valid_content_digest = _first_valid_field(
        [content_digest] if content_digest else [],
        "content_digest",
    )

    if not any(
        (
            source_revision,
            artifact_digest,
            valid_content_digest,
            release_version,
        )
    ):
        return unavailable_component_provenance()

    return ComponentProvenance(
        source_revision=source_revision,
        source_url=source_url,
        artifact_digest=artifact_digest,
        content_digest=valid_content_digest,
        release_version=release_version,
        documentation_url=documentation_url,
        provenance_status=ProvenanceStatus.DECLARED,
    )


def inherited_content_provenance(
    parent: ComponentProvenance,
    *,
    content_digest: str,
) -> ComponentProvenance:
    """Bind a content bundle to its parent source without copying image digest."""

    valid_content_digest = _first_valid_field([content_digest], "content_digest")
    if valid_content_digest is None:
        return unavailable_component_provenance()
    return ComponentProvenance(
        source_revision=parent.source_revision,
        source_url=parent.source_url,
        content_digest=valid_content_digest,
        release_version=parent.release_version,
        documentation_url=parent.documentation_url,
        provenance_status=ProvenanceStatus.DECLARED,
    )


def build_product_provenance(
    components: Mapping[ProductComponent, ComponentProvenance],
    *,
    release_version: str | None = None,
) -> ProductProvenance:
    """Canonicalize components and derive mixed/indeterminate build state."""

    ordered = {
        component: components[component]
        for component in sorted(components, key=lambda item: item.value)
    }
    independent_revisions = [
        provenance.source_revision
        for component, provenance in ordered.items()
        if component in _INDEPENDENT_COMPONENTS
        and provenance.source_revision is not None
    ]
    mixed_build: bool | None = None
    if len(independent_revisions) >= 2:
        mixed_build = len(set(independent_revisions)) > 1

    valid_release_version = _first_valid_field(
        [release_version] if release_version else [],
        "release_version",
    )
    return ProductProvenance(
        release_version=valid_release_version,
        mixed_build=mixed_build,
        components=ordered,
    )


def merge_product_provenance(
    base: ProductProvenance,
    replacements: Mapping[ProductComponent, ComponentProvenance],
) -> ProductProvenance:
    """Replace locally observed components and recompute mixed-build state."""

    components = dict(base.components)
    components.update(replacements)
    return build_product_provenance(
        components,
        release_version=base.release_version,
    )


__all__ = [
    "DEPLOYMENT_PROVENANCE_ENV",
    "MAX_DEPLOYMENT_PROVENANCE_BYTES",
    "build_product_provenance",
    "component_provenance_from_environment",
    "inherited_content_provenance",
    "merge_product_provenance",
    "unavailable_component_provenance",
]
