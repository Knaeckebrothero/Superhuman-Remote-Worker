"""Fail-closed preparation networking before an authored command can run."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock
from uuid import uuid4

import pytest

from shared.workspace_preparation_network import DEFAULT_BLOCKED_CIDRS, firewall_policy
from shared.workspace_preparation_settings import PreparationSettings
from vm_controller import preparation_firewall as firewall
from vm_controller.preparation_manifests import builder_pod


def policy(online=True):
    return {
        "version": 1,
        "networkEnabled": online,
        "blockedCidrs": list(DEFAULT_BLOCKED_CIDRS),
    }


@pytest.mark.parametrize("online", [True, False])
def test_firewall_is_a_trusted_init_with_no_guest_disk_or_authored_input(online):
    image = "registry.example/preparer@sha256:" + "2" * 64
    pod = builder_pod(
        namespace="test",
        name="build",
        uid=str(uuid4()),
        disk="owned-disk",
        input_name="authored-input",
        image=image,
        timeout=300,
        pod_firewall=policy(online),
    )
    spec = pod["spec"]
    assert not any(
        spec.get(key)
        for key in ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")
    )
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    (init,) = spec["initContainers"]
    assert init["image"] == image
    assert init["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["NET_ADMIN"],
    }
    assert init["securityContext"]["allowPrivilegeEscalation"] is False
    assert not init["securityContext"].get("privileged")
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert init["volumeMounts"] == [{"name": "firewall-runtime", "mountPath": "/run"}]
    assert init["env"] == [
        {
            "name": "SRW_PREPARATION_POD_UID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
        }
    ]
    assert json.loads(init["command"][-1]) == policy(online)
    (container,) = spec["containers"]
    assert spec["securityContext"]["runAsUser"] == 107
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert not any(v["name"] == "firewall-runtime" for v in container["volumeMounts"])
    assert not any("secret" in v or "hostPath" in v for v in spec["volumes"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(networkEnabled="true"),
        lambda p: p.update(command=["allow-all"]),
        lambda p: p.update(blockedCidrs=[]),
        lambda p: p["blockedCidrs"].remove("169.254.0.0/16"),
        lambda p: p["blockedCidrs"].remove("100.64.0.0/10"),
        lambda p: p["blockedCidrs"].append("10.0.0.0/8\nCOMMIT"),
        lambda p: p["blockedCidrs"].append("10.0.0.1/8"),
        lambda p: p["blockedCidrs"].append("::/0"),
        lambda p: p.update(blockedCidrs=["0.0.0.0/0"] * 33),
    ],
)
def test_invalid_or_weakened_operator_profiles_are_rejected(mutation):
    value = policy()
    mutation(value)
    with pytest.raises(ValueError):
        firewall_policy(value)


def test_operator_may_add_or_broaden_exclusions_without_mutating_input():
    value = policy()
    value["blockedCidrs"].append("203.0.113.0/24")
    before = deepcopy(value)
    accepted = firewall_policy(value)
    accepted["blockedCidrs"].clear()
    assert value == before
    assert firewall_policy({**value, "blockedCidrs": ["0.0.0.0/0"]})


@pytest.mark.parametrize(
    "source",
    [
        "",
        "nameserver ::1",
        "nameserver invalid",
        "nameserver 10.43.0.10 extra",
        "nameserver 1.1.1.1\nnameserver 2.2.2.2\nnameserver 3.3.3.3\nnameserver 4.4.4.4",
    ],
)
def test_unsupported_dns_profiles_fail_before_installation(source):
    with pytest.raises(ValueError):
        firewall.firewall_rules(policy(), resolv_conf=source)


def test_online_dns_is_exact_and_special_ranges_precede_web_egress():
    rules = firewall.firewall_rules(
        policy(),
        resolv_conf="search test.svc\nnameserver 10.43.0.10 # kube-dns\nnameserver 10.43.0.10\n",
    )
    assert "-A OUTPUT" not in rules["ipv6"]
    assert rules["ipv6"].count("DROP") == 3
    lines = rules["ipv4"].splitlines()
    output = [line for line in lines if line.startswith("-A OUTPUT")]
    dns = [line for line in output if "--dport 53" in line]
    assert len(dns) == 2 and all("-d 10.43.0.10/32" in line for line in dns)
    for cidr in DEFAULT_BLOCKED_CIDRS:
        assert lines.index(f"-A OUTPUT -d {cidr} -j DROP") < lines.index(
            "-A OUTPUT -p tcp --dport 443 -j ACCEPT"
        )
    assert not any("conntrack" in line for line in output)
    assert ":INPUT DROP [0:0]" in lines and ":OUTPUT DROP [0:0]" in lines


def test_offline_has_no_exceptions_even_for_dns_or_loopback():
    rules = firewall.firewall_rules(policy(False), resolv_conf="nameserver ::1")
    for value in rules.values():
        assert "ACCEPT" not in value and "-A " not in value
        assert value.count("DROP") == 3


def test_installation_failure_stops_without_running_next_family(monkeypatch):
    run = Mock(side_effect=subprocess.CalledProcessError(2, "private-error"))
    monkeypatch.setattr(firewall.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        firewall.install(policy(False), resolv_conf="")
    run.assert_called_once()
    assert run.call_args.args[0] == ["/usr/sbin/ip6tables-restore", "--wait", "5"]
    assert run.call_args.kwargs["timeout"] == 15
    assert run.call_args.kwargs["check"] is True


def test_entrypoint_requires_explicit_pod_identity_before_mutating_network(
    monkeypatch, capsys
):
    monkeypatch.delenv("SRW_PREPARATION_POD_UID", raising=False)
    monkeypatch.setattr("sys.argv", ["firewall", "--policy", json.dumps(policy(False))])
    install = Mock()
    monkeypatch.setattr(firewall, "install", install)
    assert firewall.main() == 1
    install.assert_not_called()
    assert (
        capsys.readouterr().out == "Preparation Pod firewall could not be installed.\n"
    )


def test_failed_installation_never_writes_a_success_receipt(monkeypatch, capsys):
    monkeypatch.setenv("SRW_PREPARATION_POD_UID", str(uuid4()))
    monkeypatch.setattr("sys.argv", ["firewall", "--policy", json.dumps(policy(False))])
    monkeypatch.setattr(firewall.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        firewall, "install", Mock(side_effect=RuntimeError("PRIVATE_SENTINEL"))
    )
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "nameserver 10.43.0.10")
    write = Mock()
    monkeypatch.setattr(Path, "write_text", write)
    assert firewall.main() == 1
    write.assert_not_called()
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def test_settings_require_a_versioned_profile_and_reject_unmapped_egress(monkeypatch):
    monkeypatch.setenv("VM_PREPARATION_ENABLED", "true")
    monkeypatch.setenv(
        "VM_PREPARATION_IMAGE", "registry.example/preparer@sha256:" + "2" * 64
    )
    monkeypatch.setenv("VM_PREPARATION_POD_FIREWALL", "true")
    monkeypatch.setenv("VM_PREPARATION_NETWORK_ENABLED", "false")
    monkeypatch.delenv("VM_PREPARATION_NETWORK_POLICY_REVISION", raising=False)
    with pytest.raises(ValueError, match="versioned"):
        PreparationSettings.from_environment()
    monkeypatch.setenv("VM_PREPARATION_NETWORK_POLICY_REVISION", "a" * 64)
    assert PreparationSettings.from_environment().firewall == policy(False)
    monkeypatch.setenv(
        "VM_PREPARATION_ADDITIONAL_EGRESS",
        '[{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}]',
    )
    with pytest.raises(ValueError, match="additional egress"):
        PreparationSettings.from_environment()
