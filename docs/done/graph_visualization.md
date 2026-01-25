# Building a graph change timeline in Angular 21

**Cytoscape.js with the cytoscape-angular wrapper emerges as the clear choice for your Neo4j debug cockpit**, offering native Angular 21 signals support, a new WebGL renderer (January 2025) that handles 1,200+ nodes at 100+ FPS, and proven incremental layout algorithms. The key to achieving smooth 60fps scrubbing lies in combining an anchor+delta snapshot architecture with fCoSE layouts that preserve layout stability, Canvas rendering with Web Workers for computation, and a three-tier caching strategy borrowed from video editing systems.

---

## Library recommendation: Cytoscape.js wins for Angular 21

The 2025/2026 landscape has clarified significantly. Cytoscape.js v3.31 introduced WebGL rendering in January 2025, closing the performance gap with WebGL-native alternatives while maintaining its excellent ecosystem and **the only production-ready Angular wrapper with signals support**.

### Why Cytoscape.js + cytoscape-angular

The `cytoscape-angular` package by michaelbushe provides what no other library offers: full Angular 20+ integration with signals, standalone components, model signals for two-way binding, and `takeUntilDestroyed` lifecycle patterns. This eliminates the custom wrapper development burden that Sigma.js or G6 would require.

**Performance at your scale (500-2000 nodes):** WebGL mode tested at 1,200 nodes with 16,000 edges delivers 100+ FPS versus 20 FPS with Canvas. Your 500-2000 node target sits comfortably within this envelope with headroom for growth.

**Alternatives considered:**
- **Sigma.js v3** offers pure WebGL performance but requires building a custom Angular wrapper and lacks Cytoscape's rich algorithm library
- **G6 (AntV)** has excellent built-in animation but focuses on the React ecosystem with no Angular wrapper
- **Cosmos.gl** (joined OpenJS Foundation May 2025) handles millions of nodes via GPU-accelerated layouts—overkill for your scale but worth monitoring for future scaling needs

### Rendering backend decision matrix

| Scale | Recommended Renderer | Rationale |
|-------|---------------------|-----------|
| <500 nodes | Canvas | Simpler debugging, good performance |
| 500-2000 nodes | Canvas or WebGL | Canvas sufficient; WebGL if animations heavy |
| 2000+ nodes | WebGL required | Canvas drops below 30fps |

For your use case, **start with Canvas and switch to WebGL if scrubbing performance degrades**—the Cytoscape API remains identical.

---

## Architecture: anchor+delta with adaptive intervals

The snapshot/delta hybrid approach aligns with proven patterns from temporal graph databases (AeonG, VLDB 2024), Git's packfile design, and video editing systems. The key insight: **snapshot at √N operation intervals** to achieve O(√N) reconstruction time for any state.

### Optimal snapshot interval calculation

For 3000 operations over a 5-hour job: **√3000 ≈ 55 operations**. Round up to **100 operations per snapshot** for simpler implementation, yielding ~30 anchor snapshots. This creates a maximum delta chain of 100 operations to replay—well within the 16.7ms frame budget when processed in a Web Worker.

### Three-tier caching strategy for smooth scrubbing

```
L1: Hot Window Cache (in-memory)
├── Current position ± 50 operations
├── Fully materialized graph states
└── Size: ~10 snapshots

L2: Anchor Cache (IndexedDB)
├── All snapshot anchors (every 100 ops)
├── ~30 anchors for 3000 ops
└── LRU eviction for inactive regions

L3: Full Event Store (FastAPI backend)
├── Complete delta log
├── All anchors (compressed)
└── MongoDB for audit trail persistence
```

### Delta format for graph operations

Store deltas as compact operation records rather than full state diffs:

```typescript
interface GraphDelta {
  operationIndex: number;
  timestamp: number;
  type: 'ADD_NODE' | 'REMOVE_NODE' | 'MODIFY_NODE' | 
        'ADD_EDGE' | 'REMOVE_EDGE' | 'MODIFY_EDGE';
  entityId: string;
  properties?: Record<string, unknown>;  // Only changed fields
  previousProperties?: Record<string, unknown>;  // For undo
}
```

**Critical insight from Git's packfile design:** Store recent states as full copies and older states as deltas—this optimizes for the common case of viewing recent history while maintaining efficient storage for historical data. Limit delta chain depth to **50** (Git's proven default) to bound reconstruction time.

---

## Performance techniques for 60fps scrubbing

Achieving smooth timeline scrubbing requires keeping all computation under **16.7ms per frame**. Here's the performance budget breakdown:

| Component | Budget | Strategy |
|-----------|--------|----------|
| Layout computation | 0ms | Pre-computed or Web Worker |
| Delta application | 0ms | Web Worker |
| DOM/Canvas render | ≤10ms | Batch updates, dirty rectangles |
| Event handling | ≤3ms | Debounced handlers |
| Buffer | 3.7ms | Safety margin |

### fCoSE layout for stability when nodes appear/disappear

The **fCoSE (Fast Compound Spring Embedder)** algorithm with `randomize: false` is the critical technique for preventing layout "jumping" when nodes are incrementally added or removed:

```typescript
cy.layout({
  name: 'fcose',
  randomize: false,     // Use current positions as starting point
  quality: 'proof',     // Slower cooling = more stability
  animate: true,
  animationDuration: 300,
  nodeRepulsion: 4500,
  idealEdgeLength: 50
}).run();
```

**Additional stability techniques:**
- **Pinning weights**: Assign higher stability weights to older nodes; new nodes find positions around fixed neighbors
- **Initial placement heuristics**: Use `cytoscape-layout-utilities` extension to place new nodes near their connected neighbors before running layout
- **Energy-based refinement**: Run continuous low-intensity layout in background to gradually reduce overlaps without sudden movements

### Web Worker architecture for computation

Offload all heavy computation to keep the main thread free for rendering:

```typescript
// Main thread: rendering only
const worker = new Worker('graph-worker.js');

worker.postMessage({ 
  type: 'COMPUTE_STATE',
  anchorSnapshot: nearestAnchor,
  deltas: deltasBetweenAnchorAndTarget,
  targetIndex: timelinePosition
});

worker.onmessage = (e) => {
  cy.batch(() => {
    // Apply computed state in single batch
    e.data.nodes.forEach(n => {
      cy.$(`#${n.id}`).position(n.position);
      cy.$(`#${n.id}`).data(n.data);
    });
  });
};
```

**Use Transferable Objects** (ArrayBuffers) for large datasets to avoid copy overhead when passing data between main thread and worker.

### Batch updates to minimize redraws

Cytoscape's `batch()` method is essential—it aggregates all updates into a single redraw:

```typescript
cy.batch(() => {
  // All operations here trigger ONE redraw
  newNodes.forEach(n => cy.add(n));
  removedNodes.forEach(id => cy.$(`#${id}`).remove());
  modifiedNodes.forEach(n => cy.$(`#${n.id}`).data(n.data));
});
```

---

## UX patterns from professional tools

### Timeline scrubbing interface

Gephi's dynamic network visualization provides the clearest model: a **timeline bar with a sparkline background** showing activity density (node/edge counts per time slice), a **draggable interval selector**, and **play/pause controls** with adjustable speed (0.1x to 10x).

**Hover preview pattern from video editing:** Display thumbnail graph previews when hovering over the timeline before committing to that position—Mux's timeline hover previews documentation shows this technique.

### Deleted node visualization

Research from EuroVis and professional tools reveals a consistent pattern for showing deleted elements:

1. **Ghost mode first**: Reduce opacity to 40%, add dashed border, shift color toward gray
2. **Shrink animation**: Scale node to 80% over 200ms
3. **Fade out**: Animate opacity to 0 over 300ms
4. **Remove from DOM**: After animation completes

**For scrubbing backward through time**: Reverse the animation—fade in, scale up, restore colors. Keep deleted nodes in your data structure with a `visible: false` flag rather than actually removing them, enabling instant restoration during timeline scrubbing.

```css
.node-deleted {
  opacity: 0.4;
  filter: grayscale(50%);
  outline: 2px dashed #888;
}

.node-created {
  animation: node-appear 300ms ease-out;
}

@keyframes node-appear {
  from { transform: scale(0.5); opacity: 0; }
  to { transform: scale(1); opacity: 1; }
}
```

### Minimap and overview

All professional tools (Neo4j Bloom, yFiles, Gephi) include a minimap component. Cytoscape.js provides the **Navigator extension** for this purpose. Key optimization: use **simplified rendering** in the minimap—circles only, no labels—to maintain performance even with large graphs.

### Highlighting important changes

Grafana's Node Graph panel demonstrates effective change highlighting: **arc borders** proportional to change magnitude (e.g., a red arc spanning 25% of the node border indicates that node was involved in 25% of operations in the current view). For your debug cockpit, consider:

- **Border thickness** proportional to number of modifications
- **Pulsing animation** for nodes changed in the last 5 seconds
- **Size scaling** based on centrality (use Cytoscape's built-in PageRank algorithm)

---

## Angular 21 integration patterns

### Running Cytoscape outside Angular's zone

This is the most critical performance optimization for Angular. Graph animations fire hundreds of events that trigger unnecessary change detection:

```typescript
@Component({
  selector: 'app-graph-timeline',
  standalone: true,
  imports: [CytoscapeGraphComponent],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class GraphTimelineComponent {
  private ngZone = inject(NgZone);
  private cy: cytoscape.Core;

  ngOnInit() {
    // Initialize OUTSIDE Angular zone
    this.ngZone.runOutsideAngular(() => {
      this.cy = cytoscape({
        container: this.graphContainer.nativeElement,
        // ... config
      });

      // All event handlers stay outside zone
      this.cy.on('pan zoom', () => this.handleViewportChange());
    });
  }

  // Re-enter zone ONLY when updating Angular UI
  onNodeSelected(node: NodeSingular) {
    this.ngZone.run(() => {
      this.selectedNode.set(node.data());
    });
  }
}
```

### Signal-based reactive state with cytoscape-angular

```typescript
@Component({
  standalone: true,
  imports: [CytoscapeGraphComponent]
})
export class DebugCockpitComponent {
  // Reactive signals for graph data
  readonly nodes = signal<NodeDefinition[]>([]);
  readonly edges = signal<EdgeDefinition[]>([]);
  readonly timelinePosition = signal<number>(0);
  
  // Computed graph state based on timeline position
  readonly graphState = computed(() => 
    this.computeStateAtPosition(this.timelinePosition())
  );
  
  // Effect to update Cytoscape when state changes
  constructor() {
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

---

## Accessibility and polish requirements

### Colorblind-safe palette for change states

The default green/yellow/red color scheme fails for ~8% of male users with red-green color blindness. Use the **Okabe-Ito palette** instead:

| State | Traditional | Colorblind-Safe Alternative |
|-------|-------------|----------------------------|
| Created | Green #22C55E | Blue #0072B2 |
| Modified | Yellow #EAB308 | Orange #E69F00 |
| Deleted | Red #EF4444 | Vermillion #D55E00 |

**Always pair color with a secondary encoding**: shape (circles for created, diamonds for modified, X marks for deleted), icon badges, or pattern fills.

### Prefers-reduced-motion support

```typescript
@Injectable({ providedIn: 'root' })
export class MotionPreferenceService {
  readonly prefersReducedMotion = signal(
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

// In component
readonly animationDuration = computed(() => 
  this.motionPref.prefersReducedMotion() ? 0 : 300
);
```

### Dark mode implementation

Create dedicated dark palettes rather than inverting colors. Use dark gray backgrounds (#1E1E1E to #2D2D2D), not pure black, and off-white text (#E0E0E0), not pure white:

```typescript
const darkTheme = {
  node: { 'background-color': '#66AADD', 'label-color': '#E0E0E0' },
  edge: { 'line-color': '#888888' },
  background: '#1E1E1E'
};
```

---

## Potential pitfalls and prevention strategies

### Layout instability during scrubbing

**Problem:** Force-directed layouts recalculate entirely when nodes change, causing the graph to "jump" unpredictably.

**Prevention:** Always use `randomize: false` with fCoSE. Pre-compute layouts at snapshot points and interpolate positions between snapshots rather than running layout during scrubbing.

### Memory leaks from event listeners

**Problem:** Cytoscape event handlers created in `ngOnInit` persist after component destruction.

**Prevention:** Store subscription references and clean up in `ngOnDestroy`:

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

### Timeline scrubbing performance degradation

**Problem:** Rapid scrubbing overwhelms the render queue, causing frame drops.

**Prevention:** Debounce timeline input to 60fps maximum (16.7ms intervals). Use `requestAnimationFrame` gating:

```typescript
private pendingPosition: number | null = null;

onTimelineChange(position: number) {
  this.pendingPosition = position;
  if (!this.rafId) {
    this.rafId = requestAnimationFrame(() => {
      this.updateGraph(this.pendingPosition!);
      this.pendingPosition = null;
      this.rafId = null;
    });
  }
}
```

### WebGL context limits

**Problem:** Browsers limit WebGL contexts (~8-16 per page). Multiple graph instances can exhaust this limit.

**Prevention:** Use a single shared Cytoscape instance, updating its data rather than creating/destroying instances. For multiple views, consider Canvas rendering for secondary displays.

---

## Implementation roadmap

1. **Week 1-2:** Set up cytoscape-angular with Angular 21 signals, implement basic graph rendering with Canvas, establish Zone.js optimization patterns

2. **Week 3-4:** Implement anchor+delta storage in MongoDB, create FastAPI endpoints for timeline queries, build three-tier caching layer

3. **Week 5-6:** Add fCoSE layout with stability optimizations, implement Web Worker computation, build timeline scrubbing UI with Gephi-inspired sparkline

4. **Week 7-8:** Implement change state animations (ghost nodes, fade effects), add minimap/overview, apply colorblind-safe palette and dark mode

5. **Week 9+:** Performance testing at scale, accessibility audit, edge case handling for 5+ hour job histories

The combination of Cytoscape.js's proven ecosystem, the modern cytoscape-angular wrapper, anchor+delta architecture, and fCoSE layout stability creates a solid foundation for visualizing your 3000+ operation knowledge graph modifications with smooth 60fps scrubbing.