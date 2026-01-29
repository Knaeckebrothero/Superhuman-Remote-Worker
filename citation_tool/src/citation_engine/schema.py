"""
Database Schema for Citation Engine
====================================
Defines the SQL schemas for both SQLite and PostgreSQL backends.
Includes migration support for schema versioning.

Based on the Citation & Provenance Engine Design Document v0.3.
"""

import logging

log = logging.getLogger(__name__)

# Current schema version
SCHEMA_VERSION = 1


# =============================================================================
# SQLite Schema
# =============================================================================
# Uses TEXT for JSON fields, INTEGER for booleans (0/1)

SQLITE_SCHEMA = """
-- Schema migrations table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

-- Sources table: canonical documents, websites, databases, or custom artifacts
-- job_id is TEXT in SQLite (no FK since jobs table doesn't exist in standalone mode)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    type TEXT NOT NULL CHECK(type IN ('document', 'website', 'database', 'custom')),
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata TEXT,  -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Index on identifier for quick lookups
CREATE INDEX IF NOT EXISTS idx_sources_job_id ON sources(job_id);
CREATE INDEX IF NOT EXISTS idx_sources_identifier ON sources(identifier);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);

-- Citations table: links claims to their supporting evidence
-- job_id is TEXT in SQLite (no FK since jobs table doesn't exist in standalone mode)
CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    claim TEXT NOT NULL,
    verbatim_quote TEXT,
    quote_context TEXT NOT NULL,
    quote_language TEXT,
    relevance_reasoning TEXT,
    confidence TEXT DEFAULT 'high' CHECK(confidence IN ('high', 'medium', 'low')),
    extraction_method TEXT DEFAULT 'direct_quote' CHECK(extraction_method IN ('direct_quote', 'paraphrase', 'inference', 'aggregation', 'negative')),
    source_id INTEGER NOT NULL,
    locator TEXT NOT NULL,  -- JSON
    verification_status TEXT DEFAULT 'pending' CHECK(verification_status IN ('pending', 'verified', 'failed', 'unverified')),
    verification_notes TEXT,
    similarity_score REAL,
    matched_location TEXT,  -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_citations_job_id ON citations(job_id);
CREATE INDEX IF NOT EXISTS idx_citations_source_id ON citations(source_id);
CREATE INDEX IF NOT EXISTS idx_citations_created_by ON citations(created_by);
CREATE INDEX IF NOT EXISTS idx_citations_verification_status ON citations(verification_status);
CREATE INDEX IF NOT EXISTS idx_citations_created_at ON citations(created_at);
CREATE INDEX IF NOT EXISTS idx_citations_claim ON citations(claim);

-- Insert initial migration record
INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables');
"""


# =============================================================================
# PostgreSQL Schema
# =============================================================================
# Uses JSONB for JSON fields, proper ENUM types, TIMESTAMP WITH TIME ZONE

POSTGRESQL_SCHEMA = """
-- Schema migrations table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    description TEXT
);

-- Create ENUM types if they don't exist
DO $$ BEGIN
    CREATE TYPE source_type AS ENUM ('document', 'website', 'database', 'custom');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE confidence_level AS ENUM ('high', 'medium', 'low');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE extraction_method AS ENUM ('direct_quote', 'paraphrase', 'inference', 'aggregation', 'negative');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE verification_status AS ENUM ('pending', 'verified', 'failed', 'unverified');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- Sources table
-- job_id is UUID but no FK here (FK is in main project's schema.sql)
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    job_id UUID,
    type source_type NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Index on identifier for quick lookups
CREATE INDEX IF NOT EXISTS idx_sources_job_id ON sources(job_id);
CREATE INDEX IF NOT EXISTS idx_sources_identifier ON sources(identifier);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);

-- Citations table
-- job_id is UUID but no FK here (FK is in main project's schema.sql)
CREATE TABLE IF NOT EXISTS citations (
    id SERIAL PRIMARY KEY,
    job_id UUID,
    claim TEXT NOT NULL,
    verbatim_quote TEXT,
    quote_context TEXT NOT NULL,
    quote_language TEXT,
    relevance_reasoning TEXT,
    confidence confidence_level DEFAULT 'high',
    extraction_method extraction_method DEFAULT 'direct_quote',
    source_id INTEGER NOT NULL REFERENCES sources(id),
    locator JSONB NOT NULL,
    verification_status verification_status DEFAULT 'pending',
    verification_notes TEXT,
    similarity_score REAL,
    matched_location JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_by TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_citations_job_id ON citations(job_id);
CREATE INDEX IF NOT EXISTS idx_citations_source_id ON citations(source_id);
CREATE INDEX IF NOT EXISTS idx_citations_created_by ON citations(created_by);
CREATE INDEX IF NOT EXISTS idx_citations_verification_status ON citations(verification_status);
CREATE INDEX IF NOT EXISTS idx_citations_created_at ON citations(created_at);

-- GIN index for JSONB queries on locator and metadata
CREATE INDEX IF NOT EXISTS idx_citations_locator ON citations USING GIN (locator);
CREATE INDEX IF NOT EXISTS idx_sources_metadata ON sources USING GIN (metadata);

-- Full-text search index on claim (PostgreSQL specific)
CREATE INDEX IF NOT EXISTS idx_citations_claim_fts ON citations USING GIN (to_tsvector('english', claim));

-- Insert initial migration record
INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial schema with sources and citations tables')
ON CONFLICT (version) DO NOTHING;
"""


# =============================================================================
# Schema Verification Queries
# =============================================================================

SQLITE_VERIFY_TABLES = """
SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sources', 'citations', 'schema_migrations');
"""

POSTGRESQL_VERIFY_TABLES = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name IN ('sources', 'citations', 'schema_migrations');
"""

SQLITE_GET_VERSION = """
SELECT COALESCE(MAX(version), 0) as version FROM schema_migrations;
"""

POSTGRESQL_GET_VERSION = """
SELECT COALESCE(MAX(version), 0) as version FROM schema_migrations;
"""


# =============================================================================
# Migrations
# =============================================================================
# Each migration is a tuple of (version, description, sqlite_sql, postgresql_sql)
# Migrations are applied in order, only if not already applied

MIGRATIONS: list[tuple[int, str, str, str]] = [
    # Version 1 is the initial schema, handled above
    # Add future migrations here as tuples:
    # (2, "Description", "SQLITE SQL...", "POSTGRESQL SQL..."),
]


# =============================================================================
# Public Functions
# =============================================================================


def get_schema(db_type: str) -> str:
    """
    Get the appropriate schema for the database type.

    Args:
        db_type: Either 'sqlite' or 'postgresql'

    Returns:
        SQL schema string

    Raises:
        ValueError: If db_type is not recognized
    """
    if db_type == "sqlite":
        return SQLITE_SCHEMA
    elif db_type == "postgresql":
        return POSTGRESQL_SCHEMA
    else:
        raise ValueError(f"Unknown database type: {db_type}. Expected 'sqlite' or 'postgresql'.")


def get_verify_query(db_type: str) -> str:
    """
    Get the table verification query for the database type.

    Args:
        db_type: Either 'sqlite' or 'postgresql'

    Returns:
        SQL query string

    Raises:
        ValueError: If db_type is not recognized
    """
    if db_type == "sqlite":
        return SQLITE_VERIFY_TABLES
    elif db_type == "postgresql":
        return POSTGRESQL_VERIFY_TABLES
    else:
        raise ValueError(f"Unknown database type: {db_type}. Expected 'sqlite' or 'postgresql'.")


def get_version_query(db_type: str) -> str:
    """
    Get the schema version query for the database type.

    Args:
        db_type: Either 'sqlite' or 'postgresql'

    Returns:
        SQL query string
    """
    if db_type == "sqlite":
        return SQLITE_GET_VERSION
    elif db_type == "postgresql":
        return POSTGRESQL_GET_VERSION
    else:
        raise ValueError(f"Unknown database type: {db_type}. Expected 'sqlite' or 'postgresql'.")


def get_pending_migrations(current_version: int, db_type: str) -> list[tuple[int, str, str]]:
    """
    Get migrations that need to be applied.

    Args:
        current_version: Current schema version in database
        db_type: Either 'sqlite' or 'postgresql'

    Returns:
        List of (version, description, sql) tuples for pending migrations
    """
    pending = []
    for version, description, sqlite_sql, postgresql_sql in MIGRATIONS:
        if version > current_version:
            sql = sqlite_sql if db_type == "sqlite" else postgresql_sql
            pending.append((version, description, sql))
    return pending


def get_migration_insert(db_type: str, version: int, description: str) -> str:
    """
    Get the SQL to record a migration as applied.

    Args:
        db_type: Either 'sqlite' or 'postgresql'
        version: Migration version number
        description: Migration description

    Returns:
        SQL INSERT statement
    """
    if db_type == "sqlite":
        return f"INSERT INTO schema_migrations (version, description) VALUES ({version}, '{description}')"
    else:
        return f"INSERT INTO schema_migrations (version, description) VALUES ({version}, '{description}')"


def get_current_schema_version() -> int:
    """
    Get the current schema version defined in code.

    Returns:
        Current schema version number
    """
    return SCHEMA_VERSION
