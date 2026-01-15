"""Neo4j Database Manager with session-based queries.

This module provides a modern Neo4j interface using the official driver with:
- Session-based query execution
- Namespace-based operations (requirements, entities, relationships, statistics)
- Named query loading from Cypher files
- Proper transaction handling

Part of Phase 1 database refactoring - see docs/db_refactor.md
"""

import os
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

    Provides namespace-based operations for:
    - requirements: Requirement node operations
    - entities: BusinessObject and Message operations
    - relationships: Relationship creation and queries
    - statistics: Graph statistics and analytics

    Example:
        ```python
        db = Neo4jDB()
        db.connect()

        # Create a requirement node
        node_id = db.requirements.create(
            rid="R001",
            text="System must...",
            category="functional"
        )

        # Query relationships
        fulfilled_by = db.relationships.get_fulfillment(rid="R001")

        db.close()
        ```
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        """Initialize Neo4j database manager.

        Args:
            uri: Neo4j URI (e.g., bolt://localhost:7687). Falls back to NEO4J_URI env var.
            username: Neo4j username. Falls back to NEO4J_USERNAME env var.
            password: Neo4j password. Falls back to NEO4J_PASSWORD env var.
        """
        self._uri = uri or os.getenv('NEO4J_URI', 'bolt://localhost:7687')
        self._username = username or os.getenv('NEO4J_USERNAME', 'neo4j')
        self._password = password or os.getenv('NEO4J_PASSWORD', 'neo4j_password')

        self.driver = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

        # Initialize namespaces
        self.requirements = RequirementsNamespace(self)
        self.entities = EntitiesNamespace(self)
        self.relationships = RelationshipsNamespace(self)
        self.statistics = StatisticsNamespace(self)

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


class RequirementsNamespace:
    """Namespace for requirement node operations."""

    def __init__(self, db: Neo4jDB):
        self.db = db

    def create(
        self,
        rid: str,
        text: str,
        category: Optional[str] = None,
        **properties
    ) -> str:
        """Create a Requirement node.

        Args:
            rid: Requirement ID (unique)
            text: Requirement text
            category: Requirement category (optional)
            **properties: Additional node properties

        Returns:
            Neo4j element ID of created node
        """
        props = {
            'rid': rid,
            'text': text,
            'category': category,
            **properties
        }

        # Remove None values
        props = {k: v for k, v in props.items() if v is not None}

        result = self.db.execute_write(
            """
            CREATE (r:Requirement)
            SET r = $props
            RETURN elementId(r) as element_id
            """,
            {'props': props}
        )

        element_id = result[0]['element_id'] if result else None
        logger.debug(f"Created Requirement node: {rid} (element_id={element_id})")
        return element_id

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        """Get a Requirement node by ID.

        Args:
            rid: Requirement ID

        Returns:
            Node properties as dictionary, or None if not found
        """
        result = self.db.execute_query(
            """
            MATCH (r:Requirement {rid: $rid})
            RETURN r, elementId(r) as element_id
            """,
            {'rid': rid}
        )

        if not result:
            return None

        node = dict(result[0]['r'])
        node['element_id'] = result[0]['element_id']
        return node

    def find_similar(
        self,
        text: str,
        limit: int = 5,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Find similar requirements by text.

        Args:
            text: Text to search for
            limit: Maximum number of results
            category: Filter by category (optional)

        Returns:
            List of matching requirement nodes
        """
        if category:
            result = self.db.execute_query(
                """
                MATCH (r:Requirement)
                WHERE r.category = $category
                AND r.text CONTAINS $text
                RETURN r, elementId(r) as element_id
                LIMIT $limit
                """,
                {'text': text, 'category': category, 'limit': limit}
            )
        else:
            result = self.db.execute_query(
                """
                MATCH (r:Requirement)
                WHERE r.text CONTAINS $text
                RETURN r, elementId(r) as element_id
                LIMIT $limit
                """,
                {'text': text, 'limit': limit}
            )

        return [
            {**dict(record['r']), 'element_id': record['element_id']}
            for record in result
        ]


class EntitiesNamespace:
    """Namespace for BusinessObject and Message operations."""

    def __init__(self, db: Neo4jDB):
        self.db = db

    def create_business_object(
        self,
        name: str,
        **properties
    ) -> str:
        """Create a BusinessObject node.

        Args:
            name: Business object name
            **properties: Additional node properties

        Returns:
            Neo4j element ID of created node
        """
        props = {'name': name, **properties}
        props = {k: v for k, v in props.items() if v is not None}

        result = self.db.execute_write(
            """
            CREATE (bo:BusinessObject)
            SET bo = $props
            RETURN elementId(bo) as element_id
            """,
            {'props': props}
        )

        element_id = result[0]['element_id'] if result else None
        logger.debug(f"Created BusinessObject node: {name} (element_id={element_id})")
        return element_id

    def create_message(
        self,
        name: str,
        **properties
    ) -> str:
        """Create a Message node.

        Args:
            name: Message name
            **properties: Additional node properties

        Returns:
            Neo4j element ID of created node
        """
        props = {'name': name, **properties}
        props = {k: v for k, v in props.items() if v is not None}

        result = self.db.execute_write(
            """
            CREATE (m:Message)
            SET m = $props
            RETURN elementId(m) as element_id
            """,
            {'props': props}
        )

        element_id = result[0]['element_id'] if result else None
        logger.debug(f"Created Message node: {name} (element_id={element_id})")
        return element_id

    def get_business_object(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a BusinessObject node by name.

        Args:
            name: Business object name

        Returns:
            Node properties as dictionary, or None if not found
        """
        result = self.db.execute_query(
            """
            MATCH (bo:BusinessObject {name: $name})
            RETURN bo, elementId(bo) as element_id
            """,
            {'name': name}
        )

        if not result:
            return None

        node = dict(result[0]['bo'])
        node['element_id'] = result[0]['element_id']
        return node


class RelationshipsNamespace:
    """Namespace for relationship operations."""

    def __init__(self, db: Neo4jDB):
        self.db = db

    def create_fulfillment(
        self,
        req_rid: str,
        target_name: str,
        target_type: str,  # "BusinessObject" or "Message"
        fulfilled: bool = True,
        confidence: Optional[float] = None,
        **properties
    ) -> None:
        """Create a fulfillment relationship.

        Args:
            req_rid: Requirement ID
            target_name: Target entity name
            target_type: Target node label ("BusinessObject" or "Message")
            fulfilled: True for FULFILLED_BY, False for NOT_FULFILLED_BY
            confidence: Confidence score (optional)
            **properties: Additional relationship properties
        """
        rel_type = "FULFILLED_BY_OBJECT" if fulfilled else "NOT_FULFILLED_BY_OBJECT"
        if target_type == "Message":
            rel_type = "FULFILLED_BY_MESSAGE" if fulfilled else "NOT_FULFILLED_BY_MESSAGE"

        props = {k: v for k, v in properties.items() if v is not None}
        if confidence is not None:
            props['confidence'] = confidence

        # Dynamic relationship type requires string formatting
        query = f"""
        MATCH (r:Requirement {{rid: $req_rid}})
        MATCH (t:{target_type} {{name: $target_name}})
        MERGE (r)-[rel:{rel_type}]->(t)
        SET rel += $props
        RETURN elementId(rel) as element_id
        """

        result = self.db.execute_write(
            query,
            {'req_rid': req_rid, 'target_name': target_name, 'props': props}
        )

        logger.debug(f"Created {rel_type}: {req_rid} -> {target_name}")

    def get_fulfillment(self, req_rid: str) -> Dict[str, List[Dict[str, Any]]]:
        """Get fulfillment relationships for a requirement.

        Args:
            req_rid: Requirement ID

        Returns:
            Dictionary with 'fulfilled' and 'not_fulfilled' lists
        """
        result = self.db.execute_query(
            """
            MATCH (r:Requirement {rid: $rid})
            OPTIONAL MATCH (r)-[fulfilled:FULFILLED_BY_OBJECT|FULFILLED_BY_MESSAGE]->(f)
            OPTIONAL MATCH (r)-[not_fulfilled:NOT_FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_MESSAGE]->(nf)
            RETURN
                collect(DISTINCT {
                    type: type(fulfilled),
                    target: f.name,
                    confidence: fulfilled.confidence
                }) as fulfilled_by,
                collect(DISTINCT {
                    type: type(not_fulfilled),
                    target: nf.name,
                    confidence: not_fulfilled.confidence
                }) as not_fulfilled_by
            """,
            {'rid': req_rid}
        )

        if not result:
            return {'fulfilled': [], 'not_fulfilled': []}

        return {
            'fulfilled': [r for r in result[0]['fulfilled_by'] if r['target'] is not None],
            'not_fulfilled': [r for r in result[0]['not_fulfilled_by'] if r['target'] is not None]
        }


class StatisticsNamespace:
    """Namespace for graph statistics operations."""

    def __init__(self, db: Neo4jDB):
        self.db = db

    def get_counts(self) -> Dict[str, int]:
        """Get node and relationship counts.

        Returns:
            Dictionary with counts by label/type
        """
        counts = {}

        try:
            # Node counts
            for label in ['Requirement', 'BusinessObject', 'Message']:
                result = self.db.execute_query(
                    f"MATCH (n:{label}) RETURN count(n) as count"
                )
                counts[f'{label}_count'] = result[0]['count'] if result else 0

            # Relationship counts
            result = self.db.execute_query(
                """
                MATCH ()-[r]->()
                RETURN type(r) as rel_type, count(r) as count
                """
            )
            for record in result:
                counts[f"{record['rel_type']}_count"] = record['count']

        except Exception as e:
            logger.error(f"Error getting statistics: {e}")

        return counts


__all__ = ['Neo4jDB']
