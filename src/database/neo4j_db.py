"""Neo4j Database Manager with session-based queries.

Provides a generic Neo4j interface using the official driver with:
- Session-based query execution (read and write)
- Named query loading from Cypher files
- Schema inspection
- Proper transaction handling

Connection details come from the datasource connector system
(see docs/datasources.md). No env var fallbacks.
"""

import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries" / "neo4j"


class Neo4jDB:
    """Neo4j database manager with session-based queries.

    Generic graph database client — no domain-specific namespaces.
    Used by graph tools (src/tools/graph/) via the datasource connector.

    Example:
        ```python
        db = Neo4jDB(
            uri="bolt://localhost:7687",
            username="neo4j",
            password="secret",
        )
        db.connect()

        results = db.execute_query("MATCH (n) RETURN n LIMIT 10")
        schema = db.get_schema()

        db.close()
        ```
    """

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
    ):
        """Initialize Neo4j database manager.

        Args:
            uri: Neo4j URI (e.g., bolt://localhost:7687)
            username: Neo4j username
            password: Neo4j password
        """
        self._uri = uri
        self._username = username
        self._password = password

        self.driver = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

        logger.info("Neo4jDB initialized (not connected yet)")

    def connect(self) -> bool:
        """Establish connection to Neo4j database.

        Creates the driver and verifies connectivity.
        This method is idempotent - safe to call multiple times.

        Returns:
            True if connection successful, False otherwise
        """
        if self.driver is not None:
            return True  # Already connected

        try:
            self.driver = GraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password)
            )
            # Verify connectivity
            self.driver.verify_connectivity()
            logger.info(f"Neo4j connected: {self._uri}")
            return True
        except AuthError as e:
            logger.error(f"Neo4j authentication failed: {e}")
            return False
        except ServiceUnavailable as e:
            logger.error(f"Neo4j service unavailable: {e}")
            return False
        except Exception as e:
            logger.error(f"Neo4j connection error: {e}")
            return False

    def close(self) -> None:
        """Close the database connection.

        This method is idempotent - safe to call multiple times.
        """
        if self.driver:
            self.driver.close()
            self.driver = None
            logger.info("Neo4j connection closed")

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results.

        Args:
            query: Cypher query string
            parameters: Optional query parameters

        Returns:
            List of result records as dictionaries

        Raises:
            RuntimeError: If not connected to database
        """
        if not self.driver:
            raise RuntimeError("Not connected to database. Call connect() first.")

        results = []
        try:
            with self.driver.session() as session:
                result = session.run(query, parameters or {})
                results = [dict(record) for record in result]
            return results
        except Exception as e:
            logger.error(f"Neo4j query error: {e}")
            logger.debug(f"Query: {query}")
            logger.debug(f"Parameters: {parameters}")
            raise

    def execute_write(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Execute a write transaction.

        Use this for queries that modify the graph (CREATE, MERGE, DELETE, SET).

        Args:
            query: Cypher query string
            parameters: Optional query parameters

        Returns:
            List of result records as dictionaries

        Raises:
            RuntimeError: If not connected to database
        """
        if not self.driver:
            raise RuntimeError("Not connected to database. Call connect() first.")

        def _execute_tx(tx, q, p):
            result = tx.run(q, p or {})
            return [dict(record) for record in result]

        try:
            with self.driver.session() as session:
                results = session.execute_write(_execute_tx, query, parameters)
            return results
        except Exception as e:
            logger.error(f"Neo4j write error: {e}")
            logger.debug(f"Query: {query}")
            logger.debug(f"Parameters: {parameters}")
            raise

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load a named Cypher query from a .cypher/.cql file.

        Queries are cached after first load. Query files use the format:

        ```cypher
        // name: query_name
        MATCH ...
        RETURN ...;

        // name: another_query
        MATCH ...
        ```

        Args:
            filename: Cypher file name (e.g., "finius.cypher")
            query_name: Name of the query to load

        Returns:
            Cypher query string

        Raises:
            ValueError: If query not found in file
        """
        cache_key = f"{filename}:{query_name}"
        if cache_key in self._queries:
            return self._queries[cache_key]

        file_path = QUERIES_DIR / filename
        if not file_path.exists():
            raise ValueError(f"Query file not found: {file_path}")

        content = file_path.read_text()

        # Parse named queries: // name: query_name
        pattern = r"//\s*name:\s*(\w+)\s*\n(.*?)(?=//\s*name:|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        for name, cypher in matches:
            self._queries[f"{filename}:{name}"] = cypher.strip()

        if cache_key not in self._queries:
            raise ValueError(f"Query '{query_name}' not found in {filename}")

        return self._queries[cache_key]

    def get_schema(self) -> Dict[str, Any]:
        """Retrieve the database schema.

        Returns:
            Dictionary containing node labels, relationship types, and property keys
        """
        schema = {
            'node_labels': [],
            'relationship_types': [],
            'property_keys': []
        }

        try:
            # Get node labels
            result = self.execute_query("CALL db.labels()")
            schema['node_labels'] = [record['label'] for record in result]

            # Get relationship types
            result = self.execute_query("CALL db.relationshipTypes()")
            schema['relationship_types'] = [record['relationshipType'] for record in result]

            # Get property keys
            result = self.execute_query("CALL db.propertyKeys()")
            schema['property_keys'] = [record['propertyKey'] for record in result]

        except Exception as e:
            logger.error(f"Error retrieving schema: {e}")

        return schema

    @property
    def is_connected(self) -> bool:
        """Check if connected to database."""
        return self.driver is not None


__all__ = ['Neo4jDB']
