"""The magic-link landing pages share the email's palette.

They are the click target of the permission email's Approve/Deny buttons, so
they must not be a design generation behind it.
"""

import re

from orchestrator.services import brand

CATPPUCCIN = {
    "#1e1e2e", "#181825", "#313244", "#cdd6f4", "#cba6f7",
    "#a6e3a1", "#f38ba8", "#6c7086", "#a6adc8", "#89b4fa", "#f9e2af",
}


def _pages() -> list[str]:
    from orchestrator.main import (
        _magic_link_confirmation_page,
        _magic_link_result_page,
    )

    return [
        _magic_link_confirmation_page(
            tool_name="run_command",
            tool_args_preview='{"cmd": "ls"}',
            intended_decision="approve",
            token="T1",
        ),
        _magic_link_result_page(
            title="Approved",
            body="The agent may proceed.",
            cockpit_url="https://cockpit.test/",
        ),
        # is_error=True picks a different accent -- cover both branches.
        _magic_link_result_page(
            title="Link expired",
            body="This approval link is no longer valid.",
            cockpit_url="https://cockpit.test/",
            is_error=True,
        ),
    ]


def test_no_catppuccin_remains() -> None:
    for page in _pages():
        found = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", page)}
        assert not (found & CATPPUCCIN), f"Catppuccin remains: {found & CATPPUCCIN}"


def test_pages_use_the_brand_palette() -> None:
    for page in _pages():
        assert brand.TRAVERTINE["panel-bg"] in page
        assert brand.TRAVERTINE["text-primary"] in page


def test_no_text_muted_anywhere() -> None:
    """text-muted fails WCAG AA on every Travertine surface (see
    tests/test_brand_palette.py). Both pages are short enough that a
    document-wide absence check is meaningful -- footer/legal text on these
    pages must use text-secondary instead, so its hex should never appear.
    """
    for page in _pages():
        assert brand.TRAVERTINE["text-muted"] not in page
