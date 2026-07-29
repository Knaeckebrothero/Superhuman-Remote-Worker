# Sales Page Improvement — Job Instructions

## Mission

Improve the SRW public sales page at `website/index.html` — fix what's broken, sharpen the
writing, add a German translation, and add one tasteful animation that shows an agent
actually working. Land the result as a **pull request on GitHub**.

The page is live at <https://superhuman-remote-worker.com/>. It is the only public sales
surface the company has. Treat it accordingly: this is revenue-facing, not a scratch file.

**Recommended expert:** `developer` (HTML/CSS + git + API work). `designer` also fits the
visual phases if you split the job.

**Required grant:** shell (`run_command`). The built-in `git_*` tools are **read-only** —
every commit, push, and API call in this job goes through the shell.

---

## Ground rules — do not break these

These are the page's existing hard constraints. They are not suggestions; the page's entire
value is that it loads instantly and works everywhere.

1. **≤14 KB gzipped, critical path.** Hard ceiling **14,336 bytes** for
   `gzip -9 -c website/index.html | wc -c`. Target ≤13,500 to leave headroom.
   Current baseline: **9,213 bytes**. The German page has the same independent ceiling.
   *Deferred assets (a `defer`-loaded script) do not count against this — only what the
   browser needs for first paint does.*
2. **No JS frameworks.** No React, no Vue, no Alpine, no jQuery. Vanilla JS only, and only
   where it earns its bytes.
3. **No web fonts, no raster images** beyond the existing `og-image.png` (which is only ever
   fetched by link-preview crawlers, never by the page itself).
4. **The page must be fully usable with JavaScript disabled.** Every CTA, every link, all
   content. This is currently violated — see Defect 1.
5. **Inline CSS only.** One `<style>` block. No external stylesheets.
6. **Do not touch `website/configure.html` or `website/generator.mjs`.** That's the Helm
   config generator, it has its own drift-gate test suite, and it is out of scope.
7. **Never invent a capability.** Every product claim on the page must be verifiable in this
   repo. If you cannot find it, cut the claim. See Phase 2.
8. **Do not run `kubectl` and do not deploy.** Your job ends at a merged-ready PR. A human
   does the rollout.

---

## Phase 0 — Orient and preflight

Do this first and completely. Phase 0 exists so you fail fast on the one thing that could
waste the entire job.

### 0.1 — Check out the repo

Use `checkout_project_repository` with the **SRW Repository** datasource
(`d7555d5d`). It clones to `~/repos/<repo_name>` with credentials already embedded in the
remote URL. Work there for the whole job.

### 0.2 — Read the context, in this order

- `docs/website.md` — why the page exists, the 14 KB rule, how it deploys. **Read all of it.**
- `docs/done/sales_page_landing_audit.md` — the last full audit (2026-05-29, resolved
  2026-06-25). Tells you what was already fixed and what was deliberately left alone.
- `website/index.html` — the page itself, top to bottom.
- `docker/Dockerfile.website` — how it ships. Note the **explicit `COPY` file list**.

### 0.3 — Record the baseline

```bash
gzip -9 -c website/index.html | wc -c      # expect ~9213
```

Start a budget ledger now. Every phase reports its byte delta against this number. A phase
that busts the ceiling gets trimmed before you move on, not at the end.

### 0.4 — PREFLIGHT: can you actually push? ⚠️

**Do this before writing a single line of code.** The mechanism below is known to work; what
is *unverified* is whether this datasource's token carries write scope.

```bash
cd ~/repos/<repo_name>
git remote get-url origin    # confirm it looks like https://oauth2:<token>@github.com/...
git fetch origin develop
git push --dry-run origin HEAD:refs/heads/preflight-$(date +%s)
```

`--dry-run` performs the full auth and permission handshake without writing anything.

- **Exit 0** → you have write access. Proceed, and plan to land a PR in Phase 5.
- **403 / "permission denied" / "not authorized"** → the token is read-only. **Do not
  abandon the job.** Complete Phases 1–4, commit locally, and follow the fallback in
  Phase 5.2. Report the failure prominently in your summary so the operator can fix the
  token scope.

Then confirm the API path works:

```bash
TOKEN=$(git remote get-url origin | sed -n 's|.*oauth2:\([^@]*\)@.*|\1|p')
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/Knaeckebrothero/Superhuman-Remote-Worker
```

Expect `200`.

> **Token hygiene — non-negotiable.** The token lives in the remote URL. Keep it in a shell
> variable. **Never `echo` it, never write it to a file, never paste it into a commit,
> a PR body, or your job summary.** Every tool call you make is recorded in the audit log.

### 0.5 — Branch

This job is the test case for the agent PR workflow, so it deliberately departs from the
normal "work directly on develop" convention — a PR needs a source branch:

```bash
git checkout develop && git pull origin develop
git checkout -b agent/sales-page-improvements
```

---

## Phase 1 — Fix what's broken

These three defects are **already confirmed**. Start here, don't re-derive them. Then do your
own audit pass for anything else.

### Defect 1 — Every sales CTA is a dead Cloudflare link 🔴

All five email links on the page point at
`/cdn-cgi/l/email-protection#<hex>` and depend on
`<script src="/cdn-cgi/scripts/…/email-decode.min.js">` at the bottom of the file.

`git log -S 'mailto:' -- website/index.html` returns **nothing** — a `mailto:` has never
existed in this file. The source was authored from a Cloudflare-*rendered* copy of the page
back in `33a2e9b5`, and the obfuscation has been baked into the source of truth ever since.

Why it's serious:

- The nginx container does not serve `/cdn-cgi/…`. Anything not proxied through Cloudflare —
  a local `podman run`, a preview deploy, a direct-to-origin request — gets a 404 on the
  decoder script and **five broken links**.
- Even behind Cloudflare, it makes the *only revenue contact path on the page*
  JavaScript-dependent, which violates ground rule 4.

**Fix:** restore real `mailto:` links in the source. If you want obfuscation, do it with
something that degrades gracefully and costs no external request. Cloudflare will re-apply
its own transform at the edge if that setting is still on — that's fine, that's how it's
meant to work. The *source* must be correct. Delete the `cdn-cgi` script tag.

The address is in `docs/done/sales_page_landing_audit.md` (Issue 1's link table). It also
decodes from the page's own `data-cfemail` attribute if you want to confirm it independently.
Do not guess it.

### Defect 2 — Render-blocking Google Fonts 🔴

```html
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700&display=swap">
```

Two independent problems:

- **Performance.** A render-blocking stylesheet on a third-party origin. The whole point of
  the 14 KB budget is first paint in one round trip; this adds a DNS lookup, a TLS handshake,
  and a blocking fetch to a host you don't control. The byte budget is currently being met
  and the performance goal missed anyway.
- **Legal.** Embedding Google Fonts without consent transfers the visitor's IP to the US and
  was ruled a GDPR violation by LG München I (3 O 17493/20, Jan 2022). This page sells
  *self-hosted, audit-trailed, GDPR-aware* AI to European businesses. Shipping a German
  privacy liability on the landing page is an own-goal, and a sharp buyer will notice.

**Fix — pick one and justify it in the PR:**

- **(a) Self-host a subset.** Cinzel 600/700, Latin only, `woff2`, `font-display: swap`,
  subset to the ~60 glyphs actually used in display headings. Add to the Dockerfile `COPY`
  list. Roughly 8–15 KB as a *separate file* — it does not touch the HTML budget.
- **(b) Drop it.** Use a system serif stack. Zero bytes, zero requests, zero legal exposure.
  The tradeoff is losing a distinctive part of the page's voice.

Recommend (a) — the previous audit specifically praised Cinzel for making the page not look
AI-generated. Keep the voice, lose the third party.

### Defect 3 — Primary CTA points at the dev environment 🟠

`https://cockpit.srw.works/signup` appears twice (hero + hosted lane). `srw.works` is the
**dev** cloud domain.

Verify what the correct production signup URL is before changing it. If a production hosted
cockpit isn't live yet, the honest fix is to change the CTA to something that works
(a demo request against the sales address) rather than sending buyers to a dev cluster.
**Do not invent a domain.** If you can't resolve this from the repo, leave it and flag it
in the PR.

### 1.4 — Your own audit

Now go find what I missed. Check at minimum:

- Every `href` resolves (`curl -sS -o /dev/null -w '%{http_code}'`). The 2026-05 audit found
  *eleven* dead links; assume rot has set in again.
- Heading hierarchy, landmarks, `alt`/`aria-label` on the SVGs, contrast ≥4.5:1,
  visible keyboard focus.
- Renders correctly at 360 px, 768 px, 1440 px.
- `og:`/`twitter:` meta complete and pointing at live URLs.

---

## Phase 2 — Copy pass

**Read this framing before you rewrite anything.** The copy is *good*. It is concrete, it
names real workflows, it avoids marketing fluff, and the last audit specifically found the
voice was working. Your job is a scalpel, not a rewrite. If you find yourself replacing a
specific sentence with a vaguer one, stop.

### Confirmed copy defects

**2.1 — The page advertises an expert that does not exist.** 🔴

The vignette "Triage support emails" is attributed to `secretary`. There is no secretary
expert. The real roster in `config/experts/` is:

```
assistant  bughunter  critic  curator  designer
designer-interactive  developer  general-worker  product-qa  scholar
```

The page's own "why" section correctly lists *scholar, developer, critic, curator, designer* —
so the page contradicts itself. Reattribute the vignette to a real role (`assistant` fits) or
cut it.

**This is the single most important fix in this phase.** A prospect who buys on that vignette
and finds no secretary role has been misled. Audit **every** role name, product claim, and
feature reference on the page the same way, against this repo.

**2.2 — Subject-verb agreement in the hero lede.**

> "…the research, invoice review, compliance scans, and code maintenance that **drains**
> your day."

Compound subject → "that **drain** your day." This is the first sentence a visitor reads.

**2.3 — The "Built on" section is a keyword dump.**

```
FastAPI · LangGraph · PostgreSQL/pgvector · MongoDB · Neo4j · Keycloak · Redis · Angular · Helm · Kubernetes
```

Ten names, no framing, no reason to care. It reads like an SEO block. Either give it a line
that makes it mean something to a technical buyer (what it implies about operating the thing)
or cut it and reclaim the bytes.

**2.4 — Six vignettes is a wall.**

Six near-identical cards is more than a skimming reader will absorb, and they flatten into
noise. Consider four strong ones, or vary the rhythm so the best two get more weight.
Use judgement — if you think six is right, keep six and say why in the PR.

### Rubric for any sentence you write

- **Specific beats impressive.** "Crawls product blogs, GitHub releases, and Hacker News
  every Monday at 8" is worth more than any adjective.
- **No AI-marketing register.** No "unlock", "revolutionize", "seamlessly", "empower",
  "game-changing", "harness the power of". The existing page avoids these; keep it that way.
- **Verbs over nouns.** "Agents cite every claim" beats "comprehensive citation capability".
- **Honest about limits.** The Fair Source framing is currently honest and clear. Do not
  soften it. Do not imply MIT/Apache-now.
- **Read it as a skeptical engineer** on a slow train who has seen forty AI landing pages
  this month. Would they believe a real person built this?

Leave the licensing copy alone unless it's factually wrong — it correctly reflects
FSL-1.1-ALv2, and it was carefully worded.

---

## Phase 3 — German page

Germany is the home market. This is a real conversion lever, not a nice-to-have.

### Mechanism: separate static pages

```
website/
  index.html        lang="en"   — canonical
  de/index.html     lang="de"
```

Not a JS toggle. Static pages keep the critical path at zero added bytes, stay indexable per
language, and work with JS off.

### Required plumbing — the page will not ship without this

1. **`docker/Dockerfile.website`** — the `COPY` is an *explicit file list*. Add the German
   page (and any font/script asset from other phases). Preserve the directory structure:

   ```dockerfile
   COPY website/index.html website/configure.html website/generator.mjs website/og-image.png /usr/share/nginx/html/
   COPY website/de/index.html /usr/share/nginx/html/de/
   ```

   **If you skip this, the file silently never reaches production.** This is the single
   easiest way to fail this job.

2. **nginx config** (same Dockerfile, the `printf` block) — clean `/de` URL:

   ```
   location = /de { try_files /de/index.html =404; }
   ```

3. **`hreflang`** on *both* pages, including a self-reference and `x-default`:

   ```html
   <link rel="alternate" hreflang="en" href="https://superhuman-remote-worker.com/">
   <link rel="alternate" hreflang="de" href="https://superhuman-remote-worker.com/de/">
   <link rel="alternate" hreflang="x-default" href="https://superhuman-remote-worker.com/">
   ```

4. **Language switch** in the nav. Plain links, `EN | DE`. Must survive the ≤760 px mobile
   rule that hides secondary nav items — the language toggle is not secondary.

5. **Translate the `og:`/`twitter:` meta and `<title>`** on the German page too. A German
   `og:description` is the whole point of having the page.

### Translation quality

- **Translate the pitch, not the words.** "Hand off the paperwork. Keep your team." must
  land as a German sales line, not as a literal rendering. Rewrite freely.
- **Sie**, not du. B2B register throughout.
- **Keep technical terms in English** where German developers use them in English:
  Workspace, Container, Agent, Repository, Cluster, Audit-Log, Deployment. Translating these
  into Denglisch reads as machine output and destroys credibility instantly.
- **Do not translate**: product names, expert role names (`scholar`, `critic`), tech names,
  code samples, the diff mockup, CLI snippets.
- **Compound nouns get long** — German runs ~15–30% longer than English. Re-check the layout
  at 360 px, especially buttons and the nav. Watch for overflow in the vignette cards.
- **License section:** translate carefully and conservatively. If a legal phrase is risky to
  render in German, keep the English term with a German gloss. Do not invent legal language.
- **Verify the German page's own byte budget** independently: `gzip -9 -c website/de/index.html | wc -c`.

---

## Phase 4 — The session animation

**Goal:** show an agent doing real work. The page currently *tells* you agents cite sources
and open diffs; nothing *shows* it happening. This is the missing proof.

### The one architectural rule

**Ship the finished state in the HTML. JS rewinds it and plays forward.**

Do not have JavaScript inject content. Write the completed transcript into the markup as
static HTML — exactly what a no-JS visitor should see — then let the deferred script hide it
and replay it. Consequences:

- JS disabled → visitor sees the complete, correct final state. Ground rule 4 satisfied.
- Script 404s or fails → same. No blank box, ever.
- Crawlers index the real content.

### Constraints

- `<script defer src="/replay.js">` — deferred, so it costs **zero** critical-path bytes.
  Budget it around **≤3 KB gzipped**. **Add it to the Dockerfile `COPY` list.**
- `IntersectionObserver` — start when it scrolls into view, not on load. Play **once**.
  Never loop; a looping animation on a sales page is an irritant.
- `prefers-reduced-motion: reduce` → skip the animation entirely, show the final state.
  Non-negotiable accessibility requirement.
- `aria-hidden` on the decorative mock, consistent with the existing mockups.
- No layout shift. Reserve the final height up front so the page doesn't jump — CLS is part
  of the Lighthouse score this page is judged on.
- Never animate above-the-fold content in a way that delays the reader getting the pitch.

### What to animate

Your call, but the strongest candidate is a **job transcript replay**: job submitted →
agent picks up in a workspace → runs a couple of tools → returns a cited answer → status
flips to done. That's the product's whole loop in about eight seconds.

The existing hero chat mockup and the job dashboard table are both natural hosts. The
dashboard is probably the better one — watching a status pill go `queued → running 62% →
awaiting review` is legible in a way a typing animation isn't, and it reuses markup that
already exists.

**Taste check:** one animation, done well. This is a page for engineers. If it feels like a
startup template, you've overdone it — cut it back.

---

## Phase 5 — Land it as a pull request

### 5.1 — Verify before you claim anything

Run all of these. Paste the actual output into your summary — do not assert success you
haven't observed.

```bash
# 1. Byte budget — both pages, hard ceiling 14336
gzip -9 -c website/index.html    | wc -c
gzip -9 -c website/de/index.html | wc -c

# 2. Drift gate — CI hard-fails on this
node --test website/test/generator.unit.test.mjs website/test/generator.drift.test.mjs

# 3. The image actually builds and serves what you think it does
podman build -t srw-website-test -f docker/Dockerfile.website .
podman run -d --rm -p 8099:80 --name srw-web-test srw-website-test
curl -sS -o /dev/null -w 'en   %{http_code}\n' http://localhost:8099/
curl -sS -o /dev/null -w 'de   %{http_code}\n' http://localhost:8099/de
curl -sS -o /dev/null -w 'js   %{http_code}\n' http://localhost:8099/replay.js
curl -sS -o /dev/null -w 'conf %{http_code}\n' http://localhost:8099/configure
podman stop srw-web-test
```

Every one of those must be `200`. **The `/de` and `/replay.js` checks are the ones that
catch a forgotten `COPY` line** — the exact failure that would otherwise ship a German page
that doesn't exist in production.

Then, with the container running, open it in the browser tool and actually look at it:
desktop and mobile widths, both languages, JS on and off.

### 5.2 — Commit and open the PR

```bash
git add -A
git commit -m "feat(website): fix dead CTAs, self-host fonts, add DE page and session replay"
git push -u origin agent/sales-page-improvements
```

Then open the PR:

```bash
TOKEN=$(git remote get-url origin | sed -n 's|.*oauth2:\([^@]*\)@.*|\1|p')
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/Knaeckebrothero/Superhuman-Remote-Worker/pulls \
  -d @pr-body.json | jq -r '.html_url // .message'
```

Build `pr-body.json` with `jq` rather than hand-writing JSON (the body is long and
multi-line, and unescaped newlines will produce a confusing 422):

```bash
jq -n --arg title "..." --arg head "agent/sales-page-improvements" \
      --arg base "develop" --rawfile body PR_BODY.md \
      '{title:$title, head:$head, base:$base, body:$body}' > pr-body.json
```

**Target `develop`, not `main`.** Delete `pr-body.json` and `PR_BODY.md` from the working
tree before committing — they are scaffolding, not deliverables.

**If Phase 0.4 showed the token is read-only:** commit locally on the branch, skip the push
and the API call, and state clearly at the top of your summary that the PR could not be
opened because the datasource token lacks write scope. Leave the branch and commits in the
workspace so a human can push them. That is a successful job with one blocked step — not a
failure.

### 5.3 — PR description

Write it for a reviewer who has not seen this brief:

- What changed, grouped by phase.
- **The byte-budget table** — before/after for both pages, against the 14,336 ceiling.
- Every product claim you changed or removed, and what you verified it against. Call out the
  `secretary` fix explicitly — it's the one a reviewer most needs to know about.
- Screenshots: desktop + mobile, EN + DE.
- Anything you deliberately left alone, and why.
- Open questions for the human (the production signup URL is likely one).

**Do not deploy.** No `kubectl`. Merging and `kubectl rollout restart deploy/srw-sales-page -n srw-sales-page`
are the operator's call.

---

## Definition of done

- [ ] Both pages under **14,336 bytes** gzipped, measured and reported
- [ ] Zero dead links — every `href` returns 2xx/3xx, verified with `curl`
- [ ] No third-party requests at render time (no Google Fonts, no `cdn-cgi` script)
- [ ] Page fully usable with JavaScript disabled — **all CTAs work**
- [ ] Every product claim verified against this repo; `secretary` gone
- [ ] `website/de/index.html` exists, is real German, and is reachable at `/de` **in the built image**
- [ ] `hreflang` on both pages, language toggle visible on mobile
- [ ] Animation is deferred, plays once, respects `prefers-reduced-motion`, degrades to final state
- [ ] `docker/Dockerfile.website` `COPY` list updated for **every** new file
- [ ] `node --test website/test/*.mjs` passes
- [ ] `podman build` succeeds; `/`, `/de`, `/configure`, and the script all return 200
- [ ] Branch pushed and PR opened against `develop` — **or** the read-only-token blocker is
      clearly reported with commits left on the branch
- [ ] No token, in any form, in any commit, file, PR body, or job output

---

## Appendix — quick reference

| Thing | Value |
|---|---|
| Repo | `github.com/Knaeckebrothero/Superhuman-Remote-Worker` |
| Datasource | **SRW Repository** — `d7555d5d` |
| Base branch | `develop` |
| Work branch | `agent/sales-page-improvements` |
| Live URL | <https://superhuman-remote-worker.com/> |
| Budget ceiling | 14,336 bytes gzipped, per page |
| Baseline | 9,213 bytes |
| Budget check | `gzip -9 -c website/index.html \| wc -c` |
| Drift gate | `node --test website/test/generator.unit.test.mjs website/test/generator.drift.test.mjs` |
| Deploy (human, not you) | push → CI builds image → `kubectl rollout restart deploy/srw-sales-page -n srw-sales-page` |

**Files you may change:** `website/index.html`, `website/de/index.html` (new), any new
font/script asset under `website/`, `docker/Dockerfile.website`.

**Files that are off-limits:** `website/configure.html`, `website/generator.mjs`,
`website/test/*`, anything outside `website/` and `docker/Dockerfile.website`.
