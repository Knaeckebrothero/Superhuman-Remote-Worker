# Dynamic Canvas Slice 3A — default-off live-app foundation verification

**Status:** The one-port callable, validation, and SSH transport foundation is
implemented and passed automated and default-off local-k3d verification on
2026-07-13. It is intentionally dark-shipped: no viewer session, user-content
origin, HTTP proxy, iframe renderer, or live-app URL is exposed yet.

**Feature:** `docs/features/dynamic_canvas.md`

## What this checkpoint proves

- a positively attested shell workspace can conditionally advertise the flat
  `workspace_port` form of `set_canvas` when the orchestrator master gate is
  enabled;
- the orchestrator remains authoritative and rejects port presentation while
  that default-off gate is disabled, even if an already-running agent retained
  stale capability state;
- one integer port and one canonical origin-form entry path normalize to the
  existing private `WorkspaceAppSource`; public and model-facing state expose
  only `type=workspace_app` and `entry_path`, never the port, SSH target,
  fingerprint, generation, origin generation, or a fabricated viewer URL;
- app publication checks TCP acceptance without sending HTTP bytes through a
  request-scoped SSH `direct-tcpip` channel to fixed `127.0.0.1`;
- the shared SSH transport uses exact Ed25519 host-key pinning, per-target
  single-flight connection setup, bounded health concurrency, 60-second idle
  eviction, generation-scoped retirement, and pre/post target revalidation;
- a closed application port reports `starting`, while SSH policy, capacity,
  identity, and transport failures remain typed non-readiness failures; and
- workspace SSH configurations allow only client-local forwarding to workspace
  loopback and disable gateway, agent, and tunnel forwarding.

This checkpoint does **not** prove or ship browser authentication, a separate
user-content domain, wildcard DNS/TLS, an effective private PSL boundary, raw
path preservation at ingress, HTTP framing, response streaming, redirects,
cookies, CSP, iframe sandboxing, pop-out, SSE, WebSockets, multi-port routing,
or shared-browser behavior. Cockpit therefore continues to treat
`workspace_app` as unsupported, and the companion skill remains file-focused.

## Delivered implementation surfaces

| Surface | Delivered location |
|---|---|
| Port/path policy, TCP readiness, and full commit binding check | `orchestrator/services/canvas_apps.py` |
| Shared pinned SSH/SFTP transport and direct channels | `orchestrator/services/canvas_ssh.py`, `canvas_files.py` |
| Gated internal set and status-only public representation | `orchestrator/routers/canvases.py` |
| Attested capability hydration and hot-swap handling | `orchestrator/main.py`, `src/core/workspace_backend.py`, `src/api/persistent_session.py`, `persistent_app.py` |
| Capability-scoped flat tool schema and logical redaction | `src/tools/canvas/__init__.py`, `src/api/orchestrator_client.py` |
| Workspace SSH forwarding restrictions | `docker/Dockerfile.workspace`, `docker/agent-vm-base/scripts/provision-stage2.sh` |
| Default-off gate and additional denylist plumbing | Helm values/schema/templates, Compose environments, and public examples |

## Security and operational boundary

The built-in reserved ports are `30022`, `9222`, and `38080`; deployments may
add ports through `canvas.livePreview.deniedPorts` or
`CANVAS_LIVE_PREVIEW_DENIED_PORTS`. Ports below 1024 and values above 65535 are
always rejected. The application source never accepts a hostname or URL.

`canvas.livePreview.enabled` and `CANVAS_LIVE_PREVIEW_ENABLED` default to
`false`. Enabling the flag alone still creates no browser-reachable app. It is
only a development gate for the callable foundation until the isolated viewer
boundary lands. Changing ConfigMap-backed capability values requires recycling
existing worker and persistent-session pods; their process environments do not
change in place. Server-side rechecking makes disabling fail closed while pods
are being recycled.

The short 3A health operation revalidates the complete generation, endpoint,
and fingerprint before and after opening its direct channel. The future
long-lived proxy must additionally register and actively cancel in-flight SSH,
HTTP, SSE, and WebSocket activity on revocation; the bounded idle cleanup used
here is not presented as that later revocation protocol.

## Local k3d execution record

Environment: context `k3d-srw`, namespace `srw`, 2026-07-13.

| Check | Result |
|---|---|
| Deployment gate | **PASS** — `srw-config` rendered live preview disabled. |
| Loaded implementation | **PASS** — the running orchestrator imported the new app service and canonicalized `/%61pp/` to `/app/`. |
| Default-off internal boundary | **PASS** — an authenticated delegated-owner `workspace_port:8501` set returned `503 canvas_live_preview_disabled`. |
| Orchestrator rollout | **PASS** — both orchestrator replicas were ready after the disabled-gate update. |
| Existing Canvas behavior | **PASS** — the larger Slice 0–2 regression set remained green; no live-app URL or Cockpit renderer was introduced. |

The smoke selected an owned thread only inside the orchestrator process and
printed neither its identity nor any credential. The unrelated optional
OpenCloud/MCP health state is not part of this acceptance claim.

## Automated validation

- **Python:** 652 combined Canvas, delegated client, persistent-session,
  tool-context, and workspace-tool regression tests passed. Dedicated app,
  transport, callable, and infrastructure suites cover port/path ambiguity,
  capacity, target races, connection single-flight, cancellation, channel
  classification, state redaction, and disabled-gate behavior.
- **Static:** focused Ruff check/format and `git diff --check` passed.
- **Deployment:** both required Helm lint overlays passed; an enabled test
  render carried the gate and `8501,9000` denylist through the ConfigMap and
  orchestrator environment. The values schema rejects non-integer or out-of-
  range additions. Compose YAML propagation and both public disabled examples
  are regression-tested.
- **Workspace SSH:** Docker and VM configurations resolve to local forwarding,
  fixed loopback `PermitOpen`, no gateway ports, no agent forwarding, and no
  tunnels; the VM provisioning script passes shell syntax validation.

No Cockpit build or real-browser live-app matrix is claimed by 3A because this
checkpoint deliberately adds no frontend live-app code.

## Next implementation gate

Continue Slice 3 only after the deployment has a separately registrable
user-content domain, wildcard DNS/TLS, an effective private PSL boundary,
raw-path-preserving outer host dispatch, and the specified real-browser
authentication/leakage fixtures. The next code then adds viewer sessions and a
bounded ordinary-HTTP proxy before Cockpit receives its fixed sandboxed iframe
renderer. Untrusted applications must never fall back to a top-level trusted
Cockpit or API origin.
