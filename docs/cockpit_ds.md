# Cockpit Data Service Refactor

This document describes the architectural refactor of the Debug Cockpit's data layer to improve timeline navigation performance and component reactivity.

## Problem Statement

### Current Architecture Issues

The existing timeline slider implementation has several problems:

1. **Backend round-trips on every seek**: Moving the slider triggers `getPageForTimestamp()` which:
   - Sends HTTP request to backend
   - Backend counts MongoDB documents before timestamp
   - Calculates which page contains the target entry
   - Returns page number and index
   - Frontend loads that page
   - Frontend scrolls to target entry

2. **Tight coupling**: Timeline position is coupled to pagination state, making the code complex and hard to maintain.

3. **Laggy user experience**: Each slider movement incurs network latency (50-200ms), making seeking feel unresponsive.

4. **Complex timestamp-to-page calculations**: The backend logic to find which page contains a given timestamp is convoluted and error-prone.

### Current Data Flow

```
User drags slider
    ↓
TimelineComponent.seek(seconds)
    ↓
TimeService.seek(offsetMs) + increment globalSeekVersion
    ↓
AuditService detects globalSeekVersion change
    ↓
API.getPageForTimestamp(timestamp, filter)  ← HTTP round-trip
    ↓
MongoDB counts documents before timestamp
    ↓
Backend returns {page, index}
    ↓
AuditService.loadAuditEntries() with new page  ← Another HTTP round-trip
    ↓
Frontend displays entries and scrolls to targetIndex
```

## Proposed Solution

### Core Concept

Replace the timestamp-based pagination seeking with an **index-based filtering** approach:

1. Load job data once into IndexedDB (client-side storage)
2. Keep a sliding window of ~1000 entries in live memory
3. Slider controls an array index, not a timestamp
4. Components subscribe to filtered data slices via observables
5. Components render at the end of their data arrays (most recent visible)

### Benefits

| Aspect | Current | Proposed |
|--------|---------|----------|
| Seek latency | 100-400ms (2 HTTP calls) | <10ms (local) |
| Memory usage | Page-based, unpredictable | Controlled window (1000 entries) |
| Repeat visits | Full reload | Instant from IndexedDB cache |
| Offline support | None | Previously viewed jobs accessible |
| Component coupling | Tight (pagination + time) | Loose (subscribe to data slices) |

## Architecture

### Three-Layer Data Model

```
┌──────────────────────────────────────────────────────────────────┐
│                       DataService                                │
│                                                                  │
│  ┌────────────────┐      ┌─────────────────────────────────┐    │
│  │   IndexedDB    │      │     Live Memory Window          │    │
│  │  (full job)    │ ───▶ │   (1000 entries around          │    │
│  │                │      │    current slider position)     │    │
│  └────────────────┘      └─────────────────────────────────┘    │
│         ▲                            │                           │
│         │                    sliderIndex (0..length-1)           │
│    load on job                       │                           │
│    selection                         ▼                           │
│         │                 entries$: Observable<Entry[]>          │
│  ┌──────┴───────┐                    │                           │
│  │   Backend    │                    │                           │
│  │   (8085)     │                    │                           │
│  └──────────────┘                    │                           │
└──────────────────────────────────────┼───────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────┐
              ▼                        ▼                    ▼
        AgentActivity            ChatHistory          GraphChanges
        (local filter:           (scrolls to          (local filter:
         messages/tools)          bottom)              by node type)
```

### Loading Strategy

```
Job Selected
    │
    ▼
IndexedDB has job? ──yes──▶ Load metadata, check freshness
    │ no                          │
    ▼                             ▼
Fetch from backend          Stale? ──yes──▶ Fetch updates from backend
    │                             │ no            │
    ▼                             │               │
Store in IndexedDB ◀──────────────┴───────────────┘
    │
    ▼
Load window into memory (last 1000 or around slider position)
    │
    ▼
Expose via entries$
```

### IndexedDB Schema

Using [Dexie.js](https://dexie.org/) for a cleaner API over native IndexedDB:

```typescript
// cockpit/src/app/core/services/indexed-db.service.ts

import Dexie, { Table } from 'dexie';

interface CachedAuditEntry {
  id: string;              // Composite: `${jobId}_${index}`
  jobId: string;
  index: number;           // Position in job's audit trail
  timestamp: Date;
  stepType: string;
  data: AuditEntry;        // Full entry data
}

interface CachedJobMetadata {
  jobId: string;
  totalEntries: number;
  firstTimestamp: Date;
  lastTimestamp: Date;
  cachedAt: Date;
  version: number;         // For cache invalidation
}

class CockpitDatabase extends Dexie {
  auditEntries!: Table<CachedAuditEntry>;
  jobMetadata!: Table<CachedJobMetadata>;

  constructor() {
    super('cockpit-cache');
    this.version(1).stores({
      auditEntries: 'id, jobId, [jobId+index], [jobId+stepType]',
      jobMetadata: 'jobId'
    });
  }
}
```

### DataService Interface

```typescript
// cockpit/src/app/core/services/data.service.ts

@Injectable({ providedIn: 'root' })
export class DataService {
  // Signals for state
  private readonly _sliderIndex = signal<number>(0);
  private readonly _maxIndex = signal<number>(0);
  private readonly _windowStart = signal<number>(0);
  private readonly _liveEntries = signal<AuditEntry[]>([]);

  // Public readonly signals
  readonly sliderIndex = this._sliderIndex.asReadonly();
  readonly maxIndex = this._maxIndex.asReadonly();

  // Computed filtered views (global filter applied)
  readonly entries = computed(() => {
    const all = this._liveEntries();
    const index = this._sliderIndex();
    // Return entries from 0 to sliderIndex (inclusive)
    return all.slice(0, index + 1);
  });

  // Individual data streams for components
  readonly auditEntries$: Observable<AuditEntry[]>;
  readonly chatEntries$: Observable<ChatEntry[]>;
  readonly graphChanges$: Observable<GraphDelta[]>;
  readonly todoEntries$: Observable<TodoItem[]>;

  // Methods
  async loadJob(jobId: string): Promise<void>;
  setSliderIndex(index: number): void;
  async seekToTimestamp(timestamp: Date): Promise<void>;

  // Window management (internal)
  private async loadWindow(centerIndex: number): Promise<void>;
  private async ensureIndexInWindow(index: number): Promise<void>;
}
```

### Window Management

The live memory window holds ~1000 entries centered around the current slider position:

```typescript
private readonly WINDOW_SIZE = 1000;
private readonly WINDOW_PADDING = 200; // Load more when within 200 of edge

async ensureIndexInWindow(targetIndex: number): Promise<void> {
  const start = this._windowStart();
  const entries = this._liveEntries();
  const end = start + entries.length;

  // Check if target is near window edges
  if (targetIndex < start + this.WINDOW_PADDING ||
      targetIndex > end - this.WINDOW_PADDING) {
    // Recenter window around target
    await this.loadWindow(targetIndex);
  }
}

async loadWindow(centerIndex: number): Promise<void> {
  const jobId = this._currentJobId();
  const maxIndex = this._maxIndex();

  // Calculate window bounds
  const halfWindow = Math.floor(this.WINDOW_SIZE / 2);
  let start = Math.max(0, centerIndex - halfWindow);
  let end = Math.min(maxIndex, start + this.WINDOW_SIZE);

  // Adjust start if we hit the end
  if (end === maxIndex) {
    start = Math.max(0, end - this.WINDOW_SIZE);
  }

  // Load from IndexedDB
  const entries = await this.db.auditEntries
    .where('[jobId+index]')
    .between([jobId, start], [jobId, end], true, true)
    .toArray();

  this._windowStart.set(start);
  this._liveEntries.set(entries.map(e => e.data));
}
```

### Component Integration

Components subscribe to data service observables and can apply local filters:

```typescript
// Example: AgentActivityComponent

@Component({...})
export class AgentActivityComponent {
  private readonly data = inject(DataService);

  // Local filter state
  readonly localFilter = signal<'all' | 'messages' | 'tools' | 'errors'>('all');

  // Filtered view combining global slider position with local filter
  readonly displayedEntries = computed(() => {
    const entries = this.data.entries();
    const filter = this.localFilter();

    if (filter === 'all') return entries;

    const stepTypes = FILTER_MAPPINGS[filter];
    return entries.filter(e => stepTypes.includes(e.step_type));
  });

  // Auto-scroll to bottom when new entries arrive
  readonly autoScrollToLatest = signal(true);
}
```

### Slider Component

```typescript
// Timeline slider now controls index directly

@Component({
  template: `
    <input type="range"
           [min]="0"
           [max]="data.maxIndex()"
           [ngModel]="data.sliderIndex()"
           (ngModelChange)="onSliderChange($event)">
    <span>{{ currentTimestamp() }}</span>
  `
})
export class TimelineComponent {
  private readonly data = inject(DataService);

  // Computed timestamp from current entry
  readonly currentTimestamp = computed(() => {
    const entries = this.data.entries();
    if (entries.length === 0) return null;
    return entries[entries.length - 1].timestamp;
  });

  onSliderChange(index: number): void {
    this.data.setSliderIndex(index);
  }
}
```

## Implementation Plan

### Phase 1: IndexedDB Layer

1. Add Dexie.js dependency: `npm install dexie`
2. Create `IndexedDbService` with schema definition
3. Implement cache read/write operations
4. Add cache invalidation logic (TTL or manual refresh)

**Files to create:**
- `cockpit/src/app/core/services/indexed-db.service.ts`

### Phase 2: DataService Core

1. Create new `DataService` replacing current fragmented services
2. Implement job loading with IndexedDB caching
3. Implement window management (load/slide window)
4. Expose signals and observables for components

**Files to create:**
- `cockpit/src/app/core/services/data.service.ts`

**Files to modify:**
- `cockpit/src/app/core/services/api.service.ts` (add bulk fetch endpoints)

### Phase 3: Backend Bulk Endpoints

1. Add endpoint to fetch all audit entries for a job (paginated in large chunks)
2. Add endpoint to check job data version/freshness
3. Optimize MongoDB queries for bulk retrieval

**Files to modify:**
- `cockpit/api/main.py`
- `cockpit/api/services/mongodb.py`

### Phase 4: Component Migration

1. Update `TimelineComponent` to use index-based slider
2. Update `AgentActivityComponent` to subscribe to `DataService`
3. Update `ChatHistoryComponent` with auto-scroll-to-latest
4. Update `GraphTimelineComponent` to use filtered graph deltas
5. Update `TodoListComponent` (may not need changes if not time-synced)

**Files to modify:**
- `cockpit/src/app/components/timeline/timeline.component.ts`
- `cockpit/src/app/components/agent-activity/agent-activity.component.ts`
- `cockpit/src/app/components/chat-history/chat-history.component.ts`
- `cockpit/src/app/components/graph-timeline/graph-timeline.component.ts`

### Phase 5: Cleanup

1. Remove deprecated services (`AuditService`, `TimeService` timestamp logic)
2. Remove backend timestamp-to-page endpoints
3. Update tests

**Files to remove/deprecate:**
- Timestamp seeking logic in `AuditService`
- `getPageForTimestamp` in `api.service.ts` and backend

## API Changes

### New Endpoints

```
GET /api/jobs/{job_id}/audit/bulk?offset=0&limit=5000
  → Returns large batches of audit entries for caching

GET /api/jobs/{job_id}/audit/version
  → Returns { version: number, totalEntries: number, lastUpdate: Date }
     Used for cache invalidation
```

### Deprecated Endpoints

```
GET /api/jobs/{job_id}/audit/page-for-timestamp  (remove after migration)
```

## Migration Strategy

1. Implement new DataService alongside existing services
2. Add feature flag to toggle between old/new data flow
3. Test thoroughly with large jobs (10k+ entries)
4. Remove old implementation once stable

## Performance Expectations

| Metric | Current | Target |
|--------|---------|--------|
| Initial job load | 200-500ms | 500-1000ms (more data, but cached after) |
| Repeat job load | 200-500ms | <50ms (from IndexedDB) |
| Slider seek (within window) | 100-400ms | <5ms |
| Slider seek (outside window) | 100-400ms | <20ms (IndexedDB read) |
| Memory usage | Unbounded (pages accumulate) | ~10MB max (1000 entries window) |

## Open Questions

1. **Cache invalidation**: How long should cached job data be considered fresh? Options:
   - TTL-based (e.g., 1 hour)
   - Version-based (check on job select)
   - Manual refresh button only

2. **Running jobs**: For jobs still in progress, how to handle new entries?
   - Polling for updates?
   - WebSocket for real-time?
   - Manual refresh only?

3. **Storage limits**: IndexedDB has browser-specific limits. Should we:
   - Evict old jobs (LRU)?
   - Limit cached jobs count?
   - Let browser handle it?

## References

- [Dexie.js Documentation](https://dexie.org/docs/)
- [IndexedDB API](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API)
- [Angular Signals](https://angular.dev/guide/signals)
- Current implementation: `docs/debug_cockpit.md`
