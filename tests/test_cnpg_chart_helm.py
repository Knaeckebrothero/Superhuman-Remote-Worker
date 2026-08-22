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


def test_pgvector_comes_from_the_operand_image_not_an_image_volume():
    """Phase 2 declared pgvector as a CNPG ImageVolume extension. Phase 4
    replaced that with an operand image carrying pgvector, because the only
    official extension images are PostgreSQL 18 while the operand is 16 --
    mounting one would be a major-version mismatch. Still only the vector
    database needs it at all: the app DB creates btree_gist and uuid-ossp."""
    for cluster in _cluster(*_all_cnpg()):
        assert "extensions" not in cluster["spec"]["postgresql"], cluster["metadata"][
            "name"
        ]


def test_operand_image_never_inherits_the_statefulset_tag():
    """CNPG parses imageName's tag as a PostgreSQL version and rejects
    `pgvector/pgvector:pg15` with "invalid version tag" -- verified against the
    live CRD. Phase 2 omitted the field; Phase 4 pins a purpose-built operand.
    Either way it must never be the StatefulSet's own image."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())["databases"]
    legacy = {values[key]["image"] for key in DATABASES}
    for cluster in _cluster(*_all_cnpg()):
        assert cluster["spec"].get("imageName") not in legacy, cluster["metadata"][
            "name"
        ]


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


def test_operand_image_is_used_when_pinned():
    cluster = _cluster(
        "databases.postgres.engine=cnpg",
        "databases.postgres.cnpgImage=ghcr.io/cloudnative-pg/postgresql:16.10",
    )[0]
    assert cluster["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:16.10"


# --- credential projection -------------------------------------------------
#
# The chart has three secret modes and the projection needs a different
# mechanism in each. `helm/ci/test-values.yaml` runs the External Secrets mode,
# which is also how dev is configured.

CREDENTIALS = "templates/databases/cnpg-credentials.yaml"
CREATE_MODE = ("externalSecrets.enabled=false", "secrets.create=true")


def _credentials(*settings: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in _render(*settings, show_only=CREDENTIALS)}


def _render_fails(*settings: str) -> str:
    result = subprocess.run(
        _template_command(*settings), capture_output=True, text=True
    )
    assert result.returncode != 0, "expected the render to fail"
    return result.stderr


def test_no_credential_object_when_inert():
    result = subprocess.run(
        _template_command(show_only=CREDENTIALS), capture_output=True, text=True
    )
    # Helm errors when --show-only matches nothing rendered; either way, nothing.
    assert result.returncode != 0 or "kind:" not in result.stdout


@pytest.mark.parametrize("key", sorted(DATABASES))
def test_external_secrets_mode_projects_into_a_basic_auth_shape(key):
    component, _, owner = DATABASES[key]
    external = _credentials(f"databases.{key}.engine=cnpg")[
        f"{FULLNAME}-{component}-app"
    ]
    assert external["kind"] == "ExternalSecret"
    template = external["spec"]["target"]["template"]
    assert template["type"] == "kubernetes.io/basic-auth"
    assert template["data"]["username"] == owner
    # Left for the ESO controller to substitute, not rendered away by Helm.
    assert template["data"]["password"] == "{{ .password }}"


@pytest.mark.parametrize(
    "key,password_key",
    [
        ("postgres", "POSTGRES_PASSWORD"),
        ("vector", "VECTOR_POSTGRES_PASSWORD"),
        ("audit", "AUDIT_POSTGRES_PASSWORD"),
        ("gitea", "GITEA_DB_PASSWORD"),
        ("keycloak", "KC_DB_PASSWORD"),
    ],
)
def test_external_secrets_mode_reads_the_same_vault_property_consumers_do(
    key, password_key
):
    component, _, _ = DATABASES[key]
    external = _credentials(f"databases.{key}.engine=cnpg")[
        f"{FULLNAME}-{component}-app"
    ]
    entry = external["spec"]["data"][0]
    assert entry["secretKey"] == "password"
    assert entry["remoteRef"]["property"] == password_key


def test_create_mode_projects_the_operators_own_value():
    secret = _credentials(
        *CREATE_MODE,
        "secrets.values.POSTGRES_PASSWORD=s3cret",
        "databases.postgres.engine=cnpg",
    )[f"{FULLNAME}-postgres-app"]
    assert secret["kind"] == "Secret"
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["stringData"] == {"username": "srw", "password": "s3cret"}


def test_create_mode_refuses_to_invent_a_password():
    """An empty password bootstraps a database nothing can log into, while
    looking configured. Fail at render instead."""
    stderr = _render_fails(*CREATE_MODE, "databases.postgres.engine=cnpg")
    assert "secrets.values.POSTGRES_PASSWORD is unset" in stderr


def test_create_mode_rejects_a_username_that_disagrees_with_the_owner():
    """CNPG creates the owner role. A consumer authenticating as a different
    role would own nothing -- and would find out at the first write."""
    stderr = _render_fails(
        *CREATE_MODE,
        "secrets.values.POSTGRES_PASSWORD=s3cret",
        "secrets.values.POSTGRES_USER=someoneelse",
        "databases.postgres.engine=cnpg",
    )
    assert "would own nothing" in stderr


def test_existing_secret_mode_names_the_secret_the_operator_must_create():
    stderr = _render_fails(
        "externalSecrets.enabled=false",
        "secrets.create=false",
        "secrets.existingSecret=my-secret",
        "databases.postgres.engine=cnpg",
    )
    assert f"{FULLNAME}-postgres-app" in stderr
    assert "kubernetes.io/basic-auth" in stderr


def test_credential_name_matches_what_the_cluster_references():
    cluster = _cluster("databases.postgres.engine=cnpg")[0]
    referenced = cluster["spec"]["bootstrap"]["initdb"]["secret"]["name"]
    assert referenced in _credentials("databases.postgres.engine=cnpg")


def test_every_rendered_cluster_gets_a_credential_object():
    clusters = {c["metadata"]["name"] for c in _cluster(*_all_cnpg())}
    credentials = set(_credentials(*_all_cnpg()))
    assert {f"{name}-app" for name in clusters} == credentials


# --- network policies ------------------------------------------------------
#
# There is no giteadb NetworkPolicy in the chart -- a pre-existing gap, not one
# this phase introduces. Nothing restricts that database today, so CNPG
# replication is not blocked there either; adding a policy would be a new
# restriction and belongs in its own change.
POLICIES = "templates/databases/network-policies.yaml"
POLICY_COMPONENTS = {
    "postgres": "postgres",
    "vector": "pgvector",
    "audit": "auditdb",
    "keycloak": "keycloakdb",
}


def _policies(*settings: str) -> dict[str, dict]:
    documents = _render(*settings, show_only=POLICIES)
    return {d["metadata"]["name"]: d for d in _kinds(documents, "NetworkPolicy")}


def _sources(policy: dict, kind: str) -> list[dict]:
    return [
        rule[kind]
        for entry in policy["spec"]["ingress"]
        for rule in entry.get("from", [])
        if kind in rule
    ]


@pytest.mark.parametrize("key,component", sorted(POLICY_COMPONENTS.items()))
def test_replication_between_instances_is_allowed(key, component):
    """Multi-instance CNPG streams WAL between its own pods on 5432. The
    current from-list names orchestrator/agent/etc but not the DB itself."""
    policy = _policies(f"databases.{key}.engine=cnpg", f"databases.{key}.instances=2")[
        f"{FULLNAME}-{component}"
    ]
    selectors = _sources(policy, "podSelector")
    assert any(
        s["matchLabels"].get("app.kubernetes.io/component") == component
        for s in selectors
    ), (
        "the database must accept connections from its own pods, or streaming replication is silently blocked"
    )


@pytest.mark.parametrize("key,component", sorted(POLICY_COMPONENTS.items()))
def test_operator_namespace_is_allowed_to_reach_instances(key, component):
    """If the operator cannot poll instance health it cannot fail over --
    which is the entire feature."""
    policy = _policies(f"databases.{key}.engine=cnpg")[f"{FULLNAME}-{component}"]
    namespaces = _sources(policy, "namespaceSelector")
    assert namespaces, "no namespaceSelector allows the CNPG operator in"
    assert any(
        n["matchLabels"].get("kubernetes.io/metadata.name") == "cnpg-system"
        for n in namespaces
    )


def test_operator_namespace_is_configurable():
    policy = _policies(
        "databases.postgres.engine=cnpg", "databases.operator.namespace=pg-ops"
    )[f"{FULLNAME}-postgres"]
    assert any(
        n["matchLabels"].get("kubernetes.io/metadata.name") == "pg-ops"
        for n in _sources(policy, "namespaceSelector")
    )


@pytest.mark.parametrize("key,component", sorted(POLICY_COMPONENTS.items()))
def test_statefulset_policy_is_unchanged_when_inert(key, component):
    policy = _policies()[f"{FULLNAME}-{component}"]
    components = [
        s["matchLabels"].get("app.kubernetes.io/component")
        for s in _sources(policy, "podSelector")
    ]
    assert component not in components
    assert _sources(policy, "namespaceSelector") == []


# --- disruption budgets ----------------------------------------------------

CNPG_PDB = "templates/databases/cnpg-pdb.yaml"


def test_no_database_pdb_at_a_single_instance():
    result = subprocess.run(
        _template_command("databases.postgres.engine=cnpg", show_only=CNPG_PDB),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 or "kind: PodDisruptionBudget" not in result.stdout


def test_database_pdb_appears_once_replicated():
    documents = _render(
        "databases.postgres.engine=cnpg",
        "databases.postgres.instances=2",
        show_only=CNPG_PDB,
    )
    pdb = _kinds(documents, "PodDisruptionBudget")[0]
    assert pdb["spec"]["minAvailable"] == 1
    assert (
        pdb["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"]
        == "postgres"
    )


def test_every_replicated_database_gets_a_pdb():
    documents = _render("databases.profile=ha", *_all_cnpg(), show_only=CNPG_PDB)
    names = {d["metadata"]["name"] for d in _kinds(documents, "PodDisruptionBudget")}
    assert names == {
        f"{FULLNAME}-{component}-pdb" for component, _, _ in DATABASES.values()
    }


def test_statefulset_engine_never_gets_a_database_pdb():
    """A CNPG PDB selects by component label, which the StatefulSet pod also
    carries -- so rendering one for an unmigrated database would freeze it."""
    result = subprocess.run(
        _template_command("databases.profile=ha", show_only=CNPG_PDB),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 or "kind: PodDisruptionBudget" not in result.stdout


# --- operator subchart -----------------------------------------------------


def test_chart_declares_the_operator_dependency_conditionally():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    dependency = [d for d in chart["dependencies"] if d["name"] == "cloudnative-pg"]
    assert len(dependency) == 1
    assert dependency[0]["condition"] == "databases.operator.install"
    assert dependency[0]["repository"] == "https://cloudnative-pg.github.io/charts"


def test_chart_lock_pins_the_operator():
    """CI runs `helm dependency build`, which resolves from the lock, not the
    range. An unpinned lock means a silently different operator per build."""
    lock = yaml.safe_load((CHART / "Chart.lock").read_text())
    pinned = {d["name"]: d["version"] for d in lock["dependencies"]}
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    declared = {d["name"]: d["version"] for d in chart["dependencies"]}
    assert pinned["cloudnative-pg"] == declared["cloudnative-pg"]


def test_operator_is_not_installed_by_default():
    """Phase 2 is inert: nothing renders a Cluster, so an operator would
    manage nothing -- and on a cluster that already runs one (dev runs 1.29.1
    cluster-wide) a second install fights it over cluster-scoped CRDs. Helm
    neither upgrades nor removes subchart CRDs, so that is a one-way door."""
    documents = _render()
    assert not [
        d for d in documents if "cloudnative-pg" in d["metadata"].get("name", "")
    ]


def test_operator_renders_when_asked_for():
    documents = _render("databases.operator.install=true")
    names = [d["metadata"].get("name", "") for d in documents]
    assert any("cloudnative-pg" in name for name in names)
