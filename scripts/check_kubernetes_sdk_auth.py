#!/usr/bin/env python3
"""Check installed Kubernetes SDK auth without credentials or network access.

Run directly with Python in the final orchestrator/controller environment.
See scripts/kubernetes-sdk-auth.md for image/build invocations and scope.
"""

from __future__ import annotations

import datetime
import json
import platform
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from kubernetes import __version__ as kubernetes_version
from kubernetes import config as kube_config
from kubernetes.client import (
    ApiClient,
    Configuration,
    CoordinationV1Api,
    CoreV1Api,
    CustomObjectsApi,
)
from kubernetes.config import incluster_config
from kubernetes.config.incluster_config import InClusterConfigLoader


class SmokeFailure(RuntimeError):
    """The installed SDK did not meet the generated-request auth contract."""


class MissingAuthorization(SmokeFailure):
    """The generated request omitted or changed the expected fake token."""


class _CapturedRequest(Exception):
    """Stop after SDK authentication, before transport or deserialization."""


def _deny_network(*args, **kwargs):
    raise SmokeFailure("SDK auth smoke attempted network access")


def _check_requests(api, invoke, expected_path, loader, token, path):
    """Inspect generated auth after either supported configuration bootstrap."""
    client = api.api_client
    config = client.configuration
    label = f"{path}/{type(api).__name__}"
    expected = "bearer srw-fixture-first"
    phase = "initial"

    def intercept(method, url, **kwargs):
        location = urlsplit(url)
        if (
            method != "GET"
            or location.netloc != "srw-sdk-smoke.invalid:443"
            or location.scheme != "https"
            or location.path != expected_path
        ):
            raise SmokeFailure(f"{label}: unexpected request target")
        headers = {key.lower(): value for key, value in kwargs["headers"].items()}
        if headers.get("authorization") != expected:
            # Never put header/token values into diagnostics.
            raise MissingAuthorization(f"{label}: {phase} authorization mismatch")
        raise _CapturedRequest()

    # request() runs after update_params_for_auth(), unlike call_api().
    client.request = intercept
    for phase in ("initial", "refreshed"):
        if phase == "refreshed":
            token.write_text("srw-fixture-refreshed", encoding="utf-8")
            expected = "bearer srw-fixture-refreshed"
            # Advance the loader's deadline instead of sleeping a minute.
            loader.token_expires_at = datetime.datetime.min
        try:
            invoke(api)
        except _CapturedRequest:
            pass
        else:
            raise SmokeFailure(f"{label}: {phase} did not reach intercepted transport")
    config.api_key.clear()
    config.refresh_api_key_hook = None
    phase = "missing-header control"
    try:
        invoke(api)
    except MissingAuthorization:
        pass
    except _CapturedRequest as exc:
        raise SmokeFailure(
            f"{label}: missing-header control accepted authorization"
        ) from exc
    else:
        raise SmokeFailure(f"{label}: missing-header control missed transport")


def _load_default(token, cert, environ):
    """Run the public role bootstrap with fake inputs and retain its real loader."""
    loader = None

    def fixture_loader(*args, **kwargs):
        nonlocal loader
        # Preserve the real loader/public wrapper/default-publication behavior;
        # substitute only fake inputs and retain the expiry clock for refresh.
        loader = InClusterConfigLoader(*args, **kwargs, environ=environ)
        return loader

    with (
        patch.object(incluster_config, "SERVICE_TOKEN_FILENAME", str(token)),
        patch.object(incluster_config, "SERVICE_CERT_FILENAME", str(cert)),
        patch.object(incluster_config, "InClusterConfigLoader", fixture_loader),
    ):
        kube_config.load_incluster_config()
    if loader is None or Configuration._default is None:
        raise SmokeFailure(
            "default: in-cluster bootstrap did not publish configuration"
        )
    return loader


def check_auth() -> dict:
    cases = (
        (
            CoreV1Api,
            lambda api: api.list_namespaced_pod("fixture"),
            "/api/v1/namespaces/fixture/pods",
        ),
        (
            CustomObjectsApi,
            lambda api: api.list_namespaced_custom_object(
                "kubevirt.io", "v1", "fixture", "virtualmachines"
            ),
            "/apis/kubevirt.io/v1/namespaces/fixture/virtualmachines",
        ),
        (
            CoordinationV1Api,
            lambda api: api.list_namespaced_lease("fixture"),
            "/apis/coordination.k8s.io/v1/namespaces/fixture/leases",
        ),
    )
    paths = ("explicit", "default")
    environ = {
        "KUBERNETES_SERVICE_HOST": "srw-sdk-smoke.invalid",
        "KUBERNETES_SERVICE_PORT": "443",
    }
    inherited_default = Configuration._default
    try:
        with (
            tempfile.TemporaryDirectory(prefix="srw-sdk-auth-") as directory,
            patch.object(socket, "getaddrinfo", _deny_network),
            patch.object(socket.socket, "connect", _deny_network),
            patch.object(socket.socket, "connect_ex", _deny_network),
        ):
            token = Path(directory) / "token"
            cert = Path(directory) / "ca.crt"
            cert.write_text("fixture-only-no-connection", encoding="utf-8")
            for path in paths:
                for api_type, invoke, expected_path in cases:
                    # No stale default or token may satisfy a subsequent case.
                    Configuration._default = None
                    token.write_text("srw-fixture-first", encoding="utf-8")
                    if path == "explicit":
                        config = Configuration()
                        config.proxy = None
                        loader = InClusterConfigLoader(
                            str(token), str(cert), environ=environ
                        )
                        loader.load_and_set(config)
                        with ApiClient(configuration=config) as client:
                            api = api_type(client)
                            _check_requests(
                                api, invoke, expected_path, loader, token, path
                            )
                    else:
                        loader = _load_default(token, cert, environ)
                        published_auth = dict(Configuration._default.api_key)
                        published_hook = Configuration._default.refresh_api_key_hook
                        # Match role startup: bare generated API, then SDK default
                        # publication/copy instead of an explicit ApiClient config.
                        api = api_type()
                        with api.api_client:
                            if api.api_client.configuration is Configuration._default:
                                raise SmokeFailure(
                                    f"default/{api_type.__name__}: configuration was not copied"
                                )
                            _check_requests(
                                api, invoke, expected_path, loader, token, path
                            )
                            if (
                                Configuration._default.api_key != published_auth
                                or Configuration._default.refresh_api_key_hook
                                is not published_hook
                            ):
                                raise SmokeFailure(
                                    f"default/{api_type.__name__}: client changed published auth"
                                )
    finally:
        # Restore identity even when publication, construction or a check fails.
        # set_default() would make another copy of the caller's inherited object.
        Configuration._default = inherited_default
    return {
        "kubernetes_version": kubernetes_version,
        "python_version": platform.python_version(),
        "generated_api_families": [api_type.__name__ for api_type, _, _ in cases],
        "configuration_paths": list(paths),
        "initial_headers_passed": True,
        "refreshed_headers_passed": True,
        "missing_header_negative_control_passed": True,
        "default_copy_isolation_passed": True,
        "network_requests": 0,
    }


def main() -> None:
    print(json.dumps(check_auth(), sort_keys=True))


if __name__ == "__main__":
    main()
