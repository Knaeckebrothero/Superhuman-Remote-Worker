"""
Citation Engine - Core Implementation
=====================================
The main CitationEngine class that provides citation functionality
for AI agents.

Based on the Citation & Provenance Engine Design Document v0.3.
"""

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    Citation,
    CitationContext,
    CitationResult,
    Confidence,
    ExtractionMethod,
    Locator,
    Metadata,
    Source,
    SourceType,
    VerificationResult,
    VerificationStatus,
)
from .schema import get_schema

# Set up logging
log = logging.getLogger(__name__)


class CitationEngine:
    """
    Citation & Provenance Engine for AI agents.

    Supports two modes:
    - Basic (SQLite): Single-agent, zero setup, local development
    - Multi-Agent (PostgreSQL): Shared citation pool, production deployments

    Usage:
        # Basic mode (default)
        engine = CitationEngine(mode="basic", db_path="./citations.db")

        # Multi-agent mode
        engine = CitationEngine(mode="multi-agent")

        with engine:
            source = engine.add_doc_source("./document.pdf", name="My Doc")
            result = engine.cite_doc(
                claim="The regulation requires X",
                source_id=source.id,
                quote_context="Full paragraph...",
                locator={"page": 24}
            )

    Environment Variables:
        CITATION_DB_URL: PostgreSQL connection string (multi-agent mode)
        CITATION_LLM_URL: Custom LLM endpoint (e.g., llama.cpp server)
        CITATION_LLM_MODEL: Model to use for verification (default: gpt-4o-mini)
        CITATION_REASONING_REQUIRED: none | low | medium | high (default: low)
    """

    # Database connection types
    SQLITE_MODE = "basic"
    POSTGRESQL_MODE = "multi-agent"

    def __init__(
        self,
        mode: str = "basic",
        db_path: str | None = None,
        context: CitationContext | None = None,
    ):
        """
        Initialize the Citation Engine.

        Args:
            mode: "basic" (SQLite) or "multi-agent" (PostgreSQL)
            db_path: Path to SQLite file (basic mode) or None to use default
            context: Optional citation context for audit trails

        Environment Variables:
            CITATION_DB_URL: PostgreSQL connection string (multi-agent mode)
            CITATION_LLM_URL: Custom LLM endpoint (e.g., llama.cpp server)
            CITATION_LLM_MODEL: Model to use for verification
            CITATION_REASONING_REQUIRED: none | low | medium | high
        """
        log.debug("Initializing CitationEngine...")

        self.mode = mode
        self.context = context

        # Database configuration
        if mode == self.SQLITE_MODE:
            self.db_path = db_path or os.getenv("CITATION_DB_PATH", "./citations.db")
            self._db_type = "sqlite"
            log.debug(f"Configured for SQLite mode with path: {self.db_path}")
        elif mode == self.POSTGRESQL_MODE:
            self.db_url = os.getenv("CITATION_DB_URL")
            if not self.db_url:
                log.error("CITATION_DB_URL environment variable not set for multi-agent mode")
                raise ValueError(
                    "CITATION_DB_URL environment variable required for multi-agent mode"
                )
            self._db_type = "postgresql"
            log.debug("Configured for PostgreSQL mode")
        else:
            log.error(f"Invalid mode specified: {mode}")
            raise ValueError(f"Unknown mode: {mode}. Use 'basic' or 'multi-agent'.")

        # LLM configuration
        self.llm_url = os.getenv("CITATION_LLM_URL")
        self.llm_model = os.getenv("CITATION_LLM_MODEL", "gpt-4o-mini")
        self.llm_api_key = os.getenv("OPENAI_API_KEY", "")
        self.reasoning_level = os.getenv("CITATION_REASONING_LEVEL", "high")
        self.skip_verification = os.getenv("CITATION_SKIP_VERIFICATION", "false").lower() == "true"

        # Reasoning requirement configuration
        reasoning_config = os.getenv("CITATION_REASONING_REQUIRED", "low")
        if reasoning_config not in ("none", "low", "medium", "high"):
            log.warning(
                f"Invalid CITATION_REASONING_REQUIRED value: {reasoning_config}. Using 'low'."
            )
            reasoning_config = "low"
        self.reasoning_required = reasoning_config

        # Connection will be established on first use or with context manager
        self._conn = None
        self._cursor = None
        self._llm_client = None

        log.info(
            f"CitationEngine initialized: mode={mode}, "
            f"reasoning_required={self.reasoning_required}, "
            f"reasoning_level={self.reasoning_level}, "
            f"skip_verification={self.skip_verification}"
        )

    def __enter__(self):
        """Context manager entry - establish database connection."""
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close database connection."""
        self.close()
        return False  # Don't suppress exceptions

    def __del__(self):
        """Cleanup on deletion."""
        self.close()
        log.debug("CitationEngine object destroyed.")

    # =========================================================================
    # DATABASE CONNECTION METHODS
    # =========================================================================

    def _connect(self) -> None:
        """
        Establish database connection based on mode.

        For SQLite: Creates the database file if it doesn't exist
        For PostgreSQL: Connects using the CITATION_DB_URL environment variable

        Raises:
            ConnectionError: If connection fails
        """
        if self._conn is not None:
            log.debug("Connection already established, skipping reconnect")
            return

        log.debug(f"Establishing {self._db_type} database connection...")

        try:
            if self._db_type == "sqlite":
                self._connect_sqlite()
            else:
                self._connect_postgresql()

            # Initialize schema
            self._initialize_schema()
            log.info(f"Connected to {self._db_type} database successfully")

        except Exception as e:
            log.error(f"Failed to connect to database: {e}")
            raise ConnectionError(f"Database connection failed: {e}") from e

    def _connect_sqlite(self) -> None:
        """Establish SQLite connection with proper configuration."""
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            log.debug(f"Created directory for database: {db_dir}")

        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        # Enable dictionary-like row access
        self._conn.row_factory = sqlite3.Row
        self._cursor = self._conn.cursor()

        # Enable foreign keys for SQLite
        self._cursor.execute("PRAGMA foreign_keys = ON")

        log.debug(f"Connected to SQLite database: {self.db_path}")

    def _connect_postgresql(self) -> None:
        """Establish PostgreSQL connection with proper configuration."""
        try:
            import psycopg2
            import psycopg2.extras

            self._conn = psycopg2.connect(self.db_url)
            # Use RealDictCursor for dictionary-like row access
            self._cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            log.debug("Connected to PostgreSQL database")
        except ImportError as e:
            log.error("psycopg2 not installed, cannot use multi-agent mode")
            raise ImportError(
                "psycopg2 is required for multi-agent mode. "
                "Install it with: pip install psycopg2-binary"
            ) from e

    def _initialize_schema(self) -> None:
        """Create database tables if they don't exist."""
        log.debug("Initializing database schema...")
        schema = get_schema(self._db_type)

        try:
            if self._db_type == "sqlite":
                self._cursor.executescript(schema)
            else:
                self._cursor.execute(schema)
            self._conn.commit()
            log.debug("Database schema initialized successfully")
        except Exception as e:
            self._conn.rollback()
            log.error(f"Failed to initialize schema: {e}")
            raise

    def close(self) -> None:
        """Close database connection and cleanup resources."""
        if self._conn:
            try:
                self._conn.close()
                self._conn = None
                self._cursor = None
                log.debug("Database connection closed")
            except Exception as e:
                log.error(f"Error closing database connection: {e}")
        else:
            log.debug("No database connection to close")

    def _ensure_connected(self) -> None:
        """Ensure database connection is established."""
        if self._conn is None:
            log.debug("No active connection, establishing new connection...")
            self._connect()

    @contextmanager
    def _transaction(self):
        """
        Context manager for database transactions.

        Automatically commits on success, rolls back on failure.

        Usage:
            with self._transaction():
                self._execute_insert(...)
                self._execute_insert(...)
        """
        self._ensure_connected()
        try:
            yield
            self._conn.commit()
            log.debug("Transaction committed successfully")
        except Exception as e:
            self._conn.rollback()
            log.error(f"Transaction rolled back due to error: {e}")
            raise

    def _execute(
        self,
        query: str,
        params: tuple | None = None,
        fetch: bool = True,
    ) -> list[dict[str, Any]] | int:
        """
        Execute a SQL query with comprehensive error handling and logging.

        Args:
            query: SQL query string
            params: Query parameters (tuple)
            fetch: If True, fetch and return results; if False, return lastrowid

        Returns:
            List of dictionaries (if fetch=True) or last inserted row ID
        """
        self._ensure_connected()

        try:
            if params:
                log.debug(f"Executing query with {len(params)} parameters")
                self._cursor.execute(query, params)
            else:
                log.debug("Executing query without parameters")
                self._cursor.execute(query)

            if fetch:
                rows = self._cursor.fetchall()
                log.debug(f"Query returned {len(rows)} rows")
                # Convert to list of dicts
                if self._db_type == "sqlite":
                    return [dict(row) for row in rows]
                else:
                    return [dict(row) for row in rows]
            else:
                self._conn.commit()
                lastrowid = self._cursor.lastrowid
                log.debug(f"Insert completed, lastrowid={lastrowid}")
                return lastrowid

        except Exception as e:
            self._conn.rollback()
            log.error(f"Query execution failed: {e}")
            log.debug(f"Failed query: {query[:200]}...")
            if params:
                log.debug(
                    f"Query params: {params[:5]}..."
                    if len(params) > 5
                    else f"Query params: {params}"
                )
            raise

    def _query(
        self,
        query: str,
        params: tuple | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a SELECT query and return results as list of dicts.

        Args:
            query: SQL SELECT query
            params: Query parameters

        Returns:
            List of dictionaries containing query results
        """
        return self._execute(query, params, fetch=True)

    def _insert(
        self,
        query: str,
        params: tuple | None = None,
    ) -> int:
        """
        Execute an INSERT query and return the new row ID.

        Args:
            query: SQL INSERT query
            params: Query parameters

        Returns:
            ID of the inserted row
        """
        self._ensure_connected()

        try:
            if params:
                log.debug(f"Executing insert with {len(params)} parameters")
                self._cursor.execute(query, params)
            else:
                log.debug("Executing insert without parameters")
                self._cursor.execute(query)

            self._conn.commit()

            if self._db_type == "sqlite":
                row_id = self._cursor.lastrowid
            else:
                # PostgreSQL: query should include "RETURNING id"
                result = self._cursor.fetchone()
                if result:
                    row_id = result["id"] if isinstance(result, dict) else result[0]
                else:
                    row_id = self._cursor.lastrowid

            log.debug(f"Insert completed successfully, id={row_id}")
            return row_id

        except Exception as e:
            self._conn.rollback()
            log.error(f"Insert failed: {e}")
            log.debug(f"Failed query: {query[:200]}...")
            raise

    def _update(
        self,
        query: str,
        params: tuple | None = None,
    ) -> int:
        """
        Execute an UPDATE query and return the number of affected rows.

        Args:
            query: SQL UPDATE query
            params: Query parameters

        Returns:
            Number of rows affected
        """
        self._ensure_connected()

        try:
            if params:
                log.debug(f"Executing update with {len(params)} parameters")
                self._cursor.execute(query, params)
            else:
                log.debug("Executing update without parameters")
                self._cursor.execute(query)

            self._conn.commit()
            rowcount = self._cursor.rowcount
            log.debug(f"Update completed, {rowcount} rows affected")
            return rowcount

        except Exception as e:
            self._conn.rollback()
            log.error(f"Update failed: {e}")
            raise

    # =========================================================================
    # SOURCE REGISTRATION METHODS
    # =========================================================================

    def add_doc_source(
        self,
        file_path: str,
        name: str | None = None,
        version: str | None = None,
        metadata: Metadata | None = None,
    ) -> Source:
        """
        Register a document source (PDF, markdown, txt, json, csv, images, etc.)

        Extracts text content using PyMuPDF (for PDFs) or appropriate parser.
        Stores extracted content for verification.

        Args:
            file_path: Path to the document file
            name: Human-readable name (defaults to filename)
            version: Version identifier (e.g., "2024-01")
            metadata: Additional metadata (stored as JSON)

        Returns:
            Source object with assigned ID

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If file type is not supported
        """
        file_path = str(Path(file_path).resolve())
        log.debug(f"Registering document source: {file_path}")

        if not os.path.exists(file_path):
            log.error(f"File not found: {file_path}")
            raise FileNotFoundError(f"File not found: {file_path}")

        # Extract content based on file type
        content = self._extract_document_content(file_path)
        log.debug(f"Extracted {len(content)} characters from document")

        # Compute content hash for integrity verification
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Use filename as default name
        if name is None:
            name = os.path.basename(file_path)

        source = self._register_source(
            source_type=SourceType.DOCUMENT,
            identifier=file_path,
            name=name,
            content=content,
            content_hash=content_hash,
            version=version,
            metadata=metadata,
        )

        log.info(f"Registered document source [{source.id}]: {name}")
        return source

    def add_web_source(
        self,
        url: str,
        name: str | None = None,
        version: str | None = None,
        metadata: Metadata | None = None,
    ) -> Source:
        """
        Register a website source.

        Downloads and archives page content at registration time.
        Stores HTML and extracted text for verification.

        Args:
            url: URL of the web page
            name: Human-readable name (defaults to URL)
            version: Version identifier
            metadata: Additional metadata

        Returns:
            Source object with assigned ID

        Raises:
            ConnectionError: If page cannot be fetched
        """
        log.debug(f"Registering web source: {url}")

        # Fetch and archive web content
        content, fetch_metadata = self._fetch_web_content(url)
        log.debug(f"Fetched {len(content)} characters from web page")

        # Compute content hash
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Merge metadata
        if metadata:
            fetch_metadata.update(metadata)

        source = self._register_source(
            source_type=SourceType.WEBSITE,
            identifier=url,
            name=name or url,
            content=content,
            content_hash=content_hash,
            version=version,
            metadata=fetch_metadata,
        )

        log.info(f"Registered web source [{source.id}]: {name or url}")
        return source

    def add_db_source(
        self,
        identifier: str,
        name: str,
        content: str,
        query: str | None = None,
        result_description: str | None = None,
        metadata: Metadata | None = None,
    ) -> Source:
        """
        Register a database source (SQL, NoSQL, graph DB).

        Stores query and result description in metadata.
        Content field contains string representation of result.

        Args:
            identifier: Database/table identifier (e.g., "mydb.users")
            name: Human-readable name
            content: String representation of query result
            query: The query that produced the result
            result_description: Description of what the result contains
            metadata: Additional metadata

        Returns:
            Source object with assigned ID
        """
        log.debug(f"Registering database source: {identifier}")

        # Build metadata with query info
        db_metadata = {
            "query": query,
            "result_description": result_description,
        }
        if metadata:
            db_metadata.update(metadata)

        # Compute content hash
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        source = self._register_source(
            source_type=SourceType.DATABASE,
            identifier=identifier,
            name=name,
            content=content,
            content_hash=content_hash,
            metadata=db_metadata,
        )

        log.info(f"Registered database source [{source.id}]: {name}")
        return source

    def add_custom_source(
        self,
        name: str,
        content: str,
        description: str | None = None,
        metadata: Metadata | None = None,
    ) -> Source:
        """
        Register a custom/AI-generated source.

        For artifacts created by the agent itself:
        - Computed matrices or tables
        - Generated plots or visualizations
        - Analysis outputs

        Content is provided directly by the agent.

        Args:
            name: Human-readable name for the artifact
            content: The content/data of the artifact
            description: Description of what this artifact represents
            metadata: Additional metadata

        Returns:
            Source object with assigned ID
        """
        log.debug(f"Registering custom source: {name}")

        # Build metadata with description
        custom_metadata = {"description": description}
        if metadata:
            custom_metadata.update(metadata)

        # Compute content hash
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Use name + timestamp as identifier for custom sources
        timestamp = datetime.now(timezone.utc).isoformat()
        identifier = f"custom:{name}:{timestamp}"

        source = self._register_source(
            source_type=SourceType.CUSTOM,
            identifier=identifier,
            name=name,
            content=content,
            content_hash=content_hash,
            metadata=custom_metadata,
        )

        log.info(f"Registered custom source [{source.id}]: {name}")
        return source

    def _register_source(
        self,
        source_type: SourceType,
        identifier: str,
        name: str,
        content: str,
        content_hash: str | None = None,
        version: str | None = None,
        metadata: Metadata | None = None,
    ) -> Source:
        """
        Internal method to register a source in the database.

        Args:
            source_type: Type of source
            identifier: Unique identifier (path, URL, etc.)
            name: Human-readable name
            content: Full text content
            content_hash: SHA-256 hash of content
            version: Version string
            metadata: Additional metadata (JSON)

        Returns:
            Source object with assigned ID
        """
        self._ensure_connected()

        # Serialize metadata to JSON
        metadata_json = json.dumps(metadata) if metadata else None

        # Prepare insert query based on database type
        if self._db_type == "sqlite":
            query = """
                INSERT INTO sources (type, identifier, name, version, content, content_hash, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                source_type.value,
                identifier,
                name,
                version,
                content,
                content_hash,
                metadata_json,
            )
        else:
            query = """
                INSERT INTO sources (type, identifier, name, version, content, content_hash, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """
            params = (
                source_type.value,
                identifier,
                name,
                version,
                content,
                content_hash,
                metadata_json,
            )

        source_id = self._insert(query, params)

        return Source(
            id=source_id,
            type=source_type,
            identifier=identifier,
            name=name,
            version=version,
            content=content,
            content_hash=content_hash,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )

    def _extract_document_content(self, file_path: str) -> str:
        """
        Extract text content from a document file.

        Supports:
        - PDF: Uses PyMuPDF
        - Markdown, TXT, JSON, CSV: Direct read
        - Other: Attempt to read as text

        Args:
            file_path: Path to the document

        Returns:
            Extracted text content

        Raises:
            ValueError: If file type is not supported
            ImportError: If required library is not installed
        """
        ext = Path(file_path).suffix.lower()
        log.debug(f"Extracting content from {ext} file")

        if ext == ".pdf":
            return self._extract_pdf_content(file_path)
        elif ext in (".md", ".txt", ".json", ".csv", ".xml", ".html", ".htm"):
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            log.debug(f"Read {len(content)} characters from text file")
            return content
        else:
            # Try to read as text
            try:
                with open(file_path, encoding="utf-8") as f:
                    content = f.read()
                log.debug(f"Read {len(content)} characters from file (unknown extension)")
                return content
            except UnicodeDecodeError as e:
                log.error(f"Cannot extract text from binary file: {file_path}")
                raise ValueError(
                    f"Cannot extract text from binary file: {file_path}. "
                    "Supported formats: PDF, markdown, txt, json, csv, xml, html."
                ) from e

    def _extract_pdf_content(self, file_path: str) -> str:
        """
        Extract text content from a PDF file using PyMuPDF.

        Includes page markers for better citation locators.

        Args:
            file_path: Path to the PDF file

        Returns:
            Extracted text content with page markers
        """
        try:
            import fitz  # PyMuPDF

            log.debug(f"Opening PDF with PyMuPDF: {file_path}")
            doc = fitz.open(file_path)
            text_parts = []

            for page_num, page in enumerate(doc, start=1):
                text = page.get_text()
                text_parts.append(f"--- Page {page_num} ---\n{text}")

            doc.close()

            content = "\n\n".join(text_parts)
            log.debug(f"Extracted {len(content)} characters from {len(text_parts)} PDF pages")
            return content

        except ImportError as e:
            log.error("PyMuPDF not installed, cannot extract PDF content")
            raise ImportError(
                "PyMuPDF is required for PDF extraction. Install it with: pip install pymupdf"
            ) from e

    def _fetch_web_content(self, url: str) -> tuple[str, dict[str, Any]]:
        """
        Fetch and archive web page content.

        Downloads the page, extracts text, and stores metadata.

        Args:
            url: URL to fetch

        Returns:
            Tuple of (extracted_text, metadata)

        Raises:
            ConnectionError: If fetch fails
            ImportError: If required libraries are not installed
        """
        try:
            import requests
            from bs4 import BeautifulSoup

            log.debug(f"Fetching web content from: {url}")
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Extract text
            text = soup.get_text(separator="\n", strip=True)

            # Build metadata
            metadata = {
                "url": url,
                "accessed_at": datetime.now(timezone.utc).isoformat(),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
                "title": soup.title.string if soup.title else None,
            }

            log.debug(f"Fetched {len(text)} characters, title: {metadata.get('title', 'N/A')}")
            return text, metadata

        except ImportError as e:
            log.error("requests or beautifulsoup4 not installed")
            raise ImportError(
                "requests and beautifulsoup4 are required for web fetching. "
                "Install with: pip install requests beautifulsoup4"
            ) from e
        except Exception as e:
            log.error(f"Failed to fetch URL {url}: {e}")
            raise ConnectionError(f"Failed to fetch URL {url}: {e}") from e

    # =========================================================================
    # CITATION METHODS
    # =========================================================================

    def cite_doc(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: Locator,
        verbatim_quote: str | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote",
    ) -> CitationResult:
        """
        Create a citation from a document source.

        Triggers synchronous verification.
        Returns citation ID or error with verification feedback.

        Args:
            claim: The assertion being supported
            source_id: ID of the document source
            quote_context: Paragraph containing the evidence
            locator: Location data (page, section, etc.)
            verbatim_quote: Exact quoted text (optional)
            relevance_reasoning: Why this evidence supports the claim
            confidence: Agent's confidence level (high, medium, low)
            extraction_method: How information was extracted

        Returns:
            CitationResult with citation ID and verification status

        Raises:
            ValueError: If source doesn't exist or required fields missing
        """
        log.debug(f"Creating document citation for source [{source_id}]")
        return self._create_citation(
            claim=claim,
            source_id=source_id,
            quote_context=quote_context,
            locator=locator,
            verbatim_quote=verbatim_quote,
            relevance_reasoning=relevance_reasoning,
            confidence=confidence,
            extraction_method=extraction_method,
        )

    def cite_web(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: Locator,
        verbatim_quote: str | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote",
    ) -> CitationResult:
        """
        Create a citation from a website source.

        Uses archived content (from add_web_source) for verification.

        Args:
            claim: The assertion being supported
            source_id: ID of the website source
            quote_context: Paragraph containing the evidence
            locator: Location data (heading_context, accessed_at, etc.)
            verbatim_quote: Exact quoted text (optional)
            relevance_reasoning: Why this evidence supports the claim
            confidence: Agent's confidence level
            extraction_method: How information was extracted

        Returns:
            CitationResult with citation ID and verification status
        """
        log.debug(f"Creating web citation for source [{source_id}]")
        return self._create_citation(
            claim=claim,
            source_id=source_id,
            quote_context=quote_context,
            locator=locator,
            verbatim_quote=verbatim_quote,
            relevance_reasoning=relevance_reasoning,
            confidence=confidence,
            extraction_method=extraction_method,
        )

    def cite_db(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: Locator,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
        extraction_method: str = "aggregation",
    ) -> CitationResult:
        """
        Create a citation from a database source.

        Verification uses the query result stored in source.

        Args:
            claim: The assertion being supported
            source_id: ID of the database source
            quote_context: Description of the data/result
            locator: Location data (query, table, result_description, etc.)
            relevance_reasoning: Why this data supports the claim
            confidence: Agent's confidence level
            extraction_method: How information was extracted (default: aggregation)

        Returns:
            CitationResult with citation ID and verification status
        """
        log.debug(f"Creating database citation for source [{source_id}]")
        return self._create_citation(
            claim=claim,
            source_id=source_id,
            quote_context=quote_context,
            locator=locator,
            verbatim_quote=None,  # No verbatim quote for database sources
            relevance_reasoning=relevance_reasoning,
            confidence=confidence,
            extraction_method=extraction_method,
        )

    def cite_custom(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: Locator | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
    ) -> CitationResult:
        """
        Create a citation from a custom/AI-generated source.

        For citing matrices, plots, computed results, etc.

        Args:
            claim: The assertion being supported
            source_id: ID of the custom source
            quote_context: Description of the relevant part of the artifact
            locator: Optional location data
            relevance_reasoning: Why this artifact supports the claim
            confidence: Agent's confidence level

        Returns:
            CitationResult with citation ID and verification status
        """
        log.debug(f"Creating custom citation for source [{source_id}]")
        return self._create_citation(
            claim=claim,
            source_id=source_id,
            quote_context=quote_context,
            locator=locator or {},
            verbatim_quote=None,
            relevance_reasoning=relevance_reasoning,
            confidence=confidence,
            extraction_method="inference",  # Custom sources are typically inferences
        )

    def _create_citation(
        self,
        claim: str,
        source_id: int,
        quote_context: str,
        locator: Locator,
        verbatim_quote: str | None = None,
        relevance_reasoning: str | None = None,
        confidence: str = "high",
        extraction_method: str = "direct_quote",
        quote_language: str | None = None,
    ) -> CitationResult:
        """
        Internal method to create and verify a citation.

        Args:
            All citation fields

        Returns:
            CitationResult with verification status
        """
        self._ensure_connected()
        log.debug(f"Creating citation: claim='{claim[:50]}...', source_id={source_id}")

        # Validate source exists
        source = self.get_source(source_id)
        if source is None:
            log.error(f"Source not found: {source_id}")
            raise ValueError(f"Source not found: {source_id}")

        # Validate confidence and extraction_method enums
        try:
            conf = Confidence(confidence)
        except ValueError as e:
            log.error(f"Invalid confidence value: {confidence}")
            raise ValueError(
                f"Invalid confidence: {confidence}. Use 'high', 'medium', or 'low'."
            ) from e

        try:
            ExtractionMethod(extraction_method)  # Validate only
        except ValueError as e:
            log.error(f"Invalid extraction_method value: {extraction_method}")
            raise ValueError(
                f"Invalid extraction_method: {extraction_method}. "
                "Use 'direct_quote', 'paraphrase', 'inference', 'aggregation', or 'negative'."
            ) from e

        # Check if relevance_reasoning is required based on configuration
        self._validate_reasoning_requirement(conf, relevance_reasoning)

        # Get created_by from context for audit trail
        created_by = None
        if self.context:
            created_by = (
                f"{self.context.session_id}:{self.context.agent_id}"
                if self.context.agent_id
                else self.context.session_id
            )

        # Serialize locator to JSON
        locator_json = json.dumps(locator)

        # Insert citation with pending status
        if self._db_type == "sqlite":
            query = """
                INSERT INTO citations (
                    claim, verbatim_quote, quote_context, quote_language,
                    relevance_reasoning, confidence, extraction_method,
                    source_id, locator, verification_status, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """
            params = (
                claim,
                verbatim_quote,
                quote_context,
                quote_language,
                relevance_reasoning,
                confidence,
                extraction_method,
                source_id,
                locator_json,
                created_by,
            )
        else:
            query = """
                INSERT INTO citations (
                    claim, verbatim_quote, quote_context, quote_language,
                    relevance_reasoning, confidence, extraction_method,
                    source_id, locator, verification_status, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
            """
            params = (
                claim,
                verbatim_quote,
                quote_context,
                quote_language,
                relevance_reasoning,
                confidence,
                extraction_method,
                source_id,
                locator_json,
                created_by,
            )

        citation_id = self._insert(query, params)
        log.info(f"Created citation [{citation_id}] for source [{source_id}], pending verification")

        # Perform synchronous verification
        verification = self._verify_citation(
            citation_id, source, quote_context, verbatim_quote, claim, locator
        )

        # Update citation with verification result
        self._update_verification_status(citation_id, verification)

        # Build summary note for context window management
        source_snippet = source.name[:30] + "..." if len(source.name) > 30 else source.name
        status_icon = "+" if verification.is_verified else "x"
        summary_note = f"Cited {source_snippet} ({locator}) - {status_icon}"

        result = CitationResult(
            citation_id=citation_id,
            verification_status=(
                VerificationStatus.VERIFIED
                if verification.is_verified
                else VerificationStatus.FAILED
            ),
            similarity_score=verification.similarity_score,
            matched_location=verification.matched_location,
            verification_notes=verification.reasoning if not verification.is_verified else None,
            summary_note=summary_note,
        )

        log.info(
            f"Citation [{citation_id}] verification complete: "
            f"status={result.verification_status.value}, score={verification.similarity_score:.2f}"
        )
        return result

    def _validate_reasoning_requirement(
        self,
        confidence: Confidence,
        relevance_reasoning: str | None,
    ) -> None:
        """
        Validate that relevance_reasoning is provided when required.

        Based on CITATION_REASONING_REQUIRED environment variable:
        - none: Never required
        - low: Required when confidence is low (default)
        - medium: Required when confidence is low or medium
        - high: Always required

        Raises:
            ValueError: If reasoning is required but not provided
        """
        if self.reasoning_required == "none":
            return

        needs_reasoning = False

        if self.reasoning_required == "low" and confidence == Confidence.LOW:
            needs_reasoning = True
        elif self.reasoning_required == "medium" and confidence in (
            Confidence.LOW,
            Confidence.MEDIUM,
        ):
            needs_reasoning = True
        elif self.reasoning_required == "high":
            needs_reasoning = True

        if needs_reasoning and not relevance_reasoning:
            log.warning(
                f"relevance_reasoning required for confidence={confidence.value} "
                f"but not provided (config: {self.reasoning_required})"
            )
            raise ValueError(
                f"relevance_reasoning is required for confidence='{confidence.value}' "
                f"when CITATION_REASONING_REQUIRED='{self.reasoning_required}'"
            )

    def _update_verification_status(
        self,
        citation_id: int,
        verification: VerificationResult,
    ) -> None:
        """Update citation with verification results."""
        status = "verified" if verification.is_verified else "failed"

        # Handle matched_location - ensure it's JSON serializable
        matched_location = verification.matched_location
        matched_location_json = None
        if matched_location is not None:
            # Ensure it's a proper dict, not a Mock or other non-serializable type
            if isinstance(matched_location, dict):
                try:
                    matched_location_json = json.dumps(matched_location)
                except (TypeError, ValueError) as e:
                    log.warning(f"Could not serialize matched_location: {e}")
                    matched_location_json = None

        if self._db_type == "sqlite":
            query = """
                UPDATE citations
                SET verification_status = ?,
                    verification_notes = ?,
                    similarity_score = ?,
                    matched_location = ?
                WHERE id = ?
            """
            params = (
                status,
                verification.reasoning,
                verification.similarity_score,
                matched_location_json,
                citation_id,
            )
        else:
            query = """
                UPDATE citations
                SET verification_status = %s,
                    verification_notes = %s,
                    similarity_score = %s,
                    matched_location = %s
                WHERE id = %s
            """
            params = (
                status,
                verification.reasoning,
                verification.similarity_score,
                matched_location_json,
                citation_id,
            )

        self._update(query, params)
        log.debug(f"Updated verification status for citation [{citation_id}]: {status}")

    # =========================================================================
    # VERIFICATION METHODS
    # =========================================================================

    def _setup_llm_client(self) -> None:
        """
        Initialize the verification LLM client.

        Uses LangGraph's ChatOpenAI with custom base_url to support:
        - OpenAI API
        - OpenAI-compatible endpoints (llama.cpp, vLLM, Ollama)

        Reads CITATION_LLM_URL from environment for custom endpoints.
        """
        if self._llm_client is not None:
            return

        try:
            from langchain_openai import ChatOpenAI

            # Build model_kwargs for reasoning
            model_kwargs = {}
            if self.reasoning_level and self.reasoning_level != "none":
                model_kwargs["reasoning_effort"] = self.reasoning_level

            # Build kwargs for ChatOpenAI
            kwargs = {
                "model": self.llm_model,
                "temperature": 0.0,  # Deterministic verification
                "timeout": 120,  # 2 min - verification of 3 pages should be fast
                "max_retries": 1,  # 1 retry only - avoid long waits on failures
                "max_tokens": 4096,  # Verification response is small JSON
            }

            # Add model_kwargs if non-empty
            if model_kwargs:
                kwargs["model_kwargs"] = model_kwargs

            if self.llm_url:
                kwargs["base_url"] = self.llm_url

            if self.llm_api_key:
                kwargs["api_key"] = self.llm_api_key

            base_client = ChatOpenAI(**kwargs)

            # Use structured output so the model knows when to stop
            from pydantic import BaseModel, Field

            class VerificationResponse(BaseModel):
                """Structured response for citation verification."""

                verified: bool = Field(description="Whether the citation is valid")
                similarity_score: float = Field(
                    description="How closely the quote matches the source (0-1)"
                )
                matched_text: str | None = Field(
                    default=None, description="The actual text found in the source"
                )
                reasoning: str = Field(description="Brief explanation of the decision")

            self._llm_client = base_client.with_structured_output(VerificationResponse)
            log.debug(f"Verification LLM client initialized: {self.llm_model}")

        except ImportError as e:
            log.warning("langchain-openai not installed, verification will be limited")
            raise ImportError(
                "langchain-openai is required for verification. "
                "Install with: pip install langchain-openai"
            ) from e

    def _verify_citation(
        self,
        citation_id: int,
        source: Source,
        quote_context: str,
        verbatim_quote: str | None,
        claim: str,
        locator: dict | None = None,
    ) -> VerificationResult:
        """
        Verify a citation using the verification LLM.

        Checks if the quoted text exists in the source and supports the claim.

        Args:
            citation_id: ID of the citation being verified
            source: The source being cited
            quote_context: The context provided by the agent
            verbatim_quote: The exact quote (if any)
            claim: The claim being supported
            locator: Optional locator dict with page/section info

        Returns:
            VerificationResult with verification status
        """
        log.debug(f"Verifying citation [{citation_id}] against source [{source.id}]")

        # Skip verification if configured (for development/testing)
        if self.skip_verification:
            log.info(
                f"Skipping verification for citation [{citation_id}] (CITATION_SKIP_VERIFICATION=true)"
            )
            return VerificationResult(
                is_verified=True,
                similarity_score=1.0,
                reasoning="Verification skipped (CITATION_SKIP_VERIFICATION=true)",
            )

        try:
            self._setup_llm_client()
        except ImportError as e:
            log.warning(f"LLM not available for verification: {e}")
            return VerificationResult(
                is_verified=False,
                similarity_score=0.0,
                reasoning="Verification skipped: LLM not available",
                error=str(e),
            )

        # Extract relevant portion of source content based on locator
        source_content = self._extract_relevant_content(source.content, locator)

        # Build verification prompt
        prompt = self._build_verification_prompt(
            source_content=source_content,
            quote_context=quote_context,
            verbatim_quote=verbatim_quote,
            claim=claim,
        )

        try:
            import time

            from langchain_core.messages import HumanMessage, SystemMessage

            system_prompt = self._get_verification_system_prompt()
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=prompt),
            ]

            # Invoke with structured output
            start_time = time.time()
            log.debug("Sending verification request to LLM...")

            response = self._llm_client.invoke(messages)

            elapsed_ms = int((time.time() - start_time) * 1000)
            log.debug(
                f"Verification complete in {elapsed_ms}ms: "
                f"verified={response.verified}, score={response.similarity_score:.2f}"
            )

            return VerificationResult(
                is_verified=response.verified,
                similarity_score=response.similarity_score,
                reasoning=response.reasoning,
            )

        except Exception as e:
            log.error(f"Verification failed for citation [{citation_id}]: {e}")
            return VerificationResult(
                is_verified=False,
                similarity_score=0.0,
                reasoning=f"Verification error: {e}",
                error=str(e),
            )

    def _get_verification_system_prompt(self) -> str:
        """Return the system prompt for the verification LLM."""
        return f"""Reasoning: {self.reasoning_level}

You are a citation verification assistant. Your job is to verify that:
1. The quoted text (or similar text) exists in the provided source content
2. The quoted text actually supports the claim being made

Respond in JSON format with these fields:
- verified: boolean - whether the citation is valid
- similarity_score: float (0-1) - how closely the quote matches the source
- matched_text: string - the actual text found in the source (if any)
- matched_location: object - where in the source the text was found (optional)
- reasoning: string - brief explanation of your decision

Be strict but fair. Minor paraphrasing is acceptable if the meaning is preserved.
If the quote cannot be found or doesn't support the claim, explain why."""

    def _extract_relevant_content(
        self,
        full_content: str,
        locator: dict | None,
    ) -> str:
        """
        Extract the relevant portion of source content based on locator.

        For PDFs with page markers, extracts just the cited page plus
        adjacent pages for context. This dramatically reduces LLM context
        and speeds up verification.

        Args:
            full_content: Full source content (may include page markers)
            locator: Locator dict with page/section info, or None

        Returns:
            Relevant content portion, or full content if no locator
        """
        if not locator or "page" not in locator:
            log.debug("No page locator, using full content")
            return full_content

        page_num = locator["page"]
        log.debug(f"Extracting content for page {page_num}")

        # Split content by page markers
        import re

        page_pattern = r"--- Page (\d+) ---"
        pages = re.split(page_pattern, full_content)

        # pages list: ['', '1', 'content1', '2', 'content2', ...]
        # Build page_num -> content mapping
        page_contents = {}
        for i in range(1, len(pages), 2):
            if i + 1 < len(pages):
                try:
                    num = int(pages[i])
                    page_contents[num] = pages[i + 1].strip()
                except ValueError:
                    continue

        if not page_contents:
            log.debug("No page markers found in content, using full content")
            return full_content

        # Extract target page plus adjacent pages for context
        context_pages = 1  # Include 1 page before and after
        extracted_parts = []

        for p in range(page_num - context_pages, page_num + context_pages + 1):
            if p in page_contents:
                extracted_parts.append(f"--- Page {p} ---\n{page_contents[p]}")

        if not extracted_parts:
            log.warning(f"Page {page_num} not found in source, using full content")
            return full_content

        extracted = "\n\n".join(extracted_parts)
        log.debug(f"Extracted {len(extracted)} chars for page {page_num}")
        return extracted

    def _build_verification_prompt(
        self,
        source_content: str,
        quote_context: str,
        verbatim_quote: str | None,
        claim: str,
    ) -> str:
        """Build the verification prompt."""
        # Truncate source content if too long to fit in context
        max_content_length = 50000
        if len(source_content) > max_content_length:
            source_content = (
                source_content[:max_content_length]
                + f"\n\n[... truncated, {len(source_content) - max_content_length} more characters ...]"
            )
            log.debug(f"Truncated source content to {max_content_length} characters")

        prompt = f"""Please verify this citation:

## Claim
{claim}

## Quoted Context
{quote_context}
"""

        if verbatim_quote:
            prompt += f"""
## Verbatim Quote
{verbatim_quote}
"""

        prompt += f"""
## Source Content
{source_content}

Please verify that the quoted text exists in the source and supports the claim.
Respond in JSON format."""

        return prompt

    def _parse_verification_response(self, response: str) -> VerificationResult:
        """Parse the verification LLM response."""
        try:
            import re

            # Look for JSON in the response
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                # Try parsing the entire response as JSON
                data = json.loads(response)

            return VerificationResult(
                is_verified=data.get("verified", False),
                similarity_score=float(data.get("similarity_score", 0.0)),
                matched_text=data.get("matched_text"),
                matched_location=data.get("matched_location"),
                reasoning=data.get("reasoning"),
            )

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning(f"Failed to parse verification response as JSON: {e}")
            # Try to determine verification from response text
            is_verified = (
                "verified" in response.lower()
                and "not verified" not in response.lower()
                and "unverified" not in response.lower()
            )
            return VerificationResult(
                is_verified=is_verified,
                similarity_score=0.5 if is_verified else 0.0,
                reasoning=response[:500],
            )

    # =========================================================================
    # RETRIEVAL METHODS
    # =========================================================================

    def get_source(self, source_id: int) -> Source | None:
        """
        Get a source by ID.

        Args:
            source_id: The ID of the source to retrieve

        Returns:
            Source object or None if not found
        """
        log.debug(f"Retrieving source [{source_id}]")

        if self._db_type == "sqlite":
            query = "SELECT * FROM sources WHERE id = ?"
        else:
            query = "SELECT * FROM sources WHERE id = %s"

        results = self._query(query, (source_id,))
        if not results:
            log.debug(f"Source [{source_id}] not found")
            return None

        return self._row_to_source(results[0])

    def get_citation(self, citation_id: int) -> Citation | None:
        """
        Get a citation by ID.

        Args:
            citation_id: The ID of the citation to retrieve

        Returns:
            Citation object or None if not found
        """
        log.debug(f"Retrieving citation [{citation_id}]")

        if self._db_type == "sqlite":
            query = "SELECT * FROM citations WHERE id = ?"
        else:
            query = "SELECT * FROM citations WHERE id = %s"

        results = self._query(query, (citation_id,))
        if not results:
            log.debug(f"Citation [{citation_id}] not found")
            return None

        return self._row_to_citation(results[0])

    def get_citations_for_source(self, source_id: int) -> list[Citation]:
        """
        Get all citations referencing a source.

        Args:
            source_id: The ID of the source

        Returns:
            List of Citation objects
        """
        log.debug(f"Retrieving citations for source [{source_id}]")

        if self._db_type == "sqlite":
            query = "SELECT * FROM citations WHERE source_id = ? ORDER BY created_at DESC"
        else:
            query = "SELECT * FROM citations WHERE source_id = %s ORDER BY created_at DESC"

        results = self._query(query, (source_id,))
        citations = [self._row_to_citation(row) for row in results]
        log.debug(f"Found {len(citations)} citations for source [{source_id}]")
        return citations

    def get_citations_by_session(self, session_id: str) -> list[Citation]:
        """
        Get all citations created in a session.

        Args:
            session_id: The session ID to filter by

        Returns:
            List of Citation objects
        """
        log.debug(f"Retrieving citations for session: {session_id}")

        if self._db_type == "sqlite":
            query = "SELECT * FROM citations WHERE created_by LIKE ? ORDER BY created_at DESC"
            params = (f"{session_id}%",)
        else:
            query = "SELECT * FROM citations WHERE created_by LIKE %s ORDER BY created_at DESC"
            params = (f"{session_id}%",)

        results = self._query(query, params)
        citations = [self._row_to_citation(row) for row in results]
        log.debug(f"Found {len(citations)} citations for session: {session_id}")
        return citations

    def list_sources(self, source_type: str | None = None) -> list[Source]:
        """
        List all registered sources, optionally filtered by type.

        Args:
            source_type: Optional type filter (document, website, database, custom)

        Returns:
            List of Source objects
        """
        log.debug(f"Listing sources, type filter: {source_type or 'all'}")

        if source_type:
            if self._db_type == "sqlite":
                query = "SELECT * FROM sources WHERE type = ? ORDER BY created_at DESC"
            else:
                query = "SELECT * FROM sources WHERE type = %s ORDER BY created_at DESC"
            results = self._query(query, (source_type,))
        else:
            query = "SELECT * FROM sources ORDER BY created_at DESC"
            results = self._query(query)

        sources = [self._row_to_source(row) for row in results]
        log.debug(f"Found {len(sources)} sources")
        return sources

    def list_citations(
        self,
        source_id: int | None = None,
        session_id: str | None = None,
        verification_status: str | None = None,
    ) -> list[Citation]:
        """
        List citations with optional filters.

        Args:
            source_id: Filter by source ID
            session_id: Filter by session ID
            verification_status: Filter by verification status

        Returns:
            List of Citation objects
        """
        log.debug(
            f"Listing citations: source_id={source_id}, session_id={session_id}, "
            f"status={verification_status}"
        )

        conditions = []
        params = []

        if source_id is not None:
            conditions.append("source_id = ?")
            params.append(source_id)

        if session_id is not None:
            conditions.append("created_by LIKE ?")
            params.append(f"{session_id}%")

        if verification_status is not None:
            conditions.append("verification_status = ?")
            params.append(verification_status)

        query = "SELECT * FROM citations"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"

        # Adjust placeholders for PostgreSQL
        if self._db_type == "postgresql":
            query = query.replace("?", "%s")

        results = self._query(query, tuple(params) if params else None)
        citations = [self._row_to_citation(row) for row in results]
        log.debug(f"Found {len(citations)} citations matching filters")
        return citations

    def _row_to_source(self, row: dict[str, Any]) -> Source:
        """Convert a database row to a Source object."""
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else None

        created_at = row.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        return Source(
            id=row["id"],
            type=SourceType(row["type"]),
            identifier=row["identifier"],
            name=row["name"],
            version=row.get("version"),
            content=row["content"],
            content_hash=row.get("content_hash"),
            metadata=metadata,
            created_at=created_at,
        )

    def _row_to_citation(self, row: dict[str, Any]) -> Citation:
        """Convert a database row to a Citation object."""
        locator = row.get("locator")
        if isinstance(locator, str):
            locator = json.loads(locator) if locator else {}

        matched_location = row.get("matched_location")
        if isinstance(matched_location, str):
            matched_location = json.loads(matched_location) if matched_location else None

        created_at = row.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        return Citation(
            id=row["id"],
            claim=row["claim"],
            verbatim_quote=row.get("verbatim_quote"),
            quote_context=row["quote_context"],
            quote_language=row.get("quote_language"),
            relevance_reasoning=row.get("relevance_reasoning"),
            confidence=Confidence(row.get("confidence", "high")),
            extraction_method=ExtractionMethod(row.get("extraction_method", "direct_quote")),
            source_id=row["source_id"],
            locator=locator,
            verification_status=VerificationStatus(row.get("verification_status", "pending")),
            verification_notes=row.get("verification_notes"),
            similarity_score=row.get("similarity_score"),
            matched_location=matched_location,
            created_at=created_at,
            created_by=row.get("created_by"),
        )

    # =========================================================================
    # EXPORT METHODS
    # =========================================================================

    def format_citation(
        self,
        citation_id: int,
        style: str = "inline",
    ) -> str:
        """
        Format a single citation for display/export.

        Args:
            citation_id: ID of the citation to format
            style: Citation style (inline, harvard, ieee, bibtex, apa)

        Returns:
            Formatted citation string

        Raises:
            ValueError: If citation or source not found, or unknown style
        """
        log.debug(f"Formatting citation [{citation_id}] in {style} style")

        citation = self.get_citation(citation_id)
        if citation is None:
            raise ValueError(f"Citation not found: {citation_id}")

        source = self.get_source(citation.source_id)
        if source is None:
            raise ValueError(f"Source not found for citation {citation_id}")

        if style == "inline":
            return f"[{citation.id}]"

        elif style == "harvard":
            # Author (Year) Title. Source.
            return f"{source.name} ({source.version or 'n.d.'})"

        elif style == "ieee":
            # [N] Author, "Title," Source, Year.
            return f"[{citation.id}] {source.name}, {source.version or 'n.d.'}"

        elif style == "bibtex":
            # Generate BibTeX entry
            entry_type = "misc"
            if source.type == SourceType.DOCUMENT:
                entry_type = "book"
            elif source.type == SourceType.WEBSITE:
                entry_type = "online"

            return f"""@{entry_type}{{cite{citation.id},
    title = {{{source.name}}},
    year = {{{source.version or "n.d."}}},
    note = {{{source.identifier}}}
}}"""

        elif style == "apa":
            # Author. (Year). Title. Source.
            return f"{source.name}. ({source.version or 'n.d.'})."

        else:
            raise ValueError(
                f"Unknown citation style: {style}. Supported: inline, harvard, ieee, bibtex, apa"
            )

    def export_bibliography(
        self,
        session_id: str | None = None,
        style: str = "harvard",
    ) -> str:
        """
        Export all citations (or session citations) as bibliography.

        Args:
            session_id: Optional session ID to filter citations
            style: Citation style

        Returns:
            Formatted bibliography string
        """
        log.debug(f"Exporting bibliography in {style} style, session_id={session_id}")

        citations = self.list_citations(session_id=session_id)

        if not citations:
            return "No citations found."

        lines = []
        for citation in citations:
            formatted = self.format_citation(citation.id, style)
            lines.append(formatted)

        bibliography = "\n\n".join(lines)
        log.info(f"Exported {len(citations)} citations as bibliography")
        return bibliography

    # =========================================================================
    # STATISTICS & REPORTING
    # =========================================================================

    def get_statistics(self) -> dict[str, Any]:
        """
        Get statistics about sources and citations.

        Returns:
            Dictionary with counts and breakdowns
        """
        log.debug("Calculating statistics")
        self._ensure_connected()

        stats = {
            "sources": {},
            "citations": {},
        }

        # Count sources by type
        if self._db_type == "sqlite":
            query = "SELECT type, COUNT(*) as count FROM sources GROUP BY type"
        else:
            query = "SELECT type, COUNT(*) as count FROM sources GROUP BY type"

        results = self._query(query)
        stats["sources"]["by_type"] = {row["type"]: row["count"] for row in results}
        stats["sources"]["total"] = sum(stats["sources"]["by_type"].values())

        # Count citations by verification status
        if self._db_type == "sqlite":
            query = "SELECT verification_status, COUNT(*) as count FROM citations GROUP BY verification_status"
        else:
            query = "SELECT verification_status, COUNT(*) as count FROM citations GROUP BY verification_status"

        results = self._query(query)
        stats["citations"]["by_status"] = {
            row["verification_status"]: row["count"] for row in results
        }
        stats["citations"]["total"] = sum(stats["citations"]["by_status"].values())

        log.debug(
            f"Statistics: {stats['sources']['total']} sources, {stats['citations']['total']} citations"
        )
        return stats
