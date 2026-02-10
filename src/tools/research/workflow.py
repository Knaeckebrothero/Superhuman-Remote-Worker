"""Research workflow tools for comprehensive literature search.

Provides high-level research tools that orchestrate multiple search
databases, deduplicate results, and download available papers.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


WORKFLOW_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "research_topic": {
        "module": "research.workflow",
        "function": "research_topic",
        "description": "Comprehensive literature search across multiple databases",
        "category": "research",
        "short_description": "Search arXiv + Semantic Scholar, deduplicate, download OA papers.",
        "phases": ["tactical"],
    },
}


def create_workflow_tools(context: ToolContext) -> List[Any]:
    """Create research workflow tools.

    Args:
        context: ToolContext with workspace_manager (optional but recommended)

    Returns:
        List of LangChain tool functions
    """
    from .utils.network import get_proxy_from_context

    proxy = get_proxy_from_context(context)

    @tool
    async def research_topic(
        topic: str,
        num_papers: int = 10,
        download_available: bool = True,
        include_abstracts: bool = True,
    ) -> str:
        """Comprehensive literature search across multiple databases.

        Searches arXiv and Semantic Scholar, deduplicates results by DOI/arXiv ID,
        ranks by citation count, checks open access availability, and optionally
        downloads available papers to workspace.

        Args:
            topic: Research topic or question (e.g., "graph retrieval augmented generation")
            num_papers: Target number of unique papers to find (default 10, max 30)
            download_available: Download open access papers to workspace (default True)
            include_abstracts: Include paper abstracts in the report (default True)

        Returns:
            Formatted research report with paper list, download results, and statistics
        """
        num_papers = min(max(num_papers, 1), 30)

        # Search both databases concurrently
        arxiv_papers, s2_papers = await asyncio.gather(
            _search_arxiv_raw(topic, num_papers),
            _search_semantic_scholar_raw(topic, num_papers, proxy=proxy),
            return_exceptions=True,
        )

        # Handle errors gracefully
        if isinstance(arxiv_papers, Exception):
            logger.warning(f"arXiv search failed: {arxiv_papers}")
            arxiv_papers = []
        if isinstance(s2_papers, Exception):
            logger.warning(f"Semantic Scholar search failed: {s2_papers}")
            s2_papers = []

        if not arxiv_papers and not s2_papers:
            return f"No results found for: {topic}\nTry different keywords or a broader search query."

        # Deduplicate across databases
        unique_papers = _deduplicate_papers(arxiv_papers, s2_papers)

        # Sort by citation count (descending), papers without counts go last
        unique_papers.sort(
            key=lambda p: (p.citation_count or -1),
            reverse=True,
        )

        # Trim to target count
        unique_papers = unique_papers[:num_papers]

        # Download available papers
        download_results: List[str] = []
        if download_available and context.has_workspace():
            download_results = await _download_available_papers(
                unique_papers, context, proxy=proxy
            )

        # Format report
        return _format_research_report(
            topic=topic,
            papers=unique_papers,
            download_results=download_results,
            arxiv_count=len(arxiv_papers) if isinstance(arxiv_papers, list) else 0,
            s2_count=len(s2_papers) if isinstance(s2_papers, list) else 0,
            include_abstracts=include_abstracts,
        )

    return [research_topic]


# --- Internal helpers ---


async def _search_arxiv_raw(query: str, max_results: int):
    """Search arXiv and return Paper objects."""
    from .utils.arxiv_client import ArxivClient

    client = ArxivClient()
    return await client.search(query, max_results)


async def _search_semantic_scholar_raw(query: str, max_results: int, *, proxy=None):
    """Search Semantic Scholar and return Paper objects."""
    import os

    from .utils.network import research_request
    from .utils.paper_types import AccessStatus, Paper, PaperSource

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

    async with research_request(
        "GET", url, proxy=proxy, timeout=30, params=params, headers=headers
    ) as resp:
        if resp.status == 429:
            raise Exception("Semantic Scholar rate limit hit")
        resp.raise_for_status()
        data = await resp.json()

    results = data.get("data", [])
    papers = []
    for r in results:
        ext_ids = r.get("externalIds") or {}
        oa_pdf = r.get("openAccessPdf") or {}
        papers.append(Paper(
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
        ))
    return papers


def _deduplicate_papers(arxiv_papers, s2_papers):
    """Deduplicate papers across databases using DOI and arXiv ID.

    When duplicates exist, prefer the version with richer metadata
    (more citation info, access status, etc.).
    """
    from .utils.paper_types import Paper

    seen_dois: Set[str] = set()
    seen_arxiv_ids: Set[str] = set()
    unique: List[Paper] = []

    def _paper_key(paper: Paper) -> Optional[str]:
        """Get a deduplication key for a paper."""
        if paper.doi:
            return f"doi:{paper.doi.lower()}"
        if paper.arxiv_id:
            return f"arxiv:{paper.arxiv_id}"
        return None

    def _is_duplicate(paper: Paper) -> bool:
        """Check if paper is a duplicate of one already seen."""
        if paper.doi and paper.doi.lower() in seen_dois:
            return True
        if paper.arxiv_id and paper.arxiv_id in seen_arxiv_ids:
            return True
        return False

    def _register(paper: Paper):
        """Register paper identifiers as seen."""
        if paper.doi:
            seen_dois.add(paper.doi.lower())
        if paper.arxiv_id:
            seen_arxiv_ids.add(paper.arxiv_id)

    # Process Semantic Scholar first (generally richer metadata with citations)
    for paper in s2_papers:
        if not _is_duplicate(paper):
            unique.append(paper)
            _register(paper)

    # Add arXiv papers that aren't duplicates
    for paper in arxiv_papers:
        if not _is_duplicate(paper):
            unique.append(paper)
            _register(paper)

    return unique


async def _download_available_papers(
    papers,
    context: ToolContext,
    *,
    proxy=None,
) -> List[str]:
    """Download open access papers to workspace.

    Only attempts download for papers with known PDF URLs or arXiv IDs.
    Limits concurrent downloads to avoid rate limiting.

    Returns:
        List of result messages for each download attempt
    """
    from .utils.paper_types import AccessStatus

    results = []
    dest_dir = context.workspace_manager.get_path("documents")
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloadable = [
        p for p in papers
        if p.access_status == AccessStatus.OPEN_ACCESS
        or p.arxiv_id is not None
        or p.pdf_url is not None
    ]

    if not downloadable:
        return ["No open access papers available for download."]

    for paper in downloadable[:5]:  # Limit to 5 downloads per research call
        try:
            if paper.arxiv_id:
                result = await _download_single_arxiv(paper.arxiv_id, dest_dir)
            elif paper.pdf_url:
                result = await _download_single_url(paper.pdf_url, paper.title, dest_dir, proxy=proxy)
            else:
                continue

            if result:
                # Register as citation source
                try:
                    context.get_or_register_doc_source(
                        str(result), name=paper.title
                    )
                except Exception:
                    pass
                results.append(f"  Downloaded: {paper.title} -> {result.name}")
            else:
                results.append(f"  Failed: {paper.title}")

        except Exception as e:
            logger.debug(f"Download failed for {paper.title}: {e}")
            results.append(f"  Failed: {paper.title} ({e})")

    return results


async def _download_single_arxiv(arxiv_id: str, dest_dir: Path) -> Optional[Path]:
    """Download a single paper from arXiv."""
    from .utils.arxiv_client import ArxivClient

    client = ArxivClient()
    result = await client.download(arxiv_id, dest_dir)
    return result.path if result.success else None


async def _download_single_url(
    url: str, title: str, dest_dir: Path, *, proxy=None
) -> Optional[Path]:
    """Download a PDF from a direct URL."""
    from .utils.network import research_request

    try:
        async with research_request("GET", url, proxy=proxy, timeout=60) as resp:
            if resp.status != 200:
                return None

            # Generate filename from title
            safe_title = "".join(
                c if c.isalnum() or c in " -_" else "_"
                for c in title[:80]
            ).strip()
            filename = f"{safe_title}.pdf"
            path = dest_dir / filename

            content = await resp.read()
            path.write_bytes(content)
            return path

    except Exception as e:
        logger.debug(f"URL download failed for {url}: {e}")
        return None


def _format_research_report(
    topic: str,
    papers,
    download_results: List[str],
    arxiv_count: int,
    s2_count: int,
    include_abstracts: bool,
) -> str:
    """Format a comprehensive research report."""
    from .utils.paper_types import AccessStatus

    lines = [
        f"Research Report: {topic}",
        "=" * 60,
        "",
        f"Sources searched: arXiv ({arxiv_count} results), Semantic Scholar ({s2_count} results)",
        f"Unique papers after deduplication: {len(papers)}",
    ]

    # Stats
    oa_count = sum(
        1 for p in papers
        if p.access_status == AccessStatus.OPEN_ACCESS or p.arxiv_id
    )
    lines.append(f"Open access available: {oa_count}/{len(papers)}")
    lines.append("")

    # Paper list
    lines.append("Papers (ranked by citations):")
    lines.append("-" * 40)

    for i, paper in enumerate(papers, 1):
        lines.append("")
        lines.append(paper.format(index=i))
        if include_abstracts and paper.abstract:
            # Truncate long abstracts
            abstract = paper.abstract
            if len(abstract) > 400:
                abstract = abstract[:400] + "..."
            lines.append(f"   Abstract: {abstract}")

    # Download results
    if download_results:
        lines.append("")
        lines.append("-" * 40)
        lines.append("Downloads:")
        lines.extend(download_results)

    lines.append("")
    lines.append("=" * 60)
    lines.append(
        "Use get_paper_info(identifier) for detailed metadata, "
        "or download_paper(identifier) to download specific papers."
    )

    return "\n".join(lines)
