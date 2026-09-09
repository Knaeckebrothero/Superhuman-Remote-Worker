"""Persistent workspace identities and per-attachment SSH authority."""

import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from orchestrator.services.generic_harness_runtime import (
    GenericBindings,
    GenericBoundFile,
)
from orchestrator.services.manifest_workspace_runtime import (
    ManifestWorkspaceError,
    ManifestWorkspaceRuntime,
    WorkspaceRuntimeIdentity,
    WorkspaceSSHMaterial,
    build_workspace_launch,
    generate_workspace_ssh_material,
    workspace_harness_bindings,
)
from tests.test_generic_harness_runtime import MemoryKubernetes


IDENTITY = WorkspaceRuntimeIdentity(
    "22222222-2222-4222-8222-222222222222", 1, "11111111-1111-4111-8111-111111111111"
)


@pytest.fixture
def ssh():
    return generate_workspace_ssh_material()


def plan(ssh, *, identity=IDENTITY, run_initialize=True, **recipe):
    return build_workspace_launch(
        identity,
        {"backend": "sandbox", **recipe},
        ssh,
        namespace="srw",
        default_image="workspace:fixture",
        initialize=run_initialize,
        storage_class_name="local-path",
    )


class WorkspaceKubernetes(MemoryKubernetes):
    def __init__(self):
        super().__init__()
        for action in ("create", "read", "delete", "patch"):
            setattr(
                self.core,
                f"{action}_namespaced_persistent_volume_claim",
                self.operation(
                    action, "PersistentVolumeClaim", "V1PersistentVolumeClaim"
                ),
            )

        def list_pods(*, namespace, label_selector, _request_timeout=None):
            assert _request_timeout is not None
            key, value = label_selector.split("=", 1)
            return SimpleNamespace(
                items=[
                    item
                    for (kind, ns, _), item in self.objects.items()
                    if kind == "Pod"
                    and ns == namespace
                    and item["metadata"]["labels"].get(key) == value
                ]
            )

        self.core.list_namespaced_pod = list_pods

    @property
    def pod(self):
        return self.objects[("Pod", "srw", IDENTITY.pod_name)]

    def running(self, *, terminated=False, exit_code=0, ready=False):
        super().running(terminated=terminated, exit_code=exit_code, ready=ready)
        self.pod["status"]["containerStatuses"][0]["name"] = "workspace"
        self.pod["status"]["podIP"] = "10.42.1.9"

    def init_status(self, *, exit_code=0, number=0):
        return {
            "name": f"initialize-{number}",
            "image": "workspace:fixture",
            "imageID": "containerd://sha256:init-digest",
            "ready": False,
            "restartCount": 0,
            "state": {
                "terminated": {
                    "exitCode": exit_code,
                    "reason": "Completed" if exit_code == 0 else "Error",
                }
            },
        }

    def runtime(self):
        return ManifestWorkspaceRuntime(self.core, self.networking, namespace="srw")


def test_keys_are_unique_per_attachment_and_safe_to_store_only_as_encrypted_payload(
    ssh,
):
    second = generate_workspace_ssh_material()
    assert second.client_public_key != ssh.client_public_key
    assert second.host_public_key != ssh.host_public_key
    assert WorkspaceSSHMaterial.from_json(ssh.to_json()) == ssh
    assert ssh.client_private_key not in repr(ssh)
    assert ssh.host_private_key not in repr(ssh)
    mismatched = json.loads(ssh.to_json())
    mismatched["host_public_key"] = second.host_public_key
    with pytest.raises(ValueError, match="matching Ed25519"):
        WorkspaceSSHMaterial.from_json(json.dumps(mismatched))


def test_workspace_mounts_its_own_volume_and_only_its_scoped_ssh_material(
    ssh, monkeypatch
):
    monkeypatch.setenv("WORKSPACE_SSH_SECRET", "shared-key-must-not-be-used")
    launch = plan(
        ssh, retention="Retain", resources={"cpu": 2, "memory": "3Gi", "storage": "8Gi"}
    )
    pod = launch.runtime.pod
    container = pod["spec"]["containers"][0]
    volumes = {volume["name"]: volume for volume in pod["spec"]["volumes"]}
    assert (
        volumes["workspace-data"]["persistentVolumeClaim"]["claimName"]
        == IDENTITY.pvc_name
    )
    assert volumes["workspace-pubkey"]["secret"]["secretName"] == IDENTITY.pod_name
    assert volumes["workspace-hostkey"]["secret"]["secretName"] == IDENTITY.pod_name
    assert "shared-key" not in json.dumps(pod)
    assert "envFrom" not in container
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["restartPolicy"] == "Never"
    assert container["resources"]["limits"]["cpu"] == "2"
    assert container["resources"]["limits"]["memory"] == "3Gi"
    assert launch.volume["spec"]["resources"]["requests"]["storage"] == "8Gi"
    assert launch.volume["spec"]["storageClassName"] == "local-path"
    decoded = {
        key: base64.b64decode(value).decode()
        for key, value in launch.runtime.delivery_secret["data"].items()
    }
    assert decoded["ssh-publickey"].strip() == ssh.client_public_key
    assert decoded["ssh-host-private"] == ssh.host_private_key
    assert ssh.client_private_key not in decoded.values()
    assert "user-ca.pub" not in decoded
    assert "app.kubernetes.io/component" not in pod["metadata"]["labels"]


def test_new_attachment_keeps_volume_identity_but_rotates_pod_and_credentials(ssh):
    later_identity = WorkspaceRuntimeIdentity(
        IDENTITY.instance_id, 2, "33333333-3333-4333-8333-333333333333"
    )
    first = plan(ssh)
    second = plan(
        generate_workspace_ssh_material(), identity=later_identity, run_initialize=False
    )
    assert first.volume == second.volume
    assert (
        first.runtime.pod["metadata"]["name"] != second.runtime.pod["metadata"]["name"]
    )
    assert (
        first.runtime.delivery_secret["data"] != second.runtime.delivery_secret["data"]
    )
    assert (
        first.runtime.pod["metadata"]["labels"]["srw.io/execution-id"]
        != second.runtime.pod["metadata"]["labels"]["srw.io/execution-id"]
    )


def test_custom_image_pull_policy_and_initializers_run_on_persistent_home_as_user(ssh):
    recipe = {
        "environment": {
            "image": "custom-workspace:version",
            "pullPolicy": "Always",
            "cache": "Reuse",
        },
        "initialize": [
            {"command": ["/bin/seed", "literal;$(do-not-execute)"]},
            {"command": ["/bin/check", "data"]},
        ],
    }
    launch = plan(ssh, **recipe)
    container = launch.runtime.pod["spec"]["containers"][0]
    assert container["image"] == "custom-workspace:version"
    assert container["imagePullPolicy"] == "Always"
    initializers = launch.runtime.pod["spec"]["initContainers"]
    assert len(initializers) == 2
    for initializer in initializers:
        command = initializer["command"]
        assert command[:2] == ["/bin/sh", "-c"]
        # The authored argv is shell-quoted inside su's user command, never in
        # the root setup segment. Parse both quoting layers to check the argv.
        user_line = command[2].splitlines()[-1]
        user_argv = shlex.split(user_line)
        assert user_argv[:6] == ["exec", "su", "-s", "/bin/sh", "agent-host", "-c"]
        assert "cd /home/agent-host/workspace && exec " in user_argv[6]
        assert {mount["name"] for mount in initializer["volumeMounts"]} == {
            "workspace-data"
        }
    assert (
        shlex.split(
            shlex.split(initializers[0]["command"][2].splitlines()[-1])[6].split(
                "&& exec ", 1
            )[1]
        )
        == recipe["initialize"][0]["command"]
    )
    resumed = plan(ssh, run_initialize=False, **recipe)
    assert "initContainers" not in resumed.runtime.pod["spec"]


@pytest.mark.parametrize(
    "recipe",
    [
        {"backend": "vm"},
        {"backend": "virtual"},
        {"environment": {"image": "image", "prepare": [{"command": ["setup"]}]}},
        {"environment": {"image": "image", "cache": "Rebuild"}},
        {"network": {"profileRef": {"name": "unresolved"}}},
    ],
)
def test_unsupported_workspace_capabilities_are_rejected_before_effects(ssh, recipe):
    with pytest.raises(ValueError):
        plan(ssh, **recipe)


def test_connector_cannot_replace_ssh_identity_projection(ssh):
    with pytest.raises(ValueError, match="identity mounts"):
        build_workspace_launch(
            IDENTITY,
            {"backend": "sandbox"},
            ssh,
            namespace="srw",
            default_image="workspace",
            initialize=False,
            bindings=GenericBindings(
                files=(
                    GenericBoundFile(
                        "/tmp/ssh-hostkey/ssh_host_ed25519_key", "replacement"
                    ),
                )
            ),
        )


def test_harness_binding_pins_host_identity_and_contains_no_host_private_key(ssh):
    binding = workspace_harness_bindings(
        IDENTITY, ssh, pod_ip="10.42.1.9", namespace="workspaces"
    )
    files = {item.path: item.content for item in binding.files}
    assert files["/run/srw-workspace/id_ed25519"] == ssh.client_private_key
    assert ssh.host_private_key not in files.values()
    assert (
        files["/run/srw-workspace/known_hosts"]
        == f"[10.42.1.9]:30022 {ssh.host_public_key}\n"
    )
    assert binding.descriptor["workspace"]["uid"] == IDENTITY.instance_id
    assert binding.descriptor["workspace"]["root"] == "/home/agent-host/workspace"
    peer = binding.egress[0]["to"][0]
    assert peer["podSelector"]["matchLabels"] == IDENTITY.labels
    assert (
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        == "workspaces"
    )
    assert binding.egress[0]["ports"] == [{"port": 30022, "protocol": "TCP"}]


def test_optional_openssh_helper_uses_private_permissions_and_cleans_temporary_key(
    ssh, tmp_path
):
    bindings = workspace_harness_bindings(IDENTITY, ssh, pod_ip="10.42.1.9")
    for item in bindings.files:
        target = tmp_path / Path(item.path).name
        target.write_text(item.content.replace("/run/srw-workspace", str(tmp_path)))
        target.chmod(item.mode)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        '#!/bin/sh\nset -eu\nwhile [ "$1" != -i ]; do shift; done\nshift\nstat -c \'%a\' "$1"\nprintf \'%s\\n\' "$1"\n'
    )
    fake_ssh.chmod(0o755)
    completed = subprocess.run(
        [str(tmp_path / "ssh"), "true"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(tmp_path),
        },
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    mode, temporary = completed.stdout.splitlines()
    assert mode == "600"
    assert not Path(temporary).exists()


@pytest.mark.asyncio
async def test_retained_missing_or_replaced_volume_is_never_silently_recreated(ssh):
    kube = WorkspaceKubernetes()
    runtime = kube.runtime()
    launch = plan(ssh)
    uid = await runtime.ensure_volume(launch)
    assert await runtime.ensure_volume(launch, expected_pvc_uid=uid) == uid
    volume = kube.objects[("PersistentVolumeClaim", "srw", IDENTITY.pvc_name)]
    volume["metadata"]["uid"] = "other-volume"
    with pytest.raises(ManifestWorkspaceError, match="identity"):
        await runtime.ensure_volume(launch, expected_pvc_uid=uid)
    del kube.objects[("PersistentVolumeClaim", "srw", IDENTITY.pvc_name)]
    calls = len(kube.calls)
    with pytest.raises(ManifestWorkspaceError, match="replacement is forbidden"):
        await runtime.ensure_volume(launch, expected_pvc_uid=uid)
    assert not any(action == "create" for action, *_ in kube.calls[calls:])


@pytest.mark.asyncio
async def test_workspace_initialization_requires_each_completed_initializer(ssh):
    kube = WorkspaceKubernetes()
    runtime = kube.runtime()
    launch = plan(ssh, initialize=[{"command": ["one"]}, {"command": ["two"]}])
    await runtime.ensure_volume(launch)
    observation = await runtime.launch(launch)
    assert observation.initialization_succeeded is False
    kube.pod["status"]["initContainerStatuses"] = [kube.init_status(number=0)]
    observation = await runtime.observe(IDENTITY, expected_pod_uid=observation.pod_uid)
    assert observation.initialization_succeeded is False
    kube.running(ready=True)
    kube.pod["status"]["initContainerStatuses"] = [
        kube.init_status(number=0),
        kube.init_status(number=1),
    ]
    observation = await runtime.observe(IDENTITY, expected_pod_uid=observation.pod_uid)
    assert observation.initialization_succeeded is True
    assert observation.pod_ip == "10.42.1.9"
    assert observation.readiness is True


@pytest.mark.asyncio
async def test_retire_fences_processes_but_preserves_the_exact_workspace_volume(ssh):
    kube = WorkspaceKubernetes()
    runtime = kube.runtime()
    launch = plan(ssh)
    volume_uid = await runtime.ensure_volume(launch)
    observation = await runtime.launch(launch)
    kube.running()
    assert await runtime.retire(IDENTITY, expected_pod_uid=observation.pod_uid) is False
    assert await runtime.delete_volume(IDENTITY, expected_pvc_uid=volume_uid) is False
    kube.running(terminated=True)
    assert await runtime.retire(IDENTITY, expected_pod_uid=observation.pod_uid) is True
    assert set(kind for kind, _, _ in kube.objects) == {"PersistentVolumeClaim"}
    assert (
        await runtime.ensure_volume(launch, expected_pvc_uid=volume_uid) == volume_uid
    )
    assert (
        await runtime.delete_volume(IDENTITY, expected_pvc_uid="foreign-uid") is False
    )
    assert await runtime.delete_volume(IDENTITY, expected_pvc_uid=volume_uid) is True
    assert kube.objects == {}


@pytest.mark.asyncio
async def test_failed_nonrestarting_init_can_retire_without_starting_workspace(ssh):
    kube = WorkspaceKubernetes()
    runtime = kube.runtime()
    launch = plan(ssh, initialize=[{"command": ["one"]}, {"command": ["two"]}])
    await runtime.ensure_volume(launch)
    observation = await runtime.launch(launch)
    kube.pod["status"] = {
        "phase": "Failed",
        "initContainerStatuses": [kube.init_status(exit_code=3, number=0)],
    }
    observation = await runtime.observe(IDENTITY, expected_pod_uid=observation.pod_uid)
    assert observation.initialization_succeeded is False
    assert observation.containers_terminal is True
    assert await runtime.retire(IDENTITY, expected_pod_uid=observation.pod_uid) is True
    assert set(kind for kind, _, _ in kube.objects) == {"PersistentVolumeClaim"}


@pytest.mark.asyncio
async def test_missing_running_or_restarting_init_evidence_blocks_cleanup(ssh):
    kube = WorkspaceKubernetes()
    runtime = kube.runtime()
    launch = plan(ssh, initialize=[{"command": ["one"]}, {"command": ["two"]}])
    await runtime.ensure_volume(launch)
    observation = await runtime.launch(launch)
    for statuses in (
        [],
        [kube.init_status(exit_code=0, number=0)],
        [
            kube.init_status(exit_code=3, number=0),
            {
                "name": "initialize-1",
                "image": "i",
                "imageID": "i",
                "ready": False,
                "restartCount": 0,
                "state": {"running": {}},
            },
        ],
    ):
        kube.pod["status"] = {"phase": "Failed", "initContainerStatuses": statuses}
        checked = await runtime.observe(IDENTITY, expected_pod_uid=observation.pod_uid)
        assert checked.containers_terminal is False
        assert (
            await runtime.cleanup(IDENTITY, expected_pod_uid=observation.pod_uid)
            is False
        )
    kube.pod["status"] = {
        "phase": "Failed",
        "initContainerStatuses": [kube.init_status(exit_code=3)],
    }
    kube.pod["spec"]["initContainers"][0]["restartPolicy"] = "Always"
    checked = await runtime.observe(IDENTITY, expected_pod_uid=observation.pod_uid)
    assert checked.containers_terminal is False


@pytest.mark.parametrize("complete", [True, False])
def test_entrypoint_installs_pinned_host_key_and_rejects_partial_identity(
    ssh, tmp_path, complete
):
    entrypoint = (
        Path(__file__).resolve().parents[1] / "docker/workspace-entrypoint.sh"
    ).read_text()
    # Execute the actual identity-install branch with temporary paths and the
    # invoking user's ownership. Container root ownership is verified on k3d.
    branch = entrypoint.split("if [ -d /tmp/ssh-hostkey ]; then", 1)[1].split(
        'if [ ! -s "$HOST_KEY_DIR/ssh_host_ed25519_key" ]; then', 1
    )[0]
    source = tmp_path / "secret"
    destination = tmp_path / "system"
    source.mkdir()
    destination.mkdir()
    (source / "ssh_host_ed25519_key").write_text(ssh.host_private_key)
    if complete:
        (source / "ssh_host_ed25519_key.pub").write_text(ssh.host_public_key + "\n")
    script = (
        f"set -e\nHOST_KEY_DIR={shlex.quote(str(destination))}\nif [ -d /tmp/ssh-hostkey ]; then"
        + branch
    )
    script = script.replace("/tmp/ssh-hostkey", str(source)).replace(
        "install -o root -g root", "install"
    )
    checked = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert checked.returncode == (0 if complete else 78), checked.stderr
    if complete:
        copied = destination / "ssh_host_ed25519_key"
        assert copied.read_text() == ssh.host_private_key
        assert copied.stat().st_mode & 0o777 == 0o600
    else:
        assert not (destination / "ssh_host_ed25519_key").exists()
