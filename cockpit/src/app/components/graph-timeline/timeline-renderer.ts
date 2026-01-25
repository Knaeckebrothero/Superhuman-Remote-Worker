/**
 * Timeline renderer for Cytoscape graph.
 * Handles snapshot/delta rendering with velocity-based strategy selection.
 */

import type { Core, ElementDefinition, NodeSingular, EdgeSingular } from 'cytoscape';
import type {
  GraphSnapshot,
  GraphDelta,
  NodeState,
  RelationshipState,
  ChangeState,
} from '../../core/models/graph.model';
import { fcoseLayoutOptions, gridLayoutOptions } from './graph-styles';

/** Velocity threshold for switching to snapshot rendering (ops/second) */
const FAST_SEEK_THRESHOLD = 500;

/** L1 cache size (number of snapshots to keep in memory) */
const L1_CACHE_SIZE = 10;

/**
 * Manages timeline-based graph rendering with snapshot/delta optimization.
 */
export class TimelineRenderer {
  private cy: Core;
  private snapshots: GraphSnapshot[] = [];
  private deltas: GraphDelta[] = [];

  // Current state tracking
  private currentSnapshotIndex = 0;
  private appliedDeltaIndex = -1;

  // Velocity tracking
  private lastSeekTime = 0;
  private lastSeekIndex = 0;

  // L1 cache for materialized states
  private l1Cache = new Map<number, Map<string, NodeState>>();

  // Position cache for layout stability
  private positionCache = new Map<string, { x: number; y: number }>();

  // Persistent variable to ID mapping (across all deltas)
  private varToIdPersistent = new Map<string, string>();

  // Layout running flag
  private layoutRunning = false;

  constructor(cy: Core) {
    this.cy = cy;
  }

  /**
   * Load snapshots and deltas for a new job.
   * Starts at the last snapshot to show the final graph state.
   */
  load(snapshots: GraphSnapshot[], deltas: GraphDelta[]): void {
    this.snapshots = snapshots;
    this.deltas = deltas;
    this.currentSnapshotIndex = 0;
    this.appliedDeltaIndex = -1;
    this.l1Cache.clear();
    this.positionCache.clear();
    this.varToIdPersistent.clear();

    console.log('[TimelineRenderer] Loading:', {
      snapshots: snapshots.length,
      deltas: deltas.length,
    });

    if (snapshots.length === 0 || deltas.length === 0) {
      console.log('[TimelineRenderer] No data to render');
      return;
    }

    // Find the last snapshot with actual nodes (not empty)
    let targetSnapshot = snapshots[snapshots.length - 1];
    for (let i = snapshots.length - 1; i >= 0; i--) {
      if (Object.keys(snapshots[i].nodes).length > 0) {
        targetSnapshot = snapshots[i];
        break;
      }
    }

    console.log('[TimelineRenderer] Rendering snapshot at index:', targetSnapshot.toolCallIndex,
      'with nodes:', Object.keys(targetSnapshot.nodes).length);

    // Render the snapshot with data
    this.renderFullSnapshot(targetSnapshot);
    this.currentSnapshotIndex = this.snapshots.indexOf(targetSnapshot);
    this.appliedDeltaIndex = targetSnapshot.toolCallIndex;

    // Warm L1 cache
    this.warmL1Cache(this.appliedDeltaIndex);
  }

  /**
   * Seek to a specific tool call index.
   * Uses velocity-based strategy selection.
   */
  seekTo(index: number): void {
    if (this.deltas.length === 0) return;

    const clampedIndex = Math.max(0, Math.min(index, this.deltas.length - 1));

    // Calculate velocity
    const now = performance.now();
    const elapsed = now - this.lastSeekTime;
    const distance = Math.abs(clampedIndex - this.lastSeekIndex);
    const velocity = elapsed > 0 ? (distance / elapsed) * 1000 : 0;

    this.lastSeekTime = now;
    this.lastSeekIndex = clampedIndex;

    if (velocity > FAST_SEEK_THRESHOLD) {
      // Fast scrub: jump to nearest snapshot
      this.jumpToNearestSnapshot(clampedIndex);
    } else {
      // Slow scrub: apply deltas incrementally
      this.applyDeltasTo(clampedIndex);
    }
  }

  /**
   * Get current rendered index.
   */
  getCurrentIndex(): number {
    return this.appliedDeltaIndex;
  }

  /**
   * Jump to nearest snapshot for fast seeking.
   */
  private jumpToNearestSnapshot(targetIndex: number): void {
    const snapshot = this.findNearestSnapshot(targetIndex);
    if (!snapshot) return;

    this.renderFullSnapshot(snapshot);
    this.currentSnapshotIndex = this.snapshots.indexOf(snapshot);
    this.appliedDeltaIndex = snapshot.toolCallIndex;

    // Apply remaining deltas to reach target
    if (targetIndex > this.appliedDeltaIndex) {
      for (let i = this.appliedDeltaIndex + 1; i <= targetIndex; i++) {
        this.applyDeltaForward(this.deltas[i], i);
      }
      this.appliedDeltaIndex = targetIndex;
    }

    // Warm cache around new position
    this.warmL1Cache(this.appliedDeltaIndex);
  }

  /**
   * Apply deltas incrementally for slow seeking.
   */
  private applyDeltasTo(targetIndex: number): void {
    if (targetIndex === this.appliedDeltaIndex) return;

    this.cy.batch(() => {
      if (targetIndex > this.appliedDeltaIndex) {
        // Moving forward
        for (let i = this.appliedDeltaIndex + 1; i <= targetIndex; i++) {
          this.applyDeltaForward(this.deltas[i], i);
        }
      } else {
        // Moving backward
        for (let i = this.appliedDeltaIndex; i > targetIndex; i--) {
          this.applyDeltaBackward(this.deltas[i]);
        }
      }
    });

    this.appliedDeltaIndex = targetIndex;
  }

  /**
   * Apply a delta moving forward in time.
   */
  private applyDeltaForward(delta: GraphDelta, toolIndex: number): void {
    const changes = delta.changes;

    // Clear previous change states
    this.cy.elements().removeClass('created modified deleted');

    // First, process matched variables to resolve references from MATCH clauses
    if (changes.matchedVariables) {
      for (const matched of changes.matchedVariables) {
        const matchedId = this.getNodeId(matched);
        this.varToIdPersistent.set(matched.variable, matchedId);
      }
    }

    // Add created nodes
    for (const node of changes.nodesCreated) {
      const nodeId = this.getNodeId(node);
      this.varToIdPersistent.set(node.variable, nodeId);

      const existingNode = this.cy.getElementById(nodeId);
      if (existingNode.length === 0 || !node.merge) {
        const position = this.positionCache.get(nodeId) ?? this.getNewNodePosition();

        this.cy.add({
          group: 'nodes',
          data: {
            id: nodeId,
            label: node.label,
            displayLabel: this.getDisplayLabel(nodeId, node.properties),
            ...node.properties,
            createdAt: toolIndex,
            modifiedAt: toolIndex,
          },
          position,
          classes: 'created',
        });
      }
    }

    // Mark deleted nodes
    for (const deletion of changes.nodesDeleted) {
      const nodeId = this.varToIdPersistent.get(deletion.variable) ?? deletion.variable;

      // Find matching nodes
      this.cy.nodes().forEach((ele: NodeSingular) => {
        const id = ele.id();
        if (id === nodeId || id.includes(deletion.variable)) {
          ele.addClass('deleted');
          ele.data('visible', false);
          ele.data('deletedAt', toolIndex);
        }
      });
    }

    // Update modified nodes
    for (const mod of changes.nodesModified) {
      const nodeId = this.varToIdPersistent.get(mod.variable) ?? mod.variable;

      this.cy.nodes().forEach((ele: NodeSingular) => {
        const id = ele.id();
        if (id === nodeId || id.includes(mod.variable)) {
          if (mod.removed && mod.property) {
            ele.removeData(mod.property);
          } else if (mod.property !== undefined && mod.value !== undefined) {
            ele.data(mod.property, mod.value);
          }
          ele.data('modifiedAt', toolIndex);
          ele.addClass('modified');
        }
      });
    }

    // Add created relationships
    for (const rel of changes.relationshipsCreated) {
      const sourceId = this.varToIdPersistent.get(rel.sourceVar) ?? rel.sourceVar;
      const targetId = this.varToIdPersistent.get(rel.targetVar) ?? rel.targetVar;
      const relId = `${sourceId}-${rel.type}-${targetId}`;

      // Verify source and target nodes exist before adding edge
      const sourceNode = this.cy.getElementById(sourceId);
      const targetNode = this.cy.getElementById(targetId);

      if (sourceNode.length === 0 || targetNode.length === 0) {
        console.log('[TimelineRenderer] Skipping edge - missing node:', {
          relId,
          sourceId,
          sourceExists: sourceNode.length > 0,
          targetId,
          targetExists: targetNode.length > 0,
        });
        continue;
      }

      const existingEdge = this.cy.getElementById(relId);
      if (existingEdge.length === 0 || !rel.merge) {
        this.cy.add({
          group: 'edges',
          data: {
            id: relId,
            source: sourceId,
            target: targetId,
            type: rel.type,
            ...rel.properties,
            createdAt: toolIndex,
          },
          classes: 'created',
        });
      }
    }

    // Mark deleted relationships
    for (const rel of changes.relationshipsDeleted) {
      this.cy.edges().forEach((ele: EdgeSingular) => {
        if (ele.id().includes(rel.variable)) {
          ele.addClass('deleted');
          ele.data('deletedAt', toolIndex);
        }
      });
    }
  }

  /**
   * Reverse a delta (moving backward in time).
   */
  private applyDeltaBackward(delta: GraphDelta): void {
    const changes = delta.changes;

    // Clear change states
    this.cy.elements().removeClass('created modified deleted');

    // Remove nodes that were created
    for (const node of changes.nodesCreated) {
      const nodeId = this.getNodeId(node);
      const ele = this.cy.getElementById(nodeId);
      if (ele.length > 0) {
        // Save position before removing
        const pos = ele.position();
        this.positionCache.set(nodeId, { x: pos.x, y: pos.y });
        ele.remove();
      }
    }

    // Restore nodes that were deleted
    for (const deletion of changes.nodesDeleted) {
      this.cy.nodes().forEach((ele: NodeSingular) => {
        const id = ele.id();
        if (id.includes(deletion.variable)) {
          ele.removeClass('deleted');
          ele.data('visible', true);
          ele.removeData('deletedAt');
        }
      });
    }

    // Revert modifications (note: we don't have previousProperties in deltas)
    for (const mod of changes.nodesModified) {
      this.cy.nodes().forEach((ele: NodeSingular) => {
        if (ele.id().includes(mod.variable)) {
          ele.removeClass('modified');
        }
      });
    }

    // Remove relationships that were created
    for (const rel of changes.relationshipsCreated) {
      const sourceId = this.varToIdPersistent.get(rel.sourceVar) ?? rel.sourceVar;
      const targetId = this.varToIdPersistent.get(rel.targetVar) ?? rel.targetVar;
      const relId = `${sourceId}-${rel.type}-${targetId}`;
      this.cy.getElementById(relId).remove();
    }

    // Restore deleted relationships
    for (const rel of changes.relationshipsDeleted) {
      this.cy.edges().forEach((ele: EdgeSingular) => {
        if (ele.id().includes(rel.variable)) {
          ele.removeClass('deleted');
          ele.removeData('deletedAt');
        }
      });
    }
  }

  /**
   * Render a complete snapshot (replaces all elements).
   */
  private renderFullSnapshot(snapshot: GraphSnapshot): void {
    console.log('[TimelineRenderer] renderFullSnapshot:', {
      nodeCount: Object.keys(snapshot.nodes).length,
      relCount: Object.keys(snapshot.relationships).length,
      nodes: Object.keys(snapshot.nodes),
    });

    // Rebuild variable-to-ID mapping by processing all deltas up to this snapshot
    this.rebuildVarToIdMapping(snapshot.toolCallIndex);

    this.cy.batch(() => {
      // Clear existing elements
      this.cy.elements().remove();

      // Add all nodes
      const elements: ElementDefinition[] = [];

      for (const [id, node] of Object.entries(snapshot.nodes)) {
        if (!node.visible) continue;

        const position = snapshot.positions?.[id] ?? this.positionCache.get(id) ?? this.getNewNodePosition();

        console.log('[TimelineRenderer] Adding node:', id, 'at position:', position);

        elements.push({
          group: 'nodes',
          data: {
            id: node.id,
            label: node.labels[0],
            displayLabel: this.getDisplayLabel(node.id, node.properties),
            ...node.properties,
            createdAt: node.createdAt,
            modifiedAt: node.modifiedAt,
            visible: true,
          },
          position,
        });
      }

      // Add all relationships
      for (const [id, rel] of Object.entries(snapshot.relationships)) {
        if (!rel.visible) continue;

        elements.push({
          group: 'edges',
          data: {
            id: rel.id,
            source: rel.sourceId,
            target: rel.targetId,
            type: rel.type,
            ...rel.properties,
            createdAt: rel.createdAt,
          },
        });
      }

      console.log('[TimelineRenderer] Adding elements:', elements.length);
      this.cy.add(elements);
    });

    console.log('[TimelineRenderer] After add, cy.nodes():', this.cy.nodes().length);

    // Run layout if no positions
    const hasPositions = Object.keys(snapshot.positions ?? {}).length > 0;
    if (!hasPositions && !this.layoutRunning && this.cy.nodes().length > 0) {
      console.log('[TimelineRenderer] Running layout...');
      this.runLayout(false); // Don't animate on initial load
    }

    // Fit to view
    if (this.cy.nodes().length > 0) {
      this.cy.fit(undefined, 50);
    }
  }

  /**
   * Run layout algorithm.
   */
  runLayout(animate = true): void {
    if (this.layoutRunning) return;
    this.layoutRunning = true;

    const nodeCount = this.cy.nodes().length;
    const options = nodeCount > 100 ? gridLayoutOptions : fcoseLayoutOptions;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const layout = this.cy.layout({
      ...options,
      animate,
      stop: () => {
        this.layoutRunning = false;
        this.cachePositions();
      },
    } as any);

    layout.run();
  }

  /**
   * Cache current node positions.
   */
  private cachePositions(): void {
    this.cy.nodes().forEach((node: NodeSingular) => {
      const pos = node.position();
      this.positionCache.set(node.id(), { x: pos.x, y: pos.y });
    });
  }

  /**
   * Find nearest snapshot at or before target index.
   */
  private findNearestSnapshot(targetIndex: number): GraphSnapshot | null {
    let nearest: GraphSnapshot | null = null;

    for (const snapshot of this.snapshots) {
      if (snapshot.toolCallIndex <= targetIndex) {
        nearest = snapshot;
      } else {
        break;
      }
    }

    return nearest;
  }

  /**
   * Warm L1 cache around current position.
   */
  private warmL1Cache(currentIndex: number): void {
    // This is a simplified version - in production, you'd materialize
    // graph states for nearby indices
    const cacheRadius = Math.floor(L1_CACHE_SIZE / 2);
    const start = Math.max(0, currentIndex - cacheRadius);
    const end = Math.min(this.snapshots.length, currentIndex + cacheRadius);

    // Evict old entries
    if (this.l1Cache.size > L1_CACHE_SIZE) {
      const entries = Array.from(this.l1Cache.keys());
      for (let i = 0; i < entries.length - L1_CACHE_SIZE; i++) {
        this.l1Cache.delete(entries[i]);
      }
    }
  }

  /**
   * Rebuild variable-to-ID mapping from deltas up to given index.
   * This is needed when jumping to a snapshot to ensure variable references can be resolved.
   */
  private rebuildVarToIdMapping(upToIndex: number): void {
    this.varToIdPersistent.clear();

    for (let i = 0; i <= upToIndex && i < this.deltas.length; i++) {
      const delta = this.deltas[i];
      const changes = delta.changes;

      // Process matched variables
      if (changes.matchedVariables) {
        for (const matched of changes.matchedVariables) {
          const matchedId = this.getNodeId(matched);
          this.varToIdPersistent.set(matched.variable, matchedId);
        }
      }

      // Process created nodes
      for (const node of changes.nodesCreated) {
        const nodeId = this.getNodeId(node);
        this.varToIdPersistent.set(node.variable, nodeId);
      }
    }

    console.log('[TimelineRenderer] Rebuilt varToId mapping up to index', upToIndex,
      'with', this.varToIdPersistent.size, 'entries');
  }

  /**
   * Get node ID from creation data.
   */
  private getNodeId(node: { variable: string; label: string; properties: Record<string, unknown> }): string {
    const props = node.properties;

    // Try common ID properties (domain-specific IDs first, then generic)
    for (const idProp of ['rid', 'boid', 'mid', 'sid', 'id', 'uuid', 'ID', 'name', 'title', 'key']) {
      if (idProp in props && props[idProp]) {
        return String(props[idProp]);
      }
    }

    return `${node.label}_${node.variable}`;
  }

  /**
   * Get display label for a node.
   */
  private getDisplayLabel(id: string, properties: Record<string, unknown>): string {
    // Try common display properties
    for (const prop of ['name', 'title', 'label', 'id']) {
      if (prop in properties && properties[prop]) {
        const value = String(properties[prop]);
        return value.length > 20 ? value.substring(0, 17) + '...' : value;
      }
    }

    // Fallback to ID
    return id.length > 20 ? id.substring(0, 17) + '...' : id;
  }

  /**
   * Get position for a new node (tries to place near connected nodes).
   */
  private getNewNodePosition(): { x: number; y: number } {
    // Simple random placement near center
    const width = this.cy.width();
    const height = this.cy.height();

    return {
      x: width / 2 + (Math.random() - 0.5) * width * 0.5,
      y: height / 2 + (Math.random() - 0.5) * height * 0.5,
    };
  }

  /**
   * Fit graph to viewport.
   */
  fit(): void {
    this.cy.fit(undefined, 50);
  }

  /**
   * Center graph.
   */
  center(): void {
    this.cy.center();
  }

  /**
   * Reset zoom to 1.
   */
  resetZoom(): void {
    this.cy.zoom(1);
    this.cy.center();
  }
}
