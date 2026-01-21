#!/usr/bin/env python3
"""Initialize or reset the Neo4j development database.

This script:
1. Optionally clears all existing data (--clear)
2. Applies the metamodel schema (constraints and indexes)
3. Loads seed data from a Cypher export file

Usage:
    # Apply schema only (safe for existing data)
    python scripts/init_neo4j.py

    # Clear database and apply fresh schema
    python scripts/init_neo4j.py --clear

    # Clear and load seed data
    python scripts/init_neo4j.py --clear --seed

    # Load specific seed file
    python scripts/init_neo4j.py --clear --seed-file data/seed_data.cypher

    # Export current data for later seeding
    python scripts/init_neo4j.py --export --export-file data/seed_data.cypher
"""
import logging
import argparse
import logging
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

try:
    from neo4j import GraphDatabase
except ImportError:
    print("Error: neo4j driver is required. Install with: pip install neo4j")
    sys.exit(1)


def get_connection_params():
    """Get Neo4j connection parameters from environment."""
    return {
        "uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.getenv("NEO4J_USERNAME", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", "neo4j_password"),
    }


def clear_database(driver) -> int:
    """Delete all nodes and relationships.

    Returns:
        Number of nodes deleted
    """
    with driver.session() as session:
        # Get count first
        result = session.run("MATCH (n) RETURN count(n) as count")
        count = result.single()["count"]

        if count > 0:
            # Delete in batches to avoid memory issues
            deleted = 0
            while True:
                result = session.run("""
                    MATCH (n)
                    WITH n LIMIT 10000
                    DETACH DELETE n
                    RETURN count(*) as deleted
                """)
                batch_deleted = result.single()["deleted"]
                deleted += batch_deleted
                if batch_deleted == 0:
                    break
                print(f"  Deleted {deleted} nodes...")

            return deleted
        return 0


def apply_schema(driver, schema_file: Path) -> None:
    """Apply metamodel schema (constraints and indexes).

    Args:
        driver: Neo4j driver
        schema_file: Path to metamodell.cql file
    """
    if not schema_file.exists():
        print(f"Error: Schema file not found: {schema_file}")
        sys.exit(1)

    with open(schema_file) as f:
        content = f.read()

    # Extract executable statements (CREATE CONSTRAINT, CREATE INDEX)
    # Handle multi-line statements by collecting until semicolon
    statements = []
    current_stmt = []
    in_statement = False

    for line in content.split('\n'):
        stripped = line.strip()

        # Skip comments and empty lines when not in a statement
        if not in_statement:
            if not stripped or stripped.startswith('//'):
                continue
            if stripped.startswith('CREATE CONSTRAINT') or stripped.startswith('CREATE INDEX'):
                in_statement = True
                current_stmt = [stripped]
        else:
            current_stmt.append(stripped)

        # Check if statement is complete (ends with semicolon)
        if in_statement and stripped.endswith(';'):
            full_stmt = ' '.join(current_stmt).rstrip(';')
            statements.append(full_stmt)
            current_stmt = []
            in_statement = False

    with driver.session() as session:
        for stmt in statements:
            try:
                session.run(stmt)
                print(f"  ✓ {stmt[:60]}...")
            except Exception as e:
                if "already exists" in str(e).lower() or "equivalent" in str(e).lower():
                    print(f"  ○ Already exists: {stmt[:50]}...")
                else:
                    print(f"  ✗ Failed: {stmt[:50]}... - {e}")


def load_seed_data(driver, seed_file: Path) -> int:
    """Load seed data from Cypher file.

    Args:
        driver: Neo4j driver
        seed_file: Path to seed data Cypher file

    Returns:
        Number of statements executed
    """
    if not seed_file.exists():
        print(f"Error: Seed file not found: {seed_file}")
        return 0

    with open(seed_file) as f:
        content = f.read()

    # Split by semicolons, but be careful with strings
    statements = []
    current = []
    in_string = False
    escape_next = False

    for char in content:
        if escape_next:
            current.append(char)
            escape_next = False
            continue
        if char == '\\':
            current.append(char)
            escape_next = True
            continue
        if char in ('"', "'"):
            in_string = not in_string
        if char == ';' and not in_string:
            stmt = ''.join(current).strip()
            if stmt and not stmt.startswith('//'):
                statements.append(stmt)
            current = []
        else:
            current.append(char)

    # Add last statement if no trailing semicolon
    stmt = ''.join(current).strip()
    if stmt and not stmt.startswith('//'):
        statements.append(stmt)

    executed = 0
    with driver.session() as session:
        for stmt in statements:
            # Skip comments and empty lines
            lines = [l.strip() for l in stmt.split('\n') if l.strip() and not l.strip().startswith('//')]
            stmt_clean = '\n'.join(lines)
            if not stmt_clean:
                continue

            try:
                session.run(stmt_clean)
                executed += 1
                if executed % 100 == 0:
                    print(f"  Executed {executed} statements...")
            except Exception as e:
                print(f"  Warning: {str(e)[:80]}")

    return executed


def export_data(driver, export_file: Path, use_merge: bool = False) -> int:
    """Export all nodes and relationships to Cypher file.

    Args:
        driver: Neo4j driver
        export_file: Path to write export
        use_merge: If True, use MERGE instead of CREATE (allows combining multiple exports)

    Returns:
        Number of nodes exported
    """
    with driver.session() as session:
        # Get all nodes
        nodes_result = session.run("""
            MATCH (n)
            RETURN labels(n) as labels, properties(n) as props, elementId(n) as id
        """)
        nodes = list(nodes_result)

        # Get all relationships
        rels_result = session.run("""
            MATCH (a)-[r]->(b)
            RETURN
                labels(a)[0] as from_label,
                properties(a) as from_props,
                type(r) as rel_type,
                properties(r) as rel_props,
                labels(b)[0] as to_label,
                properties(b) as to_props
        """)
        rels = list(rels_result)

    node_cmd = "MERGE" if use_merge else "CREATE"
    rel_cmd = "MERGE" if use_merge else "CREATE"

    with open(export_file, 'w') as f:
        f.write("// Neo4j Seed Data Export\n")
        f.write(f"// Exported: {nodes.__len__()} nodes, {rels.__len__()} relationships\n")
        f.write(f"// Mode: {'MERGE (idempotent)' if use_merge else 'CREATE'}\n")
        f.write("// Generated by src/scripts/init_neo4j.py --export\n\n")

        # Write nodes
        f.write("// === NODES ===\n\n")
        for node in nodes:
            labels = ':'.join(node['labels'])
            props = node['props']

            if use_merge:
                # For MERGE, match on unique key then SET remaining properties
                unique_key, unique_val = get_unique_key(labels.split(':')[0], props)
                other_props = {k: v for k, v in props.items() if k != unique_key}
                f.write(f"{node_cmd} (n:{labels} {{{unique_key}: {format_value(unique_val)}}})\n")
                if other_props:
                    set_items = ', '.join(f"n.{k} = {format_value(v)}" for k, v in other_props.items())
                    f.write(f"SET {set_items};\n")
                else:
                    f.write(";\n")
            else:
                props_str = format_props(props)
                f.write(f"{node_cmd} (:{labels} {props_str});\n")

        f.write("\n// === RELATIONSHIPS ===\n\n")
        for rel in rels:
            from_label = rel['from_label']
            from_props = rel['from_props']
            to_label = rel['to_label']
            to_props = rel['to_props']
            rel_type = rel['rel_type']
            rel_props = rel['rel_props']

            # Find unique identifier for matching
            from_key, from_val = get_unique_key(from_label, from_props)
            to_key, to_val = get_unique_key(to_label, to_props)

            rel_props_str = format_props(rel_props) if rel_props else ""

            f.write(f"MATCH (a:{from_label} {{{from_key}: {format_value(from_val)}}}), ")
            f.write(f"(b:{to_label} {{{to_key}: {format_value(to_val)}}})\n")
            f.write(f"{rel_cmd} (a)-[:{rel_type}")
            if rel_props_str:
                f.write(f" {rel_props_str}")
            f.write("]->(b);\n\n")

    return len(nodes)


def get_unique_key(label: str, props: dict) -> tuple:
    """Get unique identifier key/value for a node."""
    # Priority: rid/boid/mid > name > first prop
    if label == 'Requirement' and 'rid' in props:
        return 'rid', props['rid']
    if label == 'BusinessObject' and 'boid' in props:
        return 'boid', props['boid']
    if label == 'Message' and 'mid' in props:
        return 'mid', props['mid']
    if 'name' in props:
        return 'name', props['name']
    # Fallback to first property
    for k, v in props.items():
        return k, v
    return 'id', 'unknown'


def format_value(val) -> str:
    """Format a value for Cypher."""
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        items = ', '.join(format_value(v) for v in val)
        return f'[{items}]'
    # String - escape quotes
    val_str = str(val).replace('\\', '\\\\').replace("'", "\\'")
    return f"'{val_str}'"


def format_props(props: dict) -> str:
    """Format properties dict for Cypher."""
    if not props:
        return "{}"
    items = []
    for k, v in props.items():
        items.append(f"{k}: {format_value(v)}")
    return "{" + ", ".join(items) + "}"


def get_stats(driver) -> dict:
    """Get database statistics."""
    with driver.session() as session:
        result = session.run("""
            MATCH (n)
            WITH labels(n)[0] as label, count(*) as count
            RETURN label, count
            ORDER BY count DESC
        """)
        nodes = {r['label']: r['count'] for r in result}

        result = session.run("""
            MATCH ()-[r]->()
            WITH type(r) as type, count(*) as count
            RETURN type, count
            ORDER BY count DESC
        """)
        rels = {r['type']: r['count'] for r in result}

        return {"nodes": nodes, "relationships": rels}


def main():
    parser = argparse.ArgumentParser(
        description="Initialize or reset Neo4j development database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Apply schema only (safe)
  %(prog)s

  # Reset database completely
  %(prog)s --clear --seed

  # Export current data (CREATE mode)
  %(prog)s --export

  # Export with MERGE (idempotent, for combining datasets)
  %(prog)s --export --merge --export-file data/export_merge.cypher

  # Load additional cypher file(s) after seed
  %(prog)s --clear --seed --load-file data/extra_data.cypher

  # Combine two exports: load original, then merge mockup
  %(prog)s --clear --load-file data/original.cypher --load-file data/mockup_merge.cypher
        """
    )

    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all existing data before applying schema"
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Load seed data after applying schema"
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=project_root / "data" / "seed_data.cypher",
        help="Path to seed data file (default: data/seed_data.cypher)"
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export current database to Cypher file"
    )
    parser.add_argument(
        "--export-file",
        type=Path,
        default=project_root / "data" / "seed_data.cypher",
        help="Path to export file (default: data/seed_data.cypher)"
    )
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=project_root / "data" / "metamodell.cql",
        help="Path to schema file (default: data/metamodell.cql)"
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Use MERGE instead of CREATE in export (idempotent, allows combining datasets)"
    )
    parser.add_argument(
        "--load-file",
        type=Path,
        action="append",
        dest="load_files",
        help="Additional Cypher file(s) to load (can be specified multiple times)"
    )

    args = parser.parse_args()

    # Get connection params
    params = get_connection_params()
    print("=" * 60)
    print("Neo4j Database Initialization")
    print("=" * 60)
    print(f"URI: {params['uri']}")
    print(f"User: {params['user']}")
    print()

    # Connect
    try:
        driver = GraphDatabase.driver(
            params['uri'],
            auth=(params['user'], params['password'])
        )
        driver.verify_connectivity()
        print("Connected to Neo4j")
    except Exception as e:
        print(f"Error connecting to Neo4j: {e}")
        sys.exit(1)

    try:
        # Export mode
        if args.export:
            print()
            mode_str = "MERGE (idempotent)" if args.merge else "CREATE"
            print(f"Exporting data in {mode_str} mode...")
            count = export_data(driver, args.export_file, use_merge=args.merge)
            print(f"Exported {count} nodes to {args.export_file}")
            print()
            print("=" * 60)
            print("Export complete!")
            print("=" * 60)
            return

        # Show current stats
        print()
        print("Current database state:")
        stats = get_stats(driver)
        if stats['nodes']:
            for label, count in stats['nodes'].items():
                print(f"  {label}: {count} nodes")
        else:
            print("  (empty)")
        print()

        # Clear if requested
        if args.clear:
            print("Step 1: Clearing database...")
            deleted = clear_database(driver)
            print(f"Deleted {deleted} nodes")
        else:
            print("Step 1: Skipping clear (use --clear to reset)")
        print()

        # Apply schema
        print("Step 2: Applying schema...")
        apply_schema(driver, args.schema_file)
        print()

        # Load seed data if requested
        if args.seed:
            print("Step 3: Loading seed data...")
            if args.seed_file.exists():
                count = load_seed_data(driver, args.seed_file)
                print(f"Executed {count} statements")
            else:
                print(f"Seed file not found: {args.seed_file}")
                print("Run with --export first to create seed data from current database")
        else:
            print("Step 3: Skipping seed data (use --seed to load)")
        print()

        # Load additional files if specified
        if args.load_files:
            print("Step 4: Loading additional files...")
            for i, load_file in enumerate(args.load_files, 1):
                if load_file.exists():
                    print(f"  [{i}/{len(args.load_files)}] Loading {load_file.name}...")
                    count = load_seed_data(driver, load_file)
                    print(f"    Executed {count} statements")
                else:
                    print(f"  [{i}/{len(args.load_files)}] File not found: {load_file}")
            print()

        # Show final stats
        print("Final database state:")
        stats = get_stats(driver)
        if stats['nodes']:
            for label, count in stats['nodes'].items():
                print(f"  {label}: {count} nodes")
            print()
            for rel_type, count in stats['relationships'].items():
                print(f"  {rel_type}: {count} relationships")
        else:
            print("  (empty - use --seed to load data)")

        print()
        print("=" * 60)
        print("Neo4j initialization complete!")
        print("=" * 60)

    finally:
        driver.close()


def initialize_neo4j(logger: logging.Logger, clear: bool = False, seed: bool = False) -> bool:
    """
    Initialize Neo4j database (called by app_init.py).

    Args:
        logger: Logger instance.
        clear: If True, clear all data before initializing.
        seed: If True, load seed data.

    Returns:
        True if successful, False otherwise.
    """
    params = get_connection_params()

    try:
        driver = GraphDatabase.driver(
            params['uri'],
            auth=(params['user'], params['password'])
        )
        driver.verify_connectivity()
        logger.info(f"  Connected to Neo4j at {params['uri']}")
    except Exception as e:
        logger.warning(f"  Could not connect to Neo4j: {e}")
        return True  # Don't fail if Neo4j unavailable

    try:
        schema_file = project_root / "data" / "metamodell.cql"
        seed_file = project_root / "data" / "seed_data.cypher"

        # Show current state
        stats = get_stats(driver)
        total_nodes = sum(stats['nodes'].values()) if stats['nodes'] else 0
        logger.info(f"  Current state: {total_nodes} nodes")

        # Clear if requested
        if clear:
            logger.info("  Clearing database...")
            deleted = clear_database(driver)
            logger.info(f"  Deleted {deleted} nodes")

        # Apply schema
        logger.info("  Applying schema...")
        apply_schema(driver, schema_file)

        # Load seed data if requested
        if seed and seed_file.exists():
            logger.info("  Loading seed data...")
            count = load_seed_data(driver, seed_file)
            logger.info(f"  Loaded {count} statements")
        elif seed:
            logger.info(f"  Seed file not found: {seed_file}")
            logger.info("  Run with --export-neo4j first to create seed data")

        # Show final state
        stats = get_stats(driver)
        total_nodes = sum(stats['nodes'].values()) if stats['nodes'] else 0
        logger.info(f"  Final state: {total_nodes} nodes")

        return True

    except Exception as e:
        logger.warning(f"  Neo4j initialization error: {e}")
        return True  # Don't fail the whole init

    finally:
        driver.close()


def export_neo4j_data(logger: logging.Logger) -> bool:
    """
    Export Neo4j data to seed file (called by app_init.py).

    Args:
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    params = get_connection_params()
    export_file = project_root / "data" / "seed_data.cypher"

    try:
        driver = GraphDatabase.driver(
            params['uri'],
            auth=(params['user'], params['password'])
        )
        driver.verify_connectivity()
        logger.info(f"Connected to Neo4j at {params['uri']}")
    except Exception as e:
        logger.error(f"Could not connect to Neo4j: {e}")
        return False

    try:
        count = export_data(driver, export_file)
        logger.info(f"Exported {count} nodes to {export_file}")
        return True
    except Exception as e:
        logger.error(f"Export failed: {e}")
        return False
    finally:
        driver.close()


def backup_neo4j(backup_file: Path, logger: logging.Logger) -> bool:
    """
    Backup Neo4j database to Cypher file.

    Args:
        backup_file: Path to write the backup file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    params = get_connection_params()

    try:
        driver = GraphDatabase.driver(
            params['uri'],
            auth=(params['user'], params['password'])
        )
        driver.verify_connectivity()
        logger.info(f"  Connected to Neo4j at {params['uri']}")
    except Exception as e:
        logger.warning(f"  Could not connect to Neo4j: {e}")
        return False

    try:
        count = export_data(driver, backup_file)
        logger.info(f"  Exported {count} nodes to {backup_file}")
        return True
    except Exception as e:
        logger.error(f"  Neo4j backup failed: {e}")
        return False
    finally:
        driver.close()


def restore_neo4j(backup_file: Path, logger: logging.Logger) -> bool:
    """
    Restore Neo4j database from Cypher backup file.

    Args:
        backup_file: Path to the backup file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    if not backup_file.exists():
        logger.error(f"  Backup file not found: {backup_file}")
        return False

    params = get_connection_params()

    try:
        driver = GraphDatabase.driver(
            params['uri'],
            auth=(params['user'], params['password'])
        )
        driver.verify_connectivity()
        logger.info(f"  Connected to Neo4j at {params['uri']}")
    except Exception as e:
        logger.warning(f"  Could not connect to Neo4j: {e}")
        return False

    try:
        # Clear database first
        logger.info("  Clearing database...")
        deleted = clear_database(driver)
        logger.info(f"  Deleted {deleted} nodes")

        # Apply schema
        schema_file = project_root / "data" / "metamodell.cql"
        if schema_file.exists():
            logger.info("  Applying schema...")
            apply_schema(driver, schema_file)

        # Load backup data
        logger.info("  Loading backup data...")
        count = load_seed_data(driver, backup_file)
        logger.info(f"  Restored {count} statements")

        return True
    except Exception as e:
        logger.error(f"  Neo4j restore failed: {e}")
        return False
    finally:
        driver.close()


if __name__ == "__main__":
    main()
