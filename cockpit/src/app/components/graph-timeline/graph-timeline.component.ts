import {
  Component,
  OnDestroy,
  inject,
  signal,
  effect,
  ElementRef,
  viewChild,
  ChangeDetectionStrategy,
  NgZone,
  DestroyRef,
  computed,
} from '@angular/core';
import { GraphService } from '../../core/services/graph.service';
import { DataService } from '../../core/services/data.service';
import { cytoscapeStyles } from './graph-styles';
import { TimelineRenderer } from './timeline-renderer';
import type { Core } from 'cytoscape';

// Dynamic import for Cytoscape (loaded at runtime)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let cytoscape: any;

/**
 * Graph Timeline component for visualizing Neo4j changes over time.
 * Uses Cytoscape.js for rendering with snapshot/delta optimization.
 *
 * Uses DataService for job selection and maintains its own slider
 * for graph-specific timeline operations.
 */
@Component({
  selector: 'app-graph-timeline',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="graph-timeline-container">
      <!-- Header with controls -->
      <div class="header">
        <div class="left-controls">
          <button
            class="btn"
            (click)="loadGraphChanges()"
            [disabled]="graph.loading() || !data.currentJobId()"
            title="Load graph changes for selected job"
          >
            Load Graph
          </button>
        </div>
        <div class="right-controls">
          @if (graph.hasData()) {
            <button class="btn icon-btn" (click)="fitGraph()" title="Fit to view">
              &#x26F6;
            </button>
            <button class="btn icon-btn" (click)="runLayout()" title="Re-layout">
              &#x21BB;
            </button>
          }
        </div>
      </div>

      <!-- Summary bar -->
      @if (graph.summary(); as summary) {
        <div class="summary-bar">
          <span class="summary-item">
            <span class="summary-label">Operations:</span>
            <span class="summary-value">{{ summary.graphToolCalls }}</span>
          </span>
          <span class="summary-item created">
            <span class="dot"></span>
            <span class="summary-value">{{ summary.nodesCreated }} nodes</span>
          </span>
          <span class="summary-item modified">
            <span class="dot"></span>
            <span class="summary-value">{{ summary.nodesModified }} modified</span>
          </span>
          <span class="summary-item deleted">
            <span class="dot"></span>
            <span class="summary-value">{{ summary.nodesDeleted }} deleted</span>
          </span>
        </div>
      }

      <!-- Timeline slider -->
      @if (graph.hasData()) {
        <div class="timeline-controls">
          <button
            class="btn icon-btn"
            (click)="graph.jumpToStart()"
            [disabled]="graph.currentIndex() === 0"
            title="Jump to start"
          >
            &#x23EE;
          </button>
          <button
            class="btn icon-btn"
            (click)="graph.stepBackward()"
            [disabled]="graph.currentIndex() === 0"
            title="Step backward"
          >
            &#x23F4;
          </button>
          <input
            type="range"
            class="timeline-slider"
            [min]="0"
            [max]="graph.totalOperations() - 1"
            [value]="graph.currentIndex()"
            (input)="onTimelineChange($event)"
          />
          <button
            class="btn icon-btn"
            (click)="graph.stepForward()"
            [disabled]="graph.currentIndex() >= graph.totalOperations() - 1"
            title="Step forward"
          >
            &#x23F5;
          </button>
          <button
            class="btn icon-btn"
            (click)="graph.jumpToEnd()"
            [disabled]="graph.currentIndex() >= graph.totalOperations() - 1"
            title="Jump to end"
          >
            &#x23ED;
          </button>
          <span class="timeline-position">
            {{ graph.currentIndex() + 1 }} / {{ graph.totalOperations() }}
          </span>
        </div>
      }

      <!-- Current query display -->
      @if (graph.currentDelta(); as delta) {
        <div class="current-query">
          <span class="query-label">Cypher:</span>
          <code class="query-text">{{ truncateQuery(delta.cypherQuery) }}</code>
        </div>
      }

      <!-- Loading state -->
      @if (graph.loading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
          <span>Loading graph changes...</span>
        </div>
      }

      <!-- Error state -->
      @if (graph.error()) {
        <div class="error-state">
          <span class="error-icon">&#x26A0;</span>
          <span>{{ graph.error() }}</span>
          <button class="btn" (click)="loadGraphChanges()">Retry</button>
        </div>
      }

      <!-- Empty state -->
      @if (!graph.loading() && !graph.error() && !graph.hasData() && data.currentJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4C8;</span>
          <span>No graph operations found</span>
          <span class="empty-hint">This job has no execute_cypher_query calls</span>
        </div>
      }

      <!-- No job selected -->
      @if (!data.currentJobId() && !graph.loading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job from the timeline bar</span>
          <span class="empty-hint">Then click "Load Graph" to visualize operations</span>
        </div>
      }

      <!-- Graph container -->
      <div #graphContainer class="graph-container" [class.hidden]="!graph.hasData()"></div>

      <!-- Legend -->
      @if (graph.hasData()) {
        <div class="legend">
          <span class="legend-item">
            <span class="legend-dot created"></span>
            Created
          </span>
          <span class="legend-item">
            <span class="legend-dot modified"></span>
            Modified
          </span>
          <span class="legend-item">
            <span class="legend-dot deleted"></span>
            Deleted
          </span>
        </div>
      }
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: hidden;
    }

    .graph-timeline-container {
      display: flex;
      flex-direction: column;
      height: 100%;
      background: var(--panel-bg, #181825);
      position: relative;
    }

    /* Header */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 8px;
      background: var(--surface-0, #313244);
      border-bottom: 1px solid var(--border-color, #313244);
      flex-shrink: 0;
    }

    .left-controls,
    .right-controls {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .btn {
      padding: 6px 12px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      background: var(--panel-header-bg, #1e1e2e);
      color: var(--text-secondary, #a6adc8);
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .btn:hover:not(:disabled) {
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      border-color: var(--text-muted, #6c7086);
    }

    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .icon-btn {
      padding: 6px 8px;
      font-size: 14px;
    }

    /* Summary bar */
    .summary-bar {
      display: flex;
      gap: 16px;
      padding: 6px 12px;
      background: var(--panel-header-bg, #1e1e2e);
      border-bottom: 1px solid var(--border-color, #313244);
      font-size: 11px;
      flex-shrink: 0;
    }

    .summary-item {
      display: flex;
      align-items: center;
      gap: 4px;
      color: var(--text-muted, #6c7086);
    }

    .summary-label {
      color: var(--text-secondary, #a6adc8);
    }

    .summary-value {
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-primary, #cdd6f4);
    }

    .summary-item .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }

    .summary-item.created .dot {
      background: #0072B2;
    }

    .summary-item.modified .dot {
      background: #E69F00;
    }

    .summary-item.deleted .dot {
      background: #D55E00;
    }

    /* Timeline controls */
    .timeline-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      background: var(--surface-0, #313244);
      border-bottom: 1px solid var(--border-color, #313244);
      flex-shrink: 0;
    }

    .timeline-slider {
      flex: 1;
      height: 6px;
      -webkit-appearance: none;
      appearance: none;
      background: var(--panel-bg, #181825);
      border-radius: 3px;
      outline: none;
    }

    .timeline-slider::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 14px;
      height: 14px;
      background: var(--accent-color, #cba6f7);
      border-radius: 50%;
      cursor: pointer;
      border: 2px solid var(--panel-bg, #181825);
    }

    .timeline-slider::-moz-range-thumb {
      width: 14px;
      height: 14px;
      background: var(--accent-color, #cba6f7);
      border-radius: 50%;
      cursor: pointer;
      border: 2px solid var(--panel-bg, #181825);
    }

    .timeline-position {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      min-width: 80px;
      text-align: right;
    }

    /* Current query */
    .current-query {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      background: var(--panel-header-bg, #1e1e2e);
      border-bottom: 1px solid var(--border-color, #313244);
      font-size: 11px;
      flex-shrink: 0;
      overflow: hidden;
    }

    .query-label {
      color: var(--text-muted, #6c7086);
      flex-shrink: 0;
    }

    .query-text {
      font-family: 'JetBrains Mono', monospace;
      color: #a6e3a1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Graph container */
    .graph-container {
      flex: 1;
      min-height: 200px;
      background: var(--timeline-bg, #11111b);
    }

    .graph-container.hidden {
      display: none;
    }

    /* Legend */
    .legend {
      display: flex;
      gap: 16px;
      padding: 6px 12px;
      background: var(--surface-0, #313244);
      border-top: 1px solid var(--border-color, #313244);
      font-size: 11px;
      flex-shrink: 0;
    }

    .legend-item {
      display: flex;
      align-items: center;
      gap: 4px;
      color: var(--text-muted, #6c7086);
    }

    .legend-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      border: 2px solid;
    }

    .legend-dot.created {
      background: transparent;
      border-color: #0072B2;
    }

    .legend-dot.modified {
      background: transparent;
      border-color: #E69F00;
      border-style: dashed;
    }

    .legend-dot.deleted {
      background: rgba(213, 94, 0, 0.4);
      border-color: #D55E00;
    }

    /* Loading overlay */
    .loading-overlay {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(17, 17, 27, 0.9);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 16px;
      z-index: 10;
      color: var(--text-secondary, #a6adc8);
    }

    .spinner {
      width: 32px;
      height: 32px;
      border: 3px solid var(--surface-0, #313244);
      border-top-color: var(--accent-color, #cba6f7);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    /* Error state */
    .error-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 12px;
      padding: 40px;
      color: #f38ba8;
      flex: 1;
    }

    .error-icon {
      font-size: 32px;
    }

    /* Empty state */
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 12px;
      padding: 40px;
      color: var(--text-muted, #6c7086);
      flex: 1;
    }

    .empty-icon {
      font-size: 48px;
      opacity: 0.5;
    }

    .empty-hint {
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
      opacity: 0.6;
    }
  `],
})
export class GraphTimelineComponent implements OnDestroy {
  private readonly ngZone = inject(NgZone);
  private readonly destroyRef = inject(DestroyRef);
  readonly graph = inject(GraphService);
  readonly data = inject(DataService);

  private readonly graphContainer = viewChild<ElementRef<HTMLDivElement>>('graphContainer');

  private cy: Core | null = null;
  private renderer: TimelineRenderer | null = null;
  private cyListeners: Array<() => void> = [];

  // RAF gating for timeline scrubbing
  private pendingSeekIndex: number | null = null;
  private rafId: number | null = null;

  // Selected node for details panel
  readonly selectedNodeId = signal<string | null>(null);

  constructor() {
    // Initialize Cytoscape and load data when available
    effect(() => {
      const graphData = this.graph.changes();
      if (graphData && graphData.deltas.length > 0) {
        console.log('[GraphTimeline] Data available:', graphData.deltas.length, 'deltas');

        if (!this.cy) {
          // Initialize Cytoscape first, then load data
          this.initializeCytoscape().then(() => {
            console.log('[GraphTimeline] Cytoscape initialized, loading data');
            if (this.renderer && graphData) {
              this.renderer.load(graphData.snapshots, graphData.deltas);
              // Jump to end to show the final state
              this.graph.jumpToEnd();
            }
          });
        } else if (this.renderer) {
          // Cytoscape already exists, just load new data
          console.log('[GraphTimeline] Reloading data into existing renderer');
          this.renderer.load(graphData.snapshots, graphData.deltas);
          this.graph.jumpToEnd();
        }
      }
    });

    // Sync timeline position (only after initial load)
    effect(() => {
      const index = this.graph.currentIndex();
      if (this.renderer && this.cy) {
        console.log('[GraphTimeline] Seeking to index:', index);
        this.renderer.seekTo(index);
      }
    });

    // Clear graph when job changes
    effect(() => {
      const jobId = this.data.currentJobId();
      // When job changes, clear old graph data
      if (!jobId) {
        this.graph.clear();
      }
    });
  }

  ngOnDestroy(): void {
    // Cancel pending RAF
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
    }

    // Clean up Cytoscape listeners
    this.cyListeners.forEach((cleanup) => cleanup());

    // Destroy Cytoscape instance
    this.cy?.destroy();
    this.cy = null;
    this.renderer = null;
  }

  /**
   * Initialize Cytoscape instance outside Angular zone.
   */
  private async initializeCytoscape(): Promise<void> {
    const container = this.graphContainer()?.nativeElement;
    if (!container) return;

    // Dynamic import Cytoscape and fcose layout
    if (!cytoscape) {
      try {
        const [cyModule, fcoseModule] = await Promise.all([
          import('cytoscape'),
          import('cytoscape-fcose'),
        ]);
        cytoscape = cyModule.default;
        const fcose = fcoseModule.default;

        // Register fcose layout
        cytoscape.use(fcose);
        console.log('[GraphTimeline] Cytoscape and fcose loaded');
      } catch (e) {
        console.error('Failed to load Cytoscape:', e);
        this.graph.error.set('Failed to load graph library. Please install cytoscape and cytoscape-fcose.');
        return;
      }
    }

    // Initialize outside Angular zone
    this.ngZone.runOutsideAngular(() => {
      this.cy = cytoscape({
        container,
        style: cytoscapeStyles,
        layout: { name: 'preset' },
        minZoom: 0.1,
        maxZoom: 3,
        wheelSensitivity: 0.3,
      });

      // Create renderer
      if (this.cy) {
        this.renderer = new TimelineRenderer(this.cy);
      }

      // Load initial data
      const graphData = this.graph.changes();
      if (graphData && this.renderer) {
        this.renderer.load(graphData.snapshots, graphData.deltas);
      }

      // Set up event listeners
      this.setupCytoscapeListeners();
    });
  }

  /**
   * Set up Cytoscape event listeners.
   */
  private setupCytoscapeListeners(): void {
    if (!this.cy) return;

    // Node tap handler
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tapHandler = (evt: any) => {
      const nodeData = evt.target.data();
      if (nodeData.id) {
        this.ngZone.run(() => {
          this.selectedNodeId.set(nodeData.id ?? null);
        });
      }
    };

    this.cy.on('tap', 'node', tapHandler);
    this.cyListeners.push(() => this.cy?.off('tap', 'node', tapHandler));

    // Background tap to deselect
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const bgTapHandler = (evt: any) => {
      if (evt.target === this.cy) {
        this.ngZone.run(() => {
          this.selectedNodeId.set(null);
        });
      }
    };

    this.cy.on('tap', bgTapHandler);
    this.cyListeners.push(() => this.cy?.off('tap', bgTapHandler));
  }

  /**
   * Load graph changes for selected job.
   */
  loadGraphChanges(): void {
    const jobId = this.data.currentJobId();
    if (jobId) {
      this.graph.loadJobChanges(jobId);
    }
  }

  /**
   * Handle timeline slider change with RAF gating.
   */
  onTimelineChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    const index = parseInt(input.value, 10);

    // RAF gating to prevent render queue overflow
    this.pendingSeekIndex = index;

    if (!this.rafId) {
      this.rafId = requestAnimationFrame(() => {
        if (this.pendingSeekIndex !== null) {
          this.graph.seekTo(this.pendingSeekIndex);
        }
        this.pendingSeekIndex = null;
        this.rafId = null;
      });
    }
  }

  /**
   * Fit graph to viewport.
   */
  fitGraph(): void {
    this.renderer?.fit();
  }

  /**
   * Run layout algorithm.
   */
  runLayout(): void {
    this.renderer?.runLayout(true);
  }

  /**
   * Truncate long Cypher queries for display.
   */
  truncateQuery(query: string): string {
    const maxLength = 100;
    const cleaned = query.replace(/\s+/g, ' ').trim();
    return cleaned.length > maxLength
      ? cleaned.substring(0, maxLength - 3) + '...'
      : cleaned;
  }
}
