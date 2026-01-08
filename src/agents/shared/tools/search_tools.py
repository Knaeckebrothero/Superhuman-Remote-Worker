"""Search tools for the Universal Agent.

Provides web search (Tavily) and graph search (Neo4j) capabilities.
"""

import logging
from typing import Any, List, Optional

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
    },
    "query_similar_requirements": {
        "module": "search_tools",
        "function": "query_similar_requirements",
        "description": "Find similar requirements in the Neo4j graph",
        "category": "domain",
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

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            Search results with snippets and URLs
        """
        try:
            researcher = get_researcher()
            if researcher is None:
                # Fallback to direct Tavily if researcher not available
                return _direct_web_search(query, max_results)

            results = researcher.web_search(query, max_results=max_results)

            if not results:
                return f"No web results found for: {query}"

            result = f"Web Search Results for: {query}\n"
            result += f"Results: {len(results)}\n\n"

            for i, r in enumerate(results, 1):
                result += f"{i}. {r.get('title', 'Untitled')}\n"
                result += f"   URL: {r.get('url', 'N/A')}\n"
                result += f"   {r.get('snippet', '')[:300]}...\n\n"

            return result

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return f"Error searching web: {str(e)}"

    @tool
    def query_similar_requirements(
        text: str,
        limit: int = 5
    ) -> str:
        """Find similar requirements in the Neo4j graph.

        Args:
            text: Requirement text to match against
            limit: Maximum results

        Returns:
            Similar requirements with similarity scores
        """
        try:
            researcher = get_researcher()
            if researcher is None:
                # Fallback to direct Neo4j query
                return _direct_graph_search(text, limit, context.neo4j_conn)

            similar = researcher.find_similar_requirements(text, limit=limit)

            if not similar:
                return f"No similar requirements found in the graph."

            result = f"Similar Requirements Found: {len(similar)}\n\n"

            for i, req in enumerate(similar, 1):
                result += f"{i}. [{req.get('rid', 'N/A')}] {req.get('name', 'Unnamed')}\n"
                result += f"   Similarity: {req.get('similarity', 0):.2f}\n"
                result += f"   Text: {req.get('text', '')[:200]}...\n"
                result += f"   Status: {req.get('status', 'unknown')}\n\n"

            return result

        except Exception as e:
            logger.error(f"Graph query error: {e}")
            return f"Error querying similar requirements: {str(e)}"

    return [
        web_search,
        query_similar_requirements,
    ]


def _direct_web_search(query: str, max_results: int) -> str:
    """Direct Tavily web search without Researcher component."""
    import os

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        response = client.search(query, max_results=max_results)

        results = response.get("results", [])
        if not results:
            return f"No web results found for: {query}"

        result = f"Web Search Results for: {query}\n"
        result += f"Results: {len(results)}\n\n"

        for i, r in enumerate(results, 1):
            result += f"{i}. {r.get('title', 'Untitled')}\n"
            result += f"   URL: {r.get('url', 'N/A')}\n"
            result += f"   {r.get('content', '')[:300]}...\n\n"

        return result

    except ImportError:
        return "Error: Tavily package not installed"
    except Exception as e:
        return f"Error searching web: {str(e)}"


def _direct_graph_search(text: str, limit: int, neo4j_conn: Any) -> str:
    """Direct Neo4j search without Researcher component."""
    if neo4j_conn is None:
        return "Error: No Neo4j connection available"

    try:
        from difflib import SequenceMatcher

        # Get all requirements
        query = """
        MATCH (r:Requirement)
        RETURN r.rid AS rid, r.name AS name, r.text AS text,
               r.status AS status
        LIMIT 1000
        """
        results = neo4j_conn.execute_query(query)

        if not results:
            return "No requirements found in graph"

        # Calculate similarity
        text_lower = text.lower().strip()
        similar = []

        for req in results:
            req_text = (req.get("text") or "").lower().strip()
            if not req_text:
                continue

            similarity = SequenceMatcher(None, text_lower, req_text).ratio()

            if similarity >= 0.3:  # Lower threshold for discovery
                similar.append({
                    "rid": req.get("rid"),
                    "name": req.get("name"),
                    "text": req.get("text", "")[:200],
                    "status": req.get("status", "unknown"),
                    "similarity": round(similarity, 3),
                })

        similar.sort(key=lambda x: x["similarity"], reverse=True)
        similar = similar[:limit]

        if not similar:
            return "No similar requirements found in the graph."

        result = f"Similar Requirements Found: {len(similar)}\n\n"
        for i, req in enumerate(similar, 1):
            result += f"{i}. [{req['rid']}] {req['name']}\n"
            result += f"   Similarity: {req['similarity']:.2f}\n"
            result += f"   Text: {req['text']}...\n"
            result += f"   Status: {req['status']}\n\n"

        return result

    except Exception as e:
        return f"Error querying graph: {str(e)}"
