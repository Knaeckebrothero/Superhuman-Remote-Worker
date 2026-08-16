"""Behavioural guards for the shared email layout.

Each assertion here corresponds to a documented client failure or an
accessibility requirement -- see docs/features/email_and_login_theme_alignment.md.
"""

import re

from orchestrator.services import brand
from orchestrator.services.email_layout import Action, render_email

PALETTE_HEXES = set(brand.TRAVERTINE.values())


def test_escapes_title_and_subtitle_but_not_body() -> None:
    html = render_email(
        title='Job <img src=x onerror=alert(1)>',
        subtitle='Agent "><b>bold</b>',
        body_html="<p>trusted markup</p>",
    )
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert "<b>bold</b>" not in html
    assert "<p>trusted markup</p>" in html


def test_escapes_action_labels() -> None:
    html = render_email(
        title="t",
        body_html="<p>b</p>",
        actions=[Action(label="<script>x</script>", url="https://e.test/a")],
    )
    assert "<script>" not in html


def test_entity_encodes_non_ascii() -> None:
    """Gmail clips on non-ASCII characters independently of message size."""
    html = render_email(title="Deploy", body_html="<p>em—dash and ©</p>")
    assert "—" not in html
    assert "©" not in html
    assert "&#8212;" in html
    assert html.isascii()


def test_deny_is_a_ghost_button_not_a_second_solid_fill() -> None:
    """#446b3e vs #9c2832 is 1.24:1 -- they must differ by form, not hue."""
    html = render_email(
        title="Permission requested",
        body_html="<p>b</p>",
        actions=[
            Action(label="Approve", url="https://e.test/y", variant="approve"),
            Action(label="Deny", url="https://e.test/n", variant="deny"),
        ],
    )
    approve_cell = re.search(r'<td[^>]*>\s*<a[^>]*>Approve</a>', html, re.S)
    deny_cell = re.search(r'<td[^>]*>\s*<a[^>]*>Deny</a>', html, re.S)
    assert approve_cell and deny_cell
    assert f"background-color:{brand.TRAVERTINE['success']}" in approve_cell.group(0)
    # Ghost: card-coloured fill + danger border, never a danger fill.
    assert f"background-color:{brand.TRAVERTINE['panel-bg']}" in deny_cell.group(0)
    assert f"border:2px solid {brand.TRAVERTINE['danger']}" in deny_cell.group(0)


def test_buttons_are_padded_table_cells_not_padded_anchors() -> None:
    """Outlook Windows lacks `display`, so an <a> stays inline and vertical
    padding cannot expand the line."""
    html = render_email(
        title="t", body_html="<p>b</p>",
        actions=[Action(label="Go", url="https://e.test/g")],
    )
    anchor = re.search(r'<a [^>]*>Go</a>', html).group(0)
    assert "padding" not in anchor


def test_link_colour_is_inline_not_only_in_style_block() -> None:
    """GMX, WEB.DE, SFR and LaPoste strip <head><style> outright."""
    html = render_email(
        title="t", body_html="<p>b</p>",
        actions=[Action(label="Go", url="https://e.test/g")],
    )
    anchor = re.search(r'<a [^>]*>Go</a>', html).group(0)
    assert "color:" in anchor


def test_no_border_radius_and_no_web_fonts() -> None:
    html = render_email(title="t", body_html="<p>b</p>")
    assert "border-radius" not in html
    assert "fonts.googleapis.com" not in html
    assert "@font-face" not in html


def test_uppercase_is_css_not_baked_into_text() -> None:
    """Screen readers read the source; all-caps tokens get spelled out."""
    html = render_email(title="Permission requested", body_html="<p>b</p>")
    assert "PERMISSION REQUESTED" not in html
    assert "text-transform:uppercase" in html


def test_declares_light_colour_scheme_both_ways() -> None:
    """The meta tag alone is inert on every Apple Mail since 2019."""
    html = render_email(title="t", body_html="<p>b</p>")
    assert '<meta name="color-scheme" content="light">' in html
    assert "color-scheme: light" in html


def test_has_article_wrapper_with_lang_and_dir() -> None:
    """Webmail clients strip <html>, taking lang/dir with it."""
    html = render_email(title="t", body_html="<p>b</p>")
    assert 'role="article"' in html
    assert re.search(r'<div[^>]*role="article"[^>]*lang="en"[^>]*dir="ltr"', html)


def test_layout_tables_are_presentational() -> None:
    html = render_email(title="t", body_html="<p>b</p>")
    for table in re.findall(r"<table[^>]*>", html):
        assert 'role="presentation"' in table


def test_uses_no_unmanaged_colours() -> None:
    """Catches the way drift actually starts: a one-off hex nobody notices."""
    html = render_email(
        title="t", body_html="<p>b</p>", footer_note="n",
        actions=[
            Action(label="A", url="https://e.test/a", variant="approve"),
            Action(label="D", url="https://e.test/d", variant="deny"),
        ],
    )
    used = {brand.normalize_hex(h) for h in re.findall(r"#[0-9a-fA-F]{3,6}\b", html)}
    assert used <= PALETTE_HEXES, f"unmanaged colours: {sorted(used - PALETTE_HEXES)}"
