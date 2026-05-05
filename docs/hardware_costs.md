# Hardware & hosting costs

Pricing analysis for running the platform at the ~1000-user tier. EU/Germany
focus, Q2 2026 numbers. All figures are realistic ballparks for negotiating /
budgeting — actual quotes vary by commit term, rack density, and provider.

The reference footprint throughout: **3× Dell R630, ~1.0–1.5 kW combined
draw, 3U + a switch ≈ 5U total**.

## Bandwidth requirement (recap)

For ~1000 signups → 50–150 concurrent active sessions / 50–100 concurrent
agent jobs:

| Source | Sustained | Peak |
|---|---|---|
| LLM API egress (prompts + tool I/O) | 50–150 Mbps | 300+ |
| Browser automation (Playwright fetches) | 25–100 Mbps | 200+ |
| Cockpit (SSE/WS/REST to ~100 active users) | 10–30 Mbps | 80 |
| DB backup/replication/observability | 5–20 Mbps | — |
| **Total** | **~150–300 Mbps** | **~500–800 Mbps** |

Symmetric gigabit is workable with concurrency caps. 2–2.5 Gbps is
comfortable. 10 Gbps is overkill until ~5–10K users.

## Colocation (your hardware, someone else's building)

What you actually pay for: rack space + power commit + network + cross-
connects. Power is the single biggest line item — typically 30–50% of the
bill.

| Item | Frankfurt premium (Equinix/Interxion/NTT) | Tier-2 DE/NL (Maincubes, Hetzner colo, regional) |
|---|---|---|
| Half rack (22U), 2A @ 230V incl. | €400–700/mo | €150–300/mo |
| Additional power, metered | €0.22–0.35/kWh | €0.18–0.28/kWh |
| 1 Gbps IP transit (95th-percentile) | €150–400/mo | €50–150/mo |
| Cross-connect | €80–150/mo each | €30–80/mo each |
| Remote hands | €120–180/hr | €60–100/hr |
| /29 IPv4 | €15–40/mo | €5–20/mo |

**Metered power bill**: 1.2 kW × 24 × 30 ≈ 864 kWh/mo. At €0.25/kWh that's
**~€216/mo just for power** — which is what people mean by "DC power is
expensive." It's not actually higher per-kWh than DE business retail
(~€0.30–0.40), but you pay 24/7 for the commit, including cooling baked
into the rate via the PUE multiplier (typically 1.3–1.6×).

**Realistic monthly all-in for 3× R630 in colo**:

- Frankfurt premium: **€900–1,500/mo**
- Tier-2 DE/NL: **€400–700/mo**

Plus one-time install/setup (€200–500) and any IP-transit commit minimums.

## Cloud equivalent (rented compute, no hardware)

To match 3× R630 (~80–100 vCPU, ~1 TB aggregate RAM, NVMe):

| Provider | Spec | Compute /mo | Egress at ~200 Mbps avg (~64 TB/mo) | Total |
|---|---|---|---|---|
| **AWS** | 3× m6i.16xlarge (64 vCPU/256 GB) | ~$6,500 | ~$5,500 (after free tier, $0.09/GB) | **~$12,000** |
| **GCP** | similar n2-standard | ~$6,000 | ~$5,200 ($0.08–0.12/GB) | **~$11,000** |
| **Azure** | similar Dsv5 | ~$6,500 | ~$5,000 | **~$11,500** |
| **Hetzner Cloud** | 3× CCX63 (48 vCPU/192 GB) | €720 | included → €1/TB after 60 TB | **~€720–760** |
| **Hetzner dedicated** (their DC, your rented box) | 3× AX102 (16c Ryzen 7950X3D / 128 GB DDR5 / 2× NVMe) | €297 | 20 TB/server included, €1/TB after | **~€300–350** |
| **OVH bare-metal** | 3× Advance-2 / Scale tier | €450–600 | mostly unlimited | **~€500** |

**The egress cliff is the headline number.** AWS/GCP/Azure charge
$0.08–0.12/GB outbound. At 200 Mbps sustained that's $5,000+/month just for
traffic — more than the entire Hetzner bill. Cloud is roughly **20–30×
more expensive** than Hetzner dedicated for this exact workload, almost
entirely because of egress pricing.

## Self-hosted (current plan, friend's business uplink)

| Item | Monthly |
|---|---|
| Power, 1.2 kW @ DE business retail €0.30–0.40/kWh | €260–350 |
| Business gigabit symmetric fiber (DE) | €100–200 |
| Hardware amortized (3× used R630 + switch over 3y) | ~€100 |
| Cooling delta (rack in office space, not purpose-built) | €30–80 |
| **Total** | **~€500–700/mo** |

Cheapest option per Mbps and per vCPU at this scale. Real costs absent from
this table: your time on incidents, single-uplink/single-rack SPOF, and the
fact that residential/light-business ISPs often prohibit production SaaS in
their AGB.

## Migration path

In order, with the trigger that should move you to the next step:

1. **Stay self-hosted at the friend's business** (now): ~€500–700/mo
   all-in. Cheapest, most control, biggest SPOF.
   *Trigger to leave*: sustained >70% link utilization, repeated power/uplink
   incidents, or first paying enterprise customer with uptime SLA.
2. **Hetzner dedicated** (~€300–400/mo): cheaper than self-hosting once
   your time is counted, no power/cooling/hardware risk, EU-based, decent
   traffic allowance.
   *Tradeoff*: you don't own the metal. For regulated B2B customers
   (sovereignty, custody-of-data requirements) this can be disqualifying.
   *Trigger to leave*: customer demands true colo / your-own-hardware
   posture, or you outgrow Hetzner's per-server bandwidth caps.
3. **EU tier-2 colo with your own R630s** (~€500–900/mo): the "graduate"
   step when single-rack uptime stops being acceptable. Same hardware,
   DC-grade power/cooling/uplink, redundant IP transit.
   *Trigger to leave*: need direct DE-CIX peering, geographic redundancy,
   or co-location with specific partners.
4. **Frankfurt premium colo** (~€1,000–1,500/mo): only when DE-CIX peering
   or proximity to specific partners is required.
5. **AWS/GCP/Azure**: ~$10–15k/mo for this load. **Don't**, unless an
   enterprise customer specifically demands it and is paying for it. The
   egress model is hostile to this workload.

## Headline takeaways

- **"DC power is expensive" is half-true**. Per kWh it's similar to or
  cheaper than DE business retail; the cost is the 24/7 commit and the
  PUE-baked-in cooling.
- **The actually expensive thing at scale is cloud egress**, not DC power.
- For 1000 users, the migration path is friend's-rack → Hetzner dedicated
  (or tier-2 EU colo if R630-keeping is desired) → multi-region colo.
  Hyperscalers stay out of the core path.
- Hyperscalers are still useful for non-core needs: S3-compatible object
  storage for cross-region durability, ad-hoc batch GPU jobs, or compliance-
  mandated regional presence — pay only where the bill actually justifies
  itself.
