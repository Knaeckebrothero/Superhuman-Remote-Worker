#!/usr/bin/env python3
import argparse
import json
import subprocess
import tempfile
import time
from uuid import uuid4
from pathlib import Path

args = argparse.ArgumentParser(
    description="Verify VM preparation network isolation from the first application packet in an owned namespace."
)
args.add_argument("--kubeconfig", required=True)
args.add_argument("--context", required=True)
args.add_argument(
    "--image", required=True, help="Reachable vm-preparer image containing Python"
)
args.add_argument("--output", type=Path)
args = args.parse_args()
NS = "srw-preparation-network-" + uuid4().hex[:8]
K = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", NS]
IMAGE = args.image


def k(*args):
    return subprocess.check_output(K + list(args), text=True)


def apply(doc):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
        json.dump(doc, f)
        f.flush()
        subprocess.run(K + ["apply", "-f", f.name], check=True, capture_output=True)


def pod(name, cmd, labels):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NS, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 107,
                "runAsGroup": 107,
            },
            "containers": [
                {
                    "name": "probe",
                    "image": IMAGE,
                    "command": cmd,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"memory": "128Mi"},
                    },
                }
            ],
        },
    }


def run_probe(name, labels, tests):
    code = "import socket,json\nr={}\n"
    for label, host, port in tests:
        code += f"try:\n s=socket.create_connection(({host!r},{port}),4);s.close();r[{label!r}]=True\nexcept OSError:r[{label!r}]=False\n"
    code += "print(json.dumps(r))"
    apply(pod(name, ["python", "-c", code], labels))
    for _ in range(60):
        data = json.loads(k("get", "pod", name, "-o", "json"))
        if data.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            break
        time.sleep(2)
    assert data["status"]["phase"] == "Succeeded", data["status"]["phase"]
    return json.loads(k("logs", name))


apply(
    {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": NS, "labels": {"srw.io/test-owner": NS}},
    }
)
try:
    apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "network-control", "namespace": NS},
            "spec": {
                "podSelector": {
                    "matchLabels": {"srw.io/test-component": "network-control"}
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [{}],
                "egress": [{}],
            },
        }
    )
    apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "offline-builders", "namespace": NS},
            "spec": {
                "podSelector": {
                    "matchLabels": {"app.kubernetes.io/component": "workspace-preparer"}
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }
    )
    apply(
        pod(
            "network-control-server",
            ["python", "-m", "http.server", "8080", "--bind", "0.0.0.0"],
            {"srw.io/test-component": "network-control"},
        )
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "network-control", "namespace": NS},
            "spec": {
                "selector": {"srw.io/test-component": "network-control"},
                "ports": [{"port": 443, "targetPort": 8080}],
            },
        }
    )
    k("wait", "pod/network-control-server", "--for=condition=Ready", "--timeout=120s")
    private = json.loads(k("get", "service", "network-control", "-o", "json"))["spec"][
        "clusterIP"
    ]
    k("rollout", "status", "deployment/coredns", "-n", "kube-system", "--timeout=120s")
    tests = [
        ("public_https", "1.1.1.1", 443),
        ("private_service", private, 443),
    ]
    control = run_probe(
        "network-control-client", {"srw.io/test-component": "network-control"}, tests
    )
    assert control["private_service"] and control["public_https"], control
    labels = {"app.kubernetes.io/component": "workspace-preparer"}
    offline = run_probe("network-offline-probe", labels, tests)
    assert not any(offline.values()), offline
    render = subprocess.check_output(
        [
            "helm",
            "template",
            "cache-test",
            "helm",
            "-f",
            "helm/ci/test-values.yaml",
            "--namespace",
            NS,
            "--set",
            "vm.mode=same-cluster",
            "--set",
            "agent.tailscale.enabled=false",
            "--set",
            "vm.lifecycleAuthSecretName=fixture-auth",
            "--set",
            "vmController.persistentRootdisk.enabled=true",
            "--set",
            "vmController.preparation.enabled=true",
            "--set",
            "vmController.preparation.network.enabled=true",
            "--set",
            "vmController.preparation.network.enforcementVerified=true",
        ],
        text=True,
    )
    import yaml

    policy = next(
        d
        for d in yaml.safe_load_all(render)
        if d
        and d["kind"] == "NetworkPolicy"
        and d["metadata"]["name"].endswith("-workspace-preparer")
    )
    apply(policy)
    online = run_probe("network-online-probe", labels, tests)
    assert online["public_https"] and not online["private_service"], online
    # DNS resolution is independently checked without opening the Kubernetes API.
    dns = run_probe(
        "network-dns-probe", labels, [("public_dns", "deb.debian.org", 443)]
    )
    assert dns["public_dns"], dns
    report = {
        "control": control,
        "offline": offline,
        "online": online,
        "dns": dns,
        "passed": True,
    }
    print(json.dumps(report), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2))
finally:
    k("delete", "namespace", NS, "--wait=true", "--timeout=120s")
