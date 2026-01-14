"""Database schema and utilities for Graph-RAG system."""

from pathlib import Path

SCHEMA_DIR = Path(__file__).parent
SCHEMA_FILE = SCHEMA_DIR / "schema.sql"
SCHEMA_VECTOR_FILE = SCHEMA_DIR / "schema_vector.sql"
