---
tags:
  - feature
  - agent
  - tools
  - research
  - providers
aliases:
  - pluggable search
  - search provider abstraction
  - tavily alternatives
  - search and fetch capabilities
related:
  - "[[db_backed_model_catalog]]"
  - "[[custom_llm_endpoints]]"
  - "[[db_backed_llm_config]]"
  - "[[tts_vendor_providers]]"
  - "[[advanced_websearch]]"
  - "[[browser_workspace_executor]]"
---

# Pluggable Search & Fetch Providers

Tavily is currently the only way the agent can see the web. This makes it a
catalog-resolved choice like every other external vendor capability — an admin
picks a search provider and a fetch provider in Admin → Models, and the agent's
`web_search` / `extract_webpage` / `crawl_website` / `map_website` tools route
through whichever adapter that selects.

The immediate payoff is that a self-hosted install stops requiring a Tavily
account to have a working research agent: SearXNG needs a base URL and no key.
The structural payoff is that search stops being the one vendor dependency with
no seam.

**Status**: PROPOSED
**Date**: 2026-08-16
**Builds on**: `db_backed_model_catalog.md` (the `models` table), `custom_llm_endpoints.md` (the `llm_endpoints` transport rows), `tts_vendor_providers.md` (the precedent for a non-OpenAI-compatible vendor riding the catalog)

## Problem

### One vendor, no seam

Every other external capability in SRW is swappable. Chat, auxiliary, embedding,
vision, whisper, and TTS all resolve through the `models` catalog: pick a row,
get a transport, call it. Search does not. `src/tools/research/web.py` hardcodes
`langchain_tavily` in four places and reads `os.getenv("TAVILY_API_KEY")`
directly. There is no configuration that changes this short of editing the image.

### The on-prem signup gate

An operator installing SRW on their own hardware must go create a Tavily account
before the research tools do anything. For a Fair Source product whose pitch
includes "run it yourself," a mandatory third-party signup for a core capability
is a real adoption cost — and it is the only remaining one of its kind, since
LLM providers can already be pointed at a local vLLM or Ollama through endpoint
rows.

### In no-workspace mode Tavily is the only web path

The signup gate is sharpest in the `virtual` and `none` workspace tiers
(`no_workspace_agent_mode.md`). There, `browser_direct` is dropped on the
`supports_shell` gate, and the in-pod browser fallback was deliberately removed
2026-06-11 (`docs/done/remove_local_browser_fallback.md`). The four Tavily
tools are the **only** way such an agent can see the web — there is no degraded
path behind them.

That tier is aimed at exactly the workloads most likely to be self-hosted:
"RAG chatbots over a customer database, research/summarize agents, light file
management." An on-prem operator running it without a Tavily account has an
agent with zero web access and no alternative to configure.

### The market moved under us

Three adverse vendor events in this space in the six months to 2026-08:

- **Tavily was acquired by Nebius (Feb 2026)** — $275M, up to $400M with
  milestones. API terms and zero-data-retention commitments unchanged so far,
  published credit pricing stable through May.
- **Brave killed its free Search API tier (Feb 2026)** — the zero-cost plan that
  had existed since 2023 was replaced with metered billing at $5/1k; developers
  with saved cards were moved onto it.
- **Exa repriced (Mar 2026)** — search $5 → $7 per 1k, plus a new $12/1k
  agentic tier.

None of this is a complaint about Tavily's product, which is good. It is an
observation that this vendor category is consolidating and repricing fast, and
that we currently have no response to any of it.

### Current state is simpler than the docs claim

Worth recording, because it makes the migration cheaper than it looks:

- **There is exactly one Tavily consumer, and it is agent-side.**
  `src/tools/research/web.py` — the four tools, all reading `TAVILY_API_KEY`
  from the shared Secret via `envFrom` in `agent_provisioner.py`.
- **The orchestrator no longer consumes Tavily at all.**
  `docs/api_key_resolution.md` describes a second in-process consumer at
  `orchestrator/services/builder_search.py:tavily_search`. That file does not
  exist, and `grep -rn "web_search\|builder_search" orchestrator/` returns
  nothing.
- **The chart still mounts the key into the orchestrator for that dead
  consumer.** `helm/templates/orchestrator/deployment.yaml:654-664` carries a
  `secretKeyRef` and a comment explaining that "the builder's web_search tool
  (builder_search.py) runs in-process here."

So the "two delivery paths" footgun documented in `api_key_resolution.md` is
already gone in code; only the config and the docs still carry it. This feature
should clean both up rather than preserve them.

## Vendor landscape (surveyed 2026-08)

| Vendor | search | extract | crawl | map | Free tier | Self-host |
|---|:--:|:--:|:--:|:--:|---|---|
| **Tavily** | ✓ | ✓ | ✓ | ✓ | 1k credits/mo | ✗ |
| **Firecrawl** | ✓ | ✓ | ✓ | ✓ | 1k credits/mo | ✓ (AGPL) |
| Exa | ✓ | ✓ | ✗ | ✗ | $10 credit | ✗ |
| Linkup | ✓ | ✓ | ✗ | ✗ | $20/mo recurring | ✗ |
| Brave Search API | ✓ | ✗ | ✗ | ✗ | $5/mo credit | ✗ |
| Perplexity / You.com | ✓ | ✗ | ✗ | ✗ | — | ✗ |
| Serper | ✓ | ✗ | ✗ | ✗ | 2.5k searches | ✗ |
| SerpAPI | ✓ | ✗ | ✗ | ✗ | 250/mo | ✗ |
| **SearXNG** | ✓ | ✗ | ✗ | ✗ | unlimited | ✓ **no key** |
| **Crawl4AI** | ✗ | ✓ | ✓ | ✗ | unlimited | ✓ |
| **Jina Reader** | ✗ | ✓ | ✗ | ✗ | free tier | ✓ |

Indicative unit costs: Tavily $0.008/credit PAYG (basic search = 1 credit,
advanced = 2, 5 extractions = 1); Serper $1/1k falling to $0.30/1k at volume;
Brave and Linkup $5/1k; Exa $7/1k search plus $1/1k content pages. Linkup
carries SOC 2 Type II and zero data retention on every plan including the free
tier, which matters for pilots with compliance questions.

### The structural conclusion

**Search is as commoditized as embeddings. Crawl and map are not.**

Eleven vendors will take a query string and return ranked results — a uniform
contract, exactly like text-in/vector-out. Only two vendors cover all four
Tavily endpoints. Extract has a middle-sized pool; crawl has three viable
options; map has essentially two.

So the right seam is **not** one Tavily-shaped provider slot. It is two
capabilities with very different vendor pools, and pretending otherwise is what
would make the abstraction leak.

## Relationship to the rest of the research surface

The research tool group is four subsystems, not one. Only the first belongs in
the catalog, and the reasons the others don't are worth recording so this isn't
re-litigated.

| Group | Tools | Backends | Disposition |
|---|---|---|---|
| **Web** (`web.py`) | `web_search`, `extract_webpage`, `crawl_website`, `map_website` | Tavily only | **This feature.** Keyed, single vendor, substitutable |
| **Papers** (`papers.py`) | `search_papers`, `download_paper`, `get_paper_info` | arXiv, Semantic Scholar, Unpaywall | Untouched — already multi-backend, free, keyless |
| **Workflow** (`workflow.py`) | `research_topic` | fans out arXiv + Semantic Scholar, dedupes, ranks | Untouched — rides the papers backends |
| **Browser** (`browser_direct.py`) | 9 × `browser_*` | workspace-side browser-exec daemon | Untouched — no vendor to swap |

### Why papers keeps a different idiom

`search_papers(source="arxiv" | "semantic_scholar")` already selects a backend
**per call**, `get_paper_info` falls back to arXiv when Semantic Scholar fails,
and `research_topic` fans out across both. This feature introduces **per-config**
selection instead. Two idioms in one tool group looks like drift unless the
distinction is deliberate — it is:

- **Paper databases are complementary corpora.** arXiv holds preprints;
  Semantic Scholar holds the citation graph. Which one serves a given question
  is a property of *the question*, so the agent chooses per call.
- **Web search vendors are substitutable commodities.** Which one a deployment
  uses is a property of *the deployment* — cost, compliance, whether a key
  exists at all — so the admin chooses once, in config.

Applying per-config selection to papers would remove a capability the agent
uses well and buy nothing: all three paper backends are free and keyless, so
none of this feature's three drivers (signup gate, vendor consolidation, cost)
apply to them.

### The browser is not part of this

`browser_direct` can read a page, so it is tempting to wire it in as a fetch
backend or a fallback. This feature deliberately does neither.

It is the wrong shape for the §4 constraint — it interprets
attacker-influenceable content by design, which is exactly why it is confined
to the workspace pod — and it is absent in the tiers where a missing search
provider hurts most. More simply: this feature is about **diversifying the
vendor space for the web tools**, not about finding substitutes for them.

Models already do the sensible thing unprompted: when `web_search` returns
errors and a browser is available, they reach for it. That emergent behaviour is
fine and needs no architecture. Designing a formal fallback chain would add
config surface and a second code path to maintain in exchange for something that
already happens.

## Scope

**In scope**

- Two new `models.capabilities` values: `search` and `fetch`.
- An agent-side adapter layer under `src/tools/research/search/` with a
  capability-declaring interface.
- Slice-1 adapters: `tavily` (search + fetch, preserving current behaviour),
  `searxng` (search), `brave` (search), `firecrawl` (search + fetch).
- Dispatch-time resolution and injection of the selected providers into the
  per-job/per-session config override, mirroring `_inject_model_credentials`.
- Graceful degradation: tools whose op is unsupported by the resolved provider
  are not constructed, so they never reach the model's tool menu.
- A boot seeder that converts an existing `TAVILY_API_KEY` into catalog rows, so
  upgrades are behaviour-identical with no operator action.
- Removal of the dead orchestrator `TAVILY_API_KEY` mount and correction of
  `docs/api_key_resolution.md`.
- **Error classification at the adapter boundary** — typed provider failures
  instead of today's silent flattening into "no results". Retires
  `web_search_masks_tavily_errors_as_no_results.md`. Prerequisite for the next
  item; see §7.
- **A second capability slot, `default_search_fallback_model`**, and single-hop
  failover to it on hard provider errors. See §8.
- **Helm chart components** for the self-hostable providers, both optional:
  SearXNG for `search` **deployed by default**, Crawl4AI for `fetch`
  default-off. Ships with the egress NetworkPolicy they require. See
  "Self-hosted deployments" below.

**Out of scope**

- **Multi-hop failover chains, admin-ordered provider lists, or racing.**
  Failover here is exactly one hop, to exactly one well-known target (the
  bundled SearXNG). An admin who wants "Tavily, then Brave, then Exa" is asking
  for `llm_fallback_model_routing.md`'s shape, which is a separate feature.
- Failover on the `fetch` capability. Fetch failures are less likely to be
  quota-driven and the bundled fetch component is default-off, so there is
  usually nothing to fall back to. Add it later if the need shows up.
- Per-project or per-expert search providers. User-level override falls out of
  the catalog for free; anything narrower is not requested.
- Result quality normalisation or re-ranking across vendors. Adapters normalise
  *shape*, not *ranking*.
- Bundling Firecrawl into the chart. It is AGPL-3.0 and Crawl4AI covers the
  same role under Apache-2.0. Firecrawl stays a supported *adapter*; operators
  who want it point at their own instance or the hosted service.
- Writing our own fetch service. Crawl4AI already is one (see below).
- Retiring `langchain_tavily`. The Tavily adapter keeps using it.
- Any change to `browser_direct`, `papers.py`, or `research_topic`. The browser
  stays the workspace-side tool for interactive pages; the paper backends stay
  agent-selected per call. Neither is replaced, wrapped, or re-homed. No
  fallback wiring between them and the web tools.
- Any in-pod fetching. See the off-pod constraint in §4 — this is a security
  boundary, not a preference.

## Self-hosted deployments

The goal is a diverse vendor space, not the elimination of paid vendors. Where a
free or self-hostable option exists we should ship the ability to use it; where
none exists, requiring a provider signup is an acceptable outcome — web search
costs are trivial next to model spend.

Both capabilities happen to have a self-hostable answer, and neither needs
building:

| Role | Component | License | Footprint | Notes |
|---|---|---|---|---|
| `search` | **SearXNG** | AGPL-3.0 | ~300 MB image, 512 MB RAM min (2 GB comfortable) | Metasearch over 70+ engines. `?format=json`. No key, no quota. |
| `fetch` | **Crawl4AI** (Docker server) | **Apache-2.0** | ~4 GB RAM recommended; permanent browser ~270 MB + ~180 MB per pool browser | FastAPI server with `/crawl`, `/crawl/stream`, `/html`, `/crawl/job` (async + webhooks), `/monitor`. JWT auth on by default. |

### Deployment defaults differ by footprint

Both are optional chart subcomponents, but they do not get the same default:

- **SearXNG — deployed by default** (`searxng.enabled: true`). At ~300 MB and
  512 MB RAM it is small enough to carry unconditionally, and it is what makes a
  fresh install have working search with no signup.
- **Crawl4AI — default-off** (`crawl4ai.enabled: false`). At ~4 GB RAM it is too
  heavy to impose on every install. Operators who want keyless fetch opt in.

The asymmetry is purely about footprint. If the 4 GB figure comes down (open
question 1), Crawl4AI's default is worth revisiting.

### Two slots, one resolver

Search resolves through **two** capability defaults rather than one:

| Slot | Setting | Meaning |
|---|---|---|
| primary | `default_search_model` | Where every search goes first |
| fallback | `default_search_fallback_model` | Retried once when the primary raises a hard error (§8) |

Both are ordinary catalog rows chosen in Admin → Models and resolved by the same
`resolve_default_for_capability` chain. **The fallback is not special-cased to
SearXNG**, or to self-hosted providers, or to anything else — it is whichever row
the admin selected. Brave, a second Tavily key on a different plan, someone's own
Firecrawl: all equally valid.

There is therefore no "is the bundled SearXNG deployed?" check anywhere in the
runtime. "Is there a fallback?" is "is the fallback slot set?" — the same shape
as "is there a primary?", answered by the same resolver.

What the chart contributes is a good default, not a mechanism. When
`searxng.enabled: true`, the boot seeder creates the endpoint + catalog rows
pointing at the in-cluster Service and fills whichever slots are empty. Both
writes are insert-only, so an admin who has chosen otherwise is never clobbered.

**Seed ordering matters.** The Tavily seeder must run before the SearXNG seeder,
or a Tavily-configured upgrade would find its primary slot already taken by
SearXNG. The two are otherwise independent, and neither reasons about the other
— see the self-fallback guard in §8, which is what lets both seeders stay dumb.

Resulting out-of-the-box behaviour:

| Install | Primary | Fallback |
|---|---|---|
| fresh, nothing configured | bundled SearXNG | none — it is already primary |
| upgrade with an existing `TAVILY_API_KEY` | Tavily | bundled SearXNG |
| admin picked Brave, SearXNG deployed | Brave | bundled SearXNG, until they change it |
| `searxng.enabled: false` | whatever they picked | **none** |

That last row is the point of keeping the deployment optional: on a large
install whose upstream provider is already HA, a fallback pod idle 99.99% of the
time is dead weight. Turn it off, nothing is seeded, nothing is selected, and
there is no fallback. An admin who wants the pod but not the failover just
clears the slot in the UI.

**Capability gap, accepted.** Neither component does sitemap-style URL
discovery, so `map_website` is unavailable in the fully self-hosted
configuration. It is the rarest of the four tools and degrades cleanly per §6.
Not worth building around.

**Crawl4AI's JWT fits the existing row shape.** Its server binds to loopback
unless a token is set; that token goes in the `llm_endpoints.api_key` column
like any other credential, so the "self-hosted" case needs no special handling.

### Egress policy is required, not optional

Bringing the fetcher in-cluster is **not** security-neutral. Today the SSRF
surface is Tavily's problem — the page fetch happens on their infrastructure.
A self-hosted fetcher moves that surface inside our cluster. Principle 1 still
holds (it is not the *agent* pod, and the agent still talks to a fixed
destination), but the fetch pod now takes model-influenced URLs and makes
arbitrary outbound requests from a position of network trust.

So the chart components ship with an egress NetworkPolicy that denies:

- RFC1918 ranges and the cluster service CIDR — no reaching Postgres, NATS,
  the orchestrator, or any other in-cluster service
- `169.254.169.254` and link-local — no cloud instance metadata
- localhost / loopback beyond the pod itself

This lands **with** the components, not after them. Shipping an unrestricted
in-cluster fetcher would trade a vendor dependency for a lateral-movement path,
which is a bad trade at any price. Related: `agent_egress_networkpolicy_enablement.md`.

## Design

### 1. Two catalog capabilities

Extend the `models.capabilities` CHECK constraint
(`orchestrator/database/schema.sql:323-326`):

```sql
capabilities <@ ARRAY[
  'chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts',
  'search', 'fetch'
]::TEXT[]
```

`search` means "can answer a query with ranked web results."
`fetch` means "can retrieve page content" and covers extract, crawl, and map —
which of those three a given row actually supports is declared per-row, not per
capability.

A row may carry both (Tavily, Firecrawl do). A row may carry one (SearXNG is
`search` only; Jina would be `fetch` only).

> **Note on dead columns.** `context_window`, `reasoning_level`, and `family`
> are meaningless for a search provider. They are equally meaningless for the
> `tts` and `whisper` rows already in the table; this follows that precedent
> rather than introducing it. If the dead-column count ever justifies a split,
> it should be a separate refactor covering all non-chat capabilities at once.

### 2. Row shape

A provider is an `llm_endpoints` transport row plus a `models` catalog row,
identical in structure to how a vLLM model or the codex proxy is expressed:

```
llm_endpoints:
  label     = 'searxng-local'
  base_url  = 'https://searxng.internal:8080'
  api_key   = NULL                     -- SearXNG needs none

models:
  provider_kind = 'endpoint'
  provider_ref  = <that endpoint uuid>
  model_id      = 'searxng'
  display_label = 'SearXNG (self-hosted)'
  capabilities  = ARRAY['search']
  params_json   = { "provider": "searxng", "ops": ["search"] }
```

`params_json.provider` is the adapter selector — the same mechanism
`tts_vendor_providers.md` specifies for ElevenLabs, which is likewise not
OpenAI-compatible. `params_json.ops` declares the supported operations and is
what drives tool degradation in §6.

The nullable `api_key` on an endpoint row is what makes SearXNG fit without any
schema change: it is a base URL and nothing else.

For a full-surface vendor:

```
model_id      = 'tavily'
capabilities  = ARRAY['search', 'fetch']
params_json   = { "provider": "tavily",
                  "ops": ["search", "extract", "crawl", "map"] }
```

### 3. Resolution and delivery

Resolution reuses the existing capability chain verbatim:

1. `user_settings.default_search_model` / `default_fetch_model`
2. `postgres_db.resolve_default_for_capability('search' | 'fetch')`
3. endpoint row → `base_url` + `api_key`

The fallback slot resolves through the identical chain, keyed
`default_search_fallback_model`. It is the same lookup against the same table —
a second slot, not a second mechanism.

Delivery uses the config-override section, **not** pod env.
`_inject_model_credentials` (`orchestrator/main.py:6906`) writes into a section
dict on the per-job / per-session config payload, which is per-dispatch and
therefore respects per-user selection. Pod env is set once at provision time and
cannot. A sibling `_inject_search_credentials` writes:

```python
config["research"]["search"] = {
    "provider": "searxng",
    "base_url": "https://searxng.internal:8080",
    "api_key":  None,
    "ops":      ["search"],
}
config["research"]["fetch"] = { ... }

# Present only when the fallback slot is set and resolves to a different
# row than the primary. Absent otherwise — there is no "enabled" flag.
config["research"]["search_fallback"] = { ... }
```

The agent stops reading `TAVILY_API_KEY` from env entirely.

> **Refactor to fold in.** `resolve_capability_credentials`
> (`orchestrator/services/capability_credentials.py:23`) returns a bare
> `(model, base_url, api_key)` triple and does not surface `params_json`. TTS
> already has to go re-read `params_json.provider` separately to pick its
> backend; search and fetch would make that three consumers duplicating the same
> lookup. Extend the helper once to return a small dataclass carrying
> `provider` and `params` alongside the transport, and migrate the TTS and
> whisper call sites to it. This is in scope — it is the seam this feature sits
> on, and leaving it a triple means writing the duplication a third time.

### 4. Adapter interface

New package `src/tools/research/search/`:

```python
class SearchAdapter(Protocol):
    ops: frozenset[str]          # subset of {search, extract, crawl, map}

    def search(self, query: str, max_results: int, **kw) -> list[Result]: ...
    def extract(self, urls: list[str], **kw) -> list[Page]: ...
    def crawl(self, url: str, **kw) -> list[Page]: ...
    def map(self, url: str, **kw) -> list[str]: ...
```

Adapters normalise to two internal shapes — `Result` (title, url, snippet,
raw_content?) and `Page` (url, content, failed?) — which are what the existing
citation registration (`context.get_or_register_web_source()`), workspace
archiving, and the `MAX_RAW_CONTENT_WORDS` / `MAX_TOTAL_INLINE_CHARS` bounding
in `web.py` already consume. Those bounding and archiving paths are
vendor-agnostic and do not change.

Vendor-specific parameters that have no cross-vendor meaning (Tavily's
`search_depth`, `topic`, `chunks_per_source`) are passed through `**kw` and
ignored by adapters that do not understand them, rather than being promoted into
the interface.

#### Hard constraint: adapters fetch off-pod

**Every adapter must be a typed client pointed at a fixed, admin-configured
destination. The page fetch happens on the provider's infrastructure, never in
the agent pod.** This is not a preference — it is the security boundary
`no_workspace_agent_mode.md` design principle 1 establishes:

> The agent pod stays a control plane. Typed, fixed-destination clients in-pod
> are fine (S3/rclone, SQL/graph/Mongo, Tavily, LLM endpoints). Anything that
> executes code or *interprets attacker-influenceable content* (shell, browser,
> python) does not run in the agent pod: the pod holds internal credentials
> (`config_override.env_keys`) and currently has **no NetworkPolicy**.

The existing tools satisfy this because, as §7 of that document puts it, "the
page fetch happens on Tavily's infrastructure, so these are not an in-pod SSRF
surface." Any adapter that fetches arbitrary URLs from inside the agent pod
would turn a swap-the-vendor feature into a threat-model change.

This is therefore a **vendor selection criterion**, not just an implementation
note:

| Shape | Verdict |
|---|---|
| Hosted API (Tavily, Brave, Exa, Firecrawl cloud, Linkup) | ✓ fixed destination |
| Self-hosted service at an admin-set `base_url` (SearXNG, Firecrawl, Crawl4AI-as-a-service) | ✓ fixed destination; it fetches on its own host |
| In-process library that fetches URLs (Crawl4AI as a Python import, raw `httpx` to model-supplied URLs) | ✗ disqualified — in-pod SSRF surface |

The admin-configured `base_url` is what makes the self-hosted row safe: the
destination is fixed by configuration, never derived from model output.

**Slice-1 adapters**

| Adapter | ops | Why it's in slice 1 |
|---|---|---|
| `tavily` | search, extract, crawl, map | Preserves today's behaviour exactly; the migration target |
| `searxng` | search | Kills the on-prem signup gate; no key |
| `brave` | search | Independent index, not a Google reseller — real diversification |
| `firecrawl` | search, extract, crawl, map | The only other full-surface vendor; the true Tavily hedge |

`serper` is a trivial fifth if the cheap floor ($0.30–1/1k) is wanted.

### 5. Tool construction

`create_web_tools(context)` (`src/tools/research/web.py:160`) already builds
each tool as a closure and returns a list. Adapter selection happens once at
tool-creation time from `context`, and the four tool bodies lose their
`_get_tavily_api_key()` / `from langchain_tavily import ...` blocks in favour of
adapter calls.

Tool docstrings and `RESEARCH_TOOLS_METADATA` descriptions currently name Tavily
("Search the web for information using Tavily"). These become vendor-neutral.
Per the capability-surface cost rule, replacements must be the **same length or
shorter** — these strings are in every agent's tool menu on every turn.

### 6. Degradation

If the resolved `fetch` provider does not declare an op, that tool is simply not
appended to the returned list. It never enters the model's tool menu, so the
model cannot call a tool that would only return "not configured."

`RESEARCH_TOOLS_METADATA` stays static and complete. This matters: config files
naming `crawl_website` must still validate against
`tests/test_config_tool_names_are_registered.py`, where an unknown tool name
fails the entire batch tool load. Registration (the name is known) and
construction (an instance exists) are deliberately separate, and a test must pin
that distinction.

When nothing resolves at all — no provider selected and no bundled SearXNG —
the tools are absent. This is strictly better than today's behaviour, where they
are present and every call returns `Error: TAVILY_API_KEY not configured`,
burning a turn to learn nothing.

Note the distinction from §8: **failover handles a configured provider that
fails; degradation handles a provider that was never configured.** Nothing falls
back to the browser. In workspace-backed tiers a model may reach for
`browser_direct` on its own when the web tools error, which is fine — but that
is emergent, not a routing rule, and nothing here detects, encourages, or
depends on it.

The paper tools (`search_papers`, `research_topic`) are unaffected in every case
— they carry their own free backends and never depended on this capability.

### 7. Error classification

Today every provider failure is flattened into a benign empty result.
`_direct_web_search` checks only `response.get("results")` and never
`response.get("error")`, so `langchain_tavily` returning
`{"error": "Error 432: … usage limit …"}` reaches the model as:

```
No web results found for: <query>
```

This is a filed, still-open defect
(`web_search_masks_tavily_errors_as_no_results.md`) found live: an agent burned
roughly 30 LLM calls over five minutes retrying query variants against a spent
key. It is also a **hard prerequisite for §8** — you cannot fail over on an
error signal that does not exist, and the flagship failover case (an exhausted
quota) is precisely the one currently rendered invisible.

The adapter boundary is where this gets fixed, because it is the one place that
knows what a given vendor's failure looks like. Every adapter raises a typed
error rather than returning a sentinel string:

| Class | Triggers | Failover? |
|---|---|---|
| `ProviderAuthError` | 401, 403, malformed key | **yes** |
| `ProviderQuotaError` | 402, 432, plan exhausted | **yes** |
| `ProviderRateLimitError` | 429 | **yes** |
| `ProviderUnavailableError` | 5xx, connection refused, timeout | **yes** |
| `ProviderRequestError` | 400 and other client errors that are *our* bug | no — surface it |
| *(no error)* | zero results for a valid query | no — a legitimate answer |

The last row is the important one. **An empty result is an answer, not a
failure.** Failing over on it would double the latency and cost of every
genuinely-zero-hit search while changing nothing about the outcome.

Fixing this is worthwhile on its own merits even if §8 were dropped: the agent
learns "the tool is broken" instead of "the web is empty", and an operator gets
a real signal that a key is spent.

### 8. Single-hop failover

When the primary `search` provider raises one of the failover-eligible errors
above, the call is retried **once** against the provider in the fallback slot —
if that slot is set and resolves to a **different row** than the primary. That
is the whole mechanism.

The different-row guard is what lets the seeders stay dumb: they can fill both
slots without reasoning about whether that is sensible, and a provider that
would be its own fallback simply has none. It also covers the case of an admin
picking the same row twice by hand.

Delivery is already handled — the orchestrator injects
`config["research"]["search_fallback"]` only when the guard passes (§3), so the
agent's rule is simply "retry against the fallback config if it is present."
There is no deployment check, no enabled flag, and no chart coupling.

Deliberate limits:

- **One hop.** No chains, no ordered lists, no racing. If the fallback also
  fails, the typed error from the *primary* is what surfaces — that is the one
  the operator needs to act on.
- **`search` only.** Not `fetch`; see Scope.
- **One fallback, not an ordered list.** Admin-ordered multi-provider routing is
  `llm_fallback_model_routing.md`'s shape and stays out of scope.

**The fallback must not be silent.** If it were, we would have replaced "silent
empty results" with "silent degraded results" — the same class of bug this
feature is fixing, one layer up. So a fallback hop:

- annotates the tool result so the model knows which provider answered
- logs at WARN with the primary's typed error, so a spent key is visible to an
  operator without reading agent transcripts

A quota-exhausted Tavily key that silently rides SearXNG forever is a worse
outcome than a loud failure, because nobody ever fixes it.

## Migration and back-compat

**Seeder.** A boot step mirroring `orchestrator/init.py:_seed_codex_proxy_endpoint`:
if `TAVILY_API_KEY` is set and no `search`-capable row exists, create the Tavily
endpoint row plus a `['search','fetch']` catalog row and set both capability
defaults to it. Idempotent, insert-only, never clobbers admin edits, and an
admin deleting the row is not undone on next boot.

Consequence: an existing install upgrades to byte-identical behaviour with no
operator action, and `TAVILY_API_KEY` demotes to a seed-only variable like the
`SEED_*` family.

**Cleanup this unblocks.** All three are dead or wrong today and should land
with this work:

- Remove the `TAVILY_API_KEY` `secretKeyRef` and its comment from
  `helm/templates/orchestrator/deployment.yaml:654-664` — the consumer it
  describes does not exist.
- Correct the Tavily section of `docs/api_key_resolution.md`, which documents
  two runtime consumers and a `builder_search.py` that is gone.
- Update `docs/advanced_websearch.md`, whose "Current Implementation Deep Dive"
  describes the pre-bounding Tavily call shape.

**Test migration cost, stated honestly.** `tests/tools/research/test_web_tools.py`
sets `TAVILY_API_KEY` via `monkeypatch.setenv` and injects a mocked
`langchain_tavily` module at roughly a hundred call sites. Those fixtures must
be reshaped to inject adapter config through `ToolContext` instead. This is
mechanical but it is the single largest chunk of work in the feature, and it
should be budgeted as such rather than discovered.

## Testing

- **Adapter conformance** — one shared suite parametrised over every adapter,
  asserting each declared op returns the normalised shape and that undeclared
  ops raise rather than return empty.
- **Degradation** — a `search`-only provider yields exactly one tool from
  `create_web_tools`; `crawl_website` stays present in
  `RESEARCH_TOOLS_METADATA` and configs naming it still pass
  `test_config_tool_names_are_registered.py`.
- **Resolution** — user default beats system default beats none; an endpoint row
  with a NULL `api_key` resolves cleanly (the SearXNG case); the fallback slot
  resolves through the same chain independently of the primary.
- **Error classification** — each failover-eligible class is raised as its typed
  error and, critically, a zero-result response is **not**. The regression test
  for `web_search_masks_tavily_errors_as_no_results.md` belongs here: a Tavily
  432 must surface as a quota error, never as "No web results found."
- **Failover** — a primary raising each eligible class hits the fallback exactly
  once; a `ProviderRequestError` and an empty result each hit it zero times;
  fallback identical to primary is a no-op; a failing fallback surfaces the
  *primary's* error. The fallback hop annotates the result and logs at WARN.
- **Seeder idempotence and ordering** — two boots produce one row; an
  admin-deleted row is not recreated; with `TAVILY_API_KEY` set, Tavily lands in
  the primary slot and the bundled SearXNG in the fallback slot, never the
  reverse.
- **Bounding preserved** — `MAX_RAW_CONTENT_WORDS`, `MAX_TOTAL_INLINE_CHARS`,
  and workspace archiving behave identically across adapters. These were added
  deliberately (`docs/done/web_search_full_page_content_bloats_session_context.md`)
  and must not regress through the refactor.
- **Non-regression of the untouched groups** — `search_papers`,
  `research_topic`, and the `browser_*` tools resolve and behave identically
  regardless of search/fetch configuration, including when neither capability
  resolves. These share a tool group with the changed code and must be pinned
  against collateral damage.
- **Off-pod constraint** — each adapter is asserted to issue requests only to
  its configured `base_url` host. A model-supplied URL must never become a
  request origin from inside the agent pod. This encodes the §4 boundary as a
  test rather than a review convention.
- **Chart components** — `helm template` renders each subcomponent per its
  `enabled` flag (SearXNG default true, Crawl4AI default false), and the egress
  NetworkPolicy is asserted against a real API server
  (`apply --dry-run=server`), not a mocked client. Then a live check that the
  fetch pod genuinely cannot reach Postgres or `169.254.169.254`. A policy that
  renders but does not block is worse than none, because it reads as covered.
- **Live gate** — on dev, on a **`virtual` (no-workspace) job**: SearXNG
  selected, no Tavily key present anywhere, confirming a research job completes
  end to end. The tier matters — it is the one with no browser and no fallback,
  so it is the only place the claim is unambiguous. This is what the feature
  exists to make true, so it does not ship unverified.

## Open questions

1. **Crawl4AI's 4 GB footprint on small installs.** It is the recommended
   `fetch` component and it is not light — a permanent browser plus pool
   browsers. For the "50 searches a day" operator this may be more pod than the
   workload justifies. Worth measuring whether a trimmed configuration (smaller
   browser pool, no monitoring dashboard) brings it into a range that suits
   single-node installs, before we recommend it as the default self-hosted
   fetcher. If it cannot be trimmed, the honest guidance is "use a hosted fetch
   vendor unless you actually crawl a lot" — which is fine.
2. **AGPL and SearXNG in the chart.** Crawl4AI is Apache-2.0 so the `fetch`
   component raises nothing. SearXNG is AGPL-3.0. Referencing an unmodified
   upstream image is ordinary practice and the source is already public, so the
   risk reads as low — but SearXNG now ships **enabled by default**, which makes
   it worth one deliberate look given SRW is FSL-1.1-ALv2, rather than an
   assumption. If it ever became a concern, the graceful retreat is to flip the
   default to off and document the deployment instead of shipping it.
3. **Does `fetch` want splitting further?** Jina Reader does extract but not
   crawl. Under this design, selecting a fetch provider that only extracts means
   no crawl at all. That is probably fine — crawl and map are rare compared to
   extract — but if it bites, the fix is per-op resolution rather than
   per-capability, which is a larger config surface.
4. **Usage metering.** Search credits are a real cost and
   `usage_events` currently has no notion of them. Out of scope here, but worth
   knowing that catalog-ising search is the prerequisite that makes metering it
   possible later.

## References

- [Tavily alternatives compared — cost, search & extract](https://codenote.net/en/posts/tavily-alternatives-cost-comparison-search-extract-api/)
- [Tavily pricing 2026: credits, plans, real costs](https://coldiq.com/blog/tavily-pricing)
- [Tavily (acquired by Nebius, Feb 2026)](https://nolist.ai/item/tavily)
- [Brave drops free Search API tier](https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/)
- [Exa pricing reference](https://exa.ai/docs/reference/pricing)
- [Linkup pricing in 2026](https://coldiq.com/blog/linkup-pricing)
- [Firecrawl guide: self-hosting, pricing, limits](https://webscraping.ai/blog/firecrawl-guide)
- [SearXNG search API documentation](https://docs.searxng.org/dev/search_api.html)
- [Open source Tavily alternatives](https://openalternative.co/alternatives/tavily)
