"""Trusted init container: install Pod-local rules before any builder starts."""

import argparse
from ipaddress import ip_address
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from shared.workspace_preparation_network import firewall_policy


def resolver_addresses(source):
    addresses = []
    for line in source.splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields or fields[0] != "nameserver":
            continue
        if len(fields) != 2:
            raise ValueError("Invalid Pod DNS resolver configuration.")
        try:
            address = ip_address(fields[1])
        except ValueError:
            raise ValueError("Invalid Pod DNS resolver configuration.") from None
        if address.version != 4:
            raise ValueError("Preparation Pod firewall requires IPv4 DNS resolvers.")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not 1 <= len(addresses) <= 3:
        raise ValueError(
            "Preparation Pod firewall requires one to three DNS resolvers."
        )
    return addresses


def firewall_rules(policy, *, resolv_conf=""):
    policy = firewall_policy(policy)
    base = ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"]
    ipv4 = list(base)
    if policy["networkEnabled"]:
        # DNS exceptions are exact IP/port pairs. Other private-destination
        # traffic, including existing connections, must still pass the drops.
        for address in resolver_addresses(resolv_conf):
            for protocol in ("udp", "tcp"):
                ipv4.append(
                    f"-A OUTPUT -d {address}/32 -p {protocol} --dport 53 -j ACCEPT"
                )
        for cidr in policy["blockedCidrs"]:
            ipv4.append(f"-A OUTPUT -d {cidr} -j DROP")
        for port in (80, 443):
            ipv4.append(f"-A OUTPUT -p tcp --dport {port} -j ACCEPT")
        ipv4.append("-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT")
    return {
        "ipv6": "\n".join([*base, "COMMIT", ""]),
        "ipv4": "\n".join([*ipv4, "COMMIT", ""]),
    }


def install(policy, *, resolv_conf):
    rules = firewall_rules(policy, resolv_conf=resolv_conf)
    # A failure leaves the init container failed, so Kubernetes cannot start
    # the builder. Each family is replaced atomically in this Pod's netns.
    for family, executable in (
        ("ipv6", "/usr/sbin/ip6tables-restore"),
        ("ipv4", "/usr/sbin/iptables-restore"),
    ):
        subprocess.run(
            [executable, "--wait", "5"],
            input=rules[family],
            text=True,
            check=True,
            capture_output=True,
            timeout=15,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    try:
        # This entry point is for the controller's init container, not a local
        # operator command. The UID arrives through Kubernetes' downward API.
        UUID(os.environ["SRW_PREPARATION_POD_UID"])
        if os.geteuid() != 0 or len(args.policy) > 4096:
            raise ValueError("Invalid preparation firewall invocation.")
        policy = firewall_policy(json.loads(args.policy))
        install(policy, resolv_conf=Path("/etc/resolv.conf").read_text())
        receipt = {
            "version": 1,
            "phase": "Installed",
            "networkEnabled": policy["networkEnabled"],
        }
        Path("/dev/termination-log").write_text(json.dumps(receipt))
    except Exception:
        print("Preparation Pod firewall could not be installed.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
