---
tags:
  - feature
  - design-system
  - email
  - keycloak
  - branding
  - ux
  - accessibility
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

> Bring the two remaining unbranded surfaces — transactional email and the Keycloak login page — onto the Imperial design system. Emails move off the retired Catppuccin palette onto Travertine via a shared layout module. Keycloak gets a CSS-only child theme (`srw`) covering both login and its own transactional email, delivered as a ConfigMap so we stay on the upstream image.

**Status:** **SHIPPED** on `develop` 2026-08-16, commits `b11e06d7..36804e0e` at time of writing — but note `develop` is rebased by concurrent agents, so locate the range by its first and last commit *subjects* (`feat(brand): Travertine palette module …` through `docs(features): record the Keycloak live gate as PASSED`) rather than by these hashes. Designed 2026-08-11, revised the same day after a research pass, implemented across 12 tasks. Live gate passed via docker-compose. 138 tests green. See *Post-implementation status* at the foot of this document.
**Triggered by:** Both surfaces still carry pre-Imperial design. The email templates use Catppuccin Mocha — the palette `cockpit/src/styles/README.md` calls "the Catppuccin era" and migrates localStorage away from. Keycloak still serves the stock, now-deprecated `keycloak` login theme.
**Scope:** Three SRW application emails, a shared brand/palette module, an email layout module, a Keycloak `srw` login theme, a Keycloak `srw` email theme, the Catppuccin magic-link landing pages, and unpinning the hardcoded Keycloak SMTP port. **Does not** replace Keycloak's login pages with cockpit-native ones (see [[cockpit_owned_auth_ui]]), change email copy, brand plaintext email, or add MFA/registration flows.

## TL;DR

| Layer | Change |
|---|---|
| **Brand module** | New `orchestrator/services/brand.py` — the Travertine palette as Python constants. Single source for emails **and** the magic-link landing pages. |
| **Email module** | New `orchestrator/services/email_layout.py` — one `render_email()` for all three send-sites. Escapes text params, entity-encodes non-ASCII. |
| **Email call sites** | `send_system_notification` (currently unstyled), `send_agent_message`, and the headless permission mail collapse onto the shared renderer. Fixes a live HTML injection. |
| **Landing pages** | `_magic_link_confirmation_page` / `_magic_link_result_page` (32 Catppuccin hexes) re-skinned from the brand module, so the email→click journey stays on-brand. |
| **KC login** | New `srw` theme, `parent=keycloak.v2`, one versioned stylesheet + logo + self-hosted display font. Zero FreeMarker. |
| **KC email** | Same theme, `parent=base`, `email/html/template.ftl` only. Logo hosted externally and parameterized. |
| **Delivery** | Theme source in `helm/keycloak-theme/srw/`, ConfigMap-mounted at `/opt/keycloak/themes/srw`, with a checksum annotation forcing a pod roll. |
| **Chart fix** | `smtpServer.port` stops being hardcoded to `1025`. |

**Effort:** estimated ~1.5 days across three slices; the implementation ran longer, almost entirely in review-driven fix rounds rather than first-pass work. Nine vacuous tests, one container-crash blocker, one WCAG regression and one shell-injection vector were found *after* the code was written — see the pattern note at the foot of this document.

## Why not cockpit-owned auth pages

[[cockpit_owned_auth_ui]] evaluated theming Keycloak and rejected it — its option 2 was *"Keycloak theme heavily customized … ~3-5d to make it look acceptable and we'd still want to redo it later."*

That costing assumed a **FreeMarker rewrite**. This design touches no FreeMarker on the login side: a child theme declaring `parent=keycloak.v2` and shipping only a stylesheet inherits every template from the parent. It is deliberately disposable — when cockpit-owned auth lands, `helm/keycloak-theme/srw/login/` is deleted and `loginTheme` reverts. Nothing else depends on it.

## Verification performed

Everything below was checked against a running `quay.io/keycloak/keycloak:26.2`, in both dev and production mode, not inferred from docs.

**Confirmed working:**
- A theme mounted read-only at `/opt/keycloak/themes/srw` is discovered with **no image rebuild and no `kc.sh build`** — in production mode, with a kubelet-faithful ConfigMap projection. `FolderThemeProvider` does a live `listFiles()` per lookup.
- `styles=css/styles.css css/srw.css` resolves the first up the parent chain and the second from our theme; both were served 200. `stylesCommon` (PatternFly v5) is inherited without redeclaring.
- A single `email/html/template.ftl` override rebranded a **real** Keycloak email — an `execute-actions` mail captured in mailpit carried our wrapper, card and accent stripe, with Keycloak's message body passing through untouched.
- The **plaintext part is still generated** when only the HTML template is overridden. There is no `email/text/template.ftl` to override.
- Every one of the 16 HTML email templates in `base/email/html/` begins `<#import "template.ftl" as layout>`, so the wrapper covers all types, including ones added in future releases.
- Keycloak's default CSP is only `frame-src 'self'; frame-ancestors 'self'; object-src 'none';` — no `style-src` or `font-src`.
- `darkMode=true` is inherited by a child theme automatically.
- `parent=keycloak.v2` is the right target: v1 is formally deprecated as of KC 26.0, and `keycloak.v2/login/theme.properties` is byte-identical across 26.2.0, 26.7.1 and `main`. No 27.x exists yet; no deprecation signal for v2 anywhere.

**Observed failure modes:**
- `ERROR [DefaultThemeManager] Failed to find EMAIL theme srw, using built-in themes` — a bad or unresolvable theme name does **not** fail the deploy. Branding silently vanishes with one log line.
- ConfigMap keys cannot contain `/` (rejected by a real API server), so a flat projection yields no theme directory and the theme never appears.

## Current state

### Email

Three send-sites, each hand-rolling complete HTML inline. No shared layer, so they have already drifted:

| Site | Purpose | Today |
|---|---|---|
| `orchestrator/services/email.py:207` | `send_system_notification` | Unstyled — `font-family: sans-serif; color: #222` |
| `orchestrator/services/email.py:307` | `send_agent_message` | Catppuccin Mocha card |
| `orchestrator/services/headless_notifications.py:323` | Permission request (Approve/Deny) | Catppuccin Mocha card |

**The blast radius is larger than the templates.** The permission email's Approve/Deny buttons land on `_magic_link_confirmation_page` (`orchestrator/main.py:31470`) and `_magic_link_result_page` (`:31568`), which carry **32 Catppuccin hexes** between them. `orchestrator/mcp/templates/consent.html` carries 11 more. Restyling only the email produces a branded mail that opens a Catppuccin page.

**There is a live HTML injection.** In both files the message body is escaped but the surrounding fields are not:

```python
# email.py:307 — message_md IS escaped above; these are not
Job: {job_description[:80]}
&nbsp;&bull;&nbsp; Agent: {config_name}

# email.py:212
f"<p>Hello {to_name},</p>"
```

`job_description` is user-supplied at job creation. Not scriptable in mail clients, but it permits injected markup and forged links in outbound email. The refactor must fix this rather than carry it forward.

### Keycloak

`loginTheme` is `"keycloak"` in `docker/keycloak/realm-export.json:548`, `helm/templates/services/keycloak.yaml:531`, and `deployment/legacy/18-keycloak.yaml:425`. No custom theme exists; no `.ftl` files in the repo. `emailTheme` is unset.

`"keycloak"` is the **deprecated v1 theme** (PatternFly v3/v4). `keycloak.v2` — in the same image — is PatternFly v5 with `darkMode=true`. Part of the win is simply leaving a deprecated page.

## Design

### 1. Brand module

New `orchestrator/services/brand.py`, holding the Travertine palette as Python constants. **Not** inside `email_layout.py` — the magic-link landing pages consume it too, which is what makes the drift guard meaningful across the whole user journey.

| Role | Token | Hex |
|---|---|---|
| Page background | `app-bg` | `#f3ece0` |
| Card surface | `panel-bg` | `#fbf6ec` |
| Code / args block | `surface-0` | `#ede4d2` |
| Border | `border-color` | `#dccfb6` |
| Body text | `text-primary` | `#2a1d12` |
| Secondary text, footer / legal | `text-secondary` | `#5a4632` |
| Links, primary action | `accent-color` | `#9c2832` |
| Approve action | `success` | `#446b3e` |
| Deny action | `danger` | `#9c2832` |
| Text on filled action | `on-accent` | `#ffffff` |

**Why `text-secondary` and not `text-muted` for footer text.** `text-muted` `#8a7b66` fails WCAG AA on every Travertine surface at footer sizes — 3.26:1 on `surface-0`, 3.82:1 on `panel-bg`, 3.50:1 on `app-bg`, against the 4.5:1 normal-text threshold. `text-secondary` measures 7.06:1 and 8.27:1 respectively while staying visibly subordinate to `text-primary` body copy. Ruled 2026-08-16, during implementation: shipping a contrast failure inside a change whose headline is a WCAG 1.4.1 fix is incoherent. `text-muted` stays in `brand.TRAVERTINE` because that dict mirrors the SCSS token map and the drift guard checks every key — it simply has no server-rendered consumer.

Hardcoding is **required, not a compromise**: CSS custom properties sit at ~45% email support, and Gmail supports `var()` but not variable *declaration*. Colors must be literal hexes at render time. `docker/Dockerfile.orchestrator` also copies only `orchestrator/`, `src/`, `config/` — the runtime cannot read the SCSS even in principle.

### 2. Email layout module

New `orchestrator/services/email_layout.py`:

```python
@dataclass(frozen=True)
class Action:
    label: str
    url: str
    variant: str = "primary"   # "primary" | "danger" | "neutral"

def render_email(
    *,
    title: str,                 # PLAIN TEXT — escaped internally
    body_html: str,             # TRUSTED HTML — caller escapes
    subtitle: str | None = None,# PLAIN TEXT — escaped internally
    actions: Sequence[Action] = (),
    footer_note: str | None = None,
) -> str:
```

`title` and `subtitle` are plain text and `html.escape()`d inside. Only `body_html` is trusted. Getting this boundary right in the API is what kills the existing injection; mirroring the current signature would preserve it.

The three hand-rolled `.replace("&","&amp;")…` chains become `html.escape()`.

#### Email HTML rules

These are not the cockpit's CSS rules. Each has a specific reason:

- **Table layout, 600px, `role="presentation"`.** Outlook's Word engine doesn't support `width` on `<div>` at all. Microsoft commits to classic Outlook **until at least 2029** — the widely-repeated "Word engine dies October 2026" claim is Office LTSC 2021's EOL, not the engine's.
- **Buttons are padded `<td>`s, never padded `<a>`s.** Outlook Windows doesn't support `display`, so an `<a>` is permanently an inline box and vertical padding cannot expand the line. Because our buttons are square, **no VML is needed**.
- **Link colors inline on every `<a>`.** The list of clients stripping `<head><style>` is *growing* — GMX, WEB.DE, SFR and LaPoste all regressed between 2023 and 2025. The `<style>` block is enhancement only. Inside it: never `a:link` (unsupported on every Gmail platform), never `url()` (Gmail drops the entire style tag), and stay under Gmail's 16KB style cap.
- **Font/color set inline on the containing `<td>`** so inherited `<p>`/`<a>` fragments still render sanely with zero CSS applied.
- **Entity-encode all non-ASCII output.** Gmail clips on non-ASCII characters *independently of size* — documented repros on `©`, `é`, and an en-dash in a tiny email. An editorial serif language reaches for `—` and curly quotes constantly. One line: `body.encode("ascii", "xmlcharrefreplace").decode("ascii")`. (The famous 102KB limit is folklore; measured clipping is ~99.5KB and irrelevant at our size.)
- **Uppercase via CSS, never in the emitted string.** Screen readers read the source and spell all-caps tokens as initialisms. Emit sentence case with `text-transform: uppercase`. Never fake tracking with literal spaces. `letter-spacing` in `px`, not `em`.
- **No `border-radius`.** It is a no-op at zero in the Word engine, so emitting it is pure bytes against the style cap.
- **No web fonts.** Headings use `Georgia, 'Times New Roman', serif`. Note that Android aliases *both* names to `serif`, so Gmail Android renders Noto Serif — the layout must not depend on Georgia's metrics. Quote every multi-word font name, and ship the `<!--[if mso]>` Arial override.
- **`<meta name="color-scheme" content="light">` *plus* `:root { color-scheme: light }`** in the style block — the meta tag alone is inert on every Apple Mail since 2019.
- **`lang`/`dir` on a body wrapper `<div role="article">`**, not only `<html>` — webmail clients strip `<html>`. Avoid `<header>`/`<main>`/`<footer>`: unsupported in Outlook, and Gmail replaces them with `<u></u>`.

#### Accessibility: Approve and Deny must not differ by hue alone

Measured against the Travertine card:

| Pair | Ratio | |
|---|---|---|
| Approve `#446b3e` vs card | 5.70:1 | passes as a control |
| Deny `#9c2832` vs card | 7.06:1 | passes |
| **Approve vs Deny** | **1.24:1** | **fails WCAG 1.4.1** |

Two same-size, same-shape rectangles differing only in red vs green — the most common colour-vision-deficiency axis — on the highest-stakes action in the product. The current Catppuccin pair measures 1.56:1, so the naive port is a *regression*.

**Approve stays solid; Deny becomes a ghost button** (card-coloured fill, `#9c2832` border and text). The two fills then differ by 5.70:1 and are distinguishable by **form**, not hue. Labels remain real text, which also preserves meaning under dark-mode inversion — where `#9c2832` inverts toward cyan-green and `#446b3e` toward magenta, potentially swapping their apparent semantics.

#### Call-site migration

| Site | After |
|---|---|
| `email.py:200` | `render_email(title="Superhuman Remote Worker", body_html=…)` |
| `email.py:294` | `render_email(title=…, subtitle=job/agent/phase, body_html=message_html)` |
| `headless_notifications.py:293` | `render_email(…, actions=[Approve solid, Deny ghost], footer_note=…)` |

Plaintext bodies unchanged; the function keeps returning `(text, html)`.

### 3. Magic-link landing pages

`_magic_link_confirmation_page` and `_magic_link_result_page` are pure `str`-returning functions, so re-skinning them from `brand.py` is self-contained. `consent.html` is a static template and can follow in the same slice or trail it.

### 4. Keycloak `srw` login theme

Source at `helm/keycloak-theme/srw/` — **inside the chart, because Helm cannot read files outside its own directory**. docker-compose bind-mounts the same path.

```
helm/keycloak-theme/srw/
  login/
    theme.properties
    resources/css/srw.<datestamp>.css
    resources/img/srw-logo.svg
    resources/fonts/cinzel-600.woff2
  email/
    theme.properties
    html/template.ftl
```

`login/theme.properties`:

```properties
parent=keycloak.v2
styles=css/styles.css css/srw.20260811.css
```

**Do not carry `import=common/keycloak`.** It is inherited — `DefaultThemeManager.loadTheme()` processes imports for every theme in the chain — and redeclaring it inserts `common/keycloak` twice, shifting property-merge order so it lands *after* `keycloak.v2`. Verified: PatternFly still loads with the line removed.

**The stylesheet filename is versioned.** Theme resources are served with `Cache-Control: max-age=2592000` (30 days), and the `/resources/<tag>/` segment is the `MIGRATION_MODEL` row id — it moves only on a version migration, never on a theme edit. Bumping the filename is the only lever that doesn't require a global cache-policy change.

#### Token overrides — specificity is the trap

PatternFly v5 wraps **every** dark-mode token redefinition in `:where(.pf-v5-theme-dark)`, and `:where()` has **zero specificity by spec**. A `:root` block (0,1,0) therefore beats it unconditionally, regardless of load order. Supplying light under `:root` and dark under a media query yields **light in both modes**.

Dark values go on the bare class, after the `:root` block:

```css
:root { --pf-v5-global--primary-color--100: #9c2832; … }
.pf-v5-theme-dark { --pf-v5-global--primary-color--100: #cc4647; … }
```

Do **not** use `@media (prefers-color-scheme: dark)` for these tokens — it desyncs from the class Keycloak actually toggles. The class is applied by JS to `<html>` (`document.documentElement`), the opposite of the cockpit's body-scoped convention.

| PF5 token | Travertine (`:root`) | Senate (`.pf-v5-theme-dark`) |
|---|---|---|
| `--pf-v5-global--primary-color--100` | `#9c2832` | `#cc4647` |
| `--pf-v5-global--BackgroundColor--100` | `#fbf6ec` | `#1c1c22` |
| `--pf-v5-global--BackgroundColor--200` | `#f3ece0` | `#141418` |
| `--pf-v5-global--Color--100` | `#2a1d12` | `#f4f2ee` |
| `--pf-v5-global--Color--200` | `#5a4632` | `#8c8a87` |
| `--pf-v5-global--BorderColor--100` | `#dccfb6` | `#33333d` |
| `--pf-v5-global--link--Color` | `#9c2832` | `#cc4647` |
| `--pf-v5-global--BorderRadius--sm` | `0` | `0` |
| `--keycloak-card-top-color` | `#9c2832` | `#cc4647` |

`--keycloak-card-top-color` is the 4px stripe atop the login card, currently PatternFly blue — the highest-visibility single token on the page.

`#kc-header-wrapper` sets `color: … !important` (keycloak.v2 gets away with it via a dark background image). Overriding the background to cream without countering this yields white-on-white; `srw.css` loads later, so an equal-specificity `!important` wins.

#### Logo

`--keycloak-logo-url` works **only in combination with realm config**. keycloak.v2 renders the header as `${kcSanitize(msg("loginTitleHtml",(realm.displayNameHtml!'')))}` — text, not an `<img>`. The master realm's logo appears only because it ships `displayNameHtml` as `<div class="kc-logo-text"><span>Keycloak</span></div>`, which the stylesheet converts to an image.

Our realm currently sets `displayNameHtml` to `<strong>Superhuman Remote Worker</strong>`, which has no such hook. Both changes are required:

```jsonc
"displayNameHtml": "<div class=\"kc-logo-text\"><span>Superhuman Remote Worker</span></div>"
```
```css
:root { --keycloak-logo-url: url('../img/srw-logo.svg');
        --keycloak-logo-width: 300px; --keycloak-logo-height: 63px; }
```

Do not set `displayNameHtml` directly to an `<img>` — it survives the sanitizer but requires hardcoding `/resources/<tag>/…`, which breaks on every migration.

#### Fonts — self-hosted, and reduced in scope

**Decision: self-host Cinzel only; use the system sans stack for body text.** The login page is the one page that must work when everything else is broken, so it should not depend on `fonts.googleapis.com`. An `@import` also serializes into a render-blocking chain, and it leaks every login attempt's IP to Google — a live GDPR argument on an EU-facing auth page. Cinzel is the brand signature and is used sparsely (headings, button labels), so one subset weight is enough; Inter versus a system sans is a subtle difference not worth the ConfigMap weight.

This diverges from `cockpit/src/index.html:40`, which loads both from Google. That is deliberate: the app can degrade to a system stack mid-session, an auth page cannot.

#### Also reachable without FreeMarker

Page `<title>` (via realm display name or `login/messages/messages_en.properties`), favicon (`login/resources/img/favicon.ico` — must be a real `.ico`, so `binaryData` in the ConfigMap), and the page background (`--keycloak-bg-logo-url`). Genuinely requiring FreeMarker: footer links and any structural markup change. There is no "Powered by Keycloak" footer in keycloak.v2.

### 5. Keycloak `srw` email theme

`email/theme.properties`:

```properties
parent=base
brandName=Superhuman Remote Worker
logoUrl=${env.SRW_EMAIL_LOGO_URL:https://srw.works/img/email-logo.png}
```

**`parent=base`, not `parent=keycloak`.** `keycloak/email/` contains only `theme.properties` (verified), so they are identical today — but the extra layer is one Red Hat can add files to in any patch release, silently altering our mail. Theme properties support `${env.VAR:default}` substitution, which also gives per-environment branding with no rebuild.

`email/html/template.ftl` replaces the stock 6-line macro with the Travertine card, wrapping `<#nested>`. Constraints:

- **Guard `ltr`:** `${(ltr!true)?then('ltr','rtl')}`. The variable only exists from 26.2; on older versions an unguarded reference makes **every email fail to send**. We pin 26.2, but customer installs may not.
- **Guard `url`:** optional from 26.4 — unguarded references break scheduled-task emails.
- **Never use `${url.resourcesCommonUrl}`** — the current keycloak.org docs recommend it for email images, but it does not exist in 26.2 and throws.
- **Never serve the logo from theme resources.** Those URLs embed the migration tag; emails are archival, so on the next Keycloak upgrade every logo in every previously-sent mail 404s. Base64 data-URIs are stripped by Gmail and Outlook; CID embedding is an open feature request. Host externally, parameterized as above.
- **Only reference variables guaranteed for every email type** — `realmName`, `properties`, `msg`, `user`, `locale`, `kcSanitize`. `link`, `event`, `code` and friends are per-type; referencing them in the wrapper breaks the types that don't set them.
- Our wrapper markup is **not** sanitized — `kcSanitize` applies only to message-bundle content.

Realm gains `"emailTheme": "srw"`.

**Plaintext stays unbranded** — there is no `text/template.ftl` to override, and multipart remains valid. Worth knowing because plaintext is what spam filters preview.

### 6. Theme delivery

A ConfigMap mounted read-only at `/opt/keycloak/themes/srw`. Insertion points in `helm/templates/services/keycloak.yaml`: `volumeMounts` at line 1169, `volumes` at line 1198.

**ConfigMap keys cannot contain `/`** — a real API server rejects them. Keys are path-mangled and mapped back through `items[].path`, which *does* accept slashes:

```yaml
volumes:
  - name: srw-theme
    configMap:
      name: {{ include "srw.fullname" . }}-keycloak-theme
      items:
        - {key: login_theme.properties,  path: login/theme.properties}
        - {key: login_srw.css,           path: login/resources/css/srw.20260811.css}
        - {key: login_srw-logo.svg,      path: login/resources/img/srw-logo.svg}
        - {key: email_theme.properties,  path: email/theme.properties}
        - {key: email_template.ftl,      path: email/html/template.ftl}
```

Every file must be enumerated by hand; adding one to the ConfigMap without an `items` entry is a silent no-op. Note `login/` and `email/` both contain `theme.properties`, so `.AsConfig` cannot be used — it keys by basename and one would overwrite the other.

**A checksum annotation is mandatory.** Three independent staleness traps exist in production:

1. `cacheThemes` caches the resolved theme, so `theme.properties` edits never reload.
2. `cacheTemplates` caches **compiled FreeMarker** — this is the one that makes `template.ftl` edits appear to do nothing.
3. The **gzip cache** writes a `.gz` on first request and only regenerates if the file is *absent* — no mtime or content check. Browsers send `Accept-Encoding: gzip` and receive stale CSS while `curl` shows it fresh. Gzip engages only when theme caching is on, so this failure mode **does not exist in dev**.

```yaml
annotations:
  checksum/theme: {{ include (print $.Template.BasePath "/keycloak-theme-configmap.yaml") . | sha256sum | quote }}
```

Our `/opt/keycloak/data` is an `emptyDir`, so a pod roll does clear the gzip cache. (Had it been a PVC, stale CSS would survive restarts indefinitely.)

Dev environments should set `KC_SPI_THEME_CACHE_THEMES=false`, `KC_SPI_THEME_CACHE_TEMPLATES=false`, `KC_SPI_THEME_STATIC_MAX_AGE=-1` — runtime options, no `kc.sh build`. Never in production.

docker-compose bind-mounts the source directly:

```yaml
volumes:
  - ./helm/keycloak-theme/srw:/opt/keycloak/themes/srw:ro
```

### 7. SMTP port fix

`helm/templates/services/keycloak.yaml:914` hardcodes `-s "smtpServer.port=1025"` — a dev mail-catcher port — while `values.yaml:1918` exposes `email.smtp.port`. Any operator pointing the chart at a real relay on 587 gets a Keycloak that still dials 1025.

Fix: `{{ .Values.email.smtp.port | default "1025" }}`, with `smtpServer.starttls` from `.Values.email.smtp.useTls`. Defaults preserve current behaviour. Implicit TLS (465, needing `smtpServer.ssl=true`) is **out of scope** — it needs a separate values key and has no current consumer.

## Decisions

**Light emails, auto login.** Emails are Travertine-only. Dark HTML mail is the fragile case, and Travertine is scoped to "formal, print-adjacent" contexts. Light-only survives *full* inversion at 5.4–14.9:1. The login page follows `prefers-color-scheme` via Keycloak's own class toggle.

**Hand-rolled HTML, no email framework.** At three templates a framework isn't justified, and — decisively — **no framework would have prevented the original problem**. The failure was *drift*, not rendering: MJML would have held the same stale `#1e1e2e`. The real cause was three duplicated call sites with no shared layer, which is what this fixes. Reconsider around 10–15 templates, multiple authors, or i18n.

**Hardcoded palette + drift test, not a generator.** Both need the same SCSS parser; a generator is the test plus a `write()`. At ~11 hexes changing once per rebrand, the extra machinery isn't worth it. Revisit at ~30 tokens or 3+ consuming languages.

**ConfigMap over a custom image.** A custom image gives cleaner caching but makes us the owner of Keycloak's patch cadence — every CVE becomes our rebuild.

**Rejected: per-message Keycloak email templates.** Overriding `password-reset.ftl` et al. means owning ~16 templates for essentially the same visual result as one wrapper.

## Testing

**Email**
- Unit tests on `render_email`: escaping of `title`/`subtitle`, `body_html` passthrough, action variants, non-ASCII entity encoding, empty-actions case.
- **Palette drift test** — must scope parsing to the `$travertine-theme` block (both maps define `accent-color`, at `#9c2832` and `#cc4647`), normalize `#fff`/`#ffffff`, and **fail closed**: assert the file exists and that *every expected key was found*, or a rename makes it pass green — the exact rot it exists to prevent. On failure it should print the corrected constants block. Use the `Path(__file__).resolve().parents[1]` idiom from `tests/test_canvas_office_infra.py`; parse-and-assert across this boundary is established house style (4 precedents).
- **Unmanaged-hex guard** — assert every `#rrggbb` literal in `email_layout.py` and the landing-page functions is a member of the palette dict. The equality test catches "the palette changed"; this catches "someone added a one-off hex," which is how drift actually starts.
- Existing tests assert on semantics, not colour (`tests/test_headless_notifications_phase4.py:495-501` checks `"run_command"`, the approve/deny URLs, escaping) and survive a restyle.
- A dev `/emails` preview route rendering all three, borrowed from Zulip — cheaper than render-to-disk and it doesn't rot.
- **The one render worth looking at manually: Yahoo desktop dark mode** against `#f3ece0`/`#fbf6ec`, where modelling predicts body text at 2.08–2.54:1.

**Keycloak**
- `helm template`, then `kubectl apply --dry-run=server`.
- **Startup smoke test**: assert `srw` appears in `GET /admin/serverinfo` → `themes.login[]`, and that the versioned CSS returns 200. A mistyped `items[].path` fails silently until someone tries to log in.
- Admin console → Realm Settings → Email → **Test connection** exercises `email-test.ftl`, which imports our wrapper — the fastest end-to-end email loop.
- Playwright against local k3d for login in both colour schemes.
- Confirm the checksum annotation rolls the pod, and re-fetch the CSS **with `Accept-Encoding: gzip`** — testing with `curl` alone will not reveal the stale-gzip failure.

## Risks

1. **Three caches, one of them invisible in dev.** The gzip cache is the dangerous one; the checksum annotation is the mitigation.
2. **Silent theme fallback.** A bad theme name or a missing `items[].path` logs one ERROR and serves built-in themes. Hence the startup smoke test.
3. **`Deployment` strategic-merge.** Keycloak is a `Deployment` (`keycloak.yaml:540`); adding volumes has previously tripped the `env[N].valueFrom` patch bug here. Remedy is delete + recreate.
4. **`:where()` specificity.** Dark-mode tokens must go on the bare class, never `:root` or a media query.
5. **1 MiB ConfigMap ceiling.** CSS + SVG + one woff2 fits; base64 inflates by 33%. Adding a favicon, background image and more weights approaches the limit, and the mechanism does not degrade gracefully — crossing it means switching to a baked image.
6. **PatternFly major bumps.** PF3→PF5 in KC 24 broke every child theme. Our `--pf-v5-*` overrides carry that risk at the next major. Nothing in 26.x or `main` signals it.
7. **Floating image tag.** `values.yaml:1197` pins `26.2`, not a patch. Worth pinning exactly — a repull can move the migration tag and silently invalidate every browser's theme cache.

## Slices

1. **Brand module + email layout + three call sites + magic-link landing pages.** No chart changes; includes the injection fix and the Approve/Deny accessibility fix. Ships alone.
2. **Keycloak login theme + ConfigMap delivery + checksum annotation + smoke test.** All chart work lands here, plus the realm `displayNameHtml` change.
3. **Keycloak email theme + SMTP port fix.** Reuses slice 2's delivery; the port fix is what makes slice 3 observable outside dev.

All three slices shipped. Slice 1 was sequenced first because it touches no chart and carries the injection and accessibility fixes; slices 2–3 followed without tripping the chart's known strategic-merge hazard.

---

## Post-implementation status (2026-08-16)

**Shipped on `develop`, unpushed:** commits `b11e06d7..2043c0f6` (hashes shift under concurrent rebases; match on commit subjects). All 12 planned tasks complete except the Task 10 live gate (below). 138 tests across eight suites.

### Live gate — PASSED via docker-compose (2026-08-16)

Run against `docker compose up postgres-keycloak keycloak`, which already carries the theme bind-mount and a realm selecting `loginTheme`/`emailTheme` = `srw`. Results:

| Check | Result |
|---|---|
| Realm imported, no theme-fallback errors | PASS — `Realm 'srw' imported`, zero `Failed to find … theme` |
| Login page serves our stylesheet | PASS — `/resources/<tag>/login/srw/css/srw.20260816.css` |
| `styles=` parent chain resolves | PASS — keycloak.v2's own `styles.css` served alongside, from our theme's path |
| `.kc-logo-text` logo hook present | PASS — confirms the realm `displayNameHtml` change works |
| **`template.ftl` compiles in real Keycloak** | **PASS** — zero `Failed to template`; the only failure was `MailConnectException`, which is downstream of rendering |
| Branded email captured end to end | PASS — all Travertine tokens present in the delivered HTML |
| Footer uses the corrected `#5a4632`, not `#8a7b66` | PASS — the WCAG fix verified in real delivered mail |
| `ltr` version guard renders | PASS — `dir="ltr"` |
| Plaintext part still present | PASS — multipart intact with only the HTML side overridden |
| Realm displayName reaches Keycloak's own copy | PASS — "your Superhuman Remote Worker account", no message overrides needed |

The discriminator that made this cheap: FreeMarker renders **before** SMTP is contacted, so a template error and a connection error are distinguishable in the log even with no mail catcher running.

**Still unverified** (needs a browser or a production-mode cluster, all failing visibly rather than silently): dark-mode rendering of the login page, the gzip-cache staleness behaviour, and the ConfigMap `items[].path` mount in a real pod — though the latter is now covered by a test binding `items[].key` to rendered ConfigMap keys, and Tilt was independently observed applying the ConfigMap, volume and checksum annotation through the real deploy path.

### Superseded: the original k3d gate

The theme is verified by `helm template`, a server-side `kubectl apply --dry-run`, unit tests, and Tilt applying it through the real deploy path — but never by a running Keycloak serving a login page. The local k3d cluster could not start new pods (cluster-wide egress failure: `dial tcp: lookup registry-1.docker.io`, 8 pods stuck in `Init`, a bare busybox pod also timing out). Unrelated to this work.

**Do not run this gate against `--context main`** — that is the shared dev cluster. Local k3d (`--context k3d-srw -n srw`) or compose only.

**The highest-consequence unverified item is that Keycloak parses `email/html/template.ftl` without a FreeMarker error.** If it does not, *every* Keycloak email silently fails to send — verify-address and password reset included. Nothing in the test suite can catch that; only Keycloak's own renderer can.

`docker-compose.yaml` already carries everything needed: the theme bind-mount is in place and `docker/keycloak/realm-export.json` already selects `loginTheme`/`emailTheme` = `srw`. So:

```bash
docker compose up keycloak postgres-keycloak
# then trigger a forgot-password from the login page
```

FreeMarker renders **before** SMTP is contacted, so the Keycloak log distinguishes a template error from a connection error even with no mail catcher running. Adding a mailpit service to compose would additionally surface the rendered email for visual inspection.

Remaining unverified after that: `items[].key` correctness (now covered by a test binding keys to rendered ConfigMap data), theme resolution, the `styles=` parent chain, the `.kc-logo-text` logo hook, the `:where()` dark-mode claim, and gzip-cache staleness. All fail visibly or benignly; only the FreeMarker path fails silently.

### Follow-ups worth ticketing (found during implementation, deliberately not fixed)

1. **Shell injection on sibling fields of the same Keycloak postStart hook.** `helm/templates/services/keycloak.yaml` interpolates `.Values.keycloak.realm` and `.Values.email.smtp.from` raw into the same double-quoted shell word that this work hardened for `port`/`starttls`. Reproduced: `--set-string 'email.smtp.from=a"; id; echo "'` renders `smtpServer.from=a"; id; echo ""`. Pre-existing; both values are chart-owned, so this is hardening rather than a privilege boundary. The `srw.keycloak.smtp*` helpers in `helm/templates/_helpers.tpl` are a ready template — an anchored address regex for `from`/`envelopeFrom`, `^[A-Za-z0-9._-]+$` for the realm.
2. **`orchestrator/main.py:19187`** — an unguarded `from orchestrator.database.postgres import …` inside the stateless-resume path. Same class as the blocker fixed here: unresolvable in the flattened runtime image, where `orchestrator/` contents are copied into `/app`. Pre-existing.
3. **`orchestrator/init.py`** — module-level `orchestrator.`-prefixed imports make `import init` fail in-container. Reached unwrapped from `orchestrator/security/auth.py:401`; gated on `MCP_DEV_TOKEN`, unset in prod, so dev-only.
4. **The Dockerfile smoke-import guard is build-time only** — inert unless CI actually builds the orchestrator image on the branch.

### A pattern worth remembering

Nine assertions on this feature passed while guarding nothing, in two recurring shapes: a string assertion run against a file whose content it cannot parse (regexing a Helm template's *source* for rendered values), and a presence/absence assertion satisfied by the file's **own comment** containing the literal being checked. The second is the nastier one — writing a precise warning comment is exactly what defeats the test enforcing it.

`tests/test_keycloak_theme_infra.py` now carries `_css_rules()`, `_ftl_directives()`, `_properties_directives()` and `_template_directives()` to strip comments before content assertions. The general lesson: **assert against rendered or parsed output, not source text** — and mutation-test every guard by breaking the thing it names.
