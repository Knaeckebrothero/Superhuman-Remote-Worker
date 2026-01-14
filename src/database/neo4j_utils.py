"""
Neo4j Database Utilities
Handles connection and query execution for Neo4j database.
"""

import os
from typing import List, Dict, Any, Optional
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError


class Neo4jConnection:
    """
    Neo4j database connection handler.
    Manages connection lifecycle and provides methods for executing queries.
    """

    def __init__(self, uri: str, username: str, password: str):
        """
        Initialize Neo4j connection.

        Args:
            uri: Neo4j database URI (e.g., bolt://localhost:7687)
            username: Database username
            password: Database password
        """
        self.uri = uri
        self.username = username
        self.password = password
        self.driver = None

    def connect(self) -> bool:
        """
        Establish connection to Neo4j database.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password)
            )
            # Verify connectivity
            self.driver.verify_connectivity()
            print(f"✓ Successfully connected to Neo4j at {self.uri}")
            return True
        except AuthError:
            print(f"✗ Authentication failed for Neo4j database")
            return False
        except ServiceUnavailable:
            print(f"✗ Neo4j service unavailable at {self.uri}")
            return False
        except Exception as e:
            print(f"✗ Error connecting to Neo4j: {str(e)}")
            return False

    def close(self):
        """Close the database connection."""
        if self.driver:
            self.driver.close()
            print("✓ Neo4j connection closed")

    def execute_query(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Execute a Cypher query and return results.

        Args:
            query: Cypher query string
            parameters: Optional query parameters

        Returns:
            List of result records as dictionaries
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
            print(f"✗ Error executing query: {str(e)}")
            print(f"Query: {query}")
            raise

    def get_database_schema(self) -> Dict[str, Any]:
        """
        Retrieve the database schema including node labels and relationship types.

        Returns:
            Dictionary containing schema information
        """
        schema = {
            'node_labels': [],
            'relationship_types': [],
            'property_keys': []
        }

        try:
            # Get node labels
            labels_query = "CALL db.labels()"
            labels_result = self.execute_query(labels_query)
            schema['node_labels'] = [record['label'] for record in labels_result]

            # Get relationship types
            rel_types_query = "CALL db.relationshipTypes()"
            rel_result = self.execute_query(rel_types_query)
            schema['relationship_types'] = [record['relationshipType'] for record in rel_result]

            # Get property keys
            prop_keys_query = "CALL db.propertyKeys()"
            prop_result = self.execute_query(prop_keys_query)
            schema['property_keys'] = [record['propertyKey'] for record in prop_result]

            return schema
        except Exception as e:
            print(f"✗ Error retrieving schema: {str(e)}")
            return schema

    def get_sample_data(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Retrieve sample data from the database.

        Args:
            limit: Maximum number of nodes to retrieve

        Returns:
            List of sample nodes with their properties
        """
        query = f"""
        MATCH (n)
        RETURN labels(n) as labels, properties(n) as properties
        LIMIT {limit}
        """
        return self.execute_query(query)


def create_neo4j_connection() -> Neo4jConnection:
    """
    Create Neo4j connection from environment variables.

    Returns:
        Configured Neo4jConnection instance

    Raises:
        ValueError: If required environment variables are missing
    """
    uri = os.getenv('NEO4J_URI')
    username = os.getenv('NEO4J_USERNAME')
    password = os.getenv('NEO4J_PASSWORD')

    if not all([uri, username, password]):
        raise ValueError(
            "Missing required Neo4j environment variables. "
            "Please set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD"
        )

    return Neo4jConnection(uri, username, password)
