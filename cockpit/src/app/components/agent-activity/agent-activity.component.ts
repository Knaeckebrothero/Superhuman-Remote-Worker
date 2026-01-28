import {
  Component,
  inject,
  ElementRef,
  viewChildren,
  signal,
  computed,
} from '@angular/core';
import { DataService } from '../../core/services/data.service';
import { RequestService } from '../../core/services/request.service';
import { AuditEntry, AuditFilterCategory, AuditStepType } from '../../core/models/audit.model';

/**
 * Agent Activity component that displays MongoDB audit trail.
 * Shows chronological agent execution steps with filtering and expandable details.
 *
 * Now uses DataService for index-based filtering instead of pagination.
 * Entries are filtered by slider position - only entries up to sliderIndex are shown.
 */
@Component({
  selector: 'app-agent-activity',
  standalone: true,
  template: `
    <div class="activity-container">
      <!-- Filter Bar -->
      <div class="filter-bar">
        @for (filter of filters; track filter.value) {
          <button
            class="filter-btn"
            [class.active]="data.activeFilter() === filter.value"
            (click)="data.setFilter(filter.value)"
          >
            {{ filter.label }}
          </button>
        }
        <span class="entry-count">{{ entryCount() }}</span>
      </div>

      <!-- Loading State -->
      @if (data.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
          @if (data.loadingProgress() > 0) {
            <span class="progress">{{ data.loadingProgress() }}%</span>
          }
        </div>
      }

      <!-- Error State -->
      @if (data.error()) {
        <div class="error-state">
          <span>{{ data.error() }}</span>
          <button (click)="data.refresh()">Retry</button>
        </div>
      }

      <!-- Empty State -->
      @if (!data.isLoading() && !data.error() && entries().length === 0 && data.currentJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4C4;</span>
          <span>No audit entries for this job</span>
          <span class="empty-hint">Try selecting a different filter or moving the slider</span>
        </div>
      }

      <!-- No Job Selected -->
      @if (!data.currentJobId() && !data.isLoading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job from the timeline bar</span>
          <span class="empty-hint">Use the job dropdown at the top to select a job</span>
        </div>
      }

      <!-- Entry List -->
      @if (entries().length > 0) {
        <div class="entry-list" #entryList>
          @for (entry of entries(); track entry._id; let i = $index) {
            <div
              #entryItem
              class="entry-item"
              [class.expanded]="isExpanded(entry._id)"
              [class.phase-strategic]="entry.phase === 'strategic'"
              [class.phase-tactical]="entry.phase === 'tactical'"
              [style.border-left-color]="getStepColor(entry.step_type)"
            >
              <div class="entry-header" (click)="toggleExpanded(entry._id)">
                <span class="step-number">#{{ entry.step_number }}</span>
                <span class="step-badge" [style.background]="getStepColor(entry.step_type)">
                  {{ getStepBadge(entry.step_type) }}
                </span>
                <span class="node-name">{{ entry.node_name }}</span>
                @if (isPending(entry)) {
                  <span class="pending-indicator">pending...</span>
                }
                @if (entry.latency_ms) {
                  <span class="latency">{{ formatLatency(entry.latency_ms) }}</span>
                }
                <span class="timestamp">{{ formatTime(entry.timestamp) }}</span>
                <span class="expand-icon">{{ isExpanded(entry._id) ? '&#x25BC;' : '&#x25B6;' }}</span>
              </div>

              @if (isExpanded(entry._id)) {
                <div class="entry-details">
                  <!-- Initialize Step Details -->
                  @if (entry.step_type === 'initialize') {
                    @if (hasWorkspace(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Workspace:</span>
                        <span class="detail-value">{{ getWorkspaceCreated(entry) ? 'Created' : 'Existing' }}</span>
                      </div>
                    }
                    @if (hasPhaseAlternation(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Phase Alt:</span>
                        <span class="detail-value mono">enabled</span>
                      </div>
                      @if (getStrategicTodosCount(entry)) {
                        <div class="detail-section">
                          <span class="detail-label">Todos:</span>
                          <span class="detail-value mono">{{ getStrategicTodosCount(entry) }} strategic todos loaded</span>
                        </div>
                      }
                      @if (getInstructionsLength(entry)) {
                        <div class="detail-section">
                          <span class="detail-label">Instructions:</span>
                          <span class="detail-value mono">{{ getInstructionsLength(entry) }} chars</span>
                        </div>
                      }
                    }
                  }

                  <!-- LLM Details (combined call + response) -->
                  @if (entry.step_type === 'llm' && entry.llm) {
                    @if (entry.llm.model) {
                      <div class="detail-section">
                        <span class="detail-label">Model:</span>
                        <span class="detail-value mono">{{ entry.llm.model }}</span>
                      </div>
                    }
                    @if (getInputMessageCount(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Input:</span>
                        <span class="detail-value mono">{{ getInputMessageCount(entry) }} messages</span>
                      </div>
                    }
                    @if (getStateMessageCount(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Context:</span>
                        <span class="detail-value mono">{{ getStateMessageCount(entry) }} messages in state</span>
                      </div>
                    }
                    @if (getRequestId(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Request:</span>
                        <span
                          class="detail-value mono request-link"
                          (click)="onRequestIdClick(getRequestId(entry)!); $event.stopPropagation()"
                          title="Click to view full request"
                        >
                          {{ getRequestId(entry)!.slice(0, 12) }}...
                        </span>
                      </div>
                    }
                    @if (entry.llm.response_content_preview) {
                      <div class="detail-section">
                        <span class="detail-label">Response:</span>
                        <span class="detail-value response-preview">{{ entry.llm.response_content_preview }}</span>
                      </div>
                    } @else if (isPending(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Response:</span>
                        <span class="detail-value pending">Waiting for LLM response...</span>
                      </div>
                    }
                    @if (entry.llm.tool_calls && entry.llm.tool_calls.length > 0) {
                      <div class="detail-section">
                        <span class="detail-label">Tool Calls:</span>
                        <div class="tool-calls">
                          @for (tc of entry.llm.tool_calls; track tc.name) {
                            <span class="tool-chip">{{ tc.name }}</span>
                          }
                        </div>
                      </div>
                    }
                    @if (getLLMMetrics(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Output:</span>
                        <span class="detail-value mono">{{ getOutputChars(entry) }} chars</span>
                      </div>
                    }
                  }

                  <!-- Tool Details (combined call + result) -->
                  @if (entry.step_type === 'tool' && entry.tool) {
                    <div class="detail-section">
                      <span class="detail-label">Tool:</span>
                      <span class="detail-value mono">{{ entry.tool.name }}</span>
                    </div>
                    @if (entry.tool.arguments) {
                      <div class="detail-section">
                        <span class="detail-label">Arguments:</span>
                        <pre class="detail-json">{{ formatJson(entry.tool.arguments) }}</pre>
                      </div>
                    }
                    @if (entry.tool.success !== undefined && entry.tool.success !== null) {
                      <div class="detail-section">
                        <span class="detail-label">Status:</span>
                        <span class="detail-value" [class.success]="entry.tool.success" [class.failure]="!entry.tool.success">
                          {{ entry.tool.success ? 'Success' : 'Failed' }}
                        </span>
                      </div>
                    } @else if (isPending(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Status:</span>
                        <span class="detail-value pending">Executing...</span>
                      </div>
                    }
                    @if (entry.tool.result_preview) {
                      <div class="detail-section">
                        <span class="detail-label">Result:</span>
                        <span class="detail-value result-preview">{{ entry.tool.result_preview }}</span>
                      </div>
                    }
                    @if (entry.tool.error) {
                      <div class="detail-section">
                        <span class="detail-label">Error:</span>
                        <span class="detail-value error">{{ entry.tool.error }}</span>
                      </div>
                    }
                  }

                  <!-- Check/Routing Details -->
                  @if ((entry.step_type === 'check' || entry.step_type === 'routing' || entry.step_type === 'phase_complete')) {
                    @if (getRoutingData(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Decision:</span>
                        <span class="detail-value mono">{{ formatJson(getRoutingData(entry)) }}</span>
                      </div>
                    }
                  }

                  <!-- Error Details -->
                  @if (entry.step_type === 'error' && entry.error) {
                    <div class="detail-section">
                      <span class="detail-label">Type:</span>
                      <span class="detail-value mono">{{ entry.error.type }}</span>
                    </div>
                    <div class="detail-section">
                      <span class="detail-label">Message:</span>
                      <span class="detail-value error">{{ entry.error.message }}</span>
                    </div>
                    @if (entry.error.traceback) {
                      <div class="detail-section">
                        <span class="detail-label">Traceback:</span>
                        <pre class="detail-json error">{{ entry.error.traceback }}</pre>
                      </div>
                    }
                  }

                  <!-- Phase Info -->
                  @if (entry.phase) {
                    <div class="detail-section">
                      <span class="detail-label">Phase:</span>
                      <span class="detail-value">{{ entry.phase }}@if (entry.phase_number !== undefined) { ({{ entry.phase_number }})}</span>
                    </div>
                  }

                  <!-- Iteration -->
                  <div class="detail-section">
                    <span class="detail-label">Iteration:</span>
                    <span class="detail-value mono">{{ entry.iteration }}</span>
                  </div>
                </div>
              }
            </div>
          }
        </div>
      }

      <!-- Position indicator (replaces pagination) -->
      @if (data.totalAuditEntries() > 0) {
        <div class="position-bar">
          <span class="position-info">
            Showing {{ entries().length }} of {{ data.totalAuditEntries() }} entries
            (up to step #{{ data.sliderIndex() }})
          </span>
          <div class="position-controls">
            <button class="pos-btn" (click)="data.seekToStart()" title="Jump to start">
              &#x23EE;
            </button>
            <button class="pos-btn" (click)="data.seekToEnd()" title="Jump to end">
              &#x23ED;
            </button>
          </div>
        </div>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .activity-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Filter Bar */
      .filter-bar {
        display: flex;
        gap: 4px;
        padding: 8px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
        align-items: center;
      }

      .filter-btn {
        padding: 5px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .filter-btn:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .filter-btn.active {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-color: var(--accent-color, #cba6f7);
      }

      .entry-count {
        margin-left: auto;
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      /* Loading Overlay */
      .loading-overlay {
        position: absolute;
        top: 80px;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(17, 17, 27, 0.8);
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        z-index: 10;
      }

      .spinner {
        width: 32px;
        height: 32px;
        border: 3px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .progress {
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }

      /* Error State */
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

      .error-state button {
        padding: 8px 16px;
        border: 1px solid #f38ba8;
        border-radius: 4px;
        background: transparent;
        color: #f38ba8;
        cursor: pointer;
      }

      .error-state button:hover {
        background: rgba(243, 139, 168, 0.1);
      }

      /* Empty State */
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
        margin-top: 8px;
      }

      /* Entry List */
      .entry-list {
        flex: 1;
        overflow: auto;
        padding: 8px;
      }

      .entry-item {
        border-left: 3px solid var(--border-color, #45475a);
        margin-bottom: 4px;
        border-radius: 4px;
        background: var(--surface-0, #313244);
        transition: all 0.15s ease;
      }

      .entry-item:hover {
        background: var(--panel-header-bg, #1e1e2e);
      }

      /* Phase-based background tinting */
      .entry-item.phase-strategic {
        background: rgba(203, 166, 247, 0.06);
      }

      .entry-item.phase-strategic:hover {
        background: rgba(203, 166, 247, 0.10);
      }

      .entry-item.phase-tactical {
        background: rgba(166, 227, 161, 0.06);
      }

      .entry-item.phase-tactical:hover {
        background: rgba(166, 227, 161, 0.10);
      }

      .entry-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        cursor: pointer;
        user-select: none;
      }

      .step-number {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        min-width: 36px;
      }

      .step-badge {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 600;
        color: var(--timeline-bg, #11111b);
        text-transform: uppercase;
      }

      .node-name {
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        flex: 1;
      }

      .latency {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .timestamp {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .expand-icon {
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        width: 16px;
        text-align: center;
      }

      /* Entry Details */
      .entry-details {
        padding: 8px 10px 12px 52px;
        border-top: 1px solid var(--border-color, #45475a);
        background: rgba(0, 0, 0, 0.15);
      }

      .detail-section {
        display: flex;
        gap: 8px;
        margin-bottom: 6px;
        font-size: 12px;
      }

      .detail-section:last-child {
        margin-bottom: 0;
      }

      .detail-label {
        color: var(--text-muted, #6c7086);
        min-width: 70px;
        flex-shrink: 0;
      }

      .detail-value {
        color: var(--text-primary, #cdd6f4);
        word-break: break-word;
      }

      .detail-value.mono {
        font-family: 'JetBrains Mono', monospace;
      }

      .detail-value.success {
        color: #a6e3a1;
      }

      .detail-value.failure,
      .detail-value.error {
        color: #f38ba8;
      }

      .detail-value.response-preview,
      .detail-value.result-preview {
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 200px;
        overflow-y: auto;
        display: block;
        padding: 6px 8px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 4px;
        font-size: 11px;
        line-height: 1.4;
      }

      .detail-json {
        margin: 0;
        padding: 6px 8px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #a6e3a1;
        overflow-x: auto;
        max-height: 150px;
        white-space: pre-wrap;
        word-break: break-all;
      }

      .tool-calls {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }

      .tool-chip {
        padding: 2px 8px;
        border-radius: 3px;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
      }

      /* Position Bar (replaces Pagination) */
      .position-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .position-info {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      .position-controls {
        display: flex;
        gap: 8px;
      }

      .pos-btn {
        padding: 4px 8px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 14px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .pos-btn:hover {
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-primary, #cdd6f4);
      }

      .request-link {
        color: #89b4fa;
        cursor: pointer;
        text-decoration: underline;
        text-decoration-style: dotted;
        text-underline-offset: 2px;
      }

      .request-link:hover {
        color: #b4befe;
        text-decoration-style: solid;
      }

      .pending-indicator {
        font-size: 10px;
        color: #f9e2af;
        font-style: italic;
        animation: pulse 1.5s ease-in-out infinite;
      }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.5; }
      }

      .detail-value.pending {
        color: #f9e2af;
        font-style: italic;
      }
    `,
  ],
})
export class AgentActivityComponent {
  readonly data = inject(DataService);
  private readonly requestService = inject(RequestService);

  // Query all entry items for scrolling
  readonly entryItems = viewChildren<ElementRef<HTMLElement>>('entryItem');

  // Local expanded state (not part of DataService)
  private readonly expandedIds = signal<Set<string>>(new Set());

  readonly filters: { label: string; value: AuditFilterCategory }[] = [
    { label: 'All', value: 'all' },
    { label: 'Messages', value: 'messages' },
    { label: 'Tools', value: 'tools' },
    { label: 'Errors', value: 'errors' },
  ];

  // Use DataService's visible entries (filtered by slider position and active filter)
  readonly entries = computed(() => this.data.visibleAuditEntries());

  // Entry count display
  readonly entryCount = computed(() => {
    const visible = this.entries().length;
    const total = this.data.totalAuditEntries();
    return `${visible}/${total}`;
  });

  // Step type color mapping
  private readonly stepColors: Record<AuditStepType, string> = {
    initialize: '#89b4fa',    // Blue
    llm: '#a6e3a1',           // Green (combined LLM call+response)
    tool: '#cba6f7',          // Purple (combined tool call+result)
    check: '#fab387',         // Peach
    routing: '#94e2d5',       // Teal
    phase_complete: '#74c7ec',// Sapphire
    error: '#f38ba8',         // Red
  };

  // Step type badge labels
  private readonly stepBadges: Record<AuditStepType, string> = {
    initialize: 'INIT',
    llm: 'LLM',
    tool: 'TOOL',
    check: 'CHECK',
    routing: 'ROUTE',
    phase_complete: 'PHASE',
    error: 'ERROR',
  };

  toggleExpanded(entryId: string): void {
    const current = this.expandedIds();
    const updated = new Set(current);

    if (updated.has(entryId)) {
      updated.delete(entryId);
    } else {
      updated.add(entryId);
    }

    this.expandedIds.set(updated);
  }

  isExpanded(entryId: string): boolean {
    return this.expandedIds().has(entryId);
  }

  getStepColor(stepType: AuditStepType): string {
    return this.stepColors[stepType] || '#6c7086';
  }

  getStepBadge(stepType: AuditStepType): string {
    return this.stepBadges[stepType] || stepType.toUpperCase();
  }

  formatLatency(ms: number): string {
    if (ms < 1000) {
      return `${ms}ms`;
    }
    return `${(ms / 1000).toFixed(1)}s`;
  }

  formatTime(timestamp: string): string {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  formatJson(obj: Record<string, unknown> | unknown): string {
    try {
      return JSON.stringify(obj, null, 2);
    } catch {
      return '[object]';
    }
  }

  // Helper methods for accessing nested data that may not be typed
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private asAny(obj: unknown): any {
    return obj;
  }

  hasWorkspace(entry: AuditEntry): boolean {
    return !!this.asAny(entry)['workspace'];
  }

  getWorkspaceCreated(entry: AuditEntry): boolean {
    return this.asAny(entry)['workspace']?.['created'] === true;
  }

  hasPhaseAlternation(entry: AuditEntry): boolean {
    return this.asAny(entry)['phase_alternation'] === true;
  }

  getStrategicTodosCount(entry: AuditEntry): number | undefined {
    return this.asAny(entry)['strategic_todos'];
  }

  getInstructionsLength(entry: AuditEntry): number | undefined {
    return this.asAny(entry)['instructions_length'];
  }

  getInputMessageCount(entry: AuditEntry): number | undefined {
    return this.asAny(entry.llm)?.['input_message_count'];
  }

  getStateMessageCount(entry: AuditEntry): number | undefined {
    return this.asAny(entry)['state']?.['message_count'];
  }

  getLLMMetrics(entry: AuditEntry): Record<string, unknown> | undefined {
    return this.asAny(entry.llm)?.['metrics'];
  }

  getOutputChars(entry: AuditEntry): number {
    return this.asAny(entry.llm)?.['metrics']?.['output_chars'] || 0;
  }

  getRoutingData(entry: AuditEntry): Record<string, unknown> | undefined {
    // Routing entries may have various fields - return any non-standard fields
    const known = new Set([
      '_id', 'job_id', 'step_number', 'step_type', 'node_name', 'timestamp',
      'latency_ms', 'iteration', 'phase', 'metadata', 'agent_type', 'llm',
      'tool', 'error', 'state', 'started_at', 'completed_at'
    ]);
    const extra: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(entry)) {
      if (!known.has(key) && value !== undefined) {
        extra[key] = value;
      }
    }
    return Object.keys(extra).length > 0 ? extra : undefined;
  }

  getRequestId(entry: AuditEntry): string | undefined {
    return this.asAny(entry.llm)?.['request_id'];
  }

  onRequestIdClick(requestId: string): void {
    this.requestService.loadRequest(requestId);
  }

  /**
   * Check if an entry is still pending (completed_at is explicitly null).
   * Only applies to entries that have the started_at field (new combined format).
   */
  isPending(entry: AuditEntry): boolean {
    // Only consider as pending if it has started_at (new format) and completed_at is null
    const hasStartedAt = this.asAny(entry)['started_at'] !== undefined;
    const completedAtIsNull = this.asAny(entry)['completed_at'] === null;
    return hasStartedAt && completedAtIsNull;
  }
}
