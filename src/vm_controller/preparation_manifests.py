"""Constrained, non-restarting builders for controller-owned disks."""

from shared.workspace_preparation import PREPARATION_LABEL, canonical
from shared.workspace_preparation_network import firewall_policy


def firewall_command(policy):
    return [
        "python",
        "-m",
        "vm_controller.preparation_firewall",
        "--policy",
        canonical(firewall_policy(policy)),
    ]


def firewall_init_container(image, policy):
    return {
        "name": "network-firewall",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": firewall_command(policy),
        "env": [
            {
                "name": "SRW_PREPARATION_POD_UID",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
            }
        ],
        "securityContext": {
            "runAsNonRoot": False,
            "runAsUser": 0,
            "runAsGroup": 0,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
        },
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "64Mi"},
        },
        "volumeMounts": [{"name": "firewall-runtime", "mountPath": "/run"}],
    }


def builder_pod(
    *,
    namespace,
    name,
    uid,
    disk,
    input_name,
    image,
    timeout,
    image_pull_secrets=(),
    pod_firewall=None,
):
    body = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                PREPARATION_LABEL: uid,
                "app.kubernetes.io/component": "workspace-preparer",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": timeout,
            "terminationGracePeriodSeconds": 20,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "nodeSelector": {
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 107,
                "runAsGroup": 107,
                "fsGroup": 107,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "imagePullSecrets": [{"name": name} for name in image_pull_secrets],
            "containers": [
                {
                    "name": "builder",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {
                            "cpu": "2",
                            "memory": "3Gi",
                            "ephemeral-storage": "1Gi",
                        },
                        "limits": {
                            "cpu": "2",
                            "memory": "4Gi",
                            "ephemeral-storage": "8Gi",
                        },
                    },
                    "volumeMounts": [
                        {"name": "disk", "mountPath": "/disk"},
                        {"name": "input", "mountPath": "/request", "readOnly": True},
                        {"name": "tmp", "mountPath": "/tmp"},
                        {"name": "tmp", "mountPath": "/var/tmp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "disk", "persistentVolumeClaim": {"claimName": disk}},
                {
                    "name": "input",
                    "configMap": {
                        "name": input_name,
                        "items": [{"key": "request.json", "path": "request.json"}],
                    },
                },
                {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}},
            ],
        },
    }
    if pod_firewall is not None:
        body["spec"]["initContainers"] = [firewall_init_container(image, pod_firewall)]
        body["spec"]["volumes"].append(
            {
                "name": "firewall-runtime",
                "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"},
            }
        )
    return body
