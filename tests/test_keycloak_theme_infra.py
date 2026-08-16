"""Invariants for the Keycloak `srw` theme.

The dark-mode assertion is the important one: PatternFly v5 wraps every dark
token in :where(), which has zero specificity, so putting dark values under
:root or a media query loses to nothing and silently disables dark mode.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "helm/keycloak-theme/srw"
LOGIN_CSS = next((THEME / "login/resources/css").glob("srw.*.css"), None)


def _css_rules(text: str) -> str:
    """CSS with /* ... */ comments removed.

    The dark-mode assertions below must judge rules, not prose. Reading the raw
    file makes it impossible to document the prohibited patterns by name, which
    is exactly what the header comment needs to do.
    """
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


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
    rules = _css_rules(css)
    assert ".pf-v5-theme-dark" in rules
    assert "prefers-color-scheme" not in rules, (
        "PatternFly toggles a class, not a media query -- a media query desyncs."
    )
    dark_start = rules.index(".pf-v5-theme-dark")
    root_start = rules.index(":root")
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
    rules = _css_rules(css)
    assert "fonts.googleapis.com" not in rules
    assert "@import" not in rules


def test_light_tokens_match_the_shared_brand_palette() -> None:
    """Extends the Python drift guard across the third copy of the palette.

    brand.py is checked against the SCSS by tests/test_brand_palette.py; this
    ties the Keycloak CSS to brand.py, so all three move together or a test
    fails. Without it the login page is exactly the surface that silently rots.
    """
    from orchestrator.services import brand

    css = LOGIN_CSS.read_text()
    rules = _css_rules(css)
    root = rules[rules.index(":root {"): rules.index(".pf-v5-theme-dark")]

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


KC = ROOT / "helm/templates/services/keycloak.yaml"


def test_configmap_enumerates_every_theme_file() -> None:
    """ConfigMap keys cannot contain '/', so the nested layout only exists via
    items[].path. A file missing an items entry is a silent no-op -- and a
    theme with no login/ directory simply never appears."""
    kc = KC.read_text()
    for path in sorted(p.relative_to(THEME) for p in THEME.rglob("*") if p.is_file()):
        assert f"path: {path}" in kc, f"{path} has no items[].path entry"


def _render_theme_configmap_data() -> dict[str, str]:
    """Render the real chart and return the `data` mapping the Kubernetes API
    would actually see for the `keycloak-theme` ConfigMap.

    A regex over the *template source* can only ever match the literal
    `{{ $key }}` text -- never the templated-out key it produces -- so it
    finds nothing and passes vacuously even if the `replace "/" "_"` step is
    deleted entirely. Only the rendered output proves what key the API server
    receives. Mirrors the render helper in test_vm_chart_manifest_contract.py.
    """
    result = subprocess.run(
        [
            "helm",
            "template",
            "srw",
            str(ROOT / "helm"),
            "-f",
            str(ROOT / "helm/ci/test-values.yaml"),
            "--set",
            "keycloak.enabled=true",
            "--set",
            "keycloak.internal=true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"

    for doc in yaml.safe_load_all(result.stdout):
        if (
            doc
            and doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name", "").endswith("-keycloak-theme")
        ):
            return doc.get("data") or {}
    raise AssertionError("no keycloak-theme ConfigMap in the render")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not installed")
def test_configmap_keys_have_no_slashes() -> None:
    """A real API server rejects a ConfigMap key containing '/'. Assert on the
    rendered `data` mapping, not the template source (see
    _render_theme_configmap_data), and require at least one key so a render
    that silently produced none fails loudly instead of passing vacuously."""
    data = _render_theme_configmap_data()
    assert data, "keycloak-theme ConfigMap rendered with an EMPTY data block"
    for key in data:
        assert "/" not in key, f"{key!r} still contains '/' -- the API server would reject it"


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
