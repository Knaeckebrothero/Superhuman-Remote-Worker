"""The reference harness's private configuration inside an Expert manifest.

Only the SRW harness uses this module. Generic resource operations never unwrap
or interpret ``runtime.config``. The private fragment keeps the reference
harness's inheritance and null-clearing semantics; the containing manifest is
ordinary, immutable JSON data until this adapter is explicitly selected.
"""

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml

SRW_HARNESS_ADAPTER = "srw/v1"
BUNDLED_SRW_IMAGE = "srw-agent:latest"


def validate_srw_asset_name(value: Any) -> str:
    """An installed expert/library asset selector, never a filesystem path."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:(?:experts|subagents)/)?[A-Za-z0-9][A-Za-z0-9_-]*", value
    ):
        raise ValueError("SRW harness asset_name must select a bundled asset directory")
    return value


def srw_private_config(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return SRW settings only for the explicitly selected reference adapter."""
    if manifest.get("apiVersion") != "srw/v1alpha1" or manifest.get("kind") != "Expert":
        raise ValueError("Expected an srw/v1alpha1 Expert manifest")
    runtime = (manifest.get("spec") or {}).get("runtime") or {}
    if runtime.get("adapter") != SRW_HARNESS_ADAPTER:
        raise ValueError("This Expert does not select the srw/v1 harness adapter")
    private = runtime.get("config") or {}
    if not isinstance(private, dict):
        raise ValueError("SRW harness configuration must be an object")
    for key in ("config", "prompts"):
        if key in private and not isinstance(private[key], dict):
            raise ValueError(f"SRW harness {key} must be an object")
    if "asset_name" in private:
        validate_srw_asset_name(private["asset_name"])
    layers = private.get("layers", [])
    if not isinstance(layers, list) or any(
        not isinstance(layer, dict) for layer in layers
    ):
        raise ValueError("SRW harness layers must be an array of objects")
    return deepcopy(private)


def srw_config_fragment(document: Any) -> dict[str, Any]:
    """Read a reference-harness leaf or one of its private base/overlay files.

    Bare mappings remain the private base-file format and historical import
    format. A manifest always requires the explicit adapter; it cannot fall
    through to the old loader merely because it happens to contain tool keys.
    """
    if not isinstance(document, dict):
        raise ValueError("SRW harness configuration must be an object")
    if "apiVersion" in document or "kind" in document:
        return srw_private_config(document).get("config", {})
    return deepcopy(document)


def read_srw_config(path: str | Path) -> dict[str, Any]:
    """Load a trusted harness asset; public manifests use the strict parser."""
    return srw_config_fragment(
        yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    )
