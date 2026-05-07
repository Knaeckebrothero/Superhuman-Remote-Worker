import {Component, computed, inject, OnDestroy, OnInit, signal} from '@angular/core';
import {ApiService} from '../../core/services/api.service';
import {DataService} from '../../core/services/data.service';
import {UserService} from '../../core/services/user.service';
import {environment} from '../../core/environment';
import {JobStatus} from '../../core/models/api.model';
import {JobSummary} from '../../core/models/audit.model';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppButtonComponent} from '../../ui/button';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppChipComponent} from '../../ui/chip';
import {AppInputComponent} from '../../ui/input';
import {AppSpinnerComponent} from '../../ui/spinner';

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
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppBadgeComponent,
    AppChipComponent,
    AppInputComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="job-list-container">
      <!-- Header with filters -->
      <div class="header-bar">
        <span class="title">{{ 'jobs.title' | transloco }}</span>
        <div class="filter-chips">
          @for (filter of statusFilters; track filter.value) {
            <app-chip
              size="sm"
              [selected]="activeFilter() === filter.value"
              (clicked)="setFilter(filter.value)"
            >
              {{ filter.labelKey | transloco }}
              @if (filter.value !== 'all' && filter.value !== 'mine') {
                <span class="count">({{ getStatusCount(filter.value) }})</span>
              }
            </app-chip>
          }
        </div>
        <app-button
          variant="secondary"
          size="sm"
          class="refresh-btn"
          [disabled]="isLoading()"
          (clicked)="refresh()"
        >
          {{ 'jobs.refresh' | transloco }}
        </app-button>
        @if (snapshotStats()?.available) {
          <span class="snapshot-stats" [title]="'jobs.tooltip.snapshotStats' | transloco">
            {{ 'jobs.snapshotsSummary' | transloco:{ count: snapshotStats()!.total_snapshots, size: formatBytes(snapshotStats()!.total_size_bytes) } }}
          </span>
        }
      </div>

      <!-- Loading State -->
      @if (isLoading() && jobs().length === 0) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'jobs.loading' | transloco }}</span>
        </div>
      }

      <!-- Empty State -->
      @if (!isLoading() && displayRows().length === 0) {
        <div class="empty-state">
          <span class="empty-icon">&#x1F4CB;</span>
          <span>{{ 'jobs.noJobsFound' | transloco }}</span>
          @if (activeFilter() !== 'all') {
            <span class="empty-hint">{{ 'jobs.noJobsHintFilter' | transloco }}</span>
          } @else {
            <span class="empty-hint">{{ 'jobs.noJobsHintEmpty' | transloco }}</span>
          }
        </div>
      }

      <!-- Job Table -->
      @if (displayRows().length > 0) {
        <div class="table-container">
          <table class="job-table">
            <thead>
              <tr>
                <th class="col-prompt">{{ 'jobs.colJob' | transloco }}</th>
                <th class="col-status">{{ 'jobs.colStatus' | transloco }}</th>
                <th class="col-created">{{ 'jobs.colCreated' | transloco }}</th>
                <th class="col-actions">{{ 'jobs.colActions' | transloco }}</th>
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
                          [title]="(isExpanded(row.job.id) ? 'jobs.tooltip.collapse' : 'jobs.tooltip.expand') | transloco"
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
                      <app-badge [tone]="jobStatusTone(row.job.status)" size="sm">
                        {{ 'jobs.status.' + row.job.status | transloco }}
                      </app-badge>
                      @if (row.job.status === 'waiting' && row.hasChildren) {
                        <span class="delegation-badge" [title]="'jobs.tooltip.delegationWaiting' | transloco">
                          {{ 'jobs.delegationChildren' | transloco:{ count: getChildCount(row.job.id) } }}
                        </span>
                      }
                      @if (row.isChild && row.job.creation_order != null) {
                        <span class="delegation-badge" [title]="'jobs.tooltip.delegationChild' | transloco:{ order: row.job.creation_order }">
                          #{{ row.job.creation_order }}
                        </span>
                      }
                      @if (row.job.snapshot_status === 'available') {
                        <span class="snapshot-badge" [title]="'jobs.tooltip.snapshotAvailable' | transloco">S</span>
                      }
                    </div>
                  </td>
                  <td class="created-cell">
                    {{ formatDate(row.job.created_at) }}
                  </td>
                  <td class="actions-cell">
                    <app-button
                      variant="info"
                      size="sm"
                      [ariaLabel]="'jobs.tooltip.view' | transloco"
                      (clicked)="viewJob(row.job.id); $event.stopPropagation()"
                    >
                      {{ 'jobs.action.view' | transloco }}
                    </app-button>
                    @if (getWorkspaceUrl(row.job)) {
                      <app-button
                        variant="secondary"
                        size="sm"
                        [ariaLabel]="'jobs.tooltip.workspace' | transloco"
                        (clicked)="openWorkspace(row.job); $event.stopPropagation()"
                      >
                        {{ 'jobs.action.workspace' | transloco }}
                      </app-button>
                    }
                    @if (canOpenIde(row.job)) {
                      <app-button
                        [variant]="!row.job.snapshot_status && !hasLiveVm(row.job) ? 'secondary' : 'info'"
                        size="sm"
                        [loading]="ideLoadingJobIds().has(row.job.id)"
                        [ariaLabel]="(row.job.snapshot_status === 'available' ? 'jobs.tooltip.ideSnapshot' : 'jobs.tooltip.ideCode') | transloco"
                        (clicked)="openIde(row.job.id); $event.stopPropagation()"
                      >
                        @if (ideLoadingJobIds().has(row.job.id)) {
                          {{ 'jobs.action.starting' | transloco }}
                        } @else {
                          {{ 'jobs.action.ide' | transloco }}
                        }
                      </app-button>
                    }
                    @if (row.job.status === 'processing') {
                      <app-button
                        variant="secondary"
                        size="sm"
                        [ariaLabel]="'jobs.tooltip.pauseJob' | transloco"
                        (clicked)="pauseJob(row.job.id); $event.stopPropagation()"
                      >
                        {{ 'jobs.action.pause' | transloco }}
                      </app-button>
                    }
                    @if (row.job.status === 'pending_review') {
                      <app-button
                        variant="warning"
                        size="sm"
                        [ariaLabel]="'jobs.tooltip.reviewJob' | transloco"
                        (clicked)="reviewJob(row.job.id); $event.stopPropagation()"
                      >
                        {{ 'jobs.action.review' | transloco }}
                      </app-button>
                    }
                    @if (row.job.status === 'failed' || row.job.status === 'cancelled' || row.job.status === 'paused' || row.job.status === 'created') {
                      <app-button
                        variant="success"
                        size="sm"
                        [ariaLabel]="'jobs.tooltip.resumeJob' | transloco"
                        (clicked)="resumeJob(row.job.id); $event.stopPropagation()"
                      >
                        {{ 'jobs.action.resume' | transloco }}
                      </app-button>
                    }
                    @if (row.job.status !== 'completed' && row.job.status !== 'cancelled') {
                      @if (confirmingCancelId() === row.job.id) {
                        <app-button
                          variant="danger"
                          size="sm"
                          [loading]="cancelingJobIds().has(row.job.id)"
                          [ariaLabel]="'jobs.tooltip.confirmCancel' | transloco"
                          (clicked)="cancelJob(row.job.id); $event.stopPropagation()"
                        >
                          @if (cancelingJobIds().has(row.job.id)) {
                            {{ 'jobs.action.canceling' | transloco }}
                          } @else {
                            {{ 'jobs.action.sure' | transloco }}
                          }
                        </app-button>
                      } @else {
                        <app-button
                          variant="warning"
                          size="sm"
                          [loading]="cancelingJobIds().has(row.job.id)"
                          [ariaLabel]="'jobs.tooltip.cancelJob' | transloco"
                          (clicked)="confirmCancel(row.job.id); $event.stopPropagation()"
                        >
                          @if (cancelingJobIds().has(row.job.id)) {
                            {{ 'jobs.action.canceling' | transloco }}
                          } @else {
                            {{ 'jobs.action.cancel' | transloco }}
                          }
                        </app-button>
                      }
                    }
                    @if (row.job.status === 'completed' && !row.job.project_id) {
                      <app-button
                        variant="info"
                        size="sm"
                        [ariaLabel]="'jobs.tooltip.promoteJob' | transloco"
                        (clicked)="togglePromote(row.job.id); $event.stopPropagation()"
                      >
                        {{ 'jobs.action.promote' | transloco }}
                      </app-button>
                    }
                    @if (row.job.status !== 'processing' && row.job.status !== 'paused' && row.job.status !== 'reviewing' && row.job.status !== 'waiting') {
                      <app-button
                        variant="danger"
                        size="sm"
                        [ariaLabel]="(confirmingDeleteId() === row.job.id ? 'jobs.tooltip.confirmDelete' : 'jobs.tooltip.deleteJob') | transloco"
                        (clicked)="confirmingDeleteId() === row.job.id ? deleteJob(row.job.id) : confirmDelete(row.job.id); $event.stopPropagation()"
                      >
                        @if (confirmingDeleteId() === row.job.id) {
                          {{ 'jobs.action.confirmDelete' | transloco }}
                        } @else {
                          {{ 'jobs.action.delete' | transloco }}
                        }
                      </app-button>
                    }
                  </td>
                </tr>
                @if (promoteJobId() === row.job.id) {
                  <tr class="promote-row" (click)="$event.stopPropagation()">
                    <td colspan="5">
                      <div class="promote-form">
                        <app-input
                          size="sm"
                          [placeholder]="'jobs.promote.namePlaceholder' | transloco"
                          [value]="promoteName()"
                          (valueChange)="promoteName.set($event)"
                        />
                        <app-input
                          size="sm"
                          [placeholder]="'jobs.promote.descriptionPlaceholder' | transloco"
                          [value]="promoteDescription()"
                          (valueChange)="promoteDescription.set($event)"
                        />
                        <app-input
                          size="sm"
                          [placeholder]="'jobs.promote.goalPlaceholder' | transloco"
                          [value]="promoteGoal()"
                          (valueChange)="promoteGoal.set($event)"
                        />
                        <app-button
                          variant="info"
                          size="sm"
                          [disabled]="!promoteName().trim()"
                          (clicked)="submitPromote(row.job.id)"
                        >
                          {{ 'jobs.action.createProject' | transloco }}
                        </app-button>
                        <app-button
                          variant="secondary"
                          size="sm"
                          (clicked)="promoteJobId.set(null)"
                        >
                          {{ 'jobs.action.cancel' | transloco }}
                        </app-button>
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
          {{ 'jobs.count' | transloco:{ filtered: filteredJobs().length, total: jobs().length } }}
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
        background: var(--panel-bg, var(--panel-bg));
      }

      /* Header */
      .header-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        flex-shrink: 0;
        flex-wrap: wrap;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
      }

      .filter-chips {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
      }

      .filter-chips .count {
        opacity: 0.7;
        font-size: 10px;
        margin-left: 2px;
      }

      .refresh-btn {
        margin-left: auto;
      }

      .snapshot-stats {
        font-size: 10px;
        color: var(--text-muted);
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
        background: var(--surface-0, var(--surface-0));
        color: var(--text-muted, #6c7086);
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.5px;
        border-bottom: 1px solid var(--border-color, var(--surface-1));
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
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        color: var(--text-primary, var(--text-primary));
        vertical-align: middle;
      }

      .job-table tbody tr {
        cursor: pointer;
        transition: background 0.15s ease;
      }

      .job-table tbody tr:hover {
        background: var(--surface-0, var(--surface-0));
      }

      .job-table tbody tr.selected {
        background: color-mix(in srgb, var(--accent-color) 20%, transparent);
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
        color: var(--text-primary, var(--text-primary));
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
        background: color-mix(in srgb, var(--accent-color) 15%, transparent);
        color: var(--accent-color);
        margin-left: 2px;
        flex-shrink: 0;
      }

      .delegation-badge {
        display: inline-block;
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 9px;
        font-weight: 500;
        letter-spacing: 0.3px;
        background: var(--info-tint);
        color: var(--info);
        margin-left: 4px;
        flex-shrink: 0;
        cursor: help;
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
        background: var(--success-tint);
        color: var(--success);
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
        text-align: right;
      }

      .actions-cell app-button {
        vertical-align: middle;
      }

      .actions-cell app-button + app-button {
        margin-left: 3px;
      }

      /* Footer */
      .footer-bar {
        display: flex;
        align-items: center;
        padding: 8px 12px;
        background: var(--surface-0, var(--surface-0));
        border-top: 1px solid var(--border-color, var(--surface-0));
        flex-shrink: 0;
      }

      .job-count {
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .promote-row td {
        padding: 0 12px 10px !important;
        border-bottom: 1px solid var(--border-color, var(--surface-0));
      }

      .promote-form {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
        padding: 8px 0;
      }

      .promote-form app-input {
        flex: 1 1 160px;
        min-width: 140px;
      }

      @media (max-width: 768px) {
        .col-created {
          display: none;
        }

        .created-cell {
          display: none;
        }

        .job-id {
          display: none;
        }

        .job-table td {
          padding: 8px 6px;
        }

        .job-table th {
          padding: 8px 6px;
        }

        .job-table {
          table-layout: fixed;
        }

        .col-prompt {
          width: 40%;
        }

        .col-status {
          width: 25%;
        }

        .col-actions {
          width: 35%;
        }

        .actions-cell {
          white-space: normal;
        }

        .prompt-text {
          font-size: 11px;
        }

        .header-bar {
          padding: 8px;
          gap: 6px;
        }

        .filter-chips {
          gap: 3px;
        }

        .table-container {
          overflow-x: hidden;
        }
      }
    `,
  ],
})
export class JobListComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);
  private readonly userService = inject(UserService);
  private readonly transloco = inject(TranslocoService);

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

  readonly statusFilters: { labelKey: string; value: StatusFilter }[] = [
    { labelKey: 'jobs.filter.all', value: 'all' },
    { labelKey: 'jobs.filter.mine', value: 'mine' },
    { labelKey: 'jobs.filter.pending_review', value: 'pending_review' },
    { labelKey: 'jobs.filter.reviewing', value: 'reviewing' },
    { labelKey: 'jobs.filter.waiting', value: 'waiting' },
    { labelKey: 'jobs.filter.created', value: 'created' },
    { labelKey: 'jobs.filter.processing', value: 'processing' },
    { labelKey: 'jobs.filter.completed', value: 'completed' },
    { labelKey: 'jobs.filter.paused', value: 'paused' },
    { labelKey: 'jobs.filter.failed', value: 'failed' },
    { labelKey: 'jobs.filter.cancelled', value: 'cancelled' },
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

  jobStatusTone(status: string): BadgeTone {
    switch (status) {
      case 'completed':
        return 'success';
      case 'processing':
      case 'pending_review':
        return 'warning';
      case 'failed':
        return 'danger';
      case 'created':
      case 'waiting':
        return 'info';
      case 'reviewing':
        return 'accent';
      case 'cancelled':
      case 'paused':
        return 'neutral';
      default:
        return 'neutral';
    }
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
    // Show IDE button only on root jobs (subjobs share the parent's workspace)
    if (job.parent_job_id) return false;
    // Show if: live VM, snapshot available, or has Gitea repo
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

  getChildCount(parentId: string): number {
    const jobs = this.filteredJobs();
    return jobs.filter((j) => j.parent_job_id === parentId).length;
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString(this.transloco.getActiveLang(), {
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
