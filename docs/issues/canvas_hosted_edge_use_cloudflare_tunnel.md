# Canvas hosted edge: drop the raw-path relay, ride the Cloudflare tunnel

**Status**: Decided and EXECUTED 2026-07-14. Correction to the original
filing: `HomeLab@08d09fd` had in fact been pushed and Fleet had deployed the
edge bundle (namespace `canvas-edge` live: NGINX pod, LoadBalancer holding
10.0.51.18, issued wildcard cert). The removal therefore shipped as a
follow-up commit (`HomeLab@8a90253`) rather than a history reset; pushing
HomeLab main makes Fleet prune the live resources. The scrapped design is
archived on HomeLab branch `canvas-edge-archive`. Root-repo work (attestation
demotion, NetworkPolicy retarget to cloudflared, optional wildcard Ingress,
docs) landed the same day. Remaining: push both repos, verify Fleet prune,
and confirm the `*.srwcanvas.works` DNS record CNAMEs to the tunnel.
Related: `docs/features/dynamic_canvas.md` (Security Model / hosted edge),
`docs/operations/dynamic_canvas_hosted_edge.md` (rewritten around the
tunnel), `deployment/values-experimental.yaml` (selectors now cloudflared).

## Problem

The staged public-edge design for the Canvas live-app wildcard was:

```text
*.srwcanvas.works (Cloudflare DNS-only, grey cloud)
  → rented VPS: HAProxy L4/SNI relay (public IP)
  → WireGuard tunnel into the home cluster
  → MetalLB address 10.0.51.18
  → dedicated in-cluster NGINX edge (wildcard TLS, PROXYv2, limit_req)
  → srw-canvas-gateway:8086
```

Two things are wrong with it:

1. **Packaging.** The entire edge lives in the private HomeLab repo, not the
   Helm chart. A chart consumer cannot reproduce it and should never need
   to. Worse, most of what the NGINX layer does (host validation, header
   stripping, upgrade rejection, rate limiting) duplicates hardening the
   Canvas gateway already performs itself.
2. **Adoption cost.** The product goal is plug-and-play: cert-manager +
   Cloudflare + the SRW chart. If enabling the Canvas viewer requires a
   VPS, a VPN, and a second reverse-proxy stack for a hardening property
   that even medium-sized companies do not need, users will simply not
   enable it — or not install SRW at all. That is a bad default trade.

## Why it happened

One requirement — **byte-exact raw-path preservation** ("the app must
receive the exact request target; Cloudflare's HTTP proxy merges adjacent
slashes") — was set as an axiom and never re-priced as its costs
compounded. The chain: raw paths cannot ride Cloudflare's HTTP processing →
DNS-only wildcard → the home IP would be public in DNS (unacceptable: the
operator deliberately masks it behind the CF tunnel) → rent a VPS to mask
it → HAProxy for SNI relay → WireGuard to reach the NAT'd cluster →
MetalLB address, PROXYv2 trust, a dedicated NGINX, two attestation flags,
and a verification script. Every piece is competent engineering; the
requirement underneath it was never load-bearing for this topology. The
VPS relay is, in effect, self-hosted Cloudflare Spectrum built to route
around an Enterprise paywall.

## Analysis: raw paths are not a security boundary here

- **Normalization upstream of the boundary is the safe direction.** The
  dangerous proxy pattern is a *front* that routes on the raw path while
  the *back* normalizes (`/static/../admin`). Here it is inverted:
  Cloudflare normalizes and makes **no** path-based routing decision (the
  whole wildcard goes to one gateway), and the gateway — the only component
  making decisions — checks and forwards the **same** normalized string.
  There is no check-vs-forward differential to smuggle through.
- **The gateway's only path logic is the `/_canvas/*` reserved prefix**,
  whose endpoints carry their own strict auth (cookies, Fetch Metadata,
  single-use challenges) regardless of how a path reaches them. Compare:
  the cockpit/API already ride this exact Cloudflare tunnel with ~180
  routes of per-path auth. If slash-merging were a live threat, the
  cockpit needed the VPS first. It doesn't.
- **What normalization actually costs**: an app whose routes depend on
  adjacent (`//`) or encoded (`%2F`) slashes 404s on those routes. A
  compatibility footnote for agent-built prototypes, not an escalation.
  A path that normalizes *into* `/_canvas/*` hits the strictly-authed
  control endpoints and is rejected.
- **No Cloudflare plan fixes it anyway.** Slash-merging is a *baseline*
  normalization applied to all proxied traffic on every plan and cannot
  be disabled (the zone "Normalize incoming URLs" toggle only affects
  what rules see; `raw.*` fields exist for rule evaluation only). The
  only CF product that avoids HTTP parsing entirely is Spectrum, and
  generic TCP/UDP Spectrum is Enterprise-only. Refs:
  <https://developers.cloudflare.com/rules/normalization/how-it-works/>,
  <https://developers.cloudflare.com/spectrum/protocols-per-plan/>.
- **Riding Cloudflare improves the axes the design worried about**: CF's
  edge parser rejects malformed framing before our infrastructure sees
  it, and DDoS absorption + rate limiting replace the hand-rolled NGINX
  `limit_req` zone. The gateway already spools-and-reframes every request
  and forces `Cache-Control: private, no-store` on all responses
  (`canvas_gateway.py`, `canvas_proxy_policy.py`), so CF will not cache
  canvas content.

## Decision

**The Canvas wildcard rides the existing Cloudflare tunnel, exactly like
every other public hostname of the deployment.** Raw-path fidelity is
demoted from a universal launch gate to a documented, optional property of
an operator-chosen edge (any L4/SNI-passthrough load balancer provides it;
cloud LBs do it trivially). If a future deployment tier genuinely needs
it (multi-tenant hosted production, contractual hardening), the archived
design can be revived — nothing in this simplification forecloses it.

Explicitly unchanged: the separate registrable origin + wildcard cert, the
dedicated gateway process and restricted DB role, the browser-bound
bootstrap exchange and viewer-session tables, CSP/sandbox policy, PSL plan
(the PSL is a *cookie* boundary for production multi-tenancy and is
orthogonal to transport).

## Implementation checklist

HomeLab repo:

1. ~~Do not push `08d09fd`~~ **DONE (amended)**: `08d09fd` was already pushed
   and Fleet-deployed. Archived on `canvas-edge-archive`; the bundle is
   removed by `8a90253`, which Fleet prunes on push (namespace, pod, the
   10.0.51.18 LoadBalancer, and the wildcard cert all go away).
2. **DONE** (`8a90253`): wildcard tunnel entry added to
   `deployments_managed/cloudflare-tunnel/cloudflare-tunnel_configmap.yaml`:
   ```yaml
   - hostname: "*.srwcanvas.works"
     service: http://srw-canvas-gateway.superhuman-remote-worker.svc.cluster.local:8086
   ```
3. Cloudflare dashboard (operator): confirm the existing proxied
   `*.srwcanvas.works` record CNAMEs to this tunnel (it currently reaches the
   catch-all). Universal SSL covers a first-level wildcard; no ACM needed.
   No port opens, the home IP stays masked.

Root repo (chart + code) — all DONE 2026-07-14:

4. **Gateway NetworkPolicy ingress**: `canvasGateway` currently admits
   only the (now-dead) canvas-edge namespace/pod selectors
   (`helm/templates/canvas-gateway/network-policy.yaml`,
   `deployment/values-experimental.yaml:176-179`). Point the selectors at
   the cloudflared pods instead.
5. **Demote the raw-path attestation**: remove the `rawPathVerified`
   `const: true` requirement from `helm/values.schema.json` and the
   fail-closed check in `orchestrator/services/canvas_viewer_config.py`.
   Keep the PSL attestation for the production profile (cookie boundary,
   unrelated to transport). Update `helm/values.yaml` comments: a
   normalizing proxy (Cloudflare tunnel/proxy) in front of the gateway is
   a supported edge; adjacent-slash merging is an accepted, documented
   limitation of that mode.
6. **Verifier**: make the raw-path differential checks in
   `scripts/verify-canvas-hosted-edge.py` opt-in (e.g.
   `--expect-raw-paths`) instead of a blocking default, so the hosted
   matrix can go green behind the tunnel.
7. **Optional plug-and-play ingress**: add a default-off standard
   `Ingress` template for the gateway wildcard host mirroring
   `helm/templates/ingress.yaml` (Traefik annotations + cert-manager),
   so chart consumers without a tunnel point DNS at their ingress LB and
   are done. Note in values: a wildcard cert via cert-manager requires a
   DNS-01 solver.
8. **Docs**: rewrite `docs/operations/dynamic_canvas_hosted_edge.md`
   around the tunnel path (keep the old design as an appendix or link to
   the archive branch); annotate the Security Model raw-path/proxy
   sections in `docs/features/dynamic_canvas.md` with the demotion and
   its rationale.

## Slice-4 note

WebSockets ride Cloudflare tunnels fine; SSE works but needs heartbeats
(CF kills streams after ~100s of silence). Neither changes this decision —
the Slice-3 gateway rejects upgrades/streaming regardless.

## Revisit when

- SRW hosted production moves to rented cloud infrastructure (an L4
  passthrough LB makes raw paths free — revive as `sni-passthrough` mode);
- a customer contractually requires byte-exact path fidelity or an
  in-path hardened edge;
- application-cookie forwarding ships (its threat review may change the
  edge calculus).
