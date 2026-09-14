"""Installation-owned, public-IPv4 preparation firewall contract."""

from copy import deepcopy
from ipaddress import ip_network


DEFAULT_BLOCKED_CIDRS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def firewall_policy(value):
    """Accept only the bounded operator profile, never raw firewall commands."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "networkEnabled", "blockedCidrs"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["networkEnabled"]) is not bool
        or not isinstance(value["blockedCidrs"], list)
        or not 1 <= len(value["blockedCidrs"]) <= 32
    ):
        raise ValueError("Invalid preparation Pod firewall profile.")
    networks = []
    for cidr in value["blockedCidrs"]:
        if not isinstance(cidr, str) or len(cidr) > 32:
            raise ValueError("Preparation Pod firewall requires IPv4 CIDRs.")
        try:
            network = ip_network(cidr, strict=True)
        except ValueError:
            raise ValueError("Preparation Pod firewall requires IPv4 CIDRs.") from None
        if network.version != 4 or str(network) != cidr:
            raise ValueError("Preparation Pod firewall requires canonical IPv4 CIDRs.")
        networks.append(network)
    if any(
        not any(ip_network(required).subnet_of(network) for network in networks)
        for required in DEFAULT_BLOCKED_CIDRS
    ):
        raise ValueError(
            "Preparation Pod firewall must exclude private and special-use ranges."
        )
    return deepcopy(value)
