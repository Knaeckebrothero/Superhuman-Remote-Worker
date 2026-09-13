"""Installation-owned capability checks shared by VM admission and hosting."""

from dataclasses import dataclass
import json
import os
import re

from shared.workspace_preparation import image_reference


def disk_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[1-9][0-9]{0,5}(Mi|Gi|Ti)", value
    ):
        raise ValueError(
            "Preparation storage requires a positive Mi, Gi or Ti quantity."
        )
    return int(value[:-2]) * {"Mi": 2**20, "Gi": 2**30, "Ti": 2**40}[value[-2:]]


@dataclass(frozen=True)
class PreparationSettings:
    enabled: bool = False
    builder_image: str = ""
    disk_size: str = "30Gi"
    max_concurrent: int = 2
    max_cache_entries: int = 128
    build_timeout: int = 3600
    import_timeout: int = 2700
    cache_ttl: int = 7 * 86400
    network_enabled: bool = False
    network_policy_revision: str = "offline"
    registry_hosts: tuple = ("ghcr.io", "docker.io", "quay.io")
    insecure_registry_hosts: tuple = ()
    token_hosts: tuple = ("ghcr.io", "auth.docker.io", "quay.io")
    image_pull_secrets: tuple = ()

    @property
    def wait_budget(self):
        # Queue, base import, clone, builder, then bounded handoff.
        return 3 * self.import_timeout + self.build_timeout + 300

    @classmethod
    def from_environment(cls):
        def sequence(name, fallback):
            value = json.loads(os.getenv(name, json.dumps(fallback)))
            if (
                not isinstance(value, list)
                or len(value) > 32
                or any(not isinstance(x, str) or not x for x in value)
            ):
                raise ValueError(f"Invalid {name} configuration.")
            return tuple(value)

        value = cls(
            enabled=os.getenv("VM_PREPARATION_ENABLED", "false").lower() == "true",
            builder_image=os.getenv("VM_PREPARATION_IMAGE", ""),
            disk_size=os.getenv("VM_PREPARATION_DISK_SIZE", "30Gi"),
            max_concurrent=int(os.getenv("VM_PREPARATION_MAX_CONCURRENT", "2")),
            max_cache_entries=int(os.getenv("VM_PREPARATION_MAX_CACHE_ENTRIES", "128")),
            build_timeout=int(os.getenv("VM_PREPARATION_TIMEOUT", "3600")),
            import_timeout=int(os.getenv("VM_PREPARATION_IMPORT_TIMEOUT", "2700")),
            cache_ttl=int(os.getenv("VM_PREPARATION_CACHE_TTL", str(7 * 86400))),
            network_enabled=os.getenv("VM_PREPARATION_NETWORK_ENABLED", "false").lower()
            == "true",
            network_policy_revision=os.getenv(
                "VM_PREPARATION_NETWORK_POLICY_REVISION", "offline"
            ),
            registry_hosts=sequence(
                "VM_PREPARATION_REGISTRY_HOSTS", cls.registry_hosts
            ),
            insecure_registry_hosts=sequence(
                "VM_PREPARATION_INSECURE_REGISTRY_HOSTS", ()
            ),
            token_hosts=sequence("VM_PREPARATION_TOKEN_HOSTS", cls.token_hosts),
            image_pull_secrets=sequence("VM_PREPARATION_IMAGE_PULL_SECRETS", ()),
        )
        if value.enabled:
            image_reference(value.builder_image)
            if not 2**30 <= disk_bytes(value.disk_size) <= 2**40:
                raise ValueError("Preparation disks must be between 1Gi and 1Ti.")
            if (
                not 1 <= value.max_concurrent <= 20
                or not 2 <= value.max_cache_entries <= 1000
                or not 60 <= value.build_timeout <= 3600
                or not 60 <= value.import_timeout <= 86400
                or not 0 <= value.cache_ttl <= 90 * 86400
            ):
                raise ValueError("Invalid VM preparation limits.")
            if not set(value.insecure_registry_hosts) <= set(value.registry_hosts):
                raise ValueError(
                    "Insecure preparation registries must be allowed explicitly."
                )
            if value.network_enabled and (
                os.getenv("VM_PREPARATION_NETWORK_ISOLATION_VERIFIED", "false").lower()
                != "true"
                or not re.fullmatch(r"[0-9a-f]{64}", value.network_policy_revision)
            ):
                raise ValueError(
                    "Online preparation requires verified builder network isolation."
                )
        return value
