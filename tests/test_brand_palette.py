"""Guards the Python copy of the Travertine palette against SCSS drift.

The orchestrator image ships only orchestrator/, src/ and config/ -- it cannot
read cockpit SCSS at runtime, and email cannot use CSS variables at all (Gmail
supports var() but not variable declaration). So the hexes are duplicated into
Python by necessity. This test is the thing that stops that copy rotting the
way the Catppuccin palette did.
"""

import re
from pathlib import Path

import pytest

from orchestrator.services import brand

ROOT = Path(__file__).resolve().parents[1]


def _travertine_block() -> str:
    """Return ONLY the $travertine-theme map body.

    Scoping matters: $senate-theme defines the same keys with different values
    (accent-color is #9c2832 here and #cc4647 there), so a whole-file regex
    matches both and silently compares against the wrong map.
    """
    text = (ROOT / brand.SCSS_TOKEN_SOURCE).read_text()
    start = text.index("$travertine-theme: (")
    end = text.index("\n);", start)
    return text[start:end]


def _parse_scss_hexes(block: str) -> dict[str, str]:
    pairs = re.findall(r"'([a-z0-9-]+)':\s*(#[0-9a-fA-F]{3,8})", block)
    return {k: brand.normalize_hex(v) for k, v in pairs}


def test_scss_source_file_exists() -> None:
    """Fail closed: a rename must break this test, not silently pass it."""
    assert (ROOT / brand.SCSS_TOKEN_SOURCE).is_file(), (
        f"{brand.SCSS_TOKEN_SOURCE} is gone. If the design tokens moved, update "
        "brand.SCSS_TOKEN_SOURCE -- do not delete this test."
    )


def test_travertine_block_is_parseable() -> None:
    """Fail closed: assert we actually found a plausible map, not an empty one."""
    parsed = _parse_scss_hexes(_travertine_block())
    assert len(parsed) >= 20, (
        f"Only parsed {len(parsed)} hex tokens from $travertine-theme; the map "
        "format probably changed. Fix the parser before trusting this suite."
    )


def test_every_python_token_matches_scss() -> None:
    parsed = _parse_scss_hexes(_travertine_block())
    mismatches = []
    for key, py_value in brand.TRAVERTINE.items():
        assert key in parsed, f"token '{key}' no longer exists in $travertine-theme"
        if parsed[key] != py_value:
            mismatches.append(f"  '{key}': \"{parsed[key]}\",  # was {py_value}")
    if mismatches:
        pytest.fail(
            "brand.TRAVERTINE has drifted from the SCSS. Corrected entries:\n"
            + "\n".join(mismatches)
        )


def test_normalize_hex_expands_shorthand() -> None:
    # 'on-accent' is #fff in SCSS; comparing raw strings would false-fail.
    assert brand.normalize_hex("#FFF") == "#ffffff"
    assert brand.normalize_hex("#9C2832") == "#9c2832"
