import {Component, effect, inject, signal} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {DataService} from '../../core/services/data.service';
import {Job} from '../../core/models/api.model';
import {environment} from '../../core/environment';
import {AppButtonComponent} from '../../ui/button';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppBadgeComponent, type BadgeTone} from '../../ui/badge';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppSpinnerComponent} from '../../ui/spinner';
import {AppCopyFieldComponent} from '../../ui/copy-field';
import {JobDiffReviewComponent} from '../job-diff-review/job-diff-review.component';

interface FrozenJobData {
  freeze_type?: string;    // "phase_boundary" | "job_complete" | "vm_upgrade_required"
  phase_type?: string;     // "strategic" | "tactical"
  summary?: string;
  deliverables?: string[];
  confidence?: number;
  notes?: string;
  phase_number?: number;
  frozen_at?: string;
  command?: string;        // sudo command that triggered vm_upgrade_required freeze
  reason?: string;         // why the freeze happened
}

/**
 * Job Review component for approving or continuing frozen jobs.
 *
 * Displayed as a panel in the cockpit layout. When a job in `pending_review`
 * status is selected, shows the frozen job metadata and provides:
 * - Approve button (marks job as completed)
 * - Feedback textarea + Continue button (resumes with feedback)
 */
@Component({
  selector: 'app-job-review',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppTextareaComponent,
    AppSpinnerComponent,
    AppCopyFieldComponent,
    JobDiffReviewComponent,
  ],
  template: `
    <div class="review-container">
      <div class="header">
        <span class="title">{{ 'jobReview.title' | transloco }}</span>
        <app-icon-button
          variant="ghost"
          size="sm"
          class="refresh-btn"
          [ariaLabel]="'jobReview.refresh' | transloco"
          [disabled]="isLoading()"
          (clicked)="loadJob()"
        >
          ↻
        </app-icon-button>
      </div>

      @if (!currentJobId()) {
        <div class="empty-state">
          <span class="empty-hint">{{ 'jobReview.empty.selectJob' | transloco }}</span>
        </div>
      } @else if (isLoading()) {
        <div class="loading-state">
          <app-spinner size="lg" tone="accent" />
          <span>{{ 'jobReview.loading' | transloco }}</span>
        </div>
      } @else if (!job()) {
        <div class="empty-state">
          <span class="empty-hint">{{ 'jobReview.empty.notFound' | transloco }}</span>
        </div>
      } @else if (job()!.status !== 'pending_review') {
        <div class="not-review-state">
          <div class="status-info">
            <app-badge [tone]="jobStatusTone(job()!.status)" size="sm">
              {{ job()!.status }}
            </app-badge>
          </div>
          <span class="status-message">
            {{ 'jobReview.empty.notPending' | transloco }}
          </span>
          <span class="job-desc">{{ job()!.description }}</span>
        </div>
      } @else if (job()!.diff_status === 'pending') {
        <!-- Mode A diff review (see docs/done/job_cloud_export.md) -->
        <app-job-diff-review
          [jobId]="job()!.id"
          (resolved)="onDiffResolved()"
        />
      } @else {
        <!-- Pending Review State -->
        <div class="review-content">
          <!-- Job Info -->
          <div class="section">
            <div class="section-header">{{ 'jobReview.sections.job' | transloco }}</div>
            <div class="job-description">{{ job()!.description }}</div>
            <div class="job-meta">
              <app-copy-field class="meta-item" [label]="'jobReview.meta.jobId' | transloco" [value]="job()!.id" />
              <span class="meta-item">{{ 'jobReview.meta.created' | transloco: {date: formatDate(job()!.created_at)} }}</span>
            </div>
          </div>

          <!-- Frozen Job Summary -->
          @if (frozenData()) {
            <div class="section">
              <div class="section-header">{{ 'jobReview.sections.summary' | transloco }}</div>
              <div class="summary-text">{{ frozenData()!.summary || ('jobReview.summaryEmpty' | transloco) }}</div>
            </div>

            <!-- Confidence -->
            @if (frozenData()!.confidence !== undefined) {
              <div class="section">
                <div class="section-header">{{ 'jobReview.sections.confidence' | transloco }}</div>
                <div class="confidence-bar">
                  <div
                    class="confidence-fill"
                    [style.width.%]="(frozenData()!.confidence || 0) * 100"
                    [class.low]="(frozenData()!.confidence || 0) < 0.5"
                    [class.medium]="(frozenData()!.confidence || 0) >= 0.5 && (frozenData()!.confidence || 0) < 0.8"
                    [class.high]="(frozenData()!.confidence || 0) >= 0.8"
                  ></div>
                </div>
                <span class="confidence-label">{{ ((frozenData()!.confidence || 0) * 100).toFixed(0) }}%</span>
              </div>
            }

            <!-- Deliverables -->
            @if (frozenData()!.deliverables && frozenData()!.deliverables!.length > 0) {
              <div class="section">
                <div class="section-header">{{ 'jobReview.sections.deliverables' | transloco }}</div>
                <ul class="deliverables-list">
                  @for (d of frozenData()!.deliverables!; track d) {
                    <li class="deliverable-item">{{ d }}</li>
                  }
                </ul>
              </div>
            }

            <!-- Agent Notes -->
            @if (frozenData()!.notes) {
              <div class="section">
                <div class="section-header">{{ 'jobReview.sections.agentNotes' | transloco }}</div>
                <div class="notes-text">{{ frozenData()!.notes }}</div>
              </div>
            }
          } @else {
            <div class="section">
              <div class="section-header">{{ 'jobReview.sections.summary' | transloco }}</div>
              <div class="summary-text muted">{{ 'jobReview.noFrozenData' | transloco }}</div>
            </div>
          }

          <!-- Workspace Links -->
          @if (getWorkspaceUrl() || hasSnapshot()) {
            <div class="section workspace-links">
              @if (getWorkspaceUrl()) {
                <app-button variant="secondary" size="sm" (clicked)="openWorkspace()">
                  {{ 'jobReview.links.browseWorkspace' | transloco }}
                </app-button>
              }
              @if (hasSnapshot()) {
                <app-button
                  variant="info"
                  size="sm"
                  [loading]="ideLoading()"
                  (clicked)="openIde()"
                >
                  @if (ideLoading()) {
                    {{ 'jobReview.links.startingIde' | transloco }}
                  } @else {
                    {{ 'jobReview.links.openIde' | transloco }}
                  }
                </app-button>
              }
            </div>
          }

          <!-- Cloud folder preview (Mode B "open folder") -->
          @if (job()!.cloud_review_mode === 'open_folder') {
            <div class="section cloud-folder">
              <app-button
                variant="info"
                size="sm"
                [loading]="isExportingToCloud()"
                (clicked)="openCloudFolder()"
              >
                @if (isExportingToCloud()) {
                  {{ 'jobReview.actions.openingCloudFolder' | transloco }}
                } @else {
                  {{ 'jobReview.links.openCloudFolder' | transloco }}
                }
              </app-button>
              <div class="cloud-folder-hint">
                {{ 'jobReview.cloudFolderDisclaimer' | transloco }}
              </div>
            </div>
          }

          <!-- Actions -->
          <div class="actions-section">
            @if (frozenData()?.freeze_type === 'vm_upgrade_required') {
              <!-- VM Upgrade Required -->
              <div class="vm-upgrade-section">
                <div class="upgrade-info">
                  <div class="upgrade-title">{{ 'jobReview.vmUpgrade.title' | transloco }}</div>
                  <div class="upgrade-reason">
                    {{ 'jobReview.vmUpgrade.reason' | transloco }}
                  </div>
                  @if (frozenData()!.command) {
                    <code class="upgrade-command">{{ frozenData()!.command }}</code>
                  }
                  <div class="upgrade-hint">
                    {{ 'jobReview.vmUpgrade.hint' | transloco }}
                  </div>
                </div>
                <div class="action-group">
                  <app-button
                    variant="warning"
                    [loading]="isUpgrading()"
                    (clicked)="upgradeToVm()"
                  >
                    @if (isUpgrading()) {
                      {{ 'jobReview.vmUpgrade.upgrading' | transloco }}
                    } @else {
                      {{ 'jobReview.vmUpgrade.upgradeToVm' | transloco }}
                    }
                  </app-button>
                  <app-button
                    variant="secondary"
                    [loading]="isResuming()"
                    (clicked)="continueJob()"
                  >
                    @if (isResuming()) {
                      {{ 'jobReview.vmUpgrade.resuming' | transloco }}
                    } @else {
                      {{ 'jobReview.vmUpgrade.resumeWithoutVm' | transloco }}
                    }
                  </app-button>
                </div>
              </div>
            } @else {
              <!-- Approve / Continue (depends on freeze type) -->
              <div class="action-group">
                @if (frozenData()?.freeze_type === 'phase_boundary') {
                  <app-button
                    variant="warning"
                    [loading]="isResuming()"
                    (clicked)="continueJob()"
                  >
                    @if (isResuming()) {
                      {{ 'jobReview.actions.continuing' | transloco }}
                    } @else {
                      {{ 'jobReview.actions.continue' | transloco }}
                    }
                  </app-button>
                } @else {
                  <app-button
                    variant="success"
                    [loading]="isApproving()"
                    (clicked)="confirmingApprove() ? approveJob() : confirmApprove()"
                  >
                    @if (isApproving()) {
                      {{ 'jobReview.actions.approving' | transloco }}
                    } @else if (confirmingApprove()) {
                      {{ 'jobReview.actions.confirmApprove' | transloco }}
                    } @else {
                      {{ 'jobReview.actions.approve' | transloco }}
                    }
                  </app-button>
                }
              </div>
            }

            <!-- Divider -->
            <div class="divider">
              <span class="divider-text">{{ 'jobReview.actions.orContinueFeedback' | transloco }}</span>
            </div>

            <!-- Feedback + Continue -->
            <div class="action-group">
              <app-textarea
                [(value)]="feedbackText"
                [placeholder]="'jobReview.actions.feedbackPlaceholder' | transloco"
                [rows]="4"
              />
              <app-button
                variant="warning"
                [loading]="isResuming()"
                [disabled]="!feedbackText.trim()"
                (clicked)="continueWithFeedback()"
              >
                @if (isResuming()) {
                  {{ 'jobReview.actions.resuming' | transloco }}
                } @else {
                  {{ 'jobReview.actions.continueWithFeedback' | transloco }}
                }
              </app-button>
            </div>
          </div>

          <!-- Result Message -->
          @if (resultMessage()) {
            <div class="result-message" [class.error]="resultIsError()">
              {{ resultMessage() }}
            </div>
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

      .review-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--panel-bg, var(--panel-bg));
      }

      .header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, var(--surface-0));
        flex-shrink: 0;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, var(--text-primary));
      }

      .refresh-btn {
        margin-left: auto;
      }

      /* Empty / Loading States */
      .empty-state,
      .loading-state,
      .not-review-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 40px 20px;
        flex: 1;
        color: var(--text-muted, #6c7086);
        text-align: center;
      }

      .empty-hint {
        font-size: 12px;
        opacity: 0.7;
      }

      .status-message {
        font-size: 12px;
      }

      .job-desc {
        font-size: 11px;
        max-width: 300px;
        opacity: 0.6;
      }

      /* Review Content */
      .review-content {
        flex: 1;
        overflow: auto;
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }

      .section {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .section-header {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
      }

      .job-description {
        font-size: 13px;
        color: var(--text-primary, var(--text-primary));
        line-height: 1.4;
      }

      .job-meta {
        display: flex;
        gap: 12px;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-muted, #6c7086);
      }

      .summary-text {
        font-size: 12px;
        color: var(--text-primary, var(--text-primary));
        line-height: 1.5;
        white-space: pre-wrap;
      }

      .summary-text.muted {
        color: var(--text-muted, #6c7086);
        font-style: italic;
      }

      /* Confidence */
      .confidence-bar {
        height: 6px;
        background: var(--surface-0, var(--surface-0));
        border-radius: var(--radius-pill);
        overflow: hidden;
      }

      .confidence-fill {
        height: 100%;
        border-radius: var(--radius-pill);
        transition: width 0.3s ease;
      }

      .confidence-fill.low { background: var(--danger); }
      .confidence-fill.medium { background: var(--warning); }
      .confidence-fill.high { background: var(--success); }

      .confidence-label {
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-secondary, var(--text-secondary));
      }

      /* Deliverables */
      .deliverables-list {
        margin: 0;
        padding-left: 20px;
        font-size: 12px;
        color: var(--text-primary, var(--text-primary));
      }

      .deliverable-item {
        margin-bottom: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
      }

      /* Notes */
      .notes-text {
        font-size: 12px;
        color: var(--text-secondary, var(--text-secondary));
        line-height: 1.4;
        white-space: pre-wrap;
        font-style: italic;
      }

      .workspace-links {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }

      .cloud-folder {
        gap: 6px;
        align-items: flex-start;
      }

      .cloud-folder-hint {
        color: var(--text-secondary, var(--text-secondary));
        font-size: 0.8em;
        font-style: italic;
        line-height: 1.4;
      }

      /* Actions */
      .actions-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding-top: 8px;
        border-top: 1px solid var(--border-color, var(--surface-0));
      }

      .action-group {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .divider {
        text-align: center;
        position: relative;
      }

      .divider::before {
        content: '';
        position: absolute;
        left: 0;
        right: 0;
        top: 50%;
        height: 1px;
        background: var(--border-color, var(--surface-0));
      }

      .divider-text {
        position: relative;
        padding: 0 12px;
        background: var(--panel-bg, var(--panel-bg));
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      /* Result Message */
      .result-message {
        padding: 8px 12px;
        border-radius: var(--radius-control);
        font-size: 12px;
        background: var(--success-tint);
        color: var(--success);
        border: 1px solid var(--success-tint);
      }

      .result-message.error {
        background: var(--danger-tint);
        color: var(--danger);
        border-color: var(--danger-tint);
      }

      /* VM Upgrade Section */
      .vm-upgrade-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .upgrade-info {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .upgrade-title {
        font-weight: 600;
        color: var(--warning);
      }

      .upgrade-reason {
        color: var(--text-secondary, var(--text-secondary));
        font-size: 0.85em;
      }

      .upgrade-command {
        display: block;
        padding: 8px 12px;
        background: var(--code-bg, var(--timeline-bg));
        border: 1px solid var(--border-color, var(--surface-0));
        border-radius: var(--radius-tag);
        font-family: monospace;
        font-size: 0.9em;
        color: var(--text-primary, var(--text-primary));
        word-break: break-all;
      }

      .upgrade-hint {
        color: var(--text-secondary, var(--text-secondary));
        font-size: 0.8em;
        font-style: italic;
      }

    `,
  ],
})
export class JobReviewComponent {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);
  private readonly transloco = inject(TranslocoService);

  readonly currentJobId = this.data.currentJobId;
  readonly job = signal<Job | null>(null);
  readonly frozenData = signal<FrozenJobData | null>(null);
  readonly isLoading = signal(false);
  readonly isApproving = signal(false);
  readonly isResuming = signal(false);
  readonly isUpgrading = signal(false);
  readonly resultMessage = signal<string | null>(null);
  readonly resultIsError = signal(false);
  readonly confirmingApprove = signal(false);
  readonly ideLoading = signal(false);
  readonly isExportingToCloud = signal(false);
  private confirmTimeout: ReturnType<typeof setTimeout> | null = null;
  private idePollingInterval: ReturnType<typeof setInterval> | null = null;

  feedbackText = '';

  constructor() {
    // React to job selection changes
    effect(() => {
      const jobId = this.currentJobId();
      if (jobId) {
        this.loadJob();
      } else {
        this.job.set(null);
        this.frozenData.set(null);
        this.resultMessage.set(null);
      }
    });
  }

  /**
   * Called by the Mode A diff-review child after a successful
   * accept/reject. Refresh the job from the server so the parent panel
   * re-renders with the new status (completed) and the diff UI
   * disappears.
   */
  onDiffResolved(): void {
    this.loadJob();
  }

  loadJob(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isLoading.set(true);
    this.resultMessage.set(null);

    this.api.getJob(jobId).subscribe((job) => {
      this.job.set(job);
      this.isLoading.set(false);

      // If pending_review, also fetch workspace to get frozen job data
      if (job?.status === 'pending_review') {
        this.loadFrozenData(jobId);
      } else {
        this.frozenData.set(null);
      }
    });
  }

  private loadFrozenData(jobId: string): void {
    this.api.getFrozenJobData(jobId).subscribe((data) => {
      this.frozenData.set(data as FrozenJobData | null);
    });
  }

  getWorkspaceUrl(): string | null {
    const currentJob = this.job();
    const giteaUrl = environment.giteaUrl;
    if (!giteaUrl || !currentJob) return null;
    const repoName = currentJob.repo_name || `job-${currentJob.id}`;
    if (currentJob.branch_name) {
      return `${giteaUrl}/${repoName}/src/branch/${currentJob.branch_name}`;
    }
    return `${giteaUrl}/${repoName}`;
  }

  openWorkspace(): void {
    const currentJob = this.job();
    if (!currentJob) return;
    const url = this.getWorkspaceUrl();
    if (!url) return;
    this.api.ensureWorkspaceAccess(currentJob.id).subscribe(() => {
      window.open(url, '_blank');
    });
  }

  hasSnapshot(): boolean {
    const currentJob = this.job();
    if (!currentJob) return false;
    // Hide IDE on subjobs — they share the parent's workspace
    if (currentJob.parent_job_id) return false;
    // Show IDE button if: live VM, snapshot available, or has Gitea repo
    if (currentJob.status === 'processing') return true;
    const snapshotStatus = currentJob.context?.['snapshot']?.['status'];
    if (snapshotStatus === 'available') return true;
    return !!currentJob.repo_name;
  }

  openIde(): void {
    const currentJob = this.job();
    if (!currentJob) return;
    const jobId = currentJob.id;

    this.ideLoading.set(true);

    this.api.getIdeSession(jobId).subscribe((result) => {
      if (!result) {
        this.ideLoading.set(false);
        return;
      }

      if (result.status === 'active' || result.status === 'idle') {
        this.ideLoading.set(false);
        if (result.code_server_url) {
          window.open(result.code_server_url, '_blank');
        }
        return;
      }

      if (result.status === 'available' || result.status === 'expired' || result.status === 'failed') {
        this.api.startIdeSession(jobId).subscribe((startResult) => {
          if (!startResult || startResult.status === 'unavailable' || startResult.status === 'failed') {
            this.ideLoading.set(false);
            return;
          }
          this.pollIdeSession(jobId);
        });
        return;
      }

      if (result.status === 'restoring') {
        this.pollIdeSession(jobId);
        return;
      }

      this.ideLoading.set(false);
    });
  }

  private pollIdeSession(jobId: string): void {
    if (this.idePollingInterval) clearInterval(this.idePollingInterval);

    this.idePollingInterval = setInterval(() => {
      this.api.getIdeSession(jobId).subscribe((result) => {
        if (!result) return;

        if (result.status === 'active' || result.status === 'idle') {
          if (this.idePollingInterval) clearInterval(this.idePollingInterval);
          this.idePollingInterval = null;
          this.ideLoading.set(false);
          if (result.code_server_url) {
            window.open(result.code_server_url, '_blank');
          }
        } else if (result.status === 'failed' || result.status === 'unavailable') {
          if (this.idePollingInterval) clearInterval(this.idePollingInterval);
          this.idePollingInterval = null;
          this.ideLoading.set(false);
        }
      });
    }, 3000);
  }

  confirmApprove(): void {
    this.confirmingApprove.set(true);
    if (this.confirmTimeout) clearTimeout(this.confirmTimeout);
    this.confirmTimeout = setTimeout(() => this.confirmingApprove.set(false), 3000);
  }

  approveJob(): void {
    this.confirmingApprove.set(false);
    if (this.confirmTimeout) {
      clearTimeout(this.confirmTimeout);
      this.confirmTimeout = null;
    }

    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isApproving.set(true);
    this.resultMessage.set(null);

    this.api.approveJob(jobId).subscribe((result) => {
      this.isApproving.set(false);
      if (result) {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.approved'));
        this.resultIsError.set(false);
        // Reload to reflect new status
        this.loadJob();
      } else {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.approveFailed'));
        this.resultIsError.set(true);
      }
    });
  }

  upgradeToVm(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isUpgrading.set(true);
    this.resultMessage.set(null);

    this.api.upgradeJobToVm(jobId).subscribe((result) => {
      this.isUpgrading.set(false);
      if (result) {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.upgradeInitiated'));
        this.resultIsError.set(false);
        this.loadJob();
      } else {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.upgradeFailed'));
        this.resultIsError.set(true);
      }
    });
  }

  continueJob(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isResuming.set(true);
    this.resultMessage.set(null);

    this.api.resumeJob(jobId).subscribe((result) => {
      this.isResuming.set(false);
      if (result) {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.continuing'));
        this.resultIsError.set(false);
        this.loadJob();
      } else {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.continueFailed'));
        this.resultIsError.set(true);
      }
    });
  }

  continueWithFeedback(): void {
    const jobId = this.currentJobId();
    if (!jobId || !this.feedbackText.trim()) return;

    this.isResuming.set(true);
    this.resultMessage.set(null);

    this.api.resumeJob(jobId, this.feedbackText.trim()).subscribe((result) => {
      this.isResuming.set(false);
      if (result) {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.resumedWithFeedback'));
        this.resultIsError.set(false);
        this.feedbackText = '';
        // Reload to reflect new status
        this.loadJob();
      } else {
        this.resultMessage.set(this.transloco.translate('jobReview.messages.resumeFailed'));
        this.resultIsError.set(true);
      }
    });
  }

  openCloudFolder(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isExportingToCloud.set(true);
    this.resultMessage.set(null);

    // Reuses the Mode B export endpoint, which copies the job's declared
    // deliverables into a shared cloud folder and returns its browser URL. The
    // endpoint is re-syncable, so re-clicking after a resume-with-feedback
    // refreshes the same folder. exportJobToSharedFolder toasts success/error.
    this.api.exportJobToSharedFolder(jobId).subscribe((result) => {
      this.isExportingToCloud.set(false);
      if (result) {
        const url = result.folder?.browser_url;
        if (url) {
          window.open(url, '_blank', 'noopener');
        }
        this.resultIsError.set(false);
        // Refresh so exported_at ("last synced at") reflects the sync.
        this.loadJob();
      } else {
        this.resultMessage.set(
          this.transloco.translate('jobReview.messages.openCloudFolderFailed'),
        );
        this.resultIsError.set(true);
      }
    });
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
}
