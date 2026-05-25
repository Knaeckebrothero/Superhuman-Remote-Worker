# Browser Workspace Executor — Move CDP control onto the workspace

> **Status**: Phases 1+2 implemented + unit-tested (2026-05-25); autonomous mode deprecated (Phase 3 dropped). Pending workspace+agent image rebuild and deploy; not yet exercised end-to-end on the cluster. Phase 4 (remove the 9222 exposure) is blocked on migrating `papers.py` (see §10/R6).
> **Created**: 2026-05-25
> **Trigger**: Remote browser automation is broken cluster-wide (diagnosed from session `b4478b88`). See "Root cause" below.
>
> **Implemented in this pass:**
> - `docker/browser-exec` — workspace daemon+client (browser-use local launch, unix socket, JSON-over-stdout).
> - `docker/Dockerfile.workspace` — installs `browser-use`, ships `browser-exec`.
> - `src/tools/context.py` — `browser_exec()` helper; `close_browser()` → daemon shutdown; `get_browser_session()` is now local/dev only.
> - `src/tools/research/browser_direct.py` — nine tools dispatch through `_run_action` (remote → `browser_exec`, local → in-process), keeping URL validation + nonce wrapping agent-side.
> - `src/tools/research/browser.py` + `research/__init__.py` — `browse_website`/`download_from_website` unregistered; shared helpers retained for `papers.py`.
> - Tests updated (`tests/tools/research/test_browser_tools.py`): obsolete classes removed, dispatch + `browser_exec` parsing covered. 81 browser / 209 tools tests pass.

---

## 1. Root cause (recap)

`browser_navigate` and friends fail on the cluster with:

```
Failed to establish CDP connection to browser: [Errno 111] Connect call failed ('<pod-ip>', 9222)
```

The browser **controller** (the `browser-use` library) runs **inside the agent process** and drives Chrome — which runs in the **workspace pod** — over CDP across the pod network. `_start_remote_chromium()` launches Chrome with `--remote-debugging-address=0.0.0.0`, but **Chrome 147 (bundled by Playwright 1.59.0) ignores that flag and binds CDP to `127.0.0.1` only** (a deliberate Chrome hardening). The on-pod readiness probe (`curl localhost:9222`) passes, the URL is rewritten to the pod IP, and the agent pod's cross-pod dial is then refused.

Verified live: Chrome logs `DevTools listening on ws://127.0.0.1:9222/...`; `curl localhost:9222` works on the pod, `curl <pod-ip>:9222` is refused. The NetworkPolicy already allows 9222 and is not the cause.

This is effectively a regression from the Playwright→1.59 bump and affects **every** remote browser call, not just this session.

## 2. Goal

Make the browser a normal SSH-proxied tool, like every other tool: **logic stays on the agent, execution happens on the workspace, only results cross the boundary.** CDP never leaves the workspace's loopback. No cross-pod CDP, no port forwarding, no SSH tunnel.

Non-goal: changing the agent-facing tool contract. The nine `browser_*` tools keep their names, signatures, and output shape (`{dom, screenshot, url, title}` with `[N]` element refs). The LLM's interaction model is unchanged.

## 3. Target architecture

```
 AGENT POD                                WORKSPACE POD
 ┌─────────────────────────────┐         ┌──────────────────────────────────┐
 │ browser_navigate(url) tool  │         │  browser-exec  (daemon)           │
 │  • validate_url_with_config │  SSH    │   • browser-use BrowserSession    │
 │  • backend.exec_command(    │ ──────► │     (LOCAL launch — owns Chrome)  │
 │      "browser-exec navigate"│ exec    │   • holds page + selector map     │
 │    )                        │         │   • unix socket /tmp/browser.sock │
 │  • parse JSON stdout        │ ◄────── │   • prints JSON to stdout         │
 │  • wrap_with_nonce(result)  │ result  │            │ loopback CDP/pipe     │
 └─────────────────────────────┘         │         ┌──▼─────────┐            │
                                          │         │  Chromium  │            │
                                          │         └────────────┘            │
                                          └──────────────────────────────────┘
```

- The agent's tool functions keep all **logic**: URL validation, nonce wrapping, screenshot/DOM shaping flags, feeding results to the LLM.
- A new **`browser-exec`** program on the workspace holds a persistent `browser-use` `BrowserSession` (in local-launch mode — browser-use owns the Chrome process over loopback/pipe; no `--remote-debugging-port` at all). It performs one action per request and returns JSON.
- The agent ↔ workspace boundary carries only SSH commands and JSON results — identical pattern to `list_files()` → `ls -l`.
- Persistent Chrome on the workspace is the browser's equivalent of the filesystem: state that lives on the workspace and that successive thin commands operate on. Element ref `42` from the last snapshot is still valid on the next `browser-exec click 42` because the daemon kept the session (and its selector map) alive.

## 4. Key design decisions

| # | Decision | Rationale | Alternative (rejected) |
|---|----------|-----------|------------------------|
| D1 | **`browser-exec` uses `browser-use` on the workspace** | Faithful relocation — the current `browser_direct.py` helper bodies move verbatim. Identical DOM format + ref semantics, zero agent-facing change. | Playwright (already in image) — but its a11y snapshot differs; would mean reimplementing browser-use's serializer + ref model. Kept as fallback if dep weight is a problem (see R1). |
| D2 | **Persistent daemon, not one-shot CLI** | Interaction (`click`/`type`/`select`) needs the selector map from the prior snapshot, which only lives in a running process. Daemon holds the `BrowserSession` across calls. | Re-snapshot + persist selector map to disk and resolve by `backend_node_id` each call — more fragile. |
| D3 | **browser-use launches Chrome locally (no debugging port)** | In-workspace, browser-use owns Chrome via pipe/loopback it controls. Eliminates `_start_remote_chromium` and the `9222` exposure entirely. | Keep `agent-chromium --remote-debugging-port` on loopback + daemon connects via `cdp_url` — works but pointless extra moving part. |
| D4 | **Validation + nonce wrapping stay on the agent** | They're pure logic (`browser_security.py`), and security policy belongs with the decision-maker, before anything is dispatched. | Move into executor — couples policy to the workspace, harder to audit. |
| D5 | **Local (no-workspace) dev mode unchanged** | `_is_remote_browser()` already gates remote vs local. Local dev keeps in-process browser-use so `python agent.py` on a laptop still works without the workspace image. | Force everyone through `browser-exec` — breaks local dev. |
| D6 | **Transport = unix socket on the workspace** | No TCP port, no netpol surface; same-user same-pod. Client auto-starts the daemon if absent. | localhost HTTP — needs a port; loopback-only but still more surface than a socket. |

## 5. Component design

### 5.1 Workspace: `browser-exec`

A self-contained Python program shipped in the workspace image (the image deliberately excludes agent code, so `browser-exec` carries its own deps). Two modes in one entrypoint:

- **`browser-exec serve`** — daemon. Lazily constructs a `browser-use` `BrowserSession` (local launch, `executable_path=/usr/local/bin/agent-chromium`, `user_data_dir=<workspace>/.browser-profile`, `headless=true`). Listens on `/tmp/browser-exec.sock`. Serializes requests (the agent issues actions one at a time). Holds the page + selector map.
- **`browser-exec <action> [--json '<args>']`** — client. Connects to the socket (auto-spawns `serve` via `nohup` + retry if not running), sends the action, prints the daemon's JSON reply to **stdout**, exits.

**Actions** (1:1 with today's `browser_direct.py` helpers, which move here):
`navigate`, `snapshot`, `click`, `type`, `select`, `scroll`, `screenshot`, `back`, `shutdown`.

**JSON protocol (critical):** because `exec_command` returns only stdout (and never raises on non-zero exit), **every** outcome is a single JSON object on stdout; all logging/diagnostics go to stderr.
- Success: `{"dom": "...", "url": "...", "title": "...", "screenshot": "<b64>"|null, "tabs": [...]}`
- Failure: `{"error": "<message>"}`
The agent treats unparseable stdout as an error.

`max_dom_chars` and `include_screenshot` come in as flags from the agent (computed there from model capability) so behavior stays identical.

### 5.2 Agent: thin tools + helper

- New `ToolContext.browser_exec(action: str, **args) -> dict`: builds the command, calls `self.workspace_manager.backend.exec_command("browser-exec ...")`, `json.loads` the stdout, returns the dict (or `{"error": ...}`).
- `browser_direct.py`: each tool keeps `validate_url_with_config` + `wrap_with_nonce` + arg handling; replaces `session = await context.get_browser_session()` + browser-use calls with `result = await context.browser_exec("navigate", url=url, include_screenshot=..., max_dom_chars=...)`. The `_get_page_state`/`_click_element`/`_type_text`/`_select_option`/`_scroll` helpers **move to `browser-exec`**.
- `context.py`: remove `_browser_session`/`_browser_cdp_url`/`get_browser_session`/`export_browser_state`; `close_browser()` becomes `exec_command("browser-exec shutdown")`. Keep `should_include_screenshots()` / `get_max_dom_chars()` (now feed flags).
- `browser.py`: delete `_start_remote_chromium` / `_stop_remote_chromium` / `_get_browser_config`'s remote branch. (Autonomous mode handled in Phase 3.)
- Local mode (no workspace): keep the existing in-process browser-use path.

## 6. Phases

**Phase 0 — Optional interim hotfix (hours, no image rebuild dependency on browser-use).**
If the demo needs the "look at the page" case working before the full executor lands: implement `browser_navigate`/`browser_screenshot` via one-shot `chromium --headless --dump-dom <url>` / `--screenshot=<path> <url>` over `exec_command` (chromium is already in the image). Read-only, stateless, no daemon. Superseded by Phase 1. *Skip if we go straight to the real fix.*

**Phase 1 — Workspace executor + read path (the keystone).**
`browser-exec` daemon + client with `navigate`, `snapshot`, `screenshot`, `shutdown`. Add `browser-exec` (+ browser-use) to `Dockerfile.workspace`. Rewire `browser_navigate`/`browser_snapshot`/`browser_screenshot` and `context.browser_exec()`/`close_browser()`. Unblocks the b4478b88 case (view + screenshot).

**Phase 2 — Interaction.**
Add `click`, `type`, `select`, `scroll`, `back` to `browser-exec` (selector map persists in the daemon). Rewire the remaining direct tools. Direct browser control fully restored.

**Phase 3 — Autonomous mode (`browse_website` / `download_from_website`).**
These run a `browser-use` `Agent` (LLM loop). Relocate the loop into `browser-exec` (`browser-exec autonomous --task ... --llm-model ... --llm-key ...`), passing the per-job LLM creds the orchestrator already injects (`config_override.llm`) over the SSH channel. Downloads land on the workspace fs (already the case). *Alternative:* deprecate autonomous-on-remote in favor of the main agent driving the direct tools (the industry direction per `docs/browser_use.md` §2). Decision needed at this phase.

**Phase 4 — Cleanup / hardening.**
Remove the now-dead `9222` exposure: `port: 9222` from `helm/templates/workspace-network-policy.yaml` ingress, `9222` from `EXPOSE` in `Dockerfile.workspace`, the `cdp` `containerPort` in `orchestrator/services/container_provisioner.py`. Update `docs/browser_use.md` §1 "Remote browser support". Delete dead code.

## 7. Files touched

- **New**: `docker/workspace/browser-exec` (+ its `requirements`), `docs/features/browser_workspace_executor.md` (this doc).
- **Workspace image**: `docker/Dockerfile.workspace` (install browser-use + `COPY browser-exec`; later drop `EXPOSE 9222`).
- **Agent**: `src/tools/context.py`, `src/tools/research/browser_direct.py`, `src/tools/research/browser.py`. (`browser_security.py` unchanged.)
- **Infra cleanup (Phase 4)**: `helm/templates/workspace-network-policy.yaml`, `orchestrator/services/container_provisioner.py`.
- **Tests**: `tests/tools/research/test_browser_tools.py`, `tests/test_browser_llm_resolution.py` (+ new executor tests).

## 8. Testing

- **Agent unit**: mock `backend.exec_command` to return canned JSON; assert tools validate URLs, pass correct flags, wrap with nonce, and handle `{"error":...}` / unparseable stdout. (Replaces today's browser-use session mocks.)
- **Executor unit**: drive `browser-exec` action handlers against a headless browser-use session (or mock it); assert JSON shape and that diagnostics stay on stderr.
- **Local mode**: existing in-process path keeps its current tests.
- **End-to-end**: requires the cluster (no browser in `FilesystemTestBackend`) — exercise the way the original bug was found: real session, `browser_navigate`, confirm DOM+screenshot return.

## 9. Deployment & rollout

- Two images rebuild via CI change-detection: **workspace** (browser-use + `browser-exec`) and **agent** (tool rewiring) → Fleet sync. No manual K8s patching.
- **Running sessions are not fixed live** — the in-flight agent/workspace pods run old code; a new session (fresh pods) picks up the fix. (b4478b88 itself won't recover.)
- Order: Phase 1 needs both images. Phase 4 netpol/port removal ships only after Phases 1–3 are confirmed (nothing still dials 9222).

## 10. Risks / open questions

- **R1 — browser-use dependency weight on the workspace image.** Core browser-use pulls `cdp-use` + pydantic etc.; an LLM SDK may come transitively (litellm is now optional per `docs/browser_use.md` §7). Verify it doesn't drag torch/heavy deps; pin minimal extras, or fall back to D1's Playwright alternative if the image bloats.
- **R2 — daemon lifecycle / crash recovery.** If Chrome or the daemon dies mid-session, the next client call must auto-restart `serve` (fresh Chrome, page lost; `user_data_dir` preserves cookies). Mirrors today's health-check-restart behavior — acceptable.
- **R3 — Phase 3 LLM creds on the workspace.** Relocating the autonomous loop means per-job LLM creds transit SSH and are used by a workspace process. Trusted channel, but if undesirable, take the deprecation alternative.
- **R4 — startup latency per call.** Daemon is persistent, so only the *first* call pays Chrome launch (~1–2s); subsequent calls are socket round-trips. Negligible at LLM-step cadence.
- **R5 — concurrency.** Assumes the agent issues browser actions serially (true today). Daemon serializes; revisit if parallel browsing is ever added.
- **R6 — `papers.py` still uses the old CDP path (follow-up).** `papers.py:_try_browser_download` is an autonomous PDF-download *fallback* that imports the retained `_get_browser_config`/`_start_remote_chromium`/`_stop_remote_chromium` helpers — so it's still broken on the cluster (fails gracefully → `None`; non-browser strategies still work). Left untouched this pass to keep scope tight. Until it's migrated (or dropped), the `9222` exposure (`EXPOSE`, `containerPort cdp`, netpol ingress) must stay, so **Phase 4 is deferred**.
