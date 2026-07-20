"""Helm gate for ``canvas.sharedBrowser``."""

import json
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]


def test_shared_browser_helm_gate_is_boolean_and_default_off():
    values = yaml.safe_load((REPO / "helm/values.yaml").read_text())
    assert values["canvas"]["sharedBrowser"]["enabled"] is False

    schema = json.loads((REPO / "helm/values.schema.json").read_text())
    shared = schema["properties"]["canvas"]["properties"]["sharedBrowser"]
    assert shared["properties"]["enabled"]["type"] == "boolean"


def test_shared_browser_env_reaches_orchestrator():
    configmap = (REPO / "helm/templates/configmap.yaml").read_text()
    assert "CANVAS_SHARED_BROWSER_ENABLED" in configmap
    assert ".Values.canvas.sharedBrowser.enabled" in configmap

    deployment = (REPO / "helm/templates/orchestrator/deployment.yaml").read_text()
    assert "CANVAS_SHARED_BROWSER_ENABLED" in deployment


def test_dev_profile_enables_shared_browser():
    experimental = yaml.safe_load(
        (REPO / "deployment/values-experimental.yaml").read_text()
    )
    assert experimental["canvas"]["sharedBrowser"]["enabled"] is True
