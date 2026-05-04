# Egress proxy pool: route browser/HTTP traffic off the platform IP

## Problem

When the system is offered to the public (or B2B), all browser-automation
traffic — Playwright/`browser-use` page loads, paper downloads, web search
fetches — exits via the same handful of egress IPs we operate from (today
the homelab uplink; later a colo or cloud egress). Two failure modes follow:

1. **IP reputation collapse.** Cloudflare, Akamai, PerimeterX, DataDome, and
   the major SaaS WAFs aggressively rate-limit / CAPTCHA-wall / outright
   block IPs that exhibit headless-browser fingerprints at scale. With 1000
   tenants × N concurrent agents browsing from one ASN, we will get
   range-flagged within days. Once flagged, the entire customer base loses
   browse capability simultaneously — including the B2B customers whose own
   sites we'd otherwise have a clean reason to hit.
2. **Cross-tenant blast radius.** A single noisy tenant (scraping job,
   misbehaving agent loop, abuse) burns the shared IP for everyone. There
   is no way today to isolate one tenant's egress reputation from another's.

Browser capability is a top-priority feature of the product
([feedback_browser_priority](../../../.claude/projects/-home-ghost-Repositories-Superhuman-Remote-Worker/memory/feedback_browser_priority.md)).
If browsing breaks at scale, the product breaks at scale.

## Current state

There is already a `ProxyConfig` abstraction in
`src/tools/research/utils/network.py` that supports HTTP and SOCKS5
proxies. It is used by:

- `src/tools/research/browser.py:319-326` — passes the proxy to
  `browser-use` via `ProxySettings`.
- `src/tools/research/workflow.py`, `papers.py`, `web.py` — uses
  `research_request()` (proxy-aware aiohttp wrapper) for paper search /
  download.

It is loaded from **a single proxy** per agent process, sourced from:

- `config/defaults.yaml:195-201` (`research.proxy.{enabled,type,host,port}`)
- env vars (`RESEARCH_PROXY_TYPE`, `RESEARCH_PROXY_HOST`, ...)

That is enough for the original use case (one institutional VPN per agent
for paywalled-paper access) but is **not** enough for multi-tenant SaaS:

- No tenant → proxy mapping. Every job uses the same upstream proxy.
- No pool / rotation. A burned proxy IP sticks until config is edited.
- No orchestrator-side injection. The proxy is baked into the agent image
  config, not dispatched per-job.
- No per-tenant accounting. We can't bill tenants for egress / proxy use,
  cap their usage, or quarantine an abusive one.
- `browser_direct.py` and the direct-Playwright paths in `papers.py` (~L439
  `from browser_use import Agent, Browser`) need to be audited to confirm
  they all consult `ProxyConfig` — not just the `browser.py` workflow.
- `workflow.py:42` imports `get_proxy_from_context` *inside the function*,
  so any tool that constructs its own `aiohttp.ClientSession` outside
  `research_request()` silently bypasses the proxy. Need to grep for
  direct `aiohttp.ClientSession()` / `httpx.AsyncClient()` usage in tool
  code.

## Proposed design

### 1. Provider abstraction

Treat the egress proxy as a **datasource-shaped resource**, owned at the
project (or workspace) level the same way DB connections and the workspace
are today:

```
proxies (table)
  id              UUID PRIMARY KEY
  owner_user_id   UUID REFERENCES users(id)        -- NULL for system pool
  project_id      UUID REFERENCES projects(id)     -- NULL for global
  name            TEXT   -- "Bright Data residential EU", "self-hosted EU-1"
  provider        TEXT   -- 'brightdata' | 'oxylabs' | 'smartproxy' | 'self_hosted' | 'tor'
  type            TEXT   -- 'http' | 'socks5'
  host            TEXT
  port            INT
  username_secret TEXT   -- vault path or AES-encrypted blob
  password_secret TEXT
  rotation        TEXT   -- 'sticky_session' | 'per_request' | 'none'
  geo             TEXT   -- 'EU' | 'US' | 'DE' | NULL
  monthly_gb_cap  INT    -- soft cap for billing/quota
  enabled         BOOLEAN
  created_at      TIMESTAMPTZ
```

Migration ships under `orchestrator/database/migrations/app/NNNN_proxies.sql`
per the runbook in `docs/db_migration.md`.

### 2. Selection policy (orchestrator)

At job dispatch (`orchestrator/main.py` dispatch loop, ~L831), the
orchestrator picks a proxy by:

1. Explicit pin on the job (`config_override.proxy_id`) — used by
   "test against this exact egress" scenarios.
2. Project default (`projects.default_proxy_id`).
3. User default (`users.default_proxy_id`).
4. System pool, weighted by:
   - geo match to the target site (if known from job metadata),
   - current usage / monthly cap headroom,
   - last-known reputation score (see §4).

The selected proxy's connection details get injected into the
`JobStartRequest` payload as additional env vars
(`RESEARCH_PROXY_*` plus a new `RESEARCH_PROXY_ID` for telemetry), the
same mechanism already used for `CITATION_LLM_*`.

### 3. Agent-side wiring

`ProxyConfig.from_env()` already covers most of this. Required changes:

- Audit every browser/HTTP entry point under `src/tools/research/` and any
  future `src/tools/web/` to ensure they go through `ProxyConfig` —
  including direct Playwright launches in `browser_direct.py` and
  `papers.py`. Add a lint test that fails on unproxied
  `aiohttp.ClientSession(`/`httpx.AsyncClient(` in tool modules.
- Add structured logging on every proxied request: `proxy_id`, target
  host, response status, byte count. This feeds §4 and billing.
- `ProxyConfig` already has `to_playwright_proxy()` and
  `to_browser_use_proxy()` — extend with `to_httpx_proxy()` for the
  LLM-API client too if/when we decide to route LLM egress (see §6).

### 4. Reputation feedback loop

Per `(proxy_id, target_domain)` rolling window:

- 200/3xx counts → healthy
- 403 / 429 / Cloudflare-challenge HTML / DataDome challenge body →
  unhealthy
- Multiple consecutive unhealthy responses → temporary quarantine
  (selection policy skips it for that domain for N minutes)

A small auxiliary task in the orchestrator (or a `pg_cron` job against the
audit table) can roll up the score nightly. This is cheap; the data already
flows through the audit pipeline if we tag it.

### 5. UI / Cockpit

- Settings → Egress: list user-attached proxies, show usage / month and
  reputation. Allow add / remove / set-default.
- Project view: pick a project default proxy.
- Job view: show which `proxy_id` was used (debugging aid).

Mirrors the existing datasources UX in `cockpit/src/app/.../datasources/`.

### 6. Open questions

- **LLM API egress.** Anthropic / OpenAI don't yet IP-block, but rate
  limits are per-org-key, not per-IP, so there's no immediate reason to
  proxy that traffic. Worth revisiting if/when we host self-hosted
  inference on managed providers that do IP-rate-limit.
- **Workspace egress.** Should the workspace VM/container itself egress
  through the same pool, or just the agent's tool calls? Argument for
  workspace: shell commands run by the agent (`curl`, `pip install`,
  `npm install`) also burn the IP. Argument against: workspace egress is
  typically dependency installs, which we'd rather hit cleanly. Probably:
  agent tools through pool, workspace through clean uplink, with an
  override.
- **Tor as a fallback?** Cheap, but most reputation systems already
  deny-list Tor exits. Probably not useful as a primary, possibly useful
  as a "research the deny-list itself" tool.
- **Cost ownership in B2B.** Do we resell proxy bandwidth at markup, pass
  it through, or require enterprise tenants to BYO proxy credentials?
  Affects the schema (the `username_secret`/`password_secret` design
  already allows BYO) and the billing surface.

## Steps

1. Audit all browser / HTTP egress paths in `src/tools/` and confirm they
   route through `ProxyConfig`. Add a test that asserts no direct
   `aiohttp.ClientSession(` / `httpx.AsyncClient(` instantiation exists in
   tool modules outside `network.py`. (Smallest, ships first.)
2. Add the `proxies` table migration under
   `orchestrator/database/migrations/app/`. Frozen reference snapshot
   `schema.sql` is **not** edited — see `docs/db_migration.md`.
3. Add CRUD endpoints under `/api/proxies/` mirroring the datasources
   endpoints in `orchestrator/main.py`, with secret storage going through
   the same Vault/encrypted-blob path as datasource credentials.
4. Wire selection into the dispatch loop (`orchestrator/main.py:~831`),
   injecting `RESEARCH_PROXY_*` env vars into `JobStartRequest`.
5. Add per-request audit logging (`proxy_id`, target host, status, bytes)
   to MongoDB audit trail or a new postgres table — whichever matches the
   billing pipeline we settle on.
6. Cockpit: settings page + project default picker + per-job display.
7. Reputation rollup (cron / aux task) and quarantine logic in the
   selection policy.
8. Document the egress story in `docs/` (probably a new
   `docs/egress_proxy.md`) and link from `README.md` deployment section.

## Out of scope

- Choosing a specific proxy vendor. Bright Data, Oxylabs, Smartproxy,
  Decodo, IPRoyal — all comparable for residential pools; pick at
  procurement time. The schema treats them as opaque endpoints.
- Geo-locked browsing as a product feature (e.g. "browse this site as if
  from Japan"). Falls out for free once the schema has `geo`, but the UX
  for it is its own design.
- LLM-API egress proxying. Tracked separately if/when needed.
- Replacing the agent's primary uplink. The proxy pool is for
  reputation-sensitive third-party traffic; orchestrator ↔ cockpit ↔
  agent ↔ workspace ↔ DB stays on the platform's own network.
