"""Use real installed SDKs in fresh processes, isolated from suite client mocks."""

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_kubernetes_sdk_auth.py"
API_FAMILIES = ("CoreV1Api", "CustomObjectsApi", "CoordinationV1Api")


def run_probe(tmp_path, injection="", family="CoreV1Api"):
    driver = """
import runpy
import sys

# Independent backstop: fail before a credential read, DNS lookup or connection.
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("smoke escaped network isolation")
    if event == "open" and isinstance(args[0], str):
        if args[0].startswith("/var/run/secrets/") or args[0] == "/forbidden-kubeconfig":
            raise AssertionError("smoke read ambient credentials")
sys.addaudithook(audit)

from kubernetes import client
from kubernetes.config.incluster_config import InClusterConfigLoader
namespace = runpy.run_path(sys.argv[1], run_name="sdk_auth_smoke")
"""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            driver + injection + '\nnamespace["main"]()\n',
            str(SCRIPT),
            family,
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "KUBECONFIG": "/forbidden-kubeconfig",
            "KUBERNETES_SERVICE_HOST": "ambient-must-not-be-used.invalid",
            "KUBERNETES_SERVICE_PORT": "9",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_installed_sdk_initial_refresh_and_negative_controls(tmp_path):
    result = run_probe(tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["generated_api_families"] == list(API_FAMILIES)
    assert report["configuration_paths"] == ["explicit", "default"]
    assert report["default_copy_isolation_passed"] is True
    assert report["kubernetes_version"]
    assert report["initial_headers_passed"] is True
    assert report["refreshed_headers_passed"] is True
    assert report["missing_header_negative_control_passed"] is True
    assert report["network_requests"] == 0
    assert "srw-fixture-" not in result.stdout + result.stderr


@pytest.mark.parametrize("family", API_FAMILIES)
def test_rejects_lost_generated_auth_for_each_family(tmp_path, family):
    result = run_probe(
        tmp_path,
        """
api_type = getattr(client, sys.argv[2])
original_init = api_type.__init__
def lose_auth(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.api_client.update_params_for_auth = lambda *args, **kwargs: None
api_type.__init__ = lose_auth
""",
        family,
    )
    assert result.returncode != 0
    assert f"{family}: initial authorization mismatch" in result.stderr
    assert "srw-fixture-" not in result.stderr


@pytest.mark.parametrize("family", API_FAMILIES)
def test_rejects_stale_token_for_each_family(tmp_path, family):
    result = run_probe(
        tmp_path,
        """
api_type = getattr(client, sys.argv[2])
original_init = api_type.__init__
def disable_refresh(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.api_client.configuration.refresh_api_key_hook = None
api_type.__init__ = disable_refresh
""",
        family,
    )
    assert result.returncode != 0
    assert f"{family}: refreshed authorization mismatch" in result.stderr
    assert "srw-fixture-" not in result.stderr


def test_negative_control_rejects_auth_that_bypasses_the_configuration(tmp_path):
    result = run_probe(
        tmp_path,
        """
original_auth = client.ApiClient.update_params_for_auth
def retain_header(self, headers, queries, auth_settings, *args, **kwargs):
    original_auth(self, headers, queries, auth_settings, *args, **kwargs)
    if "authorization" in headers:
        self.fixture_retained_auth = headers["authorization"]
    else:
        headers["authorization"] = self.fixture_retained_auth
client.ApiClient.update_params_for_auth = retain_header
""",
    )
    assert result.returncode != 0
    assert "CoreV1Api: missing-header control accepted authorization" in result.stderr
    assert "srw-fixture-" not in result.stderr


def test_a_generated_noop_cannot_pass_without_reaching_transport(tmp_path):
    result = run_probe(
        tmp_path,
        "client.CustomObjectsApi.list_namespaced_custom_object = lambda *a, **kw: None\n",
    )
    assert result.returncode != 0
    assert (
        "CustomObjectsApi: initial did not reach intercepted transport" in result.stderr
    )


@pytest.mark.parametrize("family", API_FAMILIES)
@pytest.mark.parametrize(
    "fault, phase", (("auth", "initial"), ("refresh", "refreshed"))
)
def test_default_copy_must_preserve_auth_and_refresh(tmp_path, family, fault, phase):
    result = run_probe(
        tmp_path,
        """
original_init = getattr(client, sys.argv[2]).__init__
original_copy = client.Configuration.get_default_copy
copies = []
def broken_copy():
    config = original_copy()
    copies.append(config)
    return config
client.Configuration.get_default_copy = staticmethod(broken_copy)
def break_copied_config(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    # Explicit-path configuration never travels through get_default_copy().
    if any(self.api_client.configuration is copied for copied in copies):
"""
        + (
            "        self.api_client.configuration.api_key.clear()\n"
            if fault == "auth"
            else "        self.api_client.configuration.refresh_api_key_hook = None\n"
        )
        + "getattr(client, sys.argv[2]).__init__ = break_copied_config\n",
        family,
    )
    assert result.returncode != 0
    assert f"default/{family}: {phase} authorization mismatch" in result.stderr
    assert "srw-fixture-" not in result.stderr


@pytest.mark.parametrize("family", API_FAMILIES)
def test_default_path_rejects_auth_retained_after_mapping_removal(tmp_path, family):
    result = run_probe(
        tmp_path,
        """
api_type = getattr(client, sys.argv[2])
original_init = api_type.__init__
def retain_default_header(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    if args or kwargs.get("api_client") is not None:
        return
    instance = self.api_client
    original_auth = instance.update_params_for_auth
    def retain_header(headers, queries, auth_settings, *args, **kwargs):
        original_auth(headers, queries, auth_settings, *args, **kwargs)
        if "authorization" in headers:
            instance.fixture_retained_auth = headers["authorization"]
        else:
            headers["authorization"] = instance.fixture_retained_auth
    instance.update_params_for_auth = retain_header
api_type.__init__ = retain_default_header
""",
        family,
    )
    assert result.returncode != 0
    assert (
        f"default/{family}: missing-header control accepted authorization"
        in result.stderr
    )
    assert "srw-fixture-" not in result.stderr


def test_default_bootstrap_must_publish_configuration(tmp_path):
    result = run_probe(
        tmp_path,
        "client.Configuration.set_default = classmethod(lambda cls, config: None)\n",
    )
    assert result.returncode != 0
    assert (
        "default: in-cluster bootstrap did not publish configuration" in result.stderr
    )


def test_default_client_must_not_reuse_published_configuration(tmp_path):
    result = run_probe(
        tmp_path,
        """
client.Configuration.get_default_copy = classmethod(lambda cls: cls._default)
""",
    )
    assert result.returncode != 0
    assert "default/CoreV1Api: configuration was not copied" in result.stderr


def test_default_copy_must_not_share_mutable_auth_mapping(tmp_path):
    result = run_probe(
        tmp_path,
        """
original_copy = client.Configuration.get_default_copy
def shallow_auth_copy():
    config = original_copy()
    config.api_key = client.Configuration._default.api_key
    return config
client.Configuration.get_default_copy = staticmethod(shallow_auth_copy)
""",
    )
    assert result.returncode != 0
    assert "default/CoreV1Api: client changed published auth" in result.stderr


@pytest.mark.parametrize("fail_at", ("none", "publication", "refresh"))
def test_inherited_default_restored_on_success_and_failures(tmp_path, fail_at):
    setup = ""
    expected_failure = ""
    if fail_at == "publication":
        setup = (
            "client.Configuration.set_default = classmethod(lambda cls, config: None)\n"
        )
        expected_failure = "did not publish configuration"
    elif fail_at == "refresh":
        setup = """
original_copy = client.Configuration.get_default_copy
def lose_refresh():
    config = original_copy()
    config.refresh_api_key_hook = None
    return config
client.Configuration.get_default_copy = staticmethod(lose_refresh)
"""
        expected_failure = "default/CoreV1Api: refreshed authorization mismatch"
    result = run_probe(
        tmp_path,
        setup
        + """
inherited = client.Configuration()
inherited.host = "https://inherited-must-not-be-used.invalid"
inherited.api_key["authorization"] = "inherited-must-not-be-used"
client.Configuration._default = inherited
original_check = namespace["check_auth"]
def assert_restore():
    try:
        return original_check()
    finally:
        assert client.Configuration._default is inherited, "default identity not restored"
        assert inherited.api_key == {"authorization": "inherited-must-not-be-used"}, "inherited auth mutated"
namespace["check_auth"] = assert_restore
""",
    )
    if fail_at == "none":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert expected_failure in result.stderr
    assert "default identity not restored" not in result.stderr
    assert "inherited auth mutated" not in result.stderr
    assert "srw-fixture-" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "dockerfile",
    (
        "Dockerfile.orchestrator",
        "Dockerfile.orchestrator.dev",
        "Dockerfile.vm-controller",
    ),
)
def test_final_image_build_runs_offline_installed_sdk_gate(dockerfile):
    root = SCRIPT.parents[1]
    text = (root / "docker" / dockerfile).read_text()
    # Validate Docker build declarations as configuration, not Python behavior.
    lines = text.replace("\\\n", "").splitlines()
    probes = [line for line in lines if line.startswith("RUN ") and SCRIPT.name in line]
    assert len(probes) == 1
    parts = shlex.split(probes[0])
    assert "--network=none" in parts
    mount = next(part for part in parts if part.startswith("--mount="))
    options = dict(
        option.split("=", 1) for option in mount.removeprefix("--mount=").split(",")
    )
    assert options["type"] == "bind"
    assert options["source"] == SCRIPT.relative_to(root).as_posix()
    assert parts[-3:] == ["python", "-I", options["target"]]
    last_install = max(
        i for i, line in enumerate(lines) if line.startswith("RUN pip install")
    )
    last_stage = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    assert last_stage < last_install < lines.index(probes[0])


@pytest.mark.parametrize("component", ("orchestrator", "vm-controller"))
def test_probe_changes_rebuild_tilt_and_develop_images(component):
    root = SCRIPT.parents[1]
    source = SCRIPT.relative_to(root).as_posix()
    tree = ast.parse((root / "Tiltfile").read_text())
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "docker_build"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == f"srw-{component}"
    )
    watched = next(item.value for item in build.keywords if item.arg == "only")
    assert source in ast.literal_eval(watched)
    if component == "orchestrator":
        fallback = next(
            node
            for node in ast.walk(build)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "fall_back_on"
        )
        assert source in ast.literal_eval(fallback.args[0])

    workflow = (root / ".github/workflows/develop.yml").read_text()
    variable = component.upper().replace("-", "_")
    paths = re.search(
        rf"^\s*{variable}_PATHS=\((.*?)\)", workflow, re.MULTILINE | re.DOTALL
    )
    assert paths is not None
    assert source in shlex.split(paths.group(1))
    # The same inputs feed identity and the registry-existence rebuild predicate.
    assert f'{variable}_SHA=$(last_input_sha "${{{variable}_PATHS[@]}}")' in workflow
    assert f'{variable}=$(image_missing {component} "${variable}_SHA")' in workflow
