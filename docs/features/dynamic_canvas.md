---
tags:
  - feature
  - cockpit
  - agent-tool
  - collaboration
  - artifacts
aliases:
  - dynamic component
  - shared artifact
  - artifact panel
  - canvas
  - shared stage
related:
  - "[[shared_browser]]"
  - "[[canvas_durable_presentation]]"
  - "[[canvas_office_documents]]"
  - "[[agent_skills]]"
  - "[[dynamic_canvas_slice1_verification]]"
  - "[[dynamic_canvas_slice2_verification]]"
  - "[[dynamic_canvas_slice3a_verification]]"
  - "[[dynamic_canvas_slice3b_verification]]"
  - "[[dynamic_canvas_slice3c_verification]]"
  - "[[dynamic_canvas_hosted_edge]]"
  - "[[shared_application_action_layer]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[persistent_chat_ui_redesign]]"
  - "[[vm_snapshots_and_ide]]"
  - "[[workspace_network_policy_unification]]"
---

# Feature: Dynamic Canvas (Shared Artifact Stage)

> Give the agent a stage beside the conversation where it can present what it
> made or what it is working with. The user should not have to find a workspace
> file, open the IDE, forward a port, copy a proxy URL, or reconstruct an image
> from chat output just to see and collaborate on the result.

**Status:** Slices 0–2 are implemented as of 2026-07-13. The primary
file-presentation path and the Slice-2 conditional-editing API are live-verified
on local k3d, including a two-replica save race and final browser-visible CORS
validator exposure. The orchestrator and Cockpit rollouts were ready after the
final verification. The Slice-1 bodyless-`304` framing defect found during the
first acceptance pass is fixed and reverified.
The default-off Slice-3A callable/SSH foundation and Slice-3B isolated
ordinary-HTTP viewer checkpoint are also implemented and repository-verified.
Slice 3B includes the viewer-session control plane, dedicated gateway, strict
one-port proxy, browser-bound BFF challenge/exchange (with no credential in the
iframe URL), Cockpit iframe lifecycle, dark deployment plumbing, and the
trusted-parent anti-framing response boundary. Cockpit production and dev
documents, orchestrator/BFF responses, and optional MCP OAuth documents now
emit enforced `frame-ancestors 'none'` plus legacy `DENY` protection without
overwriting route-specific CSP. The separately hosted IDE/API compatibility
path remains restricted to same-origin framing rather than bypassing the
boundary, and unhandled ASGI `500` responses are protected before Starlette's
outer error renderer can answer. The Cockpit app-shell hash was bumped so the
next service-worker update detects and refetches the shell; already-open tabs
still require worker activation/reload. The Slice-3C gateway database boundary
is also implemented: the gateway constructs only a dedicated
`CANVAS_VIEWER_POSTGRES_*` pool, requires the authenticated PostgreSQL login to
remain the active role, self-attests its exact effective privileges before
serving, receives an allowlisted gateway-only ConfigMap and credential Secret,
and has no shared application DB fallback. Bundled-Postgres
development can reconcile the fixed restricted role through a bounded one-shot
Job; production requires an operator-provisioned role and separate Secret. The
packaged grants and denial contract passed a fresh fully migrated PostgreSQL 15
integration run. A secret-safe operator preflight/reconciler and production
runbook now cover the out-of-band role plus either ESO-owned or explicit native
Secret provisioning; both apply modes passed against disposable k3d objects
and left the normal local cluster dark.
The hosted edge was re-decided on 2026-07-14
(`docs/issues/canvas_hosted_edge_use_cloudflare_tunnel.md`): the previously
staged DNS-only wildcard → TCP HAProxy → WireGuard → dedicated NGINX relay is
scrapped, and the wildcard rides the deployment's existing Cloudflare Tunnel
to the internal gateway instead. Raw-path preservation is demoted from a
universal launch gate to an optional property of an operator-chosen
L4/SNI-passthrough edge; Cloudflare's baseline adjacent-slash normalization is
an accepted, documented limitation of the tunnel mode, safe because it
canonicalizes *before* the single gateway boundary. The chart additionally
gained an optional, default-off wildcard Ingress for the gateway
(`viewer.ingress`). All viewer/attestation gates remain off. This still does
not count as a production or user-facing Slice-3 release. The current one-port
proxy is cookie-free and rejects SSE, multipart streaming, and WebSocket
upgrades. The trusted Slice-3 Cockpit closeout is now implemented: typed
unsupported-browser/storage fallback, an authenticated
wrapper pop-out, conditional reset-origin UI/API, true client last-sync and
source-kind chrome, and normalized Canvas tool cards. A local production-build
Playwright harness exercises that UI and an intercepted synthetic viewer, with
27 cases across Chromium, Firefox, and WebKit projects. Chromium and Firefox
pass on the host; WebKit passes in Playwright's exact Ubuntu Noble container
after the Fedora-native launch hit an ABI mismatch. This is not
evidence for live TLS, the deployed Python gateway, real CHIPS/PSL behavior,
Safari/iOS/PWA, the hosted edge, or an
enabled k3d live-app flow. Those external browser/edge gates remain Slice-3
launch work; multi-port/streaming apps and the user-facing portions of Slices
4 and 6 remain planned. The default-off Slice-5 shared-browser container user
flow is implemented and repository-verified: agent presentation/handoff,
Cockpit open/view/drive/restart, real-Chromium image conformance, and
three-engine production-browser coverage are complete. VM artifact attestation
and the final live release gate remain outstanding, so the committed Tilt
profile stays off.
A cookie-free hosted black-box verifier now checks Cockpit/deep routes,
API/IDE, Keycloak, the deployed PWA shell hash, the authoritative PRIVATE PSL
rule, and opt-in raw-path/rate boundaries without printing URLs, response
bodies, cookies, or headers. Against the still-old hosted release it correctly
fails Cockpit/API/IDE/PWA, raw-path routing, and PSL while the final Keycloak
login document passes. No attestation was changed from that evidence.
Original brainstorm filed 2026-05-13. Pointer model agreed and repository,
security, accessibility, and comparable-product audits completed 2026-07-13.
See the
[[dynamic_canvas_slice1_verification|Slice-1 verification record]] and
[[dynamic_canvas_slice2_verification|Slice-2 verification record]], plus the
[[dynamic_canvas_slice3a_verification|Slice-3A foundation record]] and
[[dynamic_canvas_slice3b_verification|Slice-3B ordinary-HTTP record]], and the
[[dynamic_canvas_slice3c_verification|Slice-3C database-isolation record]].

## Decision Summary

The Dynamic Canvas is a shared, persistent presentation surface attached to a
persistent thread. It sits beside chat. The implemented user-facing surface
displays workspace files; a one-port live application surface is implemented
behind disabled launch gates. Multi-port applications remain planned. The
shared browser is also an implemented source type for attested container
workspaces, with agent presentation and a Cockpit renderer/control workflow;
it remains default-off pending its final deployment acceptance gate.

The important architectural decision is:

> **The canvas is a presentation control plane, not an automatic copy of every
> artifact.**

The content remains where it naturally belongs as each source adapter lands:

- Currently supported Markdown, text, LaTeX source, strict static HTML, raster
  images, and code remain workspace files. SVG/PDF adapters are planned rather
  than implied by their presence in a workspace.
- Streamlit, Vite, FastAPI, and other interactive prototypes remain processes
  listening on workspace ports.
- A shared browser remains a browser session owned by the agent runtime.
- PostgreSQL stores the durable presentation selection: what logical source is
  on stage, how it should be rendered, its presentation revision, and the
  source version observed when it was presented.

This deliberately distinguishes three identities:

- **`presentation_revision`** changes when the shared stage changes or the
  agent explicitly republishes the same source.
- **`source_version`** is a strong content hash/ETag for a file's bytes. It can
  differ from the current bytes without a presentation change when something
  writes around Canvas.
- **`origin_generation`** is a random, revocable browser origin for one live
  application trust unit. It is not derived from `canvas_id`.

V1 file canvases remain live workspace pointers. They are not immutable
artifact versions and are not promised to remain viewable after a remote
workspace is suspended or deleted. An optional content-addressed published-copy
layer is described later; it is not smuggled into the first slice as an
unacknowledged second store.

The agent changes the stage with a small typed tool surface:

```text
get_canvas()     -> inspect what is currently on stage
set_canvas(...)  -> point the stage at a supported file or gated workspace port
clear_canvas()   -> close the shared source without deleting the source itself
```

The eventual typed union also contains `workspace_app`; that form is not
advertised until its Slice-4 end-to-end adapter exists. `browser` is advertised
only when the runtime has the positive shared-browser capability. A disabled
Slice-3 deployment likewise withholds `workspace_port`.

For v1 there is one visible canvas named `main` per thread. Repeated
`set_canvas()` calls replace or refresh that stage. `canvas_id` remains an
internal persistence/API field so multiple named canvases can be added later,
but the model cannot supply arbitrary canvas IDs in v1.

## Mental Model

```text
                         ┌─────────────────────────────┐
                         │ Persistent session          │
                         │                             │
User <──── conversation ─┤ Chat     │ Shared canvas   │
Agent ──── conversation ─┤          │                 │
                         └──────────┼─────────────────┘
                                    │ logical pointer
                         ┌──────────┼───────────┐
                         │          │           │
                  workspace file  workspace   agent browser
                                    app
                              (one port dark;
                               multi-port planned)
```

Chat is the coordination surface. The canvas is the material and visual
surface. The user and agent can discuss a report in chat while editing the
report on the canvas, inspect a generated image without navigating to the IDE,
interact with a form backed by a workspace API, or hand control of a browser
between them.

"Dynamic component" does **not** mean that the agent generates or loads
arbitrary Angular component classes. The Cockpit owns a fixed registry of
trusted renderer components. The dynamic value is the source data or live
source passed to those renderers.

## Goals

- Put substantial visual, editable, or interactive results beside the
  conversation with one agent action.
- Let the agent and user work on the same file rather than copying content
  between chat and an editor.
- Make the first common artifact types cheap to render: Markdown with math,
  text/code, strict static HTML, and raster images. Add richer LaTeX, SVG,
  Mermaid, and PDF adapters deliberately rather than claiming existing support.
- Make a workspace web application reachable without manual port forwarding,
  proxy-link copying, or user-side CORS configuration.
- Let a frontend and its supporting API/WebSocket services share one safe
  browser origin.
- Host the shared-browser experience in the same visual surface rather than
  inventing another top-level UI.
- Keep the core extensible: a new renderer should be a Cockpit adapter, not a
  new end-to-end persistence system.

## Scope Assessment

The product idea is broad, but it is not one indivisible project:

| Deliverable | Relative scope | Why |
|---|---:|---|
| View-only file stage | Medium | State/tools, a new thread-file gateway, renderer registry, and split-pane UI |
| Turn-based file editing | Medium | Conditional writes, conflict UX, and both-writer discipline |
| Live one/multi-port apps | Large | New isolated domain/auth, SSH upstream transport, streaming HTTP/WS proxy, and browser security tests |
| Shared browser | Large but mostly adjacent | Canvas is only the host; browser identity, streaming, and control leases belong to `shared_browser.md` |
| Immutable history/public sharing | Separate product slice | Blob retention, quotas, privacy, restore semantics, and lifecycle policy |

This document therefore makes the view-only file loop independently shippable.
Live applications do not block it, and browser work does not leak into the
Canvas renderer implementation.

## Non-Goals

- Do not stream Angular/React component trees from the model.
- Do not make the canvas another IDE or replace the workspace filesystem.
- Do not proxy arbitrary agent-provided hosts or URLs.
- Do not build CRDT collaborative editing for v1.
- Do not build a free-form multi-tile dashboard before the single-stage loop is
  proven.
- Do not make external public sharing part of v1. A signed pop-out viewer for
  the current user is distinct from publishing an artifact to the internet.
- Do not resurrect the removed Builder agent. Job/expert/skill drafting may use
  the canvas later through shared application actions.

## Representative Use Cases

These span delivered and planned slices; they are labeled so the product vision
is not mistaken for the current tool schema.

- **Delivered:** A writer produces `output/report.md`; the agent presents it,
  the user edits a paragraph, and the agent reads the changed file before
  continuing.
- **Delivered:** A researcher updates a comparison table in place while
  explaining sources in chat.
- **Delivered:** A scholar and user work on a Markdown document with math
  preview or edit a LaTeX source file. Full-document LaTeX preview is deferred.
- **Delivered:** A data analyst writes `output/chart.png`; the image refreshes
  in place after the next analysis pass.
- **Delivered:** A designer builds a self-contained HTML mockup and presents it
  in a sandboxed renderer.
- **Planned Slice 4:** An agent starts a Vite frontend on port 5173 and a FastAPI
  backend on port 8000; the user sees one embedded application whose `/api`
  requests reach the backend.
- **Delivered behind the Slice-5 gate:** The agent opens a browser, presents
  it, and the user can inspect or take control of that same Chromium.
- **Dark-shipped one-port / later streaming:** An agent presents a local
  dashboard as a one-port workspace application. Native multipart/MJPEG camera
  feeds remain deferred until they have streaming-specific limits and browser
  tests; ordinary HTTP support does not imply them.

## Source Model

Canvas uses an eventual discriminated logical source internally; the flat
model-facing tool advertises only the source forms whose end-to-end adapters
are available. Neither form accepts a pod IP, generated proxy URL, bearer token,
or arbitrary hostname. The orchestrator resolves an admitted source against the
current thread and its authorized workspace/runtime.

### Workspace generation identity

`workspace_generation` is net-new lifecycle metadata, not an existing field to
assume. The orchestrator mints an opaque UUID when a thread first receives a
workspace backing and whenever an upgrade, restore, reprovision, or reassignment
creates a different backing. It persists alongside the thread's current
`metadata.workspace_container`/VM/virtual context through the existing JSONB
merge helpers; it is never derived from a mutable pod IP or a reused pod name.

A durable virtual `threads/<id>/` object-store namespace keeps its generation
across agent-pod restarts because the backing did not change. A new full
workspace, even if seeded from an old snapshot, gets a new generation. File and
app sources capture the current value; file state must be republished and live
origin sessions/direct channels are revoked when it changes. Browser sources
use their separate browser generation.

### Normalized source fingerprint

One security-critical `normalized_source_fingerprint` definition is shared by
state comparison, versioned content URLs, origin-session binding, and audit
logs. It is `sha256:` plus SHA-256 over RFC 8785 canonical JSON with an
explicit schema version:

- a file includes source type, workspace generation, and canonical relative
  path, but not mutable file bytes/source version;
- a live source is first normalized to the `workspace_app` shape and includes
  workspace generation, entry port, canonical entry path, canonically sorted
  supporting `{prefix, port}` pairs, and manifest hash (or `null` for the
  `workspace_port` shorthand);
- a browser includes source type and concrete browser generation.

The fingerprint excludes title, renderer/editability/alt metadata, derived
status/capabilities, `presentation_revision`, `source_version`,
`origin_generation`, and the one-shot `new_app` operation flag. The latter
forces origin rotation even when the fingerprint is unchanged. Switching
between model input forms never relies on Python object/string ordering: the
server owns normalization, sorting, canonical serialization, and hashing.

### `workspace_file`

Points at one file in the thread workspace.

```json
{
  "type": "workspace_file",
  "path": "output/research-report.md"
}
```

Properties:

- `path` is relative to the workspace root. A new thread-file gateway performs
  lexical validation plus `lstat`/canonical checks and rejects every symlink
  component in v1. The SFTP limitation under concurrent renames is documented
  in the security section rather than presented as a hard `openat2` guarantee.
- The file is the content source of truth.
- The server-normalized source also records the current workspace generation.
  A tier upgrade/reprovision must be republished even if it seeds the same path;
  the gateway never follows a pointer into a replacement workspace silently.
- Renderer selection uses server-detected MIME plus an allowlisted compatibility
  matrix. Extension is a hint, never the authority. The caller may request only
  a compatible, installed renderer.
- `editable` is a presentation capability, not a filesystem permission bypass.
  The normal thread/workspace authorization check still decides whether a user
  can save.
- Raster images require meaningful `alt_text` before presentation. Title and
  alt text are bounded (for example 200 and 1,000 characters) and rendered only
  as text in trusted chrome.
- There is no mandatory `canvases/` directory. Agents should normally reuse
  the repository's existing `output/` convention or present an existing project
  file instead of duplicating it merely for display.

### `workspace_port`

Points at one HTTP service in the current workspace.

```json
{
  "type": "workspace_port",
  "port": 8501,
  "entry_path": "/"
}
```

This is shorthand for a `workspace_app` with `entry_port = 8501` and no extra
routes. The port is an integer; there is deliberately no `host` field.
"Workspace port 8501" always means port 8501 on the workspace attached to the
current thread.

### `workspace_app` (planned Slice 4)

This planned source points at a browser application whose normalized route
table is read from a small workspace manifest. It is the Slice-4 extension of
the implemented-but-dark one-port `workspace_port` source; the current tool and
gateway do not read this manifest. A manifest keeps the eventual model-facing
tool schema flat; the existing fleet has weaker models which misformat nested
list-of-object tool arguments.

```json
{
  "type": "workspace_app",
  "manifest_path": ".srw/canvas.yaml"
}
```

Example manifest:

```yaml
version: 1
entry:
  port: 5173
  path: /
routes:
  - prefix: /api
    port: 8000
  - prefix: /ws
    port: 8000
```

When Slice 4 lands, `set_canvas` will read and validate the manifest, then store
the normalized route table and its manifest hash in presentation state. Later
edits to the manifest must not silently reconfigure an already-published proxy;
the agent will call `set_canvas` again.

The browser sees one isolated origin:

```text
https://<origin-generation>.canvas.example/           -> workspace:5173
https://<origin-generation>.canvas.example/assets/*   -> workspace:5173
https://<origin-generation>.canvas.example/api/*      -> workspace:8000
https://<origin-generation>.canvas.example/ws/*       -> workspace:8000
```

The planned HTTP, streaming-response, SSE, and WebSocket paths follow the same
route mapping. Longest path-**segment** prefix wins (`/api` matches `/api/x`,
never `/apix`); the entry port receives everything else. Slice 4 preserves the
prefix. Prefix stripping remains deferred because `X-Forwarded-Prefix` is
non-standard and rewriting `Location` cannot repair absolute paths embedded in
HTML/JavaScript.

### `browser` (container end-to-end adapter implemented)

The stored source now points at one concrete browser generation:

```json
{
  "type": "browser",
  "browser_generation": "5f0a9f5e-0000-4000-8000-000000000001"
}
```

The open endpoint and agent `browser_id = current` adapter resolve the live
daemon identity and stage this pointer through the existing Canvas control
plane. Canvas does not implement CDP streaming itself: the Cockpit Slice-5
renderer uses the authenticated screencast/control channel from
`shared_browser.md`. Resolution occurs immediately to the same concrete opaque
browser **generation**. The stored pointer never follows whichever browser
happens to become current later.

### Deferred source types

- **External URL** — only after there is an explicit allowlist/consent and
  framing policy. It must be a distinct source type, never smuggled through a
  generic string pointer.
- **Inline content** — not needed while normal sessions have a virtual or full
  workspace. If chat-only (`workspace.backend = none`) sessions need canvas
  documents later, add an orchestrator-owned artifact/blob source deliberately
  rather than overloading `workspace_file`.
- **Desktop/RDP** — a future trusted renderer/source adapter if real non-web GUI
  demand appears. See `rdp.md`.

## Multi-Port Workspace Applications (planned Slice 4)

A frontend and backend may use two workspace ports, but the user should not see
two unrelated public endpoints. The default is one canvas origin with internal
path routing.

The implemented one-port checkpoint can already use the first pattern after its
launch gates are enabled. The second pattern and every streaming/WebSocket
behavior below are Slice-4 design, not current tool behavior:

1. **Frontend-owned proxy.** Configure Vite, Next, or the chosen development
   server to proxy `/api` to `localhost:8000` inside the workspace. The canvas
   only needs `workspace_port:5173`.
2. **Canvas-owned routing.** Declare supporting routes in `.srw/canvas.yaml` and
   present that manifest. This supports static servers and frameworks where
   adding a development-proxy configuration is inconvenient.

Browser-side code must use relative URLs:

```javascript
await fetch('/api/users');

const wsScheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
const socket = new WebSocket(`${wsScheme}//${location.host}/ws`);
```

It must not fetch `http://localhost:8000`. In browser JavaScript, `localhost`
means the user's computer, not the agent workspace. The proxy will not rewrite
arbitrary JavaScript strings because doing so is unreliable and unsafe.

Route validation rules:

- `manifest_path` is a workspace-relative path and uses the same generation,
  regular-file, canonical-containment policy as a file source before parsing.
- The manifest is at most 32 KiB, parsed with a safe loader, and rejects custom
  tags, anchors/aliases, multiple documents, duplicate keys, and unknown fields.
  `version: 1` is required.
- Ports are integers from 1024 through 65535, minus a deployment-configured
  denylist. It includes at least VM/container SSH (`22`, `30022`), CDP (`9222`),
  and code-server (`38080`), plus deployment management daemons and any future
  reserved listener. The only allowed destination host is loopback inside the
  current thread's workspace.
- No route may contain a hostname, scheme, credentials, query string, `..`, or
  an encoded/recursively encoded structural character.
- Both model `entry_path` and manifest `entry.path` are origin-form absolute
  paths beginning with `/`; they cannot contain a scheme/authority, query,
  fragment, credentials, backslash, control byte, or reserved `/_canvas`
  segment.
- Prefixes pass the canonical path algorithm below, omit a trailing slash except
  for `/`, and may not claim reserved Canvas control paths such as `/_canvas/`.
- Duplicate/ambiguous prefixes are rejected; v1 caps a manifest at eight
  supporting routes.
- Every request path passes the same canonical algorithm again at runtime.
  Validation at `set_canvas` is not treated as a proxy request firewall.
- Prefixes are preserved. Applications use relative URLs and configure their
  own base/root path where needed.
- The proxy **replaces**, rather than trusts or appends, `Forwarded` and
  `X-Forwarded-*` metadata. It never follows upstream redirects server-side.
- HTTP and WebSocket proxying use the same authorization and route table.

The canonical path algorithm is normative for manifest prefixes, both forms of
`entry_path`, redirect targets, HTTP requests, and WebSocket handshakes:

1. Read the server's raw request-target bytes (for ASGI, the raw-path extension),
   before framework URL decoding. Accept only ASCII origin-form bytes whose path
   starts with exactly one `/`; non-ASCII characters must be percent-encoded
   UTF-8. Reject absolute-form, authority-form, asterisk-form, fragments,
   repeated leading slash, malformed percent escapes, raw backslashes, NULs,
   and other control bytes. Parse the query separately; it never participates
   in route selection.
2. Split on literal `/`. Preserve `/` itself and one final empty segment so a
   trailing slash remains semantically distinct; reject every empty interior
   segment/repeated slash and any
   case-insensitive percent escape for `/`, `\`, NUL, or a control byte.
   Strictly percent-decode each segment exactly once as UTF-8. Reject a decoded
   `/`, `\`, control byte, `.`/`..` segment, or a remaining `%HH` sequence;
   the last rule fails closed on recursively/double-encoded structure rather
   than guessing how an upstream framework will decode it.
3. Re-encode each decoded segment once, leaving only RFC 3986 unreserved bytes
   (`ALPHA`, `DIGIT`, `-`, `.`, `_`, `~`) literal and using uppercase hex for
   every other UTF-8 byte. Rejoin with `/` and preserve the accepted trailing
   slash. The resulting canonical encoded path is the **only** value used for reserved
   `/_canvas` checks, longest-prefix route selection, access policy, and the
   upstream request target. No later proxy layer normalizes or decodes it again.
4. A route prefix matches only when the canonical path equals the prefix or the
   next character is `/`; `/api` never matches `/apix`. Query parsing preserves
   order, duplicates, and blank values while dropping reserved Canvas control
   keys. The proxy then re-encodes it without logging raw values.

This intentionally rejects a few unusual but valid application paths in favor
of identical gateway/upstream interpretation. Supporting a broader path grammar
later requires differential tests against every supported upstream framework.
Ingress must pass the raw path without decoding, slash merging, or rewrite; the
live-app feature remains disabled in a deployment until a startup/integration
probe proves that contract end to end.

### Upstream transport decision

Arbitrary workspace ports are **not directly reachable**: the Kubernetes
NetworkPolicy admits orchestrator traffic only to SSH and code-server, and the
local Docker setup publishes only management ports. Slice 3 did not open a
broad workspace port range.

The selected transport is an orchestrator-owned SSH `direct-tcpip` channel over
the already-authorized workspace SSH connection:

```text
public canvas proxy
    -> request-scoped AsyncSSH direct TCP channel
    -> pooled authenticated SSH transport for this workspace generation
    -> 127.0.0.1:<declared app port> inside the workspace
```

Slice 3 extended the Canvas async SSH pool with direct TCP channels.
Pool only the authenticated SSH transport by `(thread_id,
workspace_generation)`; each proxy connection calls AsyncSSH
`open_connection("127.0.0.1", port)` through a connector which also receives and
revalidates `origin_generation` and origin-session identity. The current thin
adapter speaks strict ordinary HTTP over those reader/writer streams; Slice 4
adds the WebSocket transport. Do **not**
open a localhost TCP forwarder: another process/sidecar in the orchestrator pod
could bypass Canvas authorization by connecting to it. Workspace `sshd` should
make the intended boundary explicit with `AllowTcpForwarding local`,
`PermitOpen 127.0.0.1:*`, and `GatewayPorts no`. The Canvas service
still enforces the port denylist; SSH configuration is defense in depth.

Do not copy the current `AutoAddPolicy` behavior into this public-facing path.
The provisioner must capture/persist the workspace SSH host-key fingerprint for
the runtime generation, and both the upstream connector and file gateway must pin
it. A host-key mismatch invalidates the generation and closes all origin
sessions and direct channels.
Older container images generated SSH host keys at image build, so replicas
could share them; that is not a runtime identity. Slice 1 generates the key on
workspace first boot/init. The Kubernetes provisioner obtains its public-key
SHA-256 fingerprint through the trusted control plane (not TOFU on the same SSH
socket) and persists it with `workspace_generation` before Canvas connects.
Pre-provisioned Docker inventory must provide an equivalent exact, endpoint-
keyed fingerprint. Private host keys never leave the workspace.

The Slice-1 file gateway enables this path for provisioner-attested Kubernetes
workspaces and explicitly inventoried static Docker workspaces. VM Canvas stays
capability-gated until the VM controller can attest the runtime host key and
route that exact generation back to the orchestrator; key regeneration alone is
not attestation. Durable shared virtual storage uses its own backing identity
instead of SSH. The eventual direct-channel design can support Docker, pod, and
VM workspaces without widening NetworkPolicy once each adapter supplies the
same identity contract. It also lets prototype servers bind to `127.0.0.1`
(preferred) or `0.0.0.0`.

Static Docker containers are not a cross-tenant reset boundary. By default a
released Docker lease is quarantined until a future controller attests that the
container was recreated. `DOCKER_WORKSPACE_TRUSTED_DEV_REUSE=true` may enable
pinned-SSH convenience cleanup and reuse only in an explicitly single-user,
same-trust development deployment. Docker suspension/restore is disabled; it
must not bypass the shared job/thread lease CAS or reset a reassigned host.

Multiple independent public origins **within one presented app** are deferred.
They are occasionally useful for OAuth or deliberate cross-origin testing, but
they add CORS, cookie, certificate, and callback complexity that ordinary
prototypes do not need. Each unrelated app still receives its own random origin
generation.

## Renderer Registry

The Cockpit maps a source plus detected media type to a trusted, compiled
renderer. This registry is separate from the Debug dashboard's
`ComponentRegistryService`: Debug registers whole application panels; Canvas
registers renderers for untrusted or user-authored content.

| Source/content | First delivery | Editing |
|---|---|---|
| Markdown (`text/markdown`) | Dedicated Marked instance + bounded KaTeX pass in Slice 1 | Slice 2 |
| Plain text / code | Read-only text renderer in Slice 1; shared lazy Monaco adapter in Slice 2 | Slice 2 |
| LaTeX source (`.tex`) | Safe source text in Slice 1; Markdown math covers the common preview case | Slice 2; full document preview deferred |
| Strict static HTML | Sanitized, script-free opaque `srcdoc` iframe in Slice 1 | Source text in Slice 2 |
| PNG/JPEG/WebP/GIF | Raster image viewer with versioned content URL | No |
| SVG | `<img>` rendering only after a focused safety test; never inline in v1 | Deferred from first file slice |
| PDF | Native viewer/open fallback with Range support | Deferred from first file slice |
| Mermaid | Dedicated renderer with strict/sandbox security mode | Net-new and deferred |
| `workspace_port` | Default-off isolated-origin iframe in Slice 3 | Through the app |
| `workspace_app` | Planned multi-port isolated-origin iframe in Slice 4 | Through the app |
| `browser` | Default-off Cockpit shared-browser renderer/controller implemented in Slice 5 | Shared Browser control baton |

Renderer rules:

- MIME is detected server-side and checked against the selected renderer. The
  agent cannot force HTML bytes through an image renderer or label arbitrary
  bytes as Markdown.
- Slice 1 adds direct orchestrator dependencies for byte/magic sniffing and
  decoded-image inspection (for example `python-magic` + the required libmagic
  package and Pillow), plus AsyncSSH for the gateway. Binary formats require a
  matching signature. Decodable UTF-8 text uses an allowlisted
  extension/content compatibility rule—`.md`/`.tex` are safe semantic hints,
  not MIME types that magic bytes can prove. Pillow verification/dimension and
  decompression-bomb limits run before a raster URL is returned.
- Canvas Markdown uses a dedicated `marked` instance with raw HTML disabled.
  Reusing the global `ngx-markdown` provider unchanged would also enable the
  chat citation extension, which rewrites bare `[N]` tokens. Canvas either uses
  a separate parser or later implements an explicit thread-aware citation
  bridge. Its generated HTML still passes an explicit Canvas sanitizer/tag
  allowlist before a Trusted Types-compatible DOM assignment; “raw HTML off” is
  not treated as a complete injection defense.
- KaTeX keeps `trust: false`, finite `maxExpand`/`maxSize`, and escaped error
  output. Markdown links use an allowlisted protocol policy; external links are
  opened only through Cockpit-owned chrome with `noopener` and no referrer.
  Same-document fragments are allowed; relative workspace links are inert in
  v1 because the Canvas gateway is intentionally not a file browser. Markdown
  images render alt text only; the agent presents a workspace image as its own
  Canvas source. This prevents external tracking and data-URL resource bombs.
- A strict static HTML file is self-contained and script-free. Anything needing
  JavaScript, sibling assets, client routing, network access, or a backend is a
  gated one-port `workspace_port` today or the planned multi-port
  `workspace_app` in Slice 4. Slice 1 also strips image/font sources and permits
  no `data:` assets; present a raster image as its own validated source.
- Slice 2 extracted the diff-review Monaco integration into a shared lazy
  loader and language mapper with lifecycle-safe model/editor disposal. Canvas
  and diff review use that shared adapter rather than owning duplicate loaders.
- Text bytes, binary bytes, decoded image pixels, Markdown parser work, CSS,
  output nodes, and later PDF pages all have explicit limits. Before Marked can
  allocate a token tree, Slice 1 scans the at-most-2-MiB source once and rejects
  more than 10,000 lines or 40,000 syntax markers; the parsed tree is then
  capped at 10,000 tokens. Markdown and static HTML are both capped at 20,000
  sanitized nodes and 4 MiB of serialized output. Static HTML removes style
  blocks and active CSS properties, limits one inline style to 2 KiB and all
  retained inline styles to 64 KiB, and keeps only a small inert property
  allowlist. These complement the server byte/image limits. Unknown or
  incompatible types fail closed.
- Renderer overrides are an allowlisted enum. The agent cannot name a Cockpit
  component class or module path.
- Renderer capabilities are returned by the server. An older Cockpit or a
  disabled renderer currently preserves trusted title/source chrome and shows
  a generic unsupported-content message rather than a blank pane. Download and
  guided manual-preview fallbacks remain planned UX.

## Agent Tool Contract

The tool surface is small and flat. The thread/user/workspace context comes from
`ToolContext`; the model never supplies a user ID, thread ID, canvas ID,
workspace address, origin generation, or auth token. The orchestrator service
normalizes the flat tool arguments into the discriminated internal source union.

### `set_canvas`

The following is the **eventual** union, not the schema advertised in Slice 1:

```python
set_canvas(
    source_type: Literal[
        "workspace_file", "workspace_port", "workspace_app", "browser"
    ],
    path: str | None = None,             # workspace_file
    port: int | None = None,             # workspace_port
    entry_path: str = "/",              # workspace_port
    manifest_path: str | None = None,    # workspace_app
    new_app: bool = False,               # app kinds: rotate trust/storage origin
    browser_id: Literal["current"] | None = None,
    title: str | None = None,
    renderer: Literal["auto", "markdown", "text", "html", "image"] = "auto",
    editable: bool | None = None,
    alt_text: str | None = None,
) -> CanvasState
```

Tool factories generate the Pydantic schema from resolved capabilities and
reject missing, extra, or cross-kind arguments. Source kinds, fields, and
renderer enum members are exposed only when their server/client adapters and
runtime capabilities exist. Slice 1 advertises only `workspace_file`, title,
installed file renderers, and `alt_text` where required; `editable` appears in
Slice 2, app fields in Slices 3–4, and browser in Slice 5. Advertising the full
future union early causes models to select capabilities the client does not
have.

Optional inputs normalize before persistence. An omitted title becomes a
bounded server-generated label: the basename for a file, “Workspace
application” for an app, or “Shared browser” for a browser. An omitted
`editable` becomes `false`; `true` is accepted only in Slice 2 for a supported
UTF-8 text/static-HTML source on a writable backend. The stored state therefore
always has a descriptive title while presented and a concrete editability
boolean—`None` is an input convenience, not a durable third state.

Behavior:

1. Apply thread-owner authorization through the existing
   `require_thread_owner` helper, including its unscoped-admin bypass, and
   validate the logical source against the current thread/runtime capabilities.
2. For a file, read metadata/allowed bytes through the thread-file gateway,
   detect the renderer, and capture a strong `source_version`. The implemented
   app shorthand normalizes one port and binds it to the current workspace
   generation. Manifest normalization remains the Slice-4 extension; concrete
   browser-generation resolution is implemented in Slice 5.
3. In one SQL transaction, lock/upsert `main` and increment
   `presentation_revision`. First set becomes revision 1.
4. Preserve `origin_generation` only for an unchanged normalized live-app
   source in the same workspace generation. Rotate it when switching apps,
   changing routes, changing workspace generation, clearing and re-presenting,
   `new_app=True`, or revoking the source generation after a security incident.
   Closing one view attachment does not disrupt other views sharing the same app
   origin session.
5. After the state commit succeeds, invoke a runtime `canvas_event_callback`.
   It emits a small `canvas.updated` invalidation on the existing thread event
   path. Event delivery is not part of the state transaction; REST remains
   authoritative.
6. Return the normalized pointer, renderer/capabilities,
   `presentation_revision`, `source_version` where applicable, and derived
   status. Never return a durable proxy credential.

App publication may succeed as `starting` when the loopback port is not open
yet. The readiness check is a bounded TCP connect, not an unsolicited GET to an
agent-chosen path. The current viewer creates an attachment only for a ready
source and has no automatic readiness poll; the agent must republish or the
trusted client must explicitly reconcile after the process becomes ready.

Calling `set_canvas` again with the same file is the explicit v1 refresh
mechanism. It captures the new source hash and advances the presentation
revision even when the pointer is unchanged. This avoids filesystem watchers
and gives media a new URL. Calling it with an unchanged live app refreshes
health/status without discarding that app's browser storage or HMR connection.
When a different project/process takes over the same ports and route table, the
agent must set `new_app=True`; port equality is not application identity. The
implemented one-port tool can use `new_app=True` to rotate the origin. The
trusted Cockpit “Reset app storage” action lets the user conditionally rotate
the current live app's origin without changing its pointer.

### `get_canvas`

```python
get_canvas() -> CanvasState | None
```

Returns presentation metadata, source pointer, presentation/source versions,
capabilities, and derived current status. It does not return file/browser bytes
or claim durable `user_editing` presence; presence is a separate TTL-bound UI
signal. The agent uses its existing file/browser/application tools to inspect
the source.

The agent should call `get_canvas` before replacing an existing stage or
editing a file the user may have changed.

### `clear_canvas`

```python
clear_canvas() -> CanvasState | None
```

Clears the presentation pointer and emits `canvas.cleared`. It does not delete
the file, stop a process, or close the browser. Source lifecycle remains under
the existing workspace/process/browser tools. Clearing a non-empty stage
increments the presentation revision once and revokes matching live-app origin
sessions/direct channels; clearing an already-empty stage is an idempotent
no-op. If no Canvas row has ever existed, it returns `None`; an existing cleared
row returns its revisioned `cleared` state.

Authorization and source errors are HTTP/tool errors, not durable Canvas
statuses: `403` when `require_thread_owner` denies the caller, `404` for a
missing thread/source, `422` for an invalid pointer/renderer/manifest, and a
typed capability error when the workspace cannot support the requested source.
Manifest-specific validation applies only after the planned `workspace_app`
adapter lands.

### Registration and approval behavior

Canvas is its own tool category in `ToolsConfig` and `src/tools/registry.py`.
Putting it under the existing `orchestrator` category would make Fleet
Management configuration accidentally remove it. The initial adapters follow
the existing persistent-session pattern of constructing a short-lived
orchestrator client carrying internal and delegated-user headers; persistent
`ToolContext.orchestrator_client` is not currently populated.

This is a closed loader surface, so implementation must update all of it:
`ToolsConfig.canvas`, config parsing/defaults, `get_all_tool_names`, the registry
factory branch, persistent-session create override/disabled-marker allowlists,
and any admin tool catalog. Apply the final capability filter only after the
workspace backend and feature flags are resolved. The current skill resolver
includes `present-with-canvas` only when **both** `use_skill` and `set_canvas`
survived that filter; the current registry guarantees that an admitted
`set_canvas` includes file support. It does not infer availability merely from
the expert name. A future browser-only runtime must revisit this file-oriented
scoping rule and keep essential safety/handoff etiquette in its tool
description if `use_skill` is absent.

Expose get/set/clear only in authenticated persistent sessions with a nonblank
delegated internal key, then capability-filter source kinds. File presentation
requires a readable durable/remote workspace; apps require a live shell-capable
workspace; browser requires a stable Shared Browser generation. `none`,
process-local virtual memory, unattested VM/direct-remote backends, and a runtime
without `MCP_INTERNAL_KEY` do not receive nonfunctional file Canvas tools or the
companion skill.

The provisioner/runtime handoff exposes only the positive boolean
`canvas_presentation_available`. It becomes true only after the current thread
is paired with the exact usable workspace generation and, for SSH-backed
storage, its attested host-key fingerprint and lease. Initial attachment and
live capability upgrades consume the same bit. Lease IDs, trust provenance,
backing IDs, fingerprints, and generation internals remain server-side and are
removed from public job/thread representations.

These tools change only thread-authorized presentation state and do not mutate
file content or start processes, but they still respect the session's existing
permission contract: supervised mode prompts for ordinary tools;
`auto_accept`/autonomous modes do not special-case Canvas into an extra prompt.
Do not silently bypass supervised mode merely because repeated refresh approval
may be noisy. If usage proves that painful, design an explicit remembered
presentation grant rather than hardcoding a hidden exemption.

### Why not a generic string pointer

`set_canvas(pointer="localhost:8501")` looks convenient but loses the
information needed to validate and render safely:

- Which localhost: agent container, workspace pod, orchestrator, or user
  device?
- Is the source a file, HTTP app, browser, or external URL?
- Which other ports support the entry page?
- May it be edited, framed, or opened in a new tab?
- Does the value attempt to reach an orchestrator-internal or cloud-metadata
  address?

A discriminated source is only slightly more verbose for the model and gives
the orchestrator a closed, testable authorization surface.

## Expected Agent Workflow

For a file artifact:

```text
1. Create or update output/report.md with workspace tools.
2. If it may already be on stage, call get_canvas and inspect current state.
3. Call set_canvas(source_type=workspace_file, path=output/report.md).
4. Tell the user what is available to inspect or edit.
5. Before a later edit, re-read the current file so user changes are preserved.
6. After writing, call set_canvas again to refresh the stage.
```

For a one-port live application (**implemented but dark-shipped Slice 3**):

```text
1. Build the application in the workspace.
2. Make browser requests relative. If it needs a backend today, configure the
   frontend development server to proxy it behind the one presented port.
3. Prefer binding servers to `127.0.0.1`; the SSH connector reaches workspace
   loopback without exposing the port on the pod/VM network.
4. Configure the development server with the deployment-provided Canvas host
   suffix and a strict port; never use a wildcard/all-host bypass.
5. Start the process and verify the entry port.
6. Call set_canvas with workspace_port.
7. Keep the processes alive while the user is interacting.
8. On restart, call set_canvas again to refresh status/config.
   Use new_app=true when the restart is a different application/trust unit,
   not a normal restart of the same prototype.
```

The `.srw/canvas.yaml`/`workspace_app` route-manifest workflow is planned Slice
4. It is not an alternative available to agents in the current schema.

For a shared browser (**implemented behind the default-off Slice-5 gate**):

```text
1. Navigate the agent browser to the relevant state.
2. Call set_canvas(source_type=browser, browser_id=current).
3. Tell the user whether the browser is view-only or available for takeover.
4. Respect the browser ownership/handoff protocol before resuming automation.
```

## Companion Skill: `present-with-canvas`

The tools define the mechanical contract. A bundled skill teaches judgment and
workflow that does not belong in every tool description.

Implemented location:

```text
config/skills/present-with-canvas/SKILL.md
```

The first version uses `SKILL.md` plus package metadata only; it does not justify
scripts, assets, or reference files. It is concise and imperative. Its
frontmatter name is `present-with-canvas`, and its current description triggers
for substantial visual/editable workspace-file artifacts, including a strict
static-HTML prototype. Live web prototypes and browser state belong in the
future app/browser description only after those user-facing surfaces land.

The deployed file-only skill covers presentation judgment, refresh discipline,
editable-file re-read/preservation, static-HTML limits, and stage safety. It
intentionally refuses to guess app, port, browser, or routing fields which are
not advertised by the current tool schema. Extend it with app/browser guidance
only as those corresponding end-to-end presentation surfaces land. The
default-off Slice-3A–3C implementation is not yet a deployed user-facing
surface, so the bundled skill remains file-focused for this checkpoint.

The current and planned guidance contract covers:

- When substantial content belongs on the canvas rather than in chat.
- Choosing `workspace_file`, `workspace_port`, `workspace_app`, or `browser`.
- Reusing `output/`/existing project files instead of copying content into a
  canvas-specific store.
- Self-contained static HTML versus a live workspace application.
- Binding servers to workspace loopback, using relative browser URLs, and
  configuring frontend or canvas-owned backend routes plus the exact
  deployment-provided host suffix (never an all-host bypass).
- Verifying readiness before presenting and republishing after updates.
- Using `new_app=true` when unrelated code takes over the same port/manifest;
  preserving the origin only for the same prototype's ordinary refresh/HMR.
- Calling `get_canvas` and re-reading editable files immediately before
  overwriting them; never assuming the file still matches the last presented
  version.
- Browser handoff etiquette.
- Never presenting secrets, internal credentials, fake trusted auth chrome, or
  arbitrary internal network targets.
- Avoiding the canvas for short answers or tiny snippets that read better in
  chat.

The skill is deployed with the tools and is placed in scope only when both
`use_skill` and `set_canvas` are available. It uses model-invoked activation
initially; a hard
`before_tool:set_canvas` binding can dead-end a `workspace.backend = none`
runtime where `use_skill`/Canvas capabilities are absent. Critical security
rules stay enforced in code and summarized in the tool description. After
capability-aware bindings exist, telemetry can justify upgrading the skill to a
hard first-use gate.

The bundled package is a catalog floor, not an overwrite policy. A resolved
user/global `present-with-canvas` entry replaces it as a whole. Runtime
capability re-scoping withdraws only unchanged files previously created by SRW;
modified skill files and unrelated files in that directory are preserved as
user content.

## Cockpit Experience

### Desktop

The current `/sessions/:threadId` route uses a thin `ChatPageComponent` wrapper
around `PersistentChatComponent`. Slice 1 delivered Canvas as a sibling at that
wrapper rather than inserting another responsibility into the already-large
persistent chat component.

```html
<as-split>
  <as-split-area>
    <app-persistent-chat />
  </as-split-area>
  <as-split-area>
    <app-canvas-pane />
  </as-split-area>
</as-split>
```

The existing `angular-split` dependency implements the first chat/canvas split.
The current pane has Cockpit-owned chrome that agent content cannot cover:

- title, source summary, and renderer indicator;
- agent-generated/untrusted-content label where applicable;
- loading, starting, source-changed, unavailable, ended, and error states;
- edit/preview toggle for editable files;
- refresh and close controls;
- source path or logical service summary without leaking internal addresses;
- updating/editing/saved and presentation-revision status;
- a normalized file/live-app source-kind badge and the browser time of the last
  successful authoritative state GET or mutation (suppressed while a dirty
  editor buffer is intentionally preserving older state); and
- capability-gated pop-out plus live-app origin-reset controls.

`CanvasService` is keyed by `threadId`, cancels stale requests on route changes,
and has no fallback to a previously viewed thread while the root draft has no
ID. A new source opens the desktop pane by default. On mobile the pane remains
mounted but `inert` and `aria-hidden` behind the trusted Canvas toggle/tool card
until the user enters it; its internal live region therefore does not announce
while hidden. Automatically making it full-screen would strand the currently
focused chat control inside a newly inert subtree. A user preference may later
change desktop auto-open to a badge. Republishing the same source refreshes
without stealing focus or resetting scroll/editor selection. Closing the pane
is a local presentation action; `clear_canvas` changes shared thread state.

An invalidation never overwrites a dirty editor buffer. If the source version
changes while the user has unsaved edits, keep the buffer and show a trusted
“source updated—compare/reload” banner. Read-only same-source updates preserve
scroll/zoom where the renderer can map them safely.

`PersistentChatService` continues to own the canonical SSE stream and control
sender. Slice 0 delivered the smaller typed bridge: SSE/WebSocket lifecycle
stays in the existing singleton owner, while
`PersistentThreadTransportBridge` exposes a read-only decoded
`{method, params}` event stream and a narrow typed `sendCanvasControl(...)` API
to the sibling. Canvas state remains outside the already-large chat service; a
later general transport extraction is separate refactoring.

Because the current chat header also lives inside `PersistentChatComponent`,
the wrapper owns an accessible Canvas toggle/floating action instead of reaching
into that header. Tool cards provide the second reopen path.

Each successful `set_canvas` tool result gets a compact trusted tool card in
chat with the generic presentation action, a sanitized basename-oriented
subtitle, revision context, “Open Canvas”, and whether that presentation has
since been replaced. This keeps the artifact discoverable after the pane is
closed and decouples the durable conversation affordance from one side-pane
layout. Since v1 does not snapshot bytes, an old card cannot pretend to restore
an old file; it opens the current stage or explains that its revision was
replaced. The card now renders a bounded normalized file/live-app/Canvas kind
and sanitized title without echoing a port, URL, or workspace-internal pointer;
its rendered action and current/replaced context have component coverage.

### Mobile

Mobile uses the same Angular route and components. The canvas becomes a
full-screen view/drawer with a clear return-to-chat control; it is not loaded
through a second mobile component system. Multi-tile layout is not attempted on
mobile in v1. Use the existing 768 px `ViewportService` breakpoint.

Switching between Chat and Canvas in the **same thread** keeps a live iframe
mounted to preserve form/app state, but makes the hidden pane `inert` and
`aria-hidden`. The app may still consume network/CPU while hidden, so the chrome
keeps a visible Close action. Slice 5 must pause host-owned browser streaming
while its future renderer is hidden. Switching threads, clearing/replacing the
source, or closing Canvas tears the current source down and closes any live-app
view attachment. It does not revoke a shared origin session which another
Cockpit tab or authenticated wrapper pop-out is still using.

### Pop-out

The authenticated `/sessions/:threadId/canvas` wrapper route renders the same
`CanvasPaneComponent` under the normal auth guard. Ready workspace files expose
`can_pop_out=true`; a ready live app does so only when viewer-session creation
is configured. Cockpit opens only that wrapper with `noopener,noreferrer`,
suppresses the normal sidebar/PWA/readiness banners there, and retains trusted
close/navigation chrome. Pop-out is another rendering target for the same
logical source, not a second persistence model.

An untrusted live app remains a labeled sandboxed iframe using the same
short-lived embedded origin-session flow; it is never opened top-level. The
wrapper reconciles authoritative state while visible, and a bounded
`BroadcastChannel` revision pointer accelerates same-browser convergence without
trusting or copying Canvas state across tabs. Browsers missing the parent Fetch
Metadata contract, or unable to retain the transient partitioned bootstrap
cookie, receive typed trusted unsupported UX. Every other bootstrap/service
failure uses non-causal temporary-unavailable copy. Both paths direct the user
to the authenticated IDE/manual-preview workflow without a bearer URL or a
weaker top-level fallback.

## Accessibility Contract

The surrounding Canvas chrome is a trusted, accessible escape hatch even when
the presented content is not accessible:

- The splitter is keyboard operable and follows the window-splitter pattern:
  named `role="separator"`, `aria-controls`, current/min/max values, arrow-key
  movement, and a non-drag collapse/expand action.
- Every iframe has a descriptive `title`; every image has agent/user-provided
  `alt_text`. `set_canvas` returns `422` for a raster image without a meaningful
  description; “no description provided” is only a defensive fallback for a
  legacy/corrupt row, not valid creation behavior.
- A polite status region announces new/updated/unavailable content without
  moving focus. Same-source refresh never steals focus.
- “Skip to Canvas” and “Return to chat” controls let keyboard users escape an
  editor or iframe. Closing a mobile view restores focus to its opener.
- The host reflows at 320 CSS px, keeps visible focus, respects reduced motion,
  and uses at least 24×24 CSS px pointer targets.
- Real-browser tests must cover iframe tab order, splitter keys, mobile
  back/focus, and screen-reader names; jsdom alone cannot prove these behaviors.

## Persistence Model

**Workspace/live source is authoritative for content. PostgreSQL is
authoritative for shared presentation state.**

The implemented application migration sequence is:

- `0058_canvases.sql` creates the thread-scoped presentation state;
- `0059_docker_workspace_leases.sql` supplies the durable static-Docker
  endpoint authority required by the trusted file gateway; and
- `0060_canvas_events_epoch_comment.sql` records the newer event-generation
  catalog comment without modifying the already-applied `0058` bytes;
- `0061_canvas_viewer_sessions.sql` creates hashed origin sessions,
  non-credential attachments, initial bootstrap state, cleanup indexes, and
  revocation triggers; and
- `0062_canvas_bootstrap_exchange.sql` adds attachment cookie-mode binding and
  replaces transferable bootstrap state with the browser-bound
  challenge/exchange contract without modifying already-applied `0061` bytes.

Applied migrations are checksum-tracked and immutable. Restore any drift to the
recorded bytes and put later changes in a superseding migration; never rewrite a
`schema_migrations` checksum. A local rollout later detected drift in
already-applied `0061`; the repair restored its recorded bytes and kept the
subsequent attachment `cookie_mode` column and constraint in `0062`. The
migration ledger was not edited. The implemented v1 state shape is:

```sql
CREATE TABLE canvases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    canvas_id VARCHAR(64) NOT NULL DEFAULT 'main',
    source JSONB,                         -- null after clear
    title TEXT,
    renderer VARCHAR(32) NOT NULL DEFAULT 'auto',
    editable BOOLEAN NOT NULL DEFAULT FALSE,
    alt_text TEXT,
    presentation_revision BIGINT NOT NULL DEFAULT 0,
    source_fingerprint TEXT,              -- sha256 of canonical logical source
    source_version TEXT,                  -- strong sha256 ETag for file bytes
    origin_generation UUID,               -- live-app isolation generation
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, canvas_id)
);

CREATE UNIQUE INDEX uq_canvases_origin_generation
    ON canvases (origin_generation)
    WHERE origin_generation IS NOT NULL;
```

Important boundaries:

- There is no content column in v1. The row is a live selection, not an
  immutable artifact version.
- There is no `_index.yaml`; ordering/layout does not exist for the single
  canvas. If multiple canvases arrive, shared ordering can be added then.
- Split size, open/closed state, and similar personal UI preferences stay out
  of the shared canvas row. They are ephemeral component signals today;
  persistent Cockpit local/user settings remain a follow-up.
- `presentation_revision` and `source_version` have distinct meanings. Use
  `sha256:<lowercase-hex>` as the file version and emit it as a quoted strong
  HTTP ETag.
- `source_fingerprint` is computed from the normalized-source contract above
  and stored atomically with `source`; adapters never supply it.
- Viewer secrets, resolved workspace IPs, and generated proxy URLs are
  short-lived derived values and are never stored in `source`.
- `status` is derived from current source/runtime health. Invalid or
  unauthorized mutations are request errors and are not persisted as state.
- V1 is thread-scoped. Batch-job canvases can be added later with an explicit
  scope model rather than a nullable `job_id`/`thread_id` combination that
  permits both.
- Short-lived live-app authentication does not overload this row. Slice 3B
  added shared stores/tables:

    - `canvas_origin_sessions` contains only a hash of the gateway-cookie secret
      plus user, thread/canvas, issued presentation revision, normalized source
      fingerprint, workspace/origin generations, embedding site/cookie mode,
      expiry, and revoked timestamps;
    - `canvas_view_attachments` tracks non-credential per-frame/window IDs,
      hashed parent bridge nonces, issuance embedding origin and cookie mode,
      last-seen/closed timestamps, and the shared origin session they use
      (nullable until bootstrap exchange);
    - `canvas_view_bootstraps` contains one state row per attachment. It moves
      from created, to browser-challenged, to parent-authorized, to consumed,
      storing only purpose-separated hashes of the challenge, transient browser
      binding, ready receipt, and one-time exchange code alongside the expected
      presentation/origin identity and expiry. A session can therefore admit a
      new tab without a transferable URL bearer or overwriting its shared
      cookie.

  Plaintext handshake/session material is never persisted; expiry indexes
  support cleanup across multiple orchestrator replicas.

Planned Slice-4 multi-port stored source:

```json
{
  "type": "workspace_app",
  "entry_port": 5173,
  "entry_path": "/",
  "routes": [
    {"path_prefix": "/api", "port": 8000}
  ],
  "manifest_path": ".srw/canvas.yaml",
  "manifest_version": "sha256:...",
  "workspace_generation": "opaque-runtime-generation"
}
```

The current one-port shorthand normalizes to the same private source family but
stores one `entry_port`, an `entry_path`, an empty route table, and no manifest
path/version. It does not accept the example above from the tool or internal
adapter yet.

State transitions are atomic at the row level:

- `set_canvas` locks/upserts the row and advances the revision once; first set
  is revision 1;
- same-pointer refresh advances once;
- clear of a populated row nulls `source`, `source_fingerprint`, `source_version`,
  `origin_generation`, title/alt metadata, sets editability to `false`, resets renderer to
  `auto`, revokes matching origin sessions/direct channels, and advances once;
  repeated clear is a no-op;
- user clear supplies the exact current state ETag, while file edit supplies
  both the presentation revision and source-version preconditions, so neither
  can act on a source replaced between render and click/save;
- the service restricts callers to `main` in v1 even though the schema leaves
  room for later named canvases.

## Thread Workspace File Gateway

Before Slice 1, persistent threads had upload-only file routes. The existing
job file routes were text-only and did not address a persistent thread's
SFTP/virtual workspace. Slice 1 therefore introduced net-new, Canvas-scoped
file infrastructure rather than reusing a general thread file API.

The delivered gateway is protected by `require_thread_owner` (including its
existing unscoped-admin bypass) and does not expose an arbitrary thread
filesystem browser:

```text
GET/HEAD /api/persistent/threads/{thread_id}/canvases/main/content
PUT      /api/persistent/threads/{thread_id}/canvases/main/content   # Slice 2
```

Slice-2 content `PUT` carries the file hash in standard `If-Match` and the
separate current Canvas state in `X-Canvas-Presentation-Revision: <n>`; it never
accepts a caller-supplied path. Both are required before reading a request body.

The state endpoint supplies a content URL containing
`presentation_revision`, `normalized_source_fingerprint`, `source_version`, and
`ngsw-bypass=true`. The gateway derives the path from the current Canvas row;
the browser never turns this into `?path=arbitrary/file`. All three identity
values are required and checked **before** resolving its path. A stale
presentation revision with the same fingerprint returns
`409 canvas_presentation_changed` and the current revision; a different fingerprint
returns `409 canvas_replaced`, while a cleared row returns `409 canvas_cleared`.
The typed response may say whether the logical source is unchanged but never
returns a replacement path to a stale renderer. An old URL can therefore never
serve new bytes or a replacement source under its old rendering context.

`ThreadWorkspaceFileGateway` resolves the persisted thread workspace spec and
uses production backends only:

- remote/full workspaces: reuse thread-upload SSH target resolution, but use a
  generation-keyed, host-key-pinned async SSH/SFTP pool with real-path
  checks and whole-chain symlink rejection, subject to the documented SFTP
  check/open race; this same pool later opens live-app direct channels;
- virtual workspaces backed by a shared durable object store: reconstruct the
  deployment's rclone/object-store spec from orchestrator environment/Secrets
  and the deterministic `threads/<thread_id>/` prefix. Credentials are injected
  in process and never persisted in the thread or Canvas row; extract the
  existing `_virtual_workspace_rclone_spec` logic out of `main.py` rather than
  duplicating it;
- process-local in-memory virtual stores: return a typed `unavailable` unless a
  future live-agent broker is selected, because another process cannot see the
  agent's memory;
- scratch/`none`: unsupported.

Do not serve one file by unpacking the whole suspended-workspace tarball. For an
inactive remote workspace, v1 reports `unavailable`. This is the honest boundary
between a live shared stage and durable publishing.

> **SUPERSEDED 2026-07-28 for file sources** — `docs/features/canvas_durable_presentation.md`.
> The "reports `unavailable`" half no longer holds: a file Canvas now keeps one
> copy of its published bytes in the object store and serves that, read-only and
> labelled, when the workspace cannot be reached. The tarball prohibition stands
> unchanged — the copy is captured at publish time, never extracted from a
> workspace snapshot. Live apps and shared browsers still report `unavailable`.

### File response contract

For an allowed file, support `GET`, `HEAD`, and a single byte range so image and
later PDF viewers do not require another endpoint. Responses include:

```text
ETag: "sha256:..."
Content-Type: <server-detected allowed type>
Content-Length: ...
Last-Modified: ...                       # when the backend has it
Accept-Ranges: bytes
Content-Disposition: inline|attachment; filename*=UTF-8''<encoded-name>
Cache-Control: private, no-cache         # live file
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

Honor `If-None-Match` with `304` and `If-Range` for byte serving. Return
`206`/`Content-Range` for a valid range and `416` for an invalid one. If the
current bytes no longer match the row's requested `source_version`, return a
typed `409 source_changed`; do not show different bytes as the same published
presentation. The UI offers “ask agent to refresh” or an explicit “load current
workspace version”.

For unversioned SFTP, read the bounded regular file into memory and hash those
exact bytes before sending headers; do not mint a strong ETag from size/mtime
and then stream potentially different bytes. Shared virtual storage likewise
uses a transport-level `max + 1` read, so a stale object HEAD cannot turn into
an unbounded `rclone cat`. Materialization, CPU validation, retained response
buffers, and backend reads each have bounded concurrency/queue waits. A response
buffer lease is acquired before the read and held through the final ASGI send or
cancellation, preventing slow clients from retaining unlimited 25 MiB bodies.
This is transient delivery, not artifact persistence.

Default configurable limits for the first slice are 2 MiB text/Markdown/HTML,
25 MiB encoded raster image, 40 megapixels decoded image, and 50 MiB absolute
file ceiling. Active document types are never navigable from the trusted API
origin: safe raster/text may be inline, HTML/unknown active types use attachment
plus a restrictive response CSP, and HTML is fetched as text for sanitized
`srcdoc`. Filenames are encoded, never reflected as raw header text. SVG/PDF
remain deferred until their safe response/viewer path is tested.

### Optional immutable published copies

> **EXERCISED 2026-07-28, at smaller scope** — `docs/features/canvas_durable_presentation.md`.
> Offline viewing did become a requirement. What shipped is one current copy per
> Canvas in the object store (`canvas_snapshots` + `SnapshotService.put_blob`),
> not the content-addressed archive sketched below: keys are thread-scoped
> rather than hash-shared, because sharing an object between threads makes
> deletion ambiguous and leaks existence across tenants. Restore history remains
> unbuilt. The rest of this section still describes that larger option.

If offline viewing or restore history becomes a requirement, add a separate
content-addressed `canvas_blobs`/object-store layer. Snapshot only when a changed
file is published or saved, deduplicate by hash, retain the live workspace path
as the editing target, and define quotas, retention, deletion, Range, and
privacy. Existing citation snapshot helpers demonstrate content-addressed S3
storage, but S3 is optional and their whole-byte API is not automatically a
Canvas blob service. Ports and browser streams remain ephemeral.

## Control Plane and Real-Time Updates

The orchestrator remains authoritative for shared canvas state and
authorization.

Suggested layers:

```text
set_canvas LangChain tool
        │
        ├──── orchestrator client/action ────► orchestrator CanvasService
        │                                      ├─ require thread owner/admin
        │                                      ├─ validate/normalize source
        │                                      └─ commit presentation row
        │
        └──── callback after commit ─────────► agent `_broadcast`
                                               ├─ live WS compatibility fan-out
                                               └─ ordered event writer
                                                      │
                                                      ▼
                                                `thread_events` journal
                                                          │
                                                          ▼
                                               existing thread SSE stream
                                                          │
                                                          ▼
                                        shared thread transport → CanvasService
```

Do not implement separate semantics in the agent tool, REST route, and future
MCP adapter. Put validation and state transitions in one orchestrator service
or shared application action, then keep adapters thin.

Implemented user-facing state/file and trusted live-app mutation API:

```text
GET    /api/persistent/threads/{thread_id}/canvases/main
DELETE /api/persistent/threads/{thread_id}/canvases/main
POST   /api/persistent/threads/{thread_id}/canvases/main/refresh
POST   /api/persistent/threads/{thread_id}/canvases/main/reset-origin
```

`GET` returns `204 No Content` when no row has ever existed. Conditional
`DELETE` returns `204` for an absent/already-cleared no-op and `200` with the new
cleared state when it actually transitions. Public
arbitrary source-setting is not part of v1; the delegated internal agent action
uses one explicit transport adapter:

```text
GET    /api/internal/persistent/threads/{thread_id}/canvases/main
POST   /api/internal/persistent/threads/{thread_id}/canvases/main/set
DELETE /api/internal/persistent/threads/{thread_id}/canvases/main
```

The internal routes require both a valid `X-Internal-Key` and delegated
`X-MCP-User-Id`; anonymous-internal and BFF-only callers are rejected, and
public ingress strips `/api/internal/*` plus those headers. Authentication
constructs the delegated user principal, `require_thread_owner` applies the
same owner/admin rule, and the adapters call the same `CanvasService.set` and
`CanvasService.clear` actions as every future adapter. Set accepts the
capability-filtered typed request, never caller-supplied
user/workspace/origin fields; clear intentionally acts on the current row
without a browser ETag. The agent's
short-lived `OrchestratorClient(user_id=...)` already implements this header
pattern; there is no in-process service call across the runtime/orchestrator
boundary.

File-only `refresh` accepts no path/source body: it derives the current row,
re-hashes the live file, adopts that `source_version`, and advances presentation
revision. This is the explicit implementation of “load current workspace
version”. Before adopting, it reruns workspace-generation, regular-file,
whole-chain symlink, size, byte-signature/MIME, renderer-compatibility,
decoded-image, and required-alt validation. Any validation/read failure leaves
the row unchanged. The live-app-only `reset-origin` likewise accepts no pointer:
it preserves the normalized app source and presentation metadata while
atomically rotating `origin_generation` and advancing presentation revision.
The existing database trigger revokes the retired generation's origin sessions,
notifies every gateway replica, and cancels registered ordinary-HTTP/direct-
channel work. `new_app=true` remains the model-facing way to declare that an
agent presentation on the same port is a different application.

State `GET` returns a strong representation tag such as
`ETag: "canvas:<presentation_revision>:<sha256-of-authorized-serialization>"`.
One deterministic serializer hashes the exact caller-visible response bytes,
including derived status/capabilities and URLs; `presentation_revision` remains the separate
domain revision used by events and content preconditions. State responses use
`Cache-Control: private, no-cache` so conditional requests revalidate. A browser
`DELETE` or `refresh` of an existing row requires the exact current state tag
in `If-Match`; missing/stale preconditions return `428`/`412`, including when
health or caller capabilities changed. `reset-origin` uses the same exact strong
precondition and rejects a cleared or non-live-app Canvas.

"Exact" means the digest, not the wire bytes. A compressing CDN in front of the
API rewrites the strong `ETag` it forwards into the weak form, so `W/"canvas:…"`
is the only value a browser that read the state through one can echo back —
`server: cloudflare` returns `W/"canvas:1:<digest>"` for the identical response
the origin tags `"canvas:1:<digest>"`. Every state precondition therefore drops
a leading `W/` before comparing (`strong_state_precondition`), exactly as the
conditional `GET` has always done, and compares the full digest after that. It
never expands `*`: these preconditions name one state, not whatever exists now.
Miss this and the weak tag passes the conditional `GET` (`304`) while failing
every mutation (`412`/`400`), which is a live-app viewer that reconciles and
re-attaches forever. The Cockpit backs that up: a create rejected for one exact
desired state is not retried for that same state until the state changes or the
user retries. The agent tool uses a
server-identifiable internal + delegated-user adapter and may intentionally
replace current state under the row lock; never
trust an actor field/header supplied by a public client to select that behavior.
A successful browser mutation is applied from its REST response and sends a
typed source or presentation invalidation through the active runtime. A bounded
revision-only `BroadcastChannel` signal accelerates same-browser tab/pop-out
reconciliation, while focus/visibility and wrapper polling repair a missed
signal by fetching authoritative REST state; no channel payload is trusted as
Canvas state.

Slice 3B added authenticated parent APIs rather than putting a reusable bearer
in the iframe URL:

```text
POST   /api/persistent/threads/{thread_id}/canvases/main/view-attachments
POST   /api/persistent/threads/{thread_id}/canvases/main/view-attachments/{id}/authorize
POST   /api/persistent/threads/{thread_id}/canvases/main/view-attachments/{id}/renew
DELETE /api/persistent/threads/{thread_id}/canvases/main/view-attachments/{id}
```

Creation requires the current Canvas `If-Match` and an exact same-origin/
same-site CORS Fetch Metadata tuple. A client which cannot supply that browser
boundary receives `409 canvas_browser_unsupported`. The route derives the
embedding site and allowed cookie mode from trusted request/deployment state,
and returns a bounded
attachment ID, an exact `/_canvas/bootstrap?attachment_id=<uuid>` locator, and a
one-use bridge nonce held only in trusted Cockpit memory. The URL contains no
credential. The authorize endpoint accepts the gateway challenge, ready receipt,
and bridge nonce through the exact parent BFF session and returns a short-lived
exchange code only after user/thread/session/origin/source identity is
revalidated. It never accepts a caller-selected upstream or origin. Renewal
extends the linked origin session only while authorization and every
source/workspace/origin identity still match. Attachment delete is an
idempotent presence close, not a shared-session revocation. These non-safe APIs
retain BFF CSRF protection; global logout and clear/replacement use
service-level bulk revocation after their authoritative state transition.
Reset-origin reaches the same revocation path through the authoritative
origin-generation update rather than deleting one attachment.

Use separate serializers: the agent gets logical metadata/capabilities,
while the Cockpit REST response may also get a relative authorized `content_url`
or a `can_create_viewer_session` flag. Neither receives workspace addresses,
origin-session secrets, or internal SSH data. A representative state is:

```json
{
  "canvas_id": "main",
  "source": {"type": "workspace_file", "path": "output/report.md"},
  "title": "Research report",
  "renderer": "markdown",
  "editable": false,
  "alt_text": null,
  "presentation_revision": 7,
  "source_version": "sha256:...",
  "status": "ready",
  "capabilities": {
    "can_edit": false,
    "can_pop_out": true,
    "can_take_control": false
  },
  "updated_at": "..."
}
```

Suggested events:

```json
{
  "method": "canvas.updated",
  "params": {
    "canvas_id": "main",
    "presentation_revision": 7,
    "source_type": "workspace_file",
    "updated_at": "..."
  }
}
```

Events carry invalidation metadata, not full state, files, or image bytes. They
may be delayed, duplicated, or missed across a crash. On initial load and every
invalidation the Cockpit fetches authoritative REST state, ignores revisions no
newer than the one applied, and then fetches content through the authorized
adapter. It also reconciles when the route/tab regains focus after a bounded
staleness interval. Canvas does not add its own stream.

Slice 0 fixed the prerequisite ordering defect in `_broadcast`: sequence-bearing
events now pass through one per-runtime ordered, batch-capable writer instead
of independent fire-and-forget database writes. It inserts batches in sequence
order; overflow may coalesce token deltas but never silently drops state
invalidations. A failed write is logged and forces clients to reconcile.
Canvas/state invalidations retry within a bounded queue. On terminal persistence
failure, live subscribers receive a direct, non-journaled
`canvas.reconcile_required` control frame which the typed bridge handles even
when normal `_seq` WebSocket duplicates are discarded. With no live control
channel, convergence occurs on route/focus reload. This improves all persistent
events but does not claim transactional or exactly-once delivery; authoritative
Canvas REST reconciliation makes missed invalidations safe.

The current transport split matters:

- **Agent-originated change:** after the orchestrator accepts `set_canvas`, the
  tool invokes a callback on `ToolContext`; the persistent runtime calls
  `_broadcast("canvas.updated", ...)`, which queues the event for ordered
  journal persistence and canonical SSE delivery.
- **Cockpit-originated edit:** after a successful conditional file save, the
  shared transport sends
  `{"method":"canvas.source_updated","canvas_id":"main",...}` on the
  control WebSocket. The runtime calls
  `ToolContext.invalidate_recent_read(path)` rather than mutating the private
  `_recent_reads` deque externally, then reloads current Canvas state and
  broadcasts an invalidation. The saving Cockpit applies the REST response
  immediately.
- **No live agent:** the durable Canvas row/file write still succeeds when the
  workspace is available. A later route load reconciles from REST; live
  cross-tab fan-out while no session runtime exists is deferred rather than
  creating a second event sequencer that can race the agent's cursor.

The production Angular service worker registers immediately. Add
`ngsw-bypass=true` to file/image/PDF URLs because element requests cannot attach
the existing bypass header, and include both presentation/source versions to
avoid ordinary browser cache ambiguity. Live apps are on another origin and
outside the Cockpit service-worker scope.

## User Editing and Concurrency

**Decision: turn-based collaboration with honest, best-effort optimistic file
writes. No CRDT and no claim of cross-backend atomic CAS in v1.**

The pointer model makes the conflict boundary explicit: both parties edit the
same workspace file. `set_canvas` itself does not mutate content.

Mechanism:

1. A file response includes a strong SHA-256 ETag.
2. Every content `PUT` requires both `If-Match: "sha256:..."` and
   `X-Canvas-Presentation-Revision: <n>`. Missing preconditions return `428
   Precondition Required`; a false `If-Match` returns `412 Precondition Failed`
   (not `409`). A same-source republish returns `409
   canvas_presentation_changed`; an actual source change returns `409
   canvas_replaced`. After the cheap initial precondition/size check, read the
   bounded body into a private spool before taking database locks; a slow client
   must not hold the coordinator or Canvas row lock.
3. The gateway takes a **cluster-wide** coordinator lock, re-reads/hashes,
   writes a temporary file and atomically renames where the backend supports it,
   then reads back the resulting hash. Implement the lock as a bounded
   PostgreSQL session-level advisory lock on a dedicated connection, keyed by a
   stable cryptographic digest of `(thread_id, canonical_path)`—never a
   process-local mutex or Python's randomized `hash()`. After acquiring it, use
   that connection's transaction to lock the Canvas row and revalidate the
   expected revision, source path, and current hash a second time; keep the row
   lock through the bounded workspace write/readback and state update so
   clear/replacement cannot redirect an accepted save to a stale path. All
   Canvas content writers acquire locks in this order. Timeout returns a typed
   busy/`423` result and every exit path releases the lock. On success,
   CanvasService updates `source_version` and presentation revision in that
   transaction.
4. The Cockpit emits TTL-bound `user_editing`/`idle` awareness. It is a courtesy
   signal, not a durable lock and not part of `get_canvas`.
5. On `canvas.source_updated`, the agent's recent-read cache for the path is
   invalidated. The companion skill requires `get_canvas` plus a fresh
   `read_file` immediately before agent file-tool edits; shell writes to an
   editable Canvas file are discouraged.
6. After an agent write, `set_canvas` republishes and refreshes the Cockpit.

Slice-2 content `PUT` accepts bounded valid UTF-8 only for an editable
Markdown/text/code/LaTeX or strict-static-HTML source on a writable backend;
binary/image writes and arbitrary renderer changes are not part of this route.
The post-write bytes must still satisfy the selected renderer's validation. A
successful save returns `200` with the new authorized `CanvasState` and content
URL, the complete-state `ETag`, and
`X-Canvas-Content-ETag: "sha256:..."` matching `source_version` in the body.
`refresh` uses the same success envelope. Failures never partially advance the
Canvas row.

`WorkspaceBackend` currently has no conditional-write primitive, SFTP writes are
unconditional, and virtual writes may be read-modify-write. The check/write
sequence above closes common stale-save cases but cannot stop a shell command or
another process from writing outside the coordinator between checks. The UI and
docs must not call this race-free. If strict guarantees become required, extend
all production backends and agent file tools with a shared atomic CAS contract
before changing the claim; otherwise revisit CRDTs only after real simultaneous
editing demand appears.

Selection-to-message metadata is deferred. If later implemented, it may attach
`{canvas_id, path, source_version, range, selected_text}` to the next chat
message, but the agent must still reload the file: selection metadata is a
locator, not content authority. Live-app form results are separate: an
untrusted iframe can propose a result through the Canvas bridge, but Cockpit
must show a trusted “Send to agent” confirmation before it becomes user input.

## Security Model

Agent-authored files and applications are untrusted content. In-cockpit
placement is a UX choice, not a trust boundary.

### Logical-source validation and SSRF prevention

- Never accept a complete URL or arbitrary hostname in a workspace source.
- V1 uses thread-owner authorization through `require_thread_owner`, including
  that helper's existing unscoped-admin bypass, rather than broader project
  visibility. Apply it to every state, content, viewer-session bootstrap, and
  proxy request—not only pointer creation.
- Cockpit state/content mutations retain the existing non-safe-method CSRF
  guard (`X-CSRF`) and reject an unexpected browser Origin. The public viewer
  locator grants nothing; its BFF-authorized browser-bound exchange is
  single-use and does not become a general CSRF bypass.
- Resolve paths, SSH targets, browser generations, workspace generations, and
  ports from authorized thread state. A live-app upstream is always
  `127.0.0.1:<declared-port>` reached through that workspace's SSH connection;
  route data cannot express cloud metadata, another workspace, or a service
  network.
- Revalidate current thread authorization, workspace generation, origin
  generation, route, and origin session on each request. Never keep routing to a recycled workspace
  IP or old SSH target.
- Disable AsyncSSH's default-user `known_hosts` fallback explicitly and accept
  only the provisioner-recorded Ed25519 SHA-256 fingerprint. An unrelated
  matching entry in the orchestrator user's home directory is never authority
  for a Canvas generation.
- Do not expose pod IPs, internal DNS names, internal keys, or proxy credentials
  to the model or browser.
- Canonicalize file paths on the target and reject a symlink in **any** path
  component for both viewing and editing; lexical `..` rejection alone is not
  sufficient.
- Accept regular files only—never directories, devices, FIFOs, or sockets.
- Be explicit about the residual: SFTP `lstat`/`realpath` followed by pathname
  open is check-then-use, so an authorized user's concurrent workspace process
  can swap a component. The thread-authorized v1 surface accepts that limitation
  and does not call it hard containment. Before multi-user/public artifact access, add a
  root-owned workspace helper using a race-resistant primitive such as Linux
  `openat2(RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS)`.
- Detect MIME from bytes, enforce renderer/type/size compatibility, and send
  `nosniff`. Never serve active HTML/SVG as a navigable document from the
  trusted Cockpit/API origin.

### Static HTML files

The first static HTML renderer is intentionally strict: sanitize the document,
remove scripts/event handlers/forms/base/navigation URLs and external resource
URLs, and render it in an opaque-origin iframe. Declare a direct sanitizer
dependency rather than relying on DOMPurify's transitive presence through
Monaco. Treat `srcdoc` as an injection sink and use a narrowly scoped Trusted
Types policy/property assignment after sanitization.

```html
<iframe
  sandbox=""
  title="<trusted Canvas title>"
  referrerpolicy="no-referrer"
  loading="lazy"
  srcdoc="...leading Cockpit CSP followed by sanitized agent HTML...">
</iframe>
```

Baseline CSP:

```text
default-src 'none';
script-src 'none';
style-src 'unsafe-inline';
img-src 'none';
font-src 'none';
connect-src 'none';
form-action 'none';
base-uri 'none';
object-src 'none';
```

The meta CSP is prepended before agent markup, but it is not confused with a
response header: CSP meta cannot express `frame-ancestors` or `sandbox` and only
applies after the element is parsed. Sanitization plus the iframe `sandbox` are
the primary controls. Interactive self-contained JavaScript is not a second
static mode in v1; present it as a live workspace app on the untrusted origin.

### Live workspace applications

Live applications need same-application network access and therefore use a
real isolated canvas origin, separate from the Cockpit's registrable domain:

```text
cockpit.example.com
<random-origin-generation>.example-userland.com
```

This separation is mandatory. The production BFF cookie can be scoped to the
platform parent domain, so a sibling such as `canvas.example.com` could receive
trusted session material. Canvas needs separate wildcard DNS, TLS, ingress, and
an independently registrable user-content domain.

For production, `hostSuffix` equals `.` plus that dedicated domain, and the
domain's exact name is the attested effective private-PSL rule. This removes an
otherwise dangerous ambiguity where an operator might attest the registered
root while placing generated hosts below a deeper shared registrable label.
Development-only cookie-free deployments may still use a nested suffix.

For the hosted SRW deployment, the reserved user-content domain is
`srwcanvas.works`. Origins use exactly one generated label, making the private
PSL boundary and hostname-validation invariant exact:

```text
https://<origin-generation>.srwcanvas.works
DNS/TLS host: *.srwcanvas.works
effective private PSL rule: srwcanvas.works
```

The Fleet overlay records this domain, `.srwcanvas.works` host suffix, and
canonical `https://cockpit.srw.works` embedding origin. Both Canvas gates and
the PSL attestation remain false; the NetworkPolicy edge selectors name the
existing Cloudflare Tunnel connector (cloudflared) as the only pod allowed to
reach the gateway. The domain choice is therefore inert configuration, not a
launch claim.

Do not reuse a stable origin for unrelated apps. Cookies, local storage, cache,
and Service Workers survive within an origin and an old app could affect its
replacement. Use a UUIDv4 (122 random bits) or stronger opaque origin
identifier, rotate according to the state rules above, revoke old sessions, and
send `Clear-Site-Data` only as best-effort cleanup. Default proxy CSP includes
`worker-src 'none'`.

### Embedded viewer authentication

A normal cookie on the separate domain is third-party inside Cockpit. The
selected flow is:

1. The authorized Cockpit parent calls a same-origin API to register one pending
   non-credential view attachment. That call requires the exact CORS Fetch
   Metadata tuple in addition to BFF/CSRF authorization; missing or unenforceable
   metadata returns `canvas_browser_unsupported` before creating state. The
   attachment is bound to user, thread, `main`, the exact
   parent `srw_session`, normalized source fingerprint, workspace generation,
   origin generation, embedding origin, cookie mode, and expected presentation
   revision. The BFF response contains a random attachment ID, an exact
   `/_canvas/bootstrap?attachment_id=<uuid>` URL, and a one-use bridge nonce.
   The nonce remains only in trusted parent memory; the URL is a public locator,
   not a bearer credential.
2. Cockpit binds the exact iframe `WindowProxy` before mounting that URL. Before
   starting a challenge, the Canvas gateway requires a cross-site browser iframe
   navigation (`Sec-Fetch-Dest: iframe`, navigation mode, and the expected Fetch
   Metadata relationship), exact raw path/query/UUID host, bounded headers, and
   no body. It applies `frame-ancestors` for configured Cockpit origins and
   returns `Cache-Control: private, no-store` plus `Referrer-Policy: no-referrer`.
   Top-level/document navigation is rejected before changing handshake state.
3. The gateway creates a one-time challenge, ready receipt, and browser binding,
   persists only their purpose-separated hashes, and sets an attachment-specific
   transient cookie:

   ```text
   __Host-canvas_bootstrap_<attachment-uuid>=...; Secure; HttpOnly;
   SameSite=None; Partitioned; Path=/
   ```

   Its minimal server-owned document posts the versioned challenge and receipt
   only to the exact stored Cockpit origin. It contains neither the bridge nonce
   nor browser-binding value.
4. Cockpit accepts that message only from the bound `WindowProxy`, exact Canvas
   origin, expected attachment, and closed protocol schema. It sends the
   challenge, receipt, and in-memory bridge nonce to the BFF authorize endpoint.
   The BFF requires the exact user/thread/parent `srw_session` and Cockpit
   `Origin`, revalidates current Canvas/workspace/origin/policy identity, and
   returns one short-lived exchange code. A copied locator—including one opened
   by the same user under a different BFF session—cannot authorize this step.
5. Cockpit verifies every BFF echo, then posts the exchange code back only to the
   bound iframe and exact Canvas origin. The gateway document accepts it only
   from `parent` at the stored Cockpit origin, then performs a same-origin JSON
   `POST /_canvas/exchange`. That endpoint requires exact Fetch Metadata,
   `Origin`, framing, schema, challenge, exchange-code hash, UUID host, and the
   transient browser cookie. PostgreSQL atomically admits one exchange across
   replicas. It clears the transient cookie and either creates a short-lived
   **origin session** or reuses a still-valid same-parent/same-source session,
   setting/reissuing only the reserved viewer cookie:

   ```text
   __Host-canvas_session=...; Secure; HttpOnly; SameSite=None;
   Partitioned; Path=/
   ```

   The document posts an exact challenge/receipt `ready` message before calling
   `location.replace()` with the canonical entry path. Cockpit does not treat an
   iframe `load` event as authenticated readiness. The next navigation is
   initiated by the allocated Canvas origin and carries
   `Sec-Fetch-Site: same-origin`; no secret ever appeared in the URL.

   If the attachment-specific transient cookie is absent at exchange, the
   gateway returns `canvas_browser_storage_unavailable`. Only that exact typed
   storage failure, or the parent Fetch Metadata failure above, produces trusted
   unsupported-browser/privacy copy; other exchange and service failures remain
   generic secure-preview-unavailable errors.

   Record the presentation revision at issuance for audit, but authorize
   requests by the still-current origin/source/workspace identity: an unchanged
   same-app health refresh may advance presentation revision without killing
   mounted frames.
6. Every ordinary app request requires `Sec-Fetch-Site: same-origin` before
   cookie authentication. A browser which ignores the unknown `Partitioned`
   attribute might store the viewer cookie unpartitioned, but an attacker-site
   image/fetch/iframe request is still rejected before it can reach an app with
   a state-changing GET. Only the one-time cross-site iframe bootstrap is
   admitted; browsers without enforceable Fetch Metadata are unsupported.
7. The fixed cookie identifies the shared origin session, not one tab. The main
   pane, another Cockpit tab, and the authenticated wrapper pop-out in the same
   cookie partition reuse it; their attachment IDs/nonces drive UI presence
   only. Closing one attachment never revokes or overwrites the session for the
   others.
8. The Cockpit-wrapper pop-out stays embedded and uses this same flow. A target
   browser which cannot support it receives trusted unsupported guidance for the
   authenticated IDE/manual-preview workflow; it never receives a top-level app
   fallback, relaxed sandbox, or bearer token in asset URLs.

Logout, thread deletion, clear/replacement, workspace/origin generation change,
and thread-owner/admin authorization revocation invalidate matching origin
sessions and actively close their current ordinary-HTTP/direct-channel
consumers. Slice 4 must extend the same guarantee to SSE and WebSockets.
Expiry alone is not the logout mechanism. The Canvas cookie authorizes only
that origin session's viewer/proxy
requests; it cannot create/renew sessions, mutate Canvas state, or call Cockpit
APIs. Those actions still require the authenticated parent and its CSRF
protection.

Session enforcement is cluster-wide rather than a process-local cleanup hint:

- Every HTTP request resolves the hashed cookie to a non-expired session and
  rechecks the current thread authorization, source fingerprint, workspace
  generation, and origin generation before it opens an upstream channel.
- Each replica keeps a `CanvasConnectionRegistry` keyed by origin-session ID for
  in-flight ordinary HTTP exchanges and direct SSH channels. Its contract is
  intentionally extensible to future SSE/WebSocket consumers. Revocation
  commits update PostgreSQL first and then publish an opaque session or
  generation ID through PostgreSQL `NOTIFY`; every listening replica cancels
  matching current upstream and downstream I/O. NATS may accelerate the same
  signal but is not required for correctness.
- Notifications are not the safety boundary because a replica can miss one.
  Start a cancellation/revalidation guard as soon as an authenticated exchange
  begins—before reading a request body—and keep it through slow upload,
  upstream connect/header wait, response streaming, and teardown. Any current
  HTTP exchange still active at the interval revalidates its session and Canvas
  generations against PostgreSQL at most every 15 seconds and at its current
  expiry deadline. Renewal updates the shared expiry and notifies listeners;
  revocation, authorization loss, generation mismatch, or final expiry closes
  both directions with no further upstream reads. Thus a missed notification
  has a bounded revocation window rather than an indefinitely valid upload,
  header wait, or download. Slice 4 must apply the same guard continuously to
  SSE/WebSocket streams.
- View attachments are presence/bridge records, not independent network
  credentials. Closing one attachment cancels connections naturally owned by
  that frame/window but does not revoke a shared origin session used by another
  tab. Clear/replacement/logout and explicit source-wide revocation cancel the
  session everywhere.
- The authenticated SSH transport may be pooled by workspace generation, but a
  checkout revalidates that generation and its pinned host fingerprint. Idle
  transports have a bounded 60-second default TTL; every direct channel remains
  registered to and cancellable through its origin session.

The local production-build harness defines this flow, including the wrapper,
for Playwright Chromium, Firefox, and WebKit against a synthetic intercepted
gateway. Chromium and Firefox pass on the Fedora host; WebKit passes through
the matching official Playwright Ubuntu Noble container. Before the live-app
viewer is enabled, the same boundary
still requires hosted/deployed-gateway acceptance plus current desktop Safari,
Safari/iOS, installed PWA, and secure-context CHIPS coverage. Multi-tenant
production also requires the
Canvas tenant label to be an effective cookie boundary (normally a propagated
private Public Suffix List entry). The current proxy forwards no application
cookies, but hostile JavaScript can still set parent-`Domain` cookies with
`document.cookie` and poison sibling cookie jars. PSL isolation is therefore a
production boundary, not a claim that app-cookie forwarding has shipped. Plan
submission/propagation early.

Both implemented deployment modes are currently application-cookie-free: the
gateway drops all upstream `Set-Cookie` and forwards no application cookies
(the reserved host-only viewer cookie still terminates at the gateway). The
current validator allows a `development` deployment to select either
`development-cookie-free` or `psl-isolated` without a PSL attestation. Without
an effective boundary, either selection has only the explicitly insecure
local/single-user development posture; the `psl-isolated` label alone does not
establish cross-app cookie isolation. Prefer `development-cookie-free` there so
that limitation is visible. Hostile JavaScript can still set parent-domain
cookies and exhaust/poison sibling Canvas cookie jars. Production therefore
accepts only `psl-isolated` after the effective PSL or an equivalent enforceable
per-app registrable-domain boundary exists. Application-cookie
forwarding/rewriting is deferred and requires its own threat review.

### Live-app iframe and network policy

Cockpit binds only a server-generated URL and uses fixed attributes:

```html
<iframe
  sandbox="allow-scripts allow-same-origin allow-forms"
  title="<trusted Canvas title>"
  referrerpolicy="no-referrer"
  allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'">
</iframe>
```

The separate registrable origin is necessary for `allow-scripts` plus
`allow-same-origin`, but it is not sufficient by itself: a hostile app can
self-navigate its frame. Every trusted Cockpit/BFF document must deny framing
with a response `frame-ancestors 'none'` policy (and compatible legacy header),
or the renderer must interpose a cross-origin wrapper, so navigation onto the
trusted parent origin cannot turn the frame same-origin with Cockpit. This is a
production launch gate, not something the warning substitutes for. Top
navigation, popups, downloads, modals, pointer lock, fullscreen, and powerful
device capabilities remain absent until a future explicit grant model exists.

The proxy adds a non-relaxable response-header CSP. A practical development
baseline allows same-origin scripts/styles/images/fonts and same-origin
HTTP/WebSocket connections (including the minimum inline/eval/blob allowances
actually required by the supported dev server), while enforcing:

```text
default-src 'self' data: blob:;
connect-src 'self' <generated same-origin wss URL>;
frame-src 'none'; object-src 'none'; worker-src 'none';
base-uri 'self'; form-action 'self';
frame-ancestors <configured Cockpit origins>;
```

The current CSP reserves the exact same-origin WebSocket destination so the
policy need not be weakened later, but the Slice-3 proxy still rejects every
upgrade. That directive is not a claim that HMR/WebSocket transport is usable.

Every live response also gets proxy-owned `Referrer-Policy: no-referrer`,
`X-Content-Type-Options: nosniff`, and a deny-by-default Permissions Policy.
The gateway does not add wildcard CORS; parent collaboration uses the audited
bridge, while the app's own browser requests remain same-origin.

Exact directives are integration-tested and tightened. V1 strips upstream
security-policy headers rather than attempting to merge two policies; the
gateway-owned policy is the only response-header policy. An application may
add a stricter in-document meta policy where the platform supports it, but that
is not part of the security boundary. External CDN/assets and API calls are
blocked by default, so prototypes vendor dependencies or route through declared
workspace services. A script can still navigate its own iframe to an external
page—CSP `connect-src` is not a complete information-flow guarantee, and browser
APIs such as WebRTC need explicit adversarial testing. Navigation to a trusted
platform origin must fail at that origin's anti-framing response boundary as
described above. The parent cannot reliably distinguish every self-navigation
using SOP alone; an optional bridge heartbeat is only a heuristic, never
authorization. Live content therefore retains an untrusted/“do not enter
secrets” warning and the document does not market the default CSP as a complete
no-egress sandbox.

### Proxy invariants

The existing IDE proxy is a reference for workspace resolution, not a base that
is already ready for untrusted streaming apps: it currently buffers upstream
HTTP, has a finite read timeout, collapses repeated response headers, and lacks
complete WebSocket/redirect/cookie semantics.

The canonical-path algorithm above runs before route selection. The request
header boundary is also normative:

| Incoming field | Upstream behavior |
|---|---|
| `Host` / HTTP/2 `:authority` | Require the exact allocated Canvas hostname. Send that same public host (and non-default port, if any) upstream so absolute URLs and development-server host checks describe the browser-visible origin. Never send an internal IP or SSH destination. |
| `Origin` | If present, require the exact public Canvas origin and forward its canonical value. Require it for unsafe browser methods and, once Slice 4 exists, WebSocket handshakes; reject cross-origin values. An absent `Origin` is accepted only for ordinary `GET`/`HEAD`. `Sec-Fetch-*` is a defense-in-depth signal, not authorization. |
| `Forwarded` / `X-Forwarded-*` | Drop all inbound values. Emit `Forwarded: proto=https;host="<public-host>"` plus gateway-owned `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Port`. Omit `for`/`X-Forwarded-For`; strip `X-Real-IP` so workspace code never receives the viewer IP. |
| `Cookie` | Consume and remove `__Host-canvas_session`; reject malformed or duplicate reserved cookies. Forward no remaining application cookies in either current deployment mode. Never forward Cockpit/BFF/Keycloak cookie namespaces. Application-cookie support is deferred. |
| `Authorization` / `Proxy-Authorization` | Strip both in v1. Viewer authentication uses only the reserved gateway cookie, and no ingress/auth middleware may inject platform credentials into the upstream request. A prototype may use an explicitly application-owned non-reserved custom header; manifest-gated application authorization requires a separate threat review. |
| Hop-by-hop and ingress-private fields | Remove standard hop-by-hop fields and every header named by `Connection`. Strip internal routing/auth fields such as `X-Original-URL`, `X-Rewrite-URL`, `X-Forwarded-Client-Cert`, and deployment-specific identity headers. Rebuild only the fields explicitly listed here. |

HTTP framing has one parser/encoder boundary. The edge server rejects multiple
or conflicting `Content-Length`, `Content-Length` plus `Transfer-Encoding`,
unsupported transfer codings, invalid chunks, and oversized header/trailer
blocks before the Canvas handler. The handler never forwards client framing:
it strips `Content-Length`, `Transfer-Encoding`, `Trailer`, `TE`, and `Expect`,
decodes/spools the bounded request body, and synthesizes a single
`Content-Length` (or an explicit no-body request) upstream. Inbound chunked
requests are supported through this decode-and-reframe path, not by copying raw
chunks. Any disk spill is private (`0600`), unlogged, and removed on success,
error, cancellation, or revocation.

The response parser likewise rejects conflicting length/transfer metadata,
invalid chunk syntax, forbidden trailers, and every protocol switch, including
upstream `101`. It strips upstream framing and lets the trusted downstream
server encode one framing mode. V1 sends `Connection: close` upstream and uses
one HTTP request per SSH direct channel; the SSH transport, not an application
TCP connection, is what gets pooled. Any parser/framing error closes the
channel without reuse. Repository tests cover this strict adapter against
framing differentials. The public raw-path differential probe applies only to
an edge that claims byte-exact path fidelity (L4/SNI passthrough); the
selected Cloudflare-tunnel edge normalizes adjacent slashes upstream of the
gateway by design and is verified without that probe
(`docs/issues/canvas_hosted_edge_use_cloudflare_tunnel.md`).

Queries are parsed independently of the path, preserve duplicate parameters,
ordering, and blank values, and are re-encoded without logging their raw values.
Every reserved Canvas bootstrap/control key is removed before upstream. The
proxy accepts `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, and `OPTIONS`;
every other method, including `CONNECT` and `TRACE`, is rejected.

The response boundary is fail-closed:

- Remove hop-by-hop headers. The gateway replaces upstream
  `Content-Security-Policy`, `Content-Security-Policy-Report-Only`,
  `Permissions-Policy`, `Referrer-Policy`, `X-Frame-Options`,
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Embedder-Policy`, and related
  framing/security fields with its configured policy; it does not try to merge
  CSP reporting directives. V1 emits no `X-Frame-Options` because the precise
  Cockpit allowlist is enforced by CSP `frame-ancestors`.
- Strip upstream `Clear-Site-Data`, `Refresh`, `WWW-Authenticate`,
  `Proxy-Authenticate`, `Report-To`, `Reporting-Endpoints`, and `NEL`. Only a
  gateway-owned origin-retirement response may emit `Clear-Site-Data`.
- Strip all upstream `Access-Control-*` fields and do not synthesize CORS. The
  app-facing browser contract is same-origin; an `OPTIONS` response alone does
  not grant another web origin access.
- Live response caching is gateway-owned. V1 strips upstream `Cache-Control`,
  `Expires`, `Pragma`, `Age`, `ETag`, and `Last-Modified` and emits
  `Cache-Control: private, no-store` on HTML, assets, and API responses. An
  expired/revoked viewer must contact the gateway rather than replay
  authenticated app data from HTTP cache. More permissive immutable-asset
  caching requires generation/session-safe URLs and a separate design.
- Drop every upstream `Set-Cookie` field in both current deployment modes.
  Preserving and safely rewriting application cookies is deferred and must not
  be inferred from selection of the `psl-isolated` profile.

The gateway never follows a redirect. It handles `Location` as follows:

1. Reject control bytes, user information, backslashes, scheme-relative `//`
   references, reserved `/_canvas` targets, and values over a small configured
   limit. Resolve a path-relative, root-relative, query-only, or fragment-only
   reference against the current public URL, then run its path through the same
   canonical algorithm. An absolute URL is safe only when its scheme, host, and
   effective port exactly equal the public Canvas origin.
2. An absolute `http://127.0.0.1:<port>/...` or
   `http://localhost:<port>/...` URL may be rewritten to the public origin only
   when `<port>` is the exact current Slice-3 entry port. Slice 4 may extend this
   to a declared supporting port only when the canonical returned path, passed
   through normal longest-prefix route selection, resolves back to that port.
   Replace only the authority; never infer/prepend a prefix. Under that future
   route table, a backend behind `/api` must emit an `/api/...` redirect, while
   an entry-port `/login` remains valid. No other hostname is treated as
   workspace loopback.
3. Return the normalized public `Location` for those two safe cases. For an
   external, undeclared-port, ambiguous, or invalid target, remove `Location`
   and return a typed `409 canvas_navigation_blocked`; the iframe never
   auto-follows it. The current Cockpit has no external-navigation confirmation
   flow. A future opaque trusted-parent confirmation may be designed for
   well-formed external HTTPS targets, but unsafe/custom schemes, plain HTTP,
   malformed URLs, and targets with credentials remain non-confirmable.

**Planned Slice-4 WebSocket contract:** require an exact public `Origin`,
validate version/key and a bounded offered-subprotocol list, and rebuild
`Connection: Upgrade`/`Upgrade: websocket` instead of forwarding raw connection
fields. The selected upstream subprotocol must be one the client offered. It
must strip `Sec-WebSocket-Extensions` in both directions and disable
`permessage-deflate`;
later compression support needs decompression-ratio and memory accounting. The
future proxy preserves text/binary type and close code/reason, bounds individual
frames and complete reassembled messages, rate-limits messages, applies
bidirectional backpressure, and closes both peers on cancellation or policy
failure.

Request bodies are decoded into a bounded private spool with cancellation and
backpressure before reframing; HTTP responses stream without whole-body
buffering. Range/HEAD semantics are implemented rather than simulated after
buffering. Current limits cover headers, query, body, response bytes, HTTP
concurrency, connection admission, timeouts, and origin-session duration. Key
every open direct channel/cache entry by workspace and origin generation and
register it to the origin session. The underlying authenticated SSH transport
may be pooled per workspace generation, but every channel is cancellable
independently on replacement, suspension, clear, or revocation. Slice 4 adds
SSE heartbeat/idle handling, WebSocket backpressure, bandwidth limits, and
stream-specific admission.

Implemented Slice-3B deployment defaults are:

| Limit | Default |
|---|---:|
| Active connections per gateway replica | 256 |
| Request or response header block | 64 KiB and 100 fields |
| Query | 16 KiB and 256 fields |
| Request body | 10 MiB |
| Ordinary response bytes | 100 MiB |
| Concurrent HTTP upstreams per origin session, per gateway replica | 16 |
| Origin session | 15 minutes, renewable by the authenticated parent |
| Attachment / bootstrap | 20 minutes / 60 seconds |
| Active-exchange database revalidation | at most 15 seconds |
| Ordinary upstream connect/idle timeout | 5 s / 60 s |
| Request-body spool / upstream HTTP exchange | 300 s each |

Slice-4 design targets—not current Helm settings—include at most eight routes,
four concurrent SSE/WebSocket streams per origin session, a 20 MiB/s bounded
bandwidth policy, 1 MiB frames, 4 MiB reassembled messages with compression
disabled, and 100 messages/s with a bounded burst of 200. SSE/WS will not use
the ordinary response-byte/idle limit; they require stream counts,
per-frame/rate limits, heartbeat policy, renewable sessions, and hard
disconnect on revocation. Never buffer up to a limit before forwarding.

The current one-port service is one same-origin trust unit. When Slice 4 adds a
route table, every declared service in it belongs to that same unit. Browser
code uses relative URLs. The gateway forwards the allocated public Canvas host,
but the agent starts the server before that random host exists. The workspace
therefore receives a non-secret deployment setting such as
`SRW_CANVAS_HOST_SUFFIX=.example-userland.com`. Vite may allow that exact
deployment-controlled suffix (its leading-dot subdomain syntax) and use a
strict port. Once Slice 4 lands it may also proxy HMR WebSockets and derive the
HMR host from the page. It must never use `allowedHosts: true`, a generic public
suffix, or a browser-direct workspace connection. The Canvas gateway still
accepts only the exact allocated hostname, so the broader development-server
suffix does not select another thread's upstream. Frameworks which cannot
safely allow the fixed suffix need a future prepare-origin/restart flow; v1 does
not weaken host validation for them.

### Optional Canvas application bridge (planned Slice 4)

The current `postMessage` surface is only the gateway-owned authentication
bootstrap protocol. A later application bridge may let live apps opt into a
small versioned protocol for `ready`, intrinsic resize, and a proposed form
result. The parent must validate exact
`event.source`, exact origin, a per-attachment nonce, message type/schema, size, and
rate, and send with an exact `targetOrigin`. Unknown messages fail closed and
the bridge needs a teardown handshake. Agent-authored data is never silently
promoted to a trusted user message; Cockpit confirms it in host chrome.

### Browser source (container adapter implemented)

- The backend reuses the shared-browser authorization and ownership protocol.
- Canvas consumes a stable stream/control adapter; it never exposes the CDP
  debugging endpoint or makes experimental CDP screencast details part of its
  public source contract. The Cockpit adapter and capability-scoped agent tool
  integration are implemented by Plan 2.
- The daemon uses its dedicated browser profile and a concrete generation. A
  user takeover obtains the daemon-owned single-controller baton and pauses
  mutating agent actions; all transitions are serialized with browser actions.
- Native dialogs, downloads, and credential boundaries follow the browser
  feature's policy rather than being redefined by Canvas.

## Lifecycle and Failure States

Different source types have different durability:

- **Workspace file:** remains viewable only while the new gateway can read that
  backend. A live remote workspace becoming suspended is `unavailable`; a shared
  durable virtual object store may remain readable; process-local memory does
  not magically become orchestrator-visible. Out-of-band byte changes are
  `source_changed` until explicitly adopted/republished.
- **Workspace port (implemented, dark) / app manifest (planned):** presentation
  pointer persists, process does not. When the workspace sleeps/restarts,
  revoke direct channels/origin sessions and show `unavailable`.
  Never follow the same port into a new workspace generation automatically; the
  agent restarts the services and republishes.
- **Browser (implemented behind its gate):** pointer persists, browser session
  may not.
  Availability follows the browser broker/runtime capability, not the workspace
  storage backend alone. Show a clear ended or reconnect state; never silently
  select a different browser.

Validate source capability rather than assuming it from a backend name:

| Source | Required runtime capability |
|---|---|
| `workspace_file` | readable thread workspace; writable for edit mode |
| `workspace_port` | active SSH-reachable, shell-capable workspace and permitted loopback port; implemented behind Slice-3 gates |
| `workspace_app` | the same plus a validated route manifest; planned Slice 4 |
| `browser` | active authorized browser generation, stream broker, and positive agent/Cockpit shared-browser capability |

A durable virtual workspace can display files but cannot host a new server
process until the existing user-approved workspace upgrade completes. A
process-local virtual backend may be unavailable to the gateway. A no-workspace
session cannot use file/app sources; browser is exposed only if a real broker
capability exists. `set_canvas` returns a typed capability error and never
initiates an implicit upgrade.

Common statuses:

```text
ready | starting | source_changed | unavailable | ended | error | cleared
```

`unauthorized` and `invalid_source` are request errors, not durable states. The
Cockpit may preserve the last useful file visual for a transient disconnect,
overlaying status rather than replacing it with a spinner. It must tear down
live frames immediately on thread-authorization loss, origin-session
revocation, thread switch, workspace generation change, or clear.

## Reusable Infrastructure and Net-New Work

### Reuse

| Concern | Existing substrate |
|---|---|
| Thread auth and context | persistent-session token + `ToolContext` |
| Durable state/migrations | orchestrator PostgreSQL migration/service patterns |
| Live UI notification | `_broadcast` → `thread_events` → SSE vocabulary, after the ordered-writer fix |
| Workspace targeting | thread-upload SSH resolution + agent `WorkspaceBackend` behavior as a contract reference |
| HTTP/WebSocket forwarding | IDE proxy workspace-resolution patterns only, not its buffering/auth implementation |
| Browser stream/control | Implemented Plan-1 daemon/broker plus Plan-2 agent/Cockpit handoff from `shared_browser.md` |
| Chat/canvas split | `ChatPageComponent` + installed `angular-split` |
| Markdown rendering | installed Marked/ngx-markdown + math extension, with a separate Canvas parser |
| Code editing | installed Monaco + loader patterns, after extracting lifecycle-safe shared loading |
| Agent guidance | bundled Agent Skills + model-invoked `use_skill` |
| Shared product semantics | proposed shared application action layer |

### Net-new

- Canvas presentation table and orchestrator service/action.
- Typed `get_canvas`, `set_canvas`, and `clear_canvas` tools.
- Ordered persistent-event writer, Canvas callback, event handling, and a shared
  Cockpit thread-transport seam.
- Thread-owner/admin-authorized `ThreadWorkspaceFileGateway` for remote and
  shared-virtual persistent-thread files, including MIME/hash/Range/limits.
- `CanvasPaneComponent`, renderer registry, file renderer adapters, and service.
- Best-effort conditional file writes, refresh, conflict UX, and editing
  awareness in Slice 2.
- Per-app origin generations plus a partitioned viewer-session bootstrap and
  dedicated isolated gateway in Slice 3B. The separately registrable
  user-content domain and wildcard DNS/TLS edge remain operator-owned launch
  prerequisites and are intentionally not created by the chart.
- Shared pinned AsyncSSH transport and bounded one-port readiness in Slice 3A;
  request-scoped, revocation-aware, strict ordinary-HTTP forwarding in Slice
  3B. The manifest route table and streaming SSE/WebSocket multiplexer remain.
- Canvas skill and capability-aware scope.
- Browser renderer adapter over the implemented shared-browser stream
  (delivered in Slice 5).

## Implementation Slices

### Slice 0 — State and event foundation

**Implementation status:** Complete as of 2026-07-13. This includes migration
`0058`, the generated current-schema artifact, atomic Canvas state actions,
owner-protected public state/conditional-clear routes, ordered persistent-event
writing with fail-closed per-runtime epochs and direct reconciliation on
terminal Canvas failures, and the typed Cockpit transport/state bridge.
Source-setting HTTP adapters, model tools, and the companion skill were
intentionally assigned to Slice 1 and are now implemented below.

The first local rollout exposed an immutable-history violation: `0058` had
already been applied with SHA-256
`e04eb6a4e27a120ec86682226b3cfa9c6abeeeb64d53b9781a45ae83ef11cff5`,
but its repository copy was later edited. The repair restores those exact bytes,
pins the checksum in the Canvas migration test, excludes the immutable file from
new Squawk annotations, and moves the later `threads.events_epoch` comment into
forward-only migration `0060`. The database migration ledger was not edited.

- application migration `0058` and `CanvasService`
  with `require_thread_owner`-protected `main` routes and atomic revision
  semantics;
- fix `_broadcast` persistence with one ordered event writer;
- expose `{method, params}` Canvas invalidations through a small shared Cockpit
  typed bridge over the existing transport owner;
- unit-test state transitions and missed/duplicate-event REST reconciliation.

This was a narrow platform prerequisite PR. It did not claim the feature was
useful until the file stage landed, and it did not register model tools with no
usable source kind.

### Slice 1 — View-only file-backed shared stage

**Implementation status:** Implemented as of 2026-07-13; the primary
write/present/render path is live-verified on local k3d. The conditional-content
`304` framing defect found by the first pass was fixed at the start of Slice 2
and reverified against the real ASGI server. The secure VM identity adapter and
cross-tenant static-Docker recreation controller are explicit gates, not hidden
partial support.

The complete control loop uses one `main` canvas per persistent thread:

- mint/persist `workspace_generation` for durable virtual storage and trusted
  Kubernetes/Docker lifecycle adapters; fail closed for unattested VM/direct
  remote and process-local backends;
- `ThreadWorkspaceFileGateway` for remote and shared durable virtual backends,
  including provisioner-captured host fingerprints, the pinned async SSH/SFTP
  pool, canonical path, MIME, hash, GET/HEAD/Range, limits, and suspended state
  behavior;
- add the `canvas` tool category, file-only flat get/set/clear adapters,
  internal get/set/clear routes with mandatory internal + delegated-user headers,
  delegated-user orchestrator client pattern, and post-commit
  `canvas_event_callback`;
- `ChatPageComponent` split with a Canvas pane;
- dedicated Markdown/math, read-only text/code, raster image, and strict
  script-free static HTML renderers;
- versioned `ngsw-bypass` content URLs, source-changed handling, and same-pointer
  refresh behavior;
- chat `set_canvas` tool card, desktop/mobile behavior, accessibility contract,
  and translated English/German copy;
- concise `present-with-canvas` skill, model-invoked and scoped only with tools.

Static Docker hosts listen on container port `30022`; Compose passes the exact
private-key path to the orchestrator and keys every fingerprint inventory entry
by the endpoint the orchestrator actually uses. Migration `0059` makes the
endpoint-keyed `docker_workspace_leases` table the durable occupancy authority;
job/thread JSONB is only an atomic mirror. Allocation is one
PostgreSQL-advisory-lock transaction across both owner kinds, and exact endpoint,
owner, lease, status, lifecycle edge, trust provenance, and fingerprint checks
guard every transition. Quarantine survives owner deletion and blocks stale
finalizers and later allocation.

First discovery is unavailable by default. A production bootstrap may import an
endpoint as attested only after both its container and persistent workspace data
were independently recreated or sanitized and an exact host-key fingerprint was
captured. That bootstrap is one-time: configuration cannot promote an existing
quarantine and must be removed after the clean import. `ready -> releasing ->
released|quarantined` is the only reusable cleanup path; default production
release quarantines because SSH deletion is not a tenant reset. Only explicit
same-trust, single-user development reuse may return an unrecreated static host
to the pool.

This slice delivers the central promise: the agent writes a file and puts it in
front of the user without an IDE detour.

Final repository validation replayed all application migrations into fresh
PostgreSQL and regenerated/checked `schema_current.sql`; passed 199 focused
Canvas, lifecycle, and configuration tests with 4 environment skips, the full
1,007-test Cockpit suite, production Cockpit build, i18n checks, changed-Canvas
style checks, Ruff, Squawk, Helm overlays, Docker Compose rendering, workspace
shell syntax, and companion-skill validation. Repository-wide Stylelint still
has unrelated baseline findings and is not claimed clean by this slice.

Local k3d acceptance on 2026-07-13 exercised the original user flow on thread
`5432783a…`. After both orchestrator replicas converged on one current image, the
legacy durable-virtual thread was rebound to a current agent. Its backend
reported `supports_canvas=True`; it loaded `get_canvas`, `set_canvas`, and
`clear_canvas`, and deployed the `present-with-canvas` skill. The authenticated
internal adapter presented the existing `test.md` as **Test Document**, the
Cockpit rendered it, and the authorized gateway bytes hashed to the stored
`source_version`. The user confirmed the pane worked.

That pass also established an operational constraint: Canvas tool/skill scope is
resolved when an agent process is set up, so an already-running stale agent does
not hot-load a new registry and must be recreated/re-attached. Slice 2 removed
the representation `Content-Length` from bodyless `304` responses; a live
conditional request now returns a clean `304` with no Uvicorn framing error. The
exact evidence lives in [[dynamic_canvas_slice1_verification]]. The remaining
live-app real-browser matrix belongs to Slice 3 and is not claimed by this
file-only slice.

### Slice 2 — Bidirectional editing

**Implementation status:** Implemented as of 2026-07-13. The conditional-edit
backend and cross-replica coordinator are live-verified on local k3d. Cockpit's
editor/conflict behavior is covered by component and service tests and a
production build; a user-driven browser acceptance pass of the Monaco workflow
is still useful follow-up evidence, not an implementation blocker.

- Editable Markdown/text/code/LaTeX source.
- Shared Monaco loader/disposal helper.
- Required `If-Match`/presentation revision, `428`/`412`, and distinct
  `canvas_presentation_changed`/`canvas_replaced`/`canvas_cleared` conflict UI;
  cross-replica PostgreSQL advisory coordinator lock, safe
  temp/rename/readback behavior, and explicit best-effort guarantee.
- Ephemeral user-editing awareness.
- Recent-read invalidation and agent refresh/preserve-user-edit coverage;
  selection-to-next-message metadata remains deferred.
- Safe LaTeX source editing through the text renderer; compiled document
  preview remains deferred.

The delivered slice includes:

- `editable` in the file-only tool/internal-set contract, accepted only for a
  writable UTF-8 Markdown, text/code/LaTeX, or static-HTML presentation;
- owner-authorized bounded `PUT .../main/content` and conditional
  `POST .../main/refresh` responses with server-issued versioned content URLs,
  complete-state and content ETags, exact strong precondition parsing, and
  typed `428`/`412`/`409`/`423` outcomes;
- a bounded mutation-admission gate, stable SHA-256-derived signed advisory-lock
  key for `(thread_id, canonical_path)`, dedicated PostgreSQL session lock,
  locked thread-owner snapshot, Canvas row revalidation, and row update held
  across the workspace write/readback;
- same-directory exclusive SFTP temporary writes with permission preservation
  and POSIX rename, plus best-effort rclone temporary-object copy/replacement;
- refresh of both editable and read-only drifted files after rerunning the full
  path, generation, renderer, MIME, and size validation chain;
- a version-aware agent read-before-write guard: text `read_file` records the
  full-byte SHA-256, and `write_file`/`edit_file` require current bytes to match
  even when the best-effort browser control frame was missed;
- a validated, paced, exact-revision-deduplicated `canvas.source_updated`
  control path that invalidates the agent's recent read and reconciles live
  clients; Cockpit keeps only the latest committed update while its control
  socket reconnects and flushes it on open; plus one TTL-bound non-journaled
  editing-awareness lease per WebSocket client with periodic
  authorization/state revalidation;
- a pane-local dirty buffer, lazy shared Monaco adapter, edit/preview mode,
  read-only source-refresh action, original-source chrome preservation,
  reactive read-only state, model/editor disposal, and distinct conflict
  recovery without discarding user bytes on save failure, republish,
  replacement, clear, pane hide, or preview; thread/auth teardown does clear it;
- English/German UI copy and the updated `present-with-canvas` skill, which tells
  the agent to re-read shared editable bytes immediately before overwriting and
  to republish with the same editability choice.

Selection-to-message metadata and a full compiled LaTeX document preview remain
deferred. The implemented LaTeX path is safe source editing with the existing
text renderer. This slice still makes the documented best-effort claim: a shell
or another process which writes outside the coordinator can race the final
check/write interval, so it is not a cross-backend atomic CAS or CRDT.

Repository validation passed 426 focused Python tests, 1,030 Cockpit tests,
focused Ruff check/format, i18n checks, Canvas Stylelint, the companion-skill
validator, `git diff --check`, and a production Cockpit build (with the existing
bundle/CommonJS warnings). Local k3d then returned `200` for editable set/save
and refresh, a bodyless header-correct `304`, the specified stale-save `409`, and
one `200` plus one `409 canvas_presentation_changed` when identical preconditions
were raced directly against the two orchestrator replicas. The final deployed
cross-origin response also exposed both state and content validators to the
supported local Cockpit origin. See
[[dynamic_canvas_slice2_verification]].

### Slice 3 — Isolated one-port live preview

**Foundation checkpoint (Slice 3A):** Implemented and default-off as of
2026-07-13. This checkpoint adds the capability-gated flat `workspace_port`
tool/internal adapter, canonical port and entry-path policy, status-only public
state, complete pre-commit workspace target revalidation, a shared pinned
AsyncSSH transport with request-scoped `127.0.0.1` direct channels, bounded TCP
readiness, per-target connection single-flight, generation-scoped eviction,
workspace `sshd` restrictions, and Helm/Compose gate plus additional-denylist
plumbing. It creates no local listener and sends no HTTP bytes.

The master gate remains false in defaults and examples. An enabled gate still
does not create a browser-reachable viewer: no viewer session, URL, isolated
origin, proxy, or Cockpit iframe ships in 3A. Existing agent/session pods must
be recycled after a capability ConfigMap change; the orchestrator's server-side
gate makes disablement fail closed during that rollout. The companion skill
stays file-focused until presenting an app produces a usable user-facing stage.
See [[dynamic_canvas_slice3a_verification]].

**Ordinary-HTTP viewer checkpoint (Slice 3B):** Implemented and default-off as
of 2026-07-13, with the browser-bound bootstrap hardening completed in migration
`0062`. It adds:

- immutable migration `0061` provides hashed origin sessions, non-credential
  attachments, parent-BFF-session bounds, cleanup indexes, and
  transaction-delivered revocation notifications; forward-only migration
  `0062` adds attachment cookie-mode binding and the purpose-separated
  challenge/browser-binding/receipt/exchange state machine;
- owner-authorized create/authorize/renew/close attachment routes which require
  one exact BFF cookie, reject Bearer/internal-auth hybrids, bind creation to the
  current Canvas state ETag, and never put a gateway credential in the iframe
  URL;
- one exact UUID host per `origin_generation`, an iframe-only public attachment
  locator, exact parent `postMessage` bridge, attachment-specific transient
  browser cookie, atomic same-origin exchange, and reserved host-only
  `Secure; HttpOnly; SameSite=None; Partitioned` viewer cookie, with policy
  rollover and PostgreSQL revalidation on every request and at most every
  configured 15 seconds while an exchange is active;
- a dedicated ASGI gateway with no API fallback, exact raw-path/host parsing,
  bounded authentication admission and active-connection registration,
  fail-closed notification/listener behavior, client-disconnect propagation,
  and bounded SSH/HTTP teardown;
- complete-body validation/spooling before upstream bytes, a strict `h11`
  request/response adapter over a request-scoped pinned SSH direct channel to
  the single selected loopback port, common HTTP methods, HEAD/Range, safe
  same-origin/entry-loopback redirects, streamed bounded responses, and
  gateway-owned no-store/CSP/header policy;
- a Cockpit attachment controller, exact default-port HTTPS UUID-origin and
  credential-free bootstrap-URL validation before the resource-URL trust
  boundary, exact `WindowProxy`/origin/schema/challenge/receipt checks,
  bind-before-navigation, authenticated-ready (never iframe-load) semantics,
  fixed sandbox/referrer/Permissions Policy, teardown on presentation/thread/
  pane lifecycle changes, and a persistent **do not enter passwords or
  secrets** warning; and
- conditional Helm and Compose gateway plumbing, an internal-only service,
  fail-closed gateway/workspace NetworkPolicies, runtime Cockpit host-suffix
  injection, and no published development port or chart-owned Ingress; and
- trusted-parent anti-framing at every repository-owned document seam which can
  appear on the Cockpit host: production Nginx, Angular's development server,
  orchestrator/BFF HTTP responses, and the optional MCP HTTP/OAuth service.
  Existing route CSP is preserved as an independent enforced policy. VS Code
  webviews receive only `frame-ancestors 'self'` plus `SAMEORIGIN` for
  `/api/ide/...` on the exact separately configured API/IDE authority; the same
  path on a Cockpit authority remains denied. Explicit default ports, trailing
  DNS dots, missing/duplicate Host fields, and unhandled `500` responses are
  covered by fail-closed regressions. Deployments which place the IDE/API and
  Cockpit on the same authority intentionally lose IDE webview compatibility
  and must retain the chart's distinct API authority.

The bootstrap URL now carries only the canonical attachment UUID. A copied URL
does not carry the bridge nonce, browser binding, or BFF session and therefore
cannot authorize or exchange a viewer session. The browser may retain the
opaque viewer cookie only until the immutable parent BFF session's absolute
expiry. That retention is not authorization: the shorter renewable PostgreSQL
origin-session expiry remains authoritative on every gateway request. A newly
authorized exchange may reissue the already-validated same-host cookie to
refresh its browser retention bound; the parent renewal API extends only the
server lease and cannot silently resurrect an expired session.

Application traffic is deliberately **cookie-free in both deployment modes in
this checkpoint**. The gateway consumes the reserved viewer cookie, forwards no
`Cookie`, and drops every upstream `Set-Cookie`. Therefore selecting
`psl-isolated` satisfies a production configuration precondition but does not
claim full app-cookie support. SSE, multipart live streams, upgrades, and
WebSockets fail closed and remain Slice 4 work. The proxy supports only the one
`entry_port`; it does not interpret a multi-port route manifest yet.

Repository verification additionally covers exact parent-BFF binding (including
same user/different session), canonical non-credential URLs, closed bridge
schemas, wrong-frame/origin/challenge/receipt rejection, transient-cookie
binding, strict same-origin exchange, and two-replica single consumption. It
continues to cover policy rollover, expiry, renewal, shared-session reuse,
revocation/cancellation, framing ambiguity, body/response limits, redirects,
header/cookie stripping, SSH-close stalls, renderer lifecycle, default-off
manifests, and conditional network policy. See
[[dynamic_canvas_slice3b_verification]].

**Gateway database-isolation checkpoint (Slice 3C):** Implemented and
default-off as of 2026-07-14. The dedicated gateway now:

- lazily constructs a small pool only from required
  `CANVAS_VIEWER_POSTGRES_*` parts and cannot fall back to `DATABASE_URL` or the
  orchestrator's `POSTGRES_*` identity;
- requires `session_user = current_user`, then attests `CONNECT`, schema
  `USAGE`, every required column grant, and the absence of extra
  column/relation or sequence privileges, unrelated public-table access,
  elevated attributes, role membership, and database/schema `CREATE` before it
  checks schema readiness or starts the notification listener;
- replaces its shared `envFrom` projection with a dedicated allowlisted
  ConfigMap plus two explicit keys from a separate credential Secret;
- requires an operator-owned existing credential Secret in production and
  rejects chart-created credentials or automatic role provisioning there; and
- supports development against bundled Postgres through a bounded,
  service-account-token-free, deny-ingress role Job. Only that Job receives the
  application DB administrator credential; the public gateway never does. Helm
  and Compose use the same packaged psql grant contract.

The role can read only the Canvas identity, admission, workspace-binding, BFF
expiry, and viewer-state columns required by gateway authentication. It can
insert only origin sessions and update only challenge/consumption,
attachment-linkage, renewal, and revocation columns. It has no application
schema DDL, DELETE/TRUNCATE/TRIGGER, authoritative-state mutation, token-column
read, sequence, or unrelated-relation access. PostgreSQL's default PUBLIC
temporary-table capability is not represented as application-schema DDL; an
internal database whose PUBLIC role can create in `public` is rejected and must
be hardened by its operator.

The real-PostgreSQL migration CI now applies the packaged role script to a
freshly migrated PostgreSQL 15 database, runs the same startup attestation as
the gateway, rejects a broad authenticated session switched into the narrow
role, and proves representative token reads, authoritative mutations, viewer
deletes, sequence use, and public DDL fail with insufficient privilege. See
[[dynamic_canvas_slice3c_verification]].

**Trusted UI and local browser-harness closeout:** Implemented as of
2026-07-14. It adds:

- exact parent Fetch Metadata rejection as `canvas_browser_unsupported` and an
  exact missing-transient-cookie exchange outcome as
  `canvas_browser_storage_unavailable`; Cockpit maps only those causal codes to
  unsupported-browser/privacy guidance and keeps generic failures non-causal;
- an authenticated, minimal `/sessions/:threadId/canvas` wrapper which reuses
  the sandboxed pane, opens with `noopener,noreferrer`, and never promotes an
  untrusted app to a top-level document;
- `can_pop_out` for ready file sources and viewer-capable ready live apps,
  normalized source-kind and true client-sync chrome, and bounded type/title
  presentation context in rendered `set_canvas` tool cards;
- an exact-ETag, live-app-only reset-origin mutation and confirmation bound to
  the current source/revision/ETag. It rotates the origin generation, retires
  sessions and active work on the old generation, then reconciles sibling
  clients through authoritative events, a generic runtime presentation control,
  and bounded same-browser invalidation; and
- a pinned Playwright production-bundle harness with Chromium, Firefox, and
  WebKit projects. Its in-process BFF plus intercepted synthetic HTTPS viewer
  cover wrapper authentication, bootstrap/exchange, sandbox/CSP/navigation,
  synthetic credential-name/header separation, typed storage failure, reset,
  hard expiry, revocation, parent-auth expiry, and logout teardown.

The harness is deliberately not a live gateway integration: it does not open
TLS to `*.srwcanvas.works`, run the Python gateway/SSH proxy, exercise a real
partitioned cookie jar or PSL boundary, or establish Safari/iOS/PWA behavior.
The supported-path synthetic viewer emulates persistence of the partitioned
bootstrap cookie; the forced-storage-loss case explicitly disables that
emulation and proves the trusted fallback. Neither result is evidence for a
browser's real CHIPS policy or for Safari.
Those claims remain external acceptance gates. The closeout validation passed
367 focused backend/security tests, the full 1,085-test Cockpit suite, i18n,
TypeScript, Ruff, and a production build. Playwright listed 27 cases and passed
nine Chromium plus nine Firefox cases on the host. Fedora 44 could not launch
the Ubuntu WebKit bundle because its pinned `libicu74` and `libjpeg-turbo8`
ABIs were unavailable; the exact
`mcr.microsoft.com/playwright:v1.59.0-noble` runtime then passed all nine WebKit
cases. Main/Develop CI now install all three engines with Ubuntu dependencies,
but that new CI path has not yet reported acceptance evidence.

The remaining Slice-3 launch work is:

- publish the tunnel route (a `*.srwcanvas.works` ingress rule in the existing
  cloudflared ConfigMap targeting the internal gateway Service) and obtain then
  verify the effective private PSL boundary in shipping browsers
  (`docs/issues/canvas_hosted_edge_use_cloudflare_tunnel.md`);
- after the current release applies migrations through `0062`, run the
  implemented out-of-band workflow to provision the restricted gateway
  PostgreSQL role and populate its dedicated Vault entry. The chart creates the
  ESO credential mapping only when the viewer gate is enabled;
- deploy and black-box verify the implemented trusted-parent anti-framing
  boundary through the production ingress/edge and service-worker lifecycle.
  Repository tests now prove exact enforced `frame-ancestors 'none'` and
  `X-Frame-Options: DENY` behavior without replacing route CSP, plus the
  same-origin-only IDE compatibility policy. The checked-in app-shell policy
  marker forces a new PWA asset hash, but activation/reload from an already
  installed PWA, real-browser self-navigation, optional root-host routes, and
  the separately hosted Keycloak response policy remain acceptance evidence;
  and
- run the same authentication, CSP/sandbox, navigation, leakage, logout, expiry,
  reset, and cross-replica revocation matrix against the hosted Python gateway
  behind the tunnel edge with real secure-context cookies. Repeat the
  device-only portions on current desktop Safari, Safari/iOS, and installed
  PWAs; Playwright WebKit is engine coverage, not shipping-Safari evidence.

These remaining launch gates do not justify expanding the shared-secret
architecture inside this checkpoint.

**Hosted deployment checkpoint (2026-07-14):** `srwcanvas.works` is reserved
with one-label origins at `<uuid>.srwcanvas.works`. Its proxied wildcard and
Cloudflare Universal SSL certificate are live but resolve to the inert
catch-all `404`; no Canvas gateway route is published behind them.

The edge decision was then revisited
(`docs/issues/canvas_hosted_edge_use_cloudflare_tunnel.md`): the previously
staged DNS-only/HAProxy/WireGuard/NGINX relay was scrapped as
homelab-specific over-engineering for a raw-path requirement that is not
load-bearing when normalization happens upstream of the single gateway
boundary. The hosted route is the existing Cloudflare Tunnel with a wildcard
ingress rule targeting the internal gateway Service; chart consumers get the
same result through the optional `viewer.ingress` wildcard Ingress or any
route to the ClusterIP Service. Raw-path fidelity remains available to
deployments that front the gateway with an L4/SNI-passthrough load balancer
and is verified there with the verifier's explicit `--probe-raw-path`
differential. The dedicated database credential mapping belongs to the SRW
Helm release and is rendered only with the internal viewer gate.

The secret-safe production role/Secret workflow and the redacted
`scripts/verify-canvas-hosted-edge.py` harness are also implemented. The live
cluster still runs the older `sha-3a14ada` release and lacks migrations through
`0062`, the gateway, restricted role, and dedicated Secret. No tunnel ingress
rule targets the gateway yet, and the authoritative PSL has no exact private
`srwcanvas.works` rule. The live verifier therefore fails Cockpit/API/IDE/PWA
protection and PSL as expected; Keycloak's final login document already blocks
cross-origin framing. Keep `pslBoundaryVerified`, the viewer gate, and the
master live-preview gate false until the tunnel route, shipping-browser PSL
behavior, and complete hosted acceptance matrix pass.

Do not enable or claim the user-facing slice complete until real browsers prove
no BFF/Keycloak cookie or authorization-header leakage. The default-off 3A–3C
implementation and local intercepted-browser harness do not satisfy or bypass
that hosted launch gate, and the viewer was not enabled in local k3d during
these checkpoints.

### Slice 4 — Multi-port and streaming apps

- `workspace_app` manifest parsing/normalization and at most eight
  prefix-preserving same-origin routes;
- SSE heartbeat/idle behavior and streaming-specific limits;
- WebSocket subprotocols, binary/text, close propagation, Origin policy, and
  backpressure;
- optional audited Canvas bridge with trusted “Send to agent” confirmation;
- end-to-end Vite + FastAPI example using relative `/api` and `/ws`, exact
  allowed preview hosts, HMR, and no direct fallback.

### Slice 5 — Shared browser

Design locked 2026-07-20 in `shared_browser.md` (browser-exec in-process CDP
screencast, orchestrator SSH-brokered stream, canvas-hosted renderer, and a
first-class user-initiated "Open browser" flow).

**Current state (2026-07-22): the attested-container user flow is implemented
and repository-verified behind the default-off gate; release acceptance remains
partial.** Plan 1 delivered:

- the `browser-exec` loopback stream, stable browser generation, in-process
  CDP screencast/input adapter, daemon-owned single-controller baton, agent
  mutation refusal, navigation validation, fan-out, and lifecycle handling;
- fail-closed orchestrator config, owner-authorized open endpoint,
  generation-pinned SSH/WebSocket relay, viewer cap, activity marking, Canvas
  `can_stream_browser` capability, and default-off Helm wiring;
- host-side service coverage plus a Podman conformance gate that passed
  against real Chromium in the rebuilt container workspace image.

Plan 2 Tasks 1–13 delivered:

- the Cockpit `browser` protocol, controller, renderer, cold Open browser
  workflow, navigation/input toolbar, explicit baton, popout fan-out, bounded
  reconnect, and ended-generation restart;
- capability-scoped agent `set_canvas(browser)` handling plus baton state and
  clear `user_is_driving` refusal output;
- pinned browser-start transport, hardened browser-visible WebSocket admission,
  active-target/loading and zero-viewer lifecycle behavior, and the shared
  container/VM image conformance program; and
- focused backend, real container, full Cockpit unit/build, and 33-case
  Chromium/Firefox/WebKit conformance gates.

The Plan-2 execution document is
`docs/superpowers/plans/2026-07-22-shared-browser-cockpit-handoff.md`. Its final
acceptance task is partial: the required stage-2 VM build inputs and a usable
live LLM endpoint were unavailable, and this local k3d host lost
orchestrator-to-existing-workspace routing across a full pod replacement. The
real Uvicorn 1012 reconnect, executor-level handoff, viewer cap, activity,
zero-viewer release, and restart flow passed. VM runtime remains deliberately
disabled unless a deployment independently supplies both attested binding and
orchestrator routing; building the golden image would not claim either.

The precise current boundary, protocol, and redacted evidence are in
`shared_browser.md`, `docs/tests/shared_browser_verification.md`, and the two
execution plans. A 2026-07-23 usability batch from first dev-cluster use
(Enter virtual key codes, clipboard paste-in, cold-open start page,
newest-frame delivery, turn-following baton automation, launch fingerprint
hygiene) is recorded in the spec's "Usability batch (2026-07-23)" section.

The next shared-browser checkpoint is **Plan 3 — release and enablement**. It
will carry the four unfinished Plan-2 acceptance gates: reproducible live k3d
automation on healthy networking, prompt-driven form/cookie handoff with a
working LLM, the stage-2 VM image conformance run, and guarded Tilt enablement.
No new Canvas source or renderer is required for that checkpoint. Proactive
agent takeover requests and pushed baton events belong to a later
evidence-driven collaboration slice.

### Slice 6 — Expansion based on evidence

- Multiple named canvases and optionally a grid.
- Content-addressed published copies, restore history, job-scoped/offline
  artifacts, retention/quotas, and notification deep links. *(Partly done:
  offline artifacts shipped 2026-07-28 as one current copy per Canvas —
  `docs/features/canvas_durable_presentation.md`. Content addressing, restore
  history, and job-scoped canvases remain open.)*
- External allowlisted URLs.
- Job/expert/skill draft forms through shared application actions.
- Safe SVG-as-image, PDF, strict Mermaid, data-table, or desktop adapters.
- Native multipart/MJPEG media streaming, only with stream-count, bandwidth,
  cancellation, session-revalidation, and real-browser coverage.

## Verification Strategy

### Backend/tool tests

- Flat tool schema accepts exactly the arguments for each source kind and
  rejects mixed/unknown/nested route forms; only `main` can be addressed.
  Omitted title/editability normalize to a descriptive source label and
  `false`, while unsupported or unwritable `editable=true` fails closed.
- `require_thread_owner` checks, including its existing admin bypass, cover the
  implemented state, content, edit, reset-origin, viewer-session creation/
  revocation, and ordinary-HTTP entry points. The pop-out is an auth-guarded
  Cockpit rendering target for those same owner-authorized APIs; the planned
  WebSocket adapter must receive equivalent coverage when it lands.
- The internal get/set/clear routes reject missing/invalid `X-Internal-Key`, missing
  delegated user, BFF-only/public-ingress access, and a delegated caller without
  owner/admin authorization; its accepted request reaches the same service
  semantics as other adapters.
- First set/same-pointer refresh/replacement/clear/repeated clear have the
  specified atomic presentation revisions; underlying sources remain untouched.
- Canonical source fingerprints are stable across map/input ordering and
  `workspace_port` normalization, change for every security-relevant pointer
  field, and exclude presentation-only metadata. State ETags change when any
  caller-visible derived representation changes even if the numeric
  presentation revision does not.
- Ordered event persistence never inserts a later sequence before an earlier
  queued one. A missed/duplicate invalidation still converges through REST.
- Remote SFTP and shared durable virtual reads produce the same hash/MIME/range
  contract; process-local memory, scratch, suspended, missing, oversized, and
  unsupported backends return typed outcomes.
- A virtual object which grows after HEAD is stopped at the transport-level
  byte limit. Slow/cancelled ASGI sends retain at most the configured response
  leases and release them exactly once.
- Traversal, symlink escape, MIME spoofing, incompatible renderer, stale source
  version, malformed Range, and decoded-image bombs fail closed.
- Docker allocation serializes jobs and threads across replicas; releasing and
  quarantined leases remain occupied, failed cleanup is never advertised as
  reusable, and suspension/restore cannot bypass the lease CAS. Multi-user
  defaults require controller-attested recreation rather than SSH cleanup.
- Editing requires both preconditions and distinguishes `428`, `412`, and
  same-presentation/source replacement outcomes; candidate type validation and
  readback version become the returned/stored source version. Refresh reruns all
  source/renderer/image checks and leaves state unchanged on failure.
- Current one-port app validation rejects arbitrary hosts, reserved
  ports/control paths, encoded separators, recursive percent encodings,
  backslashes, dot segments, and stale workspace/origin generations. Slice-4
  manifest tests must add `/api`→`/apix`, duplicate/ambiguous route, route-count,
  and exact selected-route byte coverage.
- Origin generation is preserved for an unchanged one-port app refresh and
  rotates for replacement, `new_app`, runtime changes, clear/re-present, and
  the conditional trusted reset-origin action; reset preserves the source
  fingerprint while retiring the previous generation's sessions and active
  work. Closing an individual view attachment or revoking one origin session
  preserves it. Route-change coverage joins this contract with Slice 4.
- Multi-replica tests prove shared-session reuse across tabs, renewal, explicit
  revocation through PostgreSQL notification, and bounded closure after a
  deliberately missed notification. Current cancellation covers slow upload,
  upstream header wait, ordinary HTTP response, and direct SSH I/O from the
  start of the exchange; a closed attachment does not evict another tab. Slice
  4 must extend it to SSE/WebSocket activity.

### Cockpit tests

- Source/media type selects the expected trusted renderer.
- Thread changes cancel stale state/content loads; a no-ID draft never displays
  the previous thread's stage.
- A Canvas invalidation refreshes REST metadata/content without resetting chat;
  duplicate/older revisions are ignored.
- Repeated image presentation bypasses cache.
- User save sends both preconditions and preserves local content across
  `412`, same-source presentation change, source replacement, and clear
  conflict UI without mislabeling a republish as replacement.
- Static HTML sanitizer removes scripts, handlers, forms, base/navigation, and
  all image/font/resource URLs; Markdown raw HTML/external images are disabled.
- Pathological Markdown/static HTML fails with translated complexity UX before
  attaching oversized sanitized DOM or `srcdoc` output.
- New/hidden/refresh behavior preserves scroll/editor selection and focus.
  Tool-card tests cover normalized source kind/title, bounded metadata,
  current/replaced revision semantics, and the rendered Open Canvas action.
- Component/service tests cover live-app iframe titles, mobile Chat/Canvas
  state, authenticated pop-out routing/polling, opener/referrer isolation,
  reset-origin preconditions/control fan-out, revision-only cross-tab
  invalidation, and causal unsupported/storage-failure classification. The i18n
  check covers English/German key parity. Rendered assertions for the pane's
  `aria-live` status and raster-image `alt` propagation remain follow-ups. The
  splitter's ARIA configuration exists, but real
  keyboard/ARIA-value/collapse behavior and physical focus return remain
  browser-acceptance work. Run `npm run i18n:check`.

### Proxy and real-browser tests

Vitest/jsdom cannot enforce CSP, cookies, service workers, iframe sandboxing,
native PDF, or WebSocket proxy semantics. The production-build Playwright
harness now runs the real Angular wrapper/pane/controller, with Chromium,
Firefox, and WebKit projects, while a local BFF fixture and request interception
simulate the browser-visible gateway. The locally executed Chromium/Firefox
projects cover the implemented wrapper, iframe bootstrap/exchange,
sandbox/CSP/navigation probes, credential-name separation, copied
top-level locator rejection, typed storage fallback, origin reset, hard expiry,
authoritative revocation, parent-auth expiry, and logout teardown.

That local conformance harness blocks service workers for determinism and does
not exercise the Python gateway, SSH, public TLS/DNS, real third-party-cookie or
CHIPS policy, an effective PSL boundary, cross-replica PostgreSQL cancellation,
or the hosted raw-path edge. Playwright WebKit is not shipping Safari coverage.
Its supported-path WebKit fixture emulates partitioned-cookie persistence; the
missing-cookie test turns that emulation off. Those synthetic outcomes prove the
UI/protocol branches only, not CHIPS enforcement.
From `cockpit/`, install its pinned engines with
`npm run test:e2e:canvas:install` and run the production-build suite with
`npm run test:e2e:canvas`; `test:e2e:canvas:no-build` is only the iteration path
for an already-built `dist/cockpit/browser` tree.
Real Safari/iOS, installed-PWA/service-worker behavior, hosted-network probes,
and enabled live-k3d acceptance remain a physical-device, device-cloud, or
deployed-environment matrix. The broader remaining matrix includes:

- static fake-login/script/form/popup/top-navigation/download/storage/network
  attempts and an always-available trusted escape control;
- hosted Chromium, Firefox, and WebKit embedded/wrapper authentication against
  the real gateway, including copied-locator/different-BFF rejection and no
  bridge, binding, exchange, or session secret in logs or URLs; repeat the
  relevant matrix on current Safari/iOS and installed PWAs;
- no BFF/Keycloak cookie/internal auth header upstream, no application cookies
  or upstream `Set-Cookie` in either current mode, and origin rotation preventing
  an old Service Worker/storage entry from controlling a replacement;
  production tests use an effective PSL boundary, while insecure PSL-less
  development proves only the explicitly limited cookie-free profile;
- exact public `Host`/`Origin`, rebuilt forwarding headers without viewer IP,
  stripped platform `Authorization`, cookie-free mode, and rejection of
  cross-origin HTTP requests; add WebSocket Origin cases in Slice 4;
- removal of upstream CSP/CORS/reporting/NEL/auth-challenge/refresh/clear-site
  and cache/validator headers, with gateway-owned policy/no-store winning and
  repeated safe application headers remaining intact; reload/back/history after
  expiry or revocation cannot replay authenticated app data from HTTP cache;
- proxy-owned CSP/Permissions Policy against external fetch, nested frame,
  object, worker, WebRTC/data-channel, camera/mic/geolocation, popup, download,
  self-navigation, and fake trusted-auth attacks, documenting any browser-level
  channel which cannot be blocked;
- ordinary-response first-byte streaming, decoded/reframed chunked requests and
  streamed chunked responses, cancellation, limits, safe relative/same-origin
  and exact loopback-entry redirects, fail-closed external/ambiguous redirects,
  Range/HEAD, and every permitted method; Slice 4 adds indefinite SSE/heartbeat,
  multi-port loopback redirects, application-cookie behavior if separately
  approved, and any trusted-parent external-navigation confirmation;
- **Slice 4:** WebSocket HMR, binary/text, subprotocol negotiation, Origin, close
  code/reason, fragmented-message limits, rejected compression, message rate,
  cancellation, slow-client backpressure, and connection limits;
- conflicting/multiple length headers, transfer-coding ambiguity, invalid
  chunks/trailers, unexpected upgrades, inbound chunked reframing, parser
  differential cases, no upstream request bytes before full framing/body
  validation, and close-without-reuse after every framing failure;
- **Slice 4:** Vite + FastAPI with entry/API/WS ports through one origin and no
  browser request to workspace localhost; the fixed deployment host suffix
  works while `allowedHosts: true` and unallocated Canvas hosts fail;
- SSH direct-channel destination limited by service policy and teardown on workspace
  generation/host-key change, and no direct workspace-port ingress from
  Cockpit/other workspaces;
- mobile hidden-frame `inert` behavior, teardown on thread switch/clear, focus
  return, and accessible splitter/iframe names.

## Open Questions

- Which full-LaTeX preview path should follow Markdown math: browser rendering,
  workspace compilation to PDF, or both?
- After observing model-invoked skill usage, is a capability-aware hard
  `before_tool:set_canvas` binding worth the first-call retry?
- When job-scoped canvases arrive, are they durable job artifacts, presentation
  pointers into completed workspace snapshots, or both?
- Does a thread with `workspace.backend = none` warrant an orchestrator-owned
  inline artifact store, or is explicit workspace upgrade the right boundary?
- When multiple humans can edit one thread, is awareness per canvas sufficient,
  or does it need per-user cursors/locks?

None of these blocked Slices 0–2 or the default-off 3A–3C implementation
checkpoints. The browser authentication matrix is a blocking production
acceptance gate for a user-facing Slice 3 release, not an unanswered
architecture choice hidden in the file-stage scope.

## Relationship to Adjacent Features

- **`shared_browser.md`** — supplies the implemented Slice-5 generation,
  transport, agent handoff, and Cockpit renderer/control contract (design
  locked 2026-07-20; container flow verified 2026-07-22). Canvas owns where it
  appears; Shared Browser owns concrete browser generations, dedicated
  profiles, transport, and the single-controller handoff. Canvas must not
  depend directly on experimental CDP screencast details.
- **`agent_skills.md`** — supplies model-invoked bundled-skill discovery and
  `use_skill`. The Canvas skill is concise guidance, not a second schema spec.
- **`workspace_network_policy_unification.md`** — remains the authority for
  workspace ingress. The selected SSH direct-channel transport intentionally avoids a
  broad new Canvas port range in NetworkPolicy.
- **`builder_to_sessions_consolidation.md`** — the parked Builder mutations are
  reference material for future structured job-draft actions, not the core
  Canvas state service. The pointer-based canvas should not copy the deleted
  Builder SSE session loop verbatim.
- **`shared_application_action_layer.md`** — Canvas state and future draft
  actions should have one authoritative application semantic with thin session,
  REST, and later MCP adapters.
- **`session_turn_rendering.md`** — long-form/interactive artifacts move to the
  canvas; chat retains narration, decisions, and concise results.
- **`persistent_chat_component_style_budget.md`** — integration belongs in the
  thin page wrapper, not inside the persistent-chat monolith.
- **`vm_snapshots_and_ide.md`** — the IDE remains appropriate for real source
  work; Canvas removes the IDE/proxy detour for presentation and collaboration.
- **`rdp.md`** — desktop remains deferred and would become another source kind
  only after concrete non-web GUI demand.

## Sources and Prior Art

Primary/official sources reviewed on 2026-07-12:

### Comparable products and UI contracts

- [Claude Artifacts](https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them)
  — dedicated side window, multiple artifacts, targeted editing, and version
  selection.
- [Claude Code Artifacts](https://code.claude.com/docs/en/artifacts) — source
  files plus separately retained published versions, a sandboxed user-content
  domain, size/retention limits, and strict external-request policy.
- [ChatGPT Canvas](https://help.openai.com/en/articles/9930697-what-is-the-canvas-feature-in-chatgpt-and-how-do-i-use-it)
  and [writing/code blocks](https://help.openai.com/en/articles/20001246-working-with-writing-blocks-and-code-blocks-in-chatgpt)
  — direct/selection-targeted edits, history/diff, sandboxed previews, and the
  value of artifact UI that is not coupled only to a side pane.
- [OpenAI Apps SDK component reference](https://developers.openai.com/apps-sdk/reference)
  and [security/privacy guide](https://developers.openai.com/apps-sdk/guides/security-privacy)
  — declared UI metadata, dedicated component origins, sandboxing, and
  host-owned CSP boundaries.
- [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview)
  and [protocol overview](https://apps.extensions.modelcontextprotocol.io/api/documents/Overview.html)
  — sandboxed host renderers, capability negotiation, audited `postMessage`
  communication, lifecycle, and progressive fallback.
- [assistant-ui Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui) —
  client-side tool renderer registry and explicit human result submission.

### Web security, transport, and concurrency

- [CSP Level 3](https://www.w3.org/TR/CSP/),
  [WHATWG iframe](https://html.spec.whatwg.org/multipage/iframe-embed-object.html),
  and [MDN `srcdoc`](https://developer.mozilla.org/en-US/docs/Web/API/HTMLIFrameElement/srcdoc)
  — response/meta policy boundaries, sandbox flags, opaque origins, and
  `srcdoc` injection risk.
- [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
  — allowlisted logical targets instead of caller-provided complete URLs.
- [RFC 9110 conditional requests](https://datatracker.ietf.org/doc/html/rfc9110)
  and [RFC 6585](https://datatracker.ietf.org/doc/html/rfc6585) — strong
  `If-Match`, `412 Precondition Failed`, and `428 Precondition Required`.
- [RFC 9112 HTTP/1.1](https://www.rfc-editor.org/rfc/rfc9112.html) — message
  framing, transfer-coding, conflicting-length handling, and connection close
  requirements used by the proxy anti-smuggling boundary.
- [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
  — deterministic source-fingerprint serialization across implementations.
- [W3C Fetch Metadata Request Headers](https://www.w3.org/TR/fetch-metadata/) —
  `Sec-Fetch-Dest`/mode/site distinctions used to reject top-level bootstrap
  navigation before challenge state is created.
- [Partitioned cookies](https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Third-party_cookies/Partitioned_cookies),
  [third-party cookie guidance](https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Third-party_cookies),
  and [secure cookies](https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Cookies)
  — embedded cookie partitioning and `__Host-` requirements.
- [Public Suffix List purpose](https://publicsuffix.org/learn/) and
  [submission guidance](https://github.com/publicsuffix/list/wiki/Guidelines) —
  sibling cookie isolation, submission constraints, and why production app
  cookies need an effective tenant boundary in addition to host-only viewer
  authentication/filtering.
- [Service Worker API](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API)
  and [`worker-src`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/worker-src)
  — persistent per-origin interception and worker restriction.
- [MDN `postMessage`](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage),
  [RFC 7239 `Forwarded`](https://www.rfc-editor.org/info/rfc7239/), and
  [RFC 6455 WebSocket](https://www.rfc-editor.org/info/rfc6455/) — exact-origin
  messaging, trusted-proxy header boundaries, and WebSocket Origin/subprotocol
  semantics.
- [OpenSSH `sshd_config`](https://man.openbsd.org/sshd_config) and
  [AsyncSSH direct connections](https://asyncssh.readthedocs.io/en/latest/api.html)
  — loopback-limited direct TCP channels without a process-local listener.
- [Vite server options](https://vite.dev/config/server-options) — HMR WebSocket
  proxy behavior and the security impact of permissive allowed hosts.

### Renderer and accessibility constraints

- [SVG as an image](https://developer.mozilla.org/en-US/docs/Web/SVG/Guides/SVG_as_an_image),
  [Mermaid security levels](https://mermaid.js.org/config/usage.html#securitylevel),
  [KaTeX security](https://katex.org/docs/security), and
  [OWASP file handling](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
  — renderer-specific isolation, trust/complexity limits, and MIME/size
  validation.
- [WCAG 2.2](https://www.w3.org/TR/WCAG22/),
  [window splitter pattern](https://www.w3.org/WAI/ARIA/apg/patterns/windowsplitter/),
  [status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages),
  [iframe titles](https://www.w3.org/WAI/WCAG22/Techniques/html/H64), and
  [drag alternatives](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html)
  — the testable host accessibility contract.
- [Chrome DevTools Protocol Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/)
  and [Input](https://chromedevtools.github.io/devtools-protocol/tot/Input/) —
  screencast/input primitives and why browser transport/control remain a broker
  concern rather than Canvas state.

Repository findings came from the current SRW workspace/file backends, thread
upload/job-file routes, IDE proxy, event journal/SSE path, Canvas-adjacent docs,
skills runtime, NetworkPolicy/Helm templates, and Cockpit chat/Markdown/service
worker implementations described above.
