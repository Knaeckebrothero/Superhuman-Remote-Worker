"""
Pytest configuration for the vendored citation_engine test suite.

After the native integration (decision D3), the engine is Postgres-only and
async, so this package's coverage is the single async round-trip in
``test_integration_postgres.py`` (skip-guarded behind ``RUN_POSTGRES_TESTS``).
The former SQLite unit fixtures + sample-data fixtures were retired with the
SQLite mode; the round-trip is self-contained.

citation_engine is importable from the repo root (the top-level tests/conftest.py
puts it on sys.path). Environment comes from the process / cluster — e.g.
CITATION_DB_URL (or VECTOR_POSTGRES_*) for the Postgres integration — not a
local .env file.
"""


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "postgres: mark test as requiring PostgreSQL database"
    )
    config.addinivalue_line("markers", "llm: mark test as requiring LLM endpoint")
    config.addinivalue_line("markers", "web: mark test as requiring network access")
    config.addinivalue_line("markers", "integration: mark test as integration test")
