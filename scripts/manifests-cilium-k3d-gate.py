#!/usr/bin/env python3
"""Create a separate disposable Cilium k3d cluster and run native runtime gates.

The installed srw cluster and default kubeconfig are never changed. The existing
local test registry is temporarily attached to the new Docker network, then
detached. Test cluster, kubeconfig credentials and temporary files are removed.

Profile sources:
https://docs.cilium.io/en/stable/installation/k8s-install-helm/
https://github.com/cilium/cilium/blob/v1.18.13/Documentation/security/policy/intro.rst
"""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from uuid import uuid4

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART_VERSION = "1.18.13"
CHART_SHA256 = "7d39d95fa5528c31f33eb18d41b78da0ceace9d8c7a81334935cb817c075c32a"
NODE_IMAGE = "rancher/k3s:v1.31.5-k3s1"


class ProfileFailure(Exception):
    pass


def command(args, *, timeout=300):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise ProfileFailure(f"{args[0]} operation did not finish.") from None
    if result.returncode:
        raise ProfileFailure(f"{args[0]} operation failed (exit {result.returncode}).")
    return result.stdout


def announce(message):
    print(message, flush=True)


def run(*, service_only=False):
    name = "srw-native-gate-" + uuid4().hex[:8]
    context, node = "k3d-" + name, "k3d-" + name + "-server-0"
    registry = json.loads(command(["docker", "inspect", "srw-registry"]))[0]
    original_networks = set(registry["NetworkSettings"]["Networks"])
    if "k3d-srw" not in original_networks:
        raise ProfileFailure("The expected existing local SRW registry was not found.")
    default_config = Path.home() / ".kube/config"
    original_config = (
        hashlib.sha256(default_config.read_bytes()).hexdigest()
        if default_config.exists()
        else None
    )
    created, attached = False, False
    with tempfile.TemporaryDirectory(prefix="srw-cilium-native-gate-") as directory:
        temporary = Path(directory)
        kubeconfig, registry_config = (
            temporary / "kubeconfig",
            temporary / "registries.yaml",
        )
        registry_config.write_text(
            yaml.safe_dump(
                {
                    "mirrors": {
                        "srw-registry:5000": {"endpoint": ["http://srw-registry:5000"]}
                    }
                }
            )
        )
        chart = temporary / "cilium.tgz"
        with httpx.Client(trust_env=False, timeout=30) as source:
            response = source.get(f"https://helm.cilium.io/cilium-{CHART_VERSION}.tgz")
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != CHART_SHA256:
                raise ProfileFailure("The pinned Cilium chart digest did not match.")
            chart.write_bytes(response.content)
        try:
            announce(
                f"Creating separate disposable cluster {name}; existing srw cluster is unchanged."
            )
            created = True
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                api_port = listener.getsockname()[1]
            command(
                [
                    "k3d",
                    "cluster",
                    "create",
                    name,
                    "--servers",
                    "1",
                    "--agents",
                    "0",
                    "--image",
                    NODE_IMAGE,
                    "--api-port",
                    f"127.0.0.1:{api_port}",
                    "--kubeconfig-update-default=false",
                    "--kubeconfig-switch-context=false",
                    "--registry-config",
                    str(registry_config),
                    "--timeout",
                    "120s",
                    "--k3s-arg",
                    "--flannel-backend=none@server:*",
                    "--k3s-arg",
                    "--disable-network-policy@server:*",
                    "--k3s-arg",
                    "--disable=traefik,servicelb,metrics-server@server:*",
                    "--k3s-arg",
                    "--cluster-cidr=10.222.0.0/16@server:*",
                    "--k3s-arg",
                    "--service-cidr=10.223.0.0/16@server:*",
                ],
                timeout=150,
            )
            command(["docker", "network", "connect", context, "srw-registry"])
            attached = True
            kubeconfig.touch(mode=0o600)
            kubeconfig.write_text(command(["k3d", "kubeconfig", "get", name]))
            kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
            for _ in range(30):
                ready = subprocess.run(
                    kube + ["get", "--raw", "/readyz"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if ready.returncode == 0 and ready.stdout.strip() == "ok":
                    break
                time.sleep(1)
            else:
                raise ProfileFailure("The disposable cluster API did not become ready.")
            # Read only nonsecret CNI paths from this new node's generated config.
            config = tomllib.loads(
                command(
                    [
                        "docker",
                        "exec",
                        node,
                        "cat",
                        "/var/lib/rancher/k3s/agent/etc/containerd/config.toml",
                    ]
                )
            )
            cni = next(
                (
                    value["cni"]
                    for value in config["plugins"].values()
                    if isinstance(value, dict) and "cni" in value
                ),
                None,
            )
            values = {
                "operator": {"replicas": 1},
                "kubeProxyReplacement": False,
                "policyEnforcementMode": "always",
                "envoy": {"enabled": False},
                "l7Proxy": False,
                "hubble": {"enabled": False},
                "ipam": {"mode": "kubernetes"},
                "securityContext": {"privileged": True},
            }
            if cni:
                values["cni"] = {"binPath": cni["bin_dir"], "confPath": cni["conf_dir"]}
            values_file = temporary / "cilium-values.yaml"
            values_file.write_text(yaml.safe_dump(values))
            # Infrastructure in this disposable cluster needs API/DNS access
            # under Cilium's installation-wide always-enforce setting.
            infrastructure = temporary / "infrastructure-policy.json"
            infrastructure.write_text(
                json.dumps(
                    {
                        "apiVersion": "networking.k8s.io/v1",
                        "kind": "NetworkPolicy",
                        "metadata": {
                            "name": "gate-system-infrastructure",
                            "namespace": "kube-system",
                        },
                        "spec": {
                            "podSelector": {},
                            "policyTypes": ["Ingress", "Egress"],
                            "ingress": [{}],
                            "egress": [{}],
                        },
                    }
                )
            )
            command(kube + ["apply", "-f", str(infrastructure)])
            announce(
                f"Installing pinned Cilium {CHART_VERSION} with policyEnforcementMode=always."
            )
            command(
                [
                    "helm",
                    "--kubeconfig",
                    str(kubeconfig),
                    "--kube-context",
                    context,
                    "install",
                    "cilium",
                    str(chart),
                    "--namespace",
                    "kube-system",
                    "--values",
                    str(values_file),
                    "--wait",
                    "--timeout",
                    "8m",
                ],
                timeout=510,
            )
            # Helm's readiness wait may finish after the operator but before
            # the DaemonSet's cold image pull and init containers finish.
            command(
                kube
                + [
                    "rollout",
                    "status",
                    "daemonset/cilium",
                    "-n",
                    "kube-system",
                    "--timeout=360s",
                ],
                timeout=375,
            )
            announce("Waiting for the disposable Cilium node to become Ready.")
            try:
                command(
                    kube
                    + [
                        "wait",
                        "nodes",
                        "--all",
                        "--for=condition=Ready",
                        "--timeout=120s",
                    ],
                    timeout=135,
                )
            except ProfileFailure:
                # Only infrastructure states from this newly created cluster;
                # never Pod logs, ConfigMap data or credential-bearing bodies.
                observed = json.loads(command(kube + ["get", "nodes", "-o", "json"]))
                announce(
                    json.dumps(
                        {
                            "nodeReadiness": [
                                {
                                    "name": item["metadata"]["name"],
                                    "conditions": [
                                        {
                                            key: condition.get(key)
                                            for key in ("type", "status", "reason")
                                        }
                                        for condition in item["status"].get(
                                            "conditions", []
                                        )
                                    ],
                                }
                                for item in observed["items"]
                            ]
                        }
                    )
                )
                raise
            announce("Reading the installed Cilium policy mode and image identities.")
            config_map = json.loads(
                command(
                    kube
                    + [
                        "get",
                        "configmap",
                        "cilium-config",
                        "-n",
                        "kube-system",
                        "-o",
                        "json",
                    ]
                )
            )
            if config_map["data"].get("enable-policy") != "always":
                raise ProfileFailure(
                    "Cilium did not receive the always-enforce policy setting."
                )
            cilium_pods = json.loads(
                command(
                    kube
                    + [
                        "get",
                        "pods",
                        "-n",
                        "kube-system",
                        "-l",
                        "k8s-app=cilium",
                        "-o",
                        "json",
                    ]
                )
            )["items"]
            cilium_images = [
                status["imageID"]
                for pod in cilium_pods
                for status in pod["status"].get("containerStatuses", [])
            ]
            announce(
                json.dumps(
                    {
                        "cluster": name,
                        "ciliumVersion": CHART_VERSION,
                        "chartDigest": CHART_SHA256,
                        "ciliumImageIDs": cilium_images,
                        "values": values,
                    }
                )
            )
            base = [
                sys.executable,
                str(ROOT / "scripts/manifests-native-k3d-gate.py"),
                "--kube-context",
                context,
                "--kubeconfig",
                str(kubeconfig),
            ]
            gates = [
                ("cold-start isolation", base + ["--network-startup-samples", "12"])
            ]
            if not service_only:
                gates.append(("native SSH/workspace runtime", base))
            gates.append(
                (
                    "production manifest services and PostgreSQL",
                    [
                        sys.executable,
                        str(ROOT / "scripts/manifests-service-k3d-gate.py"),
                        "--kube-context",
                        context,
                        "--kubeconfig",
                        str(kubeconfig),
                    ],
                )
            )
            for label, command_line in gates:
                announce(f"Running {label} gate on the disposable Cilium cluster.")
                # The child script emits only sanitized evidence and cleans its
                # namespace, even on failure. Keep that evidence in this log.
                result = subprocess.run(command_line, cwd=ROOT, timeout=900)
                if result.returncode:
                    raise ProfileFailure(
                        f"The {label} gate failed on the disposable Cilium cluster."
                    )
            announce(
                json.dumps(
                    {
                        "result": "passed",
                        "cluster": name,
                        "profile": "Cilium always-enforce",
                        "nodeImage": NODE_IMAGE,
                        "ciliumVersion": CHART_VERSION,
                    }
                )
            )
        finally:
            if attached:
                command(["docker", "network", "disconnect", context, "srw-registry"])
            if created:
                announce(
                    f"Removing disposable cluster {name} and its credential files."
                )
                command(["k3d", "cluster", "delete", name], timeout=180)
            remaining = json.loads(command(["docker", "inspect", "srw-registry"]))[0]
            if set(remaining["NetworkSettings"]["Networks"]) != original_networks:
                raise ProfileFailure(
                    "The registry's original network attachments were not restored."
                )
            actual_config = (
                hashlib.sha256(default_config.read_bytes()).hexdigest()
                if default_config.exists()
                else None
            )
            if actual_config != original_config:
                raise ProfileFailure("The default kubeconfig changed during this test.")
    if any(
        cluster["name"] == name
        for cluster in json.loads(command(["k3d", "cluster", "list", "-o", "json"]))
    ):
        raise ProfileFailure("The disposable Cilium cluster was not removed.")
    if temporary.exists():
        raise ProfileFailure("The disposable kubeconfig directory was not removed.")
    announce(
        json.dumps(
            {
                "cleanup": {
                    "clusterRemoved": name,
                    "registryNetworksRestored": True,
                    "defaultKubeconfigUnchanged": True,
                    "temporaryCredentialsRemoved": True,
                }
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-only",
        action="store_true",
        help="Run cold-start isolation and production service integration; skip the separate low-level SSH/PVC adapter suite.",
    )
    args = parser.parse_args()
    try:
        run(service_only=args.service_only)
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, ProfileFailure)
            else f"Unexpected {type(exc).__name__}; no arbitrary API body logged."
        )
        announce(json.dumps({"result": "failed", "reason": reason}))
        raise SystemExit(1) from None
