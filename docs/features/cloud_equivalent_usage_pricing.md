# Cloud-equivalent usage pricing

Status: implemented in the working tree (2026-08-05); deployment pending

## Why

The usage ledger already records requested workspace compute as `vcpu-hour` and
`gib-hour`, but those rows intentionally have no canonical billing rate. That is
correct for a homelab deployment, yet it leaves us without a useful answer to
"what would this application usage cost on a public cloud?"

This feature reprices the measured quantities against provider list-price rate
cards. It is an estimate for product and capacity planning, not an import of an
AWS, Azure, or STACKIT invoice.

## Decisions

- Keep `usage_events.rate_usd` / `cost_usd` unchanged. They remain immutable,
  canonical charges (currently chiefly OpenRouter LLM cost).
- Store cloud comparison rates in separate, effective-dated rate cards in the
  app database. Repricing must never mutate usage history.
- Return cloud estimates from `GET /api/usage`, after its existing visibility
  filters. The selected window and the admin `All data` scope therefore apply
  equally to quantities and estimates.
- Price requested resources, matching the existing workspace meter. This is not
  utilization-based billing and it does not inspect provider invoices.
- Preserve each source currency. The first version does not hide an exchange
  rate assumption by summing EUR and USD.
- Show source, region, pricing model, and exclusions in the UI. The label is
  **Cloud-equivalent estimate**, never **bill** or **actual cost**.

## Rate-card formulas

Rate cards contain effective-dated components keyed by usage category, resource,
and unit. A specific resource wins over the `*` fallback, like `usage_rates`.

### Linear resource pricing

AWS ECS Fargate and Azure Container Instances publish independent vCPU-hour and
GiB-hour prices. Their estimate is:

```text
sum(quantity[unit] / capacity_per_billing_unit * rate[unit])
```

### Bundled reference instance

STACKIT Compute Engine prices a complete flavor rather than independent CPU and
RAM meters. Splitting one VM price into arbitrary CPU and RAM percentages would
double count or encode a policy with no provider basis. The STACKIT reference
card therefore uses the dominant requested share of a g2i.4 (4 vCPU, 16 GiB):

```text
max(vcpu_hours / 4, gib_hours / 16) * g2i.4_hourly_price
```

This is a node-share indicator. Aggregating a whole window loses concurrency and
bin-packing information, so it is not a promise that the workload fits into that
exact number of nodes.

## Initial cards and refresh

- STACKIT SKE / g2i.4, EU01, EUR. Seeded from the current official STACKIT
  price list. STACKIT currently documents an authenticated actual-cost API, but
  no equivalent public unauthenticated list-price endpoint; this card is
  source-labelled and can be advanced by a later migration or admin rate editor.
- AWS ECS Fargate Linux/x86, `eu-central-1`, USD. Refreshed from the official
  public Amazon ECS regional price-list file.
- Azure Container Instances Standard Linux, `germanywestcentral`, USD. Refreshed
  from the unauthenticated Azure Retail Prices API.

Refresh is change-only and non-load-bearing. A failed provider request retains
the newest stored rate and cannot prevent orchestrator startup or usage reads.

## As built

- App migration `0082_usage_cloud_rate_cards.sql` creates and seeds the cards.
- `orchestrator/services/cloud_pricing.py` parses official AWS/Azure catalogs,
  performs change-only refresh, caches rates, and computes `sum` / dominant-share
  estimates.
- `GET /api/usage` returns the scoped estimates as `cloud_estimates` alongside,
  but separate from, `total_cost_usd`.
- Cockpit Admin → Usage & Cost renders the cards with currency, components,
  formula, exclusions, and a link to the provider source.

## Exclusions and follow-ups

The initial estimate covers only quantities currently present in the ledger:
workspace pod CPU and RAM. It excludes control planes, continuously running
agent/orchestrator pods, VM workspaces, persistent disks, load balancers, public
IP addresses, egress, tax, discounts, minimum billing increments, and provider
free tiers. Those costs should be added only after the corresponding resource is
metered or explicitly modelled as shared platform overhead.

Useful follow-ups:

1. Add `gib-month` PVC allocation events and provider storage rate components.
2. Meter persistent agent and VM workspace intervals.
3. Add an admin rate-card editor and optional homelab amortization/power card.
4. Add a separate shared-platform baseline view for control plane and always-on
   infrastructure. Do not smear that baseline across users without an explicit
   allocation policy.
