# Cockpit Performance Issues

Performance audit of the cockpit Angular frontend and its orchestrator backend, focused on large jobs (5k+ audit entries).

## Architecture Overview

The cockpit uses a **sliding window pattern** with IndexedDB caching:
- Audit entries fetched in bulk (5000/chunk) from the API and cached in IndexedDB (Dexie.js)
- A window of ~1000 audit entries kept in memory, re-centered as the slider moves
- Chat entries and graph deltas use different indexing (sequence_number and toolCallIndex respectively), so they bypass windowing entirely
- Computed signals filter the in-memory data by slider position for display

---

## Critical Issues

### 1. `loadWindow()` reloads ALL chat + graph data on every window shift

**Location**: `cockpit/src/app/core/services/data.service.ts:579-588`

Every time the slider crosses a window boundary, `loadWindow()` reads *all* chat entries and *all* graph deltas from IndexedDB — even though only audit entries actually need re-windowing. The original developer noted the different indexing schemes and punted:

```typescript
// Load ALL chat entries and graph deltas (they use different indexing)
const chatCount = await this.db.getChatEntryCount(jobId);
const graphCount = await this.db.getGraphDeltaCount(jobId);
const [chatEntries, graphDeltas] = await Promise.all([
  this.db.getChatEntries(jobId, 0, chatCount),
  this.db.getGraphDeltas(jobId, 0, graphCount),
]);
```

The computed signals already handle filtering (chat by timestamp, graph by toolCallIndex), so chat/graph data only needs to be loaded **once per job**, not on every window shift.

**Fix**: Extract chat/graph loading into a `loadChatAndGraphData(jobId)` method called once in `loadJob()`. Have `loadWindow()` only reload audit entries.

---

### 2. N+1 MongoDB queries on job listing

**Location**: `orchestrator/main.py:327-329`

For every job in the list response, a separate `count_documents()` call fetches the audit entry count:

```python
for job in jobs:
    job_id = str(job["id"])
    job["audit_count"] = await mongodb.get_audit_count(job_id)
```

With `limit=100` jobs, this is 100 MongoDB round-trips just to render the job list. Same pattern at line 347-349 for the single-job endpoint.

**Fix**: Batch lookup using a MongoDB aggregation pipeline with `$facet`, or a single `$group` query for all requested job IDs.

---

### 3. `get_job_version()` makes 3 separate `count_documents()` calls

**Location**: `orchestrator/database/mongodb.py:633-685`

This endpoint is called on every job load AND every 15-second auto-refresh tick:

```python
audit_count = await audit_collection.count_documents({"job_id": job_id})
chat_count = await chat_collection.count_documents({"job_id": job_id})
graph_count = await audit_collection.count_documents({
    "job_id": job_id, "step_type": "tool", "tool.name": "execute_cypher_query"
})
```

**Fix**: Single aggregation pipeline with `$facet` to get all three counts in one query.

---

## High Priority Issues

### 4. Missing MongoDB indexes on `chat_history` collection

**Location**: `orchestrator/init.py:406-429`

The `chat_history` collection has **zero indexes**. Every query against it is a full collection scan. The `agent_audit` collection also lacks a compound index on `(job_id, step_type, tool.name)` needed for graph delta queries.

**Fix**: Add indexes during init:
- `chat_history`: `(job_id, 1)`, `(job_id, sequence_number)`
- `agent_audit`: `(job_id, step_type, tool.name)` for graph delta filtering

---

### 5. No field projection on bulk endpoints

**Location**: `orchestrator/database/mongodb.py:213-219, 449-505`

Full documents are returned from MongoDB including potentially huge `tool.result` and `llm.content` fields. For a 5k-entry job, this means transferring megabytes of data the frontend may never display (e.g., collapsed tool results).

**Fix**: Add MongoDB projections to exclude large nested fields, or return a summary payload and let the frontend fetch full details on expand.

---

### 6. `GraphService.renderedState` recomputes entire graph on every index change

**Location**: `cockpit/src/app/core/services/graph.service.ts`

The computed `renderedState` applies all deltas from index 0 to the current slider position, creating new Map objects each time. No memoization or incremental computation.

**Fix**: Track the last computed index. On slider movement, only apply deltas between the old and new index (or re-apply from a nearby snapshot if moving backwards).

---

## Medium Priority Issues

### 7. No `ChangeDetectionStrategy.OnPush` on heavy components

**Affected**:
- `cockpit/src/app/components/agent-activity/agent-activity.component.ts`
- `cockpit/src/app/components/chat-history/chat-history.component.ts`
- `cockpit/src/app/components/job-list/job-list.component.ts`

These components use Angular's default change detection. Since they use signals, `OnPush` is the correct strategy and would skip unnecessary re-evaluation cycles.

Note: `GraphTimelineComponent` already uses `OnPush` correctly.

---

### 8. No virtual scrolling in list components

All list components render every visible entry to the DOM. With 1000 entries in the audit window, that's 1000+ DOM nodes in agent-activity alone. Chat history has additional nesting (tool calls, inputs) multiplying the node count.

**Fix**: Implement `cdk-virtual-scroll-viewport` from `@angular/cdk/scrolling`. This would limit rendered DOM nodes to ~20-30 regardless of list size.

---

### 9. Auto-refresh clears cache and reloads everything

**Location**: `cockpit/src/app/core/services/data.service.ts:471-477`

When the auto-refresh detects new data, it clears the entire IndexedDB cache for the job and re-fetches all data from scratch:

```typescript
await this.db.clearJob(jobId);
this._currentJobId.set(null);
await this.loadJob(jobId);
```

For a 5k+ entry job, this means re-downloading and re-caching thousands of entries every 15 seconds if the job is still running.

**Fix**: Incremental fetch — use the version endpoint to determine how many new entries exist, then fetch only `offset=currentCount` onwards.

---

### 10. Linear search in `ChatHistoryComponent.getToolResult()`

For every tool call rendered, the component loops through the next entry's inputs to find the matching result by `tool_call_id`. With many tool calls per turn, this is O(n*m).

**Fix**: Pre-compute a `Map<tool_call_id, result>` when entries change.

---

## Low Priority Issues

### 11. No server-side caching

Every API request hits the database directly. There is no Redis, Memcached, or in-memory caching layer. Frequently accessed data like job counts and statistics are re-queried on every request.

### 12. No HTTP cache headers

API responses don't include `ETag`, `Cache-Control`, or `Last-Modified` headers. The browser can't make conditional requests or serve from HTTP cache.

### 13. IndexedDB has no eviction policy

Old job data accumulates in IndexedDB without cleanup. No storage quota monitoring or eviction of stale jobs.

### 14. Color lookup objects recreated per render

`toolCategoryColors` and `toolCategories` in `AgentActivityComponent` are defined as instance properties rather than module-level constants, causing object recreation on access.

---

## Backend-Specific Issues

### Inefficient time-range query

**Location**: `orchestrator/database/mongodb.py:284-319`

Uses two separate `find_one()` calls (sorted ascending and descending) instead of a single `$group` aggregation with `$min`/`$max`.

### Client-side aggregation for agent statistics

**Location**: `orchestrator/main.py:1100-1124`

Loads up to 500 agent records and counts statuses in Python instead of using SQL `COUNT(*) FILTER (WHERE ...)`.

### Missing PostgreSQL composite indexes

**Location**: `orchestrator/database/schema.sql:67-71`

No composite indexes for common filter+order patterns like `(status, created_at DESC)`.

---

## Recommended Fix Order

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| 1 | Load chat/graph once per job (#1) | Small | High — eliminates repeated IndexedDB reads on slider movement |
| 2 | Add MongoDB indexes for chat_history (#4) | Small | High — eliminates collection scans |
| 3 | Fix N+1 job listing queries (#2) | Medium | High — 100x faster job list endpoint |
| 4 | Consolidate version endpoint counts (#3) | Small | Medium — reduces auto-refresh overhead |
| 5 | Incremental auto-refresh (#9) | Medium | High — avoids full re-download every 15s |
| 6 | Add field projections (#5) | Small | Medium — reduces network transfer significantly |
| 7 | Add OnPush to components (#7) | Small | Medium — reduces Angular change detection cycles |
| 8 | Memoize graph state (#6) | Medium | Medium — faster slider scrubbing on graph view |
| 9 | Virtual scrolling (#8) | Large | Medium — needed for truly large entry counts |
| 10 | Server-side caching (#11) | Medium | Low-Medium — depends on access patterns |

---

## Implementation Roadmap

### Phase 1: Quick wins — Frontend data loading

Targets issues #1, #7, #14. Pure frontend changes, no API modifications needed.

#### 1a. Load chat/graph data once per job (Issue #1)

**Files**: `cockpit/src/app/core/services/data.service.ts`

Extract a `loadChatAndGraphData(jobId)` method:

```typescript
private async loadChatAndGraphData(jobId: string): Promise<void> {
  const chatCount = await this.db.getChatEntryCount(jobId);
  const graphCount = await this.db.getGraphDeltaCount(jobId);
  const [chatEntries, graphDeltas] = await Promise.all([
    this.db.getChatEntries(jobId, 0, chatCount),
    this.db.getGraphDeltas(jobId, 0, graphCount),
  ]);
  this._liveChatEntries.set(chatEntries);
  this._liveGraphDeltas.set(graphDeltas);
}
```

Call it once from `loadJob()` (after cache validation or after `fetchAndCacheJob()`). Remove the chat/graph loading from `loadWindow()` so it only handles audit entries:

```typescript
private async loadWindow(centerIndex: number): Promise<void> {
  // ... calculate start/end bounds (unchanged) ...
  const auditEntries = await this.db.getAuditEntries(jobId, start, end);
  this._windowStart.set(start);
  this._liveAuditEntries.set(auditEntries);
  // No more chat/graph loading here
}
```

**Verification**: Open a 5k+ job, scrub the slider back and forth rapidly. Should be smooth with no lag on window shifts.

#### 1b. Add OnPush to heavy components (Issue #7)

**Files**:
- `cockpit/src/app/components/agent-activity/agent-activity.component.ts`
- `cockpit/src/app/components/chat-history/chat-history.component.ts`
- `cockpit/src/app/components/job-list/job-list.component.ts`

Add to each component decorator:

```typescript
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  // ... rest unchanged
})
```

These components already use signals, so OnPush is the correct strategy. No template changes needed.

**Verification**: Open Chrome DevTools Performance tab, record while scrubbing slider. Change detection cycles should drop significantly.

#### 1c. Make color lookups constants (Issue #14)

**Files**: `cockpit/src/app/components/agent-activity/agent-activity.component.ts`

Move `toolCategoryColors` and `toolCategories` to module-level `const` declarations outside the component class.

---

### Phase 2: Backend database optimization

Targets issues #2, #3, #4, #5. Backend-only changes, no frontend modifications needed.

#### 2a. Add missing MongoDB indexes (Issue #4)

**File**: `orchestrator/init.py`

Add indexes during database initialization:

```python
# chat_history collection
await chat_collection.create_index("job_id", name="idx_chat_job_id")
await chat_collection.create_index(
    [("job_id", 1), ("sequence_number", 1)],
    name="idx_chat_job_seq"
)

# agent_audit: compound index for graph delta queries
await audit_collection.create_index(
    [("job_id", 1), ("step_type", 1), ("tool.name", 1)],
    name="idx_audit_job_type_tool"
)
```

**Verification**: Run `db.chat_history.find({job_id: "..."}).explain()` — should show `IXSCAN` instead of `COLLSCAN`. Repeat for graph delta queries.

#### 2b. Fix N+1 on job listing (Issue #2)

**File**: `orchestrator/main.py:313-336`, `orchestrator/database/mongodb.py`

Add a batch count method to mongodb.py:

```python
async def get_audit_counts_batch(self, job_ids: list[str]) -> dict[str, int]:
    pipeline = [
        {"$match": {"job_id": {"$in": job_ids}}},
        {"$group": {"_id": "$job_id", "count": {"$sum": 1}}},
    ]
    counts = {}
    async for doc in self.audit_collection.aggregate(pipeline):
        counts[doc["_id"]] = doc["count"]
    return counts
```

Replace the loop in main.py with a single batch call:

```python
jobs = await postgres_db.get_jobs(status=status, limit=limit)
if mongodb.is_available:
    job_ids = [str(j["id"]) for j in jobs]
    counts = await mongodb.get_audit_counts_batch(job_ids)
    for job in jobs:
        job["audit_count"] = counts.get(str(job["id"]), 0)
```

**Verification**: Check MongoDB profiler — job list endpoint should show 1 aggregate query instead of N count queries.

#### 2c. Consolidate version endpoint (Issue #3)

**File**: `orchestrator/database/mongodb.py:633-685`

Replace three `count_documents()` calls with a single `$facet` aggregation:

```python
async def get_job_version(self, job_id: str) -> dict:
    pipeline = [
        {"$match": {"job_id": job_id}},
        {"$facet": {
            "audit": [{"$count": "n"}],
            "graph": [
                {"$match": {"step_type": "tool", "tool.name": "execute_cypher_query"}},
                {"$count": "n"},
            ],
        }},
    ]
    result = await self.audit_collection.aggregate(pipeline).to_list(1)
    facets = result[0] if result else {}
    audit_count = facets.get("audit", [{}])[0].get("n", 0)
    graph_count = facets.get("graph", [{}])[0].get("n", 0)

    # Chat is in a separate collection, still needs its own count
    chat_count = await self.chat_collection.count_documents({"job_id": job_id})

    return {
        "auditEntryCount": audit_count,
        "chatEntryCount": chat_count,
        "graphDeltaCount": graph_count,
    }
```

This reduces the audit collection from 2 queries to 1. Chat still needs a separate query since it's in a different collection (but now has an index from step 2a).

**Verification**: Monitor with `db.setProfilingLevel(2)`. Version endpoint should show 2 queries (1 aggregate + 1 count) instead of 3 counts.

#### 2d. Add field projections to bulk endpoints (Issue #5)

**File**: `orchestrator/database/mongodb.py:449-505`

Add projection to exclude heavy fields from bulk responses. Define a standard projection for audit list views:

```python
AUDIT_LIST_PROJECTION = {
    "tool.result": 0,     # Can be megabytes (query results)
    "llm.messages": 0,    # Full message history per step
    "state": 0,           # Full agent state snapshots
}
```

Apply to `get_job_audit_bulk()` and `get_job_audit()`:

```python
cursor = collection.find(query, AUDIT_LIST_PROJECTION).sort(...).skip(...).limit(...)
```

Add a separate endpoint or parameter for fetching full entry details on expand.

**Verification**: Compare response sizes before/after with browser DevTools Network tab. Expect 50-80% reduction.

---

### Phase 3: Incremental data loading

Targets issues #9, #10. Requires coordinated frontend + backend changes.

#### 3a. Incremental auto-refresh (Issue #9)

**Files**:
- `cockpit/src/app/core/services/data.service.ts:453-483`

Replace the clear-and-reload pattern with incremental fetching:

```typescript
private async autoRefreshTick(): Promise<void> {
  if (this.isLoading()) return;
  try {
    const jobs = await firstValueFrom(this.api.getJobs());
    this.jobs.set(jobs);

    const jobId = this._currentJobId();
    if (!jobId) return;

    const versionInfo = await firstValueFrom(this.api.getJobVersion(jobId));
    const currentAuditCount = this._maxIndex() + 1;

    if (versionInfo && versionInfo.auditEntryCount > currentAuditCount) {
      // Fetch only new entries
      const newEntries = await firstValueFrom(
        this.api.getJobAuditBulk(jobId, currentAuditCount, this.BULK_FETCH_SIZE)
      );
      if (newEntries.entries.length > 0) {
        await this.db.cacheAuditEntries(jobId, newEntries.entries, currentAuditCount);
        this._maxIndex.set(versionInfo.auditEntryCount - 1);
        // Reload window if at the end (following live)
        if (this._sliderIndex() >= currentAuditCount - 1) {
          await this.loadWindow(versionInfo.auditEntryCount - 1);
          this._sliderIndex.set(versionInfo.auditEntryCount - 1);
        }
      }
      // Same pattern for chat and graph...
    }
  } catch (err) {
    console.warn('Auto-refresh error:', err);
  }
}
```

Also update `fetchAndCacheJob` to store metadata so incremental refresh knows the baseline.

**Verification**: Open a running job, watch network tab. After initial load, auto-refresh should only transfer new entries (small payloads) instead of the full dataset.

#### 3b. Pre-compute tool result map in ChatHistoryComponent (Issue #10)

**File**: `cockpit/src/app/components/chat-history/chat-history.component.ts`

Replace the per-render linear search with a computed map:

```typescript
private readonly toolResultMap = computed(() => {
  const entries = this.entries();
  const map = new Map<string, any>();
  for (const entry of entries) {
    for (const input of entry.inputs ?? []) {
      if (input.type === 'tool' && input.tool_call_id) {
        map.set(input.tool_call_id, input);
      }
    }
  }
  return map;
});

getToolResult(toolCallId: string) {
  return this.toolResultMap().get(toolCallId);
}
```

**Verification**: Profile with Chrome DevTools. `getToolResult` calls should be O(1) lookups.

---

### Phase 4: Rendering optimization

Targets issues #6, #8. More invasive frontend changes.

#### 4a. Memoize graph state computation (Issue #6)

**File**: `cockpit/src/app/core/services/graph.service.ts`

Track the last rendered index and graph state. On slider movement forward, only apply new deltas. On backward movement, find the nearest snapshot and replay from there:

```typescript
private lastRenderedIndex = -1;
private cachedNodes = new Map<string, NodeState>();
private cachedRelationships = new Map<string, RelationshipState>();

readonly renderedState = computed(() => {
  const targetIndex = this.currentIndex();
  const deltas = this.deltas();

  if (targetIndex >= this.lastRenderedIndex) {
    // Moving forward: apply only new deltas
    for (let i = this.lastRenderedIndex + 1; i <= targetIndex; i++) {
      this.applyDelta(deltas[i], this.cachedNodes, this.cachedRelationships);
    }
  } else {
    // Moving backward: rebuild from scratch (or nearest snapshot)
    this.cachedNodes.clear();
    this.cachedRelationships.clear();
    for (let i = 0; i <= targetIndex; i++) {
      this.applyDelta(deltas[i], this.cachedNodes, this.cachedRelationships);
    }
  }

  this.lastRenderedIndex = targetIndex;
  return { nodes: this.cachedNodes, relationships: this.cachedRelationships };
});
```

Note: Mutating Maps inside a computed signal requires care — the signal won't detect reference-equal Maps as changed. May need to return new Maps or use a version counter.

**Verification**: Open graph view on a large job. Scrubbing the slider forward should be near-instant. Backward scrubbing is slower but no worse than current behavior.

#### 4b. Add virtual scrolling (Issue #8)

**Files**:
- `cockpit/package.json` — add `@angular/cdk`
- `cockpit/src/app/components/agent-activity/agent-activity.component.ts`
- `cockpit/src/app/components/chat-history/chat-history.component.ts`

Install Angular CDK:
```bash
cd cockpit && npm install @angular/cdk
```

Replace the `@for` loop in agent-activity with a virtual scroll viewport:

```html
<cdk-virtual-scroll-viewport itemSize="48" class="entry-list">
  <div *cdkVirtualFor="let entry of entries(); trackBy: trackById"
       class="entry-item"
       [class.expanded]="isExpanded(entry._id)">
    <!-- existing template -->
  </div>
</cdk-virtual-scroll-viewport>
```

This limits rendered DOM nodes to ~20-30 regardless of list size. The `itemSize` needs tuning since entries have variable height (collapsed vs expanded). Consider using `autosize` strategy or a fixed estimate with `cdkVirtualScrollableElement`.

Chat history is harder due to variable-height entries with nested content. Options:
- Use `cdk-virtual-scroll-viewport` with estimated item size
- Or implement a simpler "render N nearest entries" approach using intersection observer

**Verification**: Open Chrome DevTools Elements panel. With 1000 entries in the window, only ~20-30 DOM nodes should be rendered in the list container.

---

### Phase 5: Backend polish

Targets issues #11, #12, and remaining backend items. Independent of frontend phases.

#### 5a. Add HTTP cache headers

**File**: `orchestrator/main.py`

Add cache headers to read-only endpoints:

```python
from fastapi.responses import JSONResponse

@app.get("/api/jobs/{job_id}/version")
async def get_job_version(job_id: str):
    data = await mongodb.get_job_version(job_id)
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "public, max-age=10"},  # 10s cache
    )
```

For static data (schema, table definitions): longer cache (`max-age=300`).
For dynamic data (audit, version): short cache (`max-age=5` or `10`).

#### 5b. PostgreSQL composite indexes

**File**: `orchestrator/database/schema.sql`

```sql
CREATE INDEX idx_jobs_status_created ON jobs(status, created_at DESC);
CREATE INDEX idx_jobs_agent_created ON jobs(assigned_agent_id, created_at DESC);
```

#### 5c. Server-side count caching

**File**: `orchestrator/database/mongodb.py`

Add a simple TTL cache for frequently queried counts:

```python
from functools import lru_cache
from asyncio import Lock
import time

class CountCache:
    def __init__(self, ttl_seconds=30):
        self._cache: dict[str, tuple[int, float]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> int | None:
        if key in self._cache:
            value, ts = self._cache[key]
            if time.monotonic() - ts < self._ttl:
                return value
        return None

    def set(self, key: str, value: int):
        self._cache[key] = (value, time.monotonic())
```

Use for audit/chat counts that don't need to be real-time accurate.

---

### Phase Summary

| Phase | Scope | Issues Addressed | Risk |
|-------|-------|------------------|------|
| 1 | Frontend only | #1, #7, #14 | Low — no API changes, easy to verify |
| 2 | Backend only | #2, #3, #4, #5 | Low — additive changes (indexes, projections) |
| 3 | Frontend + backend | #9, #10 | Medium — changes auto-refresh behavior |
| 4 | Frontend only | #6, #8 | Medium — rendering changes, new dependency (CDK) |
| 5 | Backend only | #11, #12 | Low — additive caching layer |

Phases 1 and 2 can be done in parallel. Phase 3 depends on phase 2 (indexes needed for incremental queries to be fast). Phase 4 is independent. Phase 5 is independent.
