"""Bounded, versioned input for isolated VM preparation and scoped artifacts."""

from copy import deepcopy
import hashlib
import json
import re
from uuid import UUID

from shared.workspace_initialization import initialization_request

VERSION = 1
BUILDER_VERSION = "srw/libguestfs-v1"
ARCHITECTURE = "amd64"
PREPARATION_LABEL = "srw.io/workspace-preparation"
ALLOCATION_LABEL = "srw.io/preparation-allocation"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def revision(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def image_reference(value):
    """Normalize an OCI reference without permitting URL/auth/query syntax."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("Preparation requires a bounded OCI image reference.")
    name, separator, digest = value.partition("@")
    if separator and not DIGEST.fullmatch(digest):
        raise ValueError("VM preparation supports sha256 image digests only.")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        host, repository = first, rest
    else:
        host, repository = "docker.io", name
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?", host):
        raise ValueError("Invalid OCI registry host.")
    repository, tag_separator, tag = repository.partition(":")
    if host == "docker.io" and "/" not in repository:
        repository = "library/" + repository
    if not re.fullmatch(r"[a-z0-9]+(?:(?:[._-]+|/)[a-z0-9]+)*", repository):
        raise ValueError("Invalid OCI repository path.")
    if tag_separator and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("Invalid OCI image tag.")
    reference = digest if separator else tag or "latest"
    return host, repository, reference


def normalized_image(value):
    host, repository, reference = image_reference(value)
    return (
        f"{host}/{repository}"
        + ("@" if reference.startswith("sha256:") else ":")
        + reference
    )


def preparation_request(
    environment,
    *,
    scope_kind,
    scope_uid,
    allocation_id,
    owner_kind,
    runtime_generation=None,
):
    if not isinstance(environment, dict) or set(environment) - {
        "image",
        "pullPolicy",
        "prepare",
        "cache",
    }:
        raise ValueError("Invalid VM preparation environment.")
    if scope_kind not in {"Account", "Project"} or owner_kind not in {"job", "session"}:
        raise ValueError("Invalid preparation ownership scope.")
    steps = initialization_request(environment.get("prepare", []))["steps"]
    pull, cache = (
        environment.get("pullPolicy", "IfNotPresent"),
        environment.get("cache", "Reuse"),
    )
    if pull not in {"Always", "IfNotPresent", "Never"} or cache not in {
        "Reuse",
        "Rebuild",
    }:
        raise ValueError("Unsupported workspace pull/cache policy.")
    payload = {
        "version": VERSION,
        "scope": {"kind": scope_kind, "uid": str(UUID(str(scope_uid)))},
        "allocationId": str(UUID(str(allocation_id))),
        "ownerKind": owner_kind,
        "image": normalized_image(environment.get("image")),
        "pullPolicy": pull,
        "cache": cache,
        "steps": steps,
    }
    if owner_kind == "session":
        payload["runtimeGeneration"] = str(UUID(str(runtime_generation)))
    return {**payload, "revision": revision(payload)}


def validate_request(value):
    try:
        expected = preparation_request(
            {
                "image": value["image"],
                "pullPolicy": value["pullPolicy"],
                "cache": value["cache"],
                "prepare": value["steps"],
            },
            scope_kind=value["scope"]["kind"],
            scope_uid=value["scope"]["uid"],
            allocation_id=value["allocationId"],
            owner_kind=value["ownerKind"],
            runtime_generation=value.get("runtimeGeneration"),
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Invalid workspace preparation request.") from exc
    if expected != value or type(value["version"]) is not int:
        raise ValueError("Workspace preparation request revision is invalid.")
    return expected


def cache_key(
    request, *, base_image, builder_image, disk_size="30Gi", network_policy="offline"
):
    request = validate_request(request)
    if not image_reference(base_image)[2].startswith("sha256:"):
        raise ValueError("Preparation requires a resolved base digest.")
    if not image_reference(builder_image)[2].startswith("sha256:"):
        raise ValueError("Preparation requires a pinned builder image.")
    key = {
        "scope": request["scope"],
        "baseImage": normalized_image(base_image),
        "builderImage": normalized_image(builder_image),
        "builder": BUILDER_VERSION,
        "architecture": ARCHITECTURE,
        "format": "raw",
        "diskSize": disk_size,
        "networkPolicy": network_policy,
        "steps": request["steps"],
    }
    if request["cache"] == "Rebuild":
        key["allocation"] = request["allocationId"]
        key["ownerKind"] = request["ownerKind"]
        if "runtimeGeneration" in request:
            key["runtimeGeneration"] = request["runtimeGeneration"]
    return revision(key)


def builder_request(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "buildUid",
        "pvcUid",
        "cacheKey",
        "steps",
        "networkEnabled",
    }:
        raise ValueError("Invalid offline builder input.")
    result = deepcopy(value)
    for key in ("buildUid", "pvcUid"):
        result[key] = str(UUID(value[key]))
    if (
        value["version"] != VERSION
        or type(value["version"]) is not int
        or type(value["networkEnabled"]) is not bool
        or not re.fullmatch(r"[0-9a-f]{64}", value["cacheKey"])
    ):
        raise ValueError("Invalid offline builder identity.")
    result["steps"] = initialization_request(value["steps"])["steps"]
    if result != value:
        raise ValueError("Offline builder identity is not canonical.")
    return result
