"""asyncssh must stay at or above the CVE-2026-62949 fix.

CVE-2026-62949 (GHSA-rw4j-r22c-9gc3, <=2.23.1, fixed 2.24.0): an authenticated
client opening a channel with maximum packet size 0 drives asyncssh's event loop
into an infinite synchronous loop, freezing every connection in the process.
The ssh-gateway is multi-tenant, so this is a total availability loss.
"""

import pathlib
import re

import pytest

REQUIREMENTS = (
    pathlib.Path(__file__).resolve().parents[1] / "requirements.txt",
    pathlib.Path(__file__).resolve().parents[1] / "orchestrator" / "requirements.txt",
)
MINIMUM = (2, 24, 0)


def _parse(spec: str) -> tuple[int, ...]:
    return tuple(int(part) for part in spec.split("."))


@pytest.mark.parametrize("path", REQUIREMENTS, ids=lambda p: p.name)
def test_asyncssh_floor_covers_cve_2026_62949(path):
    pins = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip().startswith("asyncssh")
    ]
    assert pins, f"no asyncssh pin found in {path}"
    for pin in pins:
        match = re.fullmatch(r"asyncssh>=([0-9.]+)", pin)
        assert match, f"expected an `asyncssh>=X.Y.Z` pin, got {pin!r}"
        assert _parse(match.group(1)) >= MINIMUM, (
            f"{pin} permits versions vulnerable to CVE-2026-62949"
        )
