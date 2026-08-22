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


# --- postgresql.parameters -------------------------------------------------


def test_parameters_passthrough_merges_with_max_connections():
    cluster = _clusters(
        "databases.postgres.engine=cnpg",
        "databases.postgres.parameters.maintenance_work_mem=2GB",
    )[f"{FULLNAME}-postgres"]
    parameters = cluster["spec"]["postgresql"]["parameters"]
    assert parameters["max_connections"] == "100"
    assert parameters["maintenance_work_mem"] == "2GB"


def test_all_parameters_render_as_strings():
    """CNPG's parameters map is map[string]string. An unquoted integer makes
    the whole Cluster fail admission, which reads as a template bug."""
    cluster = _clusters(
        "databases.vector.engine=cnpg",
        "databases.vector.parameters.max_parallel_maintenance_workers=4",
    )[f"{FULLNAME}-pgvector"]
    for value in cluster["spec"]["postgresql"]["parameters"].values():
        assert isinstance(value, str)


def test_vector_ships_index_build_parameters():
    """A logical restore rebuilds all 3.9 GB of this database's HNSW indexes
    from scratch. The legacy server did that at maintenance_work_mem 64MB."""
    parameters = _clusters("databases.vector.engine=cnpg")[f"{FULLNAME}-pgvector"][
        "spec"
    ]["postgresql"]["parameters"]
    assert parameters["maintenance_work_mem"] == "2GB"
    assert parameters["max_parallel_maintenance_workers"] == "4"


def test_vector_has_headroom_for_the_index_build():
    """maintenance_work_mem above the pod's memory limit is an OOM kill, not a
    slow build. The legacy limit was 1Gi against ~169Mi steady-state."""
    resources = _clusters("databases.vector.engine=cnpg")[f"{FULLNAME}-pgvector"][
        "spec"
    ]["resources"]
    assert resources["limits"]["memory"] == "8Gi"


def test_other_databases_ship_no_extra_parameters():
    for key in ("postgres", "audit", "gitea", "keycloak"):
        component = DATABASES[key]
        parameters = _clusters(f"databases.{key}.engine=cnpg")[
            f"{FULLNAME}-{component}"
        ]["spec"]["postgresql"]["parameters"]
        assert set(parameters) == {"max_connections"}, key


def test_legacy_statefulset_resources_are_untouched():
    """`resources` sizes the legacy pod too. Raising it there restarts a live
    database to reserve memory it never uses -- hence the separate key."""
    statefulsets = {d["metadata"]["name"]: d for d in _kinds(_render(), "StatefulSet")}
    container = statefulsets[f"{FULLNAME}-pgvector"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert container["resources"]["limits"]["memory"] == "1Gi"


def test_no_imagevolume_extension_against_a_mismatched_operand():
    """The only official pgvector extension images are PostgreSQL 18. The
    operand is 16 and already carries pgvector, so mounting one would be a
    major-version mismatch."""
    for cluster in _clusters(*_all_cnpg()).values():
        assert "extensions" not in cluster["spec"]["postgresql"], cluster["metadata"][
            "name"
        ]


# --- the import ------------------------------------------------------------

IMPORT_SOURCES = {
    "postgres": ("srw", "srw", "POSTGRES_PASSWORD"),
    "vector": ("srw_vector", "srw", "VECTOR_POSTGRES_PASSWORD"),
    "audit": ("srw_audit", "srw", "AUDIT_POSTGRES_PASSWORD"),
    "gitea": ("gitea", "gitea", "GITEA_DB_PASSWORD"),
    "keycloak": ("keycloak", "keycloak", "KC_DB_PASSWORD"),
}


def test_cnpg_engine_bootstraps_empty():
    """A fresh install has nothing to import from. Going straight to cnpg on
    an EXISTING database is how you get an empty one -- pass through
    migrating."""
    cluster = _clusters("databases.postgres.engine=cnpg")[f"{FULLNAME}-postgres"]
    assert "import" not in cluster["spec"]["bootstrap"]["initdb"]
    assert "externalClusters" not in cluster["spec"]


@pytest.mark.parametrize("key", sorted(IMPORT_SOURCES))
def test_migrating_imports_each_database_from_its_own_legacy_service(key):
    database, owner, password_key = IMPORT_SOURCES[key]
    component = DATABASES[key]
    cluster = _clusters(f"databases.{key}.engine=migrating")[f"{FULLNAME}-{component}"]

    import_spec = cluster["spec"]["bootstrap"]["initdb"]["import"]
    assert import_spec["type"] == "microservice"
    assert import_spec["databases"] == [database]

    source = [
        c
        for c in cluster["spec"]["externalClusters"]
        if c["name"] == import_spec["source"]["externalCluster"]
    ][0]
    assert source["connectionParameters"]["host"] == f"{FULLNAME}-{component}"
    assert source["connectionParameters"]["dbname"] == database
    assert source["connectionParameters"]["user"] == owner
    assert source["password"] == {"name": FULLNAME, "key": password_key}


def test_import_source_is_the_legacy_service_not_the_cluster_itself():
    """The Cluster and the legacy Service share a name. Pointing the import at
    `-rw` would have it bootstrap from itself."""
    cluster = _clusters("databases.postgres.engine=migrating")[f"{FULLNAME}-postgres"]
    host = cluster["spec"]["externalClusters"][0]["connectionParameters"]["host"]
    assert not host.endswith("-rw")


def test_import_parallelises_the_index_rebuild_for_vector():
    """Index creation happens in pg_restore's post-data section."""
    cluster = _clusters("databases.vector.engine=migrating")[f"{FULLNAME}-pgvector"]
    options = cluster["spec"]["bootstrap"]["initdb"]["import"][
        "pgRestorePostdataOptions"
    ]
    assert "--jobs=4" in options


def test_other_databases_do_not_parallelise_by_default():
    cluster = _clusters("databases.gitea.engine=migrating")[f"{FULLNAME}-giteadb"]
    assert (
        "pgRestorePostdataOptions"
        not in cluster["spec"]["bootstrap"]["initdb"]["import"]
    )


def test_migrating_still_renders_the_legacy_statefulset():
    """The import reads from the legacy Service, so it must still exist."""
    documents = _render("databases.postgres.engine=migrating")
    assert f"{FULLNAME}-postgres" in {
        d["metadata"]["name"] for d in _kinds(documents, "StatefulSet")
    }
