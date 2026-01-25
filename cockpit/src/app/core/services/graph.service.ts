import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';
import { AuditService } from './audit.service';
import {
  GraphChangeResponse,
  GraphSnapshot,
  GraphDelta,
  NodeState,
  RelationshipState,
  ChangeState,
  RenderNode,
  RenderRelationship,
} from '../models/graph.model';

/**
 * Service for managing graph timeline data and state.
 * Handles fetching graph changes and computing rendered state at any point in time.
 */
@Injectable({ providedIn: 'root' })
export class GraphService {
  private readonly api = inject(ApiService);
  private readonly audit = inject(AuditService);

  // Raw data from API
  readonly changes = signal<GraphChangeResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  // Current position in timeline (tool call index)
  readonly currentIndex = signal(0);

  // Last seek timestamp for velocity calculation
  private lastSeekTime = 0;
  private lastSeekIndex = 0;

  // Computed values
  readonly hasData = computed(() => {
    const data = this.changes();
    return data !== null && data.deltas.length > 0;
  });

  readonly totalOperations = computed(() => {
    return this.changes()?.deltas.length ?? 0;
  });

  readonly summary = computed(() => {
    return this.changes()?.summary ?? null;
  });

  readonly timeRange = computed(() => {
    return this.changes()?.timeRange ?? null;
  });

  /**
   * Get the currently rendered graph state.
   * Combines snapshot + deltas up to currentIndex.
   */
  readonly renderedState = computed(() => {
    const data = this.changes();
    const index = this.currentIndex();

    if (!data || data.deltas.length === 0) {
      return { nodes: [], relationships: [] };
    }

    // Find nearest snapshot before current index
    const snapshot = this.findNearestSnapshot(data.snapshots, index);

    // Start from snapshot state
    const nodes = new Map<string, NodeState>();
    const relationships = new Map<string, RelationshipState>();

    if (snapshot) {
      for (const [id, node] of Object.entries(snapshot.nodes)) {
        nodes.set(id, { ...node });
      }
      for (const [id, rel] of Object.entries(snapshot.relationships)) {
        relationships.set(id, { ...rel });
      }
    }

    // Apply deltas from snapshot to current index
    const startIndex = snapshot ? snapshot.toolCallIndex + 1 : 0;
    for (let i = startIndex; i <= index && i < data.deltas.length; i++) {
      this.applyDelta(data.deltas[i], nodes, relationships, i);
    }

    // Convert to render nodes with change state
    const renderNodes: RenderNode[] = [];
    const renderRels: RenderRelationship[] = [];

    for (const node of nodes.values()) {
      if (node.visible) {
        renderNodes.push({
          ...node,
          changeState: this.getNodeChangeState(node, index),
        });
      }
    }

    for (const rel of relationships.values()) {
      if (rel.visible) {
        renderRels.push({
          ...rel,
          changeState: this.getRelationshipChangeState(rel, index),
        });
      }
    }

    return { nodes: renderNodes, relationships: renderRels };
  });

  /**
   * Get the current delta (if any).
   */
  readonly currentDelta = computed(() => {
    const data = this.changes();
    const index = this.currentIndex();
    return data?.deltas[index] ?? null;
  });

  /**
   * Load graph changes for the currently selected job.
   */
  async loadCurrentJobChanges(): Promise<void> {
    const jobId = this.audit.selectedJobId();
    if (!jobId) {
      this.error.set('No job selected');
      return;
    }

    await this.loadJobChanges(jobId);
  }

  /**
   * Load graph changes for a specific job.
   */
  async loadJobChanges(jobId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    this.api.getGraphChanges(jobId).subscribe({
      next: (response) => {
        console.log('[GraphService] Loaded changes:', {
          deltas: response.deltas.length,
          snapshots: response.snapshots.length,
          summary: response.summary,
        });
        this.changes.set(response);
        // Start at the end to show the final graph state
        const lastIndex = Math.max(0, response.deltas.length - 1);
        this.currentIndex.set(lastIndex);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Failed to load graph changes:', err);
        this.error.set(err.message || 'Failed to load graph changes');
        this.changes.set(null);
        this.loading.set(false);
      },
    });
  }

  /**
   * Seek to a specific tool call index.
   * Uses velocity-based rendering: fast scrub jumps to snapshot, slow scrub applies deltas.
   */
  seekTo(index: number): void {
    const data = this.changes();
    if (!data) return;

    // Clamp index
    const clampedIndex = Math.max(0, Math.min(index, data.deltas.length - 1));

    // Calculate velocity
    const now = performance.now();
    const elapsed = now - this.lastSeekTime;
    const distance = Math.abs(clampedIndex - this.lastSeekIndex);
    const velocity = elapsed > 0 ? distance / elapsed * 1000 : 0; // ops per second

    this.lastSeekTime = now;
    this.lastSeekIndex = clampedIndex;
    this.currentIndex.set(clampedIndex);

    // Velocity is used by the component to decide rendering strategy
    // Fast seeks (>500 ops/sec) should use snapshot rendering
    // Slow seeks should apply deltas incrementally
  }

  /**
   * Step forward one operation.
   */
  stepForward(): void {
    const data = this.changes();
    if (!data) return;

    const current = this.currentIndex();
    if (current < data.deltas.length - 1) {
      this.currentIndex.set(current + 1);
    }
  }

  /**
   * Step backward one operation.
   */
  stepBackward(): void {
    const current = this.currentIndex();
    if (current > 0) {
      this.currentIndex.set(current - 1);
    }
  }

  /**
   * Jump to start.
   */
  jumpToStart(): void {
    this.currentIndex.set(0);
  }

  /**
   * Jump to end.
   */
  jumpToEnd(): void {
    const data = this.changes();
    if (data && data.deltas.length > 0) {
      this.currentIndex.set(data.deltas.length - 1);
    }
  }

  /**
   * Find the nearest snapshot at or before the given index.
   */
  private findNearestSnapshot(
    snapshots: GraphSnapshot[],
    index: number,
  ): GraphSnapshot | null {
    let nearest: GraphSnapshot | null = null;

    for (const snapshot of snapshots) {
      if (snapshot.toolCallIndex <= index) {
        nearest = snapshot;
      } else {
        break;
      }
    }

    return nearest;
  }

  /**
   * Apply a delta to the current graph state.
   */
  private applyDelta(
    delta: GraphDelta,
    nodes: Map<string, NodeState>,
    relationships: Map<string, RelationshipState>,
    currentToolIndex: number,
  ): void {
    const changes = delta.changes;
    const varToId = new Map<string, string>();

    // First, process matched variables to resolve references from MATCH clauses
    if (changes.matchedVariables) {
      for (const matched of changes.matchedVariables) {
        const matchedId = this.getNodeId(matched);
        varToId.set(matched.variable, matchedId);
      }
    }

    // Create nodes
    for (const creation of changes.nodesCreated) {
      const nodeId = this.getNodeId(creation);
      varToId.set(creation.variable, nodeId);

      if (!nodes.has(nodeId) || !creation.merge) {
        nodes.set(nodeId, {
          id: nodeId,
          labels: [creation.label],
          properties: { ...creation.properties },
          createdAt: currentToolIndex,
          modifiedAt: currentToolIndex,
          visible: true,
        });
      }
    }

    // Delete nodes
    for (const deletion of changes.nodesDeleted) {
      const nodeId = varToId.get(deletion.variable) ?? deletion.variable;

      // Find matching node
      for (const [id, node] of nodes) {
        if (id === nodeId || id.includes(deletion.variable)) {
          node.visible = false;
          node.deletedAt = currentToolIndex;
        }
      }
    }

    // Modify nodes
    for (const mod of changes.nodesModified) {
      const nodeId = varToId.get(mod.variable) ?? mod.variable;

      for (const [id, node] of nodes) {
        if (id === nodeId || id.includes(mod.variable)) {
          if (mod.removed && mod.property) {
            delete node.properties[mod.property];
          } else if (mod.property && mod.value !== undefined) {
            node.properties[mod.property] = mod.value;
          }
          node.modifiedAt = currentToolIndex;
        }
      }
    }

    // Create relationships
    for (const creation of changes.relationshipsCreated) {
      const sourceId = varToId.get(creation.sourceVar) ?? creation.sourceVar;
      const targetId = varToId.get(creation.targetVar) ?? creation.targetVar;
      const relId = `${sourceId}-${creation.type}-${targetId}`;

      if (!relationships.has(relId) || !creation.merge) {
        relationships.set(relId, {
          id: relId,
          type: creation.type,
          sourceId,
          targetId,
          properties: { ...creation.properties },
          createdAt: currentToolIndex,
          visible: true,
        });
      }
    }

    // Delete relationships
    for (const deletion of changes.relationshipsDeleted) {
      for (const [id, rel] of relationships) {
        if (id.includes(deletion.variable)) {
          rel.visible = false;
          rel.deletedAt = currentToolIndex;
        }
      }
    }
  }

  /**
   * Get a node ID from creation data.
   */
  private getNodeId(creation: { variable: string; label: string; properties: Record<string, unknown> }): string {
    const props = creation.properties;

    // Try common ID properties (domain-specific IDs first, then generic)
    for (const idProp of ['rid', 'boid', 'mid', 'sid', 'id', 'uuid', 'ID', 'name', 'title', 'key']) {
      if (idProp in props && props[idProp]) {
        return String(props[idProp]);
      }
    }

    // Fallback
    return `${creation.label}_${creation.variable}`;
  }

  /**
   * Determine change state for a node at current index.
   */
  private getNodeChangeState(node: NodeState, currentIndex: number): ChangeState {
    if (node.createdAt === currentIndex) {
      return 'created';
    }
    if (node.modifiedAt === currentIndex && node.modifiedAt !== node.createdAt) {
      return 'modified';
    }
    if (node.deletedAt === currentIndex) {
      return 'deleted';
    }
    return 'unchanged';
  }

  /**
   * Determine change state for a relationship at current index.
   */
  private getRelationshipChangeState(rel: RelationshipState, currentIndex: number): ChangeState {
    if (rel.createdAt === currentIndex) {
      return 'created';
    }
    if (rel.deletedAt === currentIndex) {
      return 'deleted';
    }
    return 'unchanged';
  }

  /**
   * Clear loaded data.
   */
  clear(): void {
    this.changes.set(null);
    this.currentIndex.set(0);
    this.error.set(null);
  }
}
