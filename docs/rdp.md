---
tags:
  - discussion
  - deferred
  - cockpit
  - browser
  - remote-desktop
aliases:
  - rdp
  - remote desktop
  - browser desktop
related:
  - "[[dynamic_canvas]]"
  - "[[shared_browser]]"
  - "[[vm_snapshots_and_ide]]"
---

# Remote Desktop ("RDP") — Discussion & Deferral

**Status:** Deferred. This is a decision / discussion record, **not** a feature spec — the
scope is still undecided, so there's no design doc or implementation plan yet. Captured
2026-05-30.

## TL;DR

The idea: add a browser-based remote desktop ("RDP") so a less-technical user can "visit"
the machine the agent runs on.

The finding: the concrete use cases that motivated it are largely **already covered** by two
features we've designed — [[shared_browser]] (CDP screencast of the agent's browser) and the
`url` kind of [[dynamic_canvas]]. The only thing a true remote desktop adds beyond those is
**non-web GUI** (file manager, native apps, OS dialogs).

**Decision:** defer the remote-desktop / RDP capability. Build the dynamic canvas
([[dynamic_canvas]]) properly first — it's the container any future "desktop" mode plugs into
anyway. Revisit RDP only on concrete evidence of a non-web-GUI need (see *Triggers*).

## How this came up

The ask: non-technical users should be able to look at and use the machine the agent works
on — e.g.

- test an app the agent built **without knowing port-forwarding**, or fighting **CORS /
  backend-API origins**, by using a browser *on the machine*; and
- **see (and take over) the browser window the agent already opened** — e.g. the agent fills
  in a tax form, the user opens the desktop, reviews, and submits.

"RDP" was shorthand for "remote desktop in the browser," not a requirement for the RDP
protocol specifically. (For an all-Linux stack, browser-delivered remote desktop is normally
noVNC over VNC, or an RDP→HTML5 gateway like Guacamole; the browser never speaks RDP
directly.)

## What the codebase looks like today (the rails any version would reuse)

- **Workspaces** are either Kubernetes pods (`orchestrator/services/container_provisioner.py`)
  or KubeVirt VMs (NATS `vm.lifecycle.*`). The same software ships in both images.
- **IDE in the browser:** code-server runs headless on `0.0.0.0:38080` inside each workspace
  (`docker/workspace-entrypoint.sh:56`). The browser reaches it through a **single
  orchestrator reverse-proxy route** — `/api/ide/{job_id}/proxy/{path}`
  (`orchestrator/services/ide_proxy.py` + the route in `orchestrator/main.py`) — which streams
  **HTTP and WebSocket** to `http://{pod_ip}:38080`. There is **no per-workspace ingress**; the
  orchestrator is the only public door. Auth is the **BFF cookie** (`require_approved_user` +
  `user_can_access_ide_entity`).
- **The agent's browser is headless.** `src/tools/research/browser.py:80` launches
  `agent-chromium --headless=new` with `--remote-debugging-port=9222`, driven over CDP. The
  workspace image has **no desktop environment** at all.

Two consequences for any "visit the machine" feature:

1. The proxy + BFF auth + pod-IP resolution are protocol-agnostic (HTTP + WS on one port) and
   would be reused by noVNC exactly as they're reused by code-server.
2. To *see* the agent's browser as a window, the browser has to render somewhere — i.e. it
   must stop being headless. That's the crux that splits the options below.

## Relationship to the two existing designs

- **[[dynamic_canvas]]** is the artifacts-style surface — a multi-kind tile grid the agent
  writes into. It already lists `shared_browser` and `url` as **kinds**, and has a "delivery
  mode B" (signed short-lived URL → real browser tab on an isolated domain). Any "desktop"
  capability should be **one more canvas kind**, not a separate UI tab.
- **[[shared_browser]]** is watch / take-control of the agent's browser. It deliberately chose
  **CDP screencast and rejected noVNC/VNC desktop**, because noVNC needs Xvfb + a *headed*
  browser + three extra processes, while screencast reuses the existing headless Chromium and
  is pixel-accurate + natively backpressured. A remote desktop is exactly the noVNC path it
  turned down — so adding one is a **conscious reversal** of that call.

## Needs → what already covers them

| Motivating need | Covered by |
|---|---|
| See **and take over** the browser the agent opened (tax form) | [[shared_browser]] — view (Phase 1) + take-control (Phase 2) |
| Test an app without port-forwarding / CORS | Partly — a [[dynamic_canvas]] `url` tile proxies the workspace port, but inserts the orchestrator origin (the rewriting we wanted to avoid). A browser *truly on the machine* is cleaner — and that means a **headful** browser. |
| General "visit the machine" / RDP | **Not covered** — the only genuinely net-new piece |

**The genuine gap:** the only thing a true desktop adds beyond screencast + the `url` kind is
**non-web GUI** — file manager, native apps, the OS file-picker / print / PDF-viewer dialogs
([[shared_browser]] lists these as screencast's blind spots), and arbitrary windows.

## Options considered (for when we revisit)

All three live *inside* the canvas-with-modes vision (each is a kind), and all reuse the
existing authenticated proxy. Ordered by increasing capability:

**A — Build what's specced (no desktop).** Ship [[shared_browser]] (view + take-control) and
the `url` kind. Covers the tax-form case and most app-testing. Lightest; reuses headless
Chromium and the existing proxy. Trade-off: app-testing goes through the proxy (origin
rewriting); no native GUI.

**B — On-machine browser (the screencast "escape hatch").** Flip the agent's Chromium to
**headful under Xvfb** and give the user a drivable browser (real omnibox), still streamed via
**CDP screencast** — the exact escape hatch [[shared_browser]] names. Clean localhost / CORS /
OAuth testing on a browser genuinely on the machine, plus seeing the agent's browser. **One**
extra process (Xvfb), not three. No file manager / native apps.

**C — Full Linux desktop.** A new `desktop` canvas kind via **noVNC** (Xvfb + x11vnc +
websockify in the workspace image, proxied like the IDE). File manager, native apps, OS
dialogs, arbitrary windows, a real on-machine browser — closest to the original "RDP" picture.
Heaviest; consciously reverses [[shared_browser]]'s no-VNC decision, justified only by needing
non-web GUI.

> **Substrate note** (the question that prompted this): a desktop does **not** require a
> dedicated VM. Xvfb is a userspace framebuffer — no GPU, no privileged pod — so it runs fine
> in the existing **container** workspaces; the same image change reaches the VMs for free.

## Why deferred

- The concrete, stated use cases are already covered by features we've designed (option A
  territory) — so the desktop is not on the critical path for them.
- The desktop's only unique value (non-web GUI) is speculative until we see real demand —
  classic YAGNI.
- A desktop would be a **canvas kind**, so [[dynamic_canvas]] is a prerequisite regardless.
  Building the canvas first is strictly higher-leverage and loses nothing.

## Triggers to revisit

Pick this back up if we see:

- recurring need for **native GUI** on the machine (file manager, a non-web app, an office
  suite, etc.);
- real pain from screencast's blind spots in [[shared_browser]] (native file-picker, print,
  PDF viewer, WebAuthn);
- users struggling with **proxy-origin / CORS** issues when testing apps through the `url` kind
  (→ argues for option B, the on-machine headful browser);
- product demand for a "feels like a real computer" experience for non-technical users.

## References

- [[dynamic_canvas]] — the artifacts-style multi-kind surface (the container).
- [[shared_browser]] — watch/control the agent's browser via CDP screencast; the no-noVNC
  decision and its named Xvfb escape hatch.
- [[vm_snapshots_and_ide]] — the IDE-in-browser feature whose proxy + auth rails any desktop
  would reuse.
- Code: `orchestrator/services/ide_proxy.py`, the `/api/ide/{job_id}/proxy/` route in
  `orchestrator/main.py`, `docker/workspace-entrypoint.sh:56` (code-server on `:38080`),
  `src/tools/research/browser.py:80` (headless Chromium on CDP `:9222`).
