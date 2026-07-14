---
tags:
  - operations
  - dynamic-canvas
  - edge
  - security
related:
  - "[[dynamic_canvas]]"
  - "[[dynamic_canvas_gateway_database]]"
  - "[[dynamic_canvas_slice3b_verification]]"
---

# Dynamic Canvas hosted-edge rollout

This runbook takes the default-off one-port Canvas viewer from a dark chart
render to hosted acceptance. It does not authorize enabling the feature before
all attestations below are true.

## Selected edge

The production route is:

```text
<uuid>.srwcanvas.works (Cloudflare DNS-only)
  -> public HAProxy relay (TCP/SNI only)
  -> WireGuard
  -> dedicated MetalLB Service
  -> dedicated NGINX Canvas edge
  -> internal srw-canvas-gateway:8086
```

Cloudflare's HTTP proxy and Tunnel are excluded because their mandatory
baseline normalization merges adjacent slashes before the gateway can validate
the raw request target. Cloudflare Spectrum arbitrary TCP is not available on
the current plan. The relay therefore stays layer 4, while NGINX terminates TLS,
validates UUID SNI/Host, preserves the original request URI through a URI-less
`proxy_pass`, rate-limits by the PROXYv2 client address, and emits no URI,
query, cookie, locator, or authorization data in logs.

The homelab-specific Fleet resources and exact relay/WireGuard procedure live
in `HomeLab/deployments_managed/canvas-edge/`. The SRW deployment overlay names
the matching edge selectors and `srw-canvas-gateway-db` Secret in
`deployment/values-experimental.yaml`. All four launch values intentionally
remain false:

```yaml
canvas:
  livePreview:
    enabled: false
    viewer:
      enabled: false
      rawPathVerified: false
      pslBoundaryVerified: false
```

## Current checkpoint (2026-07-14)

Repository implementation is ready for a dark rollout:

- a wildcard DNS-01 certificate and dedicated `10.0.51.18` LoadBalancer;
- single-replica, non-root, read-only NGINX with fail-closed replacement;
- UUID SNI/Host admission, PROXYv2 trust pinning, redacted logs, request and
  connection rate limits, no cache, and a single raw-target-preserving route;
- edge/gateway NetworkPolicies and exact CoreDNS egress;
- a dedicated ESO mapping for only the restricted database username/password;
- the secret-safe PostgreSQL role reconciler documented in
  `dynamic_canvas_gateway_database.md`; and
- `scripts/verify-canvas-hosted-edge.py`, whose default probes are cookie-free,
  unauthenticated, bounded, and redact URLs, response bodies, cookies, and
  headers from output.

The public route is not ready. The current cluster still runs the older
`sha-3a14ada` release, so it has neither migrations through `0062`, the Canvas
gateway, nor its dedicated role/Secret. The current wildcard is Cloudflare
proxied and reaches only the inert catch-all. No public relay or WireGuard route
exists. The authoritative private Public Suffix List has no exact
`srwcanvas.works` rule.

The current hosted verifier therefore fails closed for Cockpit/API/IDE/PWA,
raw-path routing, and PSL; the final Keycloak login document already rejects
cross-origin framing with `frame-ancestors 'self'` and `SAMEORIGIN`.

## Rollout order

1. Publish and Fleet-deploy a current SRW chart/image while the Canvas flags
   remain false. Confirm migrations through `0062` and the new trusted-parent
   anti-framing headers are live.
2. Generate the restricted database password in a private file, populate the
   dedicated Vault path, run the role reconciler with `--apply`, and wait for
   `externalsecret/srw-canvas-gateway-db` to become Ready. Never put the login in
   the shared application Secret.
3. Commit and Fleet-deploy the Canvas edge bundle. Confirm its certificate,
   Deployment, Service, NetworkPolicies, and restricted source range while it
   is still unreachable from public DNS.
4. Provision the TCP relay and WireGuard route exactly as documented by the
   edge bundle. Test invalid SNI, SNI/Host mismatch, header stripping, raw
   targets, and observable `429` responses before DNS cutover.
5. Replace the current wildcard with a DNS-only record for the relay. Do not
   add the wildcard to cloudflared or Traefik.
6. Submit the exact domain to the PSL PRIVATE section as the verified domain
   owner. After merge and browser-list propagation, prove in current Chromium,
   Firefox, and Safari that JavaScript on a generated host cannot set a
   `Domain=srwcanvas.works` cookie. Text-list presence alone is not sufficient.
7. Run the hosted verifier, its explicit raw-path probe, and a scheduled bounded
   rate probe:

   ```bash
   ./scripts/verify-canvas-hosted-edge.py
   ./scripts/verify-canvas-hosted-edge.py --probe-raw-path
   ./scripts/verify-canvas-hosted-edge.py --probe-rate-limit
   ```

8. Complete the authenticated real-browser matrix for Chromium, Firefox,
   WebKit, shipping Safari/iOS, and an already-installed PWA: iframe exchange,
   CSP/sandbox, self-navigation, copied locator, logout, expiry, reset,
   revocation, and proof that no BFF/Keycloak cookie or authorization header
   reaches a workspace application.
9. Set `rawPathVerified` and `pslBoundaryVerified` only from that evidence.
   Enable the viewer for a one-port staging acceptance, then enable the master
   live-preview gate. Agent guidance remains disabled until the staging run is
   complete.

### PSL submission precondition

The PSL is not a general-purpose security switch. Its current owner guidelines
require private entries to issue subdomains to mutually untrusting parties,
require more than two years to remain on the domain registration, prefer an
owner-created pull request plus an `_psl.srwcanvas.works` TXT record pointing at
that PR, and warn that small lab/beta projects are likely to be declined. The
domain owner must therefore confirm/extend the registration term and submit the
production service rationale personally. There is no propagation SLA after a
merge.

If the entry is declined, do not set `pslBoundaryVerified`. Reopen the domain
isolation design and choose an equivalent enforceable per-app registrable-domain
boundary, or keep the explicitly insecure development profile; PSL absence is
not something an operator checkbox may waive.

## Anti-framing acceptance

The public responses must show:

- Cockpit shell, root, and Canvas deep route: enforced
  `frame-ancestors 'none'` plus `X-Frame-Options: DENY`;
- the service-worker manifest hashes the exact protected, policy-marked shell,
  and an installed worker serves that protected shell after activation/reload;
- general API/BFF documents: the same `none`/`DENY` policy;
- the exact API/IDE authority under `/api/ide/...`: only
  `frame-ancestors 'self'` plus `SAMEORIGIN`;
- Keycloak's final login document: either `self`/`SAMEORIGIN` or stronger
  `none`/`DENY`, so a Canvas origin cannot frame it.

Keycloak does not need to use the stronger Cockpit policy. Its same-origin
policy already excludes every `*.srwcanvas.works` parent and avoids changing
OIDC behavior merely to make the headers identical.

## Rollback

Turn off live preview and the viewer before changing the public route, revoke
active Canvas sessions, and remove or return the wildcard to the inert
catch-all. Stop relay admission and confirm no alternate ingress targets the
internal gateway. Reset either attestation to false whenever the edge, domain
boundary, or browser behavior changes; never use an attestation as an outage
override.
