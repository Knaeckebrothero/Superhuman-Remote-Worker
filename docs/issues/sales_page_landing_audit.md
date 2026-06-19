# Sales page — landing-page audit (broken CTAs, missing social card, polish)

**Date:** 2026-05-29
**Status:** Open. Six independent issues; #1 and #2 affect every visitor on the live page now. #3–#7 are polish.
**Component:** Source of truth `index.html` (SRW repo root). Deployed copy in `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml` (the `srw-sales-page-content` ConfigMap, inlined HTML at `data.index.html`). Live at https://superhuman-remote-worker.com/.

> **Update (2026-06-19):** source moved to `website/index.html`; the page now ships as a CI-built nginx image (`ghcr.io/knaeckebrothero/superhuman-remote-worker-website`) rather than an inlined ConfigMap. The "re-indent into the ConfigMap" instructions below are retired — see `docs/superpowers/specs/2026-06-18-helm-config-generator-design.md` §13.

## Summary

End-to-end review of the live landing page after the 2026-05-26 redesign (`c40f468`), favicon add (`62a0034`), and scale-diagram label fix (`ea41619`). The page reads well, copy is consistent with the actual product (config names, citation language, license model), and the visual structure works at desktop widths. But every conversion path on the page is a dead link, the social card is empty, and the mobile nav hides the very link a sales page most wants visible.

The page itself is otherwise polished — issues below are point fixes, not a rewrite.

## What works (so it's not just a list of complaints)

- Visual structure (hero → vignettes → how → cockpit walkthrough → why → scale → run → built-on → footer) reads cleanly top-down.
- Cinzel display + monospace body for mocks gives the page a distinct voice without feeling AI-generated.
- Citation-style hero chat mockup is the strongest single piece — directly shows the product's defining feature.
- Scale diagram now reads correctly after `ea41619` (text no longer overlaps multi-replica boxes).
- Favicon (`62a0034`) renders cleanly at tab sizes on light + dark tab bars.

## Issue 1 — Every CTA on the page is a broken link

**Severity:** Critical (affects every visitor immediately).

Buttons and nav links on the live page all point to domains/paths that don't resolve:

| Link text | href | Status |
|---|---|---|
| Nav: Repo | `https://github.com/superhuman-remote-worker/srw` | 404 (org does not exist) |
| Nav: Docs | `https://docs.superhuman-remote-worker.com` | NXDOMAIN |
| Nav: Sales | `mailto:sales@superhuman-remote-worker.com` | Works iff MX is configured for the domain — currently it's only an A-record for the landing page |
| Hero: Start free trial | `https://cockpit.superhuman-remote-worker.com/signup` | NXDOMAIN |
| Hero: Self-host with Helm → | `https://github.com/superhuman-remote-worker/srw/blob/main/helm/README.md` | 404 |
| Run lane: Start free trial | (same as hero) | NXDOMAIN |
| Run lane: See pricing → | `https://cockpit.superhuman-remote-worker.com/pricing` | NXDOMAIN |
| Run lane: Read the Helm guide | (same as hero Helm link) | 404 |
| Run lane: Get a license → | `mailto:sales@...` | Same as nav Sales |
| Footer: Repo | (same as nav Repo) | 404 |
| Footer: Helm guide | (same as hero Helm link) | 404 |

**Why it matters:** the page is up but unactionable. A visitor who clicks anything other than the email link hits a DNS error or a 404. The "Start free trial" button is the primary conversion path and goes nowhere.

**Fix:** decide what each link should point to, then update both copies in `index.html` (some appear twice — hero + run lane, nav + footer) and propagate via the existing flow (re-indent root `index.html` into the ConfigMap's `data.index.html` block scalar, push to HomeLab, Fleet sync, rollout-restart the pod; full procedure in the `project_sales_page_deploy` memory). Options per link:

- **Repo + Helm guide:** point to the real repo (the closed-source SRW repo can't be public, so probably the public homelab Gitea mirror, or the planned OSS-split agent repo per [project_agent_oss_split](../features/agent_open_source_split.md)). If neither is ready, remove these links from the page rather than 404 visitors.
- **Docs:** if a docs site isn't deployed yet, drop the nav link.
- **Start free trial / See pricing:** until the hosted product exists, replace both CTAs with either (a) a single "Get notified" mailto with subject prefilled, or (b) a Calendly / Tally form. Don't ship a button that goes nowhere.
- **Sales mailto:** confirm MX records exist for `superhuman-remote-worker.com` and that `sales@` actually routes somewhere. If not, point to a working address (e.g., `overlygenericaddress@pm.me` or a forwarder) — but be aware Cloudflare's email-obfuscation script bakes the encoded address into the HTML (see Issue 7), so changing it requires regenerating the `data-cfemail` token too.

## Issue 2 — `og:image` is missing → blank social previews

**Severity:** Important.

`<head>` currently declares:

```html
<meta property="og:title" content="SRW — Self-hosted AI workforce platform">
<meta property="og:description" content="Specialist AI agents for the research, accounting, compliance, and code work that drains your team. Self-hosted in your cluster, audited, cited.">
```

But no `og:image`, no `twitter:card`, no `og:url`, no `og:type`. When the URL is shared on LinkedIn, Slack, Discord, Twitter, etc., the preview shows the title + description but no image — making the link visually much weaker than competitors' previews.

**Fix:**

1. Create a 1200×630 PNG social card. Easiest route: render the existing landing-page hero (title + tagline + SRW mark, on the dark `--bg`) into a static PNG via headless chrome with `--window-size=1200,630 --screenshot`. Or design a dedicated card with just the SRW mark + "Hand off the paperwork. Keep your team." centered.
2. Inline as a base64 data URI (consistent with how the favicon is handled — keeps the page a single self-contained ConfigMap entry) OR add as a second key to the ConfigMap and serve via a second nginx mount. Data URI is simpler; 1200×630 PNG at decent quality is ~80–150 KB base64 — meaningful page-weight bump, but only one extra round-trip-saved file.
3. Add the meta tags:

```html
<meta property="og:image" content="...">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="https://superhuman-remote-worker.com/">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="SRW — Self-hosted AI workforce platform">
<meta name="twitter:description" content="Specialist AI agents for the research, accounting, compliance, and code work that drains your team.">
<meta name="twitter:image" content="...">
```

(For a base64 data URI in `og:image`, support is inconsistent — LinkedIn and Twitter generally want a fetchable URL. So this argues for serving the PNG as a real file from nginx, not inlining. Means adding a second ConfigMap key + a directory mount or a second `subPath` mount in `10-deployment.yaml`.)

## Issue 3 — Nav hides "Sales" link on mobile but keeps Repo + Docs

**Severity:** Important (sales-page UX bug).

Current CSS (`index.html`, in the `@media (max-width:760px)` block):

```css
.nav .links a:nth-child(3){display:none}
```

The third link is **Sales** (`Repo`, `Docs`, `Sales` order). On any viewport ≤ 760px wide — i.e. every phone — the page hides its own contact CTA while keeping Repo and Docs.

**Fix:** decide what to drop on mobile based on what a mobile visitor most needs (almost certainly Sales > Docs > Repo). Cleanest version:

```css
/* keep Sales visible on mobile; drop Docs first */
.nav .links a:nth-child(2){display:none}
```

Or drop both Repo and Docs and keep only Sales:

```css
.nav .links a:nth-child(1),
.nav .links a:nth-child(2){display:none}
```

## Issue 4 — Hero chat mockup orphans "DPIA:" on its own line

**Severity:** Polish.

The hero chat mockup says:

> Read Q1 decision logs, audit policy v3, and the March DPIA:

At desktop width (1360 px) this wraps with just "DPIA:" alone on line 2, which reads like a stray line break rather than intentional wrapping. The bubble is 92% of a 460 px-max-width container, so it's not a CSS bug — it's that the sentence happens to break right before the last word.

**Fix:** rephrase so the last word doesn't orphan. E.g.:

- "Read Q1 decision logs, the audit policy, and March's DPIA:"
- "Read Q1 decisions, audit policy v3, and the March DPIA:" (shorter — likely fits on line 1)
- Or wrap "the March DPIA" in `<span style="white-space:nowrap">` so the break happens before "the" instead of before "DPIA:".

## Issue 5 — Scale-diagram tick marks are too subtle

**Severity:** Polish (self-criticism on the 2026-05-28 fix `ea41619`).

The fix that replaced overlapping multi-box rows with single-box-plus-tick-marks (Stage 2 agent, Stage 3 orch, Stage 3 agent) used `opacity="0.6"` and 6 px-tall ticks. The result: the multiplicity cue is now barely perceptible at normal viewing distance. The "× 3" / "× N" labels carry the meaning, but the visual replication cue — which was the entire point of the multi-box design — has effectively disappeared.

**Fix options** (pick one):

- **Bump opacity to 0.85 and tick length to 8 px** — keeps the design intent, still doesn't overlap text (text spans y=119–131, ticks at y=108–116 and y=132–140).
- **Accept the labels do all the work** — drop the ticks entirely. The page reads cleanly; subdivisions are textual only.
- **Replace ticks with a faint vertical dashed line through the full box height, drawn under the text** — needs careful color choice so glyphs stay legible.

Recommended: option 1 (bump opacity + length). Lowest-risk improvement.

## Issue 6 — No pricing on the page; "See pricing →" link is dead

**Severity:** Content gap (related to Issue 1).

The Run section says "See pricing →" but there's no price info anywhere on the page and the link goes nowhere (Issue 1). A visitor evaluating the product needs at least a ballpark before they'll write to sales.

**Fix:** add a pricing block (2–3 tiers, even if rough), or replace "See pricing" with "Talk to sales →" so expectations match reality. Until then this CTA is worse than no CTA — it implies a pricing page exists and the visitor finds nothing.

## Issue 7 — Smaller things (not worth their own sections)

- **"K8S FLEET" stage title** in the scale diagram sits at `x=730` while the outer-box visual center is `x=740`. Visible 10 px offset compared to the other two stage titles. Trivial fix: `<text x="740" ...>K8S FLEET</text>`.
- **Cloudflare email obfuscation in HTML.** The contact email is encoded with Cloudflare's `data-cfemail` attribute + a CF script reference (`/cdn-cgi/scripts/.../email-decode.min.js`). It only resolves when served *through* Cloudflare — direct-from-nginx (e.g. the `kubectl exec curl localhost` verification during the favicon deploy) shows the literal placeholder `[email protected]`. Not breaking anything today since live traffic goes through CF, but bakes a CF dependency into the source. Consider just writing the `mailto:` plainly or using a simpler obfuscation (e.g. JS-decode).
- **Hero h1 uses hard `<br>`** for the line break ("Hand off the paperwork.<br>Keep your team."). If the headline ever changes length, the break will wrap awkwardly. `text-wrap: balance` on `.hero h1` would handle this automatically:
  ```css
  .hero h1{margin:0 0 22px;text-wrap:balance}
  ```
  and drop the inline `<br>`.
- **No `og:url`, `og:type`, or `twitter:card`** — folded into Issue 2's fix.
- **No favicon raster fallback** (decided 2026-05-26 — SVG-only is fine for current browsers; flagged here for completeness in case very-old-browser support becomes a requirement).

## Suggested order of work

1. **Issue 1** (broken CTAs) — biggest single visitor-experience win; no design work, just a decision on what each link should point to.
2. **Issue 2** (og:image + meta) — required before sharing the link anywhere public.
3. **Issue 3** (mobile nav) — one-line CSS fix.
4. **Issue 4** (DPIA orphan) — one-word copy edit.
5. **Issue 5** (tick opacity bump) — two-attribute SVG edit.
6. **Issue 6** (pricing) — depends on product readiness; until then fold into Issue 1's "See pricing → Talk to sales" rewrite.
7. **Issue 7** (smaller things) — opportunistic.

All edits go in source `index.html`, propagated by re-indenting it into `HomeLab/.../10-deployment.yaml`'s `data.index.html` block scalar (4-space indent, blank lines stay empty), push to HomeLab `main`, wait for Fleet sync, then `kubectl rollout restart deploy/srw-sales-page -n srw-sales-page` — the subPath mount won't pick up the new ConfigMap without a pod restart. Full procedure in the `project_sales_page_deploy` memory.
