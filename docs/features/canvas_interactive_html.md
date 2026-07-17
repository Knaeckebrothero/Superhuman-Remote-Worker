# Canvas Slice 1.5 — Interactive Static HTML Renderer

**Status:** PROPOSED (2026-07-17)
**Parent:** `docs/features/dynamic_canvas.md` (authority for the Canvas
architecture; this doc adds one renderer mode and changes nothing about
pointers, storage, or the gateway).

## Problem

The Slice-1 file stage renders HTML through a sanitize-everything pipeline:
DOMPurify with `script`, `style`, `button`, `form`, `input`, `iframe`, `link`,
`svg` in `FORBID_TAGS`, every `src` stripped, `href` kept only for
same-document `#anchors`, and only size-capped inline `style="…"` attributes
surviving (`cockpit/src/app/views/canvas/canvas-rendering.ts:501`). That is the
right default for untrusted HTML, but it means the file stage cannot show the
single most common "agent built something visual" artifact: a self-contained
interactive HTML page (mockup, widget, small game, d3 visualization).

Observed failure (dev session `b1758f38`, 2026-07-17): an agent presented a
tabbed mockup showcase via `set_canvas` and the user saw three lines of bare
text — tab labels with the tab script stripped and a dead link. The tool call
returned success, so the agent confidently announced an "interactive mockup
showcase" it never had. Two compounding gaps:

1. **No interactive tier below the live-app viewer.** The design doc's current
   position is binary: "a strict static HTML file is self-contained and
   script-free; anything needing JavaScript … is a gated one-port
   `workspace_port`." But `workspace_port` requires a real workspace, the
   canvas gateway, and the wildcard edge. A lite (virtual/none) session has
   none of those — and lite sessions are exactly where a cheap visual stage
   matters most.
2. **The constraints are invisible to the model.** They live only in
   `config/skills/present-with-canvas/SKILL.md`; the `set_canvas` docstring
   (`src/tools/canvas/__init__.py:51`) says nothing, and the sanitizer strips
   silently, so the agent cannot self-correct.

## Proposal

Add an **explicitly requested** renderer mode, `html-interactive`, that renders
a self-contained HTML file with scripts and styles intact inside the existing
`srcdoc` iframe — using **isolation instead of sanitization** as the defense.
This is the claude.ai-Artifacts trust model: `sandbox="allow-scripts"`
*without* `allow-same-origin`, so the document runs on an opaque origin with no
cookies, no storage, no parent DOM, and no cockpit credentials, under an
injected CSP that blocks all network egress.

Strict static HTML (`html`) stays the default and the only thing `auto` can
pick. `html-interactive` is opt-in by the agent, per presentation.

What this deliberately does NOT cover (stays `workspace_port` / Slice 4):
multi-file sites (sibling assets still never resolve), anything needing a
backend or network, client routing, WebSocket/SSE. The agent contract for this
mode is "one file, everything inlined."

## Design

### Renderer plumbing (no migration)

- `orchestrator/services/canvas.py:51` — extend
  `CanvasRenderer = Literal["auto", "markdown", "text", "html", "image"]` with
  `"html-interactive"`. The DB column is `VARCHAR(32)` with no CHECK
  (`migrations/app/0058_canvases.sql:17`), so no migration.
- `orchestrator/services/canvas_files.py` — validation: `html-interactive`
  accepts exactly the byte/MIME classes `html` accepts (decodable UTF-8 text
  with HTML-compatible extension/content rule). The `auto` detection path
  (`canvas_files.py:483`) must never resolve to it.
- Editability parity with `html` (source-text editing via Monaco): add
  `html-interactive` to the editable-renderer sets at
  `orchestrator/services/canvas.py:152` and `orchestrator/routers/canvases.py:1367`.
- Raw-content download endpoint keeps `Content-Disposition: attachment` for
  both HTML renderers (`orchestrator/routers/canvases.py:844`) so raw bytes
  never render on the API origin.
- Agent tool (`src/tools/canvas/__init__.py`): add the value to the `renderer`
  Literal in **both** `_SetFileCanvasArguments` and `_SetLiveCanvasArguments`.

### Cockpit renderer

New `CanvasInteractiveHtmlRendererComponent` beside
`CanvasHtmlRendererComponent` (`canvas-renderers.component.ts:137`), and a new
`renderCanvasInteractiveHtml()` in `canvas-rendering.ts`:

- **No DOMPurify pass.** Sanitizing JavaScript is meaningless; the sandbox and
  CSP are the boundary. (Keep the existing 4 MiB source cap,
  `CANVAS_RENDER_MAX_OUTPUT_CHARS`; drop the node/style budgets — they exist to
  bound the sanitizer, which this mode doesn't run.)
- **Policy-before-content injection.** Parse the source with `DOMParser`
  (inert — scripts do not execute during parsing), create `<head>` if absent,
  insert the CSP `<meta>` and `<meta name="referrer" content="no-referrer">` as
  the **first** head children, serialize back. String-prepending is not enough:
  a meta CSP only governs content after its insertion point, and the source is
  a full attacker-authored document.
- **Iframe attributes** (mirrors the live-app iframe posture at
  `dynamic_canvas.md` §"Live-app iframe", minus the same-origin grant):

  ```html
  <iframe sandbox="allow-scripts" loading="lazy" referrerpolicy="no-referrer"
          allow="camera 'none'; microphone 'none'; geolocation 'none';
                 clipboard-read 'none'; clipboard-write 'none'"
          [title]="title()" [srcdoc]="srcdoc()"></iframe>
  ```

  No `allow-same-origin`, `allow-forms`, `allow-popups`, `allow-modals`,
  `allow-downloads`, or `allow-top-navigation*`.
- **Injected CSP** (`INTERACTIVE_HTML_CSP`):

  ```text
  default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src data:; font-src data:; media-src data:; connect-src 'none';
  form-action 'none'; base-uri 'none'; object-src 'none'; child-src 'none';
  frame-src 'none';
  ```

  `data:` URIs for images/fonts/media are allowed (unlike strict static): the
  resource-bomb concern is already bounded by the 4 MiB source cap, and inlined
  `data:` assets are how a self-contained artifact carries images. External
  URLs of any kind stay dead.

### Security model

| Vector | Defense |
|---|---|
| Cookies / storage / cockpit credentials | Opaque origin (`sandbox` without `allow-same-origin`) — no cookie jar, no localStorage, no credentialed fetch |
| Parent DOM / cockpit page | Cross-origin + sandbox; `frame-ancestors 'none'` already on cockpit (`docker/cockpit-nginx.conf:9`) |
| Network exfiltration (fetch/XHR/beacon/img/css) | Injected meta CSP `connect-src 'none'` + `default-src 'none'` + `data:`-only asset sources |
| Form submission | No `allow-forms` + `form-action 'none'` |
| Popups / top navigation / downloads / modals | Sandbox flags absent |
| Nested iframes | `child-src 'none'; frame-src 'none'` |
| **Self-navigation egress** (`location.href = attacker-URL`, `<meta refresh>`) | **Not** blocked by document CSP (`navigate-to` never shipped). Frame navigations are checked against the *embedding* document's `frame-src` → Slice C below adds a cockpit page-level CSP with a `frame-src` allowlist. Until Slice C lands, this is the one open egress channel of the mode — acceptable on the single-user dev posture, a launch gate for multi-tenant (same framing as the live viewer's `frame-ancestors` gate) |
| CPU/memory burn (`while(true)`) | Accepted, Artifacts-standard; frame is torn down on clear/replace; OnPush component keeps cockpit responsive |

Note the asymmetry with the live viewer: the live app gets
`allow-same-origin` and therefore *needs* the sacrificial registrable origin
(`*.srwcanvas.works`) and the gateway. This mode never gets same-origin, which
is precisely why it can run in the cockpit's own `srcdoc` with zero
infrastructure — no gateway, no edge DNS, no workspace. It works in lite
sessions.

### Guidance (the other half of the bug)

- `set_canvas` docstring gains one constraint sentence covering both HTML
  modes, e.g.: *"HTML renderers show one self-contained file: `html` strips
  scripts/styles/forms; `html-interactive` runs inline scripts in a locked
  sandbox with no network — inline everything, reference no external or
  sibling resources."*
- `config/skills/present-with-canvas/SKILL.md` gets the three-tier decision
  rule: strict static (documents) → `html-interactive` (self-contained
  interactive artifact) → `workspace_port` (multi-file / backed / networked
  apps).
- **Set-time advisory (server):** when the validated file contains `<script`
  or `<link` and the resolved renderer is strict `html`, the `set_canvas` tool
  result includes an advisory line ("scripts/styles will be stripped; use
  renderer=html-interactive for a self-contained interactive page"). This
  fixes the silent-strip failure for agents that never load the skill.

## Implementation slices

- **A — plumbing (server + agent):** enum value through
  `services/canvas.py`, `canvas_files.py` validation (+ never-auto),
  editability sets, attachment disposition, agent tool Literals + docstring.
  Tests: canvas service unit tests (accept/reject, auto never picks it,
  editable parity), tool schema test.
- **B — cockpit renderer:** `renderCanvasInteractiveHtml()` (DOMParser CSP
  injection, byte cap) + `CanvasInteractiveHtmlRendererComponent` + renderer
  switch wiring + specs (CSP is first head child even for headless/hostile
  sources; scripts preserved; 4 MiB rejection; sandbox attribute exact-match
  assertion).
- **C — navigation egress hardening (cockpit page CSP):** add a full
  `Content-Security-Policy` on the cockpit document (`docker/cockpit-nginx.conf`)
  including a `frame-src` allowlist (self + canvas gateway viewer origins +
  IDE proxy origin — needs a one-time inventory of every iframe the cockpit
  legitimately embeds). Shippable independently; also benefits the live
  viewer. Required before `html-interactive` is exposed to non-owner users.
- **D — guidance:** skill + docstrings + set-time advisory.

A + B + D make the mode usable on the current single-user dev posture; C is
the multi-tenant gate.

## Acceptance criteria

1. Agent presents a single-file HTML page with inline `<style>`, `<script>`,
   and a `data:` image via `set_canvas(renderer="html-interactive")`; the
   cockpit shows it styled and the script-driven interaction (e.g. tabs) works.
2. The same file via `renderer="html"` still renders text-only (strict mode
   unchanged), and the tool result carries the Slice-D advisory.
3. `renderer="auto"` never yields `html-interactive` for any input.
4. Inside the frame: `fetch()` to any external host rejects (CSP);
   `document.cookie` throws/empty (opaque origin); `window.parent` access
   throws (cross-origin); `window.open` returns null; form submit blocked.
5. `editable=true` works with source-text editing exactly as it does for
   strict `html`.
6. A lite (virtual) session — no workspace pod, no gateway — can present an
   interactive page end-to-end.
7. After Slice C: `location.href = "https://example.com"` from inside the
   frame is blocked by the cockpit `frame-src` policy.

## Open questions

- Cap `data:` asset count/size separately from the 4 MiB source cap, or is the
  source cap sufficient? (Lean: sufficient.)
- Should the planned markdown-iframe move (pre-launch hardening item 3 in
  `dynamic_canvas.md`) reuse the same DOMParser-inject + srcdoc shell? (Lean:
  yes — one shared "isolated document" helper.)
- Naming: `html-interactive` (chosen here; sorts beside `html`) vs
  `interactive_html`.
