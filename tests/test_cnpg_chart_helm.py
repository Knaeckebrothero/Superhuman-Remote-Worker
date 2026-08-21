"""Render assertions for the CloudNativePG data-tier templates.

Phase 2 of the CNPG migration ships **inert**: every database's ``engine``
defaults to ``statefulset``, so a default render is byte-identical to the
StatefulSet-only chart. These tests pin both halves — that the switch does
nothing until flipped, and that flipping it produces a correct ``Cluster``.

Spec:  knowledge-base/knowledge/superpowers/specs/2026-08-21-cnpg-data-tier-ha-design.md
Plan:  knowledge-base/knowledge/superpowers/plans/2026-08-21-cnpg-chart-foundation.md
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"

RELEASE = "cnpg-test"
CHART_NAME = yaml.safe_load((CHART / "Chart.yaml").read_text())["name"]
# `srw.fullname` is "<release>-<chart name>" absent an override.
FULLNAME = f"{RELEASE}-{CHART_NAME}"

# values key -> (cluster/component name, database, owner)
DATABASES = {
    "postgres": ("postgres", "srw", "srw"),
    "vector": ("pgvector", "srw_vector", "srw"),
    "audit": ("auditdb", "srw_audit", "srw"),
    "gitea": ("giteadb", "gitea", "gitea"),
    "keycloak": ("keycloakdb", "keycloak", "keycloak"),
}

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


def _cluster(*settings: str) -> list[dict]:
    return _kinds(
        _render(*settings, show_only="templates/databases/cnpg-cluster.yaml"), "Cluster"
    )


def _all_cnpg() -> list[str]:
    return [f"databases.{key}.engine=cnpg" for key in DATABASES]


def test_default_release_renders_no_cluster_at_all():
    """Phase 2 must be inert: engine defaults to statefulset everywhere."""
    assert _kinds(_render(), "Cluster") == []


def test_default_release_still_renders_the_five_statefulsets():
    names = {d["metadata"]["name"] for d in _kinds(_render(), "StatefulSet")}
    for component, _, _ in DATABASES.values():
        assert f"{FULLNAME}-{component}" in names


@pytest.mark.parametrize("key", sorted(DATABASES))
def test_engine_cnpg_swaps_statefulset_for_cluster(key):
    component, _, _ = DATABASES[key]
    documents = _render(f"databases.{key}.engine=cnpg")
    statefulsets = {d["metadata"]["name"] for d in _kinds(documents, "StatefulSet")}
    clusters = {d["metadata"]["name"] for d in _kinds(documents, "Cluster")}
    assert f"{FULLNAME}-{component}" not in statefulsets
    assert f"{FULLNAME}-{component}" in clusters


@pytest.mark.parametrize("key", sorted(DATABASES))
def test_engine_migrating_renders_both(key):
    """Phase 4 imports from the live Service, so both must exist at once."""
    component, _, _ = DATABASES[key]
    documents = _render(f"databases.{key}.engine=migrating")
    assert f"{FULLNAME}-{component}" in {
        d["metadata"]["name"] for d in _kinds(documents, "StatefulSet")
    }
    assert f"{FULLNAME}-{component}" in {
        d["metadata"]["name"] for d in _kinds(documents, "Cluster")
    }


@pytest.mark.parametrize("key", sorted(DATABASES))
def test_cluster_carries_database_owner_and_inherited_labels(key):
    component, database, owner = DATABASES[key]
    cluster = [
        c
        for c in _cluster(f"databases.{key}.engine=cnpg")
        if c["metadata"]["name"] == f"{FULLNAME}-{component}"
    ][0]
    initdb = cluster["spec"]["bootstrap"]["initdb"]
    assert initdb["database"] == database
    assert initdb["owner"] == owner
    labels = cluster["spec"]["inheritedMetadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == component
    assert labels["app.kubernetes.io/instance"] == RELEASE


def test_cluster_name_equals_todays_service_name():
    """Phase 4's helper flip is then a pure "-rw" suffix, nothing else."""
    statefulset_services = {d["metadata"]["name"] for d in _kinds(_render(), "Service")}
    for component, _, _ in DATABASES.values():
        assert f"{FULLNAME}-{component}" in statefulset_services
    cluster_names = {c["metadata"]["name"] for c in _cluster(*_all_cnpg())}
    assert cluster_names == {f"{FULLNAME}-{c}" for c, _, _ in DATABASES.values()}


def test_profile_single_is_one_instance_everywhere():
    clusters = _cluster(*_all_cnpg())
    assert {c["spec"]["instances"] for c in clusters} == {1}


def test_profile_ha_is_two_instances_everywhere():
    clusters = _cluster("databases.profile=ha", *_all_cnpg())
    assert {c["spec"]["instances"] for c in clusters} == {2}


def test_per_database_instances_overrides_the_profile():
    clusters = _cluster(
        "databases.profile=ha", "databases.postgres.instances=3", *_all_cnpg()
    )
    by_name = {c["metadata"]["name"]: c["spec"]["instances"] for c in clusters}
    assert by_name[f"{FULLNAME}-postgres"] == 3
    assert by_name[f"{FULLNAME}-auditdb"] == 2


def test_anti_affinity_derives_from_resolved_count_not_profile_name():
    """A generated file says instances: 3 without mentioning profile. It must
    still get hard anti-affinity, or replicas silently stack on one node."""
    cluster = _cluster(
        "databases.postgres.engine=cnpg", "databases.postgres.instances=3"
    )[0]
    affinity = cluster["spec"]["affinity"]
    assert affinity["enablePodAntiAffinity"] is True
    assert affinity["podAntiAffinityType"] == "required"
    assert affinity["topologyKey"] == "kubernetes.io/hostname"


def test_single_instance_uses_preferred_anti_affinity():
    cluster = _cluster("databases.postgres.engine=cnpg")[0]
    assert cluster["spec"]["affinity"]["podAntiAffinityType"] == "preferred"


def test_topology_key_is_overridable_for_zonal_hyperscaler_volumes():
    cluster = _cluster(
        "databases.postgres.engine=cnpg",
        "databases.postgres.instances=3",
        "databases.ha.topologyKey=topology.kubernetes.io/zone",
    )[0]
    assert cluster["spec"]["affinity"]["topologyKey"] == "topology.kubernetes.io/zone"


def test_only_the_vector_cluster_declares_pgvector():
    clusters = _cluster(*_all_cnpg())
    with_ext = {
        c["metadata"]["name"]: [
            e["name"] for e in c["spec"]["postgresql"].get("extensions", [])
        ]
        for c in clusters
    }
    assert with_ext[f"{FULLNAME}-pgvector"] == ["pgvector"]
    for name, extensions in with_ext.items():
        if name != f"{FULLNAME}-pgvector":
            assert extensions == [], f"{name} should declare no extensions"


def test_storage_and_connection_defaults():
    cluster = _cluster("databases.postgres.engine=cnpg")[0]
    assert cluster["spec"]["storage"]["size"] == "16Gi"
    postgresql = cluster["spec"]["postgresql"]
    assert postgresql is not None, "postgresql must never render as an empty mapping"
    assert postgresql["parameters"]["max_connections"] == "100"
    # Async by default: a sick standby must not stall every write on the primary.
    assert "synchronous" not in postgresql


def test_invalid_engine_is_rejected():
    result = subprocess.run(
        _template_command("databases.postgres.engine=bogus"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "engine" in result.stderr or "bogus" in result.stderr


def test_operand_image_is_omitted_rather_than_inheriting_the_statefulset_tag():
    """CNPG parses imageName's tag as a PostgreSQL version and rejects
    `pgvector/pgvector:pg15` with "invalid version tag" -- verified against
    the live CRD. An omitted imageName lets the operator pick a valid one."""
    for cluster in _cluster(*_all_cnpg()):
        assert "imageName" not in cluster["spec"], cluster["metadata"]["name"]


def test_operand_image_is_used_when_pinned():
    cluster = _cluster(
        "databases.postgres.engine=cnpg",
        "databases.postgres.cnpgImage=ghcr.io/cloudnative-pg/postgresql:16.10",
    )[0]
    assert cluster["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:16.10"
