#!/usr/bin/env python3
"""Bounded local-k3d gate for managed-repository lifecycle residues.

The gate is deliberately local-only and dry-run by default.  It creates one
disposable job row and one bare workspace Pod in ``k3d-srw/srw``.  The Pod is
held by the production process-zero finalizer while the gate exercises the
real managed ssh-agent scripts and the bounded cloud-mount shell boundary.

The script never prints child-process output.  In particular, private key
material is sent only on ``kubectl exec -i`` stdin and is neither included in
an argument nor copied into the report.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5


LOCAL_CONTEXT = "k3d-srw"
LOCAL_NAMESPACE = "srw"
LOCAL_CONFIRMATION = "LOCAL-K3D-DISPOSABLE"
PROCESS_ZERO_FINALIZER = "lifecycle.srw.dev/stateless-process-zero"
GATE_LABEL = "srw.io/local-managed-repository-residue-gate"
DEFAULT_ORCHESTRATOR_DEPLOYMENT = "srw-orchestrator"
_GATE_ID_RE = re.compile(r"srw-mr-residue-[0-9a-f]{12}\Z")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,510}\Z")


class SafetyError(RuntimeError):
    """The requested run is outside the hard-coded disposable boundary."""


class GateFailure(RuntimeError):
    """A redacted product-gate failure."""


@dataclass(frozen=True)
class GateConfig:
    context: str
    namespace: str
    orchestrator_deployment: str
    workspace_image: str | None
    gate_id: str
    owner_id: str
    run: bool
    confirmation: str | None
    timeout_seconds: int

    @property
    def pod_name(self) -> str:
        return self.gate_id

    @property
    def home_path(self) -> str:
        # Keep the ssh-agent socket below Linux's short AF_UNIX path limit.
        return f"/tmp/srw-mr-{self.gate_id.rsplit('-', 1)[-1]}"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


class KubectlRunner:
    """Exact-context kubectl runner whose exceptions never include output."""

    def __init__(self, config: GateConfig) -> None:
        self._config = config

    def argv(self, arguments: Sequence[str]) -> list[str]:
        return [
            "kubectl",
            "--context",
            self._config.context,
            "--namespace",
            self._config.namespace,
            *arguments,
        ]

    def run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        input_data: bytes | None = None,
        timeout: int | None = None,
        expect_success: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            self.argv(arguments),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout or self._config.timeout_seconds,
        )
        if expect_success and completed.returncode != 0:
            raise GateFailure(f"{operation} failed (rc={completed.returncode})")
        if not expect_success and completed.returncode == 0:
            raise GateFailure(f"{operation} unexpectedly succeeded")
        return CommandResult(completed.returncode, completed.stdout)


@dataclass
class SafeReport:
    gate_id: str
    mode: str
    phases: list[dict[str, str]] = field(default_factory=list)
    cleanup: str = "not_started"
    vm: str = "not_checked"

    def pass_phase(self, name: str) -> None:
        self.phases.append({"name": name, "result": "pass"})

    def fail_phase(self, name: str) -> None:
        self.phases.append({"name": name, "result": "fail"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "mode": self.mode,
            "phases": self.phases,
            "cleanup": self.cleanup,
            "vm": self.vm,
        }


def _validate_image(value: str) -> str:
    digest_suffix = value.rsplit("@", 1)[-1] if "@" in value else None
    if (
        not _IMAGE_RE.fullmatch(value)
        or "://" in value
        or (
            digest_suffix is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest_suffix) is None
        )
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise SafetyError("workspace image reference is malformed")
    return value


def validate_config(args: argparse.Namespace) -> GateConfig:
    if args.context != LOCAL_CONTEXT or args.namespace != LOCAL_NAMESPACE:
        raise SafetyError("this harness is restricted to k3d-srw/srw")
    if not _DNS_LABEL_RE.fullmatch(args.orchestrator_deployment):
        raise SafetyError("orchestrator deployment name is malformed")
    if not 60 <= args.timeout_seconds <= 600:
        raise SafetyError("timeout must be between 60 and 600 seconds")
    if args.run and args.confirm != LOCAL_CONFIRMATION:
        raise SafetyError(f"--run requires --confirm {LOCAL_CONFIRMATION}")
    if not args.run and args.confirm is not None:
        raise SafetyError("--confirm is accepted only with --run")
    gate_id = args.gate_id or f"srw-mr-residue-{secrets.token_hex(6)}"
    if not _GATE_ID_RE.fullmatch(gate_id):
        raise SafetyError("gate id must be srw-mr-residue followed by 12 hex digits")
    image = _validate_image(args.workspace_image) if args.workspace_image else None
    owner_id = str(uuid5(NAMESPACE_URL, f"local-k3d:{gate_id}"))
    return GateConfig(
        context=args.context,
        namespace=args.namespace,
        orchestrator_deployment=args.orchestrator_deployment,
        workspace_image=image,
        gate_id=gate_id,
        owner_id=owner_id,
        run=bool(args.run),
        confirmation=args.confirm,
        timeout_seconds=args.timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default=LOCAL_CONTEXT)
    parser.add_argument("--namespace", default=LOCAL_NAMESPACE)
    parser.add_argument(
        "--orchestrator-deployment", default=DEFAULT_ORCHESTRATOR_DEPLOYMENT
    )
    parser.add_argument("--workspace-image")
    parser.add_argument("--gate-id")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm")
    return parser


def build_workspace_pod_manifest(config: GateConfig, image: str) -> dict[str, Any]:
    """Return the one exact, secret-free disposable Pod manifest."""

    _validate_image(image)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": config.pod_name,
            "namespace": config.namespace,
            "labels": {
                "app": "srw-workspace",
                "srw/component": "workspace",
                "srw.io/component": "agent-workspace",
                "srw.io/network-tier": "internet-only",
                "srw/job-id": config.owner_id,
                GATE_LABEL: config.gate_id,
            },
            "annotations": {"srw.io/managed-by": "lifecycle-reconciler"},
            "finalizers": [PROCESS_ZERO_FINALIZER],
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 10,
            "containers": [
                {
                    "name": "workspace",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "bash",
                        "-c",
                        "trap 'exit 0' TERM INT; while :; do sleep 1; done",
                    ],
                }
            ],
        },
    }


_CONTROLLER_PROGRAM = r"""
import asyncio
import json
import os
import sys
from uuid import UUID

for candidate in ("/app", "/app/orchestrator"):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from database.postgres import PostgresDB
from services.container_provisioner import ContainerProvisioner
from services.workspace_lifecycle import WorkspaceOwner


async def run():
    mode = sys.argv[1]
    owner_id = sys.argv[2] if len(sys.argv) > 2 else ""
    db = PostgresDB(min_connections=1, max_connections=1)
    await db.connect()
    try:
        if mode == "preflight":
            table = await db.fetchval(
                "SELECT to_regclass('public.managed_repository_process_zero_receipts')"
            )
            if table is None or not hasattr(
                ContainerProvisioner, "_release_process_zero_finalizer"
            ):
                raise RuntimeError("gate artifact missing")
            print("READY")
            return
        owner_uuid = UUID(owner_id)
        if mode == "owner-create":
            result = await db.execute(
                "INSERT INTO jobs (id, description, status, context) "
                "VALUES ($1, 'local managed repository residue gate', "
                "'processing', '{}'::jsonb)",
                owner_uuid,
            )
            if result != "INSERT 0 1":
                raise RuntimeError("owner create refused")
            print("OWNER_CREATED")
            return
        if mode == "owner-bind":
            runtime_incarnation = str(UUID(sys.argv[3]))
            state = json.dumps(
                {
                    "provisioner": "k8s",
                    "status": "ready",
                    "_runtime_incarnation": runtime_incarnation,
                }
            )
            result = await db.execute(
                "UPDATE jobs SET context = jsonb_set(COALESCE(context, '{}'::jsonb), "
                "'{workspace_container}', $2::jsonb, true) "
                "WHERE id = $1 AND NOT COALESCE(context, '{}'::jsonb) "
                "? 'workspace_container'",
                owner_uuid,
                state,
            )
            if result != "UPDATE 1":
                raise RuntimeError("owner bind refused")
            print("OWNER_BOUND")
            return
        if mode in {"pod-delete", "finalizer-release"}:
            expected_uid = str(UUID(sys.argv[3]))
            pod_name = sys.argv[4]
            namespace = sys.argv[5]
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.available or provisioner._namespace != namespace:
                raise RuntimeError("kubernetes provisioner unavailable")
            owner = WorkspaceOwner.job(owner_id)
            if mode == "pod-delete":
                pod = await asyncio.to_thread(
                    provisioner._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=namespace,
                )
                observed = provisioner._require_workspace_pod_owner(
                    pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    allow_terminating=True,
                    expected_pod_name=pod_name,
                    expected_component="workspace",
                )
                if observed != expected_uid:
                    raise RuntimeError("pod replacement refused")
                await asyncio.to_thread(
                    provisioner._core_api.delete_namespaced_pod,
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=5,
                    body={"preconditions": {"uid": expected_uid}},
                )
                print("POD_DELETE_ACCEPTED")
                return
            released = await provisioner._release_process_zero_finalizer(
                owner,
                pod_name=pod_name,
                expected_runtime_incarnation=expected_uid,
                scope="workspace_container",
                expected_component="workspace",
            )
            current = await db.managed_repository_workspace_process_zero_is_current(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                provisioner="k8s",
                runtime_incarnation=expected_uid,
            )
            if not released or not current:
                raise RuntimeError("process-zero finalizer release refused")
            print("FINALIZER_RELEASED")
            return
        if mode == "owner-delete":
            result = await db.execute("DELETE FROM jobs WHERE id = $1", owner_uuid)
            if result not in {"DELETE 1", "DELETE 0"}:
                raise RuntimeError("owner delete refused")
            print("OWNER_DELETED")
            return
        raise RuntimeError("unknown gate controller mode")
    finally:
        await db.close()


asyncio.run(run())
"""


def controller_program() -> str:
    return _CONTROLLER_PROGRAM


class ManagedRepositoryResidueGate:
    def __init__(self, config: GateConfig, runner: KubectlRunner) -> None:
        self.config = config
        self.runner = runner
        self.report = SafeReport(gate_id=config.gate_id, mode="run")
        self.orchestrator_pod: str | None = None
        self.workspace_image: str | None = config.workspace_image
        self.owner_created = False
        self.pod_uid: str | None = None
        self.owner_bound = False
        self.private_home = config.home_path

    @staticmethod
    def _decode_json(output: bytes, *, operation: str) -> dict[str, Any]:
        try:
            value = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateFailure(f"{operation} returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise GateFailure(f"{operation} returned malformed JSON")
        return value

    def _pod_json(self) -> dict[str, Any] | None:
        result = self.runner.run(
            ["get", "pod", self.config.pod_name, "-o", "json", "--ignore-not-found"],
            operation="workspace Pod inspection",
        )
        if not result.stdout.strip():
            return None
        return self._decode_json(result.stdout, operation="workspace Pod inspection")

    def _select_orchestrator(self) -> None:
        deployment = self._decode_json(
            self.runner.run(
                [
                    "get",
                    "deployment",
                    self.config.orchestrator_deployment,
                    "-o",
                    "json",
                ],
                operation="orchestrator deployment inspection",
            ).stdout,
            operation="orchestrator deployment inspection",
        )
        selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels")
        if not isinstance(selector, dict) or not selector:
            raise GateFailure("orchestrator deployment selector is unavailable")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in selector.items()
        ):
            raise GateFailure("orchestrator deployment selector is malformed")
        selector_text = ",".join(
            f"{key}={value}" for key, value in sorted(selector.items())
        )
        pods = self._decode_json(
            self.runner.run(
                ["get", "pods", "-l", selector_text, "-o", "json"],
                operation="orchestrator Pod inventory",
            ).stdout,
            operation="orchestrator Pod inventory",
        ).get("items")
        if not isinstance(pods, list):
            raise GateFailure("orchestrator Pod inventory is malformed")
        candidates: list[str] = []
        for pod in pods:
            if not isinstance(pod, dict):
                continue
            metadata = pod.get("metadata") or {}
            status = pod.get("status") or {}
            statuses = status.get("containerStatuses") or []
            if (
                metadata.get("deletionTimestamp") is None
                and status.get("phase") == "Running"
                and any(
                    item.get("name") == "orchestrator" and item.get("ready") is True
                    for item in statuses
                    if isinstance(item, dict)
                )
                and isinstance(metadata.get("name"), str)
            ):
                candidates.append(metadata["name"])
        if not candidates:
            raise GateFailure("no Ready orchestrator Pod is available")
        self.orchestrator_pod = sorted(candidates)[0]

    def _controller(self, mode: str, *arguments: str, marker: str) -> None:
        if self.orchestrator_pod is None:
            raise GateFailure("orchestrator Pod was not selected")
        result = self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod,
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                mode,
                *arguments,
            ],
            operation=f"gate controller {mode}",
            input_data=_CONTROLLER_PROGRAM.encode(),
        )
        lines = {
            line.strip() for line in result.stdout.decode(errors="replace").splitlines()
        }
        if marker not in lines:
            raise GateFailure(f"gate controller {mode} did not acknowledge")

    def _discover_workspace_image(self) -> None:
        if self.workspace_image is not None:
            return
        if self.orchestrator_pod is None:
            raise GateFailure("orchestrator Pod was not selected")
        result = self.runner.run(
            [
                "exec",
                self.orchestrator_pod,
                "-c",
                "orchestrator",
                "--",
                "sh",
                "-c",
                'printf "%s" "$WORKSPACE_IMAGE"',
            ],
            operation="workspace image discovery",
        )
        try:
            candidate = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GateFailure("workspace image discovery was malformed") from exc
        self.workspace_image = _validate_image(candidate)

    def _pod_exec(
        self,
        command: str,
        *,
        operation: str,
        input_data: bytes | None = None,
        expect_success: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        return self.runner.run(
            [
                "exec",
                "-i",
                self.config.pod_name,
                "-c",
                "workspace",
                "--",
                "bash",
                "-c",
                command,
            ],
            operation=operation,
            input_data=input_data,
            expect_success=expect_success,
            timeout=timeout,
        )

    def _runtime_artifact_preflight(self) -> None:
        """Prove the actual workspace container contains both repaired seams."""

        self._pod_exec(
            "python3 - <<'PY'\n"
            "from src.core.managed_repository import (\n"
            "    managed_repository_agent_launch_command,\n"
            "    managed_repository_agent_retirement_command,\n"
            "    managed_repository_agent_zero_command,\n"
            ")\n"
            "from src.services.cloud_mount import RcloneMountManager\n"
            "assert callable(managed_repository_agent_launch_command)\n"
            "assert callable(managed_repository_agent_retirement_command)\n"
            "assert callable(managed_repository_agent_zero_command)\n"
            "assert callable(RcloneMountManager._bounded_remote_script_command)\n"
            "PY\n"
            "command -v ssh-agent >/dev/null\n"
            "command -v ssh-add >/dev/null\n"
            "command -v rclone >/dev/null\n"
            "command -v git >/dev/null",
            operation="running workspace artifact inspection",
        )

    def _wait_running(self) -> str:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is not None:
                metadata = pod.get("metadata") or {}
                status = pod.get("status") or {}
                uid = metadata.get("uid")
                statuses = status.get("containerStatuses") or []
                if (
                    status.get("phase") == "Running"
                    and isinstance(uid, str)
                    and all(
                        isinstance(item, dict) and item.get("ready") is True
                        for item in statuses
                    )
                    and statuses
                ):
                    return str(UUID(uid))
                if status.get("phase") in {"Failed", "Succeeded"}:
                    raise GateFailure("workspace Pod terminated before the gate")
            time.sleep(1)
        raise GateFailure("workspace Pod did not become Ready")

    @staticmethod
    def _pod_is_exact_terminal(pod: dict[str, Any]) -> bool:
        metadata = pod.get("metadata") or {}
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        if metadata.get("deletionTimestamp") is None:
            return False
        declared = {
            item.get("name")
            for item in spec.get("containers") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        statuses = {
            item.get("name"): item
            for item in status.get("containerStatuses") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if declared and all(
            isinstance(
                (statuses.get(name) or {}).get("state", {}).get("terminated"), dict
            )
            for name in declared
        ):
            return True
        # Match the production narrow never-scheduled proof.  An assigned Pod
        # with missing statuses remains deliberately ambiguous.
        return not spec.get("nodeName") and not any(
            isinstance(item.get("state", {}).get("running"), dict)
            for item in statuses.values()
        )

    def _wait_terminal(self) -> None:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is None:
                raise GateFailure("workspace Pod disappeared before finalizer release")
            observed_uid = str((pod.get("metadata") or {}).get("uid") or "")
            if observed_uid != self.pod_uid:
                raise GateFailure("workspace Pod was replaced during termination")
            if self._pod_is_exact_terminal(pod):
                return
            time.sleep(1)
        raise GateFailure("workspace Pod did not reach exact terminal state")

    def _wait_absent(self) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is None:
                return
            observed_uid = str((pod.get("metadata") or {}).get("uid") or "")
            if self.pod_uid is not None and observed_uid != self.pod_uid:
                raise GateFailure("same-name replacement appeared during cleanup")
            time.sleep(1)
        raise GateFailure("workspace Pod remained after finalizer release")

    def _agent_state(self, authority_id: str) -> dict[str, str]:
        slug = authority_id.replace("-", "")
        state_path = f"{self.private_home}/.ssh/srw-managed/agents/{slug}.state"
        command = (
            f"test -f {state_path!r}; "
            f'awk -F= \'$1 == "pid" || $1 == "starttime" || '
            f'$1 == "generation" || $1 == "workspace_generation" || '
            f'$1 == "runtime_incarnation" {{print $1 "=" $2}}\' {state_path!r}'
        )
        result = self._pod_exec(command, operation="ssh-agent state inspection")
        values: dict[str, str] = {}
        for line in result.stdout.decode(errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        required = {
            "pid",
            "starttime",
            "generation",
            "workspace_generation",
            "runtime_incarnation",
        }
        if set(values) != required or not values["pid"].isdigit():
            raise GateFailure("ssh-agent state receipt is malformed")
        return values

    def _exercise_managed_agent(self) -> tuple[str, int]:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        # Imported lazily so --help/plan and safety unit tests need no crypto or
        # orchestrator runtime dependencies.
        root = Path(__file__).resolve().parent.parent
        for candidate in (root, root / "orchestrator"):
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        from services.managed_repository_authority import _deploy_keypair
        from src.core.managed_repository import (
            managed_repository_agent_launch_command,
            managed_repository_agent_retirement_command,
            managed_repository_agent_zero_command,
        )

        authority_id = str(uuid4())
        private_key, _public_key, _fingerprint = _deploy_keypair()
        private_buffer = bytearray(private_key.encode())
        private_key = ""
        try:
            for generation in (1, 2):
                command = managed_repository_agent_launch_command(
                    home_path=self.private_home,
                    authority_id=authority_id,
                    generation=generation,
                    preserve_existing=True,
                    workspace_generation=self.config.owner_id,
                    runtime_incarnation=self.pod_uid,
                )
                self._pod_exec(
                    command,
                    operation=f"managed ssh-agent generation {generation} launch",
                    input_data=bytes(private_buffer),
                )
                state = self._agent_state(authority_id)
                if (
                    state["generation"] != str(generation)
                    or state["workspace_generation"] != self.config.owner_id
                    or state["runtime_incarnation"] != self.pod_uid
                ):
                    raise GateFailure("managed ssh-agent receipt changed authority")
                if generation == 1:
                    first_pid = int(state["pid"])
                else:
                    second_pid = int(state["pid"])
            if first_pid == second_pid:
                raise GateFailure("managed ssh-agent generation did not rotate")

            stale = managed_repository_agent_launch_command(
                home_path=self.private_home,
                authority_id=authority_id,
                generation=1,
                preserve_existing=True,
                workspace_generation=self.config.owner_id,
                runtime_incarnation=self.pod_uid,
            )
            self._pod_exec(
                stale,
                operation="stale managed ssh-agent replacement refusal",
                input_data=bytes(private_buffer),
                expect_success=False,
            )
            stable = self._agent_state(authority_id)
            if stable["generation"] != "2" or int(stable["pid"]) != second_pid:
                raise GateFailure("stale replacement mutated managed ssh-agent")

            # A live agent must make the independent zero proof fail.  The
            # private marker scan checks that stdin was not persisted to disk.
            self._pod_exec(
                managed_repository_agent_zero_command(home_path=self.private_home),
                operation="live managed ssh-agent zero refusal",
                expect_success=False,
            )
            self._pod_exec(
                f"if grep -Rqs -- 'PRIVATE KEY' {self.private_home!r}; then exit 91; fi",
                operation="managed key persistence scan",
            )
            retirement = managed_repository_agent_retirement_command(
                home_path=self.private_home,
                authority_ids=None,
            )
            return retirement, second_pid
        finally:
            for index in range(len(private_buffer)):
                private_buffer[index] = 0

    def _exercise_cloud_timeout(self) -> None:
        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from src.services.cloud_mount import RcloneMountManager

        marker = f"srw-cloud-{self.config.gate_id.rsplit('-', 1)[-1]}"
        script_path = f"/tmp/{marker}.sh"
        target_path = f"/tmp/{marker}-mount"
        script = f"""#!/usr/bin/env bash
set -eu
mkdir -p {target_path!r}
_srw_pid=''
cleanup() {{
  if test -n "$_srw_pid"; then
    kill "$_srw_pid" 2>/dev/null || true
    wait "$_srw_pid" 2>/dev/null || true
  fi
  rmdir {target_path!r} 2>/dev/null || true
}}
trap cleanup EXIT TERM INT
rclone rcd --rc-no-auth --rc-addr 127.0.0.1:0 --rc-baseurl /{marker} >/dev/null 2>&1 &
_srw_pid=$!
sleep 1
kill -0 "$_srw_pid"
sleep 600
"""
        self._pod_exec(
            "command -v rclone >/dev/null; command -v git >/dev/null; "
            f"umask 077; cat > {script_path!r}",
            operation="cloud timeout fixture staging",
            input_data=script.encode(),
        )
        bounded, outer_timeout = RcloneMountManager._bounded_remote_script_command(
            script_path, timeout=2
        )
        self._pod_exec(
            bounded,
            operation="bounded optional cloud timeout",
            expect_success=False,
            timeout=outer_timeout + 15,
        )
        residue_scan = f"""python3 - {marker!r} {script_path!r} {target_path!r} <<'PY'
import os
import pathlib
import sys

marker, script_path, target_path = sys.argv[1:]
if pathlib.Path(script_path).exists():
    raise SystemExit(1)
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        command = (proc / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'rclone\\0rcd\\0' in command and marker.encode() in command:
        raise SystemExit(2)
if os.path.ismount(target_path):
    raise SystemExit(3)
pathlib.Path(target_path).rmdir() if pathlib.Path(target_path).exists() else None
PY
git --version >/dev/null
"""
        self._pod_exec(
            residue_scan,
            operation="optional cloud timeout residue and unrelated Git check",
        )

    def _check_vm_availability(self) -> None:
        result = self.runner.run(
            ["api-resources", "--api-group=kubevirt.io", "-o", "name"],
            operation="KubeVirt availability check",
            expect_success=True,
        )
        resources = {
            line.strip() for line in result.stdout.decode(errors="replace").splitlines()
        }
        self.report.vm = (
            "available_not_mutated"
            if "virtualmachines.kubevirt.io" in resources
            else "blocked_kubevirt_unavailable"
        )

    def _create_owner(self) -> None:
        self._controller("owner-create", self.config.owner_id, marker="OWNER_CREATED")
        self.owner_created = True

    def _create_pod(self) -> None:
        if self.workspace_image is None:
            raise GateFailure("workspace image is unavailable")
        manifest = build_workspace_pod_manifest(self.config, self.workspace_image)
        self.runner.run(
            ["create", "-f", "-"],
            operation="disposable workspace Pod creation",
            input_data=json.dumps(manifest, separators=(",", ":")).encode(),
        )
        self.pod_uid = self._wait_running()
        self._controller(
            "owner-bind",
            self.config.owner_id,
            self.pod_uid,
            marker="OWNER_BOUND",
        )
        self.owner_bound = True

    def _delete_and_release(self) -> None:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        wrong_uid = str(uuid4())
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "pod-delete",
                self.config.owner_id,
                wrong_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="replacement UID deletion refusal",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        pod = self._pod_json()
        if pod is None or str((pod.get("metadata") or {}).get("uid")) != self.pod_uid:
            raise GateFailure("replacement refusal did not preserve the exact Pod")
        self._controller(
            "pod-delete",
            self.config.owner_id,
            self.pod_uid,
            self.config.pod_name,
            self.config.namespace,
            marker="POD_DELETE_ACCEPTED",
        )
        self._wait_terminal()
        # A mismatched runtime must neither mint a receipt nor remove the
        # finalizer from the retained exact object.
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "finalizer-release",
                self.config.owner_id,
                wrong_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="replacement UID finalizer refusal",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        retained = self._pod_json()
        finalizers = (retained or {}).get("metadata", {}).get("finalizers") or []
        if (
            retained is None
            or str((retained.get("metadata") or {}).get("uid")) != self.pod_uid
            or PROCESS_ZERO_FINALIZER not in finalizers
        ):
            raise GateFailure("replacement refusal did not retain the finalizer")
        self._controller(
            "finalizer-release",
            self.config.owner_id,
            self.pod_uid,
            self.config.pod_name,
            self.config.namespace,
            marker="FINALIZER_RELEASED",
        )
        self._wait_absent()

    def cleanup_exact(self, retirement_command: str | None = None) -> bool:
        """Best-effort exact cleanup; never force-remove the finalizer."""

        try:
            pod = self._pod_json()
            if pod is not None:
                metadata = pod.get("metadata") or {}
                observed_uid = str(metadata.get("uid") or "")
                if self.pod_uid is None:
                    self.pod_uid = str(UUID(observed_uid))
                elif observed_uid != self.pod_uid:
                    return False
                if self.owner_created and not self.owner_bound:
                    self._controller(
                        "owner-bind",
                        self.config.owner_id,
                        self.pod_uid,
                        marker="OWNER_BOUND",
                    )
                    self.owner_bound = True
                if metadata.get("deletionTimestamp") is None:
                    if retirement_command is not None:
                        self._pod_exec(
                            retirement_command,
                            operation="cleanup managed ssh-agent retirement",
                        )
                    self._controller(
                        "pod-delete",
                        self.config.owner_id,
                        self.pod_uid,
                        self.config.pod_name,
                        self.config.namespace,
                        marker="POD_DELETE_ACCEPTED",
                    )
                self._wait_terminal()
                self._controller(
                    "finalizer-release",
                    self.config.owner_id,
                    self.pod_uid,
                    self.config.pod_name,
                    self.config.namespace,
                    marker="FINALIZER_RELEASED",
                )
                self._wait_absent()
            if self.owner_created:
                self._controller(
                    "owner-delete", self.config.owner_id, marker="OWNER_DELETED"
                )
                self.owner_created = False
            return True
        except Exception:
            return False

    def run(self) -> SafeReport:
        retirement_command: str | None = None
        gate_succeeded = False
        try:
            self._select_orchestrator()
            self._controller("preflight", marker="READY")
            self._discover_workspace_image()
            self.report.pass_phase("artifact_and_local_cluster_preflight")
            self._check_vm_availability()
            self._create_owner()
            self._create_pod()
            self._runtime_artifact_preflight()
            self.report.pass_phase("exact_workspace_pod_uid_binding")
            retirement_command, _pid = self._exercise_managed_agent()
            self.report.pass_phase("managed_ssh_agent_generation_and_refusal")
            self._exercise_cloud_timeout()
            self.report.pass_phase("bounded_optional_cloud_timeout")
            self._pod_exec(
                retirement_command,
                operation="terminal managed ssh-agent retirement",
            )
            self._delete_and_release()
            self.report.pass_phase("terminal_receipt_and_finalizer_release")
            self._controller(
                "owner-delete", self.config.owner_id, marker="OWNER_DELETED"
            )
            self.owner_created = False
            gate_succeeded = True
        except Exception:
            self.report.fail_phase("gate_execution")
        finally:
            cleaned = self.cleanup_exact(retirement_command)
            self.report.cleanup = "exact_zero" if cleaned else "fail_closed_residue"
        if not gate_succeeded or self.report.cleanup != "exact_zero":
            raise GateFailure(json.dumps(self.report.as_dict(), sort_keys=True))
        return self.report


def plan_report(config: GateConfig) -> SafeReport:
    report = SafeReport(gate_id=config.gate_id, mode="plan")
    for phase in (
        "artifact_and_local_cluster_preflight",
        "exact_workspace_pod_uid_binding",
        "managed_ssh_agent_generation_and_refusal",
        "bounded_optional_cloud_timeout",
        "terminal_receipt_and_finalizer_release",
        "exact_cleanup",
    ):
        report.phases.append({"name": phase, "result": "planned"})
    report.cleanup = "planned"
    report.vm = "preflight_only"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = validate_config(build_parser().parse_args(argv))
        if not config.run:
            print(json.dumps(plan_report(config).as_dict(), sort_keys=True))
            return 0
        report = ManagedRepositoryResidueGate(config, KubectlRunner(config)).run()
        print(json.dumps(report.as_dict(), sort_keys=True))
        return 0
    except SafetyError as exc:
        print(json.dumps({"mode": "refused", "error": str(exc)}, sort_keys=True))
        return 2
    except GateFailure as exc:
        # GateFailure text is constructed only from fixed operation labels or a
        # SafeReport.  It never contains subprocess stdout/stderr/arguments.
        print(json.dumps({"mode": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"mode": "failed", "error": "unexpected_gate_failure"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
