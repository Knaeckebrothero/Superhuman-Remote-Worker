"""Academic paper search and download tools.

Provides tools for searching academic databases (arXiv, Semantic Scholar)
and downloading open access papers via arXiv and Unpaywall fallback chain.
"""

from __future__ import annotations

import logging
import re
import tempfile
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


def _transfer_to_workspace(
    context: ToolContext, local_path: Path, dest_rel: str
) -> str:
    """Transfer a local file to the workspace and return the workspace-relative path.

    Args:
        context: ToolContext with workspace.
        local_path: Path to the file on the agent pod.
        dest_rel: Workspace-relative destination (e.g. "documents/paper.pdf").

    Returns:
        Workspace-relative path of the written file.
    """
    content = local_path.read_bytes()
    context.workspace_manager.backend.write_file(dest_rel, content)
    return dest_rel


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
    ) -> str:
        """Download paper PDF to workspace documents folder.

        Uses fallback chain: arXiv -> Unpaywall (open-access copies).
        Downloaded papers are registered as citation sources when possible.

        Args:
            identifier: DOI (e.g., "10.1038/nature12373"), arXiv ID (e.g., "2408.08921"), or URL
            identifier_type: "doi", "arxiv", or "auto" (auto-detect)

        Returns:
            Path to downloaded PDF or error message with suggestions
        """
        if identifier_type == "auto":
            identifier_type = _detect_identifier_type(identifier)

        if not context.has_workspace():
            return "Could not download paper: no workspace is available."

        # Track whether we found a paywalled paper (for messaging)
        paywalled_title = None

        # A virtual workspace has ``host is None`` but its paths are object
        # keys, not agent-local files. Every tier therefore downloads to a
        # bounded local staging directory and writes the result via the
        # backend.
        with tempfile.TemporaryDirectory(prefix="paper_dl_") as temp_dir:
            dest_dir = Path(temp_dir)

            # Try arXiv first (for arXiv IDs, or DOIs that might be arXiv)
            if identifier_type == "arxiv" or "arxiv" in identifier.lower():
                result = await _try_arxiv_download(identifier, dest_dir)
                if result.success:
                    ws_path = _store_download_in_workspace(context, result.path)
                    await _register_downloaded_paper(context, result, ws_path)
                    return (
                        f"Downloaded: {result.paper.title}\n"
                        f"Path: {ws_path}\n"
                        f"Source: arXiv ({result.paper.arxiv_id})"
                    )

            # Try Unpaywall for DOIs
            if identifier_type == "doi":
                doi = DOI_PATTERN.search(identifier)
                if doi:
                    result = await _try_unpaywall_download(
                        doi.group(), dest_dir, proxy=proxy
                    )
                    if result.success:
                        ws_path = _store_download_in_workspace(context, result.path)
                        await _register_downloaded_paper(context, result, ws_path)
                        return (
                            f"Downloaded: {result.paper.title}\n"
                            f"Path: {ws_path}\n"
                            f"Source: Unpaywall (OA copy)"
                        )
                    elif (
                        result.paper and result.paper.access_status.value == "paywalled"
                    ):
                        paywalled_title = result.paper.title
                    elif result.error:
                        logger.debug(f"Unpaywall download failed: {result.error}")

        # All methods failed
        if paywalled_title:
            return (
                f"Paper is paywalled: {paywalled_title}\n"
                f"No open access version found.\n"
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

        # Try Semantic Scholar first (richer metadata). Provider/auth failures
        # remain visible while still allowing the arXiv fallback to do useful
        # work; they must not masquerade as "paper not found".
        from .utils.semantic_scholar_client import SemanticScholarProviderError

        semantic_error = None
        try:
            info = await _get_semantic_scholar_info(identifier, proxy=proxy)
        except SemanticScholarProviderError as exc:
            semantic_error = str(exc)
            logger.warning(semantic_error)
        else:
            if info:
                return info

        # Fall back to arXiv for arXiv IDs
        if id_type == "arxiv" or "arxiv" in identifier.lower():
            arxiv_info = await _get_arxiv_info(identifier)
            if semantic_error:
                return (
                    f"{semantic_error}\nUsing arXiv metadata fallback.\n\n{arxiv_info}"
                )
            return arxiv_info

        if semantic_error:
            return f"{semantic_error}\nCould not fall back to arXiv for: {identifier}"
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


async def _search_semantic_scholar(query: str, max_results: int, *, proxy=None) -> str:
    """Search Semantic Scholar and format results."""
    from .utils.semantic_scholar_client import (
        SemanticScholarProviderError,
        search_semantic_scholar,
    )

    try:
        data = await search_semantic_scholar(
            query,
            max_results,
            fields=(
                "title,authors,year,abstract,citationCount,openAccessPdf,"
                "externalIds,venue"
            ),
            proxy=proxy,
        )
    except SemanticScholarProviderError as exc:
        return str(exc)

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


async def _try_unpaywall_download(
    doi: str, dest_dir: Path, *, proxy=None
) -> "DownloadResult":
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


async def _get_semantic_scholar_info(identifier: str, *, proxy=None) -> Optional[str]:
    """Get paper info from Semantic Scholar."""
    from .utils.semantic_scholar_client import get_semantic_scholar_paper

    # Semantic Scholar accepts DOIs and arXiv IDs directly
    paper_id = identifier
    if ARXIV_PATTERN.search(identifier) and "10." not in identifier:
        arxiv_id = ARXIV_PATTERN.search(identifier).group()
        paper_id = f"ArXiv:{arxiv_id}"
    elif DOI_PATTERN.search(identifier):
        paper_id = f"DOI:{DOI_PATTERN.search(identifier).group()}"

    data = await get_semantic_scholar_paper(
        paper_id,
        fields=(
            "title,authors,year,abstract,citationCount,referenceCount,"
            "openAccessPdf,externalIds,venue,publicationDate"
        ),
        proxy=proxy,
    )
    if data is None:
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


def _store_download_in_workspace(
    context: ToolContext, local_path: Optional[Path]
) -> str:
    """Write one operation-scoped download through the workspace backend."""
    if local_path is None:
        return ""
    dest_rel = f"documents/{local_path.name}"
    _transfer_to_workspace(context, local_path, dest_rel)
    return dest_rel


async def _register_downloaded_paper(
    context: ToolContext, result, display_path: Optional[str] = None
) -> None:
    """Register a downloaded paper as a citation source."""
    if not result.success or not result.path or not result.paper:
        return

    path_str = display_path or str(result.path)
    try:
        source_id = await context.get_or_register_doc_source(
            path_str, name=result.paper.title
        )
        logger.info(
            f"Registered downloaded paper as citation source {source_id}: {result.paper.title}"
        )
    except Exception as e:
        logger.debug(f"Could not register paper as citation source: {e}")
