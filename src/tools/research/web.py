"""Web search tools for the Universal Agent.

Provides web search and content extraction capabilities using Tavily API.
Supports search, extract, crawl, and map operations.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

# Maximum words per result/page to protect LLM context window
MAX_RAW_CONTENT_WORDS = 5000

# Tool metadata for registry
# Phase availability: domain tools are tactical-only
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


def _truncate_content(content: str, max_words: int = MAX_RAW_CONTENT_WORDS) -> str:
    """Truncate content to max_words, appending a note if truncated."""
    words = content.split()
    if len(words) > max_words:
        return ' '.join(words[:max_words]) + f"\n... (truncated from {len(words)} words)"
    return content


def create_web_tools(context: ToolContext) -> List[Any]:
    """Create web search tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """

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
        return _extract_webpage(urls, context, query=query, extract_depth=extract_depth)

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
        return _crawl_website(
            url, context, instructions=instructions,
            max_depth=max_depth, max_breadth=max_breadth, limit=limit,
            select_paths=select_paths, exclude_paths=exclude_paths,
        )

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
        Does NOT extract content - use extract_webpage or crawl_website for that.
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
        return _map_website(
            url, instructions=instructions,
            max_depth=max_depth, limit=limit,
            select_paths=select_paths, exclude_paths=exclude_paths,
        )

    return [web_search, extract_webpage, crawl_website, map_website]


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
    """Direct Tavily web search.

    Args:
        query: Search query
        max_results: Maximum results to return
        context: Optional ToolContext for source registration
        search_depth: "basic" or "advanced"
        topic: "general" or "news"
        time_range: "day", "week", "month", "year", or None
        include_domains: Comma-separated domains to include
        exclude_domains: Comma-separated domains to exclude
        include_raw_content: Whether to return full page content

    Returns:
        Search results with snippets, URLs, and source IDs (if context provided)
    """
    api_key = _get_tavily_api_key()
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    try:
        from langchain_tavily import TavilySearch

        # include_raw_content must be set at construction time
        constructor_kwargs = {"api_key": api_key, "max_results": max_results}
        if include_raw_content:
            constructor_kwargs["include_raw_content"] = True
        search = TavilySearch(**constructor_kwargs)

        # Runtime parameters passed at invoke time
        invoke_kwargs: Dict[str, Any] = {"query": query}
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

        results = response.get("results", [])
        if not results:
            return f"No web results found for: {query}"

        # Register each result as a citation source if context available
        registered_sources = []
        inaccessible_sources = []
        if context is not None:
            for r in results:
                url = r.get('url', '')
                title = r.get('title', 'Untitled')
                if url:
                    try:
                        source_id, fetch_error = context.get_or_register_web_source(url, name=title)
                        registered_sources.append((url, source_id))
                        if fetch_error:
                            inaccessible_sources.append((url, source_id))
                    except Exception as e:
                        logger.warning(f"Could not register web source {url}: {e}")

        # Save web content to disk for persistence
        if context is not None:
            for r in results:
                url = r.get('url', '')
                if url:
                    content = r.get('raw_content', r.get('content', ''))
                    if content and len(content) > 50:
                        title = r.get('title', 'Untitled')
                        source_id = next((sid for u, sid in registered_sources if u == url), None)
                        context.save_web_content_to_disk(url, content, title=title, source_id=source_id)

        # Format output
        result = f"Web Search Results for: {query}\n"
        if registered_sources:
            result += f"Results: {len(results)} ({len(registered_sources)} archived as citation sources)\n\n"
        else:
            result += f"Results: {len(results)}\n\n"

        for i, r in enumerate(results, 1):
            url = r.get('url', 'N/A')
            source_id = next((sid for u, sid in registered_sources if u == url), None)
            is_inaccessible = any(u == url for u, _ in inaccessible_sources)
            result += f"{i}. {r.get('title', 'Untitled')}\n"
            result += f"   URL: {url}\n"
            if source_id and is_inaccessible:
                result += f"   Source ID: {source_id} (INACCESSIBLE - content not fetched)\n"
            elif source_id:
                result += f"   Source ID: {source_id} (archived)\n"

            if include_raw_content:
                content = r.get('raw_content', r.get('content', ''))
                result += f"   {_truncate_content(content)}\n\n"
            else:
                result += f"   {r.get('content', '')[:300]}...\n\n"

        if inaccessible_sources:
            result += (
                f"\nWARNING: {len(inaccessible_sources)} source(s) could not be fetched automatically "
                f"(HTTP 403 or similar). Use the browser tool to manually download content from these URLs "
                f"if you need to cite them:\n"
            )
            for url, _ in inaccessible_sources:
                result += f"  - {url}\n"
            result += "\n"

        if registered_sources:
            result += "To cite: use cite_web(text, url) - sources are already archived."
        else:
            result += "To cite information from these results, use cite_web(text, url, title) for each source you reference."

        return result

    except ImportError:
        return "Error: langchain-tavily package not installed"
    except Exception as e:
        return f"Error searching web: {str(e)}"


def _extract_webpage(
    urls: str,
    context: Optional[ToolContext] = None,
    query: Optional[str] = None,
    extract_depth: str = "basic",
) -> str:
    """Extract full content from web pages using Tavily Extract.

    Args:
        urls: Comma-separated URLs to extract
        context: Optional ToolContext for source registration
        query: Optional relevance ranking query
        extract_depth: "basic" or "advanced"

    Returns:
        Extracted content from each URL
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

        invoke_kwargs: Dict[str, Any] = {"urls": url_list}
        if query:
            invoke_kwargs["query"] = query

        response = extract.invoke(invoke_kwargs)
        results = response.get("results", [])
        failed = response.get("failed_results", [])

        if not results and not failed:
            return "No content could be extracted from the provided URL(s)."

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

        # Save web content to disk for persistence (before truncation)
        if context is not None:
            for r in results:
                url = r.get("url", "")
                raw = r.get("raw_content", "")
                if url and raw:
                    source_id = next((sid for u, sid in registered if u == url), None)
                    context.save_web_content_to_disk(url, raw, source_id=source_id)

        # Format output
        output = f"Extracted Content from {len(results)} URL(s)"
        if failed:
            output += f" ({len(failed)} failed)"
        output += ":\n\n"

        for i, r in enumerate(results, 1):
            url = r.get("url", "N/A")
            content = r.get("raw_content", "")
            source_id = next((sid for u, sid in registered if u == url), None)

            word_count = len(content.split())
            content = _truncate_content(content)

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


def _crawl_website(
    url: str,
    context: Optional[ToolContext] = None,
    instructions: Optional[str] = None,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 20,
    select_paths: Optional[str] = None,
    exclude_paths: Optional[str] = None,
) -> str:
    """Crawl a website using Tavily Crawl.

    Args:
        url: Starting URL
        context: Optional ToolContext for source registration
        instructions: Natural language guidance
        max_depth: Link hops (1-5)
        max_breadth: Links per page (1-500)
        limit: Total pages
        select_paths: Comma-separated path regex patterns to include
        exclude_paths: Comma-separated path regex patterns to exclude

    Returns:
        Crawled content from each page
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

        invoke_kwargs: Dict[str, Any] = {
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

        # Save web content to disk for persistence (before truncation)
        if context is not None:
            for r in results:
                page_url = r.get("url", "")
                raw = r.get("raw_content", "")
                if page_url and raw:
                    source_id = next((sid for u, sid in registered if u == page_url), None)
                    context.save_web_content_to_disk(page_url, raw, source_id=source_id)

        # Format output
        output = f"Website Crawl Results for: {url}\n"
        output += f"Pages crawled: {len(results)}\n\n"

        for i, r in enumerate(results, 1):
            page_url = r.get("url", "N/A")
            content = r.get("raw_content", "")
            source_id = next((sid for u, sid in registered if u == page_url), None)

            word_count = len(content.split())
            content = _truncate_content(content)

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


def _map_website(
    url: str,
    instructions: Optional[str] = None,
    max_depth: int = 2,
    limit: int = 50,
    select_paths: Optional[str] = None,
    exclude_paths: Optional[str] = None,
) -> str:
    """Map website structure using Tavily Map.

    Args:
        url: Starting URL
        instructions: Natural language guidance
        max_depth: Exploration depth (1-5)
        limit: Maximum URLs
        select_paths: Comma-separated path regex patterns to include
        exclude_paths: Comma-separated path regex patterns to exclude

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

        invoke_kwargs: Dict[str, Any] = {
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
