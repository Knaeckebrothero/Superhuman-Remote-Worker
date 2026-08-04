"""Async wrapper for arXiv API."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from .paper_types import AccessStatus, DownloadResult, Paper, PaperSource

logger = logging.getLogger(__name__)

# Pattern to extract arXiv ID from various formats
ARXIV_ID_PATTERN = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5}(?:v\d+)?)")


def extract_arxiv_id(identifier: str) -> Optional[str]:
    """Extract arXiv ID from a URL or identifier string."""
    match = ARXIV_ID_PATTERN.search(identifier)
    return match.group(1) if match else None


class ArxivClient:
    """Async wrapper for arXiv API."""

    def __init__(
        self,
        rate_limit_seconds: float = 3.0,
        *,
        client: Optional[Any] = None,
    ):
        """Create a wrapper around one reusable ``arxiv.Client``.

        ``Search.results()`` was removed in arxiv 4.0.  The supported API is
        ``Client.results(search)``; keeping the client on this wrapper also lets
        the library enforce its own cross-request delay and retry policy.

        ``client`` is injectable so unit tests exercise the real public API
        boundary instead of teaching a ``MagicMock`` the removed method.
        """
        self.rate_limit = rate_limit_seconds
        self._client = client

    def _get_client(self):
        if self._client is None:
            import arxiv

            self._client = arxiv.Client(delay_seconds=self.rate_limit)
        return self._client

    async def _results(self, search) -> List[Any]:
        """Exhaust ``Client.results`` off the asyncio event loop."""

        client = self._get_client()
        return await asyncio.to_thread(lambda: list(client.results(search)))

    async def search(self, query: str, max_results: int = 10) -> List[Paper]:
        """Search arXiv for papers."""
        import arxiv

        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.Relevance,
        )
        results = await self._results(search)
        return [self._to_paper(r) for r in results]

    async def get_paper(self, arxiv_id: str) -> Optional[Paper]:
        """Get a specific paper by arXiv ID."""
        import arxiv

        clean_id = extract_arxiv_id(arxiv_id) or arxiv_id
        search = arxiv.Search(id_list=[clean_id])
        results = await self._results(search)
        return self._to_paper(results[0]) if results else None

    async def download(self, arxiv_id: str, dest_dir: Path) -> DownloadResult:
        """Download paper PDF."""
        import arxiv

        clean_id = extract_arxiv_id(arxiv_id) or arxiv_id
        try:
            search = arxiv.Search(id_list=[clean_id])
            results = await self._results(search)
            if not results:
                return DownloadResult(
                    success=False, error=f"Paper {clean_id} not found on arXiv"
                )

            result = results[0]
            filename = f"{clean_id.replace('/', '_')}.pdf"
            dest_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                result.download_pdf, dirpath=str(dest_dir), filename=filename
            )
            path = dest_dir / filename
            paper = self._to_paper(result)
            return DownloadResult(
                success=True, path=path, source=PaperSource.ARXIV, paper=paper
            )
        except Exception as e:
            logger.error(f"arXiv download failed for {clean_id}: {e}")
            return DownloadResult(success=False, error=str(e))

    def _to_paper(self, result) -> Paper:
        """Convert arxiv.Result to Paper."""
        return Paper(
            title=result.title,
            authors=[a.name for a in result.authors],
            abstract=result.summary,
            doi=result.doi,
            arxiv_id=result.entry_id.split("/")[-1],
            url=result.entry_id,
            pdf_url=result.pdf_url,
            source=PaperSource.ARXIV,
            access_status=AccessStatus.OPEN_ACCESS,
            year=result.published.year if result.published else None,
        )
