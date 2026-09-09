"""Inputs to non-mutating manifest operations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ManifestExportInput(ManifestPreviewInput):
    output_format: Literal["yaml", "json"] = "yaml"
