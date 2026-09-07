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
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "requirements.txt",
)
MINIMUM = (2, 24, 0)


def _parse(spec: str) -> tuple[int, ...]:
    # Zero-pad to three components. Without this, `asyncssh>=2.24` — a fully
    # compliant pin, and the two-component style already used by Jinja2 and
    # cryptography in these same files — parses to (2, 24), which Python ranks
    # BELOW (2, 24, 0) because a shorter tuple loses on an equal prefix.
    parts = tuple(int(part) for part in spec.split("."))
    return parts + (0,) * (3 - len(parts))


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


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("2.24", True),
        ("2.24.0", True),
        ("2.24.1", True),
        ("3.0", True),
        ("2.23.1", False),
        ("2.21.0", False),
        ("2.9", False),
    ],
)
def test_version_comparison_handles_short_and_long_pins(spec, expected):
    assert (_parse(spec) >= MINIMUM) is expected
