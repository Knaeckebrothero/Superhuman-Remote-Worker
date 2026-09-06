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


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG 2.x relative-luminance contrast ratio between two hex colours."""

    def _luminance(hexstr: str) -> float:
        h = hexstr.lstrip("#")
        channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]

        def _linearize(c: float) -> float:
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (_linearize(c) for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lum_a, lum_b = _luminance(hex_a), _luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def test_footer_text_colour_meets_wcag_aa_on_light_surfaces() -> None:
    """text-muted fails AA (needs 4.5:1) on every Travertine surface it was
    used on -- 3.26:1 on surface-0, 3.82:1 on panel-bg, 3.50:1 on app-bg.
    Footer/legal/muted copy uses text-secondary instead (7.06-8.27:1 on those
    same surfaces). This guards against text-muted quietly creeping back into
    that role.
    """
    footer_colour = brand.TRAVERTINE["text-secondary"]
    for surface in ("panel-bg", "surface-0", "app-bg"):
        ratio = _contrast_ratio(footer_colour, brand.TRAVERTINE[surface])
        assert ratio >= 4.5, (
            f"text-secondary on {surface} is {ratio:.2f}:1 -- AA needs 4.5:1"
        )


def test_text_muted_itself_fails_aa_on_light_surfaces() -> None:
    """Documents *why* the ruling above exists: text-muted is not usable for
    body-weight text against any current Travertine surface. If this ever
    starts passing (e.g. the token's value changes), the ruling that moved
    footer copy to text-secondary should be revisited.
    """
    muted = brand.TRAVERTINE["text-muted"]
    for surface in ("panel-bg", "surface-0", "app-bg"):
        ratio = _contrast_ratio(muted, brand.TRAVERTINE[surface])
        assert ratio < 4.5, (
            f"text-muted on {surface} is now {ratio:.2f}:1 (>= 4.5:1) -- "
            "the text-muted-fails-AA premise behind the text-secondary "
            "ruling no longer holds; re-check whether footer text should "
            "move back."
        )
