"""Academic paper search and download tools.

Provides tools for searching academic databases (arXiv, Semantic Scholar)
and downloading open access papers via arXiv and Unpaywall fallback chain.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .utils.paper_types import DownloadResult

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

PAPER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "search_papers": {
        "module": "research.papers",
        "function": "search_papers",
        "description": "Search academic databases for papers",
        "category": "research",
        "short_description": "Search arXiv or Semantic Scholar for academic papers.",
        "phases": ["tactical"],
    },
    "download_paper": {
        "module": "research.papers",
        "function": "download_paper",
        "description": "Download paper PDF to workspace",
        "category": "research",
        "short_description": "Download paper PDF using arXiv/Unpaywall/browser fallback chain.",
        "phases": ["tactical"],
    },
    "get_paper_info": {
        "module": "research.papers",
        "function": "get_paper_info",
        "description": "Get metadata and citation info for a paper",
        "category": "research",
        "short_description": "Get paper metadata, abstract, and citations via Semantic Scholar.",
        "phases": ["tactical"],
    },
}


# DOI pattern: 10.XXXX/...
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)

# arXiv ID pattern: YYMM.NNNNN
ARXIV_PATTERN = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")


def _detect_identifier_type(identifier: str) -> str:
    """Detect whether identifier is a DOI, arXiv ID, or URL."""
    if DOI_PATTERN.search(identifier):
        return "doi"
    if ARXIV_PATTERN.search(identifier):
        return "arxiv"
    if "arxiv.org" in identifier:
        return "arxiv"
    return "doi"  # Default assumption


def _get_documents_dir(context: ToolContext) -> Path:
    """Get the documents directory from workspace, or a fallback."""
    if context.has_workspace():
        return context.workspace_manager.get_path("documents")
    return Path("./downloads")


def create_paper_tools(context: ToolContext) -> List[Any]:
    """Create academic paper tools."""
    from .utils.network import get_proxy_from_context

    proxy = get_proxy_from_context(context)

    @tool
    async def search_papers(
        query: str,
        source: str = "arxiv",
        max_results: int = 10,
    ) -> str:
        """Search for academic papers.

        Args:
            query: Search query (keywords, title, author)
            source: Database to search ("arxiv" or "semantic_scholar")
            max_results: Maximum results (default 10, max 50)

        Returns:
            Formatted list of papers with metadata and access status
        """
        max_results = min(max_results, 50)

        if source == "arxiv":
            return await _search_arxiv(query, max_results)
        elif source == "semantic_scholar":
            return await _search_semantic_scholar(query, max_results, proxy=proxy)
        else:
            return f"Unknown source: {source}. Use 'arxiv' or 'semantic_scholar'."

    @tool
    async def download_paper(
        identifier: str,
        identifier_type: str = "auto",
        use_browser_fallback: bool = True,
    ) -> str:
        """Download paper PDF to workspace documents folder.

        Uses fallback chain: arXiv -> Unpaywall -> Browser automation.
        Downloaded papers are registered as citation sources when possible.

        Args:
            identifier: DOI (e.g., "10.1038/nature12373"), arXiv ID (e.g., "2408.08921"), or URL
            identifier_type: "doi", "arxiv", or "auto" (auto-detect)
            use_browser_fallback: Try browser automation if API methods fail (default True). Useful for publisher pages with VPN access.

        Returns:
            Path to downloaded PDF or error message with suggestions
        """
        if identifier_type == "auto":
            identifier_type = _detect_identifier_type(identifier)

        dest_dir = _get_documents_dir(context)

        # Track whether we found a paywalled paper (for messaging)
        paywalled_title = None

        # Try arXiv first (for arXiv IDs, or DOIs that might be arXiv)
        if identifier_type == "arxiv" or "arxiv" in identifier.lower():
            result = await _try_arxiv_download(identifier, dest_dir)
            if result.success:
                _register_downloaded_paper(context, result)
                return (
                    f"Downloaded: {result.paper.title}\n"
                    f"Path: {result.path}\n"
                    f"Source: arXiv ({result.paper.arxiv_id})"
                )

        # Try Unpaywall for DOIs
        if identifier_type == "doi":
            doi = DOI_PATTERN.search(identifier)
            if doi:
                result = await _try_unpaywall_download(doi.group(), dest_dir, proxy=proxy)
                if result.success:
                    _register_downloaded_paper(context, result)
                    return (
                        f"Downloaded: {result.paper.title}\n"
                        f"Path: {result.path}\n"
                        f"Source: Unpaywall (OA copy)"
                    )
                elif result.paper and result.paper.access_status.value == "paywalled":
                    paywalled_title = result.paper.title
                elif result.error:
                    logger.debug(f"Unpaywall download failed: {result.error}")

        # Try browser automation as final fallback
        if use_browser_fallback:
            browser_result = await _try_browser_download(
                identifier, identifier_type, dest_dir, context, proxy=proxy
            )
            if browser_result:
                return browser_result

        # All methods failed
        if paywalled_title:
            return (
                f"Paper is paywalled: {paywalled_title}\n"
                f"No open access version found and browser download failed.\n"
                f"Suggestions:\n"
                f"  - Check if a preprint exists on arXiv\n"
                f"  - Connect to institutional VPN and configure proxy\n"
                f"  - Contact the author directly"
            )

        return (
            f"Could not download paper for identifier: {identifier}\n"
            f"Detected type: {identifier_type}\n"
            f"Suggestions:\n"
            f"  - For arXiv papers, use the arXiv ID (e.g., '2408.08921')\n"
            f"  - For other papers, use the DOI (e.g., '10.1038/nature12373')\n"
            f"  - Use search_papers to find the paper first"
        )

    @tool
    async def get_paper_info(identifier: str) -> str:
        """Get detailed paper information including abstract, authors, and citations.

        Uses Semantic Scholar for rich metadata. Falls back to arXiv for arXiv IDs.

        Args:
            identifier: DOI (e.g., "10.1038/nature12373") or arXiv ID (e.g., "2408.08921")

        Returns:
            Paper metadata including abstract, authors, citations, and access status
        """
        id_type = _detect_identifier_type(identifier)

        # Try Semantic Scholar first (richer metadata)
        info = await _get_semantic_scholar_info(identifier, proxy=proxy)
        if info:
            return info

        # Fall back to arXiv for arXiv IDs
        if id_type == "arxiv" or "arxiv" in identifier.lower():
            return await _get_arxiv_info(identifier)

        return f"Could not find paper info for: {identifier}"

    return [search_papers, download_paper, get_paper_info]


# --- Implementation helpers ---


async def _search_arxiv(query: str, max_results: int) -> str:
    """Search arXiv and format results."""
    try:
        from .utils.arxiv_client import ArxivClient

        client = ArxivClient()
        papers = await client.search(query, max_results)
    except ImportError:
        return "Error: 'arxiv' package not installed. Run: pip install arxiv"
    except Exception as e:
        return f"arXiv search error: {e}"

    if not papers:
        return f"No arXiv results for: {query}"

    lines = [f"arXiv Search Results for: {query}", f"Results: {len(papers)}", ""]
    for i, paper in enumerate(papers, 1):
        lines.append(paper.format(index=i))
        lines.append("")
    return "\n".join(lines)


async def _search_semantic_scholar(
    query: str, max_results: int, *, proxy=None
) -> str:
    """Search Semantic Scholar and format results."""
    import os

    import aiohttp

    from .utils.network import research_request

    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": max_results,
        "fields": "title,authors,year,abstract,citationCount,openAccessPdf,externalIds,venue",
    }

    try:
        async with research_request(
            "GET", url, proxy=proxy, timeout=30, params=params, headers=headers
        ) as resp:
            if resp.status == 429:
                return "Semantic Scholar rate limit hit. Try again in a few minutes or set SEMANTIC_SCHOLAR_API_KEY."
            resp.raise_for_status()
            data = await resp.json()
    except ConnectionError as e:
        return f"Semantic Scholar search error (connection): {e}"
    except aiohttp.ClientError as e:
        return f"Semantic Scholar search error: {e}"

    results = data.get("data", [])
    if not results:
        return f"No Semantic Scholar results for: {query}"

    from .utils.paper_types import AccessStatus, Paper, PaperSource

    lines = [
        f"Semantic Scholar Results for: {query}",
        f"Results: {len(results)} (of {data.get('total', '?')} total)",
        "",
    ]
    for i, r in enumerate(results, 1):
        ext_ids = r.get("externalIds") or {}
        oa_pdf = r.get("openAccessPdf") or {}
        paper = Paper(
            title=r.get("title", "Unknown"),
            authors=[a.get("name", "") for a in r.get("authors", [])],
            abstract=r.get("abstract"),
            doi=ext_ids.get("DOI"),
            arxiv_id=ext_ids.get("ArXiv"),
            url=f"https://api.semanticscholar.org/graph/v1/paper/{r.get('paperId', '')}",
            pdf_url=oa_pdf.get("url"),
            source=PaperSource.SEMANTIC_SCHOLAR,
            access_status=AccessStatus.OPEN_ACCESS if oa_pdf else AccessStatus.UNKNOWN,
            citation_count=r.get("citationCount"),
            year=r.get("year"),
            venue=r.get("venue"),
        )
        lines.append(paper.format(index=i))
        lines.append("")

    return "\n".join(lines)


async def _try_arxiv_download(identifier: str, dest_dir: Path) -> "DownloadResult":
    """Try downloading from arXiv."""
    from .utils.arxiv_client import ArxivClient, extract_arxiv_id

    arxiv_id = extract_arxiv_id(identifier) or identifier
    client = ArxivClient()
    return await client.download(arxiv_id, dest_dir)


async def _try_unpaywall_download(doi: str, dest_dir: Path, *, proxy=None) -> "DownloadResult":
    """Try downloading via Unpaywall."""
    from .utils.paper_types import DownloadResult
    from .utils.unpaywall_client import UnpaywallClient

    client = UnpaywallClient(proxy=proxy)
    if not client.is_configured():
        return DownloadResult(
            success=False,
            error="UNPAYWALL_EMAIL not configured. Set it in .env to enable Unpaywall lookups.",
        )
    return await client.download(doi, dest_dir)


async def _resolve_doi_url(doi: str, *, proxy=None) -> Optional[str]:
    """Resolve a DOI to its publisher URL by following the redirect.

    Args:
        doi: DOI string (e.g., "10.1038/nature12373")
        proxy: Optional ProxyConfig for routing through VPN.

    Returns:
        Publisher URL or None if resolution fails
    """
    from .utils.network import research_request

    doi_url = f"https://doi.org/{doi}"
    try:
        async with research_request(
            "HEAD", doi_url, proxy=proxy, timeout=15, allow_redirects=True
        ) as resp:
            return str(resp.url)
    except Exception as e:
        logger.debug(f"DOI resolution failed for {doi}: {e}")
        return doi_url  # Fall back to doi.org URL


async def _try_browser_download(
    identifier: str,
    identifier_type: str,
    dest_dir: Path,
    context: "ToolContext",
    *,
    proxy=None,
) -> Optional[str]:
    """Try downloading a paper using browser automation.

    Resolves the identifier to a URL and uses browser-use to navigate
    and download the PDF. Works best with institutional VPN/proxy.

    Args:
        identifier: Paper identifier (DOI, arXiv ID, or URL)
        identifier_type: Type of identifier
        dest_dir: Download destination directory
        context: ToolContext for browser configuration

    Returns:
        Success message string, or None if browser download failed/unavailable
    """
    try:
        from .browser import _get_browser_config, _get_browser_llm, _find_new_files, _register_downloaded_file
        from browser_use import Agent, Browser
    except ImportError:
        logger.debug("browser-use not available for fallback download")
        return None

    # Resolve identifier to a URL
    if identifier_type == "doi":
        doi_match = DOI_PATTERN.search(identifier)
        if doi_match:
            url = await _resolve_doi_url(doi_match.group(), proxy=proxy)
        else:
            return None
    elif identifier_type == "arxiv":
        arxiv_match = ARXIV_PATTERN.search(identifier)
        arxiv_id = arxiv_match.group() if arxiv_match else identifier
        url = f"https://arxiv.org/abs/{arxiv_id}"
    elif identifier.startswith("http"):
        url = identifier
    else:
        return None

    logger.info(f"Trying browser download from: {url}")

    browser = None
    try:
        llm = _get_browser_llm()
        browser_kwargs = _get_browser_config(context, downloads_path=dest_dir)
        browser = Browser(**browser_kwargs)

        agent = Agent(
            task=(
                f"Go to {url} and download the PDF of this paper. "
                f"Look for a 'Download PDF' button or link. "
                f"Accept any cookie banners if needed. "
                f"Wait for the download to complete."
            ),
            llm=llm,
            browser=browser,
            use_vision=False,
            max_actions_per_step=4,
        )

        await agent.run()

        # Check for downloaded files
        downloaded_files = _find_new_files(dest_dir)
        if downloaded_files:
            downloaded_path = downloaded_files[0]
            _register_downloaded_file(context, downloaded_path)
            return (
                f"Downloaded via browser: {downloaded_path.name}\n"
                f"Path: {downloaded_path}\n"
                f"Size: {downloaded_path.stat().st_size:,} bytes\n"
                f"Source: Browser automation ({url})"
            )

        return None

    except Exception as e:
        logger.debug(f"Browser download failed: {e}")
        return None
    finally:
        if browser is not None:
            try:
                await browser.stop()
            except Exception:
                pass


async def _get_semantic_scholar_info(identifier: str, *, proxy=None) -> Optional[str]:
    """Get paper info from Semantic Scholar."""
    import os

    import aiohttp

    from .utils.network import research_request

    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    # Semantic Scholar accepts DOIs and arXiv IDs directly
    paper_id = identifier
    if ARXIV_PATTERN.search(identifier) and "10." not in identifier:
        arxiv_id = ARXIV_PATTERN.search(identifier).group()
        paper_id = f"ArXiv:{arxiv_id}"
    elif DOI_PATTERN.search(identifier):
        paper_id = DOI_PATTERN.search(identifier).group()

    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
    params = {
        "fields": "title,authors,year,abstract,citationCount,referenceCount,openAccessPdf,externalIds,venue,publicationDate",
    }

    try:
        async with research_request(
            "GET", url, proxy=proxy, timeout=30, params=params, headers=headers
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status == 429:
                return "Semantic Scholar rate limit hit. Try again in a few minutes."
            resp.raise_for_status()
            data = await resp.json()
    except ConnectionError:
        return None
    except aiohttp.ClientError:
        return None

    ext_ids = data.get("externalIds") or {}
    oa_pdf = data.get("openAccessPdf") or {}
    authors = [a.get("name", "") for a in data.get("authors", [])]

    lines = [
        f"Paper: {data.get('title', 'Unknown')}",
        f"Authors: {', '.join(authors[:10])}",
    ]
    if len(authors) > 10:
        lines[-1] += f" (+{len(authors) - 10} more)"
    if data.get("year"):
        lines.append(f"Year: {data['year']}")
    if data.get("venue"):
        lines.append(f"Venue: {data['venue']}")
    if data.get("publicationDate"):
        lines.append(f"Published: {data['publicationDate']}")
    if ext_ids.get("DOI"):
        lines.append(f"DOI: {ext_ids['DOI']}")
    if ext_ids.get("ArXiv"):
        lines.append(f"arXiv: {ext_ids['ArXiv']}")
    lines.append(f"Citations: {data.get('citationCount', 'N/A')}")
    lines.append(f"References: {data.get('referenceCount', 'N/A')}")
    if oa_pdf.get("url"):
        lines.append(f"Open Access PDF: {oa_pdf['url']}")
    else:
        lines.append("Open Access PDF: Not available")
    if data.get("abstract"):
        lines.append(f"\nAbstract:\n{data['abstract']}")

    return "\n".join(lines)


async def _get_arxiv_info(identifier: str) -> str:
    """Get paper info from arXiv."""
    try:
        from .utils.arxiv_client import ArxivClient

        client = ArxivClient()
        paper = await client.get_paper(identifier)
    except ImportError:
        return "Error: 'arxiv' package not installed. Run: pip install arxiv"
    except Exception as e:
        return f"arXiv lookup error: {e}"

    if not paper:
        return f"Paper not found on arXiv: {identifier}"

    lines = [
        f"Paper: {paper.title}",
        f"Authors: {', '.join(paper.authors[:10])}",
    ]
    if len(paper.authors) > 10:
        lines[-1] += f" (+{len(paper.authors) - 10} more)"
    if paper.year:
        lines.append(f"Year: {paper.year}")
    if paper.arxiv_id:
        lines.append(f"arXiv: {paper.arxiv_id}")
    if paper.doi:
        lines.append(f"DOI: {paper.doi}")
    lines.append(f"Access: {paper.access_status.value}")
    if paper.pdf_url:
        lines.append(f"PDF: {paper.pdf_url}")
    if paper.abstract:
        lines.append(f"\nAbstract:\n{paper.abstract}")

    return "\n".join(lines)


def _register_downloaded_paper(context: ToolContext, result) -> None:
    """Register a downloaded paper as a citation source."""
    if not result.success or not result.path or not result.paper:
        return

    try:
        source_id = context.get_or_register_doc_source(
            str(result.path), name=result.paper.title
        )
        logger.info(
            f"Registered downloaded paper as citation source {source_id}: {result.paper.title}"
        )
    except Exception as e:
        logger.debug(f"Could not register paper as citation source: {e}")
