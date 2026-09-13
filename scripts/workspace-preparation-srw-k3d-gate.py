#!/usr/bin/env python3
"""Verify prepared VM Jobs through local MCP and the deployed SRW harness.

Requires a coherent k3d-srw deployment with offline preparation enabled. Creates
owned resources, an expiring user-scoped MCP token and a deterministic provider.
The provider requires a successful run_command result from the prepared guest.
Mutations are never replayed after a transport failure. Evidence is allowlisted;
tokens, credentials and arbitrary HTTP/agent response bodies are never printed.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import re
import ssl
from uuid import UUID, uuid4

import httpx
from kubernetes import config as kube_config

from shared.manifests import validate_documents
from shared.workspace_preparation import PREPARATION_LABEL, image_reference

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "preparation_srw_smoke", ROOT / "scripts/manifests-srw-k3d-smoke.py"
)
smoke_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_module)
cutover = smoke_module.cutover
GateFailure, require = cutover.GateFailure, cutover.require
DEFAULT_BASE = (
    "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base"
    "@sha256:db1015a32173c4553d1ad432bdb48760fdb8ba0782d25ae20349d4aaaf26c28f"
)


def preparation_identity():
    """Inspect admission and controller settings before creating a fixture."""
    shared = [
        "src/shared/workspace_preparation.py",
        "src/shared/workspace_preparation_settings.py",
        "src/shared/workspace_initialization.py",
        "src/shared/vm_workspace_storage.py",
    ]
    paths = shared + [
        "src/orchestrator/services/vm_preparation.py",
        "src/orchestrator/services/vm_provisioner.py",
        "src/orchestrator/services/retained_vm_workspaces.py",
        "src/orchestrator/services/vm_readiness.py",
        "src/orchestrator/services/vm_guest_events.py",
        "src/orchestrator/services/ide_settings.py",
    ]

    def probe(files):
        return f"""
import hashlib,json,os
from pathlib import Path
from shared.workspace_preparation_settings import PreparationSettings
p=PreparationSettings.from_environment()
print(json.dumps({{
 'files':{{f:hashlib.sha256(Path('/app',f).read_bytes()).hexdigest() for f in {files!r}}},
 'enabled':p.enabled,'diskSize':p.disk_size,'builderImage':p.builder_image,
 'networkEnabled':p.network_enabled,
 'authenticated':bool(os.getenv('VM_LIFECYCLE_HMAC_SECRET')),
 'mode':os.getenv('VM_MODE'),
 'persistentRootdisk':os.getenv('VM_PERSISTENT_ROOTDISK','false').lower()=='true'
}}))
"""

    admission = cutover.remote_json(probe(paths))
    controller_paths = sorted(
        shared
        + [str(p.relative_to(ROOT)) for p in (ROOT / "src/vm_controller").rglob("*.py")]
    )
    controller = json.loads(
        cutover.command(
            cutover.KUBECTL
            + [
                "exec",
                "deployment/srw-vm-controller",
                "--",
                "python",
                "-c",
                probe(controller_paths),
            ]
        )
    )
    for role, data in (("admission", admission), ("controller", controller)):
        require(
            data["files"]
            == {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                for p in data["files"]
            },
            "Deployed preparation source differs for " + role + ".",
        )
        require(
            data["enabled"] and data["authenticated"] and data["persistentRootdisk"],
            "Preparation hosting prerequisites are missing for " + role + ".",
        )
        require(not data["networkEnabled"], "This gate requires offline preparation.")
    require(
        admission["mode"] == "same-cluster",
        "Preparation requires same-cluster admission.",
    )
    require(
        all(
            admission[k] == controller[k]
            for k in ("diskSize", "builderImage", "networkEnabled")
        ),
        "Preparation admission and controller capabilities disagree.",
    )
    return {
        "orchestratorSourceFilesMatched": len(paths),
        "controllerSourceFilesMatched": len(controller_paths),
        "diskSize": admission["diskSize"],
        "builderImage": admission["builderImage"],
        "networkEnabled": False,
    }


def mcp_call(token, name, arguments, *, expect_json=True):
    """Use the real authenticated HTTP MCP service, with no mutation retry."""
    from shared.mcp_sdk import ensure_mcp_sdk

    ensure_mcp_sdk()
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def http_client(**kwargs):
        kwargs.update(trust_env=False, verify=ssl.create_default_context())
        kwargs.setdefault("follow_redirects", False)
        return httpx.AsyncClient(**kwargs)

    async def invoke():
        transport = StreamableHttpTransport(
            "https://mcp.localhost/mcp",
            auth=token,
            httpx_client_factory=http_client,
        )
        async with Client(transport, timeout=90, init_timeout=20) as client:
            result = await client.call_tool(name, arguments)
            require(not result.is_error, "MCP reported a tool failure.")
            require(
                len(result.content) == 1 and result.content[0].type == "text",
                "MCP returned an unexpected content shape.",
            )
            if not expect_json:
                return result.content[0].text
            try:
                value = json.loads(result.content[0].text)
            except ValueError:
                # Monitoring tools format API failures as text. Keep only a
                # machine error code, never an arbitrary upstream message.
                code = "unclassified"
                try:
                    detail = ast.literal_eval(result.content[0].text.split("\n", 1)[1])
                    candidate = detail.get("code") if isinstance(detail, dict) else None
                    if isinstance(candidate, str) and re.fullmatch(
                        r"[a-z_]{1,80}", candidate
                    ):
                        code = candidate
                except (ValueError, SyntaxError, IndexError):
                    pass
                raise GateFailure(f"MCP {name} returned an error ({code}).") from None
            require(isinstance(value, dict), "MCP operation result is not an object.")
            return value

    try:
        return asyncio.run(invoke())
    except GateFailure:
        raise
    except Exception as exc:
        raise GateFailure(
            "MCP transport or invocation failed ("
            + type(exc).__name__
            + "); mutation outcome may be unknown."
        ) from None


def preparation_recipe(prefix, base_image, *, retention="Delete"):
    require(
        prefix.startswith("cutover-")
        and prefix.endswith("-")
        and all(c.isalnum() or c == "-" for c in prefix),
        "Invalid owned preparation prefix.",
    )
    return {
        "backend": "vm",
        "retention": retention,
        "resources": {"cpu": 2, "memory": "3Gi", "storage": "30Gi"},
        "environment": {
            "image": base_image,
            "pullPolicy": "IfNotPresent",
            "cache": "Reuse",
            "prepare": [
                {
                    "command": [
                        "sh",
                        "-c",
                        "\n".join(
                            [
                                "set -eu",
                                "install -d -m 0755 /opt/srw-preparation-gate",
                                "printf '#!/bin/sh\\nprintf \"srw-prepared-tool-v1\\\\n\"\\n' > /usr/local/bin/srw-cache-check",
                                "chmod 0755 /usr/local/bin/srw-cache-check",
                                f"printf '%s\\n' '{prefix}' >> /opt/srw-preparation-gate/build-count",
                            ]
                        ),
                    ]
                }
            ],
        },
        "initialize": [
            {
                "command": [
                    "sh",
                    "-c",
                    "\n".join(
                        [
                            "set -eu",
                            'test "$(wc -l < /opt/srw-preparation-gate/build-count)" -eq 1',
                            'test "$(srw-cache-check)" = srw-prepared-tool-v1',
                            "printf '%s\\n' initialized >> .srw-initialize-count",
                        ]
                    ),
                ]
            }
        ],
    }


class PreparedSmoke(smoke_module.Smoke):
    def __init__(self, *args, base_image, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_image = base_image
        self.token = None
        self.cache_uids = set()
        self.instance_uids = set()
        self.evidence["preparedJobs"] = []

    def create_token(self):
        token = self.gate.request(
            "POST",
            "/api/mcp-tokens",
            payload={
                "name": self.prefix + "mcp",
                "scope": "user",
                "expires_in_days": 1,
            },
        )
        require(isinstance(token.get("token"), str), "MCP token creation failed.")
        self.token = token["token"]

    def mcp_apply(self, document, *, key=None, versions=None):
        metadata = document["metadata"]
        require(
            metadata["name"].startswith(self.prefix)
            and metadata.get("annotations", {}).get(cutover.OWNER_LABEL) == self.prefix,
            "Refusing an MCP mutation outside the owned fixture identities.",
        )
        self.gate.cleanup_intents.add((document["kind"], metadata["name"]))
        arguments = {"source": json.dumps(document), "format": "json"}
        validate_documents([document])
        smoke_module.progress("MCP apply: " + document["kind"] + " " + metadata["name"])
        if key:
            arguments["idempotency_key"] = key
        if versions:
            arguments["expected_versions"] = versions
        return mcp_call(self.token, "manifest_apply", arguments)

    def runtime_evidence(self, work_id):
        work_id = str(UUID(work_id))
        # Only execution/cache/disk identities are returned, never VM credentials.
        return cutover.remote_json(f"""
import asyncio,json
from uuid import UUID
from orchestrator.database.postgres import PostgresDB
async def run():
    db=PostgresDB(server_settings={{'default_transaction_read_only':'on'}}); await db.connect()
    try:
        row=await db.get_job({work_id!r})
        assert row and row['description'].startswith({"E2E-" + self.prefix!r})
        context=row.get('context') or {{}}
        if isinstance(context,str): context=json.loads(context)
        vm=context.get('vm') or {{}}
        prep=vm.get('preparation') or {{}}
        initialized=vm.get('initialization_receipt') or {{}}
        binding=await db.fetchrow('''SELECT i.id,i.generation,i.status,i.pvc_uid
            FROM srw_execution_specs s JOIN srw_execution_workspace_bindings b ON b.execution_id=s.id
            JOIN srw_workspace_instances i ON i.id=b.instance_id WHERE s.work_id=$1''',UUID({work_id!r}))
        print(json.dumps({{
            'vmUID':vm.get('vm_uid'),'pvcUID':vm.get('rootdisk_pvc_uid'),
            'vmStatus':vm.get('status'),
            'preparation':{{key:prep.get(key) for key in ('uid','phase','cacheHit','cacheKey','diskSha256')}},
            'initialization':{{key:initialized.get(key) for key in ('phase','step','exitCode')}},
            'instance':{{key:str(value) for key,value in dict(binding).items()}} if binding else None,
        }}))
    finally: await db.disconnect()
asyncio.run(run())
""")

    def owned_jobs(self):
        require(
            re.fullmatch(r"cutover-[0-9a-f]{12}-", self.prefix),
            "Invalid prepared gate identity.",
        )
        pattern = "^" + re.escape("E2E-" + self.prefix + "job-") + "[a-z-]+$"
        return cutover.remote_json(f"""
import asyncio,json
from orchestrator.database.postgres import PostgresDB
async def run():
    db=PostgresDB(server_settings={{'default_transaction_read_only':'on'}})
    await db.connect()
    try:
        rows=await db.fetch('SELECT id::text AS id FROM jobs WHERE description ~ $1', {pattern!r})
        print(json.dumps([row['id'] for row in rows]))
    finally: await db.disconnect()
asyncio.run(run())
""")

    def exercise(self, case, *, binding=None, retention="Delete"):
        self.evidence["stage"] = "job-" + case
        run_id = self.prefix + "job-" + case
        self.fixture.arm(run_id, "prepared-workspace-job", 100)
        worker = smoke_module.authored_expert(self.prefix, self.image, self.model)
        worker["spec"]["runtime"]["config"]["config"]["tools"]["shell"] = [
            "run_command"
        ]
        self.mcp_apply(worker)
        if binding is None:
            template = {
                "apiVersion": "srw/v1alpha1",
                "kind": "WorkspaceTemplate",
                "metadata": {
                    "name": self.prefix
                    + ("retained" if retention == "Retain" else "prepared"),
                    "scope": {"kind": "Account", "name": "me"},
                    "annotations": {cutover.OWNER_LABEL: self.prefix},
                },
                "spec": preparation_recipe(
                    self.prefix, self.base_image, retention=retention
                ),
            }
            self.mcp_apply(template)
            binding = {"template": {"ref": {"name": template["metadata"]["name"]}}}
        document = smoke_module.authored_job(self.prefix, worker)
        document["metadata"]["name"] = run_id
        document["spec"]["task"]["text"] = "E2E-" + run_id
        document["spec"]["timeoutSeconds"] = 3600
        document["spec"]["execution"].update(
            expert={"ref": {"name": worker["metadata"]["name"]}}, workspace=binding
        )
        validate_documents([document])
        preview = mcp_call(
            self.token,
            "manifest_preview",
            {"source": json.dumps(document), "format": "json", "resolution": "stored"},
        )
        require(
            preview["effects"] == [] and not preview["admissionReady"],
            "Preview had execution effects.",
        )
        result = self.mcp_apply(document, key=run_id)
        require(len(result["executions"]) == 1, "MCP did not admit exactly one Job.")
        self.job_id = str(UUID(next(iter(result["executions"].values()))))
        item = result["resources"][0]
        self.evidence["preparedJobs"].append({"case": case, "workID": self.job_id})
        record = self.evidence["preparedJobs"][-1]

        def finished():
            current = self.gate.current(item)
            phase = current.get("status", {}).get("phase")
            if phase in {"failed", "paused", "pending_review"}:
                record["phase"] = phase
                record["runtime"] = self.runtime_evidence(self.job_id)
                record["provider"] = self.fixture.state(run_id)
            require(
                phase not in {"failed", "paused", "pending_review"},
                "Prepared SRW Job did not complete successfully.",
            )
            return current if phase == "completed" else None

        smoke_module.wait_for(
            finished,
            "Waiting for MCP-admitted prepared VM Job " + case + ".",
            timeout=3600,
        )
        runtime = self.runtime_evidence(self.job_id)
        record["runtime"] = runtime
        if runtime["instance"]:
            self.instance_uids.add(str(UUID(runtime["instance"]["id"])))
        if runtime["preparation"].get("uid"):
            self.cache_uids.add(str(UUID(runtime["preparation"]["uid"])))
        state = self.fixture.state(run_id)
        require(
            state["worker_job_tool_steps"] >= 11
            and state["unexpected_count"] == 0
            and state["pending_calls"] == 0,
            "The prepared Job did not complete its required shell proof cleanly.",
        )
        snapshot = self.snapshot("Job", self.job_id)
        require(
            snapshot["workspaceBackend"] == "vm" and snapshot["workspace"] == binding,
            "MCP Job workspace selection changed.",
        )
        record["provider"] = state
        record["snapshot"] = snapshot
        repeated = self.mcp_apply(document)
        require(
            repeated["executions"] == result["executions"]
            and not repeated["resources"][0]["changed"],
            "MCP reapply replayed a Job.",
        )
        classified = deepcopy(item["resource"])
        classified["metadata"].update(
            tags=["prepared", "verified"], labels={"team": "v1-gate"}
        )
        scope = classified["metadata"]["scope"]
        key = f"Job/{scope['kind']}/{scope['name']}/{classified['metadata']['name']}"
        updated = self.mcp_apply(classified, versions={key: item["resourceVersion"]})
        require(
            updated["executions"] == result["executions"]
            and updated["resources"][0]["revision"] == item["revision"],
            "Metadata edit changed the completed execution.",
        )
        require(
            self.snapshot("Job", self.job_id) == snapshot,
            "Metadata edit rewrote the frozen execution snapshot.",
        )
        record["metadataEditPreservedExecution"] = True
        self.fixture.reset(run_id)
        self.check(
            "MCP-admitted prepared VM Job "
            + case
            + " completed with verified guest shell output."
        )
        return runtime

    def detached(self, uid):
        state = self.gate.request("GET", "/api/workspace-instances/" + str(UUID(uid)))
        return (
            state
            if state["status"] == "Detached" and state["executionId"] is None
            else None
        )

    def negative(self, case, *, cancel=False):
        """A failed/cancelled builder must never dispatch a harness into a VM."""
        run_id = self.prefix + "job-" + case
        self.evidence["stage"] = run_id
        worker = smoke_module.authored_expert(self.prefix, self.image, self.model)
        document = smoke_module.authored_job(self.prefix, worker)
        document["metadata"]["name"] = run_id
        document["spec"]["task"]["text"] = "E2E-" + run_id
        document["spec"]["timeoutSeconds"] = 1800
        recipe = preparation_recipe(self.prefix, self.base_image)
        recipe["environment"]["prepare"] = [
            {"command": ["sh", "-c", "sleep 300" if cancel else "exit 17", run_id]}
        ]
        document["spec"]["execution"].update(
            expert={"ref": {"name": worker["metadata"]["name"]}},
            workspace={"template": {"inline": recipe}},
        )
        result = self.mcp_apply(document, key=run_id)
        work_id = str(UUID(next(iter(result["executions"].values()))))
        item = result["resources"][0]
        record = {"case": case, "workID": work_id}
        self.evidence["preparedJobs"].append(record)

        def building():
            runtime = self.runtime_evidence(work_id)
            require(
                not runtime["vmUID"],
                "Negative preparation case unexpectedly allocated a VM.",
            )
            uid = runtime["preparation"].get("uid")
            if uid:
                self.cache_uids.add(str(UUID(uid)))
            if runtime["preparation"].get("phase") != "Building":
                return None
            pods = self.fixture.core.list_namespaced_pod(
                "srw", label_selector=PREPARATION_LABEL + "=" + str(UUID(uid))
            ).items
            return runtime if any(p.status.phase == "Running" for p in pods) else None

        if cancel:
            started = smoke_module.wait_for(
                building,
                "Waiting for the owned builder before cancellation.",
                timeout=900,
            )
            record["cancelledBuilderUID"] = started["preparation"]["uid"]
            # The control tool returns formatted text. Judge its actual effect
            # through the authenticated resource read, never an English substring.
            mcp_call(self.token, "cancel_job", {"job_id": work_id}, expect_json=False)

        def terminal():
            phase = self.gate.current(item).get("status", {}).get("phase")
            require(phase != "completed", "Negative preparation case completed a Job.")
            return phase == ("cancelled" if cancel else "failed")

        smoke_module.wait_for(
            terminal, "Waiting for the negative preparation outcome.", timeout=900
        )
        runtime = self.runtime_evidence(work_id)
        uid = runtime["preparation"].get("uid")
        if uid:
            self.cache_uids.add(str(UUID(uid)))
        require(
            not runtime["vmUID"] and not runtime["pvcUID"],
            "Failed or cancelled preparation created an execution VM or disk.",
        )
        record.update(
            runtime=runtime,
            outcome="cancelled" if cancel else "failed",
            noWorkspaceAllocated=True,
        )
        if cancel:
            smoke_module.wait_for(
                lambda: not self.fixture.core.list_namespaced_pod(
                    "srw",
                    label_selector=PREPARATION_LABEL
                    + "="
                    + record["cancelledBuilderUID"],
                ).items,
                "Waiting for the cancelled builder to retire.",
                timeout=180,
            )
        self.check("MCP preparation " + case + " stopped before workspace allocation.")

    def retire_owned_jobs(self):
        # Recover allocation IDs even if the successful admission response was lost.
        self.gate.login()
        owner_id = self.gate.request("GET", "/api/auth/me")["user"]["id"]
        for work_id in self.owned_jobs():
            runtime = self.runtime_evidence(work_id)
            uid = runtime["preparation"].get("uid")
            if uid:
                self.cache_uids.add(str(UUID(uid)))
            if runtime["instance"]:
                self.instance_uids.add(str(UUID(runtime["instance"]["id"])))
            # The original adapter smoke has exactly one Job title. This gate
            # creates several; verify each exact random prefix and immutable ID
            # before using the same public retirement path.
            path = "/api/jobs/" + str(UUID(work_id))
            receipt = {"kind": "Job", "workID": work_id, "absent": False}
            self.evidence["cleanup"].setdefault("workloads", []).append(receipt)

            def retired():
                row = self.gate.request("GET", path)
                require(
                    row.get("id") == work_id
                    and row.get("user_id") == owner_id
                    and re.fullmatch(
                        re.escape("E2E-" + self.prefix + "job-") + r"[a-z-]+",
                        row.get("description", ""),
                    ),
                    "Owned Job identity changed; cleanup refused.",
                )
                response = self.client.delete(
                    cutover.API + path, headers=self.gate.headers
                )
                receipt.setdefault("deleteStatuses", []).append(response.status_code)
                require(
                    response.status_code in {200, 503},
                    f"Owned Job retirement returned HTTP {response.status_code}.",
                )
                if response.status_code == 503:
                    return False
                response = self.client.get(
                    cutover.API + path, headers=self.gate.headers
                )
                require(response.status_code == 404, "Retired Job remains visible.")
                receipt["absent"] = True
                return True

            smoke_module.wait_for(
                retired, "Waiting for owned prepared Job retirement.", timeout=180
            )
            custom = smoke_module.kube.CustomObjectsApi(self.fixture.client)
            name = "agent-vm-" + str(UUID(work_id))

            def compute_absent():
                for plural in ("virtualmachines", "virtualmachineinstances"):
                    try:
                        custom.get_namespaced_custom_object(
                            "kubevirt.io", "v1", "srw", plural, name
                        )
                    except smoke_module.ApiException as exc:
                        if exc.status != 404:
                            raise
                    else:
                        return False
                if not runtime["instance"]:
                    try:
                        self.fixture.core.read_namespaced_persistent_volume_claim(
                            name + "-rootdisk", "srw"
                        )
                    except smoke_module.ApiException as exc:
                        if exc.status != 404:
                            raise
                    else:
                        return False
                return True

            smoke_module.wait_for(
                compute_absent,
                "Waiting for exact owned VM/VMI/disk absence.",
                timeout=180,
            )
            receipt["computeAbsent"] = True
        require(not self.owned_jobs(), "Owned prepared Jobs remain after retirement.")

    def _cleanup_owned(self):
        self.evidence["cleanup"]["stage"] = "prepared-job-retirement"
        self.retire_owned_jobs()
        super().cleanup()
        self.evidence["cleanup"]["stage"] = "retained-instance-retirement"
        for uid in sorted(self.instance_uids):
            state = smoke_module.wait_for(
                lambda: self.detached(uid),
                "Waiting for the owned retained disk to detach.",
                timeout=180,
            )
            generation = state["generation"]

            def removed():
                current = self.gate.request("GET", "/api/workspace-instances/" + uid)
                require(
                    current["generation"] == generation,
                    "Retained workspace generation changed during cleanup.",
                )
                result = self.gate.request(
                    "DELETE",
                    "/api/workspace-instances/" + uid,
                    params={"expected_generation": generation},
                )
                return result.get("deleted") is True

            smoke_module.wait_for(
                removed, "Waiting for owned retained disk retirement.", timeout=180
            )
        self.evidence["cleanup"]["retainedInstancesReleased"] = True
        self.evidence["cleanup"]["stage"] = "prepared-artifact-retirement"
        for uid in sorted(self.cache_uids):

            def evicted():
                response = self.client.delete(
                    cutover.API + "/api/workspace-cache/" + uid,
                    headers=self.gate.headers,
                )
                require(
                    response.status_code in {200, 404, 409},
                    "Owned cache eviction failed.",
                )
                return response.status_code == 404 or (
                    response.status_code == 200
                    and response.json().get("deleted") is True
                )

            smoke_module.wait_for(
                evicted,
                "Waiting for the owned prepared artifact to retire.",
                timeout=180,
            )
        self.evidence["cleanup"]["preparedArtifactsRemoved"] = True
        self.evidence["cleanup"]["baseImportPolicy"] = (
            "Scoped base import follows the installed cache TTL."
        )

    def cleanup(self):
        try:
            self._cleanup_owned()
        finally:
            self.gate.login()
            tokens = self.gate.request("GET", "/api/mcp-tokens")
            for token in tokens:
                if (
                    token.get("name") == self.prefix + "mcp"
                    and token.get("revoked_at") is None
                ):
                    self.gate.request(
                        "DELETE", "/api/mcp-tokens/" + str(UUID(token["id"]))
                    )
            remaining = self.gate.request("GET", "/api/mcp-tokens")
            require(
                not any(
                    token.get("name") == self.prefix + "mcp"
                    and token.get("revoked_at") is None
                    for token in remaining
                ),
                "The owned MCP token remains after revocation.",
            )
            self.evidence["cleanup"]["mcpTokenRevoked"] = True
            self.token = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", default=DEFAULT_BASE)
    parser.add_argument("--fixture-image")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(
        image_reference(args.base_image)[2].startswith("sha256:"),
        "Use a digest-pinned base image.",
    )
    prefix = "cutover-" + uuid4().hex[:12] + "-"
    evidence = {
        "gate": "prepared-vm-srw-mcp",
        "context": "k3d-srw",
        "gateIdentity": prefix,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "baseImage": args.base_image,
        "provider": "deterministic fixture; real SRW harness and SSH tools",
    }
    admin = fixture = smoke = None
    with httpx.Client(
        verify=ssl.create_default_context(),
        trust_env=False,
        follow_redirects=False,
        timeout=60,
    ) as client:
        try:
            evidence["deployment"] = cutover.deployed_identity()
            evidence["preparationHosting"] = preparation_identity()
            evidence["agents"] = smoke_module.deployed_agents()
            image = cutover.remote_json(
                "import json; from orchestrator.services.manifest_experts import installed_srw_image; print(json.dumps(installed_srw_image()))"
            )
            fixture_image = args.fixture_image or smoke_module.publish_fixture()
            require(
                fixture_image.startswith(
                    "srw-registry:5000/srw-manifest-fixture@sha256:"
                )
                and len(fixture_image.rsplit(":", 1)[1]) == 64,
                "Use a verified local fixture image digest.",
            )
            evidence["fixtureImage"] = fixture_image
            admin = smoke_module.TemporaryAdmin(
                client,
                "srw-manifest-admin-"
                + prefix.removeprefix("cutover-").removesuffix("-"),
            )
            evidence["temporaryIdentity"] = {"name": admin.name}
            admin.create()
            evidence["temporaryIdentity"].update(
                keycloakUID=admin.uid,
                applicationUID=admin.app_uid,
                oauthClientUID=admin.oauth_uid,
            )
            model = "zz-srw-manifest-" + uuid4().hex[:12]
            fixture = smoke_module.Fixture(
                kube_config.new_client_from_config(context="k3d-srw"),
                "srw-prepared-gate-" + uuid4().hex[:12],
                model,
            )
            smoke = PreparedSmoke(
                client, prefix, image, fixture, admin, base_image=args.base_image
            )
            smoke.gate.login()
            fixture.create(fixture_image)
            evidence["fixture"] = {"namespace": fixture.name, "uid": fixture.uid}
            smoke.register_model()
            smoke.create_token()
            first = smoke.exercise("first")
            second = smoke.exercise("second")
            require(
                first["preparation"]["cacheHit"] is False
                and second["preparation"]["cacheHit"] is True,
                "Two fresh Jobs did not demonstrate one preparation plus a cache hit.",
            )
            require(
                first["preparation"]["uid"] == second["preparation"]["uid"]
                and first["preparation"]["diskSha256"]
                == second["preparation"]["diskSha256"],
                "Cache hit selected different prepared contents.",
            )
            require(
                first["vmUID"]
                and first["pvcUID"]
                and first["vmUID"] != second["vmUID"]
                and first["pvcUID"] != second["pvcUID"],
                "Fresh Jobs did not use distinct VMs and writable disks.",
            )
            retained = smoke.exercise("retained", retention="Retain")
            require(
                retained["instance"] is not None,
                "No retained workspace instance was recorded.",
            )
            uid = str(UUID(retained["instance"]["id"]))
            smoke_module.wait_for(
                lambda: smoke.detached(uid),
                "Waiting for retained workspace handoff.",
                timeout=180,
            )
            reused = smoke.exercise("reuse", binding={"instanceRef": {"uid": uid}})
            require(
                retained["pvcUID"]
                and retained["pvcUID"] == reused["pvcUID"]
                and retained["vmUID"] != reused["vmUID"]
                and int(reused["instance"]["generation"])
                == int(retained["instance"]["generation"]) + 1,
                "Retained handoff did not preserve one disk with a new execution generation.",
            )
            smoke.negative("failed-build")
            smoke.negative("cancelled-build", cancel=True)
            evidence["status"] = "passed"
        except GateFailure as exc:
            evidence["failure"] = str(exc)
        except Exception as exc:
            evidence["failure"] = (
                "Unexpected gate failure ("
                + type(exc).__name__
                + "); details suppressed."
            )
        finally:
            cleaned = True
            for name, cleanup in (
                ("workloads", smoke.cleanup if smoke else None),
                ("fixture", fixture.close if fixture else None),
                ("applicationIdentity", admin.delete_app_user if admin else None),
                ("keycloakIdentity", admin.revoke if admin else None),
            ):
                if cleanup:
                    try:
                        cleanup()
                    except Exception as exc:
                        cleaned = False
                        evidence["status"] = "failed"
                        evidence.setdefault("cleanupFailures", []).append(
                            {
                                "stage": name,
                                "error": type(exc).__name__,
                                **(
                                    {"reason": str(exc)}
                                    if isinstance(exc, GateFailure)
                                    else {}
                                ),
                            }
                        )
                        # Preserve the fixture when owned work has not retired;
                        # its exact identities remain available for recovery.
                        break
            if smoke:
                if cleaned:
                    smoke.evidence["cleanup"].update(complete=True, stage="complete")
                evidence["execution"] = smoke.evidence
            if admin:
                evidence.setdefault("temporaryIdentity", {})["cleanup"] = (
                    admin.cleanup_evidence
                )
    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"status": evidence["status"], "evidence": str(args.output)}))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
