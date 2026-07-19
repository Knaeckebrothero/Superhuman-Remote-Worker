---
tags:
  - feature
  - agent-tool
  - communication
  - whatsapp
  - messaging
aliases:
  - whatsapp channel
  - messaging channel
  - agent whatsapp tool
related:
  - "[[notify_user_tool]]"
  - "[[headless_persistent_sessions]]"
  - "[[automations]]"
  - "[[email_datasource]]"
---

# WhatsApp Messaging Channel

> The agent messages *people*, not just its owner. "Ask these 12 people for their shift preferences and tell me who hasn't answered" becomes a tool call, not an afternoon of the manager's time.

**Status:** Design approved 2026-07-14 (brainstorm), spec written 2026-07-19. Not yet implemented.
**Filed:** 2026-07-19

## Motivation

Recurring coordination workflows — building the weekly work plan, finding cover for a sick shift, collecting availability — mean a manager individually messaging dozens of people and mentally tracking who answered. An agent that can send WhatsApp messages to registered contacts, receive their replies mid-run, and aggregate the results turns that into a single delegated task.

This is distinct from [[notify_user_tool]] (agent → **owner**, email): here the agent messages **third parties** chosen from an owner-curated contact registry, and their replies flow back into the running session/job as input.

**Scope decisions (user, 2026-07-14):**

- **Official WhatsApp Business Cloud API only.** No Baileys / whatsapp-web.js — ToS violation, and bulk-messaging patterns are exactly what Meta bans numbers for. Non-negotiable for something business-critical.
- **1:1 plus small groups.** The official Groups API caps groups at **8 participants**; that covers family/small-team groups. Larger groups are explicitly unsupported in v1 — user-facing disclaimer: *"Groups with more than 8 participants aren't supported yet."*
- **Own use now, product later.** Single-tenant (one WhatsApp number, env-var config). The channel adapter and DB schema are shaped so a multi-tenant layer (per-tenant credentials + webhook routing) can wrap them later without a rewrite.
- **Approach A — thin channel tool, agent orchestrates.** The agent's existing loop (reasoning, todos, memory) does the fan-out/track/chase/aggregate logic. No campaign engine (rejected as over-scope), no broadcast helper tools yet (deferred; can be added as convenience wrappers later).

## Platform constraints the design must absorb

Facts as of 2026-07 (verify at implementation time — Meta moves):

1. **A phone number is always required.** WhatsApp usernames (rolling out since 2026-06) hide the number but don't remove it. The number must not be registered to a consumer WhatsApp account. Acquiring the number + WABA (WhatsApp Business Account) is an owner-side prerequisite, not code.
2. **Business-initiated messages need pre-approved templates + recipient opt-in.** Free-form text is only allowed inside the **24-hour customer-service window** opened by the recipient's last inbound message. Design consequence: the orchestrator — not the agent — decides free-form vs template per send. The agent just says "message Anna: …".
3. **Per-message pricing (since 2025-07).** Utility templates are the cheap lane (~$0.004–0.05); marketing templates are the expensive lane (Germany ~€0.11). Our sends are utility-shaped; service-window replies are free until 2026-10, then utility-priced.
4. **Groups API (Cloud API, ~2026-02):** ≤8 participants, ≤10k groups per number, business must create the group. Reported to require an **Official Business Account (OBA)** — an approval we may not get quickly. Groups are therefore the last slice, and 1:1 must not depend on anything groups-specific.
5. **Webhook inbound is push.** Meta POSTs message events to a public HTTPS endpoint (HMAC-signed `X-Hub-Signature-256`, plus a GET `hub.challenge` verification handshake). Per the [[public_ip_exposure_policy|exposure policy]], the endpoint is published via **Cloudflare Tunnel**, never a direct DNS record to the home IP.

## Provider decision (revises the brainstorm default)

The brainstorm tentatively said "Twilio sandbox → 360dialog prod". **Proposed revision: drop Twilio entirely; build one Cloud-API adapter.** Rationale:

- Twilio's WhatsApp integration has **no Groups API support** and a different payload format — the sandbox adapter would be throwaway.
- Meta's Cloud API gives a **free test number with up to 5 test recipients** immediately after creating a Meta developer app — prototype speed comparable to Twilio's sandbox, on the *production* payload format.
- **360dialog is a hosted Cloud API gateway** (payload-compatible; base URL + `D360-API-KEY` header instead of `graph.facebook.com` + Bearer token). One adapter with a pluggable base URL + auth header covers Meta-direct *and* 360dialog. 360dialog remains the production recommendation (EU data residency, flat fee, no per-message markup, multi-client tooling for the "product later" path).

## Architecture

Everything follows an existing house pattern; only the inbound public webhook is a genuinely new *kind* of surface (this repo's externals are all pollers today — the IMAP poller is the closest analog).

```
agent (LangChain tool)                     third party's phone
  message_contact("anna", "…")                    │ reply
        │ POST /api/messaging/send                ▼
        ▼  (X-Internal-Key)              Meta Cloud API / 360dialog
orchestrator ── guardrails ──► ChannelAdapter ──► └──► webhook POST (HMAC)
  │  contacts registry · window logic · audit          │ Cloudflare Tunnel
  └◄── conversation binding ◄── /api/messaging/webhook/whatsapp
        │
        └─► _internal_resume_job(job_id, feedback=…)   [jobs]
            resume/input path                          [sessions]
```

### 1. Channel adapter — `orchestrator/services/messaging/`

- `base.py` — `ChannelAdapter` protocol: `send_text(to, text)`, `send_template(to, template, params)`, `parse_webhook(payload) -> list[InboundMessage]`, `verify_signature(raw_body, headers) -> bool`, `is_available` (graceful degrade when unconfigured, like `imap_poller`).
- `whatsapp_cloud.py` — the one implementation. Config via `os.getenv` (house style): `WHATSAPP_API_BASE_URL` (default Meta Graph), `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` (webhook HMAC), `WHATSAPP_VERIFY_TOKEN` (GET handshake), `WHATSAPP_DEFAULT_TEMPLATE` + `WHATSAPP_TEMPLATE_LANG` (the approved generic utility template, e.g. `Hi {{1}}, {{2}}`).
- Multi-tenant later = constructing adapters from per-tenant DB rows instead of env; the interface doesn't change.

### 2. Schema — migration `0063_messaging_channel.sql`

```sql
messaging_contacts (
    id UUID PK, handle TEXT UNIQUE,            -- 'anna' — what the agent references
    display_name TEXT, channel TEXT DEFAULT 'whatsapp',
    address TEXT,                              -- E.164 for 1:1, WA group id for groups
    kind TEXT DEFAULT 'person',                -- 'person' | 'group'
    opt_in_status TEXT DEFAULT 'pending',      -- 'pending' | 'opted_in' | 'opted_out'
    owner_user_id UUID REFERENCES users, notes TEXT, timestamps
)
messaging_conversations (
    id UUID PK, contact_id UUID FK UNIQUE,
    last_inbound_at TIMESTAMPTZ,               -- window_open = last_inbound_at > now()-24h (computed, no sweeper)
    bound_kind TEXT, bound_id UUID,            -- 'job' | 'thread' — where replies route; last-writer-wins
    updated_at
)
messaging_messages (                            -- full audit, both directions
    id UUID PK, contact_id UUID FK, direction TEXT, body TEXT,
    delivery TEXT,                             -- 'freeform' | 'template' | 'inbound'
    provider_message_id TEXT, status TEXT,     -- 'sent'|'delivered'|'read'|'failed'|'received'
    job_id UUID NULL, thread_id UUID NULL, created_at
)
```

### 3. Agent tools — `src/tools/communication/whatsapp.py`

Clone the shape of `src/tools/communication/messaging.py` (metadata dict + `create_*_tools(context)` factory, POST to orchestrator with `X-Internal-Key`); register in `src/tools/registry.py`; enable under `communication:` in `config/defaults.yaml`.

- `list_contacts() -> str` — handles, names, kind, opt-in state, window state. The agent's ground truth for who is reachable.
- `message_contact(handle: str, message: str) -> str` — the send. Returns what actually happened: `"sent (free-form)"` / `"sent via template — Anna will see: Hi Anna, <message>"` / a structured refusal (`unknown contact`, `not opted in`, `rate limit`, `group too large`). **Input that looks like a raw phone number is rejected** — contacts only.
- `check_messages(handle: str = None, since: str = None) -> str` — pull-based read of inbound messages (replies also push into the loop; this is for re-reading/aggregating).

### 4. Orchestrator surface

- **Internal (agent-called, `require_internal`):** `POST /api/messaging/send`, `GET /api/messaging/contacts`, `GET /api/messaging/messages`. New router `orchestrator/routers/messaging.py`, registered in `main.py` alongside the others.
- **Owner CRUD (BFF-authed):** contacts create/update/delete + opt-in status + group create (slice 4). v1 is REST-only (curl/scripts); a Cockpit page is a later slice.
- **Public webhook (net-new pattern):** `GET|POST /api/messaging/webhook/whatsapp` in the same router but **not** behind `require_internal` — authenticated by `X-Hub-Signature-256` HMAC (Meta); for providers without payload signing (verify 360dialog's story at implementation), fall back to an unguessable secret path segment in the webhook URL. Exposed only via Cloudflare Tunnel route.

### 5. Send pipeline (orchestrator-side, per send)

1. Resolve handle → contact; reject unknown/opted-out.
2. Guardrails: per-contact rate limit (default **10/contact/hour**), global (default **200/hour** — must clear the flagship "message 50 people, then chase stragglers" burst; env-tunable), body ≤ 4096 chars, groups: participant count ≤ 8.
3. Window check (ships in slice 3; slices 1–2 always send free-form): `last_inbound_at` within 24h → free-form `send_text`; else wrap body into the approved default utility template (`{{1}}=display_name, {{2}}=body`) → `send_template`. The tool result tells the agent which path was taken.
4. Audit row in `messaging_messages`; delivery-status webhooks update `status`.

### 6. Inbound pipeline (per webhook event)

1. Verify HMAC; parse via adapter into `InboundMessage`s (text v1; media acknowledged-but-dropped with a note in the audit row).
2. Match sender → contact. Unknown sender: store in audit with `contact_id NULL`, do not route. Matched contact in `pending`: **first inbound flips `opt_in_status` to `opted_in`** — this is the self-serve opt-in mechanism (people message the number once).
3. Update `messaging_conversations.last_inbound_at` (reopens the 24h window).
4. Route via binding: job → `_internal_resume_job(job_id, feedback="WhatsApp reply from Anna: …")` (the canonical injection primitive, per the IMAP path); thread → the sessions resume/input path. No binding → audit only; owner can see it via `check_messages` from any session.
5. Bindings are **last-writer-wins v1**: the most recent job/thread to message a contact owns the replies. Documented limitation.

### 7. What v1 explicitly does not do

Cockpit contacts UI (REST-only v1) · broadcast/campaign helpers · multi-tenant · media messages · Telegram/SMS adapters (the `ChannelAdapter` seam makes them cheap later) · `WhatsAppTransport` in `notification_service.py` for **owner** notifications (natural follow-up, separate feature) · automations trigger source ("on WhatsApp message → create job" — parked per [[automations]] roadmap).

## Guardrails summary

Follows the [[notify_user_tool]] canon (OWASP classes `send_message` as high-risk): recipient indirection (handles, never raw numbers — the owner-curated registry bounds the blast radius), opt-in enforced at send *and* implied by design (template + first-reply), rate limits at the orchestrator, full audit trail, tool availability governed by the existing config/grants surface. No per-message human approval — it would kill the "message 50 people" utility; the contact registry is the control point.

## Implementation slices

Each independently shippable; live-verify on local k3d before dev deploy per house practice.

1. **Foundation + outbound 1:1.** Migration 0063, adapter (`send_text` + `is_available`), contacts CRUD, send endpoint + guardrails, `message_contact`/`list_contacts` tools. Gate: agent messages the owner's real phone via Meta test number.
2. **Inbound + reply loop.** Webhook router (verify + receive + HMAC), Cloudflare Tunnel route, window tracking, binding + routing into job/session, `check_messages`. Gate: reply from the phone lands in the running session.
3. **Templates + window automation.** Register the default utility template with Meta, `send_template`, automatic window-based path selection, delivery-status handling. Gate: message to a cold contact arrives as template; conversation continues free-form after reply.
4. **Groups ≤8.** Group create (owner REST + adapter), `kind='group'` contacts, group send + inbound, disclaimer copy. **Entry gate: verify OBA requirement** — if OBA-blocked, slice parks without affecting 1:1.

## Open questions

1. **Production number + WABA acquisition** (owner action): which number, Meta Business verification timeline, and whether OBA (for groups) is attainable — start verification early, it's the long pole.
2. **360dialog webhook authentication** — confirm signing mechanism at slice-2 time; if none, tunnel-level protection + the secret path segment.
3. **Binding contention** (two sessions messaging the same contact) — last-writer-wins accepted for v1; revisit if dogfooding hits it.
4. **Template approval turnaround** for the generic utility template — if rejected as too generic, fall back to 2–3 use-case-specific templates (shift request, cover request).

## Decision log

- **2026-07-14:** Official API only; 1:1 + groups ≤8 with disclaimer; larger groups deferred. (User.)
- **2026-07-14:** Own use now, product later — single-tenant with multi-tenant-ready seams. (User.)
- **2026-07-14:** Approach A (thin tool, agent orchestrates); campaign engine rejected; broadcast helpers deferred. (User.)
- **2026-07-19:** Provider revised to single Cloud-API adapter (Meta test number → 360dialog prod); Twilio dropped — no Groups support, throwaway payload format. **(Pending user confirmation at spec review.)**
- **2026-07-19:** Contacts-only recipients, orchestrator-owned template/window logic, reply routing via `_internal_resume_job` / sessions input path, no sweeper (window state computed from `last_inbound_at`). (Spec.)
