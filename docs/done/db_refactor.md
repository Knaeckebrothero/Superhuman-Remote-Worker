# Database Architecture Refactoring Guide

This document describes the database architecture refactoring for the **Graph-RAG** project (FINIUS requirement traceability system). The goal is to consolidate PostgreSQL, Neo4j, and MongoDB access into a unified `src/database/` module with clean separation of concerns and consistent patterns.

**Current State:** Database code is scattered across multiple files with inconsistent patterns (mix of async/sync, ad-hoc connections, multiple drivers).

**Target State:** Clean, modular database architecture with:
- Unified connection management for PostgreSQL, Neo4j, and MongoDB
- Consistent async/sync patterns
- Connection pooling and lifecycle management
- Easy testing and multi-environment support

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Design Patterns](#core-design-patterns)
3. [Module Structure](#module-structure)
4. [Implementation Guide](#implementation-guide)
5. [Code Examples](#code-examples)
6. [Best Practices](#best-practices)

---

## Architecture Overview

The database module follows a **layered service architecture** with three databases serving distinct purposes:

```
┌───────────────────────────────────────────────────────────────────┐
│                  Application Layer                                │
│        (UniversalAgent, Tools, Dashboard, Scripts)                │
└─────────────────────────────┬─────────────────────────────────────┘
                              │ imports
                              ▼
┌───────────────────────────────────────────────────────────────────┐
│              Database Module (src/database/)                      │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │      Database Classes (Dependency Injection)               │  │
│  │  PostgresDB │ Neo4jDB │ MongoDB (optional)                 │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌────────────────────────┬──────────────────┬─────────────────┐ │
│  │  PostgresDB Class      │  Neo4jDB Class   │  MongoDB Class  │ │
│  │  - Async pooling       │  - Sync driver   │  - Lazy connect │ │
│  │  - Context managers    │  - Session mgmt  │  - Audit logs   │ │
│  │  - CRUD operations     │  - Cypher exec   │  - LLM archive  │ │
│  │  - Query loading       │  - Schema ops    │  - Audit trail  │ │
│  └────────────────────────┴──────────────────┴─────────────────┘ │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  PostgreSQL Tables (SQLAlchemy Core or DDL)                │  │
│  │  - jobs, requirements                                      │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  SQL Query Files (queries/*.sql)                           │  │
│  │  - schema.sql (DDL), complex.sql (named queries)           │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Cypher Query Files (queries/*.cypher)                     │  │
│  │  - metamodel.cql (schema), seed_data.cypher                │  │
│  └────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
                              │ uses
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│   PostgreSQL     │ │      Neo4j       │ │   MongoDB        │
│   (asyncpg)      │ │   (bolt://)      │ │   (pymongo)      │
│                  │ │                  │ │                  │
│ - Job tracking   │ │ - Knowledge      │ │ - LLM logging    │
│ - Requirements   │ │   graph          │ │ - Audit trail    │
│ - Citations      │ │ - FINIUS model   │ │ - Debugging      │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

### Key Characteristics

- **Multi-Database Support**: PostgreSQL (relational), Neo4j (graph), MongoDB (document)
- **Single Responsibility**: Each database class has one clear purpose
- **Configuration Injection**: All configuration can be overridden at initialization
- **Connection Pooling**: Automatic connection lifecycle management (asyncpg for PostgreSQL)
- **Async/Sync Clarity**: PostgreSQL async (asyncpg), Neo4j sync (official driver), MongoDB lazy (pymongo)
- **Named Queries**: Complex SQL/Cypher separated from Python code
- **Testing-Friendly**: Easy to mock or inject test database instances
- **Optional MongoDB**: Gracefully degrades if MongoDB is not available (logging only)

### Database Roles in Graph-RAG

| Database | Purpose | Current Files | Driver |
|----------|---------|---------------|--------|
| **PostgreSQL** | Job tracking, requirements, citations | `postgres_utils.py`, `schema.sql` | `asyncpg` (async) |
| **Neo4j** | FINIUS knowledge graph, requirement relationships | `neo4j_utils.py`, `data/metamodell.cql` | `neo4j` (sync) |
| **MongoDB** | LLM request logging, audit trail (optional) | `src/core/archiver.py` | `pymongo` (sync) |

---

## Core Design Patterns

### 1. **Dependency Injection Pattern**

**Purpose**: Provide flexible, testable database access without global state.

**Rationale**: Proper databases (PostgreSQL with asyncpg, Neo4j with official driver, MongoDB with pymongo) all have built-in connection pooling that handles concurrency correctly. Singletons were only needed for SQLite to avoid write conflicts. Using dependency injection instead provides better testing, flexibility, and explicit dependencies.

**Implementation** (Graph-RAG with three databases):
```python
# src/database/__init__.py
from .postgres_db import PostgresDB
from .neo4j_db import Neo4jDB
from .mongo_db import MongoDB

# OLD API (backward compatibility - will be removed in Phase 5)
from .postgres_utils import PostgresConnection
from .neo4j_utils import Neo4jConnection

__all__ = [
    'PostgresDB', 'Neo4jDB', 'MongoDB',  # NEW classes
    'PostgresConnection', 'Neo4jConnection',  # OLD (deprecated)
]
```

**Usage Pattern**:
```python
# In agent or component initialization
from src.database import PostgresDB, Neo4jDB, MongoDB

# Create instances (each component creates its own)
postgres_db = PostgresDB()
await postgres_db.connect()

neo4j_db = Neo4jDB()
neo4j_db.connect()

mongo_db = MongoDB()  # Optional, lazy connection

# Pass to components via dependency injection
context = ToolContext(postgres_conn=postgres_db, neo4j_conn=neo4j_db)

# Use throughout application lifecycle
requirement = await postgres_db.jobs.create(prompt="Extract requirements")

# Cleanup
await postgres_db.close()
neo4j_db.close()
mongo_db.close()
```

**Benefits**:
- **No global state**: Each component creates and manages its own database instances
- **Better testing**: Easy to mock or inject test database instances
- **Flexibility**: Different components can use different configurations (e.g., read replicas)
- **Explicit dependencies**: Clear which components need which databases
- **Connection pooling**: Still efficient - asyncpg/neo4j/pymongo handle pooling internally
- **Thread-safe**: Driver-level connection pooling handles concurrency correctly

---

### 2. **Connection Pooling with Context Managers**

**Purpose**: Automatically manage database connections with proper cleanup.

**Implementation**:
```python
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

class Database:
    def __init__(self, host=None, port=None, ...):
        # Build connection URL
        url = f"postgresql://{user}:{password}@{host}:{port}/{database}"

        # Create engine with connection pooling
        self.engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=min_connections,  # e.g., 5
            max_overflow=max_connections - min_connections,  # e.g., 15
            pool_pre_ping=True,  # Verify connections before use
            echo=False,  # Set True for SQL debugging
        )

    @contextmanager
    def connection(self):
        """Context manager for connections with automatic commit/rollback."""
        conn = self.engine.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
```

**Usage**:
```python
with db.connection() as conn:
    result = conn.execute(select(users).where(users.c.email == email))
    user = result.fetchone()
```

**Benefits**:
- Automatic connection lifecycle management
- Implicit transaction handling (commit on success, rollback on error)
- No connection leaks (guaranteed cleanup)
- Pool automatically handles connection reuse

**Configuration Options**:
- `pool_size`: Number of connections to maintain in pool
- `max_overflow`: Additional connections allowed beyond pool_size
- `pool_pre_ping`: Test connection health before using
- `pool_recycle`: Recycle connections after N seconds

---

### 3. **Named Query Loading Pattern**

**Purpose**: Separate complex SQL from Python code for maintainability.

**Implementation**:

**Step 1: Define queries in .sql files**
```sql
-- queries/complex.sql

-- name: get_user_with_settings
SELECT u.id, u.email, u.name, s.theme, s.language
FROM users u
LEFT JOIN user_settings s ON u.id = s.user_id
WHERE u.email = :email;

-- name: get_recent_conversations
SELECT c.id, c.name, c."createdAt", COUNT(m.id) as message_count
FROM conversations c
LEFT JOIN messages m ON c.id = m."conversationId"
WHERE c."userId" = :user_id
GROUP BY c.id, c.name, c."createdAt"
ORDER BY c."updatedAt" DESC
LIMIT :limit;
```

**Step 2: Implement query loader**
```python
import re
from pathlib import Path

QUERIES_DIR = Path(__file__).parent / "queries"

class Database:
    def __init__(self, ...):
        # Cache for loaded queries
        self._queries: dict[str, str] = {}

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load a named query from .sql file."""
        cache_key = f"{filename}:{query_name}"

        # Return from cache if already loaded
        if cache_key in self._queries:
            return self._queries[cache_key]

        # Read file
        file_path = QUERIES_DIR / filename
        content = file_path.read_text()

        # Parse named queries using regex
        pattern = r"--\s*name:\s*(\w+)\s*\n(.*?)(?=--\s*name:|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        # Cache all queries from the file
        for name, sql in matches:
            self._queries[f"{filename}:{name}"] = sql.strip()

        if cache_key not in self._queries:
            raise ValueError(f"Query '{query_name}' not found in {filename}")

        return self._queries[cache_key]
```

**Step 3: Use in database methods**
```python
def get_user_with_settings(self, email: str) -> Optional[dict]:
    """Get user with settings using named query."""
    with self.connection() as conn:
        query = self._load_query("complex.sql", "get_user_with_settings")
        result = conn.execute(text(query), {"email": email})
        return self._row_to_dict(result.fetchone())
```

**Benefits**:
- Complex SQL is reviewable and versionable
- Syntax highlighting in .sql files (better IDE support)
- Easy to test queries independently
- No string concatenation or f-strings (SQL injection safety)
- Query results are cached (no repeated file I/O)

---

### 4. **SQLAlchemy Core Table Definitions**

**Purpose**: Define schema as code with type safety, avoiding ORM overhead.

**Implementation**:
```python
# database/tables.py
from sqlalchemy import (
    MetaData, Table, Column, Integer, BigInteger,
    Text, Boolean, DateTime, ForeignKey, Index, func
)
from sqlalchemy.dialects.postgresql import JSONB

# Create metadata instance
metadata = MetaData()

# Define tables
users = Table(
    'users',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('email', Text, unique=True, nullable=False),
    Column('name', Text),
    Column('created_at', DateTime, server_default=func.now()),
)

conversations = Table(
    'conversations',
    metadata,
    Column('id', Text, primary_key=True),  # UUID as text
    Column('userId', Integer, ForeignKey('users.id'), nullable=False),
    Column('name', Text, nullable=False),
    Column('createdAt', DateTime, server_default=func.now()),
    Column('updatedAt', DateTime, server_default=func.now(), onupdate=func.now()),
    Column('version', Integer, server_default='1'),  # Optimistic locking
)

messages = Table(
    'messages',
    metadata,
    Column('id', BigInteger, primary_key=True),
    Column('conversationId', Text, ForeignKey('conversations.id'), nullable=False),
    Column('roleName', Text, nullable=False),
    Column('content', Text, nullable=False),
    Column('time', BigInteger, nullable=False),
    Column('type', Text, server_default='text'),
    Column('version', Integer, server_default='1'),
    # Semi-structured data in JSONB
    Column('agent_steps', JSONB),  # Array of reasoning steps
)

# Define indexes
idx_conversation_time = Index(
    'idx_conversation_time',
    messages.c.conversationId,
    messages.c.time
)

# Partial index (PostgreSQL-specific)
idx_messages_agent = Index(
    'idx_messages_agent',
    messages.c.agent_status,
    postgresql_where=(messages.c.type == 'agent')
)
```

**Usage in CRUD operations**:
```python
from sqlalchemy import select, insert, update, delete
from .tables import users

# Simple queries use table definitions
def get_user_by_id(self, user_id: int) -> Optional[dict]:
    with self.connection() as conn:
        stmt = select(users).where(users.c.id == user_id)
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)

def create_user(self, email: str, name: str = None) -> dict:
    with self.connection() as conn:
        stmt = insert(users).values(email=email, name=name).returning(users)
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)
```

**Benefits**:
- Type-safe query construction
- No manual SQL for simple CRUD operations
- Compile-time checking (typo in column name = error)
- Database-agnostic (mostly - use dialects for DB-specific features)
- Schema is self-documenting code

**Key Features**:
- `server_default`: Set by database (e.g., timestamps, version counters)
- `onupdate`: Auto-update timestamps on row changes
- `ForeignKey`: Enforce referential integrity
- `JSONB`: Semi-structured data with queryability
- `Index`: Performance optimization
- `postgresql_where`: Partial indexes for specific row subsets

---

### 5. **CRUD Operation Patterns**

**Purpose**: Consistent, predictable data access methods.

#### **Create Pattern** (with `returning`)
```python
def create_message(
    self,
    message_id: int,
    conversation_id: str,
    role_name: str,
    content: str,
    time: int,
    message_type: str = "text"
) -> dict:
    """Create a new message and return it."""
    with self.connection() as conn:
        stmt = (
            insert(messages)
            .values(
                id=message_id,
                conversationId=conversation_id,
                roleName=role_name,
                content=content,
                time=time,
                type=message_type,
            )
            .returning(messages)
        )
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)
```

#### **Read Pattern** (with filtering)
```python
def get_user_by_email(self, email: str) -> Optional[dict]:
    """Get user by email (case-insensitive)."""
    with self.connection() as conn:
        stmt = select(users).where(users.c.email.ilike(email))
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)

def get_messages_by_conversation(
    self,
    conversation_id: str,
    limit: int = 30
) -> list[dict]:
    """Get recent messages for a conversation."""
    with self.connection() as conn:
        stmt = (
            select(messages)
            .where(messages.c.conversationId == conversation_id)
            .order_by(messages.c.time.desc())
            .limit(limit)
        )
        results = conn.execute(stmt).fetchall()
        return [self._row_to_dict(row) for row in results]
```

#### **Update Pattern** (with optimistic locking)
```python
def update_message(
    self,
    message_id: int,
    conversation_id: str,
    content: str,
    version: int
) -> Optional[dict]:
    """Update message with optimistic locking."""
    with self.connection() as conn:
        stmt = (
            update(messages)
            .where(
                (messages.c.id == message_id) &
                (messages.c.conversationId == conversation_id) &
                (messages.c.version == version)  # Prevent race conditions
            )
            .values(
                content=content,
                version=version + 1,
                lastModified=int(time.time() * 1000)
            )
            .returning(messages)
        )
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)
```

**Optimistic Locking Benefit**: If two clients try to update the same row simultaneously, only one succeeds. The other gets `None` returned and knows to retry.

#### **Delete Pattern**
```python
def delete_conversation(self, conversation_id: str, user_id: int) -> bool:
    """Delete conversation (CASCADE will delete messages)."""
    with self.connection() as conn:
        stmt = delete(conversations).where(
            (conversations.c.id == conversation_id) &
            (conversations.c.userId == user_id)  # Security: verify ownership
        )
        result = conn.execute(stmt)
        return result.rowcount > 0
```

#### **Upsert Pattern** (Insert or Update)

**Option 1: Try update, then insert**
```python
def upsert_user_settings(
    self,
    user_id: int,
    theme: str,
    language: str
) -> dict:
    """Insert settings or update if they exist."""
    with self.connection() as conn:
        # Try update first
        stmt = (
            update(user_settings)
            .where(user_settings.c.user_id == user_id)
            .values(theme=theme, language=language)
            .returning(user_settings)
        )
        result = conn.execute(stmt).fetchone()

        # Insert if not found
        if result is None:
            stmt = (
                insert(user_settings)
                .values(user_id=user_id, theme=theme, language=language)
                .returning(user_settings)
            )
            result = conn.execute(stmt).fetchone()

        return self._row_to_dict(result)
```

**Option 2: PostgreSQL ON CONFLICT (more efficient)**
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

def upsert_file_cache(self, cache_key: str, description: str) -> dict:
    """Upsert using PostgreSQL ON CONFLICT."""
    with self.connection() as conn:
        stmt = (
            pg_insert(file_description_cache)
            .values(cache_key=cache_key, description=description)
            .on_conflict_do_update(
                index_elements=['cache_key'],
                set_={'description': description}
            )
            .returning(file_description_cache)
        )
        result = conn.execute(stmt).fetchone()
        return self._row_to_dict(result)
```

---

### 6. **Helper Methods for Data Transformation**

**Purpose**: Consistent row-to-dict conversion and data handling.

```python
def _row_to_dict(self, row) -> Optional[dict]:
    """Convert SQLAlchemy row to dictionary."""
    if row is None:
        return None
    return dict(row._mapping)
```

**Usage**:
```python
result = conn.execute(select(users).where(...)).fetchone()
user_dict = self._row_to_dict(result)  # {"id": 1, "email": "...", ...}
```

**Benefits**:
- Single source of truth for conversion logic
- Handles None consistently (returns None, not error)
- Easy to modify if you need custom serialization

---

### 7. **Configuration Injection Pattern**

**Purpose**: Allow testing and multi-environment support.

**Implementation**:
```python
class Database:
    def __init__(
        self,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None,
        min_connections: int = None,
        max_connections: int = None,
        database_url: str = None,
    ):
        # Fall back to environment/config if not provided
        self._host = host or os.getenv('POSTGRES_HOST', 'localhost')
        self._port = port or int(os.getenv('POSTGRES_PORT', 5432))
        # ... etc

        # Build connection URL
        if database_url:
            url = database_url
        else:
            url = f"postgresql://{self._user}:{self._password}@{self._host}:{self._port}/{self._database}"

        self.engine = create_engine(url, ...)
```

**Usage**:

**Production** (use config):
```python
db = Database()  # Uses env vars or config.py
```

**Testing** (inject test database):
```python
test_db = Database(database="test_db", min_connections=1, max_connections=2)
```

**Custom environment**:
```python
staging_db = Database(host="staging.example.com", database="staging_db")
```

**Benefits**:
- Easy to test (no environment pollution)
- Multi-database support (e.g., separate read replicas)
- Explicit configuration overrides
- Default behavior still works (env vars/config)

---

### 8. **Neo4j Session Management Pattern**

**Purpose**: Execute Cypher queries with proper session lifecycle in Neo4j.

**Implementation**:
```python
from neo4j import GraphDatabase
from contextlib import contextmanager
from typing import List, Dict, Optional

class Neo4jDB:
    """Neo4j database manager with session-based queries."""

    def __init__(
        self,
        uri: str = None,
        username: str = None,
        password: str = None,
    ):
        """Initialize Neo4j driver."""
        self._uri = uri or os.getenv('NEO4J_URI', 'bolt://localhost:7687')
        self._username = username or os.getenv('NEO4J_USERNAME', 'neo4j')
        self._password = password or os.getenv('NEO4J_PASSWORD', 'neo4j_password')

        # Create driver (connection pooling handled internally)
        self.driver = GraphDatabase.driver(
            self._uri,
            auth=(self._username, self._password)
        )

        # Cache for loaded queries
        self._queries: dict[str, str] = {}

    def connect(self) -> bool:
        """Verify connectivity."""
        try:
            self.driver.verify_connectivity()
            return True
        except Exception as e:
            log.error(f"Neo4j connection failed: {e}")
            return False

    def close(self) -> None:
        """Close driver."""
        if self.driver:
            self.driver.close()

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict] = None
    ) -> List[Dict]:
        """Execute Cypher query and return results."""
        with self.driver.session() as session:
            result = session.run(query, parameters or {})
            return [dict(record) for record in result]

    def execute_write(
        self,
        query: str,
        parameters: Optional[Dict] = None
    ) -> List[Dict]:
        """Execute write transaction."""
        with self.driver.session() as session:
            result = session.execute_write(
                lambda tx: list(tx.run(query, parameters or {}))
            )
            return [dict(record) for record in result]

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load named Cypher query from .cypher/.cql file."""
        # Similar pattern to PostgreSQL named queries
        cache_key = f"{filename}:{query_name}"
        if cache_key in self._queries:
            return self._queries[cache_key]

        file_path = QUERIES_DIR / "neo4j" / filename
        content = file_path.read_text()

        # Parse named queries: // name: query_name
        pattern = r"//\s*name:\s*(\w+)\s*\n(.*?)(?=//\s*name:|\\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        for name, cypher in matches:
            self._queries[f"{filename}:{name}"] = cypher.strip()

        return self._queries[cache_key]
```

**Usage**:
```python
# Simple query
results = neo4j_db.execute_query(
    "MATCH (r:Requirement {rid: $rid}) RETURN r",
    {"rid": "R001"}
)

# Write transaction
neo4j_db.execute_write(
    """
    CREATE (r:Requirement {
        rid: $rid,
        text: $text,
        category: $category
    })
    """,
    {"rid": "R002", "text": "...", "category": "functional"}
)

# Named query
query = neo4j_db._load_query("finius.cypher", "find_similar_requirements")
results = neo4j_db.execute_query(query, {"embedding": embedding_vector})
```

**Benefits**:
- Session lifecycle managed automatically
- Driver handles connection pooling internally
- Transaction support for write operations
- Named Cypher queries separated from Python code
- Easy to test with mock driver

**Key Differences from PostgreSQL**:
- **Sync not async**: Neo4j driver is synchronous
- **Session-based**: Each query creates/closes a session
- **No explicit pool**: Driver manages pooling internally
- **Graph queries**: Cypher instead of SQL

---

### 9. **MongoDB Lazy Connection Pattern**

**Purpose**: Optional MongoDB connection for logging that gracefully degrades if unavailable.

**Implementation**:
```python
from pymongo import MongoClient
from typing import Optional, List, Dict
import logging

log = logging.getLogger(__name__)

class MongoDB:
    """MongoDB manager with lazy connection for optional logging."""

    def __init__(self, url: str = None):
        """Initialize MongoDB client (lazy connection)."""
        self._url = url or os.getenv('MONGODB_URL')
        self._client: Optional[MongoClient] = None
        self._db = None
        self._connected = False

    def _connect(self) -> bool:
        """Lazy connect to MongoDB."""
        if self._connected:
            return True

        if not self._url:
            log.info("MongoDB URL not configured, logging disabled")
            return False

        try:
            self._client = MongoClient(self._url)
            # Test connection
            self._client.admin.command('ping')
            # Extract database name from URL
            db_name = self._url.split('/')[-1].split('?')[0] or 'graphrag_logs'
            self._db = self._client[db_name]
            self._connected = True
            log.info(f"MongoDB connected: {db_name}")
            return True
        except Exception as e:
            log.warning(f"MongoDB connection failed: {e}")
            self._connected = False
            return False

    def archive_llm_request(
        self,
        job_id: str,
        agent_type: str,
        messages: List[Dict],
        response: Dict,
        model: str,
        **metadata
    ) -> Optional[str]:
        """Archive LLM request/response."""
        if not self._connect():
            return None  # Gracefully degrade

        try:
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "messages": messages,
                "response": response,
                "model": model,
                "timestamp": datetime.utcnow(),
                **metadata
            }
            result = self._db.llm_requests.insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            log.error(f"Failed to archive LLM request: {e}")
            return None

    def audit_tool_call(
        self,
        job_id: str,
        agent_type: str,
        tool_name: str,
        inputs: Dict,
        **metadata
    ) -> Optional[str]:
        """Audit tool invocation."""
        if not self._connect():
            return None

        try:
            doc = {
                "job_id": job_id,
                "agent_type": agent_type,
                "event_type": "tool_call",
                "tool_name": tool_name,
                "inputs": inputs,
                "timestamp": datetime.utcnow(),
                **metadata
            }
            result = self._db.agent_audit.insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            log.error(f"Failed to audit tool call: {e}")
            return None

    def get_job_audit_trail(self, job_id: str) -> List[Dict]:
        """Get complete audit trail for a job."""
        if not self._connect():
            return []

        try:
            cursor = self._db.agent_audit.find(
                {"job_id": job_id}
            ).sort("timestamp", 1)
            return list(cursor)
        except Exception as e:
            log.error(f"Failed to get audit trail: {e}")
            return []

    def close(self) -> None:
        """Close MongoDB connection."""
        if self._client:
            self._client.close()
            self._connected = False
```

**Usage**:
```python
from src.database import mongo_db

# Archive LLM request (no error if MongoDB unavailable)
mongo_db.archive_llm_request(
    job_id="abc-123",
    agent_type="creator",
    messages=[...],
    response={...},
    model="gpt-4"
)

# Audit tool call
mongo_db.audit_tool_call(
    job_id="abc-123",
    agent_type="creator",
    tool_name="extract_document_text",
    inputs={"file_path": "doc.pdf"}
)

# Retrieve audit trail
trail = mongo_db.get_job_audit_trail("abc-123")
```

**Benefits**:
- **Lazy connection**: Only connects when first used
- **Graceful degradation**: Returns None/empty list if unavailable
- **Optional dependency**: Application works without MongoDB
- **Debugging aid**: Full audit trail for troubleshooting
- **No overhead**: Zero cost if not configured

**Key Differences from PostgreSQL/Neo4j**:
- **Optional**: Other databases are required, MongoDB is not
- **Logging only**: Not part of core application logic
- **No pooling needed**: pymongo handles internally
- **Simple documents**: No complex queries needed

---

## Module Structure

**Current Structure:**

```
src/database/
├── __init__.py              # Package initialization (currently minimal)
├── postgres_utils.py        # PostgreSQL connection and utilities (496 lines)
├── neo4j_utils.py           # Neo4j connection and utilities (161 lines)
└── schema.sql               # PostgreSQL schema (jobs, requirements tables)
```

**Target Structure:**

```
src/database/
├── __init__.py              # Module exports (classes only, no singletons)
│                            # from .postgres_db import PostgresDB
│                            # from .neo4j_db import Neo4jDB
│                            # from .mongo_db import MongoDB
│                            # __all__ = ['PostgresDB', 'Neo4jDB', 'MongoDB']
│
├── postgres_db.py           # PostgreSQL manager class (refactored from postgres_utils.py)
├── neo4j_db.py              # Neo4j manager class (refactored from neo4j_utils.py)
├── mongo_db.py              # MongoDB manager class (moved from src/core/archiver.py)
│
├── tables.py                # SQLAlchemy Core table definitions (NEW, optional)
│                            # Alternative: Keep using schema.sql DDL
│
└── queries/
    ├── postgres/
    │   ├── schema.sql       # PostgreSQL DDL (CREATE TABLE, indexes, triggers)
    │   ├── complex.sql      # Named complex SQL queries (NEW)
    │   └── seed.sql         # Test/demo data for PostgreSQL (NEW, optional)
    │
    └── neo4j/
        ├── metamodell.cql   # Neo4j schema (MOVED from data/)
        └── seed_data.cypher # Neo4j seed data (MOVED from data/)
```

**Additional Locations:**
- `src/core/archiver.py` - Will import MongoDB, no longer contain MongoDBarchiver class
- `scripts/init_db.py` - Will use PostgresDB class
- `scripts/init_neo4j.py` - Will use Neo4jDB class

### File Responsibilities

#### `__init__.py` - Module Interface
```python
"""Database module for Graph-RAG system (PostgreSQL, Neo4j, MongoDB)."""
from .postgres_db import PostgresDB
from .neo4j_db import Neo4jDB
from .mongo_db import MongoDB

# OLD API (backward compatibility - will be removed in Phase 5)
from .postgres_utils import PostgresConnection
from .neo4j_utils import Neo4jConnection

__all__ = [
    'PostgresDB',  # PostgreSQL (NEW)
    'Neo4jDB',     # Neo4j (NEW)
    'MongoDB',     # MongoDB (NEW, optional)
    'PostgresConnection',  # OLD (deprecated)
    'Neo4jConnection',     # OLD (deprecated)
]
```

#### `postgres_db.py` - PostgreSQL Manager
- **Async connection pooling** (asyncpg)
- Context managers for connection lifecycle
- CRUD operations for jobs, requirements, citations
- Query loading from `queries/postgres/complex.sql`
- Helper methods (`create_job`, `get_pending_requirement`, etc.)
- Replaces: `src/database/postgres_utils.py`

#### `neo4j_db.py` - Neo4j Manager
- **Sync driver** (official neo4j driver)
- Session management for Cypher queries
- Schema operations (load from `queries/neo4j/metamodell.cql`)
- Query execution with parameter binding
- Helper methods (`create_requirement_node`, `create_fulfillment_relationship`, etc.)
- Replaces: `src/database/neo4j_utils.py`

#### `mongo_db.py` - MongoDB Manager (Optional)
- **Lazy connection** (pymongo)
- LLM request archiving (`llm_requests` collection)
- Agent audit trail (`agent_audit` collection)
- Gracefully degrades if MongoDB not available
- Query methods (`get_conversation`, `get_job_audit_trail`, etc.)
- Replaces: MongoDB logic from `src/core/archiver.py`

#### `tables.py` - Schema Definitions (Optional)
- Define PostgreSQL tables using SQLAlchemy Core
- Define indexes
- Alternative to DDL in `queries/postgres/schema.sql`
- Useful for ORM-like features or migrations

#### `queries/postgres/schema.sql` - PostgreSQL DDL
```sql
-- Tables
CREATE TABLE IF NOT EXISTS jobs (...);
CREATE TABLE IF NOT EXISTS requirements (...);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_requirements_job_id ON requirements(job_id);

-- Views
CREATE OR REPLACE VIEW job_summary AS ...;
```

#### `queries/postgres/complex.sql` - Named SQL Queries
```sql
-- name: get_requirements_by_job
SELECT r.*, j.document_path
FROM requirements r
JOIN jobs j ON r.job_id = j.id
WHERE j.id = :job_id
ORDER BY r.created_at DESC;
```

#### `queries/neo4j/metamodell.cql` - Neo4j Schema
```cypher
// Constraints
CREATE CONSTRAINT req_rid IF NOT EXISTS FOR (r:Requirement) REQUIRE r.rid IS UNIQUE;

// Indexes
CREATE INDEX req_category IF NOT EXISTS FOR (r:Requirement) ON (r.category);
```

#### `queries/neo4j/seed_data.cypher` - Neo4j Seed Data
```cypher
// Create sample business objects
CREATE (bo:BusinessObject {name: "Vertrag", description: "..."});
```

#### Initialization Scripts
- `scripts/init_db.py` - Uses `PostgresDB` to initialize PostgreSQL
- `scripts/init_neo4j.py` - Uses `Neo4jDB` to initialize Neo4j
- `scripts/app_init.py` - Orchestrates full system initialization

---

## Current Issues & Refactoring Objectives

### Issues in Current Implementation

Based on analysis of the codebase, these issues need to be addressed:

#### 1. **Mixed Async/Sync Patterns**
- **PostgresConnection** uses `asyncpg` (async) with connection pooling
- **Dashboard (`dashboard/db.py`)** uses `psycopg2` (sync) with ad-hoc connections
- **Scripts** mix both `asyncpg` and `psycopg2`
- **Citation Engine** uses `psycopg2` directly
- **Problem**: Cannot share connections, code duplication, confusion about which pattern to use

#### 2. **ToolContext Async/Sync Mismatch**
- `ToolContext.update_job_status()` (line 224 in `src/tools/context.py`) uses sync cursor pattern:
  ```python
  cursor.execute(...)  # Won't work with asyncpg pool!
  ```
- But `context.postgres_conn` is async `PostgresConnection`
- **Problem**: This code will fail at runtime

#### 3. **Inconsistent Connection Initialization**
- **UniversalAgent** creates connections in `_setup_connections()`
- **Neo4jConnection** doesn't call `connect()` in agent initialization (relies on auto-connect)
- **Dashboard** creates connections per-request
- **Scripts** create ad-hoc connections
- **Problem**: No consistent lifecycle management, potential connection leaks

#### 4. **No Connection Validation or Health Checks**
- Connections are created but not validated
- No retry logic for transient failures
- No health checks before use
- **Problem**: Silent failures, hard to debug connection issues

#### 5. **Scattered Database Code**
- PostgreSQL utils in `src/database/postgres_utils.py`
- Neo4j utils in `src/database/neo4j_utils.py`
- MongoDB archiver in `src/core/archiver.py` (not in database module)
- Helper functions duplicated across files
- **Problem**: Hard to maintain, no single source of truth

#### 6. **No Unified Configuration**
- Each database reads env vars independently
- No central configuration class
- Connection strings built in multiple places
- **Problem**: Hard to test, hard to switch environments

#### 7. **Citation Engine Isolation**
- Citation tool (`citation_tool/`) creates its own PostgreSQL connection
- Uses `psycopg2` directly
- No integration with main application connections
- **Problem**: Extra connections, cannot reuse pooling

### Refactoring Objectives

The refactoring aims to:

1. **Consolidate** all database code into `src/database/` module
2. **Unify** connection management with consistent patterns
3. **Choose** async for PostgreSQL (keep asyncpg), sync for Neo4j/MongoDB
4. **Implement** proper connection pooling and lifecycle management
5. **Add** health checks, validation, and retry logic
6. **Use** dependency injection pattern for flexible, testable code
7. **Separate** schema (DDL/CQL) from application code
8. **Document** patterns for consistent usage across the codebase
9. **Enable** easy testing with dependency injection
10. **Maintain** backward compatibility during transition

### Migration Strategy

**Phase 1: Create New Classes (Non-Breaking)**
- Create `postgres_db.py`, `neo4j_db.py`, `mongo_db.py` with new APIs
- Keep existing `postgres_utils.py`, `neo4j_utils.py`, `archiver.py` unchanged
- Update `__init__.py` to export both old and new

**Phase 2: Update Core Components**
- Update `UniversalAgent` to use new database classes
- Update `ToolContext` to use new classes
- Update `graph_tools.py` and `cache_tools.py`
- Test thoroughly

**Phase 3: Update Dashboard & Scripts**
- Update `dashboard/db.py` to use shared `PostgresDB`
- Update `scripts/` to use new classes
- Update `citation_tool` to accept connection injection

**Phase 4: Deprecate Old Code**
- Mark old utility functions as deprecated
- Add migration guide to docstrings
- Keep old code for one release cycle

**Phase 5: Remove Old Code**
- Delete `postgres_utils.py`, `neo4j_utils.py`
- Simplify `archiver.py` to wrapper around `MongoDB`
- Update all documentation

---

## Unified Migration Roadmap

**See Detailed Deep Dives:**
- [postgres_migration_deep_dive.md](postgres_migration_deep_dive.md) - PostgreSQL async migration (5 phases, 2-3 weeks)
- [neo4j_migration_deep_dive.md](neo4j_migration_deep_dive.md) - Neo4j sync migration (5 phases, 3 weeks)
- [mongodb_migration_deep_dive.md](mongodb_migration_deep_dive.md) - MongoDB migration (4 phases, 1-2 weeks)

This section coordinates all three database migrations into a unified timeline with dependencies, critical path, and integration milestones.

### Executive Summary

**Total Duration**: 5-6 weeks (with parallel work)
**Risk Level**: Medium (critical PostgreSQL bug fix, complex coordination)
**Approach**: Parallel migration where possible, sequential where dependencies exist

**Key Decisions**:
- **PostgreSQL** migrates to async-only (asyncpg) - **CRITICAL**: Fixes runtime bug
- **Neo4j** remains synchronous (official driver) - clean namespace refactor
- **MongoDB** remains synchronous (pymongo) - simplest migration, optional component
- All three adopt **namespace pattern** for consistent API design

### Timeline Overview

```
Week 1-2: PostgreSQL Migration (Critical Path)
├─ Phase 1: Create PostgresDB class (async)
├─ Phase 2: Fix ToolContext bug (CRITICAL)
├─ Phase 3: Migrate tools (cache_tools, etc.)
└─ Phase 4: Update dashboard/scripts

Week 2-4: Neo4j Migration (Parallel with PostgreSQL Phase 3-4)
├─ Phase 1: Create Neo4jDB class (sync)
├─ Phase 2: Migrate graph tools
├─ Phase 3: Move metamodel validator
└─ Phase 4: Update agent initialization

Week 3-4: MongoDB Migration (Parallel, Low Priority)
├─ Phase 1: Create MongoDBDB class
├─ Phase 2: Update initialization scripts
├─ Phase 3: Migrate graph usage (audit points)
└─ Phase 4: Cleanup old archiver

Week 5: Integration Testing
├─ End-to-end system tests
├─ Performance testing
├─ Backward compatibility validation
└─ Documentation finalization

Week 6: Cleanup and Stabilization
├─ Remove deprecated code
├─ Final testing across all components
├─ Production deployment preparation
└─ Monitoring and rollback plans
```

### Detailed Phase Coordination

#### Week 1: PostgreSQL Phase 1-2 (Critical Path)

**Goal**: Create async PostgresDB and fix ToolContext bug

**PostgreSQL Tasks**:
- ✅ Create `src/database/postgres_db.py` (async)
- ✅ Implement connection pooling with asyncpg
- ✅ Create namespace operations (jobs, requirements, citations)
- ✅ Write unit tests (90%+ coverage)
- 🔥 **CRITICAL**: Fix `ToolContext.update_job_status()` bug
- ✅ Test async operations thoroughly

**Parallel Work** (can start):
- 📝 Neo4j Phase 1: Create Neo4jDB class structure
- 📝 MongoDB Phase 1: Create MongoDBDB class structure

**Deliverables**:
- [ ] `src/database/postgres_db.py` functional and tested
- [ ] ToolContext bug fixed and verified
- [ ] Unit tests passing (PostgreSQL)

**Blockers/Dependencies**:
- None (can start immediately)

**Risk**: High - Critical bug in ToolContext must be fixed before proceeding

---

#### Week 2: PostgreSQL Phase 3 + Neo4j Phase 1-2

**Goal**: Migrate PostgreSQL tools and start Neo4j migration

**PostgreSQL Tasks**:
- Migrate `src/tools/cache_tools.py` to use postgres_db
- Migrate `src/tools/citation_tools.py` to use postgres_db
- Update all tool usage in `src/agent.py`
- Integration testing with tools

**Neo4j Tasks**:
- Create `src/database/neo4j_db.py` (sync)
- Implement namespace operations (requirements, entities, relationships, statistics)
- Create StepType enum and dataclasses
- Write unit tests
- Start migrating `src/tools/graph_tools.py`

**Parallel Work**:
- 📝 MongoDB Phase 1-2 continues

**Deliverables**:
- [ ] PostgreSQL tools migrated
- [ ] Neo4jDB class functional
- [ ] Graph tools migration in progress

**Blockers/Dependencies**:
- Depends on: PostgreSQL Phase 1-2 complete
- Neo4j can proceed independently

**Risk**: Medium - Tool migration affects agent functionality

---

#### Week 3: PostgreSQL Phase 4 + Neo4j Phase 3 + MongoDB Phase 1-2

**Goal**: Complete PostgreSQL migration, advance Neo4j, start MongoDB integration

**PostgreSQL Tasks**:
- Update `dashboard/db.py` to use postgres_db
- Migrate scripts to use postgres_db
- Full integration testing
- Performance benchmarking

**Neo4j Tasks**:
- Complete graph tools migration
- Move `src/utils/metamodel_validator.py` to `src/database/`
- Update agent initialization in `src/agent.py`
- Integration testing with validator

**MongoDB Tasks**:
- Create `src/database/mongodb_db.py`
- Update `scripts/init_mongodb.py`
- Start migrating `src/graph.py` audit points (15+)

**Deliverables**:
- [ ] PostgreSQL fully migrated (all components)
- [ ] Neo4j tools and validator migrated
- [ ] MongoDB initialization updated

**Blockers/Dependencies**:
- PostgreSQL Phase 4 depends on Phase 3
- Neo4j Phase 3 depends on Phase 2
- MongoDB can proceed independently

**Risk**: Medium - Multiple migrations active, coordination needed

---

#### Week 4: Neo4j Phase 4 + MongoDB Phase 3 + PostgreSQL Cleanup

**Goal**: Complete Neo4j migration, advance MongoDB, start PostgreSQL cleanup

**Neo4j Tasks**:
- Finalize agent initialization updates
- Complete all testing
- Performance benchmarking
- Documentation updates

**MongoDB Tasks**:
- Complete all 15+ audit point migrations in `src/graph.py`
- Update `src/core/__init__.py` exports
- Test with MongoDB disabled (graceful degradation)
- Integration testing

**PostgreSQL Tasks**:
- Begin deprecation warnings in old code
- Update documentation
- Create migration guide

**Deliverables**:
- [ ] Neo4j fully migrated
- [ ] MongoDB audit points migrated
- [ ] PostgreSQL documentation complete

**Blockers/Dependencies**:
- Neo4j Phase 4 depends on Phase 3
- MongoDB Phase 3 depends on Phase 2

**Risk**: Low - Most complex work complete

---

#### Week 5: Integration Testing and MongoDB Cleanup

**Goal**: End-to-end system validation

**Integration Testing**:
- **Creator Agent Workflow**: Document ingestion → requirement extraction → PostgreSQL storage
- **Validator Agent Workflow**: Requirement validation → Neo4j integration → fulfillment checks
- **Dashboard**: Job management, requirement viewing, statistics
- **Audit Trail**: MongoDB logging verification (with and without MongoDB)
- **Scripts**: All initialization scripts, view scripts, job management

**MongoDB Tasks**:
- Remove old `src/core/archiver.py` implementation
- Update `scripts/view_llm_conversation.py`
- Final testing and documentation

**Performance Testing**:
- Connection pool sizing under load
- Query performance with new modules
- Memory usage validation
- Concurrent agent execution

**Test Scenarios**:
```python
# Scenario 1: Full creator workflow
python agent.py --config creator --document-path doc.pdf --prompt "Extract requirements"
# Verify: PostgreSQL storage, MongoDB audit trail

# Scenario 2: Full validator workflow
python agent.py --config validator --job-id <uuid>
# Verify: Neo4j integration, PostgreSQL updates, MongoDB audit

# Scenario 3: Dashboard operations
streamlit run dashboard/app.py
# Verify: Job listing, requirement viewing, statistics

# Scenario 4: MongoDB disabled
unset MONGODB_URL
python agent.py --config creator --prompt "test"
# Verify: Graceful degradation, no errors
```

**Deliverables**:
- [ ] All integration tests passing
- [ ] Performance benchmarks recorded
- [ ] Graceful degradation verified
- [ ] MongoDB fully migrated

**Blockers/Dependencies**:
- Depends on all Phase 3-4 completions

**Risk**: Low - Validation phase

---

#### Week 6: Final Cleanup and Production Readiness

**Goal**: Remove deprecated code, finalize documentation, prepare for production

**Cleanup Tasks**:
- Delete `src/database/postgres_utils.py`
- Delete `src/database/neo4j_utils.py`
- Remove old archiver code from `src/core/archiver.py`
- Search for remaining imports: `grep -r "postgres_utils\|neo4j_utils" src/`

**Documentation Tasks**:
- Update `CLAUDE.md` with new module usage examples
- Update `README.md` architecture diagrams
- Create `MIGRATION_GUIDE.md` for external users
- Add CHANGELOG entries for breaking changes

**Production Preparation**:
- Create rollback plan
- Set up monitoring for connection pools
- Configure alerts for database errors
- Document troubleshooting procedures

**Final Testing**:
- Clean room test (fresh checkout, fresh databases)
- Docker Compose deployment test
- Performance regression test
- Security audit (connection string handling, SQL injection protection)

**Deliverables**:
- [ ] All old code removed
- [ ] Documentation complete
- [ ] Production deployment guide ready
- [ ] Rollback procedures documented

**Blockers/Dependencies**:
- Depends on Week 5 integration testing

**Risk**: Low - Stabilization phase

---

### Critical Path Analysis

**Critical Path** (longest sequence of dependent tasks):
```
PostgreSQL Phase 1 → Phase 2 (bug fix) → Phase 3 (tools) → Phase 4 (dashboard) → Integration Testing → Cleanup
└─ 2 weeks ───────────────────────── 1 week ────────────────── 1 week ─────────── 1 week ──────────── 1 week
Total: 6 weeks
```

**Parallel Tracks**:
- **Neo4j**: Can run parallel to PostgreSQL Phase 3-4 (saves 1 week)
- **MongoDB**: Can run parallel to PostgreSQL Phase 4 and Neo4j Phase 3-4 (saves 1 week)

**Optimized Timeline**: 5-6 weeks (with effective parallelization)

### Dependency Matrix

| Component | Depends On | Can Proceed In Parallel With |
|-----------|-----------|------------------------------|
| **PostgreSQL Phase 1** | None | Neo4j Phase 1, MongoDB Phase 1 |
| **PostgreSQL Phase 2** | PostgreSQL Phase 1 | Neo4j Phase 1-2, MongoDB Phase 1 |
| **PostgreSQL Phase 3** | PostgreSQL Phase 2 | Neo4j Phase 2-3, MongoDB Phase 1-2 |
| **PostgreSQL Phase 4** | PostgreSQL Phase 3 | Neo4j Phase 3-4, MongoDB Phase 2-3 |
| **Neo4j Phase 1** | None | PostgreSQL Phase 1-2, MongoDB Phase 1 |
| **Neo4j Phase 2** | Neo4j Phase 1 | PostgreSQL Phase 3, MongoDB Phase 1-2 |
| **Neo4j Phase 3** | Neo4j Phase 2 | PostgreSQL Phase 4, MongoDB Phase 2-3 |
| **Neo4j Phase 4** | Neo4j Phase 3 | MongoDB Phase 3 |
| **MongoDB Phase 1** | None | All PostgreSQL, All Neo4j |
| **MongoDB Phase 2** | MongoDB Phase 1 | PostgreSQL Phase 3-4, Neo4j Phase 2-4 |
| **MongoDB Phase 3** | MongoDB Phase 2 | Neo4j Phase 4 |
| **MongoDB Phase 4** | MongoDB Phase 3 | None (near end) |
| **Integration Testing** | All Phase 4s complete | None |
| **Final Cleanup** | Integration Testing | None |

### Integration Milestones

#### Milestone 1: PostgreSQL Bug Fixed (End of Week 1)
**Criteria**:
- [ ] ToolContext.update_job_status() works with asyncpg
- [ ] All async patterns consistent
- [ ] No sync cursor usage with async connection pool

**Verification**:
```bash
pytest tests/test_postgres_db.py -v
pytest tests/test_tool_context.py -v
```

**Risk if Missed**: Agent crashes during job status updates (HIGH PRIORITY)

---

#### Milestone 2: All New Modules Functional (End of Week 3)
**Criteria**:
- [ ] PostgresDB fully operational (async)
- [ ] Neo4jDB fully operational (sync)
- [ ] MongoDBDB structure complete
- [ ] Unit tests passing for all three

**Verification**:
```bash
pytest tests/test_postgres_db.py tests/test_neo4j_db.py tests/test_mongodb_db.py
```

**Risk if Missed**: Integration testing delayed

---

#### Milestone 3: Agent Workflows Migrated (End of Week 4)
**Criteria**:
- [ ] Creator agent uses postgres_db for storage
- [ ] Validator agent uses neo4j_db for graph operations
- [ ] Both agents use mongodb_db for audit trails
- [ ] Tools fully migrated (cache_tools, graph_tools)

**Verification**:
```bash
python agent.py --config creator --document-path test.pdf --prompt "Extract requirements"
python agent.py --config validator --job-id <test-job-id>
```

**Risk if Missed**: System not functional end-to-end

---

#### Milestone 4: All Components Migrated (End of Week 5)
**Criteria**:
- [ ] Dashboard uses postgres_db
- [ ] All scripts use new modules
- [ ] Citation tool integrated
- [ ] MongoDB fully migrated
- [ ] Integration tests passing

**Verification**:
```bash
pytest tests/integration/ -v
streamlit run dashboard/app.py  # Manual verification
```

**Risk if Missed**: Production deployment blocked

---

#### Milestone 5: Production Ready (End of Week 6)
**Criteria**:
- [ ] Old code removed
- [ ] Documentation complete
- [ ] Performance benchmarks acceptable
- [ ] Rollback plan ready
- [ ] Clean room test passed

**Verification**:
```bash
# Fresh checkout
git clone <repo> /tmp/graphrag-clean
cd /tmp/graphrag-clean
docker-compose up -d
python scripts/app_init.py --force-reset --seed
python agent.py --config creator --prompt "test"
```

**Risk if Missed**: Production issues, customer impact

---

### Risk Management

#### High Priority Risks

**Risk 1: PostgreSQL Bug Causes Runtime Failures**
- **Probability**: High (bug confirmed in ToolContext)
- **Impact**: Critical (agent crashes)
- **Mitigation**: Fix in Week 1 (Phase 1-2), extensive testing
- **Contingency**: Temporary sync wrapper if async fix complex
- **Owner**: PostgreSQL migration lead

**Risk 2: Tool Migration Breaks Agent Functionality**
- **Probability**: Medium
- **Impact**: High (agent unusable)
- **Mitigation**: Incremental migration, rollback capability, extensive testing
- **Contingency**: Keep old code functional until verified
- **Owner**: Core team

**Risk 3: Connection Pool Exhaustion Under Load**
- **Probability**: Low
- **Impact**: Medium (performance degradation)
- **Mitigation**: Load testing in Week 5, configurable pool sizes
- **Contingency**: Increase pool size, add monitoring
- **Owner**: Performance team

#### Medium Priority Risks

**Risk 4: Namespace Pattern Adoption Confusion**
- **Probability**: Medium
- **Impact**: Low (developer experience)
- **Mitigation**: Clear documentation, code examples, migration guide
- **Contingency**: Provide backward-compatible wrappers
- **Owner**: Documentation team

**Risk 5: MongoDB Optional Behavior Issues**
- **Probability**: Low
- **Impact**: Low (logging only)
- **Mitigation**: Test with MongoDB disabled, graceful degradation
- **Contingency**: Log warnings, continue without audit trail
- **Owner**: MongoDB migration lead

**Risk 6: Integration Issues Between Databases**
- **Probability**: Medium
- **Impact**: Medium (coordination failures)
- **Mitigation**: Integration testing in Week 5, cross-database tests
- **Contingency**: Debug logs, transaction replay
- **Owner**: Integration team

#### Low Priority Risks

**Risk 7: Performance Regression**
- **Probability**: Low
- **Impact**: Medium
- **Mitigation**: Benchmark before/after, load testing
- **Contingency**: Optimize queries, tune connection pools
- **Owner**: Performance team

**Risk 8: Breaking Changes for External Users**
- **Probability**: Medium (if any external users)
- **Impact**: Low (internal system)
- **Mitigation**: Deprecation warnings, migration guide
- **Contingency**: Support old API for one release
- **Owner**: Product team

---

### Team Assignments

| Component | Lead | Support | Reviewer |
|-----------|------|---------|----------|
| **PostgreSQL Migration** | Backend Lead | DevOps | Senior Architect |
| **Neo4j Migration** | Graph Specialist | Backend Lead | Senior Architect |
| **MongoDB Migration** | Backend Lead | DevOps | Graph Specialist |
| **Integration Testing** | QA Lead | All Leads | Senior Architect |
| **Documentation** | Tech Writer | All Leads | Product Manager |
| **Production Deployment** | DevOps | Backend Lead | CTO |

---

### Communication Plan

#### Weekly Sync (Fridays)
- **Attendees**: All leads, architect, product manager
- **Duration**: 1 hour
- **Agenda**:
  - Progress review (each migration)
  - Blocker resolution
  - Risk assessment
  - Next week planning
- **Deliverable**: Status report, updated timeline

#### Daily Standups (Async)
- **Format**: Slack thread
- **Questions**:
  - What did you complete yesterday?
  - What are you working on today?
  - Any blockers?
- **Time**: 9am daily

#### Critical Issue Escalation
- **Channel**: #db-migration-urgent (Slack)
- **Response Time**: <2 hours
- **Escalation Path**: Lead → Architect → CTO

---

### Rollback Strategy

#### Per-Migration Rollback

**PostgreSQL Rollback**:
```python
# Keep old postgres_utils.py functional
# Revert imports in affected files
# Switch DATABASE_URL if schema changed
```
**Trigger**: Runtime errors, performance degradation >20%

**Neo4j Rollback**:
```python
# Keep old neo4j_utils.py functional
# Revert graph tools imports
# No schema changes (backward compatible)
```
**Trigger**: Graph query failures, data inconsistencies

**MongoDB Rollback**:
```python
# Keep old archiver.py functional
# Revert graph.py audit points
# MongoDB is optional - can disable entirely
```
**Trigger**: Audit logging failures (non-critical)

#### Full System Rollback
**Trigger**: Multiple integration test failures, production incidents

**Procedure**:
1. Revert all imports to old modules
2. Restore old database connection code
3. Restart all agents and services
4. Verify health checks pass
5. Post-mortem and revised timeline

**Estimated Time**: 2-4 hours

---

### Success Metrics

#### Performance Metrics
- **Connection Pool Utilization**: <80% under normal load
- **Query Latency**: No regression (within 10% of baseline)
- **Memory Usage**: Stable (no leaks over 24hr run)
- **Error Rate**: <0.1% database operation failures

#### Quality Metrics
- **Test Coverage**: >90% for all database modules
- **Integration Test Pass Rate**: 100%
- **Documentation Completeness**: All public APIs documented
- **Code Review Approval**: 100% of PRs reviewed and approved

#### Timeline Metrics
- **On-Time Delivery**: Each phase completed within 1 week of target
- **Blocker Resolution Time**: <24 hours average
- **Milestone Achievement**: All 5 milestones met

#### Adoption Metrics
- **Code Migration**: 100% of old utility usage replaced
- **Deprecation Warnings**: 0 in production logs after Week 6
- **External Dependencies**: citation_tool integrated

---

### Post-Migration Review

**Schedule**: Week 7 (after production deployment)

**Agenda**:
1. **What Went Well**: Successes, wins, good decisions
2. **What Could Improve**: Challenges, blockers, delays
3. **Lessons Learned**: Technical insights, process improvements
4. **Action Items**: Future improvements, technical debt

**Deliverables**:
- Post-mortem document
- Updated migration playbook
- Technical debt backlog
- Knowledge base articles

---

### Quick Reference

**Key Documents**:
- This document: Overall architecture and unified roadmap
- [postgres_migration_deep_dive.md](postgres_migration_deep_dive.md): PostgreSQL details
- [neo4j_migration_deep_dive.md](neo4j_migration_deep_dive.md): Neo4j details
- [mongodb_migration_deep_dive.md](mongodb_migration_deep_dive.md): MongoDB details

**Key Commands**:
```bash
# Run all database tests
pytest tests/test_postgres_db.py tests/test_neo4j_db.py tests/test_mongodb_db.py -v

# Run integration tests
pytest tests/integration/ -m "database" -v

# Initialize all databases
python scripts/app_init.py --force-reset --seed

# Test agent workflows
python agent.py --config creator --prompt "test"
python agent.py --config validator --job-id <uuid>

# Run dashboard
streamlit run dashboard/app.py
```

**Key Contacts**:
- PostgreSQL Lead: [Name]
- Neo4j Lead: [Name]
- MongoDB Lead: [Name]
- Integration Lead: [Name]
- Architect: [Name]

---

## Implementation Guide

### Step 1: Set Up Module Structure

Create the new directory structure (Phase 1):

```bash
# Create query directories
mkdir -p src/database/queries/postgres
mkdir -p src/database/queries/neo4j

# Create new database class files
touch src/database/postgres_db.py
touch src/database/neo4j_db.py
touch src/database/mongo_db.py

# Optional: Create tables.py for SQLAlchemy Core definitions
touch src/database/tables.py

# Move existing schema files (optional - can keep in place initially)
# mv data/metamodell.cql src/database/queries/neo4j/
# mv data/seed_data.cypher src/database/queries/neo4j/

# Move existing PostgreSQL schema
mv src/database/schema.sql src/database/queries/postgres/schema.sql

# Create complex query files
touch src/database/queries/postgres/complex.sql
```

**Note**: During Phase 1, keep existing files (`postgres_utils.py`, `neo4j_utils.py`) to maintain backward compatibility.

### Step 2: Define Configuration

The project already uses environment variables from `.env`. Ensure these are defined:

**In `.env` file:**
```bash
# PostgreSQL (required)
DATABASE_URL=postgresql://graphrag:graphrag_password@localhost:5432/graphrag
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=graphrag
POSTGRES_PASSWORD=graphrag_password
POSTGRES_DB=graphrag

# Neo4j (required for graph operations)
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=neo4j_password

# MongoDB (optional, for logging)
MONGODB_URL=mongodb://localhost:27017/graphrag_logs

# Connection pool settings (optional, defaults provided)
POSTGRES_MIN_CONNECTIONS=2
POSTGRES_MAX_CONNECTIONS=10
```

**Optional: Create `src/database/config.py` for centralized configuration:**
```python
"""Database configuration for Graph-RAG system."""
import os
from typing import Optional
from dataclasses import dataclass

@dataclass
class PostgresConfig:
    """PostgreSQL connection configuration."""
    database_url: Optional[str] = None
    host: str = "localhost"
    port: int = 5432
    user: str = "graphrag"
    password: str = "graphrag_password"
    database: str = "graphrag"
    min_connections: int = 2
    max_connections: int = 10
    command_timeout: int = 60

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        """Load configuration from environment variables."""
        return cls(
            database_url=os.getenv("DATABASE_URL"),
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "graphrag"),
            password=os.getenv("POSTGRES_PASSWORD", "graphrag_password"),
            database=os.getenv("POSTGRES_DB", "graphrag"),
            min_connections=int(os.getenv("POSTGRES_MIN_CONNECTIONS", "2")),
            max_connections=int(os.getenv("POSTGRES_MAX_CONNECTIONS", "10")),
        )

@dataclass
class Neo4jConfig:
    """Neo4j connection configuration."""
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = "neo4j_password"

    @classmethod
    def from_env(cls) -> "Neo4jConfig":
        """Load configuration from environment variables."""
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            username=os.getenv("NEO4J_USERNAME", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "neo4j_password"),
        )

@dataclass
class MongoConfig:
    """MongoDB connection configuration."""
    url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "MongoConfig":
        """Load configuration from environment variables."""
        return cls(url=os.getenv("MONGODB_URL"))
```

### Step 3: Define Tables

In `backend/database/tables.py`:
```python
from sqlalchemy import (
    MetaData, Table, Column, Integer, Text,
    DateTime, ForeignKey, Index, func
)

metadata = MetaData()

users = Table(
    'users',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('email', Text, unique=True, nullable=False),
    Column('name', Text),
    Column('created_at', DateTime, server_default=func.now()),
)

# Add more tables...
```

### Step 4: Implement Database Manager

In `backend/database/db.py`:

1. Import dependencies
2. Implement `__init__` with connection pooling
3. Implement `connection()` context manager
4. Implement `_load_query()` for named queries
5. Implement `_row_to_dict()` helper
6. Implement CRUD methods for each table

See [Code Examples](#code-examples) section for full implementation.

### Step 5: Create Module Interface

In `backend/database/__init__.py`:
```python
from .db import Database

db = Database()

__all__ = ['Database', 'db']
```

### Step 6: Write SQL Queries

In `queries/schema.sql`, define your DDL:
```sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Add indexes, triggers, etc.
```

In `queries/complex.sql`, define named queries:
```sql
-- name: get_user_with_profile
SELECT u.*, p.*
FROM users u
LEFT JOIN profiles p ON u.id = p.user_id
WHERE u.id = :user_id;
```

### Step 7: Create Initialization Script

In `backend/database/db_init.py`:
```python
import sys
from pathlib import Path
from sqlalchemy import text
from backend.database import Database

QUERIES_DIR = Path(__file__).parent / "queries"

def init_database():
    """Initialize database from schema.sql."""
    db = Database()
    schema_sql = (QUERIES_DIR / "schema.sql").read_text()

    with db.connection() as conn:
        # Split by semicolons and execute
        for statement in schema_sql.split(';'):
            if statement.strip():
                conn.execute(text(statement))

    print("Database initialized successfully")

if __name__ == '__main__':
    init_database()
```

### Step 8: Use in Application

In your FastAPI routes:
```python
from backend.database import db

@app.get("/users/{user_id}")
def get_user(user_id: int):
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
```

---

## Code Examples

### Complete Database Class Template

```python
"""PostgreSQL Database Manager."""
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Generator

from sqlalchemy import create_engine, text, select, insert, update, delete
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.pool import QueuePool

from backend.config import (
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
    POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_MIN_CONNECTIONS, POSTGRES_MAX_CONNECTIONS,
)
from backend.database.tables import metadata, users, messages

log = logging.getLogger(__name__)
QUERIES_DIR = Path(__file__).parent / "queries"


class Database:
    """PostgreSQL database manager with connection pooling."""

    def __init__(
        self,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None,
        min_connections: int = None,
        max_connections: int = None,
    ):
        """Initialize database connection pool."""
        # Configuration with fallback to config module
        self._host = host or POSTGRES_HOST
        self._port = port or POSTGRES_PORT
        self._database = database or POSTGRES_DB
        self._user = user or POSTGRES_USER
        self._password = password or POSTGRES_PASSWORD
        self._min_connections = min_connections or POSTGRES_MIN_CONNECTIONS
        self._max_connections = max_connections or POSTGRES_MAX_CONNECTIONS

        # Build connection URL
        url = f"postgresql://{self._user}:{self._password}@{self._host}:{self._port}/{self._database}"

        # Create engine with connection pooling
        self.engine: Engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=self._min_connections,
            max_overflow=self._max_connections - self._min_connections,
            pool_pre_ping=True,
            echo=False,
        )

        # Cache for loaded queries
        self._queries: dict[str, str] = {}

        log.info(f"Database connection pool initialized: {self._host}:{self._port}/{self._database}")

    @contextmanager
    def connection(self) -> Generator[Connection, None, None]:
        """Context manager for database connections with auto commit/rollback."""
        conn = self.engine.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close_all(self) -> None:
        """Close all connections in the pool."""
        self.engine.dispose()
        log.info("Database connection pool closed")

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load a named query from a .sql file."""
        cache_key = f"{filename}:{query_name}"
        if cache_key in self._queries:
            return self._queries[cache_key]

        file_path = QUERIES_DIR / filename
        content = file_path.read_text()

        # Parse named queries: -- name: query_name
        pattern = r"--\s*name:\s*(\w+)\s*\n(.*?)(?=--\s*name:|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        for name, sql in matches:
            self._queries[f"{filename}:{name}"] = sql.strip()

        if cache_key not in self._queries:
            raise ValueError(f"Query '{query_name}' not found in {filename}")

        return self._queries[cache_key]

    def _row_to_dict(self, row) -> Optional[dict]:
        """Convert SQLAlchemy row to dictionary."""
        if row is None:
            return None
        return dict(row._mapping)

    # ==================== USER CRUD ====================

    def create_user(self, email: str, name: str = None) -> dict:
        """Create a new user."""
        with self.connection() as conn:
            stmt = insert(users).values(email=email, name=name).returning(users)
            result = conn.execute(stmt).fetchone()
            return self._row_to_dict(result)

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """Get user by ID."""
        with self.connection() as conn:
            stmt = select(users).where(users.c.id == user_id)
            result = conn.execute(stmt).fetchone()
            return self._row_to_dict(result)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        """Get user by email (case-insensitive)."""
        with self.connection() as conn:
            stmt = select(users).where(users.c.email.ilike(email))
            result = conn.execute(stmt).fetchone()
            return self._row_to_dict(result)

    def update_user(self, user_id: int, name: str) -> Optional[dict]:
        """Update user name."""
        with self.connection() as conn:
            stmt = (
                update(users)
                .where(users.c.id == user_id)
                .values(name=name)
                .returning(users)
            )
            result = conn.execute(stmt).fetchone()
            return self._row_to_dict(result)

    def delete_user(self, user_id: int) -> bool:
        """Delete user."""
        with self.connection() as conn:
            stmt = delete(users).where(users.c.id == user_id)
            result = conn.execute(stmt)
            return result.rowcount > 0

    # ==================== COMPLEX QUERIES ====================

    def get_user_with_stats(self, user_id: int) -> Optional[dict]:
        """Get user with message count using named query."""
        with self.connection() as conn:
            query = self._load_query("complex.sql", "get_user_with_stats")
            result = conn.execute(text(query), {"user_id": user_id})
            return self._row_to_dict(result.fetchone())
```

### Example: Pagination with Named Queries

**In `queries/complex.sql`:**
```sql
-- name: get_messages_before_timestamp
SELECT id, "conversationId", "roleName", content, time, type
FROM messages
WHERE "conversationId" = :conversation_id
  AND time < :before_timestamp
ORDER BY time DESC
LIMIT :limit;

-- name: get_messages_after_timestamp
SELECT id, "conversationId", "roleName", content, time, type
FROM messages
WHERE "conversationId" = :conversation_id
  AND time > :after_timestamp
ORDER BY time ASC;
```

**In `db.py`:**
```python
def get_messages_paginated(
    self,
    conversation_id: str,
    before_timestamp: int = None,
    after_timestamp: int = None,
    limit: int = 30
) -> list[dict]:
    """Get messages with pagination support."""
    with self.connection() as conn:
        if before_timestamp is not None:
            # Going back in time (pagination)
            query = self._load_query("complex.sql", "get_messages_before_timestamp")
            results = conn.execute(
                text(query),
                {
                    "conversation_id": conversation_id,
                    "before_timestamp": before_timestamp,
                    "limit": limit
                }
            ).fetchall()
        elif after_timestamp is not None:
            # Going forward (incremental sync)
            query = self._load_query("complex.sql", "get_messages_after_timestamp")
            results = conn.execute(
                text(query),
                {
                    "conversation_id": conversation_id,
                    "after_timestamp": after_timestamp
                }
            ).fetchall()
        else:
            # Default: most recent messages
            stmt = (
                select(messages)
                .where(messages.c.conversationId == conversation_id)
                .order_by(messages.c.time.desc())
                .limit(limit)
            )
            results = conn.execute(stmt).fetchall()

        return [self._row_to_dict(row) for row in results]
```

---

## Best Practices

### 1. **Always Use Context Managers**

✅ **Good**:
```python
with db.connection() as conn:
    result = conn.execute(select(users))
```

❌ **Bad**:
```python
conn = db.engine.connect()
result = conn.execute(select(users))
conn.close()  # Easy to forget, connection leak
```

### 2. **Use Named Queries for Complex SQL**

✅ **Good**:
```python
# In complex.sql
-- name: get_user_conversations
SELECT c.*, COUNT(m.id) as message_count
FROM conversations c
LEFT JOIN messages m ON c.id = m."conversationId"
WHERE c."userId" = :user_id
GROUP BY c.id;

# In db.py
query = self._load_query("complex.sql", "get_user_conversations")
result = conn.execute(text(query), {"user_id": user_id})
```

❌ **Bad**:
```python
query = """
SELECT c.*, COUNT(m.id) as message_count
FROM conversations c
LEFT JOIN messages m ON c.id = m."conversationId"
WHERE c."userId" = %s
GROUP BY c.id;
""" % user_id  # SQL injection risk!
```

### 3. **Use `returning()` for Insert/Update**

✅ **Good**:
```python
stmt = insert(users).values(email=email).returning(users)
result = conn.execute(stmt).fetchone()
return self._row_to_dict(result)  # Returns created user with ID
```

❌ **Bad**:
```python
stmt = insert(users).values(email=email)
conn.execute(stmt)
# Now you need another query to get the created user...
```

### 4. **Use Optimistic Locking for Updates**

✅ **Good**:
```python
stmt = (
    update(messages)
    .where(
        (messages.c.id == message_id) &
        (messages.c.version == current_version)  # Prevents race conditions
    )
    .values(content=new_content, version=current_version + 1)
    .returning(messages)
)
result = conn.execute(stmt).fetchone()
if result is None:
    raise ConflictError("Message was modified by another client")
```

### 5. **Use Prepared Statements (Named Parameters)**

✅ **Good**:
```python
result = conn.execute(
    text("SELECT * FROM users WHERE email = :email"),
    {"email": user_email}
)
```

❌ **Bad**:
```python
result = conn.execute(
    text(f"SELECT * FROM users WHERE email = '{user_email}'")
)  # SQL injection vulnerability!
```

### 6. **Define Indexes in tables.py**

✅ **Good**:
```python
from sqlalchemy import Index

idx_messages_time = Index('idx_messages_time', messages.c.conversationId, messages.c.time)

# Partial index (PostgreSQL)
idx_agent_messages = Index(
    'idx_agent_messages',
    messages.c.agent_status,
    postgresql_where=(messages.c.type == 'agent')
)
```

### 7. **Use JSONB for Semi-Structured Data**

✅ **Good**:
```python
from sqlalchemy.dialects.postgresql import JSONB

Column('metadata', JSONB)  # Queryable, indexable
```

❌ **Bad**:
```python
Column('metadata', Text)  # Must deserialize in Python, not queryable
```

### 8. **Separate DDL from Application Code**

✅ **Good**:
- DDL in `queries/schema.sql`
- Initialization script (`db_init.py`) executes DDL
- Application creates database instances and injects them

❌ **Bad**:
- `metadata.create_all(engine)` in application startup
- Mixing table creation with business logic

### 9. **Log Database Operations**

```python
import logging
log = logging.getLogger(__name__)

def create_user(self, email: str) -> dict:
    log.debug(f"Creating user: {email}")
    with self.connection() as conn:
        # ...
    log.info(f"User created: {email}")
```

### 10. **Handle Connection Errors Gracefully**

```python
from sqlalchemy.exc import OperationalError, IntegrityError

def create_user(self, email: str) -> dict:
    try:
        with self.connection() as conn:
            stmt = insert(users).values(email=email).returning(users)
            result = conn.execute(stmt).fetchone()
            return self._row_to_dict(result)
    except IntegrityError:
        log.warning(f"User already exists: {email}")
        raise DuplicateUserError(f"User with email {email} already exists")
    except OperationalError as e:
        log.error(f"Database connection error: {e}")
        raise DatabaseUnavailableError("Database is temporarily unavailable")
```

---

## Migration Strategy (Bonus)

### Production-Safe Migrations

For production deployments, use an **additive-only migration** approach:

```python
# db_init.py
def migrate_schema_production(engine):
    """Add missing columns/tables without deleting data."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    # Expected schema from tables.py
    expected_tables = {
        'users': ['id', 'email', 'name', 'created_at'],
        'messages': ['id', 'conversationId', 'content', 'time', 'version'],
    }

    with engine.connect() as conn:
        for table_name, expected_cols in expected_tables.items():
            if table_name not in existing_tables:
                log.info(f"Table '{table_name}' will be created")
                # Execute CREATE TABLE from schema.sql
                continue

            # Check for missing columns
            existing_cols = [col['name'] for col in inspector.get_columns(table_name)]
            missing_cols = set(expected_cols) - set(existing_cols)

            for col in missing_cols:
                # Parse column definition from schema.sql
                col_def = get_column_definition(table_name, col)
                alter_sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS {col_def}'
                conn.execute(text(alter_sql))
                log.info(f"Added column '{col}' to '{table_name}'")

        conn.commit()
```

**Key Principles**:
1. **Never DROP** tables or columns in production
2. **Only ADD** missing columns/tables
3. **Warn** about schema drift (unexpected columns)
4. **Log** all changes for audit trail

---

## Summary

This refactored database architecture for Graph-RAG provides:

1. **Multi-Database Support**: Unified patterns for PostgreSQL, Neo4j, and MongoDB
2. **Clean Separation of Concerns**: Schema, queries, and logic are separated
3. **Connection Pooling**: Efficient resource management (asyncpg for PostgreSQL)
4. **Context Managers**: Safe connection lifecycle with auto-commit/rollback
5. **Named Queries**: Complex SQL/Cypher separated from Python code
6. **Async/Sync Clarity**: Async for PostgreSQL, sync for Neo4j/MongoDB
7. **Configuration Injection**: Easy testing and multi-environment support
8. **CRUD Patterns**: Consistent, predictable data access methods
9. **Production Safety**: Connection validation, retry logic, error handling
10. **Optional Logging**: MongoDB gracefully degrades if unavailable

### Key Files (After Refactoring)

**Database Module (`src/database/`):**
- `__init__.py` - Module interface exporting classes (dependency injection pattern)
- `postgres_db.py` - PostgreSQL manager with async pooling (asyncpg)
- `neo4j_db.py` - Neo4j manager with session management (neo4j driver)
- `mongo_db.py` - MongoDB manager with lazy connection (pymongo)
- `config.py` - Centralized database configuration (optional)
- `tables.py` - SQLAlchemy Core table definitions (optional alternative to DDL)

**Query Files (`src/database/queries/`):**
- `postgres/schema.sql` - PostgreSQL DDL (jobs, requirements tables)
- `postgres/complex.sql` - Named SQL queries
- `neo4j/metamodell.cql` - Neo4j schema (constraints, indexes)
- `neo4j/seed_data.cypher` - Neo4j seed data

**Usage in Application:**
- `src/agent.py` - UniversalAgent creates database instances and injects them
- `src/tools/context.py` - ToolContext receives injected database connections
- `src/tools/cache_tools.py` - Requirement CRUD using injected PostgresDB instance
- `src/tools/graph_tools.py` - Graph operations using injected Neo4jDB instance
- `src/core/archiver.py` - LLM logging using injected MongoDB instance
- `dashboard/db.py` - Dashboard creates its own PostgresDB instance
- `scripts/init_db.py` - Creates PostgresDB instance for initialization
- `scripts/init_neo4j.py` - Creates Neo4jDB instance for initialization

### Database Roles

| Database | Purpose | Key Tables/Collections | Access Pattern |
|----------|---------|------------------------|----------------|
| **PostgreSQL** | Job tracking, requirement storage, citations | `jobs`, `requirements` | Async (asyncpg pool) |
| **Neo4j** | FINIUS knowledge graph, requirement relationships | `Requirement`, `BusinessObject`, `Message` nodes | Sync (session-based) |
| **MongoDB** | LLM request logging, agent audit trail | `llm_requests`, `agent_audit` | Lazy (optional) |

### Migration Path

1. ✅ **Phase 1**: Create new classes alongside existing code (non-breaking) - **COMPLETE**
2. ✅ **Phase 2**: Update core components (UniversalAgent, ToolContext, tools) - **COMPLETE**
3. ⏳ **Phase 3**: Update dashboard and scripts
4. ⏳ **Phase 4**: Deprecate old utility functions
5. ⏳ **Phase 5**: Remove old code after testing

**Note:** All database classes (PostgreSQL, Neo4j, MongoDB) use dependency injection. Each component creates and manages its own database instances.

### Benefits of This Architecture

**For Developers:**
- Single import: `from src.database import PostgresDB, Neo4jDB, MongoDB`
- Consistent patterns across all three databases
- Easy to mock for unit tests (dependency injection)
- Clear separation of schema and logic

**For Operations:**
- Connection pooling reduces database load
- Health checks and retry logic improve reliability
- Centralized configuration simplifies deployment
- Audit logging with MongoDB for debugging

**For Maintenance:**
- Schema changes in DDL/CQL files, not Python code
- Named queries are reviewable and versionable
- Modular design allows independent updates
- Backward compatibility during migration

### Reference Implementation

This architecture is adapted from the Advanced-LLM-Chat backend (`/home/ghost/Repositories/Advanced-LLM-Chat/backend/database/`) and extended to support multiple databases (PostgreSQL, Neo4j, MongoDB) for the Graph-RAG requirement traceability system.

**Current State Analysis:**
- See exploration report from Task agent ID: `a632da0`
- Current files: `src/database/postgres_utils.py` (496 lines), `neo4j_utils.py` (161 lines)
- MongoDB logic in: `src/core/archiver.py` (720 lines)
