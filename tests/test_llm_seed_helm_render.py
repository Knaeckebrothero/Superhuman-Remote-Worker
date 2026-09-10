"""Render-level contracts for Helm's LLM seed payload.

The seeder accepts both the preferred ``capabilities`` array and the legacy
singular ``capability`` shorthand.  These tests render the ConfigMap consumed
by the real seed Job so a template that silently drops either spelling cannot
pass by testing only Python-side parsing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.schemas.provider_catalog import VALID_DEFAULT_MODEL_KINDS

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"
SEED_CONFIGMAP = "templates/orchestrator/llm-seed-configmap.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Helm is not installed"
)


def _render_seed(tmp_path: Path, seed: dict) -> subprocess.CompletedProcess:
    values = tmp_path / "llm-seed-values.yaml"
    values.write_text(
        yaml.safe_dump({"llm": {"seed": {"enabled": True, **seed}}}),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            "helm",
            "template",
            "llm-seed-test",
            str(CHART),
            "-f",
            str(CHART / "ci/test-values.yaml"),
            "-f",
            str(values),
            "--show-only",
            SEED_CONFIGMAP,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _render_seed_payload(tmp_path: Path, seed: dict) -> dict:
    result = _render_seed(tmp_path, seed)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"

    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    assert len(documents) == 1
    return yaml.safe_load(documents[0]["data"]["llm.yaml"])


def test_endpoint_models_render_e2e_capabilities_and_legacy_fallback(
    tmp_path: Path,
) -> None:
    payload = _render_seed_payload(
        tmp_path,
        {
            "systemEndpoints": [
                {
                    "label": "E2E deterministic provider",
                    "baseUrl": "http://e2e-provider:8000/v1",
                    "models": [
                        {
                            "id": "e2e-chat",
                            "displayName": "E2E Chat",
                            "capabilities": ["chat", "auxiliary"],
                            "multimodal": True,
                        },
                        {
                            "id": "e2e-embedding",
                            "displayName": "E2E Embedding",
                            "capability": "embedding",
                        },
                    ],
                }
            ]
        },
    )

    models = payload["systemEndpoints"][0]["models"]
    assert models[0]["id"] == "e2e-chat"
    assert models[0]["capabilities"] == ["chat", "auxiliary"]
    assert models[0]["multimodal"] is True
    assert "capability" not in models[0]
    assert models[1]["id"] == "e2e-embedding"
    assert models[1]["capability"] == "embedding"
    assert "capabilities" not in models[1]


def test_system_models_render_capability_arrays(tmp_path: Path) -> None:
    payload = _render_seed_payload(
        tmp_path,
        {
            "systemModels": [
                {
                    "provider": "fixture",
                    "id": "fixture-chat",
                    "capabilities": ["chat", "auxiliary"],
                    "multimodal": False,
                }
            ]
        },
    )

    model = payload["systemModels"][0]
    assert model["capabilities"] == ["chat", "auxiliary"]
    assert model["multimodal"] is False
    assert "capability" not in model


def test_defaults_render_as_kind_to_model_map(tmp_path: Path) -> None:
    payload = _render_seed_payload(
        tmp_path,
        {
            "defaults": {
                "chat": "e2e-chat",
                "embedding": "e2e-embedding",
                "search_fallback": "searxng",
                "vision": "",  # empty = not declared; must not render
            }
        },
    )

    assert payload["defaults"] == {
        "chat": "e2e-chat",
        "embedding": "e2e-embedding",
        "search_fallback": "searxng",
    }


def test_defaults_default_to_an_empty_section(tmp_path: Path) -> None:
    payload = _render_seed_payload(tmp_path, {"systemModels": []})
    # `defaults: {}` renders a bare key; the seeder treats None as "nothing".
    assert payload.get("defaults") in (None, {})


def test_defaults_unknown_kind_fails_the_render(tmp_path: Path) -> None:
    result = _render_seed(tmp_path, {"defaults": {"chatt": "e2e-chat"}})
    assert result.returncode != 0
    assert "llm.seed.defaults: unknown kind" in result.stderr
    assert "chatt" in result.stderr


def test_chart_kind_list_matches_admin_schema() -> None:
    # The template carries its own copy of the kind list so a typo fails at
    # render time; keep it identical to what Admin → Models → Defaults accepts.
    template = (CHART / SEED_CONFIGMAP).read_text(encoding="utf-8")
    match = re.search(r'\$validKinds := list ((?:"[a-z_]+"\s*)+)', template)
    assert match, "kind list not found in the seed ConfigMap template"
    chart_kinds = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert chart_kinds == VALID_DEFAULT_MODEL_KINDS
