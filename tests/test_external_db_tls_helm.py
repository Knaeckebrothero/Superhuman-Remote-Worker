from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"

SSL_MODES = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


def _template_command(*settings: str, show_only: str | None = None) -> list[str]:
    command = [
        "helm", "template", "external-db-tls-test", str(CHART),
        "-f", str(CHART / "ci/test-values.yaml"),
    ]
    if show_only:
        command.extend(["--show-only", show_only])
    for setting in settings:
        command.extend(["--set", setting])
    return command


def _render(*settings: str, show_only: str | None = None) -> list[dict]:
    rendered = subprocess.run(
        _template_command(*settings, show_only=show_only),
        check=True, capture_output=True, text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _only_kind(documents: list[dict], kind: str) -> dict:
    matches = [document for document in documents if document.get("kind") == kind]
    assert len(matches) == 1
    return matches[0]


def _config_map(*settings: str) -> dict:
    return _only_kind(_render(*settings, show_only="templates/configmap.yaml"), "ConfigMap")


pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")


def test_default_ssl_mode_is_prefer_for_all_three_app_databases():
    """`prefer` is asyncpg's own default, so the default render is a no-op."""
    data = _config_map()["data"]
    assert data["POSTGRES_SSLMODE"] == "prefer"
    assert data["VECTOR_POSTGRES_SSLMODE"] == "prefer"
    assert data["AUDIT_POSTGRES_SSLMODE"] == "prefer"


@pytest.mark.parametrize("mode", SSL_MODES)
def test_every_enum_value_renders(mode):
    data = _config_map(f"databases.postgres.sslMode={mode}")["data"]
    assert data["POSTGRES_SSLMODE"] == mode


def test_each_database_has_an_independent_ssl_mode():
    data = _config_map(
        "databases.postgres.sslMode=verify-full",
        "databases.vector.sslMode=require",
        "databases.audit.sslMode=disable",
    )["data"]
    assert data["POSTGRES_SSLMODE"] == "verify-full"
    assert data["VECTOR_POSTGRES_SSLMODE"] == "require"
    assert data["AUDIT_POSTGRES_SSLMODE"] == "disable"


def test_ssl_mode_is_emitted_in_external_mode_too():
    data = _config_map(
        "databases.postgres.internal=false",
        "databases.postgres.externalHost=pg.example.com",
        "databases.postgres.sslMode=verify-full",
    )["data"]
    assert data["POSTGRES_HOST"] == "pg.example.com"
    assert data["POSTGRES_SSLMODE"] == "verify-full"


def test_schema_rejects_an_invalid_ssl_mode():
    result = subprocess.run(
        _template_command("databases.postgres.sslMode=bogus"),
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "sslMode" in result.stderr or "bogus" in result.stderr


def test_gitea_keeps_its_own_ssl_mode_knob_and_default():
    """Gitea has a separate knob with a different default; don't unify them.

    Gitea reads GITEA__database__SSL_MODE from its own StatefulSet env, not the
    shared ConfigMap, and defaults to "disable" rather than "prefer".
    """
    assert "GITEA_DB_SSLMODE" not in _config_map()["data"]

    # gitea.yaml renders a PVC + a StatefulSet (not a Deployment), and the
    # gitea container is the only entry under `containers`.
    stateful_set = _only_kind(
        _render(show_only="templates/services/gitea.yaml"), "StatefulSet"
    )
    container = stateful_set["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "gitea"
    ssl_mode = [e for e in container["env"] if e["name"] == "GITEA__database__SSL_MODE"]
    assert len(ssl_mode) == 1
    assert ssl_mode[0]["value"] == "disable"


# ---------------------------------------------------------------------------
# A ConfigMap key is not enough. The orchestrator and the llm-seed Job build
# their env EXPLICITLY and do NOT envFrom the shared ConfigMap -- the trap is
# documented at helm/templates/orchestrator/deployment.yaml:108-110. Agent pods
# DO envFrom it, so they inherit new keys for free. These tests pin that split;
# asserting only on ConfigMap contents let a dead knob ship.
# ---------------------------------------------------------------------------

SSL_ENV_KEYS = ["POSTGRES_SSLMODE", "VECTOR_POSTGRES_SSLMODE", "AUDIT_POSTGRES_SSLMODE"]


def _container(documents: list[dict], kind: str, name: str) -> dict:
    workload = _only_kind(documents, kind)
    matches = [
        c for c in workload["spec"]["template"]["spec"]["containers"] if c["name"] == name
    ]
    assert len(matches) == 1, f"expected exactly one {name} container"
    return matches[0]


def test_orchestrator_container_receives_every_ssl_mode_env():
    container = _container(
        _render(show_only="templates/orchestrator/deployment.yaml"),
        "Deployment", "orchestrator",
    )
    by_name = {entry["name"]: entry for entry in container["env"]}
    for key in SSL_ENV_KEYS:
        assert key in by_name, (
            f"{key} is missing from the orchestrator container env. The "
            "orchestrator does not envFrom the shared ConfigMap, so adding the "
            "key to configmap.yaml alone leaves the knob dead."
        )
        assert by_name[key]["valueFrom"]["configMapKeyRef"]["key"] == key


def test_llm_seed_job_receives_the_app_ssl_mode_env():
    container = _container(
        _render("llm.seed.enabled=true", show_only="templates/orchestrator/llm-seed-job.yaml"),
        "Job", "seed",
    )
    by_name = {entry["name"]: entry for entry in container["env"]}
    assert "POSTGRES_SSLMODE" in by_name
    assert by_name["POSTGRES_SSLMODE"]["valueFrom"]["configMapKeyRef"]["key"] == "POSTGRES_SSLMODE"


def test_agent_pods_inherit_ssl_mode_via_env_from():
    """Agents need no explicit wiring -- verified live on k3d, where an agent
    pod already reported POSTGRES_SSLMODE=prefer purely through envFrom."""
    container = _container(
        _render("agent.stateless.enabled=true",
                show_only="templates/agent/stateless-deployment.yaml"),
        "Deployment", "agent",
    )
    sources = [
        source["configMapRef"]["name"]
        for source in container.get("envFrom", [])
        if "configMapRef" in source
    ]
    assert any("config" in name for name in sources), (
        "agent pods must envFrom the shared ConfigMap, or they stop inheriting new keys"
    )
