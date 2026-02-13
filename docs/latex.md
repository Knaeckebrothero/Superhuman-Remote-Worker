# LaTeX Integration for the Writer Agent

Research into available options for enabling the writer agent to produce LaTeX papers.

## LaTeX MCP Servers — Detailed Comparison

### 1. Yeok-c/latex-mcp-server — Academic Paper Focus

- **Repo**: https://github.com/Yeok-c/latex-mcp-server
- **Tools**: Compile via `latexmk`, read PDFs from citations, parse BibTeX, extract PDF text+metadata, file listing
- **Standout**: Citation-aware — can resolve `\cite{key}` to actual PDFs and read them. Built for academic writing in VSCode + LaTeX Workshop
- **Install**: `uv tool install -e .`, stdio transport
- **Limitation**: Designed for local VSCode use, not HTTP mode

### 2. TexMCP — Lightweight, HTTP-ready

- **Repo**: https://github.com/devroopsaha744/TexMCP
- **Tools**: `render_latex_document`, `render_template_document`, `list_templates`, `list_artifacts`, `get_template`
- **Standout**: HTTP mode on port 8000, 15 built-in Jinja2 templates (thesis, report, presentation, etc.), Docker support
- **Install**: pip + optional pdflatex, or Docker
- **Best fit for the agent**: Has HTTP transport, so the LangGraph agent could call it as a sidecar service

### 3. latex-mcp (LachlanBridges) — Most Complete

- **Repo**: https://github.com/LachlanBridges/latex-mcp
- **Tools**: `compile_latex`, `list_templates`, `get_template`, `list_snippets`, `get_snippet_info`, `render_snippet`
- **Standout**: Multiple engines (pdflatex, xelatex, lualatex), security sandboxing (blocks dangerous commands, restricts filesystem), auto-cleanup, caching
- **Install**: Docker recommended, HTTP transport
- **Good for**: Production use — has the most guardrails

### 4. RobertoDure/mcp-latex-server — File Management Focus

- **Repo**: https://github.com/RobertoDure/mcp-latex-server
- **Tools**: Create, edit (replace/insert/append/prepend), read, list, validate syntax, extract structure
- **Standout**: Rich editing operations, structure extraction (sections/subsections), supports article/report/book/beamer/letter
- **Limitation**: Manages `.tex` files but **does not compile to PDF** — it's an editor, not a compiler

### 5. typst-mcp — Alternative to LaTeX Entirely

- **Repo**: https://github.com/johannesbrandenburger/typst-mcp
- **Tools**: LaTeX-to-Typst conversion (via Pandoc), syntax validation, render to PNG, access Typst docs
- **Standout**: [Typst](https://github.com/typst/typst) is a modern LaTeX alternative — simpler syntax, faster compilation, 10MB binary, no TeX distribution needed
- **Install**: Docker or Python, has Claude Code config ready
- **Trade-off**: LLMs generate LaTeX more reliably (more training data), but Typst is much simpler for an agent to get right

## Recommendation for the Writer Agent

For integrating with the LangGraph agent system, ranked by preference:

### Option A: TexMCP as a Docker sidecar (recommended)

- Already has HTTP mode (port 8000) — the agent tool just does an HTTP POST
- Add it to `docker-compose.dev.yaml`, create a thin `compile_latex` tool in `src/tools/`
- Built-in templates for thesis, report, etc. — good for academic papers
- Minimal code on our side

### Option B: Native `compile_latex` tool

- Install `texlive` in the agent's environment
- ~50 lines in `src/tools/latex/` — call `subprocess.run(["pdflatex", ...])`
- No extra service, no network calls
- But you lose templates, security sandboxing, and error formatting

### Option C: Typst

- Simpler syntax = fewer LLM errors, faster compilation, tiny binary
- But less LaTeX training data in LLMs, and if the university requires `.tex` submission, it's a non-starter
