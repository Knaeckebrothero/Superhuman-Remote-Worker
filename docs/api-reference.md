# API Reference

Complete reference for all public classes, methods, and types in the Citation Engine.

## Table of Contents

- [CitationEngine](#citationengine)
  - [Constructor](#constructor)
  - [Source Registration Methods](#source-registration-methods)
  - [Citation Methods](#citation-methods)
  - [Query Methods](#query-methods)
  - [Formatting Methods](#formatting-methods)
- [Data Models](#data-models)
  - [Source](#source)
  - [Citation](#citation)
  - [CitationResult](#citationresult)
  - [VerificationResult](#verificationresult)
- [Enums](#enums)
  - [SourceType](#sourcetype)
  - [VerificationStatus](#verificationstatus)
  - [ExtractionMethod](#extractionmethod)
- [LangChain Tools](#langchain-tools)
- [Exceptions](#exceptions)

---

## CitationEngine

The main class for managing sources and citations.

```python
from citation_engine import CitationEngine
```

### Constructor

```python
CitationEngine(
    mode: str = "basic",
    db_path: str | None = None,
    db_url: str | None = None
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | `str` | `"basic"` | Operation mode: `"basic"` (SQLite) or `"multi-agent"` (PostgreSQL) |
| `db_path` | `str \| None` | `None` | SQLite database path. If `None`, uses `CITATION_DB_PATH` env var or `./citations.db` |
| `db_url` | `str \| None` | `None` | PostgreSQL connection URL. If `None`, uses `CITATION_DB_URL` env var |

**Usage:**

```python
# Basic mode with default SQLite
with CitationEngine() as engine:
    ...

# Basic mode with custom path
with CitationEngine(mode="basic", db_path="/tmp/my_citations.db") as engine:
    ...

# Multi-agent mode with PostgreSQL
with CitationEngine(mode="multi-agent", db_url="postgresql://user:pass@localhost/db") as engine:
    ...
```

**Context Manager:**

Always use `CitationEngine` as a context manager to ensure proper cleanup:

```python
with CitationEngine() as engine:
    # Database connection is open
    ...
# Connection automatically closed
```

---

### Source Registration Methods

#### add_doc_source

Register a document file as a citation source.

```python
add_doc_source(
    file_path: str,
    name: str,
    version: str | None = None,
    metadata: dict | None = None
) -> Source
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | `str` | *required* | Path to the document file |
| `name` | `str` | *required* | Human-readable name for the source |
| `version` | `str \| None` | `None` | Version identifier (e.g., "v2.1", "2024-Q4") |
| `metadata` | `dict \| None` | `None` | Additional metadata (authors, DOI, etc.) |

**Returns:** [`Source`](#source)

**Raises:**
- `FileNotFoundError`: If file does not exist
- `ValueError`: If file format is not supported

**Supported formats:** `.pdf` (requires `[pdf]` extra), `.txt`, `.md`, `.json`

**Example:**

```python
source = engine.add_doc_source(
    file_path="reports/annual_2024.pdf",
    name="Annual Report 2024",
    version="Final",
    metadata={
        "authors": ["Finance Team"],
        "department": "Finance",
        "confidential": True
    }
)
print(f"Registered source {source.id}: {source.name}")
```

---

#### add_web_source

Register a web page as a citation source. Content is archived at registration time.

```python
add_web_source(
    url: str,
    name: str | None = None,
    version: str | None = None,
    metadata: dict | None = None
) -> Source
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *required* | URL of the web page |
| `name` | `str \| None` | `None` | Display name (defaults to URL if not provided) |
| `version` | `str \| None` | `None` | Version identifier (often the access date) |
| `metadata` | `dict \| None` | `None` | Additional metadata |

**Returns:** [`Source`](#source)

**Raises:**
- `ConnectionError`: If URL cannot be fetched (network error, 4xx, 5xx)
- `ValueError`: If URL is malformed

**Requires:** `[web]` extra (`pip install citation-engine[web]`)

**Auto-populated metadata:**
- `url`: The original URL
- `accessed_at`: ISO timestamp of when content was fetched
- `title`: Page title if available

**Example:**

```python
source = engine.add_web_source(
    url="https://docs.python.org/3/library/typing.html",
    name="Python Typing Documentation",
    version="3.12",
    metadata={"language": "en"}
)
```

---

#### add_db_source

Register database query results as a citation source.

```python
add_db_source(
    identifier: str,
    name: str,
    content: str,
    query: str | None = None,
    metadata: dict | None = None
) -> Source
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `identifier` | `str` | *required* | Unique identifier (e.g., "analytics.user_metrics") |
| `name` | `str` | *required* | Human-readable name |
| `content` | `str` | *required* | Query results as formatted text |
| `query` | `str \| None` | `None` | The SQL query that produced the results |
| `metadata` | `dict \| None` | `None` | Additional metadata (database, execution time, etc.) |

**Returns:** [`Source`](#source)

**Example:**

```python
source = engine.add_db_source(
    identifier="sales.q1_summary",
    name="Q1 2024 Sales Summary",
    content="""
    | Region | Revenue | Units |
    |--------|---------|-------|
    | North  | $1.2M   | 3,400 |
    | South  | $0.9M   | 2,100 |
    """,
    query="SELECT region, SUM(revenue), COUNT(*) FROM sales WHERE quarter='Q1' GROUP BY region",
    metadata={
        "database": "sales_prod",
        "executed_at": "2024-04-01T10:00:00Z"
    }
)
```

---

#### add_custom_source

Register AI-generated or computed content as a citation source.

```python
add_custom_source(
    name: str,
    content: str,
    description: str | None = None,
    metadata: dict | None = None
) -> Source
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Name for the source |
| `content` | `str` | *required* | The content to register |
| `description` | `str \| None` | `None` | Description of what this content represents |
| `metadata` | `dict \| None` | `None` | Additional metadata (generator, model, etc.) |

**Returns:** [`Source`](#source)

**Example:**

```python
source = engine.add_custom_source(
    name="Competitor Analysis Matrix",
    content="""
    Analysis generated on 2024-04-15

    Market Position:
    - Our product: 23% market share
    - Competitor A: 31% market share
    - Competitor B: 18% market share
    """,
    description="AI-generated competitive analysis",
    metadata={
        "generated_by": "analysis-agent",
        "model": "gpt-4o",
        "confidence": "high"
    }
)
```

---

### Citation Methods

#### cite_doc

Create a citation from a document source.

```python
cite_doc(
    claim: str,
    source_id: str,
    quote_context: str,
    verbatim_quote: str | None = None,
    locator: dict | None = None,
    extraction_method: str | None = None,
    relevance_reasoning: str | None = None,
    confidence: str = "high",
    supersedes: str | None = None
) -> CitationResult
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `claim` | `str` | *required* | The claim being made |
| `source_id` | `str` | *required* | ID of the registered source |
| `quote_context` | `str` | *required* | Surrounding text that provides context |
| `verbatim_quote` | `str \| None` | `None` | Exact quote from source (if applicable) |
| `locator` | `dict \| None` | `None` | Location info (page, section, paragraph, etc.) |
| `extraction_method` | `str \| None` | `None` | How the quote was extracted: `"verbatim"`, `"paraphrase"`, `"inference"` |
| `relevance_reasoning` | `str \| None` | `None` | Explanation of how quote supports claim |
| `confidence` | `str` | `"high"` | Confidence level: `"high"`, `"medium"`, `"low"` |
| `supersedes` | `str \| None` | `None` | Citation ID this replaces (for corrections) |

**Returns:** [`CitationResult`](#citationresult)

**Raises:**
- `ValueError`: If `source_id` does not exist

**Example:**

```python
result = engine.cite_doc(
    claim="The company exceeded revenue targets by 15%",
    source_id=source.id,
    quote_context="Financial highlights indicate strong performance with revenue exceeding targets by 15% for the fiscal year.",
    verbatim_quote="revenue exceeding targets by 15%",
    locator={"page": 5, "section": "Executive Summary"},
    extraction_method="verbatim",
    confidence="high"
)
```

---

#### cite_web

Create a citation from a web source.

```python
cite_web(
    claim: str,
    source_id: str,
    quote_context: str,
    verbatim_quote: str | None = None,
    locator: dict | None = None,
    extraction_method: str | None = None,
    relevance_reasoning: str | None = None,
    confidence: str = "high",
    supersedes: str | None = None
) -> CitationResult
```

**Parameters:** Same as [`cite_doc`](#cite_doc)

**Locator fields for web sources:**
- `url`: The page URL
- `section`: Section or heading name
- `anchor`: HTML anchor/fragment
- `accessed_at`: Access timestamp

**Example:**

```python
result = engine.cite_web(
    claim="Python 3.12 introduced type parameter syntax",
    source_id=web_source.id,
    quote_context="Python 3.12 introduces a new, more compact syntax for generic classes and functions.",
    locator={
        "url": "https://docs.python.org/3/whatsnew/3.12.html",
        "section": "New Features"
    }
)
```

---

#### cite_db

Create a citation from a database source.

```python
cite_db(
    claim: str,
    source_id: str,
    quote_context: str,
    verbatim_quote: str | None = None,
    locator: dict | None = None,
    extraction_method: str | None = None,
    relevance_reasoning: str | None = None,
    confidence: str = "high",
    supersedes: str | None = None
) -> CitationResult
```

**Parameters:** Same as [`cite_doc`](#cite_doc)

**Locator fields for database sources:**
- `table`: Table name
- `query`: SQL query
- `row`: Row identifier
- `column`: Column name

**Example:**

```python
result = engine.cite_db(
    claim="North region had the highest Q1 revenue at $1.2M",
    source_id=db_source.id,
    quote_context="North | $1.2M | 3,400",
    locator={
        "table": "sales",
        "query": "SELECT region, revenue FROM sales WHERE quarter='Q1'"
    },
    extraction_method="aggregation"
)
```

---

#### cite_custom

Create a citation from a custom source.

```python
cite_custom(
    claim: str,
    source_id: str,
    quote_context: str,
    verbatim_quote: str | None = None,
    locator: dict | None = None,
    extraction_method: str | None = None,
    relevance_reasoning: str | None = None,
    confidence: str = "high",
    supersedes: str | None = None
) -> CitationResult
```

**Parameters:** Same as [`cite_doc`](#cite_doc)

**Note:** Custom sources typically require `relevance_reasoning` to explain how AI-generated content supports the claim.

**Example:**

```python
result = engine.cite_custom(
    claim="We have the second-largest market share",
    source_id=custom_source.id,
    quote_context="Our product: 23% market share, Competitor A: 31%",
    relevance_reasoning="23% is second only to Competitor A's 31%"
)
```

---

### Query Methods

#### get_source

Retrieve a source by ID.

```python
get_source(source_id: str) -> Source | None
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `source_id` | `str` | The source ID |

**Returns:** [`Source`](#source) or `None` if not found

---

#### list_sources

List all registered sources.

```python
list_sources() -> list[Source]
```

**Returns:** List of [`Source`](#source) objects

**Example:**

```python
sources = engine.list_sources()
for source in sources:
    print(f"[{source.id}] {source.name} ({source.type.value})")
```

---

#### get_citation

Retrieve a citation by ID.

```python
get_citation(citation_id: str) -> Citation | None
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `citation_id` | `str` | The citation ID (e.g., "1", "2") |

**Returns:** [`Citation`](#citation) or `None` if not found

---

### Formatting Methods

#### format_citation

Format a citation for display.

```python
format_citation(
    citation_id: str,
    style: str = "inline"
) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `citation_id` | `str` | *required* | The citation ID |
| `style` | `str` | `"inline"` | Format style: `"inline"`, `"harvard"`, `"bibtex"` |

**Returns:** Formatted citation string

**Styles:**

| Style | Example Output |
|-------|---------------|
| `inline` | `[1] Annual Report 2024, p. 5` |
| `harvard` | `Annual Report (2024) p. 5` |
| `bibtex` | `@article{annual_report_2024, title={Annual Report}, ...}` |

---

## Data Models

### Source

Represents a registered citation source.

```python
from citation_engine.models import Source
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `id` | `str` | Unique identifier (UUID) |
| `type` | [`SourceType`](#sourcetype) | Type of source |
| `name` | `str` | Display name |
| `content` | `str` | Extracted text content |
| `content_hash` | `str` | SHA-256 hash of content |
| `version` | `str \| None` | Version identifier |
| `metadata` | `dict` | Additional metadata |
| `created_at` | `datetime` | Registration timestamp |

---

### Citation

Represents a stored citation.

```python
from citation_engine.models import Citation
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `id` | `str` | Citation ID (sequential: "1", "2", ...) |
| `source_id` | `str` | ID of the source |
| `claim` | `str` | The claim being cited |
| `quote_context` | `str` | Supporting quote/context |
| `verbatim_quote` | `str \| None` | Exact quote if provided |
| `locator` | `dict` | Location information |
| `extraction_method` | [`ExtractionMethod`](#extractionmethod) | How quote was extracted |
| `relevance_reasoning` | `str \| None` | Explanation of relevance |
| `confidence` | `str` | Confidence level |
| `verification_status` | [`VerificationStatus`](#verificationstatus) | Verification result |
| `similarity_score` | `float \| None` | Verification confidence (0-1) |
| `verification_notes` | `str \| None` | Verification details |
| `supersedes` | `str \| None` | ID of superseded citation |
| `created_at` | `datetime` | Creation timestamp |

---

### CitationResult

Returned when creating a citation.

```python
from citation_engine.models import CitationResult
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `citation_id` | `str` | The assigned citation ID |
| `verification_status` | [`VerificationStatus`](#verificationstatus) | Verification result |
| `similarity_score` | `float \| None` | Verification confidence (0-1) |
| `verification_notes` | `str \| None` | Details from verification |

---

### VerificationResult

Internal model for LLM verification response.

```python
from citation_engine.models import VerificationResult
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `is_verified` | `bool` | Whether verification passed |
| `similarity_score` | `float` | Confidence score (0-1) |
| `reasoning` | `str` | LLM's reasoning |
| `matched_location` | `dict \| None` | Where quote was found |

---

## Enums

### SourceType

```python
from citation_engine import SourceType
```

| Value | Description |
|-------|-------------|
| `SourceType.DOCUMENT` | Document file (PDF, TXT, MD, JSON) |
| `SourceType.WEBSITE` | Web page |
| `SourceType.DATABASE` | Database query result |
| `SourceType.CUSTOM` | AI-generated or custom content |

---

### VerificationStatus

```python
from citation_engine import VerificationStatus
```

| Value | Description |
|-------|-------------|
| `VerificationStatus.PENDING` | Not yet verified |
| `VerificationStatus.VERIFIED` | Quote found and supports claim |
| `VerificationStatus.FAILED` | Quote not found or doesn't support claim |

---

### ExtractionMethod

```python
from citation_engine.models import ExtractionMethod
```

| Value | Description |
|-------|-------------|
| `ExtractionMethod.VERBATIM` | Direct quote |
| `ExtractionMethod.PARAPHRASE` | Reworded content |
| `ExtractionMethod.INFERENCE` | Derived from multiple facts |
| `ExtractionMethod.AGGREGATION` | Computed from data |

---

## LangChain Tools

Create tools for LangChain agents.

```python
from citation_engine.tool import create_citation_tools
```

### create_citation_tools

```python
create_citation_tools(engine: CitationEngine) -> list[StructuredTool]
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `engine` | `CitationEngine` | An initialized CitationEngine instance |

**Returns:** List of LangChain `StructuredTool` objects

**Tools created:**

| Tool Name | Description |
|-----------|-------------|
| `cite` | Create a citation for a claim |
| `register_source` | Register a new source document |
| `list_sources` | List all registered sources |
| `get_citation_status` | Check verification status of a citation |

**Example:**

```python
from citation_engine import CitationEngine
from citation_engine.tool import create_citation_tools
from langchain.agents import initialize_agent, AgentType
from langchain_openai import ChatOpenAI

engine = CitationEngine()
tools = create_citation_tools(engine)

llm = ChatOpenAI(model="gpt-4o")
agent = initialize_agent(
    tools=tools,
    llm=llm,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION
)

response = agent.run("Register the file report.pdf and cite the key findings.")
```

---

## Exceptions

The Citation Engine uses standard Python exceptions:

| Exception | When Raised |
|-----------|-------------|
| `ValueError` | Invalid parameters, unsupported file format, source not found |
| `FileNotFoundError` | Document file does not exist |
| `ConnectionError` | Web source fetch failed |
| `RuntimeError` | Database connection issues |

**Example error handling:**

```python
from citation_engine import CitationEngine

with CitationEngine() as engine:
    try:
        source = engine.add_doc_source("missing.pdf", name="Test")
    except FileNotFoundError:
        print("File not found")

    try:
        source = engine.add_web_source("https://invalid.invalid", name="Test")
    except ConnectionError as e:
        print(f"Failed to fetch: {e}")

    try:
        result = engine.cite_doc(
            claim="Test",
            source_id="nonexistent",
            quote_context="Test"
        )
    except ValueError as e:
        print(f"Invalid source: {e}")
```
