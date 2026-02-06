"""Shared data types for academic paper tools."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class PaperSource(Enum):
    ARXIV = "arxiv"
    UNPAYWALL = "unpaywall"
    SEMANTIC_SCHOLAR = "semantic_scholar"


class AccessStatus(Enum):
    OPEN_ACCESS = "open_access"
    PAYWALLED = "paywalled"
    UNKNOWN = "unknown"


@dataclass
class Paper:
    """Unified paper representation across all sources."""

    title: str
    authors: List[str]
    url: str
    source: PaperSource
    access_status: AccessStatus
    abstract: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pdf_url: Optional[str] = None
    citation_count: Optional[int] = None
    year: Optional[int] = None
    venue: Optional[str] = None

    def format(self, index: Optional[int] = None) -> str:
        """Format paper for display."""
        prefix = f"{index}. " if index is not None else ""
        lines = [f"{prefix}{self.title}"]
        if self.authors:
            lines.append(f"   Authors: {', '.join(self.authors[:5])}")
            if len(self.authors) > 5:
                lines[-1] += f" (+{len(self.authors) - 5} more)"
        if self.year:
            lines.append(f"   Year: {self.year}")
        if self.venue:
            lines.append(f"   Venue: {self.venue}")
        if self.doi:
            lines.append(f"   DOI: {self.doi}")
        if self.arxiv_id:
            lines.append(f"   arXiv: {self.arxiv_id}")
        if self.citation_count is not None:
            lines.append(f"   Citations: {self.citation_count}")
        lines.append(f"   Access: {self.access_status.value}")
        if self.pdf_url:
            lines.append(f"   PDF: {self.pdf_url}")
        lines.append(f"   URL: {self.url}")
        return "\n".join(lines)


@dataclass
class DownloadResult:
    """Result of a paper download attempt."""

    success: bool
    path: Optional[Path] = None
    source: Optional[PaperSource] = None
    error: Optional[str] = None
    paper: Optional[Paper] = None
