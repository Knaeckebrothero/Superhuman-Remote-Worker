# Advanced Web Search & Deep Research

Research into how major AI providers (OpenAI, Google, Anthropic, Perplexity) implement deep research capabilities, and what techniques we can adopt to improve our agent's web search.

## Current State

Our agent currently has these web-facing tools:

| Tool | Method | Limitation |
|------|--------|------------|
| `web_search` | Tavily single-shot API call | One query, flat result list |
| `browse_website` | Browser automation (browser-use) | Slow, heavyweight |
| `download_from_website` | Browser automation | File downloads only |
| `research_topic` | Multi-database paper search + download | Academic papers only |

The main gap: for general web research, the agent fires a single Tavily search and works with whatever comes back. No query refinement, no content extraction, no result reranking, no iterative deepening.

---

## Current Implementation Deep Dive

### What `web_search` Actually Does

Source: `src/tools/research/web.py`

The entire Tavily interaction is:

```python
from langchain_tavily import TavilySearch
search = TavilySearch(api_key=api_key, max_results=max_results)
response = search.invoke({"query": query})
```

That's it. We pass `query` and `max_results` (default 5) through the LangChain wrapper, which defaults to `search_depth="basic"`. The agent receives back:

- Title + URL per result
- A **300-character truncated snippet** of content (`r.get('content', '')[:300]`)
- Source IDs for citation (auto-registered via `context.get_or_register_web_source()`)

The tool also handles inaccessible sources (HTTP 403 etc.) by marking them and suggesting the browser tool as fallback.

### What We're Not Using from Tavily

Tavily offers four distinct API endpoints. We use one parameter from one endpoint.

### Tavily Search API — Full Parameter Reference

**What we use:**

| Parameter | Our Value | Notes |
|-----------|-----------|-------|
| `query` | Agent's search string | Required |
| `max_results` | 5 (default) | Agent can override |

**What we ignore:**

| Parameter | Type | Default | What It Does |
|-----------|------|---------|-------------|
| `search_depth` | string | `"basic"` | `"advanced"` = better relevance (2 credits vs 1) |
| `include_raw_content` | bool/string | `false` | `"markdown"` or `"text"` = full page content instead of snippets |
| `include_answer` | bool/string | `false` | `"basic"` or `"advanced"` = Tavily generates an LLM summary of results |
| `topic` | string | `"general"` | `"news"` = optimized for recent news/events |
| `time_range` | string | none | Filter by recency: `day`, `week`, `month`, `year` |
| `start_date` / `end_date` | string | none | Date range filter (YYYY-MM-DD) |
| `include_domains` | array | none | Only search these domains (max 300) |
| `exclude_domains` | array | none | Never return results from these domains (max 150) |
| `chunks_per_source` | int | 3 | Relevant chunks per source (1-3) |
| `country` | string | none | Boost results from specific country |
| `include_images` | bool | `false` | Return image results |
| `auto_parameters` | bool | `false` | Let Tavily auto-configure based on query intent |

**Biggest miss**: `include_raw_content="markdown"` would give us full page content instead of the 300-char snippets we currently truncate to. This alone would be transformative.

### Tavily Extract API — Content Extraction

Endpoint we don't use at all. Pulls clean content from specific URLs.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `urls` | string/array | required | 1-20 URLs to extract content from |
| `query` | string | none | Reranks extracted chunks by relevance to this query |
| `chunks_per_source` | int | 3 | Chunks per source (1-5, max 500 chars each) |
| `extract_depth` | string | `"basic"` | `"advanced"` for JS-heavy sites (2 credits/5 URLs) |
| `format` | string | `"markdown"` | Output format: `"markdown"` or `"text"` |
| `timeout` | float | 10s/30s | Per-URL timeout (1-60 seconds) |

**Use case**: Agent finds 5 relevant URLs from search, calls extract to read all of them at once. Returns `raw_content` (full page) per URL, plus a list of `failed_results`.

**Cost**: 1 credit per 5 URLs (basic), 2 credits per 5 URLs (advanced).

### Tavily Crawl API — Site Spidering

Endpoint we don't use. Traverses a website from a base URL following links.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | Root URL to start crawling |
| `instructions` | string | none | Natural language guidance for the crawler |
| `max_depth` | int | 1 | How far from base URL to explore (1-5) |
| `max_breadth` | int | 20 | Links to follow per page level (1-500) |
| `limit` | int | 50 | Total pages to process before stopping |
| `select_paths` | array | none | Regex patterns to include specific URL paths |
| `exclude_paths` | array | none | Regex patterns to exclude specific paths |
| `select_domains` / `exclude_domains` | array | none | Domain filtering |
| `allow_external` | bool | `true` | Follow links to external domains |
| `extract_depth` | string | `"basic"` | Content extraction depth |
| `format` | string | `"markdown"` | Output format |
| `timeout` | float | 150 | Max wait time (10-150 seconds) |

**Use case**: Agent needs to explore a documentation site (e.g., GoBD guidelines, GDPR regulation pages, API docs). Start from the index page, crawl linked pages up to a depth limit, get all content as markdown.

**Cost**: 1 credit per 5 pages (basic), 2 credits per 5 pages (advanced). With `instructions`, always 2 credits per 10 pages.

### Tavily Map API — URL Discovery

Endpoint we don't use. Discovers all URLs on a site without extracting content.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | Root URL to map |
| `instructions` | string | none | Natural language guidance (doubles cost) |
| `max_depth` | int | 1 | Exploration depth (1-5) |
| `max_breadth` | int | 20 | Links per level (1-500) |
| `limit` | int | 50 | Total links before stopping |
| `select_paths` / `exclude_paths` | array | none | Path filtering |
| `select_domains` / `exclude_domains` | array | none | Domain filtering |
| `allow_external` | bool | `true` | Follow external links |
| `timeout` | float | 150 | Max wait time (10-150 seconds) |

Returns only URLs (no content). Use case: reconnaissance before deciding what to extract or crawl.

**Cost**: 1 credit per 10 pages, 2 credits per 10 pages with instructions.

### Gap Summary

| Capability | Status | Impact |
|-----------|--------|--------|
| Basic search with short snippets | Have it | Baseline |
| Full page content from search | **Missing** — just need `include_raw_content` | High |
| Advanced search depth | **Missing** — just need `search_depth="advanced"` | Medium |
| Date/time filtering | **Missing** | Medium |
| Domain filtering | **Missing** | Medium |
| News-optimized search | **Missing** | Low-Medium |
| Read specific URLs (extract) | **Missing** — no tool exists | Very High |
| Crawl a website | **Missing** — no tool exists | High |
| Map a website (URL discovery) | **Missing** — no tool exists | Medium |
| LLM-generated search answer | **Missing** | Low (agent can do this itself) |

### Proposed New Tools

Based on the gaps, these are the concrete tools to implement:

#### 1. `extract_webpage` (Tavily Extract)

The single most impactful addition. Lets the agent read any URL(s) as clean markdown.

```python
@tool
def extract_webpage(
    urls: str | list[str],
    query: str | None = None,
    extract_depth: str = "basic",
) -> str:
    """Extract clean content from one or more web pages.

    Use this to read the full content of pages found via web_search,
    or any URL you need to examine. Returns markdown content.

    Args:
        urls: URL or list of URLs to extract (max 20)
        query: Optional query to rank extracted chunks by relevance
        extract_depth: "basic" (fast) or "advanced" (handles JS-heavy sites)
    """
```

#### 2. Enhanced `web_search`

Expose the parameters Tavily already supports:

```python
@tool
def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    topic: str = "general",
    time_range: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    include_raw_content: bool = False,
) -> str:
    """Search the web using Tavily.

    Args:
        query: Search query
        max_results: Results to return (1-20, default 5)
        search_depth: "basic" (fast, 1 credit) or "advanced" (better relevance, 2 credits)
        topic: "general" or "news" (optimized for recent events)
        time_range: Filter by recency: "day", "week", "month", "year", or None
        include_domains: Only search these domains
        exclude_domains: Never return results from these domains
        include_raw_content: If true, include full page content (not just snippets)
    """
```

#### 3. `crawl_website` (Tavily Crawl)

For exploring multi-page sites like documentation, regulations, standards.

```python
@tool
def crawl_website(
    url: str,
    instructions: str | None = None,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 20,
    select_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> str:
    """Crawl a website starting from a URL, following links.

    Use this to explore documentation sites, regulatory pages, or any
    multi-page resource. Returns content from all crawled pages.

    Args:
        url: Starting URL to crawl from
        instructions: Natural language guidance (e.g., "find pages about data retention")
        max_depth: How deep to follow links (1-5, default 1)
        max_breadth: Links to follow per page (1-500, default 20)
        limit: Total pages to process (default 20, keeps cost manageable)
        select_paths: Regex patterns to include (e.g., ["/docs/.*", "/api/.*"])
        exclude_paths: Regex patterns to exclude (e.g., ["/blog/.*"])
    """
```

#### 4. `map_website` (Tavily Map)

Quick URL discovery before committing to a full crawl.

```python
@tool
def map_website(
    url: str,
    instructions: str | None = None,
    max_depth: int = 2,
    limit: int = 50,
    select_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> str:
    """Discover all URLs on a website without extracting content.

    Use this for reconnaissance: find out what pages exist on a site
    before deciding what to read. Returns a list of discovered URLs.

    Args:
        url: Starting URL to map
        instructions: Natural language guidance for URL discovery
        max_depth: Exploration depth (1-5, default 2)
        limit: Max URLs to discover (default 50)
        select_paths: Regex patterns to include
        exclude_paths: Regex patterns to exclude
    """
```

### Tool Category Update

These tools belong in the `research` category in `config/defaults.yaml`:

```yaml
tools:
  research:
    - web_search           # Search the web (Tavily Search)
    - extract_webpage      # Read webpage content (Tavily Extract)
    - crawl_website        # Spider a site (Tavily Crawl)
    - map_website          # Discover URLs on a site (Tavily Map)
    - search_papers        # Search academic papers
    - download_paper       # Download paper PDFs
    - get_paper_info       # Paper metadata
    - browse_website       # Browser automation (heavyweight fallback)
    - download_from_website
    - research_topic       # Multi-database literature search
```

---

## How Major AI Providers Do It

### The Core Pattern

Nobody just sends a single search query. All major providers use a multi-stage pipeline:

```
Plan -> Search -> Read -> Reason -> Re-search -> Synthesize
```

This is a ReAct loop (Reasoning + Acting) applied to research: the agent thinks about what to search, searches, reads results, reasons about gaps, searches again with refined queries, and eventually synthesizes a report.

### OpenAI Deep Research

- Powered by **o3-deep-research**, a specialized reasoning model
- ReAct loop: Think -> Act -> Observe -> Think again
- Processes **hundreds of sources** per query
- Combines web search, code interpreter, and vision in one agent
- Multi-stage: query clarification (GPT-4o) -> grounding via Bing -> deep analysis (o3) -> synthesis
- Takes ~5 minutes, costs ~$0.40 per deep research
- 20% fewer major errors than o1 on real-world tasks

### Google Gemini Deep Research

- Built on **Gemini 3 Pro** reasoning core
- **Planning phase**: breaks queries into sub-tasks, shows plan to user for refinement
- **Parallel execution**: determines which sub-tasks can run simultaneously
- Deep site navigation, not just surface-level search results
- State-of-the-art factuality through specialized training
- 66.1% on DeepSearchQA benchmark, 59.2% on BrowseComp

### Perplexity AI

Purpose-built as an AI-first search engine:

- **Multi-stage retrieval pipeline**:
  1. Early stages: lexical + embedding-based scoring (optimized for speed)
  2. Candidate winnowing: gradually reduce result set
  3. Final stage: cross-encoder reranker models
- **Sub-document retrieval**: surfaces precise segments, not entire pages
- RAG to reduce hallucinations
- Personalization via user memory in context window
- Two modes: Quick Search (fast) and Pro Search (deep exploration)

### Open Source: GPT-Researcher

GitHub: `assafelovic/gpt-researcher` — consistently outperforms Perplexity and OpenAI on Carnegie Mellon's DeepResearchGym benchmark.

Architecture:
- **Planner agent**: generates research questions from the query
- **Execution agents**: gather information in parallel
- **Publisher**: aggregates findings into a report

Key pattern — **tree-like exploration**:
- Breadth: generate multiple search angles at each level
- Depth: recursively dive into each branch
- Concurrent processing with smart context management
- ~5 minutes per deep research, ~$0.40 cost with o3-mini

### LangChain Open Deep Research

GitHub: `langchain-ai/open_deep_research` — three architecture options:

1. **Plan-and-Execute**: structured workflow with human-in-the-loop planning
2. **Sequential Processing**: create sections one by one with reflection after each
3. **Supervisor-Researcher**: multi-agent coordination with parallel processing

---

## Key Techniques

### 1. Query Decomposition

Break a complex research question into focused sub-queries, execute in parallel.

```
Input:  "What are the GoBD requirements for electronic invoice archival?"

Decomposed:
  1. "GoBD Grundsätze ordnungsmäßiger Buchführung elektronische Rechnungen"
  2. "GoBD Aufbewahrungsfrist elektronische Belege"
  3. "GoBD revisionssichere Archivierung Anforderungen"
  4. "BMF Schreiben 2019 elektronische Buchführung Pflichten"
```

Each sub-query targets a different facet. Results are merged and deduplicated.

**Why it matters**: A single query often misses important aspects. Decomposition gives much better recall.

### 2. Iterative / Recursive Search

Search -> read top results -> identify gaps -> generate refined queries -> search again. Repeat 2-3 rounds.

```
Round 1: "GoBD compliance requirements" -> learns key terms
Round 2: "GoBD Verfahrensdokumentation Pflichtbestandteile" -> deeper specifics
Round 3: "GoBD Abgrenzung GDPR Datenschutz Aufbewahrung" -> resolves conflict found in round 2
```

**Why it matters**: First-round results often reveal terminology and concepts that lead to much better second-round queries. The "last mile problem" (Gemini scores 82% F1 but only 66% fully correct) is largely caused by under-retrieval of long-tail information that only surfaces through iterative deepening.

### 3. Web Content Extraction

Convert web pages to clean, structured text instead of working with raw search snippets.

| Tool | Method | Best For |
|------|--------|----------|
| **Jina Reader** | `https://r.jina.ai/{url}` -> markdown | Zero-setup, fast, 1M free tokens |
| **Firecrawl** | API with JS rendering | Complex sites, anti-bot handling |
| **Crawl4AI** | Open source, blazing fast | Self-hosted, LLM integration |
| **Tavily Extract** | `client.extract(urls)` | Already have Tavily key |

**Why it matters**: Search snippets are often truncated or miss context. Full-page extraction gives the LLM much more to work with, especially for technical/legal content.

### 4. Result Reranking

Search APIs return results ranked by their own algorithms. A cross-encoder reranker re-scores them for relevance to the actual research question.

| Model | Type | Notes |
|-------|------|-------|
| **Cohere Rerank 4** | API | State-of-the-art, multilingual |
| **BGE Reranker v2-m3** | Open source | Self-hosted, cost-effective |
| **bge-reranker-v2-gemma** | Open source (2B params) | Strongest open model |

Cross-encoders process query + document together (more accurate than bi-encoders, but slower). Typically applied to top-20 results to select top-5.

**Why it matters**: Reduces noise in the LLM context window. The agent reasons better with 5 highly relevant results than 20 mixed-quality ones.

### 5. Source Triangulation & Hallucination Detection

Verify claims by cross-referencing multiple sources. Detect when the LLM fabricates information.

Hallucination types in RAG systems:
- **Contradictions**: claims that conflict with retrieved sources
- **Unsupported claims**: assertions not grounded in any retrieved text

Detection methods (ranked by effectiveness):
1. LLM prompt-based detector (~75%+ accuracy, low cost)
2. Semantic similarity checking
3. Entailment verification
4. Entity/relation validation

**Important**: RAG does not prevent hallucinations. LLMs can fabricate while citing sources. Active verification is needed.

### 6. Parallel Sub-Agent Research

Spawn multiple research agents for different aspects of a question, then merge results.

```
Orchestrator: "Research GoBD compliance for cloud archiving"
  |
  +-- Agent A: "GoBD requirements for cloud storage providers"
  +-- Agent B: "Cloud archiving certification standards Germany"
  +-- Agent C: "GoBD audit trail requirements digital systems"
  |
  v
Synthesizer: Merge findings, resolve conflicts, produce report
```

GPT-Researcher and LangChain Deep Research both use this pattern. Can achieve up to 90% time reduction through parallelization.

---

## Implementation Roadmap

### Phase 1: Enhanced Single Search (Low Effort, High Impact)

Improve the existing `web_search` tool without changing the architecture.

**1a. Content Extraction**

Add a `read_webpage` tool that converts URLs to clean markdown via Jina Reader:

```python
async def read_webpage(url: str) -> str:
    """Fetch a webpage and return its content as clean markdown."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://r.jina.ai/{url}") as resp:
            return await resp.text()
```

Zero cost, zero API keys, instant improvement. The agent can now actually read pages it finds.

**1b. Tavily Advanced Search**

Use Tavily's `search_depth="advanced"` mode and `include_raw_content=True` for richer results:

```python
results = tavily.search(
    query=query,
    search_depth="advanced",
    include_raw_content=True,
    max_results=10
)
```

We already have a Tavily key. This is a config change.

**1c. Tavily Extract**

Use Tavily's extract endpoint to pull clean content from specific URLs:

```python
extracted = tavily.extract(urls=["https://example.com/page1", "..."])
```

Useful when the agent finds relevant URLs but needs full content.

### Phase 2: Smart Search (Medium Effort, High Impact)

Add intelligence around when and how to search.

**2a. Query Decomposition**

Before searching, have the LLM break the question into 3-5 sub-queries:

```python
async def decompose_query(question: str, llm) -> list[str]:
    """Break a complex question into focused sub-queries."""
    prompt = f"""Break this research question into 3-5 focused web search queries.
    Each query should target a different aspect of the question.
    Return one query per line, nothing else.

    Question: {question}"""
    response = await llm.ainvoke(prompt)
    return [q.strip() for q in response.content.strip().split("\n") if q.strip()]
```

**2b. Iterative Search (Search-Read-Refine Loop)**

Implement a 2-3 round search loop:

```python
async def iterative_search(question: str, rounds: int = 2) -> SearchResults:
    all_results = []
    context = ""

    for round_num in range(rounds):
        if round_num == 0:
            queries = await decompose_query(question)
        else:
            queries = await generate_followup_queries(question, context)

        round_results = await parallel_search(queries)
        all_results.extend(round_results)

        # Build context from what we've learned so far
        context = summarize_findings(all_results)

    return deduplicate_and_rank(all_results)
```

**2c. Result Deduplication & Ranking**

Deduplicate by URL/content similarity, then optionally rerank:

```python
def deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    seen_urls = set()
    unique = []
    for r in results:
        normalized = normalize_url(r.url)
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            unique.append(r)
    return unique
```

### Phase 3: Deep Research Tool (High Effort, Highest Impact)

Build a dedicated `deep_research` tool that orchestrates the full pipeline.

**3a. Deep Research Workflow**

```python
async def deep_research(question: str, max_rounds: int = 3) -> str:
    """
    Multi-round research workflow:
    1. Decompose question into sub-queries
    2. Search + extract content for each sub-query
    3. Identify gaps and generate follow-up queries
    4. Repeat until sufficient coverage
    5. Synthesize findings into a structured report with citations
    """
    findings = []

    for round_num in range(max_rounds):
        # Generate queries
        if round_num == 0:
            queries = await decompose_query(question)
        else:
            gaps = await identify_gaps(question, findings)
            if not gaps:
                break  # Sufficient coverage
            queries = await generate_gap_filling_queries(gaps)

        # Search and extract in parallel
        search_tasks = [search_and_extract(q) for q in queries]
        round_results = await asyncio.gather(*search_tasks)
        findings.extend(round_results)

    # Synthesize
    report = await synthesize_report(question, findings)
    return report
```

**3b. Search-and-Extract Helper**

```python
async def search_and_extract(query: str) -> list[Finding]:
    """Search, then extract full content from top results."""
    search_results = await tavily.search(query, max_results=5)
    findings = []

    for result in search_results:
        if result.score > threshold:
            content = await read_webpage(result.url)  # Jina Reader
            findings.append(Finding(
                query=query,
                url=result.url,
                title=result.title,
                content=content,
                snippet=result.snippet,
            ))

    return findings
```

**3c. Gap Identification**

```python
async def identify_gaps(question: str, findings: list[Finding]) -> list[str]:
    """Ask the LLM what's still missing."""
    prompt = f"""Given this research question and findings so far,
    what important aspects are still not covered?

    Question: {question}

    Findings:
    {format_findings(findings)}

    List missing aspects (one per line), or "COMPLETE" if sufficient."""

    response = await llm.ainvoke(prompt)
    if "COMPLETE" in response.content:
        return []
    return [line.strip() for line in response.content.strip().split("\n")]
```

### Phase 4: Production Hardening (Ongoing)

**4a. Caching**

Cache search results and extracted content to avoid redundant API calls:

```python
from hashlib import sha256

class SearchCache:
    def __init__(self, ttl_seconds: int = 3600):
        self._cache: dict[str, tuple[float, Any]] = {}
        self.ttl = ttl_seconds

    def get(self, query: str) -> Any | None:
        key = sha256(query.encode()).hexdigest()
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < self.ttl:
                return result
        return None

    def set(self, query: str, result: Any):
        key = sha256(query.encode()).hexdigest()
        self._cache[key] = (time.time(), result)
```

**4b. Cost Controls**

- Cap total searches per deep_research call (e.g., max 20 API calls)
- Cap content extraction calls (e.g., max 10 pages)
- Track token usage for LLM calls within the research loop
- Add timeout for entire deep_research operation

**4c. Hallucination Detection**

Post-process the final report to check claims against sources:

```python
async def verify_report(report: str, sources: list[Finding]) -> VerificationResult:
    prompt = f"""Check each factual claim in this report against the sources.

    Report:
    {report}

    Sources:
    {format_sources(sources)}

    For each claim, state: SUPPORTED, UNSUPPORTED, or CONTRADICTED.
    """
    return await llm.ainvoke(prompt)
```

---

## Priority Matrix

| Phase | Technique | Effort | Impact | Dependencies |
|-------|-----------|--------|--------|-------------|
| 1a | Content extraction (Jina Reader) | Low | High | None |
| 1b | Tavily advanced search mode | Low | Medium | Tavily key (have it) |
| 1c | Tavily extract endpoint | Low | Medium | Tavily key (have it) |
| 2a | Query decomposition | Medium | High | LLM call |
| 2b | Iterative search loop | Medium | High | 2a |
| 2c | Result deduplication | Low | Medium | None |
| 3a | Full deep_research tool | High | Very High | 1a, 2a, 2b |
| 3b | Search-and-extract helper | Medium | High | 1a |
| 3c | Gap identification | Medium | High | LLM call |
| 4a | Search caching | Low | Medium | None |
| 4b | Cost controls | Low | Medium | None |
| 4c | Hallucination detection | Medium | Medium | LLM call |

**Recommended implementation order**: 1a -> 1b -> 2a -> 2b -> 1c -> 2c -> 3b -> 3a -> 3c -> 4a -> 4b -> 4c

---

## Reference: Tools and APIs

### Search APIs for Agents

| API | Features | Cost |
|-----|----------|------|
| **Tavily** | Search, extract, crawl, map; built for agents; SOC 2 certified | Pay-per-use |
| **Serper** | Google SERP results via API | Pay-per-use |
| **Brave Search** | Independent index, privacy-focused | Free tier available |
| **SearXNG** | Self-hosted meta-search aggregator | Free (self-hosted) |

### Content Extraction

| Tool | Method | Cost |
|------|--------|------|
| **Jina Reader** | `r.jina.ai/{url}` -> markdown | 1M free tokens |
| **Firecrawl** | API, JS rendering, anti-bot | Pay-per-use |
| **Crawl4AI** | Open source, self-hosted | Free |
| **Tavily Extract** | API endpoint | Included with Tavily |

### Reranking Models

| Model | Type | Notes |
|-------|------|-------|
| **Cohere Rerank 4** | API | Best accuracy, multilingual |
| **BGE Reranker v2-m3** | Open source | Self-hosted |
| **bge-reranker-v2-gemma** | Open source (2B) | Strongest open model |

### Open Source Deep Research Projects

| Project | Architecture | Notes |
|---------|-------------|-------|
| **GPT-Researcher** | Planner-Executor-Publisher | Best benchmarks, pip installable |
| **LangChain Open Deep Research** | Plan-and-Execute / Multi-agent | MIT license, LangGraph based |
| **LangChain Deep Agents** | Supervisor + subagents | LangGraph, provider agnostic |
| **Crawl4AI** | Agentic crawler | MCP server integration |

---

## Sources

- [OpenAI Deep Research architecture](https://cobusgreyling.medium.com/openai-deep-research-ai-agent-architecture-7ac52b5f6a01)
- [OpenAI Deep Research API + Agents SDK](https://cookbook.openai.com/examples/deep_research_api/introduction_to_deep_research_api_agents)
- [Gemini Deep Research overview](https://gemini.google/overview/deep-research/)
- [Gemini Deep Research API](https://ai.google.dev/gemini-api/docs/deep-research)
- [Perplexity AI architecture](https://www.frugaltesting.com/blog/behind-perplexitys-architecture-how-ai-search-handles-real-time-web-data)
- [Perplexity search API design](https://research.perplexity.ai/articles/architecting-and-evaluating-an-ai-first-search-api)
- [GPT-Researcher deep research](https://docs.gptr.dev/blog/2025/02/26/deep-research)
- [LangChain Open Deep Research](https://blog.langchain.com/open-deep-research/)
- [LangChain Deep Agents](https://blog.langchain.com/deep-agents/)
- [Survey: From Web Search towards Agentic Deep Research](https://arxiv.org/html/2506.18959v2)
- [Survey: Deep Research Autonomous Agents](https://arxiv.org/html/2508.12752v1)
- [DeepSearchQA benchmark](https://arxiv.org/html/2601.20975)
- [ReAct: Synergizing Reasoning and Acting](https://arxiv.org/abs/2210.03629)
- [Tavily developer docs](https://www.tavily.com/blog/tavily-101-ai-powered-search-for-developers)
- [Tavily Search API reference](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily Extract API reference](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
- [Tavily Crawl API reference](https://docs.tavily.com/documentation/api-reference/endpoint/crawl)
- [Tavily Map API reference](https://docs.tavily.com/documentation/api-reference/endpoint/map)
- [RAG citation architecture](https://www.tensorlake.ai/blog/rag-citations)
- [RAG hallucination detection (AWS)](https://aws.amazon.com/blogs/machine-learning/detect-hallucinations-for-rag-based-systems/)
- [Reranker comparison](https://www.analyticsvidhya.com/blog/2025/06/top-rerankers-for-rag/)
