---
tags:
  - feature
  - design-system
  - email
  - keycloak
  - branding
  - ux
aliases:
  - branded emails
  - keycloak login theme
  - email template redesign
  - srw keycloak theme
related:
  - "[[cockpit_owned_auth_ui]]"
  - "[[design_system_completion]]"
  - "[[headless_persistent_sessions]]"
  - "[[notify_user_tool]]"
---

# Email + Keycloak login theme alignment

> Bring the two remaining unbranded surfaces — transactional email and the Keycloak login page — onto the Imperial design system. Emails move off the retired Catppuccin palette onto Travertine and gain a shared layout module. Keycloak gets a CSS-only child theme (`srw`) covering both its login pages and its own transactional email, delivered as a ConfigMap so we stay on the upstream image.

**Status:** Design approved 2026-08-11. Not yet implemented.
**Triggered by:** Both surfaces still carry pre-Imperial design. The email templates use Catppuccin Mocha — the palette `cockpit/src/styles/README.md` explicitly calls "the Catppuccin era" and migrates localStorage away from. Keycloak still serves the stock `keycloak` login theme.
**Scope:** Three SRW application emails, a shared email layout module, a Keycloak `srw` login theme, a Keycloak `srw` email theme, and unpinning the hardcoded Keycloak SMTP port. **Does not** replace Keycloak's login pages with cockpit-native ones (see [[cockpit_owned_auth_ui]]), change any email copy, touch plaintext email bodies, or add MFA/registration flows.

## TL;DR

| Layer | Change |
|---|---|
| **Email module** | New `orchestrator/services/email_layout.py` — one `render_email()` used by all three send-sites. Travertine palette, table-based, no web fonts. |
| **Email call sites** | `send_system_notification` (currently unstyled), `send_agent_message`, and the headless permission mail all collapse onto the shared renderer. |
| **KC login** | New `srw` theme, `parent=keycloak.v2`, one stylesheet + one logo. Zero FreeMarker. Overrides PatternFly v5 custom properties with Imperial tokens. Light/dark both filled. |
| **KC email** | Same `srw` theme, `email/html/template.ftl` only — the 6-line wrapper macro every KC email imports. Verify/reset/invite all inherit branding. |
| **Delivery** | Theme source in `helm/keycloak-theme/srw/`, rendered into a ConfigMap and mounted at `/opt/keycloak/themes/srw`. docker-compose bind-mounts the same directory. Upstream image unchanged. |
| **Chart fix** | `smtpServer.port` stops being hardcoded to `1025`. |

**Estimated effort:** ~1 day. Three independently-shippable slices; the email slice ships without touching the chart at all.

## Why not cockpit-owned auth pages

[[cockpit_owned_auth_ui]] evaluated theming Keycloak and rejected it — its option 2 was *"Keycloak theme heavily customized … ~3-5d to make it look acceptable and we'd still want to redo it later."*

That costing assumed a **FreeMarker rewrite**. This design does not touch FreeMarker on the login side at all: a child theme that declares `parent=keycloak.v2` and ships only a stylesheet inherits every template from the parent. That is hours, not days, and it is deliberately disposable — when cockpit-owned auth lands, `helm/keycloak-theme/srw/login/` is deleted and `loginTheme` reverts. Nothing else depends on it.

So this design does not overturn the prior decision. It fills the gap until that decision gets implemented, at a cost low enough that throwing it away is not painful.

## Current state

### Email

Three send-sites, each hand-rolling complete HTML inline. No shared layer, so they have already drifted apart:

| Site | Purpose | Today |
|---|---|---|
| `orchestrator/services/email.py:207` | `send_system_notification` | Unstyled — `font-family: sans-serif; color: #222` |
| `orchestrator/services/email.py:307` | `send_agent_message` | Catppuccin Mocha card |
| `orchestrator/services/headless_notifications.py:323` | Permission request (Approve/Deny) | Catppuccin Mocha card |

The two styled ones use `#1e1e2e` background, `#cdd6f4` text, `#cba6f7` headings, `#a6e3a1` / `#f38ba8` action buttons, and 12px/6px border radii. Every one of those values is two design generations old, and the rounded corners contradict the Roman shape pass (`--radius-sm: 0`).

### Keycloak

`loginTheme` is `"keycloak"` in three places:

- `docker/keycloak/realm-export.json:548`
- `helm/templates/services/keycloak.yaml:531`
- `deployment/legacy/18-keycloak.yaml:425`

No custom theme exists; there are no `.ftl` files anywhere in the repo. `emailTheme` is unset, so Keycloak's own mail renders on the stock template.

Worth stating plainly: `"keycloak"` is the **legacy** login theme. Verified against the shipped image, it loads PatternFly **v3 + v4**, while `keycloak.v2` — also present in the same image — loads PatternFly **v5** and declares `darkMode=true`. Part of the improvement here is simply no longer being on the deprecated page.

## Verification performed during design

All of the following were checked against `quay.io/keycloak/keycloak:26.2` directly, not assumed:

- Bundled themes are exactly `base`, `keycloak`, `keycloak.v2`. Built-in themes live inside `org.keycloak.keycloak-themes-26.2.5.jar`, not on disk — `/opt/keycloak/themes/` contains only a README, which is why mounting a theme directory there is safe and non-destructive.
- `keycloak.v2/login/theme.properties` declares `parent=base`, `styles=css/styles.css`, `stylesCommon=vendor/patternfly-v5/…`, and `darkMode=true`, with `kcDarkModeClass=pf-v5-theme-dark`.
- `base/login/theme.properties` declares **no** `styles` key, so the concrete theme owns the list. A child declaring `styles=css/styles.css css/srw.css` resolves the first up the parent chain and the second from itself. `stylesCommon` is inherited untouched.
- The PatternFly v5 custom properties the design overrides all exist in the shipped vendor CSS with the expected names.
- `base/email/html/template.ftl` is a 6-line macro, and `password-reset.ftl` / `email-verification.ftl` import it — so overriding that one file rebrands every Keycloak email.
- `keycloak/email/theme.properties` contains only `parent=base`, confirming the stock email theme adds nothing we would lose.
- The Keycloak container runs `start` (production mode), so **themes are cached** and a ConfigMap edit alone will not take effect.

## Design

### 1. Shared email layout module

New file `orchestrator/services/email_layout.py`. Pure functions, no I/O, no dependency on `EmailService` — so it tests without a mail server.

```python
@dataclass(frozen=True)
class Action:
    label: str
    url: str
    variant: str = "primary"   # "primary" | "danger" | "neutral"

def render_email(
    *,
    title: str,
    body_html: str,
    subtitle: str | None = None,
    actions: Sequence[Action] = (),
    footer_note: str | None = None,
) -> str:
    ...
```

**`body_html` is trusted, caller-escaped HTML.** The existing `html.escape` calls on tool name and arguments in `headless_notifications.py` stay exactly where they are. `render_email` is a layout function, not a sanitizing boundary, and its docstring must say so — otherwise a future caller will assume it escapes and introduce an injection.

#### Email-specific constraints

These are not the cockpit's CSS rules, and the differences are deliberate:

- **Table-based layout.** Outlook renders through the Word engine and collapses `div` + `max-width`. The current templates use `div`s and are already broken there.
- **No web fonts.** Mail clients do not reliably load them. Cinzel's role is filled by `Georgia, 'Times New Roman', serif` with uppercase and letter-spacing — a widely-available serif that preserves the Roman inscriptional intent. Body text uses a system sans stack approximating Inter.
- **`border-radius: 0`** — matches the Roman shape language and is what Outlook renders anyway.
- **`<meta name="color-scheme" content="light">`** plus `supported-color-schemes`, to stop Apple Mail and Outlook force-inverting a light card into mud.
- **A `<style>` block in `<head>`** for `a { color: … }`. Links do not inherit colour from an inline-styled ancestor, and Gmail has supported head `<style>` since 2016. Structural styling stays inline; only link colour and a couple of resets go in the block.

#### Palette

Travertine only, per the light/dark decision below. Values mirror `$travertine-theme` in `cockpit/src/styles/themes/_theme-config.scss`:

| Role | Token | Hex |
|---|---|---|
| Page background | `app-bg` | `#f3ece0` |
| Card surface | `panel-bg` | `#fbf6ec` |
| Code / args block | `surface-0` | `#ede4d2` |
| Border | `border-color` | `#dccfb6` |
| Body text | `text-primary` | `#2a1d12` |
| Secondary text | `text-secondary` | `#5a4632` |
| Muted / legal | `text-muted` | `#8a7b66` |
| Links, primary action | `accent-color` | `#9c2832` |
| Approve action | `success` | `#446b3e` |
| Deny action | `danger` | `#9c2832` |
| Text on filled action | `on-accent` | `#fff` |

Approve stays green (Laurel) and Deny becomes Blood red. Per `design/themes/README.md`, Laurel is reserved for *"real completion … only outcomes"* — an approval decision qualifies. Blood legitimately covers both brand and destructive states, so Deny reads correctly in it.

#### Drift guard

The hexes are hardcoded in Python: the orchestrator image does not contain `cockpit/src`, so runtime cannot read the SCSS. To stop this rotting the way Catppuccin did, a unit test parses `_theme-config.scss` and asserts the Python constants still match. The test runs from the repo, where both trees exist. ~20 lines, and it is the only mechanism preventing a repeat of exactly the problem this design exists to fix.

#### Call-site migration

| Site | After |
|---|---|
| `email.py:200` `send_system_notification` | `render_email(title="Superhuman Remote Worker", body_html=…)` — largest visual change, it is currently unstyled |
| `email.py:294` `send_agent_message` | `render_email(title=…, body_html=message_html)` |
| `headless_notifications.py:293` `_build_permission_email_bodies` | `render_email(title="Permission requested", subtitle=…, body_html=<tool + args>, actions=[Approve, Deny], footer_note=…)` |

Plaintext bodies are unchanged. The function keeps returning `(text, html)`.

### 2. Keycloak `srw` login theme

Source lives at `helm/keycloak-theme/srw/`.

**Why inside the chart:** Helm cannot read files outside its own directory, so `.Files.Glob` requires the theme to sit under `helm/`. docker-compose bind-mounts the same path from the repo root, keeping one source of truth for both deployment modes.

```
helm/keycloak-theme/srw/
  login/
    theme.properties
    resources/css/srw.css
    resources/img/logo.svg
  email/
    theme.properties
    html/template.ftl
```

`login/theme.properties`:

```properties
parent=keycloak.v2
import=common/keycloak
styles=css/styles.css css/srw.css
```

`css/styles.css` resolves up the chain to keycloak.v2's own stylesheet; `css/srw.css` is ours. `stylesCommon` (the PatternFly v5 vendor bundle) is inherited and must not be redeclared.

`srw.css` overrides PatternFly v5 global custom properties. Light values under `:root`, dark values under both `.pf-v5-theme-dark` and `@media (prefers-color-scheme: dark)` — keycloak.v2 already ships the dark-mode plumbing, so we only supply values:

| PF5 token | Travertine | Senate |
|---|---|---|
| `--pf-v5-global--primary-color--100` | `#9c2832` | `#cc4647` |
| `--pf-v5-global--BackgroundColor--100` | `#fbf6ec` | `#1c1c22` |
| `--pf-v5-global--BackgroundColor--200` | `#f3ece0` | `#141418` |
| `--pf-v5-global--Color--100` | `#2a1d12` | `#f4f2ee` |
| `--pf-v5-global--Color--200` | `#5a4632` | `#8c8a87` |
| `--pf-v5-global--BorderColor--100` | `#dccfb6` | `#33333d` |
| `--pf-v5-global--link--Color` | `#9c2832` | `#cc4647` |
| `--pf-v5-global--BorderRadius--sm` | `0` | `0` |
| `--pf-v5-global--FontFamily--text` | `'Inter', …` | `'Inter', …` |
| `--pf-v5-global--FontFamily--heading` | `'Cinzel', Georgia, serif` | `'Cinzel', Georgia, serif` |

Fonts load from Google Fonts, matching what `cockpit/src/index.html:40` already does. Air-gapped installs degrade to the system stack — the same behaviour the cockpit already has, so this introduces no new class of failure.

Beyond tokens, the stylesheet applies the Roman shape pass to the login card: sharp corners, and the Inset Stamp treatment on the primary submit button.

Realm config sets `"loginTheme": "srw"` in all three locations listed under *Current state*.

### 3. Keycloak `srw` email theme

`email/theme.properties` is one line, `parent=keycloak`. `email/html/template.ftl` replaces the stock wrapper:

```ftl
<#macro emailLayout>
<html lang="${locale.language}" dir="${(ltr)?then('ltr','rtl')}">
<body>
    <#nested>
</body>
</html>
</#macro>
```

…with the same Travertine card the SRW emails use, wrapping `<#nested>`. Because every Keycloak email imports this macro, verify-address, password-reset, org-invite and the event notifications all inherit branding from this single file.

The message bodies themselves come from Keycloak's `messages_*.properties` as raw HTML fragments containing `<p>` and `<a>`. They are **not** overridden — no copy changes, no `messages/` directory, no i18n surface to maintain. This is precisely why the wrapper needs its head `<style>` block: it is the only way to reach those inherited `<a>` tags.

Plaintext (`text/`) templates are untouched.

Realm config gains `"emailTheme": "srw"`.

### 4. Theme delivery

A ConfigMap rendered from `helm/keycloak-theme/**`, mounted read-only at `/opt/keycloak/themes/srw`. Insertion points in `helm/templates/services/keycloak.yaml`: `volumeMounts` at line 1169, `volumes` at line 1198.

**Key collision.** `login/theme.properties` and `email/theme.properties` share a basename, so `.AsConfig` cannot be used — it keys by basename and one would silently overwrite the other. Keys must be path-mangled (`login_theme.properties`, `email_html_template.ftl`, …) and mapped back through `items[].path`, which accepts subdirectories:

```yaml
volumes:
  - name: kc-theme
    configMap:
      name: {{ include "srw.fullname" . }}-keycloak-theme
      items:
        - key: login_theme.properties
          path: login/theme.properties
        - key: login_resources_css_srw.css
          path: login/resources/css/srw.css
        - key: login_resources_img_logo.svg
          path: login/resources/img/logo.svg
        - key: email_theme.properties
          path: email/theme.properties
        - key: email_html_template.ftl
          path: email/html/template.ftl
```

**Cache busting is mandatory, not optional.** Keycloak runs `start` (production mode), which caches themes, and updating a ConfigMap does not restart pods. Without a `checksum/keycloak-theme: {{ … | sha256sum }}` annotation on the pod template, theme edits will appear to do nothing. This is the single most likely way to lose an hour on this feature.

docker-compose mounts the same directory directly:

```yaml
volumes:
  - ./helm/keycloak-theme/srw:/opt/keycloak/themes/srw:ro
```

### 5. SMTP port fix

`helm/templates/services/keycloak.yaml:914` hardcodes `-s "smtpServer.port=1025"` — a dev mail-catcher port — while `values.yaml:1918` exposes `email.smtp.port` as configurable. Any operator pointing the chart at a real relay on 587 gets a Keycloak that still dials 1025.

Fix: `{{ .Values.email.smtp.port | default "1025" }}`, and derive `smtpServer.starttls` from `.Values.email.smtp.useTls`. Defaults preserve current behaviour exactly, so existing installs see no change.

Implicit TLS (port 465, requiring `smtpServer.ssl=true`) is **out of scope** — it needs a separate values key and has no current consumer. Noted here so the omission is deliberate rather than forgotten.

## Decisions

**Light emails, auto login.** Emails are Travertine-only. Dark HTML mail is the fragile case — Gmail on Android and Outlook.com force-invert it, and `design/themes/README.md` scopes Travertine to *"formal or print-adjacent contexts,"* which is exactly email's register. The login page instead honours `prefers-color-scheme`, mirroring the cockpit's own `system` default, so the front door behaves like the app behind it.

**ConfigMap over a custom image.** A custom Keycloak image would give cleaner runtime semantics and proper theme caching, but it makes us the owner of Keycloak's patch cadence — every KC CVE becomes our rebuild. Staying on the upstream tag means security patches land for free. The theme is text-only (SVG logo included), so the 1MB ConfigMap limit is not a constraint.

**Rejected: per-message Keycloak email templates.** Overriding `password-reset.ftl` et al. would allow bespoke layouts per message type, at the cost of owning ~16 templates across the i18n surface. The single wrapper gets essentially all the visual benefit for one file.

## Testing

**Email**
- Unit tests on `render_email`: title/subtitle render, actions become anchors with correct `href` and variant colours, empty `actions` omits the footer band, `body_html` passes through unescaped.
- The palette drift test against `_theme-config.scss`.
- Existing tests were checked and assert on semantics, not colour — `tests/test_headless_notifications_phase4.py:495-501` looks for `"run_command"`, the approve/deny URLs, and HTML escaping. All survive a restyle. `tests/test_email_service.py` passes `body_html` as a fixture and does not inspect it.
- Visual: render all three to disk, open in a browser, and send through the dev mailpit catcher on 1025.

**Keycloak**
- `helm template` to confirm the ConfigMap and mounts render, then `kubectl apply --dry-run=server` — mocked clients validate nothing about manifest shape.
- Playwright against local k3d: login page in both colour schemes, plus a password-reset mail captured in mailpit.
- Confirm the checksum annotation actually rolls the pod when the theme changes. This is the assertion that proves the delivery mechanism works, and it is easy to skip.

## Risks

1. **Theme cache** — covered above. If the login page looks unchanged after a deploy, this is why.
2. **`Deployment` strategic-merge** — Keycloak is a `Deployment` (`keycloak.yaml:540`). Adding volumes has previously triggered the `env[N].valueFrom` strategic-merge patch bug in this chart; remedy is delete + recreate the Deployment.
3. **Google Fonts availability** — degrades to system stack; acceptable and consistent with existing cockpit behaviour.
4. **`keycloak.v2` is not `keycloak`** — moving parent themes changes the underlying PatternFly major version (v3/v4 → v5). Markup and class names differ, so the login page will restructure somewhat even before our CSS applies. This is intended, but it means the "before" screenshot will differ more than a pure recolour would suggest.

## Slices

Independently shippable, in dependency order:

1. **Email module + three call sites.** No chart changes, no deploy coupling. Ships alone.
2. **Keycloak login theme + ConfigMap delivery + checksum annotation.** The chart work lands here.
3. **Keycloak email theme + SMTP port fix.** Reuses slice 2's delivery mechanism; the port fix is what makes slice 3 observable outside dev.
