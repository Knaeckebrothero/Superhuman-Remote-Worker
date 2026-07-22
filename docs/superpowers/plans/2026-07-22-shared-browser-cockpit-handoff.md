# Shared Browser — Plan 2 of 2: The Window

> **Execution mode:** Run inline in the primary session with a checkpoint after
> every task. Every implementation checkbox below starts unchecked; update it
> only after the named verification has passed.
>
> **Plan status (2026-07-22):** Tasks 1–13 are implemented and verified.
> Task 14 is partial: repository, container, Cockpit, Helm, and substantial
> live-container gates pass, while VM artifact, prompt-driven LLM, and full
> pod-rotation acceptance remain blocked by the recorded environment gaps.

**Goal:** A session owner can click **Open browser** in Cockpit, watch and drive
the exact Chromium used by the agent, hand control back and forth without
action races, and recover visibly from reconnects or an ended browser
generation. The container-workspace path finishes with a real-browser local
k3d acceptance record.

**Architecture:** Plan 1 supplied the loopback `browser-exec` stream and the
orchestrator WebSocket relay. Plan 2 first closes the remaining trust and
lifecycle gaps in that pipe: browser startup moves onto the pinned SSH pool,
the public API gains an owner-scoped pre-source capability and authoritative
open response, the WebSocket enforces Origin and command limits, and the daemon
follows browser-use's active target while emitting real loading transitions.
The agent then gains the `set_canvas(browser_id="current")` adapter and
prompt-visible baton results. Cockpit adds a pane-local binary protocol
adapter, a bounded bitmap pipeline, a canvas renderer, cold-open workflow, and
the input/control toolbar. Image conformance, three-engine Playwright, and live
k3d gates close the plan.

**Tech stack:** Python 3.12, FastAPI, asyncssh, browser-use 0.12.9/CDP, Angular
21 signals and standalone components, RxJS, Vitest 4, Playwright 1.59,
Podman/Packer, Helm, k3d/Tilt.

**Primary specs:**

- `docs/features/shared_browser.md` — browser identity, stream, baton, and
  handoff authority.
- `docs/features/dynamic_canvas.md` — trusted Canvas host and Slice-5
  lifecycle authority.
- `docs/superpowers/plans/2026-07-20-shared-browser-pipe.md` — implemented
  Plan-1 baseline.
- `docs/superpowers/plans/notes-browseruse-cdp-api.md` — real 0.12.9 CDP call
  forms.

## Execution record — 2026-07-22

Inline execution produced these path-scoped commits; nothing was pushed:

| Task | Commit |
|---|---|
| 1 | `2e103cb9` |
| 2 | `23389dfb` |
| 3 | `438f87f6` |
| 4 | `343a39a4` |
| 5 | `e2a642a5` |
| 6 | `4f2fc468` |
| 7 | `3bfd1a2c` |
| 8 | `9fdd152e` |
| 9 | `b5e59aba` |
| 10 | `0fad013f` |
| 11 | `0e107915` |
| 12 | `9b1e3ba3` |
| 13 | `0c691202` |
| Live admission fix | `e208fae9` |
| Live service-restart fix | `5296ccaa` |

Final automated evidence: 649 focused Python tests; Ruff lint; all Plan-2
Python files independently Ruff-formatted; 95 Cockpit files / 1,347 tests;
i18n parity; production build; 33 Chromium/Firefox/WebKit conformance cases;
both Helm lint overlays and default/experimental renders; and a fresh Podman
real-Chromium image conformance run. Live container evidence includes cold
open, shared executor identity, baton/refusal, bad-navigation containment,
popout, viewer cap, activity, zero-viewer release, clean-1012 reconnect, and
ended/restart. See `docs/tests/shared_browser_verification.md` for the redacted
record and the exact incomplete gates. Because those gates are incomplete,
`deployment/values-tilt.yaml` remains disabled.

**Next-plan handoff:** Plan 3 should be a release-and-enablement plan, not a
third implementation of the browser. It inherits the four unchecked Task-14
steps: VM image conformance, prompt-driven same-browser/form-cookie acceptance,
full live failure-boundary acceptance on healthy k3d networking, and guarded
Tilt enablement. Agent-initiated takeover requests and pushed baton events stay
outside that release plan as later product work.

## Baseline and release boundary

Plan 1 is already implemented: the daemon framing/listener/screencast/baton,
the default-off orchestrator broker/open route, generation-pinned relay,
activity marking, and Helm gate all exist. Its verified baseline is 55
shared-browser tests, 40 selected Canvas regressions, and a real Chromium
Podman conformance pass.

Plan 2 releases **attested container workspaces**. It also installs and runs
the identical stream conformance program in the VM golden-image build, so the
VM artifact cannot drift. It does **not** turn a guest self-report into SSH
trust and does not pretend the orchestrator can route to `100.64.0.0/10`.
VM runtime capability remains false unless a deployment independently provides
both:

1. a provisioner-attested remote binding with an exact Ed25519 fingerprint;
2. an orchestrator route to the selected VM address.

That is an explicit fail-closed acceptance case in Tasks 2, 6, and 14. Building
a trusted VM-host-key enrollment path or adding an orchestrator tailnet sidecar
is a separate infrastructure/security design, not an assumption hidden in this
UI plan.

## Locked public contracts

### Pre-source capability

Canvas state cannot advertise an open button when no Canvas row exists because
`GET .../canvases/main` correctly returns `204`. Add this owner-authenticated
endpoint:

    GET /api/persistent/threads/{thread_id}/browser/capability

Its closed response is:

    {
      "feature_enabled": true,
      "can_open_browser": true,
      "workspace_ready": false,
      "reason": null
    }

`reason` is null or one of:

- `feature_disabled`
- `workspace_required`
- `workspace_unattested`
- `workspace_unroutable`
- `transport_unavailable`

Rules:

- Disabled flag: `feature_enabled=false` and `can_open_browser=false`.
- `virtual`/`none`: feature remains discoverable, but open is disabled with
  `workspace_required`.
- A cold `sandbox`/legacy `container` session may return
  `can_open_browser=true, workspace_ready=false`; `open` owns provisioning.
- A ready target is openable only if its remote binding, generation, host-key
  fingerprint, key path, and route all pass the existing Canvas SSH authority.
- A VM or unknown/custom remote without those proofs is
  `workspace_unattested`; a proven tailnet target without an orchestrator route
  is `workspace_unroutable`.
- The response contains no host, port, backing ID, workspace generation,
  browser generation, fingerprint, or token.

### Open

    POST /api/persistent/threads/{thread_id}/browser/open
    {"title": "Shared browser"}

The public caller always requests `opened_by=user` for a newly created browser
generation. It cannot ask the server to give the initial baton to the agent.
The delegated internal Canvas adapter requests `opened_by=agent` through the
shared service, not through this public body. `initial_baton` is creation-only:
re-opening an existing generation never steals or flips its current baton.

- Cold workspace: `202 {"status":"provisioning"}` plus `Retry-After: 1`.
- Ready browser: `200` with the ordinary `CanvasPublicState` as the body and
  its strong `ETag` plus `X-Canvas-Mutation-Changed: true|false` response
  headers.
- Re-opening the same generation with the same normalized presentation is a
  no-op: no revision increment and no duplicate invalidation.
- A new browser generation is a new logical Canvas source and increments the
  presentation revision exactly once.
- The ready response does not expose the browser generation or stream port.

The Browser Canvas representation has:

    status = "ready"
    capabilities.can_pop_out = true
    capabilities.can_take_control = true
    capabilities.can_stream_browser = true

The dedicated capability answers “may the owner create/recover a browser?”;
`can_stream_browser` answers “may this already-staged browser source attach?”.
They are deliberately not aliases.

### WebSocket

    GET /api/persistent/threads/{thread_id}/browser/stream

The URL carries no token or generation. The browser supplies only the BFF
session cookie and its normal `Origin` header.

| Close | Meaning | Cockpit behavior |
|---|---|---|
| `4400` | malformed/non-binary/oversized client command | terminal protocol error |
| `4401` | no valid session | terminal authentication state |
| `4403` | bad Origin, unapproved user, or no ownership | terminal authorization state |
| `4404` | feature disabled | terminal unsupported state |
| `4409` | staged generation absent/different | ended state + Restart |
| `4429` | viewer limit | explicit limit state + manual retry |
| `4502` | stream/SSH transient failure | bounded-backoff reconnect |
| `4503` | workspace unavailable/unroutable | unavailable state + Retry/Restart |

Only binary `INPUT` and `CONTROL` client frames are accepted, each at most
64 KiB including the type byte. Server frames remain bounded by Plan 1's
8 MiB protocol cap. The broker rechecks owner, current binding, Canvas
generation, and feature gate after the potentially long `stream_info` call and
before accepting the WebSocket. The replica-local fast path uses close `4429`;
the daemon's cross-replica final cap may instead send bounded
`ERROR {"code":"viewer_limit"}` and close, which Cockpit maps to the same
explicit viewer-limit state without making the broker parse page protocol.

### Cockpit source identity

The public Browser source remains only `{"type":"browser"}`. Cockpit never
stores or renders a concrete generation. For lifecycle identity it uses:

    browser:<presentation_revision>

An idempotent open preserves that key; a replacement generation changes it.
This lets a restart auto-open as a new logical source without leaking private
identity.

## Global constraints

- Work directly on `develop`. Make one path-scoped commit after each task.
  Never push; the user pushes and CI rewrites deployment SHAs.
- Preserve the owner's dirty/untracked work, especially the nested `HomeLab/`
  checkout. Never stage it.
- Before every listed `git add`, re-read `git status --short` and the diff for
  each path. The command assumes those paths had no pre-existing owner edits;
  if they do, stage only Plan-2 hunks or defer the commit and ask rather than
  sweeping owner work into it. Never stash, reset, or rebase owner changes.
- Keep `canvas.sharedBrowser.enabled=false` in `helm/values.yaml`. The
  experimental profile stays enabled; the committed Tilt overlay is enabled
  only after Task 14's live gate passes.
- No browser/CDP listener may bind outside `127.0.0.1`. No CDP URL, SSH target,
  token, generation, or fingerprint crosses the public or model-facing
  boundary.
- Do not change the `browser-use>=0.12.9,<0.13.0` or Playwright pins.
- `docker/browser-exec` remains extensionless, stdlib-importable on the host,
  and lazy-imports browser-use only inside runtime functions.
- Preserve daemon defaults: JPEG 60, 1280×720 cap, everyNth 2, 30-second
  no-viewer baton release, 8 MiB server frame cap, three viewers.
- The decoded bitmap dimensions own the HTML canvas backing store. CDP frame
  metadata never does.
- The pane and the whole document detach their stream when hidden. Every
  replaced/stale `ImageBitmap` is explicitly closed.
- Never overwrite an unsaved Canvas edit from a user “Open browser” action.
  Disable the action with explanatory copy while the editor is dirty.
- Update both `en.json` and `de-DE.json` for every user-visible string.
- Use symbols, not line numbers, as anchors: the plan was written against
  `develop` on 2026-07-22 and lines will move task by task.

## Task order

    1 pinned SSH command
      -> 2 capability/open action
      -> 3 WebSocket hardening
      -> 4 daemon target/loading/viewer lifecycle
      -> 5 image conformance
      -> 6 agent Canvas surface
      -> 7 agent baton result UX
      -> 8 Cockpit protocol/models
      -> 9 Cockpit socket/bitmap controller
      -> 10 view-only renderer
      -> 11 open workflow/button
      -> 12 driving + baton toolbar
      -> 13 reconnect/popout/i18n/browser E2E
      -> 14 full gates/live k3d/docs/enablement

## File map

| Surface | Planned files |
|---|---|
| Pinned command | `orchestrator/services/canvas_ssh.py`, `browser_stream_broker.py` |
| Browser action/capability | `orchestrator/services/shared_browser_canvas.py` (new), `routers/shared_browser.py`, `routers/canvases.py`, `services/canvas.py` |
| WS browser boundary | `orchestrator/security/csrf.py`, `browser_stream_broker.py` |
| Daemon lifecycle | `docker/browser-exec`, `tests/tools/research/test_browser_exec_stream.py` |
| Image gate | `docker/check-browser-stream.py`, both workspace-image definitions/provisioners, CI/Tilt watch lists |
| Agent surface | `src/core/workspace_backend.py`, `src/api/persistent_session.py`, `persistent_app.py`, `src/tools/canvas/__init__.py`, `browser_direct.py` |
| Cockpit protocol/controller | `canvas-browser-protocol.ts`, `canvas-browser.controller.ts` and specs (new) |
| Cockpit renderer/control | `canvas-browser-renderer.component.ts/.scss/.spec.ts`, `canvas-browser-input.ts/.spec.ts` (new), Canvas pane/chat/service files |
| Browser E2E | `cockpit/e2e/canvas/shared-browser-conformance.spec.ts`, fixture server/config |
| Release record | `docs/tests/shared_browser_verification.md` (new), both feature docs, this plan |

---

### Task 1: Run `stream_info` through pinned, generation-bound SSH

**Files:**

- Modify: `orchestrator/services/canvas_ssh.py`
- Modify: `orchestrator/services/browser_stream_broker.py`
- Test: `tests/test_canvas_ssh_transport.py`
- Test: `tests/test_shared_browser_broker.py`

**Produces:**

    @dataclass(frozen=True, slots=True)
    class PinnedSSHCommandResult:
        exit_status: int
        stdout: bytes
        stderr: bytes

    async def PinnedSSHTransportPool.run_command(
        *,
        target: RemoteWorkspaceTarget,
        command: str,
        key_path: str,
        generation_resolver: GenerationResolver,
        timeout: float,
        max_output_bytes: int,
    ) -> PinnedSSHCommandResult

    async def exec_stream_info(
        thread: dict,
        *,
        initial_baton: str | None,
        generation_resolver: GenerationResolver,
    ) -> dict

- [x] **Step 1: Write failing transport tests.** Cover an exact pinned target,
  pre-command and post-command generation revalidation, host-key mismatch,
  non-zero command status, timeout, cancellation cleanup, a 64 KiB combined
  stdout/stderr limit, and transport invalidation on a broken connection.
  A remote command failure is channel-scoped and must not evict an otherwise
  healthy pooled transport.

- [x] **Step 2: Add `run_command`.** Lease via `checkout`, call
  `require_same_remote_workspace` before and after the remote process, read
  stdout and stderr concurrently into bounded byte buffers, and put the
  process under `asyncio.timeout`. On timeout/cancellation/output overflow,
  terminate and bounded-wait the remote process. Reuse
  `_bounded_wait_closed`. Do not add a local listener and do not call a shell
  `ssh` binary.

- [x] **Step 3: Move `exec_stream_info`.** Resolve
  `bound_workspace_generation -> resolve_remote_workspace_target -> key path`,
  reject `not orchestrator_can_reach(target.host)`, and invoke exactly:

      browser-exec stream_info --json '<canonical JSON>'

  through `PINNED_SSH_TRANSPORT_POOL.run_command`. Keep the existing strict
  generation/token/baton/port response validation. Delete
  `build_agent_ssh_cmd`, `create_subprocess_exec`, `_kill_and_reap`, and the
  raw `ssh_endpoint` path from this feature. Map command failures to stable,
  bounded `BrowserStreamUnavailable` messages; never return or log token-bearing
  stdout, raw stderr, the target, fingerprint, key path, or full command.

- [x] **Step 4: Prove the regression.** A test must patch
  `build_agent_ssh_cmd`/`asyncio.create_subprocess_exec` to raise if reached
  while a fake pinned pool returns the identity. Another test changes the
  generation during the command and expects a typed unavailable result. A
  non-zero fake result containing sentinel secrets in both streams must return
  a generic error with neither sentinel in the detail or captured logs.

- [x] **Step 5: Run:**

      python -m pytest tests/test_canvas_ssh_transport.py \
        tests/test_shared_browser_broker.py -q
      ruff check orchestrator/services/canvas_ssh.py \
        orchestrator/services/browser_stream_broker.py \
        tests/test_canvas_ssh_transport.py tests/test_shared_browser_broker.py

  Expected: all named tests pass and `rg "build_agent_ssh_cmd|StrictHostKeyChecking"
  orchestrator/services/browser_stream_broker.py` returns no matches.

- [x] **Step 6: Commit:**

      git add orchestrator/services/canvas_ssh.py \
        orchestrator/services/browser_stream_broker.py \
        tests/test_canvas_ssh_transport.py tests/test_shared_browser_broker.py
      git commit -m "fix(canvas): pin shared-browser startup SSH"

---

### Task 2: Add the pre-source capability and authoritative open action

**Files:**

- Create: `orchestrator/services/shared_browser_canvas.py`
- Modify: `orchestrator/routers/shared_browser.py`
- Modify: `orchestrator/routers/canvases.py`
- Modify: `orchestrator/services/canvas.py`
- Test: `tests/test_shared_browser_capability.py`
- Test: `tests/test_shared_browser_open.py`
- Test: `tests/test_canvas_slice0.py`

**Produces:**

    class BrowserCapabilityResponse(BaseModel):
        feature_enabled: bool
        can_open_browser: bool
        workspace_ready: bool
        reason: BrowserCapabilityReason | None

    def browser_capability(thread: dict) -> BrowserCapabilityResponse

    async def prepare_browser_canvas(
        thread: dict,
        *,
        initial_baton: Literal["agent", "user"],
        generation_resolver: GenerationResolver,
    ) -> PreparedBrowser

    async def commit_browser_canvas(
        db,
        thread_id: str,
        prepared: PreparedBrowser,
        *,
        title: str,
    ) -> CanvasMutation

- [x] **Step 1: Write the capability matrix first.** Test flag off, cold
  sandbox, cold VM, both lite tiers, ready-but-unbound, bad fingerprint,
  mismatched generation, missing key, unroutable tailnet, and a fully attested
  reachable container. Assert the public model has exactly the four locked
  fields and contains none of the private metadata used to decide.

- [x] **Step 2: Implement `browser_capability`.** Reuse
  `_thread_backend`, `remote_canvas_presentation_available`,
  `resolve_remote_workspace_target`, `resolve_ssh_key_path`, and
  `orchestrator_can_reach`. Do not duplicate a looser “ready” check. Cold open
  is positive only for `sandbox`/legacy `container` because their provisioner
  is already capable of creating the attested binding.

- [x] **Step 3: Add `GET /capability`.** Authenticate and authorize the owner
  before returning the response, including while the feature is disabled.
  An absent/unauthorized thread must not be distinguishable through this route.

- [x] **Step 4: Add an idempotent Canvas mutation.** Add
  `CanvasService.set_if_changed` (or an equivalently named browser-specific
  method as one atomic PostgreSQL transaction. Take a transaction-scoped
  advisory lock derived from a domain-separated hash of `thread_id + canvas_id`
  **before** selecting the row `FOR UPDATE`; the advisory lock also serializes
  the missing-row case that a row lock cannot protect. Compare normalized
  source JSON/fingerprint plus the stored title/renderer/editable/alt/
  source-version columns with null-safe semantics. Return the existing row with
  `changed=false` on equality; otherwise insert/update and advance exactly
  once. Keep this path browser-specific (`new_app=false`,
  `source_version=null`, no origin generation). Do not implement it as an
  unlocked read followed by `set`, and do **not** change ordinary `set`
  semantics: file/app republishing may intentionally advance a revision.

- [x] **Step 5: Factor prepare/commit.** `prepare_browser_canvas` calls the
  pinned `exec_stream_info` and validates a UUID generation. The route then
  re-runs owner admission and the capability gate before
  `commit_browser_canvas` revalidates the selected workspace target and writes
  `BrowserSource`. This prevents a long browser start from committing after an
  owner, flag, workspace generation, or endpoint change.

- [x] **Step 6: Change public `POST /open`.** Remove `opened_by` from
  `BrowserOpenRequest`; public calls always prepare with creation-time holder
  `user`, without changing an existing generation's baton. Preserve the `202`
  provisioning behavior. For `200`, build the ordinary public browser
  representation, return it through the existing `_state_response` shape with
  `ETag` and `X-Canvas-Mutation-Changed`, and expose no generation/port.

- [x] **Step 7: Complete the staged representation.** In `_represent` set all
  three positive Browser capabilities only when the same live capability
  check passes:

      CanvasCapabilities(
          can_pop_out=True,
          can_take_control=True,
          can_stream_browser=True,
      )

  A stale/unattested Browser source stays `unavailable` with all capabilities
  false.

- [x] **Step 8: Test idempotence and races.** Cover: first open revision 1;
  same generation/same title stays revision 1; changed title advances once;
  ended/new generation advances once; two concurrent identical opens produce
  one logical transition; owner/flag/generation change between prepare and
  commit fails without changing Canvas. Assert the mutation header is true
  only for the caller that caused a durable transition, and an idempotent open
  never changes the existing daemon baton.

- [x] **Step 9: Run:**

      python -m pytest tests/test_shared_browser_capability.py \
        tests/test_shared_browser_open.py tests/test_canvas_slice0.py -q
      ruff check orchestrator/services/shared_browser_canvas.py \
        orchestrator/routers/shared_browser.py orchestrator/routers/canvases.py \
        orchestrator/services/canvas.py tests/test_shared_browser_capability.py \
        tests/test_shared_browser_open.py

  Expected: all pass; an open-ready test asserts both body validation and a
  strong `ETag`.

- [x] **Step 10: Commit:**

      git add orchestrator/services/shared_browser_canvas.py \
        orchestrator/routers/shared_browser.py orchestrator/routers/canvases.py \
        orchestrator/services/canvas.py tests/test_shared_browser_capability.py \
        tests/test_shared_browser_open.py tests/test_canvas_slice0.py
      git commit -m "feat(canvas): add shared-browser open capability"

---

### Task 3: Harden the browser WebSocket boundary

**Files:**

- Modify: `orchestrator/security/csrf.py`
- Modify: `orchestrator/services/browser_stream_broker.py`
- Test: `tests/test_csrf.py`
- Test: `tests/test_shared_browser_broker.py`

**Produces:**

    def allowed_browser_origins() -> frozenset[str]
    def websocket_origin_allowed(headers) -> bool

    MAX_BROWSER_CLIENT_MESSAGE = 64 * 1024

- [x] **Step 1: Export one normalized origin authority.** Refactor the current
  CORS-mirroring origin set from `csrf.py` into a public helper used by both
  HTTP CSRF and this WebSocket. Trim environment entries, accept only canonical
  `http://`/`https://` origins with no credentials/path/query/fragment, and
  retain the four current localhost development origins.

- [x] **Step 2: Add failing WS tests.** Require exactly one non-`null` Origin.
  Cover allowed same-origin/dev origins, absent Origin, duplicate Origin,
  malformed origin, cross-site origin, and an allowed environment origin.
  Origin is the first admission check—even while the feature is disabled—so a
  cross-site probe cannot distinguish gate/auth/thread state. Rejection happens
  before `accept` and closes `4403`.

- [x] **Step 3: Bound client messages.** Text, empty, unknown-type, malformed
  ASGI receive shape, or a binary message larger than 64 KiB closes `4400`.
  Do not silently keep a bad protocol connection alive. Valid `INPUT` and
  `CONTROL` remain opaque to the relay.

- [x] **Step 4: Re-admit after startup.** After `exec_stream_info` returns,
  fetch the thread again and recheck approval/owner, feature flag, remote
  binding/route, and the latest durable Browser Canvas generation. Only then
  retain the viewer reservation and call `accept`. Reserve once after the
  initial admission but before the long startup, so one owner cannot bypass the
  per-replica startup cap with a handshake burst. The `generation_resolver`
  used by both the command and direct channel always reloads the thread.

- [x] **Step 5: Make viewer accounting exception-safe.** Reserve exactly once,
  release only if reserved, and test failures before accept, during SSH open,
  after accept, and during cancellation. Immediate activity marking remains
  before the first sleep.

- [x] **Step 6: Run:**

      python -m pytest tests/test_csrf.py tests/test_shared_browser_broker.py -q
      ruff check orchestrator/security/csrf.py \
        orchestrator/services/browser_stream_broker.py \
        tests/test_csrf.py tests/test_shared_browser_broker.py

  Expected: all pass, including `4400`, `4403`, and `4409` assertions.

- [x] **Step 7: Commit:**

      git add orchestrator/security/csrf.py \
        orchestrator/services/browser_stream_broker.py \
        tests/test_csrf.py tests/test_shared_browser_broker.py
      git commit -m "fix(canvas): harden shared-browser WebSocket admission"

---

### Task 4: Follow the active target and emit real loading state

**Files:**

- Modify: `docs/superpowers/plans/notes-browseruse-cdp-api.md`
- Modify: `docker/browser-exec`
- Modify: `orchestrator/services/browser_stream_broker.py`
- Modify: `tests/tools/research/test_browser_exec_stream.py`
- Modify: `tests/test_shared_browser_broker.py`

**Verified browser-use 0.12.9 seam:**

    from browser_use.browser.events import AgentFocusChangedEvent

    session.event_bus.on(AgentFocusChangedEvent, handler)
    target_id = session.agent_focus_target_id
    cdp = await session.get_or_create_cdp_session(target_id=target_id)

- [x] **Step 1: Extend the probe note before implementation.** In the existing
  built `srw-workspace-stream-test` image, record the exact
  `AgentFocusChangedEvent` import, `event_bus.on` call, event fields, focused
  target field, and `get_or_create_cdp_session(target_id=...)` call. Also record
  working registrations for `Page.frameStartedLoading` and
  `Page.frameStoppedLoading`. If any call differs, adapt this task to the
  observed 0.12.9 API before editing the daemon.

- [x] **Step 2: Turn `ScreencastCdp` into a session-lifetime adapter.** It
  tracks the current CDP/session ID, main frame ID, registered CDP clients, and
  running state. `start(target_id=None)` may switch targets; it stops the prior
  target, attaches the requested/current focus target, registers each client
  callback at most once, primes viewport/url/title/main-frame state, then starts
  the new screencast. Every callback ignores stale session IDs.

- [x] **Step 3: Fix zero-viewer restart.** `stop_screencast` stops the current
  screencast but retains the adapter and its callback-registration inventory.
  A later viewer calls `start` on that same adapter. Only
  `_close_session` discards it. This prevents callback multiplication on every
  hide/reveal cycle.

- [x] **Step 4: Subscribe once to active-focus changes.** When a new
  `BrowserSession` is created, install one synchronous event handler which only
  schedules work and returns. The scheduled task coalesces to
  `session.agent_focus_target_id` and takes locks in the existing order before
  switching the adapter. It must never await the daemon action lock inside the
  event-bus callback itself; browser-use may dispatch the event from the action
  currently holding that lock.

- [x] **Step 5: Emit loading transitions.** Prime the main frame with
  `Page.getFrameTree`. A main-frame `frameStartedLoading` sets
  `loading=true`. `frameStoppedLoading` sets `loading=false` and schedules a
  stale-session-guarded URL/title refresh. Main `frameNavigated` updates the
  frame ID and URL. Subframe events do nothing.

- [x] **Step 6: Enforce the cross-replica viewer cap in the daemon.** Extend
  HELLO with integer `max_viewers` from the broker config, validated in
  `1..16` with booleans rejected. `StreamHub.add_viewer` atomically refuses the
  fourth default viewer across all orchestrator replicas because every relay
  terminates at the same daemon. While viewers remain, differently configured
  rolling replicas may tighten but never relax the session's effective limit;
  at zero viewers the next authenticated HELLO may establish the new limit.
  Return `ERROR {"code":"viewer_limit"}` and close without starting a
  screencast. Keep the broker's local reservation as a fast path; the daemon
  is the final global authority.

- [x] **Step 7: Add focused unit tests.** Cover main/subframe loading,
  stale-target events, target A→B switch, newest-target coalescing, frame ACK on
  old target without rebroadcast, no callback duplication across three
  stop/start cycles, no switch with zero viewers, adapter teardown on browser
  close, invalid HELLO limits, mixed rolling-replica limits, and global viewer
  refusal.

- [x] **Step 8: Run:**

      python -m pytest tests/tools/research/test_browser_exec_stream.py \
        tests/test_shared_browser_broker.py -q
      ruff check orchestrator/services/browser_stream_broker.py \
        tests/test_shared_browser_broker.py \
        tests/tools/research/test_browser_exec_stream.py
      python -m py_compile docker/browser-exec

  Expected: all pass; compile emits no output.

- [x] **Step 9: Commit:**

      git add docs/superpowers/plans/notes-browseruse-cdp-api.md \
        docker/browser-exec orchestrator/services/browser_stream_broker.py \
        tests/tools/research/test_browser_exec_stream.py \
        tests/test_shared_browser_broker.py
      git commit -m "feat(browser): follow active shared-browser target"

---

### Task 5: Make stream conformance a build gate for both workspace images

**Files:**

- Modify: `docker/check-browser-stream.py`
- Modify: `docker/assert-browser-stack.sh`
- Modify: `docker/Dockerfile.workspace`
- Modify: `docker/agent-vm-base/stage2.pkr.hcl`
- Modify: `docker/agent-vm-base/scripts/provision-stage2.sh`
- Modify: `.github/workflows/develop.yml`
- Modify: `Tiltfile`
- Modify: `tests/test_shared_browser_infra.py`

- [x] **Step 1: Strengthen and clean the conformance program.** In addition to
  Plan 1's identity/frame/input/baton checks, make it:

  - observe `loading=true -> loading=false` for a main navigation;
  - open/click a `target="_blank"` page and receive STATE + JPEG from the new
    active target without reconnecting;
  - prove a configured one-viewer cap rejects the second connection;
  - disconnect/reconnect and prove callbacks do not duplicate frames/state;
  - close every file/socket/process and remove its socket, profile, HTML, and
    log artifacts in `finally` so the image layer stays clean.

- [x] **Step 2: Install the check in the container image.** Copy it to
  `/usr/local/bin/check-browser-stream` beside `browser-exec`, mode 0755.
  Extend `assert-browser-stack.sh` with:

      _check "shared-browser stream conformance" \
        /usr/local/bin/check-browser-stream

  The assertion still runs as the actual workspace user in the VM build.

- [x] **Step 3: Install the exact same file in VM stage 2.** Add
  `../check-browser-stream.py` to the Packer file sources and install it in
  `provision-stage2.sh` before running the shared assertion. Do not copy a
  second implementation under `agent-vm-base/files`.

- [x] **Step 4: Fix rebuild watch lists.**

  - WORKSPACE and Tilt `only`: add `docker/check-browser-stream.py`.
  - VM_BASE: add `docker/check-browser-stream.py`.
  - Keep `assert-browser-stack.sh` in VM_BASE_STAGE1 because changing the
    shared required capability must still rebuild the dependency layer.

- [x] **Step 5: Extend infra tests.** Assert both images source/install the
  same check, the shared assertion invokes it, and every relevant CI/Tilt
  change detector watches it.

- [x] **Step 6: Run the real container gate:**

      podman build -f docker/Dockerfile.workspace \
        -t srw-workspace-shared-browser-plan2 .
      podman run --rm --user agent-host \
        --entrypoint /usr/local/bin/assert-browser-stack \
        srw-workspace-shared-browser-plan2

  Expected: both build-time and explicit runs end with:

      Shared-browser stream conformance OK.
      Workspace browser stack OK.

- [x] **Step 7: Validate VM plumbing without claiming runtime routing:**

      bash -n docker/assert-browser-stack.sh
      bash -n docker/agent-vm-base/scripts/provision-stage2.sh
      (
        cd docker/agent-vm-base
        packer init stage2.pkr.hcl
        packer validate stage2.pkr.hcl
      )
      python -m pytest tests/test_shared_browser_infra.py -q

  Expected: all exit zero. The actual stage-2 image build is a Task-14/CI gate.

- [x] **Step 8: Commit:**

      git add docker/check-browser-stream.py docker/assert-browser-stack.sh \
        docker/Dockerfile.workspace docker/agent-vm-base/stage2.pkr.hcl \
        docker/agent-vm-base/scripts/provision-stage2.sh \
        .github/workflows/develop.yml Tiltfile tests/test_shared_browser_infra.py
      git commit -m "test(browser): gate both workspace images on streaming"

---

### Task 6: Let the agent present the current browser through Canvas

**Files:**

- Modify: `orchestrator/main.py`
- Modify: `orchestrator/routers/canvases.py`
- Modify: `src/core/workspace_backend.py`
- Modify: `src/api/persistent_session.py`
- Modify: `src/api/persistent_app.py`
- Modify: `src/api/orchestrator_client.py`
- Modify: `src/tools/canvas/__init__.py`
- Test: `tests/test_shared_browser_internal.py` (new)
- Test: `tests/test_canvas_tool.py`
- Test: `tests/test_canvas_slice3_callable.py`
- Test: `tests/test_persistent_session.py`
- Test: `tests/test_persistent_app.py`
- Test: `tests/test_orchestrator_client_canvas.py`

**Produces:**

    WorkspaceBackend.supports_canvas_shared_browser: bool = False

    @dataclass(frozen=True, slots=True)
    class CanvasSetResult:
        state: dict[str, Any]
        changed: bool

    set_canvas(
        source_type="browser",
        browser_id="current",
        title="Shared browser",
    )

- [x] **Step 1: Add a positive attach capability.** Extend
  `_agent_canvas_workspace_capabilities` and the internal workspace response
  with `canvas_shared_browser_available`. It is true only when:

  - `canvas_presentation_available` is already true;
  - `CANVAS_SHARED_BROWSER_ENABLED` is currently true;
  - the selected target is reachable from the orchestrator.

  Hydrate it onto `RemoteBackend.supports_canvas_shared_browser` during initial
  attach and both hot-swap paths. Default and every lite/custom/VM-unattested
  path remain false. The agent consumes this orchestrator-attested bit; it does
  not independently infer support from an environment variable.

- [x] **Step 2: Extend the closed internal request.** Add `browser` to
  `CanvasSetRequest.source_type` and:

      browser_id: Literal["current"] | None = None

  Browser requires exactly `browser_id="current"`, `renderer="auto"`,
  `editable=false`, no file/app fields, no alt text, and `new_app=false`.
  Every other source kind rejects `browser_id`.

- [x] **Step 3: Add the internal browser branch.** After delegated owner
  admission, call Task 2's shared prepare path with creation-time holder
  `agent` (without flipping an existing generation), re-admit delegated owner
  and capability after the long call, commit
  idempotently, and return the public Canvas state + ETag +
  `X-Canvas-Mutation-Changed`. Return that header for existing file/app set
  branches as true too, so the internal client has one closed response
  contract. A stale agent whose capability was withdrawn receives a typed
  server rejection and cannot stage a browser.

- [x] **Step 4: Build exact capability-scoped schemas.** Preserve today's
  file-only and file+port schemas. Add file+browser and file+port+browser
  variants chosen from:

      supports_canvas_live_apps
      supports_canvas_shared_browser

  Browser variants expose `browser_id` but still reject every cross-kind field.
  The four JSON schemas must advertise only the forms the current backend can
  execute.

- [x] **Step 5: Extend the tool body.** The browser payload sent through
  `set_thread_canvas` is exactly:

      {
        "source_type": "browser",
        "browser_id": "current",
        "renderer": "auto",
        "editable": false
      }

  Add title only when supplied. The model-facing response remains
  `source={"type":"browser"}`; never return a generation, token, endpoint, or
  stream port. Make `OrchestratorClient.set_thread_canvas` return
  `CanvasSetResult`, accepting an absent header as `true` only for rolling
  compatibility with the old always-changing file/app endpoint and rejecting
  any present value other than `true|false`. Emit the ordinary `canvas.updated`
  invalidation only when `changed=true`.

- [x] **Step 6: Update tool metadata/docstrings.** Say the tool can present a
  file, an attested live port, or the current shared browser when the matching
  capability is advertised. Explain that `browser_id="current"` resolves at
  call time and that control may remain with the user.

- [x] **Step 7: Test all four schema combinations.** Assert required and
  rejected fields, exact payloads, agent initial baton, idempotent repeat,
  logical redaction, post-commit event, stale-gate rejection, and no event on
  failure or idempotent no-op. Include client parsing for true/false/absent/
  malformed mutation headers and a hot sandbox→unattested-VM swap that
  withdraws browser advertisement.

- [x] **Step 8: Run:**

      python -m pytest tests/test_shared_browser_internal.py \
        tests/test_canvas_tool.py tests/test_canvas_slice3_callable.py \
        tests/test_persistent_session.py tests/test_persistent_app.py \
        tests/test_orchestrator_client_canvas.py -q
      ruff check orchestrator/main.py orchestrator/routers/canvases.py \
        src/core/workspace_backend.py src/api/persistent_session.py \
        src/api/persistent_app.py src/api/orchestrator_client.py \
        src/tools/canvas/__init__.py \
        tests/test_shared_browser_internal.py tests/test_canvas_tool.py \
        tests/test_orchestrator_client_canvas.py

- [x] **Step 9: Commit:**

      git add orchestrator/main.py orchestrator/routers/canvases.py \
        src/core/workspace_backend.py src/api/persistent_session.py \
        src/api/persistent_app.py src/api/orchestrator_client.py \
        src/tools/canvas/__init__.py \
        tests/test_shared_browser_internal.py tests/test_canvas_tool.py \
        tests/test_canvas_slice3_callable.py tests/test_persistent_session.py \
        tests/test_persistent_app.py tests/test_orchestrator_client_canvas.py
      git commit -m "feat(canvas): let agents present the shared browser"

---

### Task 7: Surface baton state and user-driving refusals to the model

**Files:**

- Modify: `src/tools/research/browser_direct.py`
- Test: `tests/tools/research/test_browser_tools.py`

- [x] **Step 1: Write two failing result-format tests.**

  A successful snapshot with `baton="user"` must contain:

      Browser control: user

  A daemon refusal:

      {
        "error": "user_is_driving",
        "url": "https://example.test/form",
        "message": "..."
      }

  must become clear prompt-visible prose containing the current URL, that
  read-only snapshots still work, and that the agent should ask the user to
  release control. It must not collapse to
  `Browser error: user_is_driving`.

- [x] **Step 2: Implement the special case in `_page_state_to_text`.** Use the
  daemon's bounded `message` when present, but key behavior on the stable error
  code. Keep all other error formatting unchanged. Do not wrap or interpret
  page DOM on an error result.

- [x] **Step 3: Add the control line to successful state.** Accept only
  `agent` or `user` and ignore unknown future values. Place it with URL/title,
  before nonce-wrapped DOM. Screenshot extraction remains byte-for-byte
  unchanged.

- [x] **Step 4: Run:**

      python -m pytest tests/tools/research/test_browser_tools.py -q
      ruff check src/tools/research/browser_direct.py \
        tests/tools/research/test_browser_tools.py

  Expected: all pass; existing nonce and image-tag tests remain green.

- [x] **Step 5: Commit:**

      git add src/tools/research/browser_direct.py \
        tests/tools/research/test_browser_tools.py
      git commit -m "feat(browser): surface shared-browser control handoff"

---

### Task 8: Define the fail-closed Cockpit protocol and models

**Files:**

- Modify: `cockpit/src/app/core/models/canvas.model.ts`
- Modify: `cockpit/src/app/core/services/canvas.service.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-rendering.ts`
- Create: `cockpit/src/app/views/canvas/canvas-browser-protocol.ts`
- Create: `cockpit/src/app/views/canvas/canvas-browser-protocol.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-rendering.spec.ts`
- Modify: `cockpit/src/app/core/services/canvas.service.spec.ts`

**Produces:**

    type CanvasTrustedRenderer = ... | 'browser'

    interface BrowserCapability {
      feature_enabled: boolean;
      can_open_browser: boolean;
      workspace_ready: boolean;
      reason: BrowserCapabilityReason | null;
    }

    parseBrowserServerMessage(data: ArrayBuffer): BrowserServerMessage | null
    encodeBrowserControl(message: BrowserControl): ArrayBuffer
    encodeBrowserInput(message: BrowserInput): ArrayBuffer
    browserStreamUrl(threadId: string, apiUrl?: string): string | null

- [x] **Step 1: Extend public models fail-closed.** Add optional
  `can_stream_browser?: boolean` to `CanvasCapabilities` and the locked
  `BrowserCapability` model/reason union. Absence of either positive bit means
  unavailable. Keep `BrowserCanvasSource` generation-free.

- [x] **Step 2: Select the renderer only on a double gate.**

      source.type === 'browser'
      renderer === 'auto'
      capabilities.can_stream_browser === true

  Otherwise return `unsupported`. Set `canvasSourceKey` for browser to
  `browser:<presentation_revision>`. Add selection/key tests, including a
  same-revision idempotent open and a new-revision restart.

- [x] **Step 3: Define protocol constants and closed types.** Mirror
  `HELLO=1, FRAME=2, STATE=3, INPUT=4, CONTROL=5, ERROR=6`. Server parser accepts
  only `FRAME/STATE/ERROR`. Client encoders produce only `INPUT/CONTROL`.

- [x] **Step 4: Parse STATE under strict bounds.** Require:

  - canonical UUID generation (kept protocol-private);
  - baton `agent|user`;
  - integer viewport width/height in `1..8192`;
  - URL/title null or bounded strings;
  - boolean loading;
  - UTF-8 JSON at most 64 KiB, no arrays, no extra type coercion.

  Return a public controller state without generation plus an internal
  generation value used only to pin frames.

- [x] **Step 5: Parse FRAME exactly.** WebSocket data is:

      [1-byte type][2-byte big-endian header length][header JSON][raw JPEG]

  Validate bounds, canonical generation, finite timestamp, optional positive
  header dimensions, JPEG SOI, and total 8 MiB cap. Keep the JPEG as a bounded
  slice. Header `w/h` are diagnostic only and must never size the renderer.

- [x] **Step 6: Parse ERROR.** Accept only bounded `code`/`message` strings.
  Unknown server types and malformed payloads return null; they never become
  partially trusted state.

- [x] **Step 7: Derive the socket URL.** Starting from
  `environment.apiUrl`, require HTTP(S), no credentials/query/fragment, and a
  normalized API path. Convert protocol to WS(S) and append the encoded thread
  path. Reject control characters, ambiguous slashes/backslashes, and invalid
  base URLs. Never append a query parameter.

- [x] **Step 8: Tighten `isCanvasState`.** Validate an optional
  `can_stream_browser` only when boolean and keep forward compatibility for
  other optional capabilities. Add invalid-type tests.

- [x] **Step 9: Run:**

      (
        cd cockpit
        npx vitest run src/app/views/canvas/canvas-browser-protocol.spec.ts \
          src/app/views/canvas/canvas-rendering.spec.ts \
          src/app/core/services/canvas.service.spec.ts
      )

  Expected: all pass.

- [x] **Step 10: Commit:**

      git add cockpit/src/app/core/models/canvas.model.ts \
        cockpit/src/app/core/services/canvas.service.ts \
        cockpit/src/app/core/services/canvas.service.spec.ts \
        cockpit/src/app/views/canvas/canvas-rendering.ts \
        cockpit/src/app/views/canvas/canvas-rendering.spec.ts \
        cockpit/src/app/views/canvas/canvas-browser-protocol.ts \
        cockpit/src/app/views/canvas/canvas-browser-protocol.spec.ts
      git commit -m "feat(cockpit): define shared-browser stream protocol"

---

### Task 9: Build the pane-local socket and bitmap controller

**Files:**

- Create: `cockpit/src/app/views/canvas/canvas-browser.controller.ts`
- Create: `cockpit/src/app/views/canvas/canvas-browser.controller.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.ts`

**Produces:**

    type CanvasBrowserConnectionStatus =
      | 'idle'
      | 'connecting'
      | 'ready'
      | 'reconnecting'
      | 'ended'
      | 'viewer_limit'
      | 'unauthorized'
      | 'unavailable'
      | 'error';

    class CanvasBrowserController {
      readonly connectionStatus;
      readonly pageState;
      readonly frame;
      readonly errorCode;
      syncPresentation(active, threadId, state): void;
      retry(): void;
      sendControl(message): boolean;
      sendInput(message): boolean;
    }

- [x] **Step 1: Add injectable browser seams for tests.** Provide pane-local
  tokens/factories for WebSocket construction, `createImageBitmap`, timeout,
  and document visibility. Production defaults use native browser APIs; tests
  use deterministic fakes. Do not put the controller in `providedIn:root`.

- [x] **Step 2: Implement desired-source identity.** Connect only when the pane
  is active, the document is visible, the selected thread exists, source is
  Browser, status is `ready|starting`, and `can_stream_browser===true`.
  Desired identity is thread ID + presentation revision. Any change closes the
  old socket intentionally before opening the new one.

- [x] **Step 3: Build the connection lifecycle.** Set `binaryType=arraybuffer`.
  A valid first STATE establishes the private expected generation and moves to
  ready. A FRAME before STATE or from a different generation is ignored. A
  later valid STATE with a different generation is an ended-generation
  transition: clear pixels and close instead of silently re-pinning. Malformed
  server data closes as a terminal protocol error.

- [x] **Step 4: Bound decoding.** For a valid current FRAME:

  1. if a decode is already active, drop the new frame;
  2. call `createImageBitmap(new Blob([jpeg], {type:'image/jpeg'}))`;
  3. discard and close the result if the connection epoch changed;
  4. atomically replace the frame signal and close the prior bitmap.

  Close the current bitmap on source replacement, terminal close, inactivity,
  and controller destruction. Promise rejection becomes a recoverable frame
  error without an unhandled rejection.

- [x] **Step 5: Add reconnect policy.** Use 250, 500, 1000, 2000, then 5000 ms
  capped backoff while the desired source remains active. A successful STATE
  resets the attempt. Intentional hide/source teardown never retries.

- [x] **Step 6: Map terminal/transient signals.** Implement the locked close
  table. `ERROR navigation_rejected` is nonterminal and leaves the stream
  attached. `ERROR browser_gone` and `4409` clear the bitmap and become
  `ended`. `viewer_limit` becomes an explicit manual-retry state. Auth/owner/
  disabled states never auto-retry. `4502` and unclean network loss do.

- [x] **Step 7: Detach on visibility.** Register one
  `visibilitychange` listener. Hidden document closes the socket and bitmap;
  visible document re-evaluates the still-current desired source. The Canvas
  pane's existing `active` input separately handles settings/mobile/closed
  hiding and therefore triggers the daemon's 30-second zero-viewer release.

- [x] **Step 8: Wire the pane provider/effect.** Add
  `CanvasBrowserController` to `CanvasPaneComponent.providers` and:

      effect(() => this.browser.syncPresentation(
        this.active() && this.effectiveRenderer() === 'browser',
        this.canvas.threadId(),
        this.state(),
      ));

- [x] **Step 9: Unit-test lifecycle and leaks.** Cover activation, capability
  fail-close, source replacement, hide/show, document visibility, binary URL,
  invalid STATE, mismatched later STATE, mismatched FRAME, decode skip, stale
  decode, bitmap close, backoff/reset, every close/error mapping, retry, and
  destroy. Fake timers must end with zero pending timers and every fake bitmap
  closed.

- [x] **Step 10: Run:**

      (
        cd cockpit
        npx vitest run src/app/views/canvas/canvas-browser.controller.spec.ts \
          src/app/views/canvas/canvas-pane.component.spec.ts
      )

  Expected: all pass with no unhandled promise output.

- [x] **Step 11: Commit:**

      git add cockpit/src/app/views/canvas/canvas-browser.controller.ts \
        cockpit/src/app/views/canvas/canvas-browser.controller.spec.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.spec.ts
      git commit -m "feat(cockpit): connect shared-browser Canvas streams"

---

### Task 10: Render the browser view and read-only page chrome

**Files:**

- Create: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts`
- Create: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss`
- Create: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.scss`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.spec.ts`
- Modify: `cockpit/src/assets/i18n/en.json`
- Modify: `cockpit/src/assets/i18n/de-DE.json`

- [x] **Step 1: Create the standalone renderer.** Inject the pane-local
  controller. Render trusted host chrome plus one focusable HTML `canvas`.
  The first slice of toolbar is read-only: page title, current URL, loading
  indicator, connection status, and baton label. No iframe, `innerHTML`,
  object URL, or remote resource URL is involved.

- [x] **Step 2: Paint decoded bitmaps.** On each controller frame, set the
  backing store to `bitmap.width`/`bitmap.height` and draw at `0,0`. Never use
  protocol metadata to set width/height. Clear the surface when no current
  frame exists. Unit-test a metadata/bitmap mismatch.

- [x] **Step 3: Make display geometry exact.** The element itself has the
  stream viewport aspect ratio and is contain-scaled inside the pane. Put any
  letterbox/background on a wrapper, not inside a larger event canvas, so
  `getBoundingClientRect()` describes exactly the interactive picture. Give it
  a visible focus ring and `touch-action:none`.

- [x] **Step 4: Wire renderer selection into the pane.** Treat `browser` like
  `app` in `effectiveRenderer` instead of passing it through
  `CanvasContentController.displayRenderer`. A Browser presentation counts as
  visual while connecting even before its first frame. Add the renderer to the
  template/imports and add English/German `browser` source-kind/renderer labels
  in this task.

- [x] **Step 5: Preserve all existing Canvas modes.** Content/editor/viewer
  controllers must remain idle for Browser; file/app rendering, unsaved edit
  preservation, reset-origin, popout eligibility, refresh, and overlays remain
  unchanged. Add regression cases for each source kind.

- [x] **Step 6: Add accessible non-frame states.** Connecting/reconnecting,
  unavailable, viewer-limit, protocol-error, and ended copy is rendered in
  trusted chrome and announced through the pane's existing polite live region.
  Add the corresponding English and German keys in this task. Buttons arrive
  in Tasks 11/13; no state is represented by color alone.

- [x] **Step 7: Run:**

      (
        cd cockpit
        npm run i18n:check
        npx vitest run \
          src/app/views/canvas/canvas-browser-renderer.component.spec.ts \
          src/app/views/canvas/canvas-pane.component.spec.ts \
          src/app/views/canvas/canvas-rendering.spec.ts
      )

  Expected: all pass.

- [x] **Step 8: Commit:**

      git add cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.scss \
        cockpit/src/app/views/canvas/canvas-pane.component.spec.ts \
        cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
      git commit -m "feat(cockpit): render the shared browser in Canvas"

---

### Task 11: Add capability discovery and the cold “Open browser” workflow

**Files:**

- Modify: `cockpit/src/app/core/models/canvas.model.ts`
- Modify: `cockpit/src/app/core/services/canvas.service.ts`
- Modify: `cockpit/src/app/core/services/canvas.service.spec.ts`
- Modify: `cockpit/src/app/views/chat/chat-page.component.ts`
- Modify: `cockpit/src/app/views/chat/chat-page.component.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.scss`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.spec.ts`
- Modify: `cockpit/src/assets/i18n/en.json`
- Modify: `cockpit/src/assets/i18n/de-DE.json`

**Produces on `CanvasService`:**

    readonly browserCapability: Signal<BrowserCapability | null>
    readonly browserCapabilityStatus:
      Signal<'idle' | 'loading' | 'ready' | 'error'>
    readonly browserOpenStatus:
      Signal<'idle' | 'workspace' | 'browser' | 'error'>
    readonly browserOpenError: Signal<string | null>

    openBrowser(title?: string): void
    retryOpenBrowser(): void

- [x] **Step 1: Reconcile capability with thread selection.** Start a separate
  cancellable `GET .../browser/capability` whenever `selectThread` selects a
  real thread. A thread change cancels both capability and open/retry work and
  synchronously clears their signals. Validate the four-field response before
  applying it. A `404` from an older/disabled server is a dark capability, not
  an app-wide error; auth failures clear potentially stale state.

- [x] **Step 2: Implement the bounded open state machine.**

  1. Require the currently selected thread and positive `can_open_browser`.
  2. Set phase from the capability:
     `workspace_ready ? 'browser' : 'workspace'`.
  3. POST `{"title": title}` to `.../browser/open`.
  4. On `202`, honor a bounded `Retry-After` (default one second), refresh
     capability, and retry the idempotent POST while the same thread remains
     selected.
  5. Stop after five minutes with a typed retryable error.
  6. On `200`, validate `CanvasPublicState`, `ETag`, and the strict mutation
     header through the same mutation parser, apply it locally, broadcast the
     revision only when changed, and return to idle.

  All timers/subscriptions are cancelled on thread change or destroy. Multiple
  button clicks coalesce into the active attempt.

- [x] **Step 3: Notify both runtimes.** Extend
  `applyMutationResponse(..., notifyRuntime=true)` so a Browser source sends
  the same bounded `canvas.presentation_updated` control event as an app.
  This is an invalidation only; no browser identity is included. A
  `changed=false` response still reconciles local state/ETag but emits neither
  the runtime event nor a duplicate BroadcastChannel invalidation.

- [x] **Step 4: Make empty Canvas hostable.** Change
  `ChatPage.canvasAvailable` to include
  `browserCapability.feature_enabled===true`. Fix the source-tracking effect:

  - a null/absent source on a feature-enabled thread does not force-close a
    Canvas the user explicitly opened;
  - initial capability discovery does not auto-open it;
  - an authoritative `status='cleared'` still closes it;
  - changing thread still resets it.

  This is required for the empty-state button; test the exact effect so a later
  refactor cannot reintroduce the open-then-instant-close loop.

- [x] **Step 5: Add the always-present chat toolbar action.** When
  `feature_enabled` is true, render a browser icon in the persistent-chat
  header action area even if no Canvas source exists. Clicking it opens/focuses
  Canvas immediately to show progress, then starts `openBrowser`. On a lite or
  otherwise unsupported backend, keep it visible but disabled with the
  server-reason tooltip.

- [x] **Step 6: Add the empty-state action.** When no source is staged, Canvas
  shows a primary **Open browser** button plus the capability explanation.
  During cold start it shows:

      Starting workspace…
      Starting browser…
      Connecting…

  in order, using service and socket states. Failure shows Retry. The existing
  refresh action remains a state reconcile, not an alias for browser open.
  Add English/German parity keys for the action, disabled reasons, phases,
  errors, Retry, and the dirty-edit explanation in this task.

- [x] **Step 7: Protect unsaved edits.** Both chat-toolbar and pane actions are
  disabled while `canvasDirty()`/the pane editor is dirty, with copy explaining
  that opening the browser would replace the current stage. There is no silent
  discard and no new confirmation modal in v1.

- [x] **Step 8: Cover replacement semantics.** From a clean file/app Canvas,
  Open browser replaces the single main source and the normal revision
  invalidation switches the pane. An exact repeat does not create a new
  source key. A stale open response from a previous thread is ignored.

- [x] **Step 9: Run:**

      (
        cd cockpit
        npm run i18n:check
        npx vitest run src/app/core/services/canvas.service.spec.ts \
          src/app/views/chat/chat-page.component.spec.ts \
          src/app/views/canvas/canvas-pane.component.spec.ts
      )

  Expected: all pass, including fake-timer cold provisioning and the empty-pane
  persistence regression.

- [x] **Step 10: Commit:**

      git add cockpit/src/app/core/models/canvas.model.ts \
        cockpit/src/app/core/services/canvas.service.ts \
        cockpit/src/app/core/services/canvas.service.spec.ts \
        cockpit/src/app/views/chat/chat-page.component.ts \
        cockpit/src/app/views/chat/chat-page.component.spec.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.scss \
        cockpit/src/app/views/canvas/canvas-pane.component.spec.ts \
        cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
      git commit -m "feat(cockpit): open the shared browser from Canvas"

---

### Task 12: Add coordinate-safe input, navigation, and the control baton

**Files:**

- Create: `cockpit/src/app/views/canvas/canvas-browser-input.ts`
- Create: `cockpit/src/app/views/canvas/canvas-browser-input.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser.controller.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser.controller.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss`
- Modify: `cockpit/src/assets/i18n/en.json`
- Modify: `cockpit/src/assets/i18n/de-DE.json`

**Produces:**

    mapBrowserPoint(
      clientX: number,
      clientY: number,
      rect: DOMRectReadOnly,
      viewport: {width: number; height: number},
    ): {x: number; y: number} | null

    browserModifiers(event): number  // Alt=1 Ctrl=2 Meta=4 Shift=8

- [x] **Step 1: Lock coordinate behavior with pure tests.** Map CSS display
  coordinates into CDP CSS viewport pixels:

      x = (clientX - rect.left) * viewport.width / rect.width
      y = (clientY - rect.top) * viewport.height / rect.height

  Reject zero/non-finite geometry and points outside the actual canvas. Clamp
  the accepted right/bottom edge below viewport bounds. Cover 1:1, downscale,
  upscale, fractional offsets, portrait, and the metadata-vs-bitmap mismatch.

- [x] **Step 2: Add pointer/mouse events only while user drives.**

  - pointer move -> CDP `mouseMoved`, at most once per animation frame;
  - primary/middle/secondary down/up -> `mousePressed`/`mouseReleased` with
    button, buttons, modifiers, and click count;
  - capture a supported pointer on down and release it on up/cancel so a drag
    leaving the element cannot strand a pressed button;
  - wheel -> `mouseWheel` with mapped position/modifiers and deltas normalized
    to pixels, then scaled by the viewport/display ratio (line mode uses a
    fixed 16 CSS px and page mode uses the displayed canvas extent);
  - prevent context-menu and browser scrolling only while the surface is ready,
    focused, and baton is `user`;
  - ignore unsupported pointer buttons and all events while the agent drives.

  The daemon remains the final authority and independently drops stale input.

- [x] **Step 3: Add keyboard translation.** On a focused surface, send
  `keyDown`/`keyUp` with key, code, location, repeat, modifier bitmask, and
  printable `text` only when no Ctrl/Meta/Alt command chord is active. Track
  pressed keys. On blur, visibility loss, baton release, disconnect, or source
  replacement, send best-effort keyUp for held modifiers/keys and clear local
  state. Ignore IME composition in v1 instead of emitting corrupt text.

- [x] **Step 4: Add the editable browser toolbar.**

  - URL form initialized from current STATE without overwriting text while the
    user edits;
  - submit -> `CONTROL navigate`;
  - Back and Reload -> their control operations;
  - server `navigation_rejected` -> bounded inline error, with stream intact;
  - controls disabled unless connected and baton is `user`.

  Use Angular interpolation and ordinary form controls; never treat URL/title/
  server error text as HTML.

- [x] **Step 5: Add the baton pill.**

  - Agent baton: **Agent is driving** + **Take control**.
  - User baton: **You're driving** + **Release control**.
  - Take/release sends the corresponding CONTROL once and waits for the
    authoritative STATE before changing the label.
  - Disable during reconnect/ended/unavailable states.

  Do not optimistically grant input locally. The daemon's STATE is the only
  transition authority. Add English/German parity keys for the editable
  toolbar, navigation error, baton holder, Take control, and Release control
  in this task.

- [x] **Step 6: Test output frames exactly.** Decode captured fake-socket bytes
  and assert type byte + JSON body for mouse, wheel, key, navigate, back,
  reload, take, and release. Assert no sends for agent baton, stale socket,
  hidden pane, outside pointer, or composition.

- [x] **Step 7: Test same-user multi-view behavior.** Two controller fakes may
  both receive the user baton; either may send input because v1 has one
  authenticated owner, not per-tab ownership. Their labels update only from
  broadcast STATE. This documents the intentional v1 boundary.

- [x] **Step 8: Run:**

      (
        cd cockpit
        npm run i18n:check
        npx vitest run src/app/views/canvas/canvas-browser-input.spec.ts \
          src/app/views/canvas/canvas-browser.controller.spec.ts \
          src/app/views/canvas/canvas-browser-renderer.component.spec.ts
      )

  Expected: all pass.

- [x] **Step 9: Commit:**

      git add cockpit/src/app/views/canvas/canvas-browser-input.ts \
        cockpit/src/app/views/canvas/canvas-browser-input.spec.ts \
        cockpit/src/app/views/canvas/canvas-browser.controller.ts \
        cockpit/src/app/views/canvas/canvas-browser.controller.spec.ts \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss \
        cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
      git commit -m "feat(cockpit): drive the shared browser with a baton"

---

### Task 13: Finish lifecycle UX, i18n, popout fan-out, and browser E2E

**Files:**

- Modify: `cockpit/src/assets/i18n/en.json`
- Modify: `cockpit/src/assets/i18n/de-DE.json`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss`
- Modify: `cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-pane.component.spec.ts`
- Modify: `cockpit/src/app/views/canvas/canvas-popout-page.component.spec.ts`
- Modify: `cockpit/e2e/canvas/fixture-server.mjs`
- Modify: `cockpit/e2e/canvas/playwright.config.ts`
- Create: `cockpit/e2e/canvas/shared-browser-conformance.spec.ts`

- [x] **Step 1: Complete trusted lifecycle actions.**

  - transient disconnect: Reconnecting overlay while backoff runs;
  - `viewer_limit`: explanatory state + manual Retry;
  - `ended`/`4409`/`browser_gone`: clear stale pixels and show Restart, which
    calls the ordinary open workflow and waits for a new Canvas revision;
  - `unavailable`: Retry connection plus Restart when capability permits;
  - unauthorized/disabled/protocol error: terminal copy with no retry loop.

  A reconnect within the same presentation revision is silent once STATE
  arrives. Restart never follows a moving `current` generation without a
  deliberate open/Canvas transition.

- [x] **Step 2: Verify popout fan-out.** The existing authenticated wrapper
  owns its own pane-local controller and attaches a second socket. Closing
  either window detaches only that viewer. Both paint broadcast frames and
  baton STATE; neither stores stream data in `BroadcastChannel`. The root
  Canvas service continues to use the channel only for revision invalidations.

- [x] **Step 3: Complete and audit English/German copy.** Retain the keys
  landed with Tasks 10–12 and add/audit parity for:

  - browser source kind and renderer;
  - open action, disabled reasons, dirty-edit guard;
  - workspace/browser/connecting progress;
  - URL/title/loading/back/reload;
  - agent/user baton and take/release;
  - reconnect, ended, unavailable, viewer-limit, protocol/auth errors;
  - retry/restart and navigation rejection;
  - canvas surface/toolbar accessible names.

  Keep “Untrusted” host chrome: the page is workspace-controlled content even
  though transport is authenticated.

- [x] **Step 4: Add keyboard/focus accessibility tests.** Toolbar controls have
  labels and logical tab order; the canvas has an explicit name and visible
  focus; status uses `aria-live=polite`; terminal failures use an appropriate
  alert; Take/Release exposes pressed/status semantics without color alone.
  Reduced motion removes reconnect animation.

- [x] **Step 5: Extend the production fixture HTTP API.** Add a browser
  scenario that returns pre-source capability, `204` before open, and a
  Browser Canvas state + ETag + mutation header after open. Record
  CSRF/header/cookie metadata with the existing safe recorder; never record
  cookie values.

- [x] **Step 6: Mock the binary socket with Playwright
  `page.routeWebSocket`.** Send:

  - strict STATE for a canonical private generation;
  - a small known JPEG FRAME whose decoded pixels can be sampled;
  - loading/title/URL/baton state transitions;
  - navigation rejection;
  - `4409` close and a replacement revision/socket.

  Capture client Buffer messages and decode them in the fixture test. No
  production-only test hook or debug endpoint is allowed.

- [x] **Step 7: Cover the complete three-engine UI flow.** In Chromium,
  Firefox, and WebKit:

  1. an empty Canvas exposes the Open button;
  2. open POST carries CSRF/BFF cookie but no auth token in URL;
  3. STATE + JPEG paints non-empty known pixels;
  4. scaled pointer coordinates and keyboard/control messages are exact;
  5. Take/Release waits for authoritative STATE;
  6. hiding Canvas closes the socket and revealing reconnects;
  7. popout creates a second viewer and both receive frames;
  8. `4409` shows ended, Restart creates a new revision, and no private
     generation appears in DOM/URL/local storage/console;
  9. malformed/oversized mocked frames never paint or wedge the page.

- [x] **Step 8: Update Playwright config.** Set `testMatch` to the explicit
  array `['canvas-conformance.spec.ts',
  'shared-browser-conformance.spec.ts']`. Preserve one worker and the existing
  three browser projects.

- [x] **Step 9: Run:**

      (
        cd cockpit
        npm run i18n:check
        npm test
        npm run build
        npm run test:e2e:canvas:no-build
      )

  Expected: Vitest/build pass and both Canvas E2E files pass in Chromium,
  Firefox, and WebKit.

- [x] **Step 10: Commit:**

      git add cockpit/src/assets/i18n/en.json \
        cockpit/src/assets/i18n/de-DE.json \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.ts \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.scss \
        cockpit/src/app/views/canvas/canvas-browser-renderer.component.spec.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.ts \
        cockpit/src/app/views/canvas/canvas-pane.component.spec.ts \
        cockpit/src/app/views/canvas/canvas-popout-page.component.spec.ts \
        cockpit/e2e/canvas/fixture-server.mjs \
        cockpit/e2e/canvas/playwright.config.ts \
        cockpit/e2e/canvas/shared-browser-conformance.spec.ts
      git commit -m "test(cockpit): cover shared-browser handoff end to end"

---

### Task 14: Full verification, live k3d acceptance, docs, and dev enablement

**Files:**

- Modify after the live pass: `deployment/values-tilt.yaml`
- Modify: `tests/test_shared_browser_infra.py`
- Create: `docs/tests/shared_browser_verification.md`
- Modify: `docs/features/shared_browser.md`
- Modify: `docs/features/dynamic_canvas.md`
- Modify: this plan (check completed tasks and add execution record)

- [x] **Step 1: Run the complete focused Python gate:**

      python -m pytest \
        tests/test_canvas_ssh_transport.py \
        tests/test_shared_browser_config.py \
        tests/test_shared_browser_broker.py \
        tests/test_shared_browser_open.py \
        tests/test_shared_browser_capability.py \
        tests/test_shared_browser_internal.py \
        tests/test_shared_browser_infra.py \
        tests/tools/research/test_browser_exec_stream.py \
        tests/tools/research/test_browser_tools.py \
        tests/test_canvas_tool.py \
        tests/test_orchestrator_client_canvas.py \
        tests/test_canvas_slice3_callable.py \
        tests/test_persistent_session.py \
        tests/test_persistent_app.py \
        tests/test_canvas_slice0.py \
        tests/test_canvas_slice3_infra.py -q

  Expected: all pass.

- [x] **Step 2: Run static and Cockpit gates:**

      ruff check src/ orchestrator/ tests/
      ruff format --check src/ orchestrator/ tests/
      (
        cd cockpit
        npm run i18n:check
        npm test
        npm run build
        npm run test:e2e:canvas:no-build
      )

  Expected: all pass. If full Ruff format still reports a pre-existing
  unrelated file, record it precisely and prove every touched Python file
  passes; do not reformat unrelated owner work.

- [x] **Step 3: Run chart/config gates from the repository root:**

      helm lint helm/ -f helm/ci/test-values.yaml
      helm lint helm/ -f helm/ci/customer-external-values.yaml
      helm template srw helm/ -f helm/ci/test-values.yaml \
        > /tmp/shared-browser-default.yaml
      rg -n "CANVAS_SHARED_BROWSER_ENABLED" \
        /tmp/shared-browser-default.yaml
      helm template srw helm/ -f helm/ci/test-values.yaml \
        -f deployment/values-experimental.yaml \
        > /tmp/shared-browser-experimental.yaml
      rg -n "CANVAS_SHARED_BROWSER_ENABLED" \
        /tmp/shared-browser-experimental.yaml

  Expected: both lints pass and the default render carries `"false"`.
  The experimental render carries `"true"`.

- [x] **Step 4: Re-run the real container-image gate:**

      podman build -f docker/Dockerfile.workspace \
        -t srw-workspace-shared-browser-plan2-final .
      podman run --rm --user agent-host \
        --entrypoint /usr/local/bin/assert-browser-stack \
        srw-workspace-shared-browser-plan2-final

  Expected: the real Chromium stream check passes, including loading, active
  target, reconnect, and viewer cap.

- [ ] **Step 5: Prove the VM artifact gate.** On a host/CI runner with the
  stage-1 qcow2 and KVM:

      (
        cd docker/agent-vm-base
        packer init stage2.pkr.hcl
        packer build stage2.pkr.hcl
      )

  Expected: stage 2 runs `assert-browser-stack` as `agent-host` and prints the
  same stream-conformance success before producing
  `output/agent-vm-base.qcow2`. This proves image parity only. The runtime
  capability tests must still return false for unattested/unroutable VM
  contexts.

  **2026-07-22 result:** Not run. `/dev/kvm` exists, but this host has neither
  Packer nor the required stage-1 qcow2. Static image-parity and fail-closed VM
  capability tests pass; no VM artifact claim is made.

- [x] **Step 6: Enable only the local acceptance deployment.** Before changing
  committed values, preserve any existing ignored
  `deployment/values-local.yaml`, add only
  `canvas.sharedBrowser.enabled=true` to that local overlay, and run:

      ./scripts/local-dev-tilt-up.sh

  Wait for orchestrator, Cockpit, agent, and workspace resources to be healthy.
  Confirm the live ConfigMap and both orchestrator/agent environments contain
  the boolean gate without printing any Secret. Record that this is a
  temporary acceptance override and restore only the temporary key after the
  committed Tilt overlay is proven.

- [ ] **Step 7: Execute the same-browser live flow.** In a new sandbox
  persistent session:

  1. Click the chat-header browser button before a Canvas source exists and
     observe bounded cold-start progress.
  2. Navigate as the user to a deterministic page.
  3. Ask the agent for `browser_snapshot` and verify it reports that exact URL
     and page.
  4. Release control; ask the agent to navigate/click; watch the same surface
     update.
  5. Take control; verify a mutating agent action returns the explicit
     user-driving refusal while a snapshot still works.
  6. Enter/change a form value as the user, release, and verify the next agent
     snapshot sees the same DOM/session/cookies.
  7. Open Canvas popout and verify both viewers update; close one and keep the
     other live.
  8. Hide all viewers for more than 30 seconds and verify the agent regains the
     baton.
  9. Hold one viewer through at least one activity interval and verify the
     thread's `last_activity` advances and the idle sweep does not suspend it.
  10. Close the browser through the agent, observe Ended, click Restart, and
      verify exactly one new Canvas revision/generation is selected.

  Use a workspace-local deterministic HTML form or a stable non-sensitive test
  page. Do not enter real credentials or record page/frame content in logs.

  **2026-07-22 result:** Partial. Cold open, exact executor snapshot/navigation,
  baton refusal/read behavior, popout, zero-viewer release, activity, and
  ended/restart passed. Prompt-driven calls and the form/cookie handoff were
  not claimed because the default model had no serving backend and the only
  separately probed external credential was a rejected local placeholder.

- [ ] **Step 8: Exercise failure boundaries live.** Verify lite sessions show
  the disabled explanatory button, feature-off hides empty affordances, bad
  navigation is rejected without disconnect, a fourth viewer gets the limit
  state, an orchestrator rollout reconnects, and an unattested VM exposes no
  runnable browser action.

  **2026-07-22 result:** Partial. Lite UX, bad navigation, the three-viewer cap,
  fourth-viewer terminal UX, static VM denial, and a real clean-1012 Uvicorn
  reconnect passed. A replacement orchestrator pod could not reach an existing
  workspace because the local k3d CNI returned `No route to host`. Feature-off
  reconciled live to `false`; its final browser assertion is covered by the
  production-browser suite but could not be repeated after that CNI failure.

- [x] **Step 9: Record evidence.** Create
  `docs/tests/shared_browser_verification.md` with date/context, image hashes
  or commits, summarized test counts, each live-flow result, activity timing,
  and the explicit VM image-vs-runtime boundary. Redact thread IDs, user data,
  cookies, tokens, endpoints, fingerprints, screenshots, and secret values.

- [ ] **Step 10: Enable Tilt only after the live pass.** Add:

      canvas:
        sharedBrowser:
          enabled: true

  to `deployment/values-tilt.yaml` and extend the infra test to assert:

  - chart default false;
  - experimental true;
  - Tilt true;
  - customer/external overlays do not accidentally enable it.

  `deployment/values-experimental.yaml` is already true and should not receive
  a no-op edit. Remove the temporary local override, let Tilt reconcile once
  more, and confirm the live ConfigMap remains enabled from the committed
  overlay before recording the gate.

  **2026-07-22 result:** Deliberately not performed. The temporary override was
  removed and the ConfigMap reconciled to `false`; Tilt stays off until Steps
  5, 7, and 8 complete on suitable infrastructure.

- [x] **Step 11: Close documentation honestly.** Update both feature docs from
  “backend foundation” to “container user flow implemented and verified,” link
  this plan and the verification record, list the exact automation counts, and
  retain the VM runtime caveat. Mark this plan's boxes only for completed work
  and add commit/check evidence at the top. Do not rewrite Plan 1 history.

- [x] **Step 12: Final diff hygiene:**

      git diff --check
      git status --short
      git diff --stat

  Expected: no whitespace errors; only Plan-2 paths plus the owner's existing
  unrelated entries are present.

- [x] **Step 13: Commit the gate and record:**

      git add deployment/values-tilt.yaml tests/test_shared_browser_infra.py \
        docs/tests/shared_browser_verification.md \
        docs/features/shared_browser.md docs/features/dynamic_canvas.md \
        docs/superpowers/plans/2026-07-22-shared-browser-cockpit-handoff.md
      git commit -m "docs(browser): record shared-browser user-flow verification"

  Do not push.

## Definition of done

Plan 2 is complete only when all of these are true:

- An owner sees a discoverable UI button before any Canvas source exists.
- That button provisions/reuses the session's attested container workspace,
  stages Browser Canvas state once, and attaches without exposing private
  identity.
- Cockpit paints bounded JPEG frames from the same Chromium the agent tools
  inspect.
- User input maps to CDP CSS viewport coordinates at arbitrary display scale.
- The daemon, not either UI/agent caller, serializes and authorizes the baton.
- Agent tools clearly report both control state and `user_is_driving`.
- Popout/main viewers fan out, hidden viewers detach, and zero-viewer release
  works.
- Active target changes and main-frame loading transitions work against real
  browser-use 0.12.9.
- Startup command and stream channel both use the same provisioner-attested,
  generation-bound, host-key-pinned SSH transport.
- Cross-site WS handshakes, oversized commands, stale owners/generations,
  fourth viewers, lite backends, and unattested/unroutable VMs fail closed.
- Unit/service, three-engine production-browser, container-image, VM-image,
  Helm, and live k3d gates are recorded.
- The chart default remains off; only explicitly experimental/local development
  profiles are on.

## Explicitly out of scope

- Multi-user baton ownership or per-tab user leases.
- Agent-initiated “please take control” requests or push baton events.
- Tab strip/tab selection UI. v1 follows browser-use's active agent target.
- Live viewport resize, device emulation, HiDPI passthrough, audio, clipboard,
  file chooser, native dialogs, downloads UI, WebAuthn, and headed/Xvfb mode.
- Browser recording/replay or frame persistence.
- Job-detail browser surface; this plan targets persistent-session Canvas.
- A new VM trust bootstrap or orchestrator tailnet deployment. VM image parity
  lands here; runtime stays gated by independent attestation and routing.
- Enabling the feature in chart defaults or customer/production overlays.

## Plan self-review

The review that produced this plan closed these gaps before execution:

1. **Empty-state capability:** `can_stream_browser` exists only after a Browser
   source is staged, so it cannot drive an empty UI. The dedicated
   `can_open_browser` contract fixes that without changing `204` Canvas
   semantics.
2. **Pinned startup:** Plan 1 pinned the long-lived relay but still launched
   `stream_info` through a raw `ssh` command with
   `StrictHostKeyChecking=no`. Task 1 moves the cold-start command onto the
   shared pinned pool before a UI can expose it.
3. **Cross-site WebSocket hijacking:** cookie auth alone is insufficient for a
   browser WebSocket because CSRF middleware does not process WS scopes.
   Task 3 requires an exact allowed Origin.
4. **Post-await authorization:** owner, gate, workspace, and Canvas generation
   can change while Chromium starts. Tasks 1–3 revalidate immediately before
   commit/accept.
5. **Idempotent open:** the current `CanvasService.set` always increments.
   Browser open gets a transaction-scoped presentation lock plus compare/no-op
   mutation so retries and concurrent first opens do not manufacture new
   logical sources, including when no row exists yet.
6. **Loading and target correctness:** the daemon currently never sets loading
   and keeps streaming the target attached at viewer start. Task 4 uses the
   real 0.12.9 focus event and main-frame events.
7. **Callback accumulation:** discarding the adapter at zero viewers registers
   duplicate CDP callbacks after every reconnect. The session-lifetime adapter
   prevents it.
8. **HA viewer cap:** the broker's viewer counter is replica-local. The daemon
   sees every relay and becomes the final global cap.
9. **Empty Canvas host loop:** ChatPage currently closes whenever no source
   exists. Task 11 preserves an explicitly opened empty browser host and tests
   it.
10. **Unsaved edits:** replacing the one main Canvas from a toolbar button
    could discard a user's dirty edit. Task 11 disables that action.
11. **Private generation pressure:** the controller needs frame pinning but the
    public Canvas source intentionally redacts generation. The stream parser
    keeps it private in-memory and the public logical key uses presentation
    revision.
12. **Bitmap/resource leaks:** every async decode is epoch-guarded and every
    stale/replaced bitmap is closed.
13. **Agent capability drift:** browser schema advertisement comes from the
    orchestrator-attested attach bit, not merely a process env or “remote”
    backend label.
14. **VM overclaim:** installing a working stream daemon in the golden image
   does not create trusted host-key enrollment or a route. The release matrix
   proves image parity while runtime remains visibly fail-closed.
15. **Command working directories:** every Packer/Cockpit command that changes
    directory is isolated in a subshell, and capability tests name the real
    repository files, so later commit/gate commands remain runnable from root.
16. **Dirty-tree commits:** per-task path lists are not permission to include
    pre-existing changes on the same file. Execution rechecks and isolates
    staged hunks before every commit.
