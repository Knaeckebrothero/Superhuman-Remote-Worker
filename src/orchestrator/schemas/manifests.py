"""Inputs to manifest resource and execution operations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shared.manifests.validation import MAX_SOURCE_BYTES


class ManifestScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["Account", "Project", "Catalog"]
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", max_length=63)


class ManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=MAX_SOURCE_BYTES)
    format: Literal["yaml", "json"] = "yaml"


class ManifestPreviewInput(ManifestInput):
    default_scope: ManifestScope | None = None
    resolution: Literal["bundle", "stored"] = "bundle"


class ManifestExportInput(ManifestPreviewInput):
    output_format: Literal["yaml", "json"] = "yaml"


class ManifestApplyInput(ManifestInput):
    default_scope: ManifestScope | None = None
    expected_versions: dict[str, int] = Field(default_factory=dict, max_length=100)
    plan_revision: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ManifestSecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    scope: ManifestScope | None = None
    values: dict[str, SecretStr] = Field(min_length=1, max_length=100)
    expected_version: int | None = Field(default=None, ge=1)


class ManifestOutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    attempt: int = Field(ge=1)
    outcome: Literal["Succeeded", "Failed"]
