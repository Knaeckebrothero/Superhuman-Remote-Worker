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
- **Audience split** — single page for both technical buyers and program reviewers, or `/` for marketing + `/docs` or `/developers` for technical?
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

## Stack options (to pick one)

| Option | Pros | Cons |
|---|---|---|
| **Astro + Tailwind on Cloudflare Pages** | Fast, SEO-friendly static, MDX for content, free hosting, great DX | Need to design from scratch |
| **Next.js on Vercel** | Familiar React, easy interactive components | Overkill for static, Vercel pricing if traffic spikes |
| **Plain HTML/CSS on GitHub Pages** | Zero dependencies, ship today | Hard to evolve, no component reuse |
| **Framer / Webflow** | No-code, fast to ship, designer-friendly templates | Monthly fee, vendor lock-in, harder to version-control |
| **Hugo / 11ty** | Static, fast builds, mature | Templating language is its own learning curve |

**Tentative recommendation**: Astro + Tailwind on Cloudflare Pages. Static (so cheap and fast), component-based (so we can iterate), MDX (so blog posts later are trivial), Cloudflare gives us the domain/DNS/CDN/analytics in one place, and we already use Cloudflare for the dev platform MCP.

## Best practices to keep in mind

- **Performance budget**: Lighthouse ≥95 on mobile. No 2MB hero videos.
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

- [ ] Final product/company name and domain
- [ ] Visual identity (logo, colors, fonts) — even rough
- [ ] Stack choice (default: Astro + Tailwind + Cloudflare Pages)
- [ ] Repo location (this monorepo under `website/`, or separate repo)
- [ ] Who writes the copy (you, partner, or draft together)
- [ ] Legal text source (template vs lawyer-reviewed)
