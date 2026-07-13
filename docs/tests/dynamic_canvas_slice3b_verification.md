# Dynamic Canvas Slice 3B — isolated ordinary-HTTP viewer verification

**Status:** The default-off one-port viewer-session, gateway, ordinary-HTTP
proxy, Cockpit renderer, and deployment checkpoint is implemented and passed
repository verification on 2026-07-13. It is intentionally dark-shipped: the
chart creates no wildcard Ingress/TLS resource, defaults remain disabled, and
this record does not claim production browser acceptance or local-k3d exposure.

**Feature:** `docs/features/dynamic_canvas.md`

## What this checkpoint proves

- an authorized parent Cockpit session can create, renew, and close a
  non-credential iframe attachment bound to one exact Canvas state and one
  per-source origin generation;
- a one-time bootstrap becomes a hashed, short-lived, host-only viewer session
  without putting the reusable session secret in PostgreSQL, Cockpit state, or
  a post-bootstrap URL;
- ordinary browser HTTP is validated, completely spooled, and reframed before
  a request-scoped pinned SSH direct channel sends bytes to the selected
  workspace-loopback port;
- the gateway rebuilds the upstream request and browser response under its own
  cookie-free, no-store, CSP, Permissions Policy, framing, size, timeout,
  redirect, and header contract;
- PostgreSQL notification accelerates revocation while bounded database
  revalidation, active-operation registration, client-disconnect propagation,
  and bounded transport close make missed notifications and stalled peers fail
  closed;
- Cockpit accepts only the server-minted default-port HTTPS bootstrap on the
  configured suffix with one canonical UUID label, exact bootstrap path, and
  one well-formed token, then mounts it in a fixed sandbox beside a persistent
  untrusted-app warning; and
- Helm and Compose can render the dedicated internal gateway only behind
  explicit gates and network selectors, without publishing a listener or
  wildcard edge by default.

## Deliberate boundary

This checkpoint is one-port ordinary HTTP. It does **not** implement a
multi-port route manifest, application cookies, SSE, multipart live streams,
WebSockets/HMR, a wrapper pop-out, shared browser, or a chart-owned external
edge. Both `development-cookie-free` and `psl-isolated` modes strip all
application `Cookie` and `Set-Cookie` fields; the latter is a production
configuration precondition, not a claim that app-cookie support has shipped.

The opaque viewer cookie may remain in the browser only until the immutable
parent BFF session's absolute expiry. PostgreSQL's shorter renewable
origin-session expiry remains the authorization boundary on every request.
Mode changes and removal of the stored Cockpit embedding origin revoke existing
credentials rather than waiting for their normal expiry. Pending attachments
persist their issuance cookie mode and embedding origin, so a rollover also
invalidates an unconsumed bootstrap.

## Delivered implementation surfaces

| Surface | Delivered location |
|---|---|
| Hashed origin sessions, attachments, bootstraps, expiry indexes, and revocation triggers | `orchestrator/database/migrations/app/0061_canvas_viewer_sessions.sql` |
| Fail-closed domain/profile/cookie-mode and timing configuration | `orchestrator/services/canvas_viewer_config.py` |
| Create/consume/authenticate/renew/close/revoke/cleanup semantics | `orchestrator/services/canvas_viewer_sessions.py` |
| Cross-replica notification listener and bounded active-operation registry | `orchestrator/services/canvas_session_notifications.py` |
| Parent BFF attachment routes and public capability | `orchestrator/routers/canvases.py`, `orchestrator/services/canvas.py` |
| Dedicated no-fallback ASGI gateway | `orchestrator/canvas_gateway.py` |
| Request/response policy and strict HTTP-over-SSH adapter | `orchestrator/services/canvas_proxy_policy.py`, `orchestrator/services/canvas_http.py`, `orchestrator/services/canvas_ssh.py` |
| Pane-local attachment lifecycle and fixed iframe renderer | `cockpit/src/app/views/canvas/canvas-viewer.controller.ts`, `canvas-live-app-renderer.component.ts`, `canvas-pane.component.ts` |
| Default-off gateway service/deployment/network policy and Compose profile | `helm/templates/canvas-gateway/`, `docker-compose.yaml`, `docker-compose.local.yaml` |

## Security review closures

The final review found and closed the following implementation gaps before this
record was written:

- browser-cookie retention is bounded by the parent BFF absolute lifetime while
  every request still checks the shorter server lease; a fresh authorized
  bootstrap can reissue the same validated host cookie without storing its
  plaintext, and renewal cannot resurrect an expired origin session;
- renewal takes locks in Canvas-then-session order, avoiding a replacement/
  revocation-trigger deadlock;
- authentication and renewal revoke sessions minted under a different cookie
  mode or a removed Cockpit embedding origin, and bootstrap consumption rejects
  a pending attachment minted under either superseded policy;
- the bootstrap returns a nonce-bound, server-owned transition document which
  replaces itself with the clean app entry path; ordinary traffic then requires
  `Sec-Fetch-Site: same-origin`, preventing a legacy browser which ignores
  `Partitioned` from sending an unpartitioned viewer cookie on attacker-site
  GET/HEAD subrequests;
- hourly BFF cleanup includes expired Canvas viewer state;
- a missed notification cannot make a long request fail open: the sleep and
  database query share the configured revalidation interval, and timeout or
  database failure cancels the exchange;
- initial authorization, per-session HTTP activity, total activity, request/
  response bytes, and all major waits have bounded admission or timeouts;
- disconnect monitoring starts after request-body spooling and remains active
  through SSH connection, headers, and response streaming; SSH/HTTP close waits
  are bounded;
- ordinary top-level navigation is rejected, gateway CSP includes a
  non-relaxable sandbox, and Cockpit never falls back to opening the app on a
  trusted top-level origin; and
- shared viewer cookies are cleared only for credential failures, so an
  attacker cannot log out a shared origin session by inducing an unrelated
  capacity, framing, or upstream failure.

## Automated verification

- **Python/proxy:** 234 focused Canvas gateway, strict HTTP, proxy policy,
  viewer-session, SSH transport, Slice 0–3, infrastructure, and tool tests
  passed with one expected opt-in PostgreSQL-test skip and 10 dependency
  deprecation warnings. The skipped test passed separately against the
  disposable migrated PostgreSQL instance described below.
- **PostgreSQL lifecycle:** a disposable fully migrated PostgreSQL 15 database
  passed a two-service-instance create → concurrent single-use bootstrap →
  authenticate → shared-session reuse → renew/close → trigger/`NOTIFY`
  revocation test. The same test is wired into the migration CI job rather than
  relying on mocks in the normal database-free suite.
- **Migration:** all application migrations replayed successfully through
  `0061`; `scripts/schema-snapshot.sh app --check` matched the 4,064-line
  generated `schema_current.sql`. Squawk 2.59.0 reported zero issues for
  migration `0061`.
- **Cockpit:** 26 targeted renderer/viewer tests and the full 1,058-test Vitest
  suite passed. The production build and i18n check passed; German contains all
  1,966 English keys. Canvas stylesheet lint passed. Repository-wide Stylelint
  still reports 68 unrelated baseline errors, with no Canvas file in the
  failures.
- **Deployment:** 8 Canvas infrastructure tests passed. Required Helm lint
  overlays, default and enabled renders, and both Compose files with and without
  the `canvas-viewer` profile passed. The disabled chart renders no gateway or
  Canvas Ingress; the enabled render requires non-empty edge namespace and pod
  selectors.
- **Static:** focused Ruff lint/format and `git diff --check` passed after the
  final security-review fixes.

No real-browser or local-k3d live-app acceptance is claimed. jsdom cannot prove
partitioned-cookie, Fetch Metadata, CSP/sandbox, service-worker, navigation, or
credential-leakage behavior, and the local chart intentionally lacks the
required isolated wildcard edge.

## Production launch gates and next step

Before enabling the viewer, provide the separately registrable wildcard
user-content DNS/TLS edge, effective private PSL boundary, verified raw-path
host dispatch, pre-gateway rate limits, and the full Chromium/Firefox/WebKit
plus Safari/iOS/PWA security matrix. Replace the shared application database
credential and broad ConfigMap projection with a least-privilege Canvas role
and explicit gateway-only configuration. Browsers which cannot satisfy the
embedded authentication flow must show unsupported UX rather than receive a
top-level fallback.

The next repository step is that production-browser harness and restricted
gateway credential/configuration work. After those Slice-3 launch gates are
evidenced, Slice 4 can add the validated multi-port route manifest, SSE, and
WebSocket/HMR on the same isolated-origin and revocation foundation.
