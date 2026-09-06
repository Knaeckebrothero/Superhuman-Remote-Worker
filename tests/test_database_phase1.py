"""Tests for Phase 1 database refactoring.

Tests the new PostgresDB and Neo4jDB classes.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.database import PostgresDB, Neo4jDB


class TestPostgresDB:
    """Test PostgresDB class."""

    def test_init_without_connection_string_uses_env(self):
        """Test that PostgresDB reads from environment."""
        with patch.dict(
            "os.environ", {"DATABASE_URL": "postgresql://test"}, clear=True
        ):
            db = PostgresDB()
            assert db._connection_string == "postgresql://test"
            assert not db.is_connected

    def test_init_with_connection_string(self):
        """Test PostgresDB with explicit connection string."""
        db = PostgresDB(connection_string="postgresql://custom")
        assert db._connection_string == "postgresql://custom"

    def test_init_raises_without_connection_string(self):
        """Test that PostgresDB raises error without connection string."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove DATABASE_URL if it exists
            import os

            os.environ.pop("DATABASE_URL", None)
            with pytest.raises(ValueError, match="connection string required"):
                PostgresDB()

    def test_namespaces_initialized(self):
        """Test that namespaces are initialized."""
        with patch.dict(
            "os.environ", {"DATABASE_URL": "postgresql://test"}, clear=True
        ):
            db = PostgresDB()
            assert hasattr(db, "jobs")
            assert hasattr(db, "config_overrides")
            # No citations namespace: citations live in the vector store and are
            # written only through CitationEngine. The old CitationsNamespace
            # here targeted this app DB, which has no citations table.
            assert not hasattr(db, "citations")

    def test_row_to_dict_with_none(self):
        """Test _row_to_dict handles None."""
        result = PostgresDB._row_to_dict(None)
        assert result is None

    def test_row_to_dict_with_record(self):
        """Test _row_to_dict converts record."""
        # Mock asyncpg Record (dict-like)
        mock_record = {"id": 1, "name": "test"}

        result = PostgresDB._row_to_dict(mock_record)
        assert result == {"id": 1, "name": "test"}

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        """Test connection lifecycle without reaching an ambient database."""
        pool = MagicMock()
        pool.close = AsyncMock()
        create_pool = AsyncMock(return_value=pool)

        with patch("agent.database.postgres_db.asyncpg.create_pool", create_pool):
            db = PostgresDB(connection_string="postgresql://test")
            await db.connect()
            assert db.is_connected

            await db.close()
            assert not db.is_connected

        create_pool.assert_awaited_once()
        pool.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jobs_merge_context_strips_server_owned_pull_request(self):
        db = PostgresDB(connection_string="postgresql://test")
        db.execute = AsyncMock(return_value="UPDATE 1")
        job_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

        updated = await db.jobs.merge_context(
            job_id,
            {"pull_request": {"number": 9, "url": "https://gh/pr/9"}},
        )

        assert updated is True
        sql, payload, bound_job_id = db.execute.await_args.args
        normalized_sql = " ".join(sql.split())
        assert "COALESCE(context, '{}'::jsonb) || $1::jsonb" in normalized_sql
        assert json.loads(payload) == {}
        assert bound_job_id == job_id


class TestNeo4jDB:
    """Test Neo4jDB class."""

    def test_init_with_explicit_params(self):
        """Test Neo4jDB with explicit parameters (required)."""
        db = Neo4jDB(uri="bolt://custom", username="admin", password="secret")
        assert db._uri == "bolt://custom"
        assert db._username == "admin"
        assert db._password == "secret"
        assert not db.is_connected

    def test_connect_disconnect_no_driver(self):
        """Test connection lifecycle without actual Neo4j."""
        graph_database = MagicMock()
        graph_database.driver.side_effect = RuntimeError("service unavailable")

        with patch("shared.runtime.database.neo4j_db.GraphDatabase", graph_database):
            db = Neo4jDB(uri="bolt://nonexistent", username="neo4j", password="test")
            # Should return False if connection fails.
            result = db.connect()
            assert result is False

        db.close()  # Should not raise
        assert not db.is_connected


class TestDependencyInjection:
    """Test that instances can be created and injected."""

    def test_postgres_instance_creation(self):
        """Test PostgresDB instance creation."""
        with patch.dict(
            "os.environ", {"DATABASE_URL": "postgresql://test"}, clear=True
        ):
            db = PostgresDB()
            assert isinstance(db, PostgresDB)

    def test_neo4j_instance_creation(self):
        """Test Neo4jDB instance creation."""
        db = Neo4jDB(uri="bolt://test", username="neo4j", password="test")
        assert isinstance(db, Neo4jDB)


class TestBackwardCompatibility:
    """Test that old API still works."""

    def test_old_imports_work(self):
        """Test that canonical classes can be imported."""
        from agent.database import PostgresDB, Neo4jDB

        assert PostgresDB is not None
        assert Neo4jDB is not None

    def test_old_functions_work(self):
        """Test that database classes have core methods."""
        from agent.database import PostgresDB, Neo4jDB

        # PostgresDB core methods
        assert hasattr(PostgresDB, "connect")
        assert hasattr(PostgresDB, "close")
        assert hasattr(PostgresDB, "execute")

        # Neo4jDB core methods
        assert hasattr(Neo4jDB, "connect")
        assert hasattr(Neo4jDB, "close")
        assert hasattr(Neo4jDB, "execute_query")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
