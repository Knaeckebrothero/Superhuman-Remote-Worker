# Research Guide

You are entering a tactical phase. This guide covers research methodology for your exploration work.

## Research Workflow

1. **Define the question** — What specific thing are you trying to learn? Write it down before searching.
2. **Search broadly** — Use `web_search` with varied queries. Don't stop at the first result.
3. **Save sources** — Use `cite_web` or `cite_document` for every factual claim. No citation = no claim.
4. **Persist findings** — Write results to workspace files immediately. Don't hold findings only in memory.
5. **Synthesize** — After gathering sources, write a summary connecting findings to the task.

## Tool Usage

### Web Research
```
web_search(query="specific technical question")
extract_webpage(url="https://...")       # Get full content from a promising result
research_topic(topic="...", depth="moderate")  # Automated multi-source research
```

### Academic Sources
```
search_papers(query="...")               # Find academic papers
download_paper(url="...", filename="...") # Save to documents/
get_paper_info(identifier="...")         # Metadata lookup
```

### Document Analysis
```
read_file(path="documents/report.pdf")
read_file(path="documents/report.pdf", page_start=1, page_end=5)
get_document_info(path="documents/report.pdf")  # Metadata before reading
```

### Citations
Every factual or technical claim must cite its source:
```
cite_web(url="https://...", claim="Supporting statement from source")
cite_document(file_path="documents/report.pdf", page_or_section="p. 12", claim="Key finding")
```

## Output Conventions

- Save idea artifacts to `output/ideas/` — one file per idea with evidence and proposal.
- Save experiment results to `output/experiments/` — include methodology and findings.
- Save raw research notes to `notes/` — these are working files, not deliverables.
- Reference material goes in `reference/` — domain knowledge for future phases.

## Anti-Patterns

- Don't research without a question. "Learn about X" is not a research task. "What are the top 3 approaches to X and their tradeoffs?" is.
- Don't cite from memory. If you can't point to a source, it's not a fact — it's an assumption.
- Don't hold findings in context only. Context gets compacted. Write findings to files.
- Don't deep-dive on the first interesting result. Breadth first, depth second.
