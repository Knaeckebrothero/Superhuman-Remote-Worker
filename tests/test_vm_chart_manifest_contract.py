"""Contract tests over the REAL rendered VM manifests.

Why this file exists
--------------------
Every VM test that renders a *fabricated* stand-in template has passed while the
real chart was broken — that is how the description-escaping bug survived a
passing "special characters" test (see
``tests/test_vm_template_description_escaping.py`` and
``docs/issues/vm_reliability_assessment.md`` P2-12). So this module renders the
actual charts with ``helm template`` and asserts on the real output. A fixture
that simplifies the artifact under test cannot catch structural defects in it.

What it pins
------------
1. The cloud-init file ``/etc/default/management-daemon`` carries every env key
   ``management-daemon.py`` hard-requires. A missing key means the guest boots,
   the daemon exits 1, and the VM **never registers** — the failure looks like a
   provisioning timeout with no error anywhere.
2. Placeholder closure: every ``${X}`` a chart emits is actually substituted by
   the renderer that consumes that chart. An unsubstituted placeholder reaches
   the guest verbatim.
3. ``authorized_keys`` is non-empty at chart defaults, so a default install
   cannot silently produce a VM nobody can SSH into.
4. The two charts stay in parity. They are separate copies of one template and
   have drifted before (``4d7616cf`` updated only one of them).
5. The rendered userData fits KubeVirt's 2048-byte inline ``cloudInitNoCloud``
   limit at a maximum-length description.

The required-key and substitution sets are **parsed from source**, not
hardcoded, so adding a required env var or a new placeholder updates the
contract automatically instead of silently widening the gap.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

DAEMON_SRC = ROOT / "docker/agent-vm-base/files/management-daemon.py"
CONTROLLER_SRC = ROOT / "vm/controller/controller.py"
PROVISIONER_SRC = ROOT / "orchestrator/services/vm_provisioner.py"

# KubeVirt refuses an inline cloudInitNoCloud userData larger than this.
KUBEVIRT_USERDATA_LIMIT = 2048

# orchestrator/services/vm_provisioner.py caps the description at this length;
# the budget assertion renders at the cap rather than at a convenient short value.
MAX_DESCRIPTION_LEN = 200

AUTHORIZED_KEYS_PATH = "/etc/ssh/authorized_keys/agent-host"
DAEMON_ENV_PATH = "/etc/default/management-daemon"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not installed"
)


@dataclass(frozen=True)
class Chart:
    """A chart that renders a VM template, and how to render it."""

    name: str
    path: str
    values: str

    def __str__(self) -> str:  # keeps parametrised test ids readable
        return self.name


# Both charts carry a copy of the same VM template and both are consumed by the
# VM controller. `helm/` is the same-cluster path, `helm-vm-cluster/` the
# cross-cluster one.
CHARTS = [
    Chart("helm", "helm", "helm/ci/vm-values.yaml"),
    Chart("helm-vm-cluster", "helm-vm-cluster", "helm-vm-cluster/ci/default-values.yaml"),
]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def attempt_render(chart: Chart, *overrides: str) -> subprocess.CompletedProcess[str]:
    """Run ``helm template`` and hand back the result, success or not.

    A chart *refusing* to render is a legitimate outcome for some contracts —
    fail-fast on missing required input is better than emitting a broken VM —
    so those tests need the raw result rather than an assertion.
    """
    cmd = ["helm", "template", "t", str(ROOT / chart.path), "-f", str(ROOT / chart.values)]
    for override in overrides:
        cmd += ["--set", override]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def render_chart(chart: Chart, *overrides: str) -> str:
    """Render a chart with ``helm template``; fail loudly with helm's stderr."""
    result = attempt_render(chart, *overrides)
    assert result.returncode == 0, f"helm template failed for {chart}:\n{result.stderr}"
    return result.stdout


def vm_template(rendered: str) -> dict:
    """Pull the VirtualMachine manifest out of the rendered ConfigMap."""
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "ConfigMap":
            continue
        data = doc.get("data") or {}
        if "vm-template.yaml" in data:
            return yaml.safe_load(data["vm-template.yaml"])
    raise AssertionError("no ConfigMap carrying vm-template.yaml in the render")


def user_data(vm: dict) -> str:
    """The inline cloud-init userData block from the VM manifest."""
    for volume in vm["spec"]["template"]["spec"]["volumes"]:
        cloud_init = volume.get("cloudInitNoCloud")
        if cloud_init:
            return cloud_init["userData"]
    raise AssertionError("no cloudInitNoCloud volume in the VM template")


def write_files(user_data_text: str) -> dict[str, str]:
    """Map cloud-init ``write_files`` path -> content."""
    parsed = yaml.safe_load(user_data_text)
    return {entry["path"]: entry.get("content", "") for entry in parsed["write_files"]}


def env_keys(content: str) -> set[str]:
    """Env-file keys defined in a ``KEY=value`` block."""
    return set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", content, re.MULTILINE))


def placeholders(text: str) -> set[str]:
    """Every ``${NAME}`` token in a rendered template."""
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", text))


# ---------------------------------------------------------------------------
# Contract sources — parsed from the code that owns each contract
# ---------------------------------------------------------------------------


def _env_key_of(node: ast.AST) -> str | None:
    """The literal key of an ``os.environ.get("KEY")`` anywhere inside ``node``."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            return sub.args[0].value
    return None


def daemon_required_env() -> set[str]:
    """Env vars ``management-daemon.py`` exits 1 without.

    Derived from the guard itself so the contract tracks the daemon.
    """
    tree = ast.parse(DAEMON_SRC.read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "load_config"
    )

    var_to_key: dict[str, str] = {}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            key = _env_key_of(node.value)
            if key:
                var_to_key[node.targets[0].id] = key

    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        exits = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "exit"
            for stmt in node.body
            for inner in ast.walk(stmt)
        )
        if not exits:
            continue
        required = {
            var_to_key[name.id]
            for name in ast.walk(node.test)
            if isinstance(name, ast.Name) and name.id in var_to_key
        }
        if required:
            return required

    raise AssertionError("could not locate the required-env guard in management-daemon.py")


def _replacements_in(source: Path) -> set[str]:
    """Placeholder names a renderer substitutes, read from its replacements dict."""
    text = source.read_text()
    match = re.search(r"replacements\s*=\s*\{(.*?)\n\s*\}", text, re.S)
    assert match, f"no replacements dict found in {source}"
    return set(re.findall(r'"\$\{([A-Z_][A-Z0-9_]*)\}"', match.group(1)))


def controller_replacements() -> set[str]:
    return _replacements_in(CONTROLLER_SRC)


def direct_mode_replacements() -> set[str]:
    return _replacements_in(PROVISIONER_SRC)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chart", CHARTS, ids=str)
def test_daemon_env_file_supplies_every_required_key(chart: Chart) -> None:
    """A VM whose daemon env file is missing a key boots and never registers."""
    files = write_files(user_data(vm_template(render_chart(chart))))
    assert DAEMON_ENV_PATH in files, f"{chart} writes no {DAEMON_ENV_PATH}"

    supplied = env_keys(files[DAEMON_ENV_PATH])
    missing = daemon_required_env() - supplied

    assert not missing, (
        f"{chart} omits {sorted(missing)} from {DAEMON_ENV_PATH}. "
        "management-daemon.py exits 1 without them, so the VM boots but never "
        "registers and the job fails as a provisioning timeout with no error."
    )


@pytest.mark.parametrize("chart", CHARTS, ids=str)
def test_every_emitted_placeholder_is_substituted(chart: Chart) -> None:
    """An unsubstituted ``${X}`` reaches the guest verbatim."""
    vm = vm_template(render_chart(chart))
    emitted = placeholders(yaml.safe_dump(vm))
    unsubstituted = emitted - controller_replacements()

    assert not unsubstituted, (
        f"{chart} emits {sorted(unsubstituted)}, which the VM controller never "
        "substitutes — the literal placeholder text lands in the guest."
    )


@pytest.mark.parametrize("chart", CHARTS, ids=str)
def test_no_chart_renders_an_empty_authorized_keys(chart: Chart) -> None:
    """With no SSH key configured, refuse to render — never emit an empty file.

    Both outcomes below are acceptable; only the third is a defect:
      * the chart fails the render and says which value is missing (preferred),
      * the chart renders a real key,
      * the chart renders SUCCESSFULLY with an empty authorized_keys — the VM
        then boots, reports ready, and every SSH into it fails.

    ``helm-vm-cluster`` takes the first path. ``helm`` takes the third.
    """
    result = attempt_render(chart, "vmController.vmSshPublicKey=", "ssh.publicKey=")

    if result.returncode != 0:
        assert "ssh" in result.stderr.lower(), (
            f"{chart} refused to render, but not because of the SSH key:\n{result.stderr}"
        )
        return

    files = write_files(user_data(vm_template(result.stdout)))
    assert AUTHORIZED_KEYS_PATH in files, f"{chart} writes no {AUTHORIZED_KEYS_PATH}"

    assert files[AUTHORIZED_KEYS_PATH].strip(), (
        f"{chart} rendered successfully with an EMPTY {AUTHORIZED_KEYS_PATH}. "
        "The VM boots and reports ready, and every SSH into it fails. Either "
        "supply a default key or fail the render, as helm-vm-cluster does."
    )


def test_vault_key_branch_emits_no_unsubstituted_placeholder() -> None:
    """The Vault/ESO key branch must not emit a placeholder nobody substitutes.

    ``helm-vm-cluster`` has two SSH-key branches. The inline ``ssh.publicKey``
    branch templates the real key at render time. The ``ssh.publicKeyVaultPath``
    branch instead emits a literal ``${SSH_AUTHORIZED_KEY}`` for the controller
    to fill from the synced Secret at provision time — but the controller has no
    such substitution, so that literal reaches the guest as authorized_keys.

    Only the inline branch is exercised elsewhere in this file, which is exactly
    why this defect survived: the broken branch is the one CI never renders.
    """
    chart = CHARTS[1]
    rendered = render_chart(
        chart,
        "ssh.publicKey=",
        "ssh.publicKeyVaultPath=secret/data/srw/vm-ssh",
    )
    unsubstituted = placeholders(
        yaml.safe_dump(vm_template(rendered))
    ) - controller_replacements()

    assert not unsubstituted, (
        f"{chart} on the Vault-key branch emits {sorted(unsubstituted)}, which "
        "the VM controller never substitutes. Every Vault-path install gets a "
        "literal placeholder as its authorized_keys and all SSH fails."
    )


def test_charts_agree_on_daemon_env_keys() -> None:
    """The two copies of the VM template must not drift.

    They already have: ``4d7616cf`` added ORCHESTRATOR_ID to one chart only.
    """
    supplied = {
        chart.name: env_keys(
            write_files(user_data(vm_template(render_chart(chart))))[DAEMON_ENV_PATH]
        )
        for chart in CHARTS
    }
    first, second = CHARTS[0].name, CHARTS[1].name

    assert supplied[first] == supplied[second], (
        f"the charts disagree on {DAEMON_ENV_PATH}: "
        f"{first}-only={sorted(supplied[first] - supplied[second])}, "
        f"{second}-only={sorted(supplied[second] - supplied[first])}"
    )


def test_charts_write_the_same_files() -> None:
    """Both charts must provision the same set of cloud-init files."""
    paths = {
        chart.name: set(write_files(user_data(vm_template(render_chart(chart)))))
        for chart in CHARTS
    }
    first, second = CHARTS[0].name, CHARTS[1].name

    assert paths[first] == paths[second], (
        f"write_files drift: {first}-only={sorted(paths[first] - paths[second])}, "
        f"{second}-only={sorted(paths[second] - paths[first])}"
    )


# A rendered chart still holds `${...}` placeholders; the controller expands them
# at provision time, and real values are longer than the placeholder text. So
# measuring the rendered template alone UNDERSTATES the size KubeVirt admits.
#
# These widths are GROUNDED in real values (live srw-config, real image refs,
# UUID lengths, hard caps) rather than estimated, because the margin turns out to
# be a handful of bytes and a guessed width would decide the result.
SUBSTITUTION_WIDTHS = {
    "DESCRIPTION": MAX_DESCRIPTION_LEN,  # hard cap in vm_provisioner
    "JOB_ID": 36,  # UUID
    "ORCHESTRATOR_ID": 36,  # UUID
    "OWNER_ID": 36,  # UUID
    "OWNER_KIND": len("persistent_thread"),  # longest real value
    "NATS_URL": len("nats://nats.nats.svc.cluster.local:4222"),  # live srw-config
    "HEADSCALE_URL": len("https://headscale.h4ll.app"),  # live srw-config
    "VM_IMAGE": len(
        "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:sha-5eb436e"
    ),
    "SSH_AUTHORIZED_KEY": len("ssh-ed25519 ") + 68 + len(" agent@srw"),  # ed25519
    "TAILSCALE_AUTH_KEY": 48,  # headscale pre-auth key
    "AGENT_CONFIG": len("worker_base"),
    "VM_STORAGE_CLASS": len("local-path"),
    "VM_DISK_SIZE": len("20Gi"),
    "CPU_CORES": 1,
    "MEMORY": len("16Gi"),
}

# Minimum bytes that must remain under the limit. The point is early warning: at
# a few bytes of margin, one longer image tag or a renamed field silently makes
# KubeVirt reject every VM, and the hard-limit assertion gives no notice. 64 B is
# roughly one longer identifier.
MIN_USERDATA_HEADROOM = 64


@pytest.mark.parametrize("chart", CHARTS, ids=str)
def test_userdata_fits_kubevirt_inline_limit(chart: Chart) -> None:
    """Real output, with placeholders expanded to realistic substituted widths.

    Over the limit means KubeVirt rejects the VM outright at admission.
    """
    text = user_data(vm_template(render_chart(chart)))

    worst_case = text
    for name, width in SUBSTITUTION_WIDTHS.items():
        worst_case = worst_case.replace("${%s}" % name, "x" * width)

    unmodelled = placeholders(worst_case)
    assert not unmodelled, (
        f"{sorted(unmodelled)} has no width in SUBSTITUTION_WIDTHS, so this "
        "budget would silently understate the rendered size. Add it."
    )

    size = len(worst_case.encode())
    assert size <= KUBEVIRT_USERDATA_LIMIT, (
        f"{chart} userData is {size} B once substituted, "
        f"{size - KUBEVIRT_USERDATA_LIMIT} B over KubeVirt's "
        f"{KUBEVIRT_USERDATA_LIMIT} B inline limit. KubeVirt rejects the VM at "
        "admission, so every VM-backed job fails to provision."
    )


@pytest.mark.parametrize("chart", CHARTS, ids=str)
def test_userdata_keeps_a_usable_margin(chart: Chart) -> None:
    """Early warning, separate from the hard limit.

    The limit is a cliff: one byte over and KubeVirt rejects every VM. At a
    handful of bytes of margin the hard-limit test still passes and gives no
    notice, so the margin gets its own contract.
    """
    text = user_data(vm_template(render_chart(chart)))
    for name, width in SUBSTITUTION_WIDTHS.items():
        text = text.replace("${%s}" % name, "x" * width)

    headroom = KUBEVIRT_USERDATA_LIMIT - len(text.encode())
    assert headroom >= MIN_USERDATA_HEADROOM, (
        f"{chart} has only {headroom} B of userData headroom "
        f"(want >= {MIN_USERDATA_HEADROOM} B). It still fits today, but one "
        "longer image tag, hostname or identifier pushes it over the cliff and "
        "KubeVirt starts rejecting every VM. Move to a UserDataSecretRef or trim "
        "the block."
    )


def test_direct_mode_renderer_substitutes_every_placeholder() -> None:
    """Direct K8s mode renders the same template with its own replacements dict.

    It is the documented same-cluster path
    (``docs/features/single_cluster_vm_deployment.md``), so its dict must cover
    the template too — a second renderer is only safe if it stays in parity.
    """
    emitted = placeholders(
        yaml.safe_dump(vm_template(render_chart(CHARTS[0])))
    ) | placeholders(yaml.safe_dump(vm_template(render_chart(CHARTS[1]))))
    missing = emitted - direct_mode_replacements() - {"SSH_AUTHORIZED_KEY"}

    assert not missing, (
        "vm_provisioner._render_template (direct K8s mode) never substitutes "
        f"{sorted(missing)}. A direct-mode VM boots with those literals; "
        "ORCHESTRATOR_ID missing alone makes the daemon exit 1."
    )
