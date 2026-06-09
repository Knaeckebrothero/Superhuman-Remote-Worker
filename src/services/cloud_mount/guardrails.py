"""Lightweight guardrails for rclone-backed cloud mounts.

This is intentionally a preflight advisory/block, not the full hydration-budget
system described in docs/features/rclone_cloud_mount.md. Runtime transfer
accounting still belongs in a later guard implementation.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


_CLOUD_ROOTS = ("/cloud", "/workspace/cloud")
_SHELL_OPERATORS = {"|", "||", "&&", ";", "&"}


@dataclass(frozen=True)
class CloudScanRisk:
    reason: str


def detect_cloud_scan_risk(command: str) -> CloudScanRisk | None:
    """Return a risk reason for obvious broad cloud-hydration commands.

    The detector is deliberately conservative and argv-oriented. It catches
    common accidental scans while avoiding a brittle free-form regex blocklist.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        if _mentions_cloud_path(command):
            return CloudScanRisk(
                "command mentions a cloud mount but cannot be parsed safely"
            )
        return None

    if not argv or not _argv_touches_cloud(argv):
        return None

    command_name = _base_command(argv)
    if not command_name:
        return None

    if command_name == "grep" and _has_recursive_flag(argv):
        return CloudScanRisk("recursive grep over a cloud mount can hydrate many files")
    if command_name in {"rg", "ag"}:
        return CloudScanRisk(
            f"{command_name} searches recursively by default when pointed at a cloud mount"
        )
    if command_name in {"du", "tar", "zip", "unzip"}:
        return CloudScanRisk(
            f"{command_name} over a cloud mount can traverse or materialize large trees"
        )
    if command_name in {"rsync", "scp"}:
        return CloudScanRisk(
            f"{command_name} against a cloud mount can copy large remote trees"
        )
    if command_name == "cp" and _has_recursive_flag(argv):
        return CloudScanRisk("recursive copy from a cloud mount can hydrate many files")
    if command_name == "find" and _contains_any(argv, {"-exec", "-execdir"}):
        return CloudScanRisk(
            "find -exec over a cloud mount can run content-reading commands at cloud scale"
        )

    if _contains_any(argv, _SHELL_OPERATORS) and _looks_like_complex_cloud_pipeline(
        argv
    ):
        return CloudScanRisk(
            "complex shell pipeline touching a cloud mount needs a narrower path or review"
        )

    return None


def format_cloud_scan_guard_message(command: str, risk: CloudScanRisk) -> str:
    return (
        "Cloud scan guard: this command was not run because it looks like a "
        "broad operation over rclone-mounted cloud storage.\n"
        f"Reason: {risk.reason}.\n"
        f"Command: {command}\n\n"
        "Use a narrower file or directory under /workspace/cloud, run a metadata-only "
        "query first, or ask the operator before scanning a large cloud tree."
    )


def _base_command(argv: list[str]) -> str | None:
    for token in argv:
        if token in _SHELL_OPERATORS:
            continue
        return PurePosixPath(token).name
    return None


def _argv_touches_cloud(argv: Iterable[str]) -> bool:
    return any(_is_cloud_path_token(token) for token in argv)


def _mentions_cloud_path(command: str) -> bool:
    return any(root in command for root in _CLOUD_ROOTS)


def _is_cloud_path_token(token: str) -> bool:
    if token in _SHELL_OPERATORS or token.startswith("-"):
        return False
    normalized = token.rstrip("/")
    if normalized in _CLOUD_ROOTS:
        return True
    return any(normalized.startswith(f"{root}/") for root in _CLOUD_ROOTS)


def _has_recursive_flag(argv: Iterable[str]) -> bool:
    for token in argv:
        if token in {"-r", "-R", "--recursive", "--dereference-recursive"}:
            return True
        if token.startswith("-") and not token.startswith("--"):
            if "r" in token[1:] or "R" in token[1:]:
                return True
    return False


def _contains_any(argv: Iterable[str], candidates: set[str]) -> bool:
    return any(token in candidates for token in argv)


def _looks_like_complex_cloud_pipeline(argv: list[str]) -> bool:
    cloud_index = next(
        (index for index, token in enumerate(argv) if _is_cloud_path_token(token)),
        None,
    )
    if cloud_index is None:
        return False
    risky_commands = {"grep", "rg", "ag", "python", "python3", "node", "perl", "ruby"}
    return any(PurePosixPath(token).name in risky_commands for token in argv)
