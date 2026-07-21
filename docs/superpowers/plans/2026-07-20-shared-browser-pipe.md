# Shared Browser — Plan 1 of 2: The Pipe (daemon streaming + orchestrator broker)

> **Execution complete:** This plan was run inline, task by task. Checked boxes
> record the completed implementation; the execution record below is the
> authoritative summary of the as-built result.

**Goal:** The workspace's `browser-exec` daemon streams live CDP screencast frames of its Chromium over a loopback-only TCP listener, and the orchestrator relays that stream (plus user input/control) to an authenticated WebSocket — the complete backend pipe for the shared-browser feature (spec: `docs/features/shared_browser.md`, Steps A+B).

**Architecture:** `browser-exec` (single-file Python daemon in the workspace) gains a streaming side-channel: framed protocol on `127.0.0.1:38801`, screencast via browser-use's in-process CDP client, and a daemon-held control **baton** that refuses agent mutating actions while the user drives. The orchestrator gains a fail-closed config, a `POST …/browser/open` endpoint (ensure workspace → `stream_info` over SSH → set canvas `BrowserSource`), and a binary WS relay riding `PinnedSSHTransportPool.open_loopback_connection` (the canvas live-app SSH path). Plan 2 (separate) builds the cockpit renderer on top.

**Tech Stack:** Python 3 stdlib (daemon), browser-use 0.12.x in-process CDP (`session.get_or_create_cdp_session`), FastAPI + asyncssh (orchestrator), pytest, podman (Fedora — use `:Z` on volume mounts).

## Execution record (updated 2026-07-21)

**Plan 1 is complete.** All 13 tasks below were implemented on `develop` in
commits `cd712525` through `9f06579d`, followed by the lifecycle/race review
fix in `29424593`. The feature remains dark by default, and nothing was
pushed during execution.

Implemented scope:

- browser-use `0.12.9` CDP feasibility probe and recorded call forms;
- daemon framing, navigation validation, generation/stream state, control
  baton, loopback listener, screencast, input dispatch, and real-Chromium
  container conformance gate;
- orchestrator fail-closed config, broker helpers, Canvas capability, open
  endpoint, generation-pinned WebSocket relay, activity marking, and Helm
  wiring.

Final verification evidence:

- the six shared-browser pytest files passed: **55 tests**;
- the selected Canvas regression suites passed: **40 tests**;
- a rebuilt `srw-workspace-stream-test` image passed the Podman conformance
  gate against real Chromium, including a received 6,990-byte JPEG frame;
- both Helm CI value sets linted successfully;
- `ruff check src/ orchestrator/ tests/` passed.

The full repository `ruff format --check` still reports three unrelated,
pre-existing files outside this plan's changes:
`orchestrator/services/project_loop_sweeper.py`,
`tests/test_loop_unified_advance.py`, and
`tests/test_project_loop_sweeper.py`. The files touched by this plan pass the
format check. The optional Canvas SSH transport suite also remains
environment-limited on this host because `asyncssh` is not installed (31
passed, 2 skipped, 6 failed at that import boundary).

The final review deliberately tightened several details beyond the illustrative
code snippets below. Baton changes and user input are serialized with daemon
browser actions; mutating actions re-check the baton while holding that lock;
page-state changes are broadcast; viewer activity is marked immediately on
attach; spawned SSH children are reaped; and the relay closes with `4409` when
the staged Canvas generation is either missing or different. The checked-in
implementation and tests are authoritative where a snippet below differs.

The container workspace path is conformance-proven. VM support is not yet
claimed operational: it still needs an attested remote Canvas binding,
orchestrator-to-VM routing in the target deployment, and the Plan-2 VM image
provisioning/conformance wiring. The shared SSH abstraction is ready for that
path, but the current default deployment does not prove it.

## Global Constraints

- Work directly on `develop`. Commit after every task. **NEVER `git push`** (user pushes manually; push triggers CI SHA rewrites).
- Feature is dark: `canvas.sharedBrowser.enabled` defaults to **false** everywhere; only `deployment/values-experimental.yaml` turns it on (dev profile).
- Every new listener binds **`127.0.0.1` only** — nothing new on the pod/VM network. CDP never leaves the workspace.
- Do not touch the browser-use pin `browser-use>=0.12.9,<0.13.0` (`docker/Dockerfile.workspace:192`) or the Chromium symlink contract.
- Stream defaults (spec-locked): port `38801`, JPEG quality `60`, max `1280×720`, everyNth `2`, baton auto-release grace `30 s`, max frame `8 MiB`.
- Frame protocol (spec-locked): TCP `[4-byte BE length][1-byte type][payload]`; length covers type byte + payload. Types `HELLO=1 FRAME=2 STATE=3 INPUT=4 CONTROL=5 ERROR=6`. FRAME payload = `[2-byte BE header length][header JSON][raw JPEG]`. On WS: binary messages `[1-byte type][payload]` (no length prefix).
- Run tests from the repo root with `python -m pytest <file> -v`. Local runs can be noisy under Python 3.14 — the named test files passing is the gate here; full-suite green is checked once at the end (Task 13).
- `docker/browser-exec` has **no `.py` extension**; tests load it via `importlib.machinery.SourceFileLoader`. It must stay importable with stdlib only (all browser-use imports stay lazy/function-local).
- Commit messages follow repo style: `feat(browser): …` for daemon work, `feat(canvas): …` for orchestrator work, `test(…)`/`docs(…)` accordingly.

## File Structure

| File | Role |
|---|---|
| `docker/browser-exec` (modify) | Daemon: + framing codec, nav validation, `StreamHub` (baton/viewers/generation), loopback stream server, `ScreencastCdp`, `stream_info` action |
| `docker/check-browser-stream.py` (create) | Container conformance check (run via podman in Task 7; build-time/VM wiring is a Plan-2 item) |
| `tests/tools/research/test_browser_exec_stream.py` (create) | Host-side daemon unit tests (codec, validation, baton, listener) |
| `orchestrator/services/browser_stream_config.py` (create) | Fail-closed env config |
| `orchestrator/services/browser_stream_broker.py` (create) | Codec mirror, `workspace_ready`, `ssh_endpoint`, `exec_stream_info`, `relay_browser_stream` |
| `orchestrator/routers/shared_browser.py` (create) | `POST /api/persistent/threads/{tid}/browser/open` |
| `orchestrator/routers/canvases.py` (modify) | `can_stream_browser` capability branch in `_represent` |
| `orchestrator/services/canvas.py` (modify) | `CanvasCapabilities.can_stream_browser` field |
| `orchestrator/main.py` (modify) | WS route + `include_router` |
| `tests/test_shared_browser_config.py`, `tests/test_shared_browser_broker.py`, `tests/test_shared_browser_open.py`, `tests/test_shared_browser_capability.py`, `tests/test_shared_browser_infra.py` (create) | Orchestrator tests |
| `helm/values.yaml`, `helm/values.schema.json`, `helm/templates/configmap.yaml`, `helm/templates/orchestrator/deployment.yaml`, `deployment/values-experimental.yaml` (modify) | Flag wiring (5-file pattern) |

---

### Task 1: Pre-flight — verify browser-use 0.12.x in-process CDP API (STOP gate)

The whole daemon design assumes browser-use exposes an in-process CDP client (`session.get_or_create_cdp_session()` → object with `.cdp_client.send.Page.*` / `.send.Input.*`, event registration, and a `session_id`). The feasibility pass verified this against browser-use **0.11.9** docs and the Dockerfile comment, **not** against the pinned 0.12.x in the real image. This task proves it (or stops the plan).

**Files:**
- Create: `docs/superpowers/plans/notes-browseruse-cdp-api.md` (probe results — consumed by Task 6)

**Interfaces:**
- Produces: written confirmation of the four call forms Task 6's `ScreencastCdp` uses: (1) obtain CDP session, (2) start screencast + receive `Page.screencastFrame` events, (3) `Page.screencastFrameAck`, (4) `Input.dispatchMouseEvent`/`dispatchKeyEvent`.

- [x] **Step 1: Build the workspace image locally** (uses the repo's own Dockerfile so the probe matches HEAD; takes several minutes)

```bash
podman build -f docker/Dockerfile.workspace -t srw-workspace-probe .
```

Expected: successful build ending with the `assert-browser-stack.sh` gate printing `Workspace browser stack OK.`

- [x] **Step 2: Write the probe script** to `/tmp/claude-1000/…/scratchpad/cdp_probe.py` (any scratch path works; it is volume-mounted, not committed):

```python
"""Probe browser-use's in-process CDP surface inside the workspace image."""
import asyncio, base64, inspect, os, sys

os.environ.setdefault("BROWSER_EXEC_PROFILE", "/tmp/probe-profile")

async def main() -> int:
    from browser_use import BrowserSession

    s = BrowserSession(
        headless=True,
        executable_path="/usr/local/bin/agent-chromium",
        user_data_dir="/tmp/probe-profile",
        chromium_sandbox=False,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    await s.start()
    print("browser-use version:", __import__("browser_use").__version__)
    print("has cdp_client:", hasattr(s, "cdp_client"))
    print("has get_or_create_cdp_session:", hasattr(s, "get_or_create_cdp_session"))

    cdp = await s.get_or_create_cdp_session()
    print("cdp session type:", type(cdp))
    print("session_id:", getattr(cdp, "session_id", None))
    print("target_id:", getattr(cdp, "target_id", None))
    client = getattr(cdp, "cdp_client", None)
    print("client type:", type(client))
    print("client attrs:", [a for a in dir(client) if not a.startswith("_")])
    print("send.Page screencast:", [m for m in dir(client.send.Page) if "creencast" in m])
    print("send.Input dispatch:", [m for m in dir(client.send.Input) if "ispatch" in m])
    reg = getattr(client, "register", None)
    print("register.Page attrs:", [a for a in dir(getattr(reg, "Page", object())) if "creencast" in a] if reg else "NO register attr")

    # Live round-trip: subscribe, start screencast, await one frame, ack it.
    got = asyncio.get_event_loop().create_future()

    def on_frame(params, session_id=None):
        if not got.done():
            got.set_result(params)

    client.register.Page.screencastFrame(on_frame)
    await client.send.Page.startScreencast(
        params={"format": "jpeg", "quality": 60, "maxWidth": 1280,
                "maxHeight": 720, "everyNthFrame": 2},
        session_id=cdp.session_id,
    )
    await s.navigate_to("data:text/html,<h1>probe</h1>")  # may be watchdog-blocked; frame can also arrive from about:blank
    try:
        params = await asyncio.wait_for(got, timeout=15)
        jpeg = base64.b64decode(params["data"])
        print("FRAME OK bytes:", len(jpeg), "metadata:", params.get("metadata"))
        await client.send.Page.screencastFrameAck(
            params={"sessionId": params["sessionId"]}, session_id=cdp.session_id
        )
        print("ACK OK")
    except asyncio.TimeoutError:
        print("NO FRAME within 15s"); return 1

    await client.send.Input.dispatchMouseEvent(
        params={"type": "mouseMoved", "x": 10, "y": 10}, session_id=cdp.session_id
    )
    print("INPUT OK")

    metrics = await client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
    print("layout metrics keys:", list(metrics.keys()))
    await s.stop()
    print("PROBE PASS")
    return 0

sys.exit(asyncio.run(main()))
```

- [x] **Step 3: Run the probe in the image**

```bash
podman run --rm --entrypoint python3 \
  -v /path/to/cdp_probe.py:/tmp/cdp_probe.py:ro,Z \
  srw-workspace-probe /tmp/cdp_probe.py
```

Expected: `PROBE PASS` with `FRAME OK`, `ACK OK`, `INPUT OK` printed. If an attribute is missing (e.g. no `client.register`), the probe stack-traces at that line — that's the data you need.

- [x] **Step 4: Record results.** Write `docs/superpowers/plans/notes-browseruse-cdp-api.md` capturing: browser-use version, the exact working call forms for the four capabilities, whether `target_id` exists on the session object, and the layout-metrics key holding the CSS viewport (`cssLayoutViewport` expected). If a call form differed from the probe script's, record the working form — Task 6 adapts **only** its `ScreencastCdp` class to match.

**⛔ STOP GATE:** If screencast frames or input dispatch are NOT reachable in-process by any call form, stop and report back — the spec's Piece-1 architecture needs revisiting before any code is written.

- [x] **Step 5: Commit**

```bash
git add docs/superpowers/plans/notes-browseruse-cdp-api.md
git commit -m "docs(browser): record browser-use in-process CDP probe results"
```

---

### Task 2: Daemon — stream framing codec + constants

**Files:**
- Modify: `docker/browser-exec` (new constants + codec section after the existing constants block, ~line 98)
- Test: `tests/tools/research/test_browser_exec_stream.py` (create)

**Interfaces:**
- Produces: `encode_stream_frame(ftype: int, payload: bytes) -> bytes`, `async read_stream_frame(reader) -> tuple[int, bytes]`, `encode_frame_payload(header: dict, jpeg: bytes) -> bytes`, `decode_frame_payload(payload: bytes) -> tuple[dict, bytes]`, constants `T_HELLO..T_ERROR`, `STREAM_PORT`, `STREAM_QUALITY`, `STREAM_MAX_WIDTH`, `STREAM_MAX_HEIGHT`, `STREAM_EVERY_NTH`, `MAX_STREAM_FRAME`, `BATON_GRACE_S`. Consumed by Tasks 4–7 (and mirrored by Task 9).

- [x] **Step 1: Write the failing tests**

```python
"""Host-side unit tests for docker/browser-exec streaming additions.

The daemon file has no .py extension and must stay stdlib-importable
(browser-use imports are lazy), so we load it via SourceFileLoader.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "browser_exec_mod", str(REPO / "docker" / "browser-exec")
    )
    spec = importlib.util.spec_from_loader("browser_exec_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


BE = _load()


class TestFramingCodec:
    def test_roundtrip(self):
        wire = BE.encode_stream_frame(BE.T_STATE, b'{"baton":"agent"}')
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        ftype, payload = asyncio.run(BE.read_stream_frame(reader))
        assert ftype == BE.T_STATE
        assert payload == b'{"baton":"agent"}'

    def test_length_covers_type_byte(self):
        wire = BE.encode_stream_frame(BE.T_HELLO, b"abc")
        assert wire[:4] == (4).to_bytes(4, "big")  # 1 type byte + 3 payload
        assert wire[4] == BE.T_HELLO

    def test_oversize_encode_rejected(self):
        with pytest.raises(ValueError):
            BE.encode_stream_frame(BE.T_FRAME, b"x" * (BE.MAX_STREAM_FRAME + 1))

    def test_oversize_read_rejected(self):
        reader = asyncio.StreamReader()
        reader.feed_data((BE.MAX_STREAM_FRAME + 100).to_bytes(4, "big") + b"\x02")
        reader.feed_eof()
        with pytest.raises(ValueError):
            asyncio.run(BE.read_stream_frame(reader))

    def test_frame_payload_roundtrip(self):
        header = {"generation": "g1", "w": 1280, "h": 720, "ts": 1.5}
        jpeg = b"\xff\xd8fakejpeg"
        header2, jpeg2 = BE.decode_frame_payload(
            BE.encode_frame_payload(header, jpeg)
        )
        assert header2 == header
        assert jpeg2 == jpeg
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: FAIL — `AttributeError: module 'browser_exec_mod' has no attribute 'encode_stream_frame'` (import of the file itself must succeed).

- [x] **Step 3: Implement.** In `docker/browser-exec`, add `import struct` to the stdlib import block, then insert after the `VALID_ACTIONS` block (~line 98):

```python
# ─────────────────────────────────────────────────────────────────────
# Shared-browser streaming (docs/features/shared_browser.md, Piece 1)
# ─────────────────────────────────────────────────────────────────────

# Loopback-only stream listener. Covered by sshd `PermitOpen 127.0.0.1:*`;
# never reachable from the pod/VM network — same posture as live-app ports.
STREAM_PORT = int(os.environ.get("BROWSER_EXEC_STREAM_PORT", "38801"))
STREAM_QUALITY = int(os.environ.get("BROWSER_EXEC_STREAM_QUALITY", "60"))
STREAM_MAX_WIDTH = int(os.environ.get("BROWSER_EXEC_STREAM_MAX_WIDTH", "1280"))
STREAM_MAX_HEIGHT = int(os.environ.get("BROWSER_EXEC_STREAM_MAX_HEIGHT", "720"))
STREAM_EVERY_NTH = int(os.environ.get("BROWSER_EXEC_STREAM_EVERY_NTH", "2"))
BATON_GRACE_S = float(os.environ.get("BROWSER_EXEC_BATON_GRACE", "30"))

# Framing: [4-byte BE length][1-byte type][payload]; length covers type+payload.
T_HELLO, T_FRAME, T_STATE, T_INPUT, T_CONTROL, T_ERROR = 1, 2, 3, 4, 5, 6
MAX_STREAM_FRAME = 8 * 1024 * 1024


def encode_stream_frame(ftype: int, payload: bytes) -> bytes:
    if len(payload) + 1 > MAX_STREAM_FRAME:
        raise ValueError(f"stream frame too large: {len(payload) + 1}")
    return struct.pack(">IB", len(payload) + 1, ftype) + payload


async def read_stream_frame(reader) -> tuple:
    """Read one framed message from an asyncio StreamReader."""
    head = await reader.readexactly(4)
    (length,) = struct.unpack(">I", head)
    if not 1 <= length <= MAX_STREAM_FRAME:
        raise ValueError(f"bad stream frame length: {length}")
    body = await reader.readexactly(length)
    return body[0], body[1:]


def encode_frame_payload(header: dict, jpeg: bytes) -> bytes:
    """FRAME payload: [2-byte BE header length][header JSON][raw JPEG]."""
    header_json = json.dumps(header).encode()
    return struct.pack(">H", len(header_json)) + header_json + jpeg


def decode_frame_payload(payload: bytes) -> tuple:
    (hlen,) = struct.unpack(">H", payload[:2])
    return json.loads(payload[2 : 2 + hlen].decode()), payload[2 + hlen :]
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: 5 PASS.

- [x] **Step 5: Commit**

```bash
git add docker/browser-exec tests/tools/research/test_browser_exec_stream.py
git commit -m "feat(browser): stream framing codec for browser-exec"
```

---

### Task 3: Daemon — user-navigation validation

User URL-bar navigations must apply the same policy as agent navigation (`src/tools/research/browser_security.py`), enforced daemon-side. The daemon cannot import `src/` (different machine), so the rules are vendored. `file://` goes through the existing `LocalFileServer` FIRST (agent parity — mockups render), then the served `http://127.0.0.1:…` URL is validated like any other.

**Files:**
- Modify: `docker/browser-exec` (after the codec section)
- Test: `tests/tools/research/test_browser_exec_stream.py` (extend)

**Interfaces:**
- Consumes: `LocalFileServer.url_for(url)` (exists, `docker/browser-exec:195`).
- Produces: `validate_user_nav(url: str, files) -> str` (raises `ValueError` on policy violation). Consumed by Task 5's CONTROL `navigate`.

- [x] **Step 1: Write the failing tests** (append to the test file)

```python
class _StubFiles:
    def url_for(self, url):
        if url.startswith("file://"):
            return "http://127.0.0.1:45678/mock.html"
        return url


class TestValidateUserNav:
    def test_https_passes(self):
        assert BE.validate_user_nav("https://example.com/x", _StubFiles()) == "https://example.com/x"

    def test_schemeless_gets_https(self):
        assert BE.validate_user_nav("example.com", _StubFiles()) == "https://example.com"

    def test_javascript_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("javascript:alert(1)", _StubFiles())

    def test_data_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("data:text/html,<b>x</b>", _StubFiles())

    def test_metadata_host_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("http://metadata.google.internal/", _StubFiles())

    def test_k8s_internal_blocked(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("http://orchestrator.default.svc.cluster.local/", _StubFiles())

    def test_file_translated_then_allowed(self):
        assert BE.validate_user_nav("file:///home/agent-host/workspace/mock.html", _StubFiles()) == "http://127.0.0.1:45678/mock.html"

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            BE.validate_user_nav("   ", _StubFiles())
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -k ValidateUserNav -v`
Expected: FAIL — no attribute `validate_user_nav`.

- [x] **Step 3: Implement** (in `docker/browser-exec`; `re` must be added to the stdlib imports):

```python
# Vendored from src/tools/research/browser_security.py (the daemon cannot
# import agent code): same schemes/hosts policy for viewer-driven navigation.
_NAV_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata.goog"}
_NAV_K8S_INTERNAL_RE = re.compile(
    r"\.(cluster\.local|svc|svc\.cluster\.local|pod|pod\.cluster\.local)$",
    re.IGNORECASE,
)


def validate_user_nav(url: str, files) -> str:
    """Validate a viewer-typed URL. file:// renders via LocalFileServer (agent
    parity), then the resulting URL passes the same checks as agent navigation."""
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")
    url = url.strip()
    if urlparse(url).scheme == "file":
        url = files.url_for(url)  # → http://127.0.0.1:<port>/…, or raises
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
        scheme = "https"
    if scheme in {"javascript", "data", "file"}:
        raise ValueError(f"Blocked scheme: {scheme}://")
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {scheme}://")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("URL has no hostname")
    if hostname in _NAV_BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname: {hostname}")
    if _NAV_K8S_INTERNAL_RE.search(hostname):
        raise ValueError(f"Blocked K8s internal hostname: {hostname}")
    return url
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: all PASS (13 total).

- [x] **Step 5: Commit**

```bash
git add docker/browser-exec tests/tools/research/test_browser_exec_stream.py
git commit -m "feat(browser): daemon-side user navigation validation"
```

---

### Task 4: Daemon — StreamHub, baton refusal, `stream_info` action

The baton (single-controller lease) lives in the daemon, next to the browser. While `baton == "user"`, mutating agent actions get a structured refusal **before** any lock/session work; reads pass and carry the baton state. `stream_info` is the new action the orchestrator calls over SSH: it (cold-)starts the browser, mints a **generation** + hello **token**, and returns them.

**Files:**
- Modify: `docker/browser-exec`
- Test: `tests/tools/research/test_browser_exec_stream.py` (extend)

**Interfaces:**
- Consumes: Task 2 constants.
- Produces: `class StreamHub` with `baton: str`, `generation: str|None`, `token: str|None`, `viewers: dict[int, asyncio.Queue]`, `mint_generation(initial_baton=None)`, `state_payload() -> dict`, `take_baton()`, `release_baton()`, `add_viewer(queue) -> int`, `remove_viewer(vid)`, `broadcast(frame: bytes)`, `shutdown_viewers(code: str)`, `on_state_change` callback attr; `MUTATING_ACTIONS` set; `BrowserDaemon.hub`; daemon action `stream_info` returning `{"generation", "token", "port", "baton"}`; refusal dict `{"error": "user_is_driving", "url": …, "message": …}`. Consumed by Tasks 5–7 and (over SSH) Tasks 11–12.

- [x] **Step 1: Write the failing tests** (append)

```python
class TestStreamHub:
    def test_mint_generation_sets_identity_and_initial_baton(self):
        hub = BE.StreamHub()
        assert hub.baton == "agent"
        hub.mint_generation("user")
        assert hub.baton == "user"
        assert hub.generation and hub.token and len(hub.token) == 64

    def test_state_payload_shape(self):
        hub = BE.StreamHub()
        hub.mint_generation()
        state = hub.state_payload()
        assert set(state) >= {"generation", "baton", "viewport", "url", "title", "loading"}

    def test_broadcast_drops_oldest_for_laggards(self):
        async def run():
            hub = BE.StreamHub()
            q = asyncio.Queue(maxsize=2)
            hub.add_viewer(q)
            for frame in (b"f1", b"f2", b"f3"):
                hub.broadcast(frame)
            assert q.qsize() == 2
            assert await q.get() == b"f2"  # f1 was dropped
            assert await q.get() == b"f3"
        asyncio.run(run())

    def test_auto_release_after_last_viewer_leaves(self, monkeypatch):
        monkeypatch.setattr(BE, "BATON_GRACE_S", 0.05)
        async def run():
            hub = BE.StreamHub()
            hub.mint_generation("user")
            vid = hub.add_viewer(asyncio.Queue(maxsize=4))
            hub.remove_viewer(vid)
            assert hub.baton == "user"          # not instant
            await asyncio.sleep(0.15)
            assert hub.baton == "agent"          # reverted after grace
        asyncio.run(run())

    def test_reconnect_cancels_auto_release(self, monkeypatch):
        monkeypatch.setattr(BE, "BATON_GRACE_S", 0.05)
        async def run():
            hub = BE.StreamHub()
            hub.mint_generation("user")
            vid = hub.add_viewer(asyncio.Queue(maxsize=4))
            hub.remove_viewer(vid)
            hub.add_viewer(asyncio.Queue(maxsize=4))  # reconnect within grace
            await asyncio.sleep(0.15)
            assert hub.baton == "user"
        asyncio.run(run())


class TestBatonRefusal:
    def test_mutating_action_refused_while_user_drives(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")
        daemon.hub.state_extra["url"] = "https://example.com/form"
        resp = asyncio.run(daemon.handle({"action": "click", "args": {"ref": 1}}))
        assert resp["error"] == "user_is_driving"
        assert resp["url"] == "https://example.com/form"
        assert "release control" in resp["message"]

    def test_refusal_never_touches_the_browser(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")

        async def boom():  # would only run if handle() reached the session
            raise AssertionError("session must not be touched for a refusal")

        daemon._get_session = boom
        resp = asyncio.run(daemon.handle({"action": "navigate", "args": {"url": "https://x.dev"}}))
        assert resp["error"] == "user_is_driving"

    def test_unknown_action_beats_baton_check(self):
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")
        resp = asyncio.run(daemon.handle({"action": "bogus"}))
        assert "unknown action" in resp["error"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -k "StreamHub or BatonRefusal" -v`
Expected: FAIL — no attribute `StreamHub`.

- [x] **Step 3: Implement.** Add `import secrets`, `import time`, `import uuid` to the stdlib imports. Insert after `validate_user_nav`:

```python
# Agent actions refused while the user holds the baton. Reads (snapshot,
# screenshot) always pass; stream_info/shutdown are lifecycle, not driving.
MUTATING_ACTIONS = {"navigate", "click", "type", "select", "scroll", "back"}


class StreamHub:
    """Shared-browser stream state: viewers, baton, browser generation.

    Owned by the daemon; the single authority for who is driving. See
    docs/features/shared_browser.md (Piece 1).
    """

    def __init__(self) -> None:
        self.baton = "agent"
        self.generation = None
        self.token = None
        self.viewers: dict = {}
        self._next_viewer_id = 0
        self._auto_release = None
        self.state_extra = {"url": None, "title": None, "loading": False}
        self.viewport = {"width": STREAM_MAX_WIDTH, "height": STREAM_MAX_HEIGHT}
        self.on_state_change = None  # set by the stream server; broadcasts STATE

    def mint_generation(self, initial_baton=None) -> None:
        """New browser generation ⇒ new identity + new hello token."""
        self.generation = str(uuid.uuid4())
        self.token = secrets.token_hex(32)
        if initial_baton in ("agent", "user"):
            self.baton = initial_baton

    def clear_generation(self) -> None:
        self.generation = None
        self.token = None

    def state_payload(self) -> dict:
        return {
            "generation": self.generation,
            "baton": self.baton,
            "viewport": self.viewport,
            **self.state_extra,
        }

    def take_baton(self) -> None:
        self._set_baton("user")

    def release_baton(self) -> None:
        self._set_baton("agent")

    def _set_baton(self, holder: str) -> None:
        if self.baton != holder:
            self.baton = holder
            self._notify()

    def _notify(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change()

    def add_viewer(self, queue) -> int:
        if self._auto_release is not None:
            self._auto_release.cancel()
            self._auto_release = None
        vid = self._next_viewer_id
        self._next_viewer_id += 1
        self.viewers[vid] = queue
        return vid

    def remove_viewer(self, vid) -> None:
        self.viewers.pop(vid, None)
        if not self.viewers and self.baton == "user":
            # Closed laptop must never wedge the agent — revert after a grace
            # period long enough to survive the cockpit's reconnect backoff.
            loop = asyncio.get_event_loop()
            self._auto_release = loop.call_later(BATON_GRACE_S, self.release_baton)

    def broadcast(self, frame: bytes) -> None:
        """Fan a pre-encoded frame to every viewer; drop-oldest for laggards."""
        for queue in self.viewers.values():
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def shutdown_viewers(self, code: str) -> None:
        """Tell every viewer the browser is gone; writers stop on the sentinel."""
        err = encode_stream_frame(
            T_ERROR, json.dumps({"code": code, "message": "browser ended"}).encode()
        )
        for queue in list(self.viewers.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(err)
                queue.put_nowait(None)  # close sentinel
            except asyncio.QueueFull:
                pass
```

In `BrowserDaemon.__init__` add:

```python
        self.hub = StreamHub()
```

In `BrowserDaemon._close_session`, after the session teardown add:

```python
        self.hub.clear_generation()
        self.hub.shutdown_viewers("browser_gone")
```

In `BrowserDaemon.handle`, add `"stream_info"` to `VALID_ACTIONS` (module constant) and insert the baton gate directly **after** the `if action not in VALID_ACTIONS` check and **before** `async with self._lock:`:

```python
        # Baton: while the user drives, mutating agent actions are refused
        # up-front — never spawns Chromium, never takes the action lock.
        if action in MUTATING_ACTIONS and self.hub.baton == "user":
            return {
                "error": "user_is_driving",
                "url": self.hub.state_extra.get("url"),
                "message": (
                    "The user is currently driving the shared browser. "
                    "Read-only actions (snapshot, screenshot) still work; "
                    "ask the user to release control before taking actions."
                ),
            }
```

Inside the lock, next to the other action branches, add:

```python
                if action == "stream_info":
                    # (Cold-)start the browser and return stream identity.
                    if self.hub.generation is None:
                        self.hub.mint_generation(args.get("initial_baton"))
                    return {
                        "generation": self.hub.generation,
                        "token": self.hub.token,
                        "port": STREAM_PORT,
                        "baton": self.hub.baton,
                    }
```

Note: `session = await self._get_session()` already runs before the action branches, so `stream_info` starting the browser needs no extra code. Finally, make the two read actions report the baton — in the `screenshot` branch add `"baton": self.hub.baton` to the returned dict, and in `_page_state` add before `return result`:

```python
        result["baton"] = self.hub.baton
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: all PASS (21 total).

- [x] **Step 5: Commit**

```bash
git add docker/browser-exec tests/tools/research/test_browser_exec_stream.py
git commit -m "feat(browser): stream hub, control baton, stream_info action"
```

---

### Task 5: Daemon — loopback stream listener (HELLO/STATE/CONTROL)

The TCP side-channel: authenticate via hello token, register a viewer queue, pump frames out, accept INPUT/CONTROL in. Everything a viewer receives flows through its bounded queue (STATE and FRAME stay ordered). Screencast start/stop hooks are stubs here — Task 6 fills them.

**Files:**
- Modify: `docker/browser-exec`
- Test: `tests/tools/research/test_browser_exec_stream.py` (extend)

**Interfaces:**
- Consumes: Tasks 2–4 (codec, `validate_user_nav`, `StreamHub`).
- Produces: `async start_stream_server(daemon) -> asyncio.AbstractServer` (binds `127.0.0.1:STREAM_PORT`, wires `hub.on_state_change`); `BrowserDaemon.ensure_screencast()` / `stop_screencast()` (no-op stubs until Task 6); `BrowserDaemon.user_control_nav(op, url=None)`; `BrowserDaemon.dispatch_user_input(event: dict)` (stub until Task 6). Consumed by Tasks 6–7 and the orchestrator relay (protocol peer).

- [x] **Step 1: Write the failing tests** (append)

```python
class TestStreamListener:
    @staticmethod
    async def _client(port):
        return await asyncio.open_connection("127.0.0.1", port)

    @staticmethod
    async def _send(writer, ftype, obj):
        writer.write(BE.encode_stream_frame(ftype, json.dumps(obj).encode()))
        await writer.drain()

    @staticmethod
    async def _recv(reader):
        ftype, payload = await asyncio.wait_for(BE.read_stream_frame(reader), timeout=2)
        return ftype, json.loads(payload.decode())

    def _daemon(self, monkeypatch, port):
        monkeypatch.setattr(BE, "STREAM_PORT", port)
        daemon = BE.BrowserDaemon()
        daemon.hub.mint_generation("user")
        return daemon

    def test_bad_token_gets_error_and_close(self, monkeypatch):
        async def run():
            daemon = self._daemon(monkeypatch, 38899)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38899)
                await self._send(writer, BE.T_HELLO, {"token": "wrong", "min_protocol": 1})
                ftype, err = await self._recv(reader)
                assert ftype == BE.T_ERROR
                assert err["code"] == "unauthorized"
                assert await reader.read(1) == b""  # connection closed
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())

    def test_good_token_gets_state_then_baton_flip_broadcast(self, monkeypatch):
        async def run():
            daemon = self._daemon(monkeypatch, 38898)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38898)
                await self._send(writer, BE.T_HELLO, {"token": daemon.hub.token, "min_protocol": 1})
                ftype, state = await self._recv(reader)
                assert (ftype, state["baton"]) == (BE.T_STATE, "user")
                assert state["generation"] == daemon.hub.generation
                await self._send(writer, BE.T_CONTROL, {"op": "release_baton"})
                ftype, state = await self._recv(reader)
                assert (ftype, state["baton"]) == (BE.T_STATE, "agent")
                # agent mutating action is allowed again
                resp = await daemon.handle({"action": "bogus"})  # cheap probe
                assert "unknown action" in resp["error"]
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())

    def test_disconnect_removes_viewer(self, monkeypatch):
        async def run():
            daemon = self._daemon(monkeypatch, 38897)
            server = await BE.start_stream_server(daemon)
            try:
                reader, writer = await self._client(38897)
                await self._send(writer, BE.T_HELLO, {"token": daemon.hub.token, "min_protocol": 1})
                await self._recv(reader)  # STATE
                assert len(daemon.hub.viewers) == 1
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0.1)
                assert len(daemon.hub.viewers) == 0
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -k StreamListener -v`
Expected: FAIL — no attribute `start_stream_server`.

- [x] **Step 3: Implement.** Add to `BrowserDaemon` (after `user_control_nav` insertion points below); first the daemon methods:

```python
    # ── shared-browser streaming hooks ──

    async def ensure_screencast(self) -> None:
        """Start the CDP screencast if viewers exist. Filled in by the
        screencast task (ScreencastCdp); safe no-op until then."""

    async def stop_screencast(self) -> None:
        """Stop the CDP screencast. Safe no-op until ScreencastCdp lands."""

    async def dispatch_user_input(self, event: dict) -> None:
        """Inject one viewer input event via CDP. No-op until ScreencastCdp."""

    async def user_control_nav(self, op: str, url=None) -> None:
        """Viewer-driven navigation. Runs under the action lock like any
        other session mutation; only honored while the user drives."""
        if self.hub.baton != "user":
            return
        async with self._lock:
            session = await self._get_session()
            if op == "navigate":
                await session.navigate_to(validate_user_nav(url or "", self._files))
            elif op == "back":
                await self._back(session)
            elif op == "reload":
                page = await session.get_current_page()
                if page is not None:
                    await page.evaluate("() => { location.reload(); }")
            try:
                self.hub.state_extra["url"] = await session.get_current_page_url()
                self.hub.state_extra["title"] = await session.get_current_page_title()
            except Exception:
                pass
        self.hub._notify()
```

Then the server (module level, before `_serve`):

```python
# ─────────────────────────────────────────────────────────────────────
# Stream server (loopback TCP side-channel for the shared browser)
# ─────────────────────────────────────────────────────────────────────


async def _handle_stream_conn(daemon, reader, writer):
    hub = daemon.hub
    try:
        ftype, payload = await asyncio.wait_for(read_stream_frame(reader), timeout=5.0)
        hello = json.loads(payload.decode()) if ftype == T_HELLO else {}
    except Exception:
        writer.close()
        return
    if not hub.token or hello.get("token") != hub.token:
        try:
            writer.write(
                encode_stream_frame(
                    T_ERROR, json.dumps({"code": "unauthorized"}).encode()
                )
            )
            await writer.drain()
        except Exception:
            pass
        writer.close()
        return

    queue = asyncio.Queue(maxsize=4)
    vid = hub.add_viewer(queue)
    log(f"stream viewer {vid} connected ({len(hub.viewers)} total)")
    queue.put_nowait(encode_stream_frame(T_STATE, json.dumps(hub.state_payload()).encode()))
    await daemon.ensure_screencast()

    async def pump_out():
        while True:
            frame = await queue.get()
            if frame is None:  # close sentinel (shutdown_viewers)
                break
            writer.write(frame)
            await writer.drain()

    async def pump_in():
        while True:
            ftype, payload = await read_stream_frame(reader)
            msg = json.loads(payload.decode())
            if ftype == T_INPUT:
                if hub.baton == "user":
                    await daemon.dispatch_user_input(msg)
            elif ftype == T_CONTROL:
                op = msg.get("op")
                if op == "take_baton":
                    hub.take_baton()
                elif op == "release_baton":
                    hub.release_baton()
                elif op in ("navigate", "back", "reload"):
                    try:
                        await daemon.user_control_nav(op, msg.get("url"))
                    except ValueError as exc:
                        queue.put_nowait(
                            encode_stream_frame(
                                T_ERROR,
                                json.dumps(
                                    {"code": "navigation_rejected", "message": str(exc)}
                                ).encode(),
                            )
                        )

    tasks = [asyncio.ensure_future(pump_out()), asyncio.ensure_future(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    finally:
        for task in tasks:
            task.cancel()
        hub.remove_viewer(vid)
        try:
            writer.close()
        except Exception:
            pass
        log(f"stream viewer {vid} disconnected ({len(hub.viewers)} total)")
        if not hub.viewers:
            await daemon.stop_screencast()


async def start_stream_server(daemon):
    """Bind the loopback-only stream listener and wire STATE broadcasting."""

    def broadcast_state():
        daemon.hub.broadcast(
            encode_stream_frame(
                T_STATE, json.dumps(daemon.hub.state_payload()).encode()
            )
        )

    daemon.hub.on_state_change = broadcast_state
    server = await asyncio.start_server(
        lambda r, w: _handle_stream_conn(daemon, r, w), "127.0.0.1", STREAM_PORT
    )
    log(f"stream listener on 127.0.0.1:{STREAM_PORT}")
    return server
```

Finally wire it into `_serve()` — after the unix server is created (`server = await asyncio.start_unix_server(...)` block) add:

```python
    stream_server = await start_stream_server(daemon)
```

and in the shutdown path (after `await stop_event.wait()`):

```python
    stream_server.close()
    await stream_server.wait_closed()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: all PASS (24 total).

- [x] **Step 5: Commit**

```bash
git add docker/browser-exec tests/tools/research/test_browser_exec_stream.py
git commit -m "feat(browser): loopback stream listener with hello/state/control"
```

---

### Task 6: Daemon — `ScreencastCdp` adapter, FRAME pipeline, INPUT dispatch

The only part written against browser-use's CDP surface. **Consult `docs/superpowers/plans/notes-browseruse-cdp-api.md` (Task 1) first** — if a call form recorded there differs from the code below, adapt ONLY inside `ScreencastCdp`. Not host-testable (needs Chromium); Task 7's conformance gate verifies it.

**Files:**
- Modify: `docker/browser-exec`

**Interfaces:**
- Consumes: Task 1 notes; `StreamHub.broadcast`; codec.
- Produces: real `ensure_screencast()` / `stop_screencast()` / `dispatch_user_input()` bodies; `INPUT` message contract `{"kind": "mouse"|"key"|"wheel", "params": {…CDP Input params…}}`.

- [x] **Step 1: Implement `ScreencastCdp`** (module level, after `StreamHub`):

```python
class ScreencastCdp:
    """In-process CDP adapter for screencast + input injection.

    Uses browser-use's own CDP client (session.get_or_create_cdp_session) —
    no port, no second connection; the streaming sibling of the proven
    take_screenshot() path. API verified by the Task-1 probe
    (docs/superpowers/plans/notes-browseruse-cdp-api.md); if the recorded
    call forms differ, adjust THIS CLASS ONLY.
    """

    def __init__(self, session, hub) -> None:
        self._session = session
        self._hub = hub
        self._cdp = None
        self._running = False

    async def start(self) -> None:
        self._cdp = await self._session.get_or_create_cdp_session()
        client = self._cdp.cdp_client
        client.register.Page.screencastFrame(self._on_frame_event)
        client.register.Page.frameNavigated(self._on_navigated_event)
        try:
            metrics = await client.send.Page.getLayoutMetrics(
                session_id=self._cdp.session_id
            )
            css = metrics.get("cssLayoutViewport") or {}
            self._hub.viewport = {
                "width": int(css.get("clientWidth", STREAM_MAX_WIDTH)),
                "height": int(css.get("clientHeight", STREAM_MAX_HEIGHT)),
            }
        except Exception:
            pass
        await client.send.Page.startScreencast(
            params={
                "format": "jpeg",
                "quality": STREAM_QUALITY,
                "maxWidth": STREAM_MAX_WIDTH,
                "maxHeight": STREAM_MAX_HEIGHT,
                "everyNthFrame": STREAM_EVERY_NTH,
            },
            session_id=self._cdp.session_id,
        )
        self._running = True
        log("screencast started")

    def _on_frame_event(self, params, session_id=None) -> None:
        asyncio.ensure_future(self._handle_frame(params))

    async def _handle_frame(self, params) -> None:
        if not self._running:
            return
        try:
            jpeg = base64.b64decode(params["data"])
            meta = params.get("metadata") or {}
            header = {
                "generation": self._hub.generation,
                "w": meta.get("deviceWidth"),
                "h": meta.get("deviceHeight"),
                "ts": time.time(),
            }
            self._hub.broadcast(
                encode_stream_frame(T_FRAME, encode_frame_payload(header, jpeg))
            )
        finally:
            # Ack AFTER hand-off to the per-viewer queues: CDP's built-in
            # backpressure then caps production at daemon speed while
            # drop-oldest queues absorb slow viewers.
            try:
                await self._cdp.cdp_client.send.Page.screencastFrameAck(
                    params={"sessionId": params["sessionId"]},
                    session_id=self._cdp.session_id,
                )
            except Exception:
                pass

    def _on_navigated_event(self, params, session_id=None) -> None:
        frame = params.get("frame") or {}
        if frame.get("parentId"):  # sub-frame navigation, not the page
            return
        self._hub.state_extra["url"] = frame.get("url")
        self._hub._notify()

    async def dispatch_input(self, event: dict) -> None:
        client = self._cdp.cdp_client
        sid = self._cdp.session_id
        kind = event.get("kind")
        params = event.get("params") or {}
        if kind == "mouse":
            await client.send.Input.dispatchMouseEvent(params=params, session_id=sid)
        elif kind == "key":
            await client.send.Input.dispatchKeyEvent(params=params, session_id=sid)
        elif kind == "wheel":
            await client.send.Input.dispatchMouseEvent(
                params={**params, "type": "mouseWheel"}, session_id=sid
            )

    async def stop(self) -> None:
        self._running = False
        if self._cdp is not None:
            try:
                await self._cdp.cdp_client.send.Page.stopScreencast(
                    session_id=self._cdp.session_id
                )
            except Exception:
                pass
        log("screencast stopped")
```

- [x] **Step 2: Replace the three Task-5 stubs on `BrowserDaemon`:**

```python
    async def ensure_screencast(self) -> None:
        """Start (or restart) the screencast while viewers exist."""
        if not self.hub.viewers:
            return
        if getattr(self, "_screencast", None) is not None and self._screencast._running:
            return
        async with self._lock:
            session = await self._get_session()
        self._screencast = ScreencastCdp(session, self.hub)
        await self._screencast.start()

    async def stop_screencast(self) -> None:
        screencast = getattr(self, "_screencast", None)
        self._screencast = None
        if screencast is not None:
            await screencast.stop()

    async def dispatch_user_input(self, event: dict) -> None:
        screencast = getattr(self, "_screencast", None)
        if screencast is not None and screencast._running:
            await screencast.dispatch_input(event)
```

Also add `self._screencast = None` to `BrowserDaemon.__init__`, and in `_close_session` (before `hub.clear_generation()`) add:

```python
        self._screencast = None
```

- [x] **Step 3: Sanity-check imports and syntax** (no Chromium on the host, so only a parse/test-suite check):

Run: `python -m pytest tests/tools/research/test_browser_exec_stream.py -v`
Expected: all 24 still PASS (proves the file still imports stdlib-only and nothing regressed).

- [x] **Step 4: Commit**

```bash
git add docker/browser-exec
git commit -m "feat(browser): CDP screencast pipeline and input dispatch"
```

---

### Task 7: Container conformance gate (podman, real Chromium)

End-to-end proof inside the actual workspace image: daemon spawns, stream authenticates, frames arrive, baton refuses the agent, control ops work. This is the Step-A acceptance gate. (Wiring it into `assert-browser-stack.sh` + the VM image provision is deliberately deferred to Plan 2 Step E, where VM validation happens.)

**Files:**
- Create: `docker/check-browser-stream.py`

**Interfaces:**
- Consumes: everything from Tasks 2–6, over the real unix socket + TCP listener.
- Produces: an executable check exiting 0 on pass — the future `assert-browser-stack.sh` `_check` target.

- [x] **Step 1: Write the check script** at `docker/check-browser-stream.py`:

```python
#!/usr/bin/env python3
"""Conformance check for browser-exec's shared-browser stream mode.

Runs INSIDE the workspace image (podman/CI), against the real daemon and
Chromium. Asserts: stream_info identity, hello-token auth, screencast frames,
baton refusal, control navigation. Exit 0 = pass.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

os.environ.setdefault("BROWSER_EXEC_SOCKET", "/tmp/bx-check.sock")
os.environ.setdefault("BROWSER_EXEC_PROFILE", "/tmp/bx-check-profile")
os.environ.setdefault("BROWSER_EXEC_LOG", "/tmp/bx-check.log")
os.environ.setdefault("BROWSER_EXEC_STREAM_PORT", "38801")

BROWSER_EXEC = os.environ.get("BROWSER_EXEC_BIN", "/usr/local/bin/browser-exec")

loader = importlib.machinery.SourceFileLoader("browser_exec_mod", BROWSER_EXEC)
spec = importlib.util.spec_from_loader("browser_exec_mod", loader)
BE = importlib.util.module_from_spec(spec)
loader.exec_module(BE)

PAGE = "/tmp/bx-check-page.html"


def unix_action(action: str, args: dict, timeout: float = 120.0) -> dict:
    req = json.dumps({"action": action, "args": args}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(os.environ["BROWSER_EXEC_SOCKET"])
        s.sendall(req)
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok      ' if ok else 'FAILED  '} {label}{'  ' + detail if detail else ''}")
    if not ok:
        sys.exit(1)


async def expect_frame(reader, wanted_type: int, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no frame of type {wanted_type}")
        ftype, payload = await asyncio.wait_for(
            BE.read_stream_frame(reader), timeout=remaining
        )
        if ftype == wanted_type:
            return payload


async def main() -> int:
    print("Shared-browser stream conformance:")
    with open(PAGE, "w") as fh:
        fh.write("<html><body><h1 id='t'>stream check</h1></body></html>")

    daemon = subprocess.Popen(
        [sys.executable, BROWSER_EXEC, "serve"],
        stdout=open("/tmp/bx-check.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        for _ in range(20):  # wait for the unix socket
            if os.path.exists(os.environ["BROWSER_EXEC_SOCKET"]):
                break
            time.sleep(0.5)

        info = unix_action("stream_info", {"initial_baton": "user"})
        check("stream_info returns identity", "generation" in info and "token" in info, str({k: info.get(k) for k in ("generation", "port", "baton")}))
        check("initial baton honoured", info.get("baton") == "user")

        port = int(info["port"])

        # Wrong token → unauthorized + close.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(BE.encode_stream_frame(BE.T_HELLO, json.dumps({"token": "nope", "min_protocol": 1}).encode()))
        await writer.drain()
        ftype, payload = await asyncio.wait_for(BE.read_stream_frame(reader), timeout=5)
        check("bad token rejected", ftype == BE.T_ERROR and json.loads(payload)["code"] == "unauthorized")
        writer.close()

        # Real viewer.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(BE.encode_stream_frame(BE.T_HELLO, json.dumps({"token": info["token"], "min_protocol": 1}).encode()))
        await writer.drain()
        state = json.loads(await expect_frame(reader, BE.T_STATE, timeout=5))
        check("STATE on connect", state["generation"] == info["generation"] and state["baton"] == "user")

        frame_payload = await expect_frame(reader, BE.T_FRAME)
        header, jpeg = BE.decode_frame_payload(frame_payload)
        check("screencast frame arrives", jpeg[:2] == b"\xff\xd8", f"{len(jpeg)} bytes")
        check("frame carries generation", header["generation"] == info["generation"])

        # Baton refusal: agent-side mutating action while user drives.
        resp = unix_action("navigate", {"url": f"file://{PAGE}"})
        check("agent navigate refused while user drives", resp.get("error") == "user_is_driving")

        # Read path still works and reports the baton.
        resp = unix_action("screenshot", {})
        check("agent screenshot allowed while user drives", "screenshot" in resp and resp.get("baton") == "user")

        # User navigates via CONTROL (file:// → LocalFileServer → validated).
        writer.write(BE.encode_stream_frame(BE.T_CONTROL, json.dumps({"op": "navigate", "url": f"file://{PAGE}"}).encode()))
        await writer.drain()
        state = json.loads(await expect_frame(reader, BE.T_STATE))
        check("control navigate lands", "127.0.0.1" in (state.get("url") or ""), state.get("url") or "")

        # Input dispatch does not error.
        writer.write(BE.encode_stream_frame(BE.T_INPUT, json.dumps({"kind": "mouse", "params": {"type": "mouseMoved", "x": 5, "y": 5}}).encode()))
        await writer.drain()
        await asyncio.sleep(0.5)

        # Release the baton → agent navigation works again.
        writer.write(BE.encode_stream_frame(BE.T_CONTROL, json.dumps({"op": "release_baton"}).encode()))
        await writer.drain()
        state = json.loads(await expect_frame(reader, BE.T_STATE))
        check("baton released", state["baton"] == "agent")
        resp = unix_action("navigate", {"url": f"file://{PAGE}"})
        check("agent navigate allowed after release", "error" not in resp, str(resp.get("error", "")))

        writer.close()
        print("Shared-browser stream conformance OK.")
        return 0
    finally:
        try:
            unix_action("shutdown", {}, timeout=10)
        except Exception:
            pass
        daemon.terminate()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [x] **Step 2: Make it executable and rebuild the image** (picks up the Task 2–6 daemon changes):

```bash
chmod +x docker/check-browser-stream.py
podman build -f docker/Dockerfile.workspace -t srw-workspace-stream-test .
```

- [x] **Step 3: Run the gate**

```bash
podman run --rm --entrypoint python3 \
  -v "$PWD/docker/check-browser-stream.py:/usr/local/bin/check-browser-stream:ro,Z" \
  srw-workspace-stream-test /usr/local/bin/check-browser-stream
```

Expected: every line `ok`, ending `Shared-browser stream conformance OK.`, exit 0. On failure, `podman run … cat /tmp/bx-check.log` equivalent (add `--rm=false` and inspect) shows the daemon log. Debug loop: fix `docker/browser-exec` (usually `ScreencastCdp` call forms vs the Task-1 notes), rebuild, rerun.

- [x] **Step 4: Commit**

```bash
git add docker/check-browser-stream.py
git commit -m "test(browser): container conformance gate for stream mode"
```

---

### Task 8: Orchestrator — fail-closed stream config

**Files:**
- Create: `orchestrator/services/browser_stream_config.py`
- Test: `tests/test_shared_browser_config.py`

**Interfaces:**
- Produces: `browser_stream_config() -> BrowserStreamConfig` with fields `enabled: bool`, `stream_port: int`, `max_viewers: int`, `connect_timeout_seconds: int`, `activity_interval_seconds: int`; `BrowserStreamConfigurationError`. Consumed by Tasks 10–12.

- [x] **Step 1: Write the failing tests**

```python
"""Fail-closed config for the shared-browser broker (mirrors canvas_viewer_config style)."""
import pytest

from services.browser_stream_config import (
    BrowserStreamConfigurationError,
    browser_stream_config,
)

_ENV = (
    "CANVAS_SHARED_BROWSER_ENABLED",
    "CANVAS_BROWSER_STREAM_PORT",
    "CANVAS_BROWSER_MAX_VIEWERS",
    "CANVAS_BROWSER_CONNECT_TIMEOUT_SECONDS",
    "CANVAS_BROWSER_ACTIVITY_INTERVAL_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default():
    assert browser_stream_config().enabled is False


def test_enabled_with_defaults(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    config = browser_stream_config()
    assert config.enabled is True
    assert config.stream_port == 38801
    assert config.max_viewers == 3
    assert config.connect_timeout_seconds == 10
    assert config.activity_interval_seconds == 60


def test_bounded_int_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "1")
    monkeypatch.setenv("CANVAS_BROWSER_STREAM_PORT", "80")  # below 1024 floor
    with pytest.raises(BrowserStreamConfigurationError):
        browser_stream_config()


def test_bounded_int_rejects_garbage(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "1")
    monkeypatch.setenv("CANVAS_BROWSER_MAX_VIEWERS", "lots")
    with pytest.raises(BrowserStreamConfigurationError):
        browser_stream_config()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shared_browser_config.py -v`
Expected: FAIL — `ModuleNotFoundError: services.browser_stream_config` (the repo conftest puts `orchestrator/` on `sys.path`).

- [x] **Step 3: Implement** `orchestrator/services/browser_stream_config.py`:

```python
"""Fail-closed configuration for the shared-browser stream broker.

Mirrors the canvas_viewer_config pattern: a frozen dataclass built by a
module-level factory that re-reads the environment at each call site —
no startup singleton to wire. Spec: docs/features/shared_browser.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class BrowserStreamConfigurationError(ValueError):
    """Raised when shared-browser stream configuration is unusable."""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BrowserStreamConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise BrowserStreamConfigurationError(
            f"{name} must be within [{minimum}, {maximum}]"
        )
    return value


@dataclass(frozen=True, slots=True)
class BrowserStreamConfig:
    enabled: bool
    stream_port: int
    max_viewers: int
    connect_timeout_seconds: int
    activity_interval_seconds: int


def browser_stream_config() -> BrowserStreamConfig:
    return BrowserStreamConfig(
        enabled=_truthy("CANVAS_SHARED_BROWSER_ENABLED"),
        stream_port=_bounded_int(
            "CANVAS_BROWSER_STREAM_PORT", 38801, minimum=1024, maximum=65535
        ),
        max_viewers=_bounded_int(
            "CANVAS_BROWSER_MAX_VIEWERS", 3, minimum=1, maximum=16
        ),
        connect_timeout_seconds=_bounded_int(
            "CANVAS_BROWSER_CONNECT_TIMEOUT_SECONDS", 10, minimum=1, maximum=120
        ),
        activity_interval_seconds=_bounded_int(
            "CANVAS_BROWSER_ACTIVITY_INTERVAL_SECONDS", 60, minimum=10, maximum=3600
        ),
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared_browser_config.py -v`
Expected: 4 PASS.

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/browser_stream_config.py tests/test_shared_browser_config.py
git commit -m "feat(canvas): shared-browser stream config (fail-closed)"
```

---

### Task 9: Orchestrator — broker helpers (codec mirror, workspace readiness, `stream_info` over SSH)

Shared helpers both the open endpoint and the WS relay use. The framing codec is intentionally mirrored from `docker/browser-exec` (the orchestrator cannot import a workspace file); the container gate exercises both ends together in dev.

**Files:**
- Create: `orchestrator/services/browser_stream_broker.py` (helpers half; the relay lands in Task 12)
- Test: `tests/test_shared_browser_broker.py`

**Interfaces:**
- Consumes: `build_agent_ssh_cmd` (`orchestrator/services/ssh_helpers.py:77`), Task 8 config.
- Produces: `T_HELLO..T_ERROR`, `encode_stream_frame(ftype, payload) -> bytes`, `async read_stream_frame(reader) -> tuple[int, bytes]`, `workspace_ready(thread: dict) -> bool`, `ssh_endpoint(thread: dict) -> tuple[str, int]` (raises `BrowserStreamUnavailable`), `async exec_stream_info(thread: dict, *, initial_baton: str | None = None) -> dict` (raises `BrowserStreamUnavailable`), `class BrowserStreamUnavailable(Exception)` with `.status` and `.detail`. Consumed by Tasks 11–12.

- [x] **Step 1: Write the failing tests**

```python
"""Broker helper tests: codec mirror, readiness, ssh endpoint resolution."""
import asyncio

import pytest

from services import browser_stream_broker as broker


class TestCodecMirror:
    def test_roundtrip(self):
        wire = broker.encode_stream_frame(broker.T_STATE, b'{"a":1}')
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        ftype, payload = asyncio.run(broker.read_stream_frame(reader))
        assert (ftype, payload) == (broker.T_STATE, b'{"a":1}')

    def test_length_covers_type_byte(self):
        wire = broker.encode_stream_frame(broker.T_HELLO, b"abc")
        assert wire[:4] == (4).to_bytes(4, "big")
        assert wire[4] == broker.T_HELLO


class TestWorkspaceResolution:
    def test_ready_container(self):
        thread = {"metadata": {"workspace_container": {"status": "ready", "ssh_host": "10.1.2.3", "ssh_port": 2222}}}
        assert broker.workspace_ready(thread) is True
        assert broker.ssh_endpoint(thread) == ("10.1.2.3", 2222)

    def test_vm_preferred_when_ready(self):
        thread = {"metadata": {
            "vm": {"status": "ready", "ssh_host": "100.99.1.2", "ssh_port": 22},
            "workspace_container": {"status": "ready", "ssh_host": "10.1.2.3", "ssh_port": 2222},
        }}
        assert broker.ssh_endpoint(thread) == ("100.99.1.2", 22)

    def test_not_ready(self):
        assert broker.workspace_ready({"metadata": {}}) is False
        with pytest.raises(broker.BrowserStreamUnavailable):
            broker.ssh_endpoint({"metadata": {}})


class TestExecStreamInfo:
    def test_parses_last_stdout_line(self, monkeypatch):
        async def fake_exec(*cmd, **kwargs):
            class Proc:
                returncode = 0
                async def communicate(self):
                    return (b'[browser-exec] noise\n{"generation": "g1", "token": "t", "port": 38801, "baton": "user"}\n', b"")
                def kill(self):
                    pass
            return Proc()

        monkeypatch.setattr(broker.asyncio, "create_subprocess_exec", fake_exec)
        thread = {"metadata": {"workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22}}}
        info = asyncio.run(broker.exec_stream_info(thread, initial_baton="user"))
        assert info["generation"] == "g1"

    def test_error_payload_raises(self, monkeypatch):
        async def fake_exec(*cmd, **kwargs):
            class Proc:
                returncode = 0
                async def communicate(self):
                    return (b'{"error": "could not reach browser-exec daemon"}\n', b"")
                def kill(self):
                    pass
            return Proc()

        monkeypatch.setattr(broker.asyncio, "create_subprocess_exec", fake_exec)
        thread = {"metadata": {"workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22}}}
        with pytest.raises(broker.BrowserStreamUnavailable):
            asyncio.run(broker.exec_stream_info(thread))
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shared_browser_broker.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [x] **Step 3: Implement** `orchestrator/services/browser_stream_broker.py` (helpers half):

```python
"""Shared-browser stream broker: helpers + WS↔SSH relay.

The framing constants/codec MIRROR docker/browser-exec (which the
orchestrator cannot import); the container conformance gate exercises both
ends of the protocol together. Spec: docs/features/shared_browser.md.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import struct

from services.browser_stream_config import browser_stream_config
from services.ssh_helpers import build_agent_ssh_cmd

T_HELLO, T_FRAME, T_STATE, T_INPUT, T_CONTROL, T_ERROR = 1, 2, 3, 4, 5, 6
MAX_STREAM_FRAME = 8 * 1024 * 1024
STREAM_INFO_TIMEOUT_S = 45.0  # first call cold-starts Chromium


class BrowserStreamUnavailable(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def encode_stream_frame(ftype: int, payload: bytes) -> bytes:
    if len(payload) + 1 > MAX_STREAM_FRAME:
        raise ValueError(f"stream frame too large: {len(payload) + 1}")
    return struct.pack(">IB", len(payload) + 1, ftype) + payload


async def read_stream_frame(reader) -> tuple[int, bytes]:
    head = await reader.readexactly(4)
    (length,) = struct.unpack(">I", head)
    if not 1 <= length <= MAX_STREAM_FRAME:
        raise ValueError(f"bad stream frame length: {length}")
    body = await reader.readexactly(length)
    return body[0], body[1:]


def _active_context(thread: dict) -> dict:
    """VM context when ready, else the container context — the same
    precedence resolve_remote_workspace_target applies (canvas_ssh.py:167)."""
    metadata = thread.get("metadata") or {}
    vm = metadata.get("vm") or {}
    if vm.get("status") == "ready":
        return vm
    return metadata.get("workspace_container") or {}


def workspace_ready(thread: dict) -> bool:
    return _active_context(thread).get("status") == "ready"


def ssh_endpoint(thread: dict) -> tuple[str, int]:
    context = _active_context(thread)
    if context.get("status") != "ready":
        raise BrowserStreamUnavailable(503, "workspace is not ready")
    host = context.get("ssh_host") or context.get("host") or context.get("pod_ip")
    if not host:
        raise BrowserStreamUnavailable(503, "workspace SSH endpoint unavailable")
    port = int(context.get("ssh_port") or context.get("port") or 22)
    return str(host), port


async def exec_stream_info(thread: dict, *, initial_baton: str | None = None) -> dict:
    """Run `browser-exec stream_info` on the workspace over SSH.

    Cold-starts the daemon+Chromium on first call; returns
    {generation, token, port, baton}.
    """
    host, port = ssh_endpoint(thread)
    args: dict = {}
    if initial_baton:
        args["initial_baton"] = initial_baton
    remote = f"browser-exec stream_info --json {shlex.quote(json.dumps(args))}"
    cmd = build_agent_ssh_cmd(host, port, remote, batch_mode=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=STREAM_INFO_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise BrowserStreamUnavailable(504, "browser start timed out")
    if proc.returncode != 0:
        raise BrowserStreamUnavailable(
            502, f"browser-exec unreachable: {err.decode(errors='replace')[-300:]}"
        )
    lines = [line for line in out.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        raise BrowserStreamUnavailable(502, "browser-exec returned no output")
    try:
        info = json.loads(lines[-1])
    except ValueError:
        raise BrowserStreamUnavailable(502, "browser-exec returned malformed output")
    if "error" in info:
        raise BrowserStreamUnavailable(502, str(info["error"]))
    return info
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared_browser_broker.py -v`
Expected: 7 PASS.

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/browser_stream_broker.py tests/test_shared_browser_broker.py
git commit -m "feat(canvas): browser stream broker helpers and framing mirror"
```

---

### Task 10: Orchestrator — `can_stream_browser` capability

**Files:**
- Modify: `orchestrator/services/canvas.py:160-165` (`CanvasCapabilities`)
- Modify: `orchestrator/routers/canvases.py:439-500` (`_represent`)
- Test: `tests/test_shared_browser_capability.py`

**Interfaces:**
- Consumes: `BrowserSource` (`canvas.py:102`), `browser_stream_config`, `workspace_ready`.
- Produces: `CanvasCapabilities.can_stream_browser: bool` in every canvas state response; `status == "ready"` for browser records when the feature is on and the workspace is up. Consumed by the Plan-2 cockpit renderer gate.

- [x] **Step 1: Write the failing test**

```python
"""Browser canvas records expose can_stream_browser when the feature is on."""
import asyncio
from uuid import uuid4

import pytest

from routers import canvases as canvas_routes
from services.canvas import BrowserSource, CanvasCapabilities, CanvasRecord


def _record():
    return CanvasRecord(
        thread_id="t1",
        canvas_id="main",
        source=BrowserSource(browser_generation=uuid4()),
        title="Shared browser",
        renderer="auto",
        editable=False,
        alt_text=None,
        source_version=None,
        presentation_revision=1,
        updated_at="2026-07-20T00:00:00Z",
    )


_READY_THREAD = {"metadata": {"workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22}}}


def test_capability_on_when_enabled_and_ready(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    state = asyncio.run(
        canvas_routes._represent("t1", _READY_THREAD, _record(), browser_content_url=False)
    )
    assert state.status == "ready"
    assert state.capabilities.can_stream_browser is True
    assert state.source.type == "browser"


def test_capability_off_when_disabled(monkeypatch):
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
    state = asyncio.run(
        canvas_routes._represent("t1", _READY_THREAD, _record(), browser_content_url=False)
    )
    assert state.status == "unavailable"
    assert state.capabilities.can_stream_browser is False


def test_capability_off_when_workspace_down(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    state = asyncio.run(
        canvas_routes._represent("t1", {"metadata": {}}, _record(), browser_content_url=False)
    )
    assert state.status == "unavailable"
    assert state.capabilities.can_stream_browser is False


def test_default_field_value():
    assert CanvasCapabilities().can_stream_browser is False
```

Note: if `CanvasRecord`'s constructor signature differs (check `orchestrator/services/canvas.py` around the `CanvasRecord` definition before writing), build the record with the actual fields — the assertion targets are the point, not the fixture shape.

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shared_browser_capability.py -v`
Expected: FAIL — `can_stream_browser` unknown field / capability False.

- [x] **Step 3: Implement.** In `orchestrator/services/canvas.py` extend `CanvasCapabilities`:

```python
class CanvasCapabilities(_StrictFrozenModel):
    can_edit: bool = False
    can_pop_out: bool = False
    can_take_control: bool = False
    can_create_viewer_session: bool = False
    can_stream_browser: bool = False
```

In `orchestrator/routers/canvases.py` add imports (`BrowserSource` to the existing `services.canvas` import list; `from services.browser_stream_config import browser_stream_config`; `from services.browser_stream_broker import workspace_ready`) and add a sibling branch at the end of `_represent`'s source dispatch (after the `WorkspaceAppSource` elif):

```python
    elif isinstance(record.source, BrowserSource):
        # Cheap metadata check only — the stream WS revalidates live
        # (generation + token) at attach time; never SSH here.
        if browser_stream_config().enabled and workspace_ready(thread):
            status = "ready"
            capabilities = CanvasCapabilities(
                can_pop_out=True,
                can_stream_browser=True,
            )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared_browser_capability.py -v`
Expected: 4 PASS. Also run `python -m pytest tests/test_canvas_slice0.py -v` — the slice-0 model tests must stay green (the new field defaults to False and the model is additive).

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/canvas.py orchestrator/routers/canvases.py tests/test_shared_browser_capability.py
git commit -m "feat(canvas): can_stream_browser capability on browser canvases"
```

---

### Task 11: Orchestrator — `POST …/browser/open`

The single cold-start + recovery path (spec Piece 2): lite-reject → ensure workspace (202 while provisioning) → `stream_info` over SSH → set the canvas `BrowserSource` through the existing control plane.

**Files:**
- Create: `orchestrator/routers/shared_browser.py`
- Modify: `orchestrator/routers/__init__.py` (export), `orchestrator/main.py` (include_router, ~line 7282)
- Test: `tests/test_shared_browser_open.py`

**Interfaces:**
- Consumes: `require_thread_owner` (`security.access`), `CanvasService.set` + `CanvasSetInput` + `BrowserSource` (`services.canvas`), `ensure_session_workspace` + main globals (`sessions.py:236-250` pattern), `LITE_BACKENDS` (`src.core.backends.factory`), Task 9 helpers.
- Produces: `POST /api/persistent/threads/{thread_id}/browser/open` → `200 {"status":"ready","generation":…,"stream_port":…}` | `202 {"status":"provisioning"}` | `404` flag off | `409 {"code":"workspace_required"}` lite | `5xx` per `BrowserStreamUnavailable`. `opened_by: "user"|"agent"` request field (default `"user"`) → daemon initial baton. Consumed by the Plan-2 cockpit button and agent `set_canvas` handling.

- [x] **Step 1: Write the failing tests**

```python
"""Open-endpoint flow tests with faked db/ssh/provisioning."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import shared_browser as sb
from services.browser_stream_broker import BrowserStreamUnavailable


_READY_THREAD = {
    "id": "t1",
    "metadata": {
        "workspace_backend": "container",
        "workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22},
    },
}


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(sb.router)

    async def fake_owner(request, db, thread_id):
        return {"id": "u1"}, dict(_READY_THREAD)

    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    monkeypatch.setattr(sb, "require_thread_owner", fake_owner)
    monkeypatch.setattr(sb, "_get_db", lambda: SimpleNamespace())
    return app, TestClient(app)


def test_flag_off_is_404(client, monkeypatch):
    app, tc = client
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
    resp = tc.post("/api/persistent/threads/t1/browser/open", json={})
    assert resp.status_code == 404


def test_lite_backend_is_409(client, monkeypatch):
    app, tc = client

    async def lite_owner(request, db, thread_id):
        return {"id": "u1"}, {"id": "t1", "metadata": {"workspace_backend": "virtual"}}

    monkeypatch.setattr(sb, "require_thread_owner", lite_owner)
    resp = tc.post("/api/persistent/threads/t1/browser/open", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "workspace_required"


def test_cold_workspace_is_202_and_kicks_provisioning(client, monkeypatch):
    app, tc = client
    kicked = {}

    async def cold_owner(request, db, thread_id):
        return {"id": "u1"}, {"id": "t1", "metadata": {"workspace_backend": "container"}}

    def fake_kick(thread_id, db):
        kicked["tid"] = thread_id

    monkeypatch.setattr(sb, "require_thread_owner", cold_owner)
    monkeypatch.setattr(sb, "_kick_workspace_provisioning", fake_kick)
    resp = tc.post("/api/persistent/threads/t1/browser/open", json={})
    assert resp.status_code == 202
    assert resp.json()["status"] == "provisioning"
    assert kicked["tid"] == "t1"


def test_ready_workspace_sets_canvas_and_returns_generation(client, monkeypatch):
    app, tc = client
    calls = {}

    async def fake_info(thread, *, initial_baton=None):
        calls["baton"] = initial_baton
        return {"generation": "5f0a9f5e-0000-4000-8000-000000000001", "token": "t" * 64, "port": 38801, "baton": initial_baton}

    class FakeCanvasService:
        async def set(self, thread_id, presentation):
            calls["source_type"] = presentation.source.type
            calls["generation"] = str(presentation.source.browser_generation)
            return SimpleNamespace(changed=True)

    monkeypatch.setattr(sb, "exec_stream_info", fake_info)
    monkeypatch.setattr(sb, "_get_canvas_service", lambda db: FakeCanvasService())
    resp = tc.post("/api/persistent/threads/t1/browser/open", json={"opened_by": "user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["generation"] == "5f0a9f5e-0000-4000-8000-000000000001"
    assert calls == {
        "baton": "user",
        "source_type": "browser",
        "generation": "5f0a9f5e-0000-4000-8000-000000000001",
    }


def test_browser_unreachable_maps_status(client, monkeypatch):
    app, tc = client

    async def fake_info(thread, *, initial_baton=None):
        raise BrowserStreamUnavailable(502, "browser-exec unreachable")

    monkeypatch.setattr(sb, "exec_stream_info", fake_info)
    resp = tc.post("/api/persistent/threads/t1/browser/open", json={})
    assert resp.status_code == 502
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shared_browser_open.py -v`
Expected: FAIL — `ModuleNotFoundError: routers.shared_browser`.

- [x] **Step 3: Implement** `orchestrator/routers/shared_browser.py`:

```python
"""Shared-browser open endpoint (docs/features/shared_browser.md, Piece 2).

One cold-start + recovery path: lite-reject → ensure workspace (202 while
provisioning) → browser-exec stream_info over SSH → set the canvas
BrowserSource through the existing control plane.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from security.access import require_thread_owner
from services.browser_stream_broker import (
    BrowserStreamUnavailable,
    exec_stream_info,
    workspace_ready,
)
from services.browser_stream_config import browser_stream_config
from services.canvas import BrowserSource, CanvasService, CanvasSetInput

router = APIRouter(
    prefix="/api/persistent/threads/{thread_id}/browser", tags=["shared-browser"]
)


def _get_db() -> Any:
    from main import postgres_db  # late-resolve; avoids an import cycle

    return postgres_db


def _get_canvas_service(db: Any) -> CanvasService:
    return CanvasService(db)


def _thread_backend(thread: dict) -> str:
    metadata = thread.get("metadata") or {}
    return str(metadata.get("workspace_backend") or "container")


def _is_lite_backend(thread: dict) -> bool:
    from src.core.backends.factory import LITE_BACKENDS

    return _thread_backend(thread) in LITE_BACKENDS


def _kick_workspace_provisioning(thread_id: str, db: Any) -> None:
    """Fire-and-forget ensure (sessions.py:236 pattern); callers poll open."""
    from main import (
        container_provisioner,
        ensure_session_workspace,
        workspace_suspension_service,
    )

    asyncio.create_task(
        ensure_session_workspace(
            thread_id,
            db=db,
            provisioner=container_provisioner,
            suspension=workspace_suspension_service,
        )
    )


class BrowserOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opened_by: Literal["user", "agent"] = "user"
    title: str | None = Field(default=None, max_length=200)


@router.post("/open")
async def open_shared_browser(
    thread_id: str, request: Request, body: BrowserOpenRequest
):
    config = browser_stream_config()
    if not config.enabled:
        raise HTTPException(status_code=404, detail="Shared browser is not enabled")
    db = _get_db()
    _, thread = await require_thread_owner(request, db, thread_id)

    if _is_lite_backend(thread):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workspace_required",
                "message": "The shared browser needs a full workspace; this "
                "session runs on a lite backend without one.",
            },
        )

    if not workspace_ready(thread):
        _kick_workspace_provisioning(thread_id, db)
        return JSONResponse(status_code=202, content={"status": "provisioning"})

    try:
        info = await exec_stream_info(thread, initial_baton=body.opened_by)
    except BrowserStreamUnavailable as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    await _get_canvas_service(db).set(
        thread_id,
        CanvasSetInput(
            source=BrowserSource(browser_generation=UUID(info["generation"])),
            title=body.title or "Shared browser",
            renderer="auto",
            editable=False,
        ),
    )
    return {
        "status": "ready",
        "generation": info["generation"],
        "stream_port": config.stream_port,
    }
```

Register it: in `orchestrator/routers/__init__.py`, export following the existing pattern (e.g. `from .shared_browser import router as shared_browser_router`), and in `orchestrator/main.py` add `app.include_router(shared_browser_router)` next to the existing block (~line 7282) with the matching import next to the other router imports.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared_browser_open.py -v`
Expected: 5 PASS.

- [x] **Step 5: Commit**

```bash
git add orchestrator/routers/shared_browser.py orchestrator/routers/__init__.py orchestrator/main.py tests/test_shared_browser_open.py
git commit -m "feat(canvas): shared-browser open endpoint"
```

---

### Task 12: Orchestrator — stream WebSocket relay

The binary WS ↔ SSH relay: auth-before-accept (ide_proxy_ws pattern, `main.py:14091`), `stream_info` revalidation, pinned SSH channel to the daemon's loopback listener, HELLO injection, twin pumps, viewer cap, activity marking.

**Files:**
- Modify: `orchestrator/services/browser_stream_broker.py` (relay half)
- Modify: `orchestrator/main.py` (WS route — WS routes live on `app` in this codebase, never on routers)
- Test: `tests/test_shared_browser_broker.py` (extend)

**Interfaces:**
- Consumes: `resolve_ws_user` (`security/auth.py:646`), `resolve_remote_workspace_target` + `bound_workspace_generation` + `PINNED_SSH_TRANSPORT_POOL` (`services/canvas_ssh.py`), `resolve_ssh_key_path` (`services/__init__.py:8`), `db.get_thread`, `db.merge_thread_workspace_context` (`postgres.py:3064` — bumps `threads.last_activity`), Task 9 helpers.
- Produces: `async relay_browser_stream(ws, thread_id, *, db)`; WS route `GET /api/persistent/threads/{thread_id}/browser/stream`; WS message contract: binary `[1-byte type][payload]` both directions; close codes `4404` disabled, `4401` unauthenticated, `4403` not owner/approved, `4503` workspace not ready, `4429` viewer cap, `4409` generation mismatch vs canvas, `4502` upstream unreachable.

- [x] **Step 1: Write the failing tests** (append to `tests/test_shared_browser_broker.py`)

```python
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _ws_app():
    app = FastAPI()

    @app.websocket("/stream/{thread_id}")
    async def stream(ws: WebSocket, thread_id: str):
        from main import postgres_db  # replaced by monkeypatch in tests

        await broker.relay_browser_stream(ws, thread_id, db=postgres_db)

    return app


def _connect_and_capture_close(client, url) -> int:
    try:
        with client.websocket_connect(url) as ws:
            ws.receive_bytes()
        return 1000
    except WebSocketDisconnect as exc:
        return exc.code


class TestRelayAuthGates:
    def test_disabled_closes_4404(self, monkeypatch):
        monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
        app = _ws_app()
        # db never touched when the flag is off
        monkeypatch.setattr("main.postgres_db", object(), raising=False)
        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4404

    def test_unauthenticated_closes_4401(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")

        async def no_user(ws, db):
            return None

        monkeypatch.setattr(broker, "resolve_ws_user", no_user)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", object(), raising=False)
        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4401

    def test_stale_generation_closes_4409(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")

        async def fake_user(ws, db):
            return {"id": "u1", "is_approved": True}

        async def fake_info(thread, **kwargs):
            return {"generation": "g-new", "token": "tok", "port": 38801}

        class StaleRecord:
            source = SimpleNamespace(browser_generation="g-old")

        async def fake_record(db, thread_id):
            return StaleRecord()

        thread = {
            "id": "t1",
            "user_id": "u1",
            "metadata": {"workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22}},
        }

        class FakeDB:
            async def get_thread(self, tid):
                return dict(thread)

        monkeypatch.setattr(broker, "resolve_ws_user", fake_user)
        monkeypatch.setattr(broker, "exec_stream_info", fake_info)
        monkeypatch.setattr(broker, "_get_canvas_record", fake_record)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", FakeDB(), raising=False)
        assert _connect_and_capture_close(TestClient(app), "/stream/t1") == 4409


class TestRelayHappyPath:
    def test_relays_state_frame_and_sends_hello(self, monkeypatch):
        monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
        written = []

        class FakeSSHReader:
            def __init__(self):
                state = broker.encode_stream_frame(broker.T_STATE, b'{"baton":"user"}')
                self._chunks = [state[:4], state[4:]]

            async def readexactly(self, n):
                if not self._chunks:
                    await asyncio.sleep(3600)  # hold the channel open
                chunk = self._chunks.pop(0)
                assert len(chunk) == n
                return chunk

        class FakeSSHWriter:
            def write(self, data):
                written.append(bytes(data))

            async def drain(self):
                pass

        @asynccontextmanager
        async def fake_open(**kwargs):
            yield FakeSSHReader(), FakeSSHWriter()

        async def fake_user(ws, db):
            return {"id": "u1", "is_approved": True}

        async def fake_info(thread, **kwargs):
            return {"generation": "g1", "token": "tok", "port": 38801}

        thread = {
            "id": "t1",
            "user_id": "u1",
            "metadata": {"workspace_container": {"status": "ready", "ssh_host": "h", "ssh_port": 22}},
        }

        class FakeDB:
            async def get_thread(self, tid):
                return dict(thread)

            async def merge_thread_workspace_context(self, tid, updates):
                return True

        monkeypatch.setattr(broker, "resolve_ws_user", fake_user)
        monkeypatch.setattr(broker, "exec_stream_info", fake_info)
        monkeypatch.setattr(broker, "_resolve_target", lambda thread: object())
        monkeypatch.setattr(broker, "_resolve_key_path", lambda: "/tmp/key")
        monkeypatch.setattr(broker, "_open_loopback", fake_open)
        app = _ws_app()
        monkeypatch.setattr("main.postgres_db", FakeDB(), raising=False)

        with TestClient(app).websocket_connect("/stream/t1") as ws:
            first = ws.receive_bytes()
            assert first[0] == broker.T_STATE
            assert first[1:] == b'{"baton":"user"}'
            ws.send_bytes(bytes([broker.T_CONTROL]) + b'{"op":"take_baton"}')

        # HELLO went upstream first, then the relayed CONTROL
        assert written[0][4] == broker.T_HELLO
        assert b'"token": "tok"' in written[0] or b'"token":"tok"' in written[0]
        assert any(w[4] == broker.T_CONTROL for w in written[1:])
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shared_browser_broker.py -v`
Expected: new tests FAIL — `relay_browser_stream` missing.

- [x] **Step 3: Implement the relay** (append to `orchestrator/services/browser_stream_broker.py`):

```python
# ── WS ↔ SSH relay ───────────────────────────────────────────────────

from security.auth import resolve_ws_user  # noqa: E402  (grouped with relay)
from services import resolve_ssh_key_path  # noqa: E402
from services.canvas_ssh import (  # noqa: E402
    PINNED_SSH_TRANSPORT_POOL,
    bound_workspace_generation,
    resolve_remote_workspace_target,
)

# Seams the tests monkeypatch; production uses the canvas SSH machinery.
def _resolve_target(thread: dict):
    return resolve_remote_workspace_target(
        dict(thread), bound_workspace_generation(thread)
    )


async def _get_canvas_record(db, thread_id: str):
    from services.canvas import CanvasService

    try:
        return await CanvasService(db).get(thread_id)
    except Exception:
        return None


def _resolve_key_path() -> str:
    key_path = resolve_ssh_key_path()
    if not key_path:
        raise BrowserStreamUnavailable(503, "workspace SSH key unavailable")
    return key_path


def _open_loopback(**kwargs):
    return PINNED_SSH_TRANSPORT_POOL.open_loopback_connection(**kwargs)


_ACTIVE_VIEWERS: dict[str, int] = {}


async def relay_browser_stream(ws, thread_id: str, *, db) -> None:
    """Authenticated binary WS ↔ pinned-SSH relay to the workspace stream
    listener. WS messages carry [1-byte type][payload]; the TCP side adds the
    4-byte length prefix. The broker never parses payloads."""
    config = browser_stream_config()
    if not config.enabled:
        await ws.close(code=4404, reason="Shared browser is not enabled")
        return
    user = await resolve_ws_user(ws, db)
    if not user:
        await ws.close(code=4401, reason="Authentication required")
        return
    if not user.get("is_approved"):
        await ws.close(code=4403, reason="Account pending approval")
        return
    thread = await db.get_thread(thread_id)
    if not thread or str(thread.get("user_id") or "") != str(user.get("id") or ""):
        await ws.close(code=4403, reason="Thread access denied")
        return
    if not workspace_ready(thread):
        await ws.close(code=4503, reason="Workspace not ready")
        return
    if _ACTIVE_VIEWERS.get(thread_id, 0) >= config.max_viewers:
        await ws.close(code=4429, reason="Viewer limit reached")
        return

    try:
        info = await exec_stream_info(thread)  # revalidates daemon + identity
    except BrowserStreamUnavailable:
        await ws.close(code=4502, reason="Browser unreachable")
        return

    # Generation pinning (dynamic_canvas.md Slice-5 contract): the canvas
    # pointer must never silently follow a different browser. If the daemon's
    # live generation no longer matches the staged canvas source, the viewer
    # gets an explicit ended signal (4409) and restarts via /browser/open.
    record = await _get_canvas_record(db, thread_id)
    staged = getattr(getattr(record, "source", None), "browser_generation", None)
    if staged is not None and str(staged) != str(info.get("generation")):
        await ws.close(code=4409, reason="Browser generation ended")
        return

    try:
        target = _resolve_target(thread)
        key_path = _resolve_key_path()
    except Exception:
        await ws.close(code=4503, reason="Workspace SSH unavailable")
        return

    async def generation_resolver() -> dict:
        current = await db.get_thread(thread_id)
        return dict(current) if current else {}

    await ws.accept()
    _ACTIVE_VIEWERS[thread_id] = _ACTIVE_VIEWERS.get(thread_id, 0) + 1
    try:
        async with _open_loopback(
            target=target,
            destination_port=config.stream_port,
            key_path=key_path,
            generation_resolver=generation_resolver,
        ) as (reader, writer):
            hello = json.dumps({"token": info["token"], "min_protocol": 1}).encode()
            writer.write(encode_stream_frame(T_HELLO, hello))
            await writer.drain()

            async def ws_to_tcp():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("bytes")
                    if not data or data[0] not in (T_INPUT, T_CONTROL):
                        continue  # viewers may only send input/control
                    writer.write(encode_stream_frame(data[0], data[1:]))
                    await writer.drain()

            async def tcp_to_ws():
                while True:
                    ftype, payload = await read_stream_frame(reader)
                    await ws.send_bytes(bytes([ftype]) + payload)

            async def touch_activity():
                while True:
                    await asyncio.sleep(config.activity_interval_seconds)
                    try:
                        # Bumps threads.last_activity (postgres.py:3064) so the
                        # idle sweeper never reaps a watched workspace.
                        await db.merge_thread_workspace_context(thread_id, {})
                    except Exception:
                        pass

            tasks = [
                asyncio.create_task(ws_to_tcp()),
                asyncio.create_task(tcp_to_ws()),
                asyncio.create_task(touch_activity()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
    except Exception:
        try:
            await ws.close(code=4502, reason="stream ended")
        except Exception:
            pass
    finally:
        count = _ACTIVE_VIEWERS.get(thread_id, 1) - 1
        if count <= 0:
            _ACTIVE_VIEWERS.pop(thread_id, None)
        else:
            _ACTIVE_VIEWERS[thread_id] = count
        try:
            await ws.close()
        except Exception:
            pass
```

Note the import placement: `resolve_ws_user`, `resolve_ssh_key_path`, and the `canvas_ssh` imports go at the **top of the module** with the other imports (the `# noqa` markers above are only needed if you keep them mid-file — prefer the top). Then add the WS route in `orchestrator/main.py`, next to `ide_proxy_ws` (~line 14200):

```python
@app.websocket("/api/persistent/threads/{thread_id}/browser/stream")
async def shared_browser_stream_ws(ws: WebSocket, thread_id: str):
    """Shared-browser screencast/input relay (docs/features/shared_browser.md)."""
    from services.browser_stream_broker import relay_browser_stream

    await relay_browser_stream(ws, thread_id, db=postgres_db)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared_browser_broker.py -v`
Expected: all PASS (11 total).

- [x] **Step 5: Commit**

```bash
git add orchestrator/services/browser_stream_broker.py orchestrator/main.py tests/test_shared_browser_broker.py
git commit -m "feat(canvas): shared-browser stream WebSocket relay"
```

---

### Task 13: Helm wiring + infra gate + full check

The five-file flag pattern (matching `canvas.livePreview.enabled` exactly), a slice3-infra-style gate test, and the plan's final verification sweep.

**Files:**
- Modify: `helm/values.yaml` (~line 336, under `canvas:`), `helm/values.schema.json` (~line 129, under `canvas.properties`), `helm/templates/configmap.yaml` (~line 104), `helm/templates/orchestrator/deployment.yaml` (~line 128), `deployment/values-experimental.yaml` (~line 146, under `canvas:`)
- Test: `tests/test_shared_browser_infra.py`

**Interfaces:**
- Produces: `canvas.sharedBrowser.enabled` (default `false`) → env `CANVAS_SHARED_BROWSER_ENABLED` on the orchestrator; dev profile on.

- [x] **Step 1: Write the failing test**

```python
"""Helm gate for canvas.sharedBrowser (pattern: test_canvas_slice3_infra)."""
import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def test_shared_browser_helm_gate_is_boolean_and_default_off():
    values = yaml.safe_load((REPO / "helm" / "values.yaml").read_text())
    assert values["canvas"]["sharedBrowser"]["enabled"] is False
    schema = json.loads((REPO / "helm" / "values.schema.json").read_text())
    shared = schema["properties"]["canvas"]["properties"]["sharedBrowser"]
    assert shared["properties"]["enabled"]["type"] == "boolean"


def test_shared_browser_env_reaches_orchestrator():
    configmap = (REPO / "helm" / "templates" / "configmap.yaml").read_text()
    assert "CANVAS_SHARED_BROWSER_ENABLED" in configmap
    assert ".Values.canvas.sharedBrowser.enabled" in configmap
    deployment = (
        REPO / "helm" / "templates" / "orchestrator" / "deployment.yaml"
    ).read_text()
    assert "CANVAS_SHARED_BROWSER_ENABLED" in deployment


def test_dev_profile_enables_shared_browser():
    experimental = yaml.safe_load(
        (REPO / "deployment" / "values-experimental.yaml").read_text()
    )
    assert experimental["canvas"]["sharedBrowser"]["enabled"] is True
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_shared_browser_infra.py -v`
Expected: FAIL — `KeyError: 'sharedBrowser'`.

- [x] **Step 3: Implement the five files.**

`helm/values.yaml` — under `canvas:`, sibling of `livePreview:`:

```yaml
  # Shared browser (docs/features/shared_browser.md): stream the workspace
  # Chromium into the canvas. Dark by default; dev profile enables it.
  sharedBrowser:
    enabled: false
```

`helm/values.schema.json` — under `canvas.properties`, sibling of `livePreview`:

```json
"sharedBrowser": {
  "type": "object",
  "properties": {
    "enabled": { "type": "boolean" }
  }
}
```

`helm/templates/configmap.yaml` — next to `CANVAS_LIVE_PREVIEW_ENABLED` (~line 104):

```yaml
  CANVAS_SHARED_BROWSER_ENABLED: {{ ternary "true" "false" .Values.canvas.sharedBrowser.enabled | quote }}
```

`helm/templates/orchestrator/deployment.yaml` — next to the `CANVAS_LIVE_PREVIEW_ENABLED` block (~line 128):

```yaml
        - name: CANVAS_SHARED_BROWSER_ENABLED
          valueFrom:
            configMapKeyRef:
              name: {{ include "srw.configMapName" . }}
              key: CANVAS_SHARED_BROWSER_ENABLED
```

`deployment/values-experimental.yaml` — under the existing `canvas:` block:

```yaml
  sharedBrowser:
    enabled: true
```

- [x] **Step 4: Run test to verify it passes, then lint the chart**

Run: `python -m pytest tests/test_shared_browser_infra.py -v` → 3 PASS.
Run: `helm lint helm/` → `1 chart(s) linted, 0 chart(s) failed`.

- [x] **Step 5: Full plan verification sweep**

```bash
python -m pytest tests/test_shared_browser_config.py tests/test_shared_browser_broker.py \
  tests/test_shared_browser_open.py tests/test_shared_browser_capability.py \
  tests/test_shared_browser_infra.py tests/tools/research/test_browser_exec_stream.py -v
python -m pytest tests/test_canvas_slice0.py tests/test_canvas_slice3_infra.py -v
```

Expected: all PASS (the canvas slice suites prove no regression in the models/infra the plan touched).

- [x] **Step 6: Commit**

```bash
git add helm/values.yaml helm/values.schema.json helm/templates/configmap.yaml \
  helm/templates/orchestrator/deployment.yaml deployment/values-experimental.yaml \
  tests/test_shared_browser_infra.py
git commit -m "feat(canvas): helm gate for canvas.sharedBrowser + infra tests"
```

---

## Deferred to Plan 2 (Steps C+D+E — do NOT do here)

- Cockpit renderer/controller/toolbar, open button, reconnect UI, i18n.
- Screencast re-attach on active-target change (popups/new tabs): v1 streams
  the target attached at start; a viewer reconnect restarts the screencast via
  `ensure_screencast`. Event-driven re-attach is Plan-2 polish.
- Agent-side `set_canvas` browser advertisement + `user_is_driving` refusal surfacing in `browser_direct.py` tool results.
- `internal_set_main_canvas` accepting `source_type: "browser"` (agent path).
- Wiring `check-browser-stream.py` into `assert-browser-stack.sh` + VM image provisioning.
- Live k3d smoke + `docs/tests/` verification record; dev-cluster validation of activity marking and idle interplay.
