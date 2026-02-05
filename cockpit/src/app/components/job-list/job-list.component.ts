import { Component, inject, signal, computed, OnInit, OnDestroy } from '@angular/core';
import { ApiService } from '../../core/services/api.service';
import { DataService } from '../../core/services/data.service';
import { JobStatus } from '../../core/models/api.model';
import { JobSummary } from '../../core/models/audit.model';

type StatusFilter = 'all' | JobStatus;

/**
 * Job List component that displays jobs with filtering and actions.
 */
@Component({
  selector: 'app-job-list',
  standalone: true,
  template: `
    <div class="job-list-container">
      <!-- Header with filters -->
      <div class="header-bar">
        <span class="title">Jobs</span>
        <div class="filter-chips">
          @for (filter of statusFilters; track filter.value) {
            <button
              class="filter-chip"
              [class.active]="activeFilter() === filter.value"
              (click)="setFilter(filter.value)"
            >
              {{ filter.label }}
              @if (filter.value !== 'all') {
                <span class="count">({{ getStatusCount(filter.value) }})</span>
              }
            </button>
          }
        </div>
        <button class="refresh-btn" (click)="refresh()" [disabled]="isLoading()">
          Refresh
        </button>
      </div>

      <!-- Loading State -->
      @if (isLoading() && jobs().length === 0) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>Loading jobs...</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && filteredJobs().length === 0) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4CB;</span>
          <span>No jobs found</span>
          @if (activeFilter() !== 'all') {
            <span class="empty-hint">Try selecting a different filter</span>
          } @else {
            <span class="empty-hint">Create a new job to get started</span>
          }
        </div>
      }

      <!-- Job Table -->
      @if (filteredJobs().length > 0) {
        <div class="table-container">
          <table class="job-table">
            <thead>
              <tr>
                <th class="col-status">Status</th>
                <th class="col-prompt">Prompt</th>
                <th class="col-progress">Progress</th>
                <th class="col-created">Created</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              @for (job of filteredJobs(); track job.id) {
                <tr
                  [class.selected]="selectedJobId() === job.id"
                  (click)="selectJob(job.id)"
                >
                  <td>
                    <span class="status-badge" [class]="'status-' + job.status">
                      {{ job.status }}
                    </span>
                  </td>
                  <td class="prompt-cell">
                    <span class="prompt-text" [title]="job.description">
                      {{ truncatePrompt(job.description) }}
                    </span>
                    <span class="job-id">{{ job.id.slice(0, 8) }}...</span>
                  </td>
                  <td class="progress-cell">
                    <div class="progress-info">
                      <span class="creator-status">C: {{ job.creator_status }}</span>
                      <span class="validator-status">V: {{ job.validator_status }}</span>
                    </div>
                  </td>
                  <td class="created-cell">
                    {{ formatDate(job.created_at) }}
                  </td>
                  <td class="actions-cell">
                    <button
                      class="action-btn view"
                      (click)="viewJob(job.id); $event.stopPropagation()"
                      title="View in debug panels"
                    >
                      View
                    </button>
                    @if (job.status === 'processing') {
                      <button
                        class="action-btn cancel"
                        (click)="cancelJob(job.id); $event.stopPropagation()"
                        title="Cancel job"
                      >
                        Cancel
                      </button>
                    }
                    @if (job.status !== 'completed' && job.status !== 'cancelled') {
                      <button
                        class="action-btn resume"
                        (click)="resumeJob(job.id); $event.stopPropagation()"
                        title="Resume from checkpoint"
                      >
                        Resume
                      </button>
                    }
                    @if (job.status !== 'processing') {
                      <button
                        class="action-btn delete"
                        (click)="deleteJob(job.id); $event.stopPropagation()"
                        title="Delete job"
                      >
                        Delete
                      </button>
                    }
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }

      <!-- Footer -->
      <div class="footer-bar">
        <span class="job-count">
          Showing {{ filteredJobs().length }} of {{ jobs().length }} jobs
        </span>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        overflow: hidden;
      }

      .job-list-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, #181825);
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
        flex-wrap: wrap;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .filter-chips {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
      }

      .filter-chip {
        padding: 4px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .filter-chip:hover {
        background: var(--surface-0, #313244);
      }

      .filter-chip.active {
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border-color: var(--accent-color, #cba6f7);
      }

      .filter-chip .count {
        opacity: 0.7;
        font-size: 10px;
      }

      .refresh-btn {
        margin-left: auto;
        padding: 5px 12px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 11px;
        cursor: pointer;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
      }

      /* Loading State */
      .loading-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px;
        flex: 1;
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
        opacity: 0.6;
      }

      /* Table */
      .table-container {
        flex: 1;
        overflow: auto;
      }

      .job-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }

      .job-table th {
        text-align: left;
        padding: 10px 12px;
        background: var(--surface-0, #313244);
        color: var(--text-muted, #6c7086);
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.5px;
        border-bottom: 1px solid var(--border-color, #45475a);
        position: sticky;
        top: 0;
        z-index: 1;
      }

      .col-status { width: 100px; }
      .col-prompt { width: auto; }
      .col-progress { width: 140px; }
      .col-created { width: 120px; }
      .col-actions { width: 150px; }

      .job-table td {
        padding: 10px 12px;
        border-bottom: 1px solid var(--border-color, #313244);
        color: var(--text-primary, #cdd6f4);
        vertical-align: middle;
      }

      .job-table tbody tr {
        cursor: pointer;
        transition: background 0.15s ease;
      }

      .job-table tbody tr:hover {
        background: var(--surface-0, #313244);
      }

      .job-table tbody tr.selected {
        background: rgba(203, 166, 247, 0.15);
      }

      /* Status Badge */
      .status-badge {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 500;
        text-transform: capitalize;
      }

      .status-badge.status-created {
        background: rgba(137, 180, 250, 0.2);
        color: #89b4fa;
      }

      .status-badge.status-processing {
        background: rgba(249, 226, 175, 0.2);
        color: #f9e2af;
      }

      .status-badge.status-completed {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
      }

      .status-badge.status-failed {
        background: rgba(243, 139, 168, 0.2);
        color: #f38ba8;
      }

      .status-badge.status-cancelled {
        background: rgba(108, 112, 134, 0.2);
        color: #6c7086;
      }

      /* Prompt Cell */
      .prompt-cell {
        max-width: 300px;
      }

      .prompt-text {
        display: block;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        margin-bottom: 2px;
      }

      .job-id {
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
      }

      /* Progress Cell */
      .progress-info {
        display: flex;
        flex-direction: column;
        gap: 2px;
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
      }

      .creator-status {
        color: #94e2d5;
      }

      .validator-status {
        color: #f9e2af;
      }

      /* Created Cell */
      .created-cell {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      /* Actions */
      .actions-cell {
        white-space: nowrap;
      }

      .action-btn {
        padding: 4px 8px;
        margin-right: 4px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        font-size: 10px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .action-btn:last-child {
        margin-right: 0;
      }

      .action-btn.view {
        color: #89b4fa;
        border-color: #89b4fa;
      }

      .action-btn.cancel {
        color: #f9e2af;
        border-color: #f9e2af;
      }

      .action-btn.delete {
        color: #f38ba8;
        border-color: #f38ba8;
      }

      .action-btn.resume {
        color: #a6e3a1;
        border-color: #a6e3a1;
      }

      .action-btn:hover {
        background: rgba(255, 255, 255, 0.1);
      }

      /* Footer */
      .footer-bar {
        display: flex;
        align-items: center;
        padding: 8px 12px;
        background: var(--surface-0, #313244);
        border-top: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .job-count {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }
    `,
  ],
})
export class JobListComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);

  readonly jobs = signal<JobSummary[]>([]);
  readonly isLoading = signal(false);
  readonly activeFilter = signal<StatusFilter>('all');
  readonly selectedJobId = signal<string | null>(null);

  private refreshInterval: ReturnType<typeof setInterval> | null = null;

  readonly statusFilters: { label: string; value: StatusFilter }[] = [
    { label: 'All', value: 'all' },
    { label: 'Created', value: 'created' },
    { label: 'Processing', value: 'processing' },
    { label: 'Completed', value: 'completed' },
    { label: 'Failed', value: 'failed' },
    { label: 'Cancelled', value: 'cancelled' },
  ];

  readonly filteredJobs = computed(() => {
    const filter = this.activeFilter();
    const allJobs = this.jobs();

    if (filter === 'all') {
      return allJobs;
    }
    return allJobs.filter((job) => job.status === filter);
  });

  ngOnInit(): void {
    this.refresh();
    // Auto-refresh every 30 seconds
    this.refreshInterval = setInterval(() => {
      if (!this.isLoading()) {
        this.refresh();
      }
    }, 30000);
  }

  ngOnDestroy(): void {
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
    }
  }

  refresh(): void {
    this.isLoading.set(true);
    this.api.getJobs().subscribe((jobs) => {
      this.jobs.set(jobs);
      this.isLoading.set(false);
    });
  }

  setFilter(filter: StatusFilter): void {
    this.activeFilter.set(filter);
  }

  getStatusCount(status: JobStatus): number {
    return this.jobs().filter((job) => job.status === status).length;
  }

  selectJob(jobId: string): void {
    this.selectedJobId.set(jobId);
  }

  viewJob(jobId: string): void {
    // Use DataService to switch to this job for debug panels
    this.data.setCurrentJob(jobId);
    this.selectedJobId.set(jobId);
  }

  cancelJob(jobId: string): void {
    this.api.cancelJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
      }
    });
  }

  resumeJob(jobId: string): void {
    this.api.resumeJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
      }
    });
  }

  deleteJob(jobId: string): void {
    this.api.deleteJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
        // Clear selection if deleted job was selected
        if (this.selectedJobId() === jobId) {
          this.selectedJobId.set(null);
        }
      }
    });
  }

  truncatePrompt(prompt: string | undefined, maxLength: number = 80): string {
    if (!prompt) {
      return '';
    }
    if (prompt.length <= maxLength) {
      return prompt;
    }
    return prompt.slice(0, maxLength) + '...';
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(undefined, {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
}
