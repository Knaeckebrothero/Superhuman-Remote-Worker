"""Static infrastructure gates for Dynamic Canvas live preview foundation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


# Helm swapped schema validators in 3.19: up to 3.18 (xeipuuv/gojsonschema) it
# reports the offending key as a dotted path, from 3.19 (santhosh-tekuri) as a
# JSON pointer, and every message was reworded. CI pins 3.17 while developers
# usually have something newer, so assertions on rejection text must accept both
# dialects or they only hold on the machine they were written on.
CREDENTIAL_SOURCE_REJECTED = (
    "Must validate one and only one schema (oneOf)",  # helm <= 3.18 schema gate
    "'oneOf' failed",  # helm >= 3.19 schema gate
    "require exactly one",  # template guard, when the schema gate is inactive
)


def assert_schema_rejected(
    result: subprocess.CompletedProcess[str], values_path: str
) -> None:
    """Assert values.schema.json refused the render and named ``values_path``."""
    assert result.returncode != 0, result.stdout
    pointer = "/" + values_path.replace(".", "/")
    assert values_path in result.stderr or pointer in result.stderr, result.stderr


CANVAS_GATEWAY_COMMAND = [
    "uvicorn",
    "canvas_gateway:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8086",
    "--http",
    "h11",
    "--no-access-log",
]


def test_workspace_sshd_limits_canvas_direct_channels_to_loopback() -> None:
    expected = (
        "AllowTcpForwarding local",
        "PermitOpen 127.0.0.1:*",
        "GatewayPorts no",
        "AllowAgentForwarding no",
        "PermitTunnel no",
    )
    for relative_path in (
        "docker/Dockerfile.workspace",
        "docker/agent-vm-base/scripts/provision-stage2.sh",
    ):
        source = (ROOT / relative_path).read_text()
        for directive in expected:
            assert source.count(directive) == 1, (relative_path, directive)


def test_canvas_live_preview_helm_gate_is_boolean_and_default_off() -> None:
    values = yaml.safe_load((ROOT / "helm/values.yaml").read_text())
    assert values["canvas"]["livePreview"]["enabled"] is False
    assert values["canvas"]["livePreview"]["deniedPorts"] == []

    schema = json.loads((ROOT / "helm/values.schema.json").read_text())
    enabled_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["enabled"]
    assert enabled_schema == {"type": "boolean"}
    denied_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["deniedPorts"]
    assert denied_schema == {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 65535},
        "uniqueItems": True,
    }


def test_canvas_live_preview_gate_reaches_orchestrator_and_agent_config() -> None:
    configmap = (ROOT / "helm/templates/configmap.yaml").read_text()
    deployment = (ROOT / "helm/templates/orchestrator/deployment.yaml").read_text()
    assert configmap.count("CANVAS_LIVE_PREVIEW_ENABLED:") == 1
    assert ".Values.canvas.livePreview.enabled" in configmap
    assert deployment.count("- name: CANVAS_LIVE_PREVIEW_ENABLED") == 1
    assert "key: CANVAS_LIVE_PREVIEW_ENABLED" in deployment
    assert configmap.count("CANVAS_LIVE_PREVIEW_DENIED_PORTS:") == 1
    assert ".Values.canvas.livePreview.deniedPorts" in configmap
    assert deployment.count("- name: CANVAS_LIVE_PREVIEW_DENIED_PORTS") == 1
    assert "key: CANVAS_LIVE_PREVIEW_DENIED_PORTS" in deployment

    # Dynamically provisioned agent pods consume every shared ConfigMap key via
    # envFrom without duplicating provisioner-specific env lists. The agent's
    # actual tool capability still comes only from the orchestrator's positive
    # attach bit and is covered by the callable/runtime tests.
    for relative_path in (
        "orchestrator/services/agent_provisioner.py",
        "orchestrator/services/persistent_provisioner.py",
    ):
        source = (ROOT / relative_path).read_text()
        assert '"envFrom"' in source
        assert '"configMapRef"' in source


def test_public_examples_keep_live_preview_disabled() -> None:
    env_example = (ROOT / ".env.example").read_text()
    local_values = yaml.safe_load(
        (ROOT / "deployment/values-local.yaml.example").read_text()
    )
    production_values = yaml.safe_load((ROOT / "helm/values.example.yaml").read_text())
    assert "CANVAS_LIVE_PREVIEW_ENABLED=false" in env_example
    assert "CANVAS_LIVE_PREVIEW_DENIED_PORTS=8501,9000" in env_example
    assert local_values["canvas"]["livePreview"]["enabled"] is False
    assert local_values["canvas"]["livePreview"]["deniedPorts"] == []
    assert (
        local_values["canvas"]["livePreview"]["viewer"]["database"]["credentials"][
            "vaultPath"
        ]
        == ""
    )
    assert production_values["canvas"]["livePreview"]["enabled"] is False
    assert production_values["canvas"]["livePreview"]["deniedPorts"] == []
    assert (
        production_values["canvas"]["livePreview"]["viewer"]["database"]["credentials"][
            "vaultPath"
        ]
        == ""
    )


def test_canvas_viewer_chart_values_are_default_off_and_fail_closed() -> None:
    values = yaml.safe_load((ROOT / "helm/values.yaml").read_text())
    viewer = values["canvas"]["livePreview"]["viewer"]
    assert viewer["enabled"] is False
    assert viewer["cookieMode"] == ""
    assert viewer["domain"] == ""
    assert viewer["hostSuffix"] == ""
    assert viewer["cockpitOrigins"] == []
    assert viewer["pslBoundaryVerified"] is False
    assert viewer["database"] == {
        "username": "srw_canvas_gateway",
        "credentials": {
            "create": False,
            "existingSecret": "",
            "existingSecretCnpgCompatible": False,
            "vaultPath": "",
            "passwordKey": "CANVAS_VIEWER_POSTGRES_PASSWORD",
        },
        "provisionRole": False,
        "pool": {"min": 1, "max": 4},
    }
    assert viewer["networkPolicy"]["edgeNamespaceSelector"] == {}
    assert viewer["networkPolicy"]["edgePodSelector"] == {}

    schema = json.loads((ROOT / "helm/values.schema.json").read_text())
    viewer_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["viewer"]
    assert (
        viewer_schema["properties"]["limits"]["properties"]["maxHeaderFields"][
            "minimum"
        ]
        == 10
    )
    database_schema = viewer_schema["properties"]["database"]
    assert database_schema["additionalProperties"] is False
    assert database_schema["properties"]["credentials"]["additionalProperties"] is False
    assert database_schema["properties"]["pool"]["properties"]["min"]["maximum"] == 16
    assert database_schema["properties"]["pool"]["properties"]["max"]["maximum"] == 16
    assert viewer_schema["properties"]["networkPolicy"]["additionalProperties"] is False
    non_empty = schema["definitions"]["nonEmptyLabelSelector"]
    assert len(non_empty["allOf"][1]["anyOf"]) == 2


def test_canvas_gateway_templates_default_dark_with_optional_ingress() -> None:
    gateway_dir = ROOT / "helm/templates/canvas-gateway"
    templates = {path.name: path.read_text() for path in gateway_dir.glob("*.yaml")}
    assert set(templates) == {
        "configmap.yaml",
        "database-external-secret.yaml",
        "database-role-job.yaml",
        "database-role.yaml",
        "database-secret.yaml",
        "deployment.yaml",
        "ingress.yaml",
        "network-policy.yaml",
        "service.yaml",
    }
    # The optional plug-and-play wildcard route is the only Ingress and is
    # double-gated: it renders nothing unless the viewer AND the ingress are
    # both explicitly enabled (knowledge-base/knowledge/issues/canvas_hosted_edge_use_cloudflare_tunnel.md).
    assert all(
        "kind: Ingress" not in source
        for name, source in templates.items()
        if name != "ingress.yaml"
    )
    ingress = templates["ingress.yaml"]
    assert "kind: Ingress" in ingress
    assert "if and $viewer.enabled $viewer.ingress.enabled" in ingress
    assert "type: ClusterIP" in templates["service.yaml"]
    assert "port: 8086" in templates["service.yaml"]

    deployment = templates["deployment.yaml"]
    assert "canvas_gateway:app" in deployment
    assert "--http" in deployment
    assert "- h11" in deployment
    assert "--no-access-log" in deployment
    assert "automountServiceAccountToken: false" in deployment
    assert "canvas-gateway-config" in deployment
    assert 'include "srw.configMapName"' not in deployment
    assert "CANVAS_VIEWER_POSTGRES_USER" in deployment
    assert "CANVAS_VIEWER_POSTGRES_PASSWORD" in deployment
    assert "- name: POSTGRES_USER" not in deployment
    assert "- name: POSTGRES_PASSWORD" not in deployment
    assert "DATABASE_URL" not in deployment

    database_external_secret = templates["database-external-secret.yaml"]
    assert "$viewer.enabled" in database_external_secret
    assert "$credentials.vaultPath" in database_external_secret
    assert "kind: ExternalSecret" in database_external_secret
    assert "dataFrom:" not in database_external_secret

    gateway_config = templates["configmap.yaml"]
    assert "CANVAS_VIEWER_POSTGRES_HOST:" in gateway_config
    assert "CANVAS_VIEWER_POSTGRES_PORT:" in gateway_config
    assert "CANVAS_VIEWER_POSTGRES_DB:" in gateway_config
    assert "CANVAS_VIEWER_POSTGRES_MIN_CONNECTIONS:" in gateway_config
    assert "CANVAS_VIEWER_POSTGRES_MAX_CONNECTIONS:" in gateway_config
    assert "\n  POSTGRES_HOST:" not in gateway_config
    assert "\n  POSTGRES_PORT:" not in gateway_config
    assert "\n  POSTGRES_DB:" not in gateway_config
    assert "DATABASE_URL" not in gateway_config

    role_job = templates["database-role-job.yaml"]
    assert "kind: Job" in role_job
    assert "kind: NetworkPolicy" in role_job
    assert "automountServiceAccountToken: false" in role_job
    assert "activeDeadlineSeconds:" in role_job
    assert "ttlSecondsAfterFinished:" not in role_job
    assert "helm.sh/hook" not in role_job
    assert ".Release.Revision" in role_job
    assert "operator_cli.canvas_viewer_database_attestation" in role_job

    database_role = templates["database-role.yaml"]
    assert "kind: DatabaseRole" in database_role
    assert "databaseRoleReclaimPolicy: retain" in database_role
    assert "passwordSecret:" in database_role

    policy = templates["network-policy.yaml"]
    assert "non-empty networkPolicy.edgeNamespaceSelector" in policy
    assert "port: 8086" in policy
    assert "port: 5432" in policy
    assert "port: 30022" in policy
    assert "port: 22" in policy

    shared_ingress = (ROOT / "helm/templates/ingress.yaml").read_text()
    assert "canvas-gateway" not in shared_ingress
    assert "8086" not in shared_ingress


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_canvas_gateway_helm_render_contract_and_selector_gate() -> None:
    chart = ROOT / "helm"
    test_values = chart / "ci/test-values.yaml"
    base = ["helm", "template", "canvas-test", str(chart), "-f", str(test_values)]

    default_render = subprocess.run(
        base,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    default_objects = [item for item in yaml.safe_load_all(default_render) if item]
    assert not any(
        item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway"
        for item in default_objects
    )
    assert not any(
        item.get("kind") == "ExternalSecret"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
        for item in default_objects
    )

    enabled_args = [
        "--set",
        "canvas.livePreview.enabled=true",
        "--set",
        "canvas.livePreview.viewer.enabled=true",
        "--set",
        "canvas.livePreview.viewer.cookieMode=psl-isolated",
        "--set-string",
        "canvas.livePreview.viewer.domain=example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.hostSuffix=.example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.cockpitOrigins[0]=https://cockpit.example.test",
        "--set",
        "canvas.livePreview.viewer.pslBoundaryVerified=true",
        "--set-string",
        "canvas.livePreview.viewer.database.credentials.existingSecret=canvas-viewer-db",
    ]

    rejected = subprocess.run(
        [*base, *enabled_args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "edgeNamespaceSelector" in rejected.stderr
    assert "edgePodSelector" in rejected.stderr

    # `{matchLabels: {}}` is still an unconstrained selector and must not turn
    # into namespace-wide/pod-wide ingress.
    empty_selector = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-json",
            'canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector={"matchLabels":{}}',
            "--set-json",
            'canvas.livePreview.viewer.networkPolicy.edgePodSelector={"matchLabels":{}}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert empty_selector.returncode != 0

    nested_production_suffix = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.hostSuffix=.canvas.example-userland.test",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert nested_production_suffix.returncode != 0
    assert "must exactly equal" in nested_production_suffix.stderr

    generated_production_credentials = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.database.credentials.existingSecret=",
            "--set",
            "canvas.livePreview.viewer.database.credentials.create=true",
            "--set",
            "canvas.livePreview.viewer.database.provisionRole=true",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated_production_credentials.returncode != 0
    assert "requires chart-owned databases.postgres.engine=cnpg" in (
        generated_production_credentials.stderr
    )

    provisioned_production_role = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set",
            "canvas.livePreview.viewer.database.provisionRole=true",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert provisioned_production_role.returncode != 0
    assert "requires chart-owned databases.postgres.engine=cnpg" in (
        provisioned_production_role.stderr
    )

    # Render coverage only: this proves the manifests compose, not that Helm can
    # readiness-order a newly installed operator/webhook before the CNPG CRs.
    # The supported empty-cluster sequence installs the operator first.
    cnpg_render = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.database.credentials.existingSecret=",
            "--set",
            "canvas.livePreview.viewer.database.credentials.create=true",
            "--set",
            "canvas.livePreview.viewer.database.provisionRole=true",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
            "--set-string",
            "databases.postgres.engine=cnpg",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    cnpg_objects = [item for item in yaml.safe_load_all(cnpg_render) if item]
    database_role = next(
        item
        for item in cnpg_objects
        if item.get("kind") == "DatabaseRole"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-role"
    )
    assert database_role["spec"]["name"] == "srw_canvas_gateway"
    assert database_role["spec"]["ensure"] == "present"
    role_secret_name = database_role["spec"]["passwordSecret"]["name"]
    assert role_secret_name.endswith("-canvas-gateway-db-cnpg")
    # CNPG defaults these fields and omits them from the persisted CR. Rendering
    # them explicitly makes a GitOps controller report permanent false drift.
    assert {
        "superuser",
        "createdb",
        "createrole",
        "replication",
        "bypassrls",
        "inRoles",
    }.isdisjoint(database_role["spec"])
    credential_secrets = [
        item
        for item in cnpg_objects
        if item.get("kind") == "Secret"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
    ]
    assert len(credential_secrets) == 2
    role_secret = next(
        item
        for item in credential_secrets
        if item["metadata"]["name"] == role_secret_name
    )
    gateway_secret = next(
        item
        for item in credential_secrets
        if item["metadata"]["name"] != role_secret_name
    )
    assert gateway_secret["type"] == "Opaque"
    assert {"username", "password", "CANVAS_VIEWER_POSTGRES_PASSWORD"} == set(
        gateway_secret["stringData"]
    )
    assert role_secret["type"] == "kubernetes.io/basic-auth"
    assert set(role_secret["stringData"]) == {"username", "password"}
    assert (
        gateway_secret["stringData"]["CANVAS_VIEWER_POSTGRES_PASSWORD"]
        == role_secret["stringData"]["password"]
    )
    assert role_secret["metadata"]["labels"]["cnpg.io/reload"] == "true"
    role_job = next(
        item
        for item in cnpg_objects
        if item.get("kind") == "Job"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-role"
    )
    assert "helm.sh/hook" not in role_job["metadata"].get("annotations", {})
    assert "ttlSecondsAfterFinished" not in role_job["spec"]
    role_config = next(
        item
        for item in cnpg_objects
        if item.get("kind") == "ConfigMap"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-role"
    )
    packaged_sql = role_config["data"]
    assert set(packaged_sql) == {
        "canvas-viewer-role.sql",
        "canvas-viewer-role-safety.sql",
        "canvas-viewer-self-configure.sql",
        "canvas-viewer-grants.sql",
    }
    included_files = {
        line.removeprefix("\\ir ").strip()
        for source in packaged_sql.values()
        for line in source.splitlines()
        if line.startswith("\\ir ")
    }
    assert included_files <= set(packaged_sql)

    rendered = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    assert not any(
        item.get("kind") == "ExternalSecret"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
        for item in objects
    )
    gateway_objects = [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway"
    ]
    assert {item["kind"] for item in gateway_objects} == {
        "ConfigMap",
        "Deployment",
        "NetworkPolicy",
        "Service",
    }
    for ingress_object in (item for item in objects if item["kind"] == "Ingress"):
        ingress_yaml = yaml.safe_dump(ingress_object)
        assert "canvas-gateway" not in ingress_yaml
        assert "number: 8086" not in ingress_yaml

    by_kind = {item["kind"]: item for item in gateway_objects}
    assert by_kind["Service"]["spec"]["type"] == "ClusterIP"
    assert by_kind["Service"]["spec"]["ports"][0]["port"] == 8086
    ingress = by_kind["NetworkPolicy"]["spec"]["ingress"]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8086}]
    assert ingress[0]["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"edge": "trusted"}},
            "podSelector": {"matchLabels": {"app": "viewer-edge"}},
        }
    ]
    gateway_egress_ports = {
        port["port"]
        for rule in by_kind["NetworkPolicy"]["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert {22, 53, 5432, 30022} <= gateway_egress_ports

    workspace_policies = [
        item
        for item in objects
        if item["kind"] == "NetworkPolicy"
        and "-workspace-policy-" in item["metadata"]["name"]
    ]
    assert workspace_policies
    for policy in workspace_policies:
        gateway_rules = [
            rule
            for rule in policy["spec"]["ingress"]
            if any(
                peer.get("podSelector", {})
                .get("matchLabels", {})
                .get("app.kubernetes.io/component")
                == "canvas-gateway"
                for peer in rule.get("from", [])
            )
        ]
        assert len(gateway_rules) == 1
        assert gateway_rules[0]["ports"] == [
            {"protocol": "TCP", "port": 30022},
            {"protocol": "TCP", "port": 22},
        ]
        assert len(gateway_rules[0]["from"]) == 1

    container = by_kind["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == CANVAS_GATEWAY_COMMAND
    assert container["envFrom"] == [
        {"configMapRef": {"name": by_kind["ConfigMap"]["metadata"]["name"]}}
    ]
    environment_names = {entry["name"] for entry in container["env"]}
    assert environment_names == {
        "PORT",
        "CANVAS_VIEWER_POSTGRES_PASSWORD",
        "SSH_KEY_PATH",
    }
    assert "DATABASE_URL" not in environment_names
    assert "POSTGRES_USER" not in environment_names
    assert "POSTGRES_PASSWORD" not in environment_names
    credential_refs = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in container["env"]
        if "valueFrom" in entry
    }
    # Only the password is secret material; the username arrives via the
    # allowlisted gateway ConfigMap (envFrom).
    assert credential_refs == {
        "CANVAS_VIEWER_POSTGRES_PASSWORD": {
            "name": "canvas-viewer-db",
            "key": "CANVAS_VIEWER_POSTGRES_PASSWORD",
        },
    }

    gateway_config = by_kind["ConfigMap"]["data"]
    assert {
        "CANVAS_VIEWER_POSTGRES_HOST",
        "CANVAS_VIEWER_POSTGRES_PORT",
        "CANVAS_VIEWER_POSTGRES_DB",
        "CANVAS_VIEWER_POSTGRES_USER",
        "CANVAS_VIEWER_POSTGRES_MIN_CONNECTIONS",
        "CANVAS_VIEWER_POSTGRES_MAX_CONNECTIONS",
    } <= gateway_config.keys()
    assert "DATABASE_URL" not in gateway_config
    assert "POSTGRES_HOST" not in gateway_config
    assert "POSTGRES_PORT" not in gateway_config
    assert "POSTGRES_DB" not in gateway_config


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_canvas_gateway_vault_credentials_follow_viewer_lifecycle() -> None:
    chart = ROOT / "helm"
    test_values = chart / "ci/test-values.yaml"
    base = ["helm", "template", "canvas-test", str(chart), "-f", str(test_values)]
    vault_path = "test/canvas-gateway-db"

    # Even disabling ESO entirely remains valid while the viewer is off.
    disabled_render = subprocess.run(
        [
            *base,
            "--set",
            "externalSecrets.enabled=false",
            "--set-string",
            f"canvas.livePreview.viewer.database.credentials.vaultPath={vault_path}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    disabled_objects = [item for item in yaml.safe_load_all(disabled_render) if item]
    assert not any(
        item.get("kind") == "ExternalSecret"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
        for item in disabled_objects
    )

    enabled_args = [
        "--set",
        "canvas.livePreview.enabled=true",
        "--set",
        "canvas.livePreview.viewer.enabled=true",
        "--set-string",
        "canvas.livePreview.viewer.cookieMode=psl-isolated",
        "--set-string",
        "canvas.livePreview.viewer.domain=example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.hostSuffix=.example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.cockpitOrigins[0]=https://cockpit.example.test",
        "--set",
        "canvas.livePreview.viewer.pslBoundaryVerified=true",
        "--set-string",
        "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
        "--set-string",
        "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
    ]

    no_source = subprocess.run(
        [*base, *enabled_args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert no_source.returncode != 0
    assert any(text in no_source.stderr for text in CREDENTIAL_SOURCE_REJECTED), (
        no_source.stderr
    )

    vault_without_eso = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set",
            "externalSecrets.enabled=false",
            "--set-string",
            f"canvas.livePreview.viewer.database.credentials.vaultPath={vault_path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert vault_without_eso.returncode != 0
    assert "requires externalSecrets.enabled=true" in vault_without_eso.stderr

    multiple_sources = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.database.credentials.existingSecret=canvas-viewer-db",
            "--set-string",
            f"canvas.livePreview.viewer.database.credentials.vaultPath={vault_path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert multiple_sources.returncode != 0
    assert any(
        text in multiple_sources.stderr for text in CREDENTIAL_SOURCE_REJECTED
    ), multiple_sources.stderr

    vault_render = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            f"canvas.livePreview.viewer.database.credentials.vaultPath={vault_path}",
            "--set",
            "canvas.livePreview.viewer.database.provisionRole=true",
            "--set-string",
            "databases.postgres.engine=cnpg",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(vault_render) if item]
    external_secrets = [
        item
        for item in objects
        if item.get("kind") == "ExternalSecret"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
    ]
    assert len(external_secrets) == 2
    # Viewer on + Vault coordinates renders the gateway itself too — the
    # credential mapping follows the gate rather than existing on its own.
    assert any(
        item.get("kind") == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway"
        for item in objects
    )
    database_role = next(item for item in objects if item.get("kind") == "DatabaseRole")
    role_secret_name = database_role["spec"]["passwordSecret"]["name"]
    by_target_name = {item["spec"]["target"]["name"]: item for item in external_secrets}
    role_external_secret = by_target_name[role_secret_name]
    gateway_secret_name = next(
        name for name in by_target_name if name != role_secret_name
    )
    gateway_external_secret = by_target_name[gateway_secret_name]

    # Both Kubernetes projections read the same existing Vault property; no
    # second Vault entry or manual password copy is part of the install flow.
    expected_remote_mapping = [
        {
            "secretKey": "password",
            "remoteRef": {
                "key": vault_path,
                "property": "CANVAS_VIEWER_POSTGRES_PASSWORD",
            },
        }
    ]
    for external_secret in external_secrets:
        assert "dataFrom" not in external_secret["spec"]
        assert external_secret["spec"]["data"] == expected_remote_mapping
        assert (
            external_secret["spec"]["target"]["name"]
            == external_secret["metadata"]["name"]
        )

    gateway_template = gateway_external_secret["spec"]["target"]["template"]
    assert "type" not in gateway_template
    assert set(gateway_template["data"]) == {
        "username",
        "password",
        "CANVAS_VIEWER_POSTGRES_PASSWORD",
    }
    role_template = role_external_secret["spec"]["target"]["template"]
    assert role_template["type"] == "kubernetes.io/basic-auth"
    assert role_template["metadata"]["labels"]["cnpg.io/reload"] == "true"
    assert set(role_template["data"]) == {"username", "password"}

    gateway = next(
        item
        for item in objects
        if item.get("kind") == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "canvas-gateway"
    )
    credential_refs = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in gateway["spec"]["template"]["spec"]["containers"][0]["env"]
        if "valueFrom" in entry
    }
    assert credential_refs == {
        "CANVAS_VIEWER_POSTGRES_PASSWORD": {
            "name": gateway_secret_name,
            "key": "CANVAS_VIEWER_POSTGRES_PASSWORD",
        },
    }


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_canvas_gateway_development_can_provision_restricted_internal_role() -> None:
    chart = ROOT / "helm"
    test_values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "canvas-test",
            str(chart),
            "-f",
            str(test_values),
            "--set",
            "canvas.livePreview.enabled=true",
            "--set",
            "canvas.livePreview.viewer.enabled=true",
            "--set-string",
            "canvas.livePreview.viewer.deploymentProfile=development",
            "--set-string",
            "canvas.livePreview.viewer.cookieMode=development-cookie-free",
            "--set-string",
            "canvas.livePreview.viewer.domain=example-userland.test",
            "--set-string",
            "canvas.livePreview.viewer.hostSuffix=.canvas.example-userland.test",
            "--set-string",
            "canvas.livePreview.viewer.cockpitOrigins[0]=https://cockpit.example.test",
            "--set",
            "canvas.livePreview.viewer.database.credentials.create=true",
            "--set",
            "canvas.livePreview.viewer.database.provisionRole=true",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]

    credentials = [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway-credentials"
    ]
    assert len(credentials) == 1
    assert credentials[0]["kind"] == "Secret"
    assert credentials[0]["type"] == "Opaque"
    assert set(credentials[0]["stringData"]) == {
        "username",
        "password",
        "CANVAS_VIEWER_POSTGRES_PASSWORD",
    }

    role_objects = [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway-role"
    ]
    assert {item["kind"] for item in role_objects} == {
        "ConfigMap",
        "Job",
        "NetworkPolicy",
    }
    role_by_kind = {item["kind"]: item for item in role_objects}
    job_spec = role_by_kind["Job"]["spec"]
    assert role_by_kind["Job"]["metadata"]["name"].endswith("-role-1")
    assert job_spec["activeDeadlineSeconds"] <= 1200
    assert job_spec["backoffLimit"] <= 4
    assert "ttlSecondsAfterFinished" not in job_spec
    assert job_spec["template"]["spec"]["automountServiceAccountToken"] is False
    init_container_names = {
        container["name"]
        for container in job_spec["template"]["spec"]["initContainers"]
    }
    assert {
        "wait-for-owner-schema",
        "reconcile-legacy-identity",
        "preflight-owner-contract",
        "prove-target-password",
        "reconcile-owner-grants",
    } == init_container_names
    attestation = job_spec["template"]["spec"]["containers"][0]
    assert attestation["command"] == [
        "python",
        "-m",
        "operator_cli.canvas_viewer_database_attestation",
    ]
    assert {entry["name"] for entry in attestation["env"]} == {
        "CANVAS_VIEWER_POSTGRES_PASSWORD"
    }

    role_policy = role_by_kind["NetworkPolicy"]["spec"]
    assert role_policy["ingress"] == []
    egress_ports = {
        port["port"] for rule in role_policy["egress"] for port in rule.get("ports", [])
    }
    assert egress_ports == {53, 5432}


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_canvas_gateway_cnpg_topologies_require_compatible_existing_secret() -> None:
    chart = ROOT / "helm"
    test_values = chart / "ci/test-values.yaml"
    common = [
        "helm",
        "template",
        "canvas-test",
        str(chart),
        "-f",
        str(test_values),
        "--set",
        "canvas.livePreview.enabled=true",
        "--set",
        "canvas.livePreview.viewer.enabled=true",
        "--set-string",
        "canvas.livePreview.viewer.deploymentProfile=development",
        "--set-string",
        "canvas.livePreview.viewer.cookieMode=development-cookie-free",
        "--set-string",
        "canvas.livePreview.viewer.domain=example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.hostSuffix=.canvas.example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.cockpitOrigins[0]=https://cockpit.example.test",
        "--set-string",
        "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
        "--set-string",
        "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        "--set-string",
        "canvas.livePreview.viewer.database.credentials.existingSecret=canvas-viewer-db",
        "--set",
        "canvas.livePreview.viewer.database.provisionRole=true",
    ]

    rejected = subprocess.run(
        [*common, "--set-string", "databases.postgres.engine=cnpg"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "existingSecretCnpgCompatible=true" in rejected.stderr

    for engine, expects_legacy_identity in (("migrating", True), ("cnpg", False)):
        rendered = subprocess.run(
            [
                *common,
                "--set",
                "canvas.livePreview.viewer.database.credentials.existingSecretCnpgCompatible=true",
                "--set-string",
                f"databases.postgres.engine={engine}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        objects = [item for item in yaml.safe_load_all(rendered) if item]
        assert any(
            item.get("kind") == "DatabaseRole"
            and item.get("spec", {}).get("name") == "srw_canvas_gateway"
            for item in objects
        )
        job = next(
            item
            for item in objects
            if item.get("kind") == "Job"
            and item.get("metadata", {})
            .get("labels", {})
            .get("app.kubernetes.io/component")
            == "canvas-gateway-role"
        )
        init_names = {
            container["name"]
            for container in job["spec"]["template"]["spec"]["initContainers"]
        }
        assert ("reconcile-legacy-identity" in init_names) is expects_legacy_identity

    external = subprocess.run(
        [
            *common,
            "--set",
            "databases.postgres.internal=false",
            "--set-string",
            "databases.postgres.externalHost=postgres.example.test",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert external.returncode != 0
    assert "supported only for chart-owned internal Postgres" in external.stderr
