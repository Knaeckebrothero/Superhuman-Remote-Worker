from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _render_workspace_policies(*, stateless_enabled: bool | None = None) -> list[dict]:
    chart = ROOT / "helm"
    command = [
        "helm",
        "template",
        "stateless-workspace-policy-test",
        str(chart),
        "-f",
        str(chart / "ci/test-values.yaml"),
        "--show-only",
        "templates/workspace-network-policy.yaml",
    ]
    if stateless_enabled is not None:
        command.extend(
            [
                "--set",
                f"agent.stateless.enabled={str(stateless_enabled).lower()}",
            ]
        )
    rendered = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        item
        for item in yaml.safe_load_all(rendered)
        if item
        and item.get("kind") == "NetworkPolicy"
        and "srw.io/network-tier" in item.get("metadata", {}).get("labels", {})
    ]


def _agent_ingress_rule(policy: dict) -> dict:
    expected_ports = {
        ("TCP", 30022),
        ("TCP", 9222),
    }
    matches = [
        rule
        for rule in policy["spec"]["ingress"]
        if {(port.get("protocol"), port.get("port")) for port in rule.get("ports", [])}
        == expected_ports
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_workspace_ingress_admits_stateless_agents_only_when_lane_enabled() -> None:
    disabled = _render_workspace_policies()
    enabled = _render_workspace_policies(stateless_enabled=True)

    # The chart renders one policy per configured tier. Exercise every tier so
    # a future per-tier template change cannot silently reopen only one lane.
    assert {
        policy["metadata"]["labels"]["srw.io/network-tier"] for policy in disabled
    } == {
        "home-allowed",
        "internet-only",
    }
    assert {policy["metadata"]["name"] for policy in enabled} == {
        policy["metadata"]["name"] for policy in disabled
    }

    pinned_apps = {"srw-agent", "srw-persistent-agent"}
    for policy in disabled:
        rule = _agent_ingress_rule(policy)
        apps = {peer["podSelector"]["matchLabels"]["app"] for peer in rule["from"]}
        assert apps == pinned_apps

    for policy in enabled:
        rule = _agent_ingress_rule(policy)
        apps = {peer["podSelector"]["matchLabels"]["app"] for peer in rule["from"]}
        assert apps == pinned_apps | {"srw-agent-stateless"}
