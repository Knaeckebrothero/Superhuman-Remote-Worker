"""Independent SSH workspaces for native manifests.

Instances retain a PVC across attachments; each attachment gets a new pod and
new client/host SSH identities. The resource/execution service owns authorization,
the exclusive attachment lock, encrypted key storage, initialized state, retention
decisions and durable UID publication. This component never replaces a lost PVC
or treats a cooperative SSH response as proof that old processes stopped.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import ipaddress
import json
import shlex
from uuid import UUID

from orchestrator.services.generic_harness_runtime import (
    GenericBindings,
    GenericBoundFile,
    GenericHarnessRuntime,
    GenericLaunchPlan,
    GenericPodObservation,
    GenericRuntimeError,
    GenericRuntimePolicy,
    _field,
    _matches,
    _observation,
    build_generic_launch,
)
from orchestrator.services.pinned_k8s_effect import (
    run_bounded_k8s_call,
    run_bounded_k8s_mutation,
)


SSH_PORT = 30022
SSH_USER = "agent-host"
WORKSPACE_HOME = "/home/agent-host"
WORKSPACE_ROOT = WORKSPACE_HOME + "/workspace"
_SSH_FILES = "/run/srw-workspace"
_CAPABILITIES = [
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "SETUID",
    "SETGID",
    "SYS_CHROOT",
    "KILL",
    "AUDIT_WRITE",
]


class ManifestWorkspaceError(GenericRuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceRuntimeIdentity:
    instance_id: str
    generation: int
    execution_id: str

    def __post_init__(self):
        object.__setattr__(self, "instance_id", str(UUID(str(self.instance_id))))
        object.__setattr__(self, "execution_id", str(UUID(str(self.execution_id))))
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or not 1 <= self.generation <= 2**63 - 1
        ):
            raise ValueError(
                "Workspace generation must be a positive database integer."
            )

    @property
    def pod_name(self) -> str:
        return f"srw-ws-{UUID(self.instance_id).hex}-g{self.generation}"

    @property
    def pvc_name(self) -> str:
        return f"srw-ws-{UUID(self.instance_id).hex}"

    @property
    def volume_labels(self) -> dict[str, str]:
        return {
            "srw/managed-by": "manifest-workspace-runtime",
            "srw.io/workspace-instance": self.instance_id,
        }

    @property
    def labels(self) -> dict[str, str]:
        return {
            **self.volume_labels,
            "srw.io/workspace-generation": str(self.generation),
            "srw.io/execution-id": self.execution_id,
        }


@dataclass(frozen=True, repr=False)
class WorkspaceSSHMaterial:
    client_private_key: str = field(repr=False)
    client_public_key: str
    host_private_key: str = field(repr=False)
    host_public_key: str

    def __repr__(self):
        return "WorkspaceSSHMaterial(<private material omitted>)"

    def to_json(self) -> str:
        """Secret plaintext for immediate encryption, never a resource snapshot."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> WorkspaceSSHMaterial:
        obj = json.loads(value)
        if (
            not isinstance(obj, dict)
            or set(obj)
            != {
                "client_private_key",
                "client_public_key",
                "host_private_key",
                "host_public_key",
            }
            or not all(isinstance(item, str) for item in obj.values())
        ):
            raise ValueError("Invalid encrypted workspace SSH material payload.")
        material = cls(**obj)
        material.validate()
        return material

    def validate(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        for private, public in (
            (self.client_private_key, self.client_public_key),
            (self.host_private_key, self.host_public_key),
        ):
            try:
                key = serialization.load_ssh_private_key(
                    private.encode(), password=None
                )
                actual = (
                    key.public_key()
                    .public_bytes(
                        serialization.Encoding.OpenSSH,
                        serialization.PublicFormat.OpenSSH,
                    )
                    .decode()
                )
                if not isinstance(key, Ed25519PrivateKey) or actual != public:
                    raise ValueError()
            except Exception:
                raise ValueError(
                    "Workspace SSH material is not a matching Ed25519 key pair."
                ) from None


def generate_workspace_ssh_material() -> WorkspaceSSHMaterial:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def pair():
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
            .decode()
        )
        return private, public

    return WorkspaceSSHMaterial(*pair(), *pair())


@dataclass(frozen=True)
class WorkspaceLaunchPlan:
    identity: WorkspaceRuntimeIdentity
    runtime: GenericLaunchPlan = field(repr=False)
    volume: dict = field(repr=False)
    initialize: bool


@dataclass(frozen=True)
class WorkspacePodObservation(GenericPodObservation):
    initialization_succeeded: bool = False


def build_workspace_launch(
    identity: WorkspaceRuntimeIdentity,
    recipe: dict,
    ssh: WorkspaceSSHMaterial,
    *,
    namespace: str,
    default_image: str,
    initialize: bool,
    storage_class_name: str | None = None,
    bindings: GenericBindings | None = None,
    ingress: tuple[dict, ...] = (),
    egress: tuple[dict, ...] = (),
) -> WorkspaceLaunchPlan:
    """Build one reserved attachment using the workspace SSH image protocol.

    ``initialize`` comes from the authoritative instance record. Initialization
    commands run as the unprivileged workspace user, affect its persistent home,
    and may be retried after partial failure. Image preparation/cache builds, VM
    providers and network profile resolution are separate admission capabilities.
    """
    if recipe.get("backend") != "sandbox":
        raise ValueError("This runtime supports sandbox workspace templates only.")
    environment = recipe.get("environment", {})
    if environment.get("prepare") or environment.get("cache") == "Rebuild":
        raise ValueError(
            "Workspace image preparation/cache builds are not implemented."
        )
    if "network" in recipe:
        raise ValueError(
            "Workspace network profiles must be resolved to authorized rules before launch."
        )
    if not isinstance(initialize, bool):
        raise ValueError("Workspace initialization state must come from the server.")
    ssh.validate()
    resources = recipe.get("resources", {})
    from orchestrator.services.generic_harness_runtime import _bytes

    if _bytes(resources.get("storage", "10Gi")) > _bytes("100Gi"):
        raise ValueError(
            "Workspace storage exceeds the installation's 100Gi hosting ceiling."
        )
    cpu, memory = resources.get("cpu", 1), resources.get("memory", "2Gi")
    image = environment.get("image", default_image)
    if not isinstance(image, str) or not image.strip():
        raise ValueError("A compatible workspace image is required.")
    bindings = bindings or GenericBindings()
    for item in bindings.files:
        if any(
            item.path == path
            or item.path.startswith(path + "/")
            or path.startswith(item.path + "/")
            for path in ("/tmp/ssh-pubkey", "/tmp/ssh-hostkey", "/var/lib/srw-system")
        ):
            raise ValueError(
                "Connector files cannot replace workspace SSH identity mounts."
            )
    spec = {
        "execution": {
            "expert": {
                "inline": {
                    "runtime": {
                        "image": image,
                        "pullPolicy": environment.get("pullPolicy", "IfNotPresent"),
                        "resources": {
                            "requests": {"cpu": cpu, "memory": memory},
                            "limits": {"cpu": cpu, "memory": memory},
                        },
                        "env": {
                            "SRW_WORKSPACE_OWNER_KIND": "job",
                            "SRW_WORKSPACE_OWNER_ID": identity.instance_id,
                        },
                    }
                }
            }
        },
    }
    # Reuse delivery/quantity validation and the exact-pod lifecycle. Workspace
    # storage and SSH identity are attached only to this separate workspace pod.
    base = build_generic_launch(
        identity,
        spec,
        namespace=namespace,
        bindings=bindings,
        policy=GenericRuntimePolicy(run_as_user=0, termination_grace_seconds=30),
    )
    pod = deepcopy(base.pod)
    container = pod["spec"]["containers"][0]
    container["name"] = "workspace"
    container["securityContext"]["capabilities"]["add"] = list(_CAPABILITIES)
    container.setdefault("env", []).append(
        {
            "name": "SRW_WORKSPACE_RUNTIME_UID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
        }
    )
    container["ports"] = [{"name": "ssh", "containerPort": SSH_PORT}]
    container["readinessProbe"] = {
        "tcpSocket": {"port": SSH_PORT},
        "periodSeconds": 2,
        "timeoutSeconds": 1,
        "failureThreshold": 15,
    }
    pod["spec"].setdefault("volumes", []).extend(
        [
            {
                "name": "workspace-data",
                "persistentVolumeClaim": {"claimName": identity.pvc_name},
            },
            {"name": "workspace-identity", "emptyDir": {"sizeLimit": "16Mi"}},
            {
                "name": "workspace-pubkey",
                "secret": {
                    "secretName": identity.pod_name,
                    "items": [
                        {"key": "ssh-publickey", "path": "ssh-publickey", "mode": 0o644}
                    ],
                },
            },
            {
                "name": "workspace-hostkey",
                "secret": {
                    "secretName": identity.pod_name,
                    "items": [
                        {
                            "key": "ssh-host-private",
                            "path": "ssh_host_ed25519_key",
                            "mode": 0o400,
                        },
                        {
                            "key": "ssh-host-public",
                            "path": "ssh_host_ed25519_key.pub",
                            "mode": 0o444,
                        },
                    ],
                },
            },
        ]
    )
    container.setdefault("volumeMounts", []).extend(
        [
            {"name": "workspace-data", "mountPath": WORKSPACE_HOME},
            {"name": "workspace-identity", "mountPath": "/var/lib/srw-system"},
            {
                "name": "workspace-pubkey",
                "mountPath": "/tmp/ssh-pubkey",
                "readOnly": True,
            },
            {
                "name": "workspace-hostkey",
                "mountPath": "/tmp/ssh-hostkey",
                "readOnly": True,
            },
        ]
    )
    secret = (
        deepcopy(base.delivery_secret)
        if base.delivery_secret is not None
        else {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": identity.pod_name,
                "namespace": namespace,
                "labels": identity.labels,
            },
            "immutable": True,
            "type": "Opaque",
            "data": {},
        }
    )
    for key, value in {
        # No shared gateway CA or shared worker key. The client key is scoped to
        # one attachment; forwarding is restricted to workspace loopback by the
        # image's existing sshd configuration.
        "ssh-publickey": ssh.client_public_key + "\n",
        "ssh-host-private": ssh.host_private_key,
        "ssh-host-public": ssh.host_public_key + "\n",
    }.items():
        secret["data"][key] = base64.b64encode(value.encode()).decode("ascii")
    network_policy = deepcopy(base.network_policy)
    network_policy["spec"]["ingress"] = deepcopy(list(ingress))
    network_policy["spec"]["egress"] = deepcopy(list(egress))
    steps = recipe.get("initialize", []) if initialize else []
    if steps:
        init_containers = []
        for number, step in enumerate(steps):
            # Initialization mutates only the persistent user's environment.
            # Root setup establishes ownership for a freshly bound volume; no
            # authored command is ever executed as root.
            command = shlex.join(step["command"])
            user_script = f"cd {shlex.quote(WORKSPACE_ROOT)} && exec {command}"
            init_script = (
                "set -eu\n"
                f"install -d -o 1000 -g 1000 -m 0755 {WORKSPACE_HOME} {WORKSPACE_ROOT}\n"
                f"exec su -s /bin/sh {SSH_USER} -c {shlex.quote(user_script)}\n"
            )
            init_containers.append(
                {
                    "name": f"initialize-{number}",
                    "image": image,
                    "imagePullPolicy": container["imagePullPolicy"],
                    "command": ["/bin/sh", "-c", init_script],
                    "env": deepcopy(
                        [
                            item
                            for item in container.get("env", [])
                            if item["name"] != "SRW_WORKSPACE_RUNTIME_UID"
                        ]
                    ),
                    "volumeMounts": deepcopy(
                        [
                            item
                            for item in container["volumeMounts"]
                            if item["name"] in {"workspace-data", "delivery"}
                        ]
                    ),
                    "resources": deepcopy(container["resources"]),
                    "securityContext": deepcopy(container["securityContext"]),
                }
            )
        pod["spec"]["initContainers"] = init_containers
    volume = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": identity.pvc_name,
            "namespace": namespace,
            "labels": identity.volume_labels,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": resources.get("storage", "10Gi")}},
        },
    }
    if storage_class_name is not None:
        volume["spec"]["storageClassName"] = storage_class_name
    return WorkspaceLaunchPlan(
        identity=identity,
        runtime=GenericLaunchPlan(identity, namespace, pod, secret, network_policy),
        volume=volume,
        initialize=initialize,
    )


def workspace_harness_bindings(
    identity: WorkspaceRuntimeIdentity,
    ssh: WorkspaceSSHMaterial,
    *,
    pod_ip: str,
    namespace: str | None = None,
) -> GenericBindings:
    """Deliver only this attachment's SSH identity to its authorized harness.

    SDK clients can load the key file directly. The optional ssh helper copies
    it into a caller-owned mode-0600 temporary file so OpenSSH works for images
    with either root or nonroot users; read-only Secret mounts are root-owned.
    """
    host = str(ipaddress.ip_address(pod_ip))
    ssh.validate()
    known_hosts = f"[{host}]:{SSH_PORT} {ssh.host_public_key}\n"
    helper = (
        "#!/bin/sh\nset -eu\n"
        'srw_ssh_key_file=$(mktemp "${TMPDIR:-/tmp}/srw-workspace-key.XXXXXX")\n'
        "trap 'rm -f -- \"$srw_ssh_key_file\"' EXIT HUP INT TERM\n"
        'chmod 0600 "$srw_ssh_key_file"\n'
        f'cat {shlex.quote(_SSH_FILES + "/id_ed25519")} > "$srw_ssh_key_file"\n'
        f'ssh -p {SSH_PORT} -i "$srw_ssh_key_file" -o BatchMode=yes '
        "-o IdentitiesOnly=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=yes "
        "-o HostKeyAlgorithms=ssh-ed25519 "
        f"-o UserKnownHostsFile={shlex.quote(_SSH_FILES + '/known_hosts')} "
        f'{shlex.quote(SSH_USER + "@" + host)} "$@"\n'
    )
    peer = {"podSelector": {"matchLabels": identity.labels}}
    if namespace is not None:
        peer["namespaceSelector"] = {
            "matchLabels": {"kubernetes.io/metadata.name": namespace}
        }
    return GenericBindings(
        files=(
            GenericBoundFile(_SSH_FILES + "/id_ed25519", ssh.client_private_key),
            GenericBoundFile(_SSH_FILES + "/known_hosts", known_hosts),
            GenericBoundFile(_SSH_FILES + "/ssh", helper, 0o555),
        ),
        descriptor={
            "workspace": {
                "uid": identity.instance_id,
                "generation": identity.generation,
                "backend": "sandbox",
                "transport": "ssh",
                "host": host,
                "port": SSH_PORT,
                "user": SSH_USER,
                "root": WORKSPACE_ROOT,
                "privateKeyFile": _SSH_FILES + "/id_ed25519",
                "knownHostsFile": _SSH_FILES + "/known_hosts",
                "sshCommand": [_SSH_FILES + "/ssh"],
            }
        },
        egress=(
            {
                "to": [peer],
                "ports": [{"port": SSH_PORT, "protocol": "TCP"}],
            },
        ),
    )


class ManifestWorkspaceRuntime(GenericHarnessRuntime):
    """The caller serializes attachment generations and publishes every UID."""

    def __init__(self, core_api, networking_api, *, namespace: str):
        super().__init__(
            core_api, networking_api, namespace=namespace, container_name="workspace"
        )

    async def ensure_volume(
        self, plan: WorkspaceLaunchPlan, *, expected_pvc_uid: str | None = None
    ) -> str:
        if plan.runtime.namespace != self.namespace:
            raise ValueError("The workspace plan belongs to another namespace.")
        identity = plan.identity
        try:
            volume = await run_bounded_k8s_call(
                self.core_api.read_namespaced_persistent_volume_claim,
                namespace=self.namespace,
                name=identity.pvc_name,
            )
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise ManifestWorkspaceError(
                    "Unable to observe workspace storage."
                ) from None
            if expected_pvc_uid is not None:
                raise ManifestWorkspaceError(
                    "Retained workspace storage is unavailable; a replacement is forbidden."
                ) from None
            try:
                volume = await run_bounded_k8s_mutation(
                    self.core_api.create_namespaced_persistent_volume_claim,
                    namespace=self.namespace,
                    body=plan.volume,
                )
            except Exception as create_error:
                if getattr(create_error, "status", None) != 409:
                    raise ManifestWorkspaceError(
                        "Workspace storage creation is unconfirmed."
                    ) from None
                volume = await run_bounded_k8s_call(
                    self.core_api.read_namespaced_persistent_volume_claim,
                    namespace=self.namespace,
                    name=identity.pvc_name,
                )
        uid = _field(_field(volume, "metadata"), "uid")
        if (
            not uid
            or (expected_pvc_uid and uid != expected_pvc_uid)
            or not _matches(volume, plan.volume)
        ):
            raise ManifestWorkspaceError(
                "Workspace storage identity or contract changed."
            )
        return str(uid)

    async def launch(self, plan: WorkspaceLaunchPlan) -> WorkspacePodObservation:
        # ensure_volume is deliberately separate: the caller persists the PVC
        # UID before a pod can claim it, then persists the observed pod UID.
        observed = await super().launch(plan.runtime)
        return await self.observe(plan.identity, expected_pod_uid=observed.pod_uid)

    async def observe(
        self, identity: WorkspaceRuntimeIdentity, *, expected_pod_uid: str | None
    ) -> WorkspacePodObservation:
        pod = await self._read_pod(identity)
        if pod is None:
            return WorkspacePodObservation(identity.pod_name, None, "Absent")
        observed = _observation(
            pod, identity, expected_pod_uid, container_name="workspace"
        )
        if observed.phase == "Replaced":
            return WorkspacePodObservation(**asdict(observed))
        initializers = _field(_field(pod, "spec"), "initContainers", []) or []
        statuses = _field(_field(pod, "status"), "initContainerStatuses", []) or []
        completed = {
            _field(item, "name")
            for item in statuses
            if _field(_field(_field(item, "state"), "terminated"), "exitCode") == 0
        }
        succeeded = all(_field(item, "name") in completed for item in initializers)
        return WorkspacePodObservation(
            **asdict(observed), initialization_succeeded=succeeded
        )

    async def retire(
        self, identity: WorkspaceRuntimeIdentity, *, expected_pod_uid: str
    ) -> bool:
        # Cancellation requests kernel-backed process termination; finalizer
        # cleanup waits for actual terminated-container evidence and API absence.
        await self.cancel(identity, expected_pod_uid=expected_pod_uid)
        return await self.cleanup(identity, expected_pod_uid=expected_pod_uid)

    async def delete_volume(
        self, identity: WorkspaceRuntimeIdentity, *, expected_pvc_uid: str
    ) -> bool:
        if not expected_pvc_uid:
            raise ValueError("Storage deletion requires the recorded PVC UID.")
        try:
            pods = await run_bounded_k8s_call(
                self.core_api.list_namespaced_pod,
                namespace=self.namespace,
                label_selector=f"srw.io/workspace-instance={identity.instance_id}",
            )
            if _field(pods, "items", []):
                return False
            volume = await run_bounded_k8s_call(
                self.core_api.read_namespaced_persistent_volume_claim,
                namespace=self.namespace,
                name=identity.pvc_name,
            )
            metadata = _field(volume, "metadata")
            if _field(metadata, "uid") != expected_pvc_uid or not _matches(
                _field(metadata, "labels"), identity.volume_labels
            ):
                return False
            await run_bounded_k8s_mutation(
                self.core_api.delete_namespaced_persistent_volume_claim,
                namespace=self.namespace,
                name=identity.pvc_name,
                body={"preconditions": {"uid": expected_pvc_uid}},
            )
            await run_bounded_k8s_call(
                self.core_api.read_namespaced_persistent_volume_claim,
                namespace=self.namespace,
                name=identity.pvc_name,
            )
            return False
        except Exception as exc:
            return getattr(exc, "status", None) == 404
