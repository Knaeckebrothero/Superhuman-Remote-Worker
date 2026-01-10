"""Search tools for the Universal Agent.

Provides web search capabilities using Tavily API.
"""

import logging
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
SEARCH_TOOLS_METADATA = {
    "web_search": {
        "module": "search_tools",
        "function": "web_search",
        "description": "Search the web using Tavily API",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Search the web via Tavily for context and research.",
    },
}


def create_search_tools(context: ToolContext) -> List:
    """Create search tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Lazy load researcher component
    _researcher = None

    def get_researcher():
        nonlocal _researcher
        if _researcher is None:
            try:
                from src.agents.creator.researcher import Researcher
                config = context.config or {}
                _researcher = Researcher(
                    web_search_enabled=config.get("web_search_enabled", True),
                    graph_search_enabled=config.get("graph_search_enabled", True),
                    neo4j_conn=context.neo4j_conn
                )
            except ImportError:
                logger.warning("Researcher component not available")
                _researcher = None
        return _researcher

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the web for context using Tavily.

        Each result is automatically registered as a citation source.
        Use cite_web() with the URL to create citations from these sources.

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            Search results with snippets, URLs, and source IDs
        """
        try:
            researcher = get_researcher()
            if researcher is None:
                # Fallback to direct Tavily if researcher not available
                return _direct_web_search(query, max_results, context)

            results = researcher.web_search(query, max_results=max_results)

            if not results:
                return f"No web results found for: {query}"

            # Register each result as a citation source
            registered_sources = []
            for r in results:
                url = r.get('url', '')
                title = r.get('title', 'Untitled')
                if url:
                    try:
                        source_id = context.get_or_register_web_source(url, name=title)
                        registered_sources.append((url, source_id))
                    except Exception as e:
                        logger.warning(f"Could not register web source {url}: {e}")

            # Format output with source IDs
            result = f"Web Search Results for: {query}\n"
            result += f"Results: {len(results)} ({len(registered_sources)} archived as citation sources)\n\n"

            for i, r in enumerate(results, 1):
                url = r.get('url', 'N/A')
                source_id = next((sid for u, sid in registered_sources if u == url), None)
                result += f"{i}. {r.get('title', 'Untitled')}\n"
                result += f"   URL: {url}\n"
                if source_id:
                    result += f"   Source ID: {source_id} (archived)\n"
                result += f"   {r.get('snippet', '')[:300]}...\n\n"

            result += "To cite: use cite_web(text, url) - sources are already archived."

            return result

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return f"Error searching web: {str(e)}"

    return [
        web_search,
    ]


def _direct_web_search(query: str, max_results: int, context: Optional[ToolContext] = None) -> str:
    """Direct Tavily web search without Researcher component.

    Args:
        query: Search query
        max_results: Maximum results to return
        context: Optional ToolContext for source registration

    Returns:
        Search results with snippets, URLs, and source IDs (if context provided)
    """
    import os

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    try:
        from langchain_tavily import TavilySearch
        search = TavilySearch(api_key=api_key, max_results=max_results)
        response = search.invoke({"query": query})

        results = response.get("results", [])
        if not results:
            return f"No web results found for: {query}"

        # Register each result as a citation source if context available
        registered_sources = []
        if context is not None:
            for r in results:
                url = r.get('url', '')
                title = r.get('title', 'Untitled')
                if url:
                    try:
                        source_id = context.get_or_register_web_source(url, name=title)
                        registered_sources.append((url, source_id))
                    except Exception as e:
                        logger.warning(f"Could not register web source {url}: {e}")

        # Format output
        result = f"Web Search Results for: {query}\n"
        if registered_sources:
            result += f"Results: {len(results)} ({len(registered_sources)} archived as citation sources)\n\n"
        else:
            result += f"Results: {len(results)}\n\n"

        for i, r in enumerate(results, 1):
            url = r.get('url', 'N/A')
            source_id = next((sid for u, sid in registered_sources if u == url), None)
            result += f"{i}. {r.get('title', 'Untitled')}\n"
            result += f"   URL: {url}\n"
            if source_id:
                result += f"   Source ID: {source_id} (archived)\n"
            result += f"   {r.get('content', '')[:300]}...\n\n"

        if registered_sources:
            result += "To cite: use cite_web(text, url) - sources are already archived."
        else:
            result += "To cite information from these results, use cite_web(text, url, title) for each source you reference."

        return result

    except ImportError:
        return "Error: langchain-tavily package not installed"
    except Exception as e:
        return f"Error searching web: {str(e)}"
