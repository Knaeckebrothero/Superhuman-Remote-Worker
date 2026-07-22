---
tags:
  - feature
  - cockpit
  - browser
  - canvas
  - observability
  - agent-tool
aliases:
  - shared browser window
  - browser livestream
  - agent browser viewer
  - browser in canvas
related:
  - "[[dynamic_canvas]]"
  - "[[browser_workspace_executor]]"
  - "[[sessions]]"
  - "[[notify_user_tool]]"
---

# Feature: Shared Browser

Design document for the shared browser: the user watches — and drives — the
same live Chromium the agent's browser tools use, from inside the Dynamic
Canvas. Either side can open it: the agent presents its browser to the user, or
the user opens it directly from the canvas, browses to a page, and hands it to
the agent.

**Status:** CONTAINER USER FLOW IMPLEMENTED AND REPOSITORY-VERIFIED 2026-07-22.
Plans 1 and 2 Tasks 1–13 are complete behind the default-off
`canvas.sharedBrowser.enabled` gate. The workspace daemon, pinned-SSH
orchestrator broker, agent handoff, and Cockpit open/view/drive/restart flow are
implemented. The container image, focused backend suites, Cockpit unit/build,
and three-engine production-browser gates pass. This is not yet an enabled
development release: Task 14 remains partial, so the committed Tilt profile
stays off. VM operation also remains unclaimed until remote binding, routing,
image provisioning, and conformance are attested.

This revision supersedes the earlier draft of this document, which was written
against the removed cross-pod CDP-`:9222` architecture (see "What changed"
below). Scope decisions remain locked: full shared browser in one feature
(view + user-drive + handoff), explicit control baton, auto-start on open, and
orchestrator-brokered transport. This document is the authority for browser
identity, streaming, and control leases; `dynamic_canvas.md` Slice 5 is the
hosting half.

## Current implementation boundary

The implemented container-workspace flow consists of:

- a loopback-only framed stream in `docker/browser-exec`, with generation
  identity, daemon-owned baton, daemon-side navigation validation, CDP JPEG
  screencast, input dispatch, viewer fan-out, and auto-release;
- fail-closed orchestrator configuration, authenticated open and stream
  endpoints, pinned-SSH relay, Canvas capability/source selection, immediate
  and periodic workspace-activity marking, and default-off Helm wiring;
- generation pinning that closes the WebSocket with `4409` when the Canvas
  generation is absent or no longer matches the live daemon generation;
- serialized baton, input, and mutating agent actions, so the daemon remains
  the single authority at control-handoff boundaries;
- capability-aware agent `set_canvas(browser_id="current")`, prompt-visible
  baton state, and explicit `user_is_driving` mutation refusals; and
- a Cockpit binary protocol/controller, decoded-bitmap renderer, pre-source
  Open browser action, fixed-viewport input mapping, URL controls, baton UI,
  popout fan-out, bounded reconnect, and ended-generation restart.

The final focused gate is 649 Python tests, 95 Cockpit test files / 1,347
tests, a production build, 33 Playwright cases across Chromium, Firefox, and
WebKit, both Helm lint overlays, and a real-Chromium Podman image run. The
implementation plan is
`docs/superpowers/plans/2026-07-22-shared-browser-cockpit-handoff.md`; the
redacted evidence and exact limitations are in
`docs/tests/shared_browser_verification.md`.

Still deferred are the stage-2 VM artifact run and deployment-specific VM
binding/routing attestation. Live prompt-driven handoff also needs a working
LLM endpoint, and this host's k3d CNI prevented a replacement orchestrator pod
from reaching an already-running workspace even though the real Uvicorn 1012
reconnect path passed in place. VM/runtime trust remains fail-closed. Because
those Task-14 release gates are incomplete, chart defaults and the committed
Tilt profile remain off; the full container UI is available only when an
operator explicitly enables the feature.

## Motivation

The agent runs in a black box. The user has several windows into what it's
doing — files (IDE/Gitea), shell, conversation — but the **browser is a blind
spot**. The agent navigates pages, fills forms, and verifies its own work
visually; the user sees a tool call and a text snapshot, never the page.

This gap matters four ways:

1. **Stuck states are invisible.** Cookie banners, CAPTCHAs, geo-blocks, login
   walls — obvious to a human glancing at a screen, opaque in a tool log. The
   agent loops on them before giving up.
2. **Verification is unverifiable.** When the agent claims "the page rendered
   correctly," the user has to take its word for it.
3. **Frontend work is impossible to review live.** The user should be able to
   *watch* the agent click around the UI it is building.
4. **Page hand-off is impossible.** The user cannot say "here, *this* page" —
   today they paste a URL into chat and hope the agent's fresh navigation
   reaches the same state (login walls and session state usually break this).
   With a shared browser the user opens the page themselves — logged in,
   scrolled to the right spot — and the agent picks up *that* browser, cookies
   and all.

Point 4 is the reason both open directions are first-class: agent-opens (show
the user) and user-opens (show the agent).

## What changed since the first draft

The original version of this document assumed `src/tools/research/browser.py`
launching Chromium with `--remote-debugging-port=9222`, reachable cross-pod.
That architecture was removed on 2026-06-11
(`docs/issues/remove_local_browser_fallback.md`,
`docs/features/browser_workspace_executor.md`): the only browser today is a
headless Chromium owned by the **`browser-exec` daemon inside the workspace**,
driven over SSH, with CDP confined to the workspace — no cross-pod CDP, no
debugging port on the pod network. The container workspace path is proven;
the same architecture is intended for VMs, but shared-browser VM operation is
not yet deployment-attested. Port 9222 declarations in
`container_provisioner.py` are vestigial and unreachable (Chrome binds CDP to
`127.0.0.1`).

Two more things changed:

- **The Dynamic Canvas shipped** (Slices 0–3). The host surface for the shared
  browser is the canvas `browser` source in sessions — already stubbed in
  `cockpit/src/app/core/models/canvas.model.ts` (`BrowserCanvasSource`) and
  reserved as Slice 5 in `dynamic_canvas.md` — not a new "Browser" tab in the
  job detail view. The job view is a future extension.
- **User-initiated open is promoted to first-class.** The first draft treated
  the feature as observability (agent opens, user watches, user may take
  over). The locked scope adds the reverse flow as a launch requirement: an
  "Open browser" button in the canvas that needs no agent involvement.

Everything in "Industry Context" below survives unchanged; the transport and
broker sections are rewritten.

## Decisions (locked 2026-07-20)

| Question | Decision |
|---|---|
| Scope of first build | Full shared browser: view + user-drive + handoff in one feature, built in five landable steps |
| Host surface | Dynamic Canvas `browser` source in sessions (persistent threads); job view later |
| Which browser | The **same** workspace Chromium the agent's tools use (one browser, two drivers) — never a second instance |
| Control arbitration | Explicit baton (single-controller lease). Visible holder, instant user takeover, agent *actions* refused while user drives, agent *reads* always allowed |
| Baton authority | Lives in the `browser-exec` daemon, next to the browser — race-free, HA-replica-proof |
| Cold start | "Open browser" always available on full-backend sessions; click auto-provisions workspace + spawns browser with staged progress. Lite backends: disabled with tooltip |
| Transport | Orchestrator-brokered: cockpit ↔ new binary WS on the orchestrator ↔ pinned SSH `direct-tcpip` ↔ workspace **loopback** TCP listener. CDP never leaves the workspace |
| Viewport | Fixed (daemon default, 1280×720 unless configured), letterbox-scaled in the cockpit; no live resize in v1 |
| Rollout | Dark-shipped behind `canvas.sharedBrowser.enabled` (default off); experimental profile on, committed Tilt profile held off until the live release gate passes |

## Industry Context

### How Others Do It

| System | Pattern | Transport | Interaction | Notes |
|--------|---------|-----------|-------------|-------|
| **Browserless** | Live view via CDP screencast | WebSocket (JPEG frames) | Yes — input events relayed via CDP | Their commercial "live URL" feature; the de-facto reference design |
| **Steel.dev** | Session viewer with screencast | WebSocket | Yes | Open-source equivalent of Browserless live view |
| **Selenium Grid + noVNC** | Headed Chromium with Xvfb + x11vnc + noVNC | WebSocket (VNC) | Yes | Battle-tested but heavy: display server + three extra processes |
| **Kasm Workspaces** | Full-desktop streaming via KasmVNC | WebSocket (custom VNC fork) | Yes | Streams the entire desktop; overkill |
| **Playwright Trace Viewer** | Post-hoc replay from trace files | None (offline) | No | Debugging, not live observation |
| **Cursor / Devin** | Static screenshots in chat | HTTP | No | Today's SRW pattern too |
| **OpenAI Operator** | Built-in browser viewer | Proprietary streaming | "Take control" handoff | The UX gold standard for this category |
| **Tandem Browser** | Shared browser, agent gets own tab | MCP / local HTTP | Yes — tab isolation, not mutual exclusion | Contention solved by separation |
| **Browserbase (rrweb → CDP)** | DOM-replay rebuilt on CDP screencast | WebSocket | N/A (recording) | **Abandoned rrweb** — replays silently diverged from reality on iframes/shadow DOM/canvas/video |

### Key Takeaways

1. **CDP screencast is the modern default.** Native to Chromium, headless-
   compatible, zero extra processes. `Page.startScreencast` emits base64 JPEG
   frames; `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` handle input.
2. **rrweb is disqualified** for a verification use case — replays can look
   fine while lying about what rendered (Browserbase's migration story).
3. **VNC is the legacy path** — needs Xvfb + x11vnc + websockify per
   workspace.
4. **CDP screencast has mandatory built-in backpressure.** Every frame must be
   acknowledged with `Page.screencastFrameAck` before the next arrives — the
   source can never outrun the consumer that acks. (Our design acks after
   handing the frame to bounded per-viewer queues; see Piece 1.)
5. **The hard part is not the viewer — it's the handoff.** Mutual exclusion
   (our baton) or tab isolation (Tandem). For intervention use cases (CAPTCHA
   rescue, "fix this form"), mutual exclusion on the agent's active page is
   the right default; tab isolation is a possible future collaborative mode.
6. **Streaming bandwidth is fine at our scale.** Quality 60, ~5 fps, 1280×720
   is roughly 50–200 KB/s per viewer during active page changes, near zero on
   a static page.
7. **In-process CDP access already exists.** browser-use's `BrowserSession`
   exposes its own CDP client (`session.cdp_client` /
   `get_or_create_cdp_session(...)`), and `browser-exec` already round-trips
   CDP in-process for `take_screenshot` (`Page.captureScreenshot`). Screencast
   is the streaming sibling of proven code. (The first draft's takeaway —
   "CDP is enabled on :9222" — is obsolete; see "What changed".)

## Architecture

One Chromium, two drivers, one new pipe:

```
┌─ workspace pod/VM ─────────────────────────────────────────┐
│  Chromium ←— in-process CDP —→ browser-exec daemon         │
│    • Page.startScreencast → JPEG frames                    │
│    • Input.dispatch*Event ← user input                     │
│    • BATON (single-controller lease) lives here            │
│    • browser generation identity lives here                │
│    └─ NEW stream listener on 127.0.0.1:<port> only         │
└──────────────────────┬─────────────────────────────────────┘
              SSH direct-tcpip (existing pinned pool —
              same pattern as the canvas live-app gateway)
┌──────────────────────┴─────────────────────────────────────┐
│  orchestrator: stream broker                               │
│    • WS  /api/persistent/threads/{tid}/browser/stream      │
│    • GET /api/persistent/threads/{tid}/browser/capability  │
│    • POST /api/persistent/threads/{tid}/browser/open       │
│    • stateless byte relay + auth + activity marking        │
└──────────────────────┬─────────────────────────────────────┘
                cockpit binary WebSocket (thread auth)
┌──────────────────────┴─────────────────────────────────────┐
│  cockpit: canvas 'browser' renderer (fills existing stub)  │
│    • <canvas> frame painter • URL bar + back/reload        │
│    • baton pill + toggle    • reconnect / cold-start UI    │
└────────────────────────────────────────────────────────────┘
```

No new pod-network ports, no new services, no new database tables. The only
mutable state outside existing systems (canvas presentation state, workspace
bindings) is the baton + generation, held by the daemon that owns the browser.

### Piece 1 — `browser-exec` streaming mode

`docker/browser-exec` today is a strict one-JSON-request-one-response daemon
on a unix socket, with every action serialized under one asyncio lock. It
gains a streaming side-channel:

**Loopback stream listener.** A TCP listener bound to `127.0.0.1:<port>`
(default 38801, env `BROWSER_EXEC_STREAM_PORT`). Loopback-only means it is
covered by the existing sshd `PermitOpen 127.0.0.1:*` policy and never
appears on the pod network — the same posture as canvas live-app ports. The
existing unix-socket action path is unchanged; agent tools are unaffected.

**Framing protocol** (both directions on the TCP stream): `[4-byte big-endian
length][1-byte type][payload]`. Types:

| Type | Direction | Payload | Purpose |
|---|---|---|---|
| `HELLO` | in | JSON `{token, min_protocol}` | First message. Token authenticates the broker (see below); daemon replies `STATE`. Wrong/missing token → connection closed |
| `FRAME` | out | `[2-byte BE header length][header JSON {generation, w, h, ts}][raw JPEG bytes]` | One screencast frame |
| `STATE` | out | JSON `{generation, url, title, loading, baton, viewport}` | Sent on connect and on every change (navigation, title, baton flip) |
| `INPUT` | in | JSON mouse/key/wheel event (CDP-shaped) | Honored only while `baton == "user"` |
| `CONTROL` | in | JSON `{op: "take_baton" \| "release_baton" \| "navigate" \| "back" \| "reload", ...}` | Viewer commands |
| `ERROR` | out | JSON `{code, message}` | e.g. `navigation_rejected`, `browser_gone` |

**Stream lifecycle.** The screencast CDP session
(`session.get_or_create_cdp_session` → `Page.startScreencast`, JPEG, quality/
size from env, see "Quality tuning") runs as an independent asyncio task that
**never takes the daemon's action lock** — agent tool calls and streaming
proceed concurrently (multiple CDP sessions on one target are supported; the
screenshot path proves in-process CDP works). Each frame is handed to a
**bounded per-viewer send queue** (drop-oldest for laggards, so one slow
viewer never stalls the others), and `screencastFrameAck` is sent after that
hand-off — CDP's backpressure therefore caps frame production at what the
daemon itself can process, while per-viewer drops absorb slow links. With
**zero viewers connected the screencast is stopped** (no CPU/bandwidth spent
on an unwatched browser). If the active CDP target changes (navigation,
popup), the screencast re-attaches to the new active target.

**Hello token.** The daemon mints a random token per browser generation and
returns it (with the generation and port) from a new `stream_info` action on
the existing unix-socket protocol; the orchestrator obtains it by running
`browser-exec stream_info` over the authenticated SSH channel. Defense-in-
depth on an already-loopback listener, and it doubles as the generation
check.

**Browser generation.** A UUID minted whenever the daemon starts (or
restarts) its Chromium. It appears in `STATE`/`FRAME` and in the canvas
source pointer. Per the `dynamic_canvas.md` Slice-5 contract, a stored
pointer must never silently follow a *different* browser: if the generation
behind a canvas pointer is gone (daemon restarted, workspace re-provisioned),
viewers get an explicit **ended** state with a one-click restart that mints a
new generation and updates the canvas source — reconnects to the *same*
generation are silent, attaching to a *new* one is always an explicit user
action. The daemon keeps using its existing dedicated profile dir
(`user_data_dir`), satisfying the dedicated-profile requirement.

**The baton (single-controller lease).** Daemon-held state:
`baton ∈ {agent, user}`.

- Initial holder: whoever opened the browser — `user` when the user's `open`
  started it, `agent` when the agent's first tool call did.
- `CONTROL take_baton` flips to `user` instantly (no agent consent — an
  in-flight agent action completes or fails naturally; everything after is
  refused). `release_baton` flips back to `agent`.
- While `baton == user`: **mutating** actions arriving on the unix-socket
  action path (`navigate`, `click`, `type`, `select`, `scroll`, `back`,
  `close`) return a structured refusal
  `{"error": "user_is_driving", "url": <current>, "message": ...}` instead of
  executing. **Read-only** actions (`snapshot`, `screenshot`, state queries)
  always succeed — the agent can look at the page the user is showing it.
- While `baton == agent`: `INPUT` messages are dropped (the viewer UI doesn't
  send them; dropping guards against races).
- **Auto-release:** if no viewer connection exists for 30 s while
  `baton == user`, the baton reverts to `agent` — a closed laptop can never
  wedge the agent. (A 30 s grace, not instant, so the cockpit's reconnect
  backoff and token re-mint don't cause spurious flips.)
- Baton changes are broadcast in `STATE` so all viewers agree, and are
  visible to the agent via the refusal payload and a `baton` field added to
  read-action responses.

**User navigation safety.** `CONTROL navigate` runs the **same URL
validation** the agent's `browser_navigate` applies
(`src/tools/research/browser_security.py` policy, enforced daemon-side).
The browser runs inside the workspace's network identity; user-driving must
not become a side door around the SSRF/egress policy the agent path already
enforces. Rejected navigations return `ERROR navigation_rejected` and the
URL bar shows why.

### Piece 2 — Orchestrator broker

Three endpoints on the orchestrator (`orchestrator/routers/shared_browser.py`
and `orchestrator/services/browser_stream_broker.py`), all behind
`CANVAS_SHARED_BROWSER_ENABLED` behavior and standard thread ownership auth:

**`GET /api/persistent/threads/{tid}/browser/capability`** — the pre-source
capability used before a Canvas row exists. It returns only
`feature_enabled`, `can_open_browser`, `workspace_ready`, and a bounded reason
code. It never returns a host, port, backing ID, workspace/browser generation,
fingerprint, or token. Cold container sessions may be openable before their
workspace is ready because `open` owns provisioning; lite sessions and
unattested or unroutable VM contexts fail closed.

**`POST /api/persistent/threads/{tid}/browser/open`** — the one recovery and
cold-start path (idempotent). Its public body may supply only a title; a public
caller requests the user as initial holder when a new browser generation is
created. The internal agent Canvas adapter calls the same service with the
agent as creation-time holder without exposing that choice in the public API.
Re-opening an existing generation never flips its current baton:

1. Reject lite and untrusted/unroutable remote backends with a typed capability
   error.
2. Ensure the session workspace exists (`ensure_session_workspace`) — the
   auto-start decision. A still-provisioning request returns `202` plus a
   bounded retry hint.
3. Over the same provisioner-attested, generation-bound, host-key-pinned SSH
   transport as the stream relay: spawn/ping the `browser-exec` daemon, ensure
   the listener is up, read its private hello token, and start a browser at its
   default page if no generation exists. Navigation then uses the validated
   stream control path.
4. Set the canvas presentation source to the private concrete
   `{type: "browser", browser_generation: <uuid>}` through the existing Canvas
   control plane. An atomic compare-and-no-op write keeps retries on the same
   generation at the same presentation revision.
5. Return the ordinary generation-redacted `CanvasPublicState` plus its strong
   `ETag` and a boolean mutation header. The first `STATE` stream message
   carries the private generation, viewport, and current page state only inside
   the authenticated controller.

Plan 1 shipped a dark bootstrap form of `open`; Plan 2 tightens it to this
public contract before adding either caller, including the ended-generation
restart UI.

**`GET /api/persistent/threads/{tid}/browser/stream`** (WebSocket) — the
relay:

1. Authenticate the upgrade with the BFF session cookie, exact allowed
   `Origin`, approved user, and thread ownership. Re-run admission after the
   potentially long browser startup and before accepting. Cockpit applies
   bounded-backoff reconnect on transient drops; distinct 44xx/45xx close
   codes signal malformed input, disabled/auth/owner, ended-generation,
   viewer-limit, transient transport, and unavailable-workspace states.
2. Open `PinnedSSHTransportPool.open_loopback_connection(workspace,
   BROWSER_EXEC_STREAM_PORT)` — the exact mechanism the canvas live-app
   gateway uses for long-lived byte streaming, host-key-pinned and
   generation-checked. The container path is conformance-proven. The transport
   abstraction can support attested VMs, but the shared-browser VM path is not
   operationally claimed until deployment routing and remote binding are
   independently verified.
3. Send `HELLO`, then relay both directions. WS messages are binary and carry
   `[1-byte type][payload]` — the TCP framing minus the length prefix. The
   broker bounds client commands and accepts only `INPUT`/`CONTROL`; otherwise
   it remains a byte relay and never interprets page/frame content.
4. While at least one stream WS is attached, periodically mark workspace
   activity so watching a page cannot get the workspace reaped mid-view.

HA note: each viewer WS is served by whichever replica accepted it; each opens
its own SSH channel; the daemon fans out and enforces the final global viewer
cap. No cross-replica baton/generation coordination is needed because that
shared state lives daemon-side.

The IDE proxy (`ide_proxy_ws`) is the in-repo proof that the orchestrator can
bridge binary WebSockets; its transport (direct pod-IP, fails on VMs) is
deliberately **not** copied — the SSH pool is the loopback-preserving path and
the intended VM transport once the remaining deployment attestations land.

### Piece 3 — Cockpit renderer

The implemented renderer fills the `browser` Canvas source
(`BrowserCanvasSource` in `canvas.model.ts`):

- `canvas-rendering.ts` `selectCanvasRenderer()`: `browser` source +
  `capabilities.can_stream_browser === true` → new `'browser'` renderer
  (fail-closed otherwise, like the `app` renderer).
- New `CanvasBrowserRendererComponent` +  pane-local
  `CanvasBrowserController` (sibling of `CanvasViewerController`): owns the
  stream WS, decodes `FRAME` payloads via `createImageBitmap()`, paints onto
  a `<canvas>` sized to the fixed viewport and letterbox-scaled with CSS.
  Skip-if-still-decoding to avoid backlog.
- **Browser toolbar** (headless Chromium has no chrome, so we render our
  own): URL bar (shows `STATE.url`, editable → `CONTROL navigate`),
  back/reload buttons, page title, loading indicator, and the **baton pill**
  — "You're driving" / "Agent is driving" with a single Take/Release toggle.
- **Input capture** while holding the baton: mouse down/up/move (move
  throttled), wheel, keydown/keyup with modifier state — translated to
  CDP-shaped `INPUT` messages through a unit-tested `coordinateMapper` (see
  below). Ignored/not sent while the agent drives.
- **"Open browser" affordances**: a button in the canvas empty state and an
  always-present toolbar icon (visible when the capability allows). Clicking
  while other content is staged switches the canvas source (standard
  single-main-canvas semantics). Staged cold-start progress: "Starting
  workspace… → Starting browser… → connected". On lite backends the button is
  disabled with an explanatory tooltip. Failure → explicit error state with
  retry.
- **Reconnect**: WS drop → "Reconnecting…" overlay with backoff; if the
  daemon reports/implies a dead generation → **ended** state with a restart
  button (→ `open`). The popout window and main tab may both attach (frames
  fan out); input flows only from the baton holder's user, which is the same
  user in v1.
- **Hidden-pane pause**: the chat page keeps the canvas mounted when hidden
  behind the settings pane; the renderer detaches its stream while not
  visible (per the `dynamic_canvas.md` host contract that Slice 5 pauses
  host-owned streaming) and re-attaches on reveal — combined with the
  daemon's zero-viewer stop, an unwatched browser costs nothing.
- i18n: en + de strings, matching the existing canvas translation coverage.

**Coordinate mapping** (the most error-prone part; unchanged from the first
draft, still fully applicable): `Input.dispatchMouseEvent` expects CSS pixels
relative to the browser viewport. The cockpit canvas renders at arbitrary
display size while the viewport is fixed (e.g. 1280×720) and the JPEG may be
CDP-downscaled. Every event maps
`cdp_x = canvas_x * (viewport_css_width / canvas_display_width)` (same for
Y); the canvas backing store is sized from the **decoded bitmap**, never from
frame metadata (vercel-labs/agent-browser #632: metadata reports
pre-downscale dimensions — trusting it yields blur and offset clicks).
Keyboard needs explicit modifier-state tracking. One `coordinateMapper()`
helper, unit-tested across viewport/display ratios.

### Piece 4 — Agent surface (deliberately tiny)

- `set_canvas` advertises `source_type: "browser"` only when the agent backend
  has the positive `canvas_shared_browser_available` bit from the
  orchestrator-attested attach response. It resolves `browser_id: "current"`
  at tool-call time to the concrete generation per the Slice-5 contract. The
  server-side handling shares the `open` service; the agent does not infer
  capability from a local flag or generic remote-backend label.
- Browser action tools surface the daemon's `user_is_driving` refusal as a
  clear tool result ("The user is currently driving the shared browser
  (currently on <url>). Ask them to release control, or work with read-only
  snapshots.") — prompt-visible, no schema change.
- No push notification to the agent on baton flips in v1; the agent learns
  from refusals, read-response `baton` fields, or the user's message. (An
  agent-visible baton event and agent-initiated "please take over and solve
  this CAPTCHA" requests are the designed Phase-3 follow-up; the `CONTROL`
  vocabulary leaves room via an `initiated_by` field.)

The hand-off needs no machinery beyond this: the user browses to a page,
optionally releases the baton, and tells the agent what to do; the agent's
next `browser_snapshot` reads the same Chromium — same DOM, same cookies,
same login state.

## Quality / bandwidth tuning

Consumed by `browser-exec` (env on the workspace, settable via chart):

| Env var | Default | Rationale |
|---------|---------|-----------|
| `BROWSER_EXEC_STREAM_PORT` | 38801 | Loopback-only listener port |
| `BROWSER_EXEC_STREAM_FORMAT` | `jpeg` | PNG is 3–5× larger for no live-view benefit |
| `BROWSER_EXEC_STREAM_QUALITY` | 60 | Text legibility vs bandwidth sweet spot |
| `BROWSER_EXEC_STREAM_MAX_WIDTH` | 1280 | Matches the fixed viewport |
| `BROWSER_EXEC_STREAM_MAX_HEIGHT` | 720 | 16:9 |
| `BROWSER_EXEC_STREAM_EVERY_NTH` | 2 | ~5 fps; smooth enough for observation |

Estimated bandwidth at defaults: 50–200 KB/s per viewer during active page
changes, near zero on a static page (screencast only emits on change).
Configurable because hard-coded caps are exactly how agent-browser #632
produced unfixable blur on portrait/HiDPI viewports.

## What we lose by going headless

CDP screencast captures the rendered viewport only — not native file pickers,
print dialogs, Chromium-level auth/certificate prompts, WebAuthn/OS dialogs,
or the built-in PDF viewer surface. For scripted agent flows this is mostly
fine; the known gaps are PDF-heavy pages (workaround: download instead of
view) and auth challenges that escape to native UI. Escape hatch if evidence
demands it: headed Chromium under Xvfb **still transported via CDP
screencast** (one extra process, not a VNC stack). The code must not couple
"screencast" to "headless" anywhere.

## Risks

| Risk | Mitigation |
|------|-----------|
| browser-use active-target/loading APIs drift within the pinned 0.12 line | The real 0.12.9 probe records `AgentFocusChangedEvent` and Page loading call forms, and both image definitions install the same live conformance check |
| Screencast work starves agent actions in the daemon | Streaming task never takes the action lock; zero-viewer stop; CDP ack-backpressure caps frame production |
| Slow viewer stalls the stream for everyone | Ack after fan-out with per-viewer send queues; drop frames for laggards rather than blocking the ack |
| User input collides with agent actions | Baton enforced at the daemon — the only place with a total order over both input paths |
| Closed tab wedges the agent | 30 s no-viewer auto-release to `agent` |
| Canvas pointer silently follows a new browser | Generation pinning: same-generation reconnects silent, new generation = explicit ended → restart action |
| Workspace idle-reaper kills the pod mid-view | Attached stream marks workspace activity |
| User navigation bypasses egress/SSRF policy | Daemon applies the same `browser_security` validation to `CONTROL navigate` as to agent navigation |
| Coordinate mapping bugs (wrong-place clicks) | Single `coordinateMapper()`, unit-tested across ratios; bitmap-derived dimensions, never metadata |
| Viewport resize mid-stream corrupts frames | Fixed viewport in v1; display scaling is CSS-only |
| Stream endpoint auth/abuse | Thread-ownership auth + per-generation internal daemon hello token; default cap of 3 concurrent viewers per thread; feature flag default-off |
| Sensitive page content in frames | Same boundary as the IDE/canvas: the viewer is the session owner; no new audience |
| VM workspaces behave differently than pods | Keep the VM capability fail-closed until the remote Canvas binding, orchestrator route, VM image provisioning, and the same conformance contract are attested; only the container path is currently proven |

## Build order

Five landable steps — each leaves the tree green and shippable. A through D
and the implementation portion of E are complete. Development enablement is
still held at E's Task-14 acceptance gate. The executable task order and gates
are in
`docs/superpowers/plans/2026-07-22-shared-browser-cockpit-handoff.md`:

- **A — Daemon streaming mode (implemented).** Pre-flight CDP API verification
  on the real image; stream listener + framing + screencast task + baton +
  hello token + generation; extend the `assert-browser-stack.sh`-style
  conformance gate (spawn daemon, attach, assert frames, inject click, assert
  page change, assert baton refusal).
- **B — Orchestrator broker (implemented).** `open` + stream WS + SSH relay +
  capability flag + activity marking; helm `canvas.sharedBrowser.enabled` →
  `CANVAS_SHARED_BROWSER_ENABLED`; canvas source plumbing.
- **C — Cockpit view-only (implemented).** Renderer + controller + frame painting +
  toolbar (read-only URL, title, loading) + open button + cold-start staged
  progress + lite-backend gating.
- **D — Drive + baton (implemented).** Input capture + `coordinateMapper` + baton
  pill/toggle + editable URL bar/back/reload + agent-side refusal surfacing
  + `set_canvas` browser advertisement.
- **E — Polish implemented; enablement pending.** Reconnect/ended/restart
  states, popout fan-out, German translations, and the verification record are
  complete. The dev-profile switch remains off until the VM, live LLM, and
  full pod-rotation acceptance gaps in the verification record close.

## Testing

1. **Backend unit/service suites (passing):** the final Plan-2 focused command
   passes 649 tests covering the daemon, transport, broker, capability/open,
   agent tools, persistent runtime, Canvas regressions, and infrastructure.
2. **Container conformance (passing, Podman + real workspace image):** the
   Step-A gate starts the daemon, attaches a stream, receives a real JPEG,
   injects input, observes the page change, verifies baton refusal, and checks
   zero-viewer screencast stop and generation renewal.
3. **Cockpit gates (passing):** 95 Vitest files / 1,347 tests, i18n parity, the
   production Angular build, and 33 production-bundle cases across Chromium,
   Firefox, and WebKit.
4. **Live k3d smoke (partial):** cold open, same executor/browser identity,
   baton/refusal, navigation rejection, popout, viewer cap, activity,
   zero-viewer release, 1012 reconnect, and ended/restart passed. VM image,
   natural-language LLM handoff, and full pod-rotation continuity remain
   unclaimed; see `docs/tests/shared_browser_verification.md`.

## Out of scope for v1 (future extensions)

- **Agent-initiated control requests** ("please solve this CAPTCHA") — the
  most valuable follow-up; protocol leaves room (`initiated_by`).
- **Job detail view surface** — same broker, different host UI.
- **Multi-tab UI** — v1 follows the active target only; no tab strip.
- **Tab-isolation collaborative mode** (Tandem pattern), multi-user viewers,
  recording/replay, iframe-embeddable share links, headed-under-Xvfb,
  MCP `view_agent_browser` — unchanged from the first draft's list.
- **Live viewport resize / HiDPI passthrough.**
- **Explicit "stop browser" button** — closing the pane just detaches
  viewers (zero-viewer stop already saves the CPU); the browser remains for
  the agent, and workspace lifecycle owns final teardown.

## Open questions

1. Should baton flips be pushed into the agent's context as an event (vs.
   discovered via refusals)? Deferred — decide with Phase-3 design.
2. Exact viewer-connection cap and whether it needs to be configurable.
3. Whether `open` with a URL should also be exposed as a deep-link (e.g.
   paste a URL into chat and click "open in shared browser").

## References

- [Browserless — Screen Recording & LiveURL docs](https://docs.browserless.io/baas/interactive-browser-sessions/screencasting)
- [Browserbase — rrweb → CDP screencast migration story](https://www.browserbase.com/blog/session-recordings)
- [Steel.dev — Human-in-the-Loop Controls](https://docs.steel.dev/overview/sessions-api/human-in-the-loop)
- [browser-use — "Closer to the Metal: Leaving Playwright for CDP"](https://browser-use.com/posts/playwright-to-cdp)
- [vercel-labs/agent-browser — Issue #632 (HiDPI stream resolution)](https://github.com/vercel-labs/agent-browser/issues/632)
- [CDP — Page domain (`startScreencast`, `screencastFrameAck`)](https://chromedevtools.github.io/devtools-protocol/tot/Page/)
- [CDP — Input domain (`dispatchMouseEvent` coordinates)](https://chromedevtools.github.io/devtools-protocol/tot/Input/)
- [Tandem Browser — tab-isolation pattern](https://github.com/hydro13/tandem-browser)
