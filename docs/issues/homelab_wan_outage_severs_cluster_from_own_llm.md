# WAN outage severs the cluster from its own LAN-hosted LLM (hairpin dependency)

**Status:** Open, infrastructure. Observed 2026-08-05 11:46–14:44Z during
`baseline-02`; the homelab uplink dropped for ~3 h while the cluster itself
stayed healthy throughout.

## What the outage actually looked like

- **Inside:** pods, sweepers, and dispatch all kept running. But every agent
  LLM call died — request timelines show clean 20–40 s cadence until exactly
  11:46Z, then a 178-minute hole, then one ~180–230 s retry success at
  14:44Z when the link returned. The model (`gemma-4-moe`) is served from
  the homelab itself at `ai.h4ll.app` — **in-cluster consumers resolve and
  route it via the public name**, so losing the WAN cut the cluster off from
  a model sitting on its own LAN. Same hairpin as the k3d VPN/DNS case
  (fixed there with coredns-custom).
- **Outside:** Cloudflare 530 on `api.srw.works` (edge fine, tunnel origin
  gone) + internal-zone DNS (`rancher.h4ll.app`) timing out ⇒ from any
  external observer the whole homelab looks dead. There is currently no way
  to distinguish "uplink down, cluster fine" from "cluster down" without
  physical access.

## Job casualties (baseline-02)

- `S4-csv-totals` r1 (`7eff03bc`): survived the gap, its 14:44Z retry
  *succeeded*, and the job still failed immediately after — 18 requests,
  0 phase archives. Why one successful post-recovery request wasn't enough
  to keep it alive is an open sub-question (relates to the LLM-outage
  backoff/retry-ceiling work).
- `D1-wordfreq-kata` r1: identical gap, continued normally after recovery;
  wall-clock metrics inflated by ~3 h (excluded from the baseline analysis).

## Fixes worth doing

1. **Break the hairpin:** CoreDNS rewrite / hostAlias so `ai.h4ll.app`
   resolves to the LAN route from inside the cluster (mirror of the k3d
   coredns-custom fix). Jobs then ride out WAN outages entirely.
2. **Disambiguate outages from outside:** any cluster-side signal with its
   own egress path (or simply: the pattern "CF 530 + internal DNS timeout +
   Cloudflare edge still resolving" ⇒ suspect uplink-not-cluster; verified
   correct on 08-05).
3. Revisit the S4-style post-recovery failure semantics once (1) makes
   reproduction rarer but not impossible.
