import { Component, effect, inject, signal, untracked } from '@angular/core';
import { AuditTraceService } from '../../../core/services/audit-trace.service';
import { DataService } from '../../../core/services/data.service';
import { RequestService } from '../../services/request.service';
import { AuditEntry, AuditFilterCategory, AuditStepType } from '../../../core/models/audit.model';
import { AppSpinnerComponent } from '../../../ui/spinner';

/**
 * Agent Activity component — the agent execution trace.
 *
 * Shows chronological agent steps with server-side filtering and expandable
 * detail. Backed by {@link AuditTraceService}: lean rows are fetched a page at a
 * time (infinite scroll), and a step's heavy detail (tool arguments, error
 * traceback, in-state message count) is lazy-loaded only on expand. No eager
 * download, no IndexedDB, no slider — see
 * knowledge-base/knowledge/features/debug_audit_view_refactor.md (Phase 2).
 */
@Component({
  selector: 'app-agent-activity',
  standalone: true,
  imports: [AppSpinnerComponent],
  template: `
    <div class="activity-container">
      <!-- Filter Bar -->
      <div class="filter-bar">
        @for (filter of filters; track filter.value) {
          <button
            class="filter-btn"
            [class.active]="trace.filter() === filter.value"
            (click)="trace.setFilter(filter.value)"
          >
            {{ filter.label }}
          </button>
        }
        <button
          class="sort-btn"
          (click)="trace.toggleOrder()"
          [title]="trace.order() === 'asc' ? 'Oldest first — click to show newest first' : 'Newest first — click to show oldest first'"
        >
          {{ trace.order() === 'asc' ? '↑ Oldest' : '↓ Newest' }}
        </button>
        <span class="entry-count">{{ trace.rows().length }}/{{ trace.total() }}</span>
      </div>

      <!-- Loading State (first page) -->
      @if (trace.loading()) {
        <div class="loading-overlay">
          <app-spinner size="lg" tone="accent" />
        </div>
      }

      <!-- Error State -->
      @if (trace.error()) {
        <div class="error-state">
          <span>{{ trace.error() }}</span>
          <button (click)="trace.refresh()">Retry</button>
        </div>
      }

      <!-- Empty State -->
      @if (!trace.loading() && !trace.error() && trace.rows().length === 0 && trace.jobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4C4;</span>
          <span>No audit entries for this job</span>
          <span class="empty-hint">Try a different filter</span>
        </div>
      }

      <!-- No Job Selected -->
      @if (!trace.jobId() && !trace.loading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job from the timeline bar</span>
          <span class="empty-hint">Use the job dropdown at the top to select a job</span>
        </div>
      }

      <!-- Entry List (infinite scroll) -->
      @if (trace.rows().length > 0) {
        <div class="entry-list" (scroll)="onScroll($event)">
          @for (entry of trace.rows(); track entry._id) {
            <div
              class="entry-item"
              [class.expanded]="isExpanded(entry._id)"
              [class.phase-strategic]="entry.phase === 'strategic'"
              [class.phase-tactical]="entry.phase === 'tactical'"
              [style.border-left-color]="getStepColor(entry.step_type, entry)"
            >
              <div class="entry-header" (click)="toggleExpanded(entry)">
                <span class="step-number">#{{ entry.step_number }}</span>
                <span class="step-badge" [style.background]="getStepColor(entry.step_type, entry)">
                  {{ getStepBadge(entry.step_type, entry) }}
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
                @let detail = trace.detailFor(entry);
                <div class="entry-details">
                  <!-- Initialize Step Details -->
                  @if (detail.step_type === 'initialize') {
                    @if (hasWorkspace(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Workspace:</span>
                        <span class="detail-value">{{ getWorkspaceCreated(detail) ? 'Created' : 'Existing' }}</span>
                      </div>
                    }
                    @if (hasPhaseAlternation(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Phase Alt:</span>
                        <span class="detail-value mono">enabled</span>
                      </div>
                      @if (getStrategicTodosCount(detail)) {
                        <div class="detail-section">
                          <span class="detail-label">Todos:</span>
                          <span class="detail-value mono">{{ getStrategicTodosCount(detail) }} strategic todos loaded</span>
                        </div>
                      }
                      @if (getInstructionsLength(detail)) {
                        <div class="detail-section">
                          <span class="detail-label">Instructions:</span>
                          <span class="detail-value mono">{{ getInstructionsLength(detail) }} chars</span>
                        </div>
                      }
                    }
                  }

                  <!-- LLM Details (combined call + response) -->
                  @if (detail.step_type === 'llm' && detail.llm) {
                    @if (detail.llm.model) {
                      <div class="detail-section">
                        <span class="detail-label">Model:</span>
                        <span class="detail-value mono">{{ detail.llm.model }}</span>
                      </div>
                    }
                    @if (getInputMessageCount(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Input:</span>
                        <span class="detail-value mono">{{ getInputMessageCount(detail) }} messages</span>
                      </div>
                    }
                    @if (getStateMessageCount(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Context:</span>
                        <span class="detail-value mono">{{ getStateMessageCount(detail) }} messages in state</span>
                      </div>
                    }
                    @if (getRequestId(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Request:</span>
                        <span
                          class="detail-value mono request-link"
                          (click)="onRequestIdClick(getRequestId(detail)!); $event.stopPropagation()"
                          title="Click to view full request"
                        >
                          {{ getRequestId(detail)!.slice(0, 12) }}...
                        </span>
                      </div>
                    }
                    @if (detail.llm.response_content_preview) {
                      <div class="detail-section">
                        <span class="detail-label">Response:</span>
                        <span class="detail-value response-preview">{{ detail.llm.response_content_preview }}</span>
                      </div>
                    } @else if (isPending(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Response:</span>
                        <span class="detail-value pending">Waiting for LLM response...</span>
                      </div>
                    }
                    @if (detail.llm.tool_calls && detail.llm.tool_calls.length > 0) {
                      <div class="detail-section">
                        <span class="detail-label">Tool Calls:</span>
                        <div class="tool-calls">
                          @for (tc of detail.llm.tool_calls; track tc.name) {
                            <span class="tool-chip" [style.background]="getToolColor(tc.name)">{{ tc.name }}</span>
                          }
                        </div>
                      </div>
                    }
                    @if (getLLMMetrics(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Output:</span>
                        <span class="detail-value mono">{{ getOutputChars(detail) }} chars</span>
                      </div>
                    }
                  }

                  <!-- Tool Details (combined call + result) -->
                  @if (detail.step_type === 'tool' && detail.tool) {
                    <div class="detail-section">
                      <span class="detail-label">Tool:</span>
                      <span class="detail-value mono">{{ detail.tool.name }}</span>
                    </div>
                    @if (detail.tool.arguments) {
                      <div class="detail-section">
                        <span class="detail-label">Arguments:</span>
                        <pre class="detail-json">{{ formatJson(detail.tool.arguments) }}</pre>
                      </div>
                    } @else if (!isDetailLoaded(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Arguments:</span>
                        <span class="detail-value pending">Loading details...</span>
                      </div>
                    }
                    @if (detail.tool.success !== undefined && detail.tool.success !== null) {
                      <div class="detail-section">
                        <span class="detail-label">Status:</span>
                        <span class="detail-value" [class.success]="detail.tool.success" [class.failure]="!detail.tool.success">
                          {{ detail.tool.success ? 'Success' : 'Failed' }}
                        </span>
                      </div>
                    } @else if (isPending(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Status:</span>
                        <span class="detail-value pending">Executing...</span>
                      </div>
                    }
                    @if (detail.tool.result_preview) {
                      <div class="detail-section">
                        <span class="detail-label">Result:</span>
                        <span class="detail-value result-preview">{{ detail.tool.result_preview }}</span>
                      </div>
                    }
                    @if (detail.tool.error) {
                      <div class="detail-section">
                        <span class="detail-label">Error:</span>
                        <span class="detail-value error">{{ detail.tool.error }}</span>
                      </div>
                    }
                  }

                  <!-- Check/Routing Details -->
                  @if ((detail.step_type === 'check' || detail.step_type === 'routing' || detail.step_type === 'phase_complete')) {
                    @if (getRoutingData(detail)) {
                      <div class="detail-section">
                        <span class="detail-label">Decision:</span>
                        <span class="detail-value mono">{{ formatJson(getRoutingData(detail)) }}</span>
                      </div>
                    }
                  }

                  <!-- Error Details -->
                  @if (detail.step_type === 'error' && detail.error) {
                    <div class="detail-section">
                      <span class="detail-label">Type:</span>
                      <span class="detail-value mono">{{ detail.error.type }}</span>
                    </div>
                    <div class="detail-section">
                      <span class="detail-label">Message:</span>
                      <span class="detail-value error">{{ detail.error.message }}</span>
                    </div>
                    @if (detail.error.traceback) {
                      <div class="detail-section">
                        <span class="detail-label">Traceback:</span>
                        <pre class="detail-json error">{{ detail.error.traceback }}</pre>
                      </div>
                    } @else if (!isDetailLoaded(entry)) {
                      <div class="detail-section">
                        <span class="detail-label">Traceback:</span>
                        <span class="detail-value pending">Loading details...</span>
                      </div>
                    }
                  }

                  <!-- Phase Info -->
                  @if (detail.phase) {
                    <div class="detail-section">
                      <span class="detail-label">Phase:</span>
                      <span class="detail-value">{{ detail.phase }}@if (detail.phase_number !== undefined) { ({{ detail.phase_number }})}</span>
                    </div>
                  }

                  <!-- Iteration -->
                  <div class="detail-section">
                    <span class="detail-label">Iteration:</span>
                    <span class="detail-value mono">{{ detail.iteration }}</span>
                  </div>
                </div>
              }
            </div>
          }

          <!-- Infinite-scroll footer -->
          @if (trace.loadingMore()) {
            <div class="load-more">
              <app-spinner size="sm" tone="accent" />
              <span>Loading more...</span>
            </div>
          } @else if (trace.hasMore()) {
            <button class="load-more-btn" (click)="trace.loadMore()">
              Load more ({{ trace.total() - trace.rows().length }} remaining)
            </button>
          }
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
        background: var(--panel-bg);
        position: relative;
      }

      /* Filter Bar */
      .filter-bar {
        display: flex;
        gap: 4px;
        padding: 8px;
        background: var(--panel-header-bg);
        border-bottom: 1px solid var(--border-color);
        flex-shrink: 0;
        align-items: center;
      }

      .filter-btn {
        padding: 5px 12px;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-secondary);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .filter-btn:hover {
        background: var(--surface-0);
        color: var(--text-primary);
      }

      .filter-btn.active {
        background: var(--accent-color);
        color: var(--on-accent, var(--timeline-bg));
        border-color: var(--accent-color);
      }

      .sort-btn {
        padding: 5px 10px;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-secondary);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
        white-space: nowrap;
      }

      .sort-btn:hover {
        background: var(--surface-0);
        color: var(--text-primary);
      }

      .entry-count {
        margin-left: auto;
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted);
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

      /* Error State */
      .error-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        color: var(--danger);
        flex: 1;
      }

      .error-state button {
        padding: 8px 16px;
        border: 1px solid var(--danger);
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--danger);
        cursor: pointer;
      }

      .error-state button:hover {
        background: color-mix(in srgb, var(--danger) 10%, transparent);
      }

      /* Empty State */
      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        color: var(--text-muted);
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
        border-left: 3px solid var(--border-color);
        margin-bottom: 4px;
        border-radius: var(--radius-surface);
        background: var(--surface-0);
        transition: all 0.15s ease;
      }

      .entry-item:hover {
        background: var(--panel-header-bg);
      }

      /* Phase-based background tinting */
      .entry-item.phase-strategic {
        background: color-mix(in srgb, var(--accent-color) 6%, transparent);
      }

      .entry-item.phase-strategic:hover {
        background: color-mix(in srgb, var(--accent-color) 10%, transparent);
      }

      .entry-item.phase-tactical {
        background: color-mix(in srgb, var(--success) 6%, transparent);
      }

      .entry-item.phase-tactical:hover {
        background: color-mix(in srgb, var(--success) 10%, transparent);
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
        color: var(--text-muted);
        min-width: 36px;
      }

      .step-badge {
        padding: 2px 6px;
        border-radius: var(--radius-tag);
        font-size: 10px;
        font-weight: 600;
        color: var(--timeline-bg);
        text-transform: uppercase;
      }

      .node-name {
        font-size: 12px;
        color: var(--text-primary);
        flex: 1;
      }

      .latency {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted);
      }

      .timestamp {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted);
      }

      .expand-icon {
        font-size: 10px;
        color: var(--text-muted);
        width: 16px;
        text-align: center;
      }

      /* Entry Details */
      .entry-details {
        padding: 8px 10px 12px 52px;
        border-top: 1px solid var(--border-color);
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
        color: var(--text-muted);
        min-width: 70px;
        flex-shrink: 0;
      }

      .detail-value {
        color: var(--text-primary);
        word-break: break-word;
      }

      .detail-value.mono {
        font-family: 'JetBrains Mono', monospace;
      }

      .detail-value.success {
        color: var(--success);
      }

      .detail-value.failure,
      .detail-value.error {
        color: var(--danger);
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
        border-radius: var(--radius-tag);
        font-size: 11px;
        line-height: 1.4;
      }

      .detail-json {
        margin: 0;
        padding: 6px 8px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: var(--radius-tag);
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--success);
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
        border-radius: var(--radius-tag);
        background: var(--accent-color);
        color: var(--on-accent, var(--timeline-bg));
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
      }

      /* Infinite-scroll footer */
      .load-more {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 12px;
        font-size: 12px;
        color: var(--text-muted);
      }

      .load-more-btn {
        display: block;
        width: 100%;
        padding: 8px;
        margin-top: 4px;
        border: 1px solid var(--border-color);
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-secondary);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .load-more-btn:hover {
        background: var(--surface-0);
        color: var(--text-primary);
      }

      .request-link {
        color: var(--cat-6);
        cursor: pointer;
        text-decoration: underline;
        text-decoration-style: dotted;
        text-underline-offset: 2px;
      }

      .request-link:hover {
        color: color-mix(in srgb, var(--cat-6) 78%, var(--text-primary));
        text-decoration-style: solid;
      }

      .pending-indicator {
        font-size: 10px;
        color: var(--warning);
        font-style: italic;
        animation: pulse 1.5s ease-in-out infinite;
      }

      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.5; }
      }

      .detail-value.pending {
        color: var(--warning);
        font-style: italic;
      }
    `,
  ],
})
export class AgentActivityComponent {
  readonly trace = inject(AuditTraceService);
  private readonly data = inject(DataService);
  private readonly requestService = inject(RequestService);

  // Local expanded state (which rows are open) + which have had detail fetched.
  private readonly expandedIds = signal<Set<string>>(new Set());
  private readonly detailLoadedIds = signal<Set<string>>(new Set());

  constructor() {
    // Drive the trace off the loaded job. DataService.currentJobId is the signal
    // the workbench actually sets on selection (the timeline's dropdown ->
    // data.setCurrentJob -> loadJob); jobContext.activeJobId is not populated here.
    effect(() => {
      const jobId = this.data.currentJobId();
      untracked(() => this.trace.setJob(jobId));
    });
  }

  readonly filters: { label: string; value: AuditFilterCategory }[] = [
    { label: 'All', value: 'all' },
    { label: 'Messages', value: 'messages' },
    { label: 'Tools', value: 'tools' },
    { label: 'Errors', value: 'errors' },
  ];

  /** Near the bottom → fetch the next page (infinite scroll). */
  onScroll(event: Event): void {
    const el = event.target as HTMLElement;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 240) {
      void this.trace.loadMore();
    }
  }

  // Step type color mapping (fallback for non-tool steps)
  private readonly stepColors: Record<AuditStepType, string> = {
    initialize: 'var(--cat-6)',      // lapis  (was blue)
    llm: 'var(--cat-4)',             // olive  (was green)
    tool: 'var(--cat-7)',            // violet (was purple)
    check: 'var(--cat-2)',           // copper (was peach)
    routing: 'var(--cat-5)',         // slate-teal (was teal)
    phase_complete: 'var(--cat-3)',  // gold   (was sapphire)
    error: 'var(--danger)',          // semantic
  };

  // Tool category color mapping (Imperial ramp)
  private readonly toolCategoryColors: Record<string, string> = {
    workspace: 'var(--cat-6)',      // lapis  (was blue)
    core: 'var(--cat-7)',           // violet (was purple)
    research: 'var(--cat-5)',       // slate-teal (was teal)
    citation: 'var(--cat-3)',       // gold   (was yellow)
    graph: 'var(--cat-8)',          // mauve  (was pink)
    communication: 'var(--cat-4)',  // olive  (was green)
    delegation: 'var(--cat-2)',     // copper (was peach)
  };

  // Map tool names to their categories
  private readonly toolCategories: Record<string, string> = {
    // Workspace tools
    read_file: 'workspace',
    write_file: 'workspace',
    edit_file: 'workspace',
    list_files: 'workspace',
    delete_file: 'workspace',
    search_files: 'workspace',
    file_exists: 'workspace',
    move_file: 'workspace',
    rename_file: 'workspace',
    copy_file: 'workspace',
    get_document_info: 'workspace',
    // Core tools
    next_phase_todos: 'core',
    todo_complete: 'core',
    todo_list: 'core',
    todo_rewind: 'core',
    mark_complete: 'core',
    job_complete: 'core',
    // Research tools
    web_search: 'research',
    // Citation tools
    cite_document: 'citation',
    cite_web: 'citation',
    list_sources: 'citation',
    get_citation: 'citation',
    list_citations: 'citation',
    edit_citation: 'citation',
    // Graph tools (Neo4j datasource)
    cypher_query: 'graph',
    cypher_execute: 'graph',
    get_database_schema: 'graph',
    // SQL tools (PostgreSQL datasource)
    sql_query: 'sql',
    sql_schema: 'sql',
    sql_execute: 'sql',
    // MongoDB tools (MongoDB datasource)
    mongo_query: 'mongodb',
    mongo_aggregate: 'mongodb',
    mongo_schema: 'mongodb',
    mongo_insert: 'mongodb',
    mongo_update: 'mongodb',
    // Communication tools
    send_message: 'communication',
    // Delegation tools
    delegate_work: 'delegation',
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

  toggleExpanded(entry: AuditEntry): void {
    const entryId = entry._id;
    const current = this.expandedIds();
    const updated = new Set(current);

    if (updated.has(entryId)) {
      updated.delete(entryId);
    } else {
      updated.add(entryId);
      // Lazy-load the heavy detail (arguments / traceback / state) on first open.
      void this.trace.loadDetail(entry).then(() => {
        this.detailLoadedIds.update((s) => new Set(s).add(entryId));
      });
    }

    this.expandedIds.set(updated);
  }

  isExpanded(entryId: string): boolean {
    return this.expandedIds().has(entryId);
  }

  isDetailLoaded(entry: AuditEntry): boolean {
    return this.detailLoadedIds().has(entry._id);
  }

  getStepColor(stepType: AuditStepType, entry?: AuditEntry): string {
    // For tool steps, use category-based coloring
    if (stepType === 'tool' && entry?.tool?.name) {
      const category = this.toolCategories[entry.tool.name];
      if (category && this.toolCategoryColors[category]) {
        return this.toolCategoryColors[category];
      }
    }
    return this.stepColors[stepType] || 'var(--text-muted)';
  }

  getToolCategory(toolName: string): string | undefined {
    return this.toolCategories[toolName];
  }

  getToolColor(toolName: string): string {
    const category = this.toolCategories[toolName];
    if (category && this.toolCategoryColors[category]) {
      return this.toolCategoryColors[category];
    }
    return this.stepColors.tool; // Default purple for unknown tools
  }

  getStepBadge(stepType: AuditStepType, entry?: AuditEntry): string {
    // For tool steps, show the category as the badge
    if (stepType === 'tool' && entry?.tool?.name) {
      const category = this.toolCategories[entry.tool.name];
      if (category) {
        // Abbreviate category names for badge display
        const categoryBadges: Record<string, string> = {
          workspace: 'FILE',
          core: 'CORE',
          document: 'DOC',
          research: 'WEB',
          citation: 'CITE',
          graph: 'GRAPH',
        };
        return categoryBadges[category] || 'TOOL';
      }
    }
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
    return date.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
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
      '_id', 'id', 'job_id', 'step_number', 'step_type', 'node_name', 'timestamp',
      'latency_ms', 'iteration', 'phase', 'phase_number', 'metadata', 'agent_type',
      'llm', 'tool', 'error', 'state', 'started_at', 'completed_at',
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
