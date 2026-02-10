"""Tests for Phase 1 database refactoring.

Tests the new PostgresDB, Neo4jDB, and MongoDB classes.
"""

import pytest
from unittest.mock import patch
from src.database import PostgresDB, Neo4jDB, MongoDB


class TestPostgresDB:
    """Test PostgresDB class."""

    def test_init_without_connection_string_uses_env(self):
        """Test that PostgresDB reads from environment."""
        with patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test'}):
            db = PostgresDB()
            assert db._connection_string == 'postgresql://test'
            assert not db.is_connected

    def test_init_with_connection_string(self):
        """Test PostgresDB with explicit connection string."""
        db = PostgresDB(connection_string='postgresql://custom')
        assert db._connection_string == 'postgresql://custom'

    def test_init_raises_without_connection_string(self):
        """Test that PostgresDB raises error without connection string."""
        with patch.dict('os.environ', {}, clear=True):
            # Remove DATABASE_URL if it exists
            import os
            os.environ.pop('DATABASE_URL', None)
            with pytest.raises(ValueError, match="connection string required"):
                PostgresDB()

    def test_namespaces_initialized(self):
        """Test that namespaces are initialized."""
        with patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test'}):
            db = PostgresDB()
            assert hasattr(db, 'jobs')
            assert hasattr(db, 'requirements')
            assert hasattr(db, 'citations')

    def test_row_to_dict_with_none(self):
        """Test _row_to_dict handles None."""
        result = PostgresDB._row_to_dict(None)
        assert result is None

    def test_row_to_dict_with_record(self):
        """Test _row_to_dict converts record."""
        # Mock asyncpg Record (dict-like)
        mock_record = {'id': 1, 'name': 'test'}

        result = PostgresDB._row_to_dict(mock_record)
        assert result == {'id': 1, 'name': 'test'}

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        """Test connection lifecycle (requires database)."""
        # Skip if no DATABASE_URL
        import os
        if not os.getenv('DATABASE_URL'):
            pytest.skip("DATABASE_URL not set")

        db = PostgresDB()
        await db.connect()
        assert db.is_connected

        await db.close()
        assert not db.is_connected


class TestNeo4jDB:
    """Test Neo4jDB class."""

    def test_init_with_explicit_params(self):
        """Test Neo4jDB with explicit parameters (required)."""
        db = Neo4jDB(
            uri='bolt://custom',
            username='admin',
            password='secret'
        )
        assert db._uri == 'bolt://custom'
        assert db._username == 'admin'
        assert db._password == 'secret'
        assert not db.is_connected

    def test_connect_disconnect_no_driver(self):
        """Test connection lifecycle without actual Neo4j."""
        db = Neo4jDB(uri='bolt://nonexistent', username='neo4j', password='test')
        # Should return False if connection fails
        result = db.connect()
        assert isinstance(result, bool)

        db.close()  # Should not raise
        assert not db.is_connected


class TestMongoDB:
    """Test MongoDB class."""

    def test_init_without_url_uses_env(self):
        """Test that MongoDB reads from environment."""
        with patch.dict('os.environ', {'MONGODB_URL': 'mongodb://test'}):
            db = MongoDB()
            assert db._url == 'mongodb://test'
            assert not db.is_connected

    def test_init_without_url_logs_info(self):
        """Test MongoDB handles missing URL gracefully."""
        with patch.dict('os.environ', {}, clear=True):
            import os
            os.environ.pop('MONGODB_URL', None)
            db = MongoDB()
            assert db._url is None
            assert not db.is_connected

    def test_archive_returns_none_when_not_connected(self):
        """Test that archive operations return None when unavailable."""
        db = MongoDB(url=None)

        result = db.archive_llm_request(
            job_id="test",
            agent_type="creator",
            messages=[],
            response={},
            model="gpt-4"
        )
        assert result is None

    def test_audit_returns_none_when_not_connected(self):
        """Test that audit operations return None when unavailable."""
        db = MongoDB(url=None)

        result = db.audit_tool_call(
            job_id="test",
            agent_type="creator",
            tool_name="test_tool",
            inputs={}
        )
        assert result is None

    def test_get_trail_returns_empty_when_not_connected(self):
        """Test that get operations return empty list when unavailable."""
        db = MongoDB(url=None)

        result = db.get_job_audit_trail("test")
        assert result == []


class TestDependencyInjection:
    """Test that instances can be created and injected."""

    def test_postgres_instance_creation(self):
        """Test PostgresDB instance creation."""
        with patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test'}):
            db = PostgresDB()
            assert isinstance(db, PostgresDB)

    def test_neo4j_instance_creation(self):
        """Test Neo4jDB instance creation."""
        db = Neo4jDB(uri='bolt://test', username='neo4j', password='test')
        assert isinstance(db, Neo4jDB)

    def test_mongo_instance_creation(self):
        """Test MongoDB instance creation."""
        db = MongoDB()
        assert isinstance(db, MongoDB)


class TestBackwardCompatibility:
    """Test that old API still works."""

    def test_old_imports_work(self):
        """Test that canonical classes can be imported."""
        from src.database import PostgresDB, Neo4jDB

        assert PostgresDB is not None
        assert Neo4jDB is not None

    def test_old_functions_work(self):
        """Test that database classes have core methods."""
        from src.database import PostgresDB, Neo4jDB

        # PostgresDB core methods
        assert hasattr(PostgresDB, 'connect')
        assert hasattr(PostgresDB, 'close')
        assert hasattr(PostgresDB, 'execute')

        # Neo4jDB core methods
        assert hasattr(Neo4jDB, 'connect')
        assert hasattr(Neo4jDB, 'close')
        assert hasattr(Neo4jDB, 'execute_query')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
