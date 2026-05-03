# Website

## Why we need one

- **Startup program applications** require a public website (Neo4j Startup Program triggered this — others will too: Cloudflare for Startups, AWS Activate, GCP for Startups, Microsoft for Startups, MongoDB Atlas, OpenAI Startup, Anthropic Startups, etc.).
- **Credibility** — investors, partners, and customers Google us before any first call. No site = doesn't exist.
- **Top-of-funnel** — somewhere to send people from cold outreach, demos, conference talks, GitHub README.
- **SEO surface** — own our brand keywords before someone else does.

## Goals (in priority order)

1. Pass startup-program "do you have a website?" check today.
2. Communicate what the product is in <10 seconds for a non-technical reader.
3. Give a technical reader enough depth to want a demo (architecture diagram, screenshots, maybe a short video).
4. Capture leads (email signup, "request demo" form, or at minimum a contact mailto).
5. Be cheap to maintain — no CMS, no backend, deploy on git push.

## Open questions (to discuss)

- **Domain** — do we have one yet? What's the company/product name we're going with publicly?
- **Audience split** — single page for both technical buyers and program reviewers, or `/` for marketing + `/docs` or `/developers` for technical? (All under the marketing apex domain — the cockpit stays on `app.*`.)
- **Branding** — logo, color palette, typography. Do we have any existing assets from the cockpit we can lift?
- **Demo strategy** — embed a Loom/video, link to a live sandbox, or just screenshots? Live sandbox is risky (auth, abuse, cost).
- **Open source posture** — link to the GitHub repo prominently, or keep it understated until we decide on licensing?
- **Legal pages** — Imprint (Impressum, required in DE), Privacy Policy (GDPR), Terms. These are non-negotiable for an EU-facing site.
- **Contact** — form (needs backend or Formspree-style service), or just an email? Calendly link for booking demos?

## Content sketch (one-pager v1)

1. **Hero** — one-line value prop + sub-line + primary CTA ("Request a demo" / "View on GitHub").
2. **Problem** — what pain we solve, in customer language not engineering language.
3. **Solution** — 3-4 feature blocks with icons or screenshots (orchestration, agent autonomy, knowledge graph, browser automation).
4. **How it works** — architecture diagram (orchestrator → agents → workspaces), 2-3 sentences.
5. **Screenshots** — cockpit, a job in flight, a knowledge graph view.
6. **Tech stack credibility** — "Built on" logos (Neo4j, PostgreSQL, Kubernetes, LangGraph, Anthropic, OpenAI). Doubles as social proof for startup program reviewers.
7. **Team** — short bios + photos. Investors and program reviewers care a lot about this.
8. **Contact / CTA footer**.

## Architecture: marketing site ≠ webapp

The website and the cockpit are **two separate deployments on two separate subdomains**, not one combined app. This is the industry-standard split (OpenAI / Anthropic / Stripe all do this).

| | Marketing site | Cockpit (existing) |
|---|---|---|
| **Domain** | `brand.com` (apex) | `app.brand.com` |
| **Purpose** | Acquire visitors | Serve authenticated users |
| **Audience** | Anonymous, often on mobile, ~3s attention span | Logged-in users, session lasts hours |
| **Stack** | Static HTML (Astro), zero JS by default | Angular SPA |
| **Auth** | None | Keycloak OIDC |
| **Deploy** | Cloudflare Pages (CDN edge, free) | K8s pods behind ingress (as today) |
| **Caching** | Aggressive, hours-days at edge | None / short |
| **Update cadence** | Founders push copy any time | Eng releases via CI/CD |
| **Repo** | Separate repo (or `website/` subdir) | This monorepo |

The handoff is just an `<a href="https://app.brand.com">Sign in</a>` — no shared code, no shared build, no shared deploy. Independent failure domains: a broken cockpit deploy can't take down the marketing site and vice versa.

### Why not Angular for the marketing site

Angular is built for stateful logged-in app shells. Wrong tool for marketing because:

- **Bundle size**: Angular runtime is ~150-300KB gzipped *before any of our code*. The cockpit is ~1.5MB total — fine for an app a user spends an hour in, fatal for a sales page where the median visit is <10 seconds.
- **The 14KB rule**: TCP slow-start's initial congestion window is ~14KB. Above-the-fold HTML+CSS that fits in the first round trip feels instant; anything bigger waits for a second round trip and feels sluggish. Static-site generators routinely hit this. Angular cannot.
- **SEO**: Googlebot does run JS but inconsistently and with delay. Server-rendered (or static-rendered) HTML in the initial response is the only reliable way to rank.
- **Lighthouse**: marketing sites are judged on Performance / SEO scores ≥95. Angular SPAs typically land in the 60-80 range without significant SSR effort.

If we ever need to reuse a cockpit component (a screenshot carousel, a graph viewer demo), Astro has an Angular integration that hydrates a single component as an island — we get reuse without shipping the whole framework.

### The trap to avoid

A common mistake is making the unauthenticated `/` of the SPA double as the marketing page. This always ends badly:

- Marketing visitors download the entire app bundle just to read a pitch
- Bounce rate spikes, SEO suffers
- Marketing team can't iterate copy without engineering involvement
- Shared cookie scope causes auth/analytics weirdness

Keep them separate from day one.

## Stack options (to pick one)

| Option | Pros | Cons |
|---|---|---|
| **Astro + Tailwind on Cloudflare Pages** | Fast, SEO-friendly static, MDX for content, free hosting, great DX, can island-mount Angular components later if needed | Need to design from scratch |
| **Next.js on Vercel** | Familiar React, easy interactive components | Overkill for static, Vercel pricing if traffic spikes |
| **Plain HTML/CSS on GitHub Pages** | Zero dependencies, ship today | Hard to evolve, no component reuse |
| **Framer / Webflow** | No-code, fast to ship, designer-friendly templates | Monthly fee, vendor lock-in, harder to version-control |
| **Hugo / 11ty** | Static, fast builds, mature | Templating language is its own learning curve |

**Tentative recommendation**: Astro + Tailwind on Cloudflare Pages. Static (so cheap and fast), component-based (so we can iterate), MDX (so blog posts later are trivial), Cloudflare gives us the domain/DNS/CDN/analytics in one place, and we already use Cloudflare for the dev platform MCP.

## Best practices to keep in mind

- **Performance budget**: Lighthouse ≥95 on mobile. Above-the-fold HTML+CSS ≤14KB compressed (the TCP slow-start initial congestion window) so the first paint lands in one round trip. No 2MB hero videos.
- **Accessibility**: semantic HTML, alt text, keyboard nav, color contrast ≥4.5:1.
- **GDPR**: no Google Analytics by default — use Cloudflare Web Analytics or Plausible (cookieless). Cookie banner only if we actually set non-essential cookies.
- **Open Graph + Twitter cards**: every page needs `og:image`, `og:title`, `og:description` for link previews.
- **Sitemap + robots.txt**: trivial with Astro, helps SEO from day one.
- **No dark patterns**: one CTA per section, no exit-intent popups, no fake urgency.
- **Mobile-first**: most program reviewers will skim on a phone between meetings.

## Phasing

- **Phase 0 (this week)**: Register domain. Stand up a single-page placeholder ("X — coming soon, contact: email") so the Neo4j application has something to point at. 1-2 hours.
- **Phase 1 (next 1-2 weeks)**: Build the one-pager v1 above. Astro scaffold, content draft, screenshots, deploy. Imprint + privacy page.
- **Phase 2 (later)**: Blog/changelog, docs subdomain, demo video, lead capture form, A/B testing the hero copy.

## Decisions needed before building

- [ ] Final product/company name and domain (apex `brand.com` for marketing, `app.brand.com` for cockpit)
- [ ] DNS provider (default: Cloudflare, since we already use Cloudflare Pages and the dev platform MCP)
- [ ] Visual identity (logo, colors, fonts) — even rough
- [ ] Stack choice (default: Astro + Tailwind + Cloudflare Pages)
- [ ] Repo location (this monorepo under `website/`, or separate repo — separate is cleaner given the deploy split)
- [ ] Who writes the copy (you, partner, or draft together)
- [ ] Legal text source (template vs lawyer-reviewed)
