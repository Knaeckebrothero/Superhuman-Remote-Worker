"""Runtime configuration contract between orchestrator, daemon, and C plugin."""

from pathlib import Path
import shlex
import shutil
import subprocess

import pytest
import yaml

from orchestrator.services.sudo_gate import _ttl_seconds_from_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plugin_timeout_harness(tmp_path_factory):
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is required to exercise the sudo C plugin")
    executable = tmp_path_factory.mktemp("sudo-plugin") / "timeout-harness"
    subprocess.run(
        [
            compiler,
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(ROOT / "vm/sudo-plugin"),
            "-I",
            str(ROOT / "vm/sudo-plugin/include"),
            str(ROOT / "tests/fixtures/sudo_plugin/timeout_harness.c"),
            str(ROOT / "vm/sudo-plugin/json_util.c"),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


@pytest.mark.parametrize(
    "plugin_config",
    [
        None,
        "docker/agent-vm-base/files/sudo-gate.conf",
        "vm/sudo-plugin/sudo.conf.d/sudo_gate.conf",
    ],
)
def test_plugin_delivers_approval_after_full_server_window(
    plugin_timeout_harness, plugin_config
):
    options = []
    if plugin_config:
        lines = (ROOT / plugin_config).read_text().splitlines()
        options = next(
            shlex.split(line)[3:] for line in lines if line.startswith("Plugin ")
        )
    # Includes 30s for request creation/transport and 10s for reading the plugin
    # request. The actual C parser and poll() call must preserve this budget.
    result = subprocess.run([str(plugin_timeout_harness), "1840000", *options])
    assert result.returncode == 0, (
        "plugin abandoned an approval inside the advertised window"
    )
    result = subprocess.run([str(plugin_timeout_harness), "1860000", *options])
    assert result.returncode == 1, "plugin transport margin must remain bounded"


@pytest.mark.parametrize("ttl", ["90", "1800", "2400"])
def test_custom_ttl_keeps_coordinated_transport_margins(
    plugin_timeout_harness, monkeypatch, ttl
):
    monkeypatch.setenv("SUDO_COMMAND_TTL_SECONDS", ttl)
    approval_seconds = _ttl_seconds_from_env("SUDO_COMMAND_TTL_SECONDS", 1800)
    daemon_seconds = approval_seconds + 30
    plugin_seconds = approval_seconds + 45
    result = subprocess.run(
        [
            str(plugin_timeout_harness),
            str((daemon_seconds + 10) * 1000),
            f"timeout={plugin_seconds}",
        ]
    )
    assert result.returncode == 0
    result = subprocess.run(
        [
            str(plugin_timeout_harness),
            str((plugin_seconds + 1) * 1000),
            f"timeout={plugin_seconds}",
        ]
    )
    assert result.returncode == 1


@pytest.mark.parametrize(
    "path",
    [
        "docker/agent-vm-base/files/sudo-gated-config.yaml",
        "vm/sudo-daemon/config.example.yaml",
    ],
)
def test_shipped_daemon_budget_covers_server_ttl_with_bounded_margin(monkeypatch, path):
    monkeypatch.delenv("SUDO_COMMAND_TTL_SECONDS", raising=False)
    ttl = _ttl_seconds_from_env("SUDO_COMMAND_TTL_SECONDS", 1800)
    settings = yaml.safe_load((ROOT / path).read_text())
    # Consume duration through the Go loader/socket tests as well; this check
    # guards cross-language configuration drift against the server's TTL.
    seconds = int(settings["timeouts"]["nats_request"].removesuffix("s"))
    assert ttl + 30 <= seconds <= ttl + 40
