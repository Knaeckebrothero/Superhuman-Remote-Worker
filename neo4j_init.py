"""
Neo4j Knowledge Graph initialization script for the Fessi backend.

This script initializes the Neo4j knowledge graph and optionally seeds it
with sample data for waste disposal. It supports two modes:

1. Default mode: Adds seed data if graph is empty
2. Force-reset mode: Clears all data and re-seeds

Usage:
    # Default: Seed if empty
    python -m backend.database.neo4j_init

    # Force reset: Clear all and re-seed
    python -m backend.database.neo4j_init --force-reset

    # Skip seeding (just verify connection)
    python -m backend.database.neo4j_init --no-seed
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError
from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Constants
QUERIES_DIR = Path(__file__).parent
SEED_FILE = QUERIES_DIR / "neo4j_seed.cypher"

# Expected node labels for verification
EXPECTED_LABELS = [
    "WasteCategory",
    "WasteItem",
    "DisposalMethod",
    "Location",
    "FAQ",
]


def setup_logging() -> logging.Logger:
    """
    Configure logging for Neo4j initialization.

    Returns:
        Logger instance configured for console output.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="Initialize the Fessi Neo4j knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m backend.database.neo4j_init              # Seed if empty
  python -m backend.database.neo4j_init --force-reset  # Clear and re-seed
  python -m backend.database.neo4j_init --no-seed    # Just verify connection
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete all nodes and relationships, then re-seed (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip seeding (just verify connection)",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j connection URI (default: from .env or bolt://localhost:7687)",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("NEO4J_USER", "neo4j"),
        help="Neo4j user (default: from .env or neo4j)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("NEO4J_PASSWORD", "fessi_neo4j_dev"),
        help="Neo4j password (default: from .env)",
    )
    return parser.parse_args()


def get_driver(uri: str, user: str, password: str, logger: logging.Logger):
    """
    Create a Neo4j driver and verify connectivity.

    Args:
        uri: Neo4j connection URI.
        user: Neo4j username.
        password: Neo4j password.
        logger: Logger instance.

    Returns:
        Neo4j Driver instance.

    Raises:
        ServiceUnavailable: If Neo4j is not reachable.
        AuthError: If authentication fails.
    """
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
        logger.info(f"  ✓ Connected to Neo4j at {uri}")
        return driver
    except ServiceUnavailable as e:
        logger.error(f"  ✗ Neo4j service unavailable at {uri}")
        logger.info("  Hint: Make sure Neo4j is running.")
        logger.info("  Start with: cd docker && docker-compose up -d neo4j")
        raise
    except AuthError as e:
        logger.error(f"  ✗ Neo4j authentication failed for user '{user}'")
        raise


def get_node_counts(driver, logger: logging.Logger) -> dict:
    """
    Get count of nodes by label.

    Args:
        driver: Neo4j Driver instance.
        logger: Logger instance.

    Returns:
        Dictionary mapping label names to counts.
    """
    with driver.session() as session:
        result = session.run("""
            MATCH (n)
            RETURN labels(n) as labels, count(n) as count
        """)
        counts = {}
        for record in result:
            for label in record["labels"]:
                counts[label] = counts.get(label, 0) + record["count"]
        return counts


def get_relationship_count(driver) -> int:
    """
    Get total count of relationships.

    Args:
        driver: Neo4j Driver instance.

    Returns:
        Total relationship count.
    """
    with driver.session() as session:
        result = session.run("MATCH ()-[r]->() RETURN count(r) as count")
        record = result.single()
        return record["count"] if record else 0


def clear_all_data(driver, logger: logging.Logger) -> None:
    """
    Delete all nodes and relationships in the database.

    Args:
        driver: Neo4j Driver instance.
        logger: Logger instance.
    """
    with driver.session() as session:
        # First delete relationships, then nodes
        # Using DETACH DELETE removes nodes and their relationships
        logger.info("  Deleting all nodes and relationships...")
        result = session.run("MATCH (n) DETACH DELETE n")
        summary = result.consume()
        logger.info(f"  ✓ Deleted {summary.counters.nodes_deleted} nodes")
        logger.info(f"  ✓ Deleted {summary.counters.relationships_deleted} relationships")


def execute_seed_cypher(driver, seed_path: Path, logger: logging.Logger) -> bool:
    """
    Execute the seed Cypher file to populate the knowledge graph.

    The seed file may contain multiple statements separated by semicolons.
    Each statement is executed in a separate transaction to handle
    Neo4j's transaction requirements.

    Args:
        driver: Neo4j Driver instance.
        seed_path: Path to the seed.cypher file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    if not seed_path.exists():
        logger.error(f"  ✗ Seed file not found: {seed_path}")
        return False

    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            seed_content = f.read()

        # Split by semicolons but be careful with strings
        # Simple split works for our seed file format
        statements = []
        current = []

        for line in seed_content.split('\n'):
            stripped = line.strip()
            # Skip empty lines and comments
            if not stripped or stripped.startswith('//'):
                continue
            current.append(line)
            # Check if this line ends a statement
            if stripped.endswith(';'):
                statement = '\n'.join(current).strip()
                if statement.endswith(';'):
                    statement = statement[:-1]  # Remove trailing semicolon
                if statement:
                    statements.append(statement)
                current = []

        # Handle any remaining statement without semicolon
        if current:
            statement = '\n'.join(current).strip()
            if statement:
                statements.append(statement)

        logger.info(f"  Executing {len(statements)} Cypher statements...")

        nodes_created = 0
        relationships_created = 0

        with driver.session() as session:
            for i, statement in enumerate(statements):
                try:
                    result = session.run(statement)
                    summary = result.consume()
                    nodes_created += summary.counters.nodes_created
                    relationships_created += summary.counters.relationships_created
                except Exception as e:
                    logger.warning(f"  ⚠ Statement {i+1} warning: {str(e)[:50]}")

        logger.info(f"  ✓ Created {nodes_created} nodes")
        logger.info(f"  ✓ Created {relationships_created} relationships")
        return True

    except Exception as e:
        logger.error(f"  ✗ Error executing seed: {e}")
        return False


def verify_graph(driver, logger: logging.Logger) -> bool:
    """
    Verify that all expected node types exist in the graph.

    Args:
        driver: Neo4j Driver instance.
        logger: Logger instance.

    Returns:
        True if all expected labels exist, False otherwise.
    """
    node_counts = get_node_counts(driver, logger)

    all_present = True
    for label in EXPECTED_LABELS:
        count = node_counts.get(label, 0)
        if count > 0:
            logger.info(f"    ✓ {label}: {count} nodes")
        else:
            logger.error(f"    ✗ {label}: missing")
            all_present = False

    # Also show relationship count
    rel_count = get_relationship_count(driver)
    logger.info(f"    ✓ Relationships: {rel_count}")

    return all_present


def initialize_neo4j(args: argparse.Namespace, logger: logging.Logger) -> bool:
    """
    Main Neo4j initialization logic.

    Performs the following steps:
    1. Connect to Neo4j
    2. If --force-reset: clear all data
    3. If graph is empty or --force-reset: seed data
    4. Verify graph structure

    Args:
        args: Parsed command line arguments.
        logger: Logger instance.

    Returns:
        True if initialization successful, False otherwise.
    """
    logger.info("")
    logger.info("=== Neo4j Knowledge Graph Initialization ===")
    logger.info("")

    # Step 1: Connect to Neo4j
    logger.info("[1/4] Connecting to Neo4j...")
    try:
        driver = get_driver(args.uri, args.user, args.password, logger)
    except Exception as e:
        return False

    try:
        # Step 2: Check current state / force reset
        logger.info("")
        node_counts = get_node_counts(driver, logger)
        total_nodes = sum(node_counts.values())

        if args.force_reset:
            logger.info("[2/4] Force reset - clearing all data...")
            logger.warning("  ⚠ WARNING: All existing data will be deleted!")
            clear_all_data(driver, logger)
            total_nodes = 0  # Reset count for seeding check
        else:
            logger.info(f"[2/4] Found {total_nodes} existing nodes (use --force-reset to clear)")

        # Step 3: Seed data if empty or after reset
        logger.info("")
        if args.no_seed:
            logger.info("[3/4] Skipping seed data (--no-seed flag)")
        elif total_nodes == 0 or args.force_reset:
            logger.info("[3/4] Seeding knowledge graph...")
            if not execute_seed_cypher(driver, SEED_FILE, logger):
                logger.warning("  ⚠ Seed data insertion had issues, but continuing...")
        else:
            logger.info(f"[3/4] Graph already contains data, skipping seed (use --force-reset to re-seed)")

        # Step 4: Verify graph
        logger.info("")
        logger.info("[4/4] Verifying knowledge graph...")
        if not verify_graph(driver, logger):
            logger.warning("")
            logger.warning("  ⚠ Some expected node types are missing!")

        logger.info("")
        logger.info("=== Neo4j Initialization Complete ===")
        logger.info("")

        # Show summary
        final_counts = get_node_counts(driver, logger)
        logger.info("Knowledge graph summary:")
        for label, count in sorted(final_counts.items()):
            logger.info(f"  - {label}: {count}")
        logger.info("")

        return True

    finally:
        driver.close()


def main() -> int:
    """
    Main entry point for Neo4j initialization.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    logger = setup_logging()
    args = parse_args()

    try:
        success = initialize_neo4j(args, logger)
        return 0 if success else 1
    except KeyboardInterrupt:
        logger.info("")
        logger.info("Initialization cancelled by user.")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())