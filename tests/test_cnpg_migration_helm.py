"""Render assertions for the CloudNativePG migration path (Phase 4).

`engine: migrating` renders BOTH workloads and bootstraps the Cluster by logical
import from the legacy Service. `engine: cnpg` drops the StatefulSet and flips
the host helpers to the `-rw` Service. Both still default to `statefulset`, so
this ships inert.

Spec:  knowledge-base/knowledge/superpowers/specs/2026-08-21-cnpg-data-tier-ha-design.md
Plan:  knowledge-base/knowledge/superpowers/plans/2026-08-22-cnpg-migrations.md
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"

RELEASE = "cnpg-test"
CHART_NAME = yaml.safe_load((CHART / "Chart.yaml").read_text())["name"]
FULLNAME = f"{RELEASE}-{CHART_NAME}"

# values key -> cluster/component name
DATABASES = {
    "postgres": "postgres",
    "vector": "pgvector",
    "audit": "auditdb",
    "gitea": "giteadb",
    "keycloak": "keycloakdb",
}

CLUSTER = "templates/databases/cnpg-cluster.yaml"
OPERAND = "ghcr.io/knaeckebrothero/superhuman-remote-worker-postgres:16.15"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Helm is not installed"
)


def _template_command(*settings: str, show_only: str | None = None) -> list[str]:
    command = [
        "helm",
        "template",
        RELEASE,
        str(CHART),
        "-f",
        str(CHART / "ci/test-values.yaml"),
    ]
    if show_only:
        command.extend(["--show-only", show_only])
    for setting in settings:
        command.extend(["--set", setting])
    return command


def _render(*settings: str, show_only: str | None = None) -> list[dict]:
    rendered = subprocess.run(
        _template_command(*settings, show_only=show_only),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _kinds(documents: list[dict], kind: str) -> list[dict]:
    return [d for d in documents if d.get("kind") == kind]


def _all_cnpg() -> list[str]:
    return [f"databases.{key}.engine=cnpg" for key in DATABASES]


def _clusters(*settings: str) -> dict[str, dict]:
    return {
        c["metadata"]["name"]: c
        for c in _kinds(_render(*settings, show_only=CLUSTER), "Cluster")
    }


def _configmap(*settings: str) -> dict:
    documents = _render(*settings, show_only="templates/configmap.yaml")
    return _kinds(documents, "ConfigMap")[0]["data"]


def test_every_cluster_defaults_to_the_operand_image():
    for cluster in _clusters(*_all_cnpg()).values():
        assert cluster["spec"]["imageName"] == OPERAND


def test_operand_tag_is_a_real_version_not_a_commit_sha():
    """imageName is part of the Cluster spec, so changing it rolls every
    Postgres cluster. A sha-* tag would do that on every push."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for key in DATABASES:
        tag = values["databases"][key]["cnpgImage"].rsplit(":", 1)[1]
        assert re.fullmatch(r"\d+\.\d+(-\d+)?", tag), f"{key}: {tag!r}"


def test_operand_image_is_not_wired_into_the_per_commit_tag_job():
    workflow = (ROOT / ".github/workflows/develop.yml").read_text()
    assert "cnpgImage" not in workflow
