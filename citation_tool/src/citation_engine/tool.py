"""
LangGraph Tool Wrapper for Citation Engine
==========================================
Exposes citation functionality as LangChain/LangGraph tools that can be
used by AI agents.

Based on the Citation & Provenance Engine Design Document v0.3.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from .engine import CitationEngine
from .models import CitationResult, Source, VerificationStatus

log = logging.getLogger(__name__)


# =============================================================================
# PYDANTIC SCHEMAS FOR TOOL INPUT
# =============================================================================


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
        "Leave empty for paraphrased or inferred claims.",
    )
    relevance_reasoning: str | None = Field(
        default=None,
        description="Brief explanation of why this evidence supports the claim",
    )
    source_type: str = Field(
        description="Type of source: 'document', 'website', 'database', or 'custom'"
    )
    source_identifier: str = Field(
        description="The source filename, URL, database identifier, or source ID"
    )
    locator: dict[str, Any] = Field(
        description="Location within the source. For documents: {page, section}. "
        "For websites: {heading_context}. For databases: {query, table}."
    )
    confidence: str = Field(
        default="high",
        description="Your confidence in this citation: 'high', 'medium', or 'low'",
    )
    extraction_method: str = Field(
        default="direct_quote",
        description="How you extracted the information: 'direct_quote', 'paraphrase', "
        "'inference', 'aggregation', or 'negative'",
    )


class SourceInput(BaseModel):
    """Schema for source registration."""

    source_type: str = Field(
        description="Type of source: 'document', 'website', 'database', or 'custom'"
    )
    identifier: str = Field(
        description="For documents: file path. For websites: URL. "
        "For databases/custom: unique identifier."
    )
    name: str | None = Field(
        default=None,
        description="Human-readable name for the source",
    )
    content: str | None = Field(
        default=None,
        description="For database/custom sources: the content to register",
    )
    version: str | None = Field(
        default=None,
        description="Version identifier (e.g., '2024-01')",
    )


# =============================================================================
# TOOL FACTORY FUNCTIONS
# =============================================================================


def create_citation_tools(engine: CitationEngine) -> list:
    """
    Create LangChain tools for citation functionality.

    This function creates tools that can be used with LangGraph agents.
    The tools are bound to the provided CitationEngine instance.

    Args:
        engine: Initialized CitationEngine instance

    Returns:
        List of LangChain tools: [cite, register_source, list_sources, get_citation_status]

    Usage:
        engine = CitationEngine(mode="basic")
        tools = create_citation_tools(engine)

        # Add to your LangGraph agent
        agent = create_react_agent(llm, tools)

    Example with LangGraph:
        from citation_engine import CitationEngine
        from citation_engine.tool import create_citation_tools

        engine = CitationEngine(mode="basic", db_path="./citations.db")
        with engine:
            tools = create_citation_tools(engine)

            # Create your agent with these tools
            from langgraph.prebuilt import create_react_agent
            agent = create_react_agent(model, tools)

    Raises:
        ImportError: If langchain-core is not installed
    """
    log.debug("Creating citation tools for LangChain/LangGraph integration")

    try:
        from langchain_core.tools import tool
    except ImportError as e:
        log.error("langchain-core not installed, cannot create citation tools")
        raise ImportError(
            "langchain-core is required for tool creation. Install with: pip install langchain-core"
        ) from e

    @tool(args_schema=CitationInput)
    def cite(
        claim: str,
        quote_context: str,
        source_type: str,
        source_identifier: str,
        locator: dict[str, Any],
        verbatim_quote: str | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote",
    ) -> str:
        """
        Register a citation linking your claim to a source. Returns a citation ID
        that you should embed in your response, e.g., "The law requires X [1]."

        Use this tool whenever you make a factual claim based on a specific source.
        The citation will be verified against the source to ensure accuracy.

        Args:
            claim: The assertion you are making
            quote_context: The paragraph containing the evidence
            source_type: Type of source (document, website, database, custom)
            source_identifier: Filename, URL, or source ID
            locator: Location within the source (e.g., {page: 24, section: "§ 8.1"})
            verbatim_quote: Exact quoted text (for direct citations)
            relevance_reasoning: Why this evidence supports the claim
            confidence: Your confidence level (high, medium, low)
            extraction_method: How you extracted the info

        Returns:
            Citation ID formatted for embedding, e.g., "[1]"
            If verification fails, includes failure reason.
        """
        log.debug(f"cite() called: source_type={source_type}, identifier={source_identifier}")

        # Validate source_type
        valid_types = ("document", "website", "database", "custom")
        if source_type not in valid_types:
            error_msg = (
                f"Invalid source_type: '{source_type}'. Must be one of: {', '.join(valid_types)}"
            )
            log.warning(error_msg)
            return f"Error: {error_msg}"

        # Find or resolve the source
        source_id = _resolve_source_id(engine, source_type, source_identifier)

        if source_id is None:
            # Provide helpful error message with available sources
            available = engine.list_sources(source_type=source_type)
            if available:
                source_list = ", ".join([f"[{s.id}] {s.name}" for s in available[:5]])
                error_msg = (
                    f"Source not found: '{source_identifier}'. "
                    f"Available {source_type} sources: {source_list}"
                )
            else:
                error_msg = (
                    f"Source not found: '{source_identifier}'. "
                    f"No {source_type} sources registered yet. "
                    "Use register_source() to add a source first."
                )
            log.warning(error_msg)
            return f"Error: {error_msg}"

        # Create the citation based on source type
        try:
            if source_type == "document":
                result = engine.cite_doc(
                    claim=claim,
                    source_id=source_id,
                    quote_context=quote_context,
                    locator=locator,
                    verbatim_quote=verbatim_quote,
                    relevance_reasoning=relevance_reasoning,
                    confidence=confidence,
                    extraction_method=extraction_method,
                )
            elif source_type == "website":
                result = engine.cite_web(
                    claim=claim,
                    source_id=source_id,
                    quote_context=quote_context,
                    locator=locator,
                    verbatim_quote=verbatim_quote,
                    relevance_reasoning=relevance_reasoning,
                    confidence=confidence,
                    extraction_method=extraction_method,
                )
            elif source_type == "database":
                result = engine.cite_db(
                    claim=claim,
                    source_id=source_id,
                    quote_context=quote_context,
                    locator=locator,
                    relevance_reasoning=relevance_reasoning,
                    confidence=confidence,
                    extraction_method=extraction_method,
                )
            elif source_type == "custom":
                result = engine.cite_custom(
                    claim=claim,
                    source_id=source_id,
                    quote_context=quote_context,
                    locator=locator,
                    relevance_reasoning=relevance_reasoning,
                    confidence=confidence,
                )

            # Format response based on verification status
            if result.verification_status == VerificationStatus.VERIFIED:
                log.info(f"Citation [{result.citation_id}] created and verified successfully")
                return f"[{result.citation_id}]"
            else:
                log.warning(
                    f"Citation [{result.citation_id}] created but verification failed: "
                    f"{result.verification_notes}"
                )
                # Truncate notes if too long
                notes = result.verification_notes or "Unknown reason"
                if len(notes) > 200:
                    notes = notes[:197] + "..."
                return f"[{result.citation_id}] (verification failed: {notes})"

        except ValueError as e:
            log.warning(f"Citation creation failed with ValueError: {e}")
            return f"Error: {e}"
        except Exception as e:
            log.error(f"Citation creation failed unexpectedly: {e}", exc_info=True)
            return f"Error creating citation: {e}"

    @tool(args_schema=SourceInput)
    def register_source(
        source_type: str,
        identifier: str,
        name: str | None = None,
        content: str | None = None,
        version: str | None = None,
    ) -> str:
        """
        Register a new source for citation. Call this before citing from a source
        that hasn't been registered yet.

        Args:
            source_type: Type of source (document, website, database, custom)
            identifier: Path, URL, or unique identifier
            name: Human-readable name
            content: Required for database/custom sources
            version: Version identifier

        Returns:
            Confirmation message with source ID, or error message if registration fails.
        """
        log.debug(f"register_source() called: type={source_type}, identifier={identifier}")

        # Validate source_type
        valid_types = ("document", "website", "database", "custom")
        if source_type not in valid_types:
            error_msg = (
                f"Invalid source_type: '{source_type}'. Must be one of: {', '.join(valid_types)}"
            )
            log.warning(error_msg)
            return f"Error: {error_msg}"

        try:
            if source_type == "document":
                source = engine.add_doc_source(
                    file_path=identifier,
                    name=name,
                    version=version,
                )
            elif source_type == "website":
                source = engine.add_web_source(
                    url=identifier,
                    name=name,
                    version=version,
                )
            elif source_type == "database":
                if not content:
                    return (
                        "Error: 'content' is required for database sources. "
                        "Provide the query result or data to be cited."
                    )
                source = engine.add_db_source(
                    identifier=identifier,
                    name=name or identifier,
                    content=content,
                )
            elif source_type == "custom":
                if not content:
                    return (
                        "Error: 'content' is required for custom sources. "
                        "Provide the artifact content (e.g., computed table, analysis)."
                    )
                source = engine.add_custom_source(
                    name=name or identifier,
                    content=content,
                )

            log.info(f"Source [{source.id}] registered: {source.name} ({source_type})")
            return f"Source registered successfully with ID {source.id}: {source.name}"

        except FileNotFoundError as e:
            log.warning(f"Source registration failed - file not found: {e}")
            return f"Error: File not found: {identifier}. Please check the path and try again."
        except ConnectionError as e:
            log.warning(f"Source registration failed - connection error: {e}")
            return f"Error: Could not fetch URL: {identifier}. {e}"
        except ImportError as e:
            log.warning(f"Source registration failed - missing dependency: {e}")
            return f"Error: Missing dependency: {e}"
        except Exception as e:
            log.error(f"Source registration failed unexpectedly: {e}", exc_info=True)
            return f"Error registering source: {e}"

    @tool
    def list_sources() -> str:
        """
        List all registered sources available for citation.

        Returns:
            Formatted list of sources with IDs and names, grouped by type.
        """
        log.debug("list_sources() called")
        sources = engine.list_sources()

        if not sources:
            return "No sources registered yet. Use register_source() to add sources."

        # Group by type
        by_type: dict[str, list[Source]] = {}
        for source in sources:
            type_name = source.type.value
            if type_name not in by_type:
                by_type[type_name] = []
            by_type[type_name].append(source)

        lines = [f"Registered sources ({len(sources)} total):"]

        for type_name in ["document", "website", "database", "custom"]:
            if type_name in by_type:
                lines.append(f"\n  {type_name.upper()}S:")
                for source in by_type[type_name]:
                    # Truncate name if too long
                    name = source.name
                    if len(name) > 50:
                        name = name[:47] + "..."
                    version_str = f" v{source.version}" if source.version else ""
                    lines.append(f"    [{source.id}] {name}{version_str}")

        return "\n".join(lines)

    @tool
    def get_citation_status(citation_id: int) -> str:
        """
        Get the verification status and details of a citation.

        Args:
            citation_id: The ID of the citation to check

        Returns:
            Detailed status information about the citation
        """
        log.debug(f"get_citation_status() called: citation_id={citation_id}")

        citation = engine.get_citation(citation_id)

        if citation is None:
            return f"Citation [{citation_id}] not found. Use a valid citation ID."

        source = engine.get_source(citation.source_id)
        source_name = source.name if source else "Unknown (deleted)"

        # Build detailed status response
        lines = [
            f"Citation [{citation_id}]:",
            f"  Status: {citation.verification_status.value.upper()}",
            f"  Source: [{citation.source_id}] {source_name}",
            f"  Confidence: {citation.confidence.value}",
            f"  Method: {citation.extraction_method.value}",
        ]

        # Add claim (truncated)
        claim = citation.claim
        if len(claim) > 100:
            claim = claim[:97] + "..."
        lines.append(f"  Claim: {claim}")

        # Add similarity score if available
        if citation.similarity_score is not None:
            lines.append(f"  Similarity: {citation.similarity_score:.2f}")

        # Add verification notes if failed
        if (
            citation.verification_status == VerificationStatus.FAILED
            and citation.verification_notes
        ):
            notes = citation.verification_notes
            if len(notes) > 150:
                notes = notes[:147] + "..."
            lines.append(f"  Notes: {notes}")

        return "\n".join(lines)

    log.info("Created 4 citation tools: cite, register_source, list_sources, get_citation_status")
    return [cite, register_source, list_sources, get_citation_status]


def _resolve_source_id(
    engine: CitationEngine,
    source_type: str,
    source_identifier: str,
) -> int | None:
    """
    Resolve a source identifier to a source ID.

    First tries to parse as integer (direct ID), then looks up by identifier,
    then by name.

    Args:
        engine: CitationEngine instance
        source_type: Type of source
        source_identifier: Identifier, ID, or name

    Returns:
        Source ID or None if not found
    """
    log.debug(f"Resolving source: type={source_type}, identifier={source_identifier}")

    # Try parsing as integer ID first
    try:
        source_id = int(source_identifier)
        source = engine.get_source(source_id)
        if source:
            log.debug(f"Resolved source by ID: {source_id}")
            return source_id
    except ValueError:
        pass

    # Look up by identifier
    sources = engine.list_sources(source_type=source_type)
    for source in sources:
        if source.identifier == source_identifier:
            log.debug(f"Resolved source by identifier: {source.id}")
            return source.id

    # Also try by name (case-insensitive)
    identifier_lower = source_identifier.lower()
    for source in sources:
        if source.name.lower() == identifier_lower:
            log.debug(f"Resolved source by name: {source.id}")
            return source.id

    log.debug(f"Could not resolve source: {source_identifier}")
    return None


# =============================================================================
# SIMPLIFIED FUNCTIONAL INTERFACE
# =============================================================================


class CitationTool:
    """
    Wrapper class providing a simpler interface for citation functionality.

    This can be used as an alternative to the LangChain tools for simpler
    integrations or non-LangChain workflows.

    Usage:
        with CitationTool(mode="basic", db_path="./citations.db") as tool:
            # Register a source
            source_id = tool.add_source("document", "./paper.pdf")

            # Create a citation
            result = tool.cite(
                claim="The study shows X",
                source_id=source_id,
                quote_context="Full paragraph...",
                locator={"page": 5}
            )
            print(f"Citation ID: {result.citation_id}")

    Attributes:
        engine: The underlying CitationEngine instance
    """

    def __init__(
        self,
        mode: str = "basic",
        db_path: str | None = None,
    ):
        """
        Initialize the citation tool.

        Args:
            mode: "basic" (SQLite) or "multi-agent" (PostgreSQL)
            db_path: Path to SQLite file (basic mode only)
        """
        log.debug(f"Initializing CitationTool: mode={mode}, db_path={db_path}")
        self.engine = CitationEngine(mode=mode, db_path=db_path)
        self.engine._connect()
        log.info("CitationTool initialized and connected")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False

    def close(self):
        """Close the engine connection."""
        log.debug("Closing CitationTool")
        self.engine.close()

    def add_source(
        self,
        source_type: str,
        identifier: str,
        name: str | None = None,
        content: str | None = None,
        version: str | None = None,
    ) -> int:
        """
        Register a source and return its ID.

        Args:
            source_type: Type of source (document, website, database, custom)
            identifier: Path, URL, or identifier
            name: Optional human-readable name
            content: Required for database/custom sources
            version: Optional version string

        Returns:
            Source ID

        Raises:
            ValueError: If source_type is invalid
            FileNotFoundError: If document file doesn't exist
            ConnectionError: If website cannot be fetched
        """
        log.debug(f"add_source(): type={source_type}, identifier={identifier}")

        if source_type == "document":
            source = self.engine.add_doc_source(identifier, name, version)
        elif source_type == "website":
            source = self.engine.add_web_source(identifier, name, version)
        elif source_type == "database":
            if not content:
                raise ValueError("content is required for database sources")
            source = self.engine.add_db_source(identifier, name or identifier, content)
        elif source_type == "custom":
            if not content:
                raise ValueError("content is required for custom sources")
            source = self.engine.add_custom_source(name or identifier, content)
        else:
            raise ValueError(
                f"Unknown source type: {source_type}. "
                "Use 'document', 'website', 'database', or 'custom'."
            )

        log.info(f"Added source [{source.id}]: {source.name}")
        return source.id

    def cite(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: dict[str, Any],
        verbatim_quote: str | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
    ) -> CitationResult:
        """
        Create a citation.

        Args:
            claim: The assertion being made
            source_id: ID of the source
            quote_context: Paragraph containing evidence
            locator: Location in source
            verbatim_quote: Exact quote (optional)
            relevance_reasoning: Why evidence supports claim
            confidence: Confidence level (high, medium, low)

        Returns:
            CitationResult with ID and verification status

        Raises:
            ValueError: If source not found or invalid parameters
        """
        log.debug(f"cite(): claim='{claim[:50]}...', source_id={source_id}")

        source = self.engine.get_source(source_id)
        if source is None:
            raise ValueError(f"Source not found: {source_id}")

        source_type = source.type.value

        if source_type == "document":
            result = self.engine.cite_doc(
                claim=claim,
                source_id=source_id,
                quote_context=quote_context,
                locator=locator,
                verbatim_quote=verbatim_quote,
                relevance_reasoning=relevance_reasoning,
                confidence=confidence,
            )
        elif source_type == "website":
            result = self.engine.cite_web(
                claim=claim,
                source_id=source_id,
                quote_context=quote_context,
                locator=locator,
                verbatim_quote=verbatim_quote,
                relevance_reasoning=relevance_reasoning,
                confidence=confidence,
            )
        elif source_type == "database":
            result = self.engine.cite_db(
                claim=claim,
                source_id=source_id,
                quote_context=quote_context,
                locator=locator,
                relevance_reasoning=relevance_reasoning,
                confidence=confidence,
            )
        elif source_type == "custom":
            result = self.engine.cite_custom(
                claim=claim,
                source_id=source_id,
                quote_context=quote_context,
                locator=locator,
                relevance_reasoning=relevance_reasoning,
                confidence=confidence,
            )
        else:
            raise ValueError(f"Unknown source type: {source_type}")

        log.info(
            f"Created citation [{result.citation_id}]: status={result.verification_status.value}"
        )
        return result

    def list_sources(self, source_type: str | None = None) -> list[Source]:
        """
        List all registered sources.

        Args:
            source_type: Optional filter by type

        Returns:
            List of Source objects
        """
        return self.engine.list_sources(source_type=source_type)

    def get_citation(self, citation_id: int):
        """
        Get a citation by ID.

        Args:
            citation_id: The citation ID

        Returns:
            Citation object or None if not found
        """
        return self.engine.get_citation(citation_id)

    def get_statistics(self) -> dict[str, Any]:
        """
        Get statistics about sources and citations.

        Returns:
            Dictionary with counts and breakdowns
        """
        return self.engine.get_statistics()
