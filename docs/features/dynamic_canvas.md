---
tags:
  - feature
  - cockpit
  - agent-tool
  - collaboration
aliases:
  - dynamic component
  - shared artifact
  - artifact panel
  - canvas
related:
  - "[[shared_browser]]"
  - "[[notify_user_tool]]"
  - "[[persistent_chat_ui_redesign]]"
  - "[[vm_snapshots_and_ide]]"
  - "[[workspace_network_policy_unification]]"
---

# Feature: Dynamic Canvas (Shared Artifact Surface)

A shared, bidirectionally-editable surface attached to a job (or persistent thread) where the agent can drop arbitrary typed content — text, HTML mockups, code, diagrams, an embedded live page — and the user sees the rendered result in the cockpit alongside the chat. Both sides can edit; both sides can read what the other did.

**Status:** Concept / brainstorm. No implementation yet.

## Motivation

Today the agent has narrow ways to show the user *a thing*:

- **Files** — via Gitea or the Web IDE (`vm_snapshots_and_ide.md`). Requires the user to navigate elsewhere; HTML mockups need a running webserver inside the workspace before they're viewable.
- **Chat messages** — text only. No live HTML preview, no editable surface.
- **Shared browser** — covered in `shared_browser.md`. Useful only when the agent is actively driving a Chromium session; it shows what the agent sees, not what the agent *made*.

There's no slot in the cockpit for *"here is the artifact I made for you, take a look, edit it if you want."* A designer expert wanting to show a mockup has to commit HTML to the repo, the user has to clone or open the IDE and start a server, and the iteration loop gets long. A writer expert handing back a draft has the same problem in reverse: the user can read it in chat but can't edit it without leaving the cockpit.

There's also no slot for *"here is the thing you asked about, summoned into your view."* If the user asks "how are the chickens doing?" and the agent finds the home video stream, there's nowhere to put the live feed except a chat link the user has to click. Same for matplotlib output the agent generated, a live monitoring panel it discovered, a generated SVG diagram — all of these get shoved into chat as text, file references, or screenshots, when they should be a tile the user can see, resize, and keep open while the conversation continues.

Claude's Artifacts is the obvious analogue for the *built-by-agent* case; Gemini's Dynamic UI is the closer analogue for the *summoned-into-view* case. ChatGPT Canvas, Cursor Composer, and Vercel v0 are variations on the same theme. The cockpit doesn't have any of this yet.

## What It Is

A typed content surface, rendered in the cockpit, writable by both the agent (via tools) and the user (via the cockpit UI). Each canvas holds one piece of content with a declared **kind**; the cockpit picks the renderer based on kind:

| Kind | Render | User-editable |
|---|---|---|
| `markdown` / `text` | Markdown viewer with source toggle | Yes |
| `html` | Sandboxed `<iframe srcdoc>` for mockups; HTML + JS + workspace-local SQLite is sufficient for surprisingly rich prototypes | Yes (source) |
| `code` (any language) | Monaco/CodeMirror | Yes |
| `svg` / `mermaid` | Inline render | Yes (source) |
| `image` | Static rendered output (matplotlib PNG, screenshots, generated graphics); auto-refresh on agent update | No |
| `url` | Iframe pointing at a workspace-served origin via authenticated proxy. Covers live video streams, ephemeral dev servers, designer-mockup apps, plotting dashboards | No (target is external) |
| `shared_browser` | The CDP screencast widget from `shared_browser.md` | Take-control handoff |

A job/thread can have multiple canvases laid out in a **dashboard-style grid** (see Delivery Modes below). The agent decides what to put in each tile.

## Delivery Modes

Two ways the canvas reaches the user. Both are supported; they're different rendering targets for the same underlying content.

**A. In-cockpit grid component (primary).** The cockpit gains a configurable grid surface — think dashboard tiles — where each canvas is one tile. The agent requests a tile, names it, and updates its content. The user resizes, rearranges, closes, and edits in place. This is the everyday mode: tight feedback loop with chat, no context switch out of the cockpit.

**B. External proxy URL (secondary).** For canvases that benefit from a full browser window — designer mockups with their own client-side routing, multi-page flows, viewport-sensitive layouts, devtools work — the orchestrator publishes a signed short-lived URL that opens the canvas content in a regular browser tab. The user gets real resize, real devtools, full browser features. Maps onto the iframe-embeddable viewer pattern sketched at the bottom of `shared_browser.md`.

The agent (or the user) decides per-canvas whether to expose the proxy URL. Both modes use the same backing content; switching is just a different render target.

### Why both, instead of in-app only

An earlier version of this idea proposed in-cockpit rendering only, on the theory that keeping the surface inside the application would make it harder for a misaligned or compromised agent to phish the user (e.g., serving an HTML page that mimics the user's bank login or the cockpit's own login chrome to steal credentials and escape the harness).

That security argument doesn't actually hold. The in-cockpit grid renders arbitrary HTML provided by the agent, so a phishing-style page is just as renderable inside a tile as it is in an external tab. The in-app/external boundary doesn't change the threat model in any meaningful way. Given that, simplicity and usability win: ship both modes, and address the phishing risk at the layers that can actually mitigate it (see Security Considerations).

## Security Considerations

Both delivery modes can serve agent-authored HTML, JS, and (in the proxy case) entire backends running in the workspace. Phishing is a real but bounded risk: a misaligned agent could render a page that mimics a credential prompt, hoping the user types into it.

In-cockpit rendering does not eliminate this — the grid tile renders whatever HTML the agent provides. So the in-app/external choice is not a security boundary. The actual mitigations live elsewhere:

- **Sandboxed iframe with strict CSP** for `html` content. No top-level navigation, no form submission to non-allowlisted origins, no parent-window access. Even if the agent writes a fake login form, it can't post the credentials anywhere reachable.
- **Persistent canvas chrome** the agent cannot remove or visually override — a header strip, a colored border, an "agent-generated content" pill — so the user has a stable visual signal that anything inside the tile is untrusted.
- **Real auth flows live outside any canvas**, in the cockpit's Keycloak chrome. Credentials are never collected inside a canvas tile, ever. Same rule for proxy-mode pages: the proxy never relays cookies or auth headers from the cockpit's session.
- **Proxy-mode origins are isolated.** Each proxy URL gets its own subdomain or path scope, sandboxed from the cockpit origin so any escape attempt cannot read cockpit cookies/local storage.
- **Outbound network policy** for canvas-served HTML — only allowlisted origins reachable, no surprise calls to attacker-controlled servers.

The grid-component mode and the proxy mode share these mitigations; neither is meaningfully safer than the other.

### Concrete sandbox configuration (in-cockpit)

The cockpit, *not* the agent, emits the meta CSP as the first child of `<head>` ahead of any agent-authored content. The agent never controls the CSP.

```html
<iframe
    sandbox="allow-scripts"
    referrerpolicy="no-referrer"
    loading="lazy"
    srcdoc="<!doctype html>
<meta http-equiv='Content-Security-Policy' content=&quot;default-src 'none';
script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:;
font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none';
frame-ancestors 'none';&quot;>
... agent content ...">
</iframe>
```

Why each piece:

- `sandbox="allow-scripts"` only. **Never `allow-same-origin` together with `allow-scripts`** — the iframe can mutate its own sandbox attribute and reload, escaping. `allow-forms` omitted at the sandbox layer; `form-action 'none'` is defense-in-depth.
- `connect-src 'none'` blocks `fetch`, `XHR`, `WebSocket`, `EventSource` — no covert channels back.
- `script-src 'unsafe-inline'` is acceptable here because the origin is opaque and CSP blocks all network fetches; the script can only manipulate local DOM.
- `img-src data: blob:` lets the agent embed encoded assets without opening external image fetch as a side channel.
- No `allow-top-navigation`, no `allow-popups` — phishing redirects blocked at the sandbox layer.

Cockpit document headers (set on the Angular server response):

```
Content-Security-Policy: frame-ancestors 'self'; ...
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Embedder-Policy: require-corp
```

`postMessage` listeners on the cockpit must check `event.source === iframe.contentWindow` AND `event.origin === 'null'` (opaque). Never trust message data shape.

### Concrete proxy-mode isolation (mode B)

For canvases opened in a regular browser tab via signed URL, **separate registrable domain is mandatory**, not optional. The pattern is the one used by Sandpack/csb.app, Vercel preview URLs, and `*.googleusercontent.com`:

- Cockpit on `cockpit.example.com`. Canvases proxy on `*.canvas.example-userland.com` — different eTLD+1, so cookies and storage are isolated by browser-enforced same-origin policy, not by application logic.
- Per-canvas signed token reusing the existing `srw_*` MCP-token scheme (`orchestrator/main.py:11558-11654`) with a new `canvas:{canvas_id}` scope and short TTL (~15 min). URL: `https://c-{canvas_id}.canvas.example-userland.com/?t={token}`. On first request, exchange for a session cookie scoped to that subdomain only.
- Proxy strips inbound `Cookie` headers; never forwards Keycloak auth.
- Proxy response headers include `Content-Security-Policy: frame-ancestors 'self' https://cockpit.example.com; sandbox allow-scripts allow-forms; form-action 'self'; connect-src 'self'`.
- Eventually add the proxy domain to the Public Suffix List to prevent cookie bleed between sibling canvases.

## Use Cases

- **Designer expert** authors a mockup with HTML + JS + (workspace-local) SQLite, served by an ephemeral preview server in the workspace; renders in-canvas, or pops out to a full browser tab via the proxy mode (delivery mode B) for serious tinkering with real devtools and real resize.
- **Writer expert** drafts a document section in the canvas; user edits inline; agent sees the edited version next turn.
- **Coder** sketches a function signature or interface for review before implementing across the codebase.
- **Researcher** assembles a comparison table the user can reorder or annotate.
- **Diagram-heavy phases** — Mermaid renders inline; user tweaks labels, agent picks up the change.
- **Persistent thread** — canvas becomes a standing scratchpad for the session, not a one-shot artifact.
- **Ambient information** — user asks "how are the chickens doing?", agent locates the home/farm video streaming server and renders a live feed in a canvas tile. Same pattern for any live data the user has access to: cameras, sensor dashboards, monitoring panels.
- **Data analysis** — agent runs matplotlib (or seaborn, plotly, etc.) in the workspace and pushes the rendered chart into an `image` tile. User can ask for a different cut and the tile updates in place.
- **Custom graphics** — agent hand-rolls SVG / canvas / WebGL HTML for one-off explanations the standard renderers don't cover (e.g., an architecture diagram with interactive hover, a custom chart type matplotlib doesn't ship).

## Agent Tool Surface

Adopt Anthropic's Artifacts pattern: a **single `canvas` tool** with a `command` enum. One tool slot in the prompt, fewer LLM coordination errors than a multi-tool surface, and `update` semantics reuse the same `str_replace` model the agent already uses for files.

```python
canvas(
    command: Literal["create", "update", "rewrite"],
    canvas_id: str,                   # agent-chosen slug, scoped to the job/thread
    kind: Literal["markdown", "html", "code", "svg", "mermaid", "image", "url", "shared_browser"],
    title: str | None = None,
    content: str | None = None,       # for create / rewrite
    old_str: str | None = None,       # for update — must appear exactly once in current content
    new_str: str | None = None,       # for update
    language: str | None = None,      # for `code` kind
    layout_hint: dict | None = None,  # cols/rows hint for grid placement
)
```

- `create` errors if `canvas_id` already exists in scope. Use `rewrite` to replace.
- `update` is `str_replace`: `old_str` must match exactly once in current content. On miss (e.g., user edited since the agent's last read), the tool returns `{conflict: true, current_content: ...}`; the agent re-reads and retries. Identical contract to the Anthropic [text-editor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool) — the agent already understands it.
- `rewrite` is full replacement; no `old_str`/`new_str`. Use for atomic regenerations.

**No `canvas_read` tool.** Canvas state is auto-injected into the agent's context each turn (see Persistence Model → Awareness). When the agent needs current full content, it uses the existing workspace-file read tools — the canvas is just a file.

**Trigger criteria live in the tool description**, not in code-level gating. Mirror the Anthropic Artifacts pattern: substantial content, self-contained, user likely to view/iterate. Avoid for short snippets, single-fact answers, or content that belongs in chat. Per-persona enable/disable in `config/experts/*.yaml` — designer/scholar/coder yes, critic/curator no.

Phase-restriction: `canvas` registered for **both** strategic and tactical phases. Strategic plans may need to set up canvases ("here's the table I'll fill in"); tactical execution updates them.

## Persistence Model

**Decision: workspace files are the source of truth; PostgreSQL holds metadata only.**

Reasons:
- The agent's editing model is already `read_file` / `write_file` / `str_replace`. Canvas writes become writes to `workspace/canvases/{canvas_id}.{ext}`, no new tool semantics.
- Versioning is free via the Gitea-backed workspace repo.
- MCP exposure and sharing reuse the existing workspace-file APIs.
- Files-as-truth matches the rest of the system; introducing a parallel DB-backed content store would create two sources to keep in sync.

### Layout

```
workspace/
└── canvases/
    ├── chicken-stream.url        # one line: workspace-internal URL
    ├── user-flow-mockup.html
    ├── data-analysis.png         # matplotlib output
    └── _index.yaml               # ordered list of open canvases for the grid
```

### Metadata table

`orchestrator/database/migrations/app/0002_canvases.sql`:

```sql
CREATE TABLE canvases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    thread_id UUID REFERENCES threads(id) ON DELETE CASCADE,
    canvas_id VARCHAR(64) NOT NULL,           -- the agent-chosen slug
    kind VARCHAR(20) NOT NULL CHECK (kind IN
        ('markdown','html','code','svg','mermaid','image','url','shared_browser')),
    title TEXT,
    file_path TEXT NOT NULL,                  -- relative to workspace root
    layout JSONB DEFAULT '{}',                -- { x, y, cols, rows, owner_state }
    version INTEGER NOT NULL DEFAULT 1,       -- for optimistic locking (see Concurrency)
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    CHECK ((job_id IS NOT NULL) OR (thread_id IS NOT NULL))
);

CREATE UNIQUE INDEX idx_canvases_scope
    ON canvases (COALESCE(job_id, thread_id), canvas_id);
```

`updated_at` is driven by the existing `update_updated_at_column()` trigger pattern (`migrations/app/0001_initial.sql:1061-1106`).

### Awareness (context injection)

The agent learns about open canvases through the same transient-injection pattern used for todos and memories today (`src/core/workspace_injection.py` → injected each turn from `src/graph.py`). Add a new injection block:

```
<active_canvases>
- chicken-stream (kind=url, title="Coop Cam", last_updated=2m ago, owner=user_editing)
- user-flow-mockup (kind=html, title="Onboarding Flow v3", last_updated=8s ago, owner=agent_writing)
</active_canvases>
```

~30-50 tokens per canvas, index only — never the full content. Cheap, always fresh, rides existing infrastructure. The agent reads full content via the existing file-read tool when it actually needs to edit.

## Concurrency

**Decision: turn-based with optimistic locking. No CRDTs.**

The structural argument: LangGraph's `audited_tool_node` runs tools to completion atomically — there is no agent process typing into the canvas mid-tool-call. User edits happen between agent turns, when no agent process exists to conflict with. The narrow race is "agent is mid-phase, user is editing previous content"; a typical phase is 1–10 seconds.

CRDTs (Y.js, Automerge) solve continuous coexistence — which we don't have. The known weak spot in production AI canvases (ChatGPT Canvas, Claude Artifacts) is "model loses track of user edits in long docs"; CRDTs don't fix that, current-state context injection does.

**Mechanism:**

1. The DB row's `version` integer increments on each write.
2. `canvas update` checks current content against `old_str`. Miss = user edited since last read. The tool returns `{conflict: true, current_content: "..."}`; the agent decides — re-read, merge intent, retry — or abandon.
3. The cockpit blocks user edits while the canvas's `owner_state = agent_writing`. Tile chrome dims, edit controls disabled, status pill reads "agent is editing." When the tool result returns, the cockpit clears the lock.
4. Tile chrome shows owner state at all times: a small label ("agent is editing" / "you are editing" / "synced 2s ago") plus a colored border tied to the lock.

This is what Claude Artifacts and ChatGPT Canvas actually do once you look closely, even though the marketing language ("collaborative editing") suggests otherwise.

**Streaming writes are out of scope for v1.** Token-level streaming would entangle canvas plumbing with the LLM streaming pipeline. Vercel paused their AI-SDK RSC `streamUI` for these reasons; assistant-ui's "atomic tool args + `addResult` feedback" is the model we're matching. Atomic writes per tool call land fast enough — the cost is one round trip per write, not perceived latency.

## Industry Context

Comparable production AI canvas/artifact features mostly converge on the same pattern. Worth knowing what each got right, where each broke, and what to copy.

**Claude Artifacts** is the closest analogue and the most-documented (system prompt reverse-engineered in the [dedlim gist](https://gist.github.com/dedlim/6bf6d81f77c19e20cd40594aa09e3ecd)). One `artifacts` tool with `command ∈ {create, update, rewrite}` and a `type` MIME enum (`text/markdown`, `application/vnd.ant.code`, `text/html`, `image/svg+xml`, `application/vnd.ant.mermaid`, `application/vnd.ant.react`). Update uses `str_replace` semantics — the same model affordance as the Anthropic [text-editor tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool). Server-side state keyed by id. Triggering criteria ("substantial, self-contained, user likely to iterate") live in the system prompt. Iframe sandbox forbids browser storage APIs. v2 added version branching with 90-day retention.

**ChatGPT Canvas** has a narrower kind set (documents, code) and an interaction model centered on highlight-to-edit: when the user highlights a span, that selection is fed to the model as the focus region. Without a highlight, the model defaults to rewrites. The published weak spot ([community thread](https://community.openai.com/t/assistant-cannot-see-and-access-my-edits-in-canvas/1155732)): the model frequently doesn't see user edits in long docs. **Lesson: bidirectional editing's hard part is *the model noticing user edits*, not the editing mechanics — and CRDTs don't fix it; current-state context injection does.**

**Gemini Dynamic UI** ([Google Research blog](https://research.google/blog/generative-ui-a-rich-custom-visual-interactive-user-experience-for-any-prompt/)) emits raw HTML/CSS/JS per turn. Google's own write-up reports minute-plus latency, frequent inaccuracies in generated UI, and no clear feedback loop from interactions in the generated UI back to the model. This is the "rough execution" the user flagged. **Lesson: emitting full-page HTML per turn is a trap.** Typed kinds with stable renderers beat free-form HTML for everything except the rare designer-mockup case.

**Vercel AI SDK + assistant-ui** ([assistant-ui Tool UI docs](https://www.assistant-ui.com/docs/guides/tool-ui)). Vercel's RSC `streamUI` (server streams React component trees) is **paused** — they recommend client-side renderer registries instead. assistant-ui maps tool-name → registered React component on the client; tool args stream incrementally; user interactions feed back via `addResult()`. **Lesson: server-side component streaming entangles UI render with LLM streaming in ways that hurt resilience. Renderer registry on the client, atomic tool calls from the server.** This is the architecture we want.

**Cursor / Aider** have no canvas concept — they edit the working tree directly with diff previews. Their content kind (code) already has a first-class renderer (the editor) and persistence (git). Our cockpit doesn't have that for non-code artifacts; that's the gap this feature fills.

**Vercel v0** is its own surface, file-scoped to React/Tailwind/shadcn, ZIP-exportable. Closer to a hosted IDE than a canvas. Lesson: keeping generation tightly scoped to one stack is part of why v0 works.

The pattern is well-established. The interesting questions for us are scope and integration with our agent loop, not the UX archetype itself.

## v1 Implementation Plan

Substantially most of the infrastructure already exists in the repo. Codebase research enumerated reusable patterns and the genuinely net-new pieces.

### Reusable infrastructure

| Concern | Existing pattern |
|---|---|
| Agent tool registration + phase gating | `TOOL_REGISTRY` + `filter_tools_by_phase` (`src/tools/registry.py:120-157`) |
| Transient context injection (canvas index) | `workspace_injection.py` + `src/graph.py:740-947` (mirror TODOS_INJECTION) |
| Job-scoped workspace file CRUD | `PUT /api/jobs/{job_id}/workspace/{path:path}` (`orchestrator/main.py:7758`) |
| HTTP + WebSocket reverse proxy with pod-IP resolution | `orchestrator/services/ide_proxy.py` + `main.py:7212-7360` |
| Signed short-lived tokens | `srw_<32urlsafe>` + sha256 storage (`main.py:11558-11654`) |
| Migration template + JSONB/timestamp triggers | `migrations/app/0001_initial.sql:1061-1106` |
| Agent → orchestrator state push | `OrchestratorClient` (`src/api/orchestrator_client.py:325-781`) |
| Agent broker for new routes | `src/api/dual_app.py:487-852` (port 8080 internal) |
| MCP read exposure | `@mcp.tool` + `AsyncCockpitClient` (`orchestrator/mcp/server.py`) |
| Cockpit signal-state from streamed tool calls | `JobArtifactService.applyToolCall()` + `BuilderStreamService` (`cockpit/src/app/core/services/`) — copy this shape verbatim for `CanvasService` |
| Cockpit feature scaffold | `cockpit/src/app/views/<feature>/`, eager route in `app.routes.ts` |
| Markdown rendering | `ngx-markdown` already provided in `app.config.ts` |
| Split-pane tiling | `angular-split` + `ComponentRegistry`/`ComponentHost` already wired in `cockpit/src/app/debug/` |
| Mobile-responsive | `viewport.isMobile()` signal (`core/services/viewport.service.ts`) |

### Net-new

| Concern | What to build |
|---|---|
| `0002_canvases.sql` | New migration per Persistence Model schema |
| Agent tool module | `src/tools/canvas/` — `get_canvas_metadata()` + `create_canvas_tools(context)`; ~50 lines + metadata block |
| Orchestrator REST | `POST/GET/PUT /api/jobs/{job_id}/canvases[/...]` plus an SSE/WS endpoint for cockpit live-update |
| Iframe + CSP renderer | Net-new — no iframe usage exists in `cockpit/src/app` today, no `DomSanitizer` use, no CSP injection plumbing |
| Canvas grid component | `views/canvas/canvas-grid.component.ts` + `views/canvas/canvas-tile.component.ts` + `core/services/canvas.service.ts` |
| Canvas proxy route | `/api/canvas/{token}/proxy/{path:path}` mirroring `ide_proxy_http`/`ide_proxy_ws` |
| Workspace HTTP server (mode B) | Spawn an in-workspace HTTP server (e.g., `python -m http.server` or a lightweight FastAPI app) on a known port; agent broker exposes `GET /canvas/{id}/preview` |
| MCP wrappers | `get_canvas`, `list_canvases`, `read_canvas` — three `@mcp.tool` functions |

### Library choices

- **Grid surface**: For v1, **start with `angular-split` + the existing `ComponentRegistry`** for a chat \| canvas split-pane. Already wired in `debug/`. Promote to **`angular-gridster2`** (v21.0.1, native standalone-component support, ~98K weekly downloads, tracks Angular majors lockstep) if/when true free-form drag-resize multi-tile is needed. Don't roll our own with CDK drag-drop — collision/resize/persistence/breakpoint is 3-4 weeks of reimplementation.
- **Code editor** (`code` kind): **Monaco** — net-new dependency.
- **Mermaid** (`mermaid` kind): net-new dependency.
- **Markdown** (`markdown` kind): `ngx-markdown` already provided.
- **Live media** (`url` kind, chicken-stream): **MJPEG via FFmpeg subprocess** through the orchestrator proxy. ~30 lines of Python. The `url` kind covers it; no separate `stream` kind needed in v1. Defer go2rtc / WebRTC to v2 unless latency complaints arrive.

### v1 scope (deliberately narrow, ordered)

Ship the smallest end-to-end loop first:

1. **`markdown` kind only.** Agent writes `canvases/foo.md` via the new `canvas` tool; cockpit splits the persistent-chat view (existing `angular-split`) to show a canvas pane next to chat; the pane renders one canvas via `ngx-markdown` with a textarea edit toggle. Includes the DB table, transient context injection, optimistic-locking conflict handling. Proves the loop end-to-end.
2. **Add `html` kind.** Introduce the iframe sandbox renderer with the CSP config from Security Considerations. Proves the security model end-to-end.
3. **Add `image` kind.** Agent writes a PNG to `canvases/foo.png`; cockpit auto-refreshes on file change. Unblocks the matplotlib data-analysis case.
4. **Add multi-tile grid.** Replace the single-pane canvas with a grid surface. Decide here whether `angular-split`'s nested splits are sufficient or whether to add `angular-gridster2`.
5. **Add proxy mode (B).** Workspace-side HTTP server, signed-token URL with separate registrable domain, browser-tab open. Designer-mockup tinkering becomes possible. Reuses IDE proxy template directly.
6. **Add `url` kind with MJPEG live stream.** Orchestrator-side FFmpeg subprocess, signed token, the chicken-cam use case.
7. **Defer**: `code` (Monaco), `mermaid`, `shared_browser` tile kind, MCP read access, mobile single-tile-fullscreen. Each adds value but isn't load-bearing for the core experience.

Honest estimate: steps 1-3 ~2-3 days of focused work given how much infrastructure is reusable; step 4 depends on grid-library decision; step 5 ~2-3 days for a careful proxy implementation; step 6 ~1 day. Steps 1-6 in roughly two solid weeks of focused work for a single engineer.

## Open Questions

Most of the original questions are now answered above (Persistence Model, Concurrency, Security Considerations → Concrete Sandbox Configuration, v1 Implementation Plan). What's left:

- **Slug collision policy** — `canvas_id` is agent-chosen, scoped to job/thread. After delete, can the same slug be reused immediately? If a deleted canvas's file still exists in workspace history, the file-vs-row state needs a documented rule.
- **Version history UX** — workspace files get git versioning via Gitea for free. Do we surface a version picker in the cockpit, or is "view in IDE" sufficient for now? Defer until users ask.
- **Mobile single-tile-fullscreen** — defer to after the multi-tile grid decision (step 4 in v1 scope). The cockpit's responsive shell uses `viewport.isMobile()` rather than a separate `simple/` route, so this is a layout switch, not a new shell.
- **Public Suffix List submission** — when (and whether) to submit the proxy domain. Cookie-bleed hardening; not v1 critical.
- **Per-canvas vs per-tile owner state in multi-user threads** — if two humans edit a thread's canvases simultaneously, does the lock track per-canvas or per-rendered-tile-instance? Defer until multi-user threading lands.
- **Subagent exposure** — when a parent agent delegates to a critic/curator subjob, does the subjob see the parent's canvases? Probably read-only by default. Defer.

## Relationship to Adjacent Features

- **`shared_browser.md`** — the live-Chromium tile is *one renderer kind* the canvas can host, rather than a separate UI tab. Or it stays separate; both are viable. The two should be designed coherently — they're different points on the same spectrum (showing the agent's view vs. showing what the agent built).
- **`notify_user_tool.md`** — orthogonal. Notifications can deep-link the user at a specific canvas (`/jobs/{id}/canvas/{cid}`).
- **`persistent_chat_ui_redesign.md`** — in persistent/interactive mode the canvas becomes a real-time collaboration surface, not just a job artifact.
- **`vm_snapshots_and_ide.md`** — for HTML mockups, the canvas short-circuits the IDE-plus-webserver detour. The IDE is still the right tool for editing real source files in the repo.
- **`workspace.md` / context injection** — if the canvas is a workspace file, it inherits the existing context-injection plumbing without new infrastructure.

## Next Steps (when picked up)

1. **Land the migration**: write `orchestrator/database/migrations/app/0002_canvases.sql` per the schema in Persistence Model. Wire `update_updated_at_column()` trigger.
2. **Agent tool module**: implement `src/tools/canvas/` following the registration pattern in `src/tools/core/todo.py`. Wire into `TOOL_REGISTRY` for both phases.
3. **Transient injection**: extend `src/core/workspace_injection.py` with the `<active_canvases>` block, mirror of `TODOS_INJECTION_CONTENT_PREFIX`. Hook into the per-turn injection path in `src/graph.py:740-947`.
4. **Orchestrator REST**: add `POST/GET/PUT /api/jobs/{job_id}/canvases[/...]` plus an SSE endpoint for cockpit live-update. Reuse the workspace-file write path under the hood.
5. **Cockpit `markdown` end-to-end**: `views/canvas/`, `core/services/canvas.service.ts` modeled on `JobArtifactService`/`BuilderStreamService`, attach via `angular-split` to the persistent-chat layout. Edit toggle backed by a textarea + `ngx-markdown` viewer.
6. **Iframe sandbox renderer (`html` kind)**: implement the CSP-injected `<iframe srcdoc>` per Concrete Sandbox Configuration. Manual phishing-attempt test (agent renders fake login form → verify `form-action 'none'` blocks submission, no cookie leak, no top-nav).
7. **Image and grid**: matplotlib-output flow + the multi-tile grid decision (split-pane vs gridster).
8. **Proxy mode**: workspace HTTP server + `/api/canvas/{token}/proxy/{path}` route + separate-registrable-domain DNS setup.
9. **Live MJPEG**: FFmpeg subprocess + token-gated stream endpoint + `<img>` tag in the `url` tile.

Cross-cutting: keep the `shared_browser`-as-canvas-kind decision deferred until steps 1-6 are stable. The natural answer at that point is "yes, `shared_browser` becomes a kind"; commit then, not before.

## Sources

- Claude Artifacts system prompt: [dedlim gist](https://gist.github.com/dedlim/6bf6d81f77c19e20cd40594aa09e3ecd)
- Anthropic text editor tool: [docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool)
- ChatGPT Canvas edit-visibility issue: [community thread](https://community.openai.com/t/assistant-cannot-see-and-access-my-edits-in-canvas/1155732)
- Vercel AI SDK paused `streamUI`: [docs](https://ai-sdk.dev/docs/ai-sdk-rsc/streaming-react-components)
- assistant-ui Tool UI: [docs](https://www.assistant-ui.com/docs/guides/tool-ui)
- Google Research Generative UI: [blog](https://research.google/blog/generative-ui-a-rich-custom-visual-interactive-user-experience-for-any-prompt/)
- Iframe sandbox-escape (allow-scripts + allow-same-origin): [Mozilla Discourse](https://discourse.mozilla.org/t/an-iframe-which-has-both-allow-scripts-and-allow-same-origin-for-its-sandbox-attribute-can-remove-its-sandboxing/28255)
- CSP via `<meta>` in srcdoc: [csplite](https://csplite.com/csp/test188/)
- COOP/COEP guide: [web.dev](https://web.dev/articles/coop-coep)
- Sandpack origin model: [Josh Comeau](https://www.joshwcomeau.com/react/next-level-playground/)
- Vercel public-suffix-list: [Vercel KB](https://vercel.com/kb/guide/can-i-set-a-cookie-from-my-vercel-project-subdomain-to-vercel-app)
- angular-gridster2: [npm](https://www.npmjs.com/package/angular-gridster2) · [GitHub](https://github.com/tiberiuzuld/angular-gridster2)
- gridstack.js Angular wrapper: [docs](https://gridstackjs.com/angular/doc/html/index.html)
- go2rtc (RTSP→browser gateway, deferred to v2): [GitHub](https://github.com/AlexxIT/go2rtc)
