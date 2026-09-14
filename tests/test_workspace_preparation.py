from copy import deepcopy
import json
from uuid import uuid4

import pytest

from shared.workspace_preparation import (
    builder_request,
    cache_key,
    image_reference,
    preparation_request,
    validate_request,
)
from shared.workspace_preparation_settings import PreparationSettings
from vm_controller.preparation_builder import guest_script
from vm_controller.preparation_manifests import builder_pod

BASE = "ghcr.io/example/base@sha256:" + "1" * 64
BUILDER = "ghcr.io/example/builder@sha256:" + "2" * 64


def request(**changes):
    return preparation_request(
        {
            "image": BASE,
            "prepare": [{"command": ["mkdir", "-p", "/opt/example"]}],
            **changes,
        },
        scope_kind="Project",
        scope_uid="11111111-1111-4111-8111-111111111111",
        allocation_id=str(uuid4()),
        owner_kind="job",
    )


def test_reuse_shares_only_matching_scope_base_builder_and_ordered_commands():
    first, second = request(), request()

    def key(value, base=BASE, builder=BUILDER):
        return cache_key(value, base_image=base, builder_image=builder)

    assert key(first) == key(second)
    assert key(first, base=BASE.replace("1", "3")) != key(first)
    assert key(first, builder=BUILDER.replace("2" * 64, "3" * 64)) != key(first)
    scoped = preparation_request(
        {"image": BASE, "prepare": first["steps"]},
        scope_kind="Project",
        scope_uid=str(uuid4()),
        allocation_id=first["allocationId"],
        owner_kind="job",
    )
    assert key(scoped) != key(first)
    assert key(request(prepare=[{"command": ["mkdir", "/opt/different"]}])) != key(
        first
    )


def test_rebuild_is_per_allocation_and_retry_is_stable():
    first, second = request(cache="Rebuild"), request(cache="Rebuild")

    def key(value):
        return cache_key(value, base_image=BASE, builder_image=BUILDER)

    assert key(first) == key(deepcopy(first))
    assert key(first) != key(second)
    assert key(first) != key(request())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["steps"].append({"command": ["touch", "/different"]}),
        lambda r: r.update(scope={"kind": "Account", "uid": str(uuid4())}),
        lambda r: r.update(allocationId=str(uuid4())),
        lambda r: r.update(version=True),
        lambda r: r.update(extra="ignored"),
    ],
)
def test_modified_transport_request_is_rejected(mutation):
    value = request()
    mutation(value)
    with pytest.raises(ValueError):
        validate_request(value)


@pytest.mark.parametrize(
    "image",
    [
        "https://registry.example/a",
        "user:password@registry.example/a",
        "registry.example/a?token=x",
        "registry.example/../x",
        "registry.example/a\nx",
        "registry.example/a@sha1:123",
    ],
)
def test_image_reference_rejects_url_and_path_injection(image):
    with pytest.raises(ValueError):
        image_reference(image)


def test_argv_shell_characters_remain_literal_in_guest_script(tmp_path):
    import subprocess
    import sys

    target = tmp_path / "arguments.json"
    arguments = ["$(touch forbidden)", "a'b", "line\nbreak", "`id`", "$HOME", ""]
    script = guest_script(
        [
            {
                "command": [
                    sys.executable,
                    "-c",
                    "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))",
                    str(target),
                    *arguments,
                ]
            }
        ]
    )
    subprocess.run(["/bin/sh"], input=script, text=True, check=True)
    assert json.loads(target.read_text()) == arguments
    assert not (tmp_path / "forbidden").exists()


def test_builder_has_no_kubernetes_token_host_device_or_privilege():
    manifest = builder_pod(
        namespace="test",
        name="builder",
        uid=str(uuid4()),
        disk="new-disk",
        input_name="input",
        image=BUILDER,
        timeout=300,
    )
    spec = manifest["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    assert spec["securityContext"]["runAsNonRoot"] is True
    container = spec["containers"][0]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert all("hostPath" not in v and "secret" not in v for v in spec["volumes"])
    assert "serviceAccountName" not in spec
    assert container["image"] == BUILDER


def test_online_preparation_requires_operator_isolation_verification(monkeypatch):
    monkeypatch.setenv("VM_PREPARATION_ENABLED", "true")
    monkeypatch.setenv("VM_PREPARATION_IMAGE", BUILDER)
    monkeypatch.setenv("VM_PREPARATION_NETWORK_ENABLED", "true")
    monkeypatch.delenv("VM_PREPARATION_NETWORK_ISOLATION_VERIFIED", raising=False)
    with pytest.raises(ValueError, match="verified"):
        PreparationSettings.from_environment()
    monkeypatch.setenv("VM_PREPARATION_NETWORK_ISOLATION_VERIFIED", "true")
    monkeypatch.setenv("VM_PREPARATION_NETWORK_POLICY_REVISION", "a" * 64)
    assert PreparationSettings.from_environment().network_enabled


def test_builder_input_rejects_extra_host_parameters():
    value = {
        "version": 1,
        "buildUid": str(uuid4()),
        "pvcUid": str(uuid4()),
        "cacheKey": "a" * 64,
        "steps": [],
        "networkEnabled": False,
        "diskPath": "/etc/shadow",
    }
    with pytest.raises(ValueError):
        builder_request(value)
