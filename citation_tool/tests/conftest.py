"""
Pytest configuration and shared fixtures for Citation Engine tests.

This module provides:
- Environment variable loading from .env
- Shared fixtures for database connections
- Custom markers for integration tests
- Sample data fixtures
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Load environment variables from .env file
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    # python-dotenv not installed, rely on system environment
    pass


# =============================================================================
# Custom Markers
# =============================================================================


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "postgres: mark test as requiring PostgreSQL database"
    )
    config.addinivalue_line(
        "markers", "llm: mark test as requiring LLM endpoint"
    )
    config.addinivalue_line(
        "markers", "web: mark test as requiring network access"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )


# =============================================================================
# Database Fixtures
# =============================================================================


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def postgres_db_url():
    """Get PostgreSQL connection URL from environment."""
    url = os.getenv("CITATION_DB_URL")
    if not url:
        # Build from individual components
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db = os.getenv("POSTGRES_DB", "citations")
        user = os.getenv("POSTGRES_USER", "citation_user")
        password = os.getenv("POSTGRES_PASSWORD", "citation_pass")
        url = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    return url


@pytest.fixture
def llm_config():
    """Get LLM configuration from environment."""
    return {
        "url": os.getenv("CITATION_LLM_URL"),
        "model": os.getenv("CITATION_LLM_MODEL", "gpt-4o-mini"),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
    }


# =============================================================================
# Sample Data Fixtures
# =============================================================================


@pytest.fixture
def sample_text_file():
    """Create a sample text file for testing."""
    content = """
    This is a sample document for testing.

    Section 1: Introduction
    The purpose of this document is to provide test content.

    Section 2: Main Content
    Companies must store transaction data for 10 years according to regulations.
    This is an important requirement that must be followed.

    Section 3: Technical Details
    The system processes approximately 1000 requests per second.
    Memory usage should not exceed 512MB under normal operation.

    Section 4: Conclusion
    Testing is important for software quality.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        text_path = f.name
    yield text_path
    if os.path.exists(text_path):
        os.unlink(text_path)


@pytest.fixture
def sample_pdf_content():
    """Sample content that would be extracted from a PDF."""
    return """--- Page 1 ---
    Citation Engine Documentation
    Version 1.0

    This document describes the Citation Engine architecture.

    --- Page 2 ---
    Chapter 1: Overview

    The Citation Engine is designed to provide structured citations
    for AI agents. It ensures that claims are properly backed by evidence.

    Key features include:
    - Automatic verification of quotes
    - Support for multiple source types
    - PostgreSQL backend for multi-agent deployments

    --- Page 3 ---
    Chapter 2: Implementation

    The engine uses a synchronous verification model where
    each citation is verified before being stored.

    Performance metrics show 95% accuracy in quote matching.
    """


@pytest.fixture
def sample_database_content():
    """Sample database query result content."""
    return """Query: SELECT architecture, COUNT(*) as count FROM papers GROUP BY architecture

    Results:
    +---------------+-------+
    | architecture  | count |
    +---------------+-------+
    | microservices |    42 |
    | monolith      |    18 |
    | serverless    |     8 |
    | hybrid        |     5 |
    +---------------+-------+

    Total: 73 papers analyzed
    """


@pytest.fixture
def sample_custom_content():
    """Sample AI-generated analysis content."""
    return """Paper Analysis Matrix - Generated 2024-01-15

    Summary Statistics:
    - Total papers: 73
    - Microservices adoption: 57.5% (42/73)
    - Monolith architecture: 24.7% (18/73)
    - Other architectures: 17.8% (13/73)

    Trend Analysis:
    The data shows a clear trend toward microservices architecture,
    with adoption increasing year-over-year since 2019.
    """
