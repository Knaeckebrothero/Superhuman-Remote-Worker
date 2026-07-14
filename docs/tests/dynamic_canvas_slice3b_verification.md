# Dynamic Canvas Slice 3B — isolated ordinary-HTTP viewer verification

**Status:** The default-off one-port viewer checkpoint is implemented, passed
repository verification, and the enclosing chart now deploys cleanly through
local k3d/Tilt. It remains dark-shipped: the chart creates no wildcard
Ingress/TLS resource, viewer gates remain disabled, and this record does not
claim production-browser or enabled live-app acceptance.

**Feature:** `docs/features/dynamic_canvas.md`

## What this checkpoint proves

- an authorized parent Cockpit session can create, authorize, renew, and close a
  non-credential iframe attachment bound to one exact Canvas state and one
  per-source origin generation;
- the iframe URL carries only a canonical attachment UUID; an exact Cockpit
  `WindowProxy`/origin bridge authorizes a gateway challenge through the exact
  parent BFF session, and a browser-bound same-origin exchange becomes a
  short-lived host-only viewer session without putting plaintext secrets in
  PostgreSQL or any URL;
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
  matching canonical attachment locator; it binds the iframe window before
  navigation and does not report ready until the exact authenticated receipt,
  while retaining a fixed sandbox and persistent untrusted-app warning; and
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
| Hashed origin sessions, attachments, bootstrap challenge/exchange state, expiry indexes, and revocation triggers | `orchestrator/database/migrations/app/0061_canvas_viewer_sessions.sql`, `0062_canvas_bootstrap_exchange.sql` |
| Fail-closed domain/profile/cookie-mode and timing configuration | `orchestrator/services/canvas_viewer_config.py` |
| Create/challenge/authorize/exchange/authenticate/renew/close/revoke/cleanup semantics | `orchestrator/services/canvas_viewer_sessions.py` |
| Cross-replica notification listener and bounded active-operation registry | `orchestrator/services/canvas_session_notifications.py` |
| Parent BFF create/authorize/renew/close routes and public capability | `orchestrator/routers/canvases.py`, `orchestrator/services/canvas.py` |
| Dedicated no-fallback ASGI gateway | `orchestrator/canvas_gateway.py` |
| Request/response policy and strict HTTP-over-SSH adapter | `orchestrator/services/canvas_proxy_policy.py`, `orchestrator/services/canvas_http.py`, `orchestrator/services/canvas_ssh.py` |
| Pane-local attachment lifecycle and fixed iframe renderer | `cockpit/src/app/views/canvas/canvas-viewer.controller.ts`, `canvas-live-app-renderer.component.ts`, `canvas-pane.component.ts` |
| Default-off gateway service/deployment/network policy and Compose profile | `helm/templates/canvas-gateway/`, `docker-compose.yaml`, `docker-compose.local.yaml` |
| Trusted Cockpit/BFF document anti-framing, including optional root-host MCP OAuth pages | `orchestrator/security/anti_framing.py`, `docker/cockpit-nginx.conf`, `cockpit/angular.json`, `orchestrator/mcp/run.py` |

## Security review closures

The final review found and closed the following implementation gaps before this
record was written:

- browser-cookie retention is bounded by the parent BFF absolute lifetime while
  every request still checks the shorter server lease; a fresh authorized
  exchange can reissue the same validated host cookie without storing its
  plaintext, and renewal cannot resurrect an expired origin session;
- a transferable `?token=` bootstrap was removed. The URL now contains only the
  attachment UUID; purpose-separated challenge, browser-binding, ready-receipt,
  bridge, exchange, and session hashes keep every plaintext credential out of
  PostgreSQL and URLs;
- the bridge requires the exact parent BFF session, user/thread owner, Cockpit
  `Origin`, iframe `WindowProxy`, Canvas origin, attachment, closed schema,
  challenge, and receipt. A copied URL and the same user under another BFF
  session cannot authorize it;
- the gateway sets one attachment-specific transient HttpOnly partitioned
  cookie, accepts the exchange only as a strictly framed same-origin browser
  POST, atomically consumes it across replicas, clears the transient value, and
  only then issues/reuses the viewer cookie;
- renewal takes locks in Canvas-then-session order, avoiding a replacement/
  revocation-trigger deadlock;
- authentication and renewal revoke sessions minted under a different cookie
  mode or a removed Cockpit embedding origin, and every bootstrap phase rejects
  a pending attachment minted under either superseded policy;
- the bootstrap returns a nonce-bound, server-owned handshake document which
  reports authenticated readiness before replacing itself with the clean app
  entry path; ordinary traffic then requires `Sec-Fetch-Site: same-origin`,
  preventing a legacy browser which ignores `Partitioned` from sending an
  unpartitioned viewer cookie on attacker-site GET/HEAD subrequests;
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
  capacity, framing, or upstream failure;
- Cockpit production and development documents, orchestrator/BFF HTTP
  responses, and optional MCP OAuth/error documents now independently enforce
  `frame-ancestors 'none'` and `X-Frame-Options: DENY`. The ASGI boundary
  appends rather than replaces route-owned CSP and normalizes conflicting
  legacy fields to `DENY`. It also starts a protected response before
  Starlette's outer error renderer handles an unhandled exception, closing the
  otherwise headerless `500` path; and
- the anti-framing boundary permits VS Code webviews only with
  `frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN` on the exact
  separately configured API/IDE authority. It canonicalizes explicit default
  ports and trailing DNS dots while missing/duplicate Host fields fail closed.
  A request for the same `/api/ide/...` path on a Cockpit authority remains
  denied, so compatibility does not reopen the Canvas self-navigation escape.
  A same-authority Cockpit/API deployment therefore intentionally disables IDE
  webview framing and is not a supported production topology.

## Automated verification

- **Python/Canvas:** 260 gateway, strict-HTTP, proxy-policy, viewer-session,
  SSH, Slice 0–3, infrastructure, tool, and client tests passed with the one
  expected opt-in database skip and 10 dependency deprecation warnings. The
  gateway/viewer subset contains 41 direct handshake regressions; Ruff and the
  diff check pass.
- **PostgreSQL lifecycle:** a disposable fully migrated PostgreSQL 15 database
  passed the updated two-service-instance create → challenge → exact-parent
  authorize → concurrent single-use exchange → authenticate → shared-session
  reuse → renew/close → trigger/`NOTIFY` revocation test. The run also exposed
  and closed the consumed-bootstrap foreign-key deletion edge case. The same
  test is wired into migration CI rather than relying on mocks in the normal
  database-free suite.
- **Migration:** all application migrations replayed successfully through
  `0062`; `scripts/schema-snapshot.sh app` regenerated the 4,079-line canonical
  `schema_current.sql` from that clean replay.
- **Cockpit:** 36 focused protocol/controller/renderer/URL tests and the full
  1,068-test Vitest suite passed. The production Angular build passed with only
  the existing bundle-budget/CommonJS warnings.
- **Deployment:** 9 Canvas infrastructure tests and both required Helm lint
  overlays, default and enabled renders, and both Compose files with and without
  the `canvas-viewer` profile passed. The disabled chart renders no gateway or
  Canvas Ingress; the enabled render requires non-empty edge namespace and pod
  selectors.
- **Static:** focused Ruff lint/format and `git diff --check` passed after the
  final security-review fixes.
- **Trusted-parent anti-framing follow-up:** 15 focused middleware/configuration
  tests passed, including existing and duplicate mixed-case CSP composition,
  complete conflicting-XFO replacement, streaming/repeated headers,
  redirects/handled errors, an unhandled `500`, non-HTTP bypass, canonical
  default-port/trailing-dot authority matching, missing/duplicate Host denial,
  narrow-prefix validation, and the same-origin-only IDE policy. The Cockpit
  app shell carries an explicit anti-framing policy marker so this deployment
  changes its service-worker asset hash, and Develop CI now runs the Python
  boundary assertions for Cockpit server/config-only changes. The full
  1,068-test Cockpit suite and production Angular build passed. A live Angular
  dev server returned both headers for `/` and a deep SPA route; stock Nginx
  accepted the checked-in production config and returned both headers on
  normal, SPA fallback, `404`, and conditional `304` responses. The production
  MCP image built successfully and its live HTTP `404` carried the same
  boundary. Related Canvas/IDE/proxy and MCP suites passed, and focused
  Ruff/format/diff checks stayed clean.

No real-browser or enabled local-k3d live-app acceptance is claimed. jsdom
cannot prove partitioned-cookie, Fetch Metadata, CSP/sandbox, service-worker,
navigation, or credential-leakage behavior, and the local chart intentionally
lacks the required isolated wildcard edge. A source-of-truth repair corrected
the chart-created Secret rendering and added a value-safe regression check. The
next rollout exposed checksum drift in already-applied migration `0061`; its
recorded bytes were restored, and the later attachment `cookie_mode` change
remains in forward-only migration `0062`, without editing the migration ledger
or manually patching live resources. The local Helm/Tilt release now deploys
with viewer gates disabled. The chart-managed Secret now survives Tilt's
custom-deploy Force Update and is reclaimed on reinstall; a controlled live
cycle preserved both its Kubernetes UID and complete data digest. Garage
credential pairs are atomic, its bootstrap verifies existing secrets and
revokes stale chart-managed generations, and the live store retained exactly
one managed key per bucket. Secret material was checked only structurally or by
equality and was not disclosed.

The hosted deployment has since reserved `srwcanvas.works` with origins at
`<uuid>.srwcanvas.works`, DNS/TLS wildcard `*.srwcanvas.works`, and intended
private PSL rule `srwcanvas.works`. On 2026-07-13 the proxied wildcard was
published and checked from outside the cluster: authoritative Cloudflare DNS
returned A/AAAA records for a random UUID host, the active certificate covered
the apex and wildcard, and HTTPS reached the expected Cloudflare catch-all
`404`. This is DNS/TLS evidence, not a gateway-route or production-acceptance
claim. Both feature gates and both verification attestations remain false.
Production configuration also rejects a nested host suffix: the suffix must
equal `.` plus the exact attested effective-PSL domain.

The follow-on Slice-3C checkpoint has since removed the gateway's shared
database identity and broad ConfigMap projection. Its separate verification is
recorded in [[dynamic_canvas_slice3c_verification]].

## Production launch gates and next step

Before enabling the viewer, provide the separately registrable wildcard
user-content DNS/TLS edge, effective private PSL boundary, verified raw-path
host dispatch, pre-gateway rate limits, and the full Chromium/Firefox/WebKit
plus Safari/iOS/PWA security matrix. The repository-owned trusted
Cockpit/BFF/MCP response seams now deny framing without weakening existing CSP;
the production edge, installed-PWA upgrade/browser self-navigation behavior,
optional root-host routes, same-origin code-server webviews on the distinct API
authority, and Keycloak policy must still be black-box verified before that
launch gate is attested.
Browsers which cannot satisfy the embedded authentication flow must show
unsupported UX rather than receive a top-level fallback.

The restricted gateway credential/configuration work is complete. The next
repository step is the production-browser harness and explicit unsupported-
browser UX. After the external Slice-3 launch gates are evidenced, Slice 4 can
add the validated multi-port route manifest, SSE, and WebSocket/HMR on the same
isolated-origin and revocation foundation.
