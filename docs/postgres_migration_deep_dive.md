# PostgreSQL Migration Deep Dive

**Document Status:** ✅ **COMPLETE** (Phase 1/2 implemented)
**Last Updated:** 2026-01-14
**Related:** `docs/db_refactor.md`, `docs/phase1_complete.md`

## Executive Summary

This document provides a detailed analysis and implementation plan for migrating PostgreSQL database access from the old `postgres_utils.py` module-level functions to a clean `PostgresDB` class-based architecture with **dependency injection**.

**Migration Status:** ✅ **COMPLETE**
- ✅ Phase 1: `PostgresDB` class created with namespace pattern
- ✅ Phase 2: UniversalAgent, tools (cache_tools), and entry point migrated
- ✅ Critical bug in ToolContext.update_job_status() **FIXED** (was mixing sync/async patterns)

**Key Design Decision:** No singleton pattern. Each component creates its own `PostgresDB` instance and passes it via dependency injection. This approach works because PostgreSQL with asyncpg has built-in connection pooling that handles concurrency correctly. Singletons were only needed for SQLite to avoid write conflicts.

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Critical Bug: ToolContext Async/Sync Mismatch](#2-critical-bug-toolcontext-asyncsync-mismatch)
3. [Target Architecture](#3-target-architecture)
4. [API Design](#4-api-design)
5. [Query Organization](#5-query-organization)
6. [Parameter Objects](#6-parameter-objects)
7. [Transaction Support](#7-transaction-support)
8. [Migration Strategy](#8-migration-strategy)
9. [Testing Strategy](#9-testing-strategy)
10. [Implementation Checklist](#10-implementation-checklist)

---

## 1. Current State Analysis

### 1.1 File Structure

```
src/database/
├── __init__.py
├── postgres_utils.py        496 lines
└── schema.sql               181 lines

Current schema defines:
├── jobs table               (job tracking)
├── requirements table       (extracted requirements)
├── job_summary view         (aggregated stats)
└── triggers                 (updated_at automation)
```

### 1.2 PostgresConnection Class

**Location:** `src/database/postgres_utils.py:27-167`

**Features:**
- Async connection pool using `asyncpg`
- Pool configuration: min=2, max=10, timeout=60s
- Context manager for connection acquisition
- Basic query methods: `execute()`, `fetch()`, `fetchrow()`, `fetchval()`
- Connection lifecycle: `connect()`, `disconnect()`, `is_connected`

**Strengths:**
- ✅ Proper async/await pattern
- ✅ Connection pooling
- ✅ Context manager for safety
- ✅ Simple, focused API

**Weaknesses:**
- ❌ No transaction support
- ❌ No health checks or ping
- ❌ No connection validation
- ❌ No retry logic
- ❌ SQL statements inline (not organized)
- ❌ No namespace organization for operations

### 1.3 Module-Level Functions

**Job Functions** (lines 174-276):
```python
async def create_job(conn: PostgresConnection, prompt: str, ...) -> uuid.UUID
async def get_job(conn: PostgresConnection, job_id: uuid.UUID) -> Optional[Dict]
async def update_job_status(conn: PostgresConnection, job_id: uuid.UUID, ...) -> None
```

**Requirement Functions** (lines 283-471):
```python
async def create_requirement(conn: PostgresConnection, job_id: uuid.UUID, text: str, ...) -> uuid.UUID  # 20 params!
async def get_pending_requirement(conn: PostgresConnection, job_id: Optional[uuid.UUID]) -> Optional[Dict]
async def update_requirement_status(conn: PostgresConnection, requirement_id: uuid.UUID, ...) -> None
async def count_requirements_by_status(conn: PostgresConnection, job_id: uuid.UUID) -> Dict[str, int]
```

**Problems:**
1. **Large parameter lists** - `create_requirement` has 20 parameters (lines 283-301)
2. **Dynamic SQL building** - String interpolation in update functions (lines 243-274, 412-444)
3. **Mixed concerns** - Business logic mixed with data access
4. **No parameter validation** - Direct pass-through to database
5. **Inconsistent patterns** - Some use Record, some use Dict

### 1.4 Current Usage Locations

| File | Usage Pattern | Notes |
|------|---------------|-------|
| `src/agent.py:613` | `await postgres_conn.fetchrow(query, ...)` | Async - correct ✅ |
| `src/agent.py:619-645` | Internal `_update_job_status()` | Async - correct ✅ |
| `src/tools/context.py:224` | `cursor = postgres_conn.cursor()` | **BROKEN** ❌ Sync on async! |
| `dashboard/db.py:12-100` | psycopg2 with context manager | Sync - separate driver |

**Key Finding:** Agent and tools use **asyncpg** (async), Dashboard uses **psycopg2** (sync) - two different drivers!

---

## 2. Critical Bug: ToolContext Async/Sync Mismatch ✅ **FIXED**

### 2.1 Bug Location

**File:** `src/tools/context.py`
**Method:** `update_job_status()` (previously lines 192-259)
**Severity:** 🔴 **CRITICAL** - Runtime failure (now fixed)

### 2.2 The Problem (Historical)

The code was mixing sync (psycopg2) patterns with async (asyncpg) connection:

```python
# WRONG: Trying to use sync API on async connection
cursor = self.postgres_conn.cursor()  # postgres_conn is PostgresConnection (asyncpg)
cursor.execute(...)  # This method doesn't exist on asyncpg Pool
self.postgres_conn.commit()  # This method doesn't exist on asyncpg Pool
cursor.close()  # This method doesn't exist
```

**Root Cause:**
- `self.postgres_conn` wraps an `asyncpg.Pool`
- Code tried to use `.cursor()`, `.commit()`, `.close()` which are **psycopg2 methods**
- `asyncpg` uses a completely different async API

### 2.3 The Fix ✅

**Implemented:** Converted to proper async pattern (Phase 2 migration)

```python
async def update_job_status(
    self,
    status: str,
    completed_at: bool = False,
    error_message: Optional[str] = None,
) -> bool:
    """Update job status - ASYNC version (FIXED)."""
    if not self.job_id:
        raise ValueError("No job_id available for status update")

    if not self.has_postgres():
        return False

    try:
        # Use asyncpg's execute method (no cursor needed)
        if completed_at:
            if error_message:
                await self.postgres_conn.execute(
                    """
                    UPDATE jobs
                    SET status = $1, completed_at = NOW(), error_message = $2
                    WHERE id = $3::uuid
                    """,
                    status, error_message, self.job_id,
                )
            # ... (more cases)
        return True
    except Exception as e:
        logger.error(f"Failed to update job status: {e}")
        return False
```

**Changes Made:**
1. ✅ Added `async` keyword to method signature
2. ✅ Changed placeholders from `%s` to `$1, $2, $3` (asyncpg style)
3. ✅ Added `await` before all `.execute()` calls
4. ✅ Removed `.cursor()`, `.commit()`, `.close()` calls
5. ✅ Added `::uuid` type cast for UUID parameters
6. ✅ Updated all callers (`completion_tools.py`) to use `await`

---

## 3. Target Architecture

### 3.1 Class Design

```python
class PostgresDB:
    """PostgreSQL database manager with connection pooling.

    Provides high-level async API for job and requirement management.
    Uses asyncpg for connection pooling and query execution.

    Example:
        # Dependency injection pattern
        from src.database import PostgresDB

        postgres_db = PostgresDB()
        await postgres_db.connect()
        job_id = await postgres_db.jobs.create(prompt="Extract requirements")
        await postgres_db.close()

        # Or use context manager
        async with postgres_db:
            job = await postgres_db.jobs.get(job_id)
    """

    def __init__(self, database_url: Optional[str] = None, config: Optional[PostgresConfig] = None):
        """Initialize database manager."""

    # Core connection management
    async def connect(self) -> None:
        """Establish connection pool."""

    async def disconnect(self) -> None:
        """Close connection pool."""

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()

    def is_connected(self) -> bool:
        """Check if connected."""

    async def health_check(self) -> bool:
        """Verify database connectivity."""

    # Namespace objects for operations
    @property
    def jobs(self) -> JobOperations:
        """Job management operations."""

    @property
    def requirements(self) -> RequirementOperations:
        """Requirement management operations."""

    # Low-level query execution
    async def execute(self, query: str, *args) -> str:
        """Execute a query."""

    async def fetch(self, query: str, *args) -> List[Dict]:
        """Fetch multiple rows."""

    async def fetchrow(self, query: str, *args) -> Optional[Dict]:
        """Fetch a single row."""

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch a single value."""

    # Transaction support
    @asynccontextmanager
    async def transaction(self):
        """Execute operations in a transaction."""

    # Named query support
    async def execute_named(self, query_name: str, **params) -> Any:
        """Execute a named query from queries/postgres/."""
```

### 3.2 Namespace Classes

**JobOperations** - Encapsulates all job-related database operations:
```python
class JobOperations:
    """Job management database operations."""

    def __init__(self, db: PostgresDB):
        self._db = db

    async def create(self, data: CreateJobRequest) -> uuid.UUID:
        """Create a new job."""

    async def get(self, job_id: uuid.UUID) -> Optional[Job]:
        """Get job by ID."""

    async def update_status(self, job_id: uuid.UUID, update: UpdateJobStatusRequest) -> None:
        """Update job status."""

    async def list(self, filters: Optional[JobFilters] = None, limit: int = 50) -> List[Job]:
        """List jobs with optional filters."""

    async def get_summary(self, job_id: uuid.UUID) -> Optional[JobSummary]:
        """Get job summary with requirement counts."""
```

**RequirementOperations** - Encapsulates all requirement-related operations:
```python
class RequirementOperations:
    """Requirement management database operations."""

    def __init__(self, db: PostgresDB):
        self._db = db

    async def create(self, data: CreateRequirementRequest) -> uuid.UUID:
        """Create a new requirement."""

    async def get(self, requirement_id: uuid.UUID) -> Optional[Requirement]:
        """Get requirement by ID."""

    async def get_pending(self, job_id: Optional[uuid.UUID] = None) -> Optional[Requirement]:
        """Get and lock next pending requirement (SKIP LOCKED)."""

    async def update_status(self, requirement_id: uuid.UUID, update: UpdateRequirementStatusRequest) -> None:
        """Update requirement validation status."""

    async def count_by_status(self, job_id: uuid.UUID) -> Dict[str, int]:
        """Count requirements by status."""

    async def list(self, filters: Optional[RequirementFilters] = None, limit: int = 100) -> List[Requirement]:
        """List requirements with optional filters."""
```

### 3.3 Dependency Injection Pattern

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

**Usage (NEW - Dependency Injection):**
```python
# Create instances in component initialization
from src.database import PostgresDB

postgres_db = PostgresDB()
await postgres_db.connect()

# Use throughout component lifecycle
job_id = await postgres_db.jobs.create(CreateJobRequest(prompt="Extract"))
job = await postgres_db.jobs.get(job_id)

# Pass to other components via injection
context = ToolContext(postgres_conn=postgres_db)

# Cleanup
await postgres_db.close()
```

**Old pattern (for comparison):**
```python
from src.database.postgres_utils import create_postgres_connection, create_job
conn = create_postgres_connection()
await conn.connect()
job_id = await create_job(conn, prompt="Extract")
```

---

## 4. API Design

### 4.1 Request/Response Objects

Replace long parameter lists with dataclasses:

```python
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from datetime import datetime
import uuid

@dataclass
class CreateJobRequest:
    """Request to create a new job."""
    prompt: str
    document_path: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Job:
    """Job database record."""
    id: uuid.UUID
    prompt: str
    document_path: Optional[str]
    context: Dict[str, Any]
    status: str
    creator_status: Optional[str]
    validator_status: Optional[str]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    error_message: Optional[str]
    error_details: Optional[Dict]
    total_tokens_used: int
    total_requests: int

@dataclass
class UpdateJobStatusRequest:
    """Request to update job status."""
    status: Optional[str] = None
    creator_status: Optional[str] = None
    validator_status: Optional[str] = None
    error_message: Optional[str] = None
    completed_at: bool = False  # Set completed_at to NOW()

@dataclass
class CreateRequirementRequest:
    """Request to create a requirement."""
    job_id: uuid.UUID
    text: str
    name: Optional[str] = None
    req_type: Optional[str] = None
    priority: Optional[str] = None
    source_document: Optional[str] = None
    source_location: Optional[Dict] = None
    gobd_relevant: bool = False
    gdpr_relevant: bool = False
    citations: List[str] = field(default_factory=list)
    mentioned_objects: List[str] = field(default_factory=list)
    mentioned_messages: List[str] = field(default_factory=list)
    reasoning: Optional[str] = None
    research_notes: Optional[str] = None
    confidence: float = 0.0
    tags: List[str] = field(default_factory=list)

@dataclass
class Requirement:
    """Requirement database record."""
    id: uuid.UUID
    job_id: uuid.UUID
    requirement_id: Optional[str]
    text: str
    name: Optional[str]
    type: Optional[str]
    priority: Optional[str]
    source_document: Optional[str]
    source_location: Optional[Dict]
    gobd_relevant: bool
    gdpr_relevant: bool
    citations: List[str]
    mentioned_objects: List[str]
    mentioned_messages: List[str]
    reasoning: Optional[str]
    research_notes: Optional[str]
    confidence: float
    neo4j_id: Optional[str]
    validation_result: Optional[Dict]
    rejection_reason: Optional[str]
    status: str
    retry_count: int
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime
    validated_at: Optional[datetime]
    tags: List[str]

@dataclass
class UpdateRequirementStatusRequest:
    """Request to update requirement status."""
    status: str
    validation_result: Optional[Dict] = None
    neo4j_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    error: Optional[str] = None

@dataclass
class JobFilters:
    """Filters for listing jobs."""
    status: Optional[str] = None
    creator_status: Optional[str] = None
    validator_status: Optional[str] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None

@dataclass
class RequirementFilters:
    """Filters for listing requirements."""
    job_id: Optional[uuid.UUID] = None
    status: Optional[str] = None
    gobd_relevant: Optional[bool] = None
    gdpr_relevant: Optional[bool] = None
    has_neo4j_id: Optional[bool] = None

@dataclass
class JobSummary:
    """Job summary with requirement counts (from job_summary view)."""
    id: uuid.UUID
    status: str
    creator_status: Optional[str]
    validator_status: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
    pending_requirements: int
    validating_requirements: int
    integrated_requirements: int
    rejected_requirements: int
    failed_requirements: int
    total_tokens_used: int
    total_requests: int
```

### 4.2 Benefits of Parameter Objects

**Before:**
```python
await create_requirement(
    conn, job_id, text, name, req_type, priority, source_document,
    source_location, gobd_relevant, gdpr_relevant, citations,
    mentioned_objects, mentioned_messages, reasoning, research_notes,
    confidence, tags
)  # 17 positional parameters!
```

**After:**
```python
await postgres_db.requirements.create(
    CreateRequirementRequest(
        job_id=job_id,
        text="User must be able to log in",
        req_type="functional",
        priority="high",
        gobd_relevant=True,
        confidence=0.95
    )
)  # Clear, named parameters, IDE autocomplete
```

**Advantages:**
1. ✅ IDE autocomplete and type checking
2. ✅ Self-documenting code
3. ✅ Easy to add optional fields
4. ✅ Validation at construction time
5. ✅ Clear separation of concerns
6. ✅ Easier to test

---

## 5. Query Organization

### 5.1 Directory Structure

```
src/database/
├── __init__.py
├── postgres_db.py           # PostgresDB class
├── neo4j_db.py              # Neo4jDB class
├── mongo_db.py              # MongoDB class
├── models.py                # Request/Response dataclasses
├── config.py                # Config dataclasses
└── queries/
    ├── postgres/
    │   ├── schema.sql       # Table definitions (existing)
    │   ├── jobs.sql         # Named job queries
    │   └── requirements.sql # Named requirement queries
    └── neo4j/
        ├── metamodell.cql
        └── seed_data.cypher
```

### 5.2 Named Query Files

**queries/postgres/jobs.sql:**
```sql
-- name: create_job
-- description: Create a new job with default statuses
INSERT INTO jobs (prompt, document_path, context)
VALUES ($1, $2, $3)
RETURNING id;

-- name: get_job
-- description: Get job by ID
SELECT * FROM jobs WHERE id = $1;

-- name: update_job_status
-- description: Update job status fields
UPDATE jobs
SET
    status = COALESCE($2, status),
    creator_status = COALESCE($3, creator_status),
    validator_status = COALESCE($4, validator_status),
    error_message = COALESCE($5, error_message),
    completed_at = CASE WHEN $6 THEN NOW() ELSE completed_at END,
    updated_at = NOW()
WHERE id = $1;

-- name: list_jobs
-- description: List jobs with optional filters
SELECT * FROM jobs
WHERE
    ($2::varchar IS NULL OR status = $2)
    AND ($3::varchar IS NULL OR creator_status = $3)
    AND ($4::varchar IS NULL OR validator_status = $4)
ORDER BY created_at DESC
LIMIT $1;

-- name: get_job_summary
-- description: Get job summary from view
SELECT * FROM job_summary WHERE id = $1;
```

**queries/postgres/requirements.sql:**
```sql
-- name: create_requirement
-- description: Create a new requirement
INSERT INTO requirements (
    job_id, text, name, type, priority, source_document, source_location,
    gobd_relevant, gdpr_relevant, citations, mentioned_objects,
    mentioned_messages, reasoning, research_notes, confidence, tags
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
RETURNING id;

-- name: get_requirement
-- description: Get requirement by ID
SELECT * FROM requirements WHERE id = $1;

-- name: get_pending_requirement
-- description: Get and lock next pending requirement
SELECT * FROM requirements
WHERE status = 'pending'
    AND ($1::uuid IS NULL OR job_id = $1)
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- name: update_requirement_status
-- description: Update requirement validation status
UPDATE requirements
SET
    status = $2,
    validation_result = COALESCE($3, validation_result),
    neo4j_id = COALESCE($4, neo4j_id),
    rejection_reason = COALESCE($5, rejection_reason),
    last_error = COALESCE($6, last_error),
    retry_count = CASE WHEN $6 IS NOT NULL THEN retry_count + 1 ELSE retry_count END,
    validated_at = CASE WHEN $2 IN ('integrated', 'rejected') THEN NOW() ELSE validated_at END,
    updated_at = NOW()
WHERE id = $1;

-- name: count_requirements_by_status
-- description: Count requirements by status for a job
SELECT status, COUNT(*) as count
FROM requirements
WHERE job_id = $1
GROUP BY status;

-- name: list_requirements
-- description: List requirements with optional filters
SELECT * FROM requirements
WHERE
    ($2::uuid IS NULL OR job_id = $2)
    AND ($3::varchar IS NULL OR status = $3)
    AND ($4::boolean IS NULL OR gobd_relevant = $4)
    AND ($5::boolean IS NULL OR gdpr_relevant = $5)
    AND ($6::boolean IS NULL OR (CASE WHEN $6 THEN neo4j_id IS NOT NULL ELSE neo4j_id IS NULL END))
ORDER BY created_at DESC
LIMIT $1;
```

### 5.3 Query Loader

```python
# src/database/query_loader.py
from pathlib import Path
from typing import Dict
import re

class QueryLoader:
    """Load and parse named SQL queries from .sql files."""

    def __init__(self, queries_dir: Path):
        self.queries_dir = Path(queries_dir)
        self._queries: Dict[str, str] = {}
        self._load_queries()

    def _load_queries(self) -> None:
        """Load all queries from .sql files in queries directory."""
        for sql_file in self.queries_dir.glob("*.sql"):
            self._parse_file(sql_file)

    def _parse_file(self, filepath: Path) -> None:
        """Parse a SQL file into named queries.

        Expected format:
            -- name: query_name
            -- description: Query description
            SELECT ...;

            -- name: another_query
            ...
        """
        content = filepath.read_text()

        # Split on "-- name:" markers
        pattern = r'--\s*name:\s*(\w+)\s*\n--\s*description:\s*([^\n]+)\s*\n(.*?)(?=--\s*name:|$)'
        matches = re.finditer(pattern, content, re.DOTALL)

        for match in matches:
            name = match.group(1).strip()
            description = match.group(2).strip()
            query = match.group(3).strip()

            # Remove trailing semicolon
            query = query.rstrip(';').strip()

            self._queries[name] = query

    def get(self, name: str) -> str:
        """Get a named query.

        Args:
            name: Query name

        Returns:
            SQL query string

        Raises:
            KeyError: If query name not found
        """
        if name not in self._queries:
            raise KeyError(f"Query '{name}' not found. Available: {list(self._queries.keys())}")
        return self._queries[name]

    def list_queries(self) -> List[str]:
        """List all available query names."""
        return list(self._queries.keys())
```

**Usage in PostgresDB:**
```python
class PostgresDB:
    def __init__(self, ...):
        # ...
        queries_dir = Path(__file__).parent / "queries" / "postgres"
        self._query_loader = QueryLoader(queries_dir)

    async def execute_named(self, query_name: str, *args) -> Any:
        """Execute a named query."""
        query = self._query_loader.get(query_name)
        return await self.execute(query, *args)
```

---

## 6. Parameter Objects

See section 4.1 for full dataclass definitions. Key files:

```python
# src/database/models.py - All request/response objects

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from datetime import datetime
import uuid

# Job models
@dataclass
class CreateJobRequest: ...
@dataclass
class Job: ...
@dataclass
class UpdateJobStatusRequest: ...
@dataclass
class JobFilters: ...
@dataclass
class JobSummary: ...

# Requirement models
@dataclass
class CreateRequirementRequest: ...
@dataclass
class Requirement: ...
@dataclass
class UpdateRequirementStatusRequest: ...
@dataclass
class RequirementFilters: ...
```

---

## 7. Transaction Support

### 7.1 Transaction Context Manager

```python
# In PostgresDB class

@asynccontextmanager
async def transaction(self):
    """Execute operations in a transaction.

    Example:
        async with postgres_db.transaction() as tx:
            job_id = await tx.jobs.create(CreateJobRequest(...))
            await tx.requirements.create(CreateRequirementRequest(...))
            # Commits automatically on success, rolls back on exception
    """
    if self._pool is None:
        raise RuntimeError("Not connected. Call connect() first.")

    async with self._pool.acquire() as conn:
        async with conn.transaction():
            # Create a temporary DB instance that uses this connection
            tx_db = PostgresDB.__new__(PostgresDB)
            tx_db._pool = None
            tx_db._conn = conn  # Use this specific connection
            tx_db._query_loader = self._query_loader

            # Create namespace objects that use the transaction connection
            tx_db._jobs = JobOperations(tx_db)
            tx_db._requirements = RequirementOperations(tx_db)

            yield tx_db
```

### 7.2 Usage Examples

**Atomic multi-operation:**
```python
async with postgres_db.transaction() as tx:
    # Create job
    job_id = await tx.jobs.create(CreateJobRequest(
        prompt="Extract requirements",
        document_path="/path/to/doc.pdf"
    ))

    # Create multiple requirements
    for req_text in requirement_texts:
        await tx.requirements.create(CreateRequirementRequest(
            job_id=job_id,
            text=req_text,
            req_type="functional"
        ))

    # All commits together, or all rolls back on error
```

**Error handling:**
```python
try:
    async with postgres_db.transaction() as tx:
        await tx.jobs.update_status(job_id, UpdateJobStatusRequest(status="processing"))
        # ... do work ...
        await tx.jobs.update_status(job_id, UpdateJobStatusRequest(status="completed"))
except Exception as e:
    # Transaction automatically rolled back
    logger.error(f"Transaction failed: {e}")
```

---

## 8. Migration Strategy

### 8.1 Phase 1: Create New Module (Week 1)

**Goal:** Build new `postgres_db.py` alongside existing code without breaking anything.

**Tasks:**
1. Create directory structure:
   ```
   src/database/
   ├── models.py              # NEW: Request/response dataclasses
   ├── config.py              # NEW: PostgresConfig
   ├── query_loader.py        # NEW: Query loading logic
   ├── postgres_db.py         # NEW: PostgresDB class
   ├── postgres_utils.py      # KEEP: Existing code
   └── queries/
       └── postgres/
           ├── schema.sql     # MOVE: from parent directory
           ├── jobs.sql       # NEW: Named job queries
           └── requirements.sql  # NEW: Named requirement queries
   ```

2. Implement `models.py` with all dataclasses (see section 4.1)

3. Implement `config.py`:
   ```python
   @dataclass
   class PostgresConfig:
       database_url: Optional[str] = None
       host: str = "localhost"
       port: int = 5432
       user: str = "graphrag"
       password: str = "graphrag_password"
       database: str = "graphrag"
       min_connections: int = 2
       max_connections: int = 10
       command_timeout: int = 60
   ```

4. Implement `query_loader.py` (see section 5.3)

5. Implement `postgres_db.py`:
   - Core PostgresDB class with connection management
   - JobOperations namespace class
   - RequirementOperations namespace class
   - Transaction support

6. Create named query files (see section 5.2)

7. Update `src/database/__init__.py`:
   ```python
   # NEW imports (dependency injection pattern)
   from .postgres_db import PostgresDB
   from .models import (
       Job, CreateJobRequest, UpdateJobStatusRequest,
       Requirement, CreateRequirementRequest, UpdateRequirementStatusRequest,
   )

   # OLD imports (keep for backward compatibility - will be removed in Phase 5)
   from .postgres_utils import (
       PostgresConnection,
       create_postgres_connection,
       create_job,
       get_job,
       update_job_status,
       create_requirement,
       get_pending_requirement,
       update_requirement_status,
       count_requirements_by_status,
   )

   __all__ = [
       'PostgresDB',  # NEW class
       'PostgresConnection',  # OLD (deprecated)
   ]
   ```

**Deliverable:** New PostgresDB class exists, old code still works, nothing broken.

### 8.2 Phase 2: Fix ToolContext Bug (Week 1)

**Goal:** Fix the critical async/sync bug in ToolContext.

**File:** `src/tools/context.py:192-259`

**Before (BROKEN):**
```python
def update_job_status(self, status: str, ...) -> bool:
    # SYNC method
    cursor = self.postgres_conn.cursor()  # WRONG: async Pool has no cursor()
    cursor.execute(...)  # WRONG
    self.postgres_conn.commit()  # WRONG
```

**After (FIXED):**
```python
async def update_job_status(self, status: str, ...) -> bool:
    """Update job status - ASYNC version."""
    if not self.job_id or not self.has_postgres():
        return False

    try:
        # Use injected PostgresDB instance (dependency injection)
        # Assumes self.postgres_conn is a PostgresDB instance passed via constructor

        await self.postgres_conn.jobs.update_status(
            self.job_id,
            UpdateJobStatusRequest(
                status=status,
                error_message=error_message,
                completed_at=completed_at
            )
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update job status: {e}")
        return False
```

**Impact:** ALL tools that call `context.update_job_status()` must now use `await`:
```python
# src/tools/completion_tools.py
# Before
db_updated = context.update_job_status(status="completed", completed_at=True)

# After
db_updated = await context.update_job_status(status="completed", completed_at=True)
```

**Testing:**
```bash
# Run tool tests
pytest tests/test_tools/ -v

# Specifically test completion tools
pytest tests/test_tools/test_completion_tools.py -v
```

**Deliverable:** ToolContext.update_job_status() is now async and works correctly.

### 8.3 Phase 3: Migrate Agent Code (Week 2)

**Goal:** Update `src/agent.py` to use new PostgresDB.

**Changes:**

**Before:**
```python
# src/agent.py
from src.database.postgres_utils import PostgresConnection

class UniversalAgent:
    def __init__(self, ...):
        self.postgres_conn = PostgresConnection(...)

    async def _update_job_status(self, job_id: str, status: str, ...):
        query = f"UPDATE {table} SET {status_field} = $1 WHERE id = $2"
        await self.postgres_conn.execute(query, status, job_id)
```

**After:**
```python
# src/agent.py
from src.database import PostgresDB

class UniversalAgent:
    def __init__(self, postgres_db: PostgresDB, ...):
        # Dependency injection
        self.postgres_db = postgres_db

    async def _update_job_status(self, job_id: str, status: str, ...):
        # Use high-level API
        await self.postgres_db.jobs.update_status(
            uuid.UUID(job_id),
            UpdateJobStatusRequest(status=status, error_message=error)
        )
```

**Benefits:**
- Cleaner code
- No inline SQL
- Better type safety
- Easier to test

**Testing:**
```bash
# Test agent functionality
pytest tests/test_agent.py -v

# Integration tests
pytest tests/integration/ -v
```

**Deliverable:** Agent uses new PostgresDB, old PostgresConnection no longer referenced.

### 8.4 Phase 4: Handle Dashboard Sync Access (Week 2)

**Problem:** Dashboard (`dashboard/db.py`) uses **psycopg2** (sync), but new PostgresDB uses **asyncpg** (async).

**Options:**

**Option A - Make Dashboard Async (Recommended):**
```python
# dashboard/db.py
from src.database import postgres_db
import asyncio

async def create_job_async(prompt: str, document_path: str | None = None) -> UUID:
    """Create a new job - ASYNC version."""
    return await postgres_db.jobs.create(CreateJobRequest(
        prompt=prompt,
        document_path=document_path
    ))

def create_job(prompt: str, document_path: str | None = None) -> UUID:
    """Create a new job - SYNC wrapper for Streamlit."""
    return asyncio.run(create_job_async(prompt, document_path))
```

**Option B - Separate Sync Connection (Not Recommended):**
- Keep psycopg2 for dashboard
- Maintain parallel implementation
- More code duplication

**Recommendation:** Option A - Streamlit works fine with async/await using `asyncio.run()`.

**Changes:**
```python
# Before (dashboard/db.py)
def create_job(prompt: str, document_path: str | None = None) -> UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO jobs ...")
            return cur.fetchone()[0]

# After
import asyncio
from src.database import postgres_db

def create_job(prompt: str, document_path: str | None = None) -> UUID:
    return asyncio.run(postgres_db.jobs.create(
        CreateJobRequest(prompt=prompt, document_path=document_path)
    ))
```

**Testing:**
```bash
# Start dashboard
cd dashboard && streamlit run app.py

# Test job creation through UI
# Test job listing through UI
```

**Deliverable:** Dashboard uses PostgresDB via sync wrappers.

### 8.5 Phase 5: Remove Old Code (Week 3)

**Goal:** Delete deprecated code after migration is complete.

**Tasks:**

1. **Verify no references to old code:**
   ```bash
   grep -r "from src.database.postgres_utils" src/
   grep -r "from src.database import postgres_utils" src/
   grep -r "PostgresConnection" src/
   grep -r "create_job\(" src/  # May have false positives
   ```

2. **Remove files:**
   ```bash
   git rm src/database/postgres_utils.py
   ```

3. **Update imports in `src/database/__init__.py`:**
   ```python
   # Remove OLD imports
   # from .postgres_utils import PostgresConnection, ...

   # Keep only NEW imports
   from .postgres_db import PostgresDB
   from .models import Job, Requirement, ...

   postgres_db = PostgresDB()
   ```

4. **Update tests:**
   - Remove tests for old `postgres_utils.py`
   - Ensure new tests cover all functionality

5. **Update documentation:**
   - Update CLAUDE.md
   - Update README if exists
   - Mark this document as "Implemented"

**Verification:**
```bash
# All tests pass
pytest tests/ -v

# No import errors
python -c "from src.database import postgres_db; print('OK')"

# Dashboard works
cd dashboard && streamlit run app.py
```

**Deliverable:** Old postgres_utils.py deleted, only PostgresDB remains.

---

## 9. Testing Strategy

### 9.1 Unit Tests

**File:** `tests/test_database/test_postgres_db.py`

```python
import pytest
from src.database import postgres_db
from src.database.models import CreateJobRequest, Job

@pytest.fixture
async def db():
    """Database fixture."""
    await postgres_db.connect()
    yield postgres_db
    await postgres_db.disconnect()

@pytest.mark.asyncio
async def test_create_job(db):
    """Test job creation."""
    request = CreateJobRequest(
        prompt="Test prompt",
        document_path="/path/to/doc.pdf"
    )

    job_id = await db.jobs.create(request)
    assert job_id is not None

    job = await db.jobs.get(job_id)
    assert job.prompt == "Test prompt"
    assert job.status == "created"

@pytest.mark.asyncio
async def test_transaction_rollback(db):
    """Test transaction rollback on error."""
    try:
        async with db.transaction() as tx:
            job_id = await tx.jobs.create(CreateJobRequest(prompt="Test"))
            raise ValueError("Test error")
    except ValueError:
        pass

    # Job should not exist due to rollback
    job = await db.jobs.get(job_id)
    assert job is None

@pytest.mark.asyncio
async def test_get_pending_requirement(db):
    """Test SKIP LOCKED behavior."""
    # Create job and requirements
    job_id = await db.jobs.create(CreateJobRequest(prompt="Test"))

    req1_id = await db.requirements.create(CreateRequirementRequest(
        job_id=job_id, text="Requirement 1"
    ))

    # Get pending - should lock and return req1
    req = await db.requirements.get_pending(job_id)
    assert req.id == req1_id
    assert req.status == "validating"  # Status updated

    # Get pending again - should return None (locked)
    req2 = await db.requirements.get_pending(job_id)
    assert req2 is None
```

### 9.2 Integration Tests

**File:** `tests/integration/test_postgres_integration.py`

```python
@pytest.mark.asyncio
async def test_full_workflow():
    """Test complete job workflow."""
    await postgres_db.connect()

    try:
        # 1. Create job
        job_id = await postgres_db.jobs.create(CreateJobRequest(
            prompt="Extract requirements from doc.pdf",
            document_path="/data/doc.pdf"
        ))

        # 2. Update job to processing
        await postgres_db.jobs.update_status(job_id, UpdateJobStatusRequest(
            status="processing",
            creator_status="processing"
        ))

        # 3. Create requirements
        req_ids = []
        for i in range(5):
            req_id = await postgres_db.requirements.create(CreateRequirementRequest(
                job_id=job_id,
                text=f"Requirement {i}",
                req_type="functional"
            ))
            req_ids.append(req_id)

        # 4. Validate requirements
        for req_id in req_ids:
            await postgres_db.requirements.update_status(req_id, UpdateRequirementStatusRequest(
                status="integrated",
                neo4j_id=f"neo4j_{req_id}"
            ))

        # 5. Complete job
        await postgres_db.jobs.update_status(job_id, UpdateJobStatusRequest(
            status="completed",
            creator_status="completed",
            completed_at=True
        ))

        # 6. Verify summary
        summary = await postgres_db.jobs.get_summary(job_id)
        assert summary.integrated_requirements == 5
        assert summary.status == "completed"

    finally:
        await postgres_db.disconnect()
```

### 9.3 Test Coverage Goals

| Component | Target Coverage |
|-----------|----------------|
| PostgresDB core | 95%+ |
| JobOperations | 90%+ |
| RequirementOperations | 90%+ |
| QueryLoader | 85%+ |
| Models (dataclasses) | 80%+ |

```bash
# Run with coverage
pytest tests/test_database/ --cov=src/database --cov-report=html

# View coverage report
open htmlcov/index.html
```

---

## 10. Implementation Checklist

### Phase 1: Create New Module ✓
- [ ] Create `src/database/models.py` with all dataclasses
- [ ] Create `src/database/config.py` with PostgresConfig
- [ ] Create `src/database/query_loader.py`
- [ ] Create `src/database/queries/postgres/` directory
- [ ] Write `queries/postgres/jobs.sql` with named queries
- [ ] Write `queries/postgres/requirements.sql` with named queries
- [ ] Move `schema.sql` to `queries/postgres/`
- [ ] Create `src/database/postgres_db.py`:
  - [ ] PostgresDB class with connection management
  - [ ] JobOperations class
  - [ ] RequirementOperations class
  - [ ] Transaction support
  - [ ] Health check method
- [ ] Update `src/database/__init__.py` to export PostgresDB class
- [ ] Write unit tests for PostgresDB
- [ ] Verify old code still works

### Phase 2: Fix ToolContext Bug ✓
- [ ] Update `src/tools/context.py`:
  - [ ] Make `update_job_status()` async
  - [ ] Use injected PostgresDB instance
  - [ ] Import UpdateJobStatusRequest
- [ ] Update all callers to use `await`:
  - [ ] `src/tools/completion_tools.py`
  - [ ] Any other tools calling this method
- [ ] Test tool functionality
- [ ] Verify no runtime errors

### Phase 3: Migrate Agent Code ✓
- [ ] Update `src/agent.py`:
  - [ ] Replace PostgresConnection with injected PostgresDB instance
  - [ ] Update `_update_job_status()` to use jobs.update_status()
  - [ ] Update polling queries to use new API
  - [ ] Remove inline SQL
- [ ] Test agent polling
- [ ] Test agent execution
- [ ] Integration tests pass

### Phase 4: Handle Dashboard Sync Access ✓
- [ ] Update `dashboard/db.py`:
  - [ ] Create async versions of functions
  - [ ] Add sync wrappers using `asyncio.run()`
  - [ ] Create PostgresDB instance
- [ ] Test dashboard functionality:
  - [ ] Create job through UI
  - [ ] List jobs
  - [ ] View job details
  - [ ] Cancel job
- [ ] Verify no performance issues

### Phase 5: Remove Old Code ✓
- [ ] Verify no references to old code:
  - [ ] `grep -r "postgres_utils" src/`
  - [ ] `grep -r "PostgresConnection" src/`
- [ ] Remove `src/database/postgres_utils.py`
- [ ] Update `src/database/__init__.py` (remove old imports)
- [ ] Remove old tests
- [ ] Update documentation:
  - [ ] CLAUDE.md
  - [ ] This document (mark as implemented)
- [ ] Final integration test run
- [ ] Git commit with message: "Completed PostgreSQL migration to PostgresDB class"

### Documentation ✓
- [ ] Update CLAUDE.md with new patterns
- [ ] Add examples to docstrings
- [ ] Create migration guide for external contributors
- [ ] Mark db_refactor.md as implemented

### Performance Testing ✓
- [ ] Benchmark connection pool performance
- [ ] Verify SKIP LOCKED works under load
- [ ] Test transaction rollback under various failures
- [ ] Profile memory usage

---

## Appendix A: Current vs Target Comparison

### Current (postgres_utils.py)

**Pros:**
- ✅ Uses asyncpg with connection pooling
- ✅ Simple, functional API
- ✅ Works for current use cases

**Cons:**
- ❌ Module-level functions, not class-based
- ❌ 20-parameter function signatures
- ❌ Mixed async/sync usage (ToolContext bug)
- ❌ Inline SQL strings everywhere
- ❌ No transaction support
- ❌ No health checks
- ❌ Dynamic SQL building with string interpolation
- ❌ No request/response objects
- ❌ Dashboard uses separate psycopg2 driver

### Target (postgres_db.py)

**Pros:**
- ✅ Clean class-based API
- ✅ Namespace organization (jobs, requirements)
- ✅ Dataclass request/response objects
- ✅ Named queries in separate files
- ✅ Transaction support
- ✅ Health checks built-in
- ✅ Type-safe with full IDE support
- ✅ Dependency injection pattern (flexible and testable)
- ✅ Unified async API for all consumers
- ✅ Easy to test and mock

**Cons:**
- ⚠️ More files to maintain (but better organized)
- ⚠️ Dashboard needs async wrappers (minor)

---

## Appendix B: File Size Estimates

| File | Current | Target | Change |
|------|---------|--------|--------|
| postgres_utils.py | 496 lines | 0 lines (deleted) | -496 |
| postgres_db.py | - | ~400 lines | +400 |
| models.py | - | ~300 lines | +300 |
| config.py | - | ~50 lines | +50 |
| query_loader.py | - | ~100 lines | +100 |
| jobs.sql | - | ~80 lines | +80 |
| requirements.sql | - | ~120 lines | +120 |
| **Total** | **496** | **1,050** | **+554** |

**Analysis:** More code overall, but significantly better organized and maintainable. The increase is due to:
- Dataclass definitions (replaces 20-param function signatures)
- Named query files (replaces inline SQL strings)
- Transaction support (new capability)
- Proper error handling (new capability)

---

## Appendix C: References

- **asyncpg documentation:** https://magicstack.github.io/asyncpg/
- **psycopg2 documentation:** https://www.psycopg.org/docs/
- **Python dataclasses:** https://docs.python.org/3/library/dataclasses.html
- **Related Document:** `/home/ghost/Repositories/Uni-Projekt-Graph-RAG/docs/db_refactor.md`
- **Current Implementation:** `/home/ghost/Repositories/Uni-Projekt-Graph-RAG/src/database/postgres_utils.py`
- **Schema:** `/home/ghost/Repositories/Uni-Projekt-Graph-RAG/src/database/schema.sql`
- **Bug Location:** `/home/ghost/Repositories/Uni-Projekt-Graph-RAG/src/tools/context.py:224`

---

**Document End**
