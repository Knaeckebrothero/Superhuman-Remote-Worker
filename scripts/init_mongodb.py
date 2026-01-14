#!/usr/bin/env python3
"""Initialize or reset the MongoDB database for LLM request archiving.

This script:
1. Creates the database and collections if they don't exist
2. Sets up indexes for efficient querying
3. Optionally clears existing data (--clear)

Usage:
    # Initialize MongoDB (create collections and indexes)
    python scripts/init_mongodb.py

    # Clear all data and reinitialize
    python scripts/init_mongodb.py --clear

    # Check connection only
    python scripts/init_mongodb.py --check

Environment Variables (from .env):
    MONGODB_URL - MongoDB connection string (optional)
                  Default: mongodb://localhost:27017/graphrag_logs
"""
import logging
import argparse
import logging
import os
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("init_mongodb")


def get_connection_params() -> dict:
    """Get MongoDB connection parameters from environment."""
    url = os.getenv("MONGODB_URL", "mongodb://localhost:27017/graphrag_logs")
    return {"url": url}


def get_client():
    """Create MongoDB client."""
    try:
        from pymongo import MongoClient
    except ImportError:
        print("Error: pymongo is required. Install with: pip install pymongo")
        sys.exit(1)

    params = get_connection_params()
    return MongoClient(params['url'], serverSelectionTimeoutMS=5000)


def check_connection(logger: logging.Logger) -> bool:
    """Check if MongoDB is accessible."""
    try:
        client = get_client()
        client.server_info()
        logger.info("MongoDB connection successful")
        client.close()
        return True
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return False


def clear_database(client, db_name: str, logger: logging.Logger) -> dict:
    """Clear all collections in the database.

    Returns:
        Dict with counts of deleted documents per collection.
    """
    db = client[db_name]
    deleted = {}

    for collection_name in db.list_collection_names():
        result = db[collection_name].delete_many({})
        deleted[collection_name] = result.deleted_count
        logger.info(f"  Deleted {result.deleted_count} documents from {collection_name}")

    return deleted


def create_collections_and_indexes(client, db_name: str, logger: logging.Logger) -> None:
    """Create collections and indexes for LLM request archiving and agent audit."""
    db = client[db_name]

    # =========================================================================
    # Collection: llm_requests - stores all LLM API requests and responses
    # =========================================================================
    llm_requests = db["llm_requests"]

    llm_indexes = [
        # Query by job
        ("job_id", {"name": "idx_job_id"}),
        # Query by agent
        ("agent_type", {"name": "idx_agent_type"}),
        # Query by timestamp
        ("timestamp", {"name": "idx_timestamp"}),
        # Query by model
        ("model", {"name": "idx_model"}),
        # Compound index for common queries
        ([("job_id", 1), ("agent_type", 1), ("timestamp", -1)],
         {"name": "idx_job_agent_time"}),
    ]

    logger.info("  Configuring llm_requests collection...")
    for index_spec, options in llm_indexes:
        try:
            if isinstance(index_spec, list):
                llm_requests.create_index(index_spec, **options)
            else:
                llm_requests.create_index(index_spec, **options)
            logger.info(f"    Created index: {options['name']}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"    Index exists: {options['name']}")
            else:
                logger.warning(f"    Failed to create index {options['name']}: {e}")

    # =========================================================================
    # Collection: agent_audit - stores complete agent execution history
    # =========================================================================
    agent_audit = db["agent_audit"]

    audit_indexes = [
        # Query by job
        ("job_id", {"name": "idx_audit_job_id"}),
        # Query by step type (llm_call, tool_call, etc.)
        ("step_type", {"name": "idx_audit_step_type"}),
        # Query by node name (initialize, process, tools, check)
        ("node_name", {"name": "idx_audit_node_name"}),
        # Query by timestamp
        ("timestamp", {"name": "idx_audit_timestamp"}),
        # Primary query: job + step_number for ordered retrieval
        ([("job_id", 1), ("step_number", 1)],
         {"name": "idx_audit_job_step"}),
        # Query by job + iteration for grouping
        ([("job_id", 1), ("iteration", 1), ("step_number", 1)],
         {"name": "idx_audit_job_iter_step"}),
        # Query by job + agent + step_type for filtering
        ([("job_id", 1), ("agent_type", 1), ("step_type", 1)],
         {"name": "idx_audit_job_agent_type"}),
    ]

    logger.info("  Configuring agent_audit collection...")
    for index_spec, options in audit_indexes:
        try:
            if isinstance(index_spec, list):
                agent_audit.create_index(index_spec, **options)
            else:
                agent_audit.create_index(index_spec, **options)
            logger.info(f"    Created index: {options['name']}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"    Index exists: {options['name']}")
            else:
                logger.warning(f"    Failed to create index {options['name']}: {e}")

    logger.info("  Collections and indexes configured")


def get_stats(client, db_name: str) -> dict:
    """Get database statistics."""
    db = client[db_name]
    stats = {}

    for collection_name in db.list_collection_names():
        count = db[collection_name].count_documents({})
        stats[collection_name] = count

    return stats


def initialize_mongodb(logger: logging.Logger, force_reset: bool = False) -> bool:
    """
    Initialize MongoDB (called by app_init.py).

    Args:
        logger: Logger instance.
        force_reset: If True, clear all data before initializing.

    Returns:
        True if successful, False otherwise.
    """
    params = get_connection_params()

    # Check if MongoDB URL is configured
    if not os.getenv("MONGODB_URL"):
        logger.info("  MongoDB not configured (MONGODB_URL not set)")
        logger.info("  Skipping MongoDB initialization (optional component)")
        return True

    try:
        from pymongo import MongoClient
        client = MongoClient(params['url'], serverSelectionTimeoutMS=5000)
        client.server_info()  # Test connection
        logger.info(f"  Connected to MongoDB")
    except ImportError:
        logger.info("  pymongo not installed (MongoDB is optional)")
        return True
    except Exception as e:
        logger.warning(f"  Could not connect to MongoDB: {e}")
        logger.info("  MongoDB is optional - continuing without it")
        return True

    try:
        # Parse database name from URL
        db_name = params['url'].split('/')[-1].split('?')[0]
        if not db_name:
            db_name = "graphrag_logs"

        # Show current state
        stats = get_stats(client, db_name)
        total_docs = sum(stats.values()) if stats else 0
        logger.info(f"  Current state: {total_docs} documents")

        # Clear if requested
        if force_reset and total_docs > 0:
            logger.info("  Clearing database...")
            clear_database(client, db_name, logger)

        # Create collections and indexes
        logger.info("  Configuring collections and indexes...")
        create_collections_and_indexes(client, db_name, logger)

        # Show final state
        stats = get_stats(client, db_name)
        logger.info(f"  Collections: {list(stats.keys())}")

        return True

    except Exception as e:
        logger.warning(f"  MongoDB initialization error: {e}")
        return True  # Don't fail the whole init

    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Initialize MongoDB for LLM request archiving",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initialize MongoDB
  %(prog)s

  # Clear and reinitialize
  %(prog)s --clear

  # Check connection only
  %(prog)s --check
        """
    )

    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all data before initializing"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check connection, don't initialize"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )

    args = parser.parse_args()
    logger = setup_logging(args.verbose)

    params = get_connection_params()
    print("=" * 60)
    print("MongoDB Initialization")
    print("=" * 60)
    print(f"URL: {params['url']}")
    print()

    # Check mode
    if args.check:
        success = check_connection(logger)
        sys.exit(0 if success else 1)

    # Initialize
    success = initialize_mongodb(logger, args.clear)

    print()
    if success:
        print("=" * 60)
        print("MongoDB initialization complete!")
        print("=" * 60)
    else:
        print("MongoDB initialization failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
