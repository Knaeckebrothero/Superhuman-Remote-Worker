# Cloud-equivalent usage pricing

Status: v1 implemented and deployed. The typed infrastructure successor is
implemented through Slice 2/app migration `0102`, but remains dark-launched;
the serving cloud cards still price only the active v1 aggregate workspace
quantities.

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

## Legacy v1 rate-card approximations

Rate cards contain effective-dated components keyed by usage category, resource,
and unit. A specific resource wins over the `*` fallback, like `usage_rates`.

### Linear resource approximation

AWS ECS Fargate and Azure Container Instances publish independent vCPU-hour and
GiB-hour prices. Their estimate is:

```text
sum(quantity[unit] / capacity_per_billing_unit * rate[unit])
```

This v1 calculation is linear over aggregate quantities; it does not yet model
provider-supported shapes, task/group minimums, or rounding.

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

It is also distinct from the future concurrency change-point calculator: the v1
card takes one `max` over whole-window aggregate CPU/RAM hours, while the hardened
calculator integrates concurrent requested capacity slices.

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
The current effective dates are not a complete versioned historical catalog;
the result is a current-list planning scenario unless the source/version coverage
explicitly proves otherwise.

## As built

- App migration `0082_usage_cloud_rate_cards.sql` creates and seeds the cards.
- `orchestrator/services/cloud_pricing.py` parses official AWS/Azure catalogs,
  performs change-only refresh, caches rates, and computes `sum` / dominant-share
  estimates.
- `GET /api/usage` returns the scoped estimates as `cloud_estimates` alongside,
  but separate from, `total_cost_usd`.
- Cockpit Admin → Usage & Cost renders the cards with currency, components,
  formula, exclusions, and a link to the provider source.

## Boundary of the current implementation

The implemented `sum` / `max` cards are v1 planning indicators for the existing
aggregate workspace quantities. In particular:

- the aggregate STACKIT dominant-share card is a theoretical fractional lower
  bound only with complete eligible workload coverage; otherwise it is a modeled
  reference, never an exact VM, SKE node, or bin-packing bill;
- the Fargate and ACI cards do not yet apply supported shapes, per-resource
  minimum duration, or container-group rounding; and
- aggregate rows no longer contain enough lifecycle identity to apply a
  per-Pod, per-VM, or per-disk minimum/tier exactly.

Do not add VM, PVC, PV, agent, or platform rows to these wildcard cards. The
typed successor substrate in
[`infrastructure_resource_metering.md`](infrastructure_resource_metering.md)
now provides typed resource classes, half-open workspace-Pod periods, retained
lifecycle identity, price provenance, estimate quality, and code-versioned
calculator contracts. PVC/PV collection and lifecycle code now exists behind
Slice 2's dark-launch gates; agent/VM collection does not. Pricing coverage must
still not expand until the relevant inventory has completed shadow validation,
publication/source-aware reads are deliberately activated, and a provider
adapter is tested for that exact resource class.

**Current integration state (2026-08-06):** `/api/usage` and its v1 cards remain
authoritative. `/api/usage/v2`, typed calculators, the Slice 1 workspace-Pod
path, and Slice 2 PVC/PV quantity paths are implemented, but v2 reads,
source-aware reads, cutover, and publication are disabled. Slice 2's immutable
operator registry maps only exact `(cluster, StorageClass, CSI driver, volume
mode)` selectors. No collector-supplied parameters can select a price, and an
unmapped volume is forced unpriced. The local k3d PVC/PV inventory/shadow gate
has passed, but no storage quantities were published or added to the serving
cards. Provider storage rate cards and fixtures remain Slice 5; VM pricing
remains downstream of Slice 3.

## Exclusions and follow-ups

The initial estimate covers only quantities currently present in the ledger:
workspace pod CPU and RAM. It excludes control planes, continuously running
agent/orchestrator pods, VM workspaces, persistent disks, load balancers, public
IP addresses, egress, tax, discounts, minimum billing increments, and provider
free tiers. Those costs should be added only after the corresponding resource is
metered in its proper workload/asset domain or explicitly modeled as separate
idle/overhead cost.

Useful follow-ups:

1. Shadow-validate and activate the implemented PVC-requested
   `gib-hour`/`claim-hour` and PV-provisioned `gib-hour`/`volume-hour` paths, then
   add typed provider storage-rate components; derive GiB-month only as a
   display normalization.
2. Meter persistent agent and VM workspace intervals.
3. Add an admin rate-card editor and optional homelab amortization/power card.
4. Add a separate shared-platform baseline view for control plane and always-on
   infrastructure. Do not smear that baseline across users without an explicit
   allocation policy.

These resource-coverage follow-ups, together with live-interval and utilization
semantics, are now designed in
[`infrastructure_resource_metering.md`](infrastructure_resource_metering.md).
