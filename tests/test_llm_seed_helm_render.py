"""Render-level contracts for Helm's LLM seed payload.

The seeder accepts both the preferred ``capabilities`` array and the legacy
singular ``capability`` shorthand.  These tests render the ConfigMap consumed
by the real seed Job so a template that silently drops either spelling cannot
pass by testing only Python-side parsing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"
SEED_CONFIGMAP = "templates/orchestrator/llm-seed-configmap.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Helm is not installed"
)


def _render_seed_payload(tmp_path: Path, seed: dict) -> dict:
    values = tmp_path / "llm-seed-values.yaml"
    values.write_text(
        yaml.safe_dump({"llm": {"seed": {"enabled": True, **seed}}}),
        encoding="utf-8",
    )
    result = subprocess.run(
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
