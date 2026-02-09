"""Citation tools for the Universal Agent.

Provides document and web citation capabilities for requirement traceability.
Integrates with CitationEngine for verified, persistent citations.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
CITATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "cite_document": {
        "module": "citation.sources",
        "function": "cite_document",
        "description": "Create a verified citation for document content",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Create verified citation for document content.",
        "phases": ["strategic", "tactical"],
    },
    "cite_web": {
        "module": "citation.sources",
        "function": "cite_web",
        "description": "Create a verified citation for web content",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Create verified citation for web content.",
        "phases": ["strategic", "tactical"],
    },
    "list_sources": {
        "module": "citation.sources",
        "function": "list_sources",
        "description": "List all registered citation sources",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "List all registered citation sources.",
        "phases": ["strategic", "tactical"],
    },
    "get_citation": {
        "module": "citation.sources",
        "function": "get_citation",
        "description": "Get details about a specific citation",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Get details about a specific citation by ID.",
        "phases": ["strategic", "tactical"],
    },
    "list_citations": {
        "module": "citation.sources",
        "function": "list_citations",
        "description": "List all citations created in this session",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "List all citations with status and source info.",
        "phases": ["strategic", "tactical"],
    },
    "edit_citation": {
        "module": "citation.sources",
        "function": "edit_citation",
        "description": "Edit fields of an existing citation",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Edit citation fields (claim, quote, confidence, etc.).",
        "phases": ["strategic", "tactical"],
    },
    "annotate_source": {
        "module": "citation.sources",
        "function": "annotate_source",
        "description": "Add a note, highlight, summary, question, or critique to a source",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Add annotation to a citation source.",
        "phases": ["strategic", "tactical"],
    },
    "get_annotations": {
        "module": "citation.sources",
        "function": "get_annotations",
        "description": "Get annotations for a source",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Get annotations for a citation source.",
        "phases": ["strategic", "tactical"],
    },
    "tag_source": {
        "module": "citation.sources",
        "function": "tag_source",
        "description": "Add or remove tags on a citation source",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Add or remove tags on a citation source.",
        "phases": ["strategic", "tactical"],
    },
    "search_library": {
        "module": "citation.sources",
        "function": "search_library",
        "description": "Search the source library using keyword, semantic, or hybrid search",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Search source library with hybrid retrieval and evidence labels.",
        "phases": ["strategic", "tactical"],
    },
    "generate_bibliography": {
        "module": "citation.sources",
        "function": "generate_bibliography",
        "description": "Generate a formatted bibliography/references file from citations",
        "category": "citation",
        "defer_to_workspace": True,
        "short_description": "Generate formatted bibliography file from citations.",
        "phases": ["strategic", "tactical"],
    },
}


def _format_stub_document_citation(
    citation_id: str,
    document_path: str,
    page: Optional[int],
    section: Optional[str],
    text: str,
) -> str:
    """Format citation result for stub/fallback mode."""
    return f"""Citation Created (Stub Mode)

Citation ID: {citation_id}
Source Type: Document
Document: {document_path}
Page: {page or 'N/A'}
Section: {section or 'N/A'}

Quoted Text:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Note: CitationEngine not available. Citation stored in stub mode only.
Use this citation_id when writing requirements."""


def _format_stub_web_citation(
    citation_id: str,
    url: str,
    title: Optional[str],
    accessed_date: str,
    text: str,
) -> str:
    """Format web citation result for stub/fallback mode."""
    return f"""Web Citation Created (Stub Mode)

Citation ID: {citation_id}
Source Type: Web
URL: {url}
Title: {title or 'Untitled'}
Accessed: {accessed_date}

Content:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Note: CitationEngine not available. Citation stored in stub mode only.
Use this citation_id when writing requirements."""


def create_source_tools(context: ToolContext) -> List[Any]:
    """Create citation tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Get workspace manager for path resolution
    workspace = context.workspace_manager if context.has_workspace() else None

    def resolve_path(path: str) -> str:
        """Resolve a path relative to workspace if available.

        Args:
            path: Relative or absolute path

        Returns:
            Absolute path string
        """
        if workspace is not None:
            resolved = workspace.get_path(path)
            return str(resolved)
        return path

    @tool
    def cite_document(
        text: str,
        document_path: str,
        page: Optional[int] = None,
        section: Optional[str] = None,
        claim: Optional[str] = None,
    ) -> str:
        """Create a verified citation for document content.

        Registers the document as a source (if not already registered) and creates
        a citation linking your claim to the quoted text. The citation is verified
        against the source content using an LLM.

        Tip: Use search_library first to find relevant evidence across all sources,
        then cite the specific passage with this tool.

        Args:
            text: Quoted text from the document (the evidence)
            document_path: Path to the source document
            page: Page number if applicable
            section: Section reference if applicable
            claim: The assertion being supported (defaults to summary of text)

        Returns:
            Citation ID and verification status. Use [N] format in your text.
        """
        try:
            # Try to use CitationEngine
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                # Fallback to stub behavior
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                logger.warning("CitationEngine not installed, using stub mode")
                return _format_stub_document_citation(
                    citation_id, document_path, page, section, text
                )

            # Register source and create citation
            try:
                resolved_path = resolve_path(document_path)
                source_id = context.get_or_register_doc_source(
                    resolved_path, name=Path(document_path).name
                )
            except FileNotFoundError:
                return f"Error: Document not found at {document_path}"
            except Exception as e:
                logger.warning(f"Could not register document source: {e}")
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                return _format_stub_document_citation(
                    citation_id, document_path, page, section, text
                )

            # Build locator dict
            locator = {}
            if page is not None:
                locator["page"] = page
            if section:
                locator["section"] = section

            # Create the citation
            engine = context.get_citation_engine()
            effective_claim = claim or f"Evidence from document: {text[:100]}..."
            result = engine.cite_doc(
                claim=effective_claim,
                source_id=source_id,
                quote_context=text,
                locator=locator,
                verbatim_quote=text[:500] if len(text) < 1000 else None,
                relevance_reasoning=f"Direct quote from source document supporting: {effective_claim[:100]}",
                confidence="high",
                extraction_method="direct_quote",
            )

            # Format result for agent
            status = result.verification_status.value
            citation_ref = f"[{result.citation_id}]"
            similarity = f"{result.similarity_score:.2f}" if result.similarity_score else "N/A"

            output = f"""Citation Created

Citation ID: {citation_ref}
Source Type: Document
Document: {document_path}
Page: {page or 'N/A'}
Section: {section or 'N/A'}
Status: {status.upper()}
Similarity Score: {similarity}
"""
            if result.verification_notes:
                output += f"\nNote: {result.verification_notes}"

            output += f"\n\nUse {citation_ref} when referencing this information in your text."

            return output

        except Exception as e:
            logger.error(f"Citation creation error: {e}")
            return f"Error creating citation: {str(e)}"

    @tool
    def cite_web(
        text: str,
        url: str,
        title: Optional[str] = None,
        accessed_date: Optional[str] = None,
        claim: Optional[str] = None,
    ) -> str:
        """Create a verified citation for web content.

        Registers the URL as a source (fetching and archiving its content) and creates
        a citation linking your claim to the quoted text. The citation is verified
        against the archived content using an LLM.

        Tip: Use search_library first to find relevant evidence across all sources,
        then cite the specific passage with this tool.

        Args:
            text: Quoted/paraphrased text from the web (the evidence)
            url: Source URL
            title: Page title (auto-detected if not provided)
            accessed_date: Date accessed in ISO format (defaults to today)
            claim: The assertion being supported (defaults to summary of text)

        Returns:
            Citation ID and verification status. Use [N] format in your text.
        """
        try:
            # Use today's date if not provided
            if not accessed_date:
                accessed_date = datetime.utcnow().strftime('%Y-%m-%d')

            # Try to use CitationEngine
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                logger.warning("CitationEngine not installed, using stub mode")
                return _format_stub_web_citation(
                    citation_id, url, title, accessed_date, text
                )

            # Register source and create citation
            try:
                source_id, fetch_error = context.get_or_register_web_source(url, name=title)
            except Exception as e:
                logger.warning(f"Could not register web source: {e}")
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                return _format_stub_web_citation(
                    citation_id, url, title, accessed_date, text
                )

            # Build locator dict
            locator = {"accessed_at": accessed_date}
            if title:
                locator["title"] = title

            # Create the citation
            engine = context.get_citation_engine()
            effective_claim = claim or f"Information from web source: {text[:100]}..."
            result = engine.cite_web(
                claim=effective_claim,
                source_id=source_id,
                quote_context=text,
                locator=locator,
                verbatim_quote=text[:500] if len(text) < 1000 else None,
                relevance_reasoning=f"Content from web source supporting: {effective_claim[:100]}",
                confidence="high",
                extraction_method="direct_quote",
            )

            # Format result
            status = result.verification_status.value
            citation_ref = f"[{result.citation_id}]"
            similarity = f"{result.similarity_score:.2f}" if result.similarity_score else "N/A"

            output = f"""Web Citation Created

Citation ID: {citation_ref}
Source Type: Web
URL: {url}
Title: {title or 'Untitled'}
Accessed: {accessed_date}
Status: {status.upper()}
Similarity Score: {similarity}
"""
            if result.verification_notes:
                output += f"\nNote: {result.verification_notes}"

            output += f"\n\nUse {citation_ref} when referencing this information in your text."

            return output

        except Exception as e:
            logger.error(f"Web citation error: {e}")
            return f"Error creating web citation: {str(e)}"

    @tool
    def list_sources() -> str:
        """List citation sources registered by this job.

        Shows document, web, database, and custom sources that have been
        registered for citations in the current job. Sources from other jobs
        are not visible.

        Returns:
            Formatted list of sources with IDs and types
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No sources available."

            engine = context.get_citation_engine()
            # list_sources() now filters by job_id from context automatically
            sources = engine.list_sources()

            if not sources:
                return "No sources registered yet. Use cite_document or cite_web to register sources."

            lines = [f"Registered Sources ({len(sources)} total):", ""]
            for source in sources:
                lines.append(f"  [{source.id}] {source.type.value}: {source.name}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Error listing sources: {e}")
            return f"Error listing sources: {str(e)}"

    @tool
    def get_citation(citation_id: int) -> str:
        """Get details about a specific citation from this job.

        Retrieves the full citation record including claim, source, verification
        status, and similarity score. Only citations belonging to the current job
        are accessible.

        Args:
            citation_id: The numeric citation ID (without brackets)

        Returns:
            Detailed citation information, or not found if citation doesn't exist
            or belongs to another job
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            # get_citation() now filters by job_id from context automatically
            citation = engine.get_citation(citation_id)

            if not citation:
                return f"Citation [{citation_id}] not found."

            source = engine.get_source(citation.source_id)

            similarity = f"{citation.similarity_score:.2f}" if citation.similarity_score else "N/A"
            source_name = source.name if source else "Unknown"
            source_type = f" ({source.type.value})" if source else ""

            output = f"Citation [{citation_id}]\n"
            output += f"\nSource: [{source.id}] {source_name}{source_type}"

            if citation.locator:
                loc_parts = [f"{k.capitalize()} {v}" for k, v in citation.locator.items() if v]
                if loc_parts:
                    output += f"\nLocation: {', '.join(loc_parts)}"

            output += f"\nClaim: {citation.claim}"

            if citation.verbatim_quote:
                quote = citation.verbatim_quote[:500] + "..." if len(citation.verbatim_quote) > 500 else citation.verbatim_quote
                output += f'\n\nQuote: "{quote}"'

            if citation.quote_context:
                ctx = citation.quote_context[:500] + "..." if len(citation.quote_context) > 500 else citation.quote_context
                output += f"\nContext: {ctx}"

            if citation.quote_language:
                output += f"\nLanguage: {citation.quote_language}"

            if citation.extraction_method:
                output += f"\nExtraction: {citation.extraction_method.value}"

            if citation.relevance_reasoning:
                output += f"\nReasoning: {citation.relevance_reasoning}"

            output += f"\n\nStatus: {citation.verification_status.value.upper()} | Confidence: {citation.confidence.value} | Similarity: {similarity}"

            if citation.verification_notes:
                output += f"\nNotes: {citation.verification_notes}"

            return output

        except Exception as e:
            logger.error(f"Error getting citation: {e}")
            return f"Error getting citation: {str(e)}"

    @tool
    def list_citations(source_id: Optional[int] = None, status: Optional[str] = None) -> str:
        """List citations created by this job.

        Shows citation IDs, claims (truncated), verification status, and source.
        Optionally filter by source ID or verification status. Citations from
        other jobs are not visible.

        Args:
            source_id: Filter by source ID (optional)
            status: Filter by verification status: pending, verified, failed (optional)

        Returns:
            Formatted list of citations for the current job
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No citations available."

            engine = context.get_citation_engine()
            # list_citations() now filters by job_id from context automatically
            # Pass filters directly to engine for efficiency
            citations = engine.list_citations(
                source_id=source_id,
                verification_status=status,
            )

            if not citations:
                if source_id is not None or status is not None:
                    return "No citations match the given filters."
                return "No citations created yet. Use cite_document or cite_web to create citations."

            lines = [f"Citations ({len(citations)} total):", ""]
            for c in citations:
                claim_preview = c.claim[:50] + "..." if len(c.claim) > 50 else c.claim
                confidence = f" ({c.confidence.value})" if c.confidence else ""
                status_str = c.verification_status.value.upper()
                lines.append(
                    f'  [{c.id}] {status_str}{confidence} — Source [{c.source_id}] "{claim_preview}"'
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Error listing citations: {e}")
            return f"Error listing citations: {str(e)}"

    @tool
    async def edit_citation(
        citation_id: int,
        claim: Optional[str] = None,
        verbatim_quote: Optional[str] = None,
        quote_context: Optional[str] = None,
        relevance_reasoning: Optional[str] = None,
        confidence: Optional[str] = None,
        extraction_method: Optional[str] = None,
        locator: Optional[str] = None,
    ) -> str:
        """Edit fields of an existing citation belonging to this job.

        When content fields (claim, verbatim_quote, quote_context) are changed,
        verification_status is automatically reset to 'pending' and previous
        verification results are cleared, since the old verification is no
        longer valid.

        Only citations belonging to the current job can be edited.

        Args:
            citation_id: The numeric citation ID (without brackets)
            claim: The assertion being supported
            verbatim_quote: Exact quote from source
            quote_context: Context around the quote
            relevance_reasoning: Why this citation is relevant
            confidence: Confidence level (high, medium, low)
            extraction_method: How extracted (direct_quote, paraphrase, inference, aggregation, negative)
            locator: Location reference as JSON string (e.g., '{"page": 5, "section": "3.2"}')

        Returns:
            "ok: edited citation [N]" on success, "error: {reason}" on failure
        """
        try:
            if not context.db:
                return "error: no database connection"

            # Verify citation belongs to current job before editing
            try:
                from citation_engine import CitationEngine  # noqa: F401
                engine = context.get_citation_engine()
                # get_citation filters by job_id from context
                citation = engine.get_citation(citation_id)
                if not citation:
                    return f"error: citation [{citation_id}] not found"
            except ImportError:
                pass  # If CitationEngine not available, skip ownership check

            # Build kwargs for edit, only including non-None values
            kwargs = {}
            if claim is not None:
                kwargs["claim"] = claim
            if verbatim_quote is not None:
                kwargs["verbatim_quote"] = verbatim_quote
            if quote_context is not None:
                kwargs["quote_context"] = quote_context
            if relevance_reasoning is not None:
                kwargs["relevance_reasoning"] = relevance_reasoning
            if confidence is not None:
                kwargs["confidence"] = confidence
            if extraction_method is not None:
                kwargs["extraction_method"] = extraction_method
            if locator is not None:
                try:
                    kwargs["locator"] = json.loads(locator)
                except json.JSONDecodeError:
                    return "error: locator must be valid JSON"

            if not kwargs:
                return "error: no fields provided to edit"

            content_changed = any(
                v is not None for v in [claim, verbatim_quote, quote_context]
            )

            await context.db.citations.edit(
                citation_id=citation_id,
                **kwargs
            )

            result = f"ok: edited citation [{citation_id}]"
            if content_changed:
                result += " (verification_status reset to 'pending')"
            return result

        except ValueError as e:
            return f"error: {str(e)}"
        except Exception as e:
            logger.error(f"Error editing citation: {e}")
            return f"error: {str(e)}"

    @tool
    def annotate_source(
        source_id: int,
        content: str,
        type: Optional[str] = "note",
        page: Optional[str] = None,
    ) -> str:
        """Add a note, highlight, summary, question, or critique to a source.

        Build understanding of sources by annotating them with notes, highlights,
        summaries, questions, or critiques. Annotations are per-job.

        Args:
            source_id: The numeric source ID
            content: The annotation text
            type: Annotation type: note, highlight, summary, question, critique (default: note)
            page: Optional page/section reference (e.g., "p.12", "§ 3.1")

        Returns:
            Confirmation with annotation ID, or error message
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            annotation = engine.annotate_source(
                source_id=source_id,
                content=content,
                annotation_type=type or "note",
                page_reference=page,
            )

            return (
                f"Annotation [{annotation.id}] created\n"
                f"Type: {annotation.annotation_type.value}\n"
                f"Source: [{source_id}]"
                + (f"\nPage: {page}" if page else "")
            )

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Error creating annotation: {e}")
            return f"Error creating annotation: {str(e)}"

    @tool
    def get_annotations(
        source_id: int,
        type: Optional[str] = None,
    ) -> str:
        """Get annotations for a source in the current job.

        Retrieve all notes, highlights, summaries, questions, and critiques
        attached to a source. Optionally filter by annotation type.

        Args:
            source_id: The numeric source ID
            type: Optional filter: note, highlight, summary, question, critique

        Returns:
            Formatted list of annotations, or message if none found
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            annotations = engine.get_annotations(
                source_id=source_id,
                annotation_type=type,
            )

            if not annotations:
                filter_msg = f" of type '{type}'" if type else ""
                return f"No annotations{filter_msg} found for source [{source_id}]."

            lines = [f"Annotations for source [{source_id}] ({len(annotations)} total):", ""]
            for ann in annotations:
                preview = ann.content[:200] + "..." if len(ann.content) > 200 else ann.content
                page_str = f" (p.{ann.page_reference})" if ann.page_reference else ""
                lines.append(f"  [{ann.id}] {ann.annotation_type.value}{page_str}: {preview}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Error getting annotations: {e}")
            return f"Error getting annotations: {str(e)}"

    @tool
    def tag_source(
        source_id: int,
        tags: str,
        action: Optional[str] = "add",
    ) -> str:
        """Add or remove tags on a citation source.

        Tags help organize sources for later retrieval. Tags are per-job.
        Provide tags as a comma-separated string.

        Args:
            source_id: The numeric source ID
            tags: Comma-separated tag strings (e.g., "compliance, GoBD, retention")
            action: "add" (default) or "remove"

        Returns:
            Current list of tags for the source, or error message
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

            if not tag_list:
                return "Error: no tags provided"

            if action == "remove":
                current_tags = engine.remove_tags(source_id=source_id, tags=tag_list)
                verb = "Removed"
            else:
                current_tags = engine.tag_source(source_id=source_id, tags=tag_list)
                verb = "Added"

            return (
                f"{verb} tags on source [{source_id}]\n"
                f"Current tags: {', '.join(current_tags) if current_tags else '(none)'}"
            )

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Error tagging source: {e}")
            return f"Error tagging source: {str(e)}"

    @tool
    def search_library(
        query: str,
        mode: Optional[str] = "hybrid",
        tags: Optional[str] = None,
        source_type: Optional[str] = None,
        scope: Optional[str] = "content",
        top_k: Optional[int] = 10,
    ) -> str:
        """Search the source library using keyword, semantic, or hybrid retrieval.

        Find evidence across all registered sources. Returns results with
        explainable evidence labels (HIGH/MEDIUM/LOW). Use this to find
        supporting evidence before creating citations.

        Args:
            query: Natural language query or keywords to search for
            mode: Search mode: "hybrid" (default, recommended), "keyword", or "semantic"
            tags: Optional comma-separated tags to filter by (AND logic)
            source_type: Optional filter: "document", "website", "database", "custom"
            scope: What to search: "content" (default), "annotations", or "all"
            top_k: Maximum results to return (default: 10)

        Returns:
            Formatted search results with evidence labels and source references
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()

            # Parse tags from comma-separated string
            tag_list = None
            if tags:
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]

            results = engine.search_library(
                query=query,
                mode=mode or "hybrid",
                tags=tag_list,
                source_type=source_type,
                scope=scope or "content",
                top_k=top_k or 10,
            )

            if not results.results:
                return f"No results found for: {query}\nMode: {results.mode}"

            lines = [
                f"Search Results ({len(results.results)} found)",
                f"Query: {query}",
                f"Mode: {results.mode}",
                f"Evidence: {results.overall_label}",
                "",
            ]

            for i, r in enumerate(results.results, 1):
                source_ref = f"[{r.source_id}] {r.source_name}"
                page_str = f", {r.page_reference}" if r.page_reference else ""
                preview = r.chunk_text[:300] + "..." if len(r.chunk_text) > 300 else r.chunk_text

                lines.append(f"  {i}. {r.evidence_label} ({r.evidence_reason})")
                lines.append(f"     Source: {source_ref}{page_str}")
                lines.append(f'     "{preview}"')
                lines.append("")

            return "\n".join(lines)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Error searching library: {e}")
            return f"Error searching library: {str(e)}"

    @tool
    def generate_bibliography(
        style: Optional[str] = "bibtex",
        citation_ids: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        """Generate a formatted bibliography/references section from citations.

        Produces a bibliography from all citations in the current job, or from
        a specific subset. Can write directly to a file (e.g., references.bib)
        or return the formatted text. When writing to an existing file, only
        new entries are appended — duplicates are skipped.

        Args:
            style: Format style: "bibtex" (default), "harvard", "ieee", "apa", "inline"
            citation_ids: Comma-separated citation IDs to include (e.g., "1,3,7").
                          If omitted, includes all citations for the current job.
            output_path: Workspace-relative path to write (e.g., "references.bib").
                         If omitted, returns the formatted text directly.

        Returns:
            Formatted bibliography text, or write confirmation with counts
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            effective_style = style or "bibtex"

            # Get citations — filtered subset or all
            if citation_ids:
                ids = []
                for part in citation_ids.split(","):
                    part = part.strip()
                    if part.isdigit():
                        ids.append(int(part))
                    else:
                        return f"Error: invalid citation ID '{part}' — must be numeric"

                entries = []
                for cid in ids:
                    try:
                        entry = engine.format_citation(cid, effective_style)
                        entries.append(entry)
                    except ValueError as e:
                        entries.append(f"% Error for citation {cid}: {e}")
            else:
                # All citations for this job
                all_citations = engine.list_citations()
                if not all_citations:
                    return "No citations found. Use cite_document or cite_web first."
                entries = []
                for c in all_citations:
                    try:
                        entry = engine.format_citation(c.id, effective_style)
                        entries.append(entry)
                    except ValueError as e:
                        entries.append(f"% Error for citation {c.id}: {e}")

            if not entries:
                return "No entries generated."

            bibliography = "\n\n".join(entries)

            # Return text directly if no output path
            if not output_path:
                header = f"Bibliography ({len(entries)} entries, style: {effective_style})\n"
                return header + "\n" + bibliography

            # Write to file in workspace
            if workspace is None:
                return "Error: no workspace available for file output"

            resolved = workspace.get_path(output_path)
            resolved.parent.mkdir(parents=True, exist_ok=True)

            new_count = len(entries)
            skipped = 0

            if resolved.exists():
                existing_content = resolved.read_text(encoding="utf-8")

                if effective_style == "bibtex":
                    # Extract existing BibTeX keys to avoid duplicates
                    existing_keys = set(re.findall(r"@\w+\{(\w+),", existing_content))
                    new_entries = []
                    for entry in entries:
                        match = re.search(r"@\w+\{(\w+),", entry)
                        if match and match.group(1) in existing_keys:
                            skipped += 1
                        else:
                            new_entries.append(entry)
                    new_count = len(new_entries)

                    if new_entries:
                        append_text = "\n\n" + "\n\n".join(new_entries)
                        with open(resolved, "a", encoding="utf-8") as f:
                            f.write(append_text)
                else:
                    # For non-bibtex styles, use exact string matching
                    existing_entries = set(existing_content.strip().split("\n\n"))
                    new_entries = []
                    for entry in entries:
                        if entry.strip() in existing_entries:
                            skipped += 1
                        else:
                            new_entries.append(entry)
                    new_count = len(new_entries)

                    if new_entries:
                        append_text = "\n\n" + "\n\n".join(new_entries)
                        with open(resolved, "a", encoding="utf-8") as f:
                            f.write(append_text)

                return (
                    f"Updated {output_path}: {new_count} new entries appended, "
                    f"{skipped} duplicates skipped"
                )
            else:
                with open(resolved, "w", encoding="utf-8") as f:
                    f.write(bibliography + "\n")
                return f"Written {output_path}: {len(entries)} entries ({effective_style} style)"

        except Exception as e:
            logger.error(f"Error generating bibliography: {e}")
            return f"Error generating bibliography: {str(e)}"

    return [
        cite_document,
        cite_web,
        list_sources,
        get_citation,
        list_citations,
        edit_citation,
        annotate_source,
        get_annotations,
        tag_source,
        search_library,
        generate_bibliography,
    ]
