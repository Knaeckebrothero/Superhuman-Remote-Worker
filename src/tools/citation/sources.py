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
from typing import Any, Dict, List, Literal, Optional, get_args

from langchain_core.tools import tool

from ..context import ToolContext

from src.shared.tool_catalog.definitions import (
    CITATION_TOOLS_METADATA as CITATION_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


# Closed vocabularies, mirrored from src/citation_engine/models.py and the
# Postgres enums in orchestrator/database/migrations/vector/0001_initial.sql.
#
# These MUST be Literal, not str: every citation tool is registered
# defer_to_workspace=True, so apply_description_overrides() replaces the
# docstring naming these values with a one-line short_description before the
# tool is bound. The docstring does not reach the model; the args_schema does
# (and is serialized on every call regardless). A vocabulary documented only in
# prose is therefore invisible — see
# knowledge-base/knowledge/issues/agent_tool_fixed_vocabularies_invisible_to_model.md.
#
# Spelled literally rather than derived from the enums so these signatures stay
# importable when the engine is absent (the @tool decorator evaluates
# annotations at load time, before each tool body's availability check).
# tests/test_citation_tool_vocabularies.py asserts they stay in sync.
ExtractionMethodValue = Literal[
    "direct_quote", "paraphrase", "inference", "aggregation", "negative"
]
ConfidenceValue = Literal["high", "medium", "low"]
VerificationStatusValue = Literal["pending", "verified", "failed", "unverified"]
SourceTypeValue = Literal["document", "website", "database", "custom"]
AnnotationTypeValue = Literal["note", "highlight", "summary", "question", "critique"]
SearchModeValue = Literal["hybrid", "keyword", "semantic"]
SearchScopeValue = Literal["content", "annotations", "all"]
BibliographyStyleValue = Literal["bibtex", "harvard", "ieee", "apa", "inline"]
TagActionValue = Literal["add", "remove"]


# Tool metadata for registry
# Phase availability: domain tools are tactical-only


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
Page: {page or "N/A"}
Section: {section or "N/A"}

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
Title: {title or "Untitled"}
Accessed: {accessed_date}

Content:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Note: CitationEngine not available. Citation stored in stub mode only.
Use this citation_id when writing requirements."""


#: Postgres enum type name -> the vocabulary the caller should have been offered.
#: Only the vector-DB enums reachable from these tools; ``AnnotationType`` is a
#: Python-side enum and already raises a ValueError with its own message.
_DB_ENUM_VOCABULARIES: Dict[str, tuple] = {
    "extraction_method": get_args(ExtractionMethodValue),
    "confidence_level": get_args(ConfidenceValue),
    "verification_status": get_args(VerificationStatusValue),
    "source_type": get_args(SourceTypeValue),
}


def _humanize_db_enum_error(exc: Exception) -> Optional[str]:
    """Translate a Postgres enum rejection into something an agent can act on.

    asyncpg raises ``InvalidTextRepresentationError`` for a bad enum label. It
    is a ``DataError``, **not** a ``ValueError``, so it slips past the handlers
    that return curated messages — and its text names no valid values, leaving
    an agent to brute-force the vocabulary. Last-resort net: the tool schemas
    and the engine's validation should both reject a bad value long before it
    reaches Postgres.

    Returns None when ``exc`` is not a recognised enum error, so callers can
    fall through to their normal error handling.
    """
    match = re.search(r'invalid input value for enum (\w+): "(.*)"', str(exc))
    if not match:
        return None
    enum_name, bad_value = match.group(1), match.group(2)
    allowed = _DB_ENUM_VOCABULARIES.get(enum_name)
    if not allowed:
        return None
    return (
        f"invalid value '{bad_value}' for {enum_name}. Use one of: "
        f"{', '.join(allowed)}."
    )


def _verbatim_or_none(text: str, extraction_method: str) -> Optional[str]:
    """Return ``text`` as a verbatim quote only if the agent claims it is one.

    The verifier's word-for-word check is triggered by the *presence* of
    ``verbatim_quote`` — ``VerifyCitationTask.build_context`` emits a
    "## Verbatim Quote" section only when it is set, and the prompt then
    requires the text to appear in the source. Filing a paraphrase under that
    field is what makes an otherwise-sound citation fail verification, and the
    create tools used to do exactly that: they hardcoded
    ``extraction_method="direct_quote"`` no matter what ``text`` really was.

    For any non-quote method the field stays unset, so the citation is checked
    for meaning against ``quote_context`` instead — which the verification
    prompt already supports ("a close, meaning-preserving match", ~0.7 score).

    The ``< 1000`` guard is pre-existing behaviour, kept as-is: an overlong
    "quote" is treated as context rather than a quote.
    """
    if extraction_method != "direct_quote":
        return None
    return text[:500] if len(text) < 1000 else None


def create_source_tools(context: ToolContext) -> List[Any]:
    """Create citation tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Get workspace manager for path resolution
    workspace = context.workspace_manager if context.has_workspace() else None

    @tool
    async def cite_document(
        text: str,
        document_path: str,
        page: Optional[int] = None,
        section: Optional[str] = None,
        claim: Optional[str] = None,
        extraction_method: ExtractionMethodValue = "direct_quote",
        confidence: ConfidenceValue = "high",
    ) -> str:
        """Create a verified citation for document content.

        Registers the document as a source (if not already registered) and creates
        a citation linking your claim to the quoted text. The citation is verified
        against the source content using an LLM.

        Tip: Use search_library first to find relevant evidence across all sources,
        then cite the specific passage with this tool.

        Set extraction_method honestly — it decides how the citation is verified.
        'direct_quote' means `text` appears verbatim in the source and will be
        checked word-for-word; anything else is checked for meaning instead. If
        you reworded the source, say 'paraphrase' rather than letting a reworded
        passage be checked as a quote (it will fail).

        Args:
            text: The evidence from the document — verbatim if extraction_method
                is 'direct_quote', otherwise your rewording/synthesis of it
            document_path: Path to the source document
            page: Page number if applicable
            section: Section reference if applicable
            claim: The assertion being supported (defaults to summary of text)
            extraction_method: How you got `text` from the source
            confidence: Your self-assessment of how well the source backs the claim

        Returns:
            Citation ID and verification status. Use [N] format in your text.
        """
        try:
            # Try to use CitationEngine
            try:
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                # Fallback to stub behavior
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                logger.warning("CitationEngine not installed, using stub mode")
                return _format_stub_document_citation(
                    citation_id, document_path, page, section, text
                )

            # Register source and create citation
            cloud_anchor = None
            try:
                # Phase 3 (D7): if this file was read from a user's cloud, a
                # snapshot-anchor (drift fingerprint + live pointer) was stashed
                # at read time — persist it onto the source's metadata.cloud.
                # The identity is workspace-relative. Resolving it first is a
                # backend-path bypass: virtual paths are object keys and remote
                # paths name files on another pod, not local agent files.
                cloud_anchor = context.get_cloud_anchor(document_path)
                # Phase 3b: snapshot the original bytes to the blob store (via the
                # orchestrator — the agent has no S3 creds) so the citation has a
                # "view original" backup. Mutates cloud_anchor with the blob key
                # before registration; best-effort, never blocks the citation.
                if cloud_anchor:
                    await context.snapshot_cloud_source_bytes(
                        document_path, cloud_anchor
                    )
                # Registration also materializes through local_copy in
                # get_or_register_doc_source; no backend path reaches local I/O.
                source_id = await context.get_or_register_doc_source(
                    document_path,
                    name=Path(document_path).name,
                    cloud_metadata=cloud_anchor,
                )
            except FileNotFoundError:
                return f"Error: Document not found at {document_path}"
            except Exception as e:
                # Registration failed for a real reason (extraction, embedding,
                # remote fetch, …) — the engine IS available, so report the
                # actual error instead of the misleading "not available" stub.
                logger.warning(f"Could not register document source: {e}")
                return (
                    f"Error: could not register document source '{document_path}': {e}"
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
            result = await engine.cite_doc(
                claim=effective_claim,
                source_id=source_id,
                quote_context=text,
                locator=locator,
                verbatim_quote=_verbatim_or_none(text, extraction_method),
                relevance_reasoning=f"Evidence from source document supporting: {effective_claim[:100]}",
                confidence=confidence,
                extraction_method=extraction_method,
            )

            # Format result for agent
            status = result.verification_status.value
            citation_ref = f"[{result.citation_id}]"
            similarity = (
                f"{result.similarity_score:.2f}" if result.similarity_score else "N/A"
            )

            output = f"""Citation Created

Citation ID: {citation_ref}
Source Type: Document
Document: {document_path}
Page: {page or "N/A"}
Section: {section or "N/A"}
Status: {status.upper()}
Similarity Score: {similarity}
"""
            if cloud_anchor:
                if cloud_anchor.get("snapshot_blob_key"):
                    output += (
                        "Cloud-anchored: yes "
                        "(original snapshotted for drift detection)\n"
                    )
                else:
                    output += "Cloud-anchored: yes (live pointer recorded)\n"
            if result.verification_notes:
                output += f"\nNote: {result.verification_notes}"

            output += f"\n\nUse {citation_ref} when referencing this information in your text."

            return output

        except Exception as e:
            logger.error(f"Citation creation error: {e}")
            return f"Error creating citation: {str(e)}"

    @tool
    async def cite_web(
        text: str,
        url: str,
        title: Optional[str] = None,
        accessed_date: Optional[str] = None,
        claim: Optional[str] = None,
        extraction_method: ExtractionMethodValue = "direct_quote",
        confidence: ConfidenceValue = "high",
    ) -> str:
        """Create a verified citation for web content.

        Registers the URL as a source (fetching and archiving its content) and creates
        a citation linking your claim to the quoted text. The citation is verified
        against the archived content using an LLM.

        Tip: Use search_library first to find relevant evidence across all sources,
        then cite the specific passage with this tool.

        Set extraction_method honestly — it decides how the citation is verified.
        'direct_quote' means `text` appears verbatim in the source and will be
        checked word-for-word; anything else is checked for meaning instead. If
        you reworded the source, say 'paraphrase' rather than letting a reworded
        passage be checked as a quote (it will fail).

        Args:
            text: The evidence from the web — verbatim if extraction_method is
                'direct_quote', otherwise your rewording/synthesis of it
            url: Source URL
            title: Page title (auto-detected if not provided)
            accessed_date: Date accessed in ISO format (defaults to today)
            claim: The assertion being supported (defaults to summary of text)
            extraction_method: How you got `text` from the source
            confidence: Your self-assessment of how well the source backs the claim

        Returns:
            Citation ID and verification status. Use [N] format in your text.
        """
        try:
            # Use today's date if not provided
            if not accessed_date:
                accessed_date = datetime.utcnow().strftime("%Y-%m-%d")

            # Try to use CitationEngine
            try:
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"
                logger.warning("CitationEngine not installed, using stub mode")
                return _format_stub_web_citation(
                    citation_id, url, title, accessed_date, text
                )

            # Register source and create citation
            try:
                source_id, fetch_error = await context.get_or_register_web_source(
                    url, name=title
                )
            except Exception as e:
                # Registration failed for a real reason — the engine IS
                # available, so report the actual error instead of the
                # misleading "not available" stub.
                logger.warning(f"Could not register web source: {e}")
                return f"Error: could not register web source '{url}': {e}"

            # Persist web content to disk (idempotent — no-op if already saved by research tools)
            if text:
                context.save_web_content_to_disk(
                    url, text, title=title, source_id=source_id
                )

            # Build locator dict
            locator = {"accessed_at": accessed_date}
            if title:
                locator["title"] = title

            # Create the citation
            engine = context.get_citation_engine()
            effective_claim = claim or f"Information from web source: {text[:100]}..."
            result = await engine.cite_web(
                claim=effective_claim,
                source_id=source_id,
                quote_context=text,
                locator=locator,
                verbatim_quote=_verbatim_or_none(text, extraction_method),
                relevance_reasoning=f"Content from web source supporting: {effective_claim[:100]}",
                confidence=confidence,
                extraction_method=extraction_method,
            )

            # Format result
            status = result.verification_status.value
            citation_ref = f"[{result.citation_id}]"
            similarity = (
                f"{result.similarity_score:.2f}" if result.similarity_score else "N/A"
            )

            output = f"""Web Citation Created

Citation ID: {citation_ref}
Source Type: Web
URL: {url}
Title: {title or "Untitled"}
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
    async def list_sources() -> str:
        """List citation sources registered by this job.

        Shows document, web, database, and custom sources that have been
        registered for citations in the current job. Sources from other jobs
        are not visible.

        Returns:
            Formatted list of sources with IDs and types
        """
        try:
            try:
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No sources available."

            engine = context.get_citation_engine()
            # list_sources() now filters by job_id from context automatically
            sources = await engine.list_sources()

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
    async def get_citation(citation_id: int) -> str:
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
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            # get_citation() now filters by job_id from context automatically
            citation = await engine.get_citation(citation_id)

            if not citation:
                return f"Citation [{citation_id}] not found."

            source = await engine.get_source(citation.source_id)

            similarity = (
                f"{citation.similarity_score:.2f}"
                if citation.similarity_score
                else "N/A"
            )
            source_name = source.name if source else "Unknown"
            source_type = f" ({source.type.value})" if source else ""

            output = f"Citation [{citation_id}]\n"
            output += f"\nSource: [{source.id}] {source_name}{source_type}"

            if citation.locator:
                loc_parts = [
                    f"{k.capitalize()} {v}" for k, v in citation.locator.items() if v
                ]
                if loc_parts:
                    output += f"\nLocation: {', '.join(loc_parts)}"

            output += f"\nClaim: {citation.claim}"

            if citation.verbatim_quote:
                quote = (
                    citation.verbatim_quote[:500] + "..."
                    if len(citation.verbatim_quote) > 500
                    else citation.verbatim_quote
                )
                output += f'\n\nQuote: "{quote}"'

            if citation.quote_context:
                ctx = (
                    citation.quote_context[:500] + "..."
                    if len(citation.quote_context) > 500
                    else citation.quote_context
                )
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
    async def list_citations(
        source_id: Optional[int] = None,
        status: Optional[VerificationStatusValue] = None,
    ) -> str:
        """List citations created by this job.

        Shows citation IDs, claims (truncated), verification status, and source.
        Optionally filter by source ID or verification status. Citations from
        other jobs are not visible.

        Args:
            source_id: Filter by source ID (optional)
            status: Filter by verification status: pending, verified, failed,
                unverified (optional)

        Returns:
            Formatted list of citations for the current job
        """
        try:
            try:
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed. No citations available."

            engine = context.get_citation_engine()
            # list_citations() now filters by job_id from context automatically
            # Pass filters directly to engine for efficiency
            citations = await engine.list_citations(
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
        confidence: Optional[ConfidenceValue] = None,
        extraction_method: Optional[ExtractionMethodValue] = None,
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
            try:
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "error: CitationEngine not installed"

            engine = context.get_citation_engine()

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

            # Route through the engine (vector store). Job-scoped ownership and
            # the verification-status reset on content change are handled there.
            # (Previously this hit context.db = the main app DB, where citations
            # do not live — a cross-database mis-target.)
            await engine.edit_citation(citation_id=citation_id, **kwargs)

            result = f"ok: edited citation [{citation_id}]"
            if content_changed:
                result += " (verification_status reset to 'pending')"
            return result

        except ValueError as e:
            return f"error: {str(e)}"
        except Exception as e:
            hint = _humanize_db_enum_error(e)
            if hint:
                return f"error: {hint}"
            logger.error(f"Error editing citation: {e}")
            return f"error: {str(e)}"

    @tool
    async def annotate_source(
        source_id: int,
        content: str,
        type: AnnotationTypeValue = "note",
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
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            annotation = await engine.annotate_source(
                source_id=source_id,
                content=content,
                annotation_type=type or "note",
                page_reference=page,
            )

            return (
                f"Annotation [{annotation.id}] created\n"
                f"Type: {annotation.annotation_type.value}\n"
                f"Source: [{source_id}]" + (f"\nPage: {page}" if page else "")
            )

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Error creating annotation: {e}")
            return f"Error creating annotation: {str(e)}"

    @tool
    async def get_annotations(
        source_id: int,
        type: Optional[AnnotationTypeValue] = None,
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
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            annotations = await engine.get_annotations(
                source_id=source_id,
                annotation_type=type,
            )

            if not annotations:
                filter_msg = f" of type '{type}'" if type else ""
                return f"No annotations{filter_msg} found for source [{source_id}]."

            lines = [
                f"Annotations for source [{source_id}] ({len(annotations)} total):",
                "",
            ]
            for ann in annotations:
                preview = (
                    ann.content[:200] + "..." if len(ann.content) > 200 else ann.content
                )
                page_str = f" (p.{ann.page_reference})" if ann.page_reference else ""
                lines.append(
                    f"  [{ann.id}] {ann.annotation_type.value}{page_str}: {preview}"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Error getting annotations: {e}")
            return f"Error getting annotations: {str(e)}"

    @tool
    async def tag_source(
        source_id: int,
        tags: str,
        action: TagActionValue = "add",
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
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

            if not tag_list:
                return "Error: no tags provided"

            # Match both actions explicitly. An unrecognised value must never
            # fall through to "add" — that silently did the *opposite* of a
            # remove request and still reported "Added".
            if action == "remove":
                current_tags = await engine.remove_tags(
                    source_id=source_id, tags=tag_list
                )
                verb = "Removed"
            elif action == "add":
                current_tags = await engine.tag_source(
                    source_id=source_id, tags=tag_list
                )
                verb = "Added"
            else:
                return f"error: invalid action '{action}'. Use 'add' or 'remove'."

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
    async def search_library(
        query: str,
        mode: SearchModeValue = "hybrid",
        tags: Optional[str] = None,
        source_type: Optional[SourceTypeValue] = None,
        scope: SearchScopeValue = "content",
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
                from src.citation_engine import CitationEngine  # noqa: F401
            except ImportError:
                return "CitationEngine not installed."

            engine = context.get_citation_engine()

            # Parse tags from comma-separated string
            tag_list = None
            if tags:
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]

            results = await engine.search_library(
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
                preview = (
                    r.chunk_text[:300] + "..."
                    if len(r.chunk_text) > 300
                    else r.chunk_text
                )

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
    async def generate_bibliography(
        style: BibliographyStyleValue = "bibtex",
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
                from src.citation_engine import CitationEngine  # noqa: F401
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
                        entry = await engine.format_citation(cid, effective_style)
                        entries.append(entry)
                    except ValueError as e:
                        entries.append(f"% Error for citation {cid}: {e}")
            else:
                # All citations for this job
                all_citations = await engine.list_citations()
                if not all_citations:
                    return "No citations found. Use cite_document or cite_web first."
                entries = []
                for c in all_citations:
                    try:
                        entry = await engine.format_citation(c.id, effective_style)
                        entries.append(entry)
                    except ValueError as e:
                        entries.append(f"% Error for citation {c.id}: {e}")

            if not entries:
                return "No entries generated."

            bibliography = "\n\n".join(entries)

            # Return text directly if no output path
            if not output_path:
                header = (
                    f"Bibliography ({len(entries)} entries, style: {effective_style})\n"
                )
                return header + "\n" + bibliography

            # Write to file in workspace (uses backend abstraction for remote support)
            if workspace is None:
                return "Error: no workspace available for file output"

            new_count = len(entries)
            skipped = 0

            if workspace.exists(output_path):
                existing_content = workspace.read_file(output_path)

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
                        workspace.append_file(output_path, append_text)
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
                        workspace.append_file(output_path, append_text)

                return (
                    f"Updated {output_path}: {new_count} new entries appended, "
                    f"{skipped} duplicates skipped"
                )
            else:
                workspace.write_file(output_path, bibliography + "\n")
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
