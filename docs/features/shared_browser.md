---
tags:
  - feature
  - cockpit
  - browser
  - observability
  - agent-tool
aliases:
  - shared browser window
  - browser livestream
  - agent browser viewer
related:
  - "[[vm_snapshots_and_ide]]"
  - "[[sessions]]"
  - "[[notify_user_tool]]"
---

# Feature: Shared Browser Window

Design document for letting the user watch — and optionally take control of — the agent's live browser session from the cockpit.

**Status:** Design phase.

## Motivation

The agent runs in a black box. The user has several windows into what it's doing:

- **Files** — via Gitea browse and the Web IDE (`vm_snapshots_and_ide.md`)
- **Shell / processes** — via the persistent shell and IDE terminal
- **Conversation / reasoning** — via the chat history and audit trail

But the **browser is a blind spot**. The agent navigates pages, fills forms, runs JavaScript, downloads files, and verifies its own work visually — and the user sees none of it. They see a tool call (`browser_navigate(url=...)`) and a text snapshot of the result, but never the actual page.

This gap matters in three concrete ways:

1. **Stuck states are invisible.** Cookie banners, CAPTCHAs, geo-blocks, login walls, A/B test variants the agent didn't expect — these are obvious to a human glancing at a screen and opaque from a tool log. The agent often loops on them for several phases before giving up.
2. **Verification is unverifiable.** When the agent claims "the page rendered correctly" or "the form was submitted," the user has to take its word for it. There's no equivalent of "show me what you see."
3. **Frontend work is impossible to review live.** When the agent is building a web UI (the cockpit-design use case), the user can pull the repo and run it locally — but that breaks the whole "agent does the work, I review it" loop. They should be able to *watch* the agent click around its own UI.

A shared browser window closes the gap. The cockpit gets a new component that shows the agent's live browser, and (in a second pass) lets the user take the wheel.

## Industry Context

### How Others Do It

| System | Pattern | Transport | Interaction | Notes |
|--------|---------|-----------|-------------|-------|
| **Browserless** | Live view via CDP screencast | WebSocket (JPEG frames) | Yes — input events relayed via CDP | Their commercial "live URL" feature; the de-facto reference design |
| **Steel.dev** | Session viewer with screencast | WebSocket | Yes | Open-source equivalent of Browserless live view |
| **Selenium Grid + noVNC** | Headed Chromium in container with Xvfb + x11vnc + noVNC | WebSocket (VNC protocol) | Yes — VNC native input | Battle-tested but heavy: needs display server, headed browser, three extra processes |
| **Kasm Workspaces** | Full-desktop streaming via KasmVNC | WebSocket (custom VNC fork) | Yes | Streams entire desktop, not just browser; overkill for our use case |
| **Playwright Trace Viewer** | Post-hoc replay from `.trace.zip` files | None (offline) | No — replay only | Great for debugging, useless for live observation |
| **Chrome DevTools Remote** | CDP frontend hosted at `chrome://inspect` | Direct CDP over WebSocket | Yes (full DevTools) | What CDP was designed for, but the UX is "DevTools," not "shared window" |
| **VNC-only (raw)** | Direct VNC client to a headed browser container | TCP (VNC) | Yes | Not browser-native, requires VNC client outside the cockpit |
| **Cursor / Devin** | Static screenshots in chat | HTTP (image upload) | No | Snapshots embedded in conversation; not live |
| **OpenAI Operator** | Built-in browser viewer in ChatGPT UI | Proprietary streaming | "Take control" handoff | Closed source; the UX is the gold standard for this category |
| **Tandem Browser** | Shared Electron browser, agent gets own tab/workspace | MCP / local HTTP | Yes — separation, not mutual exclusion | Contention solved by tab isolation, not pause/resume |
| **Browserbase (rrweb → CDP)** | Originally DOM-mutation replay (rrweb); rebuilt on CDP | WebSocket | N/A (recording) | **Switched away from rrweb** because it silently mis-rendered iframes, shadow DOM, canvas, and video — "looked correct while lying about what actually happened" |
| **rrweb** | Client-side DOM mutation capture | JSON events | Replay only | Cheap to store, but verification-hostile: replays can drift from reality |

### Key Takeaways

1. **CDP screencast is the modern default.** Browserless, Steel.dev, Vercel Labs `agent-browser`, and Browserbase all use it. It's native to Chromium, requires no extra processes, works with headless mode, and is designed exactly for this purpose. `Page.startScreencast` emits base64 JPEG (or PNG) frames over the existing CDP WebSocket; `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` handle input relay.

2. **rrweb is disqualified for our use case.** DOM-mutation capture is cheaper to store and transports as JSON, but Browserbase's rebuild story is definitive: rrweb can produce replays that look fine but don't match what the browser actually rendered — nested iframes, shadow DOM, canvas, and video push it past its limits. For a feature whose entire purpose is "verify what the agent actually saw," that failure mode is disqualifying.

3. **VNC is the legacy path.** It works, but it requires running a headed browser inside Xvfb with x11vnc and noVNC alongside — three extra processes per workspace. The upside is that it captures the entire viewport including OS-level dialogs (file pickers, native print prompts).

4. **CDP screencast has mandatory built-in backpressure.** Every frame must be acknowledged with `Page.screencastFrameAck` before the next is sent. We don't need to design our own flow control — if the client lags, frames naturally stop arriving. Our broker must ack correctly (after the frame is sent to the cockpit, not before).

5. **Direct CDP beats Playwright-relayed CDP.** browser-use documented significant latency wins by connecting to Chromium CDP directly rather than routing through Playwright's Node.js relay, which adds a second network hop. Our broker should open its own CDP WebSocket, not borrow Playwright's session.

6. **The hard part is not the viewer — it's the handoff.** Operator's "take control" UX is hard to copy. The agent and user fighting over the same browser tab causes race conditions. Systems solve this two ways: **mutual exclusion** (pause the agent while the user drives) or **tab isolation** (give the agent and user separate tabs and never contend). Tandem Browser uses the latter.

7. **Streaming bandwidth is the limiting factor at scale, not the design challenge for a single user.** CDP screencast at quality 60, 5 fps, 1280×720 is roughly 50–200 KB/s — entirely fine for one user watching one agent. The cockpit doesn't need to be optimized for hundreds of concurrent viewers.

8. **CDP is already enabled in our stack.** `src/tools/research/browser.py:90-101` launches Chromium with `--remote-debugging-port=9222` in remote workspaces. We don't need to add a screencast-capable browser — we already have one.

## Design

### Approach: CDP Screencast (Primary)

The agent's browser is already running with CDP enabled on port 9222 inside the workspace container. The cockpit gets a new component that:

1. Asks the orchestrator to open a streaming WebSocket to the agent's browser
2. Renders the JPEG frames it receives onto a `<canvas>`
3. (Phase 2) Captures user input on the canvas and relays it back

The orchestrator proxies the connection — it terminates the cockpit's WebSocket, opens its own connection to the agent pod's CDP port, and brokers messages between them. This keeps the existing auth model (Keycloak token at the cockpit ↔ orchestrator boundary, MCP/internal auth at the orchestrator ↔ agent boundary) and avoids exposing CDP to the public internet.

```
Cockpit (Angular)                    Orchestrator                    Agent Workspace
┌────────────────────┐               ┌────────────────┐              ┌──────────────┐
│ SharedBrowser      │  WebSocket    │ /api/jobs/{id}/│  WebSocket   │ Chromium     │
│ Component          │ ◄───────────► │ browser/stream │ ◄──────────► │ CDP :9222    │
│  - <canvas>        │  (auth: KC)   │                │  (internal)  │              │
│  - input handlers  │               │ Frame router   │              │ Page.start   │
│  - control toggle  │               │ Auth + ratelmt │              │ Screencast   │
└────────────────────┘               └────────────────┘              └──────────────┘
```

### Why CDP Screencast (vs noVNC and rrweb)

| Concern | CDP Screencast | noVNC | rrweb |
|---------|---------------|-------|-------|
| Already enabled in our workspace? | Yes (browser.py:90) | No — needs Xvfb + x11vnc + noVNC | No — needs client-side injection |
| Extra processes per workspace | 0 | 3 (Xvfb, x11vnc, websockify) | 0 |
| Headless-compatible | Yes | No — requires headed browser | Yes |
| Transport | JPEG/PNG over WebSocket | VNC protocol over WebSocket | JSON DOM events over WebSocket |
| Input relay | CDP `Input.dispatch*Event` | Native VNC input | Custom event re-dispatch |
| **Fidelity to what the browser rendered** | **Pixel-perfect** | **Pixel-perfect** | **Unreliable** — nested iframes, shadow DOM, canvas, video can silently diverge from reality |
| File pickers / native dialogs | Not visible | Visible | Not visible |
| Multi-tab support | Native (one WS per target) | Single display, all tabs visible | Per-tab |
| Built-in backpressure | Yes (`screencastFrameAck`) | Yes (VNC framebuffer updates) | No — must implement |
| Implementation cost | Cockpit component + orchestrator proxy | All of the above + workspace image rebuild + headed mode | Cockpit component + rrweb agent injection |

**Decision: CDP screencast.** rrweb is cheaper but cannot be trusted for a verification use case — Browserbase's migration story is the cautionary tale. noVNC works but requires standing up a display server and headed browser per workspace. CDP screencast is already wired up, natively backpressured, and pixel-accurate.

The one thing noVNC offers that screencast doesn't is OS-level dialog visibility (file pickers, native print). If this becomes a problem, a future "headed mode under Xvfb, still streamed via CDP screencast" hybrid is viable — see "What We Lose By Going Headless" below.

### Component: `SharedBrowserComponent` (Cockpit)

A new standalone Angular component, lazy-loaded into the job detail view as a tab next to "Logs" / "Files" / "IDE":

```
cockpit/src/app/features/job/shared-browser/
├── shared-browser.component.ts
├── shared-browser.component.html
├── shared-browser.component.scss
└── shared-browser.service.ts   # WebSocket client + frame decode
```

**State (signals):**
- `connectionState: 'idle' | 'connecting' | 'connected' | 'error'`
- `controlMode: 'observe' | 'control'`
- `currentUrl: string | null`
- `viewportSize: { width, height }`
- `frameRate: number` (for stats display)

**UI:**
- Header: current URL (read-only), "Connect" / "Disconnect" button, "Take control" toggle, FPS indicator
- Body: `<canvas>` element sized to the viewport, with the latest frame rendered
- Footer: brief status text (e.g., "Observing — agent has control" / "You have control — agent paused")

**Frame rendering:** Each incoming JPEG frame is decoded via `createImageBitmap()` and drawn to the canvas. Skip frames if the previous bitmap hasn't finished decoding to avoid backlog under slow networks.

### Orchestrator Endpoint

```
GET /api/jobs/{job_id}/browser/stream    (WebSocket upgrade)
```

**Auth:** Bearer token (Keycloak) or MCP token, same as all other orchestrator endpoints. The orchestrator validates that the requesting user owns the job (or is a project member).

**Lifecycle:**

1. Cockpit connects with `Sec-WebSocket-Protocol: srw-browser-stream-v1`
2. Orchestrator validates auth + job ownership
3. Orchestrator looks up the agent pod IP from `agents.last_known_ip` (already tracked for heartbeats)
4. Orchestrator opens a WebSocket to the agent's browser broker endpoint (see below)
5. Orchestrator sends a "start" message: `{op: 'start', quality: 60, max_fps: 5}`
6. Frames flow agent → orchestrator → cockpit as binary WebSocket messages
7. Input events flow cockpit → orchestrator → agent as JSON messages
8. On disconnect, orchestrator sends a "stop" message and tears down both WebSockets

**Why proxy through the orchestrator instead of direct connection?**

- **Auth.** The agent pod has no Keycloak integration; the orchestrator does. Putting auth at the agent boundary would mean reimplementing it.
- **Network.** Agent pods are on the cluster internal network. Direct cockpit-to-pod connections require either an Ingress per pod (operationally painful) or a tunnel (which is what the orchestrator already is).
- **Lifecycle coupling.** The orchestrator already knows when an agent pod is created, suspended, restored, or destroyed. It can cleanly close the stream on lifecycle transitions.
- **Rate limiting and audit.** Centralized policy lives at the orchestrator, not duplicated across agents.

### Agent-Side Broker

A new minimal endpoint on the agent's existing API server (`agent.py`, port 8080):

```
GET /browser/stream    (WebSocket upgrade, internal auth only)
```

The broker is a thin adapter between the orchestrator's protocol and Chromium's CDP:

```python
# Pseudo-code in src/api/browser_stream.py
import os, base64, json, asyncio

# Configurable at the workspace level — mirrors vercel-labs/agent-browser naming
STREAM_FORMAT = os.getenv("SRW_BROWSER_STREAM_FORMAT", "jpeg")
STREAM_QUALITY = int(os.getenv("SRW_BROWSER_STREAM_QUALITY", "60"))
STREAM_MAX_WIDTH = int(os.getenv("SRW_BROWSER_STREAM_MAX_WIDTH", "1280"))
STREAM_MAX_HEIGHT = int(os.getenv("SRW_BROWSER_STREAM_MAX_HEIGHT", "720"))
STREAM_EVERY_NTH = int(os.getenv("SRW_BROWSER_STREAM_EVERY_NTH", "2"))  # ~5fps

async def browser_stream(websocket):
    # Connect directly to Chromium CDP — NOT through Playwright's CDPSession.
    # browser-use documented meaningful latency wins by avoiding the Playwright
    # Node relay for high-frequency CDP traffic.
    target_id = await find_active_target_id()
    cdp = await connect_cdp(f"ws://localhost:9222/devtools/page/{target_id}")

    await cdp.send("Page.startScreencast", {
        "format": STREAM_FORMAT,
        "quality": STREAM_QUALITY,
        "maxWidth": STREAM_MAX_WIDTH,
        "maxHeight": STREAM_MAX_HEIGHT,
        "everyNthFrame": STREAM_EVERY_NTH,
    })

    async def cdp_to_client():
        async for msg in cdp:
            if msg.method == "Page.screencastFrame":
                # Send frame first, then ack. CDP won't send the next frame
                # until it receives the ack — this is the protocol's built-in
                # backpressure. If the cockpit WebSocket is slow, frames stop
                # arriving naturally; no buffer needed on our side.
                payload = {
                    "type": "frame",
                    "data": msg.params.data,  # already base64
                    "metadata": msg.params.metadata,  # deviceWidth, deviceHeight, scale, offsetTop
                }
                await websocket.send_text(json.dumps(payload))
                await cdp.send("Page.screencastFrameAck", {
                    "sessionId": msg.params.sessionId,
                })
            elif msg.method == "Page.frameNavigated":
                # Active URL changed — tell the cockpit for the header display
                await websocket.send_text(json.dumps({
                    "type": "url_changed",
                    "url": msg.params.frame.url,
                }))

    async def client_to_cdp():
        async for raw in websocket:
            msg = json.loads(raw)
            if msg["type"] == "input":
                await relay_input_event(cdp, msg)  # Phase 2 only
            elif msg["type"] == "stop":
                break

    try:
        await asyncio.gather(cdp_to_client(), client_to_cdp())
    finally:
        await cdp.send("Page.stopScreencast")
        await cdp.close()
```

**Frame metadata matters.** Vercel Labs `agent-browser` issue #632 documents a real bug: when CDP downscales a frame to fit `maxWidth`/`maxHeight`, the frame metadata still reports the original `deviceWidth`/`deviceHeight`. If the cockpit sizes its canvas backing store to the metadata dimensions and draws the downscaled JPEG into it, the result is blurry — especially on portrait/HiDPI viewports. We forward both the raw bytes and the full metadata, and the cockpit is responsible for reading the actual image dimensions from the decoded bitmap, not trusting the metadata blindly.

**Target selection:** The agent maintains the "current" browser target (the page Playwright is driving). The broker connects to that target. When the agent navigates or opens a new tab via `browser_navigate`, the active target may change — the broker should resubscribe.

**Concurrency note:** Multiple CDP clients can attach to the same target simultaneously. Playwright keeps its own CDP session for driving the browser; the screencast session is independent and doesn't interfere. This is specifically why we can have a screencast subscriber without pausing the agent's tool calls in Phase 1.

### Phase 1: View-Only

The first deliverable is **observation only** — no input relay. This unblocks 80% of the value (the user can see what the agent sees) without the complexity of the user/agent control handoff.

**Scope:**
- Cockpit component renders frames
- Orchestrator proxy
- Agent broker streams frames
- "Take control" button is present but disabled, with a tooltip explaining it's coming

**Out of scope for Phase 1:** input relay, agent pause/resume coordination, multi-tab switching.

### Phase 2: Take Control

In Phase 2, the user can click a "Take control" button to start sending input events. This requires solving the user/agent contention problem.

**Control handoff protocol:**

1. User clicks "Take control" → cockpit sends `{op: 'request_control'}` to orchestrator
2. Orchestrator calls `POST /api/jobs/{id}/browser/pause` on the agent
3. Agent pauses all browser tool calls (the next browser tool invocation blocks until the user releases control)
4. Orchestrator confirms control granted; cockpit enables input handlers
5. User interacts → cockpit sends `{op: 'input', type: 'mouse'|'key'|'wheel', ...}` events
6. Orchestrator relays to agent broker, which forwards to CDP via `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`
7. User clicks "Release control" → cockpit sends `{op: 'release_control'}`
8. Orchestrator calls `POST /api/jobs/{id}/browser/resume`
9. Agent's blocked browser tool call (if any) returns and execution continues

**Why pause the agent during user control?**

Without pausing, the agent might call `browser_click(selector="#submit")` while the user is mid-form-fill. The two streams of CDP commands collide and produce inconsistent state. The cleanest model is mutual exclusion: either the agent or the user is driving, never both.

**Alternative model — tab isolation (Tandem Browser pattern):** Rather than pause/resume, a different solution is to give the agent and the user their own browser tabs and let them work in parallel on separate targets. The user "takes control" of a specific tab, not the whole browser; the agent keeps driving its own tab uninterrupted. This avoids contention entirely, but it only helps when the user's goal is to work *alongside* the agent — not when they want to intervene in what the agent is currently doing (e.g., solve a CAPTCHA on the agent's active tab). For our primary use cases (observation, CAPTCHA rescue, verification), mutual exclusion is the better default, with tab isolation reserved for a future collaborative mode once the basics work.

**Coordinate mapping.** This is the single most error-prone part of Phase 2 and deserves its own attention:

- `Input.dispatchMouseEvent` expects coordinates in **CSS pixels relative to the viewport**, not device pixels, not image pixels.
- The cockpit canvas may be rendered at an arbitrary size (e.g., 800px wide) while the actual browser viewport is 1280 CSS px wide and the streamed frame was downscaled to fit `maxWidth`.
- Every click event must be mapped: `cdp_x = canvas_x * (viewport_css_width / canvas_display_width)`, and the same for Y.
- `deviceScaleFactor` (from the frame metadata) adds another layer if we ever support HiDPI passthrough — but for the initial version we can assume `deviceScaleFactor=1` at the CDP level and let the cockpit handle any visual HiDPI rendering on its end.
- Keyboard events are simpler since they carry key codes, not coordinates — but watch out for modifier key state synchronization (the cockpit must track Shift/Ctrl/Alt/Meta state and include it in every event).

A `coordinateMapper(canvasEl, frameMetadata)` helper in `shared-browser.service.ts` centralizes this logic so the component never touches raw coordinates.

**Audit:** When the user takes control, the orchestrator writes a record to MongoDB audit trail (`agent_audit` collection): `{event: 'user_browser_control', job_id, user_id, started_at, ended_at, action_count}`. Agents reading their own audit trail will see "user took control of the browser for 4 minutes between 14:02 and 14:06" and can factor that into their reasoning.

### Phase 3 (Future): Control Hints From Agent

The most powerful pattern is the agent *asking* for help:

```
agent: "I'm hitting a CAPTCHA on this page. Could you solve it for me?"
       [calls request_user_browser_control(reason="CAPTCHA on login page")]
       → cockpit shows a notification + auto-opens the shared browser tab
       → user solves CAPTCHA, releases control
       → agent continues
```

This mirrors the `notify_user` / `ask_user` pattern from `notify_user_tool.md` and `email_and_mobile.md`. It's the most useful version of this feature, but it depends on Phase 2 being in place.

Out of scope for this design doc — flagged here so the protocol in Phase 2 leaves room for it (specifically: the `request_control` message needs to support an `initiated_by: 'user' | 'agent'` field).

### Quality / Bandwidth Tuning

CDP screencast parameters are configurable per deployment via environment variables (matching the `vercel-labs/agent-browser` convention):

| Env var | Default | Rationale |
|---------|---------|-----------|
| `SRW_BROWSER_STREAM_FORMAT` | `jpeg` | PNG is lossless but 3–5× larger. Browserbase uses PNG for their recording pipeline and re-encodes to H.264 asynchronously — overkill for live view. JPEG is universally supported and fast |
| `SRW_BROWSER_STREAM_QUALITY` | 60 | Sweet spot for text legibility vs bandwidth |
| `SRW_BROWSER_STREAM_MAX_WIDTH` | 1280 | Matches the cockpit's typical browser viewport |
| `SRW_BROWSER_STREAM_MAX_HEIGHT` | 720 | 16:9 |
| `SRW_BROWSER_STREAM_EVERY_NTH` | 2 | ~5 fps if Chromium renders at 10 fps; smooth enough for observation |

The cockpit may also override via query string on the WebSocket URL (e.g., `?quality=80&maxWidth=1600`), so a user on a fast connection can negotiate higher quality or drop quality on a slow one. No need for runtime adaptive bitrate in v1.

**Why env vars over hard-coding:** issue #632 in `vercel-labs/agent-browser` is the cautionary tale — their hard-coded 720p cap forced severe downscaling on portrait/HiDPI viewports, producing a blurry stream with no way for users to fix it. Configurability is cheap up-front and avoids that class of bug entirely.

**Estimated bandwidth at defaults:** 50–200 KB/s during active page changes, near zero on a static page (screencast only emits frames when the viewport changes).

### What We Lose By Going Headless

CDP screencast captures what the browser renders into its viewport. It does **not** capture:

- **Native file pickers** (the OS-provided "choose file" dialog)
- **Native print dialogs**
- **Browser-level modals** outside the page viewport (Chromium's built-in auth prompts, proxy auth, certificate warnings)
- **OS-level alerts** (WebAuthn/FIDO prompts, screen sharing permission dialogs)
- **PDF viewer UI** (Chromium's built-in PDF viewer renders to a native surface, not the page)

For most of our agent's work this is fine — Playwright intercepts file choosers via `page.on('filechooser')`, and the agent rarely hits native auth prompts in scripted flows. But there are two known gaps worth naming:

1. **PDF-heavy jobs.** When the agent navigates to a PDF, Chromium's built-in PDF viewer is partially outside the CDP screencast surface (depends on headless mode flags). Users watching the stream may see a blank page instead of the PDF. Workaround: the agent downloads the PDF instead of viewing it in-browser.
2. **CAPTCHA/auth challenges that escape into native UI.** Rare, but they happen (especially WebAuthn). These are invisible in our stream.

**Escape hatch:** If the gap becomes painful, we can switch Chromium to headed mode under Xvfb while *still using CDP screencast* (not VNC) for transport. This gives us the OS-dialog visibility of a full desktop stream with the simplicity of the CDP protocol. It requires adding Xvfb to the workspace image — one extra process, not three — and is a reasonable Phase 3 evolution if we see real-world use cases hitting the limit. The current design should leave room for it (specifically: don't couple "CDP screencast" to "headless mode" in the code).

### What Could Go Wrong

| Risk | Mitigation |
|------|-----------|
| Chromium not running when user clicks "Connect" | Browser starts lazily on first agent tool call. If no browser is running, orchestrator returns 409 with message "Agent has not opened the browser yet" |
| Agent navigates while user is observing | Active target changes — broker resubscribes to new target. Brief gap in stream is acceptable |
| CDP port not exposed (local dev backend) | Feature is disabled for local backends; cockpit shows "Browser sharing not available in local mode" |
| User loses connection mid-control (Phase 2) | Orchestrator detects WebSocket close, automatically calls `/browser/resume` to unblock agent. Timeout safety: 5 minutes max control session if no activity |
| Agent and user both modify state in Phase 2 | Pause/resume protocol prevents this by design |
| Sensitive content visible in screencast (passwords, tokens) | Same as the IDE feature — the user *is* the job owner, this is their data. Same auth boundary applies |
| Bandwidth abuse | Per-job rate limit at orchestrator: max 1 active stream per job, max 5 stream-minutes per minute across all jobs per user |
| Frame backlog under slow client | CDP's own `screencastFrameAck` handles this — if the cockpit stops acking via the broker, CDP stops sending. No buffering on the orchestrator side |
| **HiDPI / portrait viewport blur** (vercel-labs #632) | Forward raw frame bytes + full metadata; cockpit reads actual image dimensions from the decoded bitmap rather than trusting metadata. Canvas backing store sized to actual image, CSS display size independent |
| **Viewport resize during stream corrupts frames** (Browserless) | Disable the cockpit's resize handler while connected. If the user resizes the browser window, restart the screencast session rather than changing viewport mid-stream |
| **Agent creates new browser context instead of reusing** (Browserless gotcha) | Our broker connects to the *active* CDP target and doesn't create new contexts. Agent code that does `browser.new_context()` would stream the wrong target — document this as an invariant in `src/tools/research/browser.py` |
| **Coordinate mapping bug — click lands in wrong place** (Phase 2) | Centralized `coordinateMapper()` helper in one place, unit-tested with several viewport/canvas ratios. Manual QA with real sites before enabling for users |
| **Sites serve different content to headless Chromium** | Known limitation, not browser-share-specific. If it becomes a problem, Xvfb+headed escape hatch is available (see "What We Lose By Going Headless") |
| **Agent opens a download / native dialog the user can't see** | Out-of-scope for the stream; agent's audit log already captures download events. Consider surfacing a "agent downloaded X" hint in the cockpit header as future polish |

## Implementation Plan

### Phase 1 — View-Only

#### Files to Create

| File | Purpose |
|------|---------|
| `src/api/browser_stream.py` | Agent-side CDP-to-WebSocket broker |
| `orchestrator/services/browser_proxy.py` | Orchestrator-side WebSocket proxy + auth + rate limiting |
| `cockpit/src/app/features/job/shared-browser/shared-browser.component.ts` | Angular component |
| `cockpit/src/app/features/job/shared-browser/shared-browser.component.html` | Template |
| `cockpit/src/app/features/job/shared-browser/shared-browser.component.scss` | Styles |
| `cockpit/src/app/features/job/shared-browser/shared-browser.service.ts` | WebSocket client + frame decode |
| `tests/test_browser_stream.py` | Broker unit tests with mocked CDP |

#### Files to Modify

| File | Change |
|------|--------|
| `agent.py` | Mount the `browser_stream` WebSocket route on the existing API server |
| `src/tools/research/browser.py` | Expose helper to find the active CDP target ID for the broker |
| `orchestrator/main.py` | Add `GET /api/jobs/{job_id}/browser/stream` WebSocket route |
| `orchestrator/database/postgres.py` | Add `get_agent_endpoint(job_id)` helper if not present (for the proxy to find the agent pod) |
| `cockpit/src/app/features/job/job-detail.component.ts` | Add "Browser" tab next to existing tabs, lazy-load the new component |

#### Implementation Order

1. **Agent broker** — Implement `browser_stream.py`, test against a locally running Chromium with CDP. Verify frames flow over WebSocket.
2. **Orchestrator proxy** — Implement the proxy endpoint with auth. Test end-to-end: cockpit dev tools → orchestrator → agent → frames received.
3. **Cockpit component** — Build the component, render frames to canvas. Wire it into the job detail view.
4. **Polish** — Connection state UI, error handling, FPS indicator, "browser not running" empty state.
5. **Audit logging** — Log stream open/close events to MongoDB audit trail.

### Phase 2 — Take Control

#### Files to Create

| File | Purpose |
|------|---------|
| `tests/test_browser_control_handoff.py` | Test the pause/resume protocol end-to-end |

#### Files to Modify

| File | Change |
|------|--------|
| `src/api/browser_stream.py` | Handle `op: 'input'` messages, dispatch via CDP `Input.*` |
| `agent.py` | Add `POST /browser/pause` and `POST /browser/resume` endpoints |
| `src/tools/research/browser.py` | Add a `_user_control_lock` checked at the start of every browser tool call |
| `orchestrator/main.py` | Add `POST /api/jobs/{job_id}/browser/pause` and `/resume` |
| `orchestrator/services/browser_proxy.py` | Relay input events bidirectionally |
| `cockpit/.../shared-browser.component.ts` | Enable input handlers when in `control` mode, send events |
| `cockpit/.../shared-browser.component.html` | Enable the "Take control" button |

## Open Questions

1. **Where does the "Browser" tab live in the cockpit?** Job detail view is the obvious answer for jobs, but persistent threads also use the browser. Probably needs to live in both (`pages/job-detail` and `simple/persistent-chat`).

2. **Should the agent see when the user is watching?** A subtle "user is observing" hint in the agent's context could be valuable — it might encourage the agent to be more deliberate. Or it might cause overthinking. Worth a small experiment after Phase 1 ships.

3. **Headed mode for visual fidelity?** CDP screencast in headless mode renders correctly but some sites detect headless mode and serve different content (e.g., bot challenges). If this becomes a problem, switching to headed mode under Xvfb is a future option — and at that point we'd already be most of the way to noVNC anyway. Defer until we see evidence it's needed.

4. **Multi-tab support.** The first version connects to the active target. If the agent opens multiple tabs (rare today), the user only sees the active one. Adding a tab strip in the cockpit is a Phase 3 polish item.

5. **Recording.** Should we save the stream as a video for post-hoc review? Playwright already has trace files for that purpose (`playwright trace`), and recording every stream is expensive. Probably no.

## Future Extensions

- **Agent-initiated control requests** (Phase 3 above) — The most valuable evolution.
- **Persistent thread integration** — When the agent is in persistent/interactive mode, the shared browser becomes a real-time collaboration surface, not just an observability tool.
- **Tab-isolation collaborative mode** — A second Phase 2 variant (Tandem Browser pattern) where the user gets their own tab in the agent's browser, operating in parallel without pausing the agent. Useful for "follow along while the agent researches" use cases.
- **Headed-mode-under-Xvfb escape hatch** — If native dialogs / PDF viewer / WebAuthn gaps become painful, run Chromium headed under Xvfb while still transporting frames via CDP screencast (not VNC). Adds one process, not three.
- **Multi-viewer support** — Project members watching the same agent browser. Trivial extension of the proxy (fan out frames to N WebSocket clients per agent connection). Each viewer independently acks frames back to its own broker session.
- **Iframe-embeddable viewer** (Steel.dev pattern) — Extract the cockpit's shared browser component as a standalone embeddable webapp served by the orchestrator, reachable via a signed short-lived URL. Enables read-only share links ("look at what my agent just did") and MCP exposure without coupling to the full cockpit.
- **Browser session export** — Capture the current browser state (cookies, localStorage, open tabs) into a snapshot, similar to the VM snapshot feature in `vm_snapshots_and_ide.md`. Useful for resuming work after a job completes.
- **MCP exposure** — A `view_agent_browser` MCP tool that returns a current screenshot, for use in Claude Code and external integrations. Cheaper than live streaming and fits the polling model of MCP better.
- **Post-hoc recording** — CDP screencast frames can be re-encoded asynchronously into HLS/fMP4 for session replay (Browserbase's approach). Explicitly out of scope for v1 — Playwright trace files cover the debugging use case — but the frame transport we're building is the raw input a recording pipeline would consume.

## References

Research informing this design (see conversation history for full annotated notes):

- [Browserless — Screen Recording & LiveURL docs](https://docs.browserless.io/baas/interactive-browser-sessions/screencasting)
- [Browserbase — "This week we fixed the worst part of Browserbase"](https://www.browserbase.com/blog/session-recordings) (rrweb → CDP screencast migration story)
- [Steel.dev — Human-in-the-Loop Controls](https://docs.steel.dev/overview/sessions-api/human-in-the-loop)
- [browser-use — "Closer to the Metal: Leaving Playwright for CDP"](https://browser-use.com/posts/playwright-to-cdp)
- [vercel-labs/agent-browser — Issue #632 (HiDPI stream resolution)](https://github.com/vercel-labs/agent-browser/issues/632)
- [Chrome DevTools Protocol — Page domain (`startScreencast`, `screencastFrameAck`)](https://chromedevtools.github.io/devtools-protocol/tot/Page/)
- [Chrome DevTools Protocol — Input domain (`dispatchMouseEvent` coordinate system)](https://chromedevtools.github.io/devtools-protocol/tot/Input/)
- [Tandem Browser — shared human-AI browser with tab-isolation pattern](https://github.com/hydro13/tandem-browser)
- [rrweb — record and replay the web](https://www.rrweb.io/)
