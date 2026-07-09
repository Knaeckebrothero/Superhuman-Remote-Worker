"""Fail-closed read-only verification for a protected-mount identity.

Given credentials for the dedicated RO account (NOT agent-service — see
docs/design/cloud_access_unification.md §3.3), attempt every mutating
WebDAV verb AND the version/trash side channels that had real RO-bypass
CVEs (Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m). Protected
cloud mode must refuse to engage unless every one is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REJECTED_STATUSES = frozenset({401, 403, 405})

# (verb, note, body-or-None). note documents the side channel; body used
# for endpoints that need a payload to be a fair test.
MUTATING_VERBS: list[tuple[str, str | None, bytes | None]] = [
    ("PUT", None, b"srw-ro-probe"),
    ("DELETE", None, None),
    ("MKCOL", None, None),
    ("MOVE", None, None),
    ("PROPPATCH", None, b'<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:"/>'),
    ("COPY", None, None),
    # side channels with historical RO-bypass CVEs:
    ("POST", "versions-restore", None),
    ("POST", "trash-restore", None),
]


@dataclass
class RoProbeResult:
    ok: bool
    failures: list[str] = field(default_factory=list)


async def probe_read_only(client, base_url: str, path: str) -> RoProbeResult:
    target = base_url.rstrip("/") + "/" + path.lstrip("/")
    failures: list[str] = []
    for verb, note, body in MUTATING_VERBS:
        kwargs = {}
        if body is not None:
            kwargs["content"] = body
        if verb == "MOVE":
            kwargs["headers"] = {"Destination": target + ".moved"}
        resp = await client.request(verb, target, **kwargs)
        if resp.status_code not in REJECTED_STATUSES:
            label = verb if not note else f"{verb} ({note})"
            failures.append(f"{label} -> {resp.status_code} (expected 401/403/405)")
    return RoProbeResult(ok=not failures, failures=failures)
