"""Tavily web search for the instruction builder."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def tavily_search(query: str, max_results: int = 5) -> str:
    """Search the web using Tavily API.

    Returns formatted text results for the LLM. On any error,
    returns an error string (never raises).
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: TAVILY_API_KEY not configured."

    payload = {
        "query": query,
        "max_results": max(1, min(10, max_results)),
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": False,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TAVILY_SEARCH_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return f"Error: Search timed out for: {query}"
    except httpx.HTTPStatusError as e:
        return f"Error: Search failed (HTTP {e.response.status_code}) for: {query}"
    except httpx.RequestError as e:
        return f"Error: Search request failed: {e}"

    # Format results as readable text for the LLM
    results = data.get("results", [])
    answer = data.get("answer")

    output = f"## Web Search: {query}\n\n"
    if answer:
        output += f"**Summary:** {answer}\n\n"
    for i, r in enumerate(results, 1):
        output += (
            f"{i}. **{r.get('title', '')}**\n"
            f"   {r.get('url', '')}\n"
            f"   {r.get('content', '')[:400]}\n\n"
        )

    return output or f"No results for: {query}"
