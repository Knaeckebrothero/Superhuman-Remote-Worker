import { Component, inject, signal, effect } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { DataService } from '../../core/services/data.service';
import { Job } from '../../core/models/api.model';
import { environment } from '../../core/environment';

interface FrozenJobData {
  summary?: string;
  deliverables?: string[];
  confidence?: number;
  notes?: string;
  phase_number?: number;
  frozen_at?: string;
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
  imports: [FormsModule],
  template: `
    <div class="review-container">
      <div class="header">
        <span class="title">Job Review</span>
        <button class="refresh-btn" (click)="loadJob()" [disabled]="isLoading()">
          &#x21bb;
        </button>
      </div>

      @if (!currentJobId()) {
        <div class="empty-state">
          <span class="empty-hint">Select a job to review</span>
        </div>
      } @else if (isLoading()) {
        <div class="loading-state">
          <div class="spinner"></div>
          <span>Loading...</span>
        </div>
      } @else if (!job()) {
        <div class="empty-state">
          <span class="empty-hint">Job not found</span>
        </div>
      } @else if (job()!.status !== 'pending_review') {
        <div class="not-review-state">
          <div class="status-info">
            <span class="status-badge" [class]="'status-' + job()!.status">
              {{ job()!.status }}
            </span>
          </div>
          <span class="status-message">
            This job is not awaiting review.
          </span>
          <span class="job-desc">{{ job()!.description }}</span>
        </div>
      } @else {
        <!-- Pending Review State -->
        <div class="review-content">
          <!-- Job Info -->
          <div class="section">
            <div class="section-header">Job</div>
            <div class="job-description">{{ job()!.description }}</div>
            <div class="job-meta">
              <span class="meta-item">ID: {{ job()!.id.slice(0, 8) }}...</span>
              <span class="meta-item">Created: {{ formatDate(job()!.created_at) }}</span>
            </div>
          </div>

          <!-- Frozen Job Summary -->
          @if (frozenData()) {
            <div class="section">
              <div class="section-header">Summary</div>
              <div class="summary-text">{{ frozenData()!.summary || 'No summary provided' }}</div>
            </div>

            <!-- Confidence -->
            @if (frozenData()!.confidence !== undefined) {
              <div class="section">
                <div class="section-header">Confidence</div>
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
                <div class="section-header">Deliverables</div>
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
                <div class="section-header">Agent Notes</div>
                <div class="notes-text">{{ frozenData()!.notes }}</div>
              </div>
            }
          } @else {
            <div class="section">
              <div class="section-header">Summary</div>
              <div class="summary-text muted">No frozen job data available</div>
            </div>
          }

          <!-- Workspace Link -->
          @if (getWorkspaceUrl()) {
            <div class="section">
              <a class="workspace-link" [href]="getWorkspaceUrl()" target="_blank">
                Browse workspace in Gitea
              </a>
            </div>
          }

          <!-- Actions -->
          <div class="actions-section">
            <!-- Approve -->
            <div class="action-group">
              <button
                class="btn approve-btn"
                (click)="approveJob()"
                [disabled]="isApproving()"
              >
                @if (isApproving()) {
                  Approving...
                } @else {
                  Approve
                }
              </button>
            </div>

            <!-- Divider -->
            <div class="divider">
              <span class="divider-text">or continue with feedback</span>
            </div>

            <!-- Feedback + Continue -->
            <div class="action-group">
              <textarea
                class="feedback-input"
                [(ngModel)]="feedbackText"
                placeholder="Write feedback or additional instructions for the agent..."
                rows="4"
              ></textarea>
              <button
                class="btn continue-btn"
                (click)="continueWithFeedback()"
                [disabled]="isResuming() || !feedbackText.trim()"
              >
                @if (isResuming()) {
                  Resuming...
                } @else {
                  Continue with Feedback
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
    `,
  ],
})
export class JobReviewComponent {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);

  readonly currentJobId = this.data.currentJobId;
  readonly job = signal<Job | null>(null);
  readonly frozenData = signal<FrozenJobData | null>(null);
  readonly isLoading = signal(false);
  readonly isApproving = signal(false);
  readonly isResuming = signal(false);
  readonly resultMessage = signal<string | null>(null);
  readonly resultIsError = signal(false);

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
    const jobId = this.currentJobId();
    const giteaUrl = environment.giteaUrl;
    if (!giteaUrl || !jobId) return null;
    return `${giteaUrl}/job-${jobId}`;
  }

  approveJob(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;

    this.isApproving.set(true);
    this.resultMessage.set(null);

    this.api.approveJob(jobId).subscribe((result) => {
      this.isApproving.set(false);
      if (result) {
        this.resultMessage.set('Job approved successfully.');
        this.resultIsError.set(false);
        // Reload to reflect new status
        this.loadJob();
      } else {
        this.resultMessage.set('Failed to approve job. Check console for details.');
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
        this.resultMessage.set('Job resumed with feedback. Agent will continue working.');
        this.resultIsError.set(false);
        this.feedbackText = '';
        // Reload to reflect new status
        this.loadJob();
      } else {
        this.resultMessage.set('Failed to resume job. Check console for details.');
        this.resultIsError.set(true);
      }
    });
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
