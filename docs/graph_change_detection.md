# Graph Change Detection & Timeline Visualization

## Overview

This document describes the architecture for visualizing Neo4j graph changes made by agents over time. The goal is to provide a "time-lapse" experience where users can scrub through a job's timeline and see how the knowledge graph evolved.

### Use Case

A typical validator agent job might run for 5 hours and perform:
- 500+ LLM requests
- 3000+ tool calls
- 500+ node deletions/creations

Users need to:
1. Understand what the agent did to the graph
2. Correlate tool calls with graph changes
3. Identify large operations (mass deletions, bulk inserts)
4. Debug unexpected graph states

### Vision

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ◀ ▶  ━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│        14:32:15                                              19:47:22   │
│        Job Start              ▲ Current: 16:45:33            Job End    │
├─────────────────────────────────┬───────────────────────────────────────┤
│  AGENT ACTIVITY                 │  GRAPH CHANGES                        │
│  ─────────────────────────      │  ─────────────────────────            │
│  16:45:30 execute_cypher_query  │      ┌─────┐                          │
│  > CREATE (r:Requirement...)    │      │ R42 │ ← NEW (blue)             │
│                                 │      └──┬──┘                          │
│  16:45:31 execute_cypher_query  │         │ FULFILLED_BY                │
│  > MATCH (r)-[rel]->(bo)        │         ▼                             │
│    DELETE rel                   │      ┌─────┐                          │
│                                 │      │BO_7 │ ← MODIFIED (orange)      │
│  16:45:33 execute_cypher_query  │      └─────┘                          │
│  > CREATE (r)-[:FULFILLED]->... │                                       │
│                          ▲      │  Legend: 🔵 Created  🟠 Modified       │
│                          │      │          🔴 Deleted  ⚪ Unchanged      │
│              current ────┘      │                                       │
└─────────────────────────────────┴───────────────────────────────────────┘
```

---

## Library Recommendation: Cytoscape.js

**Cytoscape.js v3.31+ with the cytoscape-angular wrapper** is the recommended choice for this implementation.

### Why Cytoscape.js

1. **WebGL Renderer (January 2025)**: Handles 1,200+ nodes with 16,000 edges at 100+ FPS
2. **Angular Integration**: `cytoscape-angular` provides native Angular 21 signals support, standalone components, and `takeUntilDestroyed` lifecycle patterns
3. **Rich Algorithm Library**: Built-in PageRank, centrality, clustering algorithms
4. **Proven Ecosystem**: Navigator extension for minimap, fCoSE for stable layouts

### Rendering Backend Decision Matrix

| Scale | Recommended Renderer | Rationale |
|-------|---------------------|-----------|
| <500 nodes | Canvas | Simpler debugging, good performance |
| 500-2000 nodes | Canvas or WebGL | Canvas sufficient; WebGL if animations heavy |
| 2000+ nodes | WebGL required | Canvas drops below 30fps |

**Recommendation**: Start with Canvas, switch to WebGL if scrubbing performance degrades. The API remains identical.

### Alternatives Considered

| Library | Pros | Cons |
|---------|------|------|
| **Sigma.js v3** | Pure WebGL, fast | No Angular wrapper, less algorithms |
| **G6 (AntV)** | Built-in animation | React-focused, no Angular wrapper |
| **Cosmos.gl** | Millions of nodes | Overkill for our scale |
| **ngx-graph** | Angular-native | Fewer features, limited for large graphs |

---

## Architecture

### Data Flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          GRAPH CHANGE PIPELINE                            │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐  │
│  │  MongoDB    │    │   Cypher    │    │     Graph Change Store      │  │
│  │   Audit     │───▶│   Parser    │───▶│  (snapshots + deltas)       │  │
│  │   Trail     │    │ (WebWorker) │    │                             │  │
│  └─────────────┘    └─────────────┘    └──────────────┬──────────────┘  │
│                                                       │                  │
│                                                       ▼                  │
│  ┌─────────────┐    ┌─────────────────────────────────────────────────┐ │
│  │  Timeline   │    │              Graph Component                     │ │
│  │   Slider    │───▶│  Renders nodes touched up to slider position    │ │
│  │  (velocity) │    │  Colors by state: created/modified/deleted      │ │
│  └─────────────┘    └─────────────────────────────────────────────────┘ │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Three-Tier Caching Architecture

Based on patterns from video editing systems and Git's packfile design:

```
L1: Hot Window Cache (in-memory)
├── Current position ± 50 operations
├── Fully materialized graph states
└── Size: ~10 snapshots

L2: Anchor Cache (IndexedDB)
├── All snapshot anchors (every ~55-100 ops)
├── ~30-55 anchors for 3000 ops
└── LRU eviction for inactive regions

L3: Full Event Store (FastAPI backend)
├── Complete delta log
├── All anchors (compressed)
└── MongoDB for audit trail persistence
```

### Data Source: MongoDB Audit Trail

The agent already logs all tool calls to MongoDB via `audit_tool_call()`:

```python
# From src/database/mongo_db.py
{
    "job_id": "abc-123",
    "agent_type": "validator",
    "event_type": "tool_call",
    "tool_name": "execute_cypher_query",
    "inputs": {"query": "CREATE (r:Requirement {id: 'R42'})..."},
    "output": "Query executed successfully...",
    "timestamp": "2024-01-23T16:45:30.123Z"
}
```

The `inputs.query` field contains the Cypher query, which tells us what changed.

---

## Cypher Query Parsing

### Supported Operations

| Cypher Pattern | Action | Extraction |
|----------------|--------|------------|
| `CREATE (n:Label {...})` | Create Node | label, properties |
| `CREATE (a)-[:TYPE]->(b)` | Create Relationship | source, target, type |
| `DELETE n` | Delete Node | node variable |
| `DELETE r` (relationship) | Delete Relationship | relationship variable |
| `DETACH DELETE n` | Delete Node + Relationships | node variable |
| `MERGE (n:Label {...})` | Upsert Node | label, match properties |
| `SET n.prop = value` | Modify Property | node, property, value |
| `REMOVE n.prop` | Remove Property | node, property |
| `REMOVE n:Label` | Remove Label | node, label |

### Parser Implementation

```typescript
interface ParsedCypherOperation {
  type: 'CREATE_NODE' | 'CREATE_REL' | 'DELETE_NODE' | 'DELETE_REL' |
        'MERGE_NODE' | 'SET_PROP' | 'REMOVE_PROP' | 'DETACH_DELETE';
  nodeLabel?: string;
  nodeId?: string;
  properties?: Record<string, any>;
  relationshipType?: string;
  sourceRef?: string;
  targetRef?: string;
}

function parseCypherQuery(query: string): ParsedCypherOperation[] {
  const operations: ParsedCypherOperation[] = [];

  // CREATE node pattern: CREATE (var:Label {props})
  const createNodeRegex = /CREATE\s+\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)/gi;
  let match;
  while ((match = createNodeRegex.exec(query)) !== null) {
    operations.push({
      type: 'CREATE_NODE',
      nodeLabel: match[2],
      properties: parseProperties(match[3])
    });
  }

  // DELETE pattern: DELETE var or DETACH DELETE var
  const deleteRegex = /(DETACH\s+)?DELETE\s+(\w+)/gi;
  while ((match = deleteRegex.exec(query)) !== null) {
    operations.push({
      type: match[1] ? 'DETACH_DELETE' : 'DELETE_NODE',
      nodeId: match[2]  // Variable reference, resolved later
    });
  }

  // CREATE relationship: CREATE (a)-[:TYPE]->(b)
  const createRelRegex = /CREATE\s+\((\w+)\)-\[:(\w+)\]->\((\w+)\)/gi;
  while ((match = createRelRegex.exec(query)) !== null) {
    operations.push({
      type: 'CREATE_REL',
      sourceRef: match[1],
      relationshipType: match[2],
      targetRef: match[3]
    });
  }

  // MERGE pattern: MERGE (var:Label {props})
  const mergeRegex = /MERGE\s+\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)/gi;
  while ((match = mergeRegex.exec(query)) !== null) {
    operations.push({
      type: 'MERGE_NODE',
      nodeLabel: match[2],
      properties: parseProperties(match[3])
    });
  }

  // SET pattern: SET var.prop = value
  const setRegex = /SET\s+(\w+)\.(\w+)\s*=\s*(['"]?)([^'",\s]+)\3/gi;
  while ((match = setRegex.exec(query)) !== null) {
    operations.push({
      type: 'SET_PROP',
      nodeId: match[1],
      properties: { [match[2]]: match[4] }
    });
  }

  return operations;
}

function parseProperties(propsString?: string): Record<string, any> {
  if (!propsString) return {};
  const props: Record<string, any> = {};
  // Parse "key: 'value', key2: 123" format
  const propRegex = /(\w+)\s*:\s*(['"]?)([^'",}]+)\2/g;
  let match;
  while ((match = propRegex.exec(propsString)) !== null) {
    props[match[1]] = match[3];
  }
  return props;
}
```

### Parser Limitations

The regex-based parser handles ~80% of common Cypher patterns. Edge cases:
- Complex MATCH clauses with multiple patterns
- UNWIND operations
- CALL procedures
- Subqueries

For production, consider using a proper Cypher parser library or enhancing the agent to log structured change data alongside the query.

---

## Snapshot + Delta Model

### The Problem

Naive approach: On every slider move, recompute the entire graph state from the beginning.
- 3000 tool calls × 60 slider events/second = 180,000 computations/second
- Result: Unusable lag

### The Solution: Anchor + Delta Architecture

Based on patterns from temporal graph databases (AeonG, VLDB 2024), Git's packfile design, and video editing systems.

```
Timeline:  ────●────────────────●────────────────●────────────────●────
               │                │                │                │
           Snapshot 0       Snapshot 1       Snapshot 2       Snapshot 3
           (t=0)            (t=55)           (t=110)          (t=165)
               │                │
               ├── Δ₁ (t=1)     ├── Δ₁ (t=56)
               ├── Δ₂ (t=2)     ├── Δ₂ (t=57)
               ├── ...          ├── ...
               └── Δ₅₅ (t=54)   └── Δ₅₅ (t=109)

Slow scrub (t=50 → t=60):  Apply deltas incrementally (fast)
Fast scrub (t=50 → t=165): Jump directly to Snapshot 3 (fast)
```

### Optimal Snapshot Interval: √N Formula

For N operations, snapshot every √N operations to achieve O(√N) reconstruction time:

| Total Operations | √N | Recommended Interval | Snapshots |
|-----------------|-----|---------------------|-----------|
| 1000 | 31.6 | 30-50 | 20-33 |
| 3000 | 54.8 | 55-100 | 30-55 |
| 5000 | 70.7 | 70-100 | 50-72 |

**For 3000 operations**: √3000 ≈ 55. Round to **100** for simpler implementation, yielding ~30 anchor snapshots.

### Delta Chain Depth Limit

Following Git's proven pattern, **limit delta chain depth to 50**. This bounds reconstruction time even in worst-case scenarios where the user seeks to a position far from any snapshot.

### Data Structures

```typescript
/**
 * Complete graph state at a point in time.
 * Created at regular intervals for fast seeking.
 */
interface GraphSnapshot {
  /** Timestamp of this snapshot */
  timestamp: number;

  /** Index of the tool call this snapshot represents */
  toolCallIndex: number;

  /** All nodes that exist at this point */
  nodes: Map<string, NodeState>;

  /** All relationships that exist at this point */
  relationships: Map<string, RelationshipState>;

  /** Pre-computed node positions for layout stability */
  positions?: Map<string, { x: number; y: number }>;
}

interface NodeState {
  id: string;
  labels: string[];
  properties: Record<string, any>;

  /** When this node was first seen (for color coding) */
  createdAt: number;

  /** Last modification timestamp */
  modifiedAt: number;

  /** Visibility flag for deleted nodes (enables instant restoration) */
  visible: boolean;
}

interface RelationshipState {
  id: string;
  type: string;
  sourceId: string;
  targetId: string;
  properties: Record<string, any>;
  createdAt: number;
  visible: boolean;
}

/**
 * Incremental change between tool calls.
 * Applied sequentially during slow scrubbing.
 */
interface GraphDelta {
  /** Timestamp of this change */
  timestamp: number;

  /** Tool call index */
  toolCallIndex: number;

  /** Original Cypher query (for display) */
  cypherQuery: string;

  /** Tool call ID from MongoDB (for linking to audit panel) */
  toolCallId: string;

  /** The actual changes */
  changes: {
    nodesCreated: NodeState[];
    nodesDeleted: string[];  // Node IDs
    nodesModified: Array<{
      id: string;
      properties: Record<string, any>;
      previousProperties?: Record<string, any>;  // For undo
    }>;
    relationshipsCreated: RelationshipState[];
    relationshipsDeleted: string[];  // Relationship IDs
  };
}
```

### Snapshot Creation Strategy

```typescript
const SNAPSHOT_INTERVAL = 100;  // Every 100 tool calls (~√3000)
const MAX_DELTA_CHAIN = 50;     // Git's proven default

function shouldCreateSnapshot(
  index: number,
  delta: GraphDelta,
  lastSnapshotIndex: number
): boolean {
  // Regular interval
  if (index % SNAPSHOT_INTERVAL === 0) return true;

  // Delta chain depth limit
  if (index - lastSnapshotIndex >= MAX_DELTA_CHAIN) return true;

  // Large operations (mass delete/create)
  if (delta.changes.nodesDeleted.length > 50) return true;
  if (delta.changes.nodesCreated.length > 50) return true;

  // Phase transitions (strategic ↔ tactical)
  if (isPhaseTransition(delta)) return true;

  return false;
}
```

### Memory Budget

For a 3000 tool call job with 500 unique nodes:

| Data Structure | Size Estimate |
|----------------|---------------|
| 30 snapshots × 500 nodes × 100 bytes | ~1.5 MB |
| 3000 deltas × 200 bytes average | ~600 KB |
| L1 hot cache (10 snapshots) | ~500 KB |
| Cytoscape elements | ~2 MB |
| **Total** | **~4.6 MB** |

Well within browser memory limits.

---

## Layout Stability: fCoSE Configuration

The **fCoSE (Fast Compound Spring Embedder)** algorithm with `randomize: false` prevents the graph from "jumping" when nodes are incrementally added or removed.

```typescript
// Critical: use existing positions as starting point
cy.layout({
  name: 'fcose',
  randomize: false,           // KEY: preserves existing positions
  quality: 'proof',           // Slower cooling = more stability
  animate: true,
  animationDuration: 300,
  nodeRepulsion: 4500,
  idealEdgeLength: 50,

  // Stability techniques
  fixedNodeConstraint: pinnedNodes,  // Keep important nodes fixed
  alignmentConstraint: alignments,   // Maintain visual groupings
}).run();
```

### Additional Stability Techniques

1. **Pinning Weights**: Assign higher stability weights to older nodes; new nodes find positions around fixed neighbors
2. **Initial Placement**: Use `cytoscape-layout-utilities` to place new nodes near connected neighbors before running layout
3. **Pre-computed Positions**: Store positions in snapshots, interpolate between them during scrubbing

---

## Angular 21 Integration

### Critical: Run Cytoscape Outside NgZone

This is the most important performance optimization. Graph animations fire hundreds of events that trigger unnecessary Angular change detection:

```typescript
@Component({
  selector: 'app-graph-timeline',
  standalone: true,
  imports: [CytoscapeGraphComponent],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class GraphTimelineComponent implements OnInit, OnDestroy {
  private readonly ngZone = inject(NgZone);
  private readonly destroyRef = inject(DestroyRef);
  private cy!: cytoscape.Core;
  private cyListeners: Array<() => void> = [];

  // Reactive signals for Angular UI
  readonly selectedNode = signal<NodeState | null>(null);
  readonly timelinePosition = signal<number>(0);

  ngOnInit(): void {
    // Initialize OUTSIDE Angular zone
    this.ngZone.runOutsideAngular(() => {
      this.cy = cytoscape({
        container: this.graphContainer.nativeElement,
        style: cytoscapeStyles,
        // ... config
      });

      // All event handlers stay outside zone
      const panHandler = () => this.handleViewportChange();
      this.cy.on('pan zoom', panHandler);
      this.cyListeners.push(() => this.cy.off('pan zoom', panHandler));

      const tapHandler = (evt: any) => this.handleNodeTap(evt);
      this.cy.on('tap', 'node', tapHandler);
      this.cyListeners.push(() => this.cy.off('tap', 'node', tapHandler));
    });
  }

  // Re-enter zone ONLY when updating Angular UI
  private handleNodeTap(evt: any): void {
    const nodeData = evt.target.data();
    this.ngZone.run(() => {
      this.selectedNode.set(nodeData);
    });
  }

  ngOnDestroy(): void {
    // Clean up all listeners to prevent memory leaks
    this.cyListeners.forEach(cleanup => cleanup());
    this.cy?.destroy();
  }
}
```

### Signal-Based Reactive State

```typescript
@Component({
  standalone: true,
  imports: [CytoscapeGraphComponent]
})
export class GraphTimelineComponent {
  readonly nodes = signal<NodeDefinition[]>([]);
  readonly edges = signal<EdgeDefinition[]>([]);
  readonly timelinePosition = signal<number>(0);

  // Computed graph state based on timeline position
  readonly graphState = computed(() =>
    this.computeStateAtPosition(this.timelinePosition())
  );

  constructor() {
    // Effect to update Cytoscape when state changes
    effect(() => {
      const state = this.graphState();
      this.ngZone.runOutsideAngular(() => {
        this.cy?.batch(() => {
          this.applyGraphState(state);
        });
      });
    });
  }
}
```

### requestAnimationFrame Gating for Smooth Scrubbing

Prevent render queue overflow during rapid scrubbing:

```typescript
private pendingPosition: number | null = null;
private rafId: number | null = null;

onTimelineChange(position: number): void {
  this.pendingPosition = position;

  if (!this.rafId) {
    this.rafId = requestAnimationFrame(() => {
      if (this.pendingPosition !== null) {
        this.updateGraph(this.pendingPosition);
      }
      this.pendingPosition = null;
      this.rafId = null;
    });
  }
}
```

---

## Timeline Renderer Implementation

```typescript
class GraphTimelineRenderer {
  private cy: cytoscape.Core;
  private snapshots: GraphSnapshot[] = [];
  private deltas: GraphDelta[] = [];

  private currentSnapshotIndex: number = 0;
  private appliedDeltaIndex: number = 0;
  private lastSeekTime: number = 0;
  private lastUpdateTime: number = 0;

  /**
   * Load and process changes for a job.
   */
  async loadJob(jobId: string): Promise<void> {
    // 1. Fetch from L3 (backend) or L2 (IndexedDB cache)
    const changes = await this.fetchWithCaching(jobId);

    // 2. Build snapshots and deltas (in Web Worker)
    const { snapshots, deltas } = await this.worker.buildSnapshotsAndDeltas(changes);
    this.snapshots = snapshots;
    this.deltas = deltas;

    // 3. Warm L1 cache around initial position
    this.warmL1Cache(0);

    // 4. Render initial state
    this.renderSnapshot(this.snapshots[0]);
  }

  /**
   * Seek to a specific time. Velocity determines rendering strategy.
   */
  seekTo(time: number): void {
    const now = performance.now();
    const elapsed = now - this.lastUpdateTime;
    const velocity = elapsed > 0 ? Math.abs(time - this.lastSeekTime) / elapsed : 0;

    this.lastSeekTime = time;
    this.lastUpdateTime = now;

    const FAST_THRESHOLD = 500;  // Units per second

    if (velocity > FAST_THRESHOLD) {
      this.jumpToNearestSnapshot(time);
    } else {
      this.applyDeltasTo(time);
    }
  }

  /**
   * Fast seek: Jump directly to nearest snapshot.
   */
  private jumpToNearestSnapshot(time: number): void {
    const snapshot = this.findNearestSnapshot(time);
    this.renderFullSnapshot(snapshot);
    this.currentSnapshotIndex = this.snapshots.indexOf(snapshot);
    this.appliedDeltaIndex = snapshot.toolCallIndex;

    // Pre-warm L1 cache around new position
    this.warmL1Cache(this.appliedDeltaIndex);
  }

  /**
   * Slow seek: Apply deltas incrementally.
   */
  private applyDeltasTo(time: number): void {
    const targetIndex = this.findDeltaIndexAt(time);

    if (targetIndex > this.appliedDeltaIndex) {
      // Moving forward
      for (let i = this.appliedDeltaIndex + 1; i <= targetIndex; i++) {
        this.applyDeltaForward(this.deltas[i]);
      }
    } else if (targetIndex < this.appliedDeltaIndex) {
      // Moving backward
      for (let i = this.appliedDeltaIndex; i > targetIndex; i--) {
        this.applyDeltaBackward(this.deltas[i]);
      }
    }

    this.appliedDeltaIndex = targetIndex;
  }

  /**
   * Apply a delta moving forward in time.
   */
  private applyDeltaForward(delta: GraphDelta): void {
    this.cy.batch(() => {
      // Add created nodes
      delta.changes.nodesCreated.forEach(node => {
        this.cy.add({
          group: 'nodes',
          data: {
            id: node.id,
            label: node.labels[0],
            ...node.properties
          },
          classes: 'created'
        });
      });

      // Mark deleted nodes (keep in DOM with visibility flag)
      delta.changes.nodesDeleted.forEach(id => {
        const ele = this.cy.getElementById(id);
        if (ele.length) {
          ele.addClass('deleted');
          ele.data('visible', false);
        }
      });

      // Update modified nodes
      delta.changes.nodesModified.forEach(mod => {
        const ele = this.cy.getElementById(mod.id);
        if (ele.length) {
          ele.data(mod.properties);
          ele.addClass('modified');
        }
      });

      // Add created relationships
      delta.changes.relationshipsCreated.forEach(rel => {
        this.cy.add({
          group: 'edges',
          data: {
            id: rel.id,
            source: rel.sourceId,
            target: rel.targetId,
            type: rel.type
          },
          classes: 'created'
        });
      });

      // Mark deleted relationships
      delta.changes.relationshipsDeleted.forEach(id => {
        const ele = this.cy.getElementById(id);
        if (ele.length) {
          ele.addClass('deleted');
        }
      });
    });
  }

  /**
   * Reverse a delta (moving backward in time).
   */
  private applyDeltaBackward(delta: GraphDelta): void {
    this.cy.batch(() => {
      // Remove nodes that were created
      delta.changes.nodesCreated.forEach(node => {
        this.cy.getElementById(node.id).remove();
      });

      // Restore nodes that were deleted
      delta.changes.nodesDeleted.forEach(id => {
        const ele = this.cy.getElementById(id);
        if (ele.length) {
          ele.removeClass('deleted');
          ele.data('visible', true);
        }
      });

      // Revert modifications
      delta.changes.nodesModified.forEach(mod => {
        if (mod.previousProperties) {
          const ele = this.cy.getElementById(mod.id);
          if (ele.length) {
            ele.data(mod.previousProperties);
            ele.removeClass('modified');
          }
        }
      });

      // Remove relationships that were created
      delta.changes.relationshipsCreated.forEach(rel => {
        this.cy.getElementById(rel.id).remove();
      });

      // Restore deleted relationships
      delta.changes.relationshipsDeleted.forEach(id => {
        const ele = this.cy.getElementById(id);
        if (ele.length) {
          ele.removeClass('deleted');
        }
      });
    });
  }

  /**
   * Render a complete snapshot (used for fast seeking).
   */
  private renderFullSnapshot(snapshot: GraphSnapshot): void {
    this.cy.batch(() => {
      // Clear existing elements
      this.cy.elements().remove();

      // Add all nodes with pre-computed positions if available
      snapshot.nodes.forEach(node => {
        const position = snapshot.positions?.get(node.id);
        this.cy.add({
          group: 'nodes',
          data: {
            id: node.id,
            label: node.labels[0],
            ...node.properties,
            visible: node.visible
          },
          position: position,
          classes: node.visible ? '' : 'deleted'
        });
      });

      // Add all relationships
      snapshot.relationships.forEach(rel => {
        this.cy.add({
          group: 'edges',
          data: {
            id: rel.id,
            source: rel.sourceId,
            target: rel.targetId,
            type: rel.type
          },
          classes: rel.visible ? '' : 'deleted'
        });
      });
    });

    // Run layout only if no pre-computed positions
    if (!snapshot.positions) {
      this.cy.layout({
        name: 'fcose',
        randomize: false,
        animate: false
      }).run();
    }
  }

  private findNearestSnapshot(time: number): GraphSnapshot {
    let nearest = this.snapshots[0];
    for (const snapshot of this.snapshots) {
      if (snapshot.timestamp <= time) {
        nearest = snapshot;
      } else {
        break;
      }
    }
    return nearest;
  }

  private findDeltaIndexAt(time: number): number {
    for (let i = this.deltas.length - 1; i >= 0; i--) {
      if (this.deltas[i].timestamp <= time) {
        return i;
      }
    }
    return 0;
  }

  /**
   * Warm L1 cache with snapshots around current position.
   */
  private warmL1Cache(currentIndex: number): void {
    const CACHE_RADIUS = 5;  // ±5 snapshots
    const start = Math.max(0, currentIndex - CACHE_RADIUS);
    const end = Math.min(this.snapshots.length, currentIndex + CACHE_RADIUS);

    // Pre-compute states for nearby positions
    for (let i = start; i < end; i++) {
      this.l1Cache.set(i, this.snapshots[i]);
    }
  }
}
```

---

## Backend API

### Endpoints

```
GET /api/graph/changes/{job_id}
```

Returns parsed graph changes for a job.

**Response:**
```json
{
  "jobId": "abc-123",
  "timeRange": {
    "start": "2024-01-23T14:32:15Z",
    "end": "2024-01-23T19:47:22Z"
  },
  "summary": {
    "totalToolCalls": 3000,
    "graphToolCalls": 450,
    "nodesCreated": 120,
    "nodesDeleted": 500,
    "nodesModified": 85,
    "relationshipsCreated": 200,
    "relationshipsDeleted": 150
  },
  "snapshots": [...],
  "deltas": [...]
}
```

### Backend Implementation

```python
# cockpit/api/graph_routes.py

from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import re
import math

router = APIRouter(prefix="/api/graph", tags=["graph"])

@router.get("/changes/{job_id}")
async def get_graph_changes(job_id: str) -> Dict[str, Any]:
    """
    Get parsed graph changes for a job.

    Fetches tool calls from MongoDB, parses Cypher queries,
    and returns snapshots + deltas for timeline visualization.
    """
    # 1. Get audit trail from MongoDB
    mongo = get_mongodb()
    tool_calls = mongo.get_job_audit_trail(job_id, event_type="tool_call")

    # 2. Filter to graph operations
    graph_calls = [
        tc for tc in tool_calls
        if tc.get("tool_name") == "execute_cypher_query"
    ]

    if not graph_calls:
        return {
            "jobId": job_id,
            "summary": {"totalToolCalls": len(tool_calls), "graphToolCalls": 0},
            "snapshots": [],
            "deltas": []
        }

    # 3. Parse each Cypher query
    deltas = []
    for i, tc in enumerate(graph_calls):
        query = tc.get("inputs", {}).get("query", "")
        parsed = parse_cypher_query(query)

        deltas.append({
            "timestamp": tc["timestamp"].isoformat(),
            "toolCallIndex": i,
            "cypherQuery": query,
            "toolCallId": str(tc["_id"]),
            "changes": parsed
        })

    # 4. Build snapshots using √N interval
    n = len(deltas)
    interval = max(50, min(100, int(math.sqrt(n))))  # Clamp to 50-100
    snapshots = build_snapshots(deltas, interval=interval)

    # 5. Compute summary
    summary = compute_summary(deltas)

    return {
        "jobId": job_id,
        "timeRange": {
            "start": graph_calls[0]["timestamp"].isoformat(),
            "end": graph_calls[-1]["timestamp"].isoformat()
        },
        "summary": summary,
        "snapshots": snapshots,
        "deltas": deltas
    }


def parse_cypher_query(query: str) -> Dict[str, List]:
    """Parse a Cypher query and extract graph operations."""
    changes = {
        "nodesCreated": [],
        "nodesDeleted": [],
        "nodesModified": [],
        "relationshipsCreated": [],
        "relationshipsDeleted": []
    }

    # CREATE node: CREATE (var:Label {props})
    for match in re.finditer(
        r'CREATE\s+\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)',
        query,
        re.IGNORECASE
    ):
        changes["nodesCreated"].append({
            "variable": match.group(1),
            "label": match.group(2),
            "properties": parse_properties(match.group(3))
        })

    # DELETE: DELETE var or DETACH DELETE var
    for match in re.finditer(
        r'(DETACH\s+)?DELETE\s+(\w+)',
        query,
        re.IGNORECASE
    ):
        changes["nodesDeleted"].append({
            "variable": match.group(2),
            "detach": bool(match.group(1))
        })

    # CREATE relationship: CREATE (a)-[:TYPE]->(b)
    for match in re.finditer(
        r'CREATE\s+\((\w+)\)-\[:(\w+)\s*(?:\{([^}]*)\})?\]->\((\w+)\)',
        query,
        re.IGNORECASE
    ):
        changes["relationshipsCreated"].append({
            "sourceVar": match.group(1),
            "type": match.group(2),
            "properties": parse_properties(match.group(3)),
            "targetVar": match.group(4)
        })

    # SET: SET var.prop = value
    for match in re.finditer(
        r'SET\s+(\w+)\.(\w+)\s*=\s*([^\s,]+)',
        query,
        re.IGNORECASE
    ):
        changes["nodesModified"].append({
            "variable": match.group(1),
            "property": match.group(2),
            "value": match.group(3).strip("'\"")
        })

    return changes


def parse_properties(props_str: str) -> Dict[str, Any]:
    """Parse Neo4j property string: {key: 'value', key2: 123}"""
    if not props_str:
        return {}

    props = {}
    for match in re.finditer(r"(\w+)\s*:\s*['\"]?([^'\",$}]+)['\"]?", props_str):
        props[match.group(1)] = match.group(2)
    return props
```

---

## Cytoscape Styling

### Colorblind-Safe Okabe-Ito Palette

The traditional green/yellow/red scheme fails for ~8% of male users with red-green color blindness. Use the **Okabe-Ito palette** instead:

| State | Traditional | Colorblind-Safe | Secondary Encoding |
|-------|-------------|-----------------|-------------------|
| Created | Green #22C55E | Blue #0072B2 | Solid border |
| Modified | Yellow #EAB308 | Orange #E69F00 | Dashed border |
| Deleted | Red #EF4444 | Vermillion #D55E00 | 40% opacity + grayscale |

```typescript
const cytoscapeStyles: cytoscape.Stylesheet[] = [
  // Base node style
  {
    selector: 'node',
    style: {
      'label': 'data(label)',
      'background-color': '#6c7086',
      'color': '#cdd6f4',
      'text-valign': 'center',
      'text-halign': 'center',
      'font-size': '10px',
      'width': '40px',
      'height': '40px'
    }
  },

  // Node labels by type (domain colors)
  {
    selector: 'node[label="Requirement"]',
    style: { 'background-color': '#89b4fa' }  // Blue
  },
  {
    selector: 'node[label="BusinessObject"]',
    style: { 'background-color': '#a6e3a1' }  // Green
  },
  {
    selector: 'node[label="Message"]',
    style: { 'background-color': '#f9e2af' }  // Yellow
  },
  {
    selector: 'node[label="BusinessService"]',
    style: { 'background-color': '#cba6f7' }  // Purple
  },

  // Change states (Okabe-Ito colorblind-safe)
  {
    selector: 'node.created',
    style: {
      'border-width': '4px',
      'border-color': '#0072B2',  // Blue (colorblind-safe)
      'border-style': 'solid'
    }
  },
  {
    selector: 'node.modified',
    style: {
      'border-width': '4px',
      'border-color': '#E69F00',  // Orange (colorblind-safe)
      'border-style': 'dashed'
    }
  },
  {
    selector: 'node.deleted',
    style: {
      'border-width': '4px',
      'border-color': '#D55E00',  // Vermillion (colorblind-safe)
      'border-style': 'solid',
      'opacity': 0.4,
      'background-blacken': 0.3   // Grayscale shift
    }
  },

  // Edges
  {
    selector: 'edge',
    style: {
      'width': 2,
      'line-color': '#6c7086',
      'target-arrow-color': '#6c7086',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      'label': 'data(type)',
      'font-size': '8px',
      'text-rotation': 'autorotate'
    }
  },
  {
    selector: 'edge.created',
    style: {
      'line-color': '#0072B2',
      'target-arrow-color': '#0072B2',
      'width': 3
    }
  },
  {
    selector: 'edge.deleted',
    style: {
      'line-color': '#D55E00',
      'target-arrow-color': '#D55E00',
      'line-style': 'dashed',
      'opacity': 0.4
    }
  }
];
```

### Ghost Node Animation for Deletions

Research from EuroVis shows the optimal deletion visualization pattern:

```css
/* Ghost mode for deleted nodes */
.node-deleted {
  opacity: 0.4;
  filter: grayscale(50%);
  outline: 2px dashed #888;
}

/* Smooth appearance for created nodes */
.node-created {
  animation: node-appear 300ms ease-out;
}

@keyframes node-appear {
  from {
    transform: scale(0.5);
    opacity: 0;
  }
  to {
    transform: scale(1);
    opacity: 1;
  }
}

/* Pulse for modified nodes */
.node-modified {
  animation: node-pulse 200ms ease-out;
}

@keyframes node-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.1); }
}
```

---

## Accessibility

### Prefers-Reduced-Motion Support

```typescript
@Injectable({ providedIn: 'root' })
export class MotionPreferenceService {
  readonly prefersReducedMotion = signal(
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );

  constructor() {
    window.matchMedia('(prefers-reduced-motion: reduce)')
      .addEventListener('change', (e) => {
        this.prefersReducedMotion.set(e.matches);
      });
  }
}

// In component
readonly animationDuration = computed(() =>
  this.motionPref.prefersReducedMotion() ? 0 : 300
);
```

### Dark Mode Colors

Use dark gray backgrounds, not pure black, and off-white text:

```typescript
const darkTheme = {
  background: '#1E1E1E',        // Not pure black
  surface: '#2D2D2D',
  text: '#E0E0E0',              // Not pure white
  textMuted: '#888888',
  node: { 'background-color': '#66AADD', 'color': '#E0E0E0' },
  edge: { 'line-color': '#888888' }
};
```

---

## Performance Characteristics

### Performance Budget (16.7ms frame target)

| Component | Budget | Strategy |
|-----------|--------|----------|
| Layout computation | 0ms | Pre-computed or Web Worker |
| Delta application | 0ms | Web Worker |
| DOM/Canvas render | ≤10ms | Batch updates, dirty rectangles |
| Event handling | ≤3ms | Debounced handlers |
| Buffer | 3.7ms | Safety margin |

### Benchmarks (Expected)

| Operation | 3000 Tool Calls | Target |
|-----------|-----------------|--------|
| Initial load (fetch + parse) | 1-2 seconds | < 3s |
| Build snapshots (Web Worker) | 200-500ms | < 1s |
| Render 500 nodes (Canvas) | 100-200ms | < 500ms |
| Render 1200 nodes (WebGL) | 50-100ms | < 200ms |
| Fast scrub (jump to snapshot) | 50-100ms | < 200ms |
| Slow scrub (apply 1 delta) | 5-10ms | < 16ms (60fps) |
| Slow scrub (apply 10 deltas) | 15-30ms | < 50ms |

### Memory Usage

| Data Structure | Size |
|----------------|------|
| 30 snapshots × 500 nodes | ~1.5 MB |
| 3000 deltas | ~600 KB |
| L1 hot cache | ~500 KB |
| Cytoscape elements | ~2 MB |
| **Total** | **~4.6 MB** |

---

## Potential Pitfalls and Prevention

### 1. Layout Instability During Scrubbing

**Problem:** Force-directed layouts recalculate entirely when nodes change, causing the graph to "jump" unpredictably.

**Prevention:**
- Always use `randomize: false` with fCoSE
- Pre-compute layouts at snapshot points
- Interpolate positions between snapshots rather than running layout during scrubbing

### 2. Memory Leaks from Event Listeners

**Problem:** Cytoscape event handlers created in `ngOnInit` persist after component destruction.

**Prevention:**
```typescript
private cyListeners: Array<() => void> = [];

ngOnInit() {
  const handler = () => this.onPan();
  this.cy.on('pan', handler);
  this.cyListeners.push(() => this.cy.off('pan', handler));
}

ngOnDestroy() {
  this.cyListeners.forEach(cleanup => cleanup());
  this.cy?.destroy();
}
```

### 3. WebGL Context Limits

**Problem:** Browsers limit WebGL contexts (~8-16 per page). Multiple graph instances can exhaust this limit.

**Prevention:**
- Use a single shared Cytoscape instance
- Update data rather than creating/destroying instances
- Use Canvas for secondary/thumbnail views

### 4. Angular Change Detection Storms

**Problem:** Cytoscape events trigger hundreds of unnecessary Angular change detection cycles.

**Prevention:**
- Run Cytoscape outside NgZone
- Use `ChangeDetectionStrategy.OnPush`
- Re-enter zone only for UI updates

### 5. Timeline Scrubbing Performance Degradation

**Problem:** Rapid scrubbing overwhelms the render queue, causing frame drops.

**Prevention:**
- Debounce timeline input with `requestAnimationFrame`
- Use velocity detection to switch between snapshot/delta rendering
- Keep all computation under 16.7ms per frame

---

## UX Patterns

### Timeline with Sparkline Background

Following Gephi's pattern, show activity density in the timeline background:

```typescript
interface TimelineConfig {
  sparklineData: number[];  // Node/edge counts per time slice
  markers: TimelineMarker[];  // Phase transitions, large operations
  intervalSelector: boolean;  // Allow selecting a range
  playbackSpeed: number;  // 0.1x to 10x
}
```

### Minimap / Navigator

Use Cytoscape's Navigator extension with simplified rendering:

```typescript
// Initialize navigator with performance options
const nav = this.cy.navigator({
  container: this.minimapContainer.nativeElement,
  viewLiveFramerate: 0,  // Disable live updates during pan
  thumbnailEventFramerate: 30,
  thumbnailLiveFramerate: false,
  dblClickDelay: 200
});
```

### Node Inspector Panel

Click a node to see full properties in a side panel, linked to the Agent Activity panel.

---

## Integration with Cockpit

### Component Registration

```typescript
// Add to component registry
{
  type: 'graph-timeline' as ComponentType,
  displayName: 'Graph Timeline',
  icon: '📊',
  component: () => import('./components/graph-timeline/graph-timeline.component')
}
```

### Timeline Integration

The existing timeline slider component should emit:
- `timeChange(timestamp: number)` - Current position
- `velocityChange(velocity: number)` - Scrub speed (for adaptive rendering)

The GraphTimelineComponent subscribes to these events.

### Panel Linking

When user clicks a tool call in the Agent Activity panel:
1. Agent Activity emits `toolCallSelected(toolCallId)`
2. Graph Timeline receives event
3. Seeks to that tool call's timestamp
4. Highlights the affected nodes

---

## Future Enhancements

### Phase 1 (MVP)
- [x] Document architecture
- [ ] Backend API for graph changes
- [ ] Cypher parser (basic patterns)
- [ ] GraphTimelineComponent with Cytoscape
- [ ] Snapshot/delta rendering
- [ ] Timeline slider integration

### Phase 2 (Polish)
- [ ] Web Worker for parsing and state computation
- [ ] Three-tier caching (L1/L2/L3)
- [ ] fCoSE layout with stability optimizations
- [ ] Colorblind-safe palette
- [ ] Minimap/Navigator
- [ ] Node inspector panel
- [ ] Keyboard shortcuts

### Phase 3 (Advanced)
- [ ] Live mode (watch graph changes in real-time during job)
- [ ] Diff view (compare two points in time)
- [ ] Search/filter nodes
- [ ] Timeline sparkline with activity density
- [ ] Export graph as image
- [ ] Integration with Neo4j Browser

---

## References

- [Cytoscape.js Documentation](https://js.cytoscape.org/)
- [cytoscape-angular on npm](https://www.npmjs.com/package/cytoscape-angular)
- [fCoSE Layout Algorithm](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose)
- [Neo4j Cypher Manual](https://neo4j.com/docs/cypher-manual/current/)
- [Okabe-Ito Colorblind-Safe Palette](https://jfly.uni-koeln.de/color/)
- [AeonG: Temporal Graph Database (VLDB 2024)](https://www.vldb.org/pvldb/vol17/p1-aeong.pdf)
- [Git Packfile Format](https://git-scm.com/book/en/v2/Git-Internals-Packfiles)
