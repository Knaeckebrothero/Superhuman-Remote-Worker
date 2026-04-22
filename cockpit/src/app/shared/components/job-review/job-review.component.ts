import {Component, effect, inject, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../../core/services/api.service';
import {DataService} from '../../../core/services/data.service';
import {Job} from '../../../core/models/api.model';
import {environment} from '../../../core/environment';

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
  imports: [FormsModule, TranslocoPipe],
  template: `
    <div class="review-container">
      <div class="header">
        <span class="title">{{ 'jobReview.title' | transloco }}</span>
        <button class="refresh-btn" (click)="loadJob()" [disabled]="isLoading()">
          &#x21bb;
        </button>
      </div>

      @if (!currentJobId()) {
        <div class="empty-state">
          <span class="empty-hint">{{ 'jobReview.empty.selectJob' | transloco }}</span>
        </div>
      } @else if (isLoading()) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>{{ 'jobReview.loading' | transloco }}</span>
        </div>
      } @else if (!job()) {
        <div class="empty-state">
          <span class="empty-hint">{{ 'jobReview.empty.notFound' | transloco }}</span>
        </div>
      } @else if (job()!.status !== 'pending_review') {
        <div class="not-review-state">
          <div class="status-info">
            <span class="status-badge" [class]="'status-' + job()!.status">
              {{ job()!.status }}
            </span>
          </div>
          <span class="status-message">
            {{ 'jobReview.empty.notPending' | transloco }}
          </span>
          <span class="job-desc">{{ job()!.description }}</span>
        </div>
      } @else {
        <!-- Pending Review State -->
        <div class="review-content">
          <!-- Job Info -->
          <div class="section">
            <div class="section-header">{{ 'jobReview.sections.job' | transloco }}</div>
            <div class="job-description">{{ job()!.description }}</div>
            <div class="job-meta">
              <span class="meta-item">{{ 'jobReview.meta.id' | transloco: {id: job()!.id.slice(0, 8)} }}</span>
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
                <button class="workspace-link" (click)="openWorkspace()">
                  {{ 'jobReview.links.browseWorkspace' | transloco }}
                </button>
              }
              @if (hasSnapshot()) {
                @if (ideLoading()) {
                  <button class="workspace-link ide-link loading" disabled>
                    <span class="ide-spinner"></span>
                    {{ 'jobReview.links.startingIde' | transloco }}
                  </button>
                } @else {
                  <button class="workspace-link ide-link" (click)="openIde()">
                    {{ 'jobReview.links.openIde' | transloco }}
                  </button>
                }
              }
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
                  <button
                    class="btn upgrade-btn"
                    (click)="upgradeToVm()"
                    [disabled]="isUpgrading()"
                  >
                    @if (isUpgrading()) { {{ 'jobReview.vmUpgrade.upgrading' | transloco }} } @else { {{ 'jobReview.vmUpgrade.upgradeToVm' | transloco }} }
                  </button>
                  <button
                    class="btn continue-btn"
                    (click)="continueJob()"
                    [disabled]="isResuming()"
                  >
                    @if (isResuming()) { {{ 'jobReview.vmUpgrade.resuming' | transloco }} } @else { {{ 'jobReview.vmUpgrade.resumeWithoutVm' | transloco }} }
                  </button>
                </div>
              </div>
            } @else {
              <!-- Approve / Continue (depends on freeze type) -->
              <div class="action-group">
                @if (frozenData()?.freeze_type === 'phase_boundary') {
                  <button
                    class="btn continue-btn"
                    (click)="continueJob()"
                    [disabled]="isResuming()"
                  >
                    @if (isResuming()) {
                      {{ 'jobReview.actions.continuing' | transloco }}
                    } @else {
                      {{ 'jobReview.actions.continue' | transloco }}
                    }
                  </button>
                } @else {
                  @if (confirmingApprove()) {
                    <button
                      class="btn approve-btn confirming"
                      (click)="approveJob()"
                      [disabled]="isApproving()"
                    >
                      {{ 'jobReview.actions.confirmApprove' | transloco }}
                    </button>
                  } @else {
                    <button
                      class="btn approve-btn"
                      (click)="confirmApprove()"
                      [disabled]="isApproving()"
                    >
                      @if (isApproving()) {
                        {{ 'jobReview.actions.approving' | transloco }}
                      } @else {
                        {{ 'jobReview.actions.approve' | transloco }}
                      }
                    </button>
                  }
                }
              </div>
            }

            <!-- Divider -->
            <div class="divider">
              <span class="divider-text">{{ 'jobReview.actions.orContinueFeedback' | transloco }}</span>
            </div>

            <!-- Feedback + Continue -->
            <div class="action-group">
              <textarea
                class="feedback-input"
                [(ngModel)]="feedbackText"
                [placeholder]="'jobReview.actions.feedbackPlaceholder' | transloco"
                rows="4"
              ></textarea>
              <button
                class="btn continue-btn"
                (click)="continueWithFeedback()"
                [disabled]="isResuming() || !feedbackText.trim()"
              >
                @if (isResuming()) {
                  {{ 'jobReview.actions.resuming' | transloco }}
                } @else {
                  {{ 'jobReview.actions.continueWithFeedback' | transloco }}
                }
              </button>
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
        background: var(--panel-bg, #181825);
      }

      .header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        background: var(--panel-header-bg, #1e1e2e);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .title {
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn {
        margin-left: auto;
        padding: 4px 8px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 14px;
        cursor: pointer;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
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

      .spinner {
        width: 24px;
        height: 24px;
        border: 3px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      .status-badge {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 500;
        text-transform: capitalize;
      }

      .status-badge.status-created { background: rgba(137, 180, 250, 0.2); color: #89b4fa; }
      .status-badge.status-processing { background: rgba(249, 226, 175, 0.2); color: #f9e2af; }
      .status-badge.status-completed { background: rgba(166, 227, 161, 0.2); color: #a6e3a1; }
      .status-badge.status-failed { background: rgba(243, 139, 168, 0.2); color: #f38ba8; }
      .status-badge.status-cancelled { background: rgba(108, 112, 134, 0.2); color: #6c7086; }
      .status-badge.status-pending_review { background: rgba(250, 179, 135, 0.2); color: #fab387; }

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
        color: var(--text-primary, #cdd6f4);
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
        color: var(--text-primary, #cdd6f4);
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
        background: var(--surface-0, #313244);
        border-radius: 3px;
        overflow: hidden;
      }

      .confidence-fill {
        height: 100%;
        border-radius: 3px;
        transition: width 0.3s ease;
      }

      .confidence-fill.low { background: #f38ba8; }
      .confidence-fill.medium { background: #f9e2af; }
      .confidence-fill.high { background: #a6e3a1; }

      .confidence-label {
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--text-secondary, #a6adc8);
      }

      /* Deliverables */
      .deliverables-list {
        margin: 0;
        padding-left: 20px;
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
      }

      .deliverable-item {
        margin-bottom: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
      }

      /* Notes */
      .notes-text {
        font-size: 12px;
        color: var(--text-secondary, #a6adc8);
        line-height: 1.4;
        white-space: pre-wrap;
        font-style: italic;
      }

      /* Workspace Link */
      .workspace-link {
        display: inline-block;
        padding: 6px 12px;
        border: 1px solid #94e2d5;
        border-radius: 4px;
        color: #94e2d5;
        text-decoration: none;
        font-size: 12px;
        text-align: center;
        transition: background 0.15s ease;
      }

      .workspace-link:hover {
        background: rgba(148, 226, 213, 0.1);
      }

      .workspace-links {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }

      .workspace-link.ide-link {
        border-color: #89b4fa;
        color: #89b4fa;
        cursor: pointer;
        background: none;
        font: inherit;
      }

      .workspace-link.ide-link:hover {
        background: rgba(137, 180, 250, 0.1);
      }

      .workspace-link.ide-link.loading {
        color: #6c7086;
        border-color: #6c7086;
        cursor: not-allowed;
        opacity: 0.7;
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }

      .ide-spinner {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 1.5px solid #6c7086;
        border-top-color: #89b4fa;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* Actions */
      .actions-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding-top: 8px;
        border-top: 1px solid var(--border-color, #313244);
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
        background: var(--border-color, #313244);
      }

      .divider-text {
        position: relative;
        padding: 0 12px;
        background: var(--panel-bg, #181825);
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .btn {
        padding: 8px 16px;
        border: none;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .approve-btn {
        background: rgba(166, 227, 161, 0.2);
        color: #a6e3a1;
        border: 1px solid #a6e3a1;
      }

      .approve-btn:hover:not(:disabled) {
        background: rgba(166, 227, 161, 0.3);
      }

      .approve-btn.confirming {
        animation: pulse-confirm 1s ease-in-out infinite;
      }

      @keyframes pulse-confirm {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
      }

      .continue-btn {
        background: rgba(250, 179, 135, 0.2);
        color: #fab387;
        border: 1px solid #fab387;
      }

      .continue-btn:hover:not(:disabled) {
        background: rgba(250, 179, 135, 0.3);
      }

      .feedback-input {
        width: 100%;
        padding: 8px 10px;
        border: 1px solid var(--border-color, #45475a);
        border-radius: 4px;
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-family: inherit;
        resize: vertical;
        min-height: 60px;
        box-sizing: border-box;
      }

      .feedback-input::placeholder {
        color: var(--text-muted, #6c7086);
      }

      .feedback-input:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      /* Result Message */
      .result-message {
        padding: 8px 12px;
        border-radius: 4px;
        font-size: 12px;
        background: rgba(166, 227, 161, 0.15);
        color: #a6e3a1;
        border: 1px solid rgba(166, 227, 161, 0.3);
      }

      .result-message.error {
        background: rgba(243, 139, 168, 0.15);
        color: #f38ba8;
        border-color: rgba(243, 139, 168, 0.3);
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
        color: #f9e2af;
      }

      .upgrade-reason {
        color: var(--text-secondary, #a6adc8);
        font-size: 0.85em;
      }

      .upgrade-command {
        display: block;
        padding: 8px 12px;
        background: var(--code-bg, #11111b);
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        font-family: monospace;
        font-size: 0.9em;
        color: var(--text-primary, #cdd6f4);
        word-break: break-all;
      }

      .upgrade-hint {
        color: var(--text-secondary, #a6adc8);
        font-size: 0.8em;
        font-style: italic;
      }

      .upgrade-btn {
        background: rgba(249, 226, 175, 0.2) !important;
        color: #f9e2af !important;
        border: 1px solid #f9e2af !important;
      }

      .upgrade-btn:hover:not(:disabled) {
        background: rgba(249, 226, 175, 0.3) !important;
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
