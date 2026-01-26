import { Component, inject, effect, OnInit } from '@angular/core';
import { TodoService } from '../../core/services/todo.service';
import { AuditService } from '../../core/services/audit.service';
import { TodoItem } from '../../core/models/todo.model';

/**
 * Todo list component that displays current and archived todos.
 * Shows todos from workspace files for the selected job.
 */
@Component({
  selector: 'app-todo-list',
  standalone: true,
  template: `
    <div class="todo-container">
      <!-- Header with phase selector -->
      <div class="header">
        <select
          class="phase-selector"
          [value]="todo.selectedArchive() || 'current'"
          (change)="onPhaseSelect($event)"
        >
          <option value="current" [disabled]="!todo.currentTodos()">
            Current Phase
            @if (todo.currentTodos()) {
              ({{ todo.currentTodos()!.todos.length }} todos)
            }
          </option>
          @if (todo.archives().length > 0) {
            <optgroup label="Archived Phases">
              @for (archive of todo.archives(); track archive.filename) {
                <option [value]="archive.filename">
                  {{ archive.phase_name || 'Unnamed Phase' }}
                  @if (archive.timestamp) {
                    - {{ formatArchiveDate(archive.timestamp) }}
                  }
                </option>
              }
            </optgroup>
          }
        </select>
        <button
          class="refresh-btn"
          (click)="todo.refresh()"
          [disabled]="todo.isLoading()"
          title="Refresh todos"
        >
          &#x21bb;
        </button>
      </div>

      <!-- Progress bar -->
      @if (todo.displayTodos().length > 0) {
        <div class="progress-section">
          <div class="progress-bar">
            <div
              class="progress-fill"
              [style.width.%]="todo.progress().percentage"
            ></div>
          </div>
          <span class="progress-text">
            {{ todo.progress().completed }}/{{ todo.progress().total }}
            ({{ todo.progress().percentage }}%)
          </span>
        </div>
      }

      <!-- Failure note (for failed archives) -->
      @if (todo.failureNote()) {
        <div class="failure-note">
          <span class="failure-icon">&#x26A0;</span>
          <span class="failure-text">{{ todo.failureNote() }}</span>
        </div>
      }

      <!-- Loading state -->
      @if (todo.isLoading()) {
        <div class="loading-overlay">
          <div class="spinner"></div>
        </div>
      }

      <!-- Error state -->
      @if (todo.error()) {
        <div class="error-state">
          <span>{{ todo.error() }}</span>
          <button (click)="todo.refresh()">Retry</button>
        </div>
      }

      <!-- No workspace -->
      @if (!todo.hasWorkspace() && !todo.isLoading() && audit.selectedJobId()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4C1;</span>
          <span>No workspace found</span>
          <span class="empty-hint">Job workspace may not exist yet</span>
        </div>
      }

      <!-- No job selected -->
      @if (!audit.selectedJobId() && !todo.isLoading()) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F50D;</span>
          <span>Select a job from the timeline bar</span>
          <span class="empty-hint">Todos will appear here</span>
        </div>
      }

      <!-- Empty todos -->
      @if (
        todo.hasWorkspace() &&
        !todo.isLoading() &&
        !todo.error() &&
        todo.displayTodos().length === 0 &&
        audit.selectedJobId()
      ) {
        <div class="empty-state">
          <span class="empty-icon">&#x2705;</span>
          <span>No todos in this phase</span>
          <span class="empty-hint">
            @if (todo.isShowingCurrent()) {
              The agent may be in strategic planning
            } @else {
              This archive has no recorded todos
            }
          </span>
        </div>
      }

      <!-- Todo list -->
      @if (todo.displayTodos().length > 0) {
        <div class="todo-list">
          @for (item of todo.displayTodos(); track item.content; let i = $index) {
            <div
              class="todo-item"
              [class.completed]="item.status === 'completed'"
              [class.in-progress]="item.status === 'in_progress'"
              [class.pending]="item.status === 'pending'"
              [class.high-priority]="item.priority === 'high'"
            >
              <span class="todo-status">
                @switch (item.status) {
                  @case ('completed') { &#x2705; }
                  @case ('in_progress') { &#x1F504; }
                  @case ('pending') { &#x23F3; }
                }
              </span>
              <div class="todo-content">
                <span class="todo-text">{{ item.content }}</span>
                @if (item.notes && item.notes.length > 0) {
                  <div class="todo-notes">
                    @for (note of item.notes; track note) {
                      <span class="note">{{ note }}</span>
                    }
                  </div>
                }
              </div>
              @if (item.priority === 'high') {
                <span class="priority-badge">HIGH</span>
              }
            </div>
          }
        </div>
      }

      <!-- Summary (for archives) -->
      @if (todo.selectedArchiveData()?.summary; as summary) {
        <div class="summary-bar">
          <span class="summary-item">
            Total: {{ summary.total || todo.displayTodos().length }}
          </span>
          <span class="summary-item completed">
            Completed: {{ summary.completed || 0 }}
          </span>
          <span class="summary-item pending">
            Remaining: {{ summary.not_completed || 0 }}
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

    .todo-container {
      display: flex;
      flex-direction: column;
      height: 100%;
      background: var(--panel-bg, #181825);
      position: relative;
    }

    /* Header */
    .header {
      display: flex;
      gap: 8px;
      padding: 8px;
      background: var(--surface-0, #313244);
      border-bottom: 1px solid var(--border-color, #313244);
      flex-shrink: 0;
    }

    .phase-selector {
      flex: 1;
      padding: 6px 10px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      background: var(--panel-bg, #181825);
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      font-family: 'JetBrains Mono', monospace;
      cursor: pointer;
    }

    .phase-selector:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }

    .phase-selector optgroup {
      color: var(--text-muted, #6c7086);
    }

    .refresh-btn {
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

    /* Progress section */
    .progress-section {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 12px;
      background: var(--panel-header-bg, #1e1e2e);
      border-bottom: 1px solid var(--border-color, #313244);
      flex-shrink: 0;
    }

    .progress-bar {
      flex: 1;
      height: 6px;
      background: var(--surface-0, #313244);
      border-radius: 3px;
      overflow: hidden;
    }

    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, #a6e3a1, #94e2d5);
      border-radius: 3px;
      transition: width 0.3s ease;
    }

    .progress-text {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      min-width: 80px;
      text-align: right;
    }

    /* Failure note */
    .failure-note {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 8px 12px;
      background: rgba(243, 139, 168, 0.1);
      border-bottom: 1px solid rgba(243, 139, 168, 0.2);
      flex-shrink: 0;
    }

    .failure-icon {
      color: #f38ba8;
      font-size: 14px;
    }

    .failure-text {
      font-size: 12px;
      color: #f38ba8;
      line-height: 1.4;
    }

    /* Loading overlay */
    .loading-overlay {
      position: absolute;
      top: 50px;
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

    .error-state button {
      padding: 8px 16px;
      border: 1px solid #f38ba8;
      border-radius: 4px;
      background: transparent;
      color: #f38ba8;
      cursor: pointer;
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

    /* Todo list */
    .todo-list {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
    }

    .todo-item {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 10px 12px;
      margin-bottom: 4px;
      border-radius: 4px;
      background: var(--surface-0, #313244);
      border-left: 3px solid var(--border-color, #45475a);
      transition: all 0.15s ease;
    }

    .todo-item:hover {
      background: var(--panel-header-bg, #1e1e2e);
    }

    .todo-item.completed {
      border-left-color: #a6e3a1;
      opacity: 0.7;
    }

    .todo-item.in-progress {
      border-left-color: #f9e2af;
      background: rgba(249, 226, 175, 0.05);
    }

    .todo-item.pending {
      border-left-color: #89b4fa;
    }

    .todo-item.high-priority {
      border-left-color: #f38ba8;
    }

    .todo-item.high-priority.pending {
      background: rgba(243, 139, 168, 0.05);
    }

    .todo-status {
      font-size: 14px;
      flex-shrink: 0;
      width: 20px;
      text-align: center;
    }

    .todo-content {
      flex: 1;
      min-width: 0;
    }

    .todo-text {
      font-size: 12px;
      color: var(--text-primary, #cdd6f4);
      line-height: 1.4;
      word-wrap: break-word;
    }

    .todo-item.completed .todo-text {
      text-decoration: line-through;
      color: var(--text-muted, #6c7086);
    }

    .todo-notes {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 6px;
      padding-left: 12px;
      border-left: 2px solid var(--border-color, #45475a);
    }

    .note {
      font-size: 11px;
      color: var(--text-secondary, #a6adc8);
      font-family: 'JetBrains Mono', monospace;
    }

    .priority-badge {
      padding: 2px 6px;
      border-radius: 3px;
      background: #f38ba8;
      color: var(--timeline-bg, #11111b);
      font-size: 9px;
      font-weight: 600;
      flex-shrink: 0;
    }

    /* Summary bar */
    .summary-bar {
      display: flex;
      gap: 16px;
      padding: 8px 12px;
      background: var(--surface-0, #313244);
      border-top: 1px solid var(--border-color, #313244);
      font-size: 11px;
      flex-shrink: 0;
    }

    .summary-item {
      color: var(--text-muted, #6c7086);
      font-family: 'JetBrains Mono', monospace;
    }

    .summary-item.completed {
      color: #a6e3a1;
    }

    .summary-item.pending {
      color: #f9e2af;
    }
  `],
})
export class TodoListComponent implements OnInit {
  readonly todo = inject(TodoService);
  readonly audit = inject(AuditService);

  constructor() {
    // Load todos when selected job changes
    effect(() => {
      const jobId = this.audit.selectedJobId();
      if (jobId) {
        this.todo.loadTodos();
      } else {
        this.todo.clear();
      }
    });
  }

  ngOnInit(): void {
    // Initial load if job already selected
    if (this.audit.selectedJobId()) {
      this.todo.loadTodos();
    }
  }

  onPhaseSelect(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    if (value === 'current') {
      this.todo.showCurrent();
    } else {
      this.todo.selectArchive(value);
    }
  }

  formatArchiveDate(timestamp: string): string {
    try {
      const date = new Date(timestamp);
      return date.toLocaleDateString('en-GB', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return timestamp;
    }
  }
}
