# Strategy, Funding & Next Steps — Working Notes

> **Date:** 2026-06-04
> **Status:** Personal strategy memo (not a product design doc).
> **Privacy:** Exact personal/financial figures have been generalized. Lives in a private repo.
> **Caveat:** Captures a strategy conversation, not professional advice. The German tax/legal/EXIST specifics below should be confirmed with your university's Gründungsberatung and a Steuerberater. Program terms change yearly.

---

## 1. Where things actually stand

- Solo developer, ~6 months in (last 2 full-time). Quit a working-student job and turned down a well-paid job offer to pursue this.
- The system ("agent harness" / remote-worker platform) is technically deep and broad: orchestrator with NATS lifecycle scoping, headscale mesh networking (HA + ephemeral-key GC), per-session workspace provisioning with reconcilers, a BFF auth layer over Keycloak, OpenCloud group reconciliation, code-server settings sync, etc. This is serious distributed-systems work — well beyond a hobby project.
- **Considered feature-complete enough to release** (sessions + jobs). Deliberately deferring OneDrive/Google Drive sync and cost monitoring. **No product name agreed yet.**
- **Finances:** carrying some personal debt; a modest monthly burn (AI subscriptions + server power); no income yet. Some deductible costs / a potential loss-carryforward available. Taxes done solo via WISO Steuer (not yet filed for the relevant year; tax deductions still unclaimed). **No business tax number yet → currently can't legally invoice anyone.**
- **Clients (2 pilots):**
  - Client A — further along; Kubernetes install started on their infra, but the **consulting contract (~10h/week, hourly rate) is unsigned** and they're cost-reluctant. The founder is the AI champion; the IT person and co-founder don't really get it (classic champion-vs-blocker dynamic).
  - Client B — ~2–3 weeks out from a real demo.
- **People:** A friend is interested but won't quit their job. A professor wants to co-found (proposed 50/50; would handle org/sales/legal, isn't a coder, and is currently time-constrained by university work).

---

## 2. The core reframes

**The imposter feeling isn't matched by the evidence.** What's been built is more than most funded teams ship. Solo-founding something this broad is *structurally* hard — that's a "not enough hands" problem, not a "not capable" problem. Different problem, different fix.

**It's not one decision — it's ~six separate ones.** Much of the dread comes from treating open-source-vs-SaaS, naming, the partnership, funding, hardening, and incorporation as a single undifferentiated knot. They're separable and don't all have to be answered now.

**The two scaling fears cancel out.** Fearing "1000 subscribers week one, can't scale" *and* "infra sits idle" simultaneously is the tell that this is anxiety, not analysis. SaaS infra is elastic/pay-as-you-go — you don't pre-buy a cluster. Cost tracks paying users; idle infra only happens if you over-provision up front.

**SaaS is the *harder/riskier* path right now, not the escape hatch.** Multi-tenant SaaS means holding other people's code, data, **and API keys**, where one isolation bug leaks across customers — and you carry the liability, solo, no company, with EU GDPR exposure as a data processor. It swaps slow-but-low-risk problems (B2B sales cycles) for fast-but-high-risk ones.

**The root problem: "a Ferrari for people who don't have roads."** The product assumes Kubernetes, spare servers, and AI literacy. Firms big enough to have that (SAP-scale) are unreachable for a solo dev with no company. Firms you *can* reach (1–2 IT staff, a few Proxmox boxes, no k8s, won't spend ~€2k) can't run it and don't get it. Neither "grind sales harder" nor "flip to SaaS" fixes this alone.

---

## 3. Recommended product/GTM direction

1. **Open-source the core under AGPL (or BSL/SSPL).**
   - On the "someone builds a SaaS on my open source before I can" fear: low for an unknown project — nobody strip-mines a repo with zero users; that risk only appears *after* you're popular enough to be worth copying (a good problem). By then you hold the brand, roadmap, and hosting head start.
   - It's a **license choice, not a law of nature**: MIT/Apache *enables* strip-mining; **AGPL** forces anyone offering it as a network service to open their modifications (a hard deterrent to closed competitors); **BSL/SSPL** are source-available but forbid others selling it as hosting for N years. Pick AGPL/BSL for the core → strip-mining risk goes from "low" to "near zero," while keeping the credibility/funnel benefits of being open.
   - The real risk isn't being copied; it's **not being adopted at all**.

2. **Sell managed *single-tenant* hosting as the paid product** (not multi-tenant public SaaS, at least not first).
   - Kills the SMB's #1 objection ("no k8s / won't buy a €2k server") — turns their capex into your subscription.
   - Sidesteps the multi-tenant security nightmare entirely: one isolated instance per customer, no cross-tenant blast radius.
   - Plays to your strength — you already deploy this system into infra; now it's *your* infra.
   - A dozen instances at a few hundred €/mo is "enough to keep building," and far safer than a public launch. Downside: less efficient, more ops per customer (you become a bit of an MSP) — but that's a scaling problem you'd be lucky to have.

3. **Kill deployment friction** (one-VM / docker-compose / "appliance" install). This widens your reachable market more than any sales push and is arguably the highest-leverage engineering work on the list.

4. **You don't need a multi-tenant public SaaS to hit the stated goal** ("enough to keep building, not millions"). Open-core for credibility + single-tenant hosting + maybe some consulting = sustainable and low-risk.

5. **Reddit stars ≠ revenue.** OSS-to-paid conversion is often <1%. Open source is a credibility / top-of-funnel play, not a revenue plan by itself.

6. **The name is not a blocker.** Pick a working name, ship, rename later. Inability to agree on a name with the professor is information about the partnership, not the product.

---

## 4. People / partnership (honest notes)

- **Professor at 50/50** with no code contribution and limited availability (still tied up with university) = a commitment mismatch, which is a top startup-killer. Don't hand over half the company outright. Use **vesting with a cliff**, or keep him as a **non-equity advisor** until he's proven he'll match your pace. (See EXIST below — the *mentor* role is a great way to give him a concrete, bounded, valuable job.)
- **Client A's unsigned contract:** you've started real work (k8s install) for a reluctant payer *before* a signed contract. That's giving away leverage out of scarcity. **Signed contract first, then work.**

---

## 5. Legal / tax setup (Germany)

**You don't *legally* need a Steuerberater** — not for private taxes, not even for a GmbH/UG. The "you'll need one no matter what" advice becomes *practically* true the moment you incorporate (a UG/GmbH triggers double-entry bookkeeping, an annual Jahresabschluss, and Körperschaftsteuer/Gewerbesteuer/USt/Soli filings — genuinely a job). As a sole proprietor on a simple EÜR, you + WISO is fine.

**Decouple "being able to bill" from "hiring a Steuerberater."**
- You already have a **Steuer-ID** (lifelong, personal). To invoice you need a **Steuernummer** — obtained by submitting the *Fragebogen zur steuerlichen Erfassung* via ELSTER after registering as Einzelunternehmer/Freiberufler. **Free, DIY, ~2 weeks.** (Plus a **USt-IdNr** for EU B2B if needed.)
- So you can bill Client A **this month**, as a sole proprietor, without paying an advisor or forming a company. *(But see the EXIST founding-timing rule before going actively into business.)*

**Your "one Steuerberater for both" instinct is correct** — that's the standard setup. One handles private Einkommensteuer + business bookkeeping + legal-form advice + Finanzamt registration; for a UG they pair with a notary. When you engage one, pick one who works with *Existenzgründer/startups* and knows digital/SaaS VAT.

**Two places a Steuerberater earns the fee soon:**
- **VAT choice:** new founders auto-default to *Kleinunternehmer* (no VAT charged) under the limits raised in 2025 to **€25k prior year / €100k current year**. But with real input costs (your servers+AI spend carrying ~19% VAT) and B2B clients who don't care about VAT, you may be better off **waiving Kleinunternehmer to reclaim input VAT (Vorsteuer)**.
- **Capturing your accumulated deductible costs as a proper Verlustvortrag** (loss carryforward) now, while you have costs and little income, so it shields future profit. Easy to mishandle solo.
- (Also: the fuzzy **freiberuflich vs. gewerblich** classification — software *consulting* can be freelance; selling a SaaS/hosting *product* is a trade.)

**Cost ballparks (StBVV-scaled; get quotes — most do a free/cheap Erstgespräch):**
- Simple/student private return: low hundreds €.
- Ongoing UG/GmbH bookkeeping + filings: ~**€150–500/month**.
- UG formation: ~€400 min (1-person Musterprotokoll) up to ~€800–1,000+ with notary/Handelsregister/Gewerbeanmeldung; ~3–4 weeks.

---

## 6. EXIST-Gründerstipendium (the high-leverage lead)

**Why it fits you almost perfectly:** grad student + a willing professor (ideal mentor) + innovative deep-tech product + needs runway.

**What it pays (12 months):**
- Personal stipend: **€2,500/mo graduates**, €1,000/mo enrolled students, €3,000/mo PhDs (+€150/mo per child). *(Confirm your bracket.)*
- Up to **€10,000** non-personnel/material costs (servers count).
- Up to **€5,000** for coaching / entrepreneurship advice (can fund a Steuerberater).
- **Free workspace + infrastructure** from the university.
- Requires a **university host** and a **mentor from the university** — the bounded, non-coding role the professor is suited for.

**Downsides / strings (the answers to "what's the catch"):**
- **Repayment? No** — it's a *nicht rückzahlbarer Zuschuss* (non-repayable grant), even if the startup fails. No equity taken, no loan.
- **Open source? No obligation.** That's a myth. Separate real issue = **Hochschul-IP**: since 2023 the university is expected to license the venture any *university-owned* IP it needs "at market-standard conditions" (possibly fees). If your system is purely your own solo build (not as a uni employee, not from uni research/resources), the IP is yours and there's nothing to license. **Clarify ownership with the tech-transfer office *before* applying** — don't let the professor's involvement blur it.
- **Full-time** on the venture; it's your main activity.
- **Paid side work ≤ 5h/week** (hard weekly cap).
- **No other income-replacing funding** stacked on top.
- **Milestones/reporting** (business plan, coaching, reviews).
- **Founding-timing rule (important):** must **not** have already founded a UG/GmbH *and started trading before the project begins* → disqualifying. You *can* incorporate *during* the funded year (normal, near the end). **⇒ If EXIST is on the table, do NOT form a UG now. EXIST decision first, incorporation second.**
- **Taxable:** unlike research scholarships, EXIST is declared as income (Anlage SO, *sonstige Einkünfte*), though offset by business expenses; there's case law arguing some founder stipends aren't taxable, so it's contested. For you (low other income + heavy deductibles) the *effective* tax is likely small — but it's **not** a clean tax-free €2.5k.

**Key consequence:** EXIST and "just sort out the consulting contracts" **partly cancel** — the 5h/week cap blocks a 10h/week gig, and going actively into business now can complicate eligibility. So it's largely **one path or the other**, not both.

### The funding calculus

| Option | Upside | Cost / strings |
|---|---|---|
| **EXIST** | Non-repayable; ~€2.5k/mo + up to €10k materials + €5k coaching + free infra; a year of structure; real role for the professor; **removes the contract-chasing you dislike** | Full-time on it; ≤5h/week paid side work; no other income-replacing funding; can't pre-incorporate; reporting/milestones; stipend taxable (low effective tax for you) |
| **Parents** | No strings; flexible timing | Family money/debt; no materials/coaching/infra; no professor engagement; you still do all GTM alone |
| **Consulting (hourly)** | Real, validating market income; no strings | Unsigned/uncertain; admin grind you dislike; caps build time; active business now may clash with EXIST eligibility |

**Bottom line:** If you're willing to treat the next 12 months as *head-down, get-to-launch-and-incorporate, minimal side income*, **EXIST is the best deal on the table** — and it specifically buys away the business-contract overhead you want gone. It only looks worse if your near-term plan *depends* on parallel consulting cash. Parents-money is strictly worse than EXIST except that it doesn't impose the full-time/timing strings.

---

## 7. Next actions (in order)

1. **Book a meeting with your university's Gründungsberatung / Transfer office this week.** They know EXIST cold, advise for free, and the founding-timing rule means you want this *before* any incorporation step. Confirm: eligibility bracket, IP ownership, application timeline.
2. **Engage the professor concretely** via the EXIST mentor role (a bounded, valuable, non-coding job) — a one-pager framing "product / why it fits EXIST / your mentor role / what each of us gets" can convert lukewarm interest into engagement.
3. **Decide EXIST vs. consulting** (largely mutually exclusive). Lean EXIST if you want to be head-down building for a year.
4. **If going the consulting/sole-proprietor route instead:** register as Einzelunternehmer/Freiberufler, get the Steuernummer via ELSTER (free), get Client A's contract **signed before more work**.
5. **Engage a Steuerberater at incorporation** (not before) — funded partly by EXIST's €5k coaching budget if you're in. Use them for the VAT-waiver decision, Verlustvortrag, and freiberuflich/gewerblich call.
6. **Pick a license** (AGPL recommended for the core) — neutralizes the strip-mining fear.
7. **Pick a working product name** — don't let it block anything.
8. **Engineering:** prioritize **deployment friction** (one-command install) over more features; harden the path to one paying user; OneDrive/GDrive + cost monitoring can wait.

---

## 8. Things to verify (don't take these as settled)

- Your exact EXIST stipend bracket (student vs. graduate) and eligibility window.
- IP ownership of the system vs. any Hochschul-IP claim (tech-transfer office).
- EXIST tax treatment for your case (Steuerberater) and whether anything stacks as de-minimis aid.
- Whether starting any business activity now would affect EXIST eligibility.
- Current Kleinunternehmer thresholds and whether to waive (Vorsteuer math).
- Health-insurance impact of going self-employed as a student (can knock you off cheap student/family insurance — a common founder surprise; ask the Gründungsberatung).

---

## Sources

- [EXIST-Gründungsstipendium — program terms (exist.de / BMWK)](https://exist.de/en/programm/exist-gruendungsstipendium/)
- [EXIST overview & funding amounts — Für-Gründer](https://www.fuer-gruender.de/kapital/foerdermittel/zuschuss/exist-gruenderstipendium/)
- [Side-activity ≤5h rule — Existenzgründungsportal (BMWK)](https://www.existenzgruendungsportal.de/Redaktion/DE/BMWK-Infopool/Antworten/Foerderung-Finanzierung/Foerderung/EXIST-Gruenderstipendium/EXIST-Gruenderstipendium-Nebenerwerb-erlaubt)
- [Is EXIST tax-free? — Existenzgründungsportal (BMWK)](https://www.existenzgruendungsportal.de/Redaktion/DE/BMWK-Infopool/Antworten/Foerderung-Finanzierung/Foerderung/EXIST-Gruenderstipendium/EXIST-Gruenderstipendium-steuerfrei)
- [Non-repayable grant overview — Gründerplattform](https://gruenderplattform.de/finanzierung-und-foerderung/exist-gruendungsstipendium)
- [EXIST and university IP ("Hochschul-IP") — StartingUp](https://www.starting-up.de/recht/marken-patentschutz/exist-und-hochschul-ip.html)
- [Kleinunternehmerregelung & €25k/€100k limits — IHK Region Stuttgart](https://www.ihk.de/stuttgart/fuer-unternehmen/recht-und-steuern/steuerrecht/umsatzsteuer-national/kleinunternehmerregelung-in-der-umsatzsteuer-1843632)
- [UG tax obligations & Steuerberater costs — Integral](https://www.integral.de/de/ratgeber/steuererklaerung-ug)
