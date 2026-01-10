"""Tool description generator for agent workspaces.

Generates markdown documentation for tools that agents can reference
during execution. This enables on-demand tool documentation loading
to reduce context window usage.
"""

import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)


def get_tool_docstring(tool_name: str) -> Optional[str]:
    """Get the full docstring for a tool by name.

    This imports the tool module and extracts the docstring from
    the actual tool function.

    Args:
        tool_name: Name of the tool (e.g., "read_file", "write_requirement_to_cache")

    Returns:
        The tool's docstring, or None if not found
    """
    if tool_name not in TOOL_REGISTRY:
        return None

    metadata = TOOL_REGISTRY[tool_name]
    module_name = metadata.get("module", "")

    # Map module names to actual tool creation functions
    # We need to get the docstring from the nested @tool function
    docstrings = _get_hardcoded_docstrings()

    return docstrings.get(tool_name)


def _get_hardcoded_docstrings() -> Dict[str, str]:
    """Get docstrings for all tools.

    Since the actual tool functions are created dynamically inside
    factory functions, we extract them here for documentation purposes.
    """
    return {
        # Workspace tools
        "read_file": """Read content from a file in the workspace.

Use this to retrieve files, read instructions, or access documents.
For PDF files, supports page-based access with auto-pagination.

**Arguments:**
- `path` (str): Relative path to the file (e.g., "documents/GoBD.pdf")
- `page_start` (int, optional): For PDFs: first page to read (1-indexed, default: 1)
- `page_end` (int, optional): For PDFs: last page to read (default: auto-limit by size)

**Returns:** File content or error message. For PDFs, includes page info and continuation guidance.

**Examples:**
```
read_file("instructions.md")                           # Read text file
read_file("documents/GoBD.pdf")                        # Read PDF (auto-paginates)
read_file("documents/GoBD.pdf", page_start=5, page_end=10)  # Read specific pages
```

**PDF Behavior:**
- Without page_end: Reads pages until reaching size limit (~25KB), then provides continuation guidance
- With page_end: Reads exactly the specified page range
- Each page is marked with [PAGE N] headers for reference""",

        "write_file": """Write content to a file in the workspace.

Creates parent directories automatically if they don't exist.
Overwrites the file if it already exists.

**Use this to:**
- Create plans (plans/main_plan.md)
- Save research notes (notes/research.md)
- Write intermediate results (candidates/candidates.md)
- Store processed data (chunks/chunk_001.md)

**Arguments:**
- `path` (str): Relative path for the file (e.g., "notes/research.md")
- `content` (str): Content to write

**Returns:** Confirmation message with file path and size

**Example:**
```
write_file("notes/findings.md", "# Findings\\n\\n- Found 3 requirements...")
```""",

        "append_file": """Append content to an existing file in the workspace.

Creates the file if it doesn't exist.
Use this for log-style files or incremental updates.

**Arguments:**
- `path` (str): Relative path to the file
- `content` (str): Content to append

**Returns:** Confirmation message""",

        "list_files": """List files and directories in a workspace path.

Directories are shown with a trailing slash.
Use this to explore your workspace structure.

**Arguments:**
- `path` (str, optional): Relative directory path (empty for workspace root)
- `pattern` (str, optional): Glob pattern to filter files (e.g., "*.md", "*.json")

**Returns:** List of files and directories, or message if empty

**Example:**
```
list_files()  # List workspace root
list_files("chunks")  # List all chunks
list_files("notes", "*.md")  # List only markdown files in notes
```""",

        "delete_file": """Delete a file or empty directory from the workspace.

Cannot delete non-empty directories (delete contents first).

**Arguments:**
- `path` (str): Relative path to delete

**Returns:** Confirmation or error message""",

        "search_files": """Search for text content in workspace files.

Searches through all text files and returns matching lines
with file paths and line numbers.

**Arguments:**
- `query` (str): Text or pattern to search for
- `path` (str, optional): Directory to search in (empty for entire workspace)
- `case_sensitive` (bool, optional): Whether to match case exactly (default: False)

**Returns:** Search results with file paths, line numbers, and matching lines

**Example:**
```
search_files("GoBD")  # Find all mentions of GoBD
search_files("requirement", "candidates")  # Search only in candidates folder
```""",

        "file_exists": """Check if a file or directory exists in the workspace.

**Arguments:**
- `path` (str): Relative path to check

**Returns:** "exists" or "not found" message with type (file/directory) and size""",

        "get_workspace_summary": """Get a summary of the current workspace state.

Returns information about the workspace including:
- Job ID
- Directory structure
- File counts per directory
- Total size

Use this to understand what's in your workspace.

**Returns:** Workspace summary with statistics""",

        "get_document_info": """Get metadata about a document without reading its full content.

Use this to plan how to read large documents, especially PDFs.
Returns page count, estimated size, and reading suggestions.

**Arguments:**
- `path` (str): Relative path to the document (e.g., "documents/GoBD.pdf")

**Returns:** Document information including:
- Page count (for PDFs)
- Estimated characters/tokens
- File size in bytes
- Document metadata (title, author if available)
- Suggested reading approach based on document size

**Example:**
```
get_document_info("documents/GoBD.pdf")
```

**Sample Output:**
```
Document: GoBD.pdf
Pages: 45
File size: 2,300,000 bytes
Estimated content: ~180,000 chars (~45,000 tokens)
Average per page: ~4,000 chars (~1,000 tokens)

Suggested approach (document exceeds single-read limit):
- Read ~6 pages at a time
- Start with: read_file("documents/GoBD.pdf", page_start=1, page_end=6)
- Continue with: read_file("documents/GoBD.pdf", page_start=7, page_end=12)
```""",

        # Todo tools
        "add_todo": """Add a task to the current todo list.

Use this to track concrete, actionable steps for the current phase.
Break complex work into 10-20 small, specific tasks.

**Arguments:**
- `content` (str): Task description (be specific and actionable)
- `priority` (int, optional): Higher number = more important (default 0)

**Returns:** Confirmation with todo ID

**Examples:**
```
add_todo("Extract text from document.pdf")
add_todo("Validate requirement REQ-001 against metamodel", priority=1)
```""",

        "complete_todo": """Mark a todo as complete.

Call this immediately after finishing a task.
Optionally add notes about what was accomplished or discovered.

**Arguments:**
- `todo_id` (str): The todo ID (e.g., "todo_1")
- `notes` (str, optional): Completion notes (findings, decisions, etc.)

**Returns:** Confirmation or error message""",

        "start_todo": """Mark a todo as in-progress.

Use this when you begin working on a task to track what's active.

**Arguments:**
- `todo_id` (str): The todo ID to start (e.g., "todo_1")

**Returns:** Confirmation or error message""",

        "list_todos": """List all current todos with their status.

Shows todos organized by status with visual indicators:
- ○ pending
- ◐ in progress
- ● completed
- ✗ blocked
- − skipped

**Returns:** Formatted list of all todos""",

        "get_progress": """Get progress summary for current todos.

Shows completion statistics to track phase progress.

**Returns:** Progress summary with counts and percentage""",

        "archive_and_reset": """Archive completed todos and reset for next phase.

This tool:
1. Saves all current todos to workspace/archive/todos_<phase>_<timestamp>.md
2. Clears the todo list
3. Returns confirmation prompting you to add new todos

**Use this when:**
- Completing a phase of work
- Before transitioning to a different type of task
- When instructed to archive progress

**Arguments:**
- `phase_name` (str, optional): Name for the archived phase (e.g., "phase_1_extraction")

**Returns:** Confirmation with archive path or error message""",

        "get_next_todo": """Get the next pending task to work on.

Returns the highest priority pending todo.

**Returns:** Next todo to work on or message if none pending""",

        # Document tools
        "extract_document_text": """Extract text content from a document file.

Supports PDF, DOCX, TXT, and HTML formats.
The full text is automatically written to `extracted/<filename>_full_text.txt`.
The document is also registered as a citation source.

**Arguments:**
- `file_path` (str): Path to the document. Can be relative to workspace
  (e.g., "documents/file.pdf") or absolute.

**Returns:** Extraction result with metadata (page count, language, type, character count)

**Example:**
```
extract_document_text("documents/GoBD_example.pdf")
```""",

        "chunk_document": """Split a document into chunks for processing.

Writes chunks to `chunks/chunk_001.txt` through `chunks/chunk_XXX.txt`.

**Arguments:**
- `file_path` (str): Path to the document (relative to workspace or absolute)
- `strategy` (str, optional): Chunking strategy - 'legal', 'technical', 'general', or 'by_page' (default: 'legal')
- `max_chunk_size` (int, optional): Override max chars per chunk (default: uses preset)
- `overlap` (int, optional): Override overlap between chunks (default: uses preset)

**Strategies:**
- **legal**: Large chunks (~5000 chars) respecting section boundaries (Article, Section, Paragraph)
- **technical**: Medium chunks (~3500 chars) preserving code blocks and technical structure
- **general**: Standard chunks (~2000 chars) with simple character-based splitting
- **by_page**: One chunk per page - useful for page-level citations and analysis

**Returns:** Summary of chunking results with statistics

**Examples:**
```
chunk_document("documents/contract.pdf")                     # Uses legal strategy
chunk_document("documents/api_spec.pdf", strategy="technical")
chunk_document("documents/report.pdf", strategy="by_page")   # One chunk per page
```""",

        "identify_requirement_candidates": """Identify requirement-like statements in text.

Analyzes text to find statements that may be requirements based on:
- Modal verbs (must, shall, should, muss, soll)
- Constraint patterns (at least, maximum, within X days)
- Compliance references (in accordance with, gemäß)

**Arguments:**
- `text` (str): Text to analyze for requirements
- `mode` (str, optional): Detection mode - 'strict', 'balanced', or 'permissive' (default: 'balanced')

**Returns:** List of identified candidates with confidence scores and classifications""",

        "assess_gobd_relevance": """Assess whether text is relevant to GoBD compliance.

GoBD (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung)
defines German requirements for electronic bookkeeping.

**GoBD Indicators:**
- Aufbewahrung/Aufbewahrungspflicht (retention)
- Nachvollziehbarkeit (traceability)
- Unveränderbarkeit (immutability)
- Revisionssicherheit (audit-proof)
- Protokollierung (logging)

**Arguments:**
- `text` (str): Text to assess

**Returns:** GoBD relevance assessment with confidence score and matched indicators""",

        "extract_entity_mentions": """Find business object and message mentions in text.

Identifies references to domain entities that may need to be linked
to graph nodes (BusinessObject, Message, Requirement).

**Arguments:**
- `text` (str): Text to analyze

**Returns:** Extracted entity mentions organized by type (objects, messages, requirements)""",

        # Search tools
        "web_search": """Search the web for context using Tavily.

Use this to find additional context, verify claims, or research topics.
Results include title, URL, and content snippet.

**Arguments:**
- `query` (str): Search query
- `max_results` (int, optional): Maximum results to return (default: 5)

**Returns:** Search results with snippets and URLs

**Tip:** After getting results, use `cite_web()` to create citations for any information you reference.

**Example:**
```
web_search("GoBD retention requirements Germany")
```""",

        "query_similar_requirements": """Find similar requirements in the Neo4j graph.

Use this to:
- Check for duplicates before creating new requirements
- Find related requirements for context
- Identify patterns across requirements

**Arguments:**
- `text` (str): Requirement text to match against
- `limit` (int, optional): Maximum results (default: 5)

**Returns:** Similar requirements with similarity scores

**Interpretation:**
- Similarity > 0.9 = Likely duplicate
- Similarity 0.7-0.9 = Related, may need review
- Similarity < 0.7 = Distinct""",

        # Citation tools
        "cite_document": """Create a verified citation for document content.

Registers the document as a source (if not already registered) and creates
a citation linking your claim to the quoted text. The citation is verified
against the source content.

**Arguments:**
- `text` (str): Quoted text from the document (the evidence)
- `document_path` (str): Path to the source document
- `page` (int, optional): Page number if applicable
- `section` (str, optional): Section reference if applicable
- `claim` (str, optional): The assertion being supported (defaults to summary of text)

**Returns:** Citation ID and verification status. Use [N] format in your text.

**Example:**
```
cite_document(
    text="Invoices must be retained for 10 years",
    document_path="documents/GoBD.pdf",
    page=12,
    section="3.2"
)
```""",

        "cite_web": """Create a verified citation for web content.

Registers the URL as a source (fetching and archiving its content) and creates
a citation linking your claim to the quoted text.

**Arguments:**
- `text` (str): Quoted/paraphrased text from the web (the evidence)
- `url` (str): Source URL
- `title` (str, optional): Page title (auto-detected if not provided)
- `accessed_date` (str, optional): Date accessed in ISO format (defaults to today)
- `claim` (str, optional): The assertion being supported

**Returns:** Citation ID and verification status. Use [N] format in your text.""",

        "list_sources": """List all registered citation sources.

Shows all document, web, database, and custom sources that have been
registered for citations in this session.

**Returns:** Formatted list of sources with IDs and types""",

        "get_citation": """Get details about a specific citation.

Retrieves the full citation record including claim, source, verification
status, and similarity score.

**Arguments:**
- `citation_id` (int): The numeric citation ID (without brackets)

**Returns:** Detailed citation information""",

        # Cache tools
        "write_requirement_to_cache": """Write a requirement to the PostgreSQL cache for validation.

This is how you submit completed requirements for the Validator Agent to process.
The requirement will be validated and integrated into the knowledge graph.

**Arguments:**
- `text` (str): Full requirement text
- `name` (str): Short name/title (max 80 chars)
- `req_type` (str, optional): Type - 'functional', 'compliance', 'constraint', or 'non_functional' (default: 'functional')
- `priority` (str, optional): Priority - 'high', 'medium', or 'low' (default: 'medium')
- `gobd_relevant` (bool, optional): GoBD relevance flag (default: False)
- `gdpr_relevant` (bool, optional): GDPR relevance flag (default: False)
- `source_document` (str, optional): Source document path
- `source_location` (str, optional): Location in document (e.g., "Section 3.2")
- `citations` (str, optional): Comma-separated citation IDs
- `mentioned_objects` (str, optional): Comma-separated BusinessObject names
- `mentioned_messages` (str, optional): Comma-separated Message names
- `reasoning` (str, optional): Extraction reasoning
- `confidence` (float, optional): Confidence score 0.0-1.0 (default: 0.8)

**Returns:** Requirement ID and confirmation

**Example:**
```
write_requirement_to_cache(
    text="The system must retain all invoice records for at least 10 years in accordance with GoBD requirements.",
    name="GoBD Invoice Retention",
    req_type="compliance",
    priority="high",
    gobd_relevant=True,
    source_document="documents/GoBD.pdf",
    source_location="Section 3.2",
    mentioned_objects="Invoice,Record",
    confidence=0.92
)
```""",

        # Completion tools
        "mark_complete": """Signal that the assigned task is complete.

Call this tool ONLY when you have finished all work on the task.
This will write a completion report to `output/completion.json` and end the agent loop.

**Arguments:**
- `summary` (str): Brief description of what was accomplished (1-3 sentences)
- `deliverables` (list): List of output files or artifacts created
  (e.g., ["output/requirements.json", "notes/analysis.md"])
- `confidence` (float, optional): Your confidence the task is truly complete 0.0-1.0 (default: 1.0)
- `notes` (str, optional): Notes about limitations, assumptions, or follow-up suggestions

**Returns:** Confirmation message

**Example:**
```
mark_complete(
    summary="Extracted 15 GoBD-relevant requirements from the document",
    deliverables=["output/requirements.json", "candidates/candidates.md"],
    confidence=0.95,
    notes="3 requirements need manual review due to ambiguous language"
)
```""",

        # Graph tools (for Validator)
        "execute_cypher_query": """Execute a Cypher query against the Neo4j database.

**Arguments:**
- `query` (str): A valid Cypher query string

**Returns:** String representation of query results (up to 50 records)

Use this to explore the graph, find entities, check relationships.

**Example:**
```
execute_cypher_query("MATCH (r:Requirement) RETURN r.rid, r.name LIMIT 10")
```""",

        "get_database_schema": """Get the Neo4j database schema.

**Returns:** Schema information with node labels, relationship types, and properties""",

        "find_similar_requirements": """Find existing requirements similar to the given text.

**Arguments:**
- `text` (str): Requirement text to compare
- `threshold` (float, optional): Minimum similarity score 0.0-1.0 (default: 0.7)

**Returns:** List of similar requirements with similarity scores""",

        "check_for_duplicates": """Check if a requirement text is a duplicate.

Uses high-threshold similarity (95%) to identify near-exact duplicates.

**Arguments:**
- `text` (str): Requirement text to check

**Returns:** Duplicate check result with recommendation (REJECT if duplicate found)""",

        "resolve_business_object": """Resolve a business object mention to an existing graph entity.

**Arguments:**
- `mention` (str): Text mention of a business object (e.g., "Customer", "Invoice")

**Returns:** Matched entity details (BOID, name, domain) or no match message""",

        "resolve_message": """Resolve a message mention to an existing graph entity.

**Arguments:**
- `mention` (str): Text mention of a message (e.g., "CreateOrderRequest")

**Returns:** Matched entity details (MID, name, direction) or no match message""",

        "validate_schema_compliance": """Run metamodel compliance checks against the graph.

**Arguments:**
- `check_type` (str, optional): Type of checks to run:
  - "all": All checks (default)
  - "structural": Node labels and properties (A1-A3)
  - "relationships": Relationship types and directions (B1-B3)
  - "quality": Quality gates (C1-C5)

**Returns:** Compliance report with pass/fail status and violations""",

        "create_requirement_node": """Create a new Requirement node in the Neo4j graph.

**Arguments:**
- `rid` (str): Requirement ID (e.g., "R-0042")
- `name` (str): Short descriptive name
- `text` (str): Full requirement text
- `req_type` (str, optional): Type - functional, non_functional, constraint, compliance (default: functional)
- `priority` (str, optional): Priority - high, medium, low (default: medium)
- `gobd_relevant` (bool, optional): GoBD relevance flag (default: False)
- `gdpr_relevant` (bool, optional): GDPR relevance flag (default: False)
- `compliance_status` (str, optional): Status - open, partial, fulfilled (default: open)

**Returns:** Creation result with node ID""",

        "create_fulfillment_relationship": """Create a fulfillment relationship between a Requirement and an entity.

**Arguments:**
- `requirement_rid` (str): Source requirement RID (e.g., "R-0042")
- `entity_id` (str): Target entity ID (boid for BusinessObject, mid for Message)
- `entity_type` (str): "BusinessObject" or "Message"
- `relationship_type` (str): FULFILLED_BY_OBJECT, NOT_FULFILLED_BY_OBJECT, FULFILLED_BY_MESSAGE, NOT_FULFILLED_BY_MESSAGE
- `confidence` (float, optional): Confidence score 0.0-1.0 (default: 0.5)
- `evidence` (str, optional): Evidence text for the relationship

**Returns:** Creation result""",

        "generate_requirement_id": """Generate a new unique requirement ID.

**Returns:** New RID following the R-XXXX pattern (e.g., "R-0042")""",

        "get_entity_relationships": """Get all relationships for a BusinessObject or Message.

**Arguments:**
- `entity_id` (str): Entity ID (boid or mid)
- `entity_type` (str): "BusinessObject" or "Message"

**Returns:** List of relationships involving the entity""",

        "count_graph_statistics": """Get statistics about the current graph state.

**Returns:** Counts of nodes and relationships by type""",
    }


def generate_tool_description(tool_name: str) -> str:
    """Generate a markdown description for a single tool.

    Args:
        tool_name: Name of the tool

    Returns:
        Markdown-formatted tool description
    """
    if tool_name not in TOOL_REGISTRY:
        return f"# {tool_name}\n\n*Tool not found in registry.*\n"

    metadata = TOOL_REGISTRY[tool_name]
    docstring = get_tool_docstring(tool_name)

    # Build markdown
    lines = [
        f"# {tool_name}",
        "",
        f"**Category:** {metadata.get('category', 'unknown')}",
        "",
    ]

    if docstring:
        lines.append(docstring)
    else:
        lines.append(metadata.get("description", "No description available."))

    lines.append("")

    return "\n".join(lines)


def generate_tool_index(tool_names: List[str]) -> str:
    """Generate an index markdown file listing all available tools.

    Args:
        tool_names: List of tool names to include

    Returns:
        Markdown-formatted index
    """
    # Group tools by category
    categories: Dict[str, List[str]] = {}

    for name in tool_names:
        if name in TOOL_REGISTRY:
            category = TOOL_REGISTRY[name].get("category", "other")
        else:
            category = "other"

        if category not in categories:
            categories[category] = []
        categories[category].append(name)

    # Build index
    lines = [
        "# Available Tools",
        "",
        "This directory contains detailed documentation for all tools available to this agent.",
        "Read a tool's documentation file to understand how to use it.",
        "",
        "## Quick Reference",
        "",
    ]

    # Category order
    category_order = ["workspace", "todo", "domain", "completion", "other"]

    for category in category_order:
        if category not in categories:
            continue

        tools = sorted(categories[category])
        category_title = category.replace("_", " ").title()

        lines.append(f"### {category_title} Tools")
        lines.append("")

        for tool_name in tools:
            desc = TOOL_REGISTRY.get(tool_name, {}).get("description", "")
            lines.append(f"- **[{tool_name}]({tool_name}.md)** - {desc}")

        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*To use a tool, read its documentation file for detailed arguments and examples.*")

    return "\n".join(lines)


def generate_workspace_tool_docs(
    tool_names: List[str],
    output_dir: Path,
) -> int:
    """Generate tool documentation files in a workspace directory.

    Creates:
    - tools/README.md - Index of all tools
    - tools/<tool_name>.md - Detailed doc for each tool

    Args:
        tool_names: List of tool names to document
        output_dir: Path to the tools/ directory in workspace

    Returns:
        Number of files created
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    files_created = 0

    # Generate index
    index_content = generate_tool_index(tool_names)
    index_path = output_dir / "README.md"
    index_path.write_text(index_content, encoding="utf-8")
    files_created += 1
    logger.debug(f"Generated tool index: {index_path}")

    # Generate individual tool docs
    for tool_name in tool_names:
        doc_content = generate_tool_description(tool_name)
        doc_path = output_dir / f"{tool_name}.md"
        doc_path.write_text(doc_content, encoding="utf-8")
        files_created += 1

    logger.info(f"Generated {files_created} tool documentation files in {output_dir}")

    return files_created
