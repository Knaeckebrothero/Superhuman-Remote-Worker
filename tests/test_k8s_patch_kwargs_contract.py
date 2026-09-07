"""Kubernetes patch calls must not pass client-private ``_``-prefixed kwargs.

Unit tests inject fake API objects, which accept any keyword, so a kwarg the
real generated client rejects survives every green suite and only fails when a
deployed orchestrator issues the call. ``_content_type`` was exactly that: the
35.0.0 client rejected it, and each remaining site raised ``ApiTypeError`` at runtime
instead of publishing a session route or releasing a finalizer.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO_ROOT / "src" / "orchestrator"
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
        "Keep patch calls compatible with SDK/image generations that reject "
        "private kwargs (ApiTypeError); mocked APIs cannot catch this:\n"
        + "\n".join(offenders)
    )


def test_installed_client_sends_json_patch_without_private_kwargs(tmp_path):
    """Protect the wire contract rather than a particular SDK's kwarg rejection.

    36.0.3 accepts the former override. Both generations must encode the public
    list-body call correctly. A fresh process avoids suite-wide SDK mocks.
    """
    pytest.importorskip("kubernetes")
    code = """
import json, sys
def no_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('patch probe attempted network access')
sys.addaudithook(no_network)
from kubernetes.client import ApiClient, Configuration, CoreV1Api, NetworkingV1Api
body = [{'op': 'test', 'path': '/metadata/uid', 'value': 'fixture-uid'},
        {'op': 'remove', 'path': '/metadata/finalizers'}]
class Captured(Exception): pass
receipts = []
def request(method, url, **kwargs):
    assert method == 'PATCH'
    assert url.startswith('https://srw-patch-smoke.invalid/')
    assert kwargs['headers']['Content-Type'] == 'application/json-patch+json'
    assert kwargs['body'] == body
    receipts.append(url)
    raise Captured
with ApiClient(Configuration(host='https://srw-patch-smoke.invalid')) as client:
    client.request = request
    for api_type, resources in (
        (CoreV1Api, ('pod', 'persistent_volume_claim', 'config_map', 'service')),
        (NetworkingV1Api, ('ingress',)),
    ):
        api = api_type(client)
        for resource in resources:
            try:
                getattr(api, 'patch_namespaced_' + resource)(name='p', namespace='n', body=body)
            except Captured:
                pass
            else:
                raise AssertionError('generated patch did not reach request boundary')
print(json.dumps(receipts))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 5
