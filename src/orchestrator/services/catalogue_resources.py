"""Application-owned bundled configuration reads shared by catalogue domains."""

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml


def resolve_config_dir(application_file: str) -> Path:
    """Preserve the application's environment/source/container path precedence."""
    config_dir_env = os.environ.get("CONFIG_DIR")
    if config_dir_env:
        return Path(config_dir_env)
    candidates = [
        Path(application_file).resolve().parents[2] / "config",
        Path("/app/config"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def project_settings_subsection(parsed: dict[str, Any]) -> dict[str, Any]:
    """Project a parsed model_config_matrix to the legacy settings shape.

    Returns ``{family: settings_dict}`` (drops families without a settings
    block) so callers that pre-date the unified file see the same shape
    they used to read from ``settings_matrix.yaml``.
    """
    out: dict[str, Any] = {}
    for family, sections in (parsed or {}).items():
        if not isinstance(sections, dict):
            continue
        # Tolerate both legacy flat shape and unified subsection shape — the
        # unified loader downstream is the source of truth, but per-expert
        # files written before chunk 1 may still arrive flat in tests.
        if "settings" in sections and isinstance(sections["settings"], dict):
            out[family] = sections["settings"]
        elif {"prompts", "instructions"}.isdisjoint(sections.keys()):
            # Pure legacy settings-only block (no `settings:` wrapper) — keep
            # whatever scalar/dict children it carries.
            out[family] = sections
    return out


@dataclass
class CatalogueResources:
    """One matrix cache for all catalogues in an application.

    Composition supplies the effective config-directory reader because its
    source-location/container fallback belongs to application construction.
    The first matrix read remains cached for this app's lifetime, including
    an absent file, matching the existing catalogue contract.
    """

    config_dir: Callable[[], Path]
    _settings_matrix_cache: dict[str, Any] | None = field(default=None, init=False)

    def get_config_dir(self) -> Path:
        return self.config_dir()

    def load_settings_matrix(self, config_dir: Path) -> dict[str, Any]:
        """Load and cache the settings subsection of model_config_matrix.yaml."""
        if self._settings_matrix_cache is None:
            matrix_path = config_dir / "model_config_matrix.yaml"
            if matrix_path.exists():
                with open(matrix_path) as f:
                    parsed = yaml.safe_load(f) or {}
                self._settings_matrix_cache = project_settings_subsection(parsed)
            else:
                self._settings_matrix_cache = {}
        return self._settings_matrix_cache
