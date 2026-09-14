"""Internal, signed binding of a VM execution to an exclusive retained disk."""

from uuid import UUID


WORKSPACE_LABEL = "srw.io/workspace-instance"
GENERATION_LABEL = "srw.io/workspace-generation"
EXECUTION_LABEL = "srw.io/workspace-execution"


def storage_binding(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "uid",
        "generation",
        "pvc_uid",
        "owner_id",
        "owner_kind",
    }:
        raise ValueError("Invalid retained VM storage binding.")
    for field in ("uid", "owner_id"):
        if not isinstance(value[field], str) or str(UUID(value[field])) != value[field]:
            raise ValueError("Retained VM identities require canonical UUIDs.")
    generation = value["generation"]
    if type(generation) is not int or not 1 <= generation <= 9223372036854775807:
        raise ValueError("Invalid retained VM attachment generation.")
    if value["owner_kind"] not in {"job", "thread"}:
        raise ValueError("Invalid retained VM storage accounting owner.")
    pvc_uid = value["pvc_uid"]
    if pvc_uid is not None and (
        not isinstance(pvc_uid, str) or str(UUID(pvc_uid)) != pvc_uid
    ):
        raise ValueError("Retained VM storage requires an exact PVC UID.")
    if generation > 1 and pvc_uid is None:
        raise ValueError("Reusing VM storage requires its captured PVC UID.")
    return dict(value)


def storage_name(binding: dict) -> str:
    return f"srw-ws-{UUID(binding['uid']).hex}"


def storage_labels(binding: dict, execution_id: str) -> dict[str, str]:
    return {
        WORKSPACE_LABEL: binding["uid"],
        GENERATION_LABEL: str(binding["generation"]),
        EXECUTION_LABEL: execution_id,
    }
