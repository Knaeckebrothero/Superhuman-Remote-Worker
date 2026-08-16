# Email + Keycloak Theme Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move SRW's transactional email and Keycloak's login/email pages onto the Imperial design system, fixing a live HTML injection and a WCAG 1.4.1 failure along the way.

**Architecture:** A shared Python brand module holds the Travertine palette as literal hexes (CSS variables are unusable in email). One `render_email()` layout function replaces three duplicated inline-HTML call sites and also re-skins the magic-link landing pages those emails link to. Keycloak gets a CSS-only child theme (`parent=keycloak.v2`) delivered as a ConfigMap, plus a one-file FreeMarker email wrapper — no custom image, no `kc.sh build`.

**Tech Stack:** Python 3.12 (orchestrator, FastAPI), pytest, Helm 3, Keycloak 26.2 (PatternFly v5 login theme, FreeMarker email templates), SCSS design tokens in `cockpit/src/styles/themes/_theme-config.scss`.

**Spec:** `docs/features/email_and_login_theme_alignment.md`

## Global Constraints

- **Work directly on `develop`. No feature branch, no worktree** — project convention. Commit per task. **Never `git push`** without explicit authorization.
- **`develop` has concurrent agent writers.** Re-check `git log -1` before each commit; never `git commit --amend`.
- **Palette is Travertine only** (light) for email. Login theme carries Travertine under `:root` and Senate under `.pf-v5-theme-dark`.
- **Email HTML rules** (each has a mechanism, do not "simplify" them away):
  - Table layout, `role="presentation"`, 600px card. Outlook's Word engine ignores `width` on `<div>`.
  - Buttons are padded `<td>`s, never padded `<a>`s — Outlook Windows does not support `display`, so an `<a>` stays an inline box and vertical padding cannot expand the line.
  - Link colours inline on every `<a>`; `<head><style>` is enhancement only (GMX, WEB.DE, SFR, LaPoste all strip it).
  - Inside `<style>`: never `a:link`, never `url()`, stay under 16KB.
  - No `border-radius` — a no-op at zero in the Word engine, so it is pure bytes.
  - No web fonts in email. Headings `Georgia, 'Times New Roman', serif`; quote every multi-word font name.
  - Uppercase via `text-transform`, never in the emitted string (screen readers spell out all-caps).
  - `letter-spacing` in `px`, never `em`.
  - All output entity-encoded to ASCII — Gmail clips on non-ASCII regardless of size.
- **Keycloak theme rules:**
  - Dark tokens go on the bare `.pf-v5-theme-dark` class. **Never `:root` and never `@media (prefers-color-scheme)`** — PatternFly wraps its dark tokens in `:where()`, which has zero specificity, so `:root` wins in both modes and silently disables dark mode.
  - `styles=` in a child theme *replaces* the parent's list; reference the parent file by path.
  - Do **not** declare `import=common/keycloak` in the child — it is inherited, and redeclaring shifts property-merge order.
  - Login theme `parent=keycloak.v2`. Email theme `parent=base`.
  - Never reference theme resources for email images — those URLs embed the migration tag and 404 after an upgrade.
- **Keycloak version floor:** chart pins `quay.io/keycloak/keycloak:26.2`. The `ltr` FreeMarker variable only exists from 26.2, so guard it as `${(ltr!true)?then('ltr','rtl')}` for customer installs on older tags.
- **Verification commands** run from repo root. Tests: `python -m pytest <path> -v`. Local pytest is noisy on Python 3.14; CI (3.12) is the gate — judge by the named test's result, not the suite summary.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `orchestrator/services/brand.py` | Travertine palette as Python constants. Sole colour source for email *and* the magic-link landing pages. No rendering logic. |
| `orchestrator/services/email_layout.py` | `render_email()` + `Action`. Email-safe HTML assembly only; imports colours from `brand.py`. |
| `tests/test_brand_palette.py` | Drift test vs SCSS + unmanaged-hex guard. |
| `tests/test_email_layout.py` | Escaping, entity encoding, button variants, structure. |
| `helm/keycloak-theme/srw/login/theme.properties` | Login theme declaration. |
| `helm/keycloak-theme/srw/login/resources/css/srw.20260816.css` | PF5 token overrides. Versioned filename defeats the 30-day browser cache. |
| `helm/keycloak-theme/srw/login/resources/img/srw-logo.svg` | Brand mark for the login card. |
| `helm/keycloak-theme/srw/email/theme.properties` | Email theme declaration + brand parameters. |
| `helm/keycloak-theme/srw/email/html/template.ftl` | The wrapper macro every Keycloak email imports. |
| `helm/templates/services/keycloak-theme-configmap.yaml` | Renders the theme directory into a ConfigMap. |
| `tests/test_keycloak_theme_infra.py` | Theme CSS invariants, ConfigMap completeness, checksum annotation, SMTP port. |

**Modified:**

| Path | Change |
|---|---|
| `orchestrator/services/email.py` | `send_system_notification` and `send_agent_message` call `render_email()`; raw interpolation removed. |
| `orchestrator/services/headless_notifications.py` | `_build_permission_email_bodies` calls `render_email()` with Approve/Deny actions. |
| `orchestrator/main.py` | `_magic_link_confirmation_page` (~L43919) and `_magic_link_result_page` (~L44017) re-skinned from `brand.py`. |
| `helm/templates/services/keycloak.yaml` | Theme volume + mount, checksum annotation, `loginTheme`/`emailTheme`/`displayNameHtml`, SMTP port. |
| `docker/keycloak/realm-export.json` | `loginTheme`, `emailTheme`, `displayNameHtml`. |
| `docker-compose.yaml` | Bind-mount the theme directory. |

**Deliberately not touched:** `orchestrator/mcp/templates/consent.html` (11 Catppuccin hexes) — separate surface, not on the email journey. `deployment/legacy/18-keycloak.yaml` — legacy, superseded by the chart.

---

## Slice 1 — Brand module + email (no chart changes)

### Task 1: Brand palette module with a fail-closed drift guard

**Files:**
- Create: `orchestrator/services/brand.py`
- Test: `tests/test_brand_palette.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TRAVERTINE: dict[str, str]` mapping token name → lowercase 6-digit hex (`#rrggbb`). Keys: `app-bg`, `panel-bg`, `surface-0`, `border-color`, `text-primary`, `text-secondary`, `text-muted`, `accent-color`, `success`, `danger`, `on-accent`. Also `SCSS_TOKEN_SOURCE: str` (repo-relative path to the SCSS file) and `normalize_hex(value: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brand_palette.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brand_palette.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.brand'`

- [ ] **Step 3: Write minimal implementation**

Create `orchestrator/services/brand.py`:

```python
"""Imperial (Travertine) brand colours for server-rendered surfaces.

Why literal hexes instead of reading the design tokens:

1. Email cannot use CSS custom properties. They sit at ~45% client support,
   and Gmail supports var() but not the variable *declaration* -- so colours
   must be resolved to literals at render time regardless.
2. docker/Dockerfile.orchestrator copies only orchestrator/, src/ and config/.
   The runtime has no access to cockpit SCSS even in principle.

tests/test_brand_palette.py parses the SCSS and fails closed if these drift.
Mirror of $travertine-theme in cockpit/src/styles/themes/_theme-config.scss.
"""

SCSS_TOKEN_SOURCE = "cockpit/src/styles/themes/_theme-config.scss"


def normalize_hex(value: str) -> str:
    """Lowercase and expand #abc shorthand to #aabbcc for stable comparison."""
    v = value.strip().lower()
    if len(v) == 4:  # '#abc'
        return "#" + "".join(c * 2 for c in v[1:])
    return v


TRAVERTINE: dict[str, str] = {
    "app-bg": "#f3ece0",          # travertine cream -- page ground
    "panel-bg": "#fbf6ec",        # card surface
    "surface-0": "#ede4d2",       # code / args block
    "border-color": "#dccfb6",
    "text-primary": "#2a1d12",    # deep umber
    "text-secondary": "#5a4632",
    "text-muted": "#8a7b66",
    "accent-color": "#9c2832",    # porphyry -- links, primary action
    "success": "#446b3e",         # laurel -- approve
    "danger": "#9c2832",          # blood -- deny
    "on-accent": "#ffffff",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brand_palette.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/brand.py tests/test_brand_palette.py
git commit -m "feat(brand): Travertine palette module with fail-closed drift guard

Email cannot use CSS variables (Gmail supports var() but not declaration) and
the orchestrator image ships no cockpit source, so the hexes must live in
Python. The drift test parses \$travertine-theme -- scoped to that block, since
\$senate-theme defines the same keys -- and prints corrected constants on
failure."
```

---

### Task 2: `render_email()` layout function

**Files:**
- Create: `orchestrator/services/email_layout.py`
- Test: `tests/test_email_layout.py`

**Interfaces:**
- Consumes: `brand.TRAVERTINE`.
- Produces:
  - `Action(label: str, url: str, variant: str = "primary")` — frozen dataclass. `variant` ∈ `{"primary", "approve", "deny"}`.
  - `render_email(*, title: str, body_html: str, subtitle: str | None = None, actions: Sequence[Action] = (), footer_note: str | None = None) -> str`
  - `escape_text(value: str) -> str` — thin `html.escape` wrapper used by callers for body fragments.

**Design note for the implementer:** `title`, `subtitle`, `footer_note` and every `Action.label` are **plain text and escaped internally**. `body_html` is the *only* trusted parameter. This asymmetry is the fix for a live injection — do not "helpfully" escape `body_html` or stop escaping the others.

`variant="approve"` is a solid laurel fill; `variant="deny"` is a **ghost** button (card-coloured fill, danger border and text). They must not be two solid fills: `#446b3e` and `#9c2832` measure 1.24:1 against each other, which fails WCAG 1.4.1 on the most common colour-vision-deficiency axis, for the highest-stakes action in the product.

- [ ] **Step 1: Write the failing test**

Create `tests/test_email_layout.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_layout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.email_layout'`

- [ ] **Step 3: Write minimal implementation**

Create `orchestrator/services/email_layout.py`:

```python
"""Shared layout for SRW transactional email.

Email is a restricted rendering target, not a small browser. Every rule below
has a specific mechanism behind it; see
docs/features/email_and_login_theme_alignment.md before relaxing any of them.

  * Tables, not divs -- Outlook renders via the Word engine, which does not
    support `width` on <div> at all. Microsoft supports classic Outlook until
    at least 2029, so this is not a legacy concern.
  * Buttons are padded <td>s -- the Word engine has no `display` support, so an
    <a> is permanently an inline box whose vertical padding cannot expand the
    line. Square corners mean no VML is needed.
  * Colours inline -- a growing set of clients (GMX, WEB.DE, SFR, LaPoste)
    strip <head><style> entirely. The style block is enhancement only.
  * ASCII-only output -- Gmail clips messages containing non-ASCII characters
    regardless of size.
  * Uppercase via text-transform -- screen readers read the source text and
    pronounce all-caps tokens as initialisms.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Sequence

from orchestrator.services.brand import TRAVERTINE as C

# Georgia is aliased to `serif` on Android (AOSP fonts.xml aliases both Georgia
# and Times New Roman), so Gmail Android renders Noto Serif. Do not let layout
# depend on Georgia's metrics.
_SERIF = "Georgia, 'Times New Roman', serif"
_SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"


@dataclass(frozen=True)
class Action:
    """A call-to-action button.

    variant:
      "primary" -- solid porphyry, the default CTA
      "approve" -- solid laurel
      "deny"    -- GHOST (card fill, danger border/text)

    Approve and Deny must not both be solid: #446b3e and #9c2832 measure
    1.24:1 against each other, failing WCAG 1.4.1 on the red/green axis for
    the highest-stakes action in the product. They differ by form.
    """

    label: str
    url: str
    variant: str = "primary"


def escape_text(value: str) -> str:
    """Escape caller-supplied text destined for `body_html`."""
    return html.escape(value, quote=True)


def _to_ascii(markup: str) -> str:
    return markup.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _button_cell(action: Action) -> str:
    label = html.escape(action.label, quote=True)
    url = html.escape(action.url, quote=True)
    if action.variant == "approve":
        bg, fg, border = C["success"], C["on-accent"], C["success"]
    elif action.variant == "deny":
        bg, fg, border = C["panel-bg"], C["danger"], C["danger"]
    else:
        bg, fg, border = C["accent-color"], C["on-accent"], C["accent-color"]
    return (
        f'<td align="center" bgcolor="{bg}" style="background-color:{bg};'
        f'border:2px solid {border};padding:12px 28px;'
        f"font-family:{_SANS};font-size:15px;font-weight:600;"
        f'mso-line-height-rule:exactly;line-height:20px;">'
        f'<a href="{url}" style="color:{fg};text-decoration:none;'
        f'font-family:{_SANS};font-size:15px;font-weight:600;'
        f'letter-spacing:1px;text-transform:uppercase;">{label}</a></td>'
    )


def render_email(
    *,
    title: str,
    body_html: str,
    subtitle: str | None = None,
    actions: Sequence[Action] = (),
    footer_note: str | None = None,
) -> str:
    """Render one branded email.

    `title`, `subtitle`, `footer_note` and every Action.label are PLAIN TEXT
    and are escaped here. `body_html` is the ONLY trusted parameter -- callers
    escape their own interpolations (use `escape_text`). This asymmetry is
    deliberate: it is what closes the injection that existed when each call
    site hand-rolled its own HTML.
    """
    safe_title = html.escape(title, quote=True)
    safe_subtitle = html.escape(subtitle, quote=True) if subtitle else ""
    safe_footer = html.escape(footer_note, quote=True) if footer_note else ""

    subtitle_row = (
        f'<p style="margin:6px 0 0 0;font-family:{_SANS};font-size:13px;'
        f'line-height:18px;color:{C["text-secondary"]};">{safe_subtitle}</p>'
        if safe_subtitle
        else ""
    )

    if actions:
        # Spacer cells are interleaved between buttons; a margin on the <td>
        # would not survive the Word engine.
        button_cells = ""
        for i, action in enumerate(actions):
            if i:
                button_cells += (
                    '<td width="12" style="width:12px;font-size:0;'
                    'line-height:0;">&nbsp;</td>'
                )
            button_cells += _button_cell(action)
        actions_row = (
            f'<tr><td align="center" style="padding:8px 32px 24px 32px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0"><tr>{button_cells}</tr></table></td></tr>'
        )
    else:
        actions_row = ""

    footer_row = (
        f'<tr><td style="padding:16px 32px 24px 32px;'
        f'border-top:1px solid {C["border-color"]};font-family:{_SANS};'
        f'font-size:12px;line-height:18px;color:{C["text-muted"]};">'
        f"{safe_footer}</td></tr>"
        if safe_footer
        else ""
    )

    markup = f"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{safe_title}</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings>
<o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
<!--[if mso]><style>td,div,p,a,h1 {{ font-family: Arial, Helvetica, sans-serif; }}</style><![endif]-->
<style>
:root {{ color-scheme: light; supported-color-schemes: light; }}
body {{ margin:0 !important; padding:0 !important; width:100% !important; }}
table {{ border-collapse:collapse; }}
img {{ border:0; outline:none; -ms-interpolation-mode:bicubic; }}
a {{ color:{C["accent-color"]}; }}
</style>
</head>
<body bgcolor="{C["app-bg"]}" style="margin:0;padding:0;background-color:{C["app-bg"]};">
<div role="article" aria-roledescription="email" lang="en" dir="ltr">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{C["app-bg"]}" style="background-color:{C["app-bg"]};">
<tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="{C["panel-bg"]}" style="width:600px;max-width:600px;background-color:{C["panel-bg"]};border:1px solid {C["border-color"]};">
<tr><td bgcolor="{C["accent-color"]}" style="background-color:{C["accent-color"]};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:24px 32px 16px 32px;">
<h1 style="margin:0;font-family:{_SERIF};font-size:20px;line-height:26px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{C["text-primary"]};">{safe_title}</h1>
{subtitle_row}
</td></tr>
<tr><td style="padding:0 32px 20px 32px;font-family:{_SANS};font-size:15px;line-height:24px;color:{C["text-primary"]};">
{body_html}
</td></tr>
{actions_row}
{footer_row}
</table>
</td></tr>
</table>
</div>
</body>
</html>"""
    return _to_ascii(markup)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_email_layout.py tests/test_brand_palette.py -v`
Expected: all pass. If `test_uses_no_unmanaged_colours` fails, a hex was hardcoded in the template — move it into `brand.TRAVERTINE` or reuse an existing token.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/email_layout.py tests/test_email_layout.py
git commit -m "feat(email): shared email layout with escaping and a11y-safe actions

One render_email() replaces three hand-rolled inline-HTML call sites. Text
params are escaped internally and body_html is the only trusted input, which
closes the injection each call site carried. Approve stays solid and Deny
becomes a ghost button: the two fills measured 1.24:1 against each other,
failing WCAG 1.4.1 on the red/green axis."
```

---

### Task 3: Migrate `email.py` call sites (closes the injection)

**Files:**
- Modify: `orchestrator/services/email.py:163-220` (`send_system_notification`), `:222-340` (`send_agent_message`)
- Test: `tests/test_email_service_branding.py` (create)

**Interfaces:**
- Consumes: `render_email`, `Action`, `escape_text` from Task 2.
- Produces: no signature changes. Both methods keep their existing parameters and return types.

**Current defect being fixed:** `email.py:212` emits `f"<p>Hello {to_name},</p>"` and `:307` interpolates `{job_description[:80]}` and `{config_name}` straight into HTML. `job_description` is user-supplied at job creation.

- [ ] **Step 1: Write the failing test**

Create `tests/test_email_service_branding.py`:

```python
"""The two EmailService bodies render through the shared layout, escaped."""

from orchestrator.services import brand
from orchestrator.services.email import EmailService


def test_system_notification_escapes_recipient_name() -> None:
    svc = EmailService()
    html = svc._build_system_notification_html(
        to_name="<script>alert(1)</script>",
        body_md="hello",
        cockpit_link="https://cockpit.test/",
    )
    assert "<script>" not in html
    assert brand.TRAVERTINE["panel-bg"] in html
    assert "#1e1e2e" not in html  # Catppuccin is gone


def test_agent_message_escapes_job_description_and_config_name() -> None:
    svc = EmailService()
    html = svc._build_agent_message_html(
        message_md="body text",
        job_description='<img src=x onerror=alert(1)>',
        config_name="<b>agent</b>",
        phase_str="phase 1",
        cockpit_link="https://cockpit.test/",
        reply_to_addr=None,
    )
    assert "<img src=x" not in html
    assert "<b>agent</b>" not in html
    assert "#1e1e2e" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_service_branding.py -v`
Expected: FAIL — `AttributeError: 'EmailService' object has no attribute '_build_system_notification_html'`

- [ ] **Step 3: Write minimal implementation**

Add two helpers to `EmailService` (extracting the body-building makes it testable without SMTP) and call them from the existing methods.

In `orchestrator/services/email.py`, add near the top:

```python
from orchestrator.services.email_layout import Action, escape_text, render_email
```

Add these methods to `EmailService`:

```python
    def _build_system_notification_html(
        self, *, to_name: str, body_md: str, cockpit_link: str
    ) -> str:
        body = escape_text(body_md).replace("\n", "<br>")
        greeting = escape_text(to_name)
        return render_email(
            title="Superhuman Remote Worker",
            body_html=f"<p style='margin:0 0 12px 0;'>Hello {greeting},</p>"
                      f"<p style='margin:0;'>{body}</p>",
            actions=[Action(label="Open Cockpit", url=cockpit_link)],
        )

    def _build_agent_message_html(
        self,
        *,
        message_md: str,
        job_description: str,
        config_name: str,
        phase_str: str,
        cockpit_link: str,
        reply_to_addr: str | None,
    ) -> str:
        body = escape_text(message_md).replace("\n", "<br>")
        subtitle = f"Job: {job_description[:80]} • Agent: {config_name} • {phase_str}"
        return render_email(
            title="SRW Agent Message",
            subtitle=subtitle,          # escaped inside render_email
            body_html=f"<p style='margin:0;'>{body}</p>",
            actions=[Action(label="Reply in Cockpit", url=cockpit_link)],
            footer_note="or reply directly to this email" if reply_to_addr else None,
        )
```

Then replace the body-building blocks. In `send_system_notification`, delete lines 200-213 (`body_html_msg = (...)` through the closing `)` of `body_html = (...)`) and substitute:

```python
        body_html = self._build_system_notification_html(
            to_name=to_name, body_md=body_md, cockpit_link=cockpit_link
        )
```

In `send_agent_message`, delete lines 294-326 (`message_html = (...)` through the end of the `body_html = f"""..."""` literal) and substitute:

```python
        body_html = self._build_agent_message_html(
            message_md=message_md,
            job_description=job_description,
            config_name=config_name,
            phase_str=phase_str,
            cockpit_link=cockpit_link,
            reply_to_addr=reply_to_addr,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_email_service_branding.py tests/test_email_service.py -v`
Expected: new tests pass; `tests/test_email_service.py` still passes (it passes `body_html` as a fixture and does not inspect it).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/email.py tests/test_email_service_branding.py
git commit -m "fix(email): route EmailService bodies through the shared layout

Closes an HTML injection: to_name, job_description and config_name were
interpolated raw into outbound HTML, and job_description is user-supplied at
job creation. Body building moves into two helpers so it is testable without
SMTP, and both now render on Travertine instead of Catppuccin."
```

---

### Task 4: Migrate the permission email (ghost Deny button)

**Files:**
- Modify: `orchestrator/services/headless_notifications.py:293-350` (`_build_permission_email_bodies`)
- Test: `tests/test_headless_notifications_phase4.py` (extend)

**Interfaces:**
- Consumes: `render_email`, `Action`, `escape_text`.
- Produces: `_build_permission_email_bodies` keeps its exact signature and `tuple[str, str]` return.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_headless_notifications_phase4.py`:

```python
def test_permission_email_uses_brand_palette_and_ghost_deny() -> None:
    import re
    from orchestrator.services import brand
    from orchestrator.services import headless_notifications as hn

    _text, html = hn._build_permission_email_bodies(
        tool_name="run_command",
        tool_args_preview='{"cmd": "ls"}',
        approve_url="http://x/magic/approve/T1",
        deny_url="http://x/magic/deny/T1",
        cockpit_link="http://x/cockpit",
        request_age_minutes=3,
    )
    assert "#1e1e2e" not in html and "#a6e3a1" not in html  # Catppuccin gone
    assert brand.TRAVERTINE["panel-bg"] in html

    deny_cell = re.search(r'<td[^>]*>\s*<a[^>]*>Deny</a>', html, re.S).group(0)
    assert f"background-color:{brand.TRAVERTINE['panel-bg']}" in deny_cell
    assert f"border:2px solid {brand.TRAVERTINE['danger']}" in deny_cell
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_headless_notifications_phase4.py::test_permission_email_uses_brand_palette_and_ghost_deny -v`
Expected: FAIL — `assert '#1e1e2e' not in html`

- [ ] **Step 3: Write minimal implementation**

In `orchestrator/services/headless_notifications.py`, add the import:

```python
from orchestrator.services.email_layout import Action, escape_text, render_email
```

Replace lines 316-350 (from `safe_args = (` through `return text_body, html_body`) with:

```python
    safe_args = escape_text(tool_args_preview)
    safe_tool = escape_text(tool_name)

    body_html = (
        f"<p style='margin:0 0 12px 0;'>The agent wants to call "
        f"<code style=\"background:{_C['surface-0']};padding:2px 6px;"
        f"font-family:monospace;\">{safe_tool}</code>:</p>"
        f"<pre style=\"background:{_C['surface-0']};padding:12px;"
        f"margin:0;overflow-x:auto;font-size:12px;line-height:18px;"
        f"font-family:monospace;color:{_C['text-primary']};\">{safe_args}</pre>"
    )

    html_body = render_email(
        title="Permission requested",
        subtitle=f"Waiting {request_age_minutes} min for your decision.",
        body_html=body_html,
        actions=[
            Action(label="Approve", url=approve_url, variant="approve"),
            Action(label="Deny", url=deny_url, variant="deny"),
        ],
        footer_note="Open the cockpit for full context.",
    )

    return text_body, html_body
```

Add near the imports:

```python
from orchestrator.services.brand import TRAVERTINE as _C
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_headless_notifications_phase4.py -v`
Expected: the new test passes and the pre-existing assertions at lines 495-501 (`"run_command" in html`, both magic URLs, HTML escaping) still pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/headless_notifications.py tests/test_headless_notifications_phase4.py
git commit -m "feat(email): rebrand the permission mail, ghost Deny button

Approve and Deny were two solid fills measuring 1.24:1 against each other --
worse than the Catppuccin pair they replace (1.56:1) and a WCAG 1.4.1 failure
on the red/green axis. Deny becomes a ghost button so the two differ by form
at 5.70:1."
```

---

### Task 5: Re-skin the magic-link landing pages

**Files:**
- Modify: `orchestrator/main.py` — `_magic_link_confirmation_page` (~L43919), `_magic_link_result_page` (~L44017)
- Test: `tests/test_magic_link_pages_branding.py` (create)

**Interfaces:**
- Consumes: `brand.TRAVERTINE`.
- Produces: both functions keep their signatures and return `str`.

**Why this is in slice 1:** these are where the permission email's Approve/Deny buttons land. Between them they carry 32 Catppuccin hexes. Shipping only the email produces a branded mail that opens an unbranded page.

**Note on line numbers:** `orchestrator/main.py` moves constantly. Locate the functions by name (`grep -n "def _magic_link_confirmation_page" orchestrator/main.py`), not by the line numbers above.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magic_link_pages_branding.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_magic_link_pages_branding.py -v`
Expected: FAIL — `Catppuccin remains: {...}`

Signatures verified against the current source: `_magic_link_confirmation_page(*, tool_name, tool_args_preview, intended_decision, token, extend_status=None, extends_remaining=None)` and `_magic_link_result_page(*, title, body, cockpit_url, is_error=False)`.

- [ ] **Step 3: Write minimal implementation**

In `orchestrator/main.py`, add near the other service imports:

```python
from orchestrator.services.brand import TRAVERTINE as _BRAND
```

In both functions, replace every Catppuccin literal with the corresponding token. Mapping:

| Catppuccin | Replace with |
|---|---|
| `#1e1e2e` (page bg) | `_BRAND["app-bg"]` |
| `#181825` (panel//code bg) | `_BRAND["surface-0"]` |
| `#313244` (border) | `_BRAND["border-color"]` |
| `#cdd6f4` (body text) | `_BRAND["text-primary"]` |
| `#a6adc8` (secondary text) | `_BRAND["text-secondary"]` |
| `#6c7086` (muted text) | `_BRAND["text-muted"]` |
| `#cba6f7` (heading/primary) | `_BRAND["accent-color"]` |
| `#a6e3a1` (approve/success) | `_BRAND["success"]` |
| `#f38ba8` (deny/danger) | `_BRAND["danger"]` |
| `#89b4fa` (info/link) | `_BRAND["accent-color"]` |
| `#f9e2af` (warning) | `_BRAND["text-secondary"]` |

Also remove `border-radius` declarations entirely, and flip dark-on-light assumptions (button text `#1e1e2e` on a filled button becomes `_BRAND["on-accent"]`).

One specific line in `_magic_link_result_page` needs care — it picks its accent from the error flag:

```python
accent = "#f38ba8" if is_error else "#a6e3a1"
```

becomes:

```python
accent = _BRAND["danger"] if is_error else _BRAND["success"]
```

Both pages also set `background: #1e1e2e` on `<body>` with light text; the whole polarity inverts to a cream ground with `text-primary` copy.

These are browser-rendered pages, not email — CSS variables and modern layout are fine here. Only the palette must match.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_magic_link_pages_branding.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_magic_link_pages_branding.py
git commit -m "feat(ui): rebrand magic-link landing pages onto Travertine

These are where the permission email's Approve/Deny land. Restyling only the
email would have produced a branded mail opening a Catppuccin page."
```

---

### Task 6: Dev-only email preview route

**Files:**
- Modify: `orchestrator/main.py` (add route near other debug/dev routes)
- Test: `tests/test_email_preview_route.py` (create)

**Interfaces:**
- Consumes: `render_email`, and the two `EmailService` body helpers from Task 3, plus `_build_permission_email_bodies`.
- Produces: `GET /debug/emails` → HTML index; `GET /debug/emails/{name}` → one rendered email. Names: `system`, `agent`, `permission`.

**Why:** borrowed from Zulip. Renders every email for browser inspection without sending mail. Cheaper than render-to-disk and it cannot rot, because it calls the real builders.

- [ ] **Step 1: Write the failing test**

Create `tests/test_email_preview_route.py`:

```python
"""The dev preview route renders every email through its real builder."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from orchestrator.main import app

    return TestClient(app)


@pytest.mark.parametrize("name", ["system", "agent", "permission"])
def test_preview_renders_each_email(client, name) -> None:
    resp = client.get(f"/debug/emails/{name}")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in resp.text
    assert "#1e1e2e" not in resp.text


def test_preview_index_lists_all(client) -> None:
    resp = client.get("/debug/emails")
    assert resp.status_code == 200
    for name in ("system", "agent", "permission"):
        assert name in resp.text


def test_unknown_preview_is_404(client) -> None:
    assert client.get("/debug/emails/nope").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_preview_route.py -v`
Expected: FAIL — 404 on `/debug/emails/system`

- [ ] **Step 3: Write minimal implementation**

Add to `orchestrator/main.py` (place beside the existing debug routes; if they are gated behind a debug flag, follow that same gate):

```python
@app.get("/debug/emails", response_class=HTMLResponse)
async def debug_email_index() -> str:
    """Dev-only index of rendered transactional emails (Zulip's /emails idea)."""
    links = "".join(
        f'<li><a href="/debug/emails/{n}">{n}</a></li>'
        for n in ("system", "agent", "permission")
    )
    return f"<!DOCTYPE html><html><body><h1>Email previews</h1><ul>{links}</ul></body></html>"


@app.get("/debug/emails/{name}", response_class=HTMLResponse)
async def debug_email_preview(name: str) -> str:
    from orchestrator.services import headless_notifications as hn
    from orchestrator.services.email import email_service

    link = "https://cockpit.example/preview"
    if name == "system":
        return email_service._build_system_notification_html(
            to_name="Ada Lovelace",
            body_md="Your automation was disabled after 3 consecutive failures.",
            cockpit_link=link,
        )
    if name == "agent":
        return email_service._build_agent_message_html(
            message_md="Finished the migration.\nTwo files changed.",
            job_description="Migrate the billing schema to the new ledger format",
            config_name="developer",
            phase_str="phase 2",
            cockpit_link=link,
            reply_to_addr="reply@example.com",
        )
    if name == "permission":
        _text, html = hn._build_permission_email_bodies(
            tool_name="run_command",
            tool_args_preview='{"command": "rm -rf ./build"}',
            approve_url=f"{link}/approve",
            deny_url=f"{link}/deny",
            cockpit_link=link,
            request_age_minutes=4,
        )
        return html
    raise HTTPException(status_code=404, detail="unknown email preview")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_email_preview_route.py -v`
Expected: 5 passed

- [ ] **Step 5: Look at the actual emails**

Start the orchestrator locally and open `http://localhost:8000/debug/emails`. Check each rendering in a browser. **Then check `permission` in a real client**, because the one render research could not model is Yahoo desktop dark mode against `#f3ece0`/`#fbf6ec` — predicted body text at 2.08–2.54:1. If it fails there, darken `text-primary` for email only and record it in the spec.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py tests/test_email_preview_route.py
git commit -m "feat(dev): /debug/emails preview route

Renders all three emails through their real builders, so it cannot drift from
what actually ships."
```

---

## Slice 2 — Keycloak login theme

### Task 7: Theme files

**Files:**
- Create: `helm/keycloak-theme/srw/login/theme.properties`, `helm/keycloak-theme/srw/login/resources/css/srw.20260816.css`, `helm/keycloak-theme/srw/login/resources/img/srw-logo.svg`
- Test: `tests/test_keycloak_theme_infra.py` (create)

**Interfaces:**
- Consumes: `brand.TRAVERTINE` and `brand.normalize_hex` from Task 1 (the palette-consistency test imports them; the CSS itself has no runtime dependency).
- Produces: a theme directory consumed by Task 8's ConfigMap.

- [ ] **Step 1: Write the failing test**

Create `tests/test_keycloak_theme_infra.py`:

```python
"""Invariants for the Keycloak `srw` theme.

The dark-mode assertion is the important one: PatternFly v5 wraps every dark
token in :where(), which has zero specificity, so putting dark values under
:root or a media query loses to nothing and silently disables dark mode.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "helm/keycloak-theme/srw"
LOGIN_CSS = next((THEME / "login/resources/css").glob("srw.*.css"), None)


def test_login_theme_properties_are_correct() -> None:
    props = (THEME / "login/theme.properties").read_text()
    assert "parent=keycloak.v2" in props
    # v1 is deprecated as of KC 26.0.
    assert "parent=keycloak\n" not in props
    # The parent file must be re-listed: `styles` REPLACES, it does not append.
    assert "styles=css/styles.css css/" in props
    # Inherited from keycloak.v2; redeclaring shifts property-merge order.
    assert "import=common/keycloak" not in props


def test_css_filename_is_versioned() -> None:
    """Theme resources are served with max-age=2592000 and the /resources/<tag>/
    segment only moves on a KC version migration, so the filename is the only
    cache-busting lever."""
    assert LOGIN_CSS is not None
    assert LOGIN_CSS.name != "srw.css"


def test_dark_tokens_are_on_the_bare_class_not_root_or_media_query() -> None:
    css = LOGIN_CSS.read_text()
    assert ".pf-v5-theme-dark" in css
    assert "prefers-color-scheme" not in css, (
        "PatternFly toggles a class, not a media query -- a media query desyncs."
    )
    dark_start = css.index(".pf-v5-theme-dark")
    root_start = css.index(":root")
    assert root_start < dark_start, ":root must come first so the class wins on tie"


def test_overrides_the_brandable_keycloak_variables() -> None:
    css = LOGIN_CSS.read_text()
    for token in (
        "--pf-v5-global--primary-color--100",
        "--pf-v5-global--BackgroundColor--100",
        "--pf-v5-global--Color--100",
        "--pf-v5-global--BorderRadius--sm",
        "--keycloak-card-top-color",
        "--keycloak-logo-url",
    ):
        assert token in css, f"missing override: {token}"


def test_header_wrapper_colour_is_forced() -> None:
    """keycloak.v2 sets #kc-header-wrapper colour with !important, relying on a
    dark background image we remove -- without countering it the header goes
    white-on-cream."""
    css = LOGIN_CSS.read_text()
    assert "#kc-header-wrapper" in css
    assert "!important" in css


def test_no_external_font_dependency() -> None:
    """The login page must work when everything else is down, and must not leak
    every login attempt's IP to a third party."""
    css = LOGIN_CSS.read_text()
    assert "fonts.googleapis.com" not in css
    assert "@import" not in css


def test_light_tokens_match_the_shared_brand_palette() -> None:
    """Extends the Python drift guard across the third copy of the palette.

    brand.py is checked against the SCSS by tests/test_brand_palette.py; this
    ties the Keycloak CSS to brand.py, so all three move together or a test
    fails. Without it the login page is exactly the surface that silently rots.
    """
    import re

    from orchestrator.services import brand

    css = LOGIN_CSS.read_text()
    root = css[css.index(":root {"): css.index(".pf-v5-theme-dark")]

    expected = {
        "--pf-v5-global--primary-color--100": brand.TRAVERTINE["accent-color"],
        "--pf-v5-global--BackgroundColor--100": brand.TRAVERTINE["panel-bg"],
        "--pf-v5-global--BackgroundColor--200": brand.TRAVERTINE["app-bg"],
        "--pf-v5-global--Color--100": brand.TRAVERTINE["text-primary"],
        "--pf-v5-global--Color--200": brand.TRAVERTINE["text-secondary"],
        "--pf-v5-global--BorderColor--100": brand.TRAVERTINE["border-color"],
        "--keycloak-card-top-color": brand.TRAVERTINE["accent-color"],
    }
    for token, want in expected.items():
        found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{3,8}})", root)
        assert found, f"{token} missing from the :root block"
        assert brand.normalize_hex(found.group(1)) == want, (
            f"{token} is {found.group(1)}, brand.py says {want}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -v`
Expected: FAIL — `FileNotFoundError` / `LOGIN_CSS is None`

- [ ] **Step 3: Write the theme files**

`helm/keycloak-theme/srw/login/theme.properties`:

```properties
# SRW login theme -- CSS only, zero FreeMarker.
#
# parent=keycloak.v2 : v1 (`keycloak`) is deprecated as of KC 26.0.
# styles             : this key REPLACES the parent's list, so keycloak.v2's own
#                      css/styles.css must be re-listed. It resolves up the
#                      parent chain; srw.*.css resolves from this theme and
#                      loads last, so it wins the cascade.
# NO import=          : common/keycloak is inherited. Redeclaring it inserts the
#                      import twice and shifts property-merge order.
# NO darkMode=        : inherited as true from keycloak.v2.
parent=keycloak.v2
styles=css/styles.css css/srw.20260816.css
```

`helm/keycloak-theme/srw/login/resources/css/srw.20260816.css`:

```css
/*
 * SRW Imperial theme for the Keycloak login pages.
 *
 * SPECIFICITY WARNING -- read before editing:
 * PatternFly v5 wraps every dark-mode token in :where(.pf-v5-theme-dark),
 * and :where() has ZERO specificity. A :root block (0,1,0) therefore beats it
 * unconditionally, in both colour schemes. Dark values MUST go on the bare
 * .pf-v5-theme-dark class below, which ties :root on specificity and wins by
 * source order. Do NOT move them into @media (prefers-color-scheme: dark) --
 * Keycloak toggles the class from JS on <html>, and a media query desyncs
 * from it.
 *
 * Filename is versioned: theme resources are served with max-age=2592000 and
 * the /resources/<tag>/ path segment only changes on a KC version migration.
 * Bump the datestamp (and theme.properties) whenever this file changes.
 */

/* --- Travertine (light) ------------------------------------------------- */
:root {
  --pf-v5-global--primary-color--100: #9c2832;
  --pf-v5-global--primary-color--200: #7d1e26;
  --pf-v5-global--BackgroundColor--100: #fbf6ec;
  --pf-v5-global--BackgroundColor--200: #f3ece0;
  --pf-v5-global--Color--100: #2a1d12;
  --pf-v5-global--Color--200: #5a4632;
  --pf-v5-global--BorderColor--100: #dccfb6;
  --pf-v5-global--link--Color: #9c2832;
  --pf-v5-global--link--Color--hover: #7d1e26;

  /* Roman shape pass: no rounded corners anywhere. */
  --pf-v5-global--BorderRadius--sm: 0;
  --pf-v5-global--BorderRadius--lg: 0;

  --pf-v5-global--FontFamily--text: 'Inter', -apple-system, BlinkMacSystemFont,
    'Segoe UI', Arial, sans-serif;
  --pf-v5-global--FontFamily--heading: 'Cinzel', Georgia, 'Times New Roman', serif;

  /* keycloak.v2 brand hooks. The 4px card stripe ships PatternFly blue. */
  --keycloak-card-top-color: #9c2832;
  --keycloak-logo-url: url('../img/srw-logo.svg');
  --keycloak-logo-width: 260px;
  --keycloak-logo-height: 56px;
  --keycloak-bg-logo-url: none;
}

/* --- Senate (dark) ------------------------------------------------------ */
.pf-v5-theme-dark {
  --pf-v5-global--primary-color--100: #cc4647;
  --pf-v5-global--primary-color--200: #d65a5b;
  --pf-v5-global--BackgroundColor--100: #1c1c22;
  --pf-v5-global--BackgroundColor--200: #141418;
  --pf-v5-global--Color--100: #f4f2ee;
  --pf-v5-global--Color--200: #8c8a87;
  --pf-v5-global--BorderColor--100: #33333d;
  --pf-v5-global--link--Color: #cc4647;
  --pf-v5-global--link--Color--hover: #d65a5b;
  --keycloak-card-top-color: #cc4647;
}

/* Page ground. keycloak.v2 paints a dark background image here; we drop it. */
.login-pf body {
  background: var(--pf-v5-global--BackgroundColor--200) !important;
}

/*
 * keycloak.v2 hard-codes the header to a light colour with !important, which
 * only works against its dark background image. We load later, so an equal
 * !important at equal specificity wins.
 */
#kc-header-wrapper {
  color: var(--pf-v5-global--Color--100) !important;
}

/* Inset Stamp: sharp, weighty, presses 1px on click. */
.pf-v5-c-button.pf-m-primary {
  border-radius: 0;
  font-family: var(--pf-v5-global--FontFamily--heading);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.pf-v5-c-button.pf-m-primary:active {
  transform: translateY(1px);
}
```

`helm/keycloak-theme/srw/login/resources/img/srw-logo.svg` — a wordmark sized to the 260×56 box declared above. Placeholder that satisfies the tests and renders legibly; replace with the real asset when available:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 260 56" width="260" height="56" role="img" aria-label="Superhuman Remote Worker">
  <text x="0" y="40" font-family="Georgia, serif" font-size="34" font-weight="700" letter-spacing="4" fill="#9c2832">SRW</text>
  <rect x="0" y="48" width="260" height="2" fill="#9c2832"/>
</svg>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add helm/keycloak-theme tests/test_keycloak_theme_infra.py
git commit -m "feat(keycloak): SRW login theme, CSS-only child of keycloak.v2

Dark tokens sit on the bare .pf-v5-theme-dark class: PatternFly wraps its own
dark tokens in :where(), which has zero specificity, so :root or a media query
would win in both modes and silently disable dark mode. Stylesheet filename is
versioned because theme resources are served with max-age=2592000 and the
resource tag only moves on a version migration."
```

---

### Task 8: ConfigMap delivery, mount and cache-busting annotation

**Files:**
- Create: `helm/templates/services/keycloak-theme-configmap.yaml`
- Modify: `helm/templates/services/keycloak.yaml` (pod template annotations ~L555, `volumeMounts` L1169, `volumes` L1198), `docker-compose.yaml`
- Test: `tests/test_keycloak_theme_infra.py` (extend)

**Interfaces:**
- Consumes: the theme directory from Task 7.
- Produces: a ConfigMap named `{{ include "srw.fullname" . }}-keycloak-theme` mounted at `/opt/keycloak/themes/srw`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_keycloak_theme_infra.py`:

```python
KC = ROOT / "helm/templates/services/keycloak.yaml"
CM = ROOT / "helm/templates/services/keycloak-theme-configmap.yaml"


def test_configmap_enumerates_every_theme_file() -> None:
    """ConfigMap keys cannot contain '/', so the nested layout only exists via
    items[].path. A file missing an items entry is a silent no-op -- and a
    theme with no login/ directory simply never appears."""
    kc = KC.read_text()
    for path in sorted(p.relative_to(THEME) for p in THEME.rglob("*") if p.is_file()):
        assert f"path: {path}" in kc, f"{path} has no items[].path entry"


def test_configmap_keys_have_no_slashes() -> None:
    cm = CM.read_text()
    import re
    for key in re.findall(r"^\s{2}([\w.\-]+):\s*\|", cm, re.M):
        assert "/" not in key


def test_theme_is_mounted_at_the_themes_root() -> None:
    kc = KC.read_text()
    assert "mountPath: /opt/keycloak/themes/srw" in kc


def test_pod_template_has_a_theme_checksum_annotation() -> None:
    """Three caches make theme edits invisible without a pod roll: cacheThemes,
    cacheTemplates, and the gzip cache -- which writes a .gz on first request
    and only regenerates if the file is ABSENT. Browsers send Accept-Encoding:
    gzip and get stale CSS while curl shows it fresh."""
    kc = KC.read_text()
    assert "checksum/keycloak-theme:" in kc
    assert "keycloak-theme-configmap.yaml" in kc


def test_compose_bind_mounts_the_same_source() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text()
    assert "./helm/keycloak-theme/srw:/opt/keycloak/themes/srw" in compose
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -v`
Expected: FAIL — `FileNotFoundError` on the ConfigMap template

- [ ] **Step 3: Write the implementation**

Create `helm/templates/services/keycloak-theme-configmap.yaml`:

```yaml
{{- if and .Values.keycloak.enabled .Values.keycloak.internal }}
{{- /*
  The SRW Keycloak theme, rendered from helm/keycloak-theme/srw/.

  ConfigMap keys cannot contain '/', so the nested theme layout is rebuilt at
  mount time via items[].path in the Deployment (path DOES accept slashes).
  Keys here are the path with '/' replaced by '_'. .AsConfig cannot be used:
  it keys by basename, and login/ and email/ both contain theme.properties.
*/}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "srw.fullname" . }}-keycloak-theme
  labels:
    {{- include "srw.labels" . | nindent 4 }}
data:
{{- range $path, $_ := .Files.Glob "keycloak-theme/srw/**" }}
{{- $key := $path | replace "keycloak-theme/srw/" "" | replace "/" "_" }}
  {{ $key }}: |
{{ $.Files.Get $path | indent 4 }}
{{- end }}
{{- end }}
```

In `helm/templates/services/keycloak.yaml`, add an annotations block to the pod template (currently `template: metadata: labels:` at ~L555):

```yaml
  template:
    metadata:
      annotations:
        # Theme edits are invisible without a pod roll: Keycloak caches the
        # resolved theme, the compiled FreeMarker templates, AND gzipped static
        # resources -- the last only regenerates when the .gz is absent, so
        # browsers get stale CSS while curl looks fine. /opt/keycloak/data is an
        # emptyDir here, so a roll clears it.
        checksum/keycloak-theme: {{ include (print $.Template.BasePath "/services/keycloak-theme-configmap.yaml") . | sha256sum | quote }}
      labels:
        {{- include "srw.componentSelectorLabels" (dict "context" . "component" "keycloak") | nindent 8 }}
```

Add to `volumeMounts` (after the `realm-import` entry at L1170):

```yaml
            - name: keycloak-theme
              mountPath: /opt/keycloak/themes/srw
              readOnly: true
```

Add to `volumes` (after the `realm-import` entry at L1199):

```yaml
        - name: keycloak-theme
          configMap:
            name: {{ include "srw.fullname" . }}-keycloak-theme
            items:
              - key: login_theme.properties
                path: login/theme.properties
              - key: login_resources_css_srw.20260816.css
                path: login/resources/css/srw.20260816.css
              - key: login_resources_img_srw-logo.svg
                path: login/resources/img/srw-logo.svg
```

(Task 11 adds the two `email_*` entries.)

In `docker-compose.yaml`, add to the keycloak service's `volumes`:

```yaml
      - ./helm/keycloak-theme/srw:/opt/keycloak/themes/srw:ro
```

- [ ] **Step 4: Run tests and render the chart**

```bash
python -m pytest tests/test_keycloak_theme_infra.py -v
helm template srw ./helm --set keycloak.enabled=true --set keycloak.internal=true \
  | grep -A3 'checksum/keycloak-theme'
```
Expected: tests pass; the checksum annotation renders with a real sha256.

- [ ] **Step 5: Validate against a real API server**

```bash
helm template srw ./helm --set keycloak.enabled=true --set keycloak.internal=true \
  | kubectl apply --dry-run=server -f - 2>&1 | tail -20
```
Expected: no `Invalid value` errors. Mocked clients validate nothing about manifest shape — this step is the one that catches an illegal ConfigMap key.

- [ ] **Step 6: Commit**

```bash
git add helm/templates/services/keycloak-theme-configmap.yaml helm/templates/services/keycloak.yaml docker-compose.yaml tests/test_keycloak_theme_infra.py
git commit -m "feat(keycloak): deliver the srw theme via ConfigMap

Keys are path-mangled and rebuilt through items[].path, because ConfigMap keys
cannot contain '/' and a flat projection yields no theme directory at all --
which Keycloak reports only as one ERROR line at first login. The checksum
annotation is mandatory: the gzip resource cache never invalidates, so without
a pod roll browsers receive stale CSS indefinitely while curl shows it fresh."
```

---

### Task 9: Point the realms at the theme

**Files:**
- Modify: `helm/templates/services/keycloak.yaml:531` (`loginTheme`) and `:29-30` (`displayNameHtml`), `docker/keycloak/realm-export.json:548` and `:4-5`
- Test: `tests/test_keycloak_theme_infra.py` (extend)

**Interfaces:**
- Consumes: the theme from Tasks 7-8.
- Produces: realms configured with `loginTheme: srw`.

**Critical detail:** `--keycloak-logo-url` alone does nothing. keycloak.v2 renders the header as `${kcSanitize(msg("loginTitleHtml",(realm.displayNameHtml!'')))}` — text, not an image. The master realm shows a logo only because it ships `displayNameHtml` as `<div class="kc-logo-text"><span>Keycloak</span></div>`, which the stylesheet converts to a background image. Our realm currently ships `<strong>Superhuman Remote Worker</strong>`, which has no such hook, so **both** the CSS and the realm value must change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_keycloak_theme_infra.py`:

```python
import json


def test_both_realms_use_the_srw_login_theme() -> None:
    assert '"loginTheme": "srw"' in KC.read_text()
    export = json.loads((ROOT / "docker/keycloak/realm-export.json").read_text())
    assert export["loginTheme"] == "srw"


def test_display_name_html_carries_the_logo_hook() -> None:
    """--keycloak-logo-url only renders if displayNameHtml provides
    .kc-logo-text for the stylesheet to turn into a background image."""
    assert 'kc-logo-text' in KC.read_text()
    export = json.loads((ROOT / "docker/keycloak/realm-export.json").read_text())
    assert "kc-logo-text" in export["displayNameHtml"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -k realm -v`
Expected: FAIL — `assert '"loginTheme": "srw"' in ...`

- [ ] **Step 3: Write the implementation**

In `helm/templates/services/keycloak.yaml`, line 531:

```json
      "loginTheme": "srw",
```

and line 30:

```json
      "displayNameHtml": "<div class=\"kc-logo-text\"><span>Superhuman Remote Worker</span></div>",
```

Apply the identical two changes in `docker/keycloak/realm-export.json` (lines 548 and 5).

Leave `accountTheme` as `keycloak.v3` — out of scope.

Do **not** set `displayNameHtml` to a bare `<img>`: it survives the sanitizer but requires hardcoding `/resources/<tag>/…`, which breaks on every migration.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add helm/templates/services/keycloak.yaml docker/keycloak/realm-export.json
git commit -m "feat(keycloak): select the srw login theme

displayNameHtml changes alongside it: the logo CSS variable only renders if the
realm supplies the .kc-logo-text hook keycloak.v2's stylesheet converts to a
background image. Our <strong> markup had no such hook."
```

---

### Task 10: Live gate on local k3d

**Files:** none (verification only)

**Interfaces:**
- Consumes: Tasks 7-9.
- Produces: evidence the theme loads. Nothing downstream depends on this task's output, but no further Keycloak work should proceed if it fails.

**Why a task and not a step:** a mistyped `items[].path` fails silently — Keycloak logs `Failed to find LOGIN theme srw, using built-in themes` once, at first login, and serves the built-in theme. Chart-render tests cannot catch it.

- [ ] **Step 1: Deploy to local k3d**

```bash
kubectl --context main -n superhuman-remote-worker rollout restart deploy/srw-keycloak
kubectl --context main -n superhuman-remote-worker rollout status deploy/srw-keycloak --timeout=300s
```

Verify the *running pod* picked up the theme volume, not just the Deployment spec:

```bash
kubectl --context main -n superhuman-remote-worker exec deploy/srw-keycloak -- \
  ls -R /opt/keycloak/themes/srw
```
Expected: `login/theme.properties`, `login/resources/css/srw.20260816.css`, `login/resources/img/srw-logo.svg`

- [ ] **Step 2: Assert the theme registered**

```bash
kubectl --context main -n superhuman-remote-worker logs deploy/srw-keycloak \
  | grep -i "Failed to find .* theme" || echo "no theme-fallback errors"
```
Expected: `no theme-fallback errors`

- [ ] **Step 3: Confirm the stylesheet is actually served — with gzip**

Load the login page and extract the resource tag, then fetch the CSS **twice**:

```bash
TAG=$(curl -sk "https://<keycloak-host>/realms/<realm>/protocol/openid-connect/auth?client_id=cockpit&response_type=code&redirect_uri=https://<cockpit-host>/" \
  | grep -o '/resources/[a-z0-9]*/' | head -1 | cut -d/ -f3)
curl -sk -o /dev/null -w "identity %{http_code}\n" "https://<keycloak-host>/resources/$TAG/login/srw/css/srw.20260816.css"
curl -sk -H 'Accept-Encoding: gzip' -o /dev/null -w "gzip     %{http_code}\n" "https://<keycloak-host>/resources/$TAG/login/srw/css/srw.20260816.css"
```
Expected: both 200. **Testing only the identity path will not reveal the stale-gzip failure** — that is the whole point of fetching both.

- [ ] **Step 4: Visual check in both colour schemes**

Drive Playwright against the login page (per project convention: `https://localhost`, already authenticated, never the remote cluster) with `prefers-color-scheme` forced light and then dark. Confirm: cream card with porphyry stripe in light, slate card with blood-red stripe in dark, sharp corners in both, SRW wordmark visible, and header text legible (not white-on-cream).

- [ ] **Step 5: Record the result**

Append a short PASS/FAIL note with the screenshots to `docs/features/email_and_login_theme_alignment.md` under a new "Live gate" heading, then commit.

```bash
git add docs/features/email_and_login_theme_alignment.md
git commit -m "docs(features): record the Keycloak login theme live gate"
```

---

## Slice 3 — Keycloak email theme + SMTP

### Task 11: Keycloak email wrapper

**Files:**
- Create: `helm/keycloak-theme/srw/email/theme.properties`, `helm/keycloak-theme/srw/email/html/template.ftl`
- Modify: `helm/templates/services/keycloak.yaml` (two `items` entries, `emailTheme`), `docker/keycloak/realm-export.json` (`emailTheme`)
- Test: `tests/test_keycloak_theme_infra.py` (extend)

**Interfaces:**
- Consumes: the ConfigMap mechanism from Task 8.
- Produces: branded Keycloak email.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_keycloak_theme_infra.py`:

```python
def test_email_theme_parents_base_not_keycloak() -> None:
    """keycloak/email/ holds only theme.properties today, so the layer is a
    no-op that Red Hat can add files to in any patch release."""
    props = (THEME / "email/theme.properties").read_text()
    assert "parent=base" in props


def test_email_wrapper_guards_version_dependent_variables() -> None:
    ftl = (THEME / "email/html/template.ftl").read_text()
    # `ltr` only exists from KC 26.2; unguarded it makes EVERY email fail to send.
    assert "ltr!true" in ftl
    # Never reference theme resources for images: those URLs embed the migration
    # tag and 404 for every already-delivered email after an upgrade.
    assert "url.resourcesUrl" not in ftl
    assert "url.resourcesCommonUrl" not in ftl
    # Per-type variables must not appear in a wrapper shared by all types.
    for per_type in ("${link}", "${event}", "${code}"):
        assert per_type not in ftl


def test_email_wrapper_sets_inline_fallbacks() -> None:
    """Message bodies are inherited <p>/<a> fragments; clients that strip
    <head> must still get sane typography from the containing <td>."""
    ftl = (THEME / "email/html/template.ftl").read_text()
    assert "font-family" in ftl.split("<#nested>")[0].split("<body")[1]


def test_both_realms_use_the_srw_email_theme() -> None:
    import json
    assert '"emailTheme": "srw"' in KC.read_text()
    export = json.loads((ROOT / "docker/keycloak/realm-export.json").read_text())
    assert export["emailTheme"] == "srw"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -k email -v`
Expected: FAIL — `FileNotFoundError` on `email/theme.properties`

- [ ] **Step 3: Write the implementation**

`helm/keycloak-theme/srw/email/theme.properties`:

```properties
# parent=base, NOT parent=keycloak: keycloak/email/ contains only a
# theme.properties today, so the extra layer buys nothing and is a surface Red
# Hat can add files to in a patch release.
#
# ${env.VAR:default} substitution is supported, so branding is per-environment
# with no rebuild. Always supply a default -- an unresolved ${...} is left in
# the string literally.
parent=base
brandName=Superhuman Remote Worker
logoUrl=${env.SRW_EMAIL_LOGO_URL:}
```

`helm/keycloak-theme/srw/email/html/template.ftl`:

```ftl
<#--
  SRW wrapper for every Keycloak email.

  All 16 templates in base/email/html/ import this macro, so overriding this
  one file rebrands verify-address, password-reset, org-invite and the event
  notifications -- including types added in future Keycloak releases.

  Version guards:
    ltr  exists only from KC 26.2  -> ltr!true   (unguarded = NO email sends)
    url  optional from KC 26.4     -> not referenced at all

  Only variables set for EVERY email type are used (realmName, properties,
  locale). link/event/code are per-type and would break the types that omit them.

  The logo is an EXTERNAL absolute URL, never a theme resource: theme resource
  URLs embed the migration tag, and emails are archival, so on the next
  Keycloak upgrade every logo in every already-delivered mail would 404.
  Base64 data-URIs are stripped by Gmail and Outlook.
-->
<#macro emailLayout>
<#assign _lang  = (locale.language)!"en">
<#assign _dir   = (ltr!true)?then("ltr","rtl")>
<#assign _brand = (properties.brandName)!realmName>
<!DOCTYPE html>
<html lang="${_lang}" dir="${_dir}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>${_brand}</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings>
<o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
<style>
  :root { color-scheme: light; supported-color-schemes: light; }
  body { margin:0 !important; padding:0 !important; width:100% !important; }
  table { border-collapse:collapse; }
  .srw-body p { margin:0 0 16px; font-size:15px; line-height:24px; color:#2a1d12; }
  .srw-body p:last-child { margin-bottom:0; }
  .srw-body a { color:#9c2832; font-weight:600; text-decoration:underline; }
  .srw-body b { color:#2a1d12; }
</style>
</head>
<body bgcolor="#f3ece0" style="margin:0;padding:0;background-color:#f3ece0;">
<div role="article" aria-roledescription="email" lang="${_lang}" dir="${_dir}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3ece0" style="background-color:#f3ece0;">
<tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#fbf6ec" style="width:600px;max-width:600px;background-color:#fbf6ec;border:1px solid #dccfb6;">
<tr><td bgcolor="#9c2832" style="background-color:#9c2832;height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:24px 32px 8px 32px;">
<#if (properties.logoUrl)?has_content>
  <img src="${properties.logoUrl}" width="200" alt="${_brand}" style="display:block;width:200px;max-width:200px;height:auto;">
<#else>
  <span style="font-family:Georgia,'Times New Roman',serif;font-size:20px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#2a1d12;">${_brand}</span>
</#if>
</td></tr>
<#-- Inline typography here is the fallback for clients that strip <head>. -->
<tr><td class="srw-body" style="padding:16px 32px 24px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:15px;line-height:24px;color:#2a1d12;">
<#nested>
</td></tr>
<tr><td style="padding:16px 32px 24px 32px;border-top:1px solid #dccfb6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:12px;line-height:18px;color:#8a7b66;">
${_brand}
</td></tr>
</table>
</td></tr>
</table>
</div>
</body>
</html>
</#macro>
```

Add the two `items` entries to the `keycloak-theme` volume in `helm/templates/services/keycloak.yaml`:

```yaml
              - key: email_theme.properties
                path: email/theme.properties
              - key: email_html_template.ftl
                path: email/html/template.ftl
```

Set `"emailTheme": "srw",` next to `loginTheme` in both `helm/templates/services/keycloak.yaml` and `docker/keycloak/realm-export.json`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -v`
Expected: all pass, including `test_configmap_enumerates_every_theme_file` (which now covers the two new files).

- [ ] **Step 5: Live gate via the admin Test connection button**

Roll the Keycloak pod, then in the admin console go to **Realm Settings → Email → Test connection**. That path renders `email-test.ftl`, which imports our wrapper — the fastest end-to-end check available. Capture the mail in the dev catcher and confirm the cream card, red stripe and brand header, with Keycloak's own copy intact inside.

Also confirm the message still has a plaintext part (there is no `text/template.ftl`; multipart must remain valid).

- [ ] **Step 6: Commit**

```bash
git add helm/keycloak-theme/srw/email helm/templates/services/keycloak.yaml docker/keycloak/realm-export.json tests/test_keycloak_theme_infra.py
git commit -m "feat(keycloak): brand Keycloak's own transactional email

One template.ftl override covers all 16 email types, since every one imports
the macro. parent=base rather than keycloak. The ltr variable is guarded --
it only exists from 26.2 and an unguarded reference makes every email fail to
send on older installs. The logo is an external URL because theme-resource
URLs embed the migration tag and 404 after an upgrade."
```

---

### Task 12: Unpin the Keycloak SMTP port

**Files:**
- Modify: `helm/templates/services/keycloak.yaml:914`
- Test: `tests/test_keycloak_theme_infra.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing downstream.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_keycloak_theme_infra.py`:

```python
def test_smtp_port_and_tls_come_from_values() -> None:
    """values.yaml exposes email.smtp.port but the bootstrap hardcoded 1025 --
    a dev mail-catcher port -- so any real relay on 587 was unreachable."""
    kc = KC.read_text()
    assert 'smtpServer.port=1025' not in kc
    assert ".Values.email.smtp.port" in kc
    assert ".Values.email.smtp.useTls" in kc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_keycloak_theme_infra.py -k smtp -v`
Expected: FAIL — `assert 'smtpServer.port=1025' not in kc`

- [ ] **Step 3: Write the implementation**

In `helm/templates/services/keycloak.yaml`, replace line 914 and the `starttls` line:

```bash
                        -s "smtpServer.port={{ .Values.email.smtp.port | default "1025" }}" \
```
```bash
                        -s "smtpServer.starttls={{ .Values.email.smtp.useTls | default "true" }}" \
```

Defaults reproduce today's behaviour exactly, so existing installs are unaffected. Implicit TLS (port 465, needing `smtpServer.ssl=true`) stays out of scope — it needs a separate values key and has no current consumer.

- [ ] **Step 4: Run tests and render**

```bash
python -m pytest tests/test_keycloak_theme_infra.py -v
helm template srw ./helm --set email.smtp.port=587 --set email.smtp.host=smtp.example.com \
  | grep 'smtpServer.port'
```
Expected: tests pass; the rendered output shows `smtpServer.port=587`.

- [ ] **Step 5: Commit**

```bash
git add helm/templates/services/keycloak.yaml tests/test_keycloak_theme_infra.py
git commit -m "fix(keycloak): read SMTP port and TLS from values

The bootstrap hardcoded port 1025 -- a dev mail-catcher -- while values.yaml
advertised email.smtp.port as configurable, so Keycloak alone ignored any real
relay. Defaults preserve current behaviour."
```

---

## Notes for the executor

**Slice independence.** Slice 1 (Tasks 1-6) touches no chart and ships alone; given the alpha window, it is the one to land first. Slices 2-3 modify a `Deployment` that has previously tripped the `env[N].valueFrom` strategic-merge patch bug in this chart — if `helm upgrade` fails with that error, delete and recreate the Deployment.

**Three failure modes that look like "nothing happened":**
1. A missing `items[].path` entry → no theme directory → Keycloak silently serves built-in themes, logging one ERROR at first login. Task 10 Step 2 is what catches it.
2. Theme edits without a pod roll → all three caches serve stale content.
3. The gzip cache → stale CSS to browsers while `curl` shows it fresh. Always test with `Accept-Encoding: gzip`.

**Dev iteration.** Setting `KC_SPI_THEME_CACHE_THEMES=false`, `KC_SPI_THEME_CACHE_TEMPLATES=false` and `KC_SPI_THEME_STATIC_MAX_AGE=-1` removes all three traps and makes theme edits hot-reload. These are runtime options, no `kc.sh build`. **Never set them in production.**

**Deferred deliberately:** `orchestrator/mcp/templates/consent.html` (11 Catppuccin hexes, separate surface); self-hosted Cinzel for the login page (the CSS already falls back to `Georgia, serif`, so the theme is correct without it — add the woff2 as `binaryData` later if the wordmark alone proves insufficient); implicit-TLS SMTP on port 465; `accountTheme`.
