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
**Date**: 2026-08-11
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

## Vendor landscape (surveyed 2026-08-11)

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

**Out of scope**

- Failover / fallback ordering between search providers. The catalog gives one
  selected provider per capability. Multi-provider racing or automatic failover
  is a separate feature (see `llm_fallback_model_routing.md` for the shape it
  would take).
- Per-project or per-expert search providers. User-level override falls out of
  the catalog for free; anything narrower is not requested.
- Result quality normalisation or re-ranking across vendors. Adapters normalise
  *shape*, not *ranking*.
- Bundling SearXNG, Crawl4AI, or Firecrawl into the Helm chart. Operators point
  at their own instance. Chart-bundling is a follow-up with its own licensing
  question (see Open questions).
- Retiring `langchain_tavily`. The Tavily adapter keeps using it.

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

When neither capability resolves at all — no rows, no key, fresh install —
all four tools are absent and the research tool group is effectively empty. This
is strictly better than today's behaviour, where the tools are present and every
call returns `Error: TAVILY_API_KEY not configured`.

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
  with a NULL `api_key` resolves cleanly (the SearXNG case).
- **Seeder idempotence** — two boots with `TAVILY_API_KEY` set produce one row;
  an admin-deleted row is not recreated.
- **Bounding preserved** — `MAX_RAW_CONTENT_WORDS`, `MAX_TOTAL_INLINE_CHARS`,
  and workspace archiving behave identically across adapters. These were added
  deliberately (`docs/done/web_search_full_page_content_bloats_session_context.md`)
  and must not regress through the refactor.
- **Live gate** — on dev: one job with SearXNG selected and no Tavily key
  present anywhere, confirming a research job completes end to end. This is the
  claim the feature exists to make, so it does not ship unverified.

## Open questions

1. **Crawl4AI in-process?** It is a Python library, not just a hosted service,
   and the agent image likely already carries Playwright for `browser_direct`.
   An in-process `crawl4ai` adapter would give a fully keyless on-prem stack
   (SearXNG + Crawl4AI) with no second service to deploy. Needs verification of
   image contents and dependency weight before committing.
2. **AGPL and Firecrawl.** Calling a self-hosted Firecrawl over HTTP is
   ordinary use and raises nothing. Shipping it *in* the chart is a
   distribution question worth a look given SRW is FSL-1.1-ALv2. Currently out
   of scope, but the answer determines whether chart-bundling is ever on the
   table.
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
