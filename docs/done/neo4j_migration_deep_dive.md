# Neo4j Migration Deep Dive

**Date:** 2026-01-14
**Status:** ✅ **COMPLETE** (Phase 1/2 implemented)
**Related:** `docs/db_refactor.md`, `docs/phase1_complete.md`

## Executive Summary

This document provides a comprehensive analysis of the Neo4j migration from the old `neo4j_utils.py` module-level functions to a clean `Neo4jDB` class-based architecture with **dependency injection**. Unlike PostgreSQL which is async-first, Neo4j uses the official **synchronous** Python driver and remains synchronous in the new architecture.

**Migration Status:** ✅ **COMPLETE**
- ✅ Phase 1: `Neo4jDB` class created with namespace pattern
- ✅ Phase 2: UniversalAgent and tools (graph_tools) migrated to use Neo4jDB
- ✅ No critical bugs found (implementation was already correctly using sync driver)

**Key Design Decision:** No singleton pattern. Each component creates its own `Neo4jDB` instance and passes it via dependency injection. This approach works because the official Neo4j driver has built-in connection pooling that handles concurrency correctly. Singletons were only needed for SQLite to avoid write conflicts.

### Implementation Highlights

1. **Clean Migration**: Neo4j implementation was already well-structured, so migration was straightforward
2. **Driver**: Using official `neo4j` Python driver (synchronous, thread-safe)
3. **Usage Pattern**: Session-based queries with automatic connection pooling
4. **Namespace Pattern**: `neo4j_db.requirements`, `neo4j_db.entities`, `neo4j_db.relationships`, `neo4j_db.statistics`
5. **Caching Strategy**: Tools layer caches entity lists for performance (unchanged)
6. **Metamodel Integration**: Well-integrated with metamodel validator (unchanged)

### Architecture Benefits Achieved

- ✅ **Unified interface** - Consistent API across database types
- ✅ **Namespace pattern** - Clear organization of operations
- ✅ **Dependency injection** - Each component creates and manages its own instance
- ✅ **Better testing** - Isolated, mockable components
- ✅ **Backward compatibility** - Old `Neo4jConnection` still available during transition

---

## Table of Contents

1. [Current State Analysis](#current-state-analysis)
2. [Usage Patterns](#usage-patterns)
3. [Target Architecture](#target-architecture)
4. [API Design with Dataclasses](#api-design-with-dataclasses)
5. [Query Organization](#query-organization)
6. [Migration Plan](#migration-plan)
7. [Testing Strategy](#testing-strategy)
8. [Implementation Checklist](#implementation-checklist)
9. [Appendix](#appendix)

---

## Current State Analysis

### File: `src/database/neo4j_utils.py` (162 lines)

#### Class: `Neo4jConnection` (Lines 12-139)

**Purpose**: Synchronous Neo4j connection handler with session management.

**Key Methods**:

```python
class Neo4jConnection:
    def __init__(self, uri: str, username: str, password: str):
        """Initialize connection parameters (doesn't connect yet)."""
        self.uri = uri
        self.username = username
        self.password = password
        self.driver = None

    def connect(self) -> bool:
        """Establish driver connection with connection pool."""
        try:
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password)
            )
            self.driver.verify_connectivity()  # Test connection
            print(f"✓ Successfully connected to Neo4j at {self.uri}")
            return True
        except AuthError:
            print(f"✗ Authentication failed for Neo4j database")
            return False
        except ServiceUnavailable:
            print(f"✗ Neo4j service unavailable at {self.uri}")
            return False

    def close(self):
        """Close the database connection."""
        if self.driver:
            self.driver.close()

    def execute_query(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results.

        Creates a new session for each query, executes, and returns results
        as a list of dictionaries.
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
        """Retrieve the database schema."""
        schema = {
            'node_labels': [],
            'relationship_types': [],
            'property_keys': []
        }

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

    def get_sample_data(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve sample data from the database."""
        query = f"""
        MATCH (n)
        RETURN labels(n) as labels, properties(n) as properties
        LIMIT {limit}
        """
        return self.execute_query(query)
```

#### Factory Function: `create_neo4j_connection()` (Lines 141-161)

```python
def create_neo4j_connection() -> Neo4jConnection:
    """Create Neo4j connection from environment variables.

    Reads NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD from environment.
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
```

### Strengths of Current Implementation

1. **Correct Driver Usage**: Uses official synchronous driver correctly
2. **Error Handling**: Proper exception catching with specific error types
3. **Connection Pooling**: Driver automatically manages connection pool
4. **Session Management**: Properly uses context managers for sessions
5. **Simple Interface**: Clean, straightforward API
6. **Schema Introspection**: Built-in schema discovery methods

### Weaknesses/Limitations

1. **No Parameter Validation**: Functions accept raw strings without validation
2. **Print-based Logging**: Uses `print()` instead of proper logging
3. **No Transaction Support**: No explicit transaction management API
4. **Result Type Safety**: Returns `List[Dict[str, Any]]` - no type hints for result structure
5. **No Query Organization**: Queries embedded as strings throughout codebase
6. **No Connection Retry**: No automatic reconnection on connection loss
7. **No Query Timeout**: No configurable query timeout
8. **No Result Pagination**: Large result sets consume memory

### File: `src/tools/graph_tools.py` (735 lines)

This file contains all Neo4j-based tools for the Validator Agent.

#### Tool Creation Pattern

```python
def create_graph_tools(context: ToolContext) -> List:
    """Create graph tools with injected context.

    Args:
        context: ToolContext with dependencies (must include neo4j_conn)

    Returns:
        List of LangChain tool functions
    """
    neo4j = context.neo4j_conn  # Get Neo4jConnection from context
    config = context.config or {}
    duplicate_threshold = config.get("duplicate_threshold", 0.95)

    # Entity caches (module-level state)
    _requirements_cache: Optional[List] = None
    _business_objects_cache: Optional[List] = None
    _messages_cache: Optional[List] = None
    _schema_cache: Optional[Dict] = None

    # Cache loading functions
    def load_requirements() -> List:
        nonlocal _requirements_cache
        if _requirements_cache is None and neo4j:
            query = """
            MATCH (r:Requirement)
            RETURN r.rid AS rid, r.name AS name, r.text AS text,
                   r.type AS type, r.goBDRelevant AS gobd_relevant,
                   r.complianceStatus AS compliance_status
            LIMIT 1000
            """
            _requirements_cache = neo4j.execute_query(query)
        return _requirements_cache or []
```

#### Key Tools

**1. `execute_cypher_query(query: str)` - Direct Cypher execution**
- Runs arbitrary Cypher queries
- Limits output to 50 records for display
- Used for ad-hoc exploration

**2. `find_similar_requirements(text: str, threshold: float)` - Similarity search**
- Uses Python `SequenceMatcher` for text similarity
- Queries all requirements (up to 1000) and compares in memory
- Returns top 10 matches above threshold

**3. `check_for_duplicates(text: str)` - Duplicate detection**
- High-threshold similarity check (95%)
- Prevents duplicate requirement creation

**4. `resolve_business_object(mention: str)` - Entity resolution**
- Fuzzy matching for BusinessObject entities
- Supports exact match, substring match, similarity score

**5. `resolve_message(mention: str)` - Message resolution**
- Similar to business object resolution for Message entities

**6. `validate_schema_compliance(check_type: str)` - Metamodel validation**
- Integrates with `MetamodelValidator`
- Runs structural, relationship, or quality checks
- Returns formatted compliance report

**7. `create_requirement_node(...)` - Node creation**
- Creates new Requirement nodes
- Sets default properties (createdAt, createdBy, status)
- Invalidates cache after creation

**8. `create_fulfillment_relationship(...)` - Relationship creation**
- Creates fulfillment relationships (FULFILLED_BY_*, NOT_FULFILLED_BY_*)
- Sets validation metadata (confidence, evidence, timestamps)

**9. `generate_requirement_id()` - ID generation**
- Queries for highest R-XXXX ID
- Generates next sequential ID

**10. `get_entity_relationships(entity_id, entity_type)` - Relationship queries**
- Gets all requirement relationships for an entity
- Returns relationship type and confidence

**11. `count_graph_statistics()` - Statistics**
- Counts nodes and relationships by type

#### Caching Strategy

The tools use closure-based caching:
- Cache is scoped to tool creation (per-agent lifecycle)
- Cache invalidated manually after mutations (e.g., `create_requirement_node`)
- Reduces redundant queries for entity lists

**Issue**: Cache invalidation is incomplete:
- Only `_requirements_cache` is cleared after requirement creation
- `_business_objects_cache` and `_messages_cache` not cleared if entities created elsewhere
- No TTL or automatic expiration

### File: `src/utils/metamodel_validator.py` (637 lines)

Comprehensive validation framework for the FINIUS metamodel.

#### Metamodel Definition (Lines 105-136)

```python
class MetamodelValidator:
    # Node types
    ALLOWED_NODE_LABELS = {"Requirement", "BusinessObject", "Message"}

    # Required ID properties
    REQUIRED_PROPERTIES = {
        "Requirement": ["rid"],
        "BusinessObject": ["boid"],
        "Message": ["mid"],
    }

    # Allowed relationships (source_label, rel_type, target_label)
    ALLOWED_RELATIONSHIPS = {
        # Requirement ↔ Requirement
        ("Requirement", "REFINES", "Requirement"),
        ("Requirement", "DEPENDS_ON", "Requirement"),
        ("Requirement", "TRACES_TO", "Requirement"),
        ("Requirement", "SUPERSEDES", "Requirement"),  # v2.0

        # Requirement ↔ BusinessObject
        ("Requirement", "RELATES_TO_OBJECT", "BusinessObject"),
        ("Requirement", "IMPACTS_OBJECT", "BusinessObject"),
        ("Requirement", "FULFILLED_BY_OBJECT", "BusinessObject"),  # v2.0
        ("Requirement", "NOT_FULFILLED_BY_OBJECT", "BusinessObject"),  # v2.0

        # Requirement ↔ Message
        ("Requirement", "RELATES_TO_MESSAGE", "Message"),
        ("Requirement", "IMPACTS_MESSAGE", "Message"),
        ("Requirement", "FULFILLED_BY_MESSAGE", "Message"),  # v2.0
        ("Requirement", "NOT_FULFILLED_BY_MESSAGE", "Message"),  # v2.0

        # Message ↔ BusinessObject
        ("Message", "USES_OBJECT", "BusinessObject"),
        ("Message", "PRODUCES_OBJECT", "BusinessObject"),
    }
```

#### Check Categories

**Category A: Structural Constraints (ERROR severity)**
- A1: Node labels - All nodes have valid labels
- A2: Unique constraints - ID properties are unique
- A3: Required properties - Required properties present

**Category B: Relationship Constraints (ERROR severity)**
- B1: Relationship types - All relationships are from allowed set
- B2: Relationship directions - Correct source→target label pairs
- B3: Self-loops - No invalid self-referential relationships

**Category C: Quality Gates (WARNING severity)**
- C1: Orphan requirements - Requirements connected to BO/Message
- C2: Message content - Messages have USES_OBJECT relationships
- C3: GoBD traceability - GoBD items have IMPACTS_* relationships
- C4: Unfulfilled requirements - Identify open/unfulfilled requirements
- C5: Compliance status consistency - complianceStatus matches relationships

#### Report Generation

```python
@dataclass
class CheckResult:
    check_id: str
    check_name: str
    passed: bool
    severity: Severity
    message: str
    violations: list[dict] = field(default_factory=list)
    query_used: str = ""
    execution_time_ms: float = 0.0

@dataclass
class ComplianceReport:
    timestamp: datetime
    passed: bool
    error_count: int
    warning_count: int
    checks_performed: list[str]
    results: list[CheckResult]

    def to_dict(self) -> dict:
        """Serialize for JSON storage/transmission."""

    def format_summary(self) -> str:
        """Format a human-readable summary."""
```

### File: `data/metamodell.cql` (285 lines)

Complete metamodel definition with:
- Constraint definitions (UNIQUE constraints for IDs)
- Index definitions (for performance)
- Relationship type documentation
- Node property specifications
- MERGE templates for safe upserts
- Quality gate queries
- Compliance status update procedures
- Example data

**Key Features:**
- Uses `MERGE` instead of `CREATE` for idempotency
- Includes `ON CREATE SET` and `ON MATCH SET` clauses
- Proper handling of datetime properties
- GoBD/GDPR relevance flags
- Fulfillment relationship properties (confidence, evidence, citationId)

---

## Usage Patterns

### 1. Agent Initialization (`src/agent.py:177-187`)

```python
# Neo4j connection (optional, based on config)
if self.neo4j_conn is None and self.config.connections.neo4j:
    from src.database.neo4j_utils import Neo4jConnection
    neo4j_uri = os.getenv("NEO4J_URI")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD")

    if neo4j_uri and neo4j_password:
        self.neo4j_conn = Neo4jConnection(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        if not self.neo4j_conn.connect():
            logger.warning("Failed to connect to Neo4j")
            self.neo4j_conn = None
```

**Pattern**: Conditional initialization based on config flag.

### 2. Tool Context Injection (`src/tools/context.py`)

```python
@dataclass
class ToolContext:
    workspace_manager: Optional[WorkspaceManager] = None
    todo_manager: Optional[Any] = None
    postgres_conn: Optional[Any] = None
    neo4j_conn: Optional[Any] = None  # Neo4jConnection instance
    # ...

    def has_neo4j(self) -> bool:
        """Check if Neo4j connection is available."""
        return self.neo4j_conn is not None
```

**Pattern**: Dependency injection through dataclass context.

### 3. Direct Query Execution (graph_tools.py)

```python
@tool
def execute_cypher_query(query: str) -> str:
    """Execute a Cypher query against the Neo4j database."""
    if not neo4j:
        return "Error: No Neo4j connection available"

    try:
        results = neo4j.execute_query(query)
        # Format and return results
        return formatted_results
    except Exception as e:
        return f"Error executing query: {str(e)}"
```

**Pattern**: Error handling with graceful degradation.

### 4. Cached Entity Queries (graph_tools.py)

```python
def load_requirements() -> List:
    nonlocal _requirements_cache
    if _requirements_cache is None and neo4j:
        query = """
        MATCH (r:Requirement)
        RETURN r.rid AS rid, r.name AS name, r.text AS text
        LIMIT 1000
        """
        _requirements_cache = neo4j.execute_query(query)
    return _requirements_cache or []
```

**Pattern**: Lazy loading with module-level cache.

### 5. Node Creation with Property Mapping

```python
@tool
def create_requirement_node(
    rid: str, name: str, text: str, req_type: str = "functional",
    priority: str = "medium", gobd_relevant: bool = False,
    gdpr_relevant: bool = False, compliance_status: str = "open"
) -> str:
    query = """
    CREATE (r:Requirement {
        rid: $rid,
        name: $name,
        text: $text,
        type: $type,
        priority: $priority,
        status: 'active',
        goBDRelevant: $gobd_relevant,
        gdprRelevant: $gdpr_relevant,
        complianceStatus: $compliance_status,
        createdAt: datetime(),
        createdBy: 'validator_agent'
    })
    RETURN r.rid AS rid
    """

    results = neo4j.execute_query(query, {
        "rid": rid,
        "name": name,
        "text": text,
        "type": req_type,
        "priority": priority,
        "gobd_relevant": gobd_relevant,
        "gdpr_relevant": gdpr_relevant,
        "compliance_status": compliance_status,
    })
```

**Pattern**: Parameterized queries with explicit parameter mapping.

### 6. Metamodel Validation

```python
from src.utils.metamodel_validator import MetamodelValidator, Severity

validator = MetamodelValidator(neo4j)
report = validator.run_all_checks()

if not report.passed:
    print(report.format_summary())
    for result in report.results:
        if result.severity == Severity.ERROR:
            print(f"ERROR: {result.message}")
```

**Pattern**: Validation as a service with structured reporting.

---

## Target Architecture

### Module Structure

```
src/database/
├── __init__.py           # Exports: postgres_db, neo4j_db, neo4j_validator
├── postgres_db.py        # PostgresDB class (async)
├── neo4j_db.py          # Neo4jDB class (sync) ← NEW
├── metamodel_validator.py  # Move from src/utils/
└── queries/
    ├── postgres/         # PostgreSQL queries (.sql files)
    │   ├── jobs.sql
    │   └── requirements.sql
    └── neo4j/           # Neo4j queries (.cypher files) ← NEW
        ├── requirements.cypher
        ├── entities.cypher
        ├── relationships.cypher
        ├── validation.cypher
        └── statistics.cypher
```

### Core Class: `Neo4jDB`

```python
class Neo4jDB:
    """Neo4j database manager with connection pooling.

    Synchronous interface using official neo4j Python driver.
    Provides namespace-based operations and transaction support.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        max_connection_lifetime: int = 3600,
        max_connection_pool_size: int = 50,
        connection_timeout: float = 30.0,
        query_timeout: float = 60.0,
    ):
        """Initialize Neo4j database manager.

        Args:
            uri: Neo4j URI (bolt://host:7687). Defaults to NEO4J_URI env var.
            username: Neo4j username. Defaults to NEO4J_USERNAME env var.
            password: Neo4j password. Defaults to NEO4J_PASSWORD env var.
            max_connection_lifetime: Max connection lifetime in seconds.
            max_connection_pool_size: Max connections in pool.
            connection_timeout: Connection timeout in seconds.
            query_timeout: Default query timeout in seconds.
        """
        self.uri = uri or os.getenv("NEO4J_URI")
        self.username = username or os.getenv("NEO4J_USERNAME", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD")

        if not all([self.uri, self.password]):
            raise ValueError("Neo4j URI and password required")

        self._driver: Optional[Driver] = None
        self._max_connection_lifetime = max_connection_lifetime
        self._max_connection_pool_size = max_connection_pool_size
        self._connection_timeout = connection_timeout
        self._default_query_timeout = query_timeout
        self._logger = logging.getLogger(__name__)

        # Namespace objects (created on demand)
        self._requirements: Optional[RequirementOperations] = None
        self._entities: Optional[EntityOperations] = None
        self._relationships: Optional[RelationshipOperations] = None
        self._statistics: Optional[StatisticsOperations] = None

    def connect(self) -> None:
        """Establish driver connection with connection pooling."""
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                max_connection_lifetime=self._max_connection_lifetime,
                max_connection_pool_size=self._max_connection_pool_size,
                connection_timeout=self._connection_timeout,
            )
            self._driver.verify_connectivity()
            self._logger.info(f"Neo4j connection pool established: {self.uri}")

    def disconnect(self) -> None:
        """Close driver and all connections."""
        if self._driver:
            self._driver.close()
            self._driver = None
            self._logger.info("Neo4j connection pool closed")

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results.

        Args:
            query: Cypher query string
            parameters: Query parameters
            database: Target database (optional, defaults to default DB)
            timeout: Query timeout in seconds (optional, uses default)

        Returns:
            List of result records as dictionaries

        Raises:
            RuntimeError: If not connected
            neo4j.exceptions.*: Various Neo4j errors
        """
        if not self._driver:
            raise RuntimeError("Not connected. Call connect() first.")

        timeout = timeout or self._default_query_timeout

        with self._driver.session(database=database) as session:
            result = session.run(
                query,
                parameters or {},
                timeout=timeout
            )
            records = [dict(record) for record in result]
            self._logger.debug(
                f"Query returned {len(records)} records",
                extra={"query": query[:100]}
            )
            return records

    @contextmanager
    def transaction(
        self,
        database: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """Execute operations in an explicit transaction.

        Args:
            database: Target database (optional)
            timeout: Transaction timeout in seconds (optional)

        Yields:
            Transaction object for executing queries

        Example:
            with neo4j_db.transaction() as tx:
                tx.run(query1, params1)
                tx.run(query2, params2)
                # Automatic commit on success, rollback on exception
        """
        if not self._driver:
            raise RuntimeError("Not connected. Call connect() first.")

        timeout = timeout or self._default_query_timeout

        with self._driver.session(database=database) as session:
            with session.begin_transaction() as tx:
                try:
                    yield tx
                    tx.commit()
                    self._logger.debug("Transaction committed")
                except Exception as e:
                    tx.rollback()
                    self._logger.error(f"Transaction rolled back: {e}")
                    raise

    @property
    def requirements(self) -> "RequirementOperations":
        """Requirement node operations."""
        if self._requirements is None:
            self._requirements = RequirementOperations(self)
        return self._requirements

    @property
    def entities(self) -> "EntityOperations":
        """BusinessObject and Message operations."""
        if self._entities is None:
            self._entities = EntityOperations(self)
        return self._entities

    @property
    def relationships(self) -> "RelationshipOperations":
        """Relationship creation and queries."""
        if self._relationships is None:
            self._relationships = RelationshipOperations(self)
        return self._relationships

    @property
    def statistics(self) -> "StatisticsOperations":
        """Graph statistics and metrics."""
        if self._statistics is None:
            self._statistics = StatisticsOperations(self)
        return self._statistics

    def get_schema(self) -> Dict[str, List[str]]:
        """Get database schema."""
        schema = {
            "node_labels": [],
            "relationship_types": [],
            "property_keys": [],
        }

        # Node labels
        result = self.execute_query("CALL db.labels()")
        schema["node_labels"] = [r["label"] for r in result]

        # Relationship types
        result = self.execute_query("CALL db.relationshipTypes()")
        schema["relationship_types"] = [r["relationshipType"] for r in result]

        # Property keys
        result = self.execute_query("CALL db.propertyKeys()")
        schema["property_keys"] = [r["propertyKey"] for r in result]

        return schema

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._driver is not None

    def verify_connectivity(self) -> bool:
        """Verify connection is alive."""
        if not self._driver:
            return False
        try:
            self._driver.verify_connectivity()
            return True
        except Exception as e:
            self._logger.error(f"Connectivity check failed: {e}")
            return False
```

### Namespace Classes

#### 1. RequirementOperations

```python
class RequirementOperations:
    """Operations for Requirement nodes."""

    def __init__(self, db: Neo4jDB):
        self._db = db
        self._queries = QueryLoader("neo4j/requirements.cypher")

    def create(self, req: CreateRequirementRequest) -> Requirement:
        """Create a new Requirement node."""
        query = self._queries.get("create_requirement")
        params = {
            "rid": req.rid,
            "name": req.name,
            "text": req.text,
            "type": req.type,
            "priority": req.priority,
            "status": req.status or "active",
            "gobd_relevant": req.gobd_relevant,
            "gdpr_relevant": req.gdpr_relevant,
            "compliance_status": req.compliance_status or "open",
            "created_by": req.created_by or "system",
        }

        result = self._db.execute_query(query, params)
        if not result:
            raise RuntimeError(f"Failed to create requirement {req.rid}")

        return Requirement.from_neo4j_record(result[0])

    def get_by_rid(self, rid: str) -> Optional[Requirement]:
        """Get requirement by RID."""
        query = self._queries.get("get_requirement_by_rid")
        result = self._db.execute_query(query, {"rid": rid})
        if result:
            return Requirement.from_neo4j_record(result[0])
        return None

    def find_similar(self, text: str, threshold: float = 0.7) -> List[SimilarRequirement]:
        """Find requirements with similar text (in-memory comparison)."""
        # Load all requirements (cached in production)
        all_reqs = self.list(limit=1000)

        text_lower = text.lower().strip()
        similar = []

        for req in all_reqs:
            req_text = (req.text or "").lower().strip()
            if not req_text:
                continue

            similarity = SequenceMatcher(None, text_lower, req_text).ratio()

            if similarity >= threshold:
                similar.append(SimilarRequirement(
                    requirement=req,
                    similarity_score=similarity,
                ))

        similar.sort(key=lambda x: x.similarity_score, reverse=True)
        return similar[:10]

    def check_duplicate(self, text: str, threshold: float = 0.95) -> Optional[Requirement]:
        """Check if text is a duplicate (high threshold)."""
        similar = self.find_similar(text, threshold)
        if similar:
            return similar[0].requirement
        return None

    def list(
        self,
        filters: Optional[RequirementFilters] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Requirement]:
        """List requirements with optional filters."""
        query = self._queries.get("list_requirements")
        params = {
            "limit": limit,
            "offset": offset,
            **(filters.to_params() if filters else {}),
        }

        result = self._db.execute_query(query, params)
        return [Requirement.from_neo4j_record(r) for r in result]

    def update_compliance_status(self, rid: str) -> str:
        """Recalculate and update compliance status based on relationships."""
        query = self._queries.get("update_compliance_status")
        result = self._db.execute_query(query, {"rid": rid})
        if result:
            return result[0]["complianceStatus"]
        return "open"

    def generate_next_rid(self) -> str:
        """Generate next sequential RID (R-XXXX format)."""
        query = self._queries.get("get_max_rid")
        result = self._db.execute_query(query)

        if result and result[0].get("rid"):
            last_rid = result[0]["rid"]
            match = re.search(r"R-(\d+)", last_rid)
            if match:
                next_num = int(match.group(1)) + 1
                return f"R-{next_num:04d}"

        return "R-0001"
```

#### 2. EntityOperations

```python
class EntityOperations:
    """Operations for BusinessObject and Message nodes."""

    def __init__(self, db: Neo4jDB):
        self._db = db
        self._queries = QueryLoader("neo4j/entities.cypher")

    def get_business_object(self, boid: str) -> Optional[BusinessObject]:
        """Get BusinessObject by BOID."""
        query = self._queries.get("get_business_object")
        result = self._db.execute_query(query, {"boid": boid})
        if result:
            return BusinessObject.from_neo4j_record(result[0])
        return None

    def get_message(self, mid: str) -> Optional[Message]:
        """Get Message by MID."""
        query = self._queries.get("get_message")
        result = self._db.execute_query(query, {"mid": mid})
        if result:
            return Message.from_neo4j_record(result[0])
        return None

    def list_business_objects(self, limit: int = 100) -> List[BusinessObject]:
        """List all BusinessObjects."""
        query = self._queries.get("list_business_objects")
        result = self._db.execute_query(query, {"limit": limit})
        return [BusinessObject.from_neo4j_record(r) for r in result]

    def list_messages(self, limit: int = 100) -> List[Message]:
        """List all Messages."""
        query = self._queries.get("list_messages")
        result = self._db.execute_query(query, {"limit": limit})
        return [Message.from_neo4j_record(r) for r in result]

    def resolve_business_object(self, mention: str) -> Optional[BusinessObjectMatch]:
        """Resolve a text mention to a BusinessObject."""
        all_bos = self.list_business_objects()
        mention_lower = mention.lower().strip()

        best_match = None
        best_score = 0.0

        for bo in all_bos:
            name = (bo.name or "").lower()

            # Exact match
            if mention_lower == name:
                return BusinessObjectMatch(business_object=bo, match_score=1.0)

            # Substring match
            if mention_lower in name or name in mention_lower:
                score = 0.8
                if score > best_score:
                    best_match = bo
                    best_score = score
                continue

            # Similarity match
            score = SequenceMatcher(None, mention_lower, name).ratio()
            if score > best_score:
                best_match = bo
                best_score = score

        if best_match and best_score >= 0.6:
            return BusinessObjectMatch(business_object=best_match, match_score=best_score)

        return None

    def resolve_message(self, mention: str) -> Optional[MessageMatch]:
        """Resolve a text mention to a Message (same pattern as BO)."""
        # Similar implementation to resolve_business_object
        pass
```

#### 3. RelationshipOperations

```python
class RelationshipOperations:
    """Operations for creating and querying relationships."""

    def __init__(self, db: Neo4jDB):
        self._db = db
        self._queries = QueryLoader("neo4j/relationships.cypher")

    def create_fulfillment(
        self,
        req: CreateFulfillmentRequest,
    ) -> FulfillmentRelationship:
        """Create a fulfillment relationship (FULFILLED_BY_* or NOT_FULFILLED_BY_*)."""
        query = self._queries.get(f"create_{req.relationship_type.lower()}")
        params = {
            "rid": req.requirement_rid,
            "entity_id": req.entity_id,
            "confidence": req.confidence,
            "evidence": req.evidence,
            "gap_description": req.gap_description,
            "severity": req.severity,
            "remediation": req.remediation,
            "citation_id": req.citation_id,
        }

        result = self._db.execute_query(query, params)
        if not result:
            raise RuntimeError(f"Failed to create fulfillment relationship")

        return FulfillmentRelationship.from_neo4j_record(result[0])

    def create_requirement_relationship(
        self,
        req: CreateRequirementRelationshipRequest,
    ) -> RequirementRelationship:
        """Create a Requirement→Requirement relationship (REFINES, DEPENDS_ON, etc.)."""
        query = self._queries.get(f"create_{req.relationship_type.lower()}")
        params = {
            "source_rid": req.source_rid,
            "target_rid": req.target_rid,
            "rationale": req.rationale,
            "introduced_in": req.introduced_in,
        }

        result = self._db.execute_query(query, params)
        if not result:
            raise RuntimeError(f"Failed to create requirement relationship")

        return RequirementRelationship.from_neo4j_record(result[0])

    def get_requirement_relationships(
        self,
        rid: str,
        direction: str = "both",  # "outgoing", "incoming", "both"
    ) -> List[RelationshipInfo]:
        """Get all relationships for a requirement."""
        if direction == "both":
            query = self._queries.get("get_requirement_relationships_both")
        elif direction == "outgoing":
            query = self._queries.get("get_requirement_relationships_out")
        else:
            query = self._queries.get("get_requirement_relationships_in")

        result = self._db.execute_query(query, {"rid": rid})
        return [RelationshipInfo.from_neo4j_record(r) for r in result]

    def get_entity_relationships(
        self,
        entity_id: str,
        entity_type: str,
    ) -> List[RelationshipInfo]:
        """Get all requirement relationships for a BusinessObject or Message."""
        query = self._queries.get("get_entity_relationships")
        params = {
            "entity_id": entity_id,
            "entity_type": entity_type,
        }

        result = self._db.execute_query(query, params)
        return [RelationshipInfo.from_neo4j_record(r) for r in result]
```

#### 4. StatisticsOperations

```python
class StatisticsOperations:
    """Operations for graph statistics and metrics."""

    def __init__(self, db: Neo4jDB):
        self._db = db
        self._queries = QueryLoader("neo4j/statistics.cypher")

    def count_nodes(self) -> Dict[str, int]:
        """Count nodes by label."""
        query = self._queries.get("count_nodes_by_label")
        result = self._db.execute_query(query)
        return {r["label"]: r["count"] for r in result}

    def count_relationships(self) -> Dict[str, int]:
        """Count relationships by type."""
        query = self._queries.get("count_relationships_by_type")
        result = self._db.execute_query(query)
        return {r["type"]: r["count"] for r in result}

    def compliance_summary(self) -> ComplianceSummary:
        """Get compliance status summary."""
        query = self._queries.get("compliance_summary")
        result = self._db.execute_query(query)
        if result:
            return ComplianceSummary.from_neo4j_record(result[0])
        return ComplianceSummary(open=0, partial=0, fulfilled=0)

    def gobd_statistics(self) -> GoBDStatistics:
        """Get GoBD-specific statistics."""
        query = self._queries.get("gobd_statistics")
        result = self._db.execute_query(query)
        if result:
            return GoBDStatistics.from_neo4j_record(result[0])
        return GoBDStatistics()
```

### Query Loader

```python
class QueryLoader:
    """Load and manage named Cypher queries from .cypher files."""

    def __init__(self, filepath: str):
        """Initialize query loader.

        Args:
            filepath: Path to .cypher file relative to queries/neo4j/
        """
        self._filepath = Path(__file__).parent / "queries" / "neo4j" / filepath
        self._queries: Dict[str, str] = {}
        self._load_queries()

    def _load_queries(self) -> None:
        """Parse .cypher file and extract named queries."""
        if not self._filepath.exists():
            raise FileNotFoundError(f"Query file not found: {self._filepath}")

        content = self._filepath.read_text()

        # Pattern: // name: query_name\n// description: ...\nQUERY
        pattern = r'//\s*name:\s*(\w+)\s*\n//\s*description:\s*([^\n]+)\s*\n(.*?)(?=//\s*name:|$)'
        matches = re.finditer(pattern, content, re.DOTALL)

        for match in matches:
            name = match.group(1)
            description = match.group(2).strip()
            query = match.group(3).strip()

            self._queries[name] = query

    def get(self, name: str) -> str:
        """Get a named query.

        Args:
            name: Query name

        Returns:
            Cypher query string

        Raises:
            KeyError: If query not found
        """
        if name not in self._queries:
            raise KeyError(
                f"Query '{name}' not found in {self._filepath}. "
                f"Available: {list(self._queries.keys())}"
            )
        return self._queries[name]

    def list_queries(self) -> List[str]:
        """List all available query names."""
        return list(self._queries.keys())
```

---

## API Design with Dataclasses

### Request Objects

```python
@dataclass
class CreateRequirementRequest:
    """Request to create a new Requirement node."""
    rid: str
    name: str
    text: str
    type: str = "functional"  # functional, compliance, constraint, quality
    priority: str = "medium"  # high, medium, low
    status: str = "active"  # active, draft, deprecated, superseded
    gobd_relevant: bool = False
    gdpr_relevant: bool = False
    compliance_status: str = "open"  # open, partial, fulfilled
    source: Optional[str] = None
    value_stream: Optional[str] = None
    citation_ids: List[str] = field(default_factory=list)
    created_by: str = "system"

    def validate(self) -> None:
        """Validate request fields."""
        if not self.rid or not re.match(r"R-\d{4,}", self.rid):
            raise ValueError(f"Invalid RID format: {self.rid} (expected R-XXXX)")
        if not self.name or len(self.name) > 200:
            raise ValueError("Name must be 1-200 characters")
        if not self.text:
            raise ValueError("Text is required")
        if self.type not in ("functional", "compliance", "constraint", "quality"):
            raise ValueError(f"Invalid type: {self.type}")
        if self.priority not in ("high", "medium", "low"):
            raise ValueError(f"Invalid priority: {self.priority}")


@dataclass
class RequirementFilters:
    """Filters for listing requirements."""
    type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    compliance_status: Optional[str] = None
    gobd_relevant: Optional[bool] = None
    gdpr_relevant: Optional[bool] = None
    created_by: Optional[str] = None

    def to_params(self) -> Dict[str, Any]:
        """Convert to query parameters."""
        params = {}
        if self.type:
            params["type"] = self.type
        if self.priority:
            params["priority"] = self.priority
        if self.status:
            params["status"] = self.status
        if self.compliance_status:
            params["compliance_status"] = self.compliance_status
        if self.gobd_relevant is not None:
            params["gobd_relevant"] = self.gobd_relevant
        if self.gdpr_relevant is not None:
            params["gdpr_relevant"] = self.gdpr_relevant
        if self.created_by:
            params["created_by"] = self.created_by
        return params


@dataclass
class CreateFulfillmentRequest:
    """Request to create a fulfillment relationship."""
    requirement_rid: str
    entity_id: str  # boid or mid
    entity_type: str  # "BusinessObject" or "Message"
    relationship_type: str  # FULFILLED_BY_OBJECT, NOT_FULFILLED_BY_OBJECT, etc.

    # For FULFILLED_BY_* relationships
    confidence: float = 0.5
    evidence: str = ""
    citation_id: Optional[str] = None

    # For NOT_FULFILLED_BY_* relationships
    gap_description: str = ""
    severity: str = "medium"  # critical, major, medium, minor
    remediation: str = ""

    def validate(self) -> None:
        """Validate request fields."""
        if self.entity_type not in ("BusinessObject", "Message"):
            raise ValueError(f"Invalid entity_type: {self.entity_type}")

        valid_types = {
            "BusinessObject": ["FULFILLED_BY_OBJECT", "NOT_FULFILLED_BY_OBJECT"],
            "Message": ["FULFILLED_BY_MESSAGE", "NOT_FULFILLED_BY_MESSAGE"],
        }

        if self.relationship_type not in valid_types[self.entity_type]:
            raise ValueError(
                f"Invalid relationship_type for {self.entity_type}: {self.relationship_type}"
            )

        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(f"Confidence must be 0.0-1.0, got {self.confidence}")


@dataclass
class CreateRequirementRelationshipRequest:
    """Request to create a Requirement→Requirement relationship."""
    source_rid: str
    target_rid: str
    relationship_type: str  # REFINES, DEPENDS_ON, TRACES_TO, SUPERSEDES
    rationale: str = ""
    introduced_in: Optional[str] = None

    # SUPERSEDES-specific fields
    reason: Optional[str] = None
    effective_date: Optional[datetime] = None
    citation_id: Optional[str] = None

    def validate(self) -> None:
        """Validate request fields."""
        valid_types = ["REFINES", "DEPENDS_ON", "TRACES_TO", "SUPERSEDES"]
        if self.relationship_type not in valid_types:
            raise ValueError(f"Invalid relationship_type: {self.relationship_type}")
```

### Response Objects

```python
@dataclass
class Requirement:
    """Requirement node representation."""
    rid: str
    name: str
    text: str
    type: str
    priority: str
    status: str
    gobd_relevant: bool
    gdpr_relevant: bool
    compliance_status: str
    source: Optional[str] = None
    value_stream: Optional[str] = None
    citation_ids: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    validated_by: Optional[str] = None
    validated_at: Optional[datetime] = None

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "Requirement":
        """Create from Neo4j query result."""
        return cls(
            rid=record["rid"],
            name=record["name"],
            text=record["text"],
            type=record.get("type", "functional"),
            priority=record.get("priority", "medium"),
            status=record.get("status", "active"),
            gobd_relevant=record.get("goBDRelevant", False),
            gdpr_relevant=record.get("gdprRelevant", False),
            compliance_status=record.get("complianceStatus", "open"),
            source=record.get("source"),
            value_stream=record.get("valueStream"),
            citation_ids=record.get("citationIds", []),
            created_at=record.get("createdAt"),
            updated_at=record.get("updatedAt"),
            created_by=record.get("createdBy"),
            validated_by=record.get("validatedBy"),
            validated_at=record.get("validatedAt"),
        )


@dataclass
class BusinessObject:
    """BusinessObject node representation."""
    boid: str
    name: str
    description: Optional[str] = None
    domain: Optional[str] = None
    owner: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "BusinessObject":
        """Create from Neo4j query result."""
        return cls(
            boid=record["boid"],
            name=record["name"],
            description=record.get("description"),
            domain=record.get("domain"),
            owner=record.get("owner"),
            created_at=record.get("createdAt"),
            updated_at=record.get("updatedAt"),
        )


@dataclass
class Message:
    """Message node representation."""
    mid: str
    name: str
    description: Optional[str] = None
    direction: Optional[str] = None  # inbound, outbound, bidirectional
    format: Optional[str] = None  # JSON, XML, CSV, etc.
    protocol: Optional[str] = None  # HTTP, AMQP, gRPC, etc.
    version: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "Message":
        """Create from Neo4j query result."""
        return cls(
            mid=record["mid"],
            name=record["name"],
            description=record.get("description"),
            direction=record.get("direction"),
            format=record.get("format"),
            protocol=record.get("protocol"),
            version=record.get("version"),
            created_at=record.get("createdAt"),
            updated_at=record.get("updatedAt"),
        )


@dataclass
class SimilarRequirement:
    """Requirement with similarity score."""
    requirement: Requirement
    similarity_score: float


@dataclass
class BusinessObjectMatch:
    """BusinessObject with match score."""
    business_object: BusinessObject
    match_score: float


@dataclass
class MessageMatch:
    """Message with match score."""
    message: Message
    match_score: float


@dataclass
class FulfillmentRelationship:
    """Fulfillment relationship details."""
    requirement_rid: str
    entity_id: str
    entity_type: str
    relationship_type: str
    confidence: Optional[float] = None
    evidence: Optional[str] = None
    gap_description: Optional[str] = None
    severity: Optional[str] = None
    remediation: Optional[str] = None
    citation_id: Optional[str] = None
    validated_at: Optional[datetime] = None
    validated_by_agent: Optional[str] = None

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "FulfillmentRelationship":
        """Create from Neo4j query result."""
        return cls(
            requirement_rid=record["requirement_rid"],
            entity_id=record["entity_id"],
            entity_type=record["entity_type"],
            relationship_type=record["relationship_type"],
            confidence=record.get("confidence"),
            evidence=record.get("evidence"),
            gap_description=record.get("gapDescription"),
            severity=record.get("severity"),
            remediation=record.get("remediation"),
            citation_id=record.get("citationId"),
            validated_at=record.get("validatedAt"),
            validated_by_agent=record.get("validatedByAgent"),
        )


@dataclass
class RelationshipInfo:
    """Generic relationship information."""
    source_id: str
    source_label: str
    relationship_type: str
    target_id: str
    target_label: str
    properties: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "RelationshipInfo":
        """Create from Neo4j query result."""
        return cls(
            source_id=record["source_id"],
            source_label=record["source_label"],
            relationship_type=record["rel_type"],
            target_id=record["target_id"],
            target_label=record["target_label"],
            properties=record.get("properties", {}),
        )


@dataclass
class ComplianceSummary:
    """Compliance status summary."""
    open: int = 0
    partial: int = 0
    fulfilled: int = 0

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "ComplianceSummary":
        """Create from Neo4j query result."""
        return cls(
            open=record.get("open", 0),
            partial=record.get("partial", 0),
            fulfilled=record.get("fulfilled", 0),
        )


@dataclass
class GoBDStatistics:
    """GoBD-specific statistics."""
    total_gobd_requirements: int = 0
    with_impacts: int = 0
    without_impacts: int = 0
    fulfilled: int = 0
    partial: int = 0
    open: int = 0

    @classmethod
    def from_neo4j_record(cls, record: Dict[str, Any]) -> "GoBDStatistics":
        """Create from Neo4j query result."""
        return cls(
            total_gobd_requirements=record.get("total", 0),
            with_impacts=record.get("with_impacts", 0),
            without_impacts=record.get("without_impacts", 0),
            fulfilled=record.get("fulfilled", 0),
            partial=record.get("partial", 0),
            open=record.get("open", 0),
        )
```

---

## Query Organization

### File Structure

```
src/database/queries/neo4j/
├── requirements.cypher       # Requirement CRUD and queries
├── entities.cypher           # BusinessObject and Message queries
├── relationships.cypher      # Relationship creation and queries
├── statistics.cypher         # Statistics and metrics
└── validation.cypher         # Metamodel validation queries (from validator)
```

### Example: `requirements.cypher`

```cypher
// name: create_requirement
// description: Create a new Requirement node with all properties
CREATE (r:Requirement {
    rid: $rid,
    name: $name,
    text: $text,
    type: $type,
    priority: $priority,
    status: $status,
    goBDRelevant: $gobd_relevant,
    gdprRelevant: $gdpr_relevant,
    complianceStatus: $compliance_status,
    source: $source,
    valueStream: $value_stream,
    citationIds: $citation_ids,
    createdAt: datetime(),
    updatedAt: datetime(),
    createdBy: $created_by
})
RETURN r.rid AS rid, r.name AS name, r.text AS text,
       r.type AS type, r.priority AS priority, r.status AS status,
       r.goBDRelevant AS goBDRelevant, r.gdprRelevant AS gdprRelevant,
       r.complianceStatus AS complianceStatus,
       r.createdAt AS createdAt, r.createdBy AS createdBy

// name: get_requirement_by_rid
// description: Get a single requirement by RID
MATCH (r:Requirement {rid: $rid})
RETURN r.rid AS rid, r.name AS name, r.text AS text,
       r.type AS type, r.priority AS priority, r.status AS status,
       r.goBDRelevant AS goBDRelevant, r.gdprRelevant AS gdprRelevant,
       r.complianceStatus AS complianceStatus,
       r.source AS source, r.valueStream AS valueStream,
       r.citationIds AS citationIds,
       r.createdAt AS createdAt, r.updatedAt AS updatedAt,
       r.createdBy AS createdBy, r.validatedBy AS validatedBy,
       r.validatedAt AS validatedAt

// name: list_requirements
// description: List requirements with optional filters and pagination
MATCH (r:Requirement)
WHERE ($type IS NULL OR r.type = $type)
  AND ($priority IS NULL OR r.priority = $priority)
  AND ($status IS NULL OR r.status = $status)
  AND ($compliance_status IS NULL OR r.complianceStatus = $compliance_status)
  AND ($gobd_relevant IS NULL OR r.goBDRelevant = $gobd_relevant)
  AND ($gdpr_relevant IS NULL OR r.gdprRelevant = $gdpr_relevant)
  AND ($created_by IS NULL OR r.createdBy = $created_by)
RETURN r.rid AS rid, r.name AS name, r.text AS text,
       r.type AS type, r.priority AS priority, r.status AS status,
       r.goBDRelevant AS goBDRelevant, r.gdprRelevant AS gdprRelevant,
       r.complianceStatus AS complianceStatus,
       r.createdAt AS createdAt, r.createdBy AS createdBy
ORDER BY r.createdAt DESC
SKIP $offset
LIMIT $limit

// name: update_compliance_status
// description: Recalculate and update complianceStatus based on fulfillment relationships
MATCH (r:Requirement {rid: $rid})
OPTIONAL MATCH (r)-[:FULFILLED_BY_OBJECT|FULFILLED_BY_MESSAGE]->(fulfilled)
OPTIONAL MATCH (r)-[:NOT_FULFILLED_BY_OBJECT|NOT_FULFILLED_BY_MESSAGE]->(notFulfilled)
WITH r, COUNT(DISTINCT fulfilled) AS fulfilledCount, COUNT(DISTINCT notFulfilled) AS gapCount
SET r.complianceStatus = CASE
  WHEN gapCount = 0 AND fulfilledCount > 0 THEN 'fulfilled'
  WHEN gapCount > 0 AND fulfilledCount > 0 THEN 'partial'
  ELSE 'open'
END,
r.updatedAt = datetime()
RETURN r.rid AS rid, r.complianceStatus AS complianceStatus

// name: get_max_rid
// description: Get the highest RID for generating next sequential ID
MATCH (r:Requirement)
WHERE r.rid STARTS WITH 'R-'
RETURN r.rid AS rid
ORDER BY r.rid DESC
LIMIT 1

// name: delete_requirement
// description: Delete a requirement and all its relationships
MATCH (r:Requirement {rid: $rid})
DETACH DELETE r
RETURN count(r) AS deleted_count
```

### Example: `relationships.cypher`

```cypher
// name: create_fulfilled_by_object
// description: Create FULFILLED_BY_OBJECT relationship
MATCH (r:Requirement {rid: $rid})
MATCH (b:BusinessObject {boid: $entity_id})
CREATE (r)-[rel:FULFILLED_BY_OBJECT {
    confidence: $confidence,
    evidence: $evidence,
    citationId: $citation_id,
    validatedAt: datetime(),
    validatedByAgent: 'validator_agent'
}]->(b)
RETURN r.rid AS requirement_rid, b.boid AS entity_id,
       'BusinessObject' AS entity_type, type(rel) AS relationship_type,
       rel.confidence AS confidence, rel.evidence AS evidence,
       rel.validatedAt AS validatedAt

// name: create_not_fulfilled_by_object
// description: Create NOT_FULFILLED_BY_OBJECT relationship (gap)
MATCH (r:Requirement {rid: $rid})
MATCH (b:BusinessObject {boid: $entity_id})
CREATE (r)-[rel:NOT_FULFILLED_BY_OBJECT {
    gapDescription: $gap_description,
    severity: $severity,
    remediation: $remediation,
    citationId: $citation_id,
    validatedAt: datetime(),
    validatedByAgent: 'validator_agent'
}]->(b)
RETURN r.rid AS requirement_rid, b.boid AS entity_id,
       'BusinessObject' AS entity_type, type(rel) AS relationship_type,
       rel.gapDescription AS gapDescription, rel.severity AS severity,
       rel.validatedAt AS validatedAt

// name: create_refines
// description: Create REFINES relationship (child refines parent)
MATCH (child:Requirement {rid: $source_rid})
MATCH (parent:Requirement {rid: $target_rid})
CREATE (child)-[rel:REFINES {
    rationale: $rationale,
    introducedIn: $introduced_in
}]->(parent)
RETURN child.rid AS source_rid, parent.rid AS target_rid,
       type(rel) AS relationship_type, rel.rationale AS rationale

// name: get_requirement_relationships_both
// description: Get all relationships for a requirement (both directions)
MATCH (r:Requirement {rid: $rid})
OPTIONAL MATCH (r)-[out_rel]->(target)
OPTIONAL MATCH (source)-[in_rel]->(r)
RETURN
    COALESCE(r.rid, source.rid) AS source_id,
    COALESCE(labels(r)[0], labels(source)[0]) AS source_label,
    COALESCE(type(out_rel), type(in_rel)) AS rel_type,
    COALESCE(target.rid, target.boid, target.mid, r.rid, r.boid, r.mid) AS target_id,
    COALESCE(labels(target)[0], labels(r)[0]) AS target_label,
    COALESCE(properties(out_rel), properties(in_rel)) AS properties

// name: get_entity_relationships
// description: Get all requirement relationships for a BusinessObject or Message
MATCH (r:Requirement)-[rel]->(e)
WHERE (e.boid = $entity_id OR e.mid = $entity_id)
  AND labels(e)[0] = $entity_type
RETURN r.rid AS source_id, 'Requirement' AS source_label,
       type(rel) AS rel_type,
       COALESCE(e.boid, e.mid) AS target_id,
       labels(e)[0] AS target_label,
       properties(rel) AS properties
```

### Example: `statistics.cypher`

```cypher
// name: count_nodes_by_label
// description: Count nodes by label
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS count
ORDER BY count DESC

// name: count_relationships_by_type
// description: Count relationships by type
MATCH ()-[r]->()
RETURN type(r) AS type, count(r) AS count
ORDER BY count DESC

// name: compliance_summary
// description: Get compliance status summary
MATCH (r:Requirement)
RETURN
    count(CASE WHEN r.complianceStatus = 'open' THEN 1 END) AS open,
    count(CASE WHEN r.complianceStatus = 'partial' THEN 1 END) AS partial,
    count(CASE WHEN r.complianceStatus = 'fulfilled' THEN 1 END) AS fulfilled

// name: gobd_statistics
// description: Get GoBD-specific statistics
MATCH (r:Requirement)
WHERE r.goBDRelevant = true
WITH count(r) AS total
MATCH (r:Requirement {goBDRelevant: true})
OPTIONAL MATCH (r)-[:IMPACTS_OBJECT|IMPACTS_MESSAGE]->()
WITH total, count(DISTINCT r) AS with_impacts, total - count(DISTINCT r) AS without_impacts
MATCH (r:Requirement {goBDRelevant: true})
RETURN
    total,
    with_impacts,
    without_impacts,
    count(CASE WHEN r.complianceStatus = 'fulfilled' THEN 1 END) AS fulfilled,
    count(CASE WHEN r.complianceStatus = 'partial' THEN 1 END) AS partial,
    count(CASE WHEN r.complianceStatus = 'open' THEN 1 END) AS open
```

---

## Migration Plan

### Phase 1: Create New Module (Week 1)

**Goal**: Build new `Neo4jDB` class alongside existing code.

#### Tasks:

1. **Create directory structure**
   ```bash
   mkdir -p src/database/queries/neo4j
   ```

2. **Move metamodel validator**
   ```bash
   mv src/utils/metamodel_validator.py src/database/metamodel_validator.py
   # Update imports in src/utils/__init__.py and src/database/__init__.py
   ```

3. **Create core classes**
   - `src/database/neo4j_db.py` - Neo4jDB class
   - Namespace classes: RequirementOperations, EntityOperations, RelationshipOperations, StatisticsOperations
   - QueryLoader class

4. **Create dataclasses**
   - Request objects (CreateRequirementRequest, etc.)
   - Response objects (Requirement, BusinessObject, etc.)
   - All in `src/database/neo4j_types.py`

5. **Extract queries to .cypher files**
   - `queries/neo4j/requirements.cypher` - 10 queries
   - `queries/neo4j/entities.cypher` - 8 queries
   - `queries/neo4j/relationships.cypher` - 12 queries
   - `queries/neo4j/statistics.cypher` - 6 queries
   - `queries/neo4j/validation.cypher` - 11 queries (from metamodel_validator)

6. **Update module exports (dependency injection pattern)**
   ```python
   # src/database/__init__.py
   from .postgres_db import PostgresDB
   from .neo4j_db import Neo4jDB
   from .mongo_db import MongoDB
   from .metamodel_validator import MetamodelValidator

   # OLD API (backward compatibility - will be removed in Phase 5)
   from .postgres_utils import PostgresConnection
   from .neo4j_utils import Neo4jConnection

   __all__ = [
       "PostgresDB",  # NEW
       "Neo4jDB",  # NEW
       "MongoDB",  # NEW
       "MetamodelValidator",  # NEW
       "PostgresConnection",  # OLD (deprecated)
       "Neo4jConnection",  # OLD (deprecated)
   ]
   ```

7. **Write unit tests**
   - `tests/database/test_neo4j_db.py` - Connection, query execution
   - `tests/database/test_neo4j_operations.py` - Each namespace class
   - `tests/database/test_neo4j_types.py` - Dataclass validation
   - `tests/database/test_query_loader.py` - Query parsing

**Verification**: All new tests pass, old code still works.

---

### Phase 2: Migrate Graph Tools (Week 2)

**Goal**: Update `src/tools/graph_tools.py` to use new `Neo4jDB` class.

#### Tasks:

1. **Update create_graph_tools signature**
   ```python
   def create_graph_tools(context: ToolContext) -> List:
       # OLD: neo4j = context.neo4j_conn (Neo4jConnection)
       # NEW: neo4j_db = context.neo4j_db (Neo4jDB)
       pass
   ```

2. **Update each tool to use new API**

   **Example: execute_cypher_query**
   ```python
   # OLD
   @tool
   def execute_cypher_query(query: str) -> str:
       if not neo4j:
           return "Error: No Neo4j connection available"
       results = neo4j.execute_query(query)
       # ...

   # NEW
   @tool
   def execute_cypher_query(query: str) -> str:
       if not neo4j_db or not neo4j_db.is_connected:
           return "Error: No Neo4j connection available"
       results = neo4j_db.execute_query(query)
       # ...
   ```

   **Example: create_requirement_node**
   ```python
   # OLD
   @tool
   def create_requirement_node(
       rid: str, name: str, text: str, req_type: str = "functional",
       priority: str = "medium", gobd_relevant: bool = False,
       gdpr_relevant: bool = False, compliance_status: str = "open"
   ) -> str:
       query = """CREATE (r:Requirement {...})..."""
       results = neo4j.execute_query(query, {...})
       # ...

   # NEW
   @tool
   def create_requirement_node(
       rid: str, name: str, text: str, req_type: str = "functional",
       priority: str = "medium", gobd_relevant: bool = False,
       gdpr_relevant: bool = False, compliance_status: str = "open"
   ) -> str:
       req = CreateRequirementRequest(
           rid=rid, name=name, text=text, type=req_type,
           priority=priority, gobd_relevant=gobd_relevant,
           gdpr_relevant=gdpr_relevant, compliance_status=compliance_status,
       )
       try:
           req.validate()
           result = neo4j_db.requirements.create(req)
           return f"Successfully created Requirement node: {result.rid}"
       except ValueError as e:
           return f"Validation error: {e}"
       except Exception as e:
           return f"Error creating requirement: {e}"
   ```

   **Example: find_similar_requirements**
   ```python
   # OLD
   @tool
   def find_similar_requirements(text: str, threshold: float = 0.7) -> str:
       existing = load_requirements()  # Manual cache
       # Manual similarity logic...

   # NEW
   @tool
   def find_similar_requirements(text: str, threshold: float = 0.7) -> str:
       similar = neo4j_db.requirements.find_similar(text, threshold)

       result = f"Found {len(similar)} similar requirements (threshold: {threshold}):\n\n"
       for i, s in enumerate(similar, 1):
           req = s.requirement
           result += f"{i}. [{req.rid}] {req.name}\n"
           result += f"   Similarity: {s.similarity_score:.1%}\n"
           result += f"   Text: {req.text[:100]}...\n\n"

       return result if similar else "No similar requirements found."
   ```

3. **Remove manual caching**
   - Delete `_requirements_cache`, `_business_objects_cache`, `_messages_cache`
   - Let `Neo4jDB` handle caching internally (future enhancement)

4. **Update ToolContext**
   ```python
   # src/tools/context.py
   @dataclass
   class ToolContext:
       workspace_manager: Optional[WorkspaceManager] = None
       todo_manager: Optional[Any] = None
       postgres_conn: Optional[Any] = None  # DEPRECATED
       postgres_db: Optional["PostgresDB"] = None  # NEW
       neo4j_conn: Optional[Any] = None  # DEPRECATED
       neo4j_db: Optional["Neo4jDB"] = None  # NEW
       # ...

       def has_neo4j(self) -> bool:
           """Check if Neo4j connection is available."""
           return self.neo4j_db is not None and self.neo4j_db.is_connected
   ```

5. **Write integration tests**
   - `tests/integration/test_graph_tools_neo4j_db.py`
   - Test each tool with real Neo4j database
   - Verify dataclass validation

**Verification**: All graph tools pass integration tests with new Neo4jDB.

---

### Phase 3: Migrate Agent Initialization (Week 2)

**Goal**: Update `src/agent.py` to use `neo4j_db` instead of `neo4j_conn`.

#### Tasks:

1. **Update agent initialization**
   ```python
   # src/agent.py

   # OLD
   if self.neo4j_conn is None and self.config.connections.neo4j:
       from src.database.neo4j_utils import Neo4jConnection
       neo4j_uri = os.getenv("NEO4J_URI")
       neo4j_user = os.getenv("NEO4J_USER", "neo4j")
       neo4j_password = os.getenv("NEO4J_PASSWORD")

       if neo4j_uri and neo4j_password:
           self.neo4j_conn = Neo4jConnection(
               uri=neo4j_uri,
               user=neo4j_user,
               password=neo4j_password,
           )
           if not self.neo4j_conn.connect():
               logger.warning("Failed to connect to Neo4j")
               self.neo4j_conn = None

   # NEW
   if self.neo4j_db is None and self.config.connections.neo4j:
       from src.database import neo4j_db as _neo4j_db

       try:
           _neo4j_db.connect()
           self.neo4j_db = _neo4j_db
           logger.info("Neo4j connection established")
       except Exception as e:
           logger.warning(f"Failed to connect to Neo4j: {e}")
           self.neo4j_db = None
   ```

2. **Update context creation**
   ```python
   # src/agent.py

   # OLD
   context = ToolContext(
       workspace_manager=self.workspace,
       todo_manager=self.todo_manager,
       postgres_conn=self.postgres_conn,
       neo4j_conn=self.neo4j_conn,
       # ...
   )

   # NEW
   context = ToolContext(
       workspace_manager=self.workspace,
       todo_manager=self.todo_manager,
       postgres_db=self.postgres_db,
       neo4j_db=self.neo4j_db,
       # ...
   )
   ```

3. **Update cleanup**
   ```python
   # src/agent.py

   # OLD
   if self.neo4j_conn:
       self.neo4j_conn.close()

   # NEW
   if self.neo4j_db:
       self.neo4j_db.disconnect()
   ```

**Verification**: Agent runs successfully with validator config.

---

### Phase 4: Update Metamodel Validator Usage (Week 3)

**Goal**: Update validation tool to use moved `MetamodelValidator`.

#### Tasks:

1. **Update import in graph_tools.py**
   ```python
   # OLD
   from src.utils.metamodel_validator import MetamodelValidator, Severity

   # NEW
   from src.database import MetamodelValidator, Severity
   ```

2. **Update validator initialization**
   ```python
   # OLD
   validator = MetamodelValidator(neo4j)  # Neo4jConnection

   # NEW
   # Option 1: Pass Neo4jDB (requires MetamodelValidator update)
   validator = MetamodelValidator(neo4j_db)

   # Option 2: Create adapter (temporary)
   class Neo4jConnectionAdapter:
       def __init__(self, neo4j_db: Neo4jDB):
           self._db = neo4j_db

       def execute_query(self, query: str, parameters: Optional[Dict] = None):
           return self._db.execute_query(query, parameters)

       def get_database_schema(self):
           return self._db.get_schema()

   adapter = Neo4jConnectionAdapter(neo4j_db)
   validator = MetamodelValidator(adapter)
   ```

3. **Update validate_metamodel.py script**
   ```python
   # validate_metamodel.py

   # OLD
   from src.database.neo4j_utils import create_neo4j_connection
   conn = create_neo4j_connection()
   conn.connect()

   # NEW
   from src.database import neo4j_db
   neo4j_db.connect()
   validator = MetamodelValidator(neo4j_db)
   ```

**Verification**: Metamodel validation runs successfully.

---

### Phase 5: Remove Old Code (Week 3)

**Goal**: Delete deprecated `neo4j_utils.py` and clean up.

#### Tasks:

1. **Verify no remaining usages**
   ```bash
   grep -r "neo4j_utils" src/
   grep -r "Neo4jConnection" src/
   grep -r "\.neo4j_conn" src/
   ```

2. **Remove old file**
   ```bash
   git rm src/database/neo4j_utils.py
   ```

3. **Remove deprecated ToolContext fields**
   ```python
   # src/tools/context.py
   @dataclass
   class ToolContext:
       workspace_manager: Optional[WorkspaceManager] = None
       todo_manager: Optional[Any] = None
       postgres_db: Optional["PostgresDB"] = None  # Keep
       neo4j_db: Optional["Neo4jDB"] = None  # Keep
       # Remove: postgres_conn, neo4j_conn
   ```

4. **Update all documentation**
   - CLAUDE.md - Update architecture section
   - docs/db_refactor.md - Mark Neo4j migration complete
   - README.md - Update API usage examples

5. **Final verification**
   ```bash
   pytest tests/ -v
   python validate_metamodel.py
   python agent.py --config validator --help
   ```

**Verification**: All tests pass, no references to old code.

---

## Testing Strategy

### Unit Tests

#### `tests/database/test_neo4j_db.py`

```python
import pytest
from src.database import Neo4jDB
from neo4j.exceptions import ServiceUnavailable, AuthError

class TestNeo4jDB:
    """Test Neo4jDB class."""

    def test_init_from_env(self, monkeypatch):
        """Test initialization from environment variables."""
        monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
        monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
        monkeypatch.setenv("NEO4J_PASSWORD", "test_password")

        db = Neo4jDB()
        assert db.uri == "bolt://localhost:7687"
        assert db.username == "neo4j"
        assert db.password == "test_password"

    def test_init_missing_credentials(self):
        """Test initialization fails without credentials."""
        with pytest.raises(ValueError, match="Neo4j URI and password required"):
            Neo4jDB(uri="bolt://localhost:7687", password=None)

    def test_connect_success(self, neo4j_db):
        """Test successful connection."""
        neo4j_db.connect()
        assert neo4j_db.is_connected
        assert neo4j_db.verify_connectivity()
        neo4j_db.disconnect()

    def test_connect_invalid_uri(self):
        """Test connection failure with invalid URI."""
        db = Neo4jDB(
            uri="bolt://invalid:9999",
            username="neo4j",
            password="password"
        )
        with pytest.raises(ServiceUnavailable):
            db.connect()

    def test_execute_query_not_connected(self):
        """Test query execution fails if not connected."""
        db = Neo4jDB(uri="bolt://localhost:7687", username="neo4j", password="test")
        with pytest.raises(RuntimeError, match="Not connected"):
            db.execute_query("RETURN 1")

    def test_execute_query_success(self, connected_neo4j_db):
        """Test successful query execution."""
        result = connected_neo4j_db.execute_query("RETURN 1 AS number")
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_transaction_commit(self, connected_neo4j_db):
        """Test transaction commits on success."""
        with connected_neo4j_db.transaction() as tx:
            tx.run("CREATE (n:TestNode {name: $name})", {"name": "test"})

        result = connected_neo4j_db.execute_query(
            "MATCH (n:TestNode {name: $name}) RETURN n",
            {"name": "test"}
        )
        assert len(result) == 1

    def test_transaction_rollback(self, connected_neo4j_db):
        """Test transaction rolls back on exception."""
        with pytest.raises(Exception):
            with connected_neo4j_db.transaction() as tx:
                tx.run("CREATE (n:TestNode {name: $name})", {"name": "rollback"})
                raise Exception("Test rollback")

        result = connected_neo4j_db.execute_query(
            "MATCH (n:TestNode {name: $name}) RETURN n",
            {"name": "rollback"}
        )
        assert len(result) == 0
```

#### `tests/database/test_neo4j_operations.py`

```python
import pytest
from src.database import Neo4jDB
from src.database.neo4j_types import (
    CreateRequirementRequest,
    Requirement,
    RequirementFilters,
)

class TestRequirementOperations:
    """Test RequirementOperations namespace."""

    def test_create_requirement(self, connected_neo4j_db):
        """Test requirement creation."""
        req = CreateRequirementRequest(
            rid="R-TEST-001",
            name="Test Requirement",
            text="This is a test requirement",
            type="functional",
            priority="high",
        )

        result = connected_neo4j_db.requirements.create(req)

        assert isinstance(result, Requirement)
        assert result.rid == "R-TEST-001"
        assert result.name == "Test Requirement"
        assert result.type == "functional"

    def test_get_requirement_by_rid(self, connected_neo4j_db, test_requirement):
        """Test getting requirement by RID."""
        req = connected_neo4j_db.requirements.get_by_rid(test_requirement.rid)

        assert req is not None
        assert req.rid == test_requirement.rid
        assert req.name == test_requirement.name

    def test_find_similar_requirements(self, connected_neo4j_db, test_requirements):
        """Test similarity search."""
        similar = connected_neo4j_db.requirements.find_similar(
            "invoice processing requirement",
            threshold=0.6
        )

        assert len(similar) > 0
        assert all(s.similarity_score >= 0.6 for s in similar)

    def test_check_duplicate(self, connected_neo4j_db, test_requirement):
        """Test duplicate detection."""
        duplicate = connected_neo4j_db.requirements.check_duplicate(
            test_requirement.text,
            threshold=0.95
        )

        assert duplicate is not None
        assert duplicate.rid == test_requirement.rid

    def test_list_requirements_with_filters(self, connected_neo4j_db, test_requirements):
        """Test listing with filters."""
        filters = RequirementFilters(
            type="functional",
            priority="high",
            gobd_relevant=True,
        )

        results = connected_neo4j_db.requirements.list(filters=filters, limit=10)

        assert len(results) > 0
        assert all(r.type == "functional" for r in results)
        assert all(r.priority == "high" for r in results)

    def test_generate_next_rid(self, connected_neo4j_db):
        """Test RID generation."""
        rid1 = connected_neo4j_db.requirements.generate_next_rid()
        assert rid1 == "R-0001"

        # Create requirement
        req = CreateRequirementRequest(rid=rid1, name="Test", text="Test")
        connected_neo4j_db.requirements.create(req)

        # Next RID should increment
        rid2 = connected_neo4j_db.requirements.generate_next_rid()
        assert rid2 == "R-0002"
```

### Integration Tests

#### `tests/integration/test_graph_tools_neo4j_db.py`

```python
import pytest
from src.tools.graph_tools import create_graph_tools
from src.tools.context import ToolContext
from src.database import neo4j_db

@pytest.fixture
def graph_tools(connected_neo4j_db):
    """Create graph tools with Neo4jDB."""
    context = ToolContext(neo4j_db=connected_neo4j_db)
    return create_graph_tools(context)

class TestGraphToolsIntegration:
    """Integration tests for graph tools with Neo4jDB."""

    def test_execute_cypher_query_tool(self, graph_tools):
        """Test execute_cypher_query tool."""
        tool = next(t for t in graph_tools if t.name == "execute_cypher_query")
        result = tool.invoke({"query": "RETURN 1 AS number"})

        assert "Record 1" in result
        assert "number" in result

    def test_create_requirement_node_tool(self, graph_tools):
        """Test create_requirement_node tool."""
        tool = next(t for t in graph_tools if t.name == "create_requirement_node")
        result = tool.invoke({
            "rid": "R-TOOL-001",
            "name": "Tool Test",
            "text": "Test requirement created by tool",
            "req_type": "functional",
            "priority": "medium",
        })

        assert "Successfully created" in result
        assert "R-TOOL-001" in result

    def test_find_similar_requirements_tool(self, graph_tools, test_requirements):
        """Test find_similar_requirements tool."""
        tool = next(t for t in graph_tools if t.name == "find_similar_requirements")
        result = tool.invoke({
            "text": "invoice processing",
            "threshold": 0.6,
        })

        assert "Found" in result
        assert "similar requirements" in result
```

### Coverage Goals

- **Unit Tests**: 90%+ coverage for new code
- **Integration Tests**: All tools tested against real Neo4j
- **End-to-End Tests**: Full validator workflow with new database module

---

## Implementation Checklist

### Phase 1: Create New Module

- [ ] Create directory structure (`queries/neo4j/`)
- [ ] Move `metamodel_validator.py` to `src/database/`
- [ ] Implement `Neo4jDB` class with connection management
- [ ] Implement `RequirementOperations` namespace
- [ ] Implement `EntityOperations` namespace
- [ ] Implement `RelationshipOperations` namespace
- [ ] Implement `StatisticsOperations` namespace
- [ ] Implement `QueryLoader` class
- [ ] Create all dataclasses (requests and responses)
- [ ] Extract queries to `.cypher` files
  - [ ] `requirements.cypher` (10 queries)
  - [ ] `entities.cypher` (8 queries)
  - [ ] `relationships.cypher` (12 queries)
  - [ ] `statistics.cypher` (6 queries)
  - [ ] `validation.cypher` (11 queries from validator)
- [ ] Create module exports in `src/database/__init__.py`
- [ ] Write unit tests
  - [ ] `test_neo4j_db.py` - Connection, queries, transactions
  - [ ] `test_neo4j_operations.py` - Each namespace
  - [ ] `test_neo4j_types.py` - Dataclass validation
  - [ ] `test_query_loader.py` - Query parsing
- [ ] Verify all new tests pass

### Phase 2: Migrate Graph Tools

- [ ] Update `create_graph_tools` signature to use `Neo4jDB`
- [ ] Update `execute_cypher_query` tool
- [ ] Update `get_database_schema` tool
- [ ] Update `find_similar_requirements` tool
- [ ] Update `check_for_duplicates` tool
- [ ] Update `resolve_business_object` tool
- [ ] Update `resolve_message` tool
- [ ] Update `validate_schema_compliance` tool
- [ ] Update `create_requirement_node` tool
- [ ] Update `create_fulfillment_relationship` tool
- [ ] Update `generate_requirement_id` tool
- [ ] Update `get_entity_relationships` tool
- [ ] Update `count_graph_statistics` tool
- [ ] Remove manual caching code
- [ ] Update `ToolContext` to include `neo4j_db` field
- [ ] Update `ToolContext.has_neo4j()` method
- [ ] Write integration tests
  - [ ] `test_graph_tools_neo4j_db.py` - All tools
- [ ] Verify all graph tools pass integration tests

### Phase 3: Migrate Agent Initialization

- [ ] Update agent initialization to use `neo4j_db`
- [ ] Update context creation to pass `neo4j_db`
- [ ] Update cleanup to call `neo4j_db.disconnect()`
- [ ] Test validator agent with new connection
- [ ] Verify no regressions in agent behavior

### Phase 4: Update Metamodel Validator

- [ ] Update imports to use `src.database.MetamodelValidator`
- [ ] Update `MetamodelValidator` to work with `Neo4jDB` or adapter
- [ ] Update `validate_metamodel.py` script
- [ ] Test metamodel validation with new module
- [ ] Verify all validation checks still work

### Phase 5: Remove Old Code

- [ ] Search for remaining usages of `neo4j_utils`
- [ ] Search for remaining usages of `Neo4jConnection`
- [ ] Search for remaining usages of `.neo4j_conn`
- [ ] Remove `src/database/neo4j_utils.py`
- [ ] Remove deprecated `ToolContext` fields
- [ ] Update CLAUDE.md architecture section
- [ ] Update docs/db_refactor.md
- [ ] Update README.md examples
- [ ] Run full test suite
- [ ] Run metamodel validator
- [ ] Test validator agent end-to-end
- [ ] Create migration completion PR

---

## Appendix

### A. Comparison: Old vs New

#### Creating a Requirement

**OLD (neo4j_utils.py)**
```python
query = """
CREATE (r:Requirement {
    rid: $rid,
    name: $name,
    text: $text,
    type: $type,
    priority: $priority,
    status: 'active',
    goBDRelevant: $gobd_relevant,
    gdprRelevant: $gdpr_relevant,
    complianceStatus: $compliance_status,
    createdAt: datetime(),
    createdBy: 'validator_agent'
})
RETURN r.rid AS rid
"""

results = neo4j_conn.execute_query(query, {
    "rid": rid,
    "name": name,
    "text": text,
    "type": req_type,
    "priority": priority,
    "gobd_relevant": gobd_relevant,
    "gdpr_relevant": gdpr_relevant,
    "compliance_status": compliance_status,
})
```

**NEW (neo4j_db.py)**
```python
req = CreateRequirementRequest(
    rid=rid,
    name=name,
    text=text,
    type=req_type,
    priority=priority,
    gobd_relevant=gobd_relevant,
    gdpr_relevant=gdpr_relevant,
    compliance_status=compliance_status,
)

req.validate()  # Type-safe validation
result = neo4j_db.requirements.create(req)  # Returns Requirement object
```

#### Finding Similar Requirements

**OLD**
```python
# Load all requirements
query = """
MATCH (r:Requirement)
RETURN r.rid AS rid, r.name AS name, r.text AS text
LIMIT 1000
"""
all_reqs = neo4j_conn.execute_query(query)

# Manual similarity calculation
from difflib import SequenceMatcher
text_lower = text.lower().strip()
similar = []

for req in all_reqs:
    req_text = (req.get("text") or "").lower().strip()
    if not req_text:
        continue

    similarity = SequenceMatcher(None, text_lower, req_text).ratio()

    if similarity >= threshold:
        similar.append({
            "rid": req.get("rid"),
            "name": req.get("name"),
            "similarity_score": round(similarity, 3),
        })

similar.sort(key=lambda x: x["similarity_score"], reverse=True)
```

**NEW**
```python
similar = neo4j_db.requirements.find_similar(text, threshold=0.7)
# Returns List[SimilarRequirement] with type-safe objects
```

### B. Named Query Examples

#### Requirements Query File

```cypher
// name: create_requirement
// description: Create a new Requirement node
CREATE (r:Requirement {
    rid: $rid,
    name: $name,
    text: $text,
    ...
})
RETURN r

// name: get_requirement_by_rid
// description: Get a requirement by its RID
MATCH (r:Requirement {rid: $rid})
RETURN r

// name: list_requirements
// description: List requirements with filters
MATCH (r:Requirement)
WHERE ($type IS NULL OR r.type = $type)
  AND ($priority IS NULL OR r.priority = $priority)
RETURN r
ORDER BY r.createdAt DESC
SKIP $offset
LIMIT $limit
```

### C. Type-Safe API Examples

```python
# Request validation
req = CreateRequirementRequest(
    rid="INVALID",  # Will fail validation
    name="Test",
    text="Test text",
)

try:
    req.validate()
except ValueError as e:
    print(f"Validation failed: {e}")
    # Output: Invalid RID format: INVALID (expected R-XXXX)

# Response type safety
requirement: Requirement = neo4j_db.requirements.get_by_rid("R-0001")
print(requirement.name)  # Type-safe access
print(requirement.created_at)  # datetime object, not string

# List operations with filters
filters = RequirementFilters(
    type="functional",
    gobd_relevant=True,
    compliance_status="open",
)

results: List[Requirement] = neo4j_db.requirements.list(
    filters=filters,
    limit=50,
    offset=0,
)

for req in results:
    print(f"{req.rid}: {req.name} (compliance: {req.compliance_status})")
```

### D. Transaction Example

```python
# Create requirement with relationships in a single transaction
with neo4j_db.transaction() as tx:
    # Create requirement
    req_query = neo4j_db._queries.requirements.get("create_requirement")
    tx.run(req_query, req_params)

    # Create fulfillment relationship
    rel_query = neo4j_db._queries.relationships.get("create_fulfilled_by_object")
    tx.run(rel_query, rel_params)

    # Update compliance status
    status_query = neo4j_db._queries.requirements.get("update_compliance_status")
    tx.run(status_query, {"rid": rid})

    # All committed atomically or rolled back on exception
```

### E. References

- [Neo4j Python Driver Documentation](https://neo4j.com/docs/python-manual/current/)
- [Cypher Query Language Reference](https://neo4j.com/docs/cypher-manual/current/)
- [Neo4j Best Practices](https://neo4j.com/developer/guide-performance-tuning/)
- FINIUS Metamodel v2.0: `data/metamodell.cql`
- Current implementation: `src/database/neo4j_utils.py`

---

## End of Neo4j Migration Deep Dive
