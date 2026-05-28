---
tags:
  - feature
  - billing
  - saas
  - usage-metering
  - wallet
  - cost-attribution
aliases:
  - usage metering
  - wallet billing
  - cost attribution
  - SaaS billing
related:
  - "[[multi_tenancy]]"
  - "[[cockpit_owned_auth_ui]]"
  - "[[auth_bff_and_api_tokens]]"
---

# SaaS billing + usage metering (wallet-funded, OpenRouter-style markup)

> Stub doc — captured 2026-05-28 to preserve context from the M1 SaaS-readiness planning discussion so the full design can be written in a follow-up session without re-litigating the business model. **No code, no schema decisions, no API design yet** — just the framing, the open questions, and the user-locked decisions to date.

**Status:** Stub / framing only. Full design pending.
**Triggered by:** M1 SaaS readiness planning surfaced that data-isolation work alone doesn't make the product SaaS-ready — opening signups to strangers without a billing layer means anonymous demand bills our LLM credits. The wallet/metering layer is the cost-isolation equivalent of M1.C's file-isolation work. Captured here so it gets the same M1 first-class treatment.
**Scope:** Per-user attribution of LLM + compute cost; wallet/balance ledger; debit gate at job dispatch; usage display in Cockpit. **Does not** cover Stripe wiring, payment-method storage, ToS-around-billing, dispute/refund flow, invoicing, taxes, currency handling, or the marketing-side pricing page — those are downstream of the metering layer landing.

## Business model (locked)

User-funded wallet, OpenRouter-style. Specifically:

1. **Pre-pay model.** Users deposit money into their account (via Stripe or similar — out of scope of this doc). The deposit credits a per-user wallet balance.
2. **Pay-per-use debits.** As the user runs jobs / persistent sessions / agents, the system meters the cost (LLM tokens × model rate + compute time × hardware rate) and debits the wallet in near-real-time.
3. **Markup, not flat-rate.** Our take is a margin on top of pass-through LLM + compute costs. Target ~5% — comparable to OpenRouter's published margin. Rate per model is a tunable.
4. **Hard stop at zero balance.** When the balance hits zero (or falls below a small reserve to cover an in-flight request), new jobs are blocked at dispatch and persistent sessions are suspended. UI shows a "top up your balance" CTA.
5. **No free tier.** The architecture supports it if we ever add one (a `monthly_credit_grant` column on `users`, debited first), but v1 ships without one. Limited beta users get manual wallet credits.
6. **No subscription tier.** Same — supported by future work (a flat-rate user gets a high cost cap), not in v1.

**Why this shape over alternatives:**

- **Pre-pay > post-pay** — no collections risk, no chargeback risk, no "you owe us $4000 for the LLM run we accidentally let your account fire" support nightmare.
- **Markup > flat subscription** — usage is the primary cost driver and varies by 100× across users. A flat subscription is either underpriced (losing money on heavy users) or overpriced (losing acquisition on light users). Markup tracks reality.
- **Wallet > line-of-credit** — adversarial users can't accumulate debt. Users who deposit $50 can lose at most $50.

**This shape directly informs M1.D abuse prevention** ([[multi_tenancy]] §M1.D): cryptomining is uneconomical when our compute markup means a dedicated VM rental is cheaper. M1.D doesn't need to defeat mining — economics already does. M1.D becomes defense-in-depth (data exfiltration, runaway-bug protection, fair-share resource quotas).

## Why this is in M1 SaaS readiness scope

Per [[multi_tenancy]], M1 = "we can open signups to strangers without leaking data or burning the company". Three loss vectors:

1. **Data leaks** — covered by M1.A (API isolation, done) + M1.C (cloud storage per-user OAuth, pending).
2. **Cluster compromise / abuse** — covered by M1.D (defense-in-depth, post-billing).
3. **Cost overruns** — **only** covered by billing + metering. Without it, every signup is a free LLM-credit account on our dime.

Vector 3 is the missing piece. Hence this doc.

## Where things stand technically (gap audit — TO BE VERIFIED)

These are claims to verify when the full design starts. Each is currently a "probably-yes / probably-no" not a "definitely-yes / definitely-no". **A first task of the follow-up design session is to grep + read and turn each row into a concrete answer.**

| Gap | Probable state | What to check | Why it matters |
|---|---|---|---|
| `llm_requests` carries `user_id`? | Probably yes | MongoDB collection schema; orchestrator writer in `services/mongo_audit.py` (or wherever) | Per-user LLM cost attribution. If yes, aggregation is trivial. If no, we need a backfill from `job_id → user_id` and a forward-going writer change. |
| `llm_requests` carries token counts + model identifier? | Probably yes | Same collection | Multiply tokens × per-model rate = LLM cost. Need both. |
| Workspace pod compute time is attributed to anything? | **Probably no** | `WorkspaceManager` / `container_provisioner.py` lifecycle hooks; K8s pod metadata | Cost attribution for workspace runtime. If no, need a "workspace started/stopped" event stream tied to `job_id → user_id`. |
| Agent pod compute time is attributed? | **Probably no** | Same — agent pods have `job_id` labels but no usage event stream | Cost attribution for agent runtime. Same shape as workspace. |
| VM compute time is attributed? | **Probably no** | `vm_provisioner.py`; NATS lifecycle messages | Only matters if VM backend is used; today it's disabled on dev cluster ([[project-vm-backend-disabled-on-dev]]). |
| Persistent-session token usage is attributed? | Maybe (rides on `llm_requests`) | Same — verify the persistent-session LLM call path writes the same shape of `llm_requests` rows | Persistent sessions are likely the highest per-user cost variance. |
| Per-model cost rates are configured anywhere? | **No** | grep for "rate", "cost", "price" in `config/models.yaml`, `config/settings_matrix.yaml` | Need a per-model rate table. Either a new YAML or a DB-backed table (lean DB for tunability without redeploy). |
| Wallet / balance table exists? | **No** | grep for "wallet", "balance", "credit" in `orchestrator/database/schema.sql` + migrations | Greenfield. |

## Design questions the follow-up doc must answer

1. **Schema.** `user_wallets(user_id, balance_cents, …)` + `wallet_transactions(id, user_id, amount_cents, kind, ref_id, ref_table, created_at, …)` with `kind IN ('deposit', 'debit_llm', 'debit_compute', 'refund', 'credit_grant')`. Currency: USD only v1? Cents-as-int avoids float precision.
2. **Per-model rate table.** YAML in `config/` or DB-backed (`model_rates(model_id, prompt_rate_per_1k, completion_rate_per_1k, effective_from, …)`)? Lean DB — admins should be able to update rates without a deploy.
3. **Per-resource compute rate.** Workspace pod-second, agent pod-second, VM pod-second, persistent-session-second. Hardware-cost-driven. Probably YAML / Helm value — changes less than LLM rates.
4. **Debit cadence.** Real-time (every `llm_requests` insert → wallet debit transaction) or batched (per-job aggregation at completion)? Real-time gives the hard-stop accuracy; batched is simpler but lets users overspend by one in-flight job. Lean real-time.
5. **Debit gate.** Where does "do you have enough credit?" fire? At `POST /api/jobs` (pre-create), at dispatch, on every model call inside the agent? Each has tradeoffs.
6. **Reserve.** When a job starts, reserve some headroom? Otherwise an in-flight job can spend the user from $0.01 to -$10. Solution: estimated-cost pre-debit at job start, refund the remainder on completion. Or hard-cap per-job spend.
7. **Persistent sessions** — different beast. No clear "completion" event. Probably charge as-you-go per message + per session-minute, suspend session when balance crosses zero.
8. **Cockpit usage UI.** A new `/settings/usage` page showing balance, recent transactions, projected runway, per-job cost breakdown. Reuses the API-keys page styling.
9. **Per-job cost in existing UI.** Today the job-list page shows status + name. Add a `cost_usd` column (computed from `wallet_transactions` joined by `ref_id=job_id`).
10. **Admin override.** Admins (real_is_admin, [[admin_view_as_user]] pattern) can grant manual credits — for support, beta, refunds. Audit-logged.
11. **Refund flow.** Job fails partway through with an LLM error → refund the LLM debit? Refund the compute? Policy decision.
12. **Free-tier-friendly architecture.** Even without a v1 free tier, structure the wallet so adding `monthly_credit_grant` later is one column, not a migration.

## Two slices the design should split into

Captured rough so the follow-up doc starts with shape:

**Slice 1 — Metering (read-side, no behavior change).** Build the attribution pipeline:
- Verify/fix `llm_requests` schema (user_id, tokens, model).
- Add workspace + agent pod start/stop event streams.
- Add per-model + per-compute rate tables.
- Compute usage_aggregates per user per day.
- Cockpit `/settings/usage` page reads from the aggregates.
- **No wallet, no debit gate yet** — pure observability.

This alone gives admins a "who's costing what" view. **~3-5 days**, low risk, ships value before the wallet exists.

**Slice 2 — Billing (write-side, blocks jobs).** Add wallet schema, debit transactions, dispatch gate, top-up flow (Stripe out of scope but the wallet API needs deposits to come in from *somewhere* — leave a stub).
- `user_wallets` table.
- `wallet_transactions` table fed by the metering pipeline from Slice 1.
- Pre-job credit check at dispatch.
- Suspend persistent sessions when balance crosses zero.
- Admin manual-credit endpoint.

**~5-8 days**. Risk concentrated here; Slice 1 de-risks it by proving the attribution numbers are correct first.

**Stripe / top-up UI is a third slice** — out of scope of this doc per the scope statement above.

## Out of scope (this doc)

- Stripe integration / payment method storage / 3DSecure.
- Pricing page UI / marketing.
- Subscription tiers (architecturally possible, not v1).
- Free tier (same).
- Multi-currency / FX.
- Invoicing / tax (VAT, sales tax).
- Dispute / chargeback workflows.
- Annual / enterprise billing.
- Per-org billing (M2 territory in [[multi_tenancy]]).

## Open questions for the follow-up design session

1. Markup target — 5% (OpenRouter parity), or higher to absorb compute losses on light users?
2. Real-time vs batched debit — leaning real-time, confirm.
3. Currency — USD only v1, or also EUR (sales to EU customers)?
4. Reserve strategy — fixed per-job estimate, model-aware estimate, or pay-as-you-go with overshoot protection?
5. What happens to in-flight persistent sessions when balance hits zero — hard-suspend mid-message (poor UX) or finish-current-turn-then-suspend (one-turn overshoot)?
6. Admin grants — flat tool ("give user X $20") or templated ("apply 'beta credit' grant")?

## Decisions already locked (from 2026-05-28 conversation)

| # | Decision | Value |
|---|---|---|
| 1 | Funding model | Pre-pay wallet (deposit → balance → debit on use) |
| 2 | Pricing model | Pass-through cost + markup (OpenRouter-style, target ~5%) |
| 3 | Cost attribution scope | LLM (tokens × model rate) + compute (pod-seconds × hardware rate) |
| 4 | Hard stop | Balance reaches zero → new jobs blocked at dispatch, persistent sessions suspended |
| 5 | Markup defeats mining | Yes — therefore M1.D abuse prevention reframes around data-exfiltration not cryptomining |
| 6 | Billing in M1 scope | Yes — as a parallel track to M1.C/D/E. Cannot open signups without it. |
| 7 | First implementation slice | Metering (observability) first, wallet/debit second — de-risks attribution before adding gates |
| 8 | Stripe + payment flow | Out of scope of this doc (separate follow-up) |

## References

- [[multi_tenancy]] §M1.D — abuse prevention reframing depends on this doc's billing model.
- [[cockpit_owned_auth_ui]] — public signup UI; needs billing to be live before it's safe to expose.
- [[auth_bff_and_api_tokens]] — the cookie BFF + PAT plumbing this builds on (no new auth needed for billing endpoints).
- OpenRouter pricing model reference: https://openrouter.ai/docs/use-cases/byok (for the markup approach).
