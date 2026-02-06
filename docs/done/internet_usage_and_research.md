# Internet Usage and Research Capabilities

This document explores solutions for enabling the agent to perform academic research, download papers, and interact with websites. It covers the technical landscape, limitations, and recommended implementation strategies.

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [The Reality of Open Access](#the-reality-of-open-access)
3. [Academic Paper APIs](#academic-paper-apis)
4. [Browser Automation](#browser-automation)
5. [How Browser Automation Works (Without Vision)](#how-browser-automation-works-without-vision)
6. [Accessing Paywalled Papers](#accessing-paywalled-papers)
7. [Recommended Implementation Strategy](#recommended-implementation-strategy)
8. [Implementation Roadmap](#implementation-roadmap)
9. [Environment Variables](#environment-variables)
10. [Security Considerations](#security-considerations)
11. [References](#references)

---

## Problem Statement

The agent currently cannot properly download PDFs from academic sources. When using `cite_web` on arXiv links, it only fetches the abstract page metadata, not the full paper content. This limits the agent's ability to:

- Verify quotes against source material
- Extract specific information from papers
- Perform proper academic research with citations

**Example failure:**
```
Tool: cite_web
URL: https://arxiv.org/abs/2408.08921
Status: FAILED
Note: The exact verbatim sentence does not appear anywhere in the provided
      source content (metadata and abstract).
```

The agent can only see the abstract page HTML, not the actual PDF content.

---

## The Reality of Open Access

### Coverage Statistics (2024)

**Only ~47% of academic papers are openly accessible.** The breakdown:

| Type | Coverage | Description |
|------|----------|-------------|
| **Gold OA** | 27.5% | Published in fully open access journals |
| **Green OA** | 16.7% | Author-archived in repositories (preprints, manuscripts) |
| **Hybrid OA** | 9.2% | Individual OA articles in subscription journals |
| **Bronze OA** | 10.6% | Free to read but no clear license |
| **Paywalled** | ~53% | Requires subscription or pay-per-article |

### What This Means

- **APIs like Unpaywall only find OA versions** - they cannot bypass paywalls
- **~53% of papers require institutional access** (university VPN, library subscription)
- **Preprints may differ from final published versions** - citations should note this

### Types of Open Access

| Type | Description | Example Sources |
|------|-------------|-----------------|
| **Gold** | Published OA from the start | PLOS ONE, Nature Communications |
| **Green** | Author self-archived after publication | Institutional repositories, PubMed Central |
| **Preprint** | Pre-peer-review versions | arXiv, bioRxiv, SSRN |

---

## Academic Paper APIs

These APIs provide structured access to academic papers. **Important:** They only index openly accessible content - they do not bypass paywalls.

### How API Request Flow Works

```
Your Machine                 Unpaywall API              Repository (e.g., PMC)
     |                            |                              |
     |-- "Where is DOI X?" ------>|                              |
     |<-- "OA copy at URL Y" -----|                              |
     |                                                           |
     |-- "GET PDF from URL Y" ---------------------------------->|
     |<-- PDF file ----------------------------------------------|
```

**Key insight:** The PDF download happens from YOUR machine directly to the repository. The API just tells you where to find free copies. This means:
- VPN status doesn't affect API lookups
- VPN DOES affect the final PDF download (if going to a publisher)

### arXiv API

- **Best for**: Computer Science, Physics, Mathematics papers
- **Coverage**: 2.4M+ papers, all openly accessible
- **Direct PDF access**: `https://arxiv.org/pdf/{paper_id}.pdf`
- **Python library**: [`arxiv`](https://pypi.org/project/arxiv/) with `Result.download_pdf()` method
- **Rate limit**: 1 request per 3 seconds
- **Documentation**: https://info.arxiv.org/help/api/basics.html

```python
import arxiv

# Search and download
search = arxiv.Search(query="GraphRAG", max_results=5)
for result in search.results():
    result.download_pdf(dirpath="./papers/")

# Download specific paper
paper = next(arxiv.Search(id_list=["2408.08921"]).results())
paper.download_pdf(filename="graphrag_survey.pdf")
```

**Note**: arXiv papers are preprints - they may differ from final journal versions.

### Unpaywall API

- **Best for**: Finding open access versions of any paper via DOI
- **Coverage**: 24+ million papers indexed from 50,000+ sources
- **Endpoint**: `https://api.unpaywall.org/v2/{DOI}?email={EMAIL}`
- **Returns**: `best_oa_location.url_for_pdf` when available
- **Python library**: [`unpywall`](https://unpywall.readthedocs.io/en/latest/api.html)
- **Documentation**: https://unpaywall.org/products/api

```python
from unpywall import Unpywall

# Get open access PDF URL
oa_info = Unpywall.get_oa_info(doi="10.1038/nature12373")
if oa_info.get("is_oa"):
    pdf_url = oa_info["best_oa_location"]["url_for_pdf"]

# Direct PDF download
Unpywall.download_pdf_file(doi="10.1038/nature12373", filename="paper.pdf")
```

**What Unpaywall finds:**
- Author manuscripts in institutional repositories
- Publisher OA versions (Gold/Hybrid)
- Preprints on arXiv, bioRxiv, etc.
- PubMed Central deposits

**What Unpaywall does NOT do:**
- Bypass paywalls
- Access subscription content
- Provide papers without legal OA copies

### CORE API

- **Best for**: Largest open access collection with full text
- **Coverage**: 300M+ metadata records, 40M+ full text articles
- **Requires**: Free API key (register at core.ac.uk)
- **Documentation**: https://core.ac.uk/services/api

```python
import requests

CORE_API_KEY = "your_api_key"
headers = {"Authorization": f"Bearer {CORE_API_KEY}"}

# Search for papers
response = requests.get(
    "https://api.core.ac.uk/v3/search/works",
    params={"q": "graph retrieval augmented generation"},
    headers=headers
)
results = response.json()["results"]
```

### Semantic Scholar API

- **Best for**: Citation graphs, paper discovery, metadata
- **Coverage**: 200M+ papers across all fields
- **Returns**: `openAccessPdf.url` field when available
- **Rate limit**: 100 requests/5 min (unauthenticated), higher with API key
- **Documentation**: https://api.semanticscholar.org/api-docs/

```python
import requests

# Get paper with citation info
response = requests.get(
    "https://api.semanticscholar.org/graph/v1/paper/2408.08921",
    params={"fields": "title,abstract,citationCount,openAccessPdf,references"}
)
paper = response.json()
if paper.get("openAccessPdf"):
    pdf_url = paper["openAccessPdf"]["url"]
```

---

## Browser Automation

For cases where APIs aren't available (paywalled content, dynamic websites, non-standard sources), browser automation allows the agent to interact with websites like a human.

### How Major AI Providers Handle This

| Provider | Solution | Approach | Availability |
|----------|----------|----------|--------------|
| **OpenAI** | [Operator / CUA](https://openai.com/index/computer-using-agent/) | GPT-4o vision + RL, screenshot-based GUI | Integrated into ChatGPT |
| **Anthropic** | [Computer Use](https://www.anthropic.com/news/3-5-models-and-computer-use) | Claude perceives/interacts via screenshots | API + Claude for Chrome |
| **Google** | [Gemini Agent](https://gemini.google/overview/agent/) | Chrome Auto Browse with safety critic | AI Ultra subscribers |
| **Groq** | Browser API | `browser.search` ($5/1k), `browser.open` ($1/1k) | API |

### Open Source Solutions

#### browser-use (Recommended)

- **Repository**: https://github.com/browser-use/browser-use
- **Stars**: 64k+ (very active community)
- **Language**: Python
- **Backend**: Playwright
- **LLM Support**: Model-agnostic (OpenAI, Anthropic, Ollama, local models)

```python
from browser_use import Agent, Browser
from langchain_openai import ChatOpenAI

# With vision model
agent = Agent(
    task="Download the PDF from arxiv.org/abs/2408.08921",
    llm=ChatOpenAI(model="gpt-4o"),
    browser=Browser()
)
result = await agent.run()

# With text-only model (uses DOM/accessibility tree)
agent = Agent(
    task="Download the PDF from arxiv.org/abs/2408.08921",
    llm=ChatOpenAI(model="gpt-4o-mini"),  # No vision needed!
    browser=Browser(),
    use_vision=False  # Rely on DOM extraction
)
result = await agent.run()
```

**Pros:**
- Native Python integration
- Works with ANY LLM (including local models)
- Supports both vision and DOM-based modes
- Full browser control
- Active development and large community

**Cons:**
- Adds Playwright dependency (~200MB)
- Requires browser process management
- More complex than API calls
- Can be slow (multiple page loads)

#### Playwright MCP (For Claude/MCP Integration)

- **Official**: https://github.com/microsoft/playwright-mcp
- **Community**: https://github.com/executeautomation/mcp-playwright
- Uses accessibility snapshots (no vision model needed)
- Works with Claude Code via MCP protocol

```bash
# Install official version
npm install -g @modelcontextprotocol/server-playwright

# Or community version with more features
npm install -g @executeautomation/playwright-mcp-server
```

**MCP Configuration** (`.mcp.json`):
```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-playwright"]
    }
  }
}
```

#### Skyvern

- **Repository**: https://github.com/Skyvern-AI/skyvern
- Computer vision + LLM approach
- Handles complex multi-step workflows
- Cloud-hosted option available

### Groq Browser API (Managed Service)

Groq offers browser tools for their hosted models:

| Tool | Cost | Description |
|------|------|-------------|
| `browser.search` | $5/1000 requests | Web search |
| `browser.open` | $1/1000 requests | Visit and interact with websites |

**Pros:**
- No infrastructure to manage
- Simple API integration
- Fast (Groq's inference speed)

**Cons:**
- Vendor lock-in
- Ongoing costs
- Limited to Groq models

---

## How Browser Automation Works (Without Vision)

A key insight: **text-only models can do browser automation without seeing screenshots**. They use the browser's accessibility tree instead.

### The Accessibility Tree Approach

The accessibility tree is the same structured data that screen readers use for visually impaired users. It contains semantic information about page elements.

```
┌─────────────────────────────────────────────────────────────┐
│  What the browser shows (visual):                           │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  [Email: ___________]  [Password: ___________]       │   │
│  │                        [✓ Remember me]               │   │
│  │              [ Sign In ]    [ Forgot Password? ]     │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  What the LLM receives (accessibility snapshot):            │
│                                                             │
│  [1] textbox "Email" focused                                │
│  [2] textbox "Password"                                     │
│  [3] checkbox "Remember me" unchecked                       │
│  [4] button "Sign In"                                       │
│  [5] link "Forgot Password?"                                │
└─────────────────────────────────────────────────────────────┘
```

The LLM responds with actions like:
- `type [1] "user@example.com"`
- `type [2] "password123"`
- `click [4]`

### Technical Implementation

**Playwright MCP** calls `page.accessibility.snapshot()`:

```json
{
  "role": "button",
  "name": "Download PDF",
  "ref": "e47",
  "focused": false,
  "disabled": false
}
```

**browser-use** indexes DOM elements:

```python
# Internal representation sent to LLM
elements = [
    {"index": 1, "type": "input", "name": "email", "value": ""},
    {"index": 2, "type": "input", "name": "password", "value": ""},
    {"index": 3, "type": "button", "text": "Sign In"},
    {"index": 4, "type": "link", "text": "Download PDF", "href": "/paper.pdf"}
]
```

### Vision vs DOM-Based Comparison

| Aspect | Vision (Screenshots) | DOM/Accessibility |
|--------|---------------------|-------------------|
| **Models** | GPT-4o, Claude, Gemini | Any LLM (even local) |
| **Cost** | Higher (image tokens) | Lower (text only) |
| **Speed** | Slower (image encoding) | Faster |
| **Reliability** | Can handle any UI | Fails on canvas/images |
| **Context size** | Large (images) | Small (~93% savings) |

### When DOM-Based Fails

The accessibility approach doesn't work when:
- Content rendered in `<canvas>` (games, some charts)
- Important information only in images
- Sites using custom components without ARIA labels
- CAPTCHAs (obviously)

For these cases, vision models are required.

---

## Accessing Paywalled Papers

### The Challenge

~53% of academic papers are behind paywalls. The paper APIs (Unpaywall, CORE, etc.) only find legally free versions - they cannot bypass paywalls.

### Options for Paywalled Content

| Option | How It Works | Pros | Cons |
|--------|--------------|------|------|
| **University VPN** | Route requests through institutional network | Access full catalog | Requires affiliation |
| **Interlibrary Loan** | Request through library | Legal, reliable | Slow (days) |
| **Email Author** | Authors can legally share their work | Often works | Manual, slow |
| **Preprint Servers** | Find pre-publication version | Fast, free | May differ from final |
| **ResearchGate** | Authors upload PDFs | Large collection | Gray area legally |

### VPN + Browser Automation

If running on a university network (VPN), browser automation can access paywalled content:

```
Your Machine (on VPN)         Publisher (Elsevier, Springer, etc.)
     |                                    |
     |-- Request via university IP ------>|
     |<-- "You have access" + PDF --------|
```

**Why this works:**
1. Your request originates from your machine
2. VPN routes it through university network
3. Publisher sees university's IP range
4. Access granted based on institutional subscription

**Why you need browser automation (not just HTTP requests):**
Publishers don't expose simple `GET /paper.pdf` endpoints. They have:
- JavaScript-heavy pages
- Session cookies and redirects
- "Click to download" buttons
- CAPTCHA challenges (sometimes)

Browser automation handles all of this.

### Recommended Download Flow

```python
async def download_paper(doi: str, use_browser: bool = False) -> Path:
    """
    Download paper with fallback chain.

    1. Try open access APIs first (no VPN needed)
    2. Fall back to browser automation (works with VPN for paywalled)
    """
    # Step 1: Try arXiv (if it's an arXiv paper)
    if arxiv_id := extract_arxiv_id(doi):
        return await download_from_arxiv(arxiv_id)

    # Step 2: Try Unpaywall for OA version
    oa_url = await unpaywall.get_pdf_url(doi)
    if oa_url:
        return await download_direct(oa_url)

    # Step 3: Try CORE
    core_url = await core.get_full_text_url(doi)
    if core_url:
        return await download_direct(core_url)

    # Step 4: Fall back to browser automation (requires VPN for paywalled)
    if use_browser:
        publisher_url = await resolve_doi(doi)  # doi.org redirect
        return await browser_agent.download_from_publisher(publisher_url)

    # Step 5: No access available
    raise PaperNotAccessibleError(
        f"Paper {doi} requires institutional access. "
        "Try connecting to university VPN or contact the author directly."
    )
```

---

## Recommended Implementation Strategy

### Phase 1: Academic Paper APIs (Simple, Reliable)

Add tools for structured paper access - covers ~47% of papers:

```python
@tool
async def search_papers(
    query: str,
    source: str = "semantic_scholar",
    max_results: int = 10
) -> list[Paper]:
    """
    Search for academic papers.

    Args:
        query: Search query (title, keywords, author)
        source: "semantic_scholar", "arxiv", or "core"
        max_results: Maximum papers to return

    Returns:
        List of Paper objects with metadata and OA status
    """
    pass

@tool
async def download_paper(
    identifier: str,
    identifier_type: str = "doi"
) -> Path:
    """
    Download paper PDF using fallback chain.

    Tries: arXiv -> Unpaywall -> CORE

    Args:
        identifier: DOI, arXiv ID, or URL
        identifier_type: "doi", "arxiv", or "url"

    Returns:
        Path to downloaded PDF

    Raises:
        PaperNotAccessibleError: If no OA version found
    """
    pass
```

**Implementation order:**
1. arXiv API (predictable URLs, simple, 100% success for arXiv papers)
2. Unpaywall (DOI-based lookup, broad coverage)
3. Semantic Scholar (paper discovery, citation graphs)
4. CORE (fallback for institutional repository content)

### Phase 2: Browser Automation (For Edge Cases)

Add `browser-use` for cases APIs can't handle:

```python
@tool
async def browse_and_download(
    url: str,
    task: str,
    require_vpn: bool = False
) -> Path:
    """
    Use browser automation to navigate and download files.

    Args:
        url: Starting URL
        task: Natural language description of what to do
        require_vpn: If True, warn user if not on institutional network

    Returns:
        Path to downloaded file
    """
    pass
```

**Use cases:**
- Papers behind publisher paywalls (with VPN)
- Conference proceedings not in standard databases
- Supplementary materials and datasets
- Non-standard document formats

### Phase 3: Smart Research Agent

Combine everything into a research workflow:

```python
@tool
async def research_topic(
    topic: str,
    num_papers: int = 10,
    download: bool = True
) -> ResearchReport:
    """
    Comprehensive literature search on a topic.

    1. Search multiple databases
    2. Rank by relevance and citations
    3. Download available papers
    4. Extract key information
    5. Generate summary report
    """
    pass
```

---

## Implementation Roadmap

This roadmap extends the existing `src/tools/research/` package with academic paper and browser automation capabilities.

### Current State

```
src/tools/research/
├── __init__.py          # create_research_tools(), get_research_metadata()
└── web.py               # web_search (Tavily)
```

**Existing tool**: `web_search` - Tavily-based web search with citation source registration

### Target State

```
src/tools/research/
├── __init__.py          # Updated to include all research tools
├── web.py               # web_search (Tavily) - existing
├── papers.py            # NEW: Academic paper tools
├── browser.py           # NEW: Browser automation tools
└── utils/
    ├── __init__.py
    ├── arxiv_client.py      # arXiv API wrapper
    ├── unpaywall_client.py  # Unpaywall API wrapper
    ├── semantic_scholar.py  # Semantic Scholar API wrapper
    └── paper_types.py       # Shared data types (Paper, SearchResult, etc.)
```

### Sprint 1: Academic Paper APIs (1-2 weeks)

**Goal**: Enable agent to search and download open access papers.

#### 1.1 Core Infrastructure

**File**: `src/tools/research/utils/paper_types.py`
```python
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum
from pathlib import Path

class PaperSource(Enum):
    ARXIV = "arxiv"
    UNPAYWALL = "unpaywall"
    CORE = "core"
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
    abstract: Optional[str]
    doi: Optional[str]
    arxiv_id: Optional[str]
    url: str
    pdf_url: Optional[str]
    source: PaperSource
    access_status: AccessStatus
    citation_count: Optional[int] = None
    year: Optional[int] = None
    venue: Optional[str] = None

@dataclass
class DownloadResult:
    """Result of a paper download attempt."""
    success: bool
    path: Optional[Path]
    source: Optional[PaperSource]
    error: Optional[str] = None
```

**File**: `src/tools/research/utils/arxiv_client.py`
```python
import arxiv
import asyncio
from pathlib import Path
from typing import List, Optional
from .paper_types import Paper, PaperSource, AccessStatus, DownloadResult

class ArxivClient:
    """Async wrapper for arXiv API."""

    def __init__(self, rate_limit_seconds: float = 3.0):
        self.rate_limit = rate_limit_seconds
        self._last_request = 0

    async def search(self, query: str, max_results: int = 10) -> List[Paper]:
        """Search arXiv for papers."""
        # Rate limiting
        await self._rate_limit()

        # Run sync arxiv library in thread pool
        search = arxiv.Search(query=query, max_results=max_results)
        results = await asyncio.to_thread(list, search.results())

        return [self._to_paper(r) for r in results]

    async def get_paper(self, arxiv_id: str) -> Optional[Paper]:
        """Get a specific paper by arXiv ID."""
        await self._rate_limit()
        search = arxiv.Search(id_list=[arxiv_id])
        results = await asyncio.to_thread(list, search.results())
        return self._to_paper(results[0]) if results else None

    async def download(self, arxiv_id: str, dest_dir: Path) -> DownloadResult:
        """Download paper PDF."""
        await self._rate_limit()
        try:
            search = arxiv.Search(id_list=[arxiv_id])
            results = await asyncio.to_thread(list, search.results())
            if not results:
                return DownloadResult(False, None, None, f"Paper {arxiv_id} not found")

            paper = results[0]
            filename = f"{arxiv_id.replace('/', '_')}.pdf"
            path = dest_dir / filename
            await asyncio.to_thread(paper.download_pdf, dirpath=str(dest_dir), filename=filename)
            return DownloadResult(True, path, PaperSource.ARXIV)
        except Exception as e:
            return DownloadResult(False, None, None, str(e))

    def _to_paper(self, result: arxiv.Result) -> Paper:
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

    async def _rate_limit(self):
        import time
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < self.rate_limit:
            await asyncio.sleep(self.rate_limit - elapsed)
        self._last_request = time.time()
```

**File**: `src/tools/research/utils/unpaywall_client.py`
```python
import aiohttp
import os
from typing import Optional
from .paper_types import Paper, PaperSource, AccessStatus

class UnpaywallClient:
    """Async Unpaywall API client."""

    BASE_URL = "https://api.unpaywall.org/v2"

    def __init__(self, email: Optional[str] = None):
        self.email = email or os.getenv("UNPAYWALL_EMAIL")
        if not self.email:
            raise ValueError("UNPAYWALL_EMAIL required")

    async def get_paper(self, doi: str) -> Optional[Paper]:
        """Look up open access info for a DOI."""
        url = f"{self.BASE_URL}/{doi}?email={self.email}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()

        return self._to_paper(data)

    def _to_paper(self, data: dict) -> Paper:
        best_oa = data.get("best_oa_location") or {}
        return Paper(
            title=data.get("title", "Unknown"),
            authors=[a.get("given", "") + " " + a.get("family", "")
                     for a in data.get("z_authors", [])],
            abstract=None,  # Unpaywall doesn't provide abstracts
            doi=data.get("doi"),
            arxiv_id=None,
            url=data.get("doi_url", ""),
            pdf_url=best_oa.get("url_for_pdf"),
            source=PaperSource.UNPAYWALL,
            access_status=AccessStatus.OPEN_ACCESS if data.get("is_oa") else AccessStatus.PAYWALLED,
            year=data.get("year"),
        )
```

#### 1.2 Paper Tools

**File**: `src/tools/research/papers.py`
```python
"""Academic paper search and download tools."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool
from ..context import ToolContext

logger = logging.getLogger(__name__)

PAPER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "search_papers": {
        "module": "research.papers",
        "function": "search_papers",
        "description": "Search academic databases for papers",
        "category": "research",
        "phases": ["tactical"],
        "short_description": "Search arXiv, Semantic Scholar for academic papers.",
    },
    "download_paper": {
        "module": "research.papers",
        "function": "download_paper",
        "description": "Download paper PDF to workspace",
        "category": "research",
        "phases": ["tactical"],
        "short_description": "Download paper PDF using arXiv/Unpaywall fallback chain.",
    },
    "get_paper_info": {
        "module": "research.papers",
        "function": "get_paper_info",
        "description": "Get metadata and citation info for a paper",
        "category": "research",
        "phases": ["tactical"],
        "short_description": "Get paper metadata, abstract, and citations.",
    },
}

def create_paper_tools(context: ToolContext) -> List[Any]:
    """Create academic paper tools."""

    @tool
    async def search_papers(
        query: str,
        source: str = "arxiv",
        max_results: int = 10
    ) -> str:
        """Search for academic papers.

        Args:
            query: Search query (keywords, title, author)
            source: Database to search ("arxiv", "semantic_scholar")
            max_results: Maximum results (default 10)

        Returns:
            Formatted list of papers with metadata and access status
        """
        # Implementation uses clients from utils/
        pass

    @tool
    async def download_paper(
        identifier: str,
        identifier_type: str = "auto"
    ) -> str:
        """Download paper PDF to workspace documents folder.

        Uses fallback chain: arXiv -> Unpaywall -> CORE

        Args:
            identifier: DOI, arXiv ID, or URL
            identifier_type: "doi", "arxiv", "url", or "auto" (detect)

        Returns:
            Path to downloaded PDF or error message
        """
        # Downloads to context.workspace_manager.documents_dir
        pass

    @tool
    async def get_paper_info(doi_or_arxiv_id: str) -> str:
        """Get detailed paper information.

        Args:
            doi_or_arxiv_id: Paper identifier

        Returns:
            Paper metadata including abstract, authors, citations, access status
        """
        pass

    return [search_papers, download_paper, get_paper_info]
```

#### 1.3 Update Research Package

**File**: `src/tools/research/__init__.py` (updated)
```python
"""Research toolkit - web search, academic papers, browser automation."""

from typing import Any, Dict, List
from ..context import ToolContext

def create_research_tools(context: ToolContext) -> List[Any]:
    """Create all research tools with injected context."""
    from .web import create_web_tools
    from .papers import create_paper_tools

    tools = []
    tools.extend(create_web_tools(context))
    tools.extend(create_paper_tools(context))
    return tools

def get_research_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all research tools."""
    from .web import RESEARCH_TOOLS_METADATA
    from .papers import PAPER_TOOLS_METADATA

    metadata = {}
    metadata.update(RESEARCH_TOOLS_METADATA)
    metadata.update(PAPER_TOOLS_METADATA)
    return metadata
```

#### 1.4 Dependencies

Add to `requirements.txt`:
```
arxiv>=2.1.0
aiohttp>=3.9.0
```

#### 1.5 Config Updates

Add to `config/defaults.yaml`:
```yaml
tools:
  research:
    - web_search
    - search_papers      # NEW
    - download_paper     # NEW
    - get_paper_info     # NEW
```

---

### Sprint 2: Browser Automation (2-3 weeks)

**Goal**: Enable agent to interact with websites for paywalled content and complex downloads.

#### 2.1 Browser Tools

**File**: `src/tools/research/browser.py`
```python
"""Browser automation tools using browser-use."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool
from ..context import ToolContext

logger = logging.getLogger(__name__)

BROWSER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "browse_website": {
        "module": "research.browser",
        "function": "browse_website",
        "description": "Navigate website and extract information",
        "category": "research",
        "phases": ["tactical"],
        "short_description": "Use browser to navigate and extract web content.",
    },
    "download_from_website": {
        "module": "research.browser",
        "function": "download_from_website",
        "description": "Download file from website using browser automation",
        "category": "research",
        "phases": ["tactical"],
        "short_description": "Navigate to URL and download file (PDF, etc.).",
    },
}

def create_browser_tools(context: ToolContext) -> List[Any]:
    """Create browser automation tools."""

    @tool
    async def browse_website(
        url: str,
        task: str,
        use_vision: bool = False
    ) -> str:
        """Navigate a website and complete a task.

        Uses browser automation to interact with the page.
        Default mode uses DOM/accessibility tree (works with any LLM).
        Vision mode uses screenshots (requires multimodal LLM).

        Args:
            url: Starting URL
            task: Natural language description of what to do
            use_vision: Use screenshot-based navigation (default False)

        Returns:
            Extracted information or task completion status
        """
        from browser_use import Agent, Browser
        # Get LLM from context or config
        # Run agent with task
        pass

    @tool
    async def download_from_website(
        url: str,
        download_task: str = "Find and click the download PDF button"
    ) -> str:
        """Download a file from a website using browser automation.

        Useful for:
        - Publisher pages with JavaScript download buttons
        - Pages requiring cookie acceptance
        - Complex navigation to reach download links

        Note: For paywalled content, ensure you're connected to
        institutional VPN before using this tool.

        Args:
            url: Page URL containing the download
            download_task: Instructions for finding the download

        Returns:
            Path to downloaded file or error message
        """
        pass

    return [browse_website, download_from_website]
```

#### 2.2 Browser Configuration

Add to `config/defaults.yaml`:
```yaml
browser:
  headless: true
  timeout: 60000  # 60 seconds
  use_vision: false  # DOM-based by default
  downloads_dir: null  # Use workspace documents dir
```

#### 2.3 VPN/Proxy Configuration

For accessing paywalled content through institutional networks, we support routing specific tools through a proxy or SOCKS connection.

**Concept**: Different tools can have different network routes:
- Open access APIs (arXiv, Unpaywall) → Direct connection
- Publisher downloads → Through university proxy/VPN
- Browser automation → Through proxy for paywalled content

**File**: `src/tools/research/utils/network.py`
```python
"""Network configuration for research tools."""

import os
from dataclasses import dataclass
from typing import Optional
from enum import Enum

class ProxyType(Enum):
    NONE = "none"
    HTTP = "http"
    SOCKS5 = "socks5"

@dataclass
class ProxyConfig:
    """Proxy configuration for routing requests."""
    type: ProxyType = ProxyType.NONE
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def url(self) -> Optional[str]:
        if self.type == ProxyType.NONE:
            return None
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.type.value}://{auth}{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        """Load proxy config from environment variables."""
        proxy_type = os.getenv("RESEARCH_PROXY_TYPE", "none")
        if proxy_type == "none":
            return cls()

        return cls(
            type=ProxyType(proxy_type),
            host=os.getenv("RESEARCH_PROXY_HOST"),
            port=int(os.getenv("RESEARCH_PROXY_PORT", "0")),
            username=os.getenv("RESEARCH_PROXY_USER"),
            password=os.getenv("RESEARCH_PROXY_PASS"),
        )

def get_aiohttp_session(proxy_config: Optional[ProxyConfig] = None):
    """Create aiohttp session with optional proxy."""
    import aiohttp

    if proxy_config and proxy_config.url:
        connector = aiohttp.TCPConnector()
        return aiohttp.ClientSession(
            connector=connector,
            trust_env=False,  # Don't use system proxy
        ), proxy_config.url
    return aiohttp.ClientSession(), None

async def check_institutional_access() -> bool:
    """Check if we have institutional access (e.g., connected to university VPN).

    Tests by checking if we can resolve a known paywalled resource.
    """
    import aiohttp

    # Test URLs that return different responses based on institutional access
    test_urls = [
        # These would be configured per-institution
        # "https://www.sciencedirect.com/user/institution",
    ]

    # Simple heuristic: check if common academic proxy headers are present
    # or if IP is in known institutional ranges
    # For now, rely on user configuration
    return os.getenv("INSTITUTIONAL_ACCESS", "false").lower() == "true"
```

**Config Schema** (`config/defaults.yaml`):
```yaml
research:
  # Proxy for accessing paywalled content
  proxy:
    enabled: false
    type: "socks5"  # "http", "socks5", or "none"
    host: null      # e.g., "localhost" for SSH tunnel
    port: null      # e.g., 1080 for SOCKS
    # Credentials (optional, prefer env vars)
    # username: null
    # password: null

  # Which operations should use the proxy
  proxy_routes:
    paper_download: true      # Route paper downloads through proxy
    browser_automation: true  # Route browser through proxy
    api_requests: false       # Keep API requests direct (Unpaywall, etc.)
```

**Environment Variables**:
```bash
# Proxy configuration
RESEARCH_PROXY_TYPE=socks5          # "http", "socks5", or "none"
RESEARCH_PROXY_HOST=localhost       # Proxy host
RESEARCH_PROXY_PORT=1080            # Proxy port
RESEARCH_PROXY_USER=                # Optional auth
RESEARCH_PROXY_PASS=                # Optional auth

# Institutional access flag
INSTITUTIONAL_ACCESS=true           # Set when connected to university VPN
```

**Usage with SSH Tunnel** (common for university access):
```bash
# Set up SOCKS proxy through university jump host
ssh -D 1080 -N username@university-server.edu &

# Configure agent
export RESEARCH_PROXY_TYPE=socks5
export RESEARCH_PROXY_HOST=localhost
export RESEARCH_PROXY_PORT=1080
export INSTITUTIONAL_ACCESS=true

# Run agent - browser and downloads will route through university
python agent.py --config doc_writer --description "Research GraphRAG papers"
```

**Playwright Proxy Support**:
```python
# In browser.py - configure Playwright with proxy
from playwright.async_api import async_playwright

async def create_browser_with_proxy(proxy_config: ProxyConfig):
    """Create Playwright browser with proxy configuration."""
    playwright = await async_playwright().start()

    launch_options = {
        "headless": True,
    }

    if proxy_config and proxy_config.url:
        launch_options["proxy"] = {
            "server": proxy_config.url,
        }
        if proxy_config.username:
            launch_options["proxy"]["username"] = proxy_config.username
            launch_options["proxy"]["password"] = proxy_config.password

    browser = await playwright.chromium.launch(**launch_options)
    return browser
```

**Tool Integration** - tools can check/use proxy:
```python
@tool
async def download_paper(
    identifier: str,
    use_proxy: bool = True  # Use configured proxy for paywalled content
) -> str:
    """Download paper with optional proxy routing.

    Args:
        identifier: DOI, arXiv ID, or URL
        use_proxy: Route through institutional proxy if configured

    When use_proxy=True and proxy is configured:
    - Open access sources (arXiv) → Direct connection
    - Paywalled publishers → Through proxy
    """
    proxy = ProxyConfig.from_env() if use_proxy else None

    # Try open access first (no proxy needed)
    result = await try_open_access_download(identifier)
    if result.success:
        return result

    # Fall back to publisher with proxy
    if proxy and proxy.type != ProxyType.NONE:
        return await download_from_publisher(identifier, proxy=proxy)
    else:
        return "Paper is paywalled. Configure RESEARCH_PROXY_* or connect to institutional VPN."
```

#### 2.4 Dependencies

Add to `requirements.txt`:
```
browser-use>=0.2.0
playwright>=1.40.0
```

Add post-install step:
```bash
playwright install chromium
```

---

### Sprint 3: Integration & Smart Research (1-2 weeks)

**Goal**: Combine all tools into cohesive research workflows.

#### 3.1 Unified Download Tool

Combine paper APIs and browser into single smart tool:

```python
@tool
async def download_paper(
    identifier: str,
    use_browser_fallback: bool = True
) -> str:
    """Download paper using all available methods.

    Fallback chain:
    1. arXiv (if arXiv ID)
    2. Unpaywall (if DOI, finds OA copies)
    3. CORE (institutional repositories)
    4. Browser automation (if enabled, requires VPN for paywalled)

    Args:
        identifier: DOI, arXiv ID, or URL
        use_browser_fallback: Try browser automation if APIs fail

    Returns:
        Path to downloaded PDF or detailed error with suggestions
    """
    pass
```

#### 3.2 Research Workflow Tool

```python
@tool
async def research_topic(
    topic: str,
    num_papers: int = 10,
    download_available: bool = True,
    summarize: bool = True
) -> str:
    """Comprehensive literature search.

    1. Search multiple databases (arXiv, Semantic Scholar)
    2. Deduplicate and rank by relevance/citations
    3. Check open access availability
    4. Download available papers
    5. Optionally generate summary report

    Args:
        topic: Research topic or question
        num_papers: Target number of papers
        download_available: Download OA papers to workspace
        summarize: Generate summary of findings

    Returns:
        Research report with paper list, downloads, and summary
    """
    pass
```

#### 3.3 Citation Integration

Connect paper tools with existing citation system:

```python
# In download_paper, after successful download:
if context.has_citation_manager():
    source_id = context.register_document_source(
        path=downloaded_path,
        metadata={
            "doi": paper.doi,
            "title": paper.title,
            "authors": paper.authors,
        }
    )
    # Paper is now citable with cite_document(text, source_id)
```

---

### Sprint 4: Testing & Documentation (1 week)

#### 4.1 Test Suite

```
tests/tools/research/
├── test_arxiv_client.py
├── test_unpaywall_client.py
├── test_paper_tools.py
├── test_browser_tools.py
└── conftest.py  # Fixtures, mocks
```

Key test scenarios:
- [ ] arXiv search returns valid papers
- [ ] arXiv download saves PDF correctly
- [ ] Unpaywall finds OA for known DOI
- [ ] Unpaywall returns paywalled status correctly
- [ ] Fallback chain tries all sources
- [ ] Browser tool handles simple page navigation
- [ ] Rate limiting works correctly
- [ ] Errors are handled gracefully

#### 4.2 Documentation

- [ ] Update `config/README.md` with new tools
- [ ] Add tool descriptions to `config/defaults.yaml`
- [ ] Generate tool docs in workspace `tools/` folder
- [ ] Add examples to this document

---

### Milestone Summary

| Sprint | Duration | Deliverables |
|--------|----------|--------------|
| **Sprint 1** | 1-2 weeks | `search_papers`, `download_paper`, `get_paper_info` tools with arXiv + Unpaywall |
| **Sprint 2** | 2-3 weeks | `browse_website`, `download_from_website` tools with browser-use |
| **Sprint 3** | 1-2 weeks | Unified download with fallback chain, `research_topic` workflow |
| **Sprint 4** | 1 week | Tests, documentation, integration with citation system |

**Total estimated time**: 5-8 weeks

### Dependencies to Add

```txt
# requirements.txt additions
arxiv>=2.1.0
aiohttp>=3.9.0
browser-use>=0.2.0
playwright>=1.40.0
```

### Config Schema Updates

Add to `config/schema.json`:
```json
{
  "tools": {
    "research": {
      "items": {
        "enum": [
          "web_search",
          "search_papers",
          "download_paper",
          "get_paper_info",
          "browse_website",
          "download_from_website",
          "research_topic"
        ]
      }
    }
  },
  "browser": {
    "type": "object",
    "properties": {
      "headless": {"type": "boolean", "default": true},
      "timeout": {"type": "integer", "default": 60000},
      "use_vision": {"type": "boolean", "default": false}
    }
  },
  "research": {
    "type": "object",
    "properties": {
      "proxy": {
        "type": "object",
        "description": "Proxy configuration for institutional access",
        "properties": {
          "enabled": {"type": "boolean", "default": false},
          "type": {"enum": ["none", "http", "socks5"], "default": "socks5"},
          "host": {"type": "string"},
          "port": {"type": "integer"}
        }
      },
      "proxy_routes": {
        "type": "object",
        "description": "Which operations should use the proxy",
        "properties": {
          "paper_download": {"type": "boolean", "default": true},
          "browser_automation": {"type": "boolean", "default": true},
          "api_requests": {"type": "boolean", "default": false}
        }
      }
    }
  }
}
```

**Example config** (`config/academic_researcher.yaml`):
```yaml
# yaml-language-server: $schema=schema.json
$extends: defaults

agent_id: academic_researcher
display_name: Academic Research Agent

tools:
  research:
    - web_search
    - search_papers
    - download_paper
    - get_paper_info
    - browse_website

# Browser automation settings
browser:
  headless: true
  use_vision: false  # Use DOM-based (works with any LLM)

# Proxy for institutional access
research:
  proxy:
    enabled: true
    type: socks5
    host: localhost
    port: 1080
  proxy_routes:
    paper_download: true       # Route downloads through proxy
    browser_automation: true   # Route browser through proxy
    api_requests: false        # Keep API calls direct
```

---

## Environment Variables

```bash
# Paper APIs
UNPAYWALL_EMAIL=your@email.com      # Required for Unpaywall API
CORE_API_KEY=xxx                    # Optional, for CORE API
SEMANTIC_SCHOLAR_API_KEY=xxx        # Optional, for higher rate limits

# Browser automation
BROWSER_USE_HEADLESS=true           # Run without visible browser window
BROWSER_USE_MODEL=gpt-4o-mini       # LLM for browser agent
PLAYWRIGHT_BROWSERS_PATH=/path      # Custom browser install location

# Groq browser API (if using)
GROQ_API_KEY=xxx

# Proxy/VPN Configuration (for institutional access)
RESEARCH_PROXY_TYPE=socks5          # "http", "socks5", or "none"
RESEARCH_PROXY_HOST=localhost       # Proxy host (e.g., localhost for SSH tunnel)
RESEARCH_PROXY_PORT=1080            # Proxy port
RESEARCH_PROXY_USER=                # Optional: proxy authentication
RESEARCH_PROXY_PASS=                # Optional: proxy authentication
INSTITUTIONAL_ACCESS=false          # Set to "true" when connected to university VPN
```

### Quick Setup Examples

**Using SSH tunnel to university:**
```bash
# Terminal 1: Create SOCKS proxy through university
ssh -D 1080 -N username@university-gateway.edu

# Terminal 2: Configure and run agent
export RESEARCH_PROXY_TYPE=socks5
export RESEARCH_PROXY_HOST=localhost
export RESEARCH_PROXY_PORT=1080
export INSTITUTIONAL_ACCESS=true
python agent.py --description "Download papers on GraphRAG"
```

**Using HTTP proxy:**
```bash
export RESEARCH_PROXY_TYPE=http
export RESEARCH_PROXY_HOST=proxy.university.edu
export RESEARCH_PROXY_PORT=8080
export INSTITUTIONAL_ACCESS=true
```

**No proxy (open access only):**
```bash
export RESEARCH_PROXY_TYPE=none
export INSTITUTIONAL_ACCESS=false
# Agent will only download from arXiv, Unpaywall OA sources
```

---

## Security Considerations

### Rate Limiting
- **arXiv**: 1 request per 3 seconds
- **Unpaywall**: No hard limit, but be respectful
- **Semantic Scholar**: 100 req/5 min (unauth), 1000/5 min (auth)
- **CORE**: Varies by API key tier

### Legal Considerations
- Only download papers you have rights to access
- Unpaywall/CORE only provide legally free versions
- VPN access is subject to your institution's policies
- Respect publisher terms of service

### Browser Automation Risks
- Can trigger CAPTCHAs and bot detection
- May violate some sites' ToS
- IP could get temporarily blocked
- Don't use for bulk scraping

### Data Privacy
- API requests may be logged by providers
- Browser automation leaves browser history
- Consider data retention requirements

---

## References

### Paper APIs
- [arXiv API Documentation](https://info.arxiv.org/help/api/basics.html)
- [arXiv Python Library](https://pypi.org/project/arxiv/)
- [Unpaywall API](https://unpaywall.org/products/api)
- [unpywall Python Library](https://unpywall.readthedocs.io/)
- [CORE API](https://core.ac.uk/services/api)
- [Semantic Scholar API](https://api.semanticscholar.org/api-docs/)

### Browser Automation
- [browser-use GitHub](https://github.com/browser-use/browser-use)
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [Skyvern](https://github.com/Skyvern-AI/skyvern)

### AI Provider Solutions
- [OpenAI Operator / CUA](https://openai.com/index/computer-using-agent/)
- [OpenAI Deep Research](https://openai.com/index/introducing-deep-research/)
- [Anthropic Computer Use](https://www.anthropic.com/news/3-5-models-and-computer-use)
- [Google Gemini Agent](https://gemini.google/overview/agent/)

### Open Access Statistics
- [Open Access Publishing Statistics - WordsRated](https://wordsrated.com/open-access-publishing-statistics/)
- [STM OA Dashboard 2024](https://stm-assoc.org/oa-dashboard/oa-dashboard-2024/uptake-of-open-access/)

### Background Reading
- [Green vs Gold Open Access - Penn State](https://guides.libraries.psu.edu/open-access/green)
- [Unpaywall Explained - California Digital Library](https://cdlib.org/cdlinfo/2018/07/24/so-what-is-unpaywall-anyway/)
