---
tags:
  - architecture
  - orchestrator
  - networking
  - websockets
  - refactor
status: implemented
---

# Direct Session WebSockets — Orchestrator as Lighthouse

Move long-lived bidirectional connections out of the orchestrator process. The orchestrator becomes a control plane that handles one-off REST requests (create session, fetch connection details, deprovision); the actual WebSocket data flows directly between the user's browser and the agent pod, with Traefik (or any K8s-Ingress-compatible controller) as the data-plane proxy. Co-benefit: the ~300 lines of WS proxy code that previously lived inside `orchestrator/main.py` have been extracted into a proper module.

**Status:** Implemented and smoke-tested on the dev cluster (2026-05-23). All 14 plan tasks complete; see `docs/superpowers/plans/2026-05-22-direct-session-websockets.md`. The two related issues identified during smoke testing — [[persistent_thread_double_provisioning_race]] (now in `docs/done/`) and [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]] (now in `docs/done/`) — were resolved in the same branch.

---

## Problem

Today, two long-lived WebSocket paths run through the orchestrator Python process:

- `/ws/persistent/{thread_id}` — persistent agent chat (`orchestrator/main.py:13747–14063`)
- `/api/ide/{job_id}/proxy/*` (WS half) — code-server IDE (`orchestrator/main.py:7852–7945`)

Both terminate the user's WebSocket inside the orchestrator and then dial a second WebSocket to the agent pod, ferrying frames bidirectionally. The Python process spends most of its time forwarding bytes — work it's neither designed for nor well-suited to. Three concrete pains:

1. **Reconnect is ~20+ seconds for a warm session.** Every WS open re-runs the full pre-flight pipeline inside the handshake: cookie resolution, thread lookup, workspace-status check, stale-binding heuristic (which can spuriously trigger a re-provision), agent registration polling, `/ready` polling with a 2-second initial sleep. Even on the happy path with the agent already up, ~3 seconds of orchestrator overhead is unavoidable; on the bad path, the cockpit sits in "provisioning…" pings for tens of seconds.
2. **Orchestrator restart drops every active session.** All WS upstream connections live in this process. A normal deploy is a session-loss event for every connected user.
3. **The orchestrator does not scale horizontally as a WS proxy.** Replicas serialize per-session state in awkward ways (the stale-binding cleanup is a particularly tangled example), and adding more replicas multiplies the work proportional to active sockets rather than dividing it.

Beyond the runtime pains, the WS handler bundles several control-plane responsibilities (auth, ownership, on-demand provisioning, readiness polling, in-flight notification inspection) directly into the data-plane loop. Disentangling them is most of the refactoring work.

Related issues this design addresses or unblocks:
- [[persistent_thread_double_provisioning_race]] — racey on-demand provisioning inside the WS handshake
- [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]] — symptom of the same handshake reset behavior
- [[persistent_chat_silent_disconnect]] — relates to the reconnect protocol redesign
- [[orchestrator_main_py_monolith]] — the 20k-line `main.py` problem; this is one of the easier-to-extract pieces

## Solution overview

Apply the **configurable-http-proxy pattern** (JupyterHub's term, also used by Coder/Gitpod/Codespaces/fly.io/Replit): the orchestrator writes routes; a separate data-plane proxy serves them. Routes live in Kubernetes — one standard `networking.k8s.io/v1 Ingress` resource per bound agent pod — and the cluster's existing ingress controller (Traefik in the project's default setup) reconciles them. The orchestrator process is never in the WS data path.

Principle the design follows:

> Long-lived bidirectional traffic goes direct between browser and pod. Short one-off requests (REST) stay on the orchestrator.

## Architecture

### Component overview

| Component | Role | New / changed |
|-----------|------|---------------|
| Cockpit `persistent-chat.service.ts` | Calls REST to get connection details, then opens WS directly to the Ingress URL. F4 reconnect engine unchanged except the URL it dials. | Changed |
| Orchestrator `routers/sessions.py` | New module hosting `POST /api/sessions/{thread_id}/prepare` and `GET /api/sessions/{thread_id}/connection`. | **New module** |
| Orchestrator `services/session_router.py` | Manages per-session Ingress resource lifecycle via the K8s API. | **New module** |
| Orchestrator `services/session_tokens.py` | Mints + validates the short-lived session JWT. | **New module** |
| Orchestrator `services/nats_bridge.py` | Gains subscription to `session.events.*` — receives notifications from agent pods and broadcasts to the SSE feed. | Changed |
| Orchestrator `main.py` | Loses the two WS proxy handlers and the `_inspect_session_event` / `_inspect_browser_event` helpers. | **Code removed** |
| Agent pod `/ws/chat` | Validates the session JWT on incoming connections. | Changed |
| Agent pod notification emission | Publishes `permission.request`, `vm_upgrade.needed`, `approve`, `deny` events to NATS instead of relying on orchestrator-side WS inspection. | Changed |
| Traefik (or any K8s-Ingress controller) | Reads Ingress resources, forwards WS to pods. Existing component, no config change. | Unchanged |

### Data flow — new session

```
Cockpit                        Orchestrator                    K8s API     Traefik     Agent pod
  │                                                                                          │
  │  open SSE subscription on session.lifecycle.{tid}                                        │
  │─────────────────────────────────▶│                                                       │
  │                                  │                                                       │
  │  POST /api/sessions/{tid}/prepare│                                                       │
  │─────────────────────────────────▶│ acquire advisory lock on tid                          │
  │   202 {state: "provisioning"}    │ auth, ownership, restore                              │
  │◀─────────────────────────────────│ provision pod                                         │
  │                                  │ wait for /ready                                       │
  │                                  │ create Service + Ingress  │                           │
  │                                  │───────────────────────────▶│                          │
  │                                  │                            │  reconcile               │
  │                                  │                            │──────────▶│ route added  │
  │   SSE: state=provisioning        │                                                       │
  │◀─────────────────────────────────│                                                       │
  │   SSE: state=booting             │                                                       │
  │◀─────────────────────────────────│                                                       │
  │   SSE: state=ready               │                                                       │
  │◀─────────────────────────────────│ release lock                                          │
  │                                                                                          │
  │  GET /api/sessions/{tid}/connection                                                      │
  │─────────────────────────────────▶│ 1 DB read, mint 60s JWT                               │
  │   200 {ws_url, token, expires_at}│                                                       │
  │◀─────────────────────────────────│                                                       │
  │                                                                                          │
  │  wss://api.example.com/p/{tid}/ws?t={jwt}                                                │
  │─────────────────────────────────────────────────────────▶ Traefik ─────────────────────▶│
  │                                                                                          │  validate JWT
  │◀──────────────────────────── WS frames flow direct ─────────────────────────────────────│
```

### Data flow — reconnect (warm session)

```
Cockpit                        Orchestrator                    Traefik     Agent pod
  │                                  │                            │             │
  │  GET /api/sessions/{tid}/connection                           │             │
  │─────────────────────────────────▶│                            │             │
  │                                  │ 1 DB read, mint JWT        │             │
  │   200 {ws_url, token, expires_at}│                            │             │
  │◀─────────────────────────────────│                            │             │
  │                                                                              │
  │  wss://api.example.com/p/{tid}/ws?t={jwt}                                    │
  │──────────────────────────────────────────────────▶ Traefik ─────────────────▶│  validate JWT
  │◀──────────────────────── WS frames flow direct ─────────────────────────────│
```

Target reconnect latency: under 100 ms wall-clock (one HTTP round-trip + one WS handshake).

### What changes, what stays

| Concern | Before | After |
|---------|--------|-------|
| WS data path | Browser → Orchestrator (Python proxy loop) → Pod | Browser → Traefik → Pod |
| Reconnect latency | ~3 s (happy) to ~20 s+ (stale-binding cascade) | ~100 ms |
| Orchestrator restart | All active WS dropped | No effect on active WS |
| WS auth | BFF cookie validated at orchestrator | Short-lived JWT validated at pod |
| On-demand provisioning trigger | Inside the WS handshake (blocks up to 480 s) | Inside the REST `prepare` endpoint (long-running OK, status via SSE) |
| In-flight notification events | Inspected per-frame in orchestrator proxy loop | Published by agent pod to NATS; orchestrator subscribes |
| Notification feed (SSE) | Driven by orchestrator's WS proxy inspection | Driven by NATS subscription in nats_bridge |
| Per-pod routes | None (single static `/ws/persistent/*` route in front of orchestrator) | One Ingress per bound session pod, K8s owner-ref'd to the pod |
| RBAC for orchestrator SA | (existing) | + `networking.k8s.io/v1/ingresses` create/get/list/delete |

## Component details

### Orchestrator: `orchestrator/routers/sessions.py` (new module)

Hosts two REST endpoints. Mounted by `main.py` via `app.include_router`.

```
POST /api/sessions/{thread_id}/prepare
  Auth: BFF cookie (same as existing endpoints)
  Body: { "config_name": str (optional), "config_override": {…} (optional) }
  Behavior: returns 202 immediately, starts provisioning asynchronously.
            Idempotent via Postgres advisory lock on thread_id — concurrent
            calls for the same thread block until the first finishes, then
            return its result.
  Response 202: { "state": "provisioning" }
  Progress: live phase updates published to the existing SSE notification feed
            on subject "session.lifecycle.{thread_id}".
            Events: provisioning → booting → ready (or failed).
            Events carry only state, not credentials.
  Rationale for async + SSE: 480s blocking HTTP requests would exceed
  Cloudflare's default 100s edge timeout and most browser fetch timeouts.

GET /api/sessions/{thread_id}/connection
  Auth: BFF cookie
  Side effects: none (read-only). Source of truth for the connection payload.
  Response 200: { "state": "ready", "ws_url": str, "token": str, "expires_at": int }
  Response 404: session is not bound (cockpit must call prepare instead)
  Response 409: pod is unhealthy or rebinding (cockpit retries prepare)
  Response 425: state is provisioning/booting (cockpit subscribes to SSE for "ready" then re-polls)

Cockpit flow:
  - Cold start: subscribe to SSE → POST /prepare → wait for "ready" SSE event →
                GET /connection → open WS at returned ws_url.
  - Warm reconnect: GET /connection → open WS.
  Same /connection endpoint serves both paths. One token-mint code path, one
  cockpit consumer code path.
```

Both endpoints reuse the same `{ws_url, token, expires_at}` shape so the cockpit code path is unified.

### Orchestrator: `orchestrator/services/session_router.py` (new module)

Single responsibility: keep a K8s Ingress resource in sync with the agent-binding state.

Public API:
```
async def ensure_route(thread_id: str, pod_name: str, pod_namespace: str) -> str
    # Idempotent. Creates the Service AND the Ingress if missing, returns
    # the path prefix. Both resources are ownerReference'd to the pod so
    # K8s GC deletes them when the pod is gone, even if explicit cleanup
    # is skipped (e.g., orchestrator crash between bind and teardown).

async def teardown_route(thread_id: str) -> None
    # Idempotent. Deletes the Ingress and Service. Safe to call multiple times.
```

Two resources per session, both owner-ref'd to the agent pod so K8s GC handles cleanup even if explicit teardown is skipped. Pod names already include a UUID suffix from the existing provisioner, so the Service and Ingress are named per-session (`session-{thread_id}`) rather than per-pod, and select the pod via its `srw.io/thread-id` label.

```yaml
# Service — selects the agent pod for this thread
apiVersion: v1
kind: Service
metadata:
  name: session-{thread_id}
  namespace: {orchestrator-namespace}
  labels:
    srw.io/thread-id: "{thread_id}"
    srw.io/managed-by: orchestrator
  ownerReferences:
    - apiVersion: v1
      kind: Pod
      name: {actual-pod-name}     # provided by the provisioner
      uid: {pod-uid}
      controller: false
      blockOwnerDeletion: false
spec:
  type: ClusterIP
  selector:
    srw.io/thread-id: "{thread_id}"
  ports:
    - port: 8001
      targetPort: 8001
---
# Ingress — path-based routing under the existing API hostname
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: session-{thread_id}
  namespace: {orchestrator-namespace}
  labels:
    srw.io/thread-id: "{thread_id}"
    srw.io/managed-by: orchestrator
  annotations:
    # Traefik-specific tuning. nginx-ingress and others use different
    # annotations; documented per controller in the chart README.
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
  ownerReferences:
    - apiVersion: v1
      kind: Pod
      name: {actual-pod-name}
      uid: {pod-uid}
      controller: false
      blockOwnerDeletion: false
spec:
  ingressClassName: traefik       # resolved from sessionRouter.ingressClass
  rules:
    - host: api.{global-domain}
      http:
        paths:
          - path: /p/{thread_id}
            pathType: Prefix
            backend:
              service:
                name: session-{thread_id}
                port:
                  number: 8001
```

The provisioner needs to stamp the agent pod with a `srw.io/thread-id` label on creation so the Service selector can find it. This is a small addition to the existing pod template.

### Orchestrator: `orchestrator/services/session_tokens.py` (new module)

JWT mint and validate. Signed with HS256; the shared secret is mounted as a K8s Secret read by both the orchestrator and each agent pod. Claims:

```json
{
  "sub": "{user_id}",
  "tid": "{thread_id}",
  "aud": "agent",
  "iat": 1716000000,
  "exp": 1716000060,
  "jti": "{uuid}"
}
```

- `exp` is 60 seconds by default, configurable via env var. Short enough that a leaked token from a logged WS URL is uninteresting; long enough to survive ordinary clock skew.
- The pod accepts the token on the WS handshake only. Once the WS is open, no further token check is needed — the connection itself is the live auth context. Long-lived sessions (>60 s) do not re-mint.

Open implementation question for the planning step: secret rotation strategy (single secret vs. key-ID rotation with two valid signing keys). Default for v1: single secret, rotate via Helm value change + rolling restart.

### Orchestrator: `orchestrator/services/nats_bridge.py` (modified)

Adds a subscription to `session.events.{thread_id}` (or wildcard `session.events.>`). Handler maps incoming NATS messages to the existing `notification_feed.broadcast()` call. Conceptually the same fan-out logic that `_inspect_session_event` does today — just driven by NATS rather than by inline WS parsing.

The `_inspect_session_event` and `_inspect_browser_event` helpers in `main.py:13676–13744` are deleted once this subscription is live.

### Agent pod: WS handler

Modifications to the persistent-agent FastAPI app (`/ws/chat`):

1. Read the `t` query parameter on the WS handshake. Validate as a JWT signed with the shared secret. Reject with 4401 on missing/invalid token, 4403 if `tid` doesn't match the pod's bound thread.
2. After accepting the WS, behavior is unchanged from today's pod-side code (the orchestrator's proxy was transparent to the pod).
3. When the agent emits notification-worthy messages (`permission.request`, `vm_upgrade.needed`, `approve`, `deny`, `ready`), additionally publish them to NATS subject `session.events.{thread_id}` with a payload mirroring what `_inspect_session_event` produces today.

The IDE proxy WS half (code-server) gets the same JWT-on-handshake treatment but does not need the NATS publish — there's no notification inspection on that path.

### Cockpit: `persistent-chat.service.ts`

New connect flow:

```
Cold start (no agent bound yet):
  1. Open SSE subscription for session.lifecycle.{tid}
  2. POST /api/sessions/{tid}/prepare → 202
  3. Render progress UI from SSE phase events (provisioning, booting)
  4. On SSE "ready" event: GET /api/sessions/{tid}/connection
  5. Open WS at returned ws_url with token query param

Warm reconnect:
  1. GET /api/sessions/{tid}/connection
  2. Open WS at returned ws_url with token query param

Both paths converge on step "GET /connection → open WS". The cockpit only has
one code path for token consumption.

WS error handling:
  - On 4401 (expired/invalid token): drop token, GET /connection, retry WS
  - On 4403 (thread access denied): terminal, surface to user
  - On other close codes: existing F4 reconnect engine (backoff, banner, retry)
```

The F4 reconnect engine itself is unchanged — only the URL-resolution step gains the HTTP call. The IDE proxy (`ide.service.ts` or equivalent) ships with the same connect-flow refactor in the same release.

## main.py extraction

This refactor is a useful wedge into the [[orchestrator_main_py_monolith]] problem. The following code moves out of `main.py`:

| Lines today | New home |
|-------------|----------|
| `persistent_ws_proxy` (`main.py:13747–14063`) | Deleted. Replaced by `routers/sessions.py`. |
| `ide_proxy_ws` (`main.py:7852–7945`) | Deleted. Replaced by an IDE variant in `routers/sessions.py` (or a sibling `routers/ide.py`). |
| `_inspect_session_event` / `_inspect_browser_event` (`main.py:13676–13744`) | Deleted. Logic moves to the agent pod's NATS publisher + the nats_bridge subscription. |
| `_send_session_attach`, idle-pool lookup helpers used in the WS proxy | Moved to `services/session_router.py` or kept in `services/agent_provisioner.py` where they belong. |

Net delta in `main.py`: roughly -350 lines. Net new module surface: ~600 lines spread across three focused modules, each individually testable.

## RBAC changes

Orchestrator's ServiceAccount gains:

```yaml
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "create", "delete", "patch"]
- apiGroups: [""]
  resources: ["services"]
  verbs: ["get", "list", "create", "delete", "patch"]
```

(Services rule may already exist for other reasons; verify during implementation.)

## Configuration

New environment variables:

| Variable | Component | Default | Purpose |
|----------|-----------|---------|---------|
| `SESSION_JWT_SECRET` | Orchestrator, agent pod | (required, from K8s Secret) | HS256 signing key |
| `SESSION_JWT_TTL_S` | Orchestrator | `60` | JWT lifetime in seconds |
| `SESSION_INGRESS_HOST` | Orchestrator | derived from `global.domain` | Hostname for session Ingress rules |
| `SESSION_INGRESS_CLASS` | Orchestrator | `traefik` (configurable) | Sets `spec.ingressClassName` |
| `SESSION_INGRESS_ANNOTATIONS` | Orchestrator | (project default) | JSON of controller-specific annotations |

New Helm values:

```yaml
sessionRouter:
  ingressClass: traefik         # Tier 1; other controllers documented but untested
  jwtTtlSeconds: 60
  jwtSecretName: srw-session-jwt
  annotations: {}               # Extra annotations to merge into per-session Ingress
```

No master enable/disable switch: hard cutover means the legacy WS proxy is gone in the same release. Rollback is by chart version, not by Helm value.

## Migration path

**Hard cutover.** Cockpit, orchestrator, and agent pod are updated together in one release. The legacy `/ws/persistent/{thread_id}` WS handler and the IDE WS proxy are deleted in the same change. No dual-path code, no feature flag.

The project has no production environments yet (decided 2026-05-22), so the typical "preserve in-flight sessions during upgrade" concern doesn't apply. Active dev-cluster sessions reconnect once when the orchestrator + cockpit roll, which is the same impact a normal restart has today.

## Security considerations

- **JWT replay.** Tokens are valid for 60 s. A leaked token can be reused only within that window, and the session it opens is tied to a specific thread. Add `jti` claim and a small bounded LRU on the pod to reject duplicate `jti`s within the TTL window. (Optional — re-evaluate based on actual threat model.)
- **Pod-side auth correctness.** The agent pod must validate the `tid` claim against its own bound thread, not blindly trust the URL path. This is the bug the BFF cookie change closed earlier — preserve it.
- **TLS termination.** Browser → Traefik is TLS; Traefik → pod is in-cluster HTTP. Same trust boundary as today's Orchestrator → pod hop. No new exposure.
- **WS URL leakage.** The `ws_url` and short-lived token are not sensitive once expired, but the cockpit should still avoid logging them.
- **Notification publishing as a privilege.** Agent pods publishing to `session.events.{thread_id}` must only publish for *their* `thread_id`. Subject-level ACLs on NATS, if available; otherwise validated in the bridge subscriber. (Same trust assumption as today — the agent pod has full credentials anyway.)

## Customer-impact summary

- **Helm chart prerequisite, documented:** an ingress controller that supports WebSocket upgrades and watches `networking.k8s.io/v1 Ingress` resources. Traefik (default on k3s), nginx-ingress, HAProxy ingress, Istio Gateway, and Contour all qualify. The project's own deployment uses Traefik.
- **No new component bundled in the chart.** No new pods, no new services, no new ports.
- **One new K8s Secret** to roll out (`srw-session-jwt`). Standard secret lifecycle.
- **One additional RBAC grant** on the orchestrator's existing Role.
- **External edge proxy choice is unconstrained.** Cloudflare Tunnel, Zoraxy, plain nginx, Caddy — any HTTP/HTTPS reverse proxy that supports WebSocket upgrades works in front of the cluster ingress. TLS termination can be at the external edge or at Traefik; the design doesn't care.

## Out of scope (parked)

- **Go rewrite of the orchestrator.** Separate effort. The two new REST endpoints and the IngressRoute lifecycle logic are small enough to carry over cleanly when that happens.
- **Replacing Traefik / introducing Envoy / configurable-http-proxy / a custom Go router.** Theoretical scaling benefits do not justify the operational cost at the project's current scale.
- **Browser-side Tailscale / WebRTC / peer-to-peer transport.** Not needed for this problem.
- **The IDE HTTP proxy half** (per-request HTTP to code-server). One-off requests, fits the "orchestrator handles short requests" principle. Stays in the orchestrator for now.
- **The Workspace / Gitea HTTP proxy.** Same reasoning.
- **The headscale tailnet.** Already in use for agent-pod → VM SSH. Not extended here.

## Decisions

All 8 implementation-shape questions raised during brainstorming were settled on 2026-05-22.

1. **Migration: hard cutover.** No production environments yet, no dual-path/feature-flag overhead. Cockpit + orchestrator + agent pod ship the new flow in one release; legacy WS handlers deleted in the same change.

2. **JWT rotation: single shared secret, change-and-roll.** Rotation events are rare; a brief reconnect during controlled rollout is acceptable. Key-ID rotation deferred until production scale justifies the complexity. The JWT is *not* the same thing as the BFF cookie or API tokens — it's a short-lived (60s) orchestrator-signed credential authorizing one WS handshake to one thread on one pod. New infrastructure for a new trust boundary, not a reuse of existing user-auth.

3. **K8s API test strategy: unit tests with mocked `kubernetes` client + integration tests on the dev cluster.** Mocked unit tests cover ensure/teardown idempotency, ownerReference shape, label correctness, error handling. Dev-cluster integration tests cover the actual Ingress reconciliation by Traefik and the JWT-auth handshake end-to-end.

4. **NATS subjects + ACL model:**
   - Subjects: `session.events.{thread_id}` (agent-published notification events) and `session.lifecycle.{thread_id}` (provisioning phase events, published by orchestrator and consumed by cockpit SSE).
   - Trust model: keep the existing NATS trust posture — agent pods publish on shared credentials, no per-pod subject ACLs. Defense-in-depth: the bridge subscriber matches the event's payload `thread_id` against the pod's bound thread (from DB) and drops mismatches.
   - This is a knowingly-deferred security gap. Tracked in [[nats_subject_acl_hardening]] for follow-up.

5. **Ingress controller support: Tier 1 = Traefik (tested in CI + dev).** nginx-ingress, HAProxy ingress, Istio Gateway, and Contour are Tier 2 — documented in the chart README with annotation recipes but not exercised by the project's own test suite.

6. **`prepare` idempotency: Postgres advisory lock on `thread_id`.** No HTTP `Idempotency-Key` header. Reuses the same advisory-lock pattern recommended in [[persistent_thread_double_provisioning_race]] — same fix, same place, no new concept. A retried `POST /prepare` for the same thread blocks until the in-flight call finishes provisioning, then returns the result of that call (state=ready, or state=failed with reason).

7. **`prepare` async-completion shape: cockpit polls `GET /connection` after SSE "ready" event.** Same endpoint serves cold-start and warm-reconnect — single token-mint code path on the orchestrator, single token-consumer code path on the cockpit. The 50 ms extra round trip is negligible against the ~40 s provisioning wait. SSE delivers state only, never credentials; `/connection` is the source of truth for the connection payload.

8. **IDE WS DEFERRED to a follow-up.** Original decision was "same release" but execution surfaced a real protocol mismatch: the cockpit doesn't construct the IDE WS itself — it receives `code_server_url` from the orchestrator and does `window.open(code_server_url)`. The IDE pod runs **code-server** (vendor code), so we can't add JWT validation to its `/ws` endpoint the same way we did for `persistent_app`'s `/ws/chat`. Refactoring this path requires either an auth-proxy sidecar in the workspace pod, Traefik ForwardAuth middleware, or another design choice — none of which are in scope for this iteration. The orchestrator's `ide_proxy_ws` handler in `main.py:7852–7945` and `ide_proxy_http` at `7771–7833` stay in place for now. Persistent-agent WS ships direct; IDE stays through the orchestrator until a follow-up.

## Related work

- [[orchestrator_main_py_monolith]] — broader extraction effort this contributes to
- [[persistent_thread_double_provisioning_race]] — provisioning race this design directly addresses via the advisory-lock pattern in `prepare`
- [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]] — reducer bug closely tied to the reconnect protocol
- [[persistent_chat_silent_disconnect]] — heartbeat/watchdog design that pairs with this
- [[nats_subject_acl_hardening]] — deferred follow-up: enforce publisher scope at the NATS transport layer
- [[auth_bff_and_api_tokens]] — BFF cookie model the JWT layers on top of
- [[external_headscale]] — current tailnet usage; explicitly not extended here
- [[high_availability_setup]] — orchestrator HA story this design unblocks
