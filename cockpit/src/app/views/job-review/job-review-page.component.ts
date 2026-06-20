import {Component, computed, effect, inject, OnInit, signal, untracked} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {DataService} from '../../core/services/data.service';
import {JobSummary} from '../../core/models/audit.model';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {JobReviewComponent} from './job-review.component';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppSpinnerComponent} from '../../ui/spinner';

function relativeTime(iso: string | null | undefined, nowLabel: string): string {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return nowLabel;
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

@Component({
  selector: 'app-job-review-page',
  standalone: true,
  imports: [
    TranslocoPipe,
    SidebarToggleComponent,
    JobReviewComponent,
    AppIconComponent,
    AppIconButtonComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="page">
      <header class="page-header">
        <div class="header-left">
          <app-sidebar-toggle />
          <h1 class="header-title">{{ 'jobReviewPage.title' | transloco }}</h1>
          @if (pendingJobs().length > 0) {
            <span class="header-count">({{ pendingJobs().length }})</span>
          }
        </div>
        <app-icon-button
          size="sm"
          [ariaLabel]="'jobReviewPage.refresh' | transloco"
          [tooltip]="'jobReviewPage.refresh' | transloco"
          [disabled]="isLoading()"
          (clicked)="refresh()"
        >
          <app-icon size="sm">refresh</app-icon>
        </app-icon-button>
      </header>

      <div class="page-body">
        <aside class="list-panel" [attr.aria-label]="'jobReviewPage.listAriaLabel' | transloco">
          @if (isLoading() && pendingJobs().length === 0) {
            <div class="state">
              <app-spinner size="md" tone="accent" />
              <span>{{ 'jobReviewPage.loading' | transloco }}</span>
            </div>
          } @else if (pendingJobs().length === 0) {
            <div class="state">
              <app-icon size="inherit" class="empty-icon">task_alt</app-icon>
              <span class="empty-text">{{ 'jobReviewPage.empty' | transloco }}</span>
            </div>
          } @else {
            @for (job of pendingJobs(); track job.id) {
              <button
                class="list-item"
                [class.selected]="currentJobId() === job.id"
                (click)="selectJob(job.id)"
              >
                <span class="item-desc">{{ job.description || job.id.slice(0, 8) }}</span>
                <span class="item-meta">
                  <span class="item-badge">{{ 'jobReviewPage.pendingBadge' | transloco }}</span>
                  <span class="item-time">{{ formatRelative(job.created_at) }}</span>
                </span>
              </button>
            }
          }
        </aside>

        <main class="detail-panel">
          <app-job-review />
        </main>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .page {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .page-header {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 12px;
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .header-left {
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 1;
        min-width: 0;
      }

      .header-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary, #cdd6f4);
        margin: 0;
      }

      .header-count {
        font-size: 12px;
        color: var(--text-muted, #6c7086);
        font-family: 'JetBrains Mono', monospace;
      }

      .page-body {
        flex: 1;
        display: grid;
        grid-template-columns: 280px 1fr;
        min-height: 0;
        overflow: hidden;
      }

      .list-panel {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 8px;
        overflow-y: auto;
        border-right: 1px solid var(--border-color, #313244);
        background: var(--panel-bg, #181825);
      }

      .detail-panel {
        min-width: 0;
        overflow: hidden;
        background: var(--panel-bg, #181825);
      }

      .state {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        padding: 32px 12px;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        text-align: center;
      }

      .empty-icon {
        font-size: 32px;
        opacity: 0.6;
      }

      .empty-text {
        max-width: 220px;
        line-height: 1.4;
      }

      .list-item {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 10px 12px;
        background: transparent;
        border: 1px solid transparent;
        border-radius: var(--radius-control);
        color: var(--text-secondary, #a6adc8);
        text-align: left;
        cursor: pointer;
        font-family: inherit;
        transition:
          background 0.15s ease,
          color 0.15s ease,
          border-color 0.15s ease;
      }

      .list-item:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .list-item.selected {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
        border-color: var(--accent-color, #cba6f7);
      }

      .item-desc {
        font-size: 13px;
        line-height: 1.3;
        overflow: hidden;
        text-overflow: ellipsis;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        word-break: break-word;
      }

      .item-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .item-badge {
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-weight: 600;
        font-size: 10px;
        /* Theme token (was the non-existent --warning-color → invisible #f9e2af
           fallback on the light theme). Mixed toward text for WCAG AA at 10px. */
        color: color-mix(in srgb, var(--warning) 55%, var(--text-primary));
      }

      .item-time {
        font-family: 'JetBrains Mono', monospace;
      }

      @media (max-width: 768px) {
        .page-body {
          grid-template-columns: 1fr;
          grid-template-rows: auto 1fr;
        }

        .list-panel {
          max-height: 200px;
          border-right: none;
          border-bottom: 1px solid var(--border-color, #313244);
        }
      }
    `,
  ],
})
export class JobReviewPageComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly data = inject(DataService);
  private readonly transloco = inject(TranslocoService);

  readonly pendingJobs = signal<JobSummary[]>([]);
  readonly isLoading = signal<boolean>(false);
  readonly currentJobId = this.data.currentJobId;

  constructor() {
    // Auto-select the first pending job when the list loads and nothing is selected.
    // `untracked` so we don't re-trigger on every list refresh after a manual selection.
    effect(() => {
      const jobs = this.pendingJobs();
      untracked(() => {
        if (jobs.length === 0) return;
        if (this.currentJobId()) return;
        this.data.setCurrentJob(jobs[0].id);
      });
    });
  }

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.isLoading.set(true);
    this.api.getJobs('pending_review').subscribe((jobs) => {
      this.pendingJobs.set(jobs);
      this.isLoading.set(false);
    });
  }

  selectJob(jobId: string): void {
    this.data.setCurrentJob(jobId);
  }

  formatRelative(iso: string | null | undefined): string {
    return relativeTime(iso, this.transloco.translate('jobReviewPage.timeNow'));
  }
}
