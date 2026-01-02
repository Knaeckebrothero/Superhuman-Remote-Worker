# Quick Start Guide

Get the Citation Engine running in your project in under 5 minutes.

## Installation

```bash
# Basic installation
pip install -e /path/to/citation_tool

# With PDF support
pip install -e "/path/to/citation_tool[pdf]"

# With web scraping support
pip install -e "/path/to/citation_tool[web]"

# With PostgreSQL support (for multi-agent deployments)
pip install -e "/path/to/citation_tool[postgresql]"

# Everything
pip install -e "/path/to/citation_tool[full]"
```

## Basic Usage

```python
from citation_engine import CitationEngine

# Create an engine instance (uses SQLite by default)
with CitationEngine(mode="basic") as engine:

    # 1. Register a source document
    source = engine.add_doc_source(
        file_path="reports/quarterly_report.pdf",
        name="Q4 2024 Financial Report",
        version="2024-Q4"
    )

    # 2. Create a citation for a claim
    result = engine.cite_doc(
        claim="Company revenue increased by 23% year-over-year",
        source_id=source.id,
        quote_context="Financial highlights show revenue growth of 23% compared to the previous fiscal year.",
        verbatim_quote="revenue growth of 23%",
        locator={"page": 5, "section": "Financial Highlights"}
    )

    # 3. Use the citation
    print(f"Citation ID: {result.citation_id}")           # e.g., "1"
    print(f"Status: {result.verification_status.value}")  # "verified" or "failed"
    print(f"Confidence: {result.similarity_score}")       # 0.0 to 1.0

    # 4. Format for output
    formatted = engine.format_citation(result.citation_id, style="inline")
    print(formatted)  # "[1] Q4 2024 Financial Report, p. 5"
```

## What Just Happened?

1. **Source Registration**: The PDF was read, text extracted, and stored with a content hash
2. **Citation Creation**: Your claim was linked to a specific quote in the source
3. **Verification**: An LLM checked that the quote exists and supports the claim
4. **Storage**: Everything is persisted in SQLite for future reference

## Environment Setup (Optional)

Create a `.env` file for LLM verification:

```bash
# For local LLM (llama.cpp, Ollama, vLLM)
CITATION_LLM_URL=http://localhost:8080/v1
CITATION_LLM_MODEL=mistral-7b

# For OpenAI
OPENAI_API_KEY=sk-...
CITATION_LLM_MODEL=gpt-4o-mini
```

## Next Steps

- **[Usage Guide](usage-guide.md)**: Detailed examples for all source types
- **[API Reference](api-reference.md)**: Complete method documentation
- **[Configuration](configuration.md)**: All environment variables and options
