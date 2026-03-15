#!/usr/bin/env python3
"""Manual test script for RecallStore.

Inserts 10 fake memories into PostgreSQL and runs 3 retrieval queries
to verify hybrid search ranking. Requires:
- DATABASE_URL set
- OPENAI_API_KEY set (or EMBEDDING_API_KEY)
- PostgreSQL running with pgvector extension
- Schema applied (run `python init.py` first)

Usage:
    python tests/manual_test_recall.py
"""

import asyncio
import os
import sys
import uuid

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Test memories spanning different types and topics
FAKE_MEMORIES = [
    {
        "content": "The config system uses YAML files with $extends inheritance. "
                   "Child configs deep-merge with parent (defaults.yaml). "
                   "Arrays replace entirely, dicts merge recursively, null clears a key.",
        "summary": "Config uses YAML inheritance with deep merge",
        "keywords": ["config", "yaml", "extends", "inheritance", "deep_merge"],
        "importance": 0.9,
        "memory_type": "factual",
        "source": "observer",
        "source_phase": 1,
    },
    {
        "content": "PostgreSQL uses asyncpg with connection pooling. "
                   "Pool size is configured via POSTGRES_MIN_CONNECTIONS and POSTGRES_MAX_CONNECTIONS. "
                   "pgvector extension is registered on each connection via init callback.",
        "summary": "PostgreSQL uses asyncpg with pgvector",
        "keywords": ["postgres", "asyncpg", "pool", "pgvector", "connection"],
        "importance": 0.8,
        "memory_type": "factual",
        "source": "observer",
        "source_phase": 1,
    },
    {
        "content": "When writing Neo4j Cypher queries, always use MERGE with ON CREATE SET "
                   "instead of separate MATCH + CREATE. This is ~40% faster for this schema "
                   "and avoids duplicate node creation.",
        "summary": "Use MERGE instead of MATCH+CREATE in Cypher",
        "keywords": ["neo4j", "cypher", "merge", "performance", "query"],
        "importance": 0.85,
        "memory_type": "procedural",
        "source": "todo",
        "source_phase": 2,
    },
    {
        "content": "Attempted to use ts_rank with 'simple' dictionary for German text. "
                   "Failed: German compound words not tokenized properly. "
                   "Solution: Use 'german' dictionary or split compounds before indexing.",
        "summary": "German FTS needs 'german' dictionary, not 'simple'",
        "keywords": ["fts", "tsvector", "german", "tokenization", "dictionary"],
        "importance": 0.75,
        "memory_type": "error_solution",
        "source": "tool_error",
        "source_phase": 3,
    },
    {
        "content": "The workspace.md file is injected as a transient fake tool result "
                   "before every LLM call. It survives context compaction because it's "
                   "never stored in state — it's re-injected fresh each time.",
        "summary": "workspace.md is transient injection, survives compaction",
        "keywords": ["workspace", "injection", "transient", "compaction", "context"],
        "importance": 0.8,
        "memory_type": "factual",
        "source": "observer",
        "source_phase": 1,
    },
    {
        "content": "The agent uses a phase alternation model: strategic phases for planning "
                   "and tactical phases for execution. Phase transitions happen when all "
                   "todos in the current phase are completed.",
        "summary": "Strategic/tactical phase alternation model",
        "keywords": ["phase", "strategic", "tactical", "alternation", "transition"],
        "importance": 0.9,
        "memory_type": "factual",
        "source": "compaction",
        "source_phase": 2,
    },
    {
        "content": "GoBD stands for 'Grundsätze zur ordnungsmäßigen Führung und "
                   "Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in "
                   "elektronischer Form sowie zum Datenzugriff'. It's a German regulation "
                   "for electronic bookkeeping.",
        "summary": "GoBD = German electronic bookkeeping regulation",
        "keywords": ["gobd", "german", "regulation", "bookkeeping", "compliance"],
        "importance": 0.7,
        "memory_type": "vocabulary",
        "source": "observer",
        "source_phase": 1,
    },
    {
        "content": "Citations in this system link claims to source evidence. "
                   "Each citation has a source_id (FK to sources table), a locator "
                   "(JSONB with page/section), and verification_status. "
                   "CitationEngine handles the full lifecycle.",
        "summary": "Citations link claims to sources via CitationEngine",
        "keywords": ["citation", "source", "locator", "verification", "citation_engine"],
        "importance": 0.75,
        "memory_type": "relational",
        "source": "phase_archive",
        "source_phase": 2,
    },
    {
        "content": "The todo_guide.md instruction file uses passive enforcement: "
                   "the next_phase_todos tool rejects calls until the agent reads "
                   "the guide file. This is controlled by enforce: true in the config.",
        "summary": "todo_guide uses passive enforcement (tool rejection)",
        "keywords": ["instruction_files", "enforce", "todo_guide", "passive", "tool_rejection"],
        "importance": 0.65,
        "memory_type": "procedural",
        "source": "observer",
        "source_phase": 3,
    },
    {
        "content": "The KeyRing in src/llm/key_ring.py provides API key rotation. "
                   "Comma-separated keys in env vars are rotated on auth/quota failures. "
                   "Thread-safe with masked key logging. Cooldown prevents hammering "
                   "a failing key.",
        "summary": "KeyRing rotates API keys on failure with cooldown",
        "keywords": ["keyring", "api_key", "rotation", "cooldown", "failover"],
        "importance": 0.7,
        "memory_type": "factual",
        "source": "observer",
        "source_phase": 1,
    },
]

# Test queries to evaluate retrieval quality
TEST_QUERIES = [
    {
        "query": "How does configuration inheritance work in this system?",
        "expected_topics": ["config", "yaml", "extends"],
    },
    {
        "query": "What should I know about German text search and full-text indexing?",
        "expected_topics": ["german", "fts", "tsvector"],
    },
    {
        "query": "How do strategic and tactical phases interact with the todo system?",
        "expected_topics": ["phase", "strategic", "tactical", "todo"],
    },
]


async def main():
    print("=" * 70)
    print("RecallStore Manual Test")
    print("=" * 70)

    # Check prerequisites
    if not os.getenv("DATABASE_URL"):
        print("\nERROR: DATABASE_URL not set. Run:")
        print("  export DATABASE_URL=postgresql://srw:srw_password@localhost:5432/srw")
        sys.exit(1)

    if not (os.getenv("OPENAI_API_KEY") or os.getenv("EMBEDDING_API_KEY")):
        print("\nERROR: OPENAI_API_KEY or EMBEDDING_API_KEY not set.")
        sys.exit(1)

    from src.database.postgres_db import PostgresDB
    from src.services.embedding_service import get_embedding_service
    from src.services.recall_store import RecallStore

    # Connect to database
    db = PostgresDB()
    await db.connect()
    print("\nConnected to PostgreSQL")

    # Ensure schema exists
    try:
        from pathlib import Path
        schema_path = (
            Path(__file__).parent.parent
            / "src" / "database" / "queries" / "postgres" / "schema.sql"
        )
        async with db.acquire() as conn:
            await conn.execute(schema_path.read_text())
        print("Schema verified")
    except Exception as e:
        print(f"Schema error: {e}")
        print("Run `python init.py` first to initialize the database.")
        await db.close()
        sys.exit(1)

    # Create a test job
    job_id = uuid.uuid4()
    await db.execute(
        "INSERT INTO jobs (id, description, status) VALUES ($1, $2, $3)",
        job_id,
        "Manual recall test",
        "created",
    )
    print(f"Created test job: {job_id}")

    # Create store
    embedding_service = get_embedding_service()
    store = RecallStore(
        db=db,
        embedding_service=embedding_service,
        job_id=job_id,
    )

    # Insert fake memories
    print(f"\nInserting {len(FAKE_MEMORIES)} test memories...")
    for i, mem in enumerate(FAKE_MEMORIES):
        mem_id = await store.store(**mem)
        print(f"  [{i+1}] {mem['summary']} -> {mem_id}")

    # Get stats
    stats = await store.get_stats()
    print(f"\nStats: {stats['total']} memories, {stats['total_tokens']} tokens")
    print(f"  By type: factual={stats.get('factual', 0)}, "
          f"procedural={stats.get('procedural', 0)}, "
          f"error_solution={stats.get('error_solution', 0)}, "
          f"vocabulary={stats.get('vocabulary', 0)}, "
          f"relational={stats.get('relational', 0)}")
    print(f"  By source: observer={stats.get('from_observer', 0)}, "
          f"todo={stats.get('from_todo', 0)}, "
          f"compaction={stats.get('from_compaction', 0)}, "
          f"phase_archive={stats.get('from_phase_archive', 0)}, "
          f"tool_error={stats.get('from_tool_error', 0)}")

    # Run test queries
    print(f"\n{'=' * 70}")
    print("Running retrieval queries")
    print("=" * 70)

    for i, test in enumerate(TEST_QUERIES, 1):
        query = test["query"]
        expected = test["expected_topics"]

        print(f"\n--- Query {i}: {query}")
        print(f"    Expected topics: {', '.join(expected)}")

        memories = await store.retrieve(query, budget_tokens=10000)
        print(f"    Retrieved {len(memories)} memories:")

        for j, mem in enumerate(memories, 1):
            print(f"    [{j}] (imp={mem.importance:.1f}, "
                  f"type={mem.memory_type}, "
                  f"tokens={mem.token_count}) "
                  f"{mem.summary or mem.content[:80]}")

        # Check if expected topics are covered
        all_content = " ".join(m.content.lower() for m in memories)
        missing = [t for t in expected if t.lower() not in all_content]

        if missing:
            print(f"    WARNING: Missing expected topics: {', '.join(missing)}")
        else:
            print("    OK: All expected topics found in results")

    # Cleanup
    print(f"\n{'=' * 70}")
    print("Cleanup")
    print("=" * 70)
    await db.execute("DELETE FROM memories WHERE job_id = $1", job_id)
    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)
    print("Cleaned up test data")

    await db.close()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
