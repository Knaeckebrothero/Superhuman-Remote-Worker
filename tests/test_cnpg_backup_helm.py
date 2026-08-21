"""Render assertions for CloudNativePG backups — the Barman Cloud plugin path.

Backups are off by default (`databases.backup.method: none`) and, like the rest
of Phase 2/3, render nothing until a database moves to the `cnpg` engine.

The plugin's ObjectStore CRD is installed in no cluster this repo can reach, so
`kubectl apply --dry-run=server` cannot check that resource. The vendored schema
in `tests/data/barman_objectstore_crd.yaml` is the only structural check it gets.

Spec:  knowledge-base/knowledge/superpowers/specs/2026-08-21-cnpg-data-tier-ha-design.md
Plan:  knowledge-base/knowledge/superpowers/plans/2026-08-21-cnpg-backups.md
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
FULLNAME = f"{RELEASE}-{CHART_NAME}"

DATABASES = {
    "postgres": "postgres",
    "vector": "pgvector",
    "audit": "auditdb",
    "gitea": "giteadb",
    "keycloak": "keycloakdb",
}

OBJECTSTORE = "templates/databases/cnpg-objectstore.yaml"
SCHEDULED = "templates/databases/cnpg-scheduledbackup.yaml"
CLUSTER = "templates/databases/cnpg-cluster.yaml"

BACKUP_ON = (
    "databases.backup.method=objectstore",
    "databases.backup.destinationPath=s3://srw-pgbackup/dev",
    "databases.backup.endpointURL=http://minio.minio.svc.cluster.local:9000",
)

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


def _renders_nothing(*settings: str, show_only: str) -> bool:
    """helm errors when --show-only matches a template that produced nothing."""
    result = subprocess.run(
        _template_command(*settings, show_only=show_only),
        capture_output=True,
        text=True,
    )
    return result.returncode != 0 or "kind:" not in result.stdout


def _store(*settings: str) -> dict:
    return _kinds(_render(*settings, show_only=OBJECTSTORE), "ObjectStore")[0]


def test_no_objectstore_when_backups_are_off():
    assert _renders_nothing(*_all_cnpg(), show_only=OBJECTSTORE)


def test_no_objectstore_when_no_cluster_renders():
    """Backups configured but every database still on the statefulset engine:
    an ObjectStore nothing references is litter, and would misreport the
    release as backed up."""
    assert _renders_nothing(*BACKUP_ON, show_only=OBJECTSTORE)


def test_objectstore_shape():
    store = _store(*BACKUP_ON, *_all_cnpg())
    assert store["apiVersion"] == "barmancloud.cnpg.io/v1"
    assert store["metadata"]["name"] == f"{FULLNAME}-backups"
    configuration = store["spec"]["configuration"]
    assert configuration["destinationPath"] == "s3://srw-pgbackup/dev"
    assert configuration["endpointURL"] == "http://minio.minio.svc.cluster.local:9000"


def test_server_name_is_never_set():
    """It defaults to the cluster name. Setting it on a SHARED store would
    point all five clusters at one prefix and they would overwrite each
    other's base backups."""
    assert "serverName" not in _store(*BACKUP_ON, *_all_cnpg())["spec"]["configuration"]


def test_credentials_default_to_the_charts_own_secret():
    s3 = _store(*BACKUP_ON, *_all_cnpg())["spec"]["configuration"]["s3Credentials"]
    assert s3["accessKeyId"] == {"name": FULLNAME, "key": "BACKUP_S3_ACCESS_KEY_ID"}
    assert s3["secretAccessKey"] == {
        "name": FULLNAME,
        "key": "BACKUP_S3_SECRET_ACCESS_KEY",
    }


def test_credentials_secret_is_overridable():
    store = _store(*BACKUP_ON, *_all_cnpg(), "databases.backup.credentialsSecret=my-s3")
    assert (
        store["spec"]["configuration"]["s3Credentials"]["accessKeyId"]["name"]
        == "my-s3"
    )


def test_retention_and_compression_defaults():
    store = _store(*BACKUP_ON, *_all_cnpg())
    assert store["spec"]["retentionPolicy"] == "30d"
    assert store["spec"]["configuration"]["wal"]["compression"] == "gzip"
    assert store["spec"]["configuration"]["data"]["compression"] == "gzip"


def test_endpoint_ca_is_omitted_unless_configured():
    assert "endpointCA" not in _store(*BACKUP_ON, *_all_cnpg())["spec"]["configuration"]


def test_endpoint_ca_wires_a_private_ca_bundle():
    store = _store(
        *BACKUP_ON, *_all_cnpg(), "databases.backup.endpointCA.secretName=my-ca"
    )
    assert store["spec"]["configuration"]["endpointCA"] == {
        "name": "my-ca",
        "key": "ca.crt",
    }


def test_destination_path_is_required_when_backups_are_on():
    result = subprocess.run(
        _template_command("databases.backup.method=objectstore", *_all_cnpg()),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "destinationPath" in result.stderr


def test_data_compression_rejects_wal_only_algorithms():
    """xz and zstd are valid for WAL and invalid for data. The schema has to
    catch it -- the CRD is not installed anywhere we can dry-run against."""
    result = subprocess.run(
        _template_command(
            *BACKUP_ON, *_all_cnpg(), "databases.backup.data.compression=zstd"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_wal_compression_accepts_zstd():
    store = _store(*BACKUP_ON, *_all_cnpg(), "databases.backup.wal.compression=zstd")
    assert store["spec"]["configuration"]["wal"]["compression"] == "zstd"


def test_objectstore_validates_against_the_real_crd():
    """The only structural check this resource gets. See the module docstring."""
    import jsonschema

    schema = yaml.safe_load(
        (ROOT / "tests/data/barman_objectstore_crd.yaml").read_text()
    )
    jsonschema.validate(_store(*BACKUP_ON, *_all_cnpg()), schema)
