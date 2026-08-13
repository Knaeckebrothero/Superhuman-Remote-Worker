---
tags:
  - spec
  - website
  - go-to-market
  - waitlist
  - gdpr
created: 2026-08-13
status: draft
related:
  - "[[website]]"
  - "[[sales_page_deploy]]"
  - "[[saas_roadmap]]"
  - "[[release_roadmap]]"
---

# Pre-Launch Waitlist for the SRW Sales Page

## TL;DR

Replace the sales page's hosted-signup CTAs with a qualified early-access waitlist, so we can
measure demand from a trailer + social campaign before spending the remaining pre-launch month.
Capture runs on **self-hosted Listmonk** on the homelab, with a rented EU SMTP relay for
delivery. Two legal pages (Impressum + Datenschutzerklärung) ship with it because we cannot
lawfully collect an email address without them.

Estimated effort: **1.5–2 days**, of which roughly half is Listmonk + DNS + deliverability setup.

---

## 1. Why

Two independent reasons, either of which would justify the work on its own.

**The page currently lies, and points at the wrong cluster.** Both primary CTAs
(`website/index.html:214` hero, `:422` hosted lane) read "Sign up" and link to
`https://cockpit.srw.works/signup` — the **dev test cluster**. The hosted product is not
purchasable, is roughly a month from ready, and the environment behind that link is the one we
are actively developing in. A waitlist does not merely add a capture surface; it closes that door.

**We want a demand signal before we spend the month.** The plan is a short trailer showing an
agent actually working, pushed to Reddit / YouTube / social. The number of people who sign up is
the cheapest read we will get on whether the positioning lands — but only if the signups are real
and we can tell which channel produced them. Both of those are design constraints, not
nice-to-haves (see §5, §6).

## 2. What a visitor gets

A **qualified early-access list**, not a bare "notify me" box.

A pure notify-me list maximises raw signups and teaches us nothing: everyone clicks it, roughly
2–5% convert at launch, and the resulting list cannot be segmented or personalised. Two optional
questions cost about ten seconds of friction and turn the list into something we can act on
during the month we have left.

## 3. Decisions already taken

| Question | Decision | Consequence |
|---|---|---|
| What's the offer? | Qualified early-access list | Email + 2 optional questions, not a bare email box |
| The existing `cockpit.srw.works` CTAs? | **Replaced entirely** | Dev cluster stops being publicly linked |
| Opt-in model? | **Double opt-in** | Needs a sending path; signup count becomes trustworthy |
| Impressum + privacy text? | Drafted here, reviewed by us | Template-grade, **not** lawyer-reviewed |
| Where does it run? | **Self-hosted (Listmonk)** | See §4 for what that does and does not buy |

The `/configure` self-host CTA and the "Talk to sales" mailto are **untouched** — those paths
work today.

## 4. Architecture

### 4.1 The decision: self-host the list, rent the pipe

The instinct to self-host is right, and one of my initial objections to it was wrong. I argued
that self-hosting would make homelab uptime the waitlist's uptime — but **the sales page is
already served from the homelab** (`srw-sales-page`, Traefik ingress). If the cluster is down,
the page is down and there is nothing to submit to. The form and the page already share a failure
domain. That objection does not survive contact with the deployment topology.

What does survive is a harder constraint:

> **You can self-host the list. You cannot self-host the sending.**

Listmonk is a mailing-list manager, not an MTA. Delivery still needs an SMTP relay with a clean
sending IP, and a homelab connection cannot be one:

- Spamhaus **PBL** lists residential and dynamic IP ranges by policy as ranges that should never
  send mail directly to a destination server. This is not a reputation you can earn back; it is a
  category the range sits in.
- Many consumer ISPs block outbound port 25 outright.
- The documented remedy is exactly what we will do: relay through an authenticated smarthost on
  port 587.

This also aligns with our own public-IP exposure policy — nothing new is exposed from the home
connection.

So the shape is: **subscriber data on our infrastructure, transmission rented.** That is a
genuinely better data-sovereignty story than a hosted list provider (where the list itself lives
with the vendor), and it is consistent with what the product sells.

### 4.2 Components

| Piece | Choice | Notes |
|---|---|---|
| List manager | **Listmonk** v6.2.0 (rel. 2026-06-26) | AGPL, single binary / Docker image, Postgres ≥12, native double opt-in, SQL segmentation, Go-templated campaigns |
| Database | Dedicated Postgres in the listmonk namespace | Follows the `gitea` pattern in `deployments_managed/` — own DB, not shared |
| SMTP relay | **Brevo** or **Mailjet** (both French/EU) | Keeps the whole chain in the EU so the privacy page needs no third-country transfer basis |
| Deploy | Fleet GitOps, `HomeLab/deployments_managed/listmonk/` | `00-namespace`, `02-eso`, `10-postgres`, `20-listmonk`, `30-service`, `31-ingress`, `fleet.yaml` |

### 4.3 Exposure

Listmonk's **admin UI is not public**. It goes on the tailnet (`*.h4ll.app` per the hostname
convention). Only the public subscription path is reachable from the internet, and even that is
not exposed directly:

```
visitor → superhuman-remote-worker.com (sales-page nginx)
            └── location /api/waitlist  → proxy_pass → listmonk.listmonk.svc.cluster.local:9000
```

Proxying through the existing sales-page nginx buys three things: the form POST stays
**same-origin** (so no CORS, and the progressive-enhancement fetch path works), Listmonk is never
directly addressable from outside, and the nginx config already lives in
`docker/Dockerfile.website` where we control it.

### 4.4 DNS and deliverability

Non-negotiable, and the part most likely to be done badly:

- **SPF**, **DKIM**, **DMARC** on `superhuman-remote-worker.com`.
- Note for Brevo specifically: only **DKIM** contributes to DMARC alignment unless you are on a
  dedicated IP. An SPF-only setup will not give you a passing DMARC.
- Verify with a header-analysis tool that SPF, DKIM and DMARC all pass and that `Return-Path` /
  `From` alignment is correct — before the first real campaign, not after.

**Silver lining:** the double opt-in confirmation emails between now and launch are low-volume
transactional sends that gradually warm the domain. By launch day we will have weeks of clean
sending history instead of a cold domain and one large blast.

## 5. Page changes (`website/index.html`)

- Both `Sign up` buttons become **`Get early access`**, targeting a new `#waitlist` section
  instead of leaving the site.
- Hosted-lane copy moves from present tense to a stated launch intent — it currently tells
  visitors they can buy something they cannot.
- New `#waitlist` section above the footer.
- New footer links to `/impressum` and `/privacy`.

**Byte budget.** The page's whole value is that it lands in one TCP round trip. Current state:
**9,213** gzipped against a hard **14,336** ceiling. The form is estimated at +900–1,400 gzipped,
landing near 10,600 — comfortable, but measured rather than assumed.

Nothing currently enforces that ceiling. This work adds a **byte-budget assertion to the existing
`generator-test` CI job** so the constraint is protected by something other than whoever remembers
to check.

## 6. The form

Four inputs, one invisible, plus consent.

| Field | Required | Purpose |
|---|---|---|
| `email` | yes | The actual ask |
| `deployment` — *Hosted for me / Self-hosted / Not sure yet* | no | Aggregatable. Tells us which of the two lanes we already sell people actually want |
| `use_case` — one-line free text | no | The field that teaches us something we do not already believe |
| `source` (hidden) | — | JS reads `?utm_source` from the URL. **Without this we cannot tell Reddit from YouTube from HN**, which defeats the purpose of running a campaign. Degrades to `direct` with JS off |

Plus a **required consent checkbox** linking to `/privacy`, and a honeypot field for bots.

### No-JS behaviour

The form is a real `<form method="POST">` and works with JavaScript disabled — a hard constraint
the page already holds itself to. With JS (~150 bytes) we intercept, POST via `fetch`, and swap in
an inline success state without a navigation. Progressive enhancement, not a JS dependency.

Post-submit lands on a new `/thanks` page whose only job is to say **go click the confirmation
link**. This is not decoration: under double opt-in, unconfirmed signups are silently lost, and
this page is what protects the confirm rate.

### Error handling

Duplicate and malformed addresses are Listmonk's to handle. If Listmonk is unreachable, the JS
path shows an error with the sales mailto as fallback; the no-JS path gets a browser error. We
accept that rather than building around it — as established in §4.1, a cluster that cannot serve
the POST also cannot serve the page.

## 7. Legal pages

Two new static pages, `website/impressum.html` and `website/privacy.html`, styled to match, with
nginx `location` blocks for clean URLs (the same pattern as the existing `/configure`). Both must
be added to the `COPY` line in `docker/Dockerfile.website` — easy to forget, and the page 404s if
you do.

**There is currently no Impressum and no Datenschutzerklärung anywhere in the repo or on the live
site.** For a commercial German site that is already a gap under §5 DDG, independent of this work.
The moment we store an email address it also becomes a GDPR Art. 13 problem, and the consent
checkbox has to link to a privacy notice that actually exists.

The privacy page must state: the legal basis (**Art. 6(1)(a)** consent), the retention period, the
withdrawal route, and name the **SMTP relay as a processor**. Because subscriber data sits in our
own Postgres, there is no processor to name for *storage* — a direct benefit of self-hosting.

> Drafted from standard German templates with explicit placeholders for legal entity name,
> address, USt-IdNr and managing director. **Template-grade text requiring review before going
> live. This is not legal advice.**

## 8. Verification

Automated:
- `gzip -9 -c website/index.html | wc -c` < 14,336, asserted in CI.

Manual gate before deploy:
- Submit with JS **on** and **off**.
- Confirm a subscriber lands in Listmonk with `deployment`, `use_case` and `source` populated.
- Confirm the double opt-in email arrives, and that it passes SPF/DKIM/DMARC on inspection.
- Confirm `/thanks`, `/impressum` and `/privacy` all resolve.
- Confirm the Listmonk admin UI is **not** reachable from the public internet.

## 9. Out of scope

The trailer, the campaign itself, page analytics beyond the `source` field, a German translation
of the sales page, and any cockpit or orchestrator change. Each is its own cycle.

## 10. Open questions

Flagged rather than guessed:

1. **Does Listmonk's public subscription endpoint accept arbitrary custom `attribs` via a plain
   form POST?** Listmonk stores subscriber attributes as JSONB, but whether the *public* form
   endpoint populates them directly needs confirming against a running instance. If it does not,
   the fallback is posting to Listmonk's API from a thin handler instead of the form endpoint.
2. **Listmonk captcha support** on the public form — needs checking; the honeypot is the baseline
   either way.
3. **Which EU relay** — Brevo vs Mailjet, on current free-tier send limits and DPA terms.
4. **What number changes our behaviour?** This is the most important open item and it is a
   business decision, not a technical one. The exercise is worth running only if we agree in
   advance what we do at, say, 30 signups versus 300 — whether that shifts the launch date,
   the positioning, the hosted-vs-self-host emphasis, or the pricing. Deciding after seeing the
   number invites us to rationalise whatever it turns out to be.

## Appendix — the rejected alternative

**Hosted list provider (MailerLite).** Lithuanian company, data in the EU (DE + NL, ISO 27001),
double opt-in by default, plain form POST endpoint, custom fields. Roughly **2 hours** of work
versus 1.5–2 days, and it hands you deliverability, unsubscribe handling and bounce management
already solved — the half of the problem that usually bites during launch week.

It was rejected in favour of owning the subscriber data on our own infrastructure, which is both
consistent with what the product sells and a materially simpler privacy story.

The honest counterweight, recorded so the decision can be revisited: **with a hosted provider you
inherit their sender reputation; self-hosted, launch-day deliverability is our problem.** If the
"we're live" email lands in spam, the entire waitlist was wasted. §4.4's domain-warming effect is
the mitigation, and it only works if we start sending confirmations now rather than at launch.

Also noted: MailerLite's free tier is now **250 subscribers / 2,500 emails per month** (cut from
1,000 → 500 → 250 over the past year), so the SaaS path would not have stayed free through a
campaign that worked.
