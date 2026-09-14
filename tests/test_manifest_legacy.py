"""Migration fidelity at the versioned legacy/generic configuration boundary."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from orchestrator.schemas.job_create import JobCreate
from orchestrator.security.access import redact_config_override
from orchestrator.services.config_resolver import resolve_config
from orchestrator.services.manifest_legacy import preview_legacy_job
from shared.manifests import ManifestError
from shared.runtime.core.loader import load_config_from_resolved


def migrate(job=None, **kwargs):
    options = {
        "name": "developer-job",
        "scope": {"kind": "Account", "name": "personal"},
        "image": "example/srw-worker:1",
        "workspace": {"template": {"inline": {"backend": "sandbox"}}},
        "connectors": {},
        "grant_strip": lambda data: data,
    }
    options.update(kwargs)
    return preview_legacy_job(
        job
        or JobCreate(description="Develop the application", config_name="developer"),
        **options,
    )


def test_real_legacy_expert_retains_effective_settings_prompts_and_policy_filter():
    def policy_filter(data):
        result = deepcopy(data)
        result["tools"]["workspace"] = ["read_file"]
        return result

    policy = Mock(side_effect=policy_filter)
    layers = {
        "base_defaults": {
            "llm": {"reasoning_level": "low"},
            "migration_example": {"keep": "base", "delete": True},
        },
        "project_overrides": {"migration_example": {"keep": "project"}},
    }
    job = JobCreate(
        description="Build the C++ application",
        config_name="developer",
        config_override={"migration_example": {"delete": None}},
    )
    expected = redact_config_override(
        resolve_config(
            base_config_name=job.config_name,
            request_override=deepcopy(job.config_override),
            expert_type="worker",
            grant_strip=policy_filter,
            **deepcopy(layers),
        )
    )
    result = migrate(job, grant_strip=policy, **layers)
    policy.assert_called_once()
    config = result["resolved"][0]["spec"]["runtime"]["config"]
    # Only the serialization timestamp differs between independent resolutions.
    assert {key: value for key, value in config.items() if key != "resolved_at"} == {
        key: value for key, value in expected.items() if key != "resolved_at"
    }
    assert config["agent"]["migration_example"] == {"keep": "project"}
    assert config["agent"]["tools"]["workspace"] == ["read_file"]
    assert config["agent"]["llm"]["reasoning_level"] == "high"
    hydrated = load_config_from_resolved(config)
    old_hydrated = load_config_from_resolved(expected)
    assert hydrated.tools == old_hydrated.tools
    assert hydrated.workspace == old_hydrated.workspace
    assert hydrated.extra["_resolved_prompts"] == expected["prompts"]
    assert hydrated.extra["_resolved_instructions"] == expected["instructions"]
    assignment = result["resolved"][1]["spec"]
    assert assignment["task"]["text"] == job.description
    assert assignment["completion"] == {"mode": "Reported"}
    assert (
        assignment["execution"]["workspace"]["template"]["inline"]["backend"]
        == hydrated.workspace.backend
    )
    assert result["legacy"]["executionEnabled"] is False
    assert result["admissionReady"] is False


def test_legacy_credentials_are_not_copied_into_exported_private_payload():
    sentinel = "TEST-CREDENTIAL-DO-NOT-EXPORT"
    job = JobCreate(
        description="Check migration",
        config_name="developer",
        config_override={
            "llm": {"api_key": sentinel},
            "migration_example": {
                "password": sentinel,
                "token": sentinel,
                "keep": "public",
            },
        },
    )
    result = migrate(job)
    assert sentinel not in json.dumps(result)
    assert result["documents"][0]["spec"]["runtime"]["config"]["agent"][
        "migration_example"
    ] == {"keep": "public"}


@pytest.mark.parametrize(
    "field,value", [("priority", 1), ("expert", "developer"), ("datasource_ids", [])]
)
def test_unmapped_legacy_inputs_fail_before_resolution(monkeypatch, field, value):
    resolver = Mock(
        side_effect=AssertionError("Unmapped input reached legacy resolution")
    )
    monkeypatch.setattr(
        "orchestrator.services.manifest_legacy.resolve_config", resolver
    )
    job = JobCreate(
        description="Unsupported mapping", config_name="developer", **{field: value}
    )
    with pytest.raises(ManifestError) as error:
        migrate(job)
    assert error.value.issue.code == "UnsupportedLegacyInput"
    resolver.assert_not_called()


def test_adapter_requires_explicit_policy_and_pre_dispatch_workspace_mapping():
    with pytest.raises(ManifestError) as error:
        migrate(grant_strip=None)
    assert error.value.issue.code == "MissingLegacyPolicy"
    with pytest.raises(ManifestError) as error:
        migrate(JobCreate(description="Path config", config_name="../worker_base"))
    assert error.value.issue.code == "UnsupportedLegacyConfig"
    with pytest.raises(ManifestError) as error:
        migrate(workspace=None)
    assert error.value.issue.code == "LegacyWorkspaceMismatch"


def test_live_delivery_is_rejected_instead_of_becoming_authored_config(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.manifest_legacy.resolve_config",
        lambda **kwargs: {
            "agent": {
                "workspace": {"backend": "sandbox", "remote": {"host": "internal-host"}}
            }
        },
    )
    with pytest.raises(ManifestError) as error:
        migrate()
    assert error.value.issue.code == "UnsupportedLegacyDelivery"
    assert "internal-host" not in str(error.value)


def test_explicit_connector_references_and_scope_survive_legacy_mapping():
    definition = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "repo", "scope": {"kind": "Account", "name": "personal"}},
        "spec": {
            "driver": "repository",
            "config": {"url": "https://example.com/code.git"},
            "credentials": {"token": {"secretRef": {"name": "git", "key": "token"}}},
        },
    }
    result = migrate(
        connectors={"code": {"ref": {"name": "repo"}}}, definitions=[definition]
    )
    assert result["documents"][-1]["spec"]["execution"]["connectors"] == {
        "code": {"ref": {"name": "repo"}}
    }
    credential = result["resolved"][-1]["spec"]["execution"]["connectors"]["code"][
        "inline"
    ]["credentials"]["token"]
    assert credential == {
        "secretRef": {
            "name": "git",
            "key": "token",
            "scope": {"kind": "Account", "name": "personal"},
        }
    }
    assert "credentialDelivery" in result["pendingChecks"]
