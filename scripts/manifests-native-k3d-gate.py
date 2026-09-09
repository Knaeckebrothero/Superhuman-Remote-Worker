#!/usr/bin/env python3
"""Run native harness/workspace acceptance in a disposable k3d-srw namespace.

This exercises the real Kubernetes adapters, SSH, PVCs and NetworkPolicies. A
temporary SQLite ledger enforces publication of identities before dependent
effects and survives reconstruction of the gate driver. It is deliberately not
a substitute for tests of the production admission/authorization/Postgres APIs.

The script publishes existing local images into the verified local registry;
missing upstream harness images are pulled first. It never deploys or writes to
the installed SRW namespace. Keys stay in memory and namespace-local Secrets.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from kubernetes import client, config

from orchestrator.services.generic_harness_runtime import (
    GenericAttemptIdentity,
    GenericBindings,
    GenericBoundFile,
    GenericHarnessRuntime,
    GenericRuntimePolicy,
    build_generic_launch,
)
from orchestrator.services.manifest_workspace_runtime import (
    ManifestWorkspaceError,
    ManifestWorkspaceRuntime,
    WorkspaceRuntimeIdentity,
    build_workspace_launch,
    generate_workspace_ssh_material,
    workspace_harness_bindings,
)


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "k3d-srw"
POLICY = GenericRuntimePolicy(
    cpu_request=0.05,
    cpu_limit=0.5,
    memory_request="64Mi",
    memory_limit="256Mi",
    termination_grace_seconds=2,
    # The stock Git image has a nobody account but no UID-1000 passwd entry.
    # OpenSSH requires a real local account even when only its client runs.
    run_as_user=65534,
    run_as_group=65534,
)
NAMESPACE_PREFIX = "srw-native-gate-"


class GateFailure(Exception):
    """An intentionally sanitized diagnostic, never an arbitrary API body."""


def require(condition, message):
    if not condition:
        raise GateFailure(message)


def announce(message):
    print(message, flush=True)


def command(args, *, timeout=300):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise GateFailure(f"{args[0]} inspection/publication did not finish.") from None
    require(result.returncode == 0, f"{args[0]} inspection/publication failed.")
    return result.stdout


def verify_cluster(*, context=CONTEXT, kubeconfig=None):
    require(
        re.fullmatch(r"k3d-srw(?:-native-gate-[0-9a-f]{8})?", context),
        "Only the local SRW context or a disposable native-gate context is accepted.",
    )
    api_client = config.new_client_from_config(context=context, config_file=kubeconfig)
    require(
        urlparse(api_client.configuration.host).hostname
        in {"localhost", "127.0.0.1", "0.0.0.0", "::1"},
        "The explicit k3d-srw context does not point to a local API endpoint.",
    )
    core = client.CoreV1Api(api_client)
    nodes = core.list_node(_request_timeout=15).items
    require(
        nodes and all(node.metadata.name.startswith(context + "-") for node in nodes),
        "The explicit context does not contain the expected local k3d nodes.",
    )
    registry = json.loads(command(["docker", "inspect", "srw-registry"]))[0]
    require(
        registry["State"]["Running"]
        and context in registry["NetworkSettings"]["Networks"]
        and any(
            port["HostPort"] == "5005"
            for port in registry["NetworkSettings"]["Ports"].get("5000/tcp", [])
        ),
        "The expected local registry is not running on k3d-srw/localhost:5005.",
    )
    with httpx.Client(trust_env=False, timeout=10) as registry_client:
        require(
            registry_client.get("http://localhost:5005/v2/").status_code == 200,
            "The local registry API is unavailable.",
        )
    return api_client, [node.metadata.name for node in nodes]


def publish_images(args):
    images, identities = {}, {}
    for name, local in (
        ("workspace", args.workspace_image),
        ("busybox", args.busybox_image),
        ("ssh", args.ssh_image),
        ("python", args.python_image),
    ):
        announce(f"Preparing {name} image in the local test registry.")
        found = subprocess.run(
            ["docker", "image", "inspect", local], capture_output=True, timeout=15
        )
        if found.returncode:
            require(name != "workspace", "Build the local workspace gate image first.")
            command(["docker", "pull", local])
        info = json.loads(command(["docker", "image", "inspect", local]))[0]
        if name == "workspace":
            actual = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--entrypoint",
                    "sha256sum",
                    local,
                    "/usr/local/bin/entrypoint.sh",
                ]
            ).split()[0]
            require(
                actual
                == hashlib.sha256(
                    (ROOT / "docker/workspace-entrypoint.sh").read_bytes()
                ).hexdigest(),
                "The workspace image does not contain this checkout's SSH entrypoint.",
            )
        repository = f"srw-native-gate-{name}"
        tag = f"localhost:5005/{repository}:{info['Id'].split(':')[1][:16]}"
        command(["docker", "tag", local, tag])
        command(["docker", "push", tag])
        # Docker's containerd image store may report the source index digest
        # even when a push publishes only the available platform manifest.
        # Pin the registry's actual published artifact, not that local alias.
        accept = {
            "Accept": ",".join(
                [
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                ]
            )
        }
        with httpx.Client(trust_env=False, timeout=10) as registry:
            published = registry.get(
                f"http://localhost:5005/v2/{repository}/manifests/{tag.rsplit(':', 1)[1]}",
                headers=accept,
            )
            require(
                published.status_code == 200,
                "The published registry manifest is unavailable.",
            )
            digest = published.headers.get("Docker-Content-Digest")
            require(
                digest
                and digest == "sha256:" + hashlib.sha256(published.content).hexdigest(),
                "The registry digest does not match its manifest bytes.",
            )
            require(
                registry.get(
                    f"http://localhost:5005/v2/{repository}/manifests/{digest}",
                    headers=accept,
                ).status_code
                == 200,
                "The published manifest cannot be fetched by digest.",
            )
        require(digest is not None, "The published image has no immutable digest.")
        images[name] = f"srw-registry:5000/{repository}@{digest}"
        identities[name] = {"localImageID": info["Id"], "registryDigest": digest}
    return images, identities


class Ledger:
    """Small durable identity ledger; intentionally stores no key material."""

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def write(self, identity, **values):
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "INSERT INTO records VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (identity, json.dumps(values)),
            )

    def read(self, identity):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT value FROM records WHERE id=?", (identity,)
            ).fetchone()
        return json.loads(row[0]) if row else None


class Gate:
    def __init__(self, api_client, images, ledger, *, context=CONTEXT):
        self.core = client.CoreV1Api(api_client)
        self.network = client.NetworkingV1Api(api_client)
        self.namespace = NAMESPACE_PREFIX + uuid4().hex[:12]
        self.namespace_uid = None
        self.images, self.ledger = images, ledger
        self.context = context
        self.harness = GenericHarnessRuntime(
            self.core, self.network, namespace=self.namespace
        )
        self.workspace = ManifestWorkspaceRuntime(
            self.core, self.network, namespace=self.namespace
        )
        self.tracked = []
        self.checks, self.observed_images = [], {}

    def passed(self, value):
        self.checks.append(value)
        announce(f"PASS: {value}")

    async def call(self, function, **kwargs):
        return await asyncio.to_thread(function, _request_timeout=20, **kwargs)

    async def wait(self, operation, *, message, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await operation()
            if result:
                return result
            await asyncio.sleep(1)
        raise GateFailure(message)

    async def create_namespace(self):
        result = await self.call(
            self.core.create_namespace,
            body={
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": self.namespace,
                    "labels": {"srw.io/test-gate": "native-manifest-runtime"},
                },
            },
        )
        self.namespace_uid = result.metadata.uid
        announce(f"Created disposable namespace {self.namespace} on {self.context}.")

    def generic_plan(
        self,
        identity,
        image,
        *,
        script=None,
        python=None,
        bindings=None,
        config_value=None,
    ):
        runtime = {"image": self.images[image], "pullPolicy": "IfNotPresent"}
        if script is not None:
            runtime["command"] = ["/bin/sh", "-ec", script]
        if python is not None:
            runtime["command"] = ["python", "-c", python]
        if config_value is not None:
            runtime["config"] = config_value
        return build_generic_launch(
            identity,
            {
                "execution": {"expert": {"inline": {"runtime": runtime}}},
                "completion": {"mode": "ProcessExit"},
            },
            namespace=self.namespace,
            policy=POLICY,
            bindings=bindings,
        )

    async def launch_harness(self, plan):
        identity = plan.identity
        self.ledger.write(identity.pod_name, state="Reserved", pod_uid=None)
        self.tracked.append((self.harness, identity))
        observed = await self.harness.launch(plan)
        require(observed.pod_uid, "Harness creation returned no UID.")
        self.ledger.write(identity.pod_name, state="Launched", pod_uid=observed.pod_uid)
        pod = await self.call(
            self.core.read_namespaced_pod,
            name=identity.pod_name,
            namespace=self.namespace,
        )
        require(
            pod.spec.automount_service_account_token is False,
            "Ambient service account credentials were enabled.",
        )
        require(
            pod.spec.enable_service_links is False,
            "Ambient service discovery env was enabled.",
        )
        require(
            not pod.spec.containers[0].env_from, "A shared envFrom source was injected."
        )
        require(
            all(
                not volume.persistent_volume_claim for volume in pod.spec.volumes or []
            ),
            "A workspace PVC was mounted into the harness container.",
        )
        return observed

    async def harness_terminal(self, identity, *, exit_code=None):
        row = self.ledger.read(identity.pod_name)

        async def terminal():
            result = await self.harness.observe(
                identity, expected_pod_uid=row["pod_uid"]
            )
            require(
                result.phase != "Replaced",
                "The harness UID changed during observation.",
            )
            return result if result.containers_terminal else None

        observed = await self.wait(
            terminal, message="A harness did not terminate within the gate deadline."
        )
        require(
            exit_code is None or observed.process_exit_code == exit_code,
            f"Harness exit code differs from expected {exit_code}; received {observed.process_exit_code}.",
        )
        require(observed.image_id, "The actual harness image digest was not observed.")
        self.observed_images[identity.pod_name] = observed.image_id
        self.ledger.write(
            identity.pod_name,
            state="Terminated",
            pod_uid=observed.pod_uid,
            exit_code=observed.process_exit_code,
            image_id=observed.image_id,
        )
        return observed

    async def cleanup_harness(self, identity):
        row = self.ledger.read(identity.pod_name)
        require(
            row["state"] == "Terminated",
            "An attempt was cleaned before its outcome was persisted.",
        )
        await self.wait(
            lambda: self.harness.cleanup(identity, expected_pod_uid=row["pod_uid"]),
            message="Harness cleanup did not establish absence.",
        )
        self.ledger.write(identity.pod_name, **{**row, "state": "Absent"})

    async def run_harness(self, plan, *, exit_code=0):
        await self.launch_harness(plan)
        observed = await self.harness_terminal(plan.identity, exit_code=exit_code)
        await self.cleanup_harness(plan.identity)
        return observed

    async def wait_running(self, runtime, identity, uid, *, workspace=False):
        async def ready():
            observed = await runtime.observe(identity, expected_pod_uid=uid)
            require(observed.phase != "Replaced", "A reserved pod was replaced.")
            require(
                not observed.containers_terminal,
                "A pod terminated before becoming ready.",
            )
            return (
                observed
                if observed.pod_ip
                and observed.phase == "Running"
                and (
                    not workspace
                    or observed.readiness is True
                    and observed.initialization_succeeded
                )
                else None
            )

        return await self.wait(
            ready,
            message="A pod did not become ready within the gate deadline.",
            timeout=180,
        )

    async def attach_workspace(self, instance_id, execution_id, generation, attempt):
        row = self.ledger.read(instance_id)
        require(
            row is None or row["state"] == "Detached",
            "A new workspace generation was admitted before process fencing.",
        )
        identity = WorkspaceRuntimeIdentity(instance_id, generation, execution_id)
        material = generate_workspace_ssh_material()
        initialized = bool(row and row["initialized"])
        self.ledger.write(
            instance_id,
            state="Reserved",
            generation=generation,
            pvc_uid=row["pvc_uid"] if row else None,
            pod_uid=None,
            initialized=initialized,
        )
        plan = build_workspace_launch(
            identity,
            {
                "backend": "sandbox",
                "retention": "Retain",
                "resources": {"cpu": 0.25, "memory": "256Mi", "storage": "256Mi"},
                "initialize": [
                    {
                        "command": [
                            "/bin/sh",
                            "-ec",
                            "printf 'seeded\\n' > seed.txt; n=$(cat init-count 2>/dev/null || printf 0); printf '%s\\n' $((n + 1)) > init-count; id -u > initializer-uid",
                        ]
                    }
                ],
            },
            material,
            namespace=self.namespace,
            default_image=self.images["workspace"],
            initialize=not initialized,
            ingress=(
                {
                    "from": [{"podSelector": {"matchLabels": attempt.labels}}],
                    "ports": [{"port": 30022, "protocol": "TCP"}],
                },
            ),
        )
        require(
            bool(plan.runtime.pod["spec"].get("initContainers")) != initialized,
            "Initialization did not follow the persisted server state.",
        )
        pvc_uid = await self.workspace.ensure_volume(
            plan, expected_pvc_uid=row["pvc_uid"] if row else None
        )
        record = self.ledger.read(instance_id)
        self.ledger.write(instance_id, **{**record, "pvc_uid": pvc_uid})
        self.tracked.append((self.workspace, identity))
        self.ledger.write(identity.pod_name, state="Reserved", pod_uid=None)
        created = await self.workspace.launch(plan)
        self.ledger.write(identity.pod_name, state="Launched", pod_uid=created.pod_uid)
        self.ledger.write(
            instance_id,
            **{
                **self.ledger.read(instance_id),
                "pod_uid": created.pod_uid,
                "state": "Attached",
            },
        )
        observed = await self.wait_running(
            self.workspace, identity, created.pod_uid, workspace=True
        )
        self.ledger.write(
            instance_id, **{**self.ledger.read(instance_id), "initialized": True}
        )
        require(
            observed.image_id, "The actual workspace image digest was not observed."
        )
        self.observed_images[identity.pod_name] = observed.image_id
        require(
            not await self.workspace.delete_volume(identity, expected_pvc_uid=pvc_uid),
            "Storage was deleted while a workspace pod still claimed it.",
        )
        try:
            await self.workspace.ensure_volume(plan, expected_pvc_uid=str(uuid4()))
        except ManifestWorkspaceError:
            pass
        else:
            raise GateFailure("A mismatched retained PVC UID was accepted.")
        bindings = workspace_harness_bindings(
            identity, material, pod_ip=observed.pod_ip, namespace=self.namespace
        )
        return identity, material, bindings, plan

    async def detach_workspace(self, identity):
        row = self.ledger.read(identity.instance_id)
        require(
            not await self.workspace.cleanup(identity, expected_pod_uid=row["pod_uid"]),
            "Running workspace processes were treated as already fenced.",
        )
        wrong = await self.workspace.cancel(identity, expected_pod_uid=str(uuid4()))
        require(
            wrong.phase == "Replaced",
            "Wrong-UID workspace cancellation was not rejected.",
        )
        correct = await self.workspace.observe(
            identity, expected_pod_uid=row["pod_uid"]
        )
        require(
            not correct.deletion_requested,
            "Wrong-UID workspace cancellation deleted the pod.",
        )
        await self.workspace.cancel(identity, expected_pod_uid=row["pod_uid"])

        async def terminated():
            observed = await self.workspace.observe(
                identity, expected_pod_uid=row["pod_uid"]
            )
            return observed if observed.containers_terminal else None

        await self.wait(
            terminated,
            message="Workspace processes did not terminate after cancellation.",
        )
        self.ledger.write(identity.instance_id, **{**row, "state": "Terminated"})
        await self.wait(
            lambda: self.workspace.cleanup(identity, expected_pod_uid=row["pod_uid"]),
            message="The old workspace pod was not removed after termination.",
        )
        retained = await self.call(
            self.core.read_namespaced_persistent_volume_claim,
            name=identity.pvc_name,
            namespace=self.namespace,
        )
        require(
            retained.metadata.uid == row["pvc_uid"],
            "Detachment replaced or removed the retained PVC.",
        )
        self.ledger.write(
            identity.instance_id, **{**row, "state": "Detached", "pod_uid": None}
        )

    async def prove_egress_policy(self):
        listener = GenericAttemptIdentity(str(uuid4()), 1)
        plan = self.generic_plan(
            listener,
            "python",
            python="import socket; s=socket.socket(); s.bind(('0.0.0.0',9443)); s.listen();\nwhile True:\n c,_=s.accept(); c.close()",
        )
        # A known reachable listener distinguishes an enforced deny from a dead
        # destination. Only this disposable fixture allows broad ingress.
        plan.network_policy["spec"]["ingress"] = [
            {
                "from": [{"podSelector": {}}],
                "ports": [{"port": 9443, "protocol": "TCP"}],
            }
        ]
        created = await self.launch_harness(plan)
        ready = await self.wait_running(self.harness, listener, created.pod_uid)
        allowed = GenericBindings(
            egress=(
                {
                    "to": [{"podSelector": {"matchLabels": listener.labels}}],
                    "ports": [{"port": 9443, "protocol": "TCP"}],
                },
            )
        )
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                python=connection_probe(ready.pod_ip, 9443, allowed=True),
                bindings=allowed,
            )
        )
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                python=connection_probe(ready.pod_ip, 9443, allowed=False),
            )
        )
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                python=connection_probe(ready.pod_ip, 9443, allowed=True),
                bindings=allowed,
            )
        )
        await self.harness.cancel(listener, expected_pod_uid=created.pod_uid)
        await self.harness_terminal(listener)
        await self.cleanup_harness(listener)
        self.passed("CNI default egress denial, with a reachable positive control")

    async def network_startup(self, samples):
        """Measure immediate packets under an already exercised namespace deny.

        No delay or trusted init container is placed before probe code. Later
        intervals measure convergence and established flows, never a barrier.
        """
        await self.create_namespace()
        baseline = await self.call(
            self.network.create_namespaced_network_policy,
            namespace=self.namespace,
            body={
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": "preexisting-default-deny",
                    "namespace": self.namespace,
                },
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            },
        )
        listener = GenericAttemptIdentity(str(uuid4()), 1)
        listener_plan = self.generic_plan(
            listener,
            "python",
            python="""import socket,threading
marker=b'srw-policy-gate-v1\\n'
def serve(c):
 try:
  c.settimeout(30); c.sendall(marker)
  while c.recv(1): c.sendall(marker)
 except OSError: pass
 finally: c.close()
s=socket.socket(); s.bind(('0.0.0.0',9443)); s.listen(128)
while True:
 c,_=s.accept(); threading.Thread(target=serve,args=(c,),daemon=True).start()
""",
        )
        listener_plan.network_policy["spec"]["ingress"] = [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": self.namespace
                            }
                        },
                        "podSelector": {},
                    }
                ],
                "ports": [{"port": 9443, "protocol": "TCP"}],
            }
        ]
        created = await self.launch_harness(listener_plan)
        ready = await self.wait_running(self.harness, listener, created.pod_uid)
        allowed = GenericBindings(
            egress=(
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": self.namespace
                                }
                            },
                            "podSelector": {"matchLabels": listener.labels},
                        }
                    ],
                    "ports": [{"port": 9443, "protocol": "TCP"}],
                },
            )
        )
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                python=connection_probe(ready.pod_ip, 9443, allowed=True),
                bindings=allowed,
            )
        )
        # Existing selected pod must demonstrate this baseline is effective
        # before testing what happens to subsequently created pods.
        witness = GenericAttemptIdentity(str(uuid4()), 1)
        await self.launch_harness(
            self.generic_plan(
                witness, "python", python=startup_probe(ready.pod_ip, duration=6)
            )
        )
        await self.harness_terminal(witness, exit_code=0)
        witness_result = await self.read_probe_result(witness)
        require(
            witness_result["lastAttemptBlocked"],
            "The namespace baseline never became effective.",
        )
        await self.cleanup_harness(witness)
        announce(
            "Namespace baseline denies an existing witness; launching immediate cold-start probes."
        )
        identities = []
        for _ in range(samples):
            identity = GenericAttemptIdentity(str(uuid4()), 1)
            identities.append(identity)
            await self.launch_harness(
                self.generic_plan(
                    identity, "python", python=startup_probe(ready.pod_ip, duration=8)
                )
            )
        results = []
        for identity in identities:
            await self.harness_terminal(identity, exit_code=0)
            result = await self.read_probe_result(identity)
            results.append(
                {
                    "podName": identity.pod_name,
                    "podUID": self.ledger.read(identity.pod_name)["pod_uid"],
                    **result,
                }
            )
            await self.cleanup_harness(identity)
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                python=connection_probe(ready.pod_ip, 9443, allowed=True),
                bindings=allowed,
            )
        )
        await self.harness.cancel(listener, expected_pod_uid=created.pod_uid)
        await self.harness_terminal(listener)
        await self.cleanup_harness(listener)
        report = {
            "baselineUID": baseline.metadata.uid,
            "baselineCreationTime": baseline.metadata.creation_timestamp.isoformat(),
            "baselineSelector": {},
            "baselineIngress": [],
            "baselineEgress": [],
            "witness": witness_result,
            "samples": results,
            "leakedSamples": sum(bool(row["allowedConnections"]) for row in results),
            "establishedConnectionsSurvived": sum(
                row["establishedSurvived"] for row in results
            ),
        }
        announce(json.dumps({"networkStartupEvidence": report}, indent=2))
        return report

    async def read_probe_result(self, identity):
        async def read():
            output = await self.call(
                self.core.read_namespaced_pod_log,
                name=identity.pod_name,
                namespace=self.namespace,
                container="harness",
            )
            for line in output.splitlines():
                if line.startswith("SRW_GATE_RESULT "):
                    return json.loads(line[len("SRW_GATE_RESULT ") :])
            return None

        return await self.wait(
            read,
            message="A finished probe's structured result is unavailable.",
            timeout=15,
        )

    async def exercise(self, *, check_network=True):
        await self.create_namespace()
        default = GenericAttemptIdentity(str(uuid4()), 1)
        default_plan = self.generic_plan(default, "busybox")
        require(
            "command" not in default_plan.pod["spec"]["containers"][0]
            and "args" not in default_plan.pod["spec"]["containers"][0],
            "Image launch defaults were replaced.",
        )
        await self.run_harness(default_plan)
        self.passed("unmodified BusyBox image CMD completes without SRW hooks")

        configuration = {
            "tools": {"nonexistent-tool": True},
            "preserved": None,
            "nested": [None, False],
        }
        await self.run_harness(
            self.generic_plan(
                GenericAttemptIdentity(str(uuid4()), 1),
                "python",
                config_value=configuration,
                python="import json,os,pathlib; assert json.load(open(os.environ['SRW_CONFIG_FILE'])) == "
                + repr(configuration)
                + "; assert not pathlib.Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists(); assert not any(k in os.environ for k in ('SRW_INTERNAL_API_KEY','MCP_INTERNAL_KEY','DATABASE_URL','ORCHESTRATOR_API_KEY')); assert not pathlib.Path('/home/agent-host').exists()",
            )
        )
        self.passed(
            "opaque config preserves nulls; generic image has no ambient SRW/API credentials or workspace mount"
        )
        if check_network:
            await self.prove_egress_policy()

        instance_id, execution_id = str(uuid4()), str(uuid4())
        first = GenericAttemptIdentity(execution_id, 1)
        workspace1, material1, bindings1, _ = await self.attach_workspace(
            instance_id, execution_id, 1, first
        )
        # An unrelated pod has explicit SSH egress, so this negative probe tests
        # workspace ingress selection rather than the probe's default deny.
        stranger = GenericAttemptIdentity(str(uuid4()), 1)
        if check_network:
            await self.run_harness(
                self.generic_plan(
                    stranger,
                    "python",
                    python=connection_probe(
                        bindings1.descriptor["workspace"]["host"], 30022, allowed=False
                    ),
                    bindings=GenericBindings(egress=bindings1.egress),
                )
            )
            self.passed(
                "workspace SSH ingress excludes an unrelated harness with explicit outbound access"
            )
        first_remote = """set -eu
cd /home/agent-host/workspace
test "$(id -u)" = 1000
test "$(cat initializer-uid)" = 1000
test "$(cat seed.txt)" = seeded
test "$(cat init-count)" = 1
test ! -f /var/run/secrets/kubernetes.io/serviceaccount/token
printf 'attempt-one\\n' > retained.txt
nohup sh -c 'while true; do echo tick >> orphan-one; sleep 1; done' >/dev/null 2>&1 </dev/null &
sleep 2
test -s orphan-one
"""
        await self.run_harness(
            self.generic_plan(
                first,
                "ssh",
                bindings=bindings1,
                script=ssh_script(first_remote) + "\nexit 73\n",
            ),
            exit_code=73,
        )
        await self.detach_workspace(workspace1)
        # Reconstruct the ledger driver to ensure initialization and storage
        # identities are consumed from durable state, not a previous plan.
        self.ledger = Ledger(self.ledger.path)
        self.passed(
            "failed attempt leaves initialized data; old workspace and orphan process are fenced before retry"
        )

        second = GenericAttemptIdentity(execution_id, 2)
        workspace2, material2, bindings2, _ = await self.attach_workspace(
            instance_id, execution_id, 2, second
        )
        require(
            material1.client_public_key != material2.client_public_key
            and material1.host_public_key != material2.host_public_key,
            "An attachment reused SSH identity material.",
        )
        stale_host = f"[{bindings2.descriptor['workspace']['host']}]:30022 {material1.host_public_key}\n"
        bindings2 = replace(
            bindings2,
            files=(
                *bindings2.files,
                GenericBoundFile("/run/gate/old-key", material1.client_private_key),
                GenericBoundFile("/run/gate/old-host", stale_host),
            ),
        )
        second_remote = """set -eu
cd /home/agent-host/workspace
test "$(cat init-count)" = 1
test "$(cat seed.txt)" = seeded
test "$(cat retained.txt)" = attempt-one
n=$(wc -l < orphan-one); sleep 3; test "$n" = "$(wc -l < orphan-one)"
nohup sh -c 'while true; do echo tick >> orphan-two; sleep 1; done' >/dev/null 2>&1 </dev/null &
sleep 2
test -s orphan-two
"""
        stale_checks = stale_ssh_checks(bindings2.descriptor["workspace"]["host"])
        await self.launch_harness(
            self.generic_plan(
                second,
                "ssh",
                bindings=bindings2,
                script=ssh_script(second_remote)
                + stale_checks
                + "\nprintf 'ready-to-cancel\\n'\nsleep 300\n",
            )
        )

        async def cancellation_ready():
            row = self.ledger.read(second.pod_name)
            observed = await self.harness.observe(
                second, expected_pod_uid=row["pod_uid"]
            )
            require(
                not observed.containers_terminal,
                "The retained-workspace or SSH rotation checks failed before cancellation.",
            )
            try:
                output = await self.call(
                    self.core.read_namespaced_pod_log,
                    name=second.pod_name,
                    namespace=self.namespace,
                    container="harness",
                )
            except client.ApiException as exc:
                if exc.status == 400:
                    return False
                raise
            return "ready-to-cancel" in output.splitlines()

        await self.wait(
            cancellation_ready,
            message="The second harness never completed its persistence and key checks.",
        )
        row = self.ledger.read(second.pod_name)
        require(
            not await self.harness.cleanup(second, expected_pod_uid=row["pod_uid"]),
            "A running harness finalizer was released.",
        )
        wrong = await self.harness.cancel(second, expected_pod_uid=str(uuid4()))
        require(
            wrong.phase == "Replaced", "Wrong-UID harness cancellation was accepted."
        )
        observed = await self.harness.observe(second, expected_pod_uid=row["pod_uid"])
        require(
            not observed.deletion_requested,
            "Wrong-UID harness cancellation deleted its target.",
        )
        await self.harness.cancel(second, expected_pod_uid=row["pod_uid"])
        cancelled = await self.harness_terminal(second)
        require(
            cancelled.process_exit_code != 0,
            "The cancelled harness unexpectedly completed successfully.",
        )
        await self.cleanup_harness(second)
        await self.detach_workspace(workspace2)
        self.passed(
            "retry reuses exact PVC and initializes once; rotated keys reject stale client and host identities"
        )
        self.passed(
            "running process cleanup is fenced; wrong UID cannot cancel; exact-UID cancellation terminates before removal"
        )

        third_execution = str(uuid4())
        third = GenericAttemptIdentity(third_execution, 1)
        workspace3, _, bindings3, plan3 = await self.attach_workspace(
            instance_id, third_execution, 3, third
        )
        third_remote = """set -eu
cd /home/agent-host/workspace
test "$(cat init-count)" = 1
test "$(cat retained.txt)" = attempt-one
n=$(wc -l < orphan-two); sleep 3; test "$n" = "$(wc -l < orphan-two)"
"""
        await self.run_harness(
            self.generic_plan(
                third, "ssh", bindings=bindings3, script=ssh_script(third_remote)
            )
        )
        await self.detach_workspace(workspace3)
        row = self.ledger.read(instance_id)
        require(
            not await self.workspace.delete_volume(
                workspace3, expected_pvc_uid=str(uuid4())
            ),
            "Wrong-UID storage deletion was accepted.",
        )
        await self.wait(
            lambda: self.workspace.delete_volume(
                workspace3, expected_pvc_uid=row["pvc_uid"]
            ),
            message="Explicit storage release did not finish.",
        )
        try:
            await self.workspace.ensure_volume(plan3, expected_pvc_uid=row["pvc_uid"])
        except ManifestWorkspaceError:
            pass
        else:
            raise GateFailure("A missing retained PVC was silently recreated.")
        self.ledger.write(instance_id, **{**row, "state": "Released"})
        self.passed(
            "retained instance crosses execution IDs; cancelled workspace descendants stop; explicit release pins PVC UID"
        )
        self.passed("lost retained storage is rejected instead of silently replaced")

    async def cleanup(self):
        if not self.namespace_uid:
            return
        # Never bypass the exact-UID/container-termination fence to tidy a test.
        # An unhealthy node may require operator recovery; leave the namespace
        # name in the sanitized failure instead of claiming successful cleanup.
        for runtime, identity in reversed(self.tracked):
            record = self.ledger.read(identity.pod_name)
            observed = await runtime.observe(
                identity,
                expected_pod_uid=record["pod_uid"] if record else None,
            )
            if observed.pod_absent:
                continue
            require(
                observed.phase != "Replaced", "Cleanup found a foreign runtime object."
            )
            uid = observed.pod_uid
            await runtime.cancel(identity, expected_pod_uid=uid)
            await self.wait(
                lambda: runtime.cleanup(identity, expected_pod_uid=uid),
                message=f"Cleanup is fenced in disposable namespace {self.namespace}.",
            )
        namespace = await self.call(self.core.read_namespace, name=self.namespace)
        require(
            namespace.metadata.uid == self.namespace_uid
            and namespace.metadata.labels.get("srw.io/test-gate")
            == "native-manifest-runtime",
            "Disposable namespace ownership changed; cleanup refused.",
        )
        await self.call(
            self.core.delete_namespace,
            name=self.namespace,
            body={"preconditions": {"uid": self.namespace_uid}},
        )

        async def absent():
            try:
                await self.call(self.core.read_namespace, name=self.namespace)
                return False
            except client.ApiException as exc:
                if exc.status == 404:
                    return True
                raise

        await self.wait(
            absent,
            message=f"Disposable namespace {self.namespace} is still terminating.",
        )
        announce("Disposable namespace, PVCs, and credential Secrets removed.")


def connection_probe(host, port, *, allowed):
    # CNIs may reject traffic immediately instead of silently dropping it. The
    # caller brackets the negative probe with positive controls against the same
    # listener, so a dead destination cannot pass as policy enforcement.
    return f"""import errno,socket,time
host,port={host!r},{port}
allowed={allowed!r}
for attempt in range(10 if allowed else 3):
    try:
        with socket.create_connection((host,port),timeout=2): pass
    except TimeoutError:
        if not allowed: continue
    except OSError as exc:
        if not allowed and exc.errno not in (errno.ECONNREFUSED,errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EACCES,errno.EPERM): raise SystemExit(11)
    else:
        raise SystemExit(0 if allowed else 12)
    time.sleep(1)
raise SystemExit(13 if allowed else 0)
"""


def startup_probe(host, *, duration):
    return f"""import json,socket,time
start=time.monotonic(); wall=time.time(); until=start+{duration}
allowed=[]; blocked=0; held=None; last_blocked=False
while time.monotonic()<until:
 try:
  c=socket.create_connection(({host!r},9443),timeout=.25)
  c.settimeout(.25)
  assert c.recv(128)==b'srw-policy-gate-v1\\n'
  allowed.append(round((time.monotonic()-start)*1000,1)); last_blocked=False
  if held is None: held=c
  else: c.close()
 except OSError:
  blocked+=1; last_blocked=True
 time.sleep(.05)
survived=False
if held:
 try:
  held.sendall(b'x'); survived=held.recv(128)==b'srw-policy-gate-v1\\n'
 except OSError: pass
 held.close()
print('SRW_GATE_RESULT '+json.dumps({{'startedAtUnix':wall,'allowedConnections':len(allowed),'firstAllowedMs':allowed[0] if allowed else None,'lastAllowedMs':allowed[-1] if allowed else None,'blockedConnections':blocked,'lastAttemptBlocked':last_blocked,'establishedSurvived':survived}}))
"""


def ssh_script(remote):
    return (
        """set -eu
test ! -f /var/run/secrets/kubernetes.io/serviceaccount/token || exit 90
test -z "${SRW_INTERNAL_API_KEY+x}" || exit 91
test -z "${MCP_INTERNAL_KEY+x}" || exit 91
test ! -d /home/agent-host || exit 92
test -r "$SRW_BINDINGS_FILE" || exit 93
ready=0
for n in 1 2 3 4 5 6 7 8 9 10; do
  if timeout 4 /run/srw-workspace/ssh true >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
test "$ready" = 1 || exit 94
timeout 30 /run/srw-workspace/ssh """
        + shlex.quote(remote)
        + " >/dev/null || exit 95\n"
    )


def stale_ssh_checks(host):
    target = shlex.quote("agent-host@" + host)
    options = f"-p 30022 -o ConnectTimeout=3 -o BatchMode=yes -o IdentitiesOnly=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 {target} true"
    return f"""
key=$(mktemp); out=$(mktemp)
trap 'rm -f "$key" "$out"' EXIT HUP INT TERM
chmod 0600 "$key"
cat /run/gate/old-key > "$key"
if ssh -i "$key" -o UserKnownHostsFile=/run/srw-workspace/known_hosts {options} >"$out" 2>&1; then exit 81; fi
grep -q 'Permission denied' "$out"
cat /run/srw-workspace/id_ed25519 > "$key"
if ssh -i "$key" -o UserKnownHostsFile=/run/gate/old-host {options} >"$out" 2>&1; then exit 82; fi
grep -q 'HOST IDENTIFICATION HAS CHANGED' "$out"
rm -f "$key" "$out"
trap - EXIT HUP INT TERM
"""


async def run(args):
    api_client, nodes = await asyncio.to_thread(
        verify_cluster, context=args.kube_context, kubeconfig=args.kubeconfig
    )
    try:
        images, identities = await asyncio.to_thread(publish_images, args)
        with tempfile.TemporaryDirectory(prefix="srw-native-gate-") as directory:
            gate = Gate(
                api_client,
                images,
                Ledger(Path(directory) / "identity.sqlite3"),
                context=args.kube_context,
            )
            try:
                evidence = None
                if args.network_startup_samples:
                    evidence = await gate.network_startup(args.network_startup_samples)
                else:
                    await gate.exercise(check_network=not args.functional_only)
            finally:
                await gate.cleanup()
            if evidence and evidence["leakedSamples"]:
                raise GateFailure(
                    "Preexisting namespace-wide default deny allowed cold-start traffic; this CNI has not passed native isolation acceptance."
                )
            announce(
                json.dumps(
                    {
                        "result": "passed-functional-only"
                        if args.functional_only
                        else "passed",
                        "coldStartIsolationAccepted": bool(
                            evidence is not None and not evidence["leakedSamples"]
                        ),
                        "context": args.kube_context,
                        "nodes": nodes,
                        "namespaceRemoved": gate.namespace,
                        "checks": gate.checks,
                        "images": identities,
                        "observedImageIDs": gate.observed_images,
                        "scope": "Real Kubernetes adapters with a durable test identity ledger; not production API/admission/Postgres lifecycle integration.",
                    },
                    indent=2,
                )
            )
    finally:
        api_client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kube-context", default=CONTEXT)
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--workspace-image", default="srw-manifest-workspace:gate")
    parser.add_argument("--busybox-image", default="busybox:1.36")
    parser.add_argument("--ssh-image", default="alpine/git:latest")
    parser.add_argument("--python-image", default="python:3.12-slim")
    parser.add_argument(
        "--functional-only",
        action="store_true",
        help="Exercise SSH/PVC/process functions only; explicitly does not accept network isolation.",
    )
    parser.add_argument(
        "--network-startup-samples",
        type=int,
        default=0,
        help="Only test cold-start isolation under a preexisting namespace-wide deny policy (1–32 pods).",
    )
    args = parser.parse_args()
    if not 0 <= args.network_startup_samples <= 32:
        parser.error("--network-startup-samples must be between 0 and 32")
    if args.functional_only and args.network_startup_samples:
        parser.error(
            "--functional-only cannot be combined with --network-startup-samples"
        )
    try:
        asyncio.run(run(args))
    except GateFailure as exc:
        announce(json.dumps({"result": "failed", "reason": str(exc)}))
        return 1
    except Exception as exc:
        # Kubernetes exception bodies and pod logs may contain delivery details.
        # Report only the exception class, never arbitrary secret-bearing text.
        announce(
            json.dumps(
                {
                    "result": "failed",
                    "reason": f"Unexpected {type(exc).__name__}; no arbitrary API body logged.",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
