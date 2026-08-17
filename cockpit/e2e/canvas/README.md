# Dynamic Canvas browser conformance harness

This suite loads the **production Cockpit bundle** in Playwright's Chromium,
Firefox, and WebKit engines. It enters through the authenticated
`/sessions/:threadId/canvas` pop-out wrapper and runs the real Angular Canvas
pane, attachment controller, sandboxed iframe, auth interceptor, and logout
flow.

Run it from `cockpit/`:

```bash
npm run test:e2e:canvas:install
npm run test:e2e:canvas
```

On a fresh Linux CI host, install the browser system packages as well:

```bash
npx playwright install --with-deps chromium firefox webkit
npm run test:e2e:canvas
```

`test:e2e:canvas` builds Cockpit first. `test:e2e:canvas:no-build` is only for
iteration against an already-created `dist/cockpit/browser` tree. The fixture
refuses to start without that tree and checks that its trusted-document
anti-framing headers still match `docker/cockpit-nginx.conf`.

## Executable coverage

The in-process BFF/viewer fixture records only header names, cookie names, and
proof-match booleans. It never records synthetic credential values. The suite
exercises:

- the authenticated full-window Canvas wrapper with production-hashed assets;
- exact parent attachment Fetch Metadata and cookie-BFF requests;
- challenge, parent authorization, one-time exchange, and application
  navigation in a sandboxed cross-origin iframe;
- the iframe sandbox, referrer policy, permissions allowlist, gateway CSP, and
  trusted-parent `frame-ancestors 'none'`/`DENY` boundary;
- separation of the parent session and identity-provider cookie names from all
  viewer requests, and HttpOnly isolation of viewer cookies from app script;
- copied bootstrap locators rejected as top-level documents;
- deterministic partitioned-storage loss and the trusted unsupported-browser
  fallback, with no top-level app fallback;
- origin-generation reset, old-attachment retirement, and reconnection on a
  different isolated origin (without claiming synchronous browser-storage
  deletion on the retired origin);
- stalled renewal at hard expiry, authoritative Canvas revocation, parent auth
  expiry, and logout teardown.

The fixture mirrors the browser-visible contracts, but it is deliberately an
**in-process conformance simulation**. Playwright request interception supplies
the synthetic HTTPS viewer responses. A green run does not prove public DNS,
TLS, an effective public-suffix boundary, the deployed gateway/ingress, or a
browser's real third-party-cookie/CHIPS policy.

WebKit tracking prevention does not retain `Set-Cookie` from a
Playwright-fulfilled synthetic third-party response. The WebKit supported-path
fixture therefore simulates only the successful partitioned-cookie plumbing
after observing the real parent Fetch Metadata request. The explicit
missing-cookie test disables that simulation and verifies the trusted fallback.
Neither result proves CHIPS support or substitutes for shipping Safari/iOS
testing.

The config blocks service workers for deterministic test isolation. This means
the production service-worker registration code is present in the tested
bundle, but installation, activation, update behavior, and standalone PWA
windows are outside this suite.

## External launch gates

The following remain deployment/device tests and must not be inferred from a
green local run:

- real wildcard DNS and TLS for `*.srwcanvas.works`, effective PSL separation,
  raw-path-preserving ingress, and the production BFF/gateway/revocation store;
- a real secure-context CHIPS exchange, cookie partitioning by top-level site,
  copied locator under a different signed-in user/session, and origin reset
  across actual gateway replicas;
- hosted CSP/network probes against external fetch, frame, object, worker,
  WebRTC/media, popup, download, form, and self-navigation targets;
- current desktop Safari. Playwright WebKit is useful engine coverage but is
  not the shipping Safari browser;
- physical or device-cloud Safari on iOS/iPadOS and Chromium on Android;
- installed/standalone PWA windows, service-worker update paths, mobile
  focus/inert behavior, touch resizing, and accessibility assistive-technology
  passes;
- production HTTP framing, streaming/SSE, WebSocket/HMR, and multi-port routing
  once those proxy modes are enabled.

Keep external results in the relevant `knowledge-base/knowledge/tests/dynamic_canvas_*_verification.md`
record together with browser/OS versions, deployment revision, and tested
hostname. Do not weaken this harness to make a browser without the required
Fetch Metadata or partitioned-storage behavior appear supported; that browser
must receive the trusted safe fallback instead.
