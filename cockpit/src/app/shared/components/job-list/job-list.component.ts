import {Component, computed, inject, OnDestroy, OnInit, signal} from '@angular/core';
import {ApiService} from '../../../core/services/api.service';
import {DataService} from '../../../core/services/data.service';
import {UserService} from '../../../core/services/user.service';
import {environment} from '../../../core/environment';
import {JobStatus} from '../../../core/models/api.model';
import {JobSummary} from '../../../core/models/audit.model';

type StatusFilter = 'all' | 'mine' | JobStatus;

/** A row in the hierarchical job list. */
interface JobRow {
  job: JobSummary;
  depth: number;        // 0 = root, 1 = child
  hasChildren: boolean;
  isChild: boolean;
}

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
              @if (filter.value !== 'all' && filter.value !== 'mine') {
                <span class="count">({{ getStatusCount(filter.value) }})</span>
              }
            </button>
          }
        </div>
        <button class="refresh-btn" (click)="refresh()" [disabled]="isLoading()">
          Refresh
        </button>
        @if (snapshotStats()?.available) {
          <span class="snapshot-stats" title="S3 snapshot storage">
            {{ snapshotStats()!.total_snapshots }} snapshots &middot; {{ formatBytes(snapshotStats()!.total_size_bytes) }}
          </span>
        }
      </div>

      <!-- Loading State -->
      @if (isLoading() && jobs().length === 0) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>Loading jobs...</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && displayRows().length === 0) {
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
      @if (displayRows().length > 0) {
        <div class="table-container">
          <table class="job-table">
            <thead>
              <tr>
                <th class="col-prompt">Job</th>
                <th class="col-status">Status</th>
                <th class="col-created">Created</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              @for (row of displayRows(); track row.job.id) {
                <tr
                  [class.selected]="selectedJobId() === row.job.id"
                  [class.child-row]="row.isChild"
                  (click)="selectJob(row.job.id)"
                >
                  <td class="prompt-cell">
                    <div class="prompt-inner" [style.padding-left.px]="row.isChild ? 16 : 0">
                      @if (row.hasChildren) {
                        <button
                          class="expand-btn"
                          [class.expanded]="isExpanded(row.job.id)"
                          (click)="toggleExpand(row.job.id); $event.stopPropagation()"
                          [title]="isExpanded(row.job.id) ? 'Collapse sub-jobs' : 'Expand sub-jobs'"
                        >
                          <span class="expand-chevron">&#9206;</span>
                        </button>
                      }
                      @if (row.isChild) {
                        <span class="child-connector">\u2514</span>
                      }
                      @if (getUserColor(row.job.user_id)) {
                        <span
                          class="user-dot"
                          [style.background]="getUserColor(row.job.user_id)"
                          [title]="getUserName(row.job.user_id)"
                        ></span>
                      }
                      <span class="prompt-text" [title]="row.job.description">
                        {{ row.job.description }}
                      </span>
                    </div>
                    <div class="job-id" [style.padding-left.px]="row.isChild ? 16 : 0">
                      {{ row.job.id }}
                      @if (row.isChild && row.job.config_name) {
                        <span class="config-badge">{{ row.job.config_name }}</span>
                      }
                    </div>
                  </td>
                  <td>
                    <div class="status-cell-inner">
                      <span class="status-badge" [class]="'status-' + row.job.status">
                        {{ formatStatus(row.job.status) }}
                      </span>
                      @if (row.job.snapshot_status === 'available') {
                        <span class="snapshot-badge" title="Environment snapshot available">S</span>
                      }
                    </div>
                  </td>
                  <td class="created-cell">
                    {{ formatDate(row.job.created_at) }}
                  </td>
                  <td class="actions-cell">
                    <button
                      class="action-btn view"
                      (click)="viewJob(row.job.id); $event.stopPropagation()"
                      title="View in debug panels"
                    >
                      View
                    </button>
                    @if (getWorkspaceUrl(row.job)) {
                      <button
                        class="action-btn workspace"
                        (click)="openWorkspace(row.job); $event.stopPropagation()"
                        title="Browse workspace in Gitea"
                      >
                        Workspace
                      </button>
                    }
                    @if (canOpenIde(row.job)) {
                      @if (ideLoadingJobIds().has(row.job.id)) {
                        <button
                          class="action-btn ide loading"
                          disabled
                          (click)="$event.stopPropagation()"
                          title="Starting IDE session..."
                        >
                          <span class="btn-spinner"></span>
                          Starting
                        </button>
                      } @else {
                        <button
                          class="action-btn ide"
                          [class.gitea-only]="!row.job.snapshot_status && !hasLiveVm(row.job)"
                          (click)="openIde(row.job.id); $event.stopPropagation()"
                          [title]="row.job.snapshot_status === 'available' ? 'Open workspace in Web IDE (from snapshot)' : 'Open workspace in Web IDE (code only)'"
                        >
                          IDE
                        </button>
                      }
                    }
                    @if (row.job.status === 'processing') {
                      @if (cancelingJobIds().has(row.job.id)) {
                        <button
                          class="action-btn canceling"
                          disabled
                          title="Canceling job..."
                        >
                          <span class="btn-spinner"></span>
                          Canceling
                        </button>
                      } @else {
                        <button
                          class="action-btn pause"
                          (click)="pauseJob(row.job.id); $event.stopPropagation()"
                          title="Pause job"
                        >
                          Pause
                        </button>
                        @if (confirmingCancelId() === row.job.id) {
                          <button
                            class="action-btn cancel confirming"
                            (click)="cancelJob(row.job.id); $event.stopPropagation()"
                            title="Click again to confirm cancellation"
                          >
                            Sure?
                          </button>
                        } @else {
                          <button
                            class="action-btn cancel"
                            (click)="confirmCancel(row.job.id); $event.stopPropagation()"
                            title="Cancel job"
                          >
                            Cancel
                          </button>
                        }
                      }
                    }
                    @if (row.job.status === 'pending_review') {
                      <button
                        class="action-btn review"
                        (click)="reviewJob(row.job.id); $event.stopPropagation()"
                        title="Review and approve/continue"
                      >
                        Review
                      </button>
                    }
                    @if (row.job.status === 'failed' || row.job.status === 'cancelled' || row.job.status === 'paused' || row.job.status === 'created') {
                      <button
                        class="action-btn resume"
                        (click)="resumeJob(row.job.id); $event.stopPropagation()"
                        title="Resume from checkpoint"
                      >
                        Resume
                      </button>
                    }
                    @if (row.job.status !== 'processing' && row.job.status !== 'completed' && row.job.status !== 'cancelled') {
                      @if (cancelingJobIds().has(row.job.id)) {
                        <button
                          class="action-btn canceling"
                          disabled
                          title="Canceling job..."
                        >
                          <span class="btn-spinner"></span>
                          Canceling
                        </button>
                      } @else if (confirmingCancelId() === row.job.id) {
                        <button
                          class="action-btn cancel confirming"
                          (click)="cancelJob(row.job.id); $event.stopPropagation()"
                          title="Click again to confirm cancellation"
                        >
                          Sure?
                        </button>
                      } @else {
                        <button
                          class="action-btn cancel"
                          (click)="confirmCancel(row.job.id); $event.stopPropagation()"
                          title="Cancel job"
                        >
                          Cancel
                        </button>
                      }
                    }
                    @if (row.job.status === 'completed' && !row.job.project_id) {
                      <button
                        class="action-btn promote"
                        (click)="togglePromote(row.job.id); $event.stopPropagation()"
                        title="Promote to project"
                      >
                        Promote
                      </button>
                    }
                    @if (row.job.status !== 'processing' && row.job.status !== 'paused' && row.job.status !== 'reviewing' && row.job.status !== 'waiting') {
                      @if (confirmingDeleteId() === row.job.id) {
                        <button
                          class="action-btn delete confirming"
                          (click)="deleteJob(row.job.id); $event.stopPropagation()"
                          title="Click again to confirm deletion"
                        >
                          Confirm?
                        </button>
                      } @else {
                        <button
                          class="action-btn delete"
                          (click)="confirmDelete(row.job.id); $event.stopPropagation()"
                          title="Delete job"
                        >
                          Delete
                        </button>
                      }
                    }
                  </td>
                </tr>
                @if (promoteJobId() === row.job.id) {
                  <tr class="promote-row" (click)="$event.stopPropagation()">
                    <td colspan="5">
                      <div class="promote-form">
                        <input
                          class="promote-input"
                          placeholder="Project name (required)"
                          [value]="promoteName()"
                          (input)="promoteName.set(asInputValue($event))"
                        />
                        <input
                          class="promote-input"
                          placeholder="Description"
                          [value]="promoteDescription()"
                          (input)="promoteDescription.set(asInputValue($event))"
                        />
                        <input
                          class="promote-input"
                          placeholder="Goal"
                          [value]="promoteGoal()"
                          (input)="promoteGoal.set(asInputValue($event))"
                        />
                        <button
                          class="action-btn promote"
                          [disabled]="!promoteName().trim()"
                          (click)="submitPromote(row.job.id)"
                        >
                          Create Project
                        </button>
                        <button
                          class="action-btn cancel"
                          (click)="promoteJobId.set(null)"
                        >
                          Cancel
                        </button>
                      </div>
                    </td>
                  </tr>
                }
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

      .snapshot-stats {
        font-size: 10px;
        color: #6c7086;
        margin-left: 8px;
        flex-shrink: 0;
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
        overflow-y: auto;
        overflow-x: hidden;
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

      .col-prompt { width: 100%; }
      .col-status { white-space: nowrap; }
      .col-created { white-space: nowrap; }
      .col-actions { white-space: nowrap; }

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
        white-space: nowrap;
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

      .status-badge.status-pending_review {
        background: rgba(250, 179, 135, 0.2);
        color: #fab387;
        font-weight: 600;
      }

      .status-badge.status-paused {
        background: rgba(180, 190, 254, 0.2);
        color: #b4befe;
      }

      .status-badge.status-reviewing {
        background: rgba(203, 166, 247, 0.2);
        color: #cba6f7;
        font-weight: 600;
      }

      .status-badge.status-waiting {
        background: rgba(116, 199, 236, 0.2);
        color: #74c7ec;
      }

      /* Hierarchy */
      .status-cell-inner {
        display: flex;
        align-items: center;
        gap: 4px;
      }

      .expand-btn {
        background: rgba(127, 132, 156, 0.1);
        border: 1px solid rgba(127, 132, 156, 0.25);
        border-radius: 4px;
        color: var(--text-muted, #7f849c);
        cursor: pointer;
        padding: 2px;
        width: 22px;
        height: 22px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        transition: all 0.15s ease;
      }

      .expand-btn:hover {
        color: var(--text-primary, #cdd6f4);
        background: rgba(127, 132, 156, 0.2);
        border-color: rgba(127, 132, 156, 0.4);
      }

      .expand-chevron {
        display: inline-block;
        font-size: 14px;
        line-height: 1;
        transition: transform 0.15s ease;
        transform: rotate(90deg);
      }

      .expand-btn.expanded .expand-chevron {
        transform: rotate(180deg);
      }

      .child-connector {
        color: var(--text-muted, #6c7086);
        font-size: 11px;
        opacity: 0.5;
        flex-shrink: 0;
      }

      .child-row {
        background: rgba(0, 0, 0, 0.15);
      }

      .child-row:hover {
        background: rgba(0, 0, 0, 0.25) !important;
      }

      .config-badge {
        display: inline-block;
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 9px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.3px;
        background: rgba(203, 166, 247, 0.15);
        color: #cba6f7;
        margin-left: 2px;
        flex-shrink: 0;
      }

      .snapshot-badge {
        display: inline-block;
        width: 16px;
        height: 16px;
        border-radius: 3px;
        font-size: 9px;
        font-weight: 600;
        line-height: 16px;
        text-align: center;
        background: rgba(166, 227, 161, 0.15);
        color: #a6e3a1;
        margin-left: 4px;
        flex-shrink: 0;
        cursor: help;
      }

      /* User dot */
      .user-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 4px;
        vertical-align: middle;
        flex-shrink: 0;
      }

      /* Prompt/Job Cell */
      .prompt-cell {
        max-width: 0;
        overflow: hidden;
      }

      .prompt-inner {
        display: flex;
        align-items: center;
        gap: 4px;
      }

      .prompt-text {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .job-id {
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
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
        overflow: hidden;
      }

      .action-btn {
        padding: 3px 6px;
        margin-right: 3px;
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

      .action-btn.pause {
        color: #b4befe;
        border-color: #b4befe;
      }

      .action-btn.cancel {
        color: #f9e2af;
        border-color: #f9e2af;
      }

      .action-btn.canceling {
        color: #6c7086;
        border-color: #6c7086;
        cursor: not-allowed;
        opacity: 0.7;
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }

      .btn-spinner {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 1.5px solid #6c7086;
        border-top-color: #f9e2af;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .action-btn.delete {
        color: #f38ba8;
        border-color: #f38ba8;
      }

      .action-btn.resume {
        color: #a6e3a1;
        border-color: #a6e3a1;
      }

      .action-btn.review {
        color: #fab387;
        border-color: #fab387;
        font-weight: 600;
      }

      .action-btn.workspace {
        color: #94e2d5;
        border-color: #94e2d5;
        text-decoration: none;
      }

      .action-btn.ide {
        color: #89b4fa;
        border-color: #89b4fa;
        text-decoration: none;
        cursor: pointer;
      }

      .action-btn.ide.gitea-only {
        color: #7f849c;
        border-color: #7f849c;
        opacity: 0.7;
      }

      .action-btn.ide.loading {
        color: #6c7086;
        border-color: #6c7086;
        cursor: not-allowed;
        opacity: 0.7;
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }

      .action-btn.promote {
        color: #94e2d5;
        border-color: #94e2d5;
      }

      .action-btn.promote:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .action-btn.confirming {
        font-weight: 700;
        animation: pulse-confirm 1s ease-in-out infinite;
        color: #f38ba8;
        border-color: #f38ba8;
      }

      @keyframes pulse-confirm {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
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

      .promote-row td {
        padding: 0 12px 10px !important;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .promote-form {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
        padding: 8px 0;
      }

      .promote-input {
        padding: 5px 8px;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        color: var(--text-primary, #cdd6f4);
        font-size: 11px;
        font-family: inherit;
        outline: none;
      }

      .promote-input:focus {
        border-color: var(--accent-color, #cba6f7);
      }

      @media (max-width: 768px) {
        .col-created {
          display: none;
        }

        .job-table tr {
          min-height: 44px;
        }

        .col-actions {
          width: 80px;
        }
      }
    `,
  ],
})
export class JobListComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);
  private readonly userService = inject(UserService);

  readonly jobs = signal<JobSummary[]>([]);
  readonly isLoading = signal(false);
  readonly activeFilter = signal<StatusFilter>('all');
  readonly snapshotStats = signal<{ available: boolean; total_snapshots: number; total_size_bytes: number } | null>(null);
  readonly selectedJobId = signal<string | null>(null);

  // Expand/collapse state for parent jobs
  readonly expandedJobIds = signal<Set<string>>(new Set());

  // In-flight action tracking
  readonly cancelingJobIds = signal<Set<string>>(new Set());
  readonly ideLoadingJobIds = signal<Set<string>>(new Set());
  private idePollingIntervals = new Map<string, ReturnType<typeof setInterval>>();

  // Inline confirmation state
  readonly confirmingDeleteId = signal<string | null>(null);
  readonly confirmingCancelId = signal<string | null>(null);
  private confirmTimeout: ReturnType<typeof setTimeout> | null = null;

  // Promote form state
  readonly promoteJobId = signal<string | null>(null);
  readonly promoteName = signal('');
  readonly promoteDescription = signal('');
  readonly promoteGoal = signal('');

  private refreshInterval: ReturnType<typeof setInterval> | null = null;

  readonly statusFilters: { label: string; value: StatusFilter }[] = [
    { label: 'All', value: 'all' },
    { label: 'Mine', value: 'mine' },
    { label: 'Review', value: 'pending_review' },
    { label: 'Reviewing', value: 'reviewing' },
    { label: 'Waiting', value: 'waiting' },
    { label: 'Created', value: 'created' },
    { label: 'Processing', value: 'processing' },
    { label: 'Completed', value: 'completed' },
    { label: 'Paused', value: 'paused' },
    { label: 'Failed', value: 'failed' },
    { label: 'Cancelled', value: 'cancelled' },
  ];

  readonly filteredJobs = computed(() => {
    const filter = this.activeFilter();
    const allJobs = this.jobs();

    if (filter === 'all') {
      return allJobs;
    }
    if (filter === 'mine') {
      const userId = this.userService.currentUserId();
      if (!userId) return allJobs;
      return allJobs.filter((job) => job.user_id === userId);
    }
    return allJobs.filter((job) => job.status === filter);
  });

  /**
   * Build a flat list of JobRows with hierarchy info.
   * Root jobs (no parent_job_id) appear first, followed by their children
   * when expanded. Children whose parent is not in the filtered list
   * appear as root-level items.
   */
  readonly displayRows = computed<JobRow[]>(() => {
    const jobs = this.filteredJobs();
    const expanded = this.expandedJobIds();

    // Build parent → children map from the full filtered list
    const childrenMap = new Map<string, JobSummary[]>();
    const childIds = new Set<string>();

    for (const job of jobs) {
      if (job.parent_job_id) {
        const siblings = childrenMap.get(job.parent_job_id) || [];
        siblings.push(job);
        childrenMap.set(job.parent_job_id, siblings);
        childIds.add(job.id);
      }
    }

    const rows: JobRow[] = [];

    for (const job of jobs) {
      // Skip children — they'll be inserted after their parent
      if (childIds.has(job.id)) {
        // Unless their parent is not in the filtered list
        const parentInList = jobs.some((j) => j.id === job.parent_job_id);
        if (parentInList) continue;
        // Parent not visible — show as standalone child indicator
        rows.push({
          job,
          depth: 0,
          hasChildren: childrenMap.has(job.id),
          isChild: true,
        });
        continue;
      }

      const children = childrenMap.get(job.id);
      const hasChildren = !!children && children.length > 0;

      rows.push({ job, depth: 0, hasChildren, isChild: false });

      // Insert children if expanded
      if (hasChildren && expanded.has(job.id)) {
        for (const child of children) {
          rows.push({
            job: child,
            depth: 1,
            hasChildren: childrenMap.has(child.id),
            isChild: true,
          });
        }
      }
    }

    return rows;
  });

  ngOnInit(): void {
    this.refresh();
    this.api.getSnapshotStats().subscribe((stats) => {
      if (stats) this.snapshotStats.set(stats);
    });
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
    // Clean up IDE polling intervals
    for (const interval of this.idePollingIntervals.values()) {
      clearInterval(interval);
    }
    this.idePollingIntervals.clear();
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

  getStatusCount(status: string): number {
    return this.jobs().filter((job) => job.status === status).length;
  }

  selectJob(jobId: string): void {
    this.selectedJobId.set(jobId);
  }

  toggleExpand(jobId: string): void {
    const current = this.expandedJobIds();
    const next = new Set(current);
    if (next.has(jobId)) {
      next.delete(jobId);
    } else {
      next.add(jobId);
    }
    this.expandedJobIds.set(next);
  }

  isExpanded(jobId: string): boolean {
    return this.expandedJobIds().has(jobId);
  }

  getWorkspaceUrl(job: JobSummary): string | null {
    const giteaUrl = environment.giteaUrl;
    if (!giteaUrl) return null;
    const repoName = job.repo_name || `job-${job.id}`;
    if (job.branch_name) {
      return `${giteaUrl}/${repoName}/src/branch/${job.branch_name}`;
    }
    return `${giteaUrl}/${repoName}`;
  }

  openWorkspace(job: JobSummary): void {
    const url = this.getWorkspaceUrl(job);
    if (!url) return;
    // Ensure Gitea access is granted (may have been skipped at job creation
    // if user hadn't logged into Gitea yet), then navigate.
    this.api.ensureWorkspaceAccess(job.id).subscribe(() => {
      window.open(url, '_blank');
    });
  }

  hasLiveVm(job: JobSummary): boolean {
    return job.status === 'processing';
  }

  canOpenIde(job: JobSummary): boolean {
    // Show IDE button if: live VM, snapshot available, or has Gitea repo
    return this.hasLiveVm(job) || job.snapshot_status === 'available' || !!job.repo_name;
  }

  openIde(jobId: string): void {
    // Mark as loading
    const next = new Set(this.ideLoadingJobIds());
    next.add(jobId);
    this.ideLoadingJobIds.set(next);

    // First check current session status
    this.api.getIdeSession(jobId).subscribe((result) => {
      if (!result) {
        this.removeIdeLoading(jobId);
        return;
      }

      if (result.status === 'active' || result.status === 'idle') {
        // Already active — open directly
        this.removeIdeLoading(jobId);
        if (result.code_server_url) {
          window.open(result.code_server_url, '_blank');
        }
        return;
      }

      if (result.status === 'available' || result.status === 'expired' || result.status === 'failed') {
        // Start a new session
        this.api.startIdeSession(jobId).subscribe((startResult) => {
          if (!startResult || startResult.status === 'unavailable' || startResult.status === 'failed') {
            this.removeIdeLoading(jobId);
            return;
          }
          // Poll until active
          this.pollIdeSession(jobId);
        });
        return;
      }

      if (result.status === 'restoring') {
        // Already restoring (started from another tab) — just poll
        this.pollIdeSession(jobId);
        return;
      }

      // unavailable or other — stop loading
      this.removeIdeLoading(jobId);
    });
  }

  private pollIdeSession(jobId: string): void {
    // Clear any existing poll for this job
    const existing = this.idePollingIntervals.get(jobId);
    if (existing) clearInterval(existing);

    const interval = setInterval(() => {
      this.api.getIdeSession(jobId).subscribe((result) => {
        if (!result) return;

        if (result.status === 'active' || result.status === 'idle') {
          clearInterval(interval);
          this.idePollingIntervals.delete(jobId);
          this.removeIdeLoading(jobId);
          if (result.code_server_url) {
            window.open(result.code_server_url, '_blank');
          }
        } else if (result.status === 'failed' || result.status === 'unavailable') {
          clearInterval(interval);
          this.idePollingIntervals.delete(jobId);
          this.removeIdeLoading(jobId);
        }
        // else 'restoring' — keep polling
      });
    }, 3000);

    this.idePollingIntervals.set(jobId, interval);
  }

  private removeIdeLoading(jobId: string): void {
    const next = new Set(this.ideLoadingJobIds());
    next.delete(jobId);
    this.ideLoadingJobIds.set(next);
  }

  viewJob(jobId: string): void {
    // Use DataService to switch to this job for debug panels
    this.data.setCurrentJob(jobId);
    this.selectedJobId.set(jobId);
  }

  reviewJob(jobId: string): void {
    // Select the job and open it in debug panels (review component will detect pending_review)
    this.data.setCurrentJob(jobId);
    this.selectedJobId.set(jobId);
  }

  pauseJob(jobId: string): void {
    this.api.pauseJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
      }
    });
  }

  cancelJob(jobId: string): void {
    this.clearConfirmations();
    const next = new Set(this.cancelingJobIds());
    next.add(jobId);
    this.cancelingJobIds.set(next);

    this.api.cancelJob(jobId).subscribe({
      next: (result) => {
        this.removeCanceling(jobId);
        if (result) this.refresh();
      },
      error: () => this.removeCanceling(jobId),
    });
  }

  private removeCanceling(jobId: string): void {
    const next = new Set(this.cancelingJobIds());
    next.delete(jobId);
    this.cancelingJobIds.set(next);
  }

  resumeJob(jobId: string): void {
    this.api.resumeJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
      }
    });
  }

  confirmDelete(jobId: string): void {
    this.clearConfirmations();
    this.confirmingDeleteId.set(jobId);
    this.confirmTimeout = setTimeout(() => this.clearConfirmations(), 3000);
  }

  confirmCancel(jobId: string): void {
    this.clearConfirmations();
    this.confirmingCancelId.set(jobId);
    this.confirmTimeout = setTimeout(() => this.clearConfirmations(), 3000);
  }

  private clearConfirmations(): void {
    this.confirmingDeleteId.set(null);
    this.confirmingCancelId.set(null);
    if (this.confirmTimeout) {
      clearTimeout(this.confirmTimeout);
      this.confirmTimeout = null;
    }
  }

  deleteJob(jobId: string): void {
    this.clearConfirmations();
    this.api.deleteJob(jobId).subscribe((result) => {
      if (result) {
        this.refresh();
        if (this.selectedJobId() === jobId) {
          this.selectedJobId.set(null);
        }
      }
    });
  }

  togglePromote(jobId: string): void {
    if (this.promoteJobId() === jobId) {
      this.promoteJobId.set(null);
    } else {
      this.promoteJobId.set(jobId);
      this.promoteName.set('');
      this.promoteDescription.set('');
      this.promoteGoal.set('');
    }
  }

  submitPromote(jobId: string): void {
    const name = this.promoteName().trim();
    if (!name) return;
    const userId = this.userService.currentUserId();
    if (!userId) return;

    this.api.promoteJob(jobId, {
      name,
      description: this.promoteDescription().trim() || undefined,
      goal: this.promoteGoal().trim() || undefined,
      user_id: userId,
    }).subscribe((result) => {
      if (result) {
        this.promoteJobId.set(null);
        this.refresh();
      }
    });
  }

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  getUserColor(userId?: string | null): string | null {
    if (!userId) return null;
    const user = this.userService.users().find((u) => u.id === userId);
    return user?.avatar_color ?? null;
  }

  getUserName(userId?: string | null): string {
    if (!userId) return 'Unassigned';
    const user = this.userService.users().find((u) => u.id === userId);
    return user?.display_name ?? 'Unknown';
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

  formatStatus(status: string): string {
    return status
      .split('_')
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
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

  formatBytes(bytes: number): string {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }
}
