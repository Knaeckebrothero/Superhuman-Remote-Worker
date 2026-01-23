import { Component, inject, OnInit, computed } from '@angular/core';
import { StateService } from '../../core/services/state.service';
import { ColumnDef } from '../../core/models/api.model';

/**
 * Database table viewer component.
 * Displays PostgreSQL tables with pagination.
 */
@Component({
  selector: 'app-db-table',
  standalone: true,
  template: `
    <div class="db-table-container">
      <!-- Table Selector -->
      <div class="table-selector">
        @for (tableName of availableTables; track tableName) {
          <button
            class="table-tab"
            [class.active]="tableName === state.selectedTable()"
            (click)="state.selectTable(tableName)"
          >
            {{ tableName }}
            @if (getRowCount(tableName) !== null) {
              <span class="row-count">{{ getRowCount(tableName) }}</span>
            }
          </button>
        }
        <button class="refresh-btn" (click)="state.refresh()" [disabled]="state.isLoading()">
          &#x21bb;
        </button>
      </div>

      <!-- Loading Overlay -->
      @if (state.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
        </div>
      }

      <!-- Error State -->
      @if (state.error()) {
        <div class="error-state">
          <span>{{ state.error() }}</span>
          <button (click)="state.refresh()">Retry</button>
        </div>
      }

      <!-- Empty State -->
      @if (!state.isLoading() && !state.error() && state.tableData().length === 0) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4C2;</span>
          <span>No data in {{ state.selectedTable() }}</span>
          <span class="empty-hint">Start the backend: uvicorn main:app --port 8080</span>
        </div>
      }

      <!-- Data Table -->
      @if (state.tableData().length > 0) {
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                @for (col of state.columns(); track col.name) {
                  <th [class]="'col-' + col.type">{{ col.name }}</th>
                }
              </tr>
            </thead>
            <tbody>
              @for (row of state.tableData(); track $index) {
                <tr>
                  @for (col of state.columns(); track col.name) {
                    <td [class]="'col-' + col.type">
                      {{ formatCell(row[col.name], col) }}
                    </td>
                  }
                </tr>
              }
            </tbody>
          </table>
        </div>
      }

      <!-- Pagination -->
      @if (state.pagination().total > 0) {
        <div class="pagination">
          <span class="page-info">
            Showing {{ state.pageRange().start }}-{{ state.pageRange().end }}
            of {{ state.pageRange().total }}
          </span>
          <div class="page-controls">
            <button
              class="page-btn"
              (click)="state.previousPage()"
              [disabled]="!state.hasPreviousPage()"
            >
              &lt; Prev
            </button>
            <span class="page-number">
              Page {{ state.pagination().page }} of {{ state.totalPages() }}
            </span>
            <button
              class="page-btn"
              (click)="state.nextPage()"
              [disabled]="!state.hasNextPage()"
            >
              Next &gt;
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

      .db-table-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
        position: relative;
      }

      /* Table Selector */
      .table-selector {
        display: flex;
        gap: 4px;
        padding: 8px;
        background: var(--surface-0, #313244);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .table-tab {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .table-tab:hover {
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-primary, #cdd6f4);
      }

      .table-tab.active {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
      }

      .row-count {
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 10px;
        background: rgba(0, 0, 0, 0.2);
      }

      .table-tab.active .row-count {
        background: rgba(0, 0, 0, 0.3);
      }

      .refresh-btn {
        margin-left: auto;
        padding: 6px 10px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 14px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      /* Loading Overlay */
      .loading-overlay {
        position: absolute;
        top: 48px;
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

      /* Table Wrapper */
      .table-wrapper {
        flex: 1;
        overflow: auto;
      }

      /* Data Table */
      .data-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }

      .data-table th,
      .data-table td {
        padding: 8px 12px;
        text-align: left;
        border-bottom: 1px solid var(--border-color, #313244);
        max-width: 300px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .data-table th {
        position: sticky;
        top: 0;
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-secondary, #a6adc8);
        font-weight: 600;
        text-transform: uppercase;
        font-size: 11px;
        letter-spacing: 0.5px;
        z-index: 1;
      }

      .data-table td {
        color: var(--text-primary, #cdd6f4);
      }

      .data-table tbody tr:hover {
        background: var(--surface-0, #313244);
      }

      /* Column type styling */
      .col-string {
        font-family: inherit;
      }

      .col-number {
        font-family: 'JetBrains Mono', monospace;
        text-align: right;
      }

      .col-boolean {
        text-align: center;
      }

      .col-date {
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .col-json {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #a6e3a1;
        max-width: 200px;
      }

      .col-binary {
        font-style: italic;
        color: var(--text-muted, #6c7086);
      }

      /* Pagination */
      .pagination {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .page-info {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
      }

      .page-controls {
        display: flex;
        align-items: center;
        gap: 12px;
      }

      .page-btn {
        padding: 4px 12px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .page-btn:hover:not(:disabled) {
        background: var(--panel-header-bg, #1e1e2e);
        color: var(--text-primary, #cdd6f4);
        border-color: var(--text-muted, #6c7086);
      }

      .page-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .page-number {
        font-size: 12px;
        color: var(--text-secondary, #a6adc8);
      }
    `,
  ],
})
export class DbTableComponent implements OnInit {
  readonly state = inject(StateService);

  // Hardcoded table names - always show these tabs
  readonly availableTables = ['jobs', 'requirements', 'sources', 'citations'];

  ngOnInit(): void {
    this.state.loadTables();
    this.state.loadTableData();
  }

  /**
   * Get row count for a table from the loaded tables info.
   */
  getRowCount(tableName: string): number | null {
    const table = this.state.tables().find((t) => t.name === tableName);
    return table ? table.rowCount : null;
  }

  /**
   * Format a cell value based on column type.
   */
  formatCell(value: unknown, col: ColumnDef): string {
    if (value === null || value === undefined) {
      return '-';
    }

    switch (col.type) {
      case 'date':
        return this.formatDate(value);
      case 'json':
        return this.formatJson(value);
      case 'boolean':
        return value ? 'Yes' : 'No';
      case 'binary':
        return String(value);
      default:
        return this.truncate(String(value), 100);
    }
  }

  private formatDate(value: unknown): string {
    if (!value) return '-';
    const date = new Date(String(value));
    if (isNaN(date.getTime())) return String(value);
    return date.toLocaleString('en-GB', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  private formatJson(value: unknown): string {
    if (typeof value === 'string') {
      return this.truncate(value, 50);
    }
    try {
      return this.truncate(JSON.stringify(value), 50);
    } catch {
      return '[object]';
    }
  }

  private truncate(str: string, maxLen: number): string {
    if (str.length <= maxLen) return str;
    return str.slice(0, maxLen - 3) + '...';
  }
}
