# Dynamic Canvas Slice 3B — isolated ordinary-HTTP viewer verification

**Status:** The default-off one-port viewer checkpoint and its trusted Cockpit
closeout are implemented. The original checkpoint passed repository
verification and the enclosing chart deploys cleanly through local k3d/Tilt; a
local production-bundle Playwright conformance harness defines the same wrapper
and synthetic browser-visible protocol cases for Chromium, Firefox, and WebKit.
Chromium and Firefox pass on the host; WebKit passes in the exact official
Playwright Ubuntu Noble container after the Fedora-native launch hit an ABI
mismatch, as recorded below. It
remains dark-shipped: the chart creates no wildcard Ingress/TLS resource,
viewer gates remain disabled, and this record does not claim a live Python-
gateway/TLS/CHIPS/PSL or enabled live-app acceptance.

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
WebSockets/HMR, shared browser, or a chart-owned external edge. The authenticated
wrapper pop-out, trusted reset-origin action, and causal unsupported-browser/
storage fallback are implemented, but remain unavailable to users while the
viewer gates are off.
Both `development-cookie-free` and `psl-isolated` modes strip all
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
| Conditional live-app origin rotation and old-generation revocation | `orchestrator/routers/canvases.py`, `orchestrator/services/canvas.py`, `0061_canvas_viewer_sessions.sql` |
| Dedicated no-fallback ASGI gateway | `orchestrator/canvas_gateway.py` |
| Request/response policy and strict HTTP-over-SSH adapter | `orchestrator/services/canvas_proxy_policy.py`, `orchestrator/services/canvas_http.py`, `orchestrator/services/canvas_ssh.py` |
| Pane-local attachment lifecycle, typed safe fallback, fixed iframe renderer, reset UI, and source/sync chrome | `cockpit/src/app/views/canvas/canvas-viewer.controller.ts`, `canvas-live-app-unavailable.component.ts`, `canvas-live-app-renderer.component.ts`, `canvas-pane.component.ts` |
| Authenticated minimal wrapper and same-browser revision-only invalidation | `cockpit/src/app/views/canvas/canvas-popout-page.component.ts`, `cockpit/src/app/core/services/canvas.service.ts`, `cockpit/src/app/app.routes.ts` |
| Production-bundle three-engine local conformance harness | `cockpit/e2e/canvas/` |
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
- parent attachment creation now requires exact same-origin/same-site CORS Fetch
  Metadata and returns `canvas_browser_unsupported` before creating state when
  that browser boundary is unavailable. A missing transient cookie at exchange
  returns only `canvas_browser_storage_unavailable`; Cockpit maps those two
  causal outcomes to trusted unsupported guidance and leaves generic failures
  non-causal;
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
- the authenticated pop-out renders the same fixed sandbox inside trusted
  Cockpit chrome, opens with `noopener,noreferrer`, and never top-level-navigates
  to the user-content origin. Conditional reset-origin preserves the app
  pointer, rotates the origin generation under the state ETag, and retires the
  previous generation through the existing cross-replica revocation trigger;
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
- **2026-07-14 trusted-UI/browser closeout:** the focused backend/security suite
  passed 367 tests and Ruff passed. The full Cockpit run passed 1,085 tests in
  86 files; TypeScript compilation, all 1,993 English/German i18n keys, and the
  production build passed with only the existing budget/CommonJS warnings.
  Playwright listed 27 cases and passed all nine in host Chromium plus all nine
  in host Firefox in 27.9 seconds. The Ubuntu WebKit bundle could not launch
  natively on Fedora 44 because the host lacked its pinned `libicu74` and
  `libjpeg-turbo8` ABIs. Running the same production tree in the exact
  `mcr.microsoft.com/playwright:v1.59.0-noble` image passed all nine WebKit
  cases in 19.4 seconds. Main and Develop CI now install all three engines with
  Ubuntu system dependencies and run the same production bundle, but that new
  CI job has not yet supplied acceptance evidence.

No deployed real-boundary or enabled local-k3d live-app acceptance is claimed.
The new Playwright suite defines Chromium, Firefox, and WebKit projects against
the production Angular bundle; the local evidence above comes from Chromium and
Firefox on the host and WebKit in the matching Linux container. Its BFF is an
in-process fixture and its HTTPS viewer is supplied by request interception.
The supported viewer path explicitly emulates partitioned-cookie persistence;
the forced missing-cookie case disables that emulation and proves only the
trusted fallback branch. It therefore does not prove the
Python gateway/SSH path, public DNS/TLS, a real CHIPS/third-party-cookie policy,
effective PSL separation, cross-replica PostgreSQL cancellation, installed
service workers/PWA mode, or shipping Safari. The local chart intentionally
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
host dispatch, pre-gateway rate limits, and rerun the authentication/security
matrix against that deployed Python gateway. The repository-owned trusted
Cockpit/BFF/MCP response seams now deny framing without weakening existing CSP;
the production edge, installed-PWA upgrade/browser self-navigation behavior,
optional root-host routes, same-origin code-server webviews on the distinct API
authority, and Keycloak policy must still be black-box verified before that
launch gate is attested.
Browsers which cannot satisfy the embedded authentication flow must show
unsupported UX rather than receive a top-level fallback.

The restricted gateway credential/configuration work, trusted Cockpit closeout,
and local browser harness are complete. The next acceptance step is a hosted,
raw-path-preserving one-port run with real secure-context cookies, followed by
current Safari/iOS and installed-PWA/device coverage. After the external
Slice-3 launch gates are evidenced, Slice 4 can
add the validated multi-port route manifest, SSE, and WebSocket/HMR on the same
isolated-origin and revocation foundation.
