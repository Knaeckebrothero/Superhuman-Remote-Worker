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
OPERAND = "ghcr.io/cloudnative-pg/postgresql:16.15"

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


def test_operand_is_upstream_not_a_custom_build():
    """CloudNativePG's operand already bundles pgvector, uuid-ossp and
    btree_gist. Building our own would mean owning a PostgreSQL image forever
    to gain nothing -- the versions are identical."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())["databases"]
    for key in DATABASES:
        assert values[key]["cnpgImage"].startswith("ghcr.io/cloudnative-pg/postgresql:")


def test_operand_tag_is_pinned_not_floating():
    """A moving tag would roll every Postgres cluster whenever upstream
    publishes, because imageName is part of the Cluster spec."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())["databases"]
    for key in DATABASES:
        tag = values[key]["cnpgImage"].rsplit(":", 1)[1]
        assert re.fullmatch(r"\d+\.\d+", tag), (
            f"{key}: {tag!r} is not a pinned patch version"
        )


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


# --- the -rw flip ----------------------------------------------------------


def test_hosts_are_unchanged_on_the_statefulset_engine():
    assert _configmap()["POSTGRES_HOST"] == f"{FULLNAME}-postgres"


@pytest.mark.parametrize(
    "key,variable,component",
    [
        ("postgres", "POSTGRES_HOST", "postgres"),
        ("vector", "VECTOR_POSTGRES_HOST", "pgvector"),
        ("audit", "AUDIT_POSTGRES_HOST", "auditdb"),
    ],
)
def test_hosts_still_point_at_the_legacy_service_while_migrating(
    key, variable, component
):
    """The import runs against the legacy Service and consumers keep writing
    to it. Repointing here would cut over before the data has arrived."""
    assert (
        _configmap(f"databases.{key}.engine=migrating")[variable]
        == f"{FULLNAME}-{component}"
    )


@pytest.mark.parametrize(
    "key,variable,component",
    [
        ("postgres", "POSTGRES_HOST", "postgres"),
        ("vector", "VECTOR_POSTGRES_HOST", "pgvector"),
        ("audit", "AUDIT_POSTGRES_HOST", "auditdb"),
    ],
)
def test_cnpg_engine_points_at_the_read_write_service(key, variable, component):
    assert (
        _configmap(f"databases.{key}.engine=cnpg")[variable]
        == f"{FULLNAME}-{component}-rw"
    )


def test_no_template_hardcodes_a_database_service_name():
    """The -rw flip only reaches a consumer that goes through the helper. A
    template that spells the Service name itself keeps pointing at the retired
    StatefulSet after cutover, silently -- the canvas gateway and the Gitea
    init container are exactly the kind of consumer that could.

    Checked at the source, because enabling every consumer for a render test
    means satisfying each one's unrelated schema prerequisites."""
    helpers = {
        "postgres": "srw.postgresHost",
        "pgvector": "srw.vectorPostgresHost",
        "auditdb": "srw.auditPostgresHost",
        "giteadb": "srw.giteaDbHost",
        "keycloakdb": "srw.keycloakDbJdbcUrl",
    }
    offenders = []
    for template in (CHART / "templates").rglob("*.yaml"):
        if template.name.startswith("cnpg-") or template.name.startswith("postgres"):
            continue  # the database templates themselves legitimately name it
        body = template.read_text()
        for component in helpers:
            for line in body.splitlines():
                if f'"srw.fullname" . }}}}-{component}' not in line:
                    continue
                # `name:` is the resource's own name, which legitimately
                # matches the Service. Anything else is a reference to it.
                if line.strip().startswith("name:"):
                    continue
                offenders.append(f"{template.relative_to(CHART)} spells -{component}")
    assert not offenders, "; ".join(offenders)


def test_external_mode_is_untouched_by_the_engine():
    """internal=false means someone else's database. The engine is irrelevant
    and must not append anything to their hostname."""
    config = _configmap(
        "databases.postgres.internal=false",
        "databases.postgres.externalHost=pg.example.com",
        "databases.postgres.engine=cnpg",
    )
    assert config["POSTGRES_HOST"] == "pg.example.com"


def test_gitea_host_flips_with_its_port():
    documents = _render("databases.gitea.engine=cnpg")
    gitea = [
        d
        for d in _kinds(documents, "StatefulSet")
        if d["metadata"]["name"] == f"{FULLNAME}-gitea"
    ][0]
    env = {
        e["name"]: e.get("value")
        for e in gitea["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    # Gitea takes host:port as one string.
    assert env["GITEA__database__HOST"] == f"{FULLNAME}-giteadb-rw:5432"


def test_keycloak_jdbc_url_flips():
    documents = _render("databases.keycloak.engine=cnpg")
    keycloak = [
        d
        for d in _kinds(documents, "Deployment")  # Keycloak is a Deployment
        if d["metadata"]["name"].endswith("-keycloak")
    ][0]
    env = {
        e["name"]: e.get("value")
        for e in keycloak["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["KC_DB_URL"] == f"jdbc:postgresql://{FULLNAME}-keycloakdb-rw:5432/keycloak"
    )
