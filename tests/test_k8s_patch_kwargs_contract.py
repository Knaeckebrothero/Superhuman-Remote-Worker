"""Kubernetes patch calls must not pass client-private ``_``-prefixed kwargs.

Unit tests inject fake API objects, which accept any keyword, so a kwarg the
real generated client rejects survives every green suite and only fails when a
deployed orchestrator issues the call. ``_content_type`` was exactly that: the
client dropped it, and each remaining site raised ``ApiTypeError`` at runtime
instead of publishing a session route or releasing a finalizer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO_ROOT / "orchestrator"
PATCH_PREFIX = "patch_namespaced_"


def _patch_call_sites() -> list[tuple[str, int, str, str]]:
    """Every call that names a ``patch_namespaced_*`` API, directly or wrapped."""

    sites: list[tuple[str, int, str, str]] = []
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            referenced = {
                item.attr
                for item in [node.func, *node.args, *(k.value for k in node.keywords)]
                if isinstance(item, ast.Attribute)
                and item.attr.startswith(PATCH_PREFIX)
            }
            if not referenced:
                continue
            method = sorted(referenced)[0]
            for keyword in node.keywords:
                if keyword.arg:
                    sites.append(
                        (
                            str(path.relative_to(REPO_ROOT)),
                            node.lineno,
                            method,
                            keyword.arg,
                        )
                    )
    return sites


def test_patch_sites_exist_to_guard():
    assert _patch_call_sites(), "expected orchestrator Kubernetes patch call sites"


def test_no_patch_site_passes_a_client_private_kwarg():
    offenders = [
        f"{path}:{line} {method}(..., {kwarg}=...)"
        for path, line, method, kwarg in _patch_call_sites()
        if kwarg.startswith("_")
    ]
    assert offenders == [], (
        "The generated Kubernetes client rejects its former private kwargs "
        "(ApiTypeError) and a mocked API in unit tests cannot catch it:\n"
        + "\n".join(offenders)
    )


def test_installed_client_rejects_the_removed_content_type_kwarg():
    """Pin the reason the kwarg is gone, so a revert fails loudly here."""

    pytest.importorskip("kubernetes")
    from kubernetes.client.api.core_v1_api import CoreV1Api

    api = CoreV1Api.__new__(CoreV1Api)
    with pytest.raises(Exception) as rejected:
        CoreV1Api.patch_namespaced_pod(
            api,
            name="p",
            namespace="n",
            body=[],
            _content_type="application/json-patch+json",
        )
    assert "_content_type" in str(rejected.value)
