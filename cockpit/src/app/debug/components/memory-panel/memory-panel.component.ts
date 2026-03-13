import {
  Component,
  inject,
  signal,
  computed,
  effect,
} from '@angular/core';
import { ApiService } from '../../../core/services/api.service';
import { DataService } from '../../../core/services/data.service';
import {
  Memory,
  MemoryStats,
  MemoryType,
  MemorySource,
  MemorySortField,
} from '../../../core/models/api.model';

@Component({
  selector: 'app-memory-panel',
  standalone: true,
  template: `
    <div class="memory-container">
      <!-- Header -->
      <div class="header-bar">
        <div class="tab-group">
          <button
            class="tab-btn"
            [class.active]="activeTab() === 'stats'"
            (click)="activeTab.set('stats')"
          >
            Stats
          </button>
          <button
            class="tab-btn"
            [class.active]="activeTab() === 'browser'"
            (click)="activeTab.set('browser')"
          >
            Browser
          </button>
        </div>
        <button class="refresh-btn" (click)="refresh()" title="Refresh">
          <span class="material-symbols-outlined" style="font-size: 16px">refresh</span>
        </button>
      </div>

      <!-- No Job Selected -->
      @if (!data.currentJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1f9e0;</span>
          <span>Select a job to view memories</span>
          <span class="empty-hint">Use the timeline above to select a job</span>
        </div>
      } @else {
        <!-- Loading -->
        @if (isLoading()) {
          <div class="loading-overlay">
            <div class="spinner"></div>
          </div>
        }

        <!-- Error -->
        @if (error()) {
          <div class="error-state">
            <span>{{ error() }}</span>
            <button (click)="refresh()">Retry</button>
          </div>
        }

        <!-- Stats Tab -->
        @if (activeTab() === 'stats') {
          @if (stats(); as s) {
            <div class="stats-content">
              <!-- Summary Row -->
              <div class="stats-section">
                <div class="section-label">Summary</div>
                <div class="metrics-grid">
                  <div class="metric-card">
                    <div class="metric-value">{{ s.total }}</div>
                    <div class="metric-label">Total</div>
                  </div>
                  <div class="metric-card">
                    <div class="metric-value">{{ formatNumber(s.total_tokens) }}</div>
                    <div class="metric-label">Tokens</div>
                  </div>
                  <div class="metric-card">
                    <div class="metric-value">{{ s.total_accesses }}</div>
                    <div class="metric-label">Accesses</div>
                  </div>
                  <div class="metric-card">
                    <div class="metric-value">{{ s.avg_importance !== null ? s.avg_importance.toFixed(2) : '--' }}</div>
                    <div class="metric-label">Avg Importance</div>
                  </div>
                </div>
              </div>

              <!-- By Type -->
              <div class="stats-section">
                <div class="section-label">By Type</div>
                <div class="metrics-grid">
                  @for (t of typeEntries; track t.key) {
                    <div class="metric-card">
                      <div class="metric-value" [style.color]="typeColors[t.type]">
                        {{ s[t.key] }}
                      </div>
                      <div class="metric-label">{{ t.label }}</div>
                    </div>
                  }
                </div>
              </div>

              <!-- By Source -->
              <div class="stats-section">
                <div class="section-label">By Source</div>
                <div class="metrics-grid">
                  @for (src of sourceEntries; track src.key) {
                    <div class="metric-card">
                      <div class="metric-value" [style.color]="sourceColorMap[src.source]">
                        {{ s[src.key] }}
                      </div>
                      <div class="metric-label">{{ src.label }}</div>
                    </div>
                  }
                </div>
              </div>
            </div>
          } @else if (!isLoading()) {
            <div class="empty-state">
              <span class="empty-icon">&#x1f4ca;</span>
              <span>No memory data available</span>
              <span class="empty-hint">This job may not have memory enabled</span>
            </div>
          }
        }

        <!-- Browser Tab -->
        @if (activeTab() === 'browser') {
          <div class="filter-bar">
            <button
              class="filter-btn"
              [class.active]="activeType() === null"
              (click)="setTypeFilter(null)"
            >
              All
            </button>
            @for (t of typeFilters; track t.value) {
              <button
                class="filter-btn"
                [class.active]="activeType() === t.value"
                (click)="setTypeFilter(t.value)"
                [style.border-color]="activeType() === t.value ? typeColors[t.value] : ''"
                [style.background]="activeType() === t.value ? typeColors[t.value] : ''"
              >
                {{ t.label }}
              </button>
            }

            <select
              class="inline-select"
              [value]="activeSource() ?? ''"
              (change)="setSourceFilter(asSelectValue($event))"
            >
              <option value="">All Sources</option>
              @for (src of sourceFilters; track src.value) {
                <option [value]="src.value">{{ src.label }}</option>
              }
            </select>

            <select
              class="inline-select"
              [value]="sortBy()"
              (change)="setSortBy(asSelectValue($event))"
            >
              <option value="created_at">Newest</option>
              <option value="importance">Importance</option>
              <option value="access_count">Most Accessed</option>
              <option value="token_count">Largest</option>
              <option value="last_accessed">Recently Used</option>
            </select>

            <span class="entry-count">{{ memories().length }} / {{ total() }}</span>
          </div>

          @if (memories().length === 0 && !isLoading()) {
            <div class="empty-state">
              <span class="empty-icon">&#x1f50d;</span>
              <span>No memories found</span>
              <span class="empty-hint">Try adjusting filters or select a different job</span>
            </div>
          } @else {
            <div class="memory-list">
              @for (mem of memories(); track mem.id) {
                <div class="memory-item">
                  <div class="memory-header" (click)="toggleExpanded(mem.id)">
                    <span
                      class="type-badge"
                      [style.background]="typeColors[mem.memory_type] + '33'"
                      [style.color]="typeColors[mem.memory_type]"
                    >
                      {{ formatType(mem.memory_type) }}
                    </span>
                    <span class="source-tag">{{ mem.source }}</span>
                    <span class="memory-summary">
                      {{ mem.summary || truncate(mem.content_preview, 80) }}
                    </span>
                    <span class="importance-value" [style.color]="importanceColor(mem.importance)">
                      {{ mem.importance.toFixed(2) }}
                    </span>
                    <span class="memory-time">{{ formatTime(mem.created_at) }}</span>
                    <span class="expand-icon">{{ isExpanded(mem.id) ? '&#x25BC;' : '&#x25B6;' }}</span>
                  </div>

                  @if (isExpanded(mem.id)) {
                    <div class="memory-details">
                      <div class="detail-section">
                        <div class="detail-label">Content</div>
                        <div class="detail-content">{{ mem.content_preview }}</div>
                      </div>

                      @if (mem.keywords && mem.keywords.length > 0) {
                        <div class="detail-section">
                          <div class="detail-label">Keywords</div>
                          <div class="keyword-list">
                            @for (kw of mem.keywords; track kw) {
                              <span class="keyword-chip">{{ kw }}</span>
                            }
                          </div>
                        </div>
                      }

                      <div class="detail-row">
                        @if (mem.source_phase !== null && mem.source_phase !== undefined) {
                          <span class="detail-item">Phase {{ mem.source_phase }}</span>
                        }
                        @if (mem.source_turn_start !== null && mem.source_turn_start !== undefined) {
                          <span class="detail-item">
                            Turns {{ mem.source_turn_start }}{{ mem.source_turn_end !== null && mem.source_turn_end !== undefined ? '-' + mem.source_turn_end : '' }}
                          </span>
                        }
                        <span class="detail-item">{{ mem.token_count }} tokens</span>
                        <span class="detail-item">{{ mem.access_count }} accesses</span>
                        <span class="detail-item">Last used: {{ formatDate(mem.last_accessed) }}</span>
                      </div>
                    </div>
                  }
                </div>
              }
            </div>

            <!-- Pagination -->
            @if (totalPages() > 1) {
              <div class="pagination">
                <span class="page-info">
                  Page {{ page() }} of {{ totalPages() }} ({{ total() }} total)
                </span>
                <div class="page-controls">
                  <button
                    class="page-btn"
                    (click)="setPage(page() - 1)"
                    [disabled]="page() <= 1"
                  >
                    &lt; Prev
                  </button>
                  <button
                    class="page-btn"
                    (click)="setPage(page() + 1)"
                    [disabled]="page() >= totalPages()"
                  >
                    Next &gt;
                  </button>
                </div>
              </div>
            }
          }
        }
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

      .memory-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .tab-group {
        display: flex;
        gap: 2px;
      }

      .tab-btn {
        padding: 4px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .tab-btn:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .tab-btn.active {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-color: var(--accent-color, #cba6f7);
      }

      .refresh-btn {
        margin-left: auto;
        padding: 4px 8px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        cursor: pointer;
        display: flex;
        align-items: center;
      }

      .refresh-btn:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      /* States */
      .loading-overlay {
        position: absolute;
        top: 40px;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(17, 17, 27, 0.8);
        display: flex;
        align-items: center;
        justify-content: center;
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

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

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

      /* Stats Tab */
      .stats-content {
        flex: 1;
        overflow: auto;
        padding: 12px;
      }

      .stats-section {
        margin-bottom: 16px;
      }

      .section-label {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted, #6c7086);
        margin-bottom: 8px;
        padding-left: 4px;
      }

      .metrics-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
        gap: 8px;
      }

      .metric-card {
        background: var(--surface-0, #313244);
        border-radius: 6px;
        padding: 12px 10px;
        text-align: center;
      }

      .metric-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 20px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
        line-height: 1.2;
      }

      .metric-label {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
        margin-top: 4px;
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
        flex-wrap: wrap;
      }

      .filter-btn {
        padding: 4px 10px;
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
        color: var(--timeline-bg, #11111b);
        border-color: transparent;
      }

      .inline-select {
        padding: 4px 8px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: var(--panel-bg, #181825);
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
      }

      .inline-select:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .entry-count {
        margin-left: auto;
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      /* Memory List */
      .memory-list {
        flex: 1;
        overflow: auto;
        padding: 8px;
      }

      .memory-item {
        border-left: 3px solid var(--border-color, #45475a);
        margin-bottom: 4px;
        border-radius: 4px;
        background: var(--surface-0, #313244);
        transition: all 0.15s ease;
      }

      .memory-item:hover {
        background: var(--panel-header-bg, #1e1e2e);
      }

      .memory-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        cursor: pointer;
        user-select: none;
      }

      .type-badge {
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        font-weight: 600;
        font-family: 'JetBrains Mono', monospace;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
        flex-shrink: 0;
      }

      .source-tag {
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
        white-space: nowrap;
        flex-shrink: 0;
      }

      .memory-summary {
        flex: 1;
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
      }

      .importance-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        flex-shrink: 0;
      }

      .memory-time {
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
      }

      .expand-icon {
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
        width: 12px;
        text-align: center;
      }

      /* Expanded Details */
      .memory-details {
        padding: 8px 10px 12px 52px;
        border-top: 1px solid var(--border-color, #313244);
        background: rgba(17, 17, 27, 0.3);
      }

      .detail-section {
        margin-bottom: 10px;
      }

      .detail-label {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
        margin-bottom: 4px;
      }

      .detail-content {
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'JetBrains Mono', monospace;
        background: var(--panel-bg, #181825);
        padding: 8px;
        border-radius: 4px;
        max-height: 200px;
        overflow: auto;
        line-height: 1.5;
      }

      .keyword-list {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }

      .keyword-chip {
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--surface-0, #313244);
        color: var(--text-secondary, #a6adc8);
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
      }

      .detail-row {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 8px;
      }

      .detail-item {
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      /* Pagination */
      .pagination {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        border-top: 1px solid var(--border-color, #313244);
        background: var(--panel-header-bg, #1e1e2e);
        flex-shrink: 0;
      }

      .page-info {
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .page-controls {
        display: flex;
        gap: 4px;
      }

      .page-btn {
        padding: 4px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .page-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .page-btn:disabled {
        opacity: 0.4;
        cursor: default;
      }
    `,
  ],
})
export class MemoryPanelComponent {
  private readonly api = inject(ApiService);
  readonly data = inject(DataService);

  // Data signals
  readonly stats = signal<MemoryStats | null>(null);
  readonly memories = signal<Memory[]>([]);
  readonly total = signal(0);
  readonly isLoading = signal(false);
  readonly error = signal<string | null>(null);

  // Filter signals
  readonly activeType = signal<MemoryType | null>(null);
  readonly activeSource = signal<MemorySource | null>(null);
  readonly sortBy = signal<MemorySortField>('created_at');
  readonly page = signal(1);
  readonly pageSize = 25;

  // UI signals
  readonly expandedIds = signal<Set<string>>(new Set());
  readonly activeTab = signal<'stats' | 'browser'>('browser');

  readonly totalPages = computed(() => Math.max(1, Math.ceil(this.total() / this.pageSize)));

  // Color mappings
  readonly typeColors: Record<MemoryType, string> = {
    factual: '#89b4fa',
    procedural: '#a6e3a1',
    error_solution: '#f38ba8',
    vocabulary: '#f9e2af',
    relational: '#cba6f7',
  };

  readonly sourceColorMap: Record<MemorySource, string> = {
    observer: '#94e2d5',
    todo: '#89b4fa',
    compaction: '#fab387',
    phase_archive: '#f5c2e7',
    tool_error: '#f38ba8',
  };

  // Static data for template iteration
  readonly typeEntries: { key: keyof MemoryStats; type: MemoryType; label: string }[] = [
    { key: 'factual', type: 'factual', label: 'Factual' },
    { key: 'procedural', type: 'procedural', label: 'Procedural' },
    { key: 'error_solution', type: 'error_solution', label: 'Error' },
    { key: 'vocabulary', type: 'vocabulary', label: 'Vocabulary' },
    { key: 'relational', type: 'relational', label: 'Relational' },
  ];

  readonly sourceEntries: { key: keyof MemoryStats; source: MemorySource; label: string }[] = [
    { key: 'from_observer', source: 'observer', label: 'Observer' },
    { key: 'from_todo', source: 'todo', label: 'Todo' },
    { key: 'from_compaction', source: 'compaction', label: 'Compaction' },
    { key: 'from_phase_archive', source: 'phase_archive', label: 'Phase Archive' },
    { key: 'from_tool_error', source: 'tool_error', label: 'Tool Error' },
  ];

  readonly typeFilters: { value: MemoryType; label: string }[] = [
    { value: 'factual', label: 'Factual' },
    { value: 'procedural', label: 'Procedural' },
    { value: 'error_solution', label: 'Error' },
    { value: 'vocabulary', label: 'Vocab' },
    { value: 'relational', label: 'Relational' },
  ];

  readonly sourceFilters: { value: MemorySource; label: string }[] = [
    { value: 'observer', label: 'Observer' },
    { value: 'todo', label: 'Todo' },
    { value: 'compaction', label: 'Compaction' },
    { value: 'phase_archive', label: 'Phase Archive' },
    { value: 'tool_error', label: 'Tool Error' },
  ];

  private lastJobId: string | null = null;

  constructor() {
    effect(() => {
      const jobId = this.data.currentJobId();
      if (jobId && jobId !== this.lastJobId) {
        this.lastJobId = jobId;
        this.resetFilters();
        this.loadStats(jobId);
        this.loadMemories(jobId);
      }
    });
  }

  // ===== Actions =====

  refresh(): void {
    const jobId = this.data.currentJobId();
    if (jobId) {
      this.loadStats(jobId);
      this.loadMemories(jobId);
    }
  }

  setTypeFilter(type: MemoryType | null): void {
    this.activeType.set(type);
    this.page.set(1);
    this.reloadMemories();
  }

  setSourceFilter(value: string): void {
    this.activeSource.set(value ? (value as MemorySource) : null);
    this.page.set(1);
    this.reloadMemories();
  }

  setSortBy(value: string): void {
    this.sortBy.set(value as MemorySortField);
    this.page.set(1);
    this.reloadMemories();
  }

  setPage(p: number): void {
    if (p >= 1 && p <= this.totalPages()) {
      this.page.set(p);
      this.reloadMemories();
    }
  }

  toggleExpanded(id: string): void {
    const current = new Set(this.expandedIds());
    if (current.has(id)) {
      current.delete(id);
    } else {
      current.add(id);
    }
    this.expandedIds.set(current);
  }

  isExpanded(id: string): boolean {
    return this.expandedIds().has(id);
  }

  // ===== Helpers =====

  asSelectValue(event: Event): string {
    return (event.target as HTMLSelectElement).value;
  }

  truncate(text: string, max: number): string {
    if (!text) return '';
    return text.length > max ? text.substring(0, max) + '...' : text;
  }

  formatType(type: string): string {
    return type.replace('_', ' ');
  }

  formatNumber(n: number): string {
    return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n.toString();
  }

  formatTime(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  formatDate(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }

  importanceColor(value: number): string {
    if (value >= 0.8) return '#a6e3a1';
    if (value >= 0.5) return '#f9e2af';
    return '#f38ba8';
  }

  // ===== Data Loading =====

  private resetFilters(): void {
    this.activeType.set(null);
    this.activeSource.set(null);
    this.sortBy.set('created_at');
    this.page.set(1);
    this.expandedIds.set(new Set());
    this.error.set(null);
  }

  private reloadMemories(): void {
    const jobId = this.data.currentJobId();
    if (jobId) {
      this.loadMemories(jobId);
    }
  }

  private loadStats(jobId: string): void {
    this.api.getMemoryStats(jobId).subscribe((data) => {
      this.stats.set(data);
    });
  }

  private loadMemories(jobId: string): void {
    this.isLoading.set(true);
    this.error.set(null);

    const filters: Record<string, string | number> = {
      sort_by: this.sortBy(),
      sort_order: 'desc',
      limit: this.pageSize,
      offset: (this.page() - 1) * this.pageSize,
    };
    const type = this.activeType();
    if (type) filters['memory_type'] = type;
    const source = this.activeSource();
    if (source) filters['source'] = source;

    this.api.getMemories(jobId, filters as any).subscribe({
      next: (resp) => {
        this.isLoading.set(false);
        if (resp) {
          this.memories.set(resp.memories);
          this.total.set(resp.total);
        } else {
          this.memories.set([]);
          this.total.set(0);
        }
      },
      error: (err) => {
        this.isLoading.set(false);
        this.error.set(err.message || 'Failed to load memories');
      },
    });
  }
}
