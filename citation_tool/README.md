""> Okay, how do I add this to another project? Do I just copy the "citation_tool" folder and tell the developer, hey this is the new package please integrate it into project x, but
 please don't integrate the acual code, but just use it as a python package (I wanna avoid that people integrate this manually which would make updating the package a nightmare). 

● Great question! You have several options, depending on your setup:

  Option 1: Install from Local Path (Simplest)

  If the citation_tool folder is accessible to the other project (same machine, shared network, or cloned repo):

  # In the other project's virtual environment
  pip install -e /home/ghost/Repositories/Scripts-and-Notebooks/mcps/citation_tool

  # Or with extras
  pip install -e "/home/ghost/Repositories/Scripts-and-Notebooks/mcps/citation_tool[full]"

  Add to the other project's requirements.txt:
  -e /path/to/citation_tool
  # or with extras:
  -e /path/to/citation_tool[full]

  Pros: Simple, changes reflect immediately (editable install)
  Cons: Path must be accessible on every machine

  ---
  Option 2: Install from Git (Best for Teams)

  Push citation_tool to a git repository (GitHub, GitLab, internal server), then:

  pip install git+https://github.com/youruser/citation-tool.git
  pip install "git+https://github.com/youruser/citation-tool.git#egg=citation-engine[full]"

  # Or a specific branch/tag
  pip install git+https://github.com/youruser/citation-tool.git@v1.0.0
  pip install git+https://github.com/youruser/citation-tool.git@main

  Add to requirements.txt:
  citation-engine @ git+https://github.com/youruser/citation-tool.git@main
  # or with extras:
  citation-engine[full] @ git+https://github.com/youruser/citation-tool.git@main

  Or in pyproject.toml:
  [project]
  dependencies = [
      "citation-engine @ git+https://github.com/youruser/citation-tool.git@main",
  ]

  Pros: Works across machines, versioned via git tags, updates via pip install --upgrade
  Cons: Requires git access

  ---
  Option 3: Build a Wheel and Share

  Build a distributable package file:

  cd /home/ghost/Repositories/Scripts-and-Notebooks/mcps/citation_tool
  pip install build
  python -m build

  This creates:
  dist/
  ├── citation_engine-0.1.0-py3-none-any.whl  # Binary distribution
  └── citation_engine-0.1.0.tar.gz            # Source distribution

  Share the .whl file, install with:
  pip install citation_engine-0.1.0-py3-none-any.whl

  Pros: Self-contained file, no git needed
  Cons: Manual file sharing, need to rebuild for updates

  ---
  My Recommendation

  For your team: Option 2 (Git install)

  1. Push citation_tool to a git repo (can be private)
  2. Tell developers to add this to their requirements.txt:

  # Citation Engine - DO NOT copy source code, install as package
  citation-engine[full] @ git+https://github.com/youruser/citation-tool.git@main

  3. When you update the package, developers just run:
  pip install --upgrade citation-engine

  ---
  What to Tell the Developer

  "Install the Citation Engine as a package - don't copy the source code:

  pip install git+https://github.com/youruser/citation-tool.git

  Then import and use it:
  from citation_engine import CitationEngine

  with CitationEngine() as engine:
      source = engine.add_doc_source("doc.pdf", name="My Doc")
      result = engine.cite_doc(claim="...", source_id=source.id, quote_context="...")

  Documentation: See docs/ folder in the citation-tool repo.
  Updates: Run pip install --upgrade citation-engine"""



# Citation & Provenance Engine

A structured citation system for AI agents that forces articulation of claim-to-source relationships and enables verification.

## Documentation

| Document | Description |
|----------|-------------|
| [Quick Start](docs/quickstart.md) | Get running in 5 minutes |
| [Usage Guide](docs/usage-guide.md) | Detailed examples for all features |
| [API Reference](docs/api-reference.md) | Complete method and class documentation |
| [Configuration](docs/configuration.md) | Environment variables and options |

## Overview

The Citation Engine is a Python library that provides AI agents with a structured way to cite sources and verify their claims. Instead of writing ad-hoc inline citations, agents explicitly register each claim-to-source relationship through structured tool calls.

**Key Features:**
- Forced articulation of claim-source relationships
- Synchronous LLM-based verification
- Support for documents (PDF, markdown, txt), websites, databases, and custom artifacts
- Two operation modes: Basic (SQLite) and Multi-Agent (PostgreSQL)
- LangGraph/LangChain tool integration
- Multiple citation export formats (Harvard, IEEE, BibTeX, APA)

## Installation

### Using in Another Project

Install directly from the local path:

```bash
# Basic installation
pip install -e /path/to/citation_tool

# With all features
pip install -e "/path/to/citation_tool[full]"

# With specific extras
pip install -e "/path/to/citation_tool[pdf,web]"
```

Or install from git (if pushed to a repository):

```bash
pip install git+https://github.com/youruser/citation-tool.git
pip install "git+https://github.com/youruser/citation-tool.git#egg=citation-engine[full]"
```

### Development Installation

```bash
# Clone and install for development
cd citation_tool

# Basic installation (SQLite only, no verification)
pip install -e .

# Full installation with all features
pip install -e ".[full]"

# Individual optional dependencies
pip install -e ".[pdf]"        # PDF extraction with PyMuPDF
pip install -e ".[web]"        # Web page fetching
pip install -e ".[langchain]"  # LangChain/LangGraph integration
pip install -e ".[postgresql]" # PostgreSQL for multi-agent mode

# Development installation
pip install -e ".[dev]"
```

## Quick Start

> **Note:** For comprehensive usage examples, see the [Usage Guide](docs/usage-guide.md).
> For complete API documentation, see the [API Reference](docs/api-reference.md).

### Basic Usage

```python
from citation_engine import CitationEngine

# Initialize in basic mode (SQLite)
engine = CitationEngine(mode="basic", db_path="./citations.db")

with engine:
    # Register a document source
    source = engine.add_doc_source(
        file_path="./documents/regulations.pdf",
        name="GoBD Regulations",
        version="2024-01"
    )

    # Create a citation
    result = engine.cite_doc(
        claim="Companies must store transaction data for 10 years.",
        source_id=source.id,
        quote_context="Kapitel 8 regelt die Aufbewahrungsfristen...",
        verbatim_quote="Transaktionsdaten sind für einen Zeitraum von zehn Jahren zu speichern",
        locator={"page": 24, "section": "§ 8.1"}
    )

    if result.verification_status.value == "verified":
        print(f"Citation [{result.citation_id}] verified!")
    else:
        print(f"Verification failed: {result.verification_notes}")
```

### LangGraph Integration

```python
from citation_engine import CitationEngine, create_citation_tools
from langgraph.prebuilt import create_react_agent

# Initialize engine
engine = CitationEngine(mode="basic", db_path="./citations.db")

with engine:
    # Create LangChain tools
    tools = create_citation_tools(engine)

    # Use with a LangGraph agent
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model="gpt-4o-mini")
    agent = create_react_agent(llm, tools)

    # Agent can now use cite(), register_source(), list_sources(), get_citation_status()
```

### Simplified Tool Interface

```python
from citation_engine import CitationTool

with CitationTool(mode="basic", db_path="./citations.db") as tool:
    # Register source
    source_id = tool.add_source("document", "./paper.pdf")

    # Create citation
    result = tool.cite(
        claim="The study shows X",
        source_id=source_id,
        quote_context="Full paragraph...",
        locator={"page": 5}
    )

    print(f"Citation: {result}")  # [1]
```

## Operation Modes

### Basic Mode (SQLite)

Default mode for single-agent workflows. Zero configuration required.

```python
engine = CitationEngine(mode="basic", db_path="./citations.db")
```

### Multi-Agent Mode (PostgreSQL)

For multi-agent systems with shared citation pool.

```bash
export CITATION_DB_URL="postgresql://user:pass@localhost:5432/citations"
```

```python
engine = CitationEngine(mode="multi-agent")
```

## Source Types

| Type | Description | Registration Method |
|------|-------------|---------------------|
| `document` | PDFs, markdown, txt, json, csv, images | `add_doc_source()` |
| `website` | Web pages (archived at registration) | `add_web_source()` |
| `database` | SQL, NoSQL, graph DB records | `add_db_source()` |
| `custom` | AI-generated artifacts (matrices, plots) | `add_custom_source()` |

### Examples

```python
# Document source
pdf_source = engine.add_doc_source(
    file_path="./regulations.pdf",
    name="GoBD Regulations",
    version="2024-01"
)

# Website source (content is archived at registration)
web_source = engine.add_web_source(
    url="https://example.com/regulations",
    name="Regulations Website"
)

# Database source
db_source = engine.add_db_source(
    identifier="paper_analysis.architecture_types",
    name="Architecture Analysis",
    content="42 of 60 papers use microservices (70%)",
    query="SELECT COUNT(*) FROM papers WHERE arch='microservices'"
)

# Custom source (AI-generated artifact)
custom_source = engine.add_custom_source(
    name="Paper Analysis Matrix",
    content="Paper,Architecture,Year\n1,Microservices,2023\n...",
    description="Computed matrix of paper architectures"
)
```

## Citation Methods

```python
# Document citation
result = engine.cite_doc(
    claim="The regulation requires X",
    source_id=source.id,
    quote_context="Full paragraph containing evidence...",
    verbatim_quote="Exact quoted text",
    locator={"page": 24, "section": "§ 8.1"},
    confidence="high",
    extraction_method="direct_quote"
)

# Database citation (aggregation)
result = engine.cite_db(
    claim="70% of papers use microservices",
    source_id=db_source.id,
    quote_context="Analysis of 60 papers shows 42 use microservices",
    locator={"table": "papers", "query": "..."},
    extraction_method="aggregation"
)

# Custom source citation
result = engine.cite_custom(
    claim="Analysis shows trend toward microservices",
    source_id=custom_source.id,
    quote_context="Paper analysis matrix indicates...",
    confidence="medium",
    relevance_reasoning="The matrix summarizes the architectural choices..."
)
```

## Verification

Citations are verified synchronously using an LLM. The verification checks:
1. Whether the quoted text exists in the source
2. Whether the text actually supports the claim

### Configuration

```bash
# Custom LLM endpoint (llama.cpp, vLLM, Ollama)
export CITATION_LLM_URL="http://localhost:8000/v1"

# OpenAI API key (if using OpenAI)
export OPENAI_API_KEY="sk-..."

# Model to use for verification
export CITATION_LLM_MODEL="gpt-4o-mini"

# When to require relevance reasoning
export CITATION_REASONING_REQUIRED="low"  # none, low, medium, high
```

## Export Formats

```python
# Single citation
formatted = engine.format_citation(citation_id, style="harvard")

# Export bibliography
bibliography = engine.export_bibliography(
    session_id="session_123",
    style="bibtex"
)

# Supported styles: inline, harvard, ieee, bibtex, apa
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CITATION_DB_PATH` | SQLite database path | `./citations.db` |
| `CITATION_DB_URL` | PostgreSQL connection string | - |
| `CITATION_LLM_URL` | Custom LLM endpoint | - |
| `CITATION_LLM_MODEL` | LLM model for verification | `gpt-4o-mini` |
| `OPENAI_API_KEY` | OpenAI API key | - |
| `CITATION_REASONING_REQUIRED` | When reasoning is required | `low` |

## Development

### Setup

```bash
# Clone and install
git clone <repository-url>
cd citation_tool

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install development dependencies
pip install -e ".[dev,full]"

# Copy environment template
cp .env.example .env
# Edit .env with your configuration
```

### Running Tests

```bash
# Run unit tests (no external dependencies needed)
pytest tests/test_engine.py -v

# Run with coverage
pytest tests/ -v --cov=src/citation_engine

# Format code
ruff format src/ tests/

# Lint
ruff check src/ tests/
```

### Integration Tests

Integration tests require additional setup and are disabled by default.

#### PostgreSQL Tests

```bash
# Start PostgreSQL with Podman
podman-compose up -d

# Enable PostgreSQL tests
export RUN_POSTGRES_TESTS=true

# Run PostgreSQL integration tests
pytest tests/test_integration_postgres.py -v

# Stop PostgreSQL
podman-compose down
```

#### LLM Verification Tests

```bash
# Configure LLM endpoint in .env
# CITATION_LLM_URL=http://localhost:8080/v1
# CITATION_LLM_MODEL=your-model-name

# Enable LLM tests
export RUN_LLM_TESTS=true

# Run LLM integration tests
pytest tests/test_integration_llm.py -v -s
```

#### Web Source Tests

```bash
# Install web dependencies
pip install -e ".[web]"

# Enable web tests (requires network access)
export RUN_WEB_TESTS=true

# Run web integration tests
pytest tests/test_integration_web.py -v -s
```

#### Running All Integration Tests

```bash
# Start PostgreSQL
podman-compose up -d

# Set all test flags
export RUN_POSTGRES_TESTS=true
export RUN_LLM_TESTS=true
export RUN_WEB_TESTS=true

# Run all tests
pytest tests/ -v -s

# Cleanup
podman-compose down
```

### Development Database (Podman)

The project includes a `podman-compose.yml` for local PostgreSQL development:

```bash
# Start PostgreSQL
podman-compose up -d

# View logs
podman-compose logs -f postgres

# Connect with psql
podman exec -it citation-engine-postgres psql -U citation_user -d citations

# Start with pgAdmin (optional)
podman-compose --profile admin up -d
# Access pgAdmin at http://localhost:5050

# Stop and remove volumes
podman-compose down -v
```

### Project Structure

```
citation_tool/
├── src/
│   └── citation_engine/
│       ├── __init__.py      # Package exports
│       ├── engine.py        # CitationEngine class
│       ├── models.py        # Data models (Source, Citation, etc.)
│       ├── schema.py        # Database schemas (SQLite, PostgreSQL)
│       └── tool.py          # LangChain tool wrappers
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Pytest fixtures and configuration
│   ├── test_engine.py       # Unit tests
│   ├── test_integration_postgres.py  # PostgreSQL tests
│   ├── test_integration_llm.py       # LLM verification tests
│   └── test_integration_web.py       # Web source tests
├── docs/
│   ├── quickstart.md        # Quick start guide
│   ├── usage-guide.md       # Detailed usage examples
│   ├── api-reference.md     # Complete API documentation
│   └── configuration.md     # Configuration reference
├── .env.example             # Environment template
├── podman-compose.yml       # PostgreSQL for development
├── pyproject.toml           # Project configuration
├── CLAUDE.md                # AI assistant instructions
└── README.md                # This file
```

## Troubleshooting

### psycopg2 Import Error

If you get an import error for psycopg2 when using multi-agent mode:

```bash
pip install psycopg2-binary
```

### PyMuPDF Import Error

If you get an import error when extracting PDFs:

```bash
pip install pymupdf
```

### BeautifulSoup Import Error

If you get an import error when fetching web pages:

```bash
pip install beautifulsoup4 requests
```

### LLM Connection Error

If verification fails with connection errors:

1. Check that `CITATION_LLM_URL` points to a running LLM server
2. For OpenAI, ensure `OPENAI_API_KEY` is set
3. Check network connectivity to the LLM endpoint

### PostgreSQL Connection Error

If you can't connect to PostgreSQL:

1. Ensure the container is running: `podman ps`
2. Check the connection URL in `.env`
3. Verify credentials match `podman-compose.yml`

## License

MIT
