"""Bundled configuration catalogue and persisted override administration.

Catalogue reads remain uncached and structured writes retain the shipped
catalogue's fail-closed validation. Matrix resolution stays with its existing
loader; these operations never resolve a different runtime configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException

from orchestrator.schemas.config_catalog import (
    ConfigOverrideCreate,
    ConfigOverrideUpdate,
)


class ConfigCatalogStore(Protocol):
    async def list_config_overrides(self) -> list[dict[str, Any]]: ...
    async def get_config_override(self, override_id: str) -> dict[str, Any] | None: ...
    async def upsert_config_override(
        self,
        *,
        family: str | None,
        kind: str,
        name: str,
        content: str | None = None,
        content_format: str | None = "text",
        value_json: Any = None,
        notes: str | None = None,
        user_id: Any = None,
    ) -> dict[str, Any]: ...
    async def delete_config_override(self, override_id: str) -> bool: ...


class BundledTextResolver(Protocol):
    def load(self, entry_type: str, *, bundled_only: bool = False) -> str: ...


class MatrixResolverFactory(Protocol):
    def __call__(
        self, deployment_dir: None, model_family: str, /
    ) -> BundledTextResolver: ...


@dataclass(frozen=True)
class ConfigCatalogService:
    store: ConfigCatalogStore
    project_root: Callable[[], Path]
    settings_for_family: Callable[[str, str], Any]
    guardrails_for_family: Callable[[str], dict[str, Any]]
    prompt_resolver: MatrixResolverFactory
    instruction_resolver: MatrixResolverFactory

    def load_config_catalog(self) -> list[dict[str, Any]]:
        """Human-facing descriptions for editable prompt keys.

        Read from config/prompts/catalog.yaml (shipped with the image). Missing
        file -> empty list.
        """
        import yaml

        path = self.project_root() / "config" / "prompts" / "catalog.yaml"
        if not path.exists():
            return []
        return yaml.safe_load(path.read_text()) or []

    def _config_catalog_entry(self, kind: str, name: str) -> dict[str, Any] | None:
        for entry in self.load_config_catalog():
            if entry.get("kind") == kind and entry.get("name") == name:
                return entry
        return None

    def validate_override_value(self, kind: str, name: str, value: Any) -> None:
        """Validate a structured (settings/guardrails) override value against the
        catalog. Raises HTTPException(422) on unknown key, wrong type, or out-of-bounds.

        Text kinds are validated by the Pydantic model (min_length) and are a no-op
        here. Fail-closed on write; reads stay fail-open (see the loader).
        """
        if kind not in ("settings", "guardrails"):
            return
        entry = self._config_catalog_entry(kind, name)
        if entry is None:
            raise HTTPException(status_code=422, detail=f"unknown {kind} key: {name!r}")
        vtype = entry.get("type")
        if vtype in ("number", "integer"):
            # bool is a subclass of int — reject it for numeric leaves.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(status_code=422, detail=f"{name} must be a {vtype}")
            if (
                vtype == "integer"
                and isinstance(value, float)
                and not value.is_integer()
            ):
                raise HTTPException(
                    status_code=422, detail=f"{name} must be an integer"
                )
            lo, hi = entry.get("min"), entry.get("max")
            if lo is not None and value < lo:
                raise HTTPException(status_code=422, detail=f"{name} must be >= {lo}")
            if hi is not None and value > hi:
                raise HTTPException(status_code=422, detail=f"{name} must be <= {hi}")
        elif vtype == "boolean":
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"{name} must be a boolean")
        elif vtype == "enum":
            choices = entry.get("enum", [])
            if value not in choices:
                raise HTTPException(
                    status_code=422, detail=f"{name} must be one of {choices}"
                )
        elif vtype == "json":
            if not isinstance(value, dict):
                raise HTTPException(
                    status_code=422, detail=f"{name} must be a JSON object"
                )
        # Unknown/absent type -> accept (forward-compatible with new catalog types).

    def read_bundled_config(self, kind: str, family: str | None, name: str) -> Any:
        """Read the shipped (bundled) value for (kind, family, name), bypassing overrides.

        Returns text for prompts/instructions; the file-resolved value for settings
        (a single leaf) and guardrails (the {tool_examples, nudges} dict).
        """

        if kind == "settings":
            return self.settings_for_family(family or "default", name)
        if kind == "guardrails":
            return self.guardrails_for_family(family or "default")

        resolver_cls = {
            "prompts": self.prompt_resolver,
            "instructions": self.instruction_resolver,
        }.get(kind)
        if resolver_cls is None:
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
        resolver = resolver_cls(None, family or "default")
        try:
            return resolver.load(name, bundled_only=True)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail="no bundled default for that key"
            )

    async def list_config_overrides(self) -> list[dict[str, Any]]:
        """List all prompt overrides (system-wide)."""
        return await self.store.list_config_overrides()

    async def get_config_override(self, override_id: str) -> dict[str, Any]:
        """Fetch a single prompt override by id."""
        row = await self.store.get_config_override(override_id)
        if not row:
            raise HTTPException(status_code=404, detail="override not found")
        return row

    async def create_config_override(
        self, body: ConfigOverrideCreate, *, user: dict[str, Any]
    ) -> dict[str, Any]:
        """Create or replace the override for (family, kind, name)."""
        is_text = body.kind in ("prompts", "instructions")
        if not is_text:
            self.validate_override_value(body.kind, body.name, body.value_json)
        return await self.store.upsert_config_override(
            family=body.family,
            kind=body.kind,
            name=body.name,
            content=body.content,
            content_format=body.content_format if is_text else None,
            value_json=body.value_json,
            notes=body.notes,
            user_id=user.get("id"),
        )

    async def update_config_override(
        self, override_id: str, body: ConfigOverrideUpdate, *, user: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing override's payload (family/kind/name are immutable)."""
        existing = await self.store.get_config_override(override_id)
        if not existing:
            raise HTTPException(status_code=404, detail="override not found")
        kind = existing["kind"]
        if kind in ("prompts", "instructions"):
            if body.content is None:
                raise HTTPException(
                    status_code=422, detail="content is required for this kind"
                )
            content, content_format, value_json = (
                body.content,
                body.content_format,
                None,
            )
        else:  # settings, guardrails
            if body.value_json is None:
                raise HTTPException(
                    status_code=422, detail="value_json is required for this kind"
                )
            self.validate_override_value(kind, existing["name"], body.value_json)
            content, content_format, value_json = None, None, body.value_json
        return await self.store.upsert_config_override(
            family=existing["family"],
            kind=kind,
            name=existing["name"],
            content=content,
            content_format=content_format,
            value_json=value_json,
            notes=body.notes,
            user_id=user.get("id"),
        )

    async def delete_config_override(self, override_id: str) -> dict[str, Any]:
        """Delete an override (reset to the bundled default)."""
        if not await self.store.delete_config_override(override_id):
            raise HTTPException(status_code=404, detail="override not found")
        return {"deleted": True}

    async def config_catalog(self) -> list[dict[str, Any]]:
        """List the editable prompt keys with human descriptions."""
        return self.load_config_catalog()

    async def get_bundled_config(
        self, family: str, kind: str, name: str
    ) -> dict[str, Any]:
        """Return the bundled (shipped) default for a key, plus its catalog entry.

        ``family='_'`` stands in for the global/default family.
        """
        fam = None if family == "_" else family
        return {
            "family": fam,
            "kind": kind,
            "name": name,
            "content": self.read_bundled_config(kind, fam, name),
            "catalog": self._config_catalog_entry(kind, name),
        }
