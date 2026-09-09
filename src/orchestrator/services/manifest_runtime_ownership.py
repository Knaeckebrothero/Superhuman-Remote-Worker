"""Keep the reference harness's control plane out of other harness executions."""

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException


def uses_srw_runtime(work: Mapping[str, Any]) -> bool:
    """The marker is projected from the server's immutable execution record.

    Historical rows have no record and retain the reference runtime. Authored
    job context or private harness settings never supply this marker.
    """
    return work.get("execution_harness_adapter") in (None, "srw/v1")


def require_srw_runtime(work: Mapping[str, Any]) -> None:
    if not uses_srw_runtime(work):
        raise HTTPException(
            409,
            {
                "code": "manifest_runtime_owned",
                "message": "This execution is managed through the resource API. Report its outcome through /api/resources/{uid}/outcome.",
            },
        )


def require_srw_expert_configuration(
    expert: Mapping[str, Any] | None,
    *,
    interactive: bool = False,
    trusted_image: str | None = None,
) -> None:
    """A projected generic Expert must never enter the SRW private loader."""
    if (
        expert is not None
        and "harness_adapter" in expert
        and expert["harness_adapter"] != "srw/v1"
    ):
        raise HTTPException(
            409,
            (
                "Interactive sessions require the SRW adapter; use a manifest Job for this Expert."
                if interactive
                else "This Expert requires native manifest Job admission."
            ),
        )
    if expert is not None and trusted_image is not None and expert.get("manifest"):
        require_srw_launch_configuration(
            expert["manifest"]["spec"]["runtime"], trusted_image=trusted_image
        )


def require_srw_launch_configuration(
    runtime: Mapping[str, Any], *, trusted_image: str
) -> dict[str, Any]:
    """Native and existing callers must honor the same reference-host contract."""
    if runtime.get("image") != trusted_image:
        raise HTTPException(
            422,
            "The SRW adapter requires this installation's trusted SRW image; update the Expert image and reapply affected Projects, or use generic hosting for an arbitrary image.",
        )
    if any(key in runtime for key in ("command", "args", "env", "probes", "resources")):
        raise HTTPException(
            422,
            "The SRW adapter's launch envelope is installation-managed; use generic hosting for a custom launch.",
        )
    private = runtime.get("config", {})
    if not isinstance(private, dict):
        raise HTTPException(422, "SRW private configuration must be an object.")
    if set(private) - {"config_name", "asset_name", "config", "prompts", "layers"}:
        raise HTTPException(
            422,
            "The SRW adapter accepts private config_name, asset_name, config, layers and prompts settings.",
        )
    if not isinstance(private.get("config_name", "worker_base"), str) or any(
        not isinstance(private.get(key, {}), dict) for key in ("config", "prompts")
    ):
        raise HTTPException(422, "Invalid SRW private configuration.")
    return private
