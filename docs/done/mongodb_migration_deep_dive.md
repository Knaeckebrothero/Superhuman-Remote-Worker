# MongoDB Migration Deep Dive

**Status**: ✅ **COMPLETE** (Uses dependency injection like PostgreSQL and Neo4j)
**Created**: 2026-01-14
**Part of**: Database Refactoring Initiative
**Related**: [db_refactor.md](db_refactor.md), [postgres_migration_deep_dive.md](postgres_migration_deep_dive.md), [neo4j_migration_deep_dive.md](neo4j_migration_deep_dive.md)

---

## Executive Summary

### Purpose

This document describes the MongoDB implementation using **dependency injection**, following the same patterns as PostgreSQL and Neo4j. MongoDB is used exclusively for **LLM request archiving and agent audit trails** - an optional observability component for debugging, cost tracking, and compliance.

### Current State: Dependency Injection ✅

**Key Design Decision:** MongoDB uses **dependency injection**, consistent with PostgreSQL and Neo4j. No singleton pattern is used.

**Implementation**:
- **Location**: `LLMArchiver` class in `src/core/archiver.py` (720 lines)
- **Pattern**: Dependency injection - instances created and passed as needed
- **Synchronous architecture**: Uses `pymongo` (consistent with Neo4j)
- **Optional component**: Gracefully degrades if MongoDB unavailable or pymongo not installed
- **Two collections**: `llm_requests` (LLM API calls) and `agent_audit` (agent execution steps)
- **Rich indexing**: 12 indexes across both collections for efficient querying
- **Usage**: Passed to `src/graph.py` via context at graph construction

**Rationale:**
Proper databases like MongoDB (with pymongo) have built-in connection pooling and handle concurrency correctly. Singletons were only needed for SQLite to avoid write conflicts. Using dependency injection provides better testing, flexibility, and explicit dependencies.

### Architecture Overview

1. **Namespace pattern** (if refactored): `mongodb_db.llm.archive()`, `mongodb_db.audit.audit_step()`
2. **Dependency injection**: Each component creates its own instance and passes it as parameter
3. **Consistent API**: Clean interface matching PostgreSQL/Neo4j patterns
4. **Easy testing**: Simple to mock and test with explicit dependencies
5. **Good organization**: Clear separation of concerns (connection, LLM archiving, agent auditing, querying)

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Usage Patterns](#2-usage-patterns)
3. [Target Architecture](#3-target-architecture)
4. [API Design](#4-api-design)
5. [Migration Plan](#5-migration-plan)
6. [Testing Strategy](#6-testing-strategy)
7. [Implementation Checklist](#7-implementation-checklist)
8. [Risks and Mitigations](#8-risks-and-mitigations)
9. [Appendix](#9-appendix)

---

## 1. Current State Analysis

### 1.1 Core Implementation: `src/core/archiver.py` (720 lines)

**Purpose**: Archive LLM requests/responses and complete agent execution audit trails to MongoDB for debugging, cost tracking, and compliance.

**Architecture**:
```python
class LLMArchiver:
    """Archives LLM requests and responses to MongoDB."""

    def __init__(self, mongodb_url, database_name="graphrag_logs",
                 collection_name="llm_requests",
                 audit_collection_name="agent_audit"):
        self._mongodb_url = mongodb_url
        self._client = None  # MongoClient
        self._db = None
        self._collection = None  # llm_requests
        self._audit_collection = None  # agent_audit
        self._connected = False
        self._connection_attempted = False
        self._step_counters: Dict[str, int] = {}  # Per-job step numbering

    @classmethod
    def from_env(cls) -> Optional["LLMArchiver"]:
        """Create archiver from MONGODB_URL environment variable."""
        mongodb_url = os.getenv("MONGODB_URL")
        if not mongodb_url:
            return None  # Gracefully degrade
        # Extract database name from URL
        db_name = url.split("/")[-1].split("?")[0] or "graphrag_logs"
        return cls(mongodb_url=mongodb_url, database_name=db_name)

    def _ensure_connected(self) -> bool:
        """Lazy connection establishment with retry protection."""
        if self._connected:
            return True
        if self._connection_attempted:
            return False  # Don't retry failed connections

        self._connection_attempted = True
        try:
            from pymongo import MongoClient
            self._client = MongoClient(
                self._mongodb_url,
                serverSelectionTimeoutMS=5000,
            )
            self._client.admin.command("ping")  # Test connection
            self._db = self._client[self._database_name]
            self._collection = self._db[self._collection_name]
            self._audit_collection = self._db[self._audit_collection_name]
            self._connected = True
            return True
        except ImportError:
            logger.warning("pymongo not installed, LLM archiving disabled")
            return False
        except Exception as e:
            logger.warning(f"Failed to connect to MongoDB: {e}")
            return False
```

**Key Methods**:

1. **LLM Request Archiving** (lines 212-296):
   ```python
   def archive(
       self, job_id: str, agent_type: str,
       messages: Sequence[BaseMessage], response: AIMessage,
       model: str, latency_ms: Optional[int] = None,
       iteration: Optional[int] = None,
       metadata: Optional[Dict[str, Any]] = None,
   ) -> Optional[str]:
       """Archive an LLM request/response."""
       if not self._ensure_connected():
           return None

       doc = {
           "job_id": job_id,
           "agent_type": agent_type,
           "timestamp": datetime.now(timezone.utc),
           "model": model,
           "request": {
               "messages": [_message_to_dict(m) for m in messages],
               "message_count": len(messages),
           },
           "response": _message_to_dict(response),
           "latency_ms": latency_ms,
           "iteration": iteration,
           "metadata": metadata,
           "metrics": {
               "input_chars": sum(len(m.content) for m in messages),
               "output_chars": len(response.content),
               "tool_calls": len(response.tool_calls or []),
           },
       }

       result = self._collection.insert_one(doc)
       return str(result.inserted_id)
   ```

2. **Agent Audit Trail** (lines 403-468):
   ```python
   def audit_step(
       self, job_id: str, agent_type: str,
       step_type: str, node_name: str, iteration: int,
       data: Optional[Dict[str, Any]] = None,
       latency_ms: Optional[int] = None,
       metadata: Optional[Dict[str, Any]] = None,
   ) -> Optional[str]:
       """Audit any step in the agent workflow."""
       if not self._ensure_connected():
           return None

       step_number = self._get_next_step_number(job_id)

       doc = {
           "job_id": job_id,
           "agent_type": agent_type,
           "iteration": iteration,
           "step_number": step_number,
           "step_type": step_type,  # initialize, llm_call, tool_call, etc.
           "node_name": node_name,  # initialize, process, tools, check
           "timestamp": datetime.now(timezone.utc),
           "latency_ms": latency_ms,
           "metadata": metadata,
       }

       if data:
           doc.update(data)  # Merge step-specific data

       result = self._audit_collection.insert_one(doc)
       return str(result.inserted_id)
   ```

3. **Convenience Methods for Tool Auditing** (lines 469-569):
   ```python
   def audit_tool_call(self, job_id, agent_type, iteration,
                       tool_name, call_id, arguments, ...):
       """Audit a tool call before execution."""
       args_preview = {k: self._truncate_string(v, 200)
                      for k, v in arguments.items()}
       return self.audit_step(
           step_type="tool_call",
           data={"tool": {"name": tool_name, "call_id": call_id,
                         "arguments": args_preview}}
       )

   def audit_tool_result(self, job_id, agent_type, iteration,
                         tool_name, call_id, result, success,
                         latency_ms, error=None, ...):
       """Audit a tool result after execution."""
       return self.audit_step(
           step_type="tool_result",
           data={"tool": {
               "name": tool_name,
               "result_preview": self._truncate_string(result, 500),
               "success": success,
               "error": error,
           }},
           latency_ms=latency_ms,
       )
   ```

4. **Query Methods** (lines 298-398, 571-668):
   ```python
   def get_conversation(self, job_id: str, agent_type: Optional[str] = None,
                        limit: int = 100) -> List[Dict[str, Any]]:
       """Get conversation history for a job."""
       query = {"job_id": job_id}
       if agent_type:
           query["agent_type"] = agent_type
       cursor = self._collection.find(query).sort("timestamp", 1).limit(limit)
       return list(cursor)

   def get_job_stats(self, job_id: str) -> Dict[str, Any]:
       """Get statistics for a job's LLM usage."""
       pipeline = [
           {"$match": {"job_id": job_id}},
           {"$group": {
               "_id": "$job_id",
               "total_requests": {"$sum": 1},
               "total_input_chars": {"$sum": "$metrics.input_chars"},
               "total_output_chars": {"$sum": "$metrics.output_chars"},
               "total_tool_calls": {"$sum": "$metrics.tool_calls"},
               "avg_latency_ms": {"$avg": "$latency_ms"},
               "models_used": {"$addToSet": "$model"},
           }},
       ]
       return list(self._collection.aggregate(pipeline))[0]

   def get_job_audit_trail(self, job_id: str, step_type: Optional[str] = None,
                           limit: int = 1000) -> List[Dict[str, Any]]:
       """Get complete audit trail for a job."""
       query = {"job_id": job_id}
       if step_type:
           query["step_type"] = step_type
       cursor = self._audit_collection.find(query).sort("step_number", 1).limit(limit)
       return list(cursor)

   def get_audit_stats(self, job_id: str) -> Dict[str, Any]:
       """Get audit statistics for a job."""
       # Aggregates by step_type, calculates latencies, counts
       ...
   ```

**Strengths**:
- **Optional by design**: Gracefully degrades when MongoDB unavailable
- **Lazy connection**: Only connects when first used
- **Proper error handling**: All methods return None on failure, log warnings
- **No blocking failures**: Missing pymongo or connection errors don't crash agent
- **Message serialization**: Clean conversion of LangChain messages to dicts
- **Truncation strategy**: Large arguments/results truncated to avoid document size limits
- **Step numbering**: Sequential step_number per job for ordered audit trail

**Potential Improvements**:
- **Mixed concerns**: LLM archiving + agent auditing + querying in one class (could be separated)
- **No explicit connection management**: No connect()/close() contract (relies on lazy init)
- **Database name parsing**: Database name extracted from URL with string manipulation
- **No connection pooling config**: Uses pymongo defaults
- **No bulk operations**: Each archive/audit is individual insert (no batching for performance)

### 1.2 Initialization Script: `scripts/init_mongodb.py` (318 lines)

**Purpose**: Initialize MongoDB collections with proper indexes for efficient querying.

**Key Functions**:

```python
def create_collections_and_indexes(client, db_name: str, logger):
    """Create collections and indexes for LLM archiving and auditing."""
    db = client[db_name]

    # Collection: llm_requests
    llm_requests = db["llm_requests"]
    llm_indexes = [
        ("job_id", {"name": "idx_job_id"}),
        ("agent_type", {"name": "idx_agent_type"}),
        ("timestamp", {"name": "idx_timestamp"}),
        ("model", {"name": "idx_model"}),
        ([("job_id", 1), ("agent_type", 1), ("timestamp", -1)],
         {"name": "idx_job_agent_time"}),
    ]

    # Collection: agent_audit
    agent_audit = db["agent_audit"]
    audit_indexes = [
        ("job_id", {"name": "idx_audit_job_id"}),
        ("step_type", {"name": "idx_audit_step_type"}),
        ("node_name", {"name": "idx_audit_node_name"}),
        ("timestamp", {"name": "idx_audit_timestamp"}),
        ([("job_id", 1), ("step_number", 1)],
         {"name": "idx_audit_job_step"}),
        ([("job_id", 1), ("iteration", 1), ("step_number", 1)],
         {"name": "idx_audit_job_iter_step"}),
        ([("job_id", 1), ("agent_type", 1), ("step_type", 1)],
         {"name": "idx_audit_job_agent_type"}),
    ]
```

**12 Total Indexes**:
- **llm_requests**: 5 indexes (single fields + compound for common queries)
- **agent_audit**: 7 indexes (optimized for step_number ordering and filtering)

**CLI Features**:
- `--clear`: Delete all data and reinitialize
- `--check`: Connection test only
- Called by `scripts/app_init.py` as part of full system initialization

### 1.3 Query/Visualization Tool: `scripts/view_llm_conversation.py` (1035 lines)

**Purpose**: CLI tool for querying and visualizing LLM conversations and audit trails.

**Key Features**:

1. **Conversation Viewing**:
   ```bash
   python scripts/view_llm_conversation.py --job-id <uuid>
   python scripts/view_llm_conversation.py --job-id <uuid> --stats
   python scripts/view_llm_conversation.py --recent 20
   python scripts/view_llm_conversation.py --list  # All jobs
   ```

2. **Audit Trail Viewing**:
   ```bash
   python scripts/view_llm_conversation.py --job-id <uuid> --audit
   python scripts/view_llm_conversation.py --job-id <uuid> --audit --step-type tool_call
   python scripts/view_llm_conversation.py --job-id <uuid> --audit --timeline
   python scripts/view_llm_conversation.py --job-id <uuid> --audit --stats
   ```

3. **Export Capabilities**:
   ```bash
   # Export to JSON
   python scripts/view_llm_conversation.py --job-id <uuid> --export conv.json

   # Export single request as HTML (messenger-style visualization)
   python scripts/view_llm_conversation.py --doc-id <mongodb_objectid>
   python scripts/view_llm_conversation.py --doc-id <mongodb_objectid> --html output.html
   ```

**Query Functions**: 11 total
- `view_conversation()` - Show LLM requests for job
- `view_recent()` - Recent requests across all jobs
- `get_job_stats()` - LLM usage statistics
- `export_conversation()` - JSON export
- `export_html_conversation()` - HTML messenger view
- `list_jobs()` - All jobs with LLM records
- `view_audit_trail()` - Complete audit trail
- `view_audit_timeline()` - Timeline visualization
- `get_audit_stats()` - Audit statistics

**HTML Export Template**: Embedded CSS for messenger-style conversation view with color-coded messages.

### 1.4 Usage in Agent: `src/graph.py`

**Current Pattern**: Audit points throughout the nested loop graph using dependency injection. The archiver instance is passed to the graph at construction time.

```python
from .core.archiver import get_archiver

def initialize_workspace(state):
    """Initialize workspace at job start."""
    # ... workspace setup ...

    # Audit workspace initialization
    auditor = get_archiver()
    if auditor:
        auditor.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="initialize",
            node_name="initialize",
            iteration=0,
            data={"state": {"document_path": doc_path, ...}},
        )

def process_node(state):
    """Main LLM processing node."""
    # ... before LLM call ...

    # Audit LLM call
    auditor = get_archiver()
    if auditor:
        auditor.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="llm_call",
            node_name="process",
            iteration=iteration,
            data={
                "llm": {
                    "model": model_name,
                    "input_message_count": len(messages),
                },
                "state": {"message_count": len(messages), "token_count": tokens},
            },
        )

    # LLM invocation (actual call)
    response = llm_with_tools.invoke(messages)

    # Audit LLM response
    if auditor:
        auditor.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="llm_response",
            node_name="process",
            iteration=iteration,
            data={
                "llm": {
                    "tool_calls": [{"name": tc["name"], "id": tc["id"]}
                                   for tc in tool_calls],
                    "response_content_preview": response.content[:200],
                    "metrics": {
                        "output_chars": len(response.content),
                        "tool_call_count": len(tool_calls),
                    },
                },
            },
            latency_ms=int(response_time * 1000),
        )

def tools_node(state):
    """Execute tool calls."""
    for tool_call in tool_calls:
        # Audit tool call
        auditor = get_archiver()
        if auditor:
            auditor.audit_tool_call(
                job_id=job_id,
                agent_type=agent_type,
                iteration=iteration,
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
            )

        # Execute tool
        result = tool.invoke(tool_call)

        # Audit tool result
        if auditor:
            auditor.audit_tool_result(
                job_id=job_id,
                agent_type=agent_type,
                iteration=iteration,
                tool_name=tool_name,
                call_id=call_id,
                result=result,
                success=True,
                latency_ms=int(exec_time * 1000),
            )
```

**15+ Audit Points** in `src/graph.py`:
- Workspace initialization (1x)
- Instructions read (1x)
- Planning phase LLM call/response (2x)
- Initial todos LLM call/response (2x)
- Outer loop phase transition (1x)
- Memory update LLM call/response (2x)
- Todo creation LLM call/response (2x)
- Inner loop LLM call/response (2x per iteration)
- Tool calls/results (2x per tool)
- Check decision (1x)
- Phase completion (1x)
- Goal check (3x for different outcomes)
- Error conditions (1x)

**Pattern**: Always check `if auditor:` before calling (graceful degradation).

### 1.5 Export: `src/core/__init__.py`

**Exports**:
```python
from .archiver import get_archiver, LLMArchiver

__all__ = [
    # ... other exports ...
    "get_archiver",
    "LLMArchiver",
]
```

**Usage**:
```python
# In src/graph.py
from .core.archiver import get_archiver

# In external code (if needed)
from src.core import get_archiver, LLMArchiver
```

---

## 2. Usage Patterns

### Pattern 1: Dependency Injection (CURRENT)

**Current Pattern:**
```python
# In UniversalAgent initialization
from src.core.archiver import LLMArchiver

archiver = LLMArchiver.from_env()  # Create instance
# Pass to graph builder
graph = build_nested_loop_graph(archiver=archiver)

# In graph nodes (archiver passed via context)
def audit_node(state, archiver: LLMArchiver):
    if archiver:
        archiver.audit_step(job_id=..., step_type="initialize", ...)
```

**Characteristics**:
- ✅ **Dependency injection** - instances created and passed explicitly
- Lazy connection via `LLMArchiver.from_env()`
- Reads `MONGODB_URL` from environment
- Returns None if MongoDB not configured (graceful degradation)
- **Usage**: Passed to graph at construction, used at 15+ audit points in `src/graph.py`

**Benefits:**
- No global state - better testing and flexibility
- MongoDB (pymongo) connection pooling handles concurrency correctly
- Consistent with PostgreSQL and Neo4j dependency injection patterns
- Easy to mock for unit tests

### Pattern 2: Direct LLM Archiving (Not Currently Used)

**Potential Usage**:
```python
from src.core.archiver import archive_llm_request

archive_llm_request(
    job_id="job-123",
    agent_type="creator",
    messages=[...],
    response=ai_message,
    model="gpt-4",
    latency_ms=1234,
    iteration=5,
)
```

**Note**: Currently not used directly; LLM archiving happens through manual calls to `auditor.archive()` if needed (but no current usage in codebase).

### Pattern 3: Explicit Archiver Creation (Testing/Scripts)

**Scripts**:
```python
from pymongo import MongoClient

# Direct pymongo usage
client = MongoClient("mongodb://localhost:27017/graphrag_logs")
db = client["graphrag_logs"]
collection = db["llm_requests"]

# Query
cursor = collection.find({"job_id": job_id}).sort("timestamp", 1)
```

**Scripts**: `scripts/view_llm_conversation.py`, `scripts/init_mongodb.py`

**Characteristics**:
- Direct pymongo usage for advanced queries
- Aggregation pipelines for statistics
- No use of LLMArchiver class

### Pattern 4: Initialization Check

**Current**:
```python
# scripts/app_init.py
from scripts.init_mongodb import initialize_mongodb

success = initialize_mongodb(logger, force_reset=args.force_reset)
if not success:
    logger.warning("MongoDB initialization failed (non-critical)")
```

**Characteristics**:
- Optional component
- Failures don't block application startup
- Graceful degradation

### Pattern 5: Query and Statistics

**LLM Statistics**:
```python
auditor = get_archiver()
if auditor:
    stats = auditor.get_job_stats(job_id)
    # Returns: {total_requests, total_input_chars, total_output_chars,
    #           total_tool_calls, avg_latency_ms, models_used, ...}
```

**Audit Statistics**:
```python
if auditor:
    audit_stats = auditor.get_audit_stats(job_id)
    # Returns: {total_steps, by_step_type: {count, avg_latency, ...}, ...}
```

### Pattern 6: Conversation Retrieval

**Get Conversation History**:
```python
if auditor:
    conversation = auditor.get_conversation(
        job_id="job-123",
        agent_type="creator",  # Optional filter
        limit=100,
    )
    # Returns list of LLM request/response documents
```

**Get Audit Trail**:
```python
if auditor:
    audit_trail = auditor.get_job_audit_trail(
        job_id="job-123",
        step_type="tool_call",  # Optional filter
        limit=1000,
    )
    # Returns list of audit step documents, ordered by step_number
```

---

## 3. Target Architecture

### 3.1 Module Structure

**New Location**: `src/database/mongodb_db.py`

```
src/database/
├── __init__.py              # Exports: mongodb_db, postgres_db, neo4j_db
├── mongodb_db.py            # New unified MongoDB module (this migration)
├── postgres_db.py           # Async PostgreSQL operations (separate migration)
├── neo4j_utils.py          # Current Neo4j (to be migrated)
└── schema.sql              # PostgreSQL schema
```

### 3.2 Class Design

```python
# src/database/mongodb_db.py

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from enum import Enum

from langchain_core.messages import BaseMessage, AIMessage
from pymongo import MongoClient
from pymongo.collection import Collection


class StepType(Enum):
    """Agent execution step types."""
    INITIALIZE = "initialize"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CHECK = "check"
    ROUTING = "routing"
    ERROR = "error"


class MongoDBConnection:
    """MongoDB connection manager with lazy initialization."""

    def __init__(
        self,
        connection_string: str,
        database_name: str = "graphrag_logs",
        server_selection_timeout_ms: int = 5000,
        max_pool_size: int = 100,
        min_pool_size: int = 0,
        max_idle_time_ms: int = 30000,
    ):
        """Initialize MongoDB connection manager.

        Args:
            connection_string: MongoDB connection string
            database_name: Database name
            server_selection_timeout_ms: Server selection timeout
            max_pool_size: Maximum connection pool size
            min_pool_size: Minimum connection pool size
            max_idle_time_ms: Maximum idle time for connections
        """
        self.connection_string = connection_string
        self.database_name = database_name
        self._client: Optional[MongoClient] = None
        self._db = None
        self._connected = False

        # Connection options
        self._options = {
            "serverSelectionTimeoutMS": server_selection_timeout_ms,
            "maxPoolSize": max_pool_size,
            "minPoolSize": min_pool_size,
            "maxIdleTimeMS": max_idle_time_ms,
        }

    def connect(self) -> None:
        """Establish MongoDB connection.

        Raises:
            ImportError: If pymongo is not installed
            ConnectionError: If connection fails
        """
        if self._connected:
            return

        try:
            from pymongo import MongoClient
        except ImportError:
            raise ImportError("pymongo is required for MongoDB support")

        self._client = MongoClient(self.connection_string, **self._options)

        # Test connection
        self._client.admin.command("ping")

        self._db = self._client[self.database_name]
        self._connected = True

    def is_connected(self) -> bool:
        """Check if connected to MongoDB."""
        return self._connected

    def close(self) -> None:
        """Close MongoDB connection."""
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            self._connected = False

    def get_collection(self, name: str) -> Collection:
        """Get a collection.

        Args:
            name: Collection name

        Returns:
            MongoDB collection

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Not connected to MongoDB. Call connect() first.")
        return self._db[name]

    @property
    def llm(self) -> "LLMArchiveOperations":
        """LLM request archiving operations."""
        return LLMArchiveOperations(self)

    @property
    def audit(self) -> "AuditOperations":
        """Agent audit trail operations."""
        return AuditOperations(self)

    @property
    def query(self) -> "QueryOperations":
        """Query and statistics operations."""
        return QueryOperations(self)


class LLMArchiveOperations:
    """Operations for archiving LLM requests/responses."""

    def __init__(self, connection: MongoDBConnection):
        self.conn = connection
        self.collection_name = "llm_requests"

    def archive(
        self,
        job_id: str,
        agent_type: str,
        messages: Sequence[BaseMessage],
        response: AIMessage,
        model: str,
        latency_ms: Optional[int] = None,
        iteration: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Archive an LLM request/response.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            messages: Input messages
            response: LLM response
            model: Model name
            latency_ms: Request latency in milliseconds
            iteration: Iteration number
            metadata: Additional metadata

        Returns:
            Inserted document ID

        Raises:
            RuntimeError: If not connected
        """
        collection = self.conn.get_collection(self.collection_name)

        doc = {
            "job_id": job_id,
            "agent_type": agent_type,
            "timestamp": datetime.utcnow(),
            "model": model,
            "request": {
                "messages": [self._message_to_dict(m) for m in messages],
                "message_count": len(messages),
            },
            "response": self._message_to_dict(response),
            "metrics": {
                "input_chars": sum(
                    len(m.content) if isinstance(m.content, str) else 0
                    for m in messages
                ),
                "output_chars": len(response.content) if isinstance(response.content, str) else 0,
                "tool_calls": len(response.tool_calls) if hasattr(response, "tool_calls") and response.tool_calls else 0,
            },
        }

        if latency_ms is not None:
            doc["latency_ms"] = latency_ms
        if iteration is not None:
            doc["iteration"] = iteration
        if metadata:
            doc["metadata"] = metadata

        result = collection.insert_one(doc)
        return str(result.inserted_id)

    @staticmethod
    def _message_to_dict(msg: BaseMessage) -> Dict[str, Any]:
        """Convert LangChain message to dict."""
        from langchain_core.messages import (
            SystemMessage, HumanMessage, AIMessage, ToolMessage
        )

        result = {
            "type": msg.__class__.__name__,
            "content": msg.content if isinstance(msg.content, str) else str(msg.content),
        }

        if isinstance(msg, SystemMessage):
            result["role"] = "system"
        elif isinstance(msg, HumanMessage):
            result["role"] = "human"
        elif isinstance(msg, AIMessage):
            result["role"] = "assistant"
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                    }
                    for tc in msg.tool_calls
                ]
        elif isinstance(msg, ToolMessage):
            result["role"] = "tool"
            result["tool_call_id"] = getattr(msg, "tool_call_id", "")
            result["name"] = getattr(msg, "name", "")

        if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
            result["additional_kwargs"] = msg.additional_kwargs

        return result


class AuditOperations:
    """Operations for agent audit trail."""

    def __init__(self, connection: MongoDBConnection):
        self.conn = connection
        self.collection_name = "agent_audit"
        self._step_counters: Dict[str, int] = {}  # Per-job step numbering

    def audit_step(
        self,
        job_id: str,
        agent_type: str,
        step_type: StepType,
        node_name: str,
        iteration: int,
        data: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a single agent execution step.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            step_type: Type of step
            node_name: Graph node name
            iteration: Current iteration
            data: Step-specific data
            latency_ms: Operation latency
            metadata: Additional metadata

        Returns:
            Inserted document ID

        Raises:
            RuntimeError: If not connected
        """
        collection = self.conn.get_collection(self.collection_name)

        step_number = self._get_next_step_number(job_id)

        doc = {
            "job_id": job_id,
            "agent_type": agent_type,
            "iteration": iteration,
            "step_number": step_number,
            "step_type": step_type.value,
            "node_name": node_name,
            "timestamp": datetime.utcnow(),
        }

        if latency_ms is not None:
            doc["latency_ms"] = latency_ms
        if metadata:
            doc["metadata"] = metadata
        if data:
            doc.update(data)

        result = collection.insert_one(doc)
        return str(result.inserted_id)

    def audit_tool_call(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        arguments: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a tool call before execution.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            iteration: Current iteration
            tool_name: Tool name
            call_id: Tool call ID
            arguments: Tool arguments
            metadata: Additional metadata

        Returns:
            Inserted document ID
        """
        # Truncate large arguments
        args_preview = {
            k: self._truncate(v, 200) for k, v in arguments.items()
        }

        return self.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type=StepType.TOOL_CALL,
            node_name="tools",
            iteration=iteration,
            data={
                "tool": {
                    "name": tool_name,
                    "call_id": call_id,
                    "arguments": args_preview,
                }
            },
            metadata=metadata,
        )

    def audit_tool_result(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        result: str,
        success: bool,
        latency_ms: int,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a tool result after execution.

        Args:
            job_id: Job identifier
            agent_type: Agent type
            iteration: Current iteration
            tool_name: Tool name
            call_id: Tool call ID
            result: Tool result
            success: Whether tool succeeded
            latency_ms: Execution time
            error: Error message if failed
            metadata: Additional metadata

        Returns:
            Inserted document ID
        """
        tool_data = {
            "name": tool_name,
            "call_id": call_id,
            "result_preview": self._truncate(result, 500),
            "result_size_bytes": len(result) if result else 0,
            "success": success,
        }
        if error:
            tool_data["error"] = self._truncate(error, 500)

        return self.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type=StepType.TOOL_RESULT,
            node_name="tools",
            iteration=iteration,
            data={"tool": tool_data},
            latency_ms=latency_ms,
            metadata=metadata,
        )

    def _get_next_step_number(self, job_id: str) -> int:
        """Get next step number for a job."""
        if job_id not in self._step_counters:
            self._step_counters[job_id] = 0
        self._step_counters[job_id] += 1
        return self._step_counters[job_id]

    @staticmethod
    def _truncate(s: Any, max_length: int) -> str:
        """Truncate string to max length."""
        if not s:
            return ""
        s = str(s)
        if len(s) <= max_length:
            return s
        return s[:max_length] + "... [truncated]"


class QueryOperations:
    """Operations for querying and statistics."""

    def __init__(self, connection: MongoDBConnection):
        self.conn = connection

    def get_conversation(
        self,
        job_id: str,
        agent_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get conversation history for a job.

        Args:
            job_id: Job identifier
            agent_type: Optional agent type filter
            limit: Maximum records to return

        Returns:
            List of LLM request documents
        """
        collection = self.conn.get_collection("llm_requests")

        query = {"job_id": job_id}
        if agent_type:
            query["agent_type"] = agent_type

        cursor = collection.find(query).sort("timestamp", 1).limit(limit)
        return list(cursor)

    def get_job_stats(self, job_id: str) -> Dict[str, Any]:
        """Get LLM usage statistics for a job.

        Args:
            job_id: Job identifier

        Returns:
            Statistics dictionary
        """
        collection = self.conn.get_collection("llm_requests")

        pipeline = [
            {"$match": {"job_id": job_id}},
            {
                "$group": {
                    "_id": "$job_id",
                    "total_requests": {"$sum": 1},
                    "total_input_chars": {"$sum": "$metrics.input_chars"},
                    "total_output_chars": {"$sum": "$metrics.output_chars"},
                    "total_tool_calls": {"$sum": "$metrics.tool_calls"},
                    "avg_latency_ms": {"$avg": "$latency_ms"},
                    "max_latency_ms": {"$max": "$latency_ms"},
                    "min_latency_ms": {"$min": "$latency_ms"},
                    "first_request": {"$min": "$timestamp"},
                    "last_request": {"$max": "$timestamp"},
                    "models_used": {"$addToSet": "$model"},
                }
            },
        ]

        results = list(collection.aggregate(pipeline))
        if results:
            stats = results[0]
            stats.pop("_id", None)
            return stats
        return {}

    def get_recent_requests(
        self,
        limit: int = 50,
        agent_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent LLM requests across all jobs.

        Args:
            limit: Maximum records to return
            agent_type: Optional agent type filter

        Returns:
            List of recent requests, newest first
        """
        collection = self.conn.get_collection("llm_requests")

        query = {}
        if agent_type:
            query["agent_type"] = agent_type

        cursor = collection.find(query).sort("timestamp", -1).limit(limit)
        return list(cursor)

    def get_audit_trail(
        self,
        job_id: str,
        step_type: Optional[StepType] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Get agent audit trail for a job.

        Args:
            job_id: Job identifier
            step_type: Optional step type filter
            limit: Maximum records to return

        Returns:
            List of audit step documents, ordered by step_number
        """
        collection = self.conn.get_collection("agent_audit")

        query = {"job_id": job_id}
        if step_type:
            query["step_type"] = step_type.value

        cursor = collection.find(query).sort("step_number", 1).limit(limit)
        return list(cursor)

    def get_audit_stats(self, job_id: str) -> Dict[str, Any]:
        """Get audit trail statistics for a job.

        Args:
            job_id: Job identifier

        Returns:
            Statistics dictionary with step type breakdown
        """
        collection = self.conn.get_collection("agent_audit")

        # Aggregate by step type
        pipeline = [
            {"$match": {"job_id": job_id}},
            {
                "$group": {
                    "_id": "$step_type",
                    "count": {"$sum": 1},
                    "avg_latency_ms": {"$avg": "$latency_ms"},
                    "total_latency_ms": {"$sum": "$latency_ms"},
                }
            },
            {"$sort": {"count": -1}},
        ]

        step_results = list(collection.aggregate(pipeline))

        # Get timing info
        time_pipeline = [
            {"$match": {"job_id": job_id}},
            {
                "$group": {
                    "_id": None,
                    "first_step": {"$min": "$timestamp"},
                    "last_step": {"$max": "$timestamp"},
                    "max_iteration": {"$max": "$iteration"},
                    "total_steps": {"$sum": 1},
                }
            },
        ]

        time_results = list(collection.aggregate(time_pipeline))
        time_info = time_results[0] if time_results else {}

        stats = {
            "total_steps": time_info.get("total_steps", 0),
            "max_iteration": time_info.get("max_iteration", 0),
            "first_step": time_info.get("first_step"),
            "last_step": time_info.get("last_step"),
            "by_step_type": {},
        }

        for r in step_results:
            step_type = r["_id"]
            stats["by_step_type"][step_type] = {
                "count": r["count"],
                "avg_latency_ms": r.get("avg_latency_ms"),
                "total_latency_ms": r.get("total_latency_ms"),
            }

        return stats


# Module-level connection instance
_connection: Optional[MongoDBConnection] = None


def connect(
    connection_string: Optional[str] = None,
    database_name: str = "graphrag_logs",
    **options,
) -> None:
    """Connect to MongoDB.

    Args:
        connection_string: MongoDB connection string (reads from MONGODB_URL env if not provided)
        database_name: Database name
        **options: Additional connection options

    Raises:
        ImportError: If pymongo not installed
        ConnectionError: If connection fails
        ValueError: If no connection string provided
    """
    global _connection

    if connection_string is None:
        import os
        connection_string = os.getenv("MONGODB_URL")
        if not connection_string:
            raise ValueError("No MongoDB connection string provided (set MONGODB_URL env var)")

    # Extract database name from URL if present
    if "/" in connection_string:
        url_path = connection_string.split("/")[-1]
        if url_path and "?" not in url_path:
            database_name = url_path
        elif "?" in url_path:
            db = url_path.split("?")[0]
            if db:
                database_name = db

    _connection = MongoDBConnection(connection_string, database_name, **options)
    _connection.connect()


def is_connected() -> bool:
    """Check if MongoDB is connected."""
    return _connection is not None and _connection.is_connected()


def close() -> None:
    """Close MongoDB connection."""
    global _connection
    if _connection:
        _connection.close()
        _connection = None


def get_connection() -> MongoDBConnection:
    """Get the current MongoDB connection.

    Returns:
        MongoDB connection

    Raises:
        RuntimeError: If not connected
    """
    if _connection is None or not _connection.is_connected():
        raise RuntimeError("Not connected to MongoDB. Call mongodb_db.connect() first.")
    return _connection


# Namespace properties for convenient access
@property
def llm() -> LLMArchiveOperations:
    """LLM archiving operations."""
    return get_connection().llm


@property
def audit() -> AuditOperations:
    """Audit trail operations."""
    return get_connection().audit


@property
def query() -> QueryOperations:
    """Query operations."""
    return get_connection().query
```

### 3.3 Namespace Access Pattern

**New Usage**:
```python
from src.database import mongodb_db

# Connect (usually in application startup)
mongodb_db.connect()  # Reads MONGODB_URL from env

# Archive LLM request
mongodb_db.llm.archive(
    job_id="job-123",
    agent_type="creator",
    messages=[...],
    response=ai_message,
    model="gpt-4",
    latency_ms=1234,
    iteration=5,
)

# Audit agent step
mongodb_db.audit.audit_step(
    job_id="job-123",
    agent_type="creator",
    step_type=StepType.INITIALIZE,
    node_name="initialize",
    iteration=0,
    data={"state": {...}},
)

# Audit tool call/result
mongodb_db.audit.audit_tool_call(
    job_id="job-123",
    agent_type="creator",
    iteration=5,
    tool_name="read_file",
    call_id="call_xyz",
    arguments={"path": "file.txt"},
)

mongodb_db.audit.audit_tool_result(
    job_id="job-123",
    agent_type="creator",
    iteration=5,
    tool_name="read_file",
    call_id="call_xyz",
    result="file contents...",
    success=True,
    latency_ms=42,
)

# Query conversation
conversation = mongodb_db.query.get_conversation(job_id="job-123")

# Get statistics
stats = mongodb_db.query.get_job_stats(job_id="job-123")
audit_stats = mongodb_db.query.get_audit_stats(job_id="job-123")

# Get audit trail
audit_trail = mongodb_db.query.get_audit_trail(
    job_id="job-123",
    step_type=StepType.TOOL_CALL,
)

# Close (usually in application shutdown)
mongodb_db.close()
```

---

## 4. API Design

### 4.1 Connection Management

**Module-Level Functions**:
```python
# src/database/mongodb_db.py

def connect(
    connection_string: Optional[str] = None,
    database_name: str = "graphrag_logs",
    server_selection_timeout_ms: int = 5000,
    max_pool_size: int = 100,
    min_pool_size: int = 0,
    max_idle_time_ms: int = 30000,
) -> None:
    """Connect to MongoDB. Reads MONGODB_URL env var if connection_string not provided."""

def is_connected() -> bool:
    """Check if MongoDB is connected."""

def close() -> None:
    """Close MongoDB connection."""

def get_connection() -> MongoDBConnection:
    """Get the current connection (raises if not connected)."""
```

### 4.2 LLM Archive Operations

**Namespace**: `mongodb_db.llm`

```python
class LLMArchiveOperations:
    def archive(
        self,
        job_id: str,
        agent_type: str,
        messages: Sequence[BaseMessage],
        response: AIMessage,
        model: str,
        latency_ms: Optional[int] = None,
        iteration: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Archive an LLM request/response.

        Returns:
            Document ID (MongoDB ObjectId as string)
        """
```

**Document Schema**:
```python
{
    "_id": ObjectId("..."),
    "job_id": "550e8400-e29b-41d4-a716-446655440000",
    "agent_type": "creator",
    "timestamp": ISODate("2026-01-14T10:30:00.000Z"),
    "model": "gpt-4",
    "request": {
        "messages": [
            {"role": "system", "type": "SystemMessage", "content": "..."},
            {"role": "human", "type": "HumanMessage", "content": "..."},
        ],
        "message_count": 2
    },
    "response": {
        "role": "assistant",
        "type": "AIMessage",
        "content": "...",
        "tool_calls": [
            {"id": "call_abc", "name": "read_file", "args": {...}}
        ]
    },
    "latency_ms": 1234,
    "iteration": 5,
    "metadata": {...},
    "metrics": {
        "input_chars": 1500,
        "output_chars": 300,
        "tool_calls": 1
    }
}
```

### 4.3 Audit Operations

**Namespace**: `mongodb_db.audit`

```python
class AuditOperations:
    def audit_step(
        self,
        job_id: str,
        agent_type: str,
        step_type: StepType,
        node_name: str,
        iteration: int,
        data: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a single agent execution step.

        Returns:
            Document ID
        """

    def audit_tool_call(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        arguments: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a tool call before execution."""

    def audit_tool_result(
        self,
        job_id: str,
        agent_type: str,
        iteration: int,
        tool_name: str,
        call_id: str,
        result: str,
        success: bool,
        latency_ms: int,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Audit a tool result after execution."""
```

**StepType Enum**:
```python
class StepType(Enum):
    INITIALIZE = "initialize"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CHECK = "check"
    ROUTING = "routing"
    ERROR = "error"
```

**Audit Document Schema**:
```python
{
    "_id": ObjectId("..."),
    "job_id": "550e8400-e29b-41d4-a716-446655440000",
    "agent_type": "creator",
    "iteration": 5,
    "step_number": 42,
    "step_type": "tool_call",
    "node_name": "tools",
    "timestamp": ISODate("2026-01-14T10:30:01.234Z"),
    "latency_ms": 42,
    "metadata": {...},

    # Step-specific data (merged from `data` parameter)
    "tool": {
        "name": "read_file",
        "call_id": "call_xyz",
        "arguments": {"path": "file.txt"}
    }
}
```

### 4.4 Query Operations

**Namespace**: `mongodb_db.query`

```python
class QueryOperations:
    def get_conversation(
        self,
        job_id: str,
        agent_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get conversation history for a job."""

    def get_job_stats(self, job_id: str) -> Dict[str, Any]:
        """Get LLM usage statistics for a job.

        Returns:
            {
                "total_requests": 15,
                "total_input_chars": 45000,
                "total_output_chars": 12000,
                "total_tool_calls": 8,
                "avg_latency_ms": 1234.5,
                "max_latency_ms": 3000,
                "min_latency_ms": 500,
                "first_request": datetime(...),
                "last_request": datetime(...),
                "models_used": ["gpt-4", "gpt-3.5-turbo"],
            }
        """

    def get_recent_requests(
        self,
        limit: int = 50,
        agent_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent LLM requests across all jobs."""

    def get_audit_trail(
        self,
        job_id: str,
        step_type: Optional[StepType] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Get agent audit trail for a job."""

    def get_audit_stats(self, job_id: str) -> Dict[str, Any]:
        """Get audit trail statistics for a job.

        Returns:
            {
                "total_steps": 127,
                "max_iteration": 10,
                "first_step": datetime(...),
                "last_step": datetime(...),
                "by_step_type": {
                    "llm_call": {
                        "count": 10,
                        "avg_latency_ms": 1234.5,
                        "total_latency_ms": 12345,
                    },
                    "tool_call": {...},
                    ...
                },
            }
        """
```

---

## 5. Migration Plan

### Overview

**Duration**: 1-2 weeks
**Approach**: Parallel implementation (old code remains functional)
**Phases**: 4 phases

### Phase 1: Create New Module (Days 1-3)

**Goal**: Create `src/database/mongodb_db.py` alongside existing `src/core/archiver.py`

**Tasks**:

1. **Create MongoDBConnection class**:
   - Connection management with configurable pooling
   - Lazy initialization support
   - `connect()`, `close()`, `is_connected()`, `get_collection()`
   - Environment variable parsing

2. **Create LLMArchiveOperations class**:
   - `archive()` method
   - `_message_to_dict()` helper
   - Migrate message serialization logic

3. **Create AuditOperations class**:
   - `audit_step()` method
   - `audit_tool_call()` convenience method
   - `audit_tool_result()` convenience method
   - Step counter management
   - Truncation helpers

4. **Create QueryOperations class**:
   - `get_conversation()`
   - `get_job_stats()`
   - `get_recent_requests()`
   - `get_audit_trail()`
   - `get_audit_stats()`

5. **Create StepType enum**:
   - All step type constants

6. **Module-level functions**:
   - `connect()`, `close()`, `is_connected()`, `get_connection()`
   - Namespace properties (if possible in Python)

7. **Testing**:
   - Create `tests/test_mongodb_db.py`
   - Unit tests for all operations
   - Mock pymongo for testing
   - Test optional behavior (graceful degradation)

**Success Criteria**:
- [ ] New module passes all unit tests
- [ ] Can archive LLM requests
- [ ] Can audit agent steps
- [ ] Can query conversation and stats
- [ ] Gracefully handles missing pymongo
- [ ] No changes to existing code yet

### Phase 2: Update Initialization Scripts (Days 4-5)

**Goal**: Update initialization scripts to use new module

**Tasks**:

1. **Update `scripts/init_mongodb.py`**:
   ```python
   # Old
   from pymongo import MongoClient
   client = MongoClient(url)

   # New
   from src.database import mongodb_db
   mongodb_db.connect(url)
   # Use mongodb_db.get_connection().get_collection() for index creation
   ```

2. **Update `scripts/app_init.py`**:
   ```python
   # Old
   from scripts.init_mongodb import initialize_mongodb
   initialize_mongodb(logger, force_reset)

   # New (minimal changes)
   # init_mongodb.py should handle both old and new internally
   ```

3. **Testing**:
   - Test initialization with new module
   - Test `--force-reset` flag
   - Test graceful degradation

**Success Criteria**:
- [ ] Initialization scripts work with new module
- [ ] Collections and indexes created correctly
- [ ] No breaking changes for existing deployments

### Phase 3: Migrate Graph Usage (Days 6-8)

**Goal**: Update `src/graph.py` to use new module

**Tasks**:

1. **Create adapter layer in graph.py**:
   ```python
   # Option 1: Direct migration
   from src.database import mongodb_db

   # In initialize_workspace():
   if mongodb_db.is_connected():
       mongodb_db.audit.audit_step(
           job_id=job_id,
           agent_type=agent_type,
           step_type=StepType.INITIALIZE,
           ...
       )

   # Option 2: Backward compatible wrapper
   def get_auditor():
       """Get auditor (backward compatible)."""
       if mongodb_db.is_connected():
           return mongodb_db.audit
       return None

   auditor = get_auditor()
   if auditor:
       auditor.audit_step(...)
   ```

2. **Update all 15+ audit points**:
   - Replace `get_archiver()` calls
   - Update `audit_step()` calls to use `StepType` enum
   - Update `audit_tool_call()` and `audit_tool_result()` calls

3. **Update LLM archiving** (if any):
   - Replace direct `archiver.archive()` calls
   - Use `mongodb_db.llm.archive()`

4. **Testing**:
   - Run full agent workflow with new module
   - Verify all audit points working
   - Check MongoDB documents match expected schema
   - Test with MongoDB disabled (graceful degradation)

**Success Criteria**:
- [ ] All audit points in graph.py using new module
- [ ] Agent workflow runs end-to-end
- [ ] Audit trail captured correctly
- [ ] No errors when MongoDB unavailable

### Phase 4: Cleanup and Documentation (Days 9-10)

**Goal**: Remove old code and update documentation

**Tasks**:

1. **Remove old implementation**:
   - Delete `src/core/archiver.py`
   - Remove from `src/core/__init__.py` exports
   - Search for any remaining imports

2. **Update scripts**:
   - Update `scripts/view_llm_conversation.py` to use new module
   - Add support for new StepType enum

3. **Update documentation**:
   - Update `CLAUDE.md` with new module usage
   - Update `docs/db_refactor.md`
   - Add migration notes to CHANGELOG

4. **Final testing**:
   - Full system test (PostgreSQL + Neo4j + MongoDB)
   - Test all scripts (init, view, app_init)
   - Test agent workflows (creator, validator)
   - Test graceful degradation (no MongoDB)

**Success Criteria**:
- [ ] Old code removed
- [ ] No import errors
- [ ] All tests passing
- [ ] Documentation updated
- [ ] Full system test successful

---

## 6. Testing Strategy

### 6.1 Unit Tests

**File**: `tests/test_mongodb_db.py`

**Coverage**: 90%+ target

**Test Categories**:

1. **Connection Tests**:
   ```python
   def test_connect_with_env_var(monkeypatch):
       """Test connection using MONGODB_URL env var."""

   def test_connect_explicit_url():
       """Test connection with explicit URL."""

   def test_connect_database_name_extraction():
       """Test database name parsed from URL."""

   def test_connect_missing_pymongo():
       """Test graceful failure when pymongo not installed."""

   def test_close_connection():
       """Test connection cleanup."""

   def test_is_connected():
       """Test connection status check."""
   ```

2. **LLM Archive Tests**:
   ```python
   def test_archive_llm_request():
       """Test archiving LLM request/response."""

   def test_archive_with_tool_calls():
       """Test archiving response with tool calls."""

   def test_archive_message_serialization():
       """Test LangChain message conversion."""

   def test_archive_metrics_calculation():
       """Test token/character metrics."""

   def test_archive_not_connected():
       """Test graceful failure when not connected."""
   ```

3. **Audit Tests**:
   ```python
   def test_audit_step():
       """Test basic step auditing."""

   def test_audit_step_numbering():
       """Test sequential step numbers per job."""

   def test_audit_tool_call():
       """Test tool call auditing with argument truncation."""

   def test_audit_tool_result():
       """Test tool result auditing."""

   def test_audit_all_step_types():
       """Test all StepType enum values."""
   ```

4. **Query Tests**:
   ```python
   def test_get_conversation():
       """Test conversation retrieval."""

   def test_get_conversation_filter_by_agent():
       """Test agent type filtering."""

   def test_get_job_stats():
       """Test LLM statistics aggregation."""

   def test_get_recent_requests():
       """Test recent requests query."""

   def test_get_audit_trail():
       """Test audit trail retrieval."""

   def test_get_audit_trail_filter_by_step_type():
       """Test step type filtering."""

   def test_get_audit_stats():
       """Test audit statistics aggregation."""
   ```

5. **Error Handling Tests**:
   ```python
   def test_not_connected_error():
       """Test RuntimeError when calling methods before connect()."""

   def test_connection_failure():
       """Test handling of connection failures."""

   def test_query_empty_results():
       """Test queries returning no results."""
   ```

### 6.2 Integration Tests

**File**: `tests/integration/test_mongodb_integration.py`

**Requirements**: Running MongoDB instance

**Tests**:

1. **End-to-End Archiving**:
   ```python
   @pytest.mark.integration
   def test_full_llm_archive_workflow():
       """Test complete LLM archiving workflow."""
       mongodb_db.connect()

       # Archive request
       doc_id = mongodb_db.llm.archive(...)

       # Query back
       conversation = mongodb_db.query.get_conversation(job_id)
       assert len(conversation) == 1

       # Get stats
       stats = mongodb_db.query.get_job_stats(job_id)
       assert stats["total_requests"] == 1
   ```

2. **End-to-End Auditing**:
   ```python
   @pytest.mark.integration
   def test_full_audit_workflow():
       """Test complete audit trail workflow."""
       mongodb_db.connect()

       # Audit multiple steps
       mongodb_db.audit.audit_step(...)
       mongodb_db.audit.audit_tool_call(...)
       mongodb_db.audit.audit_tool_result(...)

       # Query audit trail
       trail = mongodb_db.query.get_audit_trail(job_id)
       assert len(trail) == 3
       assert trail[0]["step_number"] < trail[1]["step_number"]

       # Get audit stats
       stats = mongodb_db.query.get_audit_stats(job_id)
       assert stats["total_steps"] == 3
   ```

3. **Index Performance**:
   ```python
   @pytest.mark.integration
   def test_query_performance():
       """Test query performance with indexes."""
       # Insert 1000 documents
       # Query with different filters
       # Verify queries use indexes (explain)
   ```

### 6.3 Migration Validation Tests

**File**: `tests/test_mongodb_migration.py`

**Tests**:

1. **Backward Compatibility**:
   ```python
   def test_old_documents_readable():
       """Test reading documents created by old implementation."""
       # Insert document with old schema
       # Read with new implementation
       # Verify all fields accessible
   ```

2. **Schema Consistency**:
   ```python
   def test_new_documents_match_old_schema():
       """Test new implementation produces same schema."""
       # Compare document structures
       # Verify field names, types match
   ```

3. **Scripts Still Work**:
   ```python
   def test_view_llm_conversation_script():
       """Test view_llm_conversation.py works with new module."""
       # Run script against test database
       # Verify output correct
   ```

### 6.4 Test Fixtures

```python
# tests/conftest.py

import pytest
from unittest.mock import Mock, MagicMock

@pytest.fixture
def mock_mongo_client(monkeypatch):
    """Mock pymongo.MongoClient."""
    mock_client = MagicMock()
    mock_client.admin.command.return_value = {}  # ping succeeds

    mock_collection = MagicMock()
    mock_collection.insert_one.return_value = Mock(inserted_id="mock_id_123")
    mock_collection.find.return_value = []
    mock_collection.aggregate.return_value = []

    mock_db = MagicMock()
    mock_db.__getitem__.return_value = mock_collection

    mock_client.__getitem__.return_value = mock_db

    monkeypatch.setattr("pymongo.MongoClient", lambda *args, **kwargs: mock_client)
    return mock_client

@pytest.fixture
def mongodb_test_instance():
    """Real MongoDB instance for integration tests."""
    from src.database import mongodb_db

    # Connect to test database
    mongodb_db.connect("mongodb://localhost:27017/test_graphrag_logs")

    yield mongodb_db

    # Cleanup
    mongodb_db.close()

@pytest.fixture
def sample_langchain_messages():
    """Sample LangChain messages for testing."""
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

    return [
        SystemMessage(content="You are a helpful assistant"),
        HumanMessage(content="Hello"),
        AIMessage(content="Hi there!", tool_calls=[
            {"id": "call_abc", "name": "search", "args": {"query": "test"}}
        ]),
    ]
```

---

## 7. Implementation Checklist

### Phase 1: Create New Module
- [ ] Create `src/database/mongodb_db.py`
- [ ] Implement `MongoDBConnection` class
  - [ ] `__init__` with connection parameters
  - [ ] `connect()` method
  - [ ] `close()` method
  - [ ] `is_connected()` method
  - [ ] `get_collection()` method
  - [ ] Namespace properties (`llm`, `audit`, `query`)
- [ ] Implement `StepType` enum
- [ ] Implement `LLMArchiveOperations` class
  - [ ] `archive()` method
  - [ ] `_message_to_dict()` helper
  - [ ] Message serialization for all message types
- [ ] Implement `AuditOperations` class
  - [ ] `audit_step()` method
  - [ ] `audit_tool_call()` convenience method
  - [ ] `audit_tool_result()` convenience method
  - [ ] `_get_next_step_number()` helper
  - [ ] `_truncate()` helper
- [ ] Implement `QueryOperations` class
  - [ ] `get_conversation()` method
  - [ ] `get_job_stats()` method with aggregation
  - [ ] `get_recent_requests()` method
  - [ ] `get_audit_trail()` method
  - [ ] `get_audit_stats()` method with aggregation
- [ ] Implement module-level functions
  - [ ] `connect()`
  - [ ] `close()`
  - [ ] `is_connected()`
  - [ ] `get_connection()`
- [ ] Create `tests/test_mongodb_db.py`
  - [ ] Connection tests (6 tests)
  - [ ] LLM archive tests (5 tests)
  - [ ] Audit tests (5 tests)
  - [ ] Query tests (7 tests)
  - [ ] Error handling tests (3 tests)
- [ ] Run unit tests and achieve 90%+ coverage

### Phase 2: Update Initialization
- [ ] Update `scripts/init_mongodb.py`
  - [ ] Import new module
  - [ ] Use `mongodb_db.connect()`
  - [ ] Use `mongodb_db.get_connection().get_collection()` for indexes
  - [ ] Test with `--clear` flag
  - [ ] Test with `--check` flag
- [ ] Update `scripts/app_init.py` if needed
- [ ] Test initialization scripts
  - [ ] Test fresh initialization
  - [ ] Test with `--force-reset`
  - [ ] Test with existing data
  - [ ] Test graceful failure (MongoDB unavailable)

### Phase 3: Migrate Graph Usage
- [ ] Update `src/graph.py`
  - [ ] Replace `from .core.archiver import get_archiver`
  - [ ] Add `from src.database import mongodb_db`
  - [ ] Add `from src.database.mongodb_db import StepType`
  - [ ] Update initialization audit point (1x)
  - [ ] Update instructions read audit point (1x)
  - [ ] Update planning LLM call/response audit points (2x)
  - [ ] Update initial todos LLM call/response audit points (2x)
  - [ ] Update phase transition audit point (1x)
  - [ ] Update memory update LLM call/response audit points (2x)
  - [ ] Update todo creation LLM call/response audit points (2x)
  - [ ] Update inner loop LLM call/response audit points (2x)
  - [ ] Update tool call/result audit points (2x per tool)
  - [ ] Update check decision audit point (1x)
  - [ ] Update phase completion audit point (1x)
  - [ ] Update goal check audit points (3x)
  - [ ] Update error condition audit point (1x)
- [ ] Update `src/core/__init__.py`
  - [ ] Remove archiver imports/exports (or keep for compatibility)
- [ ] Test agent workflows
  - [ ] Run creator agent end-to-end
  - [ ] Run validator agent end-to-end
  - [ ] Verify audit trail captured
  - [ ] Check document schemas in MongoDB
  - [ ] Test with MongoDB disabled

### Phase 4: Cleanup and Documentation
- [ ] Remove old implementation
  - [ ] Delete `src/core/archiver.py` (backup first!)
  - [ ] Remove exports from `src/core/__init__.py`
  - [ ] Search for remaining imports: `grep -r "from.*archiver import" src/`
  - [ ] Search for remaining usage: `grep -r "get_archiver\|LLMArchiver" src/`
- [ ] Update scripts
  - [ ] Update `scripts/view_llm_conversation.py` for new StepType enum
  - [ ] Test all CLI commands
- [ ] Update documentation
  - [ ] Update `CLAUDE.md` with new module examples
  - [ ] Update `docs/db_refactor.md` with completion notes
  - [ ] Add migration notes to CHANGELOG
  - [ ] Add docstring examples to `mongodb_db.py`
- [ ] Final testing
  - [ ] Run full test suite: `pytest tests/`
  - [ ] Run integration tests: `pytest tests/integration/ -m integration`
  - [ ] Test full system initialization: `python scripts/app_init.py --force-reset`
  - [ ] Test creator workflow: `python agent.py --config creator --prompt "test"`
  - [ ] Test validator workflow: `python agent.py --config validator --prompt "test"`
  - [ ] Test MongoDB disabled: unset MONGODB_URL, run workflows
  - [ ] Test view scripts: `python scripts/view_llm_conversation.py --recent 10`
- [ ] Code review
  - [ ] Review all changed files
  - [ ] Check for consistent error handling
  - [ ] Verify type hints complete
  - [ ] Check docstrings complete

---

## 8. Risks and Mitigations

### Risk 1: Breaking Changes for Existing Deployments

**Probability**: Low
**Impact**: Medium

**Risk**: Deployments using MongoDB may break if migration not careful.

**Mitigation**:
- Keep old code functional during migration (Phase 1-3)
- Test backward compatibility with old documents
- Provide clear migration guide
- MongoDB is optional - failures don't block core functionality

### Risk 2: Step Numbering Inconsistencies

**Probability**: Medium
**Impact**: Low

**Risk**: In-memory step counters reset if connection re-established mid-job.

**Mitigation**:
- Document this behavior
- Consider storing last_step_number in MongoDB for resume capability
- Low impact: step_number is for ordering, timestamp is canonical

### Risk 3: Missing pymongo Dependency

**Probability**: Low
**Impact**: Low

**Risk**: Deployments without pymongo installed may have issues.

**Mitigation**:
- Optional import: `try: import pymongo except ImportError: ...`
- Graceful degradation already implemented
- Document pymongo as optional dependency in requirements.txt

### Risk 4: MongoDB Performance Under Load

**Probability**: Low
**Impact**: Medium

**Risk**: Many audit points may impact MongoDB performance.

**Mitigation**:
- Indexes already optimized for common queries
- Connection pooling configured
- Audit calls are non-blocking (fire-and-forget pattern)
- Consider batch insert API for future optimization

### Risk 5: Document Size Limits

**Probability**: Low
**Impact**: Low

**Risk**: Very large LLM requests may exceed MongoDB 16MB document limit.

**Mitigation**:
- Already implemented: truncation of large arguments/results
- Can add check for document size before insert
- Log warning if truncation occurs

---

## 9. Appendix

### 9.1 Implementation Details

| Aspect | Current Implementation |
|--------|------------------------|
| **Location** | `src/core/archiver.py` |
| **Lines of Code** | 720 lines |
| **Structure** | Single `LLMArchiver` class with clear separation of concerns |
| **Pattern** | ✅ **Dependency injection** - instances created and passed explicitly |
| **Connection** | Lazy connection via `from_env()` factory method |
| **API Style** | Method calls on class instance |
| **Testing** | Easy to mock with dependency injection |
| **Rationale** | MongoDB (pymongo) has proper connection pooling, no singleton needed |
| **Enums** | String literals for step types |
| **Error Handling** | Returns None on error, graceful degradation |
| **Testing** | Can be tested with mocking |
| **Documentation** | Docstrings + comments |

### 9.2 MongoDB Collections Schema

**Collection**: `llm_requests`

```javascript
{
  "_id": ObjectId("..."),
  "job_id": String,  // UUID
  "agent_type": String,  // "creator", "validator", etc.
  "timestamp": ISODate,
  "model": String,  // "gpt-4", etc.
  "request": {
    "messages": [
      {
        "type": String,  // "SystemMessage", "HumanMessage", etc.
        "role": String,  // "system", "human", "assistant", "tool"
        "content": String,
        "tool_calls": [  // Optional, for AIMessage
          {
            "id": String,
            "name": String,
            "args": Object
          }
        ],
        "tool_call_id": String,  // Optional, for ToolMessage
        "name": String,  // Optional, for ToolMessage
        "additional_kwargs": Object  // Optional
      }
    ],
    "message_count": Number
  },
  "response": {
    "type": String,
    "role": String,
    "content": String,
    "tool_calls": Array  // Optional
  },
  "latency_ms": Number,  // Optional
  "iteration": Number,  // Optional
  "metadata": Object,  // Optional
  "metrics": {
    "input_chars": Number,
    "output_chars": Number,
    "tool_calls": Number
  }
}
```

**Indexes**:
- `job_id` (ascending)
- `agent_type` (ascending)
- `timestamp` (ascending)
- `model` (ascending)
- `(job_id, agent_type, timestamp)` (compound, timestamp descending)

**Collection**: `agent_audit`

```javascript
{
  "_id": ObjectId("..."),
  "job_id": String,  // UUID
  "agent_type": String,
  "iteration": Number,
  "step_number": Number,  // Sequential per job
  "step_type": String,  // "initialize", "llm_call", "tool_call", etc.
  "node_name": String,  // "initialize", "process", "tools", "check"
  "timestamp": ISODate,
  "latency_ms": Number,  // Optional
  "metadata": Object,  // Optional

  // Step-specific fields (varies by step_type)
  "state": Object,  // For initialize, check steps
  "llm": Object,  // For llm_call, llm_response steps
  "tool": Object,  // For tool_call, tool_result steps
  "check": Object,  // For check steps
  "error": Object  // For error steps
}
```

**Indexes**:
- `job_id` (ascending)
- `step_type` (ascending)
- `node_name` (ascending)
- `timestamp` (ascending)
- `(job_id, step_number)` (compound)
- `(job_id, iteration, step_number)` (compound)
- `(job_id, agent_type, step_type)` (compound)

### 9.3 Example: Complete Migration for Single Audit Point

**Old Code** (`src/graph.py`, line ~169):
```python
from .core.archiver import get_archiver

def initialize_workspace(state):
    """Initialize workspace at job start."""
    # ... workspace setup ...

    # Audit workspace initialization
    auditor = get_archiver()
    if auditor:
        auditor.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type="initialize",
            node_name="initialize",
            iteration=0,
            data={"state": {"document_path": doc_path, ...}},
        )
```

**New Code** (same location):
```python
from src.database import mongodb_db
from src.database.mongodb_db import StepType

def initialize_workspace(state):
    """Initialize workspace at job start."""
    # ... workspace setup ...

    # Audit workspace initialization
    if mongodb_db.is_connected():
        mongodb_db.audit.audit_step(
            job_id=job_id,
            agent_type=agent_type,
            step_type=StepType.INITIALIZE,
            node_name="initialize",
            iteration=0,
            data={"state": {"document_path": doc_path, ...}},
        )
```

**Changes**:
1. Import changed: `get_archiver()` → `mongodb_db`
2. Added import: `StepType` enum
3. Conditional changed: `if auditor:` → `if mongodb_db.is_connected():`
4. Call changed: `auditor.audit_step(` → `mongodb_db.audit.audit_step(`
5. Parameter changed: `step_type="initialize"` → `step_type=StepType.INITIALIZE`

### 9.4 MongoDB URL Format

**Standard Format**:
```
mongodb://[username:password@]host[:port][/database][?options]
```

**Examples**:
```bash
# Local development
MONGODB_URL=mongodb://localhost:27017/graphrag_logs

# Docker Compose
MONGODB_URL=mongodb://mongo:27017/graphrag_logs

# MongoDB Atlas
MONGODB_URL=mongodb+srv://user:pass@cluster.mongodb.net/graphrag_logs?retryWrites=true&w=majority

# With authentication
MONGODB_URL=mongodb://admin:password@localhost:27017/graphrag_logs?authSource=admin
```

### 9.5 References

**Related Documents**:
- [db_refactor.md](db_refactor.md) - Overall database refactoring plan
- [postgres_migration_deep_dive.md](postgres_migration_deep_dive.md) - PostgreSQL migration
- [neo4j_migration_deep_dive.md](neo4j_migration_deep_dive.md) - Neo4j migration

**External Resources**:
- [pymongo Documentation](https://pymongo.readthedocs.io/)
- [MongoDB Manual](https://docs.mongodb.com/manual/)
- [Connection Pooling Best Practices](https://docs.mongodb.com/manual/administration/connection-pool-overview/)
- [Aggregation Pipeline](https://docs.mongodb.com/manual/aggregation/)

**Code Locations**:
- Current implementation: `src/core/archiver.py`
- Graph usage: `src/graph.py` (15+ audit points)
- Initialization: `scripts/init_mongodb.py`
- Viewing tool: `scripts/view_llm_conversation.py`
- App init: `scripts/app_init.py`

---

**End of MongoDB Migration Deep Dive**
