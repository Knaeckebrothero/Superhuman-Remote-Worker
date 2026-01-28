"""Citation tools for the Universal Agent.

Provides document and web citation capabilities for requirement traceability.
Integrates with CitationEngine for verified, persistent citations.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
CITATION_TOOLS_METADATA = {
    "cite_document": {
        "module": "citation_tools",
        "function": "cite_document",
        "description": "Create a verified citation for document content",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Create verified citation for document content.",
        "phases": ["tactical"],
    },
    "cite_web": {
        "module": "citation_tools",
        "function": "cite_web",
        "description": "Create a verified citation for web content",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Create verified citation for web content.",
        "phases": ["tactical"],
    },
    "list_sources": {
        "module": "citation_tools",
        "function": "list_sources",
        "description": "List all registered citation sources",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "List all registered citation sources.",
        "phases": ["tactical"],
    },
    "get_citation": {
        "module": "citation_tools",
        "function": "get_citation",
        "description": "Get details about a specific citation",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get details about a specific citation by ID.",
        "phases": ["tactical"],
    },
    "list_citations": {
        "module": "citation_tools",
        "function": "list_citations",
        "description": "List all citations created in this session",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "List all citations with status and source info.",
        "phases": ["tactical"],
    },
    "edit_citation": {
        "module": "citation_tools",
        "function": "edit_citation",
        "description": "Edit fields of an existing citation",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Edit citation fields (claim, quote, confidence, etc.).",
        "phases": ["tactical"],
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


def create_citation_tools(context: ToolContext) -> List:
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
                source_id = context.get_or_register_web_source(url, name=title)
            except ConnectionError as e:
                return f"Error: Could not fetch URL {url}: {e}"
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
        """List all registered citation sources.

        Shows all document, web, database, and custom sources that have been
        registered for citations in this session.

        Returns:
            Formatted list of sources with IDs and types
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No sources available."

            engine = context.get_citation_engine()
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
        """Get details about a specific citation.

        Retrieves the full citation record including claim, source, verification
        status, and similarity score.

        Args:
            citation_id: The numeric citation ID (without brackets)

        Returns:
            Detailed citation information
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            citation = engine.get_citation(citation_id)

            if not citation:
                return f"Citation [{citation_id}] not found."

            source = engine.get_source(citation.source_id)

            similarity = f"{citation.similarity_score:.2f}" if citation.similarity_score else "N/A"
            output = f"""Citation [{citation_id}]

Claim: {citation.claim[:300]}{"..." if len(citation.claim) > 300 else ""}
Source: [{source.id}] {source.name if source else 'Unknown'}
Status: {citation.verification_status.value.upper()}
Confidence: {citation.confidence.value}
Similarity: {similarity}
Created: {citation.created_at}
"""
            if citation.verification_notes:
                output += f"\nNotes: {citation.verification_notes}"

            return output

        except Exception as e:
            logger.error(f"Error getting citation: {e}")
            return f"Error getting citation: {str(e)}"

    @tool
    def list_citations(source_id: Optional[int] = None, status: Optional[str] = None) -> str:
        """List all citations created in this session.

        Shows citation IDs, claims (truncated), verification status, and source.
        Optionally filter by source ID or verification status.

        Args:
            source_id: Filter by source ID (optional)
            status: Filter by verification status: pending, verified, failed (optional)

        Returns:
            Formatted list of citations
        """
        try:
            try:
                from citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No citations available."

            engine = context.get_citation_engine()
            citations = engine.list_citations()

            if not citations:
                return "No citations created yet. Use cite_document or cite_web to create citations."

            # Apply filters
            if source_id is not None:
                citations = [c for c in citations if c.source_id == source_id]
            if status is not None:
                citations = [c for c in citations if c.verification_status.value == status]

            if not citations:
                return "No citations match the given filters."

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
        """Edit fields of an existing citation.

        When content fields (claim, verbatim_quote, quote_context) are changed,
        verification_status is automatically reset to 'pending' and previous
        verification results are cleared, since the old verification is no
        longer valid.

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

    return [
        cite_document,
        cite_web,
        list_sources,
        get_citation,
        list_citations,
        edit_citation,
    ]
