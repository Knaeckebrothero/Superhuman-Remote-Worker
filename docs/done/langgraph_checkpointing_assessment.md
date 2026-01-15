# LangGraph Checkpointing Integration Assessment

**Date:** 2026-01-15
**Context:** 4-hour agent runs crash without recovery capability
**Goal:** Add LangGraph checkpointing to enable resumable graph execution

---

## Problem Statement

Currently when the Universal Agent crashes after hours of execution:
- ✅ Job record exists in PostgreSQL (via `jobs` table)
- ✅ Partial results may exist (via `requirements` table)
- ❌ **Graph execution state is lost** (no LangGraph checkpointing)
- ❌ Cannot resume from last checkpoint - must start over

The agent tracks *job-level* state but not *graph-level* state (which node, loop iteration, conversation history, etc.).

---

## Current Database Architecture

### PostgresDB Class (`src/database/postgres_db.py`)

```python
class PostgresDB:
    def __init__(self, connection_string: str, ...):
        self._pool: asyncpg.Pool  # Async connection pool
        self.jobs = JobsNamespace(self)
        self.requirements = RequirementsNamespace(self)
        self.citations = CitationsNamespace(self)
```

**Key characteristics:**
- Uses `asyncpg` for async operations
- Connection pooling (2-10 connections)
- Namespace-based API (`db.jobs.create()`, `db.requirements.list_by_job()`)
- Agent stores `postgres_conn` as a `PostgresDB` instance

### Schema (`src/database/queries/postgres/schema.sql`)

Already has a note on line 12:
```sql
-- Note: Agent checkpointing is handled by LangGraph's AsyncPostgresSaver.
```

**Tables:**
- `jobs` - Job tracking (status, timestamps, errors)
- `requirements` - Extracted requirements with validation state
- **No checkpoint tables yet** (will be created by PostgresSaver)

### Agent Connection Management (`src/agent.py`)

```python
# Line 175-176
self.postgres_conn = PostgresDB(connection_string=db_url)
await self.postgres_conn.connect()

# Line 492-493: Tools get access to PostgresDB
context = ToolContext(
    postgres_db=self.postgres_conn,  # PostgresDB instance
    neo4j_db=self.neo4j_conn,
)
```

---

## LangGraph Checkpointing Overview

### What Gets Checkpointed

LangGraph's `PostgresSaver` persists:
- **Graph state:** All fields in `UniversalAgentState` (messages, iteration, todos, etc.)
- **Node execution:** Which node just executed
- **Checkpoints:** After every node, before/after edges
- **Metadata:** Thread ID, timestamp, checkpoint ID

### Storage Requirements

PostgresSaver creates its own tables:
- `checkpoints` - Full state snapshots
- `checkpoint_writes` - Pending writes
- `checkpoint_migrations` - Schema version tracking

These are **separate from** your `jobs` and `requirements` tables.

### Resume Behavior

When resuming with the same `thread_id`:
```python
# Automatically resumes from last checkpoint
result = await graph.ainvoke(initial_state, config={
    "configurable": {"thread_id": "creator_job-123"}
})
```

---

## Integration Options

### Option A: Separate Connection (Recommended ✅)

**Approach:** PostgresSaver gets its own connection string, separate from PostgresDB.

**Pros:**
- ✅ Simplest implementation
- ✅ No changes to existing PostgresDB class
- ✅ Clean separation of concerns (checkpointing vs. business logic)
- ✅ PostgresSaver handles its own connection management

**Cons:**
- Additional database connections (minimal overhead)

**Implementation:**
```python
# In build_nested_loop_graph():
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Create checkpointer with connection string
checkpointer = AsyncPostgresSaver.from_conn_string(
    conn_string=os.getenv("DATABASE_URL")
)
await checkpointer.setup()  # Creates checkpoint tables

# Compile graph with checkpointer
return workflow.compile(checkpointer=checkpointer)
```

**Code changes required:**
1. `src/graph.py`: Import AsyncPostgresSaver, add checkpointer param
2. `src/agent.py`: Pass DATABASE_URL to graph builder, manage checkpointer lifecycle
3. Minor: Update cleanup logic to close checkpointer

---

### Option B: Shared Connection Pool

**Approach:** Create a `psycopg` pool shared between PostgresSaver and tools.

**Pros:**
- Single connection pool for all PostgreSQL operations

**Cons:**
- ❌ PostgresDB uses `asyncpg`, PostgresSaver uses `psycopg` (different drivers!)
- ❌ Would require rewriting PostgresDB to use psycopg
- ❌ Major refactor, high risk
- ❌ asyncpg and psycopg are incompatible

**Verdict:** Not recommended due to driver incompatibility.

---

### Option C: Dual-Driver Approach

**Approach:** Keep asyncpg for PostgresDB, add psycopg pool just for checkpointing.

**Pros:**
- ✅ No changes to PostgresDB
- ✅ PostgresSaver uses native psycopg

**Cons:**
- Two different connection pools to same database
- More complex lifecycle management
- Marginal benefit over Option A

**Verdict:** More complex than Option A with no real advantages.

---

## Recommended Solution: Option A

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Universal Agent                         │
│                                                             │
│  ┌──────────────────┐         ┌──────────────────┐        │
│  │   PostgresDB     │         │ AsyncPostgresSaver│        │
│  │   (asyncpg)      │         │   (psycopg)      │        │
│  │                  │         │                  │        │
│  │ - jobs           │         │ - checkpoints    │        │
│  │ - requirements   │         │ - checkpoint_    │        │
│  │ - citations      │         │   writes         │        │
│  └────────┬─────────┘         └────────┬─────────┘        │
│           │                            │                   │
└───────────┼────────────────────────────┼───────────────────┘
            │                            │
            └────────────┬───────────────┘
                         │
                    PostgreSQL
           (single database, different tables)
```

### Implementation Plan

#### 1. Update `src/graph.py`

**Changes:**
- Import `AsyncPostgresSaver` from `langgraph.checkpoint.postgres.aio`
- Add `checkpointer` parameter to `build_nested_loop_graph()`
- Pass checkpointer to `workflow.compile(checkpointer=checkpointer)`
- Return both compiled graph and checkpointer for lifecycle management

```python
# At top of file
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# In build_nested_loop_graph() signature
async def build_nested_loop_graph(
    llm: BaseChatModel,
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    system_prompt_template: str,
    workspace: WorkspaceManager,
    workspace_template: str = "",
    checkpointer: Optional[AsyncPostgresSaver] = None,  # NEW
) -> StateGraph:
    # ... existing node creation ...

    # Compile with checkpointer
    compiled = workflow.compile(checkpointer=checkpointer)
    return compiled
```

#### 2. Update `src/agent.py`

**Changes:**
- Create AsyncPostgresSaver in `process_job()`
- Pass checkpointer to `build_nested_loop_graph()`
- Clean up checkpointer after job completes
- Update thread_id to include job_id for resume capability

```python
# In UniversalAgent.__init__()
self._checkpointer: Optional[AsyncPostgresSaver] = None

# In process_job()
async def process_job(
    self,
    job_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    stream: bool = False,
    resume: bool = False,
) -> Dict[str, Any]:
    # ... existing setup ...

    # Create checkpointer for this job
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    self._checkpointer = AsyncPostgresSaver.from_conn_string(
        conn_string=os.getenv("DATABASE_URL")
    )
    await self._checkpointer.setup()  # Creates tables if needed

    try:
        # Build graph with checkpointer
        self._graph = build_nested_loop_graph(
            llm=self._llm,
            llm_with_tools=self._llm_with_tools,
            tools=self._tools,
            config=self.config,
            system_prompt_template=system_prompt_template,
            workspace=self._workspace_manager,
            workspace_template=workspace_template,
            checkpointer=self._checkpointer,  # NEW
        )

        # Thread ID includes job for resume
        thread_config = {
            "configurable": {
                "thread_id": f"{self.config.agent_id}_{job_id}",
            },
            "recursion_limit": self.config.limits.max_iterations * 2,
        }

        # ... rest of execution ...

    finally:
        # Clean up checkpointer
        if self._checkpointer:
            await self._checkpointer.close()
            self._checkpointer = None
```

#### 3. Resume Functionality

**No additional code needed!** LangGraph automatically resumes when:
- Same `thread_id` is used
- Checkpointer is provided
- Previous checkpoints exist

To resume after crash:
```bash
# Same job_id, graph resumes from last checkpoint
python agent.py --config creator --job-id abc-123-def --stream
```

#### 4. Database Migration

**No schema changes needed!** PostgresSaver creates its own tables:

```sql
-- Created automatically by checkpointer.setup()
CREATE TABLE checkpoints (...);
CREATE TABLE checkpoint_writes (...);
CREATE TABLE checkpoint_migrations (...);
```

These coexist with your `jobs` and `requirements` tables.

---

## Testing Strategy

### 1. Checkpoint Creation Test

```bash
# Start a long-running job
python agent.py --config creator --document-path large_doc.pdf --prompt "Extract"

# Kill it mid-execution (Ctrl+C)

# Verify checkpoints exist
psql -d graphrag -c "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 5;"
```

### 2. Resume Test

```bash
# Resume from checkpoint
python agent.py --config creator --job-id <uuid> --stream

# Should continue from where it left off, not restart
```

### 3. Checkpoint Cleanup Test

After job completes, old checkpoints can be cleaned up:
```python
# Optional: Clean old checkpoints for completed jobs
await checkpointer.delete_thread(thread_id="creator_job-123")
```

---

## Performance Considerations

### Overhead

- **Per-node checkpoint:** ~10-50ms write latency
- **Storage:** ~1-10KB per checkpoint (depends on state size)
- **Long jobs:** 500 iterations = 500 checkpoints = ~5MB

### Optimization Options

1. **Checkpoint less frequently:**
   ```python
   # Only checkpoint at phase boundaries
   workflow.compile(
       checkpointer=checkpointer,
       checkpoint=["archive_phase", "check_goal"]  # Only these nodes
   )
   ```

2. **State compaction:**
   - Already doing this with `archive_phase` context compaction
   - Keeps checkpoint size manageable

3. **Periodic cleanup:**
   - Delete checkpoints for completed jobs after N days
   - Keep only recent checkpoints per thread

---

## Debugging with PyCharm

### Can you use the debugger? YES!

**PyCharm debugging works fine with LangGraph:**
- ✅ Set breakpoints in node functions
- ✅ Step through graph execution
- ✅ Inspect state at each node
- ✅ Pause and resume execution

**However:**
- If you pause for hours, connections may timeout
- Checkpointing is better for "resume after crash" scenarios

### Best of Both Worlds

Use **both** debugging and checkpointing:
```python
# Debug with checkpointing enabled
# 1. Run with PyCharm debugger
# 2. If crash occurs (OOM, timeout, etc.), checkpoint survives
# 3. Resume from CLI without debugger for fast recovery
```

---

## Migration Path

### Phase 1: Add Checkpointing (No Breaking Changes)

1. Update `src/graph.py` to accept optional checkpointer
2. Update `src/agent.py` to create checkpointer per job
3. Test with short jobs (verify checkpoints created)
4. Test resume after manual kill

### Phase 2: Optional Optimizations

1. Add checkpoint cleanup for completed jobs
2. Consider selective checkpointing (only critical nodes)
3. Add monitoring for checkpoint table size

### Phase 3: Dashboard Integration

1. Show checkpoint count in job status
2. Add "Resume from Checkpoint" button in UI
3. Display checkpoint timestamps/nodes

---

## Code Changes Summary

### Files Modified

1. **`src/graph.py`** (~15 lines)
   - Import AsyncPostgresSaver
   - Add checkpointer param to `build_nested_loop_graph()`
   - Pass to `workflow.compile()`

2. **`src/agent.py`** (~30 lines)
   - Create checkpointer in `process_job()`
   - Pass to graph builder
   - Clean up in finally block
   - Already uses correct thread_id format

3. **`requirements.txt`** (no changes needed)
   - Already has `langgraph-checkpoint-postgres>=1.0.0`

### Files NOT Modified

- ❌ `src/database/postgres_db.py` (no changes)
- ❌ Schema files (checkpointer creates its own tables)
- ❌ Tools or managers (unaware of checkpointing)
- ❌ Config files (no new settings needed)

---

## Risk Assessment

### Low Risk ✅

- Checkpointing is isolated (separate tables)
- No changes to existing PostgresDB operations
- LangGraph checkpointing is well-tested
- Can rollback by removing checkpointer param

### Potential Issues

1. **Disk space:** Long jobs create many checkpoints
   - **Mitigation:** Cleanup old checkpoints periodically

2. **Write latency:** Checkpoint after every node
   - **Mitigation:** Negligible for 4-hour jobs (~500 iterations)

3. **Connection limit:** One more pool (minimal)
   - **Mitigation:** PostgreSQL default is 100 connections

---

## Next Steps

### Immediate (Before Code Changes)

1. ✅ Review this assessment
2. Decide on Option A (separate connection) vs alternatives
3. Verify `langgraph-checkpoint-postgres` version compatibility

### Implementation (If Approved)

1. Create feature branch `feature/langgraph-checkpointing`
2. Implement changes to `src/graph.py`
3. Implement changes to `src/agent.py`
4. Write checkpoint creation test
5. Write resume test
6. Verify in PyCharm debugger
7. Test 4-hour job with crash + resume

### Optional Enhancements

1. Add checkpoint cleanup job (daily cron)
2. Add checkpoint monitoring to dashboard
3. Document resume procedure in CLAUDE.md

---

## Questions to Resolve

1. **Checkpoint retention:** How long to keep checkpoints after job completion?
   - Recommend: 7 days for debugging, then auto-delete

2. **Selective checkpointing:** Checkpoint every node or only phase boundaries?
   - Recommend: Start with every node (default), optimize later if needed

3. **Error handling:** What if checkpoint write fails mid-execution?
   - LangGraph handles gracefully - continues execution, may skip that checkpoint

4. **Database cleanup:** Manual or automatic checkpoint deletion?
   - Recommend: Automatic cleanup script in `scripts/cleanup_checkpoints.py`

---

## Conclusion

**Recommended:** Implement Option A (Separate Connection) for LangGraph checkpointing.

**Impact:**
- ✅ Enables resume after crash
- ✅ Minimal code changes (~45 lines total)
- ✅ No changes to existing PostgresDB architecture
- ✅ Compatible with PyCharm debugging
- ✅ Low risk, high value

**Effort:** ~2-3 hours (implementation + testing)

**Risk:** Low (isolated change, well-tested library)

The agent will automatically create checkpoints during execution. After a crash, simply run with the same `--job-id` and it resumes from the last successful checkpoint.
