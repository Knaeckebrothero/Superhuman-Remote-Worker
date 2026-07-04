---
tags:
  - feature
  - observability
  - cockpit
  - usage
  - dashboard
  - frontend
related:
  - "[[usage_monitoring_and_rate_limiting]]"
  - "[[observability_and_quotas]]"
  - "[[saas_billing_and_metering]]"
aliases:
  - usage dashboard
  - fleet monitor
  - consumption ledger
  - usage view
---

# Usage Dashboard — fused fleet-monitor + per-principal consumption view

> Design doc — captured 2026-06-26 from the "make the usage page actually
> useful" conversation. This is the **read / visualization** layer on top of
> [[usage_monitoring_and_rate_limiting]] Slice 4: that slice shipped the
> append-only `usage_events` ledger (LLM tokens + workspace compute) and a
> deliberately minimal Admin → Usage page that shows a single
> `GROUP BY (category, unit)` rollup — "four numbers". The ledger underneath is
> far richer than that page exposes. This doc designs the dashboard that
> actually surfaces it.

**Status:** v1 **implemented, pushed & deployed to dev** (built/verified on local k3d
2026-06-26; pushed to `origin/develop` + rolled out to dev via `sha-fe9c9ee` 2026-06-29) — as-built record
(commits `46040008`…`896fad8d`, tests green, deviations) in the implementation plan
`docs/superpowers/plans/2026-06-26-usage-dashboard.md`. SDD final whole-branch review still pending; no dashboard fast-follows landed yet. **Cost note:** LLM `cost_usd` is priced from `usage_rates`, whose LLM rates are auto-seeded from OpenRouter (`openrouter_pricing.llm_pricing_sync_loop`) now that the LiteLLM gateway is removed ([[remove_litellm_proxy_and_gateway_concept]]). Cost reads "—" for compute rows (`usage_rates` has no compute rates yet — vcpu/gib-hour stay unpriced) and free homelab models (rate 0). v1 scope below;
everything uncertain is a named fast-follow, decided **after** seeing the page
rendered (the user's explicit preference: build a concrete v1, react to how it
feels, iterate — don't over-spec the panel set upfront).

**Driver:** Two things converged. (1) The Slice-4 page shows only a
category×unit rollup, while the ledger records per-row `user_id`, `project_id`,
`resource` (model), `ref_id` (job/thread, compute only), `ts`, and token /
compute quantities — so "who is driving consumption, on which models, over
time" is *already in the data* but unsurfaced. (2) Two HTML mockups were drawn
(`SRW-Command-Deck.html` = a live **Fleet monitor**; `SRW-Usage-Cost-Ledger.html`
= a per-user **cost ledger**); the ask is a **fusion** of the two: fleet health
+ throughput on top, per-principal attribution underneath.

**Scope (v1):** One **visibility-scoped** Cockpit page (grows the existing
Admin → Usage view), **quantity-first** (tokens & compute-hours headline; cost
columns render only when priced, else "—"), built **almost entirely on data we
already record** plus existing jobs/agent reads. Window chips (7/30/90d) + a
Grafana-style auto-refresh toggle.

**Explicitly NOT v1 — fast-follows, in priority order:**
1. **By-provider** breakdown — the one dimension needing new capture (stamp
   provider onto LLM rows via a model→catalog lookup + a one-time backfill).
2. **Export CSV** of the current view.
3. **Per-job LLM attribution** (the gateway never sees `job_id` today) → a true
   per-job cost line. Tracked in [[usage_monitoring_and_rate_limiting]].
4. **Edit-Rates admin UI + seeding real rates** → turns quantity-first into real
   dollars (the deferred [[usage_monitoring_and_rate_limiting]] "Next" item).
5. **Live RPM/TPM** "right now" panels — sourced from **LiteLLM's own metrics**,
   not the ledger (a separate data source).
6. Trend **sparkline** polish to match the mockups as drawn.

## What we already record (the queryable substrate)

The dashboard is a read over [[usage_monitoring_and_rate_limiting]]'s Slice-4
ledger; what each `usage_events` row carries bounds what v1 can show **without**
new capture. Confirmed against the schema + both emitters:

| Dimension | Recorded? | Notes |
|---|---|---|
| **When** (`ts`) | ✅ | request start (LLM) / interval end (compute) |
| **User** (`user_id`) | ✅ | LLM via scoped key; compute via owner attribution. Fleet-key-fallback LLM traffic is unattributed (null) |
| **Project** (`project_id`) | ✅ | same caveat |
| **Model** (`resource`) | ✅ (LLM) | model group/name; tokens split `prompt-token` / `completion-token` |
| **Compute** (`resource='workspace_pod'`) | ✅ | `vcpu-hour` + `gib-hour` = requested CPU/RAM × wall-clock; `details` has `cpu_millicores`, `mem_bytes`, `started_at`, `ended_at`, `tier`, `duration_h` |
| **Job/thread** (`ref_id`) | compute ✅ / **LLM ❌** | gateway never sees `job_id` (fast-follow 3) |
| **Provider** | ❌ | only the model name is stored — derivable via a catalog join, not a column (fast-follow 1) |
| **Cost** (`cost_usd`) | LLM ✅ / compute ❌ | **Updated 2026-07-04:** LLM cost is **real** — priced from `usage_rates`, whose LLM rates are auto-seeded from OpenRouter (`openrouter_pricing`) since the LiteLLM gateway was removed ([[remove_litellm_proxy_and_gateway_concept]]). Compute stays unpriced (`usage_rates` has no compute rates yet); free homelab LLM prices at rate 0. |

**Out of scope of the ledger entirely (do not promise):** GPU/VRAM (compute
meter is CPU+RAM only), **actual** vs **requested** utilization (no sampling),
agent-pod compute, VM-tier compute (container/sandbox tier only), and embeddings /
whisper / TTS (non-chat traffic). **Updated 2026-06-29 — the coverage gap narrowed:**
**codex + system-provider (minimax/openrouter) models now route through the gateway
and meter** ([[route_all_models_through_litellm_gateway]] P1–P2, live on dev);
**gemini still bypasses** (not yet canaried). So "any model not routed through the
gateway" is now a shrinking set, not the whole paid lane.

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Headline | **Quantity-first.** Tokens & compute-hours are primary; `cost` columns show only when a row is priced, else "—". No rate-seeding work this round. |
| 2 | Audience / scoping | **One visibility-scoped page for everyone.** Admin sees the fleet (all-user leaderboard + fleet-status); a non-admin sees the same page shape with **only their own** rows (no other users, no fleet-status panel). Reuses the G5 scoping already in `UsageLedger.query_usage`. |
| 3 | Freshness | **Periodic auto-refresh**, not streaming. A Grafana-style toggle (Off / 10s / 30s / 1m) reusing the debug timeline's `autoRefreshEnabled()` signal pattern. True "live RPM right now" is fast-follow 5. |
| 4 | Data posture | **Tier-1 reuse.** Ledger group-by queries + existing jobs/agent reads. Exactly one dimension (provider) needs new capture → deferred. |
| 5 | Surface | **Grow the existing `admin-usage.component.ts`** into the dashboard rather than a new parallel view; the current page becomes the default (category×unit) panel within it. |
| 6 | Mockups | A **fusion** of both, not either as-drawn. Fleet/ops on top, per-principal attribution below. |

## Architecture

### Page layout (top → bottom)

1. **KPI row** with a vs-previous-window delta: **Tokens · Compute-hours · Jobs
   completed · Agents in-field**. Agents-in-field is fleet-oriented → **admin
   only**; a non-admin's KPI row is Tokens / Compute-hours / Jobs, all
   self-scoped.
2. **Throughput** (jobs/day bar chart) + **Fleet status** (in-field / idle /
   standing-by / signal-lost) — **admin-only row**; a non-admin sees only their
   own job throughput (fleet-status hidden).
3. ⭐ **Consumption-by-user leaderboard** — per user: prompt/completion tokens,
   compute, events, a share bar, role + agent count. Admin: all users; non-admin:
   their own single row.
4. **By-model** + **By-category/unit** breakdown tables (the latter is today's
   page, preserved).
5. **By-project** breakdown (scoped to visible projects).

### Panel → data source → status

| Panel | Source | Work | Status |
|---|---|---|---|
| KPI Tokens / Compute-hours | `usage_events` aggregate (window + prev) | new agg + delta | tier-1 |
| KPI Jobs completed | jobs / daily stats | reuse | exists |
| KPI Agents in-field | agent registry | reuse | exists |
| Throughput (jobs/day) | daily job stats | reuse (maybe thin endpoint) | exists* |
| Fleet status | agent statuses → 4 buckets | status→bucket map | exists* |
| Consumption-by-user | `usage_events` `GROUP BY user_id` + cross-DB enrich | new query | tier-1 |
| By-model | `usage_events` `GROUP BY resource` | new query | tier-1 |
| By-category/unit | `query_usage` today | none | exists |
| By-project | `usage_events` `GROUP BY project_id` + enrich | new query | tier-1 |
| By-provider | `usage_events` + provider stamp | enrich + backfill | **deferred** |

\* Group-B panels read jobs + the agent registry, not the ledger. The cockpit
already has agents/jobs views, so the data is reachable; whether a **thin
aggregation endpoint** is needed (vs. deriving client-side from existing reads)
is the one open implementation question — resolved at plan time, see Open
questions.

### Backend — the only genuinely new query work

Extend `UsageLedger.query_usage` + `GET /api/usage` **additively** (the current
default response is unchanged, so the existing page keeps working):

- New `group_by` ∈ `{category_unit` (default, today's behaviour)`, user, model,
  project}`. Each grouped row carries the group key, quantity-by-unit, `events`,
  and `cost_usd` (null-safe).
- New `compare=previous` flag → also aggregate the immediately-preceding window
  of equal length `[from − Δ, from)` and return per-key + total deltas for the
  KPI trends. Two indexed, partition-pruned aggregates — cheap at v1 scale.
- **Visibility scoping is unchanged** — the same `owner_user_id` /
  `visible_project_ids` / `scope_project_id` clauses already in `query_usage`
  apply to every `group_by`. A non-admin grouping by user can only ever return
  their own row.

**Cross-DB enrichment (important — not a single SQL join).** `usage_events`
lives in `srw-auditdb`; `users` / `projects` / `jobs` live in the **app DB**. So
the leaderboard's display-name, role, and agent/job counts can't be joined in
one query. Pattern: `query_usage` runs on the **audit pool** and returns
`user_id` + aggregates; the endpoint then does a second lookup on the **app
pool** (users + per-user job/agent counts) and merges in Python — the same
cross-pool shape the Slice-4 compute materializer already uses. Same for
project names.

### Frontend

One Angular standalone component (grow `cockpit/.../admin/usage/admin-usage.component.ts`
into a `usage-dashboard`), OnPush + signals. `windowDays` and
`refreshIntervalMs` are signals; the auto-refresh toggle reuses the debug
timeline pattern (`autoRefreshEnabled()` → a `setInterval` that re-fetches,
cleared on destroy / when set to Off). New `AdminUsageService` methods, one per
`group_by`, plus a `compare` fetch for the KPI row. Every panel renders explicit
**empty / inert / non-admin** states (metering-disabled, no-data-in-window,
unpriced → "—", non-admin → self-only).

### Fleet-status bucket mapping

The mockup's four buckets (in-field / idle / standing-by / signal-lost) are a
**presentation grouping** over our real agent statuses, defined once in the
endpoint. The exact status→bucket map (and how `status=session` agents are
counted — see the known session-zombie caveat) is pinned during planning; the
data itself is already in the agent registry.

## Testing

- **Backend:** unit tests per new `group_by`, the `compare=previous` delta, and
  **visibility scoping** (a non-admin grouping by user sees only themselves;
  scope_project filters correctly) — asyncpg, mirroring the 11 existing ledger
  tests. Cross-DB enrichment tested with a stubbed app-pool lookup.
- **Frontend:** vitest rendering each panel against mock data + the empty / inert
  / non-admin-scoped states; the auto-refresh toggle starts/stops the interval.

## Open questions

1. **Group-B endpoint shape** — do jobs-completed / throughput / fleet-status
   reuse existing cockpit reads (derive client-side) or warrant one thin
   aggregation endpoint? Resolve at plan time by checking what the agents/jobs
   views already call.
2. **Fleet-status buckets** — exact agent-status→bucket mapping, incl. how
   `status=session` agents are bucketed.
3. **Trend window for "+N today"** — the mockup's KPI deltas mix "vs previous
   window" (tokens/jobs) and "today" (agents). v1 uses vs-previous-window
   uniformly unless a per-card basis feels wrong once rendered.

## Relationship to the existing docs

- **[[usage_monitoring_and_rate_limiting]]** — the parent. Slice 4 built the
  ledger + the minimal page; this is the visualization layer over it. The
  fast-follows here (provider stamp, per-job LLM attribution, edit-rates/real $)
  are the same deferred items that doc's "Next" / "Deferred" sections name —
  this doc is where they get a UI home.
- **[[observability_and_quotas]]** — owns the `usage_events` schema this reads.
- **[[saas_billing_and_metering]]** — the eventual dollar-billing consumer; this
  page stays quantity-first and does not pre-empt it.

## References

- `SRW-Command-Deck.html` / `SRW-Usage-Cost-Ledger.html` (repo root) — the source
  mockups this fuses.
- `orchestrator/services/usage_ledger.py` — `query_usage` (the read to extend).
- `orchestrator/services/litellm_gateway.py` `materialize_llm_usage` /
  `orchestrator/services/workspace_metering.py` — the emitters that define the
  queryable dimensions.
- `cockpit/src/app/views/admin/usage/admin-usage.component.ts` — the page to grow.
- `cockpit/src/app/debug/components/timeline/timeline.component.ts` — the
  `autoRefreshEnabled()` toggle pattern to reuse.
</content>
</invoke>
