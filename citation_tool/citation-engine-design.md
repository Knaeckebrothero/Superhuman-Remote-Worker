# Citation & Provenance Engine
## Design Document v0.3

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Overview](#2-solution-overview)
3. [Data Model](#3-data-model)
4. [The Citation Tool Interface](#4-the-citation-tool-interface)
5. [Verification Strategy](#5-verification-strategy)
6. [Web Search Integration](#6-web-search-integration)
7. [User Interface Strategy](#7-user-interface-strategy)
8. [CitationEngine Class Structure](#8-citationengine-class-structure)
9. [LangGraph Integration](#9-langgraph-integration)
10. [Advanced Citation Types](#10-advanced-citation-types)
11. [Error Handling](#11-error-handling)
12. [Session & Context Tracking](#12-session--context-tracking)
13. [Security & Access Control](#13-security--access-control)
14. [Design Decisions (Resolved)](#15-design-decisions-resolved)
15. [Next Steps](#16-next-steps)
16. [Appendices](#appendix-a-example-agent-flow)

---

## 1. Problem Statement

### 1.1 The Core Issue

AI agents performing complex, knowledge-intensive tasks suffer from a fundamental transparency problem. When an agent makes a claim—whether about legal compliance, waste disposal regulations, or synthesized research findings—users have no reliable way to verify *why* the agent believes what it says.

Current approaches fail in three critical ways:

1. **Hallucination Risk**: Agents generate plausible-sounding statements that may not be grounded in any provided source material.

2. **Traceability Gap**: Even when agents *do* use sources, the link between claim and source is often implicit, inconsistent, or lost entirely.

3. **Verification Friction**: Users who want to verify a claim must either trust the agent blindly or manually search through source documents—a process that defeats the purpose of using AI assistance.

### 1.2 The Citation Problem in Current AI Systems

Existing AI research tools (including those from major providers) produce reports with citations, but these citations are fundamentally broken:

- Citation formats are inconsistent (Harvard, IEEE, or arbitrary styles mixed together)
- Links often don't work or point to paywalled/changed content
- The cited source frequently doesn't contain the specific information claimed
- There's no way to see *which part* of a source backs up *which claim*
- Web sources change or disappear, making citations unverifiable

The result: AI-generated reports look authoritative but cannot be trusted for serious work.

### 1.3 The Use Cases

This engine must serve four distinct projects, each with specific citation needs:

| Project | Domain | Citation Requirement |
|---------|--------|---------------------|
| **Fessi** | Campus waste disposal assistant | Must cite specific regulations (Abfall-ABC entries) for safety and liability when advising on hazardous waste disposal |
| **Graph-ETL Pipeline** | Knowledge graph construction from legal PDFs | Must maintain provenance from PDF source → extracted rule → graph node to resolve conflicts and enable auditing |
| **Graph-RAG for GoBD** | Compliance checking against German accounting law | Must provide auditable trails linking business objects to exact legal paragraphs (§, Absatz, Satz level) |
| **SLR Tool** | Systematic literature review automation | Must rigorously cite source papers when synthesizing findings (e.g., "70% of papers use Architecture X" must link to those specific papers) |

---

## 2. Solution Overview

### 2.1 The Core Concept

The Citation & Provenance Engine is a **structured "show your work" interface** that agents use to register their epistemic commitments. Instead of writing ad-hoc inline citations, agents explicitly register each claim-to-source relationship through a tool call.

**The paradigm shift:**

```
OLD: Agent thinks → Agent writes prose with inline citations (ad-hoc, inconsistent)

NEW: Agent thinks → Agent makes claim → Agent registers citation (structured) 
     → Tool returns citation ID → Agent embeds ID in prose
```

### 2.2 What This Enables

1. **Forced Articulation**: The agent must explicitly state what it's claiming and why the source supports it
2. **Audit Log**: Every claim-source link is recorded, queryable, and verifiable
3. **Format Independence**: The same citation record can render as Harvard, IEEE, or a clickable link
4. **Verification Pipeline**: Citations can be automatically checked against source content
5. **User Transparency**: Readers can click any citation and see exactly what text the AI relied upon

### 2.3 Architecture Decision: Python Library with Two Operation Modes

Given that all four projects use LangGraph/Python, the implementation will be a **Python library** that agents import and use as a tool. The engine supports two operation modes:

#### Mode 1: Basic/Default (SQLite)

For single-agent projects like writing a Systematic Literature Review (SLR):

| Aspect | Description |
|--------|-------------|
| **Database** | SQLite (local file, no setup required) |
| **Verification** | Synchronous — agent waits for verification before continuing |
| **Use Case** | Single agent workflows, local development, simpler projects |
| **Setup** | Zero configuration, just instantiate the class |

In this mode, when an agent makes a citation, the tool:
1. Stores the citation
2. Triggers verification immediately
3. Returns OK or feedback explaining why the citation is invalid
4. Agent can only continue after verification completes

#### Mode 2: Multi-Agent (PostgreSQL)

For multi-agent projects where multiple agents need to read/write citations simultaneously:

| Aspect | Description |
|--------|-------------|
| **Database** | External PostgreSQL (shared across agents) |
| **Verification** | Synchronous — each agent still waits for its own citations to be verified |
| **Use Case** | Multi-agent systems, collaborative research, production deployments |
| **Setup** | Requires PostgreSQL connection string in environment |

In this mode, multiple agents can simultaneously add or read from a shared citation pool. Each agent still waits for its own citations to be verified before continuing — this ensures agents don't proceed with invalid citations and have the opportunity to self-correct.

> **Note on async verification:** Running a separate verification process in the background was considered, but rejected because agents that have already moved on (or finished) would need to be notified retroactively about citation failures. For now, synchronous verification in both modes is the pragmatic choice — the multi-agent citation pool is a good compromise for shared citation management.

The engine is implemented as a class (`CitationEngine`) that abstracts the database choice, providing the same interface regardless of which mode is used.

---

## 3. Data Model

### 3.1 Entity Hierarchy

The engine models three layers of citation data:

```
┌─────────────────────────────────────────────────────────────┐
│  CLAIM                                                      │
│  "Companies must store transaction data for 10 years."      │
│  ↓ supported_by                                             │
├─────────────────────────────────────────────────────────────┤
│  CITATION                                                   │
│  - verbatim_quote: "§ 8.1: Sämtliche buchungsrelevanten..." │
│  - quote_context: [full paragraph for reader convenience]   │
│  - relevance_reasoning: "The text explicitly mandates..."   │
│  ↓ references                                               │
├─────────────────────────────────────────────────────────────┤
│  SEGMENT                                                    │
│  Page 24, § 8.1, "Kapitel 8: Aufbewahrungsfristen"          │
│  ↓ part_of                                                  │
├─────────────────────────────────────────────────────────────┤
│  SOURCE                                                     │
│  "GoBD Regulations v2024-01"                                │
│  PDF, SHA-256: abc123..., Registered: 2024-06-01            │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Source Entity

Represents a canonical document, website, or database. Stored in a separate table and referenced by citations via ID.

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Auto-incrementing primary key |
| `type` | Enum | `document`, `website`, `database` |
| `identifier` | String | Filename, URL, or database identifier |
| `name` | String | Human-readable name |
| `version` | String | Version identifier (e.g., "2024-01") |
| `content` | Text | Full text content of the source (for verification) |
| `metadata` | JSON | Type-specific additional data |
| `created_at` | Timestamp | When the source was registered |

**Source Types:**

- **`document`**: PDFs, markdown files, legal texts, JSON, CSV, images, any text-based document
- **`website`**: Web pages (archived content stored in `content` field at registration)
- **`database`**: Database records (SQL, NoSQL, graph DBs), with metadata containing query, result description, etc.
- **`custom`**: AI-generated artifacts like matrices, plots, computed tables, or analysis outputs that the agent itself created

Sources are registered when passed to the agent (not before, not lazily on first cite). This allows tracking which sources the agent received, even if not cited. Custom sources are registered separately when the agent creates artifacts it may later need to cite.

### 3.3 Citation Entity

The core record linking a claim to its supporting evidence. Each citation is a separate record, even if the same quote backs multiple claims (since claims have unique context and reasoning).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | Integer | Yes | Auto-incrementing primary key (1, 2, 3...) |
| `claim` | Text | Yes | The assertion being supported |
| `verbatim_quote` | Text | No | Exact quoted text (for direct citations) |
| `quote_context` | Text | Yes | The paragraph/surrounding context containing the evidence |
| `quote_language` | String | No | ISO language code (e.g., "de", "en") |
| `relevance_reasoning` | Text | Conditional | Agent's explanation of why this evidence supports the claim (see 3.5) |
| `confidence` | Enum | No | `high`, `medium`, `low` — agent's self-assessment |
| `extraction_method` | Enum | No | `direct_quote`, `paraphrase`, `inference`, `aggregation` |
| `source_id` | FK | Yes | Reference to the source table |
| `locator` | JSON | Yes | Location data (page, section, etc.) |
| `verification_status` | Enum | No | `verified`, `unverified`, `failed`, `pending` |
| `verification_notes` | Text | No | Explanation from verification LLM if failed |
| `created_at` | Timestamp | Yes | When the citation was registered |
| `created_by` | String | No | Agent/session identifier for audit trails |

### 3.4 Locator Schema

The `locator` field contains location data. The schema is flexible — agents include whatever fields are relevant to locate the cited content.

**Document (PDF, markdown, legal text):**
```json
{
  "page": 24,
  "section": "§ 8.1",
  "section_header": "Kapitel 8: Aufbewahrungsfristen"
}
```

**Website:**
```json
{
  "heading_context": "Section Title",
  "accessed_at": "2024-06-01T14:30:00Z"
}
```

**Database:**
```json
{
  "query": "SELECT * FROM regulations WHERE category = 'retention'",
  "result_description": "3 records matching retention requirements",
  "table": "regulations"
}
```

The locator is stored as JSON and can contain any fields the agent finds useful for traceability. No strict schema enforcement — the agent decides what location information is relevant.

### 3.5 Relevance Reasoning Configuration

The `relevance_reasoning` field requirement is controlled via environment variable:

```
CITATION_REASONING_REQUIRED=low  # Options: none, low, medium, high
```

| Setting | Behavior |
|---------|----------|
| `none` | Never required |
| `low` | Required when `confidence: low` (default) |
| `medium` | Required when `confidence: low` or `medium` |
| `high` | Always required |

This allows flexibility while the system is being tuned.

---

## 4. The Citation Tool Interface

### 4.1 Tool Definition

The agent uses the citation tool to register claims with their supporting evidence:

```python
def citation_tool(
    # === THE CLAIM ===
    claim: str,                          # What the agent is asserting (REQUIRED)

    # === THE EVIDENCE ===
    quote_context: str,                  # Paragraph/context containing evidence (REQUIRED)
    verbatim_quote: str = None,          # Exact quoted text (optional, for direct citations)
    quote_language: str = None,          # ISO language code

    # === THE REASONING ===
    relevance_reasoning: str = None,     # Why this evidence supports the claim (see 3.5 for when required)

    # === THE SOURCE ===
    source_type: str,                    # document | website | database
    source_identifier: str,              # Filename, URL, or database identifier

    # === THE LOCATOR ===
    locator: dict,                       # Location data (flexible schema)

    # === OPTIONAL METADATA ===
    confidence: str = "high",            # high | medium | low
    extraction_method: str = "direct_quote"  # direct_quote | paraphrase | inference | aggregation
) -> CitationResult
```

### 4.2 Example Usage

**Direct citation from legal text:**
```python
citation_tool(
    claim="Companies must store transaction data for 10 years.",
    verbatim_quote="§ 8.1: Sämtliche buchungsrelevanten Transaktionsdaten sind für einen Zeitraum von zehn Jahren unveränderbar zu speichern.",
    quote_context="Kapitel 8 regelt die Aufbewahrungsfristen für verschiedene Dokumenttypen. § 8.1: Sämtliche buchungsrelevanten Transaktionsdaten sind für einen Zeitraum von zehn Jahren unveränderbar zu speichern. Dies gilt unabhängig vom verwendeten Speichermedium. § 8.2 beschreibt die Anforderungen an die Lesbarkeit...",
    relevance_reasoning="The text explicitly mandates a retention period of 'ten years' (zehn Jahren) for 'transaction data' (Transaktionsdaten), which directly supports the claim.",
    source_type="document",
    source_identifier="gobd.pdf",
    locator={
        "page": 24,
        "section": "§ 8.1",
        "section_header": "Kapitel 8: Aufbewahrungsfristen"
    },
    confidence="high",
    extraction_method="direct_quote"
)
```

**Aggregation citation from database:**
```python
citation_tool(
    claim="70% of reviewed papers employ a microservices architecture.",
    quote_context="Analysis of the 'architecture_type' column in the content extraction database shows 42 out of 60 papers (70%) explicitly mention or demonstrate microservices architecture patterns.",
    source_type="database",
    source_identifier="slr_content_db",
    locator={
        "table": "paper_analysis",
        "query": "SELECT COUNT(*) FROM paper_analysis WHERE architecture_type LIKE '%microservices%'",
        "result_description": "42 of 60 papers match"
    },
    confidence="high",
    extraction_method="aggregation"
)
```

### 4.3 Tool Response

The tool returns a structured result that the agent embeds in its output:

```python
@dataclass
class CitationResult:
    citation_id: str              # e.g., "cite_abc123" — embed this in prose
    verification_status: str      # verified | unverified | failed | pending
    similarity_score: float       # 0-1, how closely quote matched source (if verified)
    matched_location: dict        # Where the quote was found (if verified)
    source_registered: bool       # Whether this was a new or existing source
    formatted_reference: str      # Optional: pre-formatted citation string
```

### 4.4 Multi-Source Citations

**Design Decision:** For claims requiring multiple sources, the agent makes **multiple citation calls**, one per source.

Rationale:
- Preserves discrete, verifiable links between each piece of evidence and the claim
- Avoids the trap of listing sources without specifying what each contributes
- Each citation is independently verifiable

For aggregate claims (like "70% of papers..."), the agent cites the **aggregation artifact** (e.g., the content matrix) rather than individual papers. The matrix itself maintains links to the source papers.

---

## 5. Verification Strategy

### 5.1 The Verification Question

Should the tool verify that the `verbatim_quote` actually exists in the source and supports the claim?

**Yes.** The hope is that forcing the agent to fill out a structured citation form will itself encourage valid citations. But to catch hallucinations, an LLM verification step runs synchronously on every citation.

### 5.2 Approach: Synchronous LLM Verification

Verification runs **synchronously** — the agent waits for verification to complete before the citation tool returns a response. This applies to both Basic (SQLite) and Multi-Agent (PostgreSQL) modes.

| Step | Action |
|------|--------|
| 1 | Agent submits citation via `cite_*()` method |
| 2 | Citation is stored with `verification_status: pending` |
| 3 | Verification LLM is called immediately |
| 4 | Verification LLM receives: source content, claimed quote, claim |
| 5 | LLM determines if quote exists and supports the claim |
| 6 | Status updated to `verified` or `failed` with `verification_notes` |
| 7 | Citation tool returns result to agent |

### 5.3 Verification Failure Handling

When a citation fails verification:

1. The citation tool returns an error with:
   - `verification_status: failed`
   - `verification_notes`: explanation from verification LLM
2. The agent receives this feedback immediately
3. The agent can then decide how to react:
   - Correct the citation (find actual quote)
   - Adjust the claim to match available evidence
   - Remove the claim entirely

This creates a tight feedback loop — the agent cannot proceed until it addresses citation issues, which trains it to be more careful with citations over time.

### 5.4 Text Extraction

For document sources, text is extracted using **PyMuPDF** (good experience, reliable extraction). Extraction happens when the source is registered and stored in the `content` field. The verification LLM works with this pre-extracted text.

For websites, content is fetched and stored at registration time.

For databases, the `quote_context` provided by the agent is used directly (no extraction needed).

---

## 6. Web Search Integration

### 6.1 The Web Source Problem

Web sources present unique challenges:
- Pages change or disappear (link rot)
- No canonical segmentation (unlike PDF pages)
- Legal/ethical questions around archiving third-party content

### 6.2 Proposed Solution: Archive-on-Cite

When citing a web source, the tool:

1. Downloads the page content (HTML)
2. Takes a rendered screenshot (optional)
3. Extracts text content
4. Stores with timestamp and hash
5. Verifies the quote exists in the archived content
6. Returns archive reference in the citation

```python
citation_tool(
    claim="The BMF updated GoBD guidelines in 2024.",
    verbatim_quote="Die aktualisierten GoBD-Richtlinien treten am...",
    quote_context="[full paragraph from the page]",
    source_type="web",
    source_identifier="https://www.bundesfinanzministerium.de/...",
    locator={
        "heading_context": "Aktualisierungen 2024"
    }
    # Tool automatically archives the page
)
```

---

## 7. User Interface Strategy

### 7.1 Citation Display

The frontend renders citations as interactive elements:

```
The GoBD mandates that companies must store transaction data 
for 10 years [1].

───────────────────────────────────────────────────────────
[1] Click to expand:
   
   📄 Source: GoBD Regulations v2024-01 (gobd.pdf)
   📍 Location: Page 24, § 8.1
   
   📝 Quoted Text:
   "§ 8.1: Sämtliche buchungsrelevanten Transaktionsdaten 
   sind für einen Zeitraum von zehn Jahren unveränderbar 
   zu speichern."
   
   📖 Context:
   "Kapitel 8 regelt die Aufbewahrungsfristen für verschiedene
   Dokumenttypen. § 8.1: Sämtliche buchungsrelevanten..."
   
   ✅ Verification: Verified (similarity: 0.98)
   
   [Open Source Document] [Copy Citation]
───────────────────────────────────────────────────────────
```

### 7.2 Quick Verification Mode

For rapid review, users can hover over any citation to see:
- The `quote_context` (what the AI actually looked at)
- Verification status (green check, yellow warning, red X)
- One-click link to source location

---

## 8. CitationEngine Class Structure

The `CitationEngine` is a Python class that provides the full citation functionality as a self-contained package. It handles database connections, source registration, citation creation, and verification.

### 8.1 Class Overview

```python
class CitationEngine:
    """
    Citation & Provenance Engine for AI agents.

    Supports two modes:
    - Basic (SQLite): Single-agent, zero setup
    - Multi-Agent (PostgreSQL): Shared citation pool
    """

    # === ATTRIBUTES ===
    db: Connection          # SQLite or PostgreSQL connection
    agent: Agent            # LangGraph agent for verification
    mode: str               # "basic" or "multi-agent"

    # === MAGIC METHODS ===
    def __init__(self, mode: str = "basic", db_path: str = None):
        """
        Initialize the engine.

        Args:
            mode: "basic" (SQLite) or "multi-agent" (PostgreSQL)
            db_path: Path to SQLite file (basic mode) or None to use default

        Environment variables:
            CITATION_DB_URL: PostgreSQL connection string (multi-agent mode)
            CITATION_LLM_URL: Custom LLM endpoint (e.g., llama.cpp server)
            CITATION_REASONING_REQUIRED: none | low | medium | high
        """

    def __enter__(self): ...  # Context manager support
    def __exit__(self, *args): ...  # Clean up db connection

    # === INTERNAL METHODS ===
    def _connect(self) -> Connection:
        """
        Establish database connection based on mode.
        - Basic: SQLite file connection
        - Multi-agent: PostgreSQL connection from CITATION_DB_URL
        """

    def _setup_agent(self) -> Agent:
        """
        Initialize the verification LLM agent.

        Uses LangGraph's ChatOpenAI with custom base_url to support:
        - OpenAI API
        - OpenAI-compatible endpoints (llama.cpp, vLLM, Ollama)

        Reads CITATION_LLM_URL from environment for custom endpoints.
        """

    def _query(self, sql: str, params: tuple = None) -> List[dict]:
        """
        Execute query and return results as list of dicts.
        Abstracts SQLite vs PostgreSQL differences.
        """

    def _verify_citation(self, citation_id: int) -> VerificationResult:
        """
        Verify a citation using the verification agent.

        Called synchronously when a citation is made.
        Returns verification status and notes.
        """
```

### 8.2 Source Registration Methods

Separate methods for each source type, providing clear interfaces and type-specific handling:

```python
    # === SOURCE REGISTRATION ===

    def add_doc_source(
        self,
        file_path: str,
        name: str = None,
        version: str = None,
        metadata: dict = None
    ) -> Source:
        """
        Register a document source (PDF, markdown, txt, json, csv, images, etc.)

        Extracts text content using PyMuPDF (for PDFs) or appropriate parser.
        Stores extracted content for verification.
        """

    def add_web_source(
        self,
        url: str,
        name: str = None,
        version: str = None,
        metadata: dict = None
    ) -> Source:
        """
        Register a website source.

        Downloads and archives page content at registration time.
        Stores HTML and extracted text for verification.
        """

    def add_db_source(
        self,
        identifier: str,
        name: str,
        query: str = None,
        result_description: str = None,
        metadata: dict = None
    ) -> Source:
        """
        Register a database source (SQL, NoSQL, graph DB).

        Stores query and result description in metadata.
        Content field contains string representation of result.
        """

    def add_custom_source(
        self,
        name: str,
        content: str,
        description: str = None,
        metadata: dict = None
    ) -> Source:
        """
        Register a custom/AI-generated source.

        For artifacts created by the agent itself:
        - Computed matrices or tables
        - Generated plots or visualizations
        - Analysis outputs

        Content is provided directly by the agent.
        """
```

### 8.3 Citation Methods

Separate methods for citing different source types, plus a custom citation method:

```python
    # === CITATION METHODS ===

    def cite_doc(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: dict,                    # {page, section, section_header, ...}
        verbatim_quote: str = None,
        relevance_reasoning: str = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote"
    ) -> CitationResult:
        """
        Create a citation from a document source.

        Triggers synchronous verification.
        Returns citation ID or error with verification feedback.
        """

    def cite_web(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: dict,                    # {heading_context, accessed_at, ...}
        verbatim_quote: str = None,
        relevance_reasoning: str = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote"
    ) -> CitationResult:
        """
        Create a citation from a website source.

        Uses archived content (from add_web_source) for verification.
        """

    def cite_db(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: dict,                    # {query, table, result_description, ...}
        relevance_reasoning: str = None,
        confidence: str = "high",
        extraction_method: str = "aggregation"
    ) -> CitationResult:
        """
        Create a citation from a database source.

        Verification uses the query result stored in source.
        """

    def cite_custom(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: dict = None,
        relevance_reasoning: str = None,
        confidence: str = "high"
    ) -> CitationResult:
        """
        Create a citation from a custom/AI-generated source.

        For citing matrices, plots, computed results, etc.
        """
```

### 8.4 Retrieval Methods

```python
    # === RETRIEVAL METHODS ===

    def get_source(self, source_id: int) -> Source | None:
        """Get a source by ID."""

    def get_citation(self, citation_id: int) -> Citation | None:
        """Get a citation by ID."""

    def get_citations_for_source(self, source_id: int) -> List[Citation]:
        """Get all citations referencing a source."""

    def get_citations_by_session(self, session_id: str) -> List[Citation]:
        """Get all citations created in a session."""

    def list_sources(self, type: str = None) -> List[Source]:
        """List all registered sources, optionally filtered by type."""

    def list_citations(
        self,
        source_id: int = None,
        session_id: str = None,
        verification_status: str = None
    ) -> List[Citation]:
        """List citations with optional filters."""
```

### 8.5 Export Methods

```python
    # === EXPORT METHODS ===

    def format_citation(
        self,
        citation_id: int,
        style: str = "inline"         # inline | harvard | ieee | bibtex | apa
    ) -> str:
        """Format a single citation for display/export."""

    def export_bibliography(
        self,
        session_id: str = None,
        style: str = "harvard"
    ) -> str:
        """Export all citations (or session citations) as bibliography."""
```

### 8.6 Usage Example

```python
from citation_engine import CitationEngine

# Basic mode (SQLite, single agent)
engine = CitationEngine(mode="basic", db_path="./citations.db")

# Or multi-agent mode (PostgreSQL)
# engine = CitationEngine(mode="multi-agent")
# Requires CITATION_DB_URL environment variable

# Register sources when loading agent context
with engine:
    pdf_source = engine.add_doc_source(
        file_path="./documents/gobd.pdf",
        name="GoBD Regulations",
        version="2024-01"
    )

    web_source = engine.add_web_source(
        url="https://example.com/regulations",
        name="Updated Guidelines 2024"
    )

    # Agent makes a citation
    result = engine.cite_doc(
        claim="Companies must store transaction data for 10 years.",
        source_id=pdf_source.id,
        quote_context="Kapitel 8 regelt die Aufbewahrungsfristen...",
        verbatim_quote="§ 8.1: Sämtliche buchungsrelevanten Transaktionsdaten...",
        locator={"page": 24, "section": "§ 8.1"}
    )

    if result.verification_status == "verified":
        print(f"Citation [{result.citation_id}] verified!")
    else:
        print(f"Citation failed: {result.verification_notes}")
```

---

## 9. LangGraph Integration

### 9.1 Tool Schema for LLM

The citation tool must be defined in a way that LangGraph/LangChain can expose to the LLM:

```python
from langchain_core.tools import tool
from pydantic import BaseModel, Field

class CitationInput(BaseModel):
    """Schema for the citation tool that the LLM sees."""
    
    claim: str = Field(
        description="The specific assertion you are making that needs to be backed by evidence"
    )
    quote_context: str = Field(
        description="The full paragraph or surrounding text containing the evidence. "
                    "This helps the reader quickly verify the citation without opening the source."
    )
    verbatim_quote: str | None = Field(
        default=None,
        description="For direct citations: the exact text being quoted. "
                    "Leave empty for paraphrased or inferred claims."
    )
    relevance_reasoning: str | None = Field(
        default=None,
        description="Brief explanation of why this evidence supports the claim"
    )
    source_type: str = Field(
        description="Type of source: 'document', 'website', or 'database'"
    )
    source_identifier: str = Field(
        description="The source filename, URL, or database identifier"
    )
    locator: dict = Field(
        description="Location within the source. For documents: {page, section}. "
                    "For websites: {heading_context}. For databases: {query, table}."
    )
    confidence: str = Field(
        default="high",
        description="Your confidence in this citation: 'high', 'medium', or 'low'"
    )

@tool(args_schema=CitationInput)
def cite(
    claim: str,
    quote_context: str,
    source_type: str,
    source_identifier: str,
    locator: dict,
    verbatim_quote: str = None,
    relevance_reasoning: str = None,
    confidence: str = "high"
) -> str:
    """
    Register a citation linking your claim to a source. Returns a citation ID
    that you should embed in your response, e.g., "The law requires X [1]."

    Use this tool whenever you make a factual claim based on a specific source.
    """
    # Implementation calls the citation engine
    result = citation_engine.create_citation(...)
    return f"[{result.citation_id}]"  # Returns [1], [2], etc.
```

### 9.2 Context Window Management

**Problem:** Citation tool calls contain substantial text (quote_context, reasoning) that bloats the conversation history.

**Solution:** After the tool returns, the full tool call can be summarized or removed from the visible history, while the citation ID remains in the agent's output.

```python
# What the agent writes:
"The GoBD requires 10-year retention [1]."

# What gets stored in conversation history (after tool call processing):
"The GoBD requires 10-year retention [1]."
# Note: Cited GoBD §8.1 regarding retention periods

# What gets stored in citation database:
# Full citation record with all fields
```

**The "summary note" pattern:**
When a citation is created, the tool can return a short note that persists in context:

```python
class CitationResult:
    citation_id: int
    summary_note: str  # e.g., "Cited GoBD §8.1 (retention periods) — verified ✓"
    # ... other fields
```

This summary note can be injected into the conversation as a system message or appended to the tool result, giving the agent (and observers) a reminder of what was cited without the full payload.

### 9.3 Multi-Citation Workflow

For claims requiring multiple sources, the agent makes sequential tool calls:

```python
# Agent's reasoning:
"I need to cite three papers that support this architectural claim."

# Agent calls:
cite(claim="Microservices improve scalability", source_identifier="paper_001.pdf", ...)
# Returns: [1]

cite(claim="Microservices improve scalability", source_identifier="paper_002.pdf", ...)
# Returns: [2]

cite(claim="Microservices improve scalability", source_identifier="paper_003.pdf", ...)
# Returns: [3]

# Agent writes:
"Research shows that microservices improve scalability [1][2][3]."
```

---

## 10. Advanced Citation Types

### 10.1 Negative Citations

Sometimes it's valuable to record that a source was checked but does NOT support a claim:

```python
cite(
    claim="The GoBD requires encryption at rest",
    quote_context="§ 9 discusses data security but focuses on access controls...",
    source_identifier="gobd.pdf",
    locator={"page": 28, "section": "§ 9"},
    extraction_method="negative",  # NEW: indicates source was checked and doesn't support
    relevance_reasoning="Reviewed § 9 (Data Security) — discusses access controls but does not mention encryption requirements"
)
```

This creates an audit trail showing the agent did due diligence, even for claims it couldn't support.

### 10.2 Cross-Citation References

A citation can reference other citations for comparative or contradictory claims:

```python
class Citation:
    # ... existing fields ...
    related_citations: List[str] = None  # IDs of related citations
    relation_type: str = None            # "supports" | "contradicts" | "extends" | "supersedes"
```

Example: "The 2024 regulations [cite_002] supersede the 2019 guidance [cite_001]."

### 10.3 Spanning Locators

For quotes that cross page or section boundaries:

```python
locator = {
    "page_start": 24,
    "page_end": 25,
    "section": "§ 8.1-8.2",
    "char_offset_start": 1205,  # On page 24
    "char_offset_end": 342      # On page 25
}
```

---

## 11. Error Handling

### 11.1 Error Categories

| Error Type | Cause | Handling |
|------------|-------|----------|
| `SourceNotFound` | Referenced source doesn't exist and can't be auto-registered | Return error, suggest registering source first |
| `VerificationTimeout` | Quote verification took too long | Store with `verification_status: pending`, verify async |
| `VerificationFailed` | Quote not found in source | Store with `verification_status: failed`, include similarity score |
| `InvalidLocator` | Locator format doesn't match source type | Return validation error with expected format |
| `DatabaseUnavailable` | Storage layer unreachable | Retry with backoff, fail gracefully with error message |
| `QuoteTooLong` | Verbatim quote exceeds reasonable length | Warn agent, suggest using quote_context instead |

### 11.2 Error Response Format

```python
class CitationError:
    error_type: str
    message: str
    suggestion: str          # How to fix
    partial_result: dict     # Any data that was captured before failure
```

### 11.3 Graceful Degradation

If citation creation fails, the agent should still be able to continue:

```python
try:
    result = cite(...)
    return f"claim [{result.citation_id}]"
except CitationError as e:
    # Log the failure
    logger.warning(f"Citation failed: {e}")
    # Return inline citation as fallback
    return f"claim (see {source_identifier}, {locator})"
```

---

## 12. Session & Context Tracking

### 12.1 Session Binding

Every citation is associated with a session for audit purposes:

```python
class CitationContext:
    session_id: str           # Unique conversation/task ID
    agent_id: str             # Which agent created this
    user_id: str              # On whose behalf
    project_id: str           # Which project (Fessi, GoBD, SLR, etc.)
    created_at: datetime
    
# Passed to citation engine at initialization:
engine = CitationEngine(context=CitationContext(...))
```

### 12.2 Audit Trail Queries

```python
# "Show me everything the agent cited in this conversation"
get_citations_by_session(session_id)

# "Show me all citations made for GoBD compliance work"
get_citations_by_project(project_id="gobd_compliance")

# "Show me citations that failed verification"
get_citations_by_status(status="failed")
```

---

## 13. Security & Access Control

### 13.1 Citation Immutability

**Design Decision:** Citations are append-only. Once created, they cannot be modified or deleted.

Rationale:
- Audit integrity — you can't retroactively change what the AI claimed to cite
- Legal defensibility — the citation record is a tamper-evident log
- Debugging — you can trace exactly what happened

If a citation is wrong, create a new citation with a `supersedes` relationship to the old one.

### 13.2 Access Levels

| Role | Can Create | Can Read | Can Admin |
|------|------------|----------|-----------|
| Agent | ✓ Own session | ✓ All sources, own citations | ✗ |
| User | ✗ | ✓ Own project's citations | ✗ |
| Auditor | ✗ | ✓ All citations | ✗ |
| Admin | ✓ | ✓ | ✓ (source management, config) |

### 13.3 Multi-Tenancy

If the engine serves multiple projects:

```python
# Each project has isolated citation namespace
engine_fessi = CitationEngine(project="fessi", db_schema="fessi_citations")
engine_gobd = CitationEngine(project="gobd", db_schema="gobd_citations")

# Or single engine with project filtering
engine.create_citation(..., project="fessi")
engine.get_citations(project="fessi")
```

---

## 15. Design Decisions (Resolved)

This section documents the resolved design questions.

### 15.1 Database Choice & Operation Modes

**Decision:** Two operation modes

| Mode | Database | Use Case |
|------|----------|----------|
| **Basic** (default) | SQLite | Single-agent projects, zero setup, local development |
| **Multi-Agent** | PostgreSQL | Multiple agents sharing a citation pool, production |

Both modes use the same `CitationEngine` class interface. The mode is selected at initialization.

### 15.2 Citation ID Format

**Decision:** Auto-incrementing integers (1, 2, 3...)

Simple, readable in prose, and sufficient for the scale. No need for UUIDs or hashes.

### 15.3 Source Types

**Decision:** Four generic types instead of custom logic per source

| Type | Description | Registration Method |
|------|-------------|---------------------|
| `document` | PDFs, markdown, txt, json, csv, images | `add_doc_source()` |
| `website` | Web pages (archived at registration) | `add_web_source()` |
| `database` | SQL, NoSQL, graph DB records | `add_db_source()` |
| `custom` | AI-generated artifacts (matrices, plots, computed results) | `add_custom_source()` |

The locator schema is flexible — agents include whatever fields are relevant. No strict schema enforcement.

### 15.4 Relevance Reasoning Storage

**Decision:** Configurable via environment variable

```
CITATION_REASONING_REQUIRED=low  # Options: none, low, medium, high
```

Default: required only for `confidence: low`. This keeps flexibility while the system is tuned.

### 15.5 Source Registration Timing

**Decision:** Register when source is passed to the agent

Sources are registered via a `register_source()` method when content is provided to the agent. This:
- Tracks which sources the agent received (even if not cited)
- Avoids upfront registration burden
- Avoids lazy registration inconsistencies
- Extracts and stores text content for verification

### 15.6 Verification Strategy

**Decision:** Synchronous LLM verification with immediate feedback

- Verification runs **synchronously** — agent waits for result before continuing
- On failure: citation tool returns error with `verification_notes` explaining the issue
- Agent receives feedback immediately and can self-correct
- Applies to both Basic (SQLite) and Multi-Agent (PostgreSQL) modes
- Text extraction uses PyMuPDF

Async background verification was considered but rejected: agents that have already moved on would need retroactive notification, complicating the workflow. Synchronous verification ensures a tight feedback loop.

### 15.7 Citation Deduplication

**Decision:** Create separate records for each citation

Same quote can back different claims. Each citation has unique context and reasoning. Sources are stored separately, so no storage duplication concern.

### 15.8 Technical Implementation

| Component | Choice |
|-----------|--------|
| PDF extraction | PyMuPDF (good experience, reliable) |
| Verification | Synchronous LLM call (LangGraph ChatOpenAI with custom URL support) |
| Storage | SQLite (basic) or PostgreSQL (multi-agent) |
| LLM endpoint | OpenAI-compatible (supports llama.cpp, vLLM, Ollama via `CITATION_LLM_URL`) |

### 15.9 Performance

- Basic database indexing on source_id, session_id
- Verification runs synchronously per citation
- No caching needed (database reads are fast enough)
- Multi-agent mode uses PostgreSQL connection pooling

### 15.10 Content Length Limits

**Decision:** No limits

Let the agent do its thing. Add limits later if needed.

---

## 16. Next Steps

1. **Define database schemas** — Create tables for sources and citations (SQLite + PostgreSQL)
2. **Implement CitationEngine class** — Core Python class with mode selection (basic/multi-agent)
3. **Add `_connect()` and `_query()` methods** — Abstract SQLite vs PostgreSQL
4. **Add source registration methods** — `add_doc_source()`, `add_web_source()`, `add_db_source()`, `add_custom_source()`
5. **Add citation methods** — `cite_doc()`, `cite_web()`, `cite_db()`, `cite_custom()`
6. **Implement PyMuPDF extraction** — Extract text from PDFs on source registration
7. **Build `_setup_agent()` method** — Initialize verification LLM with custom URL support
8. **Build `_verify_citation()` method** — Synchronous LLM verification
9. **Add retrieval methods** — `get_source()`, `get_citation()`, `list_sources()`, `list_citations()`
10. **Create LangGraph tool wrapper** — Expose citation methods to agents
11. **Write tests** — Unit tests for citation creation, source registration, and verification

---

## Appendix A: Example Agent Flow

```
User: Is our invoice storage process compliant with GoBD?

Agent: [Retrieves GoBD document from knowledge base]
       [Analyzes invoice storage process]
       
Agent: Your current invoice storage process has a compliance gap.

       The GoBD requires that invoices be stored in an unalterable
       format for 10 years [1]. Your current process stores
       invoices as editable Word documents, which violates the
       immutability requirement [2].

       [Calls citation_tool for [1]]
       [Calls citation_tool for [2]]

Tool: Returns citation IDs and verification status

Agent: [Continues response with embedded citation IDs]

Frontend: [Renders citations as interactive, verifiable links]
```

---

*Document Version: 0.3*
*Last Updated: 2026-01-01*
*Status: Design Finalized — Ready for Implementation*

---

## Appendix B: Decision Log

Decisions made during the design phase, with rationale:

| # | Decision | Choice | Rationale | Date |
|---|----------|--------|-----------|------|
| 1 | Implementation approach | Python library (not microservice or MCP) | All projects use LangGraph/Python; simpler to start; can upgrade later | 2025-01-01 |
| 2 | Multi-source citations | Multiple tool calls, one per source | Preserves discrete, verifiable links; enables independent verification | 2025-01-01 |
| 3 | Quote fields | `verbatim_quote` (optional) + `quote_context` (required) | Supports both direct and indirect citations; context aids quick verification | 2025-01-01 |
| 4 | Claim storage | Always store claims in citation record | Enables querying by claim; supports audit trail | 2025-01-01 |
| 5 | Citation immutability | Append-only (no updates/deletes) | Audit integrity; legal defensibility; use `supersedes` for corrections | 2025-01-01 |
| 6 | Relevance reasoning | Configurable via env var; default required for low confidence | Flexibility while tuning; reduces context bloat | 2026-01-01 |
| 7 | Operation modes | Basic (SQLite) + Multi-Agent (PostgreSQL) | SQLite for zero-setup single agent; PostgreSQL for shared citation pool | 2026-01-01 |
| 8 | Citation ID format | Auto-incrementing integers (1, 2, 3...) | Simple, readable in prose, sufficient for scale | 2026-01-01 |
| 9 | Source types | Four generic types: document, website, database, custom | Custom for AI-generated artifacts (matrices, plots); separate methods per type | 2026-01-01 |
| 10 | Source registration | Separate methods per type (`add_doc_source()`, etc.) | Clear interfaces; type-specific handling; easier implementation | 2026-01-01 |
| 11 | Verification | Synchronous LLM (agent waits) | Tight feedback loop; agent can self-correct; async complicates notification | 2026-01-01 |
| 12 | PDF extraction | PyMuPDF | Good experience; reliable extraction | 2026-01-01 |
| 13 | Citation deduplication | Separate records per citation | Same quote can back different claims; unique context/reasoning | 2026-01-01 |
| 14 | Content length limits | No limits | Let agent decide; add limits if needed later | 2026-01-01 |
| 15 | Class structure | Single `CitationEngine` class with all methods | Self-contained package; mode selection at init; context manager support | 2026-01-01 |
| 16 | LLM endpoint | OpenAI-compatible with custom URL (`CITATION_LLM_URL`) | Supports llama.cpp, vLLM, Ollama for local inference | 2026-01-01 |
| 17 | Citation methods | Separate per source type (`cite_doc()`, `cite_web()`, etc.) | Clear interfaces; type-specific defaults; easier to use | 2026-01-01 |

---

## Appendix C: Glossary

| Term | Definition |
|------|------------|
| **Claim** | An assertion made by the AI agent that requires supporting evidence |
| **Citation** | The record linking a claim to its supporting evidence, including the quote, reasoning, and locator |
| **Source** | A canonical document, website, database, or custom artifact that can be cited |
| **Custom Source** | AI-generated artifact (matrix, plot, computed table) that can be cited |
| **Locator** | Flexible JSON data specifying where within a source the cited content is found |
| **Verbatim Quote** | The exact text being cited (for direct citations) |
| **Quote Context** | The surrounding paragraph or section containing the evidence (always required) |
| **Verification** | Synchronous LLM process confirming that a quoted passage exists and supports the claim |
| **Basic Mode** | Single-agent operation mode using SQLite for storage |
| **Multi-Agent Mode** | Multi-agent operation mode using PostgreSQL for shared citation pool |
| **Negative Citation** | A record indicating that a source was checked but does NOT support a claim |

---

## Appendix D: Related Work & Inspiration

- **Academic Citation Managers:** Zotero, Mendeley, EndNote — structured citation storage and formatting
- **Legal Citation Systems:** Bluebook, OSCOLA — precise locator conventions for legal texts
- **Wikipedia Citation Needed:** The "[citation needed]" pattern as inline verification prompts
- **Anthropic's Citation Format:** Claude's web search citations as a starting point
- **W3C PROV Ontology:** Formal provenance modeling (PROV-O) for data lineage
- **LangChain Citations:** Existing citation patterns in RAG applications
