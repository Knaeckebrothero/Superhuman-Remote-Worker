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
  - "[[canvas_hosted_edge_use_cloudflare_tunnel]]"
---

# Dynamic Canvas hosted-edge rollout

This runbook takes the default-off one-port Canvas viewer from a dark chart
render to hosted acceptance. It does not authorize enabling the feature before
the attestations below are true.

## Selected edge (decided 2026-07-14)

The hosted route rides the deployment's existing Cloudflare Tunnel, exactly
like the cockpit and API hostnames:

```text
<uuid>.srwcanvas.works (Cloudflare proxied wildcard, Universal SSL)
  -> Cloudflare Tunnel (cloudflared, outbound-only)
  -> internal srw-canvas-gateway:8086
```

Rationale, analysis, and the retirement of the previous
DNS-only/VPS/WireGuard/NGINX design are recorded in
`docs/issues/canvas_hosted_edge_use_cloudflare_tunnel.md`. The two properties
that matter:

- **Path normalization is accepted.** Cloudflare's baseline normalization
  merges adjacent slashes on every plan and cannot be disabled. This is safe
  in this topology: normalization happens *before* the security boundary, and
  the gateway checks and forwards the same canonicalized string — there is no
  check-vs-forward differential. The residual cost is fidelity (an app whose
  routes depend on `//` or encoded slashes 404s), documented as a property of
  this edge mode. Deployments that need byte-exact paths front the gateway
  with an L4/SNI-passthrough load balancer instead; both modes are supported.
- **The gateway owns its own hardening.** UUID host validation, header
  stripping, framing rejection, per-session limits, and `private, no-store`
  caching are all gateway-side, so no dedicated edge proxy is required in
  front of it.

Chart consumers without a tunnel use the optional
`canvas.livePreview.viewer.ingress` wildcard Ingress (cert-manager DNS-01) or
any equivalent route to the ClusterIP Service.

The SRW deployment overlay (`deployment/values-experimental.yaml`) names the
cloudflared namespace/pod selectors for the gateway NetworkPolicy and the
dedicated credential Vault path. Everything stays inert until the viewer is
enabled. The launch values intentionally remain false:

```yaml
canvas:
  livePreview:
    enabled: false
    viewer:
      enabled: false
      pslBoundaryVerified: false
```

## Current checkpoint (2026-07-14)

- The proxied `*.srwcanvas.works` DNS record and Cloudflare Universal SSL
  certificate are live; unmatched hosts reach the inert catch-all because no
  tunnel ingress rule targets the gateway yet.
- The gateway, restricted database boundary, ESO mapping, and
  `scripts/verify-canvas-hosted-edge.py` (cookie-free, redacted probes) are
  implemented and dark.
- The live cluster runs a pre-Canvas release; migrations through `0062`, the
  gateway Deployment, and its role/Secret do not exist there yet.
- The authoritative private Public Suffix List has no exact
  `srwcanvas.works` rule.

## Rollout order

1. Publish and Fleet-deploy a current SRW chart/image while the Canvas flags
   remain false. Confirm migrations through `0062` and the trusted-parent
   anti-framing headers are live.
2. Generate the restricted database password in a private file, populate the
   dedicated Vault path, and run the role reconciler with `--apply` (see
   `dynamic_canvas_gateway_database.md`). Never put the login in the shared
   application Secret.
3. Add the wildcard ingress rule to the tunnel ConfigMap
   (`HomeLab/deployments_managed/cloudflare-tunnel/`):

   ```yaml
   - hostname: "*.srwcanvas.works"
     service: http://srw-canvas-gateway.superhuman-remote-worker.svc.cluster.local:8086
   ```

   While the viewer is disabled the Service does not exist, so the rule
   resolves to a 502 behind the inert wildcard — acceptable and dark.
4. Submit the exact domain to the PSL PRIVATE section as the verified domain
   owner. After merge and browser-list propagation, prove in current Chromium,
   Firefox, and Safari that JavaScript on a generated host cannot set a
   `Domain=srwcanvas.works` cookie. Text-list presence alone is not
   sufficient.
5. Run the hosted verifier and a scheduled bounded rate probe:

   ```bash
   ./scripts/verify-canvas-hosted-edge.py
   ./scripts/verify-canvas-hosted-edge.py --probe-rate-limit
   ```

   The `--probe-raw-path` differential is **not** part of tunnel-mode
   acceptance: adjacent-slash merging is expected there. Run it only against
   an L4/SNI-passthrough edge that claims raw-path fidelity.
6. Complete the authenticated real-browser matrix for Chromium, Firefox,
   WebKit, shipping Safari/iOS, and an already-installed PWA: iframe exchange,
   CSP/sandbox, self-navigation, copied locator, logout, expiry, reset,
   revocation, and proof that no BFF/Keycloak cookie or authorization header
   reaches a workspace application.
7. Set `pslBoundaryVerified` only from that evidence. Enable the viewer and
   master live-preview gate for a one-port staging acceptance. This render
   creates `externalsecret/srw-canvas-gateway-db`; require it to become Ready
   and the gateway rollout to complete before the acceptance run. Agent
   guidance remains disabled until that run is complete.

### PSL submission precondition

The PSL is not a general-purpose security switch. Its current owner guidelines
require private entries to issue subdomains to mutually untrusting parties,
require more than two years to remain on the domain registration, prefer an
owner-created pull request plus an `_psl.srwcanvas.works` TXT record pointing
at that PR, and warn that small lab/beta projects are likely to be declined.
The domain owner must therefore confirm/extend the registration term and
submit the production service rationale personally. There is no propagation
SLA after a merge.

If the entry is declined, do not set `pslBoundaryVerified`. Reopen the domain
isolation design and choose an equivalent enforceable per-app
registrable-domain boundary, or keep the explicitly insecure development
profile; PSL absence is not something an operator checkbox may waive.

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
active Canvas sessions, and remove the tunnel ingress rule (the wildcard then
returns to the inert catch-all). Confirm no alternate ingress targets the
internal gateway. Reset the PSL attestation to false whenever the domain
boundary or browser behavior changes; never use an attestation as an outage
override.
