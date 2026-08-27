"""Helm rollout contract for pinned Kubernetes authority adoption."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"


def _render(*extra: str, release_name: str = "pinned-authority-proof") -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    output = subprocess.run(
        [
            "helm",
            "template",
            release_name,
            str(CHART),
            "--namespace",
            "agents-current",
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


def _configmap(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and "PINNED_LEGACY_AGENT_NAMESPACES" in (document.get("data") or {})
    )


def test_active_namespace_is_implicit_and_legacy_search_defaults_empty() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["agent"]["pinnedLegacyNamespaces"] == []

    documents = _render()
    assert _configmap(documents)["data"]["WORKSPACE_NAMESPACE"] == "agents-current"
    assert _configmap(documents)["data"]["PINNED_LEGACY_AGENT_NAMESPACES"] == ""

    current_role = next(
        document
        for document in documents
        if document.get("kind") == "Role"
        and document.get("metadata", {}).get("name", "").endswith("-orchestrator")
    )
    pvc_rule = next(
        rule
        for rule in current_role["rules"]
        if rule.get("resources") == ["persistentvolumeclaims"]
    )
    assert "patch" in pvc_rule["verbs"]


def test_explicit_legacy_namespace_renders_config_and_bounded_authority() -> None:
    documents = _render("--set", "agent.pinnedLegacyNamespaces[0]=agents-old")
    assert (
        _configmap(documents)["data"]["PINNED_LEGACY_AGENT_NAMESPACES"] == "agents-old"
    )

    role = next(
        document
        for document in documents
        if document.get("kind") == "Role"
        and document.get("metadata", {}).get("namespace") == "agents-old"
        and document.get("metadata", {})
        .get("name", "")
        .endswith("-pinned-legacy-authority")
    )
    binding = next(
        document
        for document in documents
        if document.get("kind") == "RoleBinding"
        and document.get("metadata", {}).get("namespace") == "agents-old"
        and document.get("metadata", {})
        .get("name", "")
        .endswith("-pinned-legacy-authority")
    )
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": binding["subjects"][0]["name"],
            "namespace": "agents-current",
        }
    ]
    assert {
        resource: set(rule["verbs"])
        for rule in role["rules"]
        for resource in rule["resources"]
    } == {
        "pods": {"create", "delete", "get", "patch"},
        "persistentvolumeclaims": {"create", "delete", "get", "patch"},
        "pods/finalizers": {"update", "patch"},
        "persistentvolumeclaims/finalizers": {"update", "patch"},
        "services": {"get", "create", "delete", "patch"},
        "ingresses": {"get", "create", "delete", "patch"},
    }


def test_long_release_name_preserves_dns_safe_legacy_rbac_identity() -> None:
    documents = _render(
        "--set",
        "agent.pinnedLegacyNamespaces[0]=agents-old",
        release_name="p" * 53,
    )
    role = next(
        document
        for document in documents
        if document.get("kind") == "Role"
        and document.get("metadata", {}).get("namespace") == "agents-old"
    )
    binding = next(
        document
        for document in documents
        if document.get("kind") == "RoleBinding"
        and document.get("metadata", {}).get("namespace") == "agents-old"
    )
    authority_name = role["metadata"]["name"]
    assert len(authority_name) <= 63
    assert authority_name.endswith("-pinned-legacy-authority")
    assert binding["metadata"]["name"] == authority_name
    assert binding["roleRef"]["name"] == authority_name
    assert len(binding["subjects"][0]["name"]) <= 63
