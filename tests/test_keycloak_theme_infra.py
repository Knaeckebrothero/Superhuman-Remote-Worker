"""Invariants for the Keycloak `srw` theme.

The dark-mode assertion is the important one: PatternFly v5 wraps every dark
token in :where(), which has zero specificity, so putting dark values under
:root or a media query loses to nothing and silently disables dark mode.
"""

import json
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


def _ftl_directives(text: str) -> str:
    """.ftl with <#-- ... --> comments removed.

    template.ftl's header comment documents each version guard by name,
    including the literal guard expression itself (e.g. "-> ltr!true" in its
    prose). A raw-text assertion for that guard would keep passing even if
    the real <#assign> below it were weakened to an unguarded reference,
    because the comment alone satisfies the substring check. Strip
    FreeMarker comments before judging directives -- the FTL analogue of
    _css_rules() above.
    """
    return re.sub(r"<#--.*?-->", "", text, flags=re.S)


def _properties_directives(text: str) -> str:
    """.properties with comment (#...) and blank lines removed.

    theme.properties headers routinely restate the exact key=value they are
    explaining (e.g. email/theme.properties opens "# parent=base, NOT
    parent=keycloak: ..."). A raw-text assertion for "parent=base" is
    satisfied by that comment even if the real directive were edited to
    something else. Strip comments before judging directives -- the
    .properties analogue of _css_rules() above.
    """
    lines = (line for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))
    return "\n".join(lines)


def test_login_theme_properties_are_correct() -> None:
    # The header comment restates "parent=keycloak.v2" verbatim while
    # explaining it, so this must judge directives, not raw text (see
    # _properties_directives).
    props = _properties_directives((THEME / "login/theme.properties").read_text())
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


def test_email_theme_parents_base_not_keycloak() -> None:
    """keycloak/email/ holds only theme.properties today, so the layer is a
    no-op that Red Hat can add files to in any patch release.

    The header comment restates "parent=base" verbatim while explaining it,
    so this must judge directives, not raw text (see _properties_directives).
    """
    props = _properties_directives((THEME / "email/theme.properties").read_text())
    assert "parent=base" in props


def test_email_wrapper_guards_version_dependent_variables() -> None:
    # The header comment restates "ltr!true" verbatim while explaining the
    # guard, so a raw-text presence check would keep passing even if the real
    # <#assign> were weakened to an unguarded ${ltr}. Judge directives only
    # (see _ftl_directives) -- and for the same reason, do it for the absence
    # checks below too: a comment mentioning a forbidden token in passing
    # must not be able to fail this test.
    ftl = _ftl_directives((THEME / "email/html/template.ftl").read_text())
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
    <head> must still get sane typography from the containing <td>.

    Strips FTL comments first (see _ftl_directives), then isolates the <tr>
    immediately preceding <#nested> rather than everything since <body>: the
    fallback logo <span> above it also carries a (unrelated) font-family, so
    a looser slice would still pass after the containing <td>'s own
    font-family was deleted.
    """
    ftl = _ftl_directives((THEME / "email/html/template.ftl").read_text())
    enclosing_td = ftl.split("<#nested>")[0].rsplit("<tr>", 1)[1]
    assert "font-family" in enclosing_td


def test_both_realms_use_the_srw_email_theme() -> None:
    assert '"emailTheme": "srw"' in KC.read_text()
    export = json.loads((ROOT / "docker/keycloak/realm-export.json").read_text())
    assert export["emailTheme"] == "srw"


def test_smtp_port_and_tls_come_from_values() -> None:
    """values.yaml exposes email.smtp.port but the bootstrap hardcoded 1025 --
    a dev mail-catcher port -- so any real relay on 587 was unreachable."""
    kc = KC.read_text()
    assert 'smtpServer.port=1025' not in kc
    assert ".Values.email.smtp.port" in kc
    assert ".Values.email.smtp.useTls" in kc


def _render_keycloak_poststart_script(*extra_args: str) -> str:
    """Render the real chart and return the keycloak Deployment's postStart
    shell script -- the text that actually runs `kcadm update ... smtpServer`.

    A source-text assertion can only ever see the literal `{{ }}` expression,
    never what Sprig's `default` actually decides at render time -- and that
    decision is exactly what's under test here: `default` treats a Go-template
    "empty" value (including the boolean `false`) as absent and substitutes
    its fallback, so `.Values.email.smtp.useTls | default "true"` silently
    re-enabled STARTTLS for an operator who explicitly passed
    `--set email.smtp.useTls=false` (Helm's `--set` parses that as a real
    bool). Only a rendered assertion proves the fix. Mirrors
    _render_theme_configmap_data() above, but reads the Deployment
    (kind == Deployment, name endswith "-keycloak") rather than the theme
    ConfigMap, and drills into containers[].lifecycle.postStart instead of
    .data.
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
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"

    for doc in yaml.safe_load_all(result.stdout):
        if (
            doc
            and doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name", "").endswith("-keycloak")
        ):
            containers = doc["spec"]["template"]["spec"]["containers"]
            keycloak = next(c for c in containers if c["name"] == "keycloak")
            return keycloak["lifecycle"]["postStart"]["exec"]["command"][-1]
    raise AssertionError("no keycloak Deployment in the render")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not installed")
def test_smtp_port_and_starttls_survive_every_override_shape() -> None:
    """Regression for three Sprig `default`/emptiness gotchas hit in
    sequence, each fix closing one hole while leaving another open:

    1. `default` treats a Go-template "empty" value -- including the
       boolean `false` -- as absent, so a bare `--set ...useTls=false`
       (parsed as a real bool, not a string) silently fell back to "true":
       an operator who explicitly disabled STARTTLS got it silently
       re-enabled.
    2. Piping through `toString` before `default` fixed that but traded it
       for another: `toString(nil)` renders the Go `%v` string "<nil>",
       which is non-empty, so `--set ...=null` (Helm's documented idiom for
       unsetting a value) rendered the literal text "<nil>" instead of
       falling back at all.
    3. Switching to `kindIs "invalid"` fixed both of those, but an empty
       string (`--set email.smtp.useTls=` -- reachable from an ordinary CI
       pattern like `--set email.smtp.useTls=$USE_TLS` with an unset
       variable) has kind `string`, not `invalid`, so the guard's
       else-branch rendered it verbatim: `smtpServer.starttls=`. Keycloak's
       `Boolean.parseBoolean("")` returns false, so this one fails in the
       *unsafe* direction -- STARTTLS silently disabled, credentials sent
       in cleartext -- unlike the two prior bugs, which both failed toward
       TLS staying on.

    The final guard treats "invalid" OR "empty string" as absent. All
    seven rows of the input/output matrix are asserted on RENDERED output,
    not template source: source only ever shows the literal `{{ }}`
    expression, never what the guard decided for a given input, so a
    source-text assertion cannot see any of these bugs.
    """
    # 1. Unset (normal path): values.yaml supplies the string default.
    default_script = _render_keycloak_poststart_script()
    assert "smtpServer.port=1025" in default_script
    assert "smtpServer.starttls=true" in default_script

    # 2. --set ...=null: the case neither `default` alone nor
    #    `toString | default` survived -- asserted explicitly on BOTH
    #    lines, since this is the regression the second remedy still missed
    #    and the "unset" case above never reaches this branch at all.
    null_script = _render_keycloak_poststart_script(
        "--set", "email.smtp.port=null", "--set", "email.smtp.useTls=null"
    )
    assert "smtpServer.port=1025" in null_script, (
        "--set email.smtp.port=null must fall back to the default, not render '<nil>'"
    )
    assert "smtpServer.starttls=true" in null_script, (
        "--set email.smtp.useTls=null must fall back to the default, not render '<nil>'"
    )

    # 3. --set ...= (empty string): the case `kindIs "invalid"` alone
    #    still missed -- it has kind `string`, not `invalid`, so the bare
    #    guard let it through verbatim as `smtpServer.starttls=`, which
    #    Keycloak parses as false (STARTTLS silently disabled -- the
    #    unsafe direction). Asserted explicitly on BOTH lines, distinct
    #    from the null case above, since an empty string and a nil value
    #    are different Go kinds and a fix for one does not imply the other.
    empty_script = _render_keycloak_poststart_script(
        "--set", "email.smtp.port=", "--set", "email.smtp.useTls="
    )
    assert "smtpServer.port=1025" in empty_script, (
        "--set email.smtp.port= (empty string) must fall back to the default"
    )
    assert "smtpServer.starttls=true" in empty_script, (
        "--set email.smtp.useTls= (empty string) must fall back to true, "
        "not render empty (Keycloak parses '' as false -- TLS silently off)"
    )

    # 4. --set ...useTls=false (bare boolean): the case the plain `default`
    #    remedy silently broke. Asserted on its own, not folded into the
    #    --set-string case below.
    bare_bool_script = _render_keycloak_poststart_script("--set", "email.smtp.useTls=false")
    assert "smtpServer.starttls=false" in bare_bool_script, (
        "a bare --set boolean false must render as false, not silently fall back to the default"
    )

    # 5. --set-string ...useTls=false: worked before every fix; kept as a control.
    string_script = _render_keycloak_poststart_script("--set-string", "email.smtp.useTls=false")
    assert "smtpServer.starttls=false" in string_script

    # 6. --set ...port=587: the ordinary configured-relay path this task exists for.
    port_script = _render_keycloak_poststart_script("--set", "email.smtp.port=587")
    assert "smtpServer.port=587" in port_script

    # 7. --set ...port=0: a real (if impractical) int, not absent -- must
    #    pass through literally and NOT be swallowed by the new
    #    empty-string clause (toString(0) == "0", not "").
    zero_port_script = _render_keycloak_poststart_script("--set", "email.smtp.port=0")
    assert "smtpServer.port=0" in zero_port_script, (
        "an explicit port=0 must render literally, not fall back to the default"
    )
