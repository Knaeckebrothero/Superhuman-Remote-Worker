#!/usr/bin/env python3
"""Exercise the production preparation firewall in a disposable Kubernetes namespace.

No existing workload or policy is changed. The first round intentionally has no
NetworkPolicy, proving that the init container fences the builder independently
of CNI startup timing. The second round adds the chart's online policy. A failed
init must leave the ordinary builder unstarted. Results use Pod termination
messages so the gate does not require kubelet log/exec access on every node.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
import yaml

from shared.workspace_preparation_network import DEFAULT_BLOCKED_CIDRS
from vm_controller.preparation_manifests import builder_pod

REPO = Path(__file__).resolve().parents[1]
PROBE = """import concurrent.futures,json,socket,time
targets=TARGETS
ipv6_listener=None
try:
 ipv6_listener=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
 ipv6_listener.bind(('::1',8443));ipv6_listener.listen(1)
 targets['private_ipv6_loopback']=('::1',8443)
except OSError:
 if ipv6_listener:ipv6_listener.close()
 ipv6_listener=None
def attempt(item):
 label,(host,port)=item
 try:
  s=socket.create_connection((host,port),2);s.close();return label,True
 except OSError:return label,False
start=time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
 result=dict(pool.map(attempt,targets.items()))
result['elapsedSeconds']=round(time.monotonic()-start,4)
result['ipv6LoopbackAvailable']=ipv6_listener is not None
open('/dev/termination-log','w').write(json.dumps(result))
"""


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(args):
    kube = config.new_client_from_config(
        config_file=args.kubeconfig, context=args.context
    )
    core, networking = client.CoreV1Api(kube), client.NetworkingV1Api(kube)
    ns = "srw-preparation-firewall-" + uuid4().hex[:8]
    nodes = args.node or sorted(
        n.metadata.name
        for n in core.list_node().items
        if any(
            c.type == "Ready" and c.status == "True" for c in n.status.conditions or []
        )
        and n.metadata.labels.get("kubernetes.io/arch") == "amd64"
    )
    if not nodes:
        raise RuntimeError("No eligible nodes selected.")
    created = core.create_namespace(
        {"metadata": {"name": ns, "labels": {"srw.io/test-owner": ns}}}
    )
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "context": args.context,
        "namespace": ns,
        "namespaceUID": created.metadata.uid,
        "image": args.image,
        "nodes": nodes,
        "probes": [],
        "passed": False,
        "cleanup": False,
        "sources": {
            str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                REPO / "src/shared/workspace_preparation_network.py",
                REPO / "src/vm_controller/preparation_firewall.py",
                REPO / "src/vm_controller/preparation_manifests.py",
            )
        },
    }
    save(args.output, report)

    def wait(name, *, terminal=True):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            pod = core.read_namespaced_pod(name, ns)
            if terminal and pod.status.phase in {"Succeeded", "Failed"}:
                return pod
            if not terminal and any(
                c.type == "Ready" and c.status == "True"
                for c in pod.status.conditions or []
            ):
                return pod
            time.sleep(1)
        raise RuntimeError("Probe exceeded its Pod startup budget.")

    def body(name, node, command, *, online=None, failed_init=False):
        profile = (
            None
            if online is None
            else {
                "version": 1,
                "networkEnabled": online,
                "blockedCidrs": list(DEFAULT_BLOCKED_CIDRS),
            }
        )
        pod = builder_pod(
            namespace=ns,
            name=name,
            uid=str(uuid4()),
            disk="unused",
            input_name="unused",
            image=args.image,
            timeout=args.timeout + 60,
            pod_firewall=profile,
        )
        spec = pod["spec"]
        spec["nodeName"] = node
        # Probes need no guest disk or authored request. Preserve the actual
        # builder/init security contexts, image and firewall invocation.
        spec["volumes"] = [
            v for v in spec["volumes"] if v["name"] == "firewall-runtime"
        ]
        container = spec["containers"][0]
        container["command"], container["volumeMounts"] = command, []
        container["resources"] = {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        }
        if failed_init:
            spec["initContainers"][0]["securityContext"]["capabilities"] = {
                "drop": ["ALL"]
            }
        if online is None:
            pod["metadata"]["labels"].pop("app.kubernetes.io/component")
        pod["metadata"]["labels"]["srw.io/test-owner"] = ns
        return pod

    def probe(node, wave, online, targets, *, failed_init=False):
        name = "probe-" + uuid4().hex[:12]
        command = ["python", "-c", PROBE.replace("TARGETS", repr(targets))]
        p = core.create_namespaced_pod(
            ns, body(name, node, command, online=online, failed_init=failed_init)
        )
        final = wait(name)
        row = {
            "name": name,
            "uid": p.metadata.uid,
            "node": node,
            "wave": wave,
            "online": online,
            "failedInit": failed_init,
            "phase": final.status.phase,
            "init": [
                kube.sanitize_for_serialization(s)
                for s in final.status.init_container_statuses or []
            ],
            "containers": [
                kube.sanitize_for_serialization(s)
                for s in final.status.container_statuses or []
            ],
        }
        report["probes"].append(row)
        if failed_init:
            (init,) = final.status.init_container_statuses or []
            (regular,) = final.status.container_statuses or []
            assert (
                final.status.phase == "Failed" and init.state.terminated.exit_code != 0
            )
            assert not regular.container_id and regular.restart_count == 0
            assert (
                regular.state.waiting is not None
                and regular.state.waiting.reason == "PodInitializing"
            )
        else:
            assert final.status.phase == "Succeeded"
            (regular,) = final.status.container_statuses
            row["result"] = json.loads(regular.state.terminated.message)
            expected = {
                key: online is None or online and key.startswith("public_")
                for key in targets
            }
            assert {k: row["result"][k] for k in targets} == expected
            if row["result"]["ipv6LoopbackAvailable"]:
                assert row["result"]["private_ipv6_loopback"] is (online is None)
            if online is not None:
                (init,) = final.status.init_container_statuses
                assert init.state.terminated.exit_code == 0
                assert json.loads(init.state.terminated.message) == {
                    "version": 1,
                    "phase": "Installed",
                    "networkEnabled": online,
                }
        return row

    try:
        server = body("server", nodes[0], ["python", "-m", "http.server", "8080"])
        server["metadata"]["labels"]["srw.io/net-server"] = "true"
        server["spec"]["activeDeadlineSeconds"] = 3600
        core.create_namespaced_pod(ns, server)
        service = core.create_namespaced_service(
            ns,
            {
                "metadata": {"name": "server"},
                "spec": {
                    "selector": {"srw.io/net-server": "true"},
                    "ports": [{"port": 443, "targetPort": 8080}],
                },
            },
        )
        ready = wait("server", terminal=False)
        targets = {
            "public_https": ("1.1.1.1", 443),
            "public_http": ("1.1.1.1", 80),
            "public_dns": ("deb.debian.org", 443),
            "private_service": (service.spec.cluster_ip, 443),
            "private_pod": (ready.status.pod_ip, 8080),
        }
        for mode, online in (
            ("control", None),
            ("offline-no-cni", False),
            ("online-no-cni", True),
        ):
            for wave in range(1 if online is None else args.waves):
                with ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as pool:
                    rows = list(
                        pool.map(
                            lambda n: probe(n, mode + "-" + str(wave), online, targets),
                            nodes,
                        )
                    )
                save(args.output, report)
                print(
                    json.dumps({"wave": mode + "-" + str(wave), "passed": len(rows)}),
                    flush=True,
                )
        # Compose the exact chart policy with the Pod firewall only after the
        # independent no-CNI test. The unique namespace contains no other policy.
        command = [
            "helm",
            "template",
            "firewall-gate",
            str(REPO / "helm"),
            "-n",
            ns,
            "-f",
            str(REPO / "helm/ci/test-values.yaml"),
        ]
        for value in (
            "vm.mode=same-cluster",
            "agent.tailscale.enabled=false",
            "vm.lifecycleAuthSecretName=fixture-auth",
            "vmController.persistentRootdisk.enabled=true",
            "vmController.preparation.enabled=true",
            "vmController.preparation.network.enabled=true",
            "vmController.preparation.network.enforcementVerified=true",
            "vmController.preparation.network.podFirewall=true",
        ):
            command += ["--set", value]
        rendered = subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout
        policy = next(
            d
            for d in yaml.safe_load_all(rendered)
            if d
            and d["kind"] == "NetworkPolicy"
            and d["metadata"]["name"].endswith("-workspace-preparer")
        )
        installed = networking.create_namespaced_network_policy(ns, policy)
        report["policyUID"] = installed.metadata.uid
        for wave in range(args.waves):
            with ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as pool:
                rows = list(
                    pool.map(
                        lambda n: probe(n, "online-chart-" + str(wave), True, targets),
                        nodes,
                    )
                )
            save(args.output, report)
            print(
                json.dumps({"wave": "online-chart-" + str(wave), "passed": len(rows)}),
                flush=True,
            )
        for node in nodes:
            probe(node, "init-denied", True, targets, failed_init=True)
        report["passed"] = True
    except Exception as exc:
        report["errorType"] = type(exc).__name__
        raise
    finally:
        core.delete_namespace(
            ns,
            body=client.V1DeleteOptions(
                preconditions=client.V1Preconditions(uid=created.metadata.uid)
            ),
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                core.read_namespace(ns)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                report["cleanup"] = True
                break
            time.sleep(1)
        save(args.output, report)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "cleanup": report["cleanup"],
                    "output": str(args.output),
                }
            ),
            flush=True,
        )
        kube.close()
    if not report["cleanup"]:
        raise RuntimeError("Owned test namespace cleanup did not complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context", required=True)
    parser.add_argument(
        "--image",
        required=True,
        help="Candidate preparer image; use a registry digest outside local k3d.",
    )
    parser.add_argument(
        "--node",
        action="append",
        help="Repeat to select nodes; defaults to every Ready amd64 node.",
    )
    parser.add_argument("--waves", type=int, default=3, choices=range(1, 21))
    parser.add_argument("--timeout", type=int, default=600, choices=range(60, 1801))
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
