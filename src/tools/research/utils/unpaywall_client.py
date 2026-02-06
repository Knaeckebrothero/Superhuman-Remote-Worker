"""Async Unpaywall API client for finding open access papers."""

import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

from .paper_types import AccessStatus, DownloadResult, Paper, PaperSource

logger = logging.getLogger(__name__)


class UnpaywallClient:
    """Async Unpaywall API client."""

    BASE_URL = "https://api.unpaywall.org/v2"

    def __init__(self, email: Optional[str] = None):
        self.email = email or os.getenv("UNPAYWALL_EMAIL")

    def is_configured(self) -> bool:
        """Check if the client has required configuration."""
        return bool(self.email)

    async def get_paper(self, doi: str) -> Optional[Paper]:
        """Look up open access info for a DOI.

        Args:
            doi: The DOI to look up (e.g., "10.1038/nature12373")

        Returns:
            Paper with OA info, or None if DOI not found
        """
        if not self.email:
            logger.warning("UNPAYWALL_EMAIL not configured, skipping Unpaywall lookup")
            return None

        url = f"{self.BASE_URL}/{doi}?email={self.email}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status == 422:
                        logger.warning(f"Unpaywall: invalid DOI format: {doi}")
                        return None
                    resp.raise_for_status()
                    data = await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"Unpaywall API error for {doi}: {e}")
            return None

        return self._to_paper(data)

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """Get the best OA PDF URL for a DOI.

        Returns:
            PDF URL if an OA version exists, None otherwise
        """
        paper = await self.get_paper(doi)
        if paper and paper.pdf_url:
            return paper.pdf_url
        return None

    async def download(self, doi: str, dest_dir: Path) -> DownloadResult:
        """Download the OA PDF for a DOI.

        Args:
            doi: The DOI to download
            dest_dir: Directory to save the PDF

        Returns:
            DownloadResult with path or error
        """
        paper = await self.get_paper(doi)
        if not paper:
            return DownloadResult(
                success=False, error=f"DOI {doi} not found in Unpaywall"
            )

        if not paper.pdf_url:
            status = paper.access_status.value
            return DownloadResult(
                success=False,
                error=f"No OA PDF available for {doi} (status: {status})",
                paper=paper,
            )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    paper.pdf_url, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    resp.raise_for_status()
                    content = await resp.read()

            # Create filename from DOI
            safe_doi = doi.replace("/", "_").replace(".", "_")
            filename = f"{safe_doi}.pdf"
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / filename
            path.write_bytes(content)

            return DownloadResult(
                success=True, path=path, source=PaperSource.UNPAYWALL, paper=paper
            )
        except Exception as e:
            logger.error(f"Unpaywall download failed for {doi}: {e}")
            return DownloadResult(success=False, error=str(e), paper=paper)

    def _to_paper(self, data: dict) -> Paper:
        """Convert Unpaywall API response to Paper."""
        best_oa = data.get("best_oa_location") or {}
        authors = []
        for a in data.get("z_authors") or []:
            given = a.get("given", "")
            family = a.get("family", "")
            name = f"{given} {family}".strip()
            if name:
                authors.append(name)

        return Paper(
            title=data.get("title") or "Unknown",
            authors=authors,
            doi=data.get("doi"),
            url=data.get("doi_url") or "",
            pdf_url=best_oa.get("url_for_pdf"),
            source=PaperSource.UNPAYWALL,
            access_status=(
                AccessStatus.OPEN_ACCESS
                if data.get("is_oa")
                else AccessStatus.PAYWALLED
            ),
            year=data.get("year"),
        )
