# Tavily Web Tools Implementation Plan

Implementation plan for enhancing `web_search` and adding `extract_webpage`, `crawl_website`, and `map_website` tools using the `langchain-tavily` package (already in `requirements.txt`).

## Overview

| Tool | Tavily Endpoint | Status | Purpose |
|------|----------------|--------|---------|
| `web_search` | TavilySearch | Enhance | Expose search_depth, topic, time_range, domain filters, raw content |
| `extract_webpage` | TavilyExtract | New | Read full content from 1-20 URLs |
| `crawl_website` | TavilyCrawl | New | Spider a site following links |
| `map_website` | TavilyMap | New | Discover URLs on a site (no content) |

All tools use `TAVILY_API_KEY` from environment. All are tactical-phase only.

---

## Files to Change

| File | Action |
|------|--------|
| `src/tools/research/web.py` | Modify — enhance web_search, add 3 tools + metadata + helpers |
| `config/defaults.yaml` | Modify — add 3 tool names to research list |
| `tests/tools/research/test_web_tools.py` | Create — ~40 tests |
| `tests/tools/research/test_browser_tools.py` | Modify — update 2 registry integration tests |

No changes needed to `src/tools/research/__init__.py` — new tools in `web.py` are automatically picked up via `create_web_tools()` and `RESEARCH_TOOLS_METADATA`.

---

## 1. `src/tools/research/web.py`

### 1.1 Add Helper Functions

```python
def _get_tavily_api_key() -> Optional[str]:
    """Get Tavily API key from environment."""
    import os
    return os.getenv("TAVILY_API_KEY")


def _parse_comma_list(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated string into a list, or return None."""
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items if items else None
```

Define a module-level constant for content truncation:

```python
MAX_RAW_CONTENT_WORDS = 5000
```

### 1.2 Add Metadata for New Tools

Extend `RESEARCH_TOOLS_METADATA` with 3 new entries:

```python
RESEARCH_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "web_search": {
        "module": "research.web",
        "function": "web_search",
        "description": "Search the web using Tavily API",
        "category": "research",
        "defer_to_workspace": True,
        "short_description": "Search the web via Tavily for context and research.",
        "phases": ["tactical"],
    },
    "extract_webpage": {
        "module": "research.web",
        "function": "extract_webpage",
        "description": "Extract full content from one or more web pages using Tavily Extract",
        "category": "research",
        "short_description": "Extract full page content from URLs via Tavily Extract.",
        "phases": ["tactical"],
    },
    "crawl_website": {
        "module": "research.web",
        "function": "crawl_website",
        "description": "Crawl a website starting from a URL using Tavily Crawl",
        "category": "research",
        "short_description": "Crawl website pages with depth/breadth control via Tavily Crawl.",
        "phases": ["tactical"],
    },
    "map_website": {
        "module": "research.web",
        "function": "map_website",
        "description": "Map website structure to discover URLs using Tavily Map",
        "category": "research",
        "short_description": "Discover URLs in a website's structure via Tavily Map.",
        "phases": ["tactical"],
    },
}
```

### 1.3 Enhance `web_search`

**New signature:**

```python
@tool
def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    topic: str = "general",
    time_range: Optional[str] = None,
    include_domains: Optional[str] = None,
    exclude_domains: Optional[str] = None,
    include_raw_content: bool = False,
) -> str:
    """Search the web for information using Tavily.

    Each result is automatically registered as a citation source.
    Use cite_web() with the URL to create citations from these sources.

    Args:
        query: Search query
        max_results: Maximum results to return (1-20, default 5)
        search_depth: "basic" (fast, 1 credit) or "advanced" (better relevance, 2 credits)
        topic: "general" (default) or "news" (optimized for recent events)
        time_range: Filter by recency: "day", "week", "month", "year", or None
        include_domains: Comma-separated domains to restrict search to
        exclude_domains: Comma-separated domains to exclude from results
        include_raw_content: If true, return full page content instead of snippets

    Returns:
        Search results with snippets (or full content), URLs, and source IDs
    """
    return _direct_web_search(
        query, max_results, context,
        search_depth=search_depth, topic=topic, time_range=time_range,
        include_domains=include_domains, exclude_domains=exclude_domains,
        include_raw_content=include_raw_content,
    )
```

**Changes to `_direct_web_search`:**

New signature with all parameters:

```python
def _direct_web_search(
    query: str,
    max_results: int,
    context: Optional[ToolContext] = None,
    search_depth: str = "basic",
    topic: str = "general",
    time_range: Optional[str] = None,
    include_domains: Optional[str] = None,
    exclude_domains: Optional[str] = None,
    include_raw_content: bool = False,
) -> str:
```

Key implementation details:

**Constructor** — `include_raw_content` must be set at construction time (langchain-tavily constraint, raises ValueError if passed at invoke time):

```python
from langchain_tavily import TavilySearch

constructor_kwargs = {"api_key": api_key, "max_results": max_results}
if include_raw_content:
    constructor_kwargs["include_raw_content"] = True
search = TavilySearch(**constructor_kwargs)
```

**Invoke** — runtime parameters passed to `.invoke()`:

```python
invoke_kwargs = {"query": query}
if search_depth != "basic":
    invoke_kwargs["search_depth"] = search_depth
if topic != "general":
    invoke_kwargs["topic"] = topic
if time_range:
    invoke_kwargs["time_range"] = time_range

parsed_include = _parse_comma_list(include_domains)
parsed_exclude = _parse_comma_list(exclude_domains)
if parsed_include:
    invoke_kwargs["include_domains"] = parsed_include
if parsed_exclude:
    invoke_kwargs["exclude_domains"] = parsed_exclude

response = search.invoke(invoke_kwargs)
```

**Content formatting** — when `include_raw_content=True`, use `raw_content` instead of truncated `content`:

```python
if include_raw_content:
    content = r.get('raw_content', r.get('content', ''))
    words = content.split()
    if len(words) > MAX_RAW_CONTENT_WORDS:
        content = ' '.join(words[:MAX_RAW_CONTENT_WORDS]) + f"\n... (truncated from {len(words)} words)"
    result += f"   {content}\n\n"
else:
    result += f"   {r.get('content', '')[:300]}...\n\n"
```

Everything else (citation registration, inaccessible source warnings, formatting) stays the same.

### 1.4 New Tool: `extract_webpage`

```python
@tool
def extract_webpage(
    urls: str,
    query: Optional[str] = None,
    extract_depth: str = "basic",
) -> str:
    """Extract full content from one or more web pages using Tavily Extract.

    Retrieves the complete text content of web pages as clean markdown.
    Useful for reading articles, documentation, or any web content in full.
    Each extracted URL is automatically registered as a citation source.

    Args:
        urls: URL or comma-separated list of URLs to extract (max 20)
        query: Optional query to rank extracted content by relevance
        extract_depth: "basic" (fast, default) or "advanced" (JS-heavy sites)

    Returns:
        Extracted content from each URL with source IDs for citation
    """
    api_key = _get_tavily_api_key()
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        return "Error: No URLs provided"
    if len(url_list) > 20:
        return "Error: Maximum 20 URLs allowed per request"

    try:
        from langchain_tavily import TavilyExtract

        extract = TavilyExtract(api_key=api_key, extract_depth=extract_depth)

        invoke_kwargs = {"urls": url_list}
        if query:
            invoke_kwargs["query"] = query

        response = extract.invoke(invoke_kwargs)
        results = response.get("results", [])
        failed = response.get("failed_results", [])

        if not results and not failed:
            return f"No content could be extracted from the provided URL(s)."

        # Register extracted URLs as citation sources
        registered = []
        if context is not None:
            for r in results:
                url = r.get("url", "")
                if url:
                    try:
                        source_id, fetch_error = context.get_or_register_web_source(url)
                        registered.append((url, source_id))
                    except Exception as e:
                        logger.warning(f"Could not register source {url}: {e}")

        # Format output
        output = f"Extracted Content from {len(results)} URL(s)"
        if failed:
            output += f" ({len(failed)} failed)"
        output += ":\n\n"

        for i, r in enumerate(results, 1):
            url = r.get("url", "N/A")
            content = r.get("raw_content", "")
            source_id = next((sid for u, sid in registered if u == url), None)

            words = content.split()
            word_count = len(words)
            if word_count > MAX_RAW_CONTENT_WORDS:
                content = ' '.join(words[:MAX_RAW_CONTENT_WORDS])
                content += f"\n... (truncated from {word_count} words)"

            output += f"{i}. {url}\n"
            if source_id:
                output += f"   Source ID: {source_id} (archived)\n"
            output += f"   Words: {word_count:,}\n"
            output += f"   Content:\n{content}\n\n"

        if failed:
            output += "Failed URLs:\n"
            for f_url in failed:
                u = f_url if isinstance(f_url, str) else f_url.get("url", str(f_url))
                output += f"  - {u}\n"

        if registered:
            output += "\nTo cite: use cite_web(text, url) - sources are already archived."

        return output

    except ImportError:
        return "Error: langchain-tavily package not installed"
    except Exception as e:
        return f"Error extracting content: {str(e)}"
```

### 1.5 New Tool: `crawl_website`

```python
@tool
def crawl_website(
    url: str,
    instructions: Optional[str] = None,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 20,
    select_paths: Optional[str] = None,
    exclude_paths: Optional[str] = None,
) -> str:
    """Crawl a website starting from a URL using Tavily Crawl.

    Performs a breadth-first traversal from the starting URL, extracting
    content from discovered pages. Each crawled page is automatically
    registered as a citation source. Good for exploring documentation
    sites, regulatory pages, or multi-page resources.

    Args:
        url: Starting URL to crawl from
        instructions: Natural language guidance for the crawler
        max_depth: Link hops from start URL (1-5, default 1)
        max_breadth: Links to follow per page (1-500, default 20)
        limit: Total pages to crawl (default 20, keep low for cost)
        select_paths: Comma-separated regex patterns for paths to include
        exclude_paths: Comma-separated regex patterns for paths to exclude

    Returns:
        Crawled content from each page with source IDs for citation
    """
    api_key = _get_tavily_api_key()
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    # Clamp parameters
    max_depth = max(1, min(5, max_depth))
    max_breadth = max(1, min(500, max_breadth))
    limit = max(1, limit)

    try:
        from langchain_tavily import TavilyCrawl

        crawl = TavilyCrawl(api_key=api_key)

        invoke_kwargs = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
        }
        if instructions:
            invoke_kwargs["instructions"] = instructions
        parsed_select = _parse_comma_list(select_paths)
        parsed_exclude = _parse_comma_list(exclude_paths)
        if parsed_select:
            invoke_kwargs["select_paths"] = parsed_select
        if parsed_exclude:
            invoke_kwargs["exclude_paths"] = parsed_exclude

        response = crawl.invoke(invoke_kwargs)
        results = response.get("results", [])

        if not results:
            return f"No pages could be crawled from: {url}"

        # Register crawled URLs as citation sources
        registered = []
        if context is not None:
            for r in results:
                page_url = r.get("url", "")
                if page_url:
                    try:
                        source_id, fetch_error = context.get_or_register_web_source(page_url)
                        registered.append((page_url, source_id))
                    except Exception as e:
                        logger.warning(f"Could not register source {page_url}: {e}")

        # Format output
        output = f"Website Crawl Results for: {url}\n"
        output += f"Pages crawled: {len(results)}\n\n"

        for i, r in enumerate(results, 1):
            page_url = r.get("url", "N/A")
            content = r.get("raw_content", "")
            source_id = next((sid for u, sid in registered if u == page_url), None)

            words = content.split()
            word_count = len(words)
            if word_count > MAX_RAW_CONTENT_WORDS:
                content = ' '.join(words[:MAX_RAW_CONTENT_WORDS])
                content += f"\n... (truncated from {word_count} words)"

            output += f"{i}. {page_url}\n"
            if source_id:
                output += f"   Source ID: {source_id} (archived)\n"
            output += f"   Words: {word_count:,}\n"
            output += f"   Content:\n{content}\n\n"

        if registered:
            output += "To cite: use cite_web(text, url) - sources are already archived."

        return output

    except ImportError:
        return "Error: langchain-tavily package not installed"
    except Exception as e:
        return f"Error crawling website: {str(e)}"
```

### 1.6 New Tool: `map_website`

```python
@tool
def map_website(
    url: str,
    instructions: Optional[str] = None,
    max_depth: int = 2,
    limit: int = 50,
    select_paths: Optional[str] = None,
    exclude_paths: Optional[str] = None,
) -> str:
    """Map a website's structure to discover URLs using Tavily Map.

    Creates a sitemap-like listing of all discoverable URLs on a website.
    Does NOT extract content — use extract_webpage or crawl_website for that.
    Useful for understanding site structure before targeted extraction.

    Args:
        url: Starting URL to map from
        instructions: Natural language guidance for URL discovery
        max_depth: Exploration depth (1-5, default 2)
        limit: Maximum URLs to discover (default 50)
        select_paths: Comma-separated regex patterns for paths to include
        exclude_paths: Comma-separated regex patterns for paths to exclude

    Returns:
        List of discovered URLs
    """
    api_key = _get_tavily_api_key()
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    max_depth = max(1, min(5, max_depth))
    limit = max(1, limit)

    try:
        from langchain_tavily import TavilyMap

        mapper = TavilyMap(api_key=api_key)

        invoke_kwargs = {
            "url": url,
            "max_depth": max_depth,
            "limit": limit,
        }
        if instructions:
            invoke_kwargs["instructions"] = instructions
        parsed_select = _parse_comma_list(select_paths)
        parsed_exclude = _parse_comma_list(exclude_paths)
        if parsed_select:
            invoke_kwargs["select_paths"] = parsed_select
        if parsed_exclude:
            invoke_kwargs["exclude_paths"] = parsed_exclude

        response = mapper.invoke(invoke_kwargs)
        results = response.get("results", [])

        if not results:
            return f"No URLs discovered for: {url}"

        output = f"Website Map for: {url}\n"
        output += f"URLs discovered: {len(results)}\n\n"

        for i, discovered_url in enumerate(results, 1):
            if isinstance(discovered_url, dict):
                discovered_url = discovered_url.get("url", str(discovered_url))
            output += f"{i}. {discovered_url}\n"

        output += "\nUse extract_webpage(urls) to read specific pages, "
        output += "or crawl_website(url) to crawl with content extraction."

        return output

    except ImportError:
        return "Error: langchain-tavily package not installed"
    except Exception as e:
        return f"Error mapping website: {str(e)}"
```

### 1.7 Update `create_web_tools` Return List

```python
def create_web_tools(context: ToolContext) -> List[Any]:
    # ... all tool definitions ...
    return [web_search, extract_webpage, crawl_website, map_website]
```

---

## 2. `config/defaults.yaml`

Add the 3 new tool names to the research tools list (after `web_search`):

```yaml
  # Research tools - web search, academic papers, browser & workflows (src/tools/research/)
  research:
    - web_search
    - extract_webpage
    - crawl_website
    - map_website
    - search_papers
    - download_paper
    - get_paper_info
    - browse_website
    - download_from_website
    - research_topic
```

---

## 3. `tests/tools/research/test_web_tools.py` (NEW)

### Mocking Pattern

All tools use `langchain_tavily` classes. Mock them at the import site:

```python
with patch("src.tools.research.web.TavilySearch") as MockSearch:
    mock_instance = MagicMock()
    mock_instance.invoke.return_value = {
        "results": [
            {"url": "https://example.com", "title": "Example", "content": "Some content", "raw_content": "Full content..."}
        ]
    }
    MockSearch.return_value = mock_instance
    # ... invoke tool ...
```

Note: `langchain_tavily` classes are imported inside function bodies (lazy imports), so patch at the point where they're used: `src.tools.research.web.TavilySearch`, etc. If the import happens inside the function via `from langchain_tavily import TavilySearch`, patch `langchain_tavily.TavilySearch` instead — verify which works during implementation.

### Test Classes

**TestWebToolsMetadata** (3 tests):
- `test_metadata_has_all_tools` — all 4 names in `RESEARCH_TOOLS_METADATA`
- `test_metadata_category_is_research` — all have `category == "research"`
- `test_metadata_phases_tactical` — all have `["tactical"]`

**TestCreateWebTools** (2 tests):
- `test_creates_four_tools` — `len(create_web_tools(ctx)) == 4`
- `test_tool_names` — names are `{"web_search", "extract_webpage", "crawl_website", "map_website"}`

**TestWebSearch** (~12 tests):
- `test_basic_search` — mock TavilySearch, verify formatted output
- `test_missing_api_key` — env cleared, returns error string
- `test_no_results` — empty results list
- `test_search_depth_advanced` — `search_depth="advanced"` passed in invoke kwargs
- `test_topic_news` — `topic="news"` passed in invoke kwargs
- `test_time_range` — `time_range="week"` passed in invoke kwargs
- `test_include_domains` — `"example.com, other.com"` parsed to list, passed in invoke kwargs
- `test_exclude_domains` — same parsing/passing
- `test_raw_content_creates_new_instance` — when `include_raw_content=True`, TavilySearch constructed with `include_raw_content=True`
- `test_raw_content_not_truncated` — full `raw_content` field used, not 300-char truncation
- `test_raw_content_word_limit` — content beyond 5000 words gets truncated
- `test_citation_registration` — `context.get_or_register_web_source()` called per result
- `test_inaccessible_sources_warning` — WARNING in output when `fetch_error` is set

**TestExtractWebpage** (~7 tests):
- `test_single_url` — one URL extracted, content returned
- `test_multiple_urls` — comma-separated parsed correctly, all extracted
- `test_with_query` — query parameter passed through
- `test_advanced_depth` — `extract_depth="advanced"` passed
- `test_citation_registration` — sources registered
- `test_missing_api_key` — error message
- `test_content_word_limit` — truncation at MAX_RAW_CONTENT_WORDS
- `test_failed_urls_reported` — `failed_results` shown in output

**TestCrawlWebsite** (~7 tests):
- `test_basic_crawl` — single-page crawl, content returned
- `test_with_instructions` — instructions passed
- `test_path_filters` — `select_paths`/`exclude_paths` parsed from comma-separated
- `test_depth_clamping` — `max_depth=10` clamped to 5, `max_depth=-1` clamped to 1
- `test_citation_registration` — sources registered
- `test_missing_api_key` — error message
- `test_content_word_limit` — truncation

**TestMapWebsite** (~6 tests):
- `test_basic_map` — URLs listed in output
- `test_with_instructions` — instructions passed
- `test_path_filters` — parsed correctly
- `test_no_citation_registration` — `get_or_register_web_source` NOT called
- `test_missing_api_key` — error message
- `test_guidance_message` — output mentions extract_webpage/crawl_website

**TestHelpers** (~6 tests):
- `test_get_api_key_present` — returns key
- `test_get_api_key_missing` — returns None
- `test_parse_comma_list_single` — `"a"` -> `["a"]`
- `test_parse_comma_list_multiple` — `"a, b, c"` -> `["a", "b", "c"]`
- `test_parse_comma_list_none` — `None` -> `None`
- `test_parse_comma_list_empty` — `""` -> `None`

---

## 4. `tests/tools/research/test_browser_tools.py`

Update `TestResearchToolsRegistry` (lines 698-730):

In `test_get_research_metadata_includes_all_modules` add:
```python
assert "extract_webpage" in metadata
assert "crawl_website" in metadata
assert "map_website" in metadata
```

In `test_create_research_tools_returns_all` add:
```python
assert "extract_webpage" in names
assert "crawl_website" in names
assert "map_website" in names
```

---

## Design Decisions

### String Parameters for Lists

LLM tool-calling is more reliable with string inputs than JSON arrays. `urls`, `include_domains`, `exclude_domains`, `select_paths`, `exclude_paths` all accept comma-separated strings, parsed internally via `_parse_comma_list()`.

### `include_raw_content` Construction-Time Constraint

`langchain-tavily` raises `ValueError` if `include_raw_content` is passed at invoke time. Solution: conditionally construct a separate `TavilySearch` instance with that flag.

### Word Count Limits

`MAX_RAW_CONTENT_WORDS = 5000` per result/page to protect LLM context window. Applied to `web_search` (raw mode), `extract_webpage`, and `crawl_website`. Not needed for `map_website` (URLs only).

### Cost Control Defaults

- `crawl_website` defaults to `limit=20` (Tavily default is 50)
- `map_website` defaults to `limit=50` (reasonable for URL discovery)
- Agent can override but sensible defaults prevent runaway costs

### Sync Tools

All 4 tools are synchronous (consistent with existing `web_search`). LangChain's `@tool` decorator handles both `invoke()` and `ainvoke()` automatically for sync functions.

### No Changes to `__init__.py`

New tools added to `web.py` are automatically included because `create_research_tools()` calls `create_web_tools()` and `get_research_metadata()` imports `RESEARCH_TOOLS_METADATA` — both already in `src/tools/research/__init__.py`.

---

## Verification

```bash
# Run new tests
pytest tests/tools/research/test_web_tools.py -v

# Run updated registry tests
pytest tests/tools/research/test_browser_tools.py::TestResearchToolsRegistry -v

# Full research test suite (check for regressions)
pytest tests/tools/research/ -v

# Full test suite
pytest tests/ -v
```

---

## Tavily API Cost Reference

| Endpoint | Depth | Cost |
|----------|-------|------|
| Search | basic / fast / ultra-fast | 1 credit |
| Search | advanced | 2 credits |
| Extract | basic | 1 credit per 5 URLs |
| Extract | advanced | 2 credits per 5 URLs |
| Crawl | basic | 1 credit per 5 pages |
| Crawl | advanced | 2 credits per 5 pages |
| Crawl | with instructions | 2 credits per 10 pages |
| Map | without instructions | 1 credit per 10 pages |
| Map | with instructions | 2 credits per 10 pages |
